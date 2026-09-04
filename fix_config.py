"""Restore the telegram MCP entry (absolute paths) in Claude configs.

Run this from a plain terminal while Claude Desktop is fully closed,
if the app ever reverts the entry back to bare "python".
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

ENTRY = {
    "command": sys.executable,
    "args": [os.path.join(HERE, "server.py")],
}

CONFIGS = [
    os.path.expanduser(r"~\.claude.json"),
    os.path.expandvars(r"%APPDATA%\Claude\claude_desktop_config.json"),
]

for path in CONFIGS:
    with io.open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg.setdefault("mcpServers", {})["telegram"] = dict(ENTRY)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"fixed: {path}")
