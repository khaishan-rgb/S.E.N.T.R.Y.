"""AI Halfway Optimiser engine - V1 (pure stdlib, no I/O, unit-testable).

Question: a bus is severely late. Continue the full trip, or take it off service, run it to an APPROVED halfway point and start its next
service from there? Every approved point is simulated and compared with "No halfway / continue service"; nothing is hard-coded.

What is simulated (all times are minutes from now, from LTA arrival estimates + the traffic model - never actual timetables):
  * buses around the delayed bus X:  A = the bus ahead of X, B = the bus behind X (only these are affected by removing / re-inserting X)
  * No halfway  : at every downstream stop the arrival times of A, X, B  ->  the local headways A->X and X->B
  * Halfway at stop j_h : X leaves service now, reaches j_h after the off-service travel time (+ preparation buffer), and from there runs the same
    segment times X would have had.  Before j_h the slot is empty (headway A->B: the stops X would have served lose that bus);
    from j_h the headways are A->H and H->B (H = the halfway bus, re-sorted by time, so it can also land behind B).
  * Headway metrics (avg / max / min / EWT ...) are measured from the halfway stop to the terminal, and "no halfway" is recomputed over the SAME
    stops, so the comparison is fair. The cost of skipping the section between X and j_h is shown explicitly (stops skipped, mileage loss,
    off-service time) and inside the recovery time, which is measured over the whole section (a skipped section stays irregular).

"Headway at a stop" = the LARGEST local headway around the slot (the worst wait a passenger can face there). Minimum headway and the bunching
share use ALL local headways, so a halfway that fixes one long gap but creates bunching is caught.

Data limits: LTA has no timetable, no registration numbers, no layover; lateness is estimated as (gap to the bus ahead - scheduled headway).
"""
import math

MODEL_VERSION = "halfway-1.0"
EPS = 1e-6

PARAMS = {
    # ---- eligibility / constraints
    "min_late": 15.0,             # bus must be at least this late (min) before halfway is considered
    "min_remaining_pct": 20.0,    # % of the route that must remain after the halfway point
    "max_offservice_min": 25.0,   # max off-service travel time
    "max_mileage_km": 15.0,       # max skipped revenue-service km
    "min_improve_pct": 10.0,      # a point is only recommended if average HW improves by at least this %
    "poor_recovery_min": 30.0,    # halfway is only considered if doing nothing leaves the service irregular for longer than this (or never recovers)
    "prep_buffer_min": 2.0,       # added to the off-service travel time before the bus can start the halfway trip
    "extra_layover_min": 0.0,     # any extra recovery / layover required at the halfway point
    # ---- recovery criterion
    "recover_tol_pct": 20.0,      # recovered = every local headway within +/- this % of scheduled
    "recover_points": 3,          # ... at this many consecutive monitoring points
    "monitor_points": 12,         # monitoring stops spread over the downstream section (heat-map columns)
    "fallback_kmh": 20.0,         # speed used where no traffic model exists (back-estimating the bus ahead, etc.)
    # ---- score weights (Balanced), %
    "w_regularity": 30.0, "w_maxgap": 25.0, "w_recovery": 20.0, "w_offservice": 10.0, "w_mileage": 10.0, "w_bunching": 5.0,
    "pref_recovery_boost": 1.6,   # "Faster headway recovery" multiplies the recovery and max-gap weights by this
    "pref_mileage_boost": 3.0,    # "Minimise mileage loss" multiplies the mileage weight by this
}
PREFS = ("balanced", "recovery", "mileage")


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _r(x, n=1):
    return None if x is None else round(x, n)


def _mean(v):
    return sum(v) / len(v) if v else None


def _std(v):
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))


def weights(P, pref):
    """Score weights (fractions summing to 1) for the chosen optimisation preference."""
    w = {"regularity": P["w_regularity"], "maxgap": P["w_maxgap"], "recovery": P["w_recovery"], "offservice": P["w_offservice"], "mileage": P["w_mileage"], "bunching": P["w_bunching"]}
    if pref == "recovery":
        w["recovery"] *= P["pref_recovery_boost"]
        w["maxgap"] *= P["pref_recovery_boost"]
    elif pref == "mileage":
        w["mileage"] *= P["pref_mileage_boost"]
    tot = sum(w.values()) or 1.0
    return {k: v / tot for k, v in w.items()}


# ----------------------------------------------------------------------------- arrival times at every stop
def _travel(ctx, a_km, b_km):
    tm = ctx.get("tm")
    if tm is not None:
        return tm.t(a_km, b_km)
    return abs(b_km - a_km) / max(1.0, ctx["P"]["fallback_kmh"]) * 60.0


