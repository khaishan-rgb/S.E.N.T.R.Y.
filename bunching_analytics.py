"""Bunching & Gap analytics (pure stdlib, no I/O, unit-testable).

Two measures, same charts (top services, hour of day, weekday x hour, daily trend, hotspot roads and stops, bunching level):
  * time  - minutes of bunching / long gap OBSERVED by the live collector (table bb_obs: every collector cycle adds its interval
            to the hour, service, direction, level and stop where it was seen). Fills as soon as the dashboard runs.
  * cases - LOGGED cases (bunching_event / gap_event + cases still open): only cases that lasted the required number of bus stops (15).
Nothing here is estimated or invented: no records gives empty charts.
Times are Singapore time (UTC+8, no daylight saving).
"""
import re
import statistics
import time
from collections import Counter, defaultdict

SGT = 8 * 3600
DAY = 86400
PERIODS = {"today": 1, "yesterday": 1, "7d": 7, "30d": 30}
DUR_EDGES = (0, 20, 30, 40, 60)                    # bus-stop buckets: <20, 20-29, 30-39, 40-59, 60+
DUR_LABELS = ("<20", "20–29", "30–39", "40–59", "60+")
_CODE = re.compile(r"\((\d{5})\)\s*$")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def sgt_day_start(ts):
    """Unix time of 00:00 SGT on the day containing ts."""
    return (int((ts + SGT) // DAY)) * DAY - SGT


def period_bounds(period, now=None):
    """-> {key, start, end, prev_start, days, label}. end is exclusive; prev = the same length just before."""
    now = time.time() if now is None else now
    key = period if period in PERIODS else "7d"
    d0, n = sgt_day_start(now), PERIODS[key]
    if key == "today":                      # today so far vs yesterday up to the same time
        start, end, ps, pe = d0, now, d0 - DAY, now - DAY
    elif key == "yesterday":                # yesterday vs the day before
        start, end, ps, pe = d0 - DAY, d0, d0 - 2 * DAY, d0 - DAY
    else:                                   # the last n days (today included) vs the n days before them
        start, end = d0 - (n - 1) * DAY, now
        ps, pe = start - n * DAY, start
    label = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "30d": "Last 30 days"}[key]
    prev = {"today": "yesterday at this time", "yesterday": "the day before", "7d": "the 7 days before", "30d": "the 30 days before"}[key]
    return {"key": key, "start": start, "end": end, "prev_start": ps, "prev_end": pe, "days": n, "label": label, "prev_label": prev}


def local_parts(ts):
    """(date 'YYYY-MM-DD', weekday 0=Mon, hour) in SGT."""
    t = time.gmtime(ts + SGT)
    return time.strftime("%Y-%m-%d", t), t.tm_wday, t.tm_hour


def stop_of(label, stops):
    """'Name (12345)' -> {code, name, road, lat, lon} using the LTA stop list when the code is known."""
    label = (label or "").strip()
    m = _CODE.search(label)
    code = m.group(1) if m else ""
    s = (stops or {}).get(code) if code else None
    name = (s or {}).get("name") or (label[:m.start()].strip() if m else label) or "Unknown stop"
    return {"code": code, "name": name, "road": ((s or {}).get("road") or "").strip(), "lat": (s or {}).get("lat"), "lon": (s or {}).get("lon")}


def _keep(e, kind, direction, service, operator, opmap, services=None):
    if kind in ("bb", "gap") and e.get("kind") != kind:
        return False
    if direction in (1, 2) and int(e.get("direction") or 0) != direction:
        return False
    if service and str(e.get("service", "")).upper() != service.upper():
        return False
    if services and str(e.get("service", "")).upper() not in services:
        return False
    if operator and (opmap or {}).get(str(e.get("service", "")).upper()) != operator:
        return False
    return True


def _w(e):
    return float(e.get("w", 1) or 0)


def _r(x):
    return round(x, 1) if isinstance(x, float) else x


def aggregate(events, prev_events, stops, p, kind="all", direction=0, service="", operator="", opmap=None, top_n=10, services=None):
    """events / prev_events: [{kind 'bb'|'gap', service, direction, start, end, level, stops, start_stop, alerts, open?, w?}].
    w = weight of the record: 1 for a case (default), minutes for a time observation."""
    svc_filter = {s.upper() for s in (services or []) if s}
    ev = [e for e in (events or []) if e.get("start") is not None and p["start"] <= e["start"] < p["end"] and _keep(e, kind, direction, service, operator, opmap, svc_filter)]
    pv = [e for e in (prev_events or []) if e.get("start") is not None and _keep(e, kind, direction, service, operator, opmap, svc_filter)]
    n_bb = sum(_w(e) for e in ev if e["kind"] == "bb")
    n_gap = sum(_w(e) for e in ev if e["kind"] == "gap")

    svc = {}
    hours = [{"h": h, "bb": 0, "gap": 0} for h in range(24)]
    heat = [[0] * 24 for _ in range(7)]
    days = {}
    roads, stops_out = {}, {}
    lv = Counter()
    lens, durs, worst = [], [], None
    dur_b = [{"label": DUR_LABELS[i], "bb": 0, "gap": 0} for i in range(len(DUR_EDGES))]
    for e in ev:
        k, w = e["kind"], _w(e)
        s = str(e.get("service", ""))
        d = int(e.get("direction") or 0)
        x = svc.setdefault(s, {"service": s, "events": 0, "bb": 0, "gap": 0, "d1": 0, "d2": 0, "stops": 0, "max_stops": 0})
        x["events"] += w
        x[k] += w
        if d in (1, 2):
            x[f"d{d}"] += w
        n = int(e.get("stops") or 0)
        x["stops"] += n
        x["max_stops"] = max(x["max_stops"], n)
        date, wd, hr = local_parts(e["start"])
        hours[hr][k] += w
        heat[wd][hr] += w
        dd = days.setdefault(date, {"date": date, "bb": 0, "gap": 0})
        dd[k] += w
        if k == "bb":
            lv[min(4, max(2, int(e.get("level") or 2)))] += w
        if n:
            lens.append(n)
            i = max(j for j, edge in enumerate(DUR_EDGES) if n >= edge)
            dur_b[i][k] += 1
            if worst is None or n > worst["stops"]:
                worst = {"service": s, "direction": d, "kind": k, "stops": n, "start": e["start"], "where": stop_of(e.get("start_stop"), stops)["name"]}
        if e.get("end") and e["end"] > e["start"]:
            durs.append((e["end"] - e["start"]) / 60)
        st = stop_of(e.get("start_stop"), stops)
        key = st["code"] or st["name"]
        so = stops_out.setdefault(key, {**st, "events": 0, "bb": 0, "gap": 0, "svc": Counter(), "len": 0, "n": 0})
        so["events"] += w
        so[k] += w
        so["svc"][s] += w
        so["len"] += n
        so["n"] += 1 if n else 0
        road = st["road"] or "Road not known"
        ro = roads.setdefault(road, {"road": road, "events": 0, "bb": 0, "gap": 0, "svc": Counter(), "stops": set()})
        ro["events"] += w
        ro[k] += w
        ro["svc"][s] += w
        ro["stops"].add(key)

    # every day of the period, also the quiet ones (a gap in the bars must mean "no cases", not "missing")
    day_list = []
    t = p["start"]
    while t < p["end"]:
        date, wd, _ = local_parts(t + 1)
        day_list.append({**days.get(date, {"date": date, "bb": 0, "gap": 0}), "wd": WEEKDAYS[wd]})
        t += DAY

    services = sorted(svc.values(), key=lambda x: (-x["events"], -x["max_stops"], x["service"]))
    road_list = sorted(roads.values(), key=lambda r: (-r["events"], r["road"]))
    stop_list = sorted(stops_out.values(), key=lambda s: (-s["events"], s["name"]))
    return _round({
        "period": p, "filters": {"kind": kind, "direction": direction, "service": service, "operator": operator, "services": sorted(svc_filter)},
        "totals": {"events": sum(_w(e) for e in ev), "records": len(ev), "bb": n_bb, "gap": n_gap, "open": sum(1 for e in ev if e.get("open")),
                   "median_stops": statistics.median(lens) if lens else None, "median_min": round(statistics.median(durs), 1) if durs else None,
                   "alerts": sum(int(e.get("alerts") or 0) for e in ev), "services": len(svc), "worst": worst,
                   "levels": {"2": lv[2], "3": lv[3], "4": lv[4]}},
        "prev": {"events": sum(_w(e) for e in pv), "bb": sum(_w(e) for e in pv if e["kind"] == "bb"), "gap": sum(_w(e) for e in pv if e["kind"] == "gap")},
        "services": services[:top_n], "services_total": len(services),
        "hours": hours, "heat": heat, "days": day_list, "durations": dur_b,
        "roads": [{"road": r["road"], "events": r["events"], "bb": r["bb"], "gap": r["gap"], "stops": len(r["stops"]),
                   "services": [s for s, _ in r["svc"].most_common(6)]} for r in road_list[:8]],
        "stops": [{"code": s["code"], "name": s["name"], "road": s["road"], "lat": s["lat"], "lon": s["lon"], "events": s["events"], "bb": s["bb"], "gap": s["gap"],
                   "avg_stops": round(s["len"] / s["n"], 1) if s["n"] else None, "services": [x for x, _ in s["svc"].most_common(6)]} for s in stop_list[:30]],
    })


def _round(o):
    """Minutes are summed as floats: round every number to 0.1 for the browser (counts stay whole numbers)."""
    if isinstance(o, float):
        return round(o, 1) if o != int(o) else int(o)
    if isinstance(o, dict):
        return {k: (v if k in ("period",) else _round(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def coverage(rows, p):
    """rows: [{hour_ts, service, direction, sec}] from bb_cov -> how long the collector actually watched, per hour of day and per day."""
    hours, days, svc, first, total = [0.0] * 24, {}, set(), None, 0.0
    for r in rows or []:
        t = r["hour_ts"]
        if not (p["start"] - 3600 < t < p["end"]):
            continue
        date, _, hr = local_parts(t)
        sec = float(r.get("sec") or 0)
        hours[hr] += sec
        days[date] = days.get(date, 0.0) + sec
        svc.add((r["service"], int(r["direction"] or 0)))
        total += sec
        first = t if first is None or t < first else first
    hrs_watched = len({r["hour_ts"] for r in (rows or []) if p["start"] - 3600 < r["hour_ts"] < p["end"]})
    return {"hours_watched": hrs_watched, "svc_dirs": len(svc), "services": len({s for s, _ in svc}), "first_ts": first,
            "by_hour": [round(h / 3600, 2) for h in hours], "by_day": {k: round(v / 3600, 2) for k, v in days.items()}, "svc_hours": round(total / 3600, 1)}
