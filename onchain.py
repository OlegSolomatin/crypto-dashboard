#!/usr/bin/env python3
"""
Ончейн-аналитика TON — сборщик метрик из 3 источников.
Запускается каждый час, сохраняет в onchain.db.

Источники:
  1. CoinGecko (фундаментал): market cap, volume, supply
  2. Toncenter (сеть): скорость блоков, активность
  3. Bybit (цена): синхронизация с monitor.py
"""

import sqlite3, json, urllib.request, time, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

DB = Path("/home/oleg/workspace/crypto-ton/onchain.db")
PRICES_DB = Path("/home/oleg/workspace/crypto-ton/data.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

# ==== 1. COINGECKO ====

def fetch_coingecko() -> Optional[Dict]:
    """Фундаментальные метрики: market cap, FDV, тренды."""
    try:
        url = ("https://api.coingecko.com/api/v3/coins/the-open-network"
               "?localization=false&tickers=false&community_data=false"
               "&developer_data=false")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        
        market = data.get("market_data", {})
        return {
            "price_usd": market.get("current_price", {}).get("usd"),
            "market_cap": market.get("market_cap", {}).get("usd"),
            "total_volume_24h": market.get("total_volume", {}).get("usd"),
            "high_24h": market.get("high_24h", {}).get("usd"),
            "low_24h": market.get("low_24h", {}).get("usd"),
            "price_change_24h_pct": market.get("price_change_percentage_24h"),
            "circulating_supply": market.get("circulating_supply"),
            "total_supply": market.get("total_supply"),
            "market_cap_rank": data.get("market_cap_rank"),
            "fdv": market.get("fully_diluted_valuation", {}).get("usd"),
            # Расчётные метрики
            "mcap_to_volume": (market.get("market_cap", {}).get("usd", 1) / 
                              max(market.get("total_volume", {}).get("usd", 1), 1)),
            "ath_pct": market.get("price_change_percentage_24h"),  # заменим на ath позже
        }
    except Exception as e:
        print(f"  CoinGecko error: {e}", file=sys.stderr)
        return None


# ==== 2. TONCENTER ====

def fetch_toncenter() -> Optional[Dict]:
    """Сетевые метрики: скорость блоков, шарды."""
    try:
        # Masterchain info
        req = urllib.request.Request(
            "https://toncenter.com/api/v2/getMasterchainInfo",
            headers={"User-Agent": "Hermes/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            mc = json.loads(r.read())
        
        if not mc.get("ok"):
            return None
        
        last = mc["result"]["last"]
        current_seqno = last["seqno"]
        
        # Compare with previous reading (1 hour ago)
        prev_seqno = None
        try:
            conn = sqlite3.connect(str(DB))
            row = conn.execute(
                "SELECT value FROM onchain_raw WHERE metric='masterchain_seqno' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row:
                prev_seqno = int(row[0])
            conn.close()
        except:
            pass
        
        blocks_per_minute = None
        if prev_seqno and current_seqno > prev_seqno:
            blocks_per_minute = (current_seqno - prev_seqno) / 60  # ~60 min between reads
        
        return {
            "masterchain_seqno": current_seqno,
            "blocks_per_minute": round(blocks_per_minute, 1) if blocks_per_minute else None,
            "workchain": last["workchain"],
            "shard": last["shard"],
        }
    except Exception as e:
        print(f"  Toncenter error: {e}", file=sys.stderr)
        return None


# ==== 3. АГРЕГАЦИЯ ====

def aggregate(cg: Optional[Dict], tc: Optional[Dict]) -> Dict:
    """Объединить метрики в одну запись."""
    result = {
        "ts": datetime.now(UTC_PLUS_3).isoformat(),
    }
    
    if cg:
        result.update({
            "price": cg["price_usd"],
            "market_cap": cg["market_cap"],
            "volume_24h": cg["total_volume_24h"],
            "circulating_supply": cg["circulating_supply"],
            "total_supply": cg["total_supply"],
            "market_cap_rank": cg["market_cap_rank"],
            "fdv": cg["fdv"],
            "price_change_24h_pct": cg["price_change_24h_pct"],
            "mcap_to_volume": cg["mcap_to_volume"],
        })
    
    if tc:
        result.update({
            "masterchain_seqno": tc["masterchain_seqno"],
            "blocks_per_minute": tc["blocks_per_minute"],
        })
    
    # Расчётные метрики
    if result.get("fdv") and result.get("total_supply"):
        result["fdv_to_mcap"] = round(result["fdv"] / max(result.get("market_cap", 1), 1), 2)
    
    if result.get("market_cap") and result.get("circulating_supply"):
        result["nvt_ratio"] = round(result["market_cap"] / max(result.get("volume_24h", 1), 1), 2)
    
    return result


# ==== 4. СОХРАНЕНИЕ ====

def init_db():
    conn = sqlite3.connect(str(DB))
    
    # Основная таблица ончейн-метрик
    conn.execute("""
        CREATE TABLE IF NOT EXISTS onchain (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            price REAL,
            market_cap REAL,
            volume_24h REAL,
            circulating_supply REAL,
            total_supply REAL,
            market_cap_rank INTEGER,
            fdv REAL,
            fdv_to_mcap REAL,
            price_change_24h_pct REAL,
            mcap_to_volume REAL,
            nvt_ratio REAL,
            masterchain_seqno INTEGER,
            blocks_per_minute REAL
        )
    """)
    
    # Индексы
    conn.execute("CREATE INDEX IF NOT EXISTS idx_onchain_ts ON onchain(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_onchain_mcap ON onchain(market_cap)")
    
    # Raw-кэш для сравнения (masterchain seqno между запусками)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS onchain_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            metric TEXT NOT NULL,
            value TEXT NOT NULL
        )
    """)
    
    conn.commit()
    conn.close()


def save_metrics(m: Dict):
    conn = sqlite3.connect(str(DB))
    conn.execute("""
        INSERT INTO onchain (ts, price, market_cap, volume_24h, circulating_supply,
            total_supply, market_cap_rank, fdv, fdv_to_mcap, price_change_24h_pct,
            mcap_to_volume, nvt_ratio, masterchain_seqno, blocks_per_minute)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        m.get("ts"), m.get("price"), m.get("market_cap"), m.get("volume_24h"),
        m.get("circulating_supply"), m.get("total_supply"), m.get("market_cap_rank"),
        m.get("fdv"), m.get("fdv_to_mcap"), m.get("price_change_24h_pct"),
        m.get("mcap_to_volume"), m.get("nvt_ratio"),
        m.get("masterchain_seqno"), m.get("blocks_per_minute"),
    ))
    
    # Save raw for next comparison
    if m.get("masterchain_seqno"):
        conn.execute(
            "INSERT INTO onchain_raw (ts, metric, value) VALUES (?,?,?)",
            (m["ts"], "masterchain_seqno", str(m["masterchain_seqno"]))
        )
    
    conn.commit()
    conn.close()


def analyze_anomalies():
    """Сравнить последние метрики с предыдущими, найти аномалии."""
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT * FROM onchain ORDER BY id DESC LIMIT 2"
    ).fetchall()
    conn.close()
    
    if len(rows) < 2:
        return []
    
    cols = ["id","ts","price","market_cap","volume_24h","circulating_supply",
            "total_supply","market_cap_rank","fdv","fdv_to_mcap",
            "price_change_24h_pct","mcap_to_volume","nvt_ratio",
            "masterchain_seqno","blocks_per_minute"]
    
    prev = dict(zip(cols, rows[1]))
    curr = dict(zip(cols, rows[0]))
    
    alerts = []
    
    # Объём вырос в 2 раза
    if curr["volume_24h"] and prev["volume_24h"]:
        vol_change = curr["volume_24h"] / prev["volume_24h"]
        if vol_change > 2.0:
            alerts.append(f"📊 Объём ×{vol_change:.1f} — аномальный всплеск активности")
    
    # Market cap изменился >5% за час
    if curr["market_cap"] and prev["market_cap"]:
        mcap_change = abs(curr["market_cap"] - prev["market_cap"]) / prev["market_cap"]
        if mcap_change > 0.05:
            alerts.append(f"💰 Капитализация изменилась на {mcap_change*100:+.1f}%")
    
    # NVT Ratio (аналог P/E для крипты)
    if curr["nvt_ratio"] and curr["nvt_ratio"] > 100:
        alerts.append(f"📈 NVT={curr['nvt_ratio']:.0f} — сеть переоценена относительно объёма транзакций")
    elif curr["nvt_ratio"] and curr["nvt_ratio"] < 10:
        alerts.append(f"📉 NVT={curr['nvt_ratio']:.0f} — сеть недооценена, высокая активность")
    
    return alerts


