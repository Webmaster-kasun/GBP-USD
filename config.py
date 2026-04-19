"""
config.py — Central config for GBP/USD trend-pullback bot (v2 Improved)

Sessions (SGT) — London + NY Overlap only:
  London Open  : 07:00 – 13:00
  NY Overlap   : 15:00 – 19:00

Removed vs v1:
  Asian Pre-London (06-08) — thin liquidity
  Late NY          (19-23) — low volume
"""

SYMBOL = "GBP_USD"

SESSIONS = [
    {"name": "London Open", "start": 7,  "end": 13, "max_spread": 2.0},
    {"name": "NY Overlap",  "start": 15, "end": 19, "max_spread": 2.2},
]

RISK = {
    "risk_per_trade":     0.5,   # % of account balance per trade
    "max_trades_per_day": 1,     # was 4 — quality-first mode
}

FILTERS = {
    "min_atr": 0.0005,           # 5 pips on M15 (was 0.0003 / 3 pips)
}
