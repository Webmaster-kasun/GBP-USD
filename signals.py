"""
Signal Engine — M1 Ultra-Scalp
================================
Supports: AUD/USD, EUR/USD, GBP/USD, USD/JPY
Score 3/3 required:
  L1: M5 EMA8 vs EMA21 bias
  L2: M5 RSI(7) snap <40 BUY / >60 SELL
  L3: M1 trigger candle — engulf or pin-bar
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
        "AUDUSD": "AUD_USD",
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "USDJPY": "USD_JPY",
        "EURGBP": "EUR_GBP",
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
            # fallback: insert underscore at pos 3
            instr = asset[:3]+"_"+asset[3:]
        return self._scalp_m1(instr, asset)

    def _scalp_m1(self, instrument, asset):
        reasons = []
        bull = bear = 0

        # ── L1: M5 EMA8 vs EMA21 ─────────────────────────────────────
        m5_c, _, _, _ = self._fetch_candles(instrument, "M5", 60)
        if len(m5_c) < 22:
            return 0, "NONE", "Not enough M5 data ("+str(len(m5_c))+")"

        ema8  = self._ema(m5_c, 8)[-1]
        ema21 = self._ema(m5_c, 21)[-1]

        bull_bias = ema8 > ema21 * 1.00003
        bear_bias = ema8 < ema21 * 0.99997

        if bull_bias:
            bull += 1
            reasons.append("✅ EMA bullish")
        elif bear_bias:
            bear += 1
            reasons.append("✅ EMA bearish")
        else:
            return 0, "NONE", "EMA flat — no bias"

        # ── L2: M5 RSI(7) ────────────────────────────────────────────
        rsi = self._rsi(m5_c, 7)
        if bull_bias and rsi <= 40:
            bull += 1
            reasons.append("✅ RSI="+str(round(rsi,1))+" oversold")
        elif bear_bias and rsi >= 60:
            bear += 1
            reasons.append("✅ RSI="+str(round(rsi,1))+" overbought")
        else:
            reasons.append("RSI="+str(round(rsi,1))+" not in zone")
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

        if bull >= 3: return 3, "BUY",  " | ".join(reasons)
        if bear >= 3: return 3, "SELL", " | ".join(reasons)
        return max(bull,bear), "NONE", " | ".join(reasons)

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
