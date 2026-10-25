"""S.E.N.T.R.Y. 3.0 Forecast Centre - engine and live-cycle tests (no network: provider responses are samples in each provider's
documented format, served by a fake HTTP client).
Covers: parsing, route geometry intersection, direction matching, parallel-road rejection, nearby-but-unaffected rejection, crossing roads,
road-work time windows, rainfall exposure classification, forecast maths, alert de-duplication / re-alert / acknowledgement / clearance /
escalation, stale data, API failures, demo determinism and separation, budget, no secrets in payloads, no LTA speed bands, and the
planned-timetable store fix."""
import sys, os, json, time, math, asyncio, random, tempfile
sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[1])
TMP = tempfile.mkdtemp()
os.environ["BUNCHING_DB"] = os.path.join(TMP, "t_fc.db")
os.environ["FORECAST_SERVICES"] = "51"
import forecast as F
import app

ok = fail = 0
def check(n, c, extra=""):
    global ok, fail
    if c: ok += 1; print("  ✓", n)
    else: fail += 1; print("  ✗", n, extra)
run = lambda co: asyncio.run(co)

# ---------------------------------------------------------------- geometry used throughout (an expressway corridor and its neighbours)
PIE = [(1.3330, 103.8600), (1.3334, 103.8680), (1.3338, 103.8760), (1.3340, 103.8840), (1.3339, 103.8920), (1.3336, 103.9000), (1.3333, 103.9060)]
off = lambda pts, dlat: [(a + dlat, b) for a, b in pts]
def sub(pts, lon_a, lon_b, dlat=0.0):
    L = F.Line(pts); S, _, _ = L.sample(20.0)
    return [(y / F.KY + dlat, x / F.KX) for x, y in S if lon_a * F.KX <= x <= lon_b * F.KX]
EAST = F.Line(PIE)                                            # eastbound carriageway
WEST = F.Line(off(PIE, 0.00025)[::-1])                        # westbound, 28 m north, reversed
PARA = F.Line(off([p for p in PIE if 103.865 < p[1] < 103.902], -0.00045))   # service road 50 m south, eastbound
CROSS = F.Line([(1.3200, 103.8838), (1.3300, 103.8840), (1.3340, 103.8841), (1.3400, 103.8842), (1.3480, 103.8845)])

print("PARSING (provider sample responses)")
tt_flow = {"flowSegmentData": {"frc": "FRC0", "currentSpeed": 31, "freeFlowSpeed": 78, "currentTravelTime": 98, "freeFlowTravelTime": 39, "confidence": 0.97,
           "roadClosure": False, "coordinates": {"coordinate": [{"latitude": 1.33380, "longitude": 103.8740}, {"latitude": 1.33390, "longitude": 103.8790},
                                                                {"latitude": 1.33400, "longitude": 103.8840}]}, "@version": "traffic-service-flow 1.0.120"}}
f = F.parse_tomtom_flow(tt_flow)
check("TomTom flowSegmentData: speeds, travel times, confidence and the segment's own line (direction)",
      f["cur"] == 31 and f["ff"] == 78 and f["cur_tt"] == 98 and f["conf"] == 0.97 and len(f["line"]) == 3 and not f["closure"])
check("TomTom flow without speeds -> None (never a made-up value)", F.parse_tomtom_flow({"flowSegmentData": {"freeFlowSpeed": 70}}) is None and F.parse_tomtom_flow({}) is None)
tt_inc = {"incidents": [
    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[x[1], x[0]] for x in sub(PIE, 103.870, 103.896, 0.00002)]},
     "properties": {"id": "JAM1", "iconCategory": 6, "magnitudeOfDelay": 2, "events": [{"description": "Stationary traffic", "code": 101, "iconCategory": 6}],
                    "from": "Paya Lebar Rd", "to": "Changi Airport", "roadNumbers": ["PIE"], "length": 2900.0, "delay": 420, "startTime": None, "endTime": None}},
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [103.70, 1.34]},
     "properties": {"id": "ACC9", "iconCategory": 1, "magnitudeOfDelay": 1, "events": [{"description": "Accident", "code": 1, "iconCategory": 1}],
                    "from": "", "to": "", "roadNumbers": [], "length": 0, "delay": None, "startTime": "2026-10-26T00:10:00Z", "endTime": "2026-10-26T03:00:00Z"}},
    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [0, 0.1]]}, "properties": {"id": "X", "iconCategory": 8}}]}
