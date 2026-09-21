"""SG Transport Pulse V5 - FastAPI backend.

Data sources
  LTA DataMall  (needs LTA_ACCOUNT_KEY): bus stops/routes, bus arrival, traffic speed bands, incidents
  NEA via data.gov.sg (no key needed):   real-time rainfall
  OSRM (public demo by default):         snaps the stop-to-stop route onto roads (optional, falls back)

LTA endpoint paths follow the DataMall API User Guide v6.9 (3 Aug 2026):
  v4/TrafficSpeedBands   (was probed as v3/... before, which now returns 404)
  v3/BusArrival          (was called BusArrivalv3, which is not a real path)
"""
import os, re, math, time, asyncio, json
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import headway

VERSION = "V9.0"
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
    if KEY:
        asyncio.create_task(warm())
        bb_init()
        task = asyncio.create_task(bb_loop())
    yield
    if task is not None:
        task.cancel()
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
    return keys[:BB_MAX_PAIRS]


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
            if BB_ALWAYS or t0 - BB["last_req"] < BB_IDLE_SEC:
                await bb_cycle()
            elif BB["open"]:
                bb_close_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(5.0, BB["params"]["refresh_sec"] - (time.time() - t0)))


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


@app.get("/halfway", response_class=HTMLResponse)
async def halfway_page():
    return HTMLResponse((HERE / "halfway.html").read_text(encoding="utf-8"))


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
HO_INT = ("n_trips", "reg_window", "recover_points")
HO_RANGES = {"n_trips": (5, 20), "layover_min": (0, 60), "min_layover_min": (0, 30), "start_delay_min": (-30, 60), "reg_hold_max": (0, 30), "reg_early_max": (0, 30), "reg_window": (1, 9),
             "min_dep_gap": (0, 10), "start_early_max": (0, 30), "start_late_max": (0, 60), "min_remaining_pct": (0, 90), "min_improve_pct": (0, 100), "max_mileage_km": (0.5, 100),
             "recover_tol_pct": (5, 100), "recover_points": (1, 8), "w_regularity": (0, 100), "w_maxgap": (0, 100), "w_recovery": (0, 100), "w_holding": (0, 100), "w_mileage": (0, 100),
             "w_bunching": (0, 100), "pref_recovery_boost": (1, 5), "pref_mileage_boost": (1, 10), "load_sens": (0, 0.3), "fallback_kmh": (5, 60), "offsvc_factor": (0.3, 1.0)}
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
    return {"service": svc, "direction": direction, "sched_hw": g["H"], "hw_src": g["H_src"], "ref": ho_hhmm(ref), "params": P, "n_stops": len(g["stops"]),
            "first": g["stops"][0]["name"], "last": g["stops"][-1]["name"], "route_km": round(g["prep"]["km"], 1), "run_min": round(g["tau"][-1], 1), "traffic_ok": g["traffic_ok"],
            "stops": [[s["code"], s["name"]] for s in g["stops"]], "points": [{"code": c["code"], "name": c["name"], "seq": c["seq"]} for c in cands if c.get("approved")], "n_approved": cinfo["approved"], "n_scan": len(cands) if cinfo["scanned"] else 0, "unresolved": unresolved,
            "updated": now.isoformat(timespec="seconds")}


@app.get("/api/halfway/simulate")
async def api_ho_simulate(service: str = "", direction: int = 1, ref: str = "", hw: str = "", layover: str = "", delay: str = "", late: str = "", disrupted: str = "", stop: str = "", save: str = "",
                          pref: str = "balanced", reg: str = "1", scope: str = "auto", veh: str = "own", ready: str = "", pick: str = ""):
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
    if disrupted.strip():
        try:
            dis = int(disrupted)
        except ValueError:
            return {"ok": False, "error": "Disrupted trip must be a trip number."}
    stops = g["stops"]
    scope = scope if scope in ("auto", "all", "approved") else "auto"
    veh = veh if veh in ("own", "standby") else "own"
    rdy = None
    if ready.strip():
        rdy = ho_parse_hhmm(ready)
        if rdy is None:
            return {"ok": False, "error": "Bus ready time must be a time like 08:54."}
    cands, unresolved, cinfo = ho_candidates(svc, direction, stops, stop.strip(), scope, g["prep"], {**P, **HO["params"]})
    ctx = {"H": H, "t0": t0, "late": lates, "disrupted": dis, "tau": g["tau"], "stop_s": g["prep"]["stop_s"], "route_km": g["prep"]["km"], "stop_names": [s["name"] for s in stops],
           "stop_seq": [s["seq"] for s in stops], "params": {**HO["params"], "layover_min": lay, "start_delay_min": dly}, "bunch_min": BB["params"]["bunch_min"], "candidates": cands,
           "pref": pref, "regulate": reg.strip() not in ("0", "false", "no", "off"), "veh": veh, "ready": rdy,
           "keep": [c for c in pick.split(",") if re.fullmatch(r"\d{5}", c.strip())][:6]}
    res = halfway.simulate(ctx)
    if not res["ok"]:
        return res
    cum, line, ss = g["prep"]["cum"], g["line"], g["prep"]["stop_s"]
    tm = g["tm"]

    def section(a_, b_):
        km = max(0.0, ss[b_] - ss[a_])
        mins = tm.t(ss[a_], ss[b_]) if tm is not None else km / P["fallback_kmh"] * 60.0
        kmh = km / (mins / 60.0) if mins > 0 else None
        return {"km": round(km, 2), "min": round(mins, 1), "kmh": round(kmh, 1) if kmh else None, "label": ho_speed_label(kmh) if kmh else None}
    dis_trip = res["trips"][dis - 1] if dis is not None else None
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
    if save.strip() and dis is not None:
        res["run_id"] = ho_audit(res, lates)
    return res


def ho_audit(res, lates):
    best = res["options"][res["recommended"]] if res.get("recommended") is not None else None
    scen = [{"kind": o["kind"], "label": o["label"], "code": o.get("code"), "score": o.get("score"), "viable": o.get("viable"), "hold_min": o.get("hold_min"), "skipped_km": o.get("skipped_km"),
             "max_a": (o.get("metrics_a") or {}).get("max"), "max_b": (o.get("metrics") or {}).get("max"), "avg_a": (o.get("metrics_a") or {}).get("avg"), "avg_b": (o.get("metrics") or {}).get("avg"),
             "recovery": o.get("recovery"), "violations": o["violations"]} for o in res["options"]]
    bb_sql("INSERT INTO halfway_sim(ts,service,direction,ref_time,sched_hw,disrupted,lateness,start_delay,recommended,improvement_pct,no_halfway,best,scenarios,params,model_version,pref,rec_label,score,hold_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (time.time(), res["service"], res["direction"], res["ref"], res["sched_hw"], res["disrupted"], json.dumps(lates), res["start_delay"], (best.get("code") or best["kind"]) if best else None,
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
