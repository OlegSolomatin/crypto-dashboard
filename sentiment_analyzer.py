#!/usr/bin/env python3
"""
AI-аналитик: анализирует посты на влияние цены TON.
Использует OpenRouter (бесплатная модель) для оценки каждого поста.

Анализирует:
  - Тональность (позитив/негатив/нейтрально)
  - Влияние на цену (1–10)
  - Тип новости (listing/update/partnership/hack/regulation/other)
  - Обоснование (1 предложение)

При score > 5 → алерт в трейдинг-бота.
При score > 7 → также алерт в learning.db для сигнальщика.

Использование:
  python3 sentiment_analyzer.py           # проанализировать новые посты
  python3 sentiment_analyzer.py --report  # показать последние оценки
"""

import sqlite3, json, urllib.request, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict

SENTIMENT_DB = Path("/home/oleg/workspace/crypto-ton/sentiment.db")
UTC_PLUS_3 = timezone(timedelta(hours=3))
BATCH_SIZE = 5  # постов за один API-вызов

# OpenRouter
OPENROUTER_KEY = ""
with open(Path.home() / ".hermes/.env") as f:
    for line in f:
        if line.startswith("OPENROUTER_API_KEY="):
            OPENROUTER_KEY = line.split("=", 1)[1].strip()
            break

# Бесплатная модель для анализа
ANALYST_MODEL = "google/gemini-2.0-flash-001"  # есть на OpenRouter, vision не нужен


def call_llm(posts: List[Dict]) -> Optional[List[Dict]]:
    """Отправить посты на анализ через OpenRouter."""
    if not OPENROUTER_KEY:
        print("  OpenRouter key не найден", file=sys.stderr)
        return None
    
    # Формируем промпт
    posts_text = ""
    for i, p in enumerate(posts, 1):
        src = p["source"].replace("twitter_", "@").replace("tg_", "@").replace("reg_", "🏛️ @")
        posts_text += f"[{i}] ИСТОЧНИК: {src}\nТЕКСТ: {p['content'][:500]}\n\n"
    
    prompt = f"""Ты — крипто-аналитик. Оцени влияние каждого поста на цену TON/USDT.

Для каждого поста верни JSON-массив объектов:
[
  {{
    "id": номер_поста,
    "sentiment": "positive" / "negative" / "neutral",
    "impact_score": число от 1 до 10 (1=никак, 10=сильно изменит цену),
    "news_type": "listing" / "update" / "partnership" / "hack" / "regulation" / "monetary_policy" / "adoption" / "other",
    "reasoning": "1 предложение почему"
  }}
]

ШКАЛА ВЛИЯНИЯ:
- Score 1-3: рутинные новости, мемы, репосты
- Score 4-6: анонсы фич, небольшие партнёрства, общие заявления регуляторов
- Score 7-8: крупные апдейты, листинги, рост TVL, решения ЦБ по ставкам
- Score 9-10: форки, взломы, запреты крипты, экстренные заявления ФРС/ЕЦБ

ОСОБОЕ ВНИМАНИЕ к источникам с префиксом 🏛️ (регуляторы):
- Заявления глав ЦБ (ФРС, ЕЦБ, Банк Англии) о ставках → impact_score +2 к базовой оценке
- Новости SEC/CFTC о запрете/одобрении криптовалют → impact_score 8-10
- Предупреждения о рисках, новые правила KYC/AML → impact_score 5-7
- Рутинные отчёты, статистика → impact_score 1-3

Посты для анализа:
{posts_text}"""
    
    try:
        data = json.dumps({
            "model": ANALYST_MODEL,
            "messages": [
                {"role": "system", "content": "Ты — крипто-аналитик. Отвечай ТОЛЬКО валидным JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 1000,
        }).encode()
        
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "TON AI Analyst",
            }
        )
        
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        
        content = resp["choices"][0]["message"]["content"]
        
        # Извлекаем JSON
        json_start = content.find("[")
        json_end = content.rfind("]") + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(content[json_start:json_end])
        
        print(f"  Не удалось извлечь JSON: {content[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  LLM error: {e}", file=sys.stderr)
        return None


