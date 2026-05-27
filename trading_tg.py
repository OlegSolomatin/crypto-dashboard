#!/usr/bin/env python3
"""
Утилита для отправки трейдинг-сообщений в Telegram.
Приоритет: TRADING_BOT_TOKEN → TELEGRAM_BOT_TOKEN (fallback).

Импортируется трейдинг-скриптами:
  from trading_tg import send_trading_tg
  send_trading_tg("текст")
"""

import os, urllib.request, urllib.parse, sys


def _get_credentials():
    """Вернуть (token, chat_id) для трейдинг-бота."""
    trading_token = os.getenv("TRADING_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TRADING_CHAT_ID", "").strip()
    
    if trading_token and chat_id:
        return trading_token, chat_id
    
    # Fallback: основной бот
    main_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    main_chat = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    
    return main_token, main_chat


def send_trading_tg(text: str, parse_mode: str = "HTML") -> bool:
    """Отправить трейдинг-уведомление. Возвращает True если отправлено."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        return False
    
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"  Trading TG error: {e}", file=sys.stderr)
        return False


def send_trading_test():
    """Тестовое сообщение."""
    ok = send_trading_tg("✅ <b>Трейдинг-бот подключён!</b>\n\nУведомления о сигналах и сделках будут здесь.")
    if ok:
        print("  ✅ Тестовое сообщение отправлено")
    else:
        print("  ❌ Не удалось отправить")


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_trading_test()
    else:
        # Принимаем текст из stdin или аргумента
        if len(sys.argv) > 1 and sys.argv[1] != "--test":
            send_trading_tg(" ".join(sys.argv[1:]))
        else:
            print("Usage: trading_tg.py --test  или  trading_tg.py 'текст сообщения'")
