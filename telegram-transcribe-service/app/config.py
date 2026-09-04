"""Static config for the transcribe service."""
import os
from datetime import timedelta
from pathlib import Path

# Chat id -> how far back to scan on the very first run (one-time backfill).
# Everyone else gets DEFAULT_INITIAL_WINDOW. After the first run each chat is
# driven purely by its saved cursor (last seen message id), never by a window
# again — see chats.cursor_msg_id in db.py.
# Format: {chat_id: timedelta(days=30)} — e.g. a one-time 30d catch-up for a
# chat you care about. Empty by default.
INITIAL_WINDOW_OVERRIDES: dict[int, timedelta] = {}
DEFAULT_INITIAL_WINDOW = timedelta(hours=24)

TRANSCRIBE_TIMEOUT_SECONDS = 120
WORKER_POLL_IDLE_SECONDS = 2
WORKER_PACE_SECONDS = 1  # pause between transcriptions, keeps it "1 at a time, lightly"
MAX_TRANSCRIBE_RETRIES = 3  # auto-retries on timeout before giving up as 'failed'

# Account has 1000+ eligible DM chats — a full sweep is inherently a lot of
# requests. Bias hard toward "wait more than needed" over "risk the account":
#  - never give up and skip ahead on a long flood-wait, just sit and wait it out
#  - add our own pacing between chats on top of Telethon's reactive handling,
#    instead of hammering requests back-to-back between flood-waits
FLOOD_SLEEP_THRESHOLD_SECONDS = 24 * 60 * 60
BACKLOG_SCAN_PACE_SECONDS = 3
RECONCILE_INTERVAL_SECONDS = 60 * 60

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "db.sqlite3"
SESSION_PATH = str(DATA_DIR / "session")  # Telethon appends .session itself

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PORT = int(os.environ.get("PORT", "8077"))


def parse_proxy(raw: str):
    """'direct' / '' -> None, otherwise a Telethon proxy tuple."""
    raw = (raw or "").strip()
    if not raw or raw.lower() == "direct":
        return None
    from urllib.parse import urlparse

    from python_socks import ProxyType

    u = urlparse(raw)
    scheme = (u.scheme or "socks5").lower()
    ptype = {
        "socks5": ProxyType.SOCKS5,
        "socks5h": ProxyType.SOCKS5,
        "socks4": ProxyType.SOCKS4,
        "socks4a": ProxyType.SOCKS4,
        "http": ProxyType.HTTP,
        "https": ProxyType.HTTP,
    }[scheme]
    return (ptype, u.hostname or "127.0.0.1", u.port or 1080)


def get_proxy():
    """First technique of the chain — kept for backwards compatibility."""
    return parse_proxy(get_proxy_chain()[0])


def get_proxy_chain() -> list[str]:
    """Connection techniques to try in order until one works (same as tg-archive).

    TELEGRAM_PROXY_CHAIN (comma-separated: direct, http://..., socks5://...)
    overrides everything. Otherwise: legacy TELEGRAM_PROXY, then sensible
    fallbacks — 'direct' for the VPN's TUN mode (the proxy port is not listening
    there, but the container's own traffic still goes through the tunnel) and
    the classic host proxy for proxy mode. Duplicates removed, order kept.
    """
    raw = os.environ.get("TELEGRAM_PROXY_CHAIN", "").strip()
    if raw:
        chain = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        p = os.environ.get("TELEGRAM_PROXY", "").strip()
        chain = ([p] if p else []) + ["direct", "http://host.docker.internal:10801"]
    seen: set[str] = set()
    out: list[str] = []
    for x in chain:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out or ["direct"]
