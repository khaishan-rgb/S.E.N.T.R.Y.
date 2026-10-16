"""SG Transport Pulse - Diversion Planner v2 (V16.16): the OCC diversion engine, pure logic.

Principle: find the SIMPLEST VERIFIED BUS-SUITABLE diversion that PRESERVES THE MOST USEFUL PART OF THE ORIGINAL
SERVICE while skipping only what is necessary - not the shortest path around the blockage.

The routing engine (TomTom, else OSRM) produces every road geometry. This module only measures, scores and explains
candidates that have already passed the hard validation in app.py / diversion.py. Nothing here invents a road.
"""
import math

import diversion as D

# OCC DIVERSION SCORE weights (percent). Tunable from the page (stored by the server).
# HARD limits on a diversion (applied before scoring; a candidate over any limit is rejected, never ranked)
DEFAULT_LIMITS = {"max_extra_km": 3.0, "max_extra_min": 10.0, "max_ratio": 3.0, "max_skipped": 8}
LIMIT_LABELS = {"max_extra_km": ["Extra distance safeguard", "km", 0.5, 15.0, 0.5],
                "max_extra_min": ["Extra running time safeguard", "min", 2.0, 40.0, 1.0],
                "max_ratio": ["Length vs the normal section safeguard", "\u00d7", 1.5, 8.0, 0.5],
                "max_skipped": ["Stops skipped safeguard", "stops", 1, 20, 1]}


def safeguards(m, L):
    """Safeguards, not blind rejects: every exceeded limit becomes a warning and a score penalty proportional to how far it is
    exceeded (4 points + 30 x the excess fraction, max 15 each). An excellent +3.2 km simple arterial diversion can still beat a
    complicated +2.8 km one. -> (warnings, penalty)"""
    out, pen = [], 0.0

    def hit(val, lim, txt):
        nonlocal pen
        if lim and val > lim:
            out.append(txt)
            pen += min(15.0, 4.0 + 30.0 * (val - lim) / lim)
    hit(m["added_km"], L["max_extra_km"], f"{m['added_km']:+.1f} km extra (safeguard {L['max_extra_km']:g} km)")
    hit(m["added_min"], L["max_extra_min"], f"{m['added_min']:+.0f} min extra (safeguard {L['max_extra_min']:g} min)")
    if m["normal_km"] > 0 and m["added_km"] > 0.8:
        hit(m["div_km"] / m["normal_km"], L["max_ratio"], f"{m['div_km'] / m['normal_km']:.1f}\u00d7 the normal section (safeguard {L['max_ratio']:g}\u00d7)")
    hit(m["skipped_n"], L["max_skipped"], f"{m['skipped_n']} stops skipped (safeguard {int(L['max_skipped'])})")
    return out, round(pen, 1)


def over_limit(m, L):
    """kept for compatibility: the first safeguard exceeded, or ''"""
    w, _ = safeguards(m, L)
    return w[0] if w else ""


DEFAULT_WEIGHTS = {"footprint": 25, "stops": 20, "simple": 20, "rejoin": 10, "busroad": 10, "junction": 5, "time": 5, "dist": 5}
WEIGHT_LABELS = {"footprint": "Smallest diversion footprint", "stops": "Minimum original stops affected",
                 "simple": "Bus Captain simplicity", "rejoin": "Earliest practical rejoin", "busroad": "Proven bus-suitable roads",
                 "junction": "Fewest / easiest turns", "time": "Additional running time", "dist": "Additional distance"}

PARAMS = {
    "upstream_max": 4,        # diversion starts considered: the last reachable stop and up to 4 stops before it
    "downstream_max": 8,      # rejoin stops considered: the first stop after the blockage and up to 8 more
    "max_level": 10,          # progressive search: combinations with up to this many extra stops given up
    "pairs_per_level": 6,
    "extra_levels": 1,        # after the first level with a strong valid diversion, look this much further for alternatives
    "min_candidates": 5,      # ...and keep going (at most 2 more levels) until this many distinct verified candidates exist
    "max_candidates": 10,     # candidates returned (recommendation + alternatives) - the AI operational review compares these
    "early_probe": 4,         # always also check this many earlier diversion starts (to prove or disprove an early turn-off)
    "early_window_m": 3000.0,  # ...and every stop back to the start of the route (e.g. an interchange) within this distance
    "impossible_turn_deg": 165.0,
}

