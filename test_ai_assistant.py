"""HORIZON AI assistant (AI Operations Centre): /api/assistant/status and /api/assistant/ask, offline.
No real key and no network: the Claude call is replaced by a fake client that records what would be sent."""
import asyncio
import json
import os
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BUNCHING_DB", os.path.join(_TMP, "t_ai_assistant.db"))
import app                      # noqa: E402
import checkpoint_ai as cpa     # noqa: E402


class FakeReq:
    def __init__(self, body, host="10.0.0.1", raw=None):
        self._body, self._raw = body, raw
        self.cookies = {}
        self.client = type("C", (), {"host": host})()

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, status=200, data=None):
        self.calls, self.status = [], status
        self.data = data if data is not None else {"content": [{"type": "text", "text": "Service 156 is loading at Clementi Ave 2.\nWhy:\n- 3 buses within 4 min"}]}

    async def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return FakeResp(self.status, self.data)


def run(coro):
    r = asyncio.run(coro)
    if isinstance(r, dict):
        return 200, r
    body = r.body
    if isinstance(body, (bytes, bytearray)):
        body = json.loads(body)
    elif isinstance(body, str):
        body = json.loads(body)
    return r.status_code, body


CTX = {"mode": "demo", "updated": "2026-11-08T10:00:00+08:00", "kpis": {"alerts": 3},
       "alerts": [{"id": f"a{i}", "svc": "156", "category": "loading", "loc": "Clementi Ave 2", "score": 90 - i} for i in range(50)],
       "recommendations": [{"text": f"r{i}"} for i in range(20)],
       "services": [{"svc": str(100 + i), "sev": i} for i in range(100)],
       "routes": [[1.3, 103.8]] * 5000, "secret_field": "not sent on"}


