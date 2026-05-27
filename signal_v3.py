#!/usr/bin/env python3
"""
Стратегия v3 — ФЬЮЧЕРСЫ TON/USDT x3
Баланс \$100 → маржа \$5.20/сделку → стоп-лосс/тейк-профит по формуле.
Запускается cron'ом каждые 5 мин.
"""

import sqlite3, math, os, sys, urllib.request, urllib.parse, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
TRADES_DB = Path("/home/oleg/workspace/crypto-ton/trades.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# ═══════════════════════════════════════════
#  ПАРАМЕТРЫ СТРАТЕГИИ (ФЬЮЧЕРСЫ x3)
# ═══════════════════════════════════════════
LEVERAGE = 3
INITIAL_BALANCE = 100.0
MARGIN_PER_TRADE = 5.20      # фикс маржа (5.2% от \$100)
RISK_PCT = 0.02              # рискуем 2% баланса = \$2
RISK_REWARD_RATIO = 2.0      # прибыль:риск = 2:1

# Индикаторы
BUY_RSI = 35
SELL_RSI = 70
MIN_ROWS = 100


def get_current_balance() -> float:
    """Текущий баланс = \$100 + P&L из trades.db."""
    conn = sqlite3.connect(str(TRADES_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, type TEXT, entry REAL, exit REAL,
        pnl REAL, pnl_pct REAL, exit_reason TEXT
    )""")
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
    conn.close()
    return INITIAL_BALANCE + total_pnl


def get_trade_count() -> int:
    conn = sqlite3.connect(str(TRADES_DB))
    c = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()
    return c


def get_consecutive_losses() -> int:
    conn = sqlite3.connect(str(TRADES_DB))
    rows = conn.execute("SELECT pnl FROM trades ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    count = 0
    for (pnl,) in rows:
        if pnl < 0:
            count += 1
        else:
            break
    return count


def calc_trade_params(current_price: float, balance: float):
    """
    Рассчитать стоп-лосс и тейк-профит для фьючерсов x3.
    
    margin = \$5.20 (фикс)
    position = margin × 3 = \$15.60
    tons = position / price
    
    risk_dollars = 2% баланса
      → sl_move = risk / (tons × leverage)  [движение цены в \$]
      → stop_loss = price - sl_move
    
    tp_move = sl_move × 2  [риск/прибыль 1:2]
      → take_profit = price + tp_move
    """
    position_dollars = MARGIN_PER_TRADE * LEVERAGE
    tons = position_dollars / current_price
    risk_dollars = balance * RISK_PCT
    
    # Движение цены до стоп-лосса (с учётом плеча)
    sl_move = risk_dollars / (tons * LEVERAGE)
    stop_loss = current_price - sl_move
    sl_pct = (stop_loss / current_price - 1) * 100
    
    # Движение цены до тейк-профита
    tp_move = sl_move * RISK_REWARD_RATIO
    take_profit = current_price + tp_move
    tp_pct = (take_profit / current_price - 1) * 100
    
    # Прибыль/убыток в долларах
    profit_dollars = risk_dollars * RISK_REWARD_RATIO
    loss_dollars = risk_dollars
    
    # Комиссия Bybit (мейкер 0.02% × 2 = 0.04% от позиции)
    commission = position_dollars * 0.0004
    
    return {
        "balance": round(balance, 2),
        "position": round(position_dollars, 2),
        "tons": round(tons, 2),
        "margin": MARGIN_PER_TRADE,
        "leverage": LEVERAGE,
        "stop_loss": round(stop_loss, 4),
        "sl_pct": round(sl_pct, 2),
        "take_profit": round(take_profit, 4),
        "tp_pct": round(tp_pct, 2),
        "profit": round(profit_dollars - commission, 2),
        "loss": round(loss_dollars + commission, 2),
        "commission": round(commission, 4),
        "risk_reward": RISK_REWARD_RATIO,
    }


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


def ema(data, per):
    if len(data) < per: return None
    k = 2/(per+1)
    res = sum(data[:per])/per
    for p in data[per:]:
        res = p*k + res*(1-k)
    return res


def macd_line(data) -> Optional[float]:
    e12 = ema(data, 12)
    e26 = ema(data, 26)
    if e12 is None or e26 is None: return None
    return e12 - e26


def signal(data_prices, data_volumes, balance: float) -> Optional[Dict]:
    if len(data_prices) < 100: return None
    
    cur = data_prices[-1]
    s7 = sma(data_prices, 7)
    s20 = sma(data_prices, 20)
    r = rsi(data_prices, 14)
    macd = macd_line(data_prices)
    
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
    
    # Тренд: MACD + SMA
    trend = "нейтральный"
    trend_score = 0
    if s7 and s20:
        if s7 > s20: trend_score += 1
    if macd and macd > 0: trend_score += 1
    if trend_score >= 2: trend = "↑ бычий"
    elif trend_score == 0: trend = "↓ медвежий"
    else: trend = "↔ боковик"
    
    # Расчёт SL/TP
    tp = calc_trade_params(cur, balance)
    
    # === BUY: RSI < 35 + тренд не медвежий ===
    if r and r < BUY_RSI and trend != "↓ медвежий":
        strength = "STRONG" if r < 30 and trend == "↑ бычий" else "WEAK"
        return {
            "signal": "BUY",
            "strength": strength,
            "price": round(cur, 4),
            "trade": tp,
            "sma7": round(s7 or 0, 4), "sma20": round(s20 or 0, 4),
            "rsi": round(r, 1), "macd": round(macd, 6) if macd else None,
            "vr": round(vr, 1), "bb_lower": round(bb_lo,4) if bb_lo else None,
            "bb_upper": round(bb_hi,4) if bb_hi else None,
            "trend": trend,
            "why": [
                f"RSI={r:.0f} < {BUY_RSI} ✅ перепродано",
                f"Тренд: {trend} (SMA7={'↑' if s7 and s20 and s7>s20 else '↓'} MACD={'+' if macd and macd>0 else '−'})",
            ]
        }
    
    # === SELL: RSI > 70 ===
    if r and r > SELL_RSI:
        strength = "STRONG" if r > 75 else "WEAK"
        return {
            "signal": "SELL",
            "strength": strength,
            "price": round(cur, 4),
            "trade": tp,
            "sma7": round(s7 or 0, 4), "sma20": round(s20 or 0, 4),
            "rsi": round(r, 1), "macd": round(macd, 6) if macd else None,
            "vr": round(vr, 1), "bb_lower": round(bb_lo,4) if bb_lo else None,
            "bb_upper": round(bb_hi,4) if bb_hi else None,
            "trend": trend,
            "why": [
                f"RSI={r:.0f} > {SELL_RSI} ✅ перекуплен",
                f"Тренд: {trend}",
            ]
        }
    
    return None


def save_signal(sig):
    conn = sqlite3.connect(str(SIGNALS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, signal TEXT, strength TEXT,
        price REAL, rsi REAL, balance REAL,
        sl REAL, tp REAL, trend TEXT, macd REAL
    )""")
    conn.execute("""INSERT INTO signals (ts,signal,strength,price,rsi,balance,sl,tp,trend,macd)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [datetime.now(UTC_PLUS_3).isoformat(), sig["signal"], sig["strength"],
         sig["price"], sig["rsi"], sig["trade"]["balance"],
         sig["trade"]["stop_loss"], sig["trade"]["take_profit"],
         sig["trend"], sig.get("macd")])
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
    t = sig["trade"]
    
    if sig["signal"] == "BUY":
        sl_str = f"🛑 <b>Стоп-лосс: ${t['stop_loss']:.4f}</b> ({t['sl_pct']:.1f}%)\n   → убыток: <b>-${t['loss']:.2f}</b>"
        tp_str = f"🎯 <b>Тейк-профит: ${t['take_profit']:.4f}</b> (+{t['tp_pct']:.1f}%)\n   → прибыль: <b>+${t['profit']:.2f}</b>"
    else:
        sl_str = f"🛑 <b>Стоп-лосс: ${t['stop_loss']:.4f}</b> (+{abs(t['sl_pct']):.1f}%)\n   → убыток: <b>-${t['loss']:.2f}</b>"
        tp_str = f"🎯 <b>Тейк-профит: ${t['take_profit']:.4f}</b> ({t['tp_pct']:.1f}%)\n   → прибыль: <b>+${t['profit']:.2f}</b>"
    
    # Статистика
    total_trades = get_trade_count()
    losses = get_consecutive_losses()
    stats = f"📊 Сделок: {total_trades}"
    if losses > 0:
        stats += f" | Убытков подряд: {losses}"
    if losses >= 3:
        stats += "\n⚠️ <b>ВНИМАНИЕ: 3 убытка подряд — запущен анализ!</b>"
    
    return (
        f"{em} <b>{sig['signal']} TON/USDT x{LEVERAGE}</b> {bar} {sig['strength']}\n"
        f"\n"
        f"💰 <b>Вход: ${sig['price']:.4f}</b>\n"
        f"💵 Баланс: <b>\${t['balance']:.0f}</b>\n"
        f"📐 Позиция: <b>\${t['position']:.2f}</b> = {t['tons']} TON ×{LEVERAGE}\n"
        f"{sl_str}\n"
        f"{tp_str}\n"
        f"⚖️ Риск/Прибыль: <b>1:{t['risk_reward']:.0f}</b> | Комиссия: ${t['commission']:.4f}\n"
        f"\n"
        f"📈 Тренд: {sig['trend']}\n"
        f"📊 RSI={sig['rsi']} | SMA20=${sig['sma20']:.4f} | MACD={sig.get('macd','?')}\n"
        f"📏 Bollinger: ${sig['bb_lower']:.4f} — ${sig['bb_upper']:.4f}\n"
        f"\n"
        f"{chr(10).join(sig['why'])}\n"
        f"\n"
        f"{stats}\n"
        f"\n"
        f"{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )


if __name__ == "__main__":
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT "+str(MIN_ROWS*2)).fetchall()
    conn.close()
    rows = rows[::-1]
    
    if len(rows) < MIN_ROWS:
        print(f"Данных: {len(rows)}/{MIN_ROWS}")
        sys.exit(0)
    
    prices = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    balance = get_current_balance()
    
    sig = signal(prices, volumes, balance)
    
    if sig:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] {sig['signal']} ${sig['price']:.4f} "
              f"RSI={sig['rsi']} | SL=${sig['trade']['stop_loss']:.4f} "
              f"({sig['trade']['sl_pct']:+.1f}%) | TP=${sig['trade']['take_profit']:.4f} "
              f"(+{sig['trade']['tp_pct']:.1f}%)")
        save_signal(sig)
        send_tg(format_signal(sig))
    else:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Нет сигнала "
              f"(RSI={rsi(prices):.0f}, цена=${prices[-1]:.4f}, "
              f"тренд={'↑' if sma(prices,7) and sma(prices,20) and sma(prices,7)>sma(prices,20) else '↓'})")
