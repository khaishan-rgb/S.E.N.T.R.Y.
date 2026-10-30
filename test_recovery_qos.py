"""Recovery Decision Engine V13.10 (30 Oct 2026): No action adjusts no trip, Trip adjustment also adjusts the later trips,
QoS 1.2 (adjusted departure headway <= scheduled + 6 min). Run: python3 -m unittest test_recovery_qos"""
import unittest

import recovery as R

N_ST = 49
TAU = [round(i * 75.0 / (N_ST - 1), 2) for i in range(N_ST)]           # 75-min trip, 49 stops
CODES = [f"{10000 + i}" for i in range(N_ST)]


def ctx(late, params=None, now=None):
    return {"H": 12.0, "tau": TAU, "stop_s": [round(i * 22.0 / (N_ST - 1), 3) for i in range(N_ST)], "route_km": 22.0,
            "stop_names": [f"Stop {i}" for i in range(N_ST)], "stop_codes": CODES, "t0": 420.0, "late": late,
            "candidates": [{"j": j} for j in (10, 15, 20, 25)], "aitp_up": CODES, "now": now,
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
                self.assertLessEqual(abs(row["shift"]), 8.0 + 1e-6)
                self.assertGreaterEqual(row["layover"], 7.0 - 1e-6)
        self.assertTrue(any("QoS 1.2 met" in w for w in self.r["why"]))

    def test_qos_limit_is_a_setting(self):
        r = R.optimise(ctx(LATE6, {"qos_over_max": 3.0}))
        self.assertIn("3", r["plans"]["adjust"]["qos"]["rule"])
        self.assertEqual(r["params"]["qos_over_max"], 3.0)


class Limits(unittest.TestCase):
    def test_qos_reported_honestly_when_it_cannot_be_met(self):
        # trips 1-5 have already left (live, 07:50): holding cannot close the gap in front of the late trip 6 ...
        r = R.optimise(ctx(LATE6, now=470.0))
        q = r["plans"]["adjust"]["qos"]
        self.assertTrue([b for b in q["breaches"] if b["leg"] == "UP 1" and b["between"] == "Trip 5 and Trip 7"], q)
        self.assertTrue(r["plans"]["halfway"]["qos"]["ok"])                   # ... a halfway start can (decided on EWT)
        c = ctx(LATE6, now=470.0)
        c["candidates"] = []                                                  # no halfway stop: the best full-trip plan, said plainly
        r = R.optimise(c)
        self.assertEqual(r["decision"], "adjust")
        self.assertTrue(any("cannot be fully met" in w and "Trip 5 and Trip 7" in w for w in r["why"]), r["why"])

    def test_old_total_hold_cap_still_works_when_switched_on(self):
        r = R.optimise(ctx(LATE6, {"term_total_max": 8.0}))
        holds = {}
        for row in r["plans"]["adjust"]["rows"]:
            if row["type"] == "full" and row["shift"] > 0:
                holds[row["n"]] = holds.get(row["n"], 0) + row["shift"]
        for a in r["plans"]["adjust"]["term_adj"]:
            if a["shift"] > 0:
                holds[a["n"]] = holds.get(a["n"], 0) + a["shift"]
        self.assertTrue(all(v <= 8.0 + 1.0 for v in holds.values()), holds)     # rounding to whole minutes

    def test_no_late_trip_means_nothing_to_do(self):
        r = R.optimise(ctx([0] * 10))
        self.assertEqual(r["decision"], "none")


if __name__ == "__main__":
    unittest.main()
