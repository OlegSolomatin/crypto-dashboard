"""
Live-режим: запуск стратегии на реальных данных.
Стратегия работает в реальном времени заданное количество дней.
Опрашивает Binance REST API каждые N секунд для получения новых свечей.
"""
import json, threading, time as _time, sys
from datetime import datetime, timedelta
from backtest_common import (
    STRATEGY_CONFIG, calc_rsi, fetch_binance_klines,
    calculate_pnl, build_report, save_report,
)


# === Свечные паттерны для Live-режима ===
def detect_hammer(candles, i):
    """Проверка: свеча i — молот? (как в dashboard_api.py v2)"""
    if i < 1:
        return False
    c = candles[i]
    body = abs(c["close"] - c["open"])
    lower_shadow = min(c["open"], c["close"]) - c["low"]
    upper_shadow = c["high"] - max(c["open"], c["close"])
    total_range = c["high"] - c["low"]
    if total_range == 0:
        return False
    if body == 0:
        return lower_shadow >= total_range * 0.6 and upper_shadow <= total_range * 0.1
    body_upper = max(c["open"], c["close"])
    return (lower_shadow >= body * 2.0 and
            upper_shadow <= body * 0.3 and
            body_upper >= c["low"] + total_range * 0.7)

def detect_inverted_hammer(candles, i):
    """Проверка: свеча i — обратный молот?"""
    if i < 1:
        return False
    c = candles[i]
    body = abs(c["close"] - c["open"])
    upper_shadow = c["high"] - max(c["open"], c["close"])
    lower_shadow = min(c["open"], c["close"]) - c["low"]
    total_range = c["high"] - c["low"]
    if total_range == 0:
        return False
    if body == 0:
        return upper_shadow >= total_range * 0.6 and lower_shadow <= total_range * 0.1
    body_lower = min(c["open"], c["close"])
    return (upper_shadow >= body * 2.0 and
            lower_shadow <= body * 0.3 and
            body_lower <= c["low"] + total_range * 0.3)

def detect_bullish_engulfing(candles, i):
    """Бычье поглощение: красная → зелёная полностью перекрывает тело предыдущей."""
    if i < 1:
        return False
    prev, cur = candles[i-1], candles[i]
    prev_bearish = prev["close"] < prev["open"]
    cur_bullish = cur["close"] > cur["open"]
    engulfs = cur["open"] <= prev["close"] and cur["close"] >= prev["open"]
    prev_body = abs(prev["close"] - prev["open"])
    cur_body = abs(cur["close"] - cur["open"])
    return prev_bearish and cur_bullish and engulfs and prev_body > 0 and cur_body > 0

def detect_morning_star(candles, i):
    """Утренняя звезда: красная → доджи → зелёная выше середины первой."""
    if i < 2:
        return False
    first, second, third = candles[i-2], candles[i-1], candles[i]
    first_bearish = first["close"] < first["open"]
    first_body = abs(first["close"] - first["open"])
    second_body = abs(second["close"] - second["open"])
    second_small = second_body < first_body * 0.3 if first_body > 0 else True
    third_bullish = third["close"] > third["open"]
    third_above_mid = third["close"] > (first["open"] + first["close"]) / 2
    return first_bearish and second_small and third_bullish and third_above_mid

def detect_piercing_line(candles, i):
    """Пронизывающая линия: красная → зелёная открывается ниже минимума,
    закрывается выше середины красной."""
    if i < 1:
        return False
    prev, cur = candles[i-1], candles[i]
    prev_bearish = prev["close"] < prev["open"]
    cur_bullish = cur["close"] > cur["open"]
    opens_below_prev_low = cur["open"] < prev["low"]
    prev_body = abs(prev["close"] - prev["open"])
    if prev_body == 0:
        return False
    prev_mid = (prev["open"] + prev["close"]) / 2
    closes_above_mid = cur["close"] > prev_mid
    closes_below_prev_open = cur["close"] < prev["open"]
    return (prev_bearish and cur_bullish and opens_below_prev_low and
            closes_above_mid and closes_below_prev_open)

def is_downtrend(candles, i, n=3):
    """Последние n свечей падают?"""
    if i < n + 1:
        return False
    closes = [c["close"] for c in candles]
    return all(closes[i-j] < closes[i-j-1] for j in range(1, n+1))

