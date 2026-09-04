"""Thin sqlite3 wrapper. All calls are sync and meant to be run via
asyncio.to_thread from callers — throughput here is tiny (1 transcript at a
time, a localhost UI), so a real async driver would be overkill.
"""
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    cursor_msg_id INTEGER NOT NULL DEFAULT 0,
    cursor_date TEXT,
    backlog_initialized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    chat_title TEXT,
    msg_id INTEGER NOT NULL,
    kind TEXT NOT NULL,               -- 'voice' | 'round'
    msg_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|timeout|failed|error
    attempts INTEGER NOT NULL DEFAULT 0,
    transcription_id INTEGER,
    text TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""

# Additive migrations for DBs created before a column existed. Each is
# idempotent (ignored if the column is already there).
MIGRATIONS = [
    "ALTER TABLE items ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with _lock:
            self.conn.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    self.conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # already applied
            self.conn.commit()

    # ---- chats / cursor ----

    def get_chat(self, chat_id):
        with _lock:
            cur = self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
            return cur.fetchone()

    def init_chat_cursor(self, chat_id, title, cursor_msg_id):
        with _lock:
            self.conn.execute(
                """INSERT INTO chats (chat_id, title, cursor_msg_id, cursor_date,
                       backlog_initialized, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
                (chat_id, title, cursor_msg_id, now_iso(), now_iso()),
            )
            self.conn.commit()

    def advance_cursor(self, chat_id, title, cursor_msg_id):
        with _lock:
            self.conn.execute(
                """UPDATE chats SET title=?, cursor_msg_id=MAX(cursor_msg_id, ?),
                       cursor_date=?, backlog_initialized=1, updated_at=?
                   WHERE chat_id=?""",
                (title, cursor_msg_id, now_iso(), now_iso(), chat_id),
            )
            self.conn.commit()

    def list_chats(self, query: str = "", limit: int = 200):
        sql = """SELECT c.*,
                     (SELECT COUNT(*) FROM items i WHERE i.chat_id=c.chat_id AND i.status='pending') AS pending,
                     (SELECT COUNT(*) FROM items i WHERE i.chat_id=c.chat_id AND i.status='done') AS done
                 FROM chats c"""
        args = []
        if query:
            sql += " WHERE c.title LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY c.updated_at DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with _lock:
            cur = self.conn.execute(sql, args)
            return cur.fetchall()

    def chats_count(self):
        with _lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) AS n, SUM(backlog_initialized) AS scanned FROM chats"
            )
            return cur.fetchone()

    # ---- items / queue ----

    def insert_item(self, chat_id, chat_title, msg_id, kind, msg_date):
        with _lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO items
                       (chat_id, chat_title, msg_id, kind, msg_date, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (chat_id, chat_title, msg_id, kind, msg_date, now_iso(), now_iso()),
            )
            self.conn.commit()

    def next_pending(self):
        with _lock:
            cur = self.conn.execute(
                "SELECT * FROM items WHERE status='pending' ORDER BY id ASC LIMIT 1"
            )
            return cur.fetchone()

    def set_status(self, item_id, status, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())
        with _lock:
            self.conn.execute(
                f"UPDATE items SET status=?, updated_at=?{(', ' + cols) if cols else ''} WHERE id=?",
                [status, now_iso(), *values, item_id],
            )
            self.conn.commit()

    def retry_item(self, item_id):
        """Manual retry from the UI — resets the auto-retry budget too."""
        with _lock:
            self.conn.execute(
                "UPDATE items SET status='pending', attempts=0, error=NULL, updated_at=? WHERE id=?",
                (now_iso(), item_id),
            )
            self.conn.commit()

    def requeue_stuck_processing(self) -> int:
        """Called once at startup: any item still 'processing' belongs to a
        run that died before it could record a result. Put it back in the
        queue. Returns how many were requeued."""
        with _lock:
            cur = self.conn.execute(
                "UPDATE items SET status='pending', updated_at=? WHERE status='processing'",
                (now_iso(),),
            )
            self.conn.commit()
            return cur.rowcount

    def list_recent_items(self, limit=300):
        with _lock:
            cur = self.conn.execute(
                "SELECT * FROM items ORDER BY id DESC LIMIT ?", (limit,)
            )
            return cur.fetchall()

    def status_counts(self):
        with _lock:
            cur = self.conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
            return {row["status"]: row["n"] for row in cur.fetchall()}


db = DB(config.DB_PATH)
