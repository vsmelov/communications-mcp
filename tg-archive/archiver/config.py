"""Конфигурация архиватора: config.yaml + переменные окружения.

Окружение:
  TELEGRAM_API_ID / TELEGRAM_API_HASH  — обязательны
  TELEGRAM_PROXY                       — напр. http://127.0.0.1:10801 (на хосте)
                                         или http://host.docker.internal:10801 (в контейнере)
  TG_ARCHIVE_CONFIG                    — путь к config.yaml (default: ./config.yaml)
  TG_ARCHIVE_DATA                      — корень данных (default: ./data; в контейнере /data)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
# общие креды MCP (OPENAI_API_KEY и т.п.) — уже установленные переменные не перетираются
load_dotenv(_ROOT.parent / ".env")


@dataclass
class ChatCfg:
    query: str  # id, @username или подстрока названия — как в resolve_chat MCP


@dataclass
class SyncCfg:
    poll_interval_sec: float = 600
    jitter_frac: float = 0.25
    between_jobs_sec: float = 20
    history_wait_sec: float = 3
    backfill_batch: int = 800
    edit_window: int = 100
    realtime_events: bool = True     # события Telegram: новые сообщения в дамп за секунды
    event_debounce_sec: float = 3.0  # копим шквал событий и пишем на диск одним разом


@dataclass
class MediaCfg:
    max_mb: float = 10
    per_cycle: int = 25
    delay_sec: float = 4
    video_note_source_max_mb: float = 100
    download_error_retries: int = 3

    @property
    def max_bytes(self) -> int:
        return int(self.max_mb * 1024 * 1024)

    @property
    def video_note_source_max_bytes(self) -> int:
        return int(self.video_note_source_max_mb * 1024 * 1024)


@dataclass
class TranscribeCfg:
    enabled: bool = False
    provider: str = "telegram"  # telegram = бесплатно (Premium) | openai = платно (STT)
    model: str = "gpt-4o-mini-transcribe"  # используется только при provider: openai
    kinds: list[str] = field(default_factory=lambda: ["voice", "video_notes", "audio"])
    per_cycle: int = 40
    delay_sec: float = 5.0  # пауза между запросами расшифровки (не триггерить флуд вовсе)
    daily_budget_usd: float = 5.0
    error_retries: int = 3
    # После успешной расшифровки удалять сам аудиофайл (войс/кружок/аудио):
    # текст [speech] уже сохранён, файл избыточен и занимает место. Скачанные
    # ранее — тоже подчищаются при следующем проходе. False -> хранить оба.
    drop_audio_after_transcript: bool = True


@dataclass
class DescribeCfg:
    """Описание картинок/PDF дешёвой vision-моделью через OpenRouter.

    После успешного описания сам файл удаляется (текст заменяет бинарь), но
    остаётся докачиваемым по msg_id — оригинал живёт на серверах Telegram.
    Требует OPENROUTER_API_KEY; без ключа слой молча спит.
    """
    enabled: bool = False
    provider: str = "openrouter"
    model: str = "google/gemini-2.5-flash-lite"
    kinds: list[str] = field(default_factory=lambda: ["photos", "documents"])
    per_cycle: int = 40
    delay_sec: float = 1.0
    daily_budget_usd: float = 3.0
    error_retries: int = 3
    max_mb: float = 10.0           # не описывать файлы крупнее (и не качать их зря)
    drop_after: bool = True        # удалять файл после описания (докачиваемо по id)
    max_text_chars: int = 4000     # макс. длина сохраняемого описания/саммери
    prompt: str = ""               # пусто -> дефолт из describe.DEFAULT_PROMPT (картинки)
    doc_prompt: str = ""           # пусто -> describe.DEFAULT_DOC_PROMPT (документы, детальнее)
    doc_max_input_chars: int = 20000  # сколько извлечённого текста слать в модель на саммери

    @property
    def max_bytes(self) -> int:
        return int(self.max_mb * 1024 * 1024)


@dataclass
class Config:
    api_id: int
    api_hash: str
    proxy_url: str | None
    http_port: int
    data_root: Path
    chats: list[ChatCfg] = field(default_factory=list)
    sync: SyncCfg = field(default_factory=SyncCfg)
    media: MediaCfg = field(default_factory=MediaCfg)
    transcribe: TranscribeCfg = field(default_factory=TranscribeCfg)
    describe: DescribeCfg = field(default_factory=DescribeCfg)
    # Цепочка техник подключения (по порядку до первой рабочей): "direct" =
    # напрямую (нужно в TUN-режиме VPN), либо http://host:port / socks5://host:port.
    proxy_chain: list[str] = field(default_factory=lambda: ["direct"])

    @property
    def chats_root(self) -> Path:
        return self.data_root / "chats"

    @property
    def session_path(self) -> Path:
        """Путь Telethon-сессии без суффикса .session."""
        return self.data_root / "session" / "archiver"

    @property
    def tmp_root(self) -> Path:
        return self.data_root / "tmp"


def _build_proxy_chain() -> list[str]:
    """Список техник подключения к Telegram, по порядку до первой рабочей.

    TELEGRAM_PROXY_CHAIN (через запятую) переопределяет всё. Иначе строим из
    старого TELEGRAM_PROXY + разумные фолбэки (direct для TUN-режима, классический
    прокси на host.docker.internal). Дубли убираем, порядок сохраняем.
    """
    raw = os.getenv("TELEGRAM_PROXY_CHAIN", "").strip()
    if raw:
        chain = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        p = os.getenv("TELEGRAM_PROXY", "").strip()
        chain = ([p] if p else []) + ["direct", "http://host.docker.internal:10801"]
    seen: set[str] = set()
    out: list[str] = []
    for x in chain:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out or ["direct"]


def load_config() -> Config:
    cfg_path = Path(os.getenv("TG_ARCHIVE_CONFIG", "config.yaml"))
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    sync = SyncCfg(**(raw.get("sync") or {}))
    media = MediaCfg(**(raw.get("media") or {}))
    transcribe = TranscribeCfg(**(raw.get("transcribe") or {}))
    describe = DescribeCfg(**(raw.get("describe") or {}))
    chats = [ChatCfg(query=str(c["query"])) for c in (raw.get("chats") or [])]
    if not chats:
        raise SystemExit("config.yaml: список chats пуст — архивировать нечего")

    return Config(
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        proxy_url=os.getenv("TELEGRAM_PROXY", "").strip() or None,
        proxy_chain=_build_proxy_chain(),
        http_port=int(raw.get("http_port", 8722)),
        data_root=Path(os.getenv("TG_ARCHIVE_DATA", "data")).resolve(),
        chats=chats,
        sync=sync,
        media=media,
        transcribe=transcribe,
        describe=describe,
    )
