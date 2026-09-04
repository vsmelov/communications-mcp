"""Транскрибация вложений: telegram (бесплатно) или OpenAI STT (платно).

Траты идут в единый SQLite-журнал (archiver.ledger.Ledger, task='transcribe') —
строка на запрос, включая бесплатные telegram-расшифровки (usd=0), чтобы был
полный лог. Агрегаты (всего/сутки/час) и дневной стоп-кран — оттуда же.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger("stt")

# $ за минуту речи — https://developers.openai.com/api/docs/pricing
PRICING = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
    "gpt-4o-transcribe-diarize": 0.006,
    "whisper-1": 0.006,
}


class Transcriber:
    def __init__(self, cfg, ledger):
        self.cfg = cfg  # TranscribeCfg
        self.ledger = ledger
        self._client = None

    def _openai(self):
        if self._client is None:
            from openai import OpenAI

            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY не задан — транскрибация невозможна")
            self._client = OpenAI()
        return self._client

    def needs(self, row: dict, store) -> bool:
        return (
            row.get("media") in self.cfg.kinds
            and not row.get("transcript")
            and row.get("stt_attempts", 0) < self.cfg.error_retries
            and store.has_file(row)
        )

    async def pass_for(self, syncer) -> dict:
        """Расшифровать до per_cycle файлов чата, не выходя за дневной бюджет."""
        if not self.cfg.enabled:
            return {}
        store = syncer.store
        day_spent = self.ledger.spent("transcribe")["day_usd"]
        pending = [r for r in store.sorted_rows() if self.needs(r, store)][: self.cfg.per_cycle]
        if not pending:
            return {}

        rate = PRICING.get(self.cfg.model, 0.006)
        done = failed = 0
        sec_sum = usd_sum = 0.0
        for row in pending:
            if day_spent >= self.cfg.daily_budget_usd:
                log.warning("[%s] дневной бюджет STT $%.2f исчерпан — остальное в следующие сутки",
                            store.slug, self.cfg.daily_budget_usd)
                break
            path = store.dir / row["file"]
            try:
                row["transcript"] = await asyncio.to_thread(self._transcribe_file, path)
                row["transcript_model"] = self.cfg.model
                row.pop("transcript_error", None)
                dur = float(row.get("duration_sec") or 0.0)
                usd = dur / 60 * rate
                self.ledger.add(task="transcribe", kind=row.get("media", ""),
                                provider="openai", chat_id=store.chat_id, msg_id=row["id"],
                                model=self.cfg.model, seconds=dur, usd=usd)
                day_spent += usd
                usd_sum += usd
                sec_sum += dur
                done += 1
            except Exception as exc:  # один битый файл не должен валить проход
                row["stt_attempts"] = row.get("stt_attempts", 0) + 1
                row["transcript_error"] = str(exc)[:200]
                failed += 1
                log.warning("[%s] stt %s: %s", store.slug, row["id"], exc)
        return {"transcribed": done, "failed": failed,
                "minutes": round(sec_sum / 60, 1), "usd": round(usd_sum, 4)}

    def _transcribe_file(self, path: Path) -> str:
        # OpenAI отклоняет расширение .oga («Unsupported file format»), хотя это
        # тот же Ogg/Opus, что и .ogg — подменяем только имя для API
        upload_name = path.name
        if upload_name.lower().endswith(".oga"):
            upload_name = upload_name[:-4] + ".ogg"
        with open(path, "rb") as f:
            resp = self._openai().audio.transcriptions.create(
                model=self.cfg.model, file=(upload_name, f.read()), response_format="text",
            )
        return resp if isinstance(resp, str) else getattr(resp, "text", str(resp))


# Telegram расшифровывает только голосовые и кружки (не audio-файлы/музыку).
TG_STT_KINDS = {"voice", "video_notes"}


class TelegramTranscriber:
    """Бесплатная расшифровка силами самого Telegram (Premium, messages.transcribeAudio).

    Файл не нужен — запрос идёт по (peer, msg_id), поэтому расшифровываются и
    сообщения, чьё медиа мы ещё не скачали или не храним (кружки >10 МБ).
    Telegram кэширует готовые расшифровки: то, что уже оттриггерил
    telegram-transcribe-service, возвращается мгновенно. Апдейты у архиватора
    выключены, поэтому pending-результат поллим повторными вызовами —
    повторный TranscribeAudioRequest безвреден.
    """

    def __init__(self, cfg, ledger=None):
        self.cfg = cfg  # TranscribeCfg
        self.ledger = ledger

    def needs(self, row: dict, store) -> bool:
        return (
            row.get("media") in TG_STT_KINDS
            and row.get("media") in self.cfg.kinds
            and not row.get("transcript")
            and row.get("stt_attempts", 0) < self.cfg.error_retries
        )

    async def pass_for(self, syncer) -> dict:
        if not self.cfg.enabled:
            return {}
        store = syncer.store
        pending = [r for r in store.sorted_rows() if self.needs(r, store)][: self.cfg.per_cycle]
        if not pending:
            return {}

        from telethon.errors import FloodWaitError
        from telethon.tl.functions.messages import TranscribeAudioRequest

        done = failed = 0
        for row in pending:
            # у transcribeAudio свои лимиты — держим темп, при котором флуд
            # не прилетает вовсе (см. transcribe.delay_sec)
            await syncer._sleep(self.cfg.delay_sec)
            try:
                text = None
                for _ in range(10):  # pending -> поллим тем же запросом
                    res = await syncer.client(
                        TranscribeAudioRequest(peer=syncer.entity, msg_id=row["id"])
                    )
                    if not getattr(res, "pending", False):
                        text = res.text or ""
                        break
                    await asyncio.sleep(5)
                if text is None:  # так и не дозрело — попробуем в следующий проход
                    row["stt_attempts"] = row.get("stt_attempts", 0) + 1
                    failed += 1
                elif text:
                    row["transcript"] = text
                    row["transcript_model"] = "telegram"
                    row.pop("transcript_error", None)
                    if self.ledger is not None:  # бесплатно, но пишем для полного лога
                        self.ledger.add(task="transcribe", kind=row.get("media", ""),
                                        provider="telegram", chat_id=store.chat_id,
                                        msg_id=row["id"], model="telegram",
                                        seconds=float(row.get("duration_sec") or 0.0), usd=0.0)
                    done += 1
                else:
                    # пусто = Telegram не нашёл речи (музыка, шум) — не мучаем повторами
                    row["stt_attempts"] = self.cfg.error_retries
                    row["transcript_error"] = "telegram: пустая расшифровка"
            except FloodWaitError:
                raise  # наверх — глобальная пауза воркера
            except Exception as exc:
                row["stt_attempts"] = row.get("stt_attempts", 0) + 1
                row["transcript_error"] = str(exc)[:200]
                failed += 1
                msg = str(exc).upper()
                if "PREMIUM" in msg or "TRANSCRIPTION" in msg:
                    log.warning("[%s] telegram-STT недоступен (%s) — прекращаю проход", store.slug, exc)
                    break
        return {"transcribed": done, "failed": failed, "usd": 0.0}
