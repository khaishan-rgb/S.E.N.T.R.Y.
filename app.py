"""SG Transport Pulse V5 - FastAPI backend.

Data sources
  LTA DataMall  (needs LTA_ACCOUNT_KEY): bus stops/routes, bus arrival, traffic speed bands, incidents
  NEA via data.gov.sg (no key needed):   real-time rainfall
  OSRM (public demo by default):         snaps the stop-to-stop route onto roads (optional, falls back)

LTA endpoint paths follow the DataMall API User Guide v6.9 (3 Aug 2026):
  v4/TrafficSpeedBands   (was probed as v3/... before, which now returns 404)
  v3/BusArrival          (was called BusArrivalv3, which is not a real path)
"""
import os, re, math, time, asyncio, json, hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import headway
import routegeom
import traffic

VERSION = "V13.11"
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


def parse_services(data, now, only=None):
    out = []
    for s in data.get("Services", data.get("value", [])) or []:
        svc = str(s.get("ServiceNo", "")).strip().upper()
        if only and svc != only:
            continue
        buses = [parse_bus(s.get(k), now) for k in ("NextBus", "NextBus2", "NextBus3")]
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
    if KEY:
        asyncio.create_task(warm())
        bb_init()
        task = asyncio.create_task(bb_loop())
        asyncio.create_task(rt_data_loop())
        asyncio.create_task(tr_refresh(force=True))
        trtask = asyncio.create_task(tr_loop())
    yield
    if task is not None:
        task.cancel()
    if trtask is not None:
        trtask.cancel()
    if _client is not None:
        await _client.aclose()


app = FastAPI(title=f"SG Transport Pulse {VERSION}", lifespan=lifespan)


