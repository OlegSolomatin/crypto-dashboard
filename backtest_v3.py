#!/usr/bin/env python3
"""
Бэктест стратегии v3 (фьючерсы x3) на всей истории.
Сравнение v2 (спот) vs v3 (фьючерсы x3).
"""

import sqlite3, math, random
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

LEVERAGE = 3
INITIAL_BALANCE = 100.0
MARGIN = 5.20
RISK_PCT = 0.02
RR = 2.0
BUY_RSI = 35
SELL_RSI = 70

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

conn = sqlite3.connect(str(DB))
rows = conn.execute("SELECT last_price, volume_24h FROM prices ORDER BY id ASC").fetchall()
conn.close()

prices = [r[0] for r in rows]
n = len(prices)

# ===== БЭКТЕСТ v2 (СПОТ) =====
print("=" * 70)
print("  БЭКТЕСТ v2 (СПОТ) — RSI<35 BUY, RSI>70 SELL")
print("=" * 70)

balance_v2 = 100.0
trades_v2 = []
position_v2 = None

for i in range(100, n):
    win_p = prices[:i+1]
    r = rsi(win_p)
    
    if position_v2 is None and r:
        if r < BUY_RSI:
            position_v2 = {"type": "BUY", "entry": prices[i], "entry_i": i}
        elif r > SELL_RSI:
            position_v2 = {"type": "SELL", "entry": prices[i], "entry_i": i}
    
    if position_v2:
        # Simulate 30-min hold then exit
        if i - position_v2["entry_i"] >= 30:
            entry = position_v2["entry"]
            exit_p = prices[i]
            
            if position_v2["type"] == "BUY":
                pnl_pct = (exit_p - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_p) / entry * 100
            
            # Use 5% of balance
            trade_size = balance_v2 * 0.05
            pnl_dollars = trade_size * pnl_pct / 100
            
            balance_v2 += pnl_dollars
            trades_v2.append({"type": position_v2["type"], "pnl": pnl_dollars, "pnl_pct": pnl_pct})
            position_v2 = None

if position_v2:
    entry = position_v2["entry"]
    exit_p = prices[-1]
    pnl_pct = (exit_p - entry) / entry * 100 if position_v2["type"] == "BUY" else (entry - exit_p) / entry * 100
    trade_size = balance_v2 * 0.05
    pnl_dollars = trade_size * pnl_pct / 100
    balance_v2 += pnl_dollars
    trades_v2.append({"type": position_v2["type"], "pnl": pnl_dollars, "pnl_pct": pnl_pct})

wins_v2 = sum(1 for t in trades_v2 if t["pnl"] > 0)
print(f"  Сделок: {len(trades_v2)} | Винрейт: {wins_v2/len(trades_v2)*100:.0f}%" if trades_v2 else "  Сделок: 0")
print(f"  Баланс: \${balance_v2:.2f} ({(balance_v2/100-1)*100:+.1f}%)")
if trades_v2:
    print(f"  Средний P&L: \${sum(t['pnl'] for t in trades_v2)/len(trades_v2):+.2f}")

# ===== БЭКТЕСТ v3 (ФЬЮЧЕРСЫ x3) =====
print()
print("=" * 70)
print("  БЭКТЕСТ v3 (ФЬЮЧЕРСЫ x3) — MACD-фильтр, 1:2 риск/прибыль")
print("=" * 70)

balance_v3 = INITIAL_BALANCE
trades_v3 = []
position_v3 = None

for i in range(100, n):
    win_p = prices[:i+1]
    r = rsi(win_p)
    s7 = sma(win_p, 7)
    s20 = sma(win_p, 20)
    m = ema(win_p, 12)
    m26 = ema(win_p, 26)
    macd_val = (m - m26) if m and m26 else None
    
    trend_bullish = (s7 and s20 and s7 > s20) and (macd_val and macd_val > 0)
    
    if position_v3 is None and r:
        # BUY: RSI<35 + бычий тренд
        if r < BUY_RSI and trend_bullish:
            position_v3 = {"type": "BUY", "entry": prices[i], "entry_i": i}
        # SELL: RSI>70
        elif r > SELL_RSI:
            position_v3 = {"type": "SELL", "entry": prices[i], "entry_i": i}
    
    if position_v3:
        entry = position_v3["entry"]
        cur = prices[i]
        pos_dollars = MARGIN * LEVERAGE
        tons = pos_dollars / cur
        risk_dollars = balance_v3 * RISK_PCT
        sl_move = risk_dollars / (tons * LEVERAGE)
        
        if position_v3["type"] == "BUY":
            stop_loss = entry - sl_move
            take_profit = entry + sl_move * RR
        else:
            stop_loss = entry + sl_move
            take_profit = entry - sl_move * RR
        
        exit_p = None
        exit_reason = ""
        
        # Check SL/TP hit
        for j in range(position_v3["entry_i"]+1, min(i+1, position_v3["entry_i"]+181)):
            pj = prices[j]
            if position_v3["type"] == "BUY":
                if pj <= stop_loss:
                    exit_p = stop_loss; exit_reason = "SL"; break
                if pj >= take_profit:
                    exit_p = take_profit; exit_reason = "TP"; break
            else:
                if pj >= stop_loss:
                    exit_p = stop_loss; exit_reason = "SL"; break
                if pj <= take_profit:
                    exit_p = take_profit; exit_reason = "TP"; break
            
            # Timeout 60 min
            if j - position_v3["entry_i"] >= 60:
                exit_p = pj; exit_reason = "TIMEOUT"; break
        
        if exit_p:
            if position_v3["type"] == "BUY":
                pnl_pct = (exit_p - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_p) / entry * 100
            
            pnl_dollars = tons * pnl_pct / 100 * LEVERAGE * cur
            Commission = pos_dollars * 0.0004
            pnl_dollars -= Commission
            
            balance_v3 += pnl_dollars
            trades_v3.append({
                "type": position_v3["type"], "pnl": pnl_dollars,
                "pnl_pct": pnl_pct, "exit": exit_reason,
                "entry_price": entry, "exit_price": exit_p
            })
            position_v3 = None

