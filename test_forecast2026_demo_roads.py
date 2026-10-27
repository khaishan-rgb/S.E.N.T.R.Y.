"""Demo map geometry follows real roads; traffic colours only the affected road stretch."""
import math
import unittest
import forecast2026 as f


def metres(a, b):
    return math.hypot((b[0]-a[0])*111320*math.cos(math.radians(a[1])), (b[1]-a[1])*111320)


def distance_to_line(p, line):
    best = 1e18
    for a, b in zip(line, line[1:]):
        ax, ay, bx, by = 0, 0, (b[0]-a[0])*111320*math.cos(math.radians(a[1])), (b[1]-a[1])*111320
        px, py = (p[0]-a[0])*111320*math.cos(math.radians(a[1])), (p[1]-a[1])*111320
        L = bx*bx+by*by; t = 0 if L == 0 else max(0, min(1, (px*bx+py*by)/L))
        best = min(best, math.hypot(px-t*bx, py-t*by))
    return best


class DemoRoads(unittest.TestCase):
    def test_every_demo_route_uses_real_dense_geometry_and_stops(self):
        self.assertEqual(len(f.DEMO_GEOMETRY), len(f.CORRIDORS))
        data = f.build_centre('demo', 30, 'combined', delay_min=0)
        for r in data['routes']:
            self.assertEqual(r['mode'], 'demo')
            self.assertGreater(len(r['line']), 80, r['svc'])                       # follows bends, not 4-5 corners
            self.assertLess(max(metres(a, b) for a, b in zip(r['line'], r['line'][1:])), 2500, r['svc'])
        real_stops = [s for s in data['stops'] if s['id'] and not str(s['id']).startswith('SIM')]
        self.assertGreater(len(real_stops), 400)

    def test_traffic_stretch_lies_on_its_route_and_is_short(self):
        data = f.build_centre('demo', 30, 'combined', delay_min=0)
        lines = {r['svc']: r['line'] for r in data['routes']}
        self.assertTrue(data['events'])
        for e in data['events']:
            route = lines[e['svc']]
            length = sum(metres(a, b) for a, b in zip(e['line'], e['line'][1:]))
            self.assertTrue(300 <= length <= 2000, (e['road'], length))             # a road stretch, never the whole route
            self.assertLess(max(distance_to_line(p, route) for p in e['line']), 5, e['road'])
            impact = e['impacts'][0]
            self.assertTrue(all(s['code'] for s in impact['stops']))               # real LTA stop codes
            self.assertTrue(any(s['road'] == e['road'] for s in impact['stops']), e['road'])

    def test_scenarios_name_roads_their_service_really_uses(self):
        aye = f.build_centre('demo', 30, 'accident', delay_min=0)['events']
        works = f.build_centre('demo', 30, 'roadworks', delay_min=0)['events']
        self.assertEqual([(e['svc'], e['road'], e['kind']) for e in aye], [('97', 'AYE', 'accident')])
        self.assertEqual([(e['svc'], e['road'], e['kind']) for e in works], [('190', 'Orchard Rd', 'roadworks')])

    def test_buses_sit_on_their_route(self):
        data = f.build_centre('demo', 15, 'combined', delay_min=0)
        lines = {r['svc']: r['line'] for r in data['routes']}
        for b in data['buses']:
            self.assertLess(distance_to_line(b['point'], lines[b['svc']]), 5, b['id'])


if __name__ == '__main__':
    unittest.main()
