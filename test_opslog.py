"""Offline tests for opslog.py: wet-weather impact and event-log rows. Run: python -m pytest test_opslog.py"""
import io
import random
from datetime import datetime

import opslog

TZ = opslog.SGT


def ts(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ).timestamp()


def trips(svc="147", d=1, wet_extra=6.0, n_dry=30, n_wet=10, seed=1):
    rnd = random.Random(seed)
    out = []
    for i in range(n_dry):
        out.append({"service": svc, "direction": d, "day_type": "Weekday", "start_ts": ts(2 + i % 5, 8, rnd.randint(0, 50)),
                    "rt_min": 50 + rnd.uniform(-2, 2), "rain_mm": 0.0})
    for i in range(n_wet):
        out.append({"service": svc, "direction": d, "day_type": "Weekday", "start_ts": ts(9 + i % 5, 8, rnd.randint(0, 50)),
                    "rt_min": 50 + wet_extra + rnd.uniform(-1, 1), "rain_mm": 3.0 if i % 2 else 0.8})
    return out


def test_added_minutes_found():
    s = opslog.wet_impact(trips())[("147", 1)]
    assert 4.5 <= s["added_min"] <= 7.5
    assert s["n_wet"] == 10 and s["n_dry"] == 30
    assert s["confidence"] == "good"
    assert s["periods"]["am_peak"]["added_min"] is not None
    assert s["added_heavy_min"] is not None


def test_no_figure_without_enough_wet_trips():
    s = opslog.wet_impact(trips(n_wet=2))[("147", 1)]
    assert s["added_min"] is None and s["confidence"] == "insufficient"
    mins, basis = opslog.expected_now(s, 8)
    assert mins is None and "wet trips" in basis


def test_dry_trips_must_match_day_type_and_hour():
    t = trips()
    for x in t:
        if x["rain_mm"] >= opslog.WET_MM:
            x["day_type"] = "Sunday"                 # no dry Sunday trips: nothing to compare with
    s = opslog.wet_impact(t)[("147", 1)]
    assert s["added_min"] is None


def test_unknown_rain_left_out():
    t = trips()
    for x in t:
        x["rain_mm"] = None
    assert opslog.wet_impact(t) == {}


def test_expected_now_prefers_current_period_and_heavy():
    s = opslog.wet_impact(trips())[("147", 1)]
    m1, b1 = opslog.expected_now(s, 8)
    assert m1 is not None and "morning peak" in b1
    m2, b2 = opslog.expected_now(s, 8, heavy=True)
    assert "heavy" in b2


def test_network_summary():
    st = opslog.wet_impact(trips() + trips("65", 2, wet_extra=0.2, seed=3))
    n = opslog.network_summary(st)
    assert n["measured"] == 2 and n["with_figure"] == 2 and n["slower"] == 1


def test_traffic_rows_and_merge():
    ev = [{"id": "CONGESTION-PIE-001", "kind": "congestion", "status": "active", "first_seen": ts(12, 8), "last_seen": ts(12, 8, 20),
           "cur": {"avg_kmh": 15, "ref_kmh": 60, "length_m": 1800, "road": "PIE"}, "peak": {"min_kmh": 9},
           "match": {"147|1": {"overlap_km": 1.5}, "65|2": {"overlap_km": 0.4}}},
          {"id": "WEATHER-CLEMENTI-001", "kind": "weather", "status": "cleared", "first_seen": ts(12, 9), "last_seen": ts(12, 9, 40), "cleared_time": ts(12, 9, 45),
           "cur": {"name": "Clementi", "level": "heavy", "max_mm": 4.2}, "peak": {"max_mm": 6.0}, "match": {"156|1": {"pct": 30}}},
          {"id": "CAND-7", "kind": "congestion", "status": "candidate", "match": {"1|1": {}}, "cur": {}},
          {"id": "INCIDENT-X-001", "kind": "incident", "status": "active", "first_seen": ts(12, 9), "match": {}, "cur": {}}]
    rows = opslog.traffic_rows(ev, ts(12, 10), lambda e: e["id"], lambda e, m: m["overlap_km"] * 2 if e["kind"] == "congestion" else None)
    assert [r["category"] for r in rows] == ["traffic", "wet_weather"]
    c, w = rows
    assert c["services"] == ["147:1", "65:2"] and c["peak"] == 3.0 and c["ongoing"] == 1
    assert w["peak"] == 6.0 and w["peak_unit"] == "mm" and w["ongoing"] == 0 and w["end_ts"] == ts(12, 9, 45) and w["kind"] == "rain"
    later = dict(c, services=["147:1", "174:1"], peak=1.0, start_ts=c["start_ts"] + 600)
    m = opslog.merge({"start_ts": c["start_ts"], "services": c["services"], "peak": c["peak"]}, later)
    assert m["services"] == ["65:2", "147:1", "174:1"] and m["peak"] == 3.0 and m["start_ts"] == c["start_ts"]


def test_service_text_round_trip():
    t = opslog.svc_text(["147:1", "65:2", "147:1", "10e:1"])
    assert t.startswith(",") and t.endswith(",") and ",147:" in t
    assert opslog.svc_list(t) == ["10e:1", "65:2", "147:1"] or opslog.svc_list(t) == ["65:2", "10e:1", "147:1"]


def test_workbook_opens():
    from openpyxl import load_workbook
    ev = [{"category": "bunching", "kind": "3BB", "start_ts": ts(12, 8), "end_ts": ts(12, 8, 12), "last_ts": ts(12, 8, 12), "ongoing": False,
           "location": "A → B", "services": ["147:1"], "peak": 1.2, "peak_unit": "min headway", "level": "3 buses", "detail": {"stops": 15}}]
    st = list(opslog.wet_impact(trips()).values())
    wb = load_workbook(io.BytesIO(opslog.workbook(ev, st, "note")))
    assert wb.sheetnames == ["Events", "Wet weather impact", "About"]
    assert wb["Events"]["A2"].value == "Bus bunching" and wb["Events"]["E2"].value == 12
    assert wb["Wet weather impact"]["A2"].value == "147"


