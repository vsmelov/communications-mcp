"""Тонкий фасад над ядром: компактные ответы для MCP-инструментов (server.py) и CLI.

Ошибки уровня пользователя (DutyError) превращаются в {"ok": false, "error": "…"}, чтобы агент видел понятный
текст, а не трейсбек.
"""
from __future__ import annotations

import functools

from . import control, registry, status
from .registry import DutyError


def _safe(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except DutyError as e:
            return {"ok": False, "error": str(e)}
    return wrap


def _agent_short(s: dict) -> str:
    a = s.get("agent") or {}
    st = a.get("state")
    if st == "connected":
        m = (a.get("monitors") or [{}])[0]
        return f"подключён «{m.get('title') or m.get('session_id') or m.get('cli_session_id') or '?'}»"
    if st == "rearming":
        return "перевзводит Monitor"
    if st == "idle":
        return "сессия открыта, Monitor не запущен"
    return "нет"


def _short(s: dict) -> dict:
    li = s.get("listener") or {}
    hb = s.get("heartbeat") or {}
    cn = s.get("connection") or {}
    pe = s.get("pending") or {}
    tr = s.get("traffic") or {}
    return {
        "name": s["name"], "title": s.get("title"), "active": s.get("active"), "level": s.get("level"),
        "listener": (f"{li.get('state')} PID {li.get('pid')}" if li.get("pid") else li.get("state")),
        "heartbeat": f"{hb.get('level')} ({hb.get('age')} назад)" if hb.get("age") else hb.get("level"),
        "connection": f"{cn.get('state')}" + (f" via {cn.get('via')}" if cn.get("via") and cn.get("state") == "online" else ""),
        "agent": _agent_short(s),
        "incoming_today": tr.get("today"), "pending": pe.get("count") if pe.get("known") else None,
        "alerts": [f"[{a['level']}] {a['text']}" for a in s.get("alerts", [])],
    }


@_safe
def list_duties() -> dict:
    st = status.collect()
    items = sorted(st["duties"].values(), key=lambda s: (not s.get("active"), s["name"]))
    return {"summary": st["summary"], "duties": [_short(s) for s in items],
            "unregistered": [{k: f[k] for k in ("pid", "chats", "state", "workspace", "suggested_name")}
                             for f in st["unregistered"]],
            "global_alerts": [f"[{a['level']}] {a['text']}" for a in st["alerts"]],
            "orphans": st["orphans"]}


@_safe
def duty_status(name: str) -> dict:
    registry.get(registry.load(), name)
    st = status.collect(names=[name])
    s = st["duties"][name]
    out = {k: s.get(k) for k in ("name", "title", "active", "level", "alerts", "chats", "workspace", "listener",
                                 "heartbeat", "connection", "agent", "traffic", "pending", "paths")}
    out["pending"] = {k: v for k, v in (s.get("pending") or {}).items() if k not in ("oldest_ts", "last_reply_ts")}
    out["monitor_command_hint"] = f"duty_monitor_command(\"{name}\")"
    return out


start = _safe(control.start)
stop = _safe(control.stop)
restart = _safe(control.restart)
remove = _safe(control.remove)
attach = _safe(control.attach)
handled = _safe(control.handled)
tail = _safe(control.tail)
monitor_command = _safe(control.monitor_command)
set_active = _safe(control.set_active)


@_safe
def add(**kw) -> dict:
    return control.add(**kw)


@_safe
def dashboard() -> dict:
    from . import dashboard as dash
    return dash.ensure_running()
