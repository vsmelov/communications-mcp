"""HTTP API архиватора.

GET  /                  -> HTML-дашборд мониторинга (архив, очереди, траты STT)
GET  /health            -> {"ok": true}
GET  /status            -> состояние сервиса и всех чатов
GET  /ledger?limit=25   -> последние записи журнала транскрибации (траты USD)
POST /refresh           -> {"chat": "<id|@username|подстрока названия>",
                            "timeout_sec": 120}
    Ставит чат в начало очереди, ждёт завершения синка и возвращает сводку.
    Вызывается перед ответом на вопросы в контексте чата — гарантирует
    актуальность дампа на диске.
"""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

_UI_HTML = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


def make_app(archiver) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_req: web.Request) -> web.Response:
        return web.Response(text=_UI_HTML, content_type="text/html")

    @routes.get("/health")
    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    @routes.get("/ledger")
    async def ledger(req: web.Request) -> web.Response:
        try:
            limit = max(1, min(500, int(req.query.get("limit", 25))))
        except ValueError:
            limit = 25
        entries = archiver.ledger.tail(limit)
        names = {s.store.chat_id: s.store.chat_name for s in archiver.syncers if s.store}
        for e in entries:
            e["chat"] = names.get(e.get("chat_id"))
        return web.json_response(entries)

    @routes.get("/status")
    async def status(_req: web.Request) -> web.Response:
        return web.json_response(archiver.status())

    @routes.post("/refresh")
    async def refresh(req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            body = {}
        chat = str(body.get("chat", "")).strip()
        if not chat:
            return web.json_response({"error": "укажите 'chat'"}, status=400)
        timeout = float(body.get("timeout_sec", 120))
        result = await archiver.refresh(chat, timeout)
        code = 404 if "error" in result else (200 if result.get("done") else 202)
        return web.json_response(result, status=code)  # sync_error приходит как 202

    app = web.Application()
    app.add_routes(routes)
    return app
