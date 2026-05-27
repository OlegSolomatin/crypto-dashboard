import sqlite3
from datetime import datetime

conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')

total_pnl = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
trades = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
wins = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl > 0').fetchone()[0]
open_pos = conn.execute('SELECT type, entry_price FROM paper_positions WHERE closed_ts IS NULL').fetchone()

# Trades in last 24h
trades_24h = conn.execute(
    "SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL AND closed_ts > datetime('now', '-1 day')"
).fetchone()[0]
wins_24h = conn.execute(
    "SELECT COUNT(*) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL AND closed_ts > datetime('now', '-1 day')"
).fetchone()[0]

conn.close()

balance = 100.0 + total_pnl
pct = (balance - 100.0) / 100.0 * 100
winrate_24h = (wins_24h / trades_24h * 100) if trades_24h > 0 else 0

print(f'{balance:.2f}|{pct:+.2f}|{trades_24h}|{winrate_24h:.0f}|{open_pos[0] if open_pos else "none"}|{open_pos[1] if open_pos else ""}|{trades}|{wins}|{total_pnl:.2f}')
