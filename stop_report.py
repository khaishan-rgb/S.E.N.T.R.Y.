"""Stop-by-stop MEASURED running time report (HORIZON V17.3). Pure computation + Excel/CSV builders, no I/O.

Source: rt_trip rows recorded by the live collector (app.py rt_track / rt_store). For every completed trip the collector stores
`marks`: seconds after the trip start at which the bus passed each stop of the route (interpolated between live position polls,
LTA DataMall). This module turns those into:

  * segments    one record per trip x consecutive stop pair (stop i -> stop i+1): seconds, departure time, period
  * summary     per stop pair: n, median, mean, P15, P85, min, max (minutes), average speed when the LTA distance is known,
                and the median per time period; plus cumulative time from the first stop, per period
  * excel()     Summary · Stop-to-stop · Cumulative · Trips · Segment data
  * csv_segments()  the segment records, one row each, for pivot tables / Power BI

Time periods are taken from the TRIP START time by default (the Time Period Report convention), or from the time the bus left
the "from" stop (basis="stop"), which is better for finding when a particular stretch is slow.
Accuracy: a passing time is interpolated between polls (every refresh_sec, typically 30-60 s), so a single short segment
(< ~400 m) can be off by tens of seconds; medians over many trips are reliable. Cells with fewer than min_n trips are flagged.
"""
import csv
import io
import json
import math
import statistics
from datetime import datetime, timedelta, timezone

VERSION = "stop-rt-1.0"
SGT = timezone(timedelta(hours=8))
MAX_SEG_SEC = 3600            # a single stop-to-stop time above an hour is a tracking error, not running time
MAX_KMH = 90.0                # faster than this between two stops is a position glitch
EXCEL_SEGMENT_ROWS = 25000    # above this the Segment data sheet points to the CSV (same columns)


