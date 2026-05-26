"""
Общие утилиты для бэктестов: RSI, свечи, позиции, отчёты.
Используется и историческим, и live-раннером.
"""
import json, urllib.request, sys, sqlite3, os
from pathlib import Path
from datetime import datetime
from typing import Optional

# === Конфигурация стратегий ===
STRATEGY_CONFIG = {
    "rsi": {
        "name": "RSI-стратегия (signal_v6)",
        "sl_pct": 0.012, "tp_pct": 0.024,
        "cooldown_bars": 15, "rsi_buy": 30, "rsi_sell": 75,
        "trailing": False, "trail_activate": None,
    },
    "swing": {
        "name": "SWING-стратегия",
        "sl_pct": 0.025, "tp_pct": 0.05,
        "cooldown_bars": 60, "rsi_buy": 30, "rsi_sell": 75,
        "trailing": False, "trail_activate": None,
    },
    "rsi_trailing": {
        "name": "RSI Трейлинг-стоп",
        "sl_pct": 0.012, "tp_pct": None,  # TP не используется — только трейлинг
        "cooldown_bars": 15, "rsi_buy": 30, "rsi_sell": 75,
        "trailing": True, "trail_activate": 0.005,
    },
    "hammer": {
        "name": "Hammer v2 (Молот)",
        "sl_pct": 0.005, "tp_pct": None,
        "cooldown_bars": 5, "rsi_buy": None, "rsi_sell": None,
        "trailing": False, "trail_activate": None,
        "type": "candle",
    },
    "inverted_hammer": {
        "name": "Inverted Hammer v2 (Обратный молот)",
        "sl_pct": 0.005, "tp_pct": None,
        "cooldown_bars": 5, "rsi_buy": None, "rsi_sell": None,
        "trailing": False, "trail_activate": None,
        "type": "candle",
    },
    "bullish_engulfing": {
        "name": "Bullish Engulfing (Бычье поглощение)",
        "sl_pct": 0.005, "tp_pct": None,
        "cooldown_bars": 5, "rsi_buy": None, "rsi_sell": None,
        "trailing": False, "trail_activate": None,
        "type": "candle",
    },
    "morning_star": {
        "name": "Morning Star (Утренняя звезда)",
        "sl_pct": 0.005, "tp_pct": None,
        "cooldown_bars": 5, "rsi_buy": None, "rsi_sell": None,
        "trailing": False, "trail_activate": None,
        "type": "candle",
    },
    "piercing_line": {
        "name": "Piercing Line (Пронизывающая линия)",
        "sl_pct": 0.005, "tp_pct": None,
        "cooldown_bars": 5, "rsi_buy": None, "rsi_sell": None,
        "trailing": False, "trail_activate": None,
        "type": "candle",
    },
}

# === RSI ===
def calc_rsi(closes):
    """Calculate RSI-14 for the last candle in the list."""
    if len(closes) < 15:
        return 50
    window = closes[-14:]
    gains = sum(max(window[j] - window[j-1], 0) for j in range(1, len(window)))
    losses = sum(max(window[j-1] - window[j], 0) for j in range(1, len(window)))
    return 100 - (100 / (1 + gains / losses)) if losses > 0 else 100


