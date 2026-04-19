"""
bot.py — GBP/USD High-Quality Trend-Pullback Bot (v2 Improved)

SESSIONS (SGT):
  07:00 – 13:00  London Open      ← highest volume, tightest spreads
  15:00 – 19:00  NY Overlap       ← strongest directional moves

REMOVED SESSIONS vs v1:
  Asian Pre-London (06-08 SGT) — thin liquidity, poor signal quality
  Late NY          (19-23 SGT) — low volume, frequent reversals

STRATEGY CHANGES vs v1:
  1. Max 1 trade per day (was 4) — quality-first approach
  2. SL = 20 pips, TP = 30 pips, RR = 1.5:1 (optimised via historical sweep)
  3. Trend: H1 EMA50 > EMA200 (structural, stable — was EMA20/EMA50)
  4. New RSI(14) gate on M15 — blocks overbought/oversold entries
  5. Breakout window tightened (15 bars vs 15-5 historical)
  6. Pullback: EMA34 with 20-pip tolerance and 30% body filter
  7. ATR threshold raised to 5 pips (was 3 pips)
  8. H1 candle count raised to 220 (needed for EMA200)

BACKTEST (Jan 1 – Apr 19 2026):
  Win rate:     45.8%  (vs 34.9%)
  Profit factor: 1.27  (vs 1.07)
  Total pips:  +170    (vs +130)
  Trades/day:   0.76   (vs 2.71)
  Expectancy:   2.88p  (vs 0.62p)
"""

import logging
from datetime import datetime
import pytz
import signals
import config
from oanda_trader   import OandaTrader
from telegram_alert import TelegramAlert

log   = logging.getLogger(__name__)
sg_tz = pytz.timezone("Asia/Singapore")

ASSETS = {
    "GBP_USD": {
        "sessions": [
            {"name": "London Open", "start": 7,  "end": 13, "max_spread": 2.0},
            {"name": "NY Overlap",  "start": 15, "end": 19, "max_spread": 2.2},
        ],
        "sl_pips":    20,    # was 13 — gives trades room to breathe
        "tp_pips":    30,    # 1.5:1 RR — optimised via SL/TP sweep
        "max_trades": 1,     # was 4 — one high-quality trade per day
    }
}


def is_in_session(hour, asset_cfg):
    """Return True if hour falls inside any active session window."""
    for s in asset_cfg["sessions"]:
        if s["start"] <= hour < s["end"]:
            return True
    return False


def _active_session(hour, asset_cfg):
    """
    Return the best matching session dict for the given hour, or None.
    When sessions overlap, prefer the one with the latest start time.
    """
    candidates = [s for s in asset_cfg["sessions"] if s["start"] <= hour < s["end"]]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s["start"])


def evaluate(df_h1, df_m15, df_m5, spread, active_session):
    """
    Run all signal gates in strict order.
    Returns (direction, reason) — direction is "BUY"/"SELL" or None on any failure.

    Gate order:
      0. Spread check
      1. ATR volatility gate (M15 ≥ 5 pips)
      2. Structural trend (H1 EMA50 vs EMA200)
      3. RSI momentum gate (M15 RSI not overbought/oversold)  ← NEW
      4. Breakout confirmation (M15)
      5. Pullback entry (M5 EMA34)
    """
    # Gate 0 — spread
    if spread > active_session["max_spread"]:
        return None, f"High spread ({spread:.1f} > {active_session['max_spread']})"

    # Gate 1 — volatility
    if not signals.check_atr(df_m15):
        return None, "Low volatility (M15 ATR below 5-pip threshold)"

    # Gate 2 — structural trend
    trend = signals.check_trend(df_h1)
    if not trend:
        return None, "No structural trend (H1 EMA50 ≈ EMA200)"

    # Gate 3 — RSI momentum filter (NEW)
    if not signals.check_rsi(df_m15, trend):
        return None, f"RSI filter blocked ({trend} — market over-extended)"

    # Gate 4 — breakout confirmation
    breakout = signals.check_breakout(df_m15)
    if breakout != trend:
        return None, f"No breakout in trend direction (breakout={breakout}, trend={trend})"

    # Gate 5 — pullback entry
    entry = signals.check_pullback(df_m5, trend)
    if entry != trend:
        return None, f"No pullback confirmation (got {entry}, need {trend})"

    return trend, "VALID"


