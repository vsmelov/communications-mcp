#!/usr/bin/env python3
"""Ответ Claude в Telegram-чат с подписью «<эмодзи Claude> **Claude:**» — по умолчанию для дежурств.

Зачем. Когда Claude отвечает в чате от аккаунта Владимира, собеседник должен видеть, что пишет ИИ-агент.
Подпись — премиум-эмодзи Claude (custom emoji, у аккаунта Telegram Premium) + жирное «Claude:».
MCP `send_message` кастомные эмодзи не умеет, поэтому ответы в дежурствах шлём этим скриптом.

Полезные id:
    CLAUDE_EMOJI_ID = 6273917097319865433   # премиум-эмодзи Claude, fallback-символ 🤖
                                            # (снят с собственного сообщения Владимира с этим эмодзи)

Примеры (Git Bash):
    python tools/send_reply.py --chat <chat_id> --text "привет"
    python tools/send_reply.py --chat <chat_id> --text-file reply.txt --reply-to <msg_id>
    python tools/send_reply.py --chat <chat_id> --text "без подписи" --no-sign
    python tools/send_reply.py --chat <chat_id> --file doc.docx --text "Регламент готов"

Текст — обычный Telegram-markdown (**жирный**, __курсив__, `код`, [ссылка](url)); подпись добавляется сверху
той же строкой: «🤖 Claude: текст». Печатает `SENT <chat> id <msg_id>`.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from telethon.extensions import markdown
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_chat as w  # noqa: E402 — сессия/прокси берём оттуда же, что и слушатель

CLAUDE_EMOJI_ID = 6273917097319865433
CLAUDE_EMOJI_FALLBACK = "🤖"


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def build(text: str, sign: bool):
    body, ents = markdown.parse(text)
    if not sign:
        return body, ents
    head = f"{CLAUDE_EMOJI_FALLBACK} Claude: "
    shift = u16(head)
    for e in ents:
        e.offset += shift
    sig = [MessageEntityCustomEmoji(offset=0, length=u16(CLAUDE_EMOJI_FALLBACK), document_id=CLAUDE_EMOJI_ID),
           MessageEntityBold(offset=u16(CLAUDE_EMOJI_FALLBACK + " "), length=u16("Claude:"))]
    return head + body, sig + ents


async def main(a):
    text = Path(a.text_file).read_text(encoding="utf-8") if a.text_file else a.text
    if not text or not text.strip():
        raise SystemExit("пустой текст")
    body, ents = build(text.strip(), sign=not a.no_sign)
    client = await w.connect(a.session, "send_reply")
    try:
        chat = int(a.chat) if a.chat.lstrip("-").isdigit() else a.chat
        if a.file:  # файл с подписью-caption (та же подпись Claude)
            m = await client.send_file(chat, a.file, caption=body, formatting_entities=ents,
                                       reply_to=a.reply_to or None, force_document=True)
        else:
            m = await client.send_message(chat, body, formatting_entities=ents, reply_to=a.reply_to or None,
                                          link_preview=False)
        print(f"SENT {a.chat} id {m.id}", flush=True)
        _journal_reply(a, m, len(text))
    finally:
        await client.disconnect()


def _journal_reply(a, m, chars: int) -> None:
    """05.10.2026, аддитивно: строка в журнал ответов дежурства (notes/<имя>_replies.jsonl, см. duties/journal.py) —
    по нему дашборд дежурств считает «ожидает ответа». Только метаданные (id, время, длина), без текста.
    Любая ошибка глотается: на отправку и вывод SENT журнал не влияет."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from duties.journal import record_reply
        record_reply(chat=a.chat, chat_id=getattr(m, "chat_id", None), msg_id=m.id, reply_to=a.reply_to or None,
                     with_file=bool(a.file), signed=not a.no_sign, chars=chars)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat", required=True, help="id или @username")
    ap.add_argument("--text")
    ap.add_argument("--text-file", help="utf-8 файл с текстом (удобно для длинных ответов)")
    ap.add_argument("--reply-to", type=int, default=0)
    ap.add_argument("--file", help="отправить файл (документом), текст — его подпись")
    ap.add_argument("--no-sign", action="store_true", help="не добавлять подпись «🤖 Claude:»")
    ap.add_argument("--session")
    asyncio.run(main(ap.parse_args()))