def _q(vals, p):
    """percentile with linear interpolation (p in 0-100)"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _r(x, n=2):
    return None if x is None else round(x, n)


def _hhmm(minute):
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def period_label(start_min, period):
    return f"{_hhmm(start_min)}-{_hhmm(start_min + period)}"


def local(ts):
    return datetime.fromtimestamp(ts, SGT)


def clean_trips(rows, stops, holidays=()):
    """rt_trip rows -> trips with marks aligned to the stop list. Public holidays count as Sunday/PH."""
    out = []
    hol = set(holidays)
    for r in rows:
        try:
            marks = r["marks"] if isinstance(r["marks"], list) else json.loads(r["marks"] or "[]")
        except (TypeError, ValueError):
            continue
        if not marks or not stops:
            continue
        recorded = len(marks)
        n = min(len(marks), len(stops))
        marks = [None if m is None else float(m) for m in marks[:n]]
        if sum(m is not None for m in marks) < 2:
            continue
        day_type = "Sunday/PH" if r["date"] in hol else r["day_type"]
        out.append({"id": r["id"], "date": r["date"], "day_type": day_type, "start_ts": float(r["start_ts"]),
                    "rt_min": r.get("rt_min"), "cover": r.get("cover"), "marks": marks, "route_changed": recorded != len(stops)})
    return out


def segment_records(trips, stops, period=60, basis="trip"):
    """one record per trip and consecutive stop pair with both passing times known"""
    recs = []
    for t in trips:
        m = t["marks"]
        t0 = local(t["start_ts"])
        trip_min = t0.hour * 60 + t0.minute + t0.second / 60.0
        for i in range(len(m) - 1):
            a, b = m[i], m[i + 1]
            if a is None or b is None:
                continue
            sec = b - a
            if sec <= 0 or sec > MAX_SEG_SEC:
                continue
            km = None
            if stops[i].get("dist") is not None and stops[i + 1].get("dist") is not None:
                km = float(stops[i + 1]["dist"]) - float(stops[i]["dist"])
                if km > 0 and km / (sec / 3600.0) > MAX_KMH:
                    continue
            dep = t0 + timedelta(seconds=a)
            ref_min = trip_min if basis == "trip" else dep.hour * 60 + dep.minute + dep.second / 60.0
            p0 = int(ref_min // period) * period
            recs.append({"trip": t["id"], "date": t["date"], "day_type": t["day_type"], "trip_start": t0.strftime("%H:%M:%S"),
                         "depart": dep.strftime("%H:%M:%S"), "period": period_label(p0, period), "period_start": p0,
                         "i": i, "seq_from": stops[i].get("seq"), "from_code": stops[i].get("code"), "from_name": stops[i].get("name"),
                         "seq_to": stops[i + 1].get("seq"), "to_code": stops[i + 1].get("code"), "to_name": stops[i + 1].get("name"),
                         "km": _r(km, 3) if km is not None and km > 0 else None, "sec": round(sec, 1)})
    return recs


def _dist(mins):
    return {"n": len(mins), "median": _r(statistics.median(mins)) if mins else None, "mean": _r(statistics.fmean(mins)) if mins else None,
            "p15": _r(_q(mins, 15)), "p85": _r(_q(mins, 85)), "min": _r(min(mins)) if mins else None, "max": _r(max(mins)) if mins else None}


def summarise(trips, stops, period=60, basis="trip", min_n=3):
    recs = segment_records(trips, stops, period, basis)
    periods = sorted({r["period_start"] for r in recs})
    labels = [period_label(p, period) for p in periods]
    by_seg = {}
    for r in recs:
        by_seg.setdefault(r["i"], []).append(r)
    segments = []
    for i in range(max(0, len(stops) - 1)):
        rs = by_seg.get(i, [])
        mins = [r["sec"] / 60.0 for r in rs]
        d = _dist(mins)
        km = None
        if stops[i].get("dist") is not None and stops[i + 1].get("dist") is not None:
            km = round(float(stops[i + 1]["dist"]) - float(stops[i]["dist"]), 3)
        cells = {}
        for p in periods:
            pm = [r["sec"] / 60.0 for r in rs if r["period_start"] == p]
            if pm:
                cells[period_label(p, period)] = {"median": _r(statistics.median(pm)), "p85": _r(_q(pm, 85)), "n": len(pm), "low_n": len(pm) < min_n}
        segments.append({"i": i, "from": {"seq": stops[i].get("seq"), "code": stops[i].get("code"), "name": stops[i].get("name"), "road": stops[i].get("road")},
                         "to": {"seq": stops[i + 1].get("seq"), "code": stops[i + 1].get("code"), "name": stops[i + 1].get("name"), "road": stops[i + 1].get("road")},
                         "km": km if km and km > 0 else None,
                         "kmh": _r(km / (d["median"] / 60.0), 1) if km and km > 0 and d["median"] else None,
                         **d, "periods": cells, "low_n": d["n"] < min_n})
    # cumulative time from the first stop, per period (only trips that passed the first stop)
    cumulative = []
    for j, s in enumerate(stops):
        cells = {}
        alls = []
        for p in periods:
            vals = []
            for t in trips:
                m = t["marks"]
                if j >= len(m) or m[0] is None or m[j] is None:
                    continue
                t0 = local(t["start_ts"])
                ref = t0.hour * 60 + t0.minute if basis == "trip" else (t0 + timedelta(seconds=m[0])).hour * 60 + (t0 + timedelta(seconds=m[0])).minute
                if int(ref // period) * period == p:
                    vals.append((m[j] - m[0]) / 60.0)
            alls += vals
            if vals:
                cells[period_label(p, period)] = {"median": _r(statistics.median(vals)), "n": len(vals), "low_n": len(vals) < min_n}
        cumulative.append({"seq": s.get("seq"), "code": s.get("code"), "name": s.get("name"), "km": s.get("dist"),
                           "median": _r(statistics.median(alls)) if alls else None, "n": len(alls), "periods": cells})
    totals = [t["rt_min"] for t in trips if t.get("rt_min")]
    dates = sorted({t["date"] for t in trips})
    return {"version": VERSION, "period": period, "basis": basis, "min_n": min_n, "periods": labels, "segments": segments, "cumulative": cumulative,
            "trips": len(trips), "segment_records": len(recs), "dates": [dates[0], dates[-1]] if dates else None, "days": len(dates),
            "route_rt": _dist(totals), "route_changed": sum(1 for t in trips if t.get("route_changed")),
            "day_types": sorted({t["day_type"] for t in trips})}


# --------------------------------------------------------------------------- exports
def csv_segments(recs):
    cols = ["trip", "date", "day_type", "trip_start", "period", "seq_from", "from_code", "from_name", "seq_to", "to_code", "to_name", "depart", "sec", "km"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["trip_id", "date", "day_type", "trip_start", "time_period", "from_seq", "from_stop_code", "from_stop_name", "to_seq", "to_stop_code",
                "to_stop_name", "departed_from_stop", "running_time_sec", "distance_km"])
    for r in recs:
        w.writerow([r[c] if r[c] is not None else "" for c in cols])
    return "﻿" + buf.getvalue()          # BOM: Excel opens UTF-8 stop names correctly


def excel(meta, rep, trips, stops, recs):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.utils import get_column_letter as L

    F = lambda **k: Font(name="Arial", size=k.pop("size", 9), **k)
    thin = Side(style="thin", color="C9D3E0")
    BOX = Border(left=thin, right=thin, top=thin, bottom=thin)
    NAVY = PatternFill("solid", fgColor="0B2A4A")
    CYAN = PatternFill("solid", fgColor="D8F3FF")
    GREY = PatternFill("solid", fgColor="F1F3F6")
    MID = Alignment(horizontal="center", vertical="center", wrap_text=True)
    LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

    def head(ws, row, values, widths=None):
        for c, v in enumerate(values, 1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.font = F(bold=True, color="FFFFFF")
            cell.fill = NAVY
            cell.alignment = MID
            cell.border = BOX
        if widths:
            for c, w in enumerate(widths, 1):
                ws.column_dimensions[L(c)].width = w

    fonts = {}

    def put(ws, row, col, v, fmt=None, bold=False, fill=None, italic=False, align=None):
        cell = ws.cell(row=row, column=col, value=v)
        key = (bold, italic)
        if key not in fonts:                               # one style object per variant (creating one per cell is very slow)
            fonts[key] = F(bold=bold, italic=italic, color="8A94A6" if italic else "1B2430")
        cell.font = fonts[key]
        cell.border = BOX
        cell.alignment = align or (LEFT if isinstance(v, str) else MID)
        if fmt:
            cell.number_format = fmt
        if fill:
            cell.fill = fill
        return cell

    wb = Workbook()
    # ---- Summary
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws["A1"] = "HORIZON · Stop-by-Stop Running Time Report"
    ws["A1"].font = F(bold=True, size=15, color="0B2A4A")
    ws["A2"] = "SBS Transit · Bus Operations Intelligence · MEASURED from live bus positions (LTA DataMall)"
    ws["A2"].font = F(italic=True, color="5A6B80")
    rt = rep["route_rt"]
    rows = [("Service", meta["service"]), ("Direction", meta["direction"]), ("Day type", meta.get("day_type") or "All"),
            ("Dates", f"{rep['dates'][0]} to {rep['dates'][1]}" if rep["dates"] else "—"), ("Days with data", rep["days"]),
            ("Trips used", rep["trips"]), ("Stop-to-stop records", rep["segment_records"]), ("Stops on route", len(stops)),
            ("Time period", f"{rep['period']} min, by {'trip start time' if rep['basis'] == 'trip' else 'time the bus left the stop'}"),
            ("Whole-route running time (min)", f"median {rt['median']} · P85 {rt['p85']} · min {rt['min']} · max {rt['max']} (n={rt['n']})"),
            ("Flagged cells", f"fewer than {rep['min_n']} trips — shown in grey italics"),
            ("Generated", meta["generated"]), ("Engine", VERSION)]
    if rep.get("route_changed"):
        rows.append(("Route changes", f"{rep['route_changed']} trips were recorded on a different stop list; only matching stops are used"))
    for k, (a, b) in enumerate(rows, 4):
        put(ws, k, 1, a, bold=True, fill=CYAN)
        put(ws, k, 2, b)
    notes = ["How it is measured: the collector follows every bus of a measured service along its route; when the trip completes, the time it passed each stop "
             "is interpolated between live position polls. Running time between two stops = passing time at the next stop − passing time at this stop (includes dwell at this stop).",
             "Accuracy: a single short segment can be off by tens of seconds (poll interval); medians over many trips are reliable. Impossible values (> 60 min, > 90 km/h) are dropped.",
             "History starts when a service is added to the measuring list (Running Time / Stop-by-Stop page); DataMall publishes no historical running times."]
    for k, t in enumerate(notes, len(rows) + 6):
        ws.cell(row=k, column=1, value=t).font = F(italic=True, color="5A6B80")
        ws.merge_cells(start_row=k, start_column=1, end_row=k, end_column=8)
        ws.cell(row=k, column=1).alignment = LEFT
        ws.row_dimensions[k].height = 30
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 70

    # ---- Stop-to-stop (median per period)
    ws = wb.create_sheet("Stop-to-stop")
    ws.sheet_view.showGridLines = False
    fixed = ["#", "From seq", "From stop", "To seq", "To stop", "km", "Trips", "Median (min)", "P85 (min)", "Mean (min)", "Min", "Max", "Median km/h"]
    head(ws, 1, fixed + rep["periods"], [5, 7, 34, 7, 34, 7, 7, 10, 10, 10, 8, 8, 10] + [10] * len(rep["periods"]))
    ws.row_dimensions[1].height = 32
    for r_i, s in enumerate(rep["segments"], 2):
        vals = [s["i"] + 1, s["from"]["seq"], f"{s['from']['code'] or ''} {s['from']['name'] or ''}".strip(), s["to"]["seq"],
                f"{s['to']['code'] or ''} {s['to']['name'] or ''}".strip(), s["km"], s["n"], s["median"], s["p85"], s["mean"], s["min"], s["max"], s["kmh"]]
        for c, v in enumerate(vals, 1):
            put(ws, r_i, c, v, fmt="0.00" if c in (8, 9, 10, 11, 12) else ("0.000" if c == 6 else None), italic=s["low_n"] and c >= 7)
        for c, lab in enumerate(rep["periods"], len(fixed) + 1):
            cell = s["periods"].get(lab)
            put(ws, r_i, c, cell["median"] if cell else None, fmt="0.00", italic=bool(cell and cell["low_n"]), fill=GREY if cell and cell["low_n"] else None)
    last = len(rep["segments"]) + 1
    if rep["periods"] and last > 1:
        rng = f"{L(len(fixed) + 1)}2:{L(len(fixed) + len(rep['periods']))}{last}"
        ws.conditional_formatting.add(rng, ColorScaleRule(start_type="percentile", start_value=5, start_color="E7F7EE", mid_type="percentile", mid_value=50,
                                                          mid_color="FFF4CC", end_type="percentile", end_value=95, end_color="F8C9C4"))
    tot = last + 1
    put(ws, tot, 3, "Sum of stop-to-stop medians", bold=True, fill=CYAN)
    put(ws, tot, 8, f"=SUM(H2:H{last})", fmt="0.00", bold=True, fill=CYAN)
    for c in range(len(fixed) + 1, len(fixed) + len(rep["periods"]) + 1):
        put(ws, tot, c, f"=SUM({L(c)}2:{L(c)}{last})", fmt="0.00", bold=True, fill=CYAN)
    ws.freeze_panes = "F2"

    # ---- Cumulative
    ws = wb.create_sheet("Cumulative")
    ws.sheet_view.showGridLines = False
    head(ws, 1, ["Seq", "Stop code", "Stop name", "km from start", "Trips", "Median from first stop (min)"] + rep["periods"],
         [6, 10, 34, 11, 7, 14] + [10] * len(rep["periods"]))
    ws.row_dimensions[1].height = 32
    for r_i, s in enumerate(rep["cumulative"], 2):
        for c, v in enumerate([s["seq"], s["code"], s["name"], s["km"], s["n"], s["median"]], 1):
            put(ws, r_i, c, v, fmt="0.00" if c in (4, 6) else None)
        for c, lab in enumerate(rep["periods"], 7):
            cell = s["periods"].get(lab)
            put(ws, r_i, c, cell["median"] if cell else None, fmt="0.00", italic=bool(cell and cell["low_n"]), fill=GREY if cell and cell["low_n"] else None)
    ws.freeze_panes = "D2"

    # ---- Trips: passing time at every stop
    ws = wb.create_sheet("Trips")
    ws.sheet_view.showGridLines = False
    stop_cols = [f"{s.get('seq')}. {s.get('code') or ''} {s.get('name') or ''}".strip() for s in stops]
    head(ws, 1, ["Trip id", "Date", "Day type", "Trip start", "Route time (min)", "Coverage"] + stop_cols, [9, 11, 11, 10, 11, 9] + [13] * len(stops))
    ws.row_dimensions[1].height = 60
    for r_i, t in enumerate(trips, 2):
        t0 = local(t["start_ts"])
        for c, v in enumerate([t["id"], t["date"], t["day_type"], t0.strftime("%H:%M:%S"), t.get("rt_min"), t.get("cover")], 1):
            put(ws, r_i, c, v, fmt="0.0" if c == 5 else ("0%" if c == 6 else None))
        for j, m in enumerate(t["marks"]):
            ws.cell(row=r_i, column=7 + j, value=None if m is None else (t0 + timedelta(seconds=m)).strftime("%H:%M:%S"))
    ws.freeze_panes = "E2"

    # ---- Segment data (one row per trip x stop pair)
    ws = wb.create_sheet("Segment data")
    cols = ["trip", "date", "day_type", "trip_start", "period", "seq_from", "from_code", "from_name", "seq_to", "to_code", "to_name", "depart", "sec", "km"]
    head(ws, 1, ["Trip id", "Date", "Day type", "Trip start", "Time period", "From seq", "From code", "From stop", "To seq", "To code", "To stop",
                 "Left from stop", "Running time (s)", "km"], [9, 11, 11, 10, 13, 8, 9, 30, 8, 9, 30, 12, 13, 8])
    if len(recs) <= EXCEL_SEGMENT_ROWS:
        for r_i, r in enumerate(recs, 2):
            for c, k in enumerate(cols, 1):
                ws.cell(row=r_i, column=c, value=r[k])
        ws.auto_filter.ref = f"A1:{L(len(cols))}{max(2, len(recs) + 1)}"
    else:                     # very large: Excel would take minutes to build; the CSV has every record with the same columns
        ws.cell(row=2, column=1, value=f"{len(recs):,} stop-to-stop records — too many for this sheet. Use 'Download CSV' on the Stop-by-Stop page "
                                       "(same columns, every record).").font = F(bold=True, color="B23A2E")
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- demo (clearly labelled; for previewing the report before live history exists)
def demo(days=7, seed=14, service="14"):
    """Simulated trips on the real stop list of a demo service (forecast2026_demo_routes.json): peaks slower, 10-12 min headway."""
    import random
    from pathlib import Path
    geo = json.loads((Path(__file__).resolve().parent / "forecast2026_demo_routes.json").read_text())
    g = next(x for x in geo["services"] if x["svc"] == service)
    stops = [{"seq": s["sequence"], "code": s["code"], "name": s["name"], "road": s["road"], "dist": round(s["route_m"] / 1000.0, 3)} for s in g["stops"]]
    rnd = random.Random(seed)
    start = datetime(2026, 10, 20, tzinfo=SGT)
    rows, rid = [], 1
    slow = {rnd.randrange(5, len(stops) - 5) for _ in range(4)}            # a few stretches that suffer in the peaks
    for d in range(days):
        day = start + timedelta(days=d)
        weekday = day.weekday() < 5
        t = day.replace(hour=6, minute=rnd.randrange(0, 8))
        while t.hour < 23:
            h = t.hour + t.minute / 60.0
            peak = (1.0 + 0.45 * math.exp(-((h - 8.2) / 1.1) ** 2) + 0.38 * math.exp(-((h - 18.3) / 1.3) ** 2)) if weekday else 1.0 + 0.15 * math.exp(-((h - 14) / 3) ** 2)
            marks, cur = [0.0], 0.0
            for i in range(len(stops) - 1):
                km = max(0.15, stops[i + 1]["dist"] - stops[i]["dist"])
                v = 24.0 / peak * (0.72 if i in slow and peak > 1.2 else 1.0) * rnd.uniform(0.85, 1.15)
                cur += km / v * 3600 + rnd.uniform(12, 35)                   # driving + dwell at stop i
                marks.append(round(cur))
            rows.append({"id": rid, "date": day.strftime("%Y-%m-%d"), "day_type": "Weekday" if weekday else ("Saturday" if day.weekday() == 5 else "Sunday/PH"),
                         "start_ts": t.timestamp(), "rt_min": round(cur / 60.0, 2), "cover": 1.0, "marks": json.dumps(marks)})
            rid += 1
            t += timedelta(minutes=rnd.randrange(9, 13))
    return rows, stops
