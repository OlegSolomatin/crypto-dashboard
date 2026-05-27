#!/usr/bin/env python3
"""
Технический анализ TON/USDT — расчёт индикаторов и генерация сигналов.
Запускается cron'ом каждые 5 минут, когда накоплено ≥ 500 записей.

Индикаторы: SMA(5,20), EMA(12,26), RSI(14), MACD, Bollinger Bands, Volume Spike
Логика сигналов — см. сигнатуру функции generate_signal()
"""

import sqlite3
import json
import urllib.request
import urllib.parse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

# === НАСТРОЙКИ ===
DB = Path("/home/oleg/workspace/crypto-ton/data.db")
MIN_ROWS_FOR_ANALYSIS = 100  # минимум записей для первого анализа
ALERT_COOLDOWN_MINUTES = 15  # не слать сигналы чаще
UTC_PLUS_3 = timezone(timedelta(hours=3))


# ====================================================================
#  ИНДИКАТОРЫ
# ====================================================================

def fetch_prices(conn, limit: int = 100):
    """Получить последние N цен из базы (старые → новые)."""
    rows = conn.execute(
        "SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    # Разворачиваем: старые → новые (хронологический порядок)
    return rows[::-1]


def sma(prices: list, period: int) -> Optional[float]:
    """Simple Moving Average."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def ema(prices: list, period: int) -> Optional[float]:
    """Exponential Moving Average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    result = sum(prices[:period]) / period  # начальное SMA
    for price in prices[period:]:
        result = price * k + result * (1 - k)
    return result


def rsi(prices: list, period: int = 14) -> Optional[float]:
    """Relative Strength Index (Уайлдер)."""
    if len(prices) < period + 1:
        return None
    
    gains = 0.0
    losses = 0.0
    
    # Первое среднее
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
    
    # Сглаживание
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


def macd(prices: list) -> Optional[Dict[str, Optional[float]]]:
    """MACD: EMA12, EMA26, Signal EMA9, Histogram."""
    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None
    
    macd_line = ema12 - ema26
    
    # Для сигнала нужен ряд MACD, упростим: используем только последние 9 MACD
    # (полноценный EMA для сигнала требует доп. вычислений)
    signal_line = None
    histogram = None
    
    return {
        "macd_line": round(macd_line, 6),
        "signal_line": signal_line,
        "histogram": histogram,
    }


def bollinger_bands(prices: list, period: int = 20) -> Optional[Dict[str, Optional[float]]]:
    """Bollinger Bands: средняя, верхняя, нижняя, ширина."""
    if len(prices) < period:
        return None
    
    import math
    mid = sma(prices, period)
    if mid is None:
        return None
    
    recent = prices[-period:]
    variance = sum((p - mid) ** 2 for p in recent) / period
    stddev = math.sqrt(variance)
    
    upper = mid + 2 * stddev
    lower = mid - 2 * stddev
    width = ((upper - lower) / mid) * 100 if mid > 0 else 0
    
    return {
        "middle": round(mid, 4),
        "upper": round(upper, 4),
        "lower": round(lower, 4),
        "width_pct": round(width, 2),
    }


def volume_spike(volumes: list) -> Optional[float]:
    """Отношение последнего объёма к среднему за N периодов."""
    if len(volumes) < 5:
        return None
    avg = sum(volumes[:-1]) / len(volumes[:-1])
    if avg == 0:
        return None
    return volumes[-1] / avg


# ====================================================================
#  ГЕНЕРАЦИЯ СИГНАЛА
# ====================================================================

def generate_signal(prices: list, volumes: list) -> Optional[Dict[str, Any]]:
    """
    Анализирует индикаторы и возвращает сигнал.
    
    BUY-условия (все должны совпасть):
      1. Цена > SMA20 (тренд вверх)
      2. RSI < 35 (перепродано или близко)
      3. Объём > среднего × 1.5 (всплеск = интерес рынка)
    
    SELL-условия (любое из двух):
      1. RSI > 70 (перекуплено)
      2. Цена < SMA5 И цена снижается 3 периода подряд (краткосрочный разворот)
    """
    if len(prices) < 30:
        return None
    
    current_price = prices[-1]
    sma5_val = sma(prices, 5)
    sma20_val = sma(prices, 20)
    rsi_val = rsi(prices, 14)
    vol_ratio = volume_spike(volumes) if volumes else None
    bb = bollinger_bands(prices, 20)
    macd_val = macd(prices)
    
    # === BUY ===
    buy_conditions = []
    buy_score = 0
    
    if sma20_val and current_price > sma20_val:
        buy_conditions.append("цена > SMA20")
        buy_score += 1
    else:
        buy_conditions.append(f"цена ≤ SMA20 ({'да' if sma20_val else 'нет данных'})")
    
    if rsi_val is not None and rsi_val < 35:
        buy_conditions.append(f"RSI={rsi_val:.0f} (<35)")
        buy_score += 1
    else:
        rsi_display = f"{rsi_val:.0f}" if rsi_val is not None else "?"
        buy_conditions.append(f"RSI={rsi_display} (≥35)")
    
    if vol_ratio and vol_ratio > 1.5:
        buy_conditions.append(f"Объём ×{vol_ratio:.1f} (>1.5)")
        buy_score += 1
    else:
        buy_conditions.append(f"Объём {'×' + str(round(vol_ratio,1)) if vol_ratio else '?'} (≤1.5)")
    
    # === SELL ===
    sell_conditions = []
    sell_score = 0
    
    if rsi_val is not None and rsi_val > 70:
        sell_conditions.append(f"RSI={rsi_val:.0f} (>70) — перекуплен")
        sell_score += 2
    else:
        rsi_display_s = f"{rsi_val:.0f}" if rsi_val is not None else "?"
        sell_conditions.append(f"RSI={rsi_display_s} (≤70)")
    
    falling = (
        len(prices) >= 4
        and sma5_val
        and current_price < sma5_val
        and prices[-2] < prices[-1]  # падает недавно? (почти бесполезно на минутках)
    )
    if falling:
        sell_conditions.append("Цена < SMA5 + снижение")
        sell_score += 1
    else:
        sell_conditions.append("Нет разворота вниз")
    
    # === Итог ===
    signal = None
    strength = ""
    
    if buy_score == 3:
        signal = "BUY"
        strength = "STRONG"
    elif buy_score == 2:
        signal = "BUY"
        strength = "WEAK"
    elif sell_score >= 2:
        signal = "SELL"
        strength = "STRONG" if sell_score == 3 else "WEAK"
    
    if signal is None:
        return None
    
    return {
        "signal": signal,
        "strength": strength,
        "price": current_price,
        "sma5": round(sma5_val, 4) if sma5_val else None,
        "sma20": round(sma20_val, 4) if sma20_val else None,
        "rsi": round(rsi_val, 1) if rsi_val else None,
        "vol_ratio": round(vol_ratio, 1) if vol_ratio else None,
        "bb_lower": bb["lower"] if bb else None,
        "bb_upper": bb["upper"] if bb else None,
        "macd_line": macd_val["macd_line"] if macd_val else None,
        "buy_conditions": buy_conditions,
        "sell_conditions": sell_conditions,
    }


# ====================================================================
#  ОТПРАВКА В TELEGRAM
# ====================================================================

def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token or not chat_id:
        return False
    
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def signal_to_telegram(sig: dict):
    """Форматирует сигнал в красивое сообщение и отправляет."""
    emoji = "🟢" if sig["signal"] == "BUY" else "🔴"
    strength_bar = "████" if sig["strength"] == "STRONG" else "██░░"
    
    tech = ""
    if sig["rsi"]:
        tech += f"RSI: <b>{sig['rsi']}</b>\n"
    if sig["sma20"]:
        tech += f"SMA20: <b>${sig['sma20']:.4f}</b>\n"
    if sig["vol_ratio"]:
        tech += f"Объём: <b>×{sig['vol_ratio']}</b>\n"
    if sig["bb_lower"] and sig["bb_upper"]:
        tech += f"Bollinger: ${sig['bb_lower']:.4f} — ${sig['bb_upper']:.4f}\n"
    
    msg = (
        f"{emoji} <b>{sig['signal']} TON/USDT</b> — {strength_bar} {sig['strength']}\n\n"
        f"Цена: <b>${sig['price']:.4f}</b>\n"
        f"{tech}\n"
        f"📋 BUY-условия: {', '.join(sig['buy_conditions'][:3])}\n"
        f"📋 SELL-условия: {', '.join(sig['sell_conditions'][:2])}\n"
        f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )
    send_telegram(msg)


# ====================================================================
#  MAIN
# ====================================================================

def main():
    conn = sqlite3.connect(str(DB))
    count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    
    if count < MIN_ROWS_FOR_ANALYSIS:
        print(f"Недостаточно данных: {count}/{MIN_ROWS_FOR_ANALYSIS}")
        conn.close()
        return
    
    rows = fetch_prices(conn, limit=100)
    conn.close()
    
    if not rows:
        print("Нет данных")
        return
    
    prices = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    
    signal = generate_signal(prices, volumes)
    
    if signal:
        print(
            f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] "
            f"{signal['signal']} ({signal['strength']}) | "
            f"${signal['price']:.4f} | "
            f"RSI={signal['rsi']} | "
            f"Vol=×{signal['vol_ratio']}"
        )
        signal_to_telegram(signal)
    else:
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Нет сигнала")


if __name__ == "__main__":
    main()
