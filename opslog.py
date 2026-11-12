"""HORIZON operations event log and wet-weather impact (12 Nov 2026).

Two jobs, both pure functions so they can be tested without the server:

1. The event log: one row per bunching event, long-headway gap, traffic alert (congestion, incident, road works) and rain area,
   with when it started and ended, where, which services and directions it touched, and its peak (delay minutes, rain mm,
   headway minutes). app.py fills it from data it already collects and keeps it EVENTLOG_KEEP_DAYS (default 365).

2. Wet-weather impact: how many minutes rain adds to a service's running time. Each measured trip (rt_trip, measured from
   live bus positions) already carries the rain that fell near its route during the trip (rt_cond.rain_mm, NEA gauges).
   A wet trip (>= WET_MM) is compared with dry trips of the same service, direction and day type that started within an
   hour of it; the added minutes are the median of those differences. Nothing is estimated where there are not enough trips:
   the confidence says so instead.
"""
import io
import json
import statistics
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
WET_MM = 0.2          # rain along the route during the trip: at or above this the trip counts as wet (same rule as Running Time Analytics)
HEAVY_MM = 2.0        # heavy rain along the route during the trip
PERIODS = (("am_peak", "Morning peak", 6, 9), ("midday", "Daytime", 9, 16), ("pm_peak", "Evening peak", 16, 20), ("night", "Night", 20, 30))
CATEGORIES = {"bunching": "Bus bunching", "long_headway": "Long headway", "traffic": "Traffic", "wet_weather": "Wet weather"}


# --------------------------------------------------------------------------------------------- helpers
def sgt(ts):
    return datetime.fromtimestamp(ts, SGT)


def period_of(hour):
    h = hour if hour >= 6 else hour + 24
    for key, _name, a, b in PERIODS:
        if a <= h < b:
            return key
    return "night"


def period_name(key):
    return next((n for k, n, _a, _b in PERIODS if k == key), key)


def svc_key(svc, d):
    return f"{str(svc).upper()}:{int(d)}"


def svc_text(keys):
    """',147:1,147:2,' -> stored form, so a service can be found with LIKE '%,147:%'."""
    keys = sorted({k for k in keys if k}, key=_svc_sort)
    return ("," + ",".join(keys) + ",") if keys else ""


def svc_list(text):
    return [k for k in str(text or "").split(",") if k]


def _svc_sort(k):
    s, _, d = str(k).partition(":")
    num = "".join(c for c in s if c.isdigit())
    return (int(num) if num else 9999, s, d)


def _r(x, n=1):
    return None if x is None else round(float(x), n)


# --------------------------------------------------------------------------------------------- wet-weather impact
def wet_impact(trips, wet_mm=WET_MM, heavy_mm=HEAVY_MM, min_wet=3):
    """trips: [{service, direction, day_type, start_ts, rt_min, rain_mm}] (rain_mm None = rain not known yet, left out).
    -> {(svc, dir): stats}. stats: n_wet, n_dry, n_heavy, added_min (median extra minutes when wet), added_heavy_min, pct,
    dry_min (typical dry running time), periods {key: {n, added_min}}, confidence."""
    by = {}
    for t in trips:
        if t.get("rain_mm") is None or not t.get("rt_min"):
            continue
        h = sgt(t["start_ts"]).hour
        by.setdefault((str(t["service"]).upper(), int(t["direction"])), []).append((t["day_type"], h, float(t["rt_min"]), float(t["rain_mm"])))
    out = {}
    for key, rows in by.items():
        dry = [r for r in rows if r[3] < wet_mm]
        wet = [r for r in rows if r[3] >= wet_mm]
        deltas, heavy, per, pct = [], [], {}, []
        for dt, h, rt, mm in wet:
            base = [x[2] for x in dry if x[0] == dt and min(abs(x[1] - h), 24 - abs(x[1] - h)) <= 1]
            if len(base) < 2:
                continue
            b = statistics.median(base)
            d = rt - b
            deltas.append(d)
            pct.append(d / b if b else 0.0)
            if mm >= heavy_mm:
                heavy.append(d)
            per.setdefault(period_of(h), []).append(d)
        n = len(deltas)
        st = {"service": key[0], "dir": key[1], "n_wet": n, "n_dry": len(dry), "n_heavy": len(heavy),
              "added_min": _r(statistics.median(deltas)) if n >= min_wet else None,
              "added_heavy_min": _r(statistics.median(heavy)) if len(heavy) >= min_wet else None,
              "pct": _r(100 * statistics.median(pct), 0) if n >= min_wet else None,
              "dry_min": _r(statistics.median([x[2] for x in dry])) if dry else None,
              "periods": {k: {"n": len(v), "added_min": _r(statistics.median(v)) if len(v) >= min_wet else None} for k, v in per.items()},
              "confidence": "good" if n >= 8 and len(dry) >= 20 else "limited" if n >= min_wet else "insufficient"}
        out[key] = st
    return out


