"""Recovery Decision Engine V13.10-V13.11 (30 Oct 2026): No action adjusts no trip; Trip adjustment keeps its adjusted spacing on every later
trip; Halfway leaves the other trips alone (timetable from the 2nd trip); QoS 1.2 (adjusted departure headway <= scheduled + 6 min);
average / max headway 30 min either side of the long headway. V13.14-15 (9 Nov 2026): one steady headway (option), run order kept
(no trip leaves before the trip in front of it; the +/-8 limit counts from the time it must wait behind a late trip).
Run: python3 -m unittest test_recovery_qos"""
import unittest

import recovery as R

N_ST = 49
TAU = [round(i * 75.0 / (N_ST - 1), 2) for i in range(N_ST)]           # 75-min trip, 49 stops
CODES = [f"{10000 + i}" for i in range(N_ST)]
DOWN = [(f"Stop D{i}", f"{20000 + i}", round(i * 22.0 / (N_ST - 1), 3)) for i in range(N_ST)]


def ctx(late, params=None, now=None):
    return {"H": 12.0, "tau": TAU, "stop_s": [round(i * 22.0 / (N_ST - 1), 3) for i in range(N_ST)], "route_km": 22.0,
            "stop_names": [f"Stop {i}" for i in range(N_ST)], "stop_codes": CODES, "t0": 420.0, "late": late,
            "candidates": [{"j": j} for j in (10, 15, 20, 25)], "aitp_up": CODES, "now": now, "down_stops": DOWN,
            "aitp_dn": [DOWN[int(round(k * (N_ST - 1) / 9))][1] for k in range(10)],
            "params": dict({"balance_trips": 4, "sims": 150}, **(params or {})), "mode": "balanced"}


LATE6 = [0, 0, 0, 0, 0, 16, 0, 0, 0, 0]                                # trip 6's bus arrives 16 min late (ready 13 min after its slot)


class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = R.optimise(ctx(LATE6))

    def test_no_action_adjusts_no_trip_at_all(self):
        none = self.r["plans"]["none"]
        self.assertFalse(none["later_adjusted"])
        self.assertEqual(none["term_adj"], [])                              # no later trip held or released either
        self.assertEqual(none["n_adjusted"], 0)
        C = R.Chain(ctx(LATE6), {**R.PARAMS, "balance_trips": 4, "sims": 150})
        det = C.run(C.nat[None, :].copy(), None, None, keep=True, reg=False)
        # later trips leave at max(schedule, arrival + 7): DOWN 1 departures equal that natural time exactly
        end0 = det["legs"][0]["end"][0]
        nat1 = [max(C.S[b] + C.off[1], end0[b] + 7.0) for b in range(1, C.n + 1)]
        dep1 = sorted(nat1)
        got = sorted(float(x) for x in det["legs"][1]["dep_T"][0][1:-1])
        self.assertEqual([round(x, 6) for x in got], [round(x, 6) for x in dep1])

    def test_trip_adjustment_also_adjusts_the_later_trips(self):
        adj = self.r["plans"]["adjust"]
        self.assertTrue(adj["later_adjusted"])
        self.assertTrue(any(a["L"] >= 1 for a in adj["term_adj"]))            # instructions on DOWN 1 / UP 2 / DOWN 2 too
        self.assertTrue(all(abs(a["shift"]) <= 8.0 + 1e-6 for a in adj["term_adj"]))
        if self.r["decision"] == "adjust":
            self.assertTrue(any(i.get("later") for i in self.r["instructions"]))

    def test_adjustment_beats_no_action_once_both_are_consistent(self):
        n, a = self.r["plans"]["none"]["ewt"]["avg"], self.r["plans"]["adjust"]["ewt"]["avg"]
        self.assertLess(a, n * 0.5)                                          # was 0.34 vs 0.67 with the old cap; now ~0.19 vs ~1.05
        self.assertIn(self.r["decision"], ("adjust", "halfway"))

    def test_qos_1_2_met_by_the_adjustment(self):
        q_n, q_a = self.r["plans"]["none"]["qos"], self.r["plans"]["adjust"]["qos"]
        self.assertFalse(q_n["ok"])                                          # 24-min gap at the interchange: 12 over the 12-min schedule
        self.assertGreater(q_n["max_over"], 6)
        self.assertTrue(q_a["ok"], q_a)
        self.assertLessEqual(q_a["max_over"], 6.0 + 1e-6)
        for row in self.r["plans"]["adjust"]["rows"]:                        # and still inside the per-trip limits
            if row["type"] == "full":
                self.assertLessEqual(abs(row["shift"] - row["wait"]), 8.0 + 1e-6)      # +/-8 from when it may leave (run order)
                self.assertGreaterEqual(row["layover"], 7.0 - 1e-6)
        self.assertTrue(any("QoS 1.2 met" in w for w in self.r["why"]))

    def test_qos_limit_is_a_setting(self):
        r = R.optimise(ctx(LATE6, {"qos_over_max": 3.0}))
        self.assertIn("3", r["plans"]["adjust"]["qos"]["rule"])
        self.assertEqual(r["params"]["qos_over_max"], 3.0)


