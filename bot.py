"""
OANDA — M1 Ultra-Scalp Bot
====================================
Target: SGD ~58 profit per trade | SGD ~35 max loss | 15-min max

ALL PAIRS → M1 Ultra-Scalp
  AUD/USD → Asian  6am-11am SGT
  EUR/GBP → London 2pm-7pm  SGT
  EUR/USD → London 2pm-6pm  SGT

Trade specs:
  Size:    86,000 units (0.86 lots)
  SL:      3 pips => SGD ~34.8
  TP:      5 pips => SGD ~58.1
  R:R:     1.67:1
  Max dur: 15 minutes hard close
  All 3 pairs hit TP => SGD ~174
"""

import os, json, time, logging, requests
from datetime import datetime
import pytz

from signals         import SignalEngine
from oanda_trader    import OandaTrader
from telegram_alert  import TelegramAlert
from calendar_filter import EconomicCalendar as CalendarFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

sg_tz   = pytz.timezone("Asia/Singapore")
signals = SignalEngine()

TRADE_SIZE   = 86000
SL_PIPS      = 3
TP_PIPS      = 5
MAX_DURATION = 15
USD_SGD      = 1.35

ASSETS = {
    "AUD_USD": {"instrument":"AUD_USD","asset":"AUDUSD","emoji":"🦘","strategy_label":"ULTRA-SCALP","max_score":3,"pip":0.0001,"precision":5,"stop_pips":SL_PIPS,"tp_pips":TP_PIPS,"session_start":6,"session_end":11},
    "EUR_GBP": {"instrument":"EUR_GBP","asset":"EURGBP","emoji":"🇪🇺","strategy_label":"ULTRA-SCALP","max_score":3,"pip":0.0001,"precision":5,"stop_pips":SL_PIPS,"tp_pips":TP_PIPS,"session_start":14,"session_end":19},
    "EUR_USD": {"instrument":"EUR_USD","asset":"EURUSD","emoji":"🇪🇺💵","strategy_label":"ULTRA-SCALP","max_score":3,"pip":0.0001,"precision":5,"stop_pips":SL_PIPS,"tp_pips":TP_PIPS,"session_start":14,"session_end":18},
}

DEFAULT_SETTINGS = {"signal_threshold":3,"demo_mode":True,"max_spread_pips":1.2}

def load_settings():
    try:
        with open("settings.json") as f:
            DEFAULT_SETTINGS.update(json.load(f))
    except FileNotFoundError:
        with open("settings.json","w") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
    return DEFAULT_SETTINGS

def is_in_session(hour, cfg): return cfg["session_start"] <= hour < cfg["session_end"]

def set_cooldown(today, name):
    if "cooldowns" not in today: today["cooldowns"] = {}
    today["cooldowns"][name] = datetime.now(sg_tz).isoformat()
    log.info(name + " cooldown 30 min")

def in_cooldown(today, name):
    cd = today.get("cooldowns",{}).get(name)
    if not cd: return False
    try:
        elapsed = (datetime.now(sg_tz) - datetime.fromisoformat(cd).replace(tzinfo=sg_tz)).total_seconds()/60
        return elapsed < 30
    except: return False

def detect_sl_tp_hits(today, trader, trade_log, alert):
    if "open_times" not in today: return
    for name in list(today["open_times"].keys()):
        if trader.get_position(name): continue
        try:
            url  = trader.base_url+"/v3/accounts/"+trader.account_id+"/trades?state=CLOSED&instrument="+name+"&count=1"
            data = requests.get(url, headers=trader.headers, timeout=10).json().get("trades",[])
            if data:
                pnl     = float(data[0].get("realizedPL","0"))
                pnl_sgd = round(pnl * USD_SGD, 2)
                emoji   = ASSETS.get(name,{}).get("emoji","")
                if pnl < 0:
                    set_cooldown(today, name)
                    today["losses"]        = today.get("losses",0)+1
                    today["consec_losses"] = today.get("consec_losses",0)+1
                    alert.send("🔴 SL HIT\n"+emoji+" "+name+"\nLoss: $"+str(round(pnl,2))+" USD\n     ≈ SGD "+str(abs(pnl_sgd))+"\n⏳ Cooldown 30 min\nW/L: "+str(today.get("wins",0))+"/"+str(today.get("losses",0)))
                else:
                    today["wins"]          = today.get("wins",0)+1
                    today["consec_losses"] = 0
                    alert.send("✅ TP HIT\n"+emoji+" "+name+"\nProfit: $+"+str(round(pnl,2))+" USD\n      ≈ SGD +"+str(pnl_sgd)+"\nW/L: "+str(today.get("wins",0))+"/"+str(today.get("losses",0)))
        except Exception as e:
            log.warning("SL/TP detect error "+name+": "+str(e))
        del today["open_times"][name]
        with open(trade_log,"w") as f: json.dump(today, f, indent=2)

