"""
OANDA Demo 2 — M15 Scalp Bot
==============================
ALL PAIRS → M15 Scalp Strategy
  AUD/USD → Asian  6am-11am SGT
  EUR/GBP → London 2pm-7pm  SGT
  EUR/USD → London 2pm-6pm  SGT

Key rules:
- Score 3/3 to trade
- SL 3-5 pip | TP 8-10 pip | R:R ~2:1
- Spread gate ≤ 1.2 pip
- 1 trade open per pair max
- 30 min cooldown after SL hit
- Silent outside session hours / weekends
"""

import os
import json
import time
import logging
import requests
from datetime import datetime
import pytz

from signals        import SignalEngine
from oanda_trader   import OandaTrader
from telegram_alert import TelegramAlert
from calendar_filter import EconomicCalendar as CalendarFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

sg_tz   = pytz.timezone("Asia/Singapore")
signals = SignalEngine()

# ── ASSET CONFIG ─────────────────────────────────────────────────────────────
ASSETS = {
    "AUD_USD": {
        "instrument":    "AUD_USD",
        "asset":         "AUDUSD",
        "emoji":         "🦘",
        "strategy":      "scalp_m15",
        "strategy_label":"SCALP",
        "max_score":     3,
        "pip":           0.0001,
        "precision":     5,
        "stop_pips":     4,    # 3-5 pip SL — tight scalp
        "tp_pips":       8,    # 8-10 pip TP — R:R 2:1
        "session_start": 6,
        "session_end":   11,
    },
    "EUR_GBP": {
        "instrument":    "EUR_GBP",
        "asset":         "EURGBP",
        "emoji":         "🇪🇺",
        "strategy":      "scalp_m15",
        "strategy_label":"SCALP",
        "max_score":     3,
        "pip":           0.0001,
        "precision":     5,
        "stop_pips":     3,    # EUR/GBP moves tight — 3 pip SL
        "tp_pips":       8,    # R:R ~2.7:1
        "session_start": 14,
        "session_end":   19,
    },
    "EUR_USD": {
        "instrument":    "EUR_USD",
        "asset":         "EURUSD",
        "emoji":         "🇪🇺💵",
        "strategy":      "scalp_m15",
        "strategy_label":"SCALP",
        "max_score":     3,
        "pip":           0.0001,
        "precision":     5,
        "stop_pips":     4,    # 4 pip SL
        "tp_pips":       9,    # R:R ~2.3:1
        "session_start": 14,
        "session_end":   18,
    },
}

DEFAULT_SETTINGS = {
    "signal_threshold": 3,      # scalp needs 3/3
    "demo_mode":        True,
    "max_spread_pips":  1.2,    # tight spread gate for scalp
}

def load_settings():
    try:
        with open("settings.json") as f:
            saved = json.load(f)
        DEFAULT_SETTINGS.update(saved)
    except FileNotFoundError:
        with open("settings.json", "w") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
    return DEFAULT_SETTINGS

def is_in_session(hour, config):
    return config["session_start"] <= hour < config["session_end"]

def set_cooldown(today, name, minutes=30):
    if "cooldowns" not in today: today["cooldowns"] = {}
    today["cooldowns"][name] = datetime.now(sg_tz).isoformat()
    log.info(name + " cooldown set for " + str(minutes) + " mins")

def in_cooldown(today, name):
    cd = today.get("cooldowns", {}).get(name)
    if not cd: return False
    try:
        cd_time = datetime.fromisoformat(cd).replace(tzinfo=sg_tz)
        elapsed = (datetime.now(sg_tz) - cd_time).total_seconds() / 60
        return elapsed < 30
    except:
        return False