def bus_time(ctx, bus, j):
    """Minutes from now at which `bus` is at stop j: LTA-based estimate ahead of it; for a stop it has already passed, a back-estimate (negative)
    from the traffic model (LTA keeps no history of when a bus passed a stop)."""
    vec = bus["vec"]
    v = vec[j] if j < len(vec) else None
    if v is not None:
        return v
    if j < bus["j0"]:
        return -_travel(ctx, ctx["stop_s"][j], bus["s_km"])
    return None


def _intervals(parts):
    """[(time, name)] -> sorted list of consecutive headways [(from, to, hw)]."""
    parts = sorted(parts)
    return [(parts[i][1], parts[i + 1][1], max(0.0, parts[i + 1][0] - parts[i][0])) for i in range(len(parts) - 1)]


# ----------------------------------------------------------------------------- series + metrics
def _series(ctx, halfway=None):
    """Stop-by-stop local headways for the whole comparison window. halfway = None (continue) or {"j": stop index, "ready": minutes from now}."""
    X, A, B, n = ctx["x_bus"], ctx["a_bus"], ctx["b_bus"], len(ctx["stop_s"])
    H = ctx["H"]
    out = []
    for j in range(X["j0"], n):
        tx, ta = bus_time(ctx, X, j), bus_time(ctx, A, j)
        if tx is None or ta is None:
            continue
        tb = bus_time(ctx, B, j) if B is not None else None
        assumed = tb is None
        if tb is None:
            tb = tx + H                                                       # nobody behind: assume the next bus is one scheduled headway behind X
        parts = [(ta, "A"), (tb, "B")]
        slot = None
        if halfway is None:
            parts.append((tx, "X"))
            slot = tx
        elif j >= halfway["j"]:
            th = halfway["ready"] + (tx - bus_time(ctx, X, halfway["j"]))      # H drives the segment times X would have had from the halfway stop on
            parts.append((th, "H"))
            slot = th
        ints = _intervals(parts)
        if not ints:
            continue
        hws = [h for _, _, h in ints]
        slot_i = next((k for k, (f, t, h) in enumerate(ints) if t == ("X" if halfway is None else "H")), None)
        front = next((h for f, t, h in ints if t == ("X" if halfway is None else "H")), None) if slot is not None else None
        rear = next((h for f, t, h in ints if f == ("X" if halfway is None else "H")), None) if slot is not None else None
        out.append({"j": j, "t": slot if slot is not None else (ta + tb) / 2.0, "worst": max(hws), "ints": hws, "front": front, "rear": rear,
                    "gap": (tb - ta) if slot is None else None, "assumed_b": assumed, "tA": ta, "tB": tb})
    return out


def monitor_stops(series, k):
    """Evenly spaced monitoring points over the window (always including the first and last stop)."""
    idx = list(range(len(series)))
    if len(idx) <= k:
        return idx
    return sorted({round(i * (len(idx) - 1) / (k - 1)) for i in range(k)})


def recovery_min(series, mon, H, P):
    """First time (min from now) at which every local headway is within +/- tol of scheduled at `recover_points` consecutive monitoring points."""
    lo, hi, need = H * (1 - P["recover_tol_pct"] / 100.0), H * (1 + P["recover_tol_pct"] / 100.0), int(P["recover_points"])
    ok = [all(lo - EPS <= h <= hi + EPS for h in series[i]["ints"]) for i in mon]
    for k in range(0, len(ok) - need + 1):
        if all(ok[k:k + need]):
            return series[mon[k]]["t"]
    return None


def metrics(series, H, P, bunch_min):
    worst = [s["worst"] for s in series]
    allh = [h for s in series for h in s["ints"]]
    mon = monitor_stops(series, int(P["monitor_points"]))
    sum_h = sum(allh)
    awt = sum(h * h for h in allh) / (2 * sum_h) if sum_h > 0 else 0.0
    swt = H / 2.0                                                              # sum(S^2) / (2 sum(S)) with a constant scheduled headway S
    return {
        "avg": _r(_mean(worst), 2), "max": _r(max(worst), 2), "min": _r(min(allh), 2), "std": _r(_std(worst), 2),
        "pct_ge_1_5": _r(100.0 * sum(1 for w in worst if w >= 1.5 * H - EPS) / len(worst), 1),
        "pct_ge_2": _r(100.0 * sum(1 for w in worst if w >= 2.0 * H - EPS) / len(worst), 1),
        "pct_le_half": _r(100.0 * sum(1 for h in allh if h <= 0.5 * H + EPS) / len(allh), 1),
        "pct_bunched": _r(100.0 * sum(1 for h in allh if h < bunch_min - EPS) / len(allh), 1),
        "rms_dev": _r(math.sqrt(sum((h - H) ** 2 for h in allh) / len(allh)), 3),
        "awt": _r(awt, 3), "swt": _r(swt, 3), "ewt": _r(awt - swt, 3),
        "recovery": _r(recovery_min(series, mon, H, P), 1),
        "horizon": _r(series[-1]["t"], 1),
    }


