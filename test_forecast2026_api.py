"""Real ASGI integration tests: no listening socket, secrets or provider network."""
import asyncio
import json
import os
import tempfile
import unittest
import forecast2026_server as server


class PrototypeAPI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();os.environ['SENTRY2026_DB']=self.tmp.name+'/journal.db';server.ledger=None

    def tearDown(self):
        server.ledger=None;self.tmp.cleanup()

    def request(self,path,query='',method='GET',body=None):
        messages=[];payload=json.dumps(body or {}).encode()
        async def receive():return {'type':'http.request','body':payload,'more_body':False}
        async def send(message):messages.append(message)
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':query.encode(),'root_path':'','headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('127.0.0.1',8080)}
        asyncio.run(server.app(scope,receive,send))
        return messages[0]['status'],b''.join(m.get('body',b'') for m in messages[1:])

    def test_html_modules_and_styles_are_served(self):
        for path in ['/','/forecast','/forecast2026.css','/forecast2026.js','/forecast2026-core.js','/forecast2026-map.js','/map3d.js','/basemap.js']:
            status,body=self.request(path);self.assertEqual(status,200,path);self.assertTrue(body)

    def test_shared_horizon_and_filters(self):
        status,body=self.request('/api/forecast/centre','mode=demo&horizon=120&services=166&category=bunching')
        d=json.loads(body);self.assertEqual(status,200);self.assertEqual(d['horizon'],120)
        self.assertTrue(all(s['svc']=='166' for s in d['services']))
        self.assertTrue(all(a['category']=='bunching' for a in d['alerts']))
        self.assertEqual(len(d['timeline']),5)

    def test_invalid_horizon_and_mode(self):
        self.assertEqual(self.request('/api/forecast/centre','horizon=999')[0],400)
        self.assertEqual(self.request('/api/forecast/centre','mode=fake')[0],400)

    def test_acknowledgement_and_history_survive_refresh(self):
        d=json.loads(self.request('/api/forecast/centre')[1]);a=d['alerts'][0]
        status,body=self.request('/api/forecast/centre/alerts',method='POST',body={'namespace':d['namespace'],'id':a['id'],'status':'acknowledged'})
        self.assertEqual(status,200)
        refreshed=json.loads(self.request('/api/forecast/centre')[1]);self.assertEqual(next(x for x in refreshed['alerts'] if x['id']==a['id'])['status'],'acknowledged')
        self.assertTrue(any(x['action']=='acknowledged' for x in refreshed['audit']))

    def test_standalone_live_has_no_fabricated_data_or_camera_images(self):
        d=json.loads(self.request('/api/forecast/centre','mode=live&horizon=60')[1])
        self.assertEqual(d['mode'],'live');self.assertEqual(d['buses'],[]);self.assertEqual(d['cameras'],[])
        self.assertFalse(any(d['coverage'].values()));self.assertTrue(d['error'])
        self.assertEqual(json.loads(self.request('/api/cameras')[1])['cameras'],[])

    def test_invalid_transitions_never_dispatch(self):
        self.assertEqual(self.request('/api/forecast/centre/alerts',method='POST',body={'namespace':'demo:combined:30','id':'x','status':'dispatch'})[0],400)
        self.assertEqual(self.request('/api/forecast/centre/alerts',method='POST',body={'namespace':'../../x','id':'x','status':'resolved'})[0],400)


if __name__=='__main__':unittest.main()
