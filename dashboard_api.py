#!/usr/bin/env python3
"""
JSON API для дашборда.
Основной источник свечей: Binance public API (без ключа, любые пары).
Локальные данные для paper-трейдинга, сигналов, ончейна, сентимента.

Запуск: python3 dashboard_api.py [порт]   (по умолчанию 8889)
"""

import sqlite3, json, sys, math, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

STREAM_DB = Path("/home/oleg/workspace/crypto-ton/stream.db")
PAPER_DB  = Path("/home/oleg/workspace/crypto-ton/paper.db")
ONCHAIN_DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
SENTIMENT_DB = Path("/home/oleg/workspace/crypto-ton/sentiment.db")
DATA_DB   = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")

# Поддерживаемые пары (Binance spot)
SYMBOLS = {
    "TON":    "TONUSDT",
    "BTC":    "BTCUSDT",
    "ETH":    "ETHUSDT",
    "SOL":    "SOLUSDT",
    "DOGE":   "DOGEUSDT",
    "NOT":    "NOTUSDT",
    "DOGS":   "DOGSUSDT",
    "HMSTR":  "HMSTRUSDT",
    "XRP":    "XRPUSDT",
    "SUI":    "SUIUSDT",
}

INTERVALS = {
    "1m":  ("1m", 1),
    "5m":  ("5m", 5),
    "15m": ("15m", 15),
    "30m": ("30m", 30),
    "1h":  ("1h", 60),
    "4h":  ("4h", 240),
    "1d":  ("1d", 1440),
}


# ──────────────────────────────── helpers ────────────────────────────────

def _parse_ts(ts_str):
    if ts_str is None: return None
    ts_str = str(ts_str)
    for suffix in ["+03:00", "+00:00", "Z"]:
        if suffix in ts_str:
            ts_str = ts_str.replace(suffix, "")
            break
    try:
        if "T" in ts_str:
            return datetime.strptime(ts_str.replace("T"," ")[:19], "%Y-%m-%d %H:%M:%S")
        elif "-" in ts_str and len(ts_str) >= 19:
            return datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        else:
            return datetime.fromtimestamp(float(ts_str))
    except:
        return None

def _fmt_ts(ts_str):
    dt = _parse_ts(ts_str)
    if dt is None: return ts_str[:16] if ts_str else "?"
    return dt.strftime("%d.%m %H:%M")


# ──────────────────────────────── свечи из Binance ──────────────────────

