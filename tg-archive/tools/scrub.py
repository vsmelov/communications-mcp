"""Тонкий лаунчер CLI-скраббера. Вся логика — в archiver/scrub.py, чтобы её
могли импортировать и контейнер (describe.py), и этот скрипт с хоста.

Запуск с хоста из папки tg-archive:
    python tools/scrub.py dump data/chats/<...>/dump.json
Эквивалентно:
    python -m archiver.scrub dump data/chats/<...>/dump.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.scrub import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
