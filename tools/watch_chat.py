#!/usr/bin/env python3
"""Shim: watch_chat.py lives in the tg-claude-listener submodule (github.com/vsmelov/tg-claude-listener).

Run and imported exactly as before (`python tools/watch_chat.py ...`, duties/, tests). TG_LISTENER_HOME points the
submodule at this folder, so .env, sessions/ and netpath.py here are what it uses.
"""
import importlib.util
import os
import runpy
import sys
from pathlib import Path

_HOME = Path(__file__).resolve().parents[1]
_SRC = _HOME / "tg-claude-listener" / "tools" / "watch_chat.py"
os.environ.setdefault("TG_LISTENER_HOME", str(_HOME))
sys.path.insert(0, str(_SRC.parent))

# duties/control.py reads this line without importing the script; keep it equal to the submodule's FILTER
FILTER = r"^(BATCH|READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)"

if __name__ == "__main__":
    runpy.run_path(str(_SRC), run_name="__main__")
else:
    _spec = importlib.util.spec_from_file_location("_tg_listener_" + _SRC.stem, _SRC)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
