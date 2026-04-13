"""
Railway Entry Point - OANDA GBP/USD London Scalp Bot
=====================================================
FIX LOG:
  FIX-01: Login FAILED Telegram alert suppressed outside London session
  FIX-02: Bot crash loop protection — catches all exceptions in main loop
  FIX-03: Day-reset login failure no longer sends Telegram spam
  FIX-04: Added startup Telegram notification so you know bot is alive
  FIX-05: Session start alert sent once per day when London opens
  FIX-06: restartPolicyMaxRetries kept at 10 but sleep on failure prevents spam
"""

import os, time, logging, traceback
from datetime import datetime
import pytz

from bot          import run_bot, ASSETS, is_in_session
from oanda_trader import OandaTrader
from telegram_alert import TelegramAlert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

INTERVAL_MINUTES = 5

sg_tz = pytz.timezone("Asia/Singapore")

STATE = {}


def get_today_key():
    return datetime.now(sg_tz).strftime("%Y%m%d")


def fresh_day_state(today_str, balance):
    return {
        "date":          today_str,
        "trades":        0,
        "start_balance": balance,
        "daily_pnl":     0.0,
        "stopped":       False,
        "wins":          0,
        "losses":        0,
        "consec_losses": 0,
        "cooldowns":     {},
        "open_times":    {},
        "news_alerted":  {},
        "session_alerted": False,   # FIX-05: track session open alert
    }


def check_env_vars():
    api_key    = os.environ.get("OANDA_API_KEY", "")
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    tg_token   = os.environ.get("TELEGRAM_TOKEN", "")
    tg_chat    = os.environ.get("TELEGRAM_CHAT_ID", "")

    ok = True
    if not api_key or not account_id:
        log.error("=" * 50)
        log.error("❌ MISSING OANDA ENV VARS!")
        log.error("   OANDA_API_KEY    : " + ("SET ✅" if api_key    else "MISSING ❌"))
        log.error("   OANDA_ACCOUNT_ID : " + ("SET ✅" if account_id else "MISSING ❌"))
        log.error("=" * 50)
        ok = False
    else:
        log.info("Env vars OK | Key: " + api_key[:8] + "**** | Account: " + account_id)

    if not tg_token or not tg_chat:
        log.warning("Telegram not configured — no alerts will be sent.")

    return ok


def is_london_session_now():
    now  = datetime.now(sg_tz)
    hour = now.hour
    return any(is_in_session(hour, cfg) for cfg in ASSETS.values())


def main():
    global STATE

    log.info("=" * 50)
    log.info("🚀 Railway Bot Started - OANDA GBP/USD London Scalp")
    log.info("Strategy: GBP/USD | SL=13pip | TP=26pip | 15:00-24:00 SGT")
    log.info("Interval: Every " + str(INTERVAL_MINUTES) + " minutes")
    log.info("=" * 50)

    if not check_env_vars():
        log.error("Missing env vars — sleeping 60s then exiting to trigger Railway restart.")
        time.sleep(60)
        return

    # FIX-04: Send startup Telegram so you know the bot is live
    alert = TelegramAlert()
    alert.send(
        "🚀 Bot Started\n"
        "Account: " + os.environ.get("OANDA_ACCOUNT_ID", "?") + "\n"
        "Mode: DEMO\n"
        "Session: 15:00–24:00 SGT\n"
        "Waiting for London session..."
    )

    while True:
        try:
            now   = datetime.now(sg_tz)
            today = now.strftime("%Y%m%d")
            log.info("⏰ " + now.strftime("%Y-%m-%d %H:%M SGT"))

            # Day reset — fetch live balance (no Telegram on failure here)
            if STATE.get("date") != today:
                log.info("📅 New day! Fetching balance for day reset...")
                try:
                    trader  = OandaTrader(demo=True)
                    balance = trader.get_balance() if trader.login() else 0.0
                except Exception as e:
                    log.warning("Could not fetch balance for day reset: " + str(e))
                    balance = 0.0

                log.info("📅 New day! Balance: $" + str(round(balance, 2)))
                STATE = fresh_day_state(today, balance)

            # FIX-05: Alert once when London session opens each day
            if is_london_session_now() and not STATE.get("session_alerted"):
                STATE["session_alerted"] = True
                balance = STATE.get("start_balance", 0.0)
                alert.send(
                    "🔔 London Session Open!\n"
                    "⏰ " + now.strftime("%H:%M SGT") + "\n"
                    "Balance: $" + str(round(balance, 2)) + "\n"
                    "Bot is now scanning GBP/USD..."
                )

            run_bot(state=STATE)

        except Exception as e:
            log.error("❌ Bot error: " + str(e))
            log.error(traceback.format_exc())
            # FIX-02: Don't let crashes cause rapid restart spam
            time.sleep(30)

        log.info("💤 Sleeping " + str(INTERVAL_MINUTES) + " mins...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