def detect_sl_tp_hits(today, trader, trade_log, alert):
    """
    Check if any tracked trades were closed by OANDA (SL or TP hit)
    Sets cooldown if SL hit, updates W/L counter
    """
    if "open_times" not in today: return
    for name in list(today["open_times"].keys()):
        pos = trader.get_position(name)
        if pos: continue  # Still open

        # Trade was tracked but now closed → OANDA hit SL or TP
        try:
            url  = (trader.base_url + "/v3/accounts/" + trader.account_id +
                    "/trades?state=CLOSED&instrument=" + name + "&count=1")
            resp = requests.get(url, headers=trader.headers, timeout=10)
            data = resp.json().get("trades", [])
            if data:
                realized = float(data[0].get("realizedPL", "0"))
                if realized < 0:
                    # SL hit
                    set_cooldown(today, name)
                    today["losses"]        = today.get("losses", 0) + 1
                    today["consec_losses"] = today.get("consec_losses", 0) + 1
                    alert.send(
                        "🔴 DEMO 2 SL HIT\n"
                        + ASSETS.get(name, {}).get("emoji", "") + " " + name + "\n"
                        "Loss:     $" + str(round(realized, 2)) + "\n"
                        "⏳ Cooldown: 30 mins\n"
                        "W/L: " + str(today.get("wins",0)) + "/" + str(today.get("losses",0))
                    )
                    log.info(name + " SL hit $" + str(round(realized,2)) + " — cooldown set")
                else:
                    # TP hit
                    today["wins"]          = today.get("wins", 0) + 1
                    today["consec_losses"] = 0
                    alert.send(
                        "✅ DEMO 2 TP HIT\n"
                        + ASSETS.get(name, {}).get("emoji", "") + " " + name + "\n"
                        "Profit:   $+" + str(round(realized, 2)) + "\n"
                        "W/L: " + str(today.get("wins",0)) + "/" + str(today.get("losses",0))
                    )
                    log.info(name + " TP hit $" + str(round(realized,2)))
        except Exception as e:
            log.warning("SL/TP detect error " + name + ": " + str(e))

        del today["open_times"][name]
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

