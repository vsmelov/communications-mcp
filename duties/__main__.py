"""`python -m duties` (из папки communications-mcp) — дашборд дежурств на переднем плане; подкоманды — см. cli.py."""
import sys
from pathlib import Path

if __package__ in (None, ""):  # запуск файлом: python duties/__main__.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from duties.cli import main
else:
    from .cli import main

sys.exit(main())
