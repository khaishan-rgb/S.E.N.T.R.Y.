"""SG Transport Pulse - Traffic-Aware Regulation engine (pure functions, no network, no clock of its own).

Four layers, kept apart on purpose (specification section 29):
  1. Detection      build_stretches() -> update_book(): congestion / incident / roadworks / weather events with a stable id, persistence, clear and re-alert rules
  2. Service impact RouteIndex: which service AND direction, which section, how much of the route, how much delay
  3. Headway impact classify_buses() / headway_impact(): where the buses are relative to the disruption and what the headways become
  4. Regulation     regulation_options() / restore_check(): rule / simulation based recommendations, never an unexplained confidence

Everything is deterministic and decision support only.
"""
import math

MODEL_VERSION = "traffic-1.1"
EPS = 1e-9

PARAMS = {
    # ---- speed classes (km/h) - specification section 7
    "very_slow_kmh": 20.0,      # < this: Very Slow
    "congest_kmh": 30.0,        # < this: Slow (and below) = a congested segment; 30-40 = Moderate
    "normal_kmh": 40.0,         # >= this: Normal
    "min_len_m": 500.0,         # a congestion stretch must be at least this long
    "persist_updates": 3,       # detections in a row before an alert is raised
    "clear_updates": 3,         # normal updates in a row before an alert is cleared
    "persist_new_data_only": 0, # 1 = only count an update when LTA published a new speed-band snapshot
    "refresh_s": 60,            # how often the page / background loop refreshes (LTA speed bands change about every 5 min)
    "join_gap_m": 120.0,        # two slow links on the SAME road belong to one stretch if one ends within this distance of the next start ...
    "join_gap_other_m": 30.0,   # ... links on DIFFERENT roads only join where they actually meet (a junction), so unrelated roads are not merged
    "join_angle": 60.0,         # ... and they run in about the same direction (deg)
    # ---- reference (normal) speed by LTA road category, km/h
    "ref_A": 70.0, "ref_B": 50.0, "ref_C": 45.0, "ref_D": 40.0, "ref_E": 35.0, "ref_F": 30.0, "ref_default": 50.0,
    # ---- matching a disruption to a bus service
    "match_m": 70.0,            # a route point within this distance of the disrupted road counts as on it
    "match_angle": 45.0,        # ... and the bus travels the same way as the traffic (deg) - direction matters
    "min_overlap_m": 200.0,     # ignore a route that only clips the disruption
    "sample_m": 40.0,           # spacing of the points used to measure the overlap
    "incident_m": 300.0,        # an incident this close to a route affects the service (direction not given by the source)
    "roadwork_m": 300.0,
    "weather_km": 3.0,          # a rain area affects the route sections within this distance
    "rain_group_km": 4.0,       # rain gauges closer than this form one rain area
    "rain_mod_mm": 0.5,         # mm per reading: moderate rain
    "rain_heavy_mm": 1.5,       # mm per reading: heavy rain
    # ---- lifecycle
    "worse_speed_drop_pct": 25.0,   # after acknowledgement: speed at least this % lower ...
    "worse_len_add_m": 1000.0,      # ... or the stretch this much longer = CONDITION WORSENED (re-alert)
    "improve_pct": 25.0,            # speed at least this % higher (or 30% shorter) = improving
    "ack_updates": 2,               # updates an alert stays 'acknowledged' before it is 'monitoring'
    "keep_cleared_min": 60,         # how long a cleared alert stays in the list
    # ---- buses, headway, regulation
    "bus_run_kmh": 22.0,            # assumed bus running speed for the time to impact
    "restore_last_pct": 15.0,       # the last part of the trip used to detect recovery
    "recover_tol_pct": 20.0,        # headway within +-this % of scheduled = recovered
    "reg_hold_max": 6.0,            # the most a departure may be stretched by regulation (min)
    "reg_deps": 3,                  # how many next departures the regulation may stretch
    "min_layover_min": 2.0,
    "hw_worse_min": 3.0,            # predicted headway this much above scheduled = deterioration expected
    # ---- priority score (section 30): weights (%) and levels
    "w_speed": 25.0, "w_length": 20.0, "w_buses": 20.0, "w_hw": 25.0, "w_duration": 10.0,
    "crit_score": 60.0, "high_score": 40.0, "monitor_score": 20.0,
    "sev_incident": 0.7, "sev_roadwork": 0.5, "sev_rain_mod": 0.4, "sev_rain_heavy": 0.7,
}
RANGES = {
    "very_slow_kmh": (5, 40), "congest_kmh": (10, 50), "normal_kmh": (20, 80), "min_len_m": (100, 5000), "persist_updates": (1, 20), "clear_updates": (1, 20),
    "persist_new_data_only": (0, 1), "refresh_s": (15, 600), "join_gap_m": (20, 300), "join_gap_other_m": (5, 200), "join_angle": (10, 120),
    "ref_A": (20, 120), "ref_B": (20, 120), "ref_C": (20, 120), "ref_D": (20, 120), "ref_E": (10, 120), "ref_F": (10, 120), "ref_default": (20, 120),
    "match_m": (20, 200), "match_angle": (10, 120), "min_overlap_m": (0, 2000), "sample_m": (10, 200), "incident_m": (50, 2000), "roadwork_m": (50, 2000),
    "weather_km": (0.5, 20), "rain_group_km": (0.5, 20), "rain_mod_mm": (0.05, 20), "rain_heavy_mm": (0.1, 40),
    "worse_speed_drop_pct": (5, 90), "worse_len_add_m": (100, 10000), "improve_pct": (5, 200), "ack_updates": (0, 20), "keep_cleared_min": (0, 1440),
    "bus_run_kmh": (5, 60), "restore_last_pct": (5, 50), "recover_tol_pct": (5, 60), "reg_hold_max": (0, 20), "reg_deps": (1, 8), "min_layover_min": (0, 30), "hw_worse_min": (0.5, 30),
    "w_speed": (0, 100), "w_length": (0, 100), "w_buses": (0, 100), "w_hw": (0, 100), "w_duration": (0, 100),
    "crit_score": (1, 100), "high_score": (1, 100), "monitor_score": (1, 100),
    "sev_incident": (0, 1), "sev_roadwork": (0, 1), "sev_rain_mod": (0, 1), "sev_rain_heavy": (0, 1),
}
INT_PARAMS = ("persist_updates", "clear_updates", "persist_new_data_only", "refresh_s", "ack_updates", "keep_cleared_min", "reg_deps")


