"""Статус и тревоги на синтетическом снимке процессов и временных файлах (без реальных процессов и чатов)."""
import json
import os
import time
import unittest
from datetime import datetime
from pathlib import Path

from duties import registry, status
from duties.textparse import to_msys

from .helpers import FakeSnap, TempEnv, proc

CHAT = 111222
SID = "local_00000000-0000-4000-8000-000000000001"
CLI = "00000000-0000-4000-8000-0000000000aa"


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.now = time.time()
        ws = self.env.dir / "ws"
        (ws / "notes").mkdir(parents=True)
        self.ws = ws
        self.d = {
            "name": "test", "title": "Тестовый чат", "chats": [{"id": CHAT, "title": "Тестовый чат", "spec": str(CHAT)}],
            "workspace": str(ws), "state": str(ws / "notes" / "t_state.json"), "log": str(ws / "notes" / "t_in.log"),
            "stdout": str(ws / "notes" / "t_out.log"), "replies": str(ws / "notes" / "t_replies.jsonl"),
            "script": None, "active": True, "agent": None,
        }
        self.data = registry.empty()
        self.data["duties"]["test"] = self.d
        hm = datetime.fromtimestamp(self.now - 60).strftime("%H:%M:%S")
        Path(self.d["stdout"]).write_text(
            f"READY connected via fake at {hm}\nLISTENING 1 chat(s), пачки после 0 с тишины\n"
            f"ALIVE {hm} connected=True via fake; «Тестовый чат» last_seen_id 500\n", encoding="utf-8")
        # сессия десктоп-приложения для названия агента
        sd = self.env.dir / "sessions" / "acc" / "org"
        sd.mkdir(parents=True)
        (sd / f"{SID}.json").write_text(json.dumps({"sessionId": SID, "cliSessionId": CLI, "title": "тест-сессия",
                                                    "isArchived": False, "lastActivityAt": str(int(self.now * 1000))}),
                                        encoding="utf-8")

    def tearDown(self):
        self.env.__exit__(None, None, None)

    # --- процессы
    def _listener(self, pid=1001, ct=None, state=None):
        argv = [r"C:\Python312\python.exe", "-u", r"C:\x\communications-mcp\tools\watch_chat.py", "--chat", str(CHAT),
                "--state", state or self.d["state"], "--log", self.d["log"]]
        return proc(pid, 1000, "python.exe", argv, ct if ct is not None else self.now - 100)

    def _monitor(self, wrapper_ct=None, with_orphan=True):
        ct = wrapper_ct or self.now - 50
        cmd = ["C:\\Program Files\\Git\\bin\\bash.exe", "-c",
               f"source snap.sh && eval 'L={to_msys(self.d['stdout'])}; tail -n 0 -f $L | grep x'"]
        procs = [proc(900, 1, "claude.exe", ["claude.exe", "--output-format", "stream-json"], self.now - 5000),
                 proc(1100, 900, "bash.exe", cmd, ct), proc(1101, 1100, "bash.exe", cmd, ct),
                 proc(1200, 4242, "tail.exe", ["tail", "-n", "0", "-f", to_msys(self.d["stdout"])], ct + 1)]
        if with_orphan:
            procs.append(proc(1300, 4343, "tail.exe", ["tail", "-n", "0", "-f", to_msys(self.d["stdout"])], ct - 3600))
        env = {1100: {"CLAUDE_CODE_HOST_SESSION_ID": SID, "CLAUDE_CODE_SESSION_ID": CLI, "CLAUDE_PID": "900"},
               900: {"CLAUDE_CODE_HOST_SESSION_ID": SID}}
        return procs, env

    def _collect(self, procs, env=None):
        snap = FakeSnap(procs, env=env or {})
        return status.collect(self.data, snap, now=self.now)["duties"]["test"]

    # --- тесты
    def test_all_green(self):
        mprocs, env = self._monitor()
        s = self._collect([self._listener()] + mprocs, env)
        self.assertEqual(s["listener"]["state"], "alive")
        self.assertFalse(s["listener"]["outdated"])
        self.assertEqual(s["heartbeat"]["level"], "green")
        self.assertEqual(s["connection"]["state"], "online")
        self.assertEqual(s["agent"]["state"], "connected")
        self.assertEqual(s["agent"]["monitors"][0]["session_id"], SID)
        self.assertEqual(s["agent"]["monitors"][0]["title"], "тест-сессия")
        self.assertTrue(s["agent"]["monitors"][0]["link"].endswith(SID))
        self.assertEqual(s["agent"]["orphan_pipes"], 1)
        self.assertEqual(s["alerts"], [])
        self.assertEqual(s["level"], "green")

    def test_listener_alive_agent_missing_is_red(self):
        s = self._collect([self._listener()])
        codes = [a["code"] for a in s["alerts"]]
        self.assertIn("agent_missing", codes)
        self.assertEqual(s["level"], "red")

    def test_rearming_grace_then_idle(self):
        mprocs, env = self._monitor()
        self._collect([self._listener()] + mprocs, env)  # агента «видели» — кэш в рантайме
        claude = [p for p in mprocs if p.pid == 900]
        s = self._collect([self._listener()] + claude, {900: env[900]})
        self.assertEqual(s["agent"]["state"], "rearming")
        self.assertIn("agent_rearming", [a["code"] for a in s["alerts"]])
        self.assertEqual(s["level"], "yellow")
        seen = status.load_seen()
        seen["test"]["seen_at"] = self.now - 3600
        status.save_seen(seen)
        s = self._collect([self._listener()] + claude, {900: env[900]})
        self.assertEqual(s["agent"]["state"], "idle")
        self.assertIn("agent_idle", [a["code"] for a in s["alerts"]])

    def test_dead_listener(self):
        s = self._collect([])
        self.assertEqual(s["listener"]["state"], "dead")
        self.assertEqual([a["code"] for a in s["alerts"]], ["listener_dead"])

    def test_duplicate_listener(self):
        mprocs, env = self._monitor()
        s = self._collect([self._listener(1001), self._listener(2001)] + mprocs, env)
        self.assertEqual(s["listener"]["state"], "duplicate")
        self.assertIn("listener_duplicate", [a["code"] for a in s["alerts"]])

    def test_venv_launcher_pair_is_one_listener(self):
        mprocs, env = self._monitor()
        parent = self._listener(1001)
        child = self._listener(1002)
        child.ppid = 1001
        s = self._collect([parent, child] + mprocs, env)
        self.assertEqual(s["listener"]["state"], "alive")
        self.assertEqual(s["listener"]["pids"], [1001, 1002])

    def test_outdated_listener(self):
        mprocs, env = self._monitor()
        s = self._collect([self._listener(ct=1_000_000_000.0)] + mprocs, env)
        self.assertTrue(s["listener"]["outdated"])
        self.assertIn("listener_outdated", [a["code"] for a in s["alerts"]])

    def test_heartbeat_stale_and_connection_lost(self):
        old = self.now - 40 * 60
        hm = datetime.fromtimestamp(old).strftime("%H:%M:%S")
        Path(self.d["stdout"]).write_text(f"ALIVE {hm} connected=True via fake; «x» last_seen_id 1\n"
                                          f"Error: RuntimeError: unreachable — переподключаюсь через 600 с\n", encoding="utf-8")
        os.utime(self.d["stdout"], (self.now - 60, self.now - 60))
        mprocs, env = self._monitor()
        s = self._collect([self._listener(ct=self.now - 3 * 3600)] + mprocs, env)
        codes = {a["code"]: a["level"] for a in s["alerts"]}
        self.assertEqual(codes.get("heartbeat_stale"), "red")
        self.assertEqual(codes.get("connection_lost"), "red")
        self.assertTrue(s["connection"]["since_approx"])

    def test_unanswered_alert_and_reply_journal(self):
        old = datetime.fromtimestamp(self.now - 45 * 60).strftime("%Y-%m-%d %H:%M")
        Path(self.d["log"]).write_text(json.dumps({"chat_id": CHAT, "id": 501, "date": old, "text": "x"}).join(["NEW ", "\n"]),
                                       encoding="utf-8")
        Path(self.d["replies"]).write_text(json.dumps({"kind": "reply", "chat_id": CHAT, "msg_id": 400,
                                                       "ts": self.now - 7200}) + "\n", encoding="utf-8")
        mprocs, env = self._monitor()
        s = self._collect([self._listener()] + mprocs, env)
        self.assertEqual(s["pending"]["count"], 1)
        self.assertIn("unanswered", [a["code"] for a in s["alerts"]])
        with open(self.d["replies"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "reply", "chat_id": CHAT, "msg_id": 502, "ts": self.now - 60}) + "\n")
        s = self._collect([self._listener()] + mprocs, env)
        self.assertEqual(s["pending"]["count"], 0)
        self.assertNotIn("unanswered", [a["code"] for a in s["alerts"]])

    def test_archive_outgoing_and_stale_dump(self):
        ad = self.env.dir / "archive" / f"{CHAT}-test"
        ad.mkdir()
        (ad / "manifest.json").write_text(json.dumps({"chat_id": CHAT, "updated_ts": self.now - 5 * 3600, "messages": 3}),
                                          encoding="utf-8")
        (ad / "dump.json").write_text(json.dumps({"messages": [{"id": 499, "outgoing": True}, {"id": 501},
                                                               {"id": 503, "outgoing": True}]}), encoding="utf-8")
        Path(self.d["log"]).write_text(json.dumps({"chat_id": CHAT, "id": 501, "date": "2026-01-01 10:00"}).join(["NEW ", "\n"]),
                                       encoding="utf-8")
        mprocs, env = self._monitor()
        st = status.collect(self.data, FakeSnap([self._listener()] + mprocs, env=env), now=self.now)
        s = st["duties"]["test"]
        self.assertEqual(s["pending"]["count"], 0)          # исходящий 503 в дампе после входящего 501
        self.assertIn("archive", s["pending"]["sources"])
        self.assertIn("archive_stale", [a["code"] for a in s["alerts"]])
        self.assertIn("archive_stale", [a["code"] for a in st["alerts"]])

    def test_unregistered_listener_found(self):
        other = self._listener(3001, state=str(self.ws / "notes" / "other_watch_state.json"))
        st = status.collect(self.data, FakeSnap([other]), now=self.now)
        self.assertEqual(len(st["unregistered"]), 1)
        self.assertEqual(st["unregistered"][0]["suggested_name"], "other")
        self.assertIn("unregistered", [a["code"] for a in st["alerts"]])

    def test_relative_state_resolved_by_cwd(self):
        lst = self._listener()
        lst.argv = [a if a != self.d["state"] else r"notes\t_state.json" for a in lst.argv]
        mprocs, env = self._monitor()
        snap = FakeSnap([lst] + mprocs, env=env, cwd={1001: str(self.ws)})
        s = status.collect(self.data, snap, now=self.now)["duties"]["test"]
        self.assertEqual(s["listener"]["state"], "alive")

    def test_marker_behind_counts_batches(self):
        with open(self.d["stdout"], "a", encoding="utf-8") as f:
            f.write("BATCH 1 новых сообщений в «Тестовый чат» (ids 600–600), обработать вместе\n")
        status.marker_path("test").write_text("3", encoding="utf-8")  # агент дочитал 3 строки из 4
        s = self._collect([self._listener()])
        self.assertEqual(s["agent"]["marker"]["behind_batches"], 1)
        self.assertIn("undelivered", [a["code"] for a in s["alerts"]])

    def test_inactive_duty_has_no_alerts(self):
        self.d["active"] = False
        s = self._collect([])
        self.assertEqual(s["level"], "grey")
        self.assertEqual(s["alerts"], [])

    def test_state_both_formats(self):
        Path(self.d["state"]).write_text(json.dumps({"chats": {str(CHAT): {"title": "Из state", "last_seen_id": 777}}}),
                                         encoding="utf-8")
        self.d["chats"][0]["title"] = None
        s = self._collect([])
        self.assertEqual((s["chats"][0]["title"], s["chats"][0]["last_seen_id"]), ("Из state", 777))
        legacy = self.ws / "notes" / "legacy_state.json"
        legacy.write_text(json.dumps({"chat_id": -1001234, "chat_title": "Старый", "last_seen_id": 5}), encoding="utf-8")
        self.assertEqual(status.read_state(legacy), {1234: {"title": "Старый", "last_seen_id": 5, "last_emit_at": None}})

    def test_watchers_basename_match_only_if_unique(self):
        pats = status.log_patterns(self.data)
        self.assertIn("t_out.log", pats["test"])
        self.data["duties"]["test2"] = dict(self.d, name="test2", stdout=str(self.env.dir / "other" / "t_out.log"))
        pats = status.log_patterns(self.data)
        self.assertNotIn("t_out.log", pats["test"])


if __name__ == "__main__":
    unittest.main()
