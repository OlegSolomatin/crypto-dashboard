import sqlite3
conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')
total_pnl = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
trades = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
wins = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl > 0').fetchone()[0]
open_pos = conn.execute('SELECT type, entry_price FROM paper_positions WHERE closed_ts IS NULL').fetchone()
conn.close()
balance = 100.0 + total_pnl
print(f'{balance:.2f}|{trades}|{wins}|{total_pnl:.2f}|{open_pos[0] if open_pos else "none"} {open_pos[1] if open_pos else ""}')
