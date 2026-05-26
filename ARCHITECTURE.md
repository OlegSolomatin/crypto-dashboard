```mermaid
graph TB
    subgraph "🌐 ВНЕШНИЕ API (бесплатные)"
        BYBIT["Bybit API\nЦена TON/USDT"]
        COINGECKO["CoinGecko\nMarket Cap, FDV"]
        TONCENTER["Toncenter\nБлокчейн TON"]
        TELEGRAM["Telegram Bot\nУведомления"]
    end

    subgraph "📡 СБОР ДАННЫХ (cron: каждую минуту)"
        MONITOR["monitor.py\n💾 data.db\n1253+ записей цен"]
    end

    subgraph "🔍 ОНЧЕЙН-АНАЛИТИКА (cron: каждый час)"
        ONCHAIN["onchain_v3.py\n💾 onchain.db\nNVT, корреляция, сеть"]
    end

    subgraph "📊 ТОРГОВЫЕ СИГНАЛЫ (cron: каждые 5 мин)"
        SIGNAL["signal_v5.py\n├─ RSI + MACD + Bollinger\n├─ SL/TP от волатильности\n├─ Ончейн-фильтры\n└─ Самообучение"]
        SIGNALS_DB[("💾 signals.db\nИстория сигналов")]
        TRADES_DB[("💾 trades.db\nP&L сделок")]
        LEARNING_DB[("💾 learning.db\nСобытия обучения")]
    end

    subgraph "🚨 АЛЕРТЫ (cron: каждые 5 мин)"
        ALERT["alert.py\nРезкие движения >3%"]
    end

    subgraph "🛡️ ЗАЩИТА"
        BACKUP["backup-hermes.py\n(03:00 МСК)\nGit + tar.gz"]
    end

    subgraph "🧑 ПОЛЬЗОВАТЕЛЬ"
        OLEG["👤 Олег\nTelegram / WebUI"]
    end

    %% Сбор данных
    BYBIT -->|"REST API\nкаждую минуту"| MONITOR
    BYBIT -->|"BTC цена"| ONCHAIN
    COINGECKO -->|"Market Cap, Volume"| ONCHAIN
    TONCENTER -->|"Блоки, seqno"| ONCHAIN

    %% Данные → Сигналы
    MONITOR -->|"цены"| SIGNAL
    ONCHAIN -->|"NVT, корреляция"| SIGNAL

    %% Сигналы → Базы
    SIGNAL -->|"сохраняет"| SIGNALS_DB
    SIGNAL -->|"трекинг сделок"| TRADES_DB
    SIGNAL -->|"3 убытка → анализ"| LEARNING_DB

    %% Алерты
    MONITOR -->|"цены"| ALERT

    %% Уведомления
    SIGNAL -->|"BUY/SELL + SL/TP"| TELEGRAM
    ALERT -->|"Резкое движение"| TELEGRAM
    LEARNING_DB -.->|"Предложения правок"| TELEGRAM

    %% Пользователь
    TELEGRAM -->|"Сигналы, алерты, отчёты"| OLEG
    OLEG -->|"/apply_learning"| SIGNAL
    OLEG -->|"Управление"| BACKUP

    %% Стили
    classDef source fill:#1a1a2e,stroke:#16213e,color:#e0e0e0
    classDef process fill:#0f3460,stroke:#16213e,color:#fff
    classDef storage fill:#16213e,stroke:#0f3460,color:#a0a0a0
    classDef alert fill:#533483,stroke:#16213e,color:#fff
    classDef user fill:#1a1a2e,stroke:#e94560,color:#fff

    class BYBIT,COINGECKO,TONCENTER,TELEGRAM source
    class MONITOR,ONCHAIN,SIGNAL process
    class SIGNALS_DB,TRADES_DB,LEARNING_DB storage
    class ALERT,BACKUP alert
    class OLEG user
```

## 📋 Легенда

| Цвет | Компонент | Описание |
|------|-----------|----------|
| 🔵 Тёмный | Внешние API | Бесплатные источники данных |
| 🔷 Синий | Процессы | Python-скрипты, cron-задачи |
| 🔹 Серый | Базы данных | SQLite-хранилища |
| 🟣 Фиолетовый | Защита/Алерты | Бэкапы, уведомления |
| 🔴 Красный | Пользователь | Олег через Telegram/WebUI |
