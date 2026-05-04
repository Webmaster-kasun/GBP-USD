"""
bot.py — Multi-Pair Trading Bot (v3.2)
=======================================

Pairs:
  GBP/USD  Triple EMA Momentum    SL=15p TP=25p  London 15:00-19:00 SGT
  EUR/USD  4-Layer Signal Engine  SL=15p TP=25p  London 15:00-19:00 + NY 20:00-00:00 SGT
  AUD/USD  Asian Range Breakout   SL=15p TP=25p  Asia 08:00-13:00 + London 15:00-17:00 SGT

Fixes vs previous versions:
  FIX-01: utc_hour NameError removed (was crashing every run)
  FIX-02: SL=15, TP=25 for all pairs (was mixed 13/20/26)
  FIX-03: Gap filter added for GBP/USD (skip >50 pip gaps)
  FIX-04: AUD/USD uses audusd_range strategy (was triple_ema)
  FIX-05: SGD balance in all alerts (no USD)
  FIX-06: Scan result sent to Telegram every run (shows bot is alive)
"""

import logging
import requests
from datetime import datetime, timezone
import pytz

from config         import PAIRS, RISK, FOUR_LAYER
from signals        import triple_ema_signal, four_layer_signal, audusd_signal
from oanda_trader   import OandaTrader
from telegram_alert import TelegramAlert
from calendar_filter import EconomicCalendar

log   = logging.getLogger(__name__)
sg_tz = pytz.timezone("Asia/Singapore")


def _active_session(sg_hour: int, sessions: list) -> dict | None:
    for s in sessions:
        if s["start"] <= sg_hour < s["end"]:
            return s
    return None


def _in_cooldown(state: dict, pair: str) -> bool:
    ts = state.get("cooldowns", {}).get(pair)
    if not ts: return False
    try:
        elapsed = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(ts)).total_seconds() / 60
        return elapsed < 30
    except Exception:
        return False


def _set_cooldown(state: dict, pair: str):
    state.setdefault("cooldowns", {})[pair] = datetime.now(timezone.utc).isoformat()


def _detect_closed_trades(state: dict, trader: OandaTrader, alert: TelegramAlert):
    """Check if any open positions have been closed (TP/SL hit)."""
    for pair in list(state.get("open_times", {}).keys()):
        if trader.get_position(pair):
            continue
        try:
            url  = (trader.base_url + "/v3/accounts/" + trader.account_id +
                    "/trades?state=CLOSED&instrument=" + pair + "&count=1")
            data = requests.get(url, headers=trader.headers, timeout=10).json().get("trades", [])
            if data:
                t       = data[0]
                pnl     = float(t.get("realizedPL", "0"))
                open_px = float(t.get("price", 0))
                exit_px = float(t.get("averageClosePrice", open_px))
                balance = trader.get_balance()
                cfg     = PAIRS[pair]
                state["daily_pnl"] = state.get("daily_pnl", 0.0) + pnl
                if pnl < 0:
                    _set_cooldown(state, pair)
                    state["losses"]        = state.get("losses", 0) + 1
                    state["consec_losses"] = state.get("consec_losses", 0) + 1
                    alert.send_sl_hit(pair, cfg["emoji"], pnl, balance,
                                      state.get("wins",0), state.get("losses",0),
                                      open_px, exit_px)
                else:
                    state["wins"]          = state.get("wins", 0) + 1
                    state["consec_losses"] = 0
                    alert.send_tp_hit(pair, cfg["emoji"], pnl, balance,
                                      state.get("wins",0), state.get("losses",0),
                                      open_px, exit_px)
        except Exception as e:
            log.warning("Closed trade detect %s: %s", pair, e)
        state["open_times"].pop(pair, None)


def _session_open_alert(state: dict, alert: TelegramAlert,
                         trader: OandaTrader, now: datetime, today: str):
    """Send session-open alert once per window per day per pair."""
    hour = now.hour
    sent = state.setdefault("session_alerted", {})
    for pair, cfg in PAIRS.items():
        for s in cfg["sessions"]:
            if hour == s["start"]:
                key = f"{pair}_{today}_{s['label']}_open"
                if not sent.get(key):
                    sent[key] = True
                    try:
                        bal = trader.get_balance() if trader.login() else 0.0
                    except Exception:
                        bal = 0.0
                    alert.send_session_open(
                        pair=pair, emoji=cfg["emoji"],
                        session_label=s["label"], session_hours=s["hours"],
                        balance=bal,
                        trades_today=state.get("trades", 0),
                        wins=state.get("wins", 0),
                        losses=state.get("losses", 0),
                    )