@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse((HERE / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    return {"online": True, "lta": bool(KEY), "version": VERSION, "time": now_sgt().isoformat(timespec="seconds"), "speedEndpoint": GOOD_SPEED_PATH}


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
TT = {"trips": {}, "updated": None, "rows": 0}     # {(svc, dir): [trip, ...]}
TT_TOKEN = os.getenv("TIMETABLE_TOKEN", "")         # if set, uploads/clears need header x-admin-token
TT_MAX_BYTES = 8 * 1024 * 1024
ALL_CAP = int(os.getenv("CONTROL_ALL_CAP", "160"))   # service+direction pairs scanned in All-services mode


def tt_save():
    try:
        TT_FILE.write_text(json.dumps({"updated": TT["updated"], "rows": TT["rows"], "trips": {f"{k[0]}|{k[1]}": v for k, v in TT["trips"].items()}}), encoding="utf-8")
    except OSError:
        pass          # read-only or ephemeral disk: the timetable then lives in memory only


def tt_load():
    try:
        j = json.loads(TT_FILE.read_text(encoding="utf-8"))
        TT["trips"] = {(k.split("|")[0], int(k.split("|")[1])): v for k, v in j.get("trips", {}).items()}
        TT["updated"], TT["rows"] = j.get("updated"), j.get("rows", 0)
    except (OSError, ValueError, IndexError):
        pass


def tt_summary():
    pairs = {f"{k[0]}:{k[1]}": len(v) for k, v in TT["trips"].items()}
    return {"loaded": bool(pairs), "pairs": pairs, "services": len({k[0] for k in TT["trips"]}), "trips": sum(pairs.values()), "updated": TT["updated"], "tokenRequired": bool(TT_TOKEN)}


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
    TT.update(trips=trips, updated=now_sgt().isoformat(timespec="seconds"), rows=info["rows"])
    tt_save()
    return {"ok": True, "rows": info["rows"], "errors": info["errors"], **tt_summary()}


@app.post("/api/timetable/clear")
async def api_timetable_clear(request: Request):
    if TT_TOKEN and request.headers.get("x-admin-token", "") != TT_TOKEN:
        return JSONResponse({"error": "Upload token missing or wrong."}, status_code=401)
    TT.update(trips={}, updated=None, rows=0)
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
        tt = TT["trips"].get((svc, d))
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

BB_DB = os.getenv("BUNCHING_DB", str(HERE / "bunching.db"))
BB_DEFAULT = os.getenv("BUNCHING_SERVICES", "32,145,65,33,51,74,89,200,27,157")
BB_MAX_PAIRS = int(os.getenv("BUNCHING_MAX_PAIRS", "40"))
BB_ADMIN = os.getenv("BUNCHING_ADMIN_TOKEN", "") or TT_TOKEN
BB_IDLE_SEC = 600                     # stop polling LTA when nobody has looked at the page for 10 min
BB_ALWAYS = os.getenv("BUNCHING_ALWAYS_ON", "").strip().lower() in ("1", "true", "yes")   # keep polling with nobody watching, so the event log keeps filling
BB = {"params": dict(bunching.PARAMS), "labels": {}, "master": [], "rows": {}, "watch": {}, "hist": deque(maxlen=900), "trk": {}, "open": {}, "seq": 0,
      "last_req": 0.0, "cycle": {"at": None, "took": None, "pairs": 0}, "loop_at": 0.0, "db_ok": True, "db_err": None}
BB_INT_PARAMS = ("confirm_stops", "gap_stops", "alert_step", "refresh_sec", "horizon_min")
PARAM_RANGES = {"bunch_min": (0.5, 10), "confirm_stops": (1, 60), "gap_add_min": (1, 60), "gap_stops": (1, 60), "alert_step": (1, 30), "horizon_min": (10, 60), "refresh_sec": (15, 600),
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


def bb_init():
    bb_sql("CREATE TABLE IF NOT EXISTS system_parameter(k TEXT PRIMARY KEY, v REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS service_headway_config(service TEXT, direction INTEGER, day_type TEXT, t_from TEXT, t_to TEXT, hw REAL)")
    bb_sql("CREATE TABLE IF NOT EXISTS bunching_event(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, direction INTEGER, start_ts REAL, end_ts REAL, bus_group TEXT, "
           "max_level INTEGER, start_stop TEXT, end_stop TEXT, stops INTEGER, min_hw REAL)")
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
        e["max_level"] = max(e["max_level"], g["size"] + len(g["joining"]))
        e["min_hw"] = min(e["min_hw"], g["min_hw"])
        if g["cur_j"] >= e["last_j"]:
            e["last_j"], e["end_stop"] = g["cur_j"], g["cur_stop"]
        e["last"], e["miss"] = ts, 0
        bb_alert_update(e, ts)
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
        seen.add(e["id"])
    for eid in [i for i, e in ev.items() if e["key"] == key and i not in seen]:
        e = ev[eid]
        e["miss"] = e.get("miss", 0) + 1
        if e["miss"] >= 2:
            bb_close_event(ev.pop(eid))


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
    BB["hist"].append({"ts": t0, "per": per})
    BB["cycle"] = {"at": t0, "took": round(time.time() - t0, 1), "pairs": len(keys)}
    BB["loop_at"] = time.time()


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


# ---- V14.1 shared basemap: CARTO raster tiles (keyed) with OneMap / OpenStreetMap fallback. The key is read from the environment;
# it is visible in the browser anyway (every tile URL carries it), so restrict it to your domain in the CARTO dashboard.
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "cb1_3yrv_1_668b3534c1ae1cf8528fb415").strip()


@app.get("/basemap.js")
async def basemap_js():
    from fastapi.responses import Response
    js = (HERE / "basemap.js").read_text(encoding="utf-8").replace("__CARTO_KEY__", re.sub(r"[^A-Za-z0-9_\-]", "", CARTO_API_KEY))
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
                                                         "truncated": len(svcs) * len(dirs) > BB_MAX_PAIRS},
            "open_events": sum(1 for e in bb_open_public() if e["service"] + ":" + str(e["direction"]) in kk), "alerts": bb_alerts(set(kk))}


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
    if not e or not e.get("alerts"):
        return JSONResponse({"error": "That alert is no longer active."}, status_code=404)
    e["acked_n"], e["acked_ts"] = len(e["alerts"]), time.time()
    return {"ok": True, "alert": bb_alert_public(e)}


