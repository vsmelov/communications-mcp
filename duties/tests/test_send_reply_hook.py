"""Аддитивный хук журнала в tools/send_reply.py: пишет метаданные ответа в журнал дежурства и никогда не падает.
Ничего не отправляется — вызывается только _journal_reply с фейковым «отправленным сообщением»."""
import importlib.util
import json
import types
import unittest
from pathlib import Path

from duties import config, control, registry

from .helpers import TempEnv


def _load_send_reply():
    spec = importlib.util.spec_from_file_location("send_reply_under_test", config.SEND_REPLY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # только импорт: main() не вызывается, в сеть не ходим
    return mod


class SendReplyHookTest(unittest.TestCase):
    def test_hook_writes_duty_journal(self):
        try:
            mod = _load_send_reply()
        except ImportError as e:  # нет Telethon в этом интерпретаторе
            self.skipTest(f"send_reply.py не импортируется: {e}")
        with TempEnv() as t:
            control.add(name="hooktest", chat="444000", workspace=str(t.dir / "ws"))
            args = types.SimpleNamespace(chat="444000", reply_to=12, file=None, no_sign=False)
            msg = types.SimpleNamespace(id=4321, chat_id=444000)
            mod._journal_reply(args, msg, 77)
            d = registry.get(registry.load(), "hooktest")
            rec = json.loads(Path(d["replies"]).read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual((rec["kind"], rec["msg_id"], rec["reply_to"], rec["chars"], rec["duty"]),
                             ("reply", 4321, 12, 77, "hooktest"))
            # сломанный реестр / объект без полей — хук молча ничего не роняет
            Path(config.registry_path()).write_text("{не json", encoding="utf-8")
            mod._journal_reply(args, types.SimpleNamespace(id=1), 1)
            mod._journal_reply(None, None, 0)


if __name__ == "__main__":
    unittest.main()