ev = F.parse_tomtom_incidents(tt_inc, now=1.0)
check("TomTom incidentDetails: jam line kept in travel order, kind/magnitude/delay/length; point incident; out-of-Singapore geometry dropped",
      len(ev) == 2 and ev[0]["kind"] == "jam" and ev[0]["geom"] == "line" and ev[0]["delay_s"] == 420 and ev[0]["magnitude"] == "moderate"
      and ev[0]["line"][0][1] < ev[0]["line"][-1][1] and ev[1]["kind"] == "accident" and ev[1]["geom"] == "point" and ev[1]["start"] and ev[1]["end"])
lta = F.parse_lta_incidents([{"Type": "Accident", "Latitude": 1.33392, "Longitude": 103.8940,
                              "Message": "(26/10)08:01 Accident on PIE (towards Changi Airport) after Paya Lebar Rd Exit. Avoid lane 1."},
                             {"Type": "Roadwork", "Latitude": 0, "Longitude": 0, "Message": "bad"}], now=time.mktime((2026, 10, 26, 9, 0, 0, 0, 0, -1)))
check("LTA TrafficIncidents: point only, road and 'towards' read from the message, reported time parsed; bad coordinates dropped",
      len(lta) == 1 and lta[0]["geom"] == "point" and lta[0]["road"] == "PIE" and lta[0]["towards"] == "Changi Airport" and lta[0]["reported"] is not None and lta[0]["start"] is None and not lta[0]["line"])
rw = F.normalise_roadworks([{"key": "RW1", "road": "UBI AVENUE 2", "lat": None, "lon": None, "start_epoch": 100.0, "end_epoch": 200.0, "other": "SP"}])
check("LTA RoadWorks without coordinates: road name only (cannot be matched to a route)", rw[0]["geom"] == "road_name" and rw[0]["pt"] is None)
nea2 = {"code": 0, "data": {"area_metadata": [{"name": "Ang Mo Kio", "label_location": {"latitude": 1.375, "longitude": 103.839}},
                                              {"name": "Bedok", "label_location": {"latitude": 1.321, "longitude": 103.924}}],
                            "items": [{"valid_period": {"start": "2026-10-26T08:00:00+08:00", "end": "2026-10-26T10:00:00+08:00", "text": "8 to 10 AM"},
                                       "forecasts": [{"area": "Ang Mo Kio", "forecast": "Heavy Thundery Showers"}, {"area": "Bedok", "forecast": "Cloudy"}]}]}}
nea1 = {"area_metadata": nea2["data"]["area_metadata"], "items": [{"valid_period": {"start": "2026-10-26T08:00:00+08:00", "end": "2026-10-26T10:00:00+08:00"},
                                                                    "forecasts": [{"area": "Ang Mo Kio", "forecast": "Light Rain"}, {"area": "Bedok", "forecast": "Fair (Day)"}]}]}
a2, a1 = F.parse_nea_2h(nea2), F.parse_nea_2h(nea1)
check("NEA 2-hour forecast (v2 and v1): areas, rain and heavy flags, validity",
      len(a2["areas"]) == 2 and a2["areas"][0]["heavy"] and not a2["areas"][1]["rain"] and a1["areas"][0]["rain"] and not a1["areas"][0]["heavy"] and a2["valid"][0])
here = {"results": [{"location": {"description": "PIE", "shape": {"links": [{"points": [{"lat": 1.3330, "lng": 103.86}, {"lat": 1.3334, "lng": 103.868}]}]}},
                     "currentFlow": {"speed": 10.0, "freeFlow": 20.0, "confidence": 0.9, "traversability": "open"}}]}
h = F.parse_here_flow(here)
check("HERE v7 flow (documented format): m/s converted to km/h, shape kept", len(h) == 1 and abs(h[0]["cur"] - 36.0) < 1e-6 and abs(h[0]["ff"] - 72.0) < 1e-6 and len(h[0]["line"]) == 2)
rain_v2 = {"code": 0, "data": {"stations": [{"id": "S1", "name": "A", "location": {"latitude": 1.3340, "longitude": 103.8800}},
                                            {"id": "S2", "name": "B", "location": {"latitude": 1.36, "longitude": 103.70}}],
                               "readings": [{"timestamp": "2026-10-26T08:05:00+08:00", "data": [{"stationId": "S1", "value": 2.5}, {"stationId": "S2", "value": 0}]}]}}
st = app.parse_rain(rain_v2)
check("NEA rainfall (data.gov.sg v2): every gauge kept, dry ones included (they matter for interpolation)", len(st) == 2 and {s["mm"] for s in st} == {2.5, 0.0})

