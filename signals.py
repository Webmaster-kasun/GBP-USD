"""
signals.py — Multi-Pair Signal Engine (v3.2)
============================================

Three strategies:

1. TRIPLE EMA MOMENTUM  (GBP/USD)
   - EMA5/EMA10/EMA20 aligned on H1
   - Gap filter: skip if open >50 pips from prior close (news/Monday gaps)
   - ATR gate >= 5 pips on M15

2. FOUR-LAYER ENGINE  (EUR/USD)
   - L0: H4 EMA50 macro trend
   - L1: H1 dual EMA alignment
   - L2: M15 impulse candle break (saved to state)
   - L3: M5 EMA13 pullback + RSI7
   - V1: H1 EMA200 veto
   - V2: M30 counter-trend veto

3. AUD/USD ASIAN RANGE + LONDON BREAKOUT
   - Asian range built 08:00-13:00 SGT
   - Liquidity sweep detection
   - London breakout entry 15:00-17:00 SGT
   - H4 trend filter
"""

import os
import requests
import logging
from datetime import datetime, timezone
import pytz
from config import FOUR_LAYER, TRIPLE_EMA, AUD_RANGE

log   = logging.getLogger(__name__)
sg_tz = pytz.timezone("Asia/Singapore")
UTC   = pytz.utc


# ── Shared helpers ────────────────────────────────────────────────────────────

def _fetch(instrument, granularity, count=60):
    key  = os.environ.get("OANDA_API_KEY", "")
    url  = f"https://api-fxpractice.oanda.com/v3/instruments/{instrument}/candles"
    hdrs = {"Authorization": f"Bearer {key}"}
    prm  = {"count": str(count), "granularity": granularity, "price": "M"}
    for _ in range(3):
        try:
            r = requests.get(url, headers=hdrs, params=prm, timeout=10)
            if r.status_code == 200:
                c = [x for x in r.json()["candles"] if x["complete"]]
                return ([float(x["mid"]["c"]) for x in c],
                        [float(x["mid"]["h"]) for x in c],
                        [float(x["mid"]["l"]) for x in c],
                        [float(x["mid"]["o"]) for x in c])
        except Exception as e:
            log.warning("Candle fetch: %s", e)
    return [], [], [], []


def _fetch_timed(instrument, granularity, count=60):
    key  = os.environ.get("OANDA_API_KEY", "")
    url  = f"https://api-fxpractice.oanda.com/v3/instruments/{instrument}/candles"
    hdrs = {"Authorization": f"Bearer {key}"}
    prm  = {"count": str(count), "granularity": granularity, "price": "M"}
    for _ in range(3):
        try:
            r = requests.get(url, headers=hdrs, params=prm, timeout=10)
            if r.status_code == 200:
                bars = []
                for x in r.json()["candles"]:
                    if x["complete"]:
                        ts = datetime.fromisoformat(
                            x["time"].replace("Z", "+00:00")).replace(tzinfo=UTC)
                        bars.append((ts, float(x["mid"]["h"]),
                                     float(x["mid"]["l"]), float(x["mid"]["c"]),
                                     float(x["mid"]["o"])))
                return bars
        except Exception as e:
            log.warning("Candle fetch: %s", e)
    return []


def _ema(data, period):
    if not data: return [0.0]
    if len(data) < period: return [sum(data)/len(data)]*len(data)
    e = sum(data[:period])/period
    m = 2/(period+1)
    out = [e]*period
    for p in data[period:]:
        e = p*m + e*(1-m)
        out.append(e)
    return out


def _rsi(closes, period=7):
    if len(closes) < period+1: return 50.0
    g,l = [],[]
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        g.append(max(d,0)); l.append(max(-d,0))
    ag = sum(g[-period:])/period
    al = sum(l[-period:])/period
    return 100.0 if al==0 else 100-(100/(1+ag/al))


def _atr(highs, lows, closes, period=14):
    if len(highs) < period+1: return 0.0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1,len(highs))]
    return sum(trs[-period:])/period


# ── Strategy 1: TRIPLE EMA (GBP/USD) ─────────────────────────────────────────