def _fetch_binance_klines(symbol: str, interval: str, limit: int = 200):
    """
    Тянет свечи с Binance public API. Бесплатно, без ключа.
    Возвращает список dict'ов с полями time/open/high/low/close/volume.
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        candles = []
        for k in data:
            candles.append({
                "time": int(k[0]) // 1000,  # ms → seconds
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return candles
    except Exception as e:
        print(f"[Binance] {symbol} {interval}: {e}", file=sys.stderr)
        return None


def _candles_from_local(symbol: str, minutes_per_candle: int):
    """Свечи из локальной data.db (fallback). Только для TON."""
    if symbol != "TONUSDT":
        return None
    try:
        conn = sqlite3.connect(str(DATA_DB), timeout=5)
        rows = conn.execute("""
            SELECT timestamp, last_price, volume_24h
            FROM prices ORDER BY id DESC LIMIT 1000
        """).fetchall()
        conn.close()

        if len(rows) < 5:
            return None

        rows = rows[::-1]
        candles = []
        bucket_start = None
        bucket_ohlc = None
        bucket_vol = 0.0

        for ts_str, price, vol in rows:
            if price is None:
                continue
            dt = _parse_ts(ts_str)
            if dt is None:
                continue
            # Округляем до начала интервала
            minute_bucket = (dt.minute // minutes_per_candle) * minutes_per_candle
            bucket_key = dt.replace(minute=minute_bucket, second=0, microsecond=0)

            if bucket_start is None or bucket_key > bucket_start:
                if bucket_ohlc is not None:
                    candles.append({
                        "time": int(bucket_start.timestamp()),
                        "open": bucket_ohlc[0],
                        "high": bucket_ohlc[1],
                        "low": bucket_ohlc[2],
                        "close": bucket_ohlc[3],
                        "volume": bucket_vol,
                    })
                bucket_start = bucket_key
                bucket_ohlc = [price, price, price, price]
                bucket_vol = 0.0
            else:
                bucket_ohlc[1] = max(bucket_ohlc[1], price)
                bucket_ohlc[2] = min(bucket_ohlc[2], price)
                bucket_ohlc[3] = price
                bucket_vol += 0.0  # volume_24h нельзя суммировать

        if bucket_ohlc is not None:
            candles.append({
                "time": int(bucket_start.timestamp()),
                "open": bucket_ohlc[0],
                "high": bucket_ohlc[1],
                "low": bucket_ohlc[2],
                "close": bucket_ohlc[3],
                "volume": bucket_vol,
            })

        return candles if len(candles) >= 3 else None
    except Exception as e:
        print(f"[Local] error: {e}", file=sys.stderr)
        return None


def get_chart_data(symbol: str = "TONUSDT", interval: str = "5m"):
    """Свечи + индикаторы."""
    # 1. Пробуем Binance
    candles_raw = _fetch_binance_klines(symbol, interval, limit=200)

    # 2. Fallback: локальные данные
    if not candles_raw:
        minutes = INTERVALS.get(interval, ("5m", 5))[1]
        candles_raw = _candles_from_local(symbol, minutes)

    if not candles_raw or len(candles_raw) < 5:
        return {"error": f"no data for {symbol}", "candles": []}

    last_price = candles_raw[-1]["close"]
    first_price = candles_raw[0]["close"]
    change_pct = round((last_price - first_price) / first_price * 100, 2) if first_price else 0

    prices = [c["close"] for c in candles_raw]

    # SMA
    sma20, sma50 = [], []
    for i in range(len(prices)):
        if i >= 19:
            sma20.append({"time": candles_raw[i]["time"], "value": round(sum(prices[i-19:i+1])/20, 6)})
        if i >= 49:
            sma50.append({"time": candles_raw[i]["time"], "value": round(sum(prices[i-49:i+1])/50, 6)})

    # Bollinger (20)
    bb_up, bb_lo = [], []
    for i in range(20, len(prices)):
        window = prices[i-19:i+1]
        avg = sum(window) / 20
        std = math.sqrt(sum((p-avg)**2 for p in window) / 20)
        bb_up.append({"time": candles_raw[i]["time"], "value": round(avg + 2*std, 6)})
        bb_lo.append({"time": candles_raw[i]["time"], "value": round(avg - 2*std, 6)})

    # RSI(14) Wilder
    rsi_data = []
    if len(prices) >= 15:
        gains = sum(max(prices[i]-prices[i-1], 0) for i in range(1, 15))
        losses = sum(max(prices[i-1]-prices[i], 0) for i in range(1, 15))
        avg_g, avg_l = gains/14, losses/14
        rsi_val = 100 - (100/(1+avg_g/avg_l)) if avg_l > 0 else 100
        rsi_data.append({"time": candles_raw[14]["time"], "value": round(rsi_val, 1)})
        for i in range(15, len(prices)):
            d = prices[i] - prices[i-1]
            avg_g = (avg_g*13 + max(d, 0)) / 14
            avg_l = (avg_l*13 + max(-d, 0)) / 14
            rsi_val = 100 - (100/(1+avg_g/avg_l)) if avg_l > 0 else 100
            rsi_data.append({"time": candles_raw[i]["time"], "value": round(rsi_val, 1)})

    # Volume (цветные бары)
    volume_data = []
    for c in candles_raw:
        vol = c.get("volume", 0)
        color = "#2ea043" if c["close"] >= c["open"] else "#f85149"
        volume_data.append({
            "time": c["time"],
            "value": vol,
            "color": color,
        })

    return {
        "symbol": symbol,
        "interval": interval,
        "lastPrice": round(last_price, 6),
        "changePct": change_pct,
        "candles": candles_raw[-100:],
        "volume": volume_data[-100:],
        "sma20": sma20[-100:] if sma20 else [],
        "sma50": sma50[-100:] if sma50 else [],
        "bbUpper": bb_up[-100:] if bb_up else [],
        "bbLower": bb_lo[-100:] if bb_lo else [],
        "rsi": rsi_data[-100:] if rsi_data else [],
        "rsiLast": rsi_data[-1]["value"] if rsi_data else None,
    }


# ──────────────────────────────── сигналы ────────────────────────────────

def _make_signal_row(r, source):
    if source == "v6":
        return {
            "ts": r[1], "ts_short": _fmt_ts(r[1]),
            "source": "v6", "mode": r[12] if len(r) > 12 else "",
            "signal": r[2], "strength": r[3],
            "confidence": r[4], "price": r[5],
            "sl": r[6], "tp": r[7], "rsi": r[8], "trend": r[9],
        }
    elif source == "scalp":
        return {
            "ts": r[1], "ts_short": _fmt_ts(r[1]),
            "source": "scalp", "mode": "spread_collect",
            "signal": r[2], "strength": "SCALP",
            "confidence": None, "price": r[3],
            "sl": r[4], "tp": r[5],
            "rsi": None, "trend": None,
            "spread_pct": r[6], "imbalance": r[7],
        }
    elif source == "legacy":
        return {
            "ts": r[1], "ts_short": _fmt_ts(r[1]),
            "source": "v5", "mode": "",
            "signal": r[2], "strength": r[3],
            "confidence": None, "price": r[4],
            "sl": None, "tp": None, "rsi": r[5], "trend": r[8],
        }


def get_signals():
    all_signals = []
    try:
        conn = sqlite3.connect(str(SIGNALS_DB))
        try:
            for r in conn.execute("SELECT * FROM signals_v6 ORDER BY id DESC LIMIT 15").fetchall():
                all_signals.append(_make_signal_row(r, "v6"))
        except: pass
        try:
            for r in conn.execute("SELECT * FROM signals_scalp ORDER BY id DESC LIMIT 15").fetchall():
                all_signals.append(_make_signal_row(r, "scalp"))
        except: pass
        try:
            for r in conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 5").fetchall():
                all_signals.append(_make_signal_row(r, "legacy"))
        except: pass
        conn.close()
    except: pass
    all_signals.sort(key=lambda s: s["ts"], reverse=True)
    return all_signals[:40]


def get_trades():
    try:
        conn = sqlite3.connect(str(PAPER_DB))
        rows = conn.execute("""
            SELECT opened_ts, closed_ts, type, signal_source, mode,
                   entry_price, exit_price, pnl, pnl_pct, balance_before, balance_after,
                   exit_reason, sl, tp, margin
            FROM paper_positions ORDER BY id DESC LIMIT 50
        """).fetchall()
        conn.close()
        trades = []
        for r in rows:
            trades.append({
                "opened": _fmt_ts(r[0]),
                "closed": _fmt_ts(r[1]) if r[1] else "",
                "type": r[2],
                "strategy": r[3].replace("signals_", "") if r[3] else "?",
                "mode": r[4] or "",
                "entry": round(r[5], 4) if r[5] else 0,
                "exit": round(r[6], 4) if r[6] else 0,
                "pnl": round(r[7], 4) if r[7] else 0,
                "pnl_pct": round(r[8], 4) if r[8] else 0,
                "balance_before": round(r[9], 2) if r[9] else 0,
                "balance_after": round(r[10], 2) if r[10] else 0,
                "exit_reason": r[11] or "",
                "sl": round(r[12], 4) if r[12] else 0,
                "tp": round(r[13], 4) if r[13] else 0,
                "margin": round(r[14], 2) if r[14] else 0,
            })
        return trades
    except:
        return []


def get_stats():
    trades = get_trades()
    if not trades:
        return {"total": {"trades": 0, "winRate": 0, "pnl": 0, "avgWin": 0, "avgLoss": 0, "wins": 0, "losses": 0}, "by_strategy": []}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]

    total = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "winRate": round(len(wins)/len(trades)*100, 1) if trades else 0,
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "avgWin": round(sum(t["pnl"] for t in wins)/len(wins), 2) if wins else 0,
        "avgLoss": round(sum(t["pnl"] for t in losses)/len(losses), 2) if losses else 0,
    }

    by_strat = {}
    for t in trades:
        s = t["strategy"]
        if s not in by_strat:
            by_strat[s] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        by_strat[s]["trades"] += 1
        by_strat[s]["pnl"] += t["pnl"]
        if t["pnl"] > 0: by_strat[s]["wins"] += 1
        elif t["pnl"] < 0: by_strat[s]["losses"] += 1

    by_strat_list = []
    for name, st in sorted(by_strat.items()):
        st["strategy"] = name
        st["winRate"] = round(st["wins"]/st["trades"]*100, 1) if st["trades"] else 0
        st["pnl"] = round(st["pnl"], 2)
        st["avgPnl"] = round(st["pnl"]/st["trades"], 4) if st["trades"] else 0
        by_strat_list.append(st)

    return {"total": total, "by_strategy": by_strat_list}


def get_balance():
    try:
        conn = sqlite3.connect(str(PAPER_DB))
        pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL").fetchone()[0]
        conn.close()
        return round(100.0 + (pnl or 0), 2)
    except:
        return 100.0


def get_onchain():
    try:
        conn = sqlite3.connect(str(ONCHAIN_DB))
        row = conn.execute("""
            SELECT nvt_ratio, ton_btc_correlation, blocks_per_minute, fdv_to_mcap
            FROM onchain ORDER BY id DESC LIMIT 1
        """).fetchone()
        conn.close()
        if row: return {"nvt": row[0], "btcCorr": row[1], "bpm": row[2], "fdv": row[3]}
    except: pass
    return {}


def get_sentiment():
    try:
        conn = sqlite3.connect(str(SENTIMENT_DB))
        bullish = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1 AND sentiment='positive'").fetchone()[0]
        bearish = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1 AND sentiment='negative'").fetchone()[0]
        neutral = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1 AND sentiment='neutral'").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(impact_score) FROM posts WHERE analyzed=1").fetchone()[0]
        conn.close()
        return {"bullish": bullish, "bearish": bearish, "neutral": neutral,
                "avgScore": round(avg_score, 1) if avg_score else 0}
    except: return {}


# ──────────────────────────────── HTTP ────────────────────────────────

# Backtest jobs (in-memory)
import threading
_backtests = {}  # id → {status, progress, report, start_time, error}
_backtest_counter = [0]

class BacktestRunner:
    """Запуск стратегии на исторических свечах."""
    
    @staticmethod
    def run(job_id, params):
        """Fetch candles and run strategy. Routes to LiveRunner for live mode."""
        bt = _backtests[job_id]
        mode = params.get("mode", "history")
        
        if mode == "live":
            from backtest_live import LiveRunner
            LiveRunner.run(job_id, params, _backtests)
            return
        
        import time as _time
        bt = _backtests[job_id]
        bt["cancelled"] = False
        bt["events"] = []  # detailed event log
        try:
            symbol = params["pair"]
            interval = params["timeframe"]
            days = params["period"]
            leverage = params["leverage"]
            balance = float(params["balance"])
            position_size = float(params.get("position_size", balance * 0.05))
            strategy = params["strategy"]
            speed = params.get("speed", "fast")  # "fast" or "step" (50ms/candle)
            step_delay = 0.05 if speed == "step" else 0
            # Сохраняем для файла-отчёта
            bt["params"] = params
            
            # Рассчитываем лимит свечей
            tf_minutes = INTERVALS.get(interval, ("1h", 60))[1]
            candles_needed = int((days * 24 * 60) / tf_minutes) + 100
            
            bt["message"] = f"Загрузка свечей {symbol} {interval}..."
            candles = _fetch_binance_klines(symbol, interval, limit=min(candles_needed, 1000))
            
            if not candles or len(candles) < 20:
                bt["status"] = "error"
                bt["message"] = f"Недостаточно свечей для {symbol} {interval}"
                return
            
            bt["message"] = f"Запуск {strategy}-стратегии на {len(candles)} свечах... ({speed})"
            bt["total_candles"] = len(candles)
            
            # Run strategy
            if strategy == "rsi":
                report = BacktestRunner._run_rsi(candles, balance, leverage, position_size, bt, params, step_delay)
            elif strategy == "hammer":
                report = BacktestRunner._run_hammer(candles, balance, leverage, position_size, bt, params, step_delay)
            elif strategy == "inverted_hammer":
                report = BacktestRunner._run_inverted_hammer(candles, balance, leverage, position_size, bt, params, step_delay)
            elif strategy == "rsi_trailing":
                report = BacktestRunner._run_rsi_trailing(candles, balance, leverage, position_size, bt, params, step_delay)
            else:
                report = BacktestRunner._run_swing(candles, balance, leverage, position_size, bt, params, step_delay)
            
            # Проверка: отменён?
            if bt.get("cancelled"):
                report["status"] = "CANCELLED"
                report["cancel_reason"] = "Пользователь остановил бэктест досрочно"
                bt["status"] = "cancelled"
                bt["message"] = "⏹ Бэктест остановлен пользователем"
            # Проверка: баланс обнулён?
            elif report.get("end_balance", balance) <= 0:
                report["status"] = "BALANCE_ZERO"
                bt["message"] = "⚠️ Баланс обнулён! Стратегия остановлена."
                bt["status"] = "done"
            else:
                bt["status"] = "done"
            
            bt["report"] = report
            bt["progress"] = 100
            if not bt.get("cancelled"):
                bt["message"] = bt.get("message", "Бэктест завершён")
            
            # Сохраняем файлы в любом случае (и при отмене тоже)
            saved_files = _save_backtest_report(job_id, params, report)
            bt["file"] = str(saved_files[0]) if saved_files else None
            bt["history_file"] = str(saved_files[1]) if saved_files and len(saved_files) > 1 else None
        except Exception as e:
            bt["status"] = "error"
            bt["message"] = str(e)
    
    @staticmethod
    def _run_rsi(candles, balance, leverage, position_size, bt, params, step_delay=0):
        """RSI-стратегия (signal_v6)."""
        import time as _time
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        
        SL_PCT = 0.012
        TP_PCT = 0.024
        COOLDOWN_BARS = 15
        
        trades = []
        equity = [balance]
        last_trade_bar = -COOLDOWN_BARS
        position = None  # {type, entry, sl, tp, bar, margin}
        
        for i in range(50, len(closes)):
            if bt.get("cancelled"): break
            if step_delay: _time.sleep(step_delay)
            bt["progress"] = 30 + int(60 * i / len(closes))
            bt["candles_processed"] = i
            
            # Live stats every 10 candles
            if i % 10 == 0 or i == len(closes) - 1:
                wins = sum(1 for t in trades if t["pnl"] > 0)
                bt["live_balance"] = round(balance, 2)
                bt["live_trades"] = len(trades)
                bt["live_win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0
            
            # Баланс обнулён?
            if balance <= 0:
                bt["message"] = "⚠️ Баланс обнулён! Стратегия остановлена."
                break
            
            # RSI
            window = closes[i-14:i+1]
            gains = sum(max(window[j]-window[j-1], 0) for j in range(1, len(window)))
            losses = sum(max(window[j-1]-window[j], 0) for j in range(1, len(window)))
            rsi_val = 100 - (100/(1+gains/losses)) if losses > 0 else 100
            
            cur = closes[i]
            
            # Check open position
            if position:
                if position["type"] == "BUY":
                    if lows[i] <= position["sl"]:
                        exit_price = position["sl"]
                        reason = "STOP_LOSS"
                    elif highs[i] >= position["tp"]:
                        exit_price = position["tp"]
                        reason = "TAKE_PROFIT"
                    else:
                        continue
                else:
                    if highs[i] >= position["sl"]:
                        exit_price = position["sl"]
                        reason = "STOP_LOSS"
                    elif lows[i] <= position["tp"]:
                        exit_price = position["tp"]
                        reason = "TAKE_PROFIT"
                    else:
                        continue
                
                pnl = (exit_price - position["entry"]) * position["qty"] if position["type"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
                commission = position["entry"] * position["qty"] * 0.0004 * 2
                net_pnl = pnl - commission
                balance += net_pnl
                equity.append(balance)
                
                trades.append({
                    "type": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                    "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                    "reason": reason, "bars": i - position["bar"]
                })
                bt["events"].append({
                    "ts": i, "type": "close",
                    "side": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(net_pnl, 2),
                    "reason": reason, "balance": round(balance, 2)
                })
                position = None
                last_trade_bar = i
                continue
            
            # Cooldown
            if i - last_trade_bar < COOLDOWN_BARS:
                equity.append(balance)
                continue
            
            # Signal
            margin = position_size / leverage
            qty = position_size / cur
            
            if rsi_val < 30:
                sl = round(cur * (1 - SL_PCT), 4)
                tp = round(cur * (1 + TP_PCT), 4)
                position = {"type": "BUY", "entry": cur, "sl": sl, "tp": tp, "bar": i, "qty": qty, "margin": margin}
                bt["events"].append({
                    "ts": i, "type": "signal", "side": "BUY",
                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                    "sl": sl, "tp": tp
                })
            elif rsi_val > 75:
                sl = round(cur * (1 + SL_PCT), 4)
                tp = round(cur * (1 - TP_PCT), 4)
                position = {"type": "SELL", "entry": cur, "sl": sl, "tp": tp, "bar": i, "qty": qty, "margin": margin}
                bt["events"].append({
                    "ts": i, "type": "signal", "side": "SELL",
                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                    "sl": sl, "tp": tp
                })
            else:
                equity.append(balance)
        
        # Close open position at end (or on cancel)
        if position:
            exit_price = closes[-1]
            pnl = (exit_price - position["entry"]) * position["qty"] if position["type"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            
            reason = "CANCELLED" if bt.get("cancelled") else "END_OF_PERIOD"
            trades.append({
                "type": position["type"], "entry": round(position["entry"], 4),
                "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "reason": reason, "bars": len(closes) - position["bar"]
            })
        
        return BacktestRunner._build_report(trades, balance, params)
    
    @staticmethod
    def _run_swing(candles, balance, leverage, position_size, bt, params, step_delay=0):
        """SWING-стратегия."""
        import time as _time
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        
        SL_PCT = 0.025
        TP_PCT = 0.05
        COOLDOWN_BARS = 60
        
        trades = []
        equity = [balance]
        last_trade_bar = -COOLDOWN_BARS
        position = None
        
        for i in range(50, len(closes)):
            if bt.get("cancelled"): break
            if step_delay: _time.sleep(step_delay)
            bt["progress"] = 30 + int(60 * i / len(closes))
            bt["candles_processed"] = i
            
            # Live stats every 10 candles
            if i % 10 == 0 or i == len(closes) - 1:
                wins = sum(1 for t in trades if t["pnl"] > 0)
                bt["live_balance"] = round(balance, 2)
                bt["live_trades"] = len(trades)
                bt["live_win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0
            
            # Баланс обнулён?
            if balance <= 0:
                bt["message"] = "⚠️ Баланс обнулён! Стратегия остановлена."
                break
            
            window = closes[i-14:i+1]
            gains = sum(max(window[j]-window[j-1], 0) for j in range(1, len(window)))
            losses = sum(max(window[j-1]-window[j], 0) for j in range(1, len(window)))
            rsi_val = 100 - (100/(1+gains/losses)) if losses > 0 else 100
            
            cur = closes[i]
            
            # Check position
            if position:
                if position["type"] == "BUY":
                    if lows[i] <= position["sl"]:
                        exit_price, reason = position["sl"], "STOP_LOSS"
                    elif highs[i] >= position["tp"]:
                        exit_price, reason = position["tp"], "TAKE_PROFIT"
                    else:
                        if cur > position.get("best", position["entry"]):
                            position["best"] = cur
                            if position["best"] >= position["entry"] * 1.01:
                                position["sl"] = max(position["sl"], round(position["best"] * 0.99, 4))
                        continue
                else:
                    if highs[i] >= position["sl"]:
                        exit_price, reason = position["sl"], "STOP_LOSS"
                    elif lows[i] <= position["tp"]:
                        exit_price, reason = position["tp"], "TAKE_PROFIT"
                    else:
                        if cur < position.get("best", position["entry"]):
                            position["best"] = cur
                            if position["best"] <= position["entry"] * 0.99:
                                position["sl"] = min(position["sl"], round(position["best"] * 1.01, 4))
                        continue
                
                pnl = (exit_price - position["entry"]) * position["qty"] if position["type"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
                commission = position["entry"] * position["qty"] * 0.0004 * 2
                net_pnl = pnl - commission
                balance += net_pnl
                equity.append(balance)
                
                exit_time = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                trades.append({
                    "type": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                    "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                    "reason": reason, "bars": i - position["bar"],
                    "entry_time": position.get("entry_time", ""),
                    "exit_time": exit_time
                })
                bt["events"].append({
                    "ts": i, "type": "close",
                    "side": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(net_pnl, 2),
                    "reason": reason, "balance": round(balance, 2)
                })
                position = None
                last_trade_bar = i
                continue
            
            if i - last_trade_bar < COOLDOWN_BARS:
                equity.append(balance)
                continue
            
            margin = position_size / leverage
            qty = position_size / cur
            
            if rsi_val < 30:
                sl = round(cur * (1 - SL_PCT), 4)
                tp = round(cur * (1 + TP_PCT), 4)
                entry_time = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                position = {"type": "BUY", "entry": cur, "sl": sl, "tp": tp, "bar": i, "qty": qty, "margin": margin, "best": cur, "entry_time": entry_time}
                bt["events"].append({
                    "ts": i, "type": "signal", "side": "BUY",
                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                    "sl": sl, "tp": tp
                })
            elif rsi_val > 75:
                sl = round(cur * (1 + SL_PCT), 4)
                tp = round(cur * (1 - TP_PCT), 4)
                entry_time = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                position = {"type": "SELL", "entry": cur, "sl": sl, "tp": tp, "bar": i, "qty": qty, "margin": margin, "best": cur, "entry_time": entry_time}
                bt["events"].append({
                    "ts": i, "type": "signal", "side": "SELL",
                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                    "sl": sl, "tp": tp
                })
            else:
                equity.append(balance)
        
        if position:
            exit_price = closes[-1]
            pnl = (exit_price - position["entry"]) * position["qty"] if position["type"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            reason = "CANCELLED" if bt.get("cancelled") else "END_OF_PERIOD"
            exit_time = datetime.fromtimestamp(candles[-1]["time"]).strftime("%Y-%m-%d %H:%M:%S")
            trades.append({
                "type": position["type"], "entry": round(position["entry"], 4),
                "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "reason": reason, "bars": len(closes) - position["bar"],
                "entry_time": position.get("entry_time", ""),
                "exit_time": exit_time
            })
        
        return BacktestRunner._build_report(trades, balance, params)
    
    @staticmethod
    def _run_rsi_trailing(candles, balance, leverage, position_size, bt, params, step_delay=0):
        """RSI с трейлинг-стопом — без фиксированного TP, SL скользит за ценой."""
        import time as _time
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        
        SL_PCT = 0.012        # 1.2% — расстояние трейлинг-стопа
        TRAIL_ACTIVATE = 0.005  # 0.5% — после этого стоп начинает скользить
        COOLDOWN_BARS = 15
        
        trades = []
        equity = [balance]
        last_trade_bar = -COOLDOWN_BARS
        position = None  # {type, entry, sl, bar, qty, margin, best}
        
        for i in range(50, len(closes)):
            if bt.get("cancelled"): break
            if step_delay: _time.sleep(step_delay)
            bt["progress"] = 30 + int(60 * i / len(closes))
            bt["candles_processed"] = i
            
            if i % 10 == 0 or i == len(closes) - 1:
                wins = sum(1 for t in trades if t["pnl"] > 0)
                bt["live_balance"] = round(balance, 2)
                bt["live_trades"] = len(trades)
                bt["live_win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0
            
            if balance <= 0:
                bt["message"] = "⚠️ Баланс обнулён! Стратегия остановлена."
                break
            
            # RSI
            window = closes[i-14:i+1]
            gains = sum(max(window[j]-window[j-1], 0) for j in range(1, len(window)))
            losses = sum(max(window[j-1]-window[j], 0) for j in range(1, len(window)))
            rsi_val = 100 - (100/(1+gains/losses)) if losses > 0 else 100
            
            cur = closes[i]
            
            # Check position — только трейлинг-стоп
            if position:
                # Обновляем best и SL
                if position["type"] == "BUY":
                    if cur > position["best"]:
                        position["best"] = cur
                    # Активируем трейлинг после TRAIL_ACTIVATE
                    if position["best"] >= position["entry"] * (1 + TRAIL_ACTIVATE):
                        position["sl"] = round(position["best"] * (1 - SL_PCT), 4)
                    # Проверка: пробит ли SL?
                    if lows[i] <= position["sl"]:
                        exit_price = position["sl"]
                        reason = "TRAILING_STOP"
                    else:
                        continue
                else:  # SELL
                    if cur < position["best"]:
                        position["best"] = cur
                    if position["best"] <= position["entry"] * (1 - TRAIL_ACTIVATE):
                        position["sl"] = round(position["best"] * (1 + SL_PCT), 4)
                    if highs[i] >= position["sl"]:
                        exit_price = position["sl"]
                        reason = "TRAILING_STOP"
                    else:
                        continue
                
                pnl = (exit_price - position["entry"]) * position["qty"] if position["type"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
                commission = position["entry"] * position["qty"] * 0.0004 * 2
                net_pnl = pnl - commission
                balance += net_pnl
                equity.append(balance)
                
                trades.append({
                    "type": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                    "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                    "reason": reason, "bars": i - position["bar"]
                })
                bt["events"].append({
                    "ts": i, "type": "close",
                    "side": position["type"], "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4), "pnl": round(net_pnl, 2),
                    "reason": reason, "balance": round(balance, 2)
                })
                position = None
                last_trade_bar = i
                continue
            
            # Cooldown
            if i - last_trade_bar < COOLDOWN_BARS:
                equity.append(balance)
                continue
            
            # Signal — только RSI, без фильтров
            margin = position_size / leverage
            qty = position_size / cur
            
            if rsi_val < 30:
                sl = round(cur * (1 - SL_PCT), 4)
                position = {"type": "BUY", "entry": cur, "sl": sl, "bar": i, "qty": qty, "margin": margin, "best": cur}
                bt["events"].append({
                    "ts": i, "type": "signal", "side": "BUY",
                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                    "sl": sl, "tp": "TRAILING"
                })
            elif rsi_val > 75:
                sl = round(cur * (1 + SL_PCT), 4)
                position = {"type": "SELL", "entry": cur, "sl": sl, "bar": i, "qty": qty, "margin": margin, "best": cur}
                bt["events"].append({
                    "ts": i, "type": "signal", "side": "SELL",
                    "price": round(cur, 4), "rsi": round(rsi_val, 1),
                    "sl": sl, "tp": "TRAILING"
                })
            else:
                equity.append(balance)
        
        # Close open
        if position:
            exit_price = closes[-1]
            pnl = (exit_price - position["entry"]) * position["qty"] if position["type"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            reason = "CANCELLED" if bt.get("cancelled") else "END_OF_PERIOD"
            exit_time = datetime.fromtimestamp(candles[-1]["time"]).strftime("%Y-%m-%d %H:%M:%S")
            trades.append({
                "type": position["type"], "entry": round(position["entry"], 4),
                "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "reason": reason, "bars": len(closes) - position["bar"],
                "entry_time": position.get("entry_time", ""),
                "exit_time": exit_time
            })
        
        return BacktestRunner._build_report(trades, balance, params)
    
    @staticmethod
    def _run_hammer(candles, balance, leverage, position_size, bt, params, step_delay=0):
        """Hammer-стратегия: свечной паттерн Hammer (Молот) с RR 1:2.
        Улучшения v2: SL 0.5%, подтверждение свечой, volume-фильтр, таймаут 30."""
        import time as _time
        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        opens  = [c["open"]  for c in candles]
        volumes = [c.get("volume", 0) for c in candles]
        
        COOLDOWN_BARS = 5         # минимум свечей между сделками
        RR_RATIO = 2.0            # Risk/Reward 1:2
        TIMEOUT_BARS = 30         # выход по времени через 30 свечей
        MIN_TREND_BARS = 3        # минимум свечей падения перед молотом
        SL_BUFFER = 0.995         # стоп-лосс: 0.5% ниже минимума молота
        VOL_PERIOD = 10           # период для расчёта среднего объёма
        
        trades = []
        last_trade_bar = -COOLDOWN_BARS
        position = None  # {type, entry, sl, tp, bar, qty}
        
        def is_hammer(i):
            """Проверка: свеча i — молот?"""
            body = abs(closes[i] - opens[i])
            lower_shadow = min(opens[i], closes[i]) - lows[i]
            upper_shadow = highs[i] - max(opens[i], closes[i])
            total_range = highs[i] - lows[i]
            
            if total_range == 0:
                return False
            if body == 0:
                # Doji-hammer: длинная нижняя тень без тела
                return lower_shadow >= total_range * 0.6 and upper_shadow <= total_range * 0.1
            
            body_upper = max(opens[i], closes[i])
            return (lower_shadow >= body * 2.0 and
                    upper_shadow <= body * 0.3 and
                    body_upper >= lows[i] + total_range * 0.7)
        
        def is_downtrend(i, n=MIN_TREND_BARS):
            """Проверка: последние n свечей падают?"""
            if i < n + 1:
                return False
            return all(closes[i-j] < closes[i-j-1] for j in range(1, n+1))
        
        def has_volume_confirmation(i, period=VOL_PERIOD):
            """Проверка: объём молота выше среднего за period свечей?"""
            if i < period:
                return False
            avg_vol = sum(volumes[i-period:i]) / period
            return volumes[i] > avg_vol * 1.2  # объём должен быть на 20% выше среднего
        
        for i in range(10, len(closes)):
            if bt.get("cancelled"):
                break
            if step_delay:
                _time.sleep(step_delay)
            bt["progress"] = 30 + int(60 * i / len(closes))
            bt["candles_processed"] = i
            
            # Live stats
            if i % 10 == 0 or i == len(closes) - 1:
                wins = sum(1 for t in trades if t["pnl"] > 0)
                bt["live_balance"] = round(balance, 2)
                bt["live_trades"] = len(trades)
                bt["live_win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0
            
            if balance <= 0:
                bt["message"] = "⚠️ Баланс обнулён!"
                break
            
            cur = closes[i]
            
            # Проверка открытой позиции
            if position:
                hit_sl = (position["type"] == "BUY" and lows[i] <= position["sl"])
                hit_tp = (position["type"] == "BUY" and highs[i] >= position["tp"])
                timed_out = (i - position["bar"] >= TIMEOUT_BARS)
                
                if hit_sl:
                    exit_price = position["sl"]
                    reason = "STOP_LOSS"
                elif hit_tp:
                    exit_price = position["tp"]
                    reason = "TAKE_PROFIT"
                elif timed_out:
                    exit_price = cur
                    reason = "TIMEOUT"
                else:
                    continue
                
                pnl = (exit_price - position["entry"]) * position["qty"]
                commission = position["entry"] * position["qty"] * 0.0004 * 2
                net_pnl = pnl - commission
                balance += net_pnl
                exit_time = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                trades.append({
                    "type": position["type"],
                    "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4),
                    "pnl": round(net_pnl, 4),
                    "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                    "reason": reason,
                    "bars": i - position["bar"],
                    "detected_at": position.get("detected_at", ""),
                    "entry_time": position.get("entry_time", ""),
                    "exit_time": exit_time
                })
                position = None
                continue
            
            # Кулдаун
            if i - last_trade_bar < COOLDOWN_BARS:
                continue
            
            # Поиск молота
            if is_hammer(i) and is_downtrend(i) and has_volume_confirmation(i):
                # Подтверждение: следующая свеча должна закрыться выше максимума молота
                if i + 1 >= len(closes) or closes[i + 1] <= highs[i]:
                    continue  # нет подтверждения — пропускаем
                
                # Вход на открытии свечи после подтверждения
                entry_bar = i + 2 if i + 2 < len(closes) else i + 1
                entry = closes[entry_bar]
                sl = lows[i] * SL_BUFFER  # 0.5% ниже минимума молота
                risk = entry - sl
                if risk <= 0:
                    continue
                tp = entry + risk * RR_RATIO
                
                # Размер позиции
                margin = position_size * leverage
                qty = margin / entry
                
                entry_time = datetime.fromtimestamp(candles[entry_bar]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                detected_at = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                
                position = {
                    "type": "BUY",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "bar": i,
                    "qty": qty,
                    "entry_time": entry_time,
                    "detected_at": detected_at
                }
                last_trade_bar = i
        
        # Закрыть открытую позицию в конце
        if position:
            exit_price = closes[-1]
            pnl = (exit_price - position["entry"]) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            exit_time = datetime.fromtimestamp(candles[-1]["time"]).strftime("%Y-%m-%d %H:%M:%S")
            trades.append({
                "type": position["type"],
                "entry": round(position["entry"], 4),
                "exit": round(exit_price, 4),
                "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "reason": "END_OF_PERIOD",
                "bars": len(closes) - position["bar"],
                "detected_at": position.get("detected_at", ""),
                "entry_time": position.get("entry_time", ""),
                "exit_time": exit_time
            })
        
        return BacktestRunner._build_report(trades, balance, params)
    
    @staticmethod
    def _run_inverted_hammer(candles, balance, leverage, position_size, bt, params, step_delay=0):
        """Inverted Hammer (Обратный молот) — зеркало Hammer v2 для SELL.
        Длинная верхняя тень, тело в нижней трети, после восходящего тренда.
        SL 0.5% выше максимума, TP по RR 1:2, volume-фильтр, подтверждение."""
        import time as _time
        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        opens  = [c["open"]  for c in candles]
        volumes = [c.get("volume", 0) for c in candles]
        
        COOLDOWN_BARS = 5
        RR_RATIO = 2.0
        TIMEOUT_BARS = 30
        MIN_TREND_BARS = 3
        SL_BUFFER = 1.005         # стоп-лосс: 0.5% выше максимума обратного молота
        VOL_PERIOD = 10
        
        trades = []
        last_trade_bar = -COOLDOWN_BARS
        position = None
        
        def is_inverted_hammer(i):
            """Проверка: свеча i — обратный молот?"""
            body = abs(closes[i] - opens[i])
            upper_shadow = highs[i] - max(opens[i], closes[i])
            lower_shadow = min(opens[i], closes[i]) - lows[i]
            total_range = highs[i] - lows[i]
            
            if total_range == 0:
                return False
            if body == 0:
                # Doji inverted hammer: длинная верхняя тень без тела
                return upper_shadow >= total_range * 0.6 and lower_shadow <= total_range * 0.1
            
            body_lower = min(opens[i], closes[i])
            return (upper_shadow >= body * 2.0 and
                    lower_shadow <= body * 0.3 and
                    body_lower <= lows[i] + total_range * 0.3)
        
        def is_uptrend(i, n=MIN_TREND_BARS):
            """Проверка: последние n свечей растут?"""
            if i < n + 1:
                return False
            return all(closes[i-j] > closes[i-j-1] for j in range(1, n+1))
        
        def has_volume_confirmation(i, period=VOL_PERIOD):
            """Проверка: объём выше среднего за period свечей?"""
            if i < period:
                return False
            avg_vol = sum(volumes[i-period:i]) / period
            return volumes[i] > avg_vol * 1.2
        
        for i in range(10, len(closes)):
            if bt.get("cancelled"):
                break
            if step_delay:
                _time.sleep(step_delay)
            bt["progress"] = 30 + int(60 * i / len(closes))
            bt["candles_processed"] = i
            
            if i % 10 == 0 or i == len(closes) - 1:
                wins = sum(1 for t in trades if t["pnl"] > 0)
                bt["live_balance"] = round(balance, 2)
                bt["live_trades"] = len(trades)
                bt["live_win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0
            
            if balance <= 0:
                bt["message"] = "⚠️ Баланс обнулён!"
                break
            
            cur = closes[i]
            
            # Проверка открытой позиции (SELL)
            if position:
                hit_sl = (position["type"] == "SELL" and highs[i] >= position["sl"])
                hit_tp = (position["type"] == "SELL" and lows[i] <= position["tp"])
                timed_out = (i - position["bar"] >= TIMEOUT_BARS)
                
                if hit_sl:
                    exit_price = position["sl"]
                    reason = "STOP_LOSS"
                elif hit_tp:
                    exit_price = position["tp"]
                    reason = "TAKE_PROFIT"
                elif timed_out:
                    exit_price = cur
                    reason = "TIMEOUT"
                else:
                    continue
                
                pnl = (position["entry"] - exit_price) * position["qty"]
                commission = position["entry"] * position["qty"] * 0.0004 * 2
                net_pnl = pnl - commission
                balance += net_pnl
                exit_time = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                trades.append({
                    "type": position["type"],
                    "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4),
                    "pnl": round(net_pnl, 4),
                    "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                    "reason": reason,
                    "bars": i - position["bar"],
                    "detected_at": position.get("detected_at", ""),
                    "entry_time": position.get("entry_time", ""),
                    "exit_time": exit_time
                })
                position = None
                continue
            
            # Кулдаун
            if i - last_trade_bar < COOLDOWN_BARS:
                continue
            
            # Поиск обратного молота
            if is_inverted_hammer(i) and is_uptrend(i) and has_volume_confirmation(i):
                # Подтверждение: следующая свеча должна закрыться НИЖЕ минимума обратного молота
                if i + 1 >= len(closes) or closes[i + 1] >= lows[i]:
                    continue
                
                # Вход на открытии свечи после подтверждения
                entry_bar = i + 2 if i + 2 < len(closes) else i + 1
                entry = closes[entry_bar]
                sl = highs[i] * SL_BUFFER  # 0.5% выше максимума обратного молота
                risk = sl - entry
                if risk <= 0:
                    continue
                tp = entry - risk * RR_RATIO
                
                margin = position_size * leverage
                qty = margin / entry
                
                entry_time = datetime.fromtimestamp(candles[entry_bar]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                detected_at = datetime.fromtimestamp(candles[i]["time"]).strftime("%Y-%m-%d %H:%M:%S")
                
                position = {
                    "type": "SELL",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "bar": i,
                    "qty": qty,
                    "entry_time": entry_time,
                    "detected_at": detected_at
                }
                last_trade_bar = i
        
        # Закрыть открытую позицию в конце
        if position:
            exit_price = closes[-1]
            pnl = (position["entry"] - exit_price) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            exit_time = datetime.fromtimestamp(candles[-1]["time"]).strftime("%Y-%m-%d %H:%M:%S")
            trades.append({
                "type": position["type"],
                "entry": round(position["entry"], 4),
                "exit": round(exit_price, 4),
                "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "reason": "END_OF_PERIOD",
                "bars": len(closes) - position["bar"],
                "detected_at": position.get("detected_at", ""),
                "entry_time": position.get("entry_time", ""),
                "exit_time": exit_time
            })
        
        return BacktestRunner._build_report(trades, balance, params)
    
    @staticmethod
    def _build_report(trades, end_balance, params):
        start_balance = float(params["balance"])
        winning = [t for t in trades if t["pnl"] > 0]
        losing = [t for t in trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in trades)
        
        # Max drawdown
        peak = start_balance
        max_dd = 0
        running = start_balance
        for t in trades:
            running += t["pnl"]
            peak = max(peak, running)
            dd = (peak - running) / peak * 100
            max_dd = max(max_dd, dd)
        
        return {
            "start_balance": round(start_balance, 2),
            "end_balance": round(end_balance, 2),
            "net_pnl": round(total_pnl, 2),
            "total_trades": len(trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": round(len(winning)/len(trades)*100, 1) if trades else 0,
            "avg_pnl": round(total_pnl/len(trades), 2) if trades else 0,
            "max_drawdown": round(max_dd, 1),
            "trades_list": trades[-10:],  # last 10 trades
        }


def _save_backtest_report(job_id, params, report):
    """Сохраняет ДВА файла:
    1) Индивидуальный (с датой запуска) — backtest_{strategy}_{pair}_{timestamp}.json
    2) Историю (накапливается) — backtest_{strategy}_{pair}_{timeframe}.json
    """
    import os
    out_dir = Path("/home/oleg/workspace/crypto-ton/backtests")
    os.makedirs(out_dir, exist_ok=True)

    strategy = params.get('strategy', '?')
    pair = params.get('pair', '?')
    tf = params.get('timeframe', '?')
    ts = datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S")

    # === Файл 1: индивидуальный запуск (старый формат) ===
    run_file = out_dir / f"backtest_{strategy}_{pair}_{ts_str}.json"
    run_data = {
        "job_id": job_id,
        "timestamp": ts.isoformat(),
        "params": {
            "strategy": strategy,
            "pair": pair,
            "timeframe": tf,
            "leverage": int(params.get("leverage", 1)),
            "balance": float(params.get("balance", 0)),
            "position_size": float(params.get("position_size", 0)),
            "period": int(params.get("period", 1)),
        },
        "report": report,
    }
    try:
        run_file.write_text(json.dumps(run_data, default=str, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[Backtest] Failed to save run file: {e}", file=sys.stderr)

    # === Файл 2: история (накапливается по strategy+pair+timeframe) ===
    history_file = out_dir / f"backtest_{strategy}_{pair}_{tf}.json"
    run_entry = {
        "id": job_id,
        "timestamp": ts.isoformat(),
        "balance": float(params.get("balance", 0)),
        "leverage": int(params.get("leverage", 1)),
        "position_size": float(params.get("position_size", 0)),
        "period": int(params.get("period", 1)),
        "report": report,
    }

    existing = None
    if history_file.exists():
        try:
            existing = json.loads(history_file.read_text())
        except Exception:
            existing = None

    if existing and isinstance(existing, dict) and "runs" in existing:
        existing["runs"].append(run_entry)
        existing["total_runs"] = len(existing["runs"])
        existing["updated_at"] = ts.isoformat()
        all_reports = [r["report"] for r in existing["runs"]]
        existing["summary"] = _compute_history_summary(all_reports)
    else:
        existing = {
            "strategy": strategy,
            "pair": pair,
            "timeframe": tf,
            "created_at": ts.isoformat(),
            "updated_at": ts.isoformat(),
            "total_runs": 1,
            "summary": _compute_history_summary([report]),
            "runs": [run_entry],
        }

    try:
        history_file.write_text(json.dumps(existing, default=str, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[Backtest] Failed to save history: {e}", file=sys.stderr)

    return str(run_file), str(history_file)


def _get_strategy_source(strategy: str, params: dict = None, pair: str = "TONUSDT") -> str:
    """Возвращает исходный код стратегии как строку Python-скрипта."""
    # Build params comment block
    params_block = ""
    if params:
        params_block = (
            "# ── Параметры запуска (с сайта) ──\n"
            f"# Пара:           {pair}\n"
            f"# Баланс:         ${params.get('balance', 'N/A')}\n"
            f"# Плечо:          {params.get('leverage', 'N/A')}×\n"
            f"# Сумма сделки:   ${params.get('position_size', 'N/A')}\n"
            f"# Период:         {params.get('period', 'N/A')} дн.\n"
            "#\n\n"
        )
    
    if strategy == "rsi":
        code = params_block + '''"""
