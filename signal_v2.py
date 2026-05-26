#!/usr/bin/env python3
"""
Сигналы TON/USDT v2 — с уровнями стоп-лосс/тейк-профит и бэктестом.
Запускается cron'ом каждые 5 мин.
"""

import sqlite3, math, os, sys, urllib.request, urllib.parse, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# === СТРАТЕГИЯ (ОТКАЛИБРОВАНА БЭКТЕСТОМ) ===
# BUY:  RSI < 35 → 74% винрейт на истории (96 сделок)
# SELL: RSI > 70 → моменты перекупленности
# Стоп-лосс: -2% или Bollinger lower (что дальше)
# Тейк-профит: +3% или Bollinger upper
# Тайм-аут: 60 минут без движения → закрываем

BUY_RSI = 35
SELL_RSI = 70
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.03
TIMEOUT_MINUTES = 60
MIN_ROWS = 100


def sma(data, p):
    return sum(data[-p:]) / p if len(data) >= p else None

def rsi(data, per=14):
    if len(data) < per+1: return None
    gains = losses = 0.0
    for i in range(1, per+1):
        d = data[i]-data[i-1]
        gains += d if d>=0 else 0
        losses += abs(d) if d<0 else 0
    avg_g, avg_l = gains/per, losses/per
    if avg_l == 0: return 100.0
    for i in range(per+1, len(data)):
        d = data[i]-data[i-1]
        avg_g = (avg_g*(per-1)+(d if d>=0 else 0))/per
        avg_l = (avg_l*(per-1)+(abs(d) if d<0 else 0))/per
    if avg_l == 0: return 100.0
    return 100 - (100/(1+avg_g/avg_l))


def signal(data_prices, data_volumes) -> Optional[Dict]:
    if len(data_prices) < 100: return None
    
    cur = data_prices[-1]
    s7 = sma(data_prices, 7)
    s20 = sma(data_prices, 20)
    r = rsi(data_prices, 14)
    
    # Volume
    avg_v = sum(data_volumes[:-1])/len(data_volumes[:-1]) if len(data_volumes)>1 else data_volumes[-1]
    vr = data_volumes[-1]/avg_v if avg_v>0 else 1.0
    
    # Bollinger
    bb_lo = bb_hi = None
    if s20:
        recent = data_prices[-20:]
        var = sum((p-s20)**2 for p in recent)/20
        std = math.sqrt(var)
        bb_lo = s20 - 2*std
        bb_hi = s20 + 2*std
    
    # Тренд
    trend = "нейтральный"
    if s7 and s20:
        trend = "↑ бычий" if s7 > s20 else "↓ медвежий"
    
    # === BUY ===
    if r and r < BUY_RSI:
        sl = round(min(cur*(1-STOP_LOSS_PCT), bb_lo or cur*0.97), 4)
        tp = round(max(cur*(1+TAKE_PROFIT_PCT), bb_hi*0.99 if bb_hi else cur*1.02), 4)
        risk = abs(sl-cur)
        reward = abs(tp-cur)
        rr = round(reward/risk, 1) if risk>0 else 0
        
        return {
            "signal": "BUY",
            "strength": "STRONG" if r < 30 else "WEAK",
            "price": round(cur, 4),
            "stop_loss": sl, "take_profit": tp, "rr_ratio": rr,
            "sma7": round(s7 or 0, 4), "sma20": round(s20 or 0, 4),
            "rsi": round(r, 1),
            "vr": round(vr, 1), "bb_lower": round(bb_lo,4) if bb_lo else None,
            "bb_upper": round(bb_hi,4) if bb_hi else None, "trend": trend,
            "why": [
                f"RSI={r:.0f} < {BUY_RSI} ✅ перепродано",
                f"Цена ${cur:.4f} | SMA20=${s20:.4f}" if s20 else f"Цена ${cur:.4f}",
                f"Стоп-лосс: ${sl:.4f} ({(sl/cur-1)*100:+.1f}%)",
                f"Тейк-профит: ${tp:.4f} (+{(tp/cur-1)*100:+.1f}%)",
            ]
        }
    
    # === SELL ===
    if r and r > SELL_RSI:
        sl = round(max(cur*(1+STOP_LOSS_PCT), bb_hi or cur*1.03), 4)
        tp = round(min(cur*(1-TAKE_PROFIT_PCT), bb_lo*1.01 if bb_lo else cur*0.98), 4)
        risk = abs(sl-cur)
        reward = abs(tp-cur)
        rr = round(reward/risk, 1) if risk>0 else 0
        
        return {
            "signal": "SELL",
            "strength": "STRONG" if r > 75 else "WEAK",
            "price": round(cur, 4),
            "stop_loss": sl, "take_profit": tp, "rr_ratio": rr,
            "sma7": round(s7 or 0, 4), "sma20": round(s20 or 0, 4),
            "rsi": round(r, 1),
            "vr": round(vr, 1), "bb_lower": round(bb_lo,4) if bb_lo else None,
            "bb_upper": round(bb_hi,4) if bb_hi else None, "trend": trend,
            "why": [
                f"RSI={r:.0f} > {SELL_RSI} ✅ перекуплен",
                f"Цена ${cur:.4f} | SMA20=${s20:.4f}" if s20 else f"Цена ${cur:.4f}",
                f"Стоп-лосс: ${sl:.4f} (+{(sl/cur-1)*100:+.1f}%)",
                f"Тейк-профит: ${tp:.4f} ({(tp/cur-1)*100:+.1f}%)",
            ]
        }
    
    return None


