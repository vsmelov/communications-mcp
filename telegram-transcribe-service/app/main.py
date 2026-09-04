import asyncio
import logging

import uvicorn
from telethon import TelegramClient

from . import config, worker
from .web import app as web_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def make_client(technique: str) -> TelegramClient:
    return TelegramClient(
        config.SESSION_PATH,
        config.API_ID,
        config.API_HASH,
        proxy=config.parse_proxy(technique),
        # Patience over speed: on a long flood-wait, sit and wait it out
        # instead of raising and having the caller move on to hammer the
        # next request. See app/config.py for why.
        flood_sleep_threshold=config.FLOOD_SLEEP_THRESHOLD_SECONDS,
    )


async def connect_with_fallback() -> TelegramClient:
    """Connect trying each technique of config.get_proxy_chain() in order.

    If none works (network/proxy down, VPN switched modes) do NOT crash the
    container into a restart loop: wait with exponential backoff and retry —
    the VPN will come back and we reconnect on our own. The only fatal branch
    is an unauthorized session: only a human can fix that, retrying is useless.
    """
    backoff = 5.0
    rnd = 0
    while True:
        rnd += 1
        for technique in config.get_proxy_chain():
            label = "direct" if technique.strip().lower() in ("", "direct") else technique
            client = make_client(technique)
            try:
                await client.connect()
                authorized = await client.is_user_authorized()
            except Exception as exc:  # noqa: BLE001
                log.warning("connect via %s failed: %s", label, str(exc).splitlines()[0][:120])
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
            if not authorized:
                raise RuntimeError(
                    f"Session at {config.SESSION_PATH}.session is not authorized. "
                    "Copy a valid, already-logged-in .session file to data/session.session before starting."
                )
            log.info("connected via %s", label)
            return client
        log.warning("every connection technique failed (round %d) — sleeping %.0fs before retry", rnd, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


async def main():
    client = await connect_with_fallback()
    me = await client.get_me()
    log.info("connected as %s (id=%s)", getattr(me, "username", None) or me.first_name, me.id)

    uv_config = uvicorn.Config(web_app, host="0.0.0.0", port=config.PORT, log_level="warning")
    server = uvicorn.Server(uv_config)

    await asyncio.gather(
        worker.run(client),
        server.serve(),
        client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
