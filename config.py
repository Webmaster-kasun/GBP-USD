"""
config.py
FIX-01: Session 2 (NY Overlap) extended 17:00 → 18:00 SGT
        Today's chart showed the biggest move was 15:00–18:00 SGT — bot was blind to it
"""

SYMBOL = "GBP_USD"

SESSIONS = [
    {"name": "London Open", "start": 8,  "end": 12, "max_spread": 2.0},
    {"name": "NY Overlap",  "start": 15, "end": 18, "max_spread": 2.2},  # FIX-01: was end=17
]

RISK = {
    "risk_per_trade": 0.5,       # 0.5% of balance per trade
    "max_trades_per_day": 2
}

FILTERS = {
    "min_atr": 0.0005             # FIX: was 0.0008 — now matches signals.py
}
