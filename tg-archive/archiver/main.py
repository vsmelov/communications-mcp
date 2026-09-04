"""tg-archive: постоянный архиватор избранных чатов Telegram.

Один воркер, строго последовательные задания (аккаунт один — лимиты общие).
Плановые проходы каждые poll_interval_sec; POST /refresh ставит чат в начало
очереди и ждёт результата.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import sys
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from . import render
from .config import Config, load_config
from .describe import Describer
from .ledger import Ledger
from .stt import TelegramTranscriber, Transcriber
from .sync import ChatSyncer

log = logging.getLogger("main")

REFRESH_PRIORITY = 0
EVENT_PRIORITY = 3
SCHEDULED_PRIORITY = 10


def make_client(cfg: Config, proxy_opt: str | None = None) -> TelegramClient:
    """Клиент для ОДНОЙ техники подключения. proxy_opt: "direct"/пусто = напрямую,
    иначе http://host:port или socks5://host:port."""
    kwargs: dict = {"timeout": 60}
    opt = (proxy_opt or "").strip()
    if opt and opt.lower() != "direct":
        from urllib.parse import urlparse

        import socks

        u = urlparse(opt)
        ptype = {
            "socks5": socks.SOCKS5,
            "socks5h": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
            "https": socks.HTTP,
        }[(u.scheme or "socks5").lower()]
        kwargs["proxy"] = (ptype, u.hostname or "127.0.0.1", u.port or 1080)
    cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
    # receive_updates управляет realtime-событиями (sync.realtime_events).
    # Риск: сервер шлёт в апдейтах TL-конструкторы новее нашего слоя (шареный
    # auth_key с более новым клиентом) — такие события Telethon дропает с
    # варнингом, а редкие обрывы соединения лечит реконнект в воркере;
    # пропущенное добирает плановый проход.
    client = TelegramClient(
        str(cfg.session_path), cfg.api_id, cfg.api_hash,
        receive_updates=cfg.sync.realtime_events, **kwargs,
    )
    # Порог 0: ЛЮБОЙ FloodWait долетает до воркера — мы его считаем (метрика в
    # /status), пишем в лог и ждём срок + 20% буфера (Telethon сам спал бы
    # ровно срок и молча — ни видимости, ни буфера).
    client.flood_sleep_threshold = 0
    return client


