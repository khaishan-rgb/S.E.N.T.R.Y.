import unittest
import bunching


class GroupMetadata(unittest.TestCase):
    def test_group_preserves_actual_stop_index_order_and_pair_separations(self):
        vecs=[{'id':x,'j0':1,'near':'Stop B'} for x in ['B1','B2','B3']]
        pairs=[{'now':n,'min_hw':n,'hws':{0:n,10:n-.2,20:n-.4,30:n-.5},'smap':{1:1,2:1},'travelled':0} for n in [1,2]]
        groups=bunching.build_groups(vecs,pairs,dict(bunching.PARAMS,confirm_stops=2),10,['Stop A','Stop B','Stop C'])
        self.assertEqual(groups[0]['stop_j'],1)
        self.assertEqual(groups[0]['ids'],['B1','B2','B3'])
        self.assertEqual(groups[0]['headways'],[1,2])
        self.assertEqual(groups[0]['location'],'Stop B')
        self.assertEqual(groups[0]['headway_forecasts']['10'],[.8,1.8])


class GapSkipStops(unittest.TestCase):
    def test_long_headway_at_the_first_three_stops_is_not_a_gap(self):
        lead,foll={'id':'L','j0':0},{'id':'F','j0':0}
        P=dict(bunching.PARAMS,gap_stops=2)
        early=[(0,25,0),(1,25,2),(2,25,4),(3,8,6),(4,8,8)]                       # long only at stops 1-3
        a=bunching.analyse_pair(lead,foll,early,10,P,{})
        self.assertIsNone(a['gap_state']);self.assertFalse(a['long_now'])
        b=bunching.analyse_pair(lead,foll,early,10,dict(P,gap_skip_stops=0),{})
        self.assertEqual(b['gap_state'],'confirmed');self.assertTrue(b['long_now'])
        later=[(2,8,0),(3,25,2),(4,26,4),(5,8,6)]                                # long from stop 4: still a gap (predicted)
        c=bunching.analyse_pair(lead,foll,later,10,P,{})
        self.assertEqual(c['gap_state'],'developing');self.assertEqual(c['t_gap'],2);self.assertEqual(c['gap_max_at'],4)


if __name__=='__main__':unittest.main()