def triple_ema_signal(instrument: str, max_gap_pips: float = 50.0) -> tuple:
    """Returns (direction, reason). direction: 'BUY' | 'SELL' | None"""
    PIP = 0.0001

    h1_c, h1_h, h1_l, h1_o = _fetch(instrument, "H1", 50)
    if len(h1_c) < 25:
        return None, "Not enough H1 data"

    # Gap filter
    if len(h1_c) >= 2:
        gap = abs(h1_o[-1] - h1_c[-2]) / PIP
        if gap > max_gap_pips:
            return None, f"Gap filter — {gap:.0f}p gap > {max_gap_pips:.0f}p (news/Monday gap)"

    ema5  = _ema(h1_c, 5)[-1]
    ema10 = _ema(h1_c, 10)[-1]
    ema20 = _ema(h1_c, 20)[-1]

    if ema5 < ema10 < ema20:   direction = "SELL"
    elif ema5 > ema10 > ema20: direction = "BUY"
    else: return None, f"EMAs mixed (EMA5={ema5:.5f} EMA10={ema10:.5f} EMA20={ema20:.5f})"

    m15_c, m15_h, m15_l, _ = _fetch(instrument, "M15", 30)
    if len(m15_c) < 15:
        return None, "Not enough M15 data"

    atr_p = _atr(m15_h, m15_l, m15_c, 14) / PIP
    if atr_p < TRIPLE_EMA["min_atr_pips"]:
        return None, f"ATR too low ({atr_p:.1f}p)"

    return direction, (f"Triple EMA {direction} | EMA5={ema5:.5f} "
                       f"EMA10={ema10:.5f} EMA20={ema20:.5f} | ATR={atr_p:.1f}p")


# ── Strategy 2: FOUR-LAYER ENGINE (EUR/USD) ───────────────────────────────────

