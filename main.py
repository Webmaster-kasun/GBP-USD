"""
Railway Entry Point - OANDA Demo 2 Mean Reversion Bot
======================================================
Railway runs this 24/7 as a continuous process.
Built-in scheduler runs bot.run_bot() every 5 minutes.
No GitHub Actions needed!
"""

import time
import logging
import traceback
from datetime import datetime
import pytz

# Import the bot logic
from bot import run_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

INTERVAL_MINUTES = 5   # Run every 5 minutes

def main():
    sg_tz = pytz.timezone("Asia/Singapore")
    log.info("=" * 50)
    log.info("🚀 Railway Bot Started - OANDA Demo 2")
    log.info("Strategy: Mean Reversion")
    log.info("Schedule: Every " + str(INTERVAL_MINUTES) + " minutes")
    log.info("=" * 50)

    while True:
        now = datetime.now(sg_tz)
        log.info("⏰ Running bot at " + now.strftime("%Y-%m-%d %H:%M SGT"))

        try:
            run_bot()
        except Exception as e:
            log.error("❌ Bot error: " + str(e))
            log.error(traceback.format_exc())

        # Wait exactly 5 minutes before next run
        log.info("💤 Sleeping " + str(INTERVAL_MINUTES) + " mins until next run...")
        time.sleep(INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
