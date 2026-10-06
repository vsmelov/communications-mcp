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
- Входящие; сообщения ботов пропускает (--include-bots, чтобы не пропускать). Плюс СВОИ исходящие, если текст
  или подпись к медиа начинается с --trigger (по умолчанию `/ai`) — так владелец аккаунта зовёт Claude прямо
  в чате; в NEW такие помечены "owner_call": true, в BATCH — «вызов владельца /ai».
- Новые сообщения копятся и отдаются ПАЧКОЙ после --quiet секунд тишины (по умолчанию 180): одна серия
  сообщений собеседника = одно пробуждение Claude, а не пять.
- При старте догоняет пропущенное с last_seen_id из --state (тоже одной пачкой), после эмиссии обновляет
  last_seen_id — перезапуск не дублирует. Всё эмитированное дописывается в --log (jsonl).
- Сторож соединения раз в 30 с: Telethon переподключается сам, но молча, а молчание слушателя неотличимо от
  «сообщений нет» — поэтому обрыв и восстановление печатаются явно, и после восстановления делается догон
  (события, пришедшие без связи, до процесса не доходят).
- Сверка раз в WATCH_POLL_SEC (30 с) при живой связи: push-событие может не доехать и без обрыва, поэтому
  история чатов сверяется с last_seen_id; пойманное выдаётся пачкой «догон после сверки». Дубли исключены:
  выданное (id ≤ last_seen_id) и ждущее в пачке повторно не выдаётся. WATCH_POLL_SEC=0 — выключить.

Формат stdout (для фильтра Monitor `^(BATCH|NEW |READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)`):
    READY connected via direct at 12:43:28
    READY chat resolved: Мария ТАЛЬХАРПА (id 888793799)
    LISTENING 2 chat(s), пачки после 180 с тишины
    BATCH 2 новых сообщений в «Мария ТАЛЬХАРПА» (ids 8709450–8709451), обработать вместе
    NEW {"chat_id": 888793799, "chat": "Мария ТАЛЬХАРПА", "id": 8709450, "from": "...", "date": "...", "text": "...", "media": null, "file": null}

Пример (Git Bash, из папки проекта, где лежит state):
    python C:/Users/v/PycharmProjects/claude-workspace/communications-mcp/tools/watch_chat.py \
        --chat 888793799 --chat -1004415463058 --state notes/watch_state.json --log notes/incoming.log

ОТВЕТЫ. По умолчанию каждый ответ Claude в чате подписан премиум-эмодзи Claude + «**Claude:**» — слать через
`tools/send_reply.py --chat <id> --text-file <файл> [--reply-to <msg_id>]` (MCP send_message кастомные эмодзи
не умеет). Id эмодзи — `send_reply.CLAUDE_EMOJI_ID` (6273917097319865433). Голосовые/кружки — бесплатно через
Telegram Premium: `tools/voice_text.py --chat <id> --id <msg_id>`.

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
# Telethon при обрыве сети пишет WARNING на каждую попытку («Server closed the connection», «Attempt N at
# connecting failed») — под Monitor каждая строка становится уведомлением. Обрыв и восстановление и так
# печатает сторож ниже (по одной строке), поэтому от Telethon оставляем только ERROR и выше.
import logging
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.ERROR)
ROOT = Path(__file__).resolve().parents[1]
CONNECTED_VIA = "?"
# пульс в stdout раз в N секунд: строка ALIVE НЕ проходит фильтр Monitor (не будит Claude), но видна в логе —
# чтобы по `tail notes/*_watch_stdout.log` было понятно, жив ли слушатель, а не гадать по тишине (04.10.2026)
HEARTBEAT_SEC = int(os.getenv("WATCH_HEARTBEAT_SEC", "600"))
# сверка с историей раз в N секунд при живой связи: push-событие Telegram может не доехать и без обрыва
# (07.10.2026 сообщение Инны 8720409 не пришло событием, слушатель молчал) — сверка добирает такое за ≤N с
POLL_SEC = int(os.getenv("WATCH_POLL_SEC", "30"))
load_dotenv(ROOT / ".env")

# В фильтр Monitor НЕ включаем строки NEW: каждая из них — отдельное уведомление, и на длинной пачке
# (догон, болтливый чат) Monitor глушит слушателя за флуд — 25.09.2026 так снесло оба дежурства.
# Приходит только BATCH (сколько сообщений и в каком чате), тексты читаются из --log и из дампа tg-archive.
FILTER = r"^(BATCH|READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)"


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


