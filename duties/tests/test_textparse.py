"""Разбор командных строк, stdout слушателя, журналов — на синтетике (id чатов и тексты выдуманы)."""
import json
import time
import unittest
from datetime import datetime, timedelta

from duties import textparse as tp


class PathsTest(unittest.TestCase):
    def test_norm_path_forms(self):
        a = tp.norm_path(r"C:\Users\Test\ws\notes\x_watch_stdout.log")
        self.assertEqual(a, "c:/users/test/ws/notes/x_watch_stdout.log")
        self.assertEqual(tp.norm_path("/c/Users/Test/ws/notes/x_watch_stdout.log"), a)
        self.assertEqual(tp.norm_path("C:/Users/Test//ws/notes/x_watch_stdout.log"), a)
        self.assertEqual(tp.norm_path('"C:\\Users\\Test\\ws\\notes\\x_watch_stdout.log"'), a)

    def test_norm_cmd_finds_msys_path(self):
        cmd = "bash -c \"eval 'L=/c/Users/Test/ws/notes/x_watch_stdout.log; tail -n 0 -f $L'\""
        self.assertIn(tp.norm_path(r"C:\Users\Test\ws\notes\x_watch_stdout.log"), tp.norm_cmd(cmd))
        # «/c/» внутри пути не превращается в диск
        self.assertNotIn("c:/x", tp.norm_cmd("/d/a/c/x"))

    def test_to_msys(self):
        self.assertEqual(tp.to_msys(r"C:\Users\Test\a b\x.log"), "/c/Users/Test/a b/x.log")

    def test_resolve_path(self):
        self.assertEqual(tp.norm_path(tp.resolve_path(r"notes\s.json", r"C:\ws")), "c:/ws/notes/s.json")
        self.assertEqual(tp.norm_path(tp.resolve_path(r"..\other\s.json", r"C:\ws\sub")), "c:/ws/other/s.json")
        self.assertEqual(tp.norm_path(tp.resolve_path("/c/ws/s.json", None)), "c:/ws/s.json")
        self.assertIsNone(tp.resolve_path("notes/s.json", None))


class CmdlineTest(unittest.TestCase):
    CMD_WRAPPER = (r'"C:\Windows\System32\cmd.exe" /c python -u ..\communications-mcp\tools\watch_chat.py '
                   r'--chat 111222 --state notes\t_watch_state.json --log notes\t_incoming.log '
                   r'>> notes\t_watch_stdout.log 2>&1 ')

    def test_split_quotes_and_backslashes(self):
        self.assertEqual(tp.split_cmdline(r'"C:\Program Files\py.exe" -u "a b" c\\d "e\"f"'),
                         [r"C:\Program Files\py.exe", "-u", "a b", r"c\\d", 'e"f'])
        self.assertEqual(tp.split_cmdline('x "" y'), ["x", "", "y"])

    def test_listener_from_cmd_wrapper(self):
        argv = tp.split_cmdline(self.CMD_WRAPPER)
        a = tp.parse_listener_argv(argv)
        self.assertEqual(a["chats"], ["111222"])
        self.assertEqual(a["state"], r"notes\t_watch_state.json")
        self.assertEqual(a["log"], r"notes\t_incoming.log")
        self.assertEqual(tp.parse_stdout_redirect(self.CMD_WRAPPER), r"notes\t_watch_stdout.log")

    def test_listener_python_argv_multi_chat_and_equals(self):
        argv = [r"C:\Python312\python.exe", "C:/x/communications-mcp/tools/watch_chat.py", "--chat", "111",
                "--chat", "-100222", "--state=notes/s.json", "--log", "notes/l.log", "--quiet", "30",
                "--trigger", "", "--include-bots"]
        a = tp.parse_listener_argv(argv)
        self.assertEqual(a["chats"], ["111", "-100222"])
        self.assertEqual(a["state"], "notes/s.json")
        self.assertEqual(a["quiet"], 30)
        self.assertEqual(a["trigger"], "")
        self.assertTrue(a["include_bots"])

    def test_not_a_listener(self):
        self.assertIsNone(tp.parse_listener_argv(["python", "server.py"]))
        a = tp.parse_listener_argv(["python", "watch_chat.py", "--print-filter"])
        self.assertTrue(a["print_filter"])

    def test_custom_script_name(self):
        a = tp.parse_listener_argv(["python", "tests/fake_listener.py", "--state", "s.json"],
                                   scripts=("watch_chat.py", "fake_listener.py"))
        self.assertEqual(a["state"], "s.json")

    def test_redirect_quoted_and_absent(self):
        self.assertEqual(tp.parse_stdout_redirect('cmd /c "py x.py >> "C:\\a b\\out.log" 2>&1"'), r"C:\a b\out.log")
        self.assertIsNone(tp.parse_stdout_redirect("python watch_chat.py --chat 1 2>&1"))

    def test_shell_wrapper(self):
        self.assertTrue(tp.is_shell_wrapper('"C:\\Program Files\\Git\\bin\\bash.exe" -c "source x && eval \'tail\'"'))
        self.assertFalse(tp.is_shell_wrapper('"C:\\Program Files\\Git\\usr\\bin\\bash.exe" tools/watch_health.sh'))