def analyze_posts(limit: int = 15) -> int:
    """Проанализировать неразобранные посты."""
    conn = sqlite3.connect(str(SENTIMENT_DB))
    rows = conn.execute(
        "SELECT id, source, content, fetched_ts FROM posts WHERE analyzed=0 ORDER BY id ASC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    
    if not rows:
        print("  Нет новых постов для анализа")
        return 0
    
    posts = [{"id": r[0], "source": r[1], "content": r[2], "ts": r[3]} for r in rows]
    analyzed = 0
    alerts = []
    
    # Анализируем батчами
    for i in range(0, len(posts), BATCH_SIZE):
        batch = posts[i:i+BATCH_SIZE]
        
        results = call_llm(batch)
        if not results:
            continue
        
        conn = sqlite3.connect(str(SENTIMENT_DB))
        for r in results:
            post_id = r.get("id")
            # Найти настоящий id (может быть смещён из-за батча)
            real_id = batch[r["id"] - 1]["id"] if isinstance(r["id"], int) and 1 <= r["id"] <= len(batch) else None
            if not real_id:
                continue
            
            sentiment = str(r.get("sentiment", "neutral"))[:20]
            score = float(r.get("impact_score", 0))
            news_type = str(r.get("news_type", "other"))[:30]
            reasoning = str(r.get("reasoning", ""))[:300]
            
            conn.execute("""
                UPDATE posts SET analyzed=1, sentiment=?, impact_score=?, reasoning=?
                WHERE id=?
            """, (sentiment, score, f"{news_type}: {reasoning}", real_id))
            
            analyzed += 1
            
            # Собираем алерты
            if score >= 5:
                src = batch[r["id"]-1]["source"].replace("twitter_", "🐦 ").replace("tg_", "📱 ")
                content_preview = batch[r["id"]-1]["content"][:120]
                alerts.append({
                    "source": src,
                    "score": score,
                    "sentiment": sentiment,
                    "type": news_type,
                    "reasoning": reasoning,
                    "preview": content_preview,
                })
        
        conn.commit()
        conn.close()
        time.sleep(1)  # не спамим API
    
    # Отправляем алерты
    if alerts:
        alerts.sort(key=lambda x: x["score"], reverse=True)
        for a in alerts[:5]:  # топ-5
            send_alert(a)
    
    # Сохраняем метрику
    if analyzed > 0:
        conn = sqlite3.connect(str(SENTIMENT_DB))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_sentiment (
                date TEXT PRIMARY KEY,
                bullish INTEGER DEFAULT 0,
                bearish INTEGER DEFAULT 0,
                neutral INTEGER DEFAULT 0,
                avg_score REAL DEFAULT 0
            )
        """)
        today = datetime.now(UTC_PLUS_3).strftime('%Y-%m-%d')
        stats = conn.execute("""
            SELECT 
                SUM(CASE WHEN sentiment='positive' THEN 1 ELSE 0 END),
                SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END),
                SUM(CASE WHEN sentiment='neutral' THEN 1 ELSE 0 END),
                AVG(impact_score)
            FROM posts WHERE date(fetched_ts)=?
        """, (today,)).fetchone()
        if stats and stats[0]:
            conn.execute("""
                INSERT INTO daily_sentiment (date, bullish, bearish, neutral, avg_score)
                VALUES (?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                bullish=excluded.bullish, bearish=excluded.bearish,
                neutral=excluded.neutral, avg_score=excluded.avg_score
            """, (today, stats[0], stats[1], stats[2], round(stats[3], 2)))
        conn.commit()
        conn.close()
    
    return analyzed


def send_alert(alert: Dict):
    """Отправить важный пост в трейдинг-бота."""
    token = os.getenv("TRADING_BOT_TOKEN", "").strip()
    chat = os.getenv("TRADING_CHAT_ID", "").strip()
    if not token or not chat:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token or not chat:
        return
    
    emoji = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(alert["sentiment"], "⚪")
    impact_bar = "█" * int(alert["score"]) + "░" * (10 - int(alert["score"]))
    
    msg = (
        f"{emoji} <b>AI-АНАЛИТИК | Влияние на TON: {alert['score']}/10</b>\n"
        f"{impact_bar}\n\n"
        f"Источник: {alert['source']}\n"
        f"Тип: {alert['type']}\n"
        f"Тон: {alert['sentiment']}\n\n"
        f"💬 {alert['preview']}\n\n"
        f"🧠 <i>{alert['reasoning']}</i>\n\n"
        f"{datetime.now(UTC_PLUS_3).strftime('%d.%m.%Y %H:%M')} МСК"
    )
    
    try:
        d = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=d), timeout=10)
    except:
        pass


def report():
    """Показать статистику."""
    conn = sqlite3.connect(str(SENTIMENT_DB))
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM posts WHERE analyzed=1").fetchone()[0]
    
    print(f"Постов: {total} | Проанализировано: {analyzed}")
    
    if analyzed > 0:
        stats = conn.execute("""
            SELECT sentiment, COUNT(*), ROUND(AVG(impact_score),1)
            FROM posts WHERE analyzed=1 GROUP BY sentiment ORDER BY COUNT(*) DESC
        """).fetchall()
        print(f"\nТональность:")
        for s in stats:
            print(f"  {s[0]:10s}: {s[1]} постов (средний score: {s[2]})")
        
        top = conn.execute("""
            SELECT source, sentiment, impact_score, reasoning[:80], content[:60]
            FROM posts WHERE analyzed=1 AND impact_score >= 5
            ORDER BY impact_score DESC LIMIT 5
        """).fetchall()
        if top:
            print(f"\nТоп-5 по влиянию:")
            for t in top:
                src = t[0].replace("twitter_", "🐦").replace("tg_", "📱")
                print(f"  {src} | {t[1]:8s} | score={t[2]} | {t[3]}...")
    
    conn.close()


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
        sys.exit(0)
    
    print(f"[{datetime.now(UTC_PLUS_3).strftime('%H:%M:%S')}] AI-анализ постов...")
    
    # Сначала собираем новые посты
    import sentiment_collector
    sentiment_collector.init_db()
    
    total_saved = 0
    for source_name, url in sentiment_collector.SOURCES.items():
        if source_name.startswith("twitter"):
            posts = sentiment_collector.fetch_rss(url)
            saved = sentiment_collector.save_posts(posts, source_name, "twitter")
            total_saved += saved
        elif source_name.startswith("tg"):
            channel = source_name.replace("tg_", "@")
            posts = sentiment_collector.fetch_telegram(url, channel)
            saved = sentiment_collector.save_posts(posts, source_name, "telegram")
            total_saved += saved
    
    if total_saved > 0:
        print(f"  📡 Собрано: +{total_saved} новых постов")
    
    # Анализируем
    n = analyze_posts(limit=15)
    print(f"  🧠 Проанализировано: {n} постов")