RSI-стратегия (signal_v6) — бэктест на исторических свечах Binance.
Стратегия: RSI < 30 → BUY, RSI > 75 → SELL.
Управление рисками: Stop Loss 1.2%, Take Profit 2.4%, кулдаун 15 свечей.
"""

{params_block}import time
import json

# ── Параметры ──
SL_PCT   = 0.012   # Стоп-лосс 1.2%
TP_PCT   = 0.024   # Тейк-профит 2.4%
COOLDOWN = 15      # Минимальное расстояние между сделками (в свечах)

def run(candles, balance, leverage, position_size):
    """Прогон стратегии на массиве свечей [{open,high,low,close,volume},...]"""
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    trades = []
    equity = [balance]
    last_trade_bar = -COOLDOWN
    position = None  # {type, entry, sl, tp, bar, margin}

    for i in range(50, len(closes)):
        # Баланс обнулён → остановка
        if balance <= 0:
            break

        # ── Расчёт RSI(14) ──
        window = closes[i-14:i+1]
        gains = sum(max(window[j]-window[j-1], 0) for j in range(1, len(window)))
        losses = sum(max(window[j-1]-window[j], 0) for j in range(1, len(window)))
        rsi = 100 - (100 / (1 + gains/losses)) if losses > 0 else 100

        cur = closes[i]

        # ── Проверка открытой позиции ──
        if position:
            if position["type"] == "BUY":
                if lows[i] <= position["sl"]:
                    exit_px = position["sl"]; reason = "STOP_LOSS"
                elif highs[i] >= position["tp"]:
                    exit_px = position["tp"]; reason = "TAKE_PROFIT"
                else:
                    continue
            else:  # SELL
                if highs[i] >= position["sl"]:
                    exit_px = position["sl"]; reason = "STOP_LOSS"
                elif lows[i] <= position["tp"]:
                    exit_px = position["tp"]; reason = "TAKE_PROFIT"
                else:
                    continue

            pnl_pct = (cur - exit_px) / position["entry"] * leverage
            if position["type"] == "SELL":
                pnl_pct = -pnl_pct
            pnl = position["margin"] * pnl_pct / 100
            balance += pnl
            trades.append({
                "type": position["type"], "entry": position["entry"],
                "exit": exit_px, "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2), "reason": reason,
                "bars": i - position["bar"]
            })
            position = None
            equity.append(balance)
            continue

        # ── Кулдаун ──
        if i - last_trade_bar < COOLDOWN:
            equity.append(balance)
            continue

        # ── Генерация сигнала ──
        if rsi < 30:
            side = "BUY"
            sl = cur * (1 - SL_PCT)
            tp = cur * (1 + TP_PCT)
        elif rsi > 75:
            side = "SELL"
            sl = cur * (1 + SL_PCT)
            tp = cur * (1 - TP_PCT)
        else:
            equity.append(balance)
            continue

        margin = position_size * leverage
        position = {"type": side, "entry": cur, "sl": sl, "tp": tp, "bar": i, "margin": margin}
        last_trade_bar = i
        equity.append(balance)

    # ── Итоговый отчёт ──
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "start_balance": equity[0],
        "end_balance": round(balance, 2),
        "net_pnl": round(balance - equity[0], 2),
        "total_trades": len(trades),
        "winning_trades": wins,
        "losing_trades": len(trades) - wins,
        "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
        "max_drawdown": round(max(1 - e/max(equity[:i+1]) for i, e in enumerate(equity)), 4) * 100,
        "trades_list": trades,
    }


# ═══════════════════════════════════════════════════════════
# КАК ПАРАМЕТРЫ ВЛИЯЮТ НА ТОРГОВЛЮ
# ═══════════════════════════════════════════════════════════
#
# БАЛАНС (balance) — стартовый депозит.
#   Чем больше баланс → крупнее позиции → выше прибыль/убыток.
#   Если баланс упадёт до 0, стратегия немедленно остановится.
#
# ПЛЕЧО (leverage) — кредитное плечо.
#   Усиливает P&L: pnl = margin × (изменение_цены% × leverage).
#   Плечо 1× = без плеча, 3× = утроенный риск/доход.
#   Формула: margin = position_size × leverage.
#
# СУММА СДЕЛКИ (position_size) — размер одной позиции в $.
#   Именно эта сумма умножается на leverage для расчёта маржи.
#   $50 × плечо 3× = позиция на $150 (маржа $50).
#
# ПЕРИОД (period) — на сколько дней загружаются свечи.
#   1 день ≈ 288 свечей на 5m, 7 дней ≈ 2016 свечей.
#   Больше свечей = больше истории для RSI-сигналов.
#
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Этот блок показывает, как стратегия запускается с реальными параметрами.
    # В бэктестере candles загружаются через Binance API, затем вызывается run().

    # Пример: загрузка свечей (в реальном бэктестере — через Binance API)
    # candles = fetch_candles(symbol="{PAIR}", interval="5m", days=period)

    # Пример свечи: {"open":2.0, "high":2.01, "low":1.99, "close":2.005, "volume":12345}
    candles = []  # ← здесь будут исторические свечи с Binance

    report = run(
        candles=candles,
        balance=balance,           # ← стартовый депозит из настроек
        leverage=leverage,         # ← кредитное плечо из настроек
        position_size=position_size  # ← размер позиции из настроек
    )

    print(f"Стартовый баланс:  ${report['start_balance']}")
    print(f"Конечный баланс:   ${report['end_balance']}")
    print(f"Чистый P&L:        ${report['net_pnl']}")
    print(f"Всего сделок:      {report['total_trades']}")
    print(f"Винрейт:           {report['win_rate']}%")
    print(f"Макс. просадка:    {report['max_drawdown']}%")
'''.replace("{PAIR}", pair)
        return code

    elif strategy == "swing":
        code = params_block + '''"""
