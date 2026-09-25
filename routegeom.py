"""Bus route geometry for SG Transport Pulse (V13.11).

LTA DataMall's BusRoutes only gives the ordered list of stops, not the road path, so the line between stops has to come from
somewhere else. Order of preference:

1. busrouter.sg  - https://data.busrouter.sg/v1/routes.min.json: one encoded polyline per service and direction, already
                   following the actual bus path (community dataset built from LTA route data). It is checked against the
                   LTA stop list before use: most stops must lie on the line, in order.
2. OneMap        - Singapore Land Authority routing (drive), one request per stop-to-stop leg. Needs ONEMAP_EMAIL and
                   ONEMAP_PASSWORD (free OneMap account). Knows left-hand driving and Singapore turn rules.
3. OSRM          - the old public demo router (handled in app.py).
4. Straight lines between stops.
"""
import asyncio
import math
import os
import time

BUSROUTER_URL = os.getenv("BUSROUTER_ROUTES_URL", "https://data.busrouter.sg/v1/routes.min.json")
ONEMAP_EMAIL = os.getenv("ONEMAP_EMAIL", "")
ONEMAP_PASSWORD = os.getenv("ONEMAP_PASSWORD", "")
ONEMAP_BASE = "https://www.onemap.gov.sg"

ON_LINE_M = 60.0      # a stop counts as "on the line" within this distance
MIN_MATCH = 0.85      # share of stops that must be on the line, in order, to accept a busrouter line


# --------------------------------------------------------------------------- polyline + geometry helpers
def decode_polyline(s, precision=5):
    """Google encoded polyline -> [(lat, lon), ...]."""
    out, i, lat, lon, f = [], 0, 0, 0, 10 ** precision
    n = len(s)
    while i < n:
        vals = []
        for _ in range(2):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                res |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(res >> 1) if res & 1 else res >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append((lat / f, lon / f))
    return out


def _xy(lat, lon, lat0):
    return lon * 111320.0 * math.cos(math.radians(lat0)), lat * 110574.0


def _seg_dist(p, a, b, lat0):
    """Distance in metres from point p to segment a-b, and the projected point (lat, lon)."""
    px, py = _xy(p[0], p[1], lat0)
    ax, ay = _xy(a[0], a[1], lat0)
    bx, by = _xy(b[0], b[1], lat0)
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    qx, qy = ax + t * dx, ay + t * dy
    d = math.hypot(px - qx, py - qy)
    return d, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def match_stops(line, stops):
    """Walk the stops along the line, forward only. Returns (share_on_line, seg_index_per_stop, projected_points).
    For each stop, take the first stretch of the remaining line that comes within ON_LINE_M (and follow it to its closest
    point), so loop services that start and end at the same interchange are handled in order."""
    if len(line) < 2 or not stops:
        return 0.0, [], []
    lat0 = stops[0]["lat"]
    prev, hits, segs, projs = 0, 0, [], []
    nseg = len(line) - 1
    for s in stops:
        p = (s["lat"], s["lon"])
        best = None
        j = prev
        while j < nseg:
            d, q = _seg_dist(p, line[j], line[j + 1], lat0)
            if d <= ON_LINE_M:
                best = (d, j, q)
                k = j + 1                        # follow while it keeps getting closer
                while k < nseg:
                    d2, q2 = _seg_dist(p, line[k], line[k + 1], lat0)
                    if d2 > best[0]:
                        break
                    best = (d2, k, q2)
                    k += 1
                break
            j += 1
        if best is None:                         # not on the line: note the nearest remaining point but do not move forward
            segs.append(None)
            projs.append(None)
            continue
        hits += 1
        prev = best[1]
        segs.append(best[1])
        projs.append(best[2])
    return hits / len(stops), segs, projs


def fit_line(line, stops):
    """Best orientation of one candidate line for these stops, trimmed to first..last stop. -> (score, trimmed_line)."""
    best = (0.0, None)
    for cand in (line, line[::-1]):
        score, segs, projs = match_stops(cand, stops)
        if score <= best[0]:
            continue
        idx = [(i, sg) for i, sg in enumerate(segs) if sg is not None]
        if len(idx) < 2:
            continue
        (i0, s0), (i1, s1) = idx[0], idx[-1]
        trimmed = [projs[i0]] + cand[s0 + 1:s1 + 1] + [projs[i1]]
        # stops before the first / after the last matched stop are joined straight, so nothing is cut off
        trimmed = [(s["lat"], s["lon"]) for s in stops[:i0]] + trimmed + [(s["lat"], s["lon"]) for s in stops[i1 + 1:]]
        best = (score, trimmed)
    return best


