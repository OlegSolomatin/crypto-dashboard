#!/usr/bin/env python3
"""Быстрый сбор регуляторов (по 1 RSS за вызов, чтобы не таймаутить)."""
import sys, sqlite3, hashlib, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

DB = Path("/home/oleg/workspace/crypto-ton/sentiment.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

REGULATORS = {
    "reg_federalreserve": "https://nitter.net/federalreserve/rss",
    "reg_sec": "https://nitter.net/SECGov/rss",
    "reg_cftc": "https://nitter.net/CFTC/rss",
    "reg_fdic": "https://nitter.net/FDICgov/rss",
    "reg_finra": "https://nitter.net/FINRA/rss",
    "reg_cftc_news": "https://nitter.net/CFTC_news/rss",
    "reg_ecb": "https://nitter.net/ecb/rss",
    "reg_bankofengland": "https://nitter.net/bankofengland/rss",
    "reg_bundesbank": "https://nitter.net/Bundesbank/rss",
    "reg_banquedefrance": "https://nitter.net/banquedefrance/rss",
    "reg_boj": "https://nitter.net/BOJ_News/rss",
    "reg_bis": "https://nitter.net/BIS_org/rss",
    "reg_oecd": "https://nitter.net/OECD/rss",
}


def fetch_one(name, url):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Accept': 'application/rss+xml'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())

        conn = sqlite3.connect(str(DB))
        now = datetime.now(UTC_PLUS_3).isoformat()
        saved = 0
        for item in root.findall('.//item')[:5]:
            title = (item.find('title').text or '') if item.find('title') is not None else ''
            desc = (item.find('description').text or '') if item.find('description') is not None else ''
            full = f"{title} {desc}".strip()[:2000]
            if not full:
                continue
            ch = hashlib.sha256(full.encode()).hexdigest()[:16]
            ex = conn.execute("SELECT id FROM posts WHERE content_hash=?", (ch,)).fetchone()
            if not ex:
                conn.execute(
                    "INSERT INTO posts(source,source_type,author,url,content_hash,content,fetched_ts) VALUES(?,?,?,?,?,?,?)",
                    (name, 'twitter', name.replace('reg_', ''), '', ch, full, now))
                saved += 1
        conn.commit()
        conn.close()
        return saved
    except Exception as e:
        return f"ERR: {e}"


if __name__ == "__main__":
    total = 0
    for name, url in REGULATORS.items():
        result = fetch_one(name, url)
        if isinstance(result, int):
            if result > 0:
                print(f"  🏛️ {name}: +{result}")
                total += result
        else:
            print(f"  ❌ {name}: {result}")

    # Stats
    conn = sqlite3.connect(str(DB))
    reg_count = conn.execute("SELECT COUNT(*) FROM posts WHERE source LIKE 'reg_%'").fetchone()[0]
    total_all = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    conn.close()
    print(f"\n  Регуляторов: {reg_count} | Всего постов: {total_all}")
