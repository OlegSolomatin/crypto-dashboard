#!/usr/bin/env python3
"""Analyze onchain.db and sentiment.db"""
import sqlite3

# ===== ONCHAIN.DB =====
print("=" * 70)
print("  ONCHAIN DATABASE")
print("=" * 70)
try:
    conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/onchain.db')
    conn.row_factory = sqlite3.Row
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"Tables: {tables}")
    for t in tables:
        cols = conn.execute(f'PRAGMA table_info({t})').fetchall()
        print(f"\n{t}: columns={[(c[1], c[2]) for c in cols]}")
        cnt = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f"  Rows: {cnt}")
        # Show last 10 rows
        rows = conn.execute(f'SELECT * FROM {t} ORDER BY rowid DESC LIMIT 10').fetchall()
        for r in rows:
            print(f"  {dict(r)}")
    conn.close()
except Exception as e:
    print(f"ERROR: {e}")

# ===== SENTIMENT.DB =====
print()
print("=" * 70)
print("  SENTIMENT DATABASE")
print("=" * 70)
try:
    conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/sentiment.db')
    conn.row_factory = sqlite3.Row
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"Tables: {tables}")
    for t in tables:
        cols = conn.execute(f'PRAGMA table_info({t})').fetchall()
        print(f"\n{t}: columns={[(c[1], c[2]) for c in cols]}")
        cnt = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f"  Rows: {cnt}")
        rows = conn.execute(f'SELECT * FROM {t} ORDER BY rowid DESC LIMIT 20').fetchall()
        for r in rows:
            print(f"  {dict(r)}")
    conn.close()
except Exception as e:
    print(f"ERROR: {e}")