def _stdout_sample():
    return [
        "READY connected via http://127.0.0.1:1080 at 23:50:00",
        "READY chat resolved: Тестовый чат (id 111222)",
        "LISTENING 1 chat(s), пачки после 0 с тишины; отвечать через tools/send_reply.py",
        "BATCH 2 новых сообщений в «Тестовый чат» (ids 501–502), из них 1 — вызов владельца /ai, обработать вместе",
        'NEW {"chat_id": 111222, "chat": "Тестовый чат", "id": 501, "from": "Тест", "date": "2026-01-01 23:55", "text": "секрет", "media": null, "file": null}',
        'NEW {"chat_id": 111222, "chat": "Тестовый чат", "id": 502, "from": "владелец", "date": "2026-01-01 23:56", "text": "/ai сделай", "media": "MessageMediaDocument", "file": null, "owner_call": true}',
        "ALIVE 00:00:00 connected=True via http://127.0.0.1:1080; «Тестовый чат» last_seen_id 502",
    ]


class StdoutTest(unittest.TestCase):
    def test_classify(self):
        k, d = tp.classify_line(_stdout_sample()[3])
        self.assertEqual(k, "BATCH")
        self.assertEqual((d["count"], d["first"], d["last"], d["owner_calls"]), (2, 501, 502, 1))
        self.assertEqual(tp.classify_line("ConnectionError: связь потеряна в 01:02:03, Telethon переподключается")[0], "CONN_LOST")
        self.assertEqual(tp.classify_line("Error: RuntimeError: unreachable — переподключаюсь через 60 с")[0], "ERROR")
        self.assertEqual(tp.classify_line("Traceback (most recent call last):")[0], "TRACEBACK")
        self.assertEqual(tp.classify_line("ACK failed for 5: RPCError: x")[0], "ACK_FAILED")
        self.assertEqual(tp.classify_line("ALIVE 01:02:03 connected=False via direct; «x» last_seen_id 1")[0], "ALIVE_DOWN")

    def test_resolve_times_over_midnight(self):
        mtime = datetime(2026, 1, 2, 0, 5).timestamp()
        ts = tp.resolve_times(["23:50:00", None, "00:00:00", None], mtime)
        self.assertEqual(datetime.fromtimestamp(ts[0]), datetime(2026, 1, 1, 23, 50))
        self.assertEqual(datetime.fromtimestamp(ts[2]), datetime(2026, 1, 2, 0, 0))
        self.assertEqual(ts[3], mtime)
        self.assertIsNone(ts[1])

    def test_parse_online_and_heartbeat(self):
        mtime = datetime(2026, 1, 2, 0, 1).timestamp()
        p = tp.parse_stdout(_stdout_sample(), mtime)
        self.assertEqual(p["connection"]["state"], "online")
        self.assertEqual(p["connection"]["via"], "http://127.0.0.1:1080")
        self.assertEqual(datetime.fromtimestamp(p["heartbeat_ts"]), datetime(2026, 1, 2, 0, 0))
        self.assertTrue(p["has_alive"])
        self.assertEqual(len(p["batches"]), 1)
        self.assertEqual(p["resolved_chats"], {111222: "Тестовый чат"})
        self.assertEqual(p["chats_seen"], {"Тестовый чат": 502})

    def test_parse_offline_after_error(self):
        lines = _stdout_sample() + ["ConnectionError: связь потеряна в 00:03:00, Telethon переподключается",
                                    "Error: RuntimeError: unreachable: direct: ConnectionError — переподключаюсь через 60 с"]
        mtime = datetime(2026, 1, 2, 0, 9).timestamp()
        p = tp.parse_stdout(lines, mtime)
        self.assertEqual(p["connection"]["state"], "offline")
        self.assertEqual(datetime.fromtimestamp(p["connection"]["since"]), datetime(2026, 1, 2, 0, 3))
        self.assertIn("unreachable", p["connection"]["detail"])
        # пульс — последняя здоровая строка (ALIVE 00:00), а не ошибка
        self.assertEqual(datetime.fromtimestamp(p["heartbeat_ts"]), datetime(2026, 1, 2, 0, 0))
        self.assertEqual(p["errors"], 2)

    def test_reconnect_returns_online(self):
        lines = ["ConnectionError: связь потеряна в 10:00:00, Telethon переподключается", "READY reconnected at 10:01:00"]
        p = tp.parse_stdout(lines, datetime(2026, 1, 1, 10, 2).timestamp())
        self.assertEqual(p["connection"]["state"], "online")

    def test_only_errors_no_heartbeat(self):
        p = tp.parse_stdout(["Error: RuntimeError: unreachable"] * 3, time.time())
        self.assertIsNone(p["heartbeat_ts"])
        self.assertFalse(p["has_alive"])
        self.assertEqual(p["connection"]["state"], "offline")

    def test_mask_line_hides_text(self):
        line = _stdout_sample()[4]
        masked = tp.mask_line(line)
        self.assertNotIn("секрет", masked)
        self.assertIn("id 501", masked)
        self.assertIn("текст скрыт (6 симв.)", masked)
        self.assertEqual(tp.mask_line(line, show_text=True), line)
        self.assertEqual(tp.mask_line("ALIVE 00:00:00 connected=True via x;"), "ALIVE 00:00:00 connected=True via x;")


