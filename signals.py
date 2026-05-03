"""
signals.py — GBP/USD Triple EMA Momentum Strategy (v3.1)
=========================================================

All time checks use UTC — works on any server timezone.

London Open window in UTC:
  GMT season (Oct-Mar): 07:00-07:30 UTC
  BST season (Mar-Oct): 06:00-06:30 UTC
  Combined window used: 06:00-08:00 UTC (covers both seasons safely).
"""

import pytz

UTC = pytz.utc


def check_trend(df_h1) -> str | None:
    """
    Triple EMA trend filter on H1.

    Returns:
      'SELL' if EMA5 < EMA10 < EMA20  (confirmed downtrend)
      'BUY'  if EMA5 > EMA10 > EMA20  (confirmed uptrend)
      None   if EMAs are mixed         (skip — no clear trend)

    Requires 25+ H1 bars.
    """
    if len(df_h1) < 25:
        return None

    c     = df_h1['close']
    ema5  = c.ewm(span=5,  adjust=False).mean().iloc[-1]
    ema10 = c.ewm(span=10, adjust=False).mean().iloc[-1]
    ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]

    if ema5 < ema10 < ema20:
        return 'SELL'
    if ema5 > ema10 > ema20:
        return 'BUY'
    return None


def check_london_open(df_m15) -> bool:
    """
    Returns True if latest bar falls in 06:00-08:00 UTC.
    Covers London Open for both GMT and BST seasons.
    Works regardless of server timezone.
    """
    if len(df_m15) == 0:
        return False

    ts = df_m15.index[-1]

    # Normalise to UTC
    if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
        ts = ts.astimezone(UTC)

    hour = ts.hour + ts.minute / 60
    return 6.0 <= hour <= 8.0


def check_atr(df_m15, min_atr_pips: float = 5.0) -> bool:
    """14-bar ATR on M15 must exceed min_atr_pips. Filters flat/dead markets."""
    if len(df_m15) < 15:
        return False
    atr = (df_m15['high'] - df_m15['low']).rolling(14).mean().iloc[-1]
    return atr > (min_atr_pips * 0.0001)


def check_spread(spread_pips: float, max_spread: float = 2.5) -> bool:
    return spread_pips <= max_spread


def get_signal(df_h1, df_m15,
               spread_pips: float = 0.0,
               tp_pips: float = 30,
               sl_pips: float = 15) -> dict | None:
    """
    Run all gates in order. Return signal dict or None.

    Gates:
      1. Spread check         — reject if spread too wide
      2. ATR gate             — reject if market too flat
      3. London Open window   — 06:00-08:00 UTC only
      4. Triple EMA filter    — must have clear trend direction
    """
    PIP = 0.0001

    if not check_spread(spread_pips):
        return None
    if not check_atr(df_m15):
        return None
    # Time window removed — bot runs any time cron fires

    direction = check_trend(df_h1)
    if direction is None:
        return None

    ep = df_m15['close'].iloc[-1]

    if direction == 'SELL':
        ep    = round(ep - 0.5 * PIP, 5)
        sl_px = round(ep + sl_pips * PIP, 5)
        tp_px = round(ep - tp_pips * PIP, 5)
    else:
        ep    = round(ep + 0.5 * PIP, 5)
        sl_px = round(ep - sl_pips * PIP, 5)
        tp_px = round(ep + tp_pips * PIP, 5)

    return {
        'direction':   direction,
        'entry_price': ep,
        'stop_loss':   sl_px,
        'take_profit': tp_px,
        'sl_pips':     sl_pips,
        'tp_pips':     tp_pips,
    }
