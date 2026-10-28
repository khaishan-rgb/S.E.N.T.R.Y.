"""Past Bunching & Long Headway: per-level counts (2BB / 3BB+ / long gap), average length, daily sparkline, hour and location filters."""
import unittest

import bunching_analytics as ba

DAY = 86400


class History(unittest.TestCase):
    def setUp(self):
        self.now = 1793160000                                         # 2026-10-28 12:00 SGT
        self.p = ba.period_bounds("7d", self.now)
        t = self.p["start"] + 8 * 3600                                # 08:00 SGT on the first day
        self.ev = [{"kind": "bb", "service": "16", "direction": 1, "start": t, "end": t + 600, "level": 3, "stops": 20, "start_stop": "Stop A (10001)"},
                   {"kind": "bb", "service": "16", "direction": 1, "start": t + DAY, "end": t + DAY + 1200, "level": 2, "stops": 16, "start_stop": "Stop B (10002)"},
                   {"kind": "bb", "service": "16", "direction": 2, "start": t + DAY + 3600, "end": None, "level": 4, "stops": 15, "start_stop": "Stop A (10001)", "open": True},
                   {"kind": "gap", "service": "147", "direction": 1, "start": t + 2 * DAY, "end": t + 2 * DAY + 1800, "level": None, "stops": 18, "start_stop": "Stop A (10001)"}]
        self.stops = {"10001": {"name": "Stop A", "road": "Rd", "lat": 1.3, "lon": 103.8}, "10002": {"name": "Stop B", "road": "Rd", "lat": 1.31, "lon": 103.81}}

    def test_levels_durations_and_daily(self):
        out = ba.aggregate(self.ev, [], self.stops, self.p, top_n=50)
        s16 = next(s for s in out["services"] if s["service"] == "16")
        self.assertEqual((s16["bb2"], s16["bb3p"], s16["gap"], s16["events"]), (1, 2, 0, 3))   # 4BB+ counts in 3BB+
        self.assertEqual(s16["avg_min"], 15.0)                                                # (10 + 20) / 2; the open case has no end yet
        self.assertEqual(len(s16["daily"]), len(out["days"]))
        self.assertEqual(s16["daily"][:2], [1, 2])
        self.assertEqual((out["hours"][8]["bb3p"], out["hours"][8]["bb2"], out["hours"][9]["bb3p"]), (1, 1, 1))
        self.assertEqual((out["days"][1]["bb2"], out["days"][1]["bb3p"]), (1, 1))
        a = next(x for x in out["stops"] if x["code"] == "10001")
        self.assertEqual((a["events"], a["bb3p"], a["gap"], a["n_services"]), (3, 2, 1, 2))

    def test_hour_and_location_filters(self):
        self.assertEqual(ba.aggregate(self.ev, [], self.stops, self.p, hour=9)["totals"]["events"], 1)
        self.assertEqual(ba.aggregate(self.ev, [], self.stops, self.p, stop="10002")["totals"]["events"], 1)


if __name__ == "__main__":
    unittest.main()