def expected_now(stats, hour, heavy=False):
    """Minutes rain is expected to add to this service now: the current period if it has enough wet trips, else the overall figure."""
    if not stats:
        return None, "no measured trips"
    p = (stats.get("periods") or {}).get(period_of(hour)) or {}
    if heavy and stats.get("added_heavy_min") is not None:
        return stats["added_heavy_min"], f"heavy rain, {stats['n_heavy']} wet trips"
    if p.get("added_min") is not None:
        return p["added_min"], f"{period_name(period_of(hour)).lower()}, {p['n']} wet trips"
    if stats.get("added_min") is not None:
        return stats["added_min"], f"all hours, {stats['n_wet']} wet trips"
    return None, f"only {stats.get('n_wet', 0)} wet trips so far"


def network_summary(stats):
    """Across every measured service-direction with a figure: median added minutes and how many get slower."""
    vals = [s["added_min"] for s in stats.values() if s.get("added_min") is not None]
    return {"measured": len(stats), "with_figure": len(vals), "median_added_min": _r(statistics.median(vals)) if vals else None,
            "slower": sum(1 for v in vals if v >= 1.0), "max_added_min": _r(max(vals)) if vals else None}


# --------------------------------------------------------------------------------------------- event log rows
def traffic_rows(events, now, location_fn, delay_fn, min_min=15):
    """Traffic-engine events (congestion / incident / roadworks / weather) that touch at least one bus route -> log rows.
    Only events that have lasted min_min minutes or more are logged (short, flickering alerts are not), so one that is still going
    appears once it reaches min_min. location_fn(ev) -> text; delay_fn(ev, match) -> estimated delay minutes or None."""
    rows = []
    for e in events:
        if e.get("status") not in ("active", "cleared") or str(e.get("id", "")).startswith("CAND-") or not e.get("match"):
            continue
        end_ = (e.get("cleared_time") if e["status"] == "cleared" else None) or e.get("last_seen") or now
        if end_ - (e.get("first_seen") or now) < min_min * 60:
            continue
        c = e.get("cur") or {}
        keys, delays = [], []
        for k, m in e["match"].items():
            s, _, d = k.partition("|")
            keys.append(svc_key(s, d or 1))
            dm = delay_fn(e, m)
            if dm is not None:
                delays.append(dm)
        kind = e["kind"]
        if kind == "weather":
            cat, peak, unit = "wet_weather", (e.get("peak") or {}).get("max_mm", c.get("max_mm")), "mm"
            detail = {"level": c.get("level"), "max_mm": c.get("max_mm"), "radius_km": c.get("radius_km"), "gauges": c.get("stations")}
            level = c.get("level")
        else:
            cat, unit = "traffic", "min"
            peak = max(delays) if delays else None
            level = None
            if kind == "congestion":
                detail = {"avg_kmh": _r(c.get("avg_kmh")), "ref_kmh": _r(c.get("ref_kmh")), "min_kmh": _r((e.get("peak") or {}).get("min_kmh")),
                          "length_m": round(c.get("length_m") or 0), "source": c.get("source")}
            elif kind == "incident":
                detail = {"type": c.get("type"), "message": c.get("message"), "source": c.get("source")}
            else:
                detail = {"road": c.get("road"), "source": c.get("source")}
        end = e.get("cleared_time") if e["status"] == "cleared" else None
        rows.append({"key": f"TR:{e['id']}:{int(e.get('first_seen') or now)}", "category": cat, "kind": "rain" if kind == "weather" else kind,
                     "start_ts": e.get("first_seen") or now, "end_ts": end, "last_ts": e.get("last_seen") or now, "ongoing": 0 if end else 1,
                     "location": location_fn(e), "services": keys, "peak": _r(peak, 2), "peak_unit": unit, "level": level,
                     "detail": detail, "source": c.get("source") or "lta"})
    return rows


