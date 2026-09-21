"""Bus Bunching & Headway Gap engine - V1 (pure stdlib, no I/O, unit-testable).

Track every consecutive bus -> calculate spacing at every downstream stop -> detect convergence / divergence ->
measure persistence -> classify 2BB / 3BB / 4BB+ as GROUPS (never as duplicate pairs) -> rank services.

Data limits (LTA DataMall only): there is no bus registration, trip id, SCS record or layover. Buses get stable ids from
position tracking (B1, B2 ...). Arrival estimates exist only at the sampled stops, so the ETA at every other stop is
interpolated between them (and extrapolated past the last one with the traffic model). All of this is labelled in the UI.

Headway at a stop = ETA(follower) - ETA(leader).      Bunched = headway <= bunch_thr x scheduled HW.
Confirmed bunching = bunched now AND it stays bunched for >= confirm_stops consecutive stops (stops already travelled together
since we first saw the pair + predicted stops ahead). Fewer stops = Developing. Not bunched yet but predicted to be = Early warning.
"""
import bisect

PARAMS = {
    "gap_warn": 1.5,        # developing gap: predicted HW >= 1.5 x scheduled
    "gap_crit": 2.0,        # critical gap:   predicted HW >= 2.0 x scheduled
    "bunch_thr": 0.5,       # bunched:        HW <= 0.5 x scheduled
    "confirm_stops": 10,    # bunching must persist this many consecutive stops to be confirmed
    "horizon_min": 30,      # prediction horizon
    "refresh_sec": 30,      # collector interval
    "w_gap": 30, "w_bunch": 25, "w_persist": 20, "w_deter": 15, "w_time": 10,      # risk-score weights (%)
    "yellow_conv": 0.75,    # yellow "early sign": predicted HW <= 75% of scheduled and shrinking
    "yellow_div": 1.25,     # yellow "early sign": predicted HW >= 125% of scheduled and growing
}
HORIZONS = (0, 10, 20, 30)
EPS = 1e-6                     # boundary comparisons (<=, >=) must not be defeated by floating-point noise
RISK_ORDER = {"red": 0, "orange": 1, "yellow": 2, "green": 3, "nodata": 4}


# ----------------------------------------------------------------------------- tracking
class Tracker:
    """Stable bus ids between polls and memory of how long a pair has been bunched."""

    def __init__(self):
        self.tracks, self.n, self.first = [], 0, {}

    def update(self, ts, buses, total_km):
        used = set()
        for b in sorted(buses, key=lambda x: -x["s_km"]):
            best, bd = None, 1e9
            for t in self.tracks:
                if t["id"] in used:
                    continue
                dt = max(1.0, ts - t["ts"])
                d = b["s_km"] - t["km"]
                if -0.4 <= d <= 90.0 * dt / 3600 + 0.6 and abs(d) < bd:
                    best, bd = t, abs(d)
            if best is None:
                self.n += 1
                best = {"id": f"B{self.n}"}
                self.tracks.append(best)
            best.update(km=b["s_km"], ts=ts)
            used.add(best["id"])
            b["id"] = best["id"]
        self.tracks = [t for t in self.tracks if ts - t["ts"] <= 300 and t["km"] < total_km - 0.05]

    def prior(self):
        return dict(self.first)

    def commit(self, pairs_now):
        """pairs_now: {(leader_id, follower_id): leader's next-stop index} for pairs bunched right now."""
        for k, j in pairs_now.items():
            self.first.setdefault(k, j)
        for k in list(self.first):
            if k not in pairs_now:
                del self.first[k]


