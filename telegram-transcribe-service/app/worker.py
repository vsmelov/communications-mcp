"""Backlog scan + live listener + single-lane transcription queue.

Design:
  - Each DM chat has a saved cursor (chats.cursor_msg_id). First time we ever
    see a chat we seed the cursor at "now - window" (24h, or the per-chat
    override in config.py) and scan forward from there once. Every run after
    that — including periodic reconciliation and process restarts — just asks
    Telegram for messages with id > cursor, so the catch-up is incremental
    and the one-time window never applies again.
  - New incoming DMs are also caught live via events.NewMessage, so under
    normal operation the reconciliation sweep is just a safety net for time
    the service was offline.
  - Exactly one coroutine (worker_loop) calls TranscribeAudioRequest, so at
    most one transcription is ever in flight.
"""
import asyncio
import logging
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.functions.messages import TranscribeAudioRequest
from telethon.tl.types import UpdateTranscribedAudio

from . import config
from .db import db

log = logging.getLogger("worker")

# transcription_id -> Future[UpdateTranscribedAudio], resolved by the raw
# update handler when Telegram finishes an async transcription.
_pending_futures: dict[int, asyncio.Future] = {}

# Refreshed by refresh_eligible_chats(); used by the live NewMessage handler
# to decide whether a DM qualifies without an API round-trip per message.
_eligible_chat_ids: set[int] = set()


def classify(message) -> str | None:
    if message.voice:
        return "voice"
    if message.video_note:
        return "round"
    return None


async def iter_eligible_dialogs(client: TelegramClient):
    async for dialog in client.iter_dialogs(archived=False):
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if getattr(entity, "bot", False):
            continue
        if getattr(entity, "deleted", False):
            continue
        yield dialog


async def refresh_eligible_chats(client: TelegramClient):
    ids = set()
    async for dialog in iter_eligible_dialogs(client):
        ids.add(dialog.id)
    _eligible_chat_ids.clear()
    _eligible_chat_ids.update(ids)
    return ids


async def backlog_scan_chat(client: TelegramClient, dialog):
    """Pull any voice/round-video messages newer than this chat's cursor.
    First call for a chat seeds the cursor at the initial window instead.

    The cursor is always advanced to the chat's current top message id (free
    — it's already on the Dialog object), never to "the highest id we
    happened to keep after filtering". That way a chat with zero matching
    messages in its first-scan window still gets a correct, non-zero cursor,
    instead of getting stuck at 0 and having the next reconciliation pass
    treat it as never-scanned and pull its *entire* history.
    """
    chat_id = dialog.id
    row = db.get_chat(chat_id)
    prior_cursor = row["cursor_msg_id"] if row else 0
    top_id = dialog.message.id if dialog.message else 0

    if row is None:
        window = config.INITIAL_WINDOW_OVERRIDES.get(chat_id, config.DEFAULT_INITIAL_WINDOW)
        start = datetime.now(timezone.utc) - window
        db.init_chat_cursor(chat_id, dialog.name, cursor_msg_id=0)
        async for message in client.iter_messages(dialog.entity, reverse=True, offset_date=start):
            # Telegram/Telethon occasionally hands back a stray message
            # older than offset_date when nothing actually falls inside the
            # window (observed on sparse chats) — guard explicitly rather
            # than trust the server-side bound.
            if message.date < start:
                continue
            kind = classify(message)
            if kind:
                db.insert_item(chat_id, dialog.name, message.id, kind, message.date.isoformat())
    elif top_id > prior_cursor:
        async for message in client.iter_messages(dialog.entity, reverse=True, min_id=prior_cursor):
            kind = classify(message)
            if kind:
                db.insert_item(chat_id, dialog.name, message.id, kind, message.date.isoformat())
    else:
        # Nothing new since the cursor — no API call was made, so the
        # caller shouldn't pace as if one was.
        db.advance_cursor(chat_id, dialog.name, max(top_id, prior_cursor))
        return False

    db.advance_cursor(chat_id, dialog.name, max(top_id, prior_cursor))
    return True


