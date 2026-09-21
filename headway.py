"""Pre-emptive departure adjustment engine (pure functions: no I/O, stdlib only).

For one Service + Direction it predicts each bus's movement and terminal arrival, then runs five checks:

  1 EN-ROUTE       leading bus pulling away AND the gap behind it growing        -> Comm BC / regulate spacing
  2 TRAFFIC        congestion ahead of several buses AND arrivals predicted late -> temporarily LONGER departure HW
  3 LAST-15%       traffic adjustment active AND affected buses near on-time     -> cancel, restore original HW
  4 EARLY ARRIVAL  several consecutive buses in the final 15% AND early by >5min -> temporarily SHORTER departure HW
  5 NORMALISATION  an adjustment is active but service is back to normal         -> restore scheduled HW

What the data can and cannot say (LTA DataMall publishes no operator timetable or dispatch times):
  * Scheduled headway  = midpoint of LTA's BusServices frequency band for the current time of day.
  * Gaps between buses = differences of LTA's estimated arrival times at the same stop (falls back to a model).
  * "Late / early"     = predicted full-route running time under current LTA speed bands, minus a PLANNED running
                         time. The plan is ESTIMATED (free-flow drive x (1+PLAN_SLACK) + dwell) unless the operator
                         supplies a real timetable running time, which then overrides it.
Every threshold is in CFG and is returned to the UI so nothing is hidden.
"""
import math
import re
from datetime import timedelta

CFG = {
    "FINAL_PCT": 15,         # "final 15%" of the route
    "EARLY_GAP_PCT": 40,     # leading bus counts as "early" when the gap behind exceeds sched HW by max(40%, 3 min)
    "EARLY_GAP_MIN": 3.0,
    "TREND_MIN": 2.0,        # gap "increasing" = grows by >= 2 min between nearer and farther stops
    "C1_PERSIST_MIN": 3.0,   # ... and must still be true after 3 min (filters ETA noise)
    "MULTI": 2,              # "multiple buses"
    "AFFECT_MIN": 3.0,       # a bus is traffic-affected only if congestion ahead costs it >= 3 min ...
    "PROLONG_KM": 1.5,       # ... over a continuous slow stretch of >= 1.5 km  (= PROLONGED, not a spot delay)
    "CONG_KMH": 20.0,        # "slow" = below 20 km/h (LTA speed bands 1-2)
    "FLOW_KMH": 30.0,        # congestion cost = time lost below this normal-flow speed
    "PERSIST_MIN": 10.0,     # congestion must persist >= 10 min (two LTA 5-min speed-band updates) before we act
    "CLEAR_MIN": 5.0,        # recovery / normalisation must persist >= 5 min before restoring HW (no flip-flop)
    "MIN_LAYOVER": 7.0,      # minimum layover a delayed bus still gets at the terminal
    "DEFAULT_LAYOVER": 10.0, # scheduled layover assumed when the controller has not entered one
    "HEAVY_LAYOVER": 15.0,   # duty with >= 15 min layover absorbs delay: never adjusted for traffic
    "MAX_HOLD": 5.0,         # never suggest holding one bus at the terminal longer than this (bigger bunching needs other action)
    "LATE_MARGIN": 3.0,      # delay must exceed layover slack (layover - min layover) by this much
    "LATE_MIN": 3.0,         # (display only) route-vs-timetable colouring
    "EARLY_MIN": 5.0,        # terminal arrivals predicted early by > 5 min vs plan
    "NEAR_ONTIME": 1.5,      # within 1.5 min of plan = near on-time
    "MAX_EXTEND": 3,         # extend departure HW by at most +3 min
    "MAX_SHORTEN": 1,        # shorten departure HW by at most 1 min
    "NORMAL_GAP_PCT": 25,    # a gap is "normal" within max(2 min, 25%) of sched HW
    "NORMAL_GAP_MIN": 2.0,
    "PLAN_SLACK": 0.15,      # estimated plan = free-flow drive x 1.15 + dwell
    "DWELL_MIN": 0.33,       # minutes lost per stop
    "BUS_MAX_KMH": 60.0,     # buses are speed-limited; band 7/8 readings are capped here
}
# typical free-flow bus speed by LTA RoadCategory (1 expressway ... 6 slip road, 8 short tunnel)
FREE_KMH = {1: 60.0, 2: 45.0, 3: 40.0, 4: 35.0, 5: 30.0, 6: 30.0, 8: 60.0}
DEFAULT_FREE = 40.0