def _r(x, n=1):
    return None if x is None else round(x, n)


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _f(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- geometry
def hav_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    q = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(q))


def brg(lat1, lon1, lat2, lon2):
    kx = math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2((lon2 - lon1) * kx, lat2 - lat1)) % 360


def angdiff(a, b):
    """Smallest difference between two bearings, 0..180."""
    return abs((a - b + 180) % 360 - 180)


def _xy(lat0, lat, lon, lon0):
    return (lon - lon0) * math.cos(math.radians(lat0)) * 111320.0, (lat - lat0) * 110574.0


def seg_kmh(band, mn, mx):
    """A representative km/h for an LTA speed band (its middle)."""
    b = int(band)
    if b >= 8:
        return 75.0
    mn, mx = _f(mn), _f(mx)
    if mn is None:
        mn = max(0, (b - 1) * 10)
    if mx is None or mx < mn or mx > 130:
        mx = mn + 9
    return max(3.0, (mn + mx) / 2.0)


def speed_class(kmh, P):
    return "very_slow" if kmh < P["very_slow_kmh"] else "slow" if kmh < P["congest_kmh"] else "moderate" if kmh < P["normal_kmh"] else "normal"


def seg_key(s):
    return f"{round(s[0], 5)},{round(s[1], 5)}>{round(s[2], 5)},{round(s[3], 5)}"


def ref_speed(cat, P):
    return P.get(f"ref_{str(cat).strip().upper()}", P["ref_default"]) if cat not in (None, "") else P["ref_default"]


def _nearest_name(lat, lon, landmarks, maxm=600.0):
    best, bd = None, maxm
    for la, lo, name in landmarks or ():
        if abs(la - lat) > 0.006 or abs(lo - lon) > 0.006:
            continue
        d = hav_m(lat, lon, la, lo)
        if d < bd:
            best, bd = name, d
    return best


