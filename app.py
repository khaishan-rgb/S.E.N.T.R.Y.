import os, math, asyncio
from pathlib import Path
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

app=FastAPI(title="SG Transport Pulse V3")
LTA="https://datamall2.mytransport.sg/ltaodataservice"
KEY=os.getenv("LTA_ACCOUNT_KEY","")
CACHE={}

def hdr(): return {"AccountKey":KEY,"accept":"application/json"}

async def get_lta(path, params=None):
    if not KEY: return {"value":[],"_error":"LTA_ACCOUNT_KEY is not configured in Render"}
    try:
        async with httpx.AsyncClient(timeout=18) as c:
            r=await c.get(f"{LTA}/{path}",headers=hdr(),params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e: return {"value":[],"_error":str(e)}

async def paged(path, cache_key, max_pages=40):
    # Static-ish datasets are cached in the Render process.
    if cache_key in CACHE: return CACHE[cache_key]
    rows=[]
    for skip in range(0,max_pages*500,500):
        d=await get_lta(path,{"$skip":skip})
        batch=d.get("value",[])
        rows += batch
        if len(batch)<500: break
    CACHE[cache_key]=rows
    return rows

def fnum(v):
    try: return float(v)
    except: return None

def hav(a,b):
    if None in a or None in b: return 999
    lat1,lon1,lat2,lon2=map(math.radians,[a[0],a[1],b[0],b[1]])
    x=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371*2*math.asin(math.sqrt(x))

@app.get("/",response_class=HTMLResponse)
async def home():
    return Path("index.html").read_text(encoding="utf-8")

@app.get("/api/health")
async def health():
    return {"online":True,"lta":bool(KEY),"version":"V3"}

@app.get("/api/network")
async def network():
    inc, works, lights, flood = await asyncio.gather(
        get_lta("TrafficIncidents"), get_lta("RoadWorks"),
        get_lta("FaultyTrafficLights"), get_lta("FloodAlert")
    )
    return {"incidents":inc.get("value",[]),"roadworks":works.get("value",[]),
            "lights":lights.get("value",[]),"flood":flood.get("value",[])}

@app.get("/api/search")
async def search(service:str="", stop:str=""):
    service=service.strip().upper()
    stop=stop.strip()
    routes, stops = await asyncio.gather(paged("BusRoutes","routes"),paged("BusStops","stops"))
    stopmap={str(x.get("BusStopCode")):x for x in stops}
    matching=[r for r in routes if (not service or str(r.get("ServiceNo","")).upper()==service)]
    if stop:
        matching=[r for r in matching if str(r.get("BusStopCode"))==stop or (service and str(r.get("ServiceNo","")).upper()==service)]
    services_at_stop=sorted({str(r.get("ServiceNo")) for r in routes if stop and str(r.get("BusStopCode"))==stop})
    if stop and not service:
        matching=[r for r in routes if str(r.get("BusStopCode"))==stop]

    # Full route geometry for selected service, not just queried stop.
    route_rows=[r for r in routes if service and str(r.get("ServiceNo","")).upper()==service]
    directions={}
    for r in route_rows:
        d=str(r.get("Direction","1"))
        s=stopmap.get(str(r.get("BusStopCode")),{})
        directions.setdefault(d,[]).append({
            "seq":r.get("StopSequence"),"code":r.get("BusStopCode"),
            "lat":s.get("Latitude"),"lon":s.get("Longitude"),
            "name":s.get("Description"),"road":s.get("RoadName")
        })
    for d in directions: directions[d].sort(key=lambda x:x.get("seq") or 0)

    arrivals={"Services":[]}
    if stop:
        arrivals=await get_lta("BusArrivalv3",{"BusStopCode":stop, **({"ServiceNo":service} if service else {})})

    focus=stopmap.get(stop,{}) if stop else {}
    return {"service":service,"stop":stop,"focus":focus,"servicesAtStop":services_at_stop,
            "directions":directions,"arrivals":arrivals.get("Services",arrivals.get("value",[])),
            "error":arrivals.get("_error")}

@app.get("/api/context")
async def context(service:str="", stop:str=""):
    base=await search(service,stop)
    inc=await get_lta("TrafficIncidents")
    # Keep only incidents close to selected route/stop when coordinates exist.
    pts=[]
    for arr in base["directions"].values():
        pts += [(fnum(x["lat"]),fnum(x["lon"])) for x in arr if fnum(x["lat"]) is not None]
    if base["focus"]:
        pts.append((fnum(base["focus"].get("Latitude")),fnum(base["focus"].get("Longitude"))))
    relevant=[]
    for x in inc.get("value",[]):
        lat=fnum(x.get("Latitude")); lon=fnum(x.get("Longitude"))
        if not pts or (lat is not None and any(hav((lat,lon),p)<1.5 for p in pts)):
            relevant.append(x)
    return {**base,"incidents":relevant[:80]}

@app.get("/api/stop-suggest")
async def stop_suggest(q:str=Query("")):
    q=q.strip().lower()
    stops=await paged("BusStops","stops")
    if not q:return []
    out=[]
    for s in stops:
        blob=" ".join(str(s.get(k,"")) for k in ("BusStopCode","Description","RoadName")).lower()
        if q in blob:
            out.append(s)
            if len(out)>=12:break
    return out
