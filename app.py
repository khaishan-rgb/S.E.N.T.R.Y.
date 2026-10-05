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
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import headway
import routegeom
import traffic

VERSION = "V16.16"
# V16.8: pages hidden for everyone (see Settings > Pages). Defined here because bb_init() reads the saved value while the module loads.
SITE_PAGE_IDS = ("command", "route", "headway", "bunching", "recovery", "halfplan", "trafficaware", "running", "ewt", "cameras", "diversion")     # "settings" can never be hidden
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
    yield
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


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse((HERE / "index.html").read_text(encoding="utf-8"))


@app.get("/occ-live", response_class=HTMLResponse)
async def page_occ_live():
    return HTMLResponse((HERE / "occ_live.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    return {"online": True, "lta": bool(KEY), "version": VERSION, "time": now_sgt().isoformat(timespec="seconds"), "speedEndpoint": GOOD_SPEED_PATH, "tomtom": bool(TT["key"])}


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


# ---- V15.9 shared command-platform design system + single-source navigation registry.
# Loaded by pages one at a time as they are redesigned; pages that don't include them are unaffected.
@app.get("/design-system.css")
async def design_system_css():
    from fastapi.responses import Response
    css = (HERE / "design_system.css").read_text(encoding="utf-8")
    return Response(css, media_type="text/css", headers={"Cache-Control": "public, max-age=600"})


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
                    "partial": total < fit_total, "test_gap": LG}
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
                          "offset": off, "stop_min": sm, "gap": LG, "gap_at": gap_at if LG else None, "layover": lay,
                          "waiting": sum(1 for b in tb + ob if b.get("wait") is not None)},
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
async def api_hp_plan(snap: str = "", bus: str = "", delay: str = "20", brk: str = "", stop_min: str = "", layover: str = "",
                      mode: str = "late", os_from: str = "", os_time: str = ""):
    """Late bus completes its trip; its NEXT trip starts halfway. Every stop of the next direction is tested with real-road off-service
    time from the interchange; front bus hold / rear bus advance chosen per stop; ranked by downstream EWT. Live data unchanged."""
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
                                                       "late": {"bus": int(bus), "delay": D} if has_bus and D > 0 else None}, reach)
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
TT_INC_TTL = 120 if TT_FIRST else 300   # one call for the whole island: every 2 min when TomTom finds jams first, else every 5 min
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
            hint = {401: "key rejected", 403: "key is not enabled for this TomTom product", 429: "too many calls"}.get(code, "")
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
            "fields": "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,events{description,code,iconCategory},from,to,roadNumbers,length,delay}}}"})
        r.raise_for_status()
        out, jams = [], []
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
    return HTMLResponse((HERE / name).read_text(encoding="utf-8"))


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _page("login.html")


@app.get("/command", response_class=HTMLResponse)
async def command_page():
    return _page("command.html")


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
_GATE_OPEN = ("/login", "/api/auth/", "/api/health", "/app-shell.js", "/design-system.css", "/basemap.js", "/nav-registry.js", "/favicon")