class JournalTest(unittest.TestCase):
    def test_parse_incoming_without_text(self):
        recs = tp.parse_incoming(_stdout_sample())
        self.assertEqual([r["id"] for r in recs], [501, 502])
        self.assertTrue(recs[1]["owner_call"])
        self.assertNotIn("text", recs[0])
        legacy = tp.parse_incoming(['NEW {"id": 7, "date": "2026-01-01 10:00", "text": "x"}'], default_chat_id=5)
        self.assertEqual(legacy[0]["chat_id"], 5)

    def test_chat_key(self):
        self.assertEqual(tp.chat_key(-1001234567890), 1234567890)
        self.assertEqual(tp.chat_key(-4567), 4567)
        self.assertEqual(tp.chat_key("111"), 111)
        self.assertIsNone(tp.chat_key("@user"))

    def _inc(self, now):
        return [{"chat_id": 111, "id": 10, "ts": now - 3600, "date": None, "owner_call": False},
                {"chat_id": 111, "id": 11, "ts": now - 1800, "date": None, "owner_call": False}]

    def test_pending_by_reply_id(self):
        now = time.time()
        p = tp.compute_pending(self._inc(now), [{"kind": "reply", "chat_id": 111, "msg_id": 12, "ts": now - 600}], now=now)
        self.assertEqual(p["count"], 0)
        self.assertEqual(p["sources"], ["journal"])
        p = tp.compute_pending(self._inc(now), [{"kind": "reply", "chat_id": 111, "msg_id": 9, "ts": now - 7200}], now=now)
        self.assertEqual(p["count"], 2)
        self.assertEqual(p["oldest_min"], 60)

    def test_pending_handled_and_unknown(self):
        now = time.time()
        p = tp.compute_pending(self._inc(now), [{"kind": "handled", "chat_id": None, "up_to_id": 11}], now=now)
        self.assertEqual(p["count"], 0)
        p = tp.compute_pending(self._inc(now), [], now=now)
        self.assertFalse(p["known"])
        self.assertEqual(p["count"], 0)

    def test_pending_drafts_are_approx(self):
        now = time.time()
        p = tp.compute_pending(self._inc(now), [], drafts_ts=now - 2700, now=now)
        self.assertEqual(p["count"], 1)
        self.assertTrue(p["approx"])

    def test_pending_archive_outgoing(self):
        now = time.time()
        p = tp.compute_pending(self._inc(now), [], archive_out={111: 10}, now=now)
        self.assertEqual(p["ids"], [11])
        self.assertEqual(p["sources"], ["archive"])

    def test_parse_jsonl_skips_garbage(self):
        self.assertEqual(tp.parse_jsonl(['{"a": 1}', "мусор", "", json.dumps([1])]), [{"a": 1}])

    def test_local_date(self):
        ts = tp.parse_local_date("2026-01-01 10:00")
        self.assertEqual(datetime.fromtimestamp(ts), datetime(2026, 1, 1, 10, 0))
        self.assertIsNone(tp.parse_local_date("вчера"))
        self.assertLess(abs(tp.parse_local_date((datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"))
                            - (time.time() - 300)), 61)


if __name__ == "__main__":
    unittest.main()