def per_trip(plan):
    return {c["leg"]: c["win"] for c in plan["chain"]}


class SameAsTheOCCReadsIt(unittest.TestCase):
    """Service 147 case (30 Oct): 12-min headway, trip 6 arrives 15 min late (ready exactly when trip 7 is due)."""
    @classmethod
    def setUpClass(cls):
        cls.r = R.optimise(ctx([0, 0, 0, 0, 0, 15, 0, 0, 0, 0]))

    def test_no_action_keeps_the_gap_and_the_pair(self):
        w = per_trip(self.r["plans"]["none"])
        self.assertEqual({round(x["avg"]) for x in w.values()}, {12})          # no trip lost: average stays 12
        mx = [w[k]["max"] for k in ("UP 1", "DOWN 1", "UP 2", "DOWN 2")]
        self.assertGreater(mx[0], 23)                                           # the long gap ...
        self.assertTrue(all(a >= b - 0.05 for a, b in zip(mx, mx[1:])))         # ... carries on, shrinking only by the layover slack
        self.assertLess(mx[0] - mx[-1], 3 * 3 + 0.5)                            # at most 3 min (10 scheduled - 7 minimum) per terminal
        self.assertLess(w["UP 1"]["min"], 1.0)                                  # trip 6 (ready 17:12) and trip 7 (due 17:12) leave together

    def test_trip_adjustment_is_the_same_on_every_trip(self):
        w = per_trip(self.r["plans"]["adjust"])
        self.assertEqual(len({(x["avg"], x["max"], x["min"]) for x in w.values()}), 1, w)
        self.assertLessEqual(max(x["max"] for x in w.values()), 12 + 6 + 0.5)   # QoS 1.2 holds on every trip

    def test_halfway_with_adjustment_then_timetable(self):
        p = self.r["plans"]["halfway"]
        w = per_trip(p)
        moved = [r_ for r_ in p["rows"] if r_["type"] == "full" and abs(r_["shift"]) >= 0.5]
        self.assertTrue(moved)                                                  # the trips around the lost trip ARE adjusted ...
        self.assertTrue(all(abs(r_["shift"]) <= 3.0 + 1e-6 for r_ in moved))    # ... only by what the BC wins back in the layover (10 - 7)
        self.assertTrue(p["qos"]["ok"], p["qos"])                               # the lost trip's 24-min gap is brought to <= 18 at the interchange
        dep = sorted(r_["dep"] for r_ in p["rows"] if r_["type"] != "halfway")
        self.assertLessEqual(max(b - a for a, b in zip(dep, dep[1:])), 18.0 + 1e-6)
        for lg in ("DOWN 1", "UP 2", "DOWN 2"):                                 # from the 2nd trip: the timetable
            self.assertAlmostEqual(w[lg]["max"], 12.0, delta=0.6)
            self.assertAlmostEqual(w[lg]["min"], 12.0, delta=0.6)
        self.assertEqual(p["term_adj"], [])

    def test_halfway_only_is_still_a_setting(self):
        r = R.optimise(ctx([0, 0, 0, 0, 0, 15, 0, 0, 0, 0], {"halfway_regulate": 0}))
        p = r["plans"]["halfway"]
        self.assertEqual([r_["n"] for r_ in p["rows"] if r_["type"] == "full" and abs(r_["shift"]) >= 0.5], [])

    def test_former_rules_still_available(self):
        r = R.optimise(ctx([0, 0, 0, 0, 0, 15, 0, 0, 0, 0], {"term_mode": "even"}))
        self.assertTrue(r["plans"]["adjust"]["later_adjusted"])
        self.assertEqual(r["params"]["term_mode"], "even")

    def test_window_figures(self):
        self.assertEqual(R.win_stats([12, 12, 12, 24, 0, 12, 12, 12], 30, 12), {"avg": 12.0, "max": 24.0, "min": 0.0, "n": 6})
        self.assertEqual(R.win_stats([12] * 8, 30, 12)["max"], 12.0)


class Limits(unittest.TestCase):
    def test_qos_reported_honestly_when_it_cannot_be_met(self):
        # trips 1-5 have already left (live, 07:50): holding cannot close the gap in front of the late trip 6 ...
        r = R.optimise(ctx(LATE6, now=470.0))
        q = r["plans"]["adjust"]["qos"]
        self.assertTrue([b for b in q["breaches"] if b["leg"] == "UP 1" and b["between"] == "Trip 5 and Trip 6"], q)      # run order: Trip 7 may not fill it
        hq = r["plans"]["halfway"]["qos"]                                     # a halfway start leaves the same gap at the interchange: reported too
        self.assertTrue(hq["ok"] or hq["breaches"][0]["leg"] == "UP 1")
        c = ctx(LATE6, now=470.0)
        c["candidates"] = []                                                  # no halfway stop: the best full-trip plan, said plainly
        r = R.optimise(c)
        self.assertEqual(r["decision"], "adjust")
        self.assertTrue(any("cannot be fully met" in w and "Trip 5 and Trip 6" in w for w in r["why"]), r["why"])

    def test_old_total_hold_cap_still_works_when_switched_on(self):
        r = R.optimise(ctx(LATE6, {"term_total_max": 8.0}))
        holds = {}
        for row in r["plans"]["adjust"]["rows"]:
            if row["type"] == "full" and row["shift"] - row["wait"] > 0:
                holds[row["n"]] = holds.get(row["n"], 0) + row["shift"] - row["wait"]
        for a in r["plans"]["adjust"]["term_adj"]:
            if a["shift"] > 0:
                holds[a["n"]] = holds.get(a["n"], 0) + a["shift"]
        self.assertTrue(all(v <= 8.0 + 1.0 for v in holds.values()), holds)     # rounding to whole minutes

    def test_no_late_trip_means_nothing_to_do(self):
        r = R.optimise(ctx([0] * 10))
        self.assertEqual(r["decision"], "none")



