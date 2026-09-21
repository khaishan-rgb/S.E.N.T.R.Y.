"""AI Halfway Optimiser - engine V3 (pure stdlib, no I/O, unit-testable).

One trip of a scheduled sequence (timed at the first bus stop) is lost (Disrupted). The optimiser SIMULATES the same trips under several options and
scores them, so the controller sees what to do and why:

  A. No action            - the trip stays missing; departures follow the schedule (late arrivals delay their departure).
  B. Regulate headway     - no halfway: the AI HOLDS / RELEASES the trips around the gap at the first stop to even out the headways
                            (within a hold limit, an early-release limit and each trip's arrival + minimum layover).
  C. Halfway at stop X    - a replacement trip starts at approved stop X at the disrupted trip's scheduled time there (+ optional delay).
  D. Halfway at X + regulation - as C, and the AI also chooses the replacement's start time (within a window) and the holds / releases so that the
                            headways either side of it are as even as possible.

It does NOT identify a physical bus: LTA DataMall has no schedule / duty data. All trips run the same running times down the route (optionally
stretched / shrunk by `load_sens` so a big gap grows and a short one shrinks). Everything is simulated from the entered scenario.

Score (spec section 16) = regularity benefit + max-gap reduction + recovery-time benefit - holding cost - mileage loss - new-bunching penalty, with
configurable weights; "Balanced", "Faster recovery" and "Minimise mileage" re-weight it. A halfway stop that would create bunching (a headway below the
bunching limit that the baseline does not have) is rejected: the target is even spacing, not just another bus in the gap.

"Affected headways" = the consecutive headways among the trips in the window (`reg_window` trips either side of the disrupted one, plus the replacement).
Recovery time = how long the irregular headways last (from the start of the first to the end of the last headway outside +/- tol of scheduled), measured at
the halfway stop (at stop 2 for "regulate only"). If the simulated trips end on an irregular headway it is "not recovered". EWT is a simulated penalty, not LTA's.
"""
import math

MODEL_VERSION = "halfway-3.0"
EPS = 1e-6

PARAMS = {
    "n_trips": 10,               # trips in the simulated sequence
    "layover_min": 10.0,         # scheduled layover at the first stop (scheduled arrival = scheduled departure - this)
    "min_layover_min": 2.0,      # a trip cannot depart earlier than its actual arrival + this
    "start_delay_min": 0.0,      # option C: replacement passes the halfway stop this many min after the disrupted trip's scheduled time there
    # ---- regulation (options B and D)
    "reg_hold_max": 5.0,         # a trip may be held at most this many min beyond its scheduled departure
    "reg_early_max": 3.0,        # ... or released at most this many min before it (never before arrival + minimum layover)
    "reg_window": 2,             # trips either side of the disrupted trip that may be regulated (and that count as "affected")
    "min_dep_gap": 2.0,          # minimum gap between two consecutive departures at the first stop
    "start_early_max": 5.0,      # option D: the replacement may start this many min earlier than the disrupted trip's scheduled time ...
    "start_late_max": 10.0,      # ... or this many min later (the AI picks the start that evens the headways)
    # ---- rules
    "min_remaining_pct": 20.0,   # a halfway stop is only recommended if at least this % of the route remains after it
    "min_improve_pct": 10.0,     # ... and the regularity (RMS deviation from scheduled headway) improves by at least this %
    "max_mileage_km": 15.0,      # skipped km that counts as the full mileage penalty
    "recover_tol_pct": 20.0,     # recovered = every headway within +/- this % of scheduled
    "recover_points": 3,         # ... for at least this many consecutive headways after the last irregular one
    # ---- score weights (Balanced), %
    "w_regularity": 30.0, "w_maxgap": 25.0, "w_recovery": 20.0, "w_holding": 10.0, "w_mileage": 10.0, "w_bunching": 5.0,
    "pref_recovery_boost": 1.6,  # "Faster headway recovery" multiplies the recovery and max-gap weights by this
    "pref_mileage_boost": 3.0,   # "Minimise mileage loss" multiplies the mileage weight by this
    # ---- propagation
    "load_sens": 0.0,            # 0 = identical running times; e.g. 0.05 = 5% slower per headway-of-slack ahead
    "fallback_kmh": 20.0,        # running speed if no traffic model is available
}
PREFS = ("balanced", "recovery", "mileage")


