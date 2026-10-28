"""Threshold, camera and verified schedule contracts; no secrets or external calls."""
import ast
import asyncio
import math
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
import forecast2026 as model
from forecast2026_amendments import delay_threshold, filter_traffic, select_cameras, master_schedule
import test_forecast2026_api as api_test


class AmendmentPolicies(unittest.TestCase):
    def test_exact_thresholds_dedup_and_unknown_closure(self):
        snap={'mode':'live','events':[{'id':'a','kind':'jam','delay':.79},{'id':'b','kind':'jam','delay':.8},{'id':'b','kind':'jam','delay':.8},{'id':'c','kind':'road_closed','delay':None},{'id':'d','kind':'roadworks','delay':None}],
              'services':[{'svc':'1','delay':.79,'building':True},{'svc':'2','delay':.8},{'svc':'3','delay':None,'blocked':True}],
              'alerts':[{'id':'1','category':'traffic','delay':.79},{'id':'2','category':'traffic','delay':.8},{'id':'3','category':'traffic','delay':None,'blocked':True}]}
        out=filter_traffic(snap,.8)
        self.assertEqual([e['id'] for e in out['events']],['b','d'])            # a reported closure with no delay is not shown (buses may still pass)
        self.assertEqual([e['id'] for e in out['alerts']],['2'])
        self.assertFalse(out['services'][0]['traffic_eligible'])
        self.assertFalse(out['services'][0]['building'])
        self.assertIsNone(out['services'][0]['delay'])
        self.assertFalse(out['services'][2]['traffic_eligible'])
        self.assertFalse(out['services'][2]['blocked'])                          # 'BLOCKED' is retired
        self.assertFalse(out['events'][-1]['congestion_eligible'])

    def test_delay_filter_applies_to_roadworks_and_incidents_with_a_delay(self):
        events=[{'id':'w1','kind':'roadworks','delay':2.6},{'id':'w2','kind':'roadworks','delay':6.0},{'id':'i1','kind':'accident','delay':1.0},
                {'id':'i2','kind':'accident','delay':None},{'id':'c','kind':'road_closed','delay':0.5},{'id':'w3','kind':'roadworks','delay':None}]
        out=filter_traffic({'mode':'live','events':[dict(e) for e in events],'services':[],'alerts':[]},5)
        self.assertEqual([e['id'] for e in out['events']],['w2','i2','w3'])          # +2.6 roadworks and +1.0 accident hidden at >= 5 min
        out=filter_traffic({'mode':'live','events':[dict(e) for e in events],'services':[],'alerts':[]},0)
        self.assertEqual([e['id'] for e in out['events']],['w1','w2','i1','i2','c','w3'])   # All delays: the closure shows with its measured +0.5

    def test_lta_road_block_is_not_a_closure_and_is_hidden_without_a_delay(self):
        import forecast
        ev=forecast.parse_lta_incidents([{'Type':'Road Block','Latitude':1.34,'Longitude':103.77,'Message':'(28/10)11:30 Road Block on Toh Tuck Ave.'}])[0]
        self.assertEqual(ev['kind'],'hazard')
        self.assertTrue(ev['passable'])
        events=[{'id':'rb','kind':'hazard','delay':None},{'id':'rb2','kind':'hazard','delay':7.0},{'id':'x','kind':'other','delay':None},{'id':'a','kind':'accident','delay':None}]
        for minimum in (0,5):
            out=filter_traffic({'mode':'live','events':[dict(e) for e in events],'services':[],'alerts':[]},minimum)
            self.assertEqual([e['id'] for e in out['events']],['rb2','a'])

    def test_invalid_delay_values(self):
        for v in [-.1,181,math.nan,math.inf]:
            with self.assertRaises(ValueError):delay_threshold(v)

    def test_threshold_updates_all_horizons_but_not_other_engines(self):
        low=model.build_centre('demo',30,delay_min=0)
        high=model.build_centre('demo',30,delay_min=180)
        self.assertGreater(low['kpis']['traffic']['count'],0)
        self.assertEqual(high['kpis']['traffic']['count'],0)
        self.assertTrue(all(t['traffic']['count']==0 for t in high['timeline']))
        self.assertFalse(any(a['category']=='traffic' for a in high['alerts']))
        self.assertEqual(high['kpis']['loading'],low['kpis']['loading'])
        self.assertEqual(high['kpis']['bunching'],low['kpis']['bunching'])

    def test_current_minus_typical_when_explicitly_supplied(self):
        raw={'services':[{'svc':'14','dir':1}], 'age_s':0, 'sources':{'tomtom_incidents':{'status':'live'}},
             'events':[{'id':'e','kind':'jam','status':'confirmed','delay_s':600,'current_travel_time_s':180,'typical_travel_time_s':120,'services':[{'svc':'14','dir':1}]}]}
        d=model.build_centre('live',0,raw=raw,delay_min=.8)
        self.assertEqual(d['events'][0]['delay'],1)
        self.assertEqual(d['services'][0]['delay'],1)
        self.assertIn('historical typical',d['events'][0]['delay_basis'])
        self.assertEqual(model.build_centre('live',0,raw=raw,delay_min=2)['kpis']['traffic']['count'],0)

    def test_nearest_six_cameras_deduplicate_and_keep_stale(self):
        now=datetime(2026,10,27,6,tzinfo=timezone.utc).timestamp()
        cameras=[{'id':str(i),'lat':1.3,'lon':103.8+i*.01,'link':'https://example.test/'+str(i),'taken':'2026-10-27T06:00:00Z' if i==0 else '2026-10-27T05:00:00Z'} for i in range(6)]
        rows=select_cameras(cameras+[cameras[0]],[103.8,1.3],now)
        self.assertEqual([c['id'] for c in rows],['0','1','2','3','4','5'])
        self.assertEqual([c['id'] for c in select_cameras(cameras,[103.8,1.3],now,limit=4)],['0','1','2','3'])
        self.assertEqual(rows[0]['age_minutes'],0)
        self.assertFalse(rows[0]['stale'])
        self.assertTrue(rows[1]['stale'])
        self.assertEqual(rows[0]['distance_km'],0)
        self.assertTrue(select_cameras([dict(cameras[0],taken=None)],now=now)[0]['stale'])

    def test_master_does_not_fill_unconfigured_hours_or_other_directions(self):
        now=datetime(2026,10,27,6,tzinfo=timezone.utc).timestamp()
        rows=master_schedule([{'service':'14','direction':1,'day_type':'Weekday','from':'06:00','to':'07:00','hw':10}],{('14',1),('14',2)},now)
        self.assertEqual(len(rows),4)
        self.assertTrue(all(r['dir']==1 and r['verified'] for r in rows))
        self.assertTrue(all(datetime.fromtimestamp(r['at'],timezone.utc).hour==22 for r in rows))

    def test_camera_endpoint_reuses_shared_cache_and_explains_missing_key(self):
        module=ast.parse(Path(__file__).with_name('app.py').read_text())
        fn=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='fc26_camera_context');fn.decorator_list=[]
        calls=[]
        async def cameras():
            calls.append(True)
            return {'cams':[{'id':'1','lat':1.3,'lon':103.8,'link':'https://example.test/1','taken':None}],'source':'data.gov.sg'}
        context={'KEY':'','CACHE':{'cameras':(0,time.time(),{})},'cameras_state':cameras,'in_sg':lambda a,b:1<a<2 and 103<b<105,'JSONResponse':JSONResponse}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'<camera-adapter>','exec'),context)
        data=asyncio.run(context['fc26_camera_context'](lat=1.3,lon=103.8))
        self.assertEqual(len(data['cameras']),1)
        self.assertEqual(len(calls),1)
        self.assertIn('AccountKey',data['diagnostics'][0])
        self.assertTrue(data['cameras'][0]['stale'])
        self.assertEqual(asyncio.run(context['fc26_camera_context'](lat=999,lon=103.8)).status_code,400)


