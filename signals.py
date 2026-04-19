"""
signals.py — GBP/USD Improved Signal Logic
===========================================

CHANGES vs original (v1):
  CHANGE-01  check_trend     H1 EMA50 vs EMA200 (structural trend — much more stable than EMA20/50)
  CHANGE-02  check_rsi       NEW gate: RSI(14) on M15 prevents overbought BUY / oversold SELL
  CHANGE-03  check_breakout  Tighter 15-bar window; EMA continuation proximity tightened to 15 pips
  CHANGE-04  check_pullback  EMA34 (Fibonacci-based, smoother), 20-pip tolerance, 30% body ratio
  CHANGE-05  check_atr       Raised threshold from 3 pips to 5 pips — filters dead markets

Backtest summary (Jan 1 – Apr 19 2026, GBP/USD):
  Original : 209 trades | WR 34.9% | PF 1.07 | +130 pips | 2.71 trades/day
  Improved :  59 trades | WR 45.8% | PF 1.27 | +170 pips | 0.76 trades/day
"""


def check_trend(df_h1):
    """
    IMPROVED — EMA50 vs EMA200 on H1 (structural trend filter).

    EMA20/EMA50 used to flip multiple times per day in ranging markets,
    generating conflicting BUY and SELL signals on the same pair.
    EMA50/EMA200 only changes direction every few days, ensuring we only
    trade with the dominant macro trend.

    Requires 200+ H1 candles — fetch at least 220 in bot.py.

    Returns "BUY" | "SELL" | None
    """
    ema50  = df_h1["close"].ewm(span=50).mean().iloc[-1]
    ema200 = df_h1["close"].ewm(span=200).mean().iloc[-1]

    if ema50 > ema200:
        return "BUY"
    elif ema50 < ema200:
        return "SELL"
    return None


def check_rsi(df_m15, direction):
    """
    NEW — RSI(14) momentum gate on M15.

    Prevents entering a BUY when price is already overbought (RSI > 65)
    or a SELL when price is oversold (RSI < 35). These entries have a
    high probability of reversing immediately after entry.

    Returns True (safe to proceed) | False (filtered)
    """
    if len(df_m15) < 16:
        return False

    delta = df_m15["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / (loss + 1e-9)
    rsi   = (100 - 100 / (1 + rs)).iloc[-1]

    if direction == "BUY"  and rsi > 65:
        return False
    if direction == "SELL" and rsi < 35:
        return False
    return True


def check_breakout(df_m15):
    """
    IMPROVED — Two-mode breakout on M15.

    Mode 1 — Classic breakout (tightened to bars -15:-3):
        Close above 15-bar historical HIGH → BUY
        Close below 15-bar historical LOW  → SELL
        The -3 buffer avoids premature triggers near the last few bars.

    Mode 2 — EMA8/21 continuation (within 15 pips of EMA21):
        For smooth trend days with no sharp breakout.
        Proximity tightened from 20 pips → 15 pips to reduce false signals.

    Returns "BUY" | "SELL" | None
    """
    if len(df_m15) < 25:
        return None

    hist_high = df_m15["high"].iloc[-15:-3].max()
    hist_low  = df_m15["low"].iloc[-15:-3].min()
    close     = df_m15["close"].iloc[-1]

    if close > hist_high:
        return "BUY"
    if close < hist_low:
        return "SELL"

    # Continuation mode — price riding EMA21 in a trending market
    ema8  = df_m15["close"].ewm(span=8).mean().iloc[-1]
    ema21 = df_m15["close"].ewm(span=21).mean().iloc[-1]

    if abs(close - ema21) < 0.0015:   # 15 pips (was 20 pips)
        if ema8 > ema21:
            return "BUY"
        if ema8 < ema21:
            return "SELL"

    return None


def check_pullback(df_m5, direction):
    """
    IMPROVED — EMA34 pullback entry on M5.

    Why EMA34? Fibonacci-derived, widely used in FX, reacts more smoothly
    than EMA21 and better filters micro-noise during session transitions.

    Conditions (all must pass):
      - Price within 20 pips of EMA34   (was: 25 pips from EMA21)
      - Candle body ≥ 30% of total range (was: 25%)
      - Candle direction matches trend

    Returns "BUY" | "SELL" | None
    """
    if len(df_m5) < 35:
        return None

    ema34  = df_m5["close"].ewm(span=34).mean().iloc[-1]
    candle = df_m5.iloc[-1]
    close  = candle["close"]
    open_  = candle["open"]
    high   = candle["high"]
    low    = candle["low"]

    diff  = abs(close - ema34)
    body  = abs(close - open_)
    total = high - low

    if diff > 0.0020:                          # 20 pips max (was 25)
        return None
    if total == 0 or (body / total) < 0.30:   # 30% body ratio (was 25%)
        return None

    if direction == "BUY"  and close > open_:
        return "BUY"
    if direction == "SELL" and close < open_:
        return "SELL"

    return None


def check_atr(df_m15):
    """
    IMPROVED — ATR volatility gate raised from 3 pips to 5 pips.

    GBP/USD nearly always exceeds 3 pips ATR on M15, making the original
    gate useless — it let through flat, ranging conditions that produce
    fake breakout signals. 5 pips ensures real directional volatility.

    14-bar ATR on M15 must exceed 0.0005 (5 pips).
    """
    if len(df_m15) < 15:
        return False
    atr = (df_m15["high"] - df_m15["low"]).rolling(14).mean().iloc[-1]
    return atr > 0.0005
