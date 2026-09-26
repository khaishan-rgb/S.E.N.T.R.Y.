"""
SG Transport Pulse - Halfway Planner engine (V14.0).

Answers: "Where should a bus enter / re-enter the service route to achieve the best projected EWT?"

Two modes on one framework:
  late  - RECOVER LATE DUTY: one live bus gets a SIMULATED delay; options are No action, Regulate (hold the bus(es) ahead)
          and halfway re-entry at candidate stops further along the route.
  os    - DEPLOY OS BUS: an ADDITIONAL virtual bus is available at a place and time; options are No deployment and
          insertion at candidate stops (optionally on top of a simulated delay, to create a gap to fill).

Everything here is simulation. Live DataMall observations are inputs only and are never modified.

Time unit: minutes of the day (float). Distance: km along the service route.

Projected headways are calculated at MONITORING POINTS (stops downstream of the disruption). At each point the arrival sequence is:
    the most recent bus that has already passed the point (estimated passage time in the past)
  + every bus that has not yet passed it (predicted arrival).
EWT at a point = sum(h^2) / (2 sum h) - H/2  (H = scheduled headway), the same formula as the Timetable Optimiser.
Projected EWT of an option = mean over the monitoring points. The recommendation is the option with the lowest projected EWT,
subject to a minimum improvement - never a hard-coded delay rule.
"""
import math

MODEL_VERSION = "hplan-14.2-os-regulation"

PARAMS = {
    "prep_min": 2.0,            # preparation at the entry stop before taking passengers (same as the off-service planner)
    "max_wait_min": 10.0,       # a halfway / OS bus may wait at the entry stop up to this long to centre itself in the gap
    "os_max_wait_min": 20.0,    # an OS bus may delay its departure up to this long for the same reason
    "reg_hold_max": 8.0,        # regulation: hold of the bus ahead, at most (same cap as the Timetable Optimiser)
    "max_reach_min": 45.0,      # candidate stops the bus cannot reach within this many minutes are not feasible
    "min_remaining_pct": 20.0,  # at least this % of the route must remain after the entry stop
    "n_points": 12,             # monitoring points
    "top_n": 5,                 # candidates numbered on the map
    "ewt_gain_min": 0.10,       # an action is recommended only if it lowers the projected EWT by at least this many min ...
    "ewt_gain_pct": 5.0,        # ... and by at least this % of the No-action EWT
    "offsvc_kmh": 25.0,         # off-service speed used only when road routing is unavailable (estimate)
    "detour": 1.35,             # straight-line to road distance factor for that estimate
    "bus_time_factor": 1.25,    # car routing time -> bus off-service time (same as the off-service planner)
    # ---- V14.2 downstream regulation after the OS / halfway bus enters
    "balance_trips": 6,         # buses behind the inserted bus that may be regulated (the "Balance Trips")
    "reg_bus_max": 8.0,         # regulation per bus, at most (min) - same cap as the Timetable Optimiser's hold
    "reg_per_stop": 1.0,        # regulation is applied progressively: at most this many extra min per stop (slower running / longer dwell)
    "reg_cost": 0.01,           # EWT-min charged per min of regulation, so a bus is only slowed when it really helps
    "reg_maxgap_w": 0.05,       # EWT-min charged per min the largest headway grows because of regulation (never open a new gap)
    "reg_step": 0.5,            # regulation is searched in steps of this many min
    "reg_min": 1.0,             # smaller regulations are not worth instructing a Bus Captain
    "os_min_skip_pct": 15.0,    # OS: the entry stop must skip at least this % of the route - entering at stop 2 is just a full trip
}


