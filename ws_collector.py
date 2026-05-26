#!/usr/bin/env python3
"""
СТРИМИНГ-КОЛЛЕКТОР Bybit WebSocket → SQLite
Реальные данные о цене, стакане, сделках — посекундно.

Каналы:
  • tickers.{symbol} — цена, изменение, объём (каждую секунду)
  • orderbook.{depth}.{symbol} — стакан цен (bids/asks)
  • publicTrade.{symbol} — последние сделки

Запуск:
  python3 ws_collector.py                    # foreground (для отладки)
  python3 ws_collector.py --background       # демон (через cron)
  
Для фонового режима через Hermes:
  terminal(command="python3 /home/oleg/workspace/crypto-ton/ws_collector.py", background=true)
"""

import sqlite3, json, time, sys, os, threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

try:
    import websocket
except ImportError:
    print("pip install websocket-client", file=sys.stderr)
    sys.exit(1)

DB = Path("/home/oleg/workspace/crypto-ton/stream.db")
SYMBOL = "TONUSDT"
UTC_PLUS_3 = timezone(timedelta(hours=3))

# Bybit WebSocket (публичный, без ключа)
WS_URL = "wss://stream.bybit.com/v5/public/spot"

# Храним последние значения
last_ticker: Dict = {}
last_book: Dict = {"bids": [], "asks": [], "ts": ""}
last_trades: list = []
running = True


def init_db():
    conn = sqlite3.connect(str(DB))
    
    # Таблица тикеров (1/sec)
    conn.execute("""CREATE TABLE IF NOT EXISTS tickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        price REAL, change_pct REAL, high_24h REAL, low_24h REAL,
        volume_24h REAL, turnover_24h REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickers_ts ON tickers(ts)")
    
    # Стакан (снапшоты)
    conn.execute("""CREATE TABLE IF NOT EXISTS orderbook_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        best_bid REAL, best_ask REAL, spread REAL, spread_pct REAL,
        bid_depth_1pct REAL, ask_depth_1pct REAL,
        bids_json TEXT, asks_json TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_book_ts ON orderbook_snapshots(ts)")
    
    # Сделки
    conn.execute("""CREATE TABLE IF NOT EXISTS trades_stream (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        trade_time TEXT, side TEXT, price REAL, size REAL, value REAL
    )""")
    
    conn.commit()
    conn.close()


def save_ticker(data: dict):
    conn = sqlite3.connect(str(DB))
    now = datetime.now(UTC_PLUS_3).isoformat()
    conn.execute("""
        INSERT INTO tickers (ts, price, change_pct, high_24h, low_24h, volume_24h, turnover_24h)
        VALUES (?,?,?,?,?,?,?)
    """, (now, float(data.get("lastPrice", 0)), float(data.get("price24hPcnt", 0))*100,
          float(data.get("highPrice24h", 0)), float(data.get("lowPrice24h", 0)),
          float(data.get("volume24h", 0)), float(data.get("turnover24h", 0))))
    conn.commit()
    conn.close()
    
    global last_ticker
    last_ticker = {
        "price": float(data.get("lastPrice", 0)),
        "change": float(data.get("price24hPcnt", 0)) * 100,
        "ts": now
    }


def save_orderbook(data: dict):
    if data.get("type") != "snapshot":
        return  # дельты пока не обрабатываем, только снапшоты
    
    book = data.get("data", {})
    bids = [(float(b[0]), float(b[1])) for b in book.get("b", [])[:10]]
    asks = [(float(a[0]), float(a[1])) for a in book.get("a", [])[:10]]
    
    if not bids or not asks:
        return
    
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    spread = best_ask - best_bid
    spread_pct = (spread / best_ask * 100) if best_ask > 0 else 0
    
    # Глубина: сколько TON в пределах ±1% от середины
    mid = (best_bid + best_ask) / 2
    bid_depth = sum(b[1] for b in bids if b[0] >= mid * 0.99)
    ask_depth = sum(a[1] for a in asks if a[0] <= mid * 1.01)
    
    now = datetime.now(UTC_PLUS_3).isoformat()
    conn = sqlite3.connect(str(DB))
    conn.execute("""
        INSERT INTO orderbook_snapshots (ts, best_bid, best_ask, spread, spread_pct,
            bid_depth_1pct, ask_depth_1pct, bids_json, asks_json)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (now, best_bid, best_ask, round(spread, 6), round(spread_pct, 4),
          round(bid_depth, 2), round(ask_depth, 2),
          json.dumps(bids[:5]), json.dumps(asks[:5])))
    conn.commit()
    conn.close()
    
    global last_book
    last_book = {"bids": bids[:5], "asks": asks[:5], "ts": now}


def save_trade(data: dict):
    trades = data.get("data", [])
    conn = sqlite3.connect(str(DB))
    now = datetime.now(UTC_PLUS_3).isoformat()
    
    for t in trades[:10]:
        price = float(t.get("p", 0))
        size = float(t.get("v", 0))
        conn.execute("""
            INSERT INTO trades_stream (ts, trade_time, side, price, size, value)
            VALUES (?,?,?,?,?,?)
        """, (now, t.get("T", ""), t.get("S", ""), price, size, round(price * size, 2)))
    
    conn.commit()
    conn.close()
    
    global last_trades
    last_trades = (last_trades + trades)[-20:]


def on_message(ws, message):
    try:
        data = json.loads(message)
    except:
        return
    
    topic = data.get("topic", "")
    
    if "tickers" in topic:
        save_ticker(data.get("data", {}))
    elif "orderbook" in topic:
        save_orderbook(data)
    elif "publicTrade" in topic:
        save_trade(data)


def on_error(ws, error):
    print(f"WS error: {error}", file=sys.stderr)


def on_close(ws, status, msg):
    global running
    print(f"WS closed: {status} {msg}", file=sys.stderr)
    running = False


def on_open(ws):
    # Подписываемся на каналы
    subscribe = {
        "op": "subscribe",
        "args": [
            f"tickers.{SYMBOL}",
            f"orderbook.50.{SYMBOL}",
            f"publicTrade.{SYMBOL}",
        ]
    }
    ws.send(json.dumps(subscribe))
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Подписка отправлена")


def print_status():
    """Периодическая печать статуса."""
    while running:
        time.sleep(60)
        if last_ticker:
            price = last_ticker.get("price", 0)
            change = last_ticker.get("change", 0)
            spread = 0
            if last_book.get("bids") and last_book.get("asks"):
                spread = last_book["asks"][0][0] - last_book["bids"][0][0]
            print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] "
                  f"TON={price:.4f} ({change:+.2f}%) | "
                  f"Спред={spread:.4f} | "
                  f"Бид={last_book.get('bids',[[0,0]])[0][1]:.0f} TON | "
                  f"Аск={last_book.get('asks',[[0,0]])[0][1]:.0f} TON")


def run():
    init_db()
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Подключение к Bybit WS...")
    
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )
    
    # Статус-тред
    status_thread = threading.Thread(target=print_status, daemon=True)
    status_thread.start()
    
    # Запуск с авто-переподключением
    while True:
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"WS exception: {e}", file=sys.stderr)
        
        if not running:
            break
        
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Переподключение через 5с...")
        time.sleep(5)


if __name__ == "__main__":
    run()
