"""Camera AI analytics: reply parsing, level mapping, crossing times, caching and the speed fallback (offline)."""
import asyncio
import unittest

import checkpoint_ai as cpa

CAMS = [{"id": "2701", "link": "https://img/2701.jpg", "lat": 1.45, "lon": 103.77, "taken": "t1", "desc": "Woodlands Causeway (Towards Johor)"},
        {"id": "2702", "link": "https://img/2702.jpg", "lat": 1.44, "lon": 103.77, "taken": "t1", "desc": "Woodlands Checkpoint"},
        {"id": "6710", "link": "https://img/6710.jpg", "lat": 1.33, "lon": 103.85, "taken": "t1", "desc": "PIE"}]


class Logic(unittest.TestCase):
    def test_levels(self):
        self.assertEqual([cpa.level_from_ratio(r) for r in (1, .8, .6, .3, .1, None)], ["Smooth", "Smooth", "Medium", "Jam", "Super Jam", None])
        self.assertEqual(cpa.norm_level("super jam"), "Super Jam")
        self.assertEqual(cpa.norm_level("heavy congestion"), "Jam")
        self.assertIsNone(cpa.norm_level("unknown"))
        self.assertEqual(cpa.worst(["Smooth", None, "Jam", "Medium"]), "Jam")

    def test_parse_reply_tolerates_prose_and_bad_rows(self):
        text = 'Here you go:\n[{"image":1,"level":"Medium","to_jb":"Jam","to_sg":"Smooth","confidence":0.8},{"image":9,"level":"Jam"},"x",{"image":2,"level":"bogus"}]'
        out = cpa.parse_reply(text, 2)
        self.assertEqual(out[0], {"level": "Medium", "to_jb": "Jam", "to_sg": "Smooth", "confidence": .8})
        self.assertIsNone(out[1]["level"])
        self.assertEqual(cpa.parse_reply("no json", 2), {})
        self.assertEqual(cpa.parse_reply('```json\n{"images":[{"image":1,"level":"Jam"}]}\n```', 1)[0]["level"], "Jam")
        self.assertEqual(cpa.parse_reply('{"1":{"level":"Smooth"},"2":{"level":"Super Jam"}}', 2)[1]["level"], "Super Jam")


