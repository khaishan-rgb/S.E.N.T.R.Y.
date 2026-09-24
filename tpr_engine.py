"""Zero-history Time Period Report (TPR) engine - V13.8.

Builds an operator-style Time Period Report (stop pair x time period) WITHOUT completed-trip history:

    section time(h) = driving(h) + signals + dwell(h)
    driving(h)  = live LTA speed-band time for the section, re-scaled to hour h with an hourly speed profile
                  (never faster than free-flow)
    signals     = junctions x 3 s   (junctions ~ 2.5 per km, same estimate as the Day-1 estimate)
    dwell(h)    = for every intermediate stop:  P(stop) x (base 6.06 s + deceleration 8.8 s + queue 0.085 x 8 s)
                                               + 1.52 s x passengers boarding/alighting per bus
                  passengers per bus = DataMall passenger volume of that stop and hour / days in month
                                       / services at the stop / buses of this service in that hour
                  P(stop) = 1 - exp(-passengers)   (a bus with nobody to board or alight skips the stop)

All inputs are pure data; app.py does the I/O. Every number that is an assumption is exported in ASSUMPTIONS.
"""
import calendar
import math
import random

VERSION = "tpr-zero-history-1.0"

# Relative road speed by hour (1.00 = weekday off-peak). Engineering profile, NOT measured history.
# Live speed bands anchor the level; this profile only shapes how the other hours differ from "now".
SPEED_PROFILE = {
    "Weekday":  [1.15, 1.20, 1.20, 1.20, 1.20, 1.12, 1.00, 0.82, 0.80, 0.90, 0.97, 0.97,
                 0.95, 0.96, 0.96, 0.94, 0.90, 0.80, 0.78, 0.88, 0.97, 1.03, 1.08, 1.12],
    "Saturday": [1.12, 1.20, 1.20, 1.20, 1.20, 1.15, 1.10, 1.04, 1.00, 0.96, 0.93, 0.92,
                 0.91, 0.91, 0.92, 0.92, 0.91, 0.90, 0.90, 0.93, 0.98, 1.03, 1.07, 1.10],
    "Sunday":   [1.12, 1.20, 1.20, 1.20, 1.20, 1.18, 1.14, 1.10, 1.06, 1.01, 0.97, 0.95,
                 0.94, 0.94, 0.95, 0.95, 0.94, 0.93, 0.93, 0.95, 0.99, 1.04, 1.08, 1.10],
}
SERVICE_HOURS = [0] + list(range(5, 24))      # hours 01-04 have no regular service and are left out
FALLBACK_KMH = 25.0                           # off-peak bus speed used only when live speed bands are unavailable

ASSUMPTIONS = {
    "dwell_base_s": 6.06, "dwell_per_pax_s": 1.52, "decel_s": 8.8, "queue_prob": 0.085, "queue_s": 8.0,
    "junctions_per_km": 2.5, "signal_s_per_junction": 3.0, "fallback_kmh": FALLBACK_KMH, "max_pax_per_bus": 60.0,
}

# Slot schemes. "tpr" follows the operator TPR layout (22 slots, half-hour in the peaks).
SCHEMES = {
    "tpr": [("0000", "0559"), ("0600", "0629"), ("0630", "0659"), ("0700", "0729"), ("0730", "0759"), ("0800", "0829"),
            ("0830", "0859"), ("0900", "0929"), ("0930", "0959"), ("1000", "1159"), ("1200", "1259"), ("1300", "1359"),
            ("1400", "1659"), ("1700", "1729"), ("1730", "1759"), ("1800", "1859"), ("1900", "1929"), ("1930", "1959"),
            ("2000", "2059"), ("2100", "2159"), ("2200", "2259"), ("2300", "2359")],
    "hourly": [("0000", "0059")] + [(f"{h:02d}00", f"{h:02d}59") for h in range(5, 24)],
}


def slot_hours(a, b):
    """Service hours covered by a slot, e.g. ('1000','1159') -> [10, 11]; ('0000','0559') -> [0, 5]."""
    h0, h1 = int(a[:2]), int(b[:2])
    hs = [h for h in range(h0, h1 + 1) if h in SERVICE_HOURS]
    return hs or [h0]


def days_in_month(month, day_type):
    """month 'YYYYMM' -> number of weekdays, or of weekend days (public holidays not separated)."""
    try:
        y, m = int(month[:4]), int(month[4:6])
    except (TypeError, ValueError):
        return 22 if day_type == "Weekday" else 8
    n = calendar.monthrange(y, m)[1]
    wd = sum(1 for d in range(1, n + 1) if calendar.weekday(y, m, d) < 5)
    return wd if day_type == "Weekday" else max(1, n - wd)


def dwell_sec(pax):
    a = ASSUMPTIONS
    p_stop = 1.0 - math.exp(-max(0.0, pax))
    fixed = a["dwell_base_s"] + a["decel_s"] + a["queue_prob"] * a["queue_s"]
    return p_stop * fixed + a["dwell_per_pax_s"] * pax


