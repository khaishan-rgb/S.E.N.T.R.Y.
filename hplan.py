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
