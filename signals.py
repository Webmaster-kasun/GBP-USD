"""
Signal Engine — GBP/USD Two-Session Scalp
==========================================
Pair: GBP/USD ONLY
Target: 26 pip TP | 13 pip SL | 2:1 R:R

3-Layer Signal Logic (optimized for 26 pip moves):
  L0: H1 EMA50 — overall trend direction gate
  L1: M15 structure break — last 6 candles high/low breakout (real momentum)
  L2: M5 pullback to EMA21 + strong bounce candle (body > 55% of range)
  VETO: H1 EMA200 hard block — no BUY below, no SELL above

Why this works for 26 pips:
  - H1 trend ensures we trade WITH momentum not against it
  - M15 structure break = real move starting not noise
  - M5 EMA21 pullback = better entry price more room to reach 26 pip TP
  - No RSI (too slow/rare for GBP) No MACD (lags too much for scalp)
"""

import os, requests, logging

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
        self.api_key  = os.environ.get("OANDA_API_KEY", "")
        self.base_url = "https://api-fxpractice.oanda.com"
        self.headers  = {"Authorization": "Bearer " + self.api_key}

    def _fetch_candles(self, instrument, granularity, count=60):
        url    = self.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": str(count), "granularity": granularity, "price": "M"}
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
                log.warning("Candle " + granularity + " attempt " + str(attempt+1) + " HTTP " + str(r.status_code))
            except Exception as e:
                log.warning("Candle fetch error: " + str(e))
        return [], [], [], []

    def _ema(self, data, period):
        if not data:
            return [0.0]
        if len(data) < period:
            return [sum(data) / len(data)] * len(data)
        seed = sum(data[:period]) / period
        emas = [seed] * period
        mult = 2 / (period + 1)
        for p in data[period:]:
            emas.append((p - emas[-1]) * mult + emas[-1])
        return emas

    def analyze(self, asset="GBPUSD"):
        return self._scalp_gbpusd("GBP_USD")

    def _scalp_gbpusd(self, instrument):
        reasons   = []
        score     = 0

        # ── L0: H1 EMA50 — overall trend direction ───────────────────
        h1_c, _, _, _ = self._fetch_candles(instrument, "H1", 60)
        if len(h1_c) < 51:
            return 0, "NONE", "Not enough H1 data (" + str(len(h1_c)) + ")"

        h1_ema50 = self._ema(h1_c, 50)[-1]
        h1_price = h1_c[-1]

        if h1_price > h1_ema50:
            direction = "BUY"
            reasons.append("✅ L0 H1 BUY above EMA50=" + str(round(h1_ema50, 5)))
        elif h1_price < h1_ema50:
            direction = "SELL"
            reasons.append("✅ L0 H1 SELL below EMA50=" + str(round(h1_ema50, 5)))
        else:
            return 0, "NONE", "H1 EMA50 flat — no trend"

        score = 1

        # ── L1: M15 structure break — last 6 candle high/low ─────────
        m15_c, m15_h, m15_l, m15_o = self._fetch_candles(instrument, "M15", 20)
        if len(m15_c) < 8:
            return score, "NONE", " | ".join(reasons) + " | Not enough M15 data"

        lookback       = 6
        recent_highs   = m15_h[-lookback-1:-1]
        recent_lows    = m15_l[-lookback-1:-1]
        structure_high = max(recent_highs)
        structure_low  = min(recent_lows)
        last_close     = m15_c[-1]

        # FIX-1: cap how late the entry is — if price already moved 3+ pips past
        # the structure level the move is over, skip it (prevents chasing)
        bull_break = (last_close > structure_high) and (last_close <= structure_high + 0.00080)
        bear_break = (last_close < structure_low)  and (last_close >= structure_low  - 0.00080)

        if direction == "BUY" and bull_break:
            reasons.append(
                "✅ L1 M15 break UP close=" + str(round(last_close, 5)) +
                " > high=" + str(round(structure_high, 5))
            )
            score = 2
        elif direction == "SELL" and bear_break:
            reasons.append(
                "✅ L1 M15 break DOWN close=" + str(round(last_close, 5)) +
                " < low=" + str(round(structure_low, 5))
            )
            score = 2
        else:
            reasons.append(
                "L1 no M15 break — high=" + str(round(structure_high, 5)) +
                " low=" + str(round(structure_low, 5)) +
                " close=" + str(round(last_close, 5))
            )
            return score, "NONE", " | ".join(reasons)

        # ── L2: M5 pullback to EMA21 + strong bounce candle ──────────
        m5_c, m5_h, m5_l, m5_o = self._fetch_candles(instrument, "M5", 40)
        if len(m5_c) < 22:
            return score, "NONE", " | ".join(reasons) + " | Not enough M5 data"

        ema21   = self._ema(m5_c, 21)[-1]
        c_close = m5_c[-1]
        c_open  = m5_o[-1]
        c_high  = m5_h[-1]
        c_low   = m5_l[-1]
        c_range = max(c_high - c_low, 0.00001)

        # FIX-3: minimum candle range 3 pips — filters weak micro candles
        MIN_CANDLE_RANGE = 0.00030

        # Strong bounce candle body > 55% of range + must be a real candle
        bull_body = (c_close > c_open) and ((c_close - c_low) / c_range >= 0.55) and (c_range >= MIN_CANDLE_RANGE)
        bear_body = (c_close < c_open) and ((c_high - c_close) / c_range >= 0.55) and (c_range >= MIN_CANDLE_RANGE)

        # FIX-2: wider pullback window — 1.5 pip tolerance, last 5 candles (L1 break and pullback rarely on same candle)
        ema_tol         = 0.00015
        recent_lows_m5  = m5_l[-6:-1]
        recent_highs_m5 = m5_h[-6:-1]
        bull_pullback   = any(l <= ema21 + ema_tol for l in recent_lows_m5)
        bear_pullback   = any(h >= ema21 - ema_tol for h in recent_highs_m5)

        if direction == "BUY" and bull_pullback and bull_body:
            reasons.append(
                "✅ L2 M5 pullback EMA21=" + str(round(ema21, 5)) +
                " bounce body=" + str(round((c_close - c_low) / c_range * 100)) + "%"
            )
            score = 3
        elif direction == "SELL" and bear_pullback and bear_body:
            reasons.append(
                "✅ L2 M5 pullback EMA21=" + str(round(ema21, 5)) +
                " bounce body=" + str(round((c_high - c_close) / c_range * 100)) + "%"
            )
            score = 3
        else:
            reasons.append(
                "L2 fail — EMA21=" + str(round(ema21, 5)) +
                " bull_pb=" + str(bull_pullback) +
                " bear_pb=" + str(bear_pullback) +
                " bull_body=" + str(bull_body) +
                " bear_body=" + str(bear_body)
            )
            return score, "NONE", " | ".join(reasons)

        # ── VETO: H1 EMA200 hard block ────────────────────────────────
        h1_long_c, _, _, _ = self._fetch_candles(instrument, "H1", 210)
        if len(h1_long_c) >= 200:
            h1_ema200 = self._ema(h1_long_c, 200)[-1]
            price_now = m5_c[-1]
            ema200_buf = 0.00050  # 5 pip buffer — avoid blocking trades right at EMA200
            if direction == "BUY" and price_now < h1_ema200 - ema200_buf:
                reasons.append("🚫 VETO H1 EMA200=" + str(round(h1_ema200, 5)) + " price well below — no BUY")
                return score, "NONE", " | ".join(reasons)
            elif direction == "SELL" and price_now > h1_ema200 + ema200_buf:
                reasons.append("🚫 VETO H1 EMA200=" + str(round(h1_ema200, 5)) + " price well above — no SELL")
                return score, "NONE", " | ".join(reasons)
            else:
                reasons.append("✅ VETO pass EMA200=" + str(round(h1_ema200, 5)))
        else:
            log.warning("Not enough H1 for EMA200 (" + str(len(h1_long_c)) + ") — veto skipped")
            reasons.append("⚠️ EMA200 unavailable — veto skipped")

        return score, direction, " | ".join(reasons)
