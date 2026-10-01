"""Diversion Maps - OCC Diversion Decision Engine (V16.14). Pure logic, no I/O (same pattern as offservice.py).

app.py does all the fetching (LTA DataMall stops / routes / bus arrival / speed bands / incidents / road works, OSRM road routes,
OpenStreetMap restriction tags via Overpass) and hands plain lists to the functions below. Nothing here invents data:

  * where a service runs along the blocked road   -> the service's real-road line (busrouter.sg fitted to LTA stops) vs the block line
  * where a bus can leave / rejoin its route       -> where an OSRM road route actually departs from / returns to the service line
  * running time                                   -> LTA speed bands along the line (+ stop dwell); OSRM time x factor where bands are missing
  * which buses are coming                         -> LTA Bus Arrival positions projected onto the route
  * important stops                                -> LTA stop descriptions (Stn / Int / Ter / Hosp), number of services at the stop,
                                                      and the OCC's own list. The reason is always shown.
  * road suitability                               -> offservice.findings / suitability (OpenStreetMap tags only; never assumed)

Every figure is an estimate for decision support. The controller confirms the operational diversion.
"""
import math
import re

PARAMS = {
    "line_stop_tol_m": 45.0,    # route-line check: a stop further than this from the line means the line is wrong there
    "line_len_abs_m": 150.0,    # ...or a stop-to-stop length differing from LTA's official distance by more than this
    "line_len_rel": 0.15,       # ...and by more than this share of it
    "busroad_tol_m": 25.0,      # rule 1: a service's route line within this distance, same direction, proves the road is a bus road
    "busroad_stop_tol_m": 35.0,  # ...or the chord between two consecutive stops of a service (when it has no route line)
    "busroad_ang": 35.0,        # heading tolerance (degrees) - the opposite carriageway never counts
    "busroad_gap_m": 120.0,     # an unproven stretch longer than this makes the diversion not permitted
    "busroad_turn_m": 35.0,     # junction corners: samples this close to a turn are neutral (the corner joins two bus roads)
    "busroad_edge_m": 40.0,     # ignore the turn in / out at each end (the junction itself)
    "wait_max_min": 0.0,        # buses are never held at a blockage: every affected bus is diverted (WAIT / REGULATE is not used)
    "search_stages": (          # diversion search widens stage by stage until a main-road route that meets the LTA rules is found
        {"exit_km": (0.15, 0.4, 0.8, 1.5), "rejoin_km": (0.15, 0.4, 0.8, 1.5), "via_m": ()},
        {"exit_km": (2.5, 4.0), "rejoin_km": (2.5, 4.0), "via_m": (1500.0, 3000.0)},
        {"exit_km": (6.0, 8.0, 10.0), "rejoin_km": (6.0, 8.0, 10.0), "via_m": (3000.0, 5000.0)},
    ),
    "sim_closure_max_min": 120.0,  # beyond this, buses that reach the block are treated as unable to move for the rest of the simulation
    "long_exit_offsets_km": (2.5, 4.0),    # long closures: also search diversions that leave / rejoin further from the block
    "long_rejoin_offsets_km": (2.5, 4.0),
    "uturn_near_m": 20.0,       # U-turn test: the route comes back within this distance of where it has already been...
    "uturn_deg": 150.0,         # ...heading the opposite way (U-turn at a junction, at a roundabout, or round a block)
    "uturn_min_m": 15.0,        # ignore wiggles shorter than this along the route
    "on_road_m": 32.0,          # a service runs ALONG the blocked road when its line is within this distance of the block line...
    "parallel_deg": 35.0,       # ...and runs parallel to it (undirected when both directions are blocked)
    "min_overlap_m": 40.0,      # shortest run along the block that counts (shorter = the route only crosses the road)
    "on_route_m": 25.0,         # a diversion road route is "on the service route" within this distance (and in the same direction)
    "uses_block_m": 22.0,       # a candidate diversion that runs within this distance of the block, parallel, for >= 30 m uses the blocked road
    "cross_block_m": 10.0,      # ...or crosses the blocked section (both directions blocked) closer than this, away from its ends
    "exit_offsets_km": (0.15, 0.4, 0.8, 1.5),
    "rejoin_offsets_km": (0.15, 0.4, 0.8, 1.5),
    "via_offsets_m": (350.0, 800.0),
    "dwell_min": 0.33,          # minutes lost per bus stop (the same figure Route Traffic uses)
    "fallback_kmh": 22.0,       # bus running speed where LTA speed bands do not cover a stretch
    "bus_time_factor": 1.25,    # OSRM car time -> bus time where no speed-band data covers the diversion
    "red_min": 3.0,             # bus status: minutes to the diversion point
    "amber_min": 10.0,
    "approach_km": 9.0,         # buses up to this far before the block are "approaching"
    "bunch_ratio": 0.5,         # predicted headway < 0.5 x scheduled = bunching
    "close_ratio": 0.75,
    "gap_ratio": 1.5,           # > 1.5 x scheduled = gap
    "max_hold_min": 6.0,        # longest regulation hold the recovery suggestion proposes
    "queue_discharge_min": 0.5, # buses released from a queue at the block leave this far apart
    "excess_km": 3.0,           # added distance above which a diversion is flagged "excessive detour"
    "excess_ratio": 3.0,
    "hub_services": 8,          # a stop served by at least this many services is a transfer hub
    "max_options": 4,
    # V16.15 — several blockages, major-road policy, recommendation, network plan
    "merge_gap_m": 1500.0,      # two blocked runs on one service line closer than this are bypassed by ONE diversion
    "class_step_m": 20.0,       # road-class sampling interval along a diversion
    "small_max_share": 0.10,    # a "main roads" diversion may use small roads for at most 10 % of its length...
    "small_max_run_m": 250.0,   # ...and no single small-road stretch longer than this (junction connectors only)
    "unknown_max_share": 0.35,  # above this share of unclassified road the road class is "not verified"
    "junction_search_km": 3.0,  # look this far before/after the section for a major-road junction to divert at
    "junction_min_gap_m": 150.0,
    "major_via_reach_m": 700.0, # a via point is moved onto the nearest major road within this distance
    "road_load_buses_hr": 20.0, # added buses per hour on one road above which the network plan warns
}

# LTA Traffic Speed Bands RoadCategory: A Expressway, B Major Arterial, C Arterial, D Minor Arterial, E Small Road, F Slip Road, G no category
LTA_CLASS = {"A": "major", "B": "major", "C": "major", "F": "major", "D": "medium", "E": "small"}
LTA_CAT_NAME = {"A": "expressway", "B": "major arterial", "C": "arterial", "D": "minor arterial", "E": "small road", "F": "slip road"}
OSM_CLASS = {"motorway": "major", "motorway_link": "major", "trunk": "major", "trunk_link": "major", "primary": "major", "primary_link": "major",
             "secondary": "major", "secondary_link": "major", "tertiary": "medium", "tertiary_link": "medium",
             "residential": "small", "unclassified": "small", "service": "small", "living_street": "small", "track": "small", "road": "small"}

REF_LAT = 1.35
KX = 111320.0 * math.cos(math.radians(REF_LAT))
KY = 110574.0


