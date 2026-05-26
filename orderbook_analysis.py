#!/usr/bin/env python3
"""
АНАЛИТИКА СТАКАНА — ключевые метрики для трейдинга.
Читает stream.db каждые 5 минут, анализирует дисбаланс спроса/предложения.

Метрики:
  • Спред (bid-ask) — узкий спред = ликвидность
  • Глубина стакана — сколько TON в пределах ±1% от цены
  • Дисбаланс (bid_depth / ask_depth) — перекос спроса
  • Крупные ордера (стены) — где скопления > 5000 TON
  • Имбаланс сделок (buy vs sell volume)

Использование:
  python3 orderbook_analysis.py          # однократный анализ
  python3 orderbook_analysis.py --alerts # с отправкой в Telegram
"""

import sqlite3, json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

STREAM_DB = Path("/home/oleg/workspace/crypto-ton/stream.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

def get_latest_snapshot():
    conn = sqlite3.connect(str(STREAM_DB))
    row = conn.execute("""
        SELECT ts, best_bid, best_ask, spread, spread_pct,
               bid_depth_1pct, ask_depth_1pct, bids_json, asks_json
        FROM orderbook_snapshots ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    
    if not row:
        return None
    
    return {
        "ts": row[0], "best_bid": row[1], "best_ask": row[2],
        "spread": row[3], "spread_pct": row[4],
        "bid_depth": row[5], "ask_depth": row[6],
        "bids": json.loads(row[7]) if row[7] else [],
        "asks": json.loads(row[8]) if row[8] else [],
    }


def get_trade_imbalance(minutes: int = 5):
    """Объём покупок vs продаж за последние N минут."""
    conn = sqlite3.connect(str(STREAM_DB))
    since = datetime.now(UTC_PLUS_3).isoformat()
    rows = conn.execute("""
        SELECT side, SUM(value) FROM trades_stream
        WHERE ts > datetime('now', 'localtime', ?)
        GROUP BY side
    """, (f"-{minutes} minutes",)).fetchall()
    conn.close()
    
    buy_vol = sum(r[1] for r in rows if r[0] == "Buy")
    sell_vol = sum(r[1] for r in rows if r[0] == "Sell")
    total = buy_vol + sell_vol
    
    if total == 0:
        return None
    
    return {
        "buy_volume": round(buy_vol, 2),
        "sell_volume": round(sell_vol, 2),
        "buy_ratio": round(buy_vol / total * 100, 1),
        "total_volume": round(total, 2),
    }


def find_walls(bids: list, asks: list, threshold: float = 5000):
    """Найти «стены» в стакане — крупные скопления ордеров."""
    bid_walls = [b for b in bids if b[1] >= threshold]
    ask_walls = [a for a in asks if a[1] >= threshold]
    return bid_walls, ask_walls


def analyze():
    snap = get_latest_snapshot()
    if not snap:
        return "Стакан: нет данных (WebSocket не запущен?)"
    
    imb = get_trade_imbalance(5)
    bid_walls, ask_walls = find_walls(snap["bids"], snap["asks"])
    
    lines = []
    lines.append(f"Цена: {snap['best_bid']:.4f} / {snap['best_ask']:.4f}")
    lines.append(f"Спред: {snap['spread']:.4f} ({snap['spread_pct']:.4f}%)")
    lines.append(f"Глубина ±1%: бид {snap['bid_depth']:,.0f} TON | аск {snap['ask_depth']:,.0f} TON")
    
    # Дисбаланс
    if snap["bid_depth"] > 0 and snap["ask_depth"] > 0:
        ratio = snap["bid_depth"] / snap["ask_depth"]
        if ratio > 1.5:
            lines.append(f"⚡ Дисбаланс: покупателей в {ratio:.1f}x больше — давление ВВЕРХ")
        elif ratio < 0.67:
            lines.append(f"⚡ Дисбаланс: продавцов в {1/ratio:.1f}x больше — давление ВНИЗ")
        else:
            lines.append(f"Дисбаланс: {ratio:.2f} — нейтрально")
    
    # Стены
    if bid_walls:
        lines.append(f"Стена покупок: {' '.join(f'{w[0]:.4f}({w[1]:,.0f})' for w in bid_walls[:3])}")
    if ask_walls:
        lines.append(f"Стена продаж: {' '.join(f'{w[0]:.4f}({w[1]:,.0f})' for w in ask_walls[:3])}")
    
    # Имбаланс сделок
    if imb:
        lines.append(f"Сделки за 5 мин: покупок {imb['buy_ratio']}% ({imb['total_volume']:,.0f}$)")
    
    # Топ уровней
    lines.append("Топ-3 бид: " + " | ".join(f"{b[0]:.4f} ({b[1]:,.0f})" for b in snap["bids"][:3]))
    lines.append("Топ-3 аск: " + " | ".join(f"{a[0]:.4f} ({a[1]:,.0f})" for a in snap["asks"][:3]))
    
    return {
        "text": "\n".join(lines),
        "wall_alert": len(bid_walls) + len(ask_walls) > 0,
        "imbalance_alert": snap["bid_depth"] / max(snap["ask_depth"], 1) > 2 or snap["ask_depth"] / max(snap["bid_depth"], 1) > 2,
    }


def send_tg_alert(result: dict):
    token = os.getenv("TRADING_BOT_TOKEN", "").strip()
    chat = os.getenv("TRADING_CHAT_ID", "").strip()
    if not token or not chat:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token or not chat:
        return
    
    if not result.get("wall_alert") and not result.get("imbalance_alert"):
        return  # нет значимых событий
    
    msg = f"📊 <b>АНАЛИЗ СТАКАНА TON</b>\n\n{result['text']}\n\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    
    try:
        d = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=d), timeout=10)
    except:
        pass


if __name__ == "__main__":
    result = analyze()
    if isinstance(result, str):
        print(result)
    else:
        print(result["text"])
        if "--alerts" in sys.argv:
            send_tg_alert(result)
    
    # Покажем также историю
    conn = sqlite3.connect(str(STREAM_DB))
    count_t = conn.execute("SELECT COUNT(*) FROM tickers").fetchone()[0]
    count_b = conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0]
    count_tr = conn.execute("SELECT COUNT(*) FROM trades_stream").fetchone()[0]
    conn.close()
    print(f"\nБаза: {count_t} тикеров | {count_b} снапшотов стакана | {count_tr} сделок")
