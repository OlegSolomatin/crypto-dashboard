#!/usr/bin/env python3
"""Paper DB full analysis — all closed trades with details."""
import sqlite3

conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')
conn.row_factory = sqlite3.Row

# All closed trades
trades = conn.execute('''SELECT id, opened_ts, closed_ts, type, entry_price, exit_price,
    sl, tp, qty, margin, leverage, pnl, pnl_pct, exit_reason, bars_held, balance_before, balance_after
    FROM paper_positions WHERE closed_ts IS NOT NULL ORDER BY id''').fetchall()

total_pnl = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
total_trades = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
wins = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL').fetchone()[0]
losses = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl < 0 AND closed_ts IS NOT NULL').fetchone()[0]
breakeven = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl = 0 AND closed_ts IS NOT NULL').fetchone()[0]
avg_pnl = conn.execute('SELECT AVG(pnl) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
avg_pnl_pct = conn.execute('SELECT AVG(pnl_pct) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
max_loss = conn.execute('SELECT MIN(pnl) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
max_win = conn.execute('SELECT MAX(pnl) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
sum_wins = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL').fetchone()[0]
sum_losses = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE pnl < 0 AND closed_ts IS NOT NULL').fetchone()[0]
avg_win = conn.execute('SELECT AVG(pnl) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL').fetchone()[0]
avg_loss = conn.execute('SELECT AVG(pnl) FROM paper_positions WHERE pnl < 0 AND closed_ts IS NOT NULL').fetchone()[0]
avg_bars = conn.execute('SELECT AVG(bars_held) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]

open_pos = conn.execute('SELECT type, entry_price, opened_ts, sl, tp FROM paper_positions WHERE closed_ts IS NULL').fetchall()

# Daily PnL
daily = conn.execute('''
    SELECT DATE(opened_ts) as day, COUNT(*) as cnt, SUM(pnl) as day_pnl
    FROM paper_positions WHERE closed_ts IS NOT NULL
    GROUP BY DATE(opened_ts) ORDER BY day
''').fetchall()

conn.close()

balance = 100.0 + total_pnl
winrate = (wins / total_trades * 100) if total_trades > 0 else 0
pf = abs(sum_wins / sum_losses) if sum_losses != 0 and sum_losses < 0 else float('inf')

# Max drawdown — compute from balance_before/after
balances = [100.0]
for t in trades:
    balances.append(t['balance_after'] if t['balance_after'] else balances[-1] + t['pnl'])

peak = 100.0
max_dd = 0.0
for b in balances:
    if b > peak:
        peak = b
    dd = (peak - b) / peak * 100
    if dd > max_dd:
        max_dd = dd

print("=" * 70)
print("  PAPER TRADING TON/USDT — FULL 7-DAY ANALYSIS")
print("=" * 70)
print(f"Period: 2026-05-14 to 2026-05-21 (7 days)")
print(f"Initial balance: 100.00 USD")
print(f"Current balance: {balance:.2f} USD")
print(f"Total PnL: {total_pnl:+.2f} USD ({total_pnl/100*100:+.2f}%)")
print()
print(f"Total closed trades: {total_trades}")
print(f"Wins: {wins} | Losses: {losses} | Breakeven: {breakeven}")
print(f"Winrate: {winrate:.1f}%")
print(f"Profit Factor: {pf:.2f}")
print()
print(f"Avg PnL per trade: {avg_pnl:+.4f} USD ({avg_pnl_pct:+.2f}%)")
print(f"Avg win: {avg_win:+.4f} USD | Avg loss: {avg_loss:+.4f} USD")
print(f"Avg bars held: {avg_bars:.1f}")
print(f"Max win: {max_win:+.4f} USD | Max loss: {max_loss:+.4f} USD")
print(f"Max drawdown: {max_dd:.2f}%")
print()

print("Daily breakdown:")
for d in daily:
    print(f"  {d['day']}: {d['cnt']} trades, PnL: {d['day_pnl']:+.2f} USD")

print()
print("Open positions:")
if open_pos:
    for pos in open_pos:
        print(f"  {pos['type']} @ {pos['entry_price']} (SL: {pos['sl']}, TP: {pos['tp']}) opened {pos['opened_ts']}")
else:
    print("  None")

print()
print("All closed trades:")
print("-" * 70)
for t in trades:
    print(f"#{t['id']:02d} {t['type']:4s} | Entry: {t['entry_price']:>8.4f} Exit: {t['exit_price']:>8.4f} | "
          f"PnL: {t['pnl']:>+8.4f} USD ({t['pnl_pct']:>+6.2f}%) | "
          f"Reason: {t['exit_reason']:>8s} | Bars: {t['bars_held']:>3d} | "
          f"Opened: {t['opened_ts']}")
print("-" * 70)

# Recommendations
print()
print("=" * 70)
print("  QUICK ASSESSMENT")
print("=" * 70)
if total_trades < 10:
    print("WARNING: < 10 trades — not statistically significant")
else:
    if winrate >= 55 and pf >= 1.5:
        print("STATUS: ✅ Statistically promising (enough trades, good metrics)")
    elif winrate >= 50 and pf >= 1.2:
        print("STATUS: ⚠️ Borderline — needs more data or parameter tuning")
    else:
        print("STATUS: ❌ Underperforming — strategy needs revision")

    if max_dd > 10:
        print(f"RISK: ⚠️ Max drawdown {max_dd:.1f}% is high for a 100 USD account")
    elif max_dd > 5:
        print(f"RISK: ⚡ Max drawdown {max_dd:.1f}% — moderate, acceptable with scaling")
    else:
        print(f"RISK: ✅ Max drawdown {max_dd:.1f}% — well controlled")
