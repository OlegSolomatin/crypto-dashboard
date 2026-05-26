"""
Мониторинг цен TON/USDT с Bybit (публичный API — без авторизации)
Сохраняет цены в SQLite каждую минуту.

Использование:
  python3 monitor.py          # собрать сейчас
  python3 monitor.py --daemon # непрерывный мониторинг (cron)
"""

import sqlite3
import json
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
from datetime import timedelta

# === НАСТРОЙКИ ===
SYMBOL = "TONUSDT"           # тикер Bybit
INTERVAL = 60                # секунд между сборами
DB_PATH = Path(__file__).parent / "data.db"
BYBIT_URL = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=" + SYMBOL

UTC_PLUS_3 = timezone(timedelta(hours=3))


def init_db():
    """Создать базу, если нет."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,          -- ISO 8601 UTC+3
            symbol TEXT NOT NULL,
            last_price REAL NOT NULL,         -- последняя цена
            high_24h REAL,
            low_24h REAL,
            volume_24h REAL,
            change_pct REAL                   -- изменение за 24ч в %
        )
    """)
    # Индекс для быстрых запросов за последние N свечей
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prices_ts 
        ON prices(timestamp DESC)
    """)
    conn.commit()
    conn.close()


def fetch_ticker() -> dict | None:
    """Получить тикер TON/USDT с Bybit (публичный, без ключа)."""
    try:
        req = urllib.request.Request(BYBIT_URL, headers={"User-Agent": "Hermes-Crypto/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        
        if data.get("retCode") != 0:
            print(f"  Bybit API error: {data.get('retMsg')}", file=sys.stderr)
            return None
        
        ticker = data["result"]["list"][0]
        return {
            "symbol": ticker["symbol"],
            "last_price": float(ticker["lastPrice"]),
            "high_24h": float(ticker.get("highPrice24h", 0)),
            "low_24h": float(ticker.get("lowPrice24h", 0)),
            "volume_24h": float(ticker.get("volume24h", 0)),
            "change_pct": float(ticker.get("price24hPcnt", 0)) * 100,
        }
    except Exception as e:
        print(f"  Fetch error: {e}", file=sys.stderr)
        return None


def save_ticker(ticker: dict):
    """Сохранить тикер в базу."""
    conn = sqlite3.connect(str(DB_PATH))
    now = datetime.now(UTC_PLUS_3).isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO prices (timestamp, symbol, last_price, high_24h, low_24h, volume_24h, change_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (now, ticker["symbol"], ticker["last_price"],
          ticker["high_24h"], ticker["low_24h"],
          ticker["volume_24h"], ticker["change_pct"]))
    conn.commit()
    conn.close()


def collect_once():
    """Собрать один тик и показать результат."""
    ticker = fetch_ticker()
    if ticker:
        save_ticker(ticker)
        print(f"  TON/USDT = ${ticker['last_price']:.4f}  "
              f"(24ч: {ticker['change_pct']:+.2f}%  "
              f"объём: {ticker['volume_24h']:,.0f}$)")
    return ticker


def run_daemon():
    """Непрерывный сбор (запускается cron'ом раз в минуту, делает один замер)."""
    ticker = fetch_ticker()
    if ticker:
        save_ticker(ticker)
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] "
              f"TON = ${ticker['last_price']:.4f}  "
              f"({ticker['change_pct']:+.2f}%)")


if __name__ == "__main__":
    init_db()
    
    if "--once" in sys.argv:
        collect_once()
    else:
        run_daemon()