print("STRICT ROUTE MATCHING")
jam = F.Line(sub(PIE, 103.870, 103.896, 0.00002))
m = F.match_line(EAST, jam)
check("a jam line along the eastbound route, same direction -> CONFIRMED with overlap, chainage and the bearing difference",
      m["status"] == "confirmed" and m["overlap_m"] > 2500 and m["a_m"] < m["b_m"] and m["bearing_diff"] < 5 and "same direction" in m["explain"], m)
m = F.match_line(F.Line(PIE[::-1]), jam)
check("the same road line, opposite direction of travel -> REJECTED (opposite direction)", m["status"] == "rejected" and m["reason"] == "opposite_direction", m)
m = F.match_line(WEST, jam)
check("the westbound carriageway 28 m away -> REJECTED (opposite carriageway)", m["status"] == "rejected" and m["reason"] == "opposite_direction", m)
m = F.match_line(PARA, jam)
check("a parallel service road 50 m away, same direction -> REJECTED (parallel road), never confirmed", m["status"] == "rejected" and m["reason"] == "parallel_road", m)
m = F.match_line(CROSS, jam)
check("a road crossing on a flyover -> REJECTED (crossing), not along the route", m["status"] == "rejected" and m["reason"] == "crossing", m)
far = F.Line(sub(PIE, 103.870, 103.896, -0.0015))
m = F.match_line(EAST, far)
check("a jam 165 m away (nearby, unrelated road) -> REJECTED (nearby), never confirmed", m["status"] == "rejected" and m["reason"] == "nearby", m)
spur = F.Line([(1.3338, 103.8760), (1.3338 - 0.004, 103.8762)])
m = F.match_line(EAST, spur)
check("a side road that only touches the route at a junction -> not confirmed", m["status"] != "confirmed", m)
random.seed(7)
bad = 0
for _ in range(300):                                          # nearby-but-unrelated roads must NEVER confirm
    dl = random.choice([1, -1]) * random.uniform(0.00019, 0.0030)    # 21 m .. 330 m
    a = random.uniform(103.862, 103.890); b = a + random.uniform(0.002, 0.012)
    seg = sub(PIE, a, min(b, 103.905), dl)
    if len(seg) >= 2 and F.match_line(EAST, F.Line(seg))["status"] == "confirmed":
        bad += 1
check("300 random parallel / nearby roads 21-330 m from the route: none confirmed", bad == 0, bad)
bad = 0
for _ in range(200):                                          # same-direction lines on the route itself must confirm
    a = random.uniform(103.862, 103.895); b = a + random.uniform(0.0008, 0.008)
    seg = sub(PIE, a, min(b, 103.905), random.uniform(-0.00008, 0.00008))
    if len(seg) >= 3 and F.match_line(EAST, F.Line(seg))["status"] != "confirmed":
        bad += 1
check("200 random stretches ON the route (+-9 m geometry noise), same direction: all confirmed", bad == 0, bad)
mp = F.match_point(EAST, (1.33392, 103.8940))
check("a point-only report ON the route line -> UNVERIFIED (no line / direction), never confirmed", mp["status"] == "unverified" and "cannot be confirmed" in mp["explain"])
mp = F.match_point(EAST, (1.3348, 103.8940))
check("a point report 95 m from the route -> REJECTED (nearby)", mp["status"] == "rejected" and mp["reason"] == "nearby")
idx = F.RouteIndex({("51", 1): PIE, ("51", 2): WEST.pts, ("52", 1): PARA.pts, ("53", 1): CROSS.pts, ("99", 1): [(1.30, 103.70), (1.31, 103.72)]})
jev = F.parse_tomtom_incidents(tt_inc, now=1.0)[0]
res = idx.match(jev, now=1.0)
check("island-wide: the jam affects 51 dir 1 ONLY; 51 dir 2, 52 and 53 rejected with reasons; 99 (far away) not even compared",
      [k for k, _ in res["confirmed"]] == [("51", 1)] and sorted(m["reason"] for _, m in res["rejected"]) == ["crossing", "opposite_direction", "parallel_road"]
      and ("99", 1) not in [k for k, _ in res["rejected"]], res)

print("VALIDITY (road works and incidents)")
now = 1_800_000_000.0
check("in force", F.event_active({"start": now - 3600, "end": now + 3600}, now)[0])
check("ended -> not in force", not F.event_active({"start": now - 7200, "end": now - 60}, now)[0])
check("starts after the 60-min horizon -> not in force", not F.event_active({"start": now + 4000, "end": None}, now, 60)[0])
a_, why = F.event_active({"start": now + 1200, "end": None}, now, 60)
check("starts within the horizon -> counted for the forecast, labelled", a_ and "starts" in why)
rwev = {"id": "rw", "kind": "roadworks", "line": [], "pt": (1.33392, 103.8940), "start": now - 86400 * 9, "end": now - 86400, "source": "LTA"}
m = F.match_event(EAST, rwev, now)
check("road works ON the route but its approved window has ended -> REJECTED (not in force)", m["status"] == "rejected" and m["reason"] == "not_in_force")

