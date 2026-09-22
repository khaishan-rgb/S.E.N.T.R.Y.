"""Halfway Deployment & Off-Service Route Planner (V12.9) - pure logic, no I/O.

Answers four questions for one halfway deployment:
  1. where the bus starts service (the halfway stop, chosen by recovery.py),
  2. which roads it takes off-service to get there (real road routes from OSRM, several alternatives),
  3. whether that route is operationally suitable for the selected bus type,
  4. exactly how much headway the deployment earns compared with continuing the full trip.

Suitability rules (deliberately conservative):
  * VERIFIED only when a controller has recorded this exact route (road sequence) for this stop and bus type as verified, and no blocking finding appears.
  * UNSUITABLE only when map data explicitly says so (a height / width / weight limit below the vehicle's entered dimension, or no bus / motor-vehicle access).
  * Everything else is REQUIRES REVIEW - including "no restriction found", because OpenStreetMap is not an authoritative clearance register and a routing
    engine finding a road does not prove a double-decker can use it. No bridge heights, clearances or restrictions are ever invented.
"""
import hashlib
import math
import re

PARAMS = {
    "bus_time_factor": 1.25,      # OSRM car time -> bus off-service time when no speed-band data covers the route
    "band_min_share": 0.5,        # use speed-band travel time only if it covers at least this share of the route
    "prep_min": 2.0,              # preparation at the halfway stop before entering passenger service
    "near_m": 50.0,               # road works / incidents within this distance are "on the route"
    "service_overlap_m": 30.0,    # off-service points within this distance of the service's own route count as "on the service route"
    "w_review": 3.0, "w_unsuitable": 1000.0, "w_congested_km": 1.5, "w_turn": 0.25, "w_sharp": 1.5, "w_overlap": 3.0, "w_roadworks": 2.0,
}
BUS_TYPES = {"sd": "Single deck", "dd": "Double deck", "art": "Articulated"}
STATUS_TXT = {"verified": "VERIFIED", "review": "REQUIRES REVIEW", "unsuitable": "UNSUITABLE"}


def hav_m(a, b):
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def seg_dist_m(p, a, b):
    """distance from point p to segment ab (metres, local flat approximation)."""
    kx = math.cos(math.radians(p[0])) * 111320.0
    ky = 110574.0
    ax, ay, bx, by, px, py = a[1] * kx, a[0] * ky, b[1] * kx, b[0] * ky, p[1] * kx, p[0] * ky
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_line_m(p, line):
    if not line:
        return 1e9
    if len(line) == 1:
        return hav_m(p, line[0])
    return min(seg_dist_m(p, a, b) for a, b in zip(line, line[1:]))


def line_km(line):
    return sum(hav_m(a, b) for a, b in zip(line, line[1:])) / 1000.0


def simplify(line, n=150):
    if len(line) <= n:
        return [[round(p[0], 5), round(p[1], 5)] for p in line]
    step = (len(line) - 1) / (n - 1)
    return [[round(line[round(i * step)][0], 5), round(line[round(i * step)][1], 5)] for i in range(n)]


