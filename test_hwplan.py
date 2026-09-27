import unittest

import hwplan


def route(direction, code_prefix):
    stops = [
        {
            "code": f"{code_prefix}{i:04d}",
            "name": f"Stop {i + 1}",
            "lat": 1.30 + i * 0.001,
            "lon": 103.80 + i * 0.001,
        }
        for i in range(20)
    ]
    return {
        "dir": direction,
        "stops": stops,
        "ss": [i * 0.5 for i in range(20)],
        "H": 10,
        "H_src": "test",
        "buses": [
            {"id": i + direction * 10, "s": distance, "wait": None}
            for i, distance in enumerate((8, 6, 4, 2, 0))
        ],
    }


class HalfwayPlanTests(unittest.TestCase):
    def test_ai_best_is_lowest_downstream_ewt(self):
        current = route(1, 1)
        following = route(2, 2)
        result = hwplan.plan(
            {
                "now": 840,
                "late": {"bus": 12, "delay": 20},
                "T": current,
                "O": following,
                "P": {"layover": 10},
            },
            {j: (2 + j * 0.5, j * 0.4, "road") for j in range(1, 19)},
        )

        self.assertTrue(result["ok"])
        self.assertGreater(len(result["candidates"]), 1)
        recommended = next(c for c in result["candidates"] if c["code"] == result["best"])
        self.assertEqual(recommended["ewt"], min(c["ewt"] for c in result["candidates"]))
        self.assertEqual(result["best"], result["best_ewt"])
        self.assertEqual(recommended["rank"], 1)
        self.assertIn("lowest forecast downstream EWT", recommended["why"])


if __name__ == "__main__":
    unittest.main()