print("GROUPING DUPLICATES")
g = F.group_events(F.parse_tomtom_incidents({"incidents": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[x[1], x[0]] for x in sub(PIE, 103.8925, 103.8952)]},
                                                          "properties": {"id": "A1", "iconCategory": 1, "delay": 120, "length": 300}}]}, 1.0) + lta)
check("an LTA accident point 2 m from a TomTom accident line -> ONE event, line kept, both sources listed",
      len(g) == 1 and g[0]["geom"] == "line" and g[0]["sources"] == ["TomTom", "LTA"] and g[0]["towards"] == "Changi Airport")

print("RAINFALL EXPOSURE (estimated)")
check("intensity classes (WMO, mm/h)", [F.rain_class(x) for x in (0, 1, 5, 20, 60)] == ["none", "light", "moderate", "heavy", "violent"])
stns = [{"id": "W", "lat": 1.3345, "lon": 103.8800, "mm": 2.5}, {"id": "D", "lat": 1.3300, "lon": 103.8500, "mm": 0.0}]
rx = F.rain_exposure(EAST, stns)
check("a heavy gauge (30 mm/h) next to the route: part of the route heavily exposed, with confidence and an ESTIMATED label",
      rx["km_heavy"] > 0 and 0 < rx["share_heavy"] < 1 and rx["confidence"] in ("high", "medium") and "ESTIMATED" in rx["label"] and rx["segments"])
rx = F.rain_exposure(F.Line([(1.44, 104.05), (1.44, 104.06)]), stns)
check("no gauge within 5 km: no estimate at all (coverage 0, nothing exposed)", rx["coverage"] == 0 and rx["km_wet"] == 0 and rx["max_mmh"] is None)
rx2 = F.rain_exposure(EAST, stns, prev_stations=[dict(s, mm=0.2) for s in stns])
check("rain trend from the previous reading set (rising)", rx2["trend"] == "rising")

print("FORECAST MATHS")
mid, lo, hi = F.forecast_speed(60, 80, 60, 60, 0.0, 30)
check("no anomaly, no trend -> the baseline, inside an 80 % band", mid == 60 and lo < 60 < hi)
f15, f60 = F.forecast_speed(30, 80, 60, 60, 0.0, 15), F.forecast_speed(30, 80, 60, 60, 0.0, 60)
check("an anomaly reverts towards the time-of-day baseline (+15 slower than +60), the band widens with the horizon",
      f15[0] < f60[0] < 60 and (f60[2] - f60[1]) > (f15[2] - f15[1]))
fw = F.forecast_speed(30, 80, 60, 60, -5.0, 15)
check("a falling trend lowers the forecast but is damped (never more than half the anomaly)", fw[0] < f15[0] and fw[0] >= f15[0] - 0.5 * 30 * math.exp(-0.5) - 0.1)
t0 = 1_800_000_000.0
hist = [(t0 - d * 86400 + k * 60, 40.0) for d in (1, 2, 8, 9) for k in (-10, 0, 10)]
b = F.baseline(hist, t0)
check("baseline from collected history needs >= 3 days (same day type, +-15 min)", b["kmh"] == 40.0 and b["days"] >= 3 and F.baseline(hist[:3], t0)["kmh"] is None)
s1 = F.speed_state(35, 80, 40)
s2 = F.speed_state(20, 80, 40)
check("a road that is NORMALLY slow at this time is 'recurring', not abnormal; a sudden drop is 'abnormal'", s1["state"] == "recurring_slow" and s2["state"] == "abnormal")
check("without history the reference is free-flow and says so", "no history" in F.speed_state(30, 80, None)["ref_src"])
sl, n = F.trend([(t0 - 600, 60), (t0 - 300, 50), (t0, 40)], t0)
check("trend: least squares km/h per minute", abs(sl + 2.0) < 1e-6 and n == 3)
g1 = F.jam_forecast(300, 20.0, 30)
g2 = F.jam_forecast(300, -5.0, 30)
check("jam forecast: building jams grow (damped), easing jams decay; recovery only once it stops building",
      g1[0] > 300 and g1[3] == "building" and g2[0] < 300 and g2[3] == "easing" and F.jam_recovery_min(300, 20.0) is None and F.jam_recovery_min(300, -5.0) > 0)

