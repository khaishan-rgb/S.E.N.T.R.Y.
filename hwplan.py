"""
SG Transport Pulse - Halfway Planner engine (V15.0).

Question answered: "Bus C is late on its current direction. How should the SAME Bus C operate halfway on its NEXT direction,
where should it enter, how should the buses around it be regulated, and which plan gives the lowest downstream EWT?"

Movement (one continuous bus):  complete current trip -> arrive interchange -> break -> OFF-SERVICE by real road -> enter the
next direction at the halfway stop -> continue to the final stop.

Assumptions (all shown to the controller):
  * in-service running: STOP_MIN (2) minutes between consecutive stops - used for every in-service forecast;
  * off-service running: real road routing from the interchange (passed in as `reach`), never stops x 2 min;
  * departures from the interchange follow regular slots at the target headway; a bus departs at max(ready, slot), so a bus that
    arrives with layover to spare can be advanced (up to its slack) and a bus that has not departed can be held;
  * EWT = sum(h^2) / (2 sum h) - H/2 at monitoring points along the whole next direction, averaged (minutes).
"""
import math

P0 = {"stop_min": 2.0, "break_min": 7.0, "hold_max": 5.0, "adv_max": 5.0, "adj_cost": 0.002, "n_points": 12, "max_reach_min": 60.0,
      "slow_max": 8.0, "slow_min": 1.0, "even_tol": 2.0, "slow_gate": 0.02, "dep_later_max": 10.0, "dep_earlier_max": 10.0, "layover": 10.0}
MODEL = "hwplan-15.4-os"


def hhmm(m):
    if m is None:
        return None
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _r(x, n=1):
    return None if x is None else round(x, n)


def prog(ss, s):
    """route km -> fractional stop index."""
    if s <= ss[0]:
        return 0.0
    for k in range(len(ss) - 1):
        if ss[k] <= s < ss[k + 1]:
            return k + (s - ss[k]) / max(1e-9, ss[k + 1] - ss[k])
    return float(len(ss) - 1)


def ewt(gaps, H):
    s = sum(gaps)
    return (sum(g * g for g in gaps) / (2 * s) - H / 2.0) if s > 0 else None


