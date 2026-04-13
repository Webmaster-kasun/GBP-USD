"""
OANDA — M1 Ultra-Scalp Bot  (GBP/USD ONLY — London Session)
============================================================
$10,000 demo | 1 pair | 74,000 units

Trade specs:
  Pair:    GBP/USD only
  Size:    74,000 units
  SL:      13 pips => SGD ~130
  TP:      26 pips => SGD ~260  [2:1 R:R]
  Max dur: 30 minutes hard close

Session SGT (London):
  GBP/USD  : 15:00 – 23:59 SGT  (London 08:00-17:00 + NY overlap)

Signal Engine (4 layers):
  L0: M15 EMA8 vs EMA21 — direction must match M15 momentum
  L1: M5  EMA8 vs EMA21 — trend bias confirmation
  L2: M5  RSI(9) <=35 BUY / >=65 SELL + delta>=1.0 + EMA50 + candle
  L3: M1  trigger candle — engulf or pin-bar
  L4: H1  EMA200 — hard block: only BUY above, only SELL below

FIX LOG:
  FIX-01: Login FAILED Telegram only sent inside London session
           — prevents spam during off-hours restarts/redeploys
  FIX-02: Login fail alert deduplicated — max 1 alert per 30 mins
  FIX-03: Daily summary sent at session open (moved to main.py)
"""

import os, json, time, logging, requests
from datetime import datetime, timezone
from pathlib import Path
import pytz

from signals         import SignalEngine
from oanda_trader    import OandaTrader
from telegram_alert  import TelegramAlert
from calendar_filter import EconomicCalendar as CalendarFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

sg_tz   = pytz.timezone("Asia/Singapore")
signals = SignalEngine()

TRADE_SIZE   = 74000   # => TP ~SGD 100 / SL ~SGD 50
SL_PIPS      = 13
TP_PIPS      = 26
MAX_DURATION = 30
USD_SGD      = 1.35

# GBP/USD only — London session: 15:00–23:59 SGT
ASSETS = {
    "GBP_USD": {
        "instrument": "GBP_USD", "asset": "GBPUSD", "emoji": "💷",
        "pip": 0.0001, "precision": 5,
        "stop_pips": SL_PIPS, "tp_pips": TP_PIPS,
        "max_spread": 1.5,
        "sessions": [(15, 24)],   # 15:00 – 23:59 SGT
    },
}

DEFAULT_SETTINGS = {"signal_threshold": 4, "demo_mode": True}

_SETTINGS_PATH = Path(__file__).parent / "settings.json"


def load_settings():
    try:
        with open(_SETTINGS_PATH) as f:
            DEFAULT_SETTINGS.update(json.load(f))
    except FileNotFoundError:
        with open(_SETTINGS_PATH, "w") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
    return DEFAULT_SETTINGS


def is_in_session(hour, cfg):
    return any(s <= hour < e for s, e in cfg["sessions"])


def set_cooldown(state, name):
    if "cooldowns" not in state:
        state["cooldowns"] = {}
    state["cooldowns"][name] = datetime.now(timezone.utc).isoformat()
    log.info(name + " cooldown 30 min")


def in_cooldown(state, name):
    cd = state.get("cooldowns", {}).get(name)
    if not cd:
        return False
    try:
        cd_dt   = datetime.fromisoformat(cd)
        now_utc = datetime.now(timezone.utc)
        elapsed = (now_utc - cd_dt).total_seconds() / 60
        return elapsed < 30
    except Exception as e:
        log.warning("in_cooldown parse error for " + name + ": " + str(e))
        return False


def cooldown_remaining(state, name):
    cd = state.get("cooldowns", {}).get(name)
    if not cd:
        return 0
    try:
        cd_dt   = datetime.fromisoformat(cd)
        now_utc = datetime.now(timezone.utc)
        elapsed = (now_utc - cd_dt).total_seconds() / 60
        return max(0, int(30 - elapsed))
    except:
        return "?"


def _login_fail_alert_key(now):
    """Key for deduplicating login fail alerts — one per 30-min window."""
    slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
    return "login_fail_" + now.strftime("%Y%m%d") + "_" + str(slot)


