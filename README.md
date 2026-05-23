# 🧪 Crypto Trading Dashboard

Торговый дашборд для TON/USDT с бэктестером стратегий, графиками, сигналами и лайв-трейдингом.

## 📁 Структура

```
crypto-dashboard/
├── frontend/          # Статические файлы (HTML/CSS/JS)
│   ├── dashboard.html      # Основной дашборд с графиками
│   ├── backtest.html       # Бэктестер стратегий (9-шаговый визард)
│   ├── telegram-mini-app.html  # Лендинг с плитками
│   ├── index.html          # SPA (React)
│   ├── theme.js            # Переключатель темы (тёмная/системная/светлая)
│   ├── lwcharts.js         # Библиотека графиков (Lightweight Charts)
│   ├── assets/             # Ресурсы React-приложения
│   └── fonts/              # Шрифты
├── backend/           # Python-сервер
│   ├── dashboard_api.py    # API сервер (:8889) — данные, сигналы, бэктесты
│   └── proxy_server.py     # Прокси-сервер (:8789) — статика + роутинг
└── backtests/         # Результаты бэктестов (создаётся автоматически)
```

## 🚀 Быстрый старт

### Зависимости

Только Python 3.9+ (стандартная библиотека). Никаких pip-пакетов.

```bash
python3 --version  # >= 3.9
```

### Запуск

```bash
# 1. Dashboard API (порт 8889)
cd backend
python3 dashboard_api.py &

# 2. Прокси-сервер (порт 8789)
python3 proxy_server.py &

# 3. Открыть в браузере
# http://localhost:8789/          — главная
# http://localhost:8789/dashboard — дашборд
# http://localhost:8789/backtest  — бэктестер
```

### Автозапуск (cron @reboot)

```bash
crontab -e
```

Добавить:

```
@reboot sleep 10 && cd /home/user/crypto-dashboard/backend && python3 dashboard_api.py &
@reboot sleep 12 && cd /home/user/crypto-dashboard/backend && python3 proxy_server.py &
```

## 📊 Бэктестер

9-шаговый визард для тестирования стратегий:

1. **Режим** — исторические данные / реальные (Live)
2. **Стратегия** — RSI / SWING / RSI Трейлинг-стоп
3. **Пара** — TON, BTC, ETH, SOL, DOGE, XRP, SUI, NOT
4. **Таймфрейм** — 1m, 5m, 15m, 30m, 1h, 4h, 1d
5. **Плечо** — 1×, 2×, 3×, 5×, 10×
6. **Баланс** — $100–$100,000
7. **Сумма сделки** — размер позиции в USDT
8. **Период** — 1–30 дней
9. **Подтверждение** — сводка + запуск

Данные свечей берутся с **Binance Public API** (не требует ключей).

## 🎨 Темы

Три режима (переключатель в хедере):
- 🌙 Тёмная
- 💻 Системная (prefers-color-scheme)
- ☀️ Светлая

Настройка сохраняется в localStorage.

## 📡 API Endpoints

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/chart-data` | Данные для графика (свечи, SMA, RSI, BB) |
| GET | `/api/symbols` | Список доступных пар |
| GET | `/api/fear-greed` | Индекс страха и жадности |
| POST | `/api/backtest/start` | Запустить бэктест |
| GET | `/api/backtest/progress` | Прогресс бэктеста |
| GET | `/api/backtest/report` | Отчёт бэктеста |
| POST | `/api/backtest/cancel` | Остановить бэктест |
| GET | `/api/backtest/active` | Активные бэктесты |
| GET | `/api/backtest/history` | История бэктестов |
| GET | `/api/backtest/history-file` | Файл истории по стратегии |

## ⚠️ Важно

- **Прокси должен раздавать статику из `../frontend/`** — проверь путь в `proxy_server.py`
- **Антикэш-заголовки** в `proxy_server.py** обязательны для Cloudflare
- **Без транскодинга** — графики требуют `lwcharts.js` (легковесная библиотека)
- **Binance rate limit** — 1200 weight/min, ~29 weight/запрос klines

## 🔧 Конфигурация прокси

В `proxy_server.py` настрой:

```python
WEB_DIST = Path(__file__).parent.parent / "frontend"
DASHBOARD_API = "http://127.0.0.1:8889"
```

## 🐛 Известные баги

- При редактировании HTML/JS/Python через `read_file()` в `execute_code` номера строк вставляются в контент — используй `terminal("cat file")`
- `proxy_server.py` должен отрезать query-строку (`?v=X`) при поиске файлов на диске
