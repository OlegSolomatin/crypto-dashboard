#!/usr/bin/env python3
"""
Расширенная ончейн-аналитика TON — 5 источников.
Собирает метрики каждый час в onchain.db.

Источники (все БЕСПЛАТНЫЕ, без API-ключей):
  1. CoinGecko — market cap, FDV, supply, объёмы
  2. Toncenter — скорость мастерчейна, активность сети
  3. DeDust — TVL и ликвидность TON/USDT (через резервы × цену)
  4. TON Whales — киты (>100k TON в последнем блоке)
  5. BTC/USDT — корреляция с биткоином

Где взять API:
  CoinGecko:  https://api.coingecko.com (публичный, без ключа)
  Toncenter:  https://toncenter.com (публичный, без ключа, лимит ~10 запросов/сек)
  DeDust:     https://api.dedust.io (публичный, без ключа)
  Bybit BTC:  https://api.bybit.com (публичный, без ключа)
"""

import sqlite3, json, urllib.request, sys, time as time_mod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))


# ═══════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════

def http_get(url: str, timeout: int = 15) -> Optional[dict]:
    """GET запрос с повтором при 429."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt, 8)  # exponential backoff: 1, 2, 4 сек
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time_mod.sleep(wait)
                continue
            print(f"  HTTP {e.code}: {e}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt == 2:
                print(f"  Error: {e}", file=sys.stderr)
            time_mod.sleep(0.5)
    return None


def db_save(table: str, data: Dict):
    """Универсальное сохранение dict в таблицу."""
    conn = sqlite3.connect(str(DB))
    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    # Filter only existing columns
    filtered = {k: v for k, v in data.items() if k in cols}
    if not filtered:
        conn.close()
        return
    placeholders = ", ".join(["?"] * len(filtered))
    columns = ", ".join(filtered.keys())
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(filtered.values()))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#  1. COINGECKO
# ═══════════════════════════════════════════════════════════════

def fetch_coingecko() -> Optional[Dict]:
    url = ("https://api.coingecko.com/api/v3/coins/the-open-network"
           "?localization=false&tickers=false&community_data=false&developer_data=false")
    data = http_get(url, timeout=20)
    if not data:
        return None
    m = data.get("market_data", {})
    return {
        "price_usd": (m.get("current_price") or {}).get("usd"),
        "market_cap": (m.get("market_cap") or {}).get("usd"),
        "volume_24h": (m.get("total_volume") or {}).get("usd"),
        "high_24h": (m.get("high_24h") or {}).get("usd"),
        "low_24h": (m.get("low_24h") or {}).get("usd"),
        "price_change_24h_pct": m.get("price_change_percentage_24h"),
        "circulating_supply": m.get("circulating_supply"),
        "total_supply": m.get("total_supply"),
        "market_cap_rank": data.get("market_cap_rank"),
        "fdv": (m.get("fully_diluted_valuation") or {}).get("usd"),
    }


# ═══════════════════════════════════════════════════════════════
#  2. TONCENTER
# ═══════════════════════════════════════════════════════════════

def fetch_toncenter() -> Optional[Dict]:
    data = http_get("https://toncenter.com/api/v2/getMasterchainInfo", timeout=10)
    if not data or not data.get("ok"):
        return None
    
    current_seqno = data["result"]["last"]["seqno"]
    
    # Сравнение с предыдущим часом
    conn = sqlite3.connect(str(DB))
    conn.execute("CREATE TABLE IF NOT EXISTS onchain_raw (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, metric TEXT, value TEXT)")
    row = conn.execute("SELECT value FROM onchain_raw WHERE metric='masterchain_seqno' ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    
    blocks_per_minute = None
    if row:
        prev = int(row[0])
        if current_seqno > prev:
            blocks_per_minute = round((current_seqno - prev) / 60, 1)
    
    return {"masterchain_seqno": current_seqno, "blocks_per_minute": blocks_per_minute}


# ═══════════════════════════════════════════════════════════════
#  3. DEDUST DEX TVL
# ═══════════════════════════════════════════════════════════════

def fetch_dex_tvl(ton_price_usd: Optional[float]) -> Optional[Dict]:
    """TVL — используем заглушку (DeDust v2 list не отдаёт reserves)."""
    # DeDust API v2: list endpoint doesn't include reserves.
    # To get reserves, must query each pool individually.
    # For now, skip — CoinGecko market data already includes volume.
    return None


# ═══════════════════════════════════════════════════════════════
#  4. TON WHALES
# ═══════════════════════════════════════════════════════════════

def fetch_whale_activity() -> Optional[Dict]:
    """Сканируем транзакции >100k TON в последнем мастерчейн-блоке."""
    mc = http_get("https://toncenter.com/api/v2/getMasterchainInfo", timeout=10)
    if not mc or not mc.get("ok"):
        return None
    
    seqno = mc["result"]["last"]["seqno"]
    url = (f"https://toncenter.com/api/v2/getBlockTransactions"
           f"?workchain=-1&shard=-9223372036854775808&seqno={seqno}&count=100")
    
    data = http_get(url, timeout=15)
    if not data or not data.get("ok"):
        return None
    
    txns = data["result"].get("transactions", [])
    total = len(txns)
    whales = 0
    
    for tx in txns:
        in_msg = tx.get("in_msg") or {}
        value_nano = int(in_msg.get("value", 0) or 0)
        value_ton = value_nano / 1e9
        if value_ton >= 100_000:
            whales += 1
    
    return {
        "whale_txns_last_block": whales,
        "total_txns_last_block": total,
        "whale_ratio": round(whales / max(total, 1) * 100, 1) if total > 0 else 0,
    }


# ═══════════════════════════════════════════════════════════════
#  5. BTC PRICE
# ═══════════════════════════════════════════════════════════════

def fetch_btc_price() -> Optional[float]:
    data = http_get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT", timeout=10)
    if data and data.get("retCode") == 0:
        return float(data["result"]["list"][0]["lastPrice"])
    return None


# ═══════════════════════════════════════════════════════════════
#  КОРРЕЛЯЦИЯ TON-BTC
# ═══════════════════════════════════════════════════════════════

def calculate_correlation() -> Optional[float]:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT price, btc_price FROM onchain "
        "WHERE price IS NOT NULL AND btc_price IS NOT NULL "
        "ORDER BY id DESC LIMIT 24"
    ).fetchall()
    conn.close()
    
    if len(rows) < 6:
        return None
    
    ton = [r[0] for r in rows]
    btc = [r[1] for r in rows]
    n = len(ton)
    
    avg_t = sum(ton) / n
    avg_b = sum(btc) / n
    
    cov = sum((ton[i] - avg_t) * (btc[i] - avg_b) for i in range(n))
    var_t = sum((t - avg_t) ** 2 for t in ton)
    var_b = sum((b - avg_b) ** 2 for b in btc)
    
    if var_t == 0 or var_b == 0:
        return None
    
    return round(cov / (var_t ** 0.5 * var_b ** 0.5), 3)


# ═══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(str(DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS onchain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        -- CoinGecko
        price REAL, market_cap REAL, volume_24h REAL,
        circulating_supply REAL, total_supply REAL, market_cap_rank INTEGER,
        fdv REAL, price_change_24h_pct REAL,
        -- Toncenter
        masterchain_seqno INTEGER, blocks_per_minute REAL,
        -- DEX
        dex_tvl_usd REAL, dex_ton_usdt_liquidity_usd REAL,
        dex_pool_count INTEGER, dex_ton_usdt_pools INTEGER,
        -- Whales
        whale_txns_last_block INTEGER, total_txns_last_block INTEGER, whale_ratio REAL,
        -- BTC
        btc_price REAL,
        -- Расчётные
        nvt_ratio REAL, fdv_to_mcap REAL, ton_btc_correlation REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_onchain_ts ON onchain(ts)")
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    init_db()
    now = datetime.now(UTC_PLUS_3).isoformat()
    
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Сбор ончейн-данных TON...")
    
    # 1. CoinGecko
    cg = fetch_coingecko()
    ton_price = cg["price_usd"] if cg else None
    if cg:
        print(f"  ✅ CoinGecko: ${ton_price:.4f} | MCap ${(cg['market_cap'] or 0)/1e9:.2f}B")
    else:
        print(f"  ❌ CoinGecko недоступен")
    
    # 2. Toncenter
    time_mod.sleep(0.5)
    tc = fetch_toncenter()
    if tc:
        print(f"  ✅ Toncenter: seqno={tc['masterchain_seqno']} | {tc.get('blocks_per_minute','?')} бл/мин")
    else:
        print(f"  ❌ Toncenter недоступен")
    
    # 3. DeDust DEX
    time_mod.sleep(0.5)
    dex = fetch_dex_tvl(ton_price)
    if dex:
        print(f"  ✅ DeDust TVL: ${dex['dex_tvl_usd']}M | TON/USDT: ${dex['dex_ton_usdt_liquidity_usd']}M")
    else:
        print(f"  ❌ DeDust недоступен")
    
    # 4. Whales
    time_mod.sleep(1.0)  # Toncenter rate limit
    whales = fetch_whale_activity()
    if whales:
        print(f"  ✅ Whales: {whales['whale_txns_last_block']}/{whales['total_txns_last_block']} китов (>100k TON)")
    else:
        print(f"  ❌ Whales недоступны")
    
    # 5. BTC
    time_mod.sleep(0.3)
    btc = fetch_btc_price()
    if btc:
        print(f"  ✅ BTC: ${btc:,.0f}")
    
    # Собираем в одну запись
    record = {"ts": now}
    
    if cg:
        record.update({k: cg[k] for k in ["price_usd","market_cap","volume_24h",
            "circulating_supply","total_supply","market_cap_rank","fdv","price_change_24h_pct"]})
        # Переименовываем price_usd → price для схемы
        record["price"] = cg["price_usd"]
        del record["price_usd"]
    
    if tc:
        record.update(tc)
    if dex:
        record.update(dex)
    if whales:
        record.update(whales)
    if btc:
        record["btc_price"] = btc
    
    # Расчётные метрики
    if record.get("market_cap") and record.get("volume_24h") and record["volume_24h"] > 0:
        record["nvt_ratio"] = round(record["market_cap"] / record["volume_24h"], 2)
    if record.get("fdv") and record.get("market_cap") and record["market_cap"] > 0:
        record["fdv_to_mcap"] = round(record["fdv"] / record["market_cap"], 2)
    
    db_save("onchain", record)
    
    # Сохраняем raw seqno для сравнения
    if tc:
        conn = sqlite3.connect(str(DB))
        conn.execute("INSERT INTO onchain_raw (ts, metric, value) VALUES (?,?,?)",
                     [now, "masterchain_seqno", str(tc["masterchain_seqno"])])
        conn.commit()
        conn.close()
    
    # Корреляция
    corr = calculate_correlation()
    if corr is not None:
        conn = sqlite3.connect(str(DB))
        conn.execute("UPDATE onchain SET ton_btc_correlation=? WHERE ts=?", [corr, now])
        conn.commit()
        conn.close()
        print(f"  📐 Корреляция TON/BTC: {corr:+.2f} {'🟢 следом' if abs(corr)>0.5 else '🟡 слабая'}")
    
    # Краткий отчёт
    print(f"\n  ═══ ОНЧЕЙН-ОТЧЁТ {datetime.now(UTC_PLUS_3).strftime('%d.%m %H:%M')} ═══")
    if record.get("price"):
        print(f"  💰 ${record['price']:.4f} ({record.get('price_change_24h_pct','?'):+.2f}%)")
    if record.get("nvt_ratio"):
        print(f"  ⚡ NVT: {record['nvt_ratio']:.1f} {'🟢недооценена' if record['nvt_ratio']<15 else '🟡норма' if record['nvt_ratio']<50 else '🔴переоценена'}")
    if record.get("dex_tvl_usd"):
        print(f"  💧 DEX TVL: ${record['dex_tvl_usd']}M (TON/USDT: ${record['dex_ton_usdt_liquidity_usd']}M)")
    if record.get("whale_ratio", 0) > 0:
        print(f"  🐋 Киты: {record['whale_ratio']}% транзакций в блоке")
    if record.get("blocks_per_minute"):
        print(f"  ⛓️ Сеть: {record['blocks_per_minute']} бл/мин | seqno={record.get('masterchain_seqno','?')}")
    print(f"  ✓ Сохранено в {DB.name}")


if __name__ == "__main__":
    main()