SWING-стратегия — долгосрочная торговля с широкими SL/TP и трейлинг-стопом.
RSI < 30 → BUY, RSI > 75 → SELL.
Управление: Stop Loss 2.5%, Take Profit 5%, кулдаун 60 свечей, трейлинг 1%.
"""
import time

SL_PCT    = 0.025   # Стоп-лосс 2.5%
TP_PCT    = 0.05    # Тейк-профит 5%
TRAIL_PCT = 0.01    # Трейлинг-стоп: 1% от максимума
COOLDOWN  = 60      # Кулдаун между сделками

def run(candles, balance, leverage, position_size):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    trades = []
    equity = [balance]
    last_trade_bar = -COOLDOWN
    position = None  # {type, entry, sl, tp, bar, margin, trail_high}

    for i in range(50, len(closes)):
        if balance <= 0:
            break

        window = closes[i-14:i+1]
        gains = sum(max(window[j]-window[j-1], 0) for j in range(1, len(window)))
        losses = sum(max(window[j-1]-window[j], 0) for j in range(1, len(window)))
        rsi = 100 - (100 / (1 + gains/losses)) if losses > 0 else 100

        cur = closes[i]

        if position:
            # Трейлинг-стоп
            if position["type"] == "BUY" and highs[i] > position.get("trail_high", position["entry"]):
                position["trail_high"] = highs[i]
                position["sl"] = max(position["sl"], highs[i] * (1 - TRAIL_PCT))

            if position["type"] == "BUY":
                if lows[i] <= position["sl"]:
                    exit_px = position["sl"]; reason = "TRAILING_STOP"
                elif highs[i] >= position["tp"]:
                    exit_px = position["tp"]; reason = "TAKE_PROFIT"
                else:
                    equity.append(balance); continue
            else:
                if highs[i] >= position["sl"]:
                    exit_px = position["sl"]; reason = "TRAILING_STOP"
                elif lows[i] <= position["tp"]:
                    exit_px = position["tp"]; reason = "TAKE_PROFIT"
                else:
                    equity.append(balance); continue

            pnl_pct = (cur - exit_px) / position["entry"] * leverage
            if position["type"] == "SELL":
                pnl_pct = -pnl_pct
            pnl = position["margin"] * pnl_pct / 100
            balance += pnl
            trades.append({
                "type": position["type"], "entry": position["entry"],
                "exit": exit_px, "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2), "reason": reason,
                "bars": i - position["bar"]
            })
            position = None
            equity.append(balance)
            continue

        if i - last_trade_bar < COOLDOWN:
            equity.append(balance); continue

        if rsi < 30:
            side = "BUY"
            sl = cur * (1 - SL_PCT)
            tp = cur * (1 + TP_PCT)
        elif rsi > 75:
            side = "SELL"
            sl = cur * (1 + SL_PCT)
            tp = cur * (1 - TP_PCT)
        else:
            equity.append(balance); continue

        margin = position_size * leverage
        position = {
            "type": side, "entry": cur, "sl": sl, "tp": tp,
            "bar": i, "margin": margin, "trail_high": cur
        }
        last_trade_bar = i
        equity.append(balance)

    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "start_balance": equity[0],
        "end_balance": round(balance, 2),
        "net_pnl": round(balance - equity[0], 2),
        "total_trades": len(trades),
        "winning_trades": wins,
        "losing_trades": len(trades) - wins,
        "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
        "max_drawdown": round(max(1 - e/max(equity[:i+1]) for i, e in enumerate(equity)), 4) * 100,
        "trades_list": trades,
    }


# ═══════════════════════════════════════════════════════════
# КАК ПАРАМЕТРЫ ВЛИЯЮТ НА ТОРГОВЛЮ
# ═══════════════════════════════════════════════════════════
#
# БАЛАНС (balance) — стартовый депозит. Если упадёт до 0 → стоп.
#
# ПЛЕЧО (leverage) — усиление P&L:
#   margin = position_size × leverage
#   При трейлинг-стопе плечо влияет на финальный P&L сделки.
#
# СУММА СДЕЛКИ (position_size) — базовый размер позиции до плеча.
#
# ПЕРИОД (period) — сколько дней истории загружается.
#   SWING требует МНОГО данных: кулдаун 60 свечей требует
#   минимум 2-3 дня на 1H, 7-14 дней на 4H.
#
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Точка входа — так бэктестер вызывает стратегию
    # candles = fetch_candles(symbol="{PAIR}", interval="1h", days=period)
    candles = []
    report = run(
        candles=candles,
        balance=balance,           # ← стартовый депозит
        leverage=leverage,         # ← кредитное плечо
        position_size=position_size  # ← размер позиции
    )
    print(f"P&L: ${report['net_pnl']} | Сделок: {report['total_trades']} | Винрейт: {report['win_rate']}%")
'''.replace("{PAIR}", pair)
        return code

    elif strategy == "rsi_trailing":
        code = params_block + '''"""