def run_bot():
    settings = load_settings()
    now      = datetime.now(sg_tz)
    hour     = now.hour
    alert    = TelegramAlert()
    calendar = CalendarFilter()

    log.info("Scan at " + now.strftime("%H:%M SGT"))

    # ── WEEKEND — silent ─────────────────────────────────────────────
    if now.weekday() == 5:
        log.info("Saturday — sleeping silently")
        return
    if now.weekday() == 6 and hour < 5:
        log.info("Sunday early — sleeping silently")
        return

    # ── CHECK ACTIVE SESSIONS ─────────────────────────────────────────
    active = [n for n, c in ASSETS.items() if is_in_session(hour, c)]
    if not active:
        log.info("No active sessions at " + str(hour) + ":00 SGT — silent")
        return

    # ── CONNECT ───────────────────────────────────────────────────────
    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login():
        alert.send("DEMO 2 Login FAILED!")
        return

    current_balance = trader.get_balance()

    # ── LOAD TODAY LOG ────────────────────────────────────────────────
    trade_log = "trades_" + now.strftime("%Y%m%d") + ".json"
    try:
        with open(trade_log) as f:
            today = json.load(f)
    except FileNotFoundError:
        today = {
            "trades":        0,
            "start_balance": current_balance,
            "wins":          0,
            "losses":        0,
            "consec_losses": 0,
            "cooldowns":     {},
            "open_times":    {},
        }
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)
        log.info("New day! Balance: $" + str(round(current_balance, 2)))

    start_balance = today.get("start_balance", current_balance)

    # ── PNL ───────────────────────────────────────────────────────────
    realized_pnl = round(current_balance - start_balance, 2)
    open_pnl     = 0.0
    try:
        for name in ASSETS:
            pos = trader.get_position(name)
            if pos: open_pnl += trader.check_pnl(pos)
        open_pnl = round(open_pnl, 2)
    except Exception as e:
        log.warning("PnL error: " + str(e))

    total_pnl  = round(realized_pnl + open_pnl, 2)
    pnl_emoji  = "✅" if realized_pnl >= 0 else "🔴"
    pl_sgd     = round(realized_pnl * 1.35, 2)

    # ── DETECT SL/TP HITS ────────────────────────────────────────────
    detect_sl_tp_hits(today, trader, trade_log, alert)

    # ── EOD HARD CLOSE 10:55pm SGT ────────────────────────────────────
    if hour == 22 and now.minute >= 55:
        closed = []
        for name in ASSETS:
            pos = trader.get_position(name)
            if pos:
                trader.close_position(name)
                closed.append(name)
        if closed:
            alert.send(
                "🔔 DEMO 2 EOD Close\n"
                "Closed: " + ", ".join(closed) + "\n"
                "Realized: $" + str(realized_pnl) + " | " + pnl_emoji
            )
        return

    # ── 1HR MAX DURATION CHECK ────────────────────────────────────────
    for name in ASSETS:
        pos = trader.get_position(name)
        if not pos: continue
        try:
            trade_id   = pos.get("id") or pos.get("tradeID")
            t_url      = trader.base_url + "/v3/accounts/" + trader.account_id + "/trades/" + str(trade_id)
            t_resp     = requests.get(t_url, headers=trader.headers, timeout=10)
            open_str   = t_resp.json()["trade"]["openTime"]
            open_utc   = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            now_utc    = datetime.now(pytz.utc)
            hours_open = (now_utc - open_utc).total_seconds() / 3600
            if hours_open >= 1.0:
                pnl   = trader.check_pnl(pos)
                emoji = "✅" if pnl >= 0 else "🔴"
                trader.close_position(name)
                if name in today.get("open_times", {}):
                    del today["open_times"][name]
                    with open(trade_log, "w") as f:
                        json.dump(today, f, indent=2)
                alert.send(
                    "⏰ DEMO 2 1HR LIMIT\n"
                    + ASSETS[name]["emoji"] + " " + name + "\n"
                    "Closed after " + str(round(hours_open, 1)) + "h\n"
                    "PnL: $" + str(round(pnl, 2)) + " " + emoji
                )
        except Exception as e:
            log.warning("Duration check error " + name + ": " + str(e))

    # ── SCAN PAIRS ────────────────────────────────────────────────────
    scan_results = []
    threshold    = settings.get("signal_threshold", 4)

    for name, config in ASSETS.items():

        # Skip if outside session
        if not is_in_session(hour, config):
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: off-session"
            )
            continue

        # Already in trade
        pos = trader.get_position(name)
        if pos:
            pnl       = trader.check_pnl(pos)
            direction = "BUY" if int(float(pos.get("long", {}).get("units", 0))) > 0 else "SELL"
            emoji     = "📈" if pnl >= 0 else "📉"
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: " +
                direction + " open " + emoji + " $" + str(round(pnl, 2))
            )
            continue

        # Cooldown check
        if in_cooldown(today, name):
            cd   = today.get("cooldowns", {}).get(name, "")
            try:
                cd_time = datetime.fromisoformat(cd).replace(tzinfo=sg_tz)
                elapsed = (datetime.now(sg_tz) - cd_time).total_seconds() / 60
                remaining = int(30 - elapsed)
            except:
                remaining = "?"
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: ⏳ cooldown " + str(remaining) + "min"
            )
            continue

        # Spread check
        price, bid, ask = trader.get_price(name)
        if price is None:
            scan_results.append(config["emoji"] + " " + name + ": price error")
            continue
        spread_val = (ask - bid) / config["pip"]
        if spread_val > settings.get("max_spread_pips", 2):
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: spread " + str(round(spread_val, 1)) + " pips - skip"
            )
            continue

        # News check
        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: ⚠️ NEWS - " + news_reason
            )
            continue

        # ── GET SIGNAL ───────────────────────────────────────────────
        score, direction, details = signals.analyze(asset=config["asset"])
        max_score = config["max_score"]

        log.info(name + " [" + config["strategy_label"] + "]: " +
                 str(score) + "/" + str(max_score) + " " + direction)

        if score < threshold or direction == "NONE":
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: " +
                str(score) + "/" + str(max_score) + " no setup"
            )
            continue

        # ── PLACE TRADE ──────────────────────────────────────────────
        stop_pips  = config["stop_pips"]
        tp_pips    = config["tp_pips"]
        size       = 10000   # Fixed 0.10 lots
        max_loss   = round(size * stop_pips * config["pip"], 2)
        max_profit = round(size * tp_pips   * config["pip"], 2)

        result = trader.place_order(
            instrument     = name,
            direction      = direction,
            size           = size,
            stop_distance  = stop_pips,
            limit_distance = tp_pips
        )

        if result["success"]:
            today["trades"] = today.get("trades", 0) + 1
            if "open_times" not in today: today["open_times"] = {}
            today["open_times"][name] = now.isoformat()
            with open(trade_log, "w") as f:
                json.dump(today, f, indent=2)

            price, _, _ = trader.get_price(name)

            # Strategy name for Telegram
            strat_names = {
                "scalp_m15": "M15 Scalp",
            }
            strat_name = strat_names.get(config["strategy"], config["strategy"])

            alert.send(
                "🔄 DEMO 2 NEW TRADE!\n"
                + config["emoji"] + " " + name + "\n"
                "Strategy:  " + strat_name + "\n"
                "Direction: " + direction + "\n"
                "Score:     " + str(score) + "/" + str(max_score) + " ✅\n"
                "Size:      0.10 lots\n"
                "Entry:     " + str(round(price, config["precision"])) + "\n"
                "Stop Loss: " + str(stop_pips) + " pips = $" + str(max_loss) + "\n"
                "Take Prof: " + str(tp_pips) + " pips = $" + str(max_profit) + "\n"
                "Spread:    " + str(round(spread_val, 1)) + " pips\n"
                "Signals:   " + details
            )
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: " +
                direction + " " + str(score) + "/" + str(max_score) + " ✅ PLACED!"
            )
        else:
            set_cooldown(today, name)
            with open(trade_log, "w") as f:
                json.dump(today, f, indent=2)
            scan_results.append(
                config["emoji"] + " " + name +
                " [" + config["strategy_label"] + "]: order failed"
            )

    # ── SCAN SUMMARY ─────────────────────────────────────────────────
    if realized_pnl >= 15:
        target_msg = "🎯 TARGET HIT! $" + str(round(pl_sgd, 0)) + " SGD"
    elif realized_pnl > 0:
        target_msg = "Profit $" + str(round(pl_sgd, 2)) + " SGD"
    elif realized_pnl < 0:
        target_msg = "Loss $" + str(abs(round(pl_sgd, 2))) + " SGD"
    else:
        target_msg = "Waiting for closed trades..."

    if 6 <= hour < 11:   session = "Asian 🇯🇵"
    elif 14 <= hour < 18: session = "London 🇬🇧"
    elif 18 <= hour < 23: session = "NY 🇺🇸"
    else:                 session = "Off-hours"

    wins   = today.get("wins", 0)
    losses = today.get("losses", 0)
    consec = today.get("consec_losses", 0)

    alert.send(
        "🔄 DEMO 2 Scan | DEMO2\n"
        "Time:     " + now.strftime("%H:%M SGT") + " | " + session + "\n"
        "Balance:  $" + str(round(current_balance, 2)) + "\n"
        "Realized: $" + str(round(realized_pnl, 2)) + " " + pnl_emoji + "\n"
        "= $" + str(round(pl_sgd, 2)) + " SGD\n"
        "Open PnL: $" + str(round(open_pnl, 2)) + "\n"
        + target_msg + "\n"
        "Trades: " + str(today.get("trades", 0)) + "\n"
        "W/L: " + str(wins) + "/" + str(losses) + " | Streak: " + str(consec) + "\n"
        "─────────────────────────\n"
        + "\n".join(scan_results)
    )

# ── RAILWAY MAIN LOOP ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 DEMO 2 Scalp Bot starting...")
    log.info("AUD/USD [SCALP] 6am-11am | EUR/GBP [SCALP] 2pm-7pm | EUR/USD [SCALP] 2pm-6pm")
    while True:
        try:
            run_bot()
        except Exception as e:
            log.error("Bot error: " + str(e))
        log.info("Sleeping 5 mins...")
        time.sleep(300)
