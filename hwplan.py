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

MODEL = "hwplan-15.0"
P0 = {"stop_min": 2.0, "break_min": 7.0, "hold_max": 5.0, "adv_max": 5.0, "adj_cost": 0.002, "n_points": 12, "max_reach_min": 60.0}


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
    if o_cur:                                                     # the timetable continues after the last bus already on the next direction
        anchor = max(u["dep"] for u in units) + H
    else:
        others = sorted(b["ready"] - i * H for i, b in enumerate(t_buses) if i != c)
        anchor = others[len(others) // 2] if others else t_buses[c]["ready"] - c * H
    for i, b in enumerate(t_buses):
        slot = anchor + i * H
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

    def evaluate(tab):
        """tab: {uid: {k: t}} -> (EWT, max headway, min headway, per-point headways)"""
        vals, mx, mn, perk = [], 0.0, 1e9, {}
        for k in points:
            seq = sorted((t[k], uid) for uid, t in tab.items() if k in t)
            past = [x for x in seq if x[0] <= now]
            fut = [x for x in seq if x[0] > now]
            sq = ([past[-1]] if past else []) + fut
            gaps = [b[0] - a[0] for a, b in zip(sq, sq[1:])]
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

    cands, infeasible = [], {"no_route": 0, "too_far": 0, "no_benefit": 0}
    for j in range(1, nO - 1):
        rr = reach.get(j)
        if not rr:
            infeasible["no_route"] += 1
            continue
        rmin, rkm, rsrc = rr
        if rmin > float(P["max_reach_min"]):
            infeasible["too_far"] += 1
            continue
        te = C["ready"] + rmin                                   # earliest possible halfway entry
        if te >= full_dep + SM * j - 0.5:
            infeasible["no_benefit"] += 1                        # the full trip from the interchange would get there first
            continue
        tab = dict(base); tab[C["uid"]] = times(C, entry=(j, te))
        # the buses immediately ahead / behind at the entry stop
        sq = sorted((t[j], uid) for uid, t in tab.items() if j in t and uid != C["uid"])
        front = max((x for x in sq if x[0] <= te), default=None)
        rear = min((x for x in sq if x[0] > te), default=None)
        fu = next((u for u in units if front and u["uid"] == front[1]), None)
        ru = next((u for u in units if rear and u["uid"] == rear[1]), None)
        holds = [0.0] + ([float(x) for x in range(1, int(P["hold_max"]) + 1)] if fu and fu["movable"] and fu["dep"] > now else [])
        advs = [0.0] + ([float(x) for x in range(1, int(min(P["adv_max"], ru.get("slack", 0.0))) + 1)] if ru and ru["movable"] and ru["dep"] > now else [])
        best = None
        for h in holds:
            for a in advs:
                t2 = dict(tab)
                if h:
                    t2[fu["uid"]] = times(fu, dep=fu["dep"] + h)
                if a:
                    t2[ru["uid"]] = times(ru, dep=ru["dep"] - a)
                e, mx, mn, per = evaluate(t2)
                if e is None:
                    continue
                J = e + float(P["adj_cost"]) * (h + a)
                if best is None or J < best["J"] - 1e-9:
                    best = {"J": J, "e": e, "mx": mx, "mn": mn, "per": per, "tab": t2, "h": h, "a": a}
        if not best:
            continue
        adj = {}
        if best["h"]:
            adj[fu["uid"]] = best["h"]
        if best["a"]:
            adj[ru["uid"]] = -best["a"]
        tp = lambda u: best["tab"][u["uid"]].get(j) if u else None
        # downstream headways from the entry stop (the plan's own quality after Bus C enters)
        dn_pts = [k for k in points if k >= j]
        dn_mx = max((max(best["per"][k][0]) for k in dn_pts if k in best["per"]), default=None)
        cands.append({"j": j, "no": j + 1, "code": O["stops"][j]["code"], "name": O["stops"][j]["name"], "lat": O["stops"][j]["lat"], "lon": O["stops"][j]["lon"],
                      "skip": j, "avoided": _r(SM * j), "os_min": _r(rmin), "os_km": _r(rkm, 2), "os_src": rsrc, "ready": hhmm(C["ready"]),
                      "entry": _r(te), "entry_clock": hhmm(te), "net": _r(SM * j - rmin),
                      "front": ({"uid": fu["uid"], "num": fu["num"], "role": fu.get("role"), "label": fu["label"], "pass": hhmm(tp(fu)), "adj": best["h"],
                                 "dep": hhmm(fu["dep"]), "dep_new": hhmm(fu["dep"] + best["h"]) if fu["kind"] == "next_trip" else None, "movable": fu["movable"]} if fu else None),
                      "rear": ({"uid": ru["uid"], "num": ru["num"], "role": ru.get("role"), "label": ru["label"], "pass": hhmm(tp(ru)), "adj": -best["a"],
                                "dep": hhmm(ru["dep"]), "dep_new": hhmm(ru["dep"] - best["a"]), "slack": _r(ru.get("slack", 0.0))} if ru else None),
                      "gap_before": _r(te - tp(fu)) if fu else None, "gap_after": _r(tp(ru) - te) if ru else None,
                      "gap_nointervention": _r((tp(ru) + best["a"]) - (tp(fu) - best["h"])) if fu and ru else None,
                      "max_hw": _r(dn_mx if dn_mx is not None else best["mx"]), "max_hw_all": _r(best["mx"]), "ewt": round(best["e"], 3), "score": best["J"],
                      "gain": round(e0 - best["e"], 3), "km_skipped": _r(O["ss"][j], 2), "adj": {k: v for k, v in adj.items()}, "_tab": best["tab"], "_adj": adj})
    cands.sort(key=lambda x: (x["score"], x["os_min"]))
    for i, x in enumerate(cands, 1):
        x["rank"] = i
    best = cands[0] if cands else None

    # ---- detail for every candidate (so clicking any row changes the whole plan without another request)
    def detail(x):
        tab, adj, j = x["_tab"], x["_adj"], x["j"]
        cols = [j] + [k for k in (j + 5, j + 10, j + 15) if k < nO]
        ring = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
        rows = []
        for u in ring:
            t = tab.get(u["uid"], {})
            rows.append({"role": u["role"], "num": u["num"], "label": u["label"], "halfway": u["uid"] == C["uid"],
                         "dep": None if u["uid"] == C["uid"] else hhmm((u["dep"] + adj.get(u["uid"], 0.0)) if u["kind"] == "next_trip" else u["dep"]),
                         "adj": adj.get(u["uid"], 0.0), "pass": [hhmm(t.get(k)) if t.get(k) is not None else None for k in cols],
                         "t": [_r(t.get(k)) if t.get(k) is not None else None for k in cols]})
        # headway of each bus to the one ahead of it, at each column (full fleet, not only the five)
        hwcol = []
        for k in cols:
            sq = sorted((tt[k], uid) for uid, tt in tab.items() if k in tt)
            prev = {uid: (t - sq[i - 1][0]) if i > 0 else None for i, (t, uid) in enumerate(sq)}
            hwcol.append(prev)
        for row, u in zip(rows, ring):
            hws = [hwcol[ci_][u["uid"]] if u["uid"] in hwcol[ci_] else None for ci_ in range(len(cols))]
            row["hw"] = [_r(h) for h in hws]
            row["max_hw"] = _r(max([h for h in hws if h is not None], default=0.0)) if any(h is not None for h in hws) else None
        return {"cols": [{"j": k, "no": k + 1, "code": O["stops"][k]["code"], "name": O["stops"][k]["name"]} for k in cols], "rows": rows,
                "before": seq_at(base, j), "after": seq_at(tab, j, adj)}

    def why(x):
        fr, rr_ = x.get("front"), x.get("rear")
        s = (f"Bus {Cn} completes {tl} at {hhmm(t_buses[c]['arr'])} ({D:g} min late), takes the {BRK:g}-min break and is ready at {x['ready']}. "
             f"The real road route to Stop {x['no']} (BS {x['code']}) takes about {x['os_min']:.0f} min ({x['os_km']:.1f} km), so it can enter at {x['entry_clock']}. ")
        if fr and rr_:
            g0 = x["gap_nointervention"]
            s += (f"{fr['label']} is forecast to pass Stop {x['no']} at {_shift(fr['pass'], -fr['adj'])} and {rr_['label']} at {_shift(rr_['pass'], -rr_['adj'])}"
                  + (f" \u2013 a {g0:.0f}-min gap. " if g0 else ". "))
            s += f"Bus {Cn} enters between them, leaving {x['gap_before']:.0f} / {x['gap_after']:.0f} min. "
        acts = []
        if fr and fr["adj"]:
            acts.append(f"+{fr['adj']:.0f} min to {fr['label']} (depart {fr['dep_new']})")
        if rr_ and rr_["adj"]:
            acts.append(f"{rr_['adj']:.0f} min to {rr_['label']} (depart {rr_['dep_new']})")
        if acts:
            s += "Together with " + " and ".join(acts) + ", "
        else:
            s += "With no regulation of the buses around it, "
        s += f"this gives a downstream {ol} EWT of {x['ewt']:.3f} min (no action {e0:.3f}); largest headway after entry {x['max_hw']:.0f} min."
        if x["rank"] == 1:
            s += f" Lowest forecast EWT of the {len(cands)} feasible halfway points."
        return s

    def actions(x):
        steps, n = [], 1
        fr, rr_ = x.get("front"), x.get("rear")
        if fr and fr["adj"]:
            steps.append({"n": n, "bus": fr["label"], "role": fr.get("role"), "kind": "hold", "text": f"Hold and depart {fr['adj']:.0f} min later.", "time": fr["dep_new"]}); n += 1
        steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "halfway",
                      "text": f"Complete {tl}. Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}. Break {hhmm(t_buses[c]['arr'])}\u2013{x['ready']}. "
                              f"Depart off-service {x['ready']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive {x['entry_clock']}. Enter {ol} and continue to the final stop.",
                      "time": x["entry_clock"]}); n += 1
        if rr_ and rr_["adj"]:
            steps.append({"n": n, "bus": rr_["label"], "role": rr_.get("role"), "kind": "advance", "text": f"Advance departure by {-rr_['adj']:.0f} min if operationally feasible.", "time": rr_["dep_new"]}); n += 1
        for r in ("A", "B", "D", "E"):
            u = roles.get(r)
            if u and u["uid"] not in x["_adj"]:
                steps.append({"n": n, "bus": u["label"], "role": r, "kind": "monitor", "text": "No adjustment. Monitor downstream headway.", "time": None}); n += 1
        return steps

    for x in cands:
        x["detail"] = detail(x)
        x["why"] = why(x)
        x["actions"] = actions(x)
    ring0 = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
    noaction = {"ewt": round(e0, 3), "max_hw": _r(mx0), "min_hw": _r(mn0), "risk": risk,
                "seq": [{"role": u["role"], "num": u["num"], "label": u["label"], "dep": _r(u["dep"]), "clock": hhmm(u["dep"]), "late": u["uid"] == C["uid"],
                         "in_service": u["kind"] == "in_service"} for u in ring0]}
    strip = lambda x: {k: v for k, v in x.items() if not k.startswith("_") and k != "score"}
    return {"ok": True, "model": MODEL, "now": hhmm(now), "loop": loop, "late_dir": T["dir"], "next_dir": O["dir"], "H": _r(H), "H_src": O.get("H_src") or T.get("H_src"),
            "stop_min": SM, "break_min": BRK, "late_bus": Cn, "delay": D, "c_arr": hhmm(t_buses[c]["arr"]), "c_ready": hhmm(C["ready"]), "c_full_dep": hhmm(full_dep),
            "interchange": {"code": O["stops"][0]["code"], "name": O["stops"][0]["name"], "lat": O["stops"][0]["lat"], "lon": O["stops"][0]["lon"]},
            "roles": {r: {"num": u["num"], "label": u["label"], "kind": u["kind"]} for r, u in roles.items()},
            "no_action": noaction, "candidates": [strip(x) for x in cands], "best": best["code"] if best else None,
            "infeasible": infeasible, "n_stops": nO, "points": [{"no": k + 1, "code": O["stops"][k]["code"]} for k in points]}


def _shift(clock, m):
    if not clock or not m:
        return clock
    h, mm = map(int, clock.split(":"))
    t = (h * 60 + mm + int(round(m))) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"
