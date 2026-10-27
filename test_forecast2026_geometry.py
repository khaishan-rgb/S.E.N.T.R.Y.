import unittest
import forecast as F


class RouteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.points=[(1.334,103.86),(1.334,103.87),(1.334,103.88)]
        self.route=F.Line(self.points)

    def test_same_direction_overlap_confirmed(self):
        self.assertEqual(F.match_line(self.route,F.Line(self.points))['status'],'confirmed')

    def test_opposite_carriageway_not_counted(self):
        match=F.match_line(self.route,F.Line(list(reversed(self.points))))
        self.assertEqual(match['status'],'rejected')
        self.assertEqual(match['reason'],'opposite_direction')

    def test_parallel_road_not_counted(self):
        other=F.Line([(a+.0003,b) for a,b in self.points])
        self.assertEqual(F.match_line(self.route,other)['status'],'rejected')

    def test_crossing_road_not_counted(self):
        other=F.Line([(1.33,103.87),(1.34,103.87)])
        self.assertEqual(F.match_line(self.route,other)['status'],'rejected')

    def test_point_report_never_a_verified_route_disruption(self):
        self.assertEqual(F.match_point(self.route,(1.334,103.87))['status'],'unverified')


if __name__=='__main__':unittest.main()
