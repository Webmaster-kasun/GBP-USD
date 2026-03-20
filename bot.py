"""
Signal Engine - Demo Account 2
================================
THREE PAIRS — ALL USING M15 SCALP STRATEGY:

1. AUD/USD → M15 Scalp (Asian 6am-11am SGT)
2. EUR/GBP → M15 Scalp (London 2pm-7pm SGT)
3. EUR/USD → M15 Scalp (London 2pm-6pm SGT)

Scalp Logic (score 3/3 required):
  L1: M15 EMA8 vs EMA21 bias filter
  L2: RSI(7) snap zone — <38 BUY / >62 SELL
  L3: M5 trigger candle — engulf or pin-bar
  SL: 3-5 pip | TP: 8-10 pip | R:R ~2:1
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
            record.msg = record.msg.replace(self.api_key, "***")
        return True

log.addFilter(SafeFilter())

class SignalEngine:
    def __init__(self):
        self.sg_tz      = pytz.timezone("Asia/Singapore")
        self.api_key    = os.environ.get("OANDA_API_KEY", "")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        self.base_url   = "https://api-fxpractice.oanda.com"
        self.headers    = {"Authorization": "Bearer " + self.api_key}

        # Store London range for EUR/USD breakout strategy
        # Persists across 5-min scans within same Railway process
        self.london_range = {
            "date":  None,
            "high":  None,
            "low":   None,
            "ready": False
        }

    OANDA_MAP = {
        "AUDUSD": "AUD_USD",
        "EURGBP": "EUR_GBP",
        "EURUSD": "EUR_USD",
    }

    def _fetch_candles(self, instrument, granularity, count=100):
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
                    opens   = [float(x["mid"]["o"]) for x in c]
                    return closes, highs, lows, opens
                log.warning("Candle fetch attempt " + str(attempt+1) + " failed: " + str(r.status_code))
            except Exception as e:
                log.warning("Candle fetch error: " + str(e))
        return [], [], [], []

    def analyze(self, asset="AUDUSD"):
        if asset == "AUDUSD":
            return self._scalp_m15("AUD_USD", "AUDUSD")
        elif asset == "EURGBP":
            return self._scalp_m15("EUR_GBP", "EURGBP")
        elif asset == "EURUSD":
            return self._scalp_m15("EUR_USD", "EURUSD")
        return 0, "NONE", "Unknown asset"

    # ══════════════════════════════════════════════════════════════════
    # STRATEGY: M15 SCALP — ALL PAIRS
    # Score 3/3 required to trade
    # SL: 3-5 pip | TP: 8-10 pip | R:R ~2:1
    # ══════════════════════════════════════════════════════════════════
    def _scalp_m15(self, instrument, asset):
        reasons = []
        bull    = 0
        bear    = 0

        # ── L1: M15 EMA8 vs EMA21 BIAS ───────────────────────────────
        m15_closes, m15_highs, m15_lows, m15_opens = self._fetch_candles(instrument, "M15", 60)
        if len(m15_closes) < 22:
            return 0, "NONE", "Not enough M15 data"

        ema8  = self._ema(m15_closes, 8)[-1]
        ema21 = self._ema(m15_closes, 21)[-1]

        bullish_bias = ema8 > ema21 * 1.00005
        bearish_bias = ema8 < ema21 * 0.99995

        if bullish_bias:
            bull += 1
            reasons.append("✅ EMA8>EMA21 bullish bias")
        elif bearish_bias:
            bear += 1
            reasons.append("✅ EMA8<EMA21 bearish bias")
        else:
            return 0, "NONE", "EMA8/21 flat — no bias, skip scalp"

        # ── L2: M15 RSI(7) SNAP ZONE ─────────────────────────────────
        rsi7 = self._rsi(m15_closes, 7)

        if bullish_bias and rsi7 <= 38:
            bull += 1
            reasons.append("✅ RSI7 snap oversold=" + str(round(rsi7, 1)))
        elif bearish_bias and rsi7 >= 62:
            bear += 1
            reasons.append("✅ RSI7 snap overbought=" + str(round(rsi7, 1)))
        else:
            reasons.append("RSI7=" + str(round(rsi7, 1)) + " not in snap zone")
            return max(bull, bear), "NONE", " | ".join(reasons)

        # ── L3: M5 TRIGGER CANDLE ────────────────────────────────────
        m5_closes, m5_highs, m5_lows, m5_opens = self._fetch_candles(instrument, "M5", 10)
        if len(m5_closes) < 4:
            return max(bull, bear), "NONE", " | ".join(reasons) + " | Not enough M5 data"

        c1 = m5_closes[-1]; c2 = m5_closes[-2]
        o1 = m5_opens[-1];  o2 = m5_opens[-2]
        h1 = m5_highs[-1];  l1 = m5_lows[-1]

        body1  = abs(c1 - o1)
        range1 = (h1 - l1) if (h1 - l1) > 0 else 0.00001

        # Bullish engulf: green candle closes above prior red candle's open
        bull_engulf = (c1 > o1) and (c2 < o2) and (c1 >= o2) and (o1 <= c2)
        # Bullish pin bar: lower wick >60% of range, small body
        lower_wick  = min(o1, c1) - l1
        bull_pin    = (c1 > o1) and (lower_wick / range1 > 0.60) and (body1 / range1 < 0.35)

        # Bearish engulf: red candle closes below prior green candle's open
        bear_engulf = (c1 < o1) and (c2 > o2) and (c1 <= o2) and (o1 >= c2)
        # Bearish pin bar: upper wick >60% of range, small body
        upper_wick  = h1 - max(o1, c1)
        bear_pin    = (c1 < o1) and (upper_wick / range1 > 0.60) and (body1 / range1 < 0.35)

        if bullish_bias and (bull_engulf or bull_pin):
            bull += 1
            label = "engulf" if bull_engulf else "pin-bar"
            reasons.append("✅ M5 bullish " + label)
        elif bearish_bias and (bear_engulf or bear_pin):
            bear += 1
            label = "engulf" if bear_engulf else "pin-bar"
            reasons.append("✅ M5 bearish " + label)
        else:
            reasons.append("No M5 trigger candle")
            return max(bull, bear), "NONE", " | ".join(reasons)

        reason_str = " | ".join(reasons)
        log.info(asset + " SCALP: bull=" + str(bull) + " bear=" + str(bear))

        if bull >= 3:
            return 3, "BUY", reason_str
        elif bear >= 3:
            return 3, "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    # ══════════════════════════════════════════════════════════════════
    # MATH HELPERS
    # ══════════════════════════════════════════════════════════════════
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
        if al == 0: return 100
        return 100 - (100 / (1 + ag/al))

    def _ema(self, data, period):
        if not data: return [0.0]
        if len(data) < period:
            return [sum(data)/len(data)] * len(data)
        seed = sum(data[:period]) / period
        emas = [seed] * period
        mult = 2 / (period + 1)
        for p in data[period:]:
            emas.append((p - emas[-1]) * mult + emas[-1])
        return emas