# ----------------------------------------------------------------------------- layer 1: whole-stretch detection
def build_stretches(segs, P, landmarks=None):
    """Combine adjacent slow LTA road links into continuous congestion stretches (one event per jam, not one per link).
    segs: (alat, alon, blat, blon, band, road, min, max, category). Returns stretches sorted by length, longest first."""
    slow = []
    for s in segs:
        v = seg_kmh(s[4], s[6], s[7])
        if v < P["congest_kmh"]:
            L = hav_m(s[0], s[1], s[2], s[3])
            if L > 5.0:
                slow.append((s, v, L, brg(s[0], s[1], s[2], s[3])))
    n = len(slow)
    cell = 0.0015                                                # ~165 m: larger than join_gap_m
    grid = {}
    for i, (s, _, _, _) in enumerate(slow):
        grid.setdefault((int(s[0] / cell), int(s[1] / cell)), []).append(i)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    gap, gap2, ang = P["join_gap_m"], P["join_gap_other_m"], P["join_angle"]
    for i, (s, _, _, bi) in enumerate(slow):
        cx, cy = int(s[2] / cell), int(s[3] / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    if j == i:
                        continue
                    t = slow[j][0]
                    if hav_m(s[2], s[3], t[0], t[1]) <= (gap if (s[5] or "") == (t[5] or "") else gap2) and angdiff(bi, slow[j][3]) <= ang:
                        a, b = find(i), find(j)
                        if a != b:
                            parent[a] = b
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    out = []
    for idxs in comps.values():
        L = sum(slow[i][2] for i in idxs)
        if L < P["min_len_m"]:
            continue
        avg = sum(slow[i][1] * slow[i][2] for i in idxs) / L
        roads, cats = {}, {}
        for i in idxs:
            s = slow[i][0]
            roads[s[5] or ""] = roads.get(s[5] or "", 0.0) + slow[i][2]
            cats[s[8]] = cats.get(s[8], 0.0) + slow[i][2]
        road = max(roads, key=roads.get) or "Unnamed road"
        cat = max(cats, key=cats.get)
        mb = math.atan2(sum(math.sin(math.radians(slow[i][3])) * slow[i][2] for i in idxs), sum(math.cos(math.radians(slow[i][3])) * slow[i][2] for i in idxs))
        ux, uy = math.sin(mb), math.cos(mb)                       # mean direction of travel (east, north)
        lat0 = slow[idxs[0]][0][0]
        lon0 = slow[idxs[0]][0][1]

        def proj(lat, lon):
            x, y = _xy(lat0, lat, lon, lon0)
            return x * ux + y * uy
        first = min(idxs, key=lambda i: proj(slow[i][0][0], slow[i][0][1]))
        lastn = max(idxs, key=lambda i: proj(slow[i][0][2], slow[i][0][3]))
        sa, sb = slow[first][0], slow[lastn][0]
        very = sum(slow[i][2] for i in idxs if slow[i][1] < P["very_slow_kmh"]) / L
        lats = [c for i in idxs for c in (slow[i][0][0], slow[i][0][2])]
        lons = [c for i in idxs for c in (slow[i][0][1], slow[i][0][3])]
        out.append({
            "keys": sorted(seg_key(slow[i][0]) for i in idxs), "road": road, "cat": cat, "length_m": L, "avg_kmh": avg, "min_kmh": min(slow[i][1] for i in idxs),
            "ref_kmh": ref_speed(cat, P), "very_slow_pct": 100.0 * very, "n_links": len(idxs),
            "from": _nearest_name(sa[0], sa[1], landmarks) or road, "to": _nearest_name(sb[2], sb[3], landmarks) or road,
            "start": [sa[0], sa[1]], "end": [sb[2], sb[3]], "bbox": [min(lats), min(lons), max(lats), max(lons)], "center": [sum(lats) / len(lats), sum(lons) / len(lons)],
            "segments": [[round(slow[i][0][0], 5), round(slow[i][0][1], 5), round(slow[i][0][2], 5), round(slow[i][0][3], 5), round(slow[i][1], 1)] for i in idxs]})
    out.sort(key=lambda x: -x["length_m"])
    return out


# ----------------------------------------------------------------------------- layer 2: route index (which service, which direction, which section)
class RouteIndex:
    """Stop-to-stop segments of every service and direction in a grid, so a disruption finds the routes it overlaps in one pass.
    routes: {(svc, dir): [{"seq", "code", "dist"}]}, stops: {code: {"lat", "lon", "name", "road"}}."""

    def __init__(self, routes, stops):
        self.cell = 0.003
        self.grid = {}
        self.segs = []                                   # (svc, dir, lat1, lon1, lat2, lon2, km_at_start, km_at_end, bearing)
        self.meta = {}                                   # (svc, dir) -> {"km": total route km, "n": stops, "first": name, "last": name, "stops": [(lat, lon, km)]}
        self.by_road = {}
        for (svc, d), rows in routes.items():
            pts = []
            cum = 0.0
            for r in rows:
                s = stops.get(r["code"])
                if not s:
                    continue
                if pts:
                    cum += hav_m(pts[-1][0], pts[-1][1], s["lat"], s["lon"]) / 1000.0
                km = r["dist"] if r.get("dist") is not None else cum
                pts.append((s["lat"], s["lon"], km, s))
            if len(pts) < 2:
                continue
            total = max(p[2] for p in pts) or cum
            self.meta[(svc, d)] = {"km": total, "n": len(pts), "first": pts[0][3]["name"], "last": pts[-1][3]["name"], "stops": [(p[0], p[1], p[2]) for p in pts]}
            for a, b in zip(pts, pts[1:]):
                if hav_m(a[0], a[1], b[0], b[1]) < 5.0:
                    continue
                i = len(self.segs)
                self.segs.append((svc, d, a[0], a[1], b[0], b[1], a[2], max(b[2], a[2]), brg(a[0], a[1], b[0], b[1])))
                n = int(hav_m(a[0], a[1], b[0], b[1]) // 150) + 1
                for c in {(int((a[0] + (b[0] - a[0]) * k / n) / self.cell), int((a[1] + (b[1] - a[1]) * k / n) / self.cell)) for k in range(n + 1)}:
                    self.grid.setdefault(c, []).append(i)
            for p in pts:
                road = (p[3].get("road") or "").strip().upper()
                if road:
                    self.by_road.setdefault(road, {}).setdefault((svc, d), []).append((p[0], p[1], p[2]))

    def _near(self, lat, lon):
        cx, cy = int(lat / self.cell), int(lon / self.cell)
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((cx + dx, cy + dy), ()):
                    if i not in seen:
                        seen.add(i)
                        yield i

    @staticmethod
    def _proj(seg, lat, lon):
        """(distance m, fraction 0..1 along the segment) of a point against a stop-to-stop segment."""
        lat0, lon0 = seg[2], seg[3]
        ax, ay = _xy(lat0, lat, lon, lon0)
        vx, vy = _xy(lat0, seg[4], seg[5], lon0)
        l2 = vx * vx + vy * vy
        t = _clip((ax * vx + ay * vy) / l2, 0.0, 1.0) if l2 > 1e-6 else 0.0
        return math.hypot(ax - t * vx, ay - t * vy), t

    def match_stretch(self, segments, P):
        """segments: [[alat, alon, blat, blon, kmh]]. -> {(svc, dir): {overlap_km, a_km, b_km, route_km, pct}} - only routes that run ALONG the stretch in the SAME direction."""
        step = P["sample_m"]
        hit = {}
        for s in segments:
            L = hav_m(s[0], s[1], s[2], s[3])
            n = max(1, int(round(L / step)))
            bt = brg(s[0], s[1], s[2], s[3])
            for k in range(n):
                f = (k + 0.5) / n
                lat, lon = s[0] + (s[2] - s[0]) * f, s[1] + (s[3] - s[1]) * f
                best = {}
                for i in self._near(lat, lon):
                    g = self.segs[i]
                    if angdiff(bt, g[8]) > P["match_angle"]:
                        continue
                    d, t = self._proj(g, lat, lon)
                    if d <= P["match_m"]:
                        key = (g[0], g[1])
                        if key not in best or d < best[key][0]:
                            best[key] = (d, g[6] + (g[7] - g[6]) * t)
                for key, (d, km) in best.items():
                    h = hit.setdefault(key, {"n": 0, "lo": km, "hi": km, "len": 0.0})
                    h["n"] += 1
                    h["len"] += L / n
                    h["lo"], h["hi"] = min(h["lo"], km), max(h["hi"], km)
        out = {}
        for key, h in hit.items():
            if h["len"] < P["min_overlap_m"]:
                continue
            km = self.meta[key]["km"]
            out[key] = {"overlap_km": h["len"] / 1000.0, "a_km": h["lo"], "b_km": h["hi"], "route_km": km, "pct": 100.0 * (h["len"] / 1000.0) / km if km > 0 else 0.0}
        return out

    def match_point(self, lat, lon, radius_m):
        """Routes that pass within radius_m of a point (an incident, roadworks): direction is not known, so both directions that pass by are returned."""
        out = {}
        for i in self._near(lat, lon):
            g = self.segs[i]
            d, t = self._proj(g, lat, lon)
            if d <= radius_m:
                key = (g[0], g[1])
                if key not in out or d < out[key]["dist_m"]:
                    km = g[6] + (g[7] - g[6]) * t
                    out[key] = {"dist_m": d, "a_km": km, "b_km": km, "overlap_km": 0.0, "route_km": self.meta[key]["km"], "pct": 0.0}
        return out

    def match_circle(self, lat, lon, radius_km):
        """Route sections within radius_km of a rain area: the stretch of route inside the circle."""
        out = {}
        r = radius_km * 1000.0
        n = int(radius_km * 1000 // 150) + 2
        cells = {(int((lat + dy) / self.cell), int((lon + dx) / self.cell)) for dy in [k * 0.0014 for k in range(-n, n + 1)] for dx in [k * 0.0014 for k in range(-n, n + 1)]}
        seen = set()
        for c in cells:
            for i in self.grid.get(c, ()):
                if i in seen:
                    continue
                seen.add(i)
                g = self.segs[i]
                d, t = self._proj(g, lat, lon)
                if d <= r:
                    key = (g[0], g[1])
                    km = g[6] + (g[7] - g[6]) * t
                    o = out.setdefault(key, {"lo": km, "hi": km, "len": 0.0})
                    o["lo"], o["hi"] = min(o["lo"], g[6]), max(o["hi"], g[7])
                    o["len"] += max(0.0, g[7] - g[6])
        return {k: {"overlap_km": v["len"], "a_km": v["lo"], "b_km": v["hi"], "route_km": self.meta[k]["km"], "pct": 100.0 * v["len"] / self.meta[k]["km"] if self.meta[k]["km"] > 0 else 0.0, "dist_m": 0.0} for k, v in out.items()}

    def match_road(self, road):
        r = (road or "").strip().upper()
        out = {}
        for key, pts in self.by_road.get(r, {}).items():
            km = self.meta[key]["km"]
            out[key] = {"a_km": min(p[2] for p in pts), "b_km": max(p[2] for p in pts), "overlap_km": 0.0, "route_km": km, "pct": 0.0, "dist_m": 0.0, "lat": sum(p[0] for p in pts) / len(pts), "lon": sum(p[1] for p in pts) / len(pts)}
        return out

    def position(self, svc, d, lat, lon):
        """Distance in km along the route of the point nearest to (lat, lon): used to place buses. (km, distance m) or None."""
        m = self.meta.get((svc, d))
        if not m:
            return None
        best = None
        pts = m["stops"]
        for a, b in zip(pts, pts[1:]):
            seg = (0, 0, a[0], a[1], b[0], b[1])
            dd, t = self._proj(seg, lat, lon)
            if best is None or dd < best[1]:
                best = (a[2] + (b[2] - a[2]) * t, dd)
        return best


# ----------------------------------------------------------------------------- settings validation
def validate_params(d):
    """(clean {name: value}, [errors]) for a dict of edited settings."""
    out, errs = {}, []
    for k, v in (d or {}).items():
        if k not in PARAMS:
            errs.append(f"Unknown setting {k}")
            continue
        f = _f(v)
        lo, hi = RANGES[k]
        if f is None or not (lo <= f <= hi):
            errs.append(f"{k} must be a number between {lo:g} and {hi:g}")
            continue
        out[k] = int(round(f)) if k in INT_PARAMS else f
    return out, errs


# ----------------------------------------------------------------------------- layer 1b: the event book (stable ids, persistence, clear, re-alert)
KINDS = ("congestion", "incident", "roadworks", "weather")
PREFIX = {"congestion": "CONGESTION", "incident": "INCIDENT", "roadworks": "ROADWORKS", "weather": "WEATHER"}
LEVEL_RANK = {"moderate": 1, "heavy": 2}


def new_book():
    return {"events": {}, "acks": {}, "seq": {}, "snap": None, "updates": 0, "log": []}


def _slug(s):
    t = "".join(c for c in str(s).upper() if c.isalnum())[:12]
    return t or "AREA"


def _new_id(book, kind, name):
    k = f"{PREFIX[kind]}-{_slug(name)}"
    book["seq"][k] = book["seq"].get(k, 0) + 1
    return f"{k}-{book['seq'][k]:03d}"


def _mk_event(book, kind, name, now, cur, keys=None, persist=1):
    """A new event. Until it has persisted it is a candidate with a temporary key; the stable id (CONGESTION-ORCHARD-001) is given only when the alert is raised, so
    jams that come and go never use up id numbers."""
    book["seq"]["_cand"] = book["seq"].get("_cand", 0) + 1
    ev = {"id": f"CAND-{book['seq']['_cand']}", "kind": kind, "name": name, "first_seen": now, "last_seen": now, "alert_time": None, "seen": 1, "miss": 0, "status": "candidate",
          "cleared_time": None, "cur": cur, "peak": {}, "keys": keys or [], "match": {}}
    book["events"][ev["id"]] = ev
    if ev["seen"] >= persist:
        _promote(book, ev, now)
    _peak(ev)
    return ev


def _promote(book, ev, now):
    old = ev["id"]
    ev["status"], ev["alert_time"] = "active", now
    if old.startswith("CAND-"):
        new = _new_id(book, ev["kind"], ev["cur"].get("road") or ev.get("name") or ev["kind"])
        book["events"].pop(old, None)
        ev["id"] = new
        book["events"][new] = ev


def _peak(ev):
    c = ev["cur"]
    p = ev["peak"]
    if ev["kind"] == "congestion":
        p["min_kmh"] = min(p.get("min_kmh", 1e9), c["min_kmh"])
        p["max_len_m"] = max(p.get("max_len_m", 0.0), c["length_m"])
    elif ev["kind"] == "weather":
        p["max_mm"] = max(p.get("max_mm", 0.0), c.get("max_mm", 0.0))


def _touch(book, ev, cur, now, persist, count):
    ev["cur"] = cur
    ev["last_seen"], ev["miss"] = now, 0
    if count:
        ev["seen"] += 1
    if ev["status"] == "candidate" and ev["seen"] >= persist:
        _promote(book, ev, now)
    _peak(ev)


def _missed(ev, now, P, count, book):
    if not count:
        return
    ev["miss"] += 1
    if ev["status"] == "candidate":
        ev["status"] = "dropped"
    elif ev["status"] == "active" and ev["miss"] >= P["clear_updates"]:
        ev["status"], ev["cleared_time"] = "cleared", now


def update_congestion(book, stretches, now, P, count=True):
    evs = [e for e in book["events"].values() if e["kind"] == "congestion" and e["status"] in ("candidate", "active")]
    by_key = {}
    for e in evs:
        for k in e["keys"]:
            by_key[k] = e["id"]
    used = set()
    for st in sorted(stretches, key=lambda s: -s["length_m"]):
        votes = {}
        for k in st["keys"]:
            i = by_key.get(k)
            if i and i not in used:
                votes[i] = votes.get(i, 0) + 1
        best = None
        for i, v in sorted(votes.items(), key=lambda kv: (-kv[1], book["events"][kv[0]]["first_seen"])):
            e = book["events"][i]
            if v >= max(1, 0.3 * min(len(st["keys"]), len(e["keys"]))):
                best = e
                break
        cur = {k: st[k] for k in ("road", "from", "to", "length_m", "avg_kmh", "min_kmh", "ref_kmh", "very_slow_pct", "start", "end", "center", "bbox", "segments", "cat", "n_links")}
        if best is None:
            e = _mk_event(book, "congestion", st["road"], now, cur, st["keys"], P["persist_updates"])
            if not count and e["status"] == "candidate":
                e["seen"] = 1
        else:
            best["keys"] = st["keys"]
            _touch(book, best, cur, now, P["persist_updates"], count)
            e = best
        used.add(e["id"])
    for e in evs:
        if e["id"] not in used:
            _missed(e, now, P, count, book)


def update_points(book, kind, items, now, P, count=True):
    """Incidents and roadworks: reported by the source, so they are active at once and clear after clear_updates absences. items: [{"key", ...}]"""
    evs = {e["keys"][0]: e for e in book["events"].values() if e["kind"] == kind and e["status"] in ("candidate", "active") and e["keys"]}
    seen = set()
    for it in items:
        k = it["key"]
        seen.add(k)
        if k in evs:
            _touch(book, evs[k], it, now, 1, count)
        else:
            e = _mk_event(book, kind, it.get("road") or it.get("type") or kind, now, it, [k], 1)
    for k, e in evs.items():
        if k not in seen:
            _missed(e, now, P, count, book)


def update_weather(book, cells, now, P, count=True):
    evs = [e for e in book["events"].values() if e["kind"] == "weather" and e["status"] in ("candidate", "active")]
    used = set()
    for c in sorted(cells, key=lambda c: -c["max_mm"]):
        best, bd = None, 5000.0
        for e in evs:
            if e["id"] in used:
                continue
            d = hav_m(c["lat"], c["lon"], e["cur"]["lat"], e["cur"]["lon"])
            if d < bd:
                best, bd = e, d
        if best is None:
            e = _mk_event(book, "weather", c["name"], now, c, [], 1)
        else:
            _touch(book, best, c, now, 1, count)
            e = best
        used.add(e["id"])
    for e in evs:
        if e["id"] not in used:
            _missed(e, now, P, count, book)


def rain_cells(stations, P):
    """Group rain gauges into rain areas; only moderate or heavier rain becomes an event (light rain is not an operational alert). stations: [{"name","lat","lon","mm"}]"""
    st = [s for s in stations if s["mm"] >= P["rain_mod_mm"]]
    parent = list(range(len(st)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(len(st)):
        for j in range(i + 1, len(st)):
            if hav_m(st[i]["lat"], st[i]["lon"], st[j]["lat"], st[j]["lon"]) <= P["rain_group_km"] * 1000:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(st)):
        groups.setdefault(find(i), []).append(st[i])
    out = []
    for g in groups.values():
        mx = max(g, key=lambda s: s["mm"])
        lat, lon = sum(s["lat"] for s in g) / len(g), sum(s["lon"] for s in g) / len(g)
        rad = max([hav_m(lat, lon, s["lat"], s["lon"]) for s in g] + [0.0]) / 1000.0
        out.append({"name": mx["name"], "lat": lat, "lon": lon, "radius_km": max(1.5, rad + 1.0), "max_mm": mx["mm"], "n": len(g),
                    "level": "heavy" if mx["mm"] >= P["rain_heavy_mm"] else "moderate", "stations": [s["name"] for s in g][:8]})
    return out


def refresh_matches(book, ridx, P):
    """Layer 2: for every live event, which service + direction it affects and how much of the route. An event that touches no bus route is not a bus alert and is dropped.
    Only ACTIVE events are matched (never mere candidates): route matching is the expensive part of a refresh, and most congestion candidates are noise that never persists into an
    alert, so matching them would be pure waste, repeated every cycle. A candidate is matched for the first time on the very refresh it gets promoted to active (update_congestion /
    update_points / update_weather run before this, so a newly promoted event already has status 'active' by the time this runs)."""
    for e in book["events"].values():
        if e["status"] != "active":
            continue
        c = e["cur"]
        if e["kind"] == "congestion":
            m = ridx.match_stretch(c["segments"], P)
        elif e["kind"] == "incident":
            m = ridx.match_point(c["lat"], c["lon"], P["incident_m"])
        elif e["kind"] == "roadworks":
            m = ridx.match_point(c["lat"], c["lon"], P["roadwork_m"]) if c.get("lat") is not None else ridx.match_road(c.get("road"))
            if c.get("lat") is None and m:
                v = next(iter(m.values()))
                c["lat"], c["lon"] = v["lat"], v["lon"]
        else:
            m = ridx.match_circle(c["lat"], c["lon"], max(P["weather_km"], c.get("radius_km", 0)))
        e["match"] = {f"{k[0]}|{k[1]}": v for k, v in m.items()}
        if not e["match"]:
            e["status"] = "dropped"


def counts(book, P, snap=None):
    """Does this refresh count as an update? Always, unless persist_new_data_only is on and LTA has not published a new speed-band snapshot since the last refresh."""
    return not (P["persist_new_data_only"] and snap is not None and snap == book.get("snap"))


def tick(book, now, P, snap=None):
    """One refresh cycle bookkeeping (call once per refresh, after the update_* calls): acknowledged alerts age, cleared alerts expire. Returns whether it counted as an update."""
    count = counts(book, P, snap)
    if snap is not None:
        book["snap"] = snap
    book["updates"] += 1 if count else 0
    for a in book["acks"].values():
        if count:
            a["updates"] = a.get("updates", 0) + 1
    keep = P["keep_cleared_min"] * 60
    for i in [i for i, e in book["events"].items() if e["status"] == "dropped" or (e["status"] == "cleared" and now - e["cleared_time"] > keep)]:
        del book["events"][i]
    for k in [k for k in book["acks"] if k.split(":")[0] not in book["events"]]:
        del book["acks"][k]
    return count


# ----------------------------------------------------------------------------- alerts: one per event x service x direction
def estimate_delay_min(overlap_km, cur_kmh, ref_kmh):
    """Section 23: extra travel time through the overlap = distance / current speed - distance / reference speed (an estimated traffic delay, not actual lateness)."""
    if overlap_km <= 0 or not cur_kmh or not ref_kmh:
        return 0.0
    return max(0.0, overlap_km / max(cur_kmh, 3.0) * 60.0 - overlap_km / ref_kmh * 60.0)


def event_level(ev):
    """The condition level used for 'worsened' on weather (moderate / heavy)."""
    return LEVEL_RANK.get(ev["cur"].get("level"), 0)


def condition_snapshot(ev):
    c = ev["cur"]
    return {"avg_kmh": c.get("avg_kmh"), "length_m": c.get("length_m"), "level": event_level(ev)}


def alert_state(ev, ack, P):
    """(status, worsened): new | acknowledged | monitoring | improving | cleared. After an acknowledgement a clearly worse condition raises it again (RE-ALERT)."""
    if ev["status"] == "cleared":
        return "cleared", False
    if ack is None:
        return "new", False
    s, c = ack.get("snap") or {}, ev["cur"]
    worse = improve = False
    if ev["kind"] == "congestion" and s.get("avg_kmh"):
        worse = c["avg_kmh"] <= s["avg_kmh"] * (1 - P["worse_speed_drop_pct"] / 100.0) or c["length_m"] >= (s.get("length_m") or 0) + P["worse_len_add_m"]
        improve = c["avg_kmh"] >= s["avg_kmh"] * (1 + P["improve_pct"] / 100.0) or c["length_m"] <= (s.get("length_m") or 1e9) * 0.7
    elif ev["kind"] == "weather":
        worse = event_level(ev) > (s.get("level") or 0)
        improve = event_level(ev) < (s.get("level") or 0)
    if worse:
        return "new", True
    if improve:
        return "improving", False
    return ("acknowledged" if ack.get("updates", 0) < P["ack_updates"] else "monitoring"), False


def _duration_min(ev, now):
    end = ev["cleared_time"] if ev["status"] == "cleared" and ev["cleared_time"] else now
    return max(0.0, (end - ev["first_seen"]) / 60.0)


def location_text(ev):
    c = ev["cur"]
    if ev["kind"] == "congestion":
        return f"{c['road']} ({c['from']} \u2192 {c['to']})" if c["from"] != c["to"] else c["road"]
    if ev["kind"] == "incident":
        return c.get("message") or c.get("type") or "Incident"
    if ev["kind"] == "roadworks":
        return c.get("road") or "Road works"
    return f"{c['name']} area ({'heavy' if c.get('level') == 'heavy' else 'moderate'} rain)"


def priority(ev, row, P, now, sched_hw=None, buses_n=None):
    """Section 30: a weighted score 0-100 from the components that are known (unknown ones are left out and the weights re-normalised). -> (score, level, parts)."""
    c = ev["cur"]
    parts = {}
    if ev["kind"] == "congestion":
        parts["speed"] = _clip(1.0 - c["avg_kmh"] / max(c["ref_kmh"], 1.0), 0, 1)
        parts["length"] = _clip(row["pct"] / 20.0, 0, 1)
    else:
        parts["speed"] = {"incident": P["sev_incident"], "roadworks": P["sev_roadwork"], "weather": P["sev_rain_heavy"] if c.get("level") == "heavy" else P["sev_rain_mod"]}[ev["kind"]]
        parts["length"] = _clip(row["pct"] / 50.0, 0, 1) if ev["kind"] == "weather" else _clip(row["pct"] / 20.0, 0, 1) if row["overlap_km"] else 0.1
    if buses_n is not None:
        parts["buses"] = _clip(buses_n / 4.0, 0, 1)
    if row.get("delay_min") is not None and sched_hw:
        parts["hw"] = _clip(row["delay_min"] / sched_hw, 0, 1)
    parts["duration"] = _clip(_duration_min(ev, now) / 60.0, 0, 1)
    w = {"speed": P["w_speed"], "length": P["w_length"], "buses": P["w_buses"], "hw": P["w_hw"], "duration": P["w_duration"]}
    tot = sum(w[k] for k in parts) or 1.0
    score = 100.0 * sum(w[k] * parts[k] for k in parts) / tot
    lvl = "critical" if score >= P["crit_score"] else "high" if score >= P["high_score"] else "monitor" if score >= P["monitor_score"] else "normal"
    return score, lvl, {k: _r(v, 2) for k, v in parts.items()}


def alerts(book, P, now, hw_map=None, bus_counts=None):
    """Every event x service x direction as one alert row (the controller's work queue)."""
    out = []
    for e in book["events"].values():
        if e["status"] not in ("active", "cleared"):
            continue
        c = e["cur"]
        nsvc = len({k.split("|")[0] for k in e["match"]})
        for key, m in e["match"].items():
            svc, d = key.split("|")
            d = int(d)
            aid = f"{e['id']}:{svc}:{d}"
            ack = book["acks"].get(aid)
            status, worse = alert_state(e, ack, P)
            row = {"overlap_km": m["overlap_km"], "pct": m["pct"]}
            delay = estimate_delay_min(m["overlap_km"], c["avg_kmh"], c["ref_kmh"]) if e["kind"] == "congestion" else None
            row["delay_min"] = delay
            hw = (hw_map or {}).get((svc, d))
            score, lvl, parts = priority(e, row, P, now, hw, (bus_counts or {}).get((svc, d)))
            out.append({"id": aid, "event": e["id"], "kind": e["kind"], "svc": svc, "dir": d, "location": location_text(e), "length_km": (c["length_m"] / 1000.0) if e["kind"] == "congestion" else None,
                        "speed_kmh": c["avg_kmh"] if e["kind"] == "congestion" else None, "ref_kmh": c.get("ref_kmh"), "duration_min": _duration_min(e, now), "overlap_km": m["overlap_km"], "route_pct": m["pct"],
                        "route_km": m["route_km"], "a_km": m["a_km"], "b_km": m["b_km"], "delay_min": delay, "status": status, "worsened": worse, "acked_time": ack["time"] if ack else None,
                        "acked_by": ack["by"] if ack else None, "score": score, "level": lvl, "parts": parts, "n_services": nsvc, "start": e["first_seen"], "level_detail": c.get("level"),
                        "planned": bool(e["kind"] == "roadworks" and c.get("start_epoch") and c["start_epoch"] > now),
                        "starts_in_min": ((c["start_epoch"] - now) / 60.0) if (e["kind"] == "roadworks" and c.get("start_epoch") and c["start_epoch"] > now) else None})
    return out


def acknowledge(book, alert_ids, by, now):
    """Record the acknowledgement of each existing, uncleared alert. Returns the ids that were acknowledged."""
    done = []
    for aid in alert_ids:
        eid = str(aid).split(":")[0]
        e = book["events"].get(eid)
        if not e or e["status"] != "active":
            continue
        svc_dir = str(aid).split(":", 1)[1] if ":" in str(aid) else ""
        if svc_dir.replace(":", "|") not in e["match"]:
            continue
        book["acks"][aid] = {"time": now, "by": by, "snap": condition_snapshot(e), "updates": 0}
        book["log"].append({"alert": aid, "time": now, "by": by, "snap": condition_snapshot(e)})
        done.append(aid)
    book["log"] = book["log"][-500:]
    return done


ORDER = {"critical": 0, "high": 1, "monitor": 2, "normal": 3}


def overview(book, P, now, services=None, direction=0, horizon=0, kinds=None, statuses=None, hw_map=None, bus_counts=None):
    """Filtered alert queue + the summary cards. services: set of service numbers (empty / None = all). statuses: subset of {unacknowledged, acknowledged, cleared}."""
    services = {s.strip().upper() for s in (services or []) if s and s.strip()}
    kinds = set(kinds or KINDS)
    statuses = set(statuses or ("unacknowledged", "acknowledged"))
    allrows = alerts(book, P, now, hw_map, bus_counts)
    rows = []
    for a in allrows:
        if services and a["svc"] not in services:
            continue
        if direction in (1, 2) and a["dir"] != direction:
            continue
        if a["kind"] not in kinds:
            continue
        if a["planned"] and not (horizon and a["starts_in_min"] <= horizon):        # planned roadworks only show inside the chosen time horizon
            continue
        grp = "cleared" if a["status"] == "cleared" else "unacknowledged" if a["status"] == "new" else "acknowledged"
        if grp not in statuses:
            continue
        rows.append(a)
    rows.sort(key=lambda a: (0 if a["status"] == "new" else 1, ORDER[a["level"]], -a["n_services"], -a["duration_min"]))
    cards = {}
    for k in KINDS:
        ek = {a["event"] for a in rows if a["kind"] == k}
        cards[k] = {"events": len(ek), "services": len({a["svc"] for a in rows if a["kind"] == k}), "unacknowledged": len({a["id"] for a in rows if a["kind"] == k and a["status"] == "new"})}
        was = sum(1 for e in book["events"].values() if e["kind"] == k and e["id"] in ek and e["alert_time"] and e["alert_time"] <= now - 3600)
        cards[k]["vs_last_hour"] = len(ek) - was
    total = {"services": len({a["svc"] for a in rows}), "alerts": len(rows), "unacknowledged": sum(1 for a in rows if a["status"] == "new")}
    return {"rows": rows, "cards": cards, "total": total}


# ----------------------------------------------------------------------------- layer 3: buses relative to the disruption, and the headway they will make
def classify_buses(buses, a_km, b_km, delay_min, P):
    """buses: [{"id", "pos_km", "eta_terminal_min"?}] on one service and direction; the disruption covers a_km..b_km of that route.
    approaching / inside / cleared, the time to impact for the approaching ones, and each bus's share of the estimated delay (a bus inside has only the rest of the stretch left)."""
    out = []
    span = max(b_km - a_km, 1e-6)
    for b in sorted(buses, key=lambda b: -b["pos_km"]):
        p = b["pos_km"]
        if p < a_km - 0.02:
            st, dist = "approaching", a_km - p
            tti, dl = dist / P["bus_run_kmh"] * 60.0, delay_min
        elif p <= b_km + 0.02:
            st, dist, tti = "inside", 0.0, 0.0
            dl = delay_min * _clip((b_km - p) / span, 0.0, 1.0)
        else:
            st, dist, tti, dl = "cleared", None, None, 0.0
        out.append({**b, "state": st, "dist_to_km": dist, "tti_min": tti, "delay_min": dl})
    return out


def headway_impact(cbuses, sched_hw, route_km, P):
    """Predicted arrivals at the interchange (the end of the route) with and without the disruption, and the headways between them (specification 24-25).
    A bus cannot overtake the one ahead, so a delayed bus also holds up those behind it."""
    rows = []
    for b in cbuses:
        eta = b.get("eta_terminal_min")
        src = "LTA" if eta is not None else "estimate"
        if eta is None:
            eta = max(0.0, route_km - b["pos_km"]) / P["bus_run_kmh"] * 60.0
        rows.append({**b, "arr0": float(eta), "eta_src": src})
    rows.sort(key=lambda r: r["arr0"])
    prev = None
    for r in rows:
        a1 = r["arr0"] + r["delay_min"]
        if prev is not None and a1 < prev + 0.5:
            a1 = prev + 0.5
        r["arr1"], prev = a1, a1
    h0 = [rows[i + 1]["arr0"] - rows[i]["arr0"] for i in range(len(rows) - 1)]
    h1 = [rows[i + 1]["arr1"] - rows[i]["arr1"] for i in range(len(rows) - 1)]
    for i, r in enumerate(rows):
        r["hw0"], r["hw1"] = (h0[i - 1] if i else None), (h1[i - 1] if i else None)
    m0, m1 = (max(h0) if h0 else None), (max(h1) if h1 else None)
    worse = bool(h1) and m1 - sched_hw >= P["hw_worse_min"] and m1 > (m0 or 0) + 0.5
    return {"rows": rows, "sched_hw": sched_hw, "hw_now": h0, "hw_pred": h1, "max_now": m0, "max_pred": m1, "deterioration": worse, "affected": sum(1 for r in rows if r["delay_min"] > 0.4)}


def regulation_options(rows, sched_hw, P):
    """Layer 4 (rule / simulation based): stretch the next departures at the interchange by s min each and see what the headways become.
    The buses that reach the interchange on time are held s, 2s, 3s ... (up to reg_deps of them, never more than reg_hold_max) so the gap in front of the delayed buses is smaller.
    Buses turn round after `min_layover_min`, so a bus departs its arrival + layover. Returns every option tried and the best one, or None if regulating does not help."""
    lay = P["min_layover_min"]
    deps0 = [r["arr0"] + lay for r in rows]
    deps1 = [r["arr1"] + lay for r in rows]
    n = len(rows)
    if n < 3:
        return {"options": [], "best": None, "reason": "Fewer than 3 buses are predicted at the interchange: nothing to regulate against."}
    prior = deps0[0] - sched_hw                                       # the departure before the first bus: on schedule
    early = [i for i, r in enumerate(rows) if r["delay_min"] < 0.5][: int(P["reg_deps"])]
    if not early:
        return {"options": [], "best": None, "reason": "Every bus in the window is delayed: there is no on-time departure to stretch."}

    def stats(dep):
        seq = [prior] + sorted(dep)
        h = [seq[i + 1] - seq[i] for i in range(len(seq) - 1)]
        return max(h), math.sqrt(sum((x - sched_hw) ** 2 for x in h) / len(h)), h
    base_max, base_rms, base_h = stats(deps1)
    options = []
    for s in range(0, int(P["reg_hold_max"]) + 1):
        dep = list(deps1)
        for j, i in enumerate(early, 1):
            dep[i] = deps1[i] + min(P["reg_hold_max"], s * j)
        mx, rms, h = stats(dep)
        options.append({"stretch_min": s, "max_hw": mx, "rms": rms, "next_hw": [sched_hw + s] * len(early) if s else [sched_hw] * len(early), "hw": h, "held": [round(min(P["reg_hold_max"], s * j), 1) for j in range(1, len(early) + 1)]})
    best = min(options, key=lambda o: (round(o["max_hw"], 1), round(o["rms"], 2), o["stretch_min"]))
    helps = best["stretch_min"] > 0 and best["max_hw"] <= base_max - 1.0
    return {"options": options, "best": best if helps else None, "no_action": {"max_hw": base_max, "rms": base_rms, "hw": base_h}, "n_early": len(early), "sched_hw": sched_hw,
            "reason": None if helps else "Stretching the next departures does not reduce the largest headway by at least 1 min."}


def restore_check(rows, route_km, sched_hw, current_hw, P):
    """Section 27: when the buses in the last part of the trip are predicted to arrive at about the scheduled headway again, the temporary (stretched) headway must go back to the original."""
    if not current_hw or current_hw <= sched_hw + 0.5:
        return {"recovered": False, "restore": False, "reason": "No temporary headway is in force."}
    cut = route_km * (1.0 - P["restore_last_pct"] / 100.0)
    last = sorted([r for r in rows if r["pos_km"] >= cut], key=lambda r: r["arr1"])
    if len(last) < 2:
        return {"recovered": False, "restore": False, "reason": f"Fewer than 2 buses are in the last {P['restore_last_pct']:g}% of the trip: recovery cannot be judged yet.", "n_last": len(last)}
    h = [last[i + 1]["arr1"] - last[i]["arr1"] for i in range(len(last) - 1)]
    tol = P["recover_tol_pct"] / 100.0 * sched_hw
    ok = all(abs(x - sched_hw) <= tol + EPS for x in h)
    return {"recovered": ok, "restore": ok, "n_last": len(last), "hw": h, "reason": None if ok else "Buses in the last part of the trip are still outside the headway tolerance."}


def recommend(alert, impact, reg, restore, current_hw, P):
    """The controller's recommended actions, each with the numbers behind it (specification 26: explainable, not a confidence percentage)."""
    out = []
    if impact and impact["deterioration"]:
        out.append({"key": "expect", "text": f"Headway deterioration expected on Svc {alert['svc']} Dir {alert['dir']}: largest predicted headway {impact['max_pred']:.0f} min (scheduled {impact['sched_hw']:.0f}, now {impact['max_now'] if impact['max_now'] is not None else float('nan'):.0f}), {impact['affected']} bus(es) affected.",
                    "basis": "Predicted arrivals at the interchange with the estimated traffic delay added."})
    if reg and reg.get("best"):
        b = reg["best"]
        out.append({"key": "regulate", "text": f"Stretch the next {len(b['held'])} departures to about {b['next_hw'][0]:.0f} min (scheduled {reg['sched_hw']:.0f}) to buy recovery time.",
                    "basis": f"Simulated: largest headway {reg['no_action']['max_hw']:.0f} min with no action, {b['max_hw']:.0f} min if the next departures are held {'/'.join(f'{x:g}' for x in b['held'])} min."})
    elif reg and reg.get("reason") and impact and impact["deterioration"]:
        out.append({"key": "noreg", "text": "Regulation is not recommended for now.", "basis": reg["reason"]})
    if restore and restore.get("restore"):
        out.append({"key": "restore", "text": f"Recovery detected: return the temporary {current_hw:g} min headway to the original {impact['sched_hw'] if impact else current_hw:g} min.",
                    "basis": "Buses in the last part of the trip are predicted within the headway tolerance again."})
    if not out:
        out.append({"key": "monitor", "text": "Monitor: no headway deterioration is predicted from this disruption yet.", "basis": "Re-checked at every refresh."})
    if impact and (impact["deterioration"] or (reg and reg.get("best"))):
        out.append({"key": "escalate", "text": "If the condition persists, consider halfway deployment (Halfway Optimiser) for the buses that will arrive late.", "basis": "Halfway deployment is a Layer 4 option; it is simulated on its own page."})
    return out