# ----------------------------------------------------------------------------- ETA at every stop
def eta_vector(bus, stop_s, tm, ref=None):
    """ETA (min) of a bus at every stop still ahead of it. Measured where LTA gave one, else interpolated / extrapolated.
    ref = ETA vector of the bus ahead: past this bus's last measured stop it follows the SAME road minutes later, so we reuse the
    leader's segment times instead of extrapolating both buses independently (independent model errors would fake convergence)."""
    n, s = len(stop_s), bus["s_km"]
    etas = {int(k): float(v) for k, v in (bus.get("etasf") or bus.get("etas") or {}).items() if 0 <= int(k) < n}
    j0 = next((j for j, x in enumerate(stop_s) if x > s + 1e-6), n)
    vec = [None] * n
    if not etas:
        return vec, j0
    ks = sorted(etas)
    k_last, k_first = ks[-1], ks[0]
    span_last = max(0.05, stop_s[k_last] - s)
    kmh = min(40.0, max(10.0, span_last / max(0.5, etas[k_last]) * 60))          # only used when there is no traffic model
    for j in range(j0, n):
        x = stop_s[j]
        if j in etas:
            v = etas[j]
        elif j < k_first:
            span = max(1e-6, stop_s[k_first] - s)
            v = etas[k_first] * max(0.0, x - s) / span
        elif j > k_last:
            v = etas[k_last] + (tm.t(stop_s[k_last], x) if tm else (x - stop_s[k_last]) / kmh * 60)
        else:
            i = bisect.bisect_right(ks, j)
            k1, k2 = ks[i - 1], ks[i]
            f = (x - stop_s[k1]) / max(1e-6, stop_s[k2] - stop_s[k1])
            v = etas[k1] + f * (etas[k2] - etas[k1])
        vec[j] = v
    if ref is not None:
        r0 = next((j for j, v in enumerate(ref) if v is not None), n)
        a = max(k_last, r0)
        if a < n and vec[a] is not None and ref[a] is not None:
            for j in range(a + 1, n):
                if ref[j] is not None:
                    vec[j] = vec[a] + (ref[j] - ref[a])
    m = 0.0
    for j in range(j0, n):                                                           # a bus cannot arrive earlier at a later stop
        if vec[j] is not None:
            vec[j] = max(vec[j], m)
            m = vec[j]
    return vec, j0


def pair_series(lead, foll, n):
    out = []
    for j in range(lead["j0"], n):
        el, ef = lead["vec"][j], foll["vec"][j]
        if el is not None and ef is not None:
            out.append((j, max(0.0, ef - el), el))
    return out


def _hw_at(ser, h):
    """Headway at the stop the leader reaches at time h (min from now); None once the leader has finished its trip."""
    for j, hw, e in ser:
        if e >= h:
            return hw
    return None


def _r(x, n=1):
    return None if x is None else round(x, n)


# ----------------------------------------------------------------------------- one pair of consecutive buses
def analyse_pair(lead, foll, ser, H, P, prior):
    thr, warn, crit, hz = P["bunch_thr"] * H + EPS, P["gap_warn"] * H - EPS, P["gap_crit"] * H - EPS, P["horizon_min"]
    a = {"lead": lead["id"], "foll": foll["id"], "ok": bool(ser)}
    if not ser:
        return a
    hws = {h: _hw_at(ser, h) for h in HORIZONS if h <= hz}
    within = [x for x in ser if x[2] <= hz] or ser[:1]
    now = ser[0][1]
    bunched_now = now <= thr
    k0 = 0 if bunched_now else next((k for k, (j, hw, e) in enumerate(ser) if hw <= thr and e <= hz), None)
    run = 0
    if k0 is not None:
        for j, hw, e in ser[k0:]:
            if hw > thr:
                break
            run += 1
    key = (lead["id"], foll["id"])
    travelled = max(0, lead["j0"] - prior[key]) if (bunched_now and key in prior) else 0
    mx = max(x[1] for x in within)
    vals = [v for v in hws.values() if v is not None]
    delta = vals[-1] - vals[0] if len(vals) > 1 else 0.0
    a.update(now=now, hws=hws, bunched_now=bunched_now, run=run, travelled=travelled,
             start_j=ser[k0][0] if k0 is not None else None,
             t_bunch=0.0 if bunched_now else (ser[k0][2] if k0 is not None else None),
             min_hw=min(x[1] for x in within), max_hw=mx, max_ratio=mx / H, max_at=next(x[0] for x in within if x[1] == mx),
             delta=delta, gap_state="critical" if mx >= crit else ("developing" if mx >= warn else None),
             t_gap=0.0 if now >= warn else next((e for j, hw, e in within if hw >= warn), None),
             smap={j: hw for j, hw, e in ser}, pct_gap=sum(1 for x in within if x[1] >= warn) / len(within))
    return a


