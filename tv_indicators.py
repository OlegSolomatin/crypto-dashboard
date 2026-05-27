#!/usr/bin/env python3
"""
Портированные индикаторы из TradingView Pine Script → Python.
Используются сигнальщиком v6 для дополнительных фильтров.

Индикаторы:
  • SuperTrend — тренд с адаптивным ATR
  • VWAP — средневзвешенная по объёму цена
  • ATR Stops — адаптивные стоп-лоссы по волатильности
  • Volume Profile — зоны максимального объёма
"""

import math
from typing import List, Optional, Tuple


# ═══════════════════════════════════════════
#  1. SUPERTREND
# ═══════════════════════════════════════════

def supertrend(high: List[float], low: List[float], close: List[float],
               period: int = 10, multiplier: float = 3.0) -> Tuple[List[float], List[int]]:
    """
    SuperTrend индикатор.
    Возвращает (линия SuperTrend, направление тренда: 1=бычий, -1=медвежий).
    
    Логика из Pine Script:
    atr = ta.atr(period)
    up = hl2 - (multiplier * atr)
    dn = hl2 + (multiplier * atr)
    trend = close > prev_up ? 1 : close < prev_dn ? -1 : prev_trend
    """
    if len(close) < period + 1:
        return [], []
    
    # ATR
    tr_list = []
    for i in range(1, len(close)):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i-1])
        lc = abs(low[i] - close[i-1])
        tr_list.append(max(hl, hc, lc))
    
    atr = [sum(tr_list[:period]) / period]
    for i in range(period, len(tr_list)):
        atr.append((atr[-1] * (period - 1) + tr_list[i]) / period)
    
    # SuperTrend
    st_line = []
    trend = []
    direction = 1  # 1 = бычий
    
    for i in range(period, len(close)):
        hl2 = (high[i] + low[i]) / 2
        atr_idx = i - period
        
        basic_upper = hl2 - multiplier * atr[atr_idx]  # для бычьего
        basic_lower = hl2 + multiplier * atr[atr_idx]  # для медвежьего
        
        # Расчёт с учётом предыдущих значений
        if i == period:
            upper = basic_upper
            lower = basic_lower
        else:
            upper = (basic_upper if basic_upper < st_line[-1] or close[i-1] > st_line[-1]
                     else basic_upper)
            lower = (basic_lower if basic_lower > st_line[-1] or close[i-1] < st_line[-1]
                     else basic_lower)
        
        if direction == 1:
            if close[i] < upper:
                direction = -1
                st_line.append(lower)
            else:
                st_line.append(max(upper, st_line[-1] if st_line else upper))
        else:
            if close[i] > lower:
                direction = 1
                st_line.append(upper)
            else:
                st_line.append(min(lower, st_line[-1] if st_line else lower))
        
        trend.append(direction)
    
    return st_line, trend


# ═══════════════════════════════════════════
#  2. VWAP (Volume-Weighted Average Price)
# ═══════════════════════════════════════════

def vwap(high: List[float], low: List[float], close: List[float],
         volume: List[float]) -> Optional[float]:
    """
    VWAP за весь доступный период.
    Возвращает текущее значение VWAP.
    
    VWAP = Σ(price × volume) / Σ(volume)
    price = (high + low + close) / 3
    """
    if not close or not volume:
        return None
    
    total_pv = 0.0
    total_v = 0.0
    
    for i in range(len(close)):
        typical = (high[i] + low[i] + close[i]) / 3
        total_pv += typical * volume[i]
        total_v += volume[i]
    
    return total_pv / total_v if total_v > 0 else None


def vwap_position(current_price: float, vwap_price: float) -> str:
    """
    Позиция цены относительно VWAP.
    'above' — цена выше VWAP (бычий),
    'below' — цена ниже VWAP (медвежий).
    """
    if not vwap_price:
        return "unknown"
    return "above" if current_price > vwap_price else "below"


# ═══════════════════════════════════════════
#  3. ATR STOPS (адаптивные стоп-лоссы)
# ═══════════════════════════════════════════

def atr_stops(high: List[float], low: List[float], close: List[float],
              period: int = 14, multiplier: float = 2.0) -> Tuple[Optional[float], Optional[float]]:
    """
    Адаптивные стоп-лоссы на основе ATR.
    Возвращает (long_stop, short_stop) — уровни для BUY и SELL.
    
    Логика:
    atr = average_true_range(period)
    long_stop = close - (multiplier * atr)   # стоп для BUY (защита снизу)
    short_stop = close + (multiplier * atr)  # стоп для SELL (защита сверху)
    """
    if len(close) < period + 1:
        return None, None
    
    # ATR
    tr_list = []
    for i in range(1, len(close)):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i-1])
        lc = abs(low[i] - close[i-1])
        tr_list.append(max(hl, hc, lc))
    
    atr_val = sum(tr_list[-period:]) / period
    long_stop = close[-1] - multiplier * atr_val
    short_stop = close[-1] + multiplier * atr_val
    
    return long_stop, short_stop


# ═══════════════════════════════════════════
#  4. VOLUME PROFILE (зоны максимального объёма)
# ═══════════════════════════════════════════