TURN_WORDS = [(20, "STRAIGHT"), (60, "SLIGHT"), (120, ""), (165, "SHARP")]


def turn_of(a_in, a_out):
    """signed turn angle (deg, + = right) between two headings"""
    return (a_out - a_in + 540.0) % 360.0 - 180.0


def _word(t):
    at = abs(t)
    if at < 20:
        return "STRAIGHT"
    side = "RIGHT" if t > 0 else "LEFT"
    return ("SLIGHT " + side) if at < 60 else (side if at < 120 else "SHARP " + side)


def _road_starting_near(steps, pt, tol=30.0):
    """the road of the routing step that begins closest to pt (within tol)"""
    best, bd = "", tol
    for st in steps or []:
        ln = st.get("line") or []
        if len(ln) >= 2:
            d = D.dist_m(ln[0], pt)
            if d <= bd and (st.get("road") or "").strip():
                best, bd = st["road"].strip(), d
    return best


def instructions(steps, seg, approach=None, depart=None, rejoin_road=""):
    """Turn-by-turn instructions for the diverted part, measured from the routing engine's own geometry, INCLUDING the
    turn off the original route (approach = heading of the original route at the leave point) and the turn back onto it
    (depart = heading of the original route at the rejoin point), each named after the road actually entered.
    [{"turn": "LEFT" | "RIGHT" | "STRAIGHT" | "SLIGHT LEFT" | "SHARP RIGHT" | ..., "road", "deg", "sharp"}]"""
    if len(seg) < 2:
        return []
    cm = D.cum_m(seg)
    out = []
    for st in steps or []:
        ln = st.get("line") or []
        if len(ln) < 2:
            continue
        d, s_ = D.nearest_on_line(ln[0], seg, cm)[:2]
        if d > 25.0 or s_ < 5.0 or s_ > cm[-1] - 5.0:
            continue
        a_in = D.bearing(D.point_at(seg, cm, max(0.0, s_ - 25.0)), D.point_at(seg, cm, s_))
        a_out = D.bearing(D.point_at(seg, cm, s_), D.point_at(seg, cm, min(cm[-1], s_ + 25.0)))
        t = turn_of(a_in, a_out)
        word = _word(t)
        road = (st.get("road") or "").strip()
        if word == "STRAIGHT" and out and out[-1]["road"] == road:
            continue
        if "roundabout" in str(st.get("man", "")):
            word = "ROUNDABOUT " + word if word != "STRAIGHT" else "ROUNDABOUT (straight on)"
        out.append({"turn": word, "road": road, "deg": round(t), "sharp": abs(t) >= 120, "s": round(s_)})
    # the turn off the original route - only when no junction turn was already measured there
    if approach is not None and cm[-1] > 50.0 and not any(x["s"] <= 40 and x["turn"] != "STRAIGHT" for x in out):
        t0 = turn_of(approach, D.bearing(D.point_at(seg, cm, 15.0), D.point_at(seg, cm, min(cm[-1], 45.0))))
        if abs(t0) >= 20:
            out.insert(0, {"turn": _word(t0), "road": _road_starting_near(steps, D.point_at(seg, cm, 15.0), 40.0), "deg": round(t0),
                           "sharp": abs(t0) >= 120, "s": 0})
    # the turn back onto the original route - only when no junction turn was already measured there
    if depart is not None and cm[-1] > 50.0 and not any(x["s"] >= cm[-1] - 40 and x["turn"] != "STRAIGHT" for x in out):
        t1 = turn_of(D.bearing(D.point_at(seg, cm, cm[-1] - 35.0), D.point_at(seg, cm, cm[-1] - 5.0)), depart)
        if abs(t1) >= 20:
            out.append({"turn": _word(t1), "road": rejoin_road or _road_starting_near(steps, seg[-1]), "deg": round(t1),
                        "sharp": abs(t1) >= 120, "s": round(cm[-1]), "rejoin": True})
    merged = []
    for x in out:
        if merged and merged[-1]["road"] == x["road"] and x["turn"] == "STRAIGHT":
            continue
        merged.append(x)
    return merged


