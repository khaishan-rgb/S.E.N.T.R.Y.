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
MODEL = "hwplan-15.2-interchange"


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
    t_buses = sorted(({"num": b["id"], "p": prog(T["ss"], b["s"])} for b in T["buses"]), key=lambda x: -x["p"])   # front first
    if not any(b["num"] == Cn for b in t_buses):
        return {"ok": False, "error": "The selected bus is no longer in the live snapshot - refresh the live buses."}
    for b in t_buses:
        b["arr"] = now + SM * ((nT - 1) - b["p"]) + (D if b["num"] == Cn else 0.0)
        b["ready"] = b["arr"] + BRK
    c = next(i for i, b in enumerate(t_buses) if b["num"] == Cn)
    # ---- next direction: buses already on it (in service), then the next trip of every current-direction bus, in scheduled order
    units = []
    o_cur = [] if loop else sorted(({"num": b["id"], "q": prog(O["ss"], b["s"])} for b in O["buses"]), key=lambda x: -x["q"])
    for b in o_cur:
        units.append({"uid": f"O{b['num']}", "num": b["num"], "kind": "in_service", "q": b["q"], "dep": now - SM * b["q"], "movable": False,
                      "label": f"{ol} Bus {b['num']}", "src": f"in service on {ol}"})
    LAY = max(float(P["layover"]), BRK)                            # scheduled layover: planned departure = on-time arrival + layover
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
    EDGE = 2.5 * H
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
        for u in units:
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
    seq_ids = [u["uid"] for u in units if u["uid"] != C["uid"]]
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
        others = sorted((t[j], uid) for uid, t in tabbase.items() if j in t and uid != C["uid"])
        f = (tabbase[Bu["uid"]][j], Bu["uid"]) if (Bu and j in tabbase[Bu["uid"]]) else max((x for x in others if x[0] <= e_j), default=None)
        if not f:
            return None
        nx = min((x for x in others if x[0] > f[0]), default=None) or (f[0] + 2.0 * H, None)
        mid = (f[0] + nx[0]) / 2.0
        te = max(e_j, mid)
        if te >= nx[0] - 0.5:
            return "behind"
        tab = dict(tabbase); tab[C["uid"]] = times(C, entry=(j, te))
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
        e_j = C["ready"] + rmin                                  # earliest Bus C can be at this stop
        if e_j >= full_dep + SM * j - 0.5:
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
        te, tab, f, nx = pl["te"], pl["tab"], pl["f"], pl["nx"]
        fu = U[f[1]]
        behind = sorted((t[j], uid) for uid, t in tab.items() if j in t and t[j] > te + 1e-6 and uid != C["uid"])
        ru = U[behind[0][1]] if behind else None
        tp = lambda u: tab[u["uid"]].get(j) if u else None
        dn_pts = [k for k in points if k >= j]
        dn_mx = max((max(pl["per"][k][0]) for k in dn_pts if k in pl["per"]), default=None)
        wait = te - e_j
        J = pl["e"] + float(P["adj_cost"]) * sum(abs(v) for v in dep_adj.values())
        key = (0 if pl["imb"] <= TOL else 1, pl["imb"] if pl["imb"] > TOL else 0.0, J)
        cands.append({"j": j, "no": j + 1, "code": O["stops"][j]["code"], "name": O["stops"][j]["name"], "lat": O["stops"][j]["lat"], "lon": O["stops"][j]["lon"],
                      "skip": j, "avoided": _r(SM * j), "os_min": _r(rmin), "os_km": _r(rkm, 2), "os_src": rsrc,
                      "ready": hhmm(C["ready"]), "leave": hhmm(C["ready"] + wait), "wait": _r(wait), "earliest": hhmm(e_j),
                      "entry": _r(te), "entry_clock": hhmm(te), "net": _r(SM * j - rmin),
                      "gap": {"front": fu["label"], "front_role": fu.get("role"), "rear": U[nx[1]]["label"] if nx[1] else "the next trip (beyond the forecast)",
                              "rear_role": U[nx[1]].get("role") if nx[1] else None, "t_front": hhmm(f[0]), "t_rear": hhmm(nx[0]),
                              "minutes": _r(nx[0] - f[0]), "minutes_raw": _r(pl0["nx"][0] - pl0["f"][0]) if isinstance(pl0, dict) else None,
                              "hold": 0.0, "mid": hhmm(pl["mid"]), "split": [_r(te - f[0]), _r(nx[0] - te)], "imbalance": _r(pl["imb"]), "even": pl["imb"] <= TOL},
                      "front": {"uid": fu["uid"], "num": fu["num"], "role": fu.get("role"), "label": fu["label"], "pass": hhmm(tp(fu)), "adj": dep_adj.get(fu["uid"], 0.0),
                                "dep": hhmm(fu["dep"]), "movable": fu["movable"]},
                      "rear": ({"uid": ru["uid"], "num": ru["num"], "role": ru.get("role"), "label": ru["label"], "pass": hhmm(tp(ru)), "adj": dep_adj.get(ru["uid"], 0.0),
                                "dep": hhmm(ru["dep"])} if ru else None),
                      "slows": [], "rear_log": [], "dep_adj": adj_list,
                      "ewt_noadj": round(pl0["e"], 3) if isinstance(pl0, dict) else None,
                      "gap_before": _r(te - tp(fu)) if tp(fu) is not None else None, "gap_after": _r(tp(ru) - te) if ru and tp(ru) is not None else None,
                      "max_hw": _r(dn_mx if dn_mx is not None else pl["mx"]), "max_hw_all": _r(pl["mx"]), "ewt": round(pl["e"], 3), "score": J,
                      "gain": round(e0 - pl["e"], 3), "km_skipped": _r(O["ss"][j], 2), "rkey": key + (j,),
                      "adj": dict(dep_adj), "_tab": tab, "_adj": dict(dep_adj)})
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
            if adj is not None and r == "C":
                continue                                          # Bus C does not depart from the interchange - it goes halfway
            d_ = (adj or {}).get(u["uid"], 0.0)
            out.append({"role": r, "num": u["num"], "label": u["label"], "t": _r(u["dep"] + d_), "clock": hhmm(u["dep"] + d_), "adj": d_,
                        "in_service": u["kind"] == "in_service", "late": u["uid"] == C["uid"]})
        return out

    def detail(x):
        tab, adj, j = x["_tab"], x["_adj"], x["j"]
        cols = [j] + [k for k in (j + 5, j + 10, j + 15) if k < nO]
        ring = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
        rows = []
        for u in ring:
            t = tab.get(u["uid"], {})
            rows.append({"role": u["role"], "num": u["num"], "label": u["label"], "halfway": u["uid"] == C["uid"],
                         "dep": None if u["uid"] == C["uid"] else hhmm((u["dep"] + adj.get(u["uid"], 0.0)) if u["kind"] == "next_trip" else u["dep"]),
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

    def adj_sentence():
        parts = []
        for a_ in adj_list:
            if a_["adj"] > 0:
                parts.append(f"{a_['label']} departs {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']})")
            elif a_["adj"] < 0:
                parts.append(f"{a_['label']} departs {-a_['adj']:.0f} min earlier ({a_['dep']} \u2192 {a_['dep_new']})")
        fixed = [f"{a_['label']} not adjusted ({a_['reason']})" for a_ in adj_list if not a_["adj"] and a_["reason"] not in ("", "no change needed")]
        return parts, fixed

    def why(x):
        g = x["gap"]
        parts, fixed = adj_sentence()
        s = ""
        if parts:
            s += (f"Bus {Cn}'s departure slot at {O['stops'][0]['name']} is empty, so the interchange headway is closed by spacing the buses around it evenly: "
                  + "; ".join(parts) + ". ")
        if fixed:
            s += " ".join(fixed) + ". "
        s += (f"At Stop {x['no']} the headway Bus {Cn} has to fill is then between {g['front']} ({g['t_front']}) and {g['rear']} ({g['t_rear']}): {g['minutes']:.0f} min"
              + (f" (was {g['minutes_raw']:.0f})" if g.get("minutes_raw") and abs(g["minutes_raw"] - g["minutes"]) >= 1 else "")
              + f". Half of it puts Bus {Cn} at {g['mid']}, leaving {g['split'][0]:.0f} / {g['split'][1]:.0f} min. ")
        s += (f"Bus {Cn} completes {tl} at {hhmm(t_buses[c]['arr'])} ({D:g} min late), is ready at {x['ready']} after the {BRK:g}-min break, and the real road route "
              f"to Stop {x['no']} (BS {x['code']}) takes about {x['os_min']:.0f} min ({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
        if x["wait"] and x["wait"] >= 0.5:
            s += f"It therefore leaves the interchange {x['wait']:.0f} min later ({x['leave']}) so it enters exactly mid-gap at {x['entry_clock']}. "
        if g["even"]:
            s += f"Stop {x['no']} is the first stop where Bus {Cn} can reach the middle of the gap, so it skips the fewest stops ({x['skip']}). "
        else:
            s += f"No stop lets Bus {Cn} reach the middle in time; Stop {x['no']} gives the most even split. "
        s += f"Downstream {ol} EWT {x['ewt']:.3f} min (no action {e0:.3f}"
        if x.get("ewt_noadj") is not None and parts:
            s += f"; {x['ewt_noadj']:.3f} with the halfway alone, without the interchange adjustments"
        s += f"); largest headway after entry {x['max_hw']:.0f} min."
        if best_ewt and best_ewt["code"] != x["code"]:
            s += (f" Stop {best_ewt['no']} has the lowest whole-route EWT ({best_ewt['ewt']:.3f}) because Bus {Cn} serves more stops there, "
                  f"but it splits the gap {best_ewt['gap']['split'][0]:.0f} / {best_ewt['gap']['split'][1]:.0f} min.")
        return s

    def actions(x):
        steps, n = [], 1
        for a_ in [a for a in adj_list if a["role"] in ("A", "B")]:
            if a_["adj"] > 0:
                steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "hold", "text": f"Depart the interchange {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']}) to close the headway gap.", "time": a_["dep_new"]})
            else:
                steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "monitor", "text": f"Depart as planned ({a_['dep']}) \u2013 {a_['reason'] or 'no change needed'}.", "time": a_["dep"]})
            n += 1
        wtxt = f" Extend the break by {x['wait']:.0f} min" if x["wait"] and x["wait"] >= 0.5 else ""
        steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "halfway",
                      "text": f"Complete {tl}. Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}. Break until {x['ready']}.{wtxt}"
                              f"{'.' if wtxt else ''} Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive and enter {ol} at {x['entry_clock']} "
                              f"({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}). Continue to the final stop.",
                      "time": x["entry_clock"]}); n += 1
        for a_ in [a for a in adj_list if a["role"] in ("D", "E")]:
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
    return {"ok": True, "model": MODEL, "now": hhmm(now), "loop": loop, "late_dir": T["dir"], "next_dir": O["dir"], "H": _r(H), "H_src": O.get("H_src") or T.get("H_src"),
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
