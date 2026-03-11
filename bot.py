"""
OANDA Trading Bot - Demo Account 2
====================================
Strategy: Mean Reversion
- Bollinger Bands (20, 2) entry signals
- RSI extremes confirmation (< 35 BUY / > 65 SELL)
- ATR ranging filter (skip if trending)
- Dynamic TP = Middle Bollinger Band
- Same risk controls as Demo 1
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
import pytz

from oanda_trader import OandaTrader
from signals import SignalEngine
from telegram_alert import TelegramAlert
from calendar_filter import EconomicCalendar

# Safe logging - never expose API keys
class SafeFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        key = os.environ.get("OANDA_API_KEY", "")
        if key and key in msg:
            msg = msg.replace(key, "***")
        return msg

handler      = logging.StreamHandler()
handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(message)s"))
file_handler = logging.FileHandler("performance_log.txt")
file_handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[handler, file_handler])
log = logging.getLogger(__name__)

# ── ASSET CONFIGURATION ──────────────────────────────────────────────────────
# MEAN REVERSION BEST PAIRS (5-year analysis):
# AUD/USD  → ranges for weeks, China-sensitive, slow grinding = perfect MR
# EUR/GBP  → extremely tight range 0.84-0.92, always reverts = perfect MR
# USD/CHF  → SNB keeps it rangy, mirrors EUR/USD inversely = perfect MR
# XAG/USD  → follows gold but spikier, reversals common = perfect MR
ASSETS = {
    "AUD_USD": {
        "instrument": "AUD_USD",
        "asset":      "AUDUSD",
        "emoji":      "🦘",
        "setting":    "trade_audusd",
        "stop_pips":  25,
        "tp_pips":    20,
        "pip":        0.0001,
        "precision":  5,
        "min_atr":    0.0004,
        "session_hours": [(6, 11), (14, 17)],   # Tokyo + London open
    },
    "EUR_GBP": {
        "instrument": "EUR_GBP",
        "asset":      "EURGBP",
        "emoji":      "🇪🇺",
        "setting":    "trade_eurgbp",
        "stop_pips":  20,
        "tp_pips":    15,
        "pip":        0.0001,
        "precision":  5,
        "min_atr":    0.0003,
        "session_hours": [(14, 19)],             # London only
    },
    "USD_CHF": {
        "instrument": "USD_CHF",
        "asset":      "USDCHF",
        "emoji":      "🇨🇭",
        "setting":    "trade_usdchf",
        "stop_pips":  25,
        "tp_pips":    20,
        "pip":        0.0001,
        "precision":  5,
        "min_atr":    0.0004,
        "session_hours": [(14, 23), (0, 1)],     # London + NY
    },
    "XAG_USD": {
        "instrument": "XAG_USD",
        "asset":      "XAGUSD",
        "emoji":      "🥈",
        "setting":    "trade_silver",
        "stop_pips":  50,
        "tp_pips":    60,
        "pip":        0.0001,
        "precision":  4,
        "min_atr":    0.05,
        "session_hours": [(14, 23), (0, 1)],     # London + NY
        "precision":  2,
        "min_atr":    3.0,
    },
}

def load_settings():
    default = {
        "max_trades_day":    6,       # More trades allowed (higher win rate strategy)
        "max_daily_loss":    40.0,
        "signal_threshold":  4,
        "demo_mode":         True,
        "trade_audusd":      True,   # Best MR pair - ranges for weeks
        "trade_eurgbp":      True,   # Best MR pair - tight 0.84-0.92 range
        "trade_usdchf":      True,   # Best MR pair - SNB keeps it rangy
        "trade_silver":      True,   # Best MR pair - spiky reversals
        "fixed_lot_size":     0.10,    # Fixed 0.10 lots per trade
        "max_consec_losses": 2,
        "max_spread_pips":   2,
        "strategy":          "mean_reversion",
    }
    try:
        with open("settings.json") as f:
            saved = json.load(f)
            default.update(saved)
    except FileNotFoundError:
        with open("settings.json", "w") as f:
            json.dump(default, f, indent=2)
    return default

def calc_position_size(pip_value):
    """
    Fixed lot size: 0.10 lots
    Forex = 10,000 units
    Gold  = 1 oz (minimum)
    """
    if pip_value <= 0.0001:
        return 10000   # 0.10 lots for EUR/USD, GBP/USD
    else:
        return 1       # 1 oz for Gold (minimum)

def get_bb_tp_pips(trader, instrument, direction, pip, precision):
    """
    Dynamic TP = Middle Bollinger Band (20 EMA on H1)
    This is the CORE of mean reversion - we target the mean!
    """
    try:
        import math
        url    = trader.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": "50", "granularity": "H1", "price": "M"}
        r      = requests.get(url, headers=trader.headers, params=params, timeout=10)
        if r.status_code != 200:
            return None

        candles = r.json()["candles"]
        closes  = [float(c["mid"]["c"]) for c in candles if c["complete"]]
        if len(closes) < 20:
            return None

        # Calculate BB middle (20 SMA)
        recent = closes[-20:]
        middle = sum(recent) / 20

        # Current price
        price, _, _ = trader.get_price(instrument)
        if not price:
            return None

        # Distance from current price to middle band in pips
        distance_pips = abs(price - middle) / pip
        log.info(instrument + " BB middle=" + str(round(middle, precision)) +
                 " price=" + str(round(price, precision)) +
                 " TP_pips=" + str(round(distance_pips, 1)))

        # Minimum TP = 10 pips (not worth trading if too close to mean)
        if distance_pips < 10:
            return None

        return round(distance_pips)

    except Exception as e:
        log.warning("BB TP calc error: " + str(e))
        return None

def check_spread(trader, instrument, max_spread_pips, pip):
    """Skip trade if spread too wide"""
    try:
        bid, ask, _ = trader.get_price(instrument)
        if bid is None:
            return True, 0
        bid_val, ask_val = ask, bid  # get_price returns mid, bid, ask
        # Re-fetch properly
        mid, bid_val, ask_val = trader.get_price(instrument)
        spread_pips = (ask_val - bid_val) / pip
        log.info(instrument + " spread=" + str(round(spread_pips, 1)) + " pips")
        if spread_pips > max_spread_pips:
            log.warning(instrument + " spread too wide: " + str(round(spread_pips, 1)))
            return False, spread_pips
        return True, spread_pips
    except Exception as e:
        log.warning("Spread check error: " + str(e))
        return True, 0

def is_in_cooldown(today, instrument):
    cooldowns = today.get("cooldowns", {})
    if instrument not in cooldowns:
        return False
    last_loss  = datetime.fromisoformat(cooldowns[instrument])
    wait_until = last_loss + timedelta(minutes=30)
    now_utc    = datetime.utcnow()
    if now_utc < wait_until:
        mins = int((wait_until - now_utc).seconds / 60)
        log.info(instrument + " cooldown " + str(mins) + " mins left")
        return True
    return False

def set_cooldown(today, instrument):
    if "cooldowns" not in today:
        today["cooldowns"] = {}
    today["cooldowns"][instrument] = datetime.utcnow().isoformat()

def run_bot():
    log.info("OANDA Bot Demo 2 - Mean Reversion starting!")
    settings = load_settings()
    sg_tz    = pytz.timezone("Asia/Singapore")
    now      = datetime.now(sg_tz)
    alert    = TelegramAlert()
    hour     = now.hour

    # Session detection - expanded to cover ALL pair sessions
    # Tokyo: 6am-12pm SGT (AUD/USD, USD/JPY best)
    # London: 2pm-8pm SGT (EUR, GBP pairs best)
    # NY: 8pm-11pm SGT (USD pairs best)
    tokyo_session  = (6 <= hour <= 11)
    london_session = (14 <= hour <= 19)
    ny_session     = (20 <= hour <= 23)
    late_ny        = (0 <= hour <= 1)
    good_session   = tokyo_session or london_session or ny_session or late_ny

    if 14 <= hour <= 17:
        session = "London Open 🇬🇧"
    elif 20 <= hour <= 23:
        session = "London+NY Overlap 🔥"
    elif 18 <= hour <= 19:
        session = "London Session 🇬🇧"
    elif 0 <= hour <= 1:
        session = "NY Late Session 🇺🇸"
    elif 6 <= hour <= 11:
        session = "Tokyo Session 🇯🇵"
    else:
        session = "Off-hours (SKIP)"

    # Weekend check
    if now.weekday() == 5:
        alert.send("Saturday - markets closed! Bot 2 resumes Monday 5am SGT")
        return
    if now.weekday() == 6 and hour < 5:
        alert.send("Sunday early - markets open at 5am SGT")
        return

    # Login
    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login():
        alert.send("DEMO 2 Login FAILED! Check OANDA_API_KEY and OANDA_ACCOUNT_ID secrets")
        return

    current_balance = trader.get_balance()
    mode            = "DEMO2" if settings["demo_mode"] else "LIVE"

    # Load today log
    trade_log = "trades_" + now.strftime("%Y%m%d") + ".json"
    try:
        with open(trade_log) as f:
            today = json.load(f)
    except FileNotFoundError:
        today = {
            "trades":        0,
            "start_balance": current_balance,
            "daily_pnl":     0.0,
            "stopped":       False,
            "wins":          0,
            "losses":        0,
            "consec_losses": 0,
            "cooldowns":     {}
        }
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)
        log.info("New day! Start balance: $" + str(round(current_balance, 2)))

    # PnL tracking
    start_balance = today.get("start_balance", current_balance)
    open_pnl      = sum(
        trader.check_pnl(trader.get_position(n))
        for n in ASSETS if trader.get_position(n)
    )
    realized_pnl = current_balance - start_balance
    total_pnl    = realized_pnl + open_pnl
    pl_sgd       = realized_pnl * 1.35
    pnl_emoji    = "✅" if realized_pnl >= 0 else "❌"

    today["daily_pnl"] = realized_pnl
    with open(trade_log, "w") as f:
        json.dump(today, f, indent=2)

    # Daily loss protection
    if today.get("stopped"):
        alert.send(
            "🔄 DEMO 2 Bot stopped for today\n"
            "Daily limit hit!\n"
            "Realized: $" + str(round(realized_pnl, 2)) + " USD\n"
            "Resumes tomorrow!"
        )
        return

    if realized_pnl <= -settings["max_daily_loss"]:
        today["stopped"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)
        alert.send(
            "🔴 DEMO 2 DAILY LOSS LIMIT HIT!\n"
            "Loss: $" + str(abs(round(realized_pnl, 2))) + " USD\n"
            "Limit: $" + str(settings["max_daily_loss"]) + " USD\n"
            "Bot stopped! Resumes tomorrow."
        )
        return

    # Consecutive loss protection
    consec = today.get("consec_losses", 0)
    if consec >= settings.get("max_consec_losses", 2):
        today["stopped"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)
        alert.send(
            "⛔ DEMO 2: 2 CONSECUTIVE LOSSES!\n"
            "Protecting capital!\n"
            "Stopped for today.\n"
            "Realized: $" + str(round(realized_pnl, 2)) + " USD\n"
            "Resumes tomorrow!"
        )
        return

    # Max trades
    if today["trades"] >= settings["max_trades_day"]:
        alert.send(
            "✅ DEMO 2 Max trades reached!\n"
            "Trades: " + str(today["trades"]) + "/" + str(settings["max_trades_day"]) + "\n"
            "Realized: $" + str(round(realized_pnl, 2)) + " USD " + pnl_emoji + "\n"
            "= $" + str(round(pl_sgd, 2)) + " SGD\n"
            "New trades resume tomorrow!"
        )
        return

    # Off hours - just monitor
    if not good_session:
        open_positions = []
        for name, config in ASSETS.items():
            pos = trader.get_position(name)
            if pos:
                pnl       = trader.check_pnl(pos)
                direction = "BUY" if int(float(pos["long"]["units"])) > 0 else "SELL"
                open_positions.append(config["emoji"] + " " + name + ": " + direction + " $" + str(round(pnl, 2)))

        positions_str = "\n".join(open_positions) if open_positions else "No open trades"
        alert.send(
            "📊 DEMO 2 Off-hours\n"
            "Strategy: Mean Reversion\n"
            "Time: " + now.strftime("%H:%M SGT") + "\n"
            "Balance: $" + str(round(current_balance, 2)) + "\n"
            "Realized: $" + str(round(realized_pnl, 2)) + " USD " + pnl_emoji + "\n"
            "Trading starts: 2pm SGT\n"
            "---\n" + positions_str
        )
        return

    # Active session - scan for mean reversion setups!
    signals      = SignalEngine()
    calendar     = EconomicCalendar()
    scan_results = []

    # News warning
    news_summary = calendar.get_today_summary()
    if "No high" not in news_summary:
        alert.send("⚠️ DEMO 2 NEWS ALERT!\n" + news_summary)

    for name, config in ASSETS.items():
        if not settings.get(config["setting"], True):
            continue
        if today["trades"] >= settings["max_trades_day"]:
            break

        # Check open position
        position = trader.get_position(name)
        if position:
            pnl       = trader.check_pnl(position)
            direction = "BUY" if int(float(position["long"]["units"])) > 0 else "SELL"
            emoji     = "📈" if pnl > 0 else "📉"
            scan_results.append(config["emoji"] + " " + name + ": " + direction + " open " + emoji + " $" + str(round(pnl, 2)))
            continue

        # Per-pair session filter - uses session_hours from ASSETS config
        session_hours = config.get("session_hours", [(14, 23)])
        pair_ok = any(start <= hour <= end for (start, end) in session_hours)
        if not pair_ok:
            hours_str = " & ".join(str(s) + "am-" + str(e) + "pm SGT" for (s, e) in session_hours)
            scan_results.append(config["emoji"] + " " + name + ": off-session (best " + hours_str + ")")
            continue

        # Cooldown check
        if is_in_cooldown(today, name):
            scan_results.append(config["emoji"] + " " + name + ": cooldown (30min after SL)")
            continue

        # Spread check
        spread_ok, spread_val = check_spread(trader, name, settings.get("max_spread_pips", 2), config["pip"])
        if not spread_ok:
            scan_results.append(config["emoji"] + " " + name + ": spread too wide - skip")
            continue

        # News blackout
        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            scan_results.append(config["emoji"] + " " + name + ": PAUSED - " + news_reason)
            continue

        # Mean reversion signal
        score, direction, details = signals.analyze(asset=config["asset"])
        log.info(name + ": score=" + str(score) + " dir=" + direction + " | " + details)

        if score < settings["signal_threshold"] or direction == "NONE":
            scan_results.append(config["emoji"] + " " + name + ": " + str(score) + "/5 no mean reversion setup")
            continue

        # Dynamic TP = Middle Bollinger Band
        dynamic_tp = get_bb_tp_pips(trader, name, direction, config["pip"], config["precision"])
        tp_pips    = dynamic_tp if dynamic_tp else config["tp_pips"]
        stop_pips  = config["stop_pips"]

        # Position sizing
        # Fixed 0.10 lots - no dynamic sizing
        size       = calc_position_size(config["pip"])
        max_loss   = round(size * stop_pips * config["pip"], 2)
        max_profit = round(size * tp_pips * config["pip"], 2)

        log.info(name + " size=" + str(size) + " stop=" + str(stop_pips) +
                 " tp=" + str(tp_pips) + " (dynamic=" + str(dynamic_tp is not None) + ")")

        # Place order
        result = trader.place_order(
            instrument     = name,
            direction      = direction,
            size           = size,
            stop_distance  = stop_pips,
            limit_distance = tp_pips
        )

        if result["success"]:
            today["trades"]       += 1
            today["consec_losses"] = 0
            with open(trade_log, "w") as f:
                json.dump(today, f, indent=2)

            price, _, _ = trader.get_price(name)
            tp_type     = "Dynamic BB" if dynamic_tp else "Fixed"
            alert.send(
                "🔄 DEMO 2 NEW TRADE! " + mode + "\n"
                + config["emoji"] + " " + name + "\n"
                "Strategy:  Mean Reversion\n"
                "Direction: " + direction + "\n"
                "Score:     " + str(score) + "/5\n"
                "Entry:     " + str(round(price, config["precision"])) + "\n"
                "Size:      " + str(size) + " units\n"
                "Stop Loss: " + str(stop_pips) + " pips = $" + str(max_loss) + "\n"
                "Take Prof: " + str(tp_pips) + " pips = $" + str(max_profit) + " (" + tp_type + ")\n"
                "Spread:    " + str(round(spread_val, 1)) + " pips\n"
                "Trade #" + str(today["trades"]) + "/" + str(settings["max_trades_day"]) + "\n"
                "Session: " + session + "\n"
                "Signals: " + details
            )
            scan_results.append(config["emoji"] + " " + name + ": " + direction + " PLACED! " + str(score) + "/5")
        else:
            set_cooldown(today, name)
            with open(trade_log, "w") as f:
                json.dump(today, f, indent=2)
            scan_results.append(config["emoji"] + " " + name + ": order failed")

    # Summary
    target_hit = realized_pnl >= 22
    if target_hit:
        target_msg = "🎯 TARGET HIT! $" + str(round(pl_sgd, 0)) + " SGD today!"
    elif realized_pnl > 0:
        target_msg = "Profit $" + str(round(pl_sgd, 0)) + " SGD (target $30 SGD)"
    elif realized_pnl < 0:
        target_msg = "Loss $" + str(abs(round(pl_sgd, 0))) + " SGD today"
    else:
        target_msg = "Waiting for closed trades..."

    summary = "\n".join(scan_results) if scan_results else "No setups this scan"
    wins    = today.get("wins", 0)
    losses  = today.get("losses", 0)
    consec  = today.get("consec_losses", 0)

    alert.send(
        "🔄 DEMO 2 Scan Complete! " + mode + "\n"
        "Strategy: Mean Reversion\n"
        "Time: " + now.strftime("%H:%M SGT") + "\n"
        "Session: " + session + "\n"
        "Balance: $" + str(round(current_balance, 2)) + "\n"
        "Start:   $" + str(round(start_balance, 2)) + "\n"
        "Realized: $" + str(round(realized_pnl, 2)) + " USD " + pnl_emoji + "\n"
        "= $" + str(round(pl_sgd, 2)) + " SGD\n"
        "Open PnL: $" + str(round(open_pnl, 2)) + " USD\n"
        "Total:    $" + str(round(total_pnl, 2)) + " USD\n"
        + target_msg + "\n"
        "Trades: " + str(today["trades"]) + "/" + str(settings["max_trades_day"]) + "\n"
        "W/L: " + str(wins) + "/" + str(losses) + " | Consec loss: " + str(consec) + "\n"
        "---\n"
        + summary
    )

if __name__ == "__main__":
    run_bot()
