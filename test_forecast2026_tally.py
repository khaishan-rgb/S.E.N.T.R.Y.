"""Top cards and map markers tally, and selecting a card filters the map to that category."""
import time
import unittest
import forecast2026 as f


def fixture():
    now = time.time()
    def stops(base, names): return [{'code': str(base+i), 'seq': 10+i, 'name': n, 'road': 'Rd', 'lat': 1.30+i*.002, 'lon': 103.80+i*.002, 'route_m': 1000+i*300} for i, n in enumerate(names)]
    ctx = {'105:1': stops(40240, ["St. Joseph's Instn", 'Opp Novena Sq', 'Aft Sixth Ave']),
           '105:2': stops(51101, ['Bef Thomson Flyover', 'Opp Thomson Plaza']),
           '167:1': stops(10000, ['Orchard Stn', 'Opp Ngee Ann City', 'Somerset Stn']),
           '190:1': stops(20000, ['Lucky Plaza', 'Orchard Stn', 'Opp Ngee Ann City'])}
    raw = {'age_s': 0, 'services': [{'svc': '105', 'dir': 2, 'blocked': True}, {'svc': '167', 'dir': 1}],
           'events': [{'id': 'E1', 'kind': 'road_closed', 'status': 'confirmed', 'road': 'Thomson Rd', 'pt': [103.83, 1.33], 'delay_s': None, 'services': [{'svc': '105', 'dir': 2, 'a_m': 1000, 'b_m': 1300}]},
                      {'id': 'E2', 'kind': 'roadworks', 'status': 'confirmed', 'road': 'Orchard Rd', 'pt': [103.83, 1.30], 'delay_s': 260, 'services': [{'svc': '167', 'dir': 1, 'a_m': 1000, 'b_m': 1300}, {'svc': '190', 'dir': 1, 'a_m': 1300, 'b_m': 1700}]},
                      {'id': 'E3', 'kind': 'incident', 'status': 'confirmed', 'road': 'Bt Timah Rd', 'pt': [103.81, 1.32], 'delay_s': None, 'services': [{'svc': '105', 'dir': 1, 'a_m': 1000, 'b_m': 1300}]}],
           'sources': {'tomtom_incidents': {'status': 'live', 'detail': 'fixture'}}, 'route_context': ctx}
    bus = lambda i, load, near, with_position=True: {'id': i, 'load': load, 'near': near, **({'lat': 1.31, 'lon': 103.81} if with_position else {})}
    samples = [{'service': '105', 'direction': 1, 'at': now, 'route_stops': ctx['105:1'], 'sched_hw': 10, 'hw': {'0': 1.1},
                'groups': [{'size': 3, 'status': 'confirmed', 'ids': ['B22', 'B30', 'B34'], 'location': "St. Joseph's Instn", 'stop_j': 0, 'headways': [1.1, 1.0]},
                           {'size': 3, 'status': 'confirmed', 'ids': ['B40', 'B41', 'B42'], 'location': 'Aft Sixth Ave', 'stop_j': 2, 'headways': [0.8, 1.2]}],
                'seq': [bus('B22', 'LSD', "St. Joseph's Instn", False), bus('B30', 'LSD', "St. Joseph's Instn"), bus('B34', 'SDA', 'Opp Novena Sq')]},
               {'service': '167', 'direction': 1, 'at': now, 'route_stops': ctx['167:1'], 'sched_hw': 12, 'hw': {'0': 19},
                'gaps': [{'active': True, 'max_hw': 19, 'location': 'Opp Ngee Ann City', 'stop_j': 1, 'lead': 'B7', 'foll': 'B9'}],
                'seq': [bus('B7', 'LSD', 'Orchard Stn', False), bus('B9', 'SEA', 'Opp Ngee Ann City', False)]}]
    return raw, samples


def map_markers(d):
    """Mirror of forecast2026-map.js marker rules."""
    pins = [(s, x) for s in d['services'] for x in (s.get('load_details') or []) if x.get('point')]
    covered = {(s['svc'], s['dir']) for s, _ in pins}
    count = {'loading': len(pins), 'traffic': sum(1 for e in d['events'] if e.get('point')), 'bunching': 0, 'gap': 0}
    for a in d['alerts']:
        if a['category'] in ('bunching', 'gap') and a.get('point'):
            count[a['category']] += 1
        if a['category'] == 'loading' and a.get('point') and (a['svc'], a['dir']) not in covered:
            count['loading'] += 1
    return count


