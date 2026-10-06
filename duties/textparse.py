"""Разбор текстовых артефактов дежурства — чистые функции (тестируются на синтетике).

- пути: нормализация Windows / MSYS (`C:\\a\\b`, `C:/a/b`, `/c/a/b` → `c:/a/b`);
- командные строки: разбиение по правилам Windows, аргументы слушателя (`--chat/--state/--log/...`),
  цель перенаправления stdout у обёртки `cmd.exe /c … >> лог 2>&1`;
- stdout слушателя: строки READY/LISTENING/BATCH/NEW/ALIVE/ConnectionError/Error/Traceback → состояние связи,
  пульс, пачки; у строк ALIVE/READY есть только «чч:мм:сс» — дата восстанавливается от mtime файла назад;
- журнал входящих `--log` (jsonl `NEW {...}`) и журнал ответов `replies.jsonl` — БЕЗ текстов сообщений;
- маскирование строк NEW (тексты переписки по умолчанию не показываются).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- пути -----

_DRIVE_CYG = re.compile(r"^/cygdrive/([a-zA-Z])(?=/|$)")
_DRIVE_MSYS = re.compile(r"^/([a-zA-Z])(?=/|$)")
_CMD_DRIVE = re.compile(r"(?<![\w:./\\-])/(?:cygdrive/)?([a-zA-Z])/")


def norm_path(p) -> str:
    """Ключ для сравнения путей: нижний регистр, прямые слэши, `/c/...` → `c:/...`, без хвостового слэша."""
    s = str(p or "").strip().strip('"').strip("'").replace("\\", "/")
    m = _DRIVE_CYG.match(s) or _DRIVE_MSYS.match(s)
    if m:
        s = f"{m.group(1)}:" + s[m.end():]
    s = re.sub(r"/{2,}", "/", s)
    if len(s) > 3:
        s = s.rstrip("/")
    return s.lower()


def norm_cmd(cmd: str) -> str:
    """Командная строка в той же нормализации, что norm_path: чтобы искать в ней пути подстрокой."""
    s = (cmd or "").replace("\\", "/")
    s = re.sub(r"/{2,}", "/", s)
    s = _CMD_DRIVE.sub(lambda m: f"{m.group(1)}:/", s)
    return s.lower()


def to_msys(p) -> str:
    """`C:\\Users\\v\\x` → `/c/Users/v/x` (для команд Git Bash: обратные слэши там теряются)."""
    s = str(p).replace("\\", "/")
    m = re.match(r"^([a-zA-Z]):/?(.*)$", s)
    if m:
        return f"/{m.group(1).lower()}/{m.group(2)}"
    return s


def resolve_path(p: str | None, cwd: str | None) -> str | None:
    """Абсолютный путь аргумента (относительный — от cwd процесса), в Windows-виде. None — не вычислить."""
    if not p:
        return None
    s = str(p).strip().strip('"')
    m = _DRIVE_CYG.match(s.replace("\\", "/")) or _DRIVE_MSYS.match(s.replace("\\", "/"))
    if m:
        s = f"{m.group(1)}:" + s.replace("\\", "/")[m.end():]
    path = Path(s)
    if path.is_absolute() or re.match(r"^[a-zA-Z]:[\\/]", s):
        return str(Path(s))
    if not cwd:
        return None
    return str((Path(cwd) / s).resolve(strict=False))


def same_path(a, b) -> bool:
    return bool(a) and bool(b) and norm_path(a) == norm_path(b)


# ------------------------------------------------------ командные строки -----

def split_cmdline(s: str) -> list[str]:
    """Разбиение командной строки по правилам MS C runtime (кавычки, обратные слэши перед кавычкой)."""
    args: list[str] = []
    cur: list[str] = []
    inq = False
    have = False
    i, n = 0, len(s or "")
    while i < n:
        c = s[i]
        if c == "\\":
            j = i
            while j < n and s[j] == "\\":
                j += 1
            nb = j - i
            if j < n and s[j] == '"':
                cur.append("\\" * (nb // 2))
                have = True
                if nb % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j
                continue
            cur.append("\\" * nb)
            have = True
            i = j
            continue
        if c == '"':
            if inq and i + 1 < n and s[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            inq = not inq
            have = True
            i += 1
            continue
        if c in " \t" and not inq:
            if have:
                args.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        cur.append(c)
        have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


def _basename(a: str) -> str:
    return a.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def find_script_index(argv: list[str], scripts) -> int | None:
    names = {s.lower() for s in scripts}
    for i, a in enumerate(argv):
        if _basename(a.strip('"')) in names:
            return i
    return None


_VALUE_OPTS = {"--chat", "--state", "--log", "--quiet", "--trigger", "--session", "--ack"}
_FLAG_OPTS = {"--include-bots", "--print-filter"}


def parse_listener_argv(argv: list[str], scripts=("watch_chat.py",)) -> dict | None:
    """Аргументы слушателя из argv процесса (python [-u] …/watch_chat.py --chat … --state … --log …).

    Понимает `--opt value` и `--opt=value`; на перенаправлении (`>>`, `2>&1`) останавливается — это уже cmd.exe.
    """
    i = find_script_index(argv, scripts)
    if i is None:
        return None
    out: dict = {"script": argv[i].strip('"'), "chats": [], "state": None, "log": None, "quiet": None,
                 "trigger": None, "session": None, "ack": None, "include_bots": False, "print_filter": False,
                 "extra": []}
    rest = argv[i + 1:]
    k = 0
    while k < len(rest):
        a = rest[k]
        if a in (">", ">>", "2>&1", "1>", "1>>") or a.startswith((">", "1>", "2>")):
            break
        opt, val = (a.split("=", 1) + [None])[:2] if a.startswith("--") and "=" in a else (a, None)
        if opt in _VALUE_OPTS:
            if val is None:
                if k + 1 < len(rest):
                    val = rest[k + 1]
                    k += 1
                else:
                    val = ""
            key = opt[2:].replace("-", "_")
            if key == "chat":
                out["chats"].append(val)
            elif key == "quiet":
                out["quiet"] = int(val) if re.fullmatch(r"-?\d+", val or "") else val
            else:
                out[key] = val
        elif opt in _FLAG_OPTS:
            out[opt[2:].replace("-", "_")] = True
        else:
            out["extra"].append(a)
        k += 1
    return out


_REDIRECT = re.compile(r'(?:^|\s)1?>>?\s*("([^"]+)"|[^\s"&|<>]+)')


def parse_stdout_redirect(cmdline: str) -> str | None:
    """Цель `>> файл` / `> файл` в командной строке обёртки cmd.exe (2>&1 пропускается)."""
    for m in _REDIRECT.finditer(cmdline or ""):
        target = m.group(2) or m.group(1)
        if target and not target.startswith("&"):
            return target.strip('"')
    return None


def is_shell_wrapper(cmdline: str) -> bool:
    """bash/sh, запущенный с `-c "…"` (так Claude Code запускает команды Bash-тула и Monitor)."""
    c = cmdline or ""
    return bool(re.search(r"(?:^|\s)-c(?:\s|$)", c)) or "eval '" in c


# ------------------------------------------------------- stdout слушателя -----

_T = r"(\d{1,2}:\d{2}:\d{2})"
_RE_ALIVE = re.compile(rf"^ALIVE {_T} connected=(True|False)(?: via ([^;]*))?;?(.*)$")
_RE_READY_CONN = re.compile(rf"^READY connected via (.+?) at {_T}\s*$")
_RE_READY_RECONN = re.compile(rf"^READY reconnected at {_T}")
_RE_READY_CHAT = re.compile(r"^READY chat resolved: (.*) \(id (-?\d+)\)\s*$")
_RE_READY_FIRST = re.compile(r"^READY «(.*)»: первый запуск, старт с id (\d+)")
_RE_LISTENING = re.compile(r"^LISTENING (\d+) chat\(s\)(?:, пачки после (\d+) с тишины)?")
_RE_BATCH = re.compile(r"^BATCH (\d+) нов\w* сообщени\w* в «(.*?)» \(ids (\d+)\s*[–-]\s*(\d+)\)(.*)$")
_RE_CONN_LOST = re.compile(rf"^ConnectionError: связь потеряна в {_T}")
_RE_SEEN = re.compile(r"«([^»]*)» last_seen_id (\d+)")
_RE_OWNER_CALLS = re.compile(r"из них (\d+) — вызов владельца")

HEALTHY = {"ALIVE_OK", "READY_CONNECTED", "READY_RECONNECTED", "READY_CHAT", "READY_FIRST", "READY",
           "LISTENING", "BATCH", "NEW"}
ONLINE = {"ALIVE_OK", "READY_CONNECTED", "READY_RECONNECTED", "LISTENING"}
OFFLINE = {"ALIVE_DOWN", "CONN_LOST", "CONN_ERROR", "ERROR", "TRACEBACK"}


def classify_line(line: str) -> tuple[str, dict]:
    """Тип строки stdout слушателя и её данные (без текстов сообщений)."""
    s = (line or "").rstrip("\r\n")
    if s.startswith("NEW "):
        try:
            d = json.loads(s[4:])
        except Exception:  # noqa: BLE001
            d = {}
        return "NEW", {"id": d.get("id"), "chat_id": d.get("chat_id"), "date": d.get("date"),
                       "owner_call": bool(d.get("owner_call"))}
    m = _RE_ALIVE.match(s)
    if m:
        seen = {t: int(i) for t, i in _RE_SEEN.findall(m.group(4) or "")}
        return ("ALIVE_OK" if m.group(2) == "True" else "ALIVE_DOWN"), {
            "tod": m.group(1), "via": (m.group(3) or "").strip() or None, "seen": seen}
    m = _RE_READY_CONN.match(s)
    if m:
        return "READY_CONNECTED", {"via": m.group(1).strip(), "tod": m.group(2)}
    m = _RE_READY_RECONN.match(s)
    if m:
        return "READY_RECONNECTED", {"tod": m.group(1)}
    m = _RE_READY_CHAT.match(s)
    if m:
        return "READY_CHAT", {"title": m.group(1), "id": int(m.group(2))}
    m = _RE_READY_FIRST.match(s)
    if m:
        return "READY_FIRST", {"title": m.group(1), "id": int(m.group(2))}
    if s.startswith("READY"):
        return "READY", {}
    m = _RE_LISTENING.match(s)
    if m:
        return "LISTENING", {"chats": int(m.group(1)), "quiet": int(m.group(2)) if m.group(2) else None}
    m = _RE_BATCH.match(s)
    if m:
        tail = m.group(5) or ""
        oc = _RE_OWNER_CALLS.search(tail)
        return "BATCH", {"count": int(m.group(1)), "chat": m.group(2), "first": int(m.group(3)),
                         "last": int(m.group(4)), "owner_calls": int(oc.group(1)) if oc else 0,
                         "catchup": "догон" in tail}
    m = _RE_CONN_LOST.match(s)
    if m:
        return "CONN_LOST", {"tod": m.group(1)}
    if s.startswith("ConnectionError"):
        return "CONN_ERROR", {}
    if s.startswith("Traceback"):
        return "TRACEBACK", {}
    if s.startswith("ACK failed"):
        return "ACK_FAILED", {}
    if s.startswith("Error:") or re.match(r"^\w*(Error|Exception)\b", s):
        return "ERROR", {}
    if re.match(r"^(ERROR|CRITICAL) \S+: ", s):
        return "LOG_ERROR", {}
    if s.startswith("MONITOR"):
        return "MONITOR", {}
    return "OTHER", {}


def resolve_times(tods: list[str | None], mtime: float, last_gets_mtime: bool = True) -> list[float | None]:
    """Абсолютное время строк по их «чч:мм:сс»: от mtime файла назад, переход через полночь — минус сутки.

    Последняя строка без времени получает mtime (это и есть момент её записи), если last_gets_mtime."""
    n = len(tods)
    out: list[float | None] = [None] * n
    anchor = datetime.fromtimestamp(mtime)
    for i in range(n - 1, -1, -1):
        tod = tods[i]
        if tod:
            try:
                h, mi, se = (int(x) for x in tod.split(":"))
                cand = anchor.replace(hour=h, minute=mi, second=se, microsecond=0)
            except ValueError:
                continue
            if cand > anchor + timedelta(minutes=2):
                cand -= timedelta(days=1)
            out[i] = cand.timestamp()
            anchor = cand
        elif i == n - 1 and last_gets_mtime:
            out[i] = mtime
    return out


def parse_stdout(lines: list[str], mtime: float | None) -> dict:
    """Сводка по stdout слушателя: связь, пульс, пачки, ошибки. `lines` — хвост файла (порядок сохранён)."""
    kinds: list[str] = []
    datas: list[dict] = []
    for ln in lines:
        k, d = classify_line(ln)
        kinds.append(k)
        datas.append(d)
    tods = [d.get("tod") for d in datas]
    times = resolve_times(tods, mtime) if (mtime and lines) else [None] * len(lines)
    own = resolve_times(tods, mtime, last_gets_mtime=False) if (mtime and lines) else [None] * len(lines)
    # строки без собственного времени получают время ближайшей предыдущей строки со временем (нижняя оценка)
    filled: list[float | None] = []
    prev_own: list[float | None] = []
    last_t: float | None = None
    last_own: float | None = None
    for t, o in zip(times, own):
        prev_own.append(last_own)
        if t is not None:
            last_t = t
        if o is not None:
            last_own = o
        filled.append(t if t is not None else last_t)

    conn: dict = {"state": "unknown", "via": None, "since": None, "detail": None, "since_approx": False}
    hb_ts = hb_kind = None
    last_alive_ts = None
    alive_count = 0
    errors = 0
    batches: list[dict] = []
    chats_seen: dict[str, int] = {}
    resolved: dict[int, str] = {}
    run_started = None
    quiet = None
    via = None
    for idx, (k, d) in enumerate(zip(kinds, datas)):
        t = times[idx] if times[idx] is not None else filled[idx]
        if k in ("ALIVE_OK", "READY_CONNECTED") and d.get("via"):
            via = d["via"]
        if k == "READY_CONNECTED":
            run_started = t
        if k in ONLINE or k in ("BATCH", "NEW"):
            if conn["state"] != "online":
                conn = {"state": "online", "via": via, "since": t, "detail": None, "since_approx": own[idx] is None}
            else:
                conn["via"] = via
        elif k in OFFLINE:
            if conn["state"] != "offline":
                # у строк Error/Traceback нет своего времени: связь пропала не позже предыдущего пульса
                # (Error печатается после ~5 мин ретраев Telethon, так что это ближе к правде, чем mtime)
                ts = own[idx] if own[idx] is not None else (prev_own[idx] if prev_own[idx] is not None else t)
                conn = {"state": "offline", "via": via, "since": ts, "detail": lines[idx].strip()[:220],
                        "since_approx": own[idx] is None}
            else:
                conn["detail"] = lines[idx].strip()[:220]
        if k in ("ALIVE_DOWN", "CONN_LOST", "CONN_ERROR", "ERROR", "TRACEBACK", "LOG_ERROR", "ACK_FAILED"):
            errors += 1
        if k in HEALTHY:
            hb_ts, hb_kind = t, k
        if k == "ALIVE_OK":
            alive_count += 1
            last_alive_ts = t
            chats_seen.update(d.get("seen") or {})
        if k == "ALIVE_DOWN":
            alive_count += 1
        if k == "BATCH":
            batches.append({**d, "line_index": idx})
        if k in ("READY_CHAT", "READY_FIRST"):
            resolved[d["id"]] = d["title"]
        if k == "LISTENING":
            quiet = d.get("quiet")
    return {
        "lines": len(lines),
        "connection": conn,
        "heartbeat_ts": hb_ts,
        "heartbeat_kind": hb_kind,
        "last_alive_ts": last_alive_ts,
        "has_alive": alive_count > 0,
        "last_kind": kinds[-1] if kinds else None,
        "last_ts": mtime if lines else None,
        "errors": errors,
        "batches": batches,
        "chats_seen": chats_seen,
        "resolved_chats": resolved,
        "run_started_ts": run_started,
        "quiet": quiet,
    }


def mask_line(line: str, show_text: bool = False, limit: int = 400) -> str:
    """Строка stdout для показа: NEW — только служебные поля (id, время, тип вложения, длина текста)."""
    s = (line or "").rstrip("\r\n")
    if s.startswith("NEW ") and not show_text:
        try:
            d = json.loads(s[4:])
        except Exception:  # noqa: BLE001
            return "NEW ‹не разобрать — скрыто›"
        parts = [f"id {d.get('id')}"]
        if d.get("date"):
            parts.append(str(d.get("date")))
        if d.get("owner_call"):
            parts.append("вызов владельца /ai")
        if d.get("media"):
            parts.append(f"вложение {d.get('media')}")
        parts.append(f"текст скрыт ({len(d.get('text') or '')} симв.)")
        return "NEW " + " · ".join(parts)
    return s if len(s) <= limit else s[:limit] + "…"


# -------------------------------------------------------------- журналы -----

def parse_local_date(s) -> float | None:
    """`2026-10-05 16:48` (локальное время слушателя) → unix."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(str(s)[:19], fmt).timestamp()
        except ValueError:
            continue
    return None


