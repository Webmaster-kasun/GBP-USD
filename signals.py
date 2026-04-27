"""
BETAX Supply & Demand Zone Signal Engine
=========================================
Reverse-engineered from 312 real trades (Jan 2025 – Apr 2026).

HOW THE TRADER ACTUALLY WORKS:
  1. Identifies Supply/Demand zones on Gold (key S/R levels)
  2. Places LIMIT orders inside a 3-4 pip zone window (never market entry)
  3. BUY:  entry zone = high-to-low (e.g. 3134→3130) — buying pullback into demand
  4. SELL: entry zone = low-to-high (e.g. 3100→3104) — selling push into supply
  5. SL placed just beyond zone invalidation
  6. Moves SL to breakeven after TP1 (~70-80 pips), lets remainder run to TP2
  7. Long-biased — follows Gold's trend. BUY WR 66.3% vs SELL WR 55.8%

SIGNAL SCORING (8 pts max):
  Check 1 — Zone Identification  (0-2 pts): Price at a key S/D zone level
  Check 2 — HTF Trend Alignment  (0-2 pts): H4 + D1 trend direction match
  Check 3 — Zone Rejection       (0-1 pt):  M15/M30 wick rejection at zone
  Check 4 — RSI confluence       (0-1 pt):  Momentum agrees with direction
  Check 5 — Structure break      (0-1 pt):  Previous swing high/low broken
  Check 6 — Not overextended     (0-1 pt):  Within 1000p of last zone

  Need 5/8 to trade — with zone rejection (Check 3) mandatory
  Direction: BUY heavily favoured in uptrend (matches BETAX trader's 66% BUY WR)

SL/TP MODEL (from 312-trade analysis):
  Early 2025:  SL ~35-50 pips  (Gold ~$2750)
  Mid 2025:    SL 70 pips      (standardized)
  Late 2025:   SL 90-100 pips  (Gold ~$4000+)
  2026:        SL 100 pips     (Gold ~$5000)
  TP1 = 1.5× SL (move SL to BE after TP1 hit)
  TP2 = 2.5× SL (let remainder run — avg win was 171 pips vs 66 pip SL)
"""

import os
import time
import requests
import logging

log = logging.getLogger(__name__)

CALL_DELAY = 0.5


