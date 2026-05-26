#!/usr/bin/env python3
"""
SWING REPORT — отчёт каждые 5 дней со свечным анализом.
Анализирует сделки из swing_positions, строит свечной контекст.
"""
import sqlite3, json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

SWING_DB = Path("/home/oleg/workspace/crypto-ton/swing.db")
DATA_DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))


def get_trades_since(days: int = 5):
    """Get all trades from last N days."""
    cutoff = (datetime.now(UTC_PLUS_3) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(SWING_DB))
    trades = conn.execute(
        """SELECT id, signal_id, type, entry_price, exit_price, sl, tp, 
           pnl, pnl_pct, commission, exit_reason, bars_held,
           opened_ts, closed_ts, balance_before, balance_after
        FROM swing_positions 
        WHERE closed_ts >= ? OR (opened_ts >= ? AND closed_ts IS NULL)
        ORDER BY opened_ts""",
        (cutoff, cutoff)
    ).fetchall()
    conn.close()
    return trades


def get_candles_around(ts: str, count: int = 5):
    """Get candles before and after a timestamp."""
    conn = sqlite3.connect(str(DATA_DB))
    rows = conn.execute(
        """SELECT ts, open, high, low, close FROM candles 
        WHERE ts <= ? ORDER BY ts DESC LIMIT ?""",
        (ts, count)
    ).fetchall()
    rows_before = list(reversed(rows))
    rows_after = conn.execute(
        """SELECT ts, open, high, low, close FROM candles 
        WHERE ts > ? ORDER BY ts ASC LIMIT ?""",
        (ts, count)
    ).fetchall()
    conn.close()
    return rows_before, rows_after


def candle_analysis(entry_price: float, exit_price: float, pos_type: str, opened_ts: str):
    """Анализ свечного контекста для точки входа и выхода."""
    analysis = []

    # Вход
    before, after = get_candles_around(opened_ts)
    if before:
        last_candle = before[-1]
        candle_body = abs(last_candle[4] - last_candle[1])  # close - open
        candle_range = last_candle[3] - last_candle[2]  # high - low
        body_pct = round(candle_body / candle_range * 100, 1) if candle_range > 0 else 0

        if pos_type == "BUY":
            if last_candle[4] > last_candle[1]:  # green candle
                analysis.append("🟢 Вход на зелёной свече (+ подтверждение тренда)")
            else:
                analysis.append("🔴 Вход на красной свече (контр-тренд)")
        else:
            if last_candle[4] < last_candle[1]:  # red candle
                analysis.append("🔴 Вход на красной свече (+ подтверждение тренда)")
            else:
                analysis.append("🟢 Вход на зелёной свече (контр-тренд)")

        analysis.append(f"   Тело свечи: {body_pct}% от диапазона")

    # Размер свечи при входе
    if before:
        last = before[-1]
        move_pct = round(abs(last[4] - last[1]) / last[1] * 100, 2)
        analysis.append(f"   Движение свечи входа: {move_pct}%")

    # Выход
    if exit_price:
        after_entry = after if after else []
        if after_entry:
            exit_candle = None
            for c in after_entry + before:
                if min(c[2], c[3]) <= exit_price <= max(c[2], c[3]):
                    exit_candle = c
                    break

            if pos_type == "BUY":
                if exit_price > entry_price:
                    analysis.append("✅ Выход ВЫШЕ входа (профит)")
                else:
                    analysis.append("❌ Выход НИЖЕ входа (убыток)")
            else:
                if exit_price < entry_price:
                    analysis.append("✅ Выход НИЖЕ входа (профит)")
                else:
                    analysis.append("❌ Выход ВЫШЕ входа (убыток)")

            profit_pct = abs(exit_price - entry_price) / entry_price * 100
            analysis.append(f"   От входа до выхода: {profit_pct:.2f}% движения цены")

    return analysis


def generate_report(days: int = 5):
    """Generate swing report for last N days."""
    trades = get_trades_since(days)
    closed_trades = [t for t in trades if t[13] is not None]  # closed_ts not null
    open_trades = [t for t in trades if t[13] is None]

    report = []
    report.append("=" * 45)
    report.append(f"📊 SWING-ОТЧЁТ за {days} дн. ({datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M МСК')})")
    report.append("=" * 45)

    # Статистика
    if closed_trades:
        wins = [t for t in closed_trades if t[7] > 0]
        losses = [t for t in closed_trades if t[7] <= 0]
        total_pnl = sum(t[7] for t in closed_trades)
        total_commission = sum(t[9] for t in closed_trades)
        net_pnl = total_pnl - total_commission

        report.append(f"\n📈 СДЕЛКИ ({len(closed_trades)}):")
        report.append(f"   Побед: {len(wins)} | Поражений: {len(losses)}")
        report.append(f"   Win rate: {len(wins)/len(closed_trades)*100:.1f}%" if closed_trades else "   Нет сделок")
        report.append(f"   Gross P&L: {total_pnl:+.4f} USD")
        report.append(f"   Комиссия: {total_commission:.4f} USD")
        report.append(f"   Net P&L: {net_pnl:+.4f} USD")

        avg_win = sum(t[7] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t[7] for t in losses) / len(losses) if losses else 0
        report.append(f"   Ср. победа: +{avg_win:.4f} USD | Ср. убыток: {avg_loss:.4f} USD")

        # Exit reasons
        reasons = {}
        for t in closed_trades:
            reason = t[10] or "UNKNOWN"
            reasons[reason] = reasons.get(reason, 0) + 1
        report.append(f"\n📤 Причины выхода:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            report.append(f"   {reason}: {count}")

    # Open positions
    if open_trades:
        report.append(f"\n🔓 ОТКРЫТЫЕ ПОЗИЦИИ ({len(open_trades)}):")
        for t in open_trades:
            pnl_unreal = 0
            report.append(f"   {t[2]} вх={t[3]:.4f} SL={t[5]:.4f} TP={t[6]:.4f} | откр: {t[12][:16]}")

    # Свечной анализ для последних 3 сделок
    recent = closed_trades[-3:] if closed_trades else []
    if recent:
        report.append(f"\n🕯️ СВЕЧНОЙ АНАЛИЗ (последние сделки):")
        for t in recent:
            pos_type, entry, exit_price = t[2], t[3], t[4]
            opened_ts = t[12]
            report.append(f"\n   {'─'*30}")
            report.append(f"   {pos_type}: {entry:.4f} → {exit_price:.4f}")
            candles = candle_analysis(entry, exit_price, pos_type, opened_ts)
            for line in candles:
                report.append(f"   {line}")

    # Сигналы без сделок
    conn = sqlite3.connect(str(SIGNALS_DB))
    cutoff = (datetime.now(UTC_PLUS_3) - timedelta(days=days)).isoformat()
    total_signals = conn.execute(
        "SELECT COUNT(*) FROM signals_swing WHERE ts >= ?", (cutoff,)
    ).fetchone()[0]
    conn.close()
    report.append(f"\n📡 Сигналов за период: {total_signals}")
    report.append(f"   → Сделок: {len(closed_trades)}")

    report.append(f"\n{'='*45}")
    return "\n".join(report)


if __name__ == "__main__":
    report = generate_report()
    print(report)

    # Save to file
    out_path = Path("/home/oleg/workspace/crypto-ton") / f"swing_report_{datetime.now(UTC_PLUS_3).strftime('%Y%m%d_%H%M')}.txt"
    out_path.write_text(report)
    print(f"\nОтчёт сохранён: {out_path}")