def parse_incoming(lines: list[str], default_chat_id: int | None = None) -> list[dict]:
    """Записи журнала входящих (`NEW {...}`) без текста: chat_id, id, дата, вызов владельца, вложение."""
    out = []
    for ln in lines:
        if not ln.startswith("NEW "):
            continue
        try:
            d = json.loads(ln[4:])
        except Exception:  # noqa: BLE001
            continue
        mid = d.get("id")
        if not isinstance(mid, int):
            continue
        cid = d.get("chat_id", default_chat_id)
        out.append({"chat_id": cid if cid is not None else default_chat_id, "id": mid, "date": d.get("date"),
                    "ts": parse_local_date(d.get("date")), "owner_call": bool(d.get("owner_call")),
                    "media": d.get("media")})
    return out


def parse_jsonl(lines: list[str]) -> list[dict]:
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def chat_key(cid) -> int | None:
    """Id чата без маркировки: -100123 → 123, -456 → 456 (как в именах папок tg-archive)."""
    try:
        v = int(cid)
    except (TypeError, ValueError):
        return None
    s = str(v)
    if s.startswith("-100") and len(s) > 4:
        return int(s[4:])
    return abs(v)


def compute_pending(incoming: list[dict], replies: list[dict], *, archive_out: dict | None = None,
                    drafts_ts: float | None = None, chat_ids: list | None = None, now: float) -> dict:
    """«Ожидает ответа»: входящие после последнего ответа в том же чате.

    Источники «ответили»: журнал ответов (reply — id и время отправки; handled — «обработано без ответа до id»),
    исходящие в дампе tg-archive (id), черновики notes/reply*.txt (только время — приблизительно).
    Сообщение ждёт ответа, если его id больше id последнего ответа И оно пришло позже последнего ответа.
    """
    by_chat_id: dict[int, int] = {}
    by_chat_ts: dict[int, float] = {}
    global_id = 0
    sources: set[str] = set()
    for r in replies:
        kind = r.get("kind", "reply")
        ck = chat_key(r.get("chat_id"))
        if kind == "handled":
            up = int(r.get("up_to_id") or 0)
            if ck is None:
                global_id = max(global_id, up)
            else:
                by_chat_id[ck] = max(by_chat_id.get(ck, 0), up)
            sources.add("journal")
            continue
        if ck is None:
            continue
        mid = r.get("msg_id")
        if isinstance(mid, int):
            by_chat_id[ck] = max(by_chat_id.get(ck, 0), mid)
        ts = r.get("ts")
        if isinstance(ts, (int, float)):
            by_chat_ts[ck] = max(by_chat_ts.get(ck, 0.0), float(ts))
        sources.add("journal")
    for ck, mid in (archive_out or {}).items():
        if mid:
            by_chat_id[ck] = max(by_chat_id.get(ck, 0), int(mid))
            sources.add("archive")
    if drafts_ts:
        sources.add("drafts")
    keys = {chat_key(c) for c in (chat_ids or [])} | {chat_key(r.get("chat_id")) for r in incoming}
    keys.discard(None)
    known_chats = {ck for ck in keys if ck in by_chat_id or ck in by_chat_ts} | (keys if (drafts_ts or global_id) else set())
    pending = []
    for r in incoming:
        ck = chat_key(r.get("chat_id"))
        if ck not in known_chats:
            continue
        ans_id = max(by_chat_id.get(ck, 0), global_id)
        ans_ts = max(by_chat_ts.get(ck, 0.0), drafts_ts or 0.0)
        if r["id"] <= ans_id:
            continue
        if r.get("ts") is not None and ans_ts and r["ts"] <= ans_ts:
            continue
        pending.append(r)
    oldest = min((r["ts"] for r in pending if r.get("ts")), default=None)
    last_reply_ts = max([*by_chat_ts.values(), drafts_ts or 0.0], default=0.0) or None
    return {
        "count": len(pending),
        "oldest_ts": oldest,
        "oldest_min": round((now - oldest) / 60) if oldest else None,
        "ids": [r["id"] for r in pending][-20:],
        "known": bool(known_chats) or not incoming,
        "approx": sources == {"drafts"},
        "sources": sorted(sources),
        "last_reply_ts": last_reply_ts,
        "unknown_chats": sorted(keys - known_chats),
    }
