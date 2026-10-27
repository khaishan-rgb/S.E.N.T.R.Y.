"""Standalone demo: uvicorn forecast2026_server:app --host 0.0.0.0 --port 8080.
Use the existing app.py deployment for live public-provider integrations.
"""
import os
import re
import time
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import forecast2026 as model

ROOT=Path(__file__).resolve().parent
app=FastAPI(title='SENTRY Forecast Centre 2026 prototype')
ledger=None


def journal():
    global ledger
    if ledger is None:
        ledger=model.AlertLedger(os.getenv('SENTRY2026_DB',str(ROOT/'sentry2026.db')))
    return ledger


@app.get('/',response_class=HTMLResponse)
@app.get('/forecast',response_class=HTMLResponse)
async def index():
    return HTMLResponse((ROOT/'forecast.html').read_text().replace('data-default-mode="live"','data-default-mode="demo"').replace('data-default-horizon="0"','data-default-horizon="30"'))


@app.get('/forecast2026.css')
@app.get('/forecast2026.js')
@app.get('/forecast2026-map.js')
@app.get('/forecast2026-core.js')
@app.get('/forecast2026-tv.js')
@app.get('/map3d.js')
@app.get('/basemap.js')
async def asset(request: Request):
    name=request.url.path.rsplit('/',1)[-1]
    return Response((ROOT/name).read_text(),media_type='text/css' if name.endswith('.css') else 'application/javascript',headers={'Cache-Control':'no-cache'})


@app.get('/api/forecast/centre')
async def centre(mode:str='demo',horizon:int=30,scenario:str='combined',tick:int=0,services:str='',exclude_cbs:int=0,category:str='all',delay_min:float=.8):
    if mode not in ('live','demo') or horizon not in model.HORIZONS or scenario not in dict(model.SCENARIOS):
        return JSONResponse({'error':'Unsupported mode, horizon or scenario'},status_code=400)
    try:
        delay_min=model.delay_threshold(delay_min)
    except ValueError as error:
        return JSONResponse({'error':str(error)},status_code=400)
    ns=f'{mode}:{scenario if mode=="demo" else "network"}:{horizon}'
    raw={'error':'Standalone demo server: use the existing app.py deployment to connect public live providers.','age_s':999}
    data=model.build_centre(mode,horizon,scenario,max(0,tick),raw=raw,delay_min=0)
    journal().sync(ns,data['alerts'],time.time(),can_resolve=mode=='demo')
    history,audit=journal().view(ns);status={a['id']:a['status'] for a in history};journal_rows={a['id']:a for a in history}
    filtered=model.build_centre(mode,horizon,scenario,max(0,tick),raw=raw,services=[s for s in services.upper().split(',') if s],exclude_cbs=bool(exclude_cbs),category=category,delay_min=delay_min)
    for a in filtered['alerts']:
        a['status']=status.get(a['id'],'new')
        a.update({key:journal_rows[a['id']][key] for key in ('first',) if key in journal_rows.get(a['id'],{})})
    wanted={s for s in services.upper().split(',') if s}
    history=[a for a in history if (not wanted or a['svc'] in wanted) and (not exclude_cbs or a['svc'] not in model.CBS) and (category=='all' or a['category']==category) and (a['category']!='traffic' or model.meets_delay(a,delay_min))]
    return dict(filtered,history=history,audit=audit,namespace=ns,cameras=[])


@app.post('/api/forecast/centre/alerts')
async def transition(request:Request):
    body=await request.json();ns=str(body.get('namespace',''))
    if not re.fullmatch(r'(demo|live):[a-z_]+:(0|15|30|60|120)',ns):
        return JSONResponse({'error':'Invalid namespace'},status_code=400)
    try:
        ok=journal().transition(ns,str(body.get('id','')),str(body.get('status','')),'demo controller',time.time())
        return JSONResponse({'ok':ok},status_code=200 if ok else 404)
    except ValueError:
        return JSONResponse({'error':'Invalid transition'},status_code=400)


@app.get('/api/forecast/services')
async def services():
    return {'services':[{'svc':s,'dirs':[1],'operator':'SMRT' if s=='853' else 'SBST'} for s,_,_ in model.CORRIDORS],'demo':True}


@app.get('/api/forecast/centre/cameras')
@app.get('/api/cameras')
async def cameras():
    return {'cameras':[],'error':'No source credentials in the standalone demonstration. Start uvicorn app:app for LTA and data.gov.sg camera adapters.','diagnostics':['Standalone DEMO has no external camera adapter.']}
