"""Signal engine for EMA crossover + ORB scalping — Cable AI Scalp v1.2

Dedicated to GBP/USD (Cable).
instrument is passed explicitly to analyze(); no pair is hard-coded.

Strategy: EMA crossover + Opening Range Breakout (ORB, time-decayed) + CPR Bias

Scoring (Bull — BUY):
  EMA cross  — fresh EMA9 crosses above EMA21 (last 2 candles): +3
               EMA9 already above EMA21 (aligned, no fresh cross): +1
  ORB        — price above ORB high, time-weighted:
                 0–orb_fresh_minutes:               +2 (fresh break)
                 orb_fresh_minutes–orb_aging_minutes: +1 (aging)
                 orb_aging_minutes+:                +0 (stale)
  CPR bias   — price above CPR pivot: +1

Scoring (Bear — SELL): mirror of above.

Max score: 6  |  Min threshold: signal_threshold (default 4)

All parameters read from settings (merged global + pair-specific).
Key per-pair settings used here: pair_sl_tp (sl_pips, tp_pips, pip_value_usd).

ORB cache key includes instrument so each pair has its own ORB per session.

v2.3: GBP/USD only. pip_value_usd is static $10.00 (standard for USD-quoted pairs).
GBP/USD uses static $10.00/pip (standard for USD-quoted pairs).
"""

import time
import logging
from datetime import datetime as _dt
import pytz as _pytz
from config_loader import load_secrets, load_settings, DATA_DIR
from state_utils import load_json, save_json
from oanda_trader import make_oanda_session

log = logging.getLogger(__name__)

_CPR_CACHE_FILE = DATA_DIR / "cpr_cache.json"
_ORB_CACHE_FILE = DATA_DIR / "orb_cache.json"
_SGT = _pytz.timezone("Asia/Singapore")
_UTC = _pytz.utc

# Module-level fallbacks — only used when no settings dict is passed (unit tests).
MIN_TRADE_SCORE   = 4
EMA_FAST          = 9
EMA_SLOW          = 21
SCALP_SL_PCT      = 0.0020
SCALP_TP_PCT      = 0.0050

ORB_FRESH_MINUTES = 60
ORB_AGING_MINUTES = 120

# Default ORB open hours — used as fallbacks when settings are not available (unit tests).
# Configurable via settings.json: london_session_start_hour, us_session_start_hour,
# tokyo_session_start_hour.
_DEFAULT_ORB_HOURS = {
    "London":  (16, 0),
    "US":      (21, 0),
    "US_Cont": (0, 0),
    "Tokyo":   ( 8, 0),
}

def _build_orb_sessions(settings: dict | None = None) -> dict:
    """Return ORB session open-hour map derived from settings.
    US excluded when us_session_start_hour >= 99 (disabled sentinel).
    """
    s = settings or {}
    us_h  = int(s.get("us_session_start_hour", 21))
    us_e2 = int(s.get("us_session_early_end_hour", 3))
    sessions = {
        "London": (int(s.get("london_session_start_hour", 16)), 0),
        "Tokyo":  (int(s.get("tokyo_session_start_hour",   8)), 0),
    }
    if us_h < 99:
        sessions["US"] = (us_h, 0)
    if us_e2 < 99:
        sessions["US_Cont"] = (0, 0)
    return sessions


def score_to_position_usd(score: int, settings: dict | None = None) -> int:
    """Return the risk-dollar position size for a given score."""
    full    = int((settings or {}).get("position_full_usd",    48))
    partial = int((settings or {}).get("position_partial_usd", 30))
    for threshold, size in [(4, full), (2, partial)]:
        if score > threshold:
            return size
    return 0


