#!/usr/bin/env python3
"""
Бэктестер стратегии TON/USDT на исторических данных.
Прогоняет стратегию на всей доступной истории и считает метрики.

Использование:
  python3 backtest.py
"""

import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))


# ========= ИНДИКАТОРЫ (идентичны analysis.py) =========

def sma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = prices[i] - prices[i-1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    for i in range(period + 1, len(prices)):
        diff = prices[i] - prices[i-1]
        gain = diff if diff >= 0 else 0.0
        loss = abs(diff) if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def volume_spike(volumes: List[float]) -> Optional[float]:
    if len(volumes) < 5:
        return None
    avg = sum(volumes[:-1]) / len(volumes[:-1])
    if avg == 0:
        return None
    return volumes[-1] / avg


# ========= СИГНАЛЫ (переписанные) =========

def generate_signal(prices: List[float], volumes: List[float]) -> Optional[Dict[str, Any]]:
    """
    УЛУЧШЕННАЯ версия — возвращает точные уровни стоп-лосс и тейк-профит.
    
    BUY: цена>EMA20 + RSI<35 + объём>1.3x
    SELL: RSI>75 ИЛИ цена<SMA7+ниже полосы Боллинджера
    """
    if len(prices) < 30:
        return None
    
    current = prices[-1]
    sma7_val = sma(prices, 7)
    sma20_val = sma(prices, 20)
    rsi_val = rsi(prices, 14)
    vol_ratio = volume_spike(volumes) if volumes else None
    
    # Bollinger (SMA20 ± 2σ)
    import math
    bb_upper = bb_lower = None
    if sma20_val:
        recent = prices[-20:]
        var = sum((p - sma20_val) ** 2 for p in recent) / 20
        std = math.sqrt(var)
        bb_upper = sma20_val + 2 * std
        bb_lower = sma20_val - 2 * std
    
    # Тренд
    trend = "нейтральный"
    if sma7_val and sma20_val:
        if sma7_val > sma20_val:
            trend = "↑ бычий"
        else:
            trend = "↓ медвежий"
    
    # === BUY ===
    buy_ok = (
        sma20_val and current > sma20_val
        and rsi_val is not None and rsi_val < 35
        and vol_ratio is not None and vol_ratio > 1.3
    )
    buy_score = sum([
        bool(sma20_val and current > sma20_val),
        bool(rsi_val is not None and rsi_val < 35),
        bool(vol_ratio is not None and vol_ratio > 1.3),
    ])
    
    # === SELL ===
    rsi_overbought = rsi_val is not None and rsi_val > 75
    breakdown = (
        sma7_val and current < sma7_val
        and bb_lower and current < bb_lower
    )
    sell_score = sum([rsi_overbought, breakdown])
    
    # Определить сигнал
    if buy_ok:
        signal = "BUY"
        strength = "STRONG" if buy_score == 3 else "WEAK"
        # Стоп-лосс: -2% от входа ИЛИ ниже нижней полосы Боллинджера
        stop_loss = round(min(current * 0.98, bb_lower or current * 0.97), 4)
        # Тейк-профит: SMA20 + ширина полосы (цель внизу канала)
        take_profit = round(current + (bb_upper - current) * 0.618 if bb_upper else current * 1.03, 4)
    elif sell_score >= 2:
        signal = "SELL"
        strength = "STRONG" if sell_score >= 3 else "WEAK"
        stop_loss = round(max(current * 1.02, bb_upper or current * 1.03), 4)
        take_profit = round(bb_lower if bb_lower else current * 0.97, 4)
    else:
        return None
    
    # Соотношение риск/прибыль
    risk = abs(stop_loss - current)
    reward = abs(take_profit - current)
    rr_ratio = round(reward / risk, 1) if risk > 0 else 0
    
    return {
        "signal": signal,
        "strength": strength,
        "price": round(current, 4),
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr_ratio": rr_ratio,
        "sma7": round(sma7_val or 0, 4),
        "sma20": round(sma20_val or 0, 4),
        "rsi": round(rsi_val, 1) if rsi_val else None,
        "vol_ratio": round(vol_ratio, 1) if vol_ratio else None,
        "bb_lower": round(bb_lower, 4) if bb_lower else None,
        "bb_upper": round(bb_upper, 4) if bb_upper else None,
        "trend": trend,
        "details": {
            "buy_conditions": [
                f"Цена>EMA20: {'✅' if sma20_val and current > sma20_val else '❌'} "
                f"(${current:.4f} vs ${sma20_val:.4f if sma20_val else 0:.4f})",
                f"RSI<35: {'✅' if rsi_val and rsi_val < 35 else '❌'} "
                f"(RSI={rsi_val:.0f})",
                f"Объём>1.3x: {'✅' if vol_ratio and vol_ratio > 1.3 else '❌'} "
                f"(×{vol_ratio or 0:.1f})",
            ],
            "sell_conditions": [
                f"RSI>75: {'✅' if rsi_overbought else '❌'} "
                f"(RSI={rsi_val:.0f if rsi_val else '?'})",
                f"Пробой BB: {'✅' if breakdown else '❌'} "
                f"(${current:.4f} vs BB_low=${bb_lower:.4f})" if bb_lower else f"Пробой BB: ❌ (нет данных)",
            ],
        }
    }


# ========= БЭКТЕСТ =========

def backtest() -> Tuple[List[Dict], Dict]:
    """Прогнать стратегию на всех исторических данных."""
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT timestamp, last_price, volume_24h FROM prices ORDER BY id ASC"
    ).fetchall()
    conn.close()
    
    if len(rows) < 100:
        return [], {"error": "недостаточно данных"}
    
    prices = [r[1] for r in rows]
    volumes = [r[2] for r in rows]
    timestamps = [r[0] for r in rows]
    
    # Скользящий анализ — сигнал на каждом шаге, симуляция сделок
    trades = []
    position = None  # None | {"type":"BUY"|"SELL", "entry_price", "entry_idx"}
    
    for i in range(100, len(prices)):
        window_prices = prices[:i+1]
        window_volumes = volumes[:i+1]
        
        signal = generate_signal(window_prices, window_volumes)
        
        # Если нет позиции — проверяем вход
        if position is None and signal:
            position = {
                "type": signal["signal"],
                "entry_price": signal["price"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "entry_idx": i,
                "entry_ts": timestamps[i],
                "signal": signal,
            }
            continue
        
        # Если есть позиция — проверяем выход
        if position is not None:
            current = prices[i]
            exit_reason = None
            
            if position["type"] == "BUY":
                if current <= position["stop_loss"]:
                    exit_reason = "STOP_LOSS"
                elif current >= position["take_profit"]:
                    exit_reason = "TAKE_PROFIT"
                # Автоматический выход через 60 минут
                elif i - position["entry_idx"] >= 60:
                    exit_reason = "TIMEOUT"
            else:  # SELL
                if current >= position["stop_loss"]:
                    exit_reason = "STOP_LOSS"
                elif current <= position["take_profit"]:
                    exit_reason = "TAKE_PROFIT"
                elif i - position["entry_idx"] >= 60:
                    exit_reason = "TIMEOUT"
            
            if exit_reason:
                pnl_pct = ((current - position["entry_price"]) / position["entry_price"] * 100)
                if position["type"] == "SELL":
                    pnl_pct = -pnl_pct
                
                trades.append({
                    "type": position["type"],
                    "entry_price": position["entry_price"],
                    "exit_price": current,
                    "entry_ts": position["entry_ts"],
                    "exit_ts": timestamps[i],
                    "bars_held": i - position["entry_idx"],
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": exit_reason,
                    "rr_ratio": position["signal"]["rr_ratio"],
                })
                position = None
    
    # Закрыть открытую позицию по последней цене
    if position is not None:
        current = prices[-1]
        pnl_pct = ((current - position["entry_price"]) / position["entry_price"] * 100)
        if position["type"] == "SELL":
            pnl_pct = -pnl_pct
        trades.append({
            "type": position["type"],
            "entry_price": position["entry_price"],
            "exit_price": current,
            "entry_ts": position["entry_ts"],
            "exit_ts": timestamps[-1],
            "bars_held": len(prices) - 1 - position["entry_idx"],
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": "FORCE_CLOSE",
            "rr_ratio": position["signal"]["rr_ratio"],
        })
    
    # Метрики
    if not trades:
        return trades, {"error": "нет сделок за период"}
    
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    
    win_rate = len(wins) / len(trades) * 100
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    
    # Max drawdown (кумулятивная просадка)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t["pnl_pct"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    
    profit_factor = sum(t["pnl_pct"] for t in wins) / abs(sum(t["pnl_pct"] for t in losses)) if losses else 999
    
    exit_reasons = {}
    for t in trades:
        exit_reasons[t["exit_reason"]] = exit_reasons.get(t["exit_reason"], 0) + 1
    
    metrics = {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "exit_reasons": exit_reasons,
        "period_hours": round(len(prices) / 60, 1),
    }
    
    return trades, metrics


def format_signal_telegram(sig: Dict) -> str:
    """Красивый Telegram-формат сигнала с уровнями."""
    emoji = "🟢" if sig["signal"] == "BUY" else "🔴"
    strength_bar = "▓▓▓▓" if sig["strength"] == "STRONG" else "▓▓░░"
    
    if sig["signal"] == "BUY":
        sl_note = f"Стоп-лосс: <b>${sig['stop_loss']:.4f}</b> ({(sig['stop_loss']/sig['price']-1)*100:+.1f}%)"
        tp_note = f"Тейк-профит: <b>${sig['take_profit']:.4f}</b> (+{(sig['take_profit']/sig['price']-1)*100:+.1f}%)"
    else:
        sl_note = f"Стоп-лосс: <b>${sig['stop_loss']:.4f}</b> (+{(sig['stop_loss']/sig['price']-1)*100:+.1f}%)"
        tp_note = f"Тейк-профит: <b>${sig['take_profit']:.4f}</b> ({(sig['take_profit']/sig['price']-1)*100:+.1f}%)"
    
    msg = (
        f"{emoji} <b>{sig['signal']} TON/USDT</b> {strength_bar} {sig['strength']}\n"
        f"\n"
        f"💰 Вход: <b>${sig['price']:.4f}</b>\n"
        f"🛑 {sl_note}\n"
        f"🎯 {tp_note}\n"
        f"📊 Риск/Прибыль: <b>1:{sig['rr_ratio']}</b>\n"
        f"\n"
        f"━━━━ Тех. картина ━━━━\n"
        f"{chr(10).join(sig['details']['buy_conditions'])}\n"
        f"\n"
        f"━━━━ Продажа ━━━━\n"
        f"{chr(10).join(sig['details']['sell_conditions'])}\n"
        f"\n"
        f"📈 Тренд: {sig['trend']}\n"
        f"📏 Bollinger: ${sig['bb_lower']:.4f} — ${sig['bb_upper']:.4f}\n"
        f"📊 SMA7: ${sig['sma7']:.4f} | SMA20: ${sig['sma20']:.4f} | RSI: {sig['rsi']}\n"
        f"\n"
        f"{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )
    return msg


# ========= MAIN =========

if __name__ == "__main__":
    print("=" * 60)
    print("  БЭКТЕСТ ТОРГОВОЙ СТРАТЕГИИ TON/USDT")
    print("=" * 60)
    
    trades, metrics = backtest()
    
    if "error" in metrics:
        print(f"\nОшибка: {metrics['error']}")
    else:
        print(f"\n📅 Период: {metrics['period_hours']} часов")
        print(f"📊 Сделок: {metrics['total_trades']}")
        print(f"✅ Прибыльных: {metrics['wins']} ({metrics['win_rate']}%)")
        print(f"❌ Убыточных: {metrics['losses']}")
        print(f"💰 Общий P&L: {metrics['total_pnl_pct']:+.1f}%")
        print(f"📈 Профит-фактор: {metrics['profit_factor']}")
        print(f"📉 Макс. просадка: -{metrics['max_drawdown_pct']}%")
        print(f"📊 Средний выигрыш: +{metrics['avg_win_pct']}%")
        print(f"📊 Средний убыток: {metrics['avg_loss_pct']}%")
        
        print(f"\n🔚 Причины выхода:")
        for reason, count in metrics['exit_reasons'].items():
            print(f"  {reason}: {count}")
        
        print(f"\n📋 Сделки:")
        for i, t in enumerate(trades, 1):
            emoji = "✅" if t["pnl_pct"] > 0 else "❌"
            print(
                f"  {emoji} #{i} {t['type']:5s} | "
                f"вход ${t['entry_price']:.4f} → выход ${t['exit_price']:.4f} | "
                f"{t['pnl_pct']:+.1f}% | "
                f"держали {t['bars_held']} мин | "
                f"причина: {t['exit_reason']}"
            )
    
    # Заодно покажем текущий сигнал
    print(f"\n{'=' * 60}")
    print("  ТЕКУЩИЙ СИГНАЛ")
    print("=" * 60)
    
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    rows = rows[::-1]
    
    prices = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    sig = generate_signal(prices, volumes)
    
    if sig:
        print(format_signal_telegram(sig))
    else:
        print("\n  Сигналов нет — рынок нейтральный")
        current = prices[-1]
        rsi_v = rsi(prices)
        s7 = sma(prices, 7)
        s20 = sma(prices, 20)
        bb_lower = bb_upper = "?"
        if s20:
            import math
            recent = prices[-20:]
            var = sum((p - s20) ** 2 for p in recent) / 20
            std = math.sqrt(var)
            bb_lower = f"${s20 - 2*std:.4f}"
            bb_upper = f"${s20 + 2*std:.4f}"
        print(f"\n  Цена: ${current:.4f} | RSI: {rsi_v:.1f} | SMA7: ${s7:.4f} | SMA20: ${s20:.4f}")
        print(f"  Bollinger: {bb_lower} — {bb_upper}")
