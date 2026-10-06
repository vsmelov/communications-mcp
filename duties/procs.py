"""Снимок процессов Windows для дежурств.

Список всех процессов (pid, ppid, имя) — одним вызовом Toolhelp32 через ctypes (~30 мс на ~1000 процессов;
`psutil.process_iter(['ppid'])` на этой машине тратил 37 с — он снимает карту родителей на КАЖДЫЙ процесс).
Командные строки, время старта, cwd и окружение — psutil точечно, только для интересных процессов
(python/cmd/bash/tail/grep/awk/claude). Без psutil — запасной путь через PowerShell `Get-CimInstance Win32_Process`
(медленнее, без окружения и cwd: тогда сессию агента не определить, только факт «кто-то тейлит лог»).

Окружение читается ТОЛЬКО по белому списку ключей (id сессии Claude), остальное (токены!) не сохраняется.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

try:  # psutil есть в системном Python 3.12; без него работаем через PowerShell
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None

PY_NAMES = {"python.exe", "python3.exe", "pythonw.exe", "python", "python3"}
SHELL_NAMES = {"bash.exe", "sh.exe", "dash.exe", "zsh.exe", "bash", "sh"}
PIPE_NAMES = {"tail.exe", "grep.exe", "awk.exe", "gawk.exe", "tail", "grep", "awk", "gawk"}
CMD_NAMES = {"cmd.exe"}
CLAUDE_NAMES = {"claude.exe", "claude", "node.exe"}
INTERESTING = PY_NAMES | SHELL_NAMES | PIPE_NAMES | CMD_NAMES | CLAUDE_NAMES

ENV_KEYS = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT",
            "DUTIES_STARTED_BY")
# переменные сессии Claude, которые НЕ должны наследовать запускаемые нами процессы: иначе слушатель, запущенный
# кнопкой дашборда, выглядел бы «запущенным из сессии», в которой когда-то стартовал сам дашборд
SESSION_ENV = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    create_time: float = 0.0
    argv: list = field(default_factory=list)
    cmdline: str = ""


class Snapshot:
    """Неизменяемый снимок: процессы + ленивые cwd/окружение (кэш на время жизни снимка)."""

    def __init__(self, procs: dict[int, Proc], all_pids: set[int], method: str, took_ms: int):
        self.procs = procs
        self.all_pids = all_pids
        self.method = method
        self.took_ms = took_ms
        self.taken_at = time.time()
        self._env: dict[int, dict] = {}
        self._cwd: dict[int, str | None] = {}
        self._children: dict[int, list[int]] | None = None

    def get(self, pid) -> Proc | None:
        try:
            return self.procs.get(int(pid))
        except (TypeError, ValueError):
            return None

    def alive(self, pid, create_time: float | None = None) -> bool:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid not in self.all_pids:
            return False
        if create_time:
            p = self.procs.get(pid)
            ct = p.create_time if p else _create_time(pid)
            if ct and abs(ct - float(create_time)) > 2:
                return False  # pid переиспользован другим процессом
        return True

    def parent(self, p: Proc) -> Proc | None:
        par = self.procs.get(p.ppid)
        if par is None:
            return None
        if par.create_time and p.create_time and par.create_time > p.create_time + 1:
            return None  # pid родителя занят более новым процессом — настоящий родитель умер
        return par

    def parent_alive(self, p: Proc) -> bool:
        if p.ppid not in self.all_pids:
            return False
        par = self.procs.get(p.ppid)
        if par is not None and par.create_time and p.create_time and par.create_time > p.create_time + 1:
            return False
        if par is None:
            ct = _create_time(p.ppid)
            if ct and p.create_time and ct > p.create_time + 1:
                return False
        return True

    def children(self, pid: int) -> list[Proc]:
        if self._children is None:
            ch: dict[int, list[int]] = {}
            for q in self.procs.values():
                ch.setdefault(q.ppid, []).append(q.pid)
            self._children = ch
        return [self.procs[c] for c in self._children.get(pid, []) if c in self.procs]

    def environ(self, pid: int) -> dict:
        """Белый список ключей окружения процесса (id сессии Claude). Пусто — нет psutil/доступа."""
        if pid in self._env:
            return self._env[pid]
        res: dict = {}
        if psutil is not None:
            try:
                env = psutil.Process(pid).environ()
                res = {k: env[k] for k in ENV_KEYS if env.get(k)}
            except Exception:  # noqa: BLE001
                res = {}
        self._env[pid] = res
        return res

    def cwd(self, pid: int) -> str | None:
        if pid in self._cwd:
            return self._cwd[pid]
        res = None
        if psutil is not None:
            try:
                res = psutil.Process(pid).cwd() or None
            except Exception:  # noqa: BLE001
                res = None
        self._cwd[pid] = res
        return res

    def open_files(self, pid: int) -> list[str]:
        """Открытые файлы процесса (медленно, до ~1 с) — только для обнаружения незаведённых слушателей."""
        if psutil is None:
            return []
        try:
            return [f.path for f in psutil.Process(pid).open_files()]
        except Exception:  # noqa: BLE001
            return []


# ------------------------------------------------------------ снимок -------

def _toolhelp() -> list[tuple[int, int, str]] | None:
    """(pid, ppid, exe) всех процессов одним вызовом CreateToolhelp32Snapshot."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not h or h == wintypes.HANDLE(-1).value:
        return None
    rows = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            rows.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = k32.Process32NextW(h, ctypes.byref(e))
    finally:
        k32.CloseHandle(h)
    return rows


