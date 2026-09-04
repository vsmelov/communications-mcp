"""Локальный скраббер секретов: вырезает приватные ключи, seed-фразы, токены,
адреса кошельков и «пароли» ДО отправки текста в любой внешний API.

Работает офлайн и детерминированно (regex + словарь BIP39). Ключевая идея:
очистка обязана происходить ДО отправки. Нельзя «вырезать потом дешёвой
моделью» — эта модель сама получит секрет целиком, то есть шаг очистки и
станет утечкой. Поэтому здесь ни одного сетевого вызова.

Каждый найденный секрет получает стабильный id в SQLite-хранилище, а в тексте
на его месте остаётся ссылка вида  [[secret #42 type:seed_phrase]] . Один и тот
же секрет всегда получает один id (дедуп по соли+хешу).

БЕЗОПАСНОСТЬ ХРАНИЛИЩА: сам секрет в базе НЕ хранится — только соль+SHA-256
(для стабильного id и дедупа), тип, длина и маскированное превью. Это осознанный
выбор: складывать приватные ключи чата в один plaintext-файл = создать самый
ценный файл на диске. Оригинал всё равно есть в исходном дампе. Хранилище живёт
в data/ (в .gitignore, не синкается).

Используется и как библиотека (archiver/describe.py импортирует scrub_text +
SecretVault), и как CLI (см. tools/scrub.py или `python -m archiver.scrub`).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_BIP39_PATH = _HERE / "bip39_english.txt"
_DEFAULT_VAULT = _ROOT / "data" / "secrets.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load_bip39() -> set[str]:
    try:
        words = _BIP39_PATH.read_text(encoding="utf-8").split()
        if len(words) >= 2000:
            return set(words)
    except OSError:
        pass
    return set()


_BIP39 = _load_bip39()

# --- Детерминированные паттерны. Порядок важен: специфичные/длинные раньше. ---
# Лучше перерезать лишнее, чем пропустить секрет.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem_private_key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL)),
    ("xprv", re.compile(r"\b(?:xprv|yprv|zprv|tprv|uprv|vprv)[1-9A-HJ-NP-Za-km-z]{100,115}\b")),
    ("xpub", re.compile(r"\b(?:xpub|ypub|zpub|tpub|upub|vpub)[1-9A-HJ-NP-Za-km-z]{100,115}\b")),
    ("wif_privkey", re.compile(r"\b[5KL][1-9A-HJ-NP-Za-km-z]{50,51}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("api_key_sk", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("hex64", re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")),
    ("eth_addr", re.compile(r"\b0x[0-9a-fA-F]{40}\b")),
    ("btc_addr", re.compile(r"\b(?:bc1[a-z0-9]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,39})\b")),
    ("tron_addr", re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")),
]

_LABELLED = re.compile(
    r"(?i)\b(пароль|парол[ья]|пасс|password|passwd|pwd|пин[- ]?код|pin[- ]?code|pin|"
    r"секрет\w*|secret|seed[- ]?phrase|seed|мнемоник\w*|mnemonic|passphrase|"
    r"private[ _]?key|приватн\w*\s+ключ)"
    r"\s*[:=\-]+\s*(\S{4,})"
)


class SecretVault:
    """SQLite-учёт секретов: стабильный id по соли+хешу, без хранения секрета.

    check_same_thread=False: describe-проход зовёт register() из рабочих потоков
    (asyncio.to_thread), но строго последовательно — реального параллельного
    писателя внутри процесса нет. busy_timeout прикрывает гонку с хостовым CLI,
    который пишет в тот же файл через бинд-маунт (WAL: много читателей + 1 писатель).
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS secrets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,
                length INTEGER NOT NULL,
                preview TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                first_ref TEXT
            )""")
        row = self.db.execute("SELECT v FROM meta WHERE k='salt'").fetchone()
        if row:
            self._salt = row[0]
        else:
            self._salt = token_hex(16)
            self.db.execute("INSERT INTO meta(k,v) VALUES('salt',?)", (self._salt,))
        self.db.commit()

    def register(self, kind: str, raw: str, ref: str | None = None) -> int:
        h = hashlib.sha256((self._salt + raw).encode("utf-8")).hexdigest()
        now = _now()
        cur = self.db.execute("SELECT id FROM secrets WHERE sha256=?", (h,)).fetchone()
        if cur:
            self.db.execute(
                "UPDATE secrets SET occurrences=occurrences+1, last_seen=? WHERE id=?",
                (now, cur[0]))
            self.db.commit()
            return cur[0]
        self.db.execute(
            "INSERT INTO secrets(sha256,kind,length,preview,occurrences,first_seen,last_seen,first_ref)"
            " VALUES(?,?,?,?,1,?,?,?)",
            (h, kind, len(raw), _preview(kind, raw), now, now, ref))
        self.db.commit()
        return self.db.execute("SELECT id FROM secrets WHERE sha256=?", (h,)).fetchone()[0]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM secrets").fetchone()[0]

    def commit(self):
        self.db.commit()


def _preview(kind: str, raw: str) -> str:
    """Маскированное превью для отчёта/аудита — не раскрывает секрет."""
    if kind == "seed_phrase":
        w = raw.split()
        return f"{w[0]} … {w[-1]} ({len(w)} words)" if len(w) >= 2 else "***"
    raw = raw.strip()
    return f"{raw[:3]}…{raw[-3:]}" if len(raw) > 8 else "***"


def _placeholder(kind: str, raw: str, vault: "SecretVault | None", ref: str | None) -> str:
    if vault is not None:
        return f"[[secret #{vault.register(kind, raw, ref)} type:{kind}]]"
    return f"[REDACTED:{kind}]"


def _find_bip39_runs(text: str) -> list[tuple[int, int]]:
    """Диапазоны максимальных рядов из >=12 подряд слов словаря BIP39.

    12 английских слов подряд, все из фиксированного списка 2048 — это почти
    гарантированно seed-фраза, а не обычный текст (тем более в русском чате).
    """
    if not _BIP39:
        return []
    spans: list[tuple[int, int]] = []
    run: list[re.Match] = []

    def flush():
        if len(run) >= 12:
            spans.append((run[0].start(), run[-1].end()))
        run.clear()

    for tok in re.finditer(r"[A-Za-z]+", text):
        if tok.group(0).lower() in _BIP39:
            run.append(tok)
        else:
            flush()
    flush()
    return spans


def scrub_text(text: str, vault: "SecretVault | None" = None,
               ref: str | None = None) -> tuple[str, dict[str, int]]:
    """Очистить строку. Вернуть (очищенный_текст, {тип: сколько_вырезано})."""
    counts: dict[str, int] = {}
    if not text:
        return text, counts

    for start, end in reversed(_find_bip39_runs(text)):
        ph = _placeholder("seed_phrase", text[start:end], vault, ref)
        text = text[:start] + ph + text[end:]
        counts["seed_phrase"] = counts.get("seed_phrase", 0) + 1

    def repl_labelled(m: re.Match) -> str:
        return f"{m.group(1)}: {_placeholder('labelled_secret', m.group(2), vault, ref)}"

    text, n = _LABELLED.subn(repl_labelled, text)
    if n:
        counts["labelled_secret"] = n

    for name, rx in _PATTERNS:
        def repl(m: re.Match, _k=name) -> str:
            return _placeholder(_k, m.group(0), vault, ref)
        text, n = rx.subn(repl, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts


def _merge(dst: dict[str, int], src: dict[str, int]) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def scrub_dump(obj, vault: "SecretVault | None" = None):
    """Очистить структуру дампа. Возвращает (очищенный_obj, totals, hits_by_id)."""
    totals: dict[str, int] = {}
    hits_by_id: dict[str, dict[str, int]] = {}
    msgs = obj.get("messages") if isinstance(obj, dict) else obj
    if not isinstance(msgs, list):
        raise SystemExit("dump: не найден список messages")
    for row in msgs:
        if not isinstance(row, dict):
            continue
        ref = f"msg:{row.get('id', '?')}"
        row_counts: dict[str, int] = {}
        for fld in ("text", "transcript", "description"):
            val = row.get(fld)
            if isinstance(val, str) and val:
                clean, c = scrub_text(val, vault, ref)
                if c:
                    row[fld] = clean
                    _merge(row_counts, c)
        if row_counts:
            _merge(totals, row_counts)
            hits_by_id[str(row.get("id", "?"))] = row_counts
    return obj, totals, hits_by_id


# ------------------------------------------------------------------- CLI --
def _report(totals: dict[str, int], where: str, vault: "SecretVault | None") -> None:
    if not totals:
        print(f"[scrub] {where}: секретов не найдено", file=sys.stderr)
    else:
        print(f"[scrub] {where}: вырезано по типам:", file=sys.stderr)
        for k, v in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"          {k:18s} {v}", file=sys.stderr)
    if vault is not None:
        print(f"[scrub] в хранилище уникальных секретов: {vault.count()}", file=sys.stderr)
    if not _BIP39:
        print("[scrub] ВНИМАНИЕ: bip39_english.txt не загружен — seed-фразы НЕ детектируются",
              file=sys.stderr)


def _parse_flags(argv: list[str]) -> tuple[list[str], "SecretVault | None"]:
    args = list(argv)
    vault_path: Path | None = _DEFAULT_VAULT
    if "--no-vault" in args:
        args.remove("--no-vault")
        vault_path = None
    if "--vault" in args:
        i = args.index("--vault")
        vault_path = Path(args[i + 1])
        del args[i:i + 2]
    return args, (SecretVault(vault_path) if vault_path else None)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    args, vault = _parse_flags(argv)
    mode = args[1]

    if mode == "text":
        clean, counts = scrub_text(args[2] if len(args) > 2 else sys.stdin.read(), vault)
        sys.stdout.write(clean + "\n")
        _report(counts, "text", vault)
        return 0

    if mode == "file":
        src = Path(args[2])
        dst = Path(args[3]) if len(args) > 3 else src.with_suffix(src.suffix + ".scrubbed")
        clean, counts = scrub_text(src.read_text(encoding="utf-8"), vault, ref=src.name)
        dst.write_text(clean, encoding="utf-8")
        _report(counts, src.name, vault)
        print(f"[scrub] -> {dst}", file=sys.stderr)
        return 0

    if mode == "dump":
        src = Path(args[2])
        dst = Path(args[3]) if len(args) > 3 else src.with_suffix(".scrubbed.json")
        obj = json.loads(src.read_text(encoding="utf-8"))
        clean, totals, hits = scrub_dump(obj, vault)
        dst.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        _report(totals, src.name, vault)
        if hits:
            ids = ", ".join(list(hits)[:30])
            print(f"[scrub] затронуто сообщений: {len(hits)} (id: {ids}"
                  f"{'…' if len(hits) > 30 else ''})", file=sys.stderr)
        print(f"[scrub] -> {dst}", file=sys.stderr)
        return 0

    print(f"неизвестный режим: {mode}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