RSI Трейлинг-стоп — RSI-стратегия БЕЗ фиксированного тейк-профита.
Стоп-лосс (1.2%) скользит за ценой, когда она идёт в нашу сторону.
Сделка закрывается ТОЛЬКО когда цена разворачивается и бьёт по стопу.
Активация трейлинга после +0.5% в профите.
"""
import time

SL_PCT       = 0.012   # Начальный стоп-лосс 1.2%
TRAIL_ACTIVE = 0.005   # Активация трейлинга после +0.5%
COOLDOWN     = 15      # Кулдаун между сделками

def run(candles, balance, leverage, position_size):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    trades = []
    equity = [balance]
    last_trade_bar = -COOLDOWN
    position = None  # {type, entry, sl, bar, margin, trailing_active, trail_high}

    for i in range(50, len(closes)):
        if balance <= 0:
            break

        window = closes[i-14:i+1]
        gains = sum(max(window[j]-window[j-1], 0) for j in range(1, len(window)))
        losses = sum(max(window[j-1]-window[j], 0) for j in range(1, len(window)))
        rsi = 100 - (100 / (1 + gains/losses)) if losses > 0 else 100

        cur = closes[i]

        if position:
            # Активация трейлинга
            if not position.get("trailing_active"):
                if position["type"] == "BUY" and cur >= position["entry"] * (1 + TRAIL_ACTIVE):
                    position["trailing_active"] = True
                    position["trail_high"] = cur
                elif position["type"] == "SELL" and cur <= position["entry"] * (1 - TRAIL_ACTIVE):
                    position["trailing_active"] = True
                    position["trail_low"] = cur

            # Обновление трейлинг-стопа
            if position.get("trailing_active"):
                if position["type"] == "BUY" and highs[i] > position.get("trail_high", position["entry"]):
                    position["trail_high"] = highs[i]
                    position["sl"] = highs[i] * (1 - SL_PCT)
                elif position["type"] == "SELL" and lows[i] < position.get("trail_low", position["entry"]):
                    position["trail_low"] = lows[i]
                    position["sl"] = lows[i] * (1 + SL_PCT)

            # Проверка стопа (тейк-профита НЕТ)
            if position["type"] == "BUY" and lows[i] <= position["sl"]:
                exit_px = position["sl"]; reason = "TRAILING_STOP"
            elif position["type"] == "SELL" and highs[i] >= position["sl"]:
                exit_px = position["sl"]; reason = "TRAILING_STOP"
            else:
                equity.append(balance); continue

            pnl_pct = (cur - exit_px) / position["entry"] * leverage
            if position["type"] == "SELL":
                pnl_pct = -pnl_pct
            pnl = position["margin"] * pnl_pct / 100
            balance += pnl
            trades.append({
                "type": position["type"], "entry": position["entry"],
                "exit": exit_px, "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2), "reason": reason,
                "bars": i - position["bar"]
            })
            position = None
            equity.append(balance)
            continue

        if i - last_trade_bar < COOLDOWN:
            equity.append(balance); continue

        if rsi < 30:
            side = "BUY"
            sl = cur * (1 - SL_PCT)
        elif rsi > 75:
            side = "SELL"
            sl = cur * (1 + SL_PCT)
        else:
            equity.append(balance); continue

        margin = position_size * leverage
        position = {
            "type": side, "entry": cur, "sl": sl,
            "bar": i, "margin": margin, "trailing_active": False
        }
        last_trade_bar = i
        equity.append(balance)

    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "start_balance": equity[0],
        "end_balance": round(balance, 2),
        "net_pnl": round(balance - equity[0], 2),
        "total_trades": len(trades),
        "winning_trades": wins,
        "losing_trades": len(trades) - wins,
        "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
        "max_drawdown": round(max(1 - e/max(equity[:i+1]) for i, e in enumerate(equity)), 4) * 100,
        "trades_list": trades,
    }


# ═══════════════════════════════════════════════════════════

'''
        return code

    elif strategy == "inverted_hammer":
        code = params_block + '''"""
