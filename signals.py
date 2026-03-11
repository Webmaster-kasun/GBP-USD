"""
Mean Reversion Signal Engine - Demo Account 2
==============================================
Strategy: Bollinger Bands + RSI Extremes + ATR Ranging Filter
Timeframes: H1 trend context + M15 entry signals

Logic:
- Price touches Lower BB + RSI < 35 → BUY (oversold bounce)
- Price touches Upper BB + RSI > 65 → SELL (overbought reversal)
- ATR filter ensures market is RANGING not trending
- Take profit = Middle Bollinger Band (20 EMA)
"""

import os
import requests
import logging
import math
from datetime import datetime
import pytz

log = logging.getLogger(__name__)

class SafeFilter(logging.Filter):
    def __init__(self):
        self.api_key = os.environ.get("OANDA_API_KEY", "")
    def filter(self, record):
        if self.api_key and self.api_key in str(record.getMessage()):
            record.msg = record.msg.replace(self.api_key, "***API_KEY***")
        return True

safe_filter = SafeFilter()
log.addFilter(safe_filter)

class SignalEngine:
    def __init__(self):
        self.sg_tz      = pytz.timezone("Asia/Singapore")
        self.asset      = "EURUSD"
        self.api_key    = os.environ.get("OANDA_API_KEY", "")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        self.base_url   = "https://api-fxpractice.oanda.com"
        self.headers    = {"Authorization": "Bearer " + self.api_key}

    OANDA_MAP = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "AUDUSD": "AUD_USD",
        "EURGBP": "EUR_GBP",
        "USDCAD": "USD_CAD",
        "USDCHF": "USD_CHF",
        "USDJPY": "USD_JPY",
        "XAGUSD": "XAG_USD",
        "XAUUSD": "XAU_USD",
    }

    def _fetch_candles(self, instrument, granularity, count=200):
        """Fetch candles with retry logic - 3 attempts"""
        url    = self.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=10)
                if r.status_code == 200:
                    candles = r.json()["candles"]
                    c       = [x for x in candles if x["complete"]]
                    closes  = [float(x["mid"]["c"]) for x in c]
                    highs   = [float(x["mid"]["h"]) for x in c]
                    lows    = [float(x["mid"]["l"]) for x in c]
                    return closes, highs, lows
                log.warning("Candle fetch attempt " + str(attempt+1) + " failed: " + str(r.status_code))
            except Exception as e:
                log.warning("Candle fetch attempt " + str(attempt+1) + " error: " + str(e))
        return [], [], []

    def _fetch_yahoo(self, ticker, interval="1d", range_="5d"):
        """Fetch Yahoo Finance data with retry"""
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=" + interval + "&range=" + range_
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
                    return closes
            except Exception as e:
                log.warning("Yahoo attempt " + str(attempt+1) + " error: " + str(e))
        return []

    def analyze(self, asset="EURUSD"):
        self.asset = asset
        log.info("Mean Reversion analyzing " + asset + "...")
        if asset in ["XAUUSD", "XAGUSD"]:
            return self._analyze_gold_reversion()
        return self._analyze_forex_reversion()

    def _is_jpy_pair(self):
        return "JPY" in self.asset

    # ══════════════════════════════════════════════════════════
    # FOREX MEAN REVERSION
    # H1: Bollinger Bands (20,2) + ATR ranging filter
    # M15: RSI extremes + Stochastic confirmation
    # ══════════════════════════════════════════════════════════
    def _analyze_forex_reversion(self):
        instrument = self.OANDA_MAP.get(self.asset, "EUR_USD")
        reasons    = []
        bull = 0
        bear = 0

        # ── H1 BOLLINGER BANDS + ATR RANGING FILTER ──────────
        h1_closes, h1_highs, h1_lows = self._fetch_candles(instrument, "H1", 100)
        if len(h1_closes) < 30:
            log.warning(self.asset + " not enough H1 data")
            return 0, "NONE", "Not enough H1 data"

        bb_upper, bb_middle, bb_lower, bb_width = self._bollinger_bands(h1_closes, 20, 2)
        current_h1 = h1_closes[-1]

        # ATR ranging filter - skip if market is strongly trending
        atr_h1     = self._atr(h1_highs, h1_lows, h1_closes, 14)
        atr_pct    = atr_h1 / current_h1  # ATR as % of price
        bb_pct     = bb_width / bb_middle  # BB width as % of price

        log.info(self.asset + " H1 BB upper=" + str(round(bb_upper, 5)) +
                 " mid=" + str(round(bb_middle, 5)) +
                 " lower=" + str(round(bb_lower, 5)) +
                 " width%=" + str(round(bb_pct*100, 3)))
        log.info(self.asset + " H1 ATR%=" + str(round(atr_pct*100, 4)) + " price=" + str(round(current_h1, 5)))

        # If BB is very wide = strongly trending = skip mean reversion
        if bb_pct > 0.008:  # BB wider than 0.8% of price = trending
            log.info(self.asset + " BB too wide (trending market) - skip mean reversion")
            return 0, "NONE", "Market trending - BB too wide for mean reversion"

        # Check H1 BB touch
        h1_near_lower = current_h1 <= bb_lower * 1.001   # Within 0.1% of lower band
        h1_near_upper = current_h1 >= bb_upper * 0.999   # Within 0.1% of upper band

        if h1_near_lower:
            bull += 2
            reasons.append("H1 price at Lower BB - oversold!")
        elif current_h1 < bb_middle:
            bull += 1
            reasons.append("H1 price below middle BB")

        if h1_near_upper:
            bear += 2
            reasons.append("H1 price at Upper BB - overbought!")
        elif current_h1 > bb_middle:
            bear += 1
            reasons.append("H1 price above middle BB")

        # ── M15 RSI + STOCHASTIC ENTRY ────────────────────────
        m15_closes, m15_highs, m15_lows = self._fetch_candles(instrument, "M15", 100)
        if len(m15_closes) < 30:
            log.warning(self.asset + " not enough M15 data")
            return 0, "NONE", "Not enough M15 data"

        rsi_m15   = self._rsi(m15_closes, 14)
        stoch_m15 = self._stochastic(m15_closes, m15_highs, m15_lows, 14)

        # M15 Bollinger on entry timeframe
        m15_bb_upper, m15_bb_mid, m15_bb_lower, _ = self._bollinger_bands(m15_closes, 20, 2)
        current_m15 = m15_closes[-1]

        log.info(self.asset + " M15 RSI=" + str(round(rsi_m15, 1)) +
                 " Stoch=" + str(round(stoch_m15, 1)) +
                 " price=" + str(round(current_m15, 5)))

        # RSI confirmation
        if rsi_m15 < 30:
            bull += 2
            reasons.append("M15 RSI oversold=" + str(round(rsi_m15, 0)))
        elif rsi_m15 < 40:
            bull += 1
            reasons.append("M15 RSI low=" + str(round(rsi_m15, 0)))

        if rsi_m15 > 70:
            bear += 2
            reasons.append("M15 RSI overbought=" + str(round(rsi_m15, 0)))
        elif rsi_m15 > 60:
            bear += 1
            reasons.append("M15 RSI high=" + str(round(rsi_m15, 0)))

        # Stochastic confirmation
        if stoch_m15 < 20:
            bull += 1
            reasons.append("Stoch oversold=" + str(round(stoch_m15, 0)))
        if stoch_m15 > 80:
            bear += 1
            reasons.append("Stoch overbought=" + str(round(stoch_m15, 0)))

        # M15 BB touch confirmation
        if current_m15 <= m15_bb_lower * 1.001:
            bull += 1
            reasons.append("M15 Lower BB touch")
        if current_m15 >= m15_bb_upper * 0.999:
            bear += 1
            reasons.append("M15 Upper BB touch")

        # Macro USD direction (inverse for mean reversion)
        macro = self._macro_check()
        if macro == "BULL" and bull > bear:
            bull += 1
            reasons.append("Macro supports BUY")
        elif macro == "BEAR" and bear > bull:
            bear += 1
            reasons.append("Macro supports SELL")

        log.info(self.asset + " MeanRev bull=" + str(bull) + " bear=" + str(bear))

        reason_str = " | ".join(reasons) if reasons else "No signals"

        if bull >= 4 and bull > bear:
            return min(bull, 5), "BUY", reason_str
        elif bear >= 4 and bear > bull:
            return min(bear, 5), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    def _macro_check(self):
        """Quick USD direction check"""
        try:
            closes = self._fetch_yahoo("DX-Y.NYB", "1h", "2d")
            if len(closes) >= 3:
                chg = ((closes[-1] - closes[-3]) / closes[-3]) * 100
                if chg < -0.15:
                    return "BULL"
                elif chg > 0.15:
                    return "BEAR"
        except:
            pass
        return "NEUTRAL"

    # ══════════════════════════════════════════════════════════
    # GOLD MEAN REVERSION
    # H1: BB(20,2) + ATR filter
    # M15: RSI + Stochastic extremes
    # ══════════════════════════════════════════════════════════
    def _analyze_gold_reversion(self):
        reasons = []
        bull = 0
        bear = 0

        # Gold H1 Bollinger Bands
        h1_closes, h1_highs, h1_lows = self._fetch_candles("XAU_USD", "H1", 100)
        if len(h1_closes) < 30:
            return 0, "NONE", "Not enough H1 data"

        bb_upper, bb_middle, bb_lower, bb_width = self._bollinger_bands(h1_closes, 20, 2)
        current   = h1_closes[-1]
        atr_h1    = self._atr(h1_highs, h1_lows, h1_closes, 14)
        bb_pct    = bb_width / bb_middle

        log.info("Gold H1 BB upper=" + str(round(bb_upper, 2)) +
                 " mid=" + str(round(bb_middle, 2)) +
                 " lower=" + str(round(bb_lower, 2)) +
                 " ATR=" + str(round(atr_h1, 2)))

        # Gold trending filter - wider allowed due to gold volatility
        if bb_pct > 0.025:
            log.info("Gold BB too wide (trending) - skip mean reversion")
            return 0, "NONE", "Gold trending - skip mean reversion"

        # BB touch
        if current <= bb_lower * 1.002:
            bull += 2
            reasons.append("Gold at Lower BB oversold!")
        elif current < bb_middle:
            bull += 1
            reasons.append("Gold below BB midline")

        if current >= bb_upper * 0.998:
            bear += 2
            reasons.append("Gold at Upper BB overbought!")
        elif current > bb_middle:
            bear += 1
            reasons.append("Gold above BB midline")

        # RSI
        rsi_h1 = self._rsi(h1_closes, 14)
        log.info("Gold H1 RSI=" + str(round(rsi_h1, 1)))

        if rsi_h1 < 35:
            bull += 2
            reasons.append("Gold RSI oversold=" + str(round(rsi_h1, 0)))
        elif rsi_h1 < 45:
            bull += 1
            reasons.append("Gold RSI low=" + str(round(rsi_h1, 0)))

        if rsi_h1 > 65:
            bear += 2
            reasons.append("Gold RSI overbought=" + str(round(rsi_h1, 0)))
        elif rsi_h1 > 55:
            bear += 1
            reasons.append("Gold RSI high=" + str(round(rsi_h1, 0)))

        # M15 Stochastic for entry timing
        m15_closes, m15_highs, m15_lows = self._fetch_candles("XAU_USD", "M15", 60)
        if len(m15_closes) >= 20:
            stoch = self._stochastic(m15_closes, m15_highs, m15_lows, 14)
            rsi_m15 = self._rsi(m15_closes, 14)
            log.info("Gold M15 Stoch=" + str(round(stoch, 1)) + " RSI=" + str(round(rsi_m15, 1)))

            if stoch < 20:
                bull += 1
                reasons.append("Gold M15 Stoch oversold=" + str(round(stoch, 0)))
            if stoch > 80:
                bear += 1
                reasons.append("Gold M15 Stoch overbought=" + str(round(stoch, 0)))

            if rsi_m15 < 35:
                bull += 1
                reasons.append("Gold M15 RSI oversold")
            if rsi_m15 > 65:
                bear += 1
                reasons.append("Gold M15 RSI overbought")

        # DXY for gold macro
        dxy = self._fetch_yahoo("DX-Y.NYB", "1h", "2d")
        if len(dxy) >= 3:
            chg = ((dxy[-1] - dxy[-3]) / dxy[-3]) * 100
            if chg < -0.3 and bull > bear:
                bull += 1
                reasons.append("USD falling supports Gold BUY")
            elif chg > 0.3 and bear > bull:
                bear += 1
                reasons.append("USD rising supports Gold SELL")

        log.info("Gold MeanRev bull=" + str(bull) + " bear=" + str(bear))

        reason_str = " | ".join(reasons) if reasons else "No signals"
        if bull >= 4 and bull > bear:
            return min(bull, 5), "BUY", reason_str
        elif bear >= 4 and bear > bull:
            return min(bear, 5), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    # ══════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════
    def _bollinger_bands(self, closes, period=20, std_dev=2):
        """Calculate Bollinger Bands - returns upper, middle, lower, width"""
        if len(closes) < period:
            avg = sum(closes) / len(closes)
            return avg, avg, avg, 0
        recent = closes[-period:]
        middle = sum(recent) / period
        variance = sum((x - middle) ** 2 for x in recent) / period
        std = math.sqrt(variance)
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        width = upper - lower
        return upper, middle, lower, width

    def _rsi(self, closes, period=14):
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        if len(gains) < period:
            return 50
        ag = sum(gains[-period:]) / period
        al = sum(losses[-period:]) / period
        if al == 0:
            return 100
        return 100 - (100 / (1 + ag / al))

    def _ema(self, data, period):
        if not data:
            return [0.0]
        if len(data) < period:
            avg = sum(data) / len(data)
            return [avg] * len(data)
        seed = sum(data[:period]) / period
        emas = [seed] * period
        mult = 2 / (period + 1)
        for p in data[period:]:
            emas.append((p - emas[-1]) * mult + emas[-1])
        return emas

    def _stochastic(self, closes, highs, lows, period=14):
        if len(closes) < period:
            return 50
        h = max(highs[-period:])
        l = min(lows[-period:])
        if h == l:
            return 50
        return ((closes[-1] - l) / (h - l)) * 100

    def _atr(self, highs, lows, closes, period=14):
        if len(closes) < period + 1:
            return 0.001
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            trs.append(tr)
        return sum(trs[-period:]) / period
