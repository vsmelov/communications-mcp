"""Personal Telegram MCP server (Telethon user session).

Exposes the user's own Telegram account to Claude via MCP (stdio).
Auth lives in sessions/mcp.session (created on first run, never committed).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from telethon import TelegramClient, functions, types, utils

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# на Windows консоль по умолчанию cp1252 — печать кириллицы в прогрессе иначе роняет процесс
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# httpx/openai логируют каждый POST на INFO — шумно, прогресс транскрибации печатается сам
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# $ за минуту речи — сверено с https://developers.openai.com/api/docs/pricing (2026-08-14),
# та же таблица, что в transcribe-mcp/server.py
STT_PRICING: dict[str, float] = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
    "gpt-4o-transcribe-diarize": 0.006,
    "whisper-1": 0.006,
}
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"  # самая дешёвая из приличных

_openai: OpenAI | None = None


def openai_client() -> OpenAI:
    global _openai
    if _openai is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY не задан — положите его в .env рядом с server.py")
        _openai = OpenAI()
    return _openai

INSTRUCTIONS = """\
Personal Telegram access via the user's own Telethon user session.

The tools here are NOT the full API surface — they are shortcuts for the
handful of scenarios that come up most often, and they are optimized for
those. For anything heavier, do NOT loop over these tools: call
`get_session_info` and write a direct Telethon script instead.

Rule of thumb:
  - a few messages, one chat, quick lookup  -> use the tools
  - whole histories, bulk media, stats, cross-chat scans, anything where you
    would otherwise call a tool in a loop -> write a script

Paging a tool from the model is roughly two orders of magnitude slower than a
script: every page costs a full inference round-trip, while Telethon streams
history internally. `dump` already covers the common export case; reach for a
script when `dump` does not fit.

When writing a script, ALWAYS copy the session file first (see
`get_session_info` -> `copy_first`). The MCP server holds the original open in
SQLite; a copy avoids lock contention.

What copying a session actually does — do not reason about this from the file
name alone:
  - A .session file is a local container for an auth_key, its DC, the salt and
    an entity cache. It is NOT an identity of its own.
  - Copying it does NOT create a second session or a second login. Every copy
    carries the SAME auth_key, so Telegram sees ONE authorization. The account's
    device list does not grow, no matter how many copies exist.
  - Telegram tracks authorizations, not connections. Several concurrent
    connections sharing one auth_key are normal MTProto — official clients open
    separate connections for download and upload.
  - Therefore copies do not "conflict" and none of them gets kicked. The copy
    exists purely to avoid two OS processes writing one SQLite file.

Consequences that DO matter:
  - Rate limits are per account, not per connection. Two heavy jobs running at
    once (e.g. a dump from each client) hit FloodWait sooner.
  - Revoking that device in Telegram, or the session expiring, kills every copy
    at once. Access cannot be revoked for one client individually.

ARCHIVED CHATS (the fast path). A background service (tg-archive, docker
compose) continuously keeps full dumps of selected chats on disk. For any
question in the context of one of those chats DO NOT page read_messages:
  1. call `archive_refresh(chat)` — it tops up the dump within seconds;
  2. Read/Grep the returned dump.md / dump.json paths directly.
`archive_status()` lists which chats are archived and how fresh they are.

