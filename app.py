"""SG Transport Pulse V5 - FastAPI backend.

Data sources
  LTA DataMall  (needs LTA_ACCOUNT_KEY): bus stops/routes, bus arrival, traffic speed bands, incidents
  NEA via data.gov.sg (no key needed):   real-time rainfall
  OSRM (public demo by default):         snaps the stop-to-stop route onto roads (optional, falls back)

LTA endpoint paths follow the DataMall API User Guide v6.9 (3 Aug 2026):
  v4/TrafficSpeedBands   (was probed as v3/... before, which now returns 404)
  v3/BusArrival          (was called BusArrivalv3, which is not a real path)
"""
import os, re, math, time, asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

import headway

VERSION = "V5.1"
LTA = os.getenv("LTA_BASE", "https://datamall2.mytransport.sg/ltaodataservice").rstrip("/")
KEY = os.getenv("LTA_ACCOUNT_KEY", "")
OSRM = os.getenv("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
DATAGOV_KEY = os.getenv("DATA_GOV_SG_KEY", "")  # optional, only raises data.gov.sg rate limits
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
    async def factory():
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
        return {"line": line, "source": source}, (TTL_GEOM if bad == 0 else 600), True
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
    async def factory():
        d = await get_lta(EP_ARRIVAL, {"BusStopCode": stop, **({"ServiceNo": service} if service else {})})
        return d, TTL_ARRIVAL, not d.get("_error")
    return await cached(f"arr:{stop}:{service}", factory)


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
    if KEY:
        asyncio.create_task(warm())
    yield
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
        "runs": runs, "geometry": geom["source"],
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
                break
        else:
            clusters.append({**c, "srcs": {c["src"]}, "etas": {c["src"]: c["eta"]}})
    buses = []
    for cl in clusters:
        near_i = min(range(n), key=lambda k: hav_km(cl["lat"], cl["lon"], stops[k]["lat"], stops[k]["lon"]))
        near, to = stops[near_i], stops[cl["src"]]
        buses.append({"lat": cl["lat"], "lon": cl["lon"], "load": cl["load"], "type": cl["type"], "wab": cl["wab"], "monitored": cl["monitored"],
                      "eta": cl["eta"], "toStop": {"code": to["code"], "name": to["name"]},
                      "near": {"code": near["code"], "name": near["name"], "road": near["road"]}, "progress": near_i, "etas": cl["etas"]})
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
            fq[(svc, d)] = {k: r.get(k + "_Freq") for k in ("AM_Peak", "AM_Offpeak", "PM_Peak", "PM_Offpeak")}
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


@app.get("/control", response_class=HTMLResponse)
async def control_page():
    return HTMLResponse((HERE / "control.html").read_text(encoding="utf-8"))


@app.get("/api/control")
async def api_control(services: str = "", direction: int = 0, adj: str = "", plan: str = ""):
    """Evaluate the five pre-emptive departure checks for each selected service + direction.
    adj  = HW the controller has already applied, e.g. '32:1:13'   plan = timetable running time (min), e.g. '32:1:45'"""
    svcs = []
    for t in re.split(r"[,\s]+", services.upper()):
        if t and re.fullmatch(r"[0-9A-Z]{1,6}", t) and t not in svcs:
            svcs.append(t)
    dirs = [direction] if direction in (1, 2) else [1, 2]
    combos = [(s, d) for s in svcs for d in dirs][:MAX_COMBOS]
    adj_m, plan_m = parse_triples(adj, int), parse_triples(plan, float)
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
               "sched": headway.sched_hw(fq["freqs"].get((svc, d)), now), "adj_hw": adj_m.get((svc, d)), "plan_override": plan_m.get((svc, d))}
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
            "truncated": len(svcs) * len(dirs) > MAX_COMBOS, "freqOk": bool(fq["freqs"]), "freqError": fq.get("error")}


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
