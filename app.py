import os,time,asyncio
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

app=FastAPI(title="SG Transport Pulse")
BASE=Path(__file__).parent
LTA="https://datamall2.mytransport.sg/ltaodataservice"
OM="https://www.onemap.gov.sg"
LTA_KEY=os.getenv("LTA_ACCOUNT_KEY","")
OM_TOKEN=os.getenv("ONEMAP_TOKEN","")
OM_EMAIL=os.getenv("ONEMAP_EMAIL","")
OM_PASSWORD=os.getenv("ONEMAP_PASSWORD","")
client=httpx.AsyncClient(timeout=15)
cache={"token":OM_TOKEN or None,"exp":0}

async def lta(path,params=None):
    if not LTA_KEY:return {"_error":"LTA key not configured"}
    r=await client.get(f"{LTA}/{path}",params=params or {},headers={"AccountKey":LTA_KEY,"accept":"application/json"})
    r.raise_for_status();return r.json()

async def token(force=False):
    # A pasted token works immediately. For permanent auto-renewal, set email/password as secrets.
    if not force and cache["token"] and (cache["exp"]==0 or time.time()<cache["exp"]-3600): return cache["token"]
    if not OM_EMAIL or not OM_PASSWORD:
        if cache["token"]: return cache["token"]
        raise RuntimeError("OneMap token or login secrets not configured")
    r=await client.post(f"{OM}/api/auth/post/getToken",json={"email":OM_EMAIL,"password":OM_PASSWORD})
    r.raise_for_status();d=r.json();cache.update(token=d["access_token"],exp=int(d["expiry_timestamp"]));return cache["token"]

async def om(path,params=None):
    t=await token()
    r=await client.get(f"{OM}/{path}",params=params or {},headers={"Authorization":t})
    # OneMap search may report token expiry in JSON with HTTP 200, so check both.
    expired = r.status_code==401
    try:
        jd=r.json()
        expired = expired or ("error" in jd and "token" in str(jd["error"]).lower())
    except: jd=None
    if expired and OM_EMAIL and OM_PASSWORD:
        t=await token(True)
        r=await client.get(f"{OM}/{path}",params=params or {},headers={"Authorization":t})
    r.raise_for_status();return r.json()

@app.get("/")
async def home(): return FileResponse(BASE/"index.html")

@app.get("/api/health")
async def health(): return {"online":True,"lta":bool(LTA_KEY),"oneMap":bool(cache["token"] or (OM_EMAIL and OM_PASSWORD)),"autoRenew":bool(OM_EMAIL and OM_PASSWORD)}

@app.get("/api/bus")
async def bus(stop:str,service:Optional[str]=None):
    p={"BusStopCode":stop}
    if service:p["ServiceNo"]=service
    return await lta("v3/BusArrival",p)

@app.get("/api/incidents")
async def incidents(): return await lta("TrafficIncidents")

@app.get("/api/pulse")
async def pulse():
    calls=[("incidents","TrafficIncidents"),("roadworks","RoadWorks"),("lights","FaultyTrafficLights"),("floods","FloodAlerts"),("vms","VMS")]
    res=await asyncio.gather(*[lta(x[1]) for x in calls],return_exceptions=True)
    out={}
    for (name,_),v in zip(calls,res):
        if isinstance(v,Exception): out[name]={"ok":False,"count":0}
        else:
            arr=v.get("value",[]) if isinstance(v,dict) else []
            out[name]={"ok":True,"count":len(arr),"data":arr[:30]}
    score=min(100,out["incidents"]["count"]*5+out["lights"]["count"]*5+out["floods"]["count"]*12+min(out["roadworks"]["count"],10)*2)
    state="NORMAL" if score<20 else "WATCH" if score<45 else "DISRUPTED" if score<70 else "SEVERE"
    return {"score":score,"state":state,"feeds":out,"time":int(time.time())}

@app.get("/api/search")
async def search(q:str):
    return await om("api/common/elastic/search",{"searchVal":q,"returnGeom":"Y","getAddrDetails":"Y","pageNum":1})

@app.get("/api/route")
async def route(start:str,end:str,routeType:str="drive"):
    return await om("api/public/routingsvc/route",{"start":start,"end":end,"routeType":routeType})

@app.on_event("shutdown")
async def bye(): await client.aclose()
