#!/usr/bin/env python3
"""Расшифровка голосовых/кружков из Telegram по msg_id — бесплатно силами Telegram Premium.

Для дежурств: слушатель приносит BATCH с "media": "MessageMediaDocument", а tg-archive расшифровывает
в своём темпе (минуты). Этот скрипт берёт текст сразу: messages.transcribeAudio, ждёт pending.
Если Telegram отказал (лимит/флуд) — печатает ERROR, тогда качать файл и гнать через transcribe MCP.

    python tools/voice_text.py --chat <chat_id> --id <msg_id> --id <msg_id2>

Печатает по строке на сообщение: `<id> [<длительность> с] <текст>`.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from telethon.tl.functions.messages import TranscribeAudioRequest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_chat as w  # noqa: E402


async def openai_text(client, m) -> str | None:
    import os, tempfile
    from openai import OpenAI
    path = await client.download_media(m, file=tempfile.gettempdir() + os.sep)
    try:
        with open(path, "rb") as f:
            name = os.path.basename(path)
            if name.endswith(".oga"):  # голосовые Telegram — ogg/opus, но OpenAI не знает расширение .oga
                name = name[:-4] + ".ogg"
            r = OpenAI().audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=(name, f.read()),
                                                     language="ru")
        return r.text
    except Exception as e:  # noqa: BLE001
        print(f"{m.id} OPENAI ERROR {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def main(a):
    client = await w.connect(a.session, "voice_text")
    try:
        chat = int(a.chat) if a.chat.lstrip("-").isdigit() else a.chat
        peer = await client.get_input_entity(chat)
        msgs = await client.get_messages(chat, ids=a.id)
        for m in msgs:
            if m is None:
                continue
            dur = getattr(getattr(m, "file", None), "duration", None)
            try:
                r = await client(TranscribeAudioRequest(peer=peer, msg_id=m.id))
                for _ in range(20):
                    if not r.pending:
                        break
                    await asyncio.sleep(3)
                    r = await client(TranscribeAudioRequest(peer=peer, msg_id=m.id))
                text = r.text or ""
                # Telegram иногда отдаёт обрезанную расшифровку длинного голосового (04.10.2026: 63 с → обрыв
                # на полуслове). Если просили --openai или текст подозрительно короткий для длительности —
                # докачиваем аудио и расшифровываем OpenAI (gpt-4o-mini-transcribe, ~$0.003/мин).
                if a.openai or (dur and dur > 40 and len(text) < dur * 14):
                    text = await openai_text(client, m) or text
                print(f"{m.id} [{dur} с] {text}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{m.id} ERROR {type(e).__name__}: {e}", flush=True)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat", required=True)
    ap.add_argument("--id", type=int, action="append", required=True)
    ap.add_argument("--openai", action="store_true", help="сразу через OpenAI (если Telegram режет текст)")
    ap.add_argument("--session")
    asyncio.run(main(ap.parse_args()))
