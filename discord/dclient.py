"""Тонкий клиент Discord REST (user-токен) — общий для MCP-сервера и скриптов.

Зачем отдельным модулем: главная «ручка» — get_session_info; Claude должен уметь за пару строк написать
свой скрипт для всего, что не покрыто шорткатами (дампы, статистика, обход всех каналов гильдии):

    import sys; sys.path.insert(0, r"<папка discord>")
    from dclient import Discord
    d = Discord()
    for m in d.history(channel_id, limit=5000):   # пагинация внутри, пауза между запросами внутри
        ...

Что берёт на себя клиент: токен из .env, выбор сетевого пути (direct → прокси VPN-клиента), человекоподобные
заголовки веб-клиента, пауза между запросами (DISCORD_MIN_INTERVAL, 0.6 с), 429 → ждём retry_after.
Токен нигде не печатается. Запись (POST/PUT/PATCH/DELETE) без DISCORD_WRITE=1 бросает WriteDisabled.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
load_dotenv(HERE / ".env")
load_dotenv(PARENT / ".env")  # TELEGRAM_PROXY_* для netpath

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://discord.com/api/v9"
DISCORD_EPOCH = 1420070400000

# Заголовки как у веб-клиента Chrome (взяты из реального запроса пользователя; без launch/session id).
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/154.0.0.0 Safari/537.36")
_SUPER = {
    "os": "Windows", "browser": "Chrome", "device": "", "system_locale": "en-US",
    "has_client_mods": False, "browser_user_agent": UA, "browser_version": "154.0.0.0", "os_version": "10",
    "referrer": "", "referring_domain": "", "referrer_current": "", "referring_domain_current": "",
    "release_channel": "stable", "client_build_number": 629408, "client_event_source": None,
}


class WriteDisabled(RuntimeError):
    pass


class DiscordError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"Discord HTTP {status}: {body}")
        self.status, self.body = status, body


def snowflake_to_iso(sf: int | str) -> str:
    ms = (int(sf) >> 22) + DISCORD_EPOCH
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def unix_to_snowflake(ts: float) -> int:
    """Unix-секунды → snowflake (для before/after по дате)."""
    return (int(ts * 1000) - DISCORD_EPOCH) << 22


def write_enabled() -> bool:
    return os.getenv("DISCORD_WRITE", "0").strip() == "1"


def _proxy_candidates() -> list[str]:
    raw = [x.strip() for x in os.getenv("DISCORD_PROXY_CHAIN", "").split(",") if x.strip()]
    if raw:
        return raw
    out = ["direct"]
    try:
        sys.path.insert(0, str(PARENT))
        import netpath  # type: ignore
        # httpx без socksio умеет только http-прокси; socks5 пропускаем
        out += [p for p in netpath._gather() if p.startswith("http://")]
    except Exception:  # noqa: BLE001
        pass
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


class Discord:
    def __init__(self, token: str | None = None, min_interval: float | None = None):
        self.token = (token or os.getenv("DISCORD_TOKEN", "")).strip().strip('"')
        if not self.token:
            raise RuntimeError(f"DISCORD_TOKEN не задан: впиши его в {HERE / '.env'}")
        self.min_interval = float(os.getenv("DISCORD_MIN_INTERVAL", "0.6")) if min_interval is None else min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self.proxy: str | None = None  # выбранный путь ("direct" или URL)

    # ---- сеть -------------------------------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        sp = base64.b64encode(json.dumps(_SUPER, separators=(",", ":")).encode()).decode()
        return {
            "authorization": self.token, "user-agent": UA, "accept": "*/*",
            "accept-language": "en-US,en;q=0.9,ru;q=0.8", "referer": "https://discord.com/channels/@me",
            "x-discord-locale": "en-US", "x-discord-timezone": "Europe/Moscow", "x-super-properties": sp,
            "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
        }

    def _make(self, proxy: str) -> httpx.Client:
        # trust_env=False: системный прокси съедает loopback (см. память httpx-localhost-proxy)
        return httpx.Client(headers=self._headers(), timeout=20, trust_env=False,
                            proxy=None if proxy == "direct" else proxy)

    def _connect(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        errors = []
        for cand in _proxy_candidates():
            c = self._make(cand)
            try:
                r = c.get(f"{API}/users/@me", timeout=8)
            except httpx.HTTPError as e:
                errors.append(f"{cand}: {type(e).__name__}")
                c.close()
                continue
            if r.status_code == 401:
                c.close()
                raise DiscordError(401, "токен недействителен (разлогин/смена пароля) — достань новый")
            self._client, self.proxy = c, cand
            return c
        raise RuntimeError("Discord недоступен ни по одному пути: " + "; ".join(errors))

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                _retries: int = 4) -> Any:
        if method.upper() != "GET" and not write_enabled():
            raise WriteDisabled("запись выключена: поставь DISCORD_WRITE=1 в discord/.env (и перезапусти MCP)")
        for attempt in range(_retries + 1):
            with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self._connect().request(method, API + path, params=params, json=json_body)
            except httpx.HTTPError:
                self._client = None  # путь мог умереть — переподбираем
                if attempt == _retries:
                    raise
                continue
            if r.status_code == 429:
                time.sleep(min(float(r.json().get("retry_after", 2)) + 0.3, 60))
                continue
            if r.status_code == 202:  # индекс поиска ещё строится
                time.sleep(min(float(r.json().get("retry_after", 2)) + 0.3, 30))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except Exception:  # noqa: BLE001
                    body = r.text[:300]
                raise DiscordError(r.status_code, body)
            return r.json() if r.content else None
        raise DiscordError(429, "rate limit не отпускает")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    # ---- удобные методы ---------------------------------------------------------------------------------
    def me(self) -> dict:
        return self.get("/users/@me")

    def guilds(self) -> list[dict]:
        return self.get("/users/@me/guilds", with_counts="true")

    def channels(self, guild_id: str) -> list[dict]:
        return self.get(f"/guilds/{guild_id}/channels")

    def dms(self) -> list[dict]:
        return self.get("/users/@me/channels")

    def messages(self, channel_id: str, limit: int = 50, before: str | None = None,
                 after: str | None = None, around: str | None = None) -> list[dict]:
        return self.get(f"/channels/{channel_id}/messages", limit=min(limit, 100), before=before,
                        after=after, around=around)

    def history(self, channel_id: str, limit: int = 1000, before: str | None = None,
                since: float | None = None) -> Iterator[dict]:
        """Сообщения от новых к старым, пагинация внутри. since — unix-секунды (остановиться, дойдя до даты)."""
        got, cursor = 0, before
        while got < limit:
            page = self.messages(channel_id, 100, before=cursor)
            if not page:
                return
            for m in page:
                if since and (int(m["id"]) >> 22) + DISCORD_EPOCH < since * 1000:
                    return
                yield m
                got += 1
                if got >= limit:
                    return
            cursor = page[-1]["id"]

    def search(self, *, guild_id: str | None = None, channel_id: str | None = None, content: str | None = None,
               author_id: str | None = None, in_channel: str | None = None, has: str | None = None,
               limit: int = 25, offset: int = 0) -> dict:
        base = f"/guilds/{guild_id}" if guild_id else f"/channels/{channel_id}"
        return self.get(f"{base}/messages/search", content=content, author_id=author_id, channel_id=in_channel,
                        has=has, offset=offset or None)  # страница фиксирована (25), limit обрезаем снаружи

    def send(self, channel_id: str, content: str, reply_to: str | None = None) -> dict:
        body: dict[str, Any] = {"content": content}
        if reply_to:
            body["message_reference"] = {"message_id": reply_to}
        return self.request("POST", f"/channels/{channel_id}/messages", json_body=body)


def fmt_message(m: dict) -> str:
    a = m.get("author", {})
    who = a.get("global_name") or a.get("username") or "?"
    parts = [f"[{m['timestamp'][:19]}Z] {who} ({a.get('id')}) #{m['id']}"]
    ref = m.get("referenced_message")
    if ref:
        parts.append(f"↪ ответ {ref.get('author', {}).get('username', '?')}: {(ref.get('content') or '')[:60]!r}")
    text = m.get("content") or ""
    for e in m.get("embeds", []):
        bits = [e.get("title"), e.get("description"), e.get("url")]
        text += "\n  [embed] " + " | ".join(str(b)[:200] for b in bits if b)
    for att in m.get("attachments", []):
        text += f"\n  [file] {att.get('filename')} ({att.get('size', 0) // 1024} KB) {att.get('url')}"
    for st in m.get("sticker_items", []):
        text += f"\n  [sticker] {st.get('name')}"
    rx = m.get("reactions")
    if rx:
        text += "\n  [reactions] " + " ".join(f"{r['emoji'].get('name')}×{r['count']}" for r in rx)
    if m.get("edited_timestamp"):
        text += "  (ред.)"
    return parts[0] + (" " + parts[1] if len(parts) > 1 else "") + "\n" + text.rstrip()
