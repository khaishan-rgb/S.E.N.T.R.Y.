"""Halfway Deployment Simulator - engine V2 (pure stdlib, no I/O, unit-testable).

Question: one trip of a scheduled sequence is lost (Disrupted). Does starting a replacement trip from an approved halfway stop recover headway,
and where along the route?

It does NOT identify a physical bus: LTA DataMall has no schedule, duty or trip information, so bus-to-schedule matching is unreliable. Instead the
first bus stop's scheduled timing is the common reference and the SAME sequence of trips is simulated twice:

  Scenario A - No halfway : the disrupted trip stays missing; the remaining trips depart the first stop and run downstream.
  Scenario B - Halfway    : the same, plus a replacement trip that starts at the halfway stop (at the disrupted trip's scheduled time there,
                            + an optional start delay) and runs to the terminal.

First-stop table (per trip): scheduled arrival, actual/simulated arrival (scheduled + the lateness the controller enters), lateness, the next trip's
scheduled departure and its actual/simulated departure, departure headway.
  actual departure = max(scheduled departure, actual arrival + minimum layover, previous departure)   (a trip cannot leave before the one ahead)
  the Disrupted trip has no departure.

Downstream: every trip takes the same running time between stops (from the LTA speed-band traffic model), optionally stretched or shrunk by
`load_sens` (0 = headways propagate unchanged; > 0 = a bus behind a big gap dwells longer and slows, one behind a short gap speeds up).

"Affected headways" at a stop = the headway(s) around the missing slot: A - the one gap between the trip before and the trip after the disrupted
one; B - the two gaps either side of the replacement. Compared over the same stops (from the halfway stop to the terminal), so the comparison is fair.
All figures are simulated from the entered scenario; EWT is a simulated waiting-time penalty, not LTA's.
"""
import math

MODEL_VERSION = "halfway-2.0"
EPS = 1e-6

PARAMS = {
    "n_trips": 10,               # trips in the simulated sequence
    "layover_min": 10.0,         # scheduled layover at the first stop (scheduled arrival = scheduled departure - this)
    "min_layover_min": 2.0,      # a trip cannot depart earlier than its actual arrival + this
    "start_delay_min": 0.0,      # replacement passes the halfway stop this many min after the disrupted trip's scheduled time there
    "min_remaining_pct": 20.0,   # a halfway stop is only recommended if at least this % of the route remains after it
    "min_improve_pct": 10.0,     # ... and only if the average affected headway improves by at least this %
    "load_sens": 0.0,            # headway propagation: 0 = identical running times; e.g. 0.05 = 5% slower per headway-of-slack ahead (see docstring)
    "fallback_kmh": 20.0,        # running speed if no traffic model is available
}


def _r(x, n=1):
    return None if x is None else round(x, n)


def _mean(v):
    return sum(v) / len(v) if v else None


# ----------------------------------------------------------------------------- first-stop table
def build_trips(P, H, t0, late, disrupted):
    """t0 = scheduled departure of trip 1 (minutes since midnight). late[i] = lateness of trip i+1's arrival (min). disrupted = 1-based trip number or None."""
    n = int(P["n_trips"])
    trips, prev_dep, prev_no = [], None, None
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


# ----------------------------------------------------------------------------- downstream propagation
def propagate(deps, tau, H, beta, join=None):
    """deps: [(id, departure time at the first stop)]; tau[j]: running minutes from the first stop to stop j (tau[0] = 0); join: {"id","j","t"} a trip that
    starts at stop j at time t. Returns {id: [time at each stop, or None before the trip starts]}. Trips cannot overtake."""
    n = len(tau)
    T = {tid: [None] * n for tid, _ in deps}
    for tid, d in deps:
        T[tid][0] = d + tau[0]
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
    """Affected gaps (around the missing slot) and all consecutive gaps among the active trips at stop j."""
    active = [T[t][j] for t in T if T[t][j] is not None]
    aff = _gaps([T[t][j] for t in aff_ids if t in T and T[t][j] is not None])
    return aff, _gaps(active)


def _ewt(gaps, H):
    s = sum(gaps)
    return (sum(g * g for g in gaps) / (2 * s) - H / 2.0) if s > 0 else 0.0