def _r(x, n=1):
    return None if x is None else round(x, n)


def _mean(v):
    return sum(v) / len(v) if v else None


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def weights(P, pref):
    """Score weights (fractions summing to 1) for the chosen optimisation preference."""
    w = {"regularity": P["w_regularity"], "maxgap": P["w_maxgap"], "recovery": P["w_recovery"], "holding": P["w_holding"], "mileage": P["w_mileage"], "bunching": P["w_bunching"]}
    if pref == "recovery":
        w["recovery"] *= P["pref_recovery_boost"]
        w["maxgap"] *= P["pref_recovery_boost"]
    elif pref == "mileage":
        w["mileage"] *= P["pref_mileage_boost"]
    tot = sum(w.values()) or 1.0
    return {k: v / tot for k, v in w.items()}


# ----------------------------------------------------------------------------- first-stop table
def build_trips(P, H, t0, late, disrupted):
    """t0 = scheduled departure of trip 1 (minutes since midnight). late[i] = lateness of trip i+1's arrival (min). disrupted = 1-based trip number or None."""
    n = int(P["n_trips"])
    trips, prev_dep = [], None
    for i in range(n):
        sd = t0 + i * H
        sa = sd - P["layover_min"]
        lt = float(late[i]) if i < len(late) and late[i] is not None else 0.0
        aa = sa + lt
        t = {"n": i + 1, "sch_arr": sa, "act_arr": aa, "late": lt, "sch_dep": sd, "act_dep": None, "dep_hw": None, "dep_late": None, "status": "", "disrupted": disrupted == i + 1}
        if t["disrupted"]:
            t["status"] = "Disrupted"
        else:
            dep = max(sd, aa + P["min_layover_min"], prev_dep if prev_dep is not None else -1e18)
            t["act_dep"], t["dep_late"] = dep, dep - sd
            t["dep_hw"] = None if prev_dep is None else dep - prev_dep
            t["status"] = "On time" if dep - sd < 0.5 else f"Late +{dep - sd:.0f}"
            prev_dep = dep
        trips.append(t)
    return trips


# ----------------------------------------------------------------------------- headway regulation (the AI's holding / release plan)
def _chain(nodes, g, iters=600):
    """Even out a chain of first-stop departures. nodes: [{"x","lo","hi","free"}] in trip order; fixed nodes are anchors. Minimises the squared deviation of
    the headways (equal target either side, so a free node moves to the mid-point of its neighbours) inside each node's bounds and with at least `g` min
    between consecutive departures. A free node at the end of the chain with no anchor beyond it keeps its place."""
    xs = [nd["x"] for nd in nodes]
    for _ in range(iters):
        moved = 0.0
        for k, nd in enumerate(nodes):
            if not nd["free"]:
                continue
            left = xs[k - 1] if k > 0 else None
            right = xs[k + 1] if k + 1 < len(xs) else None
            t = (left + right) / 2.0 if (left is not None and right is not None) else xs[k]
            lo = max(nd["lo"], left + g) if left is not None else nd["lo"]
            hi = min(nd["hi"], right - g) if right is not None else nd["hi"]
            t = lo if lo > hi else _clip(t, lo, hi)
            moved += abs(t - xs[k])
            xs[k] = t
        if moved < 1e-9:
            break
    return xs


