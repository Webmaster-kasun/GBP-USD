"""
telegram_alert.py — Telegram Alerts for GBP/USD Bot (SGD account)
"""
import os
import requests
import logging

log = logging.getLogger(__name__)


class TelegramAlert:
    def __init__(self):
        self.token   = os.environ.get("TELEGRAM_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            log.warning("Telegram not configured — TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing")
            return False
        try:
            url  = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
            r    = requests.post(url, data=data, timeout=10)
            if r.status_code == 200:
                log.info("Telegram sent!")
                return True
            log.warning(f"Telegram error {r.status_code}: {r.text[:200]}")
            return False
        except Exception as e:
            log.error(f"Telegram error: {e}")
            return False

    def send_startup(self, balance: float, date: str):
        self.send(
            f"🤖 <b>GBP/USD Bot Started</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Date:       {date}\n"
            f"💰 Balance:    <b>SGD {balance:,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Strategy:   Triple EMA Momentum\n"
            f"🎯 TP:         30 pips\n"
            f"🛡 SL:         15 pips\n"
            f"⚖️ RR:          2 : 1\n"
            f"🔢 Max trades: 1 per day\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Bot is running — scanning GBP/USD"
        )

    def send_new_day(self, balance: float, date: str):
        self.send(
            f"🌅 <b>New Trading Day</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Date:       {date}\n"
            f"💰 Balance:    <b>SGD {balance:,.2f}</b>\n"
            f"📊 GBP/USD strategy armed\n"
            f"🔍 Scanning for triple EMA signal..."
        )

    def send_scan_result(self, price: float, spread: float, ema5: float,
                         ema10: float, ema20: float, signal: str, reason: str):
        if ema5 < ema10 < ema20:
            trend_icon = "📉 DOWNTREND"
        elif ema5 > ema10 > ema20:
            trend_icon = "📈 UPTREND"
        else:
            trend_icon = "➡️ MIXED / NO TREND"

        signal_line = f"✅ Signal: <b>{signal}</b>" if signal else f"⏭ No signal — {reason}"

        self.send(
            f"🔍 <b>Market Scan</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💹 GBP/USD:    {price:.5f}\n"
            f"📡 Spread:     {spread:.1f} pips\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 EMA5:       {ema5:.5f}\n"
            f"📊 EMA10:      {ema10:.5f}\n"
            f"📊 EMA20:      {ema20:.5f}\n"
            f"🧭 Trend:      {trend_icon}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{signal_line}"
        )

    def send_trade_open(self, direction: str, entry: float, sl: float,
                        tp: float, sl_pips: int, tp_pips: int,
                        size: int, balance: float):
        icon = "🟢" if direction == "BUY" else "🔴"
        self.send(
            f"{icon} <b>Trade Opened — {direction}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💹 Pair:       GBP/USD\n"
            f"📌 Direction:  <b>{direction}</b>\n"
            f"🎯 Entry:      {entry:.5f}\n"
            f"🛡 Stop Loss:  {sl:.5f}  (-{sl_pips}p)\n"
            f"✅ Take Profit:{tp:.5f}  (+{tp_pips}p)\n"
            f"⚖️ RR:          1 : 2\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Size:       {size:,} units\n"
            f"💰 Balance:    <b>SGD {balance:,.2f}</b>"
        )

    def send_trade_close(self, direction: str, entry: float, exit_px: float,
                         pips: float, result: str, balance: float,
                         start_balance: float):
        day_pnl  = balance - start_balance
        icon     = "✅" if result == "WIN" else "❌"
        pip_sign = "+" if pips > 0 else ""
        pnl_sign = "+" if day_pnl >= 0 else ""
        self.send(
            f"{icon} <b>Trade Closed — {result}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💹 Pair:       GBP/USD  {direction}\n"
            f"📌 Entry:      {entry:.5f}\n"
            f"🏁 Exit:       {exit_px:.5f}\n"
            f"📊 P/L:        <b>{pip_sign}{pips:.1f} pips</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance:    <b>SGD {balance:,.2f}</b>\n"
            f"📈 Day P/L:    {pnl_sign}SGD {day_pnl:,.2f}"
        )

    def send_news_blackout(self, reason: str):
        self.send(
            f"📰 <b>News Blackout — Trading Paused</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Event:  {reason}\n"
            f"⏸ Bot paused 30 min before/after news"
        )