# ----------------------------------------------------------------------------- one candidate
def _candidate(ctx, base, cand, w, manual_ready=None):
    P, H, X, A = ctx["P"], ctx["H"], ctx["x_bus"], ctx["a_bus"]
    stop_s, route_km, bm = ctx["stop_s"], ctx["route_km"], ctx["bunch_min"]
    j = cand["j"]
    lim = {k: (cand.get(k) if cand.get(k) is not None else P[k]) for k in ("min_late", "min_remaining_pct", "max_offservice_min", "max_mileage_km")}
    off_min = cand["off_min"]
    ready = manual_ready if manual_ready is not None else off_min + P["prep_buffer_min"] + P["extra_layover_min"]
    lost_km = max(0.0, stop_s[j] - X["s_km"])
    remain_pct = 100.0 * (route_km - stop_s[j]) / route_km if route_km > 0 else 0.0
    res = {"j": j, "code": cand.get("code"), "name": cand.get("name"), "seq": cand.get("seq"), "off_min": _r(off_min, 1), "off_km": _r(cand.get("off_km"), 2),
           "off_source": cand.get("off_source"), "ready_min": _r(ready, 1), "lost_km": _r(lost_km, 2), "lost_pct": _r(100.0 * lost_km / route_km if route_km else 0, 1),
           "remain_pct": _r(remain_pct, 1), "manual_start": manual_ready is not None, "limits": lim}
    viol, warn = [], []
    tx_j = bus_time(ctx, X, j)
    if j < X["j0"] or tx_j is None:
        viol.append("The bus has already passed this point")
        res.update(violations=viol, warnings=warn, viable=False, series=[], metrics=None, no_metrics=None, full=None, improvement=None, score=None, parts=None)
        return res
    if off_min > lim["max_offservice_min"] + EPS:
        viol.append(f"Off-service travel {off_min:.0f} min is over the {lim['max_offservice_min']:.0f} min limit")
    if lost_km > lim["max_mileage_km"] + EPS:
        viol.append(f"Mileage loss {lost_km:.1f} km is over the {lim['max_mileage_km']:.1f} km limit")
    if remain_pct < lim["min_remaining_pct"] - EPS:
        viol.append(f"Only {remain_pct:.0f}% of the route remains after this point (minimum {lim['min_remaining_pct']:.0f}%)")
    if ready >= tx_j - EPS:
        viol.append(f"Bus would start here at +{ready:.0f} min - no earlier than staying in service (+{tx_j:.0f} min)")
    ta_j = bus_time(ctx, A, j)
    if ta_j is not None and ready < ta_j - EPS:
        viol.append("Bus would start ahead of the bus in front of it")
    ser = _series(ctx, {"j": j, "ready": ready})
    if not ser:
        viol.append("No downstream data to simulate")
        res.update(violations=viol, warnings=warn, viable=False, series=[], metrics=None, no_metrics=None, full=None, improvement=None, score=None, parts=None)
        return res
    m_full = metrics(ser, H, P, bm)
    down_h = [s for s in ser if s["j"] >= j]
    down_b = [s for s in base["series"] if s["j"] >= j]
    if not down_h or not down_b:
        viol.append("No downstream data after this point")
        res.update(violations=viol, warnings=warn, viable=False, series=[], metrics=None, no_metrics=None, full=None, improvement=None, score=None, parts=None)
        return res
    m = metrics(down_h, H, P, bm)                                        # halfway, from the halfway stop to the terminal
    mb = metrics(down_b, H, P, bm)                                       # no halfway, over the SAME stops
    res["skipped_stops"] = j - X["j0"]
    first = next((s for s in ser if s["j"] == j), None)
    if first is not None and first["front"] is not None and first["rear"] is not None and min(first["front"], first["rear"]) < bm - EPS:
        viol.append(f"Creates bunching at the halfway point ({min(first['front'], first['rear']):.1f} min headway, below {bm:g} min)")
    bm0 = base["metrics"]                                                # no halfway over the whole section (recovery, horizon)
    imp_min = mb["avg"] - m["avg"]
    imp_pct = 100.0 * imp_min / mb["avg"] if mb["avg"] else 0.0
    horizon = max(bm0["horizon"] or 0.0, m_full["horizon"] or 0.0)
    rec_no, rec_h = (bm0["recovery"] if bm0["recovery"] is not None else horizon), (m_full["recovery"] if m_full["recovery"] is not None else horizon)
    rec_saved = (bm0["recovery"] - m_full["recovery"]) if (bm0["recovery"] is not None and m_full["recovery"] is not None) else None
    if m["pct_bunched"] > mb["pct_bunched"] + EPS:
        warn.append(f"Bunched headways rise from {mb['pct_bunched']:.0f}% to {m['pct_bunched']:.0f}% of the section after this point")
    if m_full["recovery"] is None:
        warn.append("Headway does not return to +/-%d%% of scheduled before the terminal" % int(P["recover_tol_pct"]))
    if res["skipped_stops"] > 0:
        warn.append(f"{res['skipped_stops']} stops between the bus and this point lose bus X (headway there becomes the gap between bus A and bus B)")
    # ---- score: benefits (-1..1) minus penalties (0..1), weighted.
    # Regularity is stop-weighted and normalised by the WHOLE section's baseline, so a point that fixes more stops earns more (not just a bigger % on a short tail).
    def dev2(series):
        return sum((h - H) ** 2 for s in series for h in s["ints"])
    parts = {
        "regularity": _clip((dev2(down_b) - dev2(down_h)) / max(dev2(base["series"]), EPS), -1, 1),
        "maxgap": _clip((mb["max"] - m["max"]) / max(bm0["max"], EPS), -1, 1),
        "recovery": _clip((rec_no - rec_h) / max(rec_no, EPS), -1, 1),
        "offservice": _clip(off_min / max(lim["max_offservice_min"], EPS), 0, 1),
        "mileage": _clip(lost_km / max(lim["max_mileage_km"], EPS), 0, 1),
        "bunching": _clip((m["pct_bunched"] - mb["pct_bunched"]) / 25.0, 0, 1),
    }
    score = 100.0 * (w["regularity"] * parts["regularity"] + w["maxgap"] * parts["maxgap"] + w["recovery"] * parts["recovery"]
                     - w["offservice"] * parts["offservice"] - w["mileage"] * parts["mileage"] - w["bunching"] * parts["bunching"])
    res.update(violations=viol, warnings=warn, series=ser, metrics=m, no_metrics=mb, full=m_full, score=_r(score, 1), parts={k: _r(v, 3) for k, v in parts.items()},
               improvement={"avg_min": _r(imp_min, 2), "avg_pct": _r(imp_pct, 1), "max_min": _r(mb["max"] - m["max"], 2), "recovery_saved": _r(rec_saved, 1),
                            "ewt": _r(mb["ewt"] - m["ewt"], 3)},
               viable=not viol)
    return res