@app.middleware("http")
async def login_gate(request: Request, call_next):
    if not auth.config()["enabled"] or os.getenv("AUTH_GATE", "1") == "0":
        return await call_next(request)
    path = request.url.path
    if request.method == "OPTIONS" or path.startswith(_GATE_OPEN) or auth.read(request.cookies.get(auth.COOKIE, "")):
        return await call_next(request)
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
    config = {"lta_key": bool(KEY), "datagov_key": bool(DATAGOV_KEY), "onemap_routing": bool(routegeom.ONEMAP_EMAIL and routegeom.ONEMAP_PASSWORD),
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
    diversion.PARAMS["block_buffer_m"] = max(15.0, min(100.0, float(os.getenv("DIVERSION_BLOCK_BUFFER_M", "35"))))   # exclusion zone around a blockage
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
    for i, b in enumerate(blocks):
        for r in await asyncio.to_thread(diversion.overlap_runs, line, b["line"], b["directed"]):
            runs.append({"a": r["a"], "b": r["b"], "len": r["len"], "block": i})
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
        if not tt_spend():
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
        if dv_use_tomtom():
            res = await tt_route(coords, alternatives, bearings, DV_BUS.get(), avoid)
            if res.get("routes"):
                return res
        res = await os_osrm(coords, alternatives=alternatives, bearings=bearings, continue_straight=True)
        if bearings and not res.get("routes"):
            res = await os_osrm(coords, alternatives=alternatives, continue_straight=True)
        for r in res.get("routes") or []:
            r.setdefault("router", "OSRM (OpenStreetMap)")
        return res


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
    """the closed sections as small rectangles (TomTom avoidAreas, max 10): ~80 m pieces along each block, padded 12 m"""
    rects = []
    total = sum(diversion.line_m(b["line"]) for b in blocks) or 1.0
    piece = max(80.0, total / max_n)
    for b in blocks:
        ln = b["line"]
        cm = diversion.cum_m(ln)
        n = max(1, int(math.ceil(cm[-1] / piece)))
        for k in range(n):
            part = diversion.cut(ln, cm, cm[-1] * k / n, cm[-1] * (k + 1) / n)
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

    def heading(p):
        return diversion.nearest_on_line(p, line, cum)[3]

    def at(s_):
        return diversion.point_at(line, cum, max(0.0, min(cum[-1], s_)))
    donors = dv_donors(blocks, (svc, d))
    avoid = dv_avoid_rects(blocks, pad_m=P["block_buffer_m"]) if dv_use_tomtom() else None
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
    unavoidable = sum(1 for x in stop_s if A - P["block_buffer_m"] < x < B + P["block_buffer_m"])
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


async def route_between_stops(ctx, i, j, s0=None):
    """road routes from (just after) stop i to (just before) stop j, from the routing engine only: TomTom with the
    blockage + buffer as avoid areas and up to 3 alternatives; OSRM alternatives and routes via nearby bus roads;
    the controller's drawn points and other services' corridors as extra waypoint sets. -> [route]"""
    P, at, stop_s = ctx["P"], ctx["at"], ctx["stop_s"]
    st0 = stop_s[i] + 5.0 if s0 is None else s0       # s0: a junction probe further along the original route
    a_ = at(st0)
    b_ = at(max(stop_s[j] - 5.0, st0 + 10.0))
    h0, h1 = ctx["heading"](a_), ctx["heading"](b_)
    reqs = [([a_, b_], P["alt_per_pair"], True, "router")]          # source codes: router | busvia | manual | adopt
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
    res = await asyncio.gather(*[dv_osrm(c, alt, [h0] + [None] * (len(c) - 2) + [h1], ctx["avoid"] if av else None) for c, alt, av, _ in reqs])
    out, err = [], None
    for x, (_, _, _, src) in zip(res, reqs):
        err = err or x.get("error")
        for r in x.get("routes") or []:
            out.append(dict(r, _src=src))
    return out, err


async def validate_next_original_leg(ctx, j):
    """MANDATORY: from rejoin stop D the bus must be able to continue to the next original stop E on the original
    route, in the right direction, with no U-turn and clear of the blockage (routing engine, cached per stop)."""
    key = (ctx.get("bkey"), ctx["svc"], ctx["d"], j)
    if len(ctx["next_cache"]) > 3000:
        ctx["next_cache"].clear()
    if key in ctx["next_cache"]:
        return ctx["next_cache"][key]
    stops, stop_s, at, P = ctx["stops"], ctx["stop_s"], ctx["at"], ctx["P"]
    if j + 1 >= len(stops):
        res = {"ok": True, "next_code": "", "next_name": "end of route", "detail": "rejoin stop is the last stop of the route"}
        ctx["next_cache"][key] = res
        return res
    a_, b_ = at(stop_s[j] + 5.0), at(max(stop_s[j + 1] - 5.0, stop_s[j] + 10.0))
    normal = stop_s[j + 1] - stop_s[j]
    rr = await dv_osrm([a_, b_], 0, [ctx["heading"](a_), ctx["heading"](b_)], ctx["avoid"])
    res = {"ok": False, "next_code": stops[j + 1]["code"], "next_name": stops[j + 1]["name"], "detail": "no road route to the next stop"}
    for r in rr.get("routes") or []:
        ok, why = diversion.validate_no_uturn(r["line"], r.get("steps"), ctx["line"], ctx["cum"], stop_s[j], P)
        if not ok:
            res["detail"] = "next leg " + why
            continue
        ok, why = diversion.validate_block_avoidance(r["line"], ctx["block_lines"], None, P)
        if not ok:
            res["detail"] = "next leg " + why
            continue
        m = r["km"] * 1000.0
        if abs(m - normal) > max(P["next_leg_tol_abs_m"], P["next_leg_tol_rel"] * normal):
            res["detail"] = f"next leg is {m:.0f} m by road vs {normal:.0f} m on the original route"
            continue
        pts = diversion.densify(r["line"], 20.0)
        on = sum(1 for p in pts if diversion.nearest_on_line(p, ctx["line"], ctx["cum"])[0] <= 30.0) / max(1, len(pts))
        if on < P["next_leg_on_route"]:
            res["detail"] = f"next leg leaves the original route ({on * 100:.0f}% on it)"
            continue
        res = {"ok": True, "next_code": stops[j + 1]["code"], "next_name": stops[j + 1]["name"], "detail": f"{m:.0f} m on the original route"}
        break
    ctx["next_cache"][key] = res
    return res


async def dv_validate_route(ctx, r, i, j, s0=None):
    """HARD validation of one engine route for the pair (i, j), in order. -> (candidate | None, reject reason)"""
    P, line, cum, stop_s, A, B = ctx["P"], ctx["line"], ctx["cum"], ctx["stop_s"], ctx["A"], ctx["B"]
    if len(r.get("line") or []) < 2:
        return None, "No verified road connection found"
    st0 = stop_s[i] + 5.0 if s0 is None else s0
    a_, b_ = ctx["at"](st0), ctx["at"](stop_s[j] - 5.0)
    if diversion.dist_m(r["line"][0], a_) > P["ends_tol_m"] or diversion.dist_m(r["line"][-1], b_) > P["ends_tol_m"]:
        return None, "route cannot physically reach the stops (road network not connected there)"
    ok, why = diversion.validate_block_avoidance(r["line"], ctx["block_lines"], None, P)
    if not ok:
        return None, why
    ok, why = await asyncio.to_thread(diversion.validate_no_uturn, r["line"], r.get("steps"), line, cum, st0 - 5.0, P)
    if not ok:
        return None, why
    dep = await asyncio.to_thread(diversion.departure, r["line"], line, cum, None, (st0 - 45.0, st0 + 60.0))
    ok, why = diversion.validate_rejoin_direction(dep, stop_s[j], B, P)
    if not ok:
        return None, why
    if dep["leave_s"] > A + 5.0:
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
    # bus-road evidence: services in the same direction (level 1), bus stops (level 2), road class (level 3)
    box = diversion.bbox(seg, 300)
    net = await asyncio.to_thread(dv_busnet, st_ctx(ctx), box)
    # the stops the bus REALLY serves: last one before it leaves its route, first one after it rejoins (same rule as the
    # skipped-stop list) - the requested pair (i, j) is only where the routing engine was asked to go
    li = max([k for k, x in enumerate(stop_s) if x <= dep["leave_s"] + 1.0], default=i)
    rj = min([k for k, x in enumerate(stop_s) if x >= dep["rejoin_s"] - 1.0], default=j)
    c = {"r": r, "dep": dep, "seg": seg, "groups": keep, "signature": diversion.road_signature(keep), "div_m": div_m,
         "src": r.get("_src", "router"), "pair": (li, rj), "asked": (i, j), "road_class": diversion.road_mix(cl)}
    br = await asyncio.to_thread(dv_bus_road, c, net, ctx["bus"], bt_table())
    if ctx["bus"] == "dd" and br.get("dd") == "UNCONFIRMED":
        unk = [sv for sv in br.get("services", []) if bt_class(sv) is None]
        if unk and await dv_probe_types(st_ctx(ctx), unk, box):
            br = await asyncio.to_thread(dv_bus_road, c, net, ctx["bus"], bt_table())
    stops_along = await asyncio.to_thread(dv_stops_along, st_ctx(ctx), seg)
    ev = diversion.validate_bus_road_evidence(br, stops_along, div_m / 1000.0, ctx["bus"])
    if not ev["ok"]:
        return None, ev["text"]
    c.update(bus_road=br, evidence=ev, suit=suit)
    return c, ""


def st_ctx(ctx):
    return ctx["st"]


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
                    reasons.append(f"rejoin stop {stops[c['pair'][1]]['code']} fails the next-leg check: {nxt['detail']}")
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
        r = bb_sql("SELECT value FROM dv2_settings WHERE key='weights'", fetch=True)
        if r:
            w = json.loads(r[0]["value"])
            return {k: float(w.get(k, v)) for k, v in dv2.DEFAULT_WEIGHTS.items()}
    except Exception:
        pass
    return dict(dv2.DEFAULT_WEIGHTS)


@app.get("/api/dv2/config")
async def api_dv2_config():
    return {"tomtom_map": bool(TT["key"]), "router": "TomTom Routing (bus mode)" if dv_use_tomtom() else "OSRM (OpenStreetMap)",
            "weights": dv2_weights(), "weight_labels": dv2.WEIGHT_LABELS, "default_weights": dv2.DEFAULT_WEIGHTS,
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
    bb_sql("INSERT OR REPLACE INTO dv2_settings(key, value, by_name, ts) VALUES('weights', ?, ?, ?)", (json.dumps(w), _who(request), time.time()))
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
    rn = sorted([n for n in names if ql in n.lower()], key=lambda n: (not n.lower().startswith(ql), -names[n][4]))[:6]
    for n in rn:
        e = names[n]
        out.append({"type": "road", "label": dv2_title(n), "road": n, "sub": "Road", "lat": (e[0] + e[2]) / 2, "lon": (e[1] + e[3]) / 2,
                    "bbox": [e[0], e[1], e[2], e[3]]})
    sm = [x for x in st["stops"].values() if ql in x["name"].lower() or ql in (x.get("road") or "").lower()]
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
    """the blocked section as engine blocks: dir fwd (drawn order) / rev / both"""
    line = [tuple(p) for p in (body.get("line") or []) if isinstance(p, (list, tuple)) and len(p) == 2]
    if len(line) < 2:
        return []
    dirn = {"fwd": "fwd", "forward": "fwd", "rev": "rev", "back": "rev", "backward": "rev", "reverse": "rev", "both": "both"}.get(
        str(body.get("dir") or "both").strip().lower())
    if dirn is None:            # unknown direction: refuse rather than silently treat it as one-way
        return []
    if dirn == "rev":
        line = list(reversed(line))
    return [{"line": line, "directed": dirn != "both", "road": str(body.get("road") or "")[:80]}]


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


async def dv2_plan(st, blocks, svc, d, run_i, traffic=True):
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
    avoid = dv_avoid_rects(blocks, pad_m=P["block_buffer_m"]) if dv_use_tomtom() else None
    sec_line = diversion.cut(line, cum, A, B)
    bvias = []
    if not avoid and len(sec_line) >= 2:
        try:
            bvias = await asyncio.to_thread(dv_busroad_vias, await asyncio.to_thread(dv_busnet, st, diversion.bbox(sec_line, 1500), 0.2), sec_line)
        except Exception:
            bvias = []

    def heading(p):
        return diversion.nearest_on_line(p, line, cum)[3]

    def at(s_):
        return diversion.point_at(line, cum, max(0.0, min(cum[-1], s_)))
    ctx = {"P": P, "st": st, "svc": svc, "d": d, "line": line, "cum": cum, "stops": stops, "stop_s": stop_s, "A": A, "B": B, "bus": bus,
           "idx": idx, "at": at, "heading": heading, "avoid": avoid, "bvias": bvias, "manual": None, "donors": [],
           "block_lines": [(b_["line"], bool(b_["directed"])) for b_ in blocks], "next_cache": DV_NEXT_CACHE, "bkey": dv_block_key(blocks)}
    ia = diversion.find_last_reachable_stop(stop_s, A, None, P)
    down = diversion.get_downstream_rejoin_candidates(stop_s, B, None, None, P)
    unavoidable = sum(1 for x in stop_s if A - P["block_buffer_m"] < x < B + P["block_buffer_m"])
    base = {"ok": True, "service": svc, "direction": d, "destination": stops[-1]["name"] if stops else "", "bus_type": bus.upper(),
            "line_source": src, "router": "TomTom" if dv_use_tomtom() else "OSRM", "traffic": traffic,
            "last_reachable": {"code": stops[ia]["code"], "name": stops[ia]["name"]} if ia is not None else None,
            "first_after": {"code": stops[down[0]]["code"], "name": stops[down[0]]["name"]} if down else None,
            "unavoidable": unavoidable, "block_a": round(A, 1), "block_b": round(B, 1)}
    win = diversion.cut(line, cum, max(0.0, A - 3000.0), min(cum[-1], B + 3000.0))
    base["original_line"] = [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(win, 500)]
    base["stops"] = [{"code": stops[k]["code"], "name": stops[k]["name"], "lat": stops[k]["lat"], "lon": stops[k]["lon"], "s": round(stop_s[k]),
                      "in_block": A - P["block_buffer_m"] < stop_s[k] < B + P["block_buffer_m"]}
                     for k in range(len(stops)) if A - 3000.0 <= stop_s[k] <= B + 3000.0]
    if ia is None or not down:
        base.update(status="none", headline="NO VERIFIED DIVERSION FOUND", attempts=[], candidates=[],
                    failures=["No original stop before the blockage the bus can still serve." if ia is None else "No original stop after the blockage to rejoin at."])
        return base
    weights = dv2_weights()
    b0 = down[0]
    evaluated, attempts, valid_all = {}, [], []

    def probes(i):
        """junction probes after stop i: points further along the ORIGINAL route (latest first, short of the exclusion zone
        and of the next stop) - the bus keeps following its normal path and may only turn off at a later junction"""
        lim = min(A - P["block_buffer_m"] - 25.0, (stop_s[i + 1] - 20.0) if i + 1 < len(stop_s) else 1e18)
        pts = [stop_s[i] + off for off in (150.0, 300.0, 450.0) if stop_s[i] + off < lim]
        return sorted(pts, reverse=True)

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
            ap = diversion.nearest_on_line(c["dep"]["leave_pt"], line, cum)[3]
            dp = diversion.bearing(at(c["dep"]["rejoin_s"]), at(c["dep"]["rejoin_s"] + 25.0))
            rj_road = dv2._road_starting_near(r.get("steps"), c["dep"]["rejoin_pt"], 40.0)     # the road the bus rejoins
            instr = dv2.instructions(r.get("steps"), c["seg"], ap, dp, rj_road)
            bad = dv2.impossible_turn(instr)
            if bad:
                reasons.append(bad)
                continue
            nxt = await validate_next_original_leg(ctx, c["pair"][1])
            if not nxt["ok"]:
                reasons.append(f"rejoin stop {stops[c['pair'][1]]['code']} fails the next-leg check: {nxt['detail']}")
                continue
            c["instr"], c["next_leg"], c["probe"] = instr, nxt, s0 is not None
            if any(o["signature"] == c["signature"] and abs(o["div_m"] - c["div_m"]) < 150 and o["pair"] == c["pair"] for o in ok):
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
        cx = dv2.complexity(c["instr"], c["groups"], c["suit"])
        bl, bsc = dv2.busroad_label(c["evidence"], c["suit"])
        dd_doubt = bus == "dd" and (c.get("bus_road") or {}).get("dd") == "UNCONFIRMED"
        m = {"leave_index": li, "rejoin_index": rj, "leave_name": stops[li]["name"], "leave_code": stops[li]["code"],
             "rejoin_name": stops[rj]["name"], "rejoin_code": stops[rj]["code"], "earlier": li < ia, "leave_after_ia": li - ia,
             "skipped": [{"code": stops[k]["code"], "name": stops[k]["name"], "lat": stops[k]["lat"], "lon": stops[k]["lon"],
                          "in_block": A - P["block_buffer_m"] < stop_s[k] < B + P["block_buffer_m"]} for k in sk],
             "skipped_n": len(sk), "unavoidable": unavoidable, "extra_skipped": max(0, len(sk) - unavoidable),
             "div_km": round(c["div_m"] / 1000.0, 2), "normal_km": round((rejoin_s - leave_s) / 1000.0, 2),
             "added_km": round((c["div_m"] - (rejoin_s - leave_s)) / 1000.0, 2), "added_min": round(div_min - normal_min, 1),
             "rejoin_after_m": round(rejoin_s - B), "simplicity": cx["simplicity"], "turns": cx["turns"], "sharp": cx["sharp"],
             "road_changes": cx["road_changes"], "minor_share": cx["minor_share"], "junction": dv2.junction_quality(c["instr"]),
             "busroad_label": bl, "busroad_score": bsc, "dd_doubt": dd_doubt, "evidence_text": c["evidence"]["text"],
             "route_conf": (1.0 if str(r.get("router", "")).startswith("TomTom") else 0.8) - (0.2 if c["evidence"]["level"] == 3 else 0.0),
             "next_ok": bool(c["next_leg"]["ok"]), "next_name": c["next_leg"]["next_name"], "next_code": c["next_leg"]["next_code"],
             "instructions": c["instr"], "router": r.get("router", ""), "probe": bool(c.get("probe"))}
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
            break
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
    # distinct candidates (same roads + same stops = one)
    uniq = []
    for c in sorted(valid_all, key=lambda c: -c["score"]):
        if not any(u["signature"] == c["signature"] and u["pair"] == c["pair"] for u in uniq):
            uniq.append(c)
    strong = [c for c in uniq if c["confidence"] in ("HIGH", "MEDIUM")]
    attempts.sort(key=lambda a: (a["extra"], a["earlier"]))
    base["attempts"] = attempts
    base["search"] = {"pairs": len(attempts), "routes": sum(a["routes"] for a in attempts), "levels_used": (strong_at if strong_at is not None else len(levels) - 1)}
    ctx_m = {"ia_name": stops[ia]["name"]}

    def summary(c, tags=()):
        m = c["m"]
        seg = c["seg"]
        skip_line = diversion.cut(line, cum, c["dep"]["leave_s"], c["dep"]["rejoin_s"])
        return {"id": f"{m['leave_code']}-{m['rejoin_code']}-{c['signature'][:24]}", "tags": list(tags), "score": c["score"], "parts": c["parts"],
                "confidence": c["confidence"], "busroad_label": m["busroad_label"], "evidence_text": m["evidence_text"],
                "leave": {"code": m["leave_code"], "name": m["leave_name"]}, "rejoin": {"code": m["rejoin_code"], "name": m["rejoin_name"]},
                "next": {"code": m["next_code"], "name": m["next_name"], "ok": m["next_ok"]},
                "roads": [dv2_title(g["road"]) for g in c["groups"] if g.get("road")], "instructions": m["instructions"],
                "instruction_text": dv2.instruction_text(m["instructions"], m["rejoin_name"]),
                "skipped": m["skipped"], "skipped_n": m["skipped_n"], "unavoidable": m["unavoidable"], "extra_skipped": m["extra_skipped"],
                "div_km": m["div_km"], "normal_km": m["normal_km"], "added_km": m["added_km"], "added_min": m["added_min"],
                "turns": m["turns"], "sharp": m["sharp"], "earlier": m["earlier"], "router": m["router"],
                "line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(seg, 400)],
                "skip_line": [[round(p[0], 6), round(p[1], 6)] for p in diversion.simplify(skip_line, 200)],
                "leave_pt": [round(c["dep"]["leave_pt"][0], 6), round(c["dep"]["leave_pt"][1], 6)],
                "rejoin_pt": [round(c["dep"]["rejoin_pt"][0], 6), round(c["dep"]["rejoin_pt"][1], 6)]}

    pool = strong or uniq
    if not pool:
        base.update(status="none", headline="NO VERIFIED DIVERSION FOUND", candidates=[], failures=dv2.explain_failure(attempts),
                    why=[], why_not=[])
        return base
    best = max(pool, key=lambda c: (c["confidence"] != "LOW", c["score"]))
    early_alts = [c for c in uniq if c["m"]["leave_index"] < best["m"]["leave_index"]]
    best["m"]["preserved_vs_early"] = max([c["m"]["skipped_n"] - best["m"]["skipped_n"] for c in early_alts] + [0])
    tags = dv2.tag_alternatives(pool)
    tag_of = {}
    for t, c in tags.items():
        tag_of.setdefault(id(c), []).append(t)
    others = [c for c in uniq if c is not best][:5]
    base["status"] = "divert" if best["confidence"] in ("HIGH", "MEDIUM") else "review"
    base["headline"] = ("RECOMMENDED DIVERSION" if base["status"] == "divert" else "CONTROLLER REVIEW REQUIRED \u2014 LOW CONFIDENCE ONLY")
    base["best"] = summary(best, tag_of.get(id(best), []))
    base["candidates"] = [base["best"]] + [summary(c, tag_of.get(id(c), [])) for c in others]
    base["why"] = dv2.why_this(best, ctx_m)
    base["why_not"] = [dv2.why_not(best, c, ctx_m) for c in sorted(others, key=lambda c: c["m"]["leave_index"])[:4]]
    base["failures"] = dv2.explain_failure(attempts)
    base["weights"] = weights
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
    st = await static()
    key = "dv2plan:" + hashlib.sha1(json.dumps([[[round(p[0], 5), round(p[1], 5)] for p in b["line"]] for b in blocks]
                                               + [blocks[0]["directed"], svc, d, run_i, traffic, dv2_weights()]).encode()).hexdigest()[:24]

    async def factory():
        r = await dv2_plan(st, blocks, svc, d, run_i, traffic)
        return r, 120, bool(r.get("ok"))
    return copy.deepcopy(await cached(key, factory))
