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

"Affected headways" = the consecutive headways among the trips in the window (`reg_window` trips either side of the disrupted one, plus the replacement). The AVERAGE
(and EWT) is taken over the 2 trips either side only, so a wide window does not dilute it; max / min / RMS / counts use the whole window so the side effects of a wide ramp show.
Recovery time = how long the irregular headways last (from the start of the first to the end of the last headway outside +/- tol of scheduled), measured at
the halfway stop (at stop 2 for "regulate only"). If the simulated trips end on an irregular headway it is "not recovered". EWT is a simulated penalty, not LTA's.
"""
import math

MODEL_VERSION = "halfway-12.4-operational-priority"
EPS = 1e-6

PARAMS = {
    "n_trips": 10,               # trips in the simulated sequence
    "layover_min": 10.0,         # scheduled layover at the first stop (scheduled arrival = scheduled departure - this)
    "min_layover_min": 7.0,      # mandatory break: every trip cannot depart earlier than actual arrival + 7 min
    "start_delay_min": 0.0,      # option C: replacement passes the halfway stop this many min after the disrupted trip's scheduled time there
    # ---- regulation (options B and D)
    "reg_hold_max": 8.0,          # HARD: artificial departure adjustment may not exceed +8 min
    "max_headway_min": 30.0,      # HARD service target: selected plan must keep max headway <30 min when feasible
    "reg_early_max": 15.0,        # ... or released at most this many min before it (never before arrival + minimum layover)
    "reg_window": 3,             # rolling regulation horizon: minimum 3 trips before + 3 after the affected slot; spread recovery instead of over-holding one BC
                                 # trips either side of the disrupted trip that may be regulated (and that count as "affected"): 3 up + 3 down = 6, the lost trip's slot is shared
                                 # by those 6 trips (7 slots x headway / 6 trips). A wider window ramps the correction over more trips
    "reg_even_share": 1,         # 1 = the interchange departures share the lost trip's slot evenly (the halfway bus is NOT part of that calculation; it is placed on top);
                                 # the last trip of the window settles on its own time. 0 = the older joint calculation (kept for comparison)
    "reg_min_side": 3,           # the AI regulates at least this many trips BEFORE and AFTER the gap at the interchange (3 up + 3 down = 6). If the sequence has fewer trips before
                                 # the disrupted one, more trips AFTER it are regulated instead (0 = off: use reg_window only)
    "reg_early_future": 8.0,     # ... and then those future trips may depart up to this many min early (never before arrival + minimum layover) to close the gap
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
    "w_regularity": 30.0, "w_maxgap": 30.0, "w_recovery": 15.0, "w_holding": 15.0, "w_mileage": 7.0, "w_bunching": 3.0,
    "pref_recovery_boost": 1.6,  # "Faster headway recovery" multiplies the recovery and max-gap weights by this
    "pref_mileage_boost": 3.0,   # "Minimise mileage loss" multiplies the mileage weight by this
    # ---- propagation
    "load_sens": 0.0,            # 0 = identical running times; e.g. 0.05 = 5% slower per headway-of-slack ahead
    "auto_late_min": 1.0,        # the AI (Run AI Optimisation) considers a trip for disruption / halfway when it would leave at least this many min late; the others just run and are adjusted
    "max_disrupted": 4,          # how many trips may be disrupted (lost) at once
    "fallback_kmh": 20.0,        # running speed if no traffic model is available
    "offsvc_factor": 0.7,        # the halfway bus runs the section to the halfway stop OFF-SERVICE (no dwell, no stopping) in this fraction of the in-service running time. UNVALIDATED assumption
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
def _norm_dis(v):
    """Disrupted trips as a sorted list of unique ints (accepts None, an int, or a list / tuple / set)."""
    if v is None or v == "":
        return []
    xs = v if isinstance(v, (list, tuple, set)) else [v]
    out = []
    for x in xs:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return sorted(set(out))


def build_trips(P, H, t0, late, disrupted):
    """t0 = scheduled departure of trip 1 (minutes since midnight). late[i] = lateness of trip i+1's arrival (min). disrupted = a 1-based trip number, a list of them, or None."""
    n = int(P["n_trips"])
    dset = set(_norm_dis(disrupted))
    trips, prev_dep = [], None
    for i in range(n):
        sd = t0 + i * H
        sa = sd - P["layover_min"]
        lt = float(late[i]) if i < len(late) and late[i] is not None else 0.0
        aa = sa + lt
        t = {"n": i + 1, "sch_arr": sa, "act_arr": aa, "late": lt, "sch_dep": sd, "act_dep": None, "dep_hw": None, "dep_late": None, "status": "", "disrupted": (i + 1) in dset}
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


def _span(P, n, d, dl=None):
    """(trips before, trips after) the disrupted trip that the AI may regulate. Default window `reg_window` each side, but at least `reg_min_side` each side (3 up + 3 down);
    when the sequence has fewer than that BEFORE the disrupted trip (its 'top' trips), more trips AFTER it are used so that at least 2 x reg_min_side trips are regulated
    (and the other way round at the end of the sequence)."""
    ms, w = int(P.get("reg_min_side", 0) or 0), int(P["reg_window"])
    dl = d if dl is None else dl
    up_av, dn_av = d - 1, n - dl
    wu, wd = min(up_av, max(w, ms)), min(dn_av, max(w, ms))
    if ms:
        if wu < ms:
            wd = min(dn_av, max(wd, 2 * ms - wu))
        if wd < ms:
            wu = min(up_av, max(wu, 2 * ms - wd))
    return wu, wd


