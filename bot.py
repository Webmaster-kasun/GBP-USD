"""
bot.py — GBP/USD Triple EMA Momentum Bot (v3.1)
================================================

CHANGE v3.1: Removed SGT hardcoded session window.
All time logic now uses UTC — runs correctly on any server
(Railway, GitHub Actions, VPS in any region).

Entry window: 06:00-08:00 UTC (London Open, both GMT + BST seasons).
Max 1 trade per day. Resets at 00:00 UTC daily.
"""

import logging
from datetime import datetime
import pytz
import signals
import config
from oanda_trader   import OandaTrader
from telegram_alert import TelegramAlert

log = logging.getLogger(__name__)
UTC = pytz.utc

ASSETS = {
    'GBP_USD': {
        'sl_pips':    15,
        'tp_pips':    30,
        'max_trades': 1,
        'max_spread': 2.5,
    }
}


def run_bot(state):
    instrument = 'GBP_USD'
    asset_cfg  = ASSETS[instrument]

    now_utc = datetime.now(UTC)

    # Max 1 trade per day
    if state.get('trades', 0) >= asset_cfg['max_trades']:
        log.info(f'[{instrument}] 1 trade already taken today — done')
        return

    # One trade per window per day
    window_key   = f"{instrument}_london"
    windows_used = state.setdefault('windows_used', {})
    if windows_used.get(window_key):
        log.info(f'[{instrument}] London window already traded today')
        return

    try:
        trader = OandaTrader(demo=True)
        if not trader.login():
            log.warning(f'[{instrument}] OANDA login failed')
            return

        if trader.get_position(instrument):
            log.info(f'[{instrument}] Position already open — skipping')
            return

        mid, bid, ask = trader.get_price(instrument)
        if mid is None:
            log.warning(f'[{instrument}] Could not fetch price')
            return

        spread_pips = round((ask - bid) / 0.0001, 1)
        log.info(f'[{instrument}] Price={mid:.5f} Spread={spread_pips}p UTC={utc_hour:02d}:xx')

        df_h1  = trader.get_candles(instrument, 'H1',  50)
        df_m15 = trader.get_candles(instrument, 'M15', 30)

        if df_h1 is None or df_m15 is None:
            log.warning(f'[{instrument}] Candle fetch failed')
            return

        signal = signals.get_signal(
            df_h1, df_m15,
            spread_pips = spread_pips,
            tp_pips     = asset_cfg['tp_pips'],
            sl_pips     = asset_cfg['sl_pips'],
        )

        if signal is None:
            log.info(f'[{instrument}] No signal — triple EMA not aligned or outside window')
            return

        direction = signal['direction']
        sl_pips   = asset_cfg['sl_pips']
        tp_pips   = asset_cfg['tp_pips']

        balance  = trader.get_balance()
        risk_amt = balance * (config.RISK['risk_per_trade'] / 100)
        size     = max(1000, int((risk_amt / sl_pips) * 10000))
        size     = min(size, 50000)

        log.info(
            f'[{instrument}] >>> {direction}'
            f' | SL={sl_pips}p TP={tp_pips}p (2:1 RR)'
            f' | size={size}'
        )

        result = trader.place_order(
            instrument     = instrument,
            direction      = direction,
            size           = size,
            stop_distance  = sl_pips,
            limit_distance = tp_pips,
        )

        if result.get('success'):
            state['trades']          = state.get('trades', 0) + 1
            windows_used[window_key] = True
            log.info(f'[{instrument}] Trade placed! ID={result.get("trade_id", "?")}')

            TelegramAlert().send(
                f'Trade Opened!\n'
                f'Pair:      GBP/USD\n'
                f'Direction: {direction}\n'
                f'Strategy:  Triple EMA Momentum\n'
                f'SL: {sl_pips}p | TP: {tp_pips}p | RR: 2:1\n'
                f'Size:      {size} units\n'
                f'Balance:   ${balance:.2f}\n'
                f'Time:      {now_utc.strftime("%H:%M UTC")}'
            )
        else:
            log.error(f'[{instrument}] Order failed: {result.get("error")}')

    except Exception as e:
        log.error(f'[{instrument}] run_bot error: {e}', exc_info=True)
