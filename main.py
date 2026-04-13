"""
Railway Entry Point - OANDA GBP/USD London Scalp Bot
======================================================
Railway runs this 24/7 as a continuous process.
Runs bot every 5 minutes with IN-MEMORY state
(Railway filesystem is ephemeral - no file storage!)

FIX LOG:
  BUG-12: STATE now includes "start_balance" field (was missing — broke daily PnL calc)
  BUG-13: Day-reset block now fetches fresh balance from OANDA to seed start_balance
           (previously reset to 0.0, making daily PnL always show $0)
"""

import time
import logging
import traceback
from datetime import datetime
import pytz

from bot           import run_bot
from oanda_trader  import OandaTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

INTERVAL_MINUTES = 5

# ── IN-MEMORY STATE (persists across 5-min runs, resets on restart) ──────────
STATE = {}


def get_today_key():
    sg_tz = pytz.timezone("Asia/Singapore")
    return datetime.now(sg_tz).strftime("%Y%m%d")


def fresh_day_state(today_str, balance):
    """Return a clean state dict for a new trading day."""
    return {
        "date":          today_str,
        "trades":        0,
        # BUG-12/13 FIX: seed start_balance from live account balance
        "start_balance": balance,
        "daily_pnl":     0.0,
        "stopped":       False,
        "wins":          0,
        "losses":        0,
        "consec_losses": 0,
        "cooldowns":     {},
        "open_times":    {},
        "news_alerted":  {},
    }


def main():
    sg_tz = pytz.timezone("Asia/Singapore")
    global STATE

    log.info("=" * 50)
    log.info("🚀 Railway Bot Started - OANDA Demo 2")
    log.info("Strategy: GBP/USD London Session M1 Scalp")
    log.info("Interval: Every " + str(INTERVAL_MINUTES) + " minutes")
    log.info("=" * 50)

    while True:
        now   = datetime.now(sg_tz)
        today = now.strftime("%Y%m%d")
        log.info("⏰ " + now.strftime("%Y-%m-%d %H:%M SGT"))

        # BUG-12/13 FIX: fetch live balance when resetting for a new day
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

        try:
            run_bot(state=STATE)
        except Exception as e:
            log.error("❌ Bot error: " + str(e))
            log.error(traceback.format_exc())

        log.info("💤 Sleeping " + str(INTERVAL_MINUTES) + " mins...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