def is_uptrend(candles, i, n=3):
    """Последние n свечей растут?"""
    if i < n + 1:
        return False
    closes = [c["close"] for c in candles]
    return all(closes[i-j] > closes[i-j-1] for j in range(1, n+1))

def volume_ok(candles, i, period=10, threshold=1.2):
    """Объём выше среднего?"""
    if i < period:
        return False
    vols = [c.get("volume", 0) for c in candles]
    avg = sum(vols[i-period:i]) / period
    return vols[i] > avg * threshold


class LiveRunner:
    """Real-time strategy runner polling Binance REST API."""

    @staticmethod
    def run(job_id, params, _backtests):
        bt = _backtests[job_id]
        bt["cancelled"] = False
        bt["events"] = []
        bt["status"] = "running"
        bt["params"] = params

        try:
            symbol = params["pair"]
            interval = params["timeframe"]
            days = int(params["period"])
            leverage = int(params["leverage"])
            balance = float(params["balance"])
            position_size = float(params.get("position_size", balance * 0.05))
            strategy = params["strategy"]

            cfg = STRATEGY_CONFIG.get(strategy)
            if not cfg:
                bt["status"] = "error"
                bt["message"] = f"Неизвестная стратегия: {strategy}"
                return

            tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                          "1h": 60, "2h": 120, "4h": 240, "1d": 1440}.get(interval, 60)

            # --- Шаг 1: загружаем исторические свечи для разогрева RSI ---
            bt["message"] = f"🔴 Live: загрузка исторических свечей {symbol} {interval}..."
            candles = fetch_binance_klines(symbol, interval, limit=200)

            if not candles or len(candles) < 50:
                bt["status"] = "error"
                bt["message"] = f"Недостаточно свечей для {symbol} {interval} (нужно ≥50, получено {len(candles) if candles else 0})"
                return

            bt["message"] = f"🔴 Live: запуск {cfg['name']} на {len(candles)} свечах..."
            bt["total_candles"] = len(candles)

            # --- Шаг 2: настройка цикла ---
            start_time = datetime.now()
            end_time = start_time + timedelta(days=days)
            last_closed_time = candles[-1]["time"]

            # Состояние стратегии
            trades = []
            last_trade_bar = -cfg["cooldown_bars"]
            position = None
            candle_idx_offset = len(candles) - 1  # индекс последней исторической свечи

            # Интервал опроса: половина длины свечи, но не менее 10с
            poll_sec = max(10, int(tf_minutes * 60 / 2))

            bt["message"] = f"🔴 Live: ожидание новых свечей... (опрос каждые {poll_sec}с, {days} дн.)"

            # --- Шаг 3: главный цикл ---
            while not bt.get("cancelled"):
                now = datetime.now()
                elapsed = (now - start_time).total_seconds()
                remaining = (end_time - now).total_seconds()

                if remaining <= 0:
                    bt["message"] = "⏰ Время Live-теста истекло"
                    break

                if balance <= 0:
                    bt["message"] = "⚠️ Баланс обнулён! Стратегия остановлена."
                    break

                # Загружаем свежие свечи
                fresh = fetch_binance_klines(symbol, interval, limit=5)
                if fresh:
                    for nc in fresh:
                        if nc["time"] > last_closed_time:
                            last_closed_time = nc["time"]
                            candles.append(nc)
                            candle_idx_offset += 1
                            i = candle_idx_offset

                            # === ОБРАБОТКА ОДНОЙ СВЕЧИ ===
                            closes = [c["close"] for c in candles[:i+1]]
                            rsi_val = calc_rsi(closes)
                            cur = closes[-1]

                            # Проверка открытой позиции
                            if position:
                                exit_price = None
                                reason = None
                                sl_pct = cfg["sl_pct"]
                                is_candle = cfg.get("type") == "candle"

                                if position["type"] == "BUY":
                                    if is_candle:
                                        # Свечные стратегии: абсолютные SL/TP
                                        if nc["low"] <= position["sl"]:
                                            exit_price = position["sl"]; reason = "STOP_LOSS"
                                        elif nc["high"] >= position["tp"]:
                                            exit_price = position["tp"]; reason = "TAKE_PROFIT"
                                    else:
                                        # Trailing update
                                        if cfg["trailing"] and cur > position["best"]:
                                            position["best"] = cur
                                        if cfg["trailing"] and position["best"] >= position["entry"] * (1 + cfg["trail_activate"]):
                                            position["sl"] = round(position["best"] * (1 - sl_pct), 4)
                                        if nc["low"] <= position.get("sl", position["entry"] * (1 - sl_pct)):
                                            exit_price = position.get("sl", position["entry"] * (1 - sl_pct))
                                            reason = "TRAILING_STOP" if cfg["trailing"] else "STOP_LOSS"
                                        elif not cfg["trailing"] and cfg["tp_pct"] and nc["high"] >= position["entry"] * (1 + cfg["tp_pct"]):
                                            exit_price = position["entry"] * (1 + cfg["tp_pct"])
                                            reason = "TAKE_PROFIT"
                                else:  # SELL
                                    if is_candle:
                                        if nc["high"] >= position["sl"]:
                                            exit_price = position["sl"]; reason = "STOP_LOSS"
                                        elif nc["low"] <= position["tp"]:
                                            exit_price = position["tp"]; reason = "TAKE_PROFIT"
                                    else:
                                        if cfg["trailing"] and cur < position["best"]:
                                            position["best"] = cur
                                        if cfg["trailing"] and position["best"] <= position["entry"] * (1 - cfg["trail_activate"]):
                                            position["sl"] = round(position["best"] * (1 + sl_pct), 4)
                                        if nc["high"] >= position.get("sl", position["entry"] * (1 + sl_pct)):
                                            exit_price = position.get("sl", position["entry"] * (1 + sl_pct))
                                            reason = "TRAILING_STOP" if cfg["trailing"] else "STOP_LOSS"
                                        elif not cfg["trailing"] and cfg["tp_pct"] and nc["low"] <= position["entry"] * (1 - cfg["tp_pct"]):
                                            exit_price = position["entry"] * (1 - cfg["tp_pct"])
                                            reason = "TAKE_PROFIT"

                                if exit_price:
                                    pnl = calculate_pnl(position, exit_price)
                                    balance += pnl
                                    trades.append({
                                        "type": position["type"], "entry": round(position["entry"], 4),
                                        "exit": round(exit_price, 4), "pnl": round(pnl, 4),
                                        "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                                        "reason": reason, "bars": i - position["bar"],
                                    })
                                    bt["events"].append({
                                        "ts": i, "type": "close",
                                        "side": position["type"], "entry": round(position["entry"], 4),
                                        "exit": round(exit_price, 4), "pnl": round(pnl, 2),
                                        "reason": reason, "balance": round(balance, 2),
                                    })
                                    position = None
                                    last_trade_bar = i
                                    continue

                            # Кулдаун
                            if i - last_trade_bar < cfg["cooldown_bars"]:
                                continue

                            # Сигнал на вход
                            margin = position_size / leverage
                            qty = position_size / cur
                            sl_pct = cfg["sl_pct"]
                            
                            is_candle = cfg.get("type") == "candle"

                            if is_candle:
                                # Свечные паттерны (Hammer / Inverted Hammer)
                                if strategy == "hammer":
                                    if (detect_hammer(candles, i) and is_downtrend(candles, i)
                                            and volume_ok(candles, i)):
                                        # Подтверждение — ждём следующую свечу
                                        # В live-режиме подтверждение приходит со следующей свечой
                                        if i >= 1 and candles[i-1].get("_hammer_signal"):
                                            hammer_c = candles[i-1]
                                            # Проверяем подтверждение: закрытие > High молота
                                            if cur > hammer_c["high"]:
                                                # Вход на текущей цене
                                                sl = hammer_c["low"] * 0.995
                                                risk = cur - sl
                                                if risk > 0:
                                                    tp = cur + risk * 2.0
                                                    position = {
                                                        "type": "BUY", "entry": cur, "sl": sl, "tp": tp,
                                                        "bar": i, "qty": qty, "margin": margin, "best": cur,
                                                    }
                                                    bt["events"].append({
                                                        "ts": i, "type": "signal", "side": "BUY",
                                                        "price": round(cur, 4), "sl": round(sl, 4),
                                                        "tp": round(tp, 4),
                                                    })
                                        # Помечаем свечу как сигнал для проверки на следующей
                                        candles[i]["_hammer_signal"] = True
                                        
                                elif strategy == "inverted_hammer":
                                    if (detect_inverted_hammer(candles, i) and is_uptrend(candles, i)
                                            and volume_ok(candles, i)):
                                        if i >= 1 and candles[i-1].get("_inv_hammer_signal"):
                                            inv_c = candles[i-1]
                                            if cur < inv_c["low"]:
                                                sl = inv_c["high"] * 1.005
                                                risk = sl - cur
                                                if risk > 0:
                                                    tp = cur - risk * 2.0
                                                    position = {
                                                        "type": "SELL", "entry": cur, "sl": sl, "tp": tp,
                                                        "bar": i, "qty": qty, "margin": margin, "best": cur,
                                                    }
                                                    bt["events"].append({
                                                        "ts": i, "type": "signal", "side": "SELL",
                                                        "price": round(cur, 4), "sl": round(sl, 4),
                                                        "tp": round(tp, 4),
                                                    })
                                        candles[i]["_inv_hammer_signal"] = True
                                
                                elif strategy == "bullish_engulfing":
                                    # Усиленные фильтры: volume > 200%, RSI < 40, EMA 50
                                    vol_ok = volume_ok(candles, i, period=20, threshold=2.0)
                                    if detect_bullish_engulfing(candles, i) and vol_ok:
                                        # RSI фильтр
                                        if i >= 14:
                                            rsi_window = closes[-15:]
                                            rsi_g = sum(max(rsi_window[j]-rsi_window[j-1],0) for j in range(1,len(rsi_window)))
                                            rsi_l = sum(max(rsi_window[j-1]-rsi_window[j],0) for j in range(1,len(rsi_window)))
                                            rsi_v = 100-(100/(1+rsi_g/rsi_l)) if rsi_l>0 else 100
                                            if rsi_v >= 40:
                                                candles[i]["_bullish_engulfing_signal"] = True
                                                continue
                                        # EMA 50 фильтр
                                        if i >= 50:
                                            ema50 = closes[i-49]
                                            mult = 2/51
                                            for j in range(i-48, i+1):
                                                ema50 = (closes[j]-ema50)*mult+ema50
                                            if cur <= ema50:
                                                candles[i]["_bullish_engulfing_signal"] = True
                                                continue
                                        if i >= 1 and candles[i-1].get("_bullish_engulfing_signal"):
                                            be_c = candles[i-1]
                                            if cur > be_c["close"]:
                                                sl = min(be_c["low"], candles[i-2]["low"] if i>=2 else be_c["low"]) * 0.995
                                                risk = cur - sl
                                                if risk > 0:
                                                    tp = cur + risk * 2.0
                                                    position = {
                                                        "type": "BUY", "entry": cur, "sl": sl, "tp": tp,
                                                        "bar": i, "qty": qty, "margin": margin, "best": cur,
                                                    }
                                                    bt["events"].append({
                                                        "ts": i, "type": "signal", "side": "BUY",
                                                        "price": round(cur, 4), "sl": round(sl, 4),
                                                        "tp": round(tp, 4),
                                                    })
                                        candles[i]["_bullish_engulfing_signal"] = True
                                
                                elif strategy == "morning_star":
                                    if detect_morning_star(candles, i) and volume_ok(candles, i):
                                        if i >= 1 and candles[i-1].get("_morning_star_signal"):
                                            ms_c = candles[i-1]
                                            if cur > ms_c["close"]:
                                                pattern_low = min(candles[i-2]["low"], candles[i-1]["low"], candles[i]["low"])
                                                sl = pattern_low * 0.995
                                                risk = cur - sl
                                                if risk > 0:
                                                    tp = cur + risk * 2.0
                                                    position = {
                                                        "type": "BUY", "entry": cur, "sl": sl, "tp": tp,
                                                        "bar": i, "qty": qty, "margin": margin, "best": cur,
                                                    }
                                                    bt["events"].append({
                                                        "ts": i, "type": "signal", "side": "BUY",
                                                        "price": round(cur, 4), "sl": round(sl, 4),
                                                        "tp": round(tp, 4),
                                                    })
                                        candles[i]["_morning_star_signal"] = True
                                
                                elif strategy == "piercing_line":
                                    if detect_piercing_line(candles, i) and volume_ok(candles, i):
                                        if i >= 1 and candles[i-1].get("_piercing_line_signal"):
                                            pl_c = candles[i-1]
                                            if cur > pl_c["close"]:
                                                sl = min(pl_c["low"], candles[i-2]["low"] if i>=2 else pl_c["low"]) * 0.995
                                                risk = cur - sl
                                                if risk > 0:
                                                    tp = cur + risk * 2.0
                                                    position = {
                                                        "type": "BUY", "entry": cur, "sl": sl, "tp": tp,
                                                        "bar": i, "qty": qty, "margin": margin, "best": cur,
                                                    }
                                                    bt["events"].append({
                                                        "ts": i, "type": "signal", "side": "BUY",
                                                        "price": round(cur, 4), "sl": round(sl, 4),
                                                        "tp": round(tp, 4),
                                                    })
                                        candles[i]["_piercing_line_signal"] = True

                            elif rsi_val < cfg["rsi_buy"]:
                                sl = round(cur * (1 - sl_pct), 4)
                                tp = round(cur * (1 + cfg["tp_pct"]), 4) if cfg["tp_pct"] else None
                                position = {
                                    "type": "BUY", "entry": cur, "sl": sl, "tp": tp,
                                    "bar": i, "qty": qty, "margin": margin, "best": cur,
                                }
                                bt["events"].append({
                                    "ts": i, "type": "signal", "side": "BUY",
                                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                                    "sl": sl, "tp": tp or "TRAILING",
                                })
                            elif rsi_val > cfg["rsi_sell"]:
                                sl = round(cur * (1 + sl_pct), 4)
                                tp = round(cur * (1 - cfg["tp_pct"]), 4) if cfg["tp_pct"] else None
                                position = {
                                    "type": "SELL", "entry": cur, "sl": sl, "tp": tp,
                                    "bar": i, "qty": qty, "margin": margin, "best": cur,
                                }
                                bt["events"].append({
                                    "ts": i, "type": "signal", "side": "SELL",
                                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                                    "sl": sl, "tp": tp or "TRAILING",
                                })

                # Обновление статистики
                progress = min(99, int(elapsed / (days * 24 * 3600) * 100))
                bt["progress"] = progress
                bt["candles_processed"] = candle_idx_offset
                wins = sum(1 for t in trades if t["pnl"] > 0)
                bt["live_balance"] = round(balance, 2)
                bt["live_trades"] = len(trades)
                bt["live_win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0

                rh = int(remaining // 3600)
                rm = int((remaining % 3600) // 60)
                bt["message"] = f"🔴 Live | {cfg['name']} | Сделок: {len(trades)} | Баланс: ${balance:.2f} | Осталось: {rh}ч {rm}м"

                _time.sleep(poll_sec)

            # --- Шаг 4: закрываем открытую позицию ---
            if position and candles:
                exit_price = candles[-1]["close"]
                pnl = calculate_pnl(position, exit_price)
                balance += pnl
                reason = "CANCELLED" if bt.get("cancelled") else "END_OF_PERIOD"
                trades.append({
                    "type": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                    "reason": reason, "bars": candle_idx_offset - position.get("bar", 0),
                })

            # --- Шаг 5: финальный отчёт ---
            report = build_report(trades, balance, params)

            if bt.get("cancelled"):
                report["status"] = "CANCELLED"
                report["cancel_reason"] = "Пользователь остановил Live-тест досрочно"
                bt["status"] = "cancelled"
                bt["message"] = "⏹ Live-тест остановлен пользователем"
            elif report.get("end_balance", balance) <= 0:
                report["status"] = "BALANCE_ZERO"
                bt["message"] = "⚠️ Баланс обнулён!"
                bt["status"] = "done"
            else:
                report["status"] = "COMPLETED"
                bt["status"] = "done"
                bt["message"] = f"✅ Live-тест завершён! Сделок: {len(trades)}"

            bt["report"] = report
            bt["progress"] = 100

            saved = save_report(job_id, params, report)
            bt["file"] = str(saved[0]) if saved else None
            bt["history_file"] = str(saved[1]) if saved and len(saved) > 1 else None

        except Exception as e:
            bt["status"] = "error"
            bt["message"] = f"Live error: {e}"
            import traceback
            traceback.print_exc()
