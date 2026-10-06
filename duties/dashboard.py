"""Веб-дашборд дежурств: http://127.0.0.1:18210/ (только loopback).

Отдельное лёгкое приложение на stdlib http.server, а не страница в архиваторе tg-archive: архиватор живёт в
Docker, который на этой машине часто виснет, — дашборд дежурств должен работать как раз тогда, когда всё
остальное лежит. На архиватор (http://127.0.0.1:8722/) — ссылка с пометкой, отвечает ли он.

Защита: слушает только 127.0.0.1; заголовок Host — только 127.0.0.1:<порт> / localhost:<порт> (от DNS-rebinding);
для POST — Origin пустой или свой, Content-Type только application/json (от CSRF). Внешних CDN нет, CSP строгий.

Запуск: `python -m duties` (передний план) или `duties/run_dashboard.ps1` (скрытый процесс, singleton).
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, control, registry, status
from .registry import DutyError

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_BODY = 64 * 1024
STATE_TTL = 2.5


class _StateCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.at = 0.0

    def get(self, force: bool = False) -> dict:
        with self.lock:
            if force or self.value is None or time.time() - self.at > STATE_TTL:
                self.value = status.collect()
                self.at = time.time()
            return self.value

    def invalidate(self):
        with self.lock:
            self.value = None


class _Archiver:
    """Проверка tg-archive /health в фоне (раз в 30 с, таймаут 1.5 с) — /api/state никогда не ждёт Docker."""

    def __init__(self):
        self.state = {"ok": None, "checked_at": None, "url": config.archive_url() + "/"}
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while True:
            u = urlparse(config.archive_url())
            ok = False
            try:
                c = HTTPConnection(u.hostname or "127.0.0.1", u.port or 80, timeout=1.5)  # без прокси
                c.request("GET", "/health")
                r = c.getresponse()
                ok = r.status == 200
                c.close()
            except Exception:  # noqa: BLE001
                ok = False
            self.state = {"ok": ok, "checked_at": time.strftime("%H:%M:%S"), "url": config.archive_url() + "/"}
            time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-duties"
    sys_version = ""

    # --- служебное
    def log_message(self, fmt, *args):  # тише: только ошибки и действия
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _allowed_hosts(self) -> set[str]:
        p = self.server.server_address[1]
        return {f"127.0.0.1:{p}", f"localhost:{p}"}

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").strip().lower() in self._allowed_hosts()

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True  # не браузер (curl, скрипт) — Host уже проверен
        return origin in {f"http://{h}" for h in self._allowed_hosts()}  # "null" и чужие сайты — нет

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, text: str):
        self._json(code, {"ok": False, "error": text})

    # --- GET
    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._err(421, "неверный Host — дашборд доступен только по 127.0.0.1/localhost")
        u = urlparse(self.path)
        path = u.path
        try:
            if path in STATIC:
                fname, ctype = STATIC[path]
                body = (config.WEB / fname).read_bytes()
                return self._send(200, body, ctype)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/ping":
                return self._json(200, {"app": config.APP_ID, "version": __version__, "pid": os.getpid(),
                                        "port": self.server.server_address[1]})
            if path == "/api/state":
                st = dict(self.server.cache.get())
                st["archiver"] = self.server.archiver.state
                st["port"] = self.server.server_address[1]
                return self._json(200, st)
            m = re.fullmatch(r"/api/duties/([a-z0-9][a-z0-9_-]{0,39})/(tail|monitor-command)", path)
            if m:
                name, what = m.groups()
                q = parse_qs(u.query)
                if what == "tail":
                    lines = int((q.get("lines") or ["60"])[0] or 60)
                    show = (q.get("text") or ["0"])[0] in ("1", "true", "yes")
                    return self._json(200, control.tail(name, lines, show))
                return self._json(200, control.monitor_command(name))
            return self._err(404, "нет такого адреса")
        except DutyError as e:
            return self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._err(500, f"{type(e).__name__}: {e}")

    do_HEAD = do_GET

    # --- POST
    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._err(421, "неверный Host")
        if not self._origin_ok():
            return self._err(403, "чужой Origin — действие отклонено")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._err(415, "нужен Content-Type: application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return self._err(413, "слишком большое тело запроса")
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            if not isinstance(body, dict):
                raise ValueError("ожидался объект JSON")
        except Exception as e:  # noqa: BLE001
            return self._err(400, f"тело не JSON: {e}")
        path = urlparse(self.path).path
        try:
            if path == "/api/duties":
                res = control.add(
                    name=str(body.get("name") or ""), chat=body.get("chat") or body.get("chats") or "",
                    workspace=str(body.get("workspace") or ""), title=str(body.get("title") or ""),
                    start_now=bool(body.get("start")), quiet=int(body.get("quiet") or 0),
                    trigger=str(body.get("trigger") if body.get("trigger") is not None else "/ai"),
                    sign=bool(body.get("sign", True)), notes=str(body.get("notes") or ""),
                    adopt=bool(body.get("adopt")), state=str(body.get("state") or ""),
                    log=str(body.get("log") or ""), stdout=str(body.get("stdout") or ""))
                self.server.cache.invalidate()
                return self._json(200, res)
            m = re.fullmatch(r"/api/duties/([a-z0-9][a-z0-9_-]{0,39})/(start|stop|restart|open-readme|activate|deactivate)", path)
            if m:
                name, action = m.groups()
                if action == "start":
                    res = control.start(name)
                elif action == "stop":
                    res = control.stop(name)
                elif action == "restart":
                    res = control.restart(name)
                elif action == "open-readme":
                    res = control.open_readme(name)
                else:
                    res = control.set_active(name, action == "activate")
                self.server.cache.invalidate()
                return self._json(200, res)
            return self._err(404, "нет такого действия")
        except DutyError as e:
            return self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._err(500, f"{type(e).__name__}: {e}")

    def do_PUT(self):  # noqa: N802
        return self._err(405, "метод не поддерживается")

    do_DELETE = do_PUT
    do_PATCH = do_PUT


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # на Windows SO_REUSEADDR позволил бы занять чужой порт


def ping(port: int, timeout: float = 1.0) -> dict | None:
    """Ответ /api/ping нашего дашборда на порту или None (без системного прокси — http.client его не знает)."""
    try:
        c = HTTPConnection(config.HOST, port, timeout=timeout)
        c.request("GET", "/api/ping", headers={"Host": f"{config.HOST}:{port}"})
        r = c.getresponse()
        data = json.loads(r.read().decode("utf-8") or "{}")
        c.close()
        return data if data.get("app") == config.APP_ID else None
    except Exception:  # noqa: BLE001
        return None


def _port_busy(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((config.HOST, port)) == 0
    finally:
        s.close()


def _info_path() -> Path:
    return config.runtime_dir() / "dashboard.json"


def serve(port: int = config.DEFAULT_PORT, open_browser: bool = False, verbose: bool = False) -> int:
    """Поднять дашборд на переднем плане. Уже запущен (наш ping отвечает) — сообщить URL и выйти."""
    # дашборд — самостоятельное приложение: действия с него — «dashboard», а не сессия, из которой его запустили
    from .procs import SESSION_ENV
    for k in SESSION_ENV:
        os.environ.pop(k, None)
    os.environ["DUTIES_ACTOR"] = "dashboard"
    for p in (port, config.FALLBACK_PORT) if port == config.DEFAULT_PORT else (port,):
        if ping(p):
            print(f"дашборд уже запущен: http://{config.HOST}:{p}/", flush=True)
            return 0
        if _port_busy(p):
            print(f"порт {p} занят чужим процессом — пробую следующий", flush=True)
            continue
        try:
            httpd = Server((config.HOST, p), Handler)
        except OSError as e:
            print(f"порт {p}: {e}", flush=True)
            continue
        httpd.cache = _StateCache()
        httpd.archiver = _Archiver()
        httpd.verbose = verbose
        url = f"http://{config.HOST}:{p}/"
        try:
            _info_path().write_text(json.dumps({"pid": os.getpid(), "port": p, "url": url,
                                                "started_at": registry.now_iso()}), encoding="utf-8")
        except OSError:
            pass
        print(f"дашборд дежурств: {url} (Ctrl+C — остановить)", flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        try:
            httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0
    print("свободного порта нет (18210, 18211)", flush=True)
    return 1


def ensure_running(wait_sec: float = 10.0) -> dict:
    """Для MCP `duty_dashboard`: если дашборд не отвечает — запустить скрытым отдельным процессом и вернуть URL."""
    for p in (config.DEFAULT_PORT, config.FALLBACK_PORT):
        if ping(p):
            return {"ok": True, "url": f"http://{config.HOST}:{p}/", "started": False}
    from .procs import spawn_detached
    log = config.runtime_dir() / "dashboard.log"
    # python.exe со скрытой консолью (CREATE_NO_WINDOW у cmd.exe наследуется), cwd = communications-mcp
    cmd = f'"{config.default_python()}" -m duties dashboard --port {config.DEFAULT_PORT}'
    sp = spawn_detached(f'cmd.exe /d /s /c "{cmd} >> "{log}" 2>&1"', str(config.ROOT))
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(0.5)
        for p in (config.DEFAULT_PORT, config.FALLBACK_PORT):
            if ping(p):
                res = {"ok": True, "url": f"http://{config.HOST}:{p}/", "started": True,
                       "note": "" if p == config.DEFAULT_PORT else "порт 18210 занят — дашборд на 18211"}
                if sp.get("warning"):
                    res["warning"] = sp["warning"]
                return res
    return {"ok": False, "error": f"дашборд не ответил за {wait_sec:.0f} с — смотри {log}"}