class Analyse(unittest.TestCase):
    def setUp(self):
        cpa._CACHE.clear()
        cpa.STATUS.update(day=None, calls_today=0)
        self.only = cpa.AI_ONLY_CHECKPOINTS
        cpa.AI_ONLY_CHECKPOINTS = False                   # these tests send every camera to the AI
        self.calls = 0

    def tearDown(self):
        cpa.AI_ONLY_CHECKPOINTS = self.only

    async def fetch(self, url):
        return b"jpeg", "image/jpeg"

    async def ask(self, content):
        self.calls += 1
        self.assertEqual(sum(1 for c in content if c["type"] == "image"), 3)
        return '[{"image":1,"level":"Jam","to_jb":"Jam","to_sg":null},{"image":2,"level":"Medium","to_jb":"Super Jam","to_sg":"Smooth"},{"image":3,"level":"Smooth"}]'

    def test_ai_gives_crossing_times_per_direction_and_is_cached(self):
        out = asyncio.run(cpa.analyse(CAMS, self.fetch, self.ask, now=1000))
        self.assertEqual(out["cameras"]["6710"]["level"], "Smooth")
        wl = out["crossings"][0]
        self.assertEqual(wl["id"], "woodlands")
        self.assertEqual(wl["to_jb"], {"level": "Super Jam", "minutes": 90})      # worst of the two cameras
        self.assertEqual(wl["to_sg"], {"level": "Smooth", "minutes": 15})
        asyncio.run(cpa.analyse(CAMS, self.fetch, self.ask, now=1100))
        self.assertEqual(self.calls, 1)                                           # same photos -> no second AI call
        asyncio.run(cpa.analyse(CAMS, self.fetch, self.ask, now=1000 + cpa.CACHE_S + 1))
        self.assertEqual(self.calls, 2)

    def test_cost_controls_checkpoints_only_window_cap_and_shared_cache(self):
        cpa.AI_ONLY_CHECKPOINTS = True
        sent = []
        async def ask(content):
            sent.append(sum(1 for c in content if c["type"] == "image"))
            return '[{"image":1,"level":"Jam"},{"image":2,"level":"Jam"}]'
        async def bands(lat, lon):
            return {"level": "Smooth", "source": "LTA speed bands"}
        noon = 1793160000                                           # 2026-10-28 12:00 SGT
        out = asyncio.run(cpa.analyse(CAMS, self.fetch, ask, bands, now=noon))
        self.assertEqual(sent, [2])                                 # only the 2 checkpoint photos went to the AI
        self.assertEqual(out["cameras"]["6710"]["source"], "LTA speed bands")
        asyncio.run(cpa.analyse(CAMS, self.fetch, ask, bands, now=noon + 600))
        self.assertEqual(sent, [2])                                 # new photos within 15 min: no new AI call
        cpa._CACHE.clear()
        night = 1793124000                                          # 02:00 SGT: outside 05-24
        out = asyncio.run(cpa.analyse(CAMS, self.fetch, ask, bands, now=night))
        self.assertEqual(sent, [2])
        self.assertIn("SGT only", out["ai_paused"])
        self.assertEqual(out["cameras"]["2701"]["source"], "LTA speed bands")
        cpa.STATUS.update(calls_today=cpa.AI_DAILY_CAP)
        out = asyncio.run(cpa.analyse(CAMS, self.fetch, ask, bands, now=noon + 3600))
        self.assertIn("limit", out["ai_paused"])
        self.assertEqual(sent, [2])

    def test_without_ai_uses_speed_and_never_guesses(self):
        async def flow(lat, lon):
            return {"cur": 20.0, "free": 60.0, "ratio": 1 / 3, "conf": .9} if lat > 1.4 else None
        out = asyncio.run(cpa.analyse(CAMS, None, None, flow, now=1000))
        self.assertEqual(out["cameras"]["2701"]["level"], "Jam")
        self.assertEqual(out["cameras"]["2701"]["source"], "TomTom speed")
        self.assertNotIn("6710", out["cameras"])
        self.assertEqual(out["crossings"][0]["any"], {"level": "Jam", "minutes": 60})
        self.assertIsNone(out["crossings"][0]["to_jb"])
        self.assertEqual(asyncio.run(cpa.analyse(CAMS, None, None, None, now=1000))["cameras"], {})

    def test_photos_from_s3_are_sent_with_a_real_image_type(self):
        self.assertEqual(cpa.sniff_mime(b"\xff\xd8\xff\xe0rest", "binary/octet-stream"), "image/jpeg")
        self.assertEqual(cpa.sniff_mime(b"\x89PNG\r\n\x1a\nxxxx", ""), "image/png")
        self.assertIsNone(cpa.sniff_mime(b"<html>denied", "text/html"))
        seen = []
        async def fetch(url):
            return b"\xff\xd8\xff\xe0jpeg", "binary/octet-stream"
        async def ask(content):
            seen.extend(c["source"]["media_type"] for c in content if c["type"] == "image")
            return '[{"image":1,"level":"Jam"},{"image":2,"level":"Jam"},{"image":3,"level":"Jam"}]'
        asyncio.run(cpa.analyse(CAMS, fetch, ask, now=1000))
        self.assertEqual(seen, ["image/jpeg"] * 3)
        async def html(url):
            return b"<html>AccessDenied", "text/html"
        out = asyncio.run(cpa.analyse(CAMS, html, ask, now=99999))
        self.assertIn("no camera photo could be downloaded", out["error"])

    def test_lta_speed_band_fallback(self):
        self.assertEqual([cpa.level_from_band(b) for b in (8, 5, 4, 3, 2, 1, None)], ["Smooth", "Smooth", "Medium", "Jam", "Jam", "Super Jam", None])
        async def bands(lat, lon):
            return {"level": "Medium", "speed": 30, "free_speed": 39, "source": "LTA speed bands"}
        out = asyncio.run(cpa.analyse(CAMS, None, None, bands, now=1000))
        self.assertEqual(out["cameras"]["6710"], {"level": "Medium", "to_jb": None, "to_sg": None, "confidence": None, "source": "LTA speed bands",
                                                  "speed": 30, "free_speed": 39, "at": 1000})
        self.assertEqual(out["crossings"][0]["any"], {"level": "Medium", "minutes": 30})

    def test_lta_speeds_sent_as_text_do_not_crash(self):
        async def bands(lat, lon):
            return {"level": "Jam", "speed": "20", "free_speed": "29", "source": "LTA speed bands"}
        out = asyncio.run(cpa.analyse(CAMS, None, None, bands, now=1000))
        self.assertEqual((out["cameras"]["2701"]["speed"], out["cameras"]["2701"]["free_speed"]), (20, 29))

    def test_ai_failure_falls_back(self):
        async def broken(content):
            raise RuntimeError("quota")
        async def flow(lat, lon):
            return {"cur": 50.0, "free": 55.0, "ratio": .9, "conf": 1}
        out = asyncio.run(cpa.analyse(CAMS, self.fetch, broken, flow, now=1000))
        self.assertEqual(out["error"], "AI camera reading failed: quota")              # the real reason is shown
        self.assertEqual(out["cameras"]["6710"]["level"], "Smooth")


TUAS = [{"id": "4703", "link": "https://img/4703.jpg", "lat": 1.3487, "lon": 103.6350, "taken": "t1", "desc": "Tuas Second Link"},
        {"id": "4713", "link": "https://img/4713.jpg", "lat": 1.3410, "lon": 103.6366, "taken": "t1", "desc": "Tuas Checkpoint"}]


