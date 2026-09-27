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


# --------------------------------------------------------------------------- V16.1 fast whole-network fit (Traffic-Aware Regulation)
def dp_simplify(line, tol_m=6.0):
    """Douglas-Peucker in metres: keeps the road shape within tol_m while dropping redundant vertices. Returns the kept indices."""
    n = len(line)
    if n < 3:
        return list(range(n))
    lat0 = line[0][0]
    xy = [_xy(p[0], p[1], lat0) for p in line]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L if L > 1e-9 else math.hypot(px - ax, py - ay)
            if d > best:
                best, bi = d, i
        if bi > 0 and best > tol_m:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [i for i in range(n) if keep[i]]


def fast_fit(cands, stops, on_m=ON_LINE_M, min_match=MIN_MATCH, tol_m=6.0):
    """Fit the road polyline(s) of a service to one direction's LTA stop list, vectorised (a few ms per route).
    -> {"line": [(lat, lon)], "km": [km along the route on LTA's own distance scale per vertex], "score": share of stops on the line} or None.
    Stops are walked forward only (loop services work); the km of every vertex is interpolated between the stops it lies between,
    so a position on the line converts exactly to the same route km the rest of the app uses."""
    import numpy as np
    if len(stops) < 2 or not cands:
        return None
    lat0 = stops[0]["lat"]
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110574.0
    S = np.array([[s["lon"] * kx, s["lat"] * ky] for s in stops])
    best = None
    for ln in cands:
        if not ln or len(ln) < 2:
            continue
        for cand in (ln, ln[::-1]):
            V = np.array([[p[1] * kx, p[0] * ky] for p in cand])
            A, B = V[:-1], V[1:]
            D = B - A
            L2 = (D * D).sum(1)
            L2[L2 < 1e-9] = 1e-9
            P_ = S[:, None, :] - A[None, :, :]
            t = np.clip((P_ * D[None, :, :]).sum(2) / L2[None, :], 0.0, 1.0)
            Q = A[None, :, :] + t[:, :, None] * D[None, :, :]
            dist = np.hypot(S[:, None, 0] - Q[:, :, 0], S[:, None, 1] - Q[:, :, 1])
            near = dist <= on_m
            prev, hits, seg, tt = 0, 0, [], []
            nseg = len(A)
            for i in range(len(stops)):
                idx = np.nonzero(near[i, prev:])[0]
                if not len(idx):
                    seg.append(None); tt.append(None)
                    continue
                j = prev + int(idx[0])
                k = j
                while k + 1 < nseg and near[i, k + 1] and dist[i, k + 1] <= dist[i, k]:
                    k += 1
                hits += 1
                prev = k
                seg.append(k); tt.append(float(t[i, k]))
            score = hits / len(stops)
            if best is None or score > best[0]:
                best = (score, cand, seg, tt)
    if best is None or best[0] < min_match:
        return None
    score, cand, seg, tt = best
    m = [i for i, s_ in enumerate(seg) if s_ is not None]
    i0, i1 = m[0], m[-1]
    s0, s1 = seg[i0], seg[i1]
    lerp = lambda a, b, f: (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
    body = [lerp(cand[s0], cand[s0 + 1], tt[i0])] + list(cand[s0 + 1:s1 + 1]) + [lerp(cand[s1], cand[s1 + 1], tt[i1])]
    # metres along body for every matched stop
    cum = [0.0]
    for a, b in zip(body, body[1:]):
        ax, ay = _xy(a[0], a[1], lat0)
        bx, by = _xy(b[0], b[1], lat0)
        cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
    def pos(i):
        s_ = seg[i]
        base = cum[s_ - s0] if s_ > s0 else 0.0
        a, b = (body[0], cand[s0 + 1]) if s_ == s0 else (cand[s_], cand[s_ + 1])
        f = tt[i] if s_ != s0 else max(0.0, (tt[i] - tt[i0]) / max(1e-9, 1.0 - tt[i0]))
        ax, ay = _xy(a[0], a[1], lat0)
        bx, by = _xy(b[0], b[1], lat0)
        return base + f * math.hypot(bx - ax, by - ay)
    anchors, last_m, last_km = [], -1.0, -1.0
    for i in m:
        km_ = stops[i].get("dist")
        pm = pos(i)
        if km_ is None or pm <= last_m or km_ <= last_km:
            continue
        anchors.append((pm, float(km_)))
        last_m, last_km = pm, float(km_)
    if len(anchors) < 2:
        return None
    am = np.array([a[0] for a in anchors]); ak = np.array([a[1] for a in anchors])
    cm = np.array(cum)
    km = np.interp(cm, am, ak)
    km[cm < am[0]] = ak[0] - (am[0] - cm[cm < am[0]]) / 1000.0
    km[cm > am[-1]] = ak[-1] + (cm[cm > am[-1]] - am[-1]) / 1000.0
    line, kms = list(body), [float(x) for x in km]
    head = [((s["lat"], s["lon"]), s.get("dist")) for s in stops[:i0]]
    tail = [((s["lat"], s["lon"]), s.get("dist")) for s in stops[i1 + 1:]]
    if head:
        line = [h[0] for h in head] + line
        kms = [float(h[1]) if h[1] is not None else kms[0] for h in head] + kms
    if tail:
        line = line + [h[0] for h in tail]
        kms = kms + [float(h[1]) if h[1] is not None else kms[-1] for h in tail]
    keep = dp_simplify(line, tol_m)
    return {"line": [line[i] for i in keep], "km": [kms[i] for i in keep], "score": round(score, 3)}
