# 🥇 BETAX Gold Bot — Supply & Demand Zone Strategy

> Rebuilt from **312 real BETAX trader trades** (Jan 2025 – Apr 2026).  
> Win rate: **61.9%** | Avg RR: **2.61×** | Instrument: **XAUUSD only**

---

## What Changed from the Original Bot

| Feature | Original (CPR Breakout) | BETAX (Supply & Demand) |
|---|---|---|
| Entry method | Market order on CPR breakout | Limit zone window (3-4 pip) |
| Signal engine | CPR + EMA + RSI + Volume | S/D Zone + Rejection wick |
| SL model | ATR × 1.5, capped 800–3000p | Price-adaptive (70p→100p with Gold) |
| TP model | Mirror SL (1:1 RR) | TP1=1.5× SL, TP2=2.5× SL |
| Score system | 7-point CPR scoring | 8-point zone scoring |
| Direction bias | Neutral | LONG bias (mirrors BETAX 66% BUY WR) |
| Max trades/day | 999 | 6 (BETAX max observed) |
| Consec loss stop | 999 | 3 (then flat for the day) |
| Trade file prefix | `trades_YYYYMMDD.json` | `betax_YYYYMMDD.json` |

---

## Files Changed

```
signals.py   ← FULLY REWRITTEN (Supply & Demand zone engine)
bot.py       ← REWRITTEN (BETAX logic, SL/TP model, Telegram alerts)
settings.json← UPDATED (new defaults matching BETAX behaviour)
```

### Files Kept Unchanged
```
oanda_trader.py    ← same (OANDA API executor)
telegram_alert.py  ← same (Telegram notifications)
calendar_filter.py ← same (economic calendar filter)
requirements.txt   ← same
.github/workflows/ ← same (Railway/cron deploy)
```

---

## BETAX Strategy — Decoded Rules

### 1. Zone Identification (Check 1 — 0-2 pts)
Finds swing highs/lows + round number levels ($XX00/$XX50) on H4 charts.  
- Price AT zone (≤50 pips) → **2 pts**  
- Price NEAR zone (≤150 pips) → **1 pt**  
- No zone → **skip trade entirely**

### 2. HTF Trend Alignment (Check 2 — 0-2 pts)
Uses D1 EMA10/EMA20 + H4 EMA20/EMA50.  
- Both D1 and H4 agree → **2 pts**  
- One agrees → **1 pt**  
- Both against + London/NY → **BLOCKED** (H4 hard block, same as original)  
- Both against + Asian → **-1 pt penalty** (same as original)

### 3. Zone Rejection — MANDATORY (Check 3 — 0-1 pt)
The trader **never** enters without a wick rejection.  
Checks M15 first, M30 as fallback.  
- BUY: lower wick ≥ 40% of candle range  
- SELL: upper wick ≥ 40% of candle range  
- **No rejection = NO TRADE** (mandatory gate, not just -1pt)

### 4. RSI Confluence (Check 4 — 0-1 pt)
- BUY: RSI > 50 (relaxed — BETAX buys pullbacks, not breakouts)  
- SELL: RSI < 50  

### 5. Structure Break (Check 5 — 0-1 pt)
Confirms momentum: price broke last H1 swing high (BUY) or swing low (SELL).

### 6. Not Overextended from Zone (Check 6 — 0-1 pt)
- Zone distance ≤ 200 pips → **1 pt**  
- Zone distance > 200 pips → **0 pts** (chasing)

---

## SL/TP Model (Adaptive to Gold Price)

```
Gold ~$2750 (early 2025) → SL floor = 50 pips
Gold ~$3000              → SL floor = 60 pips
Gold ~$3500              → SL floor = 70 pips  (BETAX standardized mid-2025)
Gold ~$4000              → SL floor = 90 pips
Gold ~$4500+             → SL floor = 100 pips
Gold ~$5000+             → SL floor = 100 pips

Actual SL = max(price_floor, ATR × 1.2)  — capped at 120 pips

TP1 = SL × 1.5  → BOT sends alert: manually move SL to breakeven
TP2 = SL × 2.5  → Hard limit order on broker (full close)
RR  = 2.5:1 minimum enforced
```

