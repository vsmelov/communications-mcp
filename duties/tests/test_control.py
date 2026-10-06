"""Старт/стоп/перезапуск/заведение/журналы на ТЕСТОВОМ дежурстве: фейковый слушатель без сети, временный
реестр и воркспейс. Реальные слушатели и их файлы не трогаются (их state-файлов во временном реестре нет,
а стоп убивает только процессы с state этого дежурства — это и проверяется)."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from duties import config, control, journal, registry, status
from duties.procs import take_snapshot, terminate
from duties.registry import DutyError

from .helpers import FAKE_LISTENER, TempEnv

CHAT = "777000"


def _wait(pred, timeout=10.0, step=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


@unittest.skipUnless(sys.platform == "win32", "управление процессами — только Windows")
class ControlTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv().__enter__()
        self.ws = self.env.dir / "ws-test"
        self.extra = None
        res = control.add(name="testduty", chat=CHAT, workspace=str(self.ws), script=str(FAKE_LISTENER))
        self.assertTrue(res["ok"])
        # пульс раз в 0.4 с, пачка каждые 3 пульса, предохранитель 120 с
        registry.update(lambda d: d["duties"]["testduty"].__setitem__(
            "extra_args", ["--alive-sec", "0.4", "--batch-every", "3", "--max-sec", "120"]))

    def tearDown(self):
        try:
            control.stop("testduty")
        except Exception:  # noqa: BLE001
            pass
        if self.extra is not None:
            terminate(self.extra.pid)
            try:
                self.extra.wait(5)
            except subprocess.TimeoutExpired:
                self.extra.kill()
        self.env.__exit__(None, None, None)

    def _status(self):
        return status.collect(names=["testduty"])["duties"]["testduty"]

    def test_add_creates_workspace_and_readme(self):
        readme = (self.ws / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("{{", readme)
        self.assertIn("duty_monitor_command(\"testduty\")", readme)
        self.assertIn("Правила ответа", readme)
        self.assertIn("только через владельца", readme)
        self.assertTrue((self.ws / "notes").is_dir())
        d = registry.get(registry.load(), "testduty")
        self.assertTrue(d["monitor_command"])
        with self.assertRaises(DutyError):
            control.add(name="testduty", chat=CHAT, workspace=str(self.ws))
        with self.assertRaises(DutyError):
            control.add(name="Плохое имя", chat=CHAT)

    def test_start_dup_stop_restart(self):
        r = control.start("testduty")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["pid"])
        s = _wait(lambda: (lambda s: s if s["heartbeat"]["level"] == "green" else None)(self._status()))
        self.assertIsNotNone(s, "пульс не появился")
        self.assertEqual(s["listener"]["state"], "alive")
        self.assertEqual(s["connection"]["state"], "online")
        self.assertEqual(s["connection"]["via"], "fake")
        # переменные сессии Claude не наследуются: слушатель помечен тем, кто его запустил (здесь — CLI тестов)
        self.assertEqual(s["listener"]["started_by"], "local")
        self.assertEqual(s["chats"][0]["last_seen_id"] is not None, True)
        # дубль не запускается
        r2 = control.start("testduty")
        self.assertFalse(r2["ok"])
        self.assertIn("уже запущен", r2["error"])
        self.assertEqual(self._status()["listener"]["count"], 1)
        # хвост: тексты скрыты по умолчанию
        _wait(lambda: any(ln.startswith("NEW ") for ln in control.tail("testduty", 50)["lines"]))
        t = control.tail("testduty", 50)
        news = [ln for ln in t["lines"] if ln.startswith("NEW ")]
        self.assertTrue(news and all("текст скрыт" in ln and "синтетика" not in ln for ln in news))
        self.assertTrue(any("синтетика" in ln for ln in control.tail("testduty", 50, show_text=True)["lines"]))
        # стоп
        st = control.stop("testduty")
        self.assertTrue(st["ok"], st)
        self.assertEqual(self._status()["listener"]["state"], "dead")
        for k in ("state", "log", "stdout"):
            self.assertTrue(Path(registry.get(registry.load(), "testduty")[k]).exists(), k)
        # перезапуск из остановленного = старт
        rr = control.restart("testduty")
        self.assertTrue(rr["ok"], rr)
        self.assertEqual(self._status()["listener"]["state"], "alive")
        # журнал действий: отказ в дубле и стоп незапущенного (внутри restart) не пишутся
        acts = [json.loads(x)["action"] for x in (config.runtime_dir() / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(acts, ["add", "start", "stop", "start"])

    def test_stop_does_not_touch_foreign_listener(self):
        other_state = self.env.dir / "other" / "other_watch_state.json"
        other_state.parent.mkdir()
        self.extra = subprocess.Popen([config.default_python(), "-u", str(FAKE_LISTENER), "--chat", "1", "--state",
                                       str(other_state), "--log", str(other_state.parent / "in.log"), "--max-sec", "60"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=0x08000000)
        self.assertTrue(_wait(lambda: any(self.extra.pid in f["pids"] for f in status.collect()["unregistered"])))
        self.assertTrue(control.start("testduty")["ok"])
        self.assertTrue(control.stop("testduty")["ok"])
        self.assertIsNone(self.extra.poll(), "чужой слушатель не должен был остановиться")
        snap = take_snapshot()
        self.assertTrue(snap.alive(self.extra.pid))

    def test_spawn_does_not_hang_on_busy_stdin(self):
        """Регрессия 05.10.2026: MCP-клиент создаёт stdin сервера асинхронным (overlapped) пайпом, сервер читает его
        в соседнем потоке; дочерний процесс, унаследовавший этот stdin, вешал запуск на 30 с. Тот же расклад здесь:
        родитель — asyncio-подпроцесс с пайпами, ребёнок читает stdin в потоке и зовёт spawn_detached."""
        import asyncio
        child = (
            "import sys, threading, time; sys.path.insert(0, %r)\n"
            "threading.Thread(target=lambda: sys.stdin.buffer.readline(), daemon=True).start(); time.sleep(0.3)\n"
            "from duties.procs import spawn_detached\n"
            "t = time.time(); spawn_detached('cmd.exe /d /s /c \"exit 0\"', '.'); print(round(time.time() - t, 2), flush=True)\n"
        ) % str(config.ROOT)

        async def run():
            p = await asyncio.create_subprocess_exec(
                config.default_python(), "-c", child, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000)
            try:
                return await asyncio.wait_for(p.stdout.readline(), 25)
            except asyncio.TimeoutError:
                return None
            finally:
                p.kill()
                await p.wait()

        out = asyncio.run(run())
        self.assertTrue(out, "spawn_detached завис при занятом stdin")
        self.assertLess(float(out.decode().strip()), 10)

    def test_monitor_command(self):
        mc = control.monitor_command("testduty")
        cmd = mc["command"]
        self.assertEqual(mc["timeout_ms"], 1_800_000)
        self.assertIn("tail --pid=$$", cmd)
        self.assertIn("/^NEW /", cmd)
        d = registry.get(registry.load(), "testduty")
        from duties.textparse import to_msys
        self.assertIn(to_msys(d["stdout"]), cmd)
        self.assertIn("testduty.monitor_line", cmd)
        self.assertNotIn("\\", cmd.replace("\\'", ""))  # только прямые слэши — Bash-тул съедает обратные
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        if Path(bash).exists():
            r = subprocess.run([bash, "-n", "-c", cmd], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_journal_handled_attach_remove(self):
        d = registry.get(registry.load(), "testduty")
        ten_min_ago = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 600))
        Path(d["log"]).write_text("NEW " + json.dumps({"chat_id": int(CHAT), "id": 900, "date": ten_min_ago,
                                                      "text": "x"}) + "\n", encoding="utf-8")
        self.assertEqual(self._status()["pending"]["known"], False)
        p = journal.record_reply(chat=CHAT, chat_id=int(CHAT), msg_id=850, chars=10)
        self.assertEqual(Path(p), Path(d["replies"]))
        rec = json.loads(Path(d["replies"]).read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((rec["kind"], rec["msg_id"], rec["duty"]), ("reply", 850, "testduty"))
        self.assertNotIn("text", rec)
        # ответ час назад (id 850), входящее 10 мин назад (id 900) — ждёт ответа
        rec["ts"] = time.time() - 3600
        Path(d["replies"]).write_text(json.dumps(rec) + "\n", encoding="utf-8")
        self.assertEqual(self._status()["pending"]["count"], 1)
        h = control.handled("testduty")
        self.assertEqual(h["up_to_id"], 900)
        self.assertEqual(self._status()["pending"]["count"], 0)
        other = journal.record_reply(chat="@someone", msg_id=1)
        self.assertEqual(Path(other).name, "replies_other.jsonl")
        # привязка агента из окружения сессии
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "local_00000000-0000-4000-8000-0000000000bb"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "00000000-0000-4000-8000-0000000000cc"
        a = control.attach("testduty")
        self.assertEqual(a["agent"]["session_id"], "local_00000000-0000-4000-8000-0000000000bb")
        self.assertTrue(a["agent"]["link"].startswith("claude://claude.ai/epitaxy/local_"))
        # убрать работающее без stop_listener нельзя
        self.assertTrue(control.start("testduty")["ok"])
        with self.assertRaises(DutyError):
            control.remove("testduty")
        r = control.remove("testduty", stop_listener=True)
        self.assertTrue(r["ok"])
        self.assertNotIn("testduty", registry.load()["duties"])
        self.assertTrue((self.ws / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