class Tally(unittest.TestCase):
    def build(self, category='all', mode='live'):
        if mode == 'demo':
            return f.build_centre('demo', 30, 'combined', category=category, delay_min=.8)
        raw, samples = fixture()
        return f.build_centre('live', 0, raw=raw, samples=samples, category=category, delay_min=.8)

    def check(self, mode):
        full = self.build('all', mode)
        cards = {c: full['kpis'][c]['count'] for c in f.CATEGORIES}
        self.assertEqual(map_markers(full), cards, mode)
        for c in f.CATEGORIES:
            chosen = self.build(c, mode)
            shown = map_markers(chosen)
            self.assertEqual(shown[c], cards[c], (mode, c))                        # the selected card tallies with the map
            for other in f.CATEGORIES:
                if other != c:
                    self.assertEqual(shown[other], 0, (mode, c, other, shown))   # other categories leave the map
            self.assertTrue(all(a['category'] == c for a in chosen['alerts']))
            if c != 'traffic':
                self.assertEqual(chosen['events'], [])

    def test_live(self):
        self.check('live')

    def test_demo(self):
        self.check('demo')

    def test_markers_sit_at_the_problem_stop_not_the_incident(self):
        old, f.START_STOPS_IGNORED = f.START_STOPS_IGNORED, 0                       # fixture routes are only 3 stops long
        try:
            d = self.build('all')
        finally:
            f.START_STOPS_IGNORED = old
        bunch = sorted(a['point'] for a in d['alerts'] if a['category'] == 'bunching')
        self.assertEqual(len(bunch), 2)
        self.assertNotEqual(bunch[0], bunch[1])                                     # two groups, two places
        self.assertNotIn([103.81, 1.32], bunch)                                     # not stacked on the Bt Timah incident
        gap = next(a for a in d['alerts'] if a['category'] == 'gap')
        self.assertEqual(gap['location'], 'Opp Ngee Ann City')
        self.assertNotEqual(gap['point'], [103.83, 1.30])

    def test_traffic_card_counts_every_incident_shown(self):
        d = self.build('all')
        self.assertEqual(d['kpis']['traffic']['count'], 2)                          # roadworks + unknown-delay incident (closure with no delay hidden)
        self.assertEqual(d['kpis']['traffic']['services'], 2)                       # 105 and 167 (190 is not a watched service)


class StartStopGaps(unittest.TestCase):
    def test_long_gap_at_first_three_stops_is_dropped(self):
        stops = [{'code': str(i), 'seq': i + 1, 'name': 'S%d' % i, 'lat': 1.3, 'lon': 103.8, 'route_m': i * 300} for i in range(8)]
        self.assertTrue(f.near_start(stops, {'stop_j': 2}))
        self.assertFalse(f.near_start(stops, {'stop_j': 3}))
        self.assertTrue(f.near_start(stops, {'location': 'S1'}))
        self.assertFalse(f.near_start(stops, {'location': 'nowhere'}))                # unknown position is kept
        now = time.time()
        sample = {'service': '88', 'direction': 1, 'at': now, 'route_stops': stops, 'sched_hw': 10, 'hw': {'0': 20},
                  'gaps': [{'active': True, 'max_hw': 25, 'stop_j': 1, 'location': 'S1'}, {'active': True, 'max_hw': 22, 'stop_j': 5, 'location': 'S5'}]}
        d = f.build_centre('live', 0, raw={'age_s': 0, 'services': [{'svc': '88', 'dir': 1}], 'sources': {}}, samples=[sample])
        row = next(s for s in d['services'] if s['svc'] == '88')
        self.assertEqual(row['gaps'], 1)
        self.assertEqual(row['gap_details'][0]['location'], 'S5')


if __name__ == '__main__':
    unittest.main()