> **⚠️ TP1 Breakeven Note:**  
> OANDA doesn't support trailing stops on practice accounts natively.  
> The bot places TP2 as the hard TP order. After TP1 pips are hit, you must  
> **manually move SL to entry price** via OANDA dashboard.  
> The Telegram alert tells you the TP1 level to watch.

---

## Score Thresholds

| Session | Need to Trade | Max Trades |
|---|---|---|
| Asian (9am-1pm SGT) | 4/8 | 2 |
| London (2pm-7pm SGT) | 5/8 | 4 |
| NY (8pm-11pm SGT) | 5/8 | 4 |
| Daily total | — | 6 |

---

## Key Protections Kept from Original

- ✅ Next-session win lock (FIX 13)
- ✅ Same-candle M30 duplicate lock
- ✅ Re-entry guard (no chasing same zone)
- ✅ News calendar pause
- ✅ Spread filter
- ✅ Margin check before order
- ✅ Weekend guard
- ✅ Daily summary at 11pm SGT
- 🆕 Consecutive loss stop (3 losses = done for day)

---

## Setup (same as original)

```bash
# Environment variables required:
OANDA_API_KEY=your_key_here
OANDA_ACCOUNT_ID=your_account_id
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Run locally:
pip install -r requirements.txt
python bot.py

# Deploy: push to GitHub — Railway/cron picks up automatically
```

### settings.json — Key Parameters

```json
{
  "demo_mode": true,          // ← set false for live trading
  "max_trades_day": 6,        // BETAX never exceeded 6/day
  "max_consec_losses": 3,     // Stop after 3 straight losses
  "signal_threshold": 5,      // 5/8 to trade (London/NY)
  "signal_threshold_asian": 4,// 4/8 to trade (Asian)
  "max_trades_asian": 2,      // Max 2 Asian trades
  "max_trades_main": 4,       // Max 4 London/NY trades
  "max_spread_gold": 50,      // Skip if spread > 50 pips
  "tp1_be_move": true         // Reminds you to move SL to BE at TP1
}
```

---

## Telegram Alert Format

```
🥇 BETAX ZONE TRADE! DEMO
═══════════════════════
Direction:  BUY
Score:      6/8
Entry zone: 3134.00→3130.00
Entry px:   3134.22
Size:       12 units
ATR:        187p
SL:         90p = $9.72
TP1:        135p → move SL to BE
TP2:        225p = $24.30
R:R (TP2):  1:2.5
Spread:     12.3p
Trade #2/6
Session:    London Open — BEST for zone breakouts!
═══════════════════════
📌 Manually move SL to entry after TP1 (135p) hit
--- Signals ---
✅ ATR=187p — healthy zone volatility
✅✅ Price AT zone 3130.0 (demand) dist=42p (2 pts)
✅✅ D1=BUY H4=BUY both align with BUY (2 pts)
✅ M15 lower wick=62% — demand rejection ✅ (1 pt)
✅ RSI=54.2 > 50 — momentum supports BUY (1 pt)
✅ Structure break: price 3134.22 > swing high 3128.00 ✅ (1 pt)
✅ Zone dist=42p — fresh zone entry (1 pt)
Zone: 3130.0 (demand)
Entry window: 3134.22→3130.22
```

---

## Performance Expectations (based on BETAX data)

| Metric | BETAX Actual | Bot Target |
|---|---|---|
| Win rate | 61.9% | 55-65% |
| Avg RR | 2.61× | 2.5× (TP2 hard) |
| Best month WR | 78% (Nov 2025) | — |
| Worst month WR | 36% (Sep 2025, choppy) | — |
| Max trades/day | 6 | 6 (capped) |
| Instrument | XAUUSD only | XAUUSD only |

> The bot replicates BETAX's **zone identification + rejection entry logic**.  
> Human discretion (reading macro context, news, Gold trend bias) gave BETAX  
> his edge in Sep/Oct 2025. The bot uses ATR gates and calendar filters as  
> the closest mechanical equivalent.