class BetaxSignalEngine:
    def __init__(self, demo=True):
        self.api_key    = os.environ.get("OANDA_API_KEY", "")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        self.base_url   = "https://api-fxpractice.oanda.com" if demo else "https://api-trade.oanda.com"
        self.headers    = {"Authorization": "Bearer " + self.api_key}

    # ─────────────────────────────────────────────
    # Data fetching helpers
    # ─────────────────────────────────────────────

    def _fetch_candles(self, instrument, granularity, count=100):
        url    = self.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                time.sleep(CALL_DELAY)
                r = requests.get(url, headers=self.headers, params=params, timeout=10)
                if r.status_code == 200:
                    candles = r.json()["candles"]
                    c       = [x for x in candles if x["complete"]]
                    closes  = [float(x["mid"]["c"]) for x in c]
                    highs   = [float(x["mid"]["h"]) for x in c]
                    lows    = [float(x["mid"]["l"]) for x in c]
                    opens   = [float(x["mid"]["o"]) for x in c]
                    volumes = [int(x.get("volume", 0)) for x in c]
                    return closes, highs, lows, opens, volumes
                log.warning("Candle fetch attempt %d failed: %d", attempt + 1, r.status_code)
            except Exception as e:
                log.warning("Candle error: %s", e)
        return [], [], [], [], []

    def _get_live_price(self, instrument):
        try:
            url    = self.base_url + "/v3/accounts/" + self.account_id + "/pricing"
            params = {"instruments": instrument}
            time.sleep(CALL_DELAY)
            r = requests.get(url, headers=self.headers, params=params, timeout=10)
            if r.status_code == 200:
                prices = r.json().get("prices", [])
                if prices:
                    bid = float(prices[0]["bids"][0]["price"])
                    ask = float(prices[0]["asks"][0]["price"])
                    return round((bid + ask) / 2, 2)
        except Exception as e:
            log.warning("Live price error: %s", e)
        return None

    def _ema(self, data, period):
        if not data or len(data) < period:
            avg = sum(data) / len(data) if data else 0
            return [avg] * max(len(data), 1)
        seed = sum(data[:period]) / period
        emas = [seed] * period
        mult = 2 / (period + 1)
        for p in data[period:]:
            emas.append((p - emas[-1]) * mult + emas[-1])
        return emas

    def _calc_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return None
        deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains    = [d if d > 0 else 0 for d in deltas[-period:]]
        losses   = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)

    def _get_atr_pips(self, closes, highs, lows, period=14):
        if len(closes) < period + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return round(sum(trs[-period:]) / period / 0.01)

    # ─────────────────────────────────────────────
    # BETAX Core: Supply / Demand Zone Detection
    # ─────────────────────────────────────────────

    def _find_sd_zones(self, highs, lows, closes, lookback=50):
        """
        Identify significant S/D zones from recent swing highs/lows.
        Returns list of (price_level, zone_type) where zone_type is 'demand' or 'supply'.
        Logic mirrors BETAX trader: he picks round number clusters and swing pivots.
        """
        zones = []
        if len(highs) < 10:
            return zones

        lb = min(lookback, len(highs))
        recent_highs = highs[-lb:]
        recent_lows  = lows[-lb:]

        # Swing highs = local maxima (supply zones)
        for i in range(2, len(recent_highs) - 2):
            if (recent_highs[i] > recent_highs[i - 1] and
                    recent_highs[i] > recent_highs[i - 2] and
                    recent_highs[i] > recent_highs[i + 1] and
                    recent_highs[i] > recent_highs[i + 2]):
                zones.append((round(recent_highs[i], 2), "supply"))

        # Swing lows = local minima (demand zones)
        for i in range(2, len(recent_lows) - 2):
            if (recent_lows[i] < recent_lows[i - 1] and
                    recent_lows[i] < recent_lows[i - 2] and
                    recent_lows[i] < recent_lows[i + 1] and
                    recent_lows[i] < recent_lows[i + 2]):
                zones.append((round(recent_lows[i], 2), "demand"))

        # Round number clusters (BETAX heavily uses these — Gold likes 00/50 levels)
        last_close = closes[-1]
        for offset in range(-500, 501, 50):
            level = round(round(last_close / 50) * 50 + offset, 2)
            zones.append((level, "round"))

        return zones

    def _nearest_zone(self, price, zones, direction, pip=0.01):
        """
        Find the nearest relevant zone for the given direction.
        BUY → nearest demand zone below price (or round number)
        SELL → nearest supply zone above price (or round number)
        Returns (zone_price, zone_type, distance_pips) or (None, None, None)
        """
        best_zone  = None
        best_type  = None
        best_dist  = float("inf")

        for (level, ztype) in zones:
            dist = (price - level) / pip  # positive = level is below price

            if direction == "BUY":
                # Demand zone: price should be slightly above the zone (0 to 300 pips)
                if 0 <= dist <= 300:
                    if dist < best_dist:
                        best_dist = dist
                        best_zone = level
                        best_type = ztype
            else:  # SELL
                dist = -dist  # flip: now positive means level is above price
                if 0 <= dist <= 300:
                    if dist < best_dist:
                        best_dist = dist
                        best_zone = level
                        best_type = ztype

        return best_zone, best_type, best_dist if best_zone else None

    def _check_zone_rejection(self, direction, closes, highs, lows, opens, granularity_label="M15"):
        """
        Check if last candle shows a rejection wick AT a zone.
        BETAX always waits for a wick rejection before entering — this is his entry trigger.
        BUY:  lower wick >= 40% of candle range (price rejected lower)
        SELL: upper wick >= 40% of candle range (price rejected higher)
        """
        if not closes or len(closes) < 2:
            return False, granularity_label + " no data"

        h = highs[-1];  l = lows[-1]
        o = opens[-1];  c = closes[-1]
        total_range = h - l

        if total_range < 0.05:  # candle too small to read
            return False, granularity_label + " candle too small"

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        upper_pct  = upper_wick / total_range
        lower_pct  = lower_wick / total_range

        if direction == "SELL" and upper_pct >= 0.40:
            return True, granularity_label + " upper wick=" + str(round(upper_pct * 100)) + "% — supply rejection ✅"
        elif direction == "BUY" and lower_pct >= 0.40:
            return True, granularity_label + " lower wick=" + str(round(lower_pct * 100)) + "% — demand rejection ✅"
        else:
            side = "upper" if direction == "SELL" else "lower"
            pct  = round(upper_pct * 100) if direction == "SELL" else round(lower_pct * 100)
            return False, granularity_label + " " + side + " wick only " + str(pct) + "% — no rejection"

    def _check_structure_break(self, direction, closes, highs, lows, lookback=10):
        """
        Check if price has broken the most recent swing high (BUY) or swing low (SELL).
        BETAX enters on a zone retest after a structure break — confirms momentum.
        """
        if len(closes) < lookback + 2:
            return False, "Not enough data for structure check"

        recent_highs = highs[-(lookback + 1):-1]
        recent_lows  = lows[-(lookback + 1):-1]
        current      = closes[-1]

        if direction == "BUY":
            prev_swing_high = max(recent_highs)
            if current > prev_swing_high:
                return True, "Structure break: price " + str(round(current, 2)) + " > swing high " + str(round(prev_swing_high, 2)) + " ✅"
            return False, "No structure break above " + str(round(prev_swing_high, 2))
        else:
            prev_swing_low = min(recent_lows)
            if current < prev_swing_low:
                return True, "Structure break: price " + str(round(current, 2)) + " < swing low " + str(round(prev_swing_low, 2)) + " ✅"
            return False, "No structure break below " + str(round(prev_swing_low, 2))

    # ─────────────────────────────────────────────
    # Main Entry Point (same interface as original)
    # ─────────────────────────────────────────────

    def analyze(self, asset="XAUUSD"):
        is_asian = "ASIAN" in asset.upper()
        return self._analyze_betax(is_asian=is_asian)

    def _analyze_betax(self, is_asian=False):
        """
        BETAX-style Supply & Demand analysis.
        Returns (score, direction, details_string) — same interface as original SignalEngine.
        """
        reasons   = []
        score     = 0
        direction = "NONE"
        blocked   = False

        # ── Fetch candle data ─────────────────────────────────
        h4_closes, h4_highs, h4_lows, h4_opens, _ = self._fetch_candles("XAU_USD", "H4", 60)
        h1_closes, h1_highs, h1_lows, h1_opens, _ = self._fetch_candles("XAU_USD", "H1", 60)
        d1_closes, d1_highs, d1_lows, d1_opens, _ = self._fetch_candles("XAU_USD", "D",  10)
        m15_closes, m15_highs, m15_lows, m15_opens, _ = self._fetch_candles("XAU_USD", "M15", 20)
        m30_closes, m30_highs, m30_lows, m30_opens, _ = self._fetch_candles("XAU_USD", "M30", 20)

        if not h1_closes:
            return 0, "NONE", "No H1 price data"

        price = self._get_live_price("XAU_USD")
        if price is None:
            price = h1_closes[-1]
            log.warning("Using H1 close — live price unavailable")

        # ── ATR Gate — skip extreme volatility ───────────────
        atr_pips = self._get_atr_pips(h1_closes, h1_highs, h1_lows)
        if atr_pips is not None:
            min_atr = 200 if is_asian else 300
            if atr_pips < min_atr:
                return 0, "NONE", "ATR=" + str(atr_pips) + "p — too quiet for zone trading, skip"
            if atr_pips > 4000:
                return 0, "NONE", "ATR=" + str(atr_pips) + "p — extreme news volatility, skip"
            if atr_pips > 2000:
                reasons.append("⚠️ ATR=" + str(atr_pips) + "p — elevated (trade with caution)")
            else:
                reasons.append("✅ ATR=" + str(atr_pips) + "p — healthy zone volatility")
        else:
            reasons.append("⚠️ ATR unavailable — proceeding with caution")

        # ── HTF Context: D1 trend direction ──────────────────
        d1_direction = "NONE"
        if len(d1_closes) >= 5:
            d1_ema10 = self._ema(d1_closes, min(10, len(d1_closes)))[-1]
            d1_ema20 = self._ema(d1_closes, min(20, len(d1_closes)))[-1] if len(d1_closes) >= 20 else d1_ema10
            if d1_ema10 > d1_ema20:
                d1_direction = "BUY"
            elif d1_ema10 < d1_ema20:
                d1_direction = "SELL"
            log.info("D1 trend=%s EMA10=%.2f EMA20=%.2f", d1_direction, d1_ema10, d1_ema20)
        else:
            # Fallback: use slope of last 5 D1 closes
            if len(d1_closes) >= 3:
                d1_direction = "BUY" if d1_closes[-1] > d1_closes[-3] else "SELL"

        # ── H4 trend direction ────────────────────────────────
        h4_direction = "NONE"
        if len(h4_closes) >= 50:
            h4_ema20 = self._ema(h4_closes, 20)[-1]
            h4_ema50 = self._ema(h4_closes, 50)[-1]
            h4_direction = "BUY" if h4_ema20 > h4_ema50 else "SELL"
            log.info("H4 trend=%s EMA20=%.2f EMA50=%.2f", h4_direction, h4_ema20, h4_ema50)
        elif len(h4_closes) >= 5:
            h4_direction = "BUY" if h4_closes[-1] > h4_closes[-5] else "SELL"

        # ── Determine working direction from HTF bias ─────────
        # BETAX: strong long bias — default BUY in uptrend, need 2× confirmation to short
        if d1_direction == "BUY" and h4_direction == "BUY":
            working_direction = "BUY"
        elif d1_direction == "SELL" and h4_direction == "SELL":
            working_direction = "SELL"
        elif d1_direction == "BUY":
            # D1 bullish but H4 mixed — still prefer BUY (BETAX long bias)
            working_direction = "BUY"
        elif d1_direction == "SELL":
            working_direction = "SELL"
        else:
            return 0, "NONE", "No clear HTF direction — waiting for alignment"

        direction = working_direction

        # ── CHECK 1: ZONE IDENTIFICATION (0-2 pts) ────────────
        # Build S/D zones from H4 data (trader uses H4 charts primarily)
        h4_zones = self._find_sd_zones(h4_highs, h4_lows, h4_closes, lookback=50)
        nearest_zone, zone_type, zone_dist = self._nearest_zone(price, h4_zones, direction)

        if nearest_zone and zone_dist is not None:
            if zone_dist <= 50:
                score += 2
                reasons.append("✅✅ Price AT zone " + str(nearest_zone) + " (" + str(zone_type) + ") dist=" + str(round(zone_dist)) + "p (2 pts)")
            elif zone_dist <= 150:
                score += 1
                reasons.append("✅ Price NEAR zone " + str(nearest_zone) + " (" + str(zone_type) + ") dist=" + str(round(zone_dist)) + "p (1 pt)")
            else:
                reasons.append("❌ Nearest zone " + str(nearest_zone) + " too far " + str(round(zone_dist)) + "p (0 pts)")
                # No zone = no trade for BETAX strategy
                return 0, "NONE", " | ".join(reasons)
        else:
            reasons.append("❌ No clear S/D zone identified near price (0 pts)")
            return 0, "NONE", " | ".join(reasons)

        # ── CHECK 2: HTF TREND ALIGNMENT (0-2 pts) ────────────
        d1_ok = (d1_direction == direction)
        h4_ok = (h4_direction == direction)

        if d1_ok and h4_ok:
            score += 2
            reasons.append("✅✅ D1=" + d1_direction + " H4=" + h4_direction + " both align with " + direction + " (2 pts)")
        elif d1_ok or h4_ok:
            score += 1
            aligned   = "D1" if d1_ok else "H4"
            misaligned = "H4" if d1_ok else "D1"
            reasons.append("✅ " + aligned + "=" + direction + " aligns | " + misaligned + " against — partial (1 pt)")
            # Against BOTH = hard block in London/NY (matches BETAX H4 block logic)
            if not is_asian:
                reasons.append("⚠️ Partial HTF alignment — elevated caution")
        else:
            # Against both trends — BLOCKED for London/NY (matches FIX 8 from original)
            if not is_asian:
                blocked = True
                reasons.append("🚫 D1=" + d1_direction + " H4=" + h4_direction + " — both against " + direction + " (London/NY BLOCKED)")
            else:
                score = max(0, score - 1)
                reasons.append("⚠️ HTF against direction — Asian penalty -1pt (score=" + str(score) + ")")

        # ── CHECK 3: ZONE REJECTION — MANDATORY (0-1 pt) ─────
        # BETAX NEVER enters without a rejection wick. This is his core entry trigger.
        # Check M15 first, then M30 as fallback
        m15_rej, m15_reason = self._check_zone_rejection(direction, m15_closes, m15_highs, m15_lows, m15_opens, "M15")
        m30_rej, m30_reason = self._check_zone_rejection(direction, m30_closes, m30_highs, m30_lows, m30_opens, "M30")

        rejection_confirmed = m15_rej or m30_rej
        rejection_reason    = m15_reason if m15_rej else (m30_reason if m30_rej else m15_reason)

        if rejection_confirmed:
            score += 1
            reasons.append("✅ Zone rejection: " + rejection_reason + " (1 pt)")
        else:
            reasons.append("❌ No zone rejection — " + rejection_reason + " (0 pts)")
            # MANDATORY: no rejection = no trade (matches BETAX entry rule)
            if not blocked:
                reasons.append("🚫 MANDATORY: Zone rejection required — trade skipped")
                return 0, "NONE", " | ".join(reasons)

        # ── CHECK 4: RSI CONFLUENCE (0-1 pt) ─────────────────
        rsi_val = self._calc_rsi(h1_closes, 14)
        if rsi_val is not None:
            rsi_buy  = 45 if is_asian else 50   # BETAX buys pullbacks — RSI doesn't need to be high
            rsi_sell = 55 if is_asian else 50   # BETAX sells into strength — RSI needn't be extreme
            if direction == "BUY" and rsi_val > rsi_buy:
                score += 1
                reasons.append("✅ RSI=" + str(rsi_val) + " > " + str(rsi_buy) + " — momentum supports BUY (1 pt)")
            elif direction == "SELL" and rsi_val < rsi_sell:
                score += 1
                reasons.append("✅ RSI=" + str(rsi_val) + " < " + str(rsi_sell) + " — momentum supports SELL (1 pt)")
            else:
                reasons.append("❌ RSI=" + str(rsi_val) + " — momentum not ideal (0 pts)")
        else:
            reasons.append("⚠️ RSI unavailable (0 pts)")

        # ── CHECK 5: STRUCTURE BREAK (0-1 pt) ────────────────
        struct_ok, struct_reason = self._check_structure_break(direction, h1_closes, h1_highs, h1_lows, lookback=10)
        if struct_ok:
            score += 1
            reasons.append("✅ " + struct_reason + " (1 pt)")
        else:
            reasons.append("❌ " + struct_reason + " (0 pts)")

        # ── CHECK 6: NOT OVEREXTENDED FROM ZONE (0-1 pt) ─────
        # BETAX never chases — if price ran >1000p from zone, skip
        if zone_dist is not None:
            if zone_dist <= 200:
                score += 1
                reasons.append("✅ Zone dist=" + str(round(zone_dist)) + "p — fresh zone entry (1 pt)")
            else:
                reasons.append("❌ Zone dist=" + str(round(zone_dist)) + "p — may be chasing (0 pts)")

        final_direction = "BLOCKED" if blocked else direction
        log.info("BETAX Score=%d/8 direction=%s zone=%s", score, final_direction, str(nearest_zone))

        # Add zone info to reasons for Telegram alert
        reasons.append("Zone: " + str(nearest_zone) + " (" + str(zone_type) + ")")
        reasons.append("Entry window: " + _zone_entry_window(nearest_zone, direction))

        return score, final_direction, " | ".join(reasons)


def _zone_entry_window(zone_price, direction, spread_pips=4):
    """
    Format the entry zone window as the BETAX trader shows it.
    BUY:  zone_price to zone_price-spread  (e.g. 3134-3130)
    SELL: zone_price to zone_price+spread  (e.g. 3100-3104)
    """
    if zone_price is None:
        return "N/A"
    if direction == "BUY":
        return str(round(zone_price, 2)) + "–" + str(round(zone_price - spread_pips * 0.01, 2))
    else:
        return str(round(zone_price, 2)) + "–" + str(round(zone_price + spread_pips * 0.01, 2))


# Backwards-compatible alias so bot.py import works unchanged
SignalEngine = BetaxSignalEngine