def regulate(P, H, trips, d, with_r, delay):
    """Return ({trip: departure}, r_virtual or None, hold_min). Trips within the window of the disrupted trip may be held / released; the trips just outside
    the window are anchors. With a replacement (`with_r`) it is one more node between the trips either side of the gap, free within its start window."""
    n, w, g = len(trips), int(P["reg_window"]), P["min_dep_gap"]
    u = {t["n"]: t["act_dep"] for t in trips if not t["disrupted"]}
    sd_d = trips[d - 1]["sch_dep"]
    nodes, ids = [], []
    for i in range(max(1, d - w - 1), min(n, d + w + 1) + 1):
        if i == d:
            if with_r:
                nodes.append({"x": sd_d + delay, "lo": sd_d - P["start_early_max"], "hi": sd_d + P["start_late_max"], "free": True})
                ids.append("R")
            continue
        t = trips[i - 1]
        free = d - w <= i <= d + w
        lo = max(t["act_arr"] + P["min_layover_min"], t["sch_dep"] - P["reg_early_max"])
        hi = max(t["sch_dep"] + P["reg_hold_max"], u[i], lo)
        nodes.append({"x": u[i], "lo": lo if free else u[i], "hi": hi if free else u[i], "free": free})
        ids.append(i)
    xs = _chain(nodes, g)
    deps, rv, hold, prev = dict(u), None, 0.0, None
    for k, tid in enumerate(ids):
        nd, x = nodes[k], xs[k]
        if nd["free"]:
            x = float(round(x))                                    # controllers work in whole minutes
            lo_i, hi_i = math.ceil(nd["lo"] - 1e-9), math.floor(nd["hi"] + 1e-9)
            if lo_i <= hi_i:
                x = _clip(x, lo_i, hi_i)
            if prev is not None and x < prev + g:                  # keep the order and the minimum gap after rounding
                x = min(prev + g, max(hi_i, x))
        prev = x
        if tid == "R":
            rv = x
        else:
            hold += abs(x - u[tid])
            deps[tid] = x
    return deps, rv, hold


# ----------------------------------------------------------------------------- downstream propagation
def propagate(deps, tau, H, beta, join=None):
    """deps: [(id, departure time at the first stop)]; tau[j]: running minutes from the first stop to stop j (tau[0] = 0); join: {"id","j","t"} a trip that
    starts at stop j at time t. Returns {id: [time at each stop, or None before the trip starts]}. Trips cannot overtake."""
    n = len(tau)
    T = {tid: [None] * n for tid, _ in deps}
    for tid, dd in deps:
        T[tid][0] = dd + tau[0]
    if join:
        T[join["id"]] = [None] * n
        if join["j"] == 0:
            T[join["id"]][0] = join["t"]
    for j in range(1, n):
        prev = sorted([t for t in T if T[t][j - 1] is not None], key=lambda t: (T[t][j - 1], str(t)))
        seg = tau[j] - tau[j - 1]
        last = None
        for k, t in enumerate(prev):
            h_ahead = (T[t][j - 1] - T[prev[k - 1]][j - 1]) if k > 0 else H
            f = max(0.5, 1.0 + beta * (h_ahead - H) / H) if beta else 1.0
            tj = T[t][j - 1] + seg * f
            if last is not None and tj < last:
                tj = last
            T[t][j] = tj
            last = tj
        if join and join["j"] == j:
            T[join["id"]][j] = join["t"]
    return T


def _gaps(times):
    ts = sorted(times)
    return [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]


def _stop_rec(T, j, aff_ids):
    active = [T[t][j] for t in T if T[t][j] is not None]
    return _gaps([T[t][j] for t in aff_ids if t in T and T[t][j] is not None]), _gaps(active)


def _ewt(gaps, H):
    s = sum(gaps)
    return (sum(g * g for g in gaps) / (2 * s) - H / 2.0) if s > 0 else 0.0


def _dev2(gaps, H):
    return sum((g - H) ** 2 for g in gaps)


def _metrics(recs, H, bm):
    """recs: [(affected gaps, all gaps)] per stop of the comparison section."""
    aff = [r[0] for r in recs]
    pooled = [g for a in aff for g in a]
    worst = [max(a) for a in aff]
    allmin = [min(r[1]) for r in recs if r[1]]
    return {"avg": _r(_mean(pooled), 2), "max": _r(max(pooled), 2), "min": _r(min(pooled), 2),
            "rms": _r(math.sqrt(_mean([(g - H) ** 2 for g in pooled])), 3), "ewt": _r(_mean([_ewt(a, H) for a in aff]), 3),
            "pct_ge_1_5": _r(100.0 * sum(1 for w in worst if w >= 1.5 * H - EPS) / len(worst), 1),
            "pct_ge_2": _r(100.0 * sum(1 for w in worst if w >= 2.0 * H - EPS) / len(worst), 1),
            "pct_le_half": _r(100.0 * sum(1 for g in pooled if g <= 0.5 * H + EPS) / len(pooled), 1),
            "bunched_stops": sum(1 for m in allmin if m < bm - EPS), "min_gap": _r(min(allmin), 2) if allmin else None}


