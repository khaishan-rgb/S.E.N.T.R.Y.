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
        d = self.build('all')
        bunch = sorted(a['point'] for a in d['alerts'] if a['category'] == 'bunching')
        self.assertEqual(len(bunch), 2)
        self.assertNotEqual(bunch[0], bunch[1])                                     # two groups, two places
        self.assertNotIn([103.81, 1.32], bunch)                                     # not stacked on the Bt Timah incident
        gap = next(a for a in d['alerts'] if a['category'] == 'gap')
        self.assertEqual(gap['location'], 'Opp Ngee Ann City')
        self.assertNotEqual(gap['point'], [103.83, 1.30])

    def test_traffic_card_counts_every_incident_shown(self):
        d = self.build('all')
        self.assertEqual(d['kpis']['traffic']['count'], 3)                          # closure + roadworks + unknown-delay incident
        self.assertEqual(d['kpis']['traffic']['services'], 2)                       # 105 and 167 (190 is not a watched service)


if __name__ == '__main__':
    unittest.main()