BAND_LABEL = {"AM_Peak": "AM peak 06:30-08:30", "AM_Offpeak": "AM off-peak 08:31-16:59",
              "PM_Peak": "PM peak 17:00-19:00", "PM_Offpeak": "PM off-peak"}


# ----------------------------------------------------------------------------- geometry
def hav_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    q = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(q))


def cumulative(line):
    cum = [0.0]
    for a, b in zip(line, line[1:]):
        cum.append(cum[-1] + hav_km(a[0], a[1], b[0], b[1]))
    return cum


def project(line, cum, lat, lon):
    """Distance along the route (km) of the point on `line` nearest to (lat, lon); also returns the miss distance (km)."""
    kx, ky = math.cos(math.radians(lat)) * 111.32, 110.574
    best_d, best_s = 1e9, 0.0
    for i in range(len(line) - 1):
        ax, ay = (line[i][1] - lon) * kx, (line[i][0] - lat) * ky
        bx, by = (line[i + 1][1] - lon) * kx, (line[i + 1][0] - lat) * ky
        vx, vy = bx - ax, by - ay
        l2 = vx * vx + vy * vy
        t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, -(ax * vx + ay * vy) / l2))
        d = math.hypot(ax + t * vx, ay + t * vy)
        if d < best_d:
            best_d, best_s = d, cum[i] + t * (cum[i + 1] - cum[i])
    return best_s, best_d


def prepare(line, stops):
    """Route constants that never change for a service/direction (cache these)."""
    cum = cumulative(line)
    stop_s = []
    for s in stops:
        v = project(line, cum, s["lat"], s["lon"])[0]
        stop_s.append(max(v, stop_s[-1]) if stop_s else v)   # stops are in route order: keep monotonic
    return {"cum": cum, "stop_s": stop_s, "km": cum[-1]}


# ----------------------------------------------------------------------------- schedule (LTA BusServices frequency)
def band_key(dt):
    m = dt.hour * 60 + dt.minute
    if 6 * 60 + 30 <= m <= 8 * 60 + 30:
        return "AM_Peak"
    if 8 * 60 + 30 < m < 17 * 60:
        return "AM_Offpeak"
    if 17 * 60 <= m <= 19 * 60:
        return "PM_Peak"
    return "PM_Offpeak"


def parse_freq(s):
    nums = [int(x) for x in re.findall(r"\d+", str(s or ""))]
    if not nums:
        return None
    lo, hi = (nums[0], nums[1]) if len(nums) > 1 else (nums[0], nums[0])
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi) if hi > 0 else None


def sched_hw(freqs, now):
    """{'hw','lo','hi','band','label','fallback'} for the current time of day, or None."""
    if not freqs:
        return None
    cur = band_key(now)
    for i, k in enumerate([cur] + [k for k in BAND_LABEL if k != cur]):
        p = parse_freq(freqs.get(k))
        if p:
            return {"hw": int((p[0] + p[1]) / 2 + 0.5), "lo": p[0], "hi": p[1], "band": k,
                    "label": f"{BAND_LABEL[k]} {p[0]}-{p[1]} min", "fallback": i > 0}
    return None


# ----------------------------------------------------------------------------- running-time model
def band_speed(b, mn, mx, cap):
    if b is None:
        return None
    try:
        mn, mx = float(mn), float(mx)
    except (TypeError, ValueError):
        mn = mx = None
    if mn is None or mx is None or mx < mn or mx >= 200:
        v = (b - 1) * 10 + 5.0 if b < 8 else cap
    else:
        v = (mn + mx) / 2
    return max(5.0, min(v, cap))


def _cat(c):
    try:
        return int(c)
    except (TypeError, ValueError):
        return None