async def connect_with_fallback(cfg: Config) -> TelegramClient:
    """Подключиться, перебирая техники из cfg.proxy_chain до первой рабочей.

    Если ни одна не сработала (сеть/прокси недоступны) — НЕ падаем, а ждём с
    экспоненциальным бэкоффом и пробуем снова (VPN мог переключить режим —
    вернётся, подключимся сами). Единственная фатальная ветка — сессия не
    авторизована: это чинит только человек, ретраить бессмысленно.
    """
    backoff = 5.0
    rnd = 0
    while True:
        rnd += 1
        for opt in cfg.proxy_chain:
            label = "напрямую" if (opt or "").strip().lower() in ("", "direct") else opt
            client = make_client(cfg, opt)
            try:
                await client.connect()
            except Exception as exc:
                log.warning("подключение (%s) не удалось: %s",
                            label, str(exc).splitlines()[0][:120])
                try:
                    await client.disconnect()
                except Exception:
                    pass
                continue
            try:
                authorized = await client.is_user_authorized()
            except Exception as exc:
                log.warning("проверка авторизации (%s) не удалась: %s", label, str(exc)[:120])
                try:
                    await client.disconnect()
                except Exception:
                    pass
                continue
            if not authorized:
                raise SystemExit(
                    f"Сессия не авторизована: {cfg.session_path}.session — "
                    f"скопируйте туда валидный mcp.session"
                )
            log.info("подключились через: %s", label)
            return client
        log.warning("все техники подключения не сработали (раунд %d) — жду %.0fс и повтор",
                    rnd, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


class Job:
    def __init__(self, syncer: ChatSyncer, reason: str, priority: int):
        self.syncer = syncer
        self.reason = reason
        self.priority = priority
        self.started = False
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()


class Archiver:
    def __init__(self, client: TelegramClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self.ledger = Ledger(cfg.data_root / "usage.sqlite3")
        migrated = self.ledger.migrate_jsonl(cfg.data_root / "transcribe_ledger.jsonl")
        if migrated:
            log.info("перенесено записей трат из старого журнала: %d", migrated)
        transcriber = None
        if cfg.transcribe.enabled:
            if cfg.transcribe.provider == "telegram":
                transcriber = TelegramTranscriber(cfg.transcribe, self.ledger)
            else:
                transcriber = Transcriber(cfg.transcribe, self.ledger)
        self.transcriber = transcriber
        import os
        self.describer = None
        if cfg.describe.enabled:
            key = os.getenv("OPENROUTER_API_KEY", "").strip()
            self.describer = Describer(cfg.describe, self.ledger, key or None,
                                       vault_path=cfg.data_root / "secrets.sqlite3")
            if not key:
                log.warning("describe включён, но OPENROUTER_API_KEY пуст — слой спит")
        self.syncers = [
            ChatSyncer(client, cfg, c.query, transcriber, self.describer) for c in cfg.chats
        ]
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._queued: dict[int, Job] = {}   # id(syncer) -> job в очереди
        self._running: Job | None = None
        self.started_utc = datetime.now(timezone.utc)
        self.last_error: str | None = None
        self.flood_until: float = 0.0
        self._dirty: set[ChatSyncer] = set()   # чаты с несохранёнными событиями
        self._flush_task: asyncio.Task | None = None
        self.events_seen = 0
        self.flood_hits = 0
        self.last_flood: str | None = None

    def _note_flood(self, exc) -> None:
        self.flood_hits += 1
        self.last_flood = (
            f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC, "
            f"{exc.seconds}s, {type(exc).__name__}"
        )
        log.warning("FloodWait #%d: %ss (%s)", self.flood_hits, exc.seconds, type(exc).__name__)

    # ------------------------------------------------------------- events --
    def register_events(self) -> None:
        """Realtime: новые/правленые сообщения падают в дамп за секунды.

        Сообщение из события мержится в rows сразу (без походов в API), затем
        дебаунс-запись на диск и лёгкое задание (медиа+транскрипт) вне очереди
        плановых. Ядовитое событие Telethon дропнет сам — его доберёт плановый
        проход. Удаления в личках приходят без chat_id — ищем id по всем чатам.
        """
        from telethon import events

        def find_by_chat_id(chat_id) -> ChatSyncer | None:
            for s in self.syncers:
                if s.store and s.store.chat_id == chat_id:
                    return s
            return None

        @self.client.on(events.NewMessage())
        @self.client.on(events.MessageEdited())
        async def _on_message(event) -> None:
            s = find_by_chat_id(event.chat_id)
            if s is None or s.store is None:
                return
            try:
                row = render.row_from_msg(event.message, await s._sender_name(event.message))
                s.store.rows[event.message.id] = render.merge_row(
                    s.store.rows.get(event.message.id), row
                )
                self.events_seen += 1
                self._mark_dirty(s)
            except Exception as exc:  # событие не должно ронять клиент
                log.warning("event %s: %s", event.chat_id, exc)

        @self.client.on(events.MessageDeleted())
        async def _on_deleted(event) -> None:
            for deleted_id in event.deleted_ids:
                for s in self.syncers:
                    if s.store and deleted_id in s.store.rows:
                        s.store.rows[deleted_id]["deleted"] = True
                        self.events_seen += 1
                        self._mark_dirty(s)

    def _mark_dirty(self, syncer: ChatSyncer) -> None:
        self._dirty.add(syncer)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_dirty())

    async def _flush_dirty(self) -> None:
        await asyncio.sleep(self.cfg.sync.event_debounce_sec)
        dirty, self._dirty = self._dirty, set()
        for s in dirty:
            try:
                s.store.save(self.cfg.media.max_mb)  # текст на диске сразу
            except Exception:
                log.exception("[%s] не смогли сохранить дамп после события", s.query)
            # медиа и транскрипт нового сообщения — лёгким заданием вне очереди
            self.enqueue(s, "event", EVENT_PRIORITY)

    # ------------------------------------------------------------ enqueue --
    def enqueue(self, syncer: ChatSyncer, reason: str, priority: int) -> Job:
        """Ставит задание в очередь; уже запущенное/стоящее по этому чату — переиспользуется."""
        if self._running and self._running.syncer is syncer and not self._running.future.done():
            return self._running
        existing = self._queued.get(id(syncer))
        if existing:
            if priority < existing.priority:
                # refresh поднимает плановое задание в начало очереди: кладём
                # тот же Job вторым входом, отработанный дубль воркер пропустит
                existing.priority = priority
                existing.reason = reason
                self.queue.put_nowait((priority, next(self._seq), existing))
            return existing
        job = Job(syncer, reason, priority)
        self._queued[id(syncer)] = job
        self.queue.put_nowait((priority, next(self._seq), job))
        return job

    def find_syncer(self, query: str) -> ChatSyncer | None:
        q = query.strip().casefold()
        for s in self.syncers:
            if q == s.query.casefold():
                return s
            if s.store and (
                q == str(s.store.chat_id)
                or q in s.store.chat_name.casefold()
                or q == s.store.slug
            ):
                return s
            ent = s.entity
            if ent is not None:
                if q.lstrip("@") == (getattr(ent, "username", None) or "").casefold():
                    return s
        return None

    # ------------------------------------------------------------- status --
    def status(self) -> dict:
        chats = []
        for s in self.syncers:
            st = s.store
            item: dict = {"query": s.query}
            if st:
                rows = st.sorted_rows()
                item.update(
                    chat_id=st.chat_id,
                    name=st.chat_name,
                    slug=st.slug,
                    dir=str(st.dir),
                    messages=len(rows),
                    newest_id=st.newest_id,
                    covered_utc=[rows[-1]["date_utc"], rows[0]["date_utc"]] if rows else None,
                    backfill_done=bool(st.archive_state.get("backfill_done")),
                    last_sync_ts=st.archive_state.get("last_sync_ts"),
                    pending_media=sum(
                        1 for r in rows
                        if r.get("media") not in (None, "webpage")
                        and not r.get("file") and not r.get("skipped_reason")
                        and not r.get("described")
                    ),
                    transcribed=sum(1 for r in rows if r.get("transcript")),
                    pending_transcripts=sum(
                        1 for r in rows
                        if self.transcriber and self.transcriber.needs(r, st)
                    ),
                    described=sum(1 for r in rows if r.get("description")),
                    pending_describe=sum(
                        1 for r in rows
                        if self.describer and self.describer.needs(r, st)
                    ),
                )
            item["syncing_now"] = bool(
                self._running and self._running.syncer is s and not self._running.future.done()
            )
            item["queued"] = id(s) in self._queued
            chats.append(item)
        return {
            "service": "tg-archive",
            "started_utc": self.started_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "queue_len": self.queue.qsize(),
            "flood_wait_until_ts": int(self.flood_until) if self.flood_until > time.time() else None,
            "flood_hits_total": self.flood_hits,
            "last_flood": self.last_flood,
            "last_error": self.last_error,
            "realtime_events": self.cfg.sync.realtime_events,
            "events_seen": self.events_seen,
            "transcribe": {
                "enabled": self.cfg.transcribe.enabled,
                "provider": self.cfg.transcribe.provider,
                "model": self.cfg.transcribe.model,
                "daily_budget_usd": self.cfg.transcribe.daily_budget_usd,
                "spent": self.ledger.spent("transcribe"),
            },
            "describe": {
                "enabled": self.cfg.describe.enabled,
                "active": bool(self.describer and self.describer.available()),
                "provider": self.cfg.describe.provider,
                "model": self.cfg.describe.model,
                "daily_budget_usd": self.cfg.describe.daily_budget_usd,
                "spent": self.ledger.spent("describe"),
            },
            "usage_total": self.ledger.spent(),          # всё вместе
            "usage_breakdown": self.ledger.breakdown(),  # по task/model/provider
            "chats": chats,
        }

    # -------------------------------------------------------------- loops --
    async def worker(self) -> None:
        while True:
            _, _, job = await self.queue.get()
            if job.started:
                continue  # дубль после поднятия приоритета
            job.started = True
            self._queued.pop(id(job.syncer), None)
            self._running = job
            try:
                # пережидаем объявленный FloodWait до старта нового задания
                wait = self.flood_until - time.time()
                if wait > 0:
                    log.info("FloodWait: ждём ещё %.0f с перед заданием", wait)
                    await asyncio.sleep(wait)
                if not self.client.is_connected():
                    log.info("клиент отключён — переподключаемся")
                    await self.client.connect()
                light = job.reason in ("refresh", "event")
                # обрыв соединения и короткий FloodWait — не повод ронять
                # задание: пережидаем (флуд — срок + 20% буфера) и повторяем раз
                for attempt in (1, 2):
                    try:
                        summary = await job.syncer.sync(
                            job.reason,
                            with_backfill=not light,
                            media_limit=8 if light else None,
                        )
                        break
                    except ConnectionError:
                        if attempt == 2:
                            raise
                        log.warning("соединение оборвалось посреди синка — переподключаюсь и повторяю")
                        await asyncio.sleep(10)
                        if not self.client.is_connected():
                            await self.client.connect()
                    except FloodWaitError as exc:
                        self._note_flood(exc)
                        if attempt == 2 or exc.seconds > 600:
                            raise  # длинный флуд — отдаём глобальной паузе
                        await asyncio.sleep(exc.seconds * 1.2 + 10)
                self.last_error = None
                if not job.future.done():
                    job.future.set_result(summary)
            except FloodWaitError as exc:
                self._note_flood(exc)
                self.flood_until = time.time() + exc.seconds * 1.2 + 30
                self.last_error = f"FloodWait {exc.seconds}s ({datetime.now(timezone.utc):%H:%M:%S} UTC)"
                log.warning("FloodWait %s s — глобальная пауза до %.0f", exc.seconds, self.flood_until)
                if not job.future.done():
                    job.future.set_exception(exc)
            except Exception as exc:
                # str(TypeNotFoundError) тащит мегабайтный дамп байтов — обрезаем
                self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                log.exception("[%s] задание упало", job.syncer.query)
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self._running = None
            # передышка между заданиями — даже между refresh'ами
            base = self.cfg.sync.between_jobs_sec
            await asyncio.sleep(base * random.uniform(1 - self.cfg.sync.jitter_frac,
                                                     1 + self.cfg.sync.jitter_frac))

    async def scheduler(self) -> None:
        # стартовый прогон сразу, дальше по интервалу
        while True:
            for s in self.syncers:
                self.enqueue(s, "scheduled", SCHEDULED_PRIORITY)
            base = self.cfg.sync.poll_interval_sec
            await asyncio.sleep(base * random.uniform(1 - self.cfg.sync.jitter_frac,
                                                      1 + self.cfg.sync.jitter_frac))

    async def refresh(self, query: str, timeout_sec: float) -> dict:
        syncer = self.find_syncer(query)
        if syncer is None:
            # возможно, чат ещё ни разу не синкался — дорезолвим конфиг и поищем снова
            for s in self.syncers:
                if s.entity is None or s.store is None:
                    try:
                        await s.ensure_ready()
                    except Exception:
                        continue
            syncer = self.find_syncer(query)
        if syncer is None:
            known = [
                {"query": s.query, "name": s.store.chat_name if s.store else None}
                for s in self.syncers
            ]
            return {"error": f"чат {query!r} не входит в архив", "archived_chats": known}
        job = self.enqueue(syncer, "refresh", REFRESH_PRIORITY)
        try:
            summary = await asyncio.wait_for(asyncio.shield(job.future), timeout_sec)
            return {"done": True, **summary}
        except asyncio.TimeoutError:
            return {
                "done": False,
                "note": "синхронизация ещё идёт (возможно, FloodWait) — данные появятся на диске позже",
                "status": self.status(),
            }
        except Exception as exc:
            return {
                "done": False,
                "sync_error": f"{type(exc).__name__}: {exc}",
                "note": "синк упал; дамп на диске цел, следующий плановый проход повторит попытку",
                "status": self.status(),
            }


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    cfg = load_config()
    log.info("цепочка подключения: %s", ", ".join(cfg.proxy_chain))
    client = await connect_with_fallback(cfg)
    me = await client.get_me()
    log.info("вошли как %s (id=%s), чатов в конфиге: %d",
             me.username or me.first_name, me.id, len(cfg.chats))

    archiver = Archiver(client, cfg)
    if cfg.sync.realtime_events:
        archiver.register_events()
        log.info("realtime-события включены: новые сообщения попадают в дамп за секунды")

    from .api import make_app  # поздний импорт: api тянет aiohttp
    from aiohttp import web

    runner = web.AppRunner(make_app(archiver))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.http_port)
    await site.start()
    log.info("HTTP API на порту %d (/status, /refresh, /health)", cfg.http_port)

    await asyncio.gather(archiver.worker(), archiver.scheduler())


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
