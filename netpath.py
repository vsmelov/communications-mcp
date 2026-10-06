"""Автовыбор сетевого пути до Telegram: прокси VPN-клиента, который сейчас жив, или direct.

Зачем (04.10.2026). VPN-клиенты меняются (Invisible Man → Happ), порты прыгают (10801/10802 → 10808/10809),
режимы TUN/Proxy переключаются — и каждый раз telegram MCP, слушатели и архив молча ломались, пока кто-то не
поправит TELEGRAM_PROXY_CHAIN руками. Особенно больно, что `direct` из РФ не отказывает, а *висит* 30+ с,
и каждый вызов MCP упирался в таймаут.

Что делает `candidates()`:
1. Собирает кандидатов: TELEGRAM_PROXY_CHAIN/TELEGRAM_PROXY из .env, системный прокси Windows из реестра
   (HKCU Internet Settings — его выставляет Happ/v2rayN/Clash), и локальные порты из COMMON_PORTS,
   которые сейчас слушаются (socks5 и http на каждом), плюс direct.
2. Параллельно (потоки, таймаут PROBE_TIMEOUT) проверяет TCP до дата-центра Telegram через каждого.
3. Возвращает рабочие, отсортированные по скорости; если не сработал никто — всё подряд (на удачу).
Победитель кэшируется в `.netpath.json` на CACHE_TTL, чтобы не сканировать на каждый вызов; при неудачном
подключении вызывающий зовёт `candidates(force=True)`.

CLI: `python netpath.py` — напечатать, что найдено и сколько миллисекунд.
"""
from __future__ import annotations

import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".netpath.json"
CACHE_TTL = 600
PROBE_TIMEOUT = 4.0
TG_TARGETS = [("149.154.167.51", 443)]  # DC2
# типовые локальные порты VPN-клиентов: xray/v2rayN/Happ, Invisible Man, Clash, sing-box, NekoRay
COMMON_PORTS = [10808, 10809, 10801, 10802, 10800, 1080, 1081, 7890, 7891, 7897, 2080, 2081, 20170, 20171, 8080]


def _norm(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.lower() == "direct":
        return "direct"
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def _system_proxy() -> list[str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enabled = winreg.QueryValueEx(k, "ProxyEnable")[0]
            server = winreg.QueryValueEx(k, "ProxyServer")[0]
    except Exception:  # noqa: BLE001 — не Windows / ключа нет
        return []
    if not enabled or not server:
        return []
    out = []
    for part in str(server).split(";"):
        if "=" in part:  # формат "http=host:port;socks=host:port"
            kind, addr = part.split("=", 1)
            out.append(("socks5://" if kind.lower().startswith("socks") else "http://") + addr)
        else:
            out.append(_norm(part))
    return out


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _gather() -> list[str]:
    raw = [x for x in os.getenv("TELEGRAM_PROXY_CHAIN", "").split(",") if x.strip()]
    if os.getenv("TELEGRAM_PROXY", "").strip():
        raw.append(os.getenv("TELEGRAM_PROXY", ""))
    raw += _system_proxy()
    for p in COMMON_PORTS:
        if _port_open(p):
            raw += [f"socks5://127.0.0.1:{p}", f"http://127.0.0.1:{p}"]
    raw.append("direct")
    out, seen = [], set()
    for x in map(_norm, raw):
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _probe(technique: str) -> float | None:
    """Время TCP-соединения с Telegram через technique (сек) или None."""
    t0 = time.perf_counter()
    for host, port in TG_TARGETS:
        try:
            if technique == "direct":
                s = socket.create_connection((host, port), timeout=PROBE_TIMEOUT)
            else:
                import socks
                u = urlparse(technique)
                ptype = socks.SOCKS5 if u.scheme.startswith("socks5") else (
                    socks.SOCKS4 if u.scheme.startswith("socks4") else socks.HTTP)
                s = socks.socksocket()
                s.set_proxy(ptype, u.hostname or "127.0.0.1", u.port or 1080)
                s.settimeout(PROBE_TIMEOUT)
                s.connect((host, port))
            s.close()
            return time.perf_counter() - t0
        except Exception:  # noqa: BLE001
            continue
    return None


def probe_all() -> list[tuple[str, float | None]]:
    cands = _gather()
    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        res = list(ex.map(_probe, cands))
    return list(zip(cands, res))


def candidates(force: bool = False) -> list[str]:
    """Рабочие пути, быстрые первыми (кэш CACHE_TTL секунд)."""
    if not force:
        try:
            c = json.loads(CACHE.read_text(encoding="utf-8"))
            if time.time() - c["ts"] < CACHE_TTL and c["working"]:
                return c["working"]
        except Exception:  # noqa: BLE001
            pass
    results = probe_all()
    working = [t for t, dt in sorted((r for r in results if r[1] is not None), key=lambda r: r[1])]
    try:
        CACHE.write_text(json.dumps({"ts": time.time(), "working": working,
                                     "probe": {t: (round(dt * 1000) if dt is not None else None)
                                               for t, dt in results}}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return working or [t for t, _ in results]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    for t, dt in probe_all():
        print(f"{'OK ' if dt is not None else '-- '} {t:32} {'' if dt is None else f'{dt * 1000:.0f} ms'}")
    print("working:", candidates(force=True))
