"""
config.py
SESSIONS updated per user request:
  - Asian Pre-London:  06:00–08:00 SGT (new)
  - London Open:       07:00–13:00 SGT (was 08:00–12:00)
  - NY Overlap:        15:00–19:00 SGT (was 15:00–18:00)
  - Late NY:           19:00–23:30 SGT (new — represented as end=23, last cycle at 23:00)
  - Max trades/day:    4 (was 2)
"""

SYMBOL = "GBP_USD"

SESSIONS = [
    {"name": "Asian Pre-London", "start": 6,  "end": 8,  "max_spread": 1.8},
    {"name": "London Open",      "start": 7,  "end": 13, "max_spread": 2.0},
    {"name": "NY Overlap",       "start": 15, "end": 19, "max_spread": 2.2},
    {"name": "Late NY",          "start": 19, "end": 23, "max_spread": 2.5},  # 23:00 last cycle covers until ~23:30
]

RISK = {
    "risk_per_trade": 0.5,       # 0.5% of balance per trade
    "max_trades_per_day": 4      # was 2
}

FILTERS = {
    "min_atr": 0.0005
}