def merge(old, new):
    """Keep the earliest start, the union of services, the highest peak; the newest end / status / text."""
    if not old:
        return new
    out = dict(new)
    out["start_ts"] = min(old["start_ts"], new["start_ts"])
    out["services"] = sorted(set(old["services"]) | set(new["services"]), key=_svc_sort)
    pk = [p for p in (old.get("peak"), new.get("peak")) if p is not None]
    out["peak"] = max(pk) if pk else None
    return out


# --------------------------------------------------------------------------------------------- Excel export
def workbook(events, impact_rows, title_note=""):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    head = ["Category", "Kind", "Start", "End", "Duration (min)", "Ongoing", "Location", "Services", "Peak", "Unit", "Level", "Detail"]
    ws.append(head)
    for e in events:
        st, en = sgt(e["start_ts"]), (sgt(e["end_ts"]) if e.get("end_ts") else None)
        dur = ((e.get("end_ts") or e.get("last_ts") or e["start_ts"]) - e["start_ts"]) / 60.0
        ws.append([CATEGORIES.get(e["category"], e["category"]), e.get("kind"), st.strftime("%Y-%m-%d %H:%M"), en.strftime("%Y-%m-%d %H:%M") if en else "",
                   round(dur), "Yes" if e.get("ongoing") else "", e.get("location") or "", " ".join(e.get("services") or []),
                   e.get("peak"), e.get("peak_unit") or "", e.get("level") or "", json.dumps(e.get("detail") or {}, ensure_ascii=False)[:500]])
    ws2 = wb.create_sheet("Wet weather impact")
    ws2.append(["Service", "Direction", "Added min when wet", "Added min, heavy rain", "Change %", "Typical dry running time (min)",
                "Wet trips", "Dry trips", "Morning peak", "Daytime", "Evening peak", "Night", "Confidence"])
    for s in impact_rows:
        p = s.get("periods") or {}
        ws2.append([s["service"], s["dir"], s.get("added_min"), s.get("added_heavy_min"), s.get("pct"), s.get("dry_min"), s.get("n_wet"), s.get("n_dry"),
                    *[(p.get(k) or {}).get("added_min") for k, _n, _a, _b in PERIODS], s.get("confidence")])
    for w in (ws, ws2):
        for c in w[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="0B2A4A")
            c.alignment = Alignment(vertical="center", wrap_text=True)
        w.freeze_panes = "A2"
        for col in w.columns:
            width = max(len(str(c.value or "")) for c in col[:200])
            w.column_dimensions[col[0].column_letter].width = min(48, max(10, width + 2))
    if title_note:
        ws3 = wb.create_sheet("About")
        for line in title_note.split("\n"):
            ws3.append([line])
        ws3.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------------------------- alert analysis + predictive insights
def _day_type(dt):
    return "weekend" if dt.weekday() >= 5 else "weekday"