# ==== MAIN ====

if __name__ == "__main__":
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] Ончейн-сборщик TON")
    
    init_db()
    
    cg_data = fetch_coingecko()
    tc_data = fetch_toncenter()
    
    m = aggregate(cg_data, tc_data)
    save_metrics(m)
    
    # Вывод
    if m.get("price"):
        print(f"  Цена: \${m['price']:.4f} ({m.get('price_change_24h_pct','?'):+.2f}%)")
    if m.get("market_cap"):
        print(f"  Market Cap: \${m['market_cap']/1e9:.2f}B | Ранг #{m.get('market_cap_rank','?')}")
    if m.get("volume_24h"):
        print(f"  Объём 24ч: \${m['volume_24h']/1e6:.0f}M")
    if m.get("blocks_per_minute"):
        print(f"  Блоков/мин: {m['blocks_per_minute']}")
    if m.get("nvt_ratio"):
        print(f"  NVT Ratio: {m['nvt_ratio']:.1f}")
    if m.get("fdv_to_mcap"):
        print(f"  FDV/MCap: {m['fdv_to_mcap']:.2f}")
    
    alerts = analyze_anomalies()
    if alerts:
        print(f"\n  ⚠️ Аномалии:")
        for a in alerts:
            print(f"    {a}")
    
    print(f"  ✓ Сохранено в {DB.name}")
