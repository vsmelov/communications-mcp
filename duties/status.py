"""Статус дежурств и тревоги — только локальные файлы и процессы, без сетевых вызовов в Telegram.

На дежурство смотрим с четырёх сторон:
1. слушатель — процесс python …/watch_chat.py с этим --state (жив? дубль? запущен до правки кода?);
2. его stdout — пульс ALIVE (раз в 10 мин), связь (READY/ConnectionError/Error), пачки BATCH;
3. агент — Monitor-обёртка сессии Claude (`bash -c "… eval '…<путь к stdout>…'"`, родитель — claude.exe),
   из её окружения — id сессии; плюс маркер «до какой строки агент дочитал лог» от нашей команды Monitor;
4. переписка — журнал входящих (--log) против журнала ответов (send_reply.py → notes/<имя>_replies.jsonl),
   исходящих в дампе tg-archive и черновиков notes/reply*.txt — «ожидает ответа».

Тексты сообщений нигде не читаются в результат: из журналов берутся только id, даты и флаги.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import config, registry, sessions
from .procs import CLAUDE_NAMES, CMD_NAMES, PIPE_NAMES, PY_NAMES, SHELL_NAMES, Snapshot, take_snapshot
from .textparse import (chat_key, compute_pending, is_shell_wrapper, norm_cmd, norm_path, parse_incoming,
                        parse_jsonl, parse_listener_argv, parse_stdout, parse_stdout_redirect, resolve_path)

TAIL_BYTES = 256 * 1024
LOG_BYTES = 8 * 1024 * 1024
ARCHIVE_DUMP_MAX = 40 * 1024 * 1024
GENERIC_LOG_NAMES = {"stdout.log", "watch_stdout.log", "incoming.log", "watch.out", "out.log", "log.txt"}

_file_cache: dict[tuple, object] = {}


# ------------------------------------------------------------- файлы -------

def _stat(path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


def read_tail_lines(path, max_bytes: int = TAIL_BYTES) -> tuple[list[str], float | None, int]:
    """Последние строки файла (до max_bytes), mtime, размер. Кэш по (путь, mtime, размер)."""
    st = _stat(path)
    if st is None:
        return [], None, 0
    key = ("tail", str(path), st.st_mtime_ns, st.st_size, max_bytes)
    hit = _file_cache.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    with open(path, "rb") as f:
        if st.st_size > max_bytes:
            f.seek(st.st_size - max_bytes)
            raw = f.read()
            raw = raw.split(b"\n", 1)[1] if b"\n" in raw else raw
        else:
            raw = f.read()
    lines = raw.decode("utf-8", "replace").splitlines()
    res = (lines, st.st_mtime, st.st_size)
    _remember(key, res)
    return res


def count_lines(path) -> int:
    st = _stat(path)
    if st is None:
        return 0
    key = ("count", str(path), st.st_mtime_ns, st.st_size)
    hit = _file_cache.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    _remember(key, n)
    return n


def _remember(key, value) -> None:
    if len(_file_cache) > 400:
        _file_cache.clear()
    _file_cache[key] = value


# ------------------------------------------------------- слушатели ---------

def find_listeners(snap: Snapshot, scripts: set[str]) -> list[dict]:
    """Все процессы-слушатели (python … <script> …), склеенные с venv-лаунчером и обёрткой cmd.exe."""
    cands: dict[int, tuple] = {}
    for p in snap.procs.values():
        if p.name not in PY_NAMES or not p.argv:
            continue
        args = parse_listener_argv(p.argv, scripts)
        if not args or args.get("print_filter"):
            continue
        cands[p.pid] = (p, args)
    out = []
    used: set[int] = set()
    for pid, (p, args) in sorted(cands.items(), key=lambda kv: kv[1][0].create_time):
        if pid in used:
            continue
        par = snap.parent(p)
        if par is not None and par.pid in cands and cands[par.pid][1].get("state") == args.get("state"):
            continue  # ребёнок venv-лаунчера: войдёт в группу родителя
        group = [p]
        used.add(pid)
        stack = [p]
        while stack:
            q = stack.pop()
            for ch in snap.children(q.pid):
                if ch.pid in cands and ch.pid not in used and cands[ch.pid][1].get("state") == args.get("state"):
                    group.append(ch)
                    used.add(ch.pid)
                    stack.append(ch)
        real = group[-1]
        cwd = snap.cwd(real.pid) or snap.cwd(p.pid)
        wrapper = snap.parent(p)
        if not (wrapper is not None and wrapper.name in CMD_NAMES
                and any(s in wrapper.cmdline.lower() for s in scripts)):
            wrapper = None
        redirect = parse_stdout_redirect(wrapper.cmdline) if wrapper else None
        env = snap.environ(p.pid) or snap.environ(real.pid)
        out.append({
            "pid": p.pid, "pids": [q.pid for q in group], "create_time": p.create_time,
            "script": Path(args["script"]).name, "args": args, "cwd": cwd,
            "state_abs": resolve_path(args.get("state"), cwd),
            "log_abs": resolve_path(args.get("log"), cwd),
            "stdout_abs": resolve_path(redirect, cwd) if redirect else None,
            "wrapper_pid": wrapper.pid if wrapper else None,
            "wrapper_ct": wrapper.create_time if wrapper else None,
            "started_by": (env.get("DUTIES_STARTED_BY") or env.get("CLAUDE_CODE_HOST_SESSION_ID")
                           or env.get("CLAUDE_CODE_SESSION_ID")),
        })
    return out


def started_by_title(started_by: str | None) -> str | None:
    if not started_by:
        return None
    if started_by == "dashboard":
        return "дашборд"
    if started_by == "local":
        return "командная строка"
    return (sessions.session_meta(started_by) or {}).get("title")


def listener_matches(lst: dict, d: dict) -> bool:
    """Слушатель принадлежит дежурству, если совпадает его --state (абсолютный путь, иначе хвост пути)."""
    if lst.get("state_abs"):
        return norm_path(lst["state_abs"]) == norm_path(d.get("state"))
    rel = norm_path(lst["args"].get("state") or "").lstrip("./")
    return bool(rel) and norm_path(d.get("state")).endswith("/" + rel)


# ---------------------------------------------------- агенты (Monitor) -----

def log_patterns(data: dict) -> dict[str, list[str]]:
    """Как узнать stdout дежурства в командной строке: абсолютный путь + имя файла, если оно уникально."""
    duties = data.get("duties") or {}
    names: dict[str, int] = {}
    for d in duties.values():
        if d.get("stdout"):
            b = Path(d["stdout"]).name.lower()
            names[b] = names.get(b, 0) + 1
    out = {}
    for n, d in duties.items():
        if not d.get("stdout"):
            continue
        pats = [norm_path(d["stdout"])]
        b = Path(d["stdout"]).name.lower()
        if names.get(b) == 1 and b not in GENERIC_LOG_NAMES:
            pats.append(b)
        out[n] = pats
    return out


def find_watchers(snap: Snapshot, patterns: dict[str, list[str]]) -> tuple[dict, dict]:
    """Monitor-обёртки и процессы tail/grep/awk по каждому дежурству + глобальные сироты.

    Живой Monitor = верхняя обёртка `bash -c` (её убивают при истечении/остановке Monitor), чей родитель —
    живой процесс CLI Claude. tail/grep без живой обёртки — сироты прошлых Monitor (на Windows они переживают
    убийство обёртки; наша команда Monitor лечит это `tail --pid=$$`)."""
    per = {n: {"wrappers": [], "pipes": []} for n in patterns}
    health = []
    for p in snap.procs.values():
        if (p.name not in SHELL_NAMES and p.name not in PIPE_NAMES) or not p.cmdline:
            continue
        nc = norm_cmd(p.cmdline)
        if p.name in SHELL_NAMES and "watch_health.sh" in nc and not is_shell_wrapper(p.cmdline):
            health.append(p)
        for n, pats in patterns.items():
            if any(pt in nc for pt in pats):
                if p.name in SHELL_NAMES:
                    if is_shell_wrapper(p.cmdline):
                        per[n]["wrappers"].append(p)
                else:
                    per[n]["pipes"].append(p)
    result = {}
    orphan_pipes_total = 0
    for n, d in per.items():
        wpids = {w.pid for w in d["wrappers"]}
        monitors = []
        for w in d["wrappers"]:
            if w.ppid in wpids:
                continue  # вложенный bash той же обёртки (Git\bin\bash.exe → usr\bin\bash.exe)
            par = snap.parent(w)
            live = par is not None and par.name in CLAUDE_NAMES
            env = snap.environ(w.pid)
            if not env:
                for c in d["wrappers"]:
                    if c.ppid == w.pid:
                        env = snap.environ(c.pid)
                        if env:
                            break
            cp = env.get("CLAUDE_PID", "")
            monitors.append({
                "pid": w.pid, "create_time": w.create_time, "live": live,
                "parent_pid": w.ppid, "parent": par.name if par else None,
                "session_id": env.get("CLAUDE_CODE_HOST_SESSION_ID"),
                "cli_session_id": env.get("CLAUDE_CODE_SESSION_ID"),
                "claude_pid": int(cp) if cp.isdigit() else (par.pid if live and par else None),
                "claude_ct": (par.create_time if live and par else None),
            })
        live_m = [m for m in monitors if m["live"]]
        live_pipes = orphan = 0
        orphan_pids = []
        for q in d["pipes"]:
            if any(m["create_time"] - 2 <= q.create_time <= m["create_time"] + 30 for m in live_m):
                live_pipes += 1
            else:
                orphan += 1
                orphan_pids.append(q.pid)
        orphan_pipes_total += orphan
        result[n] = {"monitors": monitors, "pipes_live": live_pipes, "pipes_orphan": orphan,
                     "orphan_pids": orphan_pids[:50]}
    glob_info = {"pipes_orphan": orphan_pipes_total,
                 "health_loops": len(health),
                 "health_orphans": sum(1 for h in health if not snap.parent_alive(h))}
    return result, glob_info


def claude_sessions_running(snap: Snapshot) -> dict[str, dict]:
    """Живые процессы CLI Claude: local_<uuid> → {pid, create_time}."""
    out = {}
    for p in snap.procs.values():
        if p.name not in CLAUDE_NAMES or "--output-format" not in p.cmdline:
            continue
        env = snap.environ(p.pid)
        sid = env.get("CLAUDE_CODE_HOST_SESSION_ID")
        if sid:
            out[sid] = {"pid": p.pid, "create_time": p.create_time}
    return out


# ------------------------------------------------------------ архив --------

def archive_index(root: Path | None = None) -> dict:
    """Дампы tg-archive: ключ чата → {dir, updated_ts, messages}. Только manifest.json (без переписки)."""
    root = Path(root or config.archive_root())
    out: dict = {"root": str(root), "chats": {}, "newest_ts": None}
    if not root.is_dir():
        return out
    newest = None
    for d in root.iterdir():
        man = d / "manifest.json"
        st = _stat(man)
        if st is None:
            continue
        key = ("manifest", str(man), st.st_mtime_ns)
        m = _file_cache.get(key)
        if m is None:
            try:
                raw = json.loads(man.read_text(encoding="utf-8"))
                m = {"chat_id": raw.get("chat_id"), "updated_ts": raw.get("updated_ts") or st.st_mtime,
                     "messages": raw.get("messages")}
            except Exception:  # noqa: BLE001
                m = {"chat_id": None, "updated_ts": st.st_mtime, "messages": None}
            _remember(key, m)
        ck = chat_key(m.get("chat_id"))
        if ck is None:
            prefix = d.name.split("-", 1)[0]
            ck = int(prefix) if prefix.isdigit() else None
        if ck is None:
            continue
        prev = out["chats"].get(ck)
        if prev is None or (m.get("messages") or 0) > (prev.get("messages") or 0):
            out["chats"][ck] = {"dir": str(d), "updated_ts": m.get("updated_ts"), "messages": m.get("messages")}
        if m.get("updated_ts") and (newest is None or m["updated_ts"] > newest):
            newest = m["updated_ts"]
    out["newest_ts"] = newest
    return out


def archive_outgoing_max(dump_dir: str, exclude_ids: set[int]) -> int | None:
    """Максимальный id исходящего в безопасном дампе (dump.json) — без чтения текстов в результат.

    Сообщения владельца с /ai (они тоже «исходящие») не считаются ответом — их id исключаются."""
    p = Path(dump_dir) / "dump.json"
    st = _stat(p)
    if st is None or st.st_size > ARCHIVE_DUMP_MAX:
        return None
    key = ("dumpout", str(p), st.st_mtime_ns, st.st_size)
    ids = _file_cache.get(key)
    if ids is None:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            ids = sorted({int(r["id"]) for r in raw.get("messages", []) if r.get("outgoing") and isinstance(r.get("id"), int)})
        except Exception:  # noqa: BLE001
            ids = []
        _remember(key, ids)
    cand = [i for i in ids if i not in exclude_ids]  # type: ignore[union-attr]
    return max(cand) if cand else None


# ------------------------------------------------------ кэш «агента видели» -

def _seen_path() -> Path:
    return config.runtime_dir() / "agents_seen.json"


def load_seen() -> dict:
    try:
        return json.loads(_seen_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_seen(d: dict) -> None:
    p = _seen_path()
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def marker_path(name: str) -> Path:
    """Маркер нашей команды Monitor: номер последней строки stdout, которую видел агент."""
    return config.runtime_dir() / f"{name}.monitor_line"


# --------------------------------------------------------------- статус ----

def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _hm(ts: float | None) -> str | None:
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%d.%m %H:%M")


def _dur(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = max(0, int(sec))
    if sec < 90:
        return f"{sec} с"
    if sec < 5400:
        return f"{round(sec / 60)} мин"
    if sec < 2 * 86400:
        h, m = divmod(round(sec / 60), 60)
        return f"{h} ч {m} мин" if m else f"{h} ч"
    d, h = divmod(round(sec / 3600), 24)
    return f"{d} д {h} ч" if h else f"{d} д"


def script_mtime(d: dict) -> float | None:
    sp = registry.script_path(d)
    ts = [st.st_mtime for st in (_stat(sp),) if st]
    if sp.name.lower() == config.WATCH_SCRIPT.name.lower():
        st = _stat(config.NETPATH)
        if st:
            ts.append(st.st_mtime)
    return max(ts) if ts else None


def read_state(path) -> dict[int, dict]:
    """state слушателя: ключ чата → {title, last_seen_id, last_emit_at}. Понимает и старый формат 92_watch_anna.py
    (chat_id / chat_title / last_seen_id на верхнем уровне)."""
    st = _stat(path) if path else None
    if st is None:
        return {}
    key = ("state", str(path), st.st_mtime_ns, st.st_size)
    hit = _file_cache.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    out: dict[int, dict] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw.get("chats"), dict):
            for k, v in raw["chats"].items():
                ck = chat_key(k)
                if ck is not None and isinstance(v, dict):
                    out[ck] = {"title": v.get("title"), "last_seen_id": v.get("last_seen_id"),
                               "last_emit_at": v.get("last_emit_at")}
        elif raw.get("chat_id") is not None:
            ck = chat_key(raw["chat_id"])
            if ck is not None:
                out[ck] = {"title": raw.get("chat_title"), "last_seen_id": raw.get("last_seen_id"), "last_emit_at": None}
    except Exception:  # noqa: BLE001
        out = {}
    _remember(key, out)
    return out


def _drafts_ts(workspace: str | None) -> float | None:
    if not workspace:
        return None
    notes = Path(workspace) / "notes"
    if not notes.is_dir():
        return None
    best = None
    for p in notes.glob("reply*.txt"):
        st = _stat(p)
        if st and (best is None or st.st_mtime > best):
            best = st.st_mtime
    return best


def _alert(level: str, code: str, text: str) -> dict:
    return {"level": level, "code": code, "text": text}


def duty_status(d: dict, ctx: dict) -> dict:
    now = ctx["now"]
    st = ctx["settings"]
    name = d["name"]
    active = bool(d.get("active", True))
    alerts: list[dict] = []

    # --- слушатель
    mine = [lst for lst in ctx["listeners"] if listener_matches(lst, d)]
    listener: dict = {"state": "dead", "pids": [], "count": len(mine)}
    lst = None
    if mine:
        lst = sorted(mine, key=lambda x: x["create_time"])[-1]
        sm = script_mtime(d)
        outdated = bool(sm and lst["create_time"] and lst["create_time"] < sm - 1)
        listener = {
            "state": "duplicate" if len(mine) > 1 else "alive",
            "pid": lst["pid"], "pids": [p for m in mine for p in m["pids"]], "count": len(mine),
            "wrapper_pid": lst["wrapper_pid"], "started_at": _iso(lst["create_time"]),
            "uptime": _dur(now - lst["create_time"]) if lst["create_time"] else None,
            "uptime_sec": int(now - lst["create_time"]) if lst["create_time"] else None,
            "outdated": outdated, "script_changed_at": _hm(sm) if outdated else None,
            "script": lst["script"], "started_by": lst.get("started_by"),
            "started_by_title": started_by_title(lst.get("started_by")),
            "args": {k: lst["args"].get(k) for k in ("chats", "quiet", "trigger")},
        }

    # --- stdout: связь, пульс, пачки
    stdout = d.get("stdout")
    lines, mtime, size = read_tail_lines(stdout) if stdout else ([], None, 0)
    ps = parse_stdout(lines, mtime)
    total_lines = count_lines(stdout) if stdout and mtime else 0
    hb_ts = ps["heartbeat_ts"]
    hb_note = None
    if lst and hb_ts and lst["create_time"] and hb_ts < lst["create_time"] - 5:
        hb_ts = None
        hb_note = "с момента запуска слушателя пульса не было"
    hb_age = (now - hb_ts) if hb_ts else None
    if hb_age is None:
        hb_level = "none"
    elif hb_age <= st["heartbeat_green_min"] * 60:
        hb_level = "green"
    elif hb_age <= st["heartbeat_yellow_min"] * 60:
        hb_level = "yellow"
    else:
        hb_level = "red"
    heartbeat = {"level": hb_level, "ts": hb_ts, "at": _hm(hb_ts), "age": _dur(hb_age),
                 "age_sec": int(hb_age) if hb_age is not None else None, "kind": ps["heartbeat_kind"],
                 "has_alive": ps["has_alive"], "note": hb_note,
                 "last_line_at": _hm(mtime), "last_line_age": _dur(now - mtime) if mtime else None}
    conn = dict(ps["connection"])
    since_approx = bool(conn.get("since_approx")) and conn.get("state") == "offline"
    if lst and mtime and lst["create_time"] and mtime < lst["create_time"] - 2:
        conn = {"state": "starting", "via": None, "since": lst["create_time"],
                "detail": "слушатель запущен и ещё ничего не написал в stdout"}
    elif lst and lst["create_time"]:
        if conn.get("since") and conn["since"] < lst["create_time"]:
            conn["since"] = lst["create_time"]  # серия началась в прошлом запуске — считаем от старта процесса
        if conn.get("state") == "offline" and not conn.get("since"):
            # у строк Error нет времени: связи нет как минимум с последнего пульса / старта процесса
            conn["since"] = max(lst["create_time"], hb_ts or 0)
            since_approx = True
    connection = {"state": conn.get("state"), "via": conn.get("via"), "since_at": _hm(conn.get("since")),
                  "for": _dur(now - conn["since"]) if conn.get("since") else None,
                  "for_sec": int(now - conn["since"]) if conn.get("since") else None,
                  "since_approx": since_approx, "detail": conn.get("detail")}

    # --- маркер нашей команды Monitor
    mk = marker_path(name)
    marker = None
    mst = _stat(mk)
    if mst:
        try:
            ml = int(mk.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            ml = None
        if ml is not None:
            behind = max(0, total_lines - ml)
            behind_batches = 0
            if 0 < behind <= len(lines):
                behind_batches = sum(1 for ln in lines[-behind:] if ln.startswith("BATCH "))
            marker = {"line": ml, "updated_at": _hm(mst.st_mtime), "behind_lines": behind,
                      "behind_batches": behind_batches}

    # --- агент
    w = ctx["watchers"].get(name, {"monitors": [], "pipes_live": 0, "pipes_orphan": 0})
    seen_all = ctx["seen"]
    live_m = [m for m in w["monitors"] if m["live"]]
    attached = d.get("agent") or None
    running = ctx["claude_running"]
    monitors_out = []
    for m in live_m:
        desc = sessions.describe(m.get("session_id"), m.get("cli_session_id"))
        monitors_out.append({"pid": m["pid"], "since": _hm(m["create_time"]), "session_id": desc.get("session_id"),
                             "cli_session_id": desc.get("cli_session_id"), "title": desc.get("title"),
                             "link": desc.get("link")})
    seen = seen_all.get(name)
    if live_m:
        m0 = live_m[0]
        rec = {"session_id": m0.get("session_id"), "cli_session_id": m0.get("cli_session_id"),
               "claude_pid": m0.get("claude_pid"), "claude_ct": m0.get("claude_ct"), "seen_at": now,
               "monitor_pid": m0["pid"]}
        if not seen or now - seen.get("seen_at", 0) > 30 or seen.get("monitor_pid") != m0["pid"]:
            seen_all[name] = rec
            ctx["seen_dirty"] = True
        seen = rec
        agent_state = "connected"
    else:
        sess_alive = False
        sid = (seen or {}).get("session_id") or (attached or {}).get("session_id")
        if sid and sid in running:
            sess_alive = True
        elif seen and seen.get("claude_pid") and ctx["snap"].alive(seen["claude_pid"], seen.get("claude_ct")):
            sess_alive = True
        recent = bool(seen and now - seen.get("seen_at", 0) <= st["rearm_grace_min"] * 60)
        agent_state = "rearming" if (recent and sess_alive) else ("idle" if sess_alive else "none")
    att_out = None
    if attached:
        att_out = dict(attached)
        att_out["alive"] = bool(attached.get("session_id") and attached["session_id"] in running)
        meta = sessions.session_meta(attached.get("session_id"))
        if meta:
            att_out["title"] = meta.get("title") or attached.get("title")
            att_out["archived"] = meta.get("archived")
            att_out["last_activity"] = meta.get("last_activity")
    seen_desc = None
    if seen:
        sd = sessions.describe(seen.get("session_id"), seen.get("cli_session_id"))
        seen_desc = {"session_id": sd.get("session_id"), "title": sd.get("title"), "link": sd.get("link"),
                     "at": _hm(seen.get("seen_at")), "ago": _dur(now - seen.get("seen_at", now))}
    agent = {"state": agent_state, "monitors": monitors_out, "attached": att_out, "last_seen": seen_desc,
             "marker": marker, "orphan_pipes": w.get("pipes_orphan", 0)}

    # --- переписка
    chats = d.get("chats") or []
    default_cid = chats[0].get("id") if len(chats) == 1 else None
    log_lines, log_mtime, _ = read_tail_lines(d["log"], LOG_BYTES) if d.get("log") else ([], None, 0)
    incoming = parse_incoming(log_lines, default_cid)
    today = datetime.now().strftime("%Y-%m-%d")
    last_in = max(incoming, key=lambda r: (r["ts"] or 0, r["id"])) if incoming else None
    rep_lines, _, _ = read_tail_lines(d["replies"], LOG_BYTES) if d.get("replies") else ([], None, 0)
    replies = parse_jsonl(rep_lines)
    owner_ids = {r["id"] for r in incoming if r.get("owner_call")}
    arch = ctx["archive"]
    archive_out: dict = {}
    chats_out = []
    state = read_state(d.get("state"))
    for c in chats:
        ck = chat_key(c.get("id"))
        a = arch["chats"].get(ck) if ck is not None else None
        if a:
            mx = archive_outgoing_max(a["dir"], owner_ids)
            if mx:
                archive_out[ck] = mx
        si = state.get(ck) or {}
        chats_out.append({"id": c.get("id"), "title": c.get("title") or si.get("title") or c.get("spec"),
                          "spec": c.get("spec"), "last_seen_id": si.get("last_seen_id"),
                          "last_emit_at": si.get("last_emit_at"),
                          "archive": ({"updated_at": _hm(a.get("updated_ts")),
                                       "age": _dur(now - a["updated_ts"]) if a.get("updated_ts") else None,
                                       "age_sec": int(now - a["updated_ts"]) if a.get("updated_ts") else None,
                                       "messages": a.get("messages")} if a else None)})
    drafts = _drafts_ts(d.get("workspace"))
    pending = compute_pending(incoming, replies, archive_out=archive_out, drafts_ts=drafts,
                              chat_ids=[c.get("id") for c in chats], now=now)
    last_reply = None
    reps = [r for r in replies if r.get("kind", "reply") == "reply"]
    if reps:
        lr = max(reps, key=lambda r: r.get("ts") or 0)
        last_reply = {"at": _hm(lr.get("ts")), "session": lr.get("session"), "msg_id": lr.get("msg_id")}
    traffic = {"today": sum(1 for r in incoming if (r.get("date") or "").startswith(today)),
               "total": len(incoming), "owner_calls": len(owner_ids),
               "last_at": _hm(last_in["ts"]) if last_in else None, "last_id": last_in["id"] if last_in else None,
               "last_ts": last_in["ts"] if last_in else None, "replies_total": len(reps), "last_reply": last_reply}
    pend = {**pending, "oldest_at": _hm(pending.get("oldest_ts")), "last_reply_at": _hm(pending.get("last_reply_ts"))}

    # --- тревоги (только для активных дежурств)
    if active:
        if listener["state"] == "dead":
            alerts.append(_alert("red", "listener_dead", "Слушатель не запущен — сообщения не собираются"))
        elif listener["state"] == "duplicate":
            alerts.append(_alert("red", "listener_duplicate",
                                 f"{listener['count']} слушателя на один state-файл (PID {', '.join(map(str, listener['pids']))}) "
                                 "— дерутся за копию сессии Telegram; лишний остановить"))
        if lst:
            if listener.get("outdated"):
                alerts.append(_alert("yellow", "listener_outdated",
                                     f"Слушатель запущен {_hm(lst['create_time'])}, до правки кода слушателя "
                                     f"({_hm(script_mtime(d))}) — работает старая версия, перезапуск подхватит правки"))
            if hb_level == "none":
                alerts.append(_alert("red", "heartbeat_none",
                                     "Пульса нет: в stdout нет ни ALIVE, ни READY"
                                     + (" — " + hb_note if hb_note else "")))
            elif hb_level in ("yellow", "red"):
                alerts.append(_alert(hb_level, "heartbeat_stale",
                                     f"Нет пульса {heartbeat['age']} (последний в {heartbeat['at']})"))
            if connection["state"] == "offline":
                long_ = (connection.get("for_sec") is None
                         or connection["for_sec"] > st["connection_grace_min"] * 60)
                alerts.append(_alert("red" if long_ else "yellow", "connection_lost",
                                     "Связь с Telegram потеряна"
                                     + (f" {'не менее ' if since_approx else ''}{connection['for']} назад "
                                        f"(с {connection['since_at']})" if connection.get("for") else "")
                                     + (f": {connection['detail'][:140]}" if connection.get("detail") else "")))
            if agent_state == "none":
                alerts.append(_alert("red", "agent_missing", "Слушатель жив, а агента нет — никто не проснётся"))
            elif agent_state == "idle":
                who = (seen_desc or {}).get("title") or (att_out or {}).get("title") or "агента"
                alerts.append(_alert("red", "agent_idle",
                                     f"Сессия «{who}» открыта, но Monitor не запущен — на сообщения никто не проснётся"))
            elif agent_state == "rearming":
                alerts.append(_alert("yellow", "agent_rearming",
                                     f"Monitor перевзводится (последний раз видели {seen_desc['ago'] if seen_desc else '?'} назад)"))
        elif agent_state == "connected":
            alerts.append(_alert("yellow", "agent_without_listener", "Агент ждёт событий, но слушатель не запущен"))
        if marker and marker["behind_batches"] and agent_state != "connected":
            alerts.append(_alert("red", "undelivered",
                                 f"Агент не видел {marker['behind_batches']} пачек (BATCH) — придут при перевзводе Monitor"))
        elif (not marker and seen and agent_state in ("none", "idle") and traffic["last_ts"]
              and traffic["last_ts"] > seen.get("seen_at", now)):
            n = sum(1 for r in incoming if (r.get("ts") or 0) > seen.get("seen_at", now))
            alerts.append(_alert("red", "undelivered", f"Без агента пришло {n} сообщ. (после {seen_desc['at']})"))
        if pending["count"] and pending.get("oldest_min") is not None:
            if pending["oldest_min"] > st["unanswered_alert_min"]:
                alerts.append(_alert("red", "unanswered",
                                     f"Ожидает ответа: {pending['count']}, самое старое {_dur(pending['oldest_min'] * 60)}"
                                     + (" (≈ по черновикам)" if pending.get("approx") else "")))
        for c in chats_out:
            a = c.get("archive")
            if a and a.get("age_sec") and a["age_sec"] > st["archive_stale_min"] * 60:
                alerts.append(_alert("yellow", "archive_stale",
                                     f"Дамп tg-archive по «{c['title']}» не обновлялся {a['age']} — архив не пишется (Docker?)"))
                break
    else:
        if listener["state"] != "dead":
            alerts.append(_alert("yellow", "inactive_running", "Дежурство в архиве, а слушатель работает"))

    level = "grey" if not active else ("red" if any(a["level"] == "red" for a in alerts)
                                         else "yellow" if any(a["level"] == "yellow" for a in alerts) else "green")
    ws = d.get("workspace")
    return {
        "name": name, "title": d.get("title") or name, "active": active, "level": level,
        "chats": chats_out, "workspace": ws,
        "readme": str(Path(ws) / "README.md") if ws and (Path(ws) / "README.md").is_file() else None,
        "paths": {k: d.get(k) for k in ("state", "log", "stdout", "replies", "pidfile")},
        "listener": listener, "heartbeat": heartbeat, "connection": connection, "agent": agent,
        "traffic": traffic, "pending": pend, "alerts": alerts,
        "stdout": {"lines": total_lines, "size": size, "updated_at": _hm(mtime), "errors_in_tail": ps["errors"],
                   "batches_in_tail": len(ps["batches"])},
        "trigger": d.get("trigger", "/ai"), "quiet": d.get("quiet", 0), "sign": d.get("sign", True),
        "notes": d.get("notes") or "", "status_note": d.get("status_note") or "",
        "created_at": d.get("created_at"),
    }


def unregistered(listeners: list[dict], data: dict, snap: Snapshot) -> list[dict]:
    """Живые слушатели, которых нет в реестре — с подсказками для «завести»."""
    duties = list((data.get("duties") or {}).values())
    out = []
    for lst in listeners:
        if any(listener_matches(lst, d) for d in duties):
            continue
        stdout = lst.get("stdout_abs")
        if not stdout:
            skip = {norm_path(x) for x in (lst.get("state_abs"), lst.get("log_abs")) if x}
            for f in snap.open_files(lst["pids"][-1]):
                if norm_path(f) not in skip and f.lower().endswith((".log", ".out", ".txt")):
                    stdout = f
                    break
        state = lst.get("state_abs") or lst["args"].get("state")
        stem = Path(state or "duty").stem
        suggested = stem.replace("_watch_state", "").replace("watch_state", "").strip("_-").lower()
        ws = lst.get("cwd")
        if len(suggested) < 3 and ws:
            suggested = Path(ws).name.lower()
        out.append({"pid": lst["pid"], "pids": lst["pids"], "started_at": _iso(lst["create_time"]),
                    "chats": lst["args"].get("chats") or [], "state": state, "log": lst.get("log_abs"),
                    "stdout": stdout, "workspace": ws, "script": lst["script"],
                    "quiet": lst["args"].get("quiet"), "trigger": lst["args"].get("trigger"),
                    "suggested_name": suggested or "duty",
                    "started_by": lst.get("started_by"),
                    "started_by_title": started_by_title(lst.get("started_by"))})
    return out


def collect(data: dict | None = None, snap: Snapshot | None = None, now: float | None = None,
            names: list[str] | None = None) -> dict:
    """Полная картина: статус каждого дежурства, незаведённые слушатели, глобальные тревоги."""
    data = data or registry.load()
    snap = snap or take_snapshot()
    now = now or time.time()
    settings = dict(config.DEFAULT_SETTINGS)
    settings.update(data.get("settings") or {})
    scripts = registry.script_names(data)
    listeners = find_listeners(snap, scripts)
    watchers, glob_w = find_watchers(snap, log_patterns(data))
    ctx = {"now": now, "settings": settings, "snap": snap, "listeners": listeners, "watchers": watchers,
           "seen": load_seen(), "seen_dirty": False, "claude_running": claude_sessions_running(snap),
           "archive": archive_index()}
    duties = {}
    for n, d in (data.get("duties") or {}).items():
        if names and n not in names:
            continue
        d = {**d, "name": n}
        try:
            duties[n] = duty_status(d, ctx)
        except Exception as e:  # noqa: BLE001 — одно сломанное дежурство не должно гасить остальные
            duties[n] = {"name": n, "title": d.get("title") or n, "active": d.get("active", True), "level": "red",
                         "alerts": [_alert("red", "status_error", f"Статус не вычислен: {type(e).__name__}: {e}")]}
    if ctx["seen_dirty"]:
        save_seen(ctx["seen"])
    found = unregistered(listeners, data, snap) if not names else []
    galerts = []
    for f in found:
        galerts.append(_alert("yellow", "unregistered",
                              f"Найден слушатель, не заведённый в реестр: PID {f['pid']}, чаты {', '.join(map(str, f['chats']))}"))
    arch = ctx["archive"]
    if arch.get("newest_ts") and now - arch["newest_ts"] > settings["archive_stale_min"] * 60:
        galerts.append(_alert("yellow", "archive_stale",
                              f"tg-archive не обновлял ни одного дампа {_dur(now - arch['newest_ts'])} — архив не пишется (Docker?)"))
    orphans = {"pipes": glob_w["pipes_orphan"], "health_loops": glob_w["health_loops"],
               "health_orphans": glob_w["health_orphans"]}
    active = [s for s in duties.values() if s.get("active")]
    summary = {
        "active": len(active), "total": len(duties),
        "listeners_alive": sum(1 for s in active if (s.get("listener") or {}).get("state") in ("alive", "duplicate")),
        "agents_connected": sum(1 for s in active if (s.get("agent") or {}).get("state") == "connected"),
        "pending": sum((s.get("pending") or {}).get("count") or 0 for s in active),
        "red": sum(1 for s in active for a in s.get("alerts", []) if a["level"] == "red")
               + sum(1 for a in galerts if a["level"] == "red"),
        "yellow": sum(1 for s in active for a in s.get("alerts", []) if a["level"] == "yellow")
                  + sum(1 for a in galerts if a["level"] == "yellow"),
    }
    return {
        "generated_at": _iso(now), "now": now, "summary": summary, "duties": duties, "unregistered": found,
        "alerts": galerts, "orphans": orphans,
        "archive": {"root": arch.get("root"), "newest_at": _hm(arch.get("newest_ts")),
                    "age": _dur(now - arch["newest_ts"]) if arch.get("newest_ts") else None,
                    "chats": len(arch.get("chats") or {})},
        "snapshot": {"method": snap.method, "took_ms": snap.took_ms},
        "settings": settings,
    }