class AmendmentAPI(unittest.TestCase):
    setUp=api_test.PrototypeAPI.setUp
    tearDown=api_test.PrototypeAPI.tearDown
    request=api_test.PrototypeAPI.request
    def test_threshold_persists_alert_lifecycle_and_audit(self):
        import json
        low=json.loads(self.request('/api/forecast/centre','mode=demo&horizon=30&delay_min=0')[1])
        alert=next(a for a in low['alerts'] if a['category']=='traffic')
        self.request('/api/forecast/centre/alerts',method='POST',body={'namespace':low['namespace'],'id':alert['id'],'status':'acknowledged'})
        high=json.loads(self.request('/api/forecast/centre','mode=demo&horizon=30&delay_min=180')[1])
        self.assertEqual(high['kpis']['traffic']['count'],0)
        restored=json.loads(self.request('/api/forecast/centre','mode=demo&horizon=30&delay_min=0')[1])
        self.assertEqual(next(a['status'] for a in restored['alerts'] if a['id']==alert['id']),'acknowledged')
        for value in ['-1','181','nan','inf']:
            self.assertEqual(self.request('/api/forecast/centre','delay_min='+value)[0],400)

    def test_camera_context_reports_standalone_missing_adapter(self):
        import json
        data=json.loads(self.request('/api/forecast/centre/cameras')[1])
        self.assertEqual(data['cameras'],[])
        self.assertTrue(data['diagnostics'])


if __name__=='__main__':unittest.main()