# ============================================================================ geometry (local metres; accurate to well under 1 % across Singapore)
def xy(p):
    return (p[1] * KX, p[0] * KY)


def ll(q):
    return (q[1] / KY, q[0] / KX)


def dist_m(a, b):
    return math.hypot((a[1] - b[1]) * KX, (a[0] - b[0]) * KY)


def line_m(line):
    return sum(dist_m(a, b) for a, b in zip(line, line[1:]))


def cum_m(line):
    out = [0.0]
    for a, b in zip(line, line[1:]):
        out.append(out[-1] + dist_m(a, b))
    return out


def bearing(a, b):
    return math.degrees(math.atan2((b[1] - a[1]) * KX, (b[0] - a[0]) * KY)) % 360


def angdiff(a, b, undirected=False):
    d = abs((a - b + 180) % 360 - 180)
    return min(d, 180 - d) if undirected else d


def densify(line, step_m=20.0):
    """-> [(lat, lon)] with no gap longer than step_m (keeps the original vertices)."""
    if len(line) < 2:
        return list(line)
    out = [tuple(line[0])]
    for a, b in zip(line, line[1:]):
        L = dist_m(a, b)
        n = max(1, int(math.ceil(L / step_m)))
        for k in range(1, n + 1):
            f = k / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def nearest_on_line(p, line, cum=None):
    """-> (distance m, position m along line, segment index, bearing of that segment)"""
    if cum is None:
        cum = cum_m(line)
    px, py = xy(p)
    best = (1e18, 0.0, 0, 0.0)
    for i in range(len(line) - 1):
        ax, ay = xy(line[i])
        bx, by = xy(line[i + 1])
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best[0]:
            best = (d, cum[i] + t * (cum[i + 1] - cum[i]), i, bearing(line[i], line[i + 1]))
    return best


def project_window(p, line, cum, lo=-1e18, hi=1e18):
    """nearest point on `line` restricted to positions [lo, hi] (m) -> (distance m, position m). Loop routes pass the same
    place twice; the window (from where LTA says the bus is heading) picks the right passage."""
    px, py = xy(p)
    best = (1e18, None)
    for i in range(len(line) - 1):
        if cum[i + 1] < lo or cum[i] > hi:
            continue
        ax, ay = xy(line[i])
        bx, by = xy(line[i + 1])
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        s = cum[i] + t * (cum[i + 1] - cum[i])
        if s < lo or s > hi:
            s = min(max(s, lo), hi)
            q = point_at(line, cum, s)
            d = dist_m(p, q)
        else:
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best[0]:
            best = (d, s)
    return best


def stop_positions(line, cum, stops):
    """position (m) of every stop along the line, walked forward so loop services stay in order"""
    out, prev = [], 0.0
    for s in stops:
        d, pos = project_window((s["lat"], s["lon"]), line, cum, prev - 5.0, 1e18)
        if pos is None:
            pos = prev
        pos = max(pos, prev)
        out.append(pos)
        prev = pos
    return out


def point_at(line, cum, s):
    if not line:
        return None
    if len(line) == 1 or s <= 0:
        return tuple(line[0])
    if s >= cum[-1]:
        return tuple(line[-1])
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= s:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo]
    f = 0.0 if seg <= 0 else (s - cum[lo]) / seg
    a, b = line[lo], line[hi]
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def cut(line, cum, a, b):
    """part of a line between positions a and b (metres)"""
    if len(line) < 2:
        return list(line)
    if b < a:
        a, b = b, a
    mid = [tuple(line[i]) for i in range(len(line)) if a < cum[i] < b]
    return [point_at(line, cum, a)] + mid + [point_at(line, cum, b)]


def bbox(line, pad_m=0.0):
    la = [p[0] for p in line]
    lo = [p[1] for p in line]
    dy, dx = pad_m / KY, pad_m / KX
    return (min(la) - dy, min(lo) - dx, max(la) + dy, max(lo) + dx)