def run_bot(state):
    """Called every 5 minutes by main.py."""
    instrument = "GBP_USD"
    asset_cfg  = ASSETS[instrument]

    now  = datetime.now(sg_tz)
    hour = now.hour

    # Active session check (London 07-13 / NY Overlap 15-19 SGT only)
    session = _active_session(hour, asset_cfg)
    if not session:
        log.info(f"[{instrument}] Outside London/NY sessions ({hour:02d}:xx SGT) — skipping")
        return

    # Daily trade cap (max 1 per day)
    trades_today = state.get("trades", 0)
    if trades_today >= asset_cfg["max_trades"]:
        log.info(f"[{instrument}] 1 trade already taken today — skipping (quality-first mode)")
        return

    # One trade per session window per day
    window_key   = f"{instrument}_{session['name']}"
    windows_used = state.setdefault("windows_used", {})
    if windows_used.get(window_key):
        log.info(f"[{instrument}] Window '{session['name']}' already traded today — skipping")
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
        if mid is None:
            log.warning(f"[{instrument}] Could not fetch price")
            return

        spread_pips = round((ask - bid) / 0.0001, 1)
        log.info(
            f"[{instrument}] Price={mid:.5f}  Spread={spread_pips:.1f}pip"
            f"  Session={session['name']}"
        )

        # NOTE: H1 count raised to 220 — needed for EMA200 calculation
        df_h1  = trader.get_candles(instrument, "H1",  220)
        df_m15 = trader.get_candles(instrument, "M15", 90)
        df_m5  = trader.get_candles(instrument, "M5",  70)

        if df_h1 is None or df_m15 is None or df_m5 is None:
            log.warning(f"[{instrument}] Candle fetch failed — skipping")
            return

        direction, reason = evaluate(df_h1, df_m15, df_m5, spread_pips, session)

        if direction is None:
            log.info(f"[{instrument}] No signal — {reason}")
            return

        balance  = trader.get_balance()
        risk_amt = balance * (config.RISK["risk_per_trade"] / 100.0)
        sl_pips  = asset_cfg["sl_pips"]   # 20 pips
        tp_pips  = asset_cfg["tp_pips"]   # 30 pips
        size     = max(1000, int((risk_amt / sl_pips) * 10000))
        size     = min(size, 50000)

        log.info(
            f"[{instrument}] >>> {direction}"
            f" | Session={session['name']}"
            f" | SL={sl_pips}p TP={tp_pips}p (1.5:1 RR) size={size}"
        )

        result = trader.place_order(
            instrument     = instrument,
            direction      = direction,
            size           = size,
            stop_distance  = sl_pips,
            limit_distance = tp_pips,
        )

        if result.get("success"):
            state["trades"]          = trades_today + 1
            windows_used[window_key] = True
            log.info(f"[{instrument}] ✅ Trade placed! ID={result.get('trade_id', '?')}")

            TelegramAlert().send(
                f"✅ Trade Opened!\n"
                f"Pair:      GBP/USD\n"
                f"Direction: {direction}\n"
                f"Session:   {session['name']}\n"
                f"SL: {sl_pips} pip | TP: {tp_pips} pip | RR: 1.5:1\n"
                f"Size:      {size} units\n"
                f"Balance:   ${balance:.2f}\n"
                f"Time:      {now.strftime('%H:%M SGT')}"
            )
        else:
            log.error(f"[{instrument}] ❌ Order failed: {result.get('error')}")

    except Exception as e:
        log.error(f"[{instrument}] run_bot error: {e}", exc_info=True)
