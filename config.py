"""
config.py — Multi-Pair Bot (v3.2)

Pairs:
  GBP/USD  Triple EMA Momentum    SL=15p  TP=25p  London 15:00-19:00 SGT
  EUR/USD  4-Layer Signal Engine  SL=15p  TP=25p  London 15:00-19:00 + NY 20:00-00:00 SGT
  AUD/USD  Asian Range Breakout   SL=15p  TP=25p  Asia 08:00-13:00 + London 15:00-17:00 SGT

Account: SGD
"""

PAIRS = {

    "GBP_USD": {
        "emoji":       "🇬🇧",
        "pip":         0.0001,
        "strategy":    "triple_ema",        # Triple EMA Momentum
        "sl_pips":     15,
        "tp_pips":     25,                  # was 20 — fixed
        "trade_size":  10000,
        "max_trades":  1,
        "max_gap":     50.0,                # gap filter — skip if gap >50 pips
        "sessions": [
            {"label": "London", "start": 15, "end": 19,
             "max_spread": 2.0, "hours": "15:00-19:00"},
        ],
    },

    "EUR_USD": {
        "emoji":       "🇪🇺",
        "pip":         0.0001,
        "strategy":    "four_layer",        # 4-Layer Signal Engine
        "sl_pips":     15,
        "tp_pips":     25,                  # was 20 — fixed
        "trade_size":  74000,
        "max_trades":  2,
        "max_gap":     50.0,
        "sessions": [
            {"label": "London", "start": 15, "end": 19,
             "max_spread": 1.2, "hours": "15:00-19:00"},
            {"label": "NY",     "start": 20, "end": 24,
             "max_spread": 1.5, "hours": "20:00-00:00"},
        ],
    },

    "AUD_USD": {
        "emoji":       "🇦🇺",
        "pip":         0.0001,
        "strategy":    "audusd_range",      # was triple_ema — fixed to proper AUD strategy
        "sl_pips":     15,
        "tp_pips":     25,
        "trade_size":  10000,
        "max_trades":  1,
        "max_gap":     50.0,
        "sessions": [
            {"label": "Asia",   "start":  8, "end": 13,
             "max_spread": 2.5, "hours": "08:00-13:00"},
            {"label": "London", "start": 15, "end": 17,
             "max_spread": 2.0, "hours": "15:00-17:00"},
        ],
    },

}

RISK = {
    "risk_per_trade": 0.5,
}

# 4-Layer signal engine params (EUR/USD)
FOUR_LAYER = {
    "signal_threshold":  4,
    "min_atr_pips":      2.5,
    "l2_break_buffer":   0.00150,
    "l2_expiry_minutes": 90,
    "rsi_buy_max":       65,
    "rsi_sell_min":      35,
    "ema_tol":           0.00020,
    "min_m5_range":      0.00010,
}

# Triple EMA params (GBP/USD)
TRIPLE_EMA = {
    "spans":        [5, 10, 20],
    "min_atr_pips": 5.0,
    "max_spread":   2.5,
}

# AUD/USD Asian Range params
AUD_RANGE = {
    "asian_start_sgt":      8,
    "asian_end_sgt":        13,
    "breakout_start_sgt":   15,
    "breakout_end_sgt":     17,
    "max_asian_range_pips": 40,
    "min_sweep_pips":        3,
    "min_atr_pips":          4.0,
}
