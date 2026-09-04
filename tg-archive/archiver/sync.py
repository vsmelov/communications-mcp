"""Синхронизация одного чата: сообщения (вперёд/назад/правки) и вложения.

Всё строго последовательно и с паузами — аккаунт один, лимиты Telegram
общие на аккаунт, поэтому никакой параллельности здесь нет намеренно.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.errors.common import TypeNotFoundError

from . import render
from .config import Config
from .store import ChatStore

log = logging.getLogger("sync")

# Причины пропуска, которые не имеет смысла пересматривать автоматически.
_PERMANENT_SKIPS = ("аудиодорожка", "сообщение недоступно", "нет аудиодорожки",
                    "удалён после расшифровки")

# Виды, которые после расшифровки можно не хранить: текст [speech] заменяет аудио.
_AUDIO_KINDS = ("voice", "video_notes", "audio")
_DROP_REASON = "аудио удалён после расшифровки"


def _jitter(base: float, frac: float) -> float:
    return base * random.uniform(1 - frac, 1 + frac)


class ChatSyncer:
    def __init__(self, client: TelegramClient, cfg: Config, query: str,
                 transcriber=None, describer=None):
        self.client = client
        self.cfg = cfg
        self.query = query
        self.transcriber = transcriber  # stt.Transcriber / TelegramTranscriber или None
        self.describer = describer      # describe.Describer или None
        self.entity = None
        self.store: ChatStore | None = None
        self._sender_names: dict[int, str] = {}
        self._attach_store_from_disk()

    def _attach_store_from_disk(self) -> None:
        """Подхватить существующую директорию чата без похода в сеть,
        чтобы сразу после старта /status и /refresh знали имя и покрытие."""
        import json

        root = self.cfg.chats_root
        if not root.is_dir():
            return
        q = self.query.casefold().lstrip("@")
        for d in root.iterdir():
            man_p = d / "manifest.json"
            if not man_p.is_file():
                continue
            try:
                man = json.loads(man_p.read_text(encoding="utf-8"))
            except Exception:
                continue
            match = (
                q == str(man.get("chat_id"))
                or q == ((man.get("archive") or {}).get("query") or "").casefold()
                or (not q.isdigit() and q in (man.get("chat") or "").casefold())
            )
            if match:
                self.store = ChatStore(root, man["chat_id"], man.get("slug") or "chat",
                                       man.get("chat") or str(man["chat_id"]))
                return

    # ------------------------------------------------------------- helpers --
    async def _sleep(self, base: float) -> None:
        await asyncio.sleep(_jitter(base, self.cfg.sync.jitter_frac))

    async def ensure_ready(self) -> None:
        if self.entity is None:
            self.entity = await self.client.get_entity(
                int(self.query) if self.query.lstrip("-").isdigit() else self.query
            )
        if self.store is None:
            name = render.entity_name(self.entity)
            self.store = ChatStore(
                self.cfg.chats_root, self.entity.id, render.slugify(name), name
            )
        self.store.archive_state["query"] = self.query

    async def _sender_name(self, msg) -> str | None:
        if msg.sender_id is None:
            return None
        name = self._sender_names.get(msg.sender_id)
        if name is None:
            try:
                name = render.entity_name(msg.sender or await msg.get_sender())
            except Exception:
                name = str(msg.sender_id)
            self._sender_names[msg.sender_id] = name
        return name

    async def _collect_ids(self, **kwargs) -> list[int]:
        """iter_messages -> store.rows; возвращает id полученных сообщений."""
        ids: list[int] = []
        kwargs.setdefault("wait_time", self.cfg.sync.history_wait_sec)
        async for msg in self.client.iter_messages(self.entity, **kwargs):
            row = render.row_from_msg(msg, await self._sender_name(msg))
            self.store.rows[msg.id] = render.merge_row(self.store.rows.get(msg.id), row)
            ids.append(msg.id)
        return ids

    async def _collect(self, **kwargs) -> int:
        return len(await self._collect_ids(**kwargs))

    async def _careful_range(self, offset_id: int, add: int, take: int) -> tuple[list[int], int]:
        """Окно [add, add+take) от offset_id с изоляцией яда бисекцией.

        TypeNotFoundError = сервер прислал TL-конструктор новее, чем знает
        Telethon (типично — пере-сериализованное webpage-превью); падает разбор
        всей страницы. Делим окно пополам, пока не изолируем ядовитые
        сообщения поштучно; id яда знать не нужно (позиция через add_offset).
        Возвращает (id собранных, число пропущенных)."""
        await self._sleep(0.8)
        try:
            return await self._collect_ids(offset_id=offset_id, add_offset=add, limit=take), 0
        except TypeNotFoundError:
            if take == 1:
                return [], 1
            half = take // 2
            ids1, sk1 = await self._careful_range(offset_id, add, half)
            ids2, sk2 = await self._careful_range(offset_id, add + half, take - half)
            return ids1 + ids2, sk1 + sk2

    async def _collect_careful(self, *, offset_id: int = 0, min_id: int = 0,
                               max_count: int) -> tuple[int, int]:
        """Аккуратный обход окна истории страницами с бисекцией ядовитых страниц.

        min_id-границу проверяем сами по фактическим id (в запрос её не
        передаём): яд может лежать НИЖЕ границы в той же странице, и серверный
        фильтр не спас бы от падения разбора. Возвращает (собрано, пропущено)."""
        page = 20
        got = skipped = add = 0
        while got + skipped < max_count:
            take = min(page, max_count - got - skipped)
            ids, sk = await self._careful_range(offset_id, add, take)
            got += len(ids)
            skipped += sk
            add += take
            if min_id and ids and min(ids) <= min_id:
                break  # дошли до уже известных сообщений
            if len(ids) + sk < take:
                break  # конец истории
            await self._sleep(self.cfg.sync.history_wait_sec)
        if skipped:
            log.warning("[%s] пропущено %d непарсящихся сообщений (TL-слой Telethon старее сервера)",
                        self.store.slug, skipped)
        return got, skipped

    async def _collect_tolerant(self, *, offset_id: int = 0, min_id: int = 0,
                                limit: int) -> tuple[int, int]:
        """Быстрый путь одним куском; при TypeNotFoundError — аккуратный обход.

        Кап на 2000: не даём аккуратному режиму уйти в бесконечную прогулку
        по истории, если граница min_id прячется за сплошным ядом."""
        try:
            return await self._collect(offset_id=offset_id, min_id=min_id, limit=limit), 0
        except TypeNotFoundError:
            log.warning("[%s] TypeNotFoundError — включаю аккуратный обход окна", self.store.slug)
            return await self._collect_careful(
                offset_id=offset_id, min_id=min_id, max_count=min(limit, 2000)
            )

    # ---------------------------------------------------------------- sync --
    async def sync(self, reason: str, with_backfill: bool = True,
                   media_limit: int | None = None) -> dict:
        """Один проход: новые сообщения, правки, кусок бэкфилла, вложения.

        refresh-проходы зовут это с with_backfill=False и малым media_limit,
        чтобы ответить быстро; бэклог дольют плановые проходы.
        """
        await self.ensure_ready()
        st = self.store
        summary: dict = {"chat": st.chat_name, "chat_id": st.chat_id, "reason": reason}

        # 1. Новые сообщения (всё, что новее известного newest_id).
        skipped_total = 0
        before = len(st.rows)
        if st.rows:
            _, sk = await self._collect_tolerant(min_id=st.newest_id, limit=100_000)
        else:
            _, sk = await self._collect_tolerant(limit=self.cfg.sync.backfill_batch)
        skipped_total += sk
        summary["new_messages"] = len(st.rows) - before

        # 2. Правки: перечитываем хвост. Удаления в личках по дырам в id не
        # определить (id сквозные по всем личным чатам аккаунта) — не пытаемся.
        if st.rows and self.cfg.sync.edit_window:
            await self._sleep(self.cfg.sync.history_wait_sec)
            _, sk = await self._collect_tolerant(limit=self.cfg.sync.edit_window)
            skipped_total += sk

        # 3. Бэкфилл: аккуратно, кусками, пока не упрёмся в начало истории.
        if with_backfill and not st.archive_state.get("backfill_done") and st.rows:
            await self._sleep(self.cfg.sync.history_wait_sec)
            batch = self.cfg.sync.backfill_batch
            got, sk = await self._collect_tolerant(offset_id=st.oldest_id, limit=batch)
            skipped_total += sk
            summary["backfilled"] = got
            if got + sk < batch:
                st.archive_state["backfill_done"] = True
                log.info("[%s] бэкфилл завершён: вся история на диске", st.slug)

        if skipped_total:
            summary["unparseable_skipped"] = skipped_total
            st.archive_state["unparseable_total"] = (
                st.archive_state.get("unparseable_total", 0) + skipped_total
            )

        # Тексты сохраняем сразу: дамп на диске актуален ещё до медиа-фазы,
        # и упавшая на медиа синхронизация не теряет сообщения.
        st.save(self.cfg.media.max_mb)

        # 4. Транскрибация войсов/кружков ДО скачивания: провайдер telegram
        # берёт текст по msg_id без файла, поэтому расшифрованное аудио затем
        # вообще не качается (см. _needs_download). provider: openai требует
        # файл — он расшифрует на следующем проходе, после загрузки.
        if self.transcriber is not None:
            stt_res = await self.transcriber.pass_for(self)
            summary["transcribed"] = stt_res.get("transcribed", 0)
            if stt_res.get("usd"):
                summary["stt_usd"] = stt_res["usd"]

        # 5. Подчистка: уже расшифрованное аудио на диске больше не нужно.
        dropped = self._drop_transcribed_audio()
        if dropped:
            summary["audio_dropped"] = dropped

        # 6. Вложения (расшифрованное аудио сюда уже не попадёт).
        summary["media_downloaded"] = await self._media_pass(media_limit)

        # 7. Описание картинок/PDF: работает по скачанным файлам, после успеха
        # удаляет их (докачиваемо по id). Платно — с дневным бюджетом.
        if self.describer is not None:
            d_res = await self.describer.pass_for(self)
            summary["described"] = d_res.get("described", 0)
            if d_res.get("usd"):
                summary["describe_usd"] = d_res["usd"]

        st.archive_state["last_sync_ts"] = int(time.time())
        st.archive_state["last_reason"] = reason
        summary.update(st.save(self.cfg.media.max_mb))
        summary["backfill_done"] = bool(st.archive_state.get("backfill_done"))
        log.info(
            "[%s] %s: +%d сообщ., бэкфилл %s, медиа +%d, речь +%d ($%.4f), картинки +%d ($%.4f), всего %d",
            st.slug, reason, summary["new_messages"], summary.get("backfilled", "—"),
            summary["media_downloaded"], summary.get("transcribed", 0), summary.get("stt_usd", 0.0),
            summary.get("described", 0), summary.get("describe_usd", 0.0), summary["messages"],
        )
        return summary

    # --------------------------------------------------------------- media --
    def _drop_audio_enabled(self) -> bool:
        t = self.cfg.transcribe
        return bool(t.enabled and getattr(t, "drop_audio_after_transcript", False))

    def _drop_transcribed_audio(self) -> int:
        """Удалить с диска аудиофайлы, для которых уже есть расшифровка.

        Текст [speech] сохранён в дампе — сам войс/кружок избыточен. Строка
        помечается audio_dropped и skipped_reason, чтобы файл не качался снова
        и не считался «ждущим». Управляется transcribe.drop_audio_after_transcript.
        """
        if not self._drop_audio_enabled():
            return 0
        dropped = 0
        for row in self.store.rows.values():
            if (row.get("media") in _AUDIO_KINDS and row.get("transcript")
                    and not row.get("audio_dropped")):
                f = row.get("file")
                if f:
                    try:
                        (self.store.dir / f).unlink(missing_ok=True)
                    except Exception as exc:
                        log.warning("[%s] не удалить %s: %s", self.store.slug, f, exc)
                        continue
                row.pop("file", None)
                row["audio_dropped"] = True
                row["skipped_reason"] = _DROP_REASON
                dropped += 1
        if dropped:
            log.info("[%s] удалено расшифрованных аудио: %d", self.store.slug, dropped)
        return dropped

    def _needs_download(self, row: dict) -> bool:
        kind = row.get("media")
        if kind in (None, "webpage") or self.store.has_file(row):
            return False
        # уже расшифровано и включено удаление аудио — качать нечего
        if (kind in _AUDIO_KINDS and row.get("transcript") and self._drop_audio_enabled()):
            return False
        # уже описано и файл удалён — качать нечего (докачиваемо вручную по id)
        if row.get("described"):
            return False
        reason = row.get("skipped_reason") or ""
        if any(p in reason for p in _PERMANENT_SKIPS):
            return False
        if row.get("dl_attempts", 0) >= self.cfg.media.download_error_retries:
            return False
        size = row.get("size_bytes") or 0
        cap = (
            self.cfg.media.video_note_source_max_bytes
            if kind == "video_notes"
            else self.cfg.media.max_bytes
        )
        if size > cap:
            row["skipped_reason"] = (
                f"больше {self.cfg.media.video_note_source_max_mb:g} МБ (исходник кружка)"
                if kind == "video_notes"
                else f"больше {self.cfg.media.max_mb:g} МБ"
            )
            return False
        row.pop("skipped_reason", None)  # размер в лимите — старый пропуск неактуален
        return True

    async def _media_pass(self, limit: int | None = None) -> int:
        st = self.store
        cap = limit if limit is not None else self.cfg.media.per_cycle
        pending = [r for r in st.sorted_rows() if self._needs_download(r)][:cap]
        if not pending:
            return 0

        # Message-объекты для скачивания берём одним батч-запросом; если среди
        # них «ядовитое» (TL-слой) — падает весь батч, тогда добираем по одному.
        ids = [r["id"] for r in pending]
        poisoned: set[int] = set()
        try:
            msgs = await self.client.get_messages(self.entity, ids=ids)
        except TypeNotFoundError:
            msgs = []
            for mid in ids:
                try:
                    msgs.append(await self.client.get_messages(self.entity, ids=mid))
                except TypeNotFoundError:
                    poisoned.add(mid)
                await self._sleep(1.0)
        by_id = {m.id: m for m in msgs if m}

        done = 0
        for row in pending:
            msg = by_id.get(row["id"])
            if msg is None or not msg.media:
                if row["id"] in poisoned:
                    # может починиться после обновления Telethon — ограниченно ретраим
                    row["dl_attempts"] = row.get("dl_attempts", 0) + 1
                    row["skipped_reason"] = "непарсится (TL-слой Telethon)"
                else:
                    row["skipped_reason"] = "сообщение недоступно"
                continue
            await self._sleep(self.cfg.media.delay_sec)
            try:
                if row["media"] == "video_notes":
                    ok = await self._fetch_video_note_audio(row, msg)
                else:
                    ok = await self._fetch_plain(row, msg)
                done += ok
            except FloodWaitError:
                raise  # наверх — там общий сон
            except Exception as exc:
                row["dl_attempts"] = row.get("dl_attempts", 0) + 1
                row["skipped_reason"] = f"ошибка загрузки: {str(exc)[:120]}"
                log.warning("[%s] media %s: %s", st.slug, row["id"], exc)
        return done

    async def _fetch_plain(self, row: dict, msg) -> int:
        kind = row["media"]
        folder = self.store.attachments_dir(kind)
        saved = await self.client.download_media(msg, file=str(folder / render.safe_name(msg, kind)))
        if not saved:
            row["dl_attempts"] = row.get("dl_attempts", 0) + 1
            return 0
        row["file"] = str(Path(saved).relative_to(self.store.dir)).replace("\\", "/")
        row.pop("skipped_reason", None)
        return 1

    async def _fetch_video_note_audio(self, row: dict, msg) -> int:
        """Кружок: скачиваем исходник во временную папку, оставляем только
        аудиодорожку (и только если она в лимите обычных вложений)."""
        tmp_dir = self.cfg.tmp_root
        tmp_dir.mkdir(parents=True, exist_ok=True)
        src = tmp_dir / f"vn_{self.store.chat_id}_{msg.id}.mp4"
        dst = self.store.attachments_dir("video_notes") / f"{msg.id}_audio.m4a"
        try:
            saved = await self.client.download_media(msg, file=str(src))
            if not saved:
                row["dl_attempts"] = row.get("dl_attempts", 0) + 1
                return 0
            src = Path(saved)
            if not await self._extract_audio(src, dst):
                row["skipped_reason"] = "нет аудиодорожки (ffmpeg не смог извлечь)"
                return 0
            if dst.stat().st_size > self.cfg.media.max_bytes:
                dst.unlink(missing_ok=True)
                row["skipped_reason"] = f"аудиодорожка больше {self.cfg.media.max_mb:g} МБ"
                return 0
            row["file"] = str(dst.relative_to(self.store.dir)).replace("\\", "/")
            row["audio_only"] = True
            row.pop("skipped_reason", None)
            return 1
        finally:
            src.unlink(missing_ok=True)

    async def _extract_audio(self, src: Path, dst: Path) -> bool:
        # Сначала без перекодирования (в кружках обычно AAC — копия мгновенна),
        # при неудаче — перекодирование в AAC 64k.
        for args in (("-acodec", "copy"), ("-acodec", "aac", "-b:a", "64k")):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(src), "-vn", *args, str(dst),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            if await proc.wait() == 0 and dst.is_file() and dst.stat().st_size > 0:
                return True
        dst.unlink(missing_ok=True)
        return False