def analysis(events, now, days):
    """Stored events over the last `days` -> counts by type, by hour of day (per type), top services and top places."""
    cut = now - days * 86400
    ev = [e for e in events if e["start_ts"] >= cut]
    cats = list(CATEGORIES)
    by_cat = {c: 0 for c in cats}
    by_hour = {c: [0] * 24 for c in cats}
    svc, place = {}, {}
    for e in ev:
        c = e["category"]
        if c not in by_cat:
            continue
        by_cat[c] += 1
        by_hour[c][sgt(e["start_ts"]).hour] += 1
        for k in e.get("services") or []:
            s = svc.setdefault(k, {"key": k, "total": 0, **{x: 0 for x in cats}})
            s["total"] += 1
            s[c] += 1
        loc = (e.get("location") or "").strip()
        if loc:
            p = place.setdefault(loc, {"place": loc, "total": 0, "category": c, **{x: 0 for x in cats}})
            p["total"] += 1
            p[c] += 1
    tot_h = [sum(by_hour[c][h] for c in cats) for h in range(24)]
    peak = max(range(24), key=lambda h: tot_h[h]) if any(tot_h) else None
    top_cat = max(by_cat, key=by_cat.get) if any(by_cat.values()) else None
    days_seen = len({sgt(e["start_ts"]).date() for e in ev})
    return {"days": days, "total": len(ev), "by_category": by_cat, "by_hour": by_hour, "peak_hour": peak, "top_category": top_cat,
            "top_services": sorted(svc.values(), key=lambda x: (-x["total"], _svc_sort(x["key"])))[:10],
            "top_places": sorted(place.values(), key=lambda x: -x["total"])[:8], "days_with_events": days_seen}


