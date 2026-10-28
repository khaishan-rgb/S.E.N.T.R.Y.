"""Live Bunching & Long Headway view: grouping per service-direction, case status, KPIs from the same list, 1-hour change."""
import unittest

import bunching_live as bl

STOPS = [{"code": str(10000 + i), "name": f"Stop {i}", "road": "Rd", "seq": i + 1, "lat": 1.30 + i * .001, "lon": 103.80 + i * .001} for i in range(40)]


def row(svc="16", d=1, groups=(), gaps=(), at=1000.0):
    seq = [{"id": f"B{i}", "lat": 1.3 + i * .01, "lon": 103.8, "status": "Normal"} for i in range(1, 9)]
    return {"key": f"{svc}:{d}", "service": svc, "direction": d, "sched_hw": 10, "at": at, "route_stops": STOPS, "seq": seq,
            "groups": list(groups), "gaps": list(gaps), "risk": "red"}


G3 = {"status": "confirmed", "size": 3, "ids": ["B1", "B2", "B3"], "joining": [], "stops": 14, "hw_now": 1.2, "min_hw": 0.8, "cur_j": 12,
      "location": "Stop 12", "since_stop": "Stop 2 (10002)", "headways": [1.2, 1.5]}
G2 = {"status": "developing", "size": 2, "ids": ["B5", "B6"], "joining": [], "stops": 6, "hw_now": 2.0, "min_hw": 1.9, "cur_j": 20, "location": "Stop 20"}
EARLY = {"status": "early", "size": 2, "ids": ["B7", "B8"], "joining": [], "stops": 0, "hw_now": 4.0, "min_hw": 2.5, "cur_j": 25}
GAP = {"active": True, "state": "confirmed", "now": 22.0, "max_hw": 24.0, "stops": 18, "lead": "B3", "foll": "B4", "cur_j": 15, "location": "Stop 15"}
GAP_PRED = {"active": False, "state": "developing", "now": 14.0, "max_hw": 21.0, "stops": 0, "lead": "B6", "foll": "B7", "cur_j": 22}


class Cases(unittest.TestCase):
    def test_one_row_per_service_direction_with_every_active_case(self):
        ev = [{"id": 7, "kind": "bb", "key": "16:1", "cur_ids": {"B1", "B2", "B3"}, "ids": {"B1", "B2", "B3"}, "start": 400, "alerts": [1], "acked_n": 1,
               "acked_ts": 900, "acked_by": "Kim"},
              {"id": 8, "kind": "gap", "key": "16:1", "lead": "B3", "foll": "B4", "start": 950, "alerts": []}]
        v = bl.live_view([row(groups=[G3, G2, EARLY], gaps=[GAP, GAP_PRED]), row("147", 2), {"key": "9:1", "error": "x"}], ev, 1000, {"16": "SBST"})
        self.assertEqual(len(v["services"]), 1)                               # 147 has no case, the errored row is ignored
        s = v["services"][0]
        self.assertEqual(s["operator"], "SBST")
        self.assertEqual([c["kind"] for c in s["cases"]], ["3BB", "2BB", "GAP"])   # predicted group / gap are not active cases
        self.assertEqual([c["label"] for c in s["cases"]], ["A", "B", "C"])
        a, b, c = s["cases"]
        self.assertEqual((a["status"], a["acked_by"], a["duration_min"], a["stops"], a["actual_hw"], a["sched_hw"]), ("acknowledged", "Kim", 10.0, 14, 1.2, 10))
        self.assertEqual(a["stop"]["code"], "10012")
        self.assertEqual(a["point"], [STOPS[12]["lon"], STOPS[12]["lat"]])
        self.assertEqual((b["status"], b["id"], b["duration_min"]), ("developing", None, None))  # not yet tracked as an open case
        self.assertEqual((c["status"], c["actual_hw"], c["max_hw"], c["buses"]), ("new", 22.0, 24.0, ["B3", "B4"]))
        self.assertEqual((s["issue"], s["max_stops"], s["n_cases"]), ("3BB", 18, 3))
        k = v["kpi"]
        self.assertEqual((k["affected"], k["monitored"], k["bb3"], k["bb2"], k["gap"], k["cases"]), (1, 2, 1, 1, 1, 3))

    def test_escalation_and_severity_order(self):
        ev = [{"id": 1, "kind": "bb", "key": "5:1", "cur_ids": {"B1", "B2", "B3"}, "start": 0, "escalated_at": 900, "alerts": []}]
        v = bl.live_view([row("5", groups=[G3]), row("4", gaps=[GAP]), row("3", groups=[G2])], ev, 1000)
        self.assertEqual([s["service"] for s in v["services"]], ["5", "3", "4"])   # 3BB, then 2BB, then long gap
        self.assertEqual(v["services"][0]["cases"][0]["status"], "escalating")

    def test_four_bus_group_and_joining_bus(self):
        g = dict(G3, joining=["B4"])
        c = bl.service_cases(row(groups=[g]), [], 1000)[0]
        self.assertEqual((c["kind"], c["size"]), ("4BB+", 4))

    def test_one_hour_change_uses_the_same_rules(self):
        per2 = {"16:1": bl.kpi_tuple(row(groups=[G3, G2, EARLY], gaps=[GAP, GAP_PRED])), "16:2": bl.kpi_tuple(row(d=2, groups=[G2])), "7:1": bl.kpi_tuple(row("7"))}
        k = bl.kpi_from_hist(per2)
        self.assertEqual((k["affected"], k["bb3"], k["bb2"], k["gap"], k["bb2_services"]), (1, 1, 2, 1, 1))


class TalliesWithForecastCentre(unittest.TestCase):
    def test_forecast_centre_now_counts_match_the_live_cards(self):
        import time
        import forecast2026 as f
        r = row(groups=[G3, G2, EARLY], gaps=[GAP, GAP_PRED], at=time.time())
        sample = dict(r, at=time.time(), hw={"0": 1.2})
        d = f.build_centre("live", 0, raw={"age_s": 0, "services": [{"svc": "16", "dir": 1}], "sources": {}}, samples=[sample])
        fc = next(s for s in d["services"] if s["svc"] == "16")
        k = bl.live_view([r], [], time.time())["kpi"]
        self.assertEqual((fc["three"], fc["gaps"]), (k["bb3"], k["gap"]))


if __name__ == "__main__":
    unittest.main()
