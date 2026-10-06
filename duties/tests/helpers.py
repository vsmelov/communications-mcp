"""Общее для тестов: временное окружение (реестр/рантайм/архив/сессии — во временной папке) и фейковый снимок
процессов. Реальные реестр, логи и процессы дежурств тесты не трогают."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from duties.procs import Proc, Snapshot

FAKE_LISTENER = Path(__file__).resolve().parent / "fake_listener.py"
ENV_KEYS = ("DUTIES_REGISTRY", "DUTIES_RUNTIME", "DUTIES_ARCHIVE_ROOT", "DUTIES_CLAUDE_SESSIONS_DIR",
            "TELEGRAM_ARCHIVE_URL", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


class TempEnv:
    """with TempEnv() as t: … — все пути ядра смотрят во временную папку t.dir."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="duties-test-"))
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k in ENV_KEYS:
            self._saved[k] = os.environ.get(k)
        os.environ["DUTIES_REGISTRY"] = str(self.dir / "registry.json")
        os.environ["DUTIES_RUNTIME"] = str(self.dir / "runtime")
        os.environ["DUTIES_ARCHIVE_ROOT"] = str(self.dir / "archive")
        os.environ["DUTIES_CLAUDE_SESSIONS_DIR"] = str(self.dir / "sessions")
        os.environ["TELEGRAM_ARCHIVE_URL"] = "http://127.0.0.1:9"  # заведомо пустой порт: архиватор «не отвечает»
        os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        (self.dir / "archive").mkdir()
        (self.dir / "sessions").mkdir()
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class FakeSnap(Snapshot):
    """Снимок из заданных процессов; окружение и cwd — из словарей (без psutil)."""

    def __init__(self, procs: list[Proc], env: dict | None = None, cwd: dict | None = None,
                 extra_pids: set | None = None):
        super().__init__({p.pid: p for p in procs}, {p.pid for p in procs} | set(extra_pids or ()), "test", 0)
        self._fenv = env or {}
        self._fcwd = cwd or {}

    def environ(self, pid):
        return dict(self._fenv.get(pid, {}))

    def cwd(self, pid):
        return self._fcwd.get(pid)

    def open_files(self, pid):
        return []


def proc(pid, ppid, name, argv, ct=0.0) -> Proc:
    return Proc(pid=pid, ppid=ppid, name=name, create_time=ct, argv=list(argv),
                cmdline=" ".join(f'"{a}"' if " " in a else a for a in argv))
