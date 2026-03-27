"""
OANDA — M1 Ultra-Scalp Bot
====================================
$10,000 demo | 4 pairs | 86,000 units

Trade specs:
  Size:    86,000 units
  SL:      3 pips => SGD ~34.83 (USD/JPY ~SGD 23)
  TP:      5 pips => SGD ~58.05 (USD/JPY ~SGD 39)
  Max dur: 15 minutes hard close
  4 pairs all TP = SGD ~232 per session

Sessions SGT:
  AUD/USD  : 6am-11am + 2pm-10pm
  EUR/USD  : 2pm-10pm  (extended — NY session added)
  GBP/USD  : 2pm-10pm  (new pair)
  USD/JPY  : 6am-11am + 8pm-10pm (new pair)

Telegram: EVENT-ONLY

FIX LOG (all bugs resolved):
  BUG-01: run_bot() now accepts state= arg from main.py (was crashing every cycle)
  BUG-02: Removed all file-based state (trades_YYYYMMDD.json) — uses in-memory STATE only
  BUG-03: detect_sl_tp_hits() now mutates passed-in state dict, no file writes
  BUG-04: EOD close block now updates state dict, no file writes
  BUG-05: 15-min close block now updates state dict, no file writes
  BUG-06: Place trade block now updates state dict, no file writes
  BUG-07: realized_pnl now uses state["start_balance"] seeded by main.py on day reset
  BUG-08: Telegram now shows actual score/threshold, not hardcoded "3/3"
  BUG-09: settings.json resolved relative to this file, not process CWD
  BUG-10: set_cooldown() stores UTC-aware ISO string (no naive datetime)
  BUG-11: in_cooldown() uses UTC-aware comparison throughout
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

TRADE_SIZE   = 86000
SL_PIPS      = 3
TP_PIPS      = 5
MAX_DURATION = 15
USD_SGD      = 1.35

ASSETS = {
    "AUD_USD": {
        "instrument": "AUD_USD", "asset": "AUDUSD", "emoji": "🦘",
        "pip": 0.0001, "precision": 5,
        "stop_pips": SL_PIPS, "tp_pips": TP_PIPS,
        "max_spread": 1.2,
        "sessions": [(6, 11), (14, 22)],
    },
    "EUR_USD": {
        "instrument": "EUR_USD", "asset": "EURUSD", "emoji": "🇪🇺💵",
        "pip": 0.0001, "precision": 5,
        "stop_pips": SL_PIPS, "tp_pips": TP_PIPS,
        "max_spread": 1.2,
        "sessions": [(14, 22)],
    },
    "GBP_USD": {
        "instrument": "GBP_USD", "asset": "GBPUSD", "emoji": "💷",
        "pip": 0.0001, "precision": 5,
        "stop_pips": SL_PIPS, "tp_pips": TP_PIPS,
        "max_spread": 1.5,
        "sessions": [(14, 22)],
    },
    "USD_JPY": {
        "instrument": "USD_JPY", "asset": "USDJPY", "emoji": "🇯🇵",
        "pip": 0.01, "precision": 3,
        "stop_pips": SL_PIPS, "tp_pips": TP_PIPS,
        "max_spread": 1.5,
        "sessions": [(6, 11), (20, 22)],
    },
}

DEFAULT_SETTINGS = {"signal_threshold": 3, "demo_mode": True}

# BUG-09 FIX: resolve settings.json relative to this file, not process CWD
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


# BUG-10 FIX: always store UTC-aware ISO string
def set_cooldown(state, name):
    if "cooldowns" not in state:
        state["cooldowns"] = {}
    state["cooldowns"][name] = datetime.now(timezone.utc).isoformat()
    log.info(name + " cooldown 30 min")


# BUG-11 FIX: compare two UTC-aware datetimes — no naive datetime anywhere
def in_cooldown(state, name):
    cd = state.get("cooldowns", {}).get(name)
    if not cd:
        return False
    try:
        cd_dt   = datetime.fromisoformat(cd)           # has +00:00 suffix → aware
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


# BUG-03 FIX: mutates state dict only — no file writes
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
        del state["open_times"][name]   # no file write


# BUG-01 FIX: run_bot() now accepts state= from main.py
def run_bot(state):
    settings = load_settings()
    now      = datetime.now(sg_tz)
    hour     = now.hour
    alert    = TelegramAlert()
    calendar = CalendarFilter()

    log.info("Scan at " + now.strftime("%H:%M:%S SGT"))

    if now.weekday() == 5:
        log.info("Saturday — silent"); return
    if now.weekday() == 6 and hour < 5:
        log.info("Sunday early — silent"); return

    active = [n for n, c in ASSETS.items() if is_in_session(hour, c)]
    if not active:
        log.info("No active sessions at " + str(hour) + "h SGT"); return

    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login():
        alert.send("❌ Login FAILED!")
        return

    current_balance = trader.get_balance()

    # BUG-07 FIX: start_balance seeded by main.py on day reset; guard for safety
    if "start_balance" not in state or state["start_balance"] == 0.0:
        state["start_balance"] = current_balance

    # BUG-02 FIX: no file I/O anywhere — state is purely in-memory
    realized_pnl = round(current_balance - state["start_balance"], 2)
    pl_sgd       = round(realized_pnl * USD_SGD, 2)
    pnl_emoji    = "✅" if realized_pnl >= 0 else "🔴"

    detect_sl_tp_hits(state, trader, alert)

    # ── EOD close ────────────────────────────────────────────────────
    if hour == 22 and now.minute >= 55:
        closed = []
        for name in ASSETS:
            if trader.get_position(name):
                trader.close_position(name)
                closed.append(name)
                # BUG-04 FIX: update state dict only
                state.get("open_times", {}).pop(name, None)
        if closed:
            alert.send(
                "🔔 EOD Close\n"
                "Closed: " + ", ".join(closed) + "\n"
                "Today:  $" + str(realized_pnl) + " " + pnl_emoji +
                " = SGD " + str(pl_sgd) + "\n"
                "W/L: " + str(state.get("wins", 0)) + "/" + str(state.get("losses", 0))
            )
        return

    # ── 15-MIN HARD CLOSE ────────────────────────────────────────────
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
                # BUG-05 FIX: update state dict only
                state.get("open_times", {}).pop(name, None)
                alert.send(
                    "⏰ 15-MIN TIMEOUT\n"
                    + ASSETS[name]["emoji"] + " " + name + "\n"
                    "Closed at " + str(round(mins, 1)) + " min\n"
                    "PnL: $" + str(round(pnl, 2)) + " USD " + ("✅" if pnl >= 0 else "🔴") + "\n"
                    "   ≈ SGD " + str(pnl_sgd)
                )
        except Exception as e:
            log.warning("Duration check " + name + ": " + str(e))

    # ── SCAN + TRADE ──────────────────────────────────────────────────
    threshold = settings.get("signal_threshold", 3)

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

        # ── Place trade ──────────────────────────────────────────────
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
            # BUG-06 FIX: no file write — state stays in memory
            price, _, _ = trader.get_price(name)
            # BUG-08 FIX: show real score/threshold, not hardcoded "3/3"
            alert.send(
                "🔄 NEW TRADE!\n"
                + cfg["emoji"] + " " + name + "\n"
                "Direction: " + direction + "\n"
                "Score:     " + str(score) + "/" + str(threshold) + " ✅\n"
                "Size:      86,000 units\n"
                "Entry:     " + str(round(price, cfg["precision"])) + "\n"
                "SL:        " + str(SL_PIPS) + " pips ≈ SGD " + str(sl_sgd) + "\n"
                "TP:        " + str(TP_PIPS) + " pips ≈ SGD " + str(tp_sgd) + "\n"
                "Max Time:  15 min\n"
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
    log.info("🚀 Ultra-Scalp | 4 pairs | SL=3pip TP=5pip | 15min max")
    local_state = {
        "date": datetime.now(sg_tz).strftime("%Y%m%d"),
        "trades": 0, "start_balance": 0.0,
        "wins": 0, "losses": 0, "consec_losses": 0,
        "cooldowns": {}, "open_times": {}, "news_alerted": {},
    }
    while True:
        try:
            run_bot(state=local_state)
        except Exception as e:
            log.error("Bot error: " + str(e))
        time.sleep(60)
