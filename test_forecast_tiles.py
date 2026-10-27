"""No-network regressions for TomTom indexed category tags and direction markers.

Run: python -m unittest test_forecast_tiles.py
"""
import unittest
from unittest.mock import patch
import forecast as F


class IncidentTileTags(unittest.TestCase):
    def parse(self, flow_props, poi_props=None, reverse=False):
        tile = F.tiles_for_bbox(14, 1.333, 103.884, 1.333, 103.884)[0]
        line = [(1400, 1800), (1600, 1800), (1800, 1800)]
        poi = line[-1] if reverse else line[0]
        decoded = {
            "Traffic incidents flow": {"extent": 4096, "features": [
                {"props": dict(flow_props, delay=120, magnitude=2), "parts": [line]}]},
            "Traffic incidents POI": {"extent": 4096, "features": [
                {"props": dict(poi_props or flow_props, poi_type="start_poi"), "parts": [[poi]]}]},
        }
        with patch.object(F, "decode_mvt", return_value=decoded):
            return F.parse_tomtom_incident_tiles([(*tile, b"fixture")], now=100000)

    def test_indexed_categories_retain_real_types(self):
        for category, kind in [(1, "accident"), (6, "jam"), (9, "roadworks")]:
            with self.subTest(category=category):
                events = self.parse({"icon_category_0": category, "description_0": "Reported type"})
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["kind"], kind)
                self.assertEqual(events[0]["desc"], "Reported type")
                self.assertTrue(events[0]["dir_known"])
                self.assertEqual(events[0]["delay_s"], 120)

    def test_highest_numeric_priority_once_not_double_counted(self):
        props = {"icon_category_2": 6, "description_2": "Traffic", "icon_category_10": 9, "description_10": "Works"}
        events = self.parse(props)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "roadworks")
        self.assertEqual(events[0]["desc"], "Works")
        self.assertTrue(events[0]["dir_known"])

    def test_poi_with_flat_category_matches_indexed_line(self):
        events = self.parse({"icon_category_0": 6}, {"icon_category": 6}, reverse=True)
        self.assertTrue(events[0]["dir_known"])
        self.assertGreater(events[0]["line"][0][1], events[0]["line"][-1][1])

    def test_flat_categories_backward_compatible(self):
        events = self.parse({"icon_category": 1, "description": "Accident"})
        self.assertEqual(events[0]["kind"], "accident")
        self.assertEqual(events[0]["desc"], "Accident")
        self.assertTrue(events[0]["dir_known"])

    def test_malformed_unknown_does_not_invent_types(self):
        for props in [{}, {"icon_category": None}, {"icon_category_0": "bad"}, {"icon_category_0": 0}]:
            with self.subTest(props=props):
                events = self.parse(props)
                self.assertEqual(events[0]["kind"], "other")
                self.assertEqual(events[0]["label"], "Unknown")

    def test_mixed_cluster_without_specific_category_stays_skipped(self):
        self.assertEqual(self.parse({"icon_category": 13}), [])
        events = self.parse({"icon_category": 13, "icon_category_0": 9})
        self.assertEqual(events[0]["kind"], "roadworks")


if __name__ == "__main__":
    unittest.main()