# ----------------------------------------------------------------------------- groups (2BB / 3BB / 4BB+)
def build_groups(vecs, pairs, P, H, names):
    n, thr, N = len(vecs), P["bunch_thr"] * H + EPS, int(P["confirm_stops"])
    used = [False] * n
    groups = []

    def grun(a, b):
        j, r = vecs[a]["j0"], 0
        while all(pairs[i].get("smap", {}).get(j, 1e9) <= thr for i in range(a, b)):
            r += 1
            j += 1
        return r

    def trav(a, b):
        return min(pairs[i].get("travelled", 0) for i in range(a, b))

    def mk(a, b, status, stops, t_occur, start_j=None):
        size = b - a + 1
        j = start_j if start_j is not None else vecs[a]["j0"]
        return {"a": a, "b": b, "ids": [vecs[k]["id"] for k in range(a, b + 1)], "size": size, "label": f"{size}BB",
                "bucket": "2BB" if size == 2 else "3BB" if size == 3 else "4BB+", "status": status, "stops": stops,
                "min_hw": _r(min(pairs[i]["min_hw"] for i in range(a, b))), "hw_now": _r(min(pairs[i]["now"] for i in range(a, b))),
                "t_occur": _r(t_occur), "joining": [], "near": vecs[a]["near"],
                "location": names[j] if 0 <= j < len(names) else vecs[a]["near"]}

    def mark(g):
        for k in range(g["a"], g["b"] + 1):
            used[k] = True
        groups.append(g)

    for size in range(n, 1, -1):                                                     # confirmed: biggest groups first
        for a in range(0, n - size + 1):
            b = a + size - 1
            if any(used[a:b + 1]):
                continue
            r = grun(a, b)
            if r >= 1 and r + trav(a, b) >= N:
                mark(mk(a, b, "confirmed", r + trav(a, b), 0.0))
    for g in [g for g in groups if g["status"] == "confirmed"]:                       # a neighbour that is bunching with the group is "joining"
        for nb, i in ((g["b"] + 1, g["b"]), (g["a"] - 1, g["a"] - 1)):
            if 0 <= nb < n and 0 <= i < n - 1 and not used[nb] and pairs[i].get("bunched_now"):
                g["joining"].append(vecs[nb]["id"])
                used[nb] = True
    for size in range(n, 1, -1):                                                     # developing: bunched now, not yet persistent
        for a in range(0, n - size + 1):
            b = a + size - 1
            if any(used[a:b + 1]):
                continue
            r = grun(a, b)
            if r >= 1:
                mark(mk(a, b, "developing", r + trav(a, b), 0.0))
    i = 0
    while i < n - 1:                                                                 # early warning: converging, predicted to bunch
        def ew(k):
            return pairs[k].get("t_bunch") is not None and not pairs[k].get("bunched_now") and not used[k] and not used[k + 1]
        if ew(i):
            j = i
            while j + 1 < n - 1 and ew(j + 1):
                j += 1
            ps = pairs[i:j + 1]
            first = min(ps, key=lambda p: p["t_bunch"])
            mark(mk(i, j + 1, "early", min(p["run"] for p in ps), max(p["t_bunch"] for p in ps), first["start_j"]))
            i = j + 1
        else:
            i += 1
    return groups


# ----------------------------------------------------------------------------- service level
def _empty(ctx, H, note, n):
    return {"service": ctx["service"], "direction": ctx["direction"], "risk": "nodata", "score": 0, "n_buses": n, "sched_hw": H,
            "hw_src": ctx.get("H_src"), "issue": "No data", "note": note, "groups": [], "gaps": [], "seq": [], "bb": None, "gap": None,
            "hw": {}, "trend": None, "location": None, "t_occur": None, "traffic": None, "dist": {"now": [], "pred": []},
            "counts": {"gaps": 0, "bb": 0, "bb2": 0, "bb3": 0, "bb4": 0, "early": 0}, "bunched_pairs": {}}