def test_short_traffic_events_not_logged():
    base = {"kind": "incident", "status": "active", "cur": {"type": "Accident"}, "peak": {}, "match": {"147|1": {}}}
    short = dict(base, id="INCIDENT-A-001", first_seen=ts(12, 8), last_seen=ts(12, 8, 10))                  # 10 min so far: not yet
    long_ = dict(base, id="INCIDENT-B-001", first_seen=ts(12, 8), last_seen=ts(12, 8, 15))                  # 15 min: logged
    gone = dict(base, id="WEATHER-C-001", kind="weather", status="cleared", first_seen=ts(12, 8), last_seen=ts(12, 8, 9), cleared_time=ts(12, 8, 12),
                cur={"name": "C", "level": "moderate", "max_mm": 0.5})                                       # rain that lasted 12 min: never
    rows = opslog.traffic_rows([short, long_, gone], ts(12, 8, 15), lambda e: e["id"], lambda e, m: None, 15)
    assert [r["key"].split(":")[1] for r in rows] == ["INCIDENT-B-001"]


def _history(now_dt):
    """4 weeks: 147 D1 bunching most weekdays 17:10, PIE congestion on weekdays 17:30, scattered gaps."""
    ev, n = [], 0
    for back in range(1, 29):
        d = now_dt - timedelta(days=back)
        if d.weekday() < 5:
            if back % 5 != 0:
                ev.append({"category": "bunching", "kind": "3BB", "start_ts": d.replace(hour=17, minute=10).timestamp(), "services": ["147:1"],
                           "location": "Clementi Int → Blk 431", "peak": 0.9})
            if back % 2 == 0:
                ev.append({"category": "traffic", "kind": "congestion", "start_ts": d.replace(hour=17, minute=30).timestamp(), "services": ["147:1", "174:1"],
                           "location": "PIE (Clementi Ave 6 → Toh Guan Rd)", "peak": 6.0})
        ev.append({"category": "long_headway", "kind": "long_headway", "start_ts": d.replace(hour=8, minute=5).timestamp(), "services": [f"{65 + back % 3}:2"],
                   "location": "Orchard", "peak": 22.0})
    return ev


from datetime import timedelta  # noqa: E402


def test_analysis_counts():
    now_dt = datetime(2026, 11, 12, 16, 50, tzinfo=TZ)            # a Thursday
    a = opslog.analysis(_history(now_dt), now_dt.timestamp(), 30)
    assert a["by_category"]["bunching"] > 10 and a["by_category"]["long_headway"] == 28
    assert a["by_hour"]["bunching"][17] == a["by_category"]["bunching"] and a["by_hour"]["long_headway"][8] == 28
    assert a["top_services"][0]["key"] == "147:1"
    assert a["top_places"][0]["place"].startswith("Long") is False


def test_insights_predict_the_pattern():
    now_dt = datetime(2026, 11, 12, 16, 50, tzinfo=TZ)
    ins = opslog.insights(_history(now_dt), now_dt.timestamp(), (now_dt - timedelta(days=28)).timestamp())
    likely = [i for i in ins if i["kind"] == "likely"]
    assert likely and likely[0]["title"].startswith("Service 147 Dir 1: bus bunching likely 16:00")
    assert likely[0]["likelihood"] >= 60 and "of the last" in likely[0]["detail"]
    assert any(i["category"] == "traffic" and "PIE" in i["title"] for i in likely)
    assert any(i["kind"] == "peak" for i in ins)


def test_insights_say_when_history_is_short():
    now_dt = datetime(2026, 11, 12, 16, 50, tzinfo=TZ)
    ev = [e for e in _history(now_dt) if e["start_ts"] > (now_dt - timedelta(days=2)).timestamp()]
    ins = opslog.insights(ev, now_dt.timestamp(), (now_dt - timedelta(days=2)).timestamp())
    assert ins[0]["kind"] == "learning"


def test_insights_rain_now_first():
    now_dt = datetime(2026, 11, 12, 16, 50, tzinfo=TZ)
    live = {"areas": [{"name": "Clementi"}], "services": [{"service": "147", "dir": 1, "expected_min": 6.0}, {"service": "156", "dir": 2, "expected_min": None}]}
    ins = opslog.insights(_history(now_dt), now_dt.timestamp(), None, None, live)
    assert ins[0]["kind"] == "rain_now" and "147 D1 +6.0 min" in ins[0]["detail"]


def test_heavy_loading_insight():
    now_dt = datetime(2026, 11, 12, 16, 50, tzinfo=TZ)
    ev = []
    for back in range(1, 29):
        d = now_dt - timedelta(days=back)
        if d.weekday() < 5:
            ev.append({"category": "heavy_loading", "kind": "heavy_loading", "start_ts": d.replace(hour=17, minute=5).timestamp(), "services": ["43:1"],
                       "location": "Punggol Int", "peak": 96.0})
    ins = [i for i in opslog.insights(ev, now_dt.timestamp(), (now_dt - timedelta(days=28)).timestamp()) if i["kind"] == "likely"]
    assert ins and ins[0]["title"].startswith("Service 43 Dir 1: heavy loading likely") and "96% full" in ins[0]["detail"]
    a = opslog.analysis(ev, now_dt.timestamp(), 30)
    assert a["by_category"]["heavy_loading"] == 20 and a["by_hour"]["heavy_loading"][17] == 20
