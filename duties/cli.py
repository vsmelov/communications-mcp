"""Командная строка дежурств (из папки communications-mcp):

    python -m duties                         дашборд на переднем плане (http://127.0.0.1:18210/)
    python -m duties dashboard [--port N] [--open]
    python -m duties list                    сводка по всем дежурствам
    python -m duties status amir             полный статус одного (JSON)
    python -m duties start|stop|restart amir
    python -m duties tail amir [-n 40] [--text]      хвост stdout (тексты сообщений скрыты без --text)
    python -m duties monitor-command amir[,vyacheslav]   готовая команда для тула Monitor
    python -m duties attach amir [--session local_…] [--title …]
    python -m duties handled amir [--up-to ID] [--chat ID] [--note …]
    python -m duties add NAME --chat ID [--workspace PATH] [--title …] [--start]
    python -m duties discover                живые слушатели, которых нет в реестре
    python -m duties adopt PID --name NAME [--title …]   завести найденный слушатель (без перезапуска)
    python -m duties activate|deactivate NAME
"""
from __future__ import annotations

import argparse
import json
import sys


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog="python -m duties", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("dashboard")
    d.add_argument("--port", type=int, default=18210)
    d.add_argument("--open", action="store_true", help="открыть в браузере")
    d.add_argument("--verbose", action="store_true")
    sub.add_parser("list")
    for c in ("status", "start", "stop", "restart", "activate", "deactivate"):
        sub.add_parser(c).add_argument("name")
    t = sub.add_parser("tail")
    t.add_argument("name")
    t.add_argument("-n", "--lines", type=int, default=30)
    t.add_argument("--text", action="store_true", help="показать тексты сообщений")
    mc = sub.add_parser("monitor-command")
    mc.add_argument("names", help="имя или несколько через запятую")
    at = sub.add_parser("attach")
    at.add_argument("name")
    at.add_argument("--session", default="")
    at.add_argument("--title", default="")
    h = sub.add_parser("handled")
    h.add_argument("name")
    h.add_argument("--up-to", type=int, default=0)
    h.add_argument("--chat", default=None)
    h.add_argument("--note", default="")
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--chat", required=True, help="id / @username / подстрока; несколько — через запятую")
    a.add_argument("--workspace", default="")
    a.add_argument("--title", default="")
    a.add_argument("--start", action="store_true")
    a.add_argument("--quiet", type=int, default=0)
    a.add_argument("--trigger", default="/ai")
    a.add_argument("--no-sign", action="store_true")
    sub.add_parser("discover")
    ad = sub.add_parser("adopt")
    ad.add_argument("pid", type=int)
    ad.add_argument("--name", required=True)
    ad.add_argument("--title", default="")
    args = ap.parse_args(argv)

    if args.cmd in (None, "dashboard"):
        from .dashboard import serve
        return serve(port=getattr(args, "port", 18210), open_browser=getattr(args, "open", False),
                     verbose=getattr(args, "verbose", False))

    from . import api, status
    if args.cmd == "list":
        _print(api.list_duties())
    elif args.cmd == "status":
        _print(api.duty_status(args.name))
    elif args.cmd in ("start", "stop", "restart"):
        _print(getattr(api, args.cmd)(args.name))
    elif args.cmd in ("activate", "deactivate"):
        _print(api.set_active(args.name, args.cmd == "activate"))
    elif args.cmd == "tail":
        res = api.tail(args.name, args.lines, args.text)
        if res.get("lines") is not None:
            print("\n".join(res["lines"]))
        else:
            _print(res)
    elif args.cmd == "monitor-command":
        res = api.monitor_command(args.names)
        print(res.get("command") or json.dumps(res, ensure_ascii=False))
    elif args.cmd == "attach":
        _print(api.attach(args.name, args.session, args.title))
    elif args.cmd == "handled":
        _print(api.handled(args.name, args.up_to, args.chat, args.note))
    elif args.cmd == "add":
        _print(api.add(name=args.name, chat=args.chat, workspace=args.workspace, title=args.title,
                       start_now=args.start, quiet=args.quiet, trigger=args.trigger, sign=not args.no_sign))
    elif args.cmd == "discover":
        _print(status.collect()["unregistered"])
    elif args.cmd == "adopt":
        found = [f for f in status.collect()["unregistered"] if args.pid in f["pids"]]
        if not found:
            _print({"ok": False, "error": f"незаведённый слушатель с PID {args.pid} не найден (python -m duties discover)"})
            return 1
        f = found[0]
        _print(api.add(name=args.name, chat=f["chats"], workspace=f["workspace"] or "", title=args.title,
                       adopt=True, state=f["state"] or "", log=f["log"] or "", stdout=f["stdout"] or "",
                       quiet=f.get("quiet") or 0, trigger=f.get("trigger") if f.get("trigger") is not None else "/ai",
                       create_readme=False))
    return 0