def volume_profile_poc(prices: List[float], volumes: List[float],
                       num_bins: int = 20) -> Optional[float]:
    """
    Point of Control (POC) — цена с максимальным объёмом.
    Возвращает уровень POC.
    """
    if not prices or not volumes:
        return None
    
    min_p, max_p = min(prices), max(prices)
    if min_p == max_p:
        return min_p
    
    bin_width = (max_p - min_p) / num_bins
    bins = {}
    
    for p, v in zip(prices, volumes):
        if bin_width == 0:
            continue
        bin_idx = int((p - min_p) / bin_width)
        bin_idx = min(bin_idx, num_bins - 1)
        bins[bin_idx] = bins.get(bin_idx, 0) + v
    
    if not bins:
        return None
    
    max_bin = max(bins, key=bins.get)
    return min_p + (max_bin + 0.5) * bin_width


# ═══════════════════════════════════════════
#  5. КОМБИНИРОВАННАЯ ОЦЕНКА СИГНАЛА
# ═══════════════════════════════════════════

def combined_signal_confidence(data_prices: List[float], data_high: List[float],
                                data_low: List[float], data_volumes: List[float],
                                rsi_val: float, macd_val: float, trend: str,
                                nvt: Optional[float] = None) -> dict:
    """
    Комбинированная уверенность сигнала на основе всех индикаторов.
    Возвращает confidence (0-10) и список причин.
    """
    confidence = 0
    reasons = []
    
    # SuperTrend
    st_line, st_trend = supertrend(data_high[-100:], data_low[-100:], data_prices[-100:])
    if st_trend:
        if st_trend[-1] == 1:
            confidence += 2
            reasons.append("SuperTrend: ↑ бычий (+2)")
        else:
            confidence += 0
            reasons.append("SuperTrend: ↓ медвежий")
    
    # VWAP
    v = vwap(data_high, data_low, data_prices, data_volumes)
    if v:
        pos = vwap_position(data_prices[-1], v)
        if pos == "above":
            confidence += 1
            reasons.append(f"Цена выше VWAP (${v:.4f}) (+1)")
        else:
            reasons.append(f"Цена ниже VWAP (${v:.4f})")
    
    # ATR Stops
    long_sl, short_sl = atr_stops(data_high, data_low, data_prices)
    if long_sl and short_sl:
        reasons.append(f"ATR SL: long={long_sl:.4f} short={short_sl:.4f}")
    
    # Volume Profile POC
    poc = volume_profile_poc(data_prices[-100:], data_volumes[-100:])
    if poc:
        if abs(data_prices[-1] - poc) / poc < 0.01:
            confidence += 1
            reasons.append(f"Цена у POC (${poc:.4f}) — зона интереса (+1)")
    
    # RSI
    if rsi_val < 30:
        confidence += 2
        reasons.append(f"RSI={rsi_val:.0f} < 30 (+2)")
    elif rsi_val < 35:
        confidence += 1
        reasons.append(f"RSI={rsi_val:.0f} < 35 (+1)")
    
    # NVT
    if nvt and nvt < 15:
        confidence += 1
        reasons.append(f"NVT={nvt:.1f} < 15 (+1)")
    
    return {
        "confidence": min(confidence, 10),
        "reasons": reasons,
        "vwap": v,
        "supertrend": st_trend[-1] if st_trend else None,
        "atr_long_sl": long_sl,
        "atr_short_sl": short_sl,
        "poc": poc,
    }


# ═══════════════════════════════════════════
#  ТЕСТ
# ═══════════════════════════════════════════

if __name__ == "__main__":
    # Тест на реальных данных
    import sqlite3
    conn = sqlite3.connect("/home/oleg/workspace/crypto-ton/data.db")
    rows = conn.execute(
        "SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    rows = rows[::-1]  # oldest first
    
    prices = [r[0] for r in rows]
    volumes = [r[1] / 1e6 for r in rows]  # нормализуем
    # Симулируем high/low из close (для теста)
    high = [p * 1.002 for p in prices]
    low = [p * 0.998 for p in prices]
    
    # SuperTrend
    st_l, st_t = supertrend(high, low, prices)
    if st_t:
        print(f"SuperTrend: {'↑ бычий' if st_t[-1] == 1 else '↓ медвежий'} (линия: ${st_l[-1]:.4f})")
    
    # VWAP
    v = vwap(high, low, prices, volumes)
    if v:
        print(f"VWAP: ${v:.4f} — цена {'выше' if prices[-1] > v else 'ниже'} VWAP")
    
    # ATR Stops
    ls, ss = atr_stops(high, low, prices)
    if ls and ss:
        print(f"ATR Stops: long_stop=${ls:.4f} short_stop=${ss:.4f}")
    
    # Volume Profile
    poc = volume_profile_poc(prices, volumes)
    if poc:
        print(f"POC (Point of Control): ${poc:.4f}")
    
    # Combined confidence
    result = combined_signal_confidence(prices, high, low, volumes, 48, 0.001, "нейтральный")
    print(f"\nУверенность: {result['confidence']}/10")
    for r in result['reasons']:
        print(f"  {r}")