def detect_sl_tp_hits(state, trader, alert):
    if "open_times" not in state:
        return
    for name in list(state["open_times"].keys()):
        if trader.get_position(name):
            continue
        try:
            url  = (trader.base_url + "/v3/accounts/" + trader.account_id +
                    "/trades?state=CLOSED&instrument=" + name + "&count=1")
            data = requests.get(url, headers=trader.headers, timeout=10).json().get("trades", [])
            if data:
                pnl     = float(data[0].get("realizedPL", "0"))
                pnl_sgd = round(pnl * USD_SGD, 2)
                emoji   = ASSETS.get(name, {}).get("emoji", "")
                wins    = state.get("wins", 0)
                losses  = state.get("losses", 0)
                if pnl < 0:
                    set_cooldown(state, name)
                    state["losses"]        = losses + 1
                    state["consec_losses"] = state.get("consec_losses", 0) + 1
                    alert.send(
                        "🔴 SL HIT\n" + emoji + " " + name + "\n"
                        "Loss:  $" + str(round(pnl, 2)) + " USD\n"
                        "     ≈ SGD -" + str(abs(pnl_sgd)) + "\n"
                        "⏳ Cooldown 30 min\n"
                        "W/L: " + str(wins) + "/" + str(state["losses"])
                    )
                else:
                    state["wins"]          = wins + 1
                    state["consec_losses"] = 0
                    alert.send(
                        "✅ TP HIT\n" + emoji + " " + name + "\n"
                        "Profit: $+" + str(round(pnl, 2)) + " USD\n"
                        "      ≈ SGD +" + str(pnl_sgd) + "\n"
                        "W/L: " + str(state["wins"]) + "/" + str(losses)
                    )
        except Exception as e:
            log.warning("SL/TP detect error " + name + ": " + str(e))
        del state["open_times"][name]