def build(*, stops, seg_live_min, seg_free_min, seg_km, day_type, now_hour, now_day_type, traffic_live,
          pax_fn, default_pax, scheme="tpr", picked=None, pctl=85, recovery_min=7.0, draws=1000):
    """
    stops         [{code, name, seq}] in route order (n stops)
    seg_live_min  n-1 driving minutes between consecutive stops at the time of the request (live bands) or None
    seg_free_min  n-1 free-flow minutes (floor for any hour) or None
    seg_km        n-1 km
    pax_fn(code, hour) -> passengers boarding+alighting per bus at that stop in that hour, or None if unknown
    picked        stop codes chosen as timing points (first/last stop always added)
    Returns dict with slots, per-stop-segment hourly components and the picked-pair TPR.
    """
    n = len(stops)
    prof = SPEED_PROFILE.get(day_type, SPEED_PROFILE["Weekday"])
    now_f = SPEED_PROFILE.get(now_day_type, SPEED_PROFILE["Weekday"])[now_hour % 24]
    a = ASSUMPTIONS
    hours = SERVICE_HOURS
    # per stop segment, per hour
    drive = [[0.0] * 24 for _ in range(n - 1)]
    sig = [0.0] * (n - 1)
    dwell = [[0.0] * 24 for _ in range(n - 1)]
    pax_known, pax_total = 0, 0
    for i in range(n - 1):
        km = max(0.0, seg_km[i] or 0.0)
        sig[i] = km * a["junctions_per_km"] * a["signal_s_per_junction"] / 60.0
        for h in hours:
            if traffic_live and seg_live_min and seg_live_min[i] is not None:
                t = seg_live_min[i] * now_f / prof[h]
                if seg_free_min and seg_free_min[i]:
                    t = max(t, seg_free_min[i])
            else:
                t = km / (FALLBACK_KMH * prof[h]) * 60.0
            drive[i][h] = t
            # dwell happens at the stop that ends this segment, except the final terminus
            if i + 1 < n - 1:
                p = pax_fn(stops[i + 1]["code"], h)
                pax_total += 1
                if p is None:
                    p = default_pax
                else:
                    pax_known += 1
                dwell[i][h] = dwell_sec(min(p, a["max_pax_per_bus"])) / 60.0
    slots = SCHEMES.get(scheme, SCHEMES["tpr"])
    labels = [f"{x}-{y}" for x, y in slots]
    sh = [slot_hours(x, y) for x, y in slots]

    def agg(arr_h):
        return [sum(arr_h[h] for h in hs) / len(hs) for hs in sh]

    # timing points
    codes = [s["code"] for s in stops]
    pk = [c for c in (picked or []) if c in codes]
    idx = sorted({codes.index(c) for c in pk} | {0, n - 1})
    pairs = []
    for k in range(len(idx) - 1):
        i0, i1 = idx[k], idx[k + 1]
        tr = [0.0] * 24
        dw = [0.0] * 24
        for i in range(i0, i1):
            for h in hours:
                tr[h] += drive[i][h] + sig[i]
                dw[h] += dwell[i][h]
        t_s, d_s = agg(tr), agg(dw)
        pairs.append({"from": codes[i0], "to": codes[i1], "from_name": stops[i0].get("name", ""), "to_name": stops[i1].get("name", ""),
                      "from_seq": i0 + 1, "to_seq": i1 + 1, "stops_between": i1 - i0 - 1, "km": round(sum(seg_km[i0:i1]), 2),
                      "travel": [round(x, 1) for x in t_s], "dwell": [round(x, 1) for x in d_s],
                      "prop": [round(x + y, 1) for x, y in zip(t_s, d_s)]})
    total = {k: [round(sum(p[k][j] for p in pairs), 1) for j in range(len(labels))] for k in ("travel", "dwell", "prop")}
    # recommended scheduled running time per slot: route total with day-to-day variation (same spread as the Day-1 estimate)
    rnd = random.Random(202646)
    rec = []
    for j in range(len(labels)):
        tr, dw = total["travel"][j], total["dwell"][j]
        v = sorted(tr * max(.72, rnd.normalvariate(1.0, .075)) + dw * max(0., rnd.normalvariate(1.0, .18))
                   + max(0., rnd.normalvariate(recovery_min, max(.35, recovery_min * .10))) for _ in range(draws))
        rec.append(round(v[min(len(v) - 1, int(round((len(v) - 1) * pctl / 100.0)))], 1))
    total["recommended"] = rec
    return {"slots": labels, "pctl": int(pctl), "recovery_min": recovery_min, "slot_hours": sh, "pairs": pairs, "total": total, "picked": [codes[i] for i in idx],
            "pax_coverage": round(100.0 * pax_known / pax_total, 0) if pax_total else 0.0,
            "profile": prof, "now_factor": now_f}