def _validate_cpr_levels(levels: dict) -> tuple:
    required = {"pivot", "tc", "bc", "r1", "r2", "s1", "s2", "pdh", "pdl", "cpr_width_pct"}
    missing = required - set(levels.keys())
    if missing:
        return False, "missing keys: {}".format(missing)
    pivot = levels["pivot"]; tc = levels["tc"]; bc = levels["bc"]
    r1 = levels["r1"]; r2 = levels["r2"]; s1 = levels["s1"]
    s2 = levels["s2"]; pdh = levels["pdh"]; pdl = levels["pdl"]
    cpr_w = levels["cpr_width_pct"]
    if not (tc > bc):             return False, "TC must be > BC"
    if not (r1 > pivot):          return False, "R1 must be > pivot"
    if not (pivot > s1):          return False, "pivot must be > S1"
    if not (r2 > r1):             return False, "R2 must be > R1"
    if not (s2 < s1):             return False, "S2 must be < S1"
    if not (pdh > pdl):           return False, "PDH must be > PDL"
    if not (pdl <= pivot <= pdh): return False, "pivot must be between PDL and PDH"
    if not (cpr_w > 0):           return False, "cpr_width_pct must be > 0"
    return True, ""


def _price_dp(pip_size: float) -> int:
    """Decimal places for price formatting based on pip size."""
    if pip_size <= 0.0001:
        return 5   # GBP_USD (e.g. 1.27345) — Cable
    if pip_size <= 0.01:
        return 3   # JPY pairs (not used in Cable AI Scalp v1.2)
    return 2       # fallback


