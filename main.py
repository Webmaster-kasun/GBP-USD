"""
main.py — Multi-Pair Bot Entry Point (v3.2)

Pairs:    GBP/USD | EUR/USD | AUD/USD
Platform: Railway (polling) or GitHub Actions (single-shot)
Account:  SGD
"""

import os, time, logging, traceback
from datetime import datetime
import pytz

from bot            import run_bot
from oanda_trader   import OandaTrader
from telegram_alert import TelegramAlert

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

INTERVAL_MINUTES = 5
sg_tz            = pytz.timezone("Asia/Singapore")
STATE            = {}


def fresh_day_state(today: str, balance: float) -> dict:
    return {
        "date":            today,
        "trades":          0,
        "start_balance":   balance,
        "daily_pnl":       0.0,
        "wins":            0,
        "losses":          0,
        "consec_losses":   0,
        "cooldowns":       {},
        "open_times":      {},
        "news_alerted":    {},
        "session_alerted": {},
    }


def check_env_vars() -> bool:
    api_key    = os.environ.get("OANDA_API_KEY", "")
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    if not api_key or not account_id:
        log.error("Missing OANDA_API_KEY or OANDA_ACCOUNT_ID")
        return False
    if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        log.warning("Telegram not configured — no alerts will be sent")
    log.info("Env OK | Key: %s**** | Account: %s", api_key[:8], account_id)
    return True


def run_once(state: dict) -> dict:
    global STATE

    now   = datetime.now(sg_tz)
    today = now.strftime("%Y%m%d")
    alert = TelegramAlert()

    log.info("⏰ %s SGT", now.strftime("%Y-%m-%d %H:%M"))

    # Daily reset at SGT midnight
    if state.get("date") != today:
        # Daily summary for previous day
        if state.get("date"):
            try:
                trader = OandaTrader(demo=True)
                bal    = trader.get_balance() if trader.login() else 0.0
                alert.send_daily_summary(
                    balance       = bal,
                    start_balance = state.get("start_balance", 0),
                    trades        = state.get("trades", 0),
                    wins          = state.get("wins", 0),
                    losses        = state.get("losses", 0),
                    pnl           = state.get("daily_pnl", 0.0),
                )
            except Exception as e:
                log.warning("Daily summary error: %s", e)

        log.info("📅 New day — fetching balance...")
        try:
            trader  = OandaTrader(demo=True)
            balance = trader.get_balance() if trader.login() else 0.0
        except Exception as e:
            log.warning("Balance fetch error: %s", e)
            balance = 0.0

        state = fresh_day_state(today, balance)
        STATE = state
        log.info("📅 New day: %s | Balance: SGD %.2f", today, balance)
        alert.send_new_day(balance, now.strftime("%Y-%m-%d"))

    run_bot(state=state)
    return state


def main():
    global STATE

    log.info("=" * 55)
    log.info("🚀 Multi-Pair Bot v3.2 Started")
    log.info("Pairs: GBP/USD | EUR/USD | AUD/USD")
    log.info("SL=15p | TP=25p | RR=1:1.67")
    log.info("Account: SGD | Platform: Railway / GitHub Actions")
    log.info("=" * 55)

    if not check_env_vars():
        return

    is_railway = os.environ.get("RAILWAY", "").lower() in ("true", "1", "yes")

    if is_railway:
        log.info("Railway mode — polling every %d min", INTERVAL_MINUTES)
        try:
            trader  = OandaTrader(demo=True)
            balance = trader.get_balance() if trader.login() else 0.0
        except Exception:
            balance = 0.0
        TelegramAlert().send_startup(balance, "DEMO")

        while True:
            try:
                STATE = run_once(STATE)
            except Exception as e:
                log.error("Bot error: %s", e)
                log.error(traceback.format_exc())
                time.sleep(30)
            log.info("💤 Sleeping %d min...", INTERVAL_MINUTES)
            time.sleep(INTERVAL_MINUTES * 60)

    else:
        log.info("GitHub Actions mode — single run")
        try:
            STATE = run_once(STATE)
        except Exception as e:
            log.error("Bot error: %s", e)
            log.error(traceback.format_exc())


if __name__ == "__main__":
    main()
