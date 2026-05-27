#!/usr/bin/env python3
"""
СИГНАЛЬЩИК SWING — долгосрочная стратегия (3 недели)
  • TP: 5%, SL: 2.5%, тайм-аут: 24 часа
  • Комиссия: 0.04% (симулируется в paper_trader_swing.py)
  • Свечной анализ точек входа/выхода в отчёте каждые 5 дней
  • RSI + TV-индикаторы + ончейн + сентимент

Запуск: cron каждую минуту
"""

import sqlite3, json, math, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Импорт портированных TV-индикаторов
sys.path.insert(0, str(Path(__file__).parent))
try:
    from tv_indicators import (supertrend, vwap, vwap_position, 
                                atr_stops, volume_profile_poc, 
                                combined_signal_confidence)
    HAS_TV = True
except ImportError:
    HAS_TV = False

STREAM_DB = Path("/home/oleg/workspace/crypto-ton/stream.db")
DATA_DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
PAPER_DB = Path("/home/oleg/workspace/crypto-ton/paper.db")
ONCHAIN_DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
SENTIMENT_DB = Path("/home/oleg/workspace/crypto-ton/sentiment.db")
LEARNING_DB = Path("/home/oleg/workspace/crypto-ton/learning.db")
SWING_DB = Path("/home/oleg/workspace/crypto-ton/swing.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

LEVERAGE = 3
BASE_MARGIN = 5.20
COMMISSION = 0.0004  # 0.04% комиссия Bybit

# ═══ СВИНГ-СТРАТЕГИЯ (3-недельный тест) ═══
# Цель: поймать крупные движения на 5%+ с удержанием до 24 часов
# SL шире чтобы не выбивало на внутридневном шуме
PARAMS = {
    "buy_rsi": 30,              # глубокая перепроданность
    "sell_rsi": 75,             # уверенный перекуп
    "sl_price_pct": 0.025,      # 2.5% SL (шире для свинга)
    "tp_price_pct": 0.05,       # 5% TP (ждём крупного движения)
    "timeout_minutes": 1440,    # 24 часа (сутки на отработку)
    "min_confidence": 6,        # выше порог — меньше сигналов, но качественнее
    "cooldown_minutes": 60,     # час паузы между сигналами
    "cooldown_same_type_minutes": 60,  # кулдаун между однотипными сделками (30-60 мин)
    "max_same_direction": 2,    # до 2 свингов в одном направлении
    "adx_threshold": 20,
    "rsi_confirm_bars": 3,      # 3 свечи подтверждения (жёстче фильтр)
    "trailing_stop_pct": 0.01,  # трейлинг-стоп 1% для защиты прибыли
}

# ═══ ЗАГРУЗКА ДАННЫХ ═══

def get_stream_prices(limit: int = 200) -> Tuple[List[float], List[float], List[float], List[float]]:
    """Загрузить потоковые данные из stream.db (посекундные)."""
    conn = sqlite3.connect(str(STREAM_DB))
    rows = conn.execute(
        "SELECT price, change_pct, volume_24h FROM tickers ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    rows = rows[::-1]  # oldest → newest
    
    if not rows or len(rows) < 30:
        return [], [], [], []
    
    prices = [r[0] for r in rows]
    changes = [r[1] for r in rows]
    volumes = [r[2] if r[2] and r[2] > 0 else 1e6 for r in rows]
    high = [p * 1.002 for p in prices]
    low = [p * 0.998 for p in prices]
    
    return prices, high, low, volumes


def get_fallback_prices(limit: int = 200):
    """Fallback: data.db если stream.db пуст."""
    conn = sqlite3.connect(str(DATA_DB))
    rows = conn.execute(
        "SELECT last_price, change_pct, volume_24h FROM prices ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    rows = rows[::-1]
    
    if not rows:
        return [], [], [], []
    
    prices = [r[0] for r in rows]
    volumes = [r[2] if r[2] and r[2] > 0 else 1e6 for r in rows]
    high = [p * 1.002 for p in prices]
    low = [p * 0.998 for p in prices]
    
    return prices, high, low, volumes


# ═══ ИНДИКАТОРЫ ═══

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

def adx(high: List[float], low: List[float], close: List[float], period: int = 14) -> Tuple[float, str]:
    """
    Average Directional Index.
    Возвращает (ADX, направление тренда: '↑', '↓', '↔').
    ADX > 25 = сильный тренд.
    """
    n = len(close)
    if n < period + 1:
        return 0.0, '↔'
    
    tr = [max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])) for i in range(1, n)]
    pdm = [max(0, high[i] - high[i-1]) if (high[i] - high[i-1]) > (low[i-1] - low[i]) else 0 for i in range(1, n)]
    mdm = [max(0, low[i-1] - low[i]) if (low[i-1] - low[i]) > (high[i] - high[i-1]) else 0 for i in range(1, n)]
    
    atr = sum(tr[:period]) / period
    pdi = sum(pdm[:period]) / period
    mdi = sum(mdm[:period]) / period
    
    adx_vals = []
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
        pdi = (pdi * (period - 1) + pdm[i]) / period
        mdi = (mdi * (period - 1) + mdm[i]) / period
        dx = abs(pdi - mdi) / max(pdi + mdi, 0.0001) * 100
        adx_vals.append(dx if not adx_vals else (adx_vals[-1] * (period - 1) + dx) / period)
    
    if not adx_vals:
        return 0.0, '↔'
    
    # Направление
    if pdi > mdi:
        trend = '↑'
    elif mdi > pdi:
        trend = '↓'
    else:
        trend = '↔'
    
    return round(adx_vals[-1], 1), trend


