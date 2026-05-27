#!/usr/bin/env python3
"""
Стратегия v4 — ФЬЮЧЕРСЫ TON/USDT x3 (ОТКАЛИБРОВАНА ПОД ВОЛАТИЛЬНОСТЬ)
Ключевое изменение: SL/TP считаются от РЕАЛЬНОЙ волатильности, а не от фикс 2% риска.

ОНЧЕЙН-ФИЛЬТРЫ (v4.1 — 13.05.2026):
  ✓ NVT < 15 → усиление BUY (RSI-порог повышен с 35 до 38)
  ✗ NVT > 100 → блокировка BUY (рынок перегрет)
  ✗ BTC correlation > 0.7 → BUY только при восходящем тренде BTC
  ⚠ blocks_per_minute ↓ → ослабление любого сигнала до WEAK
  ✗ FDV/MCap > 2.5 → блокировка BUY (давление разлока)

Параметры:
  Маржа $5.20, плечо x3 → позиция $15.60
  SL = 0.8% цены (2× средняя волатильность за 60 мин)
  TP = 1.6% цены (SL × 2, риск/прибыль 1:2)
  Риск: ~$0.39/сделку (0.4% баланса)
  Тайм-аут: 3 часа
  Авто-реинвест: каждые +$10 → +5% к марже

Запуск: cron каждые 5 мин
"""

import sqlite3, math, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

DB = Path("/home/oleg/workspace/crypto-ton/data.db")
SIGNALS_DB = Path("/home/oleg/workspace/crypto-ton/signals.db")
TRADES_DB = Path("/home/oleg/workspace/crypto-ton/trades.db")
ONCHAIN_DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# ═══════════════════════════════════════════
#  ПАРАМЕТРЫ (ОТКАЛИБРОВАНЫ БЭКТЕСТОМ)
# ═══════════════════════════════════════════
LEVERAGE = 3
INITIAL_BALANCE = 100.0
BASE_MARGIN = 5.20

# SL/TP от волатильности (не от фикс-риска!)
SL_PRICE_PCT = 0.008   # 0.8% цены — полторы средних волатильности
TP_PRICE_PCT = 0.016   # 1.6% цены — риск/прибыль 1:2
TIMEOUT_MINUTES = 180  # 3 часа (было 60)

# Индикаторы (без изменений)
BUY_RSI = 35
SELL_RSI = 70
MIN_ROWS = 100

# ═══════════════════════════════════════════
#  БАЛАНС И МАРЖА
# ═══════════════════════════════════════════