@app.get("/api/bunching/events")
async def api_bb_events(limit: int = 100):
    limit = max(1, min(500, limit))
    closed = bb_sql("SELECT 'bb' AS kind, service, direction, start_ts AS start, end_ts AS end, bus_group AS buses, max_level AS level, stops, min_hw, NULL AS max_hw, start_stop, end_stop, start_stop AS location, alerts FROM bunching_event "
                    "UNION ALL SELECT 'gap', service, direction, start_ts, end_ts, buses, NULL, stops, NULL, max_hw, start_stop, end_stop, location, alerts FROM gap_event ORDER BY start DESC LIMIT ?", (limit,), fetch=True)
    return {"events": bb_open_public() + closed, "db_ok": BB["db_ok"], "min_stops": {"bunching": int(BB["params"]["confirm_stops"]), "gap": int(BB["params"]["gap_stops"])}}


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


async def os_osrm(coords, alternatives=0, steps=True):
    """OSRM driving route through coords [(lat, lon), ...] with road names per step. Cached 10 min."""
    path = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in coords)

    async def factory():
        try:
            prm = {"overview": "full", "geometries": "geojson", "steps": "true" if steps else "false"}
            if alternatives:
                prm["alternatives"] = str(alternatives)
            r = await client().get(f"{OSRM}/route/v1/driving/{path}", params=prm, timeout=25)
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "Ok":
                return {"routes": [], "error": j.get("message") or j.get("code")}, 120, False
            return {"routes": offservice.parse_osrm(j), "error": None}, 600, True
        except Exception as e:
            return {"routes": [], "error": f"routing service unavailable ({type(e).__name__})"}, 60, False
    return await cached(f"osrm:{alternatives}:{path}", factory)


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
@app.get("/planner", response_class=HTMLResponse)
async def planner_page():
    return HTMLResponse((HERE / "planner.html").read_text(encoding="utf-8"))


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
    return {"options": opts, "recommended": rec, "fastest": fastest, "shortest": shortest, "roadworks": rw, "incidents": inc, "P": P}, None


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
    HP_SNAP[sid] = {"created": time.time(), "svc": svc, "d": direction, "g": g, "buses": buses, "now_min": now_min, "P": P, "opp": opp}
    cands, unresolved, cinfo = ho_candidates(svc, direction, stops, "", "auto", g["prep"], P)
    strip_b = lambda bs: [{k: v for k, v in b.items() if k not in ("s", "offset")} for b in bs]
    return {"ok": True, "snap": sid, "service": svc, "direction": direction, "directions": dirs, "now": now.strftime("%H:%M:%S"), "now_min": round(now_min, 2),
            "H": g["H"], "H_src": g["H_src"], "traffic_ok": g["traffic_ok"],
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(g["tau"][-1], 1), "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": len(stops)},
            "stops": [{"j": i, "code": s_["code"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "km": round(ss[i], 2)} for i, s_ in enumerate(stops)],
            "line": ho_simplify(line, 500), "buses": strip_b(buses), "bus_error": bus_err, "off_route": off_route,
            "opp": ({"direction": opp["dir"], "line": ho_simplify(opp["g"]["line"], 400), "buses": strip_b(opp["buses"]), "km": round(opp["g"]["prep"]["km"], 2),
                     "first": opp["g"]["stops"][0]["name"], "last": opp["g"]["stops"][-1]["name"], "H": opp["g"]["H"]} if opp else None),
            "candidates": {"scope": cinfo.get("scope_used"), "approved": cinfo.get("approved"), "tested": len(cands), "unresolved": unresolved},
            "source": "LTA DataMall Bus Arrival (live) \u00b7 route running times from LTA speed bands" if g["traffic_ok"] else "LTA DataMall Bus Arrival (live) \u00b7 running times at the fallback speed (no speed bands)"}