def hm(m):
    if m is None:
        return "--:--"
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def norm_road(s):
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    for a, b in ((" AVENUE", " AVE"), (" STREET", " ST"), (" ROAD", " RD"), (" DRIVE", " DR"), (" CENTRAL", " CTRL"), (" EXPRESSWAY", " EXPWY")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


# ============================================================================ OSRM route -> road groups
def parse_osrm(j):
    """OSRM /route response -> list of routes {line, km, osrm_min, steps:[{road, km, min, man, mod, line}]}."""
    out = []
    for r in (j or {}).get("routes") or []:
        line = [(c[1], c[0]) for c in (r.get("geometry") or {}).get("coordinates") or []]
        steps = []
        for leg in r.get("legs") or []:
            for st in leg.get("steps") or []:
                g = [(c[1], c[0]) for c in (st.get("geometry") or {}).get("coordinates") or []]
                man = st.get("maneuver") or {}
                road = (st.get("name") or "").strip() or (st.get("ref") or "").strip()
                steps.append({"road": road, "ref": (st.get("ref") or "").strip(), "km": (st.get("distance") or 0) / 1000.0, "min": (st.get("duration") or 0) / 60.0,
                              "man": man.get("type") or "", "mod": man.get("modifier") or "", "line": g})
        if len(line) >= 2:
            out.append({"line": line, "km": (r.get("distance") or 0) / 1000.0 or line_km(line), "osrm_min": (r.get("duration") or 0) / 60.0, "steps": steps})
    return out


def group_roads(steps, min_km=0.08):
    """consecutive steps on the same road -> one stage; very short unnamed connectors are folded into the neighbour."""
    groups = []
    for st in steps:
        if st["man"] == "arrive" and st["km"] < 0.001:
            continue
        name = st["road"] or "(unnamed road)"
        if groups and (norm_road(groups[-1]["road"]) == norm_road(name) or (st["km"] < min_km and not st["road"])):
            g = groups[-1]
            g["km"] += st["km"]; g["min"] += st["min"]
            g["line"] += st["line"][1:] if g["line"] else st["line"]
            g["turns"] += 1 if st["man"] in ("turn", "end of road", "fork", "roundabout", "rotary", "on ramp", "off ramp") else 0
            g["sharp"] += 1 if st["mod"] in ("sharp left", "sharp right", "uturn") else 0
        else:
            groups.append({"road": name, "km": st["km"], "min": st["min"], "line": list(st["line"]), "man": st["man"], "mod": st["mod"],
                           "turns": 1 if st["man"] in ("turn", "end of road", "fork", "roundabout", "rotary", "on ramp", "off ramp") else 0,
                           "sharp": 1 if st["mod"] in ("sharp left", "sharp right", "uturn") else 0})
    # fold tiny groups (< min_km) into the previous one so the timeline stays readable
    out = []
    for g in groups:
        if out and g["km"] < min_km:
            p = out[-1]
            p["km"] += g["km"]; p["min"] += g["min"]; p["line"] += g["line"][1:]; p["turns"] += g["turns"]; p["sharp"] += g["sharp"]
        else:
            out.append(g)
    return out


def signature(stop_code, groups):
    seq = "|".join(norm_road(g["road"]) for g in groups)
    return hashlib.sha1(f"{stop_code}|{seq}".encode()).hexdigest()[:16]


# ============================================================================ restrictions (OpenStreetMap tags from Overpass)
def parse_len_m(v):
    """'4.5', '4.5 m', "14'6\\"" -> metres; anything else (none / default / below_default / unknown) -> None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(m)?$", s)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+)\s*'\s*(\d+(?:\.\d+)?)?\s*\"?$", s)
    if m:
        return int(m.group(1)) * 0.3048 + (float(m.group(2)) if m.group(2) else 0.0) * 0.0254
    return None


def parse_t(v):
    if v is None:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(t)?$", str(v).strip().lower())
    return float(m.group(1)) if m else None


def findings(route, osm, bus, dims, roadworks, incidents, P, service_line=None):
    """-> list of {sev: block|review|info, text, lat, lon}. osm: {"ok": bool, "ways": [{tags, lat, lon}]} or None."""
    out = []
    names = {norm_road(g["road"]) for g in route["groups"]} | {norm_road(s.get("ref")) for s in route.get("steps", []) if s.get("ref")}
    names.discard("")
    names.discard("UNNAMED ROAD")
    dd, art = bus == "dd", bus == "art"
    H, W, M = dims.get("height_m"), dims.get("width_m"), dims.get("weight_t")
    if not osm or not osm.get("ok"):
        out.append({"sev": "review", "text": "Road restriction data (OpenStreetMap) could not be loaded for this route - nothing about clearances or access could be checked."})
    else:
        seen = set()
        for w in osm.get("ways", []):
            t = w.get("tags") or {}
            nm = norm_road(t.get("name") or t.get("ref") or "")
            on = bool(nm) and (nm in names or norm_road(t.get("ref") or "") in names)
            key = (nm, t.get("maxheight"), t.get("bridge"), t.get("tunnel"), t.get("railway"))
            if key in seen:
                continue
            seen.add(key)
            where = t.get("name") or t.get("ref") or "an unnamed road"
            ll = {"lat": w.get("lat"), "lon": w.get("lon")}
            if on:
                for tag, lim, unit, dim in (("maxheight", parse_len_m(t.get("maxheight")), "m", H), ("maxwidth", parse_len_m(t.get("maxwidth")), "m", W), ("maxweight", parse_t(t.get("maxweight")), "t", M)):
                    raw = t.get(tag)
                    if raw is None:
                        continue
                    label = {"maxheight": "Height", "maxwidth": "Width", "maxweight": "Weight"}[tag]
                    if lim is None:
                        out.append({"sev": "review", "text": f"{label} restriction '{raw}' tagged on {where} (value not numeric) - check on site.", **ll})
                    elif dim is None:
                        out.append({"sev": "review", "text": f"{label} limit {lim:g} {unit} on {where} (OpenStreetMap). Enter the vehicle's {label.lower()} to compare.", **ll})
                    elif lim < dim - 1e-9:
                        out.append({"sev": "block", "text": f"{label} limit {lim:g} {unit} on {where} is below the entered vehicle {label.lower()} ({dim:g} {unit}).", **ll})
                    else:
                        out.append({"sev": "info", "text": f"{label} limit {lim:g} {unit} on {where}; entered vehicle {label.lower()} {dim:g} {unit}.", **ll})
                acc = {k: (t.get(k) or "").lower() for k in ("access", "motor_vehicle", "bus", "psv", "hgv")}
                bus_ok = acc["bus"] in ("yes", "designated") or acc["psv"] in ("yes", "designated")
                if not bus_ok and (acc["bus"] == "no" or acc["psv"] == "no" or acc["motor_vehicle"] in ("no",) or acc["access"] in ("no",)):
                    out.append({"sev": "block", "text": f"No bus / motor-vehicle access tagged on {where} (OpenStreetMap).", **ll})
                elif not bus_ok and (acc["access"] in ("private", "destination") or acc["motor_vehicle"] in ("private", "destination")):
                    out.append({"sev": "review", "text": f"Access '{acc['access'] or acc['motor_vehicle']}' tagged on {where} - confirm buses may use it.", **ll})
                if acc["hgv"] in ("no", "destination", "delivery"):
                    out.append({"sev": "review", "text": f"Heavy-vehicle restriction (hgv={acc['hgv']}) tagged on {where} - check whether it applies to buses.", **ll})
                if (t.get("tunnel") or "").lower() in ("yes", "building_passage") or (t.get("covered") or "").lower() == "yes":
                    if t.get("maxheight") is None:
                        out.append({"sev": "review" if (dd or art) else "info", "text": f"Tunnel / covered section on {where} with no clearance recorded.", **ll})
            else:
                overhead = (t.get("bridge") or "").lower() not in ("", "no") or (t.get("man_made") == "bridge")
                if overhead:
                    kind = "rail viaduct / bridge" if t.get("railway") else "bridge / flyover"
                    out.append({"sev": "review" if dd else "info", "text": f"Passes under or beside a {kind} ({where}); clearance is not recorded in the map data.", **ll})
    sharp = sum(g["sharp"] for g in route["groups"])
    if sharp:
        sharp_roads = [g["road"] for g in route["groups"] if g["sharp"]][:3]
        out.append({"sev": "review" if (dd or art) else "info", "text": f"{sharp} sharp turn / U-turn manoeuvre(s) (at {', '.join(sharp_roads)}) - check the turning path for this bus type."})
    for rw in roadworks or []:
        if rw.get("lat") is None:
            if norm_road(rw.get("road")) in names:
                out.append({"sev": "review", "text": f"Road works listed on {rw['road']} (LTA) - location not given; check the lane is open."})
            continue
        if dist_to_line_m((rw["lat"], rw["lon"]), route["line"]) <= P["near_m"]:
            out.append({"sev": "review", "text": f"Road works on {rw['road']} (LTA){': ' + rw['other'][:80] if rw.get('other') else ''}.", "lat": rw["lat"], "lon": rw["lon"]})
    for inc in incidents or []:
        if inc.get("lat") is None or dist_to_line_m((inc["lat"], inc["lon"]), route["line"]) > P["near_m"] * 2:
            continue
        ty = (inc.get("type") or "").lower()
        sev = "review" if any(k in ty for k in ("road block", "roadblock", "diversion", "closure", "accident")) else "info"
        out.append({"sev": sev, "text": f"LTA incident: {inc.get('type')} - {(inc.get('message') or '')[:100]}", "lat": inc["lat"], "lon": inc["lon"]})
    return out


def suitability(fs, bus, verified_rec):
    blocks = [f for f in fs if f["sev"] == "block"]
    reviews = [f for f in fs if f["sev"] == "review"]
    if blocks:
        return "unsuitable", "Map data shows a restriction this vehicle cannot pass - do not use this route."
    if verified_rec:
        return "verified", f"Verified for {BUS_TYPES[bus].lower()} by {verified_rec.get('by') or 'a controller'} on {verified_rec.get('date') or 'record'}" + (
            f"; {len(reviews)} current item(s) still to note." if reviews else ".")
    if bus == "dd":
        return "review", "Double-decker suitability not verified \u2014 operational review required."
    return "review", f"{BUS_TYPES[bus]} suitability not verified from authoritative data \u2014 operational review required."


# ============================================================================ route options
def build_options(routes, ctx, P):
    """routes: parsed OSRM routes (+ optional 'service' route) each already carrying groups, traffic, findings, status.
    ctx: leave (min), prep, planned_start (min, at the stop), slot, tau_end_minus_j.
    -> options with arrival, insertion, tags A/B/C and a score; recommended index."""
    opts = []
    for i, r in enumerate(routes):
        t = r["time_min"]
        arrive = ctx["leave"] + t
        insert = max(arrive + P["prep_min"], ctx["planned_start"]) if ctx.get("planned_start") is not None else arrive + P["prep_min"]
        cong_km = r.get("traffic", {}).get("km", {}).get("slow", 0.0)
        n_turn = sum(g["turns"] for g in r["groups"])
        n_sharp = sum(g["sharp"] for g in r["groups"])
        n_rev = sum(1 for f in r["findings"] if f["sev"] == "review")
        rw = sum(1 for f in r["findings"] if f["sev"] == "review" and "Road works" in f["text"])
        score = (t + P["w_congested_km"] * cong_km + P["w_turn"] * n_turn + P["w_sharp"] * n_sharp + P["w_review"] * min(n_rev, 4) + P["w_roadworks"] * rw
                 + (P["w_unsuitable"] if r["status"] == "unsuitable" else 0.0) - P["w_overlap"] * r.get("overlap", 0.0))
        opts.append({**r, "idx": i, "arrive": arrive, "insert": insert, "score": score, "n_turns": n_turn, "n_sharp": n_sharp, "n_review": n_rev, "congested_km": cong_km})
    ok = [o for o in opts if o["status"] != "unsuitable"] or opts
    fastest = min(ok, key=lambda o: (o["time_min"], o["km"]))
    shortest = min(ok, key=lambda o: (o["km"], o["time_min"]))
    pref = min(ok, key=lambda o: (o["score"], o["time_min"]))
    for o in opts:
        o["tags"] = [x for x, y in (("A", fastest), ("B", shortest), ("C", pref)) if o is y]
    return opts, pref["idx"], fastest["idx"], shortest["idx"]


# ============================================================================ headway maths at the halfway stop
def gaps_at(times):
    """times: [(bus, t)] sorted -> [(bus_before, bus_after, t_before, t_after, gap)]"""
    ts = sorted(times, key=lambda x: x[1])
    return [(a[0], b[0], a[1], b[1], b[1] - a[1]) for a, b in zip(ts, ts[1:])]


def headway_benefit(none_at, reg_at, hw_at, k, insert, n, focus):
    """none_at / reg_at / hw_at: [(bus, time)] at the halfway stop for No action / Regulate only / Halfway plan (from recovery.py).
    k: the halfway trip. insert: the insertion time actually achievable on the chosen road route.
    Returns Scenario A (continue full service) and B (halfway) with prev / next buses and the headway earned."""
    lo, hi = min(focus) - 1, max(focus) + 1
    win = lambda at: [(b, t) for b, t in at if lo <= b <= hi or b in (0, n + 1)]
    A = gaps_at(win(none_at))
    R = gaps_at(win(reg_at)) if reg_at else []
    hw = [(b, (insert if b == k else t)) for b, t in win(hw_at)]
    B = gaps_at(hw)
    mA = max(A, key=lambda g: g[4]) if A else None
    mR = max(R, key=lambda g: g[4]) if R else None
    mB = max(B, key=lambda g: g[4]) if B else None
    prev = next((g for g in B if g[1] == k), None)
    nxt = next((g for g in B if g[0] == k), None)
    avg = lambda G: (round(sum(g[4] for g in G) / len(G), 1)) if G else None
    rd = lambda v: None if v is None else round(v, 1)
    mA = mA and (mA[0], mA[1], mA[2], mA[3], rd(mA[4]))
    mR = mR and (mR[0], mR[1], mR[2], mR[3], rd(mR[4]))
    mB = mB and (mB[0], mB[1], mB[2], mB[3], rd(mB[4]))
    prev = prev and (prev[0], prev[1], prev[2], prev[3], rd(prev[4]))
    nxt = nxt and (nxt[0], nxt[1], nxt[2], nxt[3], rd(nxt[4]))
    return {
        "A": {"max": mA[4] if mA else None, "prev": mA[0] if mA else None, "next": mA[1] if mA else None, "t_prev": mA[2] if mA else None, "t_next": mA[3] if mA else None,
              "avg": avg(A), "gaps": [[g[0], g[1], round(g[4], 1)] for g in A]},
        "R": {"max": mR[4] if mR else None, "avg": avg(R), "gaps": [[g[0], g[1], round(g[4], 1)] for g in R]},
        "B": {"max": mB[4] if mB else None, "avg": avg(B), "gaps": [[g[0], g[1], round(g[4], 1)] for g in B],
              "prev_bus": prev[0] if prev else None, "t_prev": prev[2] if prev else None, "gap_prev": prev[4] if prev else None,
              "next_bus": nxt[1] if nxt else None, "t_next": nxt[3] if nxt else None, "gap_next": nxt[4] if nxt else None, "insert": insert},
        "earned": rd(mA[4] - mB[4]) if (mA and mB) else None,
        "earned_vs_reg": rd(mR[4] - mB[4]) if (mR and mB) else None,
    }


# ============================================================================ timeline
def timeline(opt, leave, prep, insert, end_time, first_name, stop_name, last_name):
    """stages: depart interchange -> each road -> reach halfway stop -> enter service -> final stop; road minutes scaled to the route's estimated time."""
    tot_osrm = sum(g["min"] for g in opt["groups"]) or 1.0
    scale = opt["time_min"] / tot_osrm
    st = [{"kind": "depart", "label": f"Depart {first_name} off-service", "t": leave, "clock": hm(leave)}]
    t = leave
    for gi, g in enumerate(opt["groups"]):
        dm = g["min"] * scale
        st.append({"kind": "road", "g": gi, "label": g["road"], "km": round(g["km"], 2), "min": round(dm, 1), "t": t, "clock": hm(t)})
        t += dm
    st.append({"kind": "arrive", "label": f"Reach {stop_name}", "t": t, "clock": hm(t)})
    st.append({"kind": "service", "label": "Enter passenger service", "t": insert, "clock": hm(insert), "wait": round(max(0.0, insert - t - prep), 1), "prep": prep})
    st.append({"kind": "end", "label": f"Arrive {last_name} (final stop)", "t": end_time, "clock": hm(end_time)})
    return st


# ============================================================================ explanations
def why_route(opts, rec, fastest, shortest, ben, stop_name):
    o = opts[rec]
    TW = {"A": "fastest", "B": "shortest", "C": "preferred"}
    lines = [f"{len(opts)} off-service route{'s were' if len(opts) != 1 else ' was'} evaluated: " + "; ".join(
        f"{x['label']}{' (' + ', '.join(TW[t] for t in x['tags']) + ')' if x['tags'] else ''}: {x['km']:.1f} km / {x['time_min']:.0f} min"
        + (" - unsuitable" if x["status"] == "unsuitable" else "") for x in opts) + "."]
    f, s = opts[fastest], opts[shortest]
    if o["idx"] == s["idx"] and o["idx"] == f["idx"]:
        lines.append(f"The recommended route is both the fastest and the shortest ({o['km']:.1f} km, about {o['time_min']:.0f} min).")
    elif o["idx"] == f["idx"]:
        lines.append(f"The recommended route is {o['km'] - s['km']:.1f} km longer than the shortest option but about {s['time_min'] - o['time_min']:.0f} min quicker"
                     + (" because of congestion on the shorter roads" if s.get("congested_km", 0) > o.get("congested_km", 0) + 0.2 else "") + ".")
    else:
        why = []
        if o.get("overlap", 0) > max(f.get("overlap", 0), 0) + 0.1:
            why.append(f"{o['overlap'] * 100:.0f}% of it follows the service's own road path")
        if o["n_review"] < f["n_review"]:
            why.append(f"it has fewer items needing review ({o['n_review']} vs {f['n_review']})")
        if o["n_sharp"] < f["n_sharp"]:
            why.append("it avoids sharp turns / U-turns")
        if f["status"] == "unsuitable":
            why.append("the fastest route is unsuitable for this bus type")
        dt = o["time_min"] - f["time_min"]
        lines.append((f"The recommended route takes about the same time as the fastest ({dt:+.1f} min) and is operationally simpler" if dt < 1.0 else
                      f"The recommended route takes about {dt:.0f} min longer than the fastest but is operationally simpler")
                     + (": " + "; ".join(why) if why else "") + ".")
    if ben and ben.get("A", {}).get("max") and ben.get("B", {}).get("max") is not None:
        a, b = ben["A"], ben["B"]
        lines.append(f"The bus can enter passenger service at {hm(b['insert'])}, between the bus at {hm(b['t_prev'])} and the bus at {hm(b['t_next'])} at {stop_name}."
                     if b.get("t_prev") is not None and b.get("t_next") is not None else f"The bus can enter passenger service at {hm(b['insert'])} at {stop_name}.")
        lines.append(f"The largest gap at that stop changes from {a['max']:.0f} min (no action) to {b['max']:.0f} min: about {max(0.0, ben['earned']):.0f} min of headway earned.")
    return lines


def why_works(ben, trip, first_name, stop_name, km_lost, lay):
    a, b = ben["A"], ben["B"]
    if a.get("max") is None or b.get("max") is None:
        return []
    return [
        f"Without intervention, Trip {trip}'s bus must finish its journey to {first_name} and take its {lay:.0f}-min layover before it can start again. "
        f"At {stop_name} that leaves a {a['max']:.0f}-min passenger gap (bus at {hm(a['t_prev'])}, next bus at {hm(a['t_next'])}).",
        f"By going off-service straight to {stop_name}, the bus skips the first part of the disrupted trip and re-enters service earlier, at {hm(b['insert'])}.",
        (f"It is placed between two widely separated buses: {b['gap_prev']:.0f} min after the previous bus and {b['gap_next']:.0f} min before the next one, "
         f"so the largest gap at this stop becomes {b['max']:.0f} min." if b.get("gap_prev") is not None and b.get("gap_next") is not None
         else f"The largest gap at this stop becomes {b['max']:.0f} min."),
        f"Headway benefit: {max(0.0, ben['earned']):.0f} min. Trade-off: {km_lost:.1f} km of scheduled mileage is not operated.",
    ]