def four_layer_signal(instrument: str, state: dict) -> tuple:
    """Returns (score, direction, details, layer_breakdown)"""
    cfg = FOUR_LAYER
    PIP = 0.0001
    reasons = []
    score   = 0

    # L2 pending check
    if state is not None:
        pending = state.get("l2_pending_" + instrument, {})
        if pending:
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(pending["timestamp"])).total_seconds()/60
            if age <= cfg["l2_expiry_minutes"]:
                return _l3(instrument, pending["direction"], 3,
                           ["(L0+L1+L2 confirmed)"], state, cfg)
            else:
                state.pop("l2_pending_" + instrument, None)

    # L0: H4 EMA50
    h4_c, h4_h, h4_l, _ = _fetch(instrument, "H4", 60)
    if len(h4_c) < 51:
        return 0, "NONE", "Not enough H4", {"L0": "⚠️"}
    h4_ema50 = _ema(h4_c, 50)[-1]
    direction = "BUY" if h4_c[-1] > h4_ema50 else "SELL" if h4_c[-1] < h4_ema50 else None
    if not direction:
        return 0, "NONE", "H4 EMA50 flat", {"L0": "❌"}
    reasons.append(f"✅ L0 H4 {direction}")
    score = 1

    # ATR check
    h1_c, h1_h, h1_l, _ = _fetch(instrument, "H1", 60)
    if len(h1_c) < 20:
        return score, "NONE", " | ".join(reasons), {"L0": "✅", "ATR": "⚠️"}
    atr_p = _atr(h1_h, h1_l, h1_c, 14) / PIP
    if atr_p < cfg["min_atr_pips"]:
        return score, "NONE", " | ".join(reasons), {"L0": "✅", "ATR": f"❌ {atr_p:.1f}p"}
    reasons.append(f"✅ ATR={atr_p:.1f}p")

    # L1: H1 dual EMA
    e21 = _ema(h1_c, 21)[-1]
    e50 = _ema(h1_c, 50)[-1]
    bull = h1_c[-1] > e21 > e50
    bear = h1_c[-1] < e21 < e50
    if (direction=="BUY" and bull) or (direction=="SELL" and bear):
        reasons.append("✅ L1 H1 aligned")
        score = 2
    else:
        return score, "NONE", " | ".join(reasons) + " | L1 fail", {"L0":"✅","ATR":"✅","L1":"❌"}

    # L2: M15 impulse break
    m15_c, m15_h, m15_l, m15_o = _fetch(instrument, "M15", 20)
    if len(m15_c) < 8:
        return score, "NONE", " | ".join(reasons), {"L0":"✅","ATR":"✅","L1":"✅","L2":"⚠️"}

    s_hi = max(m15_h[-6:-1]); s_lo = min(m15_l[-6:-1])
    lc,lo,lh,ll = m15_c[-1],m15_o[-1],m15_h[-1],m15_l[-1]
    rng = max(lh-ll, 0.00001)
    bull_body = (lc>lo) and ((lc-ll)/rng>=0.50)
    bear_body = (lc<lo) and ((lh-lc)/rng>=0.50)
    buf = cfg["l2_break_buffer"]
    bull_brk = (lc>s_hi) and (lc<=s_hi+buf) and bull_body
    bear_brk = (lc<s_lo) and (lc>=s_lo-buf) and bear_body

    if (direction=="BUY" and bull_brk) or (direction=="SELL" and bear_brk):
        reasons.append("✅ L2 M15 break")
        score = 3
        if state is not None:
            state["l2_pending_"+instrument] = {
                "direction": direction,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        return score, "NONE", " | ".join(reasons) + " — awaiting L3", {
            "L0":"✅","ATR":"✅","L1":"✅","L2":"✅ fired","L3":"⏳"}
    else:
        return score, "NONE", " | ".join(reasons) + " | L2 no break", {
            "L0":"✅","ATR":"✅","L1":"✅","L2":"❌"}

    return _l3(instrument, direction, score, reasons, state, cfg)


def _l3(instrument, direction, score, reasons, state, cfg):
    PIP = 0.0001
    m5_c, m5_h, m5_l, m5_o = _fetch(instrument, "M5", 50)
    if len(m5_c) < 15:
        return score, "NONE", " | ".join(reasons), {"L3":"⚠️"}

    e13  = _ema(m5_c, 13)[-1]
    rsi7 = _rsi(m5_c, 7)
    lc,lo,lh,ll = m5_c[-1],m5_o[-1],m5_h[-1],m5_l[-1]
    rng  = max(lh-ll, 0.00001)
    tol  = cfg["ema_tol"]
    bull_body = (lc>lo) and ((lc-ll)/rng>=0.50) and (rng>=cfg["min_m5_range"])
    bear_body = (lc<lo) and ((lh-lc)/rng>=0.50) and (rng>=cfg["min_m5_range"])
    bull_pb   = any(l<=e13+tol for l in m5_l[-3:-1])
    bear_pb   = any(h>=e13-tol for h in m5_h[-3:-1])

    if (direction=="BUY"  and bull_pb and bull_body and rsi7<cfg["rsi_buy_max"]) or \
       (direction=="SELL" and bear_pb and bear_body and rsi7>cfg["rsi_sell_min"]):
        reasons.append(f"✅ L3 M5 EMA13={e13:.5f} RSI7={rsi7:.1f}")
        score = 4
    else:
        return score, "NONE", " | ".join(reasons) + " | L3 fail", {
            "L0":"✅","ATR":"✅","L1":"✅","L2":"✅","L3":"❌"}

    # V1: EMA200 veto
    h1l, _, _, _ = _fetch(instrument, "H1", 210)
    if len(h1l) >= 200:
        e200 = _ema(h1l, 200)[-1]
        if (direction=="BUY" and m5_c[-1]<e200) or (direction=="SELL" and m5_c[-1]>e200):
            return score, "NONE", " | ".join(reasons) + " | V1 veto", {
                "L0":"✅","L1":"✅","L2":"✅","L3":"✅","V1":"❌ EMA200"}
        reasons.append("✅ V1 EMA200 ok")

    # V2: M30 counter-trend veto
    m30_c, m30_h, m30_l, m30_o = _fetch(instrument, "M30", 10)
    if len(m30_c) >= 4:
        ct = 0
        for i in range(-3,0):
            r2 = max(m30_h[i]-m30_l[i], 0.00001)
            if direction=="BUY"  and (m30_c[i]<m30_o[i]) and ((m30_h[i]-m30_c[i])/r2>=0.65): ct+=1
            if direction=="SELL" and (m30_c[i]>m30_o[i]) and ((m30_c[i]-m30_l[i])/r2>=0.65): ct+=1
        if ct >= 3:
            return score, "NONE", " | ".join(reasons) + " | V2 veto", {
                "L0":"✅","L1":"✅","L2":"✅","L3":"✅","V1":"✅","V2":"❌ M30"}
        reasons.append(f"✅ V2 M30 ok ({ct}/3)")

    if state is not None:
        state.pop("l2_pending_"+instrument, None)

    return score, direction, " | ".join(reasons), {
        "L0":f"✅ H4 {direction}", "ATR":"✅", "L1":"✅ H1",
        "L2":"✅ M15", "L3":f"✅ M5 RSI={rsi7:.0f}",
        "V1":"✅", "V2":"✅"}


# ── Strategy 3: AUD/USD ASIAN RANGE + LONDON BREAKOUT ────────────────────────

def audusd_signal(instrument: str, state: dict) -> tuple:
    """Returns (direction, reason, layer_breakdown)"""
    cfg    = AUD_RANGE
    PIP    = 0.0001
    now_sg = datetime.now(sg_tz)
    sg_h   = now_sg.hour + now_sg.minute/60

    if not (cfg["breakout_start_sgt"] <= sg_h < cfg["breakout_end_sgt"]):
        return None, f"Outside breakout window ({sg_h:.1f}h SGT)", {}

    # H4 trend
    h4_c, _, _, _ = _fetch(instrument, "H4", 60)
    if len(h4_c) < 51:
        return None, "Not enough H4 data", {}
    h4_trend = "BUY" if h4_c[-1] > _ema(h4_c,50)[-1] else "SELL"

    # ATR
    m15_c, m15_h, m15_l, _ = _fetch(instrument, "M15", 100)
    if len(m15_c) < 30:
        return None, "Not enough M15 data", {}
    atr_p = _atr(m15_h, m15_l, m15_c, 14) / PIP
    if atr_p < cfg["min_atr_pips"]:
        return None, f"ATR {atr_p:.1f}p < {cfg['min_atr_pips']}p", {}

    # Asian range
    asian_key  = f"AUD_USD_asian_{now_sg.strftime('%Y%m%d')}"
    asian_data = state.get(asian_key)

    if asian_data is None:
        bars = _fetch_timed(instrument, "M15", 80)
        highs, lows = [], []
        for ts, bh, bl, bc, bo in bars:
            sg = ts.astimezone(sg_tz)
            if sg.strftime("%Y%m%d")==now_sg.strftime("%Y%m%d") and 8<=sg.hour+sg.minute/60<13:
                highs.append(bh); lows.append(bl)
        if len(highs) < 8:
            return None, "Asian range building (need 8+ M15 bars 08:00-13:00 SGT)", {}
        ah = max(highs); al = min(lows)
        ar = (ah-al)/PIP
        state[asian_key] = {"high":ah,"low":al,"range":ar}
        asian_data = state[asian_key]

    ah = asian_data["high"]; al = asian_data["low"]; ar = asian_data["range"]

    if ar >= cfg["max_asian_range_pips"]:
        return None, f"Asian range {ar:.1f}p >= {cfg['max_asian_range_pips']}p — too wide", {}

    # Sweep detection
    swept_low = swept_high = False
    sd_low = sd_high = 0.0
    bars = _fetch_timed(instrument, "M15", 50)
    for ts, bh, bl, bc, bo in bars:
        sg = ts.astimezone(sg_tz)
        if sg.strftime("%Y%m%d")==now_sg.strftime("%Y%m%d") and sg.hour+sg.minute/60>=13:
            if bl < al: swept_low  = True; sd_low  = max(sd_low,  (al-bl)/PIP)
            if bh > ah: swept_high = True; sd_high = max(sd_high, (bh-ah)/PIP)

    cur = m15_c[-1]
    direction = None
    sweep_info = ""

    if swept_low and sd_low>=cfg["min_sweep_pips"] and cur>ah:
        if h4_trend=="BUY": direction="BUY"; sweep_info=f"Swept low {sd_low:.1f}p"
        else: return None, f"BUY blocked — H4={h4_trend}", {}
    elif swept_high and sd_high>=cfg["min_sweep_pips"] and cur<al:
        if h4_trend=="SELL": direction="SELL"; sweep_info=f"Swept high {sd_high:.1f}p"
        else: return None, f"SELL blocked — H4={h4_trend}", {}
    else:
        status = f"Low swept {sd_low:.1f}p" if swept_low else ("High swept {sd_high:.1f}p" if swept_high else "No sweep yet")
        return None, f"No breakout after sweep — {status}", {
            "Asian Range":f"✅ {ar:.1f}p","Sweep":f"⏳ {status}"}

    return direction, (f"AUD/USD Asian Range {direction} | AR={ar:.1f}p | {sweep_info} | H4={h4_trend}"), {
        "Trend":f"✅ H4 {h4_trend}", "ATR":f"✅ {atr_p:.1f}p",
        "Asian Range":f"✅ {ar:.1f}p", "Sweep":f"✅ {sweep_info}",
        "Breakout":f"✅ {direction}"}
