import os, time, asyncio
from datetime import datetime, timezone
from typing import Any
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app=FastAPI(title='SG Transport Pulse V2')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
LTA='https://datamall2.mytransport.sg/ltaodataservice'
CACHE={}

def h(): return {'AccountKey':os.getenv('LTA_ACCOUNT_KEY',''),'accept':'application/json'}

def cached(key, ttl=30):
    x=CACHE.get(key)
    return x[1] if x and time.time()-x[0] < ttl else None

def put(key,val): CACHE[key]=(time.time(),val); return val

async def lta(path, params=None, ttl=30):
    key=(path,tuple(sorted((params or {}).items())))
    c=cached(key,ttl)
    if c is not None:return c
    if not os.getenv('LTA_ACCOUNT_KEY'): return {'value':[],'_error':'LTA_ACCOUNT_KEY not configured'}
    try:
        async with httpx.AsyncClient(timeout=18) as client:
            r=await client.get(f'{LTA}/{path}',headers=h(),params=params); r.raise_for_status(); return put(key,r.json())
    except Exception as e:return {'value':[],'_error':str(e)}

async def lta_pages(path, max_pages=4, ttl=45):
    key=('pages',path,max_pages); c=cached(key,ttl)
    if c is not None:return c
    out=[]; err=None
    for n in range(max_pages):
        d=await lta(path, {'$skip':n*500}, ttl=ttl)
        if d.get('_error'): err=d['_error']; break
        batch=d.get('value',[]); out.extend(batch)
        if len(batch)<500: break
    return put(key,{'value':out,'_error':err,'_truncated':len(out)>=max_pages*500})

async def public_json(url, ttl=120):
    c=cached(url,ttl)
    if c is not None:return c
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r=await client.get(url); r.raise_for_status(); return put(url,r.json())
    except Exception as e:return {'data':{},'_error':str(e)}

@app.get('/',response_class=HTMLResponse)
async def home():
    with open('index.html',encoding='utf-8') as f:return f.read()

@app.get('/api/health')
async def health(): return {'online':True,'lta':bool(os.getenv('LTA_ACCOUNT_KEY')),'version':'2.0','time':datetime.now(timezone.utc).isoformat()}

@app.get('/api/bus')
async def bus(stop:str=Query(...), service:str|None=None):
    p={'BusStopCode':stop}
    if service:p['ServiceNo']=service
    return await lta('v3/BusArrival',p,15)

@app.get('/api/incidents')
async def incidents(): return await lta('TrafficIncidents',ttl=30)
@app.get('/api/roadworks')
async def roadworks(): return await lta_pages('RoadWorks',4,90)
@app.get('/api/lights')
async def lights(): return await lta('FaultyTrafficLights',ttl=60)
@app.get('/api/flood')
async def flood(): return await lta('FloodAlert',ttl=60)
@app.get('/api/vms')
async def vms(): return await lta('VMS',ttl=30)
@app.get('/api/speed')
async def speed(): return await lta_pages('TrafficSpeedBandsv2',4,45)
@app.get('/api/travel')
async def travel(): return await lta('EstTravelTimes',ttl=45)
@app.get('/api/cameras')
async def cameras(): return await lta('Traffic-Imagesv2',ttl=30)
@app.get('/api/weather')
async def weather():
    rain,forecast=await asyncio.gather(
        public_json('https://api-open.data.gov.sg/v2/real-time/api/rainfall',90),
        public_json('https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast',300)
    )
    return {'rainfall':rain,'forecast':forecast}

@app.get('/api/overview')
async def overview():
    inc,lights,flood,vms,travel=await asyncio.gather(incidents(),lights(),flood(),vms(),travel())
    I=inc.get('value',[]); L=lights.get('value',[]); F=flood.get('value',[]); V=vms.get('value',[]); T=travel.get('value',[])
    # Transparent operational signal, not an official LTA metric.
    penalty=min(70,len(I)*1.2+len(L)*3+len(F)*8)
    score=max(30,round(100-penalty))
    level='STABLE' if score>=85 else 'WATCH' if score>=70 else 'DISRUPTED'
    return {'score':score,'level':level,'incidents':len(I),'faultyLights':len(L),'floodAlerts':len(F),'vms':len(V),'travelSegments':len(T),'updated':datetime.now().astimezone().isoformat()}