def _recovery(T, j, H, P):
    """How long the irregular headways last at stop j (minutes): from the first headway outside +/- tol to the end of the last one. 0 = never irregular.
    None if the sequence ends before `recover_points` regular headways follow the last irregular one (not recovered within the simulated trips)."""
    ts = sorted(T[t][j] for t in T if T[t][j] is not None)
    g = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    lo, hi = H * (1 - P["recover_tol_pct"] / 100.0), H * (1 + P["recover_tol_pct"] / 100.0)
    bad = [i for i, x in enumerate(g) if x < lo - EPS or x > hi + EPS]
    if not bad:
        return 0.0
    if len(g) - 1 - bad[-1] < 1:
        return None                                   # the sequence ends on an irregular headway: nothing shows it recovered
    return ts[bad[-1] + 1] - ts[bad[0]]               # (if the simulated trips run out before `recover_points` regular headways, the ones that remain are all regular)


# ----------------------------------------------------------------------------- main entry
def simulate(ctx):
    """ctx: H, t0, late[], disrupted (1-based or None), tau[], stop_s[], route_km, stop_names[], stop_seq[], params, pref, regulate (bool), bunch_min,
    candidates [{j, code, name, seq, approved}]."""
    P = {**PARAMS, **(ctx.get("params") or {})}
    if not ctx.get("H") or ctx["H"] <= 0:
        return {"ok": False, "error": "Scheduled headway unknown for this service - enter it in the Headway box, or add it in Bunching & Gap -> Settings -> Service Headway Master."}
    H, tau, n_st, bm = float(ctx["H"]), ctx["tau"], len(ctx["tau"]), ctx.get("bunch_min", 3.0)
    pref = ctx.get("pref") if ctx.get("pref") in PREFS else "balanced"
    n, d, do_reg = int(P["n_trips"]), ctx.get("disrupted"), ctx.get("regulate", True)
    beta = P["load_sens"]
    trips = build_trips(P, H, ctx["t0"], ctx.get("late") or [], d)
    out = {"ok": True, "model": MODEL_VERSION, "sched_hw": H, "trips": trips, "disrupted": d, "n_trips": n, "bunch_min": bm, "params": P, "pref": pref,
           "weights": {k: _r(v, 3) for k, v in weights(P, pref).items()}, "regulate": do_reg}
    if d is None:
        out.update(options=[], recommended=None, message="Mark one trip as Disrupted to simulate losing it.", baseline=None, monitor=[])
        return out
    if not (2 <= d <= n - 1):
        return {"ok": False, "error": f"The disrupted trip must have a trip before and after it: choose a trip between 2 and {n - 1}."}
    w = int(P["reg_window"])
    win = [i for i in range(max(1, d - w), min(n, d + w) + 1) if i != d]
    u = {t["n"]: t["act_dep"] for t in trips if not t["disrupted"]}
    sd_d = trips[d - 1]["sch_dep"]
    W = weights(P, pref)
    names, seqs = ctx.get("stop_names") or [], ctx.get("stop_seq") or []

    def run(deps, j=None, rv=None):
        join = None if j is None else {"id": "R", "j": j, "t": rv + tau[j]}
        T = propagate(list(deps.items()), tau, H, beta, join)
        aff_ids = win + (["R"] if j is not None else [])
        return T, [_stop_rec(T, jj, aff_ids) for jj in range(n_st)]

    TA, recA = run(u)
    dev2A = [_dev2(r[0], H) for r in recA]
    dev2A_total = sum(dev2A) or EPS
    maxA_total = max(max(r[0]) for r in recA)
    horizon = max(1.0, max(TA[t][1] for t in TA) - min(TA[t][1] for t in TA))
    baseline = {"series": [{"j": jj, "wa": _r(max(recA[jj][0]), 1), "a": [_r(x, 1) for x in recA[jj][0]], "min_all": _r(min(recA[jj][1]), 1) if recA[jj][1] else None} for jj in range(n_st)],
                "metrics": _metrics(recA[1:], H, bm), "recovery": _r(_recovery(TA, 1, H, P), 1), "gap": _r(max(recA[0][0]), 1)}

    def build(kind, cand, deps, rv, hold, label):
        j = cand["j"] if cand else None
        start = j if j is not None else 1
        T, recs = run(deps, j, rv)
        sect = range(start, n_st)
        ma, mb = _metrics([recA[jj] for jj in sect], H, bm), _metrics([recs[jj] for jj in sect], H, bm)
        rec_a, rec_b = _recovery(TA, start, H, P), _recovery(T, start, H, P)
        opt = {"kind": kind, "label": label, "j": j, "code": cand.get("code") if cand else None, "name": cand.get("name") if cand else None, "seq": cand.get("seq") if cand else None,
               "approved": cand.get("approved", True) if cand else True, "regulated": kind in ("regulate", "halfway_reg"), "violations": [], "warnings": [],
               "hold_min": _r(hold, 1), "metrics_a": ma, "metrics": mb, "recovery_a": _r(rec_a, 1), "recovery": _r(rec_b, 1),
               "deps": [{"n": i, "dep": deps[i], "shift": _r(deps[i] - u[i], 1)} for i in sorted(deps)], "r_shift": None, "start_time": None, "sch_time": None}
        if j is not None:
            opt.update(start_time=rv + tau[j], sch_time=sd_d + tau[j], r_shift=_r(rv - sd_d, 1), skipped_stops=j, skipped_km=_r(ctx["stop_s"][j], 2), restored_stops=n_st - j,
                       restored_km=_r(ctx["route_km"] - ctx["stop_s"][j], 2))
            remain = 100.0 * (ctx["route_km"] - ctx["stop_s"][j]) / ctx["route_km"] if ctx.get("route_km") else 0.0
            opt["remain_pct"] = _r(remain, 1)
            opt["mileage_pct"] = _r(100.0 * ctx["stop_s"][j] / ctx["route_km"], 1) if ctx.get("route_km") else None
        else:
            opt.update(skipped_stops=0, skipped_km=0.0, restored_stops=n_st - 1, restored_km=_r(ctx["route_km"], 2), remain_pct=100.0, mileage_pct=0.0)
        # ---- rules
        if j is not None:
            if j < 1 or j >= n_st - 1:
                opt["violations"].append("A halfway stop must be after the first stop and before the terminal")
            if opt["remain_pct"] < P["min_remaining_pct"] - EPS:
                opt["violations"].append(f"Only {opt['remain_pct']:.0f}% of the route remains after this stop (minimum {P['min_remaining_pct']:.0f}%)")
            if opt["skipped_km"] > P["max_mileage_km"] + EPS:
                opt["violations"].append(f"Mileage loss {opt['skipped_km']:.1f} km is over the {P['max_mileage_km']:.1f} km limit")
        frac = lambda m: m["bunched_stops"] / max(1, len(sect))
        if mb["bunched_stops"] > ma["bunched_stops"]:
            opt["violations"].append(f"Creates new bunching ({mb['min_gap']:.1f} min headway, below {bm:g} min) on {mb['bunched_stops']} stops")
        imp_rms = 100.0 * (ma["rms"] - mb["rms"]) / ma["rms"] if ma["rms"] else 0.0
        if imp_rms < P["min_improve_pct"] - EPS:
            opt["violations"].append(f"Headway regularity improves only {imp_rms:.0f}% (minimum {P['min_improve_pct']:.0f}%)")
        if j is not None and not opt["approved"]:
            opt["warnings"].append("Not on the approved halfway list: what-if only")
        if j is not None and j > 0:
            opt["warnings"].append(f"The first {j} stops ({ctx['stop_s'][j]:.1f} km) stay without this trip in every option")
        if j is not None and kind == "halfway" and P["start_delay_min"]:
            opt["warnings"].append(f"Replacement assumed {P['start_delay_min']:+.0f} min against the disrupted trip's scheduled time at this stop")
        # ---- score (0 = same as doing nothing)
        dev2_o = [_dev2(recs[jj][0], H) for jj in range(n_st)]
        reg_gain = sum(dev2A[jj] - dev2_o[jj] for jj in sect) / dev2A_total
        hz = max(1.0, horizon)
        ra, rb = (rec_a if rec_a is not None else hz), (rec_b if rec_b is not None else hz)
        parts = {
            "regularity": _clip(reg_gain, -1, 1),
            "maxgap": _clip((ma["max"] - mb["max"]) / max(maxA_total, EPS), -1, 1),
            "recovery": _clip((ra - rb) / ra, -1, 1) if ra > EPS else 0.0,
            "holding": _clip(hold / (max(P["reg_hold_max"], 1.0) * max(1, len(win))), 0, 1) if kind in ("regulate", "halfway_reg") else 0.0,
            "mileage": _clip(opt["skipped_km"] / max(P["max_mileage_km"], EPS), 0, 1),
            "bunching": _clip((frac(mb) - frac(ma)) / 0.25, 0, 1),
        }
        score = 100.0 * (W["regularity"] * parts["regularity"] + W["maxgap"] * parts["maxgap"] + W["recovery"] * parts["recovery"]
                         - W["holding"] * parts["holding"] - W["mileage"] * parts["mileage"] - W["bunching"] * parts["bunching"])
        imp = ma["avg"] - mb["avg"]
        opt.update(parts={k: _r(v, 3) for k, v in parts.items()}, score=_r(score, 1), viable=not opt["violations"],
                   improvement={"avg_min": _r(imp, 2), "avg_pct": _r(100.0 * imp / ma["avg"], 1) if ma["avg"] else 0.0, "max_min": _r(ma["max"] - mb["max"], 2),
                                "rms_pct": _r(imp_rms, 1), "ewt": _r(ma["ewt"] - mb["ewt"], 3),
                                "recovery_saved": _r(rec_a - rec_b, 1) if (rec_a is not None and rec_b is not None) else None},
                   series=[{"j": jj, "wa": _r(max(recA[jj][0]), 1), "wb": _r(max(recs[jj][0]), 1), "a": [_r(x, 1) for x in recA[jj][0]], "b": [_r(x, 1) for x in recs[jj][0]],
                            "imp": _r(max(recA[jj][0]) - max(recs[jj][0]), 1), "min_b": _r(min(recs[jj][1]), 1) if recs[jj][1] else None} for jj in range(n_st)])
        return opt

    options = []
    if do_reg:
        deps, _, hold = regulate(P, H, trips, d, False, 0.0)
        options.append(build("regulate", None, deps, None, hold, "Regulate headway only (no halfway)"))
    for c in ctx.get("candidates") or []:
        j = c["j"]
        if j < 1 or j >= n_st - 1:
            options.append({"kind": "halfway", "label": f"Halfway at {c.get('name')}", "j": j, "code": c.get("code"), "name": c.get("name"), "seq": c.get("seq"), "approved": c.get("approved", True),
                            "regulated": False, "violations": ["A halfway stop must be after the first stop and before the terminal"], "warnings": [], "viable": False, "metrics": None, "metrics_a": None,
                            "series": [], "improvement": None, "score": None, "parts": None, "deps": [], "hold_min": 0})
            continue
        options.append(build("halfway", c, dict(u), sd_d + P["start_delay_min"], 0.0, f"Halfway at {c.get('name')}"))
        if do_reg:
            deps, rv, hold = regulate(P, H, trips, d, True, P["start_delay_min"])
            options.append(build("halfway_reg", c, deps, rv, hold, f"Halfway at {c.get('name')} + regulation"))
    okc = [o for o in options if o.get("viable") and o["score"] is not None and o["score"] > 0]
    best = max(okc, key=lambda o: (o["score"], -(o["j"] or 0))) if okc else None
    for k, o in enumerate(sorted([o for o in options if o.get("score") is not None], key=lambda o: (-o["score"], o["j"] or 0)), 1):
        o["rank"] = k
    if best is not None:
        msg = "Recommended - highest simulation score"
    elif not options:
        msg = "No approved halfway points are configured for this service and direction."
    else:
        msg = "No option beats doing nothing without creating a problem."
    mon = _monitor(n_st, 12)
    monitor = [{"j": j, "name": names[j] if j < len(names) else "", "seq": seqs[j] if j < len(seqs) else j + 1, "terminal": j == n_st - 1} for j in mon]
    out.update(options=options, recommended=(options.index(best) if best is not None else None), message=msg, monitor=monitor, baseline=baseline,
               gap_min=_r(u[d + 1] - u[d - 1], 1), window=win)
    return out


def _monitor(n, k):
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