async def backlog_scan_all(client: TelegramClient):
    async for dialog in iter_eligible_dialogs(client):
        try:
            made_request = await backlog_scan_chat(client, dialog)
        except Exception:
            log.exception("backlog scan failed for chat %s (%s)", dialog.id, dialog.name)
            made_request = True  # be conservative — still pace after a failure
        if made_request:
            # Deliberate pacing on top of Telethon's own flood-wait handling —
            # with 1000+ eligible chats, spacing requests out proactively is
            # safer than only reacting to flood-waits after they happen. Chats
            # with nothing new made no request, so there's nothing to pace.
            await asyncio.sleep(config.BACKLOG_SCAN_PACE_SECONDS)


async def reconciliation_loop(client: TelegramClient):
    """Runs the (paced) full backlog scan immediately, then repeats it as a
    downtime safety net. Deliberately a separate coroutine from worker_loop
    so the two run concurrently — items get queued and transcribed as they're
    found instead of waiting for the ~1391-chat scan to finish first."""
    while True:
        await refresh_eligible_chats(client)
        await backlog_scan_all(client)
        await asyncio.sleep(config.RECONCILE_INTERVAL_SECONDS)


def register_handlers(client: TelegramClient):
    @client.on(events.NewMessage(incoming=True))
    async def _on_new_message(event):
        if not event.is_private or event.chat_id not in _eligible_chat_ids:
            return
        kind = classify(event.message)
        if not kind:
            return
        chat = await event.get_chat()
        title = getattr(chat, "first_name", None) or getattr(chat, "title", None) or str(event.chat_id)
        db.insert_item(event.chat_id, title, event.message.id, kind, event.message.date.isoformat())
        db.advance_cursor(event.chat_id, title, event.message.id)

    @client.on(events.Raw(types=UpdateTranscribedAudio))
    async def _on_transcribed(update):
        fut = _pending_futures.get(update.transcription_id)
        if fut and not fut.done():
            fut.set_result(update)


async def worker_loop(client: TelegramClient):
    while True:
        item = db.next_pending()
        if item is None:
            await asyncio.sleep(config.WORKER_POLL_IDLE_SECONDS)
            continue

        db.set_status(item["id"], "processing")
        try:
            entity = await client.get_entity(item["chat_id"])
            result = await client(TranscribeAudioRequest(peer=entity, msg_id=item["msg_id"]))

            if not result.pending:
                db.set_status(item["id"], "done", text=result.text, transcription_id=result.transcription_id)
            else:
                fut = asyncio.get_event_loop().create_future()
                _pending_futures[result.transcription_id] = fut
                try:
                    update = await asyncio.wait_for(fut, timeout=config.TRANSCRIBE_TIMEOUT_SECONDS)
                    db.set_status(item["id"], "done", text=update.text, transcription_id=result.transcription_id)
                except asyncio.TimeoutError:
                    attempts = item["attempts"] + 1
                    if attempts < config.MAX_TRANSCRIBE_RETRIES:
                        # back in the queue — worker_loop will pick it up again
                        db.set_status(item["id"], "pending", attempts=attempts, transcription_id=result.transcription_id)
                    else:
                        db.set_status(
                            item["id"], "failed", attempts=attempts,
                            error=f"timed out after {attempts} attempts (120s each)",
                            transcription_id=result.transcription_id,
                        )
                finally:
                    _pending_futures.pop(result.transcription_id, None)
        except Exception as e:
            log.exception("transcribe failed for item %s", item["id"])
            db.set_status(item["id"], "error", error=str(e))

        await asyncio.sleep(config.WORKER_PACE_SECONDS)


async def run(client: TelegramClient):
    # If the process died mid-transcription last time (container restart,
    # host sleep/reboot — happens), any item stuck in 'processing' would
    # otherwise sit there forever since worker_loop only ever picks up
    # 'pending'. Requeue it; a repeat TranscribeAudioRequest is harmless.
    requeued = db.requeue_stuck_processing()
    if requeued:
        log.warning("requeued %d item(s) stuck in 'processing' from a previous run", requeued)

    await refresh_eligible_chats(client)
    register_handlers(client)
    log.info("starting: %d eligible DM chats, scanning while the worker transcribes in parallel", len(_eligible_chat_ids))

    await asyncio.gather(
        worker_loop(client),
        reconciliation_loop(client),
    )
