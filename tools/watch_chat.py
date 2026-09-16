#!/usr/bin/env python3
"""Дежурство по чату: событийный слушатель Telegram для сессии Claude (Monitor).

Зачем. MCP-ручки отвечают только когда их дёрнут; чтобы Claude в открытой сессии Claude Desktop / Claude Code
*просыпался сам* на новые сообщения в выбранных чатах, нужен внешний процесс, который печатает событие в stdout.
Этот скрипт — такой процесс: его запускают под тулом Monitor (persistent), каждая строка stdout становится
уведомлением в чате с Claude, Claude читает сообщения, делает работу и отвечает через `send_message`.

Как работает.
- Telethon на КОПИИ сессии MCP (оригинал занят сервером), та же цепочка прокси, что у сервера
  (TELEGRAM_PROXY_CHAIN из .env).
- Слушает один или несколько чатов (--chat: id, @username или подстрока названия диалога, можно повторять).
- Только входящие; сообщения ботов пропускает (--include-bots, чтобы не пропускать).
- Новые сообщения копятся и отдаются ПАЧКОЙ после --quiet секунд тишины (по умолчанию 180): одна серия
  сообщений собеседника = одно пробуждение Claude, а не пять.
- При старте догоняет пропущенное с last_seen_id из --state (тоже одной пачкой), после эмиссии обновляет
  last_seen_id — перезапуск не дублирует. Всё эмитированное дописывается в --log (jsonl).
- Сторож соединения раз в 30 с: Telethon переподключается сам, но молча, а молчание слушателя неотличимо от
  «сообщений нет» — поэтому обрыв и восстановление печатаются явно, и после восстановления делается догон
  (события, пришедшие без связи, до процесса не доходят).

Формат stdout (для фильтра Monitor `^(BATCH|NEW |READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)`):
    READY connected via direct at 12:43:28
    READY chat resolved: Мария ТАЛЬХАРПА (id 888793799)
    LISTENING 2 chat(s), пачки после 180 с тишины
    BATCH 2 новых сообщений в «Мария ТАЛЬХАРПА» (ids 8709450–8709451), обработать вместе
    NEW {"chat_id": 888793799, "chat": "Мария ТАЛЬХАРПА", "id": 8709450, "from": "...", "date": "...", "text": "...", "media": null, "file": null}

Пример (Git Bash, из папки проекта, где лежит state):
    python C:/Users/v/PycharmProjects/claude-workspace/communications-mcp/tools/watch_chat.py \
        --chat 888793799 --chat -1004415463058 --state notes/watch_state.json --log notes/incoming.log

Monitor (persistent) на эту команду с `2>&1 | grep --line-buffered -E "<фильтр выше>"`. Monitor живёт только
пока открыта сессия Claude — при новой сессии перезапустить.
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

FILTER = r"^(BATCH|NEW |READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)"


# ---------- сессия и прокси (как в server.py, без импорта сервера) ----------

def session_src(explicit: str | None) -> Path:
    raw = (explicit or os.getenv("TELEGRAM_SESSION", "")).strip()
    if raw:
        p = Path(raw).expanduser()
    else:
        desktop = ROOT / "sessions" / "desktop.session"
        p = desktop if desktop.exists() else ROOT / "sessions" / "mcp.session"
    if p.suffix != ".session":
        p = p.with_suffix(".session")
    p = p if p.is_absolute() else ROOT / p
    if not p.exists():
        raise RuntimeError(f"session file not found: {p}")
    return p


def proxy_chain() -> list[str]:
    raw = os.getenv("TELEGRAM_PROXY_CHAIN", "").strip()
    if raw:
        chain = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        p = os.getenv("TELEGRAM_PROXY", "").strip()
        chain = ([p] if p else []) + ["direct"]
    out, seen = [], set()
    for x in chain:
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out or ["direct"]


def parse_proxy(raw: str):
    raw = (raw or "").strip()
    if not raw or raw.lower() == "direct":
        return None
    import socks
    u = urlparse(raw)
    ptype = {"socks5": socks.SOCKS5, "socks5h": socks.SOCKS5, "socks4": socks.SOCKS4, "socks4a": socks.SOCKS4,
             "http": socks.HTTP, "https": socks.HTTP}[(u.scheme or "socks5").lower()]
    return (ptype, u.hostname or "127.0.0.1", u.port or 1080)


async def connect(session: str | None, tag: str) -> TelegramClient:
    tmp = Path(os.environ.get("TEMP", ".")) / f"tg_copy_watch_{tag}.session"
    shutil.copy(session_src(session), tmp)
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    errors = []
    for technique in proxy_chain():
        kwargs = {"timeout": 60}
        pr = parse_proxy(technique)
        if pr:
            kwargs["proxy"] = pr
        client = TelegramClient(str(tmp.with_suffix("")), api_id, api_hash, **kwargs)
        try:
            await client.connect()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{technique}: {type(e).__name__}: {e}")
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            continue
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("session not authorized — перелогиньтесь (relogin_desktop.py)")
        print(f"READY connected via {technique} at {datetime.now():%H:%M:%S}", flush=True)
        return client
    raise RuntimeError("unreachable: " + " | ".join(errors))


# ---------- состояние ----------

class State:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.data = {}
        self.data.setdefault("chats", {})

    def chat(self, chat_id: int) -> dict:
        return self.data["chats"].setdefault(str(chat_id), {})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- основное ----------

class Watcher:
    def __init__(self, a):
        self.a = a
        self.state = State(Path(a.state))
        self.log = Path(a.log) if a.log else None
        self.pending: dict[int, list] = {}
        self.timers: dict[int, asyncio.Task] = {}
        self.titles: dict[int, str] = {}
        self.client = None
        self.entities: list = []
        self.ids: list[int] = []

    async def resolve(self, client, spec: str):
        spec = spec.strip()
        if spec.lstrip("-").isdigit():
            ent = await client.get_entity(int(spec))
        elif spec.startswith("@"):
            ent = await client.get_entity(spec)
        else:
            ent = None
            async for d in client.iter_dialogs():
                if spec.lower() in (d.name or "").lower():
                    ent = d.entity
                    break
            if ent is None:
                raise RuntimeError(f"dialog with title containing {spec!r} not found")
        title = getattr(ent, "title", None) or " ".join(
            x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x) or spec
        chat_id = (await client.get_peer_id(ent))
        self.titles[chat_id] = title
        self.state.chat(chat_id)["title"] = title
        self.state.save()
        print(f"READY chat resolved: {title} (id {chat_id})", flush=True)
        return ent, chat_id

    async def wanted(self, m) -> bool:
        if m.out:
            return False
        if not self.a.include_bots:
            try:
                s = await m.get_sender()
            except Exception:  # noqa: BLE001
                s = None
            if s is not None and getattr(s, "bot", False):
                return False
        return True

    async def run(self):
        client = await connect(self.a.session, Path(self.a.state).stem)
        entities, ids = [], []
        for spec in self.a.chat:
            ent, chat_id = await self.resolve(client, spec)
            entities.append(ent)
            ids.append(chat_id)

        self.client, self.entities, self.ids = client, entities, ids
        await self.catchup(reason="старта")

        @client.on(events.NewMessage(chats=entities, incoming=True))
        async def handler(event):
            m = event.message
            if not await self.wanted(m):
                return
            chat_id = event.chat_id
            self.pending.setdefault(chat_id, []).append(m)
            t = self.timers.get(chat_id)
            if t is not None:
                t.cancel()
            self.timers[chat_id] = asyncio.create_task(self.wait_and_flush(chat_id))

        print(f"LISTENING {len(ids)} chat(s), пачки после {self.a.quiet} с тишины", flush=True)
        asyncio.create_task(self.watchdog())
        await client.run_until_disconnected()

    async def catchup(self, reason: str):
        """Добрать пропущенное с last_seen_id по каждому чату — одной пачкой на чат.

        Нужен и при старте, и после разрыва связи: пока соединения нет, события NewMessage
        до процесса не доходят, и полагаться на них одних нельзя.
        """
        for ent, chat_id in zip(self.entities, self.ids):
            last = int(self.state.chat(chat_id).get("last_seen_id", 0))
            missed = []
            try:
                async for m in self.client.iter_messages(ent, min_id=last, reverse=True):
                    if await self.wanted(m):
                        missed.append(m)
            except Exception as e:  # noqa: BLE001
                print(f"ConnectionError: догон по «{self.titles.get(chat_id, chat_id)}» не удался: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            if missed:
                self.flush(chat_id, missed, catchup=True, reason=reason)
            elif not last:  # первый запуск без state: отсчёт с текущего конца истории
                async for m in self.client.iter_messages(ent, limit=1):
                    self.state.chat(chat_id)["last_seen_id"] = m.id
                self.state.save()

    async def watchdog(self):
        """Раз в 30 с проверяет соединение: Telethon переподключается сам, но делает это молча,
        а молчание слушателя неотличимо от «сообщений нет». Печатает обе смены состояния и
        после восстановления связи добирает пропущенное."""
        was_connected = True
        while True:
            await asyncio.sleep(30)
            now = self.client.is_connected()
            if was_connected and not now:
                print(f"ConnectionError: связь потеряна в {datetime.now():%H:%M:%S}, Telethon переподключается",
                      flush=True)
            elif now and not was_connected:
                print(f"READY reconnected at {datetime.now():%H:%M:%S}", flush=True)
                await self.catchup(reason="переподключения")
            was_connected = now

    async def wait_and_flush(self, chat_id: int):
        try:
            await asyncio.sleep(self.a.quiet)
        except asyncio.CancelledError:
            return
        batch = self.pending.pop(chat_id, [])
        if batch:
            self.flush(chat_id, batch)

    def flush(self, chat_id: int, batch: list, catchup: bool = False, reason: str = "старта"):
        ids = [m.id for m in batch]
        title = self.titles.get(chat_id, str(chat_id))
        print(f"BATCH {len(batch)} новых сообщений в «{title}» (ids {min(ids)}–{max(ids)})"
              f"{f' — догон после {reason}' if catchup else ''}, обработать вместе", flush=True)
        for m in batch:
            self.emit(chat_id, title, m)
        st = self.state.chat(chat_id)
        st["last_seen_id"] = max(int(st.get("last_seen_id", 0)), max(ids))
        st["last_emit_at"] = datetime.now().isoformat(timespec="seconds")
        self.state.save()

    def emit(self, chat_id: int, title: str, m):
        s = m.sender
        sender = (getattr(s, "title", None) or " ".join(
            x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x) or None) if s else None
        rec = {"chat_id": chat_id, "chat": title, "id": m.id, "from": sender,
               "date": m.date.astimezone().strftime("%Y-%m-%d %H:%M"),
               "text": (m.message or "")[:2000],
               "media": (type(m.media).__name__ if m.media else None),
               "file": getattr(getattr(m, "file", None), "name", None)}
        line = "NEW " + json.dumps(rec, ensure_ascii=False)
        print(line, flush=True)
        if self.log:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat", action="append", default=[],
                    help="id (в т.ч. -100… для супергрупп), @username или подстрока названия; можно повторять")
    ap.add_argument("--state", help="json с last_seen_id по чатам (создаётся сам)")
    ap.add_argument("--log", help="jsonl-журнал всех эмитированных сообщений")
    ap.add_argument("--quiet", type=int, default=180, help="секунд тишины перед выдачей пачки (180)")
    ap.add_argument("--include-bots", action="store_true", help="не пропускать сообщения ботов")
    ap.add_argument("--session", help="путь к .session (по умолчанию TELEGRAM_SESSION, иначе sessions/desktop.session)")
    ap.add_argument("--print-filter", action="store_true", help="напечатать регулярку для фильтра Monitor и выйти")
    a = ap.parse_args()
    if a.print_filter:
        print(FILTER)
        return
    if not a.chat or not a.state:
        ap.error("нужны --chat (хотя бы один) и --state")
    asyncio.run(Watcher(a).run())


if __name__ == "__main__":
    main()