Inverted Hammer (Обратный молот) v2 — зеркало Hammer для SELL.
Длинная верхняя тень после восходящего тренда → разворот вниз.
SL = максимум × 1.005 (0.5% буфер), TP по RR 1:2.
Тайм-аут: 30 свечей. Только SELL.
"""
import time

COOLDOWN_BARS = 5
RR_RATIO = 2.0
TIMEOUT_BARS = 30
MIN_TREND_BARS = 3
SL_BUFFER = 1.005
VOL_PERIOD = 10
VOL_THRESHOLD = 1.2

def is_inverted_hammer(open_, high, low, close):
    body = abs(close - open_)
    upper_shadow = high - max(open_, close)
    lower_shadow = min(open_, close) - low
    total_range = high - low
    if total_range == 0:
        return False
    if body == 0:
        return upper_shadow >= total_range * 0.6 and lower_shadow <= total_range * 0.1
    body_lower = min(open_, close)
    return (upper_shadow >= body * 2.0 and
            lower_shadow <= body * 0.3 and
            body_lower <= low + total_range * 0.3)

def is_uptrend(closes, i, n=MIN_TREND_BARS):
    if i < n + 1:
        return False
    return all(closes[i-j] > closes[i-j-1] for j in range(1, n+1))

def has_volume_confirmation(volumes, i, period=VOL_PERIOD):
    if i < period:
        return False
    avg_vol = sum(volumes[i-period:i]) / period
    return volumes[i] > avg_vol * VOL_THRESHOLD

def run(candles, balance, leverage, position_size):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    opens  = [c["open"]  for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    trades = []
    last_trade_bar = -COOLDOWN_BARS
    position = None
    for i in range(10, len(closes)):
        if balance <= 0: break
        cur = closes[i]
        if position:
            hit_sl = highs[i] >= position["sl"]
            hit_tp = lows[i] <= position["tp"]
            timed_out = (i - position["bar"] >= TIMEOUT_BARS)
            if hit_sl: exit_price = position["sl"]; reason = "STOP_LOSS"
            elif hit_tp: exit_price = position["tp"]; reason = "TAKE_PROFIT"
            elif timed_out: exit_price = cur; reason = "TIMEOUT"
            else: continue
            pnl = (position["entry"] - exit_price) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            trades.append({"type":"SELL","entry":round(position["entry"],4),"exit":round(exit_price,4),"pnl":round(net_pnl,4),"pnl_pct":round(pnl/(position["entry"]*position["qty"])*100,2),"reason":reason,"bars":i-position["bar"]})
            position = None
            continue
        if i - last_trade_bar < COOLDOWN_BARS: continue
        if (is_inverted_hammer(opens[i],highs[i],lows[i],closes[i]) and is_uptrend(closes,i) and has_volume_confirmation(volumes,i)):
            if i+1>=len(closes) or closes[i+1]>=lows[i]: continue
            entry_bar = i+2 if i+2<len(closes) else i+1
            entry = closes[entry_bar]
            sl = highs[i] * SL_BUFFER
            risk = sl - entry
            if risk <= 0: continue
            tp = entry - risk * RR_RATIO
            margin = position_size * leverage
            qty = margin / entry
            position = {"type":"SELL","entry":entry,"sl":sl,"tp":tp,"bar":i,"qty":qty}
            last_trade_bar = i
    if position:
        exit_price = closes[-1]
        pnl = (position["entry"] - exit_price) * position["qty"]
        commission = position["entry"] * position["qty"] * 0.0004 * 2
        net_pnl = pnl - commission
        balance += net_pnl
        trades.append({"type":"SELL","entry":round(position["entry"],4),"exit":round(exit_price,4),"pnl":round(net_pnl,4),"pnl_pct":round(pnl/(position["entry"]*position["qty"])*100,2),"reason":"END_OF_PERIOD","bars":len(closes)-position["bar"]})
    wins = sum(1 for t in trades if t["pnl"]>0)
    return {"start_balance":balance-sum(t["pnl"]for t in trades),"end_balance":round(balance,2),"net_pnl":round(sum(t["pnl"]for t in trades),2),"total_trades":len(trades),"winning_trades":wins,"losing_trades":len(trades)-wins,"win_rate":round(wins/len(trades)*100,1)if trades else 0,"trades_list":trades}



# КАК ПАРАМЕТРЫ ВЛИЯЮТ НА ТОРГОВЛЮ
# ═══════════════════════════════════════════════════════════
#
# БАЛАНС (balance) — стартовый депозит.
#   При обнулении баланса стратегия останавливается.
#
# ПЛЕЧО (leverage) — усиливает P&L каждой сделки.
#   margin = position_size × leverage.
#   Трейлинг-стоп скользит за ценой, но финальный P&L
#   умножается на плечо: pnl = margin × (движение_цены% × leverage).
#
# СУММА СДЕЛКИ (position_size) — базовая сумма в $ до плеча.
#
# ПЕРИОД (period) — дней истории для загрузки свечей.
#   Трейлинг-стоп активируется после +0.5% движения.
#   Нужно достаточно свечей, чтобы цена успела дойти до активации.
#
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Так бэктестер запускает стратегию
    # candles = fetch_candles(symbol="{PAIR}", interval="5m", days=period)
    candles = []
    report = run(
        candles=candles,
        balance=balance,           # ← стартовый депозит
        leverage=leverage,         # ← кредитное плечо
        position_size=position_size  # ← размер позиции
    )
    print(f"P&L: ${report['net_pnl']} | Сделок: {report['total_trades']} | Винрейт: {report['win_rate']}%")
'''.replace("{PAIR}", pair)
        return code

    elif strategy == "hammer":
        code = params_block + '''"""
