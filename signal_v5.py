#!/usr/bin/env python3
"""
ОБУЧАЮЩИЙСЯ СИГНАЛЬЩИК v5 — ФЬЮЧЕРСЫ TON/USDT x3
Добавлено: трекинг сделок, анализ ошибок, автотюнинг параметров.

Архитектура обучения:
  1. Каждый сигнал → сохраняется в signals.db с полным контекстом
  2. Каждые 5 мин → проверка открытых «виртуальных позиций»
  3. Закрытая сделка → запись в trades.db с P&L и причиной выхода
  4. 3 убытка подряд → запуск анализа (уже сегодня вечером)
  5. Анализ → предложение правок в Telegram
  6. Пользователь одобряет → параметры обновляются

Запуск: cron каждые 5 мин
"""

import sqlite3, math, os, sys, urllib.request, urllib.parse, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Импортируем trading_tg утилиту
sys.path.insert(0, str(Path(__file__).parent))
try:
    from trading_tg import send_trading_tg as send_tg
except ImportError:
    def send_tg(text): pass

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
TRADES_DB = Path("/home/oleg/workspace/crypto-ton/trades.db")
ONCHAIN_DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
LEARNING_DB = Path("/home/oleg/workspace/crypto-ton/learning.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# ═══════════════════════════════════════════
#  БАЗОВЫЕ ПАРАМЕТРЫ (могут меняться обучением)
# ═══════════════════════════════════════════
LEVERAGE = 3
INITIAL_BALANCE = 100.0
BASE_MARGIN = 5.20

# Эти параметры могут корректироваться автотюнером
PARAMS = {
    "buy_rsi": 35,          # RSI ниже → BUY
    "sell_rsi": 70,         # RSI выше → SELL
    "sl_price_pct": 0.008,  # стоп-лосс % цены
    "tp_price_pct": 0.016,  # тейк-профит % цены
    "timeout_minutes": 180, # тайм-аут
    "trend_filter": True,   # не входить против тренда
    "nvt_filter": True,     # ончейн NVT фильтр
    "btc_filter": True,     # корреляция BTC
    "min_volume_ratio": 0.5, # мин отношение объёма к среднему
}
PARAMS_LOCKED = False  # блокировка после 3 автотюнов подряд

MIN_ROWS = 100


# ═══════════════════════════════════════════
#  БАЗЫ ДАННЫХ
# ═══════════════════════════════════════════

def init_dbs():
    conn = sqlite3.connect(str(SIGNALS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS signals_v5 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, signal TEXT, strength TEXT, price REAL, 
        sl REAL, tp REAL, rsi REAL, trend TEXT, macd REAL,
        buy_rsi_used REAL, nvt REAL, btc_corr REAL, 
        bpm REAL, fdv_mcap REAL, onchain_flags TEXT,
        trade_id INTEGER  -- ссылка на сделку после закрытия
    )""")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(TRADES_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS trades_v5 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER, entry_ts TEXT, exit_ts TEXT,
        type TEXT, entry_price REAL, exit_price REAL,
        pnl REAL, pnl_pct REAL, exit_reason TEXT,
        bars_held INTEGER,
        context_json TEXT  -- полный контекст для анализа
    )""")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(LEARNING_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS learning_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, trigger TEXT, analysis TEXT, 
        suggested_changes TEXT, applied BOOLEAN DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS active_params (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, param_name TEXT, old_value TEXT, new_value TEXT
    )""")
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════
#  ИНДИКАТОРЫ (без изменений)
# ═══════════════════════════════════════════

def sma(data, p):
    return sum(data[-p:]) / p if len(data) >= p else None

def rsi(data, per=14):
    if len(data) < per+1: return None
    gains = losses = 0.0
    for i in range(1, per+1):
        d = data[i]-data[i-1]; gains += d if d>=0 else 0; losses += abs(d) if d<0 else 0
    avg_g, avg_l = gains/per, losses/per
    if avg_l == 0: return 100.0
    for i in range(per+1, len(data)):
        d = data[i]-data[i-1]
        avg_g = (avg_g*(per-1)+(d if d>=0 else 0))/per; avg_l = (avg_l*(per-1)+(abs(d) if d<0 else 0))/per
    if avg_l == 0: return 100.0
    return 100 - (100/(1+avg_g/avg_l))

def ema(data, per):
    if len(data) < per: return None
    k = 2/(per+1); res = sum(data[:per])/per
    for p in data[per:]: res = p*k + res*(1-k)
    return res

def macd_line(data):
    e12, e26 = ema(data, 12), ema(data, 26)
    return e12 - e26 if e12 and e26 else None


# ═══════════════════════════════════════════
#  БАЛАНС И СТАТИСТИКА
# ═══════════════════════════════════════════

def get_current_balance() -> float:
    conn = sqlite3.connect(str(TRADES_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS trades_v5 (id INTEGER PRIMARY KEY AUTOINCREMENT, pnl REAL)")
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades_v5").fetchone()[0]
    conn.close()
    return INITIAL_BALANCE + total_pnl

def get_margin() -> float:
    balance = get_current_balance()
    extra = max(0, (balance - INITIAL_BALANCE) // 10)
    return BASE_MARGIN * (1 + extra * 0.05)

def get_consecutive_losses() -> int:
    conn = sqlite3.connect(str(TRADES_DB))
    rows = conn.execute("SELECT pnl FROM trades_v5 WHERE exit_reason != 'OPEN' AND pnl IS NOT NULL ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    count = 0
    for (pnl,) in rows:
        if pnl < 0: count += 1
        else: break
    return count

def get_recent_performance() -> Dict:
    """Последние N сделок: винрейт, средний P&L, причины выхода."""
    conn = sqlite3.connect(str(TRADES_DB))
    rows = conn.execute(
        "SELECT type, pnl, pnl_pct, exit_reason, bars_held FROM trades_v5 "
        "WHERE exit_reason != 'OPEN' AND pnl IS NOT NULL "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    
    if not rows:
        return {"trades": 0}
    
    wins = [r for r in rows if r[1] > 0]
    losses = [r for r in rows if r[1] <= 0]
    
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins)/len(rows)*100, 1),
        "avg_win": round(sum(r[1] for r in wins)/len(wins), 2) if wins else 0,
        "avg_loss": round(sum(r[1] for r in losses)/len(losses), 2) if losses else 0,
        "total_pnl": round(sum(r[1] for r in rows), 2),
        "exit_reasons": {r[3]: sum(1 for rr in rows if rr[3]==r[3]) for r in rows},
    }


# ═══════════════════════════════════════════
#  ОНЧЕЙН-МЕТРИКИ
# ═══════════════════════════════════════════

def get_onchain_metrics() -> Dict:
    try:
        conn = sqlite3.connect(str(ONCHAIN_DB))
        nvt_r = conn.execute("SELECT nvt_ratio FROM onchain WHERE nvt_ratio IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        corr_r = conn.execute("SELECT ton_btc_correlation FROM onchain WHERE ton_btc_correlation IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        bpm_r = conn.execute("SELECT blocks_per_minute FROM onchain WHERE blocks_per_minute IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        fdv_r = conn.execute("SELECT fdv_to_mcap FROM onchain WHERE fdv_to_mcap IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return {
            "nvt": nvt_r[0] if nvt_r else None,
            "btc_corr": corr_r[0] if corr_r else None,
            "bpm": bpm_r[0] if bpm_r else None,
            "fdv_mcap": fdv_r[0] if fdv_r else None,
        }
    except:
        return {}


# ═══════════════════════════════════════════
#  СИГНАЛ (С ФИЛЬТРАМИ)
# ═══════════════════════════════════════════

def signal(data_prices, data_volumes) -> Optional[Dict]:
    if len(data_prices) < 100: return None
    
    cur = data_prices[-1]
    s7, s20 = sma(data_prices, 7), sma(data_prices, 20)
    r = rsi(data_prices, 14)
    macd = macd_line(data_prices)
    
    avg_v = sum(data_volumes[:-1])/len(data_volumes[:-1]) if len(data_volumes)>1 else data_volumes[-1]
    vr = data_volumes[-1]/avg_v if avg_v>0 else 1.0
    
    # Bollinger
    bb_lo = bb_hi = None
    if s20:
        recent = data_prices[-20:]
        var = sum((p-s20)**2 for p in recent)/20
        std = math.sqrt(var)
        bb_lo, bb_hi = s20 - 2*std, s20 + 2*std
    
    # Тренд
    trend_score = 0
    if s7 and s20: trend_score += 1 if s7 > s20 else 0
    if macd: trend_score += 1 if macd > 0 else 0
    trend = {2: "↑ бычий", 0: "↓ медвежий"}.get(trend_score, "↔ боковик")
    
    # Ончейн
    onchain = get_onchain_metrics()
    nvt = onchain.get("nvt")
    btc_corr = onchain.get("btc_corr")
    bpm = onchain.get("bpm")
    fdv_mcap = onchain.get("fdv_mcap")
    
    # Фильтры
    effective_buy_rsi = PARAMS["buy_rsi"]
    onchain_flags = []
    onchain_weaken = False
    
    if PARAMS["nvt_filter"] and nvt is not None:
        if nvt < 15:
            effective_buy_rsi = min(38, PARAMS["buy_rsi"] + 3)
            onchain_flags.append(f"NVT={nvt:.1f}<15→усиление")
        elif nvt > 100:
            onchain_flags.append(f"NVT={nvt:.1f}>100→блок")
    
    if PARAMS["btc_filter"] and btc_corr and btc_corr > 0.7:
        onchain_flags.append(f"BTC_corr={btc_corr:.2f}>0.7→фильтр")
    
    if PARAMS["fdv_filter"] if "fdv_filter" in PARAMS else True:
        if fdv_mcap and fdv_mcap > 2.5:
            onchain_flags.append(f"FDV/MCap={fdv_mcap:.2f}>2.5→блок")
    
    if bpm and onchain.get("prev_bpm"):
        if bpm < onchain.get("prev_bpm", 0) * 0.95:
            onchain_weaken = True
            onchain_flags.append("сеть↓→ослабление")
    
    # Фильтр объёма
    if vr < PARAMS.get("min_volume_ratio", 0.5):
        return None  # низкий объём — нет сигнала
    
    # Блокировки
    blocked = any("→блок" in f for f in onchain_flags)
    
    # SL/TP — для SELL инвертированы
    balance = get_current_balance()
    margin = get_margin()
    position = margin * LEVERAGE
    tons = position / cur
    
    # BUY: SL ниже, TP выше. SELL: SL выше, TP ниже.
    # Сначала считаем для BUY (значения по умолчанию)
    buy_sl = round(cur * (1 - PARAMS["sl_price_pct"]), 4)   # ниже входа
    buy_tp = round(cur * (1 + PARAMS["tp_price_pct"]), 4)   # выше входа
    
    sl_move_pct = PARAMS["sl_price_pct"] * LEVERAGE
    tp_move_pct = PARAMS["tp_price_pct"] * LEVERAGE
    loss_d = round(margin * sl_move_pct, 2)
    profit_d = round(margin * tp_move_pct, 2)
    commission = round(position * 0.0004, 4)
    
    trade_info = {
        "balance": round(balance, 0), "margin": round(margin, 2),
        "position": round(position, 2), "tons": round(tons, 2),
        "leverage": LEVERAGE,
        # Значения для BUY (будут переопределены для SELL ниже)
        "stop_loss": buy_sl, "sl_pct": round(-PARAMS["sl_price_pct"]*100, 2),
        "take_profit": buy_tp, "tp_pct": round(PARAMS["tp_price_pct"]*100, 2),
        "profit": round(profit_d - commission, 2), "loss": round(loss_d + commission, 2),
        "commission": commission, "risk_balance_pct": round((loss_d+commission)/balance*100, 1),
        "risk_reward": 2.0,
    }
    
    result = None
    
    # BUY
    if r and r < effective_buy_rsi and not blocked and trend != "↓ медвежий":
        strength = "WEAK" if onchain_weaken else ("STRONG" if r < 30 and trend == "↑ бычий" else "WEAK")
        result = {
            "signal": "BUY", "strength": strength, "price": round(cur, 4),
            "trade": trade_info, "rsi": round(r, 1), "trend": trend,
            "macd": round(macd, 6) if macd else None,
            "onchain_flags": onchain_flags, "nvt": nvt, "btc_corr": btc_corr,
            "bpm": bpm, "fdv_mcap": fdv_mcap,
        }
    
    # SELL
    if r and r > PARAMS["sell_rsi"]:
        strength = "WEAK" if onchain_weaken else ("STRONG" if r > 75 else "WEAK")
        # Для SELL: SL ВЫШЕ входа, TP НИЖЕ входа
        sell_sl = round(cur * (1 + PARAMS["sl_price_pct"]), 4)
        sell_tp = round(cur * (1 - PARAMS["tp_price_pct"]), 4)
        sell_trade = dict(trade_info)
        sell_trade["stop_loss"] = sell_sl
        sell_trade["sl_pct"] = round(PARAMS["sl_price_pct"]*100, 2)
        sell_trade["take_profit"] = sell_tp
        sell_trade["tp_pct"] = round(-PARAMS["tp_price_pct"]*100, 2)
        result = {
            "signal": "SELL", "strength": strength, "price": round(cur, 4),
            "trade": sell_trade, "rsi": round(r, 1), "trend": trend,
            "macd": round(macd, 6) if macd else None,
            "onchain_flags": onchain_flags, "nvt": nvt, "btc_corr": btc_corr,
            "bpm": bpm, "fdv_mcap": fdv_mcap,
        }
    
    return result


# ═══════════════════════════════════════════
#  ТРЕКИНГ СДЕЛОК
# ═══════════════════════════════════════════

def get_open_position() -> Optional[Dict]:
    """Есть ли открытая виртуальная позиция."""
    conn = sqlite3.connect(str(TRADES_DB))
    row = conn.execute(
        "SELECT * FROM trades_v5 WHERE exit_reason = 'OPEN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(trades_v5)").fetchall()]
        conn.close()
        return dict(zip(cols, row))
    conn.close()
    return None


def open_position(sig_id: int, sig: Dict):
    """Открыть виртуальную позицию."""
    conn = sqlite3.connect(str(TRADES_DB))
    context = json.dumps({
        "rsi": sig["rsi"], "trend": sig["trend"], "macd": sig.get("macd"),
        "nvt": sig.get("nvt"), "btc_corr": sig.get("btc_corr"),
        "bpm": sig.get("bpm"), "fdv_mcap": sig.get("fdv_mcap"),
        "onchain_flags": sig.get("onchain_flags", []),
        "params": PARAMS.copy(),
    })
    conn.execute("""
        INSERT INTO trades_v5 (signal_id, entry_ts, type, entry_price, exit_price, exit_reason, bars_held, context_json)
        VALUES (?, ?, ?, ?, ?, 'OPEN', 0, ?)
    """, (sig_id, datetime.now(UTC_PLUS_3).isoformat(), sig["signal"], sig["price"], 0, context))
    conn.commit()
    conn.close()


def check_open_position(current_price: float, bar_count: int = 5):
    """Проверить, сработал ли SL/TP у открытой позиции."""
    pos = get_open_position()
    if not pos:
        return
    
    sl = pos.get("sl") or (pos["entry_price"] * (1 - PARAMS["sl_price_pct"]) if pos["type"] == "BUY" 
                           else pos["entry_price"] * (1 + PARAMS["sl_price_pct"]))
    tp = pos.get("tp") or (pos["entry_price"] * (1 + PARAMS["tp_price_pct"]) if pos["type"] == "BUY"
                           else pos["entry_price"] * (1 - PARAMS["tp_price_pct"]))
    
    # Упрощённо: проверяем текущую цену (для точного трекинга нужен стриминг)
    exit_p = None
    reason = ""
    
    if pos["type"] == "BUY":
        if current_price <= sl:
            exit_p = sl; reason = "STOP_LOSS"
        elif current_price >= tp:
            exit_p = tp; reason = "TAKE_PROFIT"
        elif pos["bars_held"] >= PARAMS["timeout_minutes"]:
            exit_p = current_price; reason = "TIMEOUT"
    else:  # SELL
        if current_price >= sl:
            exit_p = sl; reason = "STOP_LOSS"
        elif current_price <= tp:
            exit_p = tp; reason = "TAKE_PROFIT"
        elif pos["bars_held"] >= PARAMS["timeout_minutes"]:
            exit_p = current_price; reason = "TIMEOUT"
    
    if exit_p:
        close_position(pos["id"], exit_p, reason, pos["bars_held"] + bar_count)


def close_position(trade_id: int, exit_price: float, reason: str, bars_held: int):
    conn = sqlite3.connect(str(TRADES_DB))
    row = conn.execute("SELECT entry_price, type FROM trades_v5 WHERE id=?", (trade_id,)).fetchone()
    if not row:
        conn.close(); return
    
    entry, tp = row
    pnl_pct = (exit_price - entry) / entry * 100
    if tp == "SELL":
        pnl_pct = -pnl_pct
    
    margin = get_margin()
    tons = margin * LEVERAGE / entry
    pnl_dollars = tons * pnl_pct / 100 * LEVERAGE * exit_price
    pnl_dollars -= margin * LEVERAGE * 0.0004  # комиссия
    
    conn.execute("""
        UPDATE trades_v5 SET exit_price=?, exit_reason=?, bars_held=?, pnl=?, pnl_pct=?, exit_ts=?
        WHERE id=?
    """, (exit_price, reason, bars_held, round(pnl_dollars, 4), round(pnl_pct, 4), 
          datetime.now(UTC_PLUS_3).isoformat(), trade_id))
    conn.commit()
    conn.close()
    
    # Проверить 3 убытка подряд
    if get_consecutive_losses() >= 3:
        trigger_learning()


# ═══════════════════════════════════════════
#  САМООБУЧЕНИЕ
# ═══════════════════════════════════════════

def trigger_learning():
    """Анализирует 3 последних убытка и предлагает правки."""
    if PARAMS_LOCKED:
        return  # блокировка после 3 автотюнов
    
    conn = sqlite3.connect(str(TRADES_DB))
    last_3 = conn.execute(
        "SELECT id, type, entry_price, exit_price, pnl, pnl_pct, exit_reason, bars_held, context_json "
        "FROM trades_v5 ORDER BY id DESC LIMIT 3"
    ).fetchall()
    conn.close()
    
    if len(last_3) < 3:
        return
    
    # Парсим контексты
    contexts = []
    for row in last_3:
        try:
            ctx = json.loads(row[8]) if row[8] else {}
        except:
            ctx = {}
        contexts.append({
            "type": row[1], "pnl": row[4], "exit_reason": row[6],
            "bars": row[7], **ctx,
        })
    
    # Анализ
    analysis = []
    suggestions = []
    
    # Паттерн 1: Все против тренда?
    against_trend = sum(1 for c in contexts if c.get("trend", "").startswith("↓") and c["type"] == "BUY")
    if against_trend >= 2:
        analysis.append(f"{against_trend}/3 сделок BUY против медвежьего тренда")
        suggestions.append("buy_rsi:35→30 (покупать только при более глубокой перепроданности)")
    
    # Паттерн 2: Все по тайм-ауту?
    timeouts = sum(1 for c in contexts if c.get("exit_reason") == "TIMEOUT")
    if timeouts >= 2:
        analysis.append(f"{timeouts}/3 сделок закрыты по тайм-ауту (цена не дошла до SL/TP)")
        suggestions.append("sl_price_pct:0.008→0.006 (уменьшить SL, чтобы чаще срабатывал)")
        suggestions.append("tp_price_pct:0.016→0.012 (уменьшить TP для более вероятного достижения)")
    
    # Паттерн 3: Слабый RSI?
    avg_rsi = sum(c.get("rsi", 50) for c in contexts) / len(contexts)
    if avg_rsi > 65 and sum(1 for c in contexts if c["type"] == "SELL") >= 2:
        analysis.append(f"Средний RSI={avg_rsi:.0f} при SELL — недостаточно перекуплен")
        suggestions.append("sell_rsi:70→75 (продавать только при сильной перекупленности)")
    
    # Паттерн 4: NVT?
    nvt_vals = [c.get("nvt") for c in contexts if c.get("nvt")]
    if nvt_vals and sum(nvt_vals) / len(nvt_vals) < 15:
        analysis.append("NVT < 15 во всех сделках — сеть недооценена, но цена падала (дивергенция)")
        suggestions.append("nvt_filter: усиливать только при NVT<10 (более строгий порог)")
    
    if not suggestions:
        analysis.append("Не выявлено чёткого паттерна — продолжаем наблюдение")
        suggestions.append("no_change: ждём ещё 3 сделки для анализа")
    
    # Сохраняем
    conn = sqlite3.connect(str(LEARNING_DB))
    conn.execute("""
        INSERT INTO learning_events (ts, trigger, analysis, suggested_changes)
        VALUES (?, '3_consecutive_losses', ?, ?)
    """, (datetime.now(UTC_PLUS_3).isoformat(), json.dumps(analysis), json.dumps(suggestions)))
    conn.commit()
    conn.close()
    
    # Отправляем в Telegram
    send_learning_report(analysis, suggestions)


def send_learning_report(analysis: List[str], suggestions: List[str]):
    trading_token = os.getenv("TRADING_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TRADING_CHAT_ID", "").strip()
    
    if not trading_token or not chat_id:
        trading_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    
    if not trading_token or not chat_id:
        return
    
    perf = get_recent_performance()
    
    lines = [
        "🧠 <b>САМООБУЧЕНИЕ: 3 убытка подряд</b>",
        "",
        f"Статистика: {perf.get('trades',0)} сделок | Винрейт {perf.get('win_rate','?')}% | P&amp;L {perf.get('total_pnl','?'):+.2f}$",
        "",
        "📋 <b>Анализ паттернов:</b>",
    ]
    for a in analysis:
        lines.append(f"  • {a}")
    
    lines.append("")
    lines.append("💡 <b>Предлагаемые изменения:</b>")
    for s in suggestions:
        if s.startswith("no_change"):
            lines.append(f"  • {s.replace('no_change: ','')}")
        else:
            lines.append(f"  • {s}")
    
    lines.append("")
    lines.append("⚙️ Применить изменения? Отправь <b>/apply_learning</b>")
    
    msg = "\n".join(lines)
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=10)
    except Exception as e:
        print(f"  Telegram error: {e}", file=sys.stderr)


def apply_learning():
    """Применить последние предложенные изменения."""
    conn = sqlite3.connect(str(LEARNING_DB))
    row = conn.execute(
        "SELECT suggested_changes FROM learning_events WHERE applied=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    
    if not row:
        conn.close()
        return "Нет неприменённых изменений"
    
    suggestions = json.loads(row[0])
    applied = []
    
    for s in suggestions:
        if s.startswith("no_change:"):
            continue
        # Формат: "param_name:old→new описание"
        parts = s.split(" ", 1)
        param_change = parts[0]
        if ":" not in param_change:
            continue
        
        param, change = param_change.split(":", 1)
        if "→" not in change:
            continue
        
        old_val, new_val = change.split("→")
        
        if param in PARAMS:
            old_v = PARAMS[param]
            # Преобразуем тип
            if isinstance(old_v, float):
                new_v = float(new_val)
            elif isinstance(old_v, int):
                new_v = int(float(new_val))
            elif isinstance(old_v, bool):
                new_v = new_val.lower() in ("true", "1", "yes")
            else:
                new_v = new_val
            
            PARAMS[param] = new_v
            
            # Логируем
            conn2 = sqlite3.connect(str(LEARNING_DB))
            conn2.execute("""
                INSERT INTO active_params (ts, param_name, old_value, new_value)
                VALUES (?,?,?,?)
            """, (datetime.now(UTC_PLUS_3).isoformat(), param, str(old_v), str(new_v)))
            conn2.commit()
            conn2.close()
            
            applied.append(f"{param}: {old_v} → {new_v}")
    
    # Помечаем применённым
    conn.execute("UPDATE learning_events SET applied=1 WHERE id=?", 
                 (conn.execute("SELECT id FROM learning_events WHERE applied=0 ORDER BY id DESC LIMIT 1").fetchone()[0],))
    conn.commit()
    conn.close()
    
    return "\n".join(applied)


# ═══════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════

def send_tg(text):
    trading_token = os.getenv("TRADING_BOT_TOKEN","").strip()
    chat_id = os.getenv("TRADING_CHAT_ID","").strip()
    
    if not trading_token or not chat_id:
        trading_token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
        chat_id = os.getenv("TELEGRAM_HOME_CHANNEL","").strip()
    
    if not trading_token or not chat_id: return
    try:
        d = urllib.parse.urlencode({"chat_id":chat_id,"text":text,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{trading_token}/sendMessage",data=d),timeout=10)
    except: pass


def format_signal(sig):
    em = "🟢" if sig["signal"]=="BUY" else "🔴"
    bar = "▓▓▓▓" if sig["strength"]=="STRONG" else "▓▓░░"
    t = sig["trade"]
    
    sl_str = (
        f"🛑 <b>Стоп-лосс: ${t['stop_loss']:.4f}</b> ({t['sl_pct']:.1f}%)\n"
        f"   → убыток: <b>-${t['loss']:.2f}</b> ({t['risk_balance_pct']:.1f}% баланса)"
    )
    tp_str = (
        f"🎯 <b>Тейк-профит: ${t['take_profit']:.4f}</b> (+{t['tp_pct']:.1f}%)\n"
        f"   → прибыль: <b>+${t['profit']:.2f}</b>"
    )
    
    perf = get_recent_performance()
    stats = f"📊 Сделок: {perf.get('trades',0)}"
    if perf.get('win_rate'):
        stats += f" | Винрейт: {perf['win_rate']}%"
    if perf.get('total_pnl'):
        stats += f" | P&amp;L: {perf['total_pnl']:+.2f}$"
    
    onchain_line = ""
    if sig.get("onchain_flags"):
        onchain_line = "🌐 Ончейн: " + ", ".join(sig["onchain_flags"][:3]) + "\n"
    
    loss_count = get_consecutive_losses()
    warn = ""
    if loss_count == 2:
        warn = "⚠️ 2 убытка подряд — следующий будет триггером обучения!\n"
    elif loss_count >= 3:
        warn = "🧠 3 убытка подряд — ЗАПУЩЕН АНАЛИЗ ОБУЧЕНИЯ!\n"
    
    return (
        f"{em} <b>{sig['signal']} TON/USDT x{LEVERAGE}</b> {bar} {sig['strength']}\n"
        f"\n💰 <b>Вход: ${sig['price']:.4f}</b>\n"
        f"💵 Баланс: <b>${t['balance']:.0f}</b> | Маржа: <b>${t['margin']:.2f}</b>\n"
        f"📐 Позиция: <b>${t['position']:.2f}</b> = {t['tons']} TON ×{LEVERAGE}\n"
        f"{sl_str}\n{tp_str}\n"
        f"⚖️ Риск/Прибыль: <b>1:2</b> | Комиссия: ${t['commission']:.4f}\n"
        f"\n📈 Тренд: {sig['trend']} | RSI={sig['rsi']} | MACD={sig.get('macd','?')}\n"
        f"{onchain_line}"
        f"\n{stats}\n{warn}"
        f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )


# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════

def check_position_with_price(pos: Dict, price: float):
    """Проверить позицию по конкретной цене (для бэктест-проверки)."""
    context = json.loads(pos.get("context_json", "{}")) if pos.get("context_json") else {}
    params = context.get("params", PARAMS)
    
    sl_pct = params.get("sl_price_pct", 0.008)
    tp_pct = params.get("tp_price_pct", 0.016)
    
    if pos["type"] == "BUY":
        sl = pos["entry_price"] * (1 - sl_pct)
        tp = pos["entry_price"] * (1 + tp_pct)
    else:
        sl = pos["entry_price"] * (1 + sl_pct)
        tp = pos["entry_price"] * (1 - tp_pct)
    
    if pos["type"] == "BUY":
        if price <= sl:
            close_position(pos["id"], sl, "STOP_LOSS", pos.get("bars_held", 0) + 1)
        elif price >= tp:
            close_position(pos["id"], tp, "TAKE_PROFIT", pos.get("bars_held", 0) + 1)
    else:
        if price >= sl:
            close_position(pos["id"], sl, "STOP_LOSS", pos.get("bars_held", 0) + 1)
        elif price <= tp:
            close_position(pos["id"], tp, "TAKE_PROFIT", pos.get("bars_held", 0) + 1)
    
    # Тайм-аут
    if pos.get("bars_held", 0) >= params.get("timeout_minutes", 180):
        close_position(pos["id"], price, "TIMEOUT", pos.get("bars_held", 0) + 1)


if __name__ == "__main__":
    init_dbs()
    
    # Первый аргумент: специальные команды
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "apply_learning":
            result = apply_learning()
            send_tg(f"✅ <b>Изменения применены:</b>\n{result}")
            print(result)
            sys.exit(0)
        elif cmd == "status":
            perf = get_recent_performance()
            print(f"Сделок: {perf.get('trades',0)} | Винрейт: {perf.get('win_rate','?')}% | P&L: {perf.get('total_pnl','?')}$")
            sys.exit(0)
    
    # Загружаем цены
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT " + str(MIN_ROWS*2)
    ).fetchall()
    conn.close()
    rows = rows[::-1]
    
    if len(rows) < MIN_ROWS:
        print(f"Данных: {len(rows)}/{MIN_ROWS}")
        sys.exit(0)
    
    prices = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    
    # Проверяем открытую позицию
    pos = get_open_position()
    if pos:
        # Симулируем проверку: прошло ли 5 минут с открытия
        entry_ts = datetime.fromisoformat(pos["entry_ts"])
        minutes_passed = (datetime.now(UTC_PLUS_3) - entry_ts).total_seconds() / 60
        pos["bars_held"] = int(minutes_passed)
        
        # Проверяем по истории цен (была ли цена на уровне SL/TP)
        conn2 = sqlite3.connect(str(DB))
        recent_prices = conn2.execute(
            "SELECT last_price FROM prices WHERE id > (SELECT MAX(id) FROM prices) - ? ORDER BY id ASC",
            (int(minutes_passed) + 5,)
        ).fetchall()
        conn2.close()
        
        for (rp,) in recent_prices:
            check_position_with_price(pos, rp)
            if get_open_position() is None:
                break
    
    # Генерируем сигнал
    sig = signal(prices, volumes)
    
    if sig:
        # Сохраняем сигнал
        conn3 = sqlite3.connect(str(SIGNALS_DB))
        conn3.execute("""INSERT INTO signals_v5 
            (ts, signal, strength, price, sl, tp, rsi, trend, macd, 
             buy_rsi_used, nvt, btc_corr, bpm, fdv_mcap, onchain_flags)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(UTC_PLUS_3).isoformat(), sig["signal"], sig["strength"],
             sig["price"], sig["trade"]["stop_loss"], sig["trade"]["take_profit"],
             sig["rsi"], sig["trend"], sig.get("macd"),
             PARAMS["buy_rsi"], sig.get("nvt"), sig.get("btc_corr"),
             sig.get("bpm"), sig.get("fdv_mcap"),
             json.dumps(sig.get("onchain_flags", []))))
        sig_id = conn3.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn3.commit()
        conn3.close()
        
        # Открываем виртуальную позицию
        open_position(sig_id, sig)
        
        t = sig["trade"]
        now = datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')
        print(f"[{now}] {sig['signal']} ${sig['price']:.4f} RSI={sig['rsi']} | SL=${t['stop_loss']:.4f} | TP=${t['take_profit']:.4f}")
        send_tg(format_signal(sig))
    else:
        now = datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')
        print(f"[{now}] Нет сигнала (RSI={rsi(prices):.0f}, цена=${prices[-1]:.4f})")
