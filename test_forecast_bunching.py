"""Test the read-only bunching endpoint without starting the app or its collectors."""
import ast
import asyncio
import re
import types
import unittest
from pathlib import Path
from datetime import datetime, timezone


class CachedBunchingTests(unittest.TestCase):
    def read(self, rows, services=""):
        module = ast.parse(Path(__file__).with_name('app.py').read_text())
        fn = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'api_forecast_bunching')
        fn.decorator_list = []
        fixture = ast.Module(body=[fn], type_ignores=[])
        ctx = {'time': types.SimpleNamespace(time=lambda: 1000), 'BB': {'params': {'refresh_sec': 60}, 'rows': rows,'watch':{}}, 're':re,'BB_IDLE_SEC':300,'BB_MAX_PAIRS':40,'now_sgt': lambda: datetime.now(timezone.utc)}
        exec(compile(ast.fix_missing_locations(fixture), '<bunching-endpoint>', 'exec'), ctx)
        return asyncio.run(ctx['api_forecast_bunching'](services))['rows']

    def test_fresh_three_bus_count_and_real_zero(self):
        rows = self.read({'one': {'service':'160','direction':1,'at':990,'counts':{'bb3':2}}, 'zero':{'service':'974','direction':2,'at':990,'counts':{'bb3':0}}})
        self.assertEqual([r['groups3'] for r in rows], [2, 0])
        self.assertTrue(all(r['available'] for r in rows))

    def test_stale_missing_error_and_no_data_are_unavailable(self):
        samples = [{'at':800,'counts':{'bb3':3}}, {'counts':{'bb3':0}}, {'at':990,'error':'feed unavailable','counts':{'bb3':0}}, {'at':990,'issue':'No data','counts':{'bb3':0}}]
        rows = self.read({str(i):s for i,s in enumerate(samples)})
        self.assertTrue(all(not r['available'] and r['groups3'] is None for r in rows))

    def test_current_loading_counts_ignore_unknown_and_stale_samples(self):
        samples = {'fresh': {'service':'156','direction':2,'at':990,'counts':{'bb3':1}, 'seq':[{'load':'SEA'}, {'load':'SDA'}, {'load':'LSD'}, {'load':'LSD'}, {'load':'UNKNOWN'}]},
                   'old': {'service':'12','direction':1,'at':800,'counts':{'bb3':0}, 'seq':[{'load':'LSD'}]}}
        rows = self.read(samples)
        self.assertEqual(rows[0]['load_level'], 'LSD')
        self.assertEqual(rows[0]['load_sampled'], 4)
        self.assertEqual(rows[0]['load_counts']['LSD'], 2)
        self.assertIsNone(rows[1]['load_level'])
        self.assertEqual(rows[1]['load_sampled'], 0)

    def test_watching_services_does_not_invent_observations(self):
        self.assertEqual(self.read({}, services="156,160,not-valid!"), [])

    def test_empty_cache_is_not_a_zero_report(self):
        self.assertEqual(self.read({}), [])


if __name__ == '__main__':
    unittest.main()