# === Свечи ===
def fetch_binance_klines(symbol: str, interval: str, limit: int = 200) -> Optional[list]:
    """Тянет свечи с Binance public API. Бесплатно, без ключа."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        candles = []
        for k in data:
            candles.append({
                "time": int(k[0]) // 1000,
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })
        return candles
    except Exception as e:
        print(f"[Binance] {symbol} {interval}: {e}", file=sys.stderr)
        return None


def fetch_latest_candle(symbol: str, interval: str) -> Optional[dict]:
    """Fetch only the most recent (still-forming) candle via REST API."""
    candles = fetch_binance_klines(symbol, interval, limit=2)
    if candles and len(candles) >= 2:
        return candles[-1]  # last = still open
    return None


# === Position management (shared between modes) ===
def open_position(pos_type, entry_price, balance, leverage, position_size):
    """Create a position dict. Returns (position, margin_used)."""
    margin = position_size / leverage
    qty = position_size / entry_price
    cfg = None  # determined by strategy later
    pos = {
        "type": pos_type, "entry": entry_price,
        "qty": qty, "margin": margin, "best": entry_price,
    }
    return pos


def check_position_exit(position, candle, strategy_cfg, trailing=False):
    """
    Check if position should be closed on this candle.
    Returns (exit_price, reason) or (None, None) if no exit.
    """
    cur = candle["close"]
    high = candle["high"]
    low = candle["low"]
    sl_pct = strategy_cfg["sl_pct"]
    tp_pct = strategy_cfg["tp_pct"]

    pos_type = position["type"]

    if pos_type == "BUY":
        # Trailing stop update
        if trailing and cur > position["best"]:
            position["best"] = cur
        if trailing and position["best"] >= position["entry"] * (1 + strategy_cfg["trail_activate"]):
            position["sl"] = round(position["best"] * (1 - sl_pct), 4)

        # Check SL
        if low <= position.get("sl", position["entry"] * (1 - sl_pct)):
            return position.get("sl", position["entry"] * (1 - sl_pct)), "STOP_LOSS" if not trailing else "TRAILING_STOP"
        # Check TP (only for non-trailing)
        if not trailing and tp_pct and high >= position["entry"] * (1 + tp_pct):
            return position["entry"] * (1 + tp_pct), "TAKE_PROFIT"
        return None, None

    else:  # SELL
        if trailing and cur < position["best"]:
            position["best"] = cur
        if trailing and position["best"] <= position["entry"] * (1 - strategy_cfg["trail_activate"]):
            position["sl"] = round(position["best"] * (1 + sl_pct), 4)

        if high >= position.get("sl", position["entry"] * (1 + sl_pct)):
            return position.get("sl", position["entry"] * (1 + sl_pct)), "STOP_LOSS" if not trailing else "TRAILING_STOP"
        if not trailing and tp_pct and low <= position["entry"] * (1 - tp_pct):
            return position["entry"] * (1 - tp_pct), "TAKE_PROFIT"
        return None, None


def calculate_pnl(position, exit_price):
    """Calculate net PnL for a closed position."""
    entry = position["entry"]
    qty = position["qty"]
    if position["type"] == "BUY":
        gross = (exit_price - entry) * qty
    else:
        gross = (entry - exit_price) * qty
    commission = entry * qty * 0.0004 * 2  # 0.04% × 2 (open+close)
    return gross - commission


# === Отчёт ===
def build_report(trades, end_balance, params):
    """Build final report dict (same format as BacktestRunner._build_report)."""
    start_balance = float(params["balance"])
    winning = [t for t in trades if t["pnl"] > 0]
    losing = [t for t in trades if t["pnl"] < 0]
    total_pnl = sum(t["pnl"] for t in trades)

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
        "trades_list": trades[-10:],
    }


# === Сохранение ===
def save_report(job_id, params, report):
    """Save backtest report to 2 files (individual + history)."""
    out_dir = Path("/home/oleg/workspace/crypto-ton/backtests")
    os.makedirs(out_dir, exist_ok=True)

    strategy = params.get('strategy', '?')
    pair = params.get('pair', '?')
    tf = params.get('timeframe', '?')
    ts = datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S")

    # File 1: individual run
    run_file = out_dir / f"backtest_{strategy}_{pair}_{ts_str}.json"
    run_data = {
        "job_id": job_id,
        "timestamp": ts.isoformat(),
        "params": {
            "strategy": strategy, "pair": pair, "timeframe": tf,
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

    # File 2: cumulative history
    hist_file = out_dir / f"backtest_{strategy}_{pair}_{tf}.json"
    try:
        if hist_file.exists():
            hist = json.loads(hist_file.read_text())
        else:
            hist = {"strategy": strategy, "pair": pair, "timeframe": tf, "runs": []}
        hist["runs"].append({
            "timestamp": ts.isoformat(),
            "job_id": job_id,
            "period_days": int(params.get("period", 1)),
            "net_pnl": report.get("net_pnl"),
            "win_rate": report.get("win_rate"),
            "total_trades": report.get("total_trades"),
            "end_balance": report.get("end_balance"),
        })
        # Keep last 50 runs
        hist["runs"] = hist["runs"][-50:]
        hist_file.write_text(json.dumps(hist, default=str, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[Backtest] Failed to save history file: {e}", file=sys.stderr)

    return run_file, hist_file
