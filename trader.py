#!/usr/bin/env python3
"""
Боевой трейдер TON/USDT ×3 для Bybit Testnet.
Запускается cron'ом каждые 5 минут сразу после сигнальщика.

Требует API-ключи в .env:
  BYBIT_API_KEY=...
  BYBIT_API_SECRET=...

Использование:
  python3 trader.py           — обычный запуск (проверить сигнал и исполнить)
  python3 trader.py --dry-run  — показать что сделал бы, без реальных ордеров
  python3 trader.py --status   — показать баланс и открытые позиции
"""

import hashlib, hmac, json, os, sys, sqlite3, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
TRADES_DB = Path("/home/oleg/workspace/crypto-ton/trades.db")
EXECUTIONS_DB = Path("/home/oleg/workspace/crypto-ton/executions.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# Bybit Testnet
BASE_URL = "https://api-testnet.bybit.com"
SYMBOL = "TONUSDT"
LEVERAGE = 3
MARGIN = 5.20  # фикс для простоты (реинвест позже)

API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()

DRY_RUN = "--dry-run" in sys.argv or (not API_KEY)


# ═══════════════════════════════════════════
#  BYBIT API КЛИЕНТ
# ═══════════════════════════════════════════

def bybit_sign(params: dict, secret: str) -> str:
    """HMAC SHA256 подпись для Bybit API v5."""
    timestamp = str(int(time.time() * 1000))
    params["api_key"] = API_KEY
    params["timestamp"] = timestamp
    
    # Сортируем ключи, собираем строку
    sorted_items = sorted(params.items(), key=lambda x: x[0])
    query_string = urllib.parse.urlencode(sorted_items)
    signature = hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    
    params["sign"] = signature
    return timestamp


def bybit_request(method: str, endpoint: str, params: dict = None, body: dict = None) -> Optional[Dict]:
    """Отправить запрос к Bybit API v5."""
    if DRY_RUN and endpoint not in ["/v5/account/wallet-balance", "/v5/position/list"]:
        print(f"  [DRY RUN] {method} {endpoint} {params or body}")
        return {"retCode": 0, "result": {}, "dryRun": True}
    
    if not API_KEY:
        return None
    
    req_params = params.copy() if params else {}
    sign_params = req_params.copy()
    if body:
        sign_params.update(body)
    
    bybit_sign(sign_params, API_SECRET)
    
    url = BASE_URL + endpoint
    
    try:
        headers = {
            "X-BAPI-API-KEY": API_KEY,
            "X-BAPI-TIMESTAMP": sign_params["timestamp"],
            "X-BAPI-SIGN": sign_params["sign"],
            "X-BAPI-RECV-WINDOW": "10000",
            "Content-Type": "application/json",
        }
        
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("retCode") != 0:
                print(f"  API error: {result.get('retMsg')}", file=sys.stderr)
            return result
    except Exception as e:
        print(f"  Request error: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════
#  ТОРГОВЫЕ ОПЕРАЦИИ
# ═══════════════════════════════════════════

def get_balance() -> Optional[float]:
    """Получить баланс USDT."""
    resp = bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if not resp or resp.get("retCode") != 0:
        if DRY_RUN:
            return 100000.0  # тестнет даёт 100k USDT
        return None
    
    coins = resp["result"]["list"][0]["coin"]
    for coin in coins:
        if coin["coin"] == "USDT":
            return float(coin["walletBalance"])
    return 0.0


def set_leverage():
    """Установить плечо x3 для TONUSDT."""
    return bybit_request("POST", "/v5/position/set-leverage", body={
        "category": "linear",
        "symbol": SYMBOL,
        "buyLeverage": str(LEVERAGE),
        "sellLeverage": str(LEVERAGE),
    })


def place_order(signal_type: str, qty: float, stop_loss: float, take_profit: float, price: float) -> Optional[str]:
    """Разместить рыночный ордер на покупку/продажу."""
    side = "Buy" if signal_type == "BUY" else "Sell"
    
    # Установим позиционный режим (одна позиция на монету)
    bybit_request("POST", "/v5/position/switch-mode", body={
        "category": "linear",
        "symbol": SYMBOL,
        "mode": 0,  # MergedSingle
    })
    
    # Установим плечо
    set_leverage()
    
    # Рыночный ордер
    resp = bybit_request("POST", "/v5/order/create", body={
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",  # immediate or cancel
    })
    
    if not resp or resp.get("retCode") != 0:
        print(f"  Order failed: {resp.get('retMsg') if resp else 'no response'}", file=sys.stderr)
        return None
    
    order_id = resp["result"]["orderId"]
    
    # Поставить стоп-лосс
    sl_side = "Sell" if signal_type == "BUY" else "Buy"
    bybit_request("POST", "/v5/position/trading-stop", body={
        "category": "linear",
        "symbol": SYMBOL,
        "stopLoss": str(stop_loss),
        "takeProfit": str(take_profit),
        "slTriggerBy": "LastPrice",
        "tpTriggerBy": "LastPrice",
        "positionIdx": 0,
    })
    
    return order_id


def close_position(position_idx: int = 0):
    """Закрыть текущую позицию."""
    resp = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": SYMBOL})
    if not resp or resp.get("retCode") != 0:
        return None
    
    positions = resp["result"]["list"]
    if not positions or float(positions[0]["size"]) == 0:
        return None
    
    pos = positions[0]
    side = "Sell" if pos["side"] == "Buy" else "Buy"
    
    close_resp = bybit_request("POST", "/v5/order/create", body={
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": pos["size"],
        "timeInForce": "IOC",
        "reduceOnly": True,
    })
    
    return close_resp.get("result", {}).get("orderId") if close_resp and close_resp.get("retCode") == 0 else None


# ═══════════════════════════════════════════
#  ЖУРНАЛ ИСПОЛНЕНИЯ
# ═══════════════════════════════════════════

def get_last_signal() -> Optional[Dict]:
    """Последний неотработанный сигнал."""
    conn = sqlite3.connect(str(EXECUTIONS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS executions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER, ts TEXT, action TEXT,
        order_id TEXT, entry_price REAL, qty REAL,
        sl REAL, tp REAL, status TEXT
    )""")
    conn.commit()
    
    # Сигнал, для которого ещё нет исполнения
    try:
        row = conn.execute("""
            SELECT s.id, s.ts, s.signal, s.strength, s.price, s.sl, s.tp, s.rsi, s.trend
            FROM signals_v5 s
            LEFT JOIN executions e ON s.id = e.signal_id
            WHERE e.id IS NULL
            ORDER BY s.id DESC LIMIT 1
        """).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("""
                SELECT id, ts, signal, strength, price, sl, tp, rsi, trend
                FROM signals
                ORDER BY id DESC LIMIT 1
            """).fetchone()
        except:
            row = None
        if row:
            row = dict(row)
    conn.close()
    
    if row:
        return dict(zip(["id","ts","signal","strength","price","sl","tp","rsi","trend"], row))
    return None


def record_execution(signal_id: int, action: str, order_id: str, entry_price: float, qty: float, sl: float, tp: float, status: str = "OPEN"):
    conn = sqlite3.connect(str(EXECUTIONS_DB))
    conn.execute("""
        INSERT INTO executions (signal_id, ts, action, order_id, entry_price, qty, sl, tp, status)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (signal_id, datetime.now(UTC_PLUS_3).isoformat(), action, order_id, entry_price, qty, sl, tp, status))
    conn.commit()
    conn.close()


def send_tg(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat = os.getenv("TELEGRAM_HOME_CHANNEL","").strip()
    if not token or not chat: return
    try:
        d = urllib.parse.urlencode({"chat_id":chat,"text":text,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=d),timeout=10)
    except: pass


# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════

if __name__ == "__main__":
    if "--status" in sys.argv:
        balance = get_balance()
        print(f"Баланс: ${balance:,.2f}" if balance else "Баланс: API не настроен")
        sys.exit(0)
    
    if DRY_RUN:
        print("[DRY RUN] Bybit API ключи не найдены — тестовый режим")
    else:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Bybit Trader v1")
    
    # Проверим баланс
    balance = get_balance()
    if balance is None:
        print("  ❌ Не удалось получить баланс. Проверь API ключи.")
        sys.exit(1)
    
    print(f"  Баланс: ${balance:,.0f}")
    
    # Проверим, нет ли уже открытой позиции
    pos_resp = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": SYMBOL})
    has_position = False
    if pos_resp and pos_resp.get("retCode") == 0:
        positions = pos_resp["result"]["list"]
        has_position = any(float(p["size"]) > 0 for p in positions)
    
    if has_position:
        print("  Позиция уже открыта — ждём закрытия")
        sys.exit(0)
    
    # Ищем новый сигнал
    sig = get_last_signal()
    if not sig:
        print("  Нет новых сигналов")
        sys.exit(0)
    
    # Проверим, что сигнал свежий (< 30 минут)
    sig_ts = datetime.fromisoformat(sig["ts"])
    age = (datetime.now(UTC_PLUS_3) - sig_ts).total_seconds() / 60
    if age > 30:
        print(f"  Сигнал устарел ({age:.0f} минут) — пропускаем")
        sys.exit(0)
    
    print(f"  📡 Сигнал: {sig['signal']} ${sig['price']:.4f} | SL=${sig['sl']:.4f} | TP=${sig['tp']:.4f}")
    
    # Размер позиции
    position_usd = MARGIN * LEVERAGE
    qty = position_usd / sig["price"]
    qty = round(qty, 1)  # округляем до 1 десятой TON
    
    if qty < 0.1:
        print(f"  Слишком маленькая позиция ({qty} TON) — пропускаем")
        sys.exit(0)
    
    # Исполняем
    print(f"  ⚡ {sig['signal']} {qty} TON по ~${sig['price']:.4f}...")
    
    order_id = place_order(sig["signal"], qty, sig["sl"], sig["tp"], sig["price"])
    
    if order_id:
        record_execution(sig["id"], sig["signal"], order_id, sig["price"], qty, sig["sl"], sig["tp"])
        
        emoji = "✅" if sig["signal"] == "BUY" else "🔻"
        msg = (
            f"{emoji} <b>СДЕЛКА ИСПОЛНЕНА!</b>\n\n"
            f"{'Куплено' if sig['signal'] == 'BUY' else 'Продано'} <b>{qty} TON</b> ×{LEVERAGE}\n"
            f"Цена: <b>${sig['price']:.4f}</b>\n"
            f"Позиция: <b>${position_usd:.2f}</b>\n"
            f"Стоп-лосс: <b>${sig['sl']:.4f}</b>\n"
            f"Тейк-профит: <b>${sig['tp']:.4f}</b>\n"
            f"\nБаланс: <b>${balance:,.0f} USDT</b>\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
        )
        send_tg(msg)
        print(f"  ✅ Исполнено: order_id={order_id}")
    else:
        send_tg(f"❌ <b>Ошибка исполнения</b> {sig['signal']} — проверь тестнет")
        print(f"  ❌ Не удалось исполнить")
