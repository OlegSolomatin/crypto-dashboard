import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')

cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')

# 24h stats
total_pnl_24h = conn.execute(
    'SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL AND closed_ts >= ?',
    (cutoff,)
).fetchone()[0]

trades_24h = conn.execute(
    'SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL AND closed_ts >= ?',
    (cutoff,)
).fetchone()[0]

wins_24h = conn.execute(
    'SELECT COUNT(*) FROM paper_positions WHERE pnl > 0 AND closed_ts >= ?',
    (cutoff,)
).fetchone()[0]

# All-time for balance
total_pnl_all = conn.execute(
    'SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL'
).fetchone()[0]

# Open positions
open_pos = conn.execute(
    'SELECT type, entry_price FROM paper_positions WHERE closed_ts IS NULL'
).fetchone()

conn.close()

balance = 100.0 + total_pnl_all
pct = (total_pnl_all / 100.0) * 100
winrate = (wins_24h / trades_24h * 100) if trades_24h > 0 else 0
open_type = open_pos[0] if open_pos else "none"
open_price = open_pos[1] if open_pos else ""

print(f'{balance:.2f}|{trades_24h}|{wins_24h}|{total_pnl_all:.2f}|{pct:+.2f}|{winrate:.1f}|{open_type}|{open_price}')