def hav_km(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def hhmm(m):
    if m is None:
        return None
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _r(x, n=1):
    return None if x is None else round(x, n)


def ewt_of(gaps, H):
    s = sum(gaps)
    return (sum(g * g for g in gaps) / (2 * s) - H / 2.0) if s > 0 and gaps else None


# --------------------------------------------------------------------------------------------- building blocks
def base_arrivals(buses, ss, tt, now):
    """per bus: {stop index: predicted arrival} for the stops it has not reached yet, and {stop index: estimated past passage}
    for the stops it has already passed (used only for the most recent passer of each point)."""
    fut, past = {}, {}
    for b in buses:
        s0, off, t0 = b["s"], b.get("offset", 0.0), b.get("t0", now)
        f, p = {}, {}
        for k, sk in enumerate(ss):
            if sk > s0 + 1e-6:
                f[k] = t0 + max(0.0, tt(s0, sk) + off)
            elif not b.get("virtual"):
                p[k] = now - tt(sk, s0)
        fut[b["id"]], past[b["id"]] = f, p
    return fut, past


def monitoring_points(ss, s_from, n_points, n_stops):
    """up to n_points stops downstream of km s_from, spread evenly, never the first stop."""
    ks = [k for k in range(1, n_stops) if ss[k] > s_from + 1e-6]
    if len(ks) <= n_points:
        return ks
    step = (len(ks) - 1) / (n_points - 1)
    return sorted({ks[round(i * step)] for i in range(n_points)})


_PASS = {}


def seq_at(k, buses, fut, past, over):
    """arrival sequence at point k: [(t, bus id)], the latest past passer first. `over` = {bus id: {k: t or None}} replaces a bus's
    future arrival (None = does not serve k); an override for a bus with no live position (the OS bus) adds it."""
    key = (id(past), k)
    if key not in _PASS:
        passers = [(past[bid][k], bid) for bid in past if k in past[bid]]
        _PASS[key] = max(passers, key=lambda x: x[0]) if passers else None
    pz = _PASS[key]
    out = [pz] if pz else []
    for b in buses:
        bid = b["id"]
        if k not in fut[bid]:
            continue                                   # already past this point
        t = over[bid][k] if (bid in over and k in over[bid]) else fut[bid][k]
        if t is not None:
            out.append((t, bid))
    for bid, m in over.items():
        if bid not in fut and m.get(k) is not None:
            out.append((m[k], bid))
    out.sort(key=lambda x: x[0])
    return out


def evaluate(points, buses, fut, past, over, H, base_valid=None):
    """-> (projected EWT, max headway, per-point {k: (ewt, gaps, seq)})"""
    per, vals, mx = {}, [], 0.0
    for k in points:
        sq = seq_at(k, buses, fut, past, over)
        gaps = [b[0] - a[0] for a, b in zip(sq, sq[1:])]
        if base_valid is not None and k not in base_valid:
            continue
        if base_valid is None and len(gaps) < 2:
            continue
        if not gaps:
            continue
        e = ewt_of(gaps, H)
        per[k] = (e, gaps, sq)
        vals.append(e)
        mx = max(mx, max(gaps))
    return (sum(vals) / len(vals) if vals else None), mx, per


def _gap_around(k, t, buses, fut, past, over, self_id):
    """at stop k: the arrivals just before and after time t, ignoring bus self_id -> (prev (t, id), next (t, id))."""
    sq = [x for x in seq_at(k, buses, fut, past, over) if x[1] != self_id]
    prev = max((x for x in sq if x[0] <= t), default=None)
    nxt = min((x for x in sq if x[0] > t), default=None)
    return prev, nxt


# --------------------------------------------------------------------------------------------- the simulation
def simulate(ctx):
    """ctx:
         stops  [{code, name, lat, lon}], ss [km], tt(a_km, b_km) -> min, H (scheduled headway, min), H_src, now (min of day)
         buses  [{id, s, lat, lon, offset}]  live DataMall buses projected on the route (front = largest s)
         mode   "late" | "os"
         late   {bus: id, delay: min} or None   simulated delay: the bus is HELD at its current position for `delay` min (optional in os mode)
         os     {t0: min, lat, lon, label}                                (os mode)
         cands  [{j, code, name, lat, lon, approved, reach_min, reach_km, reach_src}]  travel from the origin (bus or OS)
         P      parameters (PARAMS defaults)
    """
    P = {**PARAMS, **(ctx.get("P") or {})}
    _PASS.clear()
    stops, ss, tt, now, buses = ctx["stops"], ctx["ss"], ctx["tt"], ctx["now"], ctx["buses"]
    n = len(stops)
    mode, late, os_ = ctx["mode"], ctx.get("late"), ctx.get("os")
    buses = list(buses)
    live_ids = [b["id"] for b in buses]
    byid = {b["id"]: b for b in buses}
    notes = []

    # ---- scheduled headway (or the live spacing when LTA publishes none)
    H, H_src = ctx.get("H"), ctx.get("H_src") or "LTA scheduled frequency"
    if not H:
        mid = n // 2
        g = [b[0] - a[0] for a, b in zip(seq_at(mid, buses, fut, past, {}), seq_at(mid, buses, fut, past, {})[1:])]
        g = sorted(x for x in g if x > 0.5)
        H = g[len(g) // 2] if g else 10.0
        H_src = "no LTA frequency: median of the live headways" if g else "no LTA frequency: 10 min assumed"
    H = float(H)

    # ---- the next departures from the first stop (DERIVED: scheduled headway after the last live departure, not live data).
    # Without them the last live bus would have nothing behind it, and any extra bus at the tail would look like an improvement.
    if buses and ss:
        rear = min(buses, key=lambda b: b["s"])
        last_dep = now - tt(ss[0], rear["s"])
        t1 = max(now + 1.0, last_dep + H)
        for i in range(2):
            buses.append({"id": f"S{i + 1}", "s": ss[0] - 1e-6, "t0": t1 + i * H, "virtual": True, "lat": None, "lon": None})
        notes.append(f"Next departures from {stops[0]['name']} assumed at the scheduled headway: {hhmm(t1)}, {hhmm(t1 + H)} (derived, not live).")
    fut, past = base_arrivals(buses, ss, tt, now)
    byid = {b["id"]: b for b in buses}

    # ---- the simulated delay (never applied to live data; only to this scenario's predictions)
    L, D, where = None, 0.0, "here"
    if late and late.get("bus") in live_ids and (late.get("delay") or 0) != 0:
        L, D = late["bus"], float(late["delay"])
    elif late and late.get("bus") is not None and late.get("bus") not in live_ids:
        notes.append("The selected bus is no longer in the live snapshot - refresh the live buses.")
    base_over = {}
    if L is not None:
        base_over[L] = {k: t + D for k, t in fut[L].items()}

    # ---- monitoring points: downstream of the disruption (late) / of the first stop (os)
    s_from = byid[L]["s"] if (mode == "late" and L is not None) else (min(ss[1:2] or [0.0]) - 1e-3)
    if mode == "late" and L is None:
        return {"ok": False, "error": "Select a live bus and inject a delay to simulate a late duty."}
    points = monitoring_points(ss, s_from, int(P["n_points"]), n)
    if len(points) < 2:
        return {"ok": False, "error": "Too few stops remain after this bus to assess headways."}

    e0, mx0, per0 = evaluate(points, buses, fut, past, base_over, H)
    if e0 is None:
        return {"ok": False, "error": "Not enough live buses on this direction to calculate headways (at least 2 needed)."}
    valid = set(per0)

    def pack(name, kind, e, mx, per, over, extra=None):
        return {"key": name, "kind": kind, "ewt": _r(e, 2), "max_hw": _r(mx), "gain": _r(e0 - e, 2) if e is not None else None,
                "over": over, "per": per, **(extra or {})}

    options = [pack("none", "none", e0, mx0, per0, base_over, {"label": "No action" if mode == "late" else "No OS deployment"})]

    # ---- REGULATE (late mode): hold the bus ahead of the gap, and the one ahead of it by half, at their current positions
    if mode == "late":
        ahead = sorted([b for b in buses if not b.get("virtual") and b["s"] > byid[L]["s"] + 1e-6], key=lambda b: b["s"])
        best = None
        if ahead:
            lead, lead2 = ahead[0], (ahead[1] if len(ahead) > 1 else None)
            cap = min(float(P["reg_hold_max"]), max(0.0, D))
            x = 1.0
            while x <= cap + 1e-6:
                ov = dict(base_over)
                ov[lead["id"]] = {k: t + x for k, t in fut[lead["id"]].items()}
                if lead2:
                    ov[lead2["id"]] = {k: t + x / 2.0 for k, t in fut[lead2["id"]].items()}
                e, mx, per = evaluate(points, buses, fut, past, ov, H, valid)
                if e is not None and (best is None or e < best[0] - 1e-9):
                    best = (e, mx, per, ov, x)
                x += 1.0
            if best:
                e, mx, per, ov, x = best
                held = [f"Bus {lead['id']} +{x:g} min"] + ([f"Bus {lead2['id']} +{x / 2:g} min"] if lead2 else [])
                options.append(pack("regulate", "regulate", e, mx, per, ov, {"label": "Regulate", "hold": x, "held": held,
                                                                                "hold_text": "Hold " + " and ".join(held) + " at their next stop"}))
        if not any(o["kind"] == "regulate" for o in options):
            options.append({"key": "regulate", "kind": "regulate", "label": "Regulate", "ewt": None, "max_hw": None, "gain": None, "over": base_over, "per": {},
                            "na": "No bus ahead of the delayed bus to regulate." if not ahead else "The delay is too small to regulate."})

    # ---- HALFWAY / OS insertion at each candidate stop
    if mode == "late":
        free = now + D                                                # held for the delay, then free to run off-service
        s_L = byid[L]["s"]
        origin_label = f"Bus {L}"
    else:
        free = os_["t0"]
        origin_label = os_.get("label") or "OS location"
    route_km = ss[-1] if ss else 0.0
    tested, skipped = [], {"behind": 0, "reach": 0, "remaining": 0, "later": 0, "near_start": 0}
    live_ids = set(live_ids)
    reg_cap, reg_rate, reg_cost, n_bal = float(P["reg_bus_max"]), max(0.25, float(P["reg_per_stop"])), float(P["reg_cost"]), int(P["balance_trips"])

    def slowed(times, x):
        """progressive regulation: +x min spread over the next ceil(x / reg_rate) stops (slower running / longer dwell), then held."""
        if x <= 0:
            return times
        ks = sorted(k for k, t in times.items() if t is not None)
        nst = max(1, math.ceil(x / reg_rate - 1e-9))
        out = dict(times)
        for i, k in enumerate(ks):
            out[k] = times[k] + x * min(1.0, (i + 1) / nst)
        return out

    def reg_span(bid, x, times):
        ks = sorted(k for k, t in times.items() if t is not None)
        nst = max(1, math.ceil(x / reg_rate - 1e-9))
        if not ks:
            return None
        k0, k1 = ks[0], ks[min(len(ks), nst) - 1]
        return {"stops": min(len(ks), nst), "from": stops[k0]["code"], "from_name": stops[k0]["name"], "to": stops[k1]["code"], "to_name": stops[k1]["name"]}

    for c in ctx["cands"]:
        j = c["j"]
        if mode == "late" and ss[j] <= s_L + 0.2:
            skipped["behind"] += 1
            continue
        if route_km and 100.0 * (route_km - ss[j]) / route_km < P["min_remaining_pct"] - 1e-9:
            skipped["remaining"] += 1
            continue
        if mode == "os" and route_km and 100.0 * ss[j] / route_km < P["os_min_skip_pct"] - 1e-9:
            skipped["near_start"] += 1         # an OS entering this close to the start is a full trip, not a halfway deployment
            continue
        if c.get("reach_min") is None or c["reach_min"] > P["max_reach_min"] + 1e-9:
            skipped["reach"] += 1
            continue
        earliest = free + c["reach_min"] + P["prep_min"]
        if mode == "late" and earliest >= base_over[L].get(j, 1e9) - 0.5:
            skipped["later"] += 1            # re-entering here is not earlier than simply continuing in service
            continue
        vid = L if mode == "late" else "OS"
        after = {k: tt(ss[j], ss[k]) for k in range(j, n)}
        wmax = float(P["max_wait_min"] if mode == "late" else P["os_max_wait_min"])
        waits = [float(w) for w in range(0, int(wmax) + 1)]

        def scen(w, holds, mx_ref=None):
            te = earliest + w
            ov = dict(base_over)
            ov[vid] = {k: (te + after[k] if k >= j else None) for k in range(n)}
            if mode == "late":                                       # not in service between its position and the entry stop
                for k in fut[L]:
                    if k < j:
                        ov[vid][k] = None
            for bid, x in holds.items():
                if x > 0:
                    ov[bid] = slowed(ov.get(bid, fut[bid]), x)
            e, mx, per = evaluate(points, buses, fut, past, ov, H, valid)
            if e is None:
                return None
            J = e + reg_cost * sum(holds.values()) + (float(P["reg_maxgap_w"]) * max(0.0, mx - mx_ref) if mx_ref is not None else 0.0)
            return {"J": J, "e": e, "mx": mx, "per": per, "ov": ov, "te": te, "w": w, "holds": dict(holds)}

        # stage 1 - insert only: the best entry time
        only = None
        for w in waits:
            r = scen(w, {})
            if r and (only is None or r["J"] < only["J"] - 1e-9):
                only = r
        if not only:
            continue
        # stage 2 - regulate the buses behind it (Balance Trips), jointly with the entry time (coordinate descent)
        sq_j = seq_at(j, buses, fut, past, only["ov"])
        fol = [(t, bid) for t, bid in sq_j if t > only["te"] + 1e-6 and bid != vid and bid in live_ids][:n_bal]
        followers = [bid for t, bid in fol]
        mref = only["mx"]
        step = max(0.25, float(P["reg_step"]))
        rnd = lambda x: round(x / step) * step
        cur = scen(only["w"], {}, mref)
        # seed: forward-headway regulation - each follower is slowed just enough to run h* behind the bus ahead of it
        hs = H * 0.5
        while hs <= H * 1.3 + 1e-6 and fol:
            holds, prev_t = {}, only["te"]
            for t, bid in fol:
                x = rnd(min(reg_cap, max(0.0, prev_t + hs - t)))
                x = x if x >= float(P["reg_min"]) - 1e-9 else 0.0
                if x > 0:
                    holds[bid] = x
                prev_t = t + x
            r = scen(only["w"], holds, mref)
            if r and r["J"] < cur["J"] - 1e-9:
                cur = r
            hs += 0.5
        grid = [0.0] + [x for x in (i * step for i in range(0, int(reg_cap / step) + 1)) if x >= float(P["reg_min"]) - 1e-9]
        for _ in range(4):
            improved = False
            for w in waits:
                if abs(w - cur["w"]) > 3 or w == cur["w"]:
                    continue
                r = scen(w, cur["holds"], mref)
                if r and r["J"] < cur["J"] - 1e-9:
                    cur, improved = r, True
            for bid in followers:
                for x in grid:
                    if abs(x - cur["holds"].get(bid, 0.0)) < 1e-9:
                        continue
                    h2 = dict(cur["holds"]); h2[bid] = x
                    r = scen(cur["w"], {b_: v for b_, v in h2.items() if v > 0}, mref)
                    if r and r["J"] < cur["J"] - 1e-9:
                        cur, improved = r, True
            if not improved:
                break
        for bid in sorted(cur["holds"], key=lambda b_: cur["holds"][b_]):
            h2 = {b_: v for b_, v in cur["holds"].items() if b_ != bid}
            r = scen(cur["w"], h2, mref)
            if r and r["e"] <= cur["e"] + 0.01:
                cur = r
        plan = cur
        te, w = plan["te"], plan["w"]
        prev, nxt = _gap_around(j, te, buses, fut, past, plan["ov"], vid)
        prev0, nxt0 = _gap_around(j, only["te"], buses, fut, past, only["ov"], vid)
        gap_before = (nxt0[0] - prev0[0]) if prev0 and nxt0 else None
        hw_after = [_r(te - prev[0]) if prev else None, _r(nxt[0] - te) if nxt else None]
        skipped_stops = sum(1 for k in fut[L] if k < j) if mode == "late" else j
        km_skip = (ss[j] - byid[L]["s"]) if mode == "late" else ss[j]
        leave = te - P["prep_min"] - c["reach_min"]
        regs = []
        for bid, x in sorted(plan["holds"].items(), key=lambda kv: fut[kv[0]].get(j, 1e9)):
            if x > 0:
                regs.append({"bus": bid, "hold": _r(x), **(reg_span(bid, x, base_over.get(bid, fut[bid])) or {})})
        o = pack(f"cand:{c['code']}", "halfway" if mode == "late" else "os", plan["e"], plan["mx"], plan["per"], plan["ov"], {
            "label": ("Halfway" if mode == "late" else "OS") + f" \u2014 BS {c['code']}" + (f" + {len(regs)} regulation{'s' if len(regs) != 1 else ''}" if regs else ""),
            "j": j, "code": c["code"], "name": c["name"], "lat": c["lat"], "lon": c["lon"], "vid": vid,
            "approved": bool(c.get("approved")), "reach_min": _r(c["reach_min"]), "reach_km": _r(c.get("reach_km"), 2), "reach_src": c.get("reach_src"),
            "leave": _r(leave), "leave_clock": hhmm(leave), "entry": _r(te), "entry_clock": hhmm(te), "wait": _r(w), "gap_before": _r(gap_before),
            "hw_after": hw_after, "between": [prev[1] if prev else None, nxt[1] if nxt else None], "skipped_stops": skipped_stops,
            "km_skipped": _r(max(0.0, km_skip), 2), "pct_skipped": round(100.0 * max(0.0, km_skip) / route_km) if route_km else None,
            "km_from_start": _r(ss[j], 2), "remaining_pct": round(100.0 * (route_km - ss[j]) / route_km) if route_km else None,
            "score": _r(plan["J"], 3), "only_score": _r(only["J"], 3), "regs": regs, "n_reg": len(regs), "followers": followers,
            "only": {"ewt": _r(only["e"], 2), "max_hw": _r(only["mx"]), "gain": _r(e0 - only["e"], 2), "wait": _r(only["w"]), "entry": _r(only["te"]),
                     "entry_clock": hhmm(only["te"]), "leave_clock": hhmm(only["te"] - P["prep_min"] - c["reach_min"]),
                     "hw_after": [_r(only["te"] - prev0[0]) if prev0 else None, _r(nxt0[0] - only["te"]) if nxt0 else None]}})
        o["per_only"] = only["per"]
        tested.append(o)

    tested.sort(key=lambda o: (o["score"], o["reach_min"] or 0))
    for i, o in enumerate(tested, 1):
        o["rank"] = i
    # the same deployment WITHOUT regulation, as its own option (best insert-only plan)
    only_best = min(tested, key=lambda o: (o["only_score"], o["reach_min"] or 0), default=None)
    if only_best is not None:
        ob = only_best
        options.append({"key": f"only:{ob['code']}", "kind": "only", "label": ("Halfway" if mode == "late" else "OS") + " only",
                        "ewt": ob["only"]["ewt"], "max_hw": ob["only"]["max_hw"], "gain": ob["only"]["gain"], "score": ob["only_score"],
                        "over": None, "per": ob["per_only"], "cand": ob["key"], "code": ob["code"], "name": ob["name"], "j": ob["j"], "vid": ob["vid"],
                        "entry_clock": ob["only"]["entry_clock"], "leave_clock": ob["only"]["leave_clock"], "reach_min": ob["reach_min"], "reach_km": ob["reach_km"],
                        "skipped_stops": ob["skipped_stops"], "km_skipped": ob["km_skipped"], "pct_skipped": ob["pct_skipped"], "n_reg": 0, "regs": [],
                        "hw_after": ob["only"]["hw_after"]})

    # ---- recommendation: lowest projected EWT with a meaningful improvement over No action
    pool = [o for o in options[1:] if o.get("ewt") is not None] + tested
    need = max(float(P["ewt_gain_min"]), e0 * float(P["ewt_gain_pct"]) / 100.0)
    for o in pool:
        o.setdefault("score", o["ewt"])
    best = min(pool, key=lambda o: (o["score"], o["kind"] == "only"), default=None)
    rec = best if best is not None and (e0 - best["ewt"]) >= need - 1e-9 else options[0]
    rec_reason = None if rec is not options[0] else (
        f"No option lowers the projected EWT by at least {need:.2f} min (best: {best['label']}, {best['ewt']:.2f} min)." if best else "No feasible option was found.")

    # ---- focus point for the headway timeline: where No action has its largest gap
    gk = max(per0, key=lambda k: max(per0[k][1]))                   # the largest predicted gap anywhere (map label)
    fk = gk
    if rec.get("j") is not None:                                      # timeline: where the recommended entry takes effect
        after = [k for k in per0 if k >= rec["j"]]
        if after:
            fk = max(after, key=lambda k: max(per0[k][1]))
    big = max(per0[gk][1])
    sq0 = per0[gk][2]
    gi = max(range(len(sq0) - 1), key=lambda i: sq0[i + 1][0] - sq0[i][0])
    gap = {"k": gk, "code": stops[gk]["code"], "name": stops[gk]["name"], "minutes": _r(big), "between": [sq0[gi][1], sq0[gi + 1][1]],
           "from_clock": hhmm(sq0[gi][0]), "to_clock": hhmm(sq0[gi + 1][0])}
    focus = {"k": fk, "code": stops[fk]["code"], "name": stops[fk]["name"]}

    def seq_out(o, per=None):
        per = per if per is not None else o["per"]
        k = fk if fk in per else (next(iter(per)) if per else None)
        if k is None:
            return None
        sq = per[k][2]
        hw = [_r(b[0] - a[0]) for a, b in zip(sq, sq[1:])]
        arr = [{"bus": b, "t": _r(t), "clock": hhmm(t)} for t, b in sq]
        out = {"k": k, "code": stops[k]["code"], "name": stops[k]["name"], "arr": arr, "hw": hw, "pattern": "-".join(str(int(round(h))) for h in hw)}
        vid = o.get("vid")
        if vid is not None:
            ix = next((i for i, a in enumerate(arr) if a["bus"] == vid), None)
            out["recovery_from"] = ix                   # index of the inserted bus in the arrival sequence
            out["recovery_hw"] = max(0, ix - 1) if ix is not None else None   # first headway the plan changes (the gap the bus is inserted into)
        return out

    for o in options + tested:
        o["timeline"] = seq_out(o)
    for o in tested:                                    # regulation details at the focus point, for the tap-to-explain view
        o["timeline_only"] = seq_out(o, o["per_only"])
        tl, t0l = o["timeline"], o["timeline_only"]
        if not tl:
            continue
        ix = tl.get("recovery_from")
        sect = [a["t"] for a in tl["arr"][ix:]] if ix is not None else []
        target = (sect[-1] - sect[0]) / (len(sect) - 1) if len(sect) >= 2 else None
        o["target_hw"] = _r(target)
        hw_ahead = lambda t_, bus: next(((t_["arr"][i]["t"] - t_["arr"][i - 1]["t"]) for i in range(1, len(t_["arr"])) if t_["arr"][i]["bus"] == bus), None) if t_ else None
        for r in o["regs"]:
            r["current_hw"] = _r(hw_ahead(t0l, r["bus"]))
            r["expected_hw"] = _r(hw_ahead(tl, r["bus"]))
            r["target_hw"] = o["target_hw"]
            r["at"] = tl["code"]
        for a in tl["arr"]:
            rg = next((r for r in o["regs"] if r["bus"] == a["bus"]), None)
            if rg:
                a["reg"] = rg["hold"]

    top = tested[:int(P["top_n"])]
    for o in [*options, *tested]:
        o["why"] = why(o, rec, e0, tested, gap, H, stops, mode, origin_label, D, where)

    def strip(o):
        return {k: v for k, v in o.items() if k not in ("over", "per", "per_only")}

    sim_bus = None
    if L is not None:
        sim_bus = {"bus": L, "delay": D, "where": where, "arr": {stops[k]["code"]: hhmm(t) for k, t in sorted(base_over[L].items())[:80]}}
    return {"ok": True, "model": MODEL_VERSION, "mode": mode, "H": _r(H), "H_src": H_src, "now": hhmm(now),
            "points": [{"k": k, "code": stops[k]["code"], "name": stops[k]["name"], "lat": stops[k]["lat"], "lon": stops[k]["lon"]} for k in points],
            "options": [strip(o) for o in options], "candidates": [strip(o) for o in tested], "top": [o["key"] for o in top],
            "recommended": rec["key"], "rec_reason": rec_reason, "min_gain": _r(need, 2), "gap": gap, "focus": focus, "sim_bus": sim_bus,
            "n_tested": len(tested), "skipped": skipped, "notes": notes, "origin_label": origin_label,
            "free_clock": hhmm(free), "regulation": {"balance_trips": n_bal, "per_bus_max": reg_cap, "per_stop": reg_rate}}


# --------------------------------------------------------------------------------------------- explainability
def why(o, rec, e0, tested, gap, H, stops, mode, origin_label, D, where):
    """plain-language reasons built only from the simulated numbers."""
    out = []
    if o["kind"] == "none":
        out.append(f"Projected EWT {e0:.1f} min if nothing is done; largest predicted gap {gap['minutes']:.0f} min at {gap['name']}.")
        if mode == "late":
            out.append(f"The simulated delay holds {origin_label} at its position for {D:g} min; it then continues in service.")
        return out
    if o.get("ewt") is None:
        return [o.get("na") or "Not available."]
    if o["kind"] == "regulate":
        out.append(o["hold_text"] + " (simulated).")
        out.append(f"Shares the gap with the buses ahead: projected EWT {e0:.1f} \u2192 {o['ewt']:.1f} min, largest headway {o['max_hw']:.0f} min.")
        return out
    who = "OS bus" if mode == "os" else "bus"
    if o["kind"] == "only":
        out.append(f"Inserts the {who} at BS {o['code']} with no regulation of the buses behind it.")
        out.append(f"EWT {e0:.1f} \u2192 {o['ewt']:.1f} min, largest headway {o['max_hw']:.0f} min.")
        c = next((t for t in tested if t["key"] == o.get("cand")), None)
        if c and c.get("n_reg"):
            out.append(f"Adding {c['n_reg']} regulation{'s' if c['n_reg'] != 1 else ''} behind it gives {c['ewt']:.1f} min.")
        return out
    gb, ha = o.get("gap_before"), o.get("hw_after") or [None, None]
    if gb and ha[0] is not None and ha[1] is not None:
        frac = ha[0] / gb if gb else 0.5
        pos = "near the centre of" if 0.35 <= frac <= 0.65 else "early in" if frac < 0.35 else "late in"
        out.append(f"Inserts the {who} {pos} the predicted {gb:.0f}-min gap at BS {o['code']} (between {_bl(o['between'][0])} and {_bl(o['between'][1])}).")
    if o.get("pct_skipped") is not None:
        out.append(f"Skips {o['skipped_stops']} stop(s), {o['km_skipped']:.1f} km ({o['pct_skipped']}% of the route) \u2014 a real halfway deployment, not a full trip.")
    reach = f"Reachable in about {o['reach_min']:.0f} min"
    if o.get("reach_km") is not None:
        reach += f" ({o['reach_km']:.1f} km) off-service"
    out.append(reach + (f" \u2014 {o['reach_src']}." if o.get("reach_src") else "."))
    if o.get("wait"):
        out.append(f"Enters {o['wait']:.0f} min after it could, so it lands in the gap rather than bunching.")
    for r in o.get("regs") or []:
        span = f" over {r['stops']} stop(s), {r['from_name']} \u2192 {r['to_name']}" if r.get("stops") else ""
        tgt = f", target headway \u2248{r['target_hw']:.0f} min" if r.get("target_hw") else ""
        out.append(f"Bus {r['bus']} slows down +{r['hold']:.0f} min{span}{tgt}.")
    if o.get("regs"):
        oo = o["only"]
        out.append(f"Regulating the buses behind it: EWT {oo['ewt']:.1f} (insert only) \u2192 {o['ewt']:.1f} min; largest headway {oo['max_hw']:.0f} \u2192 {o['max_hw']:.0f} min.")
    elif o.get("followers"):
        out.append("The buses behind it need no regulation: slowing them would not lower the EWT.")
    if o.get("rank") == 1:
        out.append(f"Best plan among {len(tested)} feasible entry points tested (EWT with regulation).")
    elif tested:
        out.append(f"Ranked {o['rank']} of {len(tested)}: EWT {o['ewt']:.1f} min vs {tested[0]['ewt']:.1f} min for the best plan.")
    arrow = "\u2193" if o["gain"] >= 0 else "\u2191"
    out.append(f"EWT {e0:.1f} \u2192 {o['ewt']:.1f} min ({arrow} {abs(o['gain']):.1f} min).")
    if mode == "late" and o.get("skipped_stops"):
        out.append(f"Trade-off: {o['skipped_stops']} stop(s) before BS {o['code']} are not served by this bus; the following bus picks them up.")
    return out


def _bl(b):
    return "the next departure" if isinstance(b, str) and b.startswith("S") else "the OS bus" if b == "OS" else f"Bus {b}" if b is not None else "\u2013"


# --------------------------------------------------------------------------------------------- turn-by-turn from OSRM steps
_COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


def _bearing(a, b):
    y = math.sin(math.radians(b[1] - a[1])) * math.cos(math.radians(b[0]))
    x = math.cos(math.radians(a[0])) * math.sin(math.radians(b[0])) - math.sin(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.cos(math.radians(b[1] - a[1]))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def instructions(steps, stop_code, stop_name):
    """OSRM steps [{road, km, min, man, mod, line}] -> [{icon, verb, road, text, km, lat, lon}] for the navigation view."""
    icon = {"left": "\u2190", "slight left": "\u2196", "sharp left": "\u21b0", "right": "\u2192", "slight right": "\u2197", "sharp right": "\u21b1",
            "straight": "\u2191", "uturn": "\u21b6", "": "\u2191"}
    out = []
    for i, st in enumerate(steps):
        man, mod, road = st.get("man") or "", st.get("mod") or "", st.get("road") or "unnamed road"
        pt = st["line"][0] if st.get("line") else None
        if man == "arrive":
            out.append({"icon": "\u25cf", "verb": "ARRIVE", "road": f"BS {stop_code}", "text": f"Arrive BS {stop_code} {stop_name} \u2014 enter service", "km": 0.0,
                        "lat": pt[0] if pt else None, "lon": pt[1] if pt else None})
            continue
        if man == "depart":
            ln = st.get("line") or []
            head = _COMPASS[int((_bearing(ln[0], ln[min(len(ln) - 1, 3)]) + 22.5) // 45) % 8] if len(ln) >= 2 else ""
            verb, text = "HEAD " + head.upper() if head else "START", f"Head {head} on {road}" if head else f"Start on {road}"
        elif man in ("turn", "end of road"):
            verb, text = ("TURN " + mod.upper()) if mod else "TURN", f"Turn {mod} onto {road}" if mod else f"Turn onto {road}"
        elif man in ("roundabout", "rotary"):
            verb, text = "ROUNDABOUT", f"At the roundabout, take the exit onto {road}"
        elif man in ("on ramp",):
            verb, text = "TAKE RAMP", f"Take the ramp {mod} onto {road}".replace("  ", " ")
        elif man in ("off ramp",):
            verb, text = "EXIT", f"Exit {mod} towards {road}".replace("  ", " ")
        elif man == "fork":
            verb, text = "KEEP " + (mod.replace("slight ", "").upper() or "AHEAD"), f"Keep {mod.replace('slight ', '') or 'ahead'} onto {road}"
        elif man == "merge":
            verb, text = "MERGE", f"Merge onto {road}"
        else:                                                              # new name / continue / notification
            if out and out[-1]["road"] == road:
                out[-1]["km"] += st.get("km") or 0.0
                continue
            verb, text = "CONTINUE", f"Continue on {road}"
        if out and out[-1]["road"] == road and man not in ("turn", "end of road", "roundabout", "rotary", "fork", "off ramp", "on ramp", "depart"):
            out[-1]["km"] += st.get("km") or 0.0
            continue
        out.append({"icon": icon.get(mod, "\u2191") if man not in ("roundabout", "rotary") else "\u21bb", "verb": verb, "road": road, "text": text,
                    "km": st.get("km") or 0.0, "lat": pt[0] if pt else None, "lon": pt[1] if pt else None})
    for x in out:
        x["km"] = round(x["km"], 2)
    return out


# ============================================================================================= V14.3 RECOVER LATE DUTY (cross-direction)
# A duty is late in the target direction T (e.g. D1). The late bus stays in service with its (simulated) delay - it is NOT the
# halfway bus, and nothing is held. A RECOVERY bus is taken from the circulation of BOTH directions:
#   A  "after_trip"      an opposite-direction (D2) bus finishes its D2 trip; instead of starting its next D1 trip at the first stop,
#                        it runs off-service to a D1 halfway stop and starts there.                       (D2 unaffected)
#   B  "instead_of_trip" a D1 bus ahead of the late bus reaches the D1 end; instead of its next D2 trip it runs off-service
#                        back to a D1 halfway stop.                                                       (D2 loses that trip)
#   C  "leave_now"       a D2 bus in service leaves D2 now and runs off-service to a D1 halfway stop.     (D2 loses the rest of its trip)
# Loop services (one direction): the next loop of a bus is its "next trip"; only A applies.
# Circulation forecast: a bus reaching the end of its trip starts its next trip in the other direction after the terminal layover.
# Score = projected EWT in D1 + projected EWT in D2 (both directions count, so taking a D2 trip is paid for).

PARAMS.update({
    "layover_min": 3.0,          # minimum terminal layover before the next trip / before a deployment (Bus Captain rest)
    "avail_horizon_min": 90.0,   # recovery buses that only become available later than this are not considered
    "instruct_min": 1.0,         # time to instruct a bus in service to leave its trip (type C)
    "late_max_wait_min": 10.0,   # the recovery bus may enter up to this long after it could, to land mid-gap
    "o_points": 8,               # monitoring points in the opposite direction
    "max_sources": 24,           # recovery options tested at most (earliest availability first)
})


def _dirlab(d):
    return f"D{d}"


def cross_sources(ctx):
    """Circulation forecast -> the buses that could recover the late duty, with where and when each becomes available.
    ctx: now, late {bus, delay}, T / O = {dir, stops, ss, tt, buses[{id, s, lat, lon, offset, near}], H}  (O None = loop service)"""
    P = {**PARAMS, **(ctx.get("P") or {})}
    now, T, O = ctx["now"], ctx["T"], ctx.get("O")
    loop, lay = O is None, float(P["layover_min"])
    nT = len(T["ss"])
    Lnum, D = ctx["late"]["bus"], float(ctx["late"]["delay"])
    Tb = [{**b, "id": f"T{b['id']}", "num": b["id"]} for b in T["buses"]]
    futT, _ = base_arrivals(Tb, T["ss"], T["tt"], now)
    endT = {b["id"]: (futT[b["id"]].get(nT - 1, now) + (D if b["num"] == Lnum else 0.0)) for b in Tb}
    L = f"T{Lnum}"
    if L not in endT:
        return None, "The selected bus is no longer in the live snapshot - refresh the live buses."
    sL = next(b["s"] for b in Tb if b["id"] == L)
    tl, ol = _dirlab(T["dir"]), (_dirlab(O["dir"]) if O else _dirlab(T["dir"]))
    src = []
    if loop:
        for b in Tb:
            if b["id"] == L:
                continue
            src.append({"rid": f"A:{b['id']}", "type": "after_trip", "bus": b["id"], "num": b["num"], "dir_now": T["dir"], "t_end": endT[b["id"]],
                        "t_avail": endT[b["id"]] + lay, "origin_key": "T_end", "origin": (T["stops"][-1]["lat"], T["stops"][-1]["lon"]),
                        "origin_label": f"{T['stops'][-1]['name']} (end of loop)", "near": b.get("near"),
                        "text": f"{tl} Bus {b['num']} finishes its loop at {T['stops'][-1]['name']}; its next loop starts halfway instead of at the first stop."})
    else:
        nO = len(O["ss"])
        Ob = [{**b, "id": f"O{b['id']}", "num": b["id"]} for b in O["buses"]]
        futO, _ = base_arrivals(Ob, O["ss"], O["tt"], now)
        endO = {b["id"]: futO[b["id"]].get(nO - 1, now) for b in Ob}
        tend = (O["stops"][-1]["lat"], O["stops"][-1]["lon"])
        for b in Ob:                                             # A: after finishing the D2 trip
            src.append({"rid": f"A:{b['id']}", "type": "after_trip", "bus": b["id"], "num": b["num"], "dir_now": O["dir"], "t_end": endO[b["id"]],
                        "t_avail": endO[b["id"]] + lay, "origin_key": "O_end", "origin": tend, "origin_label": O["stops"][-1]["name"], "near": b.get("near"),
                        "text": f"{ol} Bus {b['num']} finishes its {ol} trip at {O['stops'][-1]['name']}; instead of starting {tl} at the first stop it starts halfway. {ol} is not affected."})
        for b in Tb:                                             # B: a D1 bus ahead of the late bus, instead of its next D2 trip
            if b["s"] <= sL + 1e-6:
                continue
            src.append({"rid": f"B:{b['id']}", "type": "instead_of_trip", "bus": b["id"], "num": b["num"], "dir_now": T["dir"], "t_end": endT[b["id"]],
                        "t_avail": endT[b["id"]] + lay, "origin_key": "T_end", "origin": (T["stops"][-1]["lat"], T["stops"][-1]["lon"]),
                        "origin_label": T["stops"][-1]["name"], "near": b.get("near"),
                        "text": f"{tl} Bus {b['num']} reaches {T['stops'][-1]['name']}; instead of its next {ol} trip it runs back off-service into {tl}. {ol} loses that trip."})
        rel = cross_release_map(ctx)                             # D1 entry stop j -> the D2 stop opposite it
        for b in Ob:                                             # C: short-turn - continue D2 service to the stop opposite the entry, then cross
            ahead = [r for r in set(rel.values()) if r in futO[b["id"]]]
            if not ahead:
                continue
            first = min(futO[b["id"]][r] for r in ahead)
            src.append({"rid": f"C:{b['id']}", "type": "short_turn", "bus": b["id"], "num": b["num"], "dir_now": O["dir"], "t_end": None,
                        "t_avail": first + float(P["instruct_min"]), "origin_key": None, "origin": (b["lat"], b["lon"]), "rel": rel,
                        "origin_label": f"{ol} Bus {b['num']}", "near": b.get("near"), "text": ""})
    src = [s for s in src if s["t_avail"] - now <= float(P["avail_horizon_min"])]
    src.sort(key=lambda s: s["t_avail"])
    return src[:int(P["max_sources"])], None


def cross_release_map(ctx):
    """for every candidate entry stop j on the target direction: the opposite-direction stop nearest to it (where a short-turn crosses)."""
    O = ctx.get("O")
    if not O:
        return {}
    ost = O["stops"]
    out = {}
    for c in ctx["cands"]:
        r = min(range(1, len(ost) - 1), key=lambda i: hav_km((c["lat"], c["lon"]), (ost[i]["lat"], ost[i]["lon"])), default=None)
        if r is not None and hav_km((c["lat"], c["lon"]), (ost[r]["lat"], ost[r]["lon"])) <= 1.0:
            out[c["j"]] = r
    return out


def cross_origins(ctx, sources):
    """every place a recovery bus starts its off-service run from: {origin_key: (lat, lon)} - the terminals and the short-turn stops."""
    out = {}
    for s in sources:
        if s["type"] == "short_turn":
            for j, r in s["rel"].items():
                st = ctx["O"]["stops"][r]
                out[f"Ostop:{r}"] = (st["lat"], st["lon"])
        else:
            out[s["origin_key"]] = s["origin"]
    return out


def simulate_cross(ctx, reach):
    """reach: {origin_key: {stop index j: (minutes, km, source text)}} off-service travel from each origin to each target stop."""
    P = {**PARAMS, **(ctx.get("P") or {})}
    _PASS.clear()
    now, T, O = ctx["now"], ctx["T"], ctx.get("O")
    loop, lay, prep = O is None, float(P["layover_min"]), float(P["prep_min"])
    stops, ss, tt, nT = T["stops"], T["ss"], T["tt"], len(T["ss"])
    Lnum, D = ctx["late"]["bus"], float(ctx["late"]["delay"])
    L = f"T{Lnum}"
    sources, err = cross_sources(ctx)
    if err:
        return {"ok": False, "error": err}
    tl, ol = _dirlab(T["dir"]), (_dirlab(O["dir"]) if O else _dirlab(T["dir"]))
    HT = float(T.get("H") or 10.0)
    # ---- circulation: current trips + the next trip of every bus in the other direction
    Tb = [{**b, "id": f"T{b['id']}", "num": b["id"]} for b in T["buses"]]
    fT0, _ = base_arrivals(Tb, ss, tt, now)
    endT = {b["id"]: fT0[b["id"]].get(nT - 1, now) + (D if b["id"] == L else 0.0) for b in Tb}
    if loop:
        nextT = [{"id": f"N{b['num']}", "src": b["id"], "s": ss[0] - 1e-6, "t0": endT[b["id"]] + lay, "virtual": True} for b in Tb]
        Ob, nextO = [], []
    else:
        nO = len(O["ss"])
        Ob = [{**b, "id": f"O{b['id']}", "num": b["id"]} for b in O["buses"]]
        fO0, _ = base_arrivals(Ob, O["ss"], O["tt"], now)
        endO = {b["id"]: fO0[b["id"]].get(nO - 1, now) for b in Ob}
        nextT = [{"id": f"N{b['num']}", "src": b["id"], "s": ss[0] - 1e-6, "t0": endO[b["id"]] + lay, "virtual": True} for b in Ob]
        nextO = [{"id": f"M{b['num']}", "src": b["id"], "s": O["ss"][0] - 1e-6, "t0": endT[b["id"]] + lay, "virtual": True} for b in Tb]
    busesT = Tb + nextT
    futT, pastT = base_arrivals(busesT, ss, tt, now)
    base_overT = {L: {k: t + D for k, t in futT[L].items()}}
    pointsT = monitoring_points(ss, -1e-3, int(P["n_points"]), nT)
    e0T, mx0T, per0T = evaluate(pointsT, busesT, futT, pastT, base_overT, HT)
    if e0T is None:
        return {"ok": False, "error": f"Not enough buses on {tl} to calculate headways."}
    validT = set(per0T)
    e0O, mx0O, validO = 0.0, 0.0, set()
    if not loop:
        busesO = Ob + nextO
        futO, pastO = base_arrivals(busesO, O["ss"], O["tt"], now)
        pointsO = monitoring_points(O["ss"], -1e-3, int(P["o_points"]), len(O["ss"]))
        e0O_, mx0O, per0O = evaluate(pointsO, busesO, futO, pastO, {}, float(O.get("H") or HT))
        if e0O_ is not None:
            e0O, validO = e0O_, set(per0O)
    route_km = ss[-1] if ss else 0.0
    src_of_next = {v["src"]: v["id"] for v in nextT}          # bus -> its next T trip id
    src_of_nextO = {v["src"]: v["id"] for v in nextO}

    o_cache = {}

    def o_effect(s, r=None):
        """EWT in the opposite direction with this recovery source (B drops a D2 trip, C drops the rest of one after stop r)."""
        if loop or not validO or s["type"] == "after_trip":
            return e0O, mx0O
        key = (s["rid"], r)
        if key not in o_cache:
            if s["type"] == "instead_of_trip":
                ov = {src_of_nextO[s["bus"]]: {k: None for k in range(len(O["ss"]))}}
            else:
                ov = {s["bus"]: {k: None for k in futO[s["bus"]] if k > r}}
            e, mx, _ = evaluate(pointsO, busesO, futO, pastO, ov, float(O.get("H") or HT), validO)
            o_cache[key] = ((e if e is not None else e0O), mx)
        return o_cache[key]

    plans, skipped = [], {"reach": 0, "near_start": 0, "remaining": 0, "later": 0, "unreachable": 0}
    waits = [float(w) for w in range(0, int(P["late_max_wait_min"]) + 1)]
    for s in sources:
        vid = src_of_next.get(s["bus"]) if s["type"] in ("after_trip", "short_turn") else "H"
        for c in ctx["cands"]:
            j = c["j"]
            r = None
            if s["type"] == "short_turn":
                r = s["rel"].get(j)
                if r is None or r not in futO[s["bus"]]:
                    skipped["passed"] = skipped.get("passed", 0) + 1     # this bus has already passed the crossing point
                    continue
                t_avail = futO[s["bus"]][r] + float(P["instruct_min"])
                R = reach.get(f"Ostop:{r}") or {}
                ost = O["stops"][r]
                origin = {"label": f"{ol} BS {ost['code']} {ost['name']}", "lat": ost["lat"], "lon": ost["lon"]}
                lost = sum(1 for k in futO[s["bus"]] if k > r)
                rtext = f"{ol} Bus {s['num']} stays in {ol} service to BS {ost['code']} {ost['name']} and crosses over there at {hhmm(t_avail)}; {ol} loses the rest of that trip ({lost} stops)."
            else:
                t_avail, R = s["t_avail"], reach.get(s["origin_key"]) or {}
                origin = {"label": s["origin_label"], "lat": s["origin"][0], "lon": s["origin"][1]}
                rtext = s["text"]
            eO, mxO = o_effect(s, r)
            if j < 1:
                continue
            if route_km and 100.0 * ss[j] / route_km < float(P["os_min_skip_pct"]) - 1e-9:
                skipped["near_start"] += 1
                continue
            if route_km and 100.0 * (route_km - ss[j]) / route_km < float(P["min_remaining_pct"]) - 1e-9:
                skipped["remaining"] += 1
                continue
            rr = R.get(j)
            if not rr:
                skipped["unreachable"] += 1
                continue
            rmin, rkm, rsrc = rr
            if rmin > float(P["max_reach_min"]) + 1e-9:
                skipped["reach"] += 1
                continue
            if t_avail - now > float(P["avail_horizon_min"]):
                continue
            earliest = t_avail + rmin + prep
            if vid != "H" and vid in futT and earliest >= futT[vid].get(j, 1e9) - 0.5:
                skipped["later"] += 1          # the bus's normal full trip would reach this stop sooner - no halfway benefit
                continue
            after = {k: tt(ss[j], ss[k]) for k in range(j, nT)}
            best = None
            for w in waits:
                te = earliest + w
                ov = dict(base_overT)
                ov[vid] = {k: (te + after[k] if k >= j else None) for k in range(nT)}
                e, mx, per = evaluate(pointsT, busesT, futT, pastT, ov, HT, validT)
                if e is None:
                    continue
                if best is None or e + eO < best["score"] - 1e-9:
                    best = {"score": e + eO, "e": e, "mx": mx, "per": per, "ov": ov, "te": te, "w": w}
            if not best:
                continue
            te = best["te"]
            prev, nxt = _gap_around(j, te, busesT, futT, pastT, best["ov"], vid)
            prev0, nxt0 = _gap_around(j, te, busesT, futT, pastT, base_overT, None)
            plans.append({"key": f"plan:{s['rid']}:{c['code']}", "kind": "halfway_x", "rid": s["rid"], "rtype": s["type"], "rbus": s["bus"], "rnum": s["num"],
                          "rdir": s["dir_now"], "rlabel": f"{_dirlab(s['dir_now'])} Bus {s['num']}", "rtext": rtext, "vid": vid,
                          "origin": origin, "t_end_clock": hhmm(s["t_end"]) if s["t_end"] is not None else hhmm(t_avail), "release_stop": r,
                          "avail": _r(t_avail), "avail_clock": hhmm(t_avail), "j": j, "code": c["code"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
                          "approved": bool(c.get("approved")), "reach_min": _r(rmin), "reach_km": _r(rkm, 2), "reach_src": rsrc,
                          "leave": _r(te - prep - rmin), "leave_clock": hhmm(te - prep - rmin), "entry": _r(te), "entry_clock": hhmm(te), "wait": _r(best["w"]),
                          "skipped_stops": j, "km_skipped": _r(ss[j], 2), "pct_skipped": round(100.0 * ss[j] / route_km) if route_km else None,
                          "gap_before": _r(nxt0[0] - prev0[0]) if prev0 and nxt0 else None,
                          "hw_after": [_r(te - prev[0]) if prev else None, _r(nxt[0] - te) if nxt else None], "between": [prev[1] if prev else None, nxt[1] if nxt else None],
                          "ewt": _r(best["e"], 2), "max_hw": _r(best["mx"]), "ewt_o": _r(eO, 2), "ewt_o0": _r(e0O, 2), "max_hw_o": _r(mxO),
                          "gain_t": _r(e0T - best["e"], 2), "gain_o": _r(e0O - eO, 2), "gain": _r((e0T + e0O) - best["score"], 2), "score": round(best["score"], 4),
                          "per": best["per"], "over": best["ov"]})
    plans.sort(key=lambda p: (p["score"], p["reach_min"] or 0))
    for i, p in enumerate(plans, 1):
        p["rank"] = i
    # best plan per recovery bus
    by_bus, seen = [], set()
    for p in plans:
        if p["rid"] not in seen:
            seen.add(p["rid"]); by_bus.append(p)
    need = max(float(P["ewt_gain_min"]), (e0T + e0O) * float(P["ewt_gain_pct"]) / 100.0)
    best = plans[0] if plans else None
    rec = best["key"] if best and best["gain"] >= need - 1e-9 else "none"
    none = {"key": "none", "kind": "none", "label": "No halfway", "ewt": _r(e0T, 2), "max_hw": _r(mx0T), "gain": 0.0, "ewt_o": _r(e0O, 2), "ewt_o0": _r(e0O, 2),
            "gain_t": 0.0, "gain_o": 0.0, "score": round(e0T + e0O, 4), "per": per0T, "over": base_overT}
    rec_reason = None if rec != "none" else (
        f"No halfway deployment lowers the projected EWT by at least {need:.2f} min (best: {best['rlabel']} to BS {best['code']}, {best['gain']:+.2f})." if best
        else "No recovery bus can reach a useful halfway point in time.")
    # focus point and the D1 gap
    gk = max(per0T, key=lambda k: max(per0T[k][1]))
    fk = gk
    if best and rec != "none":
        aft = [k for k in per0T if k >= best["j"]]
        if aft:
            fk = max(aft, key=lambda k: max(per0T[k][1]))
    sq0 = per0T[gk][2]
    gi = max(range(len(sq0) - 1), key=lambda i: sq0[i + 1][0] - sq0[i][0])
    gap = {"k": gk, "code": stops[gk]["code"], "name": stops[gk]["name"], "minutes": _r(max(per0T[gk][1])), "between": [sq0[gi][1], sq0[gi + 1][1]],
           "from_clock": hhmm(sq0[gi][0]), "to_clock": hhmm(sq0[gi + 1][0])}

    def seq_out(o):
        per = o["per"]
        k = fk if fk in per else (next(iter(per)) if per else None)
        if k is None:
            return None
        sq = per[k][2]
        arr = [{"bus": b, "t": _r(t), "clock": hhmm(t)} for t, b in sq]
        hw = [_r(b[0] - a[0]) for a, b in zip(sq, sq[1:])]
        out = {"k": k, "code": stops[k]["code"], "name": stops[k]["name"], "arr": arr, "hw": hw, "pattern": "-".join(str(int(round(h))) for h in hw)}
        ix = next((i for i, a in enumerate(arr) if a["bus"] == o.get("vid")), None) if o.get("vid") else None
        if ix is not None:
            out["recovery_from"], out["recovery_hw"] = ix, max(0, ix - 1)
        return out

    none["timeline"] = seq_out(none)
    for p in plans:
        p["timeline"] = seq_out(p)
        p["between_label"] = [_bus_name(b, tl, ol) for b in p["between"]]
        p["how"] = {"after_trip": f"after its {_dirlab(p['rdir'])} trip", "instead_of_trip": f"instead of its next {ol} trip",
                    "short_turn": f"short-turn from {ol}"}[p["rtype"]]
        p["chain"] = _chain(p, tl, ol, T, O)
        p["why"] = _why_cross(p, none, plans, gap, HT, tl, ol, loop)
    none["why"] = [f"{tl} Bus {Lnum} runs {D:g} min late (simulated) and stays in service.",
                   f"Projected {tl} EWT {e0T:.1f} min; largest predicted gap {gap['minutes']:.0f} min at {gap['name']}."] + \
                  ([f"{ol} EWT {e0O:.1f} min (unchanged)."] if not loop else [])
    strip = lambda o: {k: v for k, v in o.items() if k not in ("per", "over")}
    lab = lambda b: _bus_name(b, tl, ol)
    for o in [none, *plans]:
        if o.get("timeline"):
            for a in o["timeline"]["arr"]:
                a["label"] = lab(a["bus"])
    return {"ok": True, "model": MODEL_VERSION + "+cross", "mode": "late", "cross": True, "loop": loop, "H": _r(HT), "H_src": T.get("H_src"), "now": hhmm(now),
            "target_dir": T["dir"], "opp_dir": None if loop else O["dir"], "late_bus": Lnum, "delay": D,
            "options": [strip(none)], "candidates": [strip(p) for p in plans[:120]], "by_bus": [p["key"] for p in by_bus],
            "top": [p["key"] for p in plans[:int(P["top_n"])]], "recommended": rec, "rec_reason": rec_reason, "min_gain": _r(need, 2),
            "gap": {**gap, "between_label": [lab(b) for b in gap["between"]]}, "focus": {"k": fk, "code": stops[fk]["code"], "name": stops[fk]["name"]},
            "sim_bus": {"bus": Lnum, "delay": D, "where": "here"}, "n_tested": len(plans), "n_sources": len(sources), "skipped": skipped,
            "ewt0": {"t": _r(e0T, 2), "o": _r(e0O, 2)},
            "sources": [{"rid": s["rid"], "type": s["type"], "num": s["num"], "dir_now": s["dir_now"], "label": f"{_dirlab(s['dir_now'])} Bus {s['num']}",
                         "avail_clock": hhmm(s["t_avail"]), "t_end_clock": hhmm(s["t_end"]) if s["t_end"] is not None else None} for s in sources],
            "notes": [f"Circulation forecast: a bus reaching the end of its trip starts the other direction after a {lay:g}-min layover (derived, not live).",
                      "Late duty mode tests halfway deployment only - no bus is held."],
            "regulation": {"balance_trips": 0, "per_bus_max": 0, "per_stop": 0}}


def _bus_name(b, tl, ol):
    if b is None:
        return "\u2013"
    b = str(b)
    if b == "H":
        return "recovery bus"
    if b[0] == "T":
        return f"{tl} Bus {b[1:]}"
    if b[0] == "N":
        return f"next {tl} trip (Bus {b[1:]})"
    if b[0] == "O":
        return f"{ol} Bus {b[1:]}"
    if b[0] == "M":
        return f"next {ol} trip (Bus {b[1:]})"
    return b


def _chain(p, tl, ol, T, O):
    """the recovery bus's movement: in service -> terminal / crossing point -> OFF SERVICE -> halfway entry (the page adds the headings)."""
    rb, rd = f"{_dirlab(p['rdir'])} Bus {p['rnum']}", _dirlab(p["rdir"])
    out = [{"k": "svc", "text": f"{rb} on {rd}"}]
    if p["rtype"] == "after_trip":
        out.append({"k": "term", "text": f"completes {rd} at {p['origin']['label']} {p['t_end_clock']}, free {p['avail_clock']}"})
    elif p["rtype"] == "instead_of_trip":
        out.append({"k": "term", "text": f"reaches {p['origin']['label']} {p['t_end_clock']}; next {ol} trip not run"})
    else:
        out.append({"k": "term", "text": f"at {p['origin']['label']} {p['avail_clock']}"})
    out.append({"k": "off", "text": f"{p['reach_min']:.0f} min" + (f" / {p['reach_km']:.1f} km" if p.get("reach_km") is not None else "")})
    out.append({"k": "enter", "text": f"BS {p['code']} {p['name']} {p['entry_clock']}"})
    return out


def _why_cross(p, none, plans, gap, H, tl, ol, loop):
    out = [p["rtext"]]
    if p["rtype"] != "short_turn":
        out.append(f"Forecast: it reaches {p['origin']['label']} at {p['t_end_clock']} and is available at {p['avail_clock']} (after the terminal layover).")
    out.append(f"Off-service {p['reach_min']:.0f} min" + (f" / {p['reach_km']:.1f} km" if p.get("reach_km") is not None else "") +
               f" to BS {p['code']}; enters {tl} at {p['entry_clock']}" + (f" (waits {p['wait']:.0f} min to land mid-gap)." if p.get("wait") else "."))
    gb, ha = p.get("gap_before"), p.get("hw_after") or [None, None]
    if gb and ha[0] is not None and ha[1] is not None:
        frac = ha[0] / gb if gb else 0.5
        pos = "near the centre of" if 0.35 <= frac <= 0.65 else "early in" if frac < 0.35 else "late in"
        out.append(f"Lands {pos} the predicted {gb:.0f}-min gap at BS {p['code']} (between {_bus_name(p['between'][0], tl, ol)} and "
                   f"{_bus_name(p['between'][1], tl, ol)}); headways either side about {ha[0]:.1f} / {ha[1]:.1f} min.")
    out.append(f"Skips {p['skipped_stops']} stop(s), {p['km_skipped']:.1f} km ({p['pct_skipped']}% of {tl}) \u2014 a real halfway, not a full trip.")
    out.append(f"{tl} EWT {none['ewt']:.1f} \u2192 {p['ewt']:.1f} min." + ("" if loop else
               (f" {ol} EWT {p['ewt_o0']:.1f} \u2192 {p['ewt_o']:.1f} min." if abs(p["gain_o"] or 0) >= 0.005 else f" {ol} unchanged.")))
    if p.get("rank") == 1:
        out.append(f"Lowest combined EWT of {len(plans)} plans tested ({len({q['rid'] for q in plans})} recovery buses \u00d7 entry points).")
    else:
        out.append(f"Ranked {p['rank']} of {len(plans)}.")
    return out


# ============================================================================================= V14.4 RECOVER LATE DUTY (next-trip halfway)
# D1 Bus C is late. Bus C COMPLETES its D1 trip (never terminated). Its NEXT trip (D2) is recovered: at the D2 interchange Bus C
# leaves off-service, drives the real road to a D2 halfway stop and enters there as "Bus C Halfway", then runs to the D2 end.
# Buses A and B (ahead of C on D1) reach the interchange first; their next D2 departures may be put back a few minutes
# (interchange departure adjustment only - nothing is held or slowed mid-route) to protect the headway at the start of D2,
# where Bus C's trip no longer runs. Bus C Halfway always enters BEHIND A and B.
# Score = projected EWT on D2 (+ a small cost per minute of departure adjustment). Loop services: the next loop is the "next trip".

PARAMS.update({
    "dep_adj_max": 5.0,          # a departure from the interchange may be put back at most this many min
    "dep_adj_cost": 0.01,        # EWT-min charged per min of departure adjustment (only adjust when it helps)
    "behind_min": 1.0,           # Bus C Halfway enters at least this long after the bus ahead of it has passed the entry stop
    "stage2_top": 8,             # entry stops refined with departure adjustments
})


def _pos_at(times, ss, t):
    """km along the route at time t from per-stop arrival times {k: t}; None if not yet departed."""
    ks = sorted(k for k, v in times.items() if v is not None)
    if not ks or t < times[ks[0]]:
        return None
    for a, b in zip(ks, ks[1:]):
        if times[a] <= t < times[b]:
            f = (t - times[a]) / max(1e-6, times[b] - times[a])
            return ss[a] + f * (ss[b] - ss[a])
    return ss[ks[-1]]


def simulate_next_trip(ctx, reach):
    """ctx: now, late {bus, delay}, T (late direction), O (its next-trip direction, None = loop), cands = entry stops on O,
       reach = {j: (minutes, km, source)} real-road off-service travel from the O interchange (first stop of O) to each entry stop."""
    P = {**PARAMS, **(ctx.get("P") or {})}
    _PASS.clear()
    now, T, O = ctx["now"], ctx["T"], ctx.get("O")
    loop = O is None
    O = O or T
    lay, prep = float(P["layover_min"]), float(P["prep_min"])
    nT, nO, ssO, ttO = len(T["ss"]), len(O["ss"]), O["ss"], O["tt"]
    tl, ol = _dirlab(T["dir"]), _dirlab(O["dir"])
    Cnum, D = ctx["late"]["bus"], float(ctx["late"]["delay"])
    Tb = [{**b, "id": f"T{b['id']}", "num": b["id"]} for b in T["buses"]]
    C = f"T{Cnum}"
    if not any(b["id"] == C for b in Tb):
        return {"ok": False, "error": "The selected bus is no longer in the live snapshot - refresh the live buses."}
    fT, _ = base_arrivals(Tb, T["ss"], T["tt"], now)
    arr = {b["id"]: fT[b["id"]].get(nT - 1, now) + (D if b["id"] == C else 0.0) for b in Tb}   # arrival at the interchange
    sC = next(b["s"] for b in Tb if b["id"] == C)
    ahead = sorted([b for b in Tb if b["s"] > sC + 1e-6], key=lambda b: b["s"])            # nearest ahead first
    Bb = ahead[0] if ahead else None
    Ab = ahead[1] if len(ahead) > 1 else None
    # ---- next-trip direction fleet: buses already on it + the next trip of every late-direction bus
    cur = (Tb if loop else [{**b, "id": f"O{b['id']}", "num": b["id"]} for b in O["buses"]])
    nxt = [{"id": f"M{b['num']}", "src": b["id"], "num": b["num"], "s": ssO[0] - 1e-6, "t0": arr[b["id"]] + lay, "virtual": True} for b in Tb]
    buses = cur + nxt
    fut, past = base_arrivals(buses, ssO, ttO, now)
    base_over = {C: {k: t + D for k, t in fut[C].items()}} if loop else {}
    points = monitoring_points(ssO, -1e-3, int(P["n_points"]), nO)
    HO = float(O.get("H") or T.get("H") or 10.0)
    e0, mx0, per0 = evaluate(points, buses, fut, past, base_over, HO)
    if e0 is None:
        return {"ok": False, "error": f"Not enough buses to calculate {ol} headways."}
    valid = set(per0)
    MC, MB, MA = f"M{Cnum}", (f"M{Bb['num']}" if Bb else None), (f"M{Ab['num']}" if Ab else None)
    route_km = ssO[-1] if ssO else 0.0
    t_free = arr[C] + lay                                         # Bus C ready to leave the interchange off-service
    adj_max, adj_cost, behind = float(P["dep_adj_max"]), float(P["dep_adj_cost"]), float(P["behind_min"])

    def shifted(mid, x):
        return {k: t + x for k, t in fut[mid].items()} if (mid and x) else None

    def scen(j, after, earliest, xa, xb, w):
        ov = dict(base_over)
        if MB and xb:
            ov[MB] = shifted(MB, xb)
        if MA and xa:
            ov[MA] = shifted(MA, xa)
        ahead_t = [ov.get(m, fut[m]).get(j) for m in (MA, MB) if m]
        ahead_t = [t for t in ahead_t if t is not None]
        te = max([earliest] + [t + behind for t in ahead_t]) + w   # never ahead of A / B
        ov[MC] = {k: (te + after[k] if k >= j else None) for k in range(nO)}
        e, mx, per = evaluate(points, buses, fut, past, ov, HO, valid)
        if e is None:
            return None
        return {"J": e + adj_cost * (xa + xb), "e": e, "mx": mx, "per": per, "ov": ov, "te": te, "w": w, "xa": xa, "xb": xb}

    skipped = {"near_start": 0, "remaining": 0, "unreachable": 0, "reach": 0, "later": 0}
    stage1 = []
    waits = [float(w) for w in range(0, int(P["late_max_wait_min"]) + 1)]
    for c in ctx["cands"]:
        j = c["j"]
        if j < 1:
            continue
        if route_km and 100.0 * ssO[j] / route_km < float(P["os_min_skip_pct"]) - 1e-9:
            skipped["near_start"] += 1
            continue
        if route_km and 100.0 * (route_km - ssO[j]) / route_km < float(P["min_remaining_pct"]) - 1e-9:
            skipped["remaining"] += 1
            continue
        rr = reach.get(j)
        if not rr:
            skipped["unreachable"] += 1
            continue
        rmin, rkm, rsrc = rr
        if rmin > float(P["max_reach_min"]) + 1e-9:
            skipped["reach"] += 1
            continue
        earliest = t_free + rmin + prep
        if earliest >= fut[MC].get(j, 1e9) - 0.5:
            skipped["later"] += 1          # running the full trip would reach this stop sooner - no halfway benefit
            continue
        after = {k: ttO(ssO[j], ssO[k]) for k in range(j, nO)}
        best = None
        for w in waits:
            r = scen(j, after, earliest, 0.0, 0.0, w)
            if r and (best is None or r["J"] < best["J"] - 1e-9):
                best = r
        if best:
            stage1.append((best["J"], c, after, earliest, rr, best))
    stage1.sort(key=lambda x: x[0])
    # stage 2: departure adjustments of A / B at the interchange (0..adj_max, 1-min steps) for the most promising entry stops
    grid = [float(x) for x in range(0, int(adj_max) + 1)]
    plans = []
    for idx, (J1, c, after, earliest, rr, best) in enumerate(stage1):
        if idx < int(P["stage2_top"]):
            for xb in (grid if MB else [0.0]):
                for xa in (grid if MA else [0.0]):
                    if xa == 0 and xb == 0:
                        continue
                    for w in (0.0, 1.0, 2.0, 3.0, 5.0, 8.0):
                        r = scen(c["j"], after, earliest, xa, xb, w)
                        if r and r["J"] < best["J"] - 1e-9:
                            best = r
            # final clean-up: drop an adjustment that adds nothing
            for which in ("xa", "xb"):
                if best[which]:
                    xa2, xb2 = (0.0, best["xb"]) if which == "xa" else (best["xa"], 0.0)
                    r = scen(c["j"], after, earliest, xa2, xb2, best["w"])
                    if r and r["e"] <= best["e"] + 0.005:
                        best = r
        j, rmin, rkm, rsrc = c["j"], rr[0], rr[1], rr[2]
        te = best["te"]
        prev, nx = _gap_around(j, te, buses, fut, past, best["ov"], MC)
        final = []                                                  # positions on O when Bus C enters (forecast, not hard-coded)
        for role, mid in (("A", MA), ("B", MB)):
            if mid:
                km = _pos_at(best["ov"].get(mid, fut[mid]), ssO, te)
                final.append({"role": role, "bus": mid, "num": int(mid[1:]), "km": _r(km, 2) if km is not None else 0.0, "departed": km is not None})
        final.append({"role": "C", "bus": MC, "num": Cnum, "km": _r(ssO[j], 2), "departed": True})
        plans.append({"key": f"next:{c['code']}", "kind": "next_trip", "j": j, "code": c["code"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
                      "approved": bool(c.get("approved")), "reach_min": _r(rmin), "reach_km": _r(rkm, 2), "reach_src": rsrc,
                      "leave": _r(te - prep - rmin), "leave_clock": hhmm(te - prep - rmin), "entry": _r(te), "entry_clock": hhmm(te), "wait": _r(te - earliest),
                      "adj": {"A": best["xa"], "B": best["xb"]}, "skipped_stops": j, "km_skipped": _r(ssO[j], 2),
                      "pct_skipped": round(100.0 * ssO[j] / route_km) if route_km else None,
                      "hw_after": [_r(te - prev[0]) if prev else None, _r(nx[0] - te) if nx else None], "between": [prev[1] if prev else None, nx[1] if nx else None],
                      "ewt": _r(best["e"], 2), "max_hw": _r(best["mx"]), "gain": _r(e0 - best["e"], 2), "score": round(best["J"], 4),
                      "final": final, "per": best["per"], "over": best["ov"], "vid": MC})
    plans.sort(key=lambda p: (p["score"], p["reach_min"] or 0))
    for i, p in enumerate(plans, 1):
        p["rank"] = i
    need = max(float(P["ewt_gain_min"]), e0 * float(P["ewt_gain_pct"]) / 100.0)
    best = plans[0] if plans else None
    rec = best["key"] if best and best["gain"] >= need - 1e-9 else "none"
    # the next-trip direction, no halfway: Bus C runs its full next trip, late
    none = {"key": "none", "kind": "none", "label": "No halfway", "ewt": _r(e0, 2), "max_hw": _r(mx0), "gain": 0.0, "score": round(e0, 4),
            "per": per0, "over": base_over, "vid": MC}
    gk = max(per0, key=lambda k: max(per0[k][1]))
    fk = gk
    if best and rec != "none":
        aft = [k for k in per0 if k >= best["j"]]
        if aft:
            fk = max(aft, key=lambda k: max(per0[k][1]))

    def lab(b):
        b = str(b)
        if b.startswith("M"):
            n_ = int(b[1:])
            role = "C" if n_ == Cnum else "B" if Bb and n_ == Bb["num"] else "A" if Ab and n_ == Ab["num"] else ""
            return f"Bus {n_}" + (f" ({role})" if role else "") + (" Halfway" if n_ == Cnum else "")
        if b[0] in "OT":
            return f"{ol if b[0] == 'O' else tl} Bus {b[1:]}"
        return b

    def seq_out(o):
        per = o["per"]
        k = fk if fk in per else (next(iter(per)) if per else None)
        if k is None:
            return None
        sq = per[k][2]
        arr_ = [{"bus": b, "t": _r(t), "clock": hhmm(t), "label": lab(b), "role": ("C" if b == MC else "B" if b == MB else "A" if b == MA else "")} for t, b in sq]
        hw = [_r(b[0] - a[0]) for a, b in zip(sq, sq[1:])]
        out = {"k": k, "code": O["stops"][k]["code"], "name": O["stops"][k]["name"], "arr": arr_, "hw": hw, "pattern": "-".join(str(int(round(h))) for h in hw)}
        return out

    none["timeline"] = seq_out(none)
    for p in plans:
        p["timeline"] = seq_out(p)
        p["between_label"] = [lab(b) if b else "\u2013" for b in p["between"]]
        p["why"] = _why_next(p, none, plans, Cnum, D, tl, ol, O, Ab, Bb, arr, lay)
    none["why"] = [f"{tl} Bus {Cnum} completes {tl} {D:g} min late and starts its {ol} trip late from {O['stops'][0]['name']}.",
                   f"Projected {ol} EWT {e0:.1f} min; largest {ol} headway {mx0:.0f} min."]
    rec_reason = None if rec != "none" else (
        f"No halfway entry lowers the {ol} EWT by at least {need:.2f} min (best: BS {best['code']}, {best['gain']:+.2f})." if best
        else f"No {ol} halfway stop meets the rules (skip \u2265 {P['os_min_skip_pct']:.0f}% of the route, reachable, earlier than the full trip).")
    strip = lambda o: {k: v for k, v in o.items() if k not in ("per", "over")}

    def busview(b, role):
        return {"role": role, "num": b["num"], "km": _r(b["s"], 2), "arr_clock": hhmm(arr[b["id"]]), "dep_clock": hhmm(arr[b["id"]] + lay),
                "late": role == "C"}
    d1 = [busview(b, "C" if b["id"] == C else "B" if Bb and b["id"] == Bb["id"] else "A" if Ab and b["id"] == Ab["id"] else "") for b in Tb]
    return {"ok": True, "model": MODEL_VERSION + "+next-trip", "mode": "late", "next_trip": True, "loop": loop, "H": _r(HO), "H_src": O.get("H_src") or T.get("H_src"),
            "now": hhmm(now), "late_dir": T["dir"], "next_dir": O["dir"], "late_bus": Cnum, "delay": D,
            "roles": {"A": Ab["num"] if Ab else None, "B": Bb["num"] if Bb else None, "C": Cnum},
            "interchange": {"code": O["stops"][0]["code"], "name": O["stops"][0]["name"], "lat": O["stops"][0]["lat"], "lon": O["stops"][0]["lon"]},
            "c_arrival": hhmm(arr[C]), "c_ready": hhmm(t_free), "c_normal_dep": hhmm(arr[C] + lay), "layover": lay,
            "late_route_km": _r(T["ss"][-1], 2), "next_route_km": _r(route_km, 2), "d1": d1,
            "options": [strip(none)], "candidates": [strip(p) for p in plans[:80]], "top": [p["key"] for p in plans[:int(P["top_n"])]],
            "recommended": rec, "rec_reason": rec_reason, "min_gain": _r(need, 2), "n_tested": len(plans), "skipped": skipped,
            "focus": {"k": fk, "code": O["stops"][fk]["code"], "name": O["stops"][fk]["name"]},
            "gap": {"k": gk, "code": O["stops"][gk]["code"], "name": O["stops"][gk]["name"], "minutes": _r(max(per0[gk][1])), "between": [None, None]},
            "sim_bus": {"bus": Cnum, "delay": D, "where": "here"},
            "notes": [f"Bus {Cnum} completes {tl}; only its next {ol} trip starts halfway. Interchange layover {lay:g} min (assumed).",
                      "Departure adjustments are at the interchange only - no bus is held or slowed mid-route."],
            "regulation": {"balance_trips": 0, "per_bus_max": 0, "per_stop": 0}}


def _why_next(p, none, plans, Cnum, D, tl, ol, O, Ab, Bb, arr, lay):
    out = [f"Bus {Cnum} completes {tl} ({D:g} min late) and reaches {O['stops'][0]['name']} at {hhmm(arr['T' + str(Cnum)])}."]
    out.append(f"Instead of starting {ol} late at the first stop, it runs off-service {p['reach_min']:.0f} min" +
               (f" / {p['reach_km']:.1f} km" if p.get("reach_km") is not None else "") + f" by road to BS {p['code']} and enters at {p['entry_clock']}.")
    out.append(f"Skips {p['skipped_stops']} stop(s), {p['km_skipped']:.1f} km ({p['pct_skipped']}% of {ol}) \u2014 meets the minimum skip.")
    adj = [f"Bus {b['num']} ({r}) +{p['adj'][r]:.0f} min" for r, b in (("A", Ab), ("B", Bb)) if b and p["adj"].get(r)]
    if adj:
        out.append("Interchange departures put back to cover the start of " + ol + " while Bus " + str(Cnum) + " is recovered: " + ", ".join(adj) + ".")
    else:
        out.append(f"No departure adjustment needed for Bus A / B at {O['stops'][0]['name']}.")
    ha = p.get("hw_after") or [None, None]
    if ha[0] is not None:
        out.append(f"Enters behind {p['between_label'][0]}" + (f", ahead of {p['between_label'][1]}" if ha[1] is not None else "") +
                   f"; headways either side about {ha[0]:.1f}" + (f" / {ha[1]:.1f}" if ha[1] is not None else "") + " min.")
    out.append(f"{ol} EWT {none['ewt']:.1f} \u2192 {p['ewt']:.1f} min.")
    if p.get("rank") == 1:
        out.append(f"Best of {len(plans)} feasible {ol} entry points tested.")
    return out
