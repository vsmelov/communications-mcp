"""Управление дежурствами: старт/стоп/перезапуск слушателя, заведение, привязка агента, команда Monitor, хвост.

Безопасность:
- старт не запускает второй слушатель на тот же state-файл (дубль дрался бы за копию сессии Telegram);
- стоп убивает только процессы, подтверждённо являющиеся слушателем ЭТОГО дежурства (имя скрипта + тот же
  --state + время старта процесса), и их обёртку cmd.exe; state/log/stdout не трогаются;
- Monitor сервер запустить не может — это делает сама сессия агента командой из monitor_command().
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import config, journal, registry, sessions, status
from .procs import take_snapshot, spawn_detached, terminate
from .registry import DutyError
from .textparse import mask_line, norm_path, parse_incoming, to_msys

MONITOR_REMINDER = ("Monitor запускает сама сессия агента — сервер сделать этого не может: в сессии, которая "
                    "дежурит, вызвать тул Monitor с этой командой (timeout_ms 1800000), по истечении (30 мин) "
                    "перевзвести той же командой — пропущенное за паузу она догонит сама; затем duty_attach(name).")


def actor() -> str:
    """Кто действует: «dashboard» (кнопка дашборда), id сессии Claude (MCP-тул агента) или «local» (CLI)."""
    return (os.environ.get("DUTIES_ACTOR") or os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID") or "local")


def _log_action(action: str, name: str, **kw) -> None:
    rec = {"ts": round(time.time(), 3), "date": registry.now_iso(), "action": action, "duty": name, "by": actor(),
           **kw}
    try:
        with (config.runtime_dir() / "actions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _duty(name: str) -> tuple[dict, dict]:
    data = registry.load()
    d = dict(registry.get(data, name))
    d["name"] = name
    return data, d


def _my_listeners(d: dict, data: dict, snap=None) -> tuple[list[dict], object]:
    snap = snap or take_snapshot()
    ls = [x for x in status.find_listeners(snap, registry.script_names(data)) if status.listener_matches(x, d)]
    return ls, snap


# ---------------------------------------------------------------- Monitor ---

def read_filter() -> str:
    """Фильтр Monitor из watch_chat.py (FILTER = r"…"), без импорта скрипта (там Telethon)."""
    try:
        src = config.WATCH_SCRIPT.read_text(encoding="utf-8")
        m = re.search(r'^FILTER\s*=\s*r?"([^"\n]+)"', src, re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return config.DEFAULT_FILTER


def _sq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def monitor_command(names: list[str] | str) -> dict:
    """Готовая команда для тула Monitor (одна строка bash) на одно или несколько дежурств.

    Что делает: печатает «MONITOR <имя>: старт…» и последнюю служебную строку (агент сразу видит состояние),
    затем `tail --pid=$$ -F` stdout слушателя с фильтром из watch_chat.py --print-filter (строки NEW не
    пропускаются никогда — тексты читаются из --log). awk пишет номер каждой прочитанной строки в маркер
    `duties/.runtime/<имя>.monitor_line`: при перевзводе команда продолжает с места, где остановился прошлый
    Monitor, — пачки, пришедшие в паузе между Monitor, не теряются, а дашборд видит, что агент дочитал.
    `--pid=$$` убивает tail вместе с Monitor (без этого на Windows tail/grep остаются сиротами при каждом
    истечении Monitor — к 05.10.2026 их накопилось ~80)."""
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    data = registry.load()
    duties = [dict(registry.get(data, n), name=n) for n in names]
    flt = read_filter()
    fn = ("W() { L=$1; M=$2; N=$3; mkdir -p \"${M%/*}\"; n=$(wc -l < \"$L\" 2>/dev/null || echo 0); "
          "k=$(cat \"$M\" 2>/dev/null); case \"$k\" in ''|*[!0-9]*) k=$n;; esac; [ \"$k\" -gt \"$n\" ] && k=$n; "
          "echo \"MONITOR $N: старт $(date +%H:%M:%S), новых строк с прошлого Monitor: $((n-k))\"; "
          "grep -E '^(ALIVE|READY connected|READY reconnected|ConnectionError|Error)' \"$L\" 2>/dev/null | tail -n 1; "
          "tail --pid=$$ -n +$((k+1)) -F \"$L\" 2>/dev/null | awk -v M=\"$M\" -v k=\"$k\" -v F=\"$F\" "
          "'{k++; print k > M; close(M)} $0 !~ /^NEW / && $0 ~ F {print; fflush()}'; }")
    calls = []
    for d in duties:
        if not d.get("stdout"):
            raise DutyError(f"у дежурства «{d['name']}» не задан stdout-лог")
        calls.append(f"W {_sq(to_msys(d['stdout']))} {_sq(to_msys(status.marker_path(d['name'])))} {_sq(d['name'])}")
    body = f"F={_sq(flt)}; {fn}; " + (" & ".join(calls) + " & wait" if len(calls) > 1 else calls[0])
    title = ", ".join(d.get("title") or d["name"] for d in duties)
    return {"command": body,
            "description": f"дежурство {', '.join(d['name'] for d in duties)}: пачки сообщений и сбои слушателя ({title})",
            "timeout_ms": config.MONITOR_TIMEOUT_MS,
            "rearm": "по истечении (30 мин) перевзвести той же командой — догонит пропущенное за паузу",
            "attach": f"после запуска Monitor вызвать duty_attach(\"{duties[0]['name']}\")",
            "filter": flt}


# ----------------------------------------------------------- старт/стоп ----

def _q(a) -> str:
    s = str(a)
    if s and not re.search(r'[\s"&|<>^()]', s):
        return s
    return '"' + s.replace('"', '""') + '"'


def listener_command(d: dict, data: dict) -> str:
    st = data.get("settings") or {}
    py = d.get("python") or st.get("python") or config.default_python()
    args = [py, "-u", str(registry.script_path(d))]
    for spec in registry.chat_specs(d):
        args += ["--chat", spec]
    args += ["--state", d["state"], "--log", d["log"]]
    if d.get("quiet"):
        args += ["--quiet", str(int(d["quiet"]))]
    trig = d.get("trigger", "/ai")
    if trig != "/ai":
        args += ["--trigger", trig or ""]  # пустая строка → "" (вызов владельцем выключен)
    args += [str(a) for a in (d.get("extra_args") or [])]
    return " ".join(_q(a) for a in args) + f" >> {_q(d['stdout'])} 2>&1"


def start(name: str, wait_sec: float = 8.0) -> dict:
    data, d = _duty(name)
    if not registry.chat_specs(d):
        raise DutyError(f"у дежурства «{name}» не задан ни один чат")
    for k in ("workspace", "state", "log", "stdout"):
        if not d.get(k):
            raise DutyError(f"у дежурства «{name}» не задан путь «{k}»")
    ws = Path(d["workspace"])
    if not ws.is_dir():
        raise DutyError(f"нет папки воркспейса {ws}")
    script = registry.script_path(d)
    if not script.is_file():
        raise DutyError(f"нет скрипта слушателя {script}")
    alive, _ = _my_listeners(d, data)
    if alive:
        pids = [p for x in alive for p in x["pids"]]
        return {"ok": False, "started": False, "pids": pids,
                "error": f"слушатель «{name}» уже запущен (PID {', '.join(map(str, pids))}) — второй не запускаю"}
    for k in ("state", "log", "stdout"):
        Path(d[k]).parent.mkdir(parents=True, exist_ok=True)
    inner = listener_command(d, data)
    sp = spawn_detached(f'cmd.exe /d /s /c "{inner}"', str(ws), env_extra={"DUTIES_STARTED_BY": actor()})
    launcher_pid = sp["pid"]
    found = []
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(0.5)
        found, _ = _my_listeners(d, data)
        if found:
            break
    pid = found[0]["pid"] if found else None
    if pid and d.get("pidfile"):
        try:
            Path(d["pidfile"]).write_text(str(found[0]["pids"][-1]), encoding="utf-8")
        except OSError:
            pass
    _log_action("start", name, pid=pid, wrapper=launcher_pid)
    res = {"ok": bool(found), "started": True, "pid": pid, "wrapper_pid": launcher_pid, "stdout": d["stdout"],
           "command": inner}
    if sp.get("warning"):
        res["warning"] = sp["warning"]
    if not found:
        res["error"] = (f"процесс запущен (cmd PID {launcher_pid}), но слушатель не появился за {wait_sec:.0f} с — "
                        f"смотри хвост {d['stdout']}")
    return res


def stop(name: str, timeout: float = 8.0) -> dict:
    data, d = _duty(name)
    alive, snap = _my_listeners(d, data)
    if not alive:
        return {"ok": True, "stopped": [], "message": f"слушатель «{name}» не запущен"}
    killed, failed = [], []
    script = registry.script_path(d).name.lower()
    for x in alive:
        for pid in reversed(x["pids"]):  # сначала настоящий интерпретатор, потом venv-лаунчер
            p = snap.get(pid)
            if p is None or script not in p.cmdline.lower():
                failed.append(pid)
                continue
            (killed if terminate(pid, p.create_time, timeout) else failed).append(pid)
        wp = x.get("wrapper_pid")
        if wp:
            w = snap.get(wp)
            if w is not None and script in w.cmdline.lower() and Path(d["state"]).name.lower() in w.cmdline.lower():
                if terminate(wp, w.create_time, 3):
                    killed.append(wp)
    left, _ = _my_listeners(d, data)
    _log_action("stop", name, killed=killed, failed=failed)
    return {"ok": not left, "stopped": killed, "failed": failed,
            "message": "остановлен" if not left else f"не остановились: {[p for x in left for p in x['pids']]}"}


def restart(name: str) -> dict:
    st = stop(name)
    if not st.get("ok"):
        return {"ok": False, "stop": st, "error": "стоп не удался — старт не выполняю"}
    time.sleep(1.0)
    sr = start(name)
    return {"ok": sr.get("ok", False), "stop": st, "start": sr}


# ------------------------------------------------------------ заведение ----

def _workspace_path(name: str, workspace: str) -> Path:
    if not workspace:
        return config.WORKSPACE_ROOT / name
    p = Path(workspace)
    return p if p.is_absolute() else config.WORKSPACE_ROOT / p


def render_readme(d: dict) -> str:
    tpl = (config.TEMPLATES / "duty_README.md").read_text(encoding="utf-8")
    ws = Path(d["workspace"])

    def rel(p):
        try:
            return str(Path(p).relative_to(ws)).replace("\\", "/")
        except ValueError:
            return str(p)

    def relw(p):
        return rel(p).replace("/", "\\")

    chats = d.get("chats") or []
    first = registry.chat_specs(d)[0] if registry.chat_specs(d) else "<chat_id>"
    chat_rows = "\n".join(f"| чат | `{c.get('id') or c.get('spec')}`{(' — ' + c['title']) if c.get('title') else ''} |"
                          for c in chats) or "| чат | `<chat_id>` |"
    chat_args = " ".join(f"--chat {s}" for s in registry.chat_specs(d)) or "--chat <chat_id>"
    vals = {
        "title": d.get("title") or d["name"], "name": d["name"], "chat_rows": chat_rows, "chat": first,
        "chat_args": chat_args, "state": rel(d["state"]), "log": rel(d["log"]), "stdout": rel(d["stdout"]),
        "replies": rel(d["replies"]), "state_win": relw(d["state"]), "log_win": relw(d["log"]),
        "stdout_win": relw(d["stdout"]), "workspace": str(ws), "date": datetime.now().strftime("%d.%m.%Y"),
        "trigger": d.get("trigger") or "/ai", "emoji_id": str(config.CLAUDE_EMOJI_ID),
        "sign": "с подписью «Claude:» и премиум-эмодзи (по умолчанию)" if d.get("sign", True)
                else "без подписи (`--no-sign`) — так решил владелец",
    }
    out = tpl
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def add(name: str, chat, workspace: str = "", title: str = "", *, start_now: bool = False, quiet: int = 0,
        trigger: str = "/ai", sign: bool = True, notes: str = "", adopt: bool = False, state: str = "",
        log: str = "", stdout: str = "", active: bool = True, create_readme: bool = True,
        script: str = "") -> dict:
    """Завести дежурство: воркспейс (папка + notes/ + README из шаблона, если README нет), запись в реестр,
    по желанию — старт слушателя. adopt=True — подхватить уже работающий слушатель с его путями."""
    name = registry.validate_name(name)
    chats = registry.parse_chat_specs(chat)
    if not chats:
        raise DutyError("нужен хотя бы один чат: id (супергруппы как -100…), @username или подстрока названия")
    ws = _workspace_path(name, workspace)
    notes_dir = ws / "notes"

    def ap(p, default):
        if not p:
            return str(notes_dir / default)
        pp = Path(p)
        return str(pp if pp.is_absolute() else (ws / pp))

    d = {
        "name": name, "title": title.strip() or None, "chats": chats, "workspace": str(ws),
        "state": ap(state, f"{name}_watch_state.json"), "log": ap(log, f"{name}_incoming.log"),
        "stdout": ap(stdout, f"{name}_watch_stdout.log"), "replies": str(notes_dir / f"{name}_replies.jsonl"),
        "pidfile": None, "script": script or None, "trigger": trigger if trigger is not None else "/ai",
        "quiet": int(quiet or 0), "sign": bool(sign), "extra_args": [], "active": bool(active), "agent": None,
        "monitor_command": None, "notes": notes or "", "status_note": "", "created_at": registry.now_iso(),
    }
    # названия чатов — из state, если слушатель уже их разрешил
    try:
        sd = json.loads(Path(d["state"]).read_text(encoding="utf-8"))
        titles = {str(k): v.get("title") for k, v in (sd.get("chats") or {}).items()}
        for c in d["chats"]:
            if c.get("id") is not None and titles.get(str(c["id"])):
                c["title"] = titles[str(c["id"])]
    except Exception:  # noqa: BLE001
        pass
    if not d["title"]:
        d["title"] = " + ".join(c.get("title") or c["spec"] for c in d["chats"])

    def _ins(data):
        duties = data.setdefault("duties", {})
        if name in duties:
            raise DutyError(f"дежурство «{name}» уже есть в реестре")
        for other in duties.values():
            if norm_path(other.get("state")) == norm_path(d["state"]):
                raise DutyError(f"state-файл {d['state']} уже у дежурства «{other.get('name')}»")
        duties[name] = d

    created = {"workspace": not ws.exists(), "readme": False}
    if adopt:
        if not ws.is_dir():
            raise DutyError(f"нет папки воркспейса {ws}")
    else:  # новый воркспейс: папка + notes/ (у подхваченного слушателя всё уже есть — ничего не создаём)
        ws.mkdir(parents=True, exist_ok=True)
        notes_dir.mkdir(parents=True, exist_ok=True)
    registry.update(_ins)
    readme = ws / "README.md"
    if create_readme and not readme.exists():
        readme.write_text(render_readme(d), encoding="utf-8")
        created["readme"] = True
    mc = monitor_command(name)
    registry.update(lambda data: data["duties"][name].__setitem__("monitor_command", mc["command"]))
    _log_action("add", name, adopt=adopt, workspace=str(ws))
    res = {"ok": True, "duty": name, "title": d["title"], "workspace": str(ws), "readme": str(readme),
           "created": created, "paths": {k: d[k] for k in ("state", "log", "stdout", "replies")},
           "monitor_command": mc, "reminder": MONITOR_REMINDER + " Новый воркспейс — зарегистрируй в "
           "claude-workspace/README.md (правило воркспейса)."}
    if start_now and not adopt:
        res["start"] = start(name)
    return res


def remove(name: str, stop_listener: bool = False) -> dict:
    data, d = _duty(name)
    alive, _ = _my_listeners(d, data)
    stopped = None
    if alive:
        if not stop_listener:
            raise DutyError(f"слушатель «{name}» работает — сначала duty_stop(\"{name}\") "
                            "или remove(…, stop_listener=True)")
        stopped = stop(name)
    registry.update(lambda data: data["duties"].pop(name, None))
    _log_action("remove", name)
    return {"ok": True, "removed": name, "stopped": stopped,
            "note": "файлы воркспейса (README, state, журналы) не удалялись"}


def set_active(name: str, active: bool) -> dict:
    registry.get(registry.load(), name)
    registry.update(lambda data: data["duties"][name].__setitem__("active", bool(active)))
    return {"ok": True, "duty": name, "active": bool(active)}


# ----------------------------------------------------------- агент/ответы --

def attach(name: str, session_id: str = "", session_title: str = "") -> dict:
    """Агент сообщает, что дежурит он. Без session_id берётся сессия, из которой пришёл вызов (окружение
    MCP-сервера: CLAUDE_CODE_HOST_SESSION_ID / CLAUDE_CODE_SESSION_ID)."""
    registry.get(registry.load(), name)
    env = sessions.current_from_env()
    sid = (session_id or "").strip() or env["session_id"] or env["cli_session_id"]
    if not sid:
        raise DutyError("не удалось определить сессию: передай session_id (local_<uuid> из ссылки на сессию)")
    if sid.startswith("local_"):
        local, cli = sid, (env["cli_session_id"] if sid == env["session_id"] else None)
    else:
        cli = sid
        local = env["session_id"] if cli == env["cli_session_id"] else None
    desc = sessions.describe(local, cli)
    agent = {"session_id": desc.get("session_id"), "cli_session_id": desc.get("cli_session_id"),
             "title": (session_title or "").strip() or desc.get("title"), "link": desc.get("link"),
             "attached_at": registry.now_iso()}
    registry.update(lambda data: data["duties"][name].__setitem__("agent", agent))
    _log_action("attach", name, session=agent["session_id"] or agent["cli_session_id"])
    return {"ok": True, "duty": name, "agent": agent, "monitor_command": monitor_command(name)}


def handled(name: str, up_to_id: int = 0, chat_id=None, note: str = "") -> dict:
    """Пометить входящие до id (включительно) обработанными без ответа. up_to_id=0 — всё, что есть сейчас."""
    data, d = _duty(name)
    if not up_to_id:
        lines, _, _ = status.read_tail_lines(d["log"], status.LOG_BYTES) if d.get("log") else ([], None, 0)
        cid = (d.get("chats") or [{}])[0].get("id") if len(d.get("chats") or []) == 1 else None
        inc = parse_incoming(lines, cid)
        if chat_id:
            inc = [r for r in inc if str(r.get("chat_id")) == str(chat_id)]
        up_to_id = max((r["id"] for r in inc), default=0)
    if not up_to_id:
        return {"ok": False, "error": "нет входящих — нечего помечать"}
    path = journal.record_handled(d, up_to_id=int(up_to_id), chat_id=int(chat_id) if chat_id else None, note=note)
    return {"ok": True, "duty": name, "up_to_id": int(up_to_id), "chat_id": chat_id, "journal": str(path)}


def tail(name: str, lines: int = 30, show_text: bool = False) -> dict:
    """Последние строки stdout слушателя; тексты сообщений (строки NEW) по умолчанию скрыты."""
    _, d = _duty(name)
    n = max(1, min(int(lines or 30), 500))
    if not d.get("stdout"):
        raise DutyError(f"у «{name}» не задан stdout")
    raw, mtime, size = status.read_tail_lines(d["stdout"])
    out = [mask_line(x, show_text) for x in raw[-n:]]
    return {"duty": name, "file": d["stdout"], "lines": out, "masked": not show_text, "size": size,
            "updated": datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None}


def open_readme(name: str) -> dict:
    _, d = _duty(name)
    p = Path(d["workspace"]) / "README.md"
    if not p.is_file():
        raise DutyError(f"нет файла {p}")
    os.startfile(str(p))  # type: ignore[attr-defined]  # только Windows
    return {"ok": True, "opened": str(p)}
