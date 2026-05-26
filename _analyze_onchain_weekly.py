#!/usr/bin/env python3
"""Onchain weekly trends & complete onchain analysis."""
import sqlite3, json
from datetime import datetime

conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/onchain.db')
conn.row_factory = sqlite3.Row

# Weekly stats
rows = conn.execute('''
    SELECT ts, price, nvt_ratio, ton_btc_correlation, blocks_per_minute, 
           volume_24h, price_change_24h_pct, market_cap, btc_price
    FROM onchain WHERE price IS NOT NULL
    ORDER BY ts DESC LIMIT 200
''').fetchall()

print("=" * 70)
print("  ONCHAIN WEEKLY TRENDS (TON)")
print("=" * 70)
print(f"{'Timestamp':<28} {'Price':>7} {'NVT':>7} {'BTC Corr':>8} {'Blk/min':>8} {'24hVol_M':>10} {'24hChg%':>8} {'BTC':>10}")
print("-" * 95)

for r in reversed(rows):  # chronological
    ts = r['ts'].replace('T', ' ')[:19]
    nvt = f"{r['nvt_ratio']:.2f}" if r['nvt_ratio'] else "N/A"
    corr = f"{r['ton_btc_correlation']:.3f}" if r['ton_btc_correlation'] else "N/A"
    vol = f"{r['volume_24h']/1e6:.1f}" if r['volume_24h'] else "N/A"
    chg = f"{r['price_change_24h_pct']:+.2f}%" if r['price_change_24h_pct'] else "N/A"
    print(f"{ts:<28} {r['price'] or 'N/A':>7} {nvt:>7} {corr:>8} {r['blocks_per_minute'] or 'N/A':>8} {vol:>10} {chg:>8} {r['btc_price'] or 'N/A':>10}")

# Aggregate weekly
print()
print("=" * 70)
print("  WEEKLY AGGREGATES")
print("=" * 70)

# Price range
prices = [r['price'] for r in rows if r['price']]
nvt_vals = [r['nvt_ratio'] for r in rows if r['nvt_ratio']]
corr_vals = [r['ton_btc_correlation'] for r in rows if r['ton_btc_correlation']]
block_speeds = [r['blocks_per_minute'] for r in rows if r['blocks_per_minute']]
btc_prices = [r['btc_price'] for r in rows if r['btc_price']]

if prices:
    print(f"TON Price: min={min(prices):.4f}  max={max(prices):.4f}  current={prices[-1]:.4f}  range={max(prices)-min(prices):.4f} USD")
    print(f"TON 7d change: {(prices[-1]-prices[0])/prices[0]*100:+.2f}%" if len(prices)>1 else "N/A")
if nvt_vals:
    print(f"NVT Ratio: min={min(nvt_vals):.2f}  max={max(nvt_vals):.2f}  avg={sum(nvt_vals)/len(nvt_vals):.2f}  current={nvt_vals[-1]:.2f}")
    print(f"NVT Trend: {'📈 Rising (overvalued)' if nvt_vals[-1] > nvt_vals[0] else '📉 Declining (undervalued)'}")
if corr_vals:
    print(f"TON-BTC Correlation: min={min(corr_vals):.3f}  max={max(corr_vals):.3f}  avg={sum(corr_vals)/len(corr_vals):.3f}  current={corr_vals[-1]:.3f}")
if block_speeds:
    print(f"Block speed: min={min(block_speeds):.1f}  max={max(block_speeds):.1f}  avg={sum(block_speeds)/len(block_speeds):.1f} blk/min")
if btc_prices:
    print(f"BTC Price: min={min(btc_prices):.0f}  max={max(btc_prices):.0f}  current={btc_prices[-1]:.0f} USD")
    print(f"BTC 7d change: {(btc_prices[-1]-btc_prices[0])/btc_prices[0]*100:+.2f}%" if len(btc_prices)>1 else "N/A")

conn.close()
