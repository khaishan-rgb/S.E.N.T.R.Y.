import asyncio
import os
import tempfile
import unittest
from pathlib import Path
import forecast2026 as f


class ForecastBaselines(unittest.TestCase):
    def test_demand_conservation_capacity_and_missing_inputs(self):
        self.assertEqual(f.demand_forecast(70,100,2,1,15)['passengers'],85)
        self.assertEqual(f.demand_forecast(90,100,10,0,30)['occupancy'],100)
        self.assertEqual(f.demand_forecast(20,100,0,10,30)['passengers'],0)
        self.assertIsNone(f.demand_forecast(None,100,2,1,15))

    def test_headways_use_arrival_times_and_separate_two_three_four(self):
        def test(times):return f.headway_risks([{'id':str(i),'eta':t} for i,t in enumerate(times)],10)
        self.assertEqual(len(test([0,2])['two']),1)
        self.assertEqual(len(test([0,2,3])['three']),1)
        self.assertEqual(len(test([0,1,2,3])['three']),0)
        self.assertEqual(len(test([0,1,2,3])['four_plus']),1)
        self.assertEqual(len(test([0,14])['gaps']),0)
        self.assertEqual(test([0,15])['gaps'][0]['excess'],5)

    def test_all_scenarios_and_horizons(self):
        for scenario,_ in f.SCENARIOS:
            for h in f.HORIZONS:
                with self.subTest(scenario=scenario,h=h):
                    d=f.build_centre(horizon=h,scenario=scenario,now=1000)
                    self.assertEqual(d['mode'],'demo');self.assertEqual(len(d['timeline']),5)
                    self.assertTrue(all(b['id'].startswith('SIM-') for b in d['buses']))
                    self.assertTrue(all(a['evidence']=='demo' for a in d['alerts']))
                    self.assertFalse(d['model']['validated'])

    def test_combined_risks_evolve_and_buses_converge(self):
        now=f.build_centre(horizon=0);future=f.build_centre(horizon=30)
        self.assertEqual(now['kpis']['bunching']['count'],0)
        self.assertGreaterEqual(future['kpis']['bunching']['count'],1)
        self.assertEqual(set(a['category'] for a in future['alerts']),set(f.CATEGORIES))
        a=[b for b in now['buses'] if b['svc']=='166'];b=[b for b in future['buses'] if b['svc']=='166']
        distance=lambda bs:sum(abs(bs[0]['point'][i]-bs[2]['point'][i]) for i in (0,1))
        self.assertLess(distance(b),distance(a))

    def test_demo_incident_propagation_is_time_based(self):
        before=f.build_centre(horizon=0,scenario='accident')
        after=f.build_centre(horizon=30,scenario='accident')
        self.assertEqual(before['kpis']['gap']['count'],0)
        self.assertGreater(after['kpis']['gap']['count'],0)
        works=f.build_centre(horizon=30,scenario='roadworks')
        self.assertGreater(works['kpis']['bunching']['count'],0)

    def test_filters_propagate_through_map_alerts_and_timeline(self):
        d=f.build_centre(services=['166'],category='bunching')
        for key in ['services','routes','buses','alerts']:
            self.assertTrue(all(x['svc']=='166' for x in d[key]))
        self.assertEqual(d['timeline'][2]['bunching']['count'],1)
        self.assertTrue(all(a['category']=='bunching' for a in d['alerts']))

    def test_live_missing_data_never_becomes_zero_or_demo(self):
        d=f.build_centre(mode='live',horizon=120,raw={},samples=[],now=1000)
        self.assertFalse(any(d['coverage'].values()));self.assertEqual(d['buses'],[])
        self.assertTrue(all(not k['available'] for k in d['kpis'].values()))
        self.assertTrue(all(t['loading']['occupancy'] is None for t in d['timeline']))

    def test_live_stale_samples_and_unknown_loads(self):
        s={'service':'160','direction':2,'at':990,'counts':{'bb3':0},'seq':[{'id':1,'lat':1.3,'lon':103.8,'load':''}]}
        d=f.build_centre(mode='live',horizon=0,samples=[s],now=1000)
        self.assertFalse(d['coverage']['loading']);self.assertIsNone(d['services'][0]['occupancy'])
        s['seq'][0]['load']='LSD';s['at']=800
        d=f.build_centre(mode='live',horizon=0,samples=[s],now=1000)
        self.assertEqual(d['buses'],[]);self.assertFalse(d['coverage']['bunching'])

    def test_observed_buses_remain_visible_without_headway_model(self):
        sample={'service':'156','direction':1,'at':990,'issue':'No data','observed_buses':[{'id':1,'lat':1.3,'lon':103.8,'load':'LSD'}]}
        d=f.build_centre(mode='live',horizon=0,samples=[sample],now=1000)
        self.assertEqual(len(d['buses']),1);self.assertEqual(d['kpis']['loading']['count'],1)
        self.assertFalse(d['coverage']['bunching']);self.assertFalse(d['coverage']['gap'])

    def test_live_categorical_load_not_fabricated_percentage_or_forecast(self):
        s={'service':'160','direction':2,'at':990,'seq':[{'id':1,'lat':1.3,'lon':103.8,'load':'LSD'}]}
        d=f.build_centre(mode='live',horizon=0,samples=[s],now=1000)
        self.assertEqual(d['kpis']['loading']['count'],1)
        self.assertIsNone(d['kpis']['loading']['occupancy']);self.assertEqual(d['alerts'][0]['dir'],2)
        d=f.build_centre(mode='live',horizon=60,samples=[s],now=1000)
        self.assertFalse(d['coverage']['loading']);self.assertFalse(d['coverage']['bunching'])
        self.assertTrue(all(not b['predicted'] for b in d['buses']))

    def test_opposite_or_unverified_traffic_not_rendered(self):
        raw={'services':[{'svc':'14','dir':1,'extra':{30:{'mid':3}}}],'sources':{'tomtom_flow':{'status':'live'}},'events':[{'id':'x','kind':'accident','status':'no_route_impact','pt':[103.8,1.3]}]}
        d=f.build_centre(mode='live',raw=raw,now=1000)
        self.assertEqual(d['events'],[])
        raw['sources']['tomtom_flow']['status']='stale';d=f.build_centre(mode='live',raw=raw,now=1000)
        self.assertFalse(d['coverage']['traffic']);self.assertEqual(d['alerts'],[])

    def test_same_service_opposite_direction_does_not_inherit_incident(self):
        raw={'services':[{'svc':'14','dir':1,'extra':{30:{'mid':3}}}],'sources':{'tomtom_flow':{'status':'live'}},'events':[{'id':'e','kind':'accident','status':'confirmed','pt':[103.8,1.3],'delay_s':600,'services':[{'svc':'14','dir':1}]}]}
        samples=[{'service':'14','direction':2,'at':990,'seq':[]}]
        d=f.build_centre(mode='live',horizon=0,raw=raw,samples=samples,now=1000)
        opposite=next(s for s in d['services'] if s['dir']==2)
        self.assertEqual(opposite['delay'],0)
        self.assertTrue(all(a['dir']==1 for a in d['alerts']))

    def test_stop_sequence_uses_lta_sequence_not_array_order(self):
        stops=[{'code':'00123','name':'Stop A','seq':17,'route_m':0,'lat':1.3,'lon':103.8},
               {'code':'00456','name':'Stop B','seq':23,'route_m':400,'lat':1.3,'lon':103.804}]
        self.assertEqual(f.stop_context(stops,index=1)['sequence'],23)
        self.assertEqual(f.stop_context(stops,index=0)['code'],'00123')
        affected=f.impact_stops(stops,100,300)
        self.assertEqual([s['sequence'] for s in affected],[17,23])
        self.assertIsNone(f.stop_context(stops,location='Unknown'))
        self.assertIsNone(f.stop_context(stops+[dict(stops[0])],location='Stop A'))

    def test_bunching_stop_bus_order_traffic_context_and_pair_headway(self):
        stops=[{'code':'00123','name':'Stop A','seq':17,'route_m':0}, {'code':'00456','name':'Stop B','seq':23,'route_m':400}]
        sample={'service':'151','direction':2,'at':990,'sched_hw':10,'route_stops':stops,'hw':{'0':2,'15':1.5},'focus':{'lead':'B1','foll':'B2'},
                'groups':[{'size':3,'status':'confirmed','stop_j':1,'location':'Stop B','ids':['B1','B2','B3'],'headways':[2,1]}],
                'gaps':[{'active':True,'max_hw':24,'stop_j':0,'location':'Stop A','lead':'B0','foll':'B1'}],
                'seq':[{'id':'B1','lat':1.3,'lon':103.8,'load':'SDA'}]}
        raw={'services':[{'svc':'151','dir':2}], 'sources':{'tomtom_flow':{'status':'live'}},
             'events':[{'id':'e','kind':'jam','status':'confirmed','pt':[103.8,1.3],'road':'Example Road','delay_s':120,'services':[{'svc':'151','dir':2,'a_m':100,'b_m':300}]}]}
        old,f.START_STOPS_IGNORED=f.START_STOPS_IGNORED,0     # 2-stop fixture route
        try:d=f.build_centre(mode='live',horizon=0,raw=raw,samples=[sample],now=1000)
        finally:f.START_STOPS_IGNORED=old
        row=d['services'][0]
        self.assertEqual(row['bunching_details'][0]['stop']['sequence'],23)
        self.assertEqual(row['bunching_details'][0]['bus_order'],['B1','B2','B3'])
        self.assertEqual(row['traffic_details'][0]['stops'][0]['code'],'00123')
        self.assertEqual(row['focus_headway'],2);self.assertEqual(row['headway'],24)
        self.assertEqual(row['load_counts']['SDA'],1)

    def test_collector_headway_interpolation_is_bounded(self):
        self.assertEqual(f.headway_at({'0':10,'10':8,'20':6,'30':4},15),7)
        self.assertIsNone(f.headway_at({'0':10,'30':4},60))
        self.assertEqual(f.group_headways({'headway_forecasts':{'10':[2,3],'20':[1,2]}},15),[1.5,2.5])

    def test_unsupported_horizon_and_recommendations_never_execute(self):
        with self.assertRaises(ValueError):f.build_centre(horizon=90)
        d=f.build_centre();self.assertTrue(all(r['approval']=='Controller approval required' for r in d['recommendations']))
        self.assertTrue(all('DEMO' in r['constraints'] for r in d['recommendations']))
        self.assertIsNone(f.compare_options({'headway':24})[1]['headway'])

    def test_counterfactual_constraints_and_accuracy_metrics(self):
        a={'headway':24}
        safe={'spare_buses':1,'layover':10,'minimum_layover':7,'meal_protected':True}
        self.assertEqual(f.compare_options(a,safe)[1]['headway'],12)
        self.assertIsNone(f.compare_options(a,dict(safe,meal_protected=False))[1]['headway'])
        self.assertIsNone(f.outcome_metrics([])['precision'])
        m=f.outcome_metrics([{'predicted':10,'actual':12,'alert':True,'actual_event':True,'probability':.8},{'predicted':5,'actual':7,'alert':True,'actual_event':False,'probability':.6}])
        self.assertEqual(m['mae'],2);self.assertEqual(m['precision'],.5)
        self.assertAlmostEqual(m['brier'],.2)