def plan(ctx, reach):
    """ctx: now (min of day), late {bus, delay}, T {dir, stops, ss, buses[{id, s}], H}, O {...} or None for a loop service, P.
       reach: {j: (minutes, km, source)} off-service by real road from the interchange (first stop of O) to stop j of O."""
    P = {**P0, **(ctx.get("P") or {})}
    now, T, O = ctx["now"], ctx["T"], ctx.get("O")
    loop = O is None
    O = O or T
    SM, BRK = float(P["stop_min"]), float(P["break_min"])
    nT, nO = len(T["stops"]), len(O["stops"])
    H = float(O.get("H") or T.get("H") or 10.0)
    Cn, D = ctx["late"]["bus"], float(ctx["late"]["delay"])
    tl, ol = f"D{T['dir']}", f"D{O['dir']}"
    # ---- current direction: every bus completes its trip (Bus C with its lateness), then becomes ready after the break
    t_buses = sorted(({"num": b["id"], "p": prog(T["ss"], b["s"]), "w": float(b.get("wait") or 0.0)} for b in T["buses"]),
                     key=lambda x: (-x["p"], x["w"]))                # front first; buses waiting at the first stop last
    if not any(b["num"] == Cn for b in t_buses):
        return {"ok": False, "error": "The selected bus is no longer in the live snapshot - refresh the live buses."}
    for b in t_buses:
        b["arr"] = now + b["w"] + SM * ((nT - 1) - b["p"]) + (D if b["num"] == Cn else 0.0)
        b["ready"] = b["arr"] + BRK
    c = next(i for i, b in enumerate(t_buses) if b["num"] == Cn)
    # ---- next direction: buses already on it (in service), then the next trip of every current-direction bus, in scheduled order
    units = []
    LAY = max(float(P["layover"]), BRK)                            # scheduled layover: planned departure = on-time arrival + layover
    o_all = [] if loop else [{"num": b["id"], "q": prog(O["ss"], b["s"]), "w": b.get("wait")} for b in O["buses"]]
    o_cur = sorted((b for b in o_all if b["w"] is None), key=lambda x: -x["q"])
    for b in o_cur:
        units.append({"uid": f"O{b['num']}", "num": b["num"], "kind": "in_service", "q": b["q"], "dep": now - SM * b["q"], "movable": False,
                      "label": f"{ol} Bus {b['num']}", "src": f"in service on {ol}"})
    for b in sorted((b for b in o_all if b["w"] is not None), key=lambda x: x["w"]):     # in layover at the interchange, departing on schedule
        dep_ = now + float(b["w"])
        units.append({"uid": f"O{b['num']}", "num": b["num"], "kind": "next_trip", "dep": dep_, "slot": dep_, "ready": max(now, dep_ - LAY + BRK),
                      "arr": dep_ - LAY, "slack": max(0.0, dep_ - max(dep_ - LAY + BRK, now)), "movable": True, "late": False,
                      "label": f"{ol} Bus {b['num']} (at interchange)", "src": f"in layover at {O['stops'][0]['name']}"})
    for i, b in enumerate(t_buses):
        slot = (b["arr"] - (D if b["num"] == Cn else 0.0)) + LAY      # the timetable slot is not moved by lateness
        dep = max(b["ready"], slot, now)
        units.append({"uid": f"N{b['num']}", "num": b["num"], "kind": "next_trip", "dep": dep, "slot": slot, "ready": b["ready"], "arr": b["arr"],
                      "slack": max(0.0, dep - max(b["ready"], now)), "movable": True, "late": b["num"] == Cn,
                      "label": f"Bus {b['num']}", "src": f"Bus {b['num']} after its {tl} trip"})
    ci = next(i for i, u in enumerate(units) if u["uid"] == f"N{Cn}")
    C = units[ci]
    roles = {}
    for off, r in ((-2, "A"), (-1, "B"), (1, "D"), (2, "E")):
        if 0 <= ci + off < len(units):
            roles[r] = units[ci + off]
    roles["C"] = C
    OSM = ctx.get("mode") == "os"                                 # OS mode: an EXTRA bus + Bus Captain goes halfway; Bus C runs its next trip late
    if OSM:
        os_ = ctx["os"]
        INS = {"uid": "OS", "num": "OS", "kind": "os", "role": "OS", "label": "OS bus", "movable": False, "ready": max(now, float(os_["t0"])),
               "dep": None, "src": os_.get("label") or "OS location"}
    else:
        INS = C
    for r, u in roles.items():
        u["role"] = r
    # ---- arrival times (2 min per stop in service)
    def times(u, dep=None, entry=None):
        out = {}
        if u["kind"] == "in_service":
            for k in range(nO):
                out[k] = now + SM * (k - u["q"])          # negative offsets = already passed
            return out
        if entry is not None:
            j, te = entry
            return {k: te + SM * (k - j) for k in range(j, nO)}
        d = u["dep"] if dep is None else dep
        return {k: d + SM * k for k in range(nO)}

    step = max(1, (nO - 2) // int(P["n_points"]))
    points = list(range(1, nO, step))
    if nO - 1 not in points:
        points.append(nO - 1)

    TRIM = bool(P.get("edge_trim"))                                # TEST MODE: gaps at the edge of the entered fleet are not real
    EDGE = max(2.5 * H, float(P.get("edge_gap") or 0.0) + H)
    RS = {roles[r]["uid"] for r in ("B", "C", "D", "E") if r in roles}

    def evaluate(tab):
        """tab: {uid: {k: t}} -> (EWT, max headway, min headway, per-point headways)"""
        vals, mx, mn, perk = [], 0.0, 1e9, {}
        for k in points:
            seq = sorted((t[k], uid) for uid, t in tab.items() if k in t)
            past = [x for x in seq if x[0] <= now]
            fut = [x for x in seq if x[0] > now]
            sq = ([past[-1]] if past else []) + fut
            gaps = [b[0] - a[0] for a, b in zip(sq, sq[1:])]
            if TRIM:                                                 # drop fleet-edge gaps that do not touch the scenario (B, C, D, E)
                keep = [i for i, g in enumerate(gaps) if not (g > EDGE and sq[i][1] not in RS and sq[i + 1][1] not in RS)]
                gaps = [gaps[i] for i in keep]
            if len(gaps) < 1:
                continue
            e = ewt(gaps, H)
            if e is None:
                continue
            vals.append(e); mx = max(mx, max(gaps)); mn = min(mn, min(gaps)); perk[k] = (gaps, sq)
        return (sum(vals) / len(vals) if vals else None), mx, (mn if mn < 1e9 else None), perk

    base = {u["uid"]: times(u) for u in units}
    e0, mx0, mn0, per0 = evaluate(base)
    if e0 is None:
        return {"ok": False, "error": f"Not enough buses to forecast {ol} headways."}
    risk = "HIGH" if (mn0 is not None and mn0 < 0.3 * H) or mx0 >= 2.0 * H else "MEDIUM" if (mn0 is not None and mn0 < 0.6 * H) or mx0 >= 1.5 * H else "LOW"
    full_dep = C["dep"]

    def seq_at(tab, k, adj=None):
        """buses around Bus C at stop k: ordered by passing time."""
        out = []
        for u in units + ([INS] if OSM else []):
            if not u.get("role"):
                continue
            t = tab.get(u["uid"], {}).get(k)
            if t is None:
                continue
            out.append({"uid": u["uid"], "num": u["num"], "role": u.get("role"), "t": _r(t), "clock": hhmm(t), "label": u["label"],
                        "adj": (adj or {}).get(u["uid"], 0.0)})
        out.sort(key=lambda x: x["t"])
        return out

    cands, infeasible = [], {"no_route": 0, "too_far": 0, "no_benefit": 0, "no_gap": 0, "behind_rear": 0}
    U = {u["uid"]: u for u in units}
    Bu = roles.get("B")
    SLOW_MAX, SLOW_MIN, TOL, GATE = float(P["slow_max"]), float(P["slow_min"]), float(P["even_tol"]), float(P["slow_gate"])

    # ---- INTERCHANGE REGULATION (independent of the entry stop): Bus C's departure slot is empty, so the buses around it are
    # re-spaced by headway - buses ahead (A, B) may only depart LATER, buses behind (D, E) only EARLIER; a bus that is late
    # (no layover to spare: it already departs as soon as its break ends) or has already departed is not adjusted.
    seq_ids = [u["uid"] for u in units if OSM or u["uid"] != C["uid"]]
    if OSM:
        seq_ids.sort(key=lambda uid: U[uid]["dep"])                # Bus C departs late in its own slot order
    pos = {uid: i for i, uid in enumerate(seq_ids)}
    win = [roles[r]["uid"] for r in ("A", "B", "D", "E") if r in roles]
    dep0 = {uid: U[uid]["dep"] for uid in seq_ids}
    lo, hi, why_fix = {}, {}, {}
    for r in ("A", "B", "D", "E"):
        if r not in roles:
            continue
        u = roles[r]; uid = u["uid"]
        if u["kind"] != "next_trip" or u["dep"] <= now:
            lo[uid] = hi[uid] = u["dep"]; why_fix[uid] = "already departed"
        elif r in ("A", "B"):
            lo[uid], hi[uid] = u["dep"], u["dep"] + float(P["dep_later_max"])
        else:
            earliest_dep = max(u["ready"], now)
            lo[uid], hi[uid] = max(earliest_dep, u["dep"] - float(P["dep_earlier_max"])), u["dep"]
            if lo[uid] >= hi[uid] - 0.5:
                why_fix[uid] = ("late \u2013 no layover to spare" if u["arr"] + LAY > u["slot"] + 0.5 or u["ready"] >= u["slot"] - 0.5 else "no time to spare")
    dep = dict(dep0)
    first, last = (min(pos[w] for w in win), max(pos[w] for w in win)) if win else (0, -1)
    for _ in range(200):                                          # most even spacing within the limits (sum of squared headways)
        moved = 0.0
        for w in win:
            i = pos[w]
            if i == 0 or i == len(seq_ids) - 1:
                continue
            prv, nxt = dep[seq_ids[i - 1]], dep[seq_ids[i + 1]]
            want = min(max((prv + nxt) / 2.0, lo[w], prv), hi[w], nxt)
            moved = max(moved, abs(want - dep[w])); dep[w] = want
        if moved < 1e-4:
            break
    dep_adj = {}
    for w in win:
        d_ = round(dep[w] - dep0[w])
        if abs(d_) >= 1:
            dep_adj[w] = float(d_)
    adj_list = []
    for r in ("A", "B", "D", "E"):
        if r not in roles:
            continue
        u = roles[r]; uid = u["uid"]; d_ = dep_adj.get(uid, 0.0)
        reason = why_fix.get(uid) or ("no change needed" if not d_ else "")
        adj_list.append({"role": r, "uid": uid, "num": u["num"], "label": u["label"], "dep": hhmm(u["dep"]), "dep_new": hhmm(u["dep"] + d_), "adj": d_,
                         "reason": reason, "ready": hhmm(u.get("ready")) if u.get("ready") is not None else None})
    baseA = dict(base)
    for uid, d_ in dep_adj.items():
        baseA[uid] = times(U[uid], dep=U[uid]["dep"] + d_)
    ic_hw0 = [dep0[seq_ids[i + 1]] - dep0[seq_ids[i]] for i in range(max(0, first - 1), min(len(seq_ids) - 1, last + 1))] if win else []
    ic_first = max(0, first - 1) if win else 0
    ic_hw1 = [(dep0[seq_ids[i + 1]] + dep_adj.get(seq_ids[i + 1], 0)) - (dep0[seq_ids[i]] + dep_adj.get(seq_ids[i], 0))
              for i in range(max(0, first - 1), min(len(seq_ids) - 1, last + 1))] if win else []

    def entry_plan(j, e_j, tabbase):
        """aim Bus C at the middle of the prolonged headway at stop j (bus ahead of its slot -> next bus, without Bus C)."""
        others = sorted((t[j], uid) for uid, t in tabbase.items() if j in t and uid != INS["uid"])
        f = (tabbase[Bu["uid"]][j], Bu["uid"]) if (Bu and j in tabbase[Bu["uid"]]) else max((x for x in others if x[0] <= e_j), default=None)
        if not f:
            return None
        nx = min((x for x in others if x[0] > f[0]), default=None) or (f[0] + 2.0 * H, None)
        mid = (f[0] + nx[0]) / 2.0
        te = max(e_j, mid)
        if te >= nx[0] - 0.5:
            return "behind"
        tab = dict(tabbase); tab[INS["uid"]] = times(C, entry=(j, te))
        e, mx, mn, per = evaluate(tab)
        if e is None:
            return None
        return {"f": f, "nx": nx, "mid": mid, "te": te, "tab": tab, "e": e, "mx": mx, "per": per, "imb": abs((te - f[0]) - (nx[0] - te))}

    for j in range(1, nO - 1):
        rr = reach.get(j)
        if not rr:
            infeasible["no_route"] += 1
            continue
        rmin, rkm, rsrc = rr
        if rmin > float(P["max_reach_min"]):
            infeasible["too_far"] += 1
            continue
        e_j = INS["ready"] + rmin                                # earliest the halfway bus can be at this stop
        if not OSM and e_j >= full_dep + SM * j - 0.5:
            infeasible["no_benefit"] += 1                        # the full trip from the interchange would get there first
            continue
        pl = entry_plan(j, e_j, baseA)
        if pl is None:
            infeasible["no_gap"] += 1
            continue
        if pl == "behind":
            infeasible["behind_rear"] += 1
            continue
        pl0 = entry_plan(j, e_j, base)                            # same stop without the interchange adjustments (for comparison)
        use_adj = bool(dep_adj) and not (isinstance(pl0, dict) and pl0["e"] <= pl["e"] + 1e-9 and pl0["imb"] <= max(TOL, pl["imb"]))
        if not use_adj and isinstance(pl0, dict):
            pl, pl0 = pl0, pl                                     # the halfway works better without moving the other buses
        cadj = dict(dep_adj) if use_adj else {}
        te, tab, f, nx = pl["te"], pl["tab"], pl["f"], pl["nx"]
        fu = U[f[1]]
        behind = sorted((t[j], uid) for uid, t in tab.items() if j in t and t[j] > te + 1e-6 and uid != INS["uid"])
        ru = U[behind[0][1]] if behind else None
        tp = lambda u: tab[u["uid"]].get(j) if u else None
        dn_pts = [k for k in points if k >= j]
        dn_mx = max((max(pl["per"][k][0]) for k in dn_pts if k in pl["per"]), default=None)
        wait = te - e_j
        J = pl["e"] + float(P["adj_cost"]) * sum(abs(v) for v in cadj.values())
        key = (0 if pl["imb"] <= TOL else 1, pl["imb"] if pl["imb"] > TOL else 0.0, J)
        cands.append({"j": j, "no": j + 1, "code": O["stops"][j]["code"], "name": O["stops"][j]["name"], "lat": O["stops"][j]["lat"], "lon": O["stops"][j]["lon"],
                      "skip": j, "avoided": _r(SM * j), "os_min": _r(rmin), "os_km": _r(rkm, 2), "os_src": rsrc,
                      "ready": hhmm(INS["ready"]), "leave": hhmm(INS["ready"] + wait), "wait": _r(wait), "earliest": hhmm(e_j),
                      "entry": _r(te), "entry_clock": hhmm(te), "net": _r(SM * j - rmin),
                      "gap": {"front": fu["label"], "front_role": fu.get("role"), "rear": U[nx[1]]["label"] if nx[1] else "the next trip (beyond the forecast)",
                              "rear_role": U[nx[1]].get("role") if nx[1] else None, "t_front": hhmm(f[0]), "t_rear": hhmm(nx[0]),
                              "minutes": _r(nx[0] - f[0]), "minutes_raw": _r(pl0["nx"][0] - pl0["f"][0]) if (use_adj and isinstance(pl0, dict)) else None,
                              "hold": 0.0, "mid": hhmm(pl["mid"]), "split": [_r(te - f[0]), _r(nx[0] - te)], "imbalance": _r(pl["imb"]), "even": pl["imb"] <= TOL},
                      "front": {"uid": fu["uid"], "num": fu["num"], "role": fu.get("role"), "label": fu["label"], "pass": hhmm(tp(fu)), "adj": cadj.get(fu["uid"], 0.0),
                                "dep": hhmm(fu["dep"]), "movable": fu["movable"]},
                      "rear": ({"uid": ru["uid"], "num": ru["num"], "role": ru.get("role"), "label": ru["label"], "pass": hhmm(tp(ru)), "adj": cadj.get(ru["uid"], 0.0),
                                "dep": hhmm(ru["dep"])} if ru else None),
                      "slows": [], "rear_log": [], "use_adj": use_adj,
                      "dep_adj": [dict(a_, adj=(a_["adj"] if use_adj else 0.0), dep_new=(a_["dep_new"] if use_adj else a_["dep"]),
                                       reason=(a_["reason"] if use_adj else ("not needed for this plan" if a_["adj"] else a_["reason"]))) for a_ in adj_list],
                      "ewt_noadj": round(pl0["e"], 3) if (use_adj and isinstance(pl0, dict)) else None,
                      "ewt_adj": round(pl0["e"], 3) if (not use_adj and dep_adj and isinstance(pl0, dict)) else None,
                      "gap_before": _r(te - tp(fu)) if tp(fu) is not None else None, "gap_after": _r(tp(ru) - te) if ru and tp(ru) is not None else None,
                      "max_hw": _r(dn_mx if dn_mx is not None else pl["mx"]), "max_hw_all": _r(pl["mx"]), "ewt": round(pl["e"], 3), "score": J,
                      "gain": round(e0 - pl["e"], 3), "km_skipped": _r(O["ss"][j], 2), "rkey": key + (j,),
                      "adj": dict(cadj), "_tab": tab, "_adj": dict(cadj)})
    # RECOMMENDED: the first stop where Bus C can land in the middle of the prolonged headway (fewest stops skipped);
    # if no stop allows an even split, the most even one. Lowest whole-route EWT is shown alongside.
    cands.sort(key=lambda x: x["rkey"][:2] + (x["j"],))
    for i, x in enumerate(cands, 1):
        x["rank"] = i
    best = cands[0] if cands else None
    best_ewt = min(cands, key=lambda x: (x["score"], x["j"])) if cands else None
    er = sorted(cands, key=lambda y: y["score"])
    for x in cands:
        x["ewt_rank"] = er.index(x) + 1

    def ic_lane(adj):
        out = []
        for r in ("A", "B", "C", "D", "E"):
            if r not in roles:
                continue
            u = roles[r]
            if adj is not None and r == "C" and not OSM:
                continue                                          # Bus C does not depart from the interchange - it goes halfway
            d_ = (adj or {}).get(u["uid"], 0.0)
            out.append({"role": r, "num": u["num"], "label": u["label"], "t": _r(u["dep"] + d_), "clock": hhmm(u["dep"] + d_), "adj": d_,
                        "in_service": u["kind"] == "in_service", "late": u["uid"] == C["uid"]})
        return out

    def detail(x):
        tab, adj, j = x["_tab"], x["_adj"], x["j"]
        cols = [j] + [k for k in (j + 5, j + 10, j + 15) if k < nO]
        ring = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
        if OSM:
            ring = ring[:ring.index(C) + 1] + [INS] + ring[ring.index(C) + 1:]
        rows = []
        for u in ring:
            t = tab.get(u["uid"], {})
            rows.append({"role": u["role"], "num": u["num"], "label": u["label"], "halfway": u["uid"] == INS["uid"],
                         "dep": None if u["uid"] == INS["uid"] else hhmm((u["dep"] + adj.get(u["uid"], 0.0)) if u["kind"] == "next_trip" else u["dep"]),
                         "adj": adj.get(u["uid"], 0.0), "slow": False, "pass": [hhmm(t.get(k)) if t.get(k) is not None else None for k in cols],
                         "t": [_r(t.get(k)) if t.get(k) is not None else None for k in cols]})
        hwcol = []
        for k in cols:
            sq = sorted((tt[k], uid) for uid, tt in tab.items() if k in tt)
            hw_ = {uid: (t - sq[i - 1][0]) if i > 0 else None for i, (t, uid) in enumerate(sq)}
            if TRIM:
                for i in range(1, len(sq)):
                    if (sq[i][0] - sq[i - 1][0]) > EDGE and sq[i][1] not in RS and sq[i - 1][1] not in RS:
                        hw_[sq[i][1]] = None                         # blank: edge of the entered test fleet, not a real headway
            hwcol.append(hw_)
        for row, u in zip(rows, ring):
            hws = [hwcol[ci_].get(u["uid"]) for ci_ in range(len(cols))]
            row["hw"] = [_r(h) for h in hws]
            row["max_hw"] = _r(max([h for h in hws if h is not None], default=0.0)) if any(h is not None for h in hws) else None
        return {"cols": [{"j": k, "no": k + 1, "code": O["stops"][k]["code"], "name": O["stops"][k]["name"]} for k in cols], "rows": rows,
                "before": seq_at(base, j), "after": seq_at(tab, j, adj), "ic_before": ic_lane(None), "ic_after": ic_lane(adj)}

    def adj_sentence(x):
        al = x["dep_adj"]
        parts = []
        for a_ in al:
            if a_["adj"] > 0:
                parts.append(f"{a_['label']} departs {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']})")
            elif a_["adj"] < 0:
                parts.append(f"{a_['label']} departs {-a_['adj']:.0f} min earlier ({a_['dep']} \u2192 {a_['dep_new']})")
        fixed = [f"{a_['label']} not adjusted ({a_['reason']})" for a_ in al if not a_["adj"] and a_["reason"] not in ("", "no change needed", "not needed for this plan")]
        return parts, fixed

    WHO = "the OS bus" if OSM else f"Bus {Cn}"

    def why(x):
        g = x["gap"]
        parts, fixed = adj_sentence(x)
        s = ""
        if OSM:
            s += (f"Bus {Cn} completes {tl} {D:g} min late and runs its {ol} trip from {O['stops'][0]['name']} at {hhmm(full_dep)} (full trip). "
                  f"An extra OS bus with a Bus Captain fills the gap instead. ")
        if parts:
            s += ((f"The interchange headway around the late Bus {Cn} is evened out: " if OSM else
                   f"Bus {Cn}'s departure slot at {O['stops'][0]['name']} is empty, so the interchange headway is closed by spacing the buses around it evenly: ")
                  + "; ".join(parts) + ". ")
        if fixed:
            s += " ".join(fixed) + ". "
        s += (f"At Stop {x['no']} the headway {WHO} has to fill is then between {g['front']} ({g['t_front']}) and {g['rear']} ({g['t_rear']}): {g['minutes']:.0f} min"
              + (f" (was {g['minutes_raw']:.0f})" if g.get("minutes_raw") and abs(g["minutes_raw"] - g["minutes"]) >= 1 else "")
              + f". Half of it puts {WHO} at {g['mid']}, leaving {g['split'][0]:.0f} / {g['split'][1]:.0f} min. ")
        if OSM:
            s += (f"The OS bus is available at {INS['src']} from {x['ready']}; the real road route to Stop {x['no']} (BS {x['code']}) takes about "
                  f"{x['os_min']:.0f} min ({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
        else:
            s += (f"Bus {Cn} completes {tl} at {hhmm(t_buses[c]['arr'])} ({D:g} min late), is ready at {x['ready']} after the {BRK:g}-min break, and the real road route "
                  f"to Stop {x['no']} (BS {x['code']}) takes about {x['os_min']:.0f} min ({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
        if x["wait"] and x["wait"] >= 0.5:
            s += f"It therefore leaves {'its start point' if OSM else 'the interchange'} {x['wait']:.0f} min later ({x['leave']}) so it enters exactly mid-gap at {x['entry_clock']}. "
        if g["even"]:
            s += f"Stop {x['no']} is the first stop where {WHO} can reach the middle of the gap, so it skips the fewest stops ({x['skip']}). "
        else:
            s += f"No stop lets {WHO} reach the middle in time; Stop {x['no']} gives the most even split. "
        s += f"Downstream {ol} EWT {x['ewt']:.3f} min (no action {e0:.3f}"
        if x.get("ewt_noadj") is not None and parts:
            s += f"; {x['ewt_noadj']:.3f} with the halfway alone, without the interchange adjustments"
        if x.get("ewt_adj") is not None:
            s += f"; moving the other buses at the interchange would give {x['ewt_adj']:.3f}, so they depart as planned"
        s += f"); largest headway after entry {x['max_hw']:.0f} min."
        if best_ewt and best_ewt["code"] != x["code"]:
            s += (f" Stop {best_ewt['no']} has the lowest whole-route EWT ({best_ewt['ewt']:.3f}) because {WHO} serves more stops there, "
                  f"but it splits the gap {best_ewt['gap']['split'][0]:.0f} / {best_ewt['gap']['split'][1]:.0f} min.")
        return s

    def actions(x):
        steps, n = [], 1
        for a_ in [a for a in x["dep_adj"] if a["role"] in ("A", "B")]:
            if a_["adj"] > 0:
                steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "hold", "text": f"Depart the interchange {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']}) to close the headway gap.", "time": a_["dep_new"]})
            else:
                steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "monitor", "text": f"Depart as planned ({a_['dep']}) \u2013 {a_['reason'] or 'no change needed'}.", "time": a_["dep"]})
            n += 1
        if OSM:
            steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "monitor",
                          "text": f"Complete {tl} ({D:g} min late). Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}, break, then run the full {ol} trip from {hhmm(full_dep)}.",
                          "time": hhmm(full_dep)}); n += 1
            steps.append({"n": n, "bus": "OS bus + Bus Captain", "role": "OS", "kind": "halfway",
                          "text": f"Deploy from {INS['src']}. Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive and enter {ol} at {x['entry_clock']} "
                                  f"({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}). Continue to the final stop.", "time": x["entry_clock"]}); n += 1
        wtxt = f" Extend the break by {x['wait']:.0f} min" if x["wait"] and x["wait"] >= 0.5 else ""
        if not OSM:
          steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "halfway",
                      "text": f"Complete {tl}. Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}. Break until {x['ready']}.{wtxt}"
                              f"{'.' if wtxt else ''} Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive and enter {ol} at {x['entry_clock']} "
                              f"({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}). Continue to the final stop.",
                      "time": x["entry_clock"]}); n += 1
        for a_ in [a for a in x["dep_adj"] if a["role"] in ("D", "E")]:
            if a_["adj"] < 0:
                steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "advance", "text": f"Depart the interchange {-a_['adj']:.0f} min earlier ({a_['dep']} \u2192 {a_['dep_new']}) to close the headway gap.", "time": a_["dep_new"]})
            else:
                steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "monitor", "text": f"Depart as planned ({a_['dep']}) \u2013 {a_['reason'] or 'no change needed'}.", "time": a_["dep"]})
            n += 1
        return steps

    for x in cands:
        x["detail"] = detail(x)
        x["why"] = why(x)
        x["actions"] = actions(x)
    ring0 = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
    noaction = {"ewt": round(e0, 3), "max_hw": _r(mx0), "min_hw": _r(mn0), "risk": risk,
                "seq": [{"role": u["role"], "num": u["num"], "label": u["label"], "dep": _r(u["dep"]), "clock": hhmm(u["dep"]), "late": u["uid"] == C["uid"],
                         "in_service": u["kind"] == "in_service"} for u in ring0]}
    strip = lambda x: {k: v for k, v in x.items() if not k.startswith("_") and k not in ("score", "rkey")}
    return {"ok": True, "model": MODEL, "mode": "os" if OSM else "late", "os": ({"label": INS["src"], "ready": hhmm(INS["ready"])} if OSM else None),
            "now": hhmm(now), "loop": loop, "late_dir": T["dir"], "next_dir": O["dir"], "H": _r(H), "H_src": O.get("H_src") or T.get("H_src"),
            "stop_min": SM, "break_min": BRK, "layover": LAY, "late_bus": Cn, "delay": D, "c_arr": hhmm(t_buses[c]["arr"]), "c_ready": hhmm(C["ready"]), "c_full_dep": hhmm(full_dep),
            "interchange": {"code": O["stops"][0]["code"], "name": O["stops"][0]["name"], "lat": O["stops"][0]["lat"], "lon": O["stops"][0]["lon"]},
            "roles": {r: {"num": u["num"], "label": u["label"], "kind": u["kind"]} for r, u in roles.items()},
            "no_action": noaction, "candidates": [strip(x) for x in cands], "best": best["code"] if best else None,
            "best_ewt": best_ewt["code"] if best_ewt else None, "even_tol": TOL,
            "dep_adj": adj_list, "test": TRIM,
            "ic_hw_before": [_r(h) for i, h in enumerate(ic_hw0) if not (TRIM and i == 0 and h > EDGE)],
            "ic_hw_after": [_r(h) for i, h in enumerate(ic_hw1) if not (TRIM and i == 0 and h > EDGE)],
            "infeasible": infeasible, "n_stops": nO, "points": [{"no": k + 1, "code": O["stops"][k]["code"]} for k in points]}