def complexity(instr, groups, suit):
    """ROUTE COMPLEXITY (bus captain's view): turns, sharp turns, road changes, minor-road share -> simplicity 0..1"""
    turns = sum(1 for x in instr if x["turn"] != "STRAIGHT" and not x["turn"].startswith("ROUNDABOUT (straight"))
    sharp = sum(1 for x in instr if x["sharp"])
    roads = len({g.get("road") for g in groups or [] if g.get("road")})
    minor = float((suit or {}).get("small_share") or 0.0)
    c = turns + 1.5 * sharp + 0.5 * max(0, roads - 1) + 3.0 * minor
    return {"turns": turns, "sharp": sharp, "road_changes": max(0, roads - 1), "minor_share": round(minor, 2),
            "complexity": round(c, 1), "simplicity": round(max(0.0, 1.0 - c / 12.0), 3)}


def bc_complexity(instr, groups, suit, ev):
    """BC COMPLEXITY - how hard the diversion is for a Bus Captain who may not know the area: turns, right turns (across
    traffic in Singapore), sharp turns, roundabouts, closely spaced turns (< 150 m apart), number of roads, small roads and
    roads no bus service uses (unfamiliar). -> points (lower = easier), LOW / MEDIUM / HIGH, simplicity 0..1"""
    tl = [x for x in instr if x["turn"] != "STRAIGHT" and not x["turn"].startswith("ROUNDABOUT (straight")]
    turns = len(tl)
    right = sum(1 for x in tl if "RIGHT" in x["turn"])
    sharp = sum(1 for x in tl if x.get("sharp"))
    rabout = sum(1 for x in tl if x["turn"].startswith("ROUNDABOUT"))
    ss = sorted(x.get("s", 0) for x in tl)
    close = sum(1 for a_, b_ in zip(ss, ss[1:]) if b_ - a_ < 150)
    roads = len({g.get("road") for g in groups or [] if g.get("road")})
    minor = float((suit or {}).get("small_share") or 0.0)
    unfamiliar = max(0.0, 1.0 - float((ev or {}).get("coverage") or 0.0)) if (ev or {}).get("level", 3) != 2 else 0.4
    pts = 1.0 * turns + 0.5 * right + 1.5 * sharp + 0.5 * rabout + 1.0 * close + 0.4 * max(0, roads - 1) + 2.5 * minor + 1.5 * unfamiliar
    label = "LOW" if pts <= 5.0 else ("MEDIUM" if pts <= 8.5 else "HIGH")
    return {"points": round(pts, 1), "label": label, "simplicity": round(max(0.0, 1.0 - pts / 14.0), 3), "turns": turns, "right": right,
            "sharp": sharp, "roundabouts": rabout, "close_turns": close, "roads": roads, "minor_share": round(minor, 2),
            "unfamiliar_share": round(unfamiliar, 2)}


def footprint(m):
    """DIVERSION FOOTPRINT - how much of the network the diversion disturbs: off-route distance, original route lost, stops
    skipped, how far before the blockage the bus leaves and after it the bus rejoins, roads and turns. -> index (lower =
    smaller), SMALL / MEDIUM / LARGE, score 0..1"""
    idx = (m["div_km"] / 4.0 + m["normal_km"] / 3.0 + m["skipped_n"] / 6.0 + max(0.0, m["rejoin_after_m"]) / 2000.0
           + max(0.0, m.get("leave_before_m", 0.0)) / 2000.0 + max(0, m.get("roads", 1) - 1) / 8.0 + m["turns"] / 12.0)
    label = "SMALL" if idx <= 1.8 else ("MEDIUM" if idx <= 3.2 else "LARGE")
    return {"index": round(idx, 2), "label": label, "score": round(max(0.0, 1.0 - idx / 6.0), 3)}


