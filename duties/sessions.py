"""Сессии Claude: какая сессия на дежурстве, её название и ссылка.

Откуда что берётся (проверено 05.10.2026 на десктоп-приложении 2.19675 / CLI 2.1.286):
- каждая сессия десктоп-приложения — отдельный процесс CLI `…\\claude-code\\<ver>\\…\\claude.exe --output-format
  stream-json …`; в его окружении есть CLAUDE_CODE_HOST_SESSION_ID=local_<uuid> (id сессии в приложении);
- процессы, которые сессия запускает (Bash-тул, Monitor, MCP-серверы), наследуют ещё CLAUDE_CODE_SESSION_ID (uuid
  транскрипта CLI) и CLAUDE_PID (pid процесса CLI) — по ним Monitor-обёртка и сам MCP-сервер знают свою сессию;
- метаданные: %APPDATA%\\Claude\\claude-code-sessions\\<аккаунт>\\<орг>\\local_<uuid>.json — title, cwd,
  cliSessionId, isArchived, lastActivityAt, postTurnSummary;
- ссылка, открывающая сессию в приложении: claude://claude.ai/epitaxy/local_<uuid>.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from . import config

_cache: dict[str, tuple[float, dict | None]] = {}
_cli_index: dict[str, str] = {}


def link(session_id: str | None) -> str | None:
    if session_id and session_id.startswith("local_"):
        return config.SESSION_LINK.format(sid=session_id)
    return None


def _ms_to_iso(v) -> str | None:
    try:
        return datetime.fromtimestamp(int(v) / 1000).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _read_meta(p: Path) -> dict | None:
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _cache.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        d = None
    meta = None
    if isinstance(d, dict):
        summ = d.get("postTurnSummary") or {}
        meta = {
            "session_id": d.get("sessionId"),
            "cli_session_id": d.get("cliSessionId"),
            "title": d.get("title"),
            "cwd": d.get("cwd"),
            "archived": bool(d.get("isArchived")),
            "last_activity": _ms_to_iso(d.get("lastActivityAt")),
            "last_activity_ts": (int(d["lastActivityAt"]) / 1000) if str(d.get("lastActivityAt", "")).isdigit() else None,
            "created": _ms_to_iso(d.get("createdAt")),
            "status_category": summ.get("status_category"),
            "status_detail": (summ.get("status_detail") or "")[:160] or None,
            "link": link(d.get("sessionId")),
        }
        if meta["cli_session_id"]:
            _cli_index[meta["cli_session_id"]] = meta["session_id"]
    _cache[str(p)] = (mt, meta)
    return meta


def session_meta(session_id: str | None) -> dict | None:
    """Метаданные сессии десктоп-приложения по local_<uuid> (None — не нашлось/не десктоп)."""
    if not session_id or not session_id.startswith("local_"):
        return None
    base = config.claude_sessions_dir()
    if not base.is_dir():
        return None
    for p in base.glob(f"*/*/{session_id}.json"):
        return _read_meta(p)
    return None


def find_by_cli(cli_session_id: str | None) -> dict | None:
    """Сессия приложения по uuid транскрипта CLI (полный проход по метаданным — только когда нужно)."""
    if not cli_session_id:
        return None
    sid = _cli_index.get(cli_session_id)
    if sid:
        return session_meta(sid)
    base = config.claude_sessions_dir()
    if not base.is_dir():
        return None
    for p in base.glob("*/*/local_*.json"):
        m = _read_meta(p)
        if m and m.get("cli_session_id") == cli_session_id:
            return m
    return None


def current_from_env() -> dict:
    """Сессия, в которой работает текущий процесс (MCP-сервер / Bash-команда агента)."""
    return {"session_id": os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or None,
            "cli_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID") or None}


def describe(session_id: str | None = None, cli_session_id: str | None = None) -> dict:
    """Всё, что известно о сессии: id, название, ссылка, архивная ли, последняя активность."""
    meta = session_meta(session_id) if session_id else None
    if meta is None and cli_session_id:
        meta = find_by_cli(cli_session_id)
    sid = session_id or (meta or {}).get("session_id")
    out = {"session_id": sid, "cli_session_id": cli_session_id or (meta or {}).get("cli_session_id"),
           "title": (meta or {}).get("title"), "link": link(sid)}
    if meta:
        out.update({"archived": meta.get("archived"), "last_activity": meta.get("last_activity"),
                    "status_category": meta.get("status_category"), "status_detail": meta.get("status_detail")})
    return out
