#!/usr/bin/env python3
"""Фейковый слушатель для тестов дежурств: те же аргументы и тот же формат stdout, что у tools/watch_chat.py,
но БЕЗ Telegram — никакой сети и сессий. Пишет синтетические READY / LISTENING / ALIVE / BATCH + NEW,
дописывает NEW в --log и обновляет --state. Сам завершается через --max-sec (предохранитель от сирот).

    python fake_listener.py --chat 777000 --state s.json --log in.log --alive-sec 0.5 --batch-every 3
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

FILTER = r"^(BATCH|READY|LISTENING|Traceback|RuntimeError|ConnectionError|AuthKey|.*Error)"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="append", default=[])
    ap.add_argument("--state")
    ap.add_argument("--log")
    ap.add_argument("--quiet", type=int, default=0)
    ap.add_argument("--trigger", default="/ai")
    ap.add_argument("--alive-sec", type=float, default=1.0, help="пульс ALIVE раз в N секунд")
    ap.add_argument("--batch-every", type=int, default=0, help="каждые N пульсов — синтетическая пачка (0 — нет)")
    ap.add_argument("--max-sec", type=float, default=300.0, help="завершиться через N секунд")
    ap.add_argument("--print-filter", action="store_true")
    a = ap.parse_args()
    if a.print_filter:
        print(FILTER)
        return 0
    chat_id = int(a.chat[0]) if a.chat and a.chat[0].lstrip("-").isdigit() else 777000
    title = "Тестовый чат"
    state_p = Path(a.state)
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {}
    chats = state.setdefault("chats", {})
    last = int(chats.get(str(chat_id), {}).get("last_seen_id", 100))
    print(f"READY connected via fake at {datetime.now():%H:%M:%S}", flush=True)
    print(f"READY chat resolved: {title} (id {chat_id})", flush=True)
    print(f"LISTENING 1 chat(s), пачки после {a.quiet} с тишины; фейковый слушатель для тестов", flush=True)
    t0 = time.time()
    beats = 0
    while time.time() - t0 < a.max_sec:
        time.sleep(a.alive_sec)
        beats += 1
        if a.batch_every and beats % a.batch_every == 0:
            last += 1
            print(f"BATCH 1 новых сообщений в «{title}» (ids {last}–{last}), обработать вместе", flush=True)
            rec = {"chat_id": chat_id, "chat": title, "id": last, "from": "Тест",
                   "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": f"синтетика {last}",
                   "media": None, "file": None}
            line = "NEW " + json.dumps(rec, ensure_ascii=False)
            print(line, flush=True)
            if a.log:
                with open(a.log, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        chats[str(chat_id)] = {"title": title, "last_seen_id": last}
        state_p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        print(f"ALIVE {datetime.now():%H:%M:%S} connected=True via fake; «{title}» last_seen_id {last}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
