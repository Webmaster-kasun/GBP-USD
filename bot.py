"""
BETAX Gold Bot — Supply & Demand Zone Strategy
===============================================
Rebuilt from 312 real BETAX trader trades (Jan 2025 – Apr 2026).

KEY DIFFERENCES FROM ORIGINAL (CPR Breakout) BOT:
  OLD: CPR breakout + EMA + RSI + volume confirmations
  NEW: Supply/Demand zone identification + zone rejection entry

BETAX TRADER'S EXACT RULES (reverse engineered):
  1. XAUUSD only — 100% specialization
  2. Limit orders inside 3-4 pip zone window (not market)
  3. BUY zones: high → low entry (buying demand pullback)
  4. SELL zones: low → high entry (selling supply push)
  5. SL beyond zone + breakeven management after TP1
  6. Long bias — BUY WR 66.3% vs SELL WR 55.8%
  7. SL scales with Gold price: 70p @$2750, 100p @$5000+

SL/TP STRUCTURE:
  SL  = ATR-adaptive (scales with Gold price level)
  TP1 = 1.5× SL → move SL to breakeven
  TP2 = 2.5× SL → full close (avg BETAX win = 171 pips)
  Min RR enforced: 1.5:1

SESSIONS (SGT):
  Asian:  09:00-13:00  (lower threshold, still trade)
  London: 14:00-19:00  (BEST — most BETAX wins here)
  NY:     20:00-23:00  (BEST for macro moves)

Bot runs every 5 minutes via Railway / GitHub Actions cron.
"""

import os
import json
import logging
import time
import requests
from datetime import datetime, timedelta
import pytz

from oanda_trader import OandaTrader
from signals import BetaxSignalEngine
from telegram_alert import TelegramAlert
from calendar_filter import EconomicCalendar


# ─── Logging ─────────────────────────────────────────────────────────────────

class SafeFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        key = os.environ.get("OANDA_API_KEY", "")
        if key and key in msg:
            msg = msg.replace(key, "***")
        return msg


handler      = logging.StreamHandler()
handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(message)s"))
file_handler = logging.FileHandler("betax_performance.txt")
file_handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler, file_handler])
log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────────────

ASSETS = {
    "XAU_USD": {
        "instrument":    "XAU_USD",
        "asset":         "XAUUSD",
        "emoji":         "🥇",
        "setting":       "trade_gold",
        "pip":           0.01,
        "precision":     2,
        "session_hours": [(9, 23)],
    },
}

# ─── FIXED LOT SIZING for S$1000/month target ────────────────────────────────
# Based on 312-trade backtest analysis:
#   Avg net pips/month (positive months) = 1,570 pips
#   Units needed to generate $740 USD (S$1000) from 1,570 pips:
#     units = 740 / (1570 × $0.01) = 47 units
#   SL per trade  = ~S$44  (avg 65.7 pips × $0.01 × 47 × 1.35)
#   TP per trade  = ~S$120 (avg 171 pips  × $0.01 × 47 × 1.35)
#
# Min recommended capital: S$4,400  (1% risk rule: SL = 1% of capital)
# Comfortable capital:     S$8,800  (0.5% risk rule — more breathing room)
#
# To change target, scale units proportionally:
#   S$500/month  → 24 units
#   S$1000/month → 47 units  ← CURRENT
#   S$2000/month → 94 units
#   S$3000/month → 141 units
FIXED_UNITS         = 47      # Fixed units per trade (OANDA micro-lots)
RISK_USD_PER_TRADE  = 32.51   # Expected max loss per trade in USD (~S$44)
RISK_SGD_PER_TRADE  = 43.89   # For display in Telegram alerts

