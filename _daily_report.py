import sqlite3, sys, os
sys.path.insert(0, '/home/oleg/workspace/crypto-ton')
from trading_tg import send_trading_tg

conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')
total_pnl = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
trades = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
wins = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl > 0').fetchone()[0]
open_pos = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NULL').fetchone()[0]
conn.close()

balance = 100.0 + total_pnl
pct = (balance - 100.0) / 100.0 * 100
winrate = (wins / trades * 100) if trades > 0 else 0

text = (
    "<b>ЕЖЕДНЕВНЫЙ ОТЧЁТ TON</b>\n\n"
    f"Баланс: {balance:.2f} USD ({pct:+.1f}%)\n"
    f"Сделок за 24ч: {trades} | Винрейт сегодня: {winrate:.1f}%\n"
    f"Открытых позиций: {open_pos}\n\n"
    "2026-05-22"
)

ok = send_trading_tg(text, parse_mode="HTML")
print("Sent OK" if ok else "FAILED")
