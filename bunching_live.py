"""Live Bunching & Long Headway view: one list of active cases per service-direction (pure, no I/O, unit-testable).

Input is what the collector already holds:
  rows        BB["rows"] values  - bunching.evaluate() output per service-direction (+ route_stops, at, key)
  open_events BB["open"] values  - the server's continuous case tracker (start time, acknowledgement, level history)
Same rules as the Bunching & Gap engine and the Forecast Centre (bunching.PARAMS / Settings):
  2BB / 3BB / 4BB+ = groups bunched NOW (confirmed after 15 stops, or still developing); long gap = headway long NOW.
Predicted ("early") groups and gaps that are only forecast are not active cases.
Nothing is invented: a value the feed does not give is None.
"""

SEV = {"4BB+": 0, "3BB": 0, "2BB": 1, "GAP": 2}          # red, amber, blue
NEW_SEC = 120                                            # "new" for the first 2 minutes
ESCALATED_SEC = 600                                      # "escalating" for 10 minutes after the group grew


def _stop_at(row, j):
    stops = row.get("route_stops") or []
    if isinstance(j, int) and 0 <= j < len(stops):
        s = stops[j]
        return {"code": s.get("code"), "name": s.get("name"), "road": s.get("road"), "seq": s.get("seq"),
                "lat": s.get("lat"), "lon": s.get("lon")}
    return None


def _bus_points(row, ids):
    pts = []
    for b in row.get("seq") or []:
        if b.get("id") in ids and b.get("lat") is not None and b.get("lon") is not None:
            pts.append({"id": b["id"], "lat": b["lat"], "lon": b["lon"], "status": b.get("status")})
    return pts


def _status(e, now, developing, acked):
    if acked:
        return "acknowledged"
    if e is None:
        return "developing" if developing else "continuing"
    if now - e.get("start", now) < NEW_SEC:
        return "new"
    if e.get("escalated_at") and now - e["escalated_at"] < ESCALATED_SEC:
        return "escalating"
    return "developing" if developing else "continuing"


def _match_bb(events, key, ids, used):
    for e in events:
        if e.get("kind") == "bb" and e.get("key") == key and e["id"] not in used and (set(e.get("cur_ids") or e.get("ids") or ()) & ids):
            return e
    return None


def _match_gap(events, key, lead, foll, used):
    for e in events:
        if e.get("kind") == "gap" and e.get("key") == key and e["id"] not in used and (e.get("lead") == lead or e.get("foll") == foll):
            return e
    return None


def _acked(e):
    return bool(e and e.get("acked_ts") and e.get("acked_n", 0) >= len(e.get("alerts") or []))


def service_cases(row, events, now):
    """Active cases of one service-direction, most severe first."""
    key, out, used = row.get("key"), [], set()
    sched = row.get("sched_hw")
    for g in row.get("groups") or []:
        if g.get("status") not in ("confirmed", "developing"):
            continue
        ids = set(g.get("ids") or []) | set(g.get("joining") or [])
        e = _match_bb(events, key, ids, used)
        if e:
            used.add(e["id"])
        size = len(g.get("ids") or []) + len(g.get("joining") or []) or g.get("size") or 2
        kind = "4BB+" if size >= 4 else "3BB" if size == 3 else "2BB"
        stop = _stop_at(row, g.get("cur_j"))
        buses = _bus_points(row, ids)
        acked = _acked(e)
        out.append({
            "id": e["id"] if e else None, "kind": kind, "type": "bb", "size": size, "confirmed": g.get("status") == "confirmed",
            "status": _status(e, now, g.get("status") == "developing", acked), "acked": acked, "acked_ts": (e or {}).get("acked_ts"),
            "acked_by": (e or {}).get("acked_by"), "stops": g.get("stops"), "actual_hw": g.get("hw_now"), "min_hw": g.get("min_hw"),
            "sched_hw": sched, "headways": g.get("headways") or [], "buses": sorted(ids), "bus_points": buses,
            "location": (stop or {}).get("name") or g.get("location"), "stop": stop, "since": g.get("since_stop"),
            "start": (e or {}).get("start"), "duration_min": round((now - e["start"]) / 60, 1) if e and e.get("start") else None,
            "updated": row.get("at"), "max_level": (e or {}).get("max_level"),
            "point": [stop["lon"], stop["lat"]] if stop and stop.get("lat") is not None else
                     ([sum(b["lon"] for b in buses) / len(buses), sum(b["lat"] for b in buses) / len(buses)] if buses else None)})
    for gp in row.get("gaps") or []:
        if not gp.get("active"):
            continue
        e = _match_gap(events, key, gp.get("lead"), gp.get("foll"), used)
        if e:
            used.add(e["id"])
        ids = {gp.get("lead"), gp.get("foll")} - {None}
        stop = _stop_at(row, gp.get("cur_j"))
        buses = _bus_points(row, ids)
        acked = _acked(e)
        out.append({
            "id": e["id"] if e else None, "kind": "GAP", "type": "gap", "size": 2, "confirmed": gp.get("state") == "confirmed",
            "status": _status(e, now, gp.get("state") != "confirmed", acked), "acked": acked, "acked_ts": (e or {}).get("acked_ts"),
            "acked_by": (e or {}).get("acked_by"), "stops": gp.get("stops"), "actual_hw": gp.get("now"), "max_hw": gp.get("max_hw"),
            "sched_hw": sched, "buses": [gp.get("lead"), gp.get("foll")], "bus_points": buses,
            "location": (stop or {}).get("name") or gp.get("location"), "stop": stop, "since": gp.get("since_stop"),
            "start": (e or {}).get("start"), "duration_min": round((now - e["start"]) / 60, 1) if e and e.get("start") else None,
            "updated": row.get("at"),
            "point": [stop["lon"], stop["lat"]] if stop and stop.get("lat") is not None else
                     ([sum(b["lon"] for b in buses) / len(buses), sum(b["lat"] for b in buses) / len(buses)] if buses else None)})
    out.sort(key=lambda c: (SEV[c["kind"]], c["acked"], -(c["stops"] or 0), -(c["duration_min"] or 0)))
    for i, c in enumerate(out):
        c["label"] = chr(65 + i) if i < 26 else str(i + 1)              # Group A, B, C ... within the service-direction
    return out