def bc_sentence(m, instr):
    """the brief an OCC controller reads to a Bus Captain: "After Stop A, turn left into Road X, ... and resume the normal
    route at Stop D." """
    words = {"LEFT": "turn left into", "RIGHT": "turn right into", "SLIGHT LEFT": "bear left into", "SLIGHT RIGHT": "bear right into",
             "SHARP LEFT": "turn sharp left into", "SHARP RIGHT": "turn sharp right into", "STRAIGHT": "continue straight on"}
    bits = []
    for x in instr:
        road = x.get("road") or "the next road"
        if x["turn"].startswith("ROUNDABOUT"):
            bits.append(f"at the roundabout {x['turn'].replace('ROUNDABOUT ', '').lower()} into {road}")
        else:
            bits.append(f"{words.get(x['turn'], x['turn'].lower())} {road}")
    start = f"After {m['leave_name']} ({m['leave_code']})" if not m.get("free_start") else f"Leave {m['leave_name']} ({m['leave_code']})"
    body = ", ".join(bits) if bits else "follow the diversion"
    return f"{start}, {body} and resume the normal route at {m['rejoin_name']} ({m['rejoin_code']})."


def impossible_turn(instr, P=PARAMS):
    for x in instr:
        if abs(x["deg"]) >= P["impossible_turn_deg"]:
            return f"impossible turn ({abs(x['deg'])}\u00b0) into {x['road'] or 'the next road'}"
    return ""


def junction_quality(instr):
    turns = [x for x in instr if x["turn"] != "STRAIGHT"]
    if not turns:
        return 1.0
    bad = sum(1 for x in turns if x["sharp"]) + sum(0.5 for x in turns if x["turn"].startswith("ROUNDABOUT"))
    return round(max(0.0, 1.0 - bad / len(turns)), 3)


def busroad_label(ev, suit):
    """VERY HIGH: buses already run it in this direction | HIGH: LTA bus stops on it | MEDIUM: major/arterial roads | LOW"""
    lvl, sc = ev.get("level", 3), ev.get("score", 0.25)
    if lvl == 1 and sc >= 1.0:
        return "VERY HIGH", 1.0
    if lvl == 1 or lvl == 2:
        return "HIGH", 0.8
    if (suit or {}).get("level", 0.5) >= 0.6:
        return "MEDIUM", 0.55
    return "LOW", 0.25


def occ_score(m, weights=None):
    """OCC DIVERSION SCORE 0-100 for a VALID candidate (feasibility already proven by the hard checks). Footprint, stops,
    Bus Captain simplicity and early rejoin dominate; running time and distance are minor. Safeguard penalties are subtracted."""
    W = dict(DEFAULT_WEIGHTS)
    W.update({k: float(v) for k, v in (weights or {}).items() if k in W})
    tot = sum(W.values()) or 1.0
    f = {
        "footprint": m["footprint_score"],
        "stops": max(0.0, 1.0 - m["extra_skipped"] / 6.0),
        "simple": m["simplicity"],
        "rejoin": max(0.0, 1.0 - max(0.0, m["rejoin_after_m"]) / 2500.0),
        "busroad": m["busroad_score"],
        "junction": max(0.0, 1.0 - m["turns"] / 8.0) * m["junction"],
        "time": max(0.0, 1.0 - max(0.0, m["added_min"]) / 20.0),
        "dist": max(0.0, 1.0 - max(0.0, m["added_km"]) / 6.0),
    }
    parts = {k: round(100.0 * W[k] / tot * f[k], 1) for k in W}
    if m.get("penalty"):
        parts["safeguards"] = -m["penalty"]
    return round(max(0.0, sum(parts.values())), 1), parts