class AlertLifecycle(unittest.TestCase):
    def test_dedupe_ack_monitor_resolve_realert_and_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=f.AlertLedger(Path(tmp)/'a.db');a=f.alert('traffic','14','AYE','Delay',30,[103.8,1.3],'Verified route')
            ledger.sync('demo:combined:30',[a],1000);ledger.sync('demo:combined:30',[a],1010)
            records,audit=ledger.view('demo:combined:30');self.assertEqual(len(audit),1)
            self.assertTrue(ledger.transition('demo:combined:30',a['id'],'acknowledged','test',1020))
            ledger.sync('demo:combined:30',[a],1030);self.assertEqual(ledger.view('demo:combined:30')[0][0]['status'],'acknowledged')
            ledger.transition('demo:combined:30',a['id'],'monitoring','test',1040)
            self.assertEqual(ledger.view('demo:combined:30')[0][0]['status'],'monitoring')
            ledger.sync('demo:combined:30',[],1050,can_resolve=False);self.assertNotEqual(ledger.view('demo:combined:30')[0][0]['status'],'resolved')
            ledger.sync('demo:combined:30',[],1060,can_resolve=True);self.assertEqual(ledger.view('demo:combined:30')[0][0]['status'],'resolved')
            ledger.sync('demo:combined:30',[a],1070);self.assertEqual(ledger.view('demo:combined:30')[0][0]['status'],'new')
            self.assertEqual(ledger.view('live:network:30')[0],[])

    def test_worsening_realerts_and_invalid_actions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=f.AlertLedger(Path(tmp)/'a.db');a=f.alert('gap','14','Bedok','Gap',15,None,'Traffic')
            ledger.sync('live:network:15',[a],1000);ledger.transition('live:network:15',a['id'],'acknowledged','test',1001)
            a['score']+=20;ledger.sync('live:network:15',[a],1002)
            self.assertEqual(ledger.view('live:network:15')[0][0]['status'],'new')
            with self.assertRaises(ValueError):ledger.transition('live:network:15',a['id'],'dispatch','test',1003)


if __name__=='__main__':unittest.main()
