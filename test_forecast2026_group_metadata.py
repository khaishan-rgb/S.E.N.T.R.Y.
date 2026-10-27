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


if __name__=='__main__':unittest.main()
