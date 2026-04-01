"""
Signal Engine — M1 Ultra-Scalp
================================
Supports: EUR/USD, GBP/USD
Score 4/4 required:
  L0: M15 EMA8 vs EMA21 — direction must match M15 momentum (no counter-trend scalps)
  L1: M5  EMA8 vs EMA21 bias
  L2: M5  RSI(9) <=30 BUY / >=70 SELL + strong momentum (delta>=1.0) + price vs EMA50
  L3: M1  trigger candle — engulf or pin-bar
  L4: H1  EMA200 hard block — price must be above EMA200 for BUY, below for SELL
       (L4 is a veto/block, not a score point — direction="NONE" if violated)
"""

import os, requests, logging

log = logging.getLogger(__name__)

class SafeFilter(logging.Filter):
    def __init__(self):
        self.api_key = os.environ.get("OANDA_API_KEY","")
    def filter(self, record):
        if self.api_key and self.api_key in str(record.getMessage()):
            record.msg = record.msg.replace(self.api_key,"***")
        return True
log.addFilter(SafeFilter())

class SignalEngine:
    def __init__(self):
        self.api_key    = os.environ.get("OANDA_API_KEY","")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID","")
        self.base_url   = "https://api-fxpractice.oanda.com"
        self.headers    = {"Authorization":"Bearer "+self.api_key}

    INSTR_MAP = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
    }

    def _fetch_candles(self, instrument, granularity, count=60):
        url    = self.base_url+"/v3/instruments/"+instrument+"/candles"
        params = {"count":str(count),"granularity":granularity,"price":"M"}
        for attempt in range(3):
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=10)
                if r.status_code == 200:
                    c = [x for x in r.json()["candles"] if x["complete"]]
                    return (
                        [float(x["mid"]["c"]) for x in c],
                        [float(x["mid"]["h"]) for x in c],
                        [float(x["mid"]["l"]) for x in c],
                        [float(x["mid"]["o"]) for x in c],
                    )
                log.warning("Candle "+granularity+" attempt "+str(attempt+1)+" failed: "+str(r.status_code))
            except Exception as e:
                log.warning("Candle error: "+str(e))
        return [], [], [], []

    def analyze(self, asset="EURUSD"):
        instr = self.INSTR_MAP.get(asset)
        if not instr:
            instr = asset[:3]+"_"+asset[3:]
        return self._scalp_m1(instr, asset)

    def _scalp_m1(self, instrument, asset):
        reasons = []
        bull = bear = 0

        # ── L0: M15 EMA8 vs EMA21 — momentum direction gate ──────────
        # If M15 trend disagrees with M5, skip — no counter-trend scalps
        m15_c, _, _, _ = self._fetch_candles(instrument, "M15", 40)
        if len(m15_c) < 22:
            return 0, "NONE", "Not enough M15 data ("+str(len(m15_c))+")"

        m15_ema8  = self._ema(m15_c, 8)[-1]
        m15_ema21 = self._ema(m15_c, 21)[-1]

        m15_bull = m15_ema8 > m15_ema21 * 1.00002
        m15_bear = m15_ema8 < m15_ema21 * 0.99998

        if m15_bull:
            bull += 1
            reasons.append("✅ M15 bullish")
        elif m15_bear:
            bear += 1
            reasons.append("✅ M15 bearish")
        else:
            return 0, "NONE", "M15 EMA flat — no momentum"

        # ── L1: M5 EMA8 vs EMA21 ─────────────────────────────────────
        m5_c, _, _, _ = self._fetch_candles(instrument, "M5", 60)
        if len(m5_c) < 22:
            return 0, "NONE", "Not enough M5 data ("+str(len(m5_c))+")"

        ema8  = self._ema(m5_c, 8)[-1]
        ema21 = self._ema(m5_c, 21)[-1]

        bull_bias = ema8 > ema21 * 1.00003
        bear_bias = ema8 < ema21 * 0.99997

        # L0 and L1 must agree — if M15 is bull but M5 is bear, skip
        if m15_bull and bull_bias:
            bull += 1
            reasons.append("✅ M5 EMA bullish")
        elif m15_bear and bear_bias:
            bear += 1
            reasons.append("✅ M5 EMA bearish")
        else:
            reasons.append("M5 EMA disagrees with M15 — skip")
            return max(bull,bear), "NONE", " | ".join(reasons)

        # ── L2: M5 RSI(9) — tight zones + strong momentum + EMA50 ──
        # BUY:  RSI(9) <= 30 AND rising strongly (delta >= 1.0 pt)
        #       AND price above M5 EMA50 (not buying into deep downtrend)
        #       AND bullish M5 candle (close > open on last bar)
        # SELL: RSI(9) >= 70 AND falling strongly (delta >= 1.0 pt)
        #       AND price below M5 EMA50
        #       AND bearish M5 candle (close < open on last bar)
        RSI_MOMENTUM_THRESHOLD = 1.0

        rsi_vals    = self._rsi_series(m5_c, 9)
        rsi         = rsi_vals[-1]
        rsi_prev    = rsi_vals[-2] if len(rsi_vals) >= 2 else rsi
        rsi_delta   = rsi - rsi_prev          # positive = rising, negative = falling

        ema50       = self._ema(m5_c, 50)[-1]
        price_now   = m5_c[-1]
        last_open   = m5_c[-2] if len(m5_c) >= 2 else m5_c[-1]  # previous close as proxy
        # Bullish/bearish confirmation from last completed M5 candle
        # m5_c[-1] is last complete close, m5_c[-2] is prior close
        m5_bull_candle = m5_c[-1] > m5_c[-2]
        m5_bear_candle = m5_c[-1] < m5_c[-2]

        price_above_ema50 = price_now > ema50
        price_below_ema50 = price_now < ema50

        if (bull_bias
                and rsi <= 30
                and rsi_delta >= RSI_MOMENTUM_THRESHOLD
                and price_above_ema50
                and m5_bull_candle):
            bull += 1
            reasons.append("✅ RSI="+str(round(rsi,1))+" oversold+Δ"+str(round(rsi_delta,1))+" above EMA50 bull-candle")
        elif (bear_bias
                and rsi >= 70
                and rsi_delta <= -RSI_MOMENTUM_THRESHOLD
                and price_below_ema50
                and m5_bear_candle):
            bear += 1
            reasons.append("✅ RSI="+str(round(rsi,1))+" overbought+Δ"+str(round(rsi_delta,1))+" below EMA50 bear-candle")
        else:
            reasons.append(
                "RSI="+str(round(rsi,1))
                +" Δ="+str(round(rsi_delta,2))
                +" EMA50="+str(round(ema50,5))
                +" price="+str(round(price_now,5))
                +" — L2 fail"
            )
            return max(bull,bear), "NONE", " | ".join(reasons)

        # ── L3: M1 trigger candle ─────────────────────────────────────
        m1_c, m1_h, m1_l, m1_o = self._fetch_candles(instrument, "M1", 10)
        if len(m1_c) < 3:
            return max(bull,bear), "NONE", " | ".join(reasons)+" | Not enough M1 data"

        c1 = m1_c[-1]; c2 = m1_c[-2]
        o1 = m1_o[-1]; o2 = m1_o[-2]
        h1 = m1_h[-1]; l1 = m1_l[-1]

        body1 = abs(c1 - o1)
        rng1  = max(h1 - l1, 0.00001)

        bull_engulf = (c1 > o1) and (c2 < o2) and (c1 >= o2) and (o1 <= c2)
        lower_wick  = min(o1,c1) - l1
        bull_pin    = (c1 >= o1) and (lower_wick/rng1 > 0.55) and (body1/rng1 < 0.40)

        bear_engulf = (c1 < o1) and (c2 > o2) and (c1 <= o2) and (o1 >= c2)
        upper_wick  = h1 - max(o1,c1)
        bear_pin    = (c1 <= o1) and (upper_wick/rng1 > 0.55) and (body1/rng1 < 0.40)

        if bull_bias and (bull_engulf or bull_pin):
            bull += 1
            reasons.append("✅ M1 bullish "+("engulf" if bull_engulf else "pin-bar"))
        elif bear_bias and (bear_engulf or bear_pin):
            bear += 1
            reasons.append("✅ M1 bearish "+("engulf" if bear_engulf else "pin-bar"))
        else:
            reasons.append("No M1 trigger")
            return max(bull,bear), "NONE", " | ".join(reasons)

        # Score 4/4 reached — now apply L4 H1 EMA200 hard block
        if bull >= 4:
            raw_dir = "BUY"
        elif bear >= 4:
            raw_dir = "SELL"
        else:
            return max(bull,bear), "NONE", " | ".join(reasons)

        # ── L4: H1 EMA200 hard block ─────────────────────────────────
        # Fetch 210 H1 candles to calculate EMA200
        h1_c, _, _, _ = self._fetch_candles(instrument, "H1", 210)
        if len(h1_c) < 200:
            # Not enough H1 data — allow trade but log warning
            log.warning(instrument+": Not enough H1 data for EMA200 ("+str(len(h1_c))+") — skipping H1 filter")
            reasons.append("⚠️ H1 EMA200 unavailable")
            return max(bull,bear), raw_dir, " | ".join(reasons)

        h1_ema200   = self._ema(h1_c, 200)[-1]
        current_price = m5_c[-1]

        if raw_dir == "BUY" and current_price < h1_ema200:
            reasons.append("🚫 H1 EMA200 block: price below EMA200 — no BUY")
            return max(bull,bear), "NONE", " | ".join(reasons)
        elif raw_dir == "SELL" and current_price > h1_ema200:
            reasons.append("🚫 H1 EMA200 block: price above EMA200 — no SELL")
            return max(bull,bear), "NONE", " | ".join(reasons)
        else:
            reasons.append("✅ H1 EMA200="+str(round(h1_ema200,5))+" confirms "+raw_dir)

        return max(bull,bear), raw_dir, " | ".join(reasons)

    def _rsi_series(self, closes, period=9):
        """Returns list of RSI values (same length as closes) for momentum check."""
        if len(closes) < period + 2:
            return [50.0, 50.0]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        # Seed with simple average
        ag = sum(gains[:period]) / period
        al = sum(losses[:period]) / period
        rsi_list = []
        for i in range(period, len(gains)+1):
            if i == period:
                pass  # already seeded above
            else:
                ag = (ag * (period-1) + gains[i-1]) / period
                al = (al * (period-1) + losses[i-1]) / period
            rsi_list.append(100 - (100 / (1 + ag/al)) if al != 0 else 100.0)
        return rsi_list if len(rsi_list) >= 2 else [50.0, 50.0]

    def _rsi(self, closes, period=14):
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i]-closes[i-1]
            gains.append(max(d,0)); losses.append(max(-d,0))
        if len(gains) < period: return 50
        ag = sum(gains[-period:])/period
        al = sum(losses[-period:])/period
        if al == 0: return 100
        return 100-(100/(1+ag/al))

    def _ema(self, data, period):
        if not data: return [0.0]
        if len(data) < period: return [sum(data)/len(data)]*len(data)
        seed = sum(data[:period])/period
        emas = [seed]*period
        mult = 2/(period+1)
        for p in data[period:]:
            emas.append((p-emas[-1])*mult+emas[-1])
        return emas
