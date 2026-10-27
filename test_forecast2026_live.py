"""Verify main-app live wiring without importing provider/network startup code."""
import ast
import asyncio
import sqlite3
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi.responses import JSONResponse
import forecast2026 as model


class ExistingAppLiveWiring(unittest.TestCase):
    def endpoint(self, selected=None):
        module=ast.parse(Path(__file__).with_name('app.py').read_text())
        fn=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='fc26_centre')
        fn.decorator_list=[];calls=[]
        async def wake(services=''):
            calls.append(services);return {}
        async def traffic():
            self.assertTrue(calls,'LTA must be activated before traffic collection')
            return {'services':[],'sources':{},'error':'Traffic provider unavailable'}
        async def to_thread(f,*args,**kwargs):return f(*args,**kwargs)
        ledger=SimpleNamespace(sync=lambda *a,**k:None,view=lambda ns:([],[]))
        sample={'service':'156','direction':1,'at':time.time(),'issue':'No data','observed_buses':[{'id':1,'lat':1.3,'lon':103.8,'load':'LSD'}]}
        context={'FC26_STOP_CONTEXT':{},'fc26':model,'asyncio':SimpleNamespace(to_thread=to_thread),'api_forecast_state':traffic,'api_forecast_bunching':wake,'fc26_ledger':lambda:ledger,
                 'FC_SERVICES':[],'BB_DEFAULT':'14,166','BB':{'rows':{'156:1':sample}},'FC':{},'CACHE':{},'time':time,'sqlite3':sqlite3,'JSONResponse':JSONResponse}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='fc26_enrich_context'),fn],type_ignores=[])),'<live-wiring>','exec'),context)
        data=asyncio.run(context['fc26_centre'](**({'services':selected} if selected else {})))
        return data,calls

    def test_main_endpoint_defaults_to_live_now_and_seeds_lta_without_incidents(self):
        data,calls=self.endpoint()
        self.assertEqual(data['mode'],'live');self.assertEqual(data['horizon'],0)
        self.assertEqual(calls,['14,166','14,166'])
        self.assertEqual(len(data['buses']),1)
        self.assertEqual(data['kpis']['loading']['count'],1)
        self.assertFalse(data['coverage']['bunching'])

    def test_explicit_selection_wakes_that_service(self):
        data,calls=self.endpoint('156')
        self.assertEqual(calls,['156','156'])
        self.assertEqual(data['services'][0]['svc'],'156')

    def test_html_defaults_match_existing_live_setup(self):
        html=Path(__file__).with_name('forecast.html').read_text()
        self.assertIn('data-default-mode="live"',html)
        self.assertIn('data-default-horizon="0"',html)


if __name__=='__main__':unittest.main()
