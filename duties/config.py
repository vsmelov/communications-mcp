"""Пути, порты и пороги дежурств. Всё, что зависит от машины, можно переопределить переменными окружения
(тесты так подменяют реестр и рантайм-папку на временные)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent            # communications-mcp/duties
ROOT = PKG.parent                                 # communications-mcp
WORKSPACE_ROOT = ROOT.parent                      # claude-workspace
TOOLS = ROOT / "tools"
WATCH_SCRIPT = TOOLS / "watch_chat.py"
SEND_REPLY = TOOLS / "send_reply.py"
VOICE_TEXT = TOOLS / "voice_text.py"
NETPATH = ROOT / "netpath.py"
WEB = PKG / "web"
TEMPLATES = PKG / "templates"

HOST = "127.0.0.1"
DEFAULT_PORT = 18210
FALLBACK_PORT = 18211
APP_ID = "claude-duties-dashboard"

# фильтр Monitor по умолчанию — если не удалось прочитать FILTER из watch_chat.py
DEFAULT_FILTER = r"^(BATCH|READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)"
MONITOR_TIMEOUT_MS = 1_800_000   # Monitor живёт максимум 30 мин, дальше агент перевзводит его той же командой

CLAUDE_EMOJI_ID = 6273917097319865433
SESSION_LINK = "claude://claude.ai/epitaxy/{sid}"

# пороги (минуты); переопределяются в registry.json → settings
DEFAULT_SETTINGS: dict = {
    "unanswered_alert_min": 30,      # «ожидает ответа» дольше — красная тревога
    "heartbeat_green_min": 12,       # ALIVE раз в 10 мин: до 12 мин — норма
    "heartbeat_yellow_min": 25,      # до 25 — жёлтый, дальше — красный
    "rearm_grace_min": 5,            # пауза между истечением Monitor и перевзводом — ещё не тревога
    "connection_grace_min": 5,       # короткий обрыв связи (слушатель сам переподключается) — не тревога
    "archive_stale_min": 60,         # дамп tg-archive не обновлялся дольше — «архив не пишется»
    "python": "",                    # интерпретатор для слушателей; пусто — C:\Python312\python.exe или текущий
}


def registry_path() -> Path:
    """Файл-источник правды (в .gitignore: там id чатов)."""
    return Path(os.environ.get("DUTIES_REGISTRY") or PKG / "registry.json")


def runtime_dir() -> Path:
    """Рантайм: кэш «агента видели», маркеры Monitor, журнал действий, pid дашборда (в .gitignore)."""
    p = Path(os.environ.get("DUTIES_RUNTIME") or PKG / ".runtime")
    p.mkdir(parents=True, exist_ok=True)
    return p


def archive_root() -> Path:
    """Папка с живыми дампами tg-archive (data/chats/<id>-<slug>/)."""
    raw = os.environ.get("DUTIES_ARCHIVE_ROOT") or os.environ.get("TELEGRAM_ARCHIVE_ROOT") or ""
    return Path(raw) if raw.strip() else ROOT / "tg-archive" / "data" / "chats"


def archive_url() -> str:
    return (os.environ.get("TELEGRAM_ARCHIVE_URL") or "http://127.0.0.1:8722").rstrip("/")


def claude_sessions_dir() -> Path:
    """Метаданные сессий десктоп-приложения: %APPDATA%/Claude/claude-code-sessions/<acc>/<org>/local_<uuid>.json."""
    raw = os.environ.get("DUTIES_CLAUDE_SESSIONS_DIR")
    if raw:
        return Path(raw)
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Claude" / "claude-code-sessions"


def default_python() -> str:
    """Python для слушателей: тот же системный 3.12, что у MCP-сервера (там Telethon 1.43.2 — пин)."""
    cand = Path(r"C:\Python312\python.exe")
    if cand.is_file():
        return str(cand)
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def default_pythonw() -> str:
    """pythonw.exe (без консоли) для фонового дашборда; если нет — обычный python."""
    for cand in (Path(r"C:\Python312\pythonw.exe"), Path(sys.executable).with_name("pythonw.exe")):
        if cand.is_file():
            return str(cand)
    return default_python()
