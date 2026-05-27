#!/usr/bin/env python3
"""
Проверка резких движений цены TON и алерт в Telegram.
Запускается cron'ом раз в 5 минут, сравнивает текущую цену с ценой 30 минут назад.
"""

import sqlite3
import urllib.request
import urllib.parse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
ALERT_THRESHOLD = 3.0  # процент изменения для алерта
UTC_PLUS_3 = timezone(timedelta(hours=3))

def send_telegram(message: str):
    """Приоритет: trading-бот → основной бот."""
    token = os.getenv("TRADING_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TRADING_CHAT_ID", "").strip()
    
    if not token or not chat_id:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    
    if not token or not chat_id:
        print("TRADING_BOT_TOKEN or TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Telegram send error: {e}", file=sys.stderr)
        return False


def check_alerts():
    conn = sqlite3.connect(str(DB))
    
    # Текущая цена
    now_row = conn.execute(
        "SELECT last_price, change_pct FROM prices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not now_row:
        conn.close()
        return
    
    price_now, change_24h = now_row
    
    # Цена 30 минут назад (примерно 30 записей)
    old_row = conn.execute(
        "SELECT last_price FROM prices ORDER BY id DESC LIMIT 1 OFFSET 30"
    ).fetchone()
    
    conn.close()
    
    if not old_row:
        return  # недостаточно данных
    
    price_old = old_row[0]
    change_30m = ((price_now - price_old) / price_old) * 100
    
    if abs(change_30m) >= ALERT_THRESHOLD:
        direction = "📈 РОСТ" if change_30m > 0 else "📉 ПАДЕНИЕ"
        emoji = "🟢" if change_30m > 0 else "🔴"
        msg = (
            f"{emoji} <b>TON/USDT — {direction}!</b>\n\n"
            f"Цена: <b>${price_now:.4f}</b>\n"
            f"Изменение за 30 мин: <b>{change_30m:+.1f}%</b>\n"
            f"Изменение за 24ч: <b>{change_24h:+.1f}%</b>\n"
            f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
        )
        send_telegram(msg)
        print(f"Alert sent: {change_30m:+.1f}% за 30 мин")
    else:
        print(f"OK: {change_30m:+.1f}% — без алерта")


if __name__ == "__main__":
    check_alerts()
