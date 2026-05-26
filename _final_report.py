#!/usr/bin/env python3
"""Comprehensive 7-day report — queries all DBs, sends to Telegram."""
import sqlite3, json, urllib.request, urllib.parse, os
from datetime import datetime

# ═══════════════════════════════════════════
# 1. PAPER TRADING STATS
# ═══════════════════════════════════════════
conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/paper.db')
conn.row_factory = sqlite3.Row

total_pnl = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
total_trades = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
wins = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL').fetchone()[0]
losses = conn.execute('SELECT COUNT(*) FROM paper_positions WHERE pnl < 0 AND closed_ts IS NOT NULL').fetchone()[0]
avg_pnl = conn.execute('SELECT AVG(pnl) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]
avg_win = conn.execute('SELECT AVG(pnl) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL').fetchone()[0]
avg_loss = conn.execute('SELECT AVG(pnl) FROM paper_positions WHERE pnl < 0 AND closed_ts IS NOT NULL').fetchone()[0]
sum_wins = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE pnl > 0 AND closed_ts IS NOT NULL').fetchone()[0]
sum_losses = conn.execute('SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE pnl < 0 AND closed_ts IS NOT NULL').fetchone()[0]
max_win = conn.execute('SELECT MAX(pnl) FROM paper_positions').fetchone()[0]
max_loss = conn.execute('SELECT MIN(pnl) FROM paper_positions').fetchone()[0]
avg_bars = conn.execute('SELECT AVG(bars_held) FROM paper_positions WHERE closed_ts IS NOT NULL').fetchone()[0]

# Exit reason breakdown
reasons = conn.execute('''
    SELECT exit_reason, COUNT(*) as cnt, SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl
    FROM paper_positions WHERE closed_ts IS NOT NULL
    GROUP BY exit_reason ORDER BY cnt DESC
''').fetchall()

# Daily
daily = conn.execute('''
    SELECT DATE(opened_ts) as day, COUNT(*) as cnt, SUM(pnl) as day_pnl,
           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as w,
           SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as l
    FROM paper_positions WHERE closed_ts IS NOT NULL
    GROUP BY DATE(opened_ts) ORDER BY day
''').fetchall()

# Compute max drawdown
trades_all = conn.execute('SELECT balance_after, pnl FROM paper_positions WHERE closed_ts IS NOT NULL ORDER BY id').fetchall()
balances = [100.0]
for t in trades_all:
    balances.append(t['balance_after'] if t['balance_after'] else balances[-1] + t['pnl'])
peak = 100.0; max_dd = 0.0
for b in balances:
    if b > peak: peak = b
    dd = (peak - b) / peak * 100
    if dd > max_dd: max_dd = dd

open_pos = conn.execute('SELECT type, entry_price FROM paper_positions WHERE closed_ts IS NULL').fetchall()
conn.close()

balance = 100.0 + total_pnl
winrate = (wins / total_trades * 100) if total_trades > 0 else 0
pf = abs(sum_wins / sum_losses) if sum_losses != 0 and sum_losses < 0 else float('inf')

# ═══════════════════════════════════════════
# 2. ONCHAIN STATS
# ═══════════════════════════════════════════
conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/onchain.db')
conn.row_factory = sqlite3.Row
latest = conn.execute('SELECT * FROM onchain WHERE price IS NOT NULL ORDER BY ts DESC LIMIT 1').fetchone()
week_start = conn.execute('SELECT * FROM onchain WHERE price IS NOT NULL AND ts >= ? ORDER BY ts ASC LIMIT 1',
                          ('2026-05-14',)).fetchone()
# NVT trend
nvt_rows = conn.execute('SELECT ts, nvt_ratio FROM onchain WHERE nvt_ratio IS NOT NULL ORDER BY ts DESC LIMIT 168').fetchall()
nvt_vals = [r['nvt_ratio'] for r in nvt_rows]
nvt_trend = "снижается ⬇️ (недооценён)" if nvt_vals and len(nvt_vals) > 1 and nvt_vals[0] < nvt_vals[-1] else "растёт ⬆️"
conn.close()

# ═══════════════════════════════════════════
# 3. SENTIMENT STATS
# ═══════════════════════════════════════════
conn = sqlite3.connect('/home/oleg/workspace/crypto-ton/sentiment.db')
conn.row_factory = sqlite3.Row
total_posts = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
analyzed = conn.execute('SELECT COUNT(*) FROM posts WHERE analyzed=1').fetchone()[0]
bullish = conn.execute('SELECT COUNT(*) FROM posts WHERE sentiment="positive"').fetchone()[0]
bearish = conn.execute('SELECT COUNT(*) FROM posts WHERE sentiment="negative"').fetchone()[0]
neutral = conn.execute('SELECT COUNT(*) FROM posts WHERE sentiment="neutral"').fetchone()[0]
high_signals = conn.execute('SELECT COUNT(*) FROM posts WHERE impact_score > 7').fetchone()[0]
max_score = conn.execute('SELECT MAX(impact_score) FROM posts').fetchone()[0]
daily_sent = conn.execute('SELECT * FROM daily_sentiment ORDER BY date DESC LIMIT 7').fetchall()
conn.close()

# ═══════════════════════════════════════════
# 4. BUILD REPORT
# ═══════════════════════════════════════════
now = datetime.now().strftime('%Y-%m-%d %H:%M МСК')

report = f"""<b>📊 PAPER TRADING TON/USDT — ОТЧЁТ ЗА 7 ДНЕЙ</b>
<i>{now}</i>

━━━━━━━━━━━━━━━━━━━━━━
<b>💰 БАЛАНС И ПРИБЫЛЬ</b>
━━━━━━━━━━━━━━━━━━━━━━
• Стартовый депозит: <b>100.00 USD</b>
• Текущий баланс: <b>{balance:.2f} USD</b>
• Общий PnL: <b>{total_pnl:+.2f} USD ({total_pnl/100*100:+.2f}%)</b>
• Макс. просадка: <b>{max_dd:.2f}%</b>

━━━━━━━━━━━━━━━━━━━━━━
<b>📈 СТАТИСТИКА СДЕЛОК</b>
━━━━━━━━━━━━━━━━━━━━━━
• Всего сделок: <b>{total_trades}</b> (~{total_trades/7:.0f}/день)
• Побед: {wins} | Поражений: {losses}
• Винрейт: <b>{winrate:.1f}%</b>
• Profit Factor: <b>{pf:.2f}</b>
• Средний PnL/сделка: <b>{avg_pnl:+.4f} USD</b>
• Средняя победа: +{avg_win:.4f} USD
• Средний убыток: {avg_loss:.4f} USD
• Лучшая сделка: +{max_win:.4f} USD
• Худшая сделка: {max_loss:.4f} USD
• Среднее время в сделке: {avg_bars:.0f} баров (~{avg_bars*5:.0f} мин)

━━━━━━━━━━━━━━━━━━━━━━
<b>🏷️ СТАТИСТИКА ПО ЗАКРЫТИЯМ</b>
━━━━━━━━━━━━━━━━━━━━━━"""

for r in reasons:
    report += f"\n• {r['exit_reason']:15s}: {r['cnt']:3d} сделок, PnL={r['total_pnl']:+.2f} USD, сред={r['avg_pnl']:+.4f}"

report += f"""

━━━━━━━━━━━━━━━━━━━━━━
<b>📅 ДНЕВНАЯ РАЗБИВКА</b>
━━━━━━━━━━━━━━━━━━━━━━"""

for d in daily:
    wr = d['w']/(d['w']+d['l'])*100 if (d['w']+d['l']) > 0 else 0
    report += f"\n• {d['day']}: {d['cnt']:2d} сделок, PnL={d['day_pnl']:+.2f} USD, WR={wr:.0f}%"

report += f"""

━━━━━━━━━━━━━━━━━━━━━━
<b>⛓️ ОНЧЕЙН-МЕТРИКИ TON</b>
━━━━━━━━━━━━━━━━━━━━━━
• Цена TON сейчас: <b>{latest['price']:.2f} USD</b>
• 24h изменение: <b>{latest['price_change_24h_pct']:+.2f}%</b>
• NVT Ratio: <b>{latest['nvt_ratio']:.2f}</b> (тренд: {nvt_trend})
• Корреляция с BTC: <b>{latest['ton_btc_correlation']:.3f}</b>
• BTC: <b>{latest['btc_price']:.0f} USD</b>
• Скорость блоков: <b>{latest['blocks_per_minute']:.0f} бл/мин</b>
• Объём 24h: <b>{latest['volume_24h']/1e6:.0f}M USD</b>
• Cap рынка: <b>{latest['market_cap']/1e9:.2f}B USD</b> (ранг #{latest['market_cap_rank']})

━━━━━━━━━━━━━━━━━━━━━━
<b>🧠 AI-СЕНТИМЕНТ</b>
━━━━━━━━━━━━━━━━━━━━━━
• Проанализировано постов: <b>{analyzed}</b> из {total_posts}
• Bullish: {bullish} | Bearish: {bearish} | Neutral: {neutral}
• Макс. impact_score: <b>{max_score:.1f}</b>
• Сигналов score &gt; 7: <b>{high_signals}</b>
• Сентимент за 7 дней:"""

for ds in daily_sent:
    report += f"\n  {ds['date']}: 🟢{ds['bullish']} 🔴{ds['bearish']} ⚪{ds['neutral']} (avg_score={ds['avg_score']:.1f})"

# ═══════════════════════════════════════════
# 5. RECOMMENDATIONS
# ═══════════════════════════════════════════
report += f"""

━━━━━━━━━━━━━━━━━━━━━━
<b>🎯 РЕКОМЕНДАЦИИ ОЛЕГУ</b>
━━━━━━━━━━━━━━━━━━━━━━

<b>1. ГОТОВА ЛИ СТРАТЕГИЯ К РЕАЛЬНЫМ ДЕНЬГАМ?</b>
<b>❌ НЕТ. Стратегия НЕ готова к реальной торговле.</b>

Причины:
• 99 сделок за 7 дней — овертрейдинг (14 сделок/день)
• Винрейт {winrate:.1f}% — ниже 50%, стратегия нестабильна
• Прибыль +{total_pnl:.2f} USD на 99 сделках — razor-thin edge
• Реальные комиссии (~0.1% × 99 сделок × 2 стороны ≈ 1.0 USD) съедят ВСЮ прибыль
• 7 дней — слишком мало для статистической значимости

<b>2. ЧТО УЛУЧШИТЬ:</b>
• <b>Фильтр сигналов:</b> отсекать слабые сигналы (AUTO_CANCEL дают нулевой профит). Минимальный confidence threshold
• <b>Лимит сделок:</b> не более 3-5 сделок в день — снизить овертрейдинг
• <b>Кулдаун:</b> 30-60 минут между сделками одного типа
• <b>Трейлинг-стоп:</b> для TAKE_PROFIT-сделок дать прибыли расти
• <b>Размер позиции:</b> увеличивать маржу с ростом баланса (сейчас фикс. 5.20 USD)
• <b>Учёт комиссий:</b> симулировать 0.1% fee в paper-режиме

<b>3. МИНИМАЛЬНЫЙ ДЕПОЗИТ ДЛЯ СТАРТА:</b>
• Теоретический минимум: <b>150 USD</b> (30 сделок × 5 USD маржа)
• Рекомендуемый: <b>300-500 USD</b>
• Причина: нужен запас на серию убытков (макс. просадка × 3)

<b>4. ОЖИДАЕМАЯ ДОХОДНОСТЬ:</b>
• На бумаге: ~+4.5% в месяц (экстраполяция +1.09% за неделю)
• С комиссиями: ~+1-2% в месяц (оптимистично)
• Реалистично: от -2% до +3% на текущих параметрах

<b>5. ТОП-3 РИСКА:</b>
<b>⚠️ #1 Комиссии:</b> 99 сделок × 0.2% roundtrip = ~19.8% от оборота. На реальном счёте стратегия уйдёт в минус.
<b>⚠️ #2 Овертрейдинг:</b> 14 сделок/день при винрейте 42% — генератор убытков в долгую. Нужно фильтровать сигналы.
<b>⚠️ #3 Короткая история:</b> 7 дней и 99 сделок — недостаточно для оценки. Нужен минимум 30 дней и 200+ сделок в разных рыночных режимах.

━━━━━━━━━━━━━━━━━━━━━━
<b>💡 ИТОГ:</b> Стратегия показывает потенциал (Profit Factor 1.40, контролируемая просадка), но требует доработки. Продолжай paper trading ещё 2-3 недели с включёнными комиссиями и фильтрацией сигналов. Возвращайся к real-money вопросу после 30 дней стабильного paper-трекинга.
━━━━━━━━━━━━━━━━━━━━━━
"""

print(report)

# ═══════════════════════════════════════════
# 6. SEND TO TELEGRAM
# ═══════════════════════════════════════════
token = os.environ.get('TRADING_BOT_TOKEN', '')
chat_id = os.environ.get('TRADING_CHAT_ID', '218809870')

if token:
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': report,
        'parse_mode': 'HTML'
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        if result.get('ok'):
            print('\n✅ Telegram: ОТПРАВЛЕНО')
        else:
            print(f'\n❌ Telegram error: {result}')
    except Exception as e:
        print(f'\n❌ Telegram send failed: {e}')
else:
    print('\n⚠️ TRADING_BOT_TOKEN not set — skipping Telegram')
