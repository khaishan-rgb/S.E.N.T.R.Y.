"""PROJECT INSIGHT - bus running time analytics (V13.3). Pure computation, no I/O.

Answers, for one service: which direction lacks running time, when, on which section of the route, by how many minutes,
how often, what conditions it is associated with, and what running times management could consider instead.

Method
  * Trip-level or stop-level records are normalised to trips (scheduled / actual start and end, plus stop observations where given).
  * Adequacy is never judged from one trip: every service + direction + day type + time band is summarised as a distribution
    (mean, P50, P75, P85, P90, P95, SD, n) and compared with the scheduled running time at a configurable planning percentile.
  * Selected "important" stops are auto-paired along the real stop sequence (1>5, 5>10, 10>16 ...) into sections; each section gets
    the same distribution treatment, so the route-level shortage can be attributed to where it is generated.
  * Contributors are reported as associations ("possible contributor"), never as proven causes.
  * Data that is measured, modelled or estimated is labelled as such.
"""
import math
import re
import statistics as st

VERSION = "insight-1.0"

# --------------------------------------------------------------------------- column aliases (headers are matched loosely)
ALIASES = {
    "service": ["service", "svc", "serviceno", "servicenumber", "busservice", "route", "line"],
    "direction": ["direction", "dir", "d"],
    "date": ["operatingdate", "date", "servicedate", "opdate", "tripdate"],
    "daytype": ["daytype", "dow", "daycategory", "typeofday"],
    "trip_id": ["tripid", "trip", "dutyid", "duty", "tripno", "journeyid", "runid"],
    "bus": ["busreg", "busregistration", "vehicle", "vehicleno", "bus", "plate"],
    "seq": ["stopsequence", "sequence", "seq", "stopseq", "stoporder", "order"],
    "code": ["busstopcode", "stopcode", "code", "busstop", "stopid"],
    "name": ["busstopname", "stopname", "name", "description"],
    "sched_arr": ["scheduledarrival", "schedarrival", "scharr", "plannedarrival", "scheduledarr"],
    "act_arr": ["actualarrival", "actarrival", "actarr", "arrival", "observedarrival"],
    "sched_dep": ["scheduleddeparture", "scheddeparture", "schdep", "planneddeparture", "scheduleddep"],
    "act_dep": ["actualdeparture", "actdeparture", "actdep", "departure", "observeddeparture"],
    "sched_start": ["scheduledtripstart", "schedtripstart", "schstart", "scheduledstart", "plannedstart", "scheduleddeparturetime"],
    "act_start": ["actualtripstart", "acttripstart", "actstart", "actualstart", "observedstart"],
    "sched_end": ["scheduledtripend", "schedtripend", "schend", "scheduledend", "plannedend", "scheduledarrivaltime"],
    "act_end": ["actualtripend", "acttripend", "actend", "actualend", "observedend"],
    "sched_rt": ["scheduledrunningtime", "schedrt", "schrt", "scheduledrt", "plannedrunningtime"],
    "act_rt": ["actualrunningtime", "actualrt", "actrt", "runningtime", "observedrunningtime"],
    "traffic_speed": ["trafficspeed", "speed", "avgspeed", "roadspeed", "kmh"],
    "dwell": ["dwell", "dwelltime", "stopdwell", "boardingtime"],
    "roadworks": ["roadworks", "roadwork", "works"],
    "incident": ["incident", "trafficincident", "accident"],
    "weather": ["weather", "rain", "raining", "rainfall"],
    "demand": ["passengerdemand", "demand", "boardings", "passengers", "load", "ridership"],
    "event": ["specialevent", "event", "holiday", "schoolholiday", "publicholiday", "ph"],
}
FIELDS = {a: k for k, v in ALIASES.items() for a in v}
COND_FIELDS = ("traffic_speed", "dwell", "roadworks", "incident", "weather", "demand", "event")
DAY_TYPES = ("Weekday", "Saturday", "Sunday/PH")


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def parse_time(v, base=None):
    """'HH:MM', 'HH:MM:SS', ISO date-time, or minutes -> minutes after midnight (float). None if unusable."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "null", "-", "na", "n/a"):
        return None
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        t = int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3)) / 60.0 if m.group(3) else 0.0)
        if re.search(r"\b(\d{4}-\d{2}-\d{2})|\b(\d{2}/\d{2}/\d{4})", s) and t < 3 * 60:
            t += 1440.0                                   # a date-time just after midnight belongs to the previous service day
        return t
    try:
        f = float(s)
        return f if 0 <= f <= 2880 else None
    except ValueError:
        return None


def parse_num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def parse_bool(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "y", "yes", "true", "t", "rain", "raining", "wet", "present"):
        return True
    if s in ("0", "n", "no", "false", "f", "none", "dry", "clear", "nil", "absent"):
        return False
    return None


def day_type(date_s, given=None):
    if given:
        g = str(given).strip().lower()
        if g.startswith(("wd", "weekday", "mon", "tue", "wed", "thu", "fri")):
            return "Weekday"
        if g.startswith(("sat",)):
            return "Saturday"
        if g.startswith(("sun", "ph", "hol")):
            return "Sunday/PH"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(date_s or ""))
    if not m:
        m2 = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(date_s or ""))
        if not m2:
            return "Weekday"
        d_, mo, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
    else:
        y, mo, d_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        import datetime
        w = datetime.date(y, mo, d_).weekday()
    except ValueError:
        return "Weekday"
    return "Weekday" if w <= 4 else ("Saturday" if w == 5 else "Sunday/PH")


def split_rows(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return []
    head = lines[0]
    sep = "\t" if head.count("\t") > head.count(",") else (";" if head.count(";") > head.count(",") else ",")
    out = []
    for l in lines:
        # simple CSV with quoted fields
        cells, cur, q = [], "", False
        for ch in l:
            if ch == '"':
                q = not q
            elif ch == sep and not q:
                cells.append(cur); cur = ""
            else:
                cur += ch
        cells.append(cur)
        out.append([c.strip().strip('"') for c in cells])
    return out


# --------------------------------------------------------------------------- normalise to trips
def parse_csv(text, max_trips=40000):
    rows = split_rows(text)
    if len(rows) < 2:
        return None, {"error": "The file has no data rows."}
    head = [_norm(h) for h in rows[0]]
    cols = {}
    for i, h in enumerate(head):
        f = FIELDS.get(h)
        if f and f not in cols:
            cols[f] = i
    if "service" not in cols:
        return None, {"error": "No Service column found. Expected a header like Service / Svc / ServiceNo."}
    get = lambda r, f: (r[cols[f]] if f in cols and cols[f] < len(r) else None)
    trips, order, bad, stop_level = {}, [], 0, ("code" in cols or "seq" in cols) and ("act_arr" in cols or "act_dep" in cols)
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        svc = (get(r, "service") or "").strip().upper()
        if not svc:
            bad += 1
            continue
        d = parse_num(get(r, "direction"))
        d = int(d) if d in (1, 2) else 1
        date = (get(r, "date") or "").strip()
        tid = (get(r, "trip_id") or "").strip()
        ss, se = parse_time(get(r, "sched_start")), parse_time(get(r, "sched_end"))
        as_, ae = parse_time(get(r, "act_start")), parse_time(get(r, "act_end"))
        key = (svc, d, date, tid or f"{ss}|{as_}")
        t = trips.get(key)
        if t is None:
            t = trips[key] = {"svc": svc, "dir": d, "date": date, "trip": tid, "bus": (get(r, "bus") or "").strip(),
                              "daytype": day_type(date, get(r, "daytype")), "ss": ss, "se": se, "as": as_, "ae": ae,
                              "stops": [], "cond": {}, "sched_rt": parse_num(get(r, "sched_rt")), "act_rt": parse_num(get(r, "act_rt"))}
            order.append(key)
            if len(order) > max_trips:
                break
        for f, v in (("ss", ss), ("se", se), ("as", as_), ("ae", ae)):
            if t[f] is None and v is not None:
                t[f] = v
        for f in COND_FIELDS:
            if f in cols:
                raw = get(r, f)
                val = parse_bool(raw) if f in ("roadworks", "incident", "weather", "event") else parse_num(raw)
                if val is not None:
                    t["cond"].setdefault(f, []).append(val)
        if "code" in cols or "seq" in cols:
            seq = parse_num(get(r, "seq"))
            t["stops"].append({"seq": int(seq) if seq is not None else len(t["stops"]) + 1, "code": (get(r, "code") or "").strip(),
                               "name": (get(r, "name") or "").strip(), "sa": parse_time(get(r, "sched_arr")), "aa": parse_time(get(r, "act_arr")),
                               "sd": parse_time(get(r, "sched_dep")), "ad": parse_time(get(r, "act_dep"))})
    out, dropped = [], 0
    for k in order:
        t = trips[k]
        t["stops"].sort(key=lambda s: s["seq"])
        for s in t["stops"]:                                     # after-midnight wrap inside a trip
            for f in ("sa", "aa", "sd", "ad"):
                if s[f] is not None and t["stops"] and s[f] + 600 < (t["stops"][0]["sd"] or t["stops"][0]["sa"] or s[f]):
                    s[f] += 1440
        if t["stops"]:
            first, last = t["stops"][0], t["stops"][-1]
            if t["ss"] is None:
                t["ss"] = first["sd"] if first["sd"] is not None else first["sa"]
            if t["as"] is None:
                t["as"] = first["ad"] if first["ad"] is not None else first["aa"]
            if t["se"] is None:
                t["se"] = last["sa"] if last["sa"] is not None else last["sd"]
            if t["ae"] is None:
                t["ae"] = last["aa"] if last["aa"] is not None else last["ad"]
        srt = t["sched_rt"] if t["sched_rt"] is not None else (None if t["ss"] is None or t["se"] is None else t["se"] - t["ss"])
        art = t["act_rt"] if t["act_rt"] is not None else (None if t["as"] is None or t["ae"] is None else t["ae"] - t["as"])
        if srt is not None and srt < 0:
            srt += 1440
        if art is not None and art < 0:
            art += 1440
        if srt is None or art is None or not (1 <= srt <= 600) or not (1 <= art <= 600):
            dropped += 1
            continue
        t["srt"], t["art"] = srt, art
        t["cond"] = {f: (sum(v) / len(v) if isinstance(v[0], float) else (sum(1 for x in v if x) / len(v))) for f, v in t["cond"].items() if v}
        out.append(t)
    dates = sorted({t["date"] for t in out if t["date"]})
    info = {"trips": len(out), "rows": len(rows) - 1, "dropped_incomplete": dropped, "bad_rows": bad, "stop_level": stop_level,
            "columns_used": sorted(cols.keys()), "columns_missing": sorted(set(("sched_start", "act_start", "sched_end", "act_end")) - set(cols.keys())),
            "conditions": sorted(f for f in COND_FIELDS if f in cols), "dates": [dates[0], dates[-1]] if dates else [None, None],
            "services": sorted({t["svc"] for t in out}), "daytypes": sorted({t["daytype"] for t in out}),
            "source": "actual" if out else "none", "truncated": len(order) > max_trips}
    if not out:
        info["error"] = "No trip could be reconstructed: scheduled and actual start / end times are needed (or stop-level arrivals)."
        return None, info
    return out, info


# --------------------------------------------------------------------------- statistics
def pct(vals, q):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def dist(vals, sched=None, pctl=85):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    d = {"n": len(v), "mean": round(sum(v) / len(v), 1), "p50": round(pct(v, 50), 1), "p75": round(pct(v, 75), 1), "p85": round(pct(v, 85), 1),
         "p90": round(pct(v, 90), 1), "p95": round(pct(v, 95), 1), "sd": round(st.pstdev(v), 1) if len(v) > 1 else 0.0,
         "min": round(min(v), 1), "max": round(max(v), 1)}
    d["planning"] = round(pct(v, pctl), 1)
    if sched is not None:
        d["sched"] = round(sched, 1)
        d["gap50"] = round(d["p50"] - sched, 1)
        d["gap85"] = round(d["p85"] - sched, 1)
        d["gap90"] = round(d["p90"] - sched, 1)
        d["gap"] = round(d["planning"] - sched, 1)
        d["reliability"] = round(100.0 * sum(1 for x in v if x <= sched + 1e-9) / len(v), 1)
    return d


def band_of(minutes, band):
    b = int(minutes // band) * band
    return b % 1440


def band_label(b, band):
    e = (b + band) % 1440
    return f"{int(b // 60) % 24:02d}:{int(b % 60):02d}\u2013{int(e // 60) % 24:02d}:{int(e % 60):02d}"


def filt(trips, svc=None, direction=None, daytype=None, date_from=None, date_to=None):
    out = []
    for t in trips:
        if svc and t["svc"] != svc:
            continue
        if direction and t["dir"] != direction:
            continue
        if daytype and daytype != "All" and t["daytype"] != daytype:
            continue
        if date_from and t["date"] and t["date"] < date_from:
            continue
        if date_to and t["date"] and t["date"] > date_to:
            continue
        out.append(t)
    return out


def drop_outliers(trips, k=4.0, band=60):
    """MAD-based outlier removal inside service + direction + hour of day, so that busy peak trips are not mistaken for outliers
    (a bus that broke down is not a running-time problem; a slow peak trip is)."""
    keep, removed = [], 0
    groups = {}
    for t in trips:
        b = band_of(t["ss"], band) if t["ss"] is not None else -1
        groups.setdefault((t["svc"], t["dir"], b), []).append(t)
    for g in groups.values():
        v = [t["art"] for t in g]
        med = pct(v, 50)
        mad = pct([abs(x - med) for x in v], 50) or 0.0
        lim = max(8.0, k * 1.4826 * mad)
        for t in g:
            if abs(t["art"] - med) <= lim:
                keep.append(t)
            else:
                removed += 1
    return keep, removed




# --------------------------------------------------------------------------- 1. management summary over every service
def summary(trips, pctl=85, band=30, min_n=10):
    out = []
    svcs = sorted({t["svc"] for t in trips}, key=lambda s: (len(s), s))
    for svc in svcs:
        row = {"service": svc, "n": 0, "periods_short": 0, "worst": None, "worst_period": None, "worst_dir": None}
        for d in (1, 2):
            g = [t for t in trips if t["svc"] == svc and t["dir"] == d]
            key = f"d{d}"
            if not g:
                row[key] = None
                continue
            sched = pct([t["srt"] for t in g], 50)
            dd = dist([t["art"] for t in g], sched, pctl)
            row[key] = dd
            row["n"] += dd["n"]
            bands = {}
            for t in g:
                if t["ss"] is not None:
                    bands.setdefault(band_of(t["ss"], band), []).append(t)
            for b, ts in bands.items():
                if len(ts) < min_n:
                    continue
                s_ = pct([x["srt"] for x in ts], 50)
                dp = dist([x["art"] for x in ts], s_, pctl)
                if (dp.get("gap") or 0) > 0.5:
                    row["periods_short"] += 1
                    if row["worst"] is None or dp["gap"] > row["worst"]:
                        row["worst"], row["worst_period"], row["worst_dir"] = dp["gap"], band_label(b, band), d
        row["no_sched"] = all((row.get(f"d{d}") or {}).get("gap") is None for d in (1, 2))
        g1, g2 = (row.get("d1") or {}).get("gap"), (row.get("d2") or {}).get("gap")
        row["direction_affected"] = "D1 & D2" if (g1 or 0) > 0.5 and (g2 or 0) > 0.5 else ("D1" if (g1 or 0) > 0.5 else ("D2" if (g2 or 0) > 0.5 else "-"))
        row["suggested_review"] = row["worst_period"] or "-"
        row["small_sample"] = row["n"] < min_n
        out.append(row)
    out.sort(key=lambda r: -(r["worst"] or -99))
    return out


# --------------------------------------------------------------------------- 2. one service: time bands per direction
def by_period(trips, svc, direction, band=30, pctl=85, min_n=5):
    g = [t for t in trips if t["svc"] == svc and t["dir"] == direction and t["ss"] is not None]
    bands = {}
    for t in g:
        bands.setdefault(band_of(t["ss"], band), []).append(t)
    rows = []
    for b in sorted(bands):
        ts = bands[b]
        sched = pct([t["srt"] for t in ts], 50)
        d = dist([t["art"] for t in ts], sched, pctl)
        d.update({"band": b, "label": band_label(b, band), "start": f"{int(b // 60) % 24:02d}:{int(b % 60):02d}",
                  "end": f"{int((b + band) // 60) % 24:02d}:{int((b + band) % 60):02d}",
                  "assessment": assess(d, min_n), "small_sample": d["n"] < min_n})
        rows.append(d)
    return rows


def assess(d, min_n=5):
    if d["n"] < min_n:
        return "Too few trips"
    if d.get("gap") is None:
        return "No timetable RT entered"
    if d["gap"] <= 0:
        return "Adequate"
    if d["gap"] <= 1:
        return "Marginal"
    if d["gap"] <= 3:
        return "Short"
    return "Clearly short"


# --------------------------------------------------------------------------- 3. important stops -> sections (auto-paired in sequence)
def stop_list(trips, svc, direction):
    """every stop of the service + direction in route sequence, from the data itself."""
    seen = {}
    for t in trips:
        if t["svc"] != svc or t["dir"] != direction:
            continue
        for s in t["stops"]:
            k = s["code"] or f"seq{s['seq']}"
            e = seen.setdefault(k, {"code": s["code"], "name": s["name"], "seq": s["seq"], "n": 0})
            e["n"] += 1
            e["seq"] = min(e["seq"], s["seq"])
            if not e["name"] and s["name"]:
                e["name"] = s["name"]
    out = sorted(seen.values(), key=lambda s: s["seq"])
    for i, s in enumerate(out, 1):
        s["pos"] = i
    return out


def pair_sections(stops, picked):
    """1,5,10,16,20 -> 1>5, 5>10, 10>16, 16>20 (the end of one section is the start of the next). Order follows the route, never reversed."""
    idx = {s["code"]: s for s in stops}
    chosen = sorted({c for c in picked if c in idx}, key=lambda c: idx[c]["seq"])
    return [{"from": chosen[i], "to": chosen[i + 1], "from_name": idx[chosen[i]]["name"], "to_name": idx[chosen[i + 1]]["name"],
             "from_seq": idx[chosen[i]]["seq"], "to_seq": idx[chosen[i + 1]]["seq"],
             "label": f"{idx[chosen[i]]['pos']}\u2192{idx[chosen[i + 1]]['pos']}", "stops": idx[chosen[i + 1]]["seq"] - idx[chosen[i]]["seq"]}
            for i in range(len(chosen) - 1)]


def _stop_times(t, code):
    for s in t["stops"]:
        if s["code"] == code:
            return s
    return None


def section_times(trips, sec):
    """actual and scheduled minutes between the two stops of a section, per trip."""
    out = []
    for t in trips:
        a, b = _stop_times(t, sec["from"]), _stop_times(t, sec["to"])
        if not a or not b:
            continue
        aa = a["ad"] if a["ad"] is not None else a["aa"]
        bb = b["aa"] if b["aa"] is not None else b["ad"]
        sa = a["sd"] if a["sd"] is not None else a["sa"]
        sb = b["sa"] if b["sa"] is not None else b["sd"]
        if aa is None or bb is None:
            continue
        act = bb - aa
        sch = (sb - sa) if (sa is not None and sb is not None) else None
        if act < 0 or act > 300:
            continue
        out.append({"trip": t, "act": act, "sch": sch, "start": t["ss"]})
    return out


def sections_analysis(trips, sections, pctl=85, band=30, min_n=5):
    rows, heat = [], []
    total_gap = 0.0
    for sec in sections:
        vals = section_times(trips, sec)
        if not vals:
            rows.append({**{k: sec[k] for k in ("label", "from", "to", "from_name", "to_name", "stops")}, "n": 0, "note": "No stop times for this section in the data"})
            continue
        sch = pct([v["sch"] for v in vals if v["sch"] is not None], 50)
        basis = "timetable"
        if sch is None:
            # no scheduled stop-to-stop time (LTA DataMall does not publish one): compare each section with its own typical level instead,
            # so the analysis still shows WHERE and WHEN extra time is needed. Clearly labelled.
            sch = pct([v["act"] for v in vals], 50)
            basis = "typical"
        d = dist([v["act"] for v in vals], sch, pctl)
        d.update({k: sec[k] for k in ("label", "from", "to", "from_name", "to_name", "stops")})
        d["basis"] = basis
        rows.append(d)
        if d.get("gap"):
            total_gap += max(0.0, d["gap"])
        cells = {}
        for v in vals:
            if v["start"] is None:
                continue
            cells.setdefault(band_of(v["start"], band), []).append(v)
        line = {"label": sec["label"], "cells": []}
        for b in sorted(cells):
            vv = cells[b]
            s_ = pct([x["sch"] for x in vv if x["sch"] is not None], 50)
            if s_ is None:
                s_ = sch                                                  # same basis as the section row above
            dd = dist([x["act"] for x in vv], s_, pctl)
            line["cells"].append({"band": b, "label": band_label(b, band), "gap": dd.get("gap"), "n": dd["n"], "p85": dd["p85"], "sched": dd.get("sched"),
                                  "small": dd["n"] < min_n})
        heat.append(line)
    for r in rows:
        if r.get("gap") and total_gap > 0:
            r["share"] = round(100.0 * max(0.0, r["gap"]) / total_gap, 1)
    return rows, heat


# --------------------------------------------------------------------------- 4. contributors (associations only)
def contributors(trips, vals=None, pctl=85):
    """Compare the slowest trips (above the planning percentile) with the rest on whatever condition columns exist."""
    items = vals if vals is not None else [{"trip": t, "act": t["art"]} for t in trips]
    if len(items) < 20:
        return {"ok": False, "reason": "Too few trips to compare conditions (20+ needed)."}
    cut = pct([i["act"] for i in items], pctl)
    slow = [i for i in items if i["act"] >= cut]
    rest = [i for i in items if i["act"] < cut]
    out = []
    for f in COND_FIELDS:
        a = [i["trip"]["cond"][f] for i in slow if f in i["trip"]["cond"]]
        b = [i["trip"]["cond"][f] for i in rest if f in i["trip"]["cond"]]
        if len(a) < 5 or len(b) < 5:
            continue
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        label = {"traffic_speed": "Traffic speed on the route (km/h)", "dwell": "Passenger dwell time", "roadworks": "Road works on the route",
                 "incident": "Traffic incident on the route", "weather": "Rain (≥ 0.2 mm during the trip)", "demand": "Passenger demand (tap-ins on the route, that hour)", "event": "School or public holiday"}[f]
        if f in ("roadworks", "incident", "weather", "event"):
            out.append({"factor": label, "slow": round(100 * ma, 0), "rest": round(100 * mb, 0), "unit": "% of trips", "diff": round(100 * (ma - mb), 0)})
        else:
            diff = ma - mb
            rel = (100.0 * diff / mb) if mb else None
            out.append({"factor": label, "slow": round(ma, 2), "rest": round(mb, 2), "unit": "", "diff": round(diff, 2), "pct": None if rel is None else round(rel, 0)})
    out.sort(key=lambda x: -abs(x.get("pct") or x["diff"] or 0))
    return {"ok": bool(out), "cut": round(cut, 1), "n_slow": len(slow), "n_rest": len(rest), "factors": out,
            "wording": "Associations only - these conditions frequently coincide with the slowest trips; they are not proven causes."}


# --------------------------------------------------------------------------- 5. running time options
def options(vals, sched, pctl=85):
    v = sorted(x for x in vals if x is not None)
    if not v or sched is None:
        return []
    rel = lambda rt: round(100.0 * sum(1 for x in v if x <= rt + 1e-9) / len(v), 1)
    cand = [("Keep current", sched, "No change"), ("Option A", round(pct(v, 50)), "Efficiency focused - matches typical conditions"),
            ("Option B", round(pct(v, 85)), "Higher reliability - covers most days"), ("Option C", round(pct(v, 90)), "More conservative")]
    seen, out = set(), []
    for name, rt, why in cand:
        rt = float(rt)
        if rt in seen:
            continue
        seen.add(rt)
        out.append({"name": name, "rt": rt, "change": round(rt - sched, 1), "reliability": rel(rt), "why": why,
                    "cost": "no extra resources" if rt <= sched else "adds running time to every trip in the period"})
    return out


# --------------------------------------------------------------------------- 6. quantile model (linear, pinball loss) - supporting, not the main engine
def quantile_model(trips, qs=(50, 85, 90), iters=1500, lr=0.25):
    """Small linear quantile regression on time of day, day type and any condition columns. Modelled output, clearly labelled."""
    try:
        import numpy as np
    except Exception:
        return {"ok": False, "reason": "numpy not available"}
    rows, y = [], []
    use_cond = [f for f in ("traffic_speed", "dwell", "demand") if sum(1 for t in trips if f in t["cond"]) > 0.6 * len(trips)]
    flags = [f for f in ("roadworks", "incident", "weather", "event") if sum(1 for t in trips if f in t["cond"]) > 0.6 * len(trips)]
    for t in trips:
        if t["ss"] is None:
            continue
        h = (t["ss"] % 1440) / 1440.0 * 2 * math.pi
        x = [1.0, math.sin(h), math.cos(h), math.sin(2 * h), math.cos(2 * h),
             1.0 if t["daytype"] == "Weekday" else 0.0, 1.0 if t["daytype"] == "Saturday" else 0.0]
        x += [t["cond"].get(f, 0.0) for f in use_cond] + [t["cond"].get(f, 0.0) for f in flags]
        rows.append(x); y.append(t["art"])
    if len(rows) < 60:
        return {"ok": False, "reason": f"Only {len(rows)} trips with a start time - at least 60 are needed for the model."}
    X = np.array(rows, float); Y = np.array(y, float)
    mu, sd = X[:, 1:].mean(0), X[:, 1:].std(0) + 1e-9
    Xs = np.hstack([X[:, :1], (X[:, 1:] - mu) / sd])
    out = {}
    for q in qs:
        tau = q / 100.0
        w = np.zeros(Xs.shape[1]); w[0] = float(np.median(Y))
        for i in range(iters):
            r = Y - Xs @ w
            g = -Xs.T @ np.where(r >= 0, tau, tau - 1.0) / len(Y)
            w -= lr * g * (1.0 / (1.0 + 3.0 * i / iters))
        pred = Xs @ w
        out[f"p{q}"] = {"pinball": round(float(np.mean(np.maximum(tau * (Y - pred), (tau - 1) * (Y - pred)))), 3),
                        "coverage": round(float(np.mean(Y <= pred)) * 100, 1)}
        out.setdefault("_w", {})[f"p{q}"] = w.tolist()
    return {"ok": True, "n": len(rows), "features": ["time of day (harmonics)", "day type"] + use_cond + flags, "quality": {k: v for k, v in out.items() if not k.startswith("_")},
            "note": "MODELLED output from a linear quantile regression (pinball loss). Empirical percentiles above are measured; this model is a supporting check only."}


# --------------------------------------------------------------------------- 7. scenario simulation (historical bootstrap)
def scenarios(trips, pctl=85, draws=400, seed=7):
    import random
    rnd = random.Random(seed)
    def band_trips(f):
        return [t for t in trips if f(t)]
    defs = [("Normal weekday", lambda t: t["daytype"] == "Weekday"),
            ("AM peak (07:00-09:00)", lambda t: t["ss"] is not None and 420 <= t["ss"] % 1440 < 540),
            ("PM peak (17:00-19:30)", lambda t: t["ss"] is not None and 1020 <= t["ss"] % 1440 < 1170),
            ("Rain", lambda t: t["cond"].get("weather", 0) > 0.5),
            ("Road works", lambda t: t["cond"].get("roadworks", 0) > 0.5),
            ("Heavy traffic (slowest quartile of traffic speed)", None),
            ("High passenger demand (top quartile)", None)]
    out = []
    sp = [t["cond"]["traffic_speed"] for t in trips if "traffic_speed" in t["cond"]]
    dm = [t["cond"]["demand"] for t in trips if "demand" in t["cond"]]
    sp_cut, dm_cut = (pct(sp, 25) if sp else None), (pct(dm, 75) if dm else None)
    for name, f in defs:
        if f is None:
            if name.startswith("Heavy traffic"):
                g = [t for t in trips if sp_cut is not None and t["cond"].get("traffic_speed", 1e9) <= sp_cut]
            else:
                g = [t for t in trips if dm_cut is not None and t["cond"].get("demand", -1) >= dm_cut]
        else:
            g = band_trips(f)
        if len(g) < 10:
            out.append({"name": name, "n": len(g), "available": False})
            continue
        v = [t["art"] for t in g]
        boot = []
        for _ in range(draws):
            s = [v[rnd.randrange(len(v))] for _ in range(len(v))]
            boot.append(pct(s, pctl))
        out.append({"name": name, "n": len(g), "available": True, "p50": round(pct(v, 50), 1), "planning": round(pct(v, pctl), 1),
                    "ci": [round(pct(boot, 5), 1), round(pct(boot, 95), 1)], "mean": round(sum(v) / len(v), 1)})
    return out


# --------------------------------------------------------------------------- 8. demo data (clearly labelled as modelled)
def demo(days=40, seed=11):
    """A modelled dataset so the page can be seen working: two services, both directions, stop-level times with realistic peaks."""
    import random, datetime
    rnd = random.Random(seed)
    rows = ["Service,Direction,OperatingDate,TripId,StopSequence,BusStopCode,BusStopName,ScheduledDeparture,ActualDeparture,ScheduledArrival,ActualArrival,TrafficSpeed,Dwell,Roadworks,Weather,PassengerDemand"]
    plan = {("54", 1): 88, ("54", 2): 91, ("170", 1): 70, ("170", 2): 72}
    names = ["Interchange", "Blk 101", "Stn Exit A", "Mall", "Junction", "School", "Blk 220", "Flyover", "Stn Exit C", "Depot", "Terminal"]
    base = datetime.date(2026, 8, 3)
    for dnum in range(days):
        day = base + datetime.timedelta(days=dnum)
        dow = day.weekday()
        for (svc, d), srt in plan.items():
            nst = len(names)
            for dep in range(5 * 60 + 30, 23 * 60, 20):
                h = dep / 60.0
                peak = 1.0 + (0.05 if 7 <= h < 9.5 else 0.06 if 17 <= h < 19.5 else 0.015 if 11 <= h < 14 else 0.0)
                if dow >= 5:
                    peak = 1.0 + (peak - 1.0) * 0.35
                sec_peak = 1.0 + (peak - 1.0) * 3.0                      # the pressure sits in one section of the route
                rain = rnd.random() < (0.18 if 15 <= h < 19 else 0.1)
                works = (svc == "54" and d == 2 and 10 <= dnum < 26)
                speed = round(max(12.0, 34 * (2 - peak) * (0.9 if rain else 1.0) * (0.92 if works else 1.0) + rnd.gauss(0, 2)), 1)
                demand = round(max(5.0, 40 * (peak - 0.6) + rnd.gauss(0, 6)), 0)
                t_sch, t_act = float(dep), float(dep) + max(0.0, rnd.gauss(0.4, 1.1))
                trip = f"{svc}{d}{dnum:02d}{dep}"
                for i, nm in enumerate(names, 1):
                    frac = (i - 1) / (nst - 1)
                    seg_sch = srt / (nst - 1)
                    f_ = sec_peak if 0.25 <= frac < 0.55 else peak       # stops 4-6 carry most of the extra time
                    seg_act = seg_sch * f_ * (1.06 if rain else 1.0) * (1.05 if works and 0.25 <= frac < 0.55 else 1.0) + rnd.gauss(0, .35)
                    dwell = round(max(0.1, 0.25 + demand / 120 + rnd.gauss(0, .12)), 2)
                    if i > 1:
                        t_sch += seg_sch; t_act += max(0.2, seg_act)
                    hm = lambda x: f"{int(x // 60) % 24:02d}:{int(x % 60):02d}:{int((x * 60) % 60):02d}"
                    rows.append(",".join([svc, str(d), day.isoformat(), trip, str(i), f"{40000 + i * 7 + (0 if d == 1 else 3)}", nm,
                                          hm(t_sch), hm(t_act), hm(t_sch), hm(t_act), str(speed), str(dwell), "Y" if works else "N", "Y" if rain else "N", str(int(demand))]))
    return "\n".join(rows)


# --------------------------------------------------------------------------- reference running time when no timetable exists
OFFPEAK = ((0, 7 * 60), (9 * 60 + 30, 17 * 60), (19 * 60 + 30, 24 * 60))


def baseline_of(trips, daytype_split=True):
    """The service's own quiet-period running time (P50 outside the peaks) per service + direction (+ day type).
    Used as the reference when no timetable running time has been entered: the question becomes
    'how much longer than a quiet trip does this period need', which is answerable from measured data alone."""
    groups = {}
    for t in trips:
        k = (t["svc"], t["dir"], t["daytype"] if daytype_split else "All")
        groups.setdefault(k, {"off": [], "all": []})
        groups[k]["all"].append(t["art"])
        if t["ss"] is not None and any(a <= (t["ss"] % 1440) < b for a, b in OFFPEAK):
            groups[k]["off"].append(t["art"])
    out = {}
    for k, v in groups.items():
        base = pct(v["off"], 50) if len(v["off"]) >= 8 else pct(v["all"], 50)
        if base is not None:
            out[k] = round(base, 1)
    return out


def apply_reference(trips, mode="auto", daytype_split=True):
    """mode: timetable | baseline | auto (timetable where entered, otherwise the measured baseline).
    Returns (trips, basis, baselines) - the reference is written into each trip's scheduled running time."""
    if mode == "timetable":
        return trips, "timetable", {}
    base = baseline_of(trips, daytype_split)
    used_base = False
    for t in trips:
        if mode == "baseline" or t["srt"] is None:
            b = base.get((t["svc"], t["dir"], t["daytype"] if daytype_split else "All"))
            if b is not None:
                t["srt"] = b
                t["se"] = None if t["ss"] is None else t["ss"] + b
                used_base = True
    basis = "baseline" if mode == "baseline" else ("mixed" if used_base and any(t["srt"] is not None for t in trips) else "timetable")
    return trips, basis, base