def run_bot(state: dict):
    now   = datetime.now(sg_tz)
    hour  = now.hour
    today = now.strftime("%Y%m%d")

    alert    = TelegramAlert()
    calendar = EconomicCalendar()

    log.info("Scan %s SGT", now.strftime("%H:%M:%S"))

    trader = OandaTrader(demo=True)
    if not trader.login():
        log.warning("OANDA login failed")
        return

    balance = trader.get_balance()

    # Session open alerts
    _session_open_alert(state, alert, trader, now, today)

    # Check closed trades (TP/SL hits)
    _detect_closed_trades(state, trader, alert)

    # ── Scan each pair ─────────────────────────────────────────────────
    for pair, cfg in PAIRS.items():

        session = _active_session(hour, cfg["sessions"])
        if not session:
            log.info("%s: outside sessions (%02d:xx SGT)", pair, hour)
            continue

        if trader.get_position(pair):
            log.info("%s: position open", pair)
            continue

        if _in_cooldown(state, pair):
            log.info("%s: in cooldown", pair)
            continue

        pair_trades = state.get(f"trades_{pair}", 0)
        if pair_trades >= cfg["max_trades"]:
            log.info("%s: max trades reached (%d)", pair, cfg["max_trades"])
            continue

        # Price + spread
        price, bid, ask = trader.get_price(pair)
        if price is None:
            log.warning("%s: price fetch error", pair)
            continue

        spread = round((ask - bid) / cfg["pip"], 1)
        if spread > session["max_spread"] + 0.1:
            log.info("%s: spread %.1fp > %.1fp", pair, spread, session["max_spread"])
            continue

        # News filter
        news_ok, news_reason = calendar.is_news_time(pair)
        if news_ok:
            nkey = f"{pair}_news_{now.strftime('%Y%m%d%H')}"
            if not state.get("news_alerted", {}).get(nkey):
                state.setdefault("news_alerted", {})[nkey] = True
                alert.send_news_block(pair, cfg["emoji"], news_reason)
            log.info("%s: news block", pair)
            continue

        # ── Signal ────────────────────────────────────────────────────
        strategy = cfg["strategy"]
        layer_breakdown = {}

        if strategy == "triple_ema":
            direction, reason = triple_ema_signal(pair, cfg.get("max_gap", 50.0))
            score = 1 if direction else 0
            log.info("%s triple_ema: %s | %s", pair, direction, reason)

        elif strategy == "four_layer":
            score, direction, reason, layer_breakdown = four_layer_signal(pair, state)
            if direction == "NONE": direction = None
            log.info("%s four_layer: score=%d dir=%s | %s", pair, score, direction, reason)

        elif strategy == "audusd_range":
            direction, reason, layer_breakdown = audusd_signal(pair, state)
            score = 1 if direction else 0
            log.info("%s audusd_range: %s | %s", pair, direction, reason)

        else:
            log.warning("%s: unknown strategy %s", pair, strategy)
            continue

        # Send scan result to Telegram (so you can see bot is alive)
        alert.send_scan_result(
            pair      = pair,
            emoji     = cfg["emoji"],
            price     = price,
            spread    = spread,
            session   = session["label"],
            direction = direction,
            reason    = reason,
        )

        if not direction:
            continue

        # ── Place order ────────────────────────────────────────────────
        sl_pips = cfg["sl_pips"]
        tp_pips = cfg["tp_pips"]
        size    = cfg["trade_size"]

        result = trader.place_order(
            instrument     = pair,
            direction      = direction,
            size           = size,
            stop_distance  = sl_pips,
            limit_distance = tp_pips,
        )

        if result.get("success"):
            state["trades"]             = state.get("trades", 0) + 1
            state[f"trades_{pair}"]     = pair_trades + 1
            state.setdefault("open_times", {})[pair] = now.isoformat()
            log.info("%s: PLACED %s SL=%dp TP=%dp", pair, direction, sl_pips, tp_pips)
            alert.send_trade_open(
                pair=pair, emoji=cfg["emoji"], direction=direction,
                entry_price=price, sl_pips=sl_pips, tp_pips=tp_pips,
                size=size, spread=spread, score=score,
                session_label=session["label"],
                layer_breakdown=layer_breakdown,
                balance=balance,
                trades_today=state["trades"],
            )
        else:
            _set_cooldown(state, pair)
            log.warning("%s: order failed — %s", pair, result.get("error",""))

    log.info("Scan complete.")
