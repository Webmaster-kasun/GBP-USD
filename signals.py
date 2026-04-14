def check_trend(df_h1):
    ema50 = df_h1['close'].ewm(span=50).mean().iloc[-1]
    price = df_h1['close'].iloc[-1]

    if price > ema50:
        return "BUY"
    elif price < ema50:
        return "SELL"
    return None


def check_breakout(df_m15):
    high = df_m15['high'].rolling(10).max().iloc[-2]
    low = df_m15['low'].rolling(10).min().iloc[-2]
    close = df_m15['close'].iloc[-1]

    atr = (df_m15['high'] - df_m15['low']).rolling(14).mean().iloc[-1]
    buffer = atr * 0.5

    if close > high + buffer:
        return "BUY"
    elif close < low - buffer:
        return "SELL"
    return None


def check_pullback(df_m5, direction):
    ema21 = df_m5['close'].ewm(span=21).mean().iloc[-1]
    price = df_m5['close'].iloc[-1]

    diff = abs(price - ema21)

    if diff < 0.0005:
        candle = df_m5.iloc[-1]
        body = abs(candle['close'] - candle['open'])
        total = candle['high'] - candle['low']

        if total > 0 and (body / total) > 0.45:
            return direction

    return None


def check_atr(df_m15):
    atr = (df_m15['high'] - df_m15['low']).rolling(14).mean().iloc[-1]
    return atr > 0.0008