def _regulate_joint(P, H, trips, d, with_r, delay, rv_min=None):
    """(older joint version, reg_even_share = 0) Return ({trip: departure}, r_virtual or None, hold_min). The trips inside the span (see _span) may be held / released at the first stop (the interchange); the trips just
    outside it are anchors. A trip before the first / after the last simulated one is assumed to run on schedule (a virtual anchor), so the first and last trips can be regulated
    too. With a replacement (`with_r`) it is one more node between the trips either side of the gap, free within its start window. When there are fewer than `reg_min_side`
    trips before the gap, the future trips may leave up to `reg_early_future` min early."""
    n, g = len(trips), P["min_dep_gap"]
    wu, wd = _span(P, n, d)
    short_up = int(P.get("reg_min_side", 0) or 0) > 0 and wu < int(P["reg_min_side"])
    u = {t["n"]: t["act_dep"] for t in trips if not t["disrupted"]}
    sd_d = trips[d - 1]["sch_dep"]
    sd1, sdn = trips[0]["sch_dep"], trips[-1]["sch_dep"]
    nodes, ids = [], []
    for i in range(d - wu - 1, d + wd + 2):
        if i == d:
            if with_r:
                lo_r = sd_d - P["start_early_max"] if rv_min is None else max(sd_d - P["start_early_max"], rv_min)      # the delayed bus cannot start before it is there
                nodes.append({"x": max(sd_d + delay, lo_r), "lo": lo_r, "hi": max(sd_d + P["start_late_max"], lo_r), "free": True})
                ids.append("R")
            continue
        if i < 1 or i > n:                                      # a trip outside the simulated sequence: on schedule, never moved
            x = sd1 + (i - 1) * H if i < 1 else sdn + (i - n) * H
            nodes.append({"x": x, "lo": x, "hi": x, "free": False})
            ids.append(i)
            continue
        t = trips[i - 1]
        free = d - wu <= i <= d + wd
        early = max(P["reg_early_max"], P["reg_early_future"]) if (short_up and i > d) else P["reg_early_max"]
        lo = max(t["act_arr"] + P["min_layover_min"], t["sch_dep"] - early)
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
        elif isinstance(tid, int) and 1 <= tid <= n:
            hold += abs(x - u[tid])
            deps[tid] = x
    return deps, rv, hold


def _regulate_share(P, H, trips, D, Rset, delay, rvmin, full=()):
    """The interchange departures share the lost trips' slots evenly: the trips before the first gap (wu) and after the last one (wd) - and any trips between lost ones - are held / released so
    the headways between them are equal: (k + m) slots x H spread over k trips (m = lost trips), e.g. 7 x 12 min / 6 trips = 14 min for one lost trip. The trip before the span and the LAST trip
    of the span stay on their own times. Halfway buses (Rset = the lost trips that get one) are placed afterwards, evenly inside the regulated gap they belong to, as close as their start window
    and the bus's arrival allow. Returns ({trip: departure}, {lost trip: start as a first-stop-equivalent time}, hold_min)."""
    n, g = len(trips), P["min_dep_gap"]
    d, dl, Dset = D[0], D[-1], set(D) - set(full)              # the block (first .. last marked trip); a marked trip that still runs (`full`) is a normal, regulated trip
    wu, wd = _span(P, n, d, dl)
    short_up = int(P.get("reg_min_side", 0) or 0) > 0 and wu < int(P["reg_min_side"])
    u = {t["n"]: t["act_dep"] for t in trips if not t["disrupted"]}
    sd1 = trips[0]["sch_dep"]
    last = dl + wd
    nodes, ids = [], []
    for i in range(d - wu - 1, last + 1):
        if i in Dset:
            continue
        if i < 1:                                               # a trip before the simulated sequence: on schedule, never moved
            x = sd1 + (i - 1) * H
            nodes.append({"x": x, "lo": x, "hi": x, "free": False})
            ids.append(i)
            continue
        t = trips[i - 1]
        free = d - wu <= i <= last - 1
        early = max(P["reg_early_max"], P["reg_early_future"]) if (short_up and i > d) else P["reg_early_max"]
        lo = max(t["act_arr"] + P["min_layover_min"], t["sch_dep"] - early)
        hi = max(t["sch_dep"] + P["reg_hold_max"], u[i], lo)
        nodes.append({"x": u[i], "lo": lo if free else u[i], "hi": hi if free else u[i], "free": free})
        ids.append(i)
    xs = _chain(nodes, g)
    deps, hold, prev = dict(u), 0.0, None
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
        if 1 <= tid <= n:
            hold += abs(x - u[tid])
            deps[tid] = x
    rvs = {}
    for i in Rset:                                                 # a halfway bus for lost trip i: evenly inside the regulated gap of the run of lost trips it belongs to
        l1 = i
        while l1 - 1 in Dset:
            l1 -= 1
        l2 = i
        while l2 + 1 in Dset:
            l2 += 1
        xp, xq = deps[l1 - 1], deps[l2 + 1]
        mid = xp + (xq - xp) * (i - l1 + 1) / (l2 - l1 + 2)
        sdi = trips[i - 1]["sch_dep"]
        rm = (rvmin or {}).get(i)
        lo_r = sdi - P["start_early_max"] if rm is None else max(sdi - P["start_early_max"], rm)
        hi_r = max(sdi + P["start_late_max"], lo_r)
        rv = float(round(_clip(mid, lo_r, hi_r)))
        lo_i, hi_i = math.ceil(lo_r - 1e-9), math.floor(hi_r + 1e-9)
        rvs[i] = _clip(rv, lo_i, hi_i) if lo_i <= hi_i else lo_r
    return deps, rvs, hold


def regulate(P, H, trips, D, Rset, delay, rvmin=None, full=()):
    """D = the lost trips, Rset = the lost trips that get a halfway bus. Returns ({trip: departure}, {lost trip: start}, hold_min)."""
    D, Rset = _norm_dis(D), list(Rset or [])
    if int(P.get("reg_even_share", 1)) or len(D) > 1 or full:
        return _regulate_share(P, H, trips, D, Rset, delay, rvmin or {}, full)
    d = D[0]
    deps, rv, hold = _regulate_joint(P, H, trips, d, bool(Rset), delay, (rvmin or {}).get(d))
    return deps, ({d: rv} if rv is not None else {}), hold


# ----------------------------------------------------------------------------- downstream propagation
def propagate(deps, tau, H, beta, join=None):
    """deps: [(id, departure time at the first stop)]; tau[j]: running minutes from the first stop to stop j (tau[0] = 0); join: {"id","j","t"} a trip that
    starts at stop j at time t. Returns {id: [time at each stop, or None before the trip starts]}. Trips cannot overtake."""
    n = len(tau)
    joins = [] if not join else (join if isinstance(join, list) else [join])
    T = {tid: [None] * n for tid, _ in deps}
    for tid, dd in deps:
        T[tid][0] = dd + tau[0]
    for jn in joins:
        T[jn["id"]] = [None] * n
        if jn["j"] == 0:
            T[jn["id"]][0] = jn["t"]
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
        for jn in joins:
            if jn["j"] == j:
                T[jn["id"]][j] = jn["t"]
    return T