print("ALERT BOOK")
P = dict(F.PARAMS)
bk = F.AlertBook(P)
c = {"key": "ev:J1", "kind": "jam", "level": "high", "title": "Jam", "services": [{"svc": "51", "dir": 1}], "delay": {15: 5.0, 30: 6.0, 60: 4.0}, "deps": ["tomtom_incidents"]}
n1 = bk.update([c], 1000.0, {"tomtom_incidents": True})
n2 = bk.update([dict(c, delay={15: 5.5, 30: 6.5, 60: 4.0})], 1120.0, {"tomtom_incidents": True})
check("a new condition raises ONE alert; the unchanged condition next cycle does NOT re-alert", len(n1) == 1 and n1[0]["why"] == "new" and n2 == [] and len(bk.alerts) == 1)
a = list(bk.alerts.values())[0]
bk.ack(a["id"], "Ops 1", 1130.0)
check("acknowledge: recorded with name and time, faded in the view, audit entry", bk.view(1131.0)[0]["faded"] and a["acked"]["by"] == "Ops 1" and any(x["action"] == "acknowledged" for x in bk.audit))
n3 = bk.update([dict(c, level="critical", delay={15: 12.0, 30: 14.0, 60: 9.0})], 1240.0, {"tomtom_incidents": True})
check("material worsening (high -> critical, 6 -> 14 min) RE-ALERTS and clears the acknowledgement", len(n3) == 1 and n3[0]["why"] == "worsened" and not a["acked"] and a["realerts"] == 1)
n4 = bk.update([dict(c, level="critical", delay={15: 12.0, 30: 14.5, 60: 9.0})], 1360.0, {"tomtom_incidents": True})
check("small changes after a re-alert: no new notification", n4 == [])
n5 = bk.update([dict(c, level="critical", delay={15: 12.0, 30: 14.5, 60: 9.0})], 1240.0 + 15 * 60 + 1, {"tomtom_incidents": True})
check("critical and unacknowledged for 15 min -> ESCALATED once", len(n5) == 1 and n5[0]["why"] == "escalated" and a["escalated"])
check("escalation does not repeat", bk.update([dict(c, level="critical", delay={15: 12.0, 30: 14.5, 60: 9.0})], 2400.0, {"tomtom_incidents": True}) == [])
bk.update([], 2500.0, {"tomtom_incidents": False})
check("condition missing while the source is STALE -> kept (clearance cannot be verified), flagged", a["status"] == "active" and a["stale"])
bk.update([], 2620.0, {"tomtom_incidents": True})
check("... first fresh cycle without it: still active (needs 2)", a["status"] == "active")
bk.update([], 2740.0, {"tomtom_incidents": True})
check("... second fresh cycle without it: CLEARED (verified), kept in history", a["status"] == "resolved" and any(x["action"] == "cleared" for x in bk.audit)
      and bk.view(2741.0, history=True)[-1]["id"] == a["id"] and bk.counts()["active"] == 0)
n6 = bk.update([c], 2800.0, {"tomtom_incidents": True})
check("it comes back within 30 min -> REOPENED (same alert id, history kept)", len(n6) == 1 and n6[0]["why"] == "reopened" and a["status"] == "active")
acts = [x["action"] for x in bk.audit]
check("full audit trail: raised, acknowledged, re-alerted, escalated, unverifiable, cleared, reopened",
      all(k in acts for k in ("raised", "acknowledged", "re-alerted", "escalated", "unverifiable", "cleared", "reopened")), acts)
bk2 = F.AlertBook(P)
bk2.update([dict(c, key="ev:A", level="watch"), dict(c, key="ev:B", level="critical"), dict(c, key="ev:C", level="high")], 10.0, {})
check("ranking: critical first, then high, then watch", [x["level"] for x in bk2.view(11.0)] == ["critical", "high", "watch"])

print("STORE (SQLite, migrations, calibration)")
stv = F.Store(os.path.join(TMP, "store.db"))
check("schema migrated to version 1", stv.version == 1 and stv.ok)
stv.add_forecasts([(100.0, "k", 15, 1000.0, 50.0, 40.0, 60.0), (100.0, "k", 30, 1900.0, 50.0, 40.0, 60.0)])
stv.settle("k", 1010.0, 55.0)
stv.settle("k", 1905.0, 70.0)
cal = stv.calibration(2000.0)
check("calibration: forecasts settled against later observations -> MAE and band coverage are MEASURED", cal["n"] == 2 and cal["mae"] == 12.5 and cal["coverage"] == 0.5)
bk3 = F.AlertBook(P, store=stv, now=10.0)
bk3.update([c], 10.0, {})
check("alerts and audit persisted; a restart reloads them", len(F.AlertBook(P, store=stv, now=20.0).alerts) == 1 and stv.audit_list(5)[0]["action"] == "raised")