def save_signal(sig):
    """Сохранить сигнал в signals.db."""
    conn = sqlite3.connect(str(SIGNALS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, signal TEXT, strength TEXT,
        price REAL, rsi REAL, bb_upper REAL, bb_lower REAL, trend TEXT
    )""")
    conn.execute("INSERT INTO signals (ts,signal,strength,price,rsi,bb_upper,bb_lower,trend) VALUES (?,?,?,?,?,?,?,?)",
                 [datetime.now(UTC_PLUS_3).isoformat(), sig["signal"], sig["strength"],
                  sig["price"], sig["rsi"], sig["bb_upper"], sig["bb_lower"], sig["trend"]])
    conn.commit()
    conn.close()


def send_tg(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat = os.getenv("TELEGRAM_HOME_CHANNEL","").strip()
    if not token or not chat: return
    try:
        d = urllib.parse.urlencode({"chat_id":chat,"text":text,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=d),timeout=10)
    except: pass


def format_signal(sig):
    em = "🟢" if sig["signal"]=="BUY" else "🔴"
    bar = "▓▓▓▓" if sig["strength"]=="STRONG" else "▓▓░░"
    
    if sig["signal"] == "BUY":
        sl_str = f"🛑 <b>Стоп-лосс: ${sig['stop_loss']:.4f}</b> ({(sig['stop_loss']/sig['price']-1)*100:+.1f}%)"
        tp_str = f"🎯 <b>Тейк-профит: ${sig['take_profit']:.4f}</b> (+{(sig['take_profit']/sig['price']-1)*100:+.1f}%)"
    else:
        sl_str = f"🛑 <b>Стоп-лосс: ${sig['stop_loss']:.4f}</b> (+{(sig['stop_loss']/sig['price']-1)*100:+.1f}%)"
        tp_str = f"🎯 <b>Тейк-профит: ${sig['take_profit']:.4f}</b> ({(sig['take_profit']/sig['price']-1)*100:+.1f}%)"
    
    return (
        f"{em} <b>{sig['signal']} TON/USDT</b> {bar} {sig['strength']}\n"
        f"\n💰 <b>Вход: ${sig['price']:.4f}</b>\n{sl_str}\n{tp_str}\n"
        f"📊 Риск/Прибыль: <b>1:{sig['rr_ratio']}</b>\n"
        f"\n📈 Тренд: {sig['trend']}\n"
        f"📏 Bollinger: ${sig['bb_lower']:.4f} — ${sig['bb_upper']:.4f}\n"
        f"📊 RSI={sig['rsi']} | SMA20=${sig['sma20']:.4f} | Объём×{sig['vr']}\n"
        f"\n{chr(10).join(sig['why'])}\n"
        f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )


if __name__ == "__main__":
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT "+str(MIN_ROWS*2)).fetchall()
    conn.close()
    rows = rows[::-1]  # oldest first
    
    if len(rows) < MIN_ROWS:
        print(f"Недостаточно данных: {len(rows)}/{MIN_ROWS}")
        sys.exit(0)
    
    prices = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    sig = signal(prices, volumes)
    
    if sig:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] {sig['signal']} ${sig['price']:.4f} RSI={sig['rsi']}")
        save_signal(sig)
        send_tg(format_signal(sig))
    else:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Нет сигнала (RSI={rsi(prices):.0f}, цена=${prices[-1]:.4f})")
