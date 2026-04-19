"""
main.py — Entry point for GBP/USD trend-pullback bot (v2 Improved)

Sessions (SGT):
  07:00 – 13:00  London Open
  15:00 – 19:00  NY Overlap

Run modes:
  GitHub Actions — single shot per cron trigger (every 5 min via workflow)
  Railway        — polling loop every 5 minutes (set RAILWAY=true env var)

Fixes vs v1:
  FIX-01: Session alerts updated — Asian Pre-London and Late NY removed
  FIX-02: News filter wired in — pauses 30 min before/after high-impact events
  FIX-03: State persisted across GitHub Actions runs via bot_state.json artifact
  FIX-04: Max trades per day set to 1 in fresh_day_state
"""

import os
import time
import logging
import traceback
from datetime import datetime
import pytz

from bot             import run_bot, ASSETS, is_in_session
from oanda_trader    import OandaTrader
from telegram_alert  import TelegramAlert
from calendar_filter import EconomicCalendar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

INTERVAL_MINUTES = 5
sg_tz            = pytz.timezone("Asia/Singapore")
STATE            = {}

# Only London and NY Overlap session alerts
SESSION_ALERTS = [
    {"start": 7,  "label": "London Open", "desc": "07:00–13:00 SGT"},
    {"start": 15, "label": "NY Overlap",  "desc": "15:00–19:00 SGT"},
]

STATE_FILE = "bot_state.json"


def load_state():
    import json
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                s = json.load(f)
                log.info(f"State loaded: {s.get('date')} | trades={s.get('trades', 0)}")
                return s
    except Exception as e:
        log.warning(f"State load failed: {e}")
    return {}


def save_state(state):
    import json
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
        log.info(f"State saved: {state.get('date')} | trades={state.get('trades', 0)}")
    except Exception as e:
        log.warning(f"State save failed: {e}")


def fresh_day_state(today_str, balance):
    return {
        "date":            today_str,
        "trades":          0,
        "start_balance":   balance,
        "daily_pnl":       0.0,
        "stopped":         False,
        "wins":            0,
        "losses":          0,
        "consec_losses":   0,
        "cooldowns":       {},
        "open_times":      {},
        "news_alerted":    {},
        "windows_used":    {},
        "session_alerted": {},
    }


def check_env_vars():
    api_key    = os.environ.get("OANDA_API_KEY", "")
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    tg_token   = os.environ.get("TELEGRAM_TOKEN", "")
    tg_chat    = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not api_key or not account_id:
        log.error("=" * 50)
        log.error("❌ MISSING OANDA ENV VARS!")
        log.error("   OANDA_API_KEY    : " + ("SET ✅" if api_key    else "MISSING ❌"))
        log.error("   OANDA_ACCOUNT_ID : " + ("SET ✅" if account_id else "MISSING ❌"))
        log.error("=" * 50)
        return False

    log.info("Env vars OK | Key: " + api_key[:8] + "**** | Account: " + account_id)

    if not tg_token or not tg_chat:
        log.warning("Telegram not configured — no alerts will be sent")

    return True


def check_session_open_alerts(alert, state):
    now   = datetime.now(sg_tz)
    hour  = now.hour
    today = now.strftime("%Y%m%d")

    session_alerted = state.setdefault("session_alerted", {})

    for w in SESSION_ALERTS:
        if hour != w["start"]:
            continue
        akey = f"session_open_{today}_{w['label']}"
        if session_alerted.get(akey):
            continue

        session_alerted[akey] = True
        balance = state.get("start_balance", 0.0)
        alert.send(
            f"🔔 {w['label']} Window Open!\n"
            f"⏰ {now.strftime('%H:%M SGT')} ({w['desc']})\n"
            f"Balance: ${round(balance, 2)}\n"
            f"Scanning GBP/USD..."
        )


def run_once(state, calendar):
    global STATE

    now   = datetime.now(sg_tz)
    today = now.strftime("%Y%m%d")
    log.info("⏰ " + now.strftime("%Y-%m-%d %H:%M SGT"))

    # Reset state at start of each new day
    if state.get("date") != today:
        log.info("📅 New day — fetching balance...")
        try:
            trader  = OandaTrader(demo=True)
            balance = trader.get_balance() if trader.login() else 0.0
        except Exception as e:
            log.warning("Balance fetch error: " + str(e))
            balance = 0.0
        log.info(f"📅 New day! Balance: ${round(balance, 2)}")
        state = fresh_day_state(today, balance)
        STATE = state

    alert = TelegramAlert()
    check_session_open_alerts(alert, state)

    # News blackout filter
    is_news, news_reason = calendar.is_news_time("GBP_USD")
    if is_news:
        log.warning(f"📰 NEWS BLACKOUT — skipping: {news_reason}")
        news_alerted = state.setdefault("news_alerted", {})
        nkey = f"news_{today}_{news_reason[:40]}"
        if not news_alerted.get(nkey):
            news_alerted[nkey] = True
            alert.send(f"📰 News Blackout!\n{news_reason}\nBot paused 30 min.")
        return state

    run_bot(state=state)
    return state


def main():
    global STATE

    log.info("=" * 50)
    log.info("🚀 GBP/USD Trend-Pullback Bot v2 — Improved")
    log.info("Session 1: 07:00–13:00 SGT  London Open")
    log.info("Session 2: 15:00–19:00 SGT  NY Overlap")
    log.info("GBP/USD | SL=20pip | TP=30pip (1.5:1) | Max 1 trade/day")
    log.info("Trend: H1 EMA50/200 | RSI gate | EMA34 pullback | ATR≥5pip")
    log.info("=" * 50)

    if not check_env_vars():
        log.error("Missing env vars — exiting")
        return

    calendar = EconomicCalendar()

    is_railway = os.environ.get("RAILWAY", "").lower() in ("true", "1", "yes")

    if is_railway:
        log.info("🚂 Railway mode — polling loop active")
        alert = TelegramAlert()
        alert.send(
            "🚀 Bot Started (Railway) v2!\n"
            "Pair: GBP/USD\n"
            "SL: 20 pip | TP: 30 pip | RR: 1.5:1\n"
            "Sessions: London 07-13 / NY 15-19 SGT\n"
            "Max 1 trade/day | News filter ON\n"
            "Strategy: EMA50/200 trend + RSI + EMA34 pullback"
        )
        STATE = load_state()
        while True:
            try:
                STATE = run_once(STATE, calendar)
                save_state(STATE)
            except Exception as e:
                log.error("❌ Bot error: " + str(e))
                log.error(traceback.format_exc())
                time.sleep(30)
            log.info(f"💤 Sleeping {INTERVAL_MINUTES} mins...")
            time.sleep(INTERVAL_MINUTES * 60)

    else:
        log.info("⚡ GitHub Actions mode — single run")
        STATE = load_state()
        try:
            STATE = run_once(STATE, calendar)
            save_state(STATE)
        except Exception as e:
            log.error("❌ Bot error: " + str(e))
            log.error(traceback.format_exc())


if __name__ == "__main__":
    main()
