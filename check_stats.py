import sqlite3

conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')

# All closed trades
trades = conn.execute(
    'SELECT type, entry_price, exit_price, pnl, pnl_pct, exit_reason, bars_held, opened_ts, closed_ts '
    'FROM paper_positions WHERE closed_ts IS NOT NULL ORDER BY id'
).fetchall()

# Balance
total_pnl = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions').fetchone()[0]
balance = float(100.0 + total_pnl)

# Open position
open_pos = conn.execute(
    'SELECT type, entry_price, sl, tp, opened_ts FROM paper_positions WHERE closed_ts IS NULL'
).fetchone()

conn.close()

print(f'Баланс: ${balance:.2f} ({(balance/100-1)*100:+.1f}%)')
print(f'Сделок закрыто: {len(trades)}')
if trades:
    wins = sum(1 for t in trades if t[3] > 0)
    print(f'Винрейт: {wins/len(trades)*100:.0f}% ({wins}/{len(trades)})')
    print(f'Общий P&L: ${sum(t[3] for t in trades):+.2f}')
    reasons = {}
    for t in trades:
        r = t[5]
        reasons[r] = reasons.get(r, 0) + 1
    print(f'Причины закрытия: {reasons}')
    for t in trades:
        print(f'  {t[8][:16]} | {t[0]:5s} | вх${t[1]:.4f}→вых${t[2]:.4f} | {t[3]:+.2f}$ | {t[5]} | {t[6]}мин')
if open_pos:
    print(f'Открыта: {open_pos[0]} @${open_pos[1]:.4f} (SL=${open_pos[2]:.4f} TP=${open_pos[3]:.4f}) с {open_pos[4][:16]}')
else:
    print('Открытых позиций нет')