def bbox_hit(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def simplify(line, n=200):
    if len(line) <= n:
        return [[round(p[0], 6), round(p[1], 6)] for p in line]
    step = (len(line) - 1) / (n - 1)
    return [[round(line[round(i * step)][0], 6), round(line[round(i * step)][1], 6)] for i in range(n)]


# ============================================================================ 1. which services run along the blocked section
def overlap_runs(route_line, block_line, directed=False, P=None):
    """Runs where `route_line` travels ALONG `block_line`.
    -> [{"a": m, "b": m, "len": m}] positions along route_line (metres from its first vertex). Crossing the road is not an overlap."""
    P = P or PARAMS
    if len(route_line) < 2 or len(block_line) < 2:
        return []
    if not bbox_hit(bbox(route_line, P["on_road_m"] + 5), bbox(block_line, P["on_road_m"] + 5)):
        return []
    bcum = cum_m(block_line)
    blen = bcum[-1]
    need = min(P["min_overlap_m"], 0.6 * blen) if blen > 0 else P["min_overlap_m"]
    bb = bbox(block_line, P["on_road_m"] + 30)
    rcum = cum_m(route_line)
    runs, cur = [], None
    gap_ok = 40.0
    for i in range(len(route_line) - 1):
        a, b = route_line[i], route_line[i + 1]
        seg = rcum[i + 1] - rcum[i]
        if seg <= 0:
            continue
        sb = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        if not bbox_hit(sb, bb):
            if cur and rcum[i] - cur["b"] > gap_ok:
                runs.append(cur); cur = None
            continue
        brg = bearing(a, b)
        n = max(1, int(math.ceil(seg / 15.0)))
        for k in range(n):
            f0, f1 = k / n, (k + 1) / n
            m = (a[0] + (b[0] - a[0]) * (f0 + f1) / 2, a[1] + (b[1] - a[1]) * (f0 + f1) / 2)
            d, _, _, bb_brg = nearest_on_line(m, block_line, bcum)
            ok = d <= P["on_road_m"] and angdiff(brg, bb_brg, undirected=not directed) <= P["parallel_deg"]
            s0, s1 = rcum[i] + seg * f0, rcum[i] + seg * f1
            if ok:
                if cur and s0 - cur["b"] <= gap_ok:
                    cur["b"] = s1
                else:
                    if cur:
                        runs.append(cur)
                    cur = {"a": s0, "b": s1}
            elif cur and s0 - cur["b"] > gap_ok:
                runs.append(cur); cur = None
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        r["len"] = r["b"] - r["a"]
        if r["len"] >= need:
            out.append(r)
    return out


def stops_in(stop_s, a, b, pad=0.0):
    """indexes of stops whose position along the route lies in [a - pad, b + pad]"""
    return [i for i, s in enumerate(stop_s) if a - pad <= s <= b + pad]


# ============================================================================ 2. important stops (always with the reason)
_IMP = (
    (re.compile(r"\b(STN|STATION|MRT|LRT)\b"), "MRT / LRT connection"),
    (re.compile(r"\b(INT|INTERCHANGE)\b"), "Bus interchange"),
    (re.compile(r"\b(TER|TERMINAL|TERMINUS)\b"), "Bus terminal"),
    (re.compile(r"\b(HOSP|HOSPITAL|POLYCLINIC|MED CTR|MEDICAL)\b"), "Hospital / medical"),
)


def stop_importance(name, n_services, marked=None, P=None):
    """-> list of reasons ([] = normal stop). `marked`: the OCC's own reason for this stop, if it is on the OCC list."""
    P = P or PARAMS
    out = []
    if marked:
        out.append("OCC important stop" + (f" ({marked})" if isinstance(marked, str) and marked.strip() else ""))
    up = (name or "").upper()
    for rx, why in _IMP:
        if rx.search(up):
            out.append(why)
    if n_services and n_services >= P["hub_services"]:
        out.append(f"Transfer hub ({n_services} services)")
    return out


# ============================================================================ 3. running-time profile
def profile(line, speed_fn, dwell_at=None, P=None, fallback_kmh=None):
    """Cumulative minutes at every vertex of `line`.
    speed_fn(lat, lon, bearing) -> km/h from LTA speed bands, or None where no band matches.
    dwell_at: positions (m) of stops served along the line; each adds P["dwell_min"].
    -> {"cum": [m], "t": [min], "known_share": 0..1}"""
    P = P or PARAMS
    fb = fallback_kmh or P["fallback_kmh"]
    cum = cum_m(line)
    t = [0.0]
    known = 0.0
    dw = sorted(dwell_at or [])
    di = 0
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        seg = cum[i + 1] - cum[i]
        kmh = None
        if seg > 0:
            try:
                kmh = speed_fn((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, bearing(a, b)) if speed_fn else None
            except Exception:
                kmh = None
        if kmh:
            known += seg
        v = max(5.0, kmh or fb)
        add = (seg / 1000.0) / v * 60.0
        while di < len(dw) and dw[di] <= cum[i + 1]:
            if dw[di] >= cum[i] - 1e-6:
                add += P["dwell_min"]
            di += 1
        t.append(t[-1] + add)
    return {"cum": cum, "t": t, "known_share": (known / cum[-1]) if cum[-1] > 0 else 0.0}


def t_at(prof, s):
    cum, t = prof["cum"], prof["t"]
    if not cum:
        return 0.0
    if s <= 0:
        return t[0]
    if s >= cum[-1]:
        return t[-1]
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= s:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo]
    f = 0.0 if seg <= 0 else (s - cum[lo]) / seg
    return t[lo] + (t[hi] - t[lo]) * f


# ============================================================================ 4. candidate diversion geometry
def uses_block(cand_line, block_line, directed=False, P=None):
    """True when a candidate road route runs along the blocked section (or crosses it, if both directions are blocked)."""
    P = P or PARAMS
    if len(cand_line) < 2 or len(block_line) < 2:
        return False
    if not bbox_hit(bbox(cand_line, 40), bbox(block_line, 40)):
        return False
    bcum = cum_m(block_line)
    blen = bcum[-1]
    pts = densify(cand_line, 10.0)
    run = 0.0
    for p, q in zip(pts, pts[1:]):
        d, s, _, bb = nearest_on_line(p, block_line, bcum)
        seg = dist_m(p, q)
        if d <= P["uses_block_m"] and angdiff(bearing(p, q), bb, undirected=not directed) <= P["parallel_deg"]:
            run += seg
            if run >= 30.0:
                return True
        else:
            run = 0.0
        if not directed and d <= P["cross_block_m"] and 25.0 < s < blen - 25.0:
            return True
    return False


def departure(cand_line, svc_line, svc_cum, P=None, start_window=None):
    """Where a road route from a point on the service route leaves it and where it comes back.
    Positions are tracked FORWARD along the service route from where the route starts, never by a global nearest-point
    search: loop services pass the same road twice, and a global search can snap the rejoin onto the wrong passage
    (giving impossible results such as -17 km). After leaving, the rejoin may be at most a plausible distance ahead.
    -> {"leave_s", "rejoin_s" (positions along the service line, m), "i0", "i1" (indexes into the densified candidate),
        "pts": densified candidate} or None when it never leaves the service route / never comes back in order."""
    P = P or PARAMS
    pts = densify(cand_line, 15.0)
    if len(pts) < 3:
        return None
    ccum = cum_m(pts)
    lo0, hi0 = start_window if start_window else (-1e18, 1e18)
    d0, cur = project_window(pts[0], svc_line, svc_cum, lo0, hi0)
    if cur is None:
        return None

    def seg_brg(s_):
        a_, b_ = point_at(svc_line, svc_cum, max(0.0, s_ - 6)), point_at(svc_line, svc_cum, min(svc_cum[-1], s_ + 6))
        return bearing(a_, b_)
    on, pos = [], []
    left_k = None
    for k, p in enumerate(pts):
        q = pts[k + 1] if k + 1 < len(pts) else None
        pr = pts[k - 1] if k > 0 else None
        brg = bearing(p, q) if q else bearing(pr, p)
        if left_k is None:
            lo, hi = cur - 40.0, cur + 200.0
        else:     # off the route: the normal route from the leave point to the rejoin can't be implausibly long
            lo, hi = cur - 40.0, cur + 2.5 * (ccum[k] - ccum[left_k]) + 800.0
        d, s_ = project_window(p, svc_line, svc_cum, lo, hi)
        ok = s_ is not None and d <= P["on_route_m"] and angdiff(brg, seg_brg(s_)) <= 60.0
        on.append(ok)
        pos.append(s_ if ok else None)
        if ok:
            cur = s_
            left_k = None
        elif left_k is None:
            left_k = k
    off = [k for k, x in enumerate(on) if not x]
    if not off:
        return None
    i0 = max(0, off[0] - 1)
    i1 = min(len(pts) - 1, off[-1] + 1)
    if pos[i0] is None or pos[i1] is None or pos[i1] <= pos[i0]:
        return None
    return {"leave_s": pos[i0], "rejoin_s": pos[i1], "i0": i0, "i1": i1, "pts": pts,
            "leave_pt": pts[i0], "rejoin_pt": pts[i1], "start_on": on[0], "end_on": on[-1]}


def offset_point(p, brg_deg, dist):
    r = math.radians(brg_deg)
    return (p[0] + dist * math.cos(r) / KY, p[1] + dist * math.sin(r) / KX)


def via_points(block_line, P=None):
    """points either side of the middle of the block, perpendicular to it (used to push the road router round the block)"""
    P = P or PARAMS
    cum = cum_m(block_line)
    mid = point_at(block_line, cum, cum[-1] / 2)
    d, s, i, brg = nearest_on_line(mid, block_line, cum)
    out = []
    for off in P["via_offsets_m"]:
        out.append(offset_point(mid, brg + 90, off))
        out.append(offset_point(mid, brg - 90, off))
    return out


def road_signature(groups):
    return " > ".join(g["road"] for g in groups if g.get("road"))


# ============================================================================ 5. buses
def bus_status(min_to_exit, pos_s, leave_s, block_a, block_b, P=None):
    """-> (code, label). GREEN not yet approaching / AMBER approaching / RED act now / GREY passed the diversion point."""
    P = P or PARAMS
    if pos_s > block_b:
        return "passed_block", "Past the blocked section"
    if pos_s >= block_a:
        return "inside", "Inside the affected section"
    if leave_s is not None and pos_s > leave_s + 10:
        return "passed_exit", "Passed the diversion point"
    if min_to_exit is None:
        return "unknown", "Position only"
    if min_to_exit <= P["red_min"]:
        return "red", "Immediate action"
    if min_to_exit <= P["amber_min"]:
        return "amber", "Approaching diversion point"
    return "green", "Not yet approaching"


def label_buses(buses, svc):
    """Positional identifiers (LTA Bus Arrival carries no registration): nearest the block = A."""
    order = sorted(range(len(buses)), key=lambda k: -(buses[k]["s"] if buses[k].get("s") is not None else 1e12))
    for n, k in enumerate(order):
        tag = ""
        x = n
        while True:
            tag = chr(65 + x % 26) + tag
            x = x // 26 - 1
            if x < 0:
                break
        buses[k]["label"] = f"{svc}{tag}"
    return buses


# ============================================================================ 6. simulation of no action vs a diversion option
def simulate(buses, svc_prof, block_a, block_b, ref_s, closure_min, H, option=None, P=None):
    """Predicted minute (from now) each bus passes the reference point `ref_s` on its route (the rejoin point of the option,
    or just after the block for No action).
    buses: [{"label", "s" (m along route), "eta_exit" (optional, minutes, LTA-based)}]
    option: {"leave_s", "rejoin_s", "div_min"} or None (= no action: buses queue at the block until it reopens).
    closure_min: expected closure length, None = until further notice.
    -> [{"label", "t_ref", "t_base", "mode": diverted | queued | normal | ahead | stuck, "wait", "t_exit"}] sorted by t_ref"""
    P = P or PARAMS
    out = []
    reopen = closure_min if closure_min is not None else None
    queue_last = None
    t_ref_base = t_at(svc_prof, ref_s)
    for b in sorted(buses, key=lambda x: -x["s"]):           # nearest the block first (queue order)
        s = b["s"]
        base = t_ref_base - t_at(svc_prof, s)                 # minutes to ref with the road open
        rec = {"label": b["label"], "t_base": base, "wait": 0.0, "t_exit": None}
        if s >= ref_s:
            rec.update(mode="ahead", t_ref=base)              # already past the reference point (negative = passed that long ago)
        elif s > block_b:
            rec.update(mode="normal", t_ref=base)
        elif option is not None and s <= option["leave_s"] + 10:
            t_exit = b.get("eta_exit")
            if t_exit is None:
                t_exit = t_at(svc_prof, option["leave_s"]) - t_at(svc_prof, s)
            t_ref = t_exit + option["div_min"] + (t_ref_base - t_at(svc_prof, option["rejoin_s"]))
            rec.update(mode="diverted", t_ref=t_ref, t_exit=t_exit)
        else:
            t_block = max(0.0, t_at(svc_prof, block_a) - t_at(svc_prof, s))
            if reopen is None:
                rec.update(mode="stuck", t_ref=None, wait=None, t_block=t_block)
            else:
                release = max(t_block, reopen)
                if queue_last is not None and release - queue_last < P["queue_discharge_min"] and t_block < reopen:
                    release = queue_last + P["queue_discharge_min"]
                if t_block < reopen:
                    queue_last = release
                rec.update(mode="queued" if t_block < reopen else "normal", t_block=t_block, wait=max(0.0, release - t_block),
                           t_ref=release + (t_ref_base - t_at(svc_prof, block_a)))
        out.append(rec)
    out.sort(key=lambda r: (r["t_ref"] is None, r["t_ref"] if r["t_ref"] is not None else 0))
    return out


def headways(sim, H, P=None):
    """headways between consecutive buses at the reference point -> {"gaps": [...], "max", "min", "risk", "pairs": [...]}"""
    P = P or PARAMS
    ts = [r for r in sim if r["t_ref"] is not None]
    gaps = []
    for a, b in zip(ts, ts[1:]):
        gaps.append({"front": a["label"], "rear": b["label"], "t_front": a["t_ref"], "t_rear": b["t_ref"], "gap": b["t_ref"] - a["t_ref"]})
    if not gaps:
        return {"gaps": [], "max": None, "min": None, "risk": "unknown", "bunch_at": None}
    mx = max(g["gap"] for g in gaps)
    mn = min(g["gap"] for g in gaps)
    risk = "unknown"
    bunch_at = None
    if H:
        if mn < P["bunch_ratio"] * H:
            risk = "high"
            bunch_at = next(g["t_rear"] for g in gaps if g["gap"] < P["bunch_ratio"] * H)
        elif mn < P["close_ratio"] * H:
            risk = "moderate"
        else:
            risk = "low"
    return {"gaps": gaps, "max": mx, "min": mn, "risk": risk, "bunch_at": bunch_at}


def regulation(sim, H, P=None):
    """Suggested holds at the first timing point after the rejoin point: spread the buses to the average spacing available
    (or the scheduled headway if that is smaller). Holds are capped at P["max_hold_min"]. Suggestions only - never executed.
    -> {"target", "holds": [{"label", "hold", "action"}], "after": headways(...)}"""
    P = P or PARAMS
    ts = [dict(r) for r in sim if r["t_ref"] is not None]
    if len(ts) < 2 or not H:
        return {"target": None, "holds": [{"label": r["label"], "hold": 0.0, "action": "Continue"} for r in ts], "after": headways(ts, H, P)}
    span = ts[-1]["t_ref"] - ts[0]["t_ref"]
    avg = span / (len(ts) - 1)
    target = min(H, avg) if avg > 0 else H
    new = [ts[0]["t_ref"]]
    holds = [0.0]
    for r in ts[1:]:
        want = new[-1] + target
        hold = min(P["max_hold_min"], max(0.0, want - r["t_ref"]))
        if r["mode"] == "ahead":
            hold = 0.0                                       # already past the regulation point
        holds.append(hold)
        new.append(r["t_ref"] + hold)
    after = [dict(r, t_ref=t) for r, t in zip(ts, new)]
    out = []
    for r, h in zip(ts, holds):
        act = "Continue" if h < 0.5 else f"Regulate +{round(h)} min"
        out.append({"label": r["label"], "hold": round(h, 1), "action": act, "mode": r["mode"]})
    return {"target": round(target, 1), "holds": out, "after": headways(after, H, P)}


def recovery_minutes(sim, reg, H, P=None):
    """minutes from now until the service is expected to be back within headway tolerance (None = not by regulation alone)"""
    P = P or PARAMS
    aff = [r for r in sim if r["mode"] in ("diverted", "queued") and r["t_ref"] is not None]
    if not aff:
        return 0.0
    hold = {h["label"]: h["hold"] for h in reg["holds"]}
    last = max(r["t_ref"] + hold.get(r["label"], 0.0) for r in aff)
    a = reg["after"]
    if H and a["gaps"] and (a["min"] < P["bunch_ratio"] * H or a["max"] > 2.0 * H):     # a gap this big cannot be closed by holding
        return None
    return max(0.0, last)


def bus_minutes(sim):
    """extra bus-minutes against the road being open (diverted / queued buses only)"""
    return sum((r["t_ref"] - r["t_base"]) for r in sim if r["mode"] in ("diverted", "queued") and r["t_ref"] is not None)


# ============================================================================ 7. what happens next
def timeline(sim, hw, reg, recov, closure_min, road, option_name=None):
    ev = [{"t": 0.0, "kind": "block", "text": f"{road} blocked"}]
    div = [r for r in sim if r["mode"] == "diverted"]
    qd = [r for r in sim if r["mode"] in ("queued", "stuck")]
    for r in sorted(div, key=lambda x: x["t_exit"] or 0)[:4]:
        ev.append({"t": max(0.0, r["t_exit"] or 0.0), "kind": "exit", "text": f"{r['label']} reaches the diversion point"})
        ev.append({"t": max(0.0, r["t_ref"]), "kind": "rejoin", "text": f"{r['label']} rejoins the route"})
    for r in qd[:3]:
        tb = r.get("t_block")
        if tb is not None:
            ev.append({"t": max(0.0, tb), "kind": "queue", "text": f"{r['label']} reaches the blockage and has to wait"})
    if closure_min is not None:
        ev.append({"t": float(closure_min), "kind": "reopen", "text": "Road expected to reopen (controller's estimate)"})
    if hw.get("bunch_at") is not None:
        ev.append({"t": max(0.0, hw["bunch_at"]), "kind": "bunch", "text": "Bunching predicted after the rejoin point"})
        if any(h["hold"] >= 0.5 for h in reg["holds"]):
            ev.append({"t": max(0.0, hw["bunch_at"]) + 0.1, "kind": "regulate", "text": "Headway regulation required"})
    if recov is not None:
        ev.append({"t": recov, "kind": "stable", "text": "Service expected to stabilise"})
    else:
        ev.append({"t": max([e["t"] for e in ev] + [0.0]) + 0.2, "kind": "unstable",
                   "text": "Headway not recovered by regulation alone - consider Halfway Planner"})
    ev.sort(key=lambda e: e["t"])
    for e in ev:
        e["t"] = round(e["t"], 1)
    return ev


def wait_allowed(closure_min, P=PARAMS):
    """Buses are never held at a blockage (default wait_max_min = 0): a diversion is always required.
    Kept as a setting so an operator could allow holding for very short, known closures."""
    return P["wait_max_min"] > 0 and closure_min is not None and closure_min <= P["wait_max_min"]


def closure_desc(closure_min):
    if closure_min is None:
        return "until further notice"
    if closure_min >= 120:
        return f"about {closure_min / 60:.0f} hours"
    return f"{closure_min:g} minutes"


def wait_or_divert(sim_none, opts_eval, closure_min, P=PARAMS):
    """Plain comparison of holding against diverting - no score, just the consequence of each."""
    if not wait_allowed(closure_min, P):
        found = any(o.get("added_min") is not None for o in opts_eval)
        return {"kind": "divert_only", "wait_allowed": False,
                "text": "Buses are never held at a blockage \u2014 even a whole-day closure would leave them unable to move. Every affected bus is diverted, following the LTA rules: "
                        "1 safety (main roads only), 2 fewest bus stops skipped, 3 no U-turn."
                        + ("" if found else " No route meeting all three rules was found in the widest search: escalate.")}
    q = [r for r in sim_none if r["mode"] == "queued"]
    if not q:
        return {"kind": "wait", "text": f"No bus is expected to reach the block within the {closure_min:g}-minute closure: holding upstream / regulating may be enough."}
    mx = max(r["wait"] for r in q)
    best = min((o for o in opts_eval if o.get("added_min") is not None), key=lambda o: o["added_min"], default=None)
    if best is None:
        return {"kind": "wait", "text": f"No feasible diversion found; {len(q)} bus(es) would wait up to {mx:.0f} min at the block."}
    if mx <= best["added_min"]:
        return {"kind": "wait", "text": f"Waiting costs up to {mx:.0f} min per bus ({len(q)} bus(es)); the quickest diversion adds {best['added_min']:.0f} min and skips "
                                        f"{best['skipped_n']} stop(s). WAIT / REGULATE is a reasonable alternative."}
    return {"kind": "divert", "text": f"Without a diversion {len(q)} bus(es) would wait up to {mx:.0f} min at the block; the quickest diversion adds {best['added_min']:.0f} min."}


# ============================================================================ 8. return to normal
def return_to_normal(buses, leave_s, rejoin_s, block_a):
    """buses: [{"label", "s", "on_diversion": bool, "eta_rejoin" (min, if on the diversion)}] -> per-bus instruction + normalisation minutes"""
    rows = []
    for b in sorted(buses, key=lambda x: -(x.get("s") or 0)):
        if b.get("on_diversion"):
            rows.append({"label": b["label"], "state": "On diversion", "action": "Complete diversion", "eta": b.get("eta_rejoin")})
        elif b.get("s") is not None and b["s"] < leave_s:
            rows.append({"label": b["label"], "state": "Approaching diversion point", "action": "Resume normal route", "eta": None})
        elif b.get("s") is not None and b["s"] < block_a:
            rows.append({"label": b["label"], "state": "Between diversion point and road", "action": "Resume normal route (road reopened)", "eta": None})
        else:
            rows.append({"label": b["label"], "state": "Past the section", "action": "No change", "eta": None})
    on = [r["eta"] for r in rows if r["action"] == "Complete diversion" and r["eta"] is not None]
    return {"rows": rows, "still_affected": sum(1 for r in rows if r["action"] != "No change"), "last_rejoin_min": max(on) if on else 0.0}


# ============================================================================ 9. notice
def notice(plan):
    """Plain-text operational message from a confirmed plan (dict built by app.py)."""
    L = [f"ROAD DIVERSION \u2014 {plan.get('road') or 'ROAD BLOCKAGE'}".upper(), ""]
    for s in plan.get("services") or []:
        L.append(f"Service: {s['service']}")
        L.append(f"Direction: {s['direction']}")
        L.append("")
        route = s.get("roads") or []
        if route:
            L.append("Diversion:")
            L.append(route[0])
            for r in route[1:]:
                L.append(f"\u2192 {r}")
            L.append("\u2192 Rejoin normal route" + (f" at {s['rejoin_name']}" if s.get("rejoin_name") else ""))
            if s.get("bus_road"):
                L.append(f"Safety (LTA rule 1): {s['bus_road']}")
            L.append("No U-turn on this diversion.")
        else:
            L.append(f"Action: {s.get('action') or 'Wait / regulate (no diversion)'}")
        L.append("")
        sk = s.get("skipped") or []
        if sk:
            L.append("Skipped Stops:")
            for x in sk:
                L.append(f"{x['code']}" + (f"  {x['name']}" if x.get("name") else "") + ("  (important)" if x.get("important") else ""))
            L.append("")
        hl = [h for h in (s.get("holds") or []) if h.get("action") and h.get("action") != "Continue"]
        if hl:
            L.append("Post-diversion regulation (controller to confirm):")
            for h in hl:
                L.append(f"{h['label']}  {h['action']}")
            L.append("")
        if s.get("buses") is not None:
            L.append(f"Affected Buses: {s['buses']}")
            L.append("")
    L.append(f"Effective: {plan.get('effective') or '--:--'} hrs")
    if plan.get("closure"):
        L.append(f"Expected closure: {plan['closure']}")
    L.append(f"Status: {str(plan.get('status') or 'ACTIVE').upper()}")
    if plan.get("revision") and plan["revision"] > 1:
        L.append(f"Revision: {plan['revision']}")
    if plan.get("by"):
        L.append(f"Issued by: {plan['by']}")
    return "\n".join(L).strip() + "\n"


# ============================================================================ 10. several blockages on one service line
def merge_runs(runs, gap_m=None):
    """runs: [{"a","b","len","block"}] from every blockage on ONE service line -> sections [{"a","b","len","blocks":[i..]}]
    Runs closer than gap_m along the line become one section, bypassed by one diversion (a bus cannot usefully rejoin between them)."""
    gap_m = PARAMS["merge_gap_m"] if gap_m is None else gap_m
    out = []
    for r in sorted(runs, key=lambda x: x["a"]):
        if out and r["a"] - out[-1]["b"] <= gap_m:
            o = out[-1]
            o["b"] = max(o["b"], r["b"]); o["len"] += r["len"]
            if r["block"] not in o["blocks"]:
                o["blocks"].append(r["block"])
            o["parts"].append((r["a"], r["b"], r["block"]))
        else:
            out.append({"a": r["a"], "b": r["b"], "len": r["len"], "blocks": [r["block"]], "parts": [(r["a"], r["b"], r["block"])]})
    return out


# ============================================================================ 11. road class (main roads vs small roads)
def road_mix(classes, step_m=None, P=None):
    """classes: one class per sample along the diversion ("major" | "medium" | "small" | None=unknown).
    -> shares, longest small-road stretch, and a label. MAIN ROADS / SMALL ROADS / NOT VERIFIED."""
    P = P or PARAMS
    step_m = step_m or P["class_step_m"]
    n = len(classes) or 1
    cnt = {"major": 0, "medium": 0, "small": 0, None: 0}
    run = best = 0
    for c in classes:
        cnt[c if c in cnt else None] += 1
        run = run + 1 if c == "small" else 0
        best = max(best, run)
    sh = {k: cnt[k] / n for k in ("major", "medium", "small")}
    sh["unknown"] = cnt[None] / n
    small_run = best * step_m
    if sh["small"] > P["small_max_share"] or small_run > P["small_max_run_m"]:
        label = "SMALL ROADS"
        text = f"{round(sh['small'] * 100)}% on small roads (longest stretch {round(small_run)} m) \u2014 not suitable as a bus diversion by policy"
    elif sh["unknown"] > P["unknown_max_share"]:
        label = "NOT VERIFIED"
        text = f"road class unknown for {round(sh['unknown'] * 100)}% of the route (not in LTA road network or OpenStreetMap) \u2014 verify"
    else:
        label = "MAIN ROADS"
        text = f"{round((sh['major']) * 100)}% expressway / arterial, {round(sh['medium'] * 100)}% minor arterial" + \
               (f", {round(sh['small'] * 100)}% small-road connector" if sh["small"] > 0 else "")
    return {"label": label, "text": text, "major": round(sh["major"], 3), "medium": round(sh["medium"], 3), "small": round(sh["small"], 3),
            "unknown": round(sh["unknown"], 3), "small_run_m": round(small_run)}


# ============================================================================ 12. recommendation (rules, not a score)
FEAS_ORDER = {"HIGH": 0, "VERIFICATION REQUIRED": 1, "LOW": 2, "NOT SUITABLE": 3}
CLASS_ORDER = {"MAIN ROADS": 0, "NOT VERIFIED": 1, "SMALL ROADS": 2}


LTA_RULES = ("1. Safety: only roads bus services already use (with bus stops); double-deckers only where double-deck services run",
             "2. Skip as few bus stops as possible",
             "3. No U-turn")


def option_order(o):
    """LTA diversion rules, in order (no score):
    1. safety - usable for this bus, then main roads before unverified roads (small roads are never permitted)
    2. fewest bus stops skipped, then fewest important stops skipped
    3. no U-turn (hard rule: routes with a U-turn never reach this point)
    then verified before verification-required, then least added running time."""
    br = o.get("bus_road") or {}
    return (not permitted(o), DD_ORDER.get(br.get("dd"), 0),
            o.get("skipped_n", 0), o.get("important_n", 0),
            CLASS_ORDER.get((o.get("road_class") or {}).get("label"), 1),
            FEAS_ORDER.get(o.get("feasibility"), 1), o.get("added_min") if o.get("added_min") is not None else 99)


DD_ORDER = {"PROVEN": 0, "N/A": 0, "UNCONFIRMED": 1, "SD_ONLY": 2}


def permitted(o):
    """LTA rule 1 (safety): only roads bus services already use in this direction (with their bus stops / on their
    route); for a double-decker, only where double-deck services run. Falls back to "no small roads" if the bus
    network could not be checked."""
    if o.get("feasibility") == "NOT SUITABLE":
        return False
    br = o.get("bus_road")
    if br:
        return bool(br.get("ok")) and br.get("dd") != "SD_ONLY"
    return (o.get("road_class") or {}).get("label") != "SMALL ROADS"


def recommend(opts, wod, closure_min):
    """-> {"action": "divert"|"wait"|"manual", "option": n|None, "route_if_extended": n|None, "headline", "reasons": [..], "cautions": [..]}
    Always proposes a route when any acceptable one exists. Small-road routes are never recommended."""
    ok = [o for o in opts if permitted(o)]
    small = [o for o in opts if not permitted(o) and o.get("feasibility") != "NOT SUITABLE"]
    if not ok:
        if wait_allowed(closure_min):
            return {"action": "wait", "option": None, "route_if_extended": None,
                    "headline": "NO MAIN-ROAD DIVERSION FOUND",
                    "reasons": ["Every road route found around the section either uses the blocked road, needs a U-turn, is unsuitable for this bus, or runs on small roads."],
                    "cautions": ([f"{len(small)} route(s) found but not permitted by LTA rule 1."] if small else []) +
                                ["Hold / regulate upstream for this short closure and plan the diversion manually with the depot if it extends."]}
        return {"action": "manual", "option": None, "route_if_extended": None,
                "headline": "NO DIVERSION MEETING THE LTA RULES FOUND \u2014 ESCALATE",
                "reasons": ["Buses are never held at a blockage, so a diversion is required.",
                            "Searched routes leaving and rejoining up to 10 km either side of the block.",
                            "Every road route found around the section either uses the blocked road, needs a U-turn, is unsuitable for this bus, or runs on small roads."],
                "cautions": ([f"{len(small)} route(s) found but not permitted by LTA rule 1 (not a bus road, or no double-deck service there) \u2014 review them with the depot."] if small else []) +
                            ["Escalate to the Duty Operations Manager and depot.",
                             "Options to consider (operational verification required): short-working \u2014 terminate before the block and restart after it, "
                             "using a turning facility that needs no U-turn; a wider diversion planned with the depot; LTA / Traffic Police assistance for buses already past the diversion point."]}
    best = sorted(ok, key=option_order)[0]
    rc = best.get("road_class") or {}
    fewest = min(o.get("skipped_n", 0) for o in ok)
    br = best.get("bus_road") or {}
    reasons = [f"Rule 1 (safety): {best['name']} \u2014 {br['text']}" if br.get("text") else (f"Rule 1 (safety): {best['name']} uses main roads \u2014 {rc.get('text', '')}" if rc.get("label") == "MAIN ROADS" else f"Rule 1 (safety): {best['name']} \u2014 {rc.get('text', '')}"),
               f"Rule 2: skips {best['skipped_n']} stop(s)" + (" \u2014 the fewest of the safe routes" if best.get("skipped_n", 0) == fewest else ""),
               "Rule 3: no U-turn (checked)",
               f"{best['added_km']:+.1f} km, about {best['added_min']:+.0f} min per trip ({best.get('time_src', '')})",
               f"{best['skipped_n']} stop(s) skipped" + (f", including {best['important_n']} important" if best.get("important_n") else ", none important"),
               f"Traffic on the route: {best.get('traffic', 'UNKNOWN')}"]
    if best.get("used_before"):
        reasons.append(f"Same roads used in a confirmed diversion on {best['used_before']['date']}")
    cautions = []
    if best.get("feasibility") != "HIGH":
        cautions.append("Operational verification required before buses use it.")
    if rc.get("label") == "NOT VERIFIED":
        cautions.append("Road class not fully verified.")
    if small:
        cautions.append(f"{len(small)} shorter route(s) on small roads were not recommended.")
    if wod and wod.get("kind") == "wait" and closure_min is not None:
        return {"action": "wait", "option": None, "route_if_extended": best["n"], "headline": "WAIT / REGULATE \u2014 DIVERSION READY IF THE CLOSURE EXTENDS",
                "reasons": [wod["text"]] + reasons, "cautions": cautions}
    return {"action": "divert", "option": best["n"], "route_if_extended": None, "headline": f"RECOMMENDED: {best['name']} VIA " + " \u2192 ".join(best["roads"][:4]).upper(),
            "reasons": reasons, "cautions": cautions}


# ============================================================================ 13. network plan (all services together)
def network_plan(results):
    """results: [{"service","direction","H", "rec", "option": opt|None, "buses": n}] -> groups sharing one diversion + road load warnings"""
    groups, load = {}, {}
    for r in results:
        o = r.get("option")
        if not o:
            continue
        k = o["signature"]
        g = groups.setdefault(k, {"signature": k, "roads": o["roads"], "services": [], "added_min": o["added_min"], "road_class": (o.get("road_class") or {}).get("label")})
        g["services"].append(f"{r['service']} D{r['direction']}")
        bph = 60.0 / r["H"] if r.get("H") else None
        for road in dict.fromkeys(o["roads"][1:-1] or o["roads"]):
            x = load.setdefault(road, {"road": road, "services": [], "buses_hr": 0.0, "unknown": 0, "traffic": set()})
            x["services"].append(f"{r['service']} D{r['direction']}")
            if bph:
                x["buses_hr"] += bph
            else:
                x["unknown"] += 1
            x["traffic"].add(o.get("traffic") or "UNKNOWN")
    warn = []
    for x in load.values():
        x["buses_hr"] = round(x["buses_hr"], 1)
        x["traffic"] = sorted(x["traffic"])
        heavy = "HEAVY" in x["traffic"]
        if x["buses_hr"] >= PARAMS["road_load_buses_hr"] or (heavy and len(x["services"]) >= 2):
            warn.append(f"{x['road']}: {len(x['services'])} diverted services add about {x['buses_hr']:g} buses/hour" + (" on a road already HEAVY" if heavy else "")
                        + " \u2014 consider splitting services across different diversions.")
    return {"groups": sorted(groups.values(), key=lambda g: -len(g["services"])), "road_load": sorted(load.values(), key=lambda x: -x["buses_hr"]), "warnings": warn}


def uturns(line, steps=None, P=PARAMS):
    """U-turns on a route: OSRM "uturn" manoeuvres, plus any place where the route doubles back
    along the road it came from (U-turn at a junction or roundabout, or a loop round a block that
    returns the opposite way). A bus diversion must never need one. -> [(lat, lon)] (empty = none)."""
    out = []
    for st in steps or []:
        if st.get("mod") == "uturn" and st.get("man") not in ("depart", "arrive") and st.get("line"):
            out.append(tuple(st["line"][0]))
    pts = densify(line, 8.0)
    if len(pts) < 3:
        return out
    c = cum_m(pts)
    hd = [bearing(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    cell = P["uturn_near_m"]
    grid, prev = {}, False
    for i in range(len(hd)):
        if dist_m(pts[i], pts[i + 1]) < 0.5:
            continue
        x, y = xy(pts[i])
        gx, gy = int(x // cell), int(y // cell)
        hit = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if c[i] - c[j] >= P["uturn_min_m"] and dist_m(pts[i], pts[j]) <= P["uturn_near_m"] \
                            and angdiff(hd[i], hd[j]) >= P["uturn_deg"]:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        if hit and not prev and not any(dist_m(pts[i], q) < 60 for q in out):   # one entry per doubling-back
            out.append(tuple(pts[i]))
        prev = hit
        grid.setdefault((gx, gy), []).append(i)
    return out



# ------------------------------------------------------------------------------------------------ LTA rule 1: bus roads
class BusNet:
    """Where bus services already run, and in which direction. Built from each service's real-road route line
    (busrouter.sg fitted to LTA stops) and, for services without one, the chord between consecutive LTA stops."""

    def __init__(self, lines, chords=(), P=PARAMS, cell=50.0):
        self.cell, self.grid, self.P = cell, {}, P
        self.n_lines, self.n_chords = len(lines), len({c[0] for c in chords})
        for sd, ln in lines.items():
            self._add(sd, densify(ln, 20.0), P["busroad_tol_m"])
        for sd, a, b in chords:
            self._add(sd, densify([a, b], 20.0), P["busroad_stop_tol_m"])

    def _add(self, sd, pts, tol):
        for i in range(len(pts) - 1):
            x, y = xy(pts[i])
            self.grid.setdefault((int(x // self.cell), int(y // self.cell)), []).append((sd, bearing(pts[i], pts[i + 1]), x, y, tol))

    def at(self, p, brg):
        x, y = xy(p)
        gx, gy = int(x // self.cell), int(y // self.cell)
        out = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for sd, b, ex, ey, tol in self.grid.get((gx + dx, gy + dy), ()):
                    if sd not in out and math.hypot(x - ex, y - ey) <= tol and angdiff(b, brg) <= self.P["busroad_ang"]:
                        out.add(sd)
        return out


def bus_road_check(seg, groups, net, svc_class, bus, P=PARAMS):
    """Rule 1 (safety): every part of the diversion must be a road that bus services already use in the same direction
    (a road with their bus stops / on their route). For a double-decker, the services there must run double-deckers.
    svc_class(service) -> "DD" | "SD" | None (unknown). -> dict"""
    pts = densify(seg, 25.0)
    if len(pts) < 2:
        return {"ok": True, "dd": "N/A", "coverage": 1.0, "services": [], "gaps": [], "sd_only": [], "unknown": [], "text": "Too short to check."}
    c = cum_m(pts)
    total = c[-1]
    edge = min(P["busroad_edge_m"], total / 4)
    # junction corners (heading changes by > 35 deg within 30 m): neutral, they join one bus road to the next
    turns = []
    for i in range(1, len(pts) - 1):
        j0 = max(0, i - 1)
        j1 = min(len(pts) - 1, i + 1)
        while j0 > 0 and c[i] - c[j0] < 30:
            j0 -= 1
        while j1 < len(pts) - 1 and c[j1] - c[i] < 30:
            j1 += 1
        if angdiff(bearing(pts[j0], pts[i]), bearing(pts[i], pts[j1])) > 35:
            turns.append(c[i])
    samples = []
    for i in range(len(pts) - 1):
        if c[i] < edge or c[i] > total - edge:
            continue
        if any(abs(c[i] - t) <= P["busroad_turn_m"] for t in turns):
            continue
        sds = net.at(pts[i], bearing(pts[i], pts[i + 1]))
        svcs = sorted({s_ for s_, d_ in sds})
        cls = [svc_class(x) for x in svcs]
        dd = "DD" if "DD" in cls else ("SD" if cls and all(k == "SD" for k in cls) else ("UNKNOWN" if svcs else None))
        samples.append((c[i], tuple(pts[i]), svcs, dd))
    step = 25.0

    def road_at(p):
        best, bd = "", 1e9
        for g in groups or []:
            gl = g.get("line") or []
            if len(gl) >= 2:
                d = nearest_on_line(p, gl)[0]
                if d < bd:
                    best, bd = g.get("road") or "", d
        return best or "unnamed road"

    def stretches(pred):
        out, cur = [], None
        for s_, p_, sv, dd in samples:
            if pred(sv, dd):
                if cur and s_ - cur[1] <= step * 1.5:
                    cur[1] = s_
                    cur[3].append(p_)
                else:
                    if cur:
                        out.append(cur)
                    cur = [s_, s_, None, [p_]]
            elif cur and s_ - cur[1] > step * 1.5:
                out.append(cur)
                cur = None
        if cur:
            out.append(cur)
        res = []
        for a_, b_, _, pp in out:
            ln = b_ - a_ + step
            if ln >= P["busroad_gap_m"]:
                mid = pp[len(pp) // 2]
                res.append({"road": road_at(mid), "m": round(ln), "lat": mid[0], "lon": mid[1]})
        return res

    gaps = stretches(lambda sv, dd: not sv)
    sd_only = stretches(lambda sv, dd: dd == "SD")
    unknown = stretches(lambda sv, dd: dd == "UNKNOWN")
    counts = {}
    for _, _, sv, _ in samples:
        for x in sv:
            counts[x] = counts.get(x, 0) + 1
    services = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:10]
    dd_svcs = [x for x in services if svc_class(x) == "DD"]
    cov = (sum(1 for _, _, sv, _ in samples if sv) / len(samples)) if samples else 1.0
    # bus network partly unknown (service route lines not loaded, only stop-to-stop chords): a gap is "verify", not a fail
    partial = getattr(net, "n_chords", 0) > 0.2 * max(1, getattr(net, "n_lines", 0) + getattr(net, "n_chords", 0))
    unverified_gaps = gaps if partial else []
    if partial:
        unknown = unknown + gaps
        gaps = []
    ok = not gaps
    if bus != "dd":
        dd = "N/A"
    else:
        dd = "SD_ONLY" if sd_only else ("UNCONFIRMED" if unknown else "PROVEN")
    if not ok:
        text = "Not a bus road: " + "; ".join(f"{g['road']} ({g['m']} m) has no bus service in this direction" for g in gaps[:3])
    elif unverified_gaps:
        text = ("Bus road check incomplete \u2014 bus route lines not loaded for some services; no service confirmed on "
                + ", ".join(f"{g['road']} ({g['m']} m)" for g in unverified_gaps[:3]) + " \u2014 verify")
        if dd == "PROVEN":
            dd = "UNCONFIRMED" if bus == "dd" else dd
    elif dd == "SD_ONLY":
        text = "Bus road, but only single-deck services run on " + ", ".join(f"{g['road']} ({g['m']} m)" for g in sd_only[:3]) + " \u2014 not permitted for a double-decker"
    elif dd == "UNCONFIRMED":
        text = "Bus road (services " + ", ".join(services[:6]) + "); double-deck operation not yet confirmed on " + ", ".join(f"{g['road']}" for g in unknown[:3]) + " \u2014 verify"
    elif dd == "PROVEN":
        text = "Bus road all the way; double-deckers already run here (services " + ", ".join(dd_svcs[:6]) + ")"
    else:
        text = "Bus road all the way (services " + ", ".join(services[:6]) + ")"
    return {"ok": ok, "dd": dd, "coverage": round(cov, 3), "verified": not unverified_gaps, "services": services, "dd_services": dd_svcs,
            "gaps": gaps, "sd_only": sd_only, "unknown": unknown, "text": text}



# ------------------------------------------------------------------------------------------------ correct bus route
def check_line(line, stops, P=PARAMS):
    """Check a service's road line against LTA BusRoutes: every stop must lie on it, in order, and every stop-to-stop
    length must match LTA's official distance. -> {"cum", "pos", "lat", "segs": [{i, a, b, L, D, ok, why}]}"""
    cum = cum_m(line)
    pos, lat, prev = [], [], 0.0
    for s_ in stops:
        d, p_ = project_window((s_["lat"], s_["lon"]), line, cum, prev - 5.0, 1e18)
        if p_ is None:
            d, p_ = 1e9, prev
        p_ = max(p_, prev)
        pos.append(p_)
        lat.append(d)
        prev = p_
    segs = []
    for i in range(len(stops) - 1):
        L = pos[i + 1] - pos[i]
        da, db = stops[i].get("dist"), stops[i + 1].get("dist")
        D = (db - da) * 1000.0 if (da is not None and db is not None and db > da) else None
        why = []
        if lat[i] > P["line_stop_tol_m"] or lat[i + 1] > P["line_stop_tol_m"]:
            why.append("stop off the line")
        if D is not None and abs(L - D) > max(P["line_len_abs_m"], P["line_len_rel"] * D):
            why.append(f"{L:.0f} m on the line vs {D:.0f} m by LTA")
        if L < 1.0 and (D or 0) > 30:
            why.append("stops out of order")
        segs.append({"i": i, "a": pos[i], "b": pos[i + 1], "L": L, "D": D, "ok": not why, "why": "; ".join(why)})
    return {"cum": cum, "pos": pos, "lat": lat, "segs": segs}


def assemble_line(base, chk, stops, fixed, P=PARAMS):
    """rebuild the line: the original where it matches LTA, re-routed stop-to-stop pieces where it did not"""
    cum, pos, lat = chk["cum"], chk["pos"], chk["lat"]
    out = list(cut(base, cum, 0.0, pos[0])) if pos and pos[0] > 1 else []

    def add(pts):
        for p_ in pts:
            p_ = (p_[0], p_[1])
            if not out or dist_m(out[-1], p_) > 0.5:
                out.append(p_)
    for g in chk["segs"]:
        f = fixed.get(g["i"])
        if f and f.get("line"):
            add(f["line"])
        elif g["b"] > g["a"]:
            add(cut(base, cum, g["a"], g["b"]))
        else:
            add([(stops[g["i"]]["lat"], stops[g["i"]]["lon"]), (stops[g["i"] + 1]["lat"], stops[g["i"] + 1]["lon"])])
    if pos and pos[-1] < cum[-1] - 1:
        add(cut(base, cum, pos[-1], cum[-1]))
    return out