def evaluate(ctx, P=None):
    """ctx: service, direction, now, stop_s[], stop_names[], route_km, buses[{id?, s_km, etas, etasf?, near, lat, lon, monitored, load}],
    tm (headway.TimeModel|None), H (scheduled HW min|None), H_src, prior {(a,b): first_j}."""
    P = {**PARAMS, **(P or {})}
    H, stop_s, names, tm = ctx.get("H"), ctx["stop_s"], ctx.get("stop_names") or [], ctx.get("tm")
    bs = sorted(ctx["buses"], key=lambda b: -b["s_km"])
    if not H:
        return _empty(ctx, H, "Scheduled headway unknown - add this service to the Service Headway Master.", len(bs))
    if len(bs) < 2:
        return _empty(ctx, H, "Needs at least 2 live buses to measure spacing.", len(bs))
    n_st = len(stop_s)
    vecs, prev = [], None
    for i, b in enumerate(bs):
        vec, j0 = eta_vector(b, stop_s, tm, prev)
        prev = vec
        vecs.append({"id": b.get("id") or f"B{i + 1}", "order": i + 1, "s_km": b["s_km"], "vec": vec, "j0": j0, "near": b.get("near", ""),
                     "lat": b.get("lat"), "lon": b.get("lon"), "monitored": b.get("monitored", True), "load": b.get("load", "")})
    prior = ctx.get("prior") or {}
    pairs = [analyse_pair(vecs[i], vecs[i + 1], pair_series(vecs[i], vecs[i + 1], n_st), H, P, prior) for i in range(len(vecs) - 1)]
    if not any(p["ok"] for p in pairs):
        return _empty(ctx, H, "No arrival estimates overlap between consecutive buses yet.", len(bs))
    for p in pairs:
        if not p["ok"]:
            p.update(now=None, hws={}, bunched_now=False, run=0, travelled=0, t_bunch=None, min_hw=0.0, smap={}, delta=0.0, gap_state=None)
    groups = build_groups(vecs, pairs, P, H, names)
    trend_thr = max(1.0, 0.1 * H)

    gaps = []
    for i, p in enumerate(pairs):
        if p.get("gap_state"):
            tr = "widening" if p["delta"] >= trend_thr else "recovering" if p["delta"] <= -trend_thr else "stable"
            gaps.append({"lead": p["lead"], "foll": p["foll"], "order": i + 1, "now": _r(p["now"]), "max_hw": _r(p["max_hw"]), "ratio": round(p["max_ratio"], 2),
                         "state": p["gap_state"], "t_occur": _r(p["t_gap"]), "trend": tr, "hws": {str(k): _r(v) for k, v in p["hws"].items()},
                         "location": names[p["max_at"]] if 0 <= p["max_at"] < len(names) else vecs[i]["near"], "i": i})

    conf = [g for g in groups if g["status"] == "confirmed"]
    devn = [g for g in groups if g["status"] == "developing"]
    early = [g for g in groups if g["status"] == "early"]
    crit_gap = [g for g in gaps if g["state"] == "critical"]
    dev_gap = [g for g in gaps if g["state"] == "developing"]
    yellow = any(p["ok"] and ((min([v for v in p["hws"].values() if v is not None] or [H]) <= P["yellow_conv"] * H and p["delta"] < 0) or
                              (max([v for v in p["hws"].values() if v is not None] or [H]) >= P["yellow_div"] * H and p["delta"] > 0)) for p in pairs)
    if conf or any(g["size"] >= 3 for g in devn) or crit_gap:
        risk = "red"
    elif devn or early or dev_gap:
        risk = "orange"
    elif yellow:
        risk = "yellow"
    else:
        risk = "green"

    # focus pair = the pair that explains the row
    def prank(g):
        return (-g["size"], -g["stops"])
    top = min(conf, key=prank) if conf else min(devn, key=prank) if devn else (min(early, key=lambda g: g["t_occur"]) if early else None)
    if top:
        fi = min(range(top["a"], top["b"]), key=lambda i: pairs[i]["min_hw"])
    elif gaps:
        fi = max(gaps, key=lambda g: g["ratio"])["i"]
    else:
        ok_i = [i for i, p in enumerate(pairs) if p["ok"]]
        fi = max(ok_i, key=lambda i: abs((pairs[i]["now"] or H) / H - 1))
    fp = pairs[fi]
    fhw = {str(k): _r(v) for k, v in fp["hws"].items()}

    # trend of the focus pair (bunching: converging is worse; gap: widening is worse)
    if top:
        trend = "converging" if fp["delta"] <= -trend_thr else "separating" if fp["delta"] >= trend_thr else "stable"
    else:
        trend = "widening" if fp["delta"] >= trend_thr else "recovering" if fp["delta"] <= -trend_thr else "stable"

    # time to occur (min): 0 = already happening
    ts = [0.0] * bool(conf or devn) + [g["t_occur"] for g in early] + [g["t_occur"] for g in gaps if g["t_occur"] is not None]
    t_occur = min(ts) if ts else None

    # risk score 0-100 (weights configurable)
    r_gap = max([g["ratio"] for g in gaps] or [max(p["max_ratio"] for p in pairs if p["ok"])])
    c_gap = max(0.0, min(1.0, (r_gap - 1) / max(0.01, P["gap_crit"] - 1)))
    sev = {"confirmed": 1.0, "developing": 0.6, "early": 0.4}
    c_bunch = max([min(1.0, (g["size"] - 1) / 3) * sev[g["status"]] for g in groups] or [0.0])
    c_pers = max([min(1.0, g["stops"] / max(1, P["confirm_stops"])) for g in groups if g["status"] != "early"] + [max(p.get("pct_gap", 0) for p in pairs if p["ok"]) if gaps else 0.0])
    c_det = 0.0
    for p in pairs:
        if p["ok"]:
            c_det = max(c_det, min(1.0, max(-p["delta"] if p["min_hw"] <= P["bunch_thr"] * H * 1.6 else 0, p["delta"] if p["max_hw"] >= H else 0) / H))
    c_time = 1 - min(t_occur, P["horizon_min"]) / P["horizon_min"] if t_occur is not None else 0.0
    W = [P["w_gap"], P["w_bunch"], P["w_persist"], P["w_deter"], P["w_time"]]
    score = round(100 * sum(w * c for w, c in zip(W, [c_gap, c_bunch, c_pers, c_det, c_time])) / max(1e-9, sum(W)))

    # per-bus sequence for the drill-down
    seq = []
    for k, v in enumerate(vecs):
        g = next((g for g in groups if g["a"] <= k <= g["b"]), None)
        join = next((g for g in groups if v["id"] in g["joining"]), None)
        gp = next((x for x in gaps if x["i"] == k), None)
        if g and g["status"] == "early":
            st = "Converging"
        elif g:
            st = "Leading" if k == g["a"] else "Bunched"
        elif join:
            st = "Joining"
        elif gp:
            st = "Gap"
        else:
            st = "Normal"
        seq.append({"order": v["order"], "id": v["id"], "near": v["near"], "hw_next": _r(pairs[k]["now"]) if k < len(pairs) and pairs[k]["ok"] else None,
                    "status": st, "lat": v["lat"], "lon": v["lon"], "monitored": v["monitored"], "load": v["load"]})

    # supporting traffic information (context only - never presented as the cause)
    traffic = None
    if tm is not None:
        s0 = vecs[fi]["s_km"]
        cost, slow, longest = tm.cong(s0, min(tm.total, s0 + 3.0))
        traffic = {"slow_km": _r(slow, 2), "longest_km": _r(longest, 2), "text": (f"Traffic ahead of the leading bus is slow (< 20 km/h) for {longest:.1f} km - a possible contributing factor."
                   if longest >= 0.5 else "No prolonged slow traffic within 3 km ahead of the leading bus.")}

    bb = None
    if groups:
        g = top or groups[0]
        bb = {k: g[k] for k in ("label", "bucket", "size", "status", "stops", "min_hw", "hw_now", "t_occur", "joining", "location", "ids")}
    worst_gap = max(gaps, key=lambda g: g["ratio"]) if gaps else None
    if top and worst_gap:
        issue = "Bunching + Gap"
    elif top:
        issue = "Bunching"
    elif worst_gap:
        issue = "Headway Gap"
    else:
        issue = "Normal"
    location = (top["location"] if top else worst_gap["location"] if worst_gap else vecs[fi]["near"])
    note = None
    if top and top["status"] == "early":
        note = f"Developing {top['label']}: gap now {fp['now']:.0f} min, predicted {fp['min_hw']:.0f} min, expected bunching in {top['t_occur']:.0f} min."
    dnow = [round(p["now"], 1) for p in pairs if p["ok"]]
    dpred = [round(v, 1) for p in pairs if p["ok"] for h, v in p["hws"].items() if h > 0 and v is not None]
    cnt = {"gaps": len(gaps), "bb": len(conf) + len(devn), "early": len(early)}
    cnt.update(bb2=sum(1 for g in conf + devn if g["size"] == 2), bb3=sum(1 for g in conf + devn if g["size"] == 3), bb4=sum(1 for g in conf + devn if g["size"] >= 4))
    return {"service": ctx["service"], "direction": ctx["direction"], "risk": risk, "score": score, "n_buses": len(vecs), "sched_hw": H,
            "hw_src": ctx.get("H_src"), "issue": issue, "note": note, "groups": [{k: v for k, v in g.items() if k not in ("a", "b")} for g in groups],
            "gaps": [{k: v for k, v in g.items() if k != "i"} for g in gaps], "seq": seq, "bb": bb, "gap": worst_gap and {k: v for k, v in worst_gap.items() if k != "i"},
            "hw": fhw, "focus": {"lead": fp["lead"], "foll": fp["foll"]}, "trend": trend, "location": location, "t_occur": _r(t_occur),
            "traffic": traffic, "dist": {"now": dnow, "pred": dpred}, "counts": cnt,
            "bunched_pairs": {(p["lead"], p["foll"]): vecs[i]["j0"] for i, p in enumerate(pairs) if p.get("bunched_now")},
            "score_parts": {"gap": round(c_gap, 2), "bunch": round(c_bunch, 2), "persist": round(c_pers, 2), "deter": round(c_det, 2), "time": round(c_time, 2)}}
