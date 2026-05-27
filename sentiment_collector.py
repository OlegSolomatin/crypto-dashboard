#!/usr/bin/env python3
"""
Сборщик постов для AI-аналитика.
Собирает последние посты из Twitter (Nitter RSS) + Telegram каналов.
Сохраняет в sentiment.db для последующего LLM-анализа.

Источники (все бесплатные):
  Twitter (через Nitter): @ton_blockchain, @durov, @tonkeeper, @ston_fi, @dedust_io
  Telegram: @toncoin_rus, @tondev_news, @durov

Использование:
  python3 sentiment_collector.py          # собрать новые посты
  python3 sentiment_collector.py --stats  # показать статистику
"""

import sqlite3, json, urllib.request, hashlib, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict
import xml.etree.ElementTree as ET

SENTIMENT_DB = Path("/home/oleg/workspace/crypto-ton/sentiment.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))

SOURCES = {
    # === TON экосистема (Twitter через Nitter RSS) ===
    "twitter_ton_blockchain": "https://nitter.net/ton_blockchain/rss",
    "twitter_durov": "https://nitter.net/durov/rss",
    "twitter_tonkeeper": "https://nitter.net/tonkeeper/rss",
    "twitter_ston_fi": "https://nitter.net/ston_fi/rss",
    "twitter_dedust_io": "https://nitter.net/dedust_io/rss",
    
    # === Telegram публичные каналы ===
    "tg_toncoin_rus": "https://t.me/s/toncoin_rus",
    "tg_ton_dev": "https://t.me/s/tondev_news",
    "tg_durov": "https://t.me/s/durov",
    
    # === ФИНАНСОВЫЕ РЕГУЛЯТОРЫ (Twitter RSS) ===
    # США
    "reg_federalreserve": "https://nitter.net/federalreserve/rss",
    "reg_sec": "https://nitter.net/SECGov/rss",
    "reg_cftc": "https://nitter.net/CFTC/rss",
    "reg_fdic": "https://nitter.net/FDICgov/rss",
    "reg_finra": "https://nitter.net/FINRA/rss",
    "reg_cftc_news": "https://nitter.net/CFTC_news/rss",
    
    # Европа
    "reg_ecb": "https://nitter.net/ecb/rss",
    "reg_bankofengland": "https://nitter.net/bankofengland/rss",
    "reg_bundesbank": "https://nitter.net/Bundesbank/rss",
    "reg_banquedefrance": "https://nitter.net/banquedefrance/rss",
    
    # Азия
    "reg_boj": "https://nitter.net/BOJ_News/rss",
    
    # Международные
    "reg_bis": "https://nitter.net/BIS_org/rss",
    "reg_oecd": "https://nitter.net/OECD/rss",
}


def init_db():
    conn = sqlite3.connect(str(SENTIMENT_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        source_type TEXT NOT NULL,  -- 'twitter' or 'telegram'
        author TEXT,
        url TEXT,
        content_hash TEXT UNIQUE,
        content TEXT,
        fetched_ts TEXT,
        analyzed INTEGER DEFAULT 0,
        sentiment TEXT,
        impact_score REAL,
        reasoning TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_hash ON posts(content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_analyzed ON posts(analyzed)")
    conn.commit()
    conn.close()


def fetch_rss(url: str) -> List[Dict]:
    """Собрать посты из RSS-ленты Nitter."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept': 'application/rss+xml'
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            root = ET.fromstring(r.read())
        
        posts = []
        for item in root.findall('.//item'):
            title = (item.find('title').text or '') if item.find('title') is not None else ''
            desc = (item.find('description').text or '') if item.find('description') is not None else ''
            link = (item.find('link').text or '') if item.find('link') is not None else ''
            author = (item.find('.//{http://purl.org/dc/elements/1.1/}creator').text or '') \
                     if item.find('.//{http://purl.org/dc/elements/1.1/}creator') is not None else ''
            pubdate = (item.find('pubDate').text or '') if item.find('pubDate') is not None else ''
            
            full = f"{title}\n{desc}".strip()
            if not full:
                continue
            
            posts.append({
                "content": full,
                "url": link,
                "author": author,
                "date": pubdate,
            })
        
        return posts
    except Exception as e:
        print(f"  RSS error {url[-30:]}: {e}", file=sys.stderr)
        return []


def fetch_telegram(url: str, channel: str) -> List[Dict]:
    """Собрать посты из публичного Telegram-канала."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode()
        
        posts = []
        # Ищем блоки сообщений
        msg_pattern = re.compile(
            r'<div class="tgme_widget_message_wrap[^"]*".*?'
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>.*?'
            r'<a class="tgme_widget_message_date[^"]*" href="([^"]+)"',
            re.DOTALL
        )
        
        # Альтернативный парсинг
        blocks = re.findall(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            html,
            re.DOTALL
        )
        links = re.findall(
            r'<a class="tgme_widget_message_date[^"]*" href="([^"]+)"',
            html
        )
        
        for i, block in enumerate(blocks):
            text = re.sub(r'<[^>]+>', '', block).strip()
            text = re.sub(r'\s+', ' ', text)
            if not text or len(text) < 15:
                continue
            
            link = links[i] if i < len(links) else url
            posts.append({
                "content": text,
                "url": link,
                "author": channel,
                "date": "",
            })
        
        return posts
    except Exception as e:
        print(f"  TG error {channel}: {e}", file=sys.stderr)
        return []


def save_posts(posts: List[Dict], source: str, source_type: str) -> int:
    """Сохранить новые посты в БД, избегая дубликатов."""
    conn = sqlite3.connect(str(SENTIMENT_DB))
    now = datetime.now(UTC_PLUS_3).isoformat()
    saved = 0
    
    for p in posts:
        content = p["content"][:2000]  # обрезаем слишком длинные
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        
        # Проверяем дубликат
        existing = conn.execute(
            "SELECT id FROM posts WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if existing:
            continue
        
        try:
            conn.execute("""
                INSERT INTO posts (source, source_type, author, url, content_hash, content, fetched_ts)
                VALUES (?,?,?,?,?,?,?)
            """, (source, source_type, p["author"], p["url"], content_hash, content, now))
            saved += 1
        except sqlite3.IntegrityError:
            pass
    
    conn.commit()
    conn.close()
    return saved


def print_stats():
    conn = sqlite3.connect(str(SENTIMENT_DB))
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1").fetchone()[0]
    sources = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM posts GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    
    # Recent unanalyzed
    recent = conn.execute(
        "SELECT source, content[:80], fetched_ts FROM posts WHERE analyzed=0 ORDER BY id DESC LIMIT 5"
    ).fetchall()
    conn.close()
    
    print(f"Всего постов: {total} | Проанализировано: {analyzed} | Ждут анализа: {total-analyzed}")
    print(f"\nИсточники:")
    for src, cnt in sources:
        print(f"  {src}: {cnt}")
    if recent:
        print(f"\nПоследние неразобранные:")
        for src, content, ts in recent:
            print(f"  [{src}] {content}...")


if __name__ == "__main__":
    init_db()
    
    if "--stats" in sys.argv:
        print_stats()
        sys.exit(0)
    
    now = datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')
    print(f"[{now}] Сбор постов...")
    
    total_saved = 0
    for source_name, url in SOURCES.items():
        if source_name.startswith("twitter"):
            posts = fetch_rss(url)
            saved = save_posts(posts, source_name, "twitter")
            if saved > 0:
                print(f"  ✅ {source_name}: +{saved} постов")
                total_saved += saved
        elif source_name.startswith("tg"):
            channel = source_name.replace("tg_", "@")
            posts = fetch_telegram(url, channel)
            saved = save_posts(posts, source_name, "telegram")
            if saved > 0:
                print(f"  ✅ {source_name}: +{saved} постов")
                total_saved += saved
    
    print(f"  📊 Всего новых: {total_saved}")
    print_stats()
