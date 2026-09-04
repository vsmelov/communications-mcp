"""Формат строк и рендер дампа.

Формат совместим с dump/dump_search из claude_telegram_mcp/server.py:
те же поля строк (id, ts, date_utc, from, text, media, file, ...), тот же
dump.md/dump.json и тот же manifest.json — чтобы существующие инструменты
читали архив без изменений.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from telethon import types, utils

# Подпапка на каждый вид медиа. Порядок важен: первое совпадение выигрывает,
# специфичные флаги Telethon (voice, video_note, gif) раньше общих (video, document).
MEDIA_KINDS = (
    ("sticker", "stickers"),
    ("voice", "voice"),
    ("video_note", "video_notes"),
    ("gif", "gifs"),
    ("video", "videos"),
    ("audio", "audio"),
    ("photo", "photos"),
    ("document", "documents"),
)

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}


def slugify(name: str, max_words: int = 4) -> str:
    """ASCII-слаг названия чата: 'Иван Иванов' -> 'ivan-ivanov'."""
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in (name or "").casefold())
    words = re.findall(r"[a-z0-9]+", latin)
    return "-".join(words[:max_words]) or "chat"


def utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def entity_name(entity) -> str:
    return utils.get_display_name(entity) or str(getattr(entity, "id", "?"))


def media_kind(msg) -> str | None:
    """Категория скачиваемого медиа, или None когда качать нечего."""
    if not msg.media or isinstance(msg.media, types.MessageMediaWebPage):
        return None
    for attr, folder in MEDIA_KINDS:
        if getattr(msg, attr, None):
            return folder
    return "other"


def safe_name(msg, kind: str) -> str:
    """Имя файла без коллизий: id сообщения всегда в префиксе."""
    original = getattr(msg.file, "name", None) if msg.file else None
    if original:
        cleaned = _UNSAFE_CHARS.sub("_", original).strip(". ")[:120]
    else:
        ext = (getattr(msg.file, "ext", None) if msg.file else None) or ""
        cleaned = f"{kind.rstrip('s')}{_UNSAFE_CHARS.sub('_', ext)}"
    return f"{msg.id}_{cleaned}"


def row_from_msg(msg, sender_name: str | None) -> dict:
    row: dict = {
        "id": msg.id,
        "ts": int(msg.date.timestamp()) if msg.date else None,
        "date_utc": utc_str(msg.date) if msg.date else None,
        "from": sender_name,
        "text": msg.text or "",
    }
    # reply_to бывает и MessageReplyStoryHeader (ответ на сторис) — без msg_id
    reply_id = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
    if reply_id:
        row["reply_to_id"] = reply_id
    if getattr(msg, "out", False):
        row["outgoing"] = True

    kind = media_kind(msg)
    if kind:
        row["media"] = kind
        row["size_bytes"] = getattr(msg.file, "size", None) if msg.file else None
        if kind in ("voice", "video_notes", "audio", "videos"):
            # длительность приходит в метаданных бесплатно — на ней строится
            # оценка стоимости транскрибации без скачивания
            dur = getattr(msg.file, "duration", None) if msg.file else None
            if dur:
                row["duration_sec"] = round(float(dur), 1)
    elif msg.media:
        row["media"] = "webpage"
    return row


# Поля, которые живут дольше одного перечитывания сообщения: результат
# скачивания/расшифровки не должен теряться при обновлении текста строки.
STICKY_FIELDS = (
    "file", "audio_only", "skipped_reason", "dl_attempts",
    "transcript", "transcript_model", "transcript_error", "stt_attempts",
    "deleted", "audio_dropped",
    "description", "description_model", "description_error", "desc_attempts", "described",
    "description_kind", "media_secret",
)


def merge_row(old: dict | None, new: dict) -> dict:
    if old:
        for key in STICKY_FIELDS:
            if key in old and key not in new:
                new[key] = old[key]
    return new


def render_md(chat_name: str, rows: list[dict], meta: dict) -> str:
    lines = [
        f"# {chat_name}",
        "",
        f"- Период (UTC): {meta['start']} — {meta['end']}",
        f"- Unix: {meta['start_ts']} — {meta['end_ts']}",
        f"- Сообщений: {meta['messages']}",
        f"- Вложений скачано: {meta['downloaded']} (пропущено: {meta['skipped']})",
        "",
        "---",
        "",
    ]
    for r in rows:
        text = (r["text"] or "").replace("\n", "\n  ")
        head = f"**[{r['id']}] {r['date_utc']} UTC — {r['from']}**"
        if r.get("reply_to_id"):
            head += f"  ↩︎ {r['reply_to_id']}"
        if r.get("deleted"):
            head += "  ✖️ _(удалено)_"
        lines.append(head)
        if text:
            lines.append(f"  {text}")
        if r.get("media"):
            dur = f", {r['duration_sec']:.0f}с" if r.get("duration_sec") else ""
            note_audio = " _(только аудио)_" if r.get("audio_only") else ""
            if r.get("file"):
                lines.append(f"  📎 [{r['media']}{dur}]({r['file']}){note_audio}")
            elif r.get("audio_dropped"):
                # аудио удалено намеренно — расшифровка ниже заменяет файл
                lines.append(f"  🎙️ {r['media']}{dur} — _расшифровано, аудио не хранится_")
            elif r.get("described"):
                # файл удалён; описание/саммери ниже. Оригинал докачиваем из Telegram по id.
                if r.get("description_kind") == "document":
                    lines.append(f"  📄 {r['media']} — _саммери ниже; оригинал в Telegram, "
                                 f"докачать по message id {r['id']}_")
                else:
                    lines.append(f"  🖼️ {r['media']} — _описано; оригинал докачаем по id {r['id']}_")
            else:
                note = r.get("skipped_reason") or "не скачивалось"
                lines.append(f"  📎 {r['media']}{dur} — _{note}_")
            if r.get("media_secret"):
                kinds = ", ".join(r["media_secret"])
                lines.append(f"  ⚠️ _в медиа найдено похожее на секрет ({kinds}) — вырезано_")
            if r.get("transcript"):
                speech = r["transcript"].strip().replace("\n", " ")
                lines.append(f"  [speech] _{speech}_")
            if r.get("description"):
                # сохраняем структуру полей (Type:/Summary:/…) — читаемее, чем в одну строку
                head = "📄 саммери" if r.get("description_kind") == "document" else "🖼️ описание"
                lines.append(f"  {head}:")
                for dl in r["description"].strip().splitlines():
                    if dl.strip():
                        lines.append(f"    {dl.strip()}")
        lines.append("")
    return "\n".join(lines)