def run_bot(state):
    settings  = load_settings()
    now       = datetime.now(sg_tz)
    hour      = now.hour
    alert     = TelegramAlert()
    calendar  = CalendarFilter()

    log.info("Scan at " + now.strftime("%H:%M:%S SGT"))

    active = [n for n, c in ASSETS.items() if is_in_session(hour, c)]
    if not active:
        log.info("Outside London session (" + str(hour) + "h SGT) — sleeping until 15:00 SGT")
        return

    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login():
        # FIX-01 + FIX-02: Only alert inside session, and deduplicate per 30-min window
        alert_key = _login_fail_alert_key(now)
        if not state.get("login_fail_alerted", {}).get(alert_key):
            if "login_fail_alerted" not in state:
                state["login_fail_alerted"] = {}
            state["login_fail_alerted"][alert_key] = True

            api_key    = os.environ.get("OANDA_API_KEY", "")
            account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
            if not api_key or not account_id:
                msg = (
                    "❌ Login FAILED — Missing Env Vars!\n"
                    "OANDA_API_KEY: " + ("✅ SET" if api_key else "❌ MISSING") + "\n"
                    "OANDA_ACCOUNT_ID: " + ("✅ SET" if account_id else "❌ MISSING") + "\n"
                    "→ Go to Railway → Variables and set both!"
                )
            else:
                msg = (
                    "❌ Login FAILED\n"
                    "Key: " + api_key[:8] + "****\n"
                    "Account: " + account_id + "\n"
                    "Check Railway logs for HTTP error."
                )
            alert.send(msg)
        else:
            log.warning("Login failed (alert already sent this window — suppressed)")
        return

    current_balance = trader.get_balance()

    if "start_balance" not in state or state["start_balance"] == 0.0:
        state["start_balance"] = current_balance

    realized_pnl = round(current_balance - state["start_balance"], 2)
    pl_sgd       = round(realized_pnl * USD_SGD, 2)

    detect_sl_tp_hits(state, trader, alert)

    # ── 30-MIN HARD CLOSE ──────────────────────────────────────────────
    for name in ASSETS:
        pos = trader.get_position(name)
        if not pos:
            continue
        try:
            trade_id, open_str = trader.get_open_trade_id(name)
            if not trade_id or not open_str:
                continue
            open_utc = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            mins     = (datetime.now(pytz.utc) - open_utc).total_seconds() / 60
            log.info(name + ": open " + str(round(mins, 1)) + " min")
            if mins >= MAX_DURATION:
                pnl     = trader.check_pnl(pos)
                pnl_sgd = round(pnl * USD_SGD, 2)
                trader.close_position(name)
                state.get("open_times", {}).pop(name, None)
                alert.send(
                    "⏰ 30-MIN TIMEOUT\n"
                    + ASSETS[name]["emoji"] + " " + name + "\n"
                    "Closed at " + str(round(mins, 1)) + " min\n"
                    "PnL: $" + str(round(pnl, 2)) + " USD " + ("✅" if pnl >= 0 else "🔴") + "\n"
                    "   ≈ SGD " + str(pnl_sgd)
                )
        except Exception as e:
            log.warning("Duration check " + name + ": " + str(e))

    # ── SCAN + TRADE ────────────────────────────────────────────────────
    threshold = settings.get("signal_threshold", 4)

    for name, cfg in ASSETS.items():
        if not is_in_session(hour, cfg):
            log.info(name + ": off-session"); continue

        pos = trader.get_position(name)
        if pos:
            pnl_sgd = round(trader.check_pnl(pos) * USD_SGD, 2)
            dirn    = "BUY" if int(float(pos.get("long", {}).get("units", 0))) > 0 else "SELL"
            log.info(name + ": " + dirn + " open SGD " + str(pnl_sgd))
            continue

        if in_cooldown(state, name):
            remaining = cooldown_remaining(state, name)
            log.info(name + ": cooldown " + str(remaining) + "min"); continue

        price, bid, ask = trader.get_price(name)
        if price is None:
            log.warning(name + ": price error"); continue

        spread = (ask - bid) / cfg["pip"]
        if spread > cfg["max_spread"]:
            log.info(name + ": spread " + str(round(spread, 2)) + "p > " + str(cfg["max_spread"]) + "p skip"); continue

        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            alert_key = name + "_news_" + now.strftime("%Y%m%d%H")
            if not state.get("news_alerted", {}).get(alert_key):
                if "news_alerted" not in state:
                    state["news_alerted"] = {}
                state["news_alerted"][alert_key] = True
                alert.send("⚠️ NEWS BLOCK\n" + cfg["emoji"] + " " + name + "\n" + news_reason + "\nSkipping this hour")
            log.info(name + ": news — " + news_reason); continue

        score, direction, details = signals.analyze(asset=cfg["asset"])
        log.info(name + ": score=" + str(score) + "/" + str(threshold) +
                 " dir=" + direction + " | " + details)

        if score < threshold or direction == "NONE":
            log.info(name + ": no setup — waiting"); continue

        # ── Place trade ────────────────────────────────────────────────
        sl_sgd = round(TRADE_SIZE * SL_PIPS * cfg["pip"] * USD_SGD, 2)
        tp_sgd = round(TRADE_SIZE * TP_PIPS * cfg["pip"] * USD_SGD, 2)

        result = trader.place_order(
            instrument=name, direction=direction, size=TRADE_SIZE,
            stop_distance=SL_PIPS, limit_distance=TP_PIPS
        )
        if result["success"]:
            state["trades"] = state.get("trades", 0) + 1
            if "open_times" not in state:
                state["open_times"] = {}
            state["open_times"][name] = now.isoformat()
            price, _, _ = trader.get_price(name)
            alert.send(
                "🔄 NEW TRADE!\n"
                + cfg["emoji"] + " " + name + "\n"
                "Direction: " + direction + "\n"
                "Score:     " + str(score) + "/" + str(threshold) + " ✅\n"
                "Size:      74,000 units\n"
                "Entry:     " + str(round(price, cfg["precision"])) + "\n"
                "SL:        " + str(SL_PIPS) + " pips ≈ SGD " + str(sl_sgd) + "\n"
                "TP:        " + str(TP_PIPS) + " pips ≈ SGD " + str(tp_sgd) + "\n"
                "Max Time:  30 min\n"
                "Spread:    " + str(round(spread, 2)) + "p\n"
                "Signals:   " + details
            )
            log.info(name + ": PLACED " + direction +
                     " SGD SL=" + str(sl_sgd) + " TP=" + str(tp_sgd))
        else:
            set_cooldown(state, name)
            log.warning(name + ": order failed — " + str(result.get("error", "")))

    log.info("Scan complete.")


if __name__ == "__main__":
    log.info("🚀 GBP/USD London Scalp | SL=13pip(~SGD130) TP=26pip(~SGD260) | 30min max | 15:00-24:00 SGT")
    local_state = {
        "date": datetime.now(sg_tz).strftime("%Y%m%d"),
        "trades": 0, "start_balance": 0.0,
        "wins": 0, "losses": 0, "consec_losses": 0,
        "cooldowns": {}, "open_times": {}, "news_alerted": {},
        "session_alerted": False, "login_fail_alerted": {},
    }
    while True:
        try:
            run_bot(state=local_state)
        except Exception as e:
            log.error("Bot error: " + str(e))
        time.sleep(60)