Hammer (Молот) v2 — свечной паттерн разворота после нисходящего тренда.
Улучшения: SL 0.5% буфер, подтверждение следующей свечой, volume-фильтр, таймаут 30.
Длинная нижняя тень (≥ 2× тело), маленькая верхняя тень, тело в верхней трети.
SL = минимум молота × 0.995, TP = SL + риск × 2 (RR 1:2).
Тайм-аут: 30 свечей. Только LONG.
"""
import time

# ── Параметры паттерна ──
COOLDOWN_BARS = 5         # кулдаун между сделками
RR_RATIO = 2.0            # Risk/Reward 1:2 (консервативный)
TIMEOUT_BARS = 30         # выход по времени (увеличен для надёжности)
MIN_TREND_BARS = 3        # минимум свечей падения перед молотом
SL_BUFFER = 0.995         # стоп-лосс: 0.5% ниже минимума молота
VOL_PERIOD = 10           # период для расчёта среднего объёма
VOL_THRESHOLD = 1.2       # объём молота должен быть на 20% выше среднего

def is_hammer(open_, high, low, close):
    """Проверка: свеча — молот?"""
    body = abs(close - open_)
    lower_shadow = min(open_, close) - low
    upper_shadow = high - max(open_, close)
    total_range = high - low

    if total_range == 0:
        return False
    if body == 0:
        return lower_shadow >= total_range * 0.6 and upper_shadow <= total_range * 0.1

    body_upper = max(open_, close)
    return (lower_shadow >= body * 2.0 and
            upper_shadow <= body * 0.3 and
            body_upper >= low + total_range * 0.7)

def is_downtrend(closes, i, n=MIN_TREND_BARS):
    """Последние n свечей закрывались ниже предыдущей?"""
    if i < n + 1:
        return False
    return all(closes[i-j] < closes[i-j-1] for j in range(1, n+1))

def has_volume_confirmation(volumes, i, period=VOL_PERIOD):
    """Объём молота выше среднего за period свечей?"""
    if i < period:
        return False
    avg_vol = sum(volumes[i-period:i]) / period
    return volumes[i] > avg_vol * VOL_THRESHOLD

def run(candles, balance, leverage, position_size):
    """Прогон Hammer-стратегии на массиве свечей."""
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    opens  = [c["open"]  for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    trades = []
    last_trade_bar = -COOLDOWN_BARS
    position = None  # {type, entry, sl, tp, bar, qty}

    for i in range(10, len(closes)):
        if balance <= 0:
            break

        cur = closes[i]

        # ── Проверка открытой позиции ──
        if position:
            hit_sl = lows[i] <= position["sl"]
            hit_tp = highs[i] >= position["tp"]
            timed_out = (i - position["bar"] >= TIMEOUT_BARS)

            if hit_sl:
                exit_price = position["sl"]; reason = "STOP_LOSS"
            elif hit_tp:
                exit_price = position["tp"]; reason = "TAKE_PROFIT"
            elif timed_out:
                exit_price = cur; reason = "TIMEOUT"
            else:
                continue

            pnl = (exit_price - position["entry"]) * position["qty"]
            commission = position["entry"] * position["qty"] * 0.0004 * 2
            net_pnl = pnl - commission
            balance += net_pnl
            trades.append({
                "type": position["type"], "entry": round(position["entry"], 4),
                "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "reason": reason, "bars": i - position["bar"]
            })
            position = None
            continue

        # ── Кулдаун ──
        if i - last_trade_bar < COOLDOWN_BARS:
            continue

        # ── Поиск молота ──
        if (is_hammer(opens[i], highs[i], lows[i], closes[i])
                and is_downtrend(closes, i)
                and has_volume_confirmation(volumes, i)):
            # Подтверждение: следующая свеча должна закрыться выше максимума молота
            if i + 1 >= len(closes) or closes[i + 1] <= highs[i]:
                continue

            # Вход на открытии свечи после подтверждения
            entry_bar = i + 2 if i + 2 < len(closes) else i + 1
            entry = closes[entry_bar]
            sl = lows[i] * SL_BUFFER  # 0.5% ниже минимума молота
            risk = entry - sl
            if risk <= 0:
                continue
            tp = entry + risk * RR_RATIO  # RR 1:2

            # Размер позиции: position_size × leverage ÷ цена
            margin = position_size * leverage
            qty = margin / entry

            position = {
                "type": "BUY", "entry": entry,
                "sl": sl, "tp": tp,
                "bar": i, "qty": qty
            }
            last_trade_bar = i

    # Закрыть позицию в конце периода
    if position:
        exit_price = closes[-1]
        pnl = (exit_price - position["entry"]) * position["qty"]
        commission = position["entry"] * position["qty"] * 0.0004 * 2
        net_pnl = pnl - commission
        balance += net_pnl
        trades.append({
            "type": position["type"], "entry": round(position["entry"], 4),
            "exit": round(exit_price, 4), "pnl": round(net_pnl, 4),
            "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
            "reason": "END_OF_PERIOD", "bars": len(closes) - position["bar"]
        })

    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "start_balance": balance - sum(t["pnl"] for t in trades),
        "end_balance": round(balance, 2),
        "net_pnl": round(sum(t["pnl"] for t in trades), 2),
        "total_trades": len(trades),
        "winning_trades": wins,
        "losing_trades": len(trades) - wins,
        "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
        "trades_list": trades,
    }


# ═══════════════════════════════════════════════════════════
# КАК ПАРАМЕТРЫ ВЛИЯЮТ НА ТОРГОВЛЮ
# ═══════════════════════════════════════════════════════════
#
# БАЛАНС (balance) — стартовый депозит. При обнулении → стоп.
#
# ПЛЕЧО (leverage) — размер позиции = position_size × leverage.
#   margin = position_size × leverage → qty = margin / entry_price.
#   Чем выше плечо, тем больше контрактов покупается.
#
# СУММА СДЕЛКИ (position_size) — базовая сумма в $ до плеча.
#   $50 × 3× = $150 маржи на одну сделку.
#
# ПЕРИОД (period) — дней истории. Hammer требует МИНИМУМ
#   7-14 дней на 5m, чтобы набрать достаточно паттернов.
#   На коротких периодах сделок может не быть вообще.
#
# RR 1:2 — на каждые $1 риска цель $2 прибыли.
#   При винрейте 50% матожидание положительное:
#   E = 0.5×2 − 0.5×1 = +0.5R на сделку.
#
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Так бэктестер запускает стратегию
    # candles = fetch_candles(symbol="{PAIR}", interval="5m", days=period)
    candles = []
    report = run(
        candles=candles,
        balance=balance,           # ← стартовый депозит
        leverage=leverage,         # ← кредитное плечо
        position_size=position_size  # ← размер позиции
    )
    print(f"P&L: ${report['net_pnl']} | Сделок: {report['total_trades']} | Винрейт: {report['win_rate']}%")
'''.replace("{PAIR}", pair)
        return code

    else:
        return f"# Стратегия '{strategy}' — исходный код недоступен\\n"


def _compute_history_summary(reports):
    """Сводка по всем запускам в истории."""
    if not reports:
        return {}
    total_pnl = sum(r.get("net_pnl", 0) for r in reports)
    all_trades = sum(r.get("total_trades", 0) for r in reports)
    all_wins = sum(r.get("winning_trades", 0) for r in reports)
    all_losses = sum(r.get("losing_trades", 0) for r in reports)
    win_rate = round(all_wins / all_trades * 100, 1) if all_trades else 0
    max_dd = max((r.get("max_drawdown", 0) for r in reports), default=0)
    best_run = max((r.get("net_pnl", 0) for r in reports), default=0)
    worst_run = min((r.get("net_pnl", 0) for r in reports), default=0)
    avg_pnl_per_run = round(total_pnl / len(reports), 2)
    return {
        "total_runs": len(reports),
        "total_pnl": round(total_pnl, 2),
        "total_trades": all_trades,
        "total_wins": all_wins,
        "total_losses": all_losses,
        "win_rate": win_rate,
        "max_drawdown": round(max_dd, 1),
        "best_run_pnl": round(best_run, 2),
        "worst_run_pnl": round(worst_run, 2),
        "avg_pnl_per_run": avg_pnl_per_run,
    }


# ───────────────────────────── Fear & Greed ────────────────────────────

def get_fear_greed():
    """
    Fear & Greed Index от alternative.me (бесплатный API).
    Возвращает: value, classification, рекомендацию.
    """
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        item = data.get("data", [{}])[0]
        value = int(item.get("value", 50))
        classification = item.get("value_classification", "Neutral")
        
        # Рекомендация
        if value <= 25:
            recommendation = "🔴 Покупать"
            advice = "Экстремальный страх — historically лучшее время для входа"
            color = "#ef4444"
        elif value <= 45:
            recommendation = "🟠 Присматриваться"
            advice = "Страх — можно начинать набирать позиции"
            color = "#f59e0b"
        elif value <= 55:
            recommendation = "⚪ Ждать"
            advice = "Нейтрально — без спешки, жди сигналы"
            color = "#8888aa"
        elif value <= 75:
            recommendation = "🟢 Осторожно"
            advice = "Жадность — фиксируй прибыль, не входи в новые"
            color = "#10b981"
        else:
            recommendation = "🟢🔴 Продавать"
            advice = "Экстремальная жадность — historically лучшее время для выхода"
            color = "#10b981"
        
        return {
            "value": value,
            "classification": classification,
            "recommendation": recommendation,
            "advice": advice,
            "color": color,
        }
    except Exception as e:
        return {"error": str(e), "value": 50, "classification": "Unknown",
                "recommendation": "❓ Нет данных", "advice": "API недоступен", "color": "#8888aa"}

class Handler(BaseHTTPRequestHandler):
    def _json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str, ensure_ascii=False).encode())

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        
        if path == '/api/backtest/start':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except:
                self._json({"error": "Invalid JSON"})
                return
            
            # Validate
            required = ["strategy", "pair", "timeframe", "leverage", "balance", "period"]
            for k in required:
                if k not in body:
                    self._json({"error": f"Missing field: {k}"})
                    return
            
            # Create job
            _backtest_counter[0] += 1
            job_id = f"bt_{_backtest_counter[0]}"
            _backtests[job_id] = {
                "status": "running",
                "progress": 0,
                "candles_processed": 0,
                "message": "Запуск...",
                "report": None,
            }
            
            # Start in background thread
            t = threading.Thread(target=BacktestRunner.run, args=(job_id, body), daemon=True)
            t.start()
            
            self._json({"backtest_id": job_id, "status": "started"})
        elif path == '/api/backtest/cancel':
            bt_id = qs.get('id', [None])[0]
            if bt_id and bt_id in _backtests:
                bt = _backtests[bt_id]
                if bt.get("status") == "running":
                    bt["cancelled"] = True
                    bt["message"] = "⏹ Остановка..."
                    self._json({"status": "cancelling", "backtest_id": bt_id})
                else:
                    self._json({"error": f"Backtest not running (status: {bt.get('status')})"})
            else:
                self._json({"error": "Unknown backtest ID"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == '/api/chart-data':
            symbol = qs.get('symbol', ['TONUSDT'])[0].upper()
            interval = qs.get('interval', ['5m'])[0]
            # Проверяем, что символ есть в списке
            if symbol not in SYMBOLS.values():
                symbol = "TONUSDT"
            if interval not in INTERVALS:
                interval = "5m"

            data = get_chart_data(symbol, interval)
            data["signals"]     = get_signals()
            data["trades"]      = get_trades()
            data["stats"]       = get_stats()
            data["onchain"]     = get_onchain()
            data["sentiment"]   = get_sentiment()
            data["balance"]     = get_balance()
            data["tradesTotal"] = data["stats"]["total"]["trades"]
            data["tradesToday"] = data["stats"]["total"]["trades"]
            data["symbols"]     = list(SYMBOLS.keys())
            data["intervals"]   = list(INTERVALS.keys())
            self._json(data)

        elif path == '/api/symbols':
            self._json({"symbols": list(SYMBOLS.keys()), "intervals": list(INTERVALS.keys())})

        elif path == '/api/backtest/start':
            # Handled in do_POST
            self.send_response(405); self.end_headers()

        elif path == '/api/backtest/progress':
            bt_id = qs.get('id', [None])[0]
            if bt_id and bt_id in _backtests:
                self._json(_backtests[bt_id])
            else:
                self._json({"status": "not_found", "message": "Unknown backtest ID"})

        elif path == '/api/backtest/report':
            bt_id = qs.get('id', [None])[0]
            if bt_id and bt_id in _backtests:
                bt = _backtests[bt_id]
                report = bt.get("report", {})
                if "file" in bt:
                    report["run_file"] = bt["file"]
                if "history_file" in bt:
                    report["history_file"] = bt["history_file"]
                self._json(report)
            else:
                self._json({"error": "Unknown backtest ID"})

        elif path == '/api/backtest/history':
            # Список всех файлов истории
            out_dir = Path("/home/oleg/workspace/crypto-ton/backtests")
            files = []
            if out_dir.exists():
                for f in sorted(out_dir.glob("backtest_*.json")):
                    try:
                        data = json.loads(f.read_text())
                        if "runs" in data:
                            files.append({
                                "strategy": data.get("strategy", f.stem),
                                "pair": data.get("pair", ""),
                                "timeframe": data.get("timeframe", ""),
                                "total_runs": data.get("total_runs", 0),
                                "summary": data.get("summary", {}),
                                "updated_at": data.get("updated_at", ""),
                            })
                    except Exception:
                        pass
            self._json({"files": files})

        elif path == '/api/backtest/history-file':
            strategy = qs.get('strategy', [''])[0]
            pair = qs.get('pair', [''])[0]
            tf = qs.get('timeframe', [''])[0]
            if not all([strategy, pair, tf]):
                self._json({"error": "Missing strategy/pair/timeframe"})
                return
            fpath = Path("/home/oleg/workspace/crypto-ton/backtests") / f"backtest_{strategy}_{pair}_{tf}.json"
            if fpath.exists():
                try:
                    data = json.loads(fpath.read_text())
                    # Extract params from latest run for source code
                    latest_params = {}
                    if data.get("runs"):
                        latest = data["runs"][-1]  # last run (newest after reversal in JS)
                        latest_params = {
                            "balance": latest.get("balance"),
                            "leverage": latest.get("leverage"),
                            "position_size": latest.get("position_size"),
                            "period": latest.get("period"),
                        }
                    data["source_code"] = _get_strategy_source(strategy, latest_params, pair)
                    self._json(data)
                except Exception:
                    self._json({"error": "Failed to read history file"})
            else:
                self._json({"error": "No history for this combination", "runs": []})

        elif path == '/api/backtest/active':
            # Список активных (запущенных) бэктестов
            jobs = []
            for bt_id, bt in _backtests.items():
                if bt.get("status") == "running":
                    jobs.append({
                        "id": bt_id,
                        "status": bt.get("status"),
                        "progress": bt.get("progress", 0),
                        "message": bt.get("message", ""),
                        "candles_processed": bt.get("candles_processed", 0),
                        "live_balance": bt.get("live_balance"),
                        "live_trades": bt.get("live_trades"),
                        "live_win_rate": bt.get("live_win_rate"),
                        "params": bt.get("params", {}),
                    })
            self._json({"jobs": jobs})

        elif path == '/api/fear-greed':
            self._json(get_fear_greed())

        elif path == '/' or path == '/dashboard':
            html = Path("/home/oleg/workspace/crypto-ton/dashboard.html").read_text()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode())

        elif path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8889
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"📊 Dashboard API: http://localhost:{port}")
    print(f"   График: http://localhost:{port}/dashboard")
    print(f"   JSON:   http://localhost:{port}/api/chart-data?symbol=TONUSDT&interval=5m")
    server.serve_forever()