def proxy_chain(force: bool = False) -> list[str]:
    # с 04.10.2026 — автовыбор живого пути (netpath.py: .env + системный прокси + порты VPN + direct)
    if os.getenv("NETPATH_AUTO", "1") != "0":
        sys.path.insert(0, str(ROOT))
        import netpath
        return netpath.candidates(force=force)
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
    tried = []
    for technique in (t for force in (False, True) for t in proxy_chain(force)):  # пересканинг только если кэш не сработал
        if technique in tried:
            continue
        tried.append(technique)
        # при обрыве сети Telethon по умолчанию сдаётся за 5 попыток × 1 с; держим соединение дольше
        kwargs = {"timeout": 60, "connection_retries": 20, "retry_delay": 15, "auto_reconnect": True}
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
        global CONNECTED_VIA
        CONNECTED_VIA = technique
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
        self.catch_lock = asyncio.Lock()

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

    def owner_call(self, m) -> bool:
        """Исходящее сообщение владельца аккаунта, текст/подпись которого начинается с триггера (/ai) —
        так Владимир сам зовёт Claude прямо в чате (04.10.2026). Ответы Claude начинаются с «🤖 Claude:»,
        поэтому сами себя не вызывают."""
        if not m.out or not self.a.trigger:
            return False
        return (m.message or "").lstrip().lower().startswith(self.a.trigger.lower())

    async def wanted(self, m) -> bool:
        if m.out:
            return self.owner_call(m)
        if not self.a.include_bots:
            try:
                s = await m.get_sender()
            except Exception:  # noqa: BLE001
                s = None
            if s is not None and getattr(s, "bot", False):
                return False
        return True

    async def run(self):
        """Живёт вечно: при обрыве, который Telethon не пережил, пересоздаёт клиент с паузой и догоняет пропущенное
        (20.09.2026: ночной обрыв сети убивал процесс через ~5 мин ретраев)."""
        delay = 60
        while True:
            try:
                # конфиг мог поменяться, пока мы висели в ретраях: 03.10.2026 процесс четверо суток
                # долбился в мёртвый прокси, хотя в .env уже была живая цепочка. Перечитываем каждый раз.
                load_dotenv(ROOT / ".env", override=True)
                await self.run_once()
                print("ConnectionError: Telethon отключился штатно — переподключаюсь через 60 с", flush=True)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                print(f"ConnectionError: {type(e).__name__}: {str(e)[:120]} — переподключаюсь через {delay} с", flush=True)
            except Exception as e:  # noqa: BLE001 — неизвестное тоже не должно убивать дежурство
                print(f"Error: {type(e).__name__}: {str(e)[:160]} — переподключаюсь через {delay} с", flush=True)
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self.client = None
            for t in self.timers.values():
                t.cancel()
            self.timers.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600)

    async def run_once(self):
        client = await connect(self.a.session, Path(self.a.state).stem)
        entities, ids = [], []
        for spec in self.a.chat:
            ent, chat_id = await self.resolve(client, spec)
            entities.append(ent)
            ids.append(chat_id)

        self.client, self.entities, self.ids = client, entities, ids
        await self.catchup(reason="старта")

        @client.on(events.NewMessage(chats=entities))  # и исходящие: owner_call() пропускает только /ai
        async def handler(event):
            m = event.message
            chat_id = event.chat_id
            if self.seen(chat_id, m.id) or not await self.wanted(m):
                return  # уже выдано сверкой/догоном или ждёт в пачке
            if self.seen(chat_id, m.id):
                return  # сверка успела выдать, пока wanted() ходил за отправителем
            asyncio.create_task(self.ack(m))
            self.pending.setdefault(chat_id, []).append(m)
            t = self.timers.get(chat_id)
            if t is not None:
                t.cancel()
            self.timers[chat_id] = asyncio.create_task(self.wait_and_flush(chat_id))

        print(f"LISTENING {len(ids)} chat(s), пачки после {self.a.quiet} с тишины; "
              f"отвечать через tools/send_reply.py (подпись эмодзи Claude по умолчанию), "
              f"голосовые — tools/voice_text.py", flush=True)
        wd = asyncio.create_task(self.watchdog())
        pl = asyncio.create_task(self.poller()) if POLL_SEC > 0 else None
        try:
            await client.run_until_disconnected()
        finally:
            wd.cancel()
            if pl is not None:
                pl.cancel()

    async def ack(self, m):
        """Мгновенная реакция (по умолчанию 👀) на сообщение, ушедшее Claude: собеседник сразу видит, что его
        услышали, пока Claude думает над ответом (04.10.2026). Ошибка реакции дежурство не роняет."""
        if not self.a.ack:
            return
        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji
            await self.client(SendReactionRequest(peer=await m.get_input_chat(), msg_id=m.id,
                                                  reaction=[ReactionEmoji(emoticon=self.a.ack)]))
        except Exception as e:  # noqa: BLE001
            print(f"ACK failed for {m.id}: {type(e).__name__}: {str(e)[:100]}", flush=True)

    async def catchup(self, reason: str):
        """Добрать пропущенное с last_seen_id по каждому чату — одной пачкой на чат.

        Нужен и при старте, и после разрыва связи, и периодически (сверка): пока соединения нет, события
        NewMessage до процесса не доходят, а иногда не доходят и при живой связи.
        """
        async with self.catch_lock:  # сверка и догон после переподключения не должны идти одновременно
            await self._catchup(reason)

    async def _catchup(self, reason: str):
        for ent, chat_id in zip(self.entities, self.ids):
            last = int(self.state.chat(chat_id).get("last_seen_id", 0))
            if not last:
                # первый запуск без state: отсчёт с текущего конца истории. Без этого min_id=0
                # означает «всю историю» и первая же пачка выдавала сотни старых сообщений
                # (25.09.2026, чат Инны: 570 штук).
                async for m in self.client.iter_messages(ent, limit=1):
                    self.state.chat(chat_id)["last_seen_id"] = m.id
                self.state.save()
                print(f"READY «{self.titles.get(chat_id, chat_id)}»: первый запуск, "
                      f"старт с id {self.state.chat(chat_id).get('last_seen_id', 0)}", flush=True)
                continue
            missed = []
            try:
                async for m in self.client.iter_messages(ent, min_id=last, reverse=True):
                    if not self.seen(chat_id, m.id) and await self.wanted(m):
                        missed.append(m)
            except Exception as e:  # noqa: BLE001
                print(f"ConnectionError: догон по «{self.titles.get(chat_id, chat_id)}» не удался: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            missed = [m for m in missed if not self.seen(chat_id, m.id)]  # событие могло успеть раньше
            if missed:
                self.flush(chat_id, missed, catchup=True, reason=reason)
                if reason != "старта":  # на старте реакция на старое не нужна
                    for m in missed:
                        asyncio.create_task(self.ack(m))

    def seen(self, chat_id: int, msg_id: int) -> bool:
        """Сообщение уже выдано (id ≤ last_seen_id) или лежит в копящейся пачке — чтобы событие и сверка
        не выдали одно и то же дважды."""
        if msg_id <= int(self.state.chat(chat_id).get("last_seen_id", 0)):
            return True
        return any(p.id == msg_id for p in self.pending.get(chat_id, []))

    async def poller(self):
        """Раз в POLL_SEC при живой связи сверяет историю чатов с last_seen_id (один лёгкий запрос на чат):
        страховка от недоставленных push-событий. Пойманное печатается как «догон после сверки»."""
        while self.client is not None:
            await asyncio.sleep(POLL_SEC)
            if self.client is None or not self.client.is_connected():
                continue
            try:
                await self.catchup(reason="сверки")
            except Exception as e:  # noqa: BLE001 — сверка не должна ронять дежурство
                print(f"ConnectionError: сверка не удалась: {type(e).__name__}: {str(e)[:120]}", flush=True)

    async def watchdog(self):
        """Раз в 30 с проверяет соединение: Telethon переподключается сам, но делает это молча,
        а молчание слушателя неотличимо от «сообщений нет». Печатает обе смены состояния и
        после восстановления связи добирает пропущенное."""
        was_connected = True
        last_beat = asyncio.get_running_loop().time()
        while self.client is not None:
            await asyncio.sleep(30)
            if self.client is None:
                return
            now = self.client.is_connected()
            if asyncio.get_running_loop().time() - last_beat >= HEARTBEAT_SEC:
                last_beat = asyncio.get_running_loop().time()
                seen = ", ".join(f"«{self.titles.get(i, i)}» last_seen_id {self.state.chat(i).get('last_seen_id', 0)}"
                                 for i in self.ids)
                print(f"ALIVE {datetime.now():%H:%M:%S} connected={now} via {CONNECTED_VIA}; {seen}", flush=True)
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
        calls = sum(1 for m in batch if self.owner_call(m))
        print(f"BATCH {len(batch)} новых сообщений в «{title}» (ids {min(ids)}–{max(ids)})"
              f"{f', из них {calls} — вызов владельца {self.a.trigger}' if calls else ''}"
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
        if self.owner_call(m):
            # владелец аккаунта позвал Claude в чате: текст после триггера — его задание, ответ — в этот же чат
            rec["owner_call"] = True
            rec["from"] = f"владелец аккаунта ({self.a.trigger})"
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
    # с 04.10.2026 по умолчанию 0: каждое сообщение отдаётся сразу, а докинутое во время ответа Claude получает
    # посреди хода как новое уведомление Monitor и учитывает. Ждать тишину (раньше 180 с) — только тормозило ответ.
    ap.add_argument("--quiet", type=int, default=0, help="секунд тишины перед выдачей пачки (0 = сразу)")
    ap.add_argument("--include-bots", action="store_true", help="не пропускать сообщения ботов")
    ap.add_argument("--ack", default="👀", help="реакция на каждое сообщение, ушедшее Claude ('' — без реакции)")
    ap.add_argument("--trigger", default="/ai",
                    help="свои исходящие с этого префикса (текст или подпись) тоже отдавать Claude — вызов владельцем; "
                         "'' — выключить (по умолчанию /ai)")
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