class RunOrder(unittest.TestCase):
    def test_no_trip_leaves_before_the_trip_in_front(self):
        for late in (LATE6, [0, 0, 0, 0, 0, 25, 14, 3, 0, 0], [0, 0, 0, 0, 30, 0, 0, 0, 0, 0]):
            for style in ("least_wait", "steady"):
                r = R.optimise(ctx(late, {"adjust_style": style}))
                for k in ("adjust", "halfway"):
                    p = r["plans"].get(k)
                    if not p:
                        continue
                    full = sorted([x for x in p["rows"] if x["type"] != "halfway"], key=lambda x: (x["dep"], x["n"]))
                    self.assertEqual([x["n"] for x in full], sorted(x["n"] for x in full), (late, style, k))
                    for c in p["chain"]:
                        ids = [b for b, _ in c["dep_hw"] if 1 <= b <= 10]
                        self.assertEqual(ids, sorted(ids), (late, style, k, c["leg"]))

    def test_bunched_late_trips_leave_in_run_order(self):
        r = R.optimise(ctx([0, 0, 0, 0, 0, 25, 14, 3, 0, 0]))                  # trips 6, 7, 8 ready together
        d = {x["n"]: x["dep"] for x in r["plans"]["adjust"]["rows"]}
        self.assertTrue(d[6] < d[7] < d[8], d)
        self.assertTrue(any("waits for Trip" in i.get("sub", "") for i in r["instructions"]) or r["decision"] == "halfway")

    def test_no_action_unchanged(self):
        r = R.optimise(ctx([0, 0, 0, 0, 0, 25, 0, 0, 0, 0]))
        rows = {x["n"]: x for x in r["plans"]["none"]["rows"]}
        self.assertEqual(rows[7]["shift"], 0)                                  # nobody steps in: trip 7 leaves when it is ready
        self.assertLess(rows[7]["dep"], rows[6]["dep"])

    def test_even_headway_two_duties_above_and_below(self):
        late = [0, 0, 0, 0, 10, 0, 0, 0, 0, 0]                                    # trip 5 leaves 7 min late
        r = R.optimise(ctx(late, {"adjust_style": "steady"}))
        A, st = r["plans"]["adjust"], r["plans"]["adjust"]["steady"]
        self.assertEqual((st["side"], st["first"], st["last"], st["after"]), (2, 3, 7, 8))
        moved = [x["n"] for x in A["rows"] if abs(x["shift"]) >= 0.5]
        self.assertTrue(set(moved) <= {3, 4, 6, 7}, moved)                        # only 2 duties above and 2 below move; trip 5 leaves when ready
        for g in st["segments"]:
            self.assertLessEqual(max(g["gaps"]) - min(g["gaps"]), 1.0, g)          # even on each side: at most 1 min apart
        self.assertTrue(all(abs(x["shift"]) <= 8.0 + 1e-6 for x in A["rows"]))
        self.assertLess(A["ewt"]["avg"], r["plans"]["none"]["ewt"]["avg"])
        o = {x["side"]: x for x in r["steady_alt"]["options"]}
        self.assertEqual(sorted(o), [1, 2, 3, 4, 5])
        self.assertLessEqual(o[3]["ewt"], o[1]["ewt"])                             # more duties share the delay more thinly
        r3 = R.optimise(ctx(late, {"adjust_style": "steady", "steady_side": 3}))
        self.assertEqual((r3["plans"]["adjust"]["steady"]["first"], r3["plans"]["adjust"]["steady"]["last"]), (2, 8))
        self.assertEqual(r["adjust_style"], "steady")
        self.assertEqual(R.optimise(ctx(late))["adjust_style"], "least_wait")
        up1 = [round(g) for _, g in A["chain"][0]["dep_hw"]]
        for c in A["chain"][1:]:                                                    # later trips keep the same gaps (give or take a minute of running time)
            self.assertTrue(all(abs(round(g) - u) <= 1 for (_, g), u in zip(c["dep_hw"], up1)), (c["leg"], c["dep_hw"], up1))


if __name__ == "__main__":
    unittest.main()