class Assistant(unittest.TestCase):
    def setUp(self):
        self.key, self.client = app.DV_AI_KEY, app._client
        self.cap, self.per = app.ASSIST_CAP, app.ASSIST_PER_MIN
        app.ASSIST.update(day="", n=0, who={})
        cpa._CACHE.clear()

    def tearDown(self):
        app.DV_AI_KEY, app._client = self.key, self.client
        app.ASSIST_CAP, app.ASSIST_PER_MIN = self.cap, self.per
        cpa._CACHE.clear()

    def ask(self, body, **kw):
        return run(app.assistant_ask(FakeReq(body, **kw)))

    def test_status_without_key(self):
        app.DV_AI_KEY = None
        d = asyncio.run(app.assistant_status())
        self.assertFalse(d["configured"])
        self.assertIsNone(d["model"])
        self.assertEqual(set(d["languages"]), {"en", "zh", "ms"})
        self.assertEqual(d["used_today"], 0)

    def test_ask_without_key_says_where_to_add_it_and_calls_nothing(self):
        app.DV_AI_KEY = None
        fake = app._client = FakeClient()
        st, d = self.ask({"q": "Why is 156 bunching?"})
        self.assertEqual(st, 200)
        self.assertFalse(d["ok"])
        self.assertFalse(d["configured"])
        self.assertIn("ANTHROPIC_API_KEY", d["error"])
        self.assertEqual(fake.calls, [])

    def test_validation(self):
        app.DV_AI_KEY = "test-key"
        app._client = FakeClient()
        self.assertEqual(self.ask({"q": "   "})[0], 400)
        self.assertEqual(self.ask(None, raw="not json")[0], 400)
        self.assertEqual(self.ask(None, raw="[1,2]")[0], 400)

    def test_language_prompt_snapshot_and_reply(self):
        app.DV_AI_KEY = "test-key"
        fake = app._client = FakeClient()
        hist = [{"role": "assistant", "text": "hello"}, {"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"},
                {"role": "assistant", "text": "dup"}, {"role": "user", "text": "q2"}, {"role": "bogus", "text": "x"}]
        st, d = self.ask({"q": "Kenapa 156 sesak?  " + "x" * 900, "lang": "ms", "history": hist, "ctx": CTX})
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertIn("156", d["answer"])
        self.assertEqual((d["lang"], d["mode"], d["alerts_used"]), ("ms", "demo", 30))
        self.assertIn("Advisory only", d["note"])
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "test-key")
        sent = call["json"]
        self.assertIn("Bahasa Melayu", sent["system"])
        self.assertIn("Advisory only", sent["system"])
        self.assertEqual(sent["max_tokens"], 700)
        roles = [m["role"] for m in sent["messages"]]
        self.assertEqual(roles[0], "user")
        self.assertEqual(roles[-1], "user")
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)       # alternating, as the API needs
        last = sent["messages"][-1]["content"]
        self.assertTrue(last.startswith("LIVE DATA SNAPSHOT (JSON):"))
        q = last.split("QUESTION: ", 1)[1]
        self.assertLessEqual(len(q), 600)
        snap = json.loads(last.split("\n", 1)[1].split("\n\nQUESTION:", 1)[0])
        self.assertEqual((len(snap["alerts"]), len(snap["recommendations"]), len(snap["services"])), (30, 12, 40))
        self.assertNotIn("routes", snap)
        self.assertNotIn("secret_field", snap)
        self.assertIn("now_sg", snap)

    def test_each_language(self):
        app.DV_AI_KEY = "test-key"
        for lang, word in (("en", "English"), ("zh", "简体中文"), ("ms", "Malay"), ("xx", "English")):
            app.ASSIST.update(day="", n=0, who={})
            fake = app._client = FakeClient()
            st, d = self.ask({"q": "status?", "lang": lang})
            self.assertTrue(d["ok"], lang)
            self.assertIn(word, fake.calls[0]["json"]["system"], lang)

    def test_big_snapshot_is_trimmed(self):
        ctx = {"mode": "live", "alerts": [{"t": "a" * 3000}] * 30, "services": [{"t": "s" * 3000}] * 40}
        snap, text = app.assistant_snapshot(ctx)
        self.assertLessEqual(len(text), 60000)
        self.assertTrue(snap["alerts"])

    def test_recent_checkpoint_readings_are_included_old_ones_not(self):
        cid = sorted(cpa.CHECKPOINT_IDS)[0]
        other = sorted(cpa.CHECKPOINT_IDS)[1]
        cpa._CACHE[cid] = (time.time() - 120, {"level": "Jam", "to_jb": "Jam", "to_sg": "Smooth", "source": "AI camera analytics"})
        cpa._CACHE[other] = (time.time() - 7200, {"level": "Super Jam", "to_jb": "Super Jam", "to_sg": None})
        cpa._CACHE["6710"] = (time.time(), {"level": "Jam"})                  # not a checkpoint camera
        snap, _ = app.assistant_snapshot({})
        cams = snap["checkpoints"]["cameras"]
        self.assertEqual(list(cams), [cid])
        self.assertEqual(cams[cid]["age_min"], 2)
        self.assertTrue(snap["checkpoints"]["crossings"])
        cpa._CACHE.clear()
        self.assertNotIn("checkpoints", app.assistant_snapshot({})[0])

    def test_per_minute_limit_and_daily_cap(self):
        app.DV_AI_KEY = "test-key"
        app._client = FakeClient()
        app.ASSIST_PER_MIN = 3
        codes = [self.ask({"q": "hi"}, host="1.1.1.1")[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(self.ask({"q": "hi"}, host="2.2.2.2")[0], 200)           # another person is not blocked
        app.ASSIST_CAP = app.ASSIST["n"]
        st, d = self.ask({"q": "hi"}, host="3.3.3.3")
        self.assertEqual(st, 429)
        self.assertIn("ASSISTANT_DAILY_CAP", d["error"])
        self.assertEqual(asyncio.run(app.assistant_status())["used_today"], app.ASSIST["n"])

    def test_ai_errors_are_reported_not_raised(self):
        app.DV_AI_KEY = "test-key"
        app._client = FakeClient(status=529, data={"error": {"message": "Overloaded"}})
        st, d = self.ask({"q": "hi"})
        self.assertFalse(d["ok"])
        self.assertIn("Overloaded", d["error"])

        class Boom:
            async def post(self, *a, **k):
                raise TimeoutError()
        app._client = Boom()
        st, d = self.ask({"q": "hi again"})
        self.assertFalse(d["ok"])
        self.assertIn("TimeoutError", d["error"])


if __name__ == "__main__":
    unittest.main()