def insights(events, now, since=None, wx_rows=None, wx_live=None, lookback=28, window_h=2):
    """Patterns in the stored alerts -> short predictive insights. Every insight says what it is based on; nothing is invented.
    1. likely in the next window_h hours: the same service (bunching / long headway) or place (traffic / wet weather) had an alert in this
       time window on at least 3 days, and on at least 30% of the same-type days (weekday / weekend) recorded;
    2. week-on-week change by type; 3. busiest hour; 4. rain now / services most slowed by rain."""
    t = sgt(now)
    dt = _day_type(t)
    h0 = t.hour
    hours = {(h0 + i) % 24 for i in range(window_h)}
    first = since or min((e["start_ts"] for e in events), default=now)
    cut = max(first, now - lookback * 86400)
    # same-type days recorded in the look-back (today excluded: it is still happening)
    n_days, d = 0, sgt(cut).date()
    while d < t.date():
        if _day_type(datetime(d.year, d.month, d.day, tzinfo=SGT)) == dt:
            n_days += 1
        d += timedelta(days=1)
    out = []
    span_days = (now - first) / 86400
    if span_days < 3 or n_days < 3:
        out.append({"kind": "learning", "level": "info", "title": "Still learning the patterns",
                    "detail": f"{max(0, round(span_days, 1))} days of alerts stored. Predictions appear once at least 3 {dt}s are recorded.",
                    "basis": "stored alert history"})
    else:
        groups = {}
        for e in events:
            if e["start_ts"] < cut or e["start_ts"] >= now - 60:
                continue
            s = sgt(e["start_ts"])
            if s.date() >= t.date() or _day_type(s) != dt or s.hour not in hours:
                continue
            if e["category"] in ("bunching", "long_headway"):
                keys = [(e["category"], k) for k in (e.get("services") or [])]
            else:
                keys = [(e["category"], e.get("location") or "")] if e.get("location") else []
            for key in keys:
                g = groups.setdefault(key, {"dates": set(), "places": {}, "services": {}, "peaks": []})
                g["dates"].add(s.date())
                if e.get("location"):
                    g["places"][e["location"]] = g["places"].get(e["location"], 0) + 1
                for k in e.get("services") or []:
                    g["services"][k] = g["services"].get(k, 0) + 1
                if e.get("peak") is not None:
                    g["peaks"].append(e["peak"])
        cand = []
        for (cat, key), g in groups.items():
            hits = len(g["dates"])
            share = hits / n_days
            if hits >= 3 and share >= 0.3:
                cand.append((share * min(hits, 10), cat, key, hits, share, g))
        win = f"{h0:02d}:00–{(h0 + window_h) % 24:02d}:00"
        for _, cat, key, hits, share, g in sorted(cand, key=lambda x: -x[0])[:5]:
            pk = statistics.median(g["peaks"]) if g["peaks"] else None
            if cat in ("bunching", "long_headway"):
                s_, _, d_ = key.partition(":")
                where = max(g["places"], key=g["places"].get) if g["places"] else ""
                title = f"Service {s_} Dir {d_}: {CATEGORIES[cat].lower()} likely {win}"
                detail = (f"Happened on {hits} of the last {n_days} {dt}s in this window" + (f", mostly {where}" if where else "")
                          + (f"; typical {'closest headway' if cat == 'bunching' else 'longest headway'} {pk:.1f} min" if pk is not None else "") + ".")
                svcs = [key]
            else:
                top = sorted(g["services"], key=lambda k: -g["services"][k])[:6]
                title = f"{'Rain' if cat == 'wet_weather' else 'Traffic'} likely {win}: {key[:70]}"
                detail = (f"On {hits} of the last {n_days} {dt}s in this window"
                          + (f"; usually +{pk:.1f} min delay" if pk is not None and cat == "traffic" else f"; up to {pk:.1f} mm" if pk is not None else "")
                          + (f". Affects {', '.join(x.replace(':', ' D') for x in top)}" if top else "") + ".")
                svcs = top
            out.append({"kind": "likely", "level": "high" if share >= 0.6 else "medium", "category": cat, "title": title, "detail": detail,
                        "likelihood": round(share * 100), "services": svcs, "basis": f"{hits}/{n_days} {dt}s, last {lookback} days"})
        # week on week
        a = [e for e in events if now - 7 * 86400 <= e["start_ts"] < now]
        b = [e for e in events if now - 14 * 86400 <= e["start_ts"] < now - 7 * 86400]
        if span_days >= 14:
            for cat, name in CATEGORIES.items():
                x, y = sum(1 for e in a if e["category"] == cat), sum(1 for e in b if e["category"] == cat)
                if y >= 5 and x >= y * 1.3:
                    out.append({"kind": "trend", "level": "medium", "category": cat, "title": f"{name} up {round(100 * (x - y) / y)}% this week",
                                "detail": f"{x} in the last 7 days against {y} the week before.", "basis": "last 14 days"})
                elif y >= 5 and x <= y * 0.7:
                    out.append({"kind": "trend", "level": "info", "category": cat, "title": f"{name} down {round(100 * (y - x) / y)}% this week",
                                "detail": f"{x} in the last 7 days against {y} the week before.", "basis": "last 14 days"})
        # busiest hour per type
        rec = [e for e in events if e["start_ts"] >= cut]
        for cat, name in CATEGORIES.items():
            hs = [sgt(e["start_ts"]).hour for e in rec if e["category"] == cat]
            if len(hs) >= 10:
                h = max(range(24), key=hs.count)
                out.append({"kind": "peak", "level": "info", "category": cat, "title": f"{name} peaks {h:02d}:00–{(h + 1) % 24:02d}:00",
                            "detail": f"{hs.count(h)} of {len(hs)} ({round(100 * hs.count(h) / len(hs))}%) in the last {lookback} days started in that hour.",
                            "basis": f"last {lookback} days"})
    # rain
    if wx_live and wx_live.get("services"):
        sv = [s for s in wx_live["services"] if s.get("expected_min") is not None][:4]
        areas = ", ".join(a["name"] for a in (wx_live.get("areas") or [])[:3])
        out.insert(0, {"kind": "rain_now", "level": "high", "category": "wet_weather", "title": f"Raining on bus routes now: {areas}",
                       "detail": (f"{len(wx_live['services'])} services in rain. " + ("Usually adds " + ", ".join(f"{s['service']} D{s['dir']} +{s['expected_min']:.1f} min" for s in sv) + "."
                                  if sv else "No measured wet trips yet for these services.")),
                       "services": [f"{s['service']}:{s['dir']}" for s in wx_live["services"][:8]], "basis": "NEA rain gauges + measured trips"})
    elif wx_rows:
        top = [r for r in wx_rows if r.get("added_min") is not None and r["added_min"] >= 1][:3]
        if top:
            out.append({"kind": "rain_impact", "level": "info", "category": "wet_weather", "title": "If it rains: services most slowed",
                        "detail": ", ".join(f"{r['service']} D{r['dir']} +{r['added_min']:.1f} min" for r in top) + " (median of wet vs dry trips).",
                        "services": [f"{r['service']}:{r['dir']}" for r in top], "basis": "measured trips, last 60 days"})
    return out