def overall_confidence(m):
    """HIGH / MEDIUM / LOW for a valid candidate"""
    if (m["busroad_label"] in ("VERY HIGH", "HIGH") and m["bc_label"] != "HIGH" and m["footprint_label"] != "LARGE" and m["next_ok"]
            and m["route_conf"] >= 0.9 and not m["dd_doubt"] and not m.get("warnings")):
        return "HIGH"
    if m["busroad_label"] != "LOW" and m["next_ok"]:
        return "MEDIUM"
    return "LOW"


def tag_alternatives(cands):
    """Option A minimum skipped stops / B simplest bus-road diversion / C fastest valid diversion"""
    if not cands:
        return {}
    a = min(cands, key=lambda c: (c["m"]["skipped_n"], -c["score"]))
    b = max(cands, key=lambda c: (c["m"]["simplicity"], c["m"]["busroad_score"], c["score"]))
    c_ = min(cands, key=lambda c: (c["m"]["added_min"], -c["score"]))
    return {"A": a, "B": b, "C": c_}


def instruction_text(instr, rejoin_name):
    parts = []
    for x in instr:
        if x["turn"] == "STRAIGHT":
            parts.append("STRAIGHT" + (f" ({x['road']})" if x["road"] else ""))
        else:
            parts.append(x["turn"] + (f" into {x['road']}" if x["road"] else ""))
    parts.append("REJOIN" + (f" at {rejoin_name}" if rejoin_name else ""))
    return parts


def why_this(best, ctx_m):
    """bullet reasons for the recommended diversion"""
    m = best["m"]
    out = []
    if m["leave_after_ia"] >= 0 and not m["earlier"]:
        out.append(f"Continues the normal route as long as practical \u2014 serves every reachable stop up to {m['leave_name']}")
    elif m["earlier"]:
        out.append(f"Earlier diversion point needed: no practical escape after {ctx_m['ia_name']}, so the bus leaves after {m['leave_name']}")
    if m.get("free_start"):
        out.append(f"Leaves {m['leave_name']} by a different exit \u2014 the normal exit leads straight into the blockage")
    if m.get("probe"):
        out.append("Keeps to the normal route past the stop and turns off at a later junction (the immediate turn-off does not work)")
    if m["preserved_vs_early"] > 0:
        out.append(f"Preserves {m['preserved_vs_early']} more original stop(s) than the earliest diversion checked")
    out.append(f"{m['footprint_label'].title()} diversion footprint \u2014 {m['div_km']:.1f} km off the route, rejoins {max(0, m['rejoin_after_m']):.0f} m after the blockage")
    out.append(f"BC complexity {m['bc_label']} \u2014 {m['turns']} turn(s)" + (f", {m['bc']['close_turns']} close together" if m["bc"]["close_turns"] else "")
               + (" on roads buses already use" if m["busroad_label"] == "VERY HIGH" else ""))
    out.append(f"Only {m['skipped_n']} stop(s) skipped" + (f" ({m['unavoidable']} inside the blockage)" if m["unavoidable"] else ""))
    out.append("No U-turn, no backtracking, blocked road avoided")
    out.append(f"{m['turns']} turn(s)" + (" \u2014 simple to follow" if m["simplicity"] >= 0.6 else "") + (f", {m['sharp']} sharp" if m["sharp"] else ""))
    out.append({"VERY HIGH": "Uses roads public buses already run on in this direction", "HIGH": "Uses roads with LTA bus stops",
                "MEDIUM": "Uses major / arterial roads (no bus service on them yet \u2014 verify)", "LOW": "Road suitability uncertain"}[m["busroad_label"]])
    out.append(f"Correct-direction rejoin verified at {m['rejoin_name']}")
    if m["next_ok"]:
        out.append(f"Following original stop verified: {m['next_name']}")
    return out


