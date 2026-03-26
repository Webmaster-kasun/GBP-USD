"""
OANDA — M1 Ultra-Scalp Bot
====================================
$10,000 demo | 4 pairs | 86,000 units

Trade specs:
  Size:    86,000 units
  SL:      3 pips => SGD ~34.83 (USD/JPY ~SGD 23)
  TP:      5 pips => SGD ~58.05 (USD/JPY ~SGD 39)
  Max dur: 15 minutes hard close
  4 pairs all TP = SGD ~232 per session

Sessions SGT:
  AUD/USD  : 6am-11am + 2pm-10pm
  EUR/USD  : 2pm-10pm  (extended — NY session added)
  GBP/USD  : 2pm-10pm  (new pair)
  USD/JPY  : 6am-11am + 8pm-10pm (new pair)

Telegram: EVENT-ONLY
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
    "AUD_USD": {
        "instrument":"AUD_USD","asset":"AUDUSD","emoji":"🦘",
        "pip":0.0001,"precision":5,
        "stop_pips":SL_PIPS,"tp_pips":TP_PIPS,
        "max_spread":1.2,
        "sessions":[(6,11),(14,22)],   # Asian + NY
    },
    "EUR_USD": {
        "instrument":"EUR_USD","asset":"EURUSD","emoji":"🇪🇺💵",
        "pip":0.0001,"precision":5,
        "stop_pips":SL_PIPS,"tp_pips":TP_PIPS,
        "max_spread":1.2,
        "sessions":[(14,22)],          # London + full NY
    },
    "GBP_USD": {
        "instrument":"GBP_USD","asset":"GBPUSD","emoji":"💷",
        "pip":0.0001,"precision":5,
        "stop_pips":SL_PIPS,"tp_pips":TP_PIPS,
        "max_spread":1.5,              # GBP slightly wider spread
        "sessions":[(14,22)],          # London + NY
    },
    "USD_JPY": {
        "instrument":"USD_JPY","asset":"USDJPY","emoji":"🇯🇵",
        "pip":0.01,"precision":3,      # JPY pip = 0.01
        "stop_pips":SL_PIPS,"tp_pips":TP_PIPS,
        "max_spread":1.5,
        "sessions":[(6,11),(20,22)],   # Asian + NY late
    },
}

DEFAULT_SETTINGS = {"signal_threshold":3,"demo_mode":True}

def load_settings():
    try:
        with open("settings.json") as f:
            DEFAULT_SETTINGS.update(json.load(f))
    except FileNotFoundError:
        with open("settings.json","w") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
    return DEFAULT_SETTINGS

def is_in_session(hour, cfg):
    return any(s <= hour < e for s, e in cfg["sessions"])

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
                wins    = today.get("wins",0)
                losses  = today.get("losses",0)
                if pnl < 0:
                    set_cooldown(today, name)
                    today["losses"]        = losses + 1
                    today["consec_losses"] = today.get("consec_losses",0) + 1
                    alert.send(
                        "🔴 SL HIT\n"+emoji+" "+name+"\n"
                        "Loss:  $"+str(round(pnl,2))+" USD\n"
                        "     ≈ SGD -"+str(abs(pnl_sgd))+"\n"
                        "⏳ Cooldown 30 min\n"
                        "W/L: "+str(wins)+"/"+str(today["losses"])
                    )
                else:
                    today["wins"]          = wins + 1
                    today["consec_losses"] = 0
                    alert.send(
                        "✅ TP HIT\n"+emoji+" "+name+"\n"
                        "Profit: $+"+str(round(pnl,2))+" USD\n"
                        "      ≈ SGD +"+str(pnl_sgd)+"\n"
                        "W/L: "+str(today["wins"])+"/"+str(losses)
                    )
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
    if not trader.login():
        alert.send("❌ Login FAILED!")
        return

    current_balance = trader.get_balance()

    trade_log = "trades_"+now.strftime("%Y%m%d")+".json"
    try:
        with open(trade_log) as f: today = json.load(f)
    except FileNotFoundError:
        today = {"trades":0,"start_balance":current_balance,"wins":0,"losses":0,
                 "consec_losses":0,"cooldowns":{},"open_times":{},"news_alerted":{}}
        with open(trade_log,"w") as f: json.dump(today, f, indent=2)
        log.info("New day! Balance: $"+str(round(current_balance,2)))

    realized_pnl = round(current_balance - today.get("start_balance", current_balance), 2)
    pl_sgd       = round(realized_pnl * USD_SGD, 2)
    pnl_emoji    = "✅" if realized_pnl >= 0 else "🔴"

    detect_sl_tp_hits(today, trader, trade_log, alert)

    # ── EOD close ────────────────────────────────────────────────────
    if hour == 22 and now.minute >= 55:
        closed = []
        for name in ASSETS:
            if trader.get_position(name):
                trader.close_position(name)
                closed.append(name)
        if closed:
            alert.send(
                "🔔 EOD Close\n"
                "Closed: "+", ".join(closed)+"\n"
                "Today:  $"+str(realized_pnl)+" "+pnl_emoji+" = SGD "+str(pl_sgd)+"\n"
                "W/L: "+str(today.get("wins",0))+"/"+str(today.get("losses",0))
            )
        return

    # ── 15-MIN HARD CLOSE ────────────────────────────────────────────
    for name in ASSETS:
        pos = trader.get_position(name)
        if not pos: continue
        try:
            trade_id, open_str = trader.get_open_trade_id(name)
            if not trade_id or not open_str: continue
            open_utc = datetime.fromisoformat(open_str.replace("Z","+00:00"))
            mins     = (datetime.now(pytz.utc) - open_utc).total_seconds() / 60
            log.info(name+": open "+str(round(mins,1))+" min")
            if mins >= MAX_DURATION:
                pnl     = trader.check_pnl(pos)
                pnl_sgd = round(pnl * USD_SGD, 2)
                trader.close_position(name)
                if name in today.get("open_times",{}):
                    del today["open_times"][name]
                    with open(trade_log,"w") as f: json.dump(today, f, indent=2)
                alert.send(
                    "⏰ 15-MIN TIMEOUT\n"
                    +ASSETS[name]["emoji"]+" "+name+"\n"
                    "Closed at "+str(round(mins,1))+" min\n"
                    "PnL: $"+str(round(pnl,2))+" USD "+("✅" if pnl>=0 else "🔴")+"\n"
                    "   ≈ SGD "+str(pnl_sgd)
                )
        except Exception as e:
            log.warning("Duration check "+name+": "+str(e))

    # ── SCAN + TRADE ──────────────────────────────────────────────────
    threshold = settings.get("signal_threshold", 3)

    for name, cfg in ASSETS.items():
        if not is_in_session(hour, cfg):
            log.info(name+": off-session"); continue

        pos = trader.get_position(name)
        if pos:
            pnl_sgd = round(trader.check_pnl(pos) * USD_SGD, 2)
            dirn    = "BUY" if int(float(pos.get("long",{}).get("units",0)))>0 else "SELL"
            log.info(name+": "+dirn+" open SGD "+str(pnl_sgd))
            continue

        if in_cooldown(today, name):
            cd = today.get("cooldowns",{}).get(name,"")
            try:
                remaining = int(30-(datetime.now(sg_tz)-datetime.fromisoformat(cd).replace(tzinfo=sg_tz)).total_seconds()/60)
            except: remaining = "?"
            log.info(name+": cooldown "+str(remaining)+"min"); continue

        price, bid, ask = trader.get_price(name)
        if price is None: log.warning(name+": price error"); continue

        spread = (ask - bid) / cfg["pip"]
        if spread > cfg["max_spread"]:
            log.info(name+": spread "+str(round(spread,2))+"p > "+str(cfg["max_spread"])+"p skip"); continue

        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            alert_key = name+"_news_"+now.strftime("%Y%m%d%H")
            if not today.get("news_alerted",{}).get(alert_key):
                if "news_alerted" not in today: today["news_alerted"] = {}
                today["news_alerted"][alert_key] = True
                with open(trade_log,"w") as f: json.dump(today, f, indent=2)
                alert.send("⚠️ NEWS BLOCK\n"+cfg["emoji"]+" "+name+"\n"+news_reason+"\nSkipping this hour")
            log.info(name+": news — "+news_reason); continue

        score, direction, details = signals.analyze(asset=cfg["asset"])
        log.info(name+": score="+str(score)+"/3 dir="+direction+" | "+details)

        if score < threshold or direction == "NONE":
            log.info(name+": no setup — waiting"); continue

        # ── Place trade ──────────────────────────────────────────────
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
                "🔄 NEW TRADE!\n"
                +cfg["emoji"]+" "+name+"\n"
                "Direction: "+direction+"\n"
                "Score:     3/3 ✅\n"
                "Size:      86,000 units\n"
                "Entry:     "+str(round(price, cfg["precision"]))+"\n"
                "SL:        "+str(SL_PIPS)+" pips ≈ SGD "+str(sl_sgd)+"\n"
                "TP:        "+str(TP_PIPS)+" pips ≈ SGD "+str(tp_sgd)+"\n"
                "Max Time:  15 min\n"
                "Spread:    "+str(round(spread,2))+"p\n"
                "Signals:   "+details
            )
            log.info(name+": PLACED "+direction+" SGD SL="+str(sl_sgd)+" TP="+str(tp_sgd))
        else:
            set_cooldown(today, name)
            with open(trade_log,"w") as f: json.dump(today, f, indent=2)
            log.warning(name+": order failed — "+str(result.get("error","")))

    log.info("Scan complete. Next in 60s.")

if __name__ == "__main__":
    log.info("🚀 Ultra-Scalp | 4 pairs | SL=3pip TP=5pip | 15min max")
    log.info("AUD/USD: 6-11am+2-10pm | EUR/USD: 2-10pm | GBP/USD: 2-10pm | USD/JPY: 6-11am+8-10pm")
    log.info("Best case: 4 pairs x SGD58 = SGD232/session")
    while True:
        try:
            run_bot()
        except Exception as e:
            log.error("Bot error: "+str(e))
        time.sleep(60)