def live_view(rows, open_events, now, opmap=None):
    """-> {"services": [one entry per service-direction with active cases], "monitored": [...], "kpi": {...}}."""
    opmap = opmap or {}
    events = list(open_events or [])
    services, monitored = [], []
    for row in rows or []:
        if not row or row.get("error"):
            continue
        svc, d = str(row.get("service")), row.get("direction")
        monitored.append({"service": svc, "direction": d, "risk": row.get("risk"), "buses": len(row.get("seq") or [])})
        cases = service_cases(row, events, now)
        if not cases:
            continue
        top = cases[0]
        services.append({
            "key": row.get("key") or f"{svc}:{d}", "service": svc, "direction": d, "operator": opmap.get(svc.upper(), ""),
            "sched_hw": row.get("sched_hw"), "hw_src": row.get("hw_src"), "cases": cases, "n_cases": len(cases),
            "counts": {k: sum(1 for c in cases if c["kind"] == k) for k in ("2BB", "3BB", "4BB+", "GAP")},
            "issue": top["kind"], "severity": SEV[top["kind"]], "max_stops": max((c["stops"] or 0) for c in cases),
            "actual_hw": top["actual_hw"], "location": top["location"], "point": top["point"],
            "duration_min": max((c["duration_min"] or 0) for c in cases) or None, "updated": row.get("at"),
            "new": any(c["status"] == "new" for c in cases), "all_acked": all(c["acked"] for c in cases)})
    services.sort(key=lambda s: (s["severity"], s["all_acked"], -s["max_stops"], -(s["duration_min"] or 0),
                                 (not s["service"].isdigit()), int(s["service"]) if s["service"].isdigit() else 0, s["direction"]))
    return {"services": services, "monitored": monitored, "kpi": kpis(services, monitored)}


def kpis(services, monitored):
    """All four cards from the same active-case list as the table."""
    def n(kinds):
        return sum(s["counts"].get(k, 0) for s in services for k in kinds)

    def svc(kinds):
        return len({s["service"] for s in services if any(s["counts"].get(k, 0) for k in kinds)})
    return {"affected": len({s["service"] for s in services}), "monitored": len({m["service"] for m in monitored}),
            "bb3": n(("3BB",)), "bb4": n(("4BB+",)), "bb3_services": svc(("3BB", "4BB+")),
            "bb2": n(("2BB",)), "bb2_services": svc(("2BB",)), "gap": n(("GAP",)), "gap_services": svc(("GAP",)),
            "cases": sum(s["n_cases"] for s in services)}


def kpi_tuple(row):
    """Per service-direction counts stored every collector cycle, for the 'vs 1 hour ago' change."""
    c = service_cases(row, [], 0) if row and not row.get("error") else []
    return (sum(x["kind"] == "2BB" for x in c), sum(x["kind"] == "3BB" for x in c), sum(x["kind"] == "4BB+" for x in c),
            sum(x["kind"] == "GAP" for x in c), 1 if c else 0)


def kpi_from_hist(per2):
    """per2: {key: kpi_tuple} -> the same KPI names as kpis()."""
    t = [0, 0, 0, 0]
    svcs = set()
    by = {"2BB": set(), "3BB": set(), "GAP": set()}
    for key, v in (per2 or {}).items():
        for i in range(4):
            t[i] += v[i]
        s = key.split(":")[0]
        if v[4]:
            svcs.add(s)
        if v[0]:
            by["2BB"].add(s)
        if v[1] or v[2]:
            by["3BB"].add(s)
        if v[3]:
            by["GAP"].add(s)
    return {"affected": len(svcs), "bb2": t[0], "bb3": t[1], "bb4": t[2], "gap": t[3],
            "bb2_services": len(by["2BB"]), "bb3_services": len(by["3BB"]), "gap_services": len(by["GAP"])}