# ----------------------------------------------------------------------------- lateness
def lateness(ctx, bus_i):
    """Estimated lateness of bus i = its gap to the bus ahead at its next stop minus the scheduled headway (LTA has no timetable). None if it is the first bus."""
    if bus_i <= 0:
        return None
    X, A = ctx["buses"][bus_i], ctx["buses"][bus_i - 1]
    j = X["j0"]
    tx, ta = bus_time(ctx, X, j), bus_time(ctx, A, j)
    if tx is None or ta is None:
        return None
    return {"front_hw": tx - ta, "late": max(0.0, tx - ta - ctx["H"])}


# ----------------------------------------------------------------------------- main entry
def optimise(ctx):
    """ctx: H, stop_s, route_km, buses (front -> back: id, s_km, vec, j0), x (index of the delayed bus), tm, params, pref, candidates
    [{j, code, name, seq, off_min, off_km, off_source, min_late?, min_remaining_pct?, max_offservice_min?, max_mileage_km?}], bunch_min,
    manual: {j, ready?} to simulate ONE chosen point (controller override)."""
    P = {**PARAMS, **(ctx.get("params") or {})}
    ctx = {**ctx, "P": P, "bunch_min": ctx.get("bunch_min", 3.0)}
    H, buses, xi = ctx["H"], ctx["buses"], ctx["x"]
    pref = ctx.get("pref") if ctx.get("pref") in PREFS else "balanced"
    if not H:
        return {"ok": False, "error": "Scheduled headway unknown for this service - add it in Settings -> Service Headway Master."}
    if xi is None or not (0 <= xi < len(buses)):
        return {"ok": False, "error": "Choose a delayed bus."}
    if xi == 0:
        return {"ok": False, "error": "This is the first bus on the route, so there is no bus ahead to measure a gap against."}
    ctx["x_bus"], ctx["a_bus"], ctx["b_bus"] = buses[xi], buses[xi - 1], (buses[xi + 1] if xi + 1 < len(buses) else None)
    late = lateness(ctx, xi)
    if late is None:
        return {"ok": False, "error": "Not enough arrival estimates around this bus to measure its gap."}
    base_series = _series(ctx, None)
    if not base_series:
        return {"ok": False, "error": "No downstream arrival estimates for this bus."}
    base = {"series": base_series, "metrics": metrics(base_series, H, P, ctx["bunch_min"])}
    w = weights(P, pref)
    man = ctx.get("manual")
    cands = [_candidate(ctx, base, c, w, (man.get("ready") if man and man.get("j") == c["j"] else None)) for c in ctx["candidates"]]
    # ---- gate: is halfway worth considering at all?
    reasons = []
    if late["late"] < P["min_late"] - EPS:
        reasons.append(f"Estimated lateness {late['late']:.0f} min is below the {P['min_late']:.0f} min needed for halfway")
    rec0 = base["metrics"]["recovery"]
    if rec0 is not None and rec0 <= P["poor_recovery_min"] + EPS:
        reasons.append(f"Without action the headway is predicted to recover in {rec0:.0f} min (halfway only considered above {P['poor_recovery_min']:.0f} min)")
    gate = {"late": _r(late["late"], 1), "front_hw": _r(late["front_hw"], 1), "eligible": not reasons, "reasons": reasons}
    # ---- pick
    ok = [c for c in cands if c["viable"] and c["metrics"] and c["improvement"]["avg_pct"] >= P["min_improve_pct"] - EPS and c["score"] is not None and c["score"] > 0]
    for c in cands:
        if c["viable"] and c["metrics"] and c["improvement"]["avg_pct"] < P["min_improve_pct"] - EPS:
            c["violations"] = c["violations"] + [f"Average headway improves only {c['improvement']['avg_pct']:.0f}% (minimum {P['min_improve_pct']:.0f}%)"]
            c["viable"] = False
        elif c["viable"] and c["score"] is not None and c["score"] <= 0:
            c["violations"] = c["violations"] + ["Overall score is not better than continuing the trip"]
            c["viable"] = False
    best = max(ok, key=lambda c: c["score"]) if (ok and gate["eligible"]) else None
    ranked = sorted([c for c in cands if c["score"] is not None], key=lambda c: -c["score"])
    for k, c in enumerate(ranked, 1):
        c["rank"] = k
    if best is not None:
        msg = "Recommended - highest simulation score"
    elif not gate["eligible"]:
        msg = "Halfway is not recommended: " + "; ".join(reasons)
    elif not cands:
        msg = "No approved halfway points are configured for this service and direction."
    else:
        msg = "No approved halfway point beats continuing the trip."
    mon = monitor_stops(base_series, int(P["monitor_points"]))
    names = ctx.get("stop_names") or []
    seqs = ctx.get("stop_seq") or []
    monitor = [{"j": base_series[i]["j"], "name": names[base_series[i]["j"]] if base_series[i]["j"] < len(names) else "", "seq": seqs[base_series[i]["j"]] if base_series[i]["j"] < len(seqs) else base_series[i]["j"] + 1,
                "terminal": base_series[i]["j"] == len(ctx["stop_s"]) - 1} for i in mon]
    for s in base_series:
        s["t"], s["worst"] = _r(s["t"], 1), _r(s["worst"], 1)
    return {"ok": True, "model": MODEL_VERSION, "pref": pref, "weights": {k: _r(v, 3) for k, v in w.items()}, "sched_hw": H, "bunch_min": ctx["bunch_min"],
            "gate": gate, "baseline": {"metrics": base["metrics"], "series": [_slim(s) for s in base_series]},
            "candidates": [dict(c, series=[_slim(s) for s in c["series"]]) for c in cands], "recommended": (cands.index(best) if best is not None else None),
            "message": msg, "monitor": monitor, "window": {"from_j": base_series[0]["j"], "to_j": base_series[-1]["j"]},
            "assumed_follower": any(s.get("assumed_b") for s in base_series)}


def _slim(s):
    return {"j": s["j"], "t": _r(s["t"], 1), "worst": _r(s["worst"], 1), "front": _r(s["front"], 1), "rear": _r(s["rear"], 1), "gap": _r(s.get("gap"), 1),
            "min": _r(min(s["ints"]), 1)}