if position_v3:
    entry = position_v3["entry"]
    cur = prices[-1]
    pnl_pct = (cur - entry) / entry * 100 if position_v3["type"] == "BUY" else (entry - cur) / entry * 100
    pos_dollars = MARGIN * LEVERAGE
    tons = pos_dollars / cur
    pnl_dollars = tons * pnl_pct / 100 * LEVERAGE * cur - pos_dollars * 0.0004
    balance_v3 += pnl_dollars
    trades_v3.append({"type": position_v3["type"], "pnl": pnl_dollars, "pnl_pct": pnl_pct, "exit": "OPEN"})

wins_v3 = sum(1 for t in trades_v3 if t["pnl"] > 0)
losses_v3 = sum(1 for t in trades_v3 if t["pnl"] <= 0)

print(f"  Сделок: {len(trades_v3)} | Побед: {wins_v3} | Поражений: {losses_v3}")
print(f"  Винрейт: {wins_v3/len(trades_v3)*100:.0f}%" if trades_v3 else "  Сделок: 0")
print(f"  Баланс: \${balance_v3:.2f} ({(balance_v3/100-1)*100:+.1f}%)")
if trades_v3:
    avg_win = sum(t["pnl"] for t in trades_v3 if t["pnl"]>0) / wins_v3 if wins_v3 else 0
    avg_loss = sum(t["pnl"] for t in trades_v3 if t["pnl"]<=0) / losses_v3 if losses_v3 else 0
    max_dd = 0; peak = 0; cum = 0
    for t in trades_v3:
        cum += t["pnl"]
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    print(f"  Средний выигрыш: +\${avg_win:.2f} | Средний убыток: -\${abs(avg_loss):.2f}")
    print(f"  Макс. просадка: -\${max_dd:.2f}")
    reasons = {}
    for t in trades_v3:
        reasons[t["exit"]] = reasons.get(t["exit"], 0) + 1
    print(f"  Выходы: {', '.join(f'{k}: {v}' for k,v in reasons.items())}")
    
    # Show last 5 trades
    print(f"\n  Последние сделки:")
    for t in trades_v3[-5:]:
        emoji = "✅" if t["pnl"] > 0 else "❌"
        print(f"    {emoji} {t['type']:5s} вход \${t['entry_price']:.4f} → выход \${t['exit_price']:.4f} "
              f"| {t['pnl']:+.2f}\$ ({t['pnl_pct']:+.2f}%) | {t['exit']}")


# ===== СРАВНЕНИЕ =====
print()
print("=" * 70)
print("  СРАВНЕНИЕ v2 (СПОТ) vs v3 (ФЬЮЧЕРСЫ x3)")
print("=" * 70)
print(f"  {'':20s} {'v2 СПОТ':>12s} {'v3 x3':>12s}")
print(f"  {'Сделок:':20s} {len(trades_v2):>12d} {len(trades_v3):>12d}")
print(f"  {'Винрейт:':20s} {f'{wins_v2/len(trades_v2)*100:.0f}%' if trades_v2 else 'N/A':>12s} {f'{wins_v3/len(trades_v3)*100:.0f}%' if trades_v3 else 'N/A':>12s}")
print(f"  {'Баланс:':20s} \${balance_v2:>11.2f} \${balance_v3:>11.2f}")
print(f"  {'Прирост:':20s} {(balance_v2/100-1)*100:>+11.1f}% {(balance_v3/100-1)*100:>+11.1f}%")
if trades_v2 and trades_v3:
    tpv2 = sum(t["pnl"] for t in trades_v2)
    tpv3 = sum(t["pnl"] for t in trades_v3)
    print(f"  {'Общий P&L:':20s} \${tpv2:>+11.2f} \${tpv3:>+11.2f}")
