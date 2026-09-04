#!/usr/bin/env python3
"""Проверка готовности к запуску: зависимости, .env, сессия, сабмодуль.

Ничего не скачивает и никуда не отправляет — только смотрит, что на месте,
создаёт .env и tg-archive/config.yaml из шаблонов и печатает готовую команду
подключения к Claude Code.

    python setup.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OK, WARN, BAD = "[ok]", "[ ! ]", "[x ]"

try:  # русский текст в консоли Windows (cp1251/cp866) без UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

problems: list[str] = []


def say(mark: str, text: str, hint: str = "") -> None:
    print(f" {mark} {text}")
    if hint:
        print(f"      {hint}")


def fail(text: str, hint: str) -> None:
    say(BAD, text, hint)
    problems.append(text)


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 10):
        say(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        fail(
            f"Python {v.major}.{v.minor} — нужен 3.10+",
            "поставь свежий Python и запусти скрипт им же",
        )


def check_deps() -> None:
    missing = [
        pkg
        for mod, pkg in (
            ("telethon", "telethon"),
            ("mcp", "mcp"),
            ("dotenv", "python-dotenv"),
        )
        if importlib.util.find_spec(mod) is None
    ]
    if missing:
        fail(
            "не установлены зависимости: " + ", ".join(missing),
            f'"{sys.executable}" -m pip install -r requirements.txt',
        )
    else:
        say(OK, "зависимости установлены")


def ensure_env() -> dict[str, str]:
    env_path, example = ROOT / ".env", ROOT / ".env.example"
    if not env_path.exists():
        if not example.exists():
            fail("нет ни .env, ни .env.example", "репозиторий склонирован не полностью?")
            return {}
        shutil.copyfile(example, env_path)
        say(WARN, "создан .env из .env.example", f"заполни его: {env_path}")
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, val = line.partition("=")
            values[k.strip()] = val.strip()
    return values


def check_env(values: dict[str, str]) -> None:
    for key, where in (
        ("TELEGRAM_API_ID", "https://my.telegram.org -> API development tools"),
        ("TELEGRAM_API_HASH", "оттуда же"),
    ):
        if values.get(key):
            say(OK, f"{key} задан")
        else:
            fail(f"{key} пуст в .env", where)

    chain = values.get("TELEGRAM_PROXY_CHAIN") or values.get("TELEGRAM_PROXY")
    say(OK, f"подключение: {chain}" if chain else "подключение: напрямую, без прокси")


def check_session(values: dict[str, str]) -> None:
    rel = values.get("TELEGRAM_SESSION") or "sessions/mcp.session"
    path = Path(rel) if Path(rel).is_absolute() else ROOT / rel
    if path.suffix != ".session":
        path = path.with_suffix(".session")
    if path.exists():
        say(OK, f"сессия есть: {path.name}")
    else:
        say(
            WARN,
            "сессии ещё нет",
            f'первый запуск создаст её: "{sys.executable}" server.py '
            "(спросит номер телефона и код из Telegram)",
        )


def check_submodule() -> None:
    gowa = ROOT / "whatsapp" / "gowa"
    if (gowa / "docker-compose.yml").exists():
        say(OK, "сабмодуль whatsapp/gowa на месте")
    else:
        say(
            WARN,
            "сабмодуль whatsapp/gowa не инициализирован (нужен только для WhatsApp)",
            "git submodule update --init --recursive",
        )


def ensure_archive_config() -> None:
    cfg, example = ROOT / "tg-archive" / "config.yaml", ROOT / "tg-archive" / "config.example.yaml"
    if cfg.exists():
        say(OK, "tg-archive/config.yaml на месте")
    elif example.exists():
        shutil.copyfile(example, cfg)
        say(
            WARN,
            "создан tg-archive/config.yaml из шаблона (нужен только для tg-archive)",
            f"впиши свои чаты в секцию chats: {cfg}",
        )


def check_tool(name: str, args: list[str], why: str) -> None:
    if shutil.which(name) is None:
        say(WARN, f"{name} не найден в PATH — {why}")
        return
    try:
        subprocess.run(args, capture_output=True, timeout=15, check=False)
        say(OK, f"{name} доступен")
    except Exception:
        say(WARN, f"{name} есть в PATH, но не отвечает — {why}")


def print_connect() -> None:
    print("\nПодключить к Claude Code:\n")
    print(f'  claude mcp add --scope user telegram -- "{sys.executable}" "{ROOT / "server.py"}"\n')
    print("Отдельным клиентам (Claude Desktop, Cursor) нужна своя копия сессии —")
    print("см. раздел «Несколько клиентов сразу» в README.")


def main() -> int:
    print(f"\ncommunications-mcp — проверка окружения\n{ROOT}\n")
    check_python()
    check_deps()
    values = ensure_env()
    check_env(values)
    check_session(values)
    print("\nОпционально (WhatsApp-мост и фоновый архиватор):\n")
    check_submodule()
    ensure_archive_config()
    check_tool("docker", ["docker", "info"], "нужен для whatsapp/ и tg-archive/")

    if problems:
        print(f"\nНе готово, {len(problems)} пункт(ов):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nВсё на месте.")
    print_connect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
