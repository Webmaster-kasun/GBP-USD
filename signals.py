"""
Mean Reversion Signal Engine - Demo Account 2
==============================================
TARGET: 60% win rate on short trades (< 1 hour)

5-Layer Scoring System (max 8 points, need 4 to trade):
  Layer 1: H1 Trend Guard     → blocks trades against H1 trend
  Layer 2: M15 BB Touch       → primary entry signal (2pts)
  Layer 3: M15 RSI Extreme    → oversold/overbought confirm (2pts)
  Layer 4: M15 Stochastic     → momentum exhaustion (1pt)
  Layer 5: M5 Candle Pattern  → entry timing (1pt - micro confirm)
  Bonus:   Macro USD direction → extra point if macro agrees

Win rate logic:
  - Only trade when price is AT the BB band (not near it)
  - Only trade when RSI is truly extreme (< 30 or > 70)
  - Trend Guard ensures we trade WITH range, not against trend
  - M5 candle pattern = precise entry timing = better fill price
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

    def _fetch_yahoo(self, ticker, interval="1d", range_="5d"):
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
        log.info("Analyzing " + asset + " for fast mean reversion...")
        if asset in ["XAUUSD", "XAGUSD"]:
            return self._analyze_metals_reversion()
        return self._analyze_forex_reversion()

    # ══════════════════════════════════════════════════════════════════
    # FOREX MEAN REVERSION — 5 Layer System
    # Target: 60% win rate, trades complete < 1 hour
    # Pairs: EUR/USD, AUD/USD, EUR/GBP, USD/CHF
    # ══════════════════════════════════════════════════════════════════
    def _analyze_forex_reversion(self):
        instrument = self.OANDA_MAP.get(self.asset, "EUR_USD")
        reasons    = []
        bull       = 0
        bear       = 0

        # ── LAYER 1: H1 TREND GUARD ─────────────────────────────────
        # Do NOT trade against the H1 trend — biggest cause of losses!
        h1_closes, h1_highs, h1_lows, h1_opens = self._fetch_candles(instrument, "H1", 60)
        if len(h1_closes) < 25:
            return 0, "NONE", "Not enough H1 data"

        h1_ema20   = self._ema(h1_closes, 20)[-1]
        h1_ema50   = self._ema(h1_closes, min(50, len(h1_closes)))[-1]
        _, _, _, bb_width_h1 = self._bollinger_bands(h1_closes, 20, 2)
        bb_mid_h1  = sum(h1_closes[-20:]) / 20
        bb_pct_h1  = bb_width_h1 / bb_mid_h1

        # Strong trend = BB very wide = skip mean reversion entirely
        if bb_pct_h1 > 0.008:
            log.info(self.asset + " H1 BB too wide=" + str(round(bb_pct_h1*100, 3)) + "% - strong trend, skip")
            return 0, "NONE", "H1 strong trend (BB wide=" + str(round(bb_pct_h1*100,2)) + "%) - skip MR"

        trending_up   = h1_ema20 > h1_ema50 * 1.0003
        trending_down = h1_ema20 < h1_ema50 * 0.9997
        ranging       = not trending_up and not trending_down

        log.info(self.asset + " H1 EMA20=" + str(round(h1_ema20, 5)) +
                 " EMA50=" + str(round(h1_ema50, 5)) +
                 " BB_width%=" + str(round(bb_pct_h1*100, 3)) +
                 " ranging=" + str(ranging))

        # ── LAYER 2: M15 BOLLINGER BAND TOUCH ───────────────────────
        # Primary entry signal — price must be AT the band not just near it
        m15_closes, m15_highs, m15_lows, m15_opens = self._fetch_candles(instrument, "M15", 100)
        if len(m15_closes) < 25:
            return 0, "NONE", "Not enough M15 data"

        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger_bands(m15_closes, 20, 2)
        current = m15_closes[-1]
        bb_pct  = bb_width / bb_mid

        # M15 BB also too wide = trending on M15 too = skip
        if bb_pct > 0.006:
            log.info(self.asset + " M15 BB too wide=" + str(round(bb_pct*100, 3)) + "% - trending")
            return 0, "NONE", "M15 trending - BB width=" + str(round(bb_pct*100, 2)) + "%"

        # Strict BB touch — price must be AT or beyond the band
        at_lower_bb = current <= bb_lower             # At or below lower band
        at_upper_bb = current >= bb_upper             # At or above upper band
        near_lower  = current <= bb_lower * 1.0005   # Within 0.05% of lower
        near_upper  = current >= bb_upper * 0.9995   # Within 0.05% of upper

        if at_lower_bb:
            bull += 2
            reasons.append("✅ M15 AT Lower BB (" + str(round(current, 5)) + "≤" + str(round(bb_lower, 5)) + ")")
        elif near_lower:   # elif = no double count
            bull += 1
            reasons.append("M15 near Lower BB")

        if at_upper_bb:
            bear += 2
            reasons.append("✅ M15 AT Upper BB (" + str(round(current, 5)) + "≥" + str(round(bb_upper, 5)) + ")")
        elif near_upper:   # elif = no double count
            bear += 1
            reasons.append("M15 near Upper BB")

        # ── LAYER 3: M15 RSI EXTREME ────────────────────────────────
        # Must be truly extreme for high win rate — not just 40 or 60
        rsi = self._rsi(m15_closes, 14)
        log.info(self.asset + " M15 RSI=" + str(round(rsi, 1)) + " price=" + str(round(current, 5)))

        if rsi <= 28:       # Very oversold = strong bounce signal
            bull += 2
            reasons.append("✅ M15 RSI very oversold=" + str(round(rsi, 0)))
        elif rsi <= 35:     # Oversold = decent signal
            bull += 1
            reasons.append("M15 RSI oversold=" + str(round(rsi, 0)))

        if rsi >= 72:       # Very overbought = strong reversal signal
            bear += 2
            reasons.append("✅ M15 RSI very overbought=" + str(round(rsi, 0)))
        elif rsi >= 65:     # Overbought = decent signal
            bear += 1
            reasons.append("M15 RSI overbought=" + str(round(rsi, 0)))

        # ── LAYER 4: M15 STOCHASTIC EXHAUSTION ──────────────────────
        # Stochastic shows momentum exhaustion — good for fast trades
        stoch = self._stochastic(m15_closes, m15_highs, m15_lows, 14)
        log.info(self.asset + " M15 Stoch=" + str(round(stoch, 1)))

        if stoch <= 15:     # Deep oversold only
            bull += 1
            reasons.append("✅ M15 Stoch deep oversold=" + str(round(stoch, 0)))
        elif stoch <= 25:   # elif = no double count
            bull += 1
            reasons.append("M15 Stoch oversold=" + str(round(stoch, 0)))

        if stoch >= 85:     # Deep overbought only
            bear += 1
            reasons.append("✅ M15 Stoch deep overbought=" + str(round(stoch, 0)))
        elif stoch >= 75:   # elif = no double count
            bear += 1
            reasons.append("M15 Stoch overbought=" + str(round(stoch, 0)))

        # ── LAYER 5: M5 CANDLE REVERSAL PATTERN ─────────────────────
        # Confirms the bounce is already starting — best entry timing!
        m5_closes, m5_highs, m5_lows, m5_opens = self._fetch_candles(instrument, "M5", 20)
        if len(m5_closes) >= 5:
            # Bullish reversal: last candle is green after red candles
            last_green   = m5_closes[-1] > m5_opens[-1]
            prev_red     = m5_closes[-2] < m5_opens[-2]
            # Bearish reversal: last candle is red after green candles
            last_red     = m5_closes[-1] < m5_opens[-1]
            prev_green   = m5_closes[-2] > m5_opens[-2]

            # Bullish engulf pattern = strong bounce starting
            bullish_engulf = (last_green and prev_red and
                             m5_closes[-1] > m5_opens[-2] and
                             m5_opens[-1] < m5_closes[-2])

            # Bearish engulf = strong drop starting
            bearish_engulf = (last_red and prev_green and
                             m5_closes[-1] < m5_opens[-2] and
                             m5_opens[-1] > m5_closes[-2])

            if bull > bear and last_green and prev_red:
                bull += 1
                if bullish_engulf:
                    reasons.append("✅ M5 bullish engulf - bounce started!")
                else:
                    reasons.append("M5 reversal candle green")

            if bear > bull and last_red and prev_green:
                bear += 1
                if bearish_engulf:
                    reasons.append("✅ M5 bearish engulf - drop started!")
                else:
                    reasons.append("M5 reversal candle red")

        # ── TREND GUARD PENALTY ──────────────────────────────────────
        # If H1 is trending, penalise signals going against the trend
        if trending_up and bear > bull:
            bear -= 2
            reasons.append("⚠️ H1 uptrend - SELL blocked")
        if trending_down and bull > bear:
            bull -= 2
            reasons.append("⚠️ H1 downtrend - BUY blocked")

        # Ranging market = bonus point (ideal for MR)
        if ranging:
            if bull > bear:
                bull += 1
                reasons.append("✅ H1 ranging - ideal for MR!")
            elif bear > bull:
                bear += 1
                reasons.append("✅ H1 ranging - ideal for MR!")

        bull = max(bull, 0)
        bear = max(bear, 0)

        log.info(self.asset + " Final: bull=" + str(bull) + " bear=" + str(bear) +
                 " | " + " | ".join(reasons))

        reason_str = " | ".join(reasons) if reasons else "No signals"

        if bull >= 3 and bull > bear:
            return min(bull, 8), "BUY", reason_str
        elif bear >= 3 and bear > bull:
            return min(bear, 8), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    # ══════════════════════════════════════════════════════════════════
    # METALS MEAN REVERSION (XAG/USD, XAU/USD)
    # Same 5-layer logic adapted for metals
    # ══════════════════════════════════════════════════════════════════
    def _analyze_metals_reversion(self):
        instrument = self.OANDA_MAP.get(self.asset, "XAG_USD")
        reasons    = []
        bull       = 0
        bear       = 0

        # H1 data
        h1_closes, h1_highs, h1_lows, h1_opens = self._fetch_candles(instrument, "H1", 60)
        if len(h1_closes) < 25:
            return 0, "NONE", "Not enough H1 data"

        bb_upper_h1, bb_mid_h1, bb_lower_h1, bb_width_h1 = self._bollinger_bands(h1_closes, 20, 2)
        bb_pct_h1 = bb_width_h1 / bb_mid_h1
        h1_ema20  = self._ema(h1_closes, 20)[-1]
        h1_ema50  = self._ema(h1_closes, min(50, len(h1_closes)))[-1]

        # Metals trending filter (wider allowed)
        if bb_pct_h1 > 0.02:
            return 0, "NONE", "Metal trending (BB wide=" + str(round(bb_pct_h1*100,2)) + "%) - skip"

        trending_up   = h1_ema20 > h1_ema50 * 1.001
        trending_down = h1_ema20 < h1_ema50 * 0.999
        ranging       = not trending_up and not trending_down

        # M15 BB touch
        m15_closes, m15_highs, m15_lows, m15_opens = self._fetch_candles(instrument, "M15", 80)
        if len(m15_closes) < 25:
            return 0, "NONE", "Not enough M15 data"

        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger_bands(m15_closes, 20, 2)
        current = m15_closes[-1]
        rsi     = self._rsi(m15_closes, 14)
        stoch   = self._stochastic(m15_closes, m15_highs, m15_lows, 14)

        log.info(self.asset + " M15 price=" + str(round(current, 3)) +
                 " RSI=" + str(round(rsi, 1)) + " Stoch=" + str(round(stoch, 1)) +
                 " BB_lower=" + str(round(bb_lower, 3)) + " BB_upper=" + str(round(bb_upper, 3)))

        # Layer 2: BB touch
        if current <= bb_lower:
            bull += 2
            reasons.append("✅ M15 AT Lower BB")
        elif current <= bb_lower * 1.001:
            bull += 1
            reasons.append("M15 near Lower BB")
        if current >= bb_upper:
            bear += 2
            reasons.append("✅ M15 AT Upper BB")
        elif current >= bb_upper * 0.999:
            bear += 1
            reasons.append("M15 near Upper BB")

        # Layer 3: RSI
        if rsi <= 30:
            bull += 2
            reasons.append("✅ RSI very oversold=" + str(round(rsi, 0)))
        elif rsi <= 38:
            bull += 1
            reasons.append("RSI oversold=" + str(round(rsi, 0)))
        if rsi >= 70:
            bear += 2
            reasons.append("✅ RSI very overbought=" + str(round(rsi, 0)))
        elif rsi >= 62:
            bear += 1
            reasons.append("RSI overbought=" + str(round(rsi, 0)))

        # Layer 4: Stochastic
        if stoch <= 20:
            bull += 1
            reasons.append("✅ Stoch oversold=" + str(round(stoch, 0)))
        if stoch >= 80:
            bear += 1
            reasons.append("✅ Stoch overbought=" + str(round(stoch, 0)))

        # Layer 5: M5 candle
        m5_closes, _, _, m5_opens = self._fetch_candles(instrument, "M5", 15)
        if len(m5_closes) >= 3:
            if bull > bear and m5_closes[-1] > m5_opens[-1] and m5_closes[-2] < m5_opens[-2]:
                bull += 1
                reasons.append("M5 bullish reversal candle")
            if bear > bull and m5_closes[-1] < m5_opens[-1] and m5_closes[-2] > m5_opens[-2]:
                bear += 1
                reasons.append("M5 bearish reversal candle")

        # DXY macro for metals
        try:
            dxy = self._fetch_yahoo("DX-Y.NYB", "1h", "2d")
            if len(dxy) >= 3:
                chg = ((dxy[-1] - dxy[-3]) / dxy[-3]) * 100
                if chg < -0.3 and bull > bear:
                    bull += 1
                    reasons.append("✅ USD falling - metals BUY boost")
                elif chg > 0.3 and bear > bull:
                    bear += 1
                    reasons.append("✅ USD rising - metals SELL boost")
        except:
            pass

        # Trend guard
        if trending_up and bear > bull:
            bear -= 2
            reasons.append("⚠️ H1 uptrend - SELL blocked")
        if trending_down and bull > bear:
            bull -= 2
            reasons.append("⚠️ H1 downtrend - BUY blocked")
        if ranging:
            if bull > bear:
                bull += 1
                reasons.append("✅ H1 ranging - ideal for MR!")
            elif bear > bull:
                bear += 1
                reasons.append("✅ H1 ranging - ideal for MR!")

        bull = max(bull, 0)
        bear = max(bear, 0)

        log.info(self.asset + " Metals: bull=" + str(bull) + " bear=" + str(bear))
        reason_str = " | ".join(reasons) if reasons else "No signals"

        if bull >= 4 and bull > bear:
            return min(bull, 8), "BUY", reason_str
        elif bear >= 4 and bear > bull:
            return min(bear, 8), "SELL", reason_str
        return max(bull, bear), "NONE", reason_str

    def _macro_check(self):
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

    # ══════════════════════════════════════════════
    # MATH HELPERS
    # ══════════════════════════════════════════════
    def _bollinger_bands(self, closes, period=20, std_dev=2):
        if len(closes) < period:
            avg = sum(closes) / len(closes)
            return avg, avg, avg, 0
        recent   = closes[-period:]
        middle   = sum(recent) / period
        variance = sum((x - middle) ** 2 for x in recent) / period
        std      = math.sqrt(variance)
        upper    = middle + std_dev * std
        lower    = middle - std_dev * std
        return upper, middle, lower, upper - lower

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