print("DEMO (deterministic, fictional, separate)")
d1, _ = F.demo_state(3)
d2, _ = F.demo_state(3)
j1, j2 = json.dumps(F.public_state(d1), sort_keys=True, default=str), json.dumps(F.public_state(d2), sort_keys=True, default=str)
check("the same step gives exactly the same state (deterministic)", j1 == j2)
check("demo state says DEMO everywhere it matters (mode, label) and only uses fictional X-services",
      d1["mode"] == "demo" and "FICTIONAL" in d1["demo"]["label"] and all(s["svc"].startswith("X") for s in d1["services"]))
d6, _ = F.demo_state(6)
check("demo story: verified critical at step 3, escalated by step 5, acknowledged + advisory recovery at step 6",
      any(a["level"] == "critical" for a in d1["alerts"]) and any(a.get("escalated") for a in F.demo_state(5)[0]["alerts"])
      and any(a.get("acked") for a in d6["alerts"]) and d6["recovery"]["suggested"] in ("adjust", "halfway") and "SC must approve" in d6["recovery"]["basis"])
ev3 = next(e for e in d1["events"] if e["id"] == "tomtom:DEMO-JAM-1")
check("demo jam: X101 dir 1 + X305 confirmed; X101 dir 2 (opposite), X102 (parallel), X201 (flyover) rejected",
      sorted((s["svc"], s["dir"]) for s in ev3["services"]) == [("X101", 1), ("X305", 1)]
      and sorted((r["svc"], r["reason"]) for r in ev3["rejected"]) == [("X101", "opposite_direction"), ("X102", "parallel_road"), ("X201", "crossing")])
rec = F.recovery_options("51", 1, 30.0, 8.0, 5.0)
adj = next(o for o in rec["options"] if o["id"] == "adjust")
check("recovery: the adjustment never exceeds 8 min; a layover under 7 min fails the check", max(1, min(8, 15)) == 8 and "hold the next departure 8" in adj["name"]
      and next(x for x in adj["checks"] if "Layover" in x["rule"])["ok"] is False)

print("LIVE CYCLE (fake HTTP client serving documented sample responses)")
class Resp:
    def __init__(self, code, j): self.status_code, self._j = code, j
    def json(self): return self._j
    def raise_for_status(self):
        if self.status_code >= 400:
            e = app.httpx.HTTPStatusError(f"HTTP {self.status_code}"); e.response = self; self.text = ""; raise e
CALLS = []
MODE = {"fail": False}
def flow_at(lat, lon):
    L = min((EAST, WEST), key=lambda l: float(l.locate(F.to_xy(lat, lon))[0][0]))     # the provider snaps to the nearest carriageway
    a = float(L.locate(F.to_xy(lat, lon))[1][0])
    pts = [L.point_at(a - 120), L.point_at(a), L.point_at(a + 120)]
    return {"flowSegmentData": {"currentSpeed": 24, "freeFlowSpeed": 78, "currentTravelTime": 90, "freeFlowTravelTime": 30, "confidence": 0.95, "roadClosure": False,
                                "coordinates": {"coordinate": [{"latitude": p[0], "longitude": p[1]} for p in pts]}}}
class FakeClient:
    async def get(self, url, params=None, headers=None, timeout=None, **k):
        CALLS.append(url)
        if MODE["fail"]:
            raise RuntimeError("network down")
        if "flowSegmentData" in url:
            la, lo = [float(x) for x in params["point"].split(",")]
            return Resp(200, flow_at(la, lo))
        if "incidentDetails" in url:
            return Resp(200, tt_inc)
        if url.endswith("/TrafficIncidents"):
            return Resp(200, {"value": [{"Type": "Accident", "Latitude": 1.33382, "Longitude": 103.8760, "Message": "(26/10)08:01 Accident on PIE (towards Changi Airport) after KPE."}]})
        if url.endswith("/RoadWorks"):
            return Resp(200, {"value": [] if (params or {}).get("$skip") else [{"EventID": "RW1", "StartDate": "2020-01-01", "EndDate": "2099-12-31", "RoadName": "UBI AVENUE 2", "SvcDept": "SP"}]})
        if url.endswith("/BusServices"):
            return Resp(200, {"value": [] if (params or {}).get("$skip") else [{"ServiceNo": "51", "Direction": 1, "AM_Peak_Freq": "08-08", "AM_Offpeak_Freq": "08-08", "PM_Peak_Freq": "08-08", "PM_Offpeak_Freq": "08-08"}]})
        if "rainfall" in url:
            return Resp(200, rain_v2)
        if "two-hr" in url or "2-hour" in url:
            return Resp(200, nea2)
        return Resp(404, {})
    async def post(self, *a, **k): raise RuntimeError("no post")
    async def aclose(self): pass