def _create_time(pid: int) -> float:
    if psutil is None:
        return 0.0
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return 0.0


def _snapshot_psutil() -> tuple[dict[int, Proc], set[int]]:
    rows = _toolhelp()
    procs: dict[int, Proc] = {}
    if rows is None:  # не Windows / не вышло — медленный путь psutil
        rows = []
        for p in psutil.process_iter(["pid", "name"]):
            rows.append((p.info["pid"], 0, p.info.get("name") or ""))
        need_ppid = True
    else:
        need_ppid = False
    all_pids = {pid for pid, _, _ in rows}
    for pid, ppid, name in rows:
        lname = (name or "").lower()
        if lname not in INTERESTING:
            continue
        try:
            pp = psutil.Process(pid)
            argv = pp.cmdline()
            ct = float(pp.create_time())
            if need_ppid:
                ppid = pp.ppid()
        except Exception:  # noqa: BLE001 — процесс завершился или нет доступа
            continue
        procs[pid] = Proc(pid=pid, ppid=ppid, name=lname, create_time=ct, argv=list(argv),
                          cmdline=_join_argv(argv))
    return procs, all_pids


_PS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='SilentlyContinue';"
    "$e=[datetime]'1970-01-01';"
    "Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{p=$_.ProcessId;pp=$_.ParentProcessId;"
    "n=$_.Name;c=$_.CommandLine;t=if($_.CreationDate){[double](($_.CreationDate.ToUniversalTime()-$e).TotalSeconds)}"
    "else{0}} } | ConvertTo-Json -Compress"
)


def _snapshot_powershell() -> tuple[dict[int, Proc], set[int]]:
    from .textparse import split_cmdline
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=60,
                       creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    data = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
    if isinstance(data, dict):
        data = [data]
    procs: dict[int, Proc] = {}
    all_pids: set[int] = set()
    for d in data:
        pid = int(d.get("p") or 0)
        all_pids.add(pid)
        name = (d.get("n") or "").lower()
        if name not in INTERESTING:
            continue
        cmd = d.get("c") or ""
        procs[pid] = Proc(pid=pid, ppid=int(d.get("pp") or 0), name=name, create_time=float(d.get("t") or 0),
                          argv=split_cmdline(cmd), cmdline=cmd)
    return procs, all_pids


def _join_argv(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if (" " in a or not a) and not a.startswith('"') else a for a in argv)


def take_snapshot(method: str | None = None) -> Snapshot:
    t0 = time.time()
    m = method or ("psutil" if psutil is not None else "powershell")
    try:
        procs, all_pids = _snapshot_psutil() if m == "psutil" else _snapshot_powershell()
    except Exception:  # noqa: BLE001
        if m == "psutil":
            procs, all_pids = _snapshot_powershell()
            m = "powershell"
        else:
            procs, all_pids, m = {}, set(), "none"
    return Snapshot(procs, all_pids, m, int((time.time() - t0) * 1000))


# ----------------------------------------------------------- действия ------