class TimeModel:
    """Piecewise running time along the route from LTA speed bands (+ dwell), and the same at free-flow."""

    def __init__(self, runs, stop_s, cfg):
        self.cfg, self.stop_s, self.segs, e = cfg, stop_s, [], 0.0
        raw = []
        for r in runs:
            km = float(r.get("km") or 0)
            if km <= 0:
                continue
            raw.append((e, e + km, band_speed(r.get("b"), r.get("mn"), r.get("mx"), cfg["BUS_MAX_KMH"]),
                        FREE_KMH.get(_cat(r.get("cat")), DEFAULT_FREE)))
            e += km
        self.total = e
        kk = sum(b - a for a, b, v, f in raw if v)
        kh = sum((b - a) / v for a, b, v, f in raw if v)
        self.ok = bool(raw) and kk > 0
        avg = kk / kh if kh > 0 else DEFAULT_FREE
        self.segs = [(a, b, v or avg, f) for a, b, v, f in raw]      # unmatched stretches borrow the route average
        self.cseg = [(a, b, v) for a, b, v, f in raw]                # v is None where LTA has no reading (never counted as slow)
        self.known_pct = round(100 * kk / e) if e else 0

    def _span(self, a, b, free=False):
        a, b = max(0.0, min(a, self.total)), max(0.0, min(b, self.total))
        if b <= a:
            return 0.0
        t = 0.0
        for e0, e1, v, f in self.segs:
            lo, hi = max(a, e0), min(b, e1)
            if hi > lo:
                t += (hi - lo) / (f if free else v) * 60.0
        return t

    def cong(self, a, b):
        """Congestion ahead between km a and b -> (minutes lost below FLOW_KMH on slow stretches, slow km, longest slow stretch km).
        Slow stretches separated by <= 0.3 km (junction noise) count as one; no-reading stretches are never slow."""
        a, b = max(0.0, min(a, self.total)), max(0.0, min(b, self.total))
        cost, slow, best, cur, gap = 0.0, 0.0, 0.0, 0.0, 0.0
        for e0, e1, v in self.cseg:
            lo, hi = max(a, e0), min(b, e1)
            if hi <= lo:
                continue
            d = hi - lo
            if v is not None and v < self.cfg["CONG_KMH"]:
                cost += d * (1 / v - 1 / self.cfg["FLOW_KMH"]) * 60.0
                slow += d
                cur += d + (gap if cur > 0 else 0.0)
                gap = 0.0
                best = max(best, cur)
            else:
                gap += d
                if gap > 0.3:
                    cur, gap = 0.0, 0.0
        return cost, slow, best

    def dwell(self, a, b):
        return self.cfg["DWELL_MIN"] * sum(1 for s in self.stop_s if a < s <= b)

    def t(self, a, b):
        return self._span(a, b) + self.dwell(a, b)

    def t_free(self, a, b):
        return self._span(a, b, True) + self.dwell(a, b)

    def plan_estimate(self):
        return self._span(0, self.total, True) * (1 + self.cfg["PLAN_SLACK"]) + self.dwell(0, self.total)


# ----------------------------------------------------------------------------- helpers
def _median(v):
    v = sorted(v)
    n = len(v)
    return None if not n else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)


def _r(x, n=1):
    return None if x is None else round(x, n)


def _cls(b):
    return "none" if b is None else "smooth" if b >= 5 else "moderate" if b >= 3 else "slow"


def _chk(i, name, state, detail):
    return {"id": i, "name": name, "state": state, "detail": detail}