FAKE = FakeClient()
app.client = lambda: FAKE
app.KEY = "lta-test"
app.TT["key"] = "SECRET-TT-KEY-123"
stops = {}
routes = {}
for key, line in {("51", 1): PIE, ("51", 2): WEST.pts, ("52", 1): PARA.pts, ("53", 1): CROSS.pts}.items():
    L = F.Line(line); sl = []
    for i in range(6):
        la, lo = L.point_at(L.length * i / 5); code = f"{key[0]}{key[1]}{i:02d}"
        stops[code] = {"code": code, "name": f"Stop {code}", "road": "PIE", "lat": la, "lon": lo}
        sl.append({"seq": i + 1, "code": code, "dist": L.length * i / 5000})
    routes[key] = sl
ST = {"stops": stops, "routes": routes, "dirs": {"51": [1, 2], "52": [1], "53": [1]}, "at_stop": {}, "error": None}
async def fake_static(): return ST
app.static = fake_static
app.tr_lines_kick = lambda st: None
app.TR_LINES.update(lines={k: {"line": v} for k, v in {("51", 1): PIE, ("51", 2): WEST.pts, ("52", 1): PARA.pts, ("53", 1): CROSS.pts}.items()}, n=4, at=time.time())
async def no_bands(*a, **k): raise AssertionError("LTA speed bands must not be used by the Forecast Centre")
app.bands_state = no_bands
app.FC_SERVICES[:] = ["51"]
s0 = run(app.api_forecast_state())
check("live state: mode live, evaluated, 8 KPIs", s0.get("mode") == "live" and len(s0["kpis"]) == 8 and not s0.get("error"), s0.get("error"))
K = {k["id"]: k for k in s0["kpis"]}
check("live KPIs computed from the sources (verified incidents, services affected, congestion zones, rain, road works)",
      K["verified_incidents"]["available"] and K["services_affected"]["value"] >= 1 and K["congestion_zones"]["value"] == 1 and K["rain_exposure"]["available"]
      and K["roadworks"]["value"] == 1, {k: (v.get("value"), v.get("available")) for k, v in K.items()})
check("forecast confidence is 'Not measured yet' until 30 checks exist (no invented accuracy)", K["model_confidence"]["value"] is None and "Not measured" in K["model_confidence"]["text"])
jv = next(e for e in s0["events"] if e["kind"] == "jam")
check("live jam: 51 dir 1 confirmed; 51 dir 2 / 52 / 53 rejected", [(s["svc"], s["dir"]) for s in jv["services"]] == [("51", 1)] and len(jv["rejected"]) == 3)
uv = next(e for e in s0["events"] if e["id"].startswith("lta:"))
check("LTA point on the route: UNVERIFIED, listed separately, not a confirmed service", uv["status"] == "unverified" and not uv["services"] and uv["unverified_services"])
check("probes sampled along the watched service, direction verified, within the budget", len(s0["probes"]) >= 1 and all(p["dir_ok"] for p in s0["probes"])
      and s0["config"]["probe_used"] >= 1 and s0["config"]["speed_bands_used"] is False)
check("alerts raised for the verified jam and the unverified accident (kept apart)", any(a["key"].startswith("ev:") for a in s0["alerts"]) and any(a["key"].startswith("uv:") for a in s0["alerts"]))
check("no API key anywhere in the payload", "SECRET-TT-KEY-123" not in json.dumps(s0, default=str))
check("live state never contains demo data", s0["mode"] == "live" and "DEMO" not in json.dumps(s0["events"], default=str) and "demo" not in s0)
sv = run(app.api_forecast_service(svc="51", dir=1))
check("service intelligence 51 dir 1: route, stops, verified jam with explanation, extra delay per horizon, links",
      sv.get("line") and len(sv["stops"]) == 6 and any(v["kind"] == "jam" and "same direction" in v["explain"] for v in sv["verified"])
      and set(sv["extra"]) == {15, 30, 60} and any(l["href"].startswith("/control?svc=51") for l in sv["links"]), {k: sv.get(k) for k in ("error", "verified")})
sv2 = run(app.api_forecast_service(svc="52", dir=1))
check("service 52 (parallel road): the jam is REJECTED with the parallel-road reason; no extra delay from it",
      any(r["reason"] == "parallel_road" for r in sv2["rejected"]) and not any(v["kind"] == "jam" for v in sv2["verified"]))
class Req:
    def __init__(self, body): self._b, self.cookies = json.dumps(body).encode(), {}
    async def body(self): return self._b
