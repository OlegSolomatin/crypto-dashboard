#!/usr/bin/env python3
"""Daily paper trading report for Telegram."""
import sqlite3
import urllib.request
import urllib.parse
import json
import os
from datetime import datetime, timezone, timedelta

DB = '/home/oleg/workspace/crypto-ton/paper.db'
UTC_PLUS_3 = timezone(timedelta(hours=3))

# ── Read stats ──
conn = sqlite3.connect(DB)

# 24h ago in Unix timestamp
now = datetime.now(UTC_PLUS_3)
cutoff_ts = (now - timedelta(hours=24)).timestamp()

# Trades closed in last 24h
trades_24h = conn.execute(
    'SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL AND closed_ts > ?',
    (cutoff_ts,)
).fetchone()[0]

# Wins in last 24h
wins_24h = conn.execute(
    'SELECT COUNT(*) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL AND closed_ts > ?',
    (cutoff_ts,)
).fetchone()[0]

# Total PnL (all time)
total_pnl = conn.execute(
    'SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL'
).fetchone()[0]

# Open positions
open_pos = conn.execute(
    'SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NULL'
).fetchone()[0]

conn.close()

balance = 100.0 + total_pnl
pct = (balance - 100.0) / 100.0 * 100
winrate = (wins_24h / trades_24h * 100) if trades_24h > 0 else 0

date_str = now.strftime('%d.%m.%Y %H:%M MSK')

report = (
    f"<b>ЕЖЕДНЕВНЫЙ ОТЧЁТ TON</b>\n"
    f"━━━━━━━━━━━━━━━━━━━━━\n"
    f"Баланс: {balance:.2f} USD ({pct:+.2f}%)\n"
    f"Сделок за 24ч: {trades_24h} | Винрейт сегодня: {winrate:.1f}%\n"
    f"Открытых позиций: {open_pos}\n"
    f"━━━━━━━━━━━━━━━━━━━━━\n"
    f"{date_str}"
)

print(report)

# ── Send to Telegram ──
token = os.getenv('TRADING_BOT_TOKEN', '').strip()
chat_id = os.getenv('TRADING_CHAT_ID', '218809870').strip()

if not token:
    print('\n⚠️ TRADING_BOT_TOKEN not set — skipping Telegram')
    exit(1)

url = f'https://api.telegram.org/bot{token}/sendMessage'
data = urllib.parse.urlencode({
    'chat_id': chat_id,
    'text': report,
    'parse_mode': 'HTML'
}).encode()

try:
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    if result.get('ok'):
        print('✅ Telegram: ОТПРАВЛЕНО')
    else:
        print(f'❌ Telegram error: {result}')
except Exception as e:
    print(f'❌ Telegram send failed: {e}')