@app.get("/api/hplan/testsnap")
async def api_hp_testsnap(service: str = "", direction: int = 1, time_: str = Query("", alias="time"), headway: str = "10", buses: str = "",
                          offset: str = "", stop_min: str = "2"):
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

    def fleet(g, phase):
        stops, ss = g["stops"], g["prep"]["stop_s"]
        n = len(stops)
        run = sm * (n - 1)
        out = []
        for k in range(nb):
            el = k * H + phase + 1.0                               # minutes since this bus left the first stop
            if el >= run - 0.5:
                break
            p = el / sm
            i = min(n - 2, int(p)); f = p - i
            s_km = ss[i] + f * (ss[i + 1] - ss[i])
            lat = stops[i]["lat"] + f * (stops[i + 1]["lat"] - stops[i]["lat"]); lon = stops[i]["lon"] + f * (stops[i + 1]["lon"] - stops[i]["lon"])
            near = stops[i + 1] if f >= 0.5 else stops[i]
            nxt = stops[i + 1]
            out.append({"s": s_km, "lat": lat, "lon": lon, "km": round(s_km, 2), "near": {"code": near["code"], "name": near["name"]}, "near_name": near["name"],
                        "next": {"code": nxt["code"], "name": nxt["name"], "eta_min": round((1 - f) * sm, 1), "clock": hplan.hhmm(t0 + (1 - f) * sm)},
                        "offset": 0.0, "load": None, "type": None, "monitored": 0, "test": True})
        out.sort(key=lambda b: b["s"])
        for i, b in enumerate(out, 1):
            b["id"] = i
        return out, int(run // H) + 1

    g = {**g0, "H": H, "H_src": "test input"}
    P = {**halfway.PARAMS, **HO["params"]}
    tb, fit = fleet(g, 0.0)
    opp = None
    od = next((d_ for d_ in dirs if d_ != direction), None)
    if od is not None:
        g2 = await ho_route(svc, od)
        if not g2.get("error"):
            g2 = {**g2, "H": H, "H_src": "test input"}
            ob, _ = fleet(g2, off)
            opp = {"g": g2, "buses": ob, "dir": od}
    sid = hashlib.sha1(f"test|{svc}|{direction}|{time.time()}".encode()).hexdigest()[:12]
    hp_prune()
    HP_SNAP[sid] = {"created": time.time(), "svc": svc, "d": direction, "g": g, "buses": tb, "now_min": float(t0), "P": P, "opp": opp, "test": True}
    stops, ss, line = g["stops"], g["prep"]["stop_s"], g["line"]
    strip_b = lambda bs: [{k: v for k, v in b.items() if k not in ("s", "offset")} for b in bs]
    return {"ok": True, "test": True, "snap": sid, "service": svc, "direction": direction, "directions": dirs, "now": hplan.hhmm(t0) + ":00", "now_min": float(t0),
            "H": H, "H_src": "test input", "traffic_ok": g0.get("traffic_ok"),
            "route": {"km": round(g["prep"]["km"], 2), "run_min": round(sm * (len(stops) - 1), 1), "first": stops[0]["name"], "last": stops[-1]["name"], "n_stops": len(stops)},
            "stops": [{"j": i, "code": s_["code"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "km": round(ss[i], 2)} for i, s_ in enumerate(stops)],
            "line": ho_simplify(line, 500), "buses": strip_b(tb), "bus_error": None, "off_route": 0,
            "opp": ({"direction": opp["dir"], "line": ho_simplify(opp["g"]["line"], 400), "buses": strip_b(opp["buses"]), "km": round(opp["g"]["prep"]["km"], 2),
                     "first": opp["g"]["stops"][0]["name"], "last": opp["g"]["stops"][-1]["name"], "H": H} if opp else None),
            "test_info": {"time": hplan.hhmm(t0), "headway": H, "buses": nb, "fit": fit, "placed": len(tb), "placed_opp": len(opp["buses"]) if opp else 0,
                          "offset": off, "stop_min": sm},
            "source": "TEST MODE \u00b7 synthetic evenly spaced buses on the real route (not live)"}


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
async def api_hp_plan(snap: str = "", bus: str = "", delay: str = "20", brk: str = "", stop_min: str = "", layover: str = ""):
    """Late bus completes its trip; its NEXT trip starts halfway. Every stop of the next direction is tested with real-road off-service
    time from the interchange; front bus hold / rear bus advance chosen per stop; ranked by downstream EWT. Live data unchanged."""
    S = HP_SNAP.get(snap)
    if not S:
        return {"ok": False, "error": "The live snapshot has expired. Load the live buses again.", "expired": True}
    if not str(bus).isdigit() or not any(b["id"] == int(bus) for b in S["buses"]):
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
    except ValueError:
        return {"ok": False, "error": "Lateness, break and stop-to-stop time must be numbers."}
    g = S["g"]
    mk = lambda gg, bs, d_: {"dir": d_, "stops": gg["stops"], "ss": gg["prep"]["stop_s"], "buses": bs, "H": gg["H"], "H_src": gg["H_src"]}
    T = mk(g, S["buses"], S["d"])
    O = mk(S["opp"]["g"], S["opp"]["buses"], S["opp"]["dir"]) if S.get("opp") else None
    gn = S["opp"]["g"] if S.get("opp") else g
    st = gn["stops"]
    ic = st[0]
    js = list(range(1, len(st) - 1))
    offs, off_err = await os_table((ic["lat"], ic["lon"]), [(st[j]["lat"], st[j]["lon"]) for j in js])
    fac = float(offservice.PARAMS["bus_time_factor"])
    reach = {}
    for i, j in enumerate(js):
        om = offs.get(i)
        if om:
            reach[j] = (om["min"] * fac, om.get("km"), f"real road routing (OSRM) x {fac:g} bus factor")
        else:
            km = hplan.hav_km((ic["lat"], ic["lon"]), (st[j]["lat"], st[j]["lon"])) * 1.35
            reach[j] = (km / 25.0 * 60.0, km, "estimate - road routing unavailable")
    if S.get("test"):
        Pp["edge_trim"] = True                                          # test fleet: ignore the gap in front of its first bus
    res = await asyncio.to_thread(hwplan.plan, {"now": S["now_min"], "late": {"bus": int(bus), "delay": D}, "T": T, "O": O, "P": Pp}, reach)
    res.update(snap=snap, service=S["svc"], routing="real road routing (OSRM)" if offs else f"estimate ({off_err or 'road routing unavailable'})")
    return res


@app.get("/api/hplan/route")
async def api_hp_route(snap: str = "", stop: str = "", mode: str = "late", bus: str = "", os_from: str = "", leave: str = "", from_pt: str = "", from_label: str = "", on: str = ""):
    """Off-service deployment route from the bus / OS location to the entry stop, with turn-by-turn guidance, traffic, incidents and road works."""
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
    x = next(o for o in R["options"] if o["idx"] == R["recommended"])
    near = lambda p: offservice.dist_to_line_m((p["lat"], p["lon"]), x["line"]) <= 300
    return {"ok": True, "origin": {"label": origin_label, "lat": origin[0], "lon": origin[1]},
            "stop": {"code": stops[j]["code"], "name": stops[j]["name"], "lat": stops[j]["lat"], "lon": stops[j]["lon"], "km_from_start": round(ss[j], 2)},
            "km": round(x["km"], 2), "time_min": round(x["time_min"], 1), "time_src": x["time_src"], "leave_clock": hplan.hhmm(lv),
            "arrive_clock": hplan.hhmm(lv + x["time_min"]), "entry_clock": hplan.hhmm(lv + x["time_min"] + prep), "prep_min": prep,
            "status": x["status"], "status_label": offservice.STATUS_TXT[x["status"]], "status_text": x["status_text"], "findings": x["findings"][:8],
            "line": offservice.simplify(x["line"], 300), "traffic": x["traffic"], "roads": [q_["road"] for q_ in x["groups"]],
            "steps": hplan.instructions(x.get("steps") or [], stops[j]["code"], stops[j]["name"]),
            "alternatives": [{"label": o["label"], "km": round(o["km"], 2), "time_min": round(o["time_min"], 1), "status": o["status"], "tags": o["tags"]} for o in R["options"]],
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
            res["planner"] = {"ok": False, "reason": f"Route planner failed ({type(e).__name__}); the recommendation above still stands."}
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
TR = {"params": dict(traffic.PARAMS), "book": traffic.new_book(), "last": 0.0, "ridx": None, "ridx_key": None, "feeds": {}, "lock": None, "rw": [], "stretch_cache": None}


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
                if d and d.get("taken") and (_age_min(d["taken"]) or 1e9) <= 15:
                    c["taken"] = d["taken"]
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
        st, bs, inc, rain, (rw, rw_err) = await asyncio.gather(static(), bands_state(), api_incidents(), api_rain(), tr_roadworks(now))     # concurrent: a slow feed no longer holds up the others
        book = TR["book"]
        snap = int(CACHE["bands"][1]) if "bands" in CACHE else None
        count = traffic.counts(book, P, snap)
        if st["stops"] and st["routes"]:
            key = (len(st["routes"]), len(st["stops"]))
            if TR["ridx"] is None or TR["ridx_key"] != key:
                TR["ridx"] = await asyncio.to_thread(traffic.RouteIndex, st["routes"], st["stops"])
                TR["ridx_key"] = key
        feeds = {"bands": {"ok": bool(bs.get("idx")), "error": bs.get("error"), "segments": bs.get("usable"), "age_s": cache_age("bands")},
                 "incidents": {"ok": not inc.get("error"), "error": inc.get("error"), "count": len(inc.get("incidents", []))},
                 "roadworks": {"ok": not rw_err, "error": rw_err, "count": len(rw)},
                 "rain": {"ok": not rain.get("error"), "error": rain.get("error"), "gauges_wet": len(rain.get("stations", []))},
                 "routes": {"ok": TR["ridx"] is not None, "services": len({k[0] for k in st["routes"]}) if st["routes"] else 0}}
        # a feed that failed is skipped (never read as 'all clear'), so a broken feed cannot clear an active alert by mistake
        if bs.get("idx"):
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
            traffic.update_points(book, "incident", items, now, P, count)
        if not rw_err:
            traffic.update_points(book, "roadworks", rw[:300], now, P, count)
        if not rain.get("error"):
            traffic.update_weather(book, traffic.rain_cells(rain.get("stations", []), P), now, P, count)
        traffic.tick(book, now, P, snap)
        if TR["ridx"] is not None:
            await asyncio.to_thread(traffic.refresh_matches, book, TR["ridx"], P)
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
        out.update(segments=c["segments"], length_km=round(c["length_m"] / 1000, 2), avg_kmh=round(c["avg_kmh"], 1), min_kmh=round(c["min_kmh"], 1), ref_kmh=c["ref_kmh"], road=c["road"], center=c["center"],
                   very_slow_pct=round(c["very_slow_pct"]), from_=c["from"], to=c["to"], peak_min_kmh=round(e["peak"].get("min_kmh", c["min_kmh"]), 1), peak_len_km=round(e["peak"].get("max_len_m", c["length_m"]) / 1000, 2))
    elif e["kind"] == "incident":
        out.update(lat=c["lat"], lon=c["lon"], type=c.get("type"), message=c.get("message"), reported=c.get("reported"))
    elif e["kind"] == "roadworks":
        out.update(lat=c.get("lat"), lon=c.get("lon"), road=c.get("road"), start_date=tr_hhmm(c.get("start_epoch")) and datetime.fromtimestamp(c["start_epoch"], SGT).strftime("%d %b %H:%M"),
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
    line = cached_line(svc, direction, stops)
    step = max(1, len(line) // 500)
    return {"service": svc, "direction": direction, "line": [[round(p[0], 5), round(p[1], 5)] for p in line[::step]] + ([[round(line[-1][0], 5), round(line[-1][1], 5)]] if step > 1 else []),
            "stops": [[s["lat"], s["lon"], s["name"], s["dist"]] for s in stops]}


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