# --------------------------------------------------------------------------- busrouter.sg
_BR = {"data": None, "at": 0.0, "err": None}


async def busrouter_routes(client, ttl=24 * 3600):
    if _BR["data"] is not None and time.time() - _BR["at"] < ttl:
        return _BR["data"]
    try:
        r = await client.get(BUSROUTER_URL, timeout=30)
        r.raise_for_status()
        _BR.update(data=r.json(), at=time.time(), err=None)
    except Exception as e:
        _BR["err"] = f"busrouter.sg: {type(e).__name__}"
        if _BR["data"] is None:
            return {}
    return _BR["data"]


async def busrouter_line(client, svc, stops):
    """-> (line, info) or (None, reason)."""
    data = await busrouter_routes(client)
    polys = data.get(svc) or data.get(svc.upper()) or []
    if not polys:
        return None, _BR["err"] or f"busrouter.sg has no line for service {svc}"

    def work():
        best = (0.0, None)
        for enc in polys:
            try:
                ln = decode_polyline(enc)
            except Exception:
                continue
            sc, tr = fit_line(ln, stops)
            if sc > best[0]:
                best = (sc, tr)
        return best
    score, line = await asyncio.to_thread(work)
    if line is None or score < MIN_MATCH:
        return None, f"busrouter.sg line matched only {round(score * 100)}% of LTA stops"
    return line, f"busrouter.sg, {round(score * 100)}% of stops on the line"


# --------------------------------------------------------------------------- OneMap (Singapore Land Authority)
_OM = {"token": None, "exp": 0.0}


def onemap_enabled():
    return bool(ONEMAP_EMAIL and ONEMAP_PASSWORD)


async def onemap_token(client):
    if _OM["token"] and time.time() < _OM["exp"] - 600:
        return _OM["token"]
    r = await client.post(f"{ONEMAP_BASE}/api/auth/post/getToken", json={"email": ONEMAP_EMAIL, "password": ONEMAP_PASSWORD}, timeout=20)
    r.raise_for_status()
    j = r.json()
    _OM["token"] = j.get("access_token")
    try:
        _OM["exp"] = float(j.get("expiry_timestamp") or 0) or time.time() + 3 * 24 * 3600
    except (TypeError, ValueError):
        _OM["exp"] = time.time() + 3 * 24 * 3600
    return _OM["token"]


def _len_km(line):
    return sum(math.hypot((b[1] - a[1]) * 111.32 * math.cos(math.radians(a[0])), (b[0] - a[0]) * 110.574) for a, b in zip(line, line[1:]))


async def onemap_leg(client, a, b, token, lock):
    async with lock:
        try:
            r = await client.get(f"{ONEMAP_BASE}/api/public/routingsvc/route",
                                 params={"start": f"{a[0]},{a[1]}", "end": f"{b[0]},{b[1]}", "routeType": "drive"},
                                 headers={"Authorization": token}, timeout=20)
            r.raise_for_status()
            geo = (r.json() or {}).get("route_geometry")
            if not geo:
                return None
            ln = decode_polyline(geo)
            if len(ln) < 2 or _len_km(ln) > 2.5 * _len_km([a, b]) + 0.4:   # reject U-turn detours
                return None
            return ln
        except Exception:
            return None


async def onemap_line(client, stops):
    """One OneMap drive route per consecutive stop pair. -> (line, info) or (None, reason)."""
    if not onemap_enabled():
        return None, "OneMap not configured (set ONEMAP_EMAIL and ONEMAP_PASSWORD)"
    try:
        token = await onemap_token(client)
    except Exception as e:
        return None, f"OneMap login failed ({type(e).__name__})"
    if not token:
        return None, "OneMap login returned no token"
    pts = [(s["lat"], s["lon"]) for s in stops]
    lock = asyncio.Semaphore(4)                  # be polite to OneMap's rate limit
    legs = await asyncio.gather(*[onemap_leg(client, a, b, token, lock) for a, b in zip(pts, pts[1:])])
    good = sum(1 for g in legs if g)
    if good < 0.8 * len(legs):
        return None, f"OneMap routed only {good} of {len(legs)} legs"
    line = [pts[0]]
    for (a, b), g in zip(zip(pts, pts[1:]), legs):
        line += (g[1:] if g else [b])
    return line, f"OneMap drive routing, {good} of {len(legs)} legs"
