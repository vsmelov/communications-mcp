"""Учёт трат на обработку (транскрибация + описание) в SQLite.

Строка на каждый запрос: task/kind/provider/model, токены, секунды, $ и статус.
Одна база data/usage.sqlite3. Соединение открывается на каждый вызов (WAL) —
проще и безопаснее при вызовах из to_thread, чем держать одно на всех.

spent() отдаёт всего / за скользящие сутки / за час (для /status и дашборда),
breakdown() — разрезы по task/model, tail() — последние записи.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    task TEXT, kind TEXT, provider TEXT, model TEXT,
    chat_id INTEGER, msg_id INTEGER,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    seconds REAL DEFAULT 0,
    usd REAL DEFAULT 0,
    status TEXT DEFAULT 'ok',
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS ix_usage_task ON usage(task);
"""


class Ledger:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript(_SCHEMA)

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.row_factory = sqlite3.Row
        return con

    # --------------------------------------------------------------- write --
    def add(self, *, task: str, chat_id: int, msg_id: int, kind: str = "",
            provider: str = "", model: str = "", prompt_tokens: int = 0,
            completion_tokens: int = 0, seconds: float = 0.0, usd: float = 0.0,
            status: str = "ok", error: str = "", ts: int | None = None) -> None:
        with self._con() as con:
            con.execute(
                "INSERT INTO usage(ts, task, kind, provider, model, chat_id, msg_id, "
                "prompt_tokens, completion_tokens, seconds, usd, status, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts or int(time.time()), task, kind, provider, model, chat_id, msg_id,
                 int(prompt_tokens or 0), int(completion_tokens or 0),
                 float(seconds or 0), float(usd or 0), status, error or None),
            )

    # ---------------------------------------------------------------- read --
    def spent(self, task: str | None = None) -> dict:
        now = int(time.time())
        where = "WHERE task = ?" if task else ""
        args = (task,) if task else ()
        q = (
            "SELECT "
            "  COUNT(*) AS files, "
            "  COALESCE(SUM(usd),0) AS total_usd, "
            f"  COALESCE(SUM(CASE WHEN ts >= {now-86400} THEN usd ELSE 0 END),0) AS day_usd, "
            f"  COALESCE(SUM(CASE WHEN ts >= {now-3600} THEN usd ELSE 0 END),0) AS hour_usd "
            f"FROM usage {where}"
        )
        with self._con() as con:
            r = con.execute(q, args).fetchone()
        return {
            "files": r["files"],
            "total_usd": round(r["total_usd"], 4),
            "day_usd": round(r["day_usd"], 4),
            "hour_usd": round(r["hour_usd"], 4),
        }

    def breakdown(self) -> list[dict]:
        with self._con() as con:
            rows = con.execute(
                "SELECT task, model, provider, COUNT(*) n, "
                "COALESCE(SUM(usd),0) usd, "
                "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) fails "
                "FROM usage GROUP BY task, model, provider ORDER BY usd DESC"
            ).fetchall()
        return [{"task": r["task"], "model": r["model"], "provider": r["provider"],
                 "count": r["n"], "usd": round(r["usd"], 4), "fails": r["fails"]}
                for r in rows]

    def tail(self, limit: int = 25, task: str | None = None) -> list[dict]:
        where = "WHERE task = ?" if task else ""
        args = ((task, limit) if task else (limit,))
        with self._con() as con:
            rows = con.execute(
                f"SELECT ts, task, kind, provider, model, chat_id, msg_id, "
                f"prompt_tokens, completion_tokens, seconds, usd, status "
                f"FROM usage {where} ORDER BY ts DESC, id DESC LIMIT ?", args
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- migrate --
    def migrate_jsonl(self, jsonl_path: Path, task: str = "transcribe") -> int:
        """Разово втянуть старый transcribe_ledger.jsonl, если база пуста."""
        jsonl_path = Path(jsonl_path)
        if not jsonl_path.is_file():
            return 0
        with self._con() as con:
            if con.execute("SELECT COUNT(*) c FROM usage").fetchone()["c"]:
                return 0
        n = 0
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            self.add(task=task, kind="", provider="openai",
                     model=e.get("model", ""), chat_id=e.get("chat_id", 0),
                     msg_id=e.get("msg_id", 0), seconds=e.get("seconds", 0),
                     usd=e.get("usd", 0), ts=e.get("ts"))
            n += 1
        return n
