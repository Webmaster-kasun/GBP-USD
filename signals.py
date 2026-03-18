"""
Signal Engine - Demo Account 2
================================
THREE STRATEGIES, THREE PAIRS:

1. AUD/USD → Mean Reversion (Asian 6am-11am SGT)
   Score: X/7, need 4 to trade
   Logic: BB touch + RSI extreme + Stochastic + M5 candle
   
2. EUR/GBP → Mean Reversion + Trend Bias (London 2pm-7pm SGT)
   Score: X/7, need 4 to trade
   Logic: Same as AUD/USD BUT trend direction blocks opposite trades
   
3. EUR/USD → Breakout + Pullback (London 2pm-6pm SGT)
   Score: X/5, need 4 to trade
   Logic: London range breakout → wait for pullback → enter
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
            return self._mean_reversion("AUD_USD", "AUDUSD")
        elif asset == "EURGBP":
            return self._mean_reversion_trend_bias("EUR_GBP", "EURGBP")
        elif asset == "EURUSD":
            return self._breakout_pullback("EUR_USD", "EURUSD")
        return 0, "NONE", "Unknown asset"

    # ══════════════════════════════════════════════════════════════════
    # STRATEGY 1: MEAN REVERSION — AUD/USD
    # Max 7 points, need 4 to trade
    # ══════════════════════════════════════════════════════════════════
    def _mean_reversion(self, instrument, asset):
        reasons = []
        bull    = 0
        bear    = 0

        # ── L1: H1 TREND GUARD ───────────────────────────────────────
        h1_closes, h1_highs, h1_lows, _ = self._fetch_candles(instrument, "H1", 60)
        if len(h1_closes) < 25:
            return 0, "NONE", "Not enough H1 data"

        h1_ema20 = self._ema(h1_closes, 20)[-1]
        h1_ema50 = self._ema(h1_closes, 50)[-1]
        _, _, _, bb_width_h1 = self._bollinger_bands(h1_closes, 20, 2)
        bb_pct_h1 = bb_width_h1 / (sum(h1_closes[-20:]) / 20)

        # Strong trend = BB too wide = skip mean reversion
        if bb_pct_h1 > 0.008:
            return 0, "NONE", "H1 strong trend (BB=" + str(round(bb_pct_h1*100,2)) + "%) skip MR"

        trending_up   = h1_ema20 > h1_ema50 * 1.0003
        trending_down = h1_ema20 < h1_ema50 * 0.9997
        ranging       = not trending_up and not trending_down

        # ── L2: M15 BB TOUCH ─────────────────────────────────────────
        m15_closes, m15_highs, m15_lows, m15_opens = self._fetch_candles(instrument, "M15", 100)
        if len(m15_closes) < 25:
            return 0, "NONE", "Not enough M15 data"

        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger_bands(m15_closes, 20, 2)
        current = m15_closes[-1]
        bb_pct  = bb_width / bb_mid

        if bb_pct > 0.006:
            return 0, "NONE", "M15 trending BB=" + str(round(bb_pct*100,2)) + "%"

        if current <= bb_lower:
            bull += 2
            reasons.append("✅ AT Lower BB")
        elif current <= bb_lower * 1.0005:
            bull += 1
            reasons.append("Near Lower BB")

        if current >= bb_upper:
            bear += 2
            reasons.append("✅ AT Upper BB")
        elif current >= bb_upper * 0.9995:
            bear += 1
            reasons.append("Near Upper BB")

        # ── L3: M15 RSI ──────────────────────────────────────────────
        rsi = self._rsi(m15_closes, 14)
        if rsi <= 28:
            bull += 2
            reasons.append("✅ RSI oversold=" + str(round(rsi,1)))
        elif rsi <= 35:
            bull += 1
            reasons.append("RSI=" + str(round(rsi,1)))
        if rsi >= 72:
            bear += 2
            reasons.append("✅ RSI overbought=" + str(round(rsi,1)))
        elif rsi >= 65:
            bear += 1
            reasons.append("RSI=" + str(round(rsi,1)))

        # ── L4: M15 STOCHASTIC ───────────────────────────────────────
        stoch = self._stochastic(m15_closes, m15_highs, m15_lows, 14)
        if stoch <= 15:
            bull += 1
            reasons.append("✅ Stoch oversold=" + str(round(stoch,1)))
        elif stoch <= 25:
            bull += 1
            reasons.append("Stoch=" + str(round(stoch,1)))
        if stoch >= 85:
            bear += 1
            reasons.append("✅ Stoch overbought=" + str(round(stoch,1)))
        elif stoch >= 75:
            bear += 1
            reasons.append("Stoch=" + str(round(stoch,1)))

        # ── L5: M5 REVERSAL CANDLE ───────────────────────────────────
        m5_closes, _, _, m5_opens = self._fetch_candles(instrument, "M5", 20)
        if len(m5_closes) >= 3:
            last_green = m5_closes[-1] > m5_opens[-1]
            prev_red   = m5_closes[-2] < m5_opens[-2]
            last_red   = m5_closes[-1] < m5_opens[-1]
            prev_green = m5_closes[-2] > m5_opens[-2]
            if bull > bear and last_green and prev_red:
                bull += 1
                reasons.append("✅ M5 bullish candle")
            if bear > bull and last_red and prev_green:
                bear += 1
                reasons.append("✅ M5 bearish candle")

        # ── TREND GUARD PENALTY ──────────────────────────────────────
        if trending_up and bear > bull:
            bear -= 2
            reasons.append("⚠️ H1 uptrend - SELL blocked")
        if trending_down and bull > bear:
            bull -= 2
            reasons.append("⚠️ H1 downtrend - BUY blocked")

        # ── RANGING BONUS ────────────────────────────────────────────
        if ranging:
            if bull > bear:
                bull += 1
                reasons.append("✅ H1 ranging - ideal!")
            elif bear > bull:
                bear += 1
                reasons.append("✅ H1 ranging - ideal!")

        bull = max(bull, 0)
        bear = max(bear, 0)

        log.info(asset + " MR: bull=" + str(bull) + " bear=" + str(bear))
        reason_str = " | ".join(reasons) if reasons else "No signals"

        if bull >= 4 and bull > bear:
            return min(bull, 7), "BUY", reason_str
        elif bear >= 4 and bear > bull:
            return min(bear, 7), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    # ══════════════════════════════════════════════════════════════════
    # STRATEGY 2: MEAN REVERSION + TREND BIAS — EUR/GBP
    # Same as AUD/USD but HARD blocks trades against H1 trend
    # Max 7 points, need 4 to trade
    # ══════════════════════════════════════════════════════════════════
    def _mean_reversion_trend_bias(self, instrument, asset):
        reasons = []
        bull    = 0
        bear    = 0

        # ── L1: H1 TREND — DETERMINES ALLOWED DIRECTION ──────────────
        h1_closes, _, _, _ = self._fetch_candles(instrument, "H1", 60)
        if len(h1_closes) < 25:
            return 0, "NONE", "Not enough H1 data"

        h1_ema20 = self._ema(h1_closes, 20)[-1]
        h1_ema50 = self._ema(h1_closes, 50)[-1]
        _, _, _, bb_width_h1 = self._bollinger_bands(h1_closes, 20, 2)
        bb_pct_h1 = bb_width_h1 / (sum(h1_closes[-20:]) / 20)

        if bb_pct_h1 > 0.008:
            return 0, "NONE", "H1 strong trend skip MR"

        trending_up   = h1_ema20 > h1_ema50 * 1.0003
        trending_down = h1_ema20 < h1_ema50 * 0.9997
        ranging       = not trending_up and not trending_down

        if trending_up:
            reasons.append("📈 H1 uptrend → BUY only")
        elif trending_down:
            reasons.append("📉 H1 downtrend → SELL only")
        else:
            reasons.append("↔️ H1 ranging → both OK")

        # ── L2: M15 BB TOUCH ─────────────────────────────────────────
        m15_closes, m15_highs, m15_lows, m15_opens = self._fetch_candles(instrument, "M15", 100)
        if len(m15_closes) < 25:
            return 0, "NONE", "Not enough M15 data"

        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger_bands(m15_closes, 20, 2)
        current = m15_closes[-1]
        bb_pct  = bb_width / bb_mid

        if bb_pct > 0.006:
            return 0, "NONE", "M15 trending BB=" + str(round(bb_pct*100,2)) + "%"

        if current <= bb_lower:
            bull += 2
            reasons.append("✅ AT Lower BB")
        elif current <= bb_lower * 1.0005:
            bull += 1
            reasons.append("Near Lower BB")

        if current >= bb_upper:
            bear += 2
            reasons.append("✅ AT Upper BB")
        elif current >= bb_upper * 0.9995:
            bear += 1
            reasons.append("Near Upper BB")

        # ── L3: RSI ──────────────────────────────────────────────────
        rsi = self._rsi(m15_closes, 14)
        if rsi <= 28:
            bull += 2
            reasons.append("✅ RSI oversold=" + str(round(rsi,1)))
        elif rsi <= 35:
            bull += 1
            reasons.append("RSI=" + str(round(rsi,1)))
        if rsi >= 72:
            bear += 2
            reasons.append("✅ RSI overbought=" + str(round(rsi,1)))
        elif rsi >= 65:
            bear += 1
            reasons.append("RSI=" + str(round(rsi,1)))

        # ── L4: STOCHASTIC ───────────────────────────────────────────
        stoch = self._stochastic(m15_closes, m15_highs, m15_lows, 14)
        if stoch <= 15:
            bull += 1
            reasons.append("✅ Stoch oversold=" + str(round(stoch,1)))
        elif stoch <= 25:
            bull += 1
            reasons.append("Stoch=" + str(round(stoch,1)))
        if stoch >= 85:
            bear += 1
            reasons.append("✅ Stoch overbought=" + str(round(stoch,1)))
        elif stoch >= 75:
            bear += 1
            reasons.append("Stoch=" + str(round(stoch,1)))

        # ── L5: M5 REVERSAL CANDLE ───────────────────────────────────
        m5_closes, _, _, m5_opens = self._fetch_candles(instrument, "M5", 20)
        if len(m5_closes) >= 3:
            last_green = m5_closes[-1] > m5_opens[-1]
            prev_red   = m5_closes[-2] < m5_opens[-2]
            last_red   = m5_closes[-1] < m5_opens[-1]
            prev_green = m5_closes[-2] > m5_opens[-2]
            if bull > bear and last_green and prev_red:
                bull += 1
                reasons.append("✅ M5 bullish candle")
            if bear > bull and last_red and prev_green:
                bear += 1
                reasons.append("✅ M5 bearish candle")

        # ── RANGING BONUS ────────────────────────────────────────────
        if ranging:
            if bull > bear:
                bull += 1
                reasons.append("✅ Ranging bonus")
            elif bear > bull:
                bear += 1
                reasons.append("✅ Ranging bonus")

        bull = max(bull, 0)
        bear = max(bear, 0)

        # ── HARD TREND BLOCK — KEY FIX FOR EUR/GBP ───────────────────
        # This is the main fix — completely block wrong direction
        if trending_down and bull > bear:
            reasons.append("🚫 H1 downtrend — BUY BLOCKED")
            log.info(asset + " BUY blocked by H1 downtrend")
            return max(bull, bear), "NONE", " | ".join(reasons)

        if trending_up and bear > bull:
            reasons.append("🚫 H1 uptrend — SELL BLOCKED")
            log.info(asset + " SELL blocked by H1 uptrend")
            return max(bull, bear), "NONE", " | ".join(reasons)

        log.info(asset + " MR+TB: bull=" + str(bull) + " bear=" + str(bear))
        reason_str = " | ".join(reasons) if reasons else "No signals"

        if bull >= 4 and bull > bear:
            return min(bull, 7), "BUY", reason_str
        elif bear >= 4 and bear > bull:
            return min(bear, 7), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    # ══════════════════════════════════════════════════════════════════
    # STRATEGY 3: BREAKOUT + PULLBACK — EUR/USD
    # Max 5 points, need 4 to trade
    # London session only 2pm-6pm SGT
    # ══════════════════════════════════════════════════════════════════
    def _breakout_pullback(self, instrument, asset):
        reasons = []
        now     = datetime.now(self.sg_tz)
        today   = now.strftime("%Y-%m-%d")

        # ── STEP 1: BUILD LONDON OPENING RANGE (first 30 mins) ───────
        # London opens at 2pm SGT → range = 2:00-2:30pm SGT
        # After 2:30pm, range is locked in for the day
        if self.london_range["date"] != today:
            self.london_range = {"date": today, "high": None, "low": None, "ready": False}

        if not self.london_range["ready"]:
            # Fetch M5 candles from London open
            m5_closes, m5_highs, m5_lows, _ = self._fetch_candles(instrument, "M5", 50)
            if len(m5_closes) < 6:
                return 0, "NONE", "Not enough M5 data for range"

            # Use last 6 M5 candles = 30 mins = London opening range
            range_high = max(m5_highs[-6:])
            range_low  = min(m5_lows[-6:])
            range_size = (range_high - range_low) / 0.0001  # in pips

            # Range must be meaningful (5-30 pips)
            if range_size < 5:
                return 0, "NONE", "Range too small (" + str(round(range_size,1)) + " pips)"
            if range_size > 40:
                return 0, "NONE", "Range too wide (" + str(round(range_size,1)) + " pips) - news?"

            self.london_range["high"]  = range_high
            self.london_range["low"]   = range_low
            self.london_range["ready"] = True
            log.info("EUR/USD London range set: " + str(round(range_low,5)) +
                     " - " + str(round(range_high,5)) +
                     " (" + str(round(range_size,1)) + " pips)")

        range_high = self.london_range["high"]
        range_low  = self.london_range["low"]
        reasons.append("Range: " + str(round(range_low,5)) + "-" + str(round(range_high,5)))

        # ── STEP 2: DETECT BREAKOUT ───────────────────────────────────
        m15_closes, m15_highs, m15_lows, m15_opens = self._fetch_candles(instrument, "M15", 50)
        if len(m15_closes) < 10:
            return 0, "NONE", "Not enough M15 data"

        current  = m15_closes[-1]
        prev     = m15_closes[-2]
        pip      = 0.0001

        # Breakout confirmed = closed beyond range by at least 3 pips
        broke_up   = prev > range_high + (3 * pip)  # Previous candle broke up
        broke_down = prev < range_low  - (3 * pip)  # Previous candle broke down

        bull = 0
        bear = 0

        if broke_up:
            bull += 2
            reasons.append("✅ Breakout UP above " + str(round(range_high,5)))
        elif broke_down:
            bear += 2
            reasons.append("✅ Breakout DOWN below " + str(round(range_low,5)))
        else:
            return 0, "NONE", "No breakout yet | " + " | ".join(reasons)

        # ── STEP 3: PULLBACK TO BROKEN LEVEL ─────────────────────────
        # After breakout up → price pulls back near range_high = BUY
        # After breakout down → price pulls back near range_low = SELL
        pullback_zone = 5 * pip  # Within 5 pips of broken level

        if broke_up:
            near_level = abs(current - range_high) <= pullback_zone
            if near_level and current > range_high - pullback_zone:
                bull += 1
                reasons.append("✅ Pullback to breakout level")
            elif current > range_high:
                reasons.append("Waiting for pullback (price above level)")
                return max(bull,bear), "NONE", " | ".join(reasons)
            else:
                reasons.append("Price below breakout level - too deep")
                return 0, "NONE", " | ".join(reasons)

        if broke_down:
            near_level = abs(current - range_low) <= pullback_zone
            if near_level and current < range_low + pullback_zone:
                bear += 1
                reasons.append("✅ Pullback to breakout level")
            elif current < range_low:
                reasons.append("Waiting for pullback")
                return max(bull,bear), "NONE", " | ".join(reasons)
            else:
                reasons.append("Price above breakout level - too deep")
                return 0, "NONE", " | ".join(reasons)

        # ── STEP 4: BOUNCE CONFIRMATION ───────────────────────────────
        # M15 candle must show rejection from the level
        if broke_up and current > m15_closes[-2]:
            bull += 1
            reasons.append("✅ M15 bouncing up from level")
        if broke_down and current < m15_closes[-2]:
            bear += 1
            reasons.append("✅ M15 rejecting down from level")

        # ── STEP 5: MACD MOMENTUM ────────────────────────────────────
        macd_line, signal_line = self._macd(m15_closes)
        macd_hist = macd_line - signal_line

        if broke_up and macd_hist > 0:
            bull += 1
            reasons.append("✅ MACD bullish")
        elif broke_up and macd_hist <= 0:
            reasons.append("⚠️ MACD weak")

        if broke_down and macd_hist < 0:
            bear += 1
            reasons.append("✅ MACD bearish")
        elif broke_down and macd_hist >= 0:
            reasons.append("⚠️ MACD weak")

        bull = max(bull, 0)
        bear = max(bear, 0)

        log.info(asset + " B+P: bull=" + str(bull) + " bear=" + str(bear))
        reason_str = " | ".join(reasons)

        if bull >= 4 and bull > bear:
            return min(bull, 5), "BUY", reason_str
        elif bear >= 4 and bear > bull:
            return min(bear, 5), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    # ══════════════════════════════════════════════════════════════════
    # MATH HELPERS
    # ══════════════════════════════════════════════════════════════════
    def _bollinger_bands(self, closes, period=20, std_dev=2):
        if len(closes) < period:
            avg = sum(closes) / len(closes)
            return avg, avg, avg, 0
        recent   = closes[-period:]
        middle   = sum(recent) / period
        variance = sum((x - middle) ** 2 for x in recent) / period
        std      = math.sqrt(variance)
        return middle + std_dev*std, middle, middle - std_dev*std, std_dev*std*2

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

    def _macd(self, closes, fast=12, slow=26, signal=9):
        if len(closes) < slow + signal:
            return 0, 0
        ema_fast    = self._ema(closes, fast)
        ema_slow    = self._ema(closes, slow)
        min_len     = min(len(ema_fast), len(ema_slow))
        macd_line   = [ema_fast[-(min_len-i)] - ema_slow[-(min_len-i)] for i in range(min_len)]
        signal_line = self._ema(macd_line, signal)
        return macd_line[-1], signal_line[-1]

    def _stochastic(self, closes, highs, lows, period=14):
        if len(closes) < period: return 50
        h = max(highs[-period:])
        l = min(lows[-period:])
        if h == l: return 50
        return ((closes[-1] - l) / (h - l)) * 100