def terminate(pid: int, create_time: float | None = None, timeout: float = 8.0) -> bool:
    """Завершить процесс (TerminateProcess). Проверяет время старта — чтобы не убить чужой процесс с тем же pid."""
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            if create_time and abs(float(p.create_time()) - float(create_time)) > 2:
                return False
            p.terminate()
            try:
                p.wait(timeout)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(3)
            return True
        except psutil.NoSuchProcess:
            return True
        except Exception:  # noqa: BLE001
            return False
    r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, stdin=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return r.returncode == 0


_LAUNCHER = (
    "import subprocess, sys\n"
    "kw = dict(cwd=sys.argv[2], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
    " close_fds=True)\n"
    "base = 0x08000000 | 0x00000200\n"
    "try:\n"
    "    p = subprocess.Popen(sys.argv[1], creationflags=base | 0x01000000, **kw)\n"
    "    broke = 1\n"
    "except OSError:\n"
    "    p = subprocess.Popen(sys.argv[1], creationflags=base, **kw)\n"
    "    broke = 0\n"
    "print(p.pid, broke)\n"
)


def job_limits() -> dict:
    """Job-объект текущего процесса: {in_job, kill_on_close, breakaway_ok} (Windows; иначе всё False).

    Если вызывающий сидит в Job с KILL_ON_JOB_CLOSE без разрешения breakaway (так делает, например, MCP-клиент
    Python SDK для своих серверов), всё, что мы запустим, умрёт вместе с ним — об этом надо предупредить.
    Процессы сессий Claude Code такого Job не имеют (проверено 05.10.2026: LimitFlags=0)."""
    out = {"in_job": False, "kill_on_close": False, "breakaway_ok": False}
    if sys.platform != "win32":
        return out
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", ctypes.c_size_t),
                    ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("io", ctypes.c_uint64 * 6), ("m1", ctypes.c_size_t), ("m2", ctypes.c_size_t),
                    ("m3", ctypes.c_size_t), ("m4", ctypes.c_size_t)]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        b = wintypes.BOOL()
        if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(b)) or not b.value:
            return out
        out["in_job"] = True
        info = EXT()
        if k32.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
            f = info.Basic.LimitFlags
            out["kill_on_close"] = bool(f & 0x2000)
            out["breakaway_ok"] = bool(f & 0x800) or bool(f & 0x1000)
    except Exception:  # noqa: BLE001
        pass
    return out


def spawn_detached(cmdline: str, cwd: str, env_extra: dict | None = None) -> dict:
    """Запустить командную строку отдельным процессом Windows, не привязанным к вызывающему.

    Двойной запуск: промежуточный python стартует процесс со скрытой консолью (CREATE_NO_WINDOW, своя группа,
    по возможности вне Job-объекта) и сразу выходит — у слушателя нет живого родителя, поэтому ни закрытие сессии
    Claude, ни остановка MCP-сервера, ни лимит фоновых задач его не задевают (как `Start-Process … -WindowStyle
    Hidden` из рецепта в README воркспейсов). Переменные сессии Claude не наследуются (см. SESSION_ENV),
    env_extra добавляется. Возвращает {pid (обычно cmd.exe), breakaway, warning}.
    """
    from .config import default_python  # python.exe, не pythonw: промежуточному процессу нужен stdout для pid
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
    env.update({k: str(v) for k, v in (env_extra or {}).items() if v is not None})
    # stdin=DEVNULL обязательно: в MCP-сервере stdin — транспортный пайп, который в соседнем потоке читает
    # блокирующий ReadFile; наследование этого хэндла дочерним процессом вешало запуск на 30 с (05.10.2026)
    r = subprocess.run([default_python(), "-c", _LAUNCHER, cmdline, str(cwd)], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30, env=env,
                       creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if r.returncode != 0:
        raise RuntimeError(f"запуск не удался: {(r.stderr or r.stdout).strip()[-400:]}")
    parts = r.stdout.strip().splitlines()[-1].split()
    res = {"pid": int(parts[0]), "breakaway": len(parts) > 1 and parts[1] == "1", "warning": None}
    if not res["breakaway"]:
        jl = job_limits()
        if jl["kill_on_close"]:
            res["warning"] = ("вызывающий процесс сидит в Job-объекте с KILL_ON_JOB_CLOSE (так делает, например, "
                              "тестовый MCP-клиент на Python SDK) — запущенное умрёт вместе с ним; для постоянной "
                              "работы запускай с дашборда или из сессии Claude")
    return res
