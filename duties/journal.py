"""Журнал ответов дежурства — `notes/<имя>_replies.jsonl` (путь — поле `replies` в реестре).

Зачем: чтобы дашборд считал «ожидает ответа» (входящие после последнего ответа). Пишет его
`tools/send_reply.py` после успешной отправки (аддитивно, под try/except — на отправку не влияет)
и `duty_handled` («обработано без ответа», например личное владельцу).

Строка: {"ts", "date", "kind": "reply"|"handled", "chat_id", "msg_id", "reply_to", "up_to_id", "file",
         "signed", "chars", "session"} — ТЕКСТ ОТВЕТА НЕ ХРАНИТСЯ (только длина).
Ответ в чат, не заведённый в реестр, уходит в duties/.runtime/replies_other.jsonl.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import config, registry
from .textparse import chat_key


def replies_path(d: dict) -> Path:
    if d.get("replies"):
        return Path(d["replies"])
    return Path(d["workspace"]) / "notes" / f"{d['name']}_replies.jsonl"


def find_duty_by_chat(data: dict, chat) -> dict | None:
    """Дежурство, в котором есть этот чат (по id; для @username/подстроки — по исходной записи)."""
    ck = chat_key(chat)
    for d in (data.get("duties") or {}).values():
        for c in d.get("chats") or []:
            if ck is not None and chat_key(c.get("id")) == ck:
                return d
            if ck is None and str(c.get("spec") or "").strip().lower() == str(chat).strip().lower():
                return d
    return None


def _append(path: Path, rec: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def record_reply(*, chat, chat_id=None, msg_id=None, reply_to=None, with_file: bool = False, signed: bool = True,
                 chars: int = 0, registry_path: Path | None = None) -> Path | None:
    """Записать отправленный ответ. Возвращает путь журнала (None — не записано)."""
    ts = time.time()
    rec = {"ts": round(ts, 3), "date": datetime.fromtimestamp(ts).isoformat(timespec="seconds"), "kind": "reply",
           "chat_id": chat_id if chat_id is not None else (int(chat) if str(chat).lstrip("-").isdigit() else None),
           "chat": None if str(chat).lstrip("-").isdigit() else str(chat), "msg_id": msg_id,
           "reply_to": reply_to or None, "file": bool(with_file), "signed": bool(signed), "chars": int(chars or 0),
           "session": os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")}
    try:
        data = registry.load(registry_path)
    except Exception:  # noqa: BLE001
        data = registry.empty()
    duty = find_duty_by_chat(data, chat_id if chat_id is not None else chat)
    if duty:
        rec["duty"] = duty["name"]
        return _append(replies_path(duty), rec)
    return _append(config.runtime_dir() / "replies_other.jsonl", rec)


def record_handled(duty: dict, *, up_to_id: int, chat_id=None, note: str = "") -> Path:
    """«Обработано без ответа» до id включительно (в чате chat_id или во всех чатах дежурства)."""
    ts = time.time()
    rec = {"ts": round(ts, 3), "date": datetime.fromtimestamp(ts).isoformat(timespec="seconds"), "kind": "handled",
           "chat_id": chat_id, "up_to_id": int(up_to_id), "note": (note or "")[:200], "duty": duty["name"],
           "session": os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")}
    return _append(replies_path(duty), rec)