def run_bot():
    settings = load_settings()
    now      = datetime.now(sg_tz)
    hour     = now.hour
    alert    = TelegramAlert()
    calendar = CalendarFilter()

    log.info("Scan at "+now.strftime("%H:%M:%S SGT"))

    if now.weekday() == 5: log.info("Saturday — silent"); return
    if now.weekday() == 6 and hour < 5: log.info("Sunday early — silent"); return

    active = [n for n,c in ASSETS.items() if is_in_session(hour,c)]
    if not active: log.info("No active sessions at "+str(hour)+"h SGT"); return

    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login(): alert.send("Login FAILED!"); return

    current_balance = trader.get_balance()

    trade_log = "trades_"+now.strftime("%Y%m%d")+".json"
    try:
        with open(trade_log) as f: today = json.load(f)
    except FileNotFoundError:
        today = {"trades":0,"start_balance":current_balance,"wins":0,"losses":0,"consec_losses":0,"cooldowns":{},"open_times":{}}
        with open(trade_log,"w") as f: json.dump(today, f, indent=2)
        log.info("New day! Balance: $"+str(round(current_balance,2)))

    start_balance = today.get("start_balance", current_balance)
    realized_pnl  = round(current_balance - start_balance, 2)
    pnl_emoji     = "✅" if realized_pnl >= 0 else "🔴"
    pl_sgd        = round(realized_pnl * USD_SGD, 2)

    open_pnl = 0.0
    for name in ASSETS:
        pos = trader.get_position(name)
        if pos: open_pnl += trader.check_pnl(pos)
    open_pnl = round(open_pnl, 2)

    detect_sl_tp_hits(today, trader, trade_log, alert)

    # EOD close
    if hour == 22 and now.minute >= 55:
        closed = []
        for name in ASSETS:
            if trader.get_position(name): trader.close_position(name); closed.append(name)
        if closed: alert.send("🔔 EOD Close\n"+", ".join(closed)+"\nRealized: $"+str(realized_pnl)+" "+pnl_emoji)
        return

    # ── 15-MIN HARD CLOSE ────────────────────────────────────────────
    for name in ASSETS:
        pos = trader.get_position(name)
        if not pos: continue
        try:
            tid      = pos.get("id") or pos.get("tradeID")
            t_url    = trader.base_url+"/v3/accounts/"+trader.account_id+"/trades/"+str(tid)
            open_str = requests.get(t_url, headers=trader.headers, timeout=10).json()["trade"]["openTime"]
            open_utc = datetime.fromisoformat(open_str.replace("Z","+00:00"))
            mins     = (datetime.now(pytz.utc) - open_utc).total_seconds() / 60
            if mins >= MAX_DURATION:
                pnl     = trader.check_pnl(pos)
                pnl_sgd = round(pnl * USD_SGD, 2)
                trader.close_position(name)
                if name in today.get("open_times",{}):
                    del today["open_times"][name]
                    with open(trade_log,"w") as f: json.dump(today, f, indent=2)
                alert.send("⏰ 15-MIN LIMIT\n"+ASSETS[name]["emoji"]+" "+name+"\nClosed at "+str(round(mins,1))+" min\nPnL: $"+str(round(pnl,2))+" USD "+("✅" if pnl>=0 else "🔴")+"\n   ≈ SGD "+str(pnl_sgd))
                log.info(name+" force-closed at "+str(round(mins,1))+" min")
        except Exception as e:
            log.warning("Duration check "+name+": "+str(e))

    # ── SCAN + TRADE ──────────────────────────────────────────────────
    scan_results = []
    threshold    = settings.get("signal_threshold", 3)

    for name, cfg in ASSETS.items():
        if not is_in_session(hour, cfg):
            scan_results.append(cfg["emoji"]+" "+name+": off-session"); continue

        pos = trader.get_position(name)
        if pos:
            pnl     = trader.check_pnl(pos)
            pnl_sgd = round(pnl * USD_SGD, 2)
            dirn    = "BUY" if int(float(pos.get("long",{}).get("units",0)))>0 else "SELL"
            scan_results.append(cfg["emoji"]+" "+name+": "+dirn+" open "+("📈" if pnl>=0 else "📉")+" SGD "+str(pnl_sgd))
            continue

        if in_cooldown(today, name):
            cd = today.get("cooldowns",{}).get(name,"")
            try:
                remaining = int(30-(datetime.now(sg_tz)-datetime.fromisoformat(cd).replace(tzinfo=sg_tz)).total_seconds()/60)
            except: remaining = "?"
            scan_results.append(cfg["emoji"]+" "+name+": ⏳ cooldown "+str(remaining)+"min"); continue

        price, bid, ask = trader.get_price(name)
        if price is None: scan_results.append(cfg["emoji"]+" "+name+": price error"); continue
        spread = (ask - bid) / cfg["pip"]
        if spread > settings.get("max_spread_pips", 1.2):
            scan_results.append(cfg["emoji"]+" "+name+": spread "+str(round(spread,1))+"p skip"); continue

        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            scan_results.append(cfg["emoji"]+" "+name+": ⚠️ NEWS "+news_reason); continue

        score, direction, details = signals.analyze(asset=cfg["asset"])
        if score < threshold or direction == "NONE":
            scan_results.append(cfg["emoji"]+" "+name+": "+str(score)+"/3 no setup"); continue

        # Place trade
        sl_sgd = round(TRADE_SIZE * SL_PIPS * cfg["pip"] * USD_SGD, 2)
        tp_sgd = round(TRADE_SIZE * TP_PIPS * cfg["pip"] * USD_SGD, 2)

        result = trader.place_order(instrument=name, direction=direction, size=TRADE_SIZE,
                                    stop_distance=SL_PIPS, limit_distance=TP_PIPS)
        if result["success"]:
            today["trades"] = today.get("trades",0)+1
            if "open_times" not in today: today["open_times"] = {}
            today["open_times"][name] = now.isoformat()
            with open(trade_log,"w") as f: json.dump(today, f, indent=2)
            price, _, _ = trader.get_price(name)
            alert.send(
                "🔄 NEW TRADE!\n"+cfg["emoji"]+" "+name+"\n"
                "Direction: "+direction+"\nScore:     3/3 ✅\n"
                "Size:      86,000 units\n"
                "Entry:     "+str(round(price, cfg["precision"]))+"\n"
                "SL:        "+str(SL_PIPS)+" pips ≈ SGD "+str(sl_sgd)+"\n"
                "TP:        "+str(TP_PIPS)+" pips ≈ SGD "+str(tp_sgd)+"\n"
                "Max Time:  15 min\nSpread:    "+str(round(spread,1))+"p\n"
                "Signals:   "+details
            )
            scan_results.append(cfg["emoji"]+" "+name+": "+direction+" 3/3 ✅ PLACED!")
        else:
            set_cooldown(today, name)
            with open(trade_log,"w") as f: json.dump(today, f, indent=2)
            scan_results.append(cfg["emoji"]+" "+name+": order failed")

    # ── SUMMARY ──────────────────────────────────────────────────────
    wins   = today.get("wins",0)
    losses = today.get("losses",0)

    if 6 <= hour < 11:    session = "Asian 🇯🇵"
    elif 14 <= hour < 18: session = "London 🇬🇧"
    elif 18 <= hour < 23: session = "NY 🇺🇸"
    else:                 session = "Off-hours"

    target_msg = ("🎯 SGD TARGET HIT! "+str(round(pl_sgd,0)) if pl_sgd >= 58
                  else ("Profit SGD +"+str(pl_sgd) if pl_sgd > 0
                  else ("Loss   SGD -"+str(abs(pl_sgd)) if pl_sgd < 0
                  else "Waiting for signal...")))

    alert.send(
        "🔄 Scan | ULTRA-SCALP\n"
        "Time:    "+now.strftime("%H:%M SGT")+" | "+session+"\n"
        "Balance: $"+str(round(current_balance,2))+" USD\n"
        "Today:   $"+str(realized_pnl)+" "+pnl_emoji+" = SGD "+str(pl_sgd)+"\n"
        "Open:    $"+str(open_pnl)+"\n"
        +target_msg+"\n"
        "Trades: "+str(today.get("trades",0))+" | W/L: "+str(wins)+"/"+str(losses)+"\n"
        "Config: 86k units | SL=3p(SGD35) | TP=5p(SGD58) | ≤15min\n"
        "Target: 3x TP = SGD 174/session\n"
        "─────────────────────────\n"
        +"\n".join(scan_results)
    )

if __name__ == "__main__":
    log.info("🚀 Ultra-Scalp | SL=3pip(SGD35) TP=5pip(SGD58) | 15min max")
    log.info("3 pairs x SGD58 = SGD174 target per session")
    log.info("AUD/USD 6-11am | EUR/GBP 2-7pm | EUR/USD 2-6pm SGT")
    while True:
        try:
            run_bot()
        except Exception as e:
            log.error("Bot error: "+str(e))
        time.sleep(60)
