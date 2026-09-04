# telegram-transcribe-service

Automates the Telegram "Transcribe" button (`messages.transcribeAudio`) for
your own DMs, so you don't have to tap it on every voice message / round
video. Runs as one always-on docker-compose service:

- On startup, scans every eligible DM (not archived, not a bot) for voice /
  round-video messages. First time it sees a chat it looks back 24h (30d for
  the one configured override in `app/config.py`); after that it remembers a
  per-chat cursor (last seen message id) and only ever asks for what's newer.
- New incoming DMs are caught live. A periodic sweep (every 15 min) re-checks
  cursors as a safety net for any downtime.
- Exactly one transcription request is in flight at a time, with a 120s wait
  per item — matches Telegram's own one-at-a-time button-tap behavior, just
  automated.
- Dashboard at `http://localhost:8077` — per-chat cursor/progress + a table
  of recent items with their transcript text.

## Setup

1. Copy a **valid, already-authorized** Telethon `.session` file to
   `data/session.session` (no `.session` extension in code, Telethon appends
   it). Easiest source: your existing `communications-mcp` session — copy it,
   don't move it, since that MCP server still needs its own open handle:

   ```bash
   cp "../sessions/mcp.session" "./data/session.session"
   ```

   This does **not** create a second login — same `auth_key`, same device,
   just another connection to it.

2. Copy `.env.example` to `.env` and fill in `TELEGRAM_API_ID` /
   `TELEGRAM_API_HASH` (same values as `communications-mcp/.env`). Leave
   `TELEGRAM_PROXY` pointed at `host.docker.internal` — that's how the
   container reaches your local proxy on the host.

3. Start it:

   ```bash
   docker compose up -d --build
   ```

4. Open http://localhost:8077

## Notes

- `app/config.py` — `INITIAL_WINDOW_OVERRIDES` is where per-chat one-time
  backfill windows live (empty by default: every chat gets
  `DEFAULT_INITIAL_WINDOW`). Add entries there if needed; existing chats with a
  cursor already saved ignore it.
- State lives in `data/db.sqlite3` (sqlite) and `data/session.session`
  (Telethon auth) — both are bind-mounted so `docker compose down/up`
  preserves everything.
