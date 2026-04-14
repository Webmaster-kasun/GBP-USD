from datetime import datetime
import signals
import config

state = {
    "trades_today": 0
}


def in_session():
    now = datetime.now().hour
    for s in config.SESSIONS:
        if s["start"] <= now < s["end"]:
            return s
    return None


def evaluate(df_h1, df_m15, df_m5, spread):
    session = in_session()
    if not session:
        return None, "Outside session"

    if spread > session["max_spread"]:
        return None, "High spread"

    if state["trades_today"] >= config.RISK["max_trades_per_day"]:
        return None, "Max trades reached"

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


def on_trade():
    state["trades_today"] += 1
