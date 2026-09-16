#!/usr/bin/env python3
"""Разовый интерактивный релогин для sessions/desktop.session.

Спросит номер телефона, код из Telegram и (если включён) облачный пароль 2FA.
Ничего не удаляет и не трогает другие session-файлы.

Запуск (PowerShell, из папки communications-mcp):
    python relogin_desktop.py
"""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

api_id = os.environ.get("TELEGRAM_API_ID")
api_hash = os.environ.get("TELEGRAM_API_HASH")
if not api_id or not api_hash:
    sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в .env")

session_path = ROOT / "sessions" / "desktop"  # telethon сам добавит .session

client = TelegramClient(str(session_path), int(api_id), api_hash)


def main() -> None:
    client.start()  # спросит phone / code / (2FA password) прямо в консоли
    me = client.get_me()
    print(f"\nOK, залогинен как: {me.first_name} (@{me.username}) id={me.id}")
    client.disconnect()


if __name__ == "__main__":
    main()