# ─── BETAX SL model ───────────────────────────────────────────────────────────
# From 312-trade analysis: SL grew proportionally with Gold price.
# Gold ~$2750 (early 2025) → 35-50 pips
# Gold ~$3500 (mid 2025)   → 70 pips  (standardized)
# Gold ~$4000 (late 2025)  → 90-100 pips
# Gold ~$5000 (2026)       → 100 pips
# Formula: SL = max(ATR × 1.2, price_level_floor)
ATR_SL_MULT  = 1.2
SL_FLOOR_MAP = [        # (gold_price_above, min_sl_pips)
    (5000, 100),
    (4500, 100),
    (4000,  90),
    (3500,  70),
    (3000,  60),
    (    0,  50),
]

# BETAX TP ratios (from avg win=171p vs avg sl=66p → 2.6× but uses TP1/TP2 structure)
TP1_RATIO = 1.5   # Move SL to BE after TP1
TP2_RATIO = 2.5   # Final target (bot uses TP2 as hard limit order)

# ATR gates
ATR_MIN_MAIN  = 300
ATR_MIN_ASIAN = 200
ATR_MAX       = 4000


# ─── Helpers ──────────────────────────────────────────────────────────────────

def betax_sl_pips(price, raw_atr):
    """
    Compute SL in pips using BETAX's adaptive model.
    Higher gold price = wider SL to accommodate larger ranges.
    """
    # Price-level floor
    floor = 50
    for (threshold, sl) in SL_FLOOR_MAP:
        if price >= threshold:
            floor = sl
            break

    if raw_atr is not None:
        atr_sl = int(raw_atr * ATR_SL_MULT)
        return max(floor, min(atr_sl, 120))  # cap at 120 for sanity
    return floor


def calc_position_size(balance, stop_pips, pip, score):
    """
    Fixed lot size targeting S$1,000/month.
    47 units on OANDA = ~S$44 max loss per trade, ~S$120 avg win.
    Based on 312-trade backtest: avg 1,570 net pips/month x 47 units x $0.01 = $737 USD = ~S$995.

    To scale your target:
      S$500/month  -> set FIXED_UNITS = 24
      S$1000/month -> set FIXED_UNITS = 47  (current)
      S$2000/month -> set FIXED_UNITS = 94
      S$3000/month -> set FIXED_UNITS = 141
    """
    units = FIXED_UNITS
    actual_risk = round(stop_pips * pip * units, 2)
    log.info("Fixed sizing: %d units | stop=%dp | risk=$%.2f USD (S$%.2f)",
             units, stop_pips, actual_risk, actual_risk * 1.35)
    return units


def get_atr_pips(trader, instrument, pip):
    try:
        url    = trader.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": "30", "granularity": "H1", "price": "M"}
        r      = requests.get(url, headers=trader.headers, params=params, timeout=10)
        if r.status_code != 200:
            return None
        c      = [x for x in r.json()["candles"] if x["complete"]]
        if len(c) < 15:
            return None
        highs  = [float(x["mid"]["h"]) for x in c]
        lows   = [float(x["mid"]["l"]) for x in c]
        closes = [float(x["mid"]["c"]) for x in c]
        trs    = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                  for i in range(1, len(closes))]
        atr      = sum(trs[-14:]) / 14
        atr_pips = round(atr / pip)
        log.info("%s ATR=%.4f pips=%d", instrument, atr, atr_pips)
        return max(atr_pips, 10)
    except Exception as e:
        log.warning("ATR error: %s", e)
        return None


def check_spread(trader, instrument, max_spread_pips, pip):
    try:
        mid, bid, ask = trader.get_price(instrument)
        if bid is None:
            return True, 0
        spread_pips = (ask - bid) / pip
        log.info("%s spread=%.1f pips", instrument, spread_pips)
        return (spread_pips <= max_spread_pips), spread_pips
    except Exception as e:
        log.warning("Spread error: %s", e)
        return True, 0


