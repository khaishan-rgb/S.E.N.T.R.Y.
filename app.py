import os, math, asyncio
from pathlib import Path
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

app=FastAPI(title="SG Transport Pulse V3.1")
LTA="https://datamall2.mytransport.sg/ltaodataservice"
KEY=os.getenv("LTA_ACCOUNT_KEY","")
CACHE={}

def hdr(): return {"AccountKey":KEY,"accept":"application/json"}
async def get_lta(path, params=None):
    if not KEY: return {"value":[],"_error":"LTA_ACCOUNT_KEY is not configured"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r=await c.get(f"{LTA}/{path}",headers=hdr(),params=params)
            r.raise_for_status(); return r.json()
    except Exception as e: return {"value":[],"_error":str(e)}

async def paged(path,key,max_pages=50,params=None):
    if key in CACHE:return CACHE[key]
    rows=[]
    for skip in range(0,max_pages*500,500):
        p=dict(params or {}); p["$skip"]=skip
        d=await get_lta(path,p); b=d.get("value",[])
        rows+=b
        if len(b)<500:break
    CACHE[key]=rows
    return rows


async def paged_live(path,max_pages=60):
    rows=[]
    err=None
    for skip in range(0,max_pages*500,500):
        d=await get_lta(path,{"$skip":skip})
        if d.get("_error"):
            err=d.get("_error")
            break
        b=d.get("value",[])
        rows += b
        if len(b)<500: break
    return rows,err

def num(v):
    try:return float(v)
    except:return None
def hav(a,b):
    if None in a or None in b:return 999
    lat1,lon1,lat2,lon2=map(math.radians,[a[0],a[1],b[0],b[1]])
    q=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371*2*math.asin(math.sqrt(q))
def dist_to_route(lat,lon,pts):
    return min((hav((lat,lon),p) for p in pts),default=999)

@app.get("/",response_class=HTMLResponse)
async def home():return Path("index.html").read_text(encoding="utf-8")
@app.get("/api/health")
async def health():return {"online":True,"lta":bool(KEY),"version":"V3.1"}

@app.get("/api/search")
async def search(service:str="",stop:str="",direction:int=1):
    service=service.strip().upper(); stop=stop.strip()
    routes,stops=await asyncio.gather(paged("BusRoutes","routes"),paged("BusStops","stops"))
    sm={str(x.get("BusStopCode")):x for x in stops}
    route=[r for r in routes if service and str(r.get("ServiceNo","")).upper()==service and int(r.get("Direction") or 1)==direction]
    pts=[]
    for r in sorted(route,key=lambda x:x.get("StopSequence") or 0):
        z=sm.get(str(r.get("BusStopCode")),{})
        if z.get("Latitude") is not None:
            pts.append({"seq":r.get("StopSequence"),"code":r.get("BusStopCode"),"lat":z.get("Latitude"),"lon":z.get("Longitude"),"name":z.get("Description"),"road":z.get("RoadName")})
    dirs=sorted({int(r.get("Direction") or 1) for r in routes if service and str(r.get("ServiceNo","")).upper()==service})
    atstop=sorted({str(r.get("ServiceNo")) for r in routes if stop and str(r.get("BusStopCode"))==stop})
    arr={"Services":[]}
    if stop:
        arr=await get_lta("BusArrivalv3",{"BusStopCode":stop,**({"ServiceNo":service} if service else {})})
    return {"service":service,"stop":stop,"direction":direction,"availableDirections":dirs,"route":pts,"focus":sm.get(stop,{}),"servicesAtStop":atstop,"arrivals":arr.get("Services",arr.get("value",[])),"error":arr.get("_error")}

@app.get("/api/context")
async def context(service:str="",stop:str="",direction:int=1):
    base=await search(service,stop,direction)
    pts=[(num(x["lat"]),num(x["lon"])) for x in base["route"]]
    if base["focus"]:pts.append((num(base["focus"].get("Latitude")),num(base["focus"].get("Longitude"))))
    inc=await get_lta("TrafficIncidents")
    relevant=[]
    for x in inc.get("value",[]):
        lat=num(x.get("Latitude"));lon=num(x.get("Longitude"))
        if lat is not None and (not pts or dist_to_route(lat,lon,pts)<1.2):relevant.append(x)

    # Traffic Speed Bands v3: current speeds, refreshed by LTA about every 5 min.
    speed,speed_error=await paged_live("v3/TrafficSpeedBands",max_pages=60)
    bands=[]
    for x in speed:
        a=(num(x.get("StartLat")),num(x.get("StartLon"))); b=(num(x.get("EndLat")),num(x.get("EndLon")))
        if None in a or None in b:continue
        mid=((a[0]+b[0])/2,(a[1]+b[1])/2)
        if pts and dist_to_route(mid[0],mid[1],pts)>0.80:continue
        bands.append({"road":x.get("RoadName"),"band":x.get("SpeedBand"),"min":x.get("MinimumSpeed"),"max":x.get("MaximumSpeed"),"a":a,"b":b})
        if len(bands)>=1000:break
    return {**base,"incidents":relevant[:80],"speedBands":bands,"trafficDebug":{"rawSegments":len(speed),"matchedSegments":len(bands),"error":speed_error}}

@app.get("/api/traffic-test")
async def traffic_test():
    rows,err=await paged_live("v3/TrafficSpeedBands",max_pages=60)
    sample=rows[0] if rows else None
    counts={}
    for x in rows:
        k=str(x.get("SpeedBand"))
        counts[k]=counts.get(k,0)+1
    return {"working":bool(rows),"segments":len(rows),"speedBandCounts":counts,"sample":sample,"error":err}

@app.get("/api/stop-suggest")
async def stop_suggest(q:str=Query("")):
    q=q.strip().lower(); stops=await paged("BusStops","stops")
    out=[]
    for s in stops:
        if q in " ".join(str(s.get(k,"")) for k in ("BusStopCode","Description","RoadName")).lower():
            out.append(s)
            if len(out)>=12:break
    return out