class Directions(unittest.TestCase):
    """30 Oct 2026: camera 4703 showed the lorry queue ENTERING Singapore, but the panel said 'to Johor 60 min, jam'."""

    def setUp(self):
        cpa._CACHE.clear()
        cpa.STATUS.update(day=None, calls_today=0)
        self.swapped = set(cpa.SWAPPED)
        cpa.SWAPPED.clear()

    def tearDown(self):
        cpa.SWAPPED.clear()
        cpa.SWAPPED.update(self.swapped)

    async def fetch(self, url):
        return b"\xff\xd8\xffjpeg", "image/jpeg"

    def test_prompt_says_which_way_the_traffic_goes(self):
        text = cpa.prompt(TUAS + CAMS[:1])
        self.assertIn("Singapore side", text)
        self.assertIn("LOOKS, not which way the traffic goes", text)              # "(Towards Johor)" is the camera's view, not the traffic
        line_4703 = next(l for l in text.splitlines() if l.startswith("Image 1:"))
        self.assertIn("lorries queue", line_4703)
        self.assertIn("ARRIVING INTO SINGAPORE", line_4703)
        self.assertIn("to_sg", line_4703)
        self.assertIn("sg_where", text)
        self.assertNotIn("Directions at the checkpoints", cpa.prompt(CAMS[2:]))   # ordinary cameras: no border rule

    def test_where_each_direction_was_read_is_kept(self):
        out = cpa.parse_reply('[{"image":1,"level":"Jam","to_sg":"Jam","sg_where":"lorry queue along the sea, top","to_jb":null,'
                              '"jb_where":"curved ramp","confidence":0.6}]', 1)
        self.assertEqual(out[0]["sg_where"], "lorry queue along the sea, top")
        self.assertNotIn("jb_where", out[0])                                       # no level for Johor -> no 'where' either

    def test_the_4703_photo_reads_as_traffic_into_singapore(self):
        async def ask(content):
            self.assertIn("ARRIVING INTO SINGAPORE", content[-1]["text"])
            return ('[{"image":1,"level":"Jam","to_sg":"Jam","sg_where":"lorry queue along the sea","to_jb":null,"confidence":0.8},'
                    '{"image":2,"level":"Smooth","to_sg":null,"to_jb":"Smooth","jb_where":"departure lanes","confidence":0.7}]')
        out = asyncio.run(cpa.analyse(TUAS, self.fetch, ask, now=1793160000))
        tuas = out["crossings"][0]
        self.assertEqual(tuas["to_sg"], {"level": "Jam", "minutes": 60})
        self.assertEqual(tuas["to_jb"], {"level": "Smooth", "minutes": 15})
        self.assertEqual(out["cameras"]["4703"]["sg_where"], "lorry queue along the sea")
        self.assertEqual(out["swapped"], [])

    def test_controller_swap_flips_at_once_and_undo_restores(self):
        async def ask(content):                                        # an AI reading the wrong way round
            return '[{"image":1,"level":"Jam","to_jb":"Jam","jb_where":"lorry queue","to_sg":null},{"image":2,"level":"Smooth"}]'
        now = 1793160000
        out = asyncio.run(cpa.analyse(TUAS, self.fetch, ask, now=now))
        self.assertEqual(out["crossings"][0]["to_jb"]["level"], "Jam")
        cpa.SWAPPED.add("4703")
        out = asyncio.run(cpa.analyse(TUAS, self.fetch, ask, now=now + 60))      # cached reading, no new AI call
        cam = out["cameras"]["4703"]
        self.assertEqual((cam["to_sg"], cam["to_jb"], cam["sg_where"], cam.get("jb_where"), cam["swapped"]), ("Jam", None, "lorry queue", None, True))
        self.assertEqual(out["crossings"][0]["to_sg"], {"level": "Jam", "minutes": 60})
        self.assertIsNone(out["crossings"][0]["to_jb"])
        self.assertEqual(out["swapped"], ["4703"])
        self.assertEqual(cpa._CACHE["4703"][1]["to_jb"], "Jam")                  # the stored reading itself is untouched
        cpa.SWAPPED.discard("4703")
        out = asyncio.run(cpa.analyse(TUAS, self.fetch, ask, now=now + 120))
        self.assertEqual(out["cameras"]["4703"]["to_jb"], "Jam")
        self.assertNotIn("swapped", out["cameras"]["4703"])

    def test_swap_never_touches_speed_band_readings(self):
        cpa.SWAPPED.add("4703")
        async def bands(lat, lon):
            return {"level": "Medium", "source": "LTA speed bands"}
        out = asyncio.run(cpa.analyse(TUAS, None, None, bands, now=1000))
        self.assertNotIn("swapped", out["cameras"]["4703"])
        self.assertEqual(out["crossings"][0]["any"], {"level": "Medium", "minutes": 30})


if __name__ == "__main__":
    unittest.main()
