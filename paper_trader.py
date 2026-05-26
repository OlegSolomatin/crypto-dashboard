#!/usr/bin/env python3
"""
PAPER TRADING — симуляция сделок на РЕАЛЬНЫХ данных с полным P&L-трекингом.
Запускается каждые 5 минут (сразу после signal_v5.py).

НЕ требует API-ключей — работает локально с data.db.
Все сделки виртуальные, но на реальных ценах.
"""

import sqlite3, json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
TRADES_DB = Path("/home/oleg/workspace/crypto-ton/trades.db")
DATA_DB = Path("/home/oleg/workspace/crypto-ton/data.db")
PAPER_DB = Path("/home/oleg/workspace/crypto-ton/paper.db")
ONCHAIN_DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

LEVERAGE = 3
BASE_MARGIN = 5.20

# ═══════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ
# ═══════════════════════════════════════════

def init_paper_db():
    conn = sqlite3.connect(str(PAPER_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER, signal_ts TEXT,
        opened_ts TEXT, closed_ts TEXT,
        type TEXT, entry_price REAL, exit_price REAL,
        sl REAL, tp REAL, qty REAL,
        margin REAL, leverage INTEGER,
        pnl REAL, pnl_pct REAL,
        exit_reason TEXT, bars_held INTEGER,
        balance_before REAL, balance_after REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_state (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.commit()
    conn.close()


def get_balance() -> float:
    """Текущий виртуальный баланс."""
    initial = 100.0
    conn = sqlite3.connect(str(PAPER_DB))
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_positions").fetchone()[0]
    conn.close()
    return round(initial + total_pnl, 2)


def get_margin() -> float:
    balance = get_balance()
    extra = max(0, (balance - 100.0) // 10)
    return round(BASE_MARGIN * (1 + extra * 0.05), 2)


def get_open_position() -> Optional[Dict]:
    conn = sqlite3.connect(str(PAPER_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM paper_positions WHERE closed_ts IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_recent_performance() -> Dict:
    conn = sqlite3.connect(str(PAPER_DB))
    rows = conn.execute(
        "SELECT type, pnl, pnl_pct, exit_reason, bars_held FROM paper_positions "
        "WHERE closed_ts IS NOT NULL ORDER BY id DESC LIMIT 30"
    ).fetchall()
    conn.close()
    
    if not rows:
        return {"trades": 0}
    
    wins = [r for r in rows if r[1] > 0]
    losses = [r for r in rows if r[1] <= 0]
    
    reasons = {}
    for r in rows:
        reasons[r[3]] = reasons.get(r[3], 0) + 1
    
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins)/len(rows)*100, 1),
        "avg_win": round(sum(r[1] for r in wins)/len(wins), 2) if wins else 0,
        "avg_loss": round(sum(r[1] for r in losses)/len(losses), 2) if losses else 0,
        "total_pnl": round(sum(r[1] for r in rows), 2),
        "exit_reasons": reasons,
    }


def get_consecutive_losses() -> int:
    conn = sqlite3.connect(str(PAPER_DB))
    rows = conn.execute(
        "SELECT pnl FROM paper_positions WHERE closed_ts IS NOT NULL ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()
    count = 0
    for (pnl,) in rows:
        if pnl < 0: count += 1
        else: break
    return count


# ═══════════════════════════════════════════
#  ИСПОЛНЕНИЕ СДЕЛОК
# ═══════════════════════════════════════════

def get_last_signal() -> Optional[Dict]:
    """Последний неотработанный сигнал из signals_v6 (приоритет) или signals_v5 (fallback)."""
    try:
        # Сначала получаем все ID уже открытых позиций
        paper_conn = sqlite3.connect(str(PAPER_DB))
        used_ids = paper_conn.execute("SELECT DISTINCT signal_id FROM paper_positions").fetchall()
        paper_conn.close()
        used_set = {r[0] for r in used_ids}
        
        conn = sqlite3.connect(str(SIGNALS_DB))
        conn.row_factory = sqlite3.Row
        
        # Пробуем signals_v6 (новый формат)
        try:
            rows = conn.execute("""
                SELECT id, ts, signal, strength, price, sl, tp, rsi, trend
                FROM signals_v6
                ORDER BY id DESC LIMIT 50
            """).fetchall()
            if rows:
                for row in rows:
                    d = dict(row)
                    if d["id"] not in used_set:
                        conn.close()
                        return d
        except sqlite3.OperationalError:
            pass
        
        # Fallback: signals_v5
        rows = conn.execute("""
            SELECT id, ts, signal, strength, price, sl, tp, rsi, trend
            FROM signals_v5
            ORDER BY id DESC LIMIT 50
        """).fetchall()
        conn.close()
        
        for row in rows:
            d = dict(row)
            if d["id"] not in used_set:
                return d
        return None
    except sqlite3.OperationalError:
        return None


def open_paper_position(signal_id: int, signal_ts: str, sig_type: str, price: float, sl: float, tp: float):
    """Открыть виртуальную позицию."""
    balance = get_balance()
    margin = get_margin()
    position_usd = margin * LEVERAGE
    qty = round(position_usd / price, 1)
    
    conn = sqlite3.connect(str(PAPER_DB))
    conn.execute("""
        INSERT INTO paper_positions (signal_id, signal_ts, opened_ts, type,
            entry_price, sl, tp, qty, margin, leverage, balance_before)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (signal_id, signal_ts, datetime.now(UTC_PLUS_3).isoformat(), sig_type,
          price, sl, tp, qty, margin, LEVERAGE, balance))
    conn.commit()
    conn.close()
    
    return {"balance": balance, "margin": margin, "position": position_usd, "qty": qty}


def check_and_close_positions():
    """Проверить все открытые позиции — не сработали ли SL/TP."""
    pos = get_open_position()
    if not pos:
        return
    
    # Проверяем историю цен с момента открытия
    entry_ts = pos["opened_ts"]
    
    conn = sqlite3.connect(str(DATA_DB))
    rows = conn.execute(
        "SELECT last_price, timestamp FROM prices WHERE timestamp > ? ORDER BY id ASC",
        (entry_ts,)
    ).fetchall()
    conn.close()
    
    if not rows:
        return
    
    current_price = rows[-1][0]
    bars_held = len(rows)
    exit_price = None
    reason = ""
    
    # Проверяем каждую свечу
    for (price, ts) in rows:
        if pos["type"] == "BUY":
            if price <= pos["sl"]:
                exit_price = pos["sl"]; reason = "STOP_LOSS"; break
            if price >= pos["tp"]:
                exit_price = pos["tp"]; reason = "TAKE_PROFIT"; break
        else:  # SELL
            if price >= pos["sl"]:
                exit_price = pos["sl"]; reason = "STOP_LOSS"; break
            if price <= pos["tp"]:
                exit_price = pos["tp"]; reason = "TAKE_PROFIT"; break
    
    # Тайм-аут
    if not exit_price and bars_held >= 180:
        exit_price = current_price
        reason = "TIMEOUT"
    
    if exit_price:
        close_paper_position(pos["id"], exit_price, reason, bars_held, pos)


def close_paper_position(pos_id: int, exit_price: float, reason: str, bars_held: int, pos: Dict):
    """Закрыть виртуальную позицию и посчитать P&L."""
    entry = pos["entry_price"]
    margin = pos["margin"]
    qty = pos["qty"]
    balance_before = pos["balance_before"]
    
    # P&L
    if pos["type"] == "BUY":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
    
    position_usd = margin * LEVERAGE
    pnl_dollars = qty * pnl_pct / 100 * LEVERAGE * exit_price
    Commission = position_usd * 0.0004
    pnl_dollars -= Commission
    
    balance_after = round(balance_before + pnl_dollars, 2)
    
    conn = sqlite3.connect(str(PAPER_DB))
    conn.execute("""
        UPDATE paper_positions SET
            closed_ts=?, exit_price=?, exit_reason=?, bars_held=?,
            pnl=?, pnl_pct=?, balance_after=?
        WHERE id=?
    """, (datetime.now(UTC_PLUS_3).isoformat(), exit_price, reason, bars_held,
          round(pnl_dollars, 4), round(pnl_pct, 4), balance_after, pos_id))
    conn.commit()
    conn.close()
    
    # Telegram-уведомление о закрытии
    emoji = "✅" if pnl_dollars > 0 else "❌"
    msg = (
        f"{emoji} <b>СДЕЛКА ЗАКРЫТА</b> ({reason})\n\n"
        f"{pos['type']} TON/USDT ×{LEVERAGE}\n"
        f"Вход: <b>${entry:.4f}</b> → Выход: <b>${exit_price:.4f}</b>\n"
        f"P&amp;L: <b>{pnl_dollars:+.2f}$</b> ({pnl_pct:+.2f}%)\n"
        f"Держали: <b>{bars_held} мин</b> (~{bars_held/60:.1f} ч)\n"
        f"Баланс: ${balance_before:.0f} → <b>${balance_after:.0f}</b>\n"
        f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )
    send_tg(msg)
    
    # Проверить 3 убытка подряд
    if get_consecutive_losses() >= 3:
        trigger_low_perf_alert()


# ═══════════════════════════════════════════
#  АЛЕРТ
# ═══════════════════════════════════════════

def trigger_low_perf_alert():
    perf = get_recent_performance()
    msg = (
        f"🧠 <b>3 УБЫТКА ПОДРЯД — НУЖЕН АНАЛИЗ</b>\n\n"
        f"Статистика: {perf.get('trades',0)} сделок | Винрейт {perf.get('win_rate','?')}%\n"
        f"Общий P&amp;L: {perf.get('total_pnl','?')}$\n"
        f"Баланс: <b>${get_balance():.0f}</b>\n\n"
        f"Отправь <b>/analyze</b> для глубокого анализа и предложений по улучшению стратегии."
    )
    send_tg(msg)


# ═══════════════════════════════════════════
#  ОТЧЁТ
# ═══════════════════════════════════════════

def daily_report():
    """Полный отчёт для Telegram."""
    balance = get_balance()
    perf = get_recent_performance()
    
    # Последние сделки
    lines = []
    lines.append("📊 <b>PAPER TRADING — СУТОЧНЫЙ ОТЧЁТ</b>")
    lines.append("")
    lines.append(f"Баланс: <b>${balance:.0f}</b> ({(balance/100-1)*100:+.1f}%)")
    lines.append(f"Сделок: {perf.get('trades',0)} | Винрейт: {perf.get('win_rate','?')}%")
    lines.append(f"P&amp;L: {perf.get('total_pnl',0):+.2f}$")
    lines.append(f"Средний выигрыш: +${perf.get('avg_win',0):.2f} | Средний убыток: -${abs(perf.get('avg_loss',0)):.2f}")
    if perf.get('exit_reasons'):
        reasons = ", ".join(f"{k}: {v}" for k,v in perf['exit_reasons'].items())
        lines.append(f"Выходы: {reasons}")
    
    lines.append("")
    lines.append(f"{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК")
    
    send_tg("\n".join(lines))


# ═══════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════

def send_tg(text):
    """Отправляет в трейдинг-бота (приоритет) или основного (fallback)."""
    trading_token = os.getenv("TRADING_BOT_TOKEN","").strip()
    chat_id = os.getenv("TRADING_CHAT_ID","").strip()
    
    if not trading_token or not chat_id:
        trading_token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
        chat_id = os.getenv("TELEGRAM_HOME_CHANNEL","").strip()
    
    if not trading_token or not chat_id:
        return
    try:
        d = urllib.parse.urlencode({"chat_id":chat_id,"text":text,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{trading_token}/sendMessage", data=d), timeout=10)
    except Exception as e:
        print(f"  Telegram: {e}", file=sys.stderr)


# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════

if __name__ == "__main__":
    init_paper_db()
    
    # Проверить открытые позиции
    check_and_close_positions()
    
    # Если уже есть открытая → не открываем новую
    if get_open_position():
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Позиция открыта — ждём закрытия")
        balance = get_balance()
        perf = get_recent_performance()
        print(f"  Баланс: ${balance:.0f} | Сделок: {perf.get('trades',0)} | Винрейт: {perf.get('win_rate','?')}%")
        sys.exit(0)
    
    # Ищем новый сигнал
    sig = get_last_signal()
    if not sig:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Нет новых сигналов")
        sys.exit(0)
    
    # Сигнал не старше 10 минут
    sig_ts = datetime.fromisoformat(sig["ts"])
    age = (datetime.now(UTC_PLUS_3) - sig_ts).total_seconds() / 60
    if age > 10:
        print(f"  Сигнал устарел ({age:.0f} мин) — пропускаем")
        sys.exit(0)
    
    # Открываем позицию
    result = open_paper_position(
        sig["id"], sig["ts"], sig["signal"],
        sig["price"], sig["sl"], sig["tp"]
    )
    
    emoji = "📈" if sig["signal"] == "BUY" else "📉"
    msg = (
        f"{emoji} <b>PAPER TRADE ОТКРЫТА</b>\n\n"
        f"{sig['signal']} TON/USDT ×{LEVERAGE}\n"
        f"Вход: <b>${sig['price']:.4f}</b>\n"
        f"Позиция: <b>${result['position']:.2f}</b> ({result['qty']} TON)\n"
        f"Маржа: <b>${result['margin']:.2f}</b>\n"
        f"Стоп-лосс: <b>${sig['sl']:.4f}</b>\n"
        f"Тейк-профит: <b>${sig['tp']:.4f}</b>\n"
        f"Баланс: <b>${result['balance']:.0f}$</b>\n"
        f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )
    send_tg(msg)
    
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] {sig['signal']} открыт "
          f"@{sig['price']:.4f} ({result['qty']} TON ×{LEVERAGE}) | "
          f"SL={sig['sl']:.4f} TP={sig['tp']:.4f} | Баланс ${result['balance']:.0f}")
