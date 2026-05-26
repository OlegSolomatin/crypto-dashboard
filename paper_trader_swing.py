#!/usr/bin/env python3
"""
PAPER TRADER SWING — симуляция свинг-сделок с комиссией.
Читает signals_swing, открывает виртуальные позиции, считает P&L.
Комиссия: 0.04% (как Bybit).

Запуск: cron каждую минуту
"""

import sqlite3, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
DATA_DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SWING_DB = Path("/home/oleg/workspace/crypto-ton/swing.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

LEVERAGE = 3
BASE_MARGIN = 5.20
COMMISSION_RATE = 0.0004  # 0.04%


def init_db():
    conn = sqlite3.connect(str(SWING_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS swing_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER, signal_ts TEXT,
        opened_ts TEXT, closed_ts TEXT,
        type TEXT, entry_price REAL, exit_price REAL,
        sl REAL, tp REAL, qty REAL,
        margin REAL, leverage INTEGER,
        pnl REAL, pnl_pct REAL, commission REAL,
        exit_reason TEXT, bars_held INTEGER,
        balance_before REAL, balance_after REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS swing_state (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.commit()
    conn.close()


def get_balance() -> float:
    initial = 100.0
    conn = sqlite3.connect(str(SWING_DB))
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM swing_positions").fetchone()[0]
    total_commission = conn.execute("SELECT COALESCE(SUM(commission),0) FROM swing_positions").fetchone()[0]
    conn.close()
    return round(initial + total_pnl - total_commission, 2)


def get_margin() -> float:
    balance = get_balance()
    extra = max(0, (balance - 100.0) // 10)
    return round(BASE_MARGIN * (1 + extra * 0.05), 2)


def get_latest_price() -> Optional[float]:
    conn = sqlite3.connect(str(DATA_DB))
    row = conn.execute("SELECT price FROM prices ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def get_prices_since(ts: str) -> list:
    """Get all prices after given timestamp."""
    conn = sqlite3.connect(str(DATA_DB))
    rows = conn.execute(
        "SELECT ts, price FROM prices WHERE ts >= ? ORDER BY ts ASC",
        (ts,)
    ).fetchall()
    conn.close()
    return rows


def get_last_signal(conn: sqlite3.Connection) -> Optional[dict]:
    """Get the latest unprocessed swing signal."""
    # Find the signal_id after the last processed one
    last_processed = conn.execute(
        "SELECT COALESCE(MAX(signal_id), 0) FROM swing_positions"
    ).fetchone()[0]

    sig_conn = sqlite3.connect(str(SIGNALS_DB))
    row = sig_conn.execute(
        "SELECT id, ts, signal, price, sl, tp, rsi, trend FROM signals_swing "
        "WHERE id > ? ORDER BY id ASC LIMIT 1",
        (last_processed,)
    ).fetchone()
    sig_conn.close()

    if not row:
        return None
    return {
        "id": row[0], "ts": row[1], "signal": row[2],
        "price": row[3], "sl": row[4], "tp": row[5],
        "rsi": row[6], "trend": row[7]
    }


def open_position(sig: dict, balance: float, margin: float):
    """Open a virtual swing position."""
    entry = sig["price"]
    pos_type = "BUY" if "BUY" in sig["signal"].upper() else "SELL"
    pos_size = round(margin * LEVERAGE / entry, 4)
    commission = round(pos_size * entry * COMMISSION_RATE, 4)

    conn = sqlite3.connect(str(SWING_DB))
    conn.execute(
        """INSERT INTO swing_positions 
        (signal_id, signal_ts, opened_ts, type, entry_price, sl, tp, qty, margin, leverage, 
         commission, balance_before)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sig["id"], sig["ts"], datetime.now(UTC_PLUS_3).isoformat(),
         pos_type, entry, sig["sl"], sig["tp"], pos_size, margin, LEVERAGE,
         commission, balance)
    )
    conn.commit()
    pos_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return pos_id, commission


def check_position(pos: tuple, prices: list) -> Optional[tuple]:
    """Check if SL/TP/timeout hit. Returns (exit_price, reason, bars) or None."""
    pos_id, pos_type, entry, sl, tp, opened_ts = pos

    for i, (ts, price) in enumerate(prices):
        if pos_type == "BUY":
            if price <= sl:
                return (sl, "STOP_LOSS", i + 1)
            if price >= tp:
                return (tp, "TAKE_PROFIT", i + 1)
        else:  # SELL
            if price >= sl:
                return (sl, "STOP_LOSS", i + 1)
            if price <= tp:
                return (tp, "TAKE_PROFIT", i + 1)

    return None


def close_position(pos_id: int, exit_price: float, reason: str, bars: int):
    """Close position and calculate P&L."""
    conn = sqlite3.connect(str(SWING_DB))
    pos = conn.execute(
        "SELECT type, entry_price, qty, margin, commission, balance_before FROM swing_positions WHERE id=?",
        (pos_id,)
    ).fetchone()
    if not pos:
        conn.close()
        return

    pos_type, entry, qty, margin, commission, balance_before = pos
    position_usd = qty * entry

    # P&L calculation
    if pos_type == "BUY":
        gross_pnl = (exit_price - entry) * qty
    else:
        gross_pnl = (entry - exit_price) * qty

    exit_commission = qty * exit_price * COMMISSION_RATE
    total_commission = commission + exit_commission
    net_pnl = round(gross_pnl - total_commission, 4)
    pnl_pct = round(gross_pnl / position_usd * 100, 2)
    balance_after = round(balance_before + net_pnl, 2)

    now = datetime.now(UTC_PLUS_3).isoformat()
    conn.execute(
        """UPDATE swing_positions SET 
        closed_ts=?, exit_price=?, pnl=?, pnl_pct=?, 
        exit_reason=?, bars_held=?, balance_after=?
        WHERE id=?""",
        (now, exit_price, net_pnl, pnl_pct, reason, bars, balance_after, pos_id)
    )
    conn.commit()

    # Сохраняем в swing_state для отчётов
    conn.execute(
        "INSERT OR REPLACE INTO swing_state (key, value) VALUES (?,?)",
        ("last_close_ts", now)
    )
    conn.commit()
    conn.close()

    return net_pnl, pnl_pct, total_commission


def send_trading_tg(msg: str):
    """Send notification to trading Telegram bot."""
    import urllib.request, urllib.parse
    try:
        bot_token = os.environ.get("TRADING_BOT_TOKEN", "")
        chat_id = os.environ.get("TRADING_CHAT_ID", "")
        if not bot_token:
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
        if not bot_token:
            return

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass


def main():
    init_db()

    # Check open positions
    conn = sqlite3.connect(str(SWING_DB))
    open_positions = conn.execute(
        "SELECT id, type, entry_price, sl, tp, opened_ts FROM swing_positions WHERE closed_ts IS NULL"
    ).fetchall()
    conn.close()

    # Check if any open positions should be closed
    for pos in open_positions:
        prices = get_prices_since(pos[5])  # since opened_ts
        if prices:
            result = check_position(pos, prices)
            if result:
                exit_price, reason, bars = result
                pnl, pnl_pct, commission = close_position(pos[0], exit_price, reason, bars)

                # Check timeout
                opened_dt = datetime.fromisoformat(pos[5])
                timeout_hours = 24
                if (datetime.now(UTC_PLUS_3) - opened_dt).total_seconds() > timeout_hours * 3600:
                    exit_price = prices[-1][1] if prices else pos[2]
                    pnl, pnl_pct, commission = close_position(pos[0], exit_price, "TIMEOUT", len(prices))

                # Notify
                emoji = "🟢" if pnl > 0 else "🔴"
                send_trading_tg(
                    f"{emoji} <b>SWING {pos[1]}</b>\n"
                    f"Выход: {exit_price:.2f} | {reason}\n"
                    f"P&L: {pnl:+.4f} USD ({pnl_pct:+.2f}%)\n"
                    f"Комиссия: {commission:.4f} USD\n"
                    f"Баланс: {get_balance():.2f} USD"
                )

    # Process new signals
    swing_conn = sqlite3.connect(str(SWING_DB))
    sig = get_last_signal(swing_conn)
    swing_conn.close()

    if sig:
        balance = get_balance()
        margin = get_margin()
        pos_id, commission = open_position(sig, balance, margin)

        emoji = "🟢" if "BUY" in sig["signal"].upper() else "🔴"
        send_trading_tg(
            f"{emoji} <b>SWING {sig['signal']}</b>\n"
            f"Вход: {sig['price']:.2f} USD | RSI={sig['rsi']:.1f}\n"
            f"SL: {sig['sl']:.4f} | TP: {sig['tp']:.4f}\n"
            f"Маржа: {margin:.2f} USD x{LEVERAGE}\n"
            f"Комиссия: {commission:.4f} USD\n"
            f"Баланс: {balance:.2f} USD"
        )


if __name__ == "__main__":
    main()
