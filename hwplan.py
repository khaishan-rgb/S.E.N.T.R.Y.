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
      "slow_max": 8.0, "slow_min": 1.0, "even_tol": 2.0, "slow_gate": 0.02}
MODEL = "hwplan-15.1-gap-split"


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

    cands, infeasible = [], {"no_route": 0, "too_far": 0, "no_benefit": 0, "no_gap": 0, "behind_rear": 0}
    U = {u["uid"]: u for u in units}
    Bu = roles.get("B")
    SLOW_MAX, SLOW_MIN, TOL, GATE = float(P["slow_max"]), float(P["slow_min"]), float(P["even_tol"]), float(P["slow_gate"])

    def slowed(uid, tms, x, j):
        """progressive slow-down (slower running / longer dwell) that reaches +x min at the entry stop and holds after it."""
        u = U[uid]
        if u["kind"] == "in_service":
            q = u["q"]; span = max(1.0, j - q)
            return {k: t + (x * min(1.0, (k - q) / span) if k > q else 0.0) for k, t in tms.items()}
        span = max(1.0, float(j))
        return {k: t + x * min(1.0, k / span) for k, t in tms.items()}

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
        # the PROLONGED HEADWAY Bus C has to fill at this stop: the bus ahead of Bus C's slot and the next bus after it (without Bus C)
        others = sorted((t[j], uid) for uid, t in base.items() if j in t and uid != C["uid"])
        f = (base[Bu["uid"]][j], Bu["uid"]) if (Bu and j in base[Bu["uid"]]) else max((x for x in others if x[0] <= e_j), default=None)
        nx = min((x for x in others if f and x[0] > f[0]), default=None) if f else None
        if not f:
            infeasible["no_gap"] += 1
            continue
        if not nx:                                               # nothing behind in the forecast: aim one target headway behind the bus ahead
            nx = (f[0] + 2.0 * H, None)
        fu = U[f[1]]
        holds = [0.0] + ([float(x) for x in range(1, int(P["hold_max"]) + 1)] if fu["movable"] and fu["dep"] > now else [])
        best = None
        for h in holds:
            tF, tR = f[0] + h, nx[0]
            mid = (tF + tR) / 2.0
            te = max(e_j, mid)                                   # aim for the middle of the gap; if early, leave the interchange later
            if te >= tR - 0.5:
                continue
            tab = dict(base)
            tab[C["uid"]] = times(C, entry=(j, te))
            if h:
                tab[fu["uid"]] = times(fu, dep=fu["dep"] + h)
            e, mx, mn, per = evaluate(tab)
            if e is None:
                continue
            # buses behind Bus C: slow down progressively so the headways behind it even out (forward-headway rule, EWT-checked)
            behind = sorted((t[j], uid) for uid, t in tab.items() if j in t and t[j] > te + 1e-6 and uid != C["uid"])
            tN = behind[2][0] if len(behind) >= 3 else None
            target = (tN - te) / 3.0 if tN else H                # even spacing Bus C .. D .. E .. next
            slows, prev_t, hw_log = {}, te, {}
            for tt_, uid in behind[:2]:
                cur_t = tab[uid][j]
                need = prev_t + target - cur_t
                x = float(round(min(SLOW_MAX, max(0.0, need))))
                hw_log[uid] = {"before": _r(cur_t - prev_t), "target": _r(target)}
                if x >= SLOW_MIN and (U[uid]["kind"] == "in_service" or U[uid]["dep"] >= now - 1e-6):
                    t2 = dict(tab); t2[uid] = slowed(uid, tab[uid], x, j)
                    e2, mx2, mn2, per2 = evaluate(t2)
                    if e2 is not None and e2 <= e + GATE:
                        tab, e, mx, mn, per = t2, e2, mx2, mn2, per2
                        slows[uid] = x
                        cur_t += x
                hw_log[uid]["after"] = _r(cur_t - prev_t)
                prev_t = cur_t
            imb = abs((te - tF) - (tR - te))
            J = e + float(P["adj_cost"]) * (h + sum(slows.values()))
            cand_ = {"J": J, "e": e, "mx": mx, "mn": mn, "per": per, "tab": tab, "h": h, "te": te, "mid": mid, "tF": tF, "tR": tR, "imb": imb,
                     "slows": slows, "hw_log": hw_log, "behind": behind}
            if best is None:                                     # h = 0: the split this stop gives on its own decides its ranking
                cand_["key"] = (0 if imb <= TOL else 1, imb if imb > TOL else 0.0, J)
                best = cand_
            elif J < best["J"] - 1e-9 and imb <= max(TOL, best["imb"]):
                cand_["key"] = best["key"]                       # a hold of the bus ahead is kept only if it also lowers EWT
                best = cand_
        if not best:
            infeasible["behind_rear"] += 1
            continue
        te, tab = best["te"], best["tab"]
        ru = U[best["behind"][0][1]] if best["behind"] else None
        tp = lambda u: tab[u["uid"]].get(j) if u else None
        adj = {}
        if best["h"]:
            adj[fu["uid"]] = best["h"]
        dn_pts = [k for k in points if k >= j]
        dn_mx = max((max(best["per"][k][0]) for k in dn_pts if k in best["per"]), default=None)
        wait = te - e_j
        slows_out = []
        for uid, x in best["slows"].items():
            u = U[uid]; lg = best["hw_log"].get(uid, {})
            span = max(1, int(round(j - u["q"]))) if u["kind"] == "in_service" else j
            slows_out.append({"uid": uid, "num": u["num"], "role": u.get("role"), "label": u["label"], "x": x, "stops": span,
                              "hw_before": lg.get("before"), "hw_after": lg.get("after"), "target": lg.get("target")})
        rear_log = [{"uid": uid, "role": U[uid].get("role"), "num": U[uid]["num"], "label": U[uid]["label"], "slow": best["slows"].get(uid, 0.0),
                     **best["hw_log"].get(uid, {})} for _, uid in best["behind"][:2]]
        cands.append({"j": j, "no": j + 1, "code": O["stops"][j]["code"], "name": O["stops"][j]["name"], "lat": O["stops"][j]["lat"], "lon": O["stops"][j]["lon"],
                      "skip": j, "avoided": _r(SM * j), "os_min": _r(rmin), "os_km": _r(rkm, 2), "os_src": rsrc,
                      "ready": hhmm(C["ready"]), "leave": hhmm(C["ready"] + wait), "wait": _r(wait), "earliest": hhmm(e_j),
                      "entry": _r(te), "entry_clock": hhmm(te), "net": _r(SM * j - rmin),
                      "gap": {"front": fu["label"], "front_role": fu.get("role"), "rear": U[nx[1]]["label"] if nx[1] else "the next trip (beyond the forecast)",
                              "rear_role": U[nx[1]].get("role") if nx[1] else None,
                              "t_front": hhmm(f[0]), "t_front_held": hhmm(best["tF"]), "t_rear": hhmm(nx[0]), "minutes": _r(best["tR"] - best["tF"]), "minutes_raw": _r(nx[0] - f[0]),
                              "hold": best["h"], "mid": hhmm(best["mid"]),
                              "split": [_r(te - best["tF"]), _r(best["tR"] - te)], "imbalance": _r(best["imb"]), "even": best["imb"] <= TOL},
                      "front": {"uid": fu["uid"], "num": fu["num"], "role": fu.get("role"), "label": fu["label"], "pass": hhmm(tp(fu)), "adj": best["h"],
                                "dep": hhmm(fu["dep"]), "dep_new": hhmm(fu["dep"] + best["h"]) if fu["kind"] == "next_trip" else None, "movable": fu["movable"]},
                      "rear": ({"uid": ru["uid"], "num": ru["num"], "role": ru.get("role"), "label": ru["label"], "pass": hhmm(tp(ru)),
                                "adj": best["slows"].get(ru["uid"], 0.0), "dep": hhmm(ru["dep"])} if ru else None),
                      "slows": slows_out, "rear_log": rear_log,
                      "gap_before": _r(te - tp(fu)) if tp(fu) is not None else None, "gap_after": _r(tp(ru) - te) if ru and tp(ru) is not None else None,
                      "max_hw": _r(dn_mx if dn_mx is not None else best["mx"]), "max_hw_all": _r(best["mx"]), "ewt": round(best["e"], 3), "score": best["J"],
                      "gain": round(e0 - best["e"], 3), "km_skipped": _r(O["ss"][j], 2), "rkey": best["key"] + (j,),
                      "adj": dict(adj), "_tab": tab, "_adj": {**adj, **{u: x for u, x in best["slows"].items()}}})
    # RECOMMENDED: the first stop where Bus C can land in the middle of the prolonged headway (fewest stops skipped);
    # if no stop allows an even split, the most even one. Lowest whole-route EWT is shown alongside.
    cands.sort(key=lambda x: x["rkey"][:2] + (x["j"],))
    for i, x in enumerate(cands, 1):
        x["rank"] = i
    best = cands[0] if cands else None
    best_ewt = min(cands, key=lambda x: (x["score"], x["j"])) if cands else None
    for x in cands:
        x["ewt_rank"] = sorted(cands, key=lambda y: y["score"]).index(x) + 1

    def detail(x):
        tab, adj, j = x["_tab"], x["_adj"], x["j"]
        cols = [j] + [k for k in (j + 5, j + 10, j + 15) if k < nO]
        ring = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
        rows = []
        for u in ring:
            t = tab.get(u["uid"], {})
            is_slow = u["uid"] in {s_["uid"] for s_ in x["slows"]}
            rows.append({"role": u["role"], "num": u["num"], "label": u["label"], "halfway": u["uid"] == C["uid"],
                         "dep": None if u["uid"] == C["uid"] else hhmm((u["dep"] + (adj.get(u["uid"], 0.0) if not is_slow else 0.0)) if u["kind"] == "next_trip" else u["dep"]),
                         "adj": adj.get(u["uid"], 0.0), "slow": is_slow, "pass": [hhmm(t.get(k)) if t.get(k) is not None else None for k in cols],
                         "t": [_r(t.get(k)) if t.get(k) is not None else None for k in cols]})
        hwcol = []
        for k in cols:
            sq = sorted((tt[k], uid) for uid, tt in tab.items() if k in tt)
            hwcol.append({uid: (t - sq[i - 1][0]) if i > 0 else None for i, (t, uid) in enumerate(sq)})
        for row, u in zip(rows, ring):
            hws = [hwcol[ci_].get(u["uid"]) for ci_ in range(len(cols))]
            row["hw"] = [_r(h) for h in hws]
            row["max_hw"] = _r(max([h for h in hws if h is not None], default=0.0)) if any(h is not None for h in hws) else None
        slow_ids = {s_["uid"] for s_ in x["slows"]}
        return {"cols": [{"j": k, "no": k + 1, "code": O["stops"][k]["code"], "name": O["stops"][k]["name"]} for k in cols], "rows": rows,
                "before": seq_at(base, j), "after": [dict(s_, slow=s_["uid"] in slow_ids) for s_ in seq_at(tab, j, adj)]}

    def why(x):
        g = x["gap"]
        s = (f"The prolonged headway Bus {Cn} has to fill at Stop {x['no']} is between {g['front']} ({g['t_front']}) and {g['rear']} ({g['t_rear']}): "
             f"{g['minutes_raw']:.0f} min. ")
        if g["hold"]:
            s += f"Holding {g['front']} +{g['hold']:.0f} min at the interchange (passes {g['t_front_held']}) trims it to {g['minutes']:.0f} min and lowers the EWT further. "
        s += f"Half of it puts Bus {Cn} at {g['mid']}, leaving {g['split'][0]:.0f} / {g['split'][1]:.0f} min. "
        s += (f"Bus {Cn} completes {tl} at {hhmm(t_buses[c]['arr'])} ({D:g} min late), is ready at {x['ready']} after the {BRK:g}-min break, and the real road route "
              f"to Stop {x['no']} (BS {x['code']}) takes about {x['os_min']:.0f} min ({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
        if x["wait"] and x["wait"] >= 0.5:
            s += f"It therefore leaves the interchange {x['wait']:.0f} min later ({x['leave']}) so it enters exactly mid-gap at {x['entry_clock']}. "
        if g["even"]:
            s += f"Stop {x['no']} is the first stop where Bus {Cn} can reach the middle of the gap, so it skips the fewest stops ({x['skip']}). "
        else:
            s += f"No stop lets Bus {Cn} reach the middle in time; Stop {x['no']} gives the most even split. "
        for sl in x["slows"]:
            s += f"{sl['label']} slows down +{sl['x']:.0f} min over {sl['stops']} stops so it runs about {sl['hw_after']:.0f} min behind the bus ahead (forecast {sl['hw_before']:.0f}). "
        s += f"Downstream {ol} EWT {x['ewt']:.3f} min (no action {e0:.3f}); largest headway after entry {x['max_hw']:.0f} min."
        if best_ewt and best_ewt["code"] != x["code"]:
            s += (f" Stop {best_ewt['no']} has the lowest whole-route EWT ({best_ewt['ewt']:.3f}) because Bus {Cn} serves more stops there, "
                  f"but it splits the gap {best_ewt['gap']['split'][0]:.0f} / {best_ewt['gap']['split'][1]:.0f} min.")
        return s

    def actions(x):
        steps, n = [], 1
        fr = x.get("front")
        if fr and fr["adj"]:
            steps.append({"n": n, "bus": fr["label"], "role": fr.get("role"), "kind": "hold", "text": f"Hold and depart {fr['adj']:.0f} min later.", "time": fr["dep_new"]}); n += 1
        wtxt = f" Extend the break by {x['wait']:.0f} min" if x["wait"] and x["wait"] >= 0.5 else ""
        steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "halfway",
                      "text": f"Complete {tl}. Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}. Break until {x['ready']}.{wtxt}"
                              f"{'.' if wtxt else ''} Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive and enter {ol} at {x['entry_clock']} "
                              f"({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}). Continue to the final stop.",
                      "time": x["entry_clock"]}); n += 1
        for lg in x["rear_log"]:
            if lg.get("slow"):
                steps.append({"n": n, "bus": lg["label"], "role": lg.get("role"), "kind": "slow",
                              "text": f"Slow down +{lg['slow']:.0f} min progressively (longer dwell / easy running) up to Stop {x['no']}, to run about "
                                      f"{lg['after']:.0f} min behind the bus ahead instead of {lg['before']:.0f}.", "time": None})
            else:
                steps.append({"n": n, "bus": lg["label"], "role": lg.get("role"), "kind": "keep",
                              "text": f"Keep normal running – forecast {lg['before']:.0f} min behind the bus ahead (even spacing {lg['target']:.0f} min); no slow-down needed.", "time": None})
            n += 1
        for r in ("A",) + (("B",) if not (fr and fr["adj"]) else ()):
            u = roles.get(r)
            if u and u["uid"] not in x["_adj"] and u["uid"] not in {lg["uid"] for lg in x["rear_log"]}:
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
    strip = lambda x: {k: v for k, v in x.items() if not k.startswith("_") and k not in ("score", "rkey")}
    return {"ok": True, "model": MODEL, "now": hhmm(now), "loop": loop, "late_dir": T["dir"], "next_dir": O["dir"], "H": _r(H), "H_src": O.get("H_src") or T.get("H_src"),
            "stop_min": SM, "break_min": BRK, "late_bus": Cn, "delay": D, "c_arr": hhmm(t_buses[c]["arr"]), "c_ready": hhmm(C["ready"]), "c_full_dep": hhmm(full_dep),
            "interchange": {"code": O["stops"][0]["code"], "name": O["stops"][0]["name"], "lat": O["stops"][0]["lat"], "lon": O["stops"][0]["lon"]},
            "roles": {r: {"num": u["num"], "label": u["label"], "kind": u["kind"]} for r, u in roles.items()},
            "no_action": noaction, "candidates": [strip(x) for x in cands], "best": best["code"] if best else None,
            "best_ewt": best_ewt["code"] if best_ewt else None, "even_tol": TOL,
            "infeasible": infeasible, "n_stops": nO, "points": [{"no": k + 1, "code": O["stops"][k]["code"]} for k in points]}


def _shift(clock, m):
    if not clock or not m:
        return clock
    h, mm = map(int, clock.split(":"))
    t = (h * 60 + mm + int(round(m))) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"