def why_not(best, other, ctx_m):
    """one plain-language reason why another valid candidate lost to the recommendation"""
    b, o = best["m"], other["m"]
    bits = []
    if o["skipped_n"] > b["skipped_n"]:
        n = o["skipped_n"] - b["skipped_n"]
        if o["leave_index"] < b["leave_index"]:
            bits.append(f"leaves the route earlier and bypasses {n} more original stop(s) that the bus can still serve")
        else:
            bits.append(f"skips {n} more original stop(s)")
    if o["footprint_index"] > b["footprint_index"] + 0.3:
        bits.append(f"larger diversion footprint ({o['footprint_label'].lower()}: {o['div_km']:.1f} km off the route vs {b['div_km']:.1f} km)")
    if o["bc_points"] > b["bc_points"] + 1.0:
        bits.append(f"harder for the Bus Captain ({o['bc_label'].lower()} complexity: {o['turns']} turns" + (f", {o['bc']['close_turns']} close together" if o["bc"]["close_turns"] else "") + ")")
    elif o["turns"] > b["turns"] + 1:
        bits.append(f"needs {o['turns']} turns instead of {b['turns']}")
    if o["busroad_score"] < b["busroad_score"] - 0.1:
        bits.append(f"weaker bus-road evidence ({o['busroad_label'].lower()})")
    if o["added_min"] > b["added_min"] + 2:
        bits.append(f"{o['added_min'] - b['added_min']:.0f} min slower")
    if o["rejoin_index"] > b["rejoin_index"]:
        bits.append("rejoins the route later")
    shorter = o["div_km"] < b["div_km"] - 0.05
    if o.get("warnings"):
        bits.append("exceeds a safeguard (" + "; ".join(o["warnings"][:2]) + ")")
    lead = "Technically possible" + (" and shorter" if shorter else "") + ", but "
    if not bits:
        bits.append(f"lower overall OCC score ({other['score']:.0f} vs {best['score']:.0f})")
    label = f"{o['leave_name']} \u2192 {o['rejoin_name']}"
    first = (o["instructions"][0]["turn"].lower() + " into " + (o["instructions"][0]["road"] or "the next road")) if o["instructions"] else "a different road"
    return {"title": f"Why not {first} after {o['leave_name']}?", "route": label, "text": lead + "; ".join(bits) + "."}


# validation stages in order: the reason from the route that got FURTHEST tells the controller the real obstacle
# (the router's straight-through route always fails first and says little)
_STAGES = [("reach the stops", 0), ("not connected", 0), ("blocked direction", 1), ("exclusion zone", 1), ("crosses the blocked", 1),
           ("U-turn", 2), ("back towards", 2), ("never leaves", 3), ("opposite carriageway", 4), ("does not reach the original", 4),
           ("rejoins before", 4), ("rejoins after", 4), ("only leaves", 4), ("unreasonable detour", 5), ("restricted road", 6),
           ("single-deck", 7), ("impossible turn", 8), ("next-leg", 9), ("too long", 10), ("too far round", 10), ("(limit", 10)]


def best_reason(reasons):
    def stage(r):
        return max([v for k, v in _STAGES if k.lower() in r.lower()] or [0])
    return max(reasons, key=lambda r: (stage(r), reasons.count(r)))


def explain_failure(attempts):
    """grouped reasons when no verified diversion exists"""
    groups = {}
    for a in attempts:
        if a["result"] != "valid":
            groups[a["reason"]] = groups.get(a["reason"], 0) + 1
    return [f"{n} stop pair(s): {r}" for r, n in sorted(groups.items(), key=lambda kv: -kv[1])]


def search_levels(ia, b0, n_stops, P=PARAMS):
    """progressive combinations (diversion start i, rejoin j): level = stops given up beyond the minimum.
    Within a level the bus stays on its route longer first (larger i) - do not divert too early."""
    levels = []
    for k in range(0, P["max_level"] + 1):
        lv = []
        for back in range(0, min(k, P["upstream_max"]) + 1):
            i, j = ia - back, b0 + (k - back)
            if i < 0 or j >= n_stops or (j - b0) > P["downstream_max"]:
                continue
            lv.append((i, j))
        lv.sort(key=lambda ij: -ij[0])
        levels.append(lv[:P["pairs_per_level"]])
    return levels
