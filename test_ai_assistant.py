"""HORIZON AI assistant (AI Operations Centre): /api/assistant/status and /api/assistant/ask, offline.
No real key and no network: the Claude call is replaced by a fake client that records what would be sent."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime

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
        system = sent["system"][0]["text"]
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})             # same rules every time: cached
        self.assertIn("Advisory only", system)
        self.assertEqual(sent["max_tokens"], 700)
        roles = [m["role"] for m in sent["messages"]]
        self.assertEqual(roles[0], "user")
        self.assertEqual(roles[-1], "user")
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)       # alternating, as the API needs
        last = sent["messages"][-1]["content"]
        self.assertTrue(last.startswith("Answer language: Bahasa Melayu"))
        q = last.split("QUESTION: ", 1)[1]
        self.assertLessEqual(len(q), 600)
        data = json.loads(last.split("DATA (JSON):\n", 1)[1].split("\n\nQUESTION:", 1)[0])
        snap = data["page"]
        self.assertIn("thresholds", data)
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
            self.assertIn(word, fake.calls[0]["json"]["messages"][-1]["content"].split("\n", 1)[0], lang)

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
        cams = snap["checkpoints"]["cameras"]  # noqa
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


# ---------------------------------------------------------------- HORIZON rules and per-service checks
def fake_row(svc, d, at):
    return {"service": svc, "direction": d, "key": f"{svc}:{d}", "risk": "red" if d == 1 else "green", "score": 70, "n_buses": 6, "sched_hw": 10.0,
            "hw_src": "Service Headway Master (Weekday 06:00-20:00)", "issue": "Headway Gap" if d == 1 else "Normal", "note": None,
            "groups": [], "gaps": [], "seq": [], "bb": None, "trend": "widening" if d == 1 else "stable", "location": "Opp Blk 123",
            "gap": {"now": 21.0, "max_hw": 23.0, "ratio": 2.3, "state": "confirmed", "trend": "widening", "since_stop": "Blk 101 (12345)",
                    "cur_stop": "Opp Blk 123 (12399)", "stops": 17, "active": True} if d == 1 else None,
            "traffic": {"text": "No prolonged slow traffic within 3 km ahead of the leading bus."}, "dist": {"now": [9.0, 21.0, 8.0, 7.0, 10.0] if d == 1 else [10.0, 11.0, 9.0], "pred": []},
            "counts": {"gaps": 1 if d == 1 else 0, "bb": 0, "bb2": 0, "bb3": 0, "bb4": 0, "early": 0}, "stops": 52, "route_km": 21.4, "at": at}


class HorizonRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.bb_init()
        app.rt_init()

    def setUp(self):
        self.saved = {k: getattr(app, k) for k in ("DV_AI_KEY", "_client", "static", "freq_table", "bb_run")}
        self.rows, self.watch, self.master = dict(app.BB["rows"]), dict(app.BB["watch"]), list(app.BB["master"])
        app.ASSIST.update(day="", n=0, who={})
        app.DV_AI_KEY = "test-key"
        self.fake = app._client = FakeClient()
        self.runs = []

        async def static():
            return {"dirs": {"147": [1, 2], "147A": [1], "182M": [1], "2": [1, 2], "3": [1, 2]}}

        async def freq_table():
            return {"freqs": {}}

        async def bb_run(keys):
            self.runs.append(list(keys))
            for svc, d in keys:
                app.BB["rows"][f"{svc}:{d}"] = fake_row(svc, d, time.time())
        app.static, app.freq_table, app.bb_run = static, freq_table, bb_run
        for svc in ("147", "147A"):
            app.bb_sql("DELETE FROM bunching_event WHERE service=?", (svc,))
            app.bb_sql("DELETE FROM gap_event WHERE service=?", (svc,))
            app.bb_sql("DELETE FROM bb_cov WHERE service=?", (svc,))
            app.bb_sql("DELETE FROM rt_trip WHERE service=?", (svc,))
            app.BB["rows"].pop(f"{svc}:1", None)
            app.BB["rows"].pop(f"{svc}:2", None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(app, k, v)
        app.BB["rows"], app.BB["watch"], app.BB["master"] = self.rows, self.watch, self.master

    def sent(self):
        last = self.fake.calls[-1]["json"]["messages"][-1]["content"]
        return json.loads(last.split("DATA (JSON):\n", 1)[1].split("\n\nQUESTION:", 1)[0])

    def test_the_occ_rules_are_the_system_prompt(self):
        txt = app.assistant_system()
        self.assertIn("The absence of an alert is NOT evidence that a service is operating normally.", txt)   # from horizon_ai_prompt.md
        self.assertIn("Data confidence:", txt)
        self.assertIn("observed_min", txt)                                     # the data guide for this deployment
        self.assertIn("Not connected to you: CFMS", txt)

    def test_service_numbers_in_questions(self):
        known = {"147", "147A", "182", "182M", "2", "3", "156"}
        T = app.assistant_targets
        self.assertEqual(T("How was svc 147?", known), ["147"])
        self.assertEqual(T("How is 147A doing", known), ["147A"])
        self.assertEqual(T("Any problem with 182M and 182?", known), ["182M", "182"])
        self.assertEqual(T("147号怎么样？", known), ["147"])
        self.assertEqual(T("Bagaimana bas 147 hari ini?", known), ["147"])
        self.assertEqual(T("hold 3 min at Dir 2 for 2 buses", known), [])          # times, directions and counts are not services
        self.assertEqual(T("How is svc 999?", known), ["999"])                       # named as a service, so it is checked (and reported unknown)
        self.assertEqual(T("Status on 2026-11-08 at 14:51", known), [])

    def test_time_windows(self):
        now = datetime(2026, 11, 8, 14, 51, tzinfo=app.SGT).timestamp()
        W = lambda q: app.assistant_window(q, now)
        self.assertEqual((W("How was 147 yesterday?")["name"], W("How was 147 yesterday?")["live"]), ("yesterday", False))
        self.assertEqual(W("147 昨天怎么样")["name"], "yesterday")
        self.assertEqual(W("How is 147 today?")["name"], "today")
        self.assertEqual(W("147 in the last 3 hours")["name"], "last 3 h")
        w = W("How was 147?")
        self.assertTrue(w["live"])
        self.assertEqual(round((w["end"] - w["start"]) / 60), 60)

    def test_how_was_147_is_measured_not_guessed(self):
        now = time.time()
        app.bb_sql("INSERT INTO gap_event(service,direction,start_ts,end_ts,buses,max_hw,max_ratio,sched_hw,location,start_stop,end_stop,stops,alerts) "
                   "VALUES ('147',1,?,?,'a,b',24,2.4,10,'Opp Blk 123','Blk 101','Opp Blk 123',18,1)", (now - 1500, now - 900))
        app.bb_sql("INSERT INTO bb_cov(hour_ts, service, direction, n, sec) VALUES (?,?,?,?,?)", (int((now - 1800) // 3600) * 3600, "147", 1, 90, 2700))
        app.bb_sql("INSERT INTO rt_trip(service,direction,day_type,date,start_ts,end_ts,rt_min,cover,n_obs,marks) VALUES ('147',1,'Weekday','x',?,?,62.5,0.95,40,'')",
                   (now - 3000, now - 300))
        st, d = run(app.assistant_ask(FakeReq({"q": "How was svc 147?", "lang": "en", "ctx": {"mode": "live", "alerts": [{"svc": "27"}],
                                                                                                    "focus": {"147": {"alerts": [], "services": [], "note": "Not on the Forecast Centre page"}}}})))
        self.assertTrue(d["ok"])
        self.assertEqual(d["services_checked"], ["147"])
        self.assertEqual(sorted(k for r in self.runs for k in r), [("147", 1), ("147", 2)])      # measured on demand
        self.assertIn(("147", 1), app.BB["watch"])                                                # and kept on watch for follow-ups
        data = self.sent()
        c = data["service_checks"][0]
        self.assertTrue(c["known_service"] and c["live_checks"])
        d1, d2 = c["directions"]
        self.assertEqual((d1["live"]["status"], d1["live"]["level"], d1["live"]["buses_tracked"]), ("Headway Gap", "Critical", 6))
        self.assertEqual(d1["live"]["est_headways_min"], [9.0, 21.0, 8.0, 7.0, 10.0])
        self.assertEqual(d1["live"]["worst_gap"]["since_stop"], "Blk 101 (12345)")
        e = d1["live"]["ewt_estimate"]                                                            # (81+441+64+49+100)/(2*55) - 5
        self.assertAlmostEqual(e["ewt_min"], round((81 + 441 + 64 + 49 + 100) / 110 - 5, 1))
        self.assertEqual((d1["events"]["long_headway"], d1["events"]["bunching"]), (1, 0))
        self.assertEqual(d1["events"]["observed_min"], 45)
        self.assertEqual(d2["events"]["observed_min"], 0)                                          # not watched: no evidence either way
        self.assertEqual((d2["live"]["status"], d2["live"]["level"]), ("Normal", "Normal"))
        self.assertEqual(d1["running_time"]["trips_measured"], 1)
        self.assertEqual(c["forecast_centre"]["note"], "Not on the Forecast Centre page")
        self.assertEqual(data["thresholds"]["bunch_min"], app.BB["params"]["bunch_min"])

    def test_unknown_service_and_demo_and_yesterday(self):
        run(app.assistant_ask(FakeReq({"q": "How is svc 999?", "ctx": {"mode": "live"}})))
        self.assertEqual(self.sent()["service_checks"][0], {**self.sent()["service_checks"][0], "known_service": False})
        run(app.assistant_ask(FakeReq({"q": "How is 147?", "ctx": {"mode": "demo"}})))
        c = self.sent()["service_checks"][0]
        self.assertFalse(c["live_checks"])
        self.assertNotIn("live", c["directions"][0])
        self.assertEqual(self.runs, [])                                                           # demo page: no live LTA calls
        run(app.assistant_ask(FakeReq({"q": "How was 147 yesterday?", "ctx": {"mode": "live"}})))
        c = self.sent()["service_checks"][0]
        self.assertEqual((c["window"], c["live_checks"]), ("yesterday", False))
        self.assertEqual(self.runs, [])

    def test_feed_error_is_reported_as_no_data(self):
        async def bb_run(keys):
            for svc, d in keys:
                app.BB["rows"][f"{svc}:{d}"] = {"service": svc, "direction": d, "key": f"{svc}:{d}", "error": "LTA DataMall: HTTP 503"}
        app.bb_run = bb_run
        run(app.assistant_ask(FakeReq({"q": "147 bunching?", "ctx": {"mode": "live"}})))
        live = self.sent()["service_checks"][0]["directions"][0]["live"]
        self.assertEqual(live, {"status": "No data", "error": "LTA DataMall: HTTP 503"})


if __name__ == "__main__":
    unittest.main()