class BrowserFilterParity(unittest.TestCase):
    """The browser applies card selection locally (instant toggle); it must equal the server's category filter."""
    def test_category_view_matches_server(self):
        import json, subprocess, os
        here = os.path.dirname(os.path.abspath(__file__))
        for mode in ('live', 'demo'):
            if mode == 'demo':
                full = f.build_centre('demo', 30, 'combined', delay_min=.8)
                by = {c: f.build_centre('demo', 30, 'combined', category=c, delay_min=.8) for c in f.CATEGORIES}
            else:
                raw, samples = fixture()
                full = f.build_centre('live', 0, raw=raw, samples=samples, delay_min=.8)
                by = {c: f.build_centre('live', 0, raw=raw, samples=samples, category=c, delay_min=.8) for c in f.CATEGORIES}
            script = ("import('./forecast2026-core.js').then(m=>{const d=JSON.parse(require('fs').readFileSync(0,'utf8'));const out={};"
                      "for(const c of ['loading','traffic','bunching','gap']){const v=m.categoryView(d,c);out[c]={alerts:v.alerts.map(a=>a.id).sort(),events:v.events.map(e=>e.id).sort(),"
                      "services:v.services.map(s=>s.svc+':'+s.dir).sort(),buses:v.buses.map(b=>b.id).sort(),loads:v.services.reduce((n,s)=>n+(s.load_details||[]).length,0)};}"
                      "console.log(JSON.stringify(out));})")
            result = subprocess.run(['node', '-e', script], input=json.dumps(full, default=str), capture_output=True, text=True, cwd=here, check=True)
            browser = json.loads(result.stdout)
            for c, server in by.items():
                expected = {'alerts': sorted(a['id'] for a in server['alerts']), 'events': sorted(e['id'] for e in server['events']),
                            'services': sorted(f"{s['svc']}:{s['dir']}" for s in server['services']), 'buses': sorted(b['id'] for b in server['buses']),
                            'loads': sum(len(s.get('load_details') or []) for s in server['services'])}
                self.assertEqual(browser[c], expected, (mode, c))


class RoadNames(unittest.TestCase):
    def test_missing_road_and_stretch_are_filled_from_lta_stops(self):
        now = time.time()
        stops = [{'code': '51101', 'seq': 48, 'name': 'Bef Thomson Flyover', 'road': 'Thomson Rd', 'lat': 1.3301, 'lon': 103.8301, 'route_m': 900},
                 {'code': '51109', 'seq': 49, 'name': 'Opp Thomson Plaza', 'road': 'Upp Thomson Rd', 'lat': 1.36, 'lon': 103.83, 'route_m': 4000}]
        raw = {'age_s': 0, 'services': [{'svc': '506', 'dir': 1}],
               'events': [{'id': 'X', 'kind': 'accident', 'status': 'confirmed', 'road': None, 'pt': [103.83, 1.33], 'delay_s': 400,
                           'services': [{'svc': '506', 'dir': 1}]}],           # no road, no a_m / b_m from the provider
               'sources': {'tomtom_incidents': {'status': 'live'}}, 'route_context': {'506:1': stops}}
        d = f.build_centre('live', 0, raw=raw, samples=[{'service': '506', 'direction': 1, 'at': now, 'route_stops': stops, 'seq': []}], delay_min=.8)
        s = next(x for x in d['services'] if x['svc'] == '506')
        detail = s['traffic_details'][0]
        self.assertEqual(detail['road'], 'Thomson Rd')
        self.assertEqual([x['code'] for x in detail['stops']], ['51101'])           # nearest route stop, not "unavailable"
        self.assertEqual(d['events'][0]['road'], 'Thomson Rd')
        self.assertEqual(next(a for a in d['alerts'] if a['category'] == 'traffic')['location'], 'Thomson Rd')

    def test_far_event_is_not_attached_to_a_stop(self):
        stops = [{'code': '1', 'seq': 1, 'name': 'Far', 'road': 'Far Rd', 'lat': 1.40, 'lon': 103.90}]
        self.assertIsNone(f.nearest_stop(stops, [103.80, 1.30]))


class Simplify(unittest.TestCase):
    def test_keeps_bends_and_drops_straight_points(self):
        straight = [(1.30, 103.80 + i * 0.0001) for i in range(100)]
        bend = straight + [(1.30 + i * 0.0001, 103.8099) for i in range(1, 50)]
        out = f.simplify_latlon(bend, 5.0)
        self.assertEqual(out[0], [103.8, 1.3]); self.assertEqual(out[-1], [103.8099, 1.3049])
        self.assertLessEqual(len(out), 4)                                            # two straight legs -> corner kept
        self.assertTrue(any(abs(p[0] - 103.8099) < 2e-4 and abs(p[1] - 1.30) < 2e-4 for p in out))
