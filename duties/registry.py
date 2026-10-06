"""Реестр дежурств — `duties/registry.json` (в .gitignore: там id чатов, как и в tg-archive/config.yaml).

Формат:
    {"version": 1,
     "settings": {...пороги, см. config.DEFAULT_SETTINGS...},
     "duties": {"<имя>": {
         "name", "title",
         "chats": [{"id": 473364365, "title": "…", "spec": "473364365"}],   # spec — как передавать в --chat
         "workspace": абсолютный путь воркспейса,
         "state" / "log" / "stdout" / "replies": абсолютные пути (state/log/stdout — как у слушателя),
         "pidfile": путь или null (старые схемы, например у Марии — notes/maria_watch.pid),
         "script": null (= tools/watch_chat.py) или путь к другому слушателю,
         "trigger": "/ai", "quiet": 0, "sign": true, "extra_args": [],
         "active": true/false (false — архив: тревоги не считаются),
         "agent": {"session_id": "local_…", "cli_session_id", "title", "link", "attached_at"} | null,
         "monitor_command": "…", "notes": "…", "status_note": "…", "created_at": "…"}}}

Запись — под файловой блокировкой и через временный файл + os.replace (читатели никогда не видят половину).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import config

VERSION = 1
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class DutyError(Exception):
    """Ошибка уровня пользователя (понятный текст по-русски)."""


def empty() -> dict:
    return {"version": VERSION, "settings": dict(config.DEFAULT_SETTINGS), "duties": {}}


def load(path: Path | None = None) -> dict:
    p = Path(path or config.registry_path())
    if not p.is_file():
        return empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise DutyError(f"реестр {p} не читается как JSON: {e}") from e
    if not isinstance(data, dict):
        raise DutyError(f"реестр {p}: ожидался объект JSON")
    data.setdefault("version", VERSION)
    data.setdefault("duties", {})
    st = dict(config.DEFAULT_SETTINGS)
    st.update(data.get("settings") or {})
    data["settings"] = st
    return data


def save(data: dict, path: Path | None = None) -> None:
    p = Path(path or config.registry_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


@contextlib.contextmanager
def _locked(path: Path, timeout: float = 10.0):
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 20:  # осиротевшая блокировка упавшего процесса
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise DutyError("реестр занят другим процессом (блокировка не снялась за 10 с)")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def update(fn, path: Path | None = None):
    """Изменить реестр под блокировкой: fn(data) меняет data на месте и может вернуть результат."""
    p = Path(path or config.registry_path())
    with _locked(p):
        data = load(p)
        res = fn(data)
        save(data, p)
    return res


def get(data: dict, name: str) -> dict:
    d = (data.get("duties") or {}).get(name)
    if not d:
        known = ", ".join(sorted(data.get("duties") or {})) or "реестр пуст"
        raise DutyError(f"дежурство «{name}» не найдено (есть: {known})")
    return d


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def script_path(d: dict) -> Path:
    s = d.get("script")
    return Path(s) if s else config.WATCH_SCRIPT


def script_names(data: dict) -> set[str]:
    """Имена файлов слушателей, которые ищем среди процессов (watch_chat.py + нестандартные из реестра)."""
    names = {config.WATCH_SCRIPT.name.lower()}
    for d in (data.get("duties") or {}).values():
        if d.get("script"):
            names.add(Path(d["script"]).name.lower())
    return names


def chat_ids(d: dict) -> list[int]:
    out = []
    for c in d.get("chats") or []:
        if isinstance(c.get("id"), int):
            out.append(c["id"])
    return out


def chat_specs(d: dict) -> list[str]:
    """Что передавать слушателю в --chat: id, если известен, иначе исходная запись (@username / подстрока)."""
    out = []
    for c in d.get("chats") or []:
        out.append(str(c["id"]) if isinstance(c.get("id"), int) else str(c.get("spec") or "").strip())
    return [x for x in out if x]


def parse_chat_specs(raw) -> list[dict]:
    """'473364365, @user' / ['123', 'Мария'] → [{"id": 473364365, "spec": "473364365", "title": None}, …]."""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,;\n]", str(raw or ""))
    out = []
    for it in items:
        s = it.strip()
        if not s:
            continue
        cid = int(s) if re.fullmatch(r"-?\d+", s) else None
        out.append({"id": cid, "spec": s, "title": None})
    return out


def validate_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not NAME_RE.match(n):
        raise DutyError("имя дежурства: латиница в нижнем регистре, цифры, «-» и «_», до 40 символов "
                        "(например amir, maria-site)")
    return n
