"""HTTP-дашборд: статика, API, защита Host/Origin/Content-Type — на временном реестре, сервер в потоке."""
import json
import threading
import unittest
from http.client import HTTPConnection

from duties import dashboard, registry

from .helpers import TempEnv


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv().__enter__()
        cls.httpd = dashboard.Server(("127.0.0.1", 0), dashboard.Handler)
        cls.httpd.cache = dashboard._StateCache()
        cls.httpd.archiver = type("A", (), {"state": {"ok": None, "checked_at": None, "url": "http://127.0.0.1:9/"}})()
        cls.httpd.verbose = False
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.__exit__(None, None, None)

    def req(self, method, path, body=None, headers=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r, raw

    def test_static_and_security_headers(self):
        r, raw = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIn("Дежурства".encode("utf-8"), raw)
        for p in ("/static/app.js", "/static/app.css"):
            self.assertEqual(self.req("GET", p)[0].status, 200)
        self.assertNotIn(b"https://", self.req("GET", "/static/app.js")[1])  # без внешних CDN

    def test_ping_and_state(self):
        r, raw = self.req("GET", "/api/ping")
        self.assertEqual(json.loads(raw)["app"], "claude-duties-dashboard")
        r, raw = self.req("GET", "/api/state")
        self.assertEqual(r.status, 200)
        st = json.loads(raw)
        self.assertIn("summary", st)
        self.assertIn("duties", st)

    def test_bad_host_rejected(self):
        r, _ = self.req("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(r.status, 421)
        r, _ = self.req("POST", "/api/duties", body={}, headers={"Host": "attacker.test"})
        self.assertEqual(r.status, 421)

    def test_post_needs_own_origin_and_json(self):
        r, raw = self.req("POST", "/api/duties/x/start", body={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/duties/x/start", body={}, headers={"Origin": "null"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/duties/x/start", body={}, headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)
        r, _ = self.req("POST", "/api/duties/x/start", body={}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 415)
        r, raw = self.req("POST", "/api/duties/x/start", body={}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(r.status, 400)  # прошло защиту, но такого дежурства нет
        self.assertIn("не найдено", json.loads(raw)["error"])

    def test_put_delete_not_allowed(self):
        self.assertEqual(self.req("PUT", "/api/duties")[0].status, 405)
        self.assertEqual(self.req("DELETE", "/api/duties/x")[0].status, 405)

    def test_add_via_api_and_tail(self):
        ws = self.env.dir / "ws-dash"
        r, raw = self.req("POST", "/api/duties", body={"name": "dashtest", "chat": "555000", "workspace": str(ws),
                                                       "title": "Тест <b>&</b>"},
                          headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(r.status, 200, raw)
        res = json.loads(raw)
        self.assertTrue(res["ok"])
        self.assertIn("command", res["monitor_command"])
        self.assertIn("dashtest", registry.load()["duties"])
        (ws / "notes" / "dashtest_watch_stdout.log").write_text(
            'NEW {"id": 1, "date": "2026-01-01 10:00", "text": "приватное"}\nALIVE 10:00:00 connected=True via x;\n',
            encoding="utf-8")
        r, raw = self.req("GET", "/api/duties/dashtest/tail?lines=10")
        lines = json.loads(raw)["lines"]
        self.assertEqual(len(lines), 2)
        self.assertNotIn("приватное", "\n".join(lines))
        r, raw = self.req("GET", "/api/duties/dashtest/tail?lines=10&text=1")
        self.assertIn("приватное", "\n".join(json.loads(raw)["lines"]))
        r, raw = self.req("GET", "/api/duties/dashtest/monitor-command")
        self.assertIn("tail --pid=$$", json.loads(raw)["command"])
        r, raw = self.req("GET", "/api/state")
        self.assertEqual(json.loads(raw)["duties"]["dashtest"]["title"], "Тест <b>&</b>")  # экранирует уже фронтенд


if __name__ == "__main__":
    unittest.main()