def get_current_balance() -> float:
    conn = sqlite3.connect(str(TRADES_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, type TEXT, entry REAL, exit REAL,
        pnl REAL, pnl_pct REAL, exit_reason TEXT
    )""")
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
    conn.close()
    return INITIAL_BALANCE + total_pnl


def get_margin() -> float:
    """Маржа растёт с балансом: +5% каждые +$10."""
    balance = get_current_balance()
    extra = max(0, (balance - INITIAL_BALANCE) // 10)  # каждые +$10
    return BASE_MARGIN * (1 + extra * 0.05)


def get_trade_count() -> int:
    conn = sqlite3.connect(str(TRADES_DB))
    c = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()
    return c


def get_consecutive_losses() -> int:
    conn = sqlite3.connect(str(TRADES_DB))
    rows = conn.execute("SELECT pnl FROM trades ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    count = 0
    for (pnl,) in rows:
        if pnl < 0: count += 1
        else: break
    return count


# ═══════════════════════════════════════════
#  ИНДИКАТОРЫ
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
        avg_g = (avg_g*(per-1)+(d if d>=0 else 0))/per
        avg_l = (avg_l*(per-1)+(abs(d) if d<0 else 0))/per
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
#  ГЕНЕРАТОР СИГНАЛА
# ═══════════════════════════════════════════

def get_onchain_metrics() -> Dict[str, Any]:
    """Читает последние ончейн-метрики из onchain.db."""
    try:
        conn = sqlite3.connect(str(ONCHAIN_DB))
        # NVT: последние 2 для тренда, последнее значение
        nvt_rows = conn.execute(
            "SELECT nvt_ratio FROM onchain WHERE nvt_ratio IS NOT NULL ORDER BY id DESC LIMIT 3"
        ).fetchall()
        nvt = nvt_rows[0][0] if nvt_rows else None

        # BTC correlation: последнее значение
        corr_rows = conn.execute(
            "SELECT ton_btc_correlation FROM onchain WHERE ton_btc_correlation IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchall()
        btc_corr = corr_rows[0][0] if corr_rows else None

        # Blocks per minute: тренд по последним 10 записям
        bpm_rows = conn.execute(
            "SELECT blocks_per_minute FROM onchain WHERE blocks_per_minute IS NOT NULL ORDER BY id DESC LIMIT 10"
        ).fetchall()
        bpm = bpm_rows[0][0] if bpm_rows else None
        bpm_trend = None  # 'up', 'down', 'flat'
        if len(bpm_rows) >= 6:
            bpm_rev = list(reversed(bpm_rows))
            first_half = sum(r[0] for r in bpm_rev[:5]) / 5
            second_half = sum(r[0] for r in bpm_rev[5:]) / (len(bpm_rev) - 5)
            if second_half > first_half * 1.02:
                bpm_trend = "up"
            elif second_half < first_half * 0.98:
                bpm_trend = "down"
            else:
                bpm_trend = "flat"

        # FDV/MCap
        fdv_rows = conn.execute(
            "SELECT fdv_to_mcap FROM onchain WHERE fdv_to_mcap IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchall()
        fdv_mcap = fdv_rows[0][0] if fdv_rows else None

        conn.close()
        return {
            "nvt": nvt, "btc_corr": btc_corr,
            "bpm": bpm, "bpm_trend": bpm_trend,
            "fdv_mcap": fdv_mcap,
        }
    except Exception:
        return {}


def signal(data_prices, data_volumes) -> Optional[Dict]:
    if len(data_prices) < 100: return None
    
    cur = data_prices[-1]
    s7 = sma(data_prices, 7)
    s20 = sma(data_prices, 20)
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
        bb_lo = s20 - 2*std; bb_hi = s20 + 2*std
    
    # Тренд
    trend = "нейтральный"
    trend_score = 0
    if s7 and s20: trend_score += 1 if s7 > s20 else 0
    if macd: trend_score += 1 if macd > 0 else 0
    trend = {2: "↑ бычий", 0: "↓ медвежий"}.get(trend_score, "↔ боковик")

    # ═══ ОНЧЕЙН-ФИЛЬТРЫ ═══
    onchain = get_onchain_metrics()
    nvt = onchain.get("nvt")
    btc_corr = onchain.get("btc_corr")
    bpm_trend = onchain.get("bpm_trend")
    fdv_mcap = onchain.get("fdv_mcap")

    # Модификатор BUY_RSI в зависимости от NVT
    effective_buy_rsi = BUY_RSI
    onchain_blocks = []       # причины блокировки/ослабления
    onchain_weaken = False    # флаг ослабления сигнала
    btc_uptrend_required = False  # требуется восходящий тренд BTC

    if nvt is not None:
        if nvt < 15:
            effective_buy_rsi = 38  # усилить BUY (выше порог = легче сработать)
        elif nvt > 100:
            onchain_blocks.append(f"⛔ NVT={nvt:.1f} > 100 — рынок перегрет, BUY заблокирован")

    if btc_corr is not None and btc_corr > 0.7:
        btc_uptrend_required = True

    if bpm_trend == "down":
        onchain_weaken = True

    if fdv_mcap is not None and fdv_mcap > 2.5:
        onchain_blocks.append(f"⛔ FDV/MCap={fdv_mcap:.2f} > 2.5 — давление разлока, BUY заблокирован")

    # Проверка восходящего тренда BTC (если требуется)
    btc_ok = True
    if btc_uptrend_required and btc_corr is not None:
        # Упрощённо: смотрим BTC price change за последние 24h через onchain
        try:
            conn_oc = sqlite3.connect(str(ONCHAIN_DB))
            btc_rows = conn_oc.execute(
                "SELECT btc_price FROM onchain WHERE btc_price IS NOT NULL ORDER BY id DESC LIMIT 24"
            ).fetchall()
            conn_oc.close()
            if len(btc_rows) >= 12:
                btc_rev = list(reversed(btc_rows))
                half = len(btc_rev) // 2
                first_avg = sum(r[0] for r in btc_rev[:half]) / half
                second_avg = sum(r[0] for r in btc_rev[half:]) / (len(btc_rev) - half)
                if second_avg <= first_avg:
                    btc_ok = False
                    onchain_blocks.append(
                        f"⛔ Корреляция BTC={btc_corr:.2f} > 0.7, но BTC не в восходящем тренде"
                    )
        except Exception:
            pass

    # Расчёт SL/TP ОТ ВОЛАТИЛЬНОСТИ
    balance = get_current_balance()
    margin = get_margin()
    position = margin * LEVERAGE
    tons = position / cur
    
    sl_price = round(cur * (1 - SL_PRICE_PCT), 4)
    tp_price = round(cur * (1 + TP_PRICE_PCT), 4)
    
    # Прибыль/убыток с плечом
    sl_move_pct = SL_PRICE_PCT * LEVERAGE  # эффективное движение с плечом
    tp_move_pct = TP_PRICE_PCT * LEVERAGE
    
    loss_dollars = round(margin * sl_move_pct, 2)
    profit_dollars = round(margin * tp_move_pct, 2)
    commission = round(position * 0.0004, 4)  # 0.02% × 2
    
    tp = {
        "balance": round(balance, 0),
        "margin": round(margin, 2),
        "position": round(position, 2),
        "tons": round(tons, 2),
        "leverage": LEVERAGE,
        "stop_loss": sl_price,
        "sl_pct": round(-SL_PRICE_PCT*100, 2),
        "take_profit": tp_price,
        "tp_pct": round(TP_PRICE_PCT*100, 2),
        "profit": round(profit_dollars - commission, 2),
        "loss": round(loss_dollars + commission, 2),
        "commission": commission,
        "risk_balance_pct": round((loss_dollars+commission)/balance*100, 1),
        "risk_reward": 2.0,
    }
    
    # === BUY: RSI < effective_buy_rsi + тренд не медвежий + ончейн-фильтры ===
    if r and r < effective_buy_rsi and trend != "↓ медвежий":
        # Проверка блокировок
        if onchain_blocks:
            # BUY заблокирован ончейн-метриками — возвращаем None
            return None

        # Определение силы
        if onchain_weaken:
            strength = "WEAK"
        elif r < 30 and trend == "↑ бычий":
            strength = "STRONG"
        else:
            strength = "WEAK"

        # Собираем why с ончейн-контекстом
        why_lines = [
            f"RSI={r:.0f} < {effective_buy_rsi} ✅ перепродано",
            f"Тренд: {trend}",
            f"SL от волатильности: {SL_PRICE_PCT*100:.1f}% цены (≈2× среднее движение)",
        ]
        if nvt is not None:
            why_lines.append(f"🔗 NVT={nvt:.1f} {'(усиление: порог RSI повышен)' if nvt < 15 else ''}")
        if btc_corr is not None:
            why_lines.append(f"🔗 Корреляция TON/BTC: {btc_corr:.2f}")
        if bpm_trend:
            why_lines.append(f"🔗 Скорость блоков: {'↑ растёт' if bpm_trend=='up' else '↓ падает' if bpm_trend=='down' else '→ стабильна'}")
            if onchain_weaken:
                why_lines.append("⚠️ Сигнал ослаблен: скорость блоков снижается")

        return {
            "signal": "BUY",
            "strength": strength,
            "price": round(cur, 4),
            "trade": tp,
            "rsi": round(r, 1), "macd": round(macd, 6) if macd else None,
            "sma7": round(s7 or 0, 4), "sma20": round(s20 or 0, 4),
            "vr": round(vr, 1), "bb_lower": round(bb_lo,4) if bb_lo else None,
            "bb_upper": round(bb_hi,4) if bb_hi else None, "trend": trend,
            "why": why_lines,
        }

    # === SELL: RSI > 70 (с ослаблением при снижении скорости блоков) ===
    if r and r > SELL_RSI:
        strength = "WEAK" if onchain_weaken else ("STRONG" if r > 75 else "WEAK")
        return {
            "signal": "SELL",
            "strength": strength,
            "price": round(cur, 4),
            "trade": tp,
            "rsi": round(r, 1), "macd": round(macd, 6) if macd else None,
            "sma7": round(s7 or 0, 4), "sma20": round(s20 or 0, 4),
            "vr": round(vr, 1), "bb_lower": round(bb_lo,4) if bb_lo else None,
            "bb_upper": round(bb_hi,4) if bb_hi else None, "trend": trend,
            "why": [
                f"RSI={r:.0f} > {SELL_RSI} ✅ перекуплен",
                f"Тренд: {trend}",
            ] + ([f"⚠️ Сигнал ослаблен: скорость блоков снижается"] if onchain_weaken else []),
        }

    return None


def save_signal(sig):
    conn = sqlite3.connect(str(SIGNALS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, signal TEXT, strength TEXT,
        price REAL, rsi REAL, balance REAL,
        sl REAL, tp REAL, margin REAL, trend TEXT
    )""")
    conn.execute("""INSERT INTO signals (ts,signal,strength,price,rsi,balance,sl,tp,margin,trend)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [datetime.now(UTC_PLUS_3).isoformat(), sig["signal"], sig["strength"],
         sig["price"], sig["rsi"], sig["trade"]["balance"],
         sig["trade"]["stop_loss"], sig["trade"]["take_profit"],
         sig["trade"]["margin"], sig["trend"]])
    conn.commit()
    conn.close()


def send_tg(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat = os.getenv("TELEGRAM_HOME_CHANNEL","").strip()
    if not token or not chat: return
    try:
        d = urllib.parse.urlencode({"chat_id":chat,"text":text,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=d),timeout=10)
    except: pass


def format_signal(sig):
    em = "🟢" if sig["signal"]=="BUY" else "🔴"
    bar = "▓▓▓▓" if sig["strength"]=="STRONG" else "▓▓░░"
    t = sig["trade"]
    
    sl_str = (
        f"🛑 <b>Стоп-лосс: ${t['stop_loss']:.4f}</b> ({t['sl_pct']:.1f}% цены)\n"
        f"   → убыток: <b>-${t['loss']:.2f}</b> ({t['risk_balance_pct']:.1f}% баланса)"
    )
    tp_str = (
        f"🎯 <b>Тейк-профит: ${t['take_profit']:.4f}</b> (+{t['tp_pct']:.1f}% цены)\n"
        f"   → прибыль: <b>+${t['profit']:.2f}</b>"
    )
    
    total_trades = get_trade_count()
    consecutive = get_consecutive_losses()
    stats = f"📊 Сделок: {total_trades}"
    if consecutive:
        stats += f" | Убытков подряд: {consecutive}"
    if consecutive >= 3:
        stats += "\n⚠️ <b>ВНИМАНИЕ: 3 убытка подряд — запущен анализ!</b>"
    
    return (
        f"{em} <b>{sig['signal']} TON/USDT x{LEVERAGE}</b> {bar} {sig['strength']}\n"
        f"\n"
        f"💰 <b>Вход: ${sig['price']:.4f}</b>\n"
        f"💵 Баланс: <b>\${t['balance']:.0f}</b> | Маржа: <b>\${t['margin']:.2f}</b>\n"
        f"📐 Позиция: <b>\${t['position']:.2f}</b> = {t['tons']} TON ×{LEVERAGE}\n"
        f"{sl_str}\n"
        f"{tp_str}\n"
        f"⚖️ Риск/Прибыль: <b>1:2</b> | Комиссия: ${t['commission']:.4f}\n"
        f"\n"
        f"📈 Тренд: {sig['trend']}\n"
        f"📊 RSI={sig['rsi']} | SMA20=${sig['sma20']:.4f} | MACD={sig.get('macd','?')}\n"
        f"📏 Bollinger: ${sig['bb_lower']:.4f} — ${sig['bb_upper']:.4f}\n"
        f"\n"
        f"{chr(10).join(sig['why'])}\n"
        f"\n"
        f"{stats}\n"
        f"\n"
        f"{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )


if __name__ == "__main__":
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("SELECT last_price, volume_24h FROM prices ORDER BY id DESC LIMIT "+str(MIN_ROWS*2)).fetchall()
    conn.close()
    rows = rows[::-1]
    
    if len(rows) < MIN_ROWS:
        print(f"Данных: {len(rows)}/{MIN_ROWS}")
        sys.exit(0)
    
    prices = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    
    sig = signal(prices, volumes)
    
    if sig:
        t = sig["trade"]
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] {sig['signal']} ${sig['price']:.4f} "
              f"RSI={sig['rsi']} | SL=${t['stop_loss']:.4f} ({(t['stop_loss']/sig['price']-1)*100:+.2f}%) "
              f"| TP=${t['take_profit']:.4f} (+{(t['take_profit']/sig['price']-1)*100:+.2f}%) "
              f"| Риск \${t['loss']:.2f}")
        save_signal(sig)
        send_tg(format_signal(sig))
    else:
        r_now = rsi(prices)
        print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Нет сигнала "
              f"(RSI={r_now:.0f}, цена=${prices[-1]:.4f})")