def load_settings():
    default = {
        "max_trades_day":           6,      # BETAX: max 6 trades per day observed
        "signal_threshold":         5,      # Need 5/8 London/NY
        "signal_threshold_asian":   4,      # Need 4/8 Asian
        "demo_mode":                True,
        "trade_gold":               True,
        "trade_gold_asian":         True,
        "max_consec_losses":        3,      # Stop after 3 straight losses
        "max_spread_gold":          50,     # 50 pips max spread (Gold)
        "max_spread_gold_asian":    80,
        "strategy":                 "betax_supply_demand_zone",
        "max_trades_asian":         2,      # BETAX rarely takes >2 Asian trades
        "max_trades_main":          4,      # Up to 4 London/NY
        "tp1_be_move":              True,   # Move SL to BE after TP1 (BETAX core rule)
    }
    try:
        with open("settings.json") as f:
            saved = json.load(f)
            default.update(saved)
    except FileNotFoundError:
        with open("settings.json", "w") as f:
            json.dump(default, f, indent=2)
    return default


# ─── Trade sync ───────────────────────────────────────────────────────────────

def sync_closed_trades(trader, today, trade_log):
    """Sync W/L from OANDA. Does NOT touch trade counter or entry price."""
    try:
        from datetime import timezone
        sg_tz         = pytz.timezone("Asia/Singapore")
        now_sg        = datetime.now(sg_tz)
        day_start     = now_sg.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

        url    = trader.base_url + "/v3/accounts/" + trader.account_id + "/trades"
        params = {"state": "CLOSED", "instrument": "XAU_USD", "count": "20"}
        r      = requests.get(url, headers=trader.headers, params=params, timeout=10)
        if r.status_code != 200:
            return

        trades = r.json().get("trades", [])
        wins = losses = 0
        for t in trades:
            if t.get("closeTime", "") < day_start_utc:
                continue
            pl = float(t.get("realizedPL", 0))
            if pl > 0:   wins   += 1
            elif pl < 0: losses += 1

        today["wins"]   = wins
        today["losses"] = losses

        consec = 0
        for t in sorted(trades, key=lambda x: x.get("closeTime", ""), reverse=True):
            if t.get("closeTime", "") < day_start_utc:
                break
            if float(t.get("realizedPL", 0)) < 0:
                consec += 1
            else:
                break
        today["consec_losses"] = consec

        # Update last trade result
        today_closed = [t for t in trades if t.get("closeTime", "") >= day_start_utc]
        if today_closed:
            latest = sorted(today_closed, key=lambda x: x.get("closeTime", ""))[-1]
            today["last_trade_close_result"] = "WIN" if float(latest.get("realizedPL", 0)) > 0 else "LOSS"
            today["last_trade_close_time"]   = latest.get("closeTime", "")

            # Next-session win lock (from original FIX 13)
            if float(latest.get("realizedPL", 0)) > 0:
                try:
                    from datetime import timezone as _tz
                    import pytz as _pytz
                    sg_tz         = _pytz.timezone("Asia/Singapore")
                    close_raw     = latest.get("closeTime", "")
                    close_dt      = datetime.strptime(close_raw[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=_tz.utc)
                    close_sgt     = close_dt.astimezone(sg_tz)
                    h             = close_sgt.hour
                    if h < 9:
                        next_session_sgt = close_sgt.replace(hour=9, minute=0, second=0, microsecond=0)
                    elif h < 14:
                        next_session_sgt = close_sgt.replace(hour=14, minute=0, second=0, microsecond=0)
                    elif h < 20:
                        next_session_sgt = close_sgt.replace(hour=20, minute=0, second=0, microsecond=0)
                    else:
                        next_session_sgt = (close_sgt + timedelta(days=1)).replace(
                            hour=9, minute=0, second=0, microsecond=0)
                    next_session_utc = next_session_sgt.astimezone(_tz.utc)
                    today["last_win_candle_close"] = next_session_utc.strftime("%Y-%m-%dT%H:%M")
                    log.info("Win lock: blocked until %s SGT", next_session_sgt.strftime("%H:%M"))
                except Exception as e:
                    log.warning("Win lock set error: %s", e)

        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

        log.info("Synced W=%d L=%d consec=%d", wins, losses, consec)

    except Exception as e:
        log.warning("Sync error: %s", e)


# ─── Daily summary ────────────────────────────────────────────────────────────

def send_daily_summary(alert, today, mode):
    try:
        wins     = today.get("wins", 0)
        losses   = today.get("losses", 0)
        total    = wins + losses
        wr       = round(wins / total * 100) if total > 0 else 0
        realized = today.get("daily_pnl", 0.0)
        pnl_sgd  = round(realized * 1.35, 2)
        emoji    = "🟢" if realized >= 0 else "🔴"

        msg = (
            "🥇 BETAX Gold Bot — Daily Summary\n"
            "Strategy: Supply & Demand Zones\n"
            "---------------------------------\n"
            "Mode:     " + mode + "\n"
            "Trades:   " + str(total) + "\n"
            "W / L:    " + str(wins) + " / " + str(losses) + "\n"
            "Win Rate: " + str(wr) + "%\n"
            "---------------------------------\n"
            "P&L: " + emoji + " $" + str(round(realized, 2)) + " USD\n"
            "     " + emoji + " $" + str(pnl_sgd) + " SGD\n"
            "---------------------------------\n"
            "Bot resumes 9am SGT tomorrow"
        )
        alert.send(msg)
    except Exception as e:
        log.warning("Daily summary error: %s", e)


# ─── Main bot loop ────────────────────────────────────────────────────────────

def run_bot():
    log.info("🥇 BETAX Gold Bot — Supply & Demand Zone Strategy scanning...")
    settings = load_settings()
    sg_tz    = pytz.timezone("Asia/Singapore")
    now      = datetime.now(sg_tz)
    hour     = now.hour
    alert    = TelegramAlert()

    # Weekend guard
    if now.weekday() == 5:
        log.info("Saturday — markets closed")
        return
    if now.weekday() == 6 and hour < 9:
        log.info("Sunday pre-open — skipping")
        return

    # Session labels
    asian       = (9 <= hour <= 13)
    london_open = (14 <= hour <= 17)
    london      = (14 <= hour <= 19)
    ny_overlap  = (20 <= hour <= 23)
    active      = (9 <= hour <= 23)

    if asian:
        session = "Asian Session (9am-1pm SGT)"
    elif london_open:
        session = "London Open — BEST for zone breakouts!"
    elif ny_overlap:
        session = "NY Overlap — BEST for macro zone moves!"
    elif london:
        session = "London Session"
    else:
        session = "Off-hours"

    # Connect
    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login():
        alert.send("❌ BETAX Bot: OANDA login failed\nCheck OANDA_API_KEY / OANDA_ACCOUNT_ID")
        return

    current_balance = trader.last_balance
    mode            = "DEMO" if settings["demo_mode"] else "LIVE"

    # Load daily state
    trade_log = "betax_" + now.strftime("%Y%m%d") + ".json"
    try:
        with open(trade_log) as f:
            today = json.load(f)
    except FileNotFoundError:
        today = {
            "trades":                     0,
            "start_balance":              current_balance,
            "daily_pnl":                  0.0,
            "stopped":                    False,
            "wins":                       0,
            "losses":                     0,
            "consec_losses":              0,
            "daily_summary_sent":         False,
            "news_alert_sent":            False,
            "last_trade_close_time":      None,
            "last_trade_close_result":    None,
            "last_trade_entry_price":     None,
            "last_trade_entry_time":      None,
            "last_trade_entry_score":     0,
            "last_trade_entry_direction": "",
            "asian_trades_today":         0,
            "main_trades_today":          0,
            "last_win_candle_close":      None,
            "last_entry_candle":          None,
            "last_scan_alert_min":        -61,
            "last_alert_score":           -1,
            "last_alert_direction":       "",
        }
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)
        log.info("New day! Start balance: $%.2f", current_balance)

    # PnL
    start_balance = today.get("start_balance", current_balance)
    realized_pnl  = current_balance - start_balance
    today["daily_pnl"] = realized_pnl
    with open(trade_log, "w") as f:
        json.dump(today, f, indent=2)

    sync_closed_trades(trader, today, trade_log)

    # Daily summary at 11pm
    if hour == 23 and not today.get("daily_summary_sent", False):
        send_daily_summary(alert, today, mode)
        today["daily_summary_sent"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

    if not active:
        log.info("Off-hours — sleeping")
        return

    # Max daily trades
    if today["trades"] >= settings["max_trades_day"]:
        log.info("Max daily trades reached (%d)", settings["max_trades_day"])
        return

    # Consecutive loss protection
    if today.get("consec_losses", 0) >= settings.get("max_consec_losses", 3):
        log.info("Consecutive loss limit hit — stopping for today")
        return

    # News filter
    calendar     = EconomicCalendar()
    news_summary = calendar.get_today_summary()
    if "No high" not in news_summary and not today.get("news_alert_sent"):
        alert.send("⚠️ BETAX: NEWS ALERT!\n" + news_summary + "\nAvoiding zone trades around news window!")
        today["news_alert_sent"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

    signals      = BetaxSignalEngine(demo=settings["demo_mode"])
    scan_results = []
    score        = -1
    direction    = ""
    details      = ""

    for name, config in ASSETS.items():
        if not settings.get(config["setting"], True):
            continue
        if today["trades"] >= settings["max_trades_day"]:
            break

        # Skip if position already open
        position = trader.get_position(name)
        if position:
            pnl     = trader.check_pnl(position)
            pos_dir = "BUY" if int(float(position["long"]["units"])) > 0 else "SELL"
            emoji   = "📈" if pnl > 0 else "📉"
            scan_results.append(config["emoji"] + " " + name + ": " + pos_dir + " open " + emoji + " $" + str(round(pnl, 2)))
            continue

        # Session check
        pair_ok = any(s <= hour <= e for (s, e) in config.get("session_hours", [(9, 23)]))
        if not pair_ok:
            scan_results.append(config["emoji"] + " " + name + ": off-session")
            continue

        is_asian_gold = asian and name == "XAU_USD"

        # Asian toggle
        if is_asian_gold and not settings.get("trade_gold_asian", True):
            scan_results.append(config["emoji"] + " " + name + ": Asian disabled")
            continue

        # Trade caps per session
        if is_asian_gold:
            if today.get("asian_trades_today", 0) >= settings.get("max_trades_asian", 2):
                scan_results.append(config["emoji"] + " " + name + ": Asian cap reached")
                continue
        else:
            if today.get("main_trades_today", 0) >= settings.get("max_trades_main", 4):
                scan_results.append(config["emoji"] + " " + name + ": Main session cap reached")
                continue

        # ── Next-session win lock ─────────────────────────────
        last_win_candle = today.get("last_win_candle_close")
        if last_win_candle:
            try:
                import pytz as _pytz
                sg_tz2        = _pytz.timezone("Asia/Singapore")
                unlock_utc    = datetime.strptime(last_win_candle, "%Y-%m-%dT%H:%M")
                now_utc_naive = datetime.utcnow().replace(second=0, microsecond=0)
                if now_utc_naive < unlock_utc:
                    remaining_min = max(1, int((unlock_utc - now_utc_naive).total_seconds() // 60))
                    unlock_sgt    = unlock_utc.replace(tzinfo=__import__("datetime").timezone.utc).astimezone(sg_tz2)
                    scan_results.append(config["emoji"] + " " + name +
                                        ": 🔒 Win Lock — opens at " + unlock_sgt.strftime("%H:%M SGT") +
                                        " (~" + str(remaining_min) + " min)")
                    continue
            except Exception as e:
                log.warning("Win lock check error: %s", e)

        # ── Same-candle duplicate lock ────────────────────────
        last_entry_candle = today.get("last_entry_candle")
        if last_entry_candle:
            try:
                now_utc    = datetime.utcnow()
                m30_floor  = now_utc.replace(minute=(now_utc.minute // 30) * 30, second=0, microsecond=0)
                entry_dt   = datetime.strptime(last_entry_candle, "%Y-%m-%dT%H:%M")
                if m30_floor == entry_dt:
                    scan_results.append(config["emoji"] + " " + name + ": 🔒 Same-candle lock (M30)")
                    continue
            except Exception as e:
                log.warning("Same-candle lock error: %s", e)

        # Spread check
        max_spread            = settings.get("max_spread_gold_asian", 80) if is_asian_gold else settings.get("max_spread_gold", 50)
        spread_ok, spread_val = check_spread(trader, name, max_spread, config["pip"])

        # News time check
        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            scan_results.append(config["emoji"] + " " + name + ": ⏸ PAUSED — " + news_reason)
            continue

        # ── BETAX Signal Analysis ─────────────────────────────
        asset_key = "XAUUSD_ASIAN" if is_asian_gold else config["asset"]
        threshold = settings.get("signal_threshold_asian", 4) if is_asian_gold else settings["signal_threshold"]

        score, direction, details = signals.analyze(asset=asset_key)
        log.info("%s: score=%d dir=%s | %s", name, score, direction, details[:80])

        if not spread_ok:
            scan_results.append(config["emoji"] + " " + name +
                                 ": Spread " + str(round(spread_val, 1)) + "p | Score: " + str(score) + "/8")
            continue

        if direction == "BLOCKED":
            scan_results.append(config["emoji"] + " " + name + ": 🚫 HTF blocked | " + str(score) + "/8")
            continue

        if score < threshold or direction == "NONE":
            scan_results.append(config["emoji"] + " " + name + ": " + str(score) + "/8 — no zone setup yet")
            continue

        # ── Re-entry guard (mirrors original FIX 6 + FIX 14) ─
        last_entry_price     = today.get("last_trade_entry_price") or 0
        last_entry_direction = today.get("last_trade_entry_direction", "")
        last_entry_score     = today.get("last_trade_entry_score", 0)

        if last_entry_price and last_entry_direction:
            price_now, _, _ = trader.get_price(name)
            price_moved     = (abs((price_now or 0) - last_entry_price) / config["pip"]) >= 800
            same_dir        = (direction == last_entry_direction)

            if same_dir and score <= last_entry_score and not price_moved:
                scan_results.append(config["emoji"] + " " + name +
                                     ": 🚫 Same zone chasing — " + direction + " score " + str(score) + " <= " + str(last_entry_score))
                continue
            elif same_dir and score >= 6:
                log.info("%s ALLOWED — stronger zone score %d", name, score)
                today["last_trade_entry_score"]     = 0
                today["last_trade_entry_direction"] = ""
            elif not same_dir and score >= threshold:
                log.info("%s ALLOWED — direction flip to %s", name, direction)
                today["last_trade_entry_score"]     = 0
                today["last_trade_entry_direction"] = ""
            elif price_moved and score >= threshold:
                log.info("%s ALLOWED — new zone (800p+ move)", name)
                today["last_trade_entry_score"]     = 0
                today["last_trade_entry_direction"] = ""
            else:
                scan_results.append(config["emoji"] + " " + name +
                                     ": ⏳ Re-entry blocked — " + direction + " score " + str(score) + "/8 < " + str(threshold))
                continue

        # ── Compute SL/TP ─────────────────────────────────────
        price, _, _ = trader.get_price(name)
        raw_atr     = get_atr_pips(trader, name, config["pip"])

        stop_pips = betax_sl_pips(price or 3000, raw_atr)
        tp1_pips  = int(stop_pips * TP1_RATIO)   # TP1 = 1.5× SL → move to BE
        tp2_pips  = int(stop_pips * TP2_RATIO)   # TP2 = 2.5× SL → full close

        # Enforce minimum RR
        rr = tp2_pips / stop_pips
        if rr < 1.5:
            scan_results.append(config["emoji"] + " " + name + ": RR=" + str(round(rr, 1)) + " < 1.5 — skipping")
            continue

        # Fixed size — 47 units targeting S$1,000/month
        size         = calc_position_size(current_balance, stop_pips, config["pip"], score)
        max_loss_usd = round(size * stop_pips * config["pip"], 2)
        max_loss_sgd = round(max_loss_usd * 1.35, 2)
        max_tp1_usd  = round(size * tp1_pips  * config["pip"], 2)
        max_tp1_sgd  = round(max_tp1_usd * 1.35, 2)
        max_tp2_usd  = round(size * tp2_pips  * config["pip"], 2)
        max_tp2_sgd  = round(max_tp2_usd * 1.35, 2)
        # monthly target tracker
        monthly_target_sgd = 1000
        today_pnl_sgd      = round(realized_pnl * 1.35, 2)
        remaining_sgd      = round(monthly_target_sgd - today_pnl_sgd, 2)

        # Margin check
        try:
            mr = requests.get(trader.base_url + "/v3/accounts/" + trader.account_id,
                              headers=trader.headers, timeout=10)
            if mr.status_code == 200:
                acct      = mr.json().get("account", {})
                margin_av = float(acct.get("marginAvailable", current_balance))
                max_units = int((margin_av * 0.8) / (price * 0.05)) if price else size
                if max_units < 1:
                    scan_results.append(config["emoji"] + " " + name + ": Insufficient margin")
                    continue
                if size > max_units:
                    size = max_units
        except Exception as e:
            log.warning("Margin check error: %s", e)

        # ── Place order ───────────────────────────────────────
        # BETAX uses TP2 as the hard TP limit order on broker
        result = trader.place_order(
            instrument     = name,
            direction      = direction,
            size           = size,
            stop_distance  = stop_pips,
            limit_distance = tp2_pips,
        )

        if result["success"]:
            now_utc   = datetime.utcnow()
            m30_entry = now_utc.replace(minute=(now_utc.minute // 30) * 30, second=0, microsecond=0)

            today["trades"]                    += 1
            today["consec_losses"]              = 0
            today["last_trade_entry_price"]     = price
            today["last_trade_entry_time"]      = now_utc.strftime("%Y-%m-%dT%H:%M:%S")
            today["last_trade_entry_score"]     = score
            today["last_trade_entry_direction"] = direction
            today["last_entry_candle"]          = m30_entry.strftime("%Y-%m-%dT%H:%M")

            if is_asian_gold:
                today["asian_trades_today"] = today.get("asian_trades_today", 0) + 1
            else:
                today["main_trades_today"]  = today.get("main_trades_today", 0) + 1

            with open(trade_log, "w") as f:
                json.dump(today, f, indent=2)

            # Format entry zone (BETAX-style XX-XX)
            pip = config["pip"]
            if direction == "BUY":
                zone_str = str(round(price, 2)) + "→" + str(round(price - 4 * pip, 2))
            else:
                zone_str = str(round(price, 2)) + "→" + str(round(price + 4 * pip, 2))

            alert.send(
                "🥇 BETAX ZONE TRADE! " + mode + "\n"
                "═══════════════════════\n"
                "Direction:  " + direction + "\n"
                "Score:      " + str(score) + "/8\n"
                "Entry zone: " + zone_str + "\n"
                "Entry px:   " + str(round(price, config["precision"])) + "\n"
                "Lot size:   " + str(size) + " units (fixed)\n"
                "ATR:        " + str(raw_atr) + "p\n"
                "─────────────────────\n"
                "🔴 SL:  " + str(stop_pips) + "p\n"
                "        $" + str(max_loss_usd) + " USD\n"
                "        S$" + str(max_loss_sgd) + " SGD\n"
                "─────────────────────\n"
                "🟡 TP1: " + str(tp1_pips) + "p → MOVE SL TO ENTRY\n"
                "        $" + str(max_tp1_usd) + " USD\n"
                "        S$" + str(max_tp1_sgd) + " SGD\n"
                "─────────────────────\n"
                "🟢 TP2: " + str(tp2_pips) + "p (hard limit)\n"
                "        $" + str(max_tp2_usd) + " USD\n"
                "        S$" + str(max_tp2_sgd) + " SGD\n"
                "─────────────────────\n"
                "R:R:        1:" + str(round(rr, 1)) + "\n"
                "Spread:     " + str(round(spread_val, 1)) + "p\n"
                "Trade:      #" + str(today["trades"]) + "/" + str(settings["max_trades_day"]) + " today\n"
                "Session:    " + session + "\n"
                "═══════════════════════\n"
                "🎯 S$1000/month target\n"
                "   Today P&L: S$" + str(today_pnl_sgd) + "\n"
                "   Remaining: S$" + str(remaining_sgd) + "\n"
                "═══════════════════════\n"
                "📌 After TP1 hit — move SL to entry price\n"
                "--- Signals ---\n" + details.replace(" | ", "\n")
            )
            scan_results.append(config["emoji"] + " " + name + ": " + direction + " ZONE PLACED! " + str(score) + "/8")

        else:
            log.warning("%s order failed: %s", name, result.get("error", ""))
            scan_results.append(config["emoji"] + " " + name + ": order failed — " + str(result.get("error", ""))[:50])

    # ── Scan status alert ─────────────────────────────────────
    pl_sgd   = realized_pnl * 1.35
    pnl_emoji = "✅" if realized_pnl >= 0 else "❌"
    wins     = today.get("wins", 0)
    losses   = today.get("losses", 0)

    if realized_pnl >= 59:
        target_msg = "TARGET HIT! $" + str(round(pl_sgd, 0)) + " SGD today!"
    elif realized_pnl > 0:
        target_msg = "Profit $" + str(round(pl_sgd, 0)) + " SGD"
    elif realized_pnl < 0:
        target_msg = "Loss $" + str(abs(round(pl_sgd, 0))) + " SGD"
    else:
        target_msg = "Scanning for zone setups..."

    summary = "\n".join(scan_results) if scan_results else "No zone setups this scan"

    threshold_used    = settings.get("signal_threshold_asian", 4) if asian else settings["signal_threshold"]
    trade_just_placed = any("PLACED" in r for r in scan_results)
    last_alert_min    = today.get("last_scan_alert_min", -61)
    last_alert_score  = today.get("last_alert_score", -1)
    last_alert_dir    = today.get("last_alert_direction", "")
    current_min       = now.hour * 60 + now.minute
    mins_since_alert  = (current_min - last_alert_min
                         if current_min >= last_alert_min
                         else current_min + 1440 - last_alert_min)
    score_changed = (score != last_alert_score or direction != last_alert_dir)
    should_alert  = trade_just_placed or score_changed or mins_since_alert >= 60

    if should_alert:
        today["last_scan_alert_min"]  = current_min
        today["last_alert_score"]     = score
        today["last_alert_direction"] = direction
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

        signal_detail = ""
        if score > 0 and details:
            signal_detail = "--- Zone Signals ---\n" + details.replace(" | ", "\n") + "\n"

        alert.send(
            "🥇 BETAX Bot Scan! " + mode + "\n"
            "Strategy: Supply & Demand Zones\n"
            "Time: " + now.strftime("%H:%M SGT") + " | " + session + "\n"
            "Balance: $" + str(round(current_balance, 2)) +
            " | P&L: $" + str(round(realized_pnl, 2)) + " " + pnl_emoji + "\n"
            "Trades: " + str(today["trades"]) + "/" + str(settings["max_trades_day"]) +
            " | W/L: " + str(wins) + "/" + str(losses) + "\n"
            "Need: " + str(threshold_used) + "/8 to trade\n"
            + target_msg + "\n"
            "-------------------------\n"
            "--- Setups ---\n"
            + summary + "\n"
            + signal_detail
        )
    else:
        log.info("Scan silent — next alert in %d mins", 60 - mins_since_alert)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("🥇 BETAX Gold Bot starting — scanning every 5 minutes...")
    while True:
        try:
            run_bot()
        except Exception as e:
            log.error("Bot error: %s", e)
        log.info("Sleeping 5 minutes...")
        time.sleep(300)
