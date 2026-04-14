SYMBOL = "GBPUSD"

SESSIONS = [
    {"name": "London Open", "start": 8, "end": 12, "max_spread": 2.0},
    {"name": "NY Overlap", "start": 15, "end": 18, "max_spread": 2.2},
]

RISK = {
    "risk_per_trade": 0.5,
    "max_trades_per_day": 3
}

FILTERS = {
    "min_atr": 0.0008
}