def _shift(clock, m):
    if not clock or not m:
        return clock
    h, mm = map(int, clock.split(":"))
    t = (h * 60 + mm + int(round(m))) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"


# ============================================================================================= V15.5 OS BUS PUT HALFWAY INTO A PROLONGED HEADWAY
def plan_os(ctx, reach):
    """An extra OS bus + Bus Captain is put halfway into the prolonged headway of ONE direction (the one on screen).
    ctx: now, X {dir, stops, ss, buses[{id, s}], H} = the direction with the gap, Y = the other direction (None = loop),
         os {t0, label, lat, lon}, late {bus, delay} or None (optional: simulate one bus running late), gap_rear (bus id, optional), P.
    reach: {j: (minutes, km, source)} off-service by real road from the OS start point to stop j of X.
    For every stop ahead of the rear bus of the gap: the OS bus aims at the middle of the gap (front + rear passing times / 2);
    recommended = the first stop where it can be there by then (it stands by if early) - the most stops of the gap served."""
    P = {**P0, **(ctx.get("P") or {})}
    now, X, Y = ctx["now"], ctx["X"], ctx.get("Y")
    SM, LAY = float(P["stop_min"]), max(float(P["layover"]), float(P["break_min"]))
    ss, stops, n = X["ss"], X["stops"], len(X["stops"])
    H = float(X.get("H") or 10.0)
    xl = f"D{X['dir']}"
    late = ctx.get("late") or {}
    Ln, LD = late.get("bus"), float(late.get("delay") or 0.0)
    units = []
    BRK_ = float(P["break_min"])
    for b in X["buses"]:                                            # buses on the direction now
        q = prog(ss, b["s"])
        d_ = LD if (Ln is not None and b["id"] == Ln) else 0.0
        if b.get("wait") is not None:                               # in layover at the first stop, departing on schedule
            dep_ = now + float(b["wait"]) + d_
            units.append({"uid": f"X{b['id']}", "num": b["id"], "kind": "next_trip", "dep": dep_, "ready": max(now, dep_ - LAY + BRK_),
                          "label": f"Bus {b['id']} (at first stop)", "late": d_ > 0})
            continue
        units.append({"uid": f"X{b['id']}", "num": b["id"], "kind": "in_service", "q": q, "delay": d_, "dep": now - SM * q + d_,
                      "label": f"Bus {b['id']}", "late": d_ > 0})
    ob = Y["buses"] if Y else []
    oss = Y["ss"] if Y else ss
    for b in (ob if Y else [b_ for b_ in X["buses"] if b_.get("wait") is None]):   # their next trips on this direction (circulation)
        q = prog(oss, b["s"])
        arr = now + float(b.get("wait") or 0.0) + SM * ((len(oss) - 1) - q) + (LD if (not Y and Ln is not None and b["id"] == Ln) else 0.0)
        units.append({"uid": f"N{b['id']}", "num": b["id"], "kind": "next_trip", "dep": arr + LAY, "ready": arr + float(P["break_min"]),
                      "label": f"Bus {b['id']}" + (f" (from D{Y['dir']})" if Y else " (next loop)")})
    units.sort(key=lambda u: u["dep"])

    def times(u, entry=None):
        if entry is not None:
            j, te = entry
            return {k: te + SM * (k - j) for k in range(j, n)}
        if u["kind"] == "in_service":
            return {k: now + SM * (k - u["q"]) + (u["delay"] if k > u["q"] else 0.0) for k in range(n)}
        return {k: u["dep"] + SM * k for k in range(n)}

    step = max(1, (n - 2) // int(P["n_points"]))
    points = list(range(1, n, step))
    if n - 1 not in points:
        points.append(n - 1)
    TRIM = bool(P.get("edge_trim")); EDGE = max(2.5 * H, float(P.get("edge_gap") or 0.0) + H)

    def evaluate(tab, keep_uids=()):
        vals, mx, mn, perk = [], 0.0, 1e9, {}
        for k in points:
            seq = sorted((t[k], uid) for uid, t in tab.items() if k in t)
            past = [x for x in seq if x[0] <= now]
            fut = [x for x in seq if x[0] > now]
            sq = ([past[-1]] if past else []) + fut
            gaps = [b[0] - a[0] for a, b in zip(sq, sq[1:])]
            if TRIM:
                gaps = [g for i, g in enumerate(gaps) if not (g > EDGE and sq[i][1] not in keep_uids and sq[i + 1][1] not in keep_uids)]
            if not gaps:
                continue
            e = ewt(gaps, H)
            if e is None:
                continue
            vals.append(e); mx = max(mx, max(gaps)); mn = min(mn, min(gaps)); perk[k] = (gaps, sq)
        return (sum(vals) / len(vals) if vals else None), mx, (mn if mn < 1e9 else None), perk

    base = {u["uid"]: times(u) for u in units}
    # ---- the prolonged headway: the largest gap between consecutive buses on the road or departing within the hour
    pairs = [(units[i], units[i + 1]) for i in range(len(units) - 1) if units[i + 1]["dep"] <= now + 60.0]
    if ctx.get("gap_rear") is not None:
        pairs = [p for p in pairs if p[1]["num"] == ctx["gap_rear"] and p[1]["kind"] == "in_service"] or pairs
    if TRIM:                                                         # test fleet: the seam between the two synthetic fleets is not a real gap
        pairs = [p for p in pairs if (p[1]["dep"] - p[0]["dep"]) <= EDGE] or pairs
    if not pairs:
        return {"ok": False, "error": f"Not enough buses on {xl} to find a headway gap."}
    F, R = max(pairs, key=lambda p: p[1]["dep"] - p[0]["dep"])
    gap0 = R["dep"] - F["dep"]
    iF = units.index(F)
    roles = {"B": F, "D": R}
    if iF > 0:
        roles["A"] = units[iF - 1]
    if iF + 2 < len(units):
        roles["E"] = units[iF + 2]
    for r, u in roles.items():
        u["role"] = r
    KEEP = {u["uid"] for u in roles.values()} | {"OS"}
    e0, mx0, mn0, per0 = evaluate(base, KEEP)
    if e0 is None:
        return {"ok": False, "error": f"Not enough buses to forecast {xl} headways."}
    risk = "HIGH" if gap0 >= 2.0 * H else "MEDIUM" if gap0 >= 1.5 * H else "LOW"
    t0 = max(now, float(ctx["os"]["t0"]))
    TOL = float(P["even_tol"])
    cands, infeasible = [], {"no_route": 0, "too_far": 0, "passed": 0, "behind_rear": 0}
    for j in range(1, n - 1):
        tF, tR = base[F["uid"]][j], base[R["uid"]][j]
        if tR <= now + 0.5:
            infeasible["passed"] += 1                                # the rear bus has already passed - the gap is behind this stop
            continue
        rr = reach.get(j)
        if not rr:
            infeasible["no_route"] += 1
            continue
        rmin, rkm, rsrc = rr
        if rmin > float(P["max_reach_min"]):
            infeasible["too_far"] += 1
            continue
        e_j = t0 + rmin
        mid = (tF + tR) / 2.0
        te = max(e_j, mid)
        if te >= tR - 0.5:
            infeasible["behind_rear"] += 1
            continue
        tab = dict(base); tab["OS"] = times(None, entry=(j, te))
        e, mx, mn, per = evaluate(tab, KEEP)
        if e is None:
            continue
        imb = abs((te - tF) - (tR - te))
        dn = [k for k in points if k >= j and k in per]
        dmx = max((max(per[k][0]) for k in dn), default=mx)
        cands.append({"j": j, "no": j + 1, "code": stops[j]["code"], "name": stops[j]["name"], "lat": stops[j]["lat"], "lon": stops[j]["lon"],
                      "skip": j, "avoided": _r(SM * j), "os_min": _r(rmin), "os_km": _r(rkm, 2), "os_src": rsrc,
                      "ready": hhmm(t0), "leave": hhmm(te - rmin), "wait": _r(te - e_j), "earliest": hhmm(e_j), "entry": _r(te), "entry_clock": hhmm(te),
                      "net": None, "served": n - 1 - j,
                      "gap": {"front": F["label"], "front_role": "B", "rear": R["label"], "rear_role": "D", "t_front": hhmm(tF), "t_rear": hhmm(tR),
                              "minutes": _r(tR - tF), "minutes_raw": None, "hold": 0.0, "mid": hhmm(mid), "split": [_r(te - tF), _r(tR - te)],
                              "imbalance": _r(imb), "even": imb <= TOL},
                      "front": {"uid": F["uid"], "num": F["num"], "role": "B", "label": F["label"], "pass": hhmm(tF), "adj": 0.0},
                      "rear": {"uid": R["uid"], "num": R["num"], "role": "D", "label": R["label"], "pass": hhmm(tR), "adj": 0.0},
                      "slows": [], "rear_log": [], "dep_adj": [], "use_adj": False,
                      "gap_before": _r(te - tF), "gap_after": _r(tR - te), "max_hw": _r(dmx), "max_hw_all": _r(mx), "ewt": round(e, 3), "score": e,
                      "gain": round(e0 - e, 3), "km_skipped": _r(ss[j], 2), "adj": {}, "_tab": tab, "_imb": imb})
    # ---- FULL TRIP option: the OS bus runs the whole route as an extra departure from the first stop, and the departures around it
    # are re-spaced by headway (buses ahead later, buses behind earlier within their layover, the OS bus in between)
    full = None
    rr0 = reach.get(0) or (0.0, 0.0, "starts at the first stop")
    t_os0 = t0 + rr0[0]
    dseq = sorted(units, key=lambda u: u["dep"])
    fpairs = [(dseq[i], dseq[i + 1]) for i in range(len(dseq) - 1) if dseq[i + 1]["dep"] > t_os0 + 0.5 and dseq[i + 1]["dep"] <= now + 90.0]
    if TRIM:                                                         # test fleet: skip the edge of the entered fleet (not a real gap)
        fpairs = [p for p in fpairs if (p[1]["dep"] - p[0]["dep"]) <= EDGE]
    if fpairs:
        Fp, Rp = max(fpairs, key=lambda p: (round(p[1]["dep"] - p[0]["dep"], 1), -p[0]["dep"]))
        iF_ = dseq.index(Fp)
        order = [u["uid"] for u in dseq[:iF_ + 1]] + ["OS"] + [u["uid"] for u in dseq[iF_ + 1:]]
        U = {u["uid"]: u for u in units}
        dep = {u["uid"]: u["dep"] for u in units}
        dep["OS"] = max(t_os0, (Fp["dep"] + Rp["dep"]) / 2.0)
        lo_, hi_, why_fix = {"OS": t_os0}, {"OS": Rp["dep"]}, {}
        io = order.index("OS")
        win_ids = order[max(0, io - 2):io] + order[io + 1:io + 3]
        for w in win_ids:
            u = U[w]
            if u["kind"] != "next_trip" or u["dep"] <= now:
                lo_[w] = hi_[w] = u["dep"]; why_fix[w] = "already departed"
            elif order.index(w) < io:
                lo_[w], hi_[w] = u["dep"], u["dep"] + float(P["dep_later_max"])
            else:
                lo_[w], hi_[w] = max(u["ready"], now, u["dep"] - float(P["dep_earlier_max"])), u["dep"]
                if lo_[w] >= hi_[w] - 0.5:
                    why_fix[w] = "late \u2013 no layover to spare"
        for _ in range(300):
            mv = 0.0
            for w in ["OS"] + win_ids:
                i = order.index(w)
                if i == 0 or i == len(order) - 1:
                    continue
                pv, nv = dep[order[i - 1]], dep[order[i + 1]]
                want = min(max((pv + nv) / 2.0, lo_[w], pv), hi_[w], nv)
                mv = max(mv, abs(want - dep[w])); dep[w] = want
            if mv < 1e-4:
                break
        adj_f = {w: float(round(dep[w] - U[w]["dep"])) for w in win_ids if abs(round(dep[w] - U[w]["dep"])) >= 1}
        dep_os = dep["OS"]
        tabf = dict(base)
        for w, d_ in adj_f.items():
            tabf[w] = {k: t + d_ for k, t in base[w].items()}
        tabf["OS"] = {k: dep_os + SM * k for k in range(n)}
        ef, mxf, mnf, perf = evaluate(tabf, {"OS"} | set(adj_f))
        if ef is not None:
            ringf = []
            letters = ["A", "B"][-len(order[max(0, io - 2):io]):] if io > 0 else []
            for w, lt in zip(order[max(0, io - 2):io], letters):
                ringf.append({**U[w], "role": lt})
            ringf.append({"uid": "OS", "num": "OS", "role": "OS", "label": "OS bus", "kind": "os"})
            for w, lt in zip(order[io + 1:io + 3], ["D", "E"]):
                ringf.append({**U[w], "role": lt})
            fr_ = next((u for u in ringf if u["uid"] == Fp["uid"]), None)
            ra_ = next((u for u in ringf if u["uid"] == Rp["uid"]), None)
            tF_, tR_ = dep[Fp["uid"]], dep[Rp["uid"]]
            adj_list_f = []
            for u in ringf:
                if u["uid"] == "OS":
                    continue
                d_ = adj_f.get(u["uid"], 0.0)
                adj_list_f.append({"role": u["role"], "uid": u["uid"], "num": u["num"], "label": u["label"], "dep": hhmm(U[u["uid"]]["dep"]),
                                   "dep_new": hhmm(U[u["uid"]]["dep"] + d_), "adj": d_,
                                   "reason": why_fix.get(u["uid"]) or ("no change needed" if not d_ else "")})
            ichw0 = [U[order[i + 1]]["dep"] - U[order[i]]["dep"] for i in range(max(0, io - 3), min(len(order) - 1, io + 3)) if order[i] != "OS" and order[i + 1] != "OS"]
            ichw1 = [dep[order[i + 1]] - dep[order[i]] for i in range(max(0, io - 3), min(len(order) - 1, io + 3))]
            full = {"j": 0, "no": 1, "code": stops[0]["code"], "name": stops[0]["name"], "lat": stops[0]["lat"], "lon": stops[0]["lon"], "full": True,
                    "skip": 0, "avoided": 0.0, "os_min": _r(rr0[0]), "os_km": _r(rr0[1], 2), "os_src": rr0[2],
                    "ready": hhmm(t0), "leave": hhmm(dep_os - rr0[0]), "wait": _r(dep_os - t_os0), "earliest": hhmm(t_os0), "entry": _r(dep_os), "entry_clock": hhmm(dep_os),
                    "net": None, "served": n - 1,
                    "gap": {"front": Fp["label"], "front_role": fr_["role"] if fr_ else None, "rear": Rp["label"], "rear_role": ra_["role"] if ra_ else None,
                            "t_front": hhmm(tF_), "t_rear": hhmm(tR_), "minutes": _r(Rp["dep"] - Fp["dep"]), "minutes_raw": None, "hold": 0.0,
                            "mid": hhmm((tF_ + tR_) / 2.0), "split": [_r(dep_os - tF_), _r(tR_ - dep_os)], "imbalance": _r(abs((dep_os - tF_) - (tR_ - dep_os))),
                            "even": abs((dep_os - tF_) - (tR_ - dep_os)) <= TOL},
                    "front": {"uid": Fp["uid"], "num": Fp["num"], "role": fr_["role"] if fr_ else None, "label": Fp["label"], "pass": hhmm(tF_), "adj": adj_f.get(Fp["uid"], 0.0)},
                    "rear": {"uid": Rp["uid"], "num": Rp["num"], "role": ra_["role"] if ra_ else None, "label": Rp["label"], "pass": hhmm(tR_), "adj": adj_f.get(Rp["uid"], 0.0)},
                    "slows": [], "rear_log": [], "dep_adj": adj_list_f, "use_adj": bool(adj_f), "ic_hw_before": [_r(h) for h in ichw0], "ic_hw_after": [_r(h) for h in ichw1],
                    "gap_before": _r(dep_os - tF_), "gap_after": _r(tR_ - dep_os), "max_hw": _r(mxf), "max_hw_all": _r(mxf), "ewt": round(ef, 3), "score": ef,
                    "gain": round(e0 - ef, 3), "km_skipped": 0.0, "adj": dict(adj_f), "_tab": tabf, "_imb": 0.0, "_ring": ringf, "_adj": dict(adj_f)}
    # recommended: the first stop where the OS bus reaches the middle of the gap (serves the most of it); else the most even
    cands.sort(key=lambda x: (0 if x["_imb"] <= TOL else 1, x["_imb"] if x["_imb"] > TOL else 0.0, x["j"]))
    prolonged = gap0 >= 1.5 * H
    near_start = bool(cands) and cands[0]["j"] <= max(2, int(0.1 * (n - 1)))
    full_rec = full is not None and (not prolonged or not cands or near_start)
    if full is not None:
        full["why_full"] = ("no prolonged headway" if not prolonged else "halfway point right at the start" if near_start else
                            "no halfway stop is feasible" if not cands else "")
        cands = ([full] + cands) if full_rec else (cands + [full])
    for i, x in enumerate(cands, 1):
        x["rank"] = i
    er = sorted(cands, key=lambda y: (y["score"], y["j"]))
    for x in cands:
        x["ewt_rank"] = er.index(x) + 1
    best = cands[0] if cands else None
    best_ewt = er[0] if er else None
    ring = [roles[r] for r in ("A", "B") if r in roles] + [{"uid": "OS", "num": "OS", "role": "OS", "label": "OS bus", "kind": "os"}] + [roles[r] for r in ("D", "E") if r in roles]

    def seq_at(tab, k, ring_=None, adj=None):
        out = []
        for u in (ring_ or ring):
            t = tab.get(u["uid"], {}).get(k)
            if t is not None:
                out.append({"uid": u["uid"], "num": u["num"], "role": u["role"], "t": _r(t), "clock": hhmm(t), "label": u["label"], "adj": (adj or {}).get(u["uid"], 0.0)})
        return sorted(out, key=lambda z: z["t"])

    def detail(x):
        tab, j = x["_tab"], x["j"]
        cols = [j] + [k for k in (j + 5, j + 10, j + 15) if k < n]
        hwcol = []
        for k in cols:
            sq = sorted((tt[k], uid) for uid, tt in tab.items() if k in tt)
            hw_ = {uid: (t - sq[i - 1][0]) if i > 0 else None for i, (t, uid) in enumerate(sq)}
            if TRIM:
                for i in range(1, len(sq)):
                    if (sq[i][0] - sq[i - 1][0]) > EDGE and sq[i][1] not in KEEP and sq[i - 1][1] not in KEEP:
                        hw_[sq[i][1]] = None
            hwcol.append(hw_)
        rows = []
        rg, ad = x.get("_ring") or ring, x.get("_adj") or {}
        for u in rg:
            t = tab.get(u["uid"], {})
            hws = [hwcol[i].get(u["uid"]) for i in range(len(cols))]
            rows.append({"role": u["role"], "num": u["num"], "label": u["label"], "halfway": u["uid"] == "OS",
                         "dep": (hhmm(tab["OS"][0]) if x.get("full") else None) if u["uid"] == "OS" else hhmm(u["dep"] + ad.get(u["uid"], 0.0)),
                         "adj": ad.get(u["uid"], 0.0), "slow": False,
                         "pass": [hhmm(t.get(k)) if t.get(k) is not None else None for k in cols], "t": [_r(t.get(k)) if t.get(k) is not None else None for k in cols],
                         "hw": [_r(h) for h in hws], "max_hw": _r(max([h for h in hws if h is not None], default=0.0)) if any(h is not None for h in hws) else None})
        return {"cols": [{"j": k, "no": k + 1, "code": stops[k]["code"], "name": stops[k]["name"]} for k in cols], "rows": rows,
                "before": seq_at(base, j, rg), "after": seq_at(tab, j, rg, ad)}

    def why_full(x):
        parts = []
        for a_ in x["dep_adj"]:
            if a_["adj"] > 0:
                parts.append(f"{a_['label']} departs {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']})")
            elif a_["adj"] < 0:
                parts.append(f"{a_['label']} departs {-a_['adj']:.0f} min earlier ({a_['dep']} \u2192 {a_['dep_new']})")
        fixed = [f"{a_['label']} not moved ({a_['reason']})" for a_ in x["dep_adj"] if not a_["adj"] and a_["reason"] not in ("", "no change needed")]
        head = {"no prolonged headway": f"There is no prolonged headway on {xl} (largest gap {gap0:.0f} min, target {H:.0f}), so a halfway entry would only split a normal gap. ",
                "halfway point right at the start": f"The best halfway entry would be right at the start of the route, so a full trip serves more stops. ",
                "no halfway stop is feasible": f"The OS bus cannot reach the middle of the {gap0:.0f}-min gap anywhere in time. "}.get(x.get("why_full"), "")
        s = head + (f"Deploy the OS bus as a FULL TRIP from {stops[0]['name']}, departing {x['entry_clock']} between {x['gap']['front']} and {x['gap']['rear']}. ")
        if parts:
            s += "The departures around it are re-spaced by headway: " + "; ".join(parts) + ". "
        if fixed:
            s += " ".join(fixed) + ". "
        hw1 = x.get("ic_hw_after") or []
        if hw1:
            s += "Departure headways become " + " / ".join(f"{h:.0f}" for h in hw1) + " min. "
        s += f"{xl} EWT {e0:.3f} \u2192 {x['ewt']:.3f} min; the OS bus serves all {n - 1} stops."
        return s

    def actions_full(x):
        steps, k = [], 1
        for a_ in [a for a in x["dep_adj"] if a["role"] in ("A", "B")]:
            steps.append({"n": k, "bus": a_["label"], "role": a_["role"], "bus_label": f"Bus {a_['role']} ({a_['num']})", "kind": "hold" if a_["adj"] > 0 else "monitor",
                          "text": (f"Depart {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']}) to even the headway." if a_["adj"] > 0 else f"Depart as planned ({a_['dep']}) \u2013 {a_['reason'] or 'no change needed'}."),
                          "time": a_["dep_new"]}); k += 1
        steps.append({"n": k, "bus": "OS bus + Bus Captain", "role": "OS", "kind": "halfway",
                      "text": f"Ready at {ctx['os'].get('label')} {x['ready']}." + (f" Off-service {x['os_min']:.0f} min to {stops[0]['name']}." if (x.get('os_km') or 0) > 0.3 else "")
                              + f" Depart {stops[0]['name']} at {x['entry_clock']} as a full trip ({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}, {x['gap']['split'][1]:.0f} min ahead of {x['gap']['rear']}).",
                      "time": x["entry_clock"]}); k += 1
        for a_ in [a for a in x["dep_adj"] if a["role"] in ("D", "E")]:
            steps.append({"n": k, "bus": a_["label"], "role": a_["role"], "bus_label": f"Bus {a_['role']} ({a_['num']})", "kind": "advance" if a_["adj"] < 0 else "monitor",
                          "text": (f"Depart {-a_['adj']:.0f} min earlier ({a_['dep']} \u2192 {a_['dep_new']}) to even the headway." if a_["adj"] < 0 else f"Depart as planned ({a_['dep']}) \u2013 {a_['reason'] or 'no change needed'}."),
                          "time": a_["dep_new"]}); k += 1
        return steps

    def why(x):
        g = x["gap"]
        s = (f"The prolonged headway on {xl} is between {F['label']} and {R['label']}: {gap0:.0f} min (target {H:.0f}). "
             + (f"It includes the simulated {LD:g}-min lateness of Bus {Ln}. " if Ln is not None and LD else "")
             + f"At Stop {x['no']} (BS {x['code']}) they are forecast at {g['t_front']} and {g['t_rear']}, so the middle of the gap is {g['mid']}. "
             f"The OS bus is available at {ctx['os'].get('label')} from {x['ready']}; the real road route to Stop {x['no']} takes about {x['os_min']:.0f} min "
             f"({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
        if x["wait"] and x["wait"] >= 0.5:
            s += f"It stands by {x['wait']:.0f} min and leaves at {x['leave']} to enter exactly mid-gap at {x['entry_clock']}. "
        s += f"That splits the gap {g['split'][0]:.0f} / {g['split'][1]:.0f} min. "
        if g["even"]:
            s += (f"Stop {x['no']} is the first stop ahead of {R['label']} where the OS bus can reach the middle of the gap, so it serves the most of the gap "
                  f"({x['served']} stops to the end). ")
        else:
            s += f"No stop lets the OS bus reach the middle in time; Stop {x['no']} gives the most even split. "
        s += f"{xl} EWT {e0:.3f} \u2192 {x['ewt']:.3f} min; largest headway after entry {x['max_hw']:.0f} min."
        return s

    def actions(x):
        steps = [{"n": 1, "bus": "OS bus + Bus Captain", "role": "OS", "kind": "halfway",
                  "text": f"Ready at {ctx['os'].get('label')} {x['ready']}." + (f" Stand by {x['wait']:.0f} min." if x["wait"] and x["wait"] >= 0.5 else "")
                          + f" Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), enter {xl} at {x['entry_clock']} "
                            f"({x['gap']['split'][0]:.0f} min behind {F['label']}, {x['gap']['split'][1]:.0f} min ahead of {R['label']}). Continue to the final stop.",
                  "time": x["entry_clock"]}]
        k = 2
        for r in ("A", "B", "D", "E"):
            if r in roles:
                u = roles[r]
                steps.append({"n": k, "bus": u["label"], "role": r, "kind": "monitor",
                              "text": "Keep normal running." + (" Front of the gap." if r == "B" else " Rear of the gap." if r == "D" else " Monitor headway."), "time": None}); k += 1
        return steps

    for x in cands:
        x["detail"] = detail(x)
        x["why"] = why_full(x) if x.get("full") else why(x)
        x["actions"] = actions_full(x) if x.get("full") else actions(x)
    ring0 = [roles[r] for r in ("A", "B", "D", "E") if r in roles]
    noaction = {"ewt": round(e0, 3), "max_hw": _r(mx0), "min_hw": _r(mn0), "risk": risk,
                "seq": [{"role": u["role"], "num": u["num"], "label": u["label"], "dep": _r(u["dep"]), "clock": hhmm(u["dep"]), "late": bool(u.get("late")),
                         "in_service": u["kind"] == "in_service"} for u in ring0]}
    strip = lambda x: {k: v for k, v in x.items() if not k.startswith("_") and k != "score"}
    return {"ok": True, "model": MODEL + "+os-halfway", "full_rec": bool(full_rec), "prolonged": bool(prolonged), "mode": "os", "now": hhmm(now), "loop": Y is None, "late_dir": X["dir"], "next_dir": X["dir"],
            "H": _r(H), "H_src": X.get("H_src"), "stop_min": SM, "break_min": float(P["break_min"]), "layover": LAY,
            "late_bus": Ln, "delay": LD, "c_arr": None, "c_ready": None, "c_full_dep": None,
            "os": {"label": ctx["os"].get("label"), "ready": hhmm(t0), "lat": ctx["os"].get("lat"), "lon": ctx["os"].get("lon")},
            "gap_info": {"front": F["label"], "rear": R["label"], "minutes": _r(gap0), "front_kind": F["kind"], "rear_kind": R["kind"]},
            "interchange": {"code": stops[0]["code"], "name": stops[0]["name"], "lat": stops[0]["lat"], "lon": stops[0]["lon"]},
            "roles": {r: {"num": u["num"], "label": u["label"], "kind": u["kind"]} for r, u in roles.items()},
            "no_action": noaction, "candidates": [strip(x) for x in cands], "best": best["code"] if best else None,
            "best_ewt": best_ewt["code"] if best_ewt else None, "even_tol": TOL, "dep_adj": [], "test": TRIM, "ic_hw_before": [], "ic_hw_after": [],
            "infeasible": infeasible, "n_stops": n, "points": [{"no": k + 1, "code": stops[k]["code"]} for k in points]}
