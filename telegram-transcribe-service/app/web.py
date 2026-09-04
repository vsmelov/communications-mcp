"""Minimal localhost dashboard — no framework beyond FastAPI, no auth
(binds to the docker-compose published port on localhost only)."""
import asyncio
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from .db import db

app = FastAPI()

STATUS_COLOR = {
    "pending": "#8a8a8a",
    "processing": "#c98a1f",
    "done": "#2e9e44",
    "timeout": "#b23b3b",
    "failed": "#7a1f1f",
    "error": "#b23b3b",
}

BASE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>TG transcribe</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 24px; background: #111; color: #eee; }}
  h1 {{ font-size: 18px; }}
  a {{ color: #6cb4ff; }}
  nav {{ margin-bottom: 20px; font-size: 13px; }}
  nav a {{ margin-right: 16px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 32px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #333; vertical-align: top; }}
  th {{ color: #999; font-weight: normal; font-size: 12px; text-transform: uppercase; }}
  tr:hover {{ background: #1a1a1a; }}
  .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 12px; color: #111; font-weight: 600; }}
  .muted {{ color: #888; font-size: 12px; }}
  .text {{ white-space: pre-wrap; max-width: 600px; }}
  .stats {{ display: flex; gap: 28px; flex-wrap: wrap; margin-bottom: 28px; }}
  .stat {{ min-width: 90px; }}
  .stat .n {{ font-size: 24px; font-weight: 700; }}
  .stat .l {{ font-size: 12px; color: #999; text-transform: uppercase; }}
  input[type=text] {{ background: #1a1a1a; border: 1px solid #333; color: #eee; padding: 5px 8px; border-radius: 4px; }}
  button {{ background: #222; border: 1px solid #444; color: #eee; padding: 3px 9px; border-radius: 4px; cursor: pointer; font-size: 12px; }}
  button:hover {{ background: #333; }}
</style>
</head><body>
<nav><a href="/">Overview</a><a href="/chats">Chats ({total_chats})</a></nav>
{body}
</body></html>
"""


def badge(status: str) -> str:
    color = STATUS_COLOR.get(status, "#666")
    return f'<span class="badge" style="background:{color}">{escape(status)}</span>'


def stat(n, label) -> str:
    return f'<div class="stat"><div class="n">{n}</div><div class="l">{escape(label)}</div></div>'


def render_item_row(i) -> str:
    text = escape(i["text"] or i["error"] or "")
    actions = ""
    if i["status"] in ("timeout", "failed", "error"):
        actions = f'<form method="post" action="/retry/{i["id"]}"><button type="submit">retry</button></form>'
    return (
        f"<tr><td>{escape(i['chat_title'] or str(i['chat_id']))}</td>"
        f"<td>{escape(i['kind'])}</td>"
        f"<td class='muted'>{escape(i['msg_date'] or '')}</td>"
        f"<td>{badge(i['status'])}</td>"
        f"<td class='text'>{text}</td>"
        f"<td>{actions}</td></tr>"
    )


def render_chat_row(c) -> str:
    return (
        f"<tr><td>{escape(c['title'] or str(c['chat_id']))}</td>"
        f"<td class='muted'>msg_id {c['cursor_msg_id']}</td>"
        f"<td>{c['pending']}</td><td>{c['done']}</td>"
        f"<td class='muted'>{escape(c['cursor_date'] or '')}</td></tr>"
    )


async def _total_chats() -> int:
    row = await asyncio.to_thread(db.chats_count)
    return row["n"] or 0


@app.get("/", response_class=HTMLResponse)
async def index():
    counts = await asyncio.to_thread(db.status_counts)
    chats_row = await asyncio.to_thread(db.chats_count)
    items = await asyncio.to_thread(db.list_recent_items, 300)

    stats_html = "".join([
        stat(chats_row["n"] or 0, "chats"),
        stat(chats_row["scanned"] or 0, "scanned"),
        stat(counts.get("pending", 0), "pending"),
        stat(counts.get("processing", 0), "processing"),
        stat(counts.get("done", 0), "done"),
        stat(counts.get("timeout", 0), "timeout"),
        stat(counts.get("failed", 0), "failed"),
        stat(counts.get("error", 0), "error"),
    ])
    item_rows = "\n".join(render_item_row(i) for i in items) or "<tr><td colspan=6 class='muted'>nothing yet</td></tr>"

    body = f"""
<h1>Overview</h1>
<div class="stats">{stats_html}</div>
<h1>Recent items</h1>
<table>
<tr><th>Chat</th><th>Kind</th><th>Date</th><th>Status</th><th>Transcript</th><th></th></tr>
{item_rows}
</table>
"""
    return BASE.format(body=body, total_chats=chats_row["n"] or 0)


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(q: str = "", limit: int = 200):
    chats_row = await asyncio.to_thread(db.chats_count)
    chats = await asyncio.to_thread(db.list_chats, q, limit)
    chat_rows = "\n".join(render_chat_row(c) for c in chats) or "<tr><td colspan=5 class='muted'>no chats scanned yet</td></tr>"

    body = f"""
<h1>Chats ({len(chats)} shown of {chats_row['n'] or 0}, {chats_row['scanned'] or 0} scanned)</h1>
<form method="get" action="/chats" style="margin-bottom:16px">
  <input type="text" name="q" placeholder="filter by title" value="{escape(q)}">
  <input type="hidden" name="limit" value="{0 if limit == 0 else max(limit, 200)}">
  <button type="submit">filter</button>
  <a href="/chats?limit=0{'&q=' + q if q else ''}" style="margin-left:10px">show all</a>
</form>
<table>
<tr><th>Chat</th><th>Cursor</th><th>Pending</th><th>Done</th><th>Updated</th></tr>
{chat_rows}
</table>
"""
    return BASE.format(body=body, total_chats=chats_row["n"] or 0)


@app.post("/retry/{item_id}")
async def retry(item_id: int):
    await asyncio.to_thread(db.retry_item, item_id)
    return RedirectResponse(url="/", status_code=303)