class SignalEngine:
    def __init__(self, demo: bool = True):
        secrets = load_secrets()
        self.api_key    = secrets.get("OANDA_API_KEY",    "")
        self.account_id = secrets.get("OANDA_ACCOUNT_ID", "")
        self.base_url   = (
            "https://api-fxpractice.oanda.com" if demo else "https://api-fxtrade.oanda.com"
        )
        self.headers = {
            "Authorization": "Bearer {}".format(self.api_key),
            "Content-Type":  "application/json",
        }
        self.session = make_oanda_session(allowed_methods=["GET"])

    def analyze(self, instrument: str = "GBP_USD", settings: dict | None = None):
        """Run the Cable AI Scalp EMA + ORB (time-decayed) + CPR-bias scoring engine.

        Args:
            instrument: OANDA instrument code (GBP_USD for Cable AI Scalp v1.2)
            settings:   merged (global + pair-specific) settings dict

        Returns:
            (score, direction, details, levels, position_usd)
        """
        if settings is None:
            settings = load_settings()

        _pip_size = float((settings or {}).get("pip_size", 0.0001) or 0.0001)
        _dp       = _price_dp(_pip_size)

        # -- 1. CPR levels (bias filter only) ---------------------------------
        levels, pivot, tc, bc, cpr_width_pct = self._get_cpr_levels(instrument, _dp)
        if levels is None:
            return 0, "NONE", "Could not fetch CPR levels", {}, 0

        # -- 2. M5 candles for EMA + price ------------------------------------
        _ema_fast   = int((settings or {}).get("ema_fast_period",  EMA_FAST))
        _ema_slow   = int((settings or {}).get("ema_slow_period",  EMA_SLOW))
        _atr_period = int((settings or {}).get("atr_period",       14))
        _m5_count   = int((settings or {}).get("m5_candle_count",  40))

        m5_closes, m5_highs, m5_lows = self._fetch_candles(instrument, "M5", _m5_count)
        if len(m5_closes) < _ema_slow + 3:
            return 0, "NONE", "Not enough M5 data (need {} candles)".format(_ema_slow + 3), levels, 0

        current_close = m5_closes[-1]
        atr_val = self._atr(m5_highs, m5_lows, m5_closes, _atr_period)
        levels["atr"]           = round(atr_val, _dp) if atr_val else None
        levels["current_price"] = round(current_close, _dp)
        levels["pip_size"]      = _pip_size

        # -- 3. EMA on M5 -----------------------------------------------------
        ema_fast_series = self._ema_series(m5_closes[:-1], _ema_fast)
        ema_slow_series = self._ema_series(m5_closes[:-1], _ema_slow)

        if len(ema_fast_series) < 2 or len(ema_slow_series) < 2:
            return 0, "NONE", "Not enough EMA data", levels, 0

        ema_fast_now  = ema_fast_series[-1]
        ema_slow_now  = ema_slow_series[-1]
        ema_fast_prev = ema_fast_series[-2]
        ema_slow_prev = ema_slow_series[-2]

        levels["ema{}".format(_ema_fast)] = round(ema_fast_now, _dp)
        levels["ema{}".format(_ema_slow)] = round(ema_slow_now, _dp)

        # -- 4. ORB -----------------------------------------------------------
        now_sgt      = _dt.now(_SGT)
        orb_sessions = _build_orb_sessions(settings)
        session_name = self._get_active_session(now_sgt, settings)
        # ORB cache is keyed per-instrument so each pair has its own ORB
        orb_high, orb_low, orb_formed = self._get_orb(
            session_name, instrument, now_sgt, _dp, orb_sessions,
            orb_form_min=int((settings or {}).get("orb_formation_minutes", 15)))

        _orb_age_min = 0
        if orb_formed and session_name in orb_sessions:
            import datetime as _dt_mod
            oh, om = orb_sessions[session_name]
            open_sgt = now_sgt.replace(hour=oh, minute=om, second=0, microsecond=0)
            if now_sgt.hour == 0 and session_name == "US":
                open_sgt = open_sgt - _dt_mod.timedelta(days=1)
            _orb_age_min = max(0, int((now_sgt - open_sgt).total_seconds() / 60))

        levels["orb_high"]    = round(orb_high, _dp) if orb_high else None
        levels["orb_low"]     = round(orb_low,  _dp) if orb_low  else None
        levels["orb_age_min"] = _orb_age_min
        levels["orb_formed"]  = orb_formed
        levels["session"]     = session_name

        # -- 5. Scoring -------------------------------------------------------
        fmt = "{{:.{}f}}".format(_dp)  # e.g. "{:.5f}" or "{:.3f}"
        score     = 0
        direction = "NONE"
        setup     = "No Setup"
        reasons   = []

        reasons.append(
            ("{instr} | EMA{ef}=" + fmt + " EMA{es}=" + fmt +
             " | Price=" + fmt + " | CPR pivot=" + fmt + " | CPR width={:.3f}%").format(
                ema_fast_now, ema_slow_now, current_close, pivot, cpr_width_pct,
                instr=instrument, ef=_ema_fast, es=_ema_slow,
            )
        )

        # 5a. EMA crossover ---------------------------------------------------
        fresh_bull = (ema_fast_now > ema_slow_now) and (ema_fast_prev <= ema_slow_prev)
        fresh_bear = (ema_fast_now < ema_slow_now) and (ema_fast_prev >= ema_slow_prev)
        bull_align = ema_fast_now > ema_slow_now
        bear_align = ema_fast_now < ema_slow_now

        cross_tmpl = ("EMA{ef} {verb} EMA{es} | prev(" + fmt + "/" + fmt +
                      ") -> now(" + fmt + "/" + fmt + ") (+{pts})")

        if fresh_bull:
            direction = "BUY";  score += 3;  setup = "EMA Fresh Cross Up"
            reasons.append(cross_tmpl.format(
                ema_fast_prev, ema_slow_prev, ema_fast_now, ema_slow_now,
                ef=_ema_fast, es=_ema_slow, verb="fresh cross ABOVE", pts=3,
            ))
        elif fresh_bear:
            direction = "SELL"; score += 3;  setup = "EMA Fresh Cross Down"
            reasons.append(cross_tmpl.format(
                ema_fast_prev, ema_slow_prev, ema_fast_now, ema_slow_now,
                ef=_ema_fast, es=_ema_slow, verb="fresh cross BELOW", pts=3,
            ))
        elif bull_align:
            direction = "BUY";  score += 1;  setup = "EMA Trend Up"
            reasons.append(("EMA{ef}=" + fmt + " above EMA{es}=" + fmt +
                             " | aligned bull, no fresh cross (+1)").format(
                ema_fast_now, ema_slow_now, ef=_ema_fast, es=_ema_slow))
        elif bear_align:
            direction = "SELL"; score += 1;  setup = "EMA Trend Down"
            reasons.append(("EMA{ef}=" + fmt + " below EMA{es}=" + fmt +
                             " | aligned bear, no fresh cross (+1)").format(
                ema_fast_now, ema_slow_now, ef=_ema_fast, es=_ema_slow))
        else:
            reasons.append("No EMA bias (+0)")
            return 0, "NONE", " | ".join(reasons), levels, 0

        # 5b. ORB confirmation (time-decayed) ----------------------------------
        _orb_fresh_min = int((settings or {}).get("orb_fresh_minutes", ORB_FRESH_MINUTES))
        _orb_aging_min = int((settings or {}).get("orb_aging_minutes", ORB_AGING_MINUTES))

        if orb_formed and orb_high and orb_low:
            if _orb_age_min < _orb_fresh_min:
                _orb_pts   = 2
                _orb_label = "fresh (<{}min)".format(_orb_fresh_min)
            elif _orb_age_min < _orb_aging_min:
                _orb_pts   = 1
                _orb_label = "aging ({}-{}min)".format(_orb_fresh_min, _orb_aging_min)
            else:
                _orb_pts   = 0
                _orb_label = "stale (>{}min)".format(_orb_aging_min)

            if direction == "BUY" and current_close > orb_high:
                score += _orb_pts
                reasons.append(("Price " + fmt + " > ORB high " + fmt +
                                 " | bullish ORB break (+{}) [{}]").format(
                    current_close, orb_high, _orb_pts, _orb_label))
            elif direction == "SELL" and current_close < orb_low:
                score += _orb_pts
                reasons.append(("Price " + fmt + " < ORB low " + fmt +
                                 " | bearish ORB break (+{}) [{}]").format(
                    current_close, orb_low, _orb_pts, _orb_label))
            else:
                reasons.append(("Price " + fmt + " inside ORB [" + fmt + "-" + fmt +
                                 "] | no break (+0)").format(current_close, orb_low, orb_high))
        else:
            reasons.append("ORB not yet formed for {} session (+0)".format(session_name or "N/A"))

        # 5c. CPR bias --------------------------------------------------------
        if direction == "BUY" and current_close > pivot:
            score += 1
            reasons.append(("Price " + fmt + " above CPR pivot " + fmt +
                             " | bullish bias (+1)").format(current_close, pivot))
        elif direction == "SELL" and current_close < pivot:
            score += 1
            reasons.append(("Price " + fmt + " below CPR pivot " + fmt +
                             " | bearish bias (+1)").format(current_close, pivot))
        else:
            reasons.append(("CPR bias against direction (pivot=" + fmt + ") (+0)").format(pivot))

        # 5d. Exhaustion penalty ----------------------------------------------
        _orb_contributed = orb_formed and (
            (direction == "BUY"  and orb_high and current_close > orb_high) or
            (direction == "SELL" and orb_low  and current_close < orb_low)
        )
        _exhaust_mult = float((settings or {}).get("exhaustion_atr_mult", 3.0))
        if _exhaust_mult > 0 and atr_val and atr_val > 0 and not _orb_contributed:
            ema_mid  = (ema_fast_now + ema_slow_now) / 2
            _stretch = abs(current_close - ema_mid) / atr_val
            if _stretch > _exhaust_mult:
                score = max(score - 1, 0)
                reasons.append(
                    "Exhaustion: stretch={:.2f}x ATR (>{:.1f}x) | score -1 -> {}/6".format(
                        _stretch, _exhaust_mult, score))
            else:
                reasons.append("Stretch {:.2f}x ATR (ok, no exhaustion penalty)".format(_stretch))
        elif _orb_contributed:
            reasons.append("Exhaustion check skipped — ORB breakout in progress")

        # -- 6. Position size -------------------------------------------------
        position_usd = score_to_position_usd(score, settings)

        # -- 7. Scalp SL/TP ---------------------------------------------------
        # Fixed pip SL/TP uses two clearly named values:
        #   1) sl_price_dist / tp_price_dist = price distance for order placement
        #      e.g. GBP_USD sl_pips=20, pip_size=0.0001 -> sl_price_dist=0.0020
        #   2) sl_risk_per_unit_usd / tp_reward_per_unit_usd = USD value per unit for sizing/R:R
        #      units = position_usd / sl_risk_per_unit_usd -> target USD risk for USD-quoted pairs.
        # pair_sl_tp fixed pips always used; percentage fallback unreachable.
        entry = current_close

        _pair_sl_tp = (settings or {}).get("pair_sl_tp", {})
        _pair_cfg   = _pair_sl_tp.get(instrument, {})

        if _pair_cfg.get("sl_pips") and _pair_cfg.get("tp_pips"):
            # Fixed pip mode -- pair-specific SL and TP
            # pip_value_usd: dollar value of 1 pip for 1 standard lot (100k units).
            # GBP/USD = $10.00 (standard for USD-quoted pairs).
            # sl_risk_per_unit_usd = sl_pips * (pip_value_usd / 100_000)
            # This gives units = position_usd / sl_risk_per_unit_usd -> target USD risk for GBP/USD.
            _sl_pips_fixed  = int(_pair_cfg["sl_pips"])
            _tp_pips_fixed  = int(_pair_cfg["tp_pips"])
            # pip_value_usd: static $10.00 for GBP/USD
            _pip_val_usd    = self._get_pip_value_usd(instrument, current_close, _pair_cfg)
            _pip_usd_unit   = _pip_val_usd / 100_000   # $ per unit per pip (for sizing)
            # USD risk/reward per unit — used for position sizing and R:R calculation
            sl_risk_per_unit_usd     = round(_sl_pips_fixed * _pip_usd_unit, _dp + 2)
            tp_reward_per_unit_usd   = round(_tp_pips_fixed * _pip_usd_unit, _dp + 2)
            # Price distances use pip_size * sl_pips
            levels["sl_price_dist"] = round(_sl_pips_fixed * _pip_size, _dp + 2)
            levels["tp_price_dist"] = round(_tp_pips_fixed * _pip_size, _dp + 2)
            sl_source   = "fixed_pips"
            tp_source   = "fixed_pips"

        rr_ratio = (tp_reward_per_unit_usd / sl_risk_per_unit_usd) if sl_risk_per_unit_usd > 0 else 0
        _min_rr  = float((settings or {}).get("min_rr_ratio", 1.6))
        rr_skip  = rr_ratio < _min_rr
        blockers = []
        if rr_skip:
            blockers.append("R:R {:.2f} < 1:{:.1f}".format(rr_ratio, _min_rr))

        # Pips for display only
        sl_pips = _sl_pips_fixed
        tp_pips = _tp_pips_fixed

        # -- 8. Levels dict ---------------------------------------------------
        # -- H1 trend filter ------------------------------------------------
        _h1_enabled = bool((settings or {}).get("h1_filter_enabled", True))
        _h1_period  = int((settings or {}).get("h1_ema_period", 21))
        if _h1_enabled:
            _h1 = self._get_h1_trend(instrument, _h1_period, _dp)
        else:
            _h1 = {"h1_trend": "DISABLED", "h1_ema_now": None, "h1_price": None}

        # H1 alignment: BUY needs BULLISH, SELL needs BEARISH
        _h1_aligned = (
            (_h1["h1_trend"] == "BULLISH" and direction == "BUY") or
            (_h1["h1_trend"] == "BEARISH" and direction == "SELL") or
            _h1["h1_trend"] in ("UNKNOWN", "DISABLED", "FLAT")
        )

        levels["score"]        = score
        levels["position_usd"] = position_usd
        levels["entry"]        = round(entry, _dp)
        levels["setup"]        = setup
        levels["sl_risk_per_unit_usd"] = sl_risk_per_unit_usd
        levels["sl_source"]    = sl_source
        levels["sl_pips"]      = sl_pips
        levels["tp_reward_per_unit_usd"] = tp_reward_per_unit_usd
        levels["tp_source"]    = tp_source
        levels["tp_pips"]      = tp_pips
        levels["rr_ratio"]     = round(rr_ratio, 2)
        _min_score = int((settings or {}).get("signal_threshold", MIN_TRADE_SCORE))
        levels["mandatory_checks"] = {"score_ok": score >= _min_score, "rr_ok": not rr_skip}
        levels["quality_checks"]   = {"tp_ok": True}
        levels["signal_blockers"]  = blockers
        levels["h1_trend"]         = _h1["h1_trend"]
        levels["h1_ema_now"]       = _h1["h1_ema_now"]
        levels["h1_aligned"]       = _h1_aligned

        _tp_label = "{:.1f}x RR".format(rr_ratio)
        sl_fmt = "{{:.{}f}}".format(_dp + 2)
        reasons.append(
            ("SL=" + sl_fmt + " ({src} {pips}pip) | TP=" + sl_fmt +
             " ({tsrc} {tlbl}, {tpips}pip) | R:R 1:{rr:.1f}").format(
                levels["sl_price_dist"], levels["tp_price_dist"],
                src=sl_source, pips=sl_pips,
                tsrc=tp_source, tlbl=_tp_label, tpips=tp_pips, rr=rr_ratio,
            )
        )
        if blockers:
            reasons.append("BLOCKED: " + " | ".join(blockers))

        details = " | ".join(reasons)
        _min_score = int((settings or {}).get("signal_threshold", MIN_TRADE_SCORE))
        if blockers:
            log.info("Signal BLOCKED | %s setup=%s dir=%s score=%s/6 blockers=%s",
                     instrument, setup, direction, score, "; ".join(blockers))
        elif score < _min_score:
            # Sub-threshold: no trade will fire — label clearly to avoid confusion
            log.info("Signal | %s setup=%s dir=%s score=%s/6 below_threshold (would_be=$%s)",
                     instrument, setup, direction, score, position_usd if position_usd else 0)
        else:
            log.info("Signal | %s setup=%s dir=%s score=%s/6 position=$%s",
                     instrument, setup, direction, score, position_usd)

        return score, direction, details, levels, position_usd

    # -- CPR helper -----------------------------------------------------------

    def _get_cpr_levels(self, instrument: str, dp: int = 5):
        closes, highs, lows = self._fetch_candles(instrument, "D", 3)
        if len(closes) < 2:
            return None, None, None, None, None

        ph = highs[-2]; pl = lows[-2]; pc = closes[-2]
        pivot = (ph + pl + pc) / 3
        bc    = (ph + pl) / 2
        tc    = (pivot - bc) + pivot
        dr    = ph - pl

        if tc < bc:
            tc, bc = bc, tc
            log.debug("CPR TC/BC swapped — bearish prior-day close (%s)", instrument)

        lv = {
            "pivot":         round(pivot, dp),
            "tc":            round(tc, dp),
            "bc":            round(bc, dp),
            "r1":            round((2 * pivot) - pl, dp),
            "r2":            round(pivot + dr, dp),
            "s1":            round((2 * pivot) - ph, dp),
            "s2":            round(pivot - dr, dp),
            "pdh":           round(ph, dp),
            "pdl":           round(pl, dp),
            "cpr_width_pct": round(abs(tc - bc) / pivot * 100, 3),
        }

        ok, reason = _validate_cpr_levels(lv)
        if not ok:
            log.warning("CPR validation failed — skipping | %s | %s", instrument, reason)
            return None, None, None, None, None

        log.info("CPR fetched | %s pivot=%.*f TC=%.*f BC=%.*f width=%.3f%%",
                 instrument, dp, pivot, dp, tc, dp, bc, lv["cpr_width_pct"])
        return lv, lv["pivot"], lv["tc"], lv["bc"], lv["cpr_width_pct"]

    # -- ORB helper -----------------------------------------------------------

    def _get_active_session(self, now_sgt: _dt, settings: dict | None = None):
        orb_sessions = _build_orb_sessions(settings)
        s      = settings or {}
        lon_h  = orb_sessions["London"][0]
        tok_h  = orb_sessions["Tokyo"][0]
        lon_e  = int(s.get("london_session_end_hour",    20))
        tok_e  = int(s.get("tokyo_session_end_hour",     15))
        h = now_sgt.hour
        if lon_h <= h <= lon_e: return "London"
        # US checks only when session is enabled (start_hour < 99)
        us_h  = int(s.get("us_session_start_hour",      21))
        us_e  = int(s.get("us_session_end_hour",        23))
        us_e2 = int(s.get("us_session_early_end_hour",   3))
        if us_h  < 99 and us_h <= h <= us_e:  return "US"        # late: 21–23
        if us_e2 < 99 and 0    <= h <= us_e2: return "US_Cont"   # continuation: 00–03
        if tok_h <= h <= tok_e: return "Tokyo"
        return None

    def _get_orb(self, session_name, instrument: str, now_sgt: _dt,
                 dp: int = 5, orb_sessions: dict | None = None,
                 orb_form_min: int = 15):
        """Return (orb_high, orb_low, formed) for the current session ORB.

        Cache key: {instrument}_{session_open_date}_{session_name}
        Each pair maintains its own ORB independently.
        """
        _orb_sess = orb_sessions if orb_sessions is not None else _DEFAULT_ORB_HOURS
        if session_name not in _orb_sess:
            return None, None, False

        import datetime as _dt_mod
        open_h, open_m = _orb_sess[session_name]
        open_sgt = now_sgt.replace(hour=open_h, minute=open_m, second=0, microsecond=0)

        if now_sgt.hour == 0 and session_name == "US":
            open_sgt = open_sgt - _dt_mod.timedelta(days=1)

        session_date_str = open_sgt.strftime("%Y-%m-%d")
        # Include instrument in cache key for forward compatibility
        cache_key = "{}_{}_{}" .format(instrument, session_date_str, session_name)
        orb_cache = load_json(_ORB_CACHE_FILE, {})

        if cache_key in orb_cache and orb_cache[cache_key].get("formed"):
            c = orb_cache[cache_key]
            return c["high"], c["low"], True

        minutes_since_open = (now_sgt - open_sgt).total_seconds() / 60

        if minutes_since_open < orb_form_min:
            log.debug("ORB not yet formed | %s %s (%.0f min, need %d)",
                      instrument, session_name, minutes_since_open, orb_form_min)
            return None, None, False

        open_utc = open_sgt.astimezone(_UTC)
        closes, highs, lows, times = self._fetch_candles_with_time(instrument, "M15", 12)

        for i, t in enumerate(times):
            try:
                candle_dt = _dt.fromisoformat(t.replace("Z", "+00:00")).replace(tzinfo=_UTC)
            except Exception:
                continue
            if candle_dt >= open_utc:
                orb_cache[cache_key] = {
                    "high":        round(highs[i], dp),
                    "low":         round(lows[i],  dp),
                    "formed":      True,
                    "candle_time": t,
                }
                save_json(_ORB_CACHE_FILE, orb_cache)
                log.info("ORB formed | %s %s high=%.*f low=%.*f candle=%s",
                         instrument, session_name, dp, highs[i], dp, lows[i], t)
                return highs[i], lows[i], True

        return None, None, False

    # -- Pip value helper (v1.0 GBP/USD only) --------------------------------

    def _get_pip_value_usd(self, instrument: str, current_close: float,
                           pair_cfg: dict) -> float:
        """Return pip_value_usd. GBP/USD = static $10.00 per pip per 100k units.

        JPY dynamic logic removed in v2.3 — Cable AI Scalp is GBP/USD only.
        """
        return float(pair_cfg.get("pip_value_usd", 10.0))

    # -- EMA helper -----------------------------------------------------------

    def _get_h1_trend(self, instrument: str, ema_period: int = 21, dp: int = 5) -> dict:
        """Fetch last 40 H1 candles and compute EMA trend direction.

        Returns dict with keys:
          h1_trend   : 'BULLISH' | 'BEARISH' | 'FLAT' | 'UNKNOWN'
          h1_ema_now : float | None
          h1_price   : float | None
        """
        try:
            closes, _, _ = self._fetch_candles(instrument, "H1", 40)
            if len(closes) < ema_period + 2:
                return {"h1_trend": "UNKNOWN", "h1_ema_now": None, "h1_price": None}

            ema_series = self._ema_series(closes[:-1], ema_period)
            if len(ema_series) < 2:
                return {"h1_trend": "UNKNOWN", "h1_ema_now": None, "h1_price": None}

            ema_now   = ema_series[-1]
            price_now = closes[-1]

            if price_now > ema_now:
                trend = "BULLISH"
            elif price_now < ema_now:
                trend = "BEARISH"
            else:
                trend = "FLAT"

            return {
                "h1_trend":   trend,
                "h1_ema_now": round(ema_now,   dp),
                "h1_price":   round(price_now, dp),
            }
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning("H1 trend fetch failed: %s", exc)
            return {"h1_trend": "UNKNOWN", "h1_ema_now": None, "h1_price": None}

    def _ema_series(self, closes: list, period: int) -> list:
        if len(closes) < period:
            return []
        k   = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        series = [ema]
        for price in closes[period:]:
            ema = price * k + ema * (1 - k)
            series.append(ema)
        return series

    # -- Data helpers ---------------------------------------------------------

    def _fetch_candles(self, instrument: str, granularity: str, count: int = 60):
        url    = "{}/v3/instruments/{}/candles".format(self.base_url, instrument)
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code == 200:
                    candles  = r.json().get("candles", [])
                    complete = [c for c in candles if c.get("complete")]
                    return (
                        [float(c["mid"]["c"]) for c in complete],
                        [float(c["mid"]["h"]) for c in complete],
                        [float(c["mid"]["l"]) for c in complete],
                    )
                log.warning("Fetch candles %s %s: HTTP %s", instrument, granularity, r.status_code)
            except Exception as e:
                log.warning("Fetch candles error (%s %s) attempt %s: %s",
                            instrument, granularity, attempt + 1, e)
            time.sleep(1)
        return [], [], []

    def _fetch_candles_with_time(self, instrument: str, granularity: str, count: int = 12):
        url    = "{}/v3/instruments/{}/candles".format(self.base_url, instrument)
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code == 200:
                    candles  = r.json().get("candles", [])
                    complete = [c for c in candles if c.get("complete")]
                    return (
                        [float(c["mid"]["c"]) for c in complete],
                        [float(c["mid"]["h"]) for c in complete],
                        [float(c["mid"]["l"]) for c in complete],
                        [c["time"] for c in complete],
                    )
                log.warning("Fetch candles+time %s %s: HTTP %s", instrument, granularity, r.status_code)
            except Exception as e:
                log.warning("Fetch candles+time error (%s %s) attempt %s: %s",
                            instrument, granularity, attempt + 1, e)
            time.sleep(1)
        return [], [], [], []

    def _atr(self, highs: list, lows: list, closes: list, period: int = 14):
        n = len(closes)
        if n < period + 2 or len(highs) < n or len(lows) < n:
            return None
        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            for i in range(1, n)
        ]
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr
