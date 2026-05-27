#!/usr/bin/env python3
"""
Webhook-приёмник для TradingView.
Принимает POST-запросы от TV-алертов → пересылает в Telegram + paper_trader.

Запуск: python3 tv_webhook_server.py [порт]
По умолчанию порт 8765

Данные, которые шлёт TV в Message (JSON):
{
  "symbol": "TONUSDT",
  "price": "2.15",
  "time": "2026-05-14T12:00:00Z",
  "timeframe": "15",
  "action": "buy",
  "comment": "RSI<35 + SuperTrend up"
}
"""

import json, sys, os, urllib.request, urllib.parse, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

WEBHOOK_DB = Path("/home/oleg/workspace/crypto-ton/webhook.db")
TRADES_DB = Path("/home/oleg/workspace/crypto-ton/trades.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

LEVERAGE = 3
BASE_MARGIN = 5.20


def init_db():
    conn = sqlite3.connect(str(WEBHOOK_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS webhook_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        source TEXT DEFAULT 'tradingview',
        symbol TEXT, action TEXT, price REAL,
        timeframe TEXT, comment TEXT
    )""")
    conn.commit()
    conn.close()


def send_tg(text: str):
    token = os.getenv("TRADING_BOT_TOKEN", "").strip()
    chat = os.getenv("TRADING_CHAT_ID", "").strip()
    if not token or not chat:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token or not chat:
        return
    try:
        d = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=d), timeout=10)
    except: pass


def process_webhook(data: dict):
    """Обработать входящий сигнал от TV."""
    symbol = data.get("symbol", "TONUSDT")
    action = data.get("action", "").lower()
    price = float(data.get("price", 0))
    timeframe = data.get("timeframe", "?")
    comment = data.get("comment", "")
    
    now = datetime.now(UTC_PLUS_3).isoformat()
    
    # Сохраняем
    conn = sqlite3.connect(str(WEBHOOK_DB))
    conn.execute("""
        INSERT INTO webhook_signals (ts, symbol, action, price, timeframe, comment)
        VALUES (?,?,?,?,?,?)
    """, (now, symbol, action, price, timeframe, comment))
    conn.commit()
    conn.close()
    
    # Форматируем
    emoji = {"buy": "🟢", "sell": "🔴", "long": "🟢", "short": "🔴"}.get(action, "⚪")
    
    # Расчёт SL/TP для нашей стратегии
    sl_pct = 0.008
    tp_pct = 0.016
    
    if action in ("buy", "long"):
        sl = round(price * (1 - sl_pct), 4)
        tp = round(price * (1 + tp_pct), 4)
        side = "BUY"
    else:
        sl = round(price * (1 + sl_pct), 4)
        tp = round(price * (1 - tp_pct), 4)
        side = "SELL"
    
    position_usd = BASE_MARGIN * LEVERAGE
    tons = position_usd / price
    
    msg = (
        f"{emoji} <b>SIGNAL FROM TV {side}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Монета: <b>{symbol}</b>\n"
        f"Цена: <b>${price:.4f}</b>\n"
        f"Таймфрейм: <b>{timeframe}m</b>\n\n"
        f"🛑 Стоп-лосс: <b>${sl:.4f}</b> ({sl_pct*100:.1f}%)\n"
        f"🎯 Тейк-профит: <b>${tp:.4f}</b> ({tp_pct*100:.1f}%)\n"
        f"📐 Позиция: <b>${position_usd:.2f}</b> ({tons:.1f} TON ×{LEVERAGE})\n\n"
        f"💬 <i>{comment}</i>\n\n"
        f"{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )
    send_tg(msg)
    
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] TV signal: {action} {symbol} @ ${price:.4f} | SL=${sl:.4f} TP=${tp:.4f}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/tv-webhook':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            
            try:
                data = json.loads(body)
                process_webhook(data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        elif self.path == '/stats':
            conn = sqlite3.connect(str(WEBHOOK_DB))
            count = conn.execute("SELECT COUNT(*) FROM webhook_signals").fetchone()[0]
            last = conn.execute("SELECT ts, action, symbol, price FROM webhook_signals ORDER BY id DESC LIMIT 5").fetchall()
            conn.close()
            html = f"<h1>TV Webhook Stats</h1><p>Total signals: {count}</p><table>"
            for r in last:
                html += f"<tr><td>{r[0][:19]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]:.4f}</td></tr>"
            html += "</table>"
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            html = """
            <h1>📡 TV Webhook Server — Active</h1>
            <p>Send POST to <code>/tv-webhook</code> with TradingView alert JSON.</p>
            <p>Health: <a href="/health">/health</a></p>
            <p>Stats: <a href="/stats">/stats</a></p>
            """
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())
    
    def log_message(self, format, *args):
        pass  # меньше шума


if __name__ == "__main__":
    init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"📡 TV Webhook Server: http://localhost:{port}")
    print(f"   Webhook URL: http://oleg-ai:{port}/tv-webhook")
    print(f"   Health:      http://localhost:{port}/health")
    print(f"   Stats:       http://localhost:{port}/stats")
    server.serve_forever()
