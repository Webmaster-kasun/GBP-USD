"""
bot.py — GBP/USD Multi-Session Scalp Bot
SESSIONS:
  06:00–08:00 SGT — Asian Pre-London
  07:00–13:00 SGT — London Open
  15:00–19:00 SGT — NY Overlap
  19:00–23:30 SGT — Late NY
Max 4 trades/day, 1 per session window.
"""

import logging
from datetime import datetime
import pytz
import signals
import config
from oanda_trader import OandaTrader
from telegram_alert import TelegramAlert

log = logging.getLogger(__name__)

sg_tz = pytz.timezone("Asia/Singapore")

# Asset config — imported by main.py
ASSETS = {
    "GBP_USD": {
        "sessions": [
            {"name": "Asian Pre-London", "start": 6,  "end": 8,  "max_spread": 1.8},
            {"name": "London Open",      "start": 7,  "end": 13, "max_spread": 2.0},
            {"name": "NY Overlap",       "start": 15, "end": 19, "max_spread": 2.2},
            {"name": "Late NY",          "start": 19, "end": 23, "max_spread": 2.5},
        ],
        "sl_pips": 13,
        "tp_pips": 26,
        "max_trades": 4,
    }
}


def is_in_session(hour, asset_cfg):
    """Used by main.py."""
    for s in asset_cfg["sessions"]:
        if s["start"] <= hour < s["end"]:
            return True
    return False


def in_session():
    """Returns active session dict or None. Uses SGT timezone."""
    now  = datetime.now(sg_tz)
    hour = now.hour
    for s in config.SESSIONS:
        if s["start"] <= hour < s["end"]:
            return s
    return None


def evaluate(df_h1, df_m15, df_m5, spread):
    session = in_session()
    if not session:
        return None, "Outside session"

    if spread > session["max_spread"]:
        return None, "High spread"

    if not signals.check_atr(df_m15):
        return None, "Low volatility"

    trend = signals.check_trend(df_h1)
    if not trend:
        return None, "No trend"

    breakout = signals.check_breakout(df_m15)
    if breakout != trend:
        return None, "No breakout"

    entry = signals.check_pullback(df_m5, trend)
    if entry != trend:
        return None, "No pullback"

    return trend, "VALID"


def run_bot(state):
    """Called every 5 min by main.py."""
    instrument = "GBP_USD"
    asset_cfg  = ASSETS[instrument]

    now  = datetime.now(sg_tz)
    hour = now.hour

    # Find active session — note 07:00–08:00 overlaps Asian+London, London takes priority
    active_session = None
    for s in asset_cfg["sessions"]:
        if s["start"] <= hour < s["end"]:
            active_session = s
            break  # first match wins (ordered by priority above)

    if not active_session:
        log.info(f"[{instrument}] Outside all sessions ({hour:02d}:xx SGT) — skipping")
        return

    # Max trades guard
    trades_today = state.get("trades", 0)
    if trades_today >= asset_cfg["max_trades"]:
        log.info(f"[{instrument}] Max {asset_cfg['max_trades']} trades reached — skipping")
        return

    # One trade per session window
    window_key   = f"{instrument}_{active_session['name']}"
    windows_used = state.setdefault("windows_used", {})
    if windows_used.get(window_key):
        log.info(f"[{instrument}] Window '{active_session['name']}' already traded — skipping")
        return

    try:
        trader = OandaTrader(demo=True)
        if not trader.login():
            log.warning(f"[{instrument}] OANDA login failed")
            return

        if trader.get_position(instrument):
            log.info(f"[{instrument}] Position already open — skipping")
            return

        mid, bid, ask = trader.get_price(instrument)
        if not mid:
            log.warning(f"[{instrument}] Could not get price")
            return

        spread_pips = round((ask - bid) / 0.0001, 1)
        log.info(f"[{instrument}] Price={mid:.5f}  Spread={spread_pips:.1f}pip  Session={active_session['name']}")

        if spread_pips > active_session["max_spread"]:
            log.info(f"[{instrument}] Spread {spread_pips} > limit {active_session['max_spread']} — skipping")
            return

        df_h1  = trader.get_candles(instrument, "H1",  120)
        df_m15 = trader.get_candles(instrument, "M15", 80)
        df_m5  = trader.get_candles(instrument, "M5",  60)

        if df_h1 is None or df_m15 is None or df_m5 is None:
            log.warning(f"[{instrument}] Candle fetch failed")
            return

        direction, reason = evaluate(df_h1, df_m15, df_m5, spread_pips)

        if direction is None:
            log.info(f"[{instrument}] No signal — {reason}")
            return

        balance  = trader.get_balance()
        risk_amt = balance * (config.RISK["risk_per_trade"] / 100.0)
        sl_pips  = asset_cfg["sl_pips"]
        tp_pips  = asset_cfg["tp_pips"]
        size     = max(1000, int((risk_amt / sl_pips) * 10000))
        size     = min(size, 50000)

        log.info(f"[{instrument}] >>> {direction} | Session={active_session['name']} | SL={sl_pips}p TP={tp_pips}p size={size}")

        result = trader.place_order(
            instrument     = instrument,
            direction      = direction,
            size           = size,
            stop_distance  = sl_pips,
            limit_distance = tp_pips,
        )

        if result.get("success"):
            state["trades"] = trades_today + 1
            windows_used[window_key] = True
            log.info(f"[{instrument}] ✅ Trade placed! ID={result.get('trade_id','?')}")

            TelegramAlert().send(
                f"✅ Trade Opened!\n"
                f"Pair: GBP/USD\n"
                f"Direction: {direction}\n"
                f"Session: {active_session['name']}\n"
                f"SL: {sl_pips} pip | TP: {tp_pips} pip\n"
                f"Size: {size} units\n"
                f"Balance: ${balance:.2f}\n"
                f"Time: {now.strftime('%H:%M SGT')}"
            )
        else:
            log.error(f"[{instrument}] ❌ Order failed: {result.get('error')}")

    except Exception as e:
        log.error(f"[{instrument}] run_bot error: {e}", exc_info=True)