CHAT WATCH (event-driven duty on a chat). When the user asks to "keep an eye
on" a chat and react to new messages inside THIS session, do not poll
read_messages on a timer. Run `tools/watch_chat.py` (in this server's folder)
under the Monitor tool (persistent): it listens on a copy of the session,
batches a person's consecutive messages (default 180 s of quiet = one
wake-up), catches up from `--state` on restart, and prints `BATCH …` /
`NEW {json}` lines to stdout; filter Monitor with the regex from
`python tools/watch_chat.py --print-filter`. Then read, act, and reply with
`send_message`. The Monitor lives only while the session is open. Details and
the exact command: README.md, section «Дежурство по чату».
"""


def _archived_chats_instructions() -> str:
    """Динамический хвост INSTRUCTIONS: какие чаты прямо сейчас в живом архиве.

    Список виден модели ещё до первого вызова тулов — фразы вида «посмотри
    чат с N и ответь…» сразу уходят в архивный путь, а не в пейджинг.
    """
    raw = os.getenv("TELEGRAM_ARCHIVE_ROOT", "").strip()
    if not raw:
        return ""
    lines = []
    try:
        for d in sorted(Path(raw).iterdir()):
            man_p = d / "manifest.json"
            man = json.loads(man_p.read_text(encoding="utf-8")) if man_p.is_file() else None
            if man:
                cov = man.get("covered") or {}
                full = (man.get("archive") or {}).get("backfill_done")
                lines.append(
                    f"  - {man.get('chat')} (id {man.get('chat_id')}) — "
                    f"{'full history' if full else 'recent history, backfill running'}, "
                    f"covered up to {cov.get('end')} UTC as of the server start"
                )
    except Exception:
        return ""
    if not lines:
        return ""
    return (
        "\nChats ALWAYS kept archived on disk right now (never page these):\n"
        + "\n".join(lines) + "\n"
    )


mcp = FastMCP("telegram", instructions=INSTRUCTIONS + _archived_chats_instructions())

_client: TelegramClient | None = None


def _parse_proxy(raw: str) -> tuple | None:
    """'direct' / '' -> None, иначе кортеж для Telethon (python-socks)."""
    raw = (raw or "").strip()
    if not raw or raw.lower() == "direct":
        return None
    u = urlparse(raw)
    import socks

    scheme = (u.scheme or "socks5").lower()
    ptype = {
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "socks4a": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }[scheme]
    return (ptype, u.hostname or "127.0.0.1", u.port or 1080)


def _proxy_chain(force: bool = False) -> list[str]:
    """Техники подключения по порядку до первой рабочей.

    С 04.10.2026 по умолчанию — автовыбор netpath.candidates(): .env-цепочка + системный прокси Windows +
    слушающиеся порты VPN-клиентов + direct, всё проверено параллельно, быстрые первыми (кэш 10 мин;
    force=True — пересканировать). NETPATH_AUTO=0 возвращает старое поведение ниже.
    """
    if os.getenv("NETPATH_AUTO", "1") != "0":
        import netpath
        return netpath.candidates(force=force)
    return _static_chain()


def _static_chain() -> list[str]:
    """Техники подключения по порядку до первой рабочей (как в tg-archive).

    TELEGRAM_PROXY_CHAIN (через запятую: direct, http://..., socks5://...)
    переопределяет всё. Иначе — старый TELEGRAM_PROXY, затем direct как фолбэк
    (VPN в TUN-режиме: прокси-порт не слушается, а прямое соединение идёт через
    туннель). Дубли убираем, порядок сохраняем.
    """
    raw = os.getenv("TELEGRAM_PROXY_CHAIN", "").strip()
    if raw:
        chain = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        p = os.getenv("TELEGRAM_PROXY", "").strip()
        chain = ([p] if p else []) + ["direct"]
    seen: set[str] = set()
    out: list[str] = []
    for x in chain:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out or ["direct"]


def _proxy() -> tuple | None:
    """Первый элемент цепочки — для обратной совместимости (get_session_info)."""
    return _parse_proxy(_proxy_chain()[0])


_session_cached: Path | None = None


def _session_path() -> Path:
    """Session file to use, without the .session suffix (made once per process)."""
    global _session_cached
    if _session_cached is None:
        _session_cached = _resolve_session_path()
    return _session_cached


def _resolve_session_path() -> Path:
    """Session file to use, without the .session suffix.

    Override with TELEGRAM_SESSION when more than one MCP client runs this
    server: the session is a SQLite file and two processes sharing it will
    fight over locks. Give each client its own copy.
    """
    raw = os.getenv("TELEGRAM_SESSION", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.suffix == ".session":
            p = p.with_suffix("")
        return p if p.is_absolute() else ROOT / p
    return _private_copy(ROOT / "sessions" / "mcp")


def _private_copy(master: Path) -> Path:
    """Per-process copy of the shared session file.

    Every Claude session starts its own server.py, and all of them opened
    sessions/mcp.session: Telethon keeps that SQLite file open and writes to it,
    so a second process got `database is locked` - which surfaced as "Telegram
    unreachable" although the network was fine. A copy carries the same
    auth_key (one authorization, nothing new in the device list), so each
    process just gets its own file. Copies of processes that are gone are
    removed: on Windows an open file cannot be deleted, so a failed unlink means
    its owner is still running.
    """
    import sqlite3

    run = master.parent / "run"
    run.mkdir(exist_ok=True)
    for old in run.glob("mcp-*.session*"):
        try:
            old.unlink()
        except OSError:
            pass
    copy = run / f"mcp-{os.getpid()}"
    src = sqlite3.connect(f"file:{master}.session?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(f"{copy}.session")
        try:
            src.backup(dst)   # consistent snapshot even while another process writes
        finally:
            dst.close()
    finally:
        src.close()
    return copy


_client_lock = asyncio.Lock()


async def get_client() -> TelegramClient:
    """Подключение по цепочке _proxy_chain(): первая техника, через которую
    удалось connect() + авторизованная сессия, остаётся на всю жизнь процесса.

    Под замком: параллельные вызовы тулов иначе открывали по клиенту на один и
    тот же файл сессии одновременно."""
    if _client is not None and _client.is_connected():
        return _client
    async with _client_lock:
        return await _connect()


async def _connect() -> TelegramClient:
    global _client
    if _client is not None and _client.is_connected():
        return _client

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    timeout = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "10"))
    session = str(_session_path())

    # уже был клиент (например, отвалился коннект) — пробуем поднять его же
    if _client is not None:
        try:
            await _client.connect()
            if await _client.is_user_authorized():
                return _client
        except Exception as e:  # noqa: BLE001
            logging.warning("telegram reconnect failed: %s", e)

    errors: list[str] = []
    def techniques():
        # сначала кэш netpath; если ни одна не подключилась — пересканировать сеть (VPN мог смениться)
        tried: set[str] = set()
        for force in (False, True):
            for t in _proxy_chain(force=force):
                if t not in tried:
                    tried.add(t)
                    yield t

    for technique in techniques():
        # одна попытка на технику: Telethon по умолчанию ретраит 5 раз, и мёртвый путь съедал весь таймаут MCP
        kwargs: dict = {"timeout": timeout, "connection_retries": 1}
        proxy = _parse_proxy(technique)
        if proxy:
            kwargs["proxy"] = proxy
        client = TelegramClient(session, api_id, api_hash, **kwargs)
        try:
            await client.connect()
        except Exception as e:  # noqa: BLE001
            if "database is locked" in str(e):
                # не сеть: файл сессии держит другой процесс, другие прокси тут не помогут
                raise RuntimeError(
                    f"Telegram session file is locked by another process: {session}.session "
                    "(not a network problem). Set TELEGRAM_SESSION to a private copy."
                ) from e
            errors.append(f"{technique}: {type(e).__name__}: {e}")
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            continue
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                f"Session is not authorized: {session}.session — "
                f"re-copy a valid session file there."
            )
        logging.info("telegram connected via %s", technique)
        _client = client
        return _client

    raise RuntimeError(
        "Telegram unreachable via every technique in TELEGRAM_PROXY_CHAIN "
        f"({', '.join(_proxy_chain())}). Errors: " + " | ".join(errors)
    )


async def resolve_chat(client: TelegramClient, chat: str):
    """Resolve a chat by id, @username, phone, invite link, or title substring."""
    chat = chat.strip()
    try:
        return await client.get_entity(int(chat))
    except (ValueError, TypeError):
        pass
    except Exception:
        pass
    try:
        return await client.get_entity(chat)
    except Exception:
        pass
    needle = chat.casefold()
    async for dialog in client.iter_dialogs():
        if needle in (dialog.name or "").casefold():
            return dialog.entity
    raise ValueError(f"Chat not found: {chat!r}")


def _entity_name(entity) -> str:
    return utils.get_display_name(entity) or str(getattr(entity, "id", "?"))


async def _format_message(msg, client: TelegramClient) -> dict:
    sender_name = None
    if msg.sender_id:
        try:
            sender = msg.sender or await msg.get_sender()
            sender_name = _entity_name(sender)
        except Exception:
            sender_name = str(msg.sender_id)
    out: dict = {
        "id": msg.id,
        "date": msg.date.astimezone().strftime("%Y-%m-%d %H:%M") if msg.date else None,
        "from": sender_name,
        "text": msg.text or "",
    }
    if msg.media:
        out["media"] = type(msg.media).__name__.replace("MessageMedia", "").lower()
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        out["reply_to_id"] = msg.reply_to.reply_to_msg_id
    if getattr(msg, "out", False):
        out["outgoing"] = True
    return out


@mcp.tool()
async def get_me() -> dict:
    """Get info about the logged-in Telegram account."""
    client = await get_client()
    me = await client.get_me()
    return {
        "id": me.id,
        "name": _entity_name(me),
        "username": me.username,
        "phone": me.phone,
    }


@mcp.tool()
async def list_dialogs(limit: int = 20, unread_only: bool = False, archived: bool = False) -> list[dict]:
    """List recent chats (dialogs), newest first.

    Args:
        limit: max number of dialogs to return.
        unread_only: only chats with unread messages.
        archived: include archived chats instead of the main list.
    """
    client = await get_client()
    result = []
    async for dialog in client.iter_dialogs(archived=archived):
        if unread_only and not dialog.unread_count:
            continue
        last = dialog.message
        result.append(
            {
                "id": dialog.id,
                "name": dialog.name,
                "type": "channel" if dialog.is_channel else "group" if dialog.is_group else "user",
                "unread": dialog.unread_count,
                "last_message_date": last.date.astimezone().strftime("%Y-%m-%d %H:%M") if last and last.date else None,
                "last_message": (last.text or "")[:80] if last else None,
            }
        )
        if len(result) >= limit:
            break
    return result


@mcp.tool()
async def read_messages(chat: str, limit: int = 20, before_id: int = 0) -> dict:
    """Read recent messages from a chat, newest first.

    Args:
        chat: chat id, @username, phone, or part of the chat title.
        limit: max messages to return.
        before_id: if set, return messages older than this message id (for paging).
    """
    client = await get_client()
    entity = await resolve_chat(client, chat)
    kwargs: dict = {"limit": limit}
    if before_id:
        kwargs["max_id"] = before_id
    messages = [await _format_message(m, client) async for m in client.iter_messages(entity, **kwargs)]
    return {"chat": _entity_name(entity), "chat_id": entity.id, "messages": messages}


@mcp.tool()
async def search_messages(query: str, chat: str = "", limit: int = 20) -> list[dict]:
    """Search messages by text. Searches inside one chat if given, otherwise across all chats.

    Args:
        query: text to search for.
        chat: optional chat id, @username, or title substring to limit the search.
        limit: max results.
    """
    client = await get_client()
    if chat:
        entity = await resolve_chat(client, chat)
        out = []
        async for m in client.iter_messages(entity, search=query, limit=limit):
            fm = await _format_message(m, client)
            fm["chat"] = _entity_name(entity)
            out.append(fm)
        return out
    res = await client(
        functions.messages.SearchGlobalRequest(
            q=query,
            filter=types.InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=types.InputPeerEmpty(),
            offset_id=0,
            limit=limit,
        )
    )
    entities = {}
    for e in list(res.chats) + list(res.users):
        entities[utils.get_peer_id(e)] = e
    out = []
    for m in res.messages:
        peer_id = utils.get_peer_id(m.peer_id)
        chat_entity = entities.get(peer_id)
        out.append(
            {
                "id": m.id,
                "chat": _entity_name(chat_entity) if chat_entity else str(peer_id),
                "chat_id": peer_id,
                "date": m.date.astimezone().strftime("%Y-%m-%d %H:%M") if m.date else None,
                "text": (getattr(m, "message", "") or "")[:300],
            }
        )
    return out


@mcp.tool()
async def send_message(chat: str, text: str, reply_to: int = 0) -> dict:
    """Send a text message to a chat from the user's own account.

    Args:
        chat: chat id, @username, phone, or part of the chat title.
        text: message text (Markdown supported).
        reply_to: optional message id to reply to.
    """
    client = await get_client()
    entity = await resolve_chat(client, chat)
    msg = await client.send_message(entity, text, reply_to=reply_to or None)
    return {"sent": True, "chat": _entity_name(entity), "message_id": msg.id}


@mcp.tool()
async def send_file(chat: str, file_path: str, caption: str = "") -> dict:
    """Send a local file to a chat (photo, document, etc.).

    Args:
        chat: chat id, @username, phone, or part of the chat title.
        file_path: absolute path of the file on this computer.
        caption: optional caption text.
    """
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")
    client = await get_client()
    entity = await resolve_chat(client, chat)
    msg = await client.send_file(entity, str(path), caption=caption or None)
    return {"sent": True, "chat": _entity_name(entity), "message_id": msg.id}


@mcp.tool()
async def download_media(chat: str, message_id: int, save_dir: str = "") -> dict:
    """Download the media attachment of a message to disk.

    Args:
        chat: chat id, @username, or title substring.
        message_id: id of the message containing media.
        save_dir: target directory (default: ./downloads next to the server).
    """
    client = await get_client()
    entity = await resolve_chat(client, chat)
    msg = await client.get_messages(entity, ids=message_id)
    if not msg or not msg.media:
        raise ValueError("Message has no media")
    target = Path(save_dir) if save_dir else ROOT / "downloads"
    target.mkdir(parents=True, exist_ok=True)
    saved = await client.download_media(msg, file=str(target) + os.sep)
    return {"saved_to": str(saved)}


@mcp.tool()
async def get_chat_info(chat: str) -> dict:
    """Get details about a chat, group, channel, or user."""
    client = await get_client()
    entity = await resolve_chat(client, chat)
    info: dict = {
        "id": entity.id,
        "name": _entity_name(entity),
        "type": type(entity).__name__.lower(),
    }
    for attr in ("username", "phone", "about"):
        val = getattr(entity, attr, None)
        if val:
            info[attr] = val
    if isinstance(entity, (types.Channel, types.Chat)):
        try:
            full = await client(functions.channels.GetFullChannelRequest(entity)) if isinstance(
                entity, types.Channel
            ) else await client(functions.messages.GetFullChatRequest(entity.id))
            info["participants"] = getattr(full.full_chat, "participants_count", None)
            info["about"] = getattr(full.full_chat, "about", "") or info.get("about")
        except Exception:
            pass
    return info


@mcp.tool()
async def mark_read(chat: str) -> dict:
    """Mark all messages in a chat as read."""
    client = await get_client()
    entity = await resolve_chat(client, chat)
    await client.send_read_acknowledge(entity)
    return {"marked_read": True, "chat": _entity_name(entity)}


@mcp.tool()
async def get_session_info() -> dict:
    """Where the Telethon session lives, so you can script against it directly.

    Use this whenever the tools in this server would have to be called in a
    loop (full histories, bulk media, stats, cross-chat scans). A script with
    Telethon is ~100x faster than model-driven paging.

    Always work on a COPY of the session file: the MCP server keeps the
    original open in SQLite.
    """
    session = _session_path().with_suffix(".session")
    return {
        "session_path": str(session),
        "session_exists": session.is_file(),
        "session_env_override": os.getenv("TELEGRAM_SESSION") or None,
        "env_path": str(ROOT / ".env"),
        "server_root": str(ROOT),
        "python": sys.executable,
        "api_id": os.getenv("TELEGRAM_API_ID"),
        "api_hash_env_var": "TELEGRAM_API_HASH",
        "proxy": os.getenv("TELEGRAM_PROXY") or None,
        "proxy_chain": _proxy_chain(),
        "copy_first": (
            "Copy sessions/mcp.session to a temp path and open THAT — the "
            "running MCP server holds the original open."
        ),
        "snippet": (
            "import shutil, asyncio\n"
            "from pathlib import Path\n"
            "from dotenv import load_dotenv\n"
            "from telethon import TelegramClient\n"
            "import os\n"
            f"ROOT = Path(r'{ROOT}')\n"
            "load_dotenv(ROOT / '.env')\n"
            "tmp = Path(os.environ['TEMP']) / 'tg_copy.session'\n"
            "shutil.copy(ROOT / 'sessions' / 'mcp.session', tmp)\n"
            "client = TelegramClient(str(tmp.with_suffix('')), "
            "int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'])\n"
            "# add proxy=... if TELEGRAM_PROXY is set (see _proxy() in server.py)\n"
            "async def main():\n"
            "    async with client:\n"
            "        async for m in client.iter_messages(CHAT):\n"
            "            ...\n"
            "asyncio.run(main())"
        ),
        "session_model": (
            "A copy is NOT a second session. Every copy carries the same "
            "auth_key, so Telegram sees one authorization and the account's "
            "device list does not grow. Multiple concurrent connections on one "
            "auth_key are normal MTProto, so copies do not conflict and none "
            "gets disconnected. Copy only to stop two processes writing one "
            "SQLite file."
        ),
        "note": (
            "Rate limits are per account, not per connection — parallel heavy "
            "jobs hit FloodWait sooner. Revoking the device or letting the "
            "session expire kills every copy at once."
        ),
    }


#
# ---------------------------------------------------------------- dump ------
#

# Subfolder per media kind. Order matters: the first match wins, so the more
# specific Telethon flags (voice, video_note, gif) are checked before the
# generic ones (video, document) they would also satisfy.
_MEDIA_KINDS = (
    ("sticker", "stickers"),
    ("voice", "voice"),
    ("video_note", "video_notes"),
    ("gif", "gifs"),
    ("video", "videos"),
    ("audio", "audio"),
    ("photo", "photos"),
    ("document", "documents"),
)

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _parse_ts(raw, default: datetime) -> datetime:
    """Parse a timestamp into an aware UTC datetime.

    Everything in a dump is UTC on purpose — local offsets are a constant
    source of off-by-hours confusion. Accepts, in order of preference:
      - unix epoch seconds (int, float, or digit string) — unambiguous
      - ISO 8601; a naive value is read as UTC, never as local time
      - "" or "NOW" -> default
    """
    if raw is None:
        return default
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)

    raw = str(raw).strip()
    if not raw or raw.upper() == "NOW":
        return default
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(
                f"Cannot parse timestamp {raw!r}. Use unix seconds (1755091800), "
                f"ISO (2026-08-13 or 2026-08-13T14:30, read as UTC), or 'NOW'."
            )
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}


def _slugify(name: str, max_words: int = 4) -> str:
    """ASCII slug for a chat title: 'Иван Иванов' -> 'ivan-ivanov'."""
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in (name or "").casefold())
    words = re.findall(r"[a-z0-9]+", latin)
    return "-".join(words[:max_words]) or "chat"


def _media_kind(msg) -> str | None:
    """Downloadable media category, or None when there is nothing to fetch."""
    if not msg.media or isinstance(msg.media, types.MessageMediaWebPage):
        return None
    for attr, folder in _MEDIA_KINDS:
        if getattr(msg, attr, None):
            return folder
    return "other"


def _safe_name(msg, kind: str) -> str:
    """Collision-free filename: the message id always prefixes the original name."""
    original = getattr(msg.file, "name", None) if msg.file else None
    if original:
        cleaned = _UNSAFE_CHARS.sub("_", original).strip(". ")[:120]
    else:
        ext = (getattr(msg.file, "ext", None) if msg.file else None) or ""
        cleaned = f"{kind.rstrip('s')}{_UNSAFE_CHARS.sub('_', ext)}"
    return f"{msg.id}_{cleaned}"


def _render_md(chat_name: str, rows: list[dict], meta: dict) -> str:
    lines = [
        f"# {chat_name}",
        "",
        f"- Период (UTC): {meta['start']} — {meta['end']}",
        f"- Unix: {meta['start_ts']} — {meta['end_ts']}",
        f"- Сообщений: {meta['messages']}",
        f"- Вложений скачано: {meta['downloaded']} (пропущено: {meta['skipped']})",
        "",
        "---",
        "",
    ]
    for r in rows:
        text = (r["text"] or "").replace("\n", "\n  ")
        head = f"**[{r['id']}] {r['date_utc']} UTC — {r['from']}**"
        if r.get("reply_to_id"):
            head += f"  ↩︎ {r['reply_to_id']}"
        lines.append(head)
        if text:
            lines.append(f"  {text}")
        if r.get("media"):
            dur = f", {r['duration_sec']:.0f}с" if r.get("duration_sec") else ""
            if r.get("file"):
                lines.append(f"  📎 [{r['media']}{dur}]({r['file']})")
            else:
                note = r.get("skipped_reason") or "не скачивалось"
                lines.append(f"  📎 {r['media']}{dur} — _{note}_")
            if r.get("transcript"):
                lines.append(f"  🗣️ _{r['transcript'].strip()}_")
            elif r.get("transcript_error"):
                lines.append(f"  🗣️ _расшифровка не удалась: {r['transcript_error']}_")
        lines.append("")
    return "\n".join(lines)


def _render_html(chat_name: str, rows: list[dict], meta: dict) -> str:
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{_html.escape(chat_name)}</title>",
        "<style>"
        "body{font:15px/1.5 system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;"
        "background:#0f1115;color:#e6e6e6}"
        ".m{border-left:3px solid #2a2f3a;padding:.4rem 0 .4rem .8rem;margin:.6rem 0}"
        ".m.out{border-color:#3d6fd9}"
        ".h{font-size:12px;color:#8b93a7;margin-bottom:.25rem}"
        ".t{white-space:pre-wrap;word-wrap:break-word}"
        "img,video{max-width:100%;border-radius:8px;margin-top:.4rem}"
        "a{color:#7aa2f7}"
        "</style>",
        f"<h1>{_html.escape(chat_name)}</h1>",
        f"<p>{meta['start']} — {meta['end']} UTC · {meta['messages']} сообщений · "
        f"{meta['downloaded']} вложений</p><hr>",
    ]
    for r in rows:
        cls = "m out" if r.get("outgoing") else "m"
        parts.append(f"<div class='{cls}'>")
        parts.append(
            f"<div class='h'>[{r['id']}] {_html.escape(r['date_utc'] or '')} UTC — "
            f"{_html.escape(r['from'] or '')}</div>"
        )
        if r["text"]:
            parts.append(f"<div class='t'>{_html.escape(r['text'])}</div>")
        f = r.get("file")
        if f:
            src = _html.escape(f)
            kind = r.get("media")
            if kind == "photos":
                parts.append(f"<img src='{src}' loading='lazy'>")
            elif kind in ("videos", "video_notes", "gifs"):
                parts.append(f"<video controls src='{src}'></video>")
            elif kind in ("voice", "audio"):
                parts.append(f"<audio controls src='{src}'></audio>")
            else:
                parts.append(f"<a href='{src}'>{src}</a>")
        elif r.get("media"):
            note = r.get("skipped_reason") or "не скачивалось"
            parts.append(f"<div class='h'>📎 {r['media']} — {_html.escape(note)}</div>")
        if r.get("transcript"):
            parts.append(f"<div class='t'>🗣️ {_html.escape(r['transcript'].strip())}</div>")
        elif r.get("transcript_error"):
            parts.append(f"<div class='h'>🗣️ расшифровка не удалась: {_html.escape(r['transcript_error'])}</div>")
        parts.append("</div>")
    return "\n".join(parts)


async def _row(msg, sender_cache: dict[int, str]) -> dict:
    """Message -> plain dict. Sender names are cached; resolving them per
    message would otherwise cost an API call each."""
    name = None
    if msg.sender_id is not None:
        name = sender_cache.get(msg.sender_id)
        if name is None:
            try:
                name = _entity_name(msg.sender or await msg.get_sender())
            except Exception:
                name = str(msg.sender_id)
            sender_cache[msg.sender_id] = name

    row: dict = {
        "id": msg.id,
        "ts": int(msg.date.timestamp()) if msg.date else None,
        "date_utc": _utc_str(msg.date) if msg.date else None,
        "from": name,
        "text": msg.text or "",
    }
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        row["reply_to_id"] = msg.reply_to.reply_to_msg_id
    if getattr(msg, "out", False):
        row["outgoing"] = True

    kind = _media_kind(msg)
    if kind:
        row["media"] = kind
        row["size_bytes"] = getattr(msg.file, "size", None) if msg.file else None
        if kind in ("voice", "video_notes", "audio", "videos"):
            # длительность приходит в метаданных сообщения бесплатно, без скачивания —
            # на этом строится оценка стоимости транскрибации ДО того, как что-то тратится
            dur = getattr(msg.file, "duration", None) if msg.file else None
            if dur:
                row["duration_sec"] = round(float(dur), 1)
    elif msg.media:
        row["media"] = "webpage"
    return row


def _load_rows(target: Path) -> list[dict]:
    """Messages already stored by an earlier run of this dump, if any."""
    p = target / "dump.json"
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("messages", [])
    except Exception:
        return []


def _read_manifest(d: Path) -> dict | None:
    p = d / "manifest.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


@mcp.tool()
async def dump_search(chat: str = "", chat_id: int = 0) -> list[dict]:
    """List dumps already on disk, so you can extend one instead of redoing it.

    Call this BEFORE starting a new dump. If a dump for the same chat already
    covers part of the range you want, pass its directory to `dump(out_dir=...)`
    with resume=True and only the gaps get fetched.

    Args:
        chat: optional title substring to filter on (case-insensitive).
        chat_id: optional exact chat id to filter on.
    """
    bases = [ROOT / "dumps"]
    archive_root = os.getenv("TELEGRAM_ARCHIVE_ROOT", "").strip()
    if archive_root and Path(archive_root).is_dir():
        bases.append(Path(archive_root))
    found = []
    for base in bases:
        if not base.is_dir():
            continue
        found.extend(
            _dump_entry(d, chat, chat_id, archive=(base != ROOT / "dumps"))
            for d in sorted(base.iterdir()) if d.is_dir()
        )
    return [f for f in found if f]


def _dump_entry(d: Path, chat: str, chat_id: int, archive: bool) -> dict | None:
    man = _read_manifest(d)
    if not man:
        return None
    if chat_id and man.get("chat_id") != chat_id:
        return None
    if chat and chat.casefold() not in (man.get("chat") or "").casefold():
        return None
    cov = man.get("covered", {})
    att = man.get("attachments", {})
    entry = {
        "dir": str(d),
        "chat": man.get("chat"),
        "chat_id": man.get("chat_id"),
        "messages": man.get("messages"),
        "covered_ts": [cov.get("start_ts"), cov.get("end_ts")],
        "covered_utc": [cov.get("start"), cov.get("end")],
        "oldest_id": man.get("oldest_id"),
        "newest_id": man.get("newest_id"),
        "attachments_downloaded": att.get("downloaded"),
        "attachments_pending": att.get("pending"),
        "attachment_kinds": att.get("kinds"),
        "attachments_max_mb": att.get("max_mb"),
        "updated_ts": man.get("updated_ts"),
    }
    if archive:
        # живой архив tg-archive: пополняется постоянно, "todt" не фиксирован
        entry["archive"] = True
        entry["backfill_done"] = (man.get("archive") or {}).get("backfill_done")
    return entry


@mcp.tool()
async def dump(
    chat: str,
    out_dir: str = "",
    format: str = "md",
    start_timestamp: str = "",
    end_timestamp: str = "",
    download_attachments: bool = False,
    download_attachments_max_mb: float = 30.0,
    attachment_kinds: str = "",
    max_concurrent_downloads: int = 8,
    resume: bool = True,
    transcribe: bool = False,
    transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL,
    transcribe_kinds: str = "voice,video_notes",
    max_concurrent_transcriptions: int = 5,
) -> dict:
    """Export a whole date range of a chat to disk in one call.

    Much faster than paging read_messages: Telethon streams the history
    internally and attachments are fetched concurrently.

    Args:
        chat: chat id, @username, phone, or part of the chat title.
        out_dir: override the target directory. Default:
            ./dumps/chat-<id>-<slug>-fromdt-<UTC>-todt-<UTC>/
        format: "md", "json", "html", or "all".
        start_timestamp: unix seconds (preferred) or ISO read as UTC. Default: 7 days ago.
        end_timestamp: unix seconds (preferred) or ISO read as UTC. Default: NOW.
        download_attachments: also fetch media into attachments/<kind>/ subfolders
            (photos, videos, voice, video_notes, audio, gifs, stickers, documents).
        download_attachments_max_mb: skip any single attachment larger than this.
        attachment_kinds: comma-separated whitelist, e.g. "photos,voice". Empty
            means every kind. Sizes are always reported even when nothing is
            downloaded, so you can run once dry and then pick.
        max_concurrent_downloads: parallel download slots.
        resume: reuse messages/attachments already in out_dir and fetch only the
            gaps. Run `dump_search` first to find a directory worth resuming.
        transcribe: transcribe voice/video-note messages (see transcribe_kinds) via
            OpenAI STT and write the text into each row (md/html/json) as `transcript`.
            ALWAYS COSTS REAL MONEY — call dump with transcribe=False first (the
            default) to read `transcription_estimate` in the response, which is
            computed for free from Telegram's own message metadata (duration is
            reported without downloading anything), then decide. Implies
            download_attachments=True (a local file is required to transcribe).
        transcribe_model: OpenAI STT model. Default gpt-4o-mini-transcribe — the
            cheapest ($0.003/min). Other options: gpt-transcribe, gpt-4o-transcribe,
            gpt-4o-transcribe-diarize, whisper-1 (no reason to use whisper-1 here).
        transcribe_kinds: comma-separated subset of voice,video_notes,audio,videos.
            Default "voice,video_notes" — regular voice messages + round video notes.
        max_concurrent_transcriptions: parallel OpenAI STT calls.

    Messages are fetched and written first, attachments second, transcription
    third — so an interrupted run still leaves a valid, resumable transcript.
    Re-running with transcribe=True skips files that already have a transcript.

    All timestamps in the output are UTC plus a raw unix `ts` field — local
    offsets are deliberately avoided.

    Progress (files done/total, ETA, running cost) is printed as it happens —
    visible in the process log, not in this call's return value.
    """
    fmt = format.strip().lower()
    if fmt not in ("md", "json", "html", "all"):
        raise ValueError("format must be one of: md, json, html, all")

    now = datetime.now(timezone.utc)
    end_dt = _parse_ts(end_timestamp, now)
    start_dt = _parse_ts(start_timestamp, now - timedelta(days=7))
    if start_dt >= end_dt:
        raise ValueError("start_timestamp must be earlier than end_timestamp")

    client = await get_client()
    entity = await resolve_chat(client, chat)
    chat_name = _entity_name(entity)

    if out_dir:
        target = Path(out_dir)
    else:
        stamp = "%Y%m%dT%H%M%SZ"
        target = ROOT / "dumps" / (
            f"chat-{entity.id}-{_slugify(chat_name)}"
            f"-fromdt-{start_dt.astimezone(timezone.utc):{stamp}}"
            f"-todt-{end_dt.astimezone(timezone.utc):{stamp}}"
        )
    target.mkdir(parents=True, exist_ok=True)
    media_root = target / "attachments"

    known_kinds = {folder for _, folder in _MEDIA_KINDS} | {"other"}
    wanted_kinds = {k.strip().lower() for k in attachment_kinds.split(",") if k.strip()}
    unknown = wanted_kinds - known_kinds
    if unknown:
        raise ValueError(
            f"Unknown attachment_kinds: {sorted(unknown)}. "
            f"Valid: {sorted(known_kinds)}"
        )

    duration_kinds = {"voice", "video_notes", "audio", "videos"}  # виды, у которых есть длительность
    transcribe_kind_set = {k.strip().lower() for k in transcribe_kinds.split(",") if k.strip()}
    bad_transcribe_kinds = transcribe_kind_set - duration_kinds
    if bad_transcribe_kinds:
        raise ValueError(
            f"transcribe_kinds must be a subset of {sorted(duration_kinds)}, got {sorted(bad_transcribe_kinds)}"
        )
    if transcribe_model not in STT_PRICING:
        raise ValueError(f"Unknown transcribe_model {transcribe_model!r}. Options: {sorted(STT_PRICING)}")
    if transcribe:
        download_attachments = True  # без локального файла транскрибировать нечего
        if wanted_kinds:
            wanted_kinds |= transcribe_kind_set  # не дать основному фильтру исключить нужные для расшифровки виды

    max_bytes = int(download_attachments_max_mb * 1024 * 1024)
    sender_cache: dict[int, str] = {}
    cached: dict[int, object] = {}  # id -> Message, reused by the media phase

    # ---- phase 1: messages -------------------------------------------------
    by_id = {r["id"]: r for r in (_load_rows(target) if resume else [])}
    reused = len(by_id)

    async def collect(**kw) -> None:
        async for msg in client.iter_messages(entity, **kw):
            if msg.date and msg.date < start_dt:
                break
            if msg.date and msg.date > end_dt:
                continue
            by_id[msg.id] = await _row(msg, sender_cache)
            cached[msg.id] = msg

    if by_id:
        # Only the edges are missing: newer than what we have, and older than
        # what we have if the caller now asks for a wider window.
        await collect(min_id=max(by_id))
        if not any(r["ts"] and r["ts"] <= start_dt.timestamp() for r in by_id.values()):
            await collect(offset_id=min(by_id))
    else:
        # offset_date is only safe when the caller pinned an explicit end;
        # passing it for "NOW" can clip the newest message.
        pinned = str(end_timestamp).strip() and str(end_timestamp).strip().upper() != "NOW"
        await collect(**({"offset_date": end_dt} if pinned else {}))

    rows = sorted(by_id.values(), key=lambda r: r["id"], reverse=True)
    fetched_now = len(by_id) - reused

    # ---- phase 2: attachments ---------------------------------------------
    sem = asyncio.Semaphore(max(1, max_concurrent_downloads))
    pending: list[dict] = []
    for r in rows:
        kind = r.get("media")
        if not kind or kind == "webpage":
            continue
        if wanted_kinds and kind not in wanted_kinds:
            continue
        size = r.get("size_bytes")
        if size and size > max_bytes:
            r["skipped_reason"] = f"больше {download_attachments_max_mb} МБ"
            continue
        r.pop("skipped_reason", None)
        have = r.get("file")
        if have and (target / have).is_file():
            continue  # already on disk from an earlier run
        r.pop("file", None)
        pending.append(r)

    if download_attachments and pending:
        missing = [r["id"] for r in pending if r["id"] not in cached]
        for i in range(0, len(missing), 100):  # batch, not one call per file
            for m in await client.get_messages(entity, ids=missing[i:i + 100]):
                if m:
                    cached[m.id] = m

        async def fetch(row: dict) -> None:
            msg = cached.get(row["id"])
            if msg is None:
                row["skipped_reason"] = "сообщение недоступно"
                return
            kind = row["media"]
            folder = media_root / kind
            async with sem:
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    saved = await client.download_media(
                        msg, file=str(folder / _safe_name(msg, kind))
                    )
                    if saved:
                        row["file"] = str(Path(saved).relative_to(target)).replace("\\", "/")
                except Exception as exc:  # one bad file must not kill the export
                    row["skipped_reason"] = f"ошибка загрузки: {exc}"

        await asyncio.gather(*(fetch(r) for r in pending))

    # ---- phase 3: transcription --------------------------------------------
    transcribed_now = 0
    transcribe_failed = 0
    transcribed_sec = 0.0
    if transcribe:
        to_transcribe = [
            r for r in rows
            if r.get("media") in transcribe_kind_set and r.get("file")
            and not r.get("transcript") and not r.get("transcript_error")
        ]
        total = len(to_transcribe)
        tsem = asyncio.Semaphore(max(1, max_concurrent_transcriptions))
        rate = STT_PRICING[transcribe_model]
        t0 = time.monotonic()
        done = 0
        run_cost = 0.0

        async def transcribe_one(row: dict) -> None:
            nonlocal done, run_cost, transcribed_now, transcribe_failed, transcribed_sec
            path = target / row["file"]
            async with tsem:
                try:
                    # OpenAI отклоняет расширение .oga ("Unsupported file format oga"),
                    # хотя это тот же Ogg/Opus, что и .ogg — подменяем только имя для API,
                    # файл на диске не трогаем
                    upload_name = path.name
                    if upload_name.lower().endswith(".oga"):
                        upload_name = upload_name[:-4] + ".ogg"
                    with open(path, "rb") as f:
                        resp = await asyncio.to_thread(
                            openai_client().audio.transcriptions.create,
                            model=transcribe_model, file=(upload_name, f.read()), response_format="text",
                        )
                    row["transcript"] = resp if isinstance(resp, str) else getattr(resp, "text", str(resp))
                    row["transcript_model"] = transcribe_model
                    transcribed_now += 1
                    dur = row.get("duration_sec") or 0.0
                    transcribed_sec += dur
                    run_cost += dur / 60 * rate
                except Exception as exc:  # one bad file must not kill the export
                    row["transcript_error"] = str(exc)[:200]
                    transcribe_failed += 1
            done += 1
            elapsed = time.monotonic() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(f"[transcribe] {done}/{total} — прошло {elapsed:.0f}с, ETA ~{eta:.0f}с, "
                  f"в этом запуске ${run_cost:.4f}", flush=True)

        if to_transcribe:
            print(f"[transcribe] {total} файлов ({sorted(transcribe_kind_set)}), модель {transcribe_model}",
                  flush=True)
            await asyncio.gather(*(transcribe_one(r) for r in to_transcribe))

    downloaded = sum(1 for r in rows if r.get("file"))
    meta = {
        "start_ts": int(start_dt.timestamp()),
        "end_ts": int(end_dt.timestamp()),
        "start": _utc_str(start_dt),
        "end": _utc_str(end_dt),
        "tz": "UTC",
        "messages": len(rows),
        "downloaded": downloaded,
        "skipped": sum(1 for r in rows if r.get("skipped_reason")),
    }

    written: dict[str, str] = {}
    wanted = ("md", "json", "html") if fmt == "all" else (fmt,)
    if "json" in wanted:
        p = target / "dump.json"
        p.write_text(
            json.dumps({"chat": chat_name, "chat_id": entity.id, "meta": meta, "messages": rows},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written["json"] = str(p)
    if "md" in wanted:
        p = target / "dump.md"
        p.write_text(_render_md(chat_name, rows, meta), encoding="utf-8")
        written["md"] = str(p)
    if "html" in wanted:
        p = target / "dump.html"
        p.write_text(_render_html(chat_name, rows, meta), encoding="utf-8")
        written["html"] = str(p)

    by_kind: dict[str, int] = {}
    downloaded_bytes = 0
    # What EXISTS in this range, downloaded or not. `in_chat_mb` is the full
    # size on Telegram; `within_cap_mb` is what a real run would actually pull
    # at the current max_mb. `duration_sec` needs no download at all — Telegram
    # reports it in the message metadata, which is what the cost estimate below
    # is built on, so it's accurate even on a fully dry run.
    inventory: dict[str, dict] = {}
    for r in rows:
        kind = r.get("media")
        if not kind or kind == "webpage":
            continue
        size = r.get("size_bytes") or 0
        slot = inventory.setdefault(
            kind, {"files": 0, "in_chat_mb": 0.0, "within_cap_files": 0, "within_cap_mb": 0.0, "duration_sec": 0.0}
        )
        slot["files"] += 1
        slot["in_chat_mb"] += size / 1024 / 1024
        slot["duration_sec"] += r.get("duration_sec") or 0.0
        if size <= max_bytes:
            slot["within_cap_files"] += 1
            slot["within_cap_mb"] += size / 1024 / 1024
        if r.get("file"):
            by_kind[kind] = by_kind.get(kind, 0) + 1
            downloaded_bytes += size
    for slot in inventory.values():
        slot["in_chat_mb"] = round(slot["in_chat_mb"], 1)
        slot["within_cap_mb"] = round(slot["within_cap_mb"], 1)
        slot["duration_sec"] = round(slot["duration_sec"], 1)

    est_files = sum(inventory.get(k, {}).get("files", 0) for k in transcribe_kind_set)
    est_sec = sum(inventory.get(k, {}).get("duration_sec", 0.0) for k in transcribe_kind_set)
    est_rate = STT_PRICING[transcribe_model]
    transcription_estimate = {
        "model": transcribe_model,
        "kinds": sorted(transcribe_kind_set),
        "files": est_files,
        "minutes": round(est_sec / 60, 2),
        "cost_usd": round(est_sec / 60 * est_rate, 4),
        "note": "по метаданным Telegram, без скачивания — free preview; вызовите с transcribe=True, чтобы реально расшифровать",
    }
    transcription_result = (
        {
            "enabled": True,
            "model": transcribe_model,
            "files_transcribed": transcribed_now,
            "files_failed": transcribe_failed,
            "duration_sec": round(transcribed_sec, 1),
            "cost_usd": round(transcribed_sec / 60 * est_rate, 4),
        }
        if transcribe else {"enabled": False}
    )

    still_pending = [r["id"] for r in rows if r.get("media") not in (None, "webpage")
                     and not r.get("file") and not r.get("skipped_reason")
                     and (not wanted_kinds or r.get("media") in wanted_kinds)]

    manifest = {
        "version": 1,
        "chat": chat_name,
        "chat_id": entity.id,
        "slug": _slugify(chat_name),
        "requested": {"start_ts": meta["start_ts"], "end_ts": meta["end_ts"]},
        "covered": {
            "start_ts": rows[-1]["ts"] if rows else None,
            "end_ts": rows[0]["ts"] if rows else None,
            "start": rows[-1]["date_utc"] if rows else None,
            "end": rows[0]["date_utc"] if rows else None,
        },
        "messages": len(rows),
        "oldest_id": rows[-1]["id"] if rows else None,
        "newest_id": rows[0]["id"] if rows else None,
        "attachments": {
            "enabled": download_attachments,
            "kinds": sorted(wanted_kinds) or "all",
            "max_mb": download_attachments_max_mb,
            "downloaded": downloaded,
            "pending": len(still_pending),
        },
        "transcription": {"estimate": transcription_estimate, "result": transcription_result},
        "updated_ts": int(datetime.now(timezone.utc).timestamp()),
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "chat": chat_name,
        "chat_id": entity.id,
        "out_dir": str(target),
        "files": written,
        "messages": len(rows),
        "range_utc": f"{meta['start']} — {meta['end']}",
        "range_ts": [meta["start_ts"], meta["end_ts"]],
        "oldest_id": rows[-1]["id"] if rows else None,
        "newest_id": rows[0]["id"] if rows else None,
        "messages_reused": reused,
        "messages_fetched_now": fetched_now,
        "attachments_downloaded": downloaded,
        "attachments_by_kind": by_kind,
        "attachments_mb": round(downloaded_bytes / 1024 / 1024, 1),
        "attachments_inventory": inventory,
        "attachments_pending": len(still_pending),
        "attachments_skipped": meta["skipped"],
        "transcription_estimate": transcription_estimate,
        "transcription_result": transcription_result,
    }


#
# ------------------------------------------------------------- archive ------
#
# Сервис tg-archive (docker compose, claude-workspace/tg-archive) постоянно
# держит на диске полные дампы избранных чатов. Эти два тула — мост к нему.


def _archive_url() -> str:
    return (os.getenv("TELEGRAM_ARCHIVE_URL") or "http://127.0.0.1:8722").rstrip("/")


def _archive_root() -> Path | None:
    raw = os.getenv("TELEGRAM_ARCHIVE_ROOT", "").strip()
    return Path(raw) if raw else None


async def _archive_api(method: str, path: str, payload: dict | None = None,
                       timeout: float = 10.0) -> dict:
    import httpx  # приходит зависимостью openai

    # trust_env=False: системный прокси перехватывает даже localhost-запросы
    async with httpx.AsyncClient(trust_env=False, timeout=timeout) as cli:
        resp = await cli.request(method, _archive_url() + path, json=payload)
        return resp.json()


def _archive_dirs() -> list[dict]:
    root = _archive_root()
    if not root or not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        man = _read_manifest(d) if d.is_dir() else None
        if man:
            out.append(
                {
                    "dir": str(d),
                    "dump_md": str(d / "dump.md"),
                    "dump_json": str(d / "dump.json"),
                    "chat": man.get("chat"),
                    "chat_id": man.get("chat_id"),
                    "messages": man.get("messages"),
                    "covered_utc": [
                        (man.get("covered") or {}).get("start"),
                        (man.get("covered") or {}).get("end"),
                    ],
                    "backfill_done": (man.get("archive") or {}).get("backfill_done"),
                    "updated_ts": man.get("updated_ts"),
                }
            )
    return out


@mcp.tool()
async def archive_status() -> dict:
    """List the chats we ALWAYS keep archived on disk, and how fresh each dump is.

    Selected frequently-used chats (see the server instructions for the current
    list) are continuously synced by the tg-archive service: their FULL history
    is always on disk — messages, media up to 10 MB (video notes as audio
    track), and voice/video-note transcripts (marked [speech] in dump.md).

    So when the user asks anything in the context of such a chat — «посмотри
    чат с N и ответь…», «что N писал про X» — the chat is ALREADY
    exported: do NOT page read_messages and do NOT start a new dump. Call
    archive_refresh(chat) once (seconds), then Read/Grep the dump.md /
    dump.json paths it returns.
    """
    result: dict = {"archived_chats": _archive_dirs()}
    try:
        result["service"] = await _archive_api("GET", "/status")
        result["service_reachable"] = True
    except Exception as exc:
        result["service_reachable"] = False
        result["service_error"] = (
            f"{exc} — сервис tg-archive не отвечает; дампы на диске всё равно "
            f"читаемы, но могли устареть. Поднять: docker compose up -d в "
            f"claude-workspace/tg-archive (Docker Desktop должен быть запущен)."
        )
    return result


@mcp.tool()
async def archive_refresh(chat: str, timeout_sec: float = 120) -> dict:
    """Bring an archived chat's on-disk dump up to date, right now.

    Call this BEFORE answering any request in the context of an archived chat
    (see archive_status for the list): it fetches the newest messages and
    fresh media within seconds, then you Read/Grep the dump files directly.

    Args:
        chat: chat id, @username, or part of the chat title.
        timeout_sec: how long to wait; on timeout the sync continues in the
            background and the dump on disk simply catches up a bit later.
    """
    try:
        result = await _archive_api(
            "POST", "/refresh",
            {"chat": chat, "timeout_sec": timeout_sec},
            timeout=timeout_sec + 15,
        )
    except Exception as exc:
        listing = _archive_dirs()
        needle = chat.strip().casefold()
        match = [
            e for e in listing
            if needle in (e["chat"] or "").casefold() or needle == str(e["chat_id"])
        ]
        return {
            "service_reachable": False,
            "error": f"tg-archive не отвечает: {exc}",
            "hint": "docker compose up -d в claude-workspace/tg-archive",
            "stale_dump_on_disk": match or listing,
        }
    cid = result.get("chat_id")
    if cid:
        for e in _archive_dirs():
            if e["chat_id"] == cid:
                result["files"] = {"md": e["dump_md"], "json": e["dump_json"], "dir": e["dir"]}
                break
    return result


#
# ------------------------------------------------------------- duties -------
#
# Дежурства по чатам: слушатель tools/watch_chat.py (отдельный процесс Windows) + агент — сессия Claude под
# тулом Monitor. Ядро — пакет duties/ (stdlib + psutil): реестр, статус, тревоги, старт/стоп, команда Monitor.
# Импорт ЛЕНИВЫЙ внутри каждого тула: если ядро сломано, остальные инструменты сервера работают как раньше,
# а эти отвечают {"ok": false, "error": ...}. Дашборд — http://127.0.0.1:18210/ (duty_dashboard).
# Подробно — README.md, раздел «Дежурства: дашборд и MCP».


async def _duty_call(fn_name: str, *args, **kwargs) -> dict:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from duties import api as _duty_api
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ядро дежурств не загрузилось: {type(exc).__name__}: {exc}"}
    try:
        return await asyncio.to_thread(getattr(_duty_api, fn_name), *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{fn_name}: {type(exc).__name__}: {exc}"}


@mcp.tool()
async def duty_list() -> dict:
    """Дежурства по чатам — сводка по всем, без сети (только процессы и файлы).

    Возвращает {summary, duties: [{name, title, level (green/yellow/red/grey), listener, heartbeat, connection,
    agent, incoming_today, pending, alerts}], unregistered (живые слушатели не из реестра), global_alerts, orphans}.
    """
    return await _duty_call("list_duties")


@mcp.tool()
async def duty_status(name: str) -> dict:
    """Полный статус одного дежурства: слушатель (PID, аптайм, устарел ли код), пульс ALIVE, связь с Telegram,
    агент (какая сессия держит Monitor, ссылка на неё), входящие, «ожидает ответа», тревоги, пути файлов.
    Тексты сообщений не возвращаются."""
    return await _duty_call("duty_status", name)


@mcp.tool()
async def duty_start(name: str) -> dict:
    """Запустить слушатель дежурства отдельным скрытым процессом Windows (переживает сессию и лимит 2 ч).
    Второй слушатель на тот же state не запускает. Возвращает {ok, pid, command, stdout} или {ok: false, error}."""
    return await _duty_call("start", name)


@mcp.tool()
async def duty_stop(name: str) -> dict:
    """Остановить слушатель дежурства (только процессы, подтверждённо являющиеся его watch_chat.py с этим state).
    Файлы state/log/stdout не трогаются. Возвращает {ok, stopped: [pid], failed}."""
    return await _duty_call("stop", name)


@mcp.tool()
async def duty_restart(name: str) -> dict:
    """Перезапустить слушатель дежурства (стоп + старт; пропущенное догонится по state). Возвращает {ok, stop, start}."""
    return await _duty_call("restart", name)


@mcp.tool()
async def duty_add(name: str, chat: str, workspace: str = "", title: str = "", start: bool = False,
                   quiet: int = 0, trigger: str = "/ai", sign: bool = True, notes: str = "") -> dict:
    """Завести новое дежурство: папка воркспейса + notes/ + README.md из шаблона правил (если README нет),
    запись в реестр, по желанию — старт слушателя.

    Args:
        name: короткое имя латиницей (amir, olga-shop) — для реестра и файлов notes/<name>_*.
        chat: id чата (супергруппы как -100…), @username или подстрока названия; несколько — через запятую.
        workspace: папка воркспейса (пусто — claude-workspace/<name>; относительный путь — от claude-workspace).
        title: заголовок карточки (пусто — название чата).
        start: сразу запустить слушатель.
        quiet: секунд тишины перед выдачей пачки (0 — сразу), trigger: префикс вызова владельцем (/ai),
        sign: ответы с подписью «Claude:» (влияет на шаблон README), notes: заметка в реестр.

    Возвращает {ok, workspace, readme, paths, monitor_command: {command, description, timeout_ms}, reminder}.
    Monitor запускает сама сессия агента — сервер этого сделать не может."""
    return await _duty_call("add", name=name, chat=chat, workspace=workspace, title=title, start_now=start,
                            quiet=quiet, trigger=trigger, sign=sign, notes=notes)


@mcp.tool()
async def duty_remove(name: str, stop_listener: bool = False) -> dict:
    """Убрать дежурство из реестра (файлы воркспейса не удаляются). Если слушатель работает — нужен
    stop_listener=True (иначе ошибка). Возвращает {ok, removed, stopped}."""
    return await _duty_call("remove", name, stop_listener)


@mcp.tool()
async def duty_attach(name: str, session_id: str = "", session_title: str = "") -> dict:
    """Агент сообщает, что на дежурстве именно он — вызвать один раз после запуска Monitor.

    session_id можно не передавать: сервер возьмёт сессию, из которой пришёл вызов. Дашборд покажет её
    название и ссылку «Открыть сессию». Возвращает {ok, agent: {session_id, title, link}, monitor_command}."""
    return await _duty_call("attach", name, session_id, session_title)


@mcp.tool()
async def duty_monitor_command(name: str) -> dict:
    """Готовая команда для тула Monitor (persistent): стартовый статус + tail stdout слушателя с фильтром
    watch_chat.py --print-filter; при перевзводе догоняет пачки, пришедшие в паузе; tail умирает вместе с Monitor.
    name — имя или несколько через запятую (один Monitor на несколько дежурств).
    Возвращает {command, description, timeout_ms: 1800000, rearm, attach}."""
    return await _duty_call("monitor_command", name)


@mcp.tool()
async def duty_tail(name: str, lines: int = 30, show_text: bool = False) -> dict:
    """Последние строки stdout слушателя (до 500). Тексты сообщений (строки NEW) по умолчанию скрыты —
    только id/время/тип; show_text=True — показать. Возвращает {lines, masked, file, updated}."""
    return await _duty_call("tail", name, lines, show_text)


@mcp.tool()
async def duty_handled(name: str, up_to_id: int = 0, chat_id: int = 0, note: str = "") -> dict:
    """Пометить входящие дежурства до up_to_id (включительно) обработанными без ответа — чтобы не висели в
    «ожидает ответа» (личное владельцу, «спасибо»). up_to_id=0 — всё, что пришло на сейчас; chat_id=0 — все чаты.
    Возвращает {ok, up_to_id, journal}."""
    return await _duty_call("handled", name, up_to_id, chat_id or None, note)


@mcp.tool()
async def duty_dashboard() -> dict:
    """Поднять веб-дашборд дежурств (скрытый отдельный процесс, singleton), если не запущен.
    Возвращает {ok, url: http://127.0.0.1:18210/, started}."""
    return await _duty_call("dashboard")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    mcp.run()