def _metrics(recs, H, bm, key):
    """recs: stop records over the comparison section. key 'a' or 'b'."""
    pooled = [g for r in recs for g in r[key]]
    worst = [max(r[key]) for r in recs]
    allmin = [min(r[key + "_all"]) if r[key + "_all"] else None for r in recs]
    return {"avg": _r(_mean(pooled), 2), "max": _r(max(pooled), 2), "min": _r(min(pooled), 2),
            "ewt": _r(_mean([_ewt(r[key], H) for r in recs]), 3),
            "pct_ge_1_5": _r(100.0 * sum(1 for w in worst if w >= 1.5 * H - EPS) / len(worst), 1),
            "pct_ge_2": _r(100.0 * sum(1 for w in worst if w >= 2.0 * H - EPS) / len(worst), 1),
            "bunched_stops": sum(1 for m in allmin if m is not None and m < bm - EPS), "min_gap": _r(min(m for m in allmin if m is not None), 2) if any(m is not None for m in allmin) else None}


# ----------------------------------------------------------------------------- main entry
def simulate(ctx):
    """ctx: H, t0, late[], disrupted (1-based or None), tau[] (running min from the first stop to each stop), stop_s[] (km), route_km, stop_names[], stop_seq[],
    params, bunch_min, candidates [{j, code, name, seq, approved}]."""
    P = {**PARAMS, **(ctx.get("params") or {})}
    if not ctx.get("H") or ctx["H"] <= 0:
        return {"ok": False, "error": "Scheduled headway unknown for this service - enter it in the Headway box, or add it in Bunching & Gap -> Settings -> Service Headway Master."}
    H, tau, n_st = float(ctx["H"]), ctx["tau"], len(ctx["tau"])
    bm = ctx.get("bunch_min", 3.0)
    n = int(P["n_trips"])
    d = ctx.get("disrupted")
    trips = build_trips(P, H, ctx["t0"], ctx.get("late") or [], d)
    out = {"ok": True, "model": MODEL_VERSION, "sched_hw": H, "trips": trips, "disrupted": d, "n_trips": n, "bunch_min": bm, "params": P}
    if d is None:
        out.update(candidates=[], recommended=None, message="Mark one trip as Disrupted to simulate losing it.", baseline=None, monitor=[])
        return out
    if not (2 <= d <= n - 1):
        return {"ok": False, "error": f"The disrupted trip must have a trip before and after it: choose a trip between 2 and {n - 1}."}
    deps = [(t["n"], t["act_dep"]) for t in trips if not t["disrupted"]]
    dis = trips[d - 1]
    P_id, N_id = d - 1, d + 1
    TA = propagate(deps, tau, H, P["load_sens"])
    # baseline series (Scenario A) for every stop
    base = []
    for j in range(n_st):
        aff, alls = _stop_rec(TA, j, (P_id, N_id))
        base.append({"j": j, "a": aff, "a_all": alls})
    cands = []
    for c in ctx.get("candidates") or []:
        j = c["j"]
        res = {"j": j, "code": c.get("code"), "name": c.get("name"), "seq": c.get("seq"), "approved": c.get("approved", True), "violations": [], "warnings": []}
        if j < 1 or j >= n_st - 1:
            res["violations"].append("A halfway stop must be after the first stop and before the terminal")
            res.update(viable=False, metrics_a=None, metrics_b=None, improvement=None, series=[], start_time=None)
            cands.append(res)
            continue
        r_time = dis["sch_dep"] + tau[j] + P["start_delay_min"]
        TB = propagate(deps, tau, H, P["load_sens"], {"id": "R", "j": j, "t": r_time})
        recs = []
        for jj in range(n_st):
            aff, alls = _stop_rec(TB, jj, (P_id, N_id, "R"))
            recs.append({"j": jj, "b": aff, "b_all": alls})
        sect = [(base[jj], recs[jj]) for jj in range(j, n_st)]
        ma = _metrics([{**s, "a": s["a"], "a_all": s["a_all"]} for s, _ in sect], H, bm, "a")
        mb = _metrics([{**b_, "b": b_["b"], "b_all": b_["b_all"]} for _, b_ in sect], H, bm, "b")
        imp_min = ma["avg"] - mb["avg"]
        imp_pct = 100.0 * imp_min / ma["avg"] if ma["avg"] else 0.0
        remain = 100.0 * (ctx["route_km"] - ctx["stop_s"][j]) / ctx["route_km"] if ctx.get("route_km") else 0.0
        if remain < P["min_remaining_pct"] - EPS:
            res["violations"].append(f"Only {remain:.0f}% of the route remains after this stop (minimum {P['min_remaining_pct']:.0f}%)")
        if mb["bunched_stops"] > ma["bunched_stops"]:
            first = next(jj for jj in range(j, n_st) if recs[jj]["b_all"] and min(recs[jj]["b_all"]) < bm - EPS)
            res["violations"].append(f"Creates new bunching downstream ({mb['min_gap']:.1f} min headway from stop {ctx['stop_seq'][first] if first < len(ctx.get('stop_seq') or []) else first + 1})")
        if imp_pct < P["min_improve_pct"] - EPS:
            res["violations"].append(f"Average affected headway improves only {imp_pct:.0f}% (minimum {P['min_improve_pct']:.0f}%)")
        if P["start_delay_min"] and abs(P["start_delay_min"]) > 0:
            res["warnings"].append(f"Replacement is assumed {P['start_delay_min']:+.0f} min against the disrupted trip's scheduled time at this stop")
        if not res["approved"]:
            res["warnings"].append("Not on the approved halfway list: what-if only")
        if j > 0:
            res["warnings"].append(f"The first {j} stops (and {ctx['stop_s'][j]:.1f} km) stay without this trip in both scenarios")
        series = [{"j": jj, "wa": _r(max(base[jj]["a"]), 1) if base[jj]["a"] else None, "wb": _r(max(recs[jj]["b"]), 1) if recs[jj]["b"] else None,
                   "a": [_r(x, 1) for x in base[jj]["a"]], "b": [_r(x, 1) for x in recs[jj]["b"]],
                   "imp": _r(max(base[jj]["a"]) - max(recs[jj]["b"]), 1) if (base[jj]["a"] and recs[jj]["b"]) else None,
                   "min_b": _r(min(recs[jj]["b_all"]), 1) if recs[jj]["b_all"] else None} for jj in range(n_st)]
        res.update(start_time=r_time, sch_time=dis["sch_dep"] + tau[j], skipped_stops=j, skipped_km=_r(ctx["stop_s"][j], 2), restored_stops=n_st - j, restored_km=_r(ctx["route_km"] - ctx["stop_s"][j], 2),
                   remain_pct=_r(remain, 1), metrics_a=ma, metrics_b=mb, series=series, viable=not res["violations"],
                   improvement={"avg_min": _r(imp_min, 2), "avg_pct": _r(imp_pct, 1), "max_min": _r(ma["max"] - mb["max"], 2), "ewt": _r(ma["ewt"] - mb["ewt"], 3)})
        cands.append(res)
    ok = [c for c in cands if c.get("viable")]
    best = max(ok, key=lambda c: (c["improvement"]["avg_pct"], -c["j"])) if ok else None
    for k, c in enumerate(sorted([c for c in cands if c.get("improvement")], key=lambda c: (-c["improvement"]["avg_pct"], c["j"])), 1):
        c["rank"] = k
    if best is not None:
        msg = "Best simulated option: largest headway improvement without creating bunching"
    elif not cands:
        msg = "No approved halfway points are configured for this service and direction."
    else:
        msg = "No tested halfway stop gives a worthwhile improvement without a problem."
    mon = _monitor(n_st, 12)
    names, seqs = ctx.get("stop_names") or [], ctx.get("stop_seq") or []
    monitor = [{"j": j, "name": names[j] if j < len(names) else "", "seq": seqs[j] if j < len(seqs) else j + 1, "terminal": j == n_st - 1} for j in mon]
    out.update(candidates=cands, recommended=(cands.index(best) if best is not None else None), message=msg, monitor=monitor,
               baseline={"series": [{"j": b["j"], "wa": _r(max(b["a"]), 1) if b["a"] else None, "a": [_r(x, 1) for x in b["a"]], "min_all": _r(min(b["a_all"]), 1) if b["a_all"] else None} for b in base],
                         "gap": _r(max(base[0]["a"]), 1) if base[0]["a"] else None},
               gap_min=_r(N_id and (trips[N_id - 1]["act_dep"] - trips[P_id - 1]["act_dep"]), 1))
    return out


def _monitor(n, k):
    """Evenly spaced key stops (always incl. the first and the terminal)."""
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