aid = next(a["id"] for a in s0["alerts"] if a["key"].startswith("ev:"))
r = run(app.api_forecast_ack(aid, Req({"by": "Controller A"})))
check("acknowledge over the API: stored with the name, audit trail", r.get("ok") and r["alert"]["acked"]["by"] == "Controller A"
      and run(app.api_forecast_audit(alert=aid))["audit"][0]["action"] == "acknowledged")
# budget: cap reached -> no more calls, the reason is reported
app.FC_PROBE_CAP = app.FC["budget"]["n"]
n_before = sum(1 for u in CALLS if "flowSegmentData" in u)
app.FC["probes"].clear()
run(app.fc_probe_service("51", 1, time.time(), force=True))
check("probe budget reached: no further flow calls, the reason is reported", sum(1 for u in CALLS if "flowSegmentData" in u) == n_before and "budget" in (app.FC["probe_err"] or ""))
app.FC_PROBE_CAP = 400
# stale: TomTom answers stop, the last good data is kept and marked stale
MODE["fail"] = True
app.TT["raw_at"] = time.time() - 3600
app.TT["inc_at"] = time.time()
s1 = run(app.fc_cycle(force=True))
K1 = {k["id"]: k for k in s1["kpis"]}
check("API failure after good data: last valid observations kept, KPIs marked STALE",
      app.FC["world"]["sources"]["tomtom_incidents"]["status"] == "stale" and K1["congestion_zones"]["stale"] and K1["congestion_zones"]["value"] == 1)
check("... and alerts are NOT cleared while the source is stale", all(a["status"] == "active" for a in app.FC["book"].alerts.values() if "tomtom_incidents" in (a.get("deps") or [])))
app.TT.pop("raw", None)
s2 = run(app.fc_cycle(force=True))
K2 = {k["id"]: k for k in s2["kpis"]}
check("no TomTom data at all: incident and congestion KPIs say Unavailable (value None), never 0 by default",
      K2["verified_incidents"]["value"] is None and not K2["verified_incidents"]["available"] and K2["congestion_zones"]["value"] is None)
MODE["fail"] = False
d = run(app.api_forecast_state(mode="demo", step=4))
check("demo over the API: mode demo, DEMO label, fictional clock, separate from the live book",
      d["mode"] == "demo" and d["demo"]["step"] == 4 and "FICTIONAL" in d["demo"]["label"] and all(a["id"] not in app.FC["book"].alerts for a in d["alerts"]))
r = run(app.api_forecast_ack("x", Req({"mode": "demo"})))
check("demo acknowledgement is not stored", r.get("demo") is True)

print("EXISTING MODULES")
check("planned-timetable store works again (was overwritten by the TomTom state)", app.tt_summary()["loaded"] is False and "key" in app.TT)
src = open(os.path.join(sys.argv[1], "app.py"), encoding="utf-8").read()
check("route handlers: /command -> forecast.html, /command/classic -> command.html, /forecast",
      'return _page("forecast.html")' in src and '"/command/classic"' in src and '@app.get("/forecast"' in src)

print("JSON / FIXTURES")
def strict(o):
    try:
        json.dumps(o); return True
    except (TypeError, ValueError) as e:
        print("     ", e); return False
check("live state, service views and demo states are plain JSON (no numpy / tuple-key leaks)",
      strict(s0) and strict(sv) and strict(sv2) and all(strict(run(app.api_forecast_state(mode="demo", step=k))) for k in range(7)))
if os.environ.get("FC_FIX"):
    J = lambda o: json.loads(json.dumps(o))
    fx = {"live": J(s0), "live_svc": {"51:1": J(sv), "52:1": J(sv2)}, "demo": {}, "demo_svc": {}, "services": J(run(app.api_forecast_services())), "demo_services": J(run(app.api_forecast_services(mode="demo")))}
    for k in range(7):
        fx["demo"][str(k)] = J(run(app.api_forecast_state(mode="demo", step=k)))
        for key in (("X101", 1), ("X101", 2), ("X102", 1), ("X201", 1), ("X305", 1), ("X410", 1)):
            fx["demo_svc"][f"{k}:{key[0]}:{key[1]}"] = J(run(app.api_forecast_service(svc=key[0], dir=key[1], mode="demo", step=k)))
    open(os.environ["FC_FIX"], "w").write(json.dumps(fx))
    print("  fixtures written:", os.environ["FC_FIX"], os.path.getsize(os.environ["FC_FIX"]) // 1024, "KB")
print(f"\n{ok}/{ok + fail} passed")
sys.exit(1 if fail else 0)