# ═══ СТАКАН ═══

def get_orderbook_metrics() -> Dict:
    try:
        conn = sqlite3.connect(str(STREAM_DB))
        row = conn.execute("""
            SELECT best_bid, best_ask, spread_pct, bid_depth_1pct, ask_depth_1pct, bids_json, asks_json
            FROM orderbook_snapshots ORDER BY id DESC LIMIT 1
        """).fetchone()
        conn.close()
        if not row:
            return {}
        
        bids = json.loads(row[5]) if row[5] else []
        asks = json.loads(row[6]) if row[6] else []
        
        imbalance = row[3] / max(row[4], 1)  # bid_depth / ask_depth
        
        # Стены (>5000 TON)
        bid_walls = [b for b in bids if b[1] >= 5000]
        ask_walls = [a for a in asks if a[1] >= 5000]
        
        return {
            "best_bid": row[0], "best_ask": row[1],
            "spread_pct": row[2],
            "bid_depth": row[3], "ask_depth": row[4],
            "imbalance": round(imbalance, 2),
            "bids": bids, "asks": asks,
            "bid_walls": bid_walls, "ask_walls": ask_walls,
        }
    except:
        return {}


# ═══ ОНЧЕЙН + СЕНТИМЕНТ ═══

def get_onchain_metrics() -> Dict:
    try:
        conn = sqlite3.connect(str(ONCHAIN_DB))
        row = conn.execute("""
            SELECT nvt_ratio, ton_btc_correlation FROM onchain
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        conn.close()
        return {"nvt": row[0], "btc_corr": row[1]} if row else {}
    except:
        return {}

def get_sentiment() -> Dict:
    try:
        conn = sqlite3.connect(str(SENTIMENT_DB))
        bullish = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1 AND sentiment='positive'").fetchone()[0]
        bearish = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1 AND sentiment='negative'").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(impact_score) FROM posts WHERE analyzed=1").fetchone()[0]
        conn.close()
        return {"bullish": bullish, "bearish": bearish, "avg_score": round(avg_score, 1) if avg_score else 0}
    except:
        return {}


# ═══ БАЛАНС ═══

def get_balance():
    try:
        conn = sqlite3.connect(str(PAPER_DB))
        pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL").fetchone()[0]
        conn.close()
        return 100.0 + pnl
    except:
        return 100.0

def get_margin():
    balance = get_balance()
    extra = max(0, (balance - 100.0) // 10)
    return BASE_MARGIN * (1 + extra * 0.05)

def get_consecutive_losses():
    try:
        conn = sqlite3.connect(str(PAPER_DB))
        rows = conn.execute("SELECT pnl FROM paper_positions WHERE pnl IS NOT NULL ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        count = 0
        for (pnl,) in rows:
            if pnl < 0: count += 1
            else: break
        return count
    except:
        return 0


# ═══ ГЛАВНЫЙ ГЕНЕРАТОР СИГНАЛА ═══

def signal_v6(prices, high, low, volumes) -> Optional[Dict]:
    if len(prices) < 50:
        return None
    
    cur = prices[-1]
    s7, s20 = sma(prices, 7), sma(prices, 20)
    r = rsi(prices, 14)
    macd = macd_line(prices)
    avg_v = sum(volumes[:-1])/len(volumes[:-1]) if len(volumes)>1 else volumes[-1]
    vr = volumes[-1]/avg_v if avg_v>0 else 1.0
    
    # Bollinger
    bb_lo = bb_hi = None
    if s20:
        recent = prices[-20:]
        var = sum((p-s20)**2 for p in recent)/20
        std = math.sqrt(var)
        bb_lo, bb_hi = s20 - 2*std, s20 + 2*std
    
    # Тренд
    trend_score = 0
    if s7 and s20: trend_score += 1 if s7 > s20 else 0
    if macd: trend_score += 1 if macd > 0 else 0
    trend = {2: "↑ бычий", 0: "↓ медвежий"}.get(trend_score, "↔ боковик")
    
    # TV-индикаторы
    tv_confidence = 0
    tv_reasons = []
    if HAS_TV:
        tv = combined_signal_confidence(prices, high, low, volumes, r or 50, macd or 0, trend)
        tv_confidence = tv.get("confidence", 0)
        tv_reasons = tv.get("reasons", [])
    
    # Стакан
    book = get_orderbook_metrics()
    book_bonus = 0
    book_reasons = []
    if book:
        imb = book.get("imbalance", 1)
        if imb > 1.5:
            book_bonus += 1
            book_reasons.append(f"Дисбаланс стакана: bid/ask={imb:.1f}x (давление вверх)")
        elif imb < 0.67:
            book_bonus -= 1
            book_reasons.append(f"Дисбаланс стакана: ask/bid={1/imb:.1f}x (давление вниз)")
    
    # Ончейн + сентимент
    onchain = get_onchain_metrics()
    sentiment = get_sentiment()
    nvt = onchain.get("nvt")
    
    nvt_bonus = 0
    if nvt and nvt < 15:
        nvt_bonus = 1
    elif nvt and nvt > 100:
        nvt_bonus = -2  # блок
    
    sent_bonus = 0
    if sentiment:
        bull = sentiment.get("bullish", 0)
        bear = sentiment.get("bearish", 0)
        if bull > bear * 2:
            sent_bonus = 1
        elif bear > bull * 2:
            sent_bonus = -1
    
    # Общая уверенность
    base_confidence = tv_confidence + book_bonus + nvt_bonus + sent_bonus
    confidence = max(0, min(10, base_confidence + 2))  # +2 базовых за RSI/тренд
    
    # ═══ ФИЛЬТР A: ADX (не входить против сильного тренда) ═══
    adx_val, adx_trend = adx(high, low, prices, period=14)
    adx_limit = PARAMS.get("adx_threshold", 20)
    trend_block = ""
    if adx_val > adx_limit:
        if adx_trend == '↑':
            trend_block = "SELL"  # блокируем SELL при сильной бычке
            book_reasons.append(f"ADX={adx_val:.0f}>{adx_limit} ↑ — SELL заблокирован (тренд бычий)")
        elif adx_trend == '↓':
            trend_block = "BUY"   # блокируем BUY при сильной медвежке
            book_reasons.append(f"ADX={adx_val:.0f}>{adx_limit} ↓ — BUY заблокирован (тренд медвежий)")
    else:
        book_reasons.append(f"ADX={adx_val:.0f}≤{adx_limit} — тренд слабый, сигналы разрешены")

    # ═══ ФИЛЬТР B: RSI ПОДТВЕРЖДЕНИЕ (N свечей подряд за порогом) ═══
    rsi_confirm = PARAMS.get("rsi_confirm_bars", 2)
    rsi_confirmed = True
    if rsi_confirm > 1 and r is not None:
        # Проверяем RSI на предыдущих свечах (используем RSI с откатом)
        recent_ok = 0
        for offset in range(rsi_confirm):
            idx = -(1 + offset)
            if abs(idx) > len(prices):
                break
            # Приблизительная проверка: цена должна быть в том же направлении
            if len(prices) >= abs(idx) + 15:
                r_back = rsi(prices[:idx+1] if idx < -1 else prices[:-1], 14)
                if r_back is not None:
                    if (r > PARAMS["sell_rsi"] and r_back > PARAMS["sell_rsi"]) or \
                       (r < PARAMS["buy_rsi"] and r_back < PARAMS["buy_rsi"]):
                        recent_ok += 1
            else:
                recent_ok += 1  # недостаточно данных — разрешаем
        rsi_confirmed = recent_ok >= rsi_confirm - 1
        if not rsi_confirmed:
            book_reasons.append(f"RSI за порогом < {rsi_confirm} свечей — ждём подтверждения")

    # ═══ ФИЛЬТР C: КУЛДАУН ПОСЛЕ УБЫТКА + ПО ТИПУ ═══
    cooldown_min = PARAMS.get("cooldown_minutes", 60)
    cooldown_type_min = PARAMS.get("cooldown_same_type_minutes", 60)
    cooldown_active = False
    last_closed_type = None
    last_closed_minutes = 999
    if cooldown_min > 0 or cooldown_type_min > 0:
        try:
            conn2 = sqlite3.connect(str(SWING_DB))
            last_closed = conn2.execute(
                "SELECT closed_ts, pnl, type FROM swing_positions WHERE closed_ts IS NOT NULL AND type IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn2.close()
            if last_closed:
                last_dt = datetime.fromisoformat(last_closed[0])
                last_closed_minutes = (datetime.now(UTC_PLUS_3) - last_dt).total_seconds() / 60
                last_closed_type = last_closed[2]
                last_pnl = last_closed[1]
                
                # Кулдаун после убытка (любого типа)
                if cooldown_min > 0 and last_pnl is not None and last_pnl < 0:
                    if last_closed_minutes < cooldown_min:
                        cooldown_active = True
                        book_reasons.append(f"Кулдаун {cooldown_min} мин после убытка (прошло {last_closed_minutes:.0f} мин)")
        except:
            pass

    # ═══ ФИЛЬТР D: МАКС ОДИНАКОВЫХ НАПРАВЛЕНИЙ ═══
    max_same = PARAMS.get("max_same_direction", 1)
    same_dir_blocked = False
    if max_same > 0:
        try:
            conn3 = sqlite3.connect(str(PAPER_DB))
            last_type = conn3.execute(
                "SELECT type FROM paper_positions WHERE closed_ts IS NOT NULL ORDER BY id DESC LIMIT ?",
                (max_same,)
            ).fetchall()
            conn3.close()
            if len(last_type) >= max_same and all(t[0] == last_type[0][0] for t in last_type):
                same_dir_blocked = True
                book_reasons.append(f"Уже {max_same} {last_type[0][0]} подряд — ждём смены направления")
        except:
            pass
    
    # ═══ ФИЛЬТР E: СТЕНЫ В СТАКАНЕ ═══
    wall_support = ""
    if book.get("bid_walls"):
        wall_support = "BUY"
        book_reasons.append(f"Стена покупок {book['bid_walls'][0][0]:.4f} ({book['bid_walls'][0][1]:.0f} TON) — BUY защищён")
    if book.get("ask_walls"):
        if wall_support:
            wall_support = "BOTH"
        else:
            wall_support = "SELL"
        book_reasons.append(f"Стена продаж {book['ask_walls'][0][0]:.4f} ({book['ask_walls'][0][1]:.0f} TON) — SELL защищён")
    
    # Сигнал
    if confidence < PARAMS["min_confidence"]:
        return None
    
    # Кулдаун после убытка
    if cooldown_active:
        return None
    
    effective_buy_rsi = PARAMS["buy_rsi"]
    if nvt and nvt < 15:
        effective_buy_rsi = min(38, PARAMS["buy_rsi"] + 3)
    
    # Блокировка
    blocked = nvt_bonus == -2
    
    result = None
    
    # BUY
    if r and r < effective_buy_rsi and not blocked and trend != "↓ медвежий" and rsi_confirmed:
        if trend_block == "BUY":
            return None  # ADX блок
        if same_dir_blocked:
            return None  # уже N сделок в одном направлении
        # Кулдаун по типу
        if not cooldown_active and last_closed_type == "BUY":
            if last_closed_minutes < cooldown_type_min:
                book_reasons.append(f"Кулдаун {cooldown_type_min} мин между BUY-сделками (прошло {last_closed_minutes:.0f} мин)")
                return None
        
        strength = "STRONG" if confidence >= 7 else ("WEAK" if confidence >= 4 else "WEAK")
        
        # SL/TP от стакана (если есть данные)
        buy_sl = round(cur * (1 - PARAMS["sl_price_pct"]), 4)
        buy_tp = round(cur * (1 + PARAMS["tp_price_pct"]), 4)
        
        if book.get("bid_walls"):
            # SL за стеной покупок
            wall_price = book["bid_walls"][0][0]
            buy_sl = round(wall_price * 0.999, 4)
        if wall_support in ("BUY", "BOTH"):
            confidence = min(10, confidence + 1)  # +1 уверенности за стену
        
        result = {"signal": "BUY", "strength": strength, "confidence": confidence}
    
    # SELL
    if r and r > PARAMS["sell_rsi"] and not blocked and rsi_confirmed:
        if trend_block == "SELL":
            return None  # ADX блок
        if same_dir_blocked:
            return None  # уже N сделок в одном направлении
        # Кулдаун по типу
        if not cooldown_active and last_closed_type == "SELL":
            if last_closed_minutes < cooldown_type_min:
                book_reasons.append(f"Кулдаун {cooldown_type_min} мин между SHORT-сделками (прошло {last_closed_minutes:.0f} мин)")
                return None
        
        strength = "STRONG" if confidence >= 7 else ("WEAK" if confidence >= 4 else "WEAK")
        
        sell_sl = round(cur * (1 + PARAMS["sl_price_pct"]), 4)
        sell_tp = round(cur * (1 - PARAMS["tp_price_pct"]), 4)
        
        if book.get("ask_walls"):
            wall_price = book["ask_walls"][0][0]
            sell_sl = round(wall_price * 1.001, 4)
        if wall_support in ("SELL", "BOTH"):
            confidence = min(10, confidence + 1)  # +1 уверенности за стену
        
        result = {"signal": "SELL", "strength": strength, "confidence": confidence}
    
    if not result:
        return None
    
    # Расчёт SL/TP
    balance = get_balance()
    margin = get_margin()
    position = margin * LEVERAGE
    tons = position / cur
    
    if result["signal"] == "BUY":
        sl_price = buy_sl
        tp_price = buy_tp
        sl_pct = round((sl_price / cur - 1) * 100, 2)
        tp_pct = round((tp_price / cur - 1) * 100, 2)
    else:
        sl_price = sell_sl
        tp_price = sell_tp
        sl_pct = round((sl_price / cur - 1) * 100, 2)
        tp_pct = round((tp_price / cur - 1) * 100, 2)
    
    sl_move = abs(sl_pct) / 100 * LEVERAGE
    tp_move = abs(tp_pct) / 100 * LEVERAGE
    loss_d = round(margin * sl_move, 2)
    profit_d = round(margin * tp_move, 2)
    commission = round(position * 0.0004, 4)
    
    trade = {
        "balance": round(balance, 0), "margin": round(margin, 2),
        "position": round(position, 2), "tons": round(tons, 2),
        "leverage": LEVERAGE, "stop_loss": sl_price,
        "sl_pct": sl_pct, "take_profit": tp_price,
        "tp_pct": tp_pct, "profit": round(profit_d - commission, 2),
        "loss": round(loss_d + commission, 2), "commission": commission,
        "risk_balance_pct": round((loss_d+commission)/balance*100, 1),
        "risk_reward": round(abs(tp_pct/sl_pct), 1) if sl_pct else 2.0,
    }
    
    result["trade"] = trade
    result["price"] = round(cur, 4)
    result["rsi"] = round(r, 1)
    result["trend"] = trend
    result["tv_reasons"] = tv_reasons
    result["book_reasons"] = book_reasons
    
    # Сохраняем в signals_v6
    conn = sqlite3.connect(str(SIGNALS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS signals_swing (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, signal TEXT, strength INTEGER, confidence INTEGER,
        price REAL, sl REAL, tp REAL, rsi REAL, trend TEXT,
        tv_indicators TEXT, book_metrics TEXT
    )""")
    conn.execute("""INSERT INTO signals_swing (ts, signal, strength, confidence, price, sl, tp, rsi, trend, tv_indicators, book_metrics)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(UTC_PLUS_3).isoformat(), result["signal"], result["strength"],
         confidence, result["price"], sl_price, tp_price,
         result["rsi"], trend, json.dumps(tv_reasons), json.dumps(book_reasons)))
    conn.commit()
    conn.close()
    
    return result


# ═══ TELEGRAM ═══

def send_tg(text):
    token = os.getenv("TRADING_BOT_TOKEN","").strip()
    chat = os.getenv("TRADING_CHAT_ID","").strip()
    if not token or not chat:
        token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
        chat = os.getenv("TELEGRAM_HOME_CHANNEL","").strip()
    if not token or not chat: return
    try:
        d = urllib.parse.urlencode({"chat_id":chat,"text":text,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=d),timeout=10)
    except: pass


def format_signal_v6(sig):
    em = "🟢" if sig["signal"]=="BUY" else "🔴"
    conf_bar = "█" * sig["confidence"] + "░" * (10 - sig["confidence"])
    bar = "▓▓▓▓" if sig["strength"]=="STRONG" else "▓▓░░"
    t = sig["trade"]
    
    sl_str = (
        f"🛑 <b>Стоп-лосс: {t['stop_loss']:.4f}</b> ({t['sl_pct']:+.1f}%)\n"
        f"   → убыток: <b>-{t['loss']:.2f} USD</b> ({t['risk_balance_pct']:.1f}% баланса)"
    )
    tp_str = (
        f"🎯 <b>Тейк-профит: {t['take_profit']:.4f}</b> ({t['tp_pct']:+.1f}%)\n"
        f"   → прибыль: <b>+{t['profit']:.2f} USD</b>"
    )
    
    tv_lines = ""
    if sig.get("tv_reasons"):
        tv_lines = "🧠 TV-индикаторы:\n" + "\n".join(f"  {r}" for r in sig["tv_reasons"][:4]) + "\n"
    
    book_lines = ""
    if sig.get("book_reasons"):
        book_lines = "📊 Стакан:\n" + "\n".join(f"  {r}" for r in sig["book_reasons"][:2]) + "\n"
    
    loss_count = get_consecutive_losses()
    warn = f"⚠️ Убытков подряд: {loss_count}\n" if loss_count > 0 else ""
    if loss_count >= 3:
        warn += "🧠 3 убытка подряд — нужен анализ!\n"
    
    return (
        f"{em} <b>{sig['signal']} TON/USDT ×{LEVERAGE}</b> {bar} {sig['strength']}\n"
        f"Уверенность: {conf_bar} <b>{sig['confidence']}/10</b>\n"
        f"\n💰 <b>Вход: {sig['price']:.4f}</b>\n"
        f"💵 Баланс: <b>{t['balance']:.0f} USD</b> | Маржа: <b>{t['margin']:.2f}</b>\n"
        f"📐 Позиция: <b>{t['position']:.2f} USD</b> = {t['tons']} TON ×{LEVERAGE}\n"
        f"{sl_str}\n{tp_str}\n"
        f"⚖️ Риск/Прибыль: <b>1:{t['risk_reward']}</b> | Комиссия: {t['commission']:.4f} USD\n"
        f"\n📈 Тренд: {sig['trend']} | RSI={sig['rsi']}\n"
        f"{tv_lines}\n{book_lines}\n{warn}"
        f"\n{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК | v6"
    )


# ═══ MAIN ═══

if __name__ == "__main__":
    now = datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')
    
    # Загружаем данные: ИНДИКАТОРЫ из data.db (минутные свечи — RSI не залипает)
    # Стакан — из stream.db (быстрые данные)
    prices, high, low, volumes = get_fallback_prices(limit=200)  # data.db для индикаторов
    
    if len(prices) < 50:
        print(f"[{now}] Данных недостаточно ({len(prices)})")
        sys.exit(0)
    
    # Проверка: цена застряла? (одинаковая последние 10 минут)
    last_10 = prices[-10:]
    if len(set(last_10)) <= 2:
        print(f"[{now}] Цена почти не меняется ({set(last_10)}) — пропускаем")
        sys.exit(0)
    
    # Анти-спам: не отправлять сигнал чаще чем раз в 15 минут
    try:
        last_check = sqlite3.connect(str(SIGNALS_DB))
        last_sig = last_check.execute(
            "SELECT ts FROM signals_swing WHERE signal IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_check.close()
        if last_sig and last_sig[0]:
            # ts format: '2026-05-15T11:13:42.123456+03:00'
            last_ts_str = last_sig[0].split('.')[0]  # отрезаем микросекунды
            last_dt = datetime.fromisoformat(last_ts_str.split('+')[0] + '+03:00')
            age = (datetime.now(UTC_PLUS_3) - last_dt).total_seconds()
            if age < 900:  # 15 минут
                print(f"[{now}] Сигнал уже был {age:.0f} сек назад — анти-спам")
                sys.exit(0)
    except:
        pass
    
    sig = signal_v6(prices, high, low, volumes)
    
    if sig:
        t = sig["trade"]
        print(f"[{now}] {sig['signal']} {sig['price']:.4f} RSI={sig['rsi']} "
              f"conf={sig['confidence']}/10 | SL={t['stop_loss']:.4f} TP={t['take_profit']:.4f}")
        send_tg(format_signal_v6(sig))
    else:
        print(f"[{now}] Нет сигнала (RSI={rsi(prices):.0f}, цена={prices[-1]:.4f}, "
              f"VWAP={'?' if not HAS_TV else vwap(high,low,prices,volumes):.4f})")
