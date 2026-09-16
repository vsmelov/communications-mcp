#!/usr/bin/env python3
"""Разовый интерактивный релогин для tg-archive (data/session/archiver.session).

Спросит номер телефона, код из Telegram и (если включён) облачный пароль 2FA.
Контейнер tg-archive должен быть ОСТАНОВЛЕН перед запуском (чтобы никто
не держал старый файл открытым) — docker stop tg-archive.

Запуск (PowerShell, из папки communications-mcp):
    python relogin_archiver.py
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

session_dir = ROOT / "tg-archive" / "data" / "session"
session_dir.mkdir(parents=True, exist_ok=True)
session_path = session_dir / "archiver"  # telethon сам добавит .session

client = TelegramClient(str(session_path), int(api_id), api_hash)


def main() -> None:
    client.start()  # спросит phone / code / (2FA password) прямо в консоли
    print(f"\nOK, сессия создана: {session_path}.session")
    client.disconnect()


if __name__ == "__main__":
    main()
