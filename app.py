"""SG Transport Pulse V5 - FastAPI backend.

Data sources
  LTA DataMall  (needs LTA_ACCOUNT_KEY): bus stops/routes, bus arrival, traffic speed bands, incidents
  NEA via data.gov.sg (no key needed):   real-time rainfall
  OSRM (public demo by default):         snaps the stop-to-stop route onto roads (optional, falls back)

LTA endpoint paths follow the DataMall API User Guide v6.9 (3 Aug 2026):
  v4/TrafficSpeedBands   (was probed as v3/... before, which now returns 404)
  v3/BusArrival          (was called BusArrivalv3, which is not a real path)
"""
import os, re, math, time, asyncio, json, hashlib, copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
# V17.2 self-check: a stray file named httpx.py next to app.py (e.g. a test stand-in) replaces the real library and cuts every live feed.
HTTPX_STUB = not hasattr(httpx, "__version__")
HTTPX_PROBLEM = ("Deployment problem: the web library 'httpx' is not the real one (" + str(getattr(httpx, "__file__", "?")) + "). "
                 "Delete the stray httpx.py next to app.py in the repository and redeploy: until then no live feed can be reached.") if HTTPX_STUB else None
if HTTPX_STUB:
    print("ERROR " + HTTPX_PROBLEM, flush=True)
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import headway
import routegeom
import traffic

VERSION = "V17.3"
# V16.8: pages hidden for everyone (see Settings > Pages). Defined here because bb_init() reads the saved value while the module loads.
SITE_PAGE_IDS = ("occlive", "command", "route", "headway", "bunching", "recovery", "halfplan", "trafficaware", "running", "stoprt", "ewt", "cameras", "diversion")     # "settings" can never be hidden
SITE = {"hidden": [x.strip() for x in os.getenv("HIDDEN_PAGES", "").split(",") if x.strip() in SITE_PAGE_IDS], "block": True}
LTA = os.getenv("LTA_BASE", "https://datamall2.mytransport.sg/ltaodataservice").rstrip("/")
KEY = os.getenv("LTA_ACCOUNT_KEY", "")
OSRM = os.getenv("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
DATAGOV_KEY = (os.getenv("DATAGOV_API_KEY") or os.getenv("DATA_GOV_SG_KEY") or "").strip()  # optional, raises data.gov.sg rate limits (either name works)
SGT = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent

EP_BUS_STOPS = "BusStops"
EP_BUS_ROUTES = "BusRoutes"
EP_ARRIVAL = os.getenv("LTA_ARRIVAL_PATH", "v3/BusArrival")
EP_INCIDENTS = "TrafficIncidents"
# Probed in order, and only moves to the next candidate on a 404. The working path is remembered.
SPEED_PATHS = [p for p in [os.getenv("LTA_SPEED_PATH", "").strip("/ "), "v4/TrafficSpeedBands", "v3/TrafficSpeedBands", "TrafficSpeedBandsv2"] if p]
SPEED_PATHS = list(dict.fromkeys(SPEED_PATHS))  # de-duplicate, keep order (LTA_SPEED_PATH, if set, is tried first)
GOOD_SPEED_PATH = None

# Tunables
TTL_STATIC = 12 * 3600      # bus stops / routes
TTL_BANDS = 300             # LTA refreshes speed bands about every 5 minutes
TTL_ARRIVAL = 15            # LTA refreshes bus arrival about every 20 seconds
TTL_INCIDENTS = 60
TTL_RAIN = 300
TTL_GEOM = 24 * 3600
TTL_NEGATIVE = 30           # after a failed fetch, wait this long before hitting LTA again
MATCH_MAX_M = 45.0          # max distance from route to an LTA road link to count as "same road"
MATCH_MAX_ANGLE = 40.0      # max bearing difference (undirected) between route and road link
BRIDGE_M = 120.0            # unmatched stretches shorter than this (junctions) borrow the neighbour's band
DWELL_MIN = 0.33            # assumed minutes lost per bus stop in the travel-time estimate
MAX_SAMPLE_STOPS = 14       # how many stops along a route we query to find live buses
GRID = 0.005                # spatial index cell, about 550 m

_client = None
_sem = None


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    return _client


def sem():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(12)
    return _sem


# --------------------------------------------------------------------------- caching
CACHE = {}   # key -> (expires_at, stored_at, value)
LOCKS = {}


async def cached(key, factory):
    """factory() -> (value, ttl_seconds, ok). Failures are never cached over good data."""
    now = time.time()
    hit = CACHE.get(key)
    if hit and hit[0] > now:
        return hit[2]
    lock = LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.time()
        hit = CACHE.get(key)
        if hit and hit[0] > now:
            return hit[2]
        val, ttl, ok = await factory()
        if ok:
            CACHE[key] = (now + ttl, now, val)
            return val
        if hit:  # refresh failed: keep serving the last good data, retry shortly
            CACHE[key] = (now + TTL_NEGATIVE, hit[1], hit[2])
            return hit[2]
        CACHE[key] = (now + TTL_NEGATIVE, now, val)  # short negative cache, avoids hammering LTA
        return val


def cache_age(key):
    hit = CACHE.get(key)
    return int(time.time() - hit[1]) if hit else None


# --------------------------------------------------------------------------- LTA access
async def get_lta(path, params=None, retries=1):
    if not KEY:
        return {"value": [], "_error": "LTA_ACCOUNT_KEY is not configured", "_status": 0}
    last = None
    for attempt in range(retries + 1):
        try:
            async with sem():
                r = await client().get(f"{LTA}/{path}", headers={"AccountKey": KEY, "accept": "application/json"}, params=params)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            j = r.json()
            return j if isinstance(j, dict) else {"value": j}
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            last = {"value": [], "_error": f"{path}: HTTP {code}", "_status": code}
            if code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = {"value": [], "_error": f"{path}: {type(e).__name__}", "_status": -1}
            if attempt < retries:
                await asyncio.sleep(0.6)
    return last


async def fetch_pages(path, start=0, page=500, wave=8, max_pages=400, params=None):
    """Fetch $skip pages in parallel waves. Returns (rows, error_or_None)."""
    rows = []
    skip = start
    limit = start + max_pages * page
    while skip < limit:
        res = await asyncio.gather(*[get_lta(path, {**(params or {}), "$skip": skip + i * page}) for i in range(wave)])
        for d in res:
            if d.get("_error"):
                return rows, d["_error"]
            b = d.get("value", [])
            rows += b
            if len(b) < page:
                return rows, None
        skip += wave * page
    return rows, None


# --------------------------------------------------------------------------- small helpers
def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def hav_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    q = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(q))


def bearing(lat1, lon1, lat2, lon2):
    kx = math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2((lon2 - lon1) * kx, lat2 - lat1)) % 360


def in_sg(lat, lon):
    return lat is not None and lon is not None and 1.1 < lat < 1.5 and 103.5 < lon < 104.2


def band_class(b):
    if b is None:
        return "none"
    return "smooth" if b >= 5 else ("moderate" if b >= 3 else "slow")


def speed_of(band, mn, mx):
    """A representative km/h for a band, used only for the travel-time estimate."""
    mn, mx = num(mn), num(mx)
    if band >= 8:
        return 75.0
    if mn is None:
        mn = max(0, (band - 1) * 10)
    if mx is None or mx < mn or mx > 130:
        mx = mn + 9
    return max(5.0, (mn + mx) / 2)


def pct_split(vals):
    tot = sum(vals)
    if tot <= 0:
        return [0] * len(vals)
    raw = [v * 100 / tot for v in vals]
    fl = [int(x) for x in raw]
    for i in sorted(range(len(vals)), key=lambda i: raw[i] - fl[i], reverse=True)[: 100 - sum(fl)]:
        fl[i] += 1
    return fl


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def now_sgt():
    return datetime.now(SGT)


# --------------------------------------------------------------------------- static data: stops & routes
async def _load_static():
    (stop_rows, e1), (route_rows, e2) = await asyncio.gather(fetch_pages(EP_BUS_STOPS), fetch_pages(EP_BUS_ROUTES))
    stops = {}
    for s in stop_rows:
        lat, lon = num(s.get("Latitude")), num(s.get("Longitude"))
        code = str(s.get("BusStopCode", "")).strip()
        if code and in_sg(lat, lon):
            stops[code] = {"code": code, "name": s.get("Description") or code, "road": s.get("RoadName") or "", "lat": lat, "lon": lon}
    routes, dirs, at_stop = {}, {}, {}
    for r in route_rows:
        svc = str(r.get("ServiceNo", "")).strip().upper()
        code = str(r.get("BusStopCode", "")).strip()
        try:
            d = int(r.get("Direction") or 1)
        except (TypeError, ValueError):
            d = 1
        if not svc or not code:
            continue
        routes.setdefault((svc, d), []).append({"seq": r.get("StopSequence") or 0, "code": code, "dist": num(r.get("Distance"))})
        dirs.setdefault(svc, set()).add(d)
        at_stop.setdefault(code, set()).add(svc)
    for k in routes:
        routes[k].sort(key=lambda x: x["seq"])
    err = e1 or e2
    ok = bool(stops) and bool(routes) and not err
    data = {"stops": stops, "routes": routes, "dirs": {k: sorted(v) for k, v in dirs.items()}, "at_stop": at_stop, "error": err}
    return data, TTL_STATIC, ok


async def static():
    return await cached("static", _load_static)


def route_stops(st, svc, direction):
    out = []
    for r in st["routes"].get((svc, direction), []):
        s = st["stops"].get(r["code"])
        if s:
            out.append({**s, "seq": r["seq"], "dist": r["dist"]})
    return out


# --------------------------------------------------------------------------- traffic speed bands
def norm_segment(x):
    """One LTA speed-band record -> (alat, alon, blat, blon, band, road, min, max) or None."""
    alat, alon, blat, blon = num(x.get("StartLat")), num(x.get("StartLon")), num(x.get("EndLat")), num(x.get("EndLon"))
    if alat is None and x.get("Location"):  # legacy feed: "startLat startLon endLat endLon"
        vals = [float(v) for v in re.findall(r"-?\d+\.\d+", str(x["Location"]))]
        if len(vals) >= 4:
            alat, alon, blat, blon = vals[:4]
    band = num(x.get("SpeedBand", x.get("Band")))
    if band is None or not (in_sg(alat, alon) and in_sg(blat, blon)):
        return None
    return (alat, alon, blat, blon, int(band), x.get("RoadName") or "", x.get("MinimumSpeed"), x.get("MaximumSpeed"), x.get("RoadCategory"))


class BandIndex:
    """Grid index of LTA road links so a route point can find its road link quickly."""

    def __init__(self, rows):
        self.segs = []
        grid = {}
        for x in rows:
            s = norm_segment(x)
            if not s:
                continue
            i = len(self.segs)
            self.segs.append(s)
            alat, alon, blat, blon = s[:4]
            n = int(hav_km(alat, alon, blat, blon) * 1000 // 150) + 1
            cells = {(int((alat + (blat - alat) * k / n) / GRID), int((alon + (blon - alon) * k / n) / GRID)) for k in range(n + 1)}
            for c in cells:
                grid.setdefault(c, []).append(i)
        self.grid = grid

    def match(self, lat, lon, brg):
        cx, cy = int(lat / GRID), int(lon / GRID)
        kx, ky = math.cos(math.radians(lat)) * 111320.0, 110574.0
        best, best_score = None, 1e9
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((cx + dx, cy + dy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    s = self.segs[i]
                    ax, ay = (s[1] - lon) * kx, (s[0] - lat) * ky
                    vx, vy = (s[3] - s[1]) * kx, (s[2] - s[0]) * ky
                    l2 = vx * vx + vy * vy
                    if l2 < 1.0:
                        continue
                    t = max(0.0, min(1.0, -(ax * vx + ay * vy) / l2))
                    d = math.hypot(ax + t * vx, ay + t * vy)
                    if d > MATCH_MAX_M:
                        continue
                    diff = abs((math.degrees(math.atan2(vx, vy)) % 360 - brg + 180) % 360 - 180)
                    undirected = min(diff, 180 - diff)
                    if undirected > MATCH_MAX_ANGLE:
                        continue
                    # prefer the nearest link; if two carriageways tie, prefer the one running our way
                    score = d + undirected * 0.25 + (12.0 if diff > 90 else 0.0)
                    if score < best_score:
                        best, best_score = s, score
        return best


async def fetch_speed_rows():
    global GOOD_SPEED_PATH
    paths = [GOOD_SPEED_PATH] if GOOD_SPEED_PATH else SPEED_PATHS
    errors = []
    for path in paths:
        d = await get_lta(path, {"$skip": 0})
        if d.get("_error"):
            errors.append(d["_error"])
            if d.get("_status") == 404:
                if path == GOOD_SPEED_PATH:
                    GOOD_SPEED_PATH = None
                continue
            break  # auth / network / rate limit problem: other paths will not help
        rows = list(d.get("value", []))
        err = None
        if len(rows) >= 500:
            more, err = await fetch_pages(path, start=500)
            rows += more
        GOOD_SPEED_PATH = path
        return rows, path, err
    return [], None, " | ".join(errors[-3:])


async def _load_bands():
    rows, path, err = await fetch_speed_rows()
    if not rows:
        return {"idx": None, "path": path, "error": err or "LTA returned no speed bands", "raw": 0, "usable": 0}, TTL_NEGATIVE, False
    idx = await asyncio.to_thread(BandIndex, rows)
    state = {"idx": idx, "path": path, "error": err, "raw": len(rows), "usable": len(idx.segs)}
    return state, TTL_BANDS, bool(idx.segs)


async def bands_state():
    return await cached("bands", _load_bands)


# --------------------------------------------------------------------------- route geometry (road snapping)
def line_len_km(line):
    return sum(hav_km(a[0], a[1], b[0], b[1]) for a, b in zip(line, line[1:]))


async def osrm_leg(coords):
    path = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    try:
        r = await client().get(f"{OSRM}/route/v1/driving/{path}", params={"overview": "full", "geometries": "geojson", "steps": "false"}, timeout=25)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "Ok":
            return None
        line = [(c[1], c[0]) for c in j["routes"][0]["geometry"]["coordinates"]]
        # reject absurd detours (usually a stop snapped to the wrong carriageway)
        if line_len_km(line) > 2.5 * line_len_km(coords) + 0.5:
            return None
        return line
    except Exception:
        return None


def geom_key(svc, direction, stops):
    return f"geom:{svc}:{direction}:{len(stops)}:{stops[0]['code']}:{stops[-1]['code']}"


async def route_geometry(svc, direction, stops):
    """V13.11: busrouter.sg line (checked against LTA stops) -> OneMap legs -> OSRM -> straight stop-to-stop lines."""
    async def factory():
        notes = []
        line, info = await routegeom.busrouter_line(client(), svc, stops)
        if line:
            return {"line": line, "source": "busrouter", "detail": info}, TTL_GEOM, True
        notes.append(info)
        line, info = await routegeom.onemap_line(client(), stops)
        if line:
            return {"line": line, "source": "onemap", "detail": info + " | " + notes[0]}, TTL_GEOM, True
        notes.append(info)
        pts = [(s["lat"], s["lon"]) for s in stops]
        line, bad = [], 0
        step = 39
        for start in range(0, max(1, len(pts) - 1), step):
            leg = pts[start:start + step + 1]
            if len(leg) < 2:
                break
            g = await osrm_leg(leg)
            if g is None:
                bad += 1
                g = leg
            line += g if not line else g[1:]
        if len(line) < 2:
            line = pts
            bad += 1
        source = "osrm" if bad == 0 else ("stops" if len(line) == len(pts) else "partial")
        return {"line": line, "source": source, "detail": " | ".join(notes)}, (TTL_GEOM if bad == 0 else 600), True
    return await cached(geom_key(svc, direction, stops), factory)


def cached_line(svc, direction, stops):
    hit = CACHE.get(geom_key(svc, direction, stops))
    return hit[2]["line"] if hit else [(s["lat"], s["lon"]) for s in stops]


# --------------------------------------------------------------------------- colour the route from speed bands
def chunk_line(line, min_m=25.0):
    pts = [line[0]]
    for p in line[1:]:
        if p != pts[-1]:
            pts.append(p)
    chunks, cur, acc = [], [pts[0]], 0.0
    for p in pts[1:]:
        acc += hav_km(cur[-1][0], cur[-1][1], p[0], p[1]) * 1000
        cur.append(p)
        if acc >= min_m:
            chunks.append([cur, acc])
            cur, acc = [p], 0.0
    if len(cur) > 1:
        if chunks and acc < min_m / 2:
            chunks[-1][0] += cur[1:]
            chunks[-1][1] += acc
        else:
            chunks.append([cur, acc])
    return chunks


def color_route(line, idx):
    chunks = chunk_line(line) if len(line) >= 2 else []
    recs = []
    for pts, m in chunks:
        a, b = pts[0], pts[-1]
        rec = idx.match((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, bearing(a[0], a[1], b[0], b[1])) if (idx and m >= 8) else None
        recs.append(rec)
    # bridge short unmatched stretches (junctions where one link ends and the next begins)
    i, n = 0, len(recs)
    while i < n:
        if recs[i] is None:
            j = i
            while j < n and recs[j] is None:
                j += 1
            gap = sum(chunks[k][1] for k in range(i, j))
            fill = recs[i - 1] if i > 0 else (recs[j] if j < n else None)
            if fill is not None and gap <= BRIDGE_M:
                for k in range(i, j):
                    recs[k] = fill
            i = j
        else:
            i += 1
    km = {"smooth": 0.0, "moderate": 0.0, "slow": 0.0, "none": 0.0}
    known_km = known_h = 0.0
    runs, cur_key = [], object()
    for (pts, m), rec in zip(chunks, recs):
        cls = band_class(rec[4]) if rec else "none"
        km[cls] += m / 1000
        if rec:
            known_km += m / 1000
            known_h += (m / 1000) / speed_of(rec[4], rec[6], rec[7])
        key = (rec[4], rec[5]) if rec else None
        rp = [[round(p[0], 5), round(p[1], 5)] for p in pts]
        if key == cur_key and runs:
            runs[-1]["pts"] += rp[1:]
            runs[-1]["km"] += m / 1000
        else:
            runs.append({"b": rec[4] if rec else None, "road": rec[5] if rec else "", "mn": num(rec[6]) if rec else None, "mx": num(rec[7]) if rec else None, "cat": rec[8] if rec else None, "pts": rp, "km": m / 1000})
            cur_key = key
    avg = (known_km / known_h) if known_h > 0 else 30.0
    drive_h = known_h + (km["none"] / avg)
    for r in runs:
        r["km"] = round(r["km"], 3)
    return runs, {"km": km, "driveMin": drive_h * 60, "known": known_km > 0}


# --------------------------------------------------------------------------- bus arrivals
def parse_bus(b, now):
    if not isinstance(b, dict):
        return None
    eta_dt = parse_dt(b.get("EstimatedArrival"))
    if eta_dt is None:
        return None
    if eta_dt.tzinfo is None:
        eta_dt = eta_dt.replace(tzinfo=SGT)
    lat, lon = num(b.get("Latitude")), num(b.get("Longitude"))
    if not in_sg(lat, lon):
        lat = lon = None
    return {
        "eta": max(0, round((eta_dt - now).total_seconds() / 60)),
        "etaf": max(0.0, (eta_dt - now).total_seconds() / 60),
        "lat": lat, "lon": lon,
        "load": b.get("Load") or "",
        "type": b.get("Type") or "",
        "wab": (b.get("Feature") or "") == "WAB",
        "monitored": str(b.get("Monitored", "1")) == "1",
        "dest": str(b.get("DestinationCode") or ""),
    }


# bus type per service, learned from LTA Bus Arrival ("Type": SD / DD / BD); flushed to sqlite by Diversion Maps
BT_SEEN = {}


def bt_seen(svc, typ):
    k = (svc, typ)
    BT_SEEN[k] = BT_SEEN.get(k, 0) + 1


def parse_services(data, now, only=None):
    out = []
    for s in data.get("Services", data.get("value", [])) or []:
        svc = str(s.get("ServiceNo", "")).strip().upper()
        if only and svc != only:
            continue
        buses = [parse_bus(s.get(k), now) for k in ("NextBus", "NextBus2", "NextBus3")]
        for b in buses:
            if b and b.get("type") in ("SD", "DD", "BD"):
                bt_seen(svc, b["type"])      # learn each service's bus types from LTA Bus Arrival (used by Diversion Maps rule 1)
        out.append({"service": svc, "operator": s.get("Operator") or "", "buses": [b for b in buses if b]})
    return out


async def arrivals_raw(stop, service=""):
    """Bus Arrival for a stop. Always fetched for ALL services at the stop and cached per stop, so many selected services that share
    stops (interchanges, trunk roads) cost one LTA call between them. Callers filter by service via parse_services(..., only=svc)."""
    async def factory():
        d = await get_lta(EP_ARRIVAL, {"BusStopCode": stop})
        return d, TTL_ARRIVAL, not d.get("_error")
    return await cached(f"arr:{stop}", factory)


def min_dist_km(lat, lon, line):
    kx = math.cos(math.radians(lat)) * 111.32
    return min(math.hypot((p[1] - lon) * kx, (p[0] - lat) * 110.574) for p in line)


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app):
    async def warm():
        try:
            await asyncio.gather(static(), bands_state())
        except Exception:
            pass
    task = None
    trtask = None
    trvtask = None
    if KEY:
        asyncio.create_task(warm())
        bb_init()
        task = asyncio.create_task(bb_loop())
        asyncio.create_task(rt_data_loop())
        asyncio.create_task(tr_refresh(force=True))
        trtask = asyncio.create_task(tr_loop())
        trvtask = asyncio.create_task(tr_verify_loop())      # V16.3: second-source check of congestion (our buses + Waze)
        if DV_AUTO:
            asyncio.create_task(dv_auto_loop())              # V16.14: potential road blockages for Diversion Maps (suggestions only)
    fctask = asyncio.create_task(fc_loop())                  # V17: Forecast Centre collector (idle unless the page is open or FORECAST_ALWAYS_ON=1)
    yield
    fctask.cancel()
    if task is not None:
        task.cancel()
    if trtask is not None:
        trtask.cancel()
    if trvtask is not None:
        trvtask.cancel()
    if _client is not None:
        await _client.aclose()


app = FastAPI(title=f"SG Transport Pulse {VERSION}", lifespan=lifespan)


@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.get("/")
async def home(request: Request):
    """The site opens on the Command Centre (Forecast Centre). Old Route Traffic links (/?svc=..&dir=..) still land on Route Traffic."""
    from fastapi.responses import RedirectResponse
    q = request.url.query
    return RedirectResponse("/route-traffic?" + q if q else "/command", status_code=307)


@app.get("/route-traffic", response_class=HTMLResponse)
async def route_traffic_page():
    return HTMLResponse((HERE / "index.html").read_text(encoding="utf-8"))


@app.get("/occ-live", response_class=HTMLResponse)
async def page_occ_live():
    return HTMLResponse((HERE / "occ_live.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    return {"online": True, "lta": bool(KEY), "version": VERSION, "time": now_sgt().isoformat(timespec="seconds"), "speedEndpoint": GOOD_SPEED_PATH, "tomtom": bool(TT["key"]),
            "httpx_ok": not HTTPX_STUB, "problem": HTTPX_PROBLEM if HTTPX_STUB else None}


@app.get("/api/traffic-test")
async def traffic_test():
    b = await bands_state()
    counts = {}
    if b["idx"]:
        for s in b["idx"].segs:
            counts[str(s[4])] = counts.get(str(s[4]), 0) + 1
    return {"working": bool(b["idx"]), "endpoint": b["path"], "received": b["raw"], "usable": b["usable"],
            "speedBandCounts": counts, "sample": b["idx"].segs[0] if b["idx"] else None, "error": b["error"], "ageSec": cache_age("bands")}


@app.get("/api/route")
async def api_route(service: str = "", direction: int = 1):
    svc = service.strip().upper()
    st = await static()
    if not st["stops"] or not st["routes"]:
        return {"service": svc, "direction": direction, "error": f"Could not load bus stops/routes from LTA ({st.get('error') or 'no data'})"}
    dirs = st["dirs"].get(svc, [])
    stops = route_stops(st, svc, direction)
    if not stops:
        msg = f"No bus service '{svc}' found" if not dirs else f"Service {svc} has no direction {direction}"
        return {"service": svc, "direction": direction, "availableDirections": dirs, "error": msg}
    geom = await route_geometry(svc, direction, stops)
    bands = await bands_state()
    runs, stats = await asyncio.to_thread(color_route, geom["line"], bands["idx"])
    km = stats["km"]
    total = sum(km.values())
    pct = pct_split([km["smooth"], km["moderate"], km["slow"], km["none"]])
    official = max((s["dist"] for s in stops if s["dist"]), default=None)
    # Only estimate a travel time from real LTA readings; with no traffic feed we show nothing rather than guess.
    eta = round(stats["driveMin"] + DWELL_MIN * len(stops)) if (total > 0 and stats["known"]) else None
    return {
        "service": svc, "direction": direction, "availableDirections": dirs,
        "stops": [{"seq": s["seq"], "code": s["code"], "name": s["name"], "road": s["road"], "lat": s["lat"], "lon": s["lon"]} for s in stops],
        "runs": runs, "geometry": geom["source"], "geometryDetail": geom.get("detail"),
        "summary": {
            "lengthKm": round(official if official else total, 1), "stops": len(stops), "etaMin": eta,
            "etaBasis": "current traffic" if stats["known"] else "traffic feed unavailable",
            "pct": {"smooth": pct[0], "moderate": pct[1], "slow": pct[2], "none": pct[3]},
            "km": {k: round(v, 2) for k, v in km.items()},
        },
        "traffic": {"ok": bool(bands["idx"]), "endpoint": bands["path"], "received": bands["raw"], "usable": bands["usable"], "error": bands["error"], "ageSec": cache_age("bands")},
        "updated": now_sgt().isoformat(timespec="seconds"),
    }


@app.get("/api/buses")
async def api_buses(service: str = "", direction: int = 1):
    svc = service.strip().upper()
    st = await static()
    stops = route_stops(st, svc, direction) if st["stops"] else []
    if not stops:
        return {"service": svc, "direction": direction, "buses": [], "error": "Route not available"}
    line = cached_line(svc, direction, stops)
    tol = 0.30 if len(line) > 2 * len(stops) else 0.55
    n = len(stops)
    step = max(1, math.ceil(n / MAX_SAMPLE_STOPS))
    sample = list(range(0, n, step))
    if sample[-1] != n - 1:
        sample.append(n - 1)
    results = await asyncio.gather(*[arrivals_raw(stops[i]["code"], svc) for i in sample])
    now = now_sgt()
    errors = [r["_error"] for r in results if r.get("_error")]
    # A terminal is often a stop of BOTH directions, so a bus arriving there from the other direction
    # would look like ours. At such shared stops keep only buses whose destination is this direction's last stop.
    last_code = stops[-1]["code"]
    is_loop = stops[0]["code"] == last_code
    other_dir = {r["code"] for d in st["dirs"].get(svc, []) if d != direction for r in st["routes"].get((svc, d), [])}
    origin = []          # next arrivals/departures at the first stop (used for the departure ladder), incl. buses with no GPS
    for sv in parse_services(results[0], now, svc):
        for b in sv["buses"]:
            if stops[0]["code"] in other_dir and not is_loop and b["dest"] and b["dest"] != last_code:
                continue
            origin.append({"eta": b["eta"], "monitored": b["monitored"]})
    cands, other_way = [], 0
    for i, r in zip(sample, results):
        shared = stops[i]["code"] in other_dir
        for sv in parse_services(r, now, svc):
            for b in sv["buses"]:
                if b["lat"] is None or min_dist_km(b["lat"], b["lon"], line) > tol:
                    continue
                if shared and not is_loop and b["dest"] and b["dest"] != last_code:
                    other_way += 1
                    continue
                cands.append({**b, "src": i})
    cands.sort(key=lambda c: c["eta"])
    clusters = []
    for c in cands:  # one physical bus is reported from several sampled stops: merge them
        for cl in clusters:
            if c["src"] not in cl["srcs"] and hav_km(c["lat"], c["lon"], cl["lat"], cl["lon"]) < 0.25:
                cl["srcs"].add(c["src"])
                cl["etas"][c["src"]] = c["eta"]
                cl["etasf"][c["src"]] = c["etaf"]
                break
        else:
            clusters.append({**c, "srcs": {c["src"]}, "etas": {c["src"]: c["eta"]}, "etasf": {c["src"]: c["etaf"]}})
    buses = []
    for cl in clusters:
        near_i = min(range(n), key=lambda k: hav_km(cl["lat"], cl["lon"], stops[k]["lat"], stops[k]["lon"]))
        near, to = stops[near_i], stops[cl["src"]]
        buses.append({"lat": cl["lat"], "lon": cl["lon"], "load": cl["load"], "type": cl["type"], "wab": cl["wab"], "monitored": cl["monitored"],
                      "eta": cl["eta"], "toStop": {"code": to["code"], "name": to["name"]},
                      "near": {"code": near["code"], "name": near["name"], "road": near["road"]}, "progress": near_i, "etas": cl["etas"], "etasf": cl["etasf"]})
    buses.sort(key=lambda b: b["progress"])
    for k, b in enumerate(buses, 1):
        b["id"] = k
    out = {"service": svc, "direction": direction, "buses": buses, "sampled": len(sample), "otherDirectionSkipped": other_way, "origin": origin, "updated": now.isoformat(timespec="seconds")}
    if errors and len(errors) == len(results):
        out["error"] = errors[0]
    return out


@app.get("/api/arrivals")
async def api_arrivals(stop: str = "", service: str = ""):
    stop, svc = stop.strip(), service.strip().upper()
    st = await static()
    info = st["stops"].get(stop) if st["stops"] else None
    raw = await arrivals_raw(stop, svc)
    services = parse_services(raw, now_sgt(), svc or None)
    services.sort(key=lambda s: (not s["service"].isdigit(), int(s["service"]) if s["service"].isdigit() else 0, s["service"]))
    return {"stop": info, "service": svc, "services": services, "error": raw.get("_error"),
            "servicesAtStop": sorted(st["at_stop"].get(stop, [])) if st["stops"] else [], "updated": now_sgt().isoformat(timespec="seconds")}


@app.get("/api/incidents")
async def api_incidents(service: str = "", direction: int = 1):
    async def factory():
        d = await get_lta(EP_INCIDENTS)
        return d, TTL_INCIDENTS, not d.get("_error")
    raw = await cached("incidents", factory)
    line = None
    svc = service.strip().upper()
    if svc:
        st = await static()
        stops = route_stops(st, svc, direction) if st["stops"] else []
        if stops:
            line = cached_line(svc, direction, stops)
    out = []
    for x in raw.get("value", []):
        lat, lon = num(x.get("Latitude")), num(x.get("Longitude"))
        if not in_sg(lat, lon):
            continue
        if line and min_dist_km(lat, lon, line) > 1.0:
            continue
        out.append({"type": x.get("Type") or "Incident", "message": x.get("Message") or "", "lat": lat, "lon": lon})
    return {"incidents": out[:100], "scoped": bool(line), "error": raw.get("_error")}


def parse_rain(j):
    stations, vals = {}, {}
    data = j.get("data") if isinstance(j.get("data"), dict) else None
    if data:  # data.gov.sg v2
        for s in data.get("stations", []):
            stations[s.get("id") or s.get("deviceId")] = s
        rd = data.get("readings") or []
        for r in (rd[-1].get("data", []) if rd else []):
            vals[r.get("stationId")] = num(r.get("value"))
    else:  # data.gov.sg v1
        for s in (j.get("metadata") or {}).get("stations", []):
            stations[s.get("id") or s.get("device_id")] = s
        items = j.get("items") or []
        for r in (items[-1].get("readings", []) if items else []):
            vals[r.get("station_id")] = num(r.get("value"))
    out = []
    for sid, mm in vals.items():
        s = stations.get(sid) or {}
        loc = s.get("location") or {}
        lat, lon = num(loc.get("latitude")), num(loc.get("longitude"))
        if mm is not None and in_sg(lat, lon):
            out.append({"id": sid, "name": s.get("name") or sid, "lat": lat, "lon": lon, "mm": mm})
    return out


@app.get("/api/rain")
async def api_rain():
    async def factory():
        urls = ["https://api-open.data.gov.sg/v2/real-time/api/rainfall", "https://api.data.gov.sg/v1/environment/rainfall"]
        err = None
        for u in urls:
            try:
                r = await client().get(u, headers={"x-api-key": DATAGOV_KEY} if DATAGOV_KEY else None)
                r.raise_for_status()
                st = parse_rain(r.json())
                if st:
                    return {"stations": st, "error": None}, TTL_RAIN, True
                err = "no stations in response"
            except Exception as e:
                err = f"{type(e).__name__}"
        return {"stations": [], "error": f"NEA rainfall unavailable ({err})"}, 0, False
    d = await cached("rain", factory)
    return {"stations": [s for s in d["stations"] if s["mm"] > 0], "total": len(d["stations"]), "error": d["error"]}


def band_level(b):
    """Whole-island speed layer classes, matching the existing route legend (index.html band4): smooth (LTA band >=5),
    moderate (4), slow (3), congested (<=2)."""
    if b is None:
        return "none"
    b = int(b)
    return "smooth" if b >= 5 else "moderate" if b == 4 else "slow" if b == 3 else "congested"


@app.get("/api/speedbands")
async def api_speedbands(bbox: str = ""):
    """Whole-island LTA Traffic Speed Bands, independent of any bus service (Route Traffic Mode A).
    Optional bbox=south,west,north,east limits the segments returned to what the map is currently showing."""
    bs = await bands_state()
    idx = bs.get("idx")
    if idx is None:
        return {"segments": [], "error": bs.get("error") or "Traffic speed bands unavailable", "total": 0}
    box = None
    if bbox:
        try:
            s, w, n, e = [float(v) for v in bbox.split(",")]
            box = (s, w, n, e)
        except ValueError:
            box = None
    out = []
    for s in idx.segs:
        alat, alon, blat, blon, band, road, mn, mx, cat = s
        if box and not (box[0] - 0.02 <= alat <= box[2] + 0.02 and box[1] - 0.02 <= alon <= box[3] + 0.02) and not (box[0] - 0.02 <= blat <= box[2] + 0.02 and box[1] - 0.02 <= blon <= box[3] + 0.02):
            continue
        out.append([round(alat, 5), round(alon, 5), round(blat, 5), round(blon, 5), band, band_level(band)])
    return {"segments": out, "total": len(idx.segs), "shown": len(out), "path": bs.get("path"), "error": bs.get("error")}


@app.get("/api/roadworks")
async def api_roadworks():
    """Whole-island LTA Road Works (Approved Road Works), independent of any bus service (Route Traffic Mode A)."""
    items, err = await tr_roadworks(time.time())
    out = [{"key": x["key"], "road": x["road"], "lat": x["lat"], "lon": x["lon"],
             "start": tr_hhmm(x["start_epoch"]) and datetime.fromtimestamp(x["start_epoch"], SGT).strftime("%d %b %H:%M"),
             "end": (datetime.fromtimestamp(x["end_epoch"], SGT).strftime("%d %b %H:%M") if x.get("end_epoch") else None),
             "other": x.get("other")} for x in items if x["lat"] is not None]
    return {"roadworks": out, "total": len(items), "with_location": len(out), "error": err}


# --------------------------------------------------------------------------- pre-emptive departure adjustment
EP_SERVICES = "BusServices"      # carries the scheduled dispatch frequency bands (AM/PM peak / off-peak)
MAX_COMBOS = 16                  # service+direction pairs evaluated per request (protects the LTA quota)
PREP = {}                        # route constants per service/direction (never change)


async def _load_freq():
    rows, err = await fetch_pages(EP_SERVICES)
    fq = {}
    for r in rows:
        svc = str(r.get("ServiceNo", "")).strip().upper()
        try:
            d = int(r.get("Direction") or 1)
        except (TypeError, ValueError):
            d = 1
        if svc:
            fq[(svc, d)] = {**{k: r.get(k + "_Freq") for k in ("AM_Peak", "AM_Offpeak", "PM_Peak", "PM_Offpeak")}, "Operator": str(r.get("Operator") or "").strip().upper()}
    return {"freqs": fq, "error": err}, TTL_STATIC, bool(fq) and not err


async def freq_table():
    return await cached("freqs", _load_freq)


def parse_triples(s, cast):
    """'32:1:13,145:2:12' -> {('32', 1): 13, ('145', 2): 12}"""
    out = {}
    for tok in (s or "").split(","):
        p = tok.strip().split(":")
        if len(p) == 3:
            try:
                out[(p[0].strip().upper(), int(p[1]))] = cast(p[2])
            except ValueError:
                pass
    return out


HELD = {}        # "svc:dir" -> {condition: {"since": ts, "seen": ts}}   how long each condition has been continuously true
HELD_GRACE = 420  # a condition that blips off (or is not re-evaluated) for < 7 min keeps its clock: covers ETA jitter and All-services laps


def held_minutes(key, conds, ts):
    """Minutes each check condition has been continuously true. In memory only: it restarts if the server restarts or sleeps."""
    st = HELD.setdefault(key, {})
    out = {}
    for name, on in (conds or {}).items():
        e = st.get(name)
        if on:
            if e is None or ts - e["seen"] > HELD_GRACE:
                e = st[name] = {"since": ts, "seen": ts}
            e["seen"] = ts
            out[name] = round((ts - e["since"]) / 60, 2)
        else:
            if e is not None and ts - e["seen"] > HELD_GRACE:
                del st[name]
            out[name] = 0.0
    return out


# --------------------------------------------------------------------------- planned timetable (optional, uploaded by the operator)
TT_FILE = HERE / "timetable.json"
PTT = {"trips": {}, "updated": None, "rows": 0}     # {(svc, dir): [trip, ...]}  (V17: renamed from TT - the TomTom state below re-used the name and wiped it)
TT_TOKEN = os.getenv("TIMETABLE_TOKEN", "")         # if set, uploads/clears need header x-admin-token
TT_MAX_BYTES = 8 * 1024 * 1024
ALL_CAP = int(os.getenv("CONTROL_ALL_CAP", "160"))   # service+direction pairs scanned in All-services mode


def tt_save():
    try:
        TT_FILE.write_text(json.dumps({"updated": PTT["updated"], "rows": PTT["rows"], "trips": {f"{k[0]}|{k[1]}": v for k, v in PTT["trips"].items()}}), encoding="utf-8")
    except OSError:
        pass          # read-only or ephemeral disk: the timetable then lives in memory only


def tt_load():
    try:
        j = json.loads(TT_FILE.read_text(encoding="utf-8"))
        PTT["trips"] = {(k.split("|")[0], int(k.split("|")[1])): v for k, v in j.get("trips", {}).items()}
        PTT["updated"], PTT["rows"] = j.get("updated"), j.get("rows", 0)
    except (OSError, ValueError, IndexError):
        pass


def tt_summary():
    pairs = {f"{k[0]}:{k[1]}": len(v) for k, v in PTT["trips"].items()}
    return {"loaded": bool(pairs), "pairs": pairs, "services": len({k[0] for k in PTT["trips"]}), "trips": sum(pairs.values()), "updated": PTT["updated"], "tokenRequired": bool(TT_TOKEN)}


tt_load()


@app.get("/api/timetable")
async def api_timetable():
    return tt_summary()


@app.post("/api/timetable")
async def api_timetable_upload(request: Request):
    """Body = CSV/TSV text (see headway.parse_timetable). Replaces the whole planned timetable."""
    if TT_TOKEN and request.headers.get("x-admin-token", "") != TT_TOKEN:
        return JSONResponse({"error": "Upload token missing or wrong."}, status_code=401)
    raw = await request.body()
    if len(raw) > TT_MAX_BYTES:
        return JSONResponse({"error": "File too large (max 8 MB)."}, status_code=413)
    trips, info = headway.parse_timetable(raw.decode("utf-8-sig", "replace"))
    if not trips:
        return JSONResponse({"error": "; ".join(info["errors"][:3]) or "No usable rows found.", **info}, status_code=400)
    PTT.update(trips=trips, updated=now_sgt().isoformat(timespec="seconds"), rows=info["rows"])
    tt_save()
    return {"ok": True, "rows": info["rows"], "errors": info["errors"], **tt_summary()}


@app.post("/api/timetable/clear")
async def api_timetable_clear(request: Request):
    if TT_TOKEN and request.headers.get("x-admin-token", "") != TT_TOKEN:
        return JSONResponse({"error": "Upload token missing or wrong."}, status_code=401)
    PTT.update(trips={}, updated=None, rows=0)
    tt_save()
    return {"ok": True, **tt_summary()}


@app.get("/api/control/services")
async def api_control_services(scope: str = ""):
    """Services (with directions and operator) for All-services mode. scope = operator code (SBST, SMRT, TTS, GAS) or empty for all."""
    st, fq = await static(), await freq_table()
    scope = scope.strip().upper()
    ops, out = {}, []
    for svc, dirs in st["dirs"].items():
        op = next((o for d in dirs for o in [(fq["freqs"].get((svc, d)) or {}).get("Operator")] if o), "")
        ops[op or "?"] = ops.get(op or "?", 0) + 1
        if scope in ("", "ALL") or op == scope:
            out.append({"service": svc, "dirs": dirs, "operator": op})
    out.sort(key=lambda x: ((not x["service"].isdigit()), int(x["service"]) if x["service"].isdigit() else 0, x["service"]))
    total, kept, n = sum(len(x["dirs"]) for x in out), [], 0
    for x in out:
        if n + len(x["dirs"]) > ALL_CAP:
            break
        kept.append(x); n += len(x["dirs"])
    return {"scope": scope or "ALL", "operators": ops, "services": kept, "pairs": n, "totalPairs": total, "cap": ALL_CAP, "truncated": n < total}


@app.get("/control", response_class=HTMLResponse)
async def control_page():
    return HTMLResponse((HERE / "control.html").read_text(encoding="utf-8"))


@app.get("/api/control")
async def api_control(services: str = "", direction: int = 0, adj: str = "", plan: str = "", lay: str = ""):
    """Evaluate the five pre-emptive departure checks for each selected service + direction.
    adj  = HW the controller has already applied, e.g. '32:1:13'   plan = timetable running time (min), e.g. '32:1:45'"""
    svcs = []
    for t in re.split(r"[,\s]+", services.upper()):
        if t and re.fullmatch(r"[0-9A-Z]{1,6}", t) and t not in svcs:
            svcs.append(t)
    dirs = [direction] if direction in (1, 2) else [1, 2]
    combos = [(s, d) for s in svcs for d in dirs][:MAX_COMBOS]
    adj_m, plan_m, lay_m = parse_triples(adj, int), parse_triples(plan, float), parse_triples(lay, float)
    now = now_sgt()
    st = await static()
    fq = await freq_table()

    async def one(svc, d):
        route = await api_route(svc, d)
        if route.get("error"):
            return {"service": svc, "direction": d, "error": route["error"], "skip": len(dirs) == 2 and bool(route.get("availableDirections"))}
        stops = route_stops(st, svc, d)
        line = cached_line(svc, d, stops)
        key = geom_key(svc, d, stops)
        prep = PREP.get(key)
        if prep is None:
            prep = PREP[key] = await asyncio.to_thread(headway.prepare, line, stops)
        b = await api_buses(svc, d)
        bl = b.get("buses", [])
        pos = await asyncio.to_thread(lambda: [headway.project(line, prep["cum"], x["lat"], x["lon"])[0] for x in bl])
        ctx = {"service": svc, "direction": d, "now": now, "n_stops": len(stops), "stop_s": prep["stop_s"], "route_km": prep["km"],
               "runs": route.get("runs", []), "traffic_ok": route["traffic"]["ok"],
               "buses": [{"s_km": s, "etas": x.get("etas", {}), "monitored": x["monitored"], "load": x["load"], "near": x["near"]["name"]} for s, x in zip(pos, bl)],
               "origin_etas": [o["eta"] for o in b.get("origin", [])],
               "sched": headway.sched_hw(fq["freqs"].get((svc, d)), now), "adj_hw": adj_m.get((svc, d)), "plan_override": plan_m.get((svc, d)),
               "layover": lay_m.get((svc, d)), "next_dir": (2 if d == 1 else 1) if len(st["dirs"].get(svc, [])) > 1 else d}
        tt = PTT["trips"].get((svc, d))
        if tt:
            nowm = now.hour * 60 + now.minute + now.second / 60
            codes, arrs = [x["code"] for x in stops], []
            for tr in tt:
                t = headway.planned_times(tr, codes, prep["stop_s"], prep["km"])
                ref = next((x for x in t if x is not None), None) if t else None
                if ref is not None and abs(((ref - nowm + 720) % 1440) - 720) <= 240:      # only trips within +/-4 h of now
                    arrs.append({"id": tr.get("id") or "", "t": t})
            ctx["planned"] = {"trips": arrs, "now_min": nowm}
        # pass 1 finds which conditions are true right now; the tracker says how long each has held; pass 2 decides with that
        item0 = await asyncio.to_thread(headway.evaluate, ctx)
        ctx["held"] = held_minutes(f"{svc}:{d}", item0.get("conds"), now.timestamp())
        item = await asyncio.to_thread(headway.evaluate, ctx)
        for x in item["buses"]:
            x.pop("etas", None)
        item.update(stops=len(stops), busError=b.get("error"), key=f"{svc}:{d}")
        return item

    res = await asyncio.gather(*[one(s, d) for s, d in combos], return_exceptions=True)
    items, errors = [], []
    for (s, d), r in zip(combos, res):
        if isinstance(r, Exception):
            errors.append(f"{s} Dir {d}: {type(r).__name__}")
        elif r.get("error"):
            if not r.get("skip") and r["error"] not in errors and f"{s}: {r['error']}" not in errors:
                errors.append(r["error"])
        else:
            items.append(r)
    order = {"critical": 0, "developing": 1, "stable": 2, "nodata": 3}
    items.sort(key=lambda x: (order.get(x["risk"], 9), (not x["service"].isdigit()), int(x["service"]) if x["service"].isdigit() else 0, x["service"], x["direction"]))
    nb = sum(x["n_buses"] for x in items)
    summary = {k: sum(1 for x in items if x["risk"] == k) for k in order}
    summary.update(total=len(items), buses=nb, coverage=round(100 * sum(x["monitored"] for x in items) / nb) if nb else None)
    return {"updated": now.isoformat(timespec="seconds"), "items": items, "summary": summary, "errors": errors, "cfg": headway.CFG,
            "truncated": len(svcs) * len(dirs) > MAX_COMBOS, "timetable": tt_summary(), "freqOk": bool(fq["freqs"]), "freqError": fq.get("error")}


# =============================================================================== Bus Bunching & Headway Gap (V1)
import json
import sqlite3
import contextlib
from collections import deque
import bunching
import bunching_analytics
import bunching_live

BB_DB = os.getenv("BUNCHING_DB", str(HERE / "bunching.db"))
BB_DEFAULT = os.getenv("BUNCHING_SERVICES", "32,145,65,33,51,74,89,200,27,157")
BB_MAX_PAIRS = int(os.getenv("BUNCHING_MAX_PAIRS", "40"))
BB_ADMIN = os.getenv("BUNCHING_ADMIN_TOKEN", "") or TT_TOKEN
BB_IDLE_SEC = 600                     # stop polling LTA when nobody has looked at the page for 10 min
BB_ALWAYS = os.getenv("BUNCHING_ALWAYS_ON", "").strip().lower() in ("1", "true", "yes")   # keep polling with nobody watching, so the event log keeps filling
BB = {"params": dict(bunching.PARAMS), "labels": {}, "master": [], "rows": {}, "watch": {}, "hist": deque(maxlen=900), "trk": {}, "open": {}, "seq": 0,
      "last_req": 0.0, "cycle": {"at": None, "took": None, "pairs": 0}, "loop_at": 0.0, "db_ok": True, "db_err": None}
BB_BOOT = time.time()


def bb_persistent():
    """Does the bunching database survive an upload (redeploy) or restart? BUNCHING_PERSISTENT=1 / 0 overrides the guess.
    On Render only a mounted persistent disk does (free instances cannot have one); on your own PC or server the file simply stays."""
    f = os.getenv("BUNCHING_PERSISTENT", "").strip().lower()
    if f in ("1", "true", "yes"):
        return True
    if f in ("0", "false", "no"):
        return False
    if not (os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID")):
        return True
    try:
        d, top = Path(BB_DB).resolve().parent, HERE.resolve()
        if d == top or d in top.parents:                     # next to the app (default) = the instance's own, temporary disk
            return False
        while d != d.parent and d != top:
            if os.path.ismount(d):                           # e.g. /var/data, a Render persistent disk
                return True
            d = d.parent
    except Exception:
        pass
    return False


BB_PERSIST = bb_persistent()


def bb_rec_status(now=None):
    """What the analytics RECORDING chip shows. state: on | starting (server just woke / page just opened) | paused (nobody watching,
    BUNCHING_ALWAYS_ON off) | stalled (should record but no collector cycle for 5 min) | off (no LTA key)."""
    now = now or time.time()
    every = float(BB["params"]["refresh_sec"])
    attended = BB_ALWAYS or now - BB["last_req"] < BB_IDLE_SEC
    fresh = bool(BB["loop_at"]) and now - BB["loop_at"] < max(3 * every, 2 * float(BB["cycle"].get("took") or 0) + every)
    if not KEY:
        state = "off"
    elif not attended:
        state = "paused"
    elif fresh:
        state = "on"
    elif now - max(BB_BOOT, BB.get("wake_at", 0.0)) < 300:
        state = "starting"
    else:
        state = "stalled"
    return {"state": state, "always_on": bool(BB_ALWAYS), "persistent": BB_PERSIST, "keep_days": BB_KEEP_DAYS, "since": round(BB_BOOT), "db_ok": BB["db_ok"]}


BB_INT_PARAMS = ("confirm_stops", "gap_stops", "gap_skip_stops", "alert_step", "refresh_sec", "horizon_min")
PARAM_RANGES = {"bunch_min": (0.5, 10), "confirm_stops": (1, 60), "gap_add_min": (1, 60), "gap_stops": (1, 60), "gap_skip_stops": (0, 20), "alert_step": (1, 30), "horizon_min": (10, 60), "refresh_sec": (15, 600),
                "w_gap": (0, 100), "w_bunch": (0, 100), "w_persist": (0, 100), "w_deter": (0, 100), "w_time": (0, 100), "yellow_conv": (0.3, 1), "yellow_div": (1, 3)}


def bb_sql(sql, args=(), fetch=False):
    """Tiny sqlite helper. A missing / read-only disk must never break the dashboard, so failures only set a flag."""
    try:
        with contextlib.closing(sqlite3.connect(BB_DB, timeout=10)) as c:
            c.row_factory = sqlite3.Row
            cur = c.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else None
            c.commit()
            BB["db_ok"], BB["db_err"] = True, None
            return rows
    except Exception as e:
        BB["db_ok"], BB["db_err"] = False, f"{type(e).__name__}: {e}"
        return [] if fetch else None


def bb_insert(sql, args=()):
    """V16.14: INSERT and return the new row id. (\"SELECT last_insert_rowid()\" through bb_sql runs on a NEW connection and always returns 0.)"""
    try:
        with contextlib.closing(sqlite3.connect(BB_DB, timeout=10)) as c:
            cur = c.execute(sql, args)
            c.commit()
            BB["db_ok"], BB["db_err"] = True, None
            return cur.lastrowid
    except Exception as e:
        BB["db_ok"], BB["db_err"] = False, f"{type(e).__name__}: {e}"
        return None


def bb_sql_many(sql, rows):
    """Many rows in ONE transaction (the analytics recorder writes a handful of rows every collector cycle)."""
    if not rows:
        return
    try:
        with contextlib.closing(sqlite3.connect(BB_DB, timeout=10)) as c:
            c.executemany(sql, rows)
            c.commit()
            BB["db_ok"], BB["db_err"] = True, None
    except Exception as e:
        BB["db_ok"], BB["db_err"] = False, f"{type(e).__name__}: {e}"


def bb_init():
    bb_sql("CREATE TABLE IF NOT EXISTS system_parameter(k TEXT PRIMARY KEY, v REAL)")
    # analytics: one row per bunched / long-gap TRIP (bb_trip, counted once it lasts 15 stops), and how long each service was watched (bb_cov)
    bb_sql("CREATE TABLE IF NOT EXISTS bb_trip(trip_key TEXT PRIMARY KEY, service TEXT, direction INTEGER, kind TEXT, start_ts REAL, last_ts REAL, level INTEGER, "
           "stops INTEGER, start_stop TEXT, buses TEXT)")
    bb_sql("CREATE INDEX IF NOT EXISTS bb_trip_start ON bb_trip(start_ts)")
    bb_sql("CREATE TABLE IF NOT EXISTS bb_cov(hour_ts INTEGER, service TEXT, direction INTEGER, n INTEGER, sec REAL, PRIMARY KEY(hour_ts, service, direction))")
    bb_sql("CREATE TABLE IF NOT EXISTS service_headway_config(service TEXT, direction INTEGER, day_type TEXT, t_from TEXT, t_to TEXT, hw REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS bunching_event(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, direction INTEGER, start_ts REAL, end_ts REAL, bus_group TEXT, "
           "max_level INTEGER, start_stop TEXT, end_stop TEXT, stops INTEGER, min_hw REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS bb_ack(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, kind TEXT, service TEXT, direction INTEGER, action TEXT, who TEXT, ts REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS gap_event(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, direction INTEGER, start_ts REAL, end_ts REAL, buses TEXT, "
           "max_hw REAL, max_ratio REAL, sched_hw REAL, location TEXT)")
    for tbl, cols in (("gap_event", (("start_stop", "TEXT"), ("end_stop", "TEXT"), ("stops", "INTEGER"), ("alerts", "INTEGER"))),   # older files: add the new columns
                      ("bunching_event", (("alerts", "INTEGER"),))):
        have = {r["name"] for r in bb_sql(f"PRAGMA table_info({tbl})", fetch=True)}
        for col, typ in cols:
            if col not in have:
                bb_sql(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
    if not bb_sql("SELECT v FROM system_parameter WHERE k='rules_v'", fetch=True):              # rules changed (3 min / +10 min / 15 stops): drop old saved values once
        bb_sql("DELETE FROM system_parameter")
        bb_sql("INSERT OR REPLACE INTO system_parameter(k, v) VALUES ('rules_v', 2)")
    for r in bb_sql("SELECT k, v FROM system_parameter", fetch=True):
        if r["k"] in BB["params"]:
            BB["params"][r["k"]] = r["v"] if r["k"] not in BB_INT_PARAMS else int(r["v"])
    BB["master"] = [{"service": r["service"], "direction": r["direction"], "day_type": r["day_type"], "from": r["t_from"], "to": r["t_to"], "hw": r["hw"]}
                    for r in bb_sql("SELECT * FROM service_headway_config ORDER BY service, direction, t_from", fetch=True)]
    ho_init()
    os_init()
    in_init()
    rt_init()
    tr_init()


def bb_validate_params(inp):
    out, errs = {}, []
    for k, v in (inp or {}).items():
        if k not in PARAM_RANGES:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            errs.append(f"{k}: not a number")
            continue
        lo, hi = PARAM_RANGES[k]
        if not (lo <= f <= hi):
            errs.append(f"{k}: must be between {lo} and {hi}")
        else:
            out[k] = int(round(f)) if k in BB_INT_PARAMS else f
    merged = {**BB["params"], **out}
    if sum(merged[w] for w in ("w_gap", "w_bunch", "w_persist", "w_deter", "w_time")) <= 0:
        errs.append("at least one risk weight must be above 0")
    return out, errs


def _hhmm(s):
    m = re.fullmatch(r"\s*(\d{1,2}):?(\d{2})\s*", str(s))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if (0 <= h <= 24 and 0 <= mi < 60) else None


def parse_master(text):
    """CSV/TSV -> (rows, errors). Columns: service, direction, day_type (Weekday|Saturday|Sunday|All), from, to, target_hw."""
    rows, errs = [], []
    lines = [l for l in (text or "").replace("\r", "").split("\n") if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return [], ["The file is empty."]
    delim = "\t" if "\t" in lines[0] else (";" if lines[0].count(";") > lines[0].count(",") else ",")
    if re.search(r"[A-Za-z]{4,}", lines[0]) and not re.fullmatch(r"\s*\d+\s*", lines[0].split(delim)[0]):
        lines = lines[1:]
    for i, l in enumerate(lines, 1):
        c = [x.strip().strip('"') for x in l.split(delim)]
        if len(c) < 6:
            errs.append(f"line {i}: needs 6 columns (service, direction, day_type, from, to, target_hw)")
            continue
        svc, dr, dt = c[0].upper(), c[1].lower().replace("dir", "").strip(), c[2].capitalize() if c[2] else "All"
        f, t = _hhmm(c[3]), _hhmm(c[4])
        try:
            hw = float(c[5])
        except ValueError:
            hw = None
        if not re.fullmatch(r"[0-9A-Z]{1,6}", svc) or dr not in ("1", "2") or dt not in ("Weekday", "Saturday", "Sunday", "All") or f is None or t is None or t <= f or not hw or not (1 <= hw <= 120):
            errs.append(f"line {i}: invalid values ({l[:60]})")
            continue
        rows.append({"service": svc, "direction": int(dr), "day_type": dt, "from": f"{f // 60:02d}:{f % 60:02d}", "to": f"{t // 60:02d}:{t % 60:02d}", "hw": hw})
    return rows, errs[:10]


def bb_day_type(now):
    return "Weekday" if now.weekday() < 5 else ("Saturday" if now.weekday() == 5 else "Sunday")


def bb_resolve_hw(svc, d, now, fq):
    """Scheduled headway: Service Headway Master first, else LTA BusServices frequency band, else unknown."""
    m, dt, best = now.hour * 60 + now.minute, bb_day_type(now), None
    for r in BB["master"]:
        if r["service"] != svc or r["direction"] != d or r["day_type"] not in ("All", dt):
            continue
        f, t = _hhmm(r["from"]), _hhmm(r["to"])
        if f is not None and t is not None and f <= m < t and (best is None or (t - f) < best[0]):
            best = (t - f, r)
    if best:
        r = best[1]
        return float(r["hw"]), f"Service Headway Master ({r['day_type']} {r['from']}-{r['to']})"
    lta = headway.sched_hw(fq["freqs"].get((svc, d)), now)
    if lta:
        return float(lta["hw"]), "LTA BusServices frequency: " + lta["label"]
    return None, None


async def bb_route(svc, d):
    async def factory():
        r = await api_route(svc, d)
        return r, 90, not r.get("error")
    return await cached(f"bbroute:{svc}:{d}", factory)


async def bb_eval(svc, d, now, st, fq):
    key = f"{svc}:{d}"
    route = await bb_route(svc, d)
    if route.get("error"):
        return {"service": svc, "direction": d, "key": key, "error": route["error"], "skip": bool(route.get("availableDirections"))}
    stops = route_stops(st, svc, d)
    line = cached_line(svc, d, stops)
    gk = geom_key(svc, d, stops)
    prep = PREP.get(gk)
    if prep is None:
        prep = PREP[gk] = await asyncio.to_thread(headway.prepare, line, stops)
    b = await api_buses(svc, d)
    bl = b.get("buses", [])
    pos = await asyncio.to_thread(lambda: [headway.project(line, prep["cum"], x["lat"], x["lon"])[0] for x in bl])
    tm = headway.TimeModel(route.get("runs", []), prep["stop_s"], headway.CFG) if route["traffic"]["ok"] else None
    if tm is not None and not tm.ok:
        tm = None
    H, H_src = bb_resolve_hw(svc, d, now, fq)
    buses = [{"s_km": s, "etas": x.get("etas", {}), "etasf": x.get("etasf"), "monitored": x["monitored"], "load": x["load"], "near": x["near"]["name"],
              "lat": x["lat"], "lon": x["lon"]} for s, x in zip(pos, bl)]
    trk = BB["trk"].setdefault(key, bunching.Tracker())
    trk.gap_sec = max(150, 3 * int(BB["params"]["refresh_sec"]))                     # no update for this long = polling was paused: forget everything
    ts = now.timestamp()
    trk.update(ts, buses, prep["km"])
    res = bunching.evaluate({"service": svc, "direction": d, "stop_s": prep["stop_s"], "stop_names": [s["name"] for s in stops], "route_km": prep["km"], "buses": buses,
                             "stop_labels": [f"{s['name']} ({s['code']})" if s.get("code") else s["name"] for s in stops],
                             "tm": tm, "H": H, "H_src": H_src, "prior": trk.prior(), "prior_gap": trk.prior_gap(),
                             "prior_last": trk.prior_last(), "prior_last_gap": trk.prior_last_gap(), "now_ts": ts}, BB["params"])
    # Position/crowding observations remain usable even if headway modelling lacks
    # a timetable or overlapping ETAs. Keep risk availability separate from observations.
    res["observed_buses"] = [{"id": x.get("id", i + 1), "lat": x.get("lat"), "lon": x.get("lon"),
                              "load": x.get("load"), "near": x.get("near"), "monitored": x.get("monitored", True)}
                             for i, x in enumerate(buses)]
    res["route_stops"] = [{"code": s.get("code"), "name": s.get("name"), "road": s.get("road"),
                           "seq": s.get("seq"), "lat": s.get("lat"), "lon": s.get("lon"),
                           "route_m": float(prep["stop_s"][i]) * 1000} for i,s in enumerate(stops)]
    BB["labels"][key] = [f"{s['name']} ({s['code']})" if s.get("code") else s["name"] for s in stops]
    trk.commit(res.pop("bunched_pairs", {}), res.pop("long_pairs", {}), ts)
    try:
        ids = rt_track(key, svc, d, ts, buses, prep["stop_s"], prep["km"])           # running-time observations for /running-time
        if ids:
            asyncio.create_task(rt_enrich_live(ids, line))
    except Exception:
        pass
    res.pop("score_parts", None)
    res.update(key=key, stops=len(stops), route_km=round(prep["km"], 1), updated=now.isoformat(timespec="seconds"), at=ts, busError=b.get("error"), traffic_ok=tm is not None)
    return res


# ---- event log (bunching_event / gap_event)
# An event is tracked from the first refresh a group / pair is seen in the state (bunched < bunch_min, or long headway) and is closed 2 refreshes after
# it clears. It is only LOGGED if it really lasted >= confirm_stops (bunching) / gap_stops (long headway) bus stops: stops = end stop - start stop + 1,
# measured from where the leading bus was when the state was first seen to where it was when the state was last seen. Shorter ones are dropped.
def bb_ev_stops(e):
    return max(0, e["last_j"] - e["first_j"] + 1)


def bb_ev_gate(e):
    return int(BB["params"]["confirm_stops" if e["kind"] == "bb" else "gap_stops"])


# ---- alerts: 1st alert when the case has lasted the required stops (15), then one more every `alert_step` stops (20, 25, 30 ...) while it lasts
def bb_alert_level(e):
    n, gate, step = bb_ev_stops(e), bb_ev_gate(e), max(1, int(BB["params"]["alert_step"]))
    return 0 if n < gate else 1 + (n - gate) // step


def bb_alert_update(e, ts):
    lvl, gate, step = bb_alert_level(e), bb_ev_gate(e), max(1, int(BB["params"]["alert_step"]))
    if len(e["alerts"]) < lvl:                                                       # at most ONE new alert per refresh: alerts can never arrive in a burst
        k = len(e["alerts"]) + 1
        at = gate + step * (k - 1)
        labels, idx = BB["labels"].get(e["key"]) or [], min(e["first_j"] + at - 1, e["last_j"])          # the stop where the count reached `at`, not just where the bus is now
        e["alerts"].append({"n": k, "ts": ts, "at_stops": at, "stops": bb_ev_stops(e), "stop": labels[idx] if 0 <= idx < len(labels) else e.get("end_stop")})


def bb_alert_public(e):
    n, gate, step = len(e["alerts"]), bb_ev_gate(e), max(1, int(BB["params"]["alert_step"]))
    lvl = e.get("max_level") or 2
    return {"id": e["id"], "kind": e["kind"], "key": e["key"], "service": e["service"], "direction": e["direction"],
            "label": ("BUNCHING " + ("4BB+" if lvl >= 4 else f"{lvl}BB")) if e["kind"] == "bb" else "LONG HEADWAY",
            "level": lvl if e["kind"] == "bb" else None, "count": n, "stops": bb_ev_stops(e), "gate": gate, "step": step, "next_at": gate + step * n,
            "start": e["start"], "last": e.get("last"), "start_stop": e.get("start_stop"), "end_stop": e.get("end_stop"), "buses": sorted(e["ids"]),
            "min_hw": e.get("min_hw"), "max_hw": e.get("max_hw"), "sched": e.get("sched"), "alerts": e["alerts"],
            "acked": e.get("acked_n", 0) >= n, "acked_ts": e.get("acked_ts"), "clearing": e.get("miss", 0) > 0}


def bb_alerts(keys=None):
    out = [bb_alert_public(e) for e in BB["open"].values() if e.get("alerts") and (keys is None or e["key"] in keys)]
    out.sort(key=lambda a: (-a["count"], -a["stops"], a["service"], a["direction"]))
    return out


def bb_plausible(e, cur_j, ts):
    """A case cannot move along the route faster than buses do. A bigger jump is a different case (or bad data), not a continuation."""
    return cur_j - e["last_j"] <= bunching.max_stops_moved(ts - e.get("last", ts))


def bb_expire_stale(ts):
    """An open event nobody has updated for several refresh intervals (key no longer polled, server asleep ...) is finished, never silently continued later."""
    limit = max(150, 4 * int(BB["params"]["refresh_sec"]))
    for eid in [i for i, e in BB["open"].items() if ts - e.get("last", ts) > limit]:
        bb_close_event(BB["open"].pop(eid))


def bb_events(key, res, ts):
    bb_expire_stale(ts)
    ev, seen = BB["open"], set()
    svc, d = key.split(":")[0], int(key.split(":")[1])
    for g in res.get("groups", []):
        if g["status"] not in ("confirmed", "developing"):                          # only groups that are bunched right now
            continue
        ids = set(g["ids"]) | set(g["joining"])
        e = next((e for e in ev.values() if e["kind"] == "bb" and e["key"] == key and e["id"] not in seen and e.get("cur_ids", e["ids"]) & ids and bb_plausible(e, g["cur_j"], ts)), None)
        if e is None:
            BB["seq"] += 1
            e = ev[BB["seq"]] = {"id": BB["seq"], "kind": "bb", "key": key, "service": svc, "direction": d, "start": ts, "ids": set(), "max_level": 0,
                                 "first_j": g["cur_j"], "last_j": g["cur_j"], "start_stop": g["cur_stop"], "end_stop": g["cur_stop"], "min_hw": g["min_hw"],
                                 "alerts": [], "acked_n": 0, "acked_ts": None}
        e["ids"] |= ids
        e["cur_ids"] = ids                                                            # who is in the group NOW (matching uses this, not everyone ever seen)
        e["sched"] = res.get("sched_hw")
        lv_now = g["size"] + len(g["joining"])
        if e["max_level"] and lv_now > e["max_level"]:
            e["escalated_at"] = ts                                                     # e.g. 2BB grew into 3BB: same case, higher level
        e["max_level"] = max(e["max_level"], lv_now)
        e["min_hw"] = min(e["min_hw"], g["min_hw"])
        if g["cur_j"] >= e["last_j"]:
            e["last_j"], e["end_stop"] = g["cur_j"], g["cur_stop"]
        e["last"], e["miss"] = ts, 0
        bb_alert_update(e, ts)
        bb_trip_record(e, ts)
        seen.add(e["id"])
    for gp in res.get("gaps", []):
        if not gp.get("active"):                                                     # only pairs whose headway is long right now
            continue
        pair = {gp["lead"], gp["foll"]}
        e = next((e for e in ev.values() if e["kind"] == "gap" and e["key"] == key and e["id"] not in seen and (e["lead"] == gp["lead"] or e["foll"] == gp["foll"])
                  and bb_plausible(e, gp["cur_j"], ts)), None)
        if e is None:
            BB["seq"] += 1
            e = ev[BB["seq"]] = {"id": BB["seq"], "kind": "gap", "key": key, "service": svc, "direction": d, "start": ts, "ids": set(), "max_hw": 0.0, "max_ratio": 0.0,
                                 "sched": res["sched_hw"], "location": gp["location"], "lead": gp["lead"], "foll": gp["foll"],
                                 "first_j": gp["cur_j"], "last_j": gp["cur_j"], "start_stop": gp["cur_stop"], "end_stop": gp["cur_stop"],
                                 "alerts": [], "acked_n": 0, "acked_ts": None}
        e["ids"] |= pair
        e["lead"], e["foll"] = gp["lead"], gp["foll"]
        if gp["max_hw"] >= e["max_hw"]:
            e["max_hw"], e["location"] = gp["max_hw"], gp["location"]
        e["max_ratio"] = max(e["max_ratio"], gp["ratio"])
        if gp["cur_j"] >= e["last_j"]:
            e["last_j"], e["end_stop"] = gp["cur_j"], gp["cur_stop"]
        e["sched"] = res.get("sched_hw", e.get("sched"))
        e["last"], e["miss"] = ts, 0
        bb_alert_update(e, ts)
        bb_trip_record(e, ts)
        seen.add(e["id"])
    for eid in [i for i, e in ev.items() if e["key"] == key and i not in seen]:
        e = ev[eid]
        e["miss"] = e.get("miss", 0) + 1
        if e["miss"] >= 2:
            bb_close_event(ev.pop(eid))


BB_TRIPS = bunching_analytics.TripCounter(format(int(time.time()), "x"))


def bb_trip_record(e, ts):
    """Trip-level analytics: the case counts once (per trip) as soon as it has lasted the required stops (15) - written straight away,
    not when the case ends, and kept up to date (longest stops, highest level) while it lasts."""
    n = bb_ev_stops(e)
    if n < bb_ev_gate(e):
        return
    if "trip_key" not in e:
        ids = set(e.get("cur_ids") or e["ids"]) if e["kind"] == "bb" else {e.get("lead"), e.get("foll")}
        e["trip_key"], _ = BB_TRIPS.key_for(e["key"], e["kind"], ids, ts)
    bb_sql("INSERT INTO bb_trip(trip_key, service, direction, kind, start_ts, last_ts, level, stops, start_stop, buses) VALUES (?,?,?,?,?,?,?,?,?,?) "
           "ON CONFLICT(trip_key) DO UPDATE SET last_ts = excluded.last_ts, level = MAX(level, excluded.level), stops = MAX(stops, excluded.stops), buses = excluded.buses",
           (e["trip_key"], e["service"], e["direction"], e["kind"], e["start"], ts, int(e.get("max_level") or 0) if e["kind"] == "bb" else 0, n,
            (e.get("start_stop") or "")[:120], ",".join(sorted(e["ids"]))))


def bb_close_event(e):
    """Write the event to the log - but only if it lasted at least the required number of bus stops."""
    n = bb_ev_stops(e)
    if n < bb_ev_gate(e):
        return False
    if e["kind"] == "bb":
        bb_sql("INSERT INTO bunching_event(service,direction,start_ts,end_ts,bus_group,max_level,start_stop,end_stop,stops,min_hw,alerts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (e["service"], e["direction"], e["start"], e.get("last"), ",".join(sorted(e["ids"])), e["max_level"], e["start_stop"], e.get("end_stop"), n, e["min_hw"], len(e["alerts"])))
    else:
        bb_sql("INSERT INTO gap_event(service,direction,start_ts,end_ts,buses,max_hw,max_ratio,sched_hw,location,start_stop,end_stop,stops,alerts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (e["service"], e["direction"], e["start"], e.get("last"), ",".join(sorted(e["ids"])), e["max_hw"], e["max_ratio"], e["sched"], e["location"],
                e["start_stop"], e.get("end_stop"), n, len(e["alerts"])))
    return True


def bb_close_all():
    """Nobody is watching any more (collector idle): finish every open event at the time it was last seen so nothing is lost."""
    for eid in list(BB["open"]):
        bb_close_event(BB["open"].pop(eid))


def bb_event_public(e, open_=True):
    if open_:
        return {"kind": e["kind"], "service": e["service"], "direction": e["direction"], "start": e["start"], "end": None, "buses": sorted(e["ids"]),
                "level": e.get("max_level"), "stops": bb_ev_stops(e), "min_hw": e.get("min_hw"), "max_hw": e.get("max_hw"), "location": e.get("location") or e.get("start_stop"),
                "start_stop": e.get("start_stop"), "end_stop": e.get("end_stop"), "alerts": len(e.get("alerts", [])), "open": True}
    return e


def bb_open_public(key=None):
    """Open events that have already lasted long enough to be logged (shorter ones are not shown or logged)."""
    return [bb_event_public(e) for e in BB["open"].values() if (key is None or e["key"] == key) and bb_ev_stops(e) >= bb_ev_gate(e)]


# ---- collector
async def bb_run(keys):
    now = now_sgt()
    st, fq = await static(), await freq_table()
    sem = asyncio.Semaphore(3)

    async def one(k):
        svc, d = k
        async with sem:
            try:
                r = await bb_eval(svc, d, now, st, fq)
            except Exception as e:
                r = {"service": svc, "direction": d, "key": f"{svc}:{d}", "error": f"{type(e).__name__}: {e}", "skip": False}
            r.setdefault("at", now.timestamp())
            BB["rows"][r["key"]] = r
            if not r.get("error"):
                bb_events(r["key"], r, now.timestamp())
    await asyncio.gather(*[one(k) for k in keys])


def bb_keys():
    now = time.time()
    keys = [(s, d) for s in [x for x in re.split(r"[,\s]+", BB_DEFAULT.upper()) if re.fullmatch(r"[0-9A-Z]{1,6}", x)] for d in (1, 2)]
    keys += [k for k, t in BB["watch"].items() if now - t < 2700 and k not in keys]
    keys += [(s_, d_) for s_ in rt_watch_list() for d_ in (1, 2) if (s_, d_) not in keys]      # measured round the clock
    return keys[:BB_MAX_PAIRS + 2 * RT_MAX_SERVICES]


async def bb_cycle(keys=None):
    t0 = time.time()
    keys = keys or bb_keys()
    await bb_run(keys)
    per = {}
    for k in keys:
        r = BB["rows"].get(f"{k[0]}:{k[1]}")
        if r and not r.get("error"):
            c = r["counts"]
            per[r["key"]] = (c["gaps"], c["bb"], c["bb2"], c["bb3"], c["bb4"], c["early"])
    per2 = {r["key"]: bunching_live.kpi_tuple(r) for r in (BB["rows"].get(f"{k[0]}:{k[1]}") for k in keys) if r and not r.get("error")}
    BB["hist"].append({"ts": t0, "per": per, "per2": per2})
    BB["cycle"] = {"at": t0, "took": round(time.time() - t0, 1), "pairs": len(keys)}
    BB["loop_at"] = time.time()
    try:
        bb_cov_record(keys, t0)
    except Exception:
        pass


BB_KEEP_DAYS = max(31, int(os.getenv("BUNCHING_KEEP_DAYS", "62") or 62))     # analytics history kept (62 days = 30 days + the 30 days before, for comparison)


def bb_cov_record(keys, t0):
    """Analytics coverage: every collector cycle adds its interval to bb_cov for each service direction watched, so the charts can tell
    "no bunching" from "not recorded". (Bunched / long-gap trips are written by bb_trip_record as they qualify.)"""
    every = float(BB["params"]["refresh_sec"])
    last = BB.get("obs_at")
    dt = t0 - last if last else every
    sec = dt if 5 <= dt <= 3 * max(every, RT_POLL_SEC) else every                    # a pause in polling is not counted as observed time
    BB["obs_at"] = t0
    hour = int(t0 // 3600) * 3600                                                    # Singapore is UTC+8 whole hours: same hour boundaries
    cov = []
    for k in keys:
        r = BB["rows"].get(f"{k[0]}:{k[1]}")
        if not r or r.get("error") or r.get("risk") == "nodata":
            continue
        cov.append((hour, r["service"], r["direction"], sec))
    bb_sql_many("INSERT INTO bb_cov(hour_ts, service, direction, n, sec) VALUES (?,?,?,1,?) "
                "ON CONFLICT(hour_ts, service, direction) DO UPDATE SET n = n + 1, sec = sec + excluded.sec", cov)
    if t0 - BB.get("obs_pruned", 0) > 3600:
        BB["obs_pruned"] = t0
        cut = t0 - BB_KEEP_DAYS * 86400
        bb_sql("DELETE FROM bb_trip WHERE start_ts < ?", (cut,))
        bb_sql("DELETE FROM bb_cov WHERE hour_ts < ?", (cut,))


async def bb_loop():
    while True:
        t0 = time.time()
        try:
            attended = BB_ALWAYS or t0 - BB["last_req"] < BB_IDLE_SEC
            if attended:
                await bb_cycle()
            else:
                if BB["open"]:
                    bb_close_all()
                rk = [(s_, d_) for s_ in rt_watch_list() for d_ in (1, 2)]
                if rk:
                    await bb_cycle(rk)                  # running-time services keep being measured with nobody watching
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        every = BB["params"]["refresh_sec"] if (BB_ALWAYS or time.time() - BB["last_req"] < BB_IDLE_SEC) else max(BB["params"]["refresh_sec"], RT_POLL_SEC)
        await asyncio.sleep(max(5.0, every - (time.time() - t0)))


def bb_auth(request):
    return (not BB_ADMIN) or request.headers.get("x-admin-token", "") == BB_ADMIN


def bb_sum(keys, which):
    tot = [0, 0, 0, 0, 0, 0]
    for k in keys:
        v = which.get(k)
        if v:
            tot = [a + b for a, b in zip(tot, v)]
    return {"gaps": tot[0], "bb": tot[1], "bb2": tot[2], "bb3": tot[3], "bb4": tot[4], "early": tot[5]}


@app.get("/bunching", response_class=HTMLResponse)
async def bunching_page():
    return HTMLResponse((HERE / "bunching.html").read_text(encoding="utf-8"))


@app.get("/bunching/classic", response_class=HTMLResponse)
async def bunching_classic_page():
    """The previous Bunching & Gap page (settings, headway master, event log, alert list, patterns) - linked from the new page."""
    return HTMLResponse((HERE / "bunching_classic.html").read_text(encoding="utf-8"))


# ---- V14.1 shared basemap: CARTO raster tiles (keyed) with OneMap / OpenStreetMap fallback. The key is read from the environment;
# it is visible in the browser anyway (every tile URL carries it), so restrict it to your domain in the CARTO dashboard.
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "cb1_3yrv_1_668b3534c1ae1cf8528fb415").strip()


@app.get("/basemap.js")
async def basemap_js():
    from fastapi.responses import Response
    js = (HERE / "basemap.js").read_text(encoding="utf-8").replace("__CARTO_KEY__", re.sub(r"[^A-Za-z0-9_\-]", "", CARTO_API_KEY))
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "public, max-age=600"})


@app.get("/map3d.js")
async def map3d_js():
    """V16.24: the shared 3D map (Route Traffic, Diversion Maps, Halfway Planner) - MapLibre GL + OpenFreeMap, loaded by the browser only when 3D is opened"""
    from fastapi.responses import Response
    return Response((HERE / "map3d.js").read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "public, max-age=600"})


# ---- V15.9 shared command-platform design system + single-source navigation registry.
# Loaded by pages one at a time as they are redesigned; pages that don't include them are unaffected.
@app.get("/design-system.css")
async def design_system_css():
    from fastapi.responses import Response
    css = (HERE / "design_system.css").read_text(encoding="utf-8")
    return Response(css, media_type="text/css", headers={"Cache-Control": "no-cache"})   # theme changes show on the next load


# ---- HORIZON brand: logo cut-outs, ring mark (favicon) and the portable design tokens (CSS / JSON) for other systems.
_BRAND_TYPES = {".webp": "image/webp", ".svg": "image/svg+xml", ".png": "image/png"}


_BRAND_EMBEDDED = {   # built-in copies of the HORIZON logos: served when the brand files are missing on the server
    "horizon-lockup.webp": "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",
    "horizon-wordmark.webp": "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",
    "horizon-word.webp": "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",
    "sbs-transit.webp": "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",
    "horizon-mark.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCIgcm9sZT0iaW1nIiBhcmlhLWxhYmVsPSJIT1JJWk9OIj4KICA8ZGVmcz4KICAgIDxsaW5lYXJHcmFkaWVudCBpZD0iciIgeDE9IjAiIHkxPSIwIiB4Mj0iMCIgeTI9IjEiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzlmZjBmZiIvPjxzdG9wIG9mZnNldD0iLjU1IiBzdG9wLWNvbG9yPSIjMmZjMmZmIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMGE0ZjllIiBzdG9wLW9wYWNpdHk9Ii4zNSIvPjwvbGluZWFyR3JhZGllbnQ+CiAgICA8cmFkaWFsR3JhZGllbnQgaWQ9ImYiIGN4PSIuNSIgY3k9Ii41IiByPSIuNSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjZmZmIi8+PHN0b3Agb2Zmc2V0PSIuMjUiIHN0b3AtY29sb3I9IiM3ZmUzZmYiLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMxZThmZmYiIHN0b3Atb3BhY2l0eT0iMCIvPjwvcmFkaWFsR3JhZGllbnQ+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9ImwiIHgxPSIwIiB4Mj0iMSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjMWU4ZmZmIiBzdG9wLW9wYWNpdHk9IjAiLz48c3RvcCBvZmZzZXQ9Ii41IiBzdG9wLWNvbG9yPSIjYmZmNGZmIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMWU4ZmZmIiBzdG9wLW9wYWNpdHk9IjAiLz48L2xpbmVhckdyYWRpZW50PgogIDwvZGVmcz4KICA8cmVjdCB3aWR0aD0iNjQiIGhlaWdodD0iNjQiIHJ4PSIxNCIgZmlsbD0iIzAzMGIxNyIvPgogIDxjaXJjbGUgY3g9IjMyIiBjeT0iMzIiIHI9IjE3IiBmaWxsPSJub25lIiBzdHJva2U9InVybCgjcikiIHN0cm9rZS13aWR0aD0iNC41Ii8+CiAgPHJlY3QgeD0iNCIgeT0iMzAuNiIgd2lkdGg9IjU2IiBoZWlnaHQ9IjIuOCIgcng9IjEuNCIgZmlsbD0idXJsKCNsKSIvPgogIDxjaXJjbGUgY3g9IjIwIiBjeT0iMzIiIHI9IjciIGZpbGw9InVybCgjZikiLz4KPC9zdmc+Cg==",
}


def _brand_bytes(name):
    """(bytes, where) for a logo: brand/ folder, else repo top level (uploaded without the folder), else the built-in copy."""
    for path, where in ((HERE / "brand" / name, "brand folder"), (HERE / name, "top-level file")):
        if path.is_file():
            return path.read_bytes(), where
    if name in _BRAND_EMBEDDED:
        return base64.b64decode(_BRAND_EMBEDDED[name]), "built into app.py"
    return None, "missing"


@app.get("/brand/status")
async def brand_status():
    """Open this page to see where each logo is being served from."""
    out = {}
    for name in _BRAND_EMBEDDED:
        data, where = _brand_bytes(name)
        out[name] = {"source": where, "bytes": len(data) if data else 0}
    return {"logos": out, "ok": all(v["bytes"] for v in out.values())}


@app.get("/brand/{name}")
async def brand_asset(name: str):
    from fastapi.responses import Response
    if not re.fullmatch(r"[a-z0-9-]+\.(webp|svg|png)", name):
        return Response(status_code=404)
    data, _ = _brand_bytes(name)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type=_BRAND_TYPES["." + name.rsplit(".", 1)[1]], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
@app.get("/favicon.svg")
async def favicon():
    from fastapi.responses import Response
    data, _ = _brand_bytes("horizon-mark.svg")
    return Response(data, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/horizon-tokens.css")
@app.get("/horizon-tokens.json")
async def horizon_tokens(request: Request):
    from fastapi.responses import Response
    name = request.url.path.lstrip("/")
    return Response((HERE / name).read_text(encoding="utf-8"), media_type="text/css" if name.endswith(".css") else "application/json",
                    headers={"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"})


@app.get("/nav-registry.js")
async def nav_registry_js():
    from fastapi.responses import Response
    js = (HERE / "app_shell.js").read_text(encoding="utf-8")                   # V16.0: the navigation registry now lives in the app shell
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "public, max-age=600"})


@app.get("/halfway", response_class=HTMLResponse)
async def halfway_page():
    """V14.0: Halfway Planner (live simulation: Recover Late Duty / Deploy OS Bus)."""
    return HTMLResponse((HERE / "halfway_planner.html").read_text(encoding="utf-8"))


@app.get("/halfway/os", response_class=HTMLResponse)
async def halfway_os_page():
    """the V14 planner (Recover Late Duty cross-direction + Deploy OS Bus), unchanged."""
    return HTMLResponse((HERE / "hplanner.html").read_text(encoding="utf-8"))


@app.get("/halfway/timetable", response_class=HTMLResponse)
async def halfway_timetable_page():
    """the V13 timetable-based Halfway Optimiser, unchanged (trip lateness at the first stop, approved halfway points, settings)."""
    return HTMLResponse((HERE / "halfway.html").read_text(encoding="utf-8"))


@app.get("/recovery-guide", response_class=HTMLResponse)
async def recovery_guide_page():
    return HTMLResponse((HERE / "recovery_guide.html").read_text(encoding="utf-8"))


@app.get("/api/bunching")
async def api_bunching(services: str = "", direction: int = 0):
    """Cached gap / bunching state for the selected services. Browsers never call DataMall; the collector does."""
    if time.time() - BB["last_req"] >= BB_IDLE_SEC:
        BB["wake_at"] = time.time()                          # first viewer after a quiet spell: recording restarts ("starting")
    BB["last_req"] = time.time()
    svcs = []
    for t in re.split(r"[,\s]+", (services or BB_DEFAULT).upper()):
        if t and re.fullmatch(r"[0-9A-Z]{1,6}", t) and t not in svcs:
            svcs.append(t)
    dirs = [direction] if direction in (1, 2) else [1, 2]
    keys = [(s, d) for s in svcs for d in dirs][:BB_MAX_PAIRS]
    for k in keys:
        BB["watch"][k] = time.time()
    P, alive = BB["params"], time.time() - BB["loop_at"] < 3 * BB["params"]["refresh_sec"]
    need = [k for k in keys if f"{k[0]}:{k[1]}" not in BB["rows"] or (not alive and time.time() - BB["rows"][f"{k[0]}:{k[1]}"].get("at", 0) > 2 * P["refresh_sec"])]
    if need:
        await bb_run(need)
        if not alive:
            per = {f"{k[0]}:{k[1]}": tuple(BB["rows"][f"{k[0]}:{k[1]}"]["counts"][c] for c in ("gaps", "bb", "bb2", "bb3", "bb4", "early"))
                   for k in keys if f"{k[0]}:{k[1]}" in BB["rows"] and not BB["rows"][f"{k[0]}:{k[1]}"].get("error")}
            BB["hist"].append({"ts": time.time(), "per": per})
    kk = [f"{k[0]}:{k[1]}" for k in keys]
    rows = [BB["rows"][k] for k in kk if k in BB["rows"] and not BB["rows"][k].get("error")]
    errors = []
    for k in kk:
        r = BB["rows"].get(k)
        if r and r.get("error") and not r.get("skip") and r["error"] not in errors:
            errors.append(r["error"])
    rows.sort(key=lambda r: (bunching.RISK_ORDER[r["risk"]], -r["score"], (not r["service"].isdigit()), int(r["service"]) if r["service"].isdigit() else 0, r["direction"]))
    now = time.time()
    cur = bb_sum(kk, {r["key"]: (r["counts"]["gaps"], r["counts"]["bb"], r["counts"]["bb2"], r["counts"]["bb3"], r["counts"]["bb4"], r["counts"]["early"]) for r in rows})
    prev, hist_min = None, round((now - BB["hist"][0]["ts"]) / 60, 1) if BB["hist"] else 0
    for h in reversed(BB["hist"]):
        if now - h["ts"] >= 27 * 60:
            prev = bb_sum(kk, h["per"])
            break
    trend = [[round(h["ts"]), bb_sum(kk, h["per"])["gaps"], bb_sum(kk, h["per"])["bb"]] for h in BB["hist"]]
    step = max(1, len(trend) // 120)
    return {"updated": now_sgt().isoformat(timespec="seconds"), "rows": [{k: v for k, v in r.items() if k not in ("groups_full",)} for r in rows],
            "kpi": {**cur, "services": len({r["service"] for r in rows}), "prev": prev, "history_min": hist_min}, "trend": trend[::step],
            "params": P, "errors": errors, "collector": {**BB["cycle"], "alive": alive, "db_ok": BB["db_ok"], "db_err": BB["db_err"], "max_pairs": BB_MAX_PAIRS,
                                                         "truncated": len(svcs) * len(dirs) > BB_MAX_PAIRS, "recording": bb_rec_status()},
            "open_events": sum(1 for e in bb_open_public() if e["service"] + ":" + str(e["direction"]) in kk), "alerts": bb_alerts(set(kk))}


@app.get("/api/bunching/live")
async def api_bunching_live(services: str = "", direction: int = 0):
    """Live Bunching & Long Headway: every monitored service-direction with an active 2BB / 3BB / 4BB+ / long-gap case, grouped
    (one row per service-direction, its cases inside), with the KPI cards from the same list and the change vs about 1 hour ago."""
    base = await api_bunching(services, direction)
    keys = {r["key"] for r in base["rows"]}
    rows = [BB["rows"][k] for k in keys if k in BB["rows"]]
    try:
        opmap = await occ_operator_map()
    except Exception:
        opmap = {}
    now = time.time()
    view = bunching_live.live_view(rows, list(BB["open"].values()), now, opmap)
    prev = prev_age = None
    for h in reversed(BB["hist"]):
        if now - h["ts"] >= 55 * 60 and h.get("per2") is not None:
            prev, prev_age = bunching_live.kpi_from_hist({k: v for k, v in h["per2"].items() if k in keys}), round((now - h["ts"]) / 60)
            break
    hist_min = round((now - BB["hist"][0]["ts"]) / 60, 1) if BB["hist"] else 0
    return {"updated": base["updated"], "updated_ts": now, **view, "kpi_prev": prev, "kpi_prev_min": prev_age, "history_min": hist_min,
            "params": base["params"], "errors": base["errors"], "collector": base["collector"],
            "rules": {"bunch_min": base["params"]["bunch_min"], "gap_add_min": base["params"]["gap_add_min"], "confirm_stops": base["params"]["confirm_stops"],
                      "gap_stops": base["params"]["gap_stops"], "gap_skip_stops": base["params"].get("gap_skip_stops", 0)},
            "source": "LTA DataMall Bus Arrival - estimated headways from arrival predictions (not confirmed actual headways)"}


@app.get("/api/bunching/detail")
async def api_bunching_detail(service: str = "", direction: int = 1):
    svc = service.strip().upper()
    key = f"{svc}:{direction}"
    if key not in BB["rows"] or BB["rows"][key].get("error"):
        await bb_run([(svc, direction)])
    row = BB["rows"].get(key)
    if not row or row.get("error"):
        return {"error": (row or {}).get("error", "Service not found")}
    route = await bb_route(svc, direction)
    try:
        inc = await api_incidents(svc, direction)
    except Exception:
        inc = {"incidents": [], "error": "unavailable"}
    recent = bb_open_public(key)
    recent += bb_sql("SELECT 'bb' AS kind, service, direction, start_ts AS start, end_ts AS end, bus_group AS buses, max_level AS level, stops, min_hw, start_stop, end_stop, start_stop AS location, alerts FROM bunching_event "
                     "WHERE service=? AND direction=? ORDER BY id DESC LIMIT 5", (svc, direction), fetch=True)
    return {"row": row, "route": {"stops": route.get("stops", []), "runs": route.get("runs", []), "geometry": route.get("geometry")},
            "incidents": {"count": len(inc.get("incidents", [])), "items": inc.get("incidents", [])[:3], "error": inc.get("error")}, "events": recent}


@app.get("/api/bunching/settings")
async def api_bb_settings():
    return {"params": BB["params"], "defaults": bunching.PARAMS, "ranges": PARAM_RANGES, "master": BB["master"], "tokenRequired": bool(BB_ADMIN), "db_ok": BB["db_ok"], "db_err": BB["db_err"],
            "dayType": bb_day_type(now_sgt())}


@app.post("/api/bunching/settings")
async def api_bb_settings_save(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    if body.get("reset"):
        BB["params"] = dict(bunching.PARAMS)
        bb_sql("DELETE FROM system_parameter")
    else:
        out, errs = bb_validate_params(body.get("params"))
        if errs:
            return JSONResponse({"error": "; ".join(errs)}, status_code=400)
        BB["params"].update(out)
        for k, v in out.items():
            bb_sql("INSERT OR REPLACE INTO system_parameter(k, v) VALUES (?, ?)", (k, v))
    BB["rows"].clear()          # cached results were computed with the old rules
    return {"ok": True, "params": BB["params"]}


@app.post("/api/bunching/master")
async def api_bb_master_save(request: Request):
    """Body: CSV/TSV text, or JSON {"rows": [...]}. Replaces the whole Service Headway Master."""
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    raw = (await request.body()).decode("utf-8-sig", "replace")
    if raw.lstrip().startswith("{"):
        try:
            rows, errs = parse_master("\n".join(",".join(str(r.get(k, "")) for k in ("service", "direction", "day_type", "from", "to", "hw")) for r in json.loads(raw).get("rows", [])))
        except ValueError:
            return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    else:
        rows, errs = parse_master(raw)
    if not rows:
        return JSONResponse({"error": "; ".join(errs[:3]) or "No usable rows.", "errors": errs}, status_code=400)
    bb_sql("DELETE FROM service_headway_config")
    for r in rows:
        bb_sql("INSERT INTO service_headway_config(service,direction,day_type,t_from,t_to,hw) VALUES (?,?,?,?,?,?)", (r["service"], r["direction"], r["day_type"], r["from"], r["to"], r["hw"]))
    BB["master"] = rows
    BB["rows"].clear()
    return {"ok": True, "rows": len(rows), "errors": errs, "master": rows}


@app.post("/api/bunching/master/clear")
async def api_bb_master_clear(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    bb_sql("DELETE FROM service_headway_config")
    BB["master"] = []
    BB["rows"].clear()
    return {"ok": True}


@app.get("/api/bunching/debug")
async def api_bb_debug(service: str = "", direction: int = 1):
    """What the tracker and the alert engine currently believe about one service direction (for support: send this if alerts look wrong)."""
    key = f"{service.strip().upper()}:{direction}"
    trk = BB["trk"].get(key)
    row = BB["rows"].get(key) or {}
    return {"version": VERSION, "key": key, "params": BB["params"], "now": time.time(), "row_at": row.get("at"), "risk": row.get("risk"),
            "tracker": None if trk is None else {"tracks": [{"id": t["id"], "km": round(t["km"], 2), "age_s": round(time.time() - t["ts"])} for t in trk.tracks],
                                                 "first": {f"{a}>{b}": j for (a, b), j in trk.first.items()}, "first_long": {f"{a}>{b}": j for (a, b), j in trk.firstg.items()}, "gap_sec": trk.gap_sec},
            "groups": [{k: g.get(k) for k in ("ids", "status", "stops", "since_j", "cur_j", "since_stop", "cur_stop")} for g in row.get("groups", [])],
            "events": [{"id": e["id"], "kind": e["kind"], "first_j": e["first_j"], "last_j": e["last_j"], "stops": bb_ev_stops(e), "started": e["start"], "last_seen": e.get("last"),
                        "miss": e.get("miss"), "ids": sorted(e.get("cur_ids", e["ids"])), "alerts": [{"n": a["n"], "ts": a["ts"], "at_stops": a["at_stops"], "stop": a["stop"]} for a in e["alerts"]]}
                       for e in BB["open"].values() if e["key"] == key]}


@app.post("/api/bunching/alerts/ack")
async def api_bb_alert_ack(request: Request):
    """ACT: the controller has seen / actioned this alert. It stays listed (marked) and turns un-acknowledged again if a further alert is raised."""
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
        eid = int(body.get("id"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Body must be JSON with the alert id."}, status_code=400)
    e = BB["open"].get(eid)
    if not e:
        return JSONResponse({"error": "That case is no longer active."}, status_code=404)
    undo = bool(body.get("undo"))
    who = _who(request)
    if undo:
        e["acked_n"], e["acked_ts"], e["acked_by"] = 0, None, None
    else:
        e["acked_n"], e["acked_ts"], e["acked_by"] = len(e.get("alerts") or []), time.time(), who
    bb_sql("INSERT INTO bb_ack(event_id, kind, service, direction, action, who, ts) VALUES (?,?,?,?,?,?,?)",
           (eid, e["kind"], e["service"], e["direction"], "unacknowledged" if undo else "acknowledged", who, time.time()))      # audit trail
    return {"ok": True, "alert": bb_alert_public(e) if e.get("alerts") else None, "acked_ts": e["acked_ts"], "acked_by": e.get("acked_by")}


@app.get("/api/bunching/ack-log")
async def api_bb_ack_log(limit: int = 100):
    return {"log": bb_sql("SELECT event_id, kind, service, direction, action, who, ts FROM bb_ack ORDER BY id DESC LIMIT ?", (max(1, min(500, limit)),), fetch=True)}


# ---------------------------------------------------------------------------
# V16.10: OCC Live - one combined alert queue (bunching + long headway + traffic: congestion, incident,
# roadworks, weather) plus OCC Connect (shift notes, handover, sharing). Nothing here invents alerts:
# it reads the same bb_alerts() / traffic.overview() the Headway Control and Route Traffic pages already use.
OCC_KIND_GROUP = {"bb": "bunching", "gap": "headway", "congestion": "traffic", "incident": "incident", "roadworks": "roadwork", "weather": "system"}


def _who(request: Request) -> str:
    sess = auth.read(request.cookies.get(auth.COOKIE, ""))
    if sess:
        u = sess.get("u") or {}
        return u.get("display_name") or u.get("staff_id") or "Controller"
    return "Controller"


async def occ_operator_map():
    """service -> operator code (SBST, SMRT, TTS, GAS...), from the same timetable data the Route Traffic page uses."""
    fq = await freq_table()
    out = {}
    for (svc, d), r in fq["freqs"].items():
        op = r.get("Operator")
        if op and svc not in out:
            out[svc] = op
    return out


@app.get("/api/occ/operators")
async def api_occ_operators():
    m = await occ_operator_map()
    return {"operators": sorted(set(m.values()))}


@app.get("/api/occ/queue")
async def api_occ_queue():
    opmap = await occ_operator_map()
    now = time.time()
    items = []
    for a in bb_alerts():
        sev = "critical" if (a["kind"] == "gap" or (a.get("level") or 0) >= 3) else "high"
        items.append({"id": f"bb:{a['id']}", "src": "bunching", "group": OCC_KIND_GROUP[a["kind"]], "severity": sev,
                      "status": "acknowledged" if a["acked"] else ("re-escalated" if (a["acked"] and a.get("count", 0) > 0) else "new"),
                      "title": f"Svc {a['service']} – {a['label']}", "service": a["service"], "dir": a["direction"],
                      "location": a.get("start_stop") or "", "detail": f"{a['stops']} stops" + (f" · min headway {a['min_hw']:.0f} min" if a.get("min_hw") else ""),
                      "since": a["start"], "last": a.get("last") or a["start"], "acked_by": None, "acked_at": a.get("acked_ts"),
                      "href": f"/bunching?svc={a['service']}&dir={a['direction']}", "raw_id": a["id"], "operator": opmap.get(a["service"], "")})
    try:
        ov = traffic.overview(TR["book"], TR["params"], now, None, 0, 0, None, ["unacknowledged", "acknowledged"])
        for r in ov["rows"]:
            sev = "critical" if r["level"] == "critical" else "high" if r["level"] == "high" else "medium" if r["level"] == "monitor" else "low"
            items.append({"id": f"tr:{r['id']}", "src": "traffic", "group": OCC_KIND_GROUP.get(r["kind"], "traffic"), "severity": sev,
                          "status": "re-escalated" if r.get("worsened") else ("acknowledged" if r["status"] != "new" else "new"),
                          "title": f"Svc {r['svc']} – {r['kind'].capitalize()}", "service": r["svc"], "dir": r["dir"],
                          "location": r.get("location") or "", "detail": (f"+{r['delay_min']:.0f} min delay" if r.get("delay_min") else "") + (f" · {r['n_services']} services" if r.get("n_services", 1) > 1 else ""),
                          "since": r["start"], "last": r["start"], "acked_by": r.get("acked_by"), "acked_at": r.get("acked_time"),
                          "href": f"/?svc={r['svc']}&dir={r['dir']}", "raw_id": r["id"], "operator": opmap.get(r["svc"], "")})
    except Exception:
        pass
    try:
        items += dv_queue_items()                                                     # V16.14: Diversion Maps (diversion impact / active diversions)
    except Exception:
        pass
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    # V16.12: several alerts on the same service+type (e.g. a bunching alert on two directions, or bunching + a
    # traffic alert on the same service) become ONE card, so the SC is not asked to press Acknowledge many times
    # for what is really one situation. Acknowledging a merged card acknowledges every alert inside it.
    groups, singles = {}, []
    for a in items:
        key = (a["group"], a["service"]) if a["service"] else None
        (groups.setdefault(key, []) if key else singles).append(a)
    merged = []
    for key, g in groups.items():
        if len(g) == 1:
            merged.append(g[0]); continue
        g.sort(key=lambda x: SEV_ORDER.get(x["severity"], 2))
        locs = list(dict.fromkeys(x["location"] for x in g if x["location"]))
        merged.append({"id": "grp:" + "|".join(x["id"] for x in g), "src": "grouped", "group": key[0], "severity": g[0]["severity"],
                       "status": "acknowledged" if all(x["status"] == "acknowledged" for x in g) else ("re-escalated" if any(x["status"] == "re-escalated" for x in g) else "new"),
                       "title": f"Svc {key[1]} – {len(g)} alerts", "service": key[1], "dir": None,
                       "location": "; ".join(locs[:3]) + (f" +{len(locs)-3} more" if len(locs) > 3 else ""),
                       "detail": " · ".join(dict.fromkeys(x["title"].split("–", 1)[-1].strip() for x in g)),
                       "since": min(x["since"] or now for x in g), "last": max(x["last"] or now for x in g),
                       "acked_by": None, "acked_at": None, "href": g[0]["href"], "raw_id": None, "operator": opmap.get(key[1], "")})
    items2 = merged + singles
    items2.sort(key=lambda x: (0 if x["status"] != "acknowledged" else 1, SEV_ORDER.get(x["severity"], 2), -(x["last"] or 0)))
    resolved_today = bb_sql("SELECT COUNT(*) n FROM alert_acknowledgement WHERE ack_time > ?", (now - (now % 86400),), fetch=True)
    return {"items": items2, "counts": {"critical": sum(1 for x in items2 if x["status"] != "acknowledged" and x["severity"] == "critical"),
            "action": sum(1 for x in items2 if x["status"] != "acknowledged" and x["severity"] in ("high", "medium")),
            "monitoring": sum(1 for x in items2 if x["status"] == "acknowledged"),
            "resolved_today": (resolved_today[0]["n"] if resolved_today else 0)}, "time": now_sgt().isoformat(timespec="seconds")}


def _occ_ack_one(oid, by):
    """Acknowledge a single bb:/tr: item id. Returns True if something was acknowledged."""
    if oid.startswith("dv:"):                                                       # V16.14: Diversion Maps alert
        try:
            return dv_ack_alert(int(oid[3:]), by)
        except ValueError:
            return False
    if oid.startswith("bb:"):
        e = BB["open"].get(int(oid[3:]))
        if not e or not e.get("alerts"):
            return False
        e["acked_n"], e["acked_ts"] = len(e["alerts"]), time.time()
        return True
    if oid.startswith("tr:"):
        done = traffic.acknowledge(TR["book"], [oid[3:]], by, time.time())
        for aid in done:
            p = aid.split(":")
            bb_sql("INSERT INTO alert_acknowledgement(alert_id, event_id, service, direction, ack_time, ack_by, condition) VALUES (?,?,?,?,?,?,?)",
                   (aid, p[0], p[1], int(p[2]), time.time(), by, json.dumps(TR["book"]["acks"][aid]["snap"])))
        if done:
            tr_save()
        return bool(done)
    return False


@app.post("/api/occ/ack")
async def api_occ_ack(request: Request):
    """ACK for the combined queue. Body: {"id": "bb:12"} for one alert, {"id": "grp:bb:12|tr:x:0"} for a merged
    card (acks every alert inside it), or {"ids": [...]} to acknowledge several cards/alerts at once ("Acknowledge all")."""
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except (ValueError, TypeError):
        return JSONResponse({"error": "Body must be JSON with the item id."}, status_code=400)
    by = _who(request)
    ids = body.get("ids") if isinstance(body.get("ids"), list) else ([body.get("id")] if body.get("id") else [])
    if not ids:
        return JSONResponse({"error": "Body must be JSON with the item id."}, status_code=400)
    n = 0
    for oid in ids:
        oid = str(oid or "")
        members = oid[4:].split("|") if oid.startswith("grp:") else [oid]
        for m in members:
            if _occ_ack_one(m, by):
                n += 1
    return {"ok": True, "acked": n}


def _note_public(n, replies):
    return {"id": n["id"], "tab": n["tab"], "kind": n["kind"], "title": n["title"], "body": n["body"],
            "services": [x for x in (n["services"] or "").split(",") if x], "author": n["author"], "pinned": bool(n["pinned"]),
            "source_alert": n["source_alert"], "created": n["created_ts"], "updated": n["updated_ts"],
            "replies": [{"author": r["author"], "body": r["body"], "ts": r["ts"]} for r in replies]}


@app.get("/api/occ/notes")
async def api_occ_notes(tab: str = "all"):
    rows = bb_sql("SELECT * FROM occ_note ORDER BY pinned DESC, updated_ts DESC LIMIT 300", fetch=True)
    if tab != "all":
        rows = [r for r in rows if r["tab"] == tab or (tab == "pinned" and r["pinned"])]
    ids = [r["id"] for r in rows]
    reps = {}
    if ids:
        q = "SELECT * FROM occ_reply WHERE note_id IN (%s) ORDER BY ts ASC" % ",".join("?" * len(ids))
        for r in bb_sql(q, ids, fetch=True):
            reps.setdefault(r["note_id"], []).append(r)
    return {"notes": [_note_public(r, reps.get(r["id"], [])) for r in rows], "saved": bool(BB.get("db_ok"))}


@app.post("/api/occ/notes")
async def api_occ_notes_create(request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    tab = body.get("tab") if body.get("tab") in ("mydesk", "shared", "handover") else "mydesk"
    title = str(body.get("title") or "").strip()[:120] or "Note"
    text = str(body.get("body") or "").strip()[:2000]
    if not text:
        return JSONResponse({"error": "Note text is required."}, status_code=400)
    services = ",".join(str(x).strip()[:12] for x in (body.get("services") or [])[:8] if str(x).strip())
    kind = str(body.get("kind") or "general").strip()[:24]
    remind = body.get("remind_at")
    now = time.time()
    rid = bb_insert("INSERT INTO occ_note(tab, kind, title, body, services, author, pinned, source_alert, created_ts, updated_ts) VALUES (?,?,?,?,?,?,0,?,?,?)",
                    (tab, kind, title, text + (f"\n\u23f0 Reminder: {remind}" if remind else ""), services, _who(request), body.get("source_alert"), now, now))
    if not rid:
        return JSONResponse({"error": "The note could not be saved (database unavailable)."}, status_code=503)
    row = bb_sql("SELECT * FROM occ_note WHERE id=?", (rid,), fetch=True)[0]
    return {"ok": True, "note": _note_public(row, []), "saved": bool(BB.get("db_ok"))}


@app.post("/api/occ/notes/{note_id}/reply")
async def api_occ_notes_reply(note_id: int, request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
        text = str(body.get("body") or "").strip()[:2000]
    except ValueError:
        text = ""
    if not text:
        return JSONResponse({"error": "Reply text is required."}, status_code=400)
    if not bb_sql("SELECT id FROM occ_note WHERE id=?", (note_id,), fetch=True):
        return JSONResponse({"error": "Note not found."}, status_code=404)
    now = time.time()
    bb_sql("INSERT INTO occ_reply(note_id, author, body, ts) VALUES (?,?,?,?)", (note_id, _who(request), text, now))
    bb_sql("UPDATE occ_note SET updated_ts=? WHERE id=?", (now, note_id))
    return {"ok": True}


@app.post("/api/occ/notes/{note_id}/pin")
async def api_occ_notes_pin(note_id: int, request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        body = {}
    row = bb_sql("SELECT id FROM occ_note WHERE id=?", (note_id,), fetch=True)
    if not row:
        return JSONResponse({"error": "Note not found."}, status_code=404)
    bb_sql("UPDATE occ_note SET pinned=? WHERE id=?", (1 if body.get("pinned", True) else 0, note_id))
    return {"ok": True}


@app.post("/api/occ/notes/{note_id}/tab")
async def api_occ_notes_tab(note_id: int, request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
        tab = body.get("tab")
    except ValueError:
        tab = None
    if tab not in ("mydesk", "shared", "handover"):
        return JSONResponse({"error": "tab must be mydesk, shared or handover."}, status_code=400)
    if not bb_sql("SELECT id FROM occ_note WHERE id=?", (note_id,), fetch=True):
        return JSONResponse({"error": "Note not found."}, status_code=404)
    bb_sql("UPDATE occ_note SET tab=?, updated_ts=? WHERE id=?", (tab, time.time(), note_id))
    return {"ok": True}
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# V16.11: OCC Notes & Actions - a lightweight OCC ticket. One operational matter (accident, bunching,
# traffic, handover, reminder ...) becomes one persistent record: owner, shared OCCs, watchers, a
# timestamped activity log, deliberate acknowledgement (separate from just opening it), handover and
# resolution. The old /api/occ/notes (sticky notes) endpoints above are kept so nothing already saved is
# lost, but OCC Live now uses this instead.
OCC_TEAMS = tuple(x.strip() for x in os.getenv("OCC_TEAMS", "SE1,SE2,UP1,UP2,SWOCC").split(",") if x.strip())
OCC_CATEGORIES = ("Accident", "Incident", "Breakdown", "Traffic", "Bunching", "Long Headway", "Service Regulation",
                   "Diversion", "Handover", "Reminder", "Instruction", "Equipment", "General")
OCC_PRIORITIES = ("critical", "high", "normal", "fyi")
OCC_STATUSES = ("new", "acknowledged", "action", "monitoring", "handover", "resolved")


def occ_log(ticket_id, actor, action, detail=""):
    bb_sql("INSERT INTO occ_activity(ticket_id, ts, actor, action, detail) VALUES (?,?,?,?,?)", (ticket_id, time.time(), actor, action, detail))


def occ_no():
    day = now_sgt().strftime("%y%m%d")
    n = bb_sql("SELECT COUNT(*) n FROM occ_ticket WHERE no LIKE ?", (f"OCC-{day}-%",), fetch=True)
    return f"OCC-{day}-{(n[0]['n'] if n else 0) + 1:03d}"


def occ_public(t, activity=None, acks=None):
    return {"id": t["id"], "no": t["no"], "title": t["title"], "category": t["category"], "priority": t["priority"], "status": t["status"],
            "service": t["service"], "direction": t["direction"], "bus_reg": t["bus_reg"], "location": t["location"], "description": t["description"],
            "owner": t["owner"], "shared": [x for x in (t["shared"] or "").split(",") if x], "watchers": [x for x in (t["watchers"] or "").split(",") if x],
            "personal": bool(t["personal"]), "pinned": bool(t["pinned"]), "source_alert": t["source_alert"], "author": t["author"],
            "created": t["created_ts"], "updated": t["updated_ts"], "resolved": t["resolved_ts"],
            "resolution": {"outcome": t["resolution_outcome"], "action": t["resolution_action"], "notes": t["resolution_notes"]} if t["resolved_ts"] else None,
            "activity": [{"ts": a["ts"], "actor": a["actor"], "action": a["action"], "detail": a["detail"]} for a in (activity or [])],
            "acks": [{"who": a["who"], "ts": a["ts"]} for a in (acks or [])], "activity_n": len(activity) if activity is not None else None}


def occ_get(ticket_id):
    row = bb_sql("SELECT * FROM occ_ticket WHERE id=?", (ticket_id,), fetch=True)
    return row[0] if row else None


def occ_touch(ticket_id):
    bb_sql("UPDATE occ_ticket SET updated_ts=? WHERE id=?", (time.time(), ticket_id))


@app.get("/api/occ/meta")
async def api_occ_meta():
    return {"teams": list(OCC_TEAMS), "categories": list(OCC_CATEGORIES), "priorities": list(OCC_PRIORITIES), "statuses": list(OCC_STATUSES)}


@app.get("/api/occ/tickets")
async def api_occ_tickets(status: str = "", mine: int = 0, watching: int = 0, pinned: int = 0, handover: int = 0,
                           include_resolved: int = 0, personal_only: int = 0, q: str = "", operator: str = "", service: str = "", request: Request = None):
    who = _who(request) if request else "Controller"
    opmap = await occ_operator_map()
    rows = bb_sql("SELECT * FROM occ_ticket ORDER BY pinned DESC, updated_ts DESC LIMIT 500", fetch=True)
    out = []
    for t in rows:
        if t["personal"] and t["owner"] != who and who not in (t["watchers"] or "").split(","):
            continue                                                            # a personal note is only visible to its owner unless shared/watched
        if not include_resolved and t["status"] == "resolved" and not (status == "resolved" or handover):
            continue
        if status and t["status"] != status:
            continue
        if mine and t["owner"] != who:
            continue
        if watching and who not in (t["watchers"] or "").split(","):
            continue
        if pinned and not t["pinned"]:
            continue
        if handover and t["status"] != "handover":
            continue
        if personal_only and not t["personal"]:
            continue
        if service and str(t["service"] or "").upper() != service.strip().upper():
            continue
        if operator and opmap.get(str(t["service"] or "").upper(), "") != operator.strip().upper():
            continue
        if q:
            hay = " ".join(str(t[k] or "") for k in ("title", "description", "service", "location", "bus_reg", "no")).lower()
            if q.lower() not in hay:
                continue
        out.append(t)
    counts = {"critical": sum(1 for t in rows if t["priority"] == "critical" and t["status"] != "resolved"),
              "action": sum(1 for t in rows if t["status"] in ("new", "action") and t["priority"] != "critical"),
              "monitoring": sum(1 for t in rows if t["status"] == "monitoring"),
              "handover": sum(1 for t in rows if t["status"] == "handover"),
              "resolved_today": sum(1 for t in rows if t["resolved_ts"] and t["resolved_ts"] > time.time() - (time.time() % 86400))}
    ids = [t["id"] for t in out]
    acount = {}
    if ids:
        for r in bb_sql("SELECT ticket_id, COUNT(*) n FROM occ_activity WHERE ticket_id IN (%s) GROUP BY ticket_id" % ",".join("?" * len(ids)), ids, fetch=True):
            acount[r["ticket_id"]] = r["n"]
    pub = [dict(occ_public(t), activity_n=acount.get(t["id"], 0), operator=opmap.get(str(t["service"] or "").upper(), "")) for t in out]
    return {"tickets": pub, "counts": counts, "who": who}


@app.post("/api/occ/tickets")
async def api_occ_tickets_create(request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    title = str(body.get("title") or "").strip()[:140]
    if not title:
        return JSONResponse({"error": "Title is required."}, status_code=400)
    who = _who(request)
    cat = body.get("category") if body.get("category") in OCC_CATEGORIES else "General"
    pri = body.get("priority") if body.get("priority") in OCC_PRIORITIES else "normal"
    personal = bool(body.get("personal"))
    owner = str(body.get("owner") or who).strip()[:40]
    shared = ",".join(str(x).strip()[:12] for x in (body.get("shared") or [])[:8] if str(x).strip() in OCC_TEAMS)
    watchers = ",".join(str(x).strip()[:40] for x in (body.get("watchers") or [])[:12] if str(x).strip())
    now = time.time()
    no = occ_no()
    tid = bb_insert("""INSERT INTO occ_ticket(no, title, category, priority, status, service, direction, bus_reg, location, description, owner, shared, watchers,
                                      personal, pinned, source_alert, created_ts, updated_ts, author)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
           (no, title, cat, pri, "new", str(body.get("service") or "").strip()[:12], int(body.get("direction") or 0) or None,
            str(body.get("bus_reg") or "").strip()[:16], str(body.get("location") or "").strip()[:120], str(body.get("description") or "").strip()[:4000],
            owner, shared, watchers, 1 if personal else 0, body.get("source_alert"), now, now, who))
    if not tid:
        return JSONResponse({"error": "The note could not be saved (database unavailable)."}, status_code=503)
    occ_log(tid, who, "created", f"{'Personal note' if personal else 'OCC note'} created" + (f" from alert {body['source_alert']}" if body.get("source_alert") else ""))
    if shared:
        occ_log(tid, who, "shared", "Shared with " + shared.replace(",", ", "))
    return {"ok": True, "ticket": occ_public(occ_get(tid)), "saved": bool(BB.get("db_ok"))}


@app.get("/api/occ/tickets/{ticket_id}")
async def api_occ_ticket_get(ticket_id: int):
    t = occ_get(ticket_id)
    if not t:
        return JSONResponse({"error": "Note not found."}, status_code=404)
    act = bb_sql("SELECT * FROM occ_activity WHERE ticket_id=? ORDER BY ts ASC", (ticket_id,), fetch=True)
    acks = bb_sql("SELECT * FROM occ_ack WHERE ticket_id=? ORDER BY ts ASC", (ticket_id,), fetch=True)
    return {"ticket": occ_public(t, act, acks)}


def _occ_require(ticket_id):
    t = occ_get(ticket_id)
    if not t:
        return None, JSONResponse({"error": "Note not found."}, status_code=404)
    return t, None


@app.post("/api/occ/tickets/{ticket_id}/comment")
async def api_occ_comment(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    text = str(body.get("body") or "").strip()[:4000]
    if not text:
        return JSONResponse({"error": "Comment text is required."}, status_code=400)
    who = _who(request)
    occ_log(ticket_id, who, "comment", text)
    occ_touch(ticket_id)
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/status")
async def api_occ_status(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    new = body.get("status")
    if new not in OCC_STATUSES:
        return JSONResponse({"error": "Unknown status."}, status_code=400)
    who = _who(request)
    bb_sql("UPDATE occ_ticket SET status=?, updated_ts=? WHERE id=?", (new, time.time(), ticket_id))
    occ_log(ticket_id, who, "status", f"Status changed: {t['status'].upper()} → {new.upper()}")
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/owner")
async def api_occ_owner(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    new = str(body.get("owner") or "").strip()[:40]
    if not new:
        return JSONResponse({"error": "Owner is required."}, status_code=400)
    who = _who(request)
    bb_sql("UPDATE occ_ticket SET owner=?, updated_ts=? WHERE id=?", (new, time.time(), ticket_id))
    occ_log(ticket_id, who, "owner", f"Owner changed: {t['owner'] or '(none)'} → {new}")
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/share")
async def api_occ_share(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    occ = str(body.get("occ") or "").strip()
    if occ not in OCC_TEAMS:
        return JSONResponse({"error": "Unknown OCC team."}, status_code=400)
    cur = [x for x in (t["shared"] or "").split(",") if x]
    if occ not in cur:
        cur.append(occ)
        bb_sql("UPDATE occ_ticket SET shared=?, updated_ts=? WHERE id=?", (",".join(cur), time.time(), ticket_id))
        occ_log(ticket_id, _who(request), "shared", f"Shared with {occ}")
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/watch")
async def api_occ_watch(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    who = _who(request)
    cur = [x for x in (t["watchers"] or "").split(",") if x]
    add = body.get("add", True)
    if add and who not in cur:
        cur.append(who)
        occ_log(ticket_id, who, "watch", f"{who} started watching")
    elif not add and who in cur:
        cur.remove(who)
        occ_log(ticket_id, who, "watch", f"{who} stopped watching")
    bb_sql("UPDATE occ_ticket SET watchers=? WHERE id=?", (",".join(cur), ticket_id))
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/ack")
async def api_occ_ack2(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    who = _who(request)
    if bb_sql("SELECT id FROM occ_ack WHERE ticket_id=? AND who=?", (ticket_id, who), fetch=True):
        return {"ok": True, "already": True}
    bb_sql("INSERT INTO occ_ack(ticket_id, who, ts) VALUES (?,?,?)", (ticket_id, who, time.time()))
    occ_log(ticket_id, who, "ack", f"✓ Acknowledged by {who}")
    if t["status"] == "new":
        bb_sql("UPDATE occ_ticket SET status='acknowledged', updated_ts=? WHERE id=?", (time.time(), ticket_id))
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/pin")
async def api_occ_pin(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    bb_sql("UPDATE occ_ticket SET pinned=? WHERE id=?", (1 if body.get("pinned", True) else 0, ticket_id))
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/priority")
async def api_occ_priority(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    new = body.get("priority")
    if new not in OCC_PRIORITIES:
        return JSONResponse({"error": "Unknown priority."}, status_code=400)
    who = _who(request)
    bb_sql("UPDATE occ_ticket SET priority=?, updated_ts=? WHERE id=?", (new, time.time(), ticket_id))
    occ_log(ticket_id, who, "priority", f"Priority changed: {t['priority'].upper()} → {new.upper()}")
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/handover")
async def api_occ_handover(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    who = _who(request)
    if body.get("include", True):
        bb_sql("UPDATE occ_ticket SET status='handover', updated_ts=? WHERE id=?", (time.time(), ticket_id))
        occ_log(ticket_id, who, "handover", "Added to shift handover")
    else:
        occ_log(ticket_id, who, "handover", f"Handover acknowledged by {who}")
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/personal")
async def api_occ_personal(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    who = _who(request)
    bb_sql("UPDATE occ_ticket SET personal=0, updated_ts=? WHERE id=?", (time.time(), ticket_id))
    occ_log(ticket_id, who, "status", "Converted: Personal note → OCC note")
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/resolve")
async def api_occ_resolve(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    who = _who(request)
    now = time.time()
    bb_sql("UPDATE occ_ticket SET status='resolved', resolved_ts=?, resolution_outcome=?, resolution_action=?, resolution_notes=?, updated_ts=? WHERE id=?",
           (now, str(body.get("outcome") or "")[:400], str(body.get("action") or "")[:800], str(body.get("notes") or "")[:800], now, ticket_id))
    occ_log(ticket_id, who, "resolve", "Note resolved" + (f" — {body['outcome']}" if body.get("outcome") else ""))
    return {"ok": True}


@app.post("/api/occ/tickets/{ticket_id}/reopen")
async def api_occ_reopen(ticket_id: int, request: Request):
    t, err = _occ_require(ticket_id)
    if err:
        return err
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    who = _who(request)
    bb_sql("UPDATE occ_ticket SET status='monitoring', resolved_ts=NULL, updated_ts=? WHERE id=?", (time.time(), ticket_id))
    occ_log(ticket_id, who, "reopen", "Note reopened" + (f" — {body['reason']}" if body.get("reason") else ""))
    return {"ok": True}
# ---------------------------------------------------------------------------




@app.get("/api/bunching/events")
async def api_bb_events(limit: int = 100):
    limit = max(1, min(500, limit))
    closed = bb_sql("SELECT 'bb' AS kind, service, direction, start_ts AS start, end_ts AS end, bus_group AS buses, max_level AS level, stops, min_hw, NULL AS max_hw, start_stop, end_stop, start_stop AS location, alerts FROM bunching_event "
                    "UNION ALL SELECT 'gap', service, direction, start_ts, end_ts, buses, NULL, stops, NULL, max_hw, start_stop, end_stop, location, alerts FROM gap_event ORDER BY start DESC LIMIT ?", (limit,), fetch=True)
    return {"events": bb_open_public() + closed, "db_ok": BB["db_ok"], "min_stops": {"bunching": int(BB["params"]["confirm_stops"]), "gap": int(BB["params"]["gap_stops"])}}


# Bunching & Gap analytics: top services, hour of day, weekday x hour, daily trend, hotspot roads / stops - all from the LOGGED events
# (bunching_event / gap_event + cases still open). Read-only; nothing is estimated. Aggregation lives in bunching_analytics.py.
_BB_AN_SQL = ("SELECT 'bb' AS kind, service, direction, start_ts AS start, end_ts AS end, max_level AS level, stops, start_stop, end_stop, alerts FROM bunching_event WHERE start_ts >= ? AND start_ts < ? "
              "UNION ALL SELECT 'gap', service, direction, start_ts, end_ts, NULL, stops, COALESCE(start_stop, location), end_stop, alerts FROM gap_event WHERE start_ts >= ? AND start_ts < ?")


_BB_TRIP_SQL = "SELECT trip_key, kind, service, direction, start_ts AS start, last_ts AS end, level, stops, start_stop FROM bb_trip WHERE start_ts >= ? AND start_ts < ?"


@app.get("/api/bunching/analytics")
async def api_bb_analytics(period: str = "7d", kind: str = "all", direction: int = 0, service: str = "", operator: str = "", services: str = "", measure: str = "trips",
                           hour: int = -1, stop: str = "", top: int = 10):
    p = bunching_analytics.period_bounds(period, time.time())
    measure = "cases" if measure == "cases" else "trips"
    h0 = lambda t: int(t // 3600) * 3600
    if measure == "trips":                                                           # one count per bunched / long-gap trip (15+ stops)
        cur = bb_sql(_BB_TRIP_SQL, (p["start"], p["end"]), fetch=True)
        prev = bb_sql(_BB_TRIP_SQL, (p["prev_start"], p["prev_end"]), fetch=True)
        running = {e["trip_key"] for e in BB["open"].values() if e.get("trip_key")}
        for r in cur:
            r["open"] = r.pop("trip_key") in running
        for r in prev:
            r.pop("trip_key", None)
    else:                                                                            # logged cases (15+ stops) and cases still running
        cur = bb_sql(_BB_AN_SQL, (p["start"], p["end"], p["start"], p["end"]), fetch=True)
        prev = bb_sql(_BB_AN_SQL, (p["prev_start"], p["prev_end"], p["prev_start"], p["prev_end"]), fetch=True)
        for e in bb_open_public():
            cur.append({"kind": e["kind"], "service": e["service"], "direction": e["direction"], "start": e["start"], "end": None, "level": e.get("level"),
                        "stops": e.get("stops"), "start_stop": e.get("start_stop") or e.get("location"), "end_stop": e.get("end_stop"), "alerts": e.get("alerts"), "open": True})
    try:
        stops = (await static()).get("stops") or {}
    except Exception:
        stops = {}
    try:
        opmap = await occ_operator_map()                                            # also returned, for "group by operator"
    except Exception:
        opmap = {}
    svc_list = [x for x in re.split(r"[,\s]+", services.upper()) if re.fullmatch(r"[0-9A-Z]{1,6}", x)][:60]
    out = bunching_analytics.aggregate(cur, prev, stops, p, kind=kind if kind in ("bb", "gap") else "all", direction=direction if direction in (1, 2) else 0,
                                       service=re.sub(r"[^0-9A-Za-z]", "", service)[:6], operator=operator.strip().upper()[:6], opmap=opmap, services=svc_list,
                                       top_n=max(10, min(300, top)), hour=hour if 0 <= hour <= 23 else None, stop=re.sub(r"[^0-9A-Za-z ()'&.-]", "", stop)[:80])
    cov_rows = bb_sql("SELECT hour_ts, service, direction, sec FROM bb_cov WHERE hour_ts >= ? AND hour_ts < ?", (h0(p["start"]), p["end"]), fetch=True)
    if svc_list:
        cov_rows = [r for r in cov_rows if r["service"] in svc_list]
    if direction in (1, 2):
        cov_rows = [r for r in cov_rows if int(r["direction"] or 0) == direction]
    first = bb_sql("SELECT MIN(t) AS t FROM (SELECT MIN(start_ts) AS t FROM bunching_event UNION ALL SELECT MIN(start_ts) FROM gap_event)", fetch=True)
    first_obs = bb_sql("SELECT MIN(hour_ts) AS t FROM bb_cov", fetch=True)                # when this server's recorded history begins
    rec = bb_rec_status()
    out["operators"] = {x["service"]: opmap.get(x["service"].upper(), "") for x in out.get("services", [])}
    out.update(measure=measure, coverage=bunching_analytics.coverage(cov_rows, p), db_ok=BB["db_ok"], db_err=BB["db_err"], first_ts=(first[0]["t"] if first else None),
               first_obs_ts=(first_obs[0]["t"] if first_obs else None),
               always_on=BB_ALWAYS, keep_days=BB_KEEP_DAYS, collecting=rec["state"] in ("on", "starting"), recording=rec,
               gate={"bunching": int(BB["params"]["confirm_stops"]), "gap": int(BB["params"]["gap_stops"])})
    return out


# =========================================================================== AI Halfway Optimiser
# Simulate losing ONE trip of a scheduled 10-trip sequence (timed at the first bus stop); the optimiser tests regulating the headway (hold / release trips),
# a replacement from every approved halfway stop, and the two together, scores them and recommends one. It does not identify a physical bus (LTA has no
# schedule / duty data). Engine: halfway.py. Decision support only: nothing is deployed.
import halfway
import bisect

HO = {"params": dict(halfway.PARAMS), "points": []}
HO_INT = ("n_trips", "reg_window", "recover_points", "reg_min_side", "reg_even_share", "max_disrupted", "balance_trips", "allow_adjacent_halfway", "term_reg", "term_zone")
HO_RANGES = {"n_trips": (5, 20), "layover_min": (0, 60), "min_layover_min": (0, 30), "start_delay_min": (-30, 60), "reg_hold_max": (0, 8), "reg_early_max": (0, 30), "reg_window": (1, 9), "reg_min_side": (0, 5), "auto_late_min": (0, 120), "reg_even_share": (0, 1), "max_disrupted": (1, 6), "reg_early_future": (0, 30),
             "min_dep_gap": (0, 10), "start_early_max": (0, 30), "start_late_max": (0, 60), "min_remaining_pct": (0, 90), "min_improve_pct": (0, 100), "max_mileage_km": (0.5, 100),
             "recover_tol_pct": (5, 100), "recover_points": (1, 8), "w_regularity": (0, 100), "w_maxgap": (0, 100), "w_recovery": (0, 100), "w_holding": (0, 100), "w_mileage": (0, 100),
             "w_bunching": (0, 100), "pref_recovery_boost": (1, 5), "pref_mileage_boost": (1, 10), "load_sens": (0, 0.3), "fallback_kmh": (5, 60), "offsvc_factor": (0.3, 1.0),
             "balance_trips": (1, 16), "ewt_gain_min": (0, 5), "ewt_gain_pct": (0, 100), "ewt_gain_per_km": (0, 1), "ewt_adjust_min": (0, 5), "allow_adjacent_halfway": (0, 1),
             "term_reg": (0, 1), "term_hold_max": (0, 8), "term_early_max": (0, 8), "term_tol_pct": (5, 100), "term_zone": (1, 9), "term_total_max": (0, 20)}
HO_MAX_CANDIDATES = 12
HO_MAX_SCAN = 60                                                             # stops the AI tests when it searches the whole route (a stride is used on longer routes)


def ho_init():
    bb_sql("CREATE TABLE IF NOT EXISTS halfway_parameter(k TEXT PRIMARY KEY, v REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS halfway_point_master(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, direction INTEGER, stop_code TEXT, seq INTEGER, enabled INTEGER, "
           "min_late REAL, min_remaining_pct REAL, max_offservice_min REAL, max_mileage_km REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS halfway_sim(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, service TEXT, direction INTEGER, ref_time TEXT, sched_hw REAL, disrupted INTEGER, "
           "lateness TEXT, start_delay REAL, recommended TEXT, improvement_pct REAL, no_halfway TEXT, best TEXT, scenarios TEXT, params TEXT, model_version TEXT)")
    have = {r["name"] for r in bb_sql("PRAGMA table_info(halfway_sim)", fetch=True)}
    for col, typ in (("pref", "TEXT"), ("rec_label", "TEXT"), ("score", "REAL"), ("hold_min", "REAL")):
        if col not in have:
            bb_sql(f"ALTER TABLE halfway_sim ADD COLUMN {col} {typ}")
    if not bb_sql("SELECT 1 FROM halfway_parameter WHERE k='mig_v81'", fetch=True):          # V8.1: the regulation window default went from 2 to 5 trips; an old stored 2 is the old default
        bb_sql("DELETE FROM halfway_parameter WHERE k='reg_window' AND v=2")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('mig_v81', 1)")
    if not bb_sql("SELECT 1 FROM halfway_parameter WHERE k='mig_v92'", fetch=True):          # V9.2: new defaults (window 3 = 3 up + 3 down, hold 6, early 5); an old stored default is dropped once
        for k_, v_ in (("reg_window", 5), ("reg_window", 2), ("reg_hold_max", 5), ("reg_early_max", 3), ("reg_early_future", 5)):
            bb_sql("DELETE FROM halfway_parameter WHERE k=? AND v=?", (k_, v_))
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('mig_v92', 1)")
    if not bb_sql("SELECT 1 FROM halfway_parameter WHERE k='mig_v101'", fetch=True):
        # V10.1: mandatory 7-min break and stronger headway regulation defaults. Remove only known old defaults.
        for k_, vals in (("min_layover_min", (2,)), ("reg_window", (3,)), ("reg_min_side", (3,)), ("reg_hold_max", (6,)), ("reg_early_max", (5,)), ("reg_early_future", (6,))):
            for v_ in vals:
                bb_sql("DELETE FROM halfway_parameter WHERE k=? AND v=?", (k_, v_))
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('mig_v101', 1)")
    if not bb_sql("SELECT 1 FROM halfway_parameter WHERE k='mig_v121_welfare'", fetch=True):
        # V12.1 welfare: hard +8 min adjustment cap, 7-min layover, minimum 3+3 rolling regulation horizon.
        # Remove legacy stored defaults that would otherwise override the safer engine defaults.
        bb_sql("DELETE FROM halfway_parameter WHERE k='reg_hold_max' AND v>8")
        bb_sql("DELETE FROM halfway_parameter WHERE k='reg_min_side'")
        bb_sql("DELETE FROM halfway_parameter WHERE k='reg_window'")
        bb_sql("DELETE FROM halfway_parameter WHERE k='reg_early_future' AND v>8")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('reg_hold_max', 8)")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('reg_min_side', 3)")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('reg_window', 3)")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('reg_early_future', 8)")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('min_layover_min', 7)")
        bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES ('mig_v121_welfare', 1)")
    for r in bb_sql("SELECT k, v FROM halfway_parameter", fetch=True):
        if r["k"] in HO["params"]:
            HO["params"][r["k"]] = int(r["v"]) if r["k"] in HO_INT else r["v"]
    ho_load_points()


def ho_load_points():
    HO["points"] = bb_sql("SELECT * FROM halfway_point_master ORDER BY service, direction, COALESCE(seq, 9999), stop_code", fetch=True)


def ho_validate_params(inp):
    out, errs = {}, []
    for k, v in (inp or {}).items():
        if k not in HO_RANGES:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            errs.append(f"{k}: not a number")
            continue
        lo, hi = HO_RANGES[k]
        if not (lo <= f <= hi):
            errs.append(f"{k}: must be between {lo} and {hi}")
        else:
            out[k] = int(round(f)) if k in HO_INT else f
    merged = {**HO["params"], **out}
    if sum(merged[w] for w in ("w_regularity", "w_maxgap", "w_recovery", "w_holding", "w_mileage", "w_bunching")) <= 0:
        errs.append("at least one score weight must be above 0")
    return out, errs


def _hnorm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


HO_COLS = {"service": "service", "direction": "direction", "dir": "direction", "stopcode": "stop_code", "approvedhalfwaystop": "stop_code", "halfwaystop": "stop_code", "stop": "stop_code",
           "sequence": "seq", "stopsequence": "seq", "seq": "seq", "enabled": "enabled", "halfwayenabled": "enabled"}
HO_DEFAULT_ORDER = ["service", "direction", "stop_code", "seq", "enabled"]


def parse_points(text):
    """CSV/TSV of approved halfway stops -> (rows, errors). Columns: service, direction, stop_code, sequence, enabled. A header row is optional; extra columns
    (e.g. older per-point limits) are ignored. stop_code or sequence must be given."""
    rows, errs = [], []
    lines = [l for l in (text or "").replace("\r", "").split("\n") if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return [], ["The file is empty."]
    delim = "\t" if "\t" in lines[0] else (";" if lines[0].count(";") > lines[0].count(",") else ",")
    order = HO_DEFAULT_ORDER
    first = [c.strip().strip('"') for c in lines[0].split(delim)]
    if re.search(r"[A-Za-z]{3,}", lines[0]) and not re.fullmatch(r"\d+", first[0]):
        mapped = [HO_COLS.get(_hnorm(c)) for c in first]
        if "service" in mapped and ("stop_code" in mapped or "seq" in mapped):
            order = mapped
        lines = lines[1:]
    for i, l in enumerate(lines, 1):
        c = [x.strip().strip('"') for x in l.split(delim)]
        rec = {k: (c[j] if j < len(c) else "") for j, k in enumerate(order) if k}
        svc, dr = rec.get("service", "").upper(), rec.get("direction", "").lower().replace("dir", "").strip()
        code, seq, en = rec.get("stop_code", "").strip(), rec.get("seq", "").strip(), rec.get("enabled", "").strip().lower()
        bad = None
        if not re.fullmatch(r"[0-9A-Z]{1,6}", svc) or dr not in ("1", "2"):
            bad = "service / direction"
        elif not code and not seq:
            bad = "needs a stop_code or a sequence"
        elif code and not re.fullmatch(r"\d{5}", code):
            bad = "stop_code must be 5 digits"
        elif seq and not re.fullmatch(r"\d{1,3}", seq):
            bad = "sequence must be a number"
        elif en not in ("", "yes", "y", "true", "1", "no", "n", "false", "0"):
            bad = "enabled must be yes/no"
        if bad:
            errs.append(f"line {i}: {bad} ({l[:50]})")
            continue
        rows.append({"service": svc, "direction": int(dr), "stop_code": code or None, "seq": int(seq) if seq else None, "enabled": 0 if en in ("no", "n", "false", "0") else 1})
    return rows, errs[:10]


def ho_save_points(rows, replace=True):
    if replace:
        bb_sql("DELETE FROM halfway_point_master")
    for r in rows:
        bb_sql("INSERT INTO halfway_point_master(service,direction,stop_code,seq,enabled) VALUES (?,?,?,?,?)", (r["service"], r["direction"], r["stop_code"], r["seq"], r["enabled"]))
    ho_load_points()


# ---- geometry helpers
def ho_simplify(line, n=200):
    if len(line) <= n:
        return [[round(p[0], 5), round(p[1], 5)] for p in line]
    step = (len(line) - 1) / (n - 1)
    return [[round(line[round(i * step)][0], 5), round(line[round(i * step)][1], 5)] for i in range(n)]


def ho_point_at(line, cum, s):
    s = max(0.0, min(cum[-1], s))
    i = min(len(line) - 2, max(0, bisect.bisect_right(cum, s) - 1))
    seg = cum[i + 1] - cum[i]
    f = 0.0 if seg <= 0 else (s - cum[i]) / seg
    return (line[i][0] + (line[i + 1][0] - line[i][0]) * f, line[i][1] + (line[i + 1][1] - line[i][1]) * f)


def ho_cut(line, cum, a, b):
    """Part of a route line between km a and km b."""
    if len(line) < 2:
        return []
    if b < a:
        a, b = b, a
    return [ho_point_at(line, cum, a)] + [line[i] for i in range(len(line)) if a < cum[i] < b] + [ho_point_at(line, cum, b)]


def ho_speed_label(kmh):
    return "Smooth" if kmh >= 35 else ("Moderate" if kmh >= 22 else "Congested")


async def ho_incidents_near(line, km=0.3):
    if not line:
        return []
    try:
        inc = await api_incidents("", 1)
    except Exception:
        return []
    return [{"type": x["type"], "message": x["message"][:120]} for x in inc.get("incidents", []) if min_dist_km(x["lat"], x["lon"], line) <= km][:5]


# ---- route facts for one service direction (no live buses are used)
async def ho_route(svc, d):
    st, fq = await static(), await freq_table()
    route = await bb_route(svc, d)
    if route.get("error"):
        return {"error": route["error"]}
    stops = route_stops(st, svc, d)
    line = cached_line(svc, d, stops)
    gk = geom_key(svc, d, stops)
    prep = PREP.get(gk)
    if prep is None:
        prep = PREP[gk] = await asyncio.to_thread(headway.prepare, line, stops)
    tm = headway.TimeModel(route.get("runs", []), prep["stop_s"], headway.CFG) if route["traffic"]["ok"] else None
    if tm is not None and not tm.ok:
        tm = None
    P = {**halfway.PARAMS, **HO["params"]}
    ss = prep["stop_s"]
    tau = [(tm.t(ss[0], s) if tm is not None else max(0.0, s - ss[0]) / P["fallback_kmh"] * 60.0) for s in ss]      # running minutes from the first stop
    now = now_sgt()
    H, H_src = bb_resolve_hw(svc, d, now, fq)
    return {"stops": stops, "line": line, "prep": prep, "tm": tm, "tau": tau, "H": H, "H_src": H_src, "now": now, "traffic_ok": tm is not None}


def ho_candidates(svc, d, stops, extra="", scope="auto", prep=None, P=None):
    """Halfway stops to test. scope: "approved" = the approved list only; "all" = the AI searches every eligible stop (approved ones are flagged); "auto" = the approved list
    if the service has one, otherwise every eligible stop. Returns (candidates, unresolved, info)."""
    out, unresolved, seen, info = [], [], set(), {"scope": scope, "approved": 0, "scanned": False, "skipped_end": 0, "skipped_km": 0}

    def add(j, approved, auto=False):
        if j in seen:
            return
        seen.add(j)
        s = stops[j]
        out.append({"j": j, "code": s["code"], "name": s["name"], "seq": s["seq"], "lat": s["lat"], "lon": s["lon"], "approved": approved, "auto": auto})
    appr = set()
    for row in HO["points"]:
        if row["service"] != svc or row["direction"] != d or not row["enabled"]:
            continue
        j = None
        if row["stop_code"]:
            j = next((i for i, s in enumerate(stops) if s["code"] == row["stop_code"] and i > 0), None)
        if j is None and row["seq"]:
            j = next((i for i, s in enumerate(stops) if s["seq"] == row["seq"]), None)
        if j is None:
            unresolved.append(row["stop_code"] or f"seq {row['seq']}")
        else:
            appr.add(j)
    info["approved"] = len(appr)
    if scope == "auto":
        scope = "all" if not appr else "approved"
    info["scope_used"] = scope
    if scope == "all" and prep is not None and P is not None:
        info["scanned"] = True
        ss, km, n = prep["stop_s"], prep["km"], len(stops)
        elig = []
        for j in range(1, n - 1):
            if km and 100.0 * (km - ss[j]) / km < P["min_remaining_pct"] - 1e-9:
                info["skipped_end"] += 1
            elif ss[j] > P["max_mileage_km"] + 1e-9:
                info["skipped_km"] += 1
            else:
                elig.append(j)
        if len(elig) > HO_MAX_SCAN:
            stride = math.ceil(len(elig) / HO_MAX_SCAN)
            elig = elig[::stride]
        for j in elig:
            add(j, j in appr, auto=j not in appr)
        for j in sorted(appr):
            if j not in seen:
                add(j, True)
    else:
        for j in sorted(appr):
            add(j, True)
    if extra:
        j = next((i for i, s in enumerate(stops) if s["code"] == extra and i > 0), None)
        if j is None:
            unresolved.append(extra)
        else:
            add(j, False)
    out.sort(key=lambda c: c["j"])
    return (out if info["scanned"] else out[:HO_MAX_CANDIDATES]), unresolved, info


def ho_hhmm(m):
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def ho_parse_hhmm(s):
    mm = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s or "")
    if not mm or int(mm.group(1)) > 23 or int(mm.group(2)) > 59:
        return None
    return int(mm.group(1)) * 60 + int(mm.group(2))


@app.get("/api/halfway/setup")
async def api_ho_setup(service: str = "", direction: int = 1):
    svc = service.strip().upper()
    if not svc:
        return {"error": "Enter a service number."}
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"error": g["error"]}
    P = {**halfway.PARAMS, **HO["params"]}
    now = g["now"]
    ref = ((now.hour * 60 + now.minute) // 5 + 1) * 5                    # next 5-minute mark: the first simulated departure
    cands, unresolved, cinfo = ho_candidates(svc, direction, g["stops"], "", "auto", g["prep"], P)
    ret = []
    try:                                                                         # the return direction's stops: AITP filter for the DOWN trips
        ret = [[s_["code"], s_["name"]] for s_ in route_stops(await static(), svc, 2 if direction == 1 else 1)]
    except Exception:
        ret = []
    return {"ret_stops": ret, "ret_dir": 2 if direction == 1 else 1, "service": svc, "direction": direction, "sched_hw": g["H"], "hw_src": g["H_src"], "ref": ho_hhmm(ref), "params": P, "n_stops": len(g["stops"]),
            "first": g["stops"][0]["name"], "last": g["stops"][-1]["name"], "route_km": round(g["prep"]["km"], 1), "run_min": round(g["tau"][-1], 1), "traffic_ok": g["traffic_ok"],
            "stops": [[s["code"], s["name"]] for s in g["stops"]], "points": [{"code": c["code"], "name": c["name"], "seq": c["seq"]} for c in cands if c.get("approved")], "n_approved": cinfo["approved"], "n_scan": len(cands) if cinfo["scanned"] else 0, "unresolved": unresolved,
            "updated": now.isoformat(timespec="seconds")}


@app.get("/api/halfway/simulate")
async def api_ho_simulate(service: str = "", direction: int = 1, ref: str = "", hw: str = "", layover: str = "", delay: str = "", late: str = "", disrupted: str = "", stop: str = "", save: str = "",
                          pref: str = "balanced", reg: str = "1", scope: str = "auto", veh: str = "own", ready: str = "", pick: str = "", preview: str = "", adj2: str = ""):
    svc = service.strip().upper()
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"ok": False, "error": g["error"]}
    P = {**halfway.PARAMS, **HO["params"]}
    now = g["now"]

    def num_(v, lo, hi, name, default):
        if not str(v).strip():
            return default, None
        try:
            f = float(v)
        except ValueError:
            return None, f"{name} must be a number."
        if not (lo <= f <= hi):
            return None, f"{name} must be between {lo:g} and {hi:g}."
        return f, None
    t0 = ho_parse_hhmm(ref) if ref.strip() else ((now.hour * 60 + now.minute) // 5 + 1) * 5
    if t0 is None:
        return {"ok": False, "error": "First scheduled departure must be a time like 08:10."}
    H, e1 = num_(hw, 1, 120, "Scheduled headway", g["H"])
    lay, e2 = num_(layover, 0, 60, "Scheduled layover", P["layover_min"])
    dly, e3 = num_(delay, -30, 60, "Replacement start delay", P["start_delay_min"])
    if e1 or e2 or e3:
        return {"ok": False, "error": e1 or e2 or e3}
    n = int(P["n_trips"])
    lates = []
    for i, x in enumerate([v for v in late.split(",")] if late.strip() else []):
        f, e = num_(x, -60, 300, f"Lateness of trip {i + 1}", 0.0)
        if e:
            return {"ok": False, "error": e}
        lates.append(f)
    lates = (lates + [0.0] * n)[:n]
    dis = None
    force_cont = disrupted.strip().lower() == "continue"           # the Recovery Optimiser kept every trip in full: show the adjust-and-continue view
    if force_cont:
        disrupted = ""
    auto = disrupted.strip().lower() == "auto"                # "auto": the AI decides which trips to disrupt (Run AI Optimisation), or to just adjust and continue service
    if disrupted.strip() and not auto:
        try:
            dis = sorted({int(x) for x in re.split(r"[,\s]+", disrupted.strip()) if x})          # one trip or several: "3" or "3,4,7"
        except ValueError:
            return {"ok": False, "error": "Disrupted trip must be a trip number (or a list like 3,4)."}
        dis = dis or None
        adj_ok = adj2.strip() in ("1", "true", "yes", "on") if adj2.strip() else bool(P.get("allow_adjacent_halfway", 0))
        if dis and not adj_ok:
            pair = next(((a_, b_) for a_, b_ in zip(dis, dis[1:]) if b_ - a_ == 1), None)
            if pair:
                return {"ok": False, "error": f"Trips {pair[0]} and {pair[1]} are back to back: two consecutive trips may not both be disrupted / start halfway "
                                              f"(tick \"Allow two back-to-back halfway trips\" to permit it)."}
    stops = g["stops"]
    scope = scope if scope in ("auto", "all", "approved") else "auto"
    veh = veh if veh in ("own", "standby") else "own"
    rdy = None
    if ready.strip():
        rdy = ho_parse_hhmm(ready)
        if rdy is None:
            return {"ok": False, "error": "Bus ready time must be a time like 08:54."}
    cands, unresolved, cinfo = ho_candidates(svc, direction, stops, stop.strip(), scope, g["prep"], {**P, **HO["params"]})
    if preview.strip():                                       # only the trip table (schedule, lateness, departures): no search, nothing stored
        cands, reg, save = [], "0", ""
    ctx = {"H": H, "t0": t0, "late": lates, "disrupted": dis, "tau": g["tau"], "stop_s": g["prep"]["stop_s"], "route_km": g["prep"]["km"], "stop_names": [s["name"] for s in stops],
           "stop_seq": [s["seq"] for s in stops], "params": {**HO["params"], "layover_min": lay, "start_delay_min": dly}, "bunch_min": BB["params"]["bunch_min"], "candidates": cands,
           "pref": pref, "regulate": reg.strip() not in ("0", "false", "no", "off"), "veh": veh, "ready": rdy,
           "search_candidates": (cands[::math.ceil(len(cands) / 10)] if len(cands) > 10 else cands),           # the AI's search compares its decisions on a coarser set of stops; the chosen one is then simulated on every stop
           "keep": [c for c in pick.split(",") if re.fullmatch(r"\d{5}", c.strip())][:6]}
    if force_cont:
        P_ = {**halfway.PARAMS, **ctx["params"]}
        tr0 = halfway.build_trips(P_, H, t0, lates, [])
        ctx["block"] = [t["n"] for t in tr0 if 2 <= t["n"] <= int(P_["n_trips"]) - 1 and t["act_arr"] + P_["min_layover_min"] - t["sch_dep"] >= P_["auto_late_min"] - 1e-6]
    res = halfway.decide(ctx) if auto else halfway.simulate(ctx)
    if not res["ok"]:
        return res
    cum, line, ss = g["prep"]["cum"], g["line"], g["prep"]["stop_s"]
    tm = g["tm"]

    def section(a_, b_):
        km = max(0.0, ss[b_] - ss[a_])
        mins = tm.t(ss[a_], ss[b_]) if tm is not None else km / P["fallback_kmh"] * 60.0
        kmh = km / (mins / 60.0) if mins > 0 else None
        return {"km": round(km, 2), "min": round(mins, 1), "kmh": round(kmh, 1) if kmh else None, "label": ho_speed_label(kmh) if kmh else None}
    dis_trip = res["trips"][res["disrupted"] - 1] if res.get("disrupted") else None
    for o in res["options"]:
        j = o.get("j")
        if j is None or not o.get("series"):
            continue
        o["lat"], o["lon"] = stops[j]["lat"], stops[j]["lon"]
        o["line_missing"] = ho_simplify(ho_cut(line, cum, 0.0, ss[j]), 150)               # the section this trip does not serve
        o["line_resumed"] = ho_simplify(ho_cut(line, cum, ss[j], cum[-1]), 150)            # where the replacement resumes service
        o["traffic"] = {"resumed": section(j, len(ss) - 1), "skipped": section(0, j), "incidents": await ho_incidents_near(ho_cut(line, cum, ss[j], cum[-1])),
                        "skipped_incidents": await ho_incidents_near(ho_cut(line, cum, 0.0, ss[j]))}
        own = o.get("own_arrive") if o.get("own_arrive") is not None else dis_trip["act_arr"] + HO["params"]["min_layover_min"] + g["tau"][j]       # when the halfway bus can be at the stop
        o["travel"] = {"first_to_stop": round(g["tau"][j], 1), "stop_to_end": round(g["tau"][-1] - g["tau"][j], 1), "vehicle_late": round(dis_trip["late"], 1),
                       "own_ready": own, "own_behind_start": round(own - o["start_time"], 1)}
    res.update(service=svc, direction=direction, ref=ho_hhmm(t0), hw_src=g["H_src"] if not hw.strip() else "entered by you", layover=lay, start_delay=dly, unresolved=unresolved,
               selected=stop.strip() or None, candidates=cinfo, scope=scope, ready_in=ready.strip() or None, traffic_ok=g["traffic_ok"], route={"km": round(g["prep"]["km"], 1), "run_min": round(g["tau"][-1], 1), "first": stops[0]["name"], "last": stops[-1]["name"]},
               map={"line": ho_simplify(line, 500), "first": [stops[0]["lat"], stops[0]["lon"]], "last": [stops[-1]["lat"], stops[-1]["lon"]],
                    "stops": [[round(s_["lat"], 5), round(s_["lon"], 5), s_["seq"], s_["name"], s_["code"]] for s_ in stops]}, updated=now.isoformat(timespec="seconds"))
    if save.strip() and (dis is not None or auto) and res.get("options"):
        res["run_id"] = ho_audit(res, lates)
    return res


# ----------------------------------------------------------------------------- V12.7 AI Recovery Scenario Optimiser (recovery.py)
import recovery
import offservice

OVERPASS = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter").rstrip("/")
RV_CACHE = {}          # recovery results keyed by their inputs (bus type only changes the route planner, not the optimisation)


def os_init():
    bb_sql("CREATE TABLE IF NOT EXISTS offservice_record(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, service TEXT, direction INTEGER, stop_code TEXT, "
           "bus_type TEXT, signature TEXT, roads TEXT, by_name TEXT, note TEXT, detail TEXT)")


def os_verified(svc, d, code, bus, sig):
    try:
        rows = bb_sql("SELECT * FROM offservice_record WHERE kind='route_verified' AND service=? AND direction=? AND stop_code=? AND bus_type=? AND signature=? ORDER BY ts DESC LIMIT 1",
                      (svc, d, code, bus, sig), fetch=True)
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    return {"by": r["by_name"], "date": datetime.fromtimestamp(r["ts"], SGT).strftime("%d %b %Y"), "note": r["note"]}


async def os_osrm(coords, alternatives=0, steps=True, bearings=None, continue_straight=None):
    """OSRM driving route through coords [(lat, lon), ...] with road names per step. Cached 10 min.
    bearings: optional [deg or None per coordinate] - the heading the vehicle must have there (+-45 deg).
    continue_straight: True forbids turning back at intermediate waypoints."""
    path = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in coords)
    extra = {}
    if bearings and any(b is not None for b in bearings):
        extra["bearings"] = ";".join("" if b is None else f"{int(round(b)) % 360},45" for b in bearings)
    if continue_straight is not None:
        extra["continue_straight"] = "true" if continue_straight else "false"

    async def factory():
        try:
            prm = {"overview": "full", "geometries": "geojson", "steps": "true" if steps else "false"}
            if alternatives:
                prm["alternatives"] = str(alternatives)
            prm.update(extra)
            r = await client().get(f"{OSRM}/route/v1/driving/{path}", params=prm, timeout=25)
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "Ok":
                return {"routes": [], "error": j.get("message") or j.get("code")}, 120, False
            return {"routes": offservice.parse_osrm(j), "error": None}, 600, True
        except Exception as e:
            return {"routes": [], "error": f"routing service unavailable ({type(e).__name__})"}, 60, False
    return await cached(f"osrm:{alternatives}:{path}" + (f":{sorted(extra.items())}" if extra else ""), factory)


async def os_table(origin, dests):
    """off-service minutes (car time) from the interchange to every candidate stop, one request. {index: minutes}"""
    if not dests:
        return {}, None
    pts = [origin] + dests
    path = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in pts)

    async def factory():
        try:
            r = await client().get(f"{OSRM}/table/v1/driving/{path}", params={"sources": "0", "annotations": "duration,distance"}, timeout=25)
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "Ok":
                return {"dur": None, "error": j.get("code")}, 120, False
            return {"dur": (j.get("durations") or [[]])[0], "dist": (j.get("distances") or [[]])[0], "error": None}, 900, True
        except Exception as e:
            return {"dur": None, "error": f"routing service unavailable ({type(e).__name__})"}, 60, False
    d = await cached(f"osrmtab:{path}", factory)
    if not d.get("dur"):
        return {}, d.get("error")
    dist = d.get("dist") or []
    out = {}
    for i in range(len(dests)):
        mins = d["dur"][i + 1] / 60.0 if d["dur"][i + 1] is not None else None
        km = dist[i + 1] / 1000.0 if len(dist) > i + 1 and dist[i + 1] is not None else None
        if mins is not None:
            out[i] = {"min": mins, "km": km}
    return out, None


async def os_matrix(origins, dests):
    """V14.3: off-service car minutes / km from each origin to each destination in one OSRM table request. {(i, j): {min, km}}"""
    if not origins or not dests:
        return {}, None
    pts = list(origins) + list(dests)
    path = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in pts)
    srcs = ";".join(str(i) for i in range(len(origins)))
    dsts = ";".join(str(len(origins) + i) for i in range(len(dests)))

    async def factory():
        try:
            r = await client().get(f"{OSRM}/table/v1/driving/{path}", params={"sources": srcs, "destinations": dsts, "annotations": "duration,distance"}, timeout=30)
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "Ok":
                return {"dur": None, "error": j.get("code")}, 120, False
            return {"dur": j.get("durations"), "dist": j.get("distances"), "error": None}, 900, True
        except Exception as e:
            return {"dur": None, "error": f"routing service unavailable ({type(e).__name__})"}, 60, False
    d = await cached(f"osrmmx:{path}|{srcs}", factory)
    if not d.get("dur"):
        return {}, d.get("error")
    out = {}
    for i, row in enumerate(d["dur"]):
        drow = (d.get("dist") or [[]] * len(d["dur"]))[i] or []
        for j, v in enumerate(row or []):
            if v is not None:
                out[(i, j)] = {"min": v / 60.0, "km": (drow[j] / 1000.0) if len(drow) > j and drow[j] is not None else None}
    return out, None


async def os_overpass(line):
    """ways along a route that carry restriction / structure tags (OpenStreetMap via Overpass). Cached 1 day."""
    pts = offservice.simplify(line, 120)
    coords = ",".join(f"{p[0]:.5f},{p[1]:.5f}" for p in pts)
    key = "ovp:" + hashlib.sha1(coords.encode()).hexdigest()[:20]
    sel = [
        '["highway"]["maxheight"]', '["highway"]["maxwidth"]', '["highway"]["maxweight"]', '["highway"]["hgv"]', '["highway"]["bus"]', '["highway"]["psv"]',
        '["highway"]["motor_vehicle"]', '["highway"]["access"]', '["highway"]["tunnel"]', '["highway"]["covered"]', '["bridge"]', '["man_made"="bridge"]',
    ]
    q = "[out:json][timeout:20];(" + "".join(f"way(around:12,{coords}){t};" for t in sel) + ");out tags center 400;"

    async def factory():
        try:
            r = await client().post(OVERPASS, data={"data": q}, timeout=25)
            r.raise_for_status()
            j = r.json()
            ways = [{"tags": e.get("tags") or {}, "lat": (e.get("center") or {}).get("lat"), "lon": (e.get("center") or {}).get("lon")} for e in j.get("elements", []) if e.get("type") == "way"]
            return {"ok": True, "ways": ways}, 86400, True
        except Exception as e:
            return {"ok": False, "ways": [], "error": f"{type(e).__name__}"}, 120, False
    return await cached(key, factory)


def os_overlap(line, service_line, m):
    pts = offservice.simplify(line, 60)
    sl = offservice.simplify(service_line, 400)
    if not pts or not sl:
        return 0.0
    return sum(1 for p in pts if offservice.dist_to_line_m(p, sl) <= m) / len(pts)


def os_positions(plan, pts_km, T, line, cum, n):
    """where each bus of the plan is (lat, lon) at time T on UP 1, from the recovery simulation's timing points."""
    out = []
    for b_, ts in (plan.get("up1_times") or {}).items():
        b = int(b_)
        if not 1 <= b <= n:
            continue
        pk = [(km, t) for km, t in zip(pts_km, ts) if t is not None]
        if len(pk) < 2 or not (pk[0][1] <= T <= pk[-1][1]):
            continue
        for (k0, t0), (k1, t1) in zip(pk, pk[1:]):
            if t0 <= T <= t1:
                km = k0 + (k1 - k0) * (0.0 if t1 <= t0 else (T - t0) / (t1 - t0))
                lat, lon = ho_point_at(line, cum, km)
                out.append({"n": b, "lat": round(lat, 5), "lon": round(lon, 5), "km": round(km, 2)})
                break
    return out


async def os_plan(g, res, svc, d, bus, dims):
    """Halfway Deployment & Off-Service Route Planner for the best halfway plan of a recovery result."""
    P = offservice.PARAMS
    plans = res.get("plans") or {}
    hp = plans.get("halfway")
    if not hp:
        return {"ok": False, "reason": "No feasible halfway deployment for this situation, so there is no off-service route to plan."}
    row = next(r for r in hp["rows"] if r["type"] == "halfway")
    stops, line, cum, ss = g["stops"], g["line"], g["prep"]["cum"], g["prep"]["stop_s"]
    j, k = row["j"], row["n"]
    origin, dest = (stops[0]["lat"], stops[0]["lon"]), (stops[j]["lat"], stops[j]["lon"])
    svc_line = ho_cut(line, cum, 0.0, ss[j])
    # candidate road routes: OSRM alternatives + the service's own road path (through its stops, not stopping)
    alt = await os_osrm([origin, dest], alternatives=3)
    routes = [dict(r, label=f"Road route {i + 1}", kind="osrm") for i, r in enumerate(alt["routes"])]
    via = [origin] + [(stops[i]["lat"], stops[i]["lon"]) for i in range(1, j, max(1, math.ceil(j / 20)))] + [dest]
    own = await os_osrm(via, alternatives=0) if j >= 2 else {"routes": []}
    if own["routes"]:
        r0 = own["routes"][0]
        if offservice.line_km(r0["line"]) <= 1.6 * max(0.3, ss[j]) + 0.5:
            routes.append(dict(r0, label="Along the service route (no stops)", kind="service"))
    if not routes:
        return {"ok": False, "reason": f"No road route could be calculated ({alt.get('error') or 'routing service unavailable'}). The halfway timing uses an estimate only.",
                "routing_error": alt.get("error")}
    # de-duplicate near-identical routes
    uniq = []
    for r in routes:
        if any(abs(r["km"] - u["km"]) < 0.15 and abs(r["osrm_min"] - u["osrm_min"]) < 0.6 for u in uniq):
            continue
        uniq.append(r)
    routes = uniq[:4]
    bands = await bands_state()
    idx = bands.get("idx") if isinstance(bands, dict) else None
    now = now_sgt()
    try:
        rw, _ = await tr_roadworks(now.timestamp())
    except Exception:
        rw = []
    try:
        inc = (await api_incidents("", 1)).get("incidents", [])
    except Exception:
        inc = []
    osms = await asyncio.gather(*[os_overpass(r["line"]) for r in routes])
    for r, osm in zip(routes, osms):
        r["groups"] = offservice.group_roads(r["steps"])
        runs, tinfo = color_route(r["line"], idx) if idx else ([], {"km": {}, "driveMin": None, "known": False})
        known = sum(v for kk, v in (tinfo.get("km") or {}).items() if kk != "none")
        use_band = bool(idx) and tinfo.get("known") and known >= P["band_min_share"] * max(r["km"], 0.01)
        r["time_min"] = tinfo["driveMin"] if use_band else r["osrm_min"] * P["bus_time_factor"]
        r["time_src"] = "live speed bands" if use_band else f"routing time x {P['bus_time_factor']:g} (no live speed data)"
        r["traffic"] = {"km": {kk: round(v, 2) for kk, v in (tinfo.get("km") or {}).items()}, "runs": [{"b": x["b"], "road": x["road"], "pts": offservice.simplify(x["pts"], 40)} for x in runs][:120]}
        r["overlap"] = os_overlap(r["line"], line, P["service_overlap_m"])
        r["signature"] = offservice.signature(stops[j]["code"], r["groups"])
        r["verified"] = os_verified(svc, d, stops[j]["code"], bus, r["signature"])
        r["findings"] = offservice.findings(r, osm, bus, dims, rw, inc, P)
        r["status"], r["status_text"] = offservice.suitability(r["findings"], bus, r["verified"])
        r["osm_ok"] = bool(osm and osm.get("ok"))
    leave = row["leave"]
    ctx = {"leave": leave, "planned_start": row["start"]}
    P_ = dict(P, prep_min=float(res.get("prep_min") or P["prep_min"]))
    opts, rec, fastest, shortest = offservice.build_options(routes, ctx, P_)
    n = len(res.get("trips") or [])
    none_at = (plans.get("none") or {}).get("at_j") or []
    reg_at = (plans.get("adjust") or {}).get("at_j") or []
    hw_at = hp.get("at_j") or []
    focus = res.get("focus") or list(range(1, n + 1))
    for o in opts:
        o["benefit"] = offservice.headway_benefit(none_at, reg_at, hw_at, k, o["insert"], n, focus)
    o = opts[rec]
    ben = o["benefit"]
    end_time = o["insert"] + (g["tau"][-1] - g["tau"][j])
    for x in opts:
        x["timeline"] = offservice.timeline(x, leave, P_["prep_min"], x["insert"], x["insert"] + (g["tau"][-1] - g["tau"][j]), stops[0]["name"], stops[j]["name"], stops[-1]["name"])
    full_key = res.get("full_choice") or "adjust"
    bc_full = ((plans.get(full_key) or plans.get("adjust") or {}).get("mc") or {}).get("bc_p85")
    bc_hw = (hp.get("mc") or {}).get("bc_p85")
    buses = os_positions(hp, res.get("pts_km") or [], o["insert"], line, cum, n)
    out_opts = []
    for x in opts:
        out_opts.append({"idx": x["idx"], "label": x["label"], "kind": x["kind"], "tags": x["tags"], "km": round(x["km"], 2), "time_min": round(x["time_min"], 1), "time_src": x["time_src"],
                         "arrive": round(x["arrive"], 1), "arrive_clock": offservice.hm(x["arrive"]), "insert": round(x["insert"], 1), "insert_clock": offservice.hm(x["insert"]),
                         "status": x["status"], "status_label": offservice.STATUS_TXT[x["status"]], "status_text": x["status_text"], "findings": x["findings"], "osm_ok": x["osm_ok"],
                         "verified": x["verified"], "signature": x["signature"], "overlap": round(x["overlap"], 2), "n_turns": x["n_turns"], "n_sharp": x["n_sharp"],
                         "congested_km": round(x["congested_km"], 2), "traffic": x["traffic"], "line": offservice.simplify(x["line"], 250),
                         "groups": [{"road": gg["road"], "km": round(gg["km"], 2), "min": round(gg["min"] * (x["time_min"] / (sum(q["min"] for q in x["groups"]) or 1.0)), 1),
                                     "line": offservice.simplify(gg["line"], 60)} for gg in x["groups"]],
                         "roads": [gg["road"] for gg in x["groups"]], "timeline": x["timeline"],
                         "benefit": {kk: (round(v, 1) if isinstance(v, float) else v) for kk, v in x["benefit"].items()}})
    return {"ok": True, "trip": k, "stop": {"j": j, "code": stops[j]["code"], "name": stops[j]["name"], "lat": dest[0], "lon": dest[1], "km_from_start": round(ss[j], 2)},
            "origin": {"name": stops[0]["name"], "lat": origin[0], "lon": origin[1]}, "last": {"name": stops[-1]["name"], "lat": stops[-1]["lat"], "lon": stops[-1]["lon"]},
            "bus": bus, "bus_label": offservice.BUS_TYPES[bus], "dims": dims, "leave": leave, "leave_clock": offservice.hm(leave), "act_arr_clock": offservice.hm(row["act_arr"]),
            "nat_dep_clock": offservice.hm(row["nat_dep"]), "slot_clock": row["slot_clock"], "prep_min": P_["prep_min"], "options": out_opts, "recommended": rec, "fastest": fastest, "shortest": shortest,
            "km_lost": row["km_lost"], "km_operated": round(g["prep"]["km"] - row["km_lost"], 2), "route_km": round(g["prep"]["km"], 2), "end_clock": offservice.hm(end_time),
            "bc_full_p85": bc_full, "bc_halfway_p85": bc_hw, "recommended_by_ai": res.get("decision") == "halfway",
            "why_route": offservice.why_route(opts, rec, fastest, shortest, ben, stops[j]["name"]),
            "why_works": offservice.why_works(ben, k, stops[0]["name"], stops[j]["name"], row["km_lost"], float(res.get("layover_min") or 10.0)),
            "buses": buses, "service_line": ho_simplify(line, 500), "recovered_line": ho_simplify(ho_cut(line, cum, ss[j], cum[-1]), 250),
            "skipped_line": ho_simplify(svc_line, 200), "roadworks": [x for x in rw if x.get("lat") is not None and any(offservice.dist_to_line_m((x["lat"], x["lon"]), r["line"]) <= 300 for r in routes)][:20],
            "incidents": [x for x in inc if any(offservice.dist_to_line_m((x["lat"], x["lon"]), r["line"]) <= 300 for r in routes)][:20],
            "sources": {"routing": OSRM, "restrictions": "OpenStreetMap (Overpass) - community data, not an authoritative clearance register", "traffic": "LTA speed bands, incidents, road works"}}


# ----------------------------------------------------------------------------- V13.3 Running Time Analytics (/insight)
import insight
import engineering_rt
import gzip
import base64

IN_CACHE = {}          # dataset id -> (trips, info) parsed on demand


import rtdata

RT = {"trk": {}, "saved": 0, "last": None, "src": {}}   # live running-time collection, built from the bunching collector's bus tracks
RT_POLL_SEC = float(os.getenv("RT_POLL_SEC", "60"))       # polling interval when nobody has a page open (always-on services only)
RT_MAX_SERVICES = int(os.getenv("RT_MAX_SERVICES", "8"))   # services measured round the clock (each = 2 directions x ~15 cached Bus Arrival calls)
DATAGOV_KEY = (os.getenv("DATAGOV_API_KEY") or os.getenv("DATA_GOV_SG_KEY") or "").strip()    # optional: higher data.gov.sg rate limits
RT_MIN_COVER = 0.82                                  # a trip must be observed over at least this share of the route to be stored
RT_KEEP_DAYS = float(os.getenv("RT_KEEP_DAYS", "180"))          # six months of measured trips by default


def rt_init():
    bb_sql("CREATE TABLE IF NOT EXISTS rt_trip(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, direction INTEGER, day_type TEXT, date TEXT, start_ts REAL, end_ts REAL, "
           "rt_min REAL, cover REAL, n_obs INTEGER, marks TEXT)")
    bb_sql("CREATE INDEX IF NOT EXISTS rt_trip_i ON rt_trip(service, direction, start_ts)")
    bb_sql("CREATE TABLE IF NOT EXISTS rt_sched(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, direction INTEGER, day_type TEXT, t_from TEXT, t_to TEXT, rt_min REAL, by_name TEXT, ts REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS rt_watch(service TEXT PRIMARY KEY, ts REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS rt_cond(trip_id INTEGER PRIMARY KEY, traffic_speed REAL, slow_share REAL, incident INTEGER, roadworks INTEGER, rain_mm REAL, demand REAL, "
           "rain_done INTEGER DEFAULT 0, demand_done INTEGER DEFAULT 0)")
    bb_sql("CREATE TABLE IF NOT EXISTS weather_day(date TEXT PRIMARY KEY, ts REAL, stations TEXT, readings TEXT)")
    bb_sql("CREATE TABLE IF NOT EXISTS holiday(date TEXT PRIMARY KEY, name TEXT)")
    bb_sql("CREATE TABLE IF NOT EXISTS school_holiday(id INTEGER PRIMARY KEY AUTOINCREMENT, d_from TEXT, d_to TEXT, label TEXT)")
    bb_sql("CREATE TABLE IF NOT EXISTS pv_stop(month TEXT, day_type TEXT, hour INTEGER, stop TEXT, tap_in INTEGER, tap_out INTEGER, PRIMARY KEY(month, day_type, hour, stop))")
    if not bb_sql("SELECT COUNT(*) n FROM school_holiday", (), fetch=True)[0]["n"]:
        for a, b, l in rtdata.SCHOOL_HOLIDAYS_SEED:
            bb_sql("INSERT INTO school_holiday(d_from, d_to, label) VALUES (?,?,?)", (a, b, l))


def rt_watch_list():
    try:
        return [r["service"] for r in bb_sql("SELECT service FROM rt_watch ORDER BY ts", (), fetch=True)][:RT_MAX_SERVICES]
    except Exception:
        return []


def rt_track(key, svc, d, ts, buses, stop_s, route_km):
    """Follow every tracked bus along the route; when one completes the route, store the trip with its crossing time at each stop.
    Running time here is MEASURED from live positions (LTA DataMall), not from a timetable feed."""
    done_ids = []
    if not buses or route_km <= 0.5:
        return done_ids
    T = RT["trk"].setdefault(key, {})
    seen = set()
    for b in buses:
        bid = b.get("id")
        if not bid:
            continue
        seen.add(bid)
        t = T.setdefault(bid, {"pts": [], "svc": svc, "dir": d})
        if t["pts"] and ts - t["pts"][-1][0] > 420:                      # a long gap in polling: the track cannot be trusted any more
            t["pts"] = []
        if not t["pts"] or b["s_km"] >= t["pts"][-1][1] - 0.05:
            t["pts"].append((ts, float(b["s_km"])))
    for bid in list(T):
        t = T[bid]
        done = bid not in seen
        pts = t["pts"]
        if not pts:
            if done:
                T.pop(bid, None)
            continue
        finished = pts[-1][1] >= route_km - 0.35
        if not (done or finished):
            continue
        tid = rt_store(svc, d, pts, stop_s, route_km)
        if tid:
            done_ids.append(tid)
        T.pop(bid, None)
    return done_ids


def rt_store(svc, d, pts, stop_s, route_km):
    if len(pts) < 4:
        return None
    start_km, end_km = pts[0][1], pts[-1][1]
    cover = (end_km - start_km) / route_km
    if cover < RT_MIN_COVER or start_km > 0.25 * route_km:
        return None                                                       # only trips seen from near the start of the route
    dur = (pts[-1][0] - pts[0][0]) / 60.0
    if not (3.0 <= dur <= 300.0):
        return None
    def at(km):
        """time the bus passed a point: interpolated between polls, and extrapolated up to 0.45 km beyond the first / last
        observation (the last poll is usually a few hundred metres short of the terminal)."""
        if km < pts[0][1] - 0.45 or km > pts[-1][1] + 0.45:
            return None
        if km <= pts[0][1] or km >= pts[-1][1]:
            end = 0 if km <= pts[0][1] else -1
            (t0, k0), (t1, k1) = (pts[0], pts[1]) if end == 0 else (pts[-2], pts[-1])
            if k1 <= k0:
                return pts[end][0]
            return pts[end][0] + (km - pts[end][1]) * (t1 - t0) / (k1 - k0)
        for (t0, k0), (t1, k1) in zip(pts, pts[1:]):
            if k0 <= km <= k1:
                return t0 + (t1 - t0) * (0.0 if k1 <= k0 else (km - k0) / (k1 - k0))
        return None

    marks = []
    for i, km in enumerate(stop_s):
        t = at(km)
        marks.append(None if t is None else int(round(t - pts[0][0])))
    full = at(0.0) is not None and at(route_km) is not None
    rt_min = ((at(route_km) - at(0.0)) / 60.0) if full else dur / max(cover, 0.01)   # scaled when the ends were not observed
    now = datetime.fromtimestamp(pts[0][0], SGT)
    bb_sql("INSERT INTO rt_trip(service,direction,day_type,date,start_ts,end_ts,rt_min,cover,n_obs,marks) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (svc, d, insight.day_type(now.strftime("%Y-%m-%d")), now.strftime("%Y-%m-%d"), pts[0][0], pts[-1][0], round(rt_min, 2), round(cover, 3), len(pts), json.dumps(marks)))
    RT["saved"] += 1
    RT["last"] = time.time()
    if RT["saved"] % 50 == 0 and RT_KEEP_DAYS > 0:
        cut = time.time() - RT_KEEP_DAYS * 86400
        bb_sql("DELETE FROM rt_cond WHERE trip_id IN (SELECT id FROM rt_trip WHERE start_ts < ?)", (cut,))
        bb_sql("DELETE FROM rt_trip WHERE start_ts < ?", (cut,))
    r = bb_sql("SELECT id FROM rt_trip ORDER BY id DESC LIMIT 1", (), fetch=True)
    return r[0]["id"] if r else None


async def rt_enrich_live(ids, line):
    """Conditions on the route at the time each trip finished (live-only feeds, so they must be captured now):
    speed-band traffic along the route, LTA incidents and road works within 60 m of it."""
    if not ids:
        return
    try:
        bands = await bands_state()
        idx = bands.get("idx") if isinstance(bands, dict) else None
        speed, slow = None, None
        if idx:
            runs, tinfo = color_route(line, idx)
            km = tinfo.get("km") or {}
            known = sum(v for k, v in km.items() if k != "none")
            if tinfo.get("driveMin") and known > 0.3:
                speed = round(known / (tinfo["driveMin"] / 60.0), 1)
                slow = round(km.get("slow", 0.0) / known, 3)
        try:
            inc = (await api_incidents("", 1)).get("incidents", [])
        except Exception:
            inc = []
        try:
            rw, _ = await tr_roadworks(time.time())
        except Exception:
            rw = []
        sl = offservice.simplify(line, 200)
        n_inc = sum(1 for x in inc if x.get("lat") is not None and offservice.dist_to_line_m((x["lat"], x["lon"]), sl) <= 60)
        n_rw = sum(1 for x in rw if x.get("lat") is not None and offservice.dist_to_line_m((x["lat"], x["lon"]), sl) <= 60)
        for tid in ids:
            bb_sql("INSERT OR REPLACE INTO rt_cond(trip_id, traffic_speed, slow_share, incident, roadworks, rain_mm, demand, rain_done, demand_done) "
                   "VALUES (?,?,?,?,?, NULL, NULL, 0, 0)", (tid, speed, slow, 1 if n_inc else 0, 1 if n_rw else 0))
        RT["src"]["live_conditions"] = {"ts": time.time(), "ok": True, "detail": f"speed bands {'on' if idx else 'unavailable'}, {len(inc)} incidents, {len(rw)} road works island-wide"}
    except Exception as e:
        RT["src"]["live_conditions"] = {"ts": time.time(), "ok": False, "detail": type(e).__name__}


# ---------------------------------------------------------------- scheduled open-data jobs (rain backfill, holidays, passenger volume)
async def dg_get(url, params):
    h = {"accept": "application/json"}
    if DATAGOV_KEY:
        h["x-api-key"] = DATAGOV_KEY
    r = await client().get(url, params=params, headers=h, timeout=30)
    r.raise_for_status()
    return r.json()


async def rt_holidays():
    got = {}
    for ds in rtdata.HOLIDAY_DATASETS:
        try:
            got.update(rtdata.parse_holidays(await dg_get(rtdata.HOLIDAY_URL, {"resource_id": ds, "limit": 500})))
        except Exception:
            continue
    for d, n in got.items():
        bb_sql("INSERT OR REPLACE INTO holiday(date, name) VALUES (?,?)", (d, str(n)[:80]))
    RT["src"]["holidays"] = {"ts": time.time(), "ok": bool(got), "detail": f"{len(got)} public holidays from data.gov.sg (MOM)" if got else "data.gov.sg not reachable"}


async def rt_weather_day(date_s):
    """all 5-minute rainfall readings for one day (cached; today's refreshed every 30 min)."""
    row = bb_sql("SELECT ts, stations, readings FROM weather_day WHERE date=?", (date_s,), fetch=True)
    today = now_sgt().strftime("%Y-%m-%d")
    if row and (date_s != today or time.time() - row[0]["ts"] < 1800):
        return json.loads(row[0]["stations"]), json.loads(row[0]["readings"])
    stations, readings, token, pages = {}, [], None, 0
    while pages < 60:
        prm = {"date": date_s}
        if token:
            prm["paginationToken"] = token
        st, rd, token = rtdata.parse_rain_page(await dg_get(rtdata.RAIN_URL, prm))
        stations.update(st)
        readings.extend(rd)
        pages += 1
        if not token:
            break
    stations = {k: list(v) for k, v in stations.items()}
    bb_sql("INSERT OR REPLACE INTO weather_day(date, ts, stations, readings) VALUES (?,?,?,?)", (date_s, time.time(), json.dumps(stations), json.dumps(readings)))
    return stations, readings


async def rt_fill_rain(limit_days=10):
    rows = bb_sql("SELECT t.id, t.service, t.direction, t.date, t.start_ts, t.end_ts FROM rt_trip t JOIN rt_cond c ON c.trip_id=t.id "
                  "WHERE c.rain_done=0 AND t.end_ts < ? ORDER BY t.date DESC", (time.time() - 900,), fetch=True)
    if not rows:
        RT["src"].setdefault("rain", {"ts": time.time(), "ok": True, "detail": "nothing waiting"})
        return
    st = await static()
    by_date, done, lines = {}, 0, {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    for date_s in sorted(by_date, reverse=True)[:limit_days]:
        try:
            stations, readings = await rt_weather_day(date_s)
        except Exception as e:
            RT["src"]["rain"] = {"ts": time.time(), "ok": False, "detail": f"data.gov.sg rainfall: {type(e).__name__}"}
            return
        stations = {k: tuple(v) for k, v in stations.items()}
        for r in by_date[date_s]:
            k = (r["service"], r["direction"])
            if k not in lines:
                ss = route_stops(st, *k)
                lines[k] = [(x["lat"], x["lon"]) for x in ss[::max(1, len(ss) // 6)]] if ss else []
            ids = rtdata.stations_near(lines[k], stations) if lines[k] else []
            a, b = datetime.fromtimestamp(r["start_ts"], SGT), datetime.fromtimestamp(r["end_ts"], SGT)
            mm = rtdata.rain_for_trip(readings, ids, a.hour * 60 + a.minute, b.hour * 60 + b.minute + (1440 if b.date() > a.date() else 0))
            bb_sql("UPDATE rt_cond SET rain_mm=?, rain_done=1 WHERE trip_id=?", (mm, r["id"]))
            done += 1
    RT["src"]["rain"] = {"ts": time.time(), "ok": True, "detail": f"rainfall matched to {done} trips (data.gov.sg, nearest gauges to the route)"}


async def rt_fill_pv():
    """DataMall Passenger Volume by Bus Stops: monthly, published after the month ends, last 3 months kept by LTA."""
    now = now_sgt()
    svcs = rt_watch_list() or sorted({r["service"] for r in bb_sql("SELECT DISTINCT service FROM rt_trip", (), fetch=True)})
    if not svcs:
        return
    st = await static()
    keep = {x["code"] for s_ in svcs for d_ in (1, 2) for x in route_stops(st, s_, d_)}
    got_any, msgs = False, []
    for back in (1, 2, 3):
        y, m = now.year, now.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        month = f"{y}{m:02d}"
        have = bb_sql("SELECT COUNT(*) n FROM pv_stop WHERE month=?", (month,), fetch=True)[0]["n"]
        if have:
            got_any = True
            continue
        try:
            j = await get_lta(rtdata.PV_PATH, {"Date": month})
            link = ((j.get("value") or [{}])[0] or {}).get("Link")
            if not link:
                msgs.append(f"{month}: not published")
                continue
            raw = (await client().get(link, timeout=120)).content
            data = await asyncio.to_thread(rtdata.parse_pv_zip, raw, keep)
            for (dt, hr, stop), (tin, tout) in data.items():
                bb_sql("INSERT OR REPLACE INTO pv_stop(month, day_type, hour, stop, tap_in, tap_out) VALUES (?,?,?,?,?,?)", (month, dt, hr, stop, tin, tout))
            got_any = got_any or bool(data)
            msgs.append(f"{month}: {len(data)} stop-hours")
        except Exception as e:
            msgs.append(f"{month}: {type(e).__name__}")
    RT["src"]["passenger_volume"] = {"ts": time.time(), "ok": got_any, "detail": "; ".join(msgs) or "up to date"}


async def rt_fill_demand():
    """demand for each trip = average monthly tap-ins along its route in its hour (from the nearest month available)."""
    months = [r["month"] for r in bb_sql("SELECT DISTINCT month FROM pv_stop ORDER BY month DESC", (), fetch=True)]
    if not months:
        return
    rows = bb_sql("SELECT t.id, t.service, t.direction, t.date, t.start_ts, t.day_type FROM rt_trip t JOIN rt_cond c ON c.trip_id=t.id WHERE c.demand_done=0 LIMIT 3000", (), fetch=True)
    st = await static()
    cache = {}
    for r in rows:
        a = datetime.fromtimestamp(r["start_ts"], SGT)
        mon = a.strftime("%Y%m")
        use = mon if mon in months else months[0]
        dt = "Weekday" if r["day_type"] == "Weekday" else "Weekend/PH"
        k = (r["service"], r["direction"], use, dt, a.hour)
        if k not in cache:
            codes = [x["code"] for x in route_stops(st, r["service"], r["direction"])]
            if not codes:
                cache[k] = None
            else:
                q = bb_sql("SELECT SUM(tap_in) s FROM pv_stop WHERE month=? AND day_type=? AND hour=? AND stop IN (%s)" % ",".join("?" * len(codes)),
                           (use, dt, a.hour, *codes), fetch=True)
                cache[k] = q[0]["s"]
        bb_sql("UPDATE rt_cond SET demand=?, demand_done=1 WHERE trip_id=?", (cache[k], r["id"]))


async def rt_data_cycle():
    for job in (rt_holidays, rt_fill_rain, rt_fill_pv, rt_fill_demand):
        if job is rt_holidays and time.time() - (RT["src"].get("holidays") or {}).get("ts", 0) < 86400:
            continue
        if job is rt_fill_pv and time.time() - (RT["src"].get("passenger_volume") or {}).get("ts", 0) < 6 * 3600:
            continue
        try:
            await job()
        except Exception as e:
            RT["src"][job.__name__] = {"ts": time.time(), "ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}


async def rt_data_loop():
    await asyncio.sleep(20)
    while True:
        try:
            await rt_data_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(600)


def rt_sched_for(svc, d, daytype, start_min):
    rows = bb_sql("SELECT day_type, t_from, t_to, rt_min FROM rt_sched WHERE service=? AND direction=?", (svc.upper(), d), fetch=True)
    best = None
    for r in rows:
        if r["day_type"] and r["day_type"] not in ("All", daytype):
            continue
        a, b = ho_parse_hhmm(r["t_from"] or "00:00"), ho_parse_hhmm(r["t_to"] or "23:59")
        if a is None or b is None:
            continue
        inside = (a <= start_min <= b) if a <= b else (start_min >= a or start_min <= b)
        if inside and (best is None or r["day_type"] == daytype):
            best = r["rt_min"]
    return best


async def rt_trips(service="", days=120):
    """stored live trips -> the trip records the analytics engine works with."""
    q = "SELECT * FROM rt_trip WHERE start_ts > ?"
    args = [time.time() - days * 86400]
    if service.strip():
        q += " AND service=?"
        args.append(service.strip().upper())
    rows = bb_sql(q + " ORDER BY start_ts", tuple(args), fetch=True)
    conds = {r["trip_id"]: r for r in bb_sql("SELECT * FROM rt_cond", (), fetch=True)}
    ph = {r["date"] for r in bb_sql("SELECT date FROM holiday", (), fetch=True)}
    sch = [(r["d_from"], r["d_to"], r["label"]) for r in bb_sql("SELECT d_from, d_to, label FROM school_holiday", (), fetch=True)]
    st = await static()
    stops_cache = {}
    out = []
    for r in rows:
        svc, d = r["service"], r["direction"]
        if (svc, d) not in stops_cache:
            stops_cache[(svc, d)] = route_stops(st, svc, d)
        stops = stops_cache[(svc, d)]
        s0 = datetime.fromtimestamp(r["start_ts"], SGT)
        ss_min = s0.hour * 60 + s0.minute + s0.second / 60.0
        daytype = "Sunday/PH" if r["date"] in ph else r["day_type"]
        srt = rt_sched_for(svc, d, daytype, ss_min)
        c = conds.get(r["id"])
        cond = {}
        if c:
            if c["traffic_speed"] is not None:
                cond["traffic_speed"] = float(c["traffic_speed"])
            if c["incident"] is not None:
                cond["incident"] = float(c["incident"])
            if c["roadworks"] is not None:
                cond["roadworks"] = float(c["roadworks"])
            if c["rain_mm"] is not None:
                cond["weather"] = 1.0 if c["rain_mm"] >= 0.2 else 0.0
                cond["rain_mm"] = float(c["rain_mm"])
            if c["demand"] is not None:
                cond["demand"] = float(c["demand"])
        cond["event"] = 1.0 if (r["date"] in ph or rtdata.school_holiday_on(r["date"], sch)) else 0.0
        marks = json.loads(r["marks"] or "[]")
        t = {"svc": svc, "dir": d, "date": r["date"], "trip": str(r["id"]), "bus": "", "daytype": daytype,
             "ss": ss_min, "as": ss_min, "se": None if srt is None else ss_min + srt, "ae": ss_min + r["rt_min"],
             "srt": srt, "art": r["rt_min"], "cond": cond, "cover": r["cover"],
             "stops": [{"seq": i + 1, "code": (stops[i]["code"] if i < len(stops) else f"S{i}"), "name": (stops[i]["name"] if i < len(stops) else ""),
                        "sa": None, "sd": None, "aa": None if m is None else ss_min + m / 60.0, "ad": None if m is None else ss_min + m / 60.0}
                       for i, m in enumerate(marks) if i < len(stops)]}
        out.append(t)
    return out


@app.get("/api/rt/status")
async def api_rt_status():
    rows = bb_sql("SELECT service, direction, COUNT(*) n, MIN(start_ts) a, MAX(start_ts) b, AVG(rt_min) avg FROM rt_trip GROUP BY service, direction ORDER BY service, direction", (), fetch=True)
    watch = sorted({f"{k[0]}:{k[1]}" for k in bb_keys()})
    return {"ok": True, "collecting": bool(BB_ALWAYS or time.time() - BB["last_req"] < BB_IDLE_SEC), "always_on": bool(BB_ALWAYS), "watch": watch,
            "refresh_sec": BB["params"]["refresh_sec"], "saved_this_run": RT["saved"], "keep_days": RT_KEEP_DAYS,
            "services": [{"service": r["service"], "direction": r["direction"], "trips": r["n"], "avg_rt": round(r["avg"], 1),
                          "from": datetime.fromtimestamp(r["a"], SGT).strftime("%d %b %H:%M"), "to": datetime.fromtimestamp(r["b"], SGT).strftime("%d %b %H:%M")} for r in rows],
            "bytes": (bb_sql("SELECT COALESCE(SUM(LENGTH(marks)) + COUNT(*) * 120, 0) b FROM rt_trip", (), fetch=True)[0]["b"] or 0),
            "total_trips": (bb_sql("SELECT COUNT(*) n FROM rt_trip", (), fetch=True)[0]["n"] or 0),
            "note": "Running time is measured from live bus positions (LTA DataMall). DataMall and OneMap publish no historical bus running times, so history starts when collection starts."}


@app.post("/api/rt/watch")
async def api_rt_watch(request: Request):
    b = json.loads((await request.body()).decode("utf-8") or "{}")
    svc = str(b.get("service") or "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{1,6}", svc):
        return JSONResponse({"error": "Enter a service number."}, status_code=400)
    if b.get("remove"):
        bb_sql("DELETE FROM rt_watch WHERE service=?", (svc,))
        return {"ok": True, "watch": rt_watch_list()}
    if svc not in rt_watch_list() and len(rt_watch_list()) >= RT_MAX_SERVICES:
        return JSONResponse({"error": f"Already measuring {RT_MAX_SERVICES} services round the clock (RT_MAX_SERVICES). Remove one first."}, status_code=400)
    bb_sql("INSERT OR REPLACE INTO rt_watch(service, ts) VALUES (?,?)", (svc, time.time()))
    for d in (1, 2):
        BB["watch"][(svc, int(d))] = time.time()
    BB["last_req"] = time.time()
    try:
        await bb_cycle([(svc, d) for d in (1, 2)])
    except Exception:
        pass
    return {"ok": True, "watch": rt_watch_list()}


# ---- V17.3 Stop-by-Stop measured running time report (stop_report.py). Uses the rt_trip passing times recorded by the live collector.
import stop_report


def _sr_date(v, default):
    try:
        return datetime.strptime((v or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return default


async def sr_load(service, direction, day_type, date_from, date_to, demo):
    """-> (meta, trips, stops) or (error, None, None)"""
    today = now_sgt().date()
    d_to = _sr_date(date_to, today)
    d_from = _sr_date(date_from, d_to - timedelta(days=29))
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    if demo:
        rows, stops = await asyncio.to_thread(stop_report.demo)
        svc, d, hol = "14", 1, []
        d_from = d_to = None
    else:
        svc = service.strip().upper()
        if not re.fullmatch(r"[0-9A-Z]{1,6}", svc):
            return {"error": "Choose a bus service."}, None, None
        d = 2 if str(direction) == "2" else 1
        a = datetime(d_from.year, d_from.month, d_from.day, tzinfo=SGT).timestamp()
        b = datetime(d_to.year, d_to.month, d_to.day, tzinfo=SGT).timestamp() + 86400
        rows = bb_sql("SELECT id, date, day_type, start_ts, rt_min, cover, marks FROM rt_trip WHERE service=? AND direction=? AND start_ts>=? AND start_ts<? "
                      "ORDER BY start_ts LIMIT 20000", (svc, d, a, b), fetch=True)
        st = await static()
        stops = route_stops(st, svc, d)
        hol = [r["date"] for r in bb_sql("SELECT date FROM holiday", (), fetch=True)]
        if not stops:
            return {"error": f"No LTA stop list for service {svc} direction {d}."}, None, None
    trips = stop_report.clean_trips(rows, stops, hol)
    if day_type in ("Weekday", "Saturday", "Sunday/PH"):
        trips = [t for t in trips if t["day_type"] == day_type]
    meta = {"service": svc, "direction": d, "day_type": day_type if day_type in ("Weekday", "Saturday", "Sunday/PH") else "All",
            "from": d_from.isoformat() if d_from else None, "to": d_to.isoformat() if d_to else None, "demo": bool(demo),
            "generated": now_sgt().strftime("%d %b %Y %H:%M SGT") + (" · DEMO (simulated trips)" if demo else "")}
    return meta, trips, stops


def _sr_params(period, basis, min_n):
    p = int(in_num(period, 60, 15, 120))
    p = min((15, 30, 60, 120), key=lambda x: abs(x - p))
    return p, ("stop" if basis == "stop" else "trip"), int(in_num(min_n, 3, 1, 50))


def _sr_name(meta, ext):
    span = f"{meta['from']}_{meta['to']}" if meta.get("from") else "DEMO"
    return f"HORIZON_stop_running_time_{meta['service']}_D{meta['direction']}_{span}.{ext}"


@app.get("/stop-running-time", response_class=HTMLResponse)
async def stop_rt_page():
    return _page("stop_rt.html")


@app.get("/api/stop-rt/services")
async def api_sr_services():
    rows = bb_sql("SELECT service, direction, COUNT(*) n, MIN(start_ts) a, MAX(start_ts) b FROM rt_trip GROUP BY service, direction ORDER BY service, direction", (), fetch=True)
    return {"measuring": rt_watch_list(), "max": RT_MAX_SERVICES, "keep_days": RT_KEEP_DAYS,
            "collecting": bool(BB_ALWAYS or time.time() - BB["last_req"] < BB_IDLE_SEC or rt_watch_list()),
            "with_data": [{"service": r["service"], "direction": r["direction"], "trips": r["n"],
                           "from": datetime.fromtimestamp(r["a"], SGT).strftime("%Y-%m-%d"), "to": datetime.fromtimestamp(r["b"], SGT).strftime("%Y-%m-%d")} for r in rows],
            "db_ok": BB.get("db_ok", True), "storage": bb_rec_status()}


@app.get("/api/stop-rt/report")
async def api_sr_report(service: str = "", direction: str = "1", day_type: str = "", date_from: str = "", date_to: str = "",
                        period: str = "60", basis: str = "trip", min_n: str = "3", demo: int = 0):
    meta, trips, stops = await sr_load(service, direction, day_type, date_from, date_to, demo)
    if trips is None:
        return {"ok": False, **meta}
    if not trips:
        return {"ok": False, "meta": meta, "error": f"No measured trips for service {meta['service']} direction {meta['direction']} in this period. "
                                                     "Add the service to the measuring list; history starts from then."}
    p, b, mn = _sr_params(period, basis, min_n)
    rep = await asyncio.to_thread(stop_report.summarise, trips, stops, p, b, mn)
    return {"ok": True, "meta": meta, "report": rep}


@app.get("/api/stop-rt/report.xlsx")
async def api_sr_xlsx(service: str = "", direction: str = "1", day_type: str = "", date_from: str = "", date_to: str = "",
                      period: str = "60", basis: str = "trip", min_n: str = "3", demo: int = 0):
    from fastapi.responses import Response
    meta, trips, stops = await sr_load(service, direction, day_type, date_from, date_to, demo)
    if not trips:
        return JSONResponse({"error": (meta or {}).get("error") or "No measured trips in this period."}, status_code=404)
    p, b, mn = _sr_params(period, basis, min_n)

    def build():
        rep = stop_report.summarise(trips, stops, p, b, mn)
        return stop_report.excel(meta, rep, trips, stops, stop_report.segment_records(trips, stops, p, b))
    data = await asyncio.to_thread(build)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{_sr_name(meta, "xlsx")}"', "Cache-Control": "no-store"})


@app.get("/api/stop-rt/segments.csv")
async def api_sr_csv(service: str = "", direction: str = "1", day_type: str = "", date_from: str = "", date_to: str = "",
                     period: str = "60", basis: str = "trip", demo: int = 0):
    from fastapi.responses import Response
    meta, trips, stops = await sr_load(service, direction, day_type, date_from, date_to, demo)
    if not trips:
        return JSONResponse({"error": (meta or {}).get("error") or "No measured trips in this period."}, status_code=404)
    p, b, _ = _sr_params(period, basis, "3")
    text = await asyncio.to_thread(lambda: stop_report.csv_segments(stop_report.segment_records(trips, stops, p, b)))
    return Response(text.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{_sr_name(meta, "csv")}"', "Cache-Control": "no-store"})


@app.get("/api/rt/sources")
async def api_rt_sources():
    n = lambda q: bb_sql(q, (), fetch=True)[0]["n"]
    pv = bb_sql("SELECT month, COUNT(*) n FROM pv_stop GROUP BY month ORDER BY month DESC", (), fetch=True)
    fmt = lambda x: datetime.fromtimestamp(x["ts"], SGT).strftime("%d %b %H:%M") if x and x.get("ts") else "not yet"
    src = RT["src"]
    return {"always_on": rt_watch_list(), "max_services": RT_MAX_SERVICES, "poll_sec": RT_POLL_SEC,
            "sources": [
                {"name": "Bus positions → running time", "by": "LTA DataMall Bus Arrival", "when": "every poll", "status": f"{n('SELECT COUNT(*) n FROM rt_trip'):,} trips measured",
                 "ok": True, "last": datetime.fromtimestamp(RT["last"], SGT).strftime("%d %b %H:%M") if RT["last"] else "none this run"},
                {"name": "Traffic, incidents, road works on the route", "by": "LTA DataMall (live)", "when": "captured as each trip ends",
                 "status": f"{n('SELECT COUNT(*) n FROM rt_cond WHERE traffic_speed IS NOT NULL'):,} trips with traffic speed",
                 "ok": (src.get("live_conditions") or {}).get("ok", True), "last": fmt(src.get("live_conditions")), "detail": (src.get("live_conditions") or {}).get("detail", "")},
                {"name": "Rainfall", "by": "data.gov.sg (NEA gauges, 5-min)", "when": "every 10 min, backfilled by date",
                 "status": f"{n('SELECT COUNT(*) n FROM rt_cond WHERE rain_done=1'):,} trips matched · {n('SELECT COUNT(*) n FROM weather_day'):,} days cached",
                 "ok": (src.get("rain") or {}).get("ok", True), "last": fmt(src.get("rain")), "detail": (src.get("rain") or {}).get("detail", "")},
                {"name": "Public holidays", "by": "data.gov.sg (MOM)", "when": "daily", "status": f"{n('SELECT COUNT(*) n FROM holiday'):,} dates",
                 "ok": (src.get("holidays") or {}).get("ok", True), "last": fmt(src.get("holidays")), "detail": (src.get("holidays") or {}).get("detail", "")},
                {"name": "School holidays", "by": "MOE calendar (seeded 2026, editable)", "when": "on change",
                 "status": f"{n('SELECT COUNT(*) n FROM school_holiday'):,} periods", "ok": True, "last": "-", "detail": ""},
                {"name": "Passenger volume by bus stop", "by": "LTA DataMall (monthly)", "when": "every 6 h until the new month is published",
                 "status": ", ".join(f"{p['month'][:4]}-{p['month'][4:]}: {p['n']:,}" for p in pv[:3]) or "no month loaded yet",
                 "ok": (src.get("passenger_volume") or {}).get("ok", True), "last": fmt(src.get("passenger_volume")), "detail": (src.get("passenger_volume") or {}).get("detail", "")},
            ],
            "school": [dict(r) for r in bb_sql("SELECT id, d_from, d_to, label FROM school_holiday ORDER BY d_from", (), fetch=True)]}


@app.post("/api/rt/refresh-data")
async def api_rt_refresh(request: Request):
    RT["src"].pop("holidays", None)
    RT["src"].pop("passenger_volume", None)
    await rt_data_cycle()
    return await api_rt_sources()


@app.post("/api/rt/school")
async def api_rt_school(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    b = json.loads((await request.body()).decode("utf-8") or "{}")
    if b.get("delete"):
        bb_sql("DELETE FROM school_holiday WHERE id=?", (int(b["delete"]),))
        return {"ok": True}
    a, z = str(b.get("from") or "")[:10], str(b.get("to") or "")[:10]
    if not (re.match(r"\d{4}-\d{2}-\d{2}$", a) and re.match(r"\d{4}-\d{2}-\d{2}$", z) and a <= z):
        return JSONResponse({"error": "Dates must be YYYY-MM-DD, from before to."}, status_code=400)
    bb_sql("INSERT INTO school_holiday(d_from, d_to, label) VALUES (?,?,?)", (a, z, str(b.get("label") or "School holidays")[:80]))
    IN_CACHE.clear()
    return {"ok": True}


@app.get("/api/rt/sched")
async def api_rt_sched(service: str = ""):
    q = "SELECT * FROM rt_sched" + (" WHERE service=?" if service.strip() else "") + " ORDER BY service, direction, t_from"
    rows = bb_sql(q, ((service.strip().upper(),) if service.strip() else ()), fetch=True)
    return {"rows": [dict(r) for r in rows]}


@app.post("/api/rt/sched")
async def api_rt_sched_set(request: Request):
    """Scheduled running time is not published by LTA DataMall, so the timetable value is entered here per service / direction / day type / period."""
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    b = json.loads((await request.body()).decode("utf-8") or "{}")
    if b.get("delete"):
        bb_sql("DELETE FROM rt_sched WHERE id=?", (int(b["delete"]),))
        return {"ok": True}
    svc = str(b.get("service") or "").strip().upper()
    try:
        d, rt = int(b.get("direction") or 1), float(b.get("rt_min"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Direction and running time must be numbers."}, status_code=400)
    if not svc or not (1 <= rt <= 400):
        return JSONResponse({"error": "Enter a service and a running time between 1 and 400 min."}, status_code=400)
    dt = b.get("day_type") if b.get("day_type") in ("All",) + insight.DAY_TYPES else "All"
    bb_sql("INSERT INTO rt_sched(service,direction,day_type,t_from,t_to,rt_min,by_name,ts) VALUES (?,?,?,?,?,?,?,?)",
           (svc, d, dt, str(b.get("t_from") or "00:00")[:5], str(b.get("t_to") or "23:59")[:5], rt, str(b.get("by") or "")[:60], time.time()))
    IN_CACHE.clear()
    return {"ok": True}


def in_init():
    bb_sql("CREATE TABLE IF NOT EXISTS insight_dataset(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, kind TEXT, rows INTEGER, trips INTEGER, info TEXT, blob TEXT)")


async def in_live():
    """the live LTA-measured trips, in the same shape as an uploaded dataset."""
    hit = IN_CACHE.get(0)
    if hit and time.time() - hit[2] < 120:
        return hit[0], hit[1]
    trips = await rt_trips()
    if not trips:
        info = {"error": "No running time has been measured yet. Add a service to the collector below and leave the page open; trips appear as buses complete the route.",
                "trips": 0, "rows": 0, "services": []}
        return None, info
    keep, removed = await asyncio.to_thread(insight.drop_outliers, trips)
    dates = sorted({t["date"] for t in keep if t["date"]})
    info = {"trips": len(trips), "rows": len(trips), "dropped_incomplete": 0, "bad_rows": 0, "stop_level": True, "columns_used": ["live"],
            "columns_missing": [], "conditions": [], "dates": [dates[0], dates[-1]] if dates else [None, None],
            "services": sorted({t["svc"] for t in keep}), "daytypes": sorted({t["daytype"] for t in keep}), "source": "live",
            "outliers_removed": removed, "analysed": len(keep), "no_sched": sum(1 for t in keep if t["srt"] is None),
            "cover_p50": round(insight.pct([t.get("cover") or 1 for t in keep], 50) * 100, 1)}
    IN_CACHE[0] = (keep, info, time.time())
    return keep, info


def in_load(ds_id):
    """parsed trips for a stored dataset (cached in memory)."""
    hit = IN_CACHE.get(ds_id)
    if hit:
        return hit[0], hit[1]
    rows = bb_sql("SELECT blob FROM insight_dataset WHERE id=?", (ds_id,), fetch=True)
    if not rows:
        return None, {"error": "Dataset not found."}
    text = gzip.decompress(base64.b64decode(rows[0]["blob"])).decode("utf-8", "replace")
    trips, info = insight.parse_csv(text)
    if trips is None:
        return None, info
    trips, removed = insight.drop_outliers(trips)
    info["outliers_removed"] = removed
    info["analysed"] = len(trips)
    if len(IN_CACHE) > 4:
        IN_CACHE.clear()
    IN_CACHE[ds_id] = (trips, info, time.time())
    return trips, info


def in_save(name, kind, text):
    trips, info = insight.parse_csv(text)
    if trips is None:
        return None, info.get("error") or "The file could not be read."
    blob = base64.b64encode(gzip.compress(text.encode("utf-8"))).decode()
    bb_sql("INSERT INTO insight_dataset(ts, name, kind, rows, trips, info, blob) VALUES (?,?,?,?,?,?,?)",
           (time.time(), name[:80], kind, info["rows"], info["trips"], json.dumps(info), blob))
    r = bb_sql("SELECT id FROM insight_dataset ORDER BY id DESC LIMIT 1", (), fetch=True)
    IN_CACHE.clear()
    return (r[0]["id"] if r else None), None


async def in_get(ds_id, ref="auto"):
    """ds 0 = the live LTA-measured trips; any other id = a stored sample dataset.
    `ref` decides what the actual running time is compared with: the entered timetable, or the service's own quiet-period baseline."""
    trips, info = (await in_live()) if not ds_id else (await asyncio.to_thread(in_load, int(ds_id)))
    if trips is None:
        return None, info
    ref = ref if ref in ("timetable", "baseline", "auto") else "auto"
    trips = [dict(t) for t in trips]
    trips, basis, base = await asyncio.to_thread(insight.apply_reference, trips, ref)
    info = dict(info, basis=basis, reference=ref, baselines={f"{k[0]}:{k[1]}:{k[2]}": v for k, v in base.items()},
                n_no_ref=sum(1 for t in trips if t["srt"] is None))
    return trips, info


def in_pctl(v, d=85):
    try:
        p = float(v) if str(v).strip() else d
    except ValueError:
        return d
    return min(99.0, max(50.0, p))


def in_num(v, d, lo, hi):
    try:
        x = float(str(v).strip()) if str(v).strip() else d
    except ValueError:
        return d
    return min(hi, max(lo, x))


@app.get("/running-time", response_class=HTMLResponse)
async def running_time_page():
    return HTMLResponse((HERE / "insight.html").read_text(encoding="utf-8"))


@app.get("/insight", response_class=HTMLResponse)
async def insight_page():
    return HTMLResponse((HERE / "insight.html").read_text(encoding="utf-8"))


@app.get("/api/insight/datasets")
async def api_in_datasets():
    rows = bb_sql("SELECT id, ts, name, kind, rows, trips, info FROM insight_dataset ORDER BY id DESC LIMIT 30", (), fetch=True)
    out = []
    for r in rows:
        info = json.loads(r["info"] or "{}")
        out.append({"id": r["id"], "name": r["name"], "kind": r["kind"], "rows": r["rows"], "trips": r["trips"],
                    "time": datetime.fromtimestamp(r["ts"], SGT).strftime("%d %b %H:%M"), "services": info.get("services", []),
                    "dates": info.get("dates", [None, None]), "stop_level": info.get("stop_level"), "conditions": info.get("conditions", []),
                    "daytypes": info.get("daytypes", [])})
    return {"datasets": out, "model": insight.VERSION}


@app.post("/api/insight/demo")
async def api_in_demo(request: Request, days: int = 40):
    """Create a MODELLED demo dataset so the page can be seen working without real data."""
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    text = await asyncio.to_thread(insight.demo, max(7, min(90, days)))
    ds_id, err = await asyncio.to_thread(in_save, f"DEMO (modelled data, {max(7, min(90, days))} days)", "demo", text)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True, "id": ds_id}


@app.post("/api/insight/delete")
async def api_in_delete(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    b = json.loads((await request.body()).decode("utf-8") or "{}")
    bb_sql("DELETE FROM insight_dataset WHERE id=?", (int(b.get("id") or 0),))
    IN_CACHE.clear()
    return {"ok": True}


def in_filters(trips, daytype, date_from, date_to):
    return insight.filt(trips, daytype=daytype or None, date_from=(date_from or None), date_to=(date_to or None))


@app.get("/api/insight/engineering")
async def api_in_engineering(service: str = "", direction: int = 1, pctl: int = 85, pax: float = 3.0, recovery: float = 7.0, junctions: int = -1, rain: int = 0, draws: int = 1000):
    """Day-1 running-time estimate: no completed-trip history required."""
    svc = service.strip().upper()
    if not svc:
        return {"ok": False, "error": "Enter a bus service."}
    st = await static()
    stops = route_stops(st, svc, direction)
    if not stops:
        return {"ok": False, "error": f"Service {svc} direction {direction} was not found in LTA Bus Routes."}
    geom = await route_geometry(svc, direction, stops)
    bands = await bands_state()
    runs, stats = await asyncio.to_thread(color_route, geom["line"], bands.get("idx"))
    official = max((x["dist"] for x in stops if x.get("dist") is not None), default=None)
    route_km = official or sum(stats["km"].values()) or line_len_km(geom["line"])
    # If live speed bands are unavailable, use a clearly-labelled engineering fallback rather than historical trip data.
    traffic_live = bool(stats.get("known"))
    drive_min = stats.get("driveMin") if traffic_live else route_km / 25.0 * 60.0
    try:
        inc = (await api_incidents("", 1)).get("incidents", [])
    except Exception:
        inc = []
    try:
        rw, _ = await tr_roadworks(time.time())
    except Exception:
        rw = []
    sl = offservice.simplify(geom["line"], 200)
    n_inc = sum(1 for x in inc if x.get("lat") is not None and offservice.dist_to_line_m((x["lat"], x["lon"]), sl) <= 60)
    n_rw = sum(1 for x in rw if x.get("lat") is not None and offservice.dist_to_line_m((x["lat"], x["lon"]), sl) <= 60)
    sim = await asyncio.to_thread(engineering_rt.simulate, drive_min=drive_min, stops=len(stops), route_km=route_km, pax_per_stop=pax,
                                  junctions=None if junctions < 0 else junctions, recovery_min=recovery, incidents=n_inc, roadworks=n_rw,
                                  rain=bool(rain), pctl=max(50,min(99,pctl)), draws=draws)
    return {"ok": True, "service":svc, "direction":direction, "availableDirections":st["dirs"].get(svc, []),
            "traffic":{"live":traffic_live,"basis":"live LTA speed bands" if traffic_live else "25 km/h engineering fallback","drive_min":round(drive_min,1)},
            "route":{"km":round(route_km,2),"stops":len(stops),"geometry":geom["source"]}, "simulation":sim,
            "events":{"incidents":n_inc,"roadworks":n_rw}, "model":engineering_rt.VERSION}


@app.get("/api/insight/summary")
async def api_in_summary(ds: int = 0, ref: str = "auto", pctl: str = "85", band: str = "30", daytype: str = "", date_from: str = "", date_to: str = "", min_n: str = "10"):
    trips, info = await in_get(ds, ref)
    if trips is None:
        return {"ok": False, "error": info.get("error", "No data")}
    p, b, mn = in_pctl(pctl), int(in_num(band, 30, 15, 60)), int(in_num(min_n, 10, 1, 500))
    sel = in_filters(trips, daytype, date_from, date_to)
    rows = await asyncio.to_thread(insight.summary, sel, p, b, mn)
    return {"ok": True, "rows": rows, "pctl": p, "band": b, "min_n": mn, "n_trips": len(sel), "info": info, "basis": info.get("basis"), "reference": info.get("reference"),
            "quality": {"trips_in_file": info.get("trips"), "analysed": len(sel), "outliers_removed": info.get("outliers_removed"),
                        "dropped_incomplete": info.get("dropped_incomplete"), "dates": info.get("dates"), "stop_level": info.get("stop_level"),
                        "conditions": info.get("conditions"), "missing_columns": info.get("columns_missing")}}


@app.get("/api/insight/service")
async def api_in_service(ds: int = 0, ref: str = "auto", service: str = "", pctl: str = "85", band: str = "30", daytype: str = "", date_from: str = "", date_to: str = "",
                         min_n: str = "5", model: str = "0"):
    trips, info = await in_get(ds, ref)
    if trips is None:
        return {"ok": False, "error": info.get("error", "No data")}
    svc = service.strip().upper()
    p, b, mn = in_pctl(pctl), int(in_num(band, 30, 15, 60)), int(in_num(min_n, 5, 1, 500))
    sel = in_filters(trips, daytype, date_from, date_to)
    g = [t for t in sel if t["svc"] == svc]
    if not g:
        return {"ok": False, "error": f"No trips for service {svc} with these filters."}
    out = {"ok": True, "service": svc, "pctl": p, "band": b, "min_n": mn, "dirs": {}, "n": len(g), "basis": info.get("basis"), "reference": info.get("reference"),
           "baseline": {d_: info.get("baselines", {}).get(f"{svc}:{d_}:Weekday") for d_ in (1, 2)},
           "dates": [min((t["date"] for t in g if t["date"]), default=None), max((t["date"] for t in g if t["date"]), default=None)],
           "daytypes": sorted({t["daytype"] for t in g}), "info": info}
    for d in (1, 2):
        gd = [t for t in g if t["dir"] == d]
        if not gd:
            out["dirs"][str(d)] = None
            continue
        sched = insight.pct([t["srt"] for t in gd], 50)
        dd = insight.dist([t["art"] for t in gd], sched, p)
        periods = await asyncio.to_thread(insight.by_period, gd, svc, d, b, p, mn)
        short = [r for r in periods if (r.get("gap") or 0) > 0.5 and not r["small_sample"]]
        out["dirs"][str(d)] = {"overall": dd, "periods": periods, "n_short": len(short),
                               "worst": max(short, key=lambda r: r["gap"]) if short else None,
                               "options": insight.options([t["art"] for t in gd], sched, p),
                               "stops": insight.stop_list(gd, svc, d),
                               "scenarios": await asyncio.to_thread(insight.scenarios, gd, p),
                               "contributors": await asyncio.to_thread(insight.contributors, gd, None, p)}
        if str(model) == "1":
            out["dirs"][str(d)]["model"] = await asyncio.to_thread(insight.quantile_model, gd)
    return out


@app.get("/api/insight/sections")
async def api_in_sections(ds: int = 0, ref: str = "auto", service: str = "", direction: int = 1, stops: str = "", pctl: str = "85", band: str = "30",
                          daytype: str = "", date_from: str = "", date_to: str = "", min_n: str = "5"):
    trips, info = await in_get(ds, ref)
    if trips is None:
        return {"ok": False, "error": info.get("error", "No data")}
    svc = service.strip().upper()
    p, b, mn = in_pctl(pctl), int(in_num(band, 30, 15, 60)), int(in_num(min_n, 5, 1, 500))
    sel = [t for t in in_filters(trips, daytype, date_from, date_to) if t["svc"] == svc and t["dir"] == direction]
    if not sel:
        return {"ok": False, "error": f"No trips for service {svc} direction {direction}."}
    all_stops = insight.stop_list(sel, svc, direction)
    if not all_stops:
        return {"ok": False, "error": "This dataset has no stop-level times, so the route cannot be split into sections. Upload stop-level data for sectional analysis."}
    picked = [c for c in re.split(r"[,\s]+", stops.strip()) if c]
    if len(picked) < 2:
        step = max(1, len(all_stops) // 5)
        picked = [s["code"] for s in all_stops[::step]]
        if all_stops[-1]["code"] not in picked:
            picked.append(all_stops[-1]["code"])
    secs = insight.pair_sections(all_stops, picked)
    if not secs:
        return {"ok": False, "error": "Select at least two stops of this direction."}
    rows, heat = await asyncio.to_thread(insight.sections_analysis, sel, secs, p, b, mn)
    worst = max([r for r in rows if r.get("gap") is not None], key=lambda r: r["gap"], default=None)
    contrib = None
    if worst:
        wsec = next(s for s in secs if s["label"] == worst["label"])
        vals = insight.section_times(sel, wsec)
        contrib = await asyncio.to_thread(insight.contributors, sel, vals, p)
    return {"ok": True, "service": svc, "direction": direction, "pctl": p, "band": b, "stops": all_stops, "picked": picked,
            "sections": rows, "heat": heat, "worst": worst, "contributors": contrib, "n": len(sel),
            "route_gap": round(sum(max(0.0, r.get("gap") or 0.0) for r in rows), 1)}


@app.get("/api/insight/cell")
async def api_in_cell(ds: int = 0, ref: str = "auto", service: str = "", direction: int = 1, section: str = "", stops: str = "", band_start: str = "", band: str = "30",
                      daytype: str = "", date_from: str = "", date_to: str = "", limit: int = 60):
    """the trips behind one heatmap cell."""
    trips, info = await in_get(ds, ref)
    if trips is None:
        return {"ok": False, "error": info.get("error", "No data")}
    svc = service.strip().upper()
    b = int(in_num(band, 30, 15, 60))
    sel = [t for t in in_filters(trips, daytype, date_from, date_to) if t["svc"] == svc and t["dir"] == direction]
    all_stops = insight.stop_list(sel, svc, direction)
    picked = [c for c in re.split(r"[,\s]+", stops.strip()) if c]
    secs = insight.pair_sections(all_stops, picked)
    sec = next((s for s in secs if s["label"] == section), None)
    if not sec:
        return {"ok": False, "error": "Unknown section."}
    bs = insight.parse_time(band_start)
    vals = [v for v in insight.section_times(sel, sec) if v["start"] is not None and (bs is None or insight.band_of(v["start"], b) == insight.band_of(bs, b))]
    vals.sort(key=lambda v: -v["act"])
    out = [{"date": v["trip"]["date"], "trip": v["trip"]["trip"], "bus": v["trip"]["bus"], "daytype": v["trip"]["daytype"],
            "start": insight.band_label(insight.band_of(v["start"], b), b), "start_clock": f"{int(v['start'] // 60) % 24:02d}:{int(v['start'] % 60):02d}",
            "section_act": round(v["act"], 1), "section_sch": None if v["sch"] is None else round(v["sch"], 1),
            "gap": None if v["sch"] is None else round(v["act"] - v["sch"], 1), "trip_act": round(v["trip"]["art"], 1), "trip_sch": round(v["trip"]["srt"], 1),
            "cond": {k: round(x, 2) for k, x in v["trip"]["cond"].items()}} for v in vals[:limit]]
    return {"ok": True, "section": section, "band": insight.band_label(insight.band_of(bs, b), b) if bs is not None else "All day", "n": len(vals), "trips": out}


# ----------------------------------------------------------------------------- V13.8 zero-history Time Period Report (hourly TPR + graphs + Excel)
import tpr_engine
import tpr_excel
from fastapi.responses import Response

TPR_PV_TRY = {}      # "svc:dir" -> ts of the last passenger-volume download attempt for a service nobody is collecting


def tpr_pv_init():
    bb_sql("CREATE TABLE IF NOT EXISTS tpr_pv(month TEXT, day_type TEXT, hour INTEGER, stop TEXT, tap_in INTEGER, tap_out INTEGER, PRIMARY KEY(month, day_type, hour, stop))")


async def tpr_fetch_pv(codes):
    """Latest published DataMall Passenger Volume month, kept only for these stops (separate table so the collector's own months are untouched)."""
    tpr_pv_init()
    now = now_sgt()
    for back in (1, 2, 3):
        y, m = now.year, now.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        month = f"{y}{m:02d}"
        try:
            j = await get_lta(rtdata.PV_PATH, {"Date": month})
            link = ((j.get("value") or [{}])[0] or {}).get("Link")
            if not link:
                continue
            raw = (await client().get(link, timeout=180)).content
            data = await asyncio.to_thread(rtdata.parse_pv_zip, raw, set(codes))
            for (dt, hr, stop), (tin, tout) in data.items():
                bb_sql("INSERT OR REPLACE INTO tpr_pv(month, day_type, hour, stop, tap_in, tap_out) VALUES (?,?,?,?,?,?)", (month, dt, hr, stop, tin, tout))
            if data:
                return month
        except Exception:
            continue
    return None


def tpr_pv_rows(codes, pv_day):
    """{(stop, hour): tap_in+tap_out} for the newest month that has these stops, from the collector table or the TPR table."""
    tpr_pv_init()
    if not codes:
        return None, {}
    ph = ",".join("?" * len(codes))
    for table in ("pv_stop", "tpr_pv"):
        mm = bb_sql(f"SELECT month, COUNT(*) n FROM {table} WHERE day_type=? AND stop IN ({ph}) GROUP BY month ORDER BY month DESC LIMIT 1", (pv_day, *codes), fetch=True)
        if mm and mm[0]["n"]:
            month = mm[0]["month"]
            rows = bb_sql(f"SELECT stop, hour, tap_in, tap_out FROM {table} WHERE month=? AND day_type=? AND stop IN ({ph})", (month, pv_day, *codes), fetch=True)
            return month, {(r["stop"], int(r["hour"])): (r["tap_in"] or 0) + (r["tap_out"] or 0) for r in rows}
    return None, {}


def tpr_day_dt(day_type, hour):
    """a representative datetime of that day type and hour (only used to look up the scheduled headway)."""
    base = now_sgt().replace(minute=15, second=0, microsecond=0)
    want = {"Weekday": 2, "Saturday": 5, "Sunday": 6}.get(day_type, 2)
    return (base + timedelta(days=(want - base.weekday()) % 7)).replace(hour=hour)


async def tpr_report(svc, d, day_type, scheme, stops_q, pax, pctl, recovery, wait_pv=False):
    st = await static()
    stops = route_stops(st, svc, d)
    if not stops:
        return {"ok": False, "error": f"Service {svc} direction {d} was not found in LTA Bus Routes.", "availableDirections": st["dirs"].get(svc, [])}
    route = await bb_route(svc, d)
    geom = await route_geometry(svc, d, stops)
    line = geom["line"]
    gk = geom_key(svc, d, stops)
    prep = PREP.get(gk)
    if prep is None:
        prep = PREP[gk] = await asyncio.to_thread(headway.prepare, line, stops)
    ss = prep["stop_s"]
    tm = headway.TimeModel(route.get("runs", []), ss, headway.CFG) if (route.get("traffic") or {}).get("ok") else None
    if tm is not None and not tm.ok:
        tm = None
    n = len(stops)
    seg_km, seg_live, seg_free = [], [], []
    for i in range(n - 1):
        a, b = stops[i].get("dist"), stops[i + 1].get("dist")
        km = (b - a) if (a is not None and b is not None and b > a) else max(0.0, ss[i + 1] - ss[i])
        seg_km.append(km)
        if tm is not None:
            geo = max(1e-6, ss[i + 1] - ss[i])
            scale = km / geo if geo > 0.02 else 1.0            # keep LTA distance, use the line only for the speed mix
            seg_live.append(tm._span(ss[i], ss[i + 1]) * scale)
            seg_free.append(tm._span(ss[i], ss[i + 1], free=True) * scale)
    codes = [s["code"] for s in stops]
    pv_day = "Weekday" if day_type == "Weekday" else "Weekend/PH"
    month, vol = tpr_pv_rows(codes, pv_day)
    pv_note = None
    if not vol:
        key = f"{svc}:{d}"
        if time.time() - TPR_PV_TRY.get(key, 0) > 6 * 3600:
            TPR_PV_TRY[key] = time.time()
            task = asyncio.create_task(tpr_fetch_pv(codes))
            if wait_pv:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 150)
                except Exception:
                    pass
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 25)
                except Exception:
                    pv_note = "Passenger volume is downloading from DataMall - generate again in a minute to use it."
            month, vol = tpr_pv_rows(codes, pv_day)
    fq = await freq_table()
    hw_src, hw_by_h = set(), {}
    for h in range(24):
        hw, src = bb_resolve_hw(svc, d, tpr_day_dt(day_type, h), fq)
        hw_by_h[h] = hw or 10.0
        hw_src.add(src.split(":")[0] if src else "10 min assumed (no LTA frequency)")
    ndays = tpr_engine.days_in_month(month, pv_day) if month else None

    def pax_fn(code, h):
        v = vol.get((code, h))
        if v is None or not ndays:
            return None
        n_svc = max(1, len(st["at_stop"].get(code, ())) or 1)
        return v / ndays / n_svc / (60.0 / hw_by_h[h])

    picked = [c for c in re.split(r"[,\s]+", stops_q.strip()) if c]
    now = now_sgt()
    res = tpr_engine.build(stops=[{"code": s["code"], "name": s.get("name", ""), "seq": s["seq"]} for s in stops], seg_live_min=seg_live or None,
                           seg_free_min=seg_free or None, seg_km=seg_km, day_type=day_type, now_hour=now.hour, now_day_type=bb_day_type(now),
                           traffic_live=tm is not None, pax_fn=pax_fn, default_pax=pax, scheme=scheme, picked=picked, pctl=pctl, recovery_min=recovery)
    sources = {
        "traffic": (f"live LTA speed bands at {now.strftime('%H:%M')} ({tm.known_pct}% of route with a reading), shaped by hour with the speed profile"
                    if tm is not None else f"speed bands unavailable - {tpr_engine.FALLBACK_KMH:.0f} km/h off-peak fallback x hourly profile"),
        "passengers": (f"DataMall passenger volume {month[:4]}-{month[4:]} ({pv_day}), {res['pax_coverage']:.0f}% of stop-hours covered; others use {pax} pax/stop"
                       if month else f"{pax} passengers per stop (assumption) - DataMall passenger volume not available yet"),
        "headway": ", ".join(sorted(hw_src)),
    }
    return {"ok": True, "service": svc, "direction": d, "day_type": day_type, "scheme": scheme, "availableDirections": st["dirs"].get(svc, []),
            "stops": [{"code": s["code"], "name": s.get("name", ""), "seq": s["seq"], "km": round(s["dist"], 2) if s.get("dist") is not None else None} for s in stops],
            "route_km": round(sum(seg_km), 2), "tpr": res, "sources": sources, "pv_note": pv_note, "assumptions": tpr_engine.ASSUMPTIONS,
            "generated": now.strftime("%d %b %Y %H:%M SGT"), "model": tpr_engine.VERSION,
            "label": "MODELLED - zero-history engineering TPR (no completed trips used)"}


def tpr_args(service, day_type, scheme, pctl, pax, recovery):
    return (service.strip().upper(), day_type if day_type in tpr_engine.SPEED_PROFILE else "Weekday", scheme if scheme in tpr_engine.SCHEMES else "tpr",
            in_num(pax, 3.0, 0, 50), int(in_pctl(pctl)), in_num(recovery, 7.0, 0, 60))


@app.get("/api/insight/tpr")
async def api_in_tpr(service: str = "", direction: int = 1, day_type: str = "Weekday", scheme: str = "tpr", stops: str = "", pctl: str = "85",
                     pax: str = "3", recovery: str = "7"):
    """Hourly / time-period running time for every stop pair, from the formula - no past trips needed."""
    svc, dt, sc, px, p, rc = tpr_args(service, day_type, scheme, pctl, pax, recovery)
    if not svc:
        return {"ok": False, "error": "Enter a bus service."}
    return await tpr_report(svc, direction, dt, sc, stops, px, p, rc)


@app.get("/api/insight/tpr.xlsx")
async def api_in_tpr_xlsx(service: str = "", direction: int = 0, day_type: str = "Weekday", scheme: str = "tpr", stops1: str = "", stops2: str = "",
                          pctl: str = "85", pax: str = "3", recovery: str = "7"):
    """Excel in the operator TPR layout: one TPR sheet + one graph sheet per direction. direction=0 exports both."""
    svc, dt, sc, px, p, rc = tpr_args(service, day_type, scheme, pctl, pax, recovery)
    if not svc:
        return JSONResponse({"error": "Enter a bus service."}, status_code=400)
    st = await static()
    dirs = [direction] if direction in (1, 2) else (st["dirs"].get(svc) or [1])
    reps = []
    for d in dirs:
        r = await tpr_report(svc, d, dt, sc, stops1 if d == 1 else stops2, px, p, rc, wait_pv=True)
        if r.get("ok"):
            reps.append(r)
    if not reps:
        return JSONResponse({"error": f"Service {svc} was not found in LTA Bus Routes."}, status_code=404)
    raw = await asyncio.to_thread(tpr_excel.workbook, reps, tpr_engine.ASSUMPTIONS, {k: tpr_engine.SPEED_PROFILE[k] for k in (dt,)})
    fn = f"Svc{svc}_TPR_{dt}_{'hourly' if sc == 'hourly' else 'periods'}_{now_sgt().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(raw, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# ----------------------------------------------------------------------------- V13.0 Halfway Deployment Planner (proactive: /planner)
@app.get("/planner")
async def planner_page():
    """V16.0: the standalone Off-service Route Planner is retired. Off-service routing now appears in context inside the Halfway Planner
    and the Recovery Decision Engine; its API (/api/planner/*, /api/hplan/route) is unchanged. Old bookmarks land on the Halfway Planner."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/halfway", status_code=307)


async def pl_origin(frm, stops):
    """where the bus is now: the first stop, any stop code, or 'lat,lon'."""
    f = (frm or "").strip()
    if not f or f.lower() in ("first", "start", "terminal"):
        return (stops[0]["lat"], stops[0]["lon"]), f"{stops[0]['name']} ({stops[0]['code']})", None
    m = re.match(r"^\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*$", f)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not in_sg(lat, lon):
            return None, None, "That location is outside Singapore."
        return (lat, lon), f"map location {lat:.4f}, {lon:.4f}", None
    code = re.sub(r"\D", "", f)
    if len(code) == 5:
        on = next((x for x in stops if x["code"] == code), None)
        if on:
            return (on["lat"], on["lon"]), f"{on['name']} ({code})", None
        st = await static()
        sp = (st["stops"] or {}).get(code)
        if sp:
            return (sp["lat"], sp["lon"]), f"{sp['name']} ({code})", None
    return None, None, "Enter the first stop, a 5-digit bus stop code, or a map location."


async def os_routes(origin, dest, svc, d, stop_code, bus, dims, service_line, leave, planned_start=None, prep=None):
    """road routes from `origin` to one halfway stop, with traffic, restrictions and suitability. Used by the planner page."""
    P = dict(offservice.PARAMS)
    if prep is not None:
        P["prep_min"] = prep
    alt = await os_osrm([origin, dest], alternatives=3)
    routes = [dict(r, label=f"Road route {i + 1}", kind="osrm") for i, r in enumerate(alt["routes"])]
    if not routes:
        return None, alt.get("error") or "routing service unavailable"
    uniq = []
    for r in routes:
        if any(abs(r["km"] - u["km"]) < 0.15 and abs(r["osrm_min"] - u["osrm_min"]) < 0.6 for u in uniq):
            continue
        uniq.append(r)
    routes = uniq[:3]
    bands = await bands_state()
    idx = bands.get("idx") if isinstance(bands, dict) else None
    try:
        rw, _ = await tr_roadworks(now_sgt().timestamp())
    except Exception:
        rw = []
    try:
        inc = (await api_incidents("", 1)).get("incidents", [])
    except Exception:
        inc = []
    osms = await asyncio.gather(*[os_overpass(r["line"]) for r in routes])
    for r, osm in zip(routes, osms):
        r["groups"] = offservice.group_roads(r["steps"])
        runs, tinfo = color_route(r["line"], idx) if idx else ([], {"km": {}, "driveMin": None, "known": False})
        known = sum(v for kk, v in (tinfo.get("km") or {}).items() if kk != "none")
        use_band = bool(idx) and tinfo.get("known") and known >= P["band_min_share"] * max(r["km"], 0.01)
        r["time_min"] = tinfo["driveMin"] if use_band else r["osrm_min"] * P["bus_time_factor"]
        r["time_src"] = "live speed bands" if use_band else f"routing time x {P['bus_time_factor']:g} (no live speed data)"
        r["traffic"] = {"km": {kk: round(v, 2) for kk, v in (tinfo.get("km") or {}).items()}, "runs": [{"b": x["b"], "road": x["road"], "pts": offservice.simplify(x["pts"], 40)} for x in runs][:120]}
        r["overlap"] = os_overlap(r["line"], service_line, P["service_overlap_m"]) if service_line else 0.0
        r["signature"] = offservice.signature(stop_code, r["groups"])
        r["verified"] = os_verified(svc, d, stop_code, bus, r["signature"])
        r["findings"] = offservice.findings(r, osm, bus, dims, rw, inc, P)
        r["status"], r["status_text"] = offservice.suitability(r["findings"], bus, r["verified"])
        r["osm_ok"] = bool(osm and osm.get("ok"))
    opts, rec, fastest, shortest = offservice.build_options(routes, {"leave": leave, "planned_start": planned_start}, P)
    simplest, why_simple = offservice.pick_simple(opts, P)               # V16.28: expressways, fewer turns (the Halfway Planner asks for it)
    return {"options": opts, "recommended": rec, "fastest": fastest, "shortest": shortest, "simplest": simplest, "why_simple": why_simple,
            "roadworks": rw, "incidents": inc, "P": P}, None


@app.get("/api/planner/setup")
async def api_pl_setup(service: str = "", direction: int = 1):
    svc = service.strip().upper()
    if not svc:
        st = await static()
        return {"services": sorted({k[0] for k in st["routes"]})[:900]}
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"error": g["error"]}
    st = await static()
    dirs = []
    for d_ in (1, 2):
        ds = route_stops(st, svc, d_)
        if ds:
            dirs.append({"direction": d_, "label": f"{'UP' if d_ == 1 else 'DOWN'} \u2014 {ds[0]['name']} \u2192 {ds[-1]['name']}", "n": len(ds)})
    stops, ss, tau = g["stops"], g["prep"]["stop_s"], g["tau"]
    now = g["now"]
    return {"service": svc, "direction": direction, "directions": dirs, "sched_hw": g["H"], "hw_src": g["H_src"], "traffic_ok": g["traffic_ok"],
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(tau[-1], 1), "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": len(stops)},
            "stops": [{"code": s_["code"], "name": s_["name"], "seq": s_["seq"], "j": i, "km": round(ss[i], 2), "tau": round(tau[i], 1)} for i, s_ in enumerate(stops)],
            "now": now.strftime("%H:%M"), "ref": ho_hhmm(((now.hour * 60 + now.minute) // 5 + 1) * 5), "updated": now.isoformat(timespec="seconds")}


@app.get("/api/planner/search")
async def api_pl_search(service: str = "", direction: int = 1, frm: str = "", important: str = "", max_min: str = "", max_km: str = "", min_gain: str = "",
                        bus: str = "dd", hw: str = "", ref: str = "", late: str = "30", ready: str = "", limit: int = 40):
    """Every bus stop on the direction is tested as a halfway start: road time and distance from where the bus is now (one routing request),
    stops omitted / remaining, important stops kept, and the headway the deployment would earn. Ranked - never by distance alone."""
    svc = service.strip().upper()
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"ok": False, "error": g["error"]}
    stops, ss, tau, now = g["stops"], g["prep"]["stop_s"], g["tau"], g["now"]
    origin, origin_label, err = await pl_origin(frm, stops)
    if err:
        return {"ok": False, "error": err}
    P = {**halfway.PARAMS, **HO["params"]}
    bus = bus if bus in offservice.BUS_TYPES else "dd"
    def fnum(v, dflt=None):
        try:
            return float(str(v).strip()) if str(v).strip() else dflt
        except ValueError:
            return dflt
    H = fnum(hw, g["H"]) or 0.0
    if H <= 0:
        return {"ok": False, "error": "Scheduled headway unknown for this service - enter it."}
    L = fnum(late, 30.0) or 0.0
    t0 = ho_parse_hhmm(ref) if ref.strip() else ((now.hour * 60 + now.minute) // 5 + 1) * 5
    rdy = ho_parse_hhmm(ready) if ready.strip() else now.hour * 60 + now.minute
    if t0 is None or rdy is None:
        return {"ok": False, "error": "Times must be like 14:05."}
    mx_min, mx_km, mn_gain = fnum(max_min), fnum(max_km), fnum(min_gain, 0.0) or 0.0
    imp = [c for c in re.split(r"[,\s]+", important.strip()) if c]
    imp_j = {c: next((i for i, s_ in enumerate(stops) if s_["code"] == c), None) for c in imp}
    n = len(stops)
    idxs = [j for j in range(1, n - 1) if n - j >= 3]
    offs, off_err = await os_table(origin, [(stops[j]["lat"], stops[j]["lon"]) for j in idxs])
    prep = float(P.get("prep_min", offservice.PARAMS["prep_min"]))
    cands, skipped = [], {"time": 0, "km": 0, "gain": 0, "no_route": 0, "no_benefit": 0}
    for i, j in enumerate(idxs):
        om = offs.get(i)
        if not om:
            skipped["no_route"] += 1
            continue
        off_min = om["min"] * offservice.PARAMS["bus_time_factor"]
        off_km = om["km"] if om.get("km") is not None else max(0.0, ss[j])
        ins = max(rdy + off_min + prep, t0 + tau[j] - 5.0)
        q = offservice.quick_gain(H, t0 + tau[j], L, ins)
        missed = [c for c, jj in imp_j.items() if jj is not None and jj < j]
        c = {"j": j, "code": stops[j]["code"], "name": stops[j]["name"], "seq": stops[j]["seq"], "lat": stops[j]["lat"], "lon": stops[j]["lon"],
             "off_min": round(off_min, 1), "off_km": round(off_km, 2), "route_km_from_start": round(ss[j], 2), "arrive": round(rdy + off_min, 1),
             "arrive_clock": offservice.hm(rdy + off_min), "insert": round(ins, 1), "insert_clock": offservice.hm(ins), "sched_clock": offservice.hm(t0 + tau[j]),
             "stops_total": n, "stops_omitted": j, "stops_remaining": n - j, "km_lost": round(ss[j], 2),
             "important_total": len(imp), "important_served": len(imp) - len(missed), "important_missed": len(missed),
             "missed": [{"code": c_, "name": next((x["name"] for x in stops if x["code"] == c_), c_)} for c_ in missed],
             "gain": q["gain"], "hw": q, "after_next": q["after_next"], "dev_half": q["dev_half"]}
        if mx_min is not None and off_min > mx_min + 1e-6:
            skipped["time"] += 1
            continue
        if mx_km is not None and off_km > mx_km + 1e-6:
            skipped["km"] += 1
            continue
        if q["gain"] <= 1e-6 or q["after_next"]:
            skipped["no_benefit"] += 1                 # the bus would reach this stop too late to close the gap: not a halfway candidate at all
            continue
        if q["gain"] < mn_gain - 1e-6:
            skipped["gain"] += 1
            continue
        cands.append(c)
    offservice.rank_candidates(cands)
    for c in cands:
        c["band"] = offservice.band_of(c, mn_gain, H)
    top = cands[:limit]
    return {"ok": True, "service": svc, "direction": direction, "bus": bus, "bus_label": offservice.BUS_TYPES[bus], "origin": {"label": origin_label, "lat": origin[0], "lon": origin[1]},
            "H": H, "late": L, "ref": ho_hhmm(t0), "ready": ho_hhmm(rdy), "prep_min": prep, "now": now.strftime("%H:%M"),
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(tau[-1], 1), "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": n},
            "candidates": top, "n_tested": len(idxs), "n_feasible": len(cands), "skipped": skipped, "best": offservice.best_reasons(cands, H, mn_gain),
            "important": [{"code": c_, "name": next((x["name"] for x in stops if x["code"] == c_), c_), "j": imp_j[c_]} for c_ in imp],
            "trade_off": offservice.trade_off_text(cands), "line": ho_simplify(g["line"], 400),
            "screening": "road distance and time from one routing request" if offs else f"estimate only ({off_err or 'routing unavailable'})",
            "note": "Headway figures assume the buses before and after the delayed trip run on time; the full simulation is on the Halfway Optimiser page.",
            "updated": now.isoformat(timespec="seconds")}


@app.get("/api/planner/route")
async def api_pl_route(service: str = "", direction: int = 1, frm: str = "", stop: str = "", bus: str = "dd", vh: str = "", vw: str = "", vt: str = "",
                       hw: str = "", ref: str = "", late: str = "30", ready: str = "", important: str = ""):
    """Off-service road routes to ONE chosen halfway stop: alternatives, traffic, restrictions, suitability, timeline and the headway effect."""
    svc = service.strip().upper()
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"ok": False, "error": g["error"]}
    stops, ss, tau, now = g["stops"], g["prep"]["stop_s"], g["tau"], g["now"]
    j = next((i for i, s_ in enumerate(stops) if s_["code"] == re.sub(r"\D", "", stop)), None)
    if j is None or j == 0:
        return {"ok": False, "error": "Choose a halfway stop on this route."}
    origin, origin_label, err = await pl_origin(frm, stops)
    if err:
        return {"ok": False, "error": err}
    bus = bus if bus in offservice.BUS_TYPES else "dd"
    dims = {}
    for key_, v_ in (("height_m", vh), ("width_m", vw), ("weight_t", vt)):
        try:
            dims[key_] = float(v_) if str(v_).strip() else None
        except ValueError:
            dims[key_] = None
    P = {**halfway.PARAMS, **HO["params"]}
    prep = float(P.get("prep_min", offservice.PARAMS["prep_min"]))
    try:
        H = float(hw) if hw.strip() else (g["H"] or 0.0)
        L = float(late) if late.strip() else 30.0
    except ValueError:
        return {"ok": False, "error": "Headway and lateness must be numbers."}
    t0 = ho_parse_hhmm(ref) if ref.strip() else ((now.hour * 60 + now.minute) // 5 + 1) * 5
    rdy = ho_parse_hhmm(ready) if ready.strip() else now.hour * 60 + now.minute
    cum = g["prep"]["cum"]
    R, rerr = await os_routes(origin, (stops[j]["lat"], stops[j]["lon"]), svc, direction, stops[j]["code"], bus, dims, g["line"], rdy,
                              planned_start=(t0 + tau[j] - 5.0 if H else None), prep=prep)
    if R is None:
        return {"ok": False, "error": f"No road route could be calculated ({rerr})."}
    imp = [c for c in re.split(r"[,\s]+", important.strip()) if c]
    out = []
    for x in R["options"]:
        ins = max(rdy + x["time_min"] + prep, t0 + tau[j] - 5.0)
        q = offservice.quick_gain(H, t0 + tau[j], L, ins) if H else None
        out.append({"idx": x["idx"], "label": x["label"], "tags": x["tags"], "km": round(x["km"], 2), "time_min": round(x["time_min"], 1), "time_src": x["time_src"],
                    "arrive": round(rdy + x["time_min"], 1), "arrive_clock": offservice.hm(rdy + x["time_min"]), "insert_clock": offservice.hm(ins), "insert": round(ins, 1),
                    "status": x["status"], "status_label": offservice.STATUS_TXT[x["status"]], "status_text": x["status_text"], "findings": x["findings"], "osm_ok": x["osm_ok"],
                    "verified": x["verified"], "signature": x["signature"], "overlap": round(x["overlap"], 2), "n_turns": x["n_turns"], "n_sharp": x["n_sharp"],
                    "congested_km": round(x["congested_km"], 2), "traffic": x["traffic"], "line": offservice.simplify(x["line"], 250), "roads": [q_["road"] for q_ in x["groups"]],
                    "groups": [{"road": q_["road"], "km": round(q_["km"], 2), "min": round(q_["min"] * (x["time_min"] / (sum(z["min"] for z in x["groups"]) or 1.0)), 1),
                                "line": offservice.simplify(q_["line"], 60)} for q_ in x["groups"]],
                    "timeline": offservice.timeline(x, rdy, prep, ins, ins + (tau[-1] - tau[j]), origin_label, stops[j]["name"], stops[-1]["name"]),
                    "hw": q})
    served = [s_ for i_, s_ in enumerate(stops) if i_ >= j]
    return {"ok": True, "service": svc, "direction": direction, "bus": bus, "bus_label": offservice.BUS_TYPES[bus], "dims": dims,
            "stop": {"j": j, "code": stops[j]["code"], "name": stops[j]["name"], "lat": stops[j]["lat"], "lon": stops[j]["lon"], "km_from_start": round(ss[j], 2)},
            "origin": {"label": origin_label, "lat": origin[0], "lon": origin[1]}, "last": {"name": stops[-1]["name"], "lat": stops[-1]["lat"], "lon": stops[-1]["lon"]},
            "options": out, "recommended": R["recommended"], "fastest": R["fastest"], "shortest": R["shortest"], "ready": ho_hhmm(rdy), "ref": ho_hhmm(t0), "H": H, "late": L, "prep_min": prep,
            "stops_total": len(stops), "stops_omitted": j, "stops_remaining": len(stops) - j, "km_lost": round(ss[j], 2), "km_operated": round(g["prep"]["km"] - ss[j], 2),
            "important": [{"code": c_, "name": next((x_["name"] for x_ in stops if x_["code"] == c_), c_), "served": any(x_["code"] == c_ for x_ in served),
                           "lat": next((x_["lat"] for x_ in stops if x_["code"] == c_), None), "lon": next((x_["lon"] for x_ in stops if x_["code"] == c_), None)} for c_ in imp],
            "service_line": ho_simplify(g["line"], 500), "skipped_line": ho_simplify(ho_cut(g["line"], cum, 0.0, ss[j]), 200),
            "recovered_line": ho_simplify(ho_cut(g["line"], cum, ss[j], cum[-1]), 250),
            "stop_dots": [{"code": s_["code"], "name": s_["name"], "served": i_ >= j} for i_, s_ in enumerate(stops)],
            "roadworks": [x for x in R["roadworks"] if x.get("lat") is not None and any(offservice.dist_to_line_m((x["lat"], x["lon"]), y["line"]) <= 300 for y in R["options"])][:20],
            "incidents": [x for x in R["incidents"] if any(offservice.dist_to_line_m((x["lat"], x["lon"]), y["line"]) <= 300 for y in R["options"])][:20],
            "why_route": offservice.why_route(R["options"], R["recommended"], R["fastest"], R["shortest"], None, stops[j]["name"]),
            "updated": now.isoformat(timespec="seconds")}


# =========================================================================== V14.0 Halfway Planner (live snapshot + simulation; the engine is hplan.py)
import hplan
HP_SNAP = {}                      # snapshot id -> the live buses and route model at one moment (simulations reuse it, so results are consistent)
HP_SNAP_TTL = 900


def hp_prune():
    now = time.time()
    for k in [k for k, v in HP_SNAP.items() if now - v["created"] > HP_SNAP_TTL]:
        HP_SNAP.pop(k, None)


def hp_tt(g, P):
    tm, ss = g["tm"], g["prep"]["stop_s"]
    if tm is not None:
        return lambda a, b: tm.t(a, b) if b > a else 0.0
    kmh = float(P["fallback_kmh"])
    return lambda a, b: max(0.0, b - a) / kmh * 60.0


@app.get("/api/hplan/snapshot")
async def api_hp_snapshot(service: str = "", direction: int = 1):
    """LIVE: route, stops and the live DataMall buses projected on the route at this moment. Nothing here is simulated."""
    svc = service.strip().upper()
    if not svc:
        return {"ok": False, "error": "Enter a service number."}
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"ok": False, "error": g["error"]}
    st = await static()
    dirs = [d_ for d_ in (1, 2) if route_stops(st, svc, d_)]
    P = {**halfway.PARAMS, **HO["params"]}
    buses, off_route, bus_err = await hp_live_buses(svc, direction, g, P)
    stops, ss, line, now = g["stops"], g["prep"]["stop_s"], g["line"], g["now"]
    now_min = now.hour * 60 + now.minute + now.second / 60.0
    opp = None                                                      # V14.3: the opposite direction, for the cross-direction circulation
    od = next((d_ for d_ in dirs if d_ != direction), None)
    if od is not None:
        g2 = await ho_route(svc, od)
        if not g2.get("error"):
            b2, _, _ = await hp_live_buses(svc, od, g2, P)
            opp = {"g": g2, "buses": b2, "dir": od}
    sid = hashlib.sha1(f"{svc}|{direction}|{time.time()}".encode()).hexdigest()[:12]
    hp_prune()
    HP_SNAP[sid] = {"created": time.time(), "svc": svc, "d": direction, "g": g, "buses": buses, "now_min": now_min, "P": P, "opp": opp,
                    "dirs": dirs, "now_s": now.strftime("%H:%M:%S")}
    cands, unresolved, cinfo = ho_candidates(svc, direction, stops, "", "auto", g["prep"], P)
    strip_b = lambda bs: [{k: v for k, v in b.items() if k not in ("s", "offset")} for b in bs]
    return {"ok": True, "snap": sid, "service": svc, "direction": direction, "directions": dirs, "now": now.strftime("%H:%M:%S"), "now_min": round(now_min, 2),
            "H": g["H"], "H_src": g["H_src"], "traffic_ok": g["traffic_ok"],
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(g["tau"][-1], 1), "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": len(stops)},
            "stops": [{"j": i, "code": s_["code"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "km": round(ss[i], 2)} for i, s_ in enumerate(stops)],
            "line": ho_simplify(line, 500), "buses": strip_b(buses), "bus_error": bus_err, "off_route": off_route,
            "opp": ({"direction": opp["dir"], "line": ho_simplify(opp["g"]["line"], 400), "buses": strip_b(opp["buses"]), "km": round(opp["g"]["prep"]["km"], 2),
                     "first": opp["g"]["stops"][0]["name"], "last": opp["g"]["stops"][-1]["name"], "H": opp["g"]["H"], "stops": hp_stops(opp["g"])} if opp else None),
            "candidates": {"scope": cinfo.get("scope_used"), "approved": cinfo.get("approved"), "tested": len(cands), "unresolved": unresolved},
            "source": "LTA DataMall Bus Arrival (live) \u00b7 route running times from LTA speed bands" if g["traffic_ok"] else "LTA DataMall Bus Arrival (live) \u00b7 running times at the fallback speed (no speed bands)"}


@app.get("/api/hplan/testsnap")
async def api_hp_testsnap(service: str = "", direction: int = 1, time_: str = Query("", alias="time"), headway: str = "10", buses: str = "",
                          offset: str = "", stop_min: str = "2", gap: str = "", gap_at: str = "start", layover: str = ""):
    """V15.2 TEST MODE: a synthetic, evenly spaced fleet on the service's real route (both directions) for a time you choose - for testing
    when no buses are running (e.g. after midnight). Same snapshot shape as the live one, so planning and road routing work unchanged."""
    svc = service.strip().upper()
    if not svc:
        return {"ok": False, "error": "Enter a service number."}
    try:
        H = max(2.0, min(60.0, float(headway)))
        nb = max(2, min(40, int(float(buses)))) if str(buses).strip() else 40          # empty = fill the whole route at this headway
        sm = max(0.5, min(6.0, float(stop_min or 2)))
        off = float(offset) if str(offset).strip() else H / 2.0
        LG = max(H, min(120.0, float(gap))) if str(gap).strip() else None
    except ValueError:
        return {"ok": False, "error": "Headway, number of buses and offset must be numbers."}
    t0 = ho_parse_hhmm(time_) if str(time_).strip() else None
    if t0 is None:
        n_ = now_sgt()
        t0 = n_.hour * 60 + n_.minute
    g0 = await ho_route(svc, direction)
    if g0.get("error"):
        return {"ok": False, "error": g0["error"]}
    st = await static()
    dirs = [d_ for d_ in (1, 2) if route_stops(st, svc, d_)]

    # V15.7: ONE continuous stream of buses round the loop  D1 -> layover -> D2 -> layover, every H minutes, including the buses
    # waiting in layover at a terminal. A partial fleet is one continuous block (no empty stretch inside the scenario):
    # around the D1-end / D2-start interchange for a late bus, around the start of the direction for the OS long-headway demo.
    g = {**g0, "H": H, "H_src": "test input"}
    P = {**halfway.PARAMS, **HO["params"]}
    od = next((d_ for d_ in dirs if d_ != direction), None)
    g2 = None
    if od is not None:
        g2x = await ho_route(svc, od)
        if not g2x.get("error"):
            g2 = {**g2x, "H": H, "H_src": "test input"}
    try:
        lay = max(0.0, min(40.0, float(layover))) if str(layover).strip() else 10.0
    except ValueError:
        lay = 10.0
    runT = sm * (len(g["stops"]) - 1)
    runO = sm * (len(g2["stops"]) - 1) if g2 else 0.0
    C = runT + lay + ((runO + lay) if g2 else 0.0)
    fit_total = max(2, int(C // H))
    total = min(fit_total, nb * (2 if g2 else 1)) if str(buses).strip() else fit_total
    tc = (C - lay / 2.0) if LG else (runT + lay / 2.0)               # centre of the block
    taus = sorted(tc + (k - (total - 1) / 2.0) * H + 0.37 for k in range(total))
    if LG:                                                            # one prolonged headway between the 2nd and 3rd bus of the direction
        t_sec = [t for t in taus if C <= t < C + runT] or [t for t in taus if 0 <= t < runT]
        if len(t_sec) >= 3:
            cut = t_sec[min(3, len(t_sec) - 2)] if gap_at != "middle" else t_sec[max(1, len(t_sec) // 2 - 1)]
            shifted = [t - (LG - H) for t in taus if t <= cut + 1e-9]
            kept = [t for t in taus if t > cut + 1e-9]
            # a gap means missing buses: drop shifted buses that would now sit on top of the others round the loop
            circ = lambda a_, b_: min(abs(a_ - b_) % C, C - abs(a_ - b_) % C)
            taus = sorted(kept + [t for t in shifted if all(circ(t, k_) >= H / 2.0 for k_ in kept)])

    def mkbus(gg, el=None, wait=None):
        stops, ss = gg["stops"], gg["prep"]["stop_s"]
        n = len(stops)
        if wait is not None:                                          # waiting in layover at the first stop
            st0 = stops[0]
            return {"s": ss[0], "lat": st0["lat"], "lon": st0["lon"], "km": 0.0, "near": {"code": st0["code"], "name": st0["name"]}, "near_name": st0["name"],
                    "next": {"code": st0["code"], "name": st0["name"], "eta_min": round(wait, 1), "clock": hplan.hhmm(t0 + wait)}, "wait": round(wait, 2),
                    "offset": 0.0, "load": None, "type": None, "monitored": 0, "test": True}
        p = el / sm
        i = min(n - 2, int(p)); f = p - i
        s_km = ss[i] + f * (ss[i + 1] - ss[i])
        lat = stops[i]["lat"] + f * (stops[i + 1]["lat"] - stops[i]["lat"]); lon = stops[i]["lon"] + f * (stops[i + 1]["lon"] - stops[i]["lon"])
        near = stops[i + 1] if f >= 0.5 else stops[i]
        nxt = stops[i + 1]
        return {"s": s_km, "lat": lat, "lon": lon, "km": round(s_km, 2), "near": {"code": near["code"], "name": near["name"]}, "near_name": near["name"],
                "next": {"code": nxt["code"], "name": nxt["name"], "eta_min": round((1 - f) * sm, 1), "clock": hplan.hhmm(t0 + (1 - f) * sm)},
                "offset": 0.0, "load": None, "type": None, "monitored": 0, "test": True}

    tb, ob = [], []
    for t in taus:
        tm = t % C
        if tm < runT:
            tb.append(mkbus(g, el=tm))
        elif g2 and tm < runT + lay:
            ob.append(mkbus(g2, wait=runT + lay - tm))
        elif g2 and tm < runT + lay + runO:
            ob.append(mkbus(g2, el=tm - runT - lay))
        else:
            tb.append(mkbus(g, wait=C - tm))
    for lst in (tb, ob):
        lst.sort(key=lambda b: (b["s"], -b.get("wait", 0.0)))
        for i, b in enumerate(lst, 1):
            b["id"] = i
    fit = int(round(fit_total / (2 if g2 else 1)))
    opp = {"g": g2, "buses": ob, "dir": od} if g2 else None
    sid = hashlib.sha1(f"test|{svc}|{direction}|{time.time()}".encode()).hexdigest()[:12]
    hp_prune()
    HP_SNAP[sid] = {"created": time.time(), "svc": svc, "d": direction, "g": g, "buses": tb, "now_min": float(t0), "P": P, "opp": opp, "test": True,
                    "partial": total < fit_total, "test_gap": LG, "dirs": dirs, "now_s": hplan.hhmm(t0) + ":00"}
    stops, ss, line = g["stops"], g["prep"]["stop_s"], g["line"]
    strip_b = lambda bs: [{k: v for k, v in b.items() if k not in ("s", "offset")} for b in bs]
    return {"ok": True, "test": True, "snap": sid, "service": svc, "direction": direction, "directions": dirs, "now": hplan.hhmm(t0) + ":00", "now_min": float(t0),
            "H": H, "H_src": "test input", "traffic_ok": g0.get("traffic_ok"),
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(sm * (len(stops) - 1), 1), "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": len(stops)},
            "stops": [{"j": i, "code": s_["code"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "km": round(ss[i], 2)} for i, s_ in enumerate(stops)],
            "line": ho_simplify(line, 500), "buses": strip_b(tb), "bus_error": None, "off_route": 0,
            "opp": ({"direction": opp["dir"], "line": ho_simplify(opp["g"]["line"], 400), "buses": strip_b(opp["buses"]), "km": round(opp["g"]["prep"]["km"], 2),
                     "first": opp["g"]["stops"][0]["name"], "last": opp["g"]["stops"][-1]["name"], "H": H, "stops": hp_stops(opp["g"])} if opp else None),
            "test_info": {"time": hplan.hhmm(t0), "headway": H, "buses": nb, "fit": fit, "placed": len(tb), "placed_opp": len(opp["buses"]) if opp else 0,
                          "offset": off, "stop_min": sm, "gap": LG, "gap_at": gap_at if LG else None, "layover": lay,
                          "waiting": sum(1 for b in tb + ob if b.get("wait") is not None)},
            "source": "TEST MODE \u00b7 synthetic evenly spaced buses on the real route (not live)"}


def hp_stops(g):
    ss = g["prep"]["stop_s"]
    return [{"j": i, "code": s_["code"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "km": round(ss[i], 2)} for i, s_ in enumerate(g["stops"])]


def hp_edit_fleet(S, d_main, edits):
    """V16.27: OCC corrects the fleet the planner sees (DataMall can miss a bus or show one that is not there): remove a bus, move it to
    another stop, add a missing one at a stop. Returns a NEW snapshot (the live / test one is never changed) whose main direction is
    d_main (the direction of the bus that goes halfway). Edits: {remove:[{dir,id}], move:[{dir,id,stop|j}], add:[{dir,stop|j}]}."""
    lanes = {S["d"]: (S["g"], copy.deepcopy(S["buses"]))}
    if S.get("opp"):
        lanes[S["opp"]["dir"]] = (S["opp"]["g"], copy.deepcopy(S["opp"]["buses"]))
    if d_main not in lanes:
        raise ValueError(f"Direction {d_main} is not part of this service.")

    def at_stop(g, e):
        stops, ss = g["stops"], g["prep"]["stop_s"]
        j = e.get("j")
        if not (isinstance(j, int) and 0 <= j < len(stops) and (not e.get("stop") or stops[j]["code"] == str(e["stop"]))):
            j = next((i for i, x in enumerate(stops) if x["code"] == str(e.get("stop") or "").strip()), None)
        if j is None:
            raise ValueError(f"Bus stop {e.get('stop') or e.get('j')} is not on this direction.")
        st0 = stops[j]
        return j, {"s": ss[j], "lat": st0["lat"], "lon": st0["lon"], "km": round(ss[j], 2), "near": {"code": st0["code"], "name": st0["name"]}, "near_name": st0["name"],
                   "next": None, "offset": 0.0, "load": None, "type": None, "monitored": 0}
    def lane(e):
        try:
            return lanes[int(e.get("dir") or 0)]
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Direction {e.get('dir')} is not part of this service.")
    for e in (edits.get("remove") or [])[:60]:
        g, bs = lane(e)
        bs[:] = [b for b in bs if str(b["id"]) != str(e.get("id"))]
    for e in (edits.get("move") or [])[:60]:
        g, bs = lane(e)
        b = next((x for x in bs if str(x["id"]) == str(e.get("id"))), None)
        if b is None:
            continue
        j, pos = at_stop(g, e)
        b.pop("wait", None)
        b.update(pos, moved=True)
    for e in (edits.get("add") or [])[:30]:
        g, bs = lane(e)
        j, pos = at_stop(g, e)
        nid = max([int(x["id"]) for x in bs if str(x["id"]).isdigit()] + [0]) + 1
        bs.append(dict(pos, id=nid, added=True))
    for g, bs in lanes.values():
        bs.sort(key=lambda b: (b["s"], -float(b.get("wait") or 0.0)))
    g, bs = lanes[d_main]
    od = next((d_ for d_ in lanes if d_ != d_main), None)
    return {**S, "created": time.time(), "d": d_main, "g": g, "buses": bs, "opp": ({"g": lanes[od][0], "buses": lanes[od][1], "dir": od} if od is not None else None),
            "edited": True}


@app.post("/api/hplan/edit")
async def api_hp_edit(request: Request):
    """V16.27: the live / test fleet with OCC's corrections (add / remove / move buses) and the direction of the bus that goes halfway
    -> a new snapshot id (same shape as /api/hplan/snapshot) for planning. The snapshot it starts from is never changed."""
    try:
        body = json.loads((await request.body()) or b"{}")
    except ValueError:
        return {"ok": False, "error": "Bad request."}
    base = HP_SNAP.get(str(body.get("snap") or ""))
    if not base:
        return {"ok": False, "error": "The live snapshot has expired. Load the buses again.", "expired": True}
    try:
        d_main = int(body.get("dir") or base["d"])
        S = hp_edit_fleet(base, d_main, body.get("edits") or {})
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}
    sid = hashlib.sha1(f"edit|{body.get('snap')}|{time.time()}".encode()).hexdigest()[:12]
    hp_prune()
    HP_SNAP[sid] = S
    g, opp = S["g"], S.get("opp")
    stops = g["stops"]
    strip_b = lambda bs: [{k: v for k, v in b.items() if k not in ("s", "offset")} for b in bs]
    return {"ok": True, "snap": sid, "base": body.get("snap"), "edited": True, "test": bool(S.get("test")), "service": S["svc"], "direction": S["d"],
            "directions": S.get("dirs") or sorted({S["d"]} | ({opp["dir"]} if opp else set())), "now": S.get("now_s") or hplan.hhmm(S["now_min"]) + ":00",
            "now_min": round(S["now_min"], 2), "H": g["H"], "H_src": g["H_src"], "traffic_ok": g.get("traffic_ok"),
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(g["tau"][-1], 1) if g.get("tau") else None, "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": len(stops)},
            "stops": hp_stops(g), "line": ho_simplify(g["line"], 500), "buses": strip_b(S["buses"]), "bus_error": None, "off_route": 0,
            "opp": ({"direction": opp["dir"], "line": ho_simplify(opp["g"]["line"], 400), "buses": strip_b(opp["buses"]), "km": round(opp["g"]["prep"]["km"], 2),
                     "first": opp["g"]["stops"][0]["name"], "last": opp["g"]["stops"][-1]["name"], "H": opp["g"]["H"], "stops": hp_stops(opp["g"])} if opp else None)}


async def hp_live_buses(svc, direction, g, P):
    """LIVE buses of one direction, projected on its route and calibrated to DataMall's own next-stop arrival."""
    bj = await api_buses(svc, direction)
    stops, ss, line, cum, now = g["stops"], g["prep"]["stop_s"], g["line"], g["prep"]["cum"], g["now"]
    tt = hp_tt(g, P)
    now_min = now.hour * 60 + now.minute + now.second / 60.0
    buses, off_route = [], 0
    for b in bj.get("buses", []):
        if b.get("lat") is None:
            continue
        s, miss = headway.project(line, cum, b["lat"], b["lon"])
        if miss > 0.35:
            off_route += 1
            continue
        off, calib = 0.0, None                                       # calibrate the running-time model to DataMall's own predicted arrival
        etas = b.get("etasf") or b.get("etas") or {}
        ahead = sorted((int(i), float(e)) for i, e in etas.items() if e is not None and float(e) >= 0 and ss[int(i)] > s + 0.05)
        if ahead:
            i0, e0 = ahead[0]
            off = max(-5.0, min(15.0, e0 - tt(s, ss[i0])))
            calib = {"code": stops[i0]["code"], "name": stops[i0]["name"], "eta_min": round(e0, 1), "clock": hplan.hhmm(now_min + e0)}
        near = b.get("near") or {}
        buses.append({"id": b["id"], "lat": b["lat"], "lon": b["lon"], "s": s, "offset": off, "km": round(s, 2), "near": near, "near_name": near.get("name") if isinstance(near, dict) else None,
                      "load": b.get("load"), "type": b.get("type"), "monitored": b.get("monitored"), "next": calib})
    return buses, off_route, bj.get("error")


async def hp_origin(snap, mode, bus, os_from):
    """where the moving bus starts: the live position of the delayed bus, or the OS bus's location."""
    stops = snap["g"]["stops"]
    if mode == "late":
        b = next((x for x in snap["buses"] if str(x["id"]) == str(bus)), None)
        if not b:
            return None, None, "Select one of the live buses."
        return (b["lat"], b["lon"]), f"Bus {b['id']} (live position)", None
    return await pl_origin(os_from, stops)


@app.get("/api/hplan/simulate")
async def api_hp_simulate(snap: str = "", mode: str = "late", bus: str = "", delay: str = "0", os_from: str = "", os_time: str = "", scope: str = "auto", max_reach: str = "",
                          balance: str = "", reg_max: str = "", min_skip: str = ""):
    """SIMULATION on top of one live snapshot: No action / Regulate / halfway re-entry (late) or No deployment / OS insertion (os), ranked by projected EWT."""
    S = HP_SNAP.get(snap)
    if not S:
        return {"ok": False, "error": "The live snapshot has expired. Refresh the live buses and run again.", "expired": True}
    mode = "os" if mode == "os" else "late"
    g, P = S["g"], S["P"]
    stops, ss = g["stops"], g["prep"]["stop_s"]
    try:
        D = float(delay or 0)
    except ValueError:
        return {"ok": False, "error": "Delay must be a number of minutes."}
    if mode == "late" and D <= 0:
        return {"ok": False, "error": "Inject a delay (minutes) for the selected bus."}
    if mode == "late":
        return await hp_simulate_next(S, snap, bus, D, scope, max_reach, min_skip)
    origin, origin_label, err = await hp_origin(S, mode, bus, os_from)
    if err:
        return {"ok": False, "error": err}
    t0 = S["now_min"]
    if mode == "os" and os_time.strip():
        t0p = ho_parse_hhmm(os_time)
        if t0p is None:
            return {"ok": False, "error": "OS available time must be like 14:15."}
        t0 = t0p + (1440 if t0p < S["now_min"] - 720 else 0)
        t0 = max(t0, S["now_min"])
    cands, _unres, cinfo = ho_candidates(S["svc"], S["d"], stops, "", scope if scope in ("auto", "approved", "all") else "auto", g["prep"], P)
    HP = dict(hplan.PARAMS)
    for val, key, lo, hi in ((max_reach, "max_reach_min", 5.0, 90.0), (balance, "balance_trips", 0, 12), (reg_max, "reg_bus_max", 0.0, 15.0), (min_skip, "os_min_skip_pct", 0.0, 60.0)):
        try:
            if str(val).strip():
                HP[key] = max(lo, min(hi, float(val)))
        except ValueError:
            pass
    HP["balance_trips"] = int(HP["balance_trips"])
    offs, off_err = await os_table(origin, [(c["lat"], c["lon"]) for c in cands])
    fac = float(offservice.PARAMS["bus_time_factor"])
    cl = []
    for i, c in enumerate(cands):
        om = offs.get(i)
        if om:
            cl.append({**c, "reach_min": om["min"] * fac, "reach_km": om.get("km"), "reach_src": f"road routing time x {fac:g}"})
        else:
            km = hplan.hav_km(origin, (c["lat"], c["lon"])) * HP["detour"]
            cl.append({**c, "reach_min": km / HP["offsvc_kmh"] * 60.0, "reach_km": km, "reach_src": "straight-line estimate (routing unavailable)"})
    ctx = {"stops": stops, "ss": ss, "tt": hp_tt(g, P), "H": g["H"], "H_src": g["H_src"], "now": S["now_min"], "buses": [dict(b) for b in S["buses"]], "mode": mode,
           "late": {"bus": int(bus) if str(bus).isdigit() else bus, "delay": D} if (bus and D > 0) else None,
           "os": {"t0": t0, "lat": origin[0], "lon": origin[1], "label": origin_label} if mode == "os" else None, "cands": cl, "P": HP}
    res = await asyncio.to_thread(hplan.simulate, ctx)
    res.update(origin={"label": origin_label, "lat": origin[0], "lon": origin[1]}, snap=snap, service=S["svc"], direction=S["d"],
               routing="road routing (OSRM) x bus factor" if offs else f"straight-line estimate ({off_err or 'routing unavailable'})",
               scope=cinfo.get("scope_used"), n_candidates=len(cands), os_time=hplan.hhmm(t0) if mode == "os" else None,
               labels={"live": "LTA DataMall observations (unchanged)", "sim": "Simulation layer: injected delay / virtual OS bus", "derived": "Predicted from the running-time model"})
    return res


async def hp_simulate_next(S, snap, bus, D, scope, max_reach, min_skip):
    """V14.4 RECOVER LATE DUTY: the late bus completes its trip; its NEXT trip (other direction) starts halfway, reached by real road
    from the interchange. Buses A / B ahead may leave the interchange a few minutes later. (V14.3 cross-direction search kept below.)"""
    g, P = S["g"], S["P"]
    if not str(bus).isdigit() or not any(b["id"] == int(bus) for b in S["buses"]):
        return {"ok": False, "error": "Select one of the live buses as the late duty."}
    HP = dict(hplan.PARAMS)
    for val, key, lo, hi in ((max_reach, "max_reach_min", 5.0, 90.0), (min_skip, "os_min_skip_pct", 0.0, 60.0)):
        try:
            if str(val).strip():
                HP[key] = max(lo, min(hi, float(val)))
        except ValueError:
            pass
    mk = lambda gg, bs, d_: {"dir": d_, "stops": gg["stops"], "ss": gg["prep"]["stop_s"], "tt": hp_tt(gg, P), "H": gg["H"], "H_src": gg["H_src"],
                             "buses": [{**b, "near": b.get("near_name")} for b in bs]}
    T = mk(g, S["buses"], S["d"])
    O = mk(S["opp"]["g"], S["opp"]["buses"], S["opp"]["dir"]) if S.get("opp") else None
    gn = S["opp"]["g"] if S.get("opp") else g                           # the direction of the late bus's NEXT trip
    dn = S["opp"]["dir"] if S.get("opp") else S["d"]
    cands, _u, cinfo = ho_candidates(S["svc"], dn, gn["stops"], "", scope if scope in ("auto", "approved", "all") else "auto", gn["prep"], P)
    ic = gn["stops"][0]
    offs, off_err = await os_table((ic["lat"], ic["lon"]), [(c["lat"], c["lon"]) for c in cands])
    fac = float(offservice.PARAMS["bus_time_factor"])
    reach = {}
    for i, c in enumerate(cands):
        om = offs.get(i)
        if om:
            reach[c["j"]] = (om["min"] * fac, om.get("km"), f"real-road routing time x {fac:g}")
        else:
            km = hplan.hav_km((ic["lat"], ic["lon"]), (c["lat"], c["lon"])) * HP["detour"]
            reach[c["j"]] = (km / HP["offsvc_kmh"] * 60.0, km, "straight-line estimate (road routing unavailable)")
    ctx = {"now": S["now_min"], "late": {"bus": int(bus), "delay": D}, "T": T, "O": O, "cands": cands, "P": HP}
    res = await asyncio.to_thread(hplan.simulate_next_trip, ctx, reach)
    res.update(snap=snap, service=S["svc"], direction=S["d"], scope=cinfo.get("scope_used"), n_candidates=len(cands),
               routing="real-road routing (OSRM) x bus factor" if offs else f"straight-line estimate ({off_err or 'road routing unavailable'})", origin=None,
               labels={"live": "LTA DataMall observations (unchanged)", "sim": "Simulation layer: injected delay", "derived": "Forecast from the running-time model"})
    return res


async def hp_simulate_cross(S, snap, bus, D, scope, max_reach, min_skip):
    """V14.3 RECOVER LATE DUTY: which opposite-direction (or next-trip) bus to deploy halfway, where and when - from the circulation of both directions."""
    g, P = S["g"], S["P"]
    if not str(bus).isdigit() or not any(b["id"] == int(bus) for b in S["buses"]):
        return {"ok": False, "error": "Select one of the live buses as the late duty."}
    HP = dict(hplan.PARAMS)
    for val, key, lo, hi in ((max_reach, "max_reach_min", 5.0, 90.0), (min_skip, "os_min_skip_pct", 0.0, 60.0)):
        try:
            if str(val).strip():
                HP[key] = max(lo, min(hi, float(val)))
        except ValueError:
            pass
    mk = lambda gg, bs, d_: {"dir": d_, "stops": gg["stops"], "ss": gg["prep"]["stop_s"], "tt": hp_tt(gg, P), "H": gg["H"], "H_src": gg["H_src"],
                             "buses": [{**b, "near": b.get("near_name")} for b in bs]}
    T = mk(g, S["buses"], S["d"])
    O = mk(S["opp"]["g"], S["opp"]["buses"], S["opp"]["dir"]) if S.get("opp") else None
    cands, _u, cinfo = ho_candidates(S["svc"], S["d"], g["stops"], "", scope if scope in ("auto", "approved", "all") else "auto", g["prep"], P)
    ctx = {"now": S["now_min"], "late": {"bus": int(bus), "delay": D}, "T": T, "O": O, "cands": cands, "P": HP}
    sources, err = hplan.cross_sources(ctx)
    if err:
        return {"ok": False, "error": err}
    origins = hplan.cross_origins(ctx, sources)
    keys = list(origins)
    dests = [(c["lat"], c["lon"]) for c in cands]
    fac = float(offservice.PARAMS["bus_time_factor"])
    mx, merr = await os_matrix([origins[k] for k in keys], dests)
    reach = {}
    for oi, k in enumerate(keys):
        reach[k] = {}
        for ci, c in enumerate(cands):
            m = mx.get((oi, ci))
            if m:
                reach[k][c["j"]] = (m["min"] * fac, m["km"], f"road routing time x {fac:g}")
            else:
                km = hplan.hav_km(origins[k], (c["lat"], c["lon"])) * HP["detour"]
                extra = 3.0 if k.startswith("Ostop") else 0.0          # crossing to the other side of the road: allow a turn-around
                reach[k][c["j"]] = (km / HP["offsvc_kmh"] * 60.0 + extra, km, "straight-line estimate (routing unavailable)")
    res = await asyncio.to_thread(hplan.simulate_cross, ctx, reach)
    res.update(snap=snap, service=S["svc"], direction=S["d"], scope=cinfo.get("scope_used"), n_candidates=len(cands),
               routing="road routing (OSRM matrix) x bus factor" if mx else f"straight-line estimate ({merr or 'routing unavailable'})",
               origin=None, labels={"live": "LTA DataMall observations (unchanged)", "sim": "Simulation layer: injected delay", "derived": "Circulation and headways predicted from the running-time model"})
    return res


# =========================================================================== V15.0 Halfway Planner (next-trip halfway + front/rear regulation)
import hwplan


@app.get("/api/hplan/plan")
async def api_hp_plan(snap: str = "", bus: str = "", delay: str = "20", brk: str = "", stop_min: str = "", layover: str = "",
                      mode: str = "late", os_from: str = "", os_time: str = "", gap: str = "", aitp: str = ""):
    """Late bus completes its trip; its NEXT trip starts halfway. Every stop of the next direction is tested with real-road off-service
    time from the interchange; front bus hold / rear bus advance chosen per stop; ranked by downstream EWT. Live data unchanged.
    V16.28 (additive): gap=auto|<bus no.> - the ticked bus covers that gap on the next direction (the gap ahead of that bus; auto = the
    longest), the buses around its empty slot are re-spaced; aitp=N - EWT at the AITPs (every N stops, first and last)."""
    S = HP_SNAP.get(snap)
    if not S:
        return {"ok": False, "error": "The live snapshot has expired. Load the live buses again.", "expired": True}
    has_bus = str(bus).isdigit() and any(b["id"] == int(bus) for b in S["buses"])
    if not has_bus and mode != "os":
        return {"ok": False, "error": "Select the late bus."}
    try:
        D = max(0.0, min(120.0, float(delay or 0)))
        Pp = {}
        if str(brk).strip():
            Pp["break_min"] = max(0.0, min(30.0, float(brk)))
        if str(stop_min).strip():
            Pp["stop_min"] = max(0.5, min(6.0, float(stop_min)))
        if str(layover).strip():
            Pp["layover"] = max(0.0, min(40.0, float(layover)))
        if str(aitp).strip():
            Pp["aitp"] = max(1, min(20, int(float(aitp))))
    except ValueError:
        return {"ok": False, "error": "Lateness, break and stop-to-stop time must be numbers."}
    g = S["g"]
    mk = lambda gg, bs, d_: {"dir": d_, "stops": gg["stops"], "ss": gg["prep"]["stop_s"], "buses": bs, "H": gg["H"], "H_src": gg["H_src"]}
    if mode == "os":                                                   # V15.5: OS bus put halfway into the prolonged headway of THIS direction
        X = mk(g, S["buses"], S["d"])
        Y = mk(S["opp"]["g"], S["opp"]["buses"], S["opp"]["dir"]) if S.get("opp") else None
        stx = g["stops"]
        frm = (os_from or "").strip()
        if not frm or frm.lower() in ("ic", "interchange", "first"):
            org, olabel = (stx[0]["lat"], stx[0]["lon"]), f"{stx[0]['name']} ({stx[0]['code']}, interchange)"
        else:
            org, olabel, oerr = await pl_origin(frm, stx)
            if oerr:
                return {"ok": False, "error": f"OS start: {oerr}"}
        t0 = ho_parse_hhmm(os_time) if str(os_time).strip() else None
        t0 = S["now_min"] if t0 is None else (t0 + 1440 if t0 < S["now_min"] - 720 else t0)
        fac = float(offservice.PARAMS["bus_time_factor"])
        jx = list(range(0, len(stx) - 1))                                  # 0 = the first stop (full-trip option)
        offs, off_err = await os_table(org, [(stx[j]["lat"], stx[j]["lon"]) for j in jx])
        reach = {}
        for i, j in enumerate(jx):
            om = offs.get(i)
            if om:
                reach[j] = (om["min"] * fac, om.get("km"), f"real road routing (OSRM) x {fac:g} bus factor")
            else:
                km = hplan.hav_km(org, (stx[j]["lat"], stx[j]["lon"])) * 1.35
                reach[j] = (km / 25.0 * 60.0, km, "estimate - road routing unavailable")
        if S.get("test") and S.get("partial"):
            Pp["edge_trim"] = True; Pp["edge_gap"] = S.get("test_gap")          # only a partial test fleet has an edge
        res = await asyncio.to_thread(hwplan.plan_os, {"now": S["now_min"], "X": X, "Y": Y, "P": Pp, "os": {"t0": t0, "label": olabel, "lat": org[0], "lon": org[1]},
                                                       "late": {"bus": int(bus), "delay": D} if has_bus and D > 0 else None,
                                                       "gap_rear": int(re.sub(r"\D", "", gap)) if re.sub(r"\D", "", gap or "") else None}, reach)
        res.update(snap=snap, service=S["svc"], routing="real road routing (OSRM)" if offs else f"estimate ({off_err or 'road routing unavailable'})")
        return res
    T = mk(g, S["buses"], S["d"])
    O = mk(S["opp"]["g"], S["opp"]["buses"], S["opp"]["dir"]) if S.get("opp") else None
    gn = S["opp"]["g"] if S.get("opp") else g
    st = gn["stops"]
    ic = st[0]
    js = list(range(1, len(st) - 1))
    fac = float(offservice.PARAMS["bus_time_factor"])

    async def reach_from(org):
        offs_, err_ = await os_table(org, [(st[j]["lat"], st[j]["lon"]) for j in js])
        out = {}
        for i, j in enumerate(js):
            om = offs_.get(i)
            if om:
                out[j] = (om["min"] * fac, om.get("km"), f"real road routing (OSRM) x {fac:g} bus factor")
            else:
                km = hplan.hav_km(org, (st[j]["lat"], st[j]["lon"])) * 1.35
                out[j] = (km / 25.0 * 60.0, km, "estimate - road routing unavailable")
        return out, offs_, err_
    reach, offs, off_err = await reach_from((ic["lat"], ic["lon"]))
    if S.get("test") and S.get("partial"):
        Pp["edge_trim"] = True; Pp["edge_gap"] = S.get("test_gap")              # partial test fleet: ignore the gaps at its two ends
    base_ctx = {"now": S["now_min"], "late": {"bus": int(bus), "delay": D}, "T": T, "O": O, "P": Pp}
    gp = str(gap or "").strip().lower()
    if gp and gp not in ("slot", "own") and mode != "os":                 # V16.28: cover a gap on the next direction
        base_ctx["target"] = {"rear": None if gp == "auto" else re.sub(r"\D", "", gp) or None}
    if mode == "os":                                                   # V15.4: an extra OS bus + Bus Captain goes halfway; Bus C runs its next trip late
        frm = (os_from or "").strip()
        if not frm or frm.lower() in ("ic", "interchange", "first"):
            org, olabel = (ic["lat"], ic["lon"]), f"{ic['name']} ({ic['code']}, interchange)"
        else:
            org, olabel, oerr = await pl_origin(frm, st)
            if oerr:
                return {"ok": False, "error": f"OS start: {oerr}"}
        t0 = ho_parse_hhmm(os_time) if str(os_time).strip() else None
        if t0 is None:
            t0 = S["now_min"]
        elif t0 < S["now_min"] - 720:
            t0 += 1440
        oreach = reach if org == (ic["lat"], ic["lon"]) else (await reach_from(org))[0]
        res = await asyncio.to_thread(hwplan.plan, {**base_ctx, "mode": "os", "os": {"t0": t0, "label": olabel, "lat": org[0], "lon": org[1]}}, oreach)
        if res.get("ok"):
            rc = await asyncio.to_thread(hwplan.plan, base_ctx, reach)       # the same situation recovered by Bus C going halfway, for comparison
            bc = next((x for x in rc.get("candidates") or [] if x["code"] == rc.get("best")), None) if rc.get("ok") else None
            res["compare_c"] = {"ewt": bc["ewt"], "no": bc["no"], "code": bc["code"], "split": bc["gap"]["split"]} if bc else None
            res["os"].update(lat=org[0], lon=org[1])
    else:
        res = await asyncio.to_thread(hwplan.plan, base_ctx, reach)
    res.update(snap=snap, service=S["svc"], routing="real road routing (OSRM)" if offs else f"estimate ({off_err or 'road routing unavailable'})")
    return res


@app.get("/api/hplan/route")
async def api_hp_route(snap: str = "", stop: str = "", mode: str = "late", bus: str = "", os_from: str = "", leave: str = "", from_pt: str = "", from_label: str = "", on: str = "",
                       prefer: str = ""):
    """Off-service deployment route from the bus / OS location to the entry stop, with turn-by-turn guidance, traffic, incidents and road works.
    V16.28: prefer=simple picks the simplest route (most expressway / highway, fewest turns) within a few minutes of the fastest."""
    S = HP_SNAP.get(snap)
    if not S:
        return {"ok": False, "error": "The live snapshot has expired. Refresh and run the simulation again.", "expired": True}
    g, rdir = S["g"], S["d"]
    if on == "next" and S.get("opp"):                                    # V14.4: the late bus's next trip runs in the other direction
        g, rdir = S["opp"]["g"], S["opp"]["dir"]
    stops, ss, cum = g["stops"], g["prep"]["stop_s"], g["prep"]["cum"]
    j = next((i for i, s_ in enumerate(stops) if s_["code"] == re.sub(r"\D", "", stop)), None)
    if j is None:
        return {"ok": False, "error": "Choose an entry stop on this route."}
    m_ = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", from_pt or "")
    if m_:
        origin, origin_label = (float(m_.group(1)), float(m_.group(2))), (from_label.strip()[:80] or "Recovery bus")
    else:
        origin, origin_label, err = await hp_origin(S, "os" if mode == "os" else "late", bus, os_from)
        if err:
            return {"ok": False, "error": err}
    lv = ho_parse_hhmm(leave) if leave.strip() else None
    lv = lv if lv is not None else S["now_min"]
    prep = float(hplan.PARAMS["prep_min"])
    R, rerr = await os_routes(origin, (stops[j]["lat"], stops[j]["lon"]), S["svc"], rdir, stops[j]["code"], "dd", {}, g["line"], lv, planned_start=None, prep=prep)
    if R is None:
        return {"ok": False, "error": f"No road route could be calculated ({rerr})."}
    simple = prefer.strip().lower() == "simple" and R.get("simplest") is not None
    pick = R["simplest"] if simple else R["recommended"]
    x = next(o for o in R["options"] if o["idx"] == pick)
    near = lambda p: offservice.dist_to_line_m((p["lat"], p["lon"]), x["line"]) <= 300
    tot_g = sum(q_["min"] for q_ in x["groups"]) or 1.0
    return {"ok": True, "origin": {"label": origin_label, "lat": origin[0], "lon": origin[1]},
            "stop": {"code": stops[j]["code"], "name": stops[j]["name"], "lat": stops[j]["lat"], "lon": stops[j]["lon"], "km_from_start": round(ss[j], 2)},
            "km": round(x["km"], 2), "time_min": round(x["time_min"], 1), "time_src": x["time_src"], "leave_clock": hplan.hhmm(lv),
            "arrive_clock": hplan.hhmm(lv + x["time_min"]), "entry_clock": hplan.hhmm(lv + x["time_min"] + prep), "prep_min": prep,
            "status": x["status"], "status_label": offservice.STATUS_TXT[x["status"]], "status_text": x["status_text"], "findings": x["findings"][:8],
            "line": offservice.simplify(x["line"], 300), "traffic": x["traffic"], "roads": [q_["road"] for q_ in x["groups"]],
            "steps": hplan.instructions(x.get("steps") or [], stops[j]["code"], stops[j]["name"], scale=(x["time_min"] / x["osrm_min"]) if x.get("osrm_min") else None),
            "alternatives": [{"label": o["label"], "km": round(o["km"], 2), "time_min": round(o["time_min"], 1), "status": o["status"], "tags": o["tags"],
                              "chosen": o["idx"] == pick, "xway_share": (o.get("simple") or {}).get("xway_share"), "n_turns": o.get("n_turns"), "n_roads": len(o.get("groups") or []),
                              "line": offservice.simplify(o["line"], 80)} for o in R["options"]],
            "pick": "simple" if simple else "recommended", "why": R.get("why_simple") if simple else [], "simple": x.get("simple"),
            "stages": [{"road": q_["road"], "km": round(q_["km"], 2), "min": round(q_["min"] * x["time_min"] / tot_g, 1), "xway": q_.get("xkm", 0.0) >= 0.5 * max(q_["km"], 0.01),
                        "turns": q_["turns"]} for q_ in x["groups"]],
            "service_after": ho_simplify(ho_cut(g["line"], cum, ss[j], cum[-1]), 250),
            "incidents": [p for p in R["incidents"] if p.get("lat") is not None and near(p)][:10],
            "roadworks": [p for p in R["roadworks"] if p.get("lat") is not None and near(p)][:10],
            "note": "Navigation guidance for OCC planning. Check restrictions on the ground before use."}


@app.post("/api/halfway/offservice/record")
async def api_os_record(request: Request):
    """controller records: route_verified (this road sequence is verified for this bus type at this stop) or deployment_approved (audit)."""
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    try:
        b = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    kind = b.get("kind")
    if kind not in ("route_verified", "deployment_approved"):
        return JSONResponse({"error": "kind must be route_verified or deployment_approved"}, status_code=400)
    if not str(b.get("by") or "").strip():
        return JSONResponse({"error": "Enter your name for the record."}, status_code=400)
    if b.get("bus") not in offservice.BUS_TYPES:
        return JSONResponse({"error": "Unknown bus type."}, status_code=400)
    bb_sql("INSERT INTO offservice_record(ts, kind, service, direction, stop_code, bus_type, signature, roads, by_name, note, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
           (time.time(), kind, str(b.get("service") or "").upper(), int(b.get("direction") or 1), str(b.get("stop") or ""), b["bus"], str(b.get("signature") or ""),
            " > ".join(b.get("roads") or [])[:1000], str(b["by"])[:80], str(b.get("note") or "")[:300], json.dumps(b.get("detail") or {})[:4000]))
    return {"ok": True}


@app.get("/api/halfway/offservice/records")
async def api_os_records(service: str = "", limit: int = 30):
    rows = bb_sql("SELECT id, ts, kind, service, direction, stop_code, bus_type, roads, by_name, note FROM offservice_record " + ("WHERE service=? " if service.strip() else "")
                  + "ORDER BY ts DESC LIMIT ?", ((service.strip().upper(), limit) if service.strip() else (limit,)), fetch=True)
    return {"records": [dict(r, time=datetime.fromtimestamp(r["ts"], SGT).strftime("%d %b %H:%M")) for r in rows]}


@app.get("/api/halfway/recovery")
async def api_ho_recovery(service: str = "", direction: int = 1, ref: str = "", hw: str = "", layover: str = "", late: str = "", mode: str = "balanced",
                          veh: str = "own", sims: str = "", scope: str = "all", bus: str = "dd", vh: str = "", vw: str = "", vt: str = "",
                          balance: str = "", sched: str = "", adj2: str = "", treg: str = "", aitp_up: str = "", aitp_dn: str = ""):
    """Continue full trip (+ adjustment) vs halfway deployment (+ adjustment), decided on EWT over the selected BALANCE TRIPS, with stress test.
    balance = number of subsequent trips assessed (1..16, default the Settings value, 6); sched = optional comma list of HH:MM scheduled departures
    of trips 1..n (individual scheduled headways for SWT; otherwise the constant headway). Decision support only."""
    svc = service.strip().upper()
    g = await ho_route(svc, direction)
    if g.get("error"):
        return {"ok": False, "error": g["error"]}
    P = {**halfway.PARAMS, **HO["params"]}
    now = g["now"]
    t0 = ho_parse_hhmm(ref) if ref.strip() else ((now.hour * 60 + now.minute) // 5 + 1) * 5
    if t0 is None:
        return {"ok": False, "error": "First scheduled departure must be a time like 08:10."}
    try:
        H = float(hw) if hw.strip() else g["H"]
        lay = float(layover) if layover.strip() else P["layover_min"]
        lates = [float(x) if x.strip() else 0.0 for x in late.split(",")] if late.strip() else []
        ns = int(float(sims)) if sims.strip() else 1000
    except ValueError:
        return {"ok": False, "error": "Headway, layover, lateness and simulations must be numbers."}
    if not H or H <= 0:
        return {"ok": False, "error": "Scheduled headway unknown for this service - enter it in the Headway box."}
    try:
        nbal = int(round(float(balance))) if balance.strip() else int(P.get("balance_trips", 6))
    except ValueError:
        return {"ok": False, "error": "Balance Trips must be a whole number."}
    if not 1 <= nbal <= 16:
        return {"ok": False, "error": "Balance Trips must be between 1 and 16."}
    adj_ok = int(adj2.strip() in ("1", "true", "yes", "on")) if adj2.strip() else int(P.get("allow_adjacent_halfway", 0))     # two back-to-back halfway trips allowed?
    t_reg = int(treg.strip() in ("1", "true", "yes", "on")) if treg.strip() else int(P.get("term_reg", 1))                   # AI regulates the later trips at the terminals?
    n = int(P["n_trips"])
    lates = (lates + [0.0] * n)[:n]
    sched_dep = None
    if sched.strip():
        sd_ = [ho_parse_hhmm(x) for x in sched.split(",") if x.strip()]
        if any(x is None for x in sd_):
            return {"ok": False, "error": "Scheduled departures must be times like 08:10, separated by commas."}
        for k_ in range(1, len(sd_)):
            while sd_[k_] <= sd_[k_ - 1]:
                sd_[k_] += 1440                                                  # past midnight
        if len(sd_) < n:
            return {"ok": False, "error": f"Give a scheduled departure for each of the {n} trips (or leave it empty to use the constant headway)."}
        sched_dep = sd_[:n]
        t0 = sched_dep[0]
    stops = g["stops"]
    down_stops = []
    try:                                                                          # the return direction's stops: labels for the DOWN evaluation points only
        st_ = await static()
        ds_ = route_stops(st_, svc, 2 if direction == 1 else 1)
        if len(ds_) >= 2:
            d0 = ds_[0].get("dist") or 0.0
            down_stops = [(x["name"], x["code"], max(0.0, (x.get("dist") or 0.0) - d0)) for x in ds_]
    except Exception:
        down_stops = []
    cands, _unres, _info = ho_candidates(svc, direction, stops, "", scope if scope in ("auto", "all", "approved") else "all", g["prep"], P)
    mode = {"recovery": "headway", "pref": "balanced"}.get(mode, mode)
    bus = bus if bus in offservice.BUS_TYPES else "dd"
    dims = {}
    for key_, v_ in (("height_m", vh), ("width_m", vw), ("weight_t", vt)):
        try:
            dims[key_] = float(v_) if str(v_).strip() else None
        except ValueError:
            dims[key_] = None
    # real road time from the interchange to every candidate halfway stop (one OSRM table request); falls back to an estimate
    offs, off_err = await os_table((stops[0]["lat"], stops[0]["lon"]), [(c["lat"], c["lon"]) for c in cands])
    offsvc = {c["j"]: offs[i]["min"] * offservice.PARAMS["bus_time_factor"] for i, c in enumerate(cands) if offs.get(i)}
    ctx = {"H": H, "t0": t0, "late": lates, "tau": g["tau"], "stop_s": g["prep"]["stop_s"], "route_km": g["prep"]["km"], "stop_names": [s_["name"] for s_ in stops],
           "stop_codes": [s_["code"] for s_ in stops], "candidates": cands, "now": now.hour * 60 + now.minute, "mode": mode, "veh": veh, "offsvc_min": offsvc,
           "params": {"n_trips": n, "layover_min": lay, "min_layover_min": P["min_layover_min"], "adj_max": min(8.0, P["reg_hold_max"]), "offsvc_factor": P["offsvc_factor"],
                      "start_early_max": P["start_early_max"], "start_late_max": P["start_late_max"], "max_mileage_km": P["max_mileage_km"],
                      "min_remaining_pct": P["min_remaining_pct"], "sims": max(100, min(3000, ns)), "bunch_min": BB["params"]["bunch_min"],
                      "balance_trips": nbal, "ewt_gain_min": P["ewt_gain_min"], "ewt_gain_pct": P["ewt_gain_pct"], "ewt_gain_per_km": P["ewt_gain_per_km"],
                      "ewt_adjust_min": P["ewt_adjust_min"], "allow_adjacent_halfway": adj_ok, "term_reg": t_reg,
                      **{k_: P[k_] for k_ in ("term_hold_max", "term_early_max", "term_tol_pct", "term_zone", "term_total_max")}}}
    if sched_dep:
        ctx["sched_dep"] = sched_dep
    if down_stops:
        ctx["down_stops"] = down_stops
    a_up = [c for c in re.split(r"[,\s]+", aitp_up.strip()) if re.fullmatch(r"\d{5}", c)] if aitp_up.strip() else []
    a_dn = [c for c in re.split(r"[,\s]+", aitp_dn.strip()) if re.fullmatch(r"\d{5}", c)] if aitp_dn.strip() else []
    if a_up or a_dn:                                                              # AITP ticked: the EWT is calculated at these stops only
        ctx["aitp_up"], ctx["aitp_dn"] = a_up, a_dn
    ckey = json.dumps([svc, direction, t0, H, lay, lates, mode, veh, ns, scope, now.hour * 60 + now.minute, nbal, sched_dep,
                       [P[k_] for k_ in ("ewt_gain_min", "ewt_gain_pct", "ewt_gain_per_km", "ewt_adjust_min", "term_hold_max", "term_early_max", "term_tol_pct", "term_zone", "term_total_max")], adj_ok, t_reg, sorted(a_up), sorted(a_dn)], default=str)
    hit = RV_CACHE.get(ckey)
    if hit and time.time() - hit[0] < 300:
        res = json.loads(hit[1])
    else:
        res = await asyncio.to_thread(recovery.optimise, ctx)
        # the planner's detailed road route may be slower / faster than the screening estimate for the chosen stop: re-optimise once with it
        if res.get("ok") and (res.get("plans") or {}).get("halfway"):
            try:
                pl = await os_plan(g, dict(res, layover_min=lay), svc, direction, bus, dims)
                if pl.get("ok"):
                    o_ = pl["options"][pl["recommended"]]
                    row_ = next(r for r in res["plans"]["halfway"]["rows"] if r["type"] == "halfway")
                    if abs(o_["time_min"] - (row_.get("off_min") or 0)) > 1.5:
                        ctx["offsvc_min"] = dict(offsvc, **{row_["j"]: o_["time_min"]})
                        res = await asyncio.to_thread(recovery.optimise, ctx)
                        res["offsvc_refined"] = {"stop": pl["stop"]["name"], "screen_min": row_.get("off_min"), "route_min": o_["time_min"]}
            except Exception:
                pass
        if len(RV_CACHE) > 40:
            RV_CACHE.clear()
        RV_CACHE[ckey] = (time.time(), json.dumps(res))
    res["layover_min"] = lay
    res["offsvc_source"] = "road routing (OSRM) for every candidate stop" if offsvc else f"estimate: {P['offsvc_factor']:g} x in-service running time ({off_err or 'routing unavailable'})"
    if res.get("ok") and res.get("plans"):
        try:
            res["planner"] = await os_plan(g, res, svc, direction, bus, dims)
        except Exception as e:
            res["planner"] = {"ok": False, "reason": f"Off-service routing failed ({type(e).__name__}); the recommendation above still stands."}
    if res.get("ok"):
        res.update(service=svc, direction=direction, ref=ho_hhmm(t0), route={"km": round(g["prep"]["km"], 1), "run_min": round(g["tau"][-1], 1), "first": stops[0]["name"], "last": stops[-1]["name"]},
                   traffic_ok=g["traffic_ok"], updated=now.isoformat(timespec="seconds"))
    return res


def ho_audit(res, lates):
    best = res["options"][res["recommended"]] if res.get("recommended") is not None else None
    scen = [{"kind": o["kind"], "label": o["label"], "code": o.get("code"), "score": o.get("score"), "viable": o.get("viable"), "hold_min": o.get("hold_min"), "skipped_km": o.get("skipped_km"),
             "max_a": (o.get("metrics_a") or {}).get("max"), "max_b": (o.get("metrics") or {}).get("max"), "avg_a": (o.get("metrics_a") or {}).get("avg"), "avg_b": (o.get("metrics") or {}).get("avg"),
             "recovery": o.get("recovery"), "violations": o["violations"]} for o in res["options"]]
    bb_sql("INSERT INTO halfway_sim(ts,service,direction,ref_time,sched_hw,disrupted,lateness,start_delay,recommended,improvement_pct,no_halfway,best,scenarios,params,model_version,pref,rec_label,score,hold_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (time.time(), res["service"], res["direction"], res["ref"], res["sched_hw"], (res["disrupted"] if res.get("n_lost", 1) == 1 else ",".join(map(str, res["disrupted_all"]))), json.dumps(lates), res["start_delay"], (best.get("code") or best["kind"]) if best else None,
            best["improvement"]["avg_pct"] if best else None, json.dumps(best["metrics_a"]) if best else None, json.dumps(best["metrics"]) if best else None, json.dumps(scen),
            json.dumps({**res["params"], "bunch_min": BB["params"]["bunch_min"], "weights": res["weights"]}), halfway.MODEL_VERSION, res["pref"], best["label"] if best else None,
            best["score"] if best else None, best["hold_min"] if best else None))
    row = bb_sql("SELECT MAX(id) AS id FROM halfway_sim", fetch=True)
    return row[0]["id"] if row else None


@app.get("/api/halfway/runs")
async def api_ho_runs(limit: int = 30):
    limit = max(1, min(200, limit))
    rows = bb_sql("SELECT id, ts, service, direction, ref_time, sched_hw, disrupted, start_delay, pref, rec_label, score, hold_min, recommended, improvement_pct, model_version FROM halfway_sim ORDER BY id DESC LIMIT ?", (limit,), fetch=True)
    return {"runs": rows, "db_ok": BB["db_ok"]}


@app.get("/api/halfway/runs/{run_id}")
async def api_ho_run_detail(run_id: int):
    rows = bb_sql("SELECT * FROM halfway_sim WHERE id=?", (run_id,), fetch=True)
    if not rows:
        return JSONResponse({"error": "No such simulation run."}, status_code=404)
    r = rows[0]
    for k in ("lateness", "no_halfway", "best", "scenarios", "params"):
        try:
            r[k] = json.loads(r[k]) if r[k] else None
        except ValueError:
            pass
    return r


# ---- configuration (approved stops + parameters)
async def ho_points_public():
    st = await static()
    out = []
    for r in HO["points"]:
        name, code = None, r["stop_code"]
        if st.get("stops"):
            if not code and r["seq"]:
                code = next((x["code"] for x in st["routes"].get((r["service"], r["direction"]), []) if x["seq"] == r["seq"]), None)
            name = (st["stops"].get(code) or {}).get("name") if code else None
        out.append({"id": r["id"], "service": r["service"], "direction": r["direction"], "stop_code": r["stop_code"], "seq": r["seq"], "enabled": r["enabled"], "name": name})
    return out


@app.get("/api/halfway/config")
async def api_ho_config():
    return {"params": HO["params"], "defaults": halfway.PARAMS, "ranges": HO_RANGES, "points": await ho_points_public(), "tokenRequired": bool(BB_ADMIN), "db_ok": BB["db_ok"], "model": halfway.MODEL_VERSION,
            "bunch_min": BB["params"]["bunch_min"]}


@app.post("/api/halfway/config")
async def api_ho_config_save(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    if body.get("reset"):
        HO["params"] = dict(halfway.PARAMS)
        bb_sql("DELETE FROM halfway_parameter")
    else:
        out, errs = ho_validate_params(body.get("params"))
        if errs:
            return JSONResponse({"error": "; ".join(errs)}, status_code=400)
        HO["params"].update(out)
        for k, v in out.items():
            bb_sql("INSERT OR REPLACE INTO halfway_parameter(k, v) VALUES (?, ?)", (k, v))
    return {"ok": True, "params": HO["params"]}


@app.post("/api/halfway/points")
async def api_ho_points_save(request: Request):
    """Replace ALL approved halfway stops with the uploaded CSV."""
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    rows, errs = parse_points((await request.body()).decode("utf-8-sig", "replace"))
    if not rows:
        return JSONResponse({"error": "No valid rows. " + "; ".join(errs)}, status_code=400)
    ho_save_points(rows, True)
    return {"ok": True, "rows": len(rows), "errors": errs}


@app.post("/api/halfway/points/add")
async def api_ho_points_add(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    try:
        b = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    line = ",".join(str(b.get(k, "") if b.get(k) is not None else "") for k in ("service", "direction", "stop_code", "seq", "enabled"))
    rows, errs = parse_points("service,direction,stop_code,sequence,enabled\n" + line)
    if not rows:
        return JSONResponse({"error": "; ".join(errs) or "Invalid row."}, status_code=400)
    ho_save_points(rows, False)
    return {"ok": True}


@app.post("/api/halfway/points/delete")
async def api_ho_points_delete(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    try:
        pid = int(json.loads((await request.body()).decode("utf-8") or "{}").get("id"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Body must be JSON with the point id."}, status_code=400)
    bb_sql("DELETE FROM halfway_point_master WHERE id=?", (pid,))
    ho_load_points()
    return {"ok": True}


@app.post("/api/halfway/points/clear")
async def api_ho_points_clear(request: Request):
    if not bb_auth(request):
        return JSONResponse({"error": "Admin token missing or wrong."}, status_code=401)
    ho_save_points([], True)
    return {"ok": True}


@app.get("/api/stop-suggest")
async def stop_suggest(q: str = Query("")):
    q = q.strip().lower()
    st = await static()
    if len(q) < 2 or not st["stops"]:
        return []
    starts, contains = [], []
    for s in st["stops"].values():
        if s["code"].startswith(q):
            starts.append(s)
        elif q in f"{s['name']} {s['road']}".lower():
            contains.append(s)
        if len(starts) >= 8:
            break
    return [{"code": s["code"], "name": s["name"], "road": s["road"]} for s in (starts + contains)[:8]]


# =========================================================================== Traffic-Aware Regulation (the engine is traffic.py)
EP_ROADWORKS = os.getenv("LTA_ROADWORKS_PATH", "RoadWorks")     # verify against the current DataMall user guide (road works / road openings)
TTL_ROADWORKS = 600
CAMERA_PATHS = [p for p in [os.getenv("LTA_CAMERAS_PATH", "").strip("/ "), "Traffic-Imagesv2", "v3/Traffic-Images", "TrafficImages"] if p]
CAMERA_PATHS = list(dict.fromkeys(CAMERA_PATHS))     # verify against the current DataMall user guide (Traffic Images / traffic camera snapshots)
GOOD_CAMERA_PATH = None

# Annex G (LTA DataMall API User Guide v6.9, 3 Aug 2026) - CameraID -> location description, static reference data.
# Supplied by the user, verbatim from the guide's Annex G table. The join is by CameraID against the LIVE
# Traffic-Imagesv2 response (see api_cameras): a camera the live feed returns but this table does not cover is
# still shown, labelled "Location description unavailable" - live data is never discarded for a missing static
# description. If a later edition of Annex G adds or renumbers IDs, update the entries below to match.
ANNEX_G = {
    "1111": "TPE(PIE) - Exit 2 to Loyang Ave", "1112": "TPE(PIE) - Tampines Viaduct", "1113": "Tanah Merah Coast Road towards Changi",
    "1701": "CTE (AYE) - Moulmein Flyover LP448F", "1702": "CTE (AYE) - Braddell Flyover LP274F", "1703": "CTE (SLE) - Blk 22 St George's Road",
    "1704": "CTE (AYE) - Entrance from Chin Swee Road", "1705": "CTE (AYE) - Ang Mo Kio Ave 5 Flyover", "1706": "CTE (AYE) - Yio Chu Kang Flyover",
    "1707": "CTE (AYE) - Bukit Merah Flyover", "1709": "CTE (AYE) - Exit 6 to Bukit Timah Road", "1711": "CTE (AYE) - Ang Mo Kio Flyover",
    "2701": "Woodlands Causeway (Towards Johor)", "2702": "Woodlands Checkpoint", "2703": "BKE (PIE) - Chantek F/O",
    "2704": "BKE (Woodlands Checkpoint) - Woodlands F/O", "2705": "BKE (PIE) - Dairy Farm F/O", "2706": "Entrance from Mandai Rd (Towards Checkpoint)",
    "2707": "Exit 5 to KJE (towards PIE)", "2708": "Exit 5 to KJE (Towards Checkpoint)",
    "3702": "ECP (Changi) - Entrance from PIE", "3704": "ECP (Changi) - Entrance from KPE", "3705": "ECP (AYE) - Exit 2A to Changi Coast Road",
    "3793": "ECP (Changi) - Laguna Flyover", "3795": "ECP (City) - Marine Parade F/O", "3796": "ECP (Changi) - Tanjong Katong F/O",
    "3797": "ECP (City) - Tanjung Rhu", "3798": "ECP (Changi) - Benjamin Sheares Bridge",
    "4701": "AYE (City) - Alexander Road Exit", "4702": "AYE (Jurong) - Keppel Viaduct", "4703": "Tuas Second Link",
    "4704": "AYE (CTE) - Lower Delta Road F/O", "4705": "AYE (MCE) - Entrance from Yuan Ching Rd", "4706": "AYE (Jurong) - NUS Sch of Computing TID",
    "4707": "AYE (MCE) - Entrance from Jln Ahmad Ibrahim", "4708": "AYE (CTE) - ITE College West Dover TID", "4709": "Clementi Ave 6 Entrance",
    "4710": "AYE(Tuas) - Pandan Garden", "4712": "AYE(Tuas) - Tuas Ave 8 Exit", "4713": "Tuas Checkpoint",
    "4714": "AYE (Tuas) - Near West Coast Walk", "4716": "AYE (Tuas) - Entrance from Benoi Rd", "4798": "Sentosa Tower 1", "4799": "Sentosa Tower 2",
    "5794": "PIEE (Jurong) - Bedok North", "5795": "PIEE (Jurong) - Eunos F/O", "5797": "PIEE (Jurong) - Paya Lebar F/O",
    "5798": "PIEE (Jurong) - Kallang Sims Drive Blk 62", "5799": "PIEE (Changi) - Woodsville F/O",
    "6701": "PIEW (Changi) - Blk 65A Jln Tenteram, Kim Keat", "6703": "PIEW (Changi) - Blk 173 Toa Payoh Lorong 1", "6704": "PIEW (Jurong) - Mt Pleasant F/O",
    "6705": "PIEW (Changi) - Adam F/O Special pole", "6706": "PIEW (Changi) - BKE", "6708": "Nanyang Flyover (Towards Changi)",
    "6710": "Entrance from Jln Anak Bukit (Towards Changi)", "6711": "Entrance from ECP (Towards Jurong)", "6712": "Exit 27 to Clementi Ave 6",
    "6713": "Entrance From Simei Ave (Towards Jurong)", "6714": "Exit 35 to KJE (Towards Changi)", "6715": "Hong Kah Flyover (Towards Jurong)", "6716": "AYE Flyover",
    "7791": "TPE (PIE) - Upper Changi F/O", "7793": "TPE(PIE) - Entrance to PIE from Tampines Ave 10", "7794": "TPE(SLE) - TPE Exit KPE",
    "7795": "TPE(PIE) - Entrance from Tampines FO", "7796": "TPE(SLE) - On rooftop of Blk 189A Rivervale Drive 9", "7797": "TPE(PIE) - Seletar Flyover",
    "7798": "TPE(SLE) - LP790F (On SLE Flyover)",
    "8701": "KJE (PIE) - Choa Chu Kang West Flyover", "8702": "KJE (BKE) - Exit To BKE", "8704": "KJE (BKE) - Entrance From Choa Chu Kang Dr",
    "8706": "KJE (BKE) - Tengah Flyover",
    "9701": "SLE (TPE) - Lentor F/O", "9702": "SLE(TPE) - Thomson Flyover", "9703": "SLE(Woodlands) - Woodlands South Flyover",
    "9704": "SLE(TPE) - Ulu Sembawang Flyover", "9705": "SLE(TPE) - Beside Slip Road From Woodland Ave 2", "9706": "SLE(Woodlands) - Mandai Lake Flyover",
}


# V13.10.3: expressway grouping for the /cameras page (like trafficiti.com/aye/). Based on the Annex G description and
# LTA's ID series (27xx BKE, 37xx ECP, 47xx AYE, 57xx/67xx PIE, 77xx TPE, 87xx KJE, 97xx SLE, 17xx CTE). Checkpoint
# cameras are pulled out into their own group because that is what most people open the page for.
CAMERA_ROADS = [("CHECKPOINTS", "Woodlands & Tuas Checkpoints"), ("AYE", "Ayer Rajah Expressway (AYE)"), ("BKE", "Bukit Timah Expressway (BKE)"),
                ("CTE", "Central Expressway (CTE)"), ("ECP", "East Coast Parkway (ECP)"), ("KJE", "Kranji Expressway (KJE)"),
                ("PIE", "Pan-Island Expressway (PIE)"), ("SLE", "Seletar Expressway (SLE)"), ("TPE", "Tampines Expressway (TPE)"),
                ("SENTOSA", "Sentosa Gateway"), ("OTHER", "Other cameras")]
_CP = {"2701", "2702", "4703", "4713"}
_ROAD_BY_PREFIX = {"17": "CTE", "27": "BKE", "37": "ECP", "47": "AYE", "57": "PIE", "67": "PIE", "77": "TPE", "87": "KJE", "97": "SLE"}
_ROAD_FIXED = {"1111": "TPE", "1112": "TPE", "1113": "ECP", "4798": "SENTOSA", "4799": "SENTOSA"}


def camera_road(cid):
    cid = str(cid).strip()
    if cid in _CP:
        return "CHECKPOINTS"
    return _ROAD_FIXED.get(cid) or _ROAD_BY_PREFIX.get(cid[:2], "OTHER")


def annex_g_lookup(cid):
    return ANNEX_G.get(str(cid).strip())
TTL_CAMERAS = 120      # LTA's ImageLink is a short-lived signed URL, so this list is not cached long
TR = {"params": dict(traffic.PARAMS), "book": traffic.new_book(), "last": 0.0, "ridx": None, "ridx_key": None, "feeds": {}, "lock": None, "rw": [], "stretch_cache": None, "roads": None, "roads_key": None}


@app.get("/cameras", response_class=HTMLResponse)
async def page_cameras():
    return HTMLResponse((HERE / "cameras.html").read_text(encoding="utf-8"))


@app.get("/api/cameras/gallery")
async def api_cameras_gallery(refresh: int = 0):
    """All LTA traffic cameras grouped by expressway, in Annex G order. Annex G IDs that are not in the live feed are
    listed with live=False so the page can say so instead of silently dropping them."""
    if refresh:
        hit = CACHE.get("cameras")
        if hit and time.time() - hit[1] > 30:
            CACHE.pop("cameras", None)
    cs = await cameras_state()
    cams = {c["id"]: c for c in (cs.get("cams") or [])}
    hit = CACHE.get("cameras")
    fetched = hit[1] if hit else None
    groups = {k: [] for k, _ in CAMERA_ROADS}
    for cid in list(ANNEX_G) + sorted(set(cams) - set(ANNEX_G)):
        c = cams.get(cid)
        groups[camera_road(cid)].append({"id": cid, "desc": ANNEX_G.get(cid) or (c or {}).get("desc") or "Location description unavailable",
                                          "live": bool(c), "image": c["link"] if c else None, "taken": c.get("taken") if c else None,
                                          "lat": c["lat"] if c else None, "lon": c["lon"] if c else None})
    return {"roads": [{"key": k, "name": n, "cameras": groups[k]} for k, n in CAMERA_ROADS if groups[k]],
            "total_live": len(cams), "annex_listed": len(ANNEX_G), "fetched": tr_hhmm(fetched) if fetched else None, "fetched_epoch": fetched,
            "source": cs.get("source") or "LTA DataMall \u00b7 Traffic Images", "error": cs.get("error") if not cams else None,
            "sources": cs.get("sources"), "diag": cs.get("diag")}


@app.get("/traffic", response_class=HTMLResponse)
async def page_traffic():
    return HTMLResponse((HERE / "traffic.html").read_text(encoding="utf-8"))


def tr_init():
    bb_sql("CREATE TABLE IF NOT EXISTS traffic_setting(k TEXT PRIMARY KEY, v REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS site_setting(k TEXT PRIMARY KEY, v TEXT)")                      # V16.8: site-wide settings (hidden pages)
    bb_sql("""CREATE TABLE IF NOT EXISTS occ_note(id INTEGER PRIMARY KEY AUTOINCREMENT, tab TEXT, kind TEXT, title TEXT, body TEXT,
                    services TEXT, author TEXT, pinned INTEGER DEFAULT 0, source_alert TEXT, created_ts REAL, updated_ts REAL)""")   # V16.10: OCC Connect notes
    bb_sql("CREATE TABLE IF NOT EXISTS occ_reply(id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER, author TEXT, body TEXT, ts REAL)")
    bb_sql("""CREATE TABLE IF NOT EXISTS occ_ticket(id INTEGER PRIMARY KEY AUTOINCREMENT, no TEXT, title TEXT, category TEXT, priority TEXT, status TEXT,
                    service TEXT, direction INTEGER, bus_reg TEXT, location TEXT, description TEXT, owner TEXT, shared TEXT, watchers TEXT,
                    personal INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, source_alert TEXT, created_ts REAL, updated_ts REAL, resolved_ts REAL,
                    resolution_outcome TEXT, resolution_action TEXT, resolution_notes TEXT, author TEXT)""")                          # V16.11: OCC Notes & Actions
    bb_sql("CREATE TABLE IF NOT EXISTS occ_activity(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, ts REAL, actor TEXT, action TEXT, detail TEXT)")
    bb_sql("CREATE TABLE IF NOT EXISTS occ_ack(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, who TEXT, ts REAL)")
    for r in bb_sql("SELECT k, v FROM site_setting", fetch=True):
        if r["k"] == "hidden_pages":
            SITE["hidden"] = [x for x in str(r["v"]).split(",") if x in SITE_PAGE_IDS]
        elif r["k"] == "block_hidden":
            SITE["block"] = str(r["v"]) != "0"
    bb_sql("CREATE TABLE IF NOT EXISTS traffic_state(k TEXT PRIMARY KEY, v TEXT)")
    bb_sql("CREATE TABLE IF NOT EXISTS alert_acknowledgement(id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT, event_id TEXT, service TEXT, direction INTEGER, ack_time REAL, ack_by TEXT, condition TEXT)")
    TR["params"] = dict(traffic.PARAMS)
    for r in bb_sql("SELECT k, v FROM traffic_setting", fetch=True):
        if r["k"] in traffic.PARAMS:
            TR["params"][r["k"]] = int(r["v"]) if r["k"] in traffic.INT_PARAMS else r["v"]
    row = bb_sql("SELECT v FROM traffic_state WHERE k='book'", fetch=True)
    if row:
        try:
            TR["book"] = {**traffic.new_book(), **json.loads(row[0]["v"])}
        except ValueError:
            TR["book"] = traffic.new_book()


def tr_save():
    b = TR["book"]
    bb_sql("INSERT OR REPLACE INTO traffic_state(k, v) VALUES ('book', ?)", (json.dumps({"events": {i: e for i, e in b["events"].items() if e["status"] in ("active", "cleared")}, "acks": b["acks"], "seq": b["seq"], "snap": b["snap"], "updates": b["updates"]}),))


def tr_hhmm(ts):
    return datetime.fromtimestamp(ts, SGT).strftime("%H:%M") if ts else None


def tr_epoch(s, end=False):
    """A date / time from the road works feed -> epoch seconds (Singapore time). Dates without a time start at 00:00 and end at 23:59."""
    s = str(s or "")
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    t = re.search(r"[T ](\d{1,2}):(\d{2})", s)
    hh, mm = (int(t.group(1)), int(t.group(2))) if t else ((23, 59) if end else (0, 0))
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=SGT).timestamp()
    except ValueError:
        return None


async def tr_roadworks(now):
    async def factory():
        rows, err = await fetch_pages(EP_ROADWORKS)
        return {"rows": rows, "error": err}, TTL_ROADWORKS, bool(rows) or not err
    d = await cached("roadworks", factory)
    items = []
    for x in d["rows"]:
        road = str(x.get("RoadName") or x.get("Road") or "").strip()
        st, en = tr_epoch(x.get("StartDate")), tr_epoch(x.get("EndDate"), True)
        if not road or st is None or st > now + 120 * 60 or (en is not None and en < now):
            continue
        lat, lon = num(x.get("Latitude")), num(x.get("Longitude"))
        items.append({"key": str(x.get("EventID") or f"{road}|{x.get('StartDate')}"), "road": road, "lat": lat if in_sg(lat, lon) else None, "lon": lon if in_sg(lat, lon) else None,
                      "start_epoch": st, "end_epoch": en, "other": str(x.get("Other") or x.get("SvcDept") or "")[:200]})
    return items, d.get("error")


async def fetch_cameras():
    """LTA DataMall Traffic-Imagesv2 returns ALL cameras in one call (guide v6.9 changelog: "Traffic Images API now returns
    all records per call") and does not honour $skip. The old loop kept asking for $skip=N, got the same ~90 cameras back
    every time and stacked hundreds of duplicates on identical coordinates (the "502" cluster on the map). Now: one call,
    rows de-duplicated by CameraID, and a further page is only requested if the first one was full (500 rows) AND the next
    page actually brings camera IDs we have not seen."""
    global GOOD_CAMERA_PATH
    paths = [GOOD_CAMERA_PATH] if GOOD_CAMERA_PATH else CAMERA_PATHS
    errors = []
    for path in paths:
        by_id, skip, err, d = {}, 0, None, {}
        while True:
            d = await get_lta(path, {"$skip": skip}) if skip else await get_lta(path)
            if d.get("_error"):
                err = d["_error"]
                break
            batch = d.get("value", []) or []
            new = 0
            for x in batch:
                cid = str(x.get("CameraID") or x.get("CameraId") or "").strip()
                if cid and cid not in by_id:
                    by_id[cid] = x
                    new += 1
            if len(batch) < 500 or new == 0 or skip >= 2000:   # not a full page, or the API ignored $skip: done
                break
            skip += len(batch)
        rows = list(by_id.values())
        if err:
            if rows:
                GOOD_CAMERA_PATH = path
                return rows, None
            errors.append(err)
            if d.get("_status") == 404:
                if path == GOOD_CAMERA_PATH:
                    GOOD_CAMERA_PATH = None
                continue
            break  # auth / network / rate limit problem: other paths will not help
        GOOD_CAMERA_PATH = path
        return rows, None
    return [], " | ".join(errors[-3:])


def dedupe_cams(cams):
    """One marker per CameraID, whatever the source returned."""
    seen, out = set(), []
    for c in cams:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append(c)
    return out


def norm_camera(x):
    lat, lon = num(x.get("Latitude") or x.get("Lat")), num(x.get("Longitude") or x.get("Lng") or x.get("Lon"))
    link = x.get("ImageLink") or x.get("Image") or x.get("ImageURL") or x.get("image")
    cid = str(x.get("CameraID") or x.get("CameraId") or x.get("ID") or x.get("id") or "").strip()
    if not (in_sg(lat, lon) and link and cid):
        return None
    desc = annex_g_lookup(cid)
    return {"id": cid, "lat": lat, "lon": lon, "link": str(link), "taken": x.get("Timestamp") or None,
            "desc": desc or "Location description unavailable", "desc_known": bool(desc), "road": camera_road(cid)}


async def fetch_cameras_datagov():
    """data.gov.sg Traffic Images (dataset d_6cdb6b405b25aaaacbaf7689bcc6fae0): the same LTA cameras, keyless (a
    DATAGOV_API_KEY raises the rate limit), with the time each photo was taken. -> (rows, error, feed_timestamp)"""
    try:
        r = await client().get("https://api.data.gov.sg/v1/transport/traffic-images", headers={"x-api-key": DATAGOV_KEY} if DATAGOV_KEY else None)
        if r.status_code == 429:
            return [], "data.gov.sg traffic-images: rate limited (HTTP 429) - set DATAGOV_API_KEY", None
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
        cams = (items[0].get("cameras") if items else None) or []
        rows = [{"CameraID": c.get("camera_id"), "Latitude": (c.get("location") or {}).get("latitude"), "Longitude": (c.get("location") or {}).get("longitude"),
                 "ImageLink": c.get("image"), "Timestamp": c.get("timestamp")} for c in cams]
        return rows, (None if rows else "data.gov.sg traffic-images: no cameras in response"), (items[0].get("timestamp") if items else None)
    except Exception as e:
        return [], f"data.gov.sg traffic-images: {type(e).__name__}", None


def _age_min(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 60
    except Exception:
        return None


async def cameras_state():
    """V13.12: LTA DataMall and data.gov.sg are both read every time and merged by CameraID, so a camera missing from one
    feed still shows if the other has it. Where both have a camera, LTA's photo link is used (official, keyed) and the
    data.gov.sg capture time is attached when it is recent."""
    async def factory():
        (rows, err), (rows2, err2, feed_ts) = await asyncio.gather(fetch_cameras(), fetch_cameras_datagov())
        lta = {c["id"]: c for c in dedupe_cams([c for c in (norm_camera(x) for x in rows) if c])}
        dg = {c["id"]: c for c in dedupe_cams([c for c in (norm_camera(x) for x in rows2) if c])}
        feed_age = _age_min(feed_ts)
        dg_stale = feed_age is not None and feed_age > 30
        merged = {}
        for cid in list(lta) + [k for k in dg if k not in lta]:
            c = dict(lta.get(cid) or dg[cid])
            d = dg.get(cid)
            if cid in lta:
                c["src"] = "lta"
                if d and d.get("taken") and (_age_min(d["taken"]) if _age_min(d["taken"]) is not None else 1e9) <= 15:
                    if not c.get("taken"):
                        c["link"], c["taken"], c["src"] = d["link"], d["taken"], "datagov"
            else:
                c["src"] = "datagov"
            merged[cid] = c
        cams = list(merged.values())
        both, only_lta, only_dg = len(set(lta) & set(dg)), len(set(lta) - set(dg)), len(set(dg) - set(lta))
        if lta and dg:
            src = "LTA DataMall + data.gov.sg \u00b7 Traffic Images"
        elif lta:
            src = "LTA DataMall \u00b7 Traffic Images"
        elif dg:
            src = "data.gov.sg \u00b7 LTA Traffic Images"
        else:
            src = "none"
        error = None if cams else " | ".join(e for e in (err or "LTA returned no cameras", err2) if e)
        diag = (f"LTA raw={len(rows)} valid={len(lta)}{(' err=' + err) if err else ''} | data.gov.sg raw={len(rows2)} valid={len(dg)}"
                f"{(' err=' + err2) if err2 else ''}{f' feed_age={round(feed_age)}min' if feed_age is not None else ''}{' STALE' if dg_stale else ''}"
                f" | merged={len(cams)} (both={both}, LTA only={only_lta}, data.gov.sg only={only_dg})")
        return {"cams": cams, "error": error, "raw_error": err, "fallback_error": err2, "source": src, "diag": diag,
                "sources": {"lta": len(lta), "datagov": len(dg), "datagov_feed_time": feed_ts, "datagov_stale": dg_stale}}, TTL_CAMERAS, bool(cams)
    return await cached("cameras", factory)


def nearest_camera(cams, lat, lon, max_km):
    if lat is None or lon is None:
        return None, None
    best, bd = None, max_km
    for c in cams:
        d = hav_km(lat, lon, c["lat"], c["lon"])
        if d < bd:
            best, bd = c, d
    return (best, bd) if best else (None, None)


# ---- V16.1: every service drawn on the REAL road for the traffic engine (busrouter.sg lines fitted to the LTA stops, one download for the whole network)
TR_LINES = {"lines": {}, "key": None, "at": 0.0, "task": None, "info": "not built yet", "n": 0}


async def tr_build_lines(st):
    try:
        data = await routegeom.busrouter_routes(client())
        if not data:
            TR_LINES.update(info=routegeom._BR.get("err") or "busrouter.sg unavailable", at=time.time())
            return

        def work():
            out, dec = {}, {}
            for (svc, d) in list(st["routes"].keys()):
                stops = route_stops(st, svc, d)
                if len(stops) < 2:
                    continue
                if svc not in dec:
                    polys = []
                    for enc in (data.get(svc) or data.get(svc.upper()) or []):
                        try:
                            polys.append(routegeom.decode_polyline(enc))
                        except Exception:
                            pass
                    dec[svc] = polys
                if not dec[svc]:
                    continue
                try:
                    r = routegeom.fast_fit(dec[svc], stops)
                except Exception:
                    r = None
                if r:
                    out[(svc, d)] = r
            return out
        lines = await asyncio.to_thread(work)
        TR_LINES.update(lines=lines, key=(len(st["routes"]), len(st["stops"])), at=time.time(), n=len(lines),
                        info=f"{len(lines)} of {len(st['routes'])} service-directions on the real road (busrouter.sg, fitted to LTA stops)")
    except Exception as e:
        TR_LINES.update(info=f"road lines failed ({type(e).__name__})", at=time.time())


def tr_lines_kick(st):
    """Start (in the background) a rebuild of the road lines when missing or a day old; never blocks a refresh."""
    key = (len(st["routes"]), len(st["stops"]))
    stale = TR_LINES["key"] != key or time.time() - TR_LINES["at"] > 24 * 3600
    retry_ok = TR_LINES["n"] > 0 or time.time() - TR_LINES["at"] > 1800
    if stale and retry_ok and (TR_LINES["task"] is None or TR_LINES["task"].done()):
        TR_LINES["task"] = asyncio.create_task(tr_build_lines(st))


# ---- V16.3 Waze for Cities data feed (optional). Set WAZE_FEED_URL to the JSON feed link from Waze Partner Hub > Toolbox > Waze Data Feed.
# Without it everything below is switched off and the platform behaves exactly as before.
WAZE = {"url": os.getenv("WAZE_FEED_URL", "").strip(), "at": 0.0, "ok_at": 0.0, "error": None, "jams": [], "alerts": [], "idx": None, "n_raw": 0}
WAZE_TTL = 120                      # Waze refreshes the feed every 2 minutes


async def waze_state():
    if not WAZE["url"]:
        return WAZE
    now = time.time()
    if now - WAZE["at"] < WAZE_TTL:
        return WAZE
    WAZE["at"] = now
    try:
        url = WAZE["url"] + ("" if "format=" in WAZE["url"] else ("&" if "?" in WAZE["url"] else "?") + "format=1")
        r = await client().get(url, timeout=25)
        r.raise_for_status()
        feed = r.json()
        jams, alerts_ = traffic.parse_waze(feed, TR["params"])
        idx = await asyncio.to_thread(traffic.JamIndex, jams)
        WAZE.update(jams=jams, alerts=alerts_, idx=idx, ok_at=now, error=None, n_raw=len(feed.get("jams") or []) + len(feed.get("alerts") or []))
    except Exception as e:
        WAZE["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return WAZE


def waze_live():
    return bool(WAZE["url"]) and WAZE["idx"] is not None and time.time() - WAZE["ok_at"] < 10 * 60


# ---- V16.4 TomTom Traffic API (optional second source). Set TOMTOM_API_KEY on the server (never put the key in the code).
# Two uses: (1) Flow Segment Data checks the speed on each active LTA congestion stretch; (2) Incident Details adds accidents / closures / breakdowns.
# Calls are cached and capped per day so the free tier is not used up. TOMTOM_DAILY_CAP (default 2000) is the most calls per Singapore day.
# CONGESTION_FIRST: "tomtom" (default when a TomTom key is set) = TomTom jams are detected first and LTA speed bands check them; "lta" = the old order.
TT_FIRST = bool(os.getenv("TOMTOM_API_KEY", "").strip()) and os.getenv("CONGESTION_FIRST", "tomtom").strip().lower() != "lta"
TT = {"jams": [], "referer": os.getenv("TOMTOM_REFERER", "").strip(), "key": os.getenv("TOMTOM_API_KEY", "").strip(), "cap": int(os.getenv("TOMTOM_DAILY_CAP", "2000") or 2000), "day": "", "n": 0,
      "inc_at": 0.0, "inc_ok_at": 0.0, "alerts": [], "error": None, "flow": {}, "flow_error": None, "flow_ok_at": 0.0}
TT_INC_TTL = int(os.getenv("TOMTOM_INCIDENTS_EVERY_SEC", "0") or 0) or (120 if TT_FIRST else 300)   # one call for the whole island: every 2 min when TomTom finds jams first, else every 5 min
# V17: TomTom's free tier is MONTHLY per API (Incident Details 2,500 a month = ~83 a day). TOMTOM_INCIDENTS_EVERY_SEC (e.g. 1800) slows this call down to stay inside it.
TT_FLOW_TTL = 600                   # flow: one reading per stretch point per 10 minutes
TT_FLOW_MAX_EVENTS = 10             # at most this many congestion stretches are checked per cycle
TT_SLOW_RATIO = 0.5                 # current speed / free-flow speed at or below this = jam
TT_CLEAR_RATIO = 0.8                # ... at or above this = traffic is flowing normally
TT_MIN_CONF = 0.5                   # TomTom confidence below this is ignored
TT_BBOX = "103.60,1.15,104.10,1.48"  # Singapore
TT_CATS = {1: "Accident", 7: "Lane closed", 8: "Road closed", 11: "Flooding", 14: "Broken down vehicle"}


def tt_headers():
    """If the TomTom key is limited to certain websites (Referer), send that website name. Set TOMTOM_REFERER, for example https://your-app.onrender.com/"""
    return {"Referer": TT["referer"]} if TT["referer"] else {}


# V16.26e: TomTom's free quota is counted per API (Routing has its own monthly allowance), so the diversion planner's routing
# calls get their own daily budget instead of sharing TOMTOM_DAILY_CAP with the traffic collector (which polls all day).
# TOMTOM_ROUTING_CAP (default 600 a day, about 18,600 a month: inside a 20,000-a-month free Routing allowance).
TT_RT = {"day": "", "n": 0, "cap": int(os.getenv("TOMTOM_ROUTING_CAP", "600") or 600)}


def tt_route_spend():
    """Count one routing call against today's routing budget. False = budget used up (OpenStreetMap routing is used instead)."""
    day = now_sgt().strftime("%Y-%m-%d")
    if TT_RT["day"] != day:
        TT_RT["day"], TT_RT["n"] = day, 0
    if TT_RT["n"] >= TT_RT["cap"]:
        return False
    TT_RT["n"] += 1
    return True


def tt_route_left():
    return TT_RT["cap"] - (TT_RT["n"] if TT_RT["day"] == now_sgt().strftime("%Y-%m-%d") else 0)


def tt_spend():
    """Count one call against today's cap. False = cap reached, do not call."""
    day = now_sgt().strftime("%Y-%m-%d")
    if TT["day"] != day:
        TT["day"], TT["n"] = day, 0
    if TT["n"] >= TT["cap"]:
        return False
    TT["n"] += 1
    return True


def tt_err(e):
    try:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            hint = {401: "key rejected", 403: "key not enabled for this TomTom API, or its free monthly allowance is used up (see the TomTom dashboard)",
                    429: "too many calls / quota"}.get(code, "")
            body = " ".join((e.response.text or "").split())[:100]
            if "InvalidReferer" in body:
                hint = "key is limited to certain websites: in TomTom set Allowed Referers to *, or set TOMTOM_REFERER on the server"
            return f"HTTP {code} ({hint}) {body}".replace("()", "").strip()
    except Exception:
        pass
    return f"{type(e).__name__}: {str(e)[:100]}"


def tt_live():
    return bool(TT["key"]) and time.time() - max(TT["inc_ok_at"], TT["flow_ok_at"]) < 15 * 60


async def tomtom_incidents():
    """TomTom incident list for the whole island (cached 5 min). A failed call keeps the last good list."""
    if not TT["key"]:
        return TT
    now = time.time()
    if now - TT["inc_at"] < TT_INC_TTL:
        return TT
    TT["inc_at"] = now
    if not tt_spend():
        TT["error"] = "daily call cap reached"
        return TT
    try:
        r = await client().get("https://api.tomtom.com/traffic/services/5/incidentDetails", timeout=20, headers=tt_headers(), params={
            "key": TT["key"], "bbox": TT_BBOX, "language": "en-GB", "timeValidityFilter": "present",
            "fields": "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,events{description,code,iconCategory},from,to,roadNumbers,length,delay,startTime,endTime}}}"})
        r.raise_for_status()
        out, jams = [], []
        TT["raw"], TT["raw_at"] = (r.json().get("incidents") or []), now          # V17 Forecast Centre: full geometry, re-parsed by forecast.py (no extra call)
        for inc in (r.json().get("incidents") or []):
            pr = inc.get("properties") or {}
            cat = pr.get("iconCategory")
            g = inc.get("geometry") or {}
            co = g.get("coordinates")
            if cat == 6 and g.get("type") == "LineString" and co and len(co) >= 2:      # V16.5: a traffic jam with its line, in the direction of travel
                try:
                    line = [(float(c[1]), float(c[0])) for c in co]
                except Exception:
                    line = []
                mag = pr.get("magnitudeOfDelay")
                if len(line) >= 2 and mag in (1, 2, 3):                                  # 0 unknown and 4 (road closed) are not jams
                    jams.append({"id": str(pr.get("id") or ""), "coords": line, "delay_s": pr.get("delay"), "length_m": pr.get("length"), "mag": mag,
                                 "from": (pr.get("from") or "").strip(), "to": (pr.get("to") or "").strip(), "road": " / ".join(pr.get("roadNumbers") or [])})
                continue
            if cat not in TT_CATS or not co:
                continue
            if g.get("type") == "LineString":
                pt = co[len(co) // 2]
            elif g.get("type") == "Point":
                pt = co
            else:
                continue
            try:
                lon, lat = float(pt[0]), float(pt[1])
            except Exception:
                continue
            ev = (pr.get("events") or [{}])[0]
            road = " / ".join(pr.get("roadNumbers") or []) or pr.get("from") or ""
            label = TT_CATS[cat]
            desc = (ev.get("description") or label).strip()
            out.append({"key": "tomtom|" + str(pr.get("id") or f"{lat:.5f},{lon:.5f},{cat}"), "type": "TomTom: " + label,
                        "message": f"{desc} ({road})" if road else desc, "lat": lat, "lon": lon, "road": road, "reported": None, "source": "tomtom"})
        TT.update(alerts=out, jams=jams, inc_ok_at=now, error=None)
    except Exception as e:
        TT["error"] = "incidents: " + tt_err(e)
    return TT


async def tomtom_flow(lat, lon):
    """Speed now vs free flow at one point (cached 10 min). -> {"cur","free","ratio","conf"} or None."""
    k = (round(lat, 4), round(lon, 4))
    now = time.time()
    hit = TT["flow"].get(k)
    if hit and now - hit[0] < TT_FLOW_TTL:
        return hit[1]
    if not tt_spend():
        TT["flow_error"] = "daily call cap reached"
        return hit[1] if hit else None
    try:
        r = await client().get("https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json", timeout=15, headers=tt_headers(),
                               params={"key": TT["key"], "point": f"{lat:.5f},{lon:.5f}", "unit": "KMPH"})
        r.raise_for_status()
        d = r.json().get("flowSegmentData") or {}
        cur, free = d.get("currentSpeed"), d.get("freeFlowSpeed")
        res = {"cur": float(cur), "free": float(free), "ratio": float(cur) / float(free), "conf": float(d.get("confidence") or 0)} if cur is not None and free else None
        TT["flow"][k] = (now, res)
        if len(TT["flow"]) > 400:
            for kk in sorted(TT["flow"], key=lambda x: TT["flow"][x][0])[:100]:
                TT["flow"].pop(kk, None)
        TT.update(flow_ok_at=now, flow_error=None)
        return res
    except Exception as e:
        TT["flow_error"] = "speed check: " + tt_err(e)
        return hit[1] if hit else None


@app.get("/api/tomtom/test")
async def api_tomtom_test():
    """One-click check of the TomTom key: calls both TomTom services once and shows the plain answer (the key itself is never shown)."""
    if not TT["key"]:
        return {"ok": False, "message": "TOMTOM_API_KEY is not set on the server."}
    out = {}
    tests = {"speed_check": ("https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json", {"point": "1.3521,103.8198", "unit": "KMPH"}),
             "incidents": ("https://api.tomtom.com/traffic/services/5/incidentDetails", {"bbox": TT_BBOX, "language": "en-GB", "timeValidityFilter": "present",
                                                                                          "fields": "{incidents{type,properties{iconCategory}}}"})}
    for name, (url, prm) in tests.items():
        try:
            r = await client().get(url, params=dict(prm, key=TT["key"]), timeout=20, headers=tt_headers())
            body = " ".join((r.text or "").split())[:160]
            out[name] = {"status": r.status_code, "ok": r.status_code == 200, "answer": body}
        except Exception as ex:
            out[name] = {"status": None, "ok": False, "answer": f"{type(ex).__name__}: {str(ex)[:100]}"}
    try:                                                                             # V16.9: one map tile (over Singapore, zoom 14)
        r = await client().get(f"https://api.tomtom.com/traffic/map/4/tile/flow/{TT_FLOW_STYLE}/14/12916/8130.png", params={"key": TT["key"], "tileSize": 256}, timeout=20, headers=tt_headers())
        out["map_tile"] = {"status": r.status_code, "ok": r.status_code == 200, "answer": (f"image, {len(r.content)} bytes" if r.status_code == 200 else " ".join((r.text or "").split())[:160])}
    except Exception as ex:
        out["map_tile"] = {"status": None, "ok": False, "answer": f"{type(ex).__name__}: {str(ex)[:100]}"}
    ok = all(v["ok"] for v in out.values())
    inv = any("InvalidReferer" in (v.get("answer") or "") for v in out.values())
    hint = "" if ok else ("The key is limited to certain websites (InvalidReferer). In TomTom: open your key, set Allowed Referers to * (or remove the limit). Or set TOMTOM_REFERER on Render to your site address." if inv else "Status 403 = the key is not enabled for this product. In your TomTom account: open your app, then tick 'Traffic Flow' and 'Traffic Incidents'. Or create a new key with both ticked." if any(v["status"] == 403 for v in out.values()) else "See the answer text for each service.")
    return {"ok": ok, "results": out, "hint": hint}


# ---- V16.9: TomTom traffic-flow map tiles (road colours on the Route Traffic map). The browser asks OUR server for tiles, so the key never reaches the browser.
# Tiles are cached 2 min, only tiles over Singapore are fetched, and TOMTOM_TILE_CAP (default 20000 per Singapore day) limits the calls.
TT_TILE = {"day": "", "n": 0, "cap": int(os.getenv("TOMTOM_TILE_CAP", "20000") or 20000), "cache": {}, "error": None, "ok_at": 0.0}
TT_TILE_TTL = 120
TT_FLOW_STYLE = os.getenv("TOMTOM_FLOW_STYLE", "relative").strip() or "relative"
_BLANK_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def _tile_over_sg(z, x, y):
    n = 2.0 ** z
    lon0, lon1 = x / n * 360.0 - 180.0, (x + 1) / n * 360.0 - 180.0
    lat_of = lambda ty: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    lat1, lat0 = lat_of(y), lat_of(y + 1)
    return not (lon1 < 103.55 or lon0 > 104.15 or lat1 < 1.12 or lat0 > 1.52)


@app.get("/api/tomtom/tile/{z}/{x}/{y}.png")
async def api_tomtom_tile(z: int, x: int, y: int):
    from fastapi.responses import Response
    blank = Response(_BLANK_PNG, media_type="image/png", headers={"Cache-Control": "public, max-age=60"})
    if not TT["key"] or z < 8 or z > 19 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z) or not _tile_over_sg(z, x, y):
        return blank
    now = time.time()
    hit = TT_TILE["cache"].get((z, x, y))
    if hit and now - hit[0] < TT_TILE_TTL:
        return Response(hit[1], media_type="image/png", headers={"Cache-Control": "public, max-age=60"})
    day = now_sgt().strftime("%Y-%m-%d")
    if TT_TILE["day"] != day:
        TT_TILE["day"], TT_TILE["n"] = day, 0
    if TT_TILE["n"] >= TT_TILE["cap"]:
        TT_TILE["error"] = "daily tile cap reached"
        return blank
    TT_TILE["n"] += 1
    try:
        r = await client().get(f"https://api.tomtom.com/traffic/map/4/tile/flow/{TT_FLOW_STYLE}/{z}/{x}/{y}.png", timeout=15,
                               headers=tt_headers(), params={"key": TT["key"], "tileSize": 256})
        r.raise_for_status()
        TT_TILE["cache"][(z, x, y)] = (now, r.content)
        TT_TILE.update(ok_at=now, error=None)
        if len(TT_TILE["cache"]) > 500:
            for k in sorted(TT_TILE["cache"], key=lambda k: TT_TILE["cache"][k][0])[:150]:
                TT_TILE["cache"].pop(k, None)
        return Response(r.content, media_type="image/png", headers={"Cache-Control": "public, max-age=60"})
    except Exception as e:
        TT_TILE["error"] = "map tiles: " + tt_err(e)
        return blank


async def tt_verify(e, P):
    """TomTom reading for one LTA congestion stretch -> {"live", "jam", "clear", "cur", "free", "conf"} or None (no answer)."""
    segs = (e.get("cur") or {}).get("segments") or []
    if not TT["key"] or not segs:
        return None
    s_ = segs[len(segs) // 2]
    f = await tomtom_flow((s_[0] + s_[2]) / 2.0, (s_[1] + s_[3]) / 2.0)
    if not f or f["conf"] < TT_MIN_CONF:
        return None
    return {"live": True, "cur": round(f["cur"], 1), "free": round(f["free"], 1), "conf": round(f["conf"], 2),
            "jam": f["ratio"] <= TT_SLOW_RATIO or f["cur"] <= P["verify_slow_kmh"], "clear": f["ratio"] >= TT_CLEAR_RATIO and f["cur"] > P["verify_slow_kmh"]}


# ---- V16.3 second-source verification of LTA congestion: our own buses (DataMall Bus Arrival positions) + Waze jams
TRV = {"obs": {}, "at": 0.0, "probed": 0, "error": None}


async def tr_verify_cycle():
    P = TR["params"]
    ridx = TR["ridx"]
    if not P.get("verify_on", 1) or ridx is None:
        return
    now = time.time()
    st = await static()
    await waze_state()
    wl = waze_live()
    evs = [e for e in TR["book"]["events"].values() if e["kind"] == "congestion" and e["status"] == "active" and e["match"]]
    evs.sort(key=lambda e: (-len(e["match"]), -(e["cur"].get("length_m") or 0)))
    evs = evs[:int(P.get("verify_max_events", 20))]
    ttres, ltares = {}, {}
    lidx = TR.get("ltaidx")
    for e in evs:
        if (e.get("cur") or {}).get("source") == "tomtom":                       # V16.5: TomTom found it first -> LTA speed bands check it
            if lidx is not None:
                ltares[e["id"]] = lidx.cover(e["cur"]["segments"], P)
    if TT["key"]:                                                                # V16.4: TomTom speed check of LTA-found stretches (cached, capped per day)
        for e in [x for x in evs if (x.get("cur") or {}).get("source") != "tomtom"][:TT_FLOW_MAX_EVENTS]:
            ttres[e["id"]] = await tt_verify(e, P)
    plan, stops_needed = [], set()
    for e in evs:
        key = max(e["match"].items(), key=lambda kv: kv[1]["overlap_km"])[0]
        svc, d = key.split("|")[0], int(key.split("|")[1])
        m = e["match"][key]
        rs = route_stops(st, svc, d) if st["stops"] else []
        if not rs:
            continue
        a, b = min(m["a_km"], m["b_km"]), max(m["a_km"], m["b_km"])
        down = next((x for x in rs if x.get("dist") is not None and x["dist"] >= b), None)       # first stop after the stretch: buses inside it are "next bus" there
        mid = next((x for x in rs if x.get("dist") is not None and a < x["dist"] < b), None)
        codes = [x["code"] for x in (down, mid) if x]
        if codes:
            plan.append((e, codes))
            stops_needed.update(codes)
    res = dict(zip(stops_needed, await asyncio.gather(*[arrivals_raw(c) for c in stops_needed]))) if stops_needed else {}
    now_dt = now_sgt()
    for e, codes in plan:
        ob = TRV["obs"].setdefault(e["id"], [])
        for key, m in list(e["match"].items())[:8]:
            svc, d = key.split("|")[0], int(key.split("|")[1])
            rs = route_stops(st, svc, d) if st["stops"] else []
            last_code = rs[-1]["code"] if rs else None
            loop = bool(rs) and rs[0]["code"] == last_code
            a, b = min(m["a_km"], m["b_km"]), max(m["a_km"], m["b_km"])
            for c in codes:
                r = res.get(c) or {}
                if r.get("_error"):
                    continue
                for sv in parse_services(r, now_dt, svc):
                    for bus in sv["buses"]:
                        if bus.get("lat") is None or (bus.get("dest") and not loop and last_code and bus["dest"] != last_code):
                            continue
                        pos = ridx.position(svc, d, bus["lat"], bus["lon"])
                        if not pos or pos[1] > 60 or not (a - 0.3 <= pos[0] <= b + 0.05):
                            continue
                        if not any(abs(o[0] - now) < 5 and o[1] == svc and o[2] == d and abs(o[3] - pos[0]) < 0.02 for o in ob):
                            ob.append((now, svc, d, pos[0]))
        cut = now - P["verify_window_min"] * 60 - 300
        TRV["obs"][e["id"]] = [o for o in ob if o[0] >= cut]
        m0 = max(e["match"].values(), key=lambda v: v["overlap_km"])
        speeds = traffic.bus_speed_samples(TRV["obs"][e["id"]], min(m0["a_km"], m0["b_km"]), max(m0["a_km"], m0["b_km"]), now, P)
        wz = WAZE["idx"].cover(e["cur"]["segments"], P) if wl else ({"live": False} if WAZE["url"] else None)
        e["verify"] = dict(traffic.verdict(speeds, wz, P, ttres.get(e["id"]), ltares.get(e["id"])), at=now)
    in_plan = {e["id"] for e, _ in plan}
    for e in evs:                                                                # V16.4: stretches with no bus data still get a Waze / TomTom verdict
        if e["id"] in in_plan:
            continue
        wz = WAZE["idx"].cover(e["cur"]["segments"], P) if wl else ({"live": False} if WAZE["url"] else None)
        if wz is not None or ttres.get(e["id"]) or ltares.get(e["id"]):
            e["verify"] = dict(traffic.verdict([], wz, P, ttres.get(e["id"]), ltares.get(e["id"])), at=now)
    live_ids = {e["id"] for e in evs}
    for k in [k for k in TRV["obs"] if k not in TR["book"]["events"] or TR["book"]["events"][k]["status"] != "active"]:
        TRV["obs"].pop(k, None)
    TRV.update(at=now, probed=len(plan), error=None)


async def tr_verify_loop():
    await asyncio.sleep(20)
    while True:
        try:
            await tr_verify_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            TRV["error"] = f"{type(e).__name__}"
        await asyncio.sleep(60)


async def tr_refresh(force=False):
    """One detection cycle: speed bands -> whole congestion stretches; incidents, road works and rain -> events; then every live event is matched to the services (and directions) it affects."""
    P = TR["params"]
    now = time.time()
    if not force and TR["last"] and now - TR["last"] < P["refresh_s"] * 0.9:
        return
    if TR["lock"] is None:
        TR["lock"] = asyncio.Lock()
    async with TR["lock"]:
        now = time.time()
        if not force and TR["last"] and now - TR["last"] < P["refresh_s"] * 0.9:
            return
        st, bs, inc, rain, (rw, rw_err), _tt = await asyncio.gather(static(), bands_state(), api_incidents(), api_rain(), tr_roadworks(now), tomtom_incidents())     # concurrent: a slow feed no longer holds up the others
        book = TR["book"]
        snap = int(CACHE["bands"][1]) if "bands" in CACHE else None
        count = traffic.counts(book, P, snap)
        if st["stops"] and st["routes"]:
            tr_lines_kick(st)
            key = (len(st["routes"]), len(st["stops"]), TR_LINES["at"], TR_LINES["n"])
            if TR["ridx"] is None or TR["ridx_key"] != key:
                TR["ridx"] = await asyncio.to_thread(traffic.RouteIndex, st["routes"], st["stops"], TR_LINES["lines"])
                TR["ridx_key"] = key
        if bs.get("idx") and (TR.get("roads_key") != id(bs["idx"])):
            TR["roads"] = await asyncio.to_thread(traffic.RoadLinks, bs["idx"].segs)       # the speed-band network by road name: ties incidents / road works to their road
            TR["roads_key"] = id(bs["idx"])
        feeds = {"bands": {"ok": bool(bs.get("idx")), "error": bs.get("error"), "segments": bs.get("usable"), "age_s": cache_age("bands")},
                 "incidents": {"ok": not inc.get("error"), "error": inc.get("error"), "count": len(inc.get("incidents", []))},
                 "roadworks": {"ok": not rw_err, "error": rw_err, "count": len(rw)},
                 "rain": {"ok": not rain.get("error"), "error": rain.get("error"), "gauges_wet": len(rain.get("stations", []))},
                 "waze": {"configured": bool(WAZE["url"]), "ok": waze_live(), "error": WAZE["error"], "jams": len(WAZE["jams"]), "alerts": len(WAZE["alerts"])},
                 "tomtom": {"configured": bool(TT["key"]), "ok": not (TT["error"] or TT["flow_error"]), "pending": TT["inc_ok_at"] == 0.0 and not TT["error"], "error": TT["error"] or TT["flow_error"], "alerts": len(TT["alerts"]), "calls_today": TT["n"], "cap": TT["cap"]},
                 "verify": {"on": bool(P.get("verify_on", 1)), "probed": TRV["probed"], "error": TRV["error"]},
                 "routes": {"ok": TR["ridx"] is not None, "services": len({k[0] for k in st["routes"]}) if st["routes"] else 0,
                            "real_road": TR["ridx"].n_exact if TR["ridx"] is not None else 0, "detail": TR_LINES["info"]}}
        # a feed that failed is skipped (never read as 'all clear'), so a broken feed cannot clear an active alert by mistake
        tt_used = bool(TT_FIRST and TT["inc_ok_at"] and now - TT["inc_ok_at"] < 15 * 60)           # V16.5: TomTom found jams recently -> it detects first
        if TT_FIRST and bs.get("idx") and TR.get("lta_key") != id(bs["idx"]):
            TR["ltaidx"] = await asyncio.to_thread(traffic.JamIndex, traffic.lta_jam_lines(bs["idx"].segs, P))       # LTA slow links, to check TomTom's jams
            TR["lta_key"] = id(bs["idx"])
        if tt_used:
            landmarks = [(s["lat"], s["lon"], s["name"]) for s in st["stops"].values()] if st["stops"] else []
            tstr = traffic.stretches_from_jams(TT["jams"], P, landmarks)
            traffic.update_congestion(book, tstr, now, P, count)
            feeds["tomtom"]["first"] = True
            feeds["tomtom"]["jams"] = len(tstr)
        elif bs.get("idx"):
            # build_stretches is the expensive part of a refresh; LTA speed bands only change every ~5 min (TTL_BANDS), so recomputing on
            # every refresh_s (60s) cycle wastes most of that work. Reuse the last result while the underlying band snapshot is unchanged.
            bkey = (id(bs["idx"]), len(st["stops"]))
            if TR["stretch_cache"] and TR["stretch_cache"][0] == bkey:
                stretches = TR["stretch_cache"][1]
            else:
                landmarks = [(s["lat"], s["lon"], s["name"]) for s in st["stops"].values()]
                stretches = await asyncio.to_thread(traffic.build_stretches, bs["idx"].segs, P, landmarks)
                TR["stretch_cache"] = (bkey, stretches)
            traffic.update_congestion(book, stretches, now, P, count)
            feeds["bands"]["stretches"] = len(stretches)
        if not inc.get("error"):
            items = []
            for x in inc.get("incidents", []):
                m = re.search(r"\((\d{1,2})/(\d{1,2})\)\s*(\d{1,2}:\d{2})", x.get("message") or "")
                items.append({"key": f"{x['type']}|{round(x['lat'], 4)}|{round(x['lon'], 4)}", "type": x["type"], "message": x.get("message") or "", "lat": x["lat"], "lon": x["lon"], "reported": m.group(3) if m else None})
            # V16.3: Waze user reports (accidents, closures, on-road hazards) join the incident list, unless LTA already reports one within 200 m
            if WAZE["url"]:
                await waze_state()
                if time.time() - WAZE["ok_at"] < 10 * 60:                  # a failed Waze fetch keeps the last good list, so it cannot clear Waze incidents by mistake
                    for w in WAZE["alerts"]:
                        if not any(traffic.hav_m(w["lat"], w["lon"], x["lat"], x["lon"]) < 200 for x in items if not str(x["key"]).startswith("waze|")):
                            items.append(w)
            # V16.4: TomTom incidents (accidents, closures, breakdowns, flooding), unless another source already reports one within 200 m
            if TT["key"]:
                await tomtom_incidents()
                if time.time() - TT["inc_ok_at"] < 15 * 60:                    # a failed TomTom call keeps the last good list
                    for w in TT["alerts"]:
                        if not any(traffic.hav_m(w["lat"], w["lon"], x["lat"], x["lon"]) < 200 for x in items if not str(x["key"]).startswith("tomtom|")):
                            items.append(w)
            traffic.update_points(book, "incident", items, now, P, count)
        if not rw_err:
            traffic.update_points(book, "roadworks", rw[:300], now, P, count)
        if not rain.get("error"):
            traffic.update_weather(book, traffic.rain_cells(rain.get("stations", []), P), now, P, count)
        traffic.tick(book, now, P, snap)
        if TR["ridx"] is not None:
            await asyncio.to_thread(traffic.refresh_matches, book, TR["ridx"], P, TR.get("roads"))
        TR["last"], TR["feeds"] = now, feeds
        tr_save()


async def tr_ensure_fresh(force):
    """Kick tr_refresh if the data is stale, WITHOUT making the caller wait for a slow LTA round trip: a refresh already running is left to finish on its own (the caller uses
    whatever is in TR now), and a fresh one is only started in the background. The single exception is the very first request after startup, which has nothing to show yet:
    that one waits, but never more than a few seconds, so the page always answers quickly even while LTA is slow."""
    P = TR["params"]
    stale = force or not TR["last"] or (time.time() - TR["last"] >= P["refresh_s"] * 0.9)
    if not stale or (TR["lock"] is not None and TR["lock"].locked()):
        return
    first = TR["last"] == 0
    task = asyncio.create_task(tr_refresh(force=True))
    if first:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
        except Exception:
            pass


async def tr_loop():
    while True:
        try:
            await tr_refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(15, TR["params"]["refresh_s"]))


def tr_event_public(e, now):
    c = e["cur"]
    out = {"id": e["id"], "kind": e["kind"], "status": e["status"], "start": tr_hhmm(e["first_seen"]), "alerted": tr_hhmm(e["alert_time"]), "location": traffic.location_text(e),
           "services": sorted({k.split("|")[0] for k in e["match"]}, key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s))}
    if e["kind"] == "congestion":
        out.update(verify=e.get("verify"))
        out.update(segments=c["segments"], length_km=round(c["length_m"] / 1000, 2), avg_kmh=round(c["avg_kmh"], 1), min_kmh=round(c["min_kmh"], 1), ref_kmh=c["ref_kmh"], road=c["road"], center=c["center"],
                   very_slow_pct=round(c["very_slow_pct"]), from_=c["from"], to=c["to"], peak_min_kmh=round(e["peak"].get("min_kmh", c["min_kmh"]), 1), peak_len_km=round(e["peak"].get("max_len_m", c["length_m"]) / 1000, 2))
    elif e["kind"] == "incident":
        out.update(lat=c["lat"], lon=c["lon"], type=c.get("type"), message=c.get("message"), reported=c.get("reported"), road=c.get("road_used"), geo=c.get("geo"), basis=c.get("match_basis"),
                   source=c.get("source") or "lta")
    elif e["kind"] == "roadworks":
        out.update(lat=c.get("lat"), lon=c.get("lon"), road=c.get("road"), geo=c.get("geo"), basis=c.get("match_basis"), approx_pos=bool(c.get("approx_pos")), start_date=tr_hhmm(c.get("start_epoch")) and datetime.fromtimestamp(c["start_epoch"], SGT).strftime("%d %b %H:%M"),
                   end_date=(datetime.fromtimestamp(c["end_epoch"], SGT).strftime("%d %b %H:%M") if c.get("end_epoch") else None), other=c.get("other"))
    else:
        out.update(lat=c["lat"], lon=c["lon"], radius_km=c.get("radius_km"), level=c.get("level"), max_mm=c.get("max_mm"), n=c.get("n"), name=c.get("name"), stations=c.get("stations"))
    return out


def tr_alert_public(a):
    o = dict(a)
    o["start_hhmm"], o["acked_hhmm"] = tr_hhmm(a["start"]), tr_hhmm(a["acked_time"])
    for k, n in (("length_km", 2), ("speed_kmh", 0), ("duration_min", 0), ("overlap_km", 2), ("route_pct", 1), ("route_km", 1), ("a_km", 2), ("b_km", 2), ("delay_min", 1), ("score", 0), ("starts_in_min", 0)):
        if o.get(k) is not None:
            o[k] = round(o[k], n)
    return o


async def tr_hw_map(rows, now_dt):
    keys = {(a["svc"], a["dir"]) for a in rows}
    if not keys:
        return {}
    try:
        fq = await freq_table()
    except Exception:
        return {}
    out = {}
    for svc, d in keys:
        hw, _ = bb_resolve_hw(svc, d, now_dt, fq)
        if hw:
            out[(svc, d)] = hw
    return out


@app.get("/api/cameras")
async def api_cameras(service: str = "", direction: int = 1, km: float = 0.35, refresh: int = 0):
    """LTA DataMall Traffic Images: still photos (LTA publishes no video), refreshed by LTA every few minutes.
    With a service: only the cameras within `km` of that route, in route order, each with its nearest bus stop. refresh=1 asks for a
    fresh list (the image links are short-lived signed URLs) but never more often than every 30 s, to protect the LTA quota."""
    if refresh:
        hit = CACHE.get("cameras")
        if hit and time.time() - hit[1] > 30:
            CACHE.pop("cameras", None)
    cs = await cameras_state()
    cams = cs.get("cams") or []
    hit = CACHE.get("cameras")
    fetched = hit[1] if hit else None
    st = await static()
    svc = service.strip().upper()
    km = max(0.1, min(3.0, km))
    out, nearby = [], []
    if svc:
        stops = route_stops(st, svc, direction) if st["stops"] else []
        if not stops:
            return {"cameras": [], "error": "Route not available", "total": len(cams), "fetched": tr_hhmm(fetched) if fetched else None}
        line = cached_line(svc, direction, stops)
        cum = [0.0]
        for a, b in zip(line, line[1:]):
            cum.append(cum[-1] + hav_km(a[0], a[1], b[0], b[1]))
        for c in cams:
            d = min_dist_km(c["lat"], c["lon"], line)
            if d > km:
                if d <= 5.0:
                    ns = min(stops, key=lambda x: hav_km(c["lat"], c["lon"], x["lat"], x["lon"]))
                    nearby.append({"id": c["id"], "lat": c["lat"], "lon": c["lon"], "image": c["link"], "taken": c.get("taken"), "dist_km": round(d, 2),
                                   "desc": c.get("desc"), "desc_known": c.get("desc_known"), "road": c.get("road"),
                                   "near": {"code": ns["code"], "name": ns["name"], "road": ns["road"], "km": round(hav_km(c["lat"], c["lon"], ns["lat"], ns["lon"]), 2)}})
                continue
            k = min(range(len(line)), key=lambda i: (line[i][0] - c["lat"]) ** 2 + (line[i][1] - c["lon"]) ** 2)
            ns = min(stops, key=lambda x: hav_km(c["lat"], c["lon"], x["lat"], x["lon"]))
            out.append({"id": c["id"], "lat": c["lat"], "lon": c["lon"], "image": c["link"], "taken": c.get("taken"), "dist_km": round(d, 2), "route_km": round(cum[k], 2),
                        "desc": c.get("desc"), "desc_known": c.get("desc_known"), "road": c.get("road"),
                        "near": {"code": ns["code"], "name": ns["name"], "road": ns["road"], "km": round(hav_km(c["lat"], c["lon"], ns["lat"], ns["lon"]), 2)}})
        out.sort(key=lambda x: x["route_km"])
        nearby = sorted(nearby, key=lambda x: x["dist_km"])[:4]
    else:
        allst = list(st["stops"].values()) if st["stops"] else []
        for c in cams:
            ns = min(allst, key=lambda x: hav_km(c["lat"], c["lon"], x["lat"], x["lon"])) if allst else None
            out.append({"id": c["id"], "lat": c["lat"], "lon": c["lon"], "image": c["link"], "taken": c.get("taken"), "desc": c.get("desc"), "desc_known": c.get("desc_known"), "road": c.get("road"),
                        "near": {"code": ns["code"], "name": ns["name"], "road": ns["road"],
                        "km": round(hav_km(c["lat"], c["lon"], ns["lat"], ns["lon"]), 2)} if ns else None})
    live_ids = {c["id"] for c in cams}
    annex = {"listed": len(ANNEX_G), "live": len(live_ids & set(ANNEX_G)),
             "missing": sorted(set(ANNEX_G) - live_ids), "not_in_annex": sorted(live_ids - set(ANNEX_G))}
    return {"cameras": out, "nearby": nearby, "km": km, "total": len(cams), "annex_g": annex, "fetched": tr_hhmm(fetched) if fetched else None, "fetched_epoch": fetched,
            "source": cs.get("source") or "LTA DataMall \u00b7 Traffic Images", "error": cs.get("error") if not cams else None, "lta_error": cs.get("raw_error"),
            "diag": cs.get("diag"), "sources": cs.get("sources"),
            "line_approx": bool(svc) and not CACHE.get(geom_key(svc, direction, route_stops(st, svc, direction))) if st["stops"] else None, "note": "Still photos only: LTA publishes no traffic video. Images are updated by LTA about every 1-5 minutes."}


@app.get("/api/traffic/overview")
async def api_tr_overview(services: str = "", direction: int = 0, horizon: int = 0, types: str = "", status: str = "", operators: str = "", min_delay: str = "", refresh: int = 0):
    await tr_ensure_fresh(bool(refresh))
    P, book, now = TR["params"], TR["book"], time.time()
    svcs = [s for s in re.split(r"[,\s]+", services.upper()) if s]
    ops = {o for o in re.split(r"[,\s]+", operators.upper()) if o}
    if ops:
        fq = await freq_table()
        op_svcs = {svc for (svc, d), r in fq["freqs"].items() if r["Operator"] in ops}
        svcs = sorted(set(svcs) & op_svcs) if svcs else sorted(op_svcs)
        if not svcs:                                                # named operator(s) run no service the person typed, or (with none typed) no service at all
            return {"rows": [], "cards": {k: {"events": 0, "services": 0, "unacknowledged": 0, "vs_last_hour": 0} for k in traffic.KINDS}, "total": {"services": 0, "alerts": 0, "unacknowledged": 0},
                    "events": [], "feeds": TR["feeds"], "updated": tr_hhmm(TR["last"]), "updated_epoch": TR["last"], "refresh_s": P["refresh_s"], "model": traffic.MODEL_VERSION, "updates": book.get("updates", 0),
                    "params": {k: P[k] for k in ("persist_updates", "clear_updates", "min_len_m", "congest_kmh", "very_slow_kmh", "normal_kmh")}, "now": tr_hhmm(now)}
    kinds = [k for k in re.split(r"[,\s]+", types.lower()) if k in traffic.KINDS] or list(traffic.KINDS)
    stat = [k for k in re.split(r"[,\s]+", status.lower()) if k in ("unacknowledged", "acknowledged", "cleared")] or ["unacknowledged", "acknowledged"]
    md = num(min_delay) if min_delay.strip() else None
    full = traffic.overview(book, P, now, svcs, direction, max(0, min(int(horizon), 120)), kinds, stat, min_delay=md)
    hw = await tr_hw_map(full["rows"], now_sgt())
    ov = traffic.overview(book, P, now, svcs, direction, max(0, min(int(horizon), 120)), kinds, stat, hw, min_delay=md)
    ids = {a["event"] for a in ov["rows"]}
    return {"rows": [tr_alert_public(a) for a in ov["rows"]], "cards": ov["cards"], "total": ov["total"], "events": [tr_event_public(book["events"][i], now) for i in ids if i in book["events"]],
            "feeds": TR["feeds"], "updated": tr_hhmm(TR["last"]), "updated_epoch": TR["last"], "loading": TR["last"] == 0, "refresh_s": P["refresh_s"], "model": traffic.MODEL_VERSION, "updates": book["updates"],
            "params": {k: P[k] for k in ("persist_updates", "clear_updates", "min_len_m", "congest_kmh", "very_slow_kmh", "normal_kmh", "min_delay_min")}, "now": tr_hhmm(now)}


@app.get("/api/traffic/services")
async def api_tr_services():
    st = await static()
    fq = await freq_table()
    out = []
    ops = set()
    for svc, dirs in st["dirs"].items():
        svc_ops = sorted({fq["freqs"][(svc, d)]["Operator"] for d in dirs if (svc, d) in fq["freqs"] and fq["freqs"][(svc, d)]["Operator"]})
        ops.update(svc_ops)
        out.append({"svc": svc, "dirs": dirs, "operators": svc_ops})
    out.sort(key=lambda x: (not x["svc"][:1].isdigit(), int(re.match(r"\d+", x["svc"]).group()) if re.match(r"\d+", x["svc"]) else 0, x["svc"]))
    return {"services": out, "operators": sorted(ops)}


@app.get("/api/traffic/route")
async def api_tr_route(service: str = "", direction: int = 1):
    svc = service.strip().upper()
    st = await static()
    stops = route_stops(st, svc, direction) if st["stops"] else []
    if not stops:
        return {"line": [], "stops": [], "error": "Route not available"}
    # V16.1: the SAME real-road line the traffic engine matched against (so what is drawn is what was matched), with the route km of every vertex.
    # Before V16.1 this returned whatever geometry happened to be cached - on a cold cache that was straight stop-to-stop lines across blocks.
    fit, source = TR_LINES["lines"].get((svc, direction)), "busrouter.sg (fitted to LTA stops)"
    if not fit:
        g = await route_geometry(svc, direction, stops)
        source = g.get("source") or "stops"
        try:
            fit = routegeom.fast_fit([g["line"]], stops) if g.get("line") and source != "stops" else None
        except Exception:
            fit = None
        if not fit:
            fit = {"line": [(s["lat"], s["lon"]) for s in stops], "km": [s["dist"] for s in stops]}
            source = "stops (road geometry unavailable - straight lines between stops)"
    return {"service": svc, "direction": direction, "line": [[round(p[0], 6), round(p[1], 6)] for p in fit["line"]], "km": [round(k, 3) if k is not None else None for k in fit["km"]],
            "stops": [[s["lat"], s["lon"], s["name"], s["dist"]] for s in stops], "source": source, "real_road": not source.startswith("stops")}


@app.get("/api/traffic/detail")
async def api_tr_detail(alert: str = "", current_hw: str = ""):
    """The selected alert: the disruption, the buses relative to it, the headway they will make, and the regulation the simulator recommends."""
    await tr_ensure_fresh(False)
    P, book, now = TR["params"], TR["book"], time.time()
    parts = alert.split(":")
    if len(parts) != 3 or parts[0] not in book["events"] or f"{parts[1]}|{parts[2]}" not in book["events"][parts[0]]["match"]:
        return JSONResponse({"error": "That alert is no longer active."}, status_code=404)
    e, svc, d = book["events"][parts[0]], parts[1], int(parts[2])
    m = e["match"][f"{svc}|{d}"]
    now_dt = now_sgt()
    fq0 = await freq_table()
    hw0, hw_src = bb_resolve_hw(svc, d, now_dt, fq0)
    row = next((a for a in traffic.alerts(book, P, now, {(svc, d): hw0} if hw0 else None) if a["id"] == alert), None)
    cs = await cameras_state()
    pt = e["cur"].get("center") or [e["cur"].get("lat"), e["cur"].get("lon")]
    cam, cam_dist = nearest_camera(cs.get("cams") or [], pt[0] if pt else None, pt[1] if pt else None, P["camera_km"]) if pt else (None, None)
    camera = {"id": cam["id"], "image": cam["link"], "dist_km": round(cam_dist, 2)} if cam else None
    out = {"alert": tr_alert_public(row) if row else None, "event": tr_event_public(e, now), "sched_hw": hw0, "sched_hw_src": hw_src, "buses": [], "impact": None, "regulation": None, "restore": None, "chart": None,
           "camera": camera, "camera_error": cs.get("error") if not camera and not cs.get("cams") else None,
           "affected_services": [{"svc": k.split("|")[0], "dir": int(k.split("|")[1]), "pct": round(v["pct"], 1)} for k, v in sorted(e["match"].items(), key=lambda kv: (-kv[1]["pct"], kv[0]))][:40]}
    if TR["ridx"] is None or e["status"] != "active":
        out["recommendations"] = traffic.recommend(row or {"svc": svc, "dir": d}, None, None, None, None, P)
        return out
    bj = await api_buses(svc, d)
    meta = TR["ridx"].meta.get((svc, d))
    buses = []
    for b in bj.get("buses", []):
        if b.get("lat") is None:
            continue
        pos = TR["ridx"].position(svc, d, b["lat"], b["lon"])
        if pos is None or pos[1] > 450:
            continue
        n_last = (meta["n"] - 1) if meta else None
        eta = (b.get("etasf") or {}).get(n_last)
        buses.append({"id": b["id"], "pos_km": pos[0], "eta_terminal_min": eta, "lat": b["lat"], "lon": b["lon"], "near": (b.get("near") or {}).get("name"), "load": b.get("load")})
    delay = (row or {}).get("delay_min") or 0.0
    cb = traffic.classify_buses(buses, m["a_km"], m["b_km"], delay, P)
    H = hw0 or None
    h_note = hw_src
    if not H:
        etas = sorted(b["eta_terminal_min"] for b in cb if b.get("eta_terminal_min") is not None)
        gaps = [y - x for x, y in zip(etas, etas[1:])]
        H = round(sum(gaps) / len(gaps), 1) if gaps else 10.0
        h_note = "scheduled headway unknown: the average of the current predicted headways" if gaps else "scheduled headway unknown: 10 min assumed"
    out["sched_hw"], out["sched_hw_src"] = H, h_note
    out["buses"] = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in b.items() if k in ("id", "pos_km", "state", "dist_to_km", "tti_min", "delay_min", "eta_terminal_min", "lat", "lon", "near", "load")} for b in cb]
    if cb and meta:
        imp = traffic.headway_impact(cb, H, meta["km"], P)
        cur_hw = num(current_hw)
        reg = traffic.regulation_options(imp["rows"], H, P)
        rs = traffic.restore_check(imp["rows"], meta["km"], H, cur_hw, P)
        lay = P["min_layover_min"]
        clock0 = now_dt.hour * 60 + now_dt.minute + now_dt.second / 60.0
        deps = sorted(r["arr1"] + lay for r in imp["rows"])
        chart = None
        if reg.get("no_action"):
            chart = {"x": [round(clock0 + t, 1) for t in deps], "sched": H, "noadj": [round(v, 1) for v in reg["no_action"]["hw"]], "reg": [round(v, 1) for v in reg["best"]["hw"]] if reg.get("best") else None,
                     "from": round(clock0 + min([b["tti_min"] for b in cb if b["state"] == "approaching"] + [0.0]), 1), "to": round(clock0 + max(r["arr1"] for r in imp["rows"]), 1), "now": round(clock0, 1)}
        out["impact"] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in imp.items() if k != "rows"}
        out["impact"]["rows"] = [{"id": r["id"], "state": r["state"], "arr0": round(r["arr0"], 1), "arr1": round(r["arr1"], 1), "hw0": _r1(r["hw0"]), "hw1": _r1(r["hw1"]), "delay": round(r["delay_min"], 1), "eta_src": r["eta_src"]} for r in imp["rows"]]
        out["regulation"] = {k: v for k, v in reg.items() if k != "options"}
        out["regulation"]["options"] = [{"stretch_min": o["stretch_min"], "max_hw": round(o["max_hw"], 1), "rms": round(o["rms"], 2)} for o in reg.get("options", [])]
        out["restore"] = rs
        out["chart"] = chart
        out["recommendations"] = traffic.recommend(row or {"svc": svc, "dir": d}, imp, reg, rs, cur_hw, P)
    else:
        out["recommendations"] = traffic.recommend(row or {"svc": svc, "dir": d}, None, None, None, None, P)
        out["buses_note"] = "No live buses could be placed on this route right now." if not bj.get("error") else bj["error"]
    return out


def _r1(x):
    return None if x is None else round(x, 1)


@app.post("/api/traffic/ack")
async def api_tr_ack(request: Request):
    """ACKNOWLEDGE: the controller has seen these alerts. They stay in the list (marked) and are raised again if the condition worsens."""
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
        ids = [str(x) for x in body.get("alerts", [])][:500]
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Body must be JSON with a list of alert ids."}, status_code=400)
    by = re.sub(r"[^\w .@-]", "", str(body.get("by") or "")).strip()[:40] or "controller"
    book = TR["book"]
    done = traffic.acknowledge(book, ids, by, time.time())
    for aid in done:
        p = aid.split(":")
        bb_sql("INSERT INTO alert_acknowledgement(alert_id, event_id, service, direction, ack_time, ack_by, condition) VALUES (?,?,?,?,?,?,?)", (aid, p[0], p[1], int(p[2]), time.time(), by, json.dumps(book["acks"][aid]["snap"])))
    tr_save()
    return {"ok": True, "acked": len(done), "ids": done, "skipped": len(ids) - len(done), "by": by}


@app.get("/api/traffic/log")
async def api_tr_log(limit: int = 100):
    rows = bb_sql("SELECT * FROM alert_acknowledgement ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),), fetch=True)
    return {"acks": [{"alert": r["alert_id"], "service": r["service"], "direction": r["direction"], "time": tr_hhmm(r["ack_time"]), "by": r["ack_by"], "condition": r["condition"]} for r in rows]}


@app.get("/api/traffic/settings")
async def api_tr_settings():
    return {"params": TR["params"], "defaults": traffic.PARAMS, "ranges": {k: list(v) for k, v in traffic.RANGES.items()}, "model": traffic.MODEL_VERSION, "roadworks_path": EP_ROADWORKS}


@app.post("/api/traffic/settings")
async def api_tr_settings_post(request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    if body.get("reset"):
        TR["params"] = dict(traffic.PARAMS)
        bb_sql("DELETE FROM traffic_setting")
        return {"ok": True, "params": TR["params"]}
    clean, errs = traffic.validate_params(body.get("params") or {})
    if errs:
        return JSONResponse({"error": "; ".join(errs)}, status_code=400)
    TR["params"].update(clean)
    for k, v in clean.items():
        bb_sql("INSERT OR REPLACE INTO traffic_setting(k, v) VALUES (?, ?)", (k, float(v)))
    return {"ok": True, "params": TR["params"]}


# =========================================================================== V16.0 Command Platform shell
# New pages (login, command centre, settings), the /recovery alias, and read-only system status for the shared command bar.
# Nothing here changes an engine, a collector or a stored setting.
import auth  # noqa: E402  (authentication boundary - see auth.py; no built-in credentials)


def _page(name):
    # no-cache: browsers must re-check the page on every visit, so a redeploy is picked up immediately
    return HTMLResponse((HERE / name).read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _page("login.html")


@app.get("/command", response_class=HTMLResponse)
async def command_page():
    """V17: the Command Centre is now the Forecast Centre (the home page). The previous page stays at /command/classic."""
    return _page("forecast.html")


@app.get("/command/classic", response_class=HTMLResponse)
async def command_classic_page():
    return _page("command.html")


@app.get("/forecast", response_class=HTMLResponse)
async def forecast_page():
    return _page("forecast.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _page("settings.html")


@app.get("/recovery", response_class=HTMLResponse)
async def recovery_page():
    """Recovery Decision Engine (formerly Timetable Optimiser). /halfway/timetable keeps working for old bookmarks."""
    return _page("halfway.html")


# ---- V16.8: hide pages for everyone (Settings > Pages). Saved in the database; HIDDEN_PAGES (comma list, e.g. cameras,running) is the default when nothing is saved,
# and survives a Render restart. The list is put at the top of /app-shell.js, so every page knows it before it draws its menu.


@app.get("/app-shell.js")
async def app_shell_js():
    from fastapi.responses import Response
    head = "window.__SGTP_HIDDEN=" + json.dumps(SITE["hidden"]) + ";window.__SGTP_BLOCK=" + ("true" if SITE["block"] else "false") + ";\n"
    return Response(head + (HERE / "app_shell.js").read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/api/site/pages")
async def api_site_pages():
    return {"hidden": SITE["hidden"], "block": SITE["block"], "pages": list(SITE_PAGE_IDS)}


@app.post("/api/site/pages")
async def api_site_pages_save(request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    hidden = body.get("hidden", SITE["hidden"])
    if not isinstance(hidden, list) or any(x not in SITE_PAGE_IDS for x in hidden):
        return JSONResponse({"error": "Unknown page name."}, status_code=400)
    SITE["hidden"] = [x for x in SITE_PAGE_IDS if x in hidden]
    if "block" in body:
        SITE["block"] = bool(body["block"])
    bb_sql("INSERT OR REPLACE INTO site_setting(k, v) VALUES ('hidden_pages', ?)", (",".join(SITE["hidden"]),))
    bb_sql("INSERT OR REPLACE INTO site_setting(k, v) VALUES ('block_hidden', ?)", ("1" if SITE["block"] else "0",))
    return {"ok": True, "hidden": SITE["hidden"], "block": SITE["block"], "saved": bool(BB.get("db_ok"))}


# ---- authentication hooks (open access until AUTH_PROVIDER + AUTH_SECRET are set)
@app.get("/api/auth/config")
async def api_auth_config():
    return auth.config()


@app.get("/api/auth/session")
async def api_auth_session(request: Request):
    s = auth.read(request.cookies.get(auth.COOKIE, ""))
    cfg = auth.config()
    return {"enabled": cfg["enabled"], "mode": cfg["mode"], "authenticated": bool(s), "user": s["u"] if s else None,
            "since": datetime.fromtimestamp(s["iat"], SGT).isoformat(timespec="seconds") if s else None,
            "expires": datetime.fromtimestamp(s["exp"], SGT).isoformat(timespec="seconds") if s else None}


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    ident, pw = str(body.get("identifier") or "").strip()[:120], str(body.get("password") or "")
    if not auth.config()["enabled"]:
        return JSONResponse({"error": "No authentication provider is configured on this server.", "mode": "open"}, status_code=501)
    if not ident or not pw:
        return JSONResponse({"error": "Enter your Staff ID and password."}, status_code=400)
    user, err = await asyncio.to_thread(auth.authenticate, ident, pw)
    if not user:
        await asyncio.sleep(0.6)                                             # slow down guessing
        return JSONResponse({"error": err}, status_code=401)
    tok = auth.issue(user)
    r = JSONResponse({"ok": True, "user": auth.read(tok)["u"]})
    r.set_cookie(auth.COOKIE, tok, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                 max_age=int(auth.SESSION_HOURS * 3600) if body.get("remember") else None)
    return r


@app.post("/api/auth/logout")
async def api_auth_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


# ---- V16.6: the login gate. When the login is on, every page and API needs a signed-in session (except the login page itself, its assets and the health check).
# AUTH_GATE=0 keeps the login page but does not block anything.
_GATE_OPEN = ("/login", "/api/auth/", "/api/health", "/app-shell.js", "/design-system.css", "/basemap.js", "/nav-registry.js", "/favicon", "/brand/", "/horizon-tokens")

# V16.27: a TV / LED panel shows the live diversions without signing in (a login session expires). Set DIVERSION_TV_KEY in the
# Render Environment and open /diversion/tv?key=<that key> once on the TV: a TV cookie then opens ONLY the TV page, its script,
# the 3D map script and the read-only list of live diversions. Without DIVERSION_TV_KEY the TV page needs a normal login.
TV_KEY = os.getenv("DIVERSION_TV_KEY", "").strip()
_TV_PATHS = ("/diversion/tv", "/diversion-tv.js", "/api/dv2/live", "/map3d.js", "/brand/", "/design-system.css")


def _tv_token():
    return hashlib.sha256(("sentry-tv:" + TV_KEY).encode()).hexdigest()


def tv_access(request):
    """0 = no TV access, 1 = the TV cookie, 2 = the key in the link (the cookie is set)"""
    if not TV_KEY or request.url.path not in _TV_PATHS or request.method != "GET":
        return 0
    import hmac
    if hmac.compare_digest(request.query_params.get("key", ""), TV_KEY):
        return 2
    return 1 if hmac.compare_digest(request.cookies.get("sentry_tv", ""), _tv_token()) else 0


@app.middleware("http")
async def login_gate(request: Request, call_next):
    if not auth.config()["enabled"] or os.getenv("AUTH_GATE", "1") == "0":
        return await call_next(request)
    path = request.url.path
    if request.method == "OPTIONS" or path.startswith(_GATE_OPEN) or auth.read(request.cookies.get(auth.COOKIE, "")):
        return await call_next(request)
    tv = tv_access(request)
    if tv:
        resp = await call_next(request)
        if tv == 2:
            resp.set_cookie("sentry_tv", _tv_token(), max_age=400 * 86400, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
        return resp
    if path.startswith("/api/"):
        return JSONResponse({"error": "Sign in required.", "login": "/login"}, status_code=401)
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    nxt = path + ("?" + request.url.query if request.url.query else "")
    return RedirectResponse("/login?next=" + quote(nxt, safe="/?=&"), status_code=303)


# ---- system status: what the server actually knows about each data feed (never invented)
def _feed(key, ttl, label, source, prefix=False):
    if prefix:
        hits = [(k, v) for k, v in CACHE.items() if k.startswith(key)]
        hit = max(hits, key=lambda kv: kv[1][1])[1] if hits else None
    else:
        hit = CACHE.get(key)
    if not hit:
        return {"id": key.rstrip(":"), "label": label, "source": source, "status": "idle", "age_s": None, "last": None, "refresh_s": ttl,
                "detail": "Not requested since the server started (feeds load when a page needs them)."}
    age = int(time.time() - hit[1])
    val = hit[2]
    err = val.get("error") if isinstance(val, dict) else None
    status = "error" if err and not (isinstance(val, dict) and (val.get("rows") or val.get("stops") or val.get("idx"))) else ("ok" if age <= max(3 * ttl, ttl + 120) else "stale")
    return {"id": key.rstrip(":"), "label": label, "source": source, "status": status, "age_s": age, "refresh_s": ttl,
            "last": datetime.fromtimestamp(hit[1], SGT).isoformat(timespec="seconds"), "detail": str(err)[:200] if err else None}


@app.get("/api/system/status")
async def api_system_status():
    now = time.time()
    feeds = [
        _feed("static", TTL_STATIC, "Bus routes & stops", "LTA DataMall"),
        _feed("arr:", TTL_ARRIVAL, "Bus arrival", "LTA DataMall", prefix=True),
        _feed("bands", TTL_BANDS, "Traffic speed bands", "LTA DataMall"),
        _feed("incidents", TTL_INCIDENTS, "Traffic incidents", "LTA DataMall"),
        _feed("roadworks", TTL_ROADWORKS, "Road works", "LTA DataMall"),
        _feed("cameras", TTL_CAMERAS, "Traffic images", "LTA DataMall + data.gov.sg"),
        _feed("rain", TTL_RAIN, "Rainfall (weather)", "NEA via data.gov.sg"),
    ]
    if WAZE["url"]:
        feeds.append({"id": "waze", "label": "Waze traffic (jams & user reports)", "source": "Waze for Cities", "refresh_s": WAZE_TTL,
                      "status": "ok" if waze_live() else ("error" if WAZE["error"] else "idle"), "age_s": int(now - WAZE["ok_at"]) if WAZE["ok_at"] else None,
                      "last": datetime.fromtimestamp(WAZE["ok_at"], SGT).isoformat(timespec="seconds") if WAZE["ok_at"] else None,
                      "detail": WAZE["error"] or f"{len(WAZE['jams'])} jams, {len(WAZE['alerts'])} usable reports"})
    if TT["key"]:
        feeds.append({"id": "tomtom", "label": "TomTom traffic (speed check & incidents)", "source": "TomTom Traffic API", "refresh_s": TT_INC_TTL,
                      "status": "ok" if tt_live() and not TT["error"] else ("error" if (TT["error"] or TT["flow_error"]) else "idle"),
                      "age_s": int(now - TT["inc_ok_at"]) if TT["inc_ok_at"] else None,
                      "last": datetime.fromtimestamp(TT["inc_ok_at"], SGT).isoformat(timespec="seconds") if TT["inc_ok_at"] else None,
                      "detail": TT["error"] or TT["flow_error"] or (("finds congestion first; " if TT_FIRST else "") + f"{len(TT['jams'])} jams, {len(TT['alerts'])} incidents; {TT['n']} of {TT['cap']} calls and {TT_TILE['n']} of {TT_TILE['cap']} map tiles used today" + (f"; map tiles: {TT_TILE['error']}" if TT_TILE["error"] else ""))})
    bb_alive = bool(BB.get("loop_at")) and now - BB["loop_at"] < 3 * BB["params"]["refresh_sec"]
    engines = [
        {"id": "bunching", "label": "Bunching & gap collector", "status": "ok" if bb_alive else ("idle" if not BB.get("loop_at") else "stale"),
         "last": datetime.fromtimestamp(BB["loop_at"], SGT).isoformat(timespec="seconds") if BB.get("loop_at") else None,
         "age_s": int(now - BB["loop_at"]) if BB.get("loop_at") else None, "refresh_s": BB["params"]["refresh_sec"], "db_ok": BB.get("db_ok")},
        {"id": "traffic", "label": "Traffic-aware engine", "status": "ok" if TR["last"] and now - TR["last"] < 3 * TR["params"]["refresh_s"] else ("idle" if not TR["last"] else "stale"),
         "last": datetime.fromtimestamp(TR["last"], SGT).isoformat(timespec="seconds") if TR["last"] else None,
         "age_s": int(now - TR["last"]) if TR["last"] else None, "refresh_s": TR["params"]["refresh_s"], "feeds": TR["feeds"]},
    ]
    config = {"httpx_ok": not HTTPX_STUB, "problem": HTTPX_PROBLEM, "lta_key": bool(KEY), "datagov_key": bool(DATAGOV_KEY), "onemap_routing": bool(routegeom.ONEMAP_EMAIL and routegeom.ONEMAP_PASSWORD),
              "osrm": OSRM, "carto_key": bool(CARTO_API_KEY), "waze_feed": bool(WAZE["url"]), "tomtom_key": bool(TT["key"])}
    newest = max([f["last"] for f in feeds + engines if f.get("last") and f["status"] in ("ok", "stale")] or [None], key=lambda x: x or "")
    return {"version": VERSION, "platform": "V16.0", "time": now_sgt().isoformat(timespec="seconds"), "feeds": feeds, "engines": engines,
            "config": config, "newest": newest, "auth": auth.config()["mode"]}


@app.get("/api/system/notifications")
async def api_system_notifications():
    """For the notification bell: alerts the collectors have ALREADY raised (open bunching / long-headway alerts, unacknowledged
    high / critical traffic alerts). Reads cached state only - never triggers a DataMall call."""
    out = []
    for a in bb_alerts():
        out.append({"id": f"bb:{a['id']}", "kind": "bunching" if a["kind"] == "bb" else "gap", "severity": "critical" if (a["kind"] == "gap" or (a.get("level") or 0) >= 3) else "warning",
                    "title": f"Svc {a['service']} Dir {a['direction']} \u00b7 {a['label']}", "detail": f"{a['stops']} stops" + (f" \u00b7 max headway {a['max_hw']:.0f} min" if a.get("max_hw") else "") + (f" \u00b7 min headway {a['min_hw']:.1f} min" if a.get("min_hw") and a["kind"] == "bb" else ""),
                    "acked": a["acked"], "ts": a.get("last") or a.get("start"), "href": f"/bunching?svc={a['service']}&dir={a['direction']}"})
    try:
        ov = traffic.overview(TR["book"], TR["params"], time.time(), None, 0, 0, None, ["unacknowledged"])
        for r in ov["rows"]:
            if r["level"] not in ("critical", "high"):
                continue
            out.append({"id": f"tr:{r['id']}", "kind": "traffic", "severity": "critical" if r["level"] == "critical" else "warning",
                        "title": f"Svc {r['svc']} Dir {r['dir']} \u00b7 {r['kind'].capitalize()}", "detail": (r.get("location") or "") + (f" \u00b7 +{r['delay_min']:.0f} min" if r.get("delay_min") else ""),
                        "acked": False, "ts": r.get("start"), "href": f"/?svc={r['svc']}&dir={r['dir']}"})
    except Exception:
        pass
    try:
        for x in dv_queue_items():                                                    # V16.14: Diversion Maps
            out.append({"id": x["id"], "kind": "diversion", "severity": "critical" if x["severity"] == "critical" else "warning", "title": x["title"],
                        "detail": x["detail"], "acked": x["status"] == "acknowledged", "ts": x["last"], "href": x["href"]})
    except Exception:
        pass
    try:                                                                              # V17: Forecast Centre alerts (verified, high / critical)
        if FC.get("book") is not None:
            for a in FC["book"].view(time.time()):
                if a["level"] in ("critical", "high"):
                    out.append({"id": "fc:" + a["id"], "kind": "forecast", "severity": "critical" if a["level"] == "critical" else "warning",
                                "title": a["title"], "detail": (a.get("observed") or "")[:120], "acked": bool(a.get("acked")), "ts": a.get("last"), "href": "/command"})
    except Exception:
        pass
    out.sort(key=lambda x: (x["acked"], 0 if x["severity"] == "critical" else 1, -(x["ts"] or 0)))
    return {"items": out[:40], "unacknowledged": sum(1 for x in out if not x["acked"]), "time": now_sgt().isoformat(timespec="seconds")}


# =========================================================================== V16.14 Diversion Maps - OCC Diversion Decision Engine
# BLOCK -> ANALYSE -> PLAN -> SIMULATE -> CONFIRM -> MONITOR -> RECOVER. The logic is diversion.py (pure, no I/O). Everything below only
# gathers data the platform already uses: LTA stops / routes / Bus Arrival / speed bands / incidents / road works, the real-road service
# lines of the traffic engine (TR_LINES), OSRM road routes (os_osrm), OpenStreetMap restriction tags (os_overpass + offservice.findings),
# the scheduled headway (bb_resolve_hw), OCC Notes & Actions tickets and the OCC Live alert queue. Decision support only: nothing is
# executed and nothing is sent outside the platform; the controller confirms every step.
import diversion  # noqa: E402

try:
    diversion.PARAMS["block_buffer_m"] = max(15.0, min(100.0, float(os.getenv("DIVERSION_BLOCK_BUFFER_M", "35"))))   # exclusion zone either side of a blockage
    diversion.PARAMS["block_end_margin_m"] = max(0.0, min(50.0, float(os.getenv("DIVERSION_BLOCK_END_MARGIN_M", "10"))))  # usable junction at each end
except ValueError:
    pass
try:
    diversion.PARAMS["wait_max_min"] = float(os.getenv("DIVERSION_WAIT_MAX_MIN", "0"))   # 0 = buses are never held; always divert
except ValueError:
    pass
DV_MAX_POLL = int(os.getenv("DIVERSION_MAX_POLL", "16"))            # service-directions whose live buses are polled per analysis (protects the LTA quota)
DV_AUTO = os.getenv("DIVERSION_AUTO_DETECT", "").strip().lower() in ("1", "true", "yes")   # future mode: LTA incidents raise "potential road blockage" alerts
DV_STATUSES = ("detected", "analysing", "planned", "active", "monitoring", "recovering", "ended")
DV = {"init": False, "bbox": {}, "bbox_at": None, "prep": {}}
DV_INCIDENT_TYPES = ("road block", "roadblock", "accident", "vehicle breakdown", "obstacle", "diversion", "heavy traffic", "road works", "roadworks", "unattended vehicle")


def dv_init():
    if DV["init"]:
        return
    bb_sql("""CREATE TABLE IF NOT EXISTS diversion_plan(id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts REAL, updated_ts REAL, status TEXT, road TEXT, road_norm TEXT,
              lat REAL, lon REAL, block TEXT, closure_min REAL, closure_label TEXT, services TEXT, created_by TEXT, created_occ TEXT, shared TEXT, revision INTEGER DEFAULT 1,
              ticket_id INTEGER, notice TEXT, started_ts REAL, ended_ts REAL, recovery TEXT, alert_acked_ts REAL, alert_acked_by TEXT, log TEXT, summary TEXT)""")
    bb_sql("CREATE TABLE IF NOT EXISTS diversion_ack(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, occ TEXT, who TEXT, ts REAL, revision INTEGER)")
    bb_sql("CREATE TABLE IF NOT EXISTS important_stop(code TEXT PRIMARY KEY, reason TEXT, by_name TEXT, ts REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS dv_line_fix(service TEXT, direction INTEGER, key TEXT, data TEXT, ts REAL, PRIMARY KEY(service, direction))")
    bb_sql("CREATE TABLE IF NOT EXISTS bus_type_seen(service TEXT, type TEXT, n INTEGER, last_ts REAL, PRIMARY KEY(service, type))")
    bb_sql("CREATE TABLE IF NOT EXISTS bus_type_override(service TEXT PRIMARY KEY, types TEXT, note TEXT, by_name TEXT, ts REAL)")
    DV["init"] = True


def _dv_body(raw):
    try:
        b = json.loads(raw.decode("utf-8") or "{}")
        return b if isinstance(b, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def dv_clean_line(v, max_pts=600):
    out = []
    for p in (v or [])[:max_pts]:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if in_sg(lat, lon):
            out.append((lat, lon))
    return out


def dv_flags(body):
    """page-wide choices sent with every request: TEST data (synthetic buses), include live traffic, operator / services filter"""
    svcs = body.get("services_filter") or []
    if isinstance(svcs, str):
        svcs = re.split(r"[\s,;]+", svcs)
    return {"test": bool(body.get("test")), "traffic": body.get("traffic") is not False,
            "operator": str(body.get("operator") or "").strip().upper()[:6],
            "services": {str(x).strip().upper() for x in svcs if str(x).strip()}}


def dv_test_buses(line, cum, prof, stop_s, lo_s, hi_s, A, H, bus_type="DD"):
    """TEST data: a synthetic fleet at the scheduled headway on the service's real route (same record shape as live buses).
    First bus 0.3 headway before the block, then every headway upstream; one bus just past the block. Never shown as live."""
    H = H or 10.0
    t_blk = diversion.t_at(prof, A)
    ts = prof["t"]
    out = []

    cm_ = prof["cum"]

    def s_at(t):          # position (m) reached at running time t (min): inverse of the time profile
        if not ts or t < ts[0] or t > ts[-1]:
            return None
        lo, hi = 0, len(ts) - 1
        while hi - lo > 1:
            m_ = (lo + hi) // 2
            if ts[m_] <= t:
                lo = m_
            else:
                hi = m_
        f = 0.0 if ts[hi] == ts[lo] else (t - ts[lo]) / (ts[hi] - ts[lo])
        return cm_[lo] + (cm_[hi] - cm_[lo]) * f
    k = 0
    while k < 12:
        t = t_blk - (0.3 + k) * H
        if t < 0:
            break
        s_ = s_at(t)
        if s_ is None or s_ < lo_s:
            break
        out.append(s_)
        k += 1
    t_after = t_blk + 0.6 * H
    if ts and t_after <= ts[-1]:
        s_ = s_at(t_after)
        if s_ is not None and s_ <= hi_s + 300:
            out.append(s_)
    res = []
    for s_ in out:
        p_ = diversion.point_at(line, cum, s_)
        res.append({"lat": round(p_[0], 6), "lon": round(p_[1], 6), "s": round(s_, 1), "off_m": 0.0, "on_diversion": False,
                    "load": "SEA", "type": bus_type, "wab": True, "monitored": True, "eta_anchor": None, "test": True})
    return res


def dv_speed_fn(idx):
    if not idx:
        return None

    def f(lat, lon, brg):
        s = idx.match(lat, lon, brg)
        return speed_of(s[4], s[6], s[7]) if s else None
    return f


def dv_hhmm(ts=None):
    return datetime.fromtimestamp(ts or time.time(), SGT).strftime("%H:%M")


def dv_svc_key(s):
    m = re.match(r"(\d+)(.*)", s or "")
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, s or "")


def dv_marked():
    dv_init()
    return {r["code"]: (r["reason"] or "listed") for r in bb_sql("SELECT code, reason FROM important_stop", fetch=True)}


DV_LINES = {}          # (svc, d) -> verified-line state (checked against LTA stop distances, bad segments re-routed)


async def dv_line(st, svc, d, focus=None, focus_km=3.0, max_fix=24):
    """the service's CORRECT road line -> (line, source label). The real-road line (busrouter.sg fitted to LTA stops) is
    checked against LTA BusRoutes: every stop must lie on it in order, and each stop-to-stop length must match LTA's
    official distance. Segments that fail are re-routed stop to stop on real roads (no U-turn), keeping the route whose
    length matches LTA best. With a focus point, segments near it are fixed first; fixes are cached and stored."""
    base, src = await dv_line_base(st, svc, d)
    if not base:
        return base, src
    stops = route_stops(st, svc, d)
    if len(stops) < 2:
        return base, src
    key = f"{len(base)}:{round(base[0][0], 5)}:{round(base[-1][1], 5)}:{len(stops)}"
    ent = DV_LINES.get((svc, d))
    if not ent or ent["key"] != key:
        ent = dv_line_load(svc, d, key)
        if ent is None:
            chk = await asyncio.to_thread(diversion.check_line, base, stops)
            ent = {"key": key, "chk": chk, "fixed": {}, "tried": set()}
        DV_LINES[(svc, d)] = ent
        if len(DV_LINES) > 600:
            DV_LINES.pop(next(iter(DV_LINES)))
    chk = ent["chk"]
    bad = [g for g in chk["segs"] if not g["ok"] and g["i"] not in ent["tried"]]
    if focus is not None and bad:
        fxs = focus if (focus and isinstance(focus[0], (list, tuple))) else [focus]      # one point or several (every blockage)

        def dist_focus(g):
            a_, b_ = stops[g["i"]], stops[g["i"] + 1]
            return min(diversion.dist_m(fx, (q["lat"], q["lon"])) for fx in fxs for q in (a_, b_))
        bad = sorted([g for g in bad if dist_focus(g) <= focus_km * 1000.0], key=dist_focus)
    bad = bad[:max_fix]
    if bad:
        res = await asyncio.gather(*[dv_fix_segment(base, chk, stops, g) for g in bad])
        for g, f in zip(bad, res):
            ent["tried"].add(g["i"])
            if f:
                ent["fixed"][g["i"]] = f
        dv_line_save(svc, d, ent)
    nbad = sum(1 for g in chk["segs"] if not g["ok"])
    if not nbad:
        ent["line"], ent["label"] = base, src + " \u00b7 checked against LTA stop distances"
        return base, ent["label"]
    if ent.get("built") != len(ent["fixed"]):
        ent["line"] = await asyncio.to_thread(diversion.assemble_line, base, chk, stops, ent["fixed"])
        ent["built"] = len(ent["fixed"])
    good_fix = sum(1 for f in ent["fixed"].values() if f.get("ok"))
    left = nbad - good_fix
    ent["label"] = (f"{src} \u00b7 {nbad} of {len(chk['segs'])} stop-to-stop segments did not match LTA; {good_fix} re-routed on real roads"
                    + (f", {left} still unverified" if left else ""))
    ent["unverified"] = left
    return ent["line"], ent["label"]


async def dv_fix_segment(base, chk, stops, g):
    """re-route one stop-to-stop segment on real roads, heading the way the bus travels, no U-turn;
    keep the road route whose length is closest to LTA's official distance"""
    i, P = g["i"], diversion.PARAMS
    tol = P["line_stop_tol_m"]
    ends = []
    for k in (i, i + 1):
        if chk["lat"][k] <= tol:
            ends.append(diversion.point_at(base, chk["cum"], chk["pos"][k]))
        else:
            ends.append((stops[k]["lat"], stops[k]["lon"]))
    a, b = ends
    hb = diversion.bearing(a, b)
    try:
        rr = await dv_osrm([a, b], 2, [hb if chk["lat"][i] > tol else diversion.nearest_on_line(a, base, chk["cum"])[3], None])
    except Exception:
        return None
    D = g["D"]
    best = None
    for r in rr.get("routes") or []:
        if diversion.uturns(r["line"], r.get("steps")):
            continue
        m = r["km"] * 1000.0
        err = abs(m - D) if D else m
        if best is None or err < best[0]:
            best = (err, r["line"], m)
    if not best:
        return None
    ok = D is None or best[0] <= max(P["line_len_abs_m"], P["line_len_rel"] * D)
    base_err = abs(g["L"] - D) if D else None
    if not ok and base_err is not None and base_err <= best[0] and "stop off the line" not in g["why"]:
        return None                 # the original is no worse: keep it (still marked unverified)
    return {"line": [(round(p_[0], 6), round(p_[1], 6)) for p_ in best[1]], "ok": ok, "m": round(best[2]), "D": D}


def dv_line_load(svc, d, key):
    try:
        dv_init()
        r = bb_sql("SELECT * FROM dv_line_fix WHERE service=? AND direction=? AND key=?", (svc, d, key), fetch=True)
        if not r:
            return None
        x = json.loads(r[0]["data"])
        return {"key": key, "chk": x["chk"], "fixed": {int(k): v for k, v in x["fixed"].items()}, "tried": set(x["tried"])}
    except Exception:
        return None


def dv_line_save(svc, d, ent):
    try:
        dv_init()
        data = json.dumps({"chk": ent["chk"], "fixed": ent["fixed"], "tried": sorted(ent["tried"])})
        bb_sql("INSERT OR REPLACE INTO dv_line_fix(service, direction, key, data, ts) VALUES(?,?,?,?,?)", (svc, d, ent["key"], data, time.time()))
    except Exception:
        pass


async def dv_line_base(st, svc, d):
    """the service's real-road line (the same line the traffic engine matches) -> (line, source label)"""
    fit = TR_LINES["lines"].get((svc, d))
    if fit and len(fit.get("line") or []) >= 2:
        return [tuple(p) for p in fit["line"]], "real road (busrouter.sg fitted to LTA stops)"
    stops = route_stops(st, svc, d)
    if len(stops) < 2:
        return None, "no route"
    g = await route_geometry(svc, d, stops)
    src = g.get("source") or "stops"
    return [tuple(p) for p in g["line"]], ("real road (" + src + ")" if src not in ("stops", "partial") else "approximate (straight lines between stops)")


def dv_prep(svc, d, line, stops):
    key = (svc, d, len(line), round(line[0][0], 5), round(line[-1][1], 5))
    hit = DV["prep"].get(key)
    if hit is None:
        cum = diversion.cum_m(line)
        hit = DV["prep"][key] = {"cum": cum, "stop_s": diversion.stop_positions(line, cum, stops)}
        if len(DV["prep"]) > 400:
            DV["prep"].pop(next(iter(DV["prep"])))
    return hit


def dv_bboxes():
    """bounding box of every real-road service line, rebuilt when the traffic engine rebuilds its lines"""
    if DV["bbox_at"] != TR_LINES["at"]:
        DV["bbox"] = {k: diversion.bbox(v["line"]) for k, v in TR_LINES["lines"].items() if len(v.get("line") or []) >= 2}
        DV["bbox_at"] = TR_LINES["at"]
    return DV["bbox"]


# ---- 1. snap a dropped block onto the road network (LTA speed-band road links; the bus network as a fallback)
def dv_corridor_lta(idx, lat, lon, reach_m=1500.0):
    if not idx:
        return None
    p = (lat, lon)
    cx, cy = int(lat / GRID), int(lon / GRID)
    best, bd = None, 1e9
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for i in idx.grid.get((cx + dx, cy + dy), ()):
                s = idx.segs[i]
                d = offservice.seg_dist_m(p, (s[0], s[1]), (s[2], s[3]))
                if d < bd:
                    best, bd = i, d
    if best is None or bd > 40.0:
        return None
    s0 = idx.segs[best]
    road = s0[5] or ""
    nr = offservice.norm_road(road)
    pool = set()
    for dx in range(-5, 6):
        for dy in range(-5, 6):
            for i in idx.grid.get((cx + dx, cy + dy), ()):
                if offservice.norm_road(idx.segs[i][5] or "") == nr:
                    pool.add(i)
    used = {best}
    chain = [(s0[0], s0[1]), (s0[2], s0[3])]

    def brg_of(s):
        return diversion.bearing((s[0], s[1]), (s[2], s[3]))

    def extend(forward):
        cur = best
        tot = 0.0
        while tot < reach_m:
            c = idx.segs[cur]
            end = (c[2], c[3]) if forward else (c[0], c[1])
            cb = brg_of(c)
            cand, cd = None, 1e9
            for j in pool:
                if j in used:
                    continue
                s = idx.segs[j]
                start = (s[0], s[1]) if forward else (s[2], s[3])
                d = diversion.dist_m(end, start)
                if d <= 30.0 and diversion.angdiff(cb, brg_of(s)) <= 50.0 and d < cd:
                    cand, cd = j, d
            if cand is None:
                break
            used.add(cand)
            s = idx.segs[cand]
            nxt = (s[2], s[3]) if forward else (s[0], s[1])
            tot += diversion.dist_m(end, nxt)
            if forward:
                chain.append(nxt)
            else:
                chain.insert(0, nxt)
            cur = cand
    extend(True)
    extend(False)
    return {"line": chain, "road": road, "source": "LTA road links (Traffic Speed Bands network)", "snap_m": round(bd, 1)}


def dv_corridor_bus(lat, lon, reach_m=1500.0):
    p, best = (lat, lon), None
    for k, bb in dv_bboxes().items():
        if not (bb[0] - 0.001 <= lat <= bb[2] + 0.001 and bb[1] - 0.001 <= lon <= bb[3] + 0.001):
            continue
        line = TR_LINES["lines"][k]["line"]
        d, s, _, _ = diversion.nearest_on_line(p, line)
        if d <= 35.0 and (best is None or d < best[0]):
            best = (d, s, line, k)
    if not best:
        return None
    d, s, line, k = best
    cum = diversion.cum_m(line)
    return {"line": diversion.cut(line, cum, max(0.0, s - reach_m), min(cum[-1], s + reach_m)), "road": "", "source": f"bus route line (service {k[0]})", "snap_m": round(d, 1)}


async def dv_road_name(lat, lon):
    async def factory():
        try:
            r = await client().get(f"{OSRM}/nearest/v1/driving/{lon:.6f},{lat:.6f}", params={"number": 1}, timeout=10)
            r.raise_for_status()
            w = (r.json().get("waypoints") or [{}])[0]
            return {"name": w.get("name") or ""}, 3600, True
        except Exception:
            return {"name": ""}, 60, False
    return (await cached(f"dvname:{lat:.4f},{lon:.4f}", factory)).get("name") or ""


@app.get("/api/diversion/snap")
async def api_dv_snap(lat: float = 0.0, lon: float = 0.0):
    if not in_sg(lat, lon):
        return {"ok": False, "error": "Drop the road block on a road in Singapore."}
    bands = await bands_state()
    cor = await asyncio.to_thread(dv_corridor_lta, bands.get("idx"), lat, lon)
    if not cor:
        cor = dv_corridor_bus(lat, lon)
    if not cor:
        return {"ok": False, "error": "No road found within 40 m of that point. Drop the block directly on the road."}
    if not cor["road"]:
        cor["road"] = await dv_road_name(lat, lon)
    line = cor["line"]
    cum = diversion.cum_m(line)
    d, s, _, brg = diversion.nearest_on_line((lat, lon), line, cum)
    a, b = max(0.0, s - 150.0), min(cum[-1], s + 150.0)
    return {"ok": True, "road": cor["road"] or "Unnamed road", "source": cor["source"], "snap_m": cor["snap_m"],
            "corridor": [[round(p[0], 6), round(p[1], 6)] for p in line], "corridor_m": [round(x, 1) for x in cum],
            "at_m": round(s, 1), "start_m": round(a, 1), "end_m": round(b, 1), "bearing": round(brg), "length_m": round(cum[-1])}


# ---- 2. live buses on one service-direction near the block (LTA Bus Arrival, projected onto the real-road line)
async def dv_buses(st, svc, d, line, cum, stops, stop_s, lo_s, hi_s, anchor_s=None, div_lines=None):
    n = len(stops)
    win = [i for i in range(n) if lo_s - 400 <= stop_s[i] <= hi_s + 400]
    if not win:
        return [], 0, None
    anchor = None
    if anchor_s is not None:
        before = [i for i in win if stop_s[i] <= anchor_s + 5]
        anchor = before[-1] if before else None
    step = max(1, math.ceil(len(win) / 7))
    sample = sorted(set(win[::step] + [win[-1]] + ([anchor] if anchor is not None else [])))
    results = await asyncio.gather(*[arrivals_raw(stops[i]["code"], svc) for i in sample])
    now = now_sgt()
    last_code = stops[-1]["code"]
    is_loop = stops[0]["code"] == last_code
    other_dir = {r["code"] for dd in st["dirs"].get(svc, []) if dd != d for r in st["routes"].get((svc, dd), [])}
    cands = []
    err = None
    for i, r in zip(sample, results):
        if r.get("_error"):
            err = r["_error"]
        shared = stops[i]["code"] in other_dir
        for sv in parse_services(r, now, svc):
            for b in sv["buses"]:
                if b["lat"] is None:
                    continue
                if shared and not is_loop and b["dest"] and b["dest"] != last_code:
                    continue
                cands.append({**b, "src": i})
    cands.sort(key=lambda c: c["etaf"])
    clusters = []
    for c in cands:
        for cl in clusters:
            if c["src"] not in cl["etas"] and hav_km(c["lat"], c["lon"], cl["lat"], cl["lon"]) < 0.25:
                cl["etas"][c["src"]] = c["etaf"]
                break
        else:
            clusters.append({**c, "etas": {c["src"]: c["etaf"]}})
    out = []
    for cl in clusters:
        src_s = stop_s[cl["src"]]
        dist, pos = diversion.project_window((cl["lat"], cl["lon"]), line, cum, src_s - 15000.0, src_s + 60.0)
        on_div = False
        if div_lines:
            on_div = any(diversion.nearest_on_line((cl["lat"], cl["lon"]), dl)[0] <= 60.0 for dl in div_lines if len(dl) >= 2)
        if (pos is None or dist > 80.0) and not on_div:
            continue
        if not on_div and not (lo_s - 500 <= pos <= hi_s + 800):
            continue
        on_d = bool(on_div and (dist is None or dist > 40.0))
        out.append({"lat": round(cl["lat"], 6), "lon": round(cl["lon"], 6), "s": (round(pos, 1) if pos is not None else None) if not on_d else None, "off_m": round(dist, 1),
                    "on_diversion": on_d, "load": cl["load"], "type": cl["type"], "wab": cl["wab"],
                    "monitored": cl["monitored"], "eta_anchor": round(cl["etas"][anchor], 2) if anchor is not None and anchor in cl["etas"] else None})
    out.sort(key=lambda b: -(b["s"] if b["s"] is not None else 1e12))
    return out, len(sample), err


# ---- 3. impact: which services run along the blocked section, which stops become inaccessible, which buses are coming
def dv_blocks(body):
    """blocks from a request: {"blocks": [{line, road, directed}, ...]} (V16.15) or the single {"block": {...}} of V16.14"""
    raw = body.get("blocks")
    if not isinstance(raw, list) or not raw:
        raw = [body.get("block") or {}]
    out = []
    for b in raw[:8]:
        if not isinstance(b, dict):
            continue
        line = dv_clean_line(b.get("line"))
        if len(line) >= 2:
            out.append({"line": line, "road": str(b.get("road") or "Road")[:80], "directed": bool(b.get("directed"))})
    return out


def dv_roads_txt(blocks, idxs=None):
    idxs = range(len(blocks)) if idxs is None else idxs
    return " / ".join(dict.fromkeys(blocks[i]["road"] for i in idxs if i < len(blocks))) or "Road"


async def dv_sections(line, blocks):
    """blocked runs of every blockage along one service line, merged into sections one diversion can bypass"""
    runs = []
    cum = diversion.cum_m(line)
    for i, b in enumerate(blocks):
        bl = b["line"]
        for r in await asyncio.to_thread(diversion.overlap_runs, line, bl, b["directed"]):
            # overlap detection samples the line and rounds outwards (~30 m past each end); tighten the run to the exact
            # projection of the drawn block's ends, so the junctions at the ends of the blockage are not treated as closed
            ends = []
            for q in (bl[0], bl[-1]):
                d_, s_ = diversion.project_window(q, line, cum, r["a"] - 80.0, r["b"] + 80.0)
                if s_ is not None and d_ <= 40.0:
                    ends.append(s_)
            a_, b_ = r["a"], r["b"]
            if len(ends) == 2 and max(ends) - min(ends) > 5.0:
                a_, b_ = max(a_, min(ends)), min(b_, max(ends))
            runs.append({"a": a_, "b": b_, "len": b_ - a_, "block": i})
    return diversion.merge_runs(runs)


async def dv_uses_any(cand, blocks):
    for b in blocks:
        if await asyncio.to_thread(diversion.uses_block, cand, b["line"], b["directed"]):
            return True
    return False


async def dv_entries(st, blocks):
    """every service-direction that runs ALONG any blockage. Blocked runs close together on one line form one section."""
    marked = dv_marked()
    hits = []
    if TR_LINES["lines"]:
        bbs = [diversion.bbox(b["line"], 60) for b in blocks]
        keys = [k for k, bx in dv_bboxes().items() if any(diversion.bbox_hit(bx, bb) for bb in bbs)]
        basis = "real-road service lines (busrouter.sg fitted to LTA stops)"
    else:
        near = set()
        for code, s_ in st["stops"].items():
            if any(min(diversion.dist_m((s_["lat"], s_["lon"]), p) for p in b["line"]) <= 500 for b in blocks):
                near.add(code)
        keys = sorted({(svc, d) for code in near for svc in st["at_stop"].get(code, ()) for d in st["dirs"].get(svc, [])})[:40]
        basis = "road geometry per service (network lines not built yet)"
    focus = [b["line"][len(b["line"]) // 2] for b in blocks] if blocks else None
    for k in keys:
        svc, d = k
        line, src = await dv_line(st, svc, d, focus=focus)
        if not line:
            continue
        secs = await dv_sections(line, blocks)
        if not secs:
            continue
        stops = route_stops(st, svc, d)
        pr = dv_prep(svc, d, line, stops)
        for ri, r in enumerate(secs):
            aff, seen = [], set()
            for a_, b_, bi in r["parts"]:
                for i in diversion.stops_in(pr["stop_s"], a_, b_, 35.0):
                    s_ = stops[i]
                    if i in seen or diversion.nearest_on_line((s_["lat"], s_["lon"]), blocks[bi]["line"])[0] > 45.0:
                        continue
                    seen.add(i)
                    why = diversion.stop_importance(s_["name"], len(st["at_stop"].get(s_["code"], ())), marked.get(s_["code"]))
                    aff.append({"code": s_["code"], "name": s_["name"], "important": why})
            hits.append({"service": svc, "direction": d, "run": ri, "a": round(r["a"], 1), "b": round(r["b"], 1), "len_m": round(r["len"]),
                         "blocks": r["blocks"], "roads": [blocks[i]["road"] for i in r["blocks"]],
                         "first": stops[0]["name"], "last": stops[-1]["name"], "stops_inaccessible": aff, "line_source": src})
    hits.sort(key=lambda h: (dv_svc_key(h["service"]), h["direction"], h["run"]))
    return hits, basis


@app.post("/api/diversion/analyse")
async def api_dv_analyse(request: Request):
    dv_init()
    body = _dv_body(await request.body())
    blocks = dv_blocks(body)
    if not blocks:
        return JSONResponse({"error": "Place the road block on the map first."}, status_code=400)
    block, blk = blocks[0]["line"], blocks[0]
    st = await static()
    if not st["stops"] or not st["routes"]:
        return {"ok": False, "error": f"Bus routes could not be loaded from LTA ({st.get('error') or 'no data'})."}
    tr_lines_kick(st)
    fq = await freq_table()
    opmap = await occ_operator_map()
    fl = dv_flags(body)
    entries, basis = await dv_entries(st, blocks)
    all_n = len(entries)
    if fl["operator"]:
        entries = [e for e in entries if opmap.get(e["service"], "").upper() == fl["operator"]]
    if fl["services"]:
        entries = [e for e in entries if e["service"] in fl["services"]]
    now = now_sgt()
    polled = 0
    errs = []
    for e in entries:
        H, H_src = bb_resolve_hw(e["service"], e["direction"], now, fq)
        e["H"], e["H_src"], e["operator"] = H, H_src, opmap.get(e["service"], "")
        e["buses"], e["buses_polled"] = [], False
        if polled >= DV_MAX_POLL:
            continue
        line, _ = await dv_line(st, e["service"], e["direction"], focus=[b["line"][len(b["line"]) // 2] for b in blocks])
        stops = route_stops(st, e["service"], e["direction"])
        pr = dv_prep(e["service"], e["direction"], line, stops)
        bands = await bands_state()
        prof = diversion.profile(line, dv_speed_fn(bands.get("idx")) if fl["traffic"] else None, pr["stop_s"])
        if fl["test"]:
            buses, n, err = dv_test_buses(line, pr["cum"], prof, pr["stop_s"], e["a"] - diversion.PARAMS["approach_km"] * 1000, e["b"], e["a"], H), 0, None
        else:
            buses, n, err = await dv_buses(st, e["service"], e["direction"], line, pr["cum"], stops, pr["stop_s"],
                                           e["a"] - diversion.PARAMS["approach_km"] * 1000, e["b"], anchor_s=e["a"])
        polled += 1
        if err:
            errs.append(err)
        diversion.label_buses(buses, e["service"])
        for b in buses:
            if b["s"] is None:
                continue
            m = diversion.t_at(prof, e["a"]) - diversion.t_at(prof, b["s"]) if b["s"] < e["a"] else None
            if b.get("eta_anchor") is not None and m is not None:
                m = b["eta_anchor"]                     # LTA Bus Arrival ETA at the last stop before the block
                b["eta_basis"] = "LTA arrival time"
            else:
                b["eta_basis"] = "TEST data (synthetic bus)" if fl["test"] else ("estimated from speed bands" if fl["traffic"] else "estimated without traffic")
            b["min_to_block"] = round(m, 1) if m is not None else None
            b["status"], b["status_text"] = diversion.bus_status(m, b["s"], None, e["a"], e["b"])
        e["buses"] = [b for b in buses if b["s"] is None or b["s"] <= e["b"] + 50]
        e["buses_polled"] = True
    bbs = [diversion.bbox(b["line"], 400) for b in blocks]
    inb = lambda la, lo: any(bb_[0] <= la <= bb_[2] and bb_[1] <= lo <= bb_[3] for bb_ in bbs)
    try:
        inc = [x for x in (await api_incidents("", 1)).get("incidents", []) if inb(x["lat"], x["lon"])]
    except Exception:
        inc = []
    try:
        rw, _ = await tr_roadworks(time.time())
        rw = [{"road": x["road"], "lat": x["lat"], "lon": x["lon"], "other": x.get("other")} for x in rw if x.get("lat") is not None and inb(x["lat"], x["lon"])]
    except Exception:
        rw = []
    cum = diversion.cum_m(block)
    mid = diversion.point_at(block, cum, cum[-1] / 2)
    n_bus = sum(len([b for b in e["buses"] if b.get("status") not in ("passed_block",)]) for e in entries)
    return {"ok": True, "road": dv_roads_txt(blocks), "length_m": round(sum(diversion.line_m(b["line"]) for b in blocks)), "blocks_n": len(blocks), "entries": entries,
            "services": len({e["service"] for e in entries}), "service_dirs": len(entries), "buses": n_bus,
            "polled": polled, "not_polled": max(0, len(entries) - polled), "poll_cap": DV_MAX_POLL, "basis": basis,
            "lines": TR_LINES["info"], "arrival_error": errs[0] if errs and len(errs) == polled else None,
            "incidents": inc[:10], "roadworks": rw[:10], "playbook": dv_playbook_rows(mid[0], mid[1], blk["road"]),
            "updated": now.isoformat(timespec="seconds"),
            "filter": {"operator": fl["operator"], "services": sorted(fl["services"]), "shown": len(entries), "total": all_n},
            "far_apart": dv_far_apart(blocks),
            "test": fl["test"], "traffic": fl["traffic"],
            "source": "TEST DATA \u00b7 synthetic buses at the scheduled headway on the real route (not live)" if fl["test"] else "LTA DataMall Bus Arrival (live)"}


# ---- 4. diversion options for one service-direction
def dv_traffic_label(km):
    tot = sum(km.values()) or 0.0
    known = tot - km.get("none", 0.0)
    if tot <= 0 or known < 0.3 * tot:
        return "UNKNOWN", "no live speed data on most of this road"
    slow, mod = km.get("slow", 0.0) / known, km.get("moderate", 0.0) / known
    if slow >= 0.25:
        return "HEAVY", f"{round(slow * 100)}% slow (LTA speed bands)"
    if slow + mod >= 0.3:
        return "MODERATE", f"{round((slow + mod) * 100)}% moderate or slow (LTA speed bands)"
    return "NORMAL", "LTA speed bands"


def dv_used_before(svc, d, sig):
    rows = bb_sql("SELECT id, started_ts, services FROM diversion_plan WHERE started_ts IS NOT NULL ORDER BY started_ts DESC LIMIT 200", fetch=True)
    for r in rows:
        try:
            for s_ in json.loads(r["services"] or "[]"):
                if s_.get("service") == svc and int(s_.get("direction") or 0) == d and s_.get("signature") == sig:
                    return {"id": r["id"], "date": datetime.fromtimestamp(r["started_ts"], SGT).strftime("%d %b %Y")}
        except (ValueError, TypeError):
            continue
    return None


DV_OSRM_SEM = asyncio.Semaphore(4)


import contextvars
DV_BUS = contextvars.ContextVar("dv_bus", default="dd")      # bus type of the plan being computed (TomTom vehicle dimensions)
DV_ROUTER = os.getenv("DIVERSION_ROUTER", "tomtom").strip().lower()          # tomtom (when TOMTOM_API_KEY is set) | osrm
DV_TT_MODE = os.getenv("DIVERSION_TOMTOM_MODE", "bus").strip().lower()      # TomTom travelMode: bus | truck
DV_TT_STATS = {"ok": 0, "fail": 0, "capped": 0, "error": None}
# vehicle dimensions sent to TomTom (m, kg): double-deck 4.4 m high; articulated 18 m long
DV_TT_DIMS = {"dd": (4.4, 2.55, 12.0, 18000), "sd": (3.2, 2.55, 12.0, 16000), "bd": (3.2, 2.55, 18.0, 25000)}
# V16.25: diagnosis only (which of a double-decker's dimensions TomTom refuses on a road): its height with a single-decker's
# weight, and its weight with a single-decker's height
DV_TT_DIMS.update({"dd_ht": (4.4, 2.55, 12.0, 16000), "dd_wt": (3.2, 2.55, 12.0, 18000)})
_TT_MAN = {"DEPART": ("depart", ""), "ARRIVE": ("arrive", ""), "ARRIVE_LEFT": ("arrive", ""), "ARRIVE_RIGHT": ("arrive", ""),
           "STRAIGHT": ("continue", "straight"), "KEEP_RIGHT": ("fork", "slight right"), "KEEP_LEFT": ("fork", "slight left"),
           "BEAR_RIGHT": ("turn", "slight right"), "BEAR_LEFT": ("turn", "slight left"), "TURN_RIGHT": ("turn", "right"),
           "TURN_LEFT": ("turn", "left"), "SHARP_RIGHT": ("turn", "sharp right"), "SHARP_LEFT": ("turn", "sharp left"),
           "MAKE_UTURN": ("turn", "uturn"), "ENTER_MOTORWAY": ("on ramp", ""), "ENTER_FREEWAY": ("on ramp", ""), "ENTER_HIGHWAY": ("on ramp", ""),
           "TAKE_EXIT": ("off ramp", ""), "MOTORWAY_EXIT_LEFT": ("off ramp", "slight left"), "MOTORWAY_EXIT_RIGHT": ("off ramp", "slight right"),
           "SWITCH_MOTORWAY_LEFT": ("fork", "slight left"), "SWITCH_MOTORWAY_RIGHT": ("fork", "slight right")}


def tt_parse_route(j):
    """TomTom Routing calculateRoute response -> the same route shape as offservice.parse_osrm
    {line, km, osrm_min, steps:[{road, ref, km, min, man, mod, line}], router}"""
    out = []
    for r in (j or {}).get("routes") or []:
        pts = [(p["latitude"], p["longitude"]) for leg in r.get("legs") or [] for p in leg.get("points") or []]
        if len(pts) < 2:
            continue
        sm = r.get("summary") or {}
        L, T = float(sm.get("lengthInMeters") or 0), float(sm.get("travelTimeInSeconds") or 0)
        ins = (r.get("guidance") or {}).get("instructions") or []
        steps = []
        for k, a in enumerate(ins):
            b = ins[k + 1] if k + 1 < len(ins) else None
            i0 = int(a.get("pointIndex") or 0)
            i1 = int(b.get("pointIndex")) if b and b.get("pointIndex") is not None else len(pts) - 1
            mn = a.get("maneuver") or ""
            man, mod = _TT_MAN.get(mn, ("roundabout", "") if mn.startswith("ROUNDABOUT") else ("turn", ""))
            road = (a.get("street") or "").strip() or ", ".join(a.get("roadNumbers") or [])
            o0 = float(a.get("routeOffsetInMeters") or 0)
            o1 = float(b.get("routeOffsetInMeters")) if b and b.get("routeOffsetInMeters") is not None else L
            t0 = float(a.get("travelTimeInSeconds") or 0)
            t1 = float(b.get("travelTimeInSeconds")) if b and b.get("travelTimeInSeconds") is not None else T
            steps.append({"road": road, "ref": ", ".join(a.get("roadNumbers") or []), "km": max(0.0, o1 - o0) / 1000.0,
                          "min": max(0.0, t1 - t0) / 60.0, "man": man, "mod": mod, "line": pts[i0:max(i0 + 1, i1) + 1]})
        if not steps:
            steps = [{"road": "", "ref": "", "km": L / 1000.0, "min": T / 60.0, "man": "depart", "mod": "", "line": pts}]
        out.append({"line": pts, "km": L / 1000.0 or offservice.line_km(pts), "osrm_min": T / 60.0, "steps": steps,
                    "router": "TomTom (" + DV_TT_MODE + ")", "traffic_s": float(sm.get("trafficDelayInSeconds") or 0)})
    return out


def dv_use_tomtom():
    return DV_ROUTER == "tomtom" and bool(TT["key"])


async def tt_route(coords, alternatives=0, bearings=None, bus="dd", avoid=None):
    """TomTom Routing API: bus / truck routing with vehicle dimensions (respects height, width and turn restrictions in
    TomTom's map) and live traffic. Cached 10 min; counts against TOMTOM_DAILY_CAP like the traffic calls."""
    locs = ":".join(f"{lat:.6f},{lon:.6f}" for lat, lon in coords)
    h, w, l_, kg = DV_TT_DIMS.get(bus, DV_TT_DIMS["dd"])
    prm = {"key": TT["key"], "travelMode": DV_TT_MODE, "routeType": "fastest", "traffic": "true", "instructionsType": "coded",
           "routeRepresentation": "polyline", "computeTravelTimeFor": "all", "vehicleHeight": h, "vehicleWidth": w, "vehicleLength": l_,
           "vehicleWeight": kg, "vehicleCommercial": "true"}
    if alternatives and len(coords) == 2:          # TomTom computes alternatives only without waypoints
        prm["maxAlternatives"] = min(5, int(alternatives))
    if bearings and bearings[0] is not None:
        prm["vehicleHeading"] = int(round(bearings[0])) % 360
    body = {"avoidAreas": {"rectangles": [{"southWestCorner": {"latitude": round(a[0], 6), "longitude": round(a[1], 6)},
                                          "northEastCorner": {"latitude": round(a[2], 6), "longitude": round(a[3], 6)}} for a in avoid[:10]]}} if avoid else None
    key = "tt_route:" + hashlib.sha1(json.dumps([locs, {k: v for k, v in prm.items() if k != "key"}, body]).encode()).hexdigest()[:24]

    async def factory():
        if not tt_route_spend():
            DV_TT_STATS["capped"] += 1
            return {"routes": [], "error": "TomTom daily cap reached"}, 60, False
        try:
            url = f"https://api.tomtom.com/routing/1/calculateRoute/{locs}/json"
            if body:      # the closed section is an area the route must avoid: TomTom returns the real detour itself
                r = await client().post(url, params=prm, json=body, headers=tt_headers(), timeout=25)
            else:
                r = await client().get(url, params=prm, headers=tt_headers(), timeout=25)
            r.raise_for_status()
            routes = tt_parse_route(r.json())
            DV_TT_STATS["ok"] += 1
            return {"routes": routes, "error": None}, 600, True
        except Exception as e:
            DV_TT_STATS["fail"] += 1
            DV_TT_STATS["error"] = tt_err(e)
            return {"routes": [], "error": tt_err(e)}, 60, False
    return await cached(key, factory)


async def dv_osrm(coords, alternatives, bearings=None, avoid=None):
    """Road routing for diversions, with a small concurrency cap (the network plan asks for many routes at once).
    TomTom Routing (bus mode, vehicle dimensions, live traffic) when TOMTOM_API_KEY is set; OSRM (OpenStreetMap) otherwise
    or when TomTom fails / its daily cap is reached. Buses may not U-turn: OSRM gets no turning back at via points and the
    start / end heading; any U-turn left in a result from either router is rejected by diversion.uturns()."""
    async with DV_OSRM_SEM:
        tt_err_ = None
        if dv_use_tomtom():
            res = await tt_route(coords, alternatives, bearings, DV_BUS.get(), avoid)
            if res.get("routes"):
                return res
            tt_err_ = res.get("error")                                  # None = TomTom answered "no route" (a real answer)
        res = await os_osrm(coords, alternatives=alternatives, bearings=bearings, continue_straight=True)
        if bearings and not res.get("routes"):
            res = await os_osrm(coords, alternatives=alternatives, continue_straight=True)
        for r in res.get("routes") or []:
            r.setdefault("router", "OSRM (OpenStreetMap)")
        return dict(res, tt_error=tt_err_) if tt_err_ else res


def lta_cat(v):
    """LTA RoadCategory: letters A-G (DataMall v3) or digits 1-8 (v4) -> letter"""
    v = str(v or "").strip().upper()
    return {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E", "6": "F", "8": "G"}.get(v, v[:1])


def dv_classes(seg, idx, osm_ways=None):
    """road class ("major" | "medium" | "small" | None) every class_step_m along a diversion"""
    P = diversion.PARAMS
    cum = diversion.cum_m(seg)
    L = cum[-1] if cum else 0.0
    step = P["class_step_m"]
    out = []
    for k in range(max(2, int(L / step) + 1)):
        x = min(L, k * step)
        p = diversion.point_at(seg, cum, x)
        q = diversion.point_at(seg, cum, min(L, x + 10.0)) if x + 10.0 <= L else diversion.point_at(seg, cum, max(0.0, x - 10.0))
        brg = diversion.bearing(p, q) if x + 10.0 <= L else diversion.bearing(q, p)
        c = None
        if idx:
            m = idx.match(p[0], p[1], brg)
            if m is not None and len(m) > 8:
                c = diversion.LTA_CLASS.get(lta_cat(m[8]))
        if c is None and osm_ways:
            best = 18.0
            for w in osm_ways:
                if len(w["geom"]) < 2:
                    continue
                dd = diversion.nearest_on_line(p, w["geom"])[0]
                if dd < best:
                    best, c = dd, diversion.OSM_CLASS.get(w["hw"])
        out.append(c)
    return out


def dv_major_junctions(line, cum, A, B, idx):
    """points on the service route where it meets a major road (LTA category A/B/C/D/F link crossing it): natural places to divert and rejoin"""
    P = diversion.PARAMS
    reach, gap = P["junction_search_km"] * 1000.0, P["junction_min_gap_m"]

    def scan(s0, s1, step):
        found, s_ = [], s0
        while (step > 0 and s_ <= s1) or (step < 0 and s_ >= s1):
            p = diversion.point_at(line, cum, s_)
            q = diversion.point_at(line, cum, min(cum[-1], s_ + 10.0))
            brg = diversion.bearing(p, q)
            own = idx.match(p[0], p[1], brg)
            own_road = offservice.norm_road(own[5]) if own is not None else ""
            for turn in (90.0, 45.0, 135.0):
                m = idx.match(p[0], p[1], (brg + turn) % 360)
                if m is not None and diversion.LTA_CLASS.get(lta_cat(m[8] if len(m) > 8 else "")) in ("major", "medium") \
                        and offservice.norm_road(m[5]) != own_road:
                    if not found or abs(found[-1] - s_) >= gap:
                        found.append(round(s_, 1))
                    break
            if len(found) >= 3:
                break
            s_ += step
        return found
    return scan(A - 60.0, max(0.0, A - reach), -20.0), scan(B + 60.0, min(cum[-1], B + reach), 20.0)


def dv_major_vias(points, idx):
    """move each via point onto the nearest major-road link (LTA category A/B/C/F) within reach; drop it if none"""
    reach = diversion.PARAMS["major_via_reach_m"]
    out = []
    for v in points:
        cx, cy = int(v[0] / GRID), int(v[1] / GRID)
        best, bd = None, reach
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                for i_ in idx.grid.get((cx + dx, cy + dy), ()):
                    sg = idx.segs[i_]
                    if len(sg) <= 8 or diversion.LTA_CLASS.get(lta_cat(sg[8])) != "major":
                        continue
                    mid = ((sg[0] + sg[2]) / 2, (sg[1] + sg[3]) / 2)
                    dd = diversion.dist_m(v, mid)
                    if dd < bd:
                        bd, best = dd, mid
        if best and all(diversion.dist_m(best, o) > 150 for o in out):
            out.append(best)
    return out[:6]


async def os_highways(line):
    """OpenStreetMap highway class of the ways along a route (Overpass). Cached 1 day. Used where LTA has no road link."""
    pts = offservice.simplify(line, 120)
    coords = ",".join(f"{p[0]:.5f},{p[1]:.5f}" for p in pts)
    key = "ovh2:" + hashlib.sha1(coords.encode()).hexdigest()[:20]
    q = f'[out:json][timeout:20];way(around:15,{coords})["highway"];out tags geom 600;'

    async def factory():
        try:
            r = await client().post(OVERPASS, data={"data": q}, timeout=25)
            r.raise_for_status()
            ways = [{"hw": (e.get("tags") or {}).get("highway"), "name": (e.get("tags") or {}).get("name", ""),
                     "acc": {k: v for k, v in (e.get("tags") or {}).items() if k in ("access", "motor_vehicle", "motorcar", "psv", "bus", "hgv", "service", "maxheight")},
                     "geom": [(g["lat"], g["lon"]) for g in (e.get("geometry") or [])]}
                    for e in r.json().get("elements", []) if e.get("type") == "way"]
            return {"ok": True, "ways": ways}, 86400, True
        except Exception as e:
            return {"ok": False, "ways": [], "error": type(e).__name__}, 120, False
    return await cached(key, factory)


def dv_opt_params(body):
    """shared request parsing for /options and /plan_all"""
    closure = body.get("closure_min")
    try:
        closure = float(closure) if closure not in (None, "", "open") else None
    except (TypeError, ValueError):
        closure = None
    bus = body.get("bus_type") if body.get("bus_type") in offservice.BUS_TYPES else "dd"
    dims = {}
    for k_ in ("height_m", "width_m", "weight_t"):
        try:
            dims[k_] = float((body.get("dims") or {}).get(k_)) if (body.get("dims") or {}).get(k_) not in (None, "") else None
        except (TypeError, ValueError):
            dims[k_] = None
    return closure, bus, dims, bool(body.get("allow_small"))


@app.post("/api/diversion/options")
async def api_dv_options(request: Request):
    dv_init()
    body = _dv_body(await request.body())
    blocks = dv_blocks(body)
    svc = str(body.get("service") or "").strip().upper()
    try:
        d = int(body.get("direction") or 1)
        run_i = int(body.get("run") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "direction must be a number."}, status_code=400)
    closure, bus, dims, allow_small = dv_opt_params(body)
    if not blocks or not svc:
        return JSONResponse({"error": "A placed block and a service are required."}, status_code=400)
    st = await static()
    fl = dv_flags(body)
    manual = dv_manual(body)
    if manual:      # the controller's drawn route becomes a corridor every affected service can adopt
        dv_register_manual(blocks, manual)
    return await dv_options_cached(st, blocks, svc, d, run_i, closure, bus, dims, body.get("others") or [], True, allow_small, fl["test"], fl["traffic"], manual)


def dv_ladder_info(stops, stop_s, leave_s, rejoin_s):
    """the last stop the bus serves before it leaves its route, and the first stop it serves after rejoining
    (the same rule as the skipped-stop list, so the two can never disagree)"""
    sk = set(diversion.stops_in(stop_s, leave_s + 1.0, rejoin_s - 1.0))
    li = max([i for i, x in enumerate(stop_s) if x <= rejoin_s and i not in sk and (not sk or i < min(sk))], default=None)
    ri = min([i for i, x in enumerate(stop_s) if x >= leave_s and i not in sk and (not sk or i > max(sk))], default=None)
    if li is None or ri is None:
        return None
    return {"leave_code": stops[li]["code"], "leave_name": stops[li]["name"], "rejoin_code": stops[ri]["code"], "rejoin_name": stops[ri]["name"],
            "stops_between": max(0, ri - li - 1)}


def dv_far_apart(blocks, km=2.0):
    """blockages more than `km` apart are probably separate incidents -> [(road a, road b, km)]"""
    out = []
    mids = [(b.get("road") or "road", b["line"][len(b["line"]) // 2]) for b in blocks]
    for i in range(len(mids)):
        for j in range(i + 1, len(mids)):
            dk = diversion.dist_m(mids[i][1], mids[j][1]) / 1000.0
            if dk > km:
                out.append({"a": mids[i][0], "b": mids[j][0], "km": round(dk, 1)})
    return out


def dv_avoid_rects(blocks, pad_m=12.0, max_n=10):
    """the closed sections as small rectangles (TomTom avoidAreas, max 10): ~80 m pieces along each block, padded pad_m.
    The section is trimmed at both ends by pad + end margin, because an axis-aligned box padded in every direction would
    otherwise also close the junctions at the ends of the blockage - which the bus may still use to divert or rejoin."""
    rects = []
    m_end = diversion.PARAMS.get("block_end_margin_m", 10.0)
    total = sum(diversion.line_m(b["line"]) for b in blocks) or 1.0
    piece = max(80.0, total / max_n)
    for b in blocks:
        ln = b["line"]
        cm = diversion.cum_m(ln)
        trim = min(pad_m + m_end, cm[-1] * 0.35)
        s_lo, s_hi = trim, cm[-1] - trim          # (not lo / hi: lo is reused below for longitudes)
        if s_hi <= s_lo:
            s_lo, s_hi = cm[-1] * 0.4, cm[-1] * 0.6
        n = max(1, int(math.ceil((s_hi - s_lo) / piece)))
        for k in range(n):
            part = diversion.cut(ln, cm, s_lo + (s_hi - s_lo) * k / n, s_lo + (s_hi - s_lo) * (k + 1) / n)
            la = [p[0] for p in part]
            lo = [p[1] for p in part]
            dla, dlo = pad_m / diversion.KY, pad_m / diversion.KX
            rects.append((min(la) - dla, min(lo) - dlo, max(la) + dla, max(lo) + dlo))
    return rects[:max_n]


def dv_busroad_vias(net, sec_line, n=8):
    """waypoints ON roads other bus services already use, around the block (rule 1 by construction), one per direction
    sector and distance band - used to push the router round the closure along real bus roads"""
    if not net or len(sec_line) < 2:
        return []
    cm = diversion.cum_m(sec_line)
    mid = diversion.point_at(sec_line, cm, cm[-1] / 2)
    mx, my = diversion.xy(mid)
    best = {}
    for entries in net.grid.values():
        for sd, b, x, y, tol in entries[::3]:
            dd = math.hypot(x - mx, y - my)
            if dd < 250 or dd > 1400:
                continue
            p = (y / diversion.KY, x / diversion.KX)
            if diversion.nearest_on_line(p, sec_line)[0] < 150:
                continue
            band = 0 if dd < 700 else 1
            sector = int(((math.degrees(math.atan2(x - mx, y - my)) + 360) % 360) // 45)
            target = 450 if band == 0 else 1000
            k = (sector, band)
            if k not in best or abs(dd - target) < best[k][0]:
                best[k] = (abs(dd - target), p)
    pts = [v[1] for k, v in sorted(best.items())]
    return pts[:n * 2]


DV_DONORS = {}      # blockage key -> {(svc, d): permitted diversion corridor found for that service}
DV_NEXT_CACHE = {}  # (svc, d, stop index) -> next-original-leg verification


def dv_block_key(blocks):
    return hashlib.sha1(json.dumps([[[round(p[0], 5), round(p[1], 5)] for p in b["line"]] for b in blocks]).encode()).hexdigest()[:16]


def dv_donors(blocks, exclude=None):
    """permitted diversion corridors other services already have for this blockage (30 min)"""
    now = time.time()
    reg = DV_DONORS.get(dv_block_key(blocks)) or {}
    return [v for k, v in sorted(reg.items()) if k != exclude and now - v["ts"] < 1800]


def dv_manual(body):
    """controller-drawn waypoints [[lat, lon], ...] (max 20, inside Singapore)"""
    out = []
    for v in (body.get("manual_vias") or [])[:20]:
        try:
            la, lo = float(v[0]), float(v[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 1.1 <= la <= 1.5 and 103.5 <= lo <= 104.1:
            out.append([round(la, 6), round(lo, 6)])
    return out if len(out) >= 1 else None


def dv_register_manual(blocks, manual):
    reg = DV_DONORS.setdefault(dv_block_key(blocks), {})
    vv = [tuple(v) for v in manual]
    reg[("OCC", 0)] = {"service": "OCC", "direction": 0, "vias": vv if len(vv) >= 2 else vv * 2, "seg": vv, "roads": [], "signature": "",
                       "ts": time.time(), "label": "the controller's route"}


def dv_register_donors(blocks, svc, d, opts):
    key = dv_block_key(blocks)
    reg = DV_DONORS.setdefault(key, {})
    best = [o for o in opts if o.get("permitted") and o.get("_seg") and o.get("confidence", "HIGH") in ("HIGH", "MEDIUM")]
    if not best:
        return
    o = best[0]
    seg = o["_seg"]
    cm = diversion.cum_m(seg)
    vias = [diversion.point_at(seg, cm, cm[-1] * f) for f in (0.2, 0.4, 0.6, 0.8)] if cm[-1] > 0 else []
    reg[(svc, d)] = {"service": svc, "direction": d, "vias": vias, "seg": diversion.simplify(seg, 120), "roads": list(o["roads"]),
                     "signature": o["signature"], "ts": time.time(), "label": f"{svc} D{d}"}
    if len(DV_DONORS) > 50:
        DV_DONORS.pop(next(iter(DV_DONORS)))


async def dv_options_cached(st, blocks, svc, d, run_i, closure, bus, dims, others, poll, allow_small, test=False, traffic=True, manual=None):
    """options are reused for 90 s so the network plan and the per-service view never compute the same thing twice"""
    key = "dvopt:" + hashlib.sha1(json.dumps([[b["line"], b["directed"]] for b in blocks] + [svc, d, run_i, closure, bus, dims, poll, allow_small, test, traffic,
                                              sorted(x["label"] for x in dv_donors(blocks, (svc, d))), manual],
                                             default=str).encode()).hexdigest()[:24]

    async def factory():
        r = await dv_options_core(st, blocks, svc, d, run_i, closure, bus, dims, others, poll, allow_small, test, traffic, manual)
        return r, 90, bool(r.get("ok"))
    r = await cached(key, factory)
    return copy.deepcopy(r)


async def dv_options_core(st, blocks, svc, d, run_i, closure, bus, dims, others, poll=True, allow_small=False, test=False, traffic=True, manual=None):
    P = diversion.PARAMS
    tr_lines_kick(st)          # the bus-road check (LTA rule 1) needs every service's route line
    DV_BUS.set(bus)
    line, src = await dv_line(st, svc, d, focus=[b["line"][len(b["line"]) // 2] for b in blocks] if blocks else None)
    if not line:
        return {"ok": False, "error": f"No route for service {svc} direction {d}."}
    stops = route_stops(st, svc, d)
    pr = dv_prep(svc, d, line, stops)
    cum, stop_s = pr["cum"], pr["stop_s"]
    runs = await dv_sections(line, blocks)
    if not runs:
        return {"ok": False, "error": f"Service {svc} direction {d} does not run along the blocked section."}
    run = runs[min(run_i, len(runs) - 1)]
    A, B = run["a"], run["b"]
    road_txt = dv_roads_txt(blocks, run["blocks"])
    bands = await bands_state()
    idx = bands.get("idx")
    sfn = dv_speed_fn(idx) if traffic else None       # traffic excluded: road-routing times only
    prof = diversion.profile(line, sfn, stop_s)
    fq = await freq_table()
    now = now_sgt()
    H, H_src = bb_resolve_hw(svc, d, now, fq)
    marked = dv_marked()

    # V16.15: deterministic stop-to-stop engine (dv_stop_search). The routing engine makes every geometry;
    # validation first (hard rejects), scoring second; fewest stops skipped by search order.
    long_closure = not diversion.wait_allowed(closure, P)
    sec_line = diversion.cut(line, cum, A, B)
    jx, jr = [], []

    def heading(p, s=None):
        # V16.26g: with s (m along the route) the route's heading at THAT passage - see dv_heading_near
        return dv_heading_near(p, line, cum, s) if s is not None else diversion.nearest_on_line(p, line, cum)[3]

    def at(s_):
        return diversion.point_at(line, cum, max(0.0, min(cum[-1], s_)))
    donors = dv_donors(blocks, (svc, d))
    avoid = dv_avoid_rects(blocks, pad_m=min(P["block_buffer_m"], 20.0)) if dv_use_tomtom() else None
    bvias = []
    if not avoid and len(sec_line) >= 2:
        try:
            bvias = await asyncio.to_thread(dv_busroad_vias, await asyncio.to_thread(dv_busnet, st, diversion.bbox(sec_line, 1500), 0.2), sec_line)
        except Exception:
            bvias = []
    ctx = {"P": P, "st": st, "svc": svc, "d": d, "line": line, "cum": cum, "stops": stops, "stop_s": stop_s, "A": A, "B": B, "bus": bus,
           "idx": idx, "at": at, "heading": heading, "avoid": avoid, "bvias": bvias, "manual": manual, "donors": donors,
           "block_lines": [b_["line"] for b_ in blocks], "next_cache": DV_NEXT_CACHE, "bkey": dv_block_key(blocks)}
    sr = await dv_stop_search(ctx)
    cands, rejects, rejected = sr["cands"], sr["rejects"], sr["rejected"]
    raw = [None] * sr["routes"]
    routing_err = sr["error"]
    ia_ = sr["ia"]
    search = {"mode": "stops", "attempts": sr["attempts"], "summary": sr["summary"], "routes_tested": sr["routes"],
              "last_reachable": {"code": stops[ia_]["code"], "name": stops[ia_]["name"]} if ia_ is not None else None,
              "first_after": {"code": stops[sr["downstream"][0]]["code"], "name": stops[sr["downstream"][0]]["name"]} if sr["downstream"] else None,
              "buffer_m": P["block_buffer_m"], "stage": 1, "stages": 1, "max_km": 0}
    unavoidable = sum(1 for x in stop_s if A - P["block_end_margin_m"] < x < B + P["block_end_margin_m"])
    small_hidden = []          # every candidate here passed hard validation; rejected routes are in `rejects`
    cands = cands[:8]
    try:
        rw, _ = await tr_roadworks(now.timestamp())
    except Exception:
        rw = []
    try:
        inc = (await api_incidents("", 1)).get("incidents", [])
    except Exception:
        inc = []
    osms = await asyncio.gather(*[os_overpass(c["seg"]) for c in cands]) if cands else []
    opts = []
    for c, osm in zip(cands, osms):
        dep, seg, r = c["dep"], c["seg"], c["r"]
        leave_s, rejoin_s = dep["leave_s"], dep["rejoin_s"]
        fb = (r["km"] / (r["osrm_min"] * offservice.PARAMS["bus_time_factor"] / 60.0)) if (traffic and r.get("osrm_min")) else None   # traffic off: same standard speed as the normal route
        dprof = diversion.profile(seg, sfn, None, fallback_kmh=fb)
        div_min = dprof["t"][-1]
        normal_min = diversion.t_at(prof, rejoin_s) - diversion.t_at(prof, leave_s)
        runs_, tinfo = color_route(seg, idx) if idx else ([], {"km": {"none": c["div_m"] / 1000.0}})
        tl, tl_basis = dv_traffic_label(tinfo["km"]) if traffic else ("NOT INCLUDED", "live traffic switched off on the map")
        skipped = []
        for i in diversion.stops_in(stop_s, leave_s + 1.0, rejoin_s - 1.0):
            s_ = stops[i]
            why = diversion.stop_importance(s_["name"], len(st["at_stop"].get(s_["code"], ())), marked.get(s_["code"]))
            skipped.append({"code": s_["code"], "name": s_["name"], "important": why, "lat": s_["lat"], "lon": s_["lon"], "in_block": A - 35 <= stop_s[i] <= B + 35})
        route_like = {"groups": c["groups"], "steps": r["steps"], "line": seg}
        fnd = offservice.findings(route_like, osm, bus, dims, rw, inc, offservice.PARAMS)
        status, status_text = offservice.suitability(fnd, bus, None)
        added_m = c["div_m"] - (rejoin_s - leave_s)
        excessive = added_m > P["excess_km"] * 1000 or (rejoin_s - leave_s > 0 and c["div_m"] / (rejoin_s - leave_s) > P["excess_ratio"] and added_m > 1000)
        n_sharp = sum(g["sharp"] for g in c["groups"])
        used = dv_used_before(svc, d, c["signature"])
        reviews = [f for f in fnd if f["sev"] == "review"]
        if status == "unsuitable":          # vehicle restriction on these roads: hard reject
            rejects.append({"why": "unsuitable for this bus: " + status_text, "detail": "", "roads": [g["road"] for g in c["groups"]][:8],
                            "km": round(c["div_m"] / 1000.0, 2), "src": c.get("src", "router"), "router": r.get("router", ""),
                            "line": [[round(p_[0], 6), round(p_[1], 6)] for p_ in diversion.simplify(seg, 60)]})
            continue
        if status == "unsuitable":
            feas, feas_text = "NOT SUITABLE", status_text
        elif excessive:
            feas, feas_text = "LOW", f"Excessive detour (+{added_m / 1000:.1f} km). " + status_text
        elif used and not reviews:
            feas, feas_text = "HIGH", f"Same roads used in a confirmed diversion on {used['date']}; no restriction or works found on them now. Still confirm on the ground."
        else:
            feas, feas_text = "VERIFICATION REQUIRED", "Operational verification required. " + status_text
        opts.append({"signature": c["signature"], "roads": [g["road"] for g in c["groups"]], "road_class": c["road_class"], "bus_road": c.get("bus_road"),
                     "router": r.get("router", "OSRM (OpenStreetMap)"), "controller_route": c.get("src") == "manual", "found_by": c.get("src", "normal"),
                     "ladder": dv_ladder_info(stops, stop_s, leave_s, rejoin_s),
                     "confidence": c.get("confidence", "LOW"), "evidence": c.get("evidence", {}), "suitability": c.get("suit", {}),
                     "next_leg": c.get("next_leg"), "earlier": bool(c.get("earlier")),
                     "last_reachable": {"code": stops[c["pair"][0]]["code"], "name": stops[c["pair"][0]]["name"]} if c.get("pair") else None,
                     "rejoin_target": {"code": stops[c["pair"][1]]["code"], "name": stops[c["pair"][1]]["name"]} if c.get("pair") else None,
                     "groups": [{"road": g["road"], "km": round(g["km"], 2), "turns": g["turns"], "sharp": g["sharp"]} for g in c["groups"]],
                     "leave_s": round(leave_s, 1), "rejoin_s": round(rejoin_s, 1), "leave_pt": [round(dep["leave_pt"][0], 6), round(dep["leave_pt"][1], 6)],
                     "rejoin_pt": [round(dep["rejoin_pt"][0], 6), round(dep["rejoin_pt"][1], 6)],
                     "leave_before_block_m": round(A - leave_s), "rejoin_after_block_m": round(rejoin_s - B),
                     "div_km": round(c["div_m"] / 1000, 2), "normal_km": round((rejoin_s - leave_s) / 1000, 2), "added_km": round(added_m / 1000, 2),
                     "div_min": round(div_min, 1), "normal_min": round(normal_min, 1), "added_min": round(div_min - normal_min, 1),
                     "time_src": ("LTA speed bands" if dprof["known_share"] >= 0.5 else f"routing time x {offservice.PARAMS['bus_time_factor']:g} (little live speed data)") if traffic
                                 else f"standard bus speed {P['fallback_kmh']:g} km/h on both routes (live traffic excluded)",
                     "skipped": skipped, "skipped_n": len(skipped), "important_n": sum(1 for x in skipped if x["important"]),
                     "traffic": tl, "traffic_basis": tl_basis, "traffic_runs": [{"b": x["b"], "pts": x["pts"]} for x in runs_][:80],
                     "status": status, "status_text": status_text, "feasibility": feas, "feasibility_text": feas_text, "excessive": excessive,
                     "n_turns": sum(g["turns"] for g in c["groups"]), "n_sharp": n_sharp, "used_before": used,
                     "findings": [{"sev": f["sev"], "text": f["text"], "lat": f.get("lat"), "lon": f.get("lon")} for f in fnd][:12],
                     "osm_ok": bool(osm and osm.get("ok")), "line": diversion.simplify(seg, 300),
                     "prof_t": None, "_prof": dprof, "_seg": seg})
    for o in opts:          # BUS DIVERSION SCORE (validation already passed)
        o["score"], o["score_parts"] = diversion.score_diversion_candidate(o["evidence"] or {"score": 0.25}, o["suitability"] or {"level": 0.5},
                                                                          o["added_min"], o["added_km"], max(0, o["skipped_n"] - unavoidable),
                                                                          o["n_turns"], o["n_sharp"], P)
        o["checks"] = [["Road network connected (routing engine geometry)", True], ["No blocked road (" + f"{P['block_buffer_m']:.0f}" + " m exclusion zone)", True],
                       ["No U-turn, no backtracking", True], ["Bus-suitable roads (no private / service / restricted road)", True],
                       ["Existing bus-service / bus-stop road evidence", (o["evidence"] or {}).get("level", 3) <= 2],
                       ["Correct-direction rejoin", True], ["Next original stop reachable", bool((o.get("next_leg") or {}).get("ok"))]]
        o["permitted"] = True
        o["rules"] = {"main_roads": (o.get("road_class") or {}).get("label") == "MAIN ROADS", "no_uturn": True}
    opts.sort(key=lambda o: (diversion.CONF_RANK.get(o["confidence"], 3), -o["score"]))
    opts = opts[:P["max_options"]]
    for i, o in enumerate(opts, 1):
        o["n"] = i
        o["name"] = f"OPTION {i}"

    # last safe diversion point = the latest usable exit before the block
    usable = [o for o in opts if o["permitted"]]          # never anchored on a small-road route (LTA rule 1)
    last = max(usable, key=lambda o: o["leave_s"]) if usable else None

    # live buses (LTA Bus Arrival) on the approach, with their minutes to the last diversion point
    anchor_s = last["leave_s"] if last else A
    if test:
        buses, n_polled, arr_err = dv_test_buses(line, cum, prof, stop_s, A - P["approach_km"] * 1000, max([o["rejoin_s"] for o in opts] + [B + 300]), A, H,
                                                 "DD" if bus == "dd" else "SD"), 0, None
    elif poll:
        buses, n_polled, arr_err = await dv_buses(st, svc, d, line, cum, stops, stop_s, A - P["approach_km"] * 1000, max([o["rejoin_s"] for o in opts] + [B + 300]),
                                                  anchor_s=anchor_s, div_lines=[o["_seg"] for o in opts])
    else:
        buses, n_polled, arr_err = [], 0, None
    diversion.label_buses(buses, svc)
    on_div = [dict(b, status="on_diversion", status_text="On a diversion route now") for b in buses if b["s"] is None]
    buses = [b for b in buses if b["s"] is not None]
    for b in buses:
        mt = diversion.t_at(prof, anchor_s) - diversion.t_at(prof, b["s"]) if b["s"] <= anchor_s else None
        if mt is not None and b.get("eta_anchor") is not None:
            anchor_i = max([i for i, x in enumerate(stop_s) if x <= anchor_s + 5] or [0])
            mt = b["eta_anchor"] + (diversion.t_at(prof, anchor_s) - diversion.t_at(prof, stop_s[anchor_i]))
            b["eta_basis"] = "LTA arrival time"
        else:
            b["eta_basis"] = "estimated from speed bands"
        b["min_to_exit"] = round(mt, 1) if mt is not None else None
        b["m_to_exit"] = round(anchor_s - b["s"]) if b["s"] <= anchor_s else None
        b["status"], b["status_text"] = diversion.bus_status(mt, b["s"], anchor_s, A, B)
    ordered = sorted(buses, key=lambda b: -b["s"])
    for k, b in enumerate(ordered):
        if k > 0:
            b["gap_ahead_min"] = round(diversion.t_at(prof, ordered[k - 1]["s"]) - diversion.t_at(prof, b["s"]), 1)
    sim_buses = []
    for b in buses:
        x = {"label": b["label"], "s": b["s"]}
        sim_buses.append(x)

    def sim_for(o):
        sb = []
        for b in buses:
            x = {"label": b["label"], "s": b["s"]}
            if o is not None and b.get("min_to_exit") is not None:
                x["eta_exit"] = b["min_to_exit"] + (diversion.t_at(prof, o["leave_s"]) - diversion.t_at(prof, anchor_s))
            sb.append(x)
        return sb

    ref_none = min([o["rejoin_s"] for o in opts] + [min(cum[-1], B + 200.0)])
    closure_real = closure
    if closure is not None and closure > P["sim_closure_max_min"]:
        closure = None          # whole day / many hours: a bus that reaches the block is unable to move (simulated as stuck)
    s_none = diversion.simulate(sim_buses, prof, A, B, ref_none, closure, H)
    hw_none = diversion.headways(s_none, H)
    reg_none = diversion.regulation(s_none, H)
    rec_none = diversion.recovery_minutes(s_none, reg_none, H) if closure is not None else None
    cols = [{"key": "none", "name": "NO ACTION", "skipped_n": 0, "important_n": 0, "added_km": 0.0,
             "added_min": None, "affected": sum(1 for r in s_none if r["mode"] in ("queued", "stuck")),
             "max_wait": max([r["wait"] for r in s_none if r["mode"] == "queued"] or [0.0]) if closure is not None else None,
             "sim": s_none, "hw": hw_none, "reg": reg_none, "recovery_min": rec_none, "bus_min": round(diversion.bus_minutes(s_none), 1) if closure is not None else None,
             "timeline": diversion.timeline(s_none, hw_none, reg_none, rec_none, closure, road_txt)}]
    for o in opts:
        opt_ = {"leave_s": o["leave_s"], "rejoin_s": o["rejoin_s"], "div_min": o["div_min"]}
        s_ = diversion.simulate(sim_for(o), prof, A, B, o["rejoin_s"], closure, H, opt_)
        hw_ = diversion.headways(s_, H)
        reg_ = diversion.regulation(s_, H)
        rec_ = diversion.recovery_minutes(s_, reg_, H) if (closure is not None or not any(r["mode"] == "stuck" for r in s_)) else None
        o["affected_buses"] = sum(1 for r in s_ if r["mode"] == "diverted")
        cols.append({"key": f"o{o['n']}", "name": o["name"], "skipped_n": o["skipped_n"], "important_n": o["important_n"], "added_km": o["added_km"],
                     "added_min": o["added_min"], "affected": o["affected_buses"], "max_wait": max([r["wait"] or 0 for r in s_ if r["mode"] == "queued"] or [0.0]),
                     "sim": s_, "hw": hw_, "reg": reg_, "recovery_min": rec_, "bus_min": round(diversion.bus_minutes(s_), 1),
                     "timeline": diversion.timeline(s_, hw_, reg_, rec_, closure, road_txt, o["name"])})
    closure = closure_real
    wod = diversion.wait_or_divert(s_none, opts, closure)
    rec = diversion.select_best_diversion(opts, search)
    cols[0]["viable"] = diversion.wait_allowed(closure, P)
    # buses already past the last diversion point: with no waiting possible, find each a way out from where it is now
    # (road route forward from its position, avoiding every blockage, no U-turn), or flag it as unable to move
    trapped = []
    if long_closure:
        best = next((o for o in opts if o["n"] == rec.get("option")), opts[0] if opts else None)
        tgt_s = best["rejoin_s"] if best else min(cum[-1], B + 400.0)
        tgt = diversion.point_at(line, cum, tgt_s)
        for b in [b for b in buses if b.get("status") in ("passed_exit", "inside")][:4]:
            t = {"label": b["label"], "lat": b["lat"], "lon": b["lon"], "status": b["status"], "escape": None}
            if b["status"] == "inside":
                t["note"] = "Inside the closed section: on-site instruction needed (no routing through a closure)."
            else:
                start = (b["lat"], b["lon"])
                hdg = [diversion.nearest_on_line(start, line, cum)[3], diversion.nearest_on_line(tgt, line, cum)[3]]
                rr = await dv_osrm([start, tgt], 2, hdg)
                for r in rr.get("routes") or []:
                    if await dv_uses_any(r["line"], blocks) or await asyncio.to_thread(diversion.uturns, r["line"], r.get("steps")):
                        continue
                    roads = [g["road"] for g in offservice.group_roads(r["steps"]) if g.get("road")]
                    t["escape"] = {"roads": roads[:8], "km": round(r["km"], 2), "min": round(r["osrm_min"], 1),
                                   "line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(r["line"], 200)],
                                   "time_src": "OSRM road time (estimate)"}
                    break
                if not t["escape"]:
                    t["note"] = "No road route forward without a U-turn and avoiding the block: unable to move until reopened. Escalate (LTA / Traffic Police assistance)."
            trapped.append(t)
    # other affected services that could use the same diversion (their line passes the same exit and rejoin points, in that order)
    also = []
    for x in (others or [])[:20]:
        try:
            s2, d2 = str(x.get("service")).upper(), int(x.get("direction"))
        except (TypeError, ValueError, AttributeError):
            continue
        if (s2, d2) == (svc, d):
            continue
        l2, _ = await dv_line(st, s2, d2)
        if not l2:
            continue
        c2 = diversion.cum_m(l2)
        for o in opts:
            de, pe, _, _ = diversion.nearest_on_line(tuple(o["leave_pt"]), l2, c2)
            dr, prj, _, _ = diversion.nearest_on_line(tuple(o["rejoin_pt"]), l2, c2)
            if de <= 30 and dr <= 30 and prj > pe:
                o.setdefault("also", []).append(f"{s2} D{d2}")
    # map + animation data: the service line around the section with its time profile, and each option's line with its own profile
    lo = max(0.0, min([b["s"] for b in buses] + [A - 1500.0]) - 300.0)
    hi = min(cum[-1], max([o["rejoin_s"] for o in opts] + [B + 300.0]) + 1200.0)
    n_s = max(2, min(500, int((hi - lo) / 25)))
    win_s = [lo + (hi - lo) * k / (n_s - 1) for k in range(n_s)]
    win = {"s": [round(x, 1) for x in win_s], "t": [round(diversion.t_at(prof, x), 3) for x in win_s],
           "pts": [[round(p[0], 6), round(p[1], 6)] for p in (diversion.point_at(line, cum, x) for x in win_s)]}
    dv_register_donors(blocks, svc, d, opts)
    for o in opts:
        shared = []
        for dn in donors:
            dseg = dn["seg"]
            smp = o["_seg"][::max(1, len(o["_seg"]) // 30)]
            near = sum(1 for p_ in smp if diversion.nearest_on_line(p_, dseg)[0] <= 35.0)
            if smp and near >= 0.6 * len(smp):
                shared.append(dn["label"])
        o["shared_with"] = shared
    for o in opts:
        dp = o.pop("_prof")
        seg = o.pop("_seg")
        k = max(1, len(seg) // 250)
        o["anim"] = {"pts": [[round(seg[i][0], 6), round(seg[i][1], 6)] for i in range(0, len(seg), k)] + [[round(seg[-1][0], 6), round(seg[-1][1], 6)]],
                     "t": [round(dp["t"][i], 3) for i in range(0, len(seg), k)] + [round(dp["t"][-1], 3)]}
        o.pop("prof_t", None)
    rejoin_stop = {}
    for o in opts:
        nxt = next((i for i, x in enumerate(stop_s) if x >= o["rejoin_s"]), None)
        if nxt is not None:
            o["rejoin_stop"] = {"code": stops[nxt]["code"], "name": stops[nxt]["name"]}
            rejoin_stop[o["n"]] = o["rejoin_stop"]
    blocked_stops = [{"code": stops[i]["code"], "name": stops[i]["name"], "lat": stops[i]["lat"], "lon": stops[i]["lon"]}
                     for i in sorted({i for a_, b_, bi_ in run["parts"] for i in diversion.stops_in(stop_s, a_, b_, 35.0)
                                      if diversion.nearest_on_line((stops[i]["lat"], stops[i]["lon"]), blocks[bi_]["line"])[0] <= 45.0})]
    last_pt = None
    if last:
        last_pt = {"lat": last["leave_pt"][0], "lon": last["leave_pt"][1], "s": last["leave_s"], "before_block_m": last["leave_before_block_m"], "option": last["n"],
                   "road": last["roads"][1] if len(last["roads"]) > 1 else (last["roads"][0] if last["roads"] else "")}
    return {"ok": True, "service": svc, "direction": d, "run": run_i, "road": road_txt, "block_a": round(A, 1), "block_b": round(B, 1), "H": H, "H_src": H_src,
            "closure_min": closure, "bus_type": bus, "bus_label": offservice.BUS_TYPES[bus], "line_source": src,
            "service_line": diversion.simplify(line, 700), "blocked_stops": blocked_stops,
            "block_pts": [[round(p[0], 6), round(p[1], 6)] for p in diversion.cut(line, cum, A, B)],
            "block_runs": [[[round(p[0], 6), round(p[1], 6)] for p in diversion.cut(line, cum, a_, b_)] for a_, b_, _ in run["parts"]],
            "section_blocks": run["blocks"], "section_roads": road_txt, "recommendation": rec, "polled": bool(poll),
            "small_hidden": len(small_hidden), "small_hidden_roads": [" > ".join(g["road"] for g in c["groups"])[:120] for c in small_hidden][:4],
            "junctions": {"exit": len(jx), "rejoin": len(jr)},
            "options": opts, "rejected": rejected, "candidates_tested": len(raw), "routing_error": routing_err if not opts else None,
            "last_point": last_pt, "buses": buses, "buses_on_diversion": on_div, "stops_polled": n_polled, "arrival_error": arr_err,
            "compare": cols, "wait_or_divert": wod, "window": win,
            "wait_allowed": diversion.wait_allowed(closure, P), "wait_max_min": P["wait_max_min"], "closure_desc": diversion.closure_desc(closure),
            "long_closure": long_closure, "trapped": trapped, "search": search,
            "router": ("TomTom Routing (" + DV_TT_MODE + " mode, vehicle dimensions, live traffic) \u00b7 OSRM fallback") if dv_use_tomtom() else "OSRM (OpenStreetMap)",
            "router_stats": dict(DV_TT_STATS) if dv_use_tomtom() else None,
            "rejects": rejects,
            "status": {"router": "TomTom" if dv_use_tomtom() else "OSRM", "tomtom_key": bool(TT["key"]), "tomtom_calls_today": TT["n"], "tomtom_cap": TT["cap"],
                       "tomtom_error": DV_TT_STATS.get("error"), "avoid_areas": len(avoid or []), "busroad_waypoints": len(bvias),
                       "bus_lines": TR_LINES.get("n", 0), "bus_lines_info": TR_LINES.get("info"), "manual_waypoints": len(manual or []),
                       "bus_types_known": len(DV_BT_CACHE.get("table") or {})},
            "time_basis": "LTA speed bands" if prof["known_share"] >= 0.5 else f"assumed {P['fallback_kmh']:g} km/h where no speed data (speed bands cover {round(prof['known_share'] * 100)}%)",
            "important_basis": "LTA stop names (Stn / Int / Ter / Hosp), services at the stop, and the OCC important-stop list",
            "updated": now.isoformat(timespec="seconds")}


# ---- 4b. network plan: every affected service at once (V16.15)
def dv_opt_compact(o, full=False):
    if not o:
        return None
    x = {k_: o.get(k_) for k_ in ("n", "name", "signature", "roads", "road_class", "added_km", "added_min", "div_km", "skipped_n", "important_n", "traffic",
                                  "feasibility", "feasibility_text", "leave_pt", "rejoin_pt", "rejoin_stop", "also", "used_before", "affected_buses",
                                  "shared_with", "permitted", "confidence", "score", "last_reachable", "rejoin_target", "earlier", "next_leg")}
    x["line"] = diversion.simplify([tuple(p_) for p_ in o["line"]], 160) if o.get("line") else []
    x["skipped"] = [{"code": s_["code"], "name": s_["name"], "important": s_["important"]} for s_ in o.get("skipped", [])] if full else None
    return x


@app.post("/api/diversion/plan_all")
async def api_dv_plan_all(request: Request):
    """Recommended diversion for EVERY affected service-direction, grouped where services can share one route, with the load each
    diverted road takes. Same LTA rules as the per-service view (1 bus roads only, DD where DD runs; 2 fewest stops skipped; 3 no U-turn). Decision support: nothing is executed."""
    dv_init()
    body = _dv_body(await request.body())
    blocks = dv_blocks(body)
    if not blocks:
        return JSONResponse({"error": "Place the road block on the map first."}, status_code=400)
    closure, bus, dims, allow_small = dv_opt_params(body)
    fl = dv_flags(body)
    manual = dv_manual(body)
    if manual:
        dv_register_manual(blocks, manual)
    st = await static()
    if not st["stops"] or not st["routes"]:
        return {"ok": False, "error": f"Bus routes could not be loaded from LTA ({st.get('error') or 'no data'})."}
    ents = body.get("entries")
    if not isinstance(ents, list) or not ents:
        ents, _ = await dv_entries(st, blocks)
    ents = [{"service": str(e.get("service")).upper(), "direction": int(e.get("direction") or 1), "run": int(e.get("run") or 0)} for e in ents[:30]]
    others = [{"service": e["service"], "direction": e["direction"]} for e in ents]
    sem = asyncio.Semaphore(3)

    async def one(i, e):
        async with sem:
            try:
                return await dv_options_cached(st, blocks, e["service"], e["direction"], e["run"], closure, bus, dims, others, i < DV_MAX_POLL, allow_small, fl["test"], fl["traffic"])
            except Exception as ex:
                return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    res = await asyncio.gather(*[one(i, e) for i, e in enumerate(ents)])
    retry = [i for i, (e, r) in enumerate(zip(ents, res)) if r.get("ok") and r["recommendation"]["action"] in ("manual", "review")
             and dv_donors(blocks, (e["service"], e["direction"]))]
    if retry:       # adopt corridors other services divert by (each still checked against the LTA rules)
        again = await asyncio.gather(*[one(i, ents[i]) for i in retry])
        for i, r in zip(retry, again):
            if r.get("ok"):
                res[i] = r
    rows, net_in = [], []
    for e, r in zip(ents, res):
        if not r.get("ok"):
            rows.append({**e, "ok": False, "error": r.get("error"), "action": "manual"})
            continue
        rec = r["recommendation"]
        pick = rec.get("option") or rec.get("route_if_extended")
        o = next((x for x in r["options"] if x["n"] == pick), None)
        col = next((c for c in r["compare"] if c["key"] == (f"o{rec['option']}" if rec.get("option") else "none")), None)
        none = next((c for c in r["compare"] if c["key"] == "none"), None)
        div = bool(o and rec.get("option"))
        choice = {"service": e["service"], "direction": e["direction"], "run": e["run"], "option": rec["option"] if div else 0,
                  "sig": o["signature"] if div else "wait", "action": (o["name"] + " \u2014 divert") if div else "Wait / regulate (no diversion)",
                  "roads": list(o["roads"]) if div else [], "signature": o["signature"] if div else "",
                  "leave_pt": o["leave_pt"] if div else None, "rejoin_pt": o["rejoin_pt"] if div else None,
                  "leave_s": o["leave_s"] if div else None, "rejoin_s": o["rejoin_s"] if div else None, "block_a": r["block_a"], "block_b": r["block_b"],
                  "line": o["line"] if div else [], "rejoin_name": (o.get("rejoin_stop") or {}).get("name", "") if div else "",
                  "skipped": [{"code": x["code"], "name": x["name"], "important": bool(x["important"])} for x in o["skipped"]] if div else [],
                  "buses": o.get("affected_buses", 0) if div else ((none or {}).get("affected") or 0),
                  "holds": [{"label": h["label"], "action": h["action"]} for h in ((col or {}).get("reg") or {}).get("holds", []) if h.get("hold", 0) >= .5],
                  "added_min": o["added_min"] if div else None, "road_class": (o.get("road_class") or {}).get("label", "") if div else "",
                  "shared_with": o.get("shared_with", []) if div else []}
        rows.append({**e, "ok": True, "road": r.get("road"), "H": r["H"], "action": rec["action"], "choice": choice, "headline": rec["headline"], "reasons": rec["reasons"],
                     "donors_checked": [x["label"] for x in dv_donors(blocks, (e["service"], e["direction"]))] if rec["action"] == "manual" else [],
                     "cautions": rec["cautions"], "option": dv_opt_compact(o), "options_n": len(r["options"]), "small_hidden": r["small_hidden"],
                     "buses": len([b for b in r["buses"] if b.get("status") not in ("passed_block",)]), "polled": r["polled"],
                     "bus_min": col["bus_min"] if col else None, "bus_min_none": none["bus_min"] if none else None,
                     "risk": (col or {}).get("hw", {}).get("risk"), "recovery_min": (col or {}).get("recovery_min"),
                     "rec_key": f"o{rec['option']}" if rec.get("option") else "none", "signature": o["signature"] if o and rec.get("option") else "wait"})
        if o and rec.get("option"):
            net_in.append({"service": e["service"], "direction": e["direction"], "H": r["H"], "option": o})
    net = diversion.network_plan(net_in)
    ok_rows = [x for x in rows if x.get("ok")]
    tot = {"service_dirs": len(rows), "divert": sum(1 for x in rows if x["action"] == "divert"), "wait": sum(1 for x in rows if x["action"] == "wait"),
           "manual": sum(1 for x in rows if x["action"] == "manual"), "review": sum(1 for x in rows if x["action"] == "review"),
           "skipped": sum((x["option"] or {}).get("skipped_n") or 0 for x in ok_rows if x["action"] == "divert"),
           "important": sum((x["option"] or {}).get("important_n") or 0 for x in ok_rows if x["action"] == "divert"),
           "bus_min": round(sum(x["bus_min"] or 0 for x in ok_rows), 1), "bus_min_none": None if any(x["bus_min_none"] is None for x in ok_rows) else round(sum(x["bus_min_none"] or 0 for x in ok_rows), 1),   # None = buses unable to move: not measurable
           "wait_allowed": diversion.wait_allowed(closure),
           "shared_routes": sum(1 for g in net["groups"] if len(g["services"]) > 1)}
    return {"ok": True, "road": dv_roads_txt(blocks), "blocks_n": len(blocks), "closure_min": closure, "allow_small": allow_small, "rows": rows,
            "network": net, "totals": tot, "poll_cap": DV_MAX_POLL, "ai": bool(DV_AI_KEY),
            "basis": "Each service: road routes (OSRM) that avoid every blockage, timed on LTA speed bands, road class from LTA road category / "
                     "OpenStreetMap. Chosen by the LTA diversion rules: 1 safety (main roads only), 2 fewest bus stops skipped, 3 no U-turn \u2014 no score.",
            "updated": now_sgt().isoformat(timespec="seconds")}


DV_AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
DV_AI_MODEL = os.environ.get("DIVERSION_AI_MODEL", "claude-sonnet-5-5").strip()
DV_AI_SYSTEM = """You are an assistant to a bus Operations Control Centre (OCC) controller in Singapore reviewing a proposed network diversion plan.
You receive a JSON plan that was computed by the platform from LTA DataMall and road-routing data. Rules:
- Use ONLY facts in the JSON. Never invent roads, restrictions, stops, times, bus positions or traffic.
- Do not change the recommended routes. You may point out risks, conflicts between services, and what the controller should verify.
- Anything not in the data must be stated as "needs verification", never assumed.
- Be brief and operational. Plain text, no markdown headers. Sections: SUMMARY (2-3 sentences), RISKS (up to 5 lines starting with "- "),
  CHECK BEFORE CONFIRMING (up to 5 lines starting with "- ")."""


@app.get("/api/diversion/ai")
async def api_dv_ai_status():
    return {"available": bool(DV_AI_KEY), "model": DV_AI_MODEL if DV_AI_KEY else None}


@app.post("/api/diversion/ai_review")
async def api_dv_ai_review(request: Request):
    """Optional plain-language review of a computed network plan by Claude (only when ANTHROPIC_API_KEY is set). Advisory text only."""
    if not DV_AI_KEY:
        return {"ok": False, "available": False, "error": "AI review is not configured (set ANTHROPIC_API_KEY)."}
    body = _dv_body(await request.body())
    plan = body.get("plan") or {}
    slim = {"blockage": plan.get("road"), "closure_min": plan.get("closure_min"), "totals": plan.get("totals"),
            "warnings": (plan.get("network") or {}).get("warnings"), "shared_routes": [{"roads": g["roads"], "services": g["services"]} for g in (plan.get("network") or {}).get("groups", [])],
            "services": [{k_: x.get(k_) for k_ in ("service", "direction", "action", "headline", "reasons", "cautions", "buses", "risk", "recovery_min")}
                         | {"route": {k_: (x.get("option") or {}).get(k_) for k_ in ("roads", "added_km", "added_min", "skipped_n", "important_n", "traffic", "feasibility", "road_class")}}
                         for x in (plan.get("rows") or [])[:30]]}
    try:
        r = await client().post("https://api.anthropic.com/v1/messages", timeout=60,
                                headers={"x-api-key": DV_AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                json={"model": DV_AI_MODEL, "max_tokens": 900, "system": DV_AI_SYSTEM,
                                      "messages": [{"role": "user", "content": "Review this diversion plan:\n" + json.dumps(slim, default=str)[:24000]}]})
        j = r.json()
        if r.status_code != 200:
            return {"ok": False, "available": True, "error": (j.get("error") or {}).get("message") or f"HTTP {r.status_code}"}
        text = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text").strip()
        return {"ok": True, "available": True, "text": text, "model": DV_AI_MODEL,
                "note": "AI-written review of the computed plan. Advisory only: routes are unchanged and the controller decides."}
    except Exception as e:
        return {"ok": False, "available": True, "error": f"AI service unavailable ({type(e).__name__})"}


# ---- 5. plans: save, confirm, share, acknowledge per OCC, update, end, recover (+ OCC Notes & Actions ticket)
def dv_row(pid):
    dv_init()
    r = bb_sql("SELECT * FROM diversion_plan WHERE id=?", (pid,), fetch=True)
    return r[0] if r else None


def dv_public(r, with_acks=True):
    def j(v, dflt):
        try:
            return json.loads(v) if v else dflt
        except (TypeError, ValueError):
            return dflt
    acks = []
    if with_acks:
        acks = bb_sql("SELECT occ, who, ts, revision FROM diversion_ack WHERE plan_id=? ORDER BY ts ASC", (r["id"],), fetch=True)
    rev = r["revision"] or 1
    latest = {}
    for a in acks:
        if a["revision"] == rev:
            latest[a["occ"]] = {"who": a["who"], "ts": a["ts"]}
    shared = [x for x in (r["shared"] or "").split(",") if x]
    teams = list(dict.fromkeys(([r["created_occ"]] if r["created_occ"] else []) + shared))
    return {"id": r["id"], "status": r["status"], "road": r["road"], "lat": r["lat"], "lon": r["lon"], "block": j(r["block"], {}),
            "closure_min": r["closure_min"], "closure_label": r["closure_label"], "services": j(r["services"], []), "summary": j(r["summary"], {}),
            "created_by": r["created_by"], "created_occ": r["created_occ"], "shared": shared, "revision": rev, "ticket_id": r["ticket_id"],
            "notice": r["notice"], "created": r["created_ts"], "updated": r["updated_ts"], "started": r["started_ts"], "ended": r["ended_ts"],
            "recovery": j(r["recovery"], None), "log": j(r["log"], []), "alert_acked": bool(r["alert_acked_ts"] and r["alert_acked_ts"] >= (r["updated_ts"] or 0) - 1),
            "acks": [{"occ": t, "acked": t in latest, "who": latest.get(t, {}).get("who"), "ts": latest.get(t, {}).get("ts"), "involved": t in teams,
                      "creator": t == r["created_occ"]} for t in OCC_TEAMS],
            "teams": list(OCC_TEAMS)}


def dv_log(pid, who, text):
    r = dv_row(pid)
    if not r:
        return
    try:
        lg = json.loads(r["log"] or "[]")
    except ValueError:
        lg = []
    lg.append({"ts": time.time(), "who": who, "text": text[:400]})
    bb_sql("UPDATE diversion_plan SET log=? WHERE id=?", (json.dumps(lg[-120:]), pid))


def dv_playbook_rows(lat, lon, road):
    dv_init()
    nr = offservice.norm_road(road or "")
    out = []
    for r in bb_sql("SELECT * FROM diversion_plan WHERE started_ts IS NOT NULL ORDER BY started_ts DESC LIMIT 300", fetch=True):
        near = r["lat"] is not None and lat is not None and diversion.dist_m((lat, lon), (r["lat"], r["lon"])) <= 400
        if not (near or (nr and r["road_norm"] == nr)):
            continue
        try:
            svcs = json.loads(r["services"] or "[]")
        except ValueError:
            svcs = []
        out.append({"id": r["id"], "road": r["road"], "last_used": datetime.fromtimestamp(r["started_ts"], SGT).strftime("%d %b %Y"),
                    "services": [f"{s_['service']}" for s_ in svcs], "detail": [{"service": s_["service"], "direction": s_["direction"], "roads": s_.get("roads") or [],
                                                                                   "signature": s_.get("signature"), "action": s_.get("action")} for s_ in svcs],
                    "block": json.loads(r["block"] or "{}"), "closure_label": r["closure_label"], "status": r["status"]})
        if len(out) >= 5:
            break
    return out


@app.get("/api/diversion/playbook")
async def api_dv_playbook(lat: float = 0.0, lon: float = 0.0, road: str = ""):
    return {"items": dv_playbook_rows(lat if lat else None, lon if lon else None, road)}


def dv_wait_block(body, r, services):
    """WAIT / REGULATE is only allowed for a short, known closure. -> (closure_min, error or None)"""
    if "closure_min" in body:
        c, _, _, _ = dv_opt_params(body)
    else:
        c = r["closure_min"] if "closure_min" in r.keys() else None
    small = [f"{x['service']} D{x['direction']}" for x in services if x.get("roads") and
             (x.get("permitted") is False or str(x.get("road_class") or "").upper() == "SMALL ROADS" or x.get("dd") == "SD_ONLY")]
    if small:
        return c, ("LTA rule 1 (safety): the diversion must use roads bus services already run on (with their bus stops), and for a "
                   "double-decker only where double-deck services run. Choose a permitted diversion for " + ", ".join(small) + ".")
    if diversion.wait_allowed(c):
        return c, None
    waits = [f"{x['service']} D{x['direction']}" for x in services if not x.get("roads") and not x.get("option")]
    if waits:
        return c, ("Buses are never held at a blockage (even a whole-day closure would leave them unable to move). "
                   "Choose a diversion that meets the LTA rules for " + ", ".join(waits) + ".")
    return c, None


def dv_clean_services(v):
    out = []
    for s_ in (v or [])[:30]:
        if not isinstance(s_, dict):
            continue
        out.append({"road_class": str(s_.get("road_class") or "")[:20], "permitted": s_.get("permitted") is not False,
                    "bus_road": str(s_.get("bus_road") or "")[:200], "dd": str(s_.get("dd") or "")[:12], "service": str(s_.get("service") or "")[:8].upper(), "direction": int(s_.get("direction") or 1) if str(s_.get("direction") or "1").isdigit() else 1,
                    "run": int(s_.get("run") or 0) if str(s_.get("run") or "0").isdigit() else 0,
                    "action": str(s_.get("action") or "")[:80], "option": s_.get("option") if isinstance(s_.get("option"), int) else None,
                    "roads": [str(x)[:60] for x in (s_.get("roads") or [])[:12]], "signature": str(s_.get("signature") or "")[:400],
                    "leave_pt": s_.get("leave_pt") if isinstance(s_.get("leave_pt"), list) else None, "rejoin_pt": s_.get("rejoin_pt") if isinstance(s_.get("rejoin_pt"), list) else None,
                    "leave_s": s_.get("leave_s"), "rejoin_s": s_.get("rejoin_s"), "block_a": s_.get("block_a"), "block_b": s_.get("block_b"),
                    "rejoin_name": str(s_.get("rejoin_name") or "")[:60], "line": dv_clean_line(s_.get("line"), 400) and [list(p) for p in dv_clean_line(s_.get("line"), 400)],
                    "skipped": [{"code": str(x.get("code"))[:6], "name": str(x.get("name") or "")[:60], "important": bool(x.get("important"))} for x in (s_.get("skipped") or [])[:40] if isinstance(x, dict)],
                    "buses": int(s_.get("buses") or 0) if str(s_.get("buses") or "0").isdigit() else 0,
                    "holds": [{"label": str(h.get("label"))[:10], "action": str(h.get("action"))[:40]} for h in (s_.get("holds") or [])[:20] if isinstance(h, dict)]})
    return out


def dv_block_json(blocks):
    """stored block: the first blockage at the top level (V16.14 readers) plus every blockage under "blocks" """
    b0 = blocks[0]
    return json.dumps({"line": [list(p) for p in b0["line"]], "road": b0["road"], "directed": b0["directed"], "length_m": round(diversion.line_m(b0["line"])),
                       "blocks": [{"line": [list(p) for p in b["line"]], "road": b["road"], "directed": b["directed"], "length_m": round(diversion.line_m(b["line"]))}
                                  for b in blocks]})


def dv_test_refusal(body):
    if body.get("test"):
        return JSONResponse({"error": "TEST data is on: plans built on synthetic buses cannot be saved, raised to OCC Live or confirmed. Switch the data to LIVE."}, status_code=400)
    return None


@app.post("/api/diversion/plans")
async def api_dv_create(request: Request):
    dv_init()
    body = _dv_body(await request.body())
    if dv_test_refusal(body):
        return dv_test_refusal(body)
    blocks = dv_blocks(body)
    if not blocks:
        return JSONResponse({"error": "A placed road block is required."}, status_code=400)
    blk, line = blocks[0], blocks[0]["line"]
    who = _who(request)
    occ = str(body.get("occ") or "").strip()
    occ = occ if occ in OCC_TEAMS else ""
    cum = diversion.cum_m(line)
    mid = diversion.point_at(line, cum, cum[-1] / 2)
    status = body.get("status") if body.get("status") in ("detected", "planned") else "detected"
    road = dv_roads_txt(blocks)[:80]
    now = time.time()
    pid = bb_insert("""INSERT INTO diversion_plan(created_ts, updated_ts, status, road, road_norm, lat, lon, block, closure_min, closure_label, services, created_by, created_occ,
              shared, revision, summary, log) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
           (now, now, status, road, offservice.norm_road(road), mid[0], mid[1], dv_block_json(blocks),
            body.get("closure_min") if isinstance(body.get("closure_min"), (int, float)) else None, str(body.get("closure_label") or "")[:40],
            json.dumps(dv_clean_services(body.get("services"))), who, occ, "", json.dumps(body.get("summary") if isinstance(body.get("summary"), dict) else {}),
            json.dumps([{"ts": now, "who": who, "text": f"Road blockage placed on {road} ({status.upper()})" + (f" by {occ}" if occ else "")}])))
    if not pid:
        return JSONResponse({"error": "The diversion could not be saved (database unavailable)."}, status_code=503)
    return {"ok": True, "plan": dv_public(dv_row(pid)), "saved": bool(BB.get("db_ok"))}


@app.get("/api/diversion/plans")
async def api_dv_list(include_ended: int = 0, limit: int = 50):
    dv_init()
    q = "SELECT * FROM diversion_plan" + ("" if include_ended else " WHERE status != 'ended'") + " ORDER BY updated_ts DESC LIMIT ?"
    return {"plans": [dv_public(r) for r in bb_sql(q, (max(1, min(200, limit)),), fetch=True)], "teams": list(OCC_TEAMS), "saved": bool(BB.get("db_ok"))}


@app.get("/api/diversion/plans/{pid}")
async def api_dv_get(pid: int):
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    return {"plan": dv_public(r)}


def dv_ticket_desc(r, notice):
    return (notice or "") + f"\n\nOpen the plan: /diversion?id={r['id']}"


@app.post("/api/diversion/plans/{pid}/confirm")
async def api_dv_confirm(pid: int, request: Request):
    """The controller confirms the operational diversion. Records it, prepares the notice and opens an OCC Notes & Actions ticket
    (category Diversion). Nothing is sent outside the platform."""
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    body = _dv_body(await request.body())
    if dv_test_refusal(body):
        return dv_test_refusal(body)
    who = _who(request)
    occ = str(body.get("occ") or r["created_occ"] or "").strip()
    occ = occ if occ in OCC_TEAMS else ""
    services = dv_clean_services(body.get("services")) or json.loads(r["services"] or "[]")
    share = [x for x in (body.get("share") or []) if x in OCC_TEAMS and x != occ]
    closure_c, werr = dv_wait_block(body, r, services)
    if werr:
        return JSONResponse({"error": werr}, status_code=400)
    if "closure_min" in body:
        bb_sql("UPDATE diversion_plan SET closure_min=? WHERE id=?", (closure_c, pid))
    now = time.time()
    closure_label = str(body.get("closure_label") or r["closure_label"] or "")[:40]
    nb = dv_blocks(body) if (body.get("blocks") or body.get("block")) else []
    if nb:
        bb_sql("UPDATE diversion_plan SET block=?, road=?, road_norm=? WHERE id=?", (dv_block_json(nb), dv_roads_txt(nb)[:80], offservice.norm_road(nb[0]["road"]), pid))
        r = dv_row(pid)
    plan = {"road": r["road"], "services": services, "effective": dv_hhmm(now), "status": "active", "closure": closure_label, "by": (occ + " \u00b7 " if occ else "") + who}
    text = str(body.get("notice") or "").strip()[:6000] or diversion.notice(plan)
    bb_sql("UPDATE diversion_plan SET status='active', services=?, notice=?, started_ts=COALESCE(started_ts, ?), updated_ts=?, created_occ=COALESCE(NULLIF(created_occ,''), ?), shared=?, closure_label=?, summary=? WHERE id=?",
           (json.dumps(services), text, now, now, occ, ",".join(share), closure_label, json.dumps(body.get("summary") if isinstance(body.get("summary"), dict) else {}), pid))
    dv_log(pid, who, "Diversion CONFIRMED and ACTIVE" + (f" by {occ}" if occ else ""))
    if occ:
        bb_sql("INSERT INTO diversion_ack(plan_id, occ, who, ts, revision) VALUES (?,?,?,?,?)", (pid, occ, who, now, r["revision"] or 1))
    tid = r["ticket_id"]
    if not tid:
        svc0 = services[0] if services else {}
        tid = bb_insert("""INSERT INTO occ_ticket(no, title, category, priority, status, service, direction, bus_reg, location, description, owner, shared, watchers,
                  personal, pinned, source_alert, created_ts, updated_ts, author) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?,?)""",
               (occ_no(), f"Road diversion \u2014 {r['road']}"[:140], "Diversion", "high", "action", svc0.get("service") or "", svc0.get("direction"), "", r["road"][:120],
                dv_ticket_desc(r, text)[:4000], who, ",".join(share), "", f"dv:{pid}", now, now, who))
        if tid:
            occ_log(tid, who, "created", f"Diversion plan #{pid} confirmed on Diversion Maps")
            if share:
                occ_log(tid, who, "shared", "Shared with " + ", ".join(share))
            bb_sql("UPDATE diversion_plan SET ticket_id=? WHERE id=?", (tid, pid))
    return {"ok": True, "plan": dv_public(dv_row(pid))}


@app.post("/api/diversion/plans/{pid}/update")
async def api_dv_update(pid: int, request: Request):
    """New revision of an active diversion. Every OCC sees the latest revision and must acknowledge it again."""
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    body = _dv_body(await request.body())
    who = _who(request)
    rev = (r["revision"] or 1) + 1
    services = dv_clean_services(body.get("services")) or json.loads(r["services"] or "[]")
    closure_c, werr = dv_wait_block(body, r, services)
    if werr:
        return JSONResponse({"error": werr}, status_code=400)
    if "closure_min" in body:
        bb_sql("UPDATE diversion_plan SET closure_min=? WHERE id=?", (closure_c, pid))
    closure_label = str(body.get("closure_label") or r["closure_label"] or "")[:40]
    note = str(body.get("note") or "").strip()[:400]
    plan = {"road": r["road"], "services": services, "effective": dv_hhmm(r["started_ts"] or time.time()), "status": r["status"], "closure": closure_label, "revision": rev,
            "by": ((r["created_occ"] + " \u00b7 ") if r["created_occ"] else "") + who}
    text = str(body.get("notice") or "").strip()[:6000] or diversion.notice(plan)
    now = time.time()
    bb_sql("UPDATE diversion_plan SET revision=?, services=?, notice=?, closure_label=?, updated_ts=? WHERE id=?", (rev, json.dumps(services), text, closure_label, now, pid))
    nb = dv_blocks(body) if (body.get("blocks") or body.get("block")) else []
    if nb:
        bb_sql("UPDATE diversion_plan SET block=?, road=?, road_norm=? WHERE id=?", (dv_block_json(nb), dv_roads_txt(nb)[:80], offservice.norm_road(nb[0]["road"]), pid))
    dv_log(pid, who, f"Revision {rev}" + (f": {note}" if note else ""))
    occ = str(body.get("occ") or "").strip()
    if occ in OCC_TEAMS:
        bb_sql("INSERT INTO diversion_ack(plan_id, occ, who, ts, revision) VALUES (?,?,?,?,?)", (pid, occ, who, now, rev))
    if r["ticket_id"]:
        occ_log(r["ticket_id"], who, "comment", f"Diversion revision {rev}" + (f" \u2014 {note}" if note else "") + "\n" + text[:3000])
        occ_touch(r["ticket_id"])
    return {"ok": True, "plan": dv_public(dv_row(pid))}


@app.post("/api/diversion/notice")
async def api_dv_notice(request: Request):
    """Preview of the operational message (nothing is saved or sent)."""
    body = _dv_body(await request.body())
    plan = {"road": str(body.get("road") or "")[:80], "services": dv_clean_services(body.get("services")), "effective": str(body.get("effective") or dv_hhmm())[:5],
            "status": str(body.get("status") or "active")[:12], "closure": str(body.get("closure_label") or "")[:40],
            "revision": int(body.get("revision") or 1) if str(body.get("revision") or "1").isdigit() else 1, "by": str(body.get("by") or "")[:60]}
    return {"text": diversion.notice(plan)}


@app.post("/api/diversion/plans/{pid}/notify")
async def api_dv_notify(pid: int, request: Request):
    """NOTIFY OCC: posts the current notice to the diversion's OCC Notes & Actions ticket (seen in OCC Live by the shared OCCs).
    Internal to the platform - nothing is sent externally."""
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    who = _who(request)
    if not r["ticket_id"]:
        return JSONResponse({"error": "Confirm the diversion first: the OCC ticket is created on confirmation."}, status_code=400)
    occ_log(r["ticket_id"], who, "comment", f"Diversion notice (revision {r['revision'] or 1}) sent to OCCs\n" + (r["notice"] or "")[:3500])
    occ_touch(r["ticket_id"])
    dv_log(pid, who, "Notice posted to the OCC ticket")
    return {"ok": True, "ticket_id": r["ticket_id"]}


@app.post("/api/diversion/plans/{pid}/share")
async def api_dv_share(pid: int, request: Request):
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    body = _dv_body(await request.body())
    add = [x for x in (body.get("occs") or []) if x in OCC_TEAMS]
    if not add:
        return JSONResponse({"error": "Choose at least one OCC."}, status_code=400)
    cur = [x for x in (r["shared"] or "").split(",") if x]
    new = [x for x in add if x not in cur and x != r["created_occ"]]
    cur += new
    bb_sql("UPDATE diversion_plan SET shared=?, updated_ts=? WHERE id=?", (",".join(cur), time.time(), pid))
    who = _who(request)
    if new:
        dv_log(pid, who, "Shared with " + ", ".join(new))
        if r["ticket_id"]:
            t = occ_get(r["ticket_id"])
            if t:
                tsh = [x for x in (t["shared"] or "").split(",") if x]
                bb_sql("UPDATE occ_ticket SET shared=?, updated_ts=? WHERE id=?", (",".join(list(dict.fromkeys(tsh + new))), time.time(), r["ticket_id"]))
                occ_log(r["ticket_id"], who, "shared", "Shared with " + ", ".join(new))
    return {"ok": True, "plan": dv_public(dv_row(pid))}


@app.post("/api/diversion/plans/{pid}/ack")
async def api_dv_ack(pid: int, request: Request):
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    body = _dv_body(await request.body())
    occ = str(body.get("occ") or "").strip()
    if occ not in OCC_TEAMS:
        return JSONResponse({"error": "Choose your OCC first."}, status_code=400)
    who = _who(request)
    rev = r["revision"] or 1
    if not bb_sql("SELECT id FROM diversion_ack WHERE plan_id=? AND occ=? AND revision=?", (pid, occ, rev), fetch=True):
        bb_sql("INSERT INTO diversion_ack(plan_id, occ, who, ts, revision) VALUES (?,?,?,?,?)", (pid, occ, who, time.time(), rev))
        dv_log(pid, who, f"\u2713 Acknowledged by {occ} (revision {rev})")
        if r["ticket_id"]:
            occ_log(r["ticket_id"], who, "ack", f"\u2713 Diversion revision {rev} acknowledged by {occ} ({who})")
    return {"ok": True, "plan": dv_public(dv_row(pid))}


@app.post("/api/diversion/plans/{pid}/status")
async def api_dv_status(pid: int, request: Request):
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    body = _dv_body(await request.body())
    new = body.get("status")
    allowed = {"detected": ("planned", "ended"), "planned": ("detected", "ended"), "active": ("monitoring",), "monitoring": ("active",)}   # ended = discard a plan never activated
    if new not in allowed.get(r["status"], ()):
        return JSONResponse({"error": f"Cannot change {r['status'].upper()} to {str(new).upper()} here."}, status_code=400)
    who = _who(request)
    if new == "ended":
        bb_sql("UPDATE diversion_plan SET status='ended', ended_ts=?, updated_ts=? WHERE id=?", (time.time(), time.time(), pid))
        dv_log(pid, who, "Plan discarded (never activated)")
        if r["ticket_id"]:
            occ_log(r["ticket_id"], who, "status", "Diversion plan discarded (never activated)")
            bb_sql("UPDATE occ_ticket SET status='resolved', resolved_ts=?, updated_ts=? WHERE id=?", (time.time(), time.time(), r["ticket_id"]))
        return {"ok": True, "plan": dv_public(dv_row(pid))}
    bb_sql("UPDATE diversion_plan SET status=?, updated_ts=? WHERE id=?", (new, time.time(), pid))
    dv_log(pid, who, f"Status {r['status'].upper()} \u2192 {new.upper()}")
    if r["ticket_id"]:
        occ_log(r["ticket_id"], who, "status", f"Diversion status {r['status'].upper()} \u2192 {new.upper()}")
    return {"ok": True, "plan": dv_public(dv_row(pid))}


@app.post("/api/diversion/plans/{pid}/end")
async def api_dv_end(pid: int, request: Request):
    """Road reopened: RETURN-TO-NORMAL analysis. Checks where every bus of every diverted service is now (LTA Bus Arrival) and proposes
    who completes the diversion and who resumes the normal route. Status -> RECOVERING; the controller confirms the recovery plan."""
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    who = _who(request)
    st = await static()
    fq = await freq_table()
    bands = await bands_state()
    sfn = dv_speed_fn(bands.get("idx"))
    now = now_sgt()
    out = []
    for s_ in json.loads(r["services"] or "[]"):
        svc, d = s_["service"], int(s_["direction"])
        line, _ = await dv_line(st, svc, d)
        if not line or s_.get("leave_s") is None:
            out.append({"service": svc, "direction": d, "rows": [], "note": "No diversion route recorded for this service (wait / regulate plan)."})
            continue
        stops = route_stops(st, svc, d)
        pr = dv_prep(svc, d, line, stops)
        prof = diversion.profile(line, sfn, pr["stop_s"])
        dl = [tuple(p) for p in (s_.get("line") or [])]
        A = float(s_.get("block_a") or s_["leave_s"])
        buses, _, err = await dv_buses(st, svc, d, line, pr["cum"], stops, pr["stop_s"], float(s_["leave_s"]) - 6000, float(s_["rejoin_s"]), div_lines=[dl] if dl else None)
        diversion.label_buses([b for b in buses if b["s"] is not None] or buses, svc)
        rows_in = []
        for b in buses:
            eta = None
            if b["on_diversion"] and dl:
                dd_, pos, _, _ = diversion.nearest_on_line((b["lat"], b["lon"]), dl)
                dprof = diversion.profile(dl, sfn, None)
                eta = round(dprof["t"][-1] - diversion.t_at(dprof, pos), 1)
            rows_in.append({"label": b.get("label") or "?", "s": b["s"] if not b["on_diversion"] else None, "on_diversion": b["on_diversion"], "eta_rejoin": eta})
        rtn = diversion.return_to_normal(rows_in, float(s_["leave_s"]), float(s_["rejoin_s"]), A)
        H, _ = bb_resolve_hw(svc, d, now, fq)
        norm = rtn["last_rejoin_min"] + (H or 0) * 1.0
        out.append({"service": svc, "direction": d, "rows": rtn["rows"], "still_affected": rtn["still_affected"],
                    "normalise_min": round(norm, 0), "H": H, "arrival_error": err,
                    "basis": "last bus on the diversion rejoins, then one scheduled headway to re-space (estimate)"})
    rec = {"ts": time.time(), "by": who, "services": out, "normalise_min": max([x.get("normalise_min") or 0 for x in out] + [0])}
    bb_sql("UPDATE diversion_plan SET status='recovering', recovery=?, updated_ts=? WHERE id=?", (json.dumps(rec), time.time(), pid))
    dv_log(pid, who, "Road reopened \u2014 RETURN-TO-NORMAL analysis run (RECOVERING)")
    if r["ticket_id"]:
        occ_log(r["ticket_id"], who, "status", "Road reopened \u2014 diversion RECOVERING")
        bb_sql("UPDATE occ_ticket SET status='monitoring', updated_ts=? WHERE id=?", (time.time(), r["ticket_id"]))
    return {"ok": True, "recovery": rec, "plan": dv_public(dv_row(pid))}


@app.post("/api/diversion/plans/{pid}/recovery")
async def api_dv_recovery(pid: int, request: Request):
    r = dv_row(pid)
    if not r:
        return JSONResponse({"error": "Diversion not found."}, status_code=404)
    body = _dv_body(await request.body())
    who = _who(request)
    close = bool(body.get("close", True))
    now = time.time()
    if close:
        bb_sql("UPDATE diversion_plan SET status='ended', ended_ts=?, updated_ts=? WHERE id=?", (now, now, pid))
        dv_log(pid, who, "Recovery plan confirmed \u2014 diversion ENDED")
        if r["ticket_id"]:
            bb_sql("UPDATE occ_ticket SET status='resolved', resolved_ts=?, resolution_outcome=?, resolution_action=?, updated_ts=? WHERE id=?",
                   (now, "Road reopened, service returned to normal route", "Diversion ended on Diversion Maps", now, r["ticket_id"]))
            occ_log(r["ticket_id"], who, "resolve", "Diversion ended \u2014 service returned to normal route")
    else:
        dv_log(pid, who, "Recovery plan confirmed \u2014 monitoring headway")
    return {"ok": True, "plan": dv_public(dv_row(pid))}


# ---- 6. important stops maintained by the OCC (added to the automatic Stn / Int / Ter / Hosp / transfer-hub detection)
@app.get("/api/diversion/important")
async def api_dv_important():
    dv_init()
    st = await static()
    rows = bb_sql("SELECT * FROM important_stop ORDER BY code", fetch=True)
    return {"stops": [{"code": r["code"], "reason": r["reason"], "by": r["by_name"], "name": (st["stops"].get(r["code"]) or {}).get("name", "")} for r in rows]}


@app.post("/api/diversion/important")
async def api_dv_important_save(request: Request):
    dv_init()
    body = _dv_body(await request.body())
    code = re.sub(r"\D", "", str(body.get("code") or ""))
    if len(code) != 5:
        return JSONResponse({"error": "Enter a 5-digit bus stop code."}, status_code=400)
    if body.get("remove"):
        bb_sql("DELETE FROM important_stop WHERE code=?", (code,))
    else:
        bb_sql("INSERT OR REPLACE INTO important_stop(code, reason, by_name, ts) VALUES (?,?,?,?)", (code, str(body.get("reason") or "")[:60], _who(request), time.time()))
    return {"ok": True}


# ---- 7. future automatic incident mode: LTA incidents / road works that MAY block a bus road. Suggestions only - never activates a diversion.
async def dv_detect():
    st = await static()
    try:
        raw = (await api_incidents("", 1)).get("incidents", [])
    except Exception:
        raw = []
    out = []
    for x in raw:
        ty = (x.get("type") or "").lower()
        if not any(k in ty for k in DV_INCIDENT_TYPES):
            continue
        near = [c for c, s_ in st["stops"].items() if abs(s_["lat"] - x["lat"]) < 0.004 and abs(s_["lon"] - x["lon"]) < 0.004
                and diversion.dist_m((s_["lat"], s_["lon"]), (x["lat"], x["lon"])) <= 250]
        svcs = sorted({v for c in near for v in st["at_stop"].get(c, ())}, key=dv_svc_key)
        if not svcs:
            continue
        out.append({"id": "inc:" + hashlib.sha1(f"{x['lat']:.5f},{x['lon']:.5f},{x['type']}".encode()).hexdigest()[:10], "type": x["type"], "message": x["message"],
                    "lat": x["lat"], "lon": x["lon"], "services_near": svcs[:12], "source": "LTA Traffic Incidents"})
    return out[:20]


@app.get("/api/diversion/detect")
async def api_dv_detect():
    return {"items": await dv_detect(), "auto_alerts": DV_AUTO,
            "note": "Potential road blockages from LTA Traffic Incidents near bus stops. Review each one: nothing is diverted automatically."}


# ---- 8. OCC Live alert queue + notification bell integration
def dv_queue_items():
    dv_init()
    items = []
    for r in bb_sql("SELECT * FROM diversion_plan WHERE status NOT IN ('ended') ORDER BY updated_ts DESC LIMIT 30", fetch=True):
        p = dv_public(r, with_acks=False)
        sm = p["summary"] or {}
        svcs = p["services"] or []
        chips = sm.get("chips") or [f"{s_['service']} D{s_['direction']}" for s_ in svcs]
        n_svc = sm.get("services") if sm.get("services") is not None else len({s_["service"] for s_ in svcs})
        n_bus = sm.get("buses")
        acked = p["alert_acked"]
        crit = p["status"] in ("detected", "planned") and not acked
        title = ("Diversion impact detected" if p["status"] in ("detected", "planned") else f"Diversion {p['status']}") + f" \u2013 {p['road']}"
        detail = f"{n_svc} services affected" + (f" \u00b7 {n_bus} buses potentially affected" if n_bus is not None else "") + (" \u00b7 " + " \u00b7 ".join(chips[:8]) if chips else "")
        items.append({"id": f"dv:{p['id']}", "src": "diversion", "group": "diversion", "severity": "critical" if crit else "high",
                      "status": "acknowledged" if acked else ("re-escalated" if r["alert_acked_ts"] else "new"),
                      "title": title, "service": "", "dir": None, "location": p["road"], "detail": detail,
                      "since": p["created"], "last": p["updated"], "acked_by": r["alert_acked_by"], "acked_at": r["alert_acked_ts"],
                      "href": f"/diversion?id={p['id']}", "raw_id": p["id"], "operator": ""})
    if DV_AUTO:
        for x in DV.get("auto", []):
            items.append({"id": x["id"], "src": "diversion", "group": "diversion", "severity": "high", "status": "new",
                          "title": f"Potential road blockage \u2013 {x['type']}", "service": "", "dir": None, "location": x["message"][:80],
                          "detail": "Services nearby: " + ", ".join(x["services_near"][:8]) + " \u00b7 review on Diversion Maps", "since": x.get("seen"), "last": x.get("seen"),
                          "acked_by": None, "acked_at": None, "href": f"/diversion?lat={x['lat']}&lon={x['lon']}", "raw_id": None, "operator": ""})
    return items


def dv_ack_alert(pid, by):
    r = dv_row(pid)
    if not r:
        return False
    bb_sql("UPDATE diversion_plan SET alert_acked_ts=?, alert_acked_by=? WHERE id=?", (time.time(), by, pid))
    dv_log(pid, by, f"OCC Live alert acknowledged by {by}")
    return True


async def dv_auto_loop():
    """future automatic incident mode (DIVERSION_AUTO_DETECT=1): refreshes the potential-blockage list for the OCC Live queue every 2 min"""
    while True:
        try:
            now = time.time()
            prev = {x["id"]: x.get("seen") for x in DV.get("auto", [])}
            items = await dv_detect()
            for x in items:
                x["seen"] = prev.get(x["id"]) or now
            DV["auto"] = items
        except Exception:
            pass
        await asyncio.sleep(120)


@app.get("/diversion.js")
async def diversion_js():
    return Response((HERE / "diversion.js").read_text(encoding="utf-8"), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/diversion", response_class=HTMLResponse)
async def diversion_page(request: Request):
    if request.query_params.get("id"):        # OCC plan links (alerts, tickets) open in the classic planner
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/diversion/classic?" + str(request.query_params), status_code=302)
    return _page("diversion.html")


@app.get("/diversion/classic", response_class=HTMLResponse)
async def diversion_classic_page():
    return _page("diversion_classic.html")


@app.get("/diversion/tv", response_class=HTMLResponse)
async def diversion_tv_page():
    """V16.27: live diversions for a TV / LED panel (full screen, no menu)"""
    return _page("diversion_tv.html")


@app.get("/diversion-tv.js")
async def diversion_tv_js():
    return Response((HERE / "diversion_tv.js").read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/diversion/history", response_class=HTMLResponse)
async def diversion_history_page():
    """V16.27: saved diversions - filter, duplicate with a new period, end, export to Excel"""
    return _page("diversion_history.html")


@app.get("/diversion-history.js")
async def diversion_history_js():
    return Response((HERE / "diversion_history.js").read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/diversion-classic.js")
async def diversion_classic_js():
    return Response((HERE / "diversion_classic.js").read_text(encoding="utf-8"), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})





# =========================================================================== V16.14 Diversion Maps - LTA rule 1: bus roads + bus types
DV_BT_CACHE = {"at": 0.0, "table": {}}


def bt_flush():
    """write bus types seen in LTA Bus Arrival into sqlite (counts add up over time)"""
    if not BT_SEEN:
        return
    dv_init()
    items = list(BT_SEEN.items())
    BT_SEEN.clear()
    now = time.time()
    for (svc, typ), n in items:
        try:
            bb_sql("INSERT INTO bus_type_seen(service, type, n, last_ts) VALUES(?,?,?,?) "
                   "ON CONFLICT(service, type) DO UPDATE SET n = n + excluded.n, last_ts = excluded.last_ts", (svc, typ, n, now))
        except Exception:
            pass
    DV_BT_CACHE["at"] = 0.0


def bt_table(force=False):
    """service -> {"seen": {type: n}, "override": [types] | None, "note", "by"}"""
    bt_flush()
    if not force and time.time() - DV_BT_CACHE["at"] < 60:
        return DV_BT_CACHE["table"]
    dv_init()
    t = {}
    for r in bb_sql("SELECT service, type, n, last_ts FROM bus_type_seen", fetch=True) or []:
        t.setdefault(r["service"], {"seen": {}, "override": None})["seen"][r["type"]] = r["n"]
    for r in bb_sql("SELECT * FROM bus_type_override", fetch=True) or []:
        e = t.setdefault(r["service"], {"seen": {}, "override": None})
        e["override"] = [x for x in (r["types"] or "").split(",") if x]
        e["note"], e["by"], e["ts"] = r["note"] or "", r["by_name"] or "", r["ts"]
    DV_BT_CACHE.update(at=time.time(), table=t)
    return t


def bt_class(svc, table=None):
    """ "DD" if the service runs double-deckers, "SD" if it is known to run only single-deck / articulated buses,
    None if not known yet. OCC entries override what LTA Bus Arrival has shown."""
    e = (table if table is not None else bt_table()).get(svc)
    if not e:
        return None
    if e.get("override"):
        return "DD" if "DD" in e["override"] else "SD"
    seen = e.get("seen") or {}
    if seen.get("DD", 0) >= 1:
        return "DD"
    if sum(seen.values()) >= 5:
        return "SD"
    return None


DV_NET_CACHE = {}


def dv_busnet(st, box, pad_km=0.8):
    """bus network around a diversion: every service with a stop within 6 km of the area, as the real-road route line
    (TR_LINES) clipped to the area, or consecutive-stop chords when a service has no line."""
    s0, w0, n0, e0 = box
    pk = pad_km / 111.0
    S_, W_, N_, E_ = s0 - pk, w0 - pk, n0 + pk, e0 + pk
    key = (round(S_, 3), round(W_, 3), round(N_, 3), round(E_, 3), TR_LINES.get("at"), len(st.get("routes") or {}))
    if key in DV_NET_CACHE:
        return DV_NET_CACHE[key]
    big = 6.0 / 111.0
    near_stops = {c for c, x in st["stops"].items() if s0 - big <= x["lat"] <= n0 + big and w0 - big <= x["lon"] <= e0 + big}
    sds = [k for k, rr in st["routes"].items() if any(r["code"] in near_stops for r in rr)]

    def inside(p):
        return S_ <= p[0] <= N_ and W_ <= p[1] <= E_
    lines, chords = {}, []
    for sd in sds:
        ent = (TR_LINES.get("lines") or {}).get(sd)
        ln = ent.get("line") if isinstance(ent, dict) else None
        if ln and len(ln) >= 2:
            run, k = [], 0
            for p in ln:
                if inside(p):
                    run.append((p[0], p[1]))
                elif run:
                    if len(run) >= 2:
                        lines[(sd, k)] = run
                        k += 1
                    run = []
            if len(run) >= 2:
                lines[(sd, k)] = run
        else:
            rr = st["routes"][sd]
            for a, b in zip(rr, rr[1:]):
                A_, B_ = st["stops"].get(a["code"]), st["stops"].get(b["code"])
                if A_ and B_ and (inside((A_["lat"], A_["lon"])) or inside((B_["lat"], B_["lon"]))):
                    pa, pb = (A_["lat"], A_["lon"]), (B_["lat"], B_["lon"])
                    if diversion.dist_m(pa, pb) < 700:
                        chords.append((sd, pa, pb))
    net = diversion.BusNet({}, chords)
    for (sd, k), v in lines.items():         # several clipped runs per service: add each
        net._add(sd, diversion.densify(v, 20.0), diversion.PARAMS["busroad_tol_m"])
    if len(DV_NET_CACHE) > 40:
        DV_NET_CACHE.clear()
    DV_NET_CACHE[key] = net
    return net


async def dv_probe_types(st, services, box, limit=6):
    """services whose bus type is unknown: ask LTA Bus Arrival at one of their stops in the area (cached per stop)"""
    s0, w0, n0, e0 = box
    pk = 2.0 / 111.0
    todo, stops = [], []
    tab = bt_table()
    for svc in services:
        if bt_class(svc, tab) is not None or svc in todo:
            continue
        code = None
        for (sv, d), rr in st["routes"].items():
            if sv != svc:
                continue
            for r in rr:
                x = st["stops"].get(r["code"])
                if x and s0 - pk <= x["lat"] <= n0 + pk and w0 - pk <= x["lon"] <= e0 + pk:
                    code = r["code"]
                    break
            if code:
                break
        if code:
            todo.append(svc)
            stops.append(code)
        if len(todo) >= limit:
            break
    if not stops:
        return 0
    res = await asyncio.gather(*[arrivals_raw(c) for c in dict.fromkeys(stops)], return_exceptions=True)
    now = now_sgt()
    for r in res:
        if isinstance(r, dict):
            parse_services(r, now)        # records every bus type it sees
    bt_flush()
    bt_table(force=True)
    return len(todo)


def dv_bus_road(c, net, bus, table):
    return diversion.bus_road_check(c["seg"], c["groups"], net, lambda sv: bt_class(sv, table), bus)


@app.get("/api/diversion/bustypes")
async def api_dv_bustypes(q: str = ""):
    """bus type per service: learned from LTA Bus Arrival, or entered by OCC (verify with landtransportguru.net)"""
    t = bt_table(force=True)
    out = []
    for svc in sorted(t, key=lambda x: (len(x), x)):
        if q and q.upper() not in svc:
            continue
        e = t[svc]
        out.append({"service": svc, "class": bt_class(svc, t), "seen": e.get("seen") or {}, "override": e.get("override"),
                    "note": e.get("note", ""), "by": e.get("by", ""), "source": "OCC entry" if e.get("override") else "LTA Bus Arrival"})
    return {"services": out[:400], "reference": "https://landtransportguru.net/",
            "basis": "Learned from the bus Type in LTA Bus Arrival (SD / DD / BD) whenever this platform polls it; OCC entries override."}


@app.post("/api/diversion/bustypes")
async def api_dv_bustypes_set(request: Request):
    body = _dv_body(await request.body())
    svc = str(body.get("service") or "").strip().upper()[:6]
    if not re.match(r"^[0-9A-Z]{1,5}$", svc):
        return JSONResponse({"error": "Service number required."}, status_code=400)
    dv_init()
    if body.get("remove"):
        bb_sql("DELETE FROM bus_type_override WHERE service=?", (svc,))
    else:
        types = [t for t in (body.get("types") or []) if t in ("SD", "DD", "BD")]
        if not types:
            return JSONResponse({"error": "Choose SD, DD and/or BD."}, status_code=400)
        bb_sql("INSERT OR REPLACE INTO bus_type_override(service, types, note, by_name, ts) VALUES(?,?,?,?,?)",
               (svc, ",".join(types), str(body.get("note") or "")[:200], _who(request), time.time()))
    bt_table(force=True)
    for k in [k for k in CACHE if str(k).startswith("dvopt:")]:      # options depend on bus types: recompute
        CACHE.pop(k, None)
    return {"ok": True, "service": svc, "class": bt_class(svc)}



@app.get("/api/diversion/route")
async def api_dv_route(service: str = "", direction: int = 1, lat: float = None, lon: float = None):
    """a service's CORRECT road line for the map (checked against LTA stop distances; segments near lat/lon fixed first)"""
    svc = service.strip().upper()
    st = await static()
    if not svc or not st.get("routes"):
        return {"line": [], "error": "unknown service"}
    focus = (lat, lon) if lat is not None and lon is not None else None
    line, label = await dv_line(st, svc, direction, focus=focus, max_fix=12)
    ent = DV_LINES.get((svc, direction)) or {}
    return {"service": svc, "direction": direction, "line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(line, 400)] if line else [],
            "source": label, "unverified": ent.get("unverified", 0)}


# =========================================================================== V16.15 deterministic stop-to-stop diversion engine
# LTA bus route -> blockage -> last reachable stop A -> rejoin targets B, C, D... -> routing engine -> HARD validation
# -> fail: next stop -> valid: verify the next original leg -> alternatives -> score -> select -> explanation.
# Every diversion geometry comes from the routing engine (TomTom, else OSRM). Nothing here draws or infers a road.
def dv_osm_flags(seg, ways, min_m=40.0):
    """OpenStreetMap stretches a bus must not use (private / no access / service / track / footway) -> ["service road (60 m)", ...]"""
    if not ways or len(seg) < 2:
        return []
    cm = diversion.cum_m(seg)
    out, run, run_txt = [], 0.0, ""
    step = 20.0
    x = 0.0
    while x <= cm[-1]:
        p = diversion.point_at(seg, cm, x)
        best, bw = 12.0, None
        for w in ways:
            if len(w["geom"]) >= 2:
                dd = diversion.nearest_on_line(p, w["geom"])[0]
                if dd < best:
                    best, bw = dd, w
        bad = ""
        if bw:
            acc = bw.get("acc") or {}
            open_to_bus = acc.get("psv") in ("yes", "designated") or acc.get("bus") in ("yes", "designated")
            if bw.get("hw") in diversion.RESTRICTED_HW and not open_to_bus:
                bad = f"{bw.get('hw')} road" + (f" {bw.get('name')}" if bw.get("name") else "")
            elif not open_to_bus and any(acc.get(k) in diversion.RESTRICTED_ACCESS for k in ("access", "motor_vehicle", "motorcar", "hgv")):
                bad = "restricted access" + (f" ({bw.get('name')})" if bw.get("name") else "")
        if bad:
            run += step
            run_txt = run_txt or bad
        else:
            if run >= min_m:
                out.append(f"{run_txt} ({run:.0f} m)")
            run, run_txt = 0.0, ""
        x += step
    if run >= min_m:
        out.append(f"{run_txt} ({run:.0f} m)")
    return out


def dv_stops_along(st, seg, tol=25.0, ends_m=60.0):
    """LTA bus stops along the diverted roads (evidence that buses use them). Stops within ends_m of either end are not
    counted: those are the service's own stops at the leave / rejoin points, or stops on the cross streets there."""
    if len(seg) < 2:
        return 0
    s0, w0, n0, e0 = diversion.bbox(seg, tol + 5)
    cm = diversion.cum_m(seg)
    n = 0
    for x in st["stops"].values():
        if s0 <= x["lat"] <= n0 and w0 <= x["lon"] <= e0:
            dd, ss = diversion.nearest_on_line((x["lat"], x["lon"]), seg, cm)[:2]
            if dd <= tol and ends_m <= ss <= cm[-1] - ends_m:
                n += 1
    return n


_TERMINAL_RX = re.compile(r"\b(INT|INTERCHANGE|TER|TERMINAL|BUS PK|BUS PARK|DEPOT)\b", re.I)


def dv_heading_near(p, line, cum, s, span=40.0):
    """V16.26g: the bus's heading at p, from the piece of its route around position s (m along the route) only.
    A loop route, or a road the bus drives out and back on, passes the same spot twice - once each way. The nearest piece
    of line alone can then be the OTHER passage: the routing engine was told the bus faces the wrong way at the stop, and
    its route turned round (a U-turn) to get there. Same tie-break as nearest_on_line (the first piece)."""
    px, py = diversion.xy(p)
    best, brg = 1e18, None
    for i in range(len(line) - 1):
        if cum[i + 1] < s - span or cum[i] > s + span:
            continue
        ax, ay = diversion.xy(line[i])
        bx, by = diversion.xy(line[i + 1])
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best:
            best, brg = d, diversion.bearing(line[i], line[i + 1])
    return brg if brg is not None else diversion.nearest_on_line(p, line, cum)[3]


def dv_free_start(ctx, i):
    """A bus at an interchange / terminal, or at the first stop of its trip, can leave by ANY exit (e.g. turn right
    out of the interchange instead of the normal left). Stops named "Opp / Aft / Bef ... Int" are on the road outside."""
    if i == 0:
        return True
    nm = (ctx["stops"][i].get("name") or "").strip().upper()
    if nm.startswith(("OPP ", "AFT ", "BEF ", "BEFORE ", "AFTER ")):
        return False
    return bool(_TERMINAL_RX.search(nm))


async def route_between_stops(ctx, i, j, s0=None):
    """road routes from (just after) stop i to (just before) stop j, from the routing engine only: TomTom with the
    blockage + buffer as avoid areas and up to 3 alternatives; OSRM alternatives and routes via nearby bus roads;
    the controller's drawn points and other services' corridors as extra waypoint sets. -> [route]"""
    P, at, stop_s = ctx["P"], ctx["at"], ctx["stop_s"]
    st0 = stop_s[i] + 1.0 if s0 is None else s0   # right at the stop: a junction just after it stays usable       # s0: a junction probe further along the original route
    free = s0 is None and dv_free_start(ctx, i)
    # free start: from the stop itself (inside the interchange), no forced heading - the routing engine picks the exit
    a_ = (ctx["stops"][i]["lat"], ctx["stops"][i]["lon"]) if free else at(st0)
    b_s = max(stop_s[j] - 5.0, st0 + 10.0)
    b_ = at(b_s)
    h0, h1 = (None if free else ctx["heading"](a_, st0)), ctx["heading"](b_, b_s)
    reqs = [([a_, b_], P["alt_per_pair"], True, "router")]          # source codes: router | busvia | manual | adopt | ddchain
    brs = {}
    ch = (ctx.get("chains") or {}).get((i, j)) if s0 is None else None
    if ch:          # V16.23: stop to stop along services that already run double-deckers (diversion.stop_chains)
        coords_ = {c_: (ctx["st"]["stops"][c_]["lat"], ctx["st"]["stops"][c_]["lon"]) for c_ in ch["stops"] if c_ in ctx["st"]["stops"]}
        vv = diversion.chain_vias(ch, coords_)
        if vv:
            reqs.append(([a_] + [v[0] for v in vv] + [b_], 0, True, "ddchain"))
            brs[len(reqs) - 1] = [h0] + [v[1] for v in vv] + [h1]
    if not ctx["avoid"]:
        mid = ((a_[0] + b_[0]) / 2, (a_[1] + b_[1]) / 2)
        for v in sorted(ctx["bvias"], key=lambda v: diversion.dist_m(mid, v))[:3]:
            reqs.append(([a_, tuple(v), b_], 0, False, "busvia"))
    if ctx["manual"]:
        reqs.append(([a_] + [tuple(v) for v in ctx["manual"]] + [b_], 0, True, "manual" ))
    for dn in ctx["donors"][:2]:
        for vv in (dn["vias"], list(reversed(dn["vias"]))):
            if len(vv) >= 2:
                reqs.append(([a_] + [tuple(v) for v in vv] + [b_], 0, True, "manual" if dn["service"] == "OCC" else "adopt"))
    res = await asyncio.gather(*[dv_osrm(c, alt, brs.get(k_) or ([h0] + [None] * (len(c) - 2) + [h1]), ctx["avoid"] if av else None) for k_, (c, alt, av, _) in enumerate(reqs)])
    out, err = [], None
    for x, (_, _, _, src) in zip(res, reqs):
        err = err or x.get("error")
        for r in x.get("routes") or []:
            out.append(dict(r, _src=src))
    return out, err


async def validate_next_original_leg(ctx, j):
    """MANDATORY: after rejoining at stop D the bus carries on along its OWN route to its next stop E, and that stretch must
    be clear of the blockage (in the blocked direction). -> {ok, next_code, next_name, detail}, cached per stop.
    V16.26h: checked on the bus's own route line (checked against LTA's stop distances), no longer by asking the routing
    engine to drive D -> E again. The bus runs D -> E every day, but a re-routed D -> E could fail on the router's own map:
    TomTom takes the bus's heading at the start only, so on a dual carriageway the end point could land on the other side
    and the route turned round to reach it ("next leg requires a U-turn"); or its map refused a 4.4 m / 18 t bus on a road
    the bus uses daily. Which way the bus arrives at D is checked separately (validate_rejoin_direction)."""
    key = (ctx.get("bkey"), ctx["svc"], ctx["d"], j)
    if len(ctx["next_cache"]) > 3000:
        ctx["next_cache"].clear()
    if key in ctx["next_cache"]:
        return ctx["next_cache"][key]
    stops, stop_s, P = ctx["stops"], ctx["stop_s"], ctx["P"]
    if j + 1 >= len(stops):
        res = {"ok": True, "next_code": "", "next_name": "end of route", "detail": "rejoin stop is the last stop of the route"}
        ctx["next_cache"][key] = res
        return res
    nc, nn = stops[j + 1]["code"], stops[j + 1]["name"]
    m = stop_s[j + 1] - stop_s[j]
    own = diversion.cut(ctx["line"], ctx["cum"], stop_s[j], stop_s[j + 1])
    res = {"ok": True, "next_code": nc, "next_name": nn, "detail": f"{m:.0f} m on its own route to {nc}, clear of the blockage"}
    if len(own) >= 2:
        ok, why = diversion.validate_block_avoidance(own, ctx["block_lines"], None, P)
        if not ok:
            res = {"ok": False, "next_code": nc, "next_name": nn, "detail": f"its own route on to {nc} {why}"}
    ctx["next_cache"][key] = res
    return res


def dv_run_vias(pts, n_max=6, turn_deg=35.0, min_run=80.0):
    """V16.26b: waypoints in the MIDDLE of each straight stretch of a route (never at a junction), so the routing engine cannot
    reach one from a side road or the other carriageway and add turns. None when the line has no stretch that long."""
    if len(pts) < 2:
        return None
    d = diversion.densify(pts, 20.0)
    if len(d) < 3:
        return None
    brg = [diversion.bearing(d[k], d[k + 1]) for k in range(len(d) - 1)]
    cuts = [0] + [k for k in range(1, len(brg)) if diversion.angdiff(brg[k - 1], brg[k]) > turn_deg] + [len(d) - 1]
    runs = []
    for a, b in zip(cuts, cuts[1:]):
        seg = d[a:b + 1]
        if len(seg) < 2:
            continue
        lm = diversion.line_m(seg)
        if lm >= min_run:
            runs.append((lm, diversion.point_at(seg, diversion.cum_m(seg), lm / 2.0), a))
    if not runs:
        return None
    return [x[1] for x in sorted(sorted(runs, key=lambda x: -x[0])[:n_max], key=lambda x: x[2])]


def dv_road_at(steps, pt, tol=35.0):
    """V16.26: the named road of the routing step passing closest to pt (within tol m), '' when none"""
    best, bd = "", tol
    for st_ in steps or []:
        nm = (st_.get("road") or "").strip()
        if not nm:
            continue
        for q in (st_.get("line") or [])[::2]:
            d_ = diversion.dist_m(pt, q)
            if d_ < bd:
                best, bd = nm, d_
    return best


async def dv_validate_route(ctx, r, i, j, s0=None):
    """HARD validation of one engine route for the pair (i, j), in order. -> (candidate | None, reject reason)"""
    P, line, cum, stop_s, A, B = ctx["P"], ctx["line"], ctx["cum"], ctx["stop_s"], ctx["A"], ctx["B"]
    if len(r.get("line") or []) < 2:
        return None, "No verified road connection found"
    st0 = stop_s[i] + 1.0 if s0 is None else s0   # right at the stop: a junction just after it stays usable
    free = s0 is None and dv_free_start(ctx, i)
    a_ = (ctx["stops"][i]["lat"], ctx["stops"][i]["lon"]) if free else ctx["at"](st0)
    b_ = ctx["at"](stop_s[j] - 5.0)
    if diversion.dist_m(r["line"][0], a_) > P["ends_tol_m"] or diversion.dist_m(r["line"][-1], b_) > P["ends_tol_m"]:
        return None, "route cannot physically reach the stops (road network not connected there)"
    ok, why = diversion.validate_block_avoidance(r["line"], ctx["block_lines"], None, P)
    if not ok:
        return None, why
    ok, why = await asyncio.to_thread(diversion.validate_no_uturn, r["line"], r.get("steps"), line, cum, st0 - 5.0, P, 150.0 if free else 0.0,
                                      (B, stop_s[j] + 30.0))
    if not ok:
        return None, why
    dep = await asyncio.to_thread(diversion.departure, r["line"], line, cum, None, (st0 - 45.0, st0 + 60.0), free, stop_s[i])
    ok, why = diversion.validate_rejoin_direction(dep, stop_s[j], B, P)
    if not ok:
        return None, why
    if dep["leave_s"] > A + P.get("block_end_margin_m", 10.0):          # leaving AT the start junction is fine
        return None, "only leaves the route inside the blockage"
    seg = dep["pts"][dep["i0"]:dep["i1"] + 1]
    div_m = diversion.line_m(seg)
    ok, why = diversion.validate_detour(div_m, dep["rejoin_s"] - dep["leave_s"], P)
    if not ok:
        return None, why
    groups_all = offservice.group_roads(r["steps"])
    keep = []
    for g in groups_all:
        gl = g["line"] or []
        if gl and any(diversion.nearest_on_line(p, line, cum)[0] > P["on_route_m"] for p in gl[::max(1, len(gl) // 8)]):
            keep.append(g)
    lead = next((g for g in groups_all if g["line"] and diversion.nearest_on_line(dep["leave_pt"], g["line"])[0] < 30), None)
    if lead is not None and (not keep or keep[0] is not lead):
        keep.insert(0, lead)
    # road suitability: LTA road category, OpenStreetMap class + access tags
    hw = await os_highways(seg)
    ways = hw.get("ways") if hw.get("ok") else None
    cl = await asyncio.to_thread(dv_classes, seg, ctx["idx"], ways)
    flags = await asyncio.to_thread(dv_osm_flags, seg, ways) if ways else []
    suit = diversion.validate_road_suitability(cl, flags, P)
    if not suit["ok"]:
        return None, suit["text"]
    if ways:                                                              # V16.24: a real height limit is a hard stop
        need = dv2.PARAMS["dd_height_m"] if ctx["bus"] == "dd" else dv2.PARAMS["sd_height_m"]
        low = await asyncio.to_thread(dv_low_clearance, seg, ways, need)
        if low:
            return None, f"Height limit {low[0][1]:.1f} m on {low[0][0]} \u2014 too low for a {'double' if ctx['bus'] == 'dd' else 'single'}-deck bus"
    # bus-road evidence: services in the same direction (level 1), bus stops (level 2), road class (level 3)
    box = diversion.bbox(seg, 300)
    net = await asyncio.to_thread(dv_busnet, st_ctx(ctx), box)
    # the stops the bus REALLY serves: last one before it leaves its route, first one after it rejoins (same rule as the
    # skipped-stop list) - the requested pair (i, j) is only where the routing engine was asked to go
    li = max([k for k, x in enumerate(stop_s) if x <= dep["leave_s"] + 1.0], default=i)
    rj = min([k for k, x in enumerate(stop_s) if x >= dep["rejoin_s"] - 1.0], default=j)
    c = {"r": r, "dep": dep, "seg": seg, "groups": keep, "signature": diversion.road_signature(keep), "div_m": div_m,
         "src": r.get("_src", "router"), "pair": (li, rj), "asked": (i, j), "road_class": diversion.road_mix(cl), "free": free}
    br = await asyncio.to_thread(dv_bus_road, c, net, ctx["bus"], bt_table())
    if ctx["bus"] == "dd" and br.get("dd") == "UNCONFIRMED":
        unk = [sv for sv in br.get("services", []) if bt_class(sv) is None]
        if unk and await dv_probe_types(st_ctx(ctx), unk, box):
            br = await asyncio.to_thread(dv_bus_road, c, net, ctx["bus"], bt_table())
    stops_along = await asyncio.to_thread(dv_stops_along, st_ctx(ctx), seg)
    ev = diversion.validate_bus_road_evidence(br, stops_along, div_m / 1000.0, ctx["bus"], allow_sd=ctx.get("dd_sd_ok", False))
    if not ev["ok"]:
        return None, ev["text"]
    c.update(bus_road=br, evidence=ev, suit=suit)
    return c, ""


def st_ctx(ctx):
    return ctx["st"]


def dv_low_clearance(seg, ways, need_m):
    """V16.24: OpenStreetMap height limits (maxheight) on the roads the route really drives along (same heading), lower than the
    bus needs -> [(road name, limit m)]"""
    out = {}
    if not ways or len(seg) < 2:
        return []
    low = [(w, offservice.parse_len_m((w.get("acc") or {}).get("maxheight"))) for w in ways]
    low = [(w, h) for w, h in low if h is not None and h < need_m and len(w.get("geom") or []) >= 2]
    if not low:
        return []
    pts = diversion.densify(seg, 20.0)
    for k in range(len(pts) - 1):
        brg = diversion.bearing(pts[k], pts[k + 1])
        for w, h in low:
            d, _, i_, wb = diversion.nearest_on_line(pts[k], w["geom"])
            a = diversion.angdiff(brg, wb)
            if d <= 12.0 and (a <= 30.0 or a >= 150.0):
                nm = w.get("name") or "an unnamed road"
                out[nm] = min(h, out.get(nm, 99.0))
    return sorted(out.items(), key=lambda x: x[1])


def dv_dd_chains(st, svc, d, stops, ia, down, b0, block_lines, bus, upstream=4):
    """V16.23 double-deck stop chain: from the last reachable stop (and up to `upstream` stops before it), move stop to stop
    along services that already run double-deckers (LTA BusRoutes order, bus type learned from LTA Bus Arrival / OCC entries),
    changing service only at a shared stop, until a stop on the own route after the blockage. -> ({(i, j): chain}, info)"""
    P = diversion.PARAMS
    tab = bt_table()
    coords = {c: (x["lat"], x["lon"]) for c, x in st["stops"].items()}
    bl0 = block_lines[0][0] if block_lines else None
    if not bl0:
        return {}, {"n": 0, "text": "no blockage"}
    centre = bl0[len(bl0) // 2]
    near = {c for c, q in coords.items() if diversion.dist_m(q, centre) <= P["chain_radius_m"]}
    routes = {}
    for key, rr in st["routes"].items():
        if any(r["code"] in near for r in rr):
            routes[key] = [(r["code"], r.get("dist")) for r in rr if r["code"] in coords]
    starts_i = [i for i in range(ia, max(-1, ia - upstream - 1), -1)]
    targets = {stops[j]["code"]: j - b0 for j in down}
    res = diversion.stop_chains(routes, coords, lambda s_: bt_class(s_, tab), (svc, d), [stops[i]["code"] for i in starts_i], targets, block_lines, bus, centre, P)
    out = {}
    for i in starts_i:
        for tcode, ch in (res.get(stops[i]["code"]) or {}).items():
            j = next((jj for jj in down if stops[jj]["code"] == tcode), None)
            if j is None or all(lg["own"] for lg in ch["legs"]):
                continue        # only the own route: nothing new
            out[(i, j)] = ch
    svcs = {k[0] for k in routes}
    n_dd = sum(1 for x in svcs if bt_class(x, tab) == "DD")
    n_unk = sum(1 for x in svcs if bt_class(x, tab) is None)
    best = min(out.values(), key=lambda c: c["cost"]) if out else None
    info = {"n": len(out), "services_near": len(svcs), "dd_services": n_dd, "unknown_services": n_unk, "radius_km": round(P["chain_radius_m"] / 1000.0, 1),
            "best": dv_chain_out(best, st) if best else None,
            "text": (f"{len(out)} stop-to-stop chain(s) on {'double-deck' if bus == 'dd' else 'bus'} services found; shortest {dv_chain_out(best, st)['text']}" if best else
                     f"No stop-to-stop chain on {'double-deck' if bus == 'dd' else 'bus'} services links a stop before the blockage to one after it within {P['chain_radius_m'] / 1000.0:g} km "
                     f"({n_dd} of {len(svcs)} services nearby are known to run double-deckers, {n_unk} not known yet)")}
    return out, info


def dv_chain_out(ch, st):
    """a chain for the page: the stops with names, one leg per service, and a one-line text"""
    if not ch:
        return None
    nm = lambda c: (st["stops"].get(c) or {}).get("name", "")
    stops_ = [{"code": c, "name": nm(c), "lat": st["stops"][c]["lat"], "lon": st["stops"][c]["lon"]} for c in ch["stops"] if c in st["stops"]]
    legs = [{"svc": lg["svc"], "own": lg["own"], "from": lg["from"], "from_name": nm(lg["from"]), "to": lg["to"], "to_name": nm(lg["to"]),
             "stops": len(lg["stops"]) - 1, "codes": list(lg["stops"]), "km": round(lg["m"] / 1000.0, 2)} for lg in ch["legs"]]
    text = " → ".join([ch["stops"][0]] + [f"({lg['svc']}{' own route' if lg['own'] else ''}) {lg['to']}" for lg in legs]) + f", {ch['m'] / 1000.0:.1f} km"
    return {"stops": stops_, "legs": legs, "km": round(ch["m"] / 1000.0, 2), "transfers": ch["transfers"], "unconfirmed": ch["unconfirmed"], "text": text}


async def dv_stop_search(ctx):
    """The OCC controller's search: last reachable stop A; try A->B, A->C, A->D... (fewest stops skipped first);
    stop at the first rejoin stop that gives a HIGH or MEDIUM valid diversion (keeping up to 3 alternatives there);
    only if nothing works from A, try earlier diversion points. -> dict"""
    P, stops, stop_s, A, B = ctx["P"], ctx["stops"], ctx["stop_s"], ctx["A"], ctx["B"]
    ia = diversion.find_last_reachable_stop(stop_s, A, None, P)
    downstream = diversion.get_downstream_rejoin_candidates(stop_s, B, None, None, P)
    out = {"cands": [], "attempts": [], "rejects": [], "routes": 0, "error": None, "ia": ia, "downstream": downstream,
           "rejected": {"uses_block": 0, "no_bypass": 0, "duplicate": 0, "uturn": 0}}
    if ia is None or not downstream:
        out["summary"] = ("No original stop before the blockage that the bus can still serve." if ia is None
                          else "No original stop after the blockage to rejoin at (the blockage is at the end of the route).")
        return out
    found_good, low_keep = False, []
    exits = [ia - k for k in range(0, P["max_exit_fallback"] + 1) if ia - k >= 0]
    for ei, i in enumerate(exits):
        for j in downstream:
            routes, err = await route_between_stops(ctx, i, j)
            out["routes"] += len(routes)
            out["error"] = out["error"] or err
            valid, reasons = [], []
            for r in routes:
                c, why = await dv_validate_route(ctx, r, i, j)
                if c is None:
                    reasons.append(why)
                    if "U-turn" in why:
                        out["rejected"]["uturn"] += 1
                    elif "exclusion" in why:
                        out["rejected"]["uses_block"] += 1
                    elif "rejoin" in why or "leaves" in why:
                        out["rejected"]["no_bypass"] += 1
                    if len(out["rejects"]) < 40:
                        names = []
                        for st_ in r.get("steps") or []:
                            nm = (st_.get("road") or "").strip()
                            if nm and (not names or names[-1] != nm):
                                names.append(nm)
                        out["rejects"].append({"why": why, "detail": f"{stops[i]['code']} \u2192 {stops[j]['code']}", "roads": names[:8],
                                               "km": round(r.get("km") or 0, 2), "src": r.get("_src", "router"), "router": r.get("router", ""),
                                               "line": [[round(p_[0], 6), round(p_[1], 6)] for p_ in diversion.simplify(r["line"], 60)]})
                    continue
                if any(v["signature"] == c["signature"] and abs(v["div_m"] - c["div_m"]) < 150 for v in valid):
                    out["rejected"]["duplicate"] += 1
                    continue
                valid.append(c)
            kept = []
            for c in valid:         # MANDATORY: the bus can continue from its real rejoin stop to the next original stop
                nxt = await validate_next_original_leg(ctx, c["pair"][1])
                if not nxt["ok"]:
                    reasons.append(f"rejoin stop {stops[c['pair'][1]]['code']} fails the next-leg check (from there on to its next stop {nxt['next_code'] or 'on the route'}): {nxt['detail']}")
                    continue
                c["next_leg"] = nxt
                c["earlier"] = c["pair"][0] < ia
                c["confidence"] = diversion.confidence_level(c["evidence"], c["suit"], True, ctx["bus"])
                kept.append(c)
            valid = kept
            best_conf = min([diversion.CONF_RANK[c["confidence"]] for c in valid], default=None)
            if valid:
                verdict = "valid"
                reason = f"{len(valid)} valid route(s), best {['HIGH', 'MEDIUM', 'LOW'][best_conf]} confidence"
                real = sorted({(c["pair"][0], c["pair"][1]) for c in valid})
                if real and real[0] != (i, j):
                    reason += f" \u2014 the road actually leaves after {stops[real[0][0]]['code']} and rejoins at {stops[real[0][1]]['code']}"
            elif not routes:
                verdict, reason = "fail", "No verified road connection found"
            else:
                top = max(set(reasons), key=reasons.count) if reasons else "No verified road connection found"
                verdict, reason = "fail", top
            out["attempts"].append({"from_code": stops[i]["code"], "from_name": stops[i]["name"], "to_code": stops[j]["code"], "to_name": stops[j]["name"],
                                    "skips": j - i - 1, "routes": len(routes), "result": verdict, "reason": reason, "earlier": ei > 0})
            if valid and best_conf is not None and best_conf <= 1:
                out["cands"] += sorted(valid, key=lambda c: diversion.CONF_RANK[c["confidence"]])[:P["alt_per_pair"]]
                found_good = True
                break
            if valid and not low_keep:
                low_keep = valid[:1]            # routable but weak evidence: shown for review, never recommended
        if found_good:
            break
    out["cands"] = out["cands"] + [c for c in low_keep if c not in out["cands"]]
    good = [a for a in out["attempts"] if a["result"] == "valid"]
    if good:
        g = good[-1] if found_good else good[0]
        out["summary"] = f"{g['from_name']} \u2192 {g['to_name']} after {len(out['attempts'])} attempt(s)"
    else:
        out["summary"] = f"No valid diversion: {len(out['attempts'])} stop pair(s) tried, {out['routes']} road route(s) checked."
    return out


# =========================================================================== V16.16 Diversion Planner v2 (rebuilt page)
# Search -> block a road segment -> blocked direction -> affected services -> each service analysed -> easiest practical
# diversion. Engine: upstream (where to leave) x downstream (where to rejoin), progressive from the blockage outwards;
# routing engine geometry only; hard validation first; OCC score second (stops preserved and simplicity before distance).
import dv2
from urllib.parse import quote

TT_MAP = {"cache": {}, "day": "", "n": 0, "cap": int(os.getenv("TOMTOM_MAP_DAILY_CAP", "20000")), "error": None}


@app.get("/api/tomtom/map/{z}/{x}/{y}.png")
async def api_tomtom_map(z: int, x: int, y: int):
    """TomTom Maps basemap tiles (night style), proxied so the key stays on the server. Separate daily cap."""
    from fastapi.responses import Response
    blank = Response(_BLANK_PNG, media_type="image/png", headers={"Cache-Control": "public, max-age=60"})
    if not TT["key"] or z < 3 or z > 20 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return blank
    now = time.time()
    hit = TT_MAP["cache"].get((z, x, y))
    if hit and now - hit[0] < 86400 * 3:
        return Response(hit[1], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    day = now_sgt().strftime("%Y-%m-%d")
    if TT_MAP["day"] != day:
        TT_MAP["day"], TT_MAP["n"] = day, 0
    if TT_MAP["n"] >= TT_MAP["cap"]:
        TT_MAP["error"] = "daily map tile cap reached"
        return blank
    TT_MAP["n"] += 1
    try:
        r = await client().get(f"https://api.tomtom.com/map/1/tile/basic/night/{z}/{x}/{y}.png", timeout=15,
                               headers=tt_headers(), params={"key": TT["key"], "tileSize": 256})
        r.raise_for_status()
        if len(TT_MAP["cache"]) > 4000:
            TT_MAP["cache"].clear()
        TT_MAP["cache"][(z, x, y)] = (now, r.content)
        return Response(r.content, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        TT_MAP["error"] = tt_err(e)
        return blank


def dv2_db():
    dv_init()
    bb_sql("CREATE TABLE IF NOT EXISTS dv2_settings(key TEXT PRIMARY KEY, value TEXT, by_name TEXT, ts REAL)")


def dv2_weights():
    try:
        dv2_db()
        r = bb_sql("SELECT value FROM dv2_settings WHERE key='weights_v3'", fetch=True)
        if r:
            w = json.loads(r[0]["value"])
            return {k: float(w.get(k, v)) for k, v in dv2.DEFAULT_WEIGHTS.items()}
    except Exception:
        pass
    return dict(dv2.DEFAULT_WEIGHTS)


def dv2_limits():
    try:
        dv2_db()
        r = bb_sql("SELECT value FROM dv2_settings WHERE key='safeguards_v3'", fetch=True)
        if r:
            w = json.loads(r[0]["value"])
            return {k: float(w.get(k, v)) for k, v in dv2.DEFAULT_LIMITS.items()}
    except Exception:
        pass
    return dict(dv2.DEFAULT_LIMITS)


@app.get("/api/dv2/limits")
async def api_dv2_limits_get():
    return {"limits": dv2_limits(), "labels": dv2.LIMIT_LABELS, "defaults": dv2.DEFAULT_LIMITS}


@app.post("/api/dv2/limits")
async def api_dv2_limits_set(request: Request):
    body = _dv_body(await request.body())
    w = {}
    for k, v in dv2.DEFAULT_LIMITS.items():
        lo, hi = dv2.LIMIT_LABELS[k][2], dv2.LIMIT_LABELS[k][3]
        try:
            w[k] = max(lo, min(hi, float((body.get("limits") or {}).get(k, v))))
        except (TypeError, ValueError):
            w[k] = v
    if body.get("reset"):
        w = dict(dv2.DEFAULT_LIMITS)
    dv2_db()
    bb_sql("INSERT OR REPLACE INTO dv2_settings(key, value, by_name, ts) VALUES('safeguards_v3', ?, ?, ?)", (json.dumps(w), _who(request), time.time()))
    for k in [k for k in CACHE if str(k).startswith("dv2plan:")]:
        CACHE.pop(k, None)
    return {"ok": True, "limits": w}


@app.get("/api/dv2/config")
async def api_dv2_config():
    return {"tomtom_map": bool(TT["key"]), "router": "TomTom Routing (bus mode)" if dv_use_tomtom() else "OSRM (OpenStreetMap)",
            "tomtom_routing_left": tt_route_left() if dv_use_tomtom() else None, "tomtom_routing_cap": TT_RT["cap"] if dv_use_tomtom() else None,
            "weights": dv2_weights(), "weight_labels": dv2.WEIGHT_LABELS, "default_weights": dv2.DEFAULT_WEIGHTS,
            "limits": dv2_limits(), "limit_labels": dv2.LIMIT_LABELS, "default_limits": dv2.DEFAULT_LIMITS,
            "buffer_m": diversion.PARAMS["block_buffer_m"], "map_error": TT_MAP["error"]}


@app.get("/api/dv2/weights")
async def api_dv2_weights_get():
    return {"weights": dv2_weights(), "labels": dv2.WEIGHT_LABELS, "defaults": dv2.DEFAULT_WEIGHTS}


@app.post("/api/dv2/weights")
async def api_dv2_weights_set(request: Request):
    body = _dv_body(await request.body())
    w = {k: max(0.0, min(100.0, float((body.get("weights") or {}).get(k, v)))) for k, v in dv2.DEFAULT_WEIGHTS.items()}
    if body.get("reset"):
        w = dict(dv2.DEFAULT_WEIGHTS)
    dv2_db()
    bb_sql("INSERT OR REPLACE INTO dv2_settings(key, value, by_name, ts) VALUES('weights_v3', ?, ?, ?)", (json.dumps(w), _who(request), time.time()))
    for k in [k for k in CACHE if str(k).startswith("dv2plan:")]:
        CACHE.pop(k, None)
    return {"ok": True, "weights": w}


# ---------- search: bus stops, roads (LTA road links), places (TomTom / OneMap)
DV2_ROADS = {"key": None, "names": {}}


def dv2_road_index(idx):
    if not idx:
        return {}
    if DV2_ROADS["key"] == id(idx):
        return DV2_ROADS["names"]
    names = {}
    for sg in idx.segs:
        nm = (sg[5] or "").strip()
        if not nm:
            continue
        e = names.setdefault(nm, [90.0, 180.0, -90.0, -180.0, 0])
        e[0], e[1] = min(e[0], sg[0], sg[2]), min(e[1], sg[1], sg[3])
        e[2], e[3] = max(e[2], sg[0], sg[2]), max(e[3], sg[1], sg[3])
        e[4] += 1
    DV2_ROADS.update(key=id(idx), names=names)
    return names


def dv2_title(s):
    s = str(s or "")
    return s.title().replace("Ave ", "Ave ").replace(" Rd", " Rd") if s.isupper() else s


async def dv2_places(q):
    """places / buildings: TomTom Search when the key is set, else OneMap (public). Cached 1 day."""
    key = "dv2place:" + hashlib.sha1(q.lower().encode()).hexdigest()[:16]

    async def factory():
        out = []
        try:
            if TT["key"] and tt_spend():
                r = await client().get(f"https://api.tomtom.com/search/2/search/{quote(q)}.json", headers=tt_headers(), timeout=12,
                                       params={"key": TT["key"], "countrySet": "SG", "limit": 6, "lat": 1.3521, "lon": 103.8198})
                r.raise_for_status()
                for x in r.json().get("results", []):
                    pos = x.get("position") or {}
                    nm = (x.get("poi") or {}).get("name") or (x.get("address") or {}).get("freeformAddress") or q
                    out.append({"type": "place", "label": nm, "sub": (x.get("address") or {}).get("freeformAddress", ""),
                                "lat": pos.get("lat"), "lon": pos.get("lon"), "src": "TomTom"})
            else:
                r = await client().get("https://www.onemap.gov.sg/api/common/elastic/search", timeout=12,
                                       params={"searchVal": q, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1})
                r.raise_for_status()
                for x in (r.json().get("results") or [])[:6]:
                    out.append({"type": "place", "label": dv2_title(x.get("SEARCHVAL")), "sub": dv2_title(x.get("ADDRESS")),
                                "lat": float(x.get("LATITUDE")), "lon": float(x.get("LONGITUDE")), "src": "OneMap"})
        except Exception:
            return [], 120, False
        return [p for p in out if p.get("lat") and in_sg(p["lat"], p["lon"])], 86400, True
    return await cached(key, factory)


_DV2_ABBR = {"ave": "avenue", "av": "avenue", "rd": "road", "st": "street", "dr": "drive", "cres": "crescent", "ctrl": "central",
             "nth": "north", "sth": "south", "pl": "place", "cl": "close", "lor": "lorong", "jln": "jalan", "bt": "bukit",
             "upp": "upper", "expy": "expressway", "hway": "highway", "hwy": "highway", "pk": "park", "c'wealth": "commonwealth",
             "cwealth": "commonwealth", "terr": "terrace", "gdns": "gardens", "int": "interchange"}


def dv2_norm(t):
    """road-name text for matching: lower case, abbreviations expanded ("Clementi Ave 3" == "CLEMENTI AVENUE 3")"""
    words = re.sub(r"[^a-z0-9' ]", " ", str(t).lower()).split()
    return " ".join(_DV2_ABBR.get(w, w) for w in words)


def dv2_match_roads(names, q, n=6):
    qn = dv2_norm(q)
    hit = [k for k in names if qn in dv2_norm(k)]
    return sorted(hit, key=lambda k: (not dv2_norm(k).startswith(qn), dv2_norm(k) != qn, -names[k][4]))[:n]


def dv2_junction(idx, a, b):
    """where two roads meet: the closest pair of points between their LTA road links (<= 60 m) -> (lat, lon) | None"""
    sa = [s_ for s_ in (idx.segs if idx else []) if s_[5] == a]
    sb = [s_ for s_ in (idx.segs if idx else []) if s_[5] == b]
    best, bd = None, 60.0
    pb = [(s_[0], s_[1]) for s_ in sb] + [(s_[2], s_[3]) for s_ in sb]
    for s_ in sa:
        for pa in ((s_[0], s_[1]), (s_[2], s_[3])):
            for q_ in pb:
                d = diversion.dist_m(pa, q_)
                if d < bd:
                    best, bd = ((pa[0] + q_[0]) / 2, (pa[1] + q_[1]) / 2), d
    return best


@app.get("/api/dv2/search")
async def api_dv2_search(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    st = await static()
    ql = q.lower()
    out = []
    if re.fullmatch(r"\d{5}", q) and q in st["stops"]:
        x = st["stops"][q]
        out.append({"type": "stop", "label": f"{x['code']} {x['name']}", "sub": x.get("road", ""), "lat": x["lat"], "lon": x["lon"], "code": q})
    bands = await bands_state()
    names = dv2_road_index(bands.get("idx"))
    parts = [x for x in re.split(r"\s*(?:/|&|\band\b|\bx\b)\s*", q, flags=re.I) if len(x.strip()) >= 2]
    if len(parts) == 2:       # a junction: "Clementi Ave 3 / Commonwealth Ave West"
        ra, rb = dv2_match_roads(names, parts[0], 1), dv2_match_roads(names, parts[1], 1)
        if ra and rb and ra[0] != rb[0]:
            jp = dv2_junction(bands.get("idx"), ra[0], rb[0])
            ea, eb = names[ra[0]], names[rb[0]]
            out.append({"type": "junction", "label": f"{dv2_title(ra[0])} / {dv2_title(rb[0])}", "roads": [ra[0], rb[0]],
                        "sub": "Junction" if jp else "Both roads (they do not meet)", "lat": jp[0] if jp else (ea[0] + eb[2]) / 2,
                        "lon": jp[1] if jp else (ea[1] + eb[3]) / 2, "found": bool(jp),
                        "bbox": [min(ea[0], eb[0]), min(ea[1], eb[1]), max(ea[2], eb[2]), max(ea[3], eb[3])]})
    rn = dv2_match_roads(names, q, 6) if len(parts) != 2 else []
    for n in rn:
        e = names[n]
        out.append({"type": "road", "label": dv2_title(n), "road": n, "sub": "Road", "lat": (e[0] + e[2]) / 2, "lon": (e[1] + e[3]) / 2,
                    "bbox": [e[0], e[1], e[2], e[3]]})
    qn = dv2_norm(q)
    sm = [x for x in st["stops"].values() if ql in x["name"].lower() or qn in dv2_norm(x.get("road") or "") or qn in dv2_norm(x["name"])] if len(parts) != 2 else []
    sm.sort(key=lambda x: (not x["name"].lower().startswith(ql), x["name"]))
    for x in sm[:6]:
        if not any(o.get("code") == x["code"] for o in out):
            out.append({"type": "stop", "label": f"{x['code']} {x['name']}", "sub": x.get("road", ""), "lat": x["lat"], "lon": x["lon"], "code": x["code"]})
    if len(out) < 8:
        out += (await dv2_places(q))[:8 - len(out)]
    return {"results": out[:14]}


@app.get("/api/dv2/roadgeom")
async def api_dv2_roadgeom(name: str = ""):
    """every LTA road link with this name (to highlight the searched road)"""
    bands = await bands_state()
    idx = bands.get("idx")
    if not idx or not name:
        return {"segments": []}
    segs = [[round(s[0], 6), round(s[1], 6), round(s[2], 6), round(s[3], 6)] for s in idx.segs if (s[5] or "") == name][:1500]
    return {"name": dv2_title(name), "segments": segs}


def dv2_names_near(idx, p, radius_m, exclude):
    """road names of LTA links within radius of p (other than `exclude`) -> {name: count}"""
    if not idx:
        return {}
    cx, cy = int(p[0] / GRID), int(p[1] / GRID)
    out = {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for i in idx.grid.get((cx + dx, cy + dy), ()):
                s = idx.segs[i]
                nm = (s[5] or "").strip()
                if not nm or nm == exclude:
                    continue
                if diversion.nearest_on_line(p, [(s[0], s[1]), (s[2], s[3])])[0] <= radius_m:
                    out[nm] = out.get(nm, 0) + 1
    return out


def dv2_towards(idx, cor, cm, s_from, step, road):
    """name of the first cross road found travelling along the corridor from s_from (step > 0 forwards, < 0 backwards)"""
    s_ = s_from
    L = cm[-1]
    for _ in range(40):
        s_ += step
        if s_ < 0 or s_ > L:
            break
        nm = dv2_names_near(idx, diversion.point_at(cor, cm, s_), 22.0, road)
        if nm:
            return dv2_title(max(nm.items(), key=lambda kv: kv[1])[0])
    return None


COMPASS = ["Northbound", "North-eastbound", "Eastbound", "South-eastbound", "Southbound", "South-westbound", "Westbound", "North-westbound"]


@app.get("/api/dv2/block")
async def api_dv2_block(lat: float = 0.0, lon: float = 0.0):
    """snap a tap to the road and describe both travel directions ("Towards <next cross road>")"""
    j = await api_dv_snap(lat=lat, lon=lon)
    if not j.get("ok"):
        return j
    bands = await bands_state()
    idx = bands.get("idx")
    cor, cm = j["corridor"], j["corridor_m"]
    road = j["road"]
    raw = next((s[5] for s in (idx.segs if idx else []) if dv2_title(s[5]) == dv2_title(road)), road) if idx else road
    fwd = dv2_towards(idx, cor, cm, j["end_m"], 25.0, raw)
    rev = dv2_towards(idx, cor, cm, j["start_m"], -25.0, raw)
    b = diversion.bearing(diversion.point_at(cor, cm, j["start_m"]), diversion.point_at(cor, cm, j["end_m"]))
    cf, cr = COMPASS[int(((b + 22.5) % 360) // 45)], COMPASS[int(((b + 180 + 22.5) % 360) // 45)]
    j["dir_labels"] = {"fwd": cf + (" \u00b7 towards " + fwd if fwd else ""), "rev": cr + (" \u00b7 towards " + rev if rev else ""),
                       "fwd_bearing": round(b), "rev_bearing": round((b + 180) % 360)}
    j["road"] = dv2_title(road)
    return j


def dv2_blocks(body):
    """the blocked sections as engine blocks. Either one section {line, dir, road} or several: {blocks: [{line, dir, road}, ...]}.
    dir: fwd (drawn order) / rev / both. A section with an unknown direction is refused rather than guessed."""
    items = body.get("blocks") if isinstance(body.get("blocks"), list) else [body]
    out = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        line = [tuple(p) for p in (it.get("line") or []) if isinstance(p, (list, tuple)) and len(p) == 2]
        if len(line) < 2:
            continue
        dirn = {"fwd": "fwd", "forward": "fwd", "rev": "rev", "back": "rev", "backward": "rev", "reverse": "rev", "both": "both"}.get(
            str(it.get("dir") or "both").strip().lower())
        if dirn is None:
            continue
        if dirn == "rev":
            line = list(reversed(line))
        out.append({"line": line, "directed": dirn != "both", "road": str(it.get("road") or "")[:80]})
    return out


@app.post("/api/dv2/affected")
async def api_dv2_affected(request: Request):
    """every service-direction whose actual route runs ALONG the blocked section in a blocked direction"""
    body = _dv_body(await request.body())
    blocks = dv2_blocks(body)
    if not blocks:
        return JSONResponse({"error": "Place the blockage first."}, status_code=400)
    st = await static()
    tr_lines_kick(st)
    entries, basis = await dv_entries(st, blocks)
    opmap = await occ_operator_map()
    out = []
    for e in entries:
        stops = route_stops(st, e["service"], e["direction"])
        out.append({"service": e["service"], "direction": e["direction"], "run": e.get("run", 0), "operator": opmap.get(e["service"], ""),
                    "destination": stops[-1]["name"] if stops else "", "bus": "DD" if bt_class(e["service"]) in ("DD", None) else "SD"})
    out.sort(key=lambda x: (len(x["service"]), x["service"], x["direction"]))
    return {"ok": True, "services": out, "basis": basis, "dir": body.get("dir") or "both"}


async def dv2_plan(st, blocks, svc, d, run_i, traffic=True, donors=None):
    P, Q = diversion.PARAMS, dv2.PARAMS
    mids = [b["line"][len(b["line"]) // 2] for b in blocks]
    line, src = await dv_line(st, svc, d, focus=mids)
    if not line:
        return {"ok": False, "error": f"No route line for service {svc} direction {d}."}
    stops = route_stops(st, svc, d)
    pr = dv_prep(svc, d, line, stops)
    cum, stop_s = pr["cum"], pr["stop_s"]
    runs = await dv_sections(line, blocks)
    if not runs:
        return {"ok": False, "error": f"Service {svc} direction {d} does not run along the blocked section in the blocked direction."}
    run = runs[min(run_i, len(runs) - 1)]
    A, B = run["a"], run["b"]
    bands = await bands_state()
    idx = bands.get("idx")
    sfn = dv_speed_fn(idx) if traffic else None
    prof = diversion.profile(line, sfn, stop_s)
    bus = "sd" if bt_class(svc) == "SD" else "dd"
    DV_BUS.set(bus)
    avoid = dv_avoid_rects(blocks, pad_m=min(P["block_buffer_m"], 20.0)) if dv_use_tomtom() else None
    sec_line = diversion.cut(line, cum, A, B)
    bvias = []
    if not avoid and len(sec_line) >= 2:
        try:
            bvias = await asyncio.to_thread(dv_busroad_vias, await asyncio.to_thread(dv_busnet, st, diversion.bbox(sec_line, 1500), 0.2), sec_line)
        except Exception:
            bvias = []

    def heading(p, s=None):
        # V16.26g: with s (m along the route) the route's heading at THAT passage - see dv_heading_near
        return dv_heading_near(p, line, cum, s) if s is not None else diversion.nearest_on_line(p, line, cum)[3]

    def at(s_):
        return diversion.point_at(line, cum, max(0.0, min(cum[-1], s_)))
    ctx = {"P": P, "st": st, "svc": svc, "d": d, "line": line, "cum": cum, "stops": stops, "stop_s": stop_s, "A": A, "B": B, "bus": bus,
           "idx": idx, "at": at, "heading": heading, "avoid": avoid, "bvias": bvias, "manual": None, "donors": [],
           "block_lines": [(b_["line"], bool(b_["directed"])) for b_ in blocks], "next_cache": DV_NEXT_CACHE, "bkey": dv_block_key(blocks)}
    ia = diversion.find_last_reachable_stop(stop_s, A, None, P)
    down = diversion.get_downstream_rejoin_candidates(stop_s, B, None, None, P)
    unavoidable = sum(1 for x in stop_s if A - P["block_end_margin_m"] < x < B + P["block_end_margin_m"])
    base = {"ok": True, "service": svc, "direction": d, "destination": stops[-1]["name"] if stops else "", "bus_type": bus.upper(),
            "line_source": src, "router": "TomTom" if dv_use_tomtom() else "OSRM", "traffic": traffic,
            "last_reachable": {"code": stops[ia]["code"], "name": stops[ia]["name"]} if ia is not None else None,
            "first_after": {"code": stops[down[0]]["code"], "name": stops[down[0]]["name"]} if down else None,
            "unavoidable": unavoidable, "block_a": round(A, 1), "block_b": round(B, 1)}
    win = diversion.cut(line, cum, max(0.0, A - 3000.0), min(cum[-1], B + 3000.0))
    base["original_line"] = [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(win, 500)]
    base["stops"] = [{"code": stops[k]["code"], "name": stops[k]["name"], "lat": stops[k]["lat"], "lon": stops[k]["lon"], "s": round(stop_s[k]),
                      "in_block": A - P["block_end_margin_m"] < stop_s[k] < B + P["block_end_margin_m"]}
                     for k in range(len(stops)) if A - 3000.0 <= stop_s[k] <= B + 3000.0]
    if ia is None or not down:
        base.update(status="none", headline="NO VERIFIED DIVERSION FOUND", attempts=[], candidates=[],
                    failures=["No original stop before the blockage the bus can still serve." if ia is None else "No original stop after the blockage to rejoin at."])
        return base
    weights = dv2_weights()
    limits = dv2_limits()
    ctx["dd_sd_ok"] = bool(limits.get("dd_sd_roads", 1))              # V16.24: double-deckers on single-deck-only roads: flagged (1) or never (0)
    b0 = down[0]
    try:
        ctx["chains"], chain_info = await asyncio.to_thread(dv_dd_chains, st, svc, d, stops, ia, down, b0, ctx["block_lines"], bus, Q["upstream_max"])
    except Exception as e:
        ctx["chains"], chain_info = {}, {"n": 0, "text": f"stop-chain search failed ({type(e).__name__})"}
    base["dd_chain"] = chain_info
    evaluated, attempts, valid_all = {}, [], []

    def probes(i):
        """junction probes after stop i: points further along the ORIGINAL route (latest first, short of the exclusion zone
        and of the next stop) - the bus keeps following its normal path and may only turn off at a later junction"""
        lim = min(A - P["block_end_margin_m"] - 25.0, (stop_s[i + 1] - 20.0) if i + 1 < len(stop_s) else 1e18)
        pts = [stop_s[i] + off for off in (150.0, 300.0, 450.0) if stop_s[i] + off < lim]
        return sorted(pts, reverse=True)

    def late_exits(i):
        """V16.23: points along a long stop-less stretch after stop i (latest first), short of the blockage"""
        lim = min(A - P["block_end_margin_m"] - 25.0, (stop_s[i + 1] - 20.0) if i + 1 < len(stop_s) else 1e18)
        if lim - stop_s[i] < Q["late_exit_gap_m"]:
            return []
        return [lim - back for back in Q["late_exit_back_m"] if lim - back > stop_s[i] + 450.0]

    async def eval_pair(i, j):
        routes, err = await route_between_stops(ctx, i, j)
        routes = [(r, None) for r in routes]
        ok, reasons = [], []
        probe_used = None
        for pass_ in range(2):
            if pass_ == 1:
                if ok:
                    break
                more = []
                for s0 in probes(i):        # nothing valid from the stop itself: keep to the normal route a little longer
                    rr, _ = await route_between_stops(ctx, i, j, s0)
                    more += [(r, s0) for r in rr]
                    if rr:
                        probe_used = s0
                if not more:
                    break
                routes += more
                cand_routes = more
            else:
                cand_routes = list(routes)
            await eval_routes(i, j, cand_routes, ok, reasons)
        # V16.23 late exit: a long stretch with no stop between this stop and the blockage (an expressway) - also try staying on
        # the normal route and leaving at a later exit; nearest the blockage first, 3 at a time, stop at the first batch that works
        late = late_exits(i)
        for b_ in range(0, len(late), Q["late_exit_batch"]):
            batch = late[b_:b_ + Q["late_exit_batch"]]
            got = await asyncio.gather(*[route_between_stops(ctx, i, j, s0) for s0 in batch])
            more = [(r, s0) for s0, (rr, _) in zip(batch, got) for r in rr]
            routes += more
            k0 = len(ok)
            await eval_routes(i, j, more, ok, reasons)
            if len(ok) > k0:
                break
        if ok:
            res, reason = "valid", f"{len(ok)} valid route(s)" + (" \u2014 found by continuing on the normal route to a later junction" if any(c.get("probe") for c in ok) else "")
        elif not routes:
            res, reason = "fail", "No verified road connection found"
        else:
            res, reason = "fail", dv2.best_reason(reasons) if reasons else "No verified road connection found"
        evaluated[(i, j)] = ok
        attempts.append({"from_code": stops[i]["code"], "from_name": stops[i]["name"], "to_code": stops[j]["code"], "to_name": stops[j]["name"],
                         "extra": (ia - i) + (j - b0), "earlier": i < ia, "routes": len(routes), "result": res, "reason": reason})
        return ok

    async def eval_routes(i, j, cand_routes, ok, reasons):
        for r, s0 in cand_routes:
            c, why = await dv_validate_route(ctx, r, i, j, s0)
            if c is None:
                reasons.append(why)
                continue
            ap = heading(c["dep"]["leave_pt"], c["dep"]["leave_s"])
            c["exit_differs"] = False
            if c.get("free"):         # free start: a different exit only if the bus really sets off another way than its route
                rl = c["r"]["line"]
                rc_ = diversion.cum_m(rl)
                h0 = diversion.bearing(rl[0], diversion.point_at(rl, rc_, min(rc_[-1], 30.0)))
                nm_ = (stops[c["asked"][0]].get("name") or "").upper()
                off_ = diversion.nearest_on_line((stops[c["asked"][0]]["lat"], stops[c["asked"][0]]["lon"]), line, cum)[0]
                interchange_like = (bool(_TERMINAL_RX.search(nm_)) and not nm_.startswith(("OPP ", "AFT ", "BEF "))) or off_ >= 20.0
                if interchange_like and diversion.angdiff(h0, heading(rl[0], stop_s[c["asked"][0]])) > 60.0:
                    ap, c["exit_differs"] = None, True
            dp = diversion.bearing(at(c["dep"]["rejoin_s"]), at(c["dep"]["rejoin_s"] + 25.0))
            rj_road = dv2._road_starting_near(r.get("steps"), c["dep"]["rejoin_pt"], 40.0)     # the road the bus rejoins
            instr = dv2.instructions(r.get("steps"), c["seg"], ap, dp, rj_road)
            bad = dv2.impossible_turn(instr)
            if bad:
                reasons.append(bad)
                continue
            nxt = await validate_next_original_leg(ctx, c["pair"][1])
            if not nxt["ok"]:
                reasons.append(f"rejoin stop {stops[c['pair'][1]]['code']} fails the next-leg check (from there on to its next stop {nxt['next_code'] or 'on the route'}): {nxt['detail']}")
                continue
            c["instr"], c["next_leg"], c["probe"], c["s0"] = instr, nxt, s0 is not None, s0
            pts_, i0_, i1_ = c["dep"]["pts"], c["dep"]["i0"], c["dep"]["i1"]                  # V16.26: the road before / after the diversion
            c["from_road"] = dv2_title(dv_road_at(r.get("steps"), pts_[max(0, i0_ - 2)])) if i0_ >= 2 else ""
            c["back_road"] = dv2_title(rj_road or dv_road_at(r.get("steps"), pts_[min(len(pts_) - 1, i1_ + 2)]))
            twin = next((o for o in ok if o["signature"] == c["signature"] and abs(o["div_m"] - c["div_m"]) < 150 and o["pair"] == c["pair"]), None)
            if twin is not None:
                if c.get("src") == "ddchain":
                    twin["src"], twin["asked"] = "ddchain", c.get("asked")     # the same roads, proven stop by stop on double-deck services
                continue
            ok.append(c)

    def metrics(c):
        r, dep, seg = c["r"], c["dep"], c["seg"]
        li, rj = c["pair"]
        leave_s, rejoin_s = dep["leave_s"], dep["rejoin_s"]
        fb = (r["km"] / (r["osrm_min"] * offservice.PARAMS["bus_time_factor"] / 60.0)) if (traffic and r.get("osrm_min")) else None
        dprof = diversion.profile(seg, sfn, None, fallback_kmh=fb)
        div_min = dprof["t"][-1]
        normal_min = diversion.t_at(prof, rejoin_s) - diversion.t_at(prof, leave_s)
        sk = diversion.stops_in(stop_s, leave_s + 1.0, rejoin_s - 1.0)
        cx = dv2.bc_complexity(c["instr"], c["groups"], c["suit"], c["evidence"])
        bl, bsc = dv2.busroad_label(c["evidence"], c["suit"])
        dd_doubt = bus == "dd" and (c.get("bus_road") or {}).get("dd") in ("UNCONFIRMED", "SD_ONLY")
        m = {"leave_index": li, "rejoin_index": rj, "leave_name": stops[li]["name"], "leave_code": stops[li]["code"],
             "rejoin_name": stops[rj]["name"], "rejoin_code": stops[rj]["code"], "earlier": li < ia, "leave_after_ia": li - ia,
             "skipped": [{"code": stops[k]["code"], "name": stops[k]["name"], "lat": stops[k]["lat"], "lon": stops[k]["lon"],
                          "in_block": A - P["block_end_margin_m"] < stop_s[k] < B + P["block_end_margin_m"]} for k in sk],
             "skipped_n": len(sk), "unavoidable": unavoidable, "extra_skipped": max(0, len(sk) - unavoidable),
             "div_km": round(c["div_m"] / 1000.0, 2), "normal_km": round((rejoin_s - leave_s) / 1000.0, 2),
             "added_km": round((c["div_m"] - (rejoin_s - leave_s)) / 1000.0, 2), "added_min": round(div_min - normal_min, 1),
             "rejoin_after_m": round(rejoin_s - B), "simplicity": cx["simplicity"], "turns": cx["turns"], "sharp": cx["sharp"],
             "road_changes": max(0, cx["roads"] - 1), "minor_share": cx["minor_share"], "junction": dv2.junction_quality(c["instr"]),
             "busroad_label": bl, "busroad_score": bsc, "dd_doubt": dd_doubt, "evidence_text": c["evidence"]["text"],
             "route_conf": (1.0 if str(r.get("router", "")).startswith("TomTom") else 0.8) - (0.2 if c["evidence"]["level"] == 3 else 0.0),
             "next_ok": bool(c["next_leg"]["ok"]), "next_name": c["next_leg"]["next_name"], "next_code": c["next_leg"]["next_code"],
             "instructions": c["instr"], "router": r.get("router", ""), "probe": bool(c.get("probe")), "free_start": bool(c.get("exit_differs")),
             "bc": cx, "bc_label": cx["label"], "bc_points": cx["points"], "roads": cx["roads"], "leave_before_m": round(max(0.0, A - leave_s)),
             "keep_km": round(max(0.0, leave_s - stop_s[li]) / 1000.0, 1) if (c.get("probe") or c.get("src") == "shared") else 0.0,  # V16.23: on the normal route past the stop
             "keep_road": dv2_title(((r.get("steps") or [{}])[0].get("road") or "")) if (c.get("probe") or c.get("src") == "shared") else ""}
        fp = dv2.footprint(m)
        m.update(footprint_label=fp["label"], footprint_index=fp["index"], footprint_score=fp["score"])
        m["warnings"], m["penalty"] = dv2.safeguards(m, limits)
        br_ = c.get("bus_road") or {}
        if bus == "dd" and br_.get("dd") == "SD_ONLY":                    # V16.24: allowed, but the controller must confirm
            sdl = br_.get("sd_only") or []
            m["dd_sd"] = ", ".join(sorted({x["road"] for x in sdl})[:3]) or "part of the route"
            m["warnings"].append(f"Double-decker on {m['dd_sd']}: only single-deck services run there \u2014 confirm the bus fits (trees, bridges)")
            m["penalty"] = round(m["penalty"] + min(12.0, 5.0 + 6.0 * sum(x["m"] for x in sdl) / 1000.0), 1)
        m["early_turn"] = c.get("src") == "early"
        if c.get("tt_weight"):                                            # V16.25: TomTom shows a weight limit there (height fine)
            m["tt_weight"] = c["tt_weight"]
            m["warnings"].append(f"Double-decker on {c['tt_weight']}: TomTom's map shows a weight limit under 18 t \u2014 confirm buses may use it")
            m["penalty"] = round(m["penalty"] + Q["share_weight_pen"], 1)
        return m

    # progressive search: nearest combinations first; stop expanding soon after a strong valid diversion
    levels = dv2.search_levels(ia, b0, len(stops))
    strong_at = None
    for k, lv in enumerate(levels):
        if not lv:
            continue
        res = await asyncio.gather(*[eval_pair(i, j) for i, j in lv if (i, j) not in evaluated])
        for ok in res:
            for c in ok:
                c["m"] = metrics(c)
                c["score"], c["parts"] = dv2.occ_score(c["m"], weights)
                c["confidence"] = dv2.overall_confidence(c["m"])
                valid_all.append(c)
        if strong_at is None and any(c["confidence"] in ("HIGH", "MEDIUM") for c in valid_all):
            strong_at = k
        if strong_at is not None and k >= strong_at + Q["extra_levels"]:
            distinct = len({(c["signature"], c["pair"]) for c in valid_all})
            if distinct >= Q["min_candidates"] or k >= strong_at + Q["extra_levels"] + 2:
                break           # enough verified candidates for a real comparison (or the search has gone far enough)
    # prove or disprove an early diversion: earlier diversion starts towards the same rejoin stop
    if valid_all:
        best0 = max(valid_all, key=lambda c: c["score"])
        rj0 = best0.get("asked", best0["pair"])[1]          # the stop the routing engine was asked to reach
        # look back Q["early_probe"] stops, and right back to the start of the route (e.g. an interchange) when it is near
        first_i = 0 if stop_s[ia] - stop_s[0] <= Q["early_window_m"] else max(0, ia - Q["early_probe"])
        early = [(i, rj0) for i in range(first_i, ia) if (i, rj0) not in evaluated][-8:]
        if early:
            res = await asyncio.gather(*[eval_pair(i, j) for i, j in early])
            for ok in res:
                for c in ok:
                    c["m"] = metrics(c)
                    c["score"], c["parts"] = dv2.occ_score(c["m"], weights)
                    c["confidence"] = dv2.overall_confidence(c["m"])
                    valid_all.append(c)
    # V16.23: always test the best double-deck stop chains too, so the stop-to-stop option can be compared
    chain_todo = [ij for _, ij in sorted((ch["cost"], ij) for ij, ch in (ctx.get("chains") or {}).items() if ij not in evaluated)][:2]
    if chain_todo:
        res = await asyncio.gather(*[eval_pair(i, j) for i, j in chain_todo])
        for ok in res:
            for c in ok:
                c["m"] = metrics(c)
                c["score"], c["parts"] = dv2.occ_score(c["m"], weights)
                c["confidence"] = dv2.overall_confidence(c["m"])
                valid_all.append(c)
    base["dd_chain"]["tested"] = sum(1 for c in valid_all if c.get("src") == "ddchain")
    # V16.24 turn back early: a valid diversion that passes close to an earlier stop of the bus's own route (before the stop
    # it rejoins at) is also asked back to THAT stop - the OCC rule: always connect back at the nearest stop the service serves
    tries = 0
    for c in sorted(valid_all, key=lambda c: -c["score"])[:3]:
        rj, seg_ = c["pair"][1], c["seg"]
        if rj <= b0 or tries >= Q["early_rejoin_tries"] or len(seg_) < 3:
            continue
        scum = diversion.cum_m(seg_)
        offk = [k for k, p in enumerate(seg_) if diversion.nearest_on_line(p, line, cum)[0] > 60.0]
        for j2 in [j for j in down if j < rj]:
            dd_, pos_, _, _ = diversion.nearest_on_line((stops[j2]["lat"], stops[j2]["lon"]), seg_, scum)
            pre = [k for k in offk if scum[k] < pos_ - 30.0]
            if dd_ > Q["early_rejoin_m"] or not pre or tries >= Q["early_rejoin_tries"]:
                continue
            tries += 1
            ap = diversion.point_at(seg_, scum, pos_)
            vias = [seg_[k] for k in pre[::max(1, len(pre) // 5)]][:5] + [ap]
            a0 = c["r"]["line"][0]
            sb2 = max(stop_s[j2] - 5.0, stop_s[c["asked"][0]] + 10.0)
            b_ = at(sb2)
            sa2 = c["s0"] if c.get("s0") is not None else stop_s[c["asked"][0]] + 1.0
            rr = await dv_osrm([a0] + vias + [b_], 0, [None if c.get("free") else heading(a0, sa2)] + [None] * len(vias) + [heading(b_, sb2)], ctx["avoid"])
            got = [(dict(r, _src="early"), c.get("s0")) for r in rr.get("routes") or []]
            ok2, rs2 = [], []
            if got:
                await eval_routes(c["asked"][0], j2, got, ok2, rs2)
            i0 = c["asked"][0]
            attempts.append({"from_code": stops[i0]["code"], "from_name": stops[i0]["name"], "to_code": stops[j2]["code"], "to_name": stops[j2]["name"],
                             "extra": (ia - i0) + (j2 - b0), "earlier": i0 < ia, "routes": len(got), "result": "valid" if ok2 else "fail",
                             "reason": ("turns back early from the diversion to the nearer stop" if ok2 else
                                        "turning back early: " + (dv2.best_reason(rs2) if rs2 else "no road route"))})
            for c2 in ok2:
                c2["m"] = metrics(c2)
                c2["score"], c2["parts"] = dv2.occ_score(c2["m"], weights)
                c2["confidence"] = dv2.overall_confidence(c2["m"])
                valid_all.append(c2)
            if ok2:
                break
    # V16.25 share pass: the other affected services' best diversions (sent by the page once every service is analysed) are
    # tried for THIS service - from its own stops, for its own bus size - so services at the same blockage never end up on
    # different routes without a reason. Each one is either adopted as a candidate or comes back with the exact reason.
    # V16.26: the same pass checks a route the controller DREW on the map, or the route of a saved past case ("prefer": the
    # controller's choice - used whenever it passes the checks; when it does not, the reason and the stops it would skip come back).
    shared, shared_c = [], []
    win0 = max(0.0, A - 3000.0)

    def share_geom(dn):
        """where the other diversion leaves / comes back onto THIS service's route, and its off-route points"""
        dl = [(float(p_[0]), float(p_[1])) for p_ in (dn.get("line") or [])][:600]
        if len(dl) < 2:
            return None, "no route line"
        dd_ = diversion.densify(dl, 25.0)
        wc = diversion.cum_m(win)
        prj = [diversion.nearest_on_line(p_, win, wc) for p_ in dd_]
        on = Q["share_on_route_m"]
        k_off = next((k for k, x in enumerate(prj) if x[0] > 50.0), None)          # where it first leaves this service's route
        if k_off is None:
            return None, f"stays on {svc_txt}'s own route"
        k_st = max([k for k in range(k_off) if prj[k][0] <= on and prj[k][1] + win0 <= A + P["block_end_margin_m"]], default=None)
        if k_st is None:
            # V16.26g: a route that goes the OTHER way past the blockage (a service in the opposite direction, when both
            # directions are blocked) is never mixed in: each direction is planned on its own
            pre = [prj[k][1] + win0 for k in range(k_off) if prj[k][0] <= on]
            post = [prj[k][1] + win0 for k in range(k_off, len(prj)) if prj[k][0] <= on]
            if pre and post and min(pre) >= B - P["block_end_margin_m"] and max(post) <= A + P["block_end_margin_m"]:
                return None, f"goes the other way past the blockage (the opposite direction to {svc_txt}) \u2014 each direction is planned on its own"
            return None, f"turns off where {svc_txt} does not run before the blockage"
        k_end = next((k for k in range(k_off, len(prj)) if prj[k][0] <= on and prj[k][1] + win0 >= B + P["block_end_margin_m"]), None)
        if k_end is None:
            return None, f"does not come back onto {svc_txt}'s route after the blockage"
        sl, sr = prj[k_st][1] + win0, prj[k_end][1] + win0
        off = [dd_[k] for k in range(k_st, k_end) if prj[k][0] > 50.0]
        return {"dd": dd_[k_st:k_end + 1], "off": off, "sl": sl, "sr": sr, "lp": dd_[k_st], "rp": dd_[k_end]}, ""

    def near_frac(pts, ref, tol):
        if not pts or len(ref) < 2:
            return 0.0
        rc = diversion.cum_m(ref)
        return sum(1 for p_ in pts if diversion.nearest_on_line(p_, ref, rc)[0] <= tol) / len(pts)

    def follows(r, g, expect_m):
        """the routing engine's route really drives the other diversion's roads (no way round, no extra loop)"""
        rl = r.get("line") or []
        return (len(rl) >= 2 and diversion.line_m(rl) <= 1.25 * expect_m + 300.0
                and near_frac(g["off"][::max(1, len(g["off"]) // 40)], rl, Q["share_follow_m"]) >= Q["share_follow_frac"])

    async def share_one(dn):
        sv_ = str(dn.get("service") or "?")[:12]
        lab = str(dn.get("label") or "")[:60]
        who_ = lab or f"service {sv_}"
        out_ = {"from": sv_, "label": lab, "prefer": bool(dn.get("prefer")), "roads": [str(x)[:60] for x in (dn.get("roads") or [])][:12],
                "ok": False, "used": False, "reason": ""}
        try:
            g, why_ = await asyncio.to_thread(share_geom, dn)
        except (TypeError, ValueError, KeyError, IndexError):
            g, why_ = None, "incomplete route data"
        if g is None:
            out_["reason"] = why_[:1].upper() + why_[1:]
            return out_, []
        i = max([k for k in range(ia + 1) if stop_s[k] <= g["sl"] + 1.0], default=None)
        j = min([k for k in down if stop_s[k] >= g["sr"] - 1.0], default=None)
        if i is None or j is None:
            out_["reason"] = "Turns off before the first stop of the route" if i is None else "Comes back too far down the route"
            return out_, []
        # what it means for this service even if it fails a check (the controller may still decide to use it)
        out_["geom"] = {"leave": {"code": stops[i]["code"], "name": stops[i]["name"]}, "rejoin": {"code": stops[j]["code"], "name": stops[j]["name"]},
                        "skipped": [{"code": stops[k]["code"], "name": stops[k]["name"]} for k in range(len(stops)) if g["sl"] + 1.0 < stop_s[k] < g["sr"] - 1.0],
                        "km": round(diversion.line_m(g["dd"]) / 1000.0, 2)}
        free = dv_free_start(ctx, i)
        a_ = (stops[i]["lat"], stops[i]["lon"]) if free else at(stop_s[i] + 1.0)
        sb_ = max(stop_s[j] - 5.0, stop_s[i] + 10.0)
        b_ = at(sb_)
        off = g["off"]
        vias = dv_run_vias(off)                                          # V16.26b: mid-stretch waypoints: no extra turns
        if not vias:
            nv = min(6, len(off))
            vias = [off[round(k * (len(off) - 1) / max(1, nv - 1))] for k in range(nv)]
        nv = len(vias)
        brs = [None if free else heading(a_, stop_s[i] + 1.0)] + [None] * nv + [heading(b_, sb_)]
        expect = max(0.0, g["sl"] - stop_s[i]) + diversion.line_m(g["dd"]) + max(0.0, stop_s[j] - g["sr"])
        rr = await dv_osrm([a_] + vias + [b_], 0, brs, ctx["avoid"])
        tt = dv_use_tomtom() and not rr.get("tt_error")                  # V16.26e: TomTom out of budget / down: OpenStreetMap's route counts
        got = [r for r in rr.get("routes") or [] if (not tt or str(r.get("router", "")).startswith("TomTom")) and follows(r, g, expect)]
        ok2, rs2 = [], []
        if got:
            await eval_routes(i, j, [(dict(r, _src="shared"), None) for r in got], ok2, rs2)
        reason = ""
        if not got:
            other = [dv2_title(x.get("road") or "") for r in rr.get("routes") or [] for x in r.get("steps") or []]
            miss = [x for x in out_["roads"][1:] if x not in other] or out_["roads"][1:] or out_["roads"]
            where = ", ".join(list(dict.fromkeys(miss))[:3]) or "those roads"
            reason = f"The routing engine would not drive a {'double' if bus == 'dd' else 'single'}-deck bus along {where}"
            if tt and bus == "dd":                       # which limit? the double-decker's height, or its weight
                coords_d = [g["lp"]] + vias + [g["rp"]]
                brs_d = [diversion.bearing(g["dd"][0], g["dd"][1])] + [None] * (nv + 1)
                exp_d = diversion.line_m(g["dd"])

                async def fits(key):
                    x = await tt_route(coords_d, 0, brs_d, key, ctx["avoid"])
                    return [r for r in x.get("routes") or [] if follows(r, g, exp_d)], (x.get("error") if not x.get("routes") else None)
                (full, e1), (h_only, e2), (w_only, e3) = await asyncio.gather(fits("dd"), fits("dd_wt"), fits("dd_ht"))
                if full:
                    reason = f"TomTom takes a double-decker along {where} only from {who_}'s turn-off, not from stop {stops[i]['code']}"
                elif e1 and e2 and e3:
                    reason += f" (TomTom could not be asked why: {e1})"
                elif h_only and not w_only:
                    reason = f"TomTom's map has a height limit under 4.4 m on {where} — too low for a double-deck bus"
                elif w_only and not h_only:
                    reason = f"TomTom's map has a weight limit under 18 t on {where}"
                    if not limits.get("dd_weight_roads", 1):
                        reason += " (OCC setting: double-deckers never)"
                    else:              # the height is fine: drive it as a double-decker but flag the weight limit to confirm
                        x = await tt_route([a_] + vias + [b_], 0, brs, "dd_ht", ctx["avoid"])
                        got_w = [dict(r, _src="shared") for r in x.get("routes") or [] if follows(r, g, expect)]
                        if got_w:
                            await eval_routes(i, j, [(r, None) for r in got_w], ok2, rs2)
                            for c2 in ok2:
                                c2["tt_weight"] = where
                        if not ok2:
                            reason += "; from this service's stops: " + (dv2.best_reason(rs2) if rs2 else "no road route")
                else:
                    reason = f"TomTom's map keeps a 4.4 m, 18 t double-decker off {where}"
        elif not ok2:
            reason = dv2.best_reason(rs2) if rs2 else "No verified road connection found"
        for c2 in ok2:
            c2["shared_from"], c2["shared_label"] = sv_, lab
            if dn.get("prefer"):
                c2["prefer"], c2["prefer_for"] = True, [str(x)[:8] for x in (dn.get("for") or [])][:30]
            c2["m"] = metrics(c2)
            c2["m"]["shared_label"] = lab
            c2["score"], c2["parts"] = dv2.occ_score(c2["m"], weights)
            c2["confidence"] = dv2.overall_confidence(c2["m"])
        out_["reason"] = reason
        attempts.append({"from_code": stops[i]["code"], "from_name": stops[i]["name"], "to_code": stops[j]["code"], "to_name": stops[j]["name"],
                         "extra": (ia - i) + (j - b0), "earlier": i < ia, "routes": len(rr.get("routes") or []), "result": "valid" if ok2 else "fail",
                         "reason": f"{who_}'s diversion: " + ("valid for this bus too" if ok2 else reason)})
        return out_, ok2
    svc_txt = f"service {svc}"
    for out_, cs in await asyncio.gather(*[share_one(dn) for dn in (donors or [])[:Q["share_max"]] if isinstance(dn, dict)]):
        shared.append(out_)
        shared_c.append(cs)
        valid_all.extend(cs)
    # distinct candidates (same roads + same stops = one)
    uniq = []
    for c in sorted(valid_all, key=lambda c: (-c["score"], c.get("src") != "ddchain")):
        twin = next((u for u in uniq if u["signature"] == c["signature"] and u["pair"] == c["pair"]), None)
        if twin is None:
            uniq.append(c)
        elif c.get("src") == "ddchain" and twin.get("src") != "ddchain" and abs(twin["score"] - c["score"]) < 0.5:
            twin["src"], twin["asked"] = "ddchain", c.get("asked")         # same roads: the stop chain proves them stop by stop
    shared_u = []                                                         # V16.25: the candidate each shared diversion became
    for cs in shared_c:
        tw = next((u for c in cs for u in uniq if u is c or (u["signature"] == c["signature"] and u["pair"] == c["pair"])), None)
        if tw is not None:
            tw.setdefault("shared_from", cs[0]["shared_from"])
            tw["m"]["shared_from"] = tw["shared_from"]
            if any(c.get("prefer") for c in cs):                         # V16.26: the controller's route (drawn / past case)
                tw["prefer"], tw["shared_from"], tw["shared_label"] = True, cs[0]["shared_from"], cs[0].get("shared_label", "")
                tw["prefer_for"] = cs[0].get("prefer_for") or []
                tw["m"]["shared_from"] = tw["shared_from"]
            tw.setdefault("shared_label", cs[0].get("shared_label", ""))
            tw["m"]["shared_label"] = tw["shared_label"]
        shared_u.append(tw)
    strong = [c for c in uniq if c["confidence"] in ("HIGH", "MEDIUM")]
    attempts.sort(key=lambda a: (a["extra"], a["earlier"]))
    base["attempts"] = attempts
    base["search"] = {"pairs": len(attempts), "routes": sum(a["routes"] for a in attempts), "levels_used": (strong_at if strong_at is not None else len(levels) - 1)}
    ctx_m = {"ia_name": stops[ia]["name"], "b0": b0}

    def summary(c, tags=()):
        m = c["m"]
        seg = c["seg"]
        skip_line = diversion.cut(line, cum, c["dep"]["leave_s"], c["dep"]["rejoin_s"])
        return {"id": f"{m['leave_code']}-{m['rejoin_code']}-{c['signature'][:24]}", "tags": list(tags), "score": c["score"], "parts": c["parts"],
                "confidence": c["confidence"], "busroad_label": m["busroad_label"], "evidence_text": m["evidence_text"],
                "leave": {"code": m["leave_code"], "name": m["leave_name"]}, "rejoin": {"code": m["rejoin_code"], "name": m["rejoin_name"]},
                "next": {"code": m["next_code"], "name": m["next_name"], "ok": m["next_ok"]},
                "roads": [dv2_title(g["road"]) for g in c["groups"] if g.get("road")], "instructions": m["instructions"],
                "off_roads": [dv2_title(g["road"]) for g in c["groups"] if g.get("road") and any(        # V16.25: only the roads OFF the route
                    diversion.nearest_on_line(p_, line, cum)[0] > P["on_route_m"] for p_ in (g.get("line") or [])[::max(1, len(g.get("line") or []) // 8)])],
                "instruction_text": ([f"STAY on the normal route {m['keep_km']:.1f} km" + (f" ({m['keep_road']})" if m.get("keep_road") else "")] if m.get("keep_km", 0.0) >= 0.5 else [])
                                    + dv2.instruction_text(m["instructions"], m["rejoin_name"]),
                "keep_km": m.get("keep_km", 0.0), "keep_road": m.get("keep_road", ""),
                "skipped": m["skipped"], "skipped_n": m["skipped_n"], "unavoidable": m["unavoidable"], "extra_skipped": m["extra_skipped"],
                "div_km": m["div_km"], "normal_km": m["normal_km"], "added_km": m["added_km"], "added_min": m["added_min"],
                "turns": m["turns"], "sharp": m["sharp"], "earlier": m["earlier"], "router": m["router"],
                "bc_label": m["bc_label"], "bc_points": m["bc_points"], "bc": m["bc"], "footprint_label": m["footprint_label"],
                "footprint_index": m["footprint_index"], "warnings": m["warnings"], "bc_brief": dv2.bc_sentence(m, m["instructions"]),
                "leave_before_m": m["leave_before_m"], "rejoin_after_m": m["rejoin_after_m"],
                "road_mix": (c.get("suit") or {}).get("text", ""), "sensible": not (m["bc_label"] == "HIGH" and m["footprint_label"] == "LARGE"),
                "unavoidable": m["unavoidable"],
                "line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(seg, 400)],
                "skip_line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(skip_line, 200)],
                "leave_pt": [round(c["dep"]["leave_pt"][0], 6), round(c["dep"]["leave_pt"][1], 6)],
                "rejoin_pt": [round(c["dep"]["rejoin_pt"][0], 6), round(c["dep"]["rejoin_pt"][1], 6)],
                "found_by": c.get("src", "router"), "shared_from": c.get("shared_from"), "shared_label": c.get("shared_label") or "",
                "preferred": m.get("preferred") or "", "back_road": c.get("back_road", ""),
                "from_road": c.get("from_road") or (dv2_title(str(blocks[0].get("road") or "")) if A - c["dep"]["leave_s"] < 2000.0 else ""),
                "dd_chain": dv_chain_out((ctx.get("chains") or {}).get(c.get("asked")), st) if c.get("src") == "ddchain" else None}

    pool = strong or uniq
    base["shared"] = shared

    def share_report(best=None):
        for out_, c in zip(shared, shared_u):
            if c is None:
                continue
            out_["ok"], out_["id"] = True, f"{c['m']['leave_code']}-{c['m']['rejoin_code']}-{c['signature'][:24]}"
            out_["leave"], out_["rejoin"], out_["skipped_n"] = c["m"]["leave_code"], c["m"]["rejoin_code"], c["m"]["skipped_n"]
            if best is None:
                out_["reason"] = "Valid for this bus, but low confidence"
            elif c is best:
                out_["used"], out_["reason"] = True, "Used \u2014 this is the recommended diversion"
            else:
                out_["reason"] = (f"Valid for service {svc} too, but not chosen ({c['confidence'].lower()} confidence): " if not any(c is x for x in pool) else
                                  f"Valid for service {svc} too, but not chosen: ") + dv2.why_not(best, c, ctx_m)["text"]
    if not pool:
        share_report()
        base.update(status="none", headline="NO SUITABLE DIVERSION FOUND \u2014 CONTROLLER REVIEW REQUIRED", candidates=[], failures=dv2.explain_failure(attempts),
                    why=[], why_not=[])
        return base
    def sensible(c):        # final human check: could OCC explain this to a Bus Captain who does not know the area?
        return not (c["m"]["bc_label"] == "HIGH" and c["m"]["footprint_label"] == "LARGE")
    best0 = max(pool, key=lambda c: (c["confidence"] != "LOW", sensible(c), c["score"]))
    near = [c for c in pool if (c["confidence"] != "LOW") == (best0["confidence"] != "LOW") and sensible(c) == sensible(best0)
            and c["score"] >= best0["score"] - Q["near_rejoin_pts"]]
    best = min(near, key=lambda c: (c["m"]["rejoin_index"], -c["score"]))      # V16.24: the nearest practical rejoin stop
    if best is not best0 and best["m"]["rejoin_index"] < best0["m"]["rejoin_index"]:
        best["m"]["nearer_than"] = (best0["m"]["rejoin_index"] - best["m"]["rejoin_index"], best0["m"]["rejoin_name"])
    # V16.25 consistency (OCC rule: keep diversions short, and let every affected service use the same short one when it can):
    # a shorter route another service already uses wins, if it is as practical and skips at most 1 more stop
    same = [c for c in pool if c.get("shared_from") and (c["confidence"] != "LOW") == (best["confidence"] != "LOW") and sensible(c) == sensible(best)
            and c["m"]["skipped_n"] <= best["m"]["skipped_n"] + Q["share_extra_skip"]]
    if same:
        cs = min(same, key=lambda c: (c["m"]["div_km"], c["m"]["rejoin_index"], -c["score"]))
        if cs is not best and cs["m"]["div_km"] < best["m"]["div_km"] - Q["share_shorter_km"]:
            cs["m"]["consistent"] = (cs["shared_from"], best["m"]["div_km"])
            best = cs
    # V16.26f: no confident route of its own (it would be REVIEW): the route another affected service uses wins, if it passed
    # every hard check for this bus and is not large-and-complex
    if not (best["confidence"] in ("HIGH", "MEDIUM") and sensible(best)):
        sh_ok = [c for c in uniq if c.get("shared_from") and sensible(c)]
        if sh_ok:
            cs = min(sh_ok, key=lambda c: (c["confidence"] == "LOW", c["m"]["div_km"]))
            if cs is not best:
                cs["m"]["rescued"] = cs["shared_from"]
                best = cs
    # V16.26: a route the controller chose (drawn on the map, or followed from a saved case) wins whenever it passed the checks
    pref = [c for c in uniq if c.get("prefer")]
    if pref:
        cp = min(pref, key=lambda c: (svc not in (c.get("prefer_for") or []), c["m"]["div_km"]))
        cp["m"]["preferred"] = cp.get("shared_label") or "OCC"
        best = cp
    early_alts = [c for c in uniq if c["m"]["leave_index"] < best["m"]["leave_index"]]
    best["m"]["preserved_vs_early"] = max([c["m"]["skipped_n"] - best["m"]["skipped_n"] for c in early_alts] + [0])
    tags = dv2.tag_alternatives(pool)
    tag_of = {}
    for t, c in tags.items():
        tag_of.setdefault(id(c), []).append(t)
    others = [c for c in uniq if c is not best][:Q["max_candidates"] - 1]
    for c in shared_u:                       # V16.25: another service's diversion that works for this bus is always shown
        if c is not None and c is not best and not any(c is x for x in others):
            others = others[:Q["max_candidates"] - 2] + [c]
    share_report(best)
    # V16.26f: a route that passed every hard check for this bus and is the route other affected services already use is not left
    # on REVIEW for low confidence alone (weak bus-road evidence): it is recommended, with a PLEASE CONFIRM - one route for all
    same_low = bool(best.get("shared_from")) and best["confidence"] == "LOW" and sensible(best)
    if same_low:
        best["m"]["warnings"] = list(best["m"]["warnings"]) + [f"Please confirm: low confidence for this bus ({best['m']['busroad_label'].lower()} bus-road evidence), "
                                                               f"but it passed every hard check and service {best['shared_from']} uses the same route"]
    base["status"] = "divert" if (best["confidence"] in ("HIGH", "MEDIUM") and sensible(best)) or best.get("prefer") or same_low else "review"
    base["headline"] = ("RECOMMENDED DIVERSION" if base["status"] == "divert" else
                        "NO SUITABLE DIVERSION FOUND \u2014 CONTROLLER REVIEW REQUIRED" + (" (best route is large and complex for a Bus Captain)" if not sensible(best) else " (low confidence only)"))
    base["best"] = summary(best, tag_of.get(id(best), []))
    base["candidates"] = [base["best"]] + [summary(c, tag_of.get(id(c), [])) for c in others]
    base["why"] = dv2.why_this(best, ctx_m)
    base["why_not"] = [dv2.why_not(best, c, ctx_m) for c in sorted(others, key=lambda c: c["m"]["leave_index"])[:4]]
    base["failures"] = dv2.explain_failure(attempts)
    base["weights"] = weights
    base["limits"] = limits
    if dv_use_tomtom() and tt_route_left() <= 0:                         # V16.26e
        base["router_note"] = ("TomTom's routing budget for today is used up (TOMTOM_ROUTING_CAP) \u2014 routes come from OpenStreetMap: no live traffic "
                               "and no bus size in the router; OpenStreetMap height limits and the bus-service checks still apply")
    return base


@app.post("/api/dv2/plan")
async def api_dv2_plan(request: Request):
    body = _dv_body(await request.body())
    blocks = dv2_blocks(body)
    svc = str(body.get("service") or "").strip().upper()
    try:
        d, run_i = int(body.get("direction") or 1), int(body.get("run") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "direction must be a number."}, status_code=400)
    if not blocks or not svc:
        return JSONResponse({"error": "A blockage and a service are required."}, status_code=400)
    traffic = body.get("traffic") is not False
    donors = []                    # V16.25: the other affected services' best diversions, to be tried for this service too
    for dn in (body.get("donors") if isinstance(body.get("donors"), list) else [])[:dv2.PARAMS["share_max"]]:
        if isinstance(dn, dict) and isinstance(dn.get("line"), list) and isinstance(dn.get("leave_pt"), list) and isinstance(dn.get("rejoin_pt"), list):
            donors.append({"service": str(dn.get("service") or "")[:12], "line": dn["line"][:600], "leave_pt": dn["leave_pt"][:2],
                           "rejoin_pt": dn["rejoin_pt"][:2], "roads": [str(x)[:60] for x in (dn.get("roads") or [])[:12]],
                           "label": str(dn.get("label") or "")[:60], "prefer": bool(dn.get("prefer")),          # V16.26: drawn / past-case route
                           "for": [str(x)[:8] for x in (dn.get("for") or [])[:30]] if isinstance(dn.get("for"), list) else []})
    st = await static()
    key = "dv2plan:" + hashlib.sha1(json.dumps([[[round(p[0], 5), round(p[1], 5)] for p in b["line"]] for b in blocks]
                                               + [blocks[0]["directed"], svc, d, run_i, traffic, dv2_weights()]
                                               + ([donors, dv2_limits()] if donors else [])).encode()).hexdigest()[:24]

    async def factory():
        r = await dv2_plan(st, blocks, svc, d, run_i, traffic, donors)
        return r, 120, bool(r.get("ok"))
    return copy.deepcopy(await cached(key, factory))


# =========================================================================== V16.26 hand-drawn diversions and saved case studies
def dv_backtracks(r):
    """V16.26f: where a joined route turns back on itself - a U-turn, or round a block / through a gap in the divider back
    along the other carriageway (it comes back within 25 m of where it already was, 120 m+ earlier). [(kind, (lat, lon))]"""
    ev = [("uturn", tuple(st_["line"][0])) for st_ in r.get("steps") or [] if (st_.get("mod") or "") == "uturn" and st_.get("line")]
    d = diversion.densify(r.get("line") or [], 20.0)
    grid, k = {}, 0
    cell = lambda p: (int(p[0] * 3600.0), int(p[1] * 3600.0))            # ~30 m cells
    while k < len(d):
        p, c = d[k], cell(d[k])
        hit = next((j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((c[0] + dx, c[1] + dy), [])
                    if j < k - 6 and diversion.dist_m(p, d[j]) < 25.0), None)
        if hit is not None:
            mid = d[(hit + k) // 2]                                      # the far end of the loop: where it turned back
            ev.append(("loop", tuple(mid)))
            for q in range(k, min(len(d), k + 15)):
                grid.setdefault(cell(d[q]), []).append(q)
            k += 15
            continue
        grid.setdefault(c, []).append(k)
        k += 1
    out = []
    for kind, at_ in sorted(ev, key=lambda e: e[0] != "uturn"):           # one event per place, a real U-turn first
        if all(diversion.dist_m(at_, o[1]) > 120.0 for o in out):
            out.append((kind, at_))
    return out


def dv_turns(steps):
    """turns a Bus Captain makes on a routing result: (turns, U-turns, turns closer than 80 m to the previous one)"""
    n = u = z = 0
    acc, last = 0.0, None
    for st_ in steps or []:
        man, mod = (st_.get("man") or ""), (st_.get("mod") or "")
        if man not in ("depart", "arrive"):
            turn = man in ("roundabout", "rotary", "roundabout turn") or mod in ("left", "right", "sharp left", "sharp right", "uturn")
            if mod == "uturn":
                u += 1
            if turn:
                n += 1
                if last is not None and acc - last < 0.08:
                    z += 1
                last = acc
        acc += float(st_.get("km") or 0.0)
    return n, u, z


async def dv2_draw_route(taps, mode="smart", ref=None, block_lines=None, lead=None):
    """V16.26b/c/d: the route the controller means when tapping along the roads.
    exact: through every tap. smart (default): the same, then every tap that only adds turns is treated as 'on the way' and
    dropped when the route still passes within 60 m of it - the fewest turns, no U-turns, no zigzag through side roads.
    Each tap carries the drawing direction (it is matched to the carriageway going that way).
    Start / end: a tap near the bus's own route (before the blockage / after it) means "leave / rejoin around here": the
    routing engine starts 150 m earlier on the route (ends 150 m later) and picks the junction itself, the tap only guides it.
    A tap on the green route (the current suggestion) means "follow the green route to here"; anywhere else is joined to the
    bus's route by the routing engine."""
    P = diversion.PARAMS
    raw = list(taps)
    t, merged = [raw[0]], [0]
    for k, p in enumerate(raw[1:], 1):                                   # double taps
        if diversion.dist_m(t[-1], p) > 25.0:
            t.append(p)
        merged.append(len(t) - 1)
    if len(t) < 2:
        t = [raw[0], raw[-1]]
        merged = [0] + [1] * (len(raw) - 1)
    n_t = len(t)
    rc = diversion.cum_m(ref) if ref and len(ref) >= 2 else None
    lead = lead if lead and len(lead) >= 2 else None
    lc = diversion.cum_m(lead) if lead else None
    A_ = B_ = None
    if rc and block_lines:                                               # the blocked section along the bus's route
        ss = [diversion.nearest_on_line(p, ref, rc) for bl, _ in block_lines for p in (bl[0], bl[-1])]
        ss = [x[1] for x in ss if x[0] <= 40.0]
        if ss:
            A_, B_ = min(ss), max(ss)

    def on_ref(p, lo, hi):
        """nearest point of the bus's route between positions lo..hi (m along it): (distance, position, heading) or None"""
        lo, hi = max(0.0, lo), min(rc[-1], hi)
        if hi - lo < 5.0:
            return None
        part = diversion.cut(ref, rc, lo, hi)
        d_, s_, _, h_ = diversion.nearest_on_line(p, part, diversion.cum_m(part))
        return d_, s_ + lo, h_

    def ref_pt(s_):
        s_ = max(0.0, min(rc[-1], s_))
        return diversion.point_at(ref, rc, s_), diversion.nearest_on_line(diversion.point_at(ref, rc, s_), ref, rc)[3]

    async def joiner(frm, to, h_from, h_to):
        r = await os_osrm([frm, to], 0, bearings=[h_from, h_to], continue_straight=True)
        if not r.get("routes"):
            r = await os_osrm([frm, to], 0, continue_straight=True)
        best_ = None
        for x in r.get("routes") or []:
            n_, u_, z_ = dv_turns(x.get("steps"))
            c_ = diversion.line_m(x["line"]) / 1000.0 + 0.45 * n_ + 6.0 * u_ + 1.5 * z_
            best_ = c_ if best_ is None else min(best_, c_)
        return best_

    # mids: the taps the route should pass (each {"p", "h", "ci"}); head / tail: fixed points (p, heading) before / after them
    mids = [{"p": t[k], "h": None, "ci": k} for k in range(1, n_t - 1)]
    head, tail, ends = [(t[0], None)], [(t[-1], None)], {"start": "tap", "end": "tap"}
    if rc:
        top = (A_ - 40.0) if A_ is not None else rc[-1]
        bot = (B_ + 40.0) if B_ is not None else 0.0
        pr = on_ref(t[0], 0.0, top)
        if pr and pr[0] <= 120.0:                                        # leave the bus's route around here
            head, ends["start"] = [ref_pt(pr[1] - 150.0)], "route"
            mids.insert(0, {"p": t[0], "h": pr[2], "ci": 0})
        elif lead and diversion.nearest_on_line(t[0], lead, lc)[0] <= 80.0:
            _, sl, _, hl = diversion.nearest_on_line(t[0], lead, lc)
            pre = diversion.cut(lead, lc, 0.0, sl)
            head = [(lead[0], diversion.nearest_on_line(lead[0], ref, rc)[3])] + [(v, None) for v in (dv_run_vias(pre) or [])] + [(diversion.point_at(lead, lc, sl), hl)]
            ends["start"] = "green"
        else:
            s0 = diversion.nearest_on_line(t[0], ref, rc)[1]
            sc = min(s0, top)
            h0d = diversion.bearing(t[0], t[1])
            best_c = None
            for x in [x for x in dict.fromkeys([sc, sc - 250.0, sc - 500.0]) if 0.0 <= x <= top]:
                pt, hx = ref_pt(x)
                c_ = await joiner(pt, t[0], hx, h0d)
                if c_ is not None and (best_c is None or c_ < best_c[0]):
                    best_c = (c_, pt, hx)
            if best_c:
                head, ends["start"] = [(best_c[1], best_c[2]), (t[0], h0d)], "joined"
        pr = on_ref(t[-1], bot, rc[-1])
        if pr and pr[0] <= 120.0:                                        # back on the bus's route around here
            tail, ends["end"] = [ref_pt(pr[1] + 150.0)], "route"
            mids.append({"p": t[-1], "h": pr[2], "ci": n_t - 1})
        elif lead and diversion.nearest_on_line(t[-1], lead, lc)[0] <= 80.0:
            _, sl, _, hl = diversion.nearest_on_line(t[-1], lead, lc)
            post_ = diversion.cut(lead, lc, sl, lc[-1])
            tail = [(diversion.point_at(lead, lc, sl), hl)] + [(v, None) for v in (dv_run_vias(post_) or [])] + [(lead[-1], diversion.nearest_on_line(lead[-1], ref, rc)[3])]
            ends["end"] = "green"
        else:
            s1 = diversion.nearest_on_line(t[-1], ref, rc)[1]
            sc = max(s1, bot)
            h1d = diversion.bearing(t[-2], t[-1])
            best_c = None
            for x in [x for x in dict.fromkeys([sc, sc + 250.0, sc + 500.0]) if bot <= x <= rc[-1]]:
                pt, hx = ref_pt(x)
                c_ = await joiner(t[-1], pt, h1d, hx)
                if c_ is not None and (best_c is None or c_ < best_c[0]):
                    best_c = (c_, pt, hx)
            if best_c:
                tail, ends["end"] = [(t[-1], h1d), (best_c[1], best_c[2])], "joined"
    seq = [head[-1][0]] + [m["p"] for m in mids] + [tail[0][0]]
    for k, m in enumerate(mids):                                         # the drawing direction at each tap
        if m["h"] is None:
            m["h"] = diversion.bearing(seq[k], seq[k + 2])

    async def go(keep):
        coords = [p for p, _ in head] + [mids[i]["p"] for i in keep] + [p for p, _ in tail]
        hb, tb = [b for _, b in head], [b for _, b in tail]
        tries = [hb + [mids[i]["h"] for i in keep] + tb, [hb[0]] + [None] * (len(coords) - 2) + [tb[-1]], None]
        for brs in tries:
            r = await os_osrm(coords, alternatives=2 if len(coords) == 2 else 0, bearings=brs, continue_straight=True)
            if r.get("routes"):
                return r["routes"]
        if dv_use_tomtom():
            r = await tt_route(coords, 0, None, "sd", None)
            return r.get("routes") or []
        return []

    def judge(r, keep):
        ln = r.get("line") or []
        if len(ln) < 2:
            return None
        lc_ = diversion.cum_m(ln)
        far = [diversion.nearest_on_line(p, ln, lc_)[0] for p in t]
        n, u, z = dv_turns(r.get("steps"))
        km_ = diversion.line_m(ln) / 1000.0
        through = False
        if block_lines:
            ok_, _ = diversion.validate_block_avoidance(ln, block_lines, None, P)
            through = not ok_
        bt = dv_backtracks(r)
        loops = sum(1 for kd, _ in bt if kd == "loop")
        cost = km_ + 0.45 * n + 6.0 * u + 4.0 * loops + 1.5 * z + (25.0 if through else 0.0)
        return {"r": r, "keep": list(keep), "far": far, "turns": n, "uturns": u, "zig": z, "km": km_, "through": through, "cost": cost, "bt": bt}

    allk = list(range(len(mids)))
    exact = [j for j in (judge(r, allk) for r in await go(allk)) if j]
    exact = min(exact, key=lambda j: j["cost"]) if exact else None
    best = exact
    if mode == "smart":
        tol = 60.0
        covers = lambda j: j and max(j["far"]) <= tol and not j["through"]
        pool = [exact] if covers(exact) else []
        pool += [j for j in (judge(r, []) for r in await go([])) if covers(j)]           # straight from start to end
        keep = list(allk)
        cur = exact
        for i in list(allk):                                            # each tap: does the route need it?
            trial = [x for x in keep if x != i]
            js = [j for j in (judge(r, trial) for r in await go(trial)) if covers(j)]
            if js:
                jb = min(js, key=lambda j: j["cost"])
                if cur is None or not covers(cur) or jb["cost"] <= cur["cost"] + 0.05:
                    keep, cur = trial, jb
                    pool.append(jb)
        if pool:
            best = min(pool, key=lambda j: (j["cost"], -len(j["keep"])))
        elif exact:                                                      # nothing passes every tap: the cleanest that passes the most
            cand = [exact] + [j for j in (judge(r, []) for r in await go([])) if j and not j["through"]]
            best = min(cand, key=lambda j: (sum(1 for f in j["far"] if f > tol), j["cost"]))
    if not best:
        return None
    r = best["r"]
    roads = []
    for st_ in r.get("steps") or []:
        nm = dv2_title((st_.get("road") or "").strip())
        if nm and (not roads or roads[-1] != nm):
            roads.append(nm)
    kept_ci = {mids[i]["ci"] for i in best["keep"]}
    state = []
    for k in range(len(raw)):
        ci = merged[k]
        st = "start" if ci == 0 else "end" if ci == n_t - 1 else ("kept" if ci in kept_ci else "on the way")
        state.append("missed" if best["far"][ci] > 60.0 else st)
    return {"line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(r["line"], 500)], "km": round(best["km"], 2), "roads": roads,
            "router": r.get("router") or "OpenStreetMap", "turns": best["turns"], "uturns": best["uturns"], "zig": best["zig"], "through": best["through"],
            "taps": state, "tap_far": [round(best["far"][merged[k]]) for k in range(len(raw))], "far": round(max(best["far"]), 0), "ends": ends,
            "backtracks": [{"kind": kd, "at": [round(a_[0], 6), round(a_[1], 6)],
                            "tap": min(range(len(raw)), key=lambda q: diversion.dist_m(raw[q], a_)) + 1} for kd, a_ in best["bt"]],
            "exact": {"turns": exact["turns"], "uturns": exact["uturns"], "km": round(exact["km"], 2)} if exact else None}


_RULE_TXT = {"no_right_turn": "No right turn", "no_left_turn": "No left turn", "no_u_turn": "No U-turn", "no_straight_on": "No straight on",
             "only_right_turn": "Right turn only", "only_left_turn": "Left turn only", "only_straight_on": "Straight on only",
             "no_entry": "No entry", "no_exit": "No exit"}


@app.get("/api/dv2/roadrules")
async def api_dv2_roadrules(s: float = 0.0, w: float = 0.0, n: float = 0.0, e: float = 0.0):
    """V16.26d: which way the roads go and which turns are banned around the drawing (OpenStreetMap, Overpass; cached 1 day):
    one-way roads (each carriageway of a dual road is one) in their direction of travel, and turn restrictions at junctions."""
    if not (1.1 < s < n < 1.55 and 103.5 < w < e < 104.2):
        return JSONResponse({"error": "Bad area."}, status_code=400)
    if n - s > 0.03 or e - w > 0.03:
        return {"ok": True, "zoom_in": True, "oneway": [], "turns": []}
    s, w, n, e = (round(v, 3) for v in (s - .0005, w - .0005, n + .0005, e + .0005))
    bb = f"{s},{w},{n},{e}"
    drive = "^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|busway)$"
    q = (f'[out:json][timeout:25];(way["highway"~"{drive}"]["oneway"~"^(yes|1|true|-1)$"]({bb});way["highway"~"^(motorway|motorway_link)$"]({bb});'
         f'way["highway"~"{drive}"]["junction"~"^(roundabout|circular)$"]({bb}););out tags geom;'
         f'relation["type"="restriction"]({bb})->.rs;.rs out tags geom;way(r.rs);out tags;')

    async def factory():
        try:
            r = await client().post(OVERPASS, data={"data": q}, timeout=30)
            r.raise_for_status()
            els = r.json().get("elements", [])
        except Exception as ex:
            return {"ok": False, "oneway": [], "turns": [], "error": type(ex).__name__}, 120, False
        names, oneway, turns = {}, [], []
        for el in els:
            tg = el.get("tags") or {}
            if el.get("type") == "way":
                names[el.get("id")] = dv2_title(tg.get("name") or tg.get("ref") or "")
                g = [(x["lat"], x["lon"]) for x in (el.get("geometry") or [])]
                if len(g) >= 2:
                    ow = str(tg.get("oneway") or "").lower()
                    d_ = -1 if ow == "-1" else 1
                    if ow in ("yes", "1", "true", "-1") or tg.get("highway") in ("motorway", "motorway_link") or tg.get("junction") in ("roundabout", "circular"):
                        if ow == "no":
                            continue
                        oneway.append({"name": names[el["id"]], "line": [[round(a, 6), round(b, 6)] for a, b in (g if d_ == 1 else g[::-1])]})
        for el in els:
            if el.get("type") != "relation":
                continue
            tg = el.get("tags") or {}
            kind = tg.get("restriction") or tg.get("restriction:motor_vehicle") or tg.get("restriction:motorcar") or tg.get("restriction:bus") or ""
            if kind not in _RULE_TXT:
                continue
            mem = el.get("members") or []
            fr = next((m for m in mem if m.get("role") == "from" and m.get("type") == "way"), None)
            to = next((m for m in mem if m.get("role") == "to" and m.get("type") == "way"), None)
            via = next((m for m in mem if m.get("role") == "via"), None)
            if not (fr and to and via):
                continue
            if via.get("type") == "node" and "lat" in via:
                at_ = (via["lat"], via["lon"])
            else:
                vg = [(x["lat"], x["lon"]) for x in (via.get("geometry") or [])]
                if not vg:
                    continue
                at_ = vg[len(vg) // 2]
            fg = [(x["lat"], x["lon"]) for x in (fr.get("geometry") or [])]
            tgm = [(x["lat"], x["lon"]) for x in (to.get("geometry") or [])]
            if len(fg) < 2 or len(tgm) < 2:
                continue
            if diversion.dist_m(fg[0], at_) < diversion.dist_m(fg[-1], at_):
                fg = fg[::-1]                                            # the from-way, travelling towards the junction
            if diversion.dist_m(tgm[-1], at_) < diversion.dist_m(tgm[0], at_):
                tgm = tgm[::-1]                                          # the to-way, leaving the junction
            ex_ = str(tg.get("except") or "").lower()
            fn, tn = names.get(fr.get("ref"), ""), names.get(to.get("ref"), "")
            turns.append({"kind": kind, "label": _RULE_TXT[kind], "at": [round(at_[0], 6), round(at_[1], 6)], "from": fn, "to": tn,
                          "from_brg": round(diversion.bearing(fg[-2], fg[-1])), "to_brg": round(diversion.bearing(tgm[0], tgm[1])),
                          "bus_ok": any(x in ex_ for x in ("psv", "bus"))})
        return {"ok": True, "oneway": oneway[:400], "turns": turns[:200]}, 86400, True
    return await cached("dvrules:" + bb, factory)


@app.post("/api/dv2/manual")
async def api_dv2_manual(request: Request):
    """A diversion the controller draws: the tapped points joined along the roads (OpenStreetMap routing; TomTom when OSRM is
    unreachable). mode "smart" (default) cleans it up to what the controller means: fewest turns, no U-turn, no zigzag, start
    and end on the bus's route ("ref"); "exact" goes through every tap. Each service is then checked on it by /api/dv2/plan."""
    body = _dv_body(await request.body())
    pts = []
    for p in (body.get("points") if isinstance(body.get("points"), list) else [])[:30]:
        try:
            q = (float(p[0]), float(p[1]))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if 1.1 < q[0] < 1.55 and 103.5 < q[1] < 104.2:
            pts.append(q)
    if len(pts) < 2:
        return JSONResponse({"error": "Tap at least 2 points along the roads: where the bus leaves its route, the roads it takes, where it is back on route."}, status_code=400)
    mode = "exact" if str(body.get("mode") or "smart").lower() == "exact" else "smart"
    ref = [tuple(p) for p in _cpts(body.get("ref"), 3000)] or None
    blocks = dv2_blocks(body) if body.get("blocks") else []
    lead = [tuple(p) for p in _cpts(body.get("lead"), 600)] or None          # V16.26c: the route shown in green (start / end on it is fine)
    res = await dv2_draw_route(pts, mode, ref, [(b_["line"], bool(b_["directed"])) for b_ in blocks] or None, lead)
    if not res:
        return {"ok": True, "snapped": False, "mode": mode, "line": [[round(p[0], 6), round(p[1], 6)] for p in pts], "km": round(diversion.line_m(pts) / 1000.0, 2),
                "roads": [], "router": "", "turns": None, "taps": ["kept"] * len(pts),
                "note": "These points could not be joined along the roads — the straight lines between the taps are kept."}
    note = []
    if res["through"]:
        note.append("this route still drives through the blocked section — tap a way round it")
    missed = [str(k + 1) for k, x in enumerate(res["taps"]) if x == "missed"]
    if missed:
        note.append(("taps " + ", ".join(missed) if len(missed) > 1 else "tap " + missed[0]) + " cannot be reached the way you drew it — "
                    "probably a one-way road or a banned turn: see the road arrows and ⛔ signs on the map")
    for b_ in (res.get("backtracks") or [])[:2]:                        # V16.26f: why it turns back, and what to do
        note.append(("U-turn" if b_["kind"] == "uturn" else "the route turns back (round a block)") + f" near tap {b_['tap']} \u2014 it cannot reach "
                    f"tap {b_['tap']} going forward: drag tap {b_['tap']} to the side of the road the bus is on, or before the junction")
    return dict(res, ok=True, snapped=True, mode=mode, note="; ".join(note))


DV2_CASE_COLS = {"ok": False}


def dv2_case_db():
    dv2_db()
    bb_sql("CREATE TABLE IF NOT EXISTS dv2_case(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, by_name TEXT, road TEXT, lat REAL, lon REAL, "
           "data TEXT, followed INTEGER DEFAULT 0, from_case INTEGER, manual INTEGER DEFAULT 0)")
    if not DV2_CASE_COLS["ok"]:          # V16.27: the diversion's period (start / end time); cases saved before have none
        cols = {r_["name"] for r_ in (bb_sql("PRAGMA table_info(dv2_case)", fetch=True) or [])}
        for c_ in ("start_ts", "end_ts"):
            if cols and c_ not in cols:
                bb_sql(f"ALTER TABLE dv2_case ADD COLUMN {c_} REAL")
        DV2_CASE_COLS["ok"] = bool(cols)


def _cpts(v, n):
    out = []
    for p in (v if isinstance(v, list) else [])[:n]:
        try:
            out.append([round(float(p[0]), 6), round(float(p[1]), 6)])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def _cnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def dv2_stop_ref(v):
    return {"code": str(v.get("code") or "")[:6], "name": str(v.get("name") or "")[:60]} if isinstance(v, dict) else None


def dv2_ll(x):
    try:
        la, lo = float(x.get("lat")), float(x.get("lon"))
        return {"lat": round(la, 6), "lon": round(lo, 6)} if (la or lo) else {}
    except (TypeError, ValueError):
        return {}


def dv2_when(v):
    """V16.27: a start / end time from the page -> epoch seconds | None. 'YYYY-MM-DDTHH:MM' is Singapore time (what OCC typed);
    epoch seconds are taken as they are. Raises ValueError for anything else."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if v > 0 else None
    t = datetime.fromisoformat(str(v).strip()[:32])
    return (t if t.tzinfo else t.replace(tzinfo=SGT)).timestamp()


def dv2_fmt(ts, f="%d %b %Y %H:%M"):
    return datetime.fromtimestamp(ts, SGT).strftime(f) if ts else ""


def dv2_state(r, now=None):
    """live | upcoming | ended | saved (no period: saved before V16.27)"""
    st, en = r.get("start_ts"), r.get("end_ts")
    if st is None:
        return "saved"
    now = now or time.time()
    if now < st:
        return "upcoming"
    return "ended" if en is not None and now >= en else "live"


def dv2_case_clean(body):
    """a saved case study: the blockage, each diverted route with its services, the message and the controller's notes"""
    blocks = [{"line": _cpts(b.get("line"), 400), "dir": str(b.get("dir") or "")[:6], "road": str(b.get("road") or "")[:80]}
              for b in (body.get("blocks") if isinstance(body.get("blocks"), list) else [])[:8] if isinstance(b, dict)]
    page = [{"cor": _cpts(b.get("cor"), 3000), "cm": [round(_cnum(x), 1) for x in (b.get("cm") if isinstance(b.get("cm"), list) else [])[:3000]],
             "a": _cnum(b.get("a")), "b": _cnum(b.get("b")), "road": str(b.get("road") or "")[:80], "dir": str(b.get("dir") or "")[:6],
             "labels": {k: (str(v)[:60] if not isinstance(v, (int, float)) else v) for k, v in (b.get("labels") or {}).items()
                        if k in ("fwd", "rev", "fwd_bearing", "rev_bearing")} if isinstance(b.get("labels"), dict) else {}}
            for b in (body.get("page") if isinstance(body.get("page"), list) else [])[:8] if isinstance(b, dict)]
    routes = []
    for r in (body.get("routes") if isinstance(body.get("routes"), list) else [])[:12]:
        if not isinstance(r, dict):
            continue
        lp, rp = _cpts([r.get("leave_pt")], 1), _cpts([r.get("rejoin_pt")], 1)
        routes.append({"n": int(_cnum(r.get("n"), len(routes) + 1)), "services": [str(x)[:8] for x in (r.get("services") or [])[:40]],
                       "roads": [str(x)[:60] for x in (r.get("roads") or [])[:20]], "text": str(r.get("text") or "")[:600],
                       "line": _cpts(r.get("line"), 500), "leave_pt": lp[0] if lp else None, "rejoin_pt": rp[0] if rp else None,
                       "skipped": [dict({"code": str(x.get("code") or "")[:6], "name": str(x.get("name") or "")[:60]}, **dv2_ll(x)) for x in (r.get("skipped") or [])[:40] if isinstance(x, dict)],
                       "km": round(_cnum(r.get("km")), 2), "runs": str(r.get("runs") or "")[:2000], "mileage": str(r.get("mileage") or "")[:20],
                       "source": str(r.get("source") or "")[:20], "flags": [str(x)[:240] for x in (r.get("flags") or [])[:10]],
                       # V16.27: what the TV needs to replay the diversion road by road
                       "legs": [{"road": str(x.get("road") or "")[:60], "turn": str(x.get("turn") or "")[:16], "s": round(_cnum(x.get("s")), 1)}
                                for x in (r.get("legs") if isinstance(r.get("legs"), list) else [])[:30] if isinstance(x, dict)],
                       "from_road": str(r.get("from_road") or "")[:60], "back_road": str(r.get("back_road") or "")[:60],
                       "leave": dv2_stop_ref(r.get("leave")), "rejoin": dv2_stop_ref(r.get("rejoin")),
                       "orig": _cpts(r.get("orig"), 500), "skip_line": _cpts(r.get("skip_line"), 500),
                       "added_km": round(_cnum(r.get("added_km")), 2), "added_min": round(_cnum(r.get("added_min")), 1), "turns": int(_cnum(r.get("turns"), 0))})
    services = [{"service": str(s_.get("service") or "")[:8], "direction": int(_cnum(s_.get("direction"), 1)), "run": int(_cnum(s_.get("run"), 0)),
                 "route": int(_cnum(s_.get("route"), 0)), "leave": str(s_.get("leave") or "")[:6], "rejoin": str(s_.get("rejoin") or "")[:6],
                 "source": str(s_.get("source") or "")[:20]}
                for s_ in (body.get("services") if isinstance(body.get("services"), list) else [])[:60] if isinstance(s_, dict)]
    fc = body.get("from_case")
    return {"v": 1, "blocks": blocks, "page": page, "routes": routes, "services": services, "text": str(body.get("text") or "")[:6000],
            "note": str(body.get("note") or "")[:1000], "from_case": int(fc) if isinstance(fc, (int, float)) and fc > 0 else None,
            "_start": body.get("start"), "_end": body.get("end")}


def dv2_case_brief(r, data=None, full=False):
    try:
        d = data or json.loads(r["data"] or "{}")
    except ValueError:
        d = {}
    out = {"id": r["id"], "ts": r["ts"], "date": datetime.fromtimestamp(r["ts"], SGT).strftime("%d %b %Y"), "time": datetime.fromtimestamp(r["ts"], SGT).strftime("%H:%M"),
           "by": r["by_name"] or "", "road": r["road"] or "", "followed": int(r["followed"] or 0), "manual": bool(r["manual"]), "note": d.get("note", ""),
           "services": [s_["service"] for s_ in d.get("services", [])],
           "routes": [{k: v for k, v in x.items() if full or k not in ("line", "runs", "orig", "skip_line", "legs")} for x in d.get("routes", [])]}
    st, en = r.get("start_ts"), r.get("end_ts")                         # V16.27: the period
    out.update(start_ts=st, end_ts=en, start=dv2_fmt(st), end=dv2_fmt(en), start_in=dv2_fmt(st, "%Y-%m-%dT%H:%M"), end_in=dv2_fmt(en, "%Y-%m-%dT%H:%M"),
               state=dv2_state(r), from_case=r.get("from_case"), blocked=", ".join(dict.fromkeys(b.get("road", "") for b in d.get("blocks", []) if b.get("road"))) or (r["road"] or ""),
               dir_txt=", ".join(dict.fromkeys(((p_.get("labels") or {}).get(p_.get("dir")) or ("Both directions" if p_.get("dir") == "both" else "")) for p_ in d.get("page", []))).strip(", "))
    if full:
        out.update(blocks=d.get("blocks", []), page=d.get("page", []), text=d.get("text", ""), service_detail=d.get("services", []))
    return out


@app.post("/api/dv2/cases")
async def api_dv2_case_save(request: Request):
    """save a case study (shared by everyone): next time this road is blocked the planner offers its route again"""
    dv2_case_db()
    d = dv2_case_clean(_dv_body(await request.body()))
    if not d["blocks"] or not d["routes"]:
        return JSONResponse({"error": "Nothing to save yet: a blockage and at least one diverted route are needed."}, status_code=400)
    try:
        st, en = dv2_period(d.pop("_start", None), d.pop("_end", None))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    b0 = d["blocks"][0]["line"] or [[0.0, 0.0]]
    mid = b0[len(b0) // 2]
    manual = any(x["source"] == "manual" for x in d["routes"])
    cid = bb_insert("INSERT INTO dv2_case(ts, by_name, road, lat, lon, data, followed, from_case, manual, start_ts, end_ts) VALUES (?,?,?,?,?,?,0,?,?,?,?)",
                    (time.time(), _who(request), d["blocks"][0]["road"], mid[0], mid[1], json.dumps(d), d["from_case"], 1 if manual else 0, st, en))
    if not cid:
        return JSONResponse({"error": "The case could not be saved (database unavailable)."}, status_code=503)
    if d["from_case"]:
        bb_sql("UPDATE dv2_case SET followed = followed + 1 WHERE id = ?", (d["from_case"],))
    r = bb_sql("SELECT * FROM dv2_case WHERE id = ?", (cid,), fetch=True)
    return {"ok": True, "case": dv2_case_brief(r[0]) if r else {"id": cid}}


@app.get("/api/dv2/cases")
async def api_dv2_case_list(limit: int = 50):
    dv2_case_db()
    rows = bb_sql("SELECT * FROM dv2_case ORDER BY ts DESC LIMIT ?", (max(1, min(200, int(limit))),), fetch=True) or []
    return {"cases": [dv2_case_brief(r) for r in rows], "saved": bool(BB.get("db_ok"))}


@app.get("/api/dv2/cases/{cid}")
async def api_dv2_case_get(cid: int):
    dv2_case_db()
    r = bb_sql("SELECT * FROM dv2_case WHERE id = ?", (cid,), fetch=True)
    if not r:
        return JSONResponse({"error": "Case not found."}, status_code=404)
    return {"case": dv2_case_brief(r[0], full=True)}


@app.post("/api/dv2/cases/{cid}/delete")
async def api_dv2_case_delete(cid: int):
    dv2_case_db()
    bb_sql("DELETE FROM dv2_case WHERE id = ?", (cid,))
    return {"ok": True}


# ---- V16.27: the diversion's period, live diversions for the TV, history (filter, duplicate, end, export to Excel)
def dv2_period(start, end, default_start=None):
    """-> (start_ts, end_ts). No start: default_start (None = a case without a period). End is optional ("until further notice")."""
    try:
        st, en = dv2_when(start), dv2_when(end)
    except (TypeError, ValueError):
        raise ValueError("The start or end time is not a valid date and time.")
    st = st if st is not None else default_start
    if en is not None and st is None:
        st = time.time()
    if en is not None and en <= st:
        raise ValueError("The end time must be after the start time.")
    return st, en


def dv2_case_row(cid):
    r = bb_sql("SELECT * FROM dv2_case WHERE id = ?", (cid,), fetch=True)
    return r[0] if r else None


@app.get("/api/dv2/live")
async def api_dv2_live(case: int = 0):
    """what the TV shows: every diversion whose period covers now (start <= now < end, or no end yet), oldest first, and the ones
    starting in the next 24 h. case=ID: that one record only (preview on the TV, whatever its period)."""
    dv2_case_db()
    now = time.time()
    if case:
        r = dv2_case_row(case)
        return {"ok": True, "now": now, "preview": True, "live": [dv2_case_brief(r, full=True)] if r else [], "upcoming": []}
    rows = bb_sql("SELECT * FROM dv2_case WHERE start_ts IS NOT NULL AND start_ts <= ? AND (end_ts IS NULL OR end_ts > ?) ORDER BY start_ts",
                  (now + 24 * 3600.0, now), fetch=True) or []
    return {"ok": True, "now": now, "preview": False, "saved": bool(BB.get("db_ok")),
            "live": [dv2_case_brief(r, full=True) for r in rows if r["start_ts"] <= now][:12],
            "upcoming": [dv2_case_brief(r) for r in rows if r["start_ts"] > now][:12]}


def dv2_history_rows(frm="", to="", road="", service="", state="", limit=300):
    """saved diversions, newest first, whose period overlaps [frm, to] (dates, Singapore time), on a road / for a service"""
    now = time.time()
    try:
        lo = datetime.fromisoformat(frm).replace(tzinfo=SGT).timestamp() if frm else None
        hi = (datetime.fromisoformat(to).replace(tzinfo=SGT) + timedelta(days=1)).timestamp() if to else None
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD.")
    rows = bb_sql("SELECT * FROM dv2_case ORDER BY COALESCE(start_ts, ts) DESC LIMIT 3000", fetch=True) or []
    q, sv = road.strip().lower(), service.strip().upper()
    out = []
    for r in rows:
        a = r.get("start_ts") or r["ts"]
        b = r.get("end_ts") or (max(now, a) if dv2_state(r, now) in ("live", "upcoming") else a)
        if (lo is not None and b < lo) or (hi is not None and a >= hi):
            continue
        if state and dv2_state(r, now) != state:
            continue
        if q or sv:
            try:
                d = json.loads(r["data"] or "{}")
            except ValueError:
                d = {}
            if q and q not in " ".join([r["road"] or ""] + [b_.get("road", "") for b_ in d.get("blocks", [])]
                                       + [x.get("text", "") + " " + " ".join(x.get("roads", [])) for x in d.get("routes", [])]).lower():
                continue
            if sv and sv not in [str(x.get("service", "")).upper() for x in d.get("services", [])]:
                continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


@app.get("/api/dv2/history")
async def api_dv2_history(frm: str = Query("", alias="from"), to: str = "", road: str = "", service: str = "", state: str = "", limit: int = 300):
    dv2_case_db()
    try:
        rows = dv2_history_rows(frm, to, road, service, state, max(1, min(1000, int(limit))))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"cases": [dv2_case_brief(r) for r in rows], "saved": bool(BB.get("db_ok")), "now": time.time()}


@app.post("/api/dv2/cases/{cid}/duplicate")
async def api_dv2_case_duplicate(cid: int, request: Request):
    """use a past diversion again: the same blockage, routes and message as a NEW record with its own start / end time"""
    dv2_case_db()
    body = _dv_body(await request.body())
    r = dv2_case_row(cid)
    if not r:
        return JSONResponse({"error": "Case not found."}, status_code=404)
    try:
        st, en = dv2_period(body.get("start"), body.get("end"), default_start=time.time())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        d = json.loads(r["data"] or "{}")
    except ValueError:
        d = {}
    d["from_case"] = cid
    if isinstance(body.get("note"), str):
        d["note"] = body["note"][:1000]
    nid = bb_insert("INSERT INTO dv2_case(ts, by_name, road, lat, lon, data, followed, from_case, manual, start_ts, end_ts) VALUES (?,?,?,?,?,?,0,?,?,?,?)",
                    (time.time(), _who(request), r["road"], r["lat"], r["lon"], json.dumps(d), cid, r["manual"], st, en))
    if not nid:
        return JSONResponse({"error": "Could not save the copy (database unavailable)."}, status_code=503)
    bb_sql("UPDATE dv2_case SET followed = followed + 1 WHERE id = ?", (cid,))      # used again: ranks higher next time
    return {"ok": True, "case": dv2_case_brief(dv2_case_row(nid))}


@app.post("/api/dv2/cases/{cid}/times")
async def api_dv2_case_times(cid: int, request: Request):
    """change the start / end time of a record; {"end_now": true} ends a live diversion now (an upcoming one is called off)"""
    dv2_case_db()
    body = _dv_body(await request.body())
    r = dv2_case_row(cid)
    if not r:
        return JSONResponse({"error": "Case not found."}, status_code=404)
    now = time.time()
    if body.get("end_now"):
        en = now
        st = min(r.get("start_ts") or r["ts"], now - 1.0)                # an upcoming one is called off: it never runs
    else:
        try:
            st, en = dv2_period(body.get("start", r.get("start_ts")), body.get("end") if "end" in body else r.get("end_ts"), default_start=r.get("start_ts") or r["ts"])
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    bb_sql("UPDATE dv2_case SET start_ts = ?, end_ts = ? WHERE id = ?", (st, en, cid))
    return {"ok": True, "case": dv2_case_brief(dv2_case_row(cid))}


DV2_STATE_TXT = {"live": "LIVE", "upcoming": "Upcoming", "ended": "Ended", "saved": "No period"}


def dv2_history_xlsx(rows):
    """Excel: one row per diverted route, one per service, and the message of each record"""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    head_f, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0B1A2E")
    when = lambda ts: datetime.fromtimestamp(ts, SGT).replace(tzinfo=None) if ts else None

    def sheet(ws, title, cols, data, widths):
        ws.title = title
        ws.append(cols)
        for c_ in ws[1]:
            c_.font, c_.fill, c_.alignment = head_f, head_fill, Alignment(vertical="center", wrap_text=True)
        for row in data:
            ws.append(row)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=2):
            for c_ in row:
                if isinstance(c_.value, datetime):
                    c_.number_format = "dd mmm yyyy hh:mm"
                c_.alignment = Alignment(vertical="top", wrap_text=isinstance(c_.value, str) and len(c_.value) > 40)
        ws.freeze_panes = "A2"
        if data:
            ws.auto_filter.ref = ws.dimensions

    R, SV, M = [], [], []
    for r in rows:
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            d = {}
        b = dv2_case_brief(r, data=d)
        st0, en0, stt = when(r.get("start_ts")), when(r.get("end_ts")), DV2_STATE_TXT.get(b["state"], b["state"])
        for x in d.get("routes", []):
            sk = x.get("skipped") or []
            try:
                mil = float(x.get("mileage") or "")
            except ValueError:
                mil = x.get("mileage") or None
            R.append([r["id"], stt, st0, en0 if en0 else ("Until further notice" if b["state"] in ("live", "upcoming") else None), b["blocked"], b["dir_txt"],
                      x.get("n"), ", ".join(x.get("services") or []), x.get("text", ""), len(sk), ", ".join("BS " + s_["code"] for s_ in sk),
                      x.get("km") or None, len([t for t in str(x.get("runs") or "").splitlines() if t.strip()]) or None, str(x.get("runs") or "").strip() or None,
                      mil, {"manual": "Drawn by OCC", "case": "Saved case", "ai": "AI review", "alternative": "Alternative", "algorithm": "Planner"}.get(x.get("source"), x.get("source") or ""),
                      b["note"], b["by"], when(r["ts"])])
        rt = {x.get("n"): x for x in d.get("routes", [])}
        for s_ in d.get("services", []):
            x = rt.get(s_.get("route")) or {}
            SV.append([r["id"], stt, st0, en0, b["blocked"], s_.get("service"), s_.get("direction"), s_.get("route"), s_.get("leave"), s_.get("rejoin"),
                       x.get("text", ""), {"manual": "Drawn by OCC", "case": "Saved case"}.get(s_.get("source"), s_.get("source") or "")])
        M.append([r["id"], stt, st0, en0, b["blocked"], d.get("text", "")])
    wb_ws = wb.active
    sheet(wb_ws, "Diversions", ["Case", "Status", "Start", "End", "Blocked road", "Direction", "Route", "Services", "Diversion route", "Stops skipped",
                                "Skipped stops", "Diversion km", "Runs", "Diverted runs", "Mileage salvaged (km)", "Route by", "Note", "Saved by", "Saved at"],
          R, [7, 10, 17, 17, 24, 13, 7, 18, 60, 8, 30, 10, 6, 34, 12, 13, 30, 14, 17])
    sheet(wb.create_sheet(), "Services", ["Case", "Status", "Start", "End", "Blocked road", "Service", "Direction", "Route", "Leave stop", "Rejoin stop",
                                          "Diversion route", "Route by"], SV, [7, 10, 17, 17, 24, 9, 9, 7, 11, 11, 60, 13])
    sheet(wb.create_sheet(), "Messages", ["Case", "Status", "Start", "End", "Blocked road", "OCC message"], M, [7, 10, 17, 17, 24, 90])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


@app.get("/api/dv2/history/export")
async def api_dv2_history_export(frm: str = Query("", alias="from"), to: str = "", road: str = "", service: str = "", state: str = ""):
    from fastapi.responses import Response as _Resp
    dv2_case_db()
    try:
        rows = dv2_history_rows(frm, to, road, service, state, 1000)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    data = await asyncio.to_thread(dv2_history_xlsx, rows)
    name = "diversions_" + (frm or "all") + ("_to_" + to if to else "") + ".xlsx"
    return _Resp(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def dv2_case_match(now, old):
    """0..1: how well a saved blockage matches the current one (same stretch of road, same blocked direction)"""
    best = 0.0
    for a in now:
        la = a["line"]
        ca = diversion.cum_m(la)
        ma = diversion.point_at(la, ca, ca[-1] / 2.0)
        da = diversion.densify(la, 20.0)
        for b in old:
            lb = b["line"]
            if len(lb) < 2:
                continue
            cb = diversion.cum_m(lb)
            db = diversion.densify(lb, 20.0)
            ov = max(sum(1 for p in db if diversion.nearest_on_line(p, la, ca)[0] <= 60.0) / len(db),
                     sum(1 for p in da if diversion.nearest_on_line(p, lb, cb)[0] <= 60.0) / len(da))
            if ov < 0.3:
                if diversion.dist_m(ma, diversion.point_at(lb, cb, cb[-1] / 2.0)) > 150.0:
                    continue
                ov = 0.3
            if a["directed"] and b["directed"]:
                ha = diversion.nearest_on_line(ma, la, ca)[3]
                hb = diversion.nearest_on_line(ma, lb, cb)[3]
                if diversion.angdiff(ha, hb) > 60.0:
                    continue
            best = max(best, ov)
    return best


@app.post("/api/dv2/cases/near")
async def api_dv2_case_near(request: Request):
    """saved cases for (about) this blockage, best first: same place and direction, routes the controller drew or edited,
    routes followed again before, and recent ones rank higher (the planner learns from what OCC actually did)"""
    dv2_case_db()
    now = dv2_blocks(_dv_body(await request.body()))
    if not now:
        return {"cases": []}
    mid = now[0]["line"][len(now[0]["line"]) // 2]
    out = []
    for r in bb_sql("SELECT * FROM dv2_case ORDER BY ts DESC LIMIT 500", fetch=True) or []:
        if r["lat"] is None or diversion.dist_m(mid, (r["lat"], r["lon"])) > 3000.0:
            continue
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        old = dv2_blocks({"blocks": d.get("blocks", [])})
        mt = await asyncio.to_thread(dv2_case_match, now, old)
        if mt < 0.3:
            continue
        age = max(0.0, (time.time() - (r["ts"] or 0)) / 86400.0)
        rel = mt * (1.0 + 0.5 * bool(r["manual"]) + 0.25 * min(4, int(r["followed"] or 0))) / (1.0 + age / 120.0)
        c = dv2_case_brief(r, d, full=True)
        c["match"], c["rank"] = round(mt, 2), round(rel, 3)
        out.append(c)
    out.sort(key=lambda c: -c["rank"])
    return {"cases": out[:3]}


# =========================================================================== V16.19 AI-assisted route planning (AI Operational Review)
# Routing engine -> hard validation -> operational scoring -> the AI compares the VERIFIED candidates and recommends one.
# The AI never sees or produces coordinates: it gets candidate letters with measured facts and must answer with one of those
# letters. Its answer is validated server-side; anything invalid, or no AI configured, falls back to the algorithm's ranking.
DV_AI_ROUTE_SYSTEM = """You are an experienced Singapore bus OCC (Operations Control Centre) controller who also trains Bus Captains.
A road is blocked. A routing engine has already produced the candidate diversions below and a validator has already rejected
every unsafe or impossible one (blocked road, U-turn, wrong-way or prohibited movement, restricted road, wrong-direction rejoin,
bus unable to continue to the next original stop). Every candidate you see is physically valid. Your job is to choose the most
operationally practical one - NOT the shortest.

Judge in roughly this priority:
1 safety and feasibility (all given candidates passed; prefer higher bus-road confidence when in doubt)
2 the blocked section is avoided completely (all given candidates do)
3 rejoin the original route as early as reasonably practical
4 minimise skipped bus stops - and consider which stops (an MRT, interchange, hospital or school stop matters more)
5 keep the diversion geographically small (small footprint, little distance off the route)
6 simple for a Bus Captain to follow: few turns, few right turns across traffic, no closely spaced turns
7 fewer turns
8 major / recognisable roads and roads other buses already use, rather than small local streets
9 avoid complicated local street networks, loops and backtracking
10 additional running time, then additional distance.
A slightly longer route with fewer turns on main roads that skips fewer stops and rejoins cleanly usually beats a shorter one
through small streets. Skipping 2 stops is generally better than 6 unless there is a strong operational reason.
OCC also wants every affected service on the same short diversion: a candidate marked same_route_as_other_affected_service
is the route another affected service uses - prefer it when it is about as practical and not longer.

Rules: use ONLY the facts given for each candidate - do not assume anything about roads from your own knowledge, and never
mention a road, stop or turn that is not in the facts. You must choose one of the given candidate letters.
Answer with ONE JSON object and nothing else:
{"recommended_candidate": "<letter>", "confidence": <0..1>, "ranking": ["<letter>", ...],
 "reason": "<2-3 sentences an OCC controller can read out>",
 "operational_advantages": ["<short>", ...], "concerns": ["<short>", ...]}"""


def dv_ai_facts(c):
    """the measured facts of one VERIFIED candidate, in plain operational terms (no coordinates, no scores)"""
    bc = c.get("bc") or {}
    return {"diversion_starts_after_stop": f"{c['leave']['code']} {c['leave']['name']}",
            "rejoins_at_stop": f"{c['rejoin']['code']} {c['rejoin']['name']}",
            "next_original_stop_verified": c["next"]["ok"],
            "roads_in_order": c["roads"], "turn_by_turn": c.get("instruction_text") or [],
            "turns": c["turns"], "right_turns": bc.get("right", 0), "sharp_turns": c.get("sharp", 0),
            "closely_spaced_turns": bc.get("close_turns", 0), "roundabouts": bc.get("roundabouts", 0),
            "road_types": c.get("road_mix", ""), "share_on_roads_no_bus_uses": bc.get("unfamiliar_share"),
            "bus_road_confidence": c.get("busroad_label"), "bus_road_evidence": c.get("evidence_text", ""),
            "stops_skipped": c["skipped_n"], "stops_skipped_inside_blockage": c.get("unavoidable", 0),
            "skipped_stop_names": [s_["name"] for s_ in c.get("skipped", [])][:12],
            "extra_distance_km": c["added_km"], "extra_time_min": c["added_min"], "distance_off_route_km": c["div_km"],
            "leaves_route_m_before_blockage": c.get("leave_before_m", 0), "rejoins_m_after_blockage": max(0, c.get("rejoin_after_m", 0)),
            "diversion_footprint": c.get("footprint_label"), "bus_captain_complexity": c.get("bc_label"),
            "safeguards_exceeded": c.get("warnings") or [], "u_turn": "none (validated)", "earlier_diversion_point": bool(c.get("earlier")),
            "same_route_as_other_affected_service": c.get("shared_from") or None}


def dv_ai_parse(text, letters):
    """validate the AI answer -> (dict, None) or (None, why)"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None, "no JSON object in the answer"
    try:
        j = json.loads(t[a:b + 1])
    except Exception:
        return None, "the answer is not valid JSON"
    rec = str(j.get("recommended_candidate") or "").strip().upper()[:2]
    if rec not in letters:
        return None, f"recommended candidate '{rec}' does not exist"
    try:
        conf = float(j.get("confidence"))
    except (TypeError, ValueError):
        return None, "confidence is not a number"
    if 1.0 < conf <= 100.0:
        conf /= 100.0
    if not 0.0 <= conf <= 1.0:
        return None, "confidence out of range"

    def strs(v, n, ln):
        return [str(x)[:ln] for x in (v or []) if isinstance(x, (str, int, float))][:n] if isinstance(v, list) else []
    rank = [x for x in (str(y).strip().upper()[:2] for y in (j.get("ranking") or []) if isinstance(y, str)) if x in letters]
    return {"recommended_candidate": rec, "confidence": round(conf, 2), "reason": str(j.get("reason") or "")[:800],
            "operational_advantages": strs(j.get("operational_advantages"), 8, 200), "concerns": strs(j.get("concerns"), 8, 200),
            "ranking": list(dict.fromkeys(rank))}, None


@app.post("/api/diversion/ai_route_plan")
async def api_dv_ai_route_plan(request: Request):
    """Routing engine candidates -> AI Operational Review -> recommendation (with the algorithm's ranking as the fallback)."""
    plan = await api_dv2_plan(request)
    if not isinstance(plan, dict):
        return plan
    cands = plan.get("candidates") or []
    best = plan.get("best") or {}
    out = {"ok": True, "routing": {"generated": len(cands), "routes_checked": sum(a_.get("routes", 0) for a_ in plan.get("attempts") or []),
                                   "text": f"Generated {len(cands)} feasible diversion route{'s' if len(cands) != 1 else ''}"},
           "algorithm_choice": best.get("id"), "model": DV_AI_MODEL if DV_AI_KEY else None}
    if not cands:
        out.update(status="none", message="No verified diversion to review.", review={"compared": 0, "text": "Nothing to compare"})
        return out
    # neutral letters (by extra distance, not by the algorithm's ranking) so the review is independent
    order = sorted(cands, key=lambda c: (c["added_km"], c["id"]))
    letters = {chr(65 + i): c["id"] for i, c in enumerate(order[:26])}
    out["letters"] = letters
    out["review"] = {"compared": len(cands), "text": f"Compared {len(cands)} route{'s' if len(cands) != 1 else ''} on OCC and Bus Captain operational considerations"}
    if not DV_AI_KEY:
        out.update(status="fallback", message="AI Operational Review unavailable \u2014 displaying best algorithm-ranked route.", why="not configured (set ANTHROPIC_API_KEY)")
        return out
    packet = {"blockage": [b_.get("road") for b_ in dv2_blocks(_dv_body(await request.body()))],
              "service": plan.get("service"), "direction": plan.get("direction"),
              "candidates": {L_: dv_ai_facts(next(c for c in cands if c["id"] == cid)) for L_, cid in letters.items()}}
    key = "dvai:" + hashlib.sha1(json.dumps([DV_AI_MODEL, packet], sort_keys=True, default=str).encode()).hexdigest()[:24]

    async def factory():
        try:
            r = await client().post("https://api.anthropic.com/v1/messages", timeout=40,
                                    headers={"x-api-key": DV_AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                    json={"model": DV_AI_MODEL, "max_tokens": 900, "temperature": 0, "system": DV_AI_ROUTE_SYSTEM,
                                          "messages": [{"role": "user", "content": "Candidates (JSON):\n" + json.dumps(packet, default=str)[:28000]}]})
            j = r.json()
            if r.status_code != 200:
                return {"error": (j.get("error") or {}).get("message") or f"HTTP {r.status_code}"}, 60, False
            return {"text": "".join(c_.get("text", "") for c_ in j.get("content", []) if c_.get("type") == "text")}, 1800, True
        except Exception as e:
            return {"error": f"AI service unavailable ({type(e).__name__})"}, 60, False
    res = await cached(key, factory)
    if res.get("error"):
        out.update(status="fallback", message="AI Operational Review unavailable \u2014 displaying best algorithm-ranked route.", why=res["error"])
        return out
    ai, why = dv_ai_parse(res.get("text"), letters)
    if not ai:
        out.update(status="fallback", message="AI answer could not be used \u2014 displaying best algorithm-ranked route.", why=why)
        return out
    cid = letters[ai["recommended_candidate"]]
    chosen = next(c for c in cands if c["id"] == cid)
    out.update(status="ai", ai=ai, candidate_id=cid, agrees=cid == best.get("id"),
               algorithm_letter=next((L_ for L_, v in letters.items() if v == best.get("id")), None),
               flagged=("The AI's choice is large and complex for a Bus Captain \u2014 controller review required." if not chosen.get("sensible", True) else
                        "The AI's choice has LOW overall confidence (weak bus-road evidence) \u2014 controller review required." if chosen.get("confidence") == "LOW" else ""))
    return out


# =========================================================================== V17 S.E.N.T.R.Y. 3.0 - Forecast Centre (the home page)
# Data: TomTom incidents & jams (the call the traffic engine already makes - full geometry kept), TomTom Flow probes along watched / selected
# service routes (own daily budget, keeps a reserve of TomTom's shared cap), LTA incidents and road works, NEA rain gauges and the NEA
# 2-hour forecast, the real-road service lines (TR_LINES), running-time history (rt_trip). NO LTA speed bands. The engine is forecast.py
# (pure); this block only collects, stores (SQLite) and serves. Demo mode is a separate, fictional world and is never mixed into live data.
import forecast  # noqa: E402
import numpy as np  # noqa: E402
import statistics  # noqa: E402

FC_DB = os.getenv("FORECAST_DB", "").strip() or BB_DB                     # default: the platform's existing SQLite file
FC_EVERY = max(60, int(os.getenv("FORECAST_REFRESH_SEC", "120") or 120))  # evaluation cycle (s)
FC_PROBE_MIN = max(5, int(os.getenv("FORECAST_PROBE_EVERY_MIN", "15") or 15))
FC_PROBES_PER_ROUTE = max(1, min(12, int(os.getenv("FORECAST_PROBES_PER_ROUTE", "6") or 6)))
FC_PROBE_CAP = int(os.getenv("FORECAST_PROBE_CAP", "200") or 200)          # flow calls a day for forecast probes (200/day = ~6,000 of the 20,000 free Flow Segment Data calls a month)
FC_RESERVE = int(os.getenv("FORECAST_TOMTOM_RESERVE", "800") or 800)       # never take TomTom's shared daily cap below this
FC_ALWAYS = os.getenv("FORECAST_ALWAYS_ON", "").strip().lower() in ("1", "true", "yes")
FC_KEEP_DAYS = int(os.getenv("FORECAST_KEEP_DAYS", "35") or 35)
FC_PROVIDER = (os.getenv("FORECAST_TRAFFIC_PROVIDER", "tomtom") or "tomtom").strip().lower()   # tomtom | here
HERE_KEY = os.getenv("HERE_API_KEY", "").strip()                           # optional second provider (server side only)
FC_SERVICES = [s.strip().upper() for s in (os.getenv("FORECAST_SERVICES") or os.getenv("BUNCHING_SERVICES") or "").split(",") if s.strip()][:8]
FC_TILE_ZOOM = max(11, min(15, int(os.getenv("FORECAST_TILE_ZOOM", "13") or 13)))          # TomTom incident tiles (backup when Incident Details fails)
FC_TILE_EVERY = max(5, int(os.getenv("FORECAST_TILE_EVERY_MIN", "15") or 15))
FC_TILE_CAP = int(os.getenv("FORECAST_TILE_CAP", "3000") or 3000)                         # tile calls a day for the backup (~88 tiles a sweep at zoom 13)
FC_TILE_MODE = (os.getenv("FORECAST_TILE_MODE", "backup") or "backup").strip().lower()      # backup = only when Incident Details fails | always | off
FC = {"store": None, "book": None, "jt": forecast.JamTracker(), "index": None, "index_at": None, "state": None, "world": None, "last": 0.0,
      "last_req": 0.0, "lock": None, "probes": {}, "selected": {}, "budget": {"day": "", "n": 0}, "rain_prev": None, "rain_seen": None,
      "nea2h": None, "nea2h_at": 0.0, "nea2h_err": None, "lta_inc_at": 0.0, "recur": {}, "jam_slot": None, "purged": "", "demo": {},
      "err": None, "cycle_ms": None, "probe_err": None, "provider_ok_at": 0.0,
      "tiles": {"at": 0.0, "ok_at": 0.0, "events": [], "error": None, "day": "", "n": 0, "last_n": 0, "why": ""}}


def fc_store():
    if FC["store"] is None:
        FC["store"] = forecast.Store(FC_DB)
        FC["book"] = forecast.AlertBook(store=FC["store"])
    return FC["store"]


def fc_lock():
    if FC["lock"] is None:
        FC["lock"] = asyncio.Lock()
    return FC["lock"]


def fc_budget_ok():
    day = now_sgt().strftime("%Y-%m-%d")
    if FC["budget"]["day"] != day:
        FC["budget"] = {"day": day, "n": 0}
    left_shared = TT["cap"] - (TT["n"] if TT["day"] == day else 0)
    return FC["budget"]["n"] < FC_PROBE_CAP and left_shared > FC_RESERVE


class FcProvider:
    """Traffic provider abstraction: flow(lat, lon) -> one road segment (speeds km/h + its geometry) or None."""
    name = "none"

    def configured(self):
        return False

    async def flow(self, lat, lon, brg=None):
        return None


class FcTomTom(FcProvider):
    name = "tomtom"

    def configured(self):
        return bool(TT["key"])

    async def flow(self, lat, lon, brg=None):
        if not tt_spend():
            raise RuntimeError("TomTom daily cap reached")
        r = await fc_get("https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json",
                         {"key": TT["key"], "point": f"{lat:.5f},{lon:.5f}", "unit": "KMPH"}, tt_headers())
        return forecast.parse_tomtom_flow(r)


class FcHere(FcProvider):
    name = "here"

    def configured(self):
        return bool(HERE_KEY)

    async def flow(self, lat, lon, brg=None):
        r = await fc_get("https://data.traffic.hereapi.com/v7/flow", {"in": f"circle:{lat:.5f},{lon:.5f};r=60", "locationReferencing": "shape", "apiKey": HERE_KEY})
        segs = forecast.parse_here_flow(r)
        best, bd = None, 1e9
        for s_ in segs:                                                    # the segment nearest the point (and, given a bearing, aligned with it)
            L = forecast.Line(s_["line"])
            if not L.ok:
                continue
            d, _, b = L.locate(forecast.to_xy(lat, lon))
            pen = 0 if brg is None else (0 if forecast.angdiff(float(b[0]), brg) <= 35 else 1000)
            if d[0] + pen < bd:
                best, bd = s_, d[0] + pen
        return best


def fc_provider():
    p = FcHere() if FC_PROVIDER == "here" else FcTomTom()
    return p if p.configured() else (FcTomTom() if FcTomTom().configured() else (FcHere() if FcHere().configured() else FcProvider()))


async def fc_get(url, params, headers=None, tries=3):
    """GET JSON with retry + exponential backoff on 429 / 5xx / network errors."""
    last = None
    for k in range(tries):
        try:
            r = await client().get(url, params=params, headers=headers or {}, timeout=15)
            if r.status_code in (429, 500, 502, 503, 504) and k + 1 < tries:
                await asyncio.sleep(0.5 * (2 ** k))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if k + 1 < tries and not (isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (400, 401, 403, 404)):
                await asyncio.sleep(0.5 * (2 ** k))
                continue
            break
    raise last if last else RuntimeError("request failed")


def fc_src(status, age=None, detail="", label=""):
    return {"status": status, "age_s": None if age is None else int(age), "detail": detail, "label": label}


async def fc_nea2h():
    now = time.time()
    if FC["nea2h"] is not None and now - FC["nea2h_at"] < 600:
        return FC["nea2h"]
    for u in ("https://api-open.data.gov.sg/v2/real-time/api/two-hr-weather-forecast", "https://api.data.gov.sg/v1/environment/2-hour-weather-forecast"):
        try:
            j = await fc_get(u, None, {"x-api-key": DATAGOV_KEY} if DATAGOV_KEY else None, tries=2)
            p = forecast.parse_nea_2h(j)
            if p["areas"]:
                FC.update(nea2h=p, nea2h_at=now, nea2h_err=None)
                return p
            FC["nea2h_err"] = "no areas in response"
        except Exception as e:
            FC["nea2h_err"] = f"{type(e).__name__}"
    return FC["nea2h"]


async def fc_lta_incidents():
    async def factory():                                   # the same cache entry as /api/incidents: one LTA call serves both
        d = await get_lta(EP_INCIDENTS)
        return d, TTL_INCIDENTS, not d.get("_error")
    return await cached("incidents", factory)


async def fc_index():
    """The island-wide route index from the real-road service lines (rebuilt when TR_LINES changes)."""
    try:
        st = await static()
        if st.get("routes"):
            tr_lines_kick(st)
    except Exception:
        pass
    if TR_LINES["n"] and FC["index_at"] != TR_LINES["at"]:
        lines = {k: v["line"] for k, v in TR_LINES["lines"].items()}
        FC["index"] = await asyncio.to_thread(forecast.RouteIndex, lines)
        FC["index_at"] = TR_LINES["at"]
    return FC["index"]


async def fc_route(svc, d):
    """(Line, stops) for one service-direction: the index line when there is one, else the platform's route geometry."""
    st = await static()
    stops = route_stops(st, svc, d) if st.get("stops") else []
    idx = FC["index"]
    if idx is not None and (svc, d) in idx.lines:
        return idx.lines[(svc, d)], stops
    if len(stops) >= 2:
        g = await route_geometry(svc, d, stops)
        L = forecast.Line(g["line"])
        return (L if L.ok else None), stops
    return None, stops


def fc_probe_points(L, n):
    """n points spread along the route, away from the ends (terminals are not representative)."""
    if L is None or not L.ok or L.length < 600:
        return []
    k = max(1, min(n, int(L.length // 2000) or 1))
    out = []
    for i in range(k):
        s_ = L.length * (i + 0.5) / k
        la, lo = L.point_at(s_)
        _, _, b = L.locate(forecast.to_xy(la, lo))
        out.append((round(la, 5), round(lo, 5), float(b[0])))
    return out


async def fc_probe_service(svc, d, now, force=False):
    """Sample the provider along one service route (within the daily budget); stores the readings and the forecasts it issues."""
    prov = fc_provider()
    if prov.name == "none":
        return 0
    L, _ = await fc_route(svc, d)
    n = 0
    for la, lo, brg in fc_probe_points(L, FC_PROBES_PER_ROUTE):
        key = f"{la:.4f},{lo:.4f},{int(((brg + 22.5) % 360) // 45)}"
        last = FC["probes"].get(key)
        if last and now - last["ts"] < FC_PROBE_MIN * 60 and not (force and now - last["ts"] > 300):
            if (svc, d) not in last["svcs"]:
                last["svcs"].append((svc, d))
            continue
        if not fc_budget_ok():
            FC["probe_err"] = "daily probe budget reached (FORECAST_PROBE_CAP / FORECAST_TOMTOM_RESERVE)"
            break
        FC["budget"]["n"] += 1
        try:
            res = await prov.flow(la, lo, brg)
        except Exception as e:
            FC["probe_err"] = f"{prov.name}: " + (tt_err(e) if prov.name == "tomtom" else f"{type(e).__name__}")
            continue
        if not res:
            continue
        FC["provider_ok_at"] = now
        FC["probe_err"] = None
        rec = dict(res, key=key, lat=la, lon=lo, brg=brg, ts=now, svcs=[(svc, d)], road=res.get("road") or "")
        FC["probes"][key] = rec
        stv = fc_store()
        stv.settle(key, now, res["cur"])
        stv.add_probes([(key, now, svc, d, la, lo, res["cur"], res["ff"], res.get("conf"), 1 if res.get("closure") else 0, prov.name)])
        hist = stv.probe_hist(key, now - FC_KEEP_DAYS * 86400, now - 1800)
        series = stv.probe_hist(key, now - 1800)
        b0 = forecast.baseline(hist, now)
        slope, _ = forecast.trend(series, now)
        rows = []
        for h in forecast.PARAMS["horizons"]:
            bh = forecast.baseline(hist, now + h * 60)["kmh"]
            mid, lo_, hi_ = forecast.forecast_speed(res["cur"], res["ff"], b0["kmh"], bh, slope, h, b0.get("sigma"))
            rows.append((now, key, h, now + h * 60, mid, lo_, hi_))
        stv.add_forecasts(rows)
        n += 1
    for k in [k for k, v in FC["probes"].items() if now - v["ts"] > 3 * 3600]:
        FC["probes"].pop(k, None)
    return n


def fc_hist_rt(svc, d, now):
    """Median measured running time (min) for this service-direction at this time of day (same day type, last 60 days)."""
    dt = now_sgt()
    dtype = bb_day_type(dt)
    m0 = dt.hour * 60 + dt.minute
    rows = bb_sql("SELECT start_ts, rt_min FROM rt_trip WHERE service=? AND direction=? AND day_type=? AND start_ts>=? AND cover>=0.8",
                  (svc, d, dtype, now - 60 * 86400), fetch=True) or []
    vals = []
    for r in rows:
        t = datetime.fromtimestamp(r["start_ts"], SGT)
        if abs(t.hour * 60 + t.minute - m0) <= 45 and r["rt_min"]:
            vals.append(float(r["rt_min"]))
    return round(statistics.median(vals), 1) if len(vals) >= 5 else None


async def fc_incident_tiles(now, why):
    """TomTom vector incident tiles over the bus network (tile allowance), every FORECAST_TILE_EVERY_MIN, within FORECAST_TILE_CAP a day."""
    T = FC["tiles"]
    if not TT["key"] or FC_TILE_MODE == "off":
        return T
    if now - T["at"] < FC_TILE_EVERY * 60:
        return T
    day = now_sgt().strftime("%Y-%m-%d")
    if T["day"] != day:
        T["day"], T["n"] = day, 0
    idx = FC["index"]
    if idx is not None and len(idx):
        lo = np.min([L.lo for L in idx.lines.values()], 0)
        hi = np.max([L.hi for L in idx.lines.values()], 0)
        south, west, north, east = lo[1] / forecast.KY, lo[0] / forecast.KX, hi[1] / forecast.KY, hi[0] / forecast.KX
    else:
        south, west, north, east = 1.22, 103.6, 1.47, 104.05
    want = [t for t in forecast.tiles_for_bbox(FC_TILE_ZOOM, south - .005, west - .005, north + .005, east + .005) if _tile_over_sg(*t)]
    if T["n"] + len(want) > FC_TILE_CAP:
        T["error"] = f"daily tile budget reached ({FC_TILE_CAP}, FORECAST_TILE_CAP)"
        return T
    T["at"], T["why"] = now, why
    sem_ = asyncio.Semaphore(6)
    got, errs = [], []

    async def one(t):
        z, x, y = t
        async with sem_:
            try:
                TT_TILE["n"] = TT_TILE.get("n", 0) + 1
                T["n"] += 1
                r = await client().get(f"https://api.tomtom.com/traffic/map/4/tile/incidents/{z}/{x}/{y}.pbf", timeout=15, headers=tt_headers(),
                                       params={"key": TT["key"], "t": "-1"})
                if r.status_code == 204:
                    return
                r.raise_for_status()
                if r.content:
                    got.append((z, x, y, r.content))
            except Exception as e:
                errs.append(tt_err(e))
    await asyncio.gather(*[one(t) for t in want])
    if got or not errs:
        T.update(events=await asyncio.to_thread(forecast.parse_tomtom_incident_tiles, got, now), ok_at=now, error=None, last_n=len(want))
    else:
        T["error"] = "incident tiles: " + errs[0]
    return T


async def fc_world(now):
    """Collect everything the engine needs (live). Each source is marked live / stale / unavailable / off: nothing is ever filled in."""
    stv = fc_store()
    src = {}
    idx = await fc_index()
    src["route_index"] = fc_src("live" if idx is not None and len(idx) else "unavailable", (now - TR_LINES["at"]) if TR_LINES["at"] else None,
                                f"{len(idx)} service-direction lines (busrouter.sg, fitted to LTA stops)" if idx is not None else (TR_LINES.get("info") or "not built yet"),
                                "Route geometry")
    events = []
    # TomTom incidents & jams (shared call with the traffic engine)
    if TT["key"]:
        try:
            await tomtom_incidents()
        except Exception:
            pass
        raw, at = TT.get("raw"), TT.get("raw_at") or 0
        fresh_raw = raw is not None and now - at <= 2 * TT_INC_TTL + 120
        if raw is not None:
            age = now - at
            src["tomtom_incidents"] = fc_src("live" if fresh_raw else "stale", age, TT.get("error") or f"{len(raw)} incidents incl. jams", "TomTom incidents & jams")
        else:
            src["tomtom_incidents"] = fc_src("unavailable", None, TT.get("error") or "no response yet", "TomTom incidents & jams")
        use_tiles = FC_TILE_MODE == "always" or (FC_TILE_MODE == "backup" and not fresh_raw)
        if use_tiles:
            T = await fc_incident_tiles(now, "always on" if FC_TILE_MODE == "always" else "Incident Details unavailable")
            ta = (now - T["ok_at"]) if T["ok_at"] else None
            src["tomtom_tiles"] = fc_src("live" if ta is not None and ta <= FC_TILE_EVERY * 120 else ("stale" if ta is not None else "unavailable"), ta,
                                         T["error"] or f"backup in use ({T['why']}): {len(T['events'])} incidents incl. jams from {T['last_n']} tiles; {T['n']}/{FC_TILE_CAP} tiles today",
                                         "TomTom incident tiles (backup)")
        else:
            src["tomtom_tiles"] = fc_src("idle", None, "standby: used automatically when TomTom Incident Details fails", "TomTom incident tiles (backup)")
        if fresh_raw:
            events += forecast.parse_tomtom_incidents({"incidents": raw}, now)
        elif use_tiles and FC["tiles"]["ok_at"]:
            events += list(FC["tiles"]["events"])                               # the backup: same incidents and jams, from the tiles
        elif raw is not None:
            events += forecast.parse_tomtom_incidents({"incidents": raw}, now)  # last known, marked stale
    else:
        src["tomtom_incidents"] = fc_src("off", None, "TOMTOM_API_KEY not set", "TomTom incidents & jams")
        src["tomtom_tiles"] = fc_src("off", None, "TOMTOM_API_KEY not set", "TomTom incident tiles (backup)")
    if WAZE["url"]:
        try:
            await waze_state()
        except Exception:
            pass
        wa = (now - WAZE["ok_at"]) if WAZE["ok_at"] else None
        src["waze"] = fc_src("live" if waze_live() else ("stale" if wa is not None else "unavailable"), wa, WAZE["error"] or f"{len(WAZE['jams'])} jams, {len(WAZE['alerts'])} reports", "Waze for Cities")
        if WAZE["jams"] or WAZE["alerts"]:
            events += forecast.waze_events(WAZE["jams"], WAZE["alerts"], now)
    # LTA incidents
    if KEY:
        raw = await fc_lta_incidents()
        age = cache_age("incidents")
        if raw.get("value") or not raw.get("_error"):
            events += forecast.parse_lta_incidents(raw.get("value") or [], now)
            src["lta_incidents"] = fc_src("live" if (age or 0) <= 2 * TTL_INCIDENTS + 60 else "stale", age, raw.get("_error") or f"{len(raw.get('value') or [])} incidents (point only)", "LTA incidents")
        else:
            src["lta_incidents"] = fc_src("unavailable", age, raw.get("_error"), "LTA incidents")
        items, err = await tr_roadworks(now)
        age = cache_age("roadworks")
        events += forecast.normalise_roadworks(items, now)
        src["lta_roadworks"] = fc_src("unavailable" if err and not items else ("live" if (age or 0) <= 2 * TTL_ROADWORKS + 60 else "stale"), age,
                                      err or f"{len(items)} in force or starting within 2 h", "LTA road works")
    else:
        src["lta_incidents"] = fc_src("off", None, "LTA_ACCOUNT_KEY not set", "LTA incidents")
        src["lta_roadworks"] = fc_src("off", None, "LTA_ACCOUNT_KEY not set", "LTA road works")
    # NEA rainfall (all gauges, zero readings included) + the previous reading set for the trend
    await api_rain()
    hit = CACHE.get("rain")
    stations = (hit[2].get("stations") if hit else None) or []
    if hit and stations:
        if FC["rain_seen"] != hit[1]:
            FC["rain_prev"], FC["rain_seen"] = FC.get("rain_cur"), hit[1]
            FC["rain_cur"] = stations
        age = now - hit[1]
        src["nea_rain"] = fc_src("live" if age <= 2 * TTL_RAIN + 120 else "stale", age, f"{len(stations)} gauges, {sum(1 for s_ in stations if s_['mm'] > 0)} wet", "NEA rainfall")
    else:
        src["nea_rain"] = fc_src("unavailable", None, (hit[2].get("error") if hit else None) or "no gauges", "NEA rainfall")
    nea = await fc_nea2h()
    src["nea_forecast"] = fc_src("live" if nea and now - FC["nea2h_at"] < 1800 else ("stale" if nea else "unavailable"), (now - FC["nea2h_at"]) if nea else None,
                                 FC["nea2h_err"] or (f"{len(nea['areas'])} areas" if nea else ""), "NEA 2-h forecast")
    # probes on the watched + recently selected services
    sel = [k for k, t in FC["selected"].items() if now - t < 900][:3]
    want = []
    if FC_SERVICES:
        st = await static()
        for svc in FC_SERVICES:
            for d in st.get("dirs", {}).get(svc, []):
                want.append((svc, d))
    for k in sel:
        if k not in want:
            want.append(k)
    prov = fc_provider()
    for svc, d in want[:12]:
        try:
            await fc_probe_service(svc, d, now)
        except Exception as e:
            FC["probe_err"] = f"{type(e).__name__}"
    probes = []
    for key, p in FC["probes"].items():
        for (svc, d) in p["svcs"]:
            probes.append({"key": key, "svc": svc, "dir": d, "lat": p["lat"], "lon": p["lon"], "road": p.get("road") or "", "cur": p["cur"], "ff": p["ff"],
                           "conf": p.get("conf"), "closure": p.get("closure"), "line": p.get("line"), "ts": p["ts"], "provider": p.get("provider"),
                           "series": stv.probe_hist(key, now - 1800), "hist": stv.probe_hist(key, now - FC_KEEP_DAYS * 86400, now - 1800)})
    pa = (now - FC["provider_ok_at"]) if FC["provider_ok_at"] else None
    src["tomtom_flow"] = fc_src("off" if prov.name == "none" else ("live" if pa is not None and pa <= FC_PROBE_MIN * 120 else ("stale" if pa is not None else ("unavailable" if FC["probe_err"] else "idle"))),
                                pa, FC["probe_err"] or (f"{prov.name}: {len(FC['probes'])} probe points, {FC['budget']['n']}/{FC_PROBE_CAP} calls today" if prov.name != "none" else "no traffic provider key"),
                                f"{'HERE' if prov.name == 'here' else 'TomTom'} flow probes")
    # jams: build-up rate, recurrence for the time of day, snapshots every 10 min for future recurrence statistics
    FC["jt"].update(events, now)
    stv.cover(now)
    slot = int(now // 600)
    jrows, info = [], {}
    hol = set()
    for ev in events:
        if ev.get("delay_s") is None and ev["kind"] != "jam":
            continue
        rate, _ = FC["jt"].rate(ev["id"])
        cell = forecast.jam_cell(ev) if ev["kind"] == "jam" else None
        rec = None
        if cell:
            ck = (cell, forecast.slot15(now))
            hit_ = FC["recur"].get(ck)
            if not hit_ or now - hit_[0] > 600:
                hit_ = (now, stv.jam_recurrence(cell, now, hol))
                FC["recur"][ck] = hit_
            rec = hit_[1]
            if FC["jam_slot"] != slot:
                jrows.append((now, ev["id"], cell, ev.get("mag"), ev.get("delay_s"), ev.get("length_m"), ev.get("road") or ""))
        info[ev["id"]] = {"rate": rate, "recurring": rec["recurring"] if rec else None, "typical_s": rec["typical_s"] if rec else None,
                          "detail": rec["detail"] if rec else None, "first_seen": FC["jt"].first_seen(ev["id"])}
    if jrows:
        stv.add_jams(jrows)
    FC["jam_slot"] = slot
    if len(FC["recur"]) > 3000:
        FC["recur"].clear()
    # headways and running-time history for the services that matter
    fq = await freq_table()
    keys = set(want)
    world = {"demo": False, "now": now, "index": idx, "routes": {}, "events": events, "probes": probes,
             "rain": {"stations": stations, "prev": FC.get("rain_prev"), "ts": hit[1] if hit else None}, "nea_2h": nea or {}, "sources": src,
             "headways": {}, "hist_rt": {}, "stops": {}, "focus": list(keys), "holidays": (), "jam_info": info,
             "calibration": stv.calibration(now)}
    return world, fq


async def fc_cycle(force=False):
    """One evaluation: collect, evaluate (in a thread), update the alert book. Never runs twice at once."""
    now = time.time()
    if not force and FC["state"] is not None and now - FC["last"] < FC_EVERY - 5:
        return FC["state"]
    async with fc_lock():
        if not force and FC["state"] is not None and time.time() - FC["last"] < FC_EVERY - 5:
            return FC["state"]
        t0 = time.time()
        try:
            world, fq = await fc_world(now)
            state = await asyncio.to_thread(forecast.evaluate, world)
            keys = set(state["impacts"].keys())
            dtn = now_sgt()
            route_static = await static()
            for k in keys:
                world["stops"][k] = route_stops(route_static, k[0], k[1])
            for k in keys:                                                   # headways + running-time history, then re-evaluate those services
                try:
                    hw_, _ = bb_resolve_hw(k[0], k[1], dtn, fq)
                except Exception:
                    hw_ = None
                if hw_:
                    world["headways"][k] = hw_
                rt_ = await asyncio.to_thread(fc_hist_rt, k[0], k[1], now)
                if rt_:
                    world["hist_rt"][k] = rt_
            if world["headways"] or world["hist_rt"] or world["stops"]:
                state = await asyncio.to_thread(forecast.evaluate, world)
            fresh = {n: v["status"] == "live" for n, v in world["sources"].items()}
            fc_store()
            FC["book"].update(state["cands"], now, fresh)
            state["kpis"].append(forecast.critical_kpi(FC["book"]))
            FC.update(state=state, world=world, last=now, err=None, cycle_ms=round((time.time() - t0) * 1000))
            if FC["purged"] != now_sgt().strftime("%Y-%m-%d"):
                FC["purged"] = now_sgt().strftime("%Y-%m-%d")
                await asyncio.to_thread(FC["store"].purge, now, FC_KEEP_DAYS)
        except Exception as e:
            FC["err"] = f"{type(e).__name__}: {str(e)[:200]}"
        return FC["state"]


async def fc_loop():
    await asyncio.sleep(20)
    while True:
        try:
            if FC_ALWAYS or time.time() - FC["last_req"] < 600:
                await fc_cycle()
        except Exception as e:
            FC["err"] = f"{type(e).__name__}: {str(e)[:200]}"
        await asyncio.sleep(FC_EVERY)


def fc_demo(step):
    step = max(0, min(len(forecast.DEMO_STEPS) - 1, int(step or 0)))
    if step not in FC["demo"]:
        FC["demo"][step] = forecast.demo_state(step)
    return FC["demo"][step]


def fc_payload(state, book_view, counts, audit, extra=None):
    out = forecast.public_state(state)
    out.update(alerts=book_view, alert_counts=counts, audit=audit)
    if extra:
        out.update(extra)
    return out


def fc_config():
    prov = fc_provider()
    return {"provider": prov.name, "tomtom_key": bool(TT["key"]), "here_key": bool(HERE_KEY), "lta_key": bool(KEY), "probe_cap": FC_PROBE_CAP,
            "probe_used": FC["budget"]["n"] if FC["budget"]["day"] == now_sgt().strftime("%Y-%m-%d") else 0, "tomtom_reserve": FC_RESERVE,
            "tomtom_shared": {"used": TT["n"] if TT["day"] == now_sgt().strftime("%Y-%m-%d") else 0, "cap": TT["cap"]},
            "watched": FC_SERVICES, "refresh_s": FC_EVERY, "always_on": FC_ALWAYS, "store": {"ok": FC["store"].ok if FC["store"] else None,
            "schema": FC["store"].version if FC["store"] else None, "err": FC["store"].err if FC["store"] else None,
            "history_days": FC["store"].history_days() if FC["store"] else 0}, "speed_bands_used": False,
            "httpx_stub": HTTPX_STUB, "problem": HTTPX_PROBLEM if HTTPX_STUB else None,
            "tiles": {"mode": FC_TILE_MODE, "zoom": FC_TILE_ZOOM, "every_min": FC_TILE_EVERY, "cap": FC_TILE_CAP, "used": FC["tiles"]["n"] if FC["tiles"]["day"] == now_sgt().strftime("%Y-%m-%d") else 0},
            "waze": bool(WAZE["url"])}


FC_VT = {"cache": {}}


@app.get("/api/tomtom/vflow/{z}/{x}/{y}.pbf")
async def api_tomtom_vflow(z: int, x: int, y: int):
    """TomTom Traffic Flow VECTOR tiles (relative speed 0-1 per road line), proxied so the key stays on the server; the Forecast Centre
    colours them itself. Same daily tile counter as the raster flow tiles."""
    from fastapi.responses import Response
    empty = Response(b"", media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=60"})
    if not TT["key"] or z < 9 or z > 18 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z) or not _tile_over_sg(z, x, y):
        return empty
    now = time.time()
    hit = FC_VT["cache"].get((z, x, y))
    if hit and now - hit[0] < TT_TILE_TTL:
        return Response(hit[1], media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=60"})
    day = now_sgt().strftime("%Y-%m-%d")
    if TT_TILE["day"] != day:
        TT_TILE["day"], TT_TILE["n"] = day, 0
    if TT_TILE["n"] >= TT_TILE["cap"]:
        TT_TILE["error"] = "daily tile cap reached"
        return empty
    TT_TILE["n"] += 1
    try:
        r = await client().get(f"https://api.tomtom.com/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.pbf", timeout=15, headers=tt_headers(), params={"key": TT["key"]})
        if r.status_code == 204:
            return empty
        r.raise_for_status()
        body = r.content
        if body[:2] == b"\x1f\x8b":
            import gzip as _gz
            body = _gz.decompress(body)
        FC_VT["cache"][(z, x, y)] = (now, body)
        if len(FC_VT["cache"]) > 600:
            for k in sorted(FC_VT["cache"], key=lambda k: FC_VT["cache"][k][0])[:200]:
                FC_VT["cache"].pop(k, None)
        TT_TILE.update(ok_at=now, error=None)
        return Response(body, media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=60"})
    except Exception as e:
        TT_TILE["error"] = "vector flow tiles: " + tt_err(e)
        return empty


@app.get("/api/forecast/state")
async def api_forecast_state(mode: str = "live", step: int = 0):
    if mode == "demo":
        state, world = fc_demo(step)
        return fc_payload(state, state["alerts"], state["alert_counts"], state["audit"],
                          {"demo": state["demo"], "recovery": state.get("recovery"), "config": {"provider": "demo", "speed_bands_used": False}})
    FC["last_req"] = time.time()
    state = await fc_cycle()
    fc_store()
    if state is None:
        return {"mode": "live", "error": FC["err"] or "The Forecast Centre is starting: no evaluation yet.", "kpis": [], "alerts": [], "events": [],
                "config": fc_config(), "time": now_sgt().isoformat(timespec="seconds")}
    now = time.time()
    return fc_payload(state, FC["book"].view(now, history=True), FC["book"].counts(), FC["store"].audit_list(40),
                      {"config": fc_config(), "age_s": round(now - FC["last"]), "cycle_ms": FC["cycle_ms"], "error": FC["err"]})


@app.get("/api/forecast/service")
async def api_forecast_service(svc: str = "", dir: int = 1, mode: str = "live", step: int = 0):
    svc = svc.strip().upper()
    if not svc:
        return JSONResponse({"error": "Pick a service."}, status_code=400)
    key = (svc, int(dir))
    if mode == "demo":
        state, world = fc_demo(step)
        v = forecast.service_view(state, world, key)
        if state.get("recovery") and state["recovery"]["svc"] == svc and state["recovery"]["dir"] == int(dir):
            v["recovery"] = state["recovery"]
        return v
    FC["last_req"] = time.time()
    FC["selected"][key] = time.time()
    state = await fc_cycle()
    if state is None:
        return JSONResponse({"error": FC["err"] or "No evaluation yet."}, status_code=503)
    world = FC["world"]
    L, stops = await fc_route(svc, key[1])
    if L is None:
        return {"svc": svc, "dir": key[1], "error": f"No route geometry for service {svc} direction {key[1]}."}
    if world["index"] is None or key not in world["index"].lines:
        world["routes"][key] = L.pts
    world["stops"][key] = stops
    n = await fc_probe_service(svc, key[1], time.time(), force=True)          # on demand, within the budget
    if n:
        p = FC["probes"]
        world["probes"] = [x for x in world["probes"] if (x["svc"], x["dir"]) != key] + [
            {"key": k, "svc": svc, "dir": key[1], "lat": v["lat"], "lon": v["lon"], "road": v.get("road") or "", "cur": v["cur"], "ff": v["ff"], "conf": v.get("conf"),
             "closure": v.get("closure"), "line": v.get("line"), "ts": v["ts"], "provider": v.get("provider"), "series": FC["store"].probe_hist(k, time.time() - 1800),
             "hist": FC["store"].probe_hist(k, time.time() - FC_KEEP_DAYS * 86400, time.time() - 1800)} for k, v in p.items() if key in v["svcs"]]
    try:
        hw_, _ = bb_resolve_hw(svc, key[1], now_sgt(), await freq_table())
        if hw_:
            world["headways"][key] = hw_
    except Exception:
        pass
    rt_ = await asyncio.to_thread(fc_hist_rt, svc, key[1], time.time())
    if rt_:
        world["hist_rt"][key] = rt_
    await asyncio.to_thread(forecast.recompute_service, state, world, key)
    v = await asyncio.to_thread(forecast.service_view, state, world, key)
    im = state["impacts"].get(key)
    if im and im["extra"][30]["mid"] >= 2:
        v["recovery"] = forecast.recovery_options(svc, key[1], im["extra"][30]["mid"], im["hw"])
    st = await static()
    v["directions"] = st.get("dirs", {}).get(svc, [])
    return v


@app.get("/api/forecast/bunching")
async def api_forecast_bunching(services: str = ""):
    """Watch requested services through the existing collector; never fabricate missing samples."""
    now = time.time()
    requested = list(dict.fromkeys(s for s in re.split(r"[,\s]+", services.upper()) if re.fullmatch(r"[0-9A-Z]{1,6}", s)))
    if requested:
        if now - BB.get("last_req", 0) >= BB_IDLE_SEC:
            BB["wake_at"] = now
        BB["last_req"] = now
        for service in requested[:max(1, BB_MAX_PAIRS // 2)]:
            for direction in (1, 2):
                BB["watch"][(service, direction)] = now
    max_age = 2 * BB["params"].get("refresh_sec", 60)
    rows = []
    for sample in list(BB["rows"].values()):
        observed = sample.get("at")
        age = max(0, now - observed) if observed else None
        count = (sample.get("counts") or {}).get("bb3")
        available = (age is not None and age <= max_age and not sample.get("error")
                     and sample.get("issue") != "No data" and isinstance(count, (int, float)))
        loads = {"SEA": 0, "SDA": 0, "LSD": 0}
        if available:
            for bus in sample.get("seq") or []:
                load = str(bus.get("load") or "").upper()
                if load in loads:
                    loads[load] += 1
        level = "LSD" if loads["LSD"] else "SDA" if loads["SDA"] else "SEA" if loads["SEA"] else None
        rows.append({"svc": str(sample.get("service") or "").upper(), "dir": sample.get("direction"),
                     "groups3": count if available else None, "available": bool(available), "age_s": round(age) if age is not None else None,
                     "load_level": level, "load_counts": loads, "load_sampled": sum(loads.values())})
    return {"rows": rows, "source": "bunching collector", "updated": now_sgt().isoformat(timespec="seconds")}


@app.get("/api/forecast/services")
async def api_forecast_services(mode: str = "live"):
    if mode == "demo":
        return {"services": [{"svc": s, "dirs": [1, 2] if s == "X101" else [1]} for s in ("X101", "X102", "X201", "X305", "X410")], "demo": True}
    st = await static()
    dirs = st.get("dirs") or {}

    def keyf(s_):
        m = re.match(r"(\d+)(.*)", s_)
        return (0, int(m.group(1)), m.group(2)) if m else (1, 0, s_)
    opmap = await occ_operator_map()
    return {"services": [{"svc": s_, "dirs": dirs[s_], "operator": opmap.get(s_, "")} for s_ in sorted(dirs, key=keyf)], "error": st.get("error")}


@app.post("/api/forecast/alerts/{aid}/ack")
async def api_forecast_ack(aid: str, request: Request):
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError:
        body = {}
    if body.get("mode") == "demo":
        return {"ok": True, "demo": True, "note": "Demo acknowledgement: shown on this screen only, not stored."}
    fc_store()
    s_ = auth.read(request.cookies.get(auth.COOKIE, "")) if hasattr(auth, "read") else None
    u_ = s_.get("u") if s_ else None
    who = ((u_.get("name") or u_.get("id")) if isinstance(u_, dict) else (str(u_) if u_ else None)) or str(body.get("by") or "").strip()[:60] or "controller"
    a = FC["book"].ack(aid, who, time.time(), str(body.get("note") or ""))
    if a is None:
        return JSONResponse({"error": "Unknown alert."}, status_code=404)
    return {"ok": True, "alert": a, "counts": FC["book"].counts()}


@app.get("/api/forecast/audit")
async def api_forecast_audit(alert: str = "", limit: int = 200):
    fc_store()
    return {"audit": FC["store"].audit_list(max(1, min(1000, limit)), alert or None)}


@app.get("/api/forecast/recovery")
async def api_forecast_recovery(svc: str = "", dir: int = 1, extra: float = 0.0, hw: float = 0.0, layover: float = -1):
    return forecast.recovery_options(svc.strip().upper(), int(dir), extra, hw or None, None if layover < 0 else layover)


@app.get("/api/forecast/health")
async def api_forecast_health():
    fc_store()
    w = FC.get("world") or {}
    return {"engine": forecast.ENGINE, "method": forecast.METHOD, "params": forecast.PARAMS, "sources": w.get("sources") or {},
            "last": datetime.fromtimestamp(FC["last"], SGT).isoformat(timespec="seconds") if FC["last"] else None, "error": FC["err"],
            "cycle_ms": FC["cycle_ms"], "config": fc_config(), "calibration": FC["store"].calibration(time.time()),
            "index": len(FC["index"]) if FC["index"] is not None else 0, "probes": len(FC["probes"])}

# S.E.N.T.R.Y 2026 standalone command-centre adapter. Existing provider collectors
# and strict geometry/direction matching are reused without changing their API.
import forecast2026 as fc26
FC26_LEDGER = None


def fc26_ledger():
    global FC26_LEDGER
    if FC26_LEDGER is None:
        FC26_LEDGER = fc26.AlertLedger(FC_DB)
    return FC26_LEDGER


@app.get('/forecast2026.css')
async def fc26_styles():
    from fastapi.responses import Response
    return Response((HERE / 'forecast2026.css').read_text(), media_type='text/css', headers={'Cache-Control':'no-cache'})


@app.get('/forecast2026.js')
@app.get('/forecast2026-core.js')
@app.get('/forecast2026-map.js')
@app.get('/forecast2026-tv.js')
async def fc26_script(request: Request):
    from fastapi.responses import Response
    name = request.url.path.rsplit('/', 1)[-1]
    return Response((HERE / name).read_text(), media_type='application/javascript', headers={'Cache-Control':'no-cache'})


FC26_STOP_CONTEXT = {}


def fc26_enrich_context(raw, samples):
    """Reuse cached LTA StopSequence and verified route-chainage context, no API calls."""
    static_hit=CACHE.get('static')
    st=static_hit[2] if static_hit and isinstance(static_hit[2],dict) else {}
    idx=(FC.get('world') or {}).get('index')
    lines=idx.lines if idx is not None else {}
    sample_by={(str(s.get('service') or '').upper(),s.get('direction')):s for s in samples}
    keys=set(sample_by)
    for event in raw.get('events') or []:
        if event.get('status')=='confirmed':
            keys.update((s['svc'],s['dir']) for s in event.get('services') or [])
    context={}
    for key in keys:
        sample=sample_by.get(key,{})
        stops=sample.get('route_stops') or (route_stops(st,*key) if st.get('routes') else [])
        line=lines.get(key)
        cache_key=(key,id(line),id(st),tuple((s.get('code'),s.get('seq')) for s in stops))
        cached_rows=FC26_STOP_CONTEXT.get(cache_key)
        if cached_rows is None:
            cached_rows=[dict(s) for s in stops]
            valid=[(i,s) for i,s in enumerate(cached_rows) if s.get('lat') is not None and s.get('lon') is not None]
            if line is not None and valid:
                _,positions,_=line.locate([forecast.to_xy(s['lat'],s['lon']) for _,s in valid])
                for (i,_),position in zip(valid,positions):cached_rows[i]['route_m']=float(position)
            FC26_STOP_CONTEXT[cache_key]=cached_rows
        context[f'{key[0]}:{key[1]}']=cached_rows
    if len(FC26_STOP_CONTEXT)>500:FC26_STOP_CONTEXT.clear()
    samples=[dict(s,route_stops=context.get(f"{str(s.get('service') or '').upper()}:{s.get('direction')}",s.get('route_stops') or [])) for s in samples]
    return dict(raw,route_context=context,headway_schedule=fc26.master_schedule(BB.get('master',[]),keys,time.time())),samples


FC26_LINES = {}   # (svc, dir, id(Line)) -> simplified [[lon, lat]]: built once per route-index build, not per request


def fc26_line(key, L):
    hit = FC26_LINES.get((key, id(L)))
    if hit is None:
        if len(FC26_LINES) > 6000:
            FC26_LINES.clear()
        hit = FC26_LINES[(key, id(L))] = fc26.simplify_latlon(L.pts, 5.0)
    return hit


async def fc26_live_state():
    """Answer at once from the last completed evaluation; refresh it in the background when due."""
    if FC["state"] is not None and time.time() - FC["last"] >= FC_EVERY - 5:
        task = FC.get("fc26_refresh")
        if task is None or task.done():
            FC["fc26_refresh"] = asyncio.create_task(fc_cycle())
        FC["last_req"] = time.time()
        now = time.time()
        return fc_payload(FC["state"], FC["book"].view(now, history=True), FC["book"].counts(), FC["store"].audit_list(40),
                          {"config": fc_config(), "age_s": round(now - FC["last"]), "cycle_ms": FC["cycle_ms"], "error": FC["err"]})
    return await api_forecast_state()


def fc26_response(request, data):
    """JSON, gzip-compressed when the browser accepts it (route geometry compresses ~5x)."""
    import gzip as _gzip
    from fastapi.responses import Response
    body = json.dumps(data, separators=(',', ':'), default=lambda o: o.item() if hasattr(o, 'item') else str(o)).encode()
    if len(body) > 2048 and 'gzip' in (request.headers.get('accept-encoding') or ''):
        return Response(_gzip.compress(body, 5), media_type='application/json', headers={'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding', 'Cache-Control': 'no-store'})
    return Response(body, media_type='application/json', headers={'Cache-Control': 'no-store'})


@app.get('/api/forecast/centre')
async def fc26_centre(request: Request, mode: str='live', horizon: int=0, scenario: str='combined', tick: int=0,
                     services: str='', exclude_cbs: int=0, category: str='all', delay_min: float=.8, routes_v: str=''):
    if mode not in ('demo','live') or horizon not in fc26.HORIZONS or scenario not in dict(fc26.SCENARIOS):
        return JSONResponse({'error':'Unsupported data mode, horizon or scenario'},status_code=400)
    try:
        delay_min=fc26.delay_threshold(delay_min)
    except ValueError as error:
        return JSONResponse({'error':str(error)},status_code=400)
    raw, samples = {}, []
    if mode=='live':
        # Start LTA collection independently of traffic incidents or TomTom availability.
        initial_watch = services or ','.join(FC_SERVICES) or BB_DEFAULT
        await api_forecast_bunching(services=initial_watch)
        raw = await fc26_live_state()
        # Refresh the existing bunching collector watch list, within its normal budget.
        await api_forecast_bunching(services=services or ','.join(s['svc'] for s in raw.get('services',[])[:20]) or initial_watch)
        samples = list(BB['rows'].values())
        raw = dict(raw or {}, bb_params=dict(BB['params']))
        raw,samples = await asyncio.to_thread(fc26_enrich_context,raw,samples)
    # Synchronise the full namespace before applying user filters. Filters cannot resolve alerts.
    data = await asyncio.to_thread(fc26.build_centre,mode,horizon,scenario,max(0,tick),raw,samples,delay_min=0)
    ns=f'{mode}:{scenario if mode=="demo" else "network"}:{horizon}'
    journal_rows={}
    try:
        ledger=fc26_ledger()
        ledger.sync(ns,data['alerts'],time.time(),can_resolve=mode=='demo')
        history,audit=ledger.view(ns)
        statuses={a['id']:a['status'] for a in history}
        journal_rows={a['id']:a for a in history}
        for a in data['alerts']:
            a['status']=statuses.get(a['id'],'new')
        data.update(history=history,audit=audit,namespace=ns)
    except sqlite3.Error:
        data.update(history=[],audit=[],namespace=ns,journal_error='Alert persistence is unavailable')
    wanted={s.strip().upper() for s in services.split(',') if s.strip()}
    if delay_min>=0:  # Always filter the full, unfiltered audit snapshot for this view.
        filtered=await asyncio.to_thread(fc26.build_centre,mode,horizon,scenario,max(0,tick),raw,samples,list(wanted),bool(exclude_cbs),category,delay_min=delay_min)
        for key in ('services','alerts','events','buses','routes','stops','timeline','service_series','kpis','recommendations','context_routes','delay_filter','headway_history','headway_schedule'):
            data[key]=filtered[key]
        for a in data['alerts']:
            a['status']=statuses.get(a['id'],'new') if 'statuses' in locals() else 'new'
            a.update({key:journal_rows[a['id']][key] for key in ('first',) if key in journal_rows.get(a['id'],{})})
        data['history']=[a for a in data.get('history',[]) if (not wanted or a['svc'] in wanted) and (not exclude_cbs or a['svc'] not in fc26.CBS) and (category=='all' or a['category']==category) and (a['category']!='traffic' or fc26.meets_delay(a,delay_min))]
    if mode=='live' and wanted:
        world=FC.get('world') or {}
        idx=world.get('index')
        lines=idx.lines if idx is not None else {}
        for key,line in lines.items():
            if key[0] in wanted and (not exclude_cbs or key[0] not in fc26.CBS) and len(data['routes'])<40:
                data['routes'].append({'svc':key[0],'dir':key[1],'line':fc26_line(key,line),'mode':'live'})
                rs=(world.get('stops') or {}).get(key,[])
                dest=(rs[-1].get('name') if rs else None)
                for stop in rs:
                    data['stops'].append({'id':stop.get('code'),'name':stop.get('name'),'point':[stop['lon'],stop['lat']],'svc':key[0],'dir':key[1],
                                          'seq':stop.get('seq'),'km':stop.get('dist'),'road':stop.get('road'),'dest':dest})
    if mode=='live':
        idx=(FC.get('world') or {}).get('index')
        if idx is not None:
            route_keys={(s['svc'],s['dir']) for s in data['services']}
            ordered=sorted(idx.lines.items(),key=lambda item:(item[0][0] not in wanted,item[0] not in route_keys))
            context=[{'svc':k[0],'dir':k[1],'line':fc26_line(k,v)} for k,v in ordered[:250] if not exclude_cbs or k[0] not in fc26.CBS]
            if not wanted:
                present={(r['svc'],r['dir']) for r in data['routes']}
                data['routes'] += [r for r in context if any(s['svc']==r['svc'] and s['dir']==r['dir'] for s in data['services']) and (r['svc'],r['dir']) not in present]
            # The browser keeps the background route set; resend it only when it changes.
            version=f"{FC.get('index_at')}:{hash(tuple((r['svc'],r['dir']) for r in context)) & 0xffffffff:x}"
            data['context_routes_version']=version
            if routes_v==version:
                data['context_routes']=None;data['context_routes_cached']=True
            else:
                data['context_routes']=context
    try:
        data['acks']=fc26_ledger().acks(time.time())
    except sqlite3.Error:
        data['acks']={}
    # Demo cameras are deliberately not filled with fabricated traffic photos.
    data['cameras']=[]
    if mode=='live':
        hit=CACHE.get('cameras')
        if hit:
            cams=(hit[2].get('cams') or []) if isinstance(hit[2],dict) else []
            data['cameras']=[{'id':c['id'],'image':c['link'],'label':c.get('desc') or c.get('road') or 'Traffic camera','taken':c.get('taken')} for c in cams[:4]]
    return fc26_response(request, data)


@app.post('/api/forecast/centre/ack')
async def fc26_ack(request: Request):
    """Acknowledge (or undo) one Heavy loading / Incident-traffic / Bunching-gap board card for every controller."""
    try:
        body=await request.json()
        key=str(body.get('key',''))
        if not re.fullmatch(r'(load|event|svc|3bb|gap):[A-Za-z0-9:._#\-]{1,190}',key):
            raise ValueError('Invalid card')
        user=auth.read(request.cookies.get(auth.COOKIE,'')) if hasattr(auth,'read') else None
        identity=(user or {}).get('u') if isinstance(user,dict) else None
        who=str((identity.get('name') or identity.get('id')) if isinstance(identity,dict) else identity or 'controller')[:80]
        now=time.time()
        fc26_ledger().ack(key,who,now,undo=bool(body.get('undo')))
        return {'ok':True,'key':key,'who':who,'ts':now}
    except (ValueError,TypeError):
        return JSONResponse({'error':'Invalid acknowledgement'},status_code=400)
    except sqlite3.Error:
        return JSONResponse({'error':'Acknowledgement store unavailable'},status_code=503)


@app.post('/api/forecast/centre/alerts')
async def fc26_transition(request: Request):
    try:
        body=await request.json()
        ns=str(body.get('namespace',''))
        if not re.fullmatch(r'(demo|live):[a-z_]+:(0|15|30|60|120)',ns):
            raise ValueError('Invalid alert namespace')
        user=auth.read(request.cookies.get(auth.COOKIE,'')) if hasattr(auth,'read') else None
        identity=(user or {}).get('u') if isinstance(user,dict) else None
        who=str((identity.get('name') or identity.get('id')) if isinstance(identity,dict) else identity or 'controller')[:80]
        ok=fc26_ledger().transition(ns,str(body.get('id','')),str(body.get('status','')),who,time.time())
        return JSONResponse({'ok':ok},status_code=200 if ok else 404)
    except (ValueError,TypeError):
        return JSONResponse({'error':'Invalid alert transition'},status_code=400)


def fc26_camera_ai_io(cpa):
    """Network helpers for checkpoint_ai: download a camera photo; ask Claude (vision). Errors carry the API's own message."""
    async def fetch_image(url):
        r=await client().get(url,timeout=15)
        r.raise_for_status()
        data,mime=r.content,r.headers.get('content-type','')
        try:                                   # shrink to 768 px wide: about a sixth of the image tokens, still enough to see a queue
            from PIL import Image
            import io as _io
            im=Image.open(_io.BytesIO(data)).convert('RGB')
            if im.width>768:
                im=im.resize((768,round(im.height*768/im.width)))
            buf=_io.BytesIO();im.save(buf,'JPEG',quality=72);data,mime=buf.getvalue(),'image/jpeg'
        except Exception:
            pass                               # Pillow missing or not an image: send as is
        return data,mime
    async def ask_model(content):
        last=None
        for model in dict.fromkeys([cpa.MODEL,DV_AI_MODEL]):          # camera model first, then the Diversion review model
            r=await client().post('https://api.anthropic.com/v1/messages',timeout=60,
                                  headers={'x-api-key':DV_AI_KEY,'anthropic-version':'2023-06-01','content-type':'application/json'},
                                  json={'model':model,'max_tokens':4000,'messages':[{'role':'user','content':content}]})
            if r.status_code==200:
                cpa.STATUS['model']=model
                j=r.json()
                blocks=[b for b in (j.get('content') or []) if isinstance(b,dict)]
                text=''.join(b.get('text','') for b in blocks if b.get('type')=='text')
                if not text.strip():          # say why: e.g. stop_reason max_tokens / refusal, or only non-text blocks came back
                    raise RuntimeError(f"Claude ({model}) returned no text: stop_reason={j.get('stop_reason')}, blocks={[b.get('type') for b in blocks]}")
                return text
            try:
                msg=(r.json().get('error') or {}).get('message') or r.text[:160]
            except Exception:
                msg=r.text[:160]
            last=f'Claude API HTTP {r.status_code} ({model}): {msg}'
            if r.status_code not in (400,404) or 'model' not in str(msg).lower():
                break                                                     # only a model problem is worth retrying
        raise RuntimeError(last or 'Claude API did not answer')
    return fetch_image,ask_model


CAM_SWAP = {"loaded": False, "info": {}}


def cam_swap_load(cpa):
    """Checkpoint cameras whose two directions a controller swapped (kept in the database, shared by every viewer)."""
    if CAM_SWAP["loaded"]:
        return
    bb_sql("CREATE TABLE IF NOT EXISTS camera_dir_swap(cam TEXT PRIMARY KEY, swapped INTEGER, who TEXT, ts REAL)")
    for r in bb_sql("SELECT cam, swapped, who, ts FROM camera_dir_swap", fetch=True) or []:
        cam = str(r["cam"])
        (cpa.SWAPPED.add if r["swapped"] else cpa.SWAPPED.discard)(cam)
        CAM_SWAP["info"][cam] = {"swapped": bool(r["swapped"]), "who": r["who"], "ts": r["ts"]}
    CAM_SWAP["loaded"] = True


@app.post('/api/forecast/centre/camera-ai/swap')
async def fc26_camera_swap(request: Request):
    """A controller says a checkpoint camera shows its two directions the wrong way round: swap them (or undo) for everyone."""
    import checkpoint_ai as cpa
    try:
        body = await request.json()
        cam = str(body.get('id', '')).strip()
        if cam not in cpa.CHECKPOINT_IDS:
            raise ValueError('not a checkpoint camera')
        on = bool(body.get('swapped'))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({'error': 'Pick one of the checkpoint cameras: ' + ', '.join(sorted(cpa.CHECKPOINT_IDS)) + '.'}, status_code=400)
    cam_swap_load(cpa)
    user = auth.read(request.cookies.get(auth.COOKIE, '')) if hasattr(auth, 'read') else None
    identity = (user or {}).get('u') if isinstance(user, dict) else None
    who = str((identity.get('name') or identity.get('id')) if isinstance(identity, dict) else identity or 'controller')[:80]
    now = time.time()
    (cpa.SWAPPED.add if on else cpa.SWAPPED.discard)(cam)
    bb_sql("INSERT OR REPLACE INTO camera_dir_swap(cam, swapped, who, ts) VALUES (?,?,?,?)", (cam, int(on), who, now))
    CAM_SWAP["info"][cam] = {"swapped": on, "who": who, "ts": now}
    return {'ok': True, 'id': cam, 'swapped': on, 'who': who, 'ts': now, 'saved': bool(BB.get('db_ok', True))}


@app.get('/api/camera-ai/test')
async def api_camera_ai_test():
    """One-click check of AI camera analytics: is the key set, can the checkpoint photos be downloaded, does Claude answer."""
    import checkpoint_ai as cpa
    if not DV_AI_KEY:
        return {'ok':False,'key_set':False,'answer':'ANTHROPIC_API_KEY is not set on this server (Render > Environment), so cameras use LTA speed bands only.'}
    cs=await cameras_state()
    cams={str(c['id']):c for c in (cs.get('cams') or [])}
    pick=[cams[i] for x in cpa.CROSSINGS for i in x['cams'] if i in cams]
    if not pick:
        return {'ok':False,'key_set':True,'answer':'The checkpoint cameras (2701, 2702, 4703, 4713) are not in the LTA camera feed right now.','camera_error':cs.get('error')}
    cam_swap_load(cpa)
    fetch_image,ask_model=fc26_camera_ai_io(cpa)
    out=await cpa.analyse(pick,fetch_image,ask_model,None,force=True)
    ok=not out.get('error') and bool(out.get('cameras'))
    return {'ok':ok,'key_set':True,'model':cpa.STATUS.get('model'),'photos_sent':cpa.STATUS.get('images'),
            'answer':'AI camera analytics is working.' if ok else (out.get('error') or 'Claude answered but gave no level for these photos.'),
            'levels':{k:{x:v.get(x) for x in ('level','to_jb','to_sg','sg_where','jb_where','swapped') if v.get(x) is not None} for k,v in out.get('cameras',{}).items()},
            'crossings':out.get('crossings'),'ai_reply':cpa.STATUS.get('reply')}


@app.get('/api/forecast/centre/camera-ai')
async def fc26_camera_ai(ids: str=''):
    """AI camera analytics: traffic level (Smooth / Medium / Jam / Super Jam) for the cameras on screen and estimated
    crossing time to JB / SG from the Woodlands and Tuas checkpoint cameras. Claude vision when ANTHROPIC_API_KEY is
    set, else TomTom speed at the camera, else nothing. Cached per photo (checkpoint_ai.CACHE_S)."""
    import checkpoint_ai as cpa
    cam_swap_load(cpa)
    cs=await cameras_state()
    cams={str(c['id']):c for c in (cs.get('cams') or [])}
    want=[i for i in dict.fromkeys([x.strip() for x in ids.split(',') if x.strip()][:6]) if i in cams]
    pick=[cams[i] for i in dict.fromkeys(want+[c for x in cpa.CROSSINGS for c in x['cams']]) if i in cams]
    fetch_image,ask_model=fc26_camera_ai_io(cpa)
    bs=await bands_state()
    idx=bs.get('idx')
    async def flow(lat,lon):
        # LTA Traffic Speed Bands (free, already cached for Route Traffic). Never TomTom: its shared daily cap feeds the
        # Forecast Centre traffic layer, and camera look-ups must not use it up.
        if idx is None:
            return None
        # Typical (median) band of the road links within 300 m, not the single worst one: the worst link near a
        # checkpoint is usually the immigration plaza or a slip road, which read "Super Jam" even on an empty night.
        k,near=math.cos(math.radians(lat)),[]
        for alat,alon,blat,blon,band,road,mn,mx,cat in idx.segs:
            if any(((pa-lat)*111.32)**2+((po-lon)*111.32*k)**2<=0.09 for pa,po in ((alat,alon),(blat,blon),((alat+blat)/2,(alon+blon)/2))):
                near.append((band,mn,mx,road))
        if not near:
            return None
        near.sort(key=lambda x:x[0])
        best=near[(len(near)-1)//2]
        return {'level':cpa.level_from_band(best[0]),'speed':best[1],'free_speed':best[2],'band':best[0],'road':best[3],'source':'LTA speed bands'}
    out=await cpa.analyse(pick,fetch_image if DV_AI_KEY else None,ask_model if DV_AI_KEY else None,flow)
    out['configured']={'ai':bool(DV_AI_KEY),'speed':idx is not None}
    out['swap_info']={k:v for k,v in CAM_SWAP['info'].items() if v.get('swapped')}
    out['ai_status']={k:cpa.STATUS.get(k) for k in ('ok_at','error','error_at','model')}
    return out


@app.get('/api/forecast/centre/cameras')
async def fc26_camera_context(lat: float=None, lon: float=None):
    """Reuse shared camera cache; no static-route download or 350-metre cutoff."""
    from forecast2026_amendments import select_cameras
    if (lat is None)!=(lon is None) or (lat is not None and not in_sg(lat,lon)):
        return JSONResponse({'error':'A valid Singapore latitude and longitude are required'},status_code=400)
    cs=await cameras_state()
    hit=CACHE.get('cameras')
    notes=[]
    if not KEY:
        notes.append('LTA DataMall AccountKey is not configured; using public data.gov.sg fallback where available.')
    if cs.get('raw_error'):notes.append('LTA: '+cs['raw_error'])
    if cs.get('fallback_error'):notes.append('data.gov.sg: '+cs['fallback_error'])
    return {'cameras':select_cameras(cs.get('cams') or [],[lon,lat] if lat is not None else None),
            'fetched_epoch':hit[1] if hit else None,'source':cs.get('source'),'error':cs.get('error'),
            'diagnostics':notes,'context':'Nearest available cameras; proximity does not confirm bus-route impact.' if lat is not None else 'Latest available network cameras',
            'note':'Source still images; unknown capture times are labelled unverified.'}