# ----------------------------------------------------------------------------- the engine
def evaluate(ctx, cfg=CFG):
    """ctx: service, direction, now(datetime), n_stops, stop_s[], route_km, runs[], traffic_ok, buses[], origin_etas[],
    sched(dict|None), adj_hw(int|None), plan_override(float|None).
    Each bus: s_km, etas{stop_index: minutes}, monitored, load, near."""
    C = cfg
    now, end = ctx["now"], float(ctx.get("route_km") or 0)
    stop_s, n_stops = ctx["stop_s"], ctx["n_stops"]
    tm = TimeModel(ctx.get("runs") or [], stop_s, C) if ctx.get("traffic_ok") else None
    if tm is not None and not tm.ok:
        tm = None

    raw = sorted([b for b in ctx["buses"] if b.get("s_km") is not None], key=lambda b: -b["s_km"])
    buses = []
    for i, b in enumerate(raw):
        s, etas = min(max(b["s_km"], 0.0), end), b.get("etas") or {}
        if n_stops - 1 in etas:
            et, src = etas[n_stops - 1], "LTA"
        elif etas and tm:
            k = max(etas)
            et, src = etas[k] + tm.t(stop_s[k], end), "LTA + traffic"
        elif tm:
            et, src = tm.t(s, end), "traffic model"
        else:
            et, src = None, None
        cost, cslow, clong = tm.cong(s, end) if tm else (None, None, None)
        buses.append({"id": i + 1, "s_km": round(s, 2), "pct": round(100 * s / end, 1) if end else 0.0, "etas": etas,
                      "eta_term": _r(et), "term_src": src,
                      "term_clock": (now + timedelta(minutes=et)).strftime("%H:%M") if et is not None else None,
                      "ahead_delay": _r(cost), "cong_km": _r(clong), "affected": cost is not None and cost >= C["AFFECT_MIN"] and clong >= C["PROLONG_KM"],
                      "monitored": bool(b.get("monitored")), "load": b.get("load") or "", "near": b.get("near") or "",
                      "gap_behind": None, "gap_trend": None, "gap_src": None})

    # gaps between consecutive buses (leader = nearer the terminal)
    gaps = []
    for i in range(len(buses) - 1):
        a, f = buses[i], buses[i + 1]
        common = sorted(set(a["etas"]) & set(f["etas"]))
        g = trend = src = None
        if common:
            k0, k1 = common[0], common[-1]
            g, src = max(0, f["etas"][k0] - a["etas"][k0]), "LTA"
            if k1 != k0:
                trend = max(0, f["etas"][k1] - a["etas"][k1]) - g
        elif tm:
            g, src = tm.t(f["s_km"], a["s_km"]), "model"
        a["gap_behind"], a["gap_trend"], a["gap_src"] = _r(g), trend, src
        gaps.append({"lead": a["id"], "follow": f["id"], "gap": _r(g), "trend": trend, "src": src})
    known = [g["gap"] for g in gaps if g["gap"] is not None]

    sched = ctx.get("sched")
    if sched:
        H, H_src, lo, hi = sched["hw"], "LTA BusServices frequency: " + sched["label"], sched["lo"], sched["hi"]
    elif len(known) >= 2:
        H, H_src, lo, hi = int(_median(known) + 0.5), "observed median gap (no LTA frequency available)", None, None
    else:
        H, H_src, lo, hi = None, None, None, None
    adj = ctx.get("adj_hw")
    cur = adj if adj else H
    adj_active = bool(adj and H and adj != H)

    plan_src, route_dev, pred_run, plan_run = None, None, None, None
    if tm:
        pred_run = tm.t(0, tm.total)
        po = ctx.get("plan_override")
        plan_run, plan_src = (po, "timetable (user)") if po else (tm.plan_estimate(), "estimated (no timetable)")
        route_dev = pred_run - plan_run

    lay = ctx.get("layover")
    L = float(lay) if lay else C["DEFAULT_LAYOVER"]
    heavy, slack = L >= C["HEAVY_LAYOVER"], max(0.0, L - C["MIN_LAYOVER"])
    trigger = slack + C["LATE_MARGIN"]
    held = ctx.get("held")                      # minutes each condition has been continuously true; None = no persistence gating
    hd = lambda k: 1e9 if held is None else held.get(k, 0.0)
    aff = [b for b in buses if b["affected"]]
    n_aff = len(aff)
    mean_cost = sum(b["ahead_delay"] for b in aff) / n_aff if aff else 0.0
    delay2 = route_dev if (tm and ctx.get("plan_override")) else mean_cost   # a real timetable beats the estimate
    ok_delay = delay2 <= max(C["NEAR_ONTIME"], slack)
    layinfo = {"min": L, "src": "set by you" if lay else "assumed", "heavy": heavy, "slack": round(slack, 1), "trigger": round(trigger, 1)}

    result = {"service": ctx["service"], "direction": ctx["direction"], "n_buses": len(buses),
              "monitored": sum(1 for b in buses if b["monitored"]), "sched": sched, "sched_hw": H, "hw_source": H_src,
              "current_hw": cur, "adj_active": adj_active, "pred_run_min": _r(pred_run), "plan_run_min": _r(plan_run),
              "plan_source": plan_src, "route_dev_min": _r(route_dev), "traffic_ok": tm is not None,
              "traffic_known_pct": tm.known_pct if tm else None, "buses": buses, "gaps": gaps,
              "avg_gap": _r(sum(known) / len(known)) if known else None,
              "origin_etas": sorted(ctx.get("origin_etas") or [])[:3], "route_km": round(end, 2),
              "strip": {"final_pct": 100 - C["FINAL_PCT"], "runs": []},
              "layover": layinfo, "delay_ahead": _r(delay2), "conds": {}, "held": held or {}, "watch": None, "turn": None}
    if tm and end:
        e = 0.0
        for r in ctx.get("runs") or []:
            km = float(r.get("km") or 0)
            if km > 0:
                result["strip"]["runs"].append([round(100 * e / end, 2), round(100 * (e + km) / end, 2), _cls(r.get("b"))])
                e += km

    if len(buses) < 2:
        na = "Needs at least 2 live buses"
        result.update(risk="nodata", checks=[_chk(i, n, "na", na) for i, n in
                      enumerate(["En-route spacing", "Traffic", "Last 15% recovery", "Early arrival", "Normalisation"], 1)],
                      rec={"code": "NONE", "headline": "Insufficient live data", "from_hw": cur, "to_hw": cur, "actions": [],
                           "rationale": f"Only {len(buses)} bus with a live position was found on this route right now, "
                                        "so gaps between buses cannot be predicted.", "bc_message": "", "confidence": "Low"},
                      departures=None)
        return result

    final_from = 100 - C["FINAL_PCT"]
    fmt = lambda x: f"{x:.0f}" if x is not None and abs(x - round(x)) < 0.05 else f"{x:.1f}"

    # ---- 1 EN-ROUTE: leading bus pulling away AND the gap behind it growing
    c1 = None
    if H is None:
        chk1 = _chk(1, "En-route spacing", "na", "Scheduled headway unknown")
    else:
        thr = H + max(H * C["EARLY_GAP_PCT"] / 100, C["EARLY_GAP_MIN"])
        pulled = [g for g in gaps if g["gap"] is not None and g["gap"] >= thr]
        hit = [g for g in pulled if g["trend"] is not None and g["trend"] >= C["TREND_MIN"]]
        if hit and hd("c1") >= C["C1_PERSIST_MIN"]:
            c1 = max(hit, key=lambda g: g["gap"])
            chk1 = _chk(1, "En-route spacing", "triggered",
                        f"Bus #{c1['lead']} is {fmt(c1['gap'])} min ahead of the bus behind (scheduled {H}) and the gap "
                        f"grows +{fmt(c1['trend'])} min downstream (held {fmt(min(hd('c1'), 99))} min).")
        elif hit:
            g = max(hit, key=lambda g: g["gap"])
            chk1 = _chk(1, "En-route spacing", "watch", f"Bus #{g['lead']} is {fmt(g['gap'])} min ahead and the gap is growing - "
                        f"confirming ({fmt(hd('c1'))} of {fmt(C['C1_PERSIST_MIN'])} min) before contacting the BC.")
        elif pulled:
            g = max(pulled, key=lambda g: g["gap"])
            chk1 = _chk(1, "En-route spacing", "watch", f"Bus #{g['lead']} is {fmt(g['gap'])} min ahead of the bus behind "
                        f"(scheduled {H}) but the gap is not clearly growing.")
        else:
            chk1 = _chk(1, "En-route spacing", "clear", f"No bus is pulling away (threshold {fmt(thr)} min vs scheduled {H}).")

    # ---- 2 TRAFFIC: PROLONGED congestion ahead of several buses AND delay the terminal layover cannot absorb
    c2, to2, cond2 = None, None, False
    if tm is None:
        chk2 = _chk(2, "Traffic", "na", "LTA traffic feed unavailable")
    elif n_aff < C["MULTI"]:
        chk2 = _chk(2, "Traffic", "clear", f"{n_aff} bus(es) face prolonged congestion (needs {C['MULTI']}+ buses, a slow stretch of "
                    f">= {fmt(C['PROLONG_KM'])} km costing >= {fmt(C['AFFECT_MIN'])} min). Brief slow spots are ignored.")
    elif heavy:
        chk2 = _chk(2, "Traffic", "clear", f"Heavy-layover duty ({fmt(L)} min layover): ~{fmt(delay2)} min of delay is absorbed "
                    "at the terminal. No adjustment.")
    elif delay2 < trigger:
        chk2 = _chk(2, "Traffic", "clear", f"Congestion ahead costs ~{fmt(delay2)} min, but layover ({fmt(L)} min, minimum {fmt(C['MIN_LAYOVER'])}) "
                    f"absorbs {fmt(slack)} min (+{fmt(C['LATE_MARGIN'])} margin). No adjustment.")
    else:
        cond2 = True
        if hd("c2") >= C["PERSIST_MIN"] and H:
            excess = delay2 - slack
            k = min(C["MAX_EXTEND"], 1 if excess < 5 else 2 if excess < 8 else 3)
            to2, c2 = H + k, {"k": k}
            chk2 = _chk(2, "Traffic", "triggered", f"{n_aff} buses face prolonged congestion (held {fmt(min(hd('c2'), 99))} min); "
                        f"~{fmt(delay2)} min delay exceeds the layover slack of {fmt(slack)} min ({fmt(L)} min layover, minimum "
                        f"{fmt(C['MIN_LAYOVER'])}).")
        else:
            chk2 = _chk(2, "Traffic", "watch", f"{n_aff} buses face prolonged congestion (~{fmt(delay2)} min delay > {fmt(slack)} min slack). "
                        f"Confirming it persists ({fmt(hd('c2'))} of {fmt(C['PERSIST_MIN'])} min) - no adjustment yet.")

    # ---- 3 LAST-15% RECOVERY: traffic adjustment active AND affected buses now near on-time (held, so it does not flip-flop)
    c3 = False
    clear_now = tm is not None and not cond2 and ok_delay and (n_aff < C["MULTI"] or all(b["pct"] >= final_from for b in aff))
    if not (adj and H and adj > H):
        chk3 = _chk(3, "Last 15% recovery", "clear", "No traffic (longer-HW) adjustment is active.")
    elif tm is None:
        chk3 = _chk(3, "Last 15% recovery", "na", "Cannot confirm recovery without the traffic feed.")
    elif clear_now and hd("clear") >= C["CLEAR_MIN"]:
        c3 = True
        chk3 = _chk(3, "Last 15% recovery", "triggered", f"Affected buses are in the last {C['FINAL_PCT']}% or clear, and remaining delay "
                    f"(~{fmt(delay2)} min) is within layover slack. Held {fmt(min(hd('clear'), 99))} min.")
    elif clear_now:
        chk3 = _chk(3, "Last 15% recovery", "watch", f"Recovering - confirming ({fmt(hd('clear'))} of {fmt(C['CLEAR_MIN'])} min) before restoring HW.")
    else:
        chk3 = _chk(3, "Last 15% recovery", "watch", f"Adjustment {H}->{adj} still needed: {n_aff} bus(es) with prolonged congestion, ~{fmt(delay2)} min delay.")

    # ---- 4 EARLY ARRIVAL: several consecutive buses in the final 15% AND early by > 5 min
    c4, to4, nf = False, None, 0
    for b in buses:
        if b["pct"] >= final_from:
            nf += 1
        else:
            break
    cond4 = bool(tm and ctx.get("plan_override") and H and nf >= C["MULTI"] and route_dev <= -C["EARLY_MIN"] and not (adj and adj > H))
    if tm is None:
        chk4 = _chk(4, "Early arrival", "na", "LTA traffic feed unavailable")
    elif not ctx.get("plan_override"):
        chk4 = _chk(4, "Early arrival", "na", "Needs your timetable running time (LTA publishes none), so early arrival is not judged.")
    elif cond4 and hd("c4") < C["CLEAR_MIN"]:
        chk4 = _chk(4, "Early arrival", "watch", f"{nf} buses look early; confirming ({fmt(hd('c4'))} of {fmt(C['CLEAR_MIN'])} min).")
    elif cond4:
        c4, to4 = True, max(1, H - C["MAX_SHORTEN"])
        chk4 = _chk(4, "Early arrival", "triggered", f"{nf} consecutive buses are in the last {C['FINAL_PCT']}% and the route is "
                    f"running {abs(route_dev):.1f} min faster than plan (plan {plan_src}).")
    else:
        chk4 = _chk(4, "Early arrival", "clear", f"{nf} consecutive bus(es) in the last {C['FINAL_PCT']}%; running time "
                    f"{route_dev:+.1f} min vs plan (needs {C['MULTI']}+ buses and > {C['EARLY_MIN']:.0f} min early).")

    # ---- 5 NORMALISATION: an adjustment is active but everything is back to normal
    c5 = False
    if not adj_active:
        chk5 = _chk(5, "Normalisation", "clear", "Scheduled headway is in effect.")
    else:
        tol = max(C["NORMAL_GAP_MIN"], (H or 0) * C["NORMAL_GAP_PCT"] / 100)
        normal = bool(known) and all(abs(g - H) <= tol for g in known) and clear_now
        if normal and not (c1 or c2 or c3 or c4) and hd("clear") >= C["CLEAR_MIN"]:
            c5 = True
            chk5 = _chk(5, "Normalisation", "triggered", f"Gaps are within +/-{fmt(tol)} min of scheduled {H} and traffic is "
                        "back to normal.")
        elif normal and not (c1 or c2 or c3 or c4):
            chk5 = _chk(5, "Normalisation", "watch", f"Looks normal - confirming ({fmt(hd('clear'))} of {fmt(C['CLEAR_MIN'])} min).")
        else:
            chk5 = _chk(5, "Normalisation", "watch", "Adjustment is active; service has not fully normalised yet.")
    checks = [chk1, chk2, chk3, chk4, chk5]

    # ---- recommendation (priority: restore > extend > shorten > regulate > normalise)
    actions = []
    if c3:
        actions.append(("RESTORE", adj, H, "Cancel traffic adjustment", 3))
    if c2:
        if adj and adj >= to2:
            actions.append(("HOLD", adj, adj, f"Keep departure HW at {adj} min (traffic adjustment in effect)", 2))
        else:
            actions.append(("EXTEND", cur, to2, f"Extend departure HW {cur} -> {to2} min", 2))
    if c4:
        if adj and adj <= to4:
            actions.append(("HOLD", adj, adj, f"Keep departure HW at {adj} min (early-arrival adjustment in effect)", 4))
        else:
            actions.append(("SHORTEN", cur, to4, f"Shorten departure HW {cur} -> {to4} min", 4))
    if c1:
        actions.append(("REGULATE", cur, cur, f"Comm BC: regulate spacing (Bus #{c1['lead']} ahead)", 1))
    if c5:
        actions.append(("NORMALISE", adj, H, f"Restore scheduled HW {adj} -> {H} min", 5))
    if not actions and adj_active:
        actions.append(("HOLD", adj, adj, f"Keep departure HW at {adj} min (adjustment in effect)", 0))
    if not actions:
        actions.append(("NONE", cur, cur, "No adjustment needed", 0))
    code, f_hw, t_hw, headline, _ = actions[0]
    also = [{"code": a[0], "headline": a[3], "check": a[4]} for a in actions[1:] if a[0] != code]

    crit = (c2 and delay2 >= trigger + 4) or (c1 and c1["gap"] >= 1.5 * (H or 1e9))
    risk = "critical" if crit else ("developing" if (c1 or c2 or c3 or c4 or c5) else "stable")
    watch = next((c["detail"] for c in checks if c["state"] == "watch" and c["id"] in (1, 2)), None)
    if chk2["state"] == "watch" and cond2:
        watch = f"Congestion ahead - confirming {fmt(min(hd('c2'), C['PERSIST_MIN']))}/{fmt(C['PERSIST_MIN'])} min"
    elif chk1["state"] == "watch":
        watch = "Gap growing - confirming"
    elif adj_active and not (c1 or c2 or c3 or c4 or c5):
        watch = "Adjustment in effect"

    # departure ladder: next departures from the origin, spaced at the new HW
    orig = result["origin_etas"]
    dep = None
    if orig and code in ("EXTEND", "SHORTEN", "RESTORE", "NORMALISE") and t_hw:
        adjd = [orig[0]]
        for _ in orig[1:]:
            adjd.append(adjd[-1] + t_hw)
        dep = {"orig": orig, "adjusted": adjd}

    # predicted arrival + next trip / departure timing at the terminal (min layover applies; heavy-layover duties are left alone)
    T = t_hw if (code in ("EXTEND", "SHORTEN", "RESTORE", "NORMALISE") and t_hw) else cur
    nd = ctx.get("next_dir") or ctx["direction"]
    clock = lambda m: (now + timedelta(minutes=m)).strftime("%H:%M")
    turn, prev = [], None
    for b in sorted([b for b in buses if b["eta_term"] is not None], key=lambda b: b["eta_term"]):
        arr = b["eta_term"]
        earliest = arr + C["MIN_LAYOVER"]
        want = 0.0 if (heavy or prev is None or not T) else max(0.0, prev + T - earliest)     # hold needed for full spacing
        hold = 0.0 if heavy else min(C["MAX_HOLD"], want)
        rel = arr + L if heavy else earliest + hold
        capped = (not heavy) and want > C["MAX_HOLD"] + 0.05
        prev = rel
        turn.append({"id": b["id"], "arr": _r(arr), "arr_clock": clock(arr), "src": b["term_src"], "next_dir": nd, "capped": capped,
                     "earliest_clock": clock(earliest), "dep_clock": clock(rel), "layover": _r(rel - arr), "hold": _r(hold if hold >= 1 else 0.0)})
    result["turn"] = {"rows": turn, "hw": T, "min_layover": C["MIN_LAYOVER"], "heavy": heavy, "loop": nd == ctx["direction"], "max_hold": C["MAX_HOLD"]}
    result["conds"] = {"c1": bool(hit) if H else False, "c2": cond2, "c4": cond4, "clear": bool(clear_now)}
    result["watch"] = watch

    # rationale + BC message
    facts = []
    if H:
        facts.append(f"Scheduled headway {H} min ({H_src}).")
    if route_dev is not None and ctx.get("plan_override"):
        facts.append(f"Predicted running time {fmt(pred_run)} min vs your timetable {fmt(plan_run)} min: {route_dev:+.1f} min.")
    if tm is not None:
        facts.append(f"Delay from prolonged congestion ahead ~{fmt(delay2)} min; layover {fmt(L)} min ({layinfo['src']}) absorbs {fmt(slack)} min.")
    facts.append(f"{len(buses)} live buses; gaps behind: " + (", ".join(f"#{g['lead']} {fmt(g['gap'])} min" for g in gaps if g["gap"] is not None) or "n/a") + ".")
    why = {"EXTEND": f"Congestion is affecting {n_aff} buses and terminal arrivals are predicted late. A temporarily longer departure headway "
                     f"({f_hw} -> {t_hw} min) spreads departures to match the slower running time. Cancel once affected buses recover.",
           "SHORTEN": "Several consecutive buses in the final stretch are predicted well ahead of plan, so there is spare layover. "
                      f"A temporarily shorter headway ({f_hw} -> {t_hw} min) uses it. Restore the scheduled HW when arrivals normalise.",
           "RESTORE": f"Buses that were slowed by traffic are near the terminal or clear of congestion and predicted near on-time. Cancel the "
                      f"traffic adjustment and restore departure HW to {t_hw} min.",
           "NORMALISE": f"Gaps and running time are back within normal range. Restore the scheduled departure HW ({t_hw} min).",
           "REGULATE": "The leading bus is pulling away from the bus behind and the gap is still growing. Ask the Bus Captain to regulate speed / "
                       "hold at the next control point before the gap becomes a service gap.",
           "HOLD": "The current adjustment is still appropriate; keep monitoring.",
           "NONE": "Headways are within the normal range and running time is close to plan."}[code]
    bc = ""
    if c1:
        bc = (f"Svc {ctx['service']} Dir {ctx['direction']}: Bus #{c1['lead']} is running {fmt(c1['gap'])} min ahead of the following bus "
              f"(scheduled HW {H}). Please regulate speed / hold at the next control point to maintain spacing.")
    elif code in ("EXTEND", "SHORTEN"):
        bc = (f"Svc {ctx['service']} Dir {ctx['direction']}: departure HW temporarily {f_hw} -> {t_hw} min "
              f"({'traffic delays' if code == 'EXTEND' else 'early arrivals'}). Space departures accordingly until advised.")
    elif code in ("RESTORE", "NORMALISE"):
        bc = f"Svc {ctx['service']} Dir {ctx['direction']}: service back to normal. Resume scheduled departure HW {t_hw} min."

    conf = 3
    conf -= (len(buses) < 3) + (len(buses) < 2)
    conf -= (sched is None)
    conf -= (tm is None)
    conf -= (lay is None and bool(c2))
    result.update(risk=risk, checks=checks, departures=dep,
                  rec={"code": code, "headline": headline, "from_hw": f_hw, "to_hw": t_hw, "actions": also,
                       "rationale": " ".join(facts) + " " + why, "bc_message": bc,
                       "confidence": "High" if conf >= 3 else "Medium" if conf == 2 else "Low"})
    return result