def _tsd(T):
    """{trip id: [time at each stop or None]} rounded to 0.1 min (JSON-friendly: keys are strings)."""
    return {str(k): [None if x is None else round(x, 1) for x in v] for k, v in T.items()}


def _gaps(times):
    ts = sorted(times)
    return [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]


def _stop_rec(T, j, aff_ids, core_ids=None):
    """(headways among the regulated window, headways among all active trips, headways among the core trips next to the gap)."""
    active = [T[t][j] for t in T if T[t][j] is not None]
    core = aff_ids if core_ids is None else core_ids
    return (_gaps([T[t][j] for t in aff_ids if t in T and T[t][j] is not None]), _gaps(active), _gaps([T[t][j] for t in core if t in T and T[t][j] is not None]))


def _ewt(gaps, H):
    s = sum(gaps)
    return (sum(g * g for g in gaps) / (2 * s) - H / 2.0) if s > 0 else 0.0


def _dev2(gaps, H):
    return sum((g - H) ** 2 for g in gaps)


def _metrics(recs, H, bm):
    """recs: [(affected gaps, all gaps)] per stop of the comparison section."""
    aff = [r[0] for r in recs]
    pooled = [g for a in aff for g in a]
    core = [r[2] for r in recs]
    cpool = [g for a in core for g in a]
    worst = [max(a) for a in aff]
    allmin = [min(r[1]) for r in recs if r[1]]
    # avg / EWT: the headways around the gap (2 trips either side + the replacement), so a wide regulation window does not dilute them with normal headways;
    # max / min / rms / % counts / bunching: every headway inside the regulated window, so the side effects of a wide ramp are counted
    return {"avg": _r(_mean(cpool), 2), "max": _r(max(pooled), 2), "min": _r(min(pooled), 2),
            "rms": _r(math.sqrt(_mean([(g - H) ** 2 for g in pooled])), 3), "ewt": _r(_mean([_ewt(a, H) for a in core]), 3),
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
    """ctx: H, t0, late[], disrupted (a 1-based trip, a list of them, or None), tau[], stop_s[], route_km, stop_names[], stop_seq[], params, pref, regulate (bool), bunch_min,
    candidates [{j, code, name, seq, approved}], veh, ready, keep."""
    P = {**PARAMS, **(ctx.get("params") or {})}
    if not ctx.get("H") or ctx["H"] <= 0:
        return {"ok": False, "error": "Scheduled headway unknown for this service - enter it in the Headway box, or add it in Bunching & Gap -> Settings -> Service Headway Master."}
    H, tau, n_st, bm = float(ctx["H"]), ctx["tau"], len(ctx["tau"]), ctx.get("bunch_min", 3.0)
    pref = ctx.get("pref") if ctx.get("pref") in PREFS else "balanced"
    n, do_reg = int(P["n_trips"]), ctx.get("regulate", True)
    D = _norm_dis(ctx.get("disrupted"))
    beta = P["load_sens"]
    trips = build_trips(P, H, ctx["t0"], ctx.get("late") or [], D)
    veh = ctx.get("veh") if ctx.get("veh") in ("own", "standby") else "standby"          # own = the delayed bus runs off-service to the halfway stop; standby = a spare bus is at the stop at the slot time
    out = {"ok": True, "model": MODEL_VERSION, "sched_hw": H, "trips": trips, "disrupted": D[0] if D else None, "disrupted_all": D, "n_lost": len(D), "n_trips": n, "mode": "disrupt" if D else "continue",
           "bunch_min": bm, "params": P, "pref": pref, "weights": {k: _r(v, 3) for k, v in weights(P, pref).items()}, "regulate": do_reg, "veh": veh}
    block = _norm_dis(ctx.get("block"))
    if not D and not block:
        out.update(options=[], recommended=None, best_gain=None, baseline=None, monitor=[],
                   message="Enter the lateness of the late trips, then press Run AI Optimisation: the AI decides whether to disrupt a trip (halfway) or just adjust and continue service.")
        return out
    cont = not D                                   # continue service: nothing is lost, the AI only holds / releases the trips around the late ones
    Bk = D if D else block
    do_reg = True if cont else do_reg
    m, d, dl, Dset = len(D), Bk[0], Bk[-1], set(D)
    if d < 2 or dl > n - 1:
        return {"ok": False, "error": ("The disrupted trip must have a trip before and after it: choose a trip" if m == 1 else "The disrupted trips must have a trip before and after them: choose trips") + f" between 2 and {n - 1}."}
    if m > int(P["max_disrupted"]):
        return {"ok": False, "error": f"At most {int(P['max_disrupted'])} trips can be disrupted at once."}
    F = _clip(P["offsvc_factor"], 0.2, 1.0)
    own_ready = {i: (float(ctx["ready"]) if ctx.get("ready") is not None else trips[i - 1]["act_arr"] + P["min_layover_min"]) for i in D}     # the earliest each halfway bus can leave the first stop (default: the delayed bus itself)
    sd = {i: trips[i - 1]["sch_dep"] for i in D}
    delay = P["start_delay_min"]
    rid = (lambda i: "R") if m == 1 else (lambda i: f"R{i}")
    wu, wd = _span(P, n, d, dl)
    win = [i for i in range(d - wu, dl + wd + 1) if i not in Dset]
    core = [i for i in win if min(abs(i - x) for x in Bk) <= 2]
    ms = int(P.get("reg_min_side", 0) or 0)
    show_up = min(d - 1, ms or 3); show_dn = min(n - dl, max(ms or 3, 2 * (ms or 3) - show_up))          # the trips drawn in the simple before / after picture (3 up + 3 down, more down if the top is short)
    u = {t["n"]: t["act_dep"] for t in trips if not t["disrupted"]}
    W = weights(P, pref)
    names, seqs = ctx.get("stop_names") or [], ctx.get("stop_seq") or []

    def run(deps, j=None, rvs=None):
        rvs = rvs or {}
        joins = [{"id": rid(i), "j": j, "t": rv + tau[j]} for i, rv in sorted(rvs.items())] if j is not None else []
        T = propagate(list(deps.items()), tau, H, beta, joins)
        rids = [rid(i) for i in sorted(rvs)] if j is not None else []
        return T, [_stop_rec(T, jj, win + rids, core + rids) for jj in range(n_st)]

    TA, recA = run(u)
    dev2A = [_dev2(r[0], H) for r in recA]
    dev2A_total = sum(dev2A) or EPS
    maxA_total = max(max(r[0]) for r in recA)
    horizon = max(1.0, max(TA[t][1] for t in TA) - min(TA[t][1] for t in TA))
    baseline = {"series": [{"j": jj, "wa": _r(max(recA[jj][0]), 1), "a": [_r(x, 1) for x in recA[jj][0]], "min_all": _r(min(recA[jj][1]), 1) if recA[jj][1] else None} for jj in range(n_st)],
                "metrics": _metrics(recA[1:], H, bm), "recovery": _r(_recovery(TA, 1, H, P), 1), "gap": _r(max(recA[0][0]), 1)}

    def build(kind, cand, deps, rvs, hold, label, light=False, excl=None):
        rvs = rvs or {}
        j = cand["j"] if cand else None
        f = min(rvs) if rvs else None                       # the first halfway bus (the fields a single-disruption page reads)
        start = j if j is not None else 1
        T, recs = run(deps, j, rvs)
        sect = range(start, n_st)
        ma, mb = _metrics([recA[jj] for jj in sect], H, bm), _metrics([recs[jj] for jj in sect], H, bm)
        rec_a, rec_b = _recovery(TA, start, H, P), _recovery(T, start, H, P)
        opt = {"kind": kind, "label": label, "tsd": None if light else _tsd(T), "reg_trips": sum(1 for i in deps if abs(deps[i] - u[i]) >= 0.5) + sum(1 for i in rvs if abs(rvs[i] - sd[i] - delay) >= 0.5),
               "j": j, "code": cand.get("code") if cand else None, "name": cand.get("name") if cand else None, "seq": cand.get("seq") if cand else None,
               "approved": cand.get("approved", True) if cand else True, "regulated": kind in ("regulate", "halfway_reg"), "violations": [], "warnings": [],
               "hold_min": _r(hold, 1), "metrics_a": ma, "metrics": mb, "recovery_a": _r(rec_a, 1), "recovery": _r(rec_b, 1),
               "deps": [{"n": i, "dep": deps[i], "shift": _r(deps[i] - u[i], 1)} for i in sorted(deps)], "r_shift": None, "start_time": None, "sch_time": None}
        if j is not None:
            opt.update(start_time=rvs[f] + tau[j], sch_time=sd[f] + tau[j], r_shift=_r(rvs[f] - sd[f], 1), skipped_stops=j, skipped_km=_r(ctx["stop_s"][j], 2), restored_stops=n_st - j,
                       restored_km=_r(ctx["route_km"] - ctx["stop_s"][j], 2))
            remain = 100.0 * (ctx["route_km"] - ctx["stop_s"][j]) / ctx["route_km"] if ctx.get("route_km") else 0.0
            opt["remain_pct"] = _r(remain, 1)
            opt["mileage_pct"] = _r(100.0 * ctx["stop_s"][j] / ctx["route_km"], 1) if ctx.get("route_km") else None
            arrive = own_ready[f] + F * tau[j]                                # when the delayed bus could be at this stop, running off-service from the first stop
            opt.update(veh=veh, leave_first=own_ready[f], off_min=_r(F * tau[j], 1), own_arrive=arrive, late_start=_r(rvs[f] - sd[f], 1), wait_min=_r(max(0.0, rvs[f] + tau[j] - arrive), 1) if veh == "own" else None,
                       auto=bool(cand.get("auto")))
            opt["repl"] = [{"trip": i, "id": rid(i), "start_time": _r(rvs[i] + tau[j], 1), "sch_time": _r(sd[i] + tau[j], 1), "late_start": _r(rvs[i] - sd[i], 1), "leave_first": _r(own_ready[i], 1),
                            "own_arrive": _r(own_ready[i] + F * tau[j], 1), "off_min": _r(F * tau[j], 1), "bus_late": _r(trips[i - 1]["late"], 1),
                            "wait_min": _r(max(0.0, rvs[i] + tau[j] - (own_ready[i] + F * tau[j])), 1) if veh == "own" else None} for i in sorted(rvs)]
            opt["skipped_trips"] = list(excl or [])
        else:
            opt.update(skipped_stops=0, skipped_km=0.0, restored_stops=n_st - 1, restored_km=_r(ctx["route_km"], 2), remain_pct=100.0, mileage_pct=0.0, repl=[], skipped_trips=[])
        # ---- rules
        if j is not None:
            if j < 1 or j >= n_st - 1:
                opt["violations"].append("A halfway stop must be after the first stop and before the terminal")
            if opt["remain_pct"] < P["min_remaining_pct"] - EPS:
                opt["violations"].append(f"Only {opt['remain_pct']:.0f}% of the route remains after this stop (minimum {P['min_remaining_pct']:.0f}%)")
            if opt["skipped_km"] > P["max_mileage_km"] + EPS:
                opt["violations"].append(f"Mileage loss {opt['skipped_km']:.1f} km is over the {P['max_mileage_km']:.1f} km limit")
            if veh == "own":
                for i in sorted(rvs):
                    if rvs[i] - sd[i] > P["start_late_max"] + EPS:
                        opt["violations"].append((f"Trip {i}: " if m > 1 else "") + f"The delayed bus cannot fill the gap from here: it reaches this stop {rvs[i] - sd[i]:.0f} min after the lost trip's slot (limit {P['start_late_max']:.0f} min) - use a standby bus or a later stop")
                        break
            for i in (excl or []):
                opt["warnings"].append(f"Trip {i}'s bus cannot reach this stop in time: its slot gets no halfway bus and is shared by the regulated trips")
        frac = lambda mm: mm["bunched_stops"] / max(1, len(sect))
        if mb["bunched_stops"] > ma["bunched_stops"]:
            opt["violations"].append(f"Creates new bunching ({mb['min_gap']:.1f} min headway, below {bm:g} min) on {mb['bunched_stops']} stops")
        imp_rms = 100.0 * (ma["rms"] - mb["rms"]) / ma["rms"] if ma["rms"] else 0.0
        if imp_rms < P["min_improve_pct"] - EPS:
            opt["violations"].append(f"Headway regularity improves only {imp_rms:.0f}% (minimum {P['min_improve_pct']:.0f}%)")
        if j is not None and not opt["approved"] and not cand.get("auto"):
            opt["warnings"].append("Not on the approved halfway list: what-if only")
        if j is not None and j > 0:
            opt["warnings"].append(f"The first {j} stop{'' if j == 1 else 's'} ({ctx['stop_s'][j]:.1f} km) stay without this trip in every option")
        if j is not None and kind == "halfway" and delay:
            opt["warnings"].append(f"Replacement assumed {delay:+.0f} min against the disrupted trip's scheduled time at this stop")
        # ---- score (0 = same as doing nothing)
        dev2_o = [_dev2(recs[jj][0], H) for jj in range(n_st)]
        reg_gain = sum(dev2A[jj] - dev2_o[jj] for jj in range(n_st)) / dev2A_total          # every stop: the sections a halfway bus cannot reach only count if the regulation improves them
        gain_min = sum(max(recA[jj][0]) - max(recs[jj][0]) for jj in range(n_st)) / n_st       # headway gain: the average reduction of the largest headway over every stop of the route
        hz = max(1.0, horizon)
        ra, rb = (rec_a if rec_a is not None else hz), (rec_b if rec_b is not None else hz)
        parts = {
            "regularity": _clip(reg_gain, -1, 1),
            "maxgap": _clip(gain_min / max(maxA_total, EPS), -1, 1),          # averaged over EVERY stop: a halfway bus cannot fix the stops before its start
            "recovery": _clip((ra - rb) / ra, -1, 1) if ra > EPS else 0.0,
            "holding": _clip(hold / (max(P["reg_hold_max"], 1.0) * 4.0), 0, 1) if kind in ("regulate", "halfway_reg") else 0.0,
            "mileage": _clip(opt["skipped_km"] / max(P["max_mileage_km"], EPS), 0, 1),
            "bunching": _clip((frac(mb) - frac(ma)) / 0.25, 0, 1),
        }
        score = 100.0 * (W["regularity"] * parts["regularity"] + W["maxgap"] * parts["maxgap"] + W["recovery"] * parts["recovery"]
                         - W["holding"] * parts["holding"] - W["mileage"] * parts["mileage"] - W["bunching"] * parts["bunching"])
        imp = ma["avg"] - mb["avg"]
        opt.update(parts={k: _r(v, 3) for k, v in parts.items()}, score=_r(score, 1), viable=not opt["violations"],
                   gain={"min": _r(gain_min, 2), "pct": _r(100.0 * gain_min / max(maxA_total, EPS), 1)},
                   improvement={"avg_min": _r(imp, 2), "avg_pct": _r(100.0 * imp / ma["avg"], 1) if ma["avg"] else 0.0, "max_min": _r(ma["max"] - mb["max"], 2),
                                "rms_pct": _r(imp_rms, 1), "ewt": _r(ma["ewt"] - mb["ewt"], 3),
                                "recovery_saved": _r(rec_a - rec_b, 1) if (rec_a is not None and rec_b is not None) else None},
                   series=None if light else [{"j": jj, "wa": _r(max(recA[jj][0]), 1), "wb": _r(max(recs[jj][0]), 1), "a": [_r(x, 1) for x in recA[jj][0]], "b": [_r(x, 1) for x in recs[jj][0]],
                            "imp": _r(max(recA[jj][0]) - max(recs[jj][0]), 1), "min_b": _r(min(recs[jj][1]), 1) if recs[jj][1] else None} for jj in range(n_st)])
        return opt

    options = []
    if do_reg:
        deps, _, hold = regulate(P, H, trips, Bk, [], 0.0, None, full=(Bk if cont else ()))
        options.append(build("regulate", None, deps, None, hold, "Adjust the trips and continue service (no disruption, no halfway)" if cont else "Regulate headway only (no halfway)"))
    for c in ([] if cont else (ctx.get("candidates") or [])):
        j = c["j"]
        if j < 1 or j >= n_st - 1:
            options.append({"kind": "halfway", "label": f"Halfway at {c.get('name')}", "j": j, "code": c.get("code"), "name": c.get("name"), "seq": c.get("seq"), "approved": c.get("approved", True),
                            "regulated": False, "violations": ["A halfway stop must be after the first stop and before the terminal"], "warnings": [], "viable": False, "metrics": None, "metrics_a": None,
                            "series": [], "improvement": None, "score": None, "parts": None, "deps": [], "hold_min": 0})
            continue
        rvm = {i: ((own_ready[i] + F * tau[j] - tau[j]) if veh == "own" else None) for i in D}          # earliest start (as a first-stop-equivalent time) each delayed bus allows
        base = {i: (max(sd[i] + delay, rvm[i]) if rvm[i] is not None else sd[i] + delay) for i in D}
        feas = [i for i in D if veh != "own" or base[i] - sd[i] <= P["start_late_max"] + EPS]
        use = feas if feas else list(D)                      # buses that cannot make it are left out; if none can, the option is built (and rejected) with all of them
        excl = [i for i in D if i not in use]
        options.append(build("halfway", c, dict(u), {i: base[i] for i in use}, 0.0, f"Halfway at {c.get('name')}", excl=excl))
        if do_reg:
            deps, rvs, hold = regulate(P, H, trips, D, use, delay, {i: rvm[i] for i in use})
            options.append(build("halfway_reg", c, deps, rvs, hold, f"Halfway at {c.get('name')} + regulation", excl=excl))
    if do_reg and int(P.get("reg_even_share", 1)):        # with AI regulation on, a plan must regulate the interchange: a plain halfway bus is kept as a reference
        for o in options:
            if o["kind"] == "halfway" and o.get("metrics"):
                o["ref_only"] = True
                o["warnings"].append("Reference only: it leaves the interchange headway unregulated, so the stops before the halfway bus starts keep the long gap")
    okc = [o for o in options if o.get("viable") and o["score"] is not None and o["score"] > 0 and not o.get("ref_only")]
    best = max(okc, key=lambda o: (o["score"], -(o["j"] or 0))) if okc else None
    for k, o in enumerate(sorted([o for o in options if o.get("score") is not None], key=lambda o: (-o["score"], o["j"] or 0)), 1):
        o["rank"] = k
    # ---- the halfway plan with the most headway gain (the average reduction of the largest headway over every stop)
    gpool = [o for o in options if o["kind"] in ("halfway", "halfway_reg") and o.get("viable") and o.get("gain") and o["gain"]["min"] > 0]
    gpool = [o for o in gpool if not o.get("ref_only")] or gpool
    bgain = max(gpool, key=lambda o: (o["gain"]["min"], o["score"] or 0, -(o["j"] or 0))) if gpool else None
    hw_opts = [o for o in options if o["kind"] in ("halfway", "halfway_reg") and o.get("metrics")]
    reg_o = next((o for o in options if o["kind"] == "regulate" and o.get("metrics")), None)
    reg_enough = bool(reg_o and reg_o["metrics"]["max"] <= H * (1 + P["recover_tol_pct"] / 100.0) + EPS and reg_o["metrics"]["min"] >= H * (1 - P["recover_tol_pct"] / 100.0) - EPS)
    if best is not None:
        msg = "Recommended - lowest regulated headway spread"
    elif not options:
        msg = "No halfway stop could be tested for this service and direction (none eligible, or none approved)."
    elif veh == "own" and hw_opts and all(any("cannot fill the gap" in v for v in o["violations"]) for o in hw_opts if o["kind"] == "halfway"):
        msg = (f"The delayed bus cannot reach any tested stop in time to fill the gap (it would be more than {P['start_late_max']:.0f} min after the lost trip's slot at every stop). "
               "Choose 'Standby bus' if a spare bus is available, or rely on regulation.")
    else:
        msg = "No option beats doing nothing without creating a problem."
    # ---- start-time tolerance for the plain halfway options we keep in detail (one lost trip), and a lighter payload for the rest (a full-route scan tests dozens of stops)
    keep_codes = set(ctx.get("keep") or [])
    scored = sorted([o for o in options if o.get("score") is not None], key=lambda o: (-o["score"], o["j"] or 0))
    if len(options) > 24:
        keep = {id(o) for o in scored[:10]} | {id(o) for o in options if o["kind"] == "regulate" or o.get("code") in keep_codes}
        for o_ in (best, bgain):
            if o_ is not None:
                keep.add(id(o_))
    else:
        keep = {id(o) for o in options}
    wcache = {}

    def start_window(c):
        j = c["j"]
        if m != 1:
            return None
        if j in wcache:
            return wcache[j]
        lo_rv = sd[d] - P["start_early_max"]
        if veh == "own":
            lo_rv = max(lo_rv, own_ready[d] + F * tau[j] - tau[j])
        ok = []
        hi_rv = sd[d] + P["start_late_max"]
        for k in [lo_rv] + [float(x) for x in range(math.floor(lo_rv) + 1, math.floor(hi_rv + 1e-9) + 1)]:       # the earliest possible start, then whole minutes
            o2 = build("halfway", c, dict(u), {d: k}, 0.0, "", light=True)
            if o2["viable"] and o2["score"] is not None and o2["score"] > 0:
                ok.append(k)
        wcache[j] = {"from": _r(min(ok) + tau[j], 1), "to": _r(max(ok) + tau[j], 1), "sched": _r(sd[d] + tau[j], 1)} if ok else None
        return wcache[j]
    cmap = {c["j"]: c for c in (ctx.get("candidates") or [])}
    for o in options:
        if id(o) in keep:
            o["detail"] = True
            if o["kind"] in ("halfway", "halfway_reg") and o.get("metrics") and o["j"] in cmap:
                o["start_window"] = start_window(cmap[o["j"]])
        else:
            o["detail"] = False
            o["series"] = None
            o["tsd"] = None
    mon = _monitor(n_st, 12)
    monitor = [{"j": j, "name": names[j] if j < len(names) else "", "seq": seqs[j] if j < len(seqs) else j + 1, "terminal": j == n_st - 1} for j in mon]
    gaps = []
    for x in D:                                                       # (nothing to do in continue mode)
        p_, q_ = x - 1, x + 1
        while p_ in Dset:
            p_ -= 1
        while q_ in Dset:
            q_ += 1
        gaps.append(u[q_] - u[p_])
    kk = (dl - d + 1 - m) + wu + wd                                   # regulated trips: those before / between / after the lost ones
    early_ready = [i for i in D if veh == "own" and own_ready[i] <= sd[i] + delay + EPS]
    out.update(options=options, recommended=(options.index(best) if best is not None else None), best_gain=(options.index(bgain) if bgain is not None else None), message=msg, monitor=monitor, baseline=baseline,
               gap_min=_r(max(gaps), 1) if gaps else baseline["gap"], block=Bk, window=win, share=({"slots": kk + m, "trips": kk, "hw": _r(H * (kk + m) / kk, 1), "lost": m} if kk else None),
               span={"up": wu, "dn": wd, "min_side": ms, "short_up": bool(ms and wu < ms), "show_up": show_up, "show_dn": show_dn, "lost": m},
               reg_enough=reg_enough,
               ready_note=("The bus is ready at the first stop before the lost trip's slot, so it can simply run the whole trip: a halfway start is only worth it if that bus cannot be at the first stop in time."
                           if early_ready else None),
               scan={"stops_tested": len({o["j"] for o in options if o["kind"] == "halfway"}), "stops_viable": len({o["j"] for o in options if o["kind"] in ("halfway", "halfway_reg") and o.get("viable") and (o.get("score") or 0) > 0}),
                     "veh": veh, "offsvc_factor": F, "own_ready": _r(own_ready[d], 1) if d in own_ready else None, "detail_kept": sum(1 for o in options if o.get("detail"))})
    baseline["tsd"] = _tsd(TA)
    baseline["dis_path"] = [round(sd[d] + t, 1) for t in tau] if d in sd else None            # where the (first) lost trip WOULD have been (its scheduled first-stop time + running times)
    baseline["dis_paths"] = {str(i): [round(sd[i] + t, 1) for t in tau] for i in D}
    baseline["u"] = {str(i): round(u[i], 1) for i in u}
    return out


# ----------------------------------------------------------------------------- the AI decision (Run AI Optimisation): disrupt + halfway + adjust, or adjust and continue service
def _quality(P, H, bm, W, opt_tsd, hold, mileage_km, buses, cand, n, n_st):
    """How good the resulting service is, on a scale that does not depend on which trips were disrupted (so different decisions can be compared): the headways among the trips around the
    late ones (plus any halfway bus) at every stop - regularity (RMS around the scheduled headway), the largest headway, the share of stops that are within tolerance, bunching - minus
    the cost of holding and of the mileage a halfway bus skips. Same weights as the score (so 'Faster recovery' and 'Minimise mileage' apply)."""
    if not opt_tsd:
        return None
    lo_t, hi_t = max(1, cand[0] - 4), min(n, cand[-1] + 4)
    ids = [k for k in opt_tsd if (k.isdigit() and lo_t <= int(k) <= hi_t) or k.startswith("R")]
    tol = P["recover_tol_pct"] / 100.0
    sq = cnt = stops = bun = ok = 0
    mx = 0.0
    for jj in range(1, n_st):
        ts = sorted(opt_tsd[k][jj] for k in ids if opt_tsd[k][jj] is not None)
        if len(ts) < 2:
            continue
        g = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        stops += 1
        sq += sum((x - H) ** 2 for x in g)
        cnt += len(g)
        mx = max(mx, max(g))
        bun += 1 if min(g) < bm - EPS else 0
        ok += 1 if all(abs(x - H) <= tol * H + EPS for x in g) else 0
    if not stops:
        return None
    rms = math.sqrt(sq / cnt)
    parts = {"regularity": _clip(1.0 - rms / H, 0, 1), "maxgap": _clip(1.0 - max(0.0, mx - H) / H, 0, 1), "recovery": ok / stops,
             "holding": _clip(hold / (max(P["reg_hold_max"], 1.0) * 4.0), 0, 1), "mileage": _clip(mileage_km * buses / max(P["max_mileage_km"], EPS), 0, 1), "bunching": _clip(bun / stops / 0.25, 0, 1)}
    q = 100.0 * (W["regularity"] * parts["regularity"] + W["maxgap"] * parts["maxgap"] + W["recovery"] * parts["recovery"]
                 - W["holding"] * parts["holding"] - W["mileage"] * parts["mileage"] - W["bunching"] * parts["bunching"])
    return {"q": _r(q, 1), "rms": _r(rms, 2), "max": _r(mx, 1), "ok_stops_pct": _r(100.0 * ok / stops, 0), "bunched_stops": bun}


def decide(ctx):
    """What to do about the late trips (Run AI Optimisation). The AI looks at the trips that would leave late and compares, on one common measure of the resulting headways:
      * continue service - nobody is disrupted, the trips around the late ones are held / released (adjusted), or left as they are;
      * disrupt ONE late trip and deploy a halfway bus for it (the other late trips still run their full route and are adjusted around it), at the best stop.
    Two trips together are not disrupted (too heavy). The preference decides what counts: 'Faster headway recovery' = the largest headway, 'Minimise mileage loss' = the km a halfway bus skips.
    Returns the normal simulate() result for the chosen decision plus `ai` {decision, suggest (the trips to tick as Disrupted), candidates, alternatives}."""
    P = {**PARAMS, **(ctx.get("params") or {})}
    probe = simulate({**ctx, "disrupted": [], "block": []})
    if not probe.get("ok"):
        return probe
    H, tau, n_st, bm, n = float(ctx["H"]), ctx["tau"], len(ctx["tau"]), ctx.get("bunch_min", 3.0), int(P["n_trips"])
    pref = ctx.get("pref") if ctx.get("pref") in PREFS else "balanced"
    W = weights(P, pref)
    trips0 = build_trips(P, H, ctx["t0"], ctx.get("late") or [], [])
    own_late = lambda t: max(0.0, t["act_arr"] + P["min_layover_min"] - t["sch_dep"])                # how late THIS bus would leave on its own (not because a bus ahead of it is late)
    cand = sorted(sorted([t["n"] for t in trips0 if 2 <= t["n"] <= n - 1 and own_late(t) >= P["auto_late_min"] - EPS], key=lambda i: -own_late(trips0[i - 1]))[:int(P["max_disrupted"])])
    if not cand:
        probe["ai"] = {"mode": "auto", "decision": "none", "suggest": [], "candidates": [], "alternatives": []}
        probe["message"] = "No trip would leave late: nothing to disrupt or adjust."
        return probe
    sc = ctx.get("search_candidates") or ctx.get("candidates") or []
    entries = []

    def rate(res, o, S):
        km = o.get("skipped_km") or 0.0
        return _quality(P, H, bm, W, o.get("tsd"), o.get("hold_min") or 0.0, km, len(o.get("repl") or []), cand, n, n_st)
    for S in [()] + [(c,) for c in cand]:
        r = simulate({**ctx, "disrupted": list(S), "block": cand, "candidates": sc, "keep": [], "pref": pref})
        if not r.get("ok"):
            continue
        for o in r["options"]:
            if not o.get("detail") or not o.get("metrics") or not o.get("viable") or o.get("ref_only") or not o.get("tsd"):
                continue
            if S and o["kind"] not in ("halfway", "halfway_reg"):          # a disrupted trip must be deployed halfway: cancelling it is not one of the AI's decisions
                continue
            qq = rate(r, o, S)
            if qq:
                entries.append({"S": S, "kind": o["kind"], "label": o["label"], "j": o.get("j"), "name": o.get("name"), "skipped_km": o.get("skipped_km") or 0.0, "hold": o.get("hold_min") or 0.0, **qq})
        if not S and r.get("baseline") and r["baseline"].get("tsd"):
            qq = _quality(P, H, bm, W, r["baseline"]["tsd"], 0.0, 0.0, 0, cand, n, n_st)
            if qq:
                entries.append({"S": (), "kind": "none", "label": "Continue service as it is (no change)", "j": None, "name": None, "skipped_km": 0.0, "hold": 0.0, **qq})
    if not entries:
        probe["ai"] = {"mode": "auto", "decision": "none", "suggest": [], "candidates": cand, "alternatives": []}
        probe["message"] = "No plan improves the headways without creating a problem: continue service as it is."
        return probe
    # Whole-trip operational decision. Do not judge only the first-stop adjustment.
    # Operational priority requested by OCC:
    #   Headway focus: arrival delay >20 min => Halfway first, then adjustment, then regulation.
    #   Mileage focus: arrival delay >30 min => Halfway first, then adjustment, then regulation.
    #                  arrival delay <=30 min => preserve the full trip and regulate 3 before + 3 after as evenly as possible.
    # `late` is the entered/observed arrival delay, not delay created later by regulation.
    max_input_delay = max((float(t.get("late") or 0.0) for t in trips0), default=0.0)
    severe = [c for c in cand if float(trips0[c - 1].get("late") or 0.0) > 20.0 + EPS]
    # HARD company constraints. Only plans below 30 min max headway are eligible when at least
    # one such plan exists. If physics/fleet availability makes <30 impossible, choose the plan
    # with the LOWEST achievable maximum headway first, then quality, and flag the exception.
    max_hw_limit = float(P.get("max_headway_min", 30.0))
    feasible_hw = [e for e in entries if e["max"] < max_hw_limit - EPS]
    constraint_met = bool(feasible_hw)
    decision_pool = feasible_hw if constraint_met else entries
    if not constraint_met:
        min_achievable = min(e["max"] for e in entries)
        decision_pool = [e for e in entries if e["max"] <= min_achievable + EPS]

    cont = [e for e in decision_pool if not e["S"]]
    half = [e for e in decision_pool if e["S"]]
    best_cont = max(cont, key=lambda e: e["q"]) if cont else None
    best_half = max(half, key=lambda e: e["q"]) if half else None

    # A continue/adjust plan is operationally unacceptable when it still leaves a >=2H gap,
    # or needs heavy cumulative holding while a severely late trip exists. In that case the AI
    # must actively test recovery by disrupting ONE trip and starting it halfway.
    cont_bad = bool(best_cont and (best_cont["max"] >= 2.0 * H - EPS or
                    (severe and best_cont["hold"] >= P["reg_hold_max"] * 1.5 - EPS)))

    if pref == "recovery":
        # HEADWAY FOCUS: when observed arrival delay is >20 min, Halfway has first priority.
        # Only fall back to adjustment/regulation when no viable halfway plan exists.
        if max_input_delay > 20.0 + EPS and best_half:
            top = best_half
        elif best_cont:
            top = best_cont
        elif best_half:
            top = best_half
        else:
            top = max(decision_pool, key=lambda e: e["q"])
    elif pref == "mileage":
        # MILEAGE FOCUS: when observed arrival delay is >30 min, Halfway has first priority.
        # At <=30 min preserve mileage: keep the full trip and balance/regulate the 3+3 horizon.
        if max_input_delay > 30.0 + EPS:
            top = best_half or best_cont or max(decision_pool, key=lambda e: e["q"])
        else:
            top = best_cont or best_half or max(decision_pool, key=lambda e: e["q"])
    else:
        # Balanced/company mode keeps the whole-route quality comparison, subject to hard constraints.
        top = max(decision_pool, key=lambda e: (e["q"], -len(e["S"]), -e["skipped_km"], -e["hold"]))
        if severe and best_half and best_cont and (cont_bad or best_half["max"] <= best_cont["max"] - 0.10 * H):
            top = best_half
    S = top["S"]
    final = simulate({**ctx, "disrupted": list(S), "block": cand, "pref": pref})
    if not final.get("ok"):
        return final
    best_i, best_q = None, None
    for k, o in enumerate(final["options"]):
        if not o.get("detail") or not o.get("metrics") or not o.get("viable") or o.get("ref_only") or not o.get("tsd"):
            continue
        if S and o["kind"] not in ("halfway", "halfway_reg"):
            continue
        qq = rate(final, o, S)
        if not qq:
            continue
        o["q"] = qq
        # Keep the final displayed recommendation inside the same hard <30 min rule.
        # When <30 is impossible globally, only accept the best-achievable max headway.
        if constraint_met and qq["max"] >= max_hw_limit - EPS:
            continue
        if not constraint_met and qq["max"] > top["max"] + EPS:
            continue
        if best_q is None or (round(qq["q"], 1), -(o.get("skipped_km") or 0.0)) > best_q:
            best_i, best_q = k, (round(qq["q"], 1), -(o.get("skipped_km") or 0.0))
    if top["kind"] == "none" and final.get("baseline") and final["baseline"].get("tsd"):
        q0 = _quality(P, H, bm, W, final["baseline"]["tsd"], 0.0, 0.0, 0, cand, n, n_st)
        if q0 and (best_q is None or q0["q"] >= best_q[0]):
            best_i = None                                                   # nothing beats leaving the service as it is
    final["recommended"] = best_i
    by_S = {}
    for e in entries:
        cur = by_S.get(e["S"])
        if cur is None or e["q"] > cur["q"]:
            by_S[e["S"]] = e
    alts = [{"suggest": list(e["S"]), "kind": e["kind"], "label": e["label"], "stop": e["name"], "q": e["q"], "max_hw": e["max"], "rms": e["rms"], "ok_stops_pct": e["ok_stops_pct"], "skipped_km": _r(e["skipped_km"], 1),
             "hold_min": _r(e["hold"], 1), "chosen": e["S"] == S and e["kind"] == top["kind"]} for e in sorted(by_S.values(), key=lambda e: -e["q"])]
    decision = "halfway" if S else ("adjust" if best_i is not None else "none")
    final["ai"] = {"mode": "auto", "decision": decision, "suggest": list(S), "candidates": cand, "alternatives": alts, "pref": pref,
                   "constraints": {"max_adjustment_min": 8, "min_layover_min": 7, "halfway_layover_omitted": True,
                                   "min_horizon": "3 UP + 3 DOWN", "departed_locked": True, "headway_lt_min": max_hw_limit},
                   "headway_constraint_met": constraint_met, "best_achievable_max_headway": top["max"],
                   "observed_max_delay_min": _r(max_input_delay, 1),
                   "priority_rule": ("delay>20: halfway>adjustment>regulate" if pref == "recovery" else
                                     "delay>30: halfway>adjustment>regulate; delay<=30: regulate 3+3" if pref == "mileage" else
                                     "balanced whole-route optimisation")}
    base_msg = ("Recommended - the AI ticks trip %d as Disrupted (halfway) and adjusts the trips around it" % S[0] if S else
                ("Recommended - adjust the trips and continue service (no disruption)" if best_i is not None else "Recommended - continue service as it is: no plan beats it"))
    final["message"] = base_msg if constraint_met else (f"WARNING: <{max_hw_limit:g} min headway is not achievable with the available trips. "
                                                         f"Best achievable maximum headway is {top['max']:.1f} min. " + base_msg)
    return final


def _monitor(n, k):
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
