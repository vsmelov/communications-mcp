"""Хранилище одного чата.

Файлы в папке чата:
  - dump.unsafe.json — СЫРОЙ источник истины (text/transcript как в Telegram, могут
                       содержать секреты). Его грузит архиватор как состояние.
  - dump.json        — БЕЗОПАСНАЯ копия (секреты вырезаны -> [[secret #id ...]]).
  - dump.md          — БЕЗОПАСНЫЙ человекочитаемый рендер.
  - manifest.json    — метаданные (совместимо с dump_search из MCP).
  - README.md        — автоописание папки.

Безопасные dump.json / dump.md архиватор держит актуальными на каждом save
(инкрементально: чистит только изменившиеся строки, см. _scrubbed_rows). Именно
их читают MCP-тулы и агенты — сырьё с секретами наружу не утекает.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import readme, render
from .scrub import SecretVault, scrub_text


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ChatStore:
    def __init__(self, root: Path, chat_id: int, slug: str, chat_name: str):
        self.chat_id = chat_id
        self.slug = slug
        self.chat_name = chat_name
        self.dir = root / f"{chat_id}-{slug}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rows: dict[int, dict] = {}
        self.archive_state: dict = {"backfill_done": False}
        self._vault_path = root.parent / "secrets.sqlite3"  # общий с describe и CLI
        self._sv: SecretVault | None = None
        self._scrub_cache: dict[int, tuple] = {}  # id -> (сигнатура, {поле: очищенное})
        self._load()

    # ---------------------------------------------------------------- load --
    def _load(self) -> None:
        # Источник истины — сырой dump.unsafe.json. Разовая миграция со старого
        # формата: пока его нет, читаем прежний dump.json (на тот момент он ещё
        # сырой). После первого save появляется dump.unsafe.json и берётся он.
        p = self.dir / "dump.unsafe.json"
        if not p.is_file():
            p = self.dir / "dump.json"
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self.rows = {r["id"]: r for r in data.get("messages", [])}
            except Exception:
                self.rows = {}
        man = self.dir / "manifest.json"
        if man.is_file():
            try:
                self.archive_state = json.loads(man.read_text(encoding="utf-8")).get(
                    "archive", self.archive_state
                )
            except Exception:
                pass

    # --------------------------------------------------------------- props --
    @property
    def newest_id(self) -> int | None:
        return max(self.rows) if self.rows else None

    @property
    def oldest_id(self) -> int | None:
        return min(self.rows) if self.rows else None

    def sorted_rows(self) -> list[dict]:
        return sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)

    def attachments_dir(self, kind: str) -> Path:
        d = self.dir / "attachments" / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def has_file(self, row: dict) -> bool:
        f = row.get("file")
        return bool(f) and (self.dir / f).is_file()

    # --------------------------------------------------------------- scrub --
    def _vault(self) -> SecretVault:
        if self._sv is None:
            self._sv = SecretVault(self._vault_path)
        return self._sv

    def _scrubbed_rows(self, rows: list[dict]) -> list[dict]:
        """Копии строк с вырезанными секретами в text/transcript/description.

        Кэш по строке (сигнатура из трёх полей): повторно чистим только
        изменившиеся строки — на 109к сообщений полный проход идёт лишь раз после
        старта, дальше почти бесплатно.
        """
        vault = self._vault()
        out: list[dict] = []
        for r in rows:
            sig = (r.get("text", ""), r.get("transcript", ""), r.get("description", ""))
            cached = self._scrub_cache.get(r["id"])
            if cached and cached[0] == sig:
                fields = cached[1]
            else:
                fields = {}
                ref = f"{self.chat_id}:{r['id']}"
                for fld in ("text", "transcript", "description"):
                    val = r.get(fld)
                    if isinstance(val, str) and val:
                        clean, _ = scrub_text(val, vault, ref)
                        if clean != val:
                            fields[fld] = clean
                self._scrub_cache[r["id"]] = (sig, fields)
            if fields:
                nr = dict(r)
                nr.update(fields)
                out.append(nr)
            else:
                out.append(r)
        return out

    # ---------------------------------------------------------------- save --
    def save(self, media_max_mb: float) -> dict:
        rows = self.sorted_rows()
        downloaded = sum(1 for r in rows if r.get("file"))
        skipped = sum(1 for r in rows if r.get("skipped_reason"))
        pending = sum(
            1 for r in rows
            if r.get("media") not in (None, "webpage")
            and not r.get("file") and not r.get("skipped_reason")
            and not r.get("described")
        )
        now = datetime.now(timezone.utc)
        meta = {
            "start_ts": rows[-1]["ts"] if rows else None,
            "end_ts": rows[0]["ts"] if rows else None,
            "start": rows[-1]["date_utc"] if rows else None,
            "end": rows[0]["date_utc"] if rows else None,
            "tz": "UTC",
            "messages": len(rows),
            "downloaded": downloaded,
            "skipped": skipped,
        }

        # 1) СЫРОЙ источник истины — первым, чтобы raw гарантированно был на диске
        #    до перезаписи безопасной копии.
        _write_atomic(
            self.dir / "dump.unsafe.json",
            json.dumps(
                {"chat": self.chat_name, "chat_id": self.chat_id, "meta": meta, "messages": rows},
                ensure_ascii=False, indent=2,
            ),
        )
        # 2) БЕЗОПАСНЫЕ копии (секреты вырезаны) — их читают тулы/агенты и шлют в API
        safe_rows = self._scrubbed_rows(rows)
        _write_atomic(
            self.dir / "dump.json",
            json.dumps(
                {"chat": self.chat_name, "chat_id": self.chat_id, "meta": meta, "messages": safe_rows},
                ensure_ascii=False, indent=2,
            ),
        )
        _write_atomic(self.dir / "dump.md", render.render_md(self.chat_name, safe_rows, meta))

        manifest = {
            "version": 1,
            "chat": self.chat_name,
            "chat_id": self.chat_id,
            "slug": self.slug,
            "requested": {"start_ts": meta["start_ts"], "end_ts": meta["end_ts"]},
            "covered": {k: meta[k] for k in ("start_ts", "end_ts", "start", "end")},
            "messages": len(rows),
            "oldest_id": rows[-1]["id"] if rows else None,
            "newest_id": rows[0]["id"] if rows else None,
            "attachments": {
                "enabled": True,
                "kinds": "all",
                "max_mb": media_max_mb,
                "downloaded": downloaded,
                "pending": pending,
            },
            "archive": {
                **self.archive_state,
                "service": "tg-archive",
                "last_save_utc": render.utc_str(now),
            },
            "updated_ts": int(now.timestamp()),
        }
        _write_atomic(self.dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        _write_atomic(
            self.dir / "README.md",
            readme.render_readme(self.chat_name, self.chat_id, self.slug, rows, meta, manifest["archive"]),
        )
        # устаревший ручной снимок больше не нужен — безопасная копия теперь dump.json
        (self.dir / "dump.scrubbed.json").unlink(missing_ok=True)
        return {"messages": len(rows), "downloaded": downloaded, "pending_media": pending}
