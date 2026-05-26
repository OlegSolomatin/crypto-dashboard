#!/usr/bin/env python3
"""
Расширенная ончейн-аналитика TON — 5 источников данных.
Собирает метрики каждый час в onchain.db.

Источники:
  1. CoinGecko — фундаментал (market cap, FDV, NVT, supply)
  2. Toncenter — сеть (скорость блоков, активность мастерчейна)
  3. DeDust + STON.fi — DEX (TVL, ликвидность, пулы TON/USDT)
  4. TON Whales — киты (транзакции >100k TON)
  5. TON Foundation — баланс фонда
  6. BTC/USDT — корреляция с биткоином

API ключи: НЕ НУЖНЫ (все публичные и бесплатные)
"""

import sqlite3, json, urllib.request, time, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# Адреса для отслеживания
TON_FOUNDATION = "EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"
TON_BRIDGE = "Ef87m7_QrVM4uXAPCDM4DuF9Rj5Rwa5vVfTZDxFNkeo2gwaB"  # TON-Ethereum bridge
WHALE_THRESHOLD = 100_000  # транзакции >100k TON = кит


# ═══════════════════════════════════════════
#  1. COINGECKO (фундаментал)
# ═══════════════════════════════════════════

def fetch_coingecko() -> Optional[Dict]:
    try:
        url = ("https://api.coingecko.com/api/v3/coins/the-open-network"
               "?localization=false&tickers=false&community_data=false&developer_data=false")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        m = data.get("market_data", {})
        return {
            "price_usd": m.get("current_price", {}).get("usd"),
            "market_cap": m.get("market_cap", {}).get("usd"),
            "volume_24h": m.get("total_volume", {}).get("usd"),
            "high_24h": m.get("high_24h", {}).get("usd"),
            "low_24h": m.get("low_24h", {}).get("usd"),
            "price_change_24h_pct": m.get("price_change_percentage_24h"),
            "circulating_supply": m.get("circulating_supply"),
            "total_supply": m.get("total_supply"),
            "market_cap_rank": data.get("market_cap_rank"),
            "fdv": m.get("fully_diluted_valuation", {}).get("usd"),
        }
    except Exception as e:
        print(f"  CoinGecko: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════
#  2. TONCENTER (сеть)
# ═══════════════════════════════════════════

def fetch_toncenter() -> Optional[Dict]:
    try:
        req = urllib.request.Request(
            "https://toncenter.com/api/v2/getMasterchainInfo",
            headers={"User-Agent": "Hermes/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            mc = json.loads(r.read())
        if not mc.get("ok"):
            return None
        
        current_seqno = mc["result"]["last"]["seqno"]
        
        # Сравнение с предыдущим часом
        prev_seqno = None
        conn = sqlite3.connect(str(DB))
        conn.execute("CREATE TABLE IF NOT EXISTS onchain_raw (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, metric TEXT, value TEXT)")
        row = conn.execute("SELECT value FROM onchain_raw WHERE metric='masterchain_seqno' ORDER BY ts DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            prev_seqno = int(row[0])
        
        blocks_per_minute = None
        if prev_seqno and current_seqno > prev_seqno:
            blocks_per_minute = round((current_seqno - prev_seqno) / 60, 1)
        
        return {
            "masterchain_seqno": current_seqno,
            "blocks_per_minute": blocks_per_minute,
        }
    except Exception as e:
        print(f"  Toncenter: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════
#  3. DEX (DeDust + STON.fi)
# ═══════════════════════════════════════════

def fetch_dex_metrics() -> Optional[Dict]:
    """TVL и ликвидность TON/USDT на двух главных DEX."""
    tvl_total = 0
    ton_usdt_liquidity = 0
    pool_count = 0
    
    # DeDust
    try:
        url = "https://api.dedust.io/v2/pools?page_size=25&sort_by=tvl&page=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if isinstance(data, list):
                pool_count += len(data)
                for pool in data:
                    total_supply = pool.get("totalSupply", "0")
                    if not total_supply or total_supply == "0":
                        continue
                    tvl = float(total_supply)
                    tvl_total += tvl
                    # Проверяем, TON/USDT ли это
                    assets = pool.get("assets", [])
                    if len(assets) >= 2:
                        symbols = [a.get("symbol", "").upper() for a in assets if a.get("symbol")]
                        if "TON" in symbols and any(s in str(symbols) for s in ["USDT","JUSDT","STTSDT"]):
                            ton_usdt_liquidity += tvl
    except Exception as e:
        print(f"  DeDust: {e}", file=sys.stderr)
    
    # STON.fi
    try:
        url = "https://api.ston.fi/v1/pools?page_size=100&page=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            pools = data.get("pool_list", [])
            pool_count += len(pools)
            for pool in pools:
                tvl = float(pool.get("tvl", 0) or 0)
                tvl_total += tvl
                tokens = pool.get("token0_address", ""), pool.get("token1_address", "")
                # Проверяем TON/USDT (приблизительно по названиям)
                if ("ton" in str(pool).lower() or "TON" in str(pool)) and (
                    "usdt" in str(pool).lower() or "USDT" in str(pool) or
                    "jUSDT" in str(pool) or "stTON" in str(pool)
                ):
                    ton_usdt_liquidity += tvl
    except Exception as e:
        print(f"  STON.fi: {e}", file=sys.stderr)
    
    return {
        "dex_tvl_total": round(tvl_total / 1e6, 2),  # миллионы долларов
        "dex_ton_usdt_liquidity": round(ton_usdt_liquidity / 1e6, 2),
        "dex_pool_count": pool_count,
    }


# ═══════════════════════════════════════════
#  4. TON WHALES (крупные транзакции)
# ═══════════════════════════════════════════

def fetch_whale_activity() -> Optional[Dict]:
    """Сканирование крупных транзакций в мастерчейне."""
    try:
        # Получаем последний блок мастерчейна
        req = urllib.request.Request(
            "https://toncenter.com/api/v2/getMasterchainInfo",
            headers={"User-Agent": "Hermes/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            mc = json.loads(r.read())
        if not mc.get("ok"):
            return None
        
        # Transactions in masterchain block
        seqno = mc["result"]["last"]["seqno"]
        url = f"https://toncenter.com/api/v2/getBlockTransactions?workchain=-1&shard=-9223372036854775808&seqno={seqno}&count=100"
        
        req2 = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req2, timeout=15) as r:
            data = json.loads(r.read())
        
        if not data.get("ok"):
            return None
        
        txns = data["result"].get("transactions", [])
        total_txns = len(txns)
        large_txns = 0
        
        for tx in txns:
            # Check value
            if "in_msg" in tx and tx["in_msg"].get("value"):
                value_ton = int(tx["in_msg"]["value"]) / 1e9
                if value_ton >= WHALE_THRESHOLD:
                    large_txns += 1
        
        return {
            "whale_txns_last_block": large_txns,
            "total_txns_last_block": total_txns,
            "whale_ratio": round(large_txns / max(total_txns, 1) * 100, 1),
        }
    except Exception as e:
        print(f"  Whales: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════
#  5. TON FOUNDATION (баланс фонда)
# ═══════════════════════════════════════════

def fetch_foundation_balance() -> Optional[Dict]:
    """Баланс TON Foundation и моста."""
    foundations = {
        "ton_foundation": TON_FOUNDATION,
        "ton_bridge": TON_BRIDGE,
    }
    result = {}
    
    for name, addr in foundations.items():
        try:
            url = f"https://toncenter.com/api/v2/getWalletInformation?address={addr}"
            req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
                if data.get("ok"):
                    balance = int(data["result"]["balance"]) / 1e9
                    result[f"{name}_balance"] = round(balance, 0)
        except Exception as e:
            print(f"  Foundation/{name}: {e}", file=sys.stderr)
    
    return result if result else None


# ═══════════════════════════════════════════
#  6. BTC/USDT CORRELATION
# ═══════════════════════════════════════════

def fetch_btc_price() -> Optional[float]:
    """Текущая цена BTC для расчёта корреляции."""
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if data.get("retCode") == 0:
                return float(data["result"]["list"][0]["lastPrice"])
    except Exception as e:
        print(f"  BTC: {e}", file=sys.stderr)
    return None


# ═══════════════════════════════════════════
#  АГРЕГАЦИЯ + СОХРАНЕНИЕ
# ═══════════════════════════════════════════

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
        dex_tvl_total REAL, dex_ton_usdt_liquidity REAL, dex_pool_count INTEGER,
        -- Whales
        whale_txns_last_block INTEGER, total_txns_last_block INTEGER, whale_ratio REAL,
        -- Foundation
        ton_foundation_balance REAL, ton_bridge_balance REAL,
        -- BTC
        btc_price REAL,
        -- Расчётные метрики
        nvt_ratio REAL, fdv_to_mcap REAL, mcap_to_volume REAL,
        ton_btc_correlation REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_onchain_ts ON onchain(ts)")
    conn.commit()
    conn.close()


def save_metrics(m: Dict):
    conn = sqlite3.connect(str(DB))
    conn.execute("""INSERT INTO onchain (
        ts, price, market_cap, volume_24h, circulating_supply, total_supply,
        market_cap_rank, fdv, price_change_24h_pct,
        masterchain_seqno, blocks_per_minute,
        dex_tvl_total, dex_ton_usdt_liquidity, dex_pool_count,
        whale_txns_last_block, total_txns_last_block, whale_ratio,
        ton_foundation_balance, ton_bridge_balance,
        btc_price, nvt_ratio, fdv_to_mcap, mcap_to_volume, ton_btc_correlation
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        m.get("ts"), m.get("price"), m.get("market_cap"), m.get("volume_24h"),
        m.get("circulating_supply"), m.get("total_supply"),
        m.get("market_cap_rank"), m.get("fdv"), m.get("price_change_24h_pct"),
        m.get("masterchain_seqno"), m.get("blocks_per_minute"),
        m.get("dex_tvl_total"), m.get("dex_ton_usdt_liquidity"), m.get("dex_pool_count"),
        m.get("whale_txns_last_block"), m.get("total_txns_last_block"), m.get("whale_ratio"),
        m.get("ton_foundation_balance"), m.get("ton_bridge_balance"),
        m.get("btc_price"), m.get("nvt_ratio"), m.get("fdv_to_mcap"),
        m.get("mcap_to_volume"), m.get("ton_btc_correlation"),
    ))
    conn.commit()
    conn.close()


def calculate_correlation() -> Optional[float]:
    """Корреляция TON-BTC на последних 24 записях (24 часа)."""
    conn = sqlite3.connect(str(DB))
    # Check if column exists
    cols = [c[1] for c in conn.execute("PRAGMA table_info(onchain)").fetchall()]
    if "btc_price" not in cols:
        conn.close()
        return None
    rows = conn.execute(
        "SELECT price, btc_price FROM onchain WHERE price IS NOT NULL AND btc_price IS NOT NULL ORDER BY id DESC LIMIT 24"
    ).fetchall()
    conn.close()
    
    if len(rows) < 6:
        return None
    
    ton = [r[0] for r in rows]
    btc = [r[1] for r in rows]
    
    # Пирсон
    n = len(ton)
    avg_ton = sum(ton)/n
    avg_btc = sum(btc)/n
    
    cov = sum((ton[i]-avg_ton)*(btc[i]-avg_btc) for i in range(n))
    var_ton = sum((t-avg_ton)**2 for t in ton)
    var_btc = sum((b-avg_btc)**2 for b in btc)
    
    if var_ton == 0 or var_btc == 0:
        return None
    
    return round(cov / (var_ton**0.5 * var_btc**0.5), 3)


def aggregate(cg, tc, dex, whales, foundation, btc) -> Dict:
    now = datetime.now(UTC_PLUS_3).isoformat()
    m = {"ts": now}
    
    if cg:
        m.update({
            "price": cg["price_usd"], "market_cap": cg["market_cap"],
            "volume_24h": cg["volume_24h"], "circulating_supply": cg["circulating_supply"],
            "total_supply": cg["total_supply"], "market_cap_rank": cg["market_cap_rank"],
            "fdv": cg["fdv"], "price_change_24h_pct": cg["price_change_24h_pct"],
        })
    
    if tc:
        m.update(tc)
    
    if dex:
        m.update(dex)
    
    if whales:
        m.update(whales)
    
    if foundation:
        m.update(foundation)
    
    if btc:
        m["btc_price"] = btc
    
    # Расчётные метрики
    if m.get("market_cap") and m.get("volume_24h") and m["volume_24h"] > 0:
        m["nvt_ratio"] = round(m["market_cap"] / m["volume_24h"], 2)
        m["mcap_to_volume"] = round(m["market_cap"] / m["volume_24h"], 2)
    
    if m.get("fdv") and m.get("market_cap") and m["market_cap"] > 0:
        m["fdv_to_mcap"] = round(m["fdv"] / m["market_cap"], 2)
    
    m["ton_btc_correlation"] = calculate_correlation()
    
    return m


def print_report(m: Dict):
    """Краткий отчёт в консоль."""
    print(f"\n  ═══ TON ОНЧЕЙН-ОТЧЁТ {datetime.now(UTC_PLUS_3).strftime('%d.%m.%y %H:%M')} ═══")
    
    if m.get("price"):
        print(f"  💰 Цена: \${m['price']:.4f} ({m.get('price_change_24h_pct','?'):+.2f}%)")
    if m.get("market_cap"):
        print(f"  📊 Market Cap: \${m['market_cap']/1e9:.2f}B (#{m.get('market_cap_rank','?')})")
    if m.get("volume_24h"):
        print(f"  📈 Объём 24ч: \${m['volume_24h']/1e6:.0f}M")
    if m.get("nvt_ratio"):
        print(f"  ⚡ NVT Ratio: {m['nvt_ratio']:.1f} {'🟢' if m['nvt_ratio']<15 else '🟡' if m['nvt_ratio']<50 else '🔴'}")
    if m.get("fdv_to_mcap"):
        print(f"  🔓 FDV/MCap: {m['fdv_to_mcap']:.2f} (разлок)")
    if m.get("blocks_per_minute"):
        print(f"  ⛓️ Блоков/мин: {m['blocks_per_minute']}")
    
    if m.get("dex_tvl_total"):
        print(f"\n  💧 DEX TVL: \${m['dex_tvl_total']:.1f}M | TON/USDT: \${m.get('dex_ton_usdt_liquidity',0):.1f}M | Пулов: {m.get('dex_pool_count',0)}")
    
    if m.get("whale_ratio", 0) > 0:
        print(f"  🐋 Киты: {m['whale_txns_last_block']}/{m['total_txns_last_block']} транзакций ({m['whale_ratio']}%) в последнем блоке")
    
    if m.get("ton_foundation_balance"):
        print(f"  🏛️ Фонд: {m['ton_foundation_balance']:,.0f} TON | Мост: {m.get('ton_bridge_balance','?'):,} TON")
    
    if m.get("btc_price"):
        print(f"  ₿ BTC: \${m['btc_price']:,.0f}")
    if m.get("ton_btc_correlation") is not None:
        corr = m['ton_btc_correlation']
        print(f"  📐 Корреляция TON/BTC: {corr:+.2f} {'🟢 следом' if corr>0.5 else '🟡 слабая' if corr>0.2 else '🔴 независимо'}")


# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Сбор ончейн-данных...")
    
    cg = fetch_coingecko()
    tc = fetch_toncenter()
    dex = fetch_dex_metrics()
    whales = fetch_whale_activity()
    foundation = fetch_foundation_balance()
    btc = fetch_btc_price()
    
    m = aggregate(cg, tc, dex, whales, foundation, btc)
    save_metrics(m)
    
    # Save raw masterchain for next comparison
    if m.get("masterchain_seqno"):
        conn = sqlite3.connect(str(DB))
        conn.execute("INSERT INTO onchain_raw (ts, metric, value) VALUES (?,?,?)",
                     [m["ts"], "masterchain_seqno", str(m["masterchain_seqno"])])
        conn.commit()
        conn.close()
    
    print_report(m)
    print(f"\n  ✓ Сохранено в {DB.name}")
