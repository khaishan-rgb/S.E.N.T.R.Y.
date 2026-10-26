"""S.E.N.T.R.Y. 3.0 - Forecast Centre engine (pure Python + numpy: no network, no web framework).

What it does
  1. Parses provider responses (TomTom Traffic Flow / Incident Details, optional HERE Traffic Flow v7, LTA incidents and road works,
     NEA rainfall and the NEA 2-hour forecast) into one event model.
  2. STRICT ROUTE MATCHING. An event affects a bus service only when its own line geometry runs ALONG the service's route line, in the
     same direction of travel, for a minimum length, inside its validity period. Nearby events, parallel roads, the opposite carriageway
     and roads that only cross the route (junctions, flyovers) are rejected, each with a written reason. A point-only event (LTA) can
     never be confirmed: at best it is UNVERIFIED (shown on the map, never counted as a confirmed affected service).
  3. Rainfall EXPOSURE: NEA gauge readings are interpolated (inverse distance) onto points every 200 m of the route. The result is
     always labelled an estimate, with a confidence from the distance to the nearest gauges.
  4. Explainable statistical FORECASTS for +15 / +30 / +60 min: persistence of the current anomaly with mean reversion to the historical
     time-of-day baseline, plus a damped trend. Uncertainty bands come from the measured anomaly spread (or a wide default while the
     history is building). Accuracy is only ever reported once it has been MEASURED against later observations (calibration()).
  5. A rule-based service risk estimate (critical / high / watch / info), clearly labelled as rules, not machine learning.
  6. An alert book: ranking, duplicate grouping, no repeat alerts for unchanged conditions, re-alert on material worsening,
     acknowledge, verified clearance, escalation of persistent critical alerts, and a full audit trail.
  7. A deterministic, clearly FICTIONAL demo world for showcase mode (fictional services X101, X102, X201, X305, X410).

Machine learning (LightGBM etc.) is deliberately NOT used yet: it needs weeks of collected snapshots and a time-based validation.
The store keeps exactly those snapshots (fc_probe, fc_jam) so a model can be trained later without changing the page.
"""
import contextlib
import hashlib
import json
import math
import re
import sqlite3
import statistics
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import numpy as np

SGT = timezone(timedelta(hours=8))
ENGINE = "FC-1.0"
METHOD = ("Statistical rule-based estimate: persistence of the current anomaly with mean reversion to the time-of-day baseline and a "
          "damped trend. Not machine learning.")

PARAMS = {
    # ---- strict route matching
    "on_route_m": 15.0,         # an event line within this distance of the route line is "on" the route
    "parallel_max_m": 60.0,     # same-direction line further than on_route_m but within this = a parallel road (rejected)
    "opp_max_m": 45.0,          # opposite-direction line within this = the other carriageway / other direction (rejected)
    "dir_max_deg": 35.0,        # bearing difference for "same direction"
    "opp_min_deg": 145.0,       # bearing difference for "opposite direction"
    "min_overlap_m": 40.0,      # an event must run along the route at least this far (or half its own length when shorter)
    "point_on_route_m": 12.0,   # a point-only event this close to the route line is UNVERIFIED (never confirmed)
    "nearby_m": 300.0,          # reported as "nearby, not on the route" up to this distance (for the explanation only)
    "sample_m": 10.0,           # event lines are sampled every 10 m for the overlap test
    "dup_m": 120.0,             # two reports of the same kind of event within this distance are one event (grouped)
    # ---- rainfall exposure (5-min gauge totals; mm/h = x12)
    "rain_step_m": 200.0,
    "rain_max_km": 5.0,         # no estimate further than this from every gauge
    "rain_hi_conf_km": 2.0,     # nearest gauge within 2 km = high confidence, within 3.5 km = medium, else low
    "rain_med_conf_km": 3.5,
    "rain_light_mmh": 0.1, "rain_mod_mmh": 2.5, "rain_heavy_mmh": 10.0, "rain_violent_mmh": 50.0,   # WMO intensity classes
    "rain_severe_share": 0.20,  # a service is "severely exposed" with >= 20 % of its route in COUNTED heavy rain
    "rain_count_km": 2.0,       # rain is COUNTED only on route stretches within 2 km of a gauge that itself reports heavy rain (nothing further)
    "alert_unverified": False,  # point-only reports (LTA) never raise alerts or service risk: map + evidence list only, "not counted"
    # ---- forecasting
    "horizons": [15, 30, 60],
    "tau_min": 30.0,            # e-folding time of a speed anomaly (mean reversion)
    "jam_tau_min": 40.0,        # e-folding time of a jam's delay when it is not growing
    "trend_window_min": 30.0,
    "default_sigma_frac": 0.18, # speed uncertainty (fraction of free-flow speed) while there is no measured history
    "hist_min_days": 3,         # a baseline needs readings on at least this many different days
    "hist_keep_days": 35,
    "abnormal_ratio": 0.70,     # current speed below 70 % of the time-of-day baseline = abnormal
    "normal_ratio": 0.85,
    "slow_ff_ratio": 0.60,      # below 60 % of free-flow = slow road
    "recur_share": 0.5,         # a jam seen on >= half of the past same-day-type days at this time is recurring (normal for the time)
    # ---- service risk rules (minutes)
    "crit_min": 10.0, "crit_hw": 0.8,   # critical: predicted extra delay >= max(10, 0.8 x headway)
    "high_min": 5.0, "high_hw": 0.5,    # high: >= max(5, 0.5 x headway)
    "watch_min": 2.0,
    # ---- alerts
    "resolve_cycles": 2,        # absent from 2 consecutive evaluations with FRESH data = cleared (verified)
    "escalate_min": 15.0,       # critical and unacknowledged for 15 min = escalated
    "worse_min": 3.0,           # re-alert when the 30-min predicted delay grows by >= max(3 min, 50 %)
    "worse_frac": 0.5,
    "worse_services": 3,        # ... or 3 more verified services are affected
    "reopen_min": 30.0,         # a cleared alert that comes back within 30 min reopens (same id) instead of a new alert
}

LEVELS = ["info", "watch", "high", "critical"]
RANK = {k: i for i, k in enumerate(LEVELS)}

# Singapore: one fixed local projection is accurate to < 0.1 % across the island
LAT0 = 1.3521
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 110574.0


def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def in_sg(lat, lon):
    return lat is not None and lon is not None and 1.1 < lat < 1.5 and 103.5 < lon < 104.2


def angdiff(a, b):
    d = abs((a - b) % 360.0)
    return 360.0 - d if d > 180.0 else d


def hav_m(lat1, lon1, lat2, lon2):
    return math.hypot((lon2 - lon1) * KX, (lat2 - lat1) * KY)


def sgt(ts):
    return datetime.fromtimestamp(ts, SGT)


def hhmm(ts):
    return sgt(ts).strftime("%H:%M") if ts else None


def day_type(ts, holidays=()):
    d = sgt(ts)
    if d.strftime("%Y-%m-%d") in holidays or d.weekday() == 6:
        return "sun_ph"
    return "sat" if d.weekday() == 5 else "weekday"


def slot15(ts):
    d = sgt(ts)
    return (d.hour * 60 + d.minute) // 15


def stable_id(*parts):
    return "fc-" + hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


# =========================================================================== geometry
class Line:
    """A polyline in a local metric projection: lengths, chainage and the bearing of every segment."""

    def __init__(self, pts):
        clean = []
        for p in pts or []:
            la, lo = num(p[0]), num(p[1])
            if la is None or lo is None:
                continue
            if not clean or (abs(clean[-1][0] - la) > 1e-9 or abs(clean[-1][1] - lo) > 1e-9):
                clean.append((la, lo))
        self.pts = clean
        self.ok = len(clean) >= 2
        if not self.ok:
            self.X = np.zeros((len(clean), 2))
            self.length = 0.0
            return
        X = np.array([[p[1] * KX, p[0] * KY] for p in clean], dtype=float)
        self.X = X
        self.A, self.B = X[:-1], X[1:]
        self.D = self.B - self.A
        self.L = np.hypot(self.D[:, 0], self.D[:, 1])
        self.L[self.L < 1e-6] = 1e-6
        self.cum = np.concatenate([[0.0], np.cumsum(self.L)])
        self.length = float(self.cum[-1])
        self.brg = (np.degrees(np.arctan2(self.D[:, 0], self.D[:, 1])) + 360.0) % 360.0
        self.lo = X.min(0)
        self.hi = X.max(0)

    def bbox_overlaps(self, lo, hi, pad):
        return self.ok and not (hi[0] + pad < self.lo[0] or lo[0] - pad > self.hi[0] or hi[1] + pad < self.lo[1] or lo[1] - pad > self.hi[1])

    def locate(self, P, pad=None):
        """Nearest point on this line for every row of P (N x 2, metres) ->
        (dist m, chainage m, bearing of the segment) arrays. pad limits the work to segments near P's bounding box."""
        P = np.asarray(P, dtype=float).reshape(-1, 2)
        if not self.ok or not len(P):
            n = len(P)
            return np.full(n, np.inf), np.zeros(n), np.zeros(n)
        idx = np.arange(len(self.A))
        if pad is not None:
            lo, hi = P.min(0) - pad, P.max(0) + pad
            sx = np.minimum(self.A[:, 0], self.B[:, 0]) <= hi[0]
            sx &= np.maximum(self.A[:, 0], self.B[:, 0]) >= lo[0]
            sx &= np.minimum(self.A[:, 1], self.B[:, 1]) <= hi[1]
            sx &= np.maximum(self.A[:, 1], self.B[:, 1]) >= lo[1]
            idx = np.nonzero(sx)[0]
            if not len(idx):
                n = len(P)
                return np.full(n, np.inf), np.zeros(n), np.zeros(n)
        A, D, L2 = self.A[idx], self.D[idx], (self.L[idx] ** 2)
        R = P[:, None, :] - A[None, :, :]
        t = np.clip((R * D[None, :, :]).sum(2) / L2[None, :], 0.0, 1.0)
        Q = A[None, :, :] + t[:, :, None] * D[None, :, :]
        d = np.hypot(P[:, None, 0] - Q[:, :, 0], P[:, None, 1] - Q[:, :, 1])
        j = d.argmin(1)
        r = np.arange(len(P))
        seg = idx[j]
        along = self.cum[seg] + t[r, j] * self.L[seg]
        return d[r, j], along, self.brg[seg]

    def sample(self, step):
        """Points every `step` metres (and the last vertex) with the bearing of the segment each lies on, and the length each represents."""
        if not self.ok:
            return np.zeros((0, 2)), np.zeros(0), np.zeros(0)
        n = max(1, int(math.ceil(self.length / step)))
        s = np.linspace(0.0, self.length, n + 1)
        seg = np.clip(np.searchsorted(self.cum, s, side="right") - 1, 0, len(self.A) - 1)
        f = (s - self.cum[seg]) / self.L[seg]
        P = self.A[seg] + f[:, None] * self.D[seg]
        w = np.full(len(s), self.length / n)
        w[0] = w[-1] = self.length / n / 2.0
        return P, self.brg[seg], w

    def point_at(self, s):
        """(lat, lon) at chainage s metres."""
        if not self.ok:
            return self.pts[0] if self.pts else None
        s = min(max(0.0, s), self.length)
        i = int(min(len(self.A) - 1, max(0, np.searchsorted(self.cum, s, side="right") - 1)))
        f = (s - self.cum[i]) / self.L[i]
        x, y = self.A[i] + f * self.D[i]
        return (y / KY, x / KX)

    def cut(self, a, b):
        """The part of the line between chainages a and b, as [(lat, lon)]."""
        if not self.ok or b <= a:
            return []
        out = [self.point_at(a)]
        for k in range(len(self.cum)):
            if a < self.cum[k] < b:
                out.append(self.pts[k])
        out.append(self.point_at(b))
        return out


def to_xy(lat, lon):
    return np.array([[lon * KX, lat * KY]])


# =========================================================================== provider parsers
TT_ICON = {0: "Unknown", 1: "Accident", 2: "Fog", 3: "Dangerous conditions", 4: "Rain", 5: "Ice", 6: "Jam", 7: "Lane closed",
           8: "Road closed", 9: "Road works", 10: "Wind", 11: "Flooding", 14: "Broken down vehicle"}
TT_KIND = {1: "accident", 6: "jam", 7: "lane_closed", 8: "road_closed", 9: "roadworks", 11: "flooding", 14: "breakdown",
           2: "hazard", 3: "hazard", 4: "hazard", 5: "hazard", 10: "hazard", 0: "other"}
TT_MAG = {0: "unknown", 1: "minor", 2: "moderate", 3: "major", 4: "undefined"}

# Can a bus get through? (None = not known from the feed)
BUS_PASS = {
    "road_closed": (False, "Road closed - buses cannot pass"),
    "flooding": (None, "Flooding - may be impassable, verify on site"),
    "lane_closed": (True, "Lane(s) closed - passable with reduced capacity"),
    "roadworks": (True, "Road works - passable unless a closure is reported"),
    "accident": (True, "Accident - passable with delay unless closed"),
    "breakdown": (True, "Broken-down vehicle - passable with delay"),
    "jam": (True, "Slow traffic - passable"),
    "hazard": (True, "Hazard - passable with care"),
    "other": (None, "Unknown restriction"),
}


def _ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_tomtom_flow(j):
    """TomTom Traffic Flow - Flow Segment Data (v4, absolute, json). -> dict or None.
    The segment's own coordinates are kept: they give the road direction used for strict matching."""
    d = (j or {}).get("flowSegmentData") or {}
    cur, ff = num(d.get("currentSpeed")), num(d.get("freeFlowSpeed"))
    if cur is None or not ff:
        return None
    co = (d.get("coordinates") or {}).get("coordinate") or []
    line = []
    for c in co:
        la, lo = num((c or {}).get("latitude")), num((c or {}).get("longitude"))
        if in_sg(la, lo):
            line.append((la, lo))
    return {"cur": cur, "ff": ff, "cur_tt": num(d.get("currentTravelTime")), "ff_tt": num(d.get("freeFlowTravelTime")),
            "conf": num(d.get("confidence")) or 0.0, "closure": bool(d.get("roadClosure")), "frc": d.get("frc"), "line": line,
            "provider": "tomtom"}


def parse_here_flow(j):
    """HERE Traffic API v7 /flow (locationReferencing=shape) -> list of segments with the same fields as parse_tomtom_flow.
    Speeds in the v7 response are metres per second. NOT validated against a live HERE response in this build."""
    out = []
    for r in (j or {}).get("results") or []:
        loc, cf = r.get("location") or {}, r.get("currentFlow") or {}
        sp, ff = num(cf.get("speed")), num(cf.get("freeFlow"))
        if sp is None or not ff:
            continue
        line = []
        for link in ((loc.get("shape") or {}).get("links") or []):
            for p in link.get("points") or []:
                la, lo = num(p.get("lat")), num(p.get("lng"))
                if in_sg(la, lo) and (not line or line[-1] != (la, lo)):
                    line.append((la, lo))
        out.append({"cur": sp * 3.6, "ff": ff * 3.6, "cur_tt": None, "ff_tt": None, "conf": num(cf.get("confidence")) or 0.0,
                    "closure": (cf.get("traversability") == "closed"), "frc": None, "line": line, "road": loc.get("description") or "",
                    "provider": "here"})
    return out


def parse_tomtom_incidents(j, now=None):
    """TomTom Incident Details v5 -> events (line or point), jams included (kind "jam").
    TomTom gives incident lines in the direction of travel, which is what direction matching relies on."""
    now = now or time.time()
    out = []
    for inc in (j or {}).get("incidents") or []:
        pr = inc.get("properties") or {}
        g = inc.get("geometry") or {}
        cat = pr.get("iconCategory")
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            cat = 0
        kind = TT_KIND.get(cat, "other")
        co = g.get("coordinates") or []
        line, pt = [], None
        try:
            if g.get("type") == "LineString":
                line = [(float(c[1]), float(c[0])) for c in co]
                line = [p for p in line if in_sg(*p)]
            elif g.get("type") == "Point":
                pt = (float(co[1]), float(co[0]))
        except (TypeError, ValueError, IndexError):
            continue
        if len(line) < 2:
            line = []
            if pt is None and co:
                continue
        if not line and not (pt and in_sg(*pt)):
            continue
        ev0 = (pr.get("events") or [{}])[0] or {}
        roads = [r for r in (pr.get("roadNumbers") or []) if r]
        desc = (ev0.get("description") or TT_ICON.get(cat, "Incident")).strip()
        mag = pr.get("magnitudeOfDelay")
        passable, ptxt = BUS_PASS.get(kind, BUS_PASS["other"])
        st, en = _ts(pr.get("startTime")), _ts(pr.get("endTime"))
        out.append({
            "id": "tomtom:" + str(pr.get("id") or stable_id(cat, line[:1] or pt)), "source": "TomTom", "provider": "tomtom", "src_key": "tomtom_incidents",
            "kind": kind, "label": TT_ICON.get(cat, "Incident"), "desc": desc, "road": " / ".join(roads),
            "from": (pr.get("from") or "").strip(), "to": (pr.get("to") or "").strip(),
            "line": line, "pt": pt if not line else line[len(line) // 2], "geom": "line" if line else "point",
            "delay_s": num(pr.get("delay")), "length_m": num(pr.get("length")), "magnitude": TT_MAG.get(mag, "unknown") if mag is not None else "unknown",
            "mag": mag if isinstance(mag, int) else None, "start": st, "end": en, "passable": passable, "pass_text": ptxt,
            "observed": now, "validity": "present (TomTom timeValidityFilter)" if st is None and en is None else "start/end from TomTom",
        })
    return out


_LTA_TIME = re.compile(r"^\((\d{1,2})/(\d{1,2})\)\s*(\d{1,2}):(\d{2})\s*")
_LTA_ROAD = re.compile(r"\bon\s+(.+?)(?:\s+\(towards\s+([^)]+)\))?(?:\s+(?:after|before|at|near|from)\b|\.|$)", re.I)
LTA_KIND = {"accident": "accident", "vehicle breakdown": "breakdown", "road block": "road_closed", "roadwork": "roadworks",
            "obstacle": "hazard", "heavy traffic": "jam", "diversion": "road_closed", "unattended vehicle": "hazard",
            "misc.": "other", "plant failure": "other", "reverse flow": "hazard", "weather": "hazard"}


def parse_lta_incidents(rows, now=None):
    """LTA DataMall TrafficIncidents -> point events. LTA gives a point and a message, no line and no direction:
    by policy these are never confirmed against a bus route on their own."""
    now = now or time.time()
    out = []
    for x in rows or []:
        lat, lon = num(x.get("Latitude")), num(x.get("Longitude"))
        if not in_sg(lat, lon):
            continue
        typ = str(x.get("Type") or "Incident").strip()
        msg = str(x.get("Message") or "").strip()
        kind = LTA_KIND.get(typ.lower(), "other")
        rep = None
        m = _LTA_TIME.match(msg)
        body = msg[m.end():] if m else msg
        if m:
            d0 = sgt(now)
            try:
                rep = datetime(d0.year, int(m.group(2)), int(m.group(1)), int(m.group(3)), int(m.group(4)), tzinfo=SGT).timestamp()
                if rep > now + 86400:                    # a December report read in January
                    rep = datetime(d0.year - 1, int(m.group(2)), int(m.group(1)), int(m.group(3)), int(m.group(4)), tzinfo=SGT).timestamp()
            except ValueError:
                rep = None
        road, towards = "", ""
        r = _LTA_ROAD.search(body)
        if r:
            road, towards = r.group(1).strip(), (r.group(2) or "").strip()
        passable, ptxt = BUS_PASS.get(kind, BUS_PASS["other"])
        out.append({"id": "lta:" + stable_id(typ, round(lat, 5), round(lon, 5), msg[:60]), "source": "LTA", "provider": "lta", "src_key": "lta_incidents", "kind": kind,
                    "label": typ, "desc": body or typ, "road": road, "towards": towards, "from": "", "to": "", "line": [], "pt": (lat, lon),
                    "geom": "point", "delay_s": None, "length_m": None, "magnitude": "unknown", "mag": None, "start": None, "end": None, "reported": rep,
                    "passable": passable, "pass_text": ptxt, "observed": now, "validity": "current (LTA live list)"})
    return out


def normalise_roadworks(items, now=None):
    """LTA RoadWorks (already parsed by app.tr_roadworks: road, optional point, start/end epoch) -> point events (or road-name only)."""
    now = now or time.time()
    out = []
    for x in items or []:
        lat, lon = num(x.get("lat")), num(x.get("lon"))
        pt = (lat, lon) if in_sg(lat, lon) else None
        out.append({"id": "ltarw:" + str(x.get("key") or stable_id(x.get("road"), x.get("start_epoch"))), "source": "LTA", "provider": "lta", "src_key": "lta_roadworks",
                    "kind": "roadworks", "label": "Road works", "desc": (x.get("other") or "Approved road works")[:160], "road": x.get("road") or "",
                    "from": "", "to": "", "line": [], "pt": pt, "geom": "point" if pt else "road_name", "delay_s": None, "length_m": None,
                    "magnitude": "unknown", "mag": None, "start": x.get("start_epoch"), "end": x.get("end_epoch"),
                    "passable": True, "pass_text": "Approved works window - lane restrictions not given by LTA", "observed": now,
                    "validity": "approved window (LTA dates)"})
    return out


RAIN_WORDS = ("rain", "shower", "thunder", "drizzle")
HEAVY_WORDS = ("heavy",)


def parse_nea_2h(j):
    """NEA 2-hour weather forecast (data.gov.sg v2 or v1) -> {"areas": [{name, lat, lon, text, rain, heavy}], "valid": (start, end)}."""
    j = j or {}
    data = j.get("data") if isinstance(j.get("data"), dict) else j
    meta = data.get("area_metadata") or []
    items = data.get("items") or []
    if not items:
        return {"areas": [], "valid": None}
    it = items[-1] or {}
    fc = {f.get("area"): (f.get("forecast") if isinstance(f.get("forecast"), str) else (f.get("forecast") or {}).get("text")) for f in (it.get("forecasts") or [])}
    vp = it.get("valid_period") or {}
    out = []
    for a in meta:
        loc = a.get("label_location") or {}
        la, lo = num(loc.get("latitude")), num(loc.get("longitude"))
        txt = fc.get(a.get("name"))
        if not in_sg(la, lo) or not txt:
            continue
        low = txt.lower()
        out.append({"name": a.get("name"), "lat": la, "lon": lo, "text": txt, "rain": any(w in low for w in RAIN_WORDS),
                    "heavy": any(w in low for w in HEAVY_WORDS) and any(w in low for w in RAIN_WORDS)})
    return {"areas": out, "valid": (_ts(vp.get("start")), _ts(vp.get("end")))}


# =========================================================================== event validity and grouping
def event_active(ev, t, horizon_min=0):
    """Is the event in force at time t (or within the next horizon_min)? -> (bool, reason)."""
    st, en = ev.get("start"), ev.get("end")
    if en is not None and en < t:
        return False, f"ended {hhmm(en)}"
    if st is not None and st > t + horizon_min * 60:
        return False, f"starts {sgt(st).strftime('%d %b %H:%M')}"
    if st is not None and st > t:
        return True, f"starts {hhmm(st)} (within the forecast horizon)"
    return True, "in force" + (f" until {sgt(en).strftime('%d %b %H:%M')}" if en else "")


FAMILY = {"accident": "incident", "breakdown": "incident", "hazard": "incident", "road_closed": "closure", "lane_closed": "closure",
          "roadworks": "roadworks", "flooding": "flooding", "jam": "jam", "other": "incident"}


def group_events(events, P=None):
    """Duplicate reports of one event (same family, close together) -> one event. A line report wins over a point report (it can be
    verified); the other reports are kept as corroborating sources."""
    P = P or PARAMS
    lines = [e for e in events if e.get("line")]
    rest = [e for e in events if not e.get("line")]
    out = []
    for e in lines + rest:
        fam = FAMILY.get(e["kind"], "incident")
        host = None
        for g in out:
            if FAMILY.get(g["kind"], "incident") != fam or (fam == "jam" and e["kind"] == "jam" and g["provider"] == e["provider"]):
                continue
            if e.get("pt") and g.get("line"):
                d = Line(g["line"]).locate(to_xy(*e["pt"]))[0][0] if len(g["line"]) >= 2 else math.inf
            elif e.get("pt") and g.get("pt"):
                d = hav_m(e["pt"][0], e["pt"][1], g["pt"][0], g["pt"][1])
            else:
                d = math.inf
            if d <= P["dup_m"]:
                host = g
                break
        if host is None:
            e = dict(e, sources=[e["source"]], members=[e["id"]])
            out.append(e)
        else:
            if e["source"] not in host["sources"]:
                host["sources"].append(e["source"])
            host["members"].append(e["id"])
            if not host.get("road") and e.get("road"):
                host["road"] = e["road"]
            if e.get("towards") and not host.get("towards"):
                host["towards"] = e["towards"]
    return out


# =========================================================================== STRICT ROUTE MATCHING
def match_line(route, ev_line, P=None):
    """Does the event line run ALONG the route line in the SAME direction? route, ev_line: Line.
    -> {"status": confirmed | rejected | none, "reason", "explain", "overlap_m", "a_m", "b_m", "dist_m", "bearing_diff"}"""
    P = P or PARAMS
    if not route.ok or not ev_line.ok:
        return {"status": "none", "reason": "no_geometry", "explain": "No line geometry to compare."}
    pad = max(P["parallel_max_m"], P["nearby_m"]) + 20.0
    if not route.bbox_overlaps(ev_line.lo, ev_line.hi, pad):
        return {"status": "none", "reason": "far", "explain": "Not near the route."}
    S, sb, w = ev_line.sample(P["sample_m"])
    d, along, rb = route.locate(S, pad=pad)
    if not np.isfinite(d).any():
        return {"status": "none", "reason": "far", "explain": "Not near the route."}
    diff = np.abs((sb - rb) % 360.0)
    diff = np.where(diff > 180.0, 360.0 - diff, diff)
    on = d <= P["on_route_m"]
    same = diff <= P["dir_max_deg"]
    opp = diff >= P["opp_min_deg"]
    ov = float(w[on & same].sum())
    ov_opp = float(w[(d <= P["opp_max_m"]) & opp].sum())
    par = float(w[(d > P["on_route_m"]) & (d <= P["parallel_max_m"]) & same].sum())
    cross = float(w[on & ~same & ~opp].sum())
    need = max(P["sample_m"], min(P["min_overlap_m"], 0.5 * ev_line.length))
    dmin = float(np.min(d))
    base = {"overlap_m": round(ov), "event_m": round(ev_line.length), "dist_m": round(dmin, 1)}
    if ov >= need:
        k = on & same
        a, b = float(along[k].min()), float(along[k].max())
        bd = float(np.median(diff[k]))
        dm = float(np.median(d[k]))
        return dict(base, status="confirmed", reason="same_direction_overlap", a_m=round(a), b_m=round(b), dist_m=round(dm, 1),
                    bearing_diff=round(bd, 1),
                    explain=f"Runs along the route for {ov:.0f} m (route km {a / 1000:.2f}-{b / 1000:.2f}), {dm:.0f} m from the route line, "
                            f"same direction of travel (bearings differ by {bd:.0f} deg).")
    if ov_opp >= need:
        k = (d <= P["opp_max_m"]) & opp
        return dict(base, status="rejected", reason="opposite_direction", bearing_diff=round(float(np.median(diff[k])), 1),
                    explain=f"Rejected: on the opposite carriageway / direction ({ov_opp:.0f} m alongside, travel bearings differ by "
                            f"{float(np.median(diff[k])):.0f} deg). The bus does not travel in this direction here.")
    if par >= need:
        k = (d > P["on_route_m"]) & (d <= P["parallel_max_m"]) & same
        return dict(base, status="rejected", reason="parallel_road", dist_m=round(float(np.median(d[k])), 1),
                    explain=f"Rejected: a parallel road {float(np.median(d[k])):.0f} m from the route (same direction, but more than "
                            f"{P['on_route_m']:.0f} m away). The bus does not use this road.")
    if cross > 0:
        return dict(base, status="rejected", reason="crossing",
                    explain=f"Rejected: only crosses the route ({cross:.0f} m within {P['on_route_m']:.0f} m at an angle) - a junction or a "
                            f"flyover/underpass, not the road the bus drives along.")
    if ov > 0:
        return dict(base, status="rejected", reason="too_short",
                    explain=f"Rejected: only {ov:.0f} m runs along the route (needs {need:.0f} m).")
    if dmin <= P["nearby_m"]:
        return dict(base, status="rejected", reason="nearby",
                    explain=f"Rejected: nearby ({dmin:.0f} m from the route line) but not on the bus route.")
    return {"status": "none", "reason": "far", "explain": "Not near the route."}


def match_point(route, pt, P=None):
    """A point-only event (LTA incident / road works) against a route. Never 'confirmed': no line, no direction, no extent."""
    P = P or PARAMS
    if not route.ok or not pt:
        return {"status": "none", "reason": "no_geometry", "explain": "No location."}
    d, along, _ = route.locate(to_xy(*pt), pad=P["nearby_m"] + 50.0)
    d, along = float(d[0]), float(along[0])
    if d <= P["point_on_route_m"]:
        return {"status": "unverified", "reason": "point_on_line", "dist_m": round(d, 1), "a_m": round(along), "b_m": round(along),
                "explain": f"UNVERIFIED: the reported point is {d:.0f} m from the route line (route km {along / 1000:.2f}), but the feed gives "
                           f"no road line, carriageway or direction, so it cannot be confirmed that the bus passes it."}
    if d <= P["nearby_m"]:
        return {"status": "rejected", "reason": "nearby", "dist_m": round(d, 1),
                "explain": f"Rejected: {d:.0f} m from the route line - nearby, not on the route."}
    return {"status": "none", "reason": "far", "explain": "Not near the route."}


def match_event(route, ev, now=None, horizon_min=60, P=None):
    """One event against one route (Line), validity included."""
    P = P or PARAMS
    now = now or time.time()
    if ev.get("line") and len(ev["line"]) >= 2:
        m = match_line(route, ev.get("_L") or Line(ev["line"]), P)
        if ev.get("dir_known") is False and (m["status"] == "confirmed" or m.get("reason") == "opposite_direction"):
            m = dict(m, status="unverified", reason="direction_unknown",
                     explain=f"UNVERIFIED: the line runs along the route ({m.get('overlap_m') or m.get('event_m')} m) but its direction of travel is not known "
                             f"(no TomTom start marker), so it is not counted.")
    elif ev.get("pt"):
        m = match_point(route, ev["pt"], P)
    else:
        return {"status": "none", "reason": "no_location", "explain": "UNVERIFIED: the feed gives only a road name, no location."}
    act, why = event_active(ev, now, horizon_min)
    m["active"], m["validity"] = act, why
    if m["status"] in ("confirmed", "unverified") and not act:
        m = dict(m, status="rejected", reason="not_in_force", explain=f"Rejected: not in force ({why}). " + m["explain"])
    return m


class RouteIndex:
    """Coarse grid (500 m cells) of which service lines pass each cell: island-wide matching only compares an event with the lines
    that can be near it."""
    CELL = 500.0

    def __init__(self, lines):
        self.lines = {}
        self.cells = {}
        for key, pts in (lines or {}).items():
            L = pts if isinstance(pts, Line) else Line(pts)
            if not L.ok:
                continue
            self.lines[key] = L
            S, _, _ = L.sample(self.CELL / 2)
            for c in {(int(x // self.CELL), int(y // self.CELL)) for x, y in np.vstack([S, L.X])}:
                self.cells.setdefault(c, []).append(key)

    def __len__(self):
        return len(self.lines)

    def candidates(self, X, pad_m=400.0):
        X = np.asarray(X, dtype=float).reshape(-1, 2)
        if not len(X):
            return []
        r = int(math.ceil(pad_m / self.CELL))
        cs = set()
        for x, y in X:
            cx, cy = int(x // self.CELL), int(y // self.CELL)
            for i in range(cx - r, cx + r + 1):
                for k in range(cy - r, cy + r + 1):
                    cs.add((i, k))
        out = set()
        for c in cs:
            out.update(self.cells.get(c, ()))
        return sorted(out)

    def match(self, ev, now=None, horizon_min=60, P=None, keep_rejected=True):
        """All service lines an event affects. -> {"confirmed": [(key, m)], "unverified": [...], "rejected": [...]}"""
        P = P or PARAMS
        res = {"confirmed": [], "unverified": [], "rejected": []}
        if ev.get("line") and len(ev["line"]) >= 2:
            L = ev.get("_L") or Line(ev["line"])
            ev["_L"] = L
            S, _, _ = L.sample(100.0)
            keys = self.candidates(S)
        elif ev.get("pt"):
            keys = self.candidates(to_xy(*ev["pt"]))
        else:
            return res
        for k in keys:
            m = match_event(self.lines[k], ev, now, horizon_min, P)
            if m["status"] in res and (m["status"] != "rejected" or keep_rejected):
                res[m["status"]].append((k, m))
        return res


# =========================================================================== rainfall exposure (ESTIMATED)
def rain_class(mmh):
    P = PARAMS
    if mmh is None:
        return "unknown"
    if mmh < P["rain_light_mmh"]:
        return "none"
    if mmh < P["rain_mod_mmh"]:
        return "light"
    if mmh < P["rain_heavy_mmh"]:
        return "moderate"
    if mmh < P["rain_violent_mmh"]:
        return "heavy"
    return "violent"


def idw(X, stations, P=None, with_near=False):
    """Inverse-distance (power 2) rainfall estimate at points X (N x 2 m) from gauges within rain_max_km.
    -> (mm/h array, nearest-gauge km array, confidence labels[, highest reading (mm/h) of the gauges within rain_count_km])"""
    P = P or PARAMS
    st = [s for s in stations or [] if s.get("mm") is not None and in_sg(s.get("lat"), s.get("lon"))]
    n = len(X)
    if not st or not n:
        return (np.full(n, np.nan), np.full(n, np.inf), ["none"] * n) + ((np.zeros(n),) if with_near else ())
    G = np.array([[s["lon"] * KX, s["lat"] * KY] for s in st])
    v = np.array([s["mm"] * 12.0 for s in st])               # 5-min total -> mm/h
    D = np.hypot(X[:, None, 0] - G[None, :, 0], X[:, None, 1] - G[None, :, 1]) / 1000.0
    near = D.min(1)
    W = np.where(D <= P["rain_max_km"], 1.0 / np.maximum(D, 0.25) ** 2, 0.0)
    ws = W.sum(1)
    est = np.where(ws > 0, (W * v[None, :]).sum(1) / np.where(ws > 0, ws, 1.0), np.nan)
    conf = ["high" if k <= P["rain_hi_conf_km"] else "medium" if k <= P["rain_med_conf_km"] else "low" if k <= P["rain_max_km"] else "none" for k in near]
    if with_near:
        nmax = np.where(D <= P["rain_count_km"], v[None, :], -1.0).max(1)
        return est, near, conf, nmax
    return est, near, conf


def rain_exposure(route, stations, prev_stations=None, P=None):
    """Estimated rainfall along one route. Always an ESTIMATE from gauges, never a road-level observation."""
    P = P or PARAMS
    if not route.ok:
        return None
    S, _, w = route.sample(P["rain_step_m"])
    est, near, conf, nmax = idw(S, stations, P, with_near=True)
    cls = [rain_class(None if np.isnan(e) else float(e)) for e in est]
    known = ~np.isnan(est)
    good = np.array([c in ("high", "medium") for c in conf])
    counted = (nmax >= P["rain_heavy_mmh"]) & np.nan_to_num(est >= P["rain_heavy_mmh"], nan=False)     # a gauge within 2 km itself reads heavy
    wet = np.array([c in ("moderate", "heavy", "violent") for c in cls])
    heavy = np.array([c in ("heavy", "violent") for c in cls])
    tot = float(w.sum()) or 1.0
    km_wet, km_heavy = float(w[wet].sum()) / 1000, float(w[heavy].sum()) / 1000
    km_heavy_good = float(w[heavy & counted].sum()) / 1000
    cover = float(w[known].sum()) / tot
    mean_wet = float(np.nanmean(est[wet])) if wet.any() else 0.0
    trend = "unknown"
    if prev_stations:
        e0, _, _ = idw(S, prev_stations, P)
        a, b = np.nanmean(e0) if np.isfinite(e0).any() else np.nan, np.nanmean(est) if np.isfinite(est).any() else np.nan
        if np.isfinite(a) and np.isfinite(b):
            trend = "rising" if b > a * 1.25 + 0.5 else "easing" if b < a * 0.75 - 0.5 else "steady"
    lvl_conf = "high" if good[wet].all() and wet.any() else ("medium" if wet.any() and good[wet].mean() >= 0.5 else ("low" if wet.any() else "-"))
    segs = []
    if wet.any():                                                         # contiguous wet stretches for the map
        s_m = np.linspace(0.0, route.length, len(S))
        i = 0
        while i < len(S):
            if not wet[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(S) and wet[j + 1]:
                j += 1
            segs.append({"a_m": float(s_m[i]), "b_m": float(s_m[j]) + P["rain_step_m"] / 2, "cls": max((cls[k] for k in range(i, j + 1)), key=lambda c: ["light", "moderate", "heavy", "violent"].index(c) if c in ("light", "moderate", "heavy", "violent") else -1),
                         "mmh": round(float(np.nanmax(est[i:j + 1])), 1)})
            i = j + 1
    return {"km_wet": round(km_wet, 2), "km_heavy": round(km_heavy, 2), "share_wet": round(km_wet * 1000 / tot, 3), "share_heavy": round(km_heavy * 1000 / tot, 3),
            "share_heavy_good": round(km_heavy_good * 1000 / tot, 3), "mean_mmh": round(mean_wet, 1),
            "max_mmh": round(float(np.nanmax(est)), 1) if known.any() else None, "coverage": round(cover, 2), "confidence": lvl_conf,
            "nearest_gauge_km": round(float(np.min(near)), 1) if len(near) else None, "trend": trend, "segments": segs,
            "km_counted": round(km_heavy_good, 2),
            "label": f"ESTIMATED from NEA rain gauges (inverse-distance interpolation), not observed at road level. COUNTED only where a gauge within {P['rain_count_km']:.0f} km of the route reports heavy rain."}


# =========================================================================== traffic: baselines, trend, forecast (explainable)
def baseline(rows, t, holidays=(), P=None):
    """Time-of-day baseline speed from collected history. rows: [(ts, kmh)] for ONE road segment (older than today).
    Uses readings on the same day type, within +-15 min of t's time of day. -> {"kmh", "days", "n", "sigma"} or None when there are
    readings on fewer than hist_min_days different days."""
    P = P or PARAMS
    dt, sl = day_type(t, holidays), slot15(t)
    today = sgt(t).strftime("%Y-%m-%d")
    by_day = {}
    for ts, v in rows or []:
        if v is None or ts >= t - 60:
            continue
        ds = sgt(ts).strftime("%Y-%m-%d")
        if ds == today or day_type(ts, holidays) != dt or abs(slot15(ts) - sl) > 1:
            continue
        by_day.setdefault(ds, []).append(float(v))
    if len(by_day) < P["hist_min_days"]:
        return {"kmh": None, "days": len(by_day), "n": sum(len(v) for v in by_day.values()), "sigma": None}
    med = [statistics.median(v) for v in by_day.values()]
    allv = [x for v in by_day.values() for x in v]
    b = statistics.median(med)
    sig = statistics.pstdev([x - b for x in allv]) if len(allv) > 1 else None
    return {"kmh": round(b, 1), "days": len(by_day), "n": len(allv), "sigma": round(sig, 2) if sig is not None else None}


def trend(series, now, P=None):
    """Least-squares slope (km/h per minute) of the readings in the last trend_window_min. -> (slope, n)"""
    P = P or PARAMS
    pts = [(ts, v) for ts, v in series or [] if v is not None and now - P["trend_window_min"] * 60 <= ts <= now + 1]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 240:
        return 0.0, len(pts)
    x = np.array([(ts - now) / 60.0 for ts, _ in pts])
    y = np.array([v for _, v in pts])
    xm = x.mean()
    den = ((x - xm) ** 2).sum()
    return (float(((x - xm) * (y - y.mean())).sum() / den) if den > 0 else 0.0), len(pts)


def forecast_speed(cur, ff, base_now, base_h, slope, h, sigma=None, P=None):
    """Speed in h minutes: the time-of-day baseline at t+h plus the current anomaly decaying with tau, plus a damped trend.
    -> (mid, lo, hi) km/h. lo-hi is an 80 % interval (+-1.28 sigma)."""
    P = P or PARAMS
    tau = P["tau_min"]
    b0 = base_now if base_now is not None else ff
    bh = base_h if base_h is not None else b0
    dec = math.exp(-h / tau)
    tr = slope * min(h, 15.0)
    cap = 0.5 * max(abs(cur - b0), 0.1 * (ff or b0 or 50.0))
    mid = bh + (cur - b0) * dec + max(-cap, min(cap, tr)) * dec
    top = max(ff or 0, bh or 0) * 1.05
    mid = min(max(3.0, mid), top) if top else max(3.0, mid)
    s = sigma if sigma is not None else P["default_sigma_frac"] * (ff or b0 or 50.0)
    sh = s * math.sqrt(max(0.0, 1.0 - math.exp(-2.0 * h / tau))) + 0.03 * (ff or b0 or 50.0)
    lo, hi = max(3.0, mid - 1.28 * sh), (min(top, mid + 1.28 * sh) if top else mid + 1.28 * sh)
    return round(mid, 1), round(lo, 1), round(hi, 1)


def speed_state(cur, ff, base, closure=False, P=None):
    """Classify a reading against the time-of-day baseline (or free-flow while history builds).
    A road that is normally slow at this time is 'recurring', NOT abnormal."""
    P = P or PARAMS
    if closure:
        return {"state": "closed", "ratio": 0.0, "ref": base or ff, "ref_src": "history" if base else "free-flow", "deterioration_pct": 100}
    if base:
        r = cur / base
        st = "abnormal" if r < P["abnormal_ratio"] else ("below_normal" if r < P["normal_ratio"] else
             ("recurring_slow" if ff and cur / ff < P["slow_ff_ratio"] else "normal"))
        return {"state": st, "ratio": round(r, 2), "ref": base, "ref_src": "history", "deterioration_pct": round(max(0.0, 1 - r) * 100)}
    r = cur / ff if ff else 1.0
    st = "slow_no_history" if r < P["slow_ff_ratio"] else "normal"
    return {"state": st, "ratio": round(r, 2), "ref": ff, "ref_src": "free-flow (no history yet)", "deterioration_pct": round(max(0.0, 1 - r) * 100)}


def jam_growth(hist):
    """Delay growth (s/min) and length growth (m/min) from a jam's own history [(ts, delay_s, length_m)], newest last."""
    h = [x for x in hist or [] if x[1] is not None]
    if len(h) < 2:
        return 0.0, 0.0
    t1, d1, l1 = h[-1]
    old = [x for x in h[:-1] if t1 - x[0] >= 300] or h[:1]
    t0, d0, l0 = old[-1]
    dtm = max(1.0, (t1 - t0) / 60.0)
    return (d1 - d0) / dtm, ((l1 or 0) - (l0 or 0)) / dtm


def jam_forecast(delay_s, rate_s_min, h, P=None):
    """A jam's delay in h minutes (s): growing jams keep growing at a damped rate, others decay with jam_tau_min.
    -> (mid, lo, hi, trend)"""
    P = P or PARAMS
    d0 = max(0.0, delay_s or 0.0)
    if rate_s_min > 0.5:
        mid = d0 + rate_s_min * h * math.exp(-h / 60.0)
        mid = min(mid, 3 * d0 + 300)
        tr = "building"
    else:
        mid = d0 * math.exp(-h / P["jam_tau_min"]) + min(0.0, rate_s_min) * min(h, 10) * 0.5
        mid = max(0.0, mid)
        tr = "easing" if rate_s_min < -0.5 else "steady"
    k = 1.0 - math.exp(-h / 30.0)
    return round(mid), round(mid * (1 - 0.5 * k)), round(mid * (1 + 0.8 * k) + 30 * k), tr


def jam_recovery_min(delay_s, rate_s_min, P=None):
    """Minutes until the jam's delay is expected under 1 min; None while it is still building."""
    P = P or PARAMS
    if rate_s_min > 0.5:
        return None
    if (delay_s or 0) <= 60:
        return 0
    return round(P["jam_tau_min"] * math.log(delay_s / 60.0))


# =========================================================================== service impact and risk (rule-based)
def risk_level(extra_min, hw, blocked=False, heavy_share=0.0, unverified=0, P=None):
    P = P or PARAMS
    hw = hw or 10.0
    if blocked:
        return "critical"
    if extra_min >= max(P["crit_min"], P["crit_hw"] * hw):
        return "critical"
    if extra_min >= max(P["high_min"], P["high_hw"] * hw):
        return "high"
    if extra_min >= P["watch_min"] or heavy_share >= PARAMS["rain_severe_share"] or (unverified and P.get("alert_unverified")):
        return "watch"
    return "info"


def risk_score(extra30, extra60, blocked, heavy_share, unverified, level):
    s = 100.0 if blocked else 0.0
    s += 6.0 * extra30 + 3.0 * extra60 + 40.0 * heavy_share + 6.0 * unverified + 10.0 * RANK.get(level, 0)
    return round(min(100.0, s))


def service_impact(key, route, confirmed, unverified, probes, rain, hw, jam_info, now, horizons=None, hist_rt=None, P=None):
    """Predicted extra running time for one service-direction, per horizon, from VERIFIED evidence only.
    confirmed: [(ev, m)] (status confirmed); unverified: [(ev, m)]; probes: verified-direction probes on this route.
    jam_info: {event id: {"rate", "typical_s", "recurring"}}"""
    P = P or PARAMS
    horizons = horizons or P["horizons"]
    blocked = any(ev["kind"] == "road_closed" and m.get("active") for ev, m in confirmed)
    maybe_blocked = any(ev["kind"] == "flooding" for ev, m in confirmed)
    contrib, intervals = [], []
    jam_iv = [(m["a_m"], m["b_m"]) for ev, m in confirmed if ev["kind"] == "jam" and m.get("a_m") is not None]
    confirmed = sorted(confirmed, key=lambda x: x[0]["kind"] != "jam")
    for ev, m in confirmed:
        inside = False
        if ev["kind"] != "jam" and m.get("a_m") is not None and jam_iv:
            ln = max(1.0, m["b_m"] - m["a_m"])
            inside = sum(max(0.0, min(m["b_m"], y) - max(m["a_m"], x)) for x, y in jam_iv) >= 0.5 * ln
        if ev["kind"] not in ("jam", "accident", "breakdown", "lane_closed", "roadworks", "flooding", "road_closed", "hazard", "other"):
            continue
        share = 1.0
        if ev.get("length_m") and m.get("overlap_m"):
            share = min(1.0, max(0.05, m["overlap_m"] / max(ev["length_m"], m["overlap_m"])))
        ji = jam_info.get(ev["id"], {})
        rate = ji.get("rate", 0.0)
        typical = ji.get("typical_s") or 0.0
        per = {}
        for h in horizons:
            if ev.get("delay_s") is not None:
                mid, lo, hi, tr = jam_forecast(ev["delay_s"], rate, h, P)
            else:
                mid = lo = hi = 0.0
                tr = "unknown"
            ab = max(0.0, mid - typical) if ji.get("recurring") else mid       # recurring congestion is already in the timetable
            per[h] = (ab * share / 60.0, max(0.0, lo - (typical if ji.get("recurring") else 0)) * share / 60.0,
                      max(0.0, hi - (typical if ji.get("recurring") else 0)) * share / 60.0)
            if inside:
                per[h] = (0.0, 0.0, 0.0)
        contrib.append({"kind": ev["kind"], "id": ev["id"], "label": ev.get("label"), "road": ev.get("road") or ev.get("from"), "a_m": m.get("a_m"), "b_m": m.get("b_m"),
                        "share": round(share, 2), "per": per, "trend": tr if ev.get("delay_s") is not None else "unknown",
                        "recurring": ji.get("recurring"), "recovery_min": jam_recovery_min(ev.get("delay_s"), rate, P) if ev.get("delay_s") else None,
                        "basis": "inside a verified jam: its delay is already counted in the jam" if inside else "TomTom delay on the overlapping stretch" + (" minus the usual delay for this time (recurring)" if ji.get("recurring") else
                                 (" (recurrence unknown - history building)" if ji.get("recurring") is None and ev["kind"] == "jam" else ""))})
        if m.get("a_m") is not None:
            intervals.append((m["a_m"], m["b_m"]))
    for pr in probes or []:
        a, b = pr.get("sec_a"), pr.get("sec_b")
        if a is None or b is None or b <= a:
            continue
        cov = sum(max(0.0, min(b, y) - max(a, x)) for x, y in intervals)
        if cov > 0.5 * (b - a):
            continue                                        # a verified jam already explains this stretch
        L = b - a
        ref = pr.get("base_h") or {}
        per = {}
        for h in horizons:
            fh = (pr.get("fc") or {}).get(h)
            if not fh:
                continue
            rv = ref.get(h) or pr.get("base_now") or pr["ff"]
            t_ref = L / max(1.0, rv / 3.6)
            per[h] = tuple(max(0.0, (L / max(1.0, v / 3.6) - t_ref) / 60.0) for v in (fh[0], fh[2], fh[1]))   # mid, lo (fast), hi (slow)
        contrib.append({"kind": "speed", "id": pr["key"], "label": "Slower than normal" if pr.get("state", {}).get("ref_src") == "history" else "Slower than free-flow",
                        "road": pr.get("road") or "", "a_m": a, "b_m": b, "share": 1.0, "per": per, "trend": pr.get("trend_txt", "steady"),
                        "recurring": None, "recovery_min": None, "basis": f"probe speed vs {pr.get('state', {}).get('ref_src', 'reference')}"})
    extra = {}
    for h in horizons:
        mid = sum(c["per"].get(h, (0, 0, 0))[0] for c in contrib)
        lo = sum(c["per"].get(h, (0, 0, 0))[1] for c in contrib)
        hi = sum(c["per"].get(h, (0, 0, 0))[2] for c in contrib)
        extra[h] = {"mid": round(mid, 1), "lo": round(min(lo, mid), 1), "hi": round(max(hi, mid), 1)}
    heavy = (rain or {}).get("share_heavy_good", 0.0) or 0.0
    unv = sum(1 for ev, m in unverified if ev["kind"] in ("road_closed", "accident", "flooding", "breakdown", "lane_closed"))
    unv_risk = unv if P.get("alert_unverified") else 0
    levels = {h: risk_level(extra[h]["mid"], hw, blocked, heavy, unv_risk, P) for h in horizons}
    lvl = max(levels.values(), key=lambda k: RANK[k])
    h30 = 30 if 30 in extra else horizons[0]
    crit = sorted([c for c in contrib if max((v[0] for v in c["per"].values()), default=0) >= 0.5 or c["kind"] in ("road_closed", "flooding")],
                  key=lambda c: -max((v[0] for v in c["per"].values()), default=0))[:5]
    recs = [c["recovery_min"] for c in contrib if c.get("recovery_min") is not None]
    building = any(c["trend"] == "building" for c in contrib)
    pred_rt = {h: round(hist_rt + extra[h]["mid"], 1) for h in horizons} if hist_rt else None
    return {"svc": key[0], "dir": key[1], "level": lvl, "levels": levels, "extra": extra, "blocked": blocked, "maybe_blocked": maybe_blocked,
            "score": risk_score(extra[h30]["mid"], extra[horizons[-1]]["mid"], blocked, heavy, unv_risk, lvl), "hw": hw,
            "rain": rain, "heavy_share": heavy, "unverified": unv, "contrib": contrib, "critical_segments": crit,
            "recovery_min": None if building else (max(recs) if recs else None), "building": building,
            "hist_rt": hist_rt, "pred_rt": pred_rt, "length_m": round(route.length) if route is not None and route.ok else None}


REC = {
    "road_closed": "Road closed on the route: assess a diversion in Diversion Maps; consider holding departures at the interchange (max 8 min, keep the 7-min layover). SC approval required.",
    "flooding": "Possible flooding on the route: verify with cameras / bus captain report; prepare a diversion in Diversion Maps. SC approval required.",
    "jam": "Congestion on the route: check Headway Control for a pre-emptive departure adjustment (max 8 min, departed trips locked); if the gap reaches 2x the headway consider a halfway recovery at an approved halfway point.",
    "accident": "Incident on the route: monitor the delay; prepare Headway Control adjustments. Verify with traffic cameras before acting.",
    "breakdown": "Broken-down vehicle on the route: monitor; brief bus captains if the delay grows.",
    "lane_closed": "Lane closure on the route: expect slower running; watch Bunching & Gap for the services listed.",
    "roadworks": "Road works on the route: expect slower running in the works window; watch Bunching & Gap.",
    "speed": "Abnormal slowdown on the route: check Headway Control for a pre-emptive departure adjustment (max 8 min, departed trips locked).",
    "rain": "Heavy rain estimated over the routes: monitor running time and headways; remind bus captains of wet-weather driving.",
    "unverified": "Unverified report near the route: confirm with traffic cameras or a bus captain report before acting.",
}


def links_for(svc, d, kind):
    q = f"svc={svc}&dir={d}" if svc else ""
    out = []
    if kind in ("road_closed", "flooding"):
        out.append({"label": "Diversion Maps", "href": "/diversion"})
    out += [{"label": "Headway Control", "href": "/control" + (f"?{q}" if q else "")},
            {"label": "Recovery Decision Engine", "href": "/recovery" + (f"?{q}" if q else "")},
            {"label": "Halfway Planner", "href": "/halfway" + (f"?{q}" if q else "")},
            {"label": "Bunching & Gap", "href": "/bunching" + (f"?{q}" if q else "")},
            {"label": "Route Traffic", "href": "/" + (f"?{q}" if q else "")}]
    if kind in ("jam", "speed", "accident", "breakdown", "lane_closed"):
        out.append({"label": "Traffic-Aware Regulation", "href": "/traffic"})
    return out


# =========================================================================== jam history (growth) kept between evaluations
class JamTracker:
    """Recent history of each jam / incident delay, to measure build-up (delay and length growth per minute)."""

    def __init__(self, keep_s=7200):
        self.h = {}
        self.keep = keep_s

    def update(self, events, now):
        for ev in events:
            if ev.get("delay_s") is None:
                continue
            q = self.h.setdefault(ev["id"], deque(maxlen=60))
            if not q or now - q[-1][0] >= 30:
                q.append((now, float(ev["delay_s"]), float(ev.get("length_m") or 0)))
        for k in [k for k, q in self.h.items() if q and now - q[-1][0] > self.keep]:
            del self.h[k]

    def rate(self, eid):
        return jam_growth(list(self.h.get(eid, ())))

    def first_seen(self, eid):
        q = self.h.get(eid)
        return q[0][0] if q else None


def jam_cell(ev):
    """~550 m cell + direction octant of a jam's middle, for recurrence statistics."""
    L = ev.get("_L") or (Line(ev["line"]) if ev.get("line") and len(ev["line"]) >= 2 else None)
    if L is None or not L.ok:
        p = ev.get("pt")
        return f"{round(p[0] * 200)}:{round(p[1] * 200)}:x" if p else None
    mid = L.point_at(L.length / 2)
    i = int(min(len(L.brg) - 1, max(0, np.searchsorted(L.cum, L.length / 2) - 1)))
    return f"{round(mid[0] * 200)}:{round(mid[1] * 200)}:{int(((L.brg[i] + 22.5) % 360) // 45)}"


def simplify(pts, step=25.0, nd=5):
    """[(lat, lon)] -> [[lon, lat]] every ~step m (map payload size)."""
    L = Line(pts)
    if not L.ok:
        return [[round(p[1], nd), round(p[0], nd)] for p in pts]
    S, _, _ = L.sample(step)
    return [[round(x / KX, nd), round(y / KY, nd)] for x, y in S]


def _src_ok(src, *names):
    return any((src.get(n) or {}).get("status") in ("live", "stale") for n in names)


def _src_stale(src, *names):
    return any((src.get(n) or {}).get("status") == "stale" for n in names)


# =========================================================================== the evaluation pipeline (live and demo share it)
def prepare_probes(world, routes, P=None):
    """Verify each probe's direction against its service route, give it a stretch of the route, its baseline and its forecasts."""
    P = P or PARAMS
    now = world["now"]
    hol = world.get("holidays", ())
    by_key = {}
    for pr in world.get("probes") or []:
        key = (pr["svc"], pr["dir"])
        R = routes.get(key)
        if R is None or not R.ok:
            continue
        pr = dict(pr)
        a = float(R.locate(to_xy(pr["lat"], pr["lon"]))[1][0])
        pr["a_m"] = a
        if pr.get("line") and len(pr["line"]) >= 2:
            m = match_line(R, Line(pr["line"]), dict(P, min_overlap_m=min(P["min_overlap_m"], 20.0)))
            pr["dir_ok"] = m["status"] == "confirmed"
            pr["dir_txt"] = m["explain"]
        else:
            pr["dir_ok"] = None
            pr["dir_txt"] = "The provider returned no segment geometry: direction not verified (shown, not used for delays)."
        b0 = baseline(pr.get("hist"), now, hol, P)
        pr["base_info"] = b0
        pr["base_now"] = b0["kmh"]
        pr["base_h"] = {h: baseline(pr.get("hist"), now + h * 60, hol, P)["kmh"] for h in P["horizons"]}
        slope, n = trend(pr.get("series"), now, P)
        pr["slope"] = round(slope, 2)
        pr["trend_txt"] = "worsening" if slope < -0.4 else "recovering" if slope > 0.4 else "steady"
        pr["state"] = speed_state(pr["cur"], pr["ff"], pr["base_now"], pr.get("closure"), P)
        sig = b0.get("sigma")
        pr["fc"] = {h: forecast_speed(pr["cur"], pr["ff"], pr["base_now"], pr["base_h"][h], slope, h, sig, P) for h in P["horizons"]}
        ab = 0
        for ts, v in sorted(pr.get("series") or [], reverse=True):              # persistence: how long it has been abnormal
            ref = pr["base_now"] or pr["ff"]
            if v is not None and ref and v / ref < (P["abnormal_ratio"] if pr["base_now"] else P["slow_ff_ratio"]):
                ab = (now - ts) / 60.0
            else:
                break
        pr["persist_min"] = round(ab)
        by_key.setdefault(key, []).append(pr)
    for key, lst in by_key.items():                                             # each verified probe stands for a stretch of the route
        R = routes[key]
        lst.sort(key=lambda p: p["a_m"])
        for i, p in enumerate(lst):
            lo = (lst[i - 1]["a_m"] + p["a_m"]) / 2 if i else max(0.0, p["a_m"] - 1500)
            hi = (lst[i + 1]["a_m"] + p["a_m"]) / 2 if i + 1 < len(lst) else min(R.length, p["a_m"] + 1500)
            p["sec_a"], p["sec_b"] = max(lo, p["a_m"] - 1500), min(hi, p["a_m"] + 1500)
    return by_key


def evaluate(world, P=None):
    """world -> the whole Forecast Centre state (KPIs, map layers, services, alert candidates, horizon comparison).
    Everything is computed from the inputs; nothing is invented when a source is missing (the KPI says Unavailable)."""
    P = P or PARAMS
    now = world["now"]
    H = P["horizons"]
    src = world.get("sources") or {}
    idx = world.get("index")
    routes = dict(idx.lines) if idx is not None else {}
    for k, v in (world.get("routes") or {}).items():
        routes.setdefault(k, v if isinstance(v, Line) else Line(v))
    hw = world.get("headways") or {}
    jt = world.get("jam_info") or {}

    # ---- 1. events: group duplicates, validity, strict matching against every service line near them
    events = group_events(world.get("events") or [], P)
    conf_by, unv_by, ev_out = {}, {}, []
    for ev in events:
        act, why = event_active(ev, now, H[-1])
        ev["active"], ev["validity_txt"] = act, why
        if not act:
            continue
        res = idx.match(ev, now, H[-1], P) if idx is not None and len(idx) else {"confirmed": [], "unverified": [], "rejected": []}
        ev["_res"] = res
        for k, m in res["confirmed"]:
            conf_by.setdefault(k, []).append((ev, m))
        for k, m in res["unverified"]:
            unv_by.setdefault(k, []).append((ev, m))
        ev_out.append(ev)

    # ---- 2. probes (verified direction only count)
    probes_by = prepare_probes(world, routes, P)

    # ---- 3. rainfall (estimated)
    rain = world.get("rain") or {}
    stations = rain.get("stations") or []
    prev = rain.get("prev") or None
    max_mmh = max([s["mm"] * 12 for s in stations if s.get("mm") is not None] or [0.0])
    rain_by = {}
    if stations and routes:
        if max_mmh >= P["rain_mod_mmh"]:
            wet = [s for s in stations if (s.get("mm") or 0) * 12 >= P["rain_mod_mmh"]]
            G = np.array([[s["lon"] * KX, s["lat"] * KY] for s in wet])
            keys = idx.candidates(G, P["rain_max_km"] * 1000) if idx is not None else list(routes)
            for k in keys:
                if k in routes:
                    rain_by[k] = rain_exposure(routes[k], stations, prev, P)

    # ---- 4. service impact for every service with verified evidence, probes, focus or rain exposure
    focus = set(tuple(x) for x in world.get("focus") or [])
    keys = set(conf_by) | set(probes_by) | {k for k in focus if k in routes} | {k for k, r in rain_by.items() if r and r["share_heavy_good"] >= P["rain_severe_share"]}
    if P.get("alert_unverified"):
        keys |= set(unv_by)
    impacts = {}
    for k in keys:
        R = routes.get(k)
        if R is None:
            continue
        rx = rain_by.get(k)
        if rx is None and k in focus and stations:
            rx = rain_exposure(R, stations, prev, P)
        good_probes = [p for p in probes_by.get(k, []) if p.get("dir_ok")]
        impacts[k] = service_impact(k, R, conf_by.get(k, []), unv_by.get(k, []), good_probes, rx, hw.get(k), jt, now, H,
                                    (world.get("hist_rt") or {}).get(k), P)
        impacts[k]["hw_src"] = "scheduled" if hw.get(k) else "assumed 10 min (no headway on record)"
        impacts[k]["probes"] = probes_by.get(k, [])

    # ---- 5. per-event view (what each event does to the services it really touches)
    ev_view = []
    for ev in ev_out:
        res = ev["_res"]
        per_svc = []
        for k, m in res["confirmed"]:
            im = impacts.get(k)
            c = next((c for c in (im or {}).get("contrib", []) if c["id"] == ev["id"]), None)
            per_svc.append({"svc": k[0], "dir": k[1], "a_m": m.get("a_m"), "b_m": m.get("b_m"), "overlap_m": m.get("overlap_m"),
                            "explain": m["explain"], "extra": {h: round(c["per"].get(h, (0, 0, 0))[0], 1) for h in H} if c else {h: 0.0 for h in H},
                            "hw": hw.get(k)})
        per_svc.sort(key=lambda x: -x["extra"].get(30, 0))
        fc = {}
        rate = jt.get(ev["id"], {}).get("rate", 0.0)
        if ev.get("delay_s") is not None:
            for h in H:
                mid, lo, hi, tr = jam_forecast(ev["delay_s"], rate, h, P)
                fc[h] = {"mid": mid, "lo": lo, "hi": hi, "trend": tr}
        lvl = "info"
        for s in per_svc:
            for h in H:
                lv = risk_level(s["extra"][h], s["hw"], ev["kind"] == "road_closed", 0, 0, P)
                if RANK[lv] > RANK[lvl]:
                    lvl = lv
        related = []
        if ev["kind"] == "jam" and per_svc:
            for other in ev_out:
                if other is ev or other["kind"] == "jam":
                    continue
                for k, m in res["confirmed"]:
                    om = next((mm for kk, mm in (other.get("_res") or {}).get("confirmed", []) if kk == k), None) if other.get("_res") else None
                    if om and om.get("a_m") is not None and m.get("a_m") is not None and om["a_m"] <= m["b_m"] and om["b_m"] >= m["a_m"]:
                        related.append(f"{other.get('label')} ({' + '.join(other.get('sources') or [other['source']])})")
                        break
        status = "confirmed" if per_svc else ("unverified" if res["unverified"] else "no_route_impact")
        if status == "unverified" and P.get("alert_unverified") and ev["kind"] in ("road_closed", "accident", "flooding", "breakdown", "lane_closed"):
            lvl = "watch"
        rej = [{"svc": k[0], "dir": k[1], "reason": m["reason"], "explain": m["explain"]} for k, m in res["rejected"]
               if m["reason"] in ("opposite_direction", "parallel_road", "crossing", "too_short", "not_in_force")][:12]
        ev_view.append({
            "id": ev["id"], "kind": ev["kind"], "label": ev.get("label"), "desc": ev.get("desc"), "road": ev.get("road"), "from": ev.get("from"),
            "to": ev.get("to"), "towards": ev.get("towards") or ev.get("to"), "geom": ev["geom"], "sources": ev.get("sources") or [ev["source"]],
            "line": simplify(ev["line"]) if ev.get("line") else None, "pt": [round(ev["pt"][1], 6), round(ev["pt"][0], 6)] if ev.get("pt") else None,
            "status": status, "level": lvl, "services": per_svc,
            "unverified_services": [{"svc": k[0], "dir": k[1], "explain": m["explain"]} for k, m in res["unverified"]][:20],
            "rejected": rej, "n_rejected": len(res["rejected"]), "delay_s": ev.get("delay_s"), "length_m": ev.get("length_m"),
            "magnitude": ev.get("magnitude"), "fc": fc, "passable": ev.get("passable"), "pass_text": ev.get("pass_text"),
            "validity": ev.get("validity_txt"), "start": ev.get("start"), "end": ev.get("end"),
            "recurring": (jt.get(ev["id"]) or {}).get("recurring"), "recur_detail": (jt.get(ev["id"]) or {}).get("detail"),
            "first_seen": (jt.get(ev["id"]) or {}).get("first_seen"), "growth": round(rate, 1) if rate else 0.0, "src_key": ev.get("src_key") or "tomtom_incidents",
            "recovery_min": jam_recovery_min(ev.get("delay_s"), rate, P) if ev.get("delay_s") else None, "related": sorted(set(related)),
        })
    rw_total = sum(1 for e in ev_out if e["kind"] == "roadworks")
    rw_near = sum(1 for e in ev_view if e["kind"] == "roadworks" and e["status"] == "unverified")
    rw_ver = sum(1 for e in ev_view if e["kind"] == "roadworks" and e["status"] == "confirmed")
    ev_view = [e for e in ev_view if not (e["kind"] == "roadworks" and e["status"] == "no_route_impact")]     # far from every route: counted, not sent
    for ev in ev_out:
        ev.pop("_res", None)
    ev_view.sort(key=lambda e: (-RANK[e["level"]], e["status"] != "confirmed", -(e["delay_s"] or 0)))

    # ---- 6. congestion zones (TomTom jams grouped where they touch)
    jams = [e for e in ev_out if e["kind"] == "jam" and e.get("line")]
    zones = []
    for e in jams:
        L = e.get("_L") or Line(e["line"])
        hit = None
        for z in zones:
            if any(float(np.min(Line(o["line"]).locate(L.sample(100)[0], pad=400)[0])) <= 300 for o in z["members"]):
                hit = z
                break
        if hit is None:
            zones.append({"members": [e]})
        else:
            hit["members"].append(e)
    zone_out = []
    for z in zones:
        mm = z["members"]
        mags = [m.get("mag") or 0 for m in mm]
        evv = [x for x in ev_view if x["id"] in {m["id"] for m in mm}]
        if max(mags) < 2 and not any(x["recurring"] is False for x in evv):
            continue
        pts = [p for m in mm for p in m["line"]]
        zone_out.append({"n": len(mm), "lat": round(sum(p[0] for p in pts) / len(pts), 5), "lon": round(sum(p[1] for p in pts) / len(pts), 5),
                         "km": round(sum((m.get("length_m") or 0) for m in mm) / 1000, 1), "max_mag": max(mags), "delay_min": round(max((m.get("delay_s") or 0) for m in mm) / 60, 1),
                         "roads": sorted({m.get("road") or m.get("from") or "" for m in mm} - {""})[:3],
                         "services": len({(s["svc"], s["dir"]) for x in evv for s in x["services"]}),
                         "event_ids": [x["id"] for x in evv],
                         "abnormal": any(x["recurring"] is False for x in evv), "recurring": all(x["recurring"] for x in evv) if evv else None})

    # ---- 7. services ranked
    svc_list = sorted(impacts.values(), key=lambda s: (-RANK[s["level"]], -s["score"]))
    affected = [s for s in svc_list if any(c["kind"] != "speed" for c in s["contrib"]) or s["extra"][H[0]]["mid"] >= P["watch_min"]]

    # ---- 8. KPIs (Unavailable when the data needed is not there)
    def kpi(id_, label, value, sub, ok, stale=False, basis="", unit="", level=None):
        return {"id": id_, "label": label, "value": value if ok else None, "sub": sub if ok else (sub or "Source unavailable"),
                "available": bool(ok), "stale": bool(stale and ok), "basis": basis, "unit": unit, "level": level}
    tt_inc = _src_ok(src, "tomtom_incidents", "tomtom_tiles", "waze")
    have_idx = idx is not None and len(idx) > 0
    lta_unv = sum(1 for e in ev_view if e["status"] == "unverified" and e["kind"] not in ("jam", "roadworks"))
    ver_inc = [e for e in ev_view if e["status"] == "confirmed" and e["kind"] not in ("jam", "roadworks")]
    kpis = [
        kpi("verified_incidents", "Verified route incidents", len(ver_inc),
            f"{lta_unv} unverified report(s) near routes - not counted" if lta_unv else "only events whose own line runs along a route", tt_inc and have_idx,
            _src_stale(src, "tomtom_incidents", "tomtom_tiles"), "Incidents whose own line runs along a bus route in the same direction (TomTom / Waze geometry)",
            level="critical" if any(e["kind"] == "road_closed" for e in ver_inc) else ("high" if ver_inc else None)),
        kpi("services_affected", "Bus services potentially affected", len({s["svc"] for s in affected}),
            f"{len(affected)} service-directions, verified matches only", (tt_inc or _src_ok(src, "tomtom_flow")) and have_idx,
            _src_stale(src, "tomtom_incidents", "tomtom_flow"), "Distinct services with a verified route match or a predicted extra delay >= 2 min"),
        kpi("congestion_zones", "Congestion risk zones", len(zone_out),
            f"{sum(1 for z in zone_out if z['abnormal'])} abnormal for the time of day" if any(z["abnormal"] for z in zone_out) else
            ("recurrence history building" if zone_out and all(z["recurring"] is None for z in zone_out) else f"{len(jams)} jams island-wide"),
            tt_inc, _src_stale(src, "tomtom_incidents", "tomtom_tiles"), "Jams of moderate+ magnitude (or abnormal for the time), grouped where they touch (TomTom / Waze)"),
        kpi("predicted_disruptions", "Predicted service disruptions", sum(1 for s in svc_list if RANK[s["level"]] >= RANK["high"]),
            f"{sum(1 for s in svc_list if s['level'] == 'critical')} critical within 60 min", (tt_inc or _src_ok(src, "tomtom_flow")) and have_idx,
            _src_stale(src, "tomtom_incidents", "tomtom_flow"), "Service-directions at HIGH or CRITICAL risk at +15/+30/+60 min (rule-based)"),
        kpi("rain_exposure", "Severe rainfall exposure", sum(1 for k, r in rain_by.items() if r and r["share_heavy_good"] >= P["rain_severe_share"]),
            (f"est. - {sum(1 for s in stations if (s.get('mm') or 0) > 0)} of {len(stations)} gauges wet, max {max_mmh:.0f} mm/h" if stations else ""),
            _src_ok(src, "nea_rain") and have_idx, _src_stale(src, "nea_rain"),
            f"ESTIMATED: service-directions with >= {int(P['rain_severe_share'] * 100)} % of the route in heavy rain (>= {P['rain_heavy_mmh']:.0f} mm/h), counted only within {P['rain_count_km']:.0f} km of a gauge reporting it"),
        kpi("roadworks", "Roadwork restrictions", rw_ver,
            f"{rw_near} near routes (LTA points, not counted) - {rw_total:,} in force island-wide",
            _src_ok(src, "lta_roadworks", "tomtom_incidents", "tomtom_tiles"), _src_stale(src, "lta_roadworks"),
            "Road works whose own line runs along a bus route (TomTom). LTA approved works give no road line, so they are listed, not counted."),
    ]
    cal = world.get("calibration") or {}
    if cal.get("n", 0) >= 30:
        kpis.append(kpi("model_confidence", "Forecast model confidence", round(cal["coverage"] * 100), f"measured: {cal['n']} checks, MAE {cal['mae']:.1f} km/h", True,
                        basis="Share of later observations that fell inside the forecast's 80 % band (last 7 days)", unit="%"))
    else:
        kpis.append({"id": "model_confidence", "label": "Forecast model confidence", "value": None, "available": True, "stale": False, "unit": "",
                     "text": "Not measured yet", "sub": f"rule-based - {cal.get('n', 0)} of 30 checks collected" if not world.get("demo") else "DEMO - not a measured accuracy",
                     "basis": "Accuracy is shown only once forecasts have been checked against later observations", "level": None})

    # ---- 9. horizon comparison
    comp = []
    fc2h = world.get("nea_2h") or {}
    for h in H:
        lv = [s["levels"][h] for s in svc_list]
        tot = sum(s["extra"][h]["mid"] for s in svc_list)
        worst = sorted(svc_list, key=lambda s: -s["extra"][h]["mid"])[:3]
        comp.append({"h": h, "critical": lv.count("critical"), "high": lv.count("high"), "watch": lv.count("watch"),
                     "extra_total": round(tot, 1), "worst": [{"svc": s["svc"], "dir": s["dir"], "extra": s["extra"][h]} for s in worst if s["extra"][h]["mid"] > 0],
                     "jams_building": sum(1 for e in ev_view if e["kind"] == "jam" and (e["fc"].get(h) or {}).get("trend") == "building"),
                     "rain_note": (f"NEA 2-h forecast: rain in {sum(1 for a in fc2h.get('areas', []) if a['rain'])} of {len(fc2h.get('areas', []))} areas"
                                   if fc2h.get("areas") and h >= 30 else ("persistence of current gauge readings" if stations else "rain data unavailable"))})

    # ---- 10. alert candidates
    cands = []
    for e in ev_view:
        if e["status"] == "confirmed" and RANK[e["level"]] >= RANK["watch"] or (e["status"] == "confirmed" and e["kind"] in ("road_closed", "flooding")):
            top = e["services"][0] if e["services"] else None
            d = {h: max((s["extra"][h] for s in e["services"]), default=0.0) for h in H}
            hz = next((h for h in H if risk_level(d[h], top and top["hw"], e["kind"] == "road_closed", 0, 0, P) == e["level"]), H[0])
            cands.append({"key": "ev:" + e["id"], "kind": e["kind"], "level": e["level"], "verified": True,
                          "title": f"{e['label'] if e.get('label') and e['label'] != 'Unknown' else 'Traffic disruption'} - {e['road'] or e['from'] or ('service ' + str(top['svc']) + ' dir ' + str(top['dir']) if top else 'matched route segment')}" + (f" towards {e['towards']}" if e.get("towards") else ""),
                          "location": " - ".join(x for x in (e.get("from"), e.get("to")) if x) or (e.get("road") or "Road name not supplied by source"),
                          "segment": f"route km {top['a_m'] / 1000:.1f}-{top['b_m'] / 1000:.1f} of {top['svc']} dir {top['dir']}" if top and top.get("a_m") is not None else "",
                          "services": [{"svc": s["svc"], "dir": s["dir"], "extra30": s["extra"].get(30, 0)} for s in e["services"]],
                          "direction": f"towards {e['towards']}" if e.get("towards") else "direction of the TomTom line",
                          "delay": d, "horizon": hz,
                          "observed": (f"{e['label']}: TomTom delay {round((e['delay_s'] or 0) / 60)} min over {round((e['length_m'] or 0) / 100) / 10} km now" if e.get("delay_s") is not None else f"{e['label']} reported by {' + '.join(e['sources'])}")
                                      + (f"; includes {', '.join(e['related'])}" if e.get("related") else ""),
                          "predicted": {h: e["fc"].get(h) for h in H} if e["fc"] else None,
                          "confidence": "Verified route match (line geometry + direction)" + ("; recurring for this time" if e["recurring"] else "; abnormal for this time" if e["recurring"] is False else "; recurrence not known yet"),
                          "source": " + ".join(e["sources"]), "deps": [e.get("src_key") or "tomtom_incidents"], "recommended": REC.get(e["kind"], REC["accident"]),
                          "links": links_for(top["svc"], top["dir"], e["kind"]) if top else links_for(None, None, e["kind"]),
                          "explain": [s["explain"] for s in e["services"][:3]] + [r["explain"] for r in e["rejected"][:3]],
                          "lat": e["pt"][1] if e.get("pt") else None, "lon": e["pt"][0] if e.get("pt") else None, "score": max((s["extra"].get(30, 0) for s in e["services"]), default=0)})
        elif e["status"] == "unverified" and P.get("alert_unverified") and e["kind"] in ("road_closed", "accident", "flooding", "breakdown", "lane_closed"):
            cands.append({"key": "uv:" + e["id"], "kind": e["kind"], "level": "watch", "verified": False,
                          "title": f"UNVERIFIED {e['label']} - {e['road'] or 'near a bus route'}" + (f" towards {e['towards']}" if e.get("towards") else ""),
                          "location": e.get("desc") or "", "segment": "", "services": [{"svc": s["svc"], "dir": s["dir"], "extra30": None} for s in e["unverified_services"]],
                          "direction": f"towards {e['towards']} (from the message, not verified)" if e.get("towards") else "not given by the source",
                          "delay": {h: None for h in H}, "horizon": None, "observed": f"Point report by {' + '.join(e['sources'])}", "predicted": None,
                          "confidence": "UNVERIFIED - point only, no line or direction: not counted as a confirmed affected service",
                          "source": " + ".join(e["sources"]), "deps": ["lta_incidents"], "recommended": REC["unverified"], "links": links_for(None, None, e["kind"]),
                          "explain": [s["explain"] for s in e["unverified_services"][:3]], "lat": e["pt"][1] if e.get("pt") else None,
                          "lon": e["pt"][0] if e.get("pt") else None, "score": 0})
    for s in svc_list:
        sp = [c for c in s["contrib"] if c["kind"] == "speed"]
        if not sp:
            continue
        d = {h: round(sum(c["per"].get(h, (0, 0, 0))[0] for c in sp), 1) for h in H}
        lv = max((risk_level(d[h], s["hw"], False, 0, 0, P) for h in H), key=lambda k: RANK[k])
        if RANK[lv] < RANK["watch"]:
            continue
        hz = next((h for h in H if risk_level(d[h], s["hw"], False, 0, 0, P) == lv), H[0])
        c0 = max(sp, key=lambda c: c["per"].get(30, (0,))[0])
        cands.append({"key": f"sp:{s['svc']}:{s['dir']}", "kind": "speed", "level": lv, "verified": True,
                      "title": f"Abnormal slowdown - service {s['svc']} dir {s['dir']}", "location": c0.get("road") or "",
                      "segment": f"route km {c0['a_m'] / 1000:.1f}-{c0['b_m'] / 1000:.1f}", "services": [{"svc": s["svc"], "dir": s["dir"], "extra30": d.get(30)}],
                      "direction": f"dir {s['dir']} (probe direction verified)", "delay": d, "horizon": hz,
                      "observed": c0["basis"], "predicted": None, "confidence": "Probe speed, direction verified; " + ("history baseline" if "history" in c0["basis"] else "free-flow reference (history building)"),
                      "source": "TomTom Flow", "deps": ["tomtom_flow"], "recommended": REC["speed"], "links": links_for(s["svc"], s["dir"], "speed"),
                      "explain": [c0["basis"]], "lat": None, "lon": None, "score": d.get(30, 0)})
    wet_keys = [k for k, r in rain_by.items() if r and r["share_heavy_good"] >= P["rain_severe_share"]]
    if wet_keys:
        top = max((s for s in stations if s.get("mm") is not None), key=lambda s: s["mm"])
        lv = "high" if max_mmh >= P["rain_violent_mmh"] else "watch"
        cands.append({"key": f"rain:{round(top['lat'] * 20)}:{round(top['lon'] * 20)}", "kind": "rain", "level": lv, "verified": False,
                      "title": f"Heavy rain (estimated) over {len({k[0] for k in wet_keys})} services", "location": f"near {top.get('name') or 'gauge ' + str(top.get('id'))}",
                      "segment": "", "services": [{"svc": k[0], "dir": k[1], "extra30": None, "share": rain_by[k]["share_heavy_good"]} for k in sorted(wet_keys, key=lambda k: -rain_by[k]["share_heavy_good"])][:30],
                      "direction": "all directions", "delay": {h: None for h in H}, "horizon": 15, "observed": f"max {max_mmh:.0f} mm/h at a gauge",
                      "predicted": None, "confidence": "ESTIMATED exposure from gauges - not road-level", "source": "NEA", "deps": ["nea_rain"],
                      "recommended": REC["rain"], "links": links_for(wet_keys[0][0], wet_keys[0][1], "rain"), "explain": [rain_by[wet_keys[0]]["label"]],
                      "lat": top["lat"], "lon": top["lon"], "score": 0})

    return {"mode": "demo" if world.get("demo") else "live", "now": now, "time": sgt(now).isoformat(timespec="seconds"), "engine": ENGINE, "method": METHOD,
            "horizons": H, "kpis": kpis, "events": ev_view, "zones": zone_out, "services": [_svc_brief(s) for s in svc_list[:40]],
            "impacts": impacts, "comparison": comp, "cands": cands,
            "probes": [_probe_brief(p) for lst in probes_by.values() for p in lst],
            "rain": {"stations": [{"id": s.get("id"), "name": s.get("name"), "lat": s["lat"], "lon": s["lon"], "mmh": round((s.get("mm") or 0) * 12, 1),
                                   "cls": rain_class((s.get("mm") or 0) * 12)} for s in stations],
                     "max_mmh": round(max_mmh, 1), "wet": sum(1 for s in stations if (s.get("mm") or 0) > 0), "areas": fc2h.get("areas", []),
                     "label": "Gauge readings (observed at the gauge). Route exposure is ESTIMATED."},
            "sources": src, "calibration": cal, "_ev": ev_out,
            "network": {"service_dirs": len(idx) if idx is not None else 0, "gauges": len(stations), "affected_dirs": len(affected),
                        "unverified_reports": lta_unv, "roadworks_verified": rw_ver, "roadworks_near": rw_near, "roadworks_total": rw_total,
                        "zones_abnormal": sum(1 for z in zone_out if z["abnormal"])}}


def _svc_brief(s):
    return {"svc": s["svc"], "dir": s["dir"], "level": s["level"], "levels": s["levels"], "score": s["score"], "extra": s["extra"], "blocked": s["blocked"],
            "maybe_blocked": s["maybe_blocked"], "heavy_share": s["heavy_share"], "unverified": s["unverified"], "recovery_min": s["recovery_min"],
            "building": s["building"], "hw": s["hw"], "hw_src": s.get("hw_src"), "hist_rt": s["hist_rt"], "pred_rt": s["pred_rt"],
            "causes": sorted({c["label"] or c["kind"] for c in s["contrib"]})[:4]}


def _probe_brief(p):
    return {"key": p["key"], "svc": p["svc"], "dir": p["dir"], "lat": p["lat"], "lon": p["lon"], "road": p.get("road") or "", "cur": p["cur"], "ff": p["ff"],
            "conf": p.get("conf"), "base": p["base_now"], "base_days": (p.get("base_info") or {}).get("days"), "state": p["state"], "fc": p["fc"],
            "slope": p["slope"], "trend": p["trend_txt"], "persist_min": p["persist_min"], "dir_ok": p.get("dir_ok"), "dir_txt": p.get("dir_txt"),
            "a_m": round(p["a_m"]), "sec": [round(p.get("sec_a") or 0), round(p.get("sec_b") or 0)], "ts": p.get("ts"), "provider": p.get("provider", "tomtom")}


def service_view(state, world, key, P=None):
    """Service intelligence for one service-direction: route, stops, verified / unverified / rejected events with the reason for each,
    probes, estimated rain exposure, predicted extra running time, critical stretches and recovery."""
    P = P or PARAMS
    H = P["horizons"]
    idx = world.get("index")
    R = (idx.lines.get(key) if idx is not None else None) or (Line(world["routes"][key]) if key in (world.get("routes") or {}) else None)
    if R is None or not R.ok:
        return {"svc": key[0], "dir": key[1], "error": "No route geometry for this service-direction."}
    now = world["now"]
    im = state["impacts"].get(key)
    if im is None:
        rx = rain_exposure(R, (world.get("rain") or {}).get("stations") or [], (world.get("rain") or {}).get("prev"), P) if (world.get("rain") or {}).get("stations") else None
        im = service_impact(key, R, [], [], [], rx, (world.get("headways") or {}).get(key), world.get("jam_info") or {}, now, H, (world.get("hist_rt") or {}).get(key), P)
        im["probes"] = []
        im["hw_src"] = "scheduled" if (world.get("headways") or {}).get(key) else "assumed 10 min (no headway on record)"
    ver, unv, rej = [], [], []
    for ev in state.get("_ev") or []:
        m = match_event(R, ev, now, H[-1], P)
        item = {"id": ev["id"], "kind": ev["kind"], "label": ev.get("label"), "road": ev.get("road"), "from": ev.get("from"), "to": ev.get("to"),
                "sources": ev.get("sources") or [ev["source"]], "explain": m["explain"], "reason": m.get("reason"), "a_m": m.get("a_m"), "b_m": m.get("b_m"),
                "delay_s": ev.get("delay_s"), "pass_text": ev.get("pass_text"), "validity": ev.get("validity_txt")}
        if m["status"] == "confirmed":
            item["line"] = simplify(R.cut(m["a_m"], m["b_m"]), 20) if m.get("b_m", 0) > m.get("a_m", 0) else None
            ver.append(item)
        elif m["status"] == "unverified":
            unv.append(item)
        elif m["status"] == "rejected":
            rej.append(item)
    stops = []
    for s in (world.get("stops") or {}).get(key, []):
        a = float(R.locate(to_xy(s["lat"], s["lon"]))[1][0])
        stops.append({"code": s.get("code"), "name": s.get("name"), "lat": s["lat"], "lon": s["lon"], "km": round(a / 1000, 2)})
    rain = im.get("rain")
    if rain:
        rain = dict(rain, segments=[dict(sg, line=simplify(R.cut(sg["a_m"], min(R.length, sg["b_m"])), 30)) for sg in rain.get("segments", [])])
    crit = [dict(c, per={h: round(v[0], 1) for h, v in c["per"].items()},
                 line=simplify(R.cut(c["a_m"], c["b_m"]), 20) if c.get("a_m") is not None and c.get("b_m") and c["b_m"] > c["a_m"] else None)
            for c in im["critical_segments"]]
    sections = []
    for p in im.get("probes") or []:
        sections.append(dict(_probe_brief(p), line=simplify(R.cut(p["sec_a"], p["sec_b"]), 25) if p.get("sec_b", 0) > p.get("sec_a", 0) else None))
    first, last = (stops[0]["name"], stops[-1]["name"]) if len(stops) >= 2 else ("", "")
    return {"svc": key[0], "dir": key[1], "toward": last, "from": first, "length_km": round(R.length / 1000, 1),
            "line": simplify(R.pts, 30), "stops": stops, "level": im["level"], "levels": im["levels"], "extra": im["extra"], "score": im["score"],
            "blocked": im["blocked"], "maybe_blocked": im["maybe_blocked"], "hw": im["hw"], "hw_src": im.get("hw_src"),
            "hist_rt": im["hist_rt"], "pred_rt": im["pred_rt"], "recovery_min": im["recovery_min"], "building": im["building"],
            "verified": ver, "unverified": unv, "rejected": rej[:25], "sections": sections, "rain": rain, "critical": crit,
            "links": links_for(key[0], key[1], "jam"), "method": METHOD, "mode": state["mode"],
            "rt_note": None if im["hist_rt"] else "Historical running time unavailable: this service is not (yet) measured by Running Time Analytics."}


def public_state(state):
    """Strip internal fields before sending the state to the browser."""
    return {k: v for k, v in state.items() if k not in ("impacts", "cands", "_ev")}


# =========================================================================== alert book
class AlertBook:
    """Smart OCC alert queue: ranked, grouped, no repeats for unchanged conditions, re-alert on material worsening, acknowledgement,
    verified clearance (only with FRESH data), escalation of persistent critical alerts, full audit trail."""

    def __init__(self, P=None, store=None, now=None):
        self.P = P or PARAMS
        self.alerts = {}
        self.audit = deque(maxlen=5000)
        self.store = store
        if store is not None:
            for a in store.load_alerts((now or time.time()) - 86400):
                self.alerts[a["id"]] = a

    def _log(self, a, action, now, by="system", detail=""):
        rec = {"ts": now, "alert_id": a["id"], "action": action, "by": by, "level": a["level"], "detail": detail, "title": a.get("title")}
        self.audit.append(rec)
        a.setdefault("history", []).append({"ts": now, "action": action, "by": by, "level": a["level"], "detail": detail})
        a["history"] = a["history"][-40:]
        if self.store is not None:
            self.store.audit(rec)

    @staticmethod
    def _d30(c):
        d = c.get("delay") or {}
        v = d.get(30, d.get("30"))
        return v or 0.0

    def update(self, cands, now, fresh):
        P = self.P
        notes, seen, changed = [], set(), set()
        for c in cands:
            aid = stable_id(c["key"])
            seen.add(aid)
            a = self.alerts.get(aid)
            snap = {k: c.get(k) for k in ("kind", "title", "location", "segment", "services", "direction", "delay", "horizon", "observed", "predicted",
                                           "confidence", "source", "recommended", "links", "explain", "lat", "lon", "verified", "score", "deps")}
            if a is None or (a["status"] == "resolved" and now - (a.get("resolved_at") or 0) > P["reopen_min"] * 60):
                a = dict(snap, id=aid, key=c["key"], level=c["level"], peak=c["level"], status="active", first=now, last=now, missed=0, stale=False,
                         acked=None, escalated=False, realerts=0, crit_since=now if c["level"] == "critical" else None,
                         notice={"level": c["level"], "d30": self._d30(c), "n": len(c.get("services") or [])}, history=[])
                self.alerts[aid] = a
                self._log(a, "raised", now, detail=c.get("observed") or "")
                notes.append({"id": aid, "why": "new", "level": a["level"], "title": a["title"]})
                changed.add(aid)
                continue
            reopened = a["status"] == "resolved"
            if reopened:
                a.update(status="active", resolved_at=None, acked=None)
                self._log(a, "reopened", now, detail="condition returned")
                notes.append({"id": aid, "why": "reopened", "level": c["level"], "title": c["title"]})
            old = a["level"]
            n0 = a["notice"]
            d30 = self._d30(c)
            worse = []
            if RANK[c["level"]] > RANK[n0["level"]]:
                worse.append(f"level {n0['level']} -> {c['level']}")
            if d30 >= n0["d30"] + max(P["worse_min"], P["worse_frac"] * n0["d30"]) and d30 > 0:
                worse.append(f"30-min delay {n0['d30']:.0f} -> {d30:.0f} min")
            if len(c.get("services") or []) >= n0["n"] + P["worse_services"]:
                worse.append(f"services {n0['n']} -> {len(c.get('services') or [])}")
            a.update(snap)
            a.update(level=c["level"], last=now, missed=0, stale=False)
            if RANK[c["level"]] > RANK[a["peak"]]:
                a["peak"] = c["level"]
            if c["level"] == "critical" and old != "critical":
                a["crit_since"] = now
            elif c["level"] != "critical":
                a["crit_since"] = None
            if worse and not reopened:
                a["realerts"] += 1
                a["notice"] = {"level": c["level"], "d30": d30, "n": len(c.get("services") or [])}
                if a["acked"]:
                    a["acked"] = None
                self._log(a, "re-alerted", now, detail="worsened: " + "; ".join(worse))
                notes.append({"id": aid, "why": "worsened", "level": c["level"], "title": c["title"], "detail": "; ".join(worse)})
            elif RANK[c["level"]] < RANK[old]:
                a["notice"] = dict(a["notice"], level=c["level"], d30=min(a["notice"]["d30"], d30))
                self._log(a, "eased", now, detail=f"level {old} -> {c['level']}")
            changed.add(aid)
        for aid, a in self.alerts.items():
            if a["status"] != "active" or aid in seen:
                continue
            deps = a.get("deps") or []
            if deps and all(fresh.get(d) for d in deps):
                a["missed"] = a.get("missed", 0) + 1
                if a["missed"] >= P["resolve_cycles"]:
                    a.update(status="resolved", resolved_at=now)
                    self._log(a, "cleared", now, detail="verified: no longer present in fresh data")
                changed.add(aid)
            elif not a.get("stale"):
                a["stale"] = True
                self._log(a, "unverifiable", now, detail="source stale or unavailable: clearance cannot be verified, alert kept")
                changed.add(aid)
        for aid, a in self.alerts.items():
            if a["status"] == "active" and a["level"] == "critical" and not a.get("acked") and not a.get("escalated") and a.get("crit_since") \
                    and now - a["crit_since"] >= P["escalate_min"] * 60:
                a["escalated"] = True
                self._log(a, "escalated", now, detail=f"critical and unacknowledged for {round((now - a['crit_since']) / 60)} min")
                notes.append({"id": aid, "why": "escalated", "level": a["level"], "title": a["title"]})
                changed.add(aid)
        for aid in [k for k, a in self.alerts.items() if a["status"] == "resolved" and now - (a.get("resolved_at") or now) > 86400]:
            self.alerts.pop(aid, None)
        if self.store is not None:
            for aid in changed:
                if aid in self.alerts:
                    self.store.save_alert(self.alerts[aid])
        return notes

    def ack(self, aid, by, now, note=""):
        a = self.alerts.get(aid)
        if a is None:
            return None
        a["acked"] = {"by": (by or "controller")[:60], "ts": now, "level": a["level"], "note": (note or "")[:200]}
        self._log(a, "acknowledged", now, by=a["acked"]["by"], detail=a["acked"]["note"])
        if self.store is not None:
            self.store.save_alert(a)
        return a

    def view(self, now, history=False):
        act = [a for a in self.alerts.values() if a["status"] == "active"]
        act.sort(key=lambda a: (bool(a.get("acked")), not a.get("escalated"), -RANK[a["level"]], -(a.get("score") or 0), -a["first"]))
        out = act
        if history:
            res = sorted([a for a in self.alerts.values() if a["status"] == "resolved"], key=lambda a: -(a.get("resolved_at") or 0))
            out = act + res
        return [dict(a, age_min=round((now - a["first"]) / 60), faded=bool(a.get("acked"))) for a in out]

    def counts(self):
        act = [a for a in self.alerts.values() if a["status"] == "active"]
        return {"active": len(act), "critical_unacked": sum(1 for a in act if a["level"] == "critical" and not a.get("acked")),
                "unacked": sum(1 for a in act if not a.get("acked")), "escalated": sum(1 for a in act if a.get("escalated") and not a.get("acked")),
                "acked": sum(1 for a in act if a.get("acked")), "resolved_24h": sum(1 for a in self.alerts.values() if a["status"] == "resolved")}


def critical_kpi(book):
    c = book.counts()
    return {"id": "critical_unacked", "label": "Critical unacknowledged", "value": c["critical_unacked"], "available": True, "stale": False, "unit": "",
            "sub": f"{c['escalated']} escalated - {c['unacked']} unacknowledged in total", "basis": "Critical alerts nobody has acknowledged",
            "level": "critical" if c["critical_unacked"] else None}


# =========================================================================== store (SQLite; PostGIS-ready schema notes in FORECAST_CENTRE.md)
class Store:
    SCHEMA = 1
    MIGRATIONS = {
        1: [
            "CREATE TABLE IF NOT EXISTS fc_meta(k TEXT PRIMARY KEY, v TEXT)",
            "CREATE TABLE IF NOT EXISTS fc_probe(key TEXT, ts REAL, svc TEXT, dir INTEGER, lat REAL, lon REAL, cur REAL, ff REAL, conf REAL, closure INTEGER, provider TEXT)",
            "CREATE INDEX IF NOT EXISTS fc_probe_k ON fc_probe(key, ts)",
            "CREATE TABLE IF NOT EXISTS fc_jam(ts REAL, id TEXT, cell TEXT, mag INTEGER, delay_s REAL, length_m REAL, road TEXT)",
            "CREATE INDEX IF NOT EXISTS fc_jam_c ON fc_jam(cell, ts)",
            "CREATE TABLE IF NOT EXISTS fc_cover(day TEXT, slot INTEGER, dt TEXT, n INTEGER, PRIMARY KEY(day, slot))",
            "CREATE TABLE IF NOT EXISTS fc_fc(issued REAL, key TEXT, h INTEGER, target REAL, pred REAL, lo REAL, hi REAL, obs REAL, err REAL)",
            "CREATE INDEX IF NOT EXISTS fc_fc_k ON fc_fc(key, target)",
            "CREATE TABLE IF NOT EXISTS fc_alert(id TEXT PRIMARY KEY, status TEXT, updated REAL, body TEXT)",
            "CREATE TABLE IF NOT EXISTS fc_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, alert_id TEXT, action TEXT, by_name TEXT, level TEXT, detail TEXT, title TEXT)",
            "CREATE INDEX IF NOT EXISTS fc_audit_a ON fc_audit(alert_id, ts)",
        ],
    }

    def __init__(self, path):
        self.path = path
        self.ok, self.err = True, None
        self.version = self.migrate()

    def _run(self, fn):
        try:
            with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as c:
                c.row_factory = sqlite3.Row
                r = fn(c)
                c.commit()
                self.ok, self.err = True, None
                return r
        except Exception as e:                                          # a missing / read-only disk never breaks the page
            self.ok, self.err = False, f"{type(e).__name__}: {e}"
            return None

    def migrate(self):
        def f(c):
            c.execute("CREATE TABLE IF NOT EXISTS fc_meta(k TEXT PRIMARY KEY, v TEXT)")
            row = c.execute("SELECT v FROM fc_meta WHERE k='schema_version'").fetchone()
            v = int(row[0]) if row else 0
            for n in sorted(self.MIGRATIONS):
                if n > v:
                    for sql in self.MIGRATIONS[n]:
                        c.execute(sql)
                    c.execute("INSERT OR REPLACE INTO fc_meta(k, v) VALUES ('schema_version', ?)", (str(n),))
                    v = n
            return v
        return self._run(f) or 0

    def add_probes(self, rows):
        if rows:
            self._run(lambda c: c.executemany("INSERT INTO fc_probe VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows))

    def probe_hist(self, key, since, until=None):
        until = until or time.time() + 60
        r = self._run(lambda c: c.execute("SELECT ts, cur FROM fc_probe WHERE key=? AND ts>=? AND ts<=? ORDER BY ts", (key, since, until)).fetchall())
        return [(x[0], x[1]) for x in r or []]

    def add_jams(self, rows):
        if rows:
            self._run(lambda c: c.executemany("INSERT INTO fc_jam VALUES (?,?,?,?,?,?,?)", rows))

    def cover(self, now, holidays=()):
        d = sgt(now)
        self._run(lambda c: c.execute("INSERT INTO fc_cover(day, slot, dt, n) VALUES (?,?,?,1) ON CONFLICT(day, slot) DO UPDATE SET n=n+1",
                                      (d.strftime("%Y-%m-%d"), slot15(now), day_type(now, holidays))))

    def jam_recurrence(self, cell, t, holidays=(), P=None, days=35):
        """Share of past same-day-type days (with the collector running at this time) on which a jam was seen in this cell
        +-30 min, and the usual delay. -> {"recurring": bool | None, "share", "days", "typical_s", "detail"}"""
        P = P or PARAMS
        if not cell:
            return {"recurring": None, "share": None, "days": 0, "typical_s": None, "detail": "no location"}
        dt, sl, today = day_type(t, holidays), slot15(t), sgt(t).strftime("%Y-%m-%d")
        since = sgt(t - days * 86400).strftime("%Y-%m-%d")
        cov = self._run(lambda c: c.execute("SELECT DISTINCT day FROM fc_cover WHERE dt=? AND slot BETWEEN ? AND ? AND day>=? AND day<?",
                                            (dt, sl - 2, sl + 2, since, today)).fetchall()) or []
        cov_days = {r[0] for r in cov}
        if len(cov_days) < P["hist_min_days"]:
            return {"recurring": None, "share": None, "days": len(cov_days), "typical_s": None,
                    "detail": f"recurrence unknown: {len(cov_days)} of {P['hist_min_days']} days of history at this time"}
        rows = self._run(lambda c: c.execute("SELECT ts, delay_s FROM fc_jam WHERE cell=? AND ts>=?", (cell, t - days * 86400)).fetchall()) or []
        hit = {}
        for ts, dl in rows:
            ds = sgt(ts).strftime("%Y-%m-%d")
            if ds in cov_days and abs(slot15(ts) - sl) <= 2:
                hit[ds] = max(hit.get(ds, 0), dl or 0)
        share = len(hit) / len(cov_days)
        typ = statistics.median(hit.values()) if hit else None
        rec = share >= P["recur_share"]
        return {"recurring": rec, "share": round(share, 2), "days": len(cov_days), "typical_s": typ,
                "detail": f"seen on {len(hit)} of {len(cov_days)} past {dt.replace('_', '/')} days at this time" + (f", usual delay {round(typ / 60)} min" if typ else "")}

    def add_forecasts(self, rows):
        if rows:
            self._run(lambda c: c.executemany("INSERT INTO fc_fc(issued, key, h, target, pred, lo, hi) VALUES (?,?,?,?,?,?,?)", rows))

    def settle(self, key, ts, obs, tol=240):
        self._run(lambda c: c.execute("UPDATE fc_fc SET obs=?, err=? - pred WHERE key=? AND obs IS NULL AND target BETWEEN ? AND ?",
                                      (obs, obs, key, ts - tol, ts + tol)))

    def calibration(self, now, days=7):
        r = self._run(lambda c: c.execute("SELECT h, pred, lo, hi, obs FROM fc_fc WHERE obs IS NOT NULL AND target>=?", (now - days * 86400,)).fetchall()) or []
        if not r:
            return {"n": 0, "mae": None, "coverage": None, "by_h": {}}
        err = [abs(x[4] - x[1]) for x in r]
        inside = [1 if x[2] <= x[4] <= x[3] else 0 for x in r]
        by = {}
        for x, e, i in zip(r, err, inside):
            b = by.setdefault(int(x[0]), [0, 0.0, 0])
            b[0] += 1
            b[1] += e
            b[2] += i
        return {"n": len(r), "mae": round(sum(err) / len(err), 2), "coverage": round(sum(inside) / len(inside), 3),
                "by_h": {h: {"n": b[0], "mae": round(b[1] / b[0], 2), "coverage": round(b[2] / b[0], 3)} for h, b in sorted(by.items())}}

    def save_alert(self, a):
        body = json.dumps(a, default=str)
        self._run(lambda c: c.execute("INSERT OR REPLACE INTO fc_alert(id, status, updated, body) VALUES (?,?,?,?)", (a["id"], a["status"], time.time(), body)))

    def load_alerts(self, since):
        r = self._run(lambda c: c.execute("SELECT body FROM fc_alert WHERE updated>=?", (since,)).fetchall()) or []
        out = []
        for x in r:
            try:
                a = json.loads(x[0])
                a["delay"] = {int(k) if str(k).isdigit() else k: v for k, v in (a.get("delay") or {}).items()}
                out.append(a)
            except ValueError:
                pass
        return out

    def audit(self, rec):
        self._run(lambda c: c.execute("INSERT INTO fc_audit(ts, alert_id, action, by_name, level, detail, title) VALUES (?,?,?,?,?,?,?)",
                                      (rec["ts"], rec["alert_id"], rec["action"], rec["by"], rec["level"], rec["detail"], rec.get("title"))))

    def audit_list(self, limit=200, alert_id=None):
        q, a = "SELECT ts, alert_id, action, by_name, level, detail, title FROM fc_audit", ()
        if alert_id:
            q, a = q + " WHERE alert_id=?", (alert_id,)
        r = self._run(lambda c: c.execute(q + " ORDER BY id DESC LIMIT ?", a + (int(limit),)).fetchall()) or []
        return [{"ts": x[0], "alert_id": x[1], "action": x[2], "by": x[3], "level": x[4], "detail": x[5], "title": x[6]} for x in r]

    def history_days(self):
        r = self._run(lambda c: c.execute("SELECT COUNT(DISTINCT day) FROM fc_cover").fetchone())
        return int(r[0]) if r else 0

    def purge(self, now, keep_days=35):
        cut = now - keep_days * 86400
        def f(c):
            c.execute("DELETE FROM fc_probe WHERE ts<?", (cut,))
            c.execute("DELETE FROM fc_jam WHERE ts<?", (cut,))
            c.execute("DELETE FROM fc_fc WHERE issued<?", (now - 14 * 86400,))
            c.execute("DELETE FROM fc_audit WHERE ts<?", (now - 90 * 86400,))
            c.execute("DELETE FROM fc_alert WHERE status='resolved' AND updated<?", (now - 30 * 86400,))
            c.execute("DELETE FROM fc_cover WHERE day<?", (sgt(cut).strftime("%Y-%m-%d"),))
        self._run(f)


# =========================================================================== recovery options (advisory, never sent)
def recovery_options(svc, d, extra_min, hw, layover_min=None, P=None):
    """Advisory recovery scenarios for a predicted delay, checked against the existing operational rules.
    Simple headway arithmetic only; the Recovery Decision Engine does the full EWT comparison."""
    hw = hw or 10.0
    extra = max(0.0, extra_min or 0.0)
    hold = float(min(8, max(1, round(extra / 2)))) if extra >= 2 else 0.0
    opts = [
        {"id": "none", "name": "Continue (no action)", "gap_max": round(hw + extra, 1), "gap_min": round(max(0.0, hw - extra), 1),
         "checks": [{"rule": "No change to departures", "ok": True}],
         "note": f"The delayed bus runs about {extra:.0f} min late: the gap ahead of it grows to ~{hw + extra:.0f} min and the bus behind closes to ~{max(0.0, hw - extra):.0f} min" + (" (bunching risk)." if hw - extra < 2 else ".")},
        {"id": "adjust", "name": f"Pre-emptive departure adjustment: hold the next departure {hold:.0f} min", "gap_max": round(hw + extra - hold / 2, 1),
         "gap_min": round(max(0.0, hw - extra + hold), 1),
         "checks": [{"rule": "Adjustment within the 8-min maximum", "ok": hold <= 8},
                    {"rule": "Layover stays at least 7 min", "ok": True if layover_min is None or layover_min >= 7 else False,
                     "note": "holding a departure lengthens the layover" if layover_min is None else f"layover {layover_min:.0f} min"},
                    {"rule": "Already-departed trips stay locked", "ok": True},
                    {"rule": "Subsequent trips re-timed in Headway Control", "ok": None, "note": "check in Headway Control"}],
         "note": "Spreads the buses behind the delay so they do not catch up with it.", "href": f"/control?svc={svc}&dir={d}"},
        {"id": "halfway", "name": "Halfway recovery at an approved halfway point", "gap_max": round((hw + extra) / 2 + 1, 1), "gap_min": None,
         "checks": [{"rule": "Approved halfway locations only", "ok": None, "note": "choose in Halfway Planner"},
                    {"rule": "Minimum 7-min break before re-entry", "ok": None, "note": "checked by Halfway Planner"},
                    {"rule": "Subsequent trips accounted for", "ok": None, "note": "Recovery Decision Engine"}],
         "note": "Worth it when the gap reaches about 2x the headway.", "href": f"/halfway?svc={svc}&dir={d}"},
    ]
    best = "adjust" if extra >= 2 and extra < 2 * hw else ("halfway" if extra >= 2 * hw else "none")
    return {"svc": svc, "dir": d, "extra_min": round(extra, 1), "hw": hw, "options": opts, "suggested": best,
            "basis": "Advisory scenario - simple headway arithmetic. No operational instruction is sent: an authorised SC must approve any action.",
            "engine": {"label": "Compare on EWT in the Recovery Decision Engine", "href": f"/recovery?svc={svc}&dir={d}"}}


# =========================================================================== DEMO world (deterministic, fictional)
DEMO_BASE = datetime(2026, 1, 6, 7, 40, tzinfo=SGT).timestamp()      # a Tuesday; the date is fictional for the demo
DEMO_STEPS = [
    {"t": 0, "title": "Normal operations", "say": "Traffic is normal for a weekday morning. Every KPI is computed from the (fictional) feeds."},
    {"t": 10, "title": "Traffic builds up", "say": "Speeds on the expressway eastbound start to drop; TomTom reports a small jam."},
    {"t": 20, "title": "Deterioration detected", "say": "Speed is 50 % below the usual for 08:00 on a weekday - abnormal, not the normal peak. An LTA accident report arrives: a point only, so it is shown as UNVERIFIED on the map and NOT counted."},
    {"t": 25, "title": "Route impact verified", "say": "TomTom's jam and accident lines run along X101 eastbound and X305: CONFIRMED. X101 westbound (opposite carriageway), X102 (parallel road) and X201 (flyover) are REJECTED."},
    {"t": 30, "title": "Prediction generated", "say": "+15 / +30 / +60 min forecasts with uncertainty bands; the jam is still building, recovery not before the jam stops growing."},
    {"t": 41, "title": "OCC alerted", "say": "The critical alert has been unacknowledged for 15 min: it is ESCALATED. Duplicates are grouped, unchanged conditions do not re-alert."},
    {"t": 44, "title": "Recovery option evaluated", "say": "Advisory options checked against the rules (max 8-min adjustment, 7-min layover, departed trips locked). Nothing is sent without SC approval."},
]


def _demo_geo():
    pie_e = [(1.3330, 103.8600), (1.3334, 103.8680), (1.3338, 103.8760), (1.3340, 103.8840), (1.3339, 103.8920), (1.3336, 103.9000), (1.3333, 103.9060)]
    off_n = lambda pts, dlat: [(a + dlat, b) for a, b in pts]
    x101_1 = [(1.3290, 103.8480), (1.3305, 103.8540), (1.3322, 103.8580)] + pie_e + [(1.3350, 103.9100), (1.3380, 103.9150)]
    x101_2 = [(1.3380, 103.9150), (1.3352, 103.9102)] + off_n(pie_e, 0.00025)[::-1] + [(1.3324, 103.8578), (1.3305, 103.8540), (1.3290, 103.8480)]
    x102_1 = [(1.3310, 103.8620)] + off_n([p for p in pie_e if 103.865 < p[1] < 103.902], -0.00045) + [(1.3300, 103.9030)]
    x201_1 = [(1.3200, 103.8838), (1.3300, 103.8840), (1.3340, 103.8841), (1.3400, 103.8842), (1.3480, 103.8845)]
    x305_1 = [(1.3420, 103.8700), (1.3380, 103.8740), (1.3338, 103.8760), (1.3340, 103.8840), (1.3320, 103.8850), (1.3250, 103.8860)]
    x410_1 = [(1.3330, 103.7000), (1.3360, 103.7080), (1.3390, 103.7150), (1.3420, 103.7230), (1.3450, 103.7300)]
    return {("X101", 1): x101_1, ("X101", 2): x101_2, ("X102", 1): x102_1, ("X201", 1): x201_1, ("X305", 1): x305_1, ("X410", 1): x410_1}, pie_e


def _sub(pts, lon_a, lon_b, dlat=0.0):
    L = Line(pts)
    xa, xb = lon_a * KX, lon_b * KX
    S, _, _ = L.sample(25.0)
    out = [(y / KY + dlat, x / KX) for x, y in S if xa <= x <= xb]
    return out


def _h(*a):
    return int(hashlib.md5("|".join(map(str, a)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def demo_world(step):
    """The fictional world at demo step k (0-6). Same structure as a live world: the real engine runs on it."""
    step = max(0, min(len(DEMO_STEPS) - 1, int(step)))
    now = DEMO_BASE + DEMO_STEPS[step]["t"] * 60
    lines, pie = _demo_geo()
    names = {("X101", 1): ("Demo Central Int", "Demo East Int"), ("X101", 2): ("Demo East Int", "Demo Central Int"), ("X102", 1): ("Service Rd West", "Service Rd East"),
             ("X201", 1): ("Demo South", "Demo North"), ("X305", 1): ("Demo Hill", "Demo Park"), ("X410", 1): ("Demo West Int", "Demo Lakeside")}
    stops = {}
    for k, pts in lines.items():
        L = Line(pts)
        n = max(4, int(L.length / 700))
        st = []
        for i in range(n + 1):
            la, lo = L.point_at(L.length * i / n)
            nm = names[k][0] if i == 0 else names[k][1] if i == n else f"{k[0]} Stop {i}"
            st.append({"code": f"9{k[0][1:]}{k[1]}{i:02d}", "name": nm, "lat": round(la, 6), "lon": round(lo, 6)})
        stops[k] = st
    jam_steps = {1: (1, 450, 40), 2: (2, 1800, 150), 3: (3, 2900, 420), 4: (3, 3000, 520), 5: (3, 3000, 560), 6: (3, 3000, 545)}
    events = []
    if step in jam_steps:
        mag, ln, dl = jam_steps[step]
        lon_b = 103.8960
        lon_a = lon_b - ln / KX
        events.append({"id": "tomtom:DEMO-JAM-1", "source": "TomTom", "provider": "tomtom", "kind": "jam", "label": "Jam", "desc": "Stationary traffic",
                       "road": "DEMO EXPWY", "from": "Demo Rd Exit", "to": "Demo Airport Exit", "line": _sub(pie, lon_a, lon_b, 0.00002), "pt": None, "geom": "line",
                       "delay_s": dl, "length_m": ln, "magnitude": TT_MAG[mag], "mag": mag, "start": None, "end": None, "passable": True,
                       "pass_text": BUS_PASS["jam"][1], "observed": now, "validity": "present"})
    if step >= 3:
        events.append({"id": "tomtom:DEMO-ACC-1", "source": "TomTom", "provider": "tomtom", "kind": "accident", "label": "Accident", "desc": "Accident, lane 1 blocked",
                       "road": "DEMO EXPWY", "from": "Demo Rd Exit", "to": "Demo Airport Exit", "line": _sub(pie, 103.8925, 103.8952, 0.00002), "pt": None, "geom": "line",
                       "delay_s": 150, "length_m": 300, "magnitude": "major", "mag": 3, "start": now - 1500, "end": None, "passable": True,
                       "pass_text": BUS_PASS["accident"][1], "observed": now, "validity": "present"})
        events.append({"id": "tomtom:DEMO-JAM-W", "source": "TomTom", "provider": "tomtom", "kind": "jam", "label": "Jam", "desc": "Slow traffic (rubbernecking)",
                       "road": "DEMO EXPWY", "from": "Demo Airport Exit", "to": "Demo Rd Exit", "line": _sub(pie, 103.8900, 103.8950, 0.00025)[::-1], "pt": None, "geom": "line",
                       "delay_s": 50, "length_m": 500, "magnitude": "minor", "mag": 1, "start": None, "end": None, "passable": True,
                       "pass_text": BUS_PASS["jam"][1], "observed": now, "validity": "present"})
    if step >= 2:
        events.append({"id": "lta:DEMO-ACC-1", "source": "LTA", "provider": "lta", "kind": "accident", "label": "Accident",
                       "desc": "Accident on DEMO EXPWY (towards Demo Airport) after Demo Rd Exit. Avoid lane 1.", "road": "DEMO EXPWY", "towards": "Demo Airport",
                       "from": "", "to": "", "line": [], "pt": (1.33392, 103.8940), "geom": "point", "delay_s": None, "length_m": None, "magnitude": "unknown",
                       "mag": None, "start": DEMO_BASE + 19 * 60, "end": None, "passable": True, "pass_text": BUS_PASS["accident"][1], "observed": now, "validity": "current"})
    events.append({"id": "ltarw:DEMO-RW-1", "source": "LTA", "provider": "lta", "kind": "roadworks", "label": "Road works", "desc": "Cable laying (fictional)",
                   "road": "Demo Service Road", "from": "", "to": "", "line": [], "pt": (1.33335, 103.8800), "geom": "point", "delay_s": None, "length_m": None,
                   "magnitude": "unknown", "mag": None, "start": DEMO_BASE - 86400 * 3, "end": DEMO_BASE + 86400 * 10, "passable": True,
                   "pass_text": "Approved works window - lane restrictions not given by LTA", "observed": now, "validity": "approved window"})
    events.append({"id": "tomtom:DEMO-RW-2", "source": "TomTom", "provider": "tomtom", "kind": "roadworks", "label": "Road works", "desc": "Road works, lane 2 closed (fictional)",
                   "road": "Demo West Ave", "from": "Demo Lakeside", "to": "Demo Hill", "line": _sub(lines[("X410", 1)], 103.7090, 103.7140, 0.0), "pt": None, "geom": "line",
                   "delay_s": 60, "length_m": 600, "magnitude": "minor", "mag": 1, "start": DEMO_BASE - 7200, "end": DEMO_BASE + 6 * 3600, "passable": True,
                   "pass_text": BUS_PASS["roadworks"][1], "observed": now, "validity": "start/end from TomTom"})
    # probes: three on the expressway eastbound (X101 dir 1), one in the west (X410)
    sp = {0: 72, 1: 58, 2: 38, 3: 24, 4: 22, 5: 21, 6: 23}
    probes = []
    for i, lon in enumerate((103.8740, 103.8840, 103.8940)):
        ly = Line(lines[("X101", 1)])
        a = float(ly.locate(to_xy(1.3338, lon))[1][0])
        seg = [ly.point_at(a - 150), ly.point_at(a), ly.point_at(a + 150)]
        key = f"DEMO-P{i + 1}"
        hist = []
        for dd in range(1, 36):
            ts0 = DEMO_BASE - dd * 86400
            if sgt(ts0).weekday() >= 5:
                continue
            for m in range(-40, 130, 10):
                v = 70 - 6 * math.exp(-((m - 35) / 30.0) ** 2) + (_h(key, dd, m) - 0.5) * 8
                hist.append((ts0 + m * 60, round(v, 1)))
        series = []
        for m in range(0, DEMO_STEPS[step]["t"] + 1, 2):
            k0 = max(s for s in range(step + 1) if DEMO_STEPS[s]["t"] <= m)
            k1 = min(step, k0 + 1)
            t0, t1 = DEMO_STEPS[k0]["t"], DEMO_STEPS[k1]["t"]
            f = 0 if t1 == t0 else (m - t0) / (t1 - t0)
            v = sp[k0] + (sp[k1] - sp[k0]) * f - i * 2 + (_h(key, m) - 0.5) * 2
            series.append((DEMO_BASE + m * 60, round(v, 1)))
        cur = series[-1][1]
        probes.append({"key": key, "svc": "X101", "dir": 1, "lat": seg[1][0], "lon": seg[1][1], "road": "DEMO EXPWY (eastbound)", "cur": cur, "ff": 80.0, "conf": 0.95,
                       "closure": False, "line": seg, "ts": now, "series": series, "hist": hist, "provider": "tomtom"})
    lw = Line(lines[("X410", 1)])
    seg = [lw.point_at(1800), lw.point_at(1950), lw.point_at(2100)]
    hist = [(DEMO_BASE - dd * 86400 + m * 60, 46 + (_h("W", dd, m) - 0.5) * 6) for dd in range(1, 36) if sgt(DEMO_BASE - dd * 86400).weekday() < 5 for m in range(-40, 130, 10)]
    wv = {0: 45, 1: 44, 2: 41, 3: 37, 4: 36, 5: 35, 6: 35}[step]
    probes.append({"key": "DEMO-P4", "svc": "X410", "dir": 1, "lat": seg[1][0], "lon": seg[1][1], "road": "Demo West Ave", "cur": wv, "ff": 60.0, "conf": 0.9, "closure": False,
                   "line": seg, "ts": now, "series": [(now - 600, wv + 2), (now - 300, wv + 1), (now, wv)], "hist": hist, "provider": "tomtom"})
    st_xy = [("S01", "Demo West Gauge A", 1.3360, 103.7050), ("S02", "Demo West Gauge B", 1.3420, 103.7180), ("S03", "Demo West Gauge C", 1.3300, 103.7250),
             ("S04", "Demo Central Gauge", 1.3400, 103.8300), ("S05", "Demo East Gauge A", 1.3350, 103.8800), ("S06", "Demo East Gauge B", 1.3250, 103.9100),
             ("S07", "Demo North Gauge", 1.3900, 103.8500), ("S08", "Demo South Gauge", 1.2900, 103.8400), ("S09", "Demo Far West", 1.3500, 103.6800),
             ("S10", "Demo Far East", 1.3600, 103.9600)]
    wet = {0: (0, 0, 0), 1: (0.4, 0.2, 0), 2: (2.2, 1.6, 0.6), 3: (3.0, 2.4, 1.0), 4: (3.4, 2.6, 1.2), 5: (2.8, 2.2, 1.0), 6: (2.0, 1.4, 0.6)}[step]
    wet_prev = {0: (0, 0, 0), 1: (0, 0, 0), 2: (0.4, 0.2, 0), 3: (2.2, 1.6, 0.6), 4: (3.0, 2.4, 1.0), 5: (3.4, 2.6, 1.2), 6: (2.8, 2.2, 1.0)}[step]
    stations = [{"id": s[0], "name": s[1], "lat": s[2], "lon": s[3], "mm": (wet[i] if i < 3 else 0.0)} for i, s in enumerate(st_xy)]
    prev = [{"id": s[0], "name": s[1], "lat": s[2], "lon": s[3], "mm": (wet_prev[i] if i < 3 else 0.0)} for i, s in enumerate(st_xy)]
    areas = [{"name": "Demo West", "lat": 1.3380, "lon": 103.7100, "text": "Heavy Thundery Showers" if step >= 2 else "Cloudy", "rain": step >= 2, "heavy": step >= 2},
             {"name": "Demo Central", "lat": 1.3400, "lon": 103.8300, "text": "Cloudy", "rain": False, "heavy": False},
             {"name": "Demo East", "lat": 1.3350, "lon": 103.8900, "text": "Partly Cloudy (Day)", "rain": False, "heavy": False},
             {"name": "Demo North", "lat": 1.3900, "lon": 103.8500, "text": "Light Showers" if step >= 4 else "Cloudy", "rain": step >= 4, "heavy": False}]
    sources = {n: {"status": "live", "age_s": 30, "detail": "DEMO data (fictional)", "label": lbl} for n, lbl in (
        ("tomtom_incidents", "TomTom incidents & jams"), ("tomtom_flow", "TomTom flow probes"), ("lta_incidents", "LTA incidents"),
        ("lta_roadworks", "LTA road works"), ("nea_rain", "NEA rainfall"), ("nea_forecast", "NEA 2-h forecast"), ("route_index", "Route geometry"),
        ("running_time", "Running-time history"))}
    return {"demo": True, "now": now, "step": step, "index": RouteIndex(lines), "routes": {}, "events": events, "probes": probes,
            "rain": {"stations": stations, "prev": prev, "ts": now}, "nea_2h": {"areas": areas}, "sources": sources,
            "headways": {("X101", 1): 8.0, ("X101", 2): 8.0, ("X102", 1): 12.0, ("X201", 1): 10.0, ("X305", 1): 15.0, ("X410", 1): 9.0},
            "hist_rt": {("X101", 1): 42.0, ("X101", 2): 41.0, ("X305", 1): 35.0, ("X410", 1): 38.0}, "stops": stops,
            "focus": [("X101", 1), ("X101", 2), ("X102", 1), ("X201", 1), ("X305", 1), ("X410", 1)], "holidays": (),
            "calibration": {"n": 0, "mae": None, "coverage": None, "by_h": {}}}


def demo_state(step, P=None):
    """Replay the demo from step 0 to `step` through the real engine and a fresh alert book (deterministic)."""
    P = P or PARAMS
    step = max(0, min(len(DEMO_STEPS) - 1, int(step)))
    book, jt = AlertBook(P), JamTracker()
    state = world = None
    for k in range(step + 1):
        world = demo_world(k)
        jt.update(world["events"], world["now"])
        world["jam_info"] = {}
        for ev in world["events"]:
            if ev.get("delay_s") is not None:
                r, _ = jt.rate(ev["id"])
                world["jam_info"][ev["id"]] = {"rate": r, "recurring": False if ev["kind"] == "jam" and "W" not in ev["id"] else None,
                                               "typical_s": None, "first_seen": jt.first_seen(ev["id"]),
                                               "detail": "seen on 2 of 20 past weekdays at this time (DEMO history)" if ev["kind"] == "jam" and "W" not in ev["id"] else None}
        state = evaluate(world, P)
        book.update(state["cands"], world["now"], {n: True for n in world["sources"]})
        if k == 6:
            crit = [a for a in book.alerts.values() if a["status"] == "active" and (a["level"] == "critical" or a.get("escalated"))]
            for a in crit:
                book.ack(a["id"], "DEMO Controller", world["now"] - 60, "Acknowledged in the demo")
    state["kpis"].append(critical_kpi(book))
    state["alerts"] = book.view(world["now"], history=True)
    state["alert_counts"] = book.counts()
    state["audit"] = list(book.audit)[-60:][::-1]
    state["demo"] = {"step": step, "steps": DEMO_STEPS, "title": DEMO_STEPS[step]["title"], "say": DEMO_STEPS[step]["say"],
                     "clock": sgt(world["now"]).strftime("%H:%M"), "label": "DEMO - FICTIONAL SCENARIO (services X101-X410 do not exist)"}
    top = max(state["impacts"].values(), key=lambda s: s["extra"][30]["mid"], default=None)
    if top is not None and step >= 6:
        state["recovery"] = recovery_options(top["svc"], top["dir"], top["extra"][30]["mid"], top["hw"], 9.0, P)
    return state, world


def recompute_service(state, world, key, P=None):
    """Re-evaluate ONE service-direction (after on-demand probes for the service someone selected), in place in state["impacts"]."""
    P = P or PARAMS
    idx = world.get("index")
    R = (idx.lines.get(key) if idx is not None else None) or (Line(world["routes"][key]) if key in (world.get("routes") or {}) else None)
    if R is None or not R.ok:
        return None
    now = world["now"]
    conf, unv = [], []
    for ev in state.get("_ev") or []:
        m = match_event(R, ev, now, P["horizons"][-1], P)
        if m["status"] == "confirmed":
            conf.append((ev, m))
        elif m["status"] == "unverified":
            unv.append((ev, m))
    sub = {"now": now, "probes": [p for p in world.get("probes") or [] if (p["svc"], p["dir"]) == key], "holidays": world.get("holidays", ())}
    pb = prepare_probes(sub, {key: R}, P).get(key, [])
    rain = world.get("rain") or {}
    rx = rain_exposure(R, rain.get("stations") or [], rain.get("prev"), P) if rain.get("stations") else None
    im = service_impact(key, R, conf, unv, [p for p in pb if p.get("dir_ok")], rx, (world.get("headways") or {}).get(key), world.get("jam_info") or {}, now,
                        P["horizons"], (world.get("hist_rt") or {}).get(key), P)
    im["probes"] = pb
    im["hw_src"] = "scheduled" if (world.get("headways") or {}).get(key) else "assumed 10 min (no headway on record)"
    state["impacts"][key] = im
    return im


# =========================================================================== backup sources (V17.3)
# 1) TomTom VECTOR INCIDENT TILES: the same incidents and jams as Incident Details, as road lines, from the tile allowance
#    (Traffic Flow & Incidents vector tiles, 200,000 a month) instead of Incident Details (2,500 a month).
#    Direction: each incident has a "start_poi" marker at its beginning; the line is oriented to start there. A line with no start
#    marker cannot be oriented -> UNVERIFIED (shown, not counted).
# 2) Waze for Cities jams (lines in the direction of traffic) and alerts (points), when WAZE_FEED_URL is set.
import gzip
import struct


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def _pb_fields(b):
    """Protocol-buffer fields of a message -> (field number, wire type, int | bytes)."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield f, wt, v


def _packed(b):
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def _zz(n):
    return (n >> 1) ^ -(n & 1)


def _mvt_value(b):
    for f, wt, v in _pb_fields(b):
        if f == 1:
            return bytes(v).decode("utf-8", "replace")
        if f == 2:
            return struct.unpack("<f", bytes(v))[0]
        if f == 3:
            return struct.unpack("<d", bytes(v))[0]
        if f == 4:
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 5:
            return v
        if f == 6:
            return _zz(v)
        if f == 7:
            return bool(v)
    return None


def _mvt_geom(cmds):
    """Geometry commands -> list of parts, each a list of (x, y) tile units."""
    parts, cur, x, y, i = [], None, 0, 0, 0
    while i < len(cmds):
        c = cmds[i]
        i += 1
        cid, cnt = c & 7, c >> 3
        if cid == 7:                                   # ClosePath
            if cur:
                cur.append(cur[0])
            continue
        for _ in range(cnt):
            x += _zz(cmds[i])
            y += _zz(cmds[i + 1])
            i += 2
            if cid == 1:                               # MoveTo starts a new part
                cur = [(x, y)]
                parts.append(cur)
            elif cur is not None:
                cur.append((x, y))
    return parts


def decode_mvt(data):
    """A Mapbox Vector Tile (gzip or not) -> {layer name: {"extent", "features": [{"type", "props", "parts"}]}}."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    out = {}
    for f, wt, lb in _pb_fields(memoryview(data)):
        if f != 3 or wt != 2:
            continue
        name, extent, keys, vals, raw = "", 4096, [], [], []
        for lf, lwt, lv in _pb_fields(lb):
            if lf == 1:
                name = bytes(lv).decode("utf-8", "replace")
            elif lf == 2:
                raw.append(lv)
            elif lf == 3:
                keys.append(bytes(lv).decode("utf-8", "replace"))
            elif lf == 4:
                vals.append(_mvt_value(lv))
            elif lf == 5:
                extent = lv
        feats = []
        for fb in raw:
            tags, gtype, geom = [], 0, []
            for ff, fwt, fv in _pb_fields(fb):
                if ff == 2:
                    tags = _packed(fv) if fwt == 2 else tags + [fv]
                elif ff == 3:
                    gtype = fv
                elif ff == 4:
                    geom = _packed(fv) if fwt == 2 else geom + [fv]
            props = {keys[tags[k]]: vals[tags[k + 1]] for k in range(0, len(tags) - 1, 2) if tags[k] < len(keys) and tags[k + 1] < len(vals)}
            feats.append({"type": gtype, "props": props, "parts": _mvt_geom(geom)})
        out[name] = {"extent": extent, "features": feats}
    return out


def tile_ll(z, tx, ty, px, py, extent=4096):
    n = 2.0 ** z
    lon = (tx + px / extent) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + py / extent) / n))))
    return lat, lon


def tiles_for_bbox(z, south, west, north, east):
    n = 2 ** z
    x0, x1 = int((west + 180) / 360 * n), int((east + 180) / 360 * n)
    yy = lambda lat: int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(yy(north), yy(south) + 1)]


def _endpoint_m(a, b):
    return hav_m(a[0], a[1], b[0], b[1])


def join_pieces(lines, tol=6.0):
    """Pieces of one line cut by tile borders -> whole lines. Tiles carry a margin, so neighbouring pieces usually OVERLAP for a stretch
    (sometimes they only touch); either way they are joined once, in any stored orientation. A piece lying wholly on another is dropped."""
    lines = [list(l) for l in lines if len(l) >= 2]
    on = lambda L, p: float(L.locate(to_xy(*p))[0][0]) <= tol
    changed = True
    while changed:
        changed = False
        for i in range(len(lines)):
            La = Line(lines[i])
            if not La.ok:
                continue
            for j in range(len(lines)):
                if i == j:
                    continue
                Lb0 = Line(lines[j])
                if not Lb0.ok:
                    continue
                if on(La, lines[j][0]) and on(La, lines[j][-1]):             # j lies on i: a duplicate piece
                    del lines[j]
                    changed = True
                    break
                for b in (lines[j], lines[j][::-1]):
                    a = lines[i]
                    if _endpoint_m(a[-1], b[0]) <= tol:
                        new = a + b[1:]
                    else:
                        Lb = Line(b)
                        if not (on(La, b[0]) and on(Lb, a[-1])):
                            continue
                        s0 = float(La.locate(to_xy(*b[0]))[1][0])
                        head = La.cut(0.0, s0) if s0 > 0.5 else [a[0]]
                        new = head[:-1] + b if len(head) >= 2 else b
                    lines[i] = new
                    del lines[j]
                    changed = True
                    break
                if changed:
                    break
            if changed:
                break
    return lines


def parse_tomtom_incident_tiles(tiles, now=None, P=None):
    """[(z, x, y, pbf bytes)] -> incident events. Pieces of one incident cut by tile borders are joined; each joined line is oriented from
    its TomTom start marker (start_poi). Without a start marker the direction is unknown: dir_known False (never confirmed)."""
    P = P or PARAMS
    now = now or time.time()
    segs, pois = [], []
    for z, tx, ty, data in tiles:
        try:
            L = decode_mvt(bytes(data))
        except Exception:
            continue
        fl = L.get("Traffic incidents flow") or L.get("Traffic incident flow") or {}
        po = L.get("Traffic incidents POI") or L.get("Traffic incident POI") or {}
        ext = fl.get("extent", 4096)
        for ft in fl.get("features", []):
            pr = ft["props"]
            try:
                cat = int(pr.get("icon_category", 0))
            except (TypeError, ValueError):
                cat = 0
            if cat == 13:                                        # a cluster of mixed incidents (low zoom): not usable
                continue
            for part in ft["parts"]:
                pts = [tile_ll(z, tx, ty, px, py, ext) for px, py in part]
                pts = [p for p in pts if in_sg(*p)]
                if len(pts) >= 2:
                    segs.append({"cat": cat, "mag": pr.get("magnitude"), "delay": pr.get("delay"), "desc": pr.get("description") or "",
                                 "road_type": pr.get("road_type") or "", "pts": pts})
        pext = po.get("extent", 4096)
        for ft in po.get("features", []):
            pr = ft["props"]
            if (pr.get("poi_type") or "start_poi") != "start_poi":
                continue
            for part in ft["parts"]:
                for px, py in part:
                    try:
                        cat = int(pr.get("icon_category", 0))
                    except (TypeError, ValueError):
                        cat = 0
                    pois.append((tile_ll(z, tx, ty, px, py, pext), cat))
    groups = {}
    for s_ in segs:
        groups.setdefault((s_["cat"], s_["mag"], s_["delay"], s_["desc"]), []).append(s_["pts"])
    events = []
    for (cat, mag, delay, desc), lines in groups.items():
        lines = join_pieces(lines)
        for ln in lines:
            if Line(ln).length < 20:
                continue
            ds, de = min([_endpoint_m(p, ln[0]) for p, c in pois if c == cat] or [1e9]), min([_endpoint_m(p, ln[-1]) for p, c in pois if c == cat] or [1e9])
            dir_known = min(ds, de) <= 40.0
            if dir_known and de < ds:
                ln = ln[::-1]
            kind = TT_KIND.get(cat, "other")
            passable, ptxt = BUS_PASS.get(kind, BUS_PASS["other"])
            mid = ln[len(ln) // 2]
            try:
                mg = int(mag) if mag is not None else None
            except (TypeError, ValueError):
                mg = None
            events.append({"id": "tomtom-tile:" + stable_id(cat, round(ln[0][0], 4), round(ln[0][1], 4), round(ln[-1][0], 4), round(ln[-1][1], 4)),
                           "source": "TomTom tiles", "provider": "tomtom", "src_key": "tomtom_tiles", "kind": kind, "label": TT_ICON.get(cat, "Incident"),
                           "desc": desc or TT_ICON.get(cat, "Incident"), "road": "", "from": "", "to": "", "line": ln, "pt": mid, "geom": "line",
                           "dir_known": dir_known, "delay_s": num(delay), "length_m": round(Line(ln).length), "magnitude": TT_MAG.get(mg, "unknown"), "mag": mg,
                           "start": None, "end": None, "passable": passable, "pass_text": ptxt, "observed": now,
                           "validity": "present (TomTom incident tiles)" + ("" if dir_known else "; direction unknown (no start marker)")})
    return events


WAZE_MAG = {0: 0, 1: 1, 2: 1, 3: 2, 4: 3, 5: 4}


def waze_events(jams, alerts, now=None):
    """Waze for Cities feed (already parsed by traffic.parse_waze) -> events. Jam lines follow the direction of traffic."""
    now = now or time.time()
    out = []
    for j in jams or []:
        ln = [(float(a), float(b)) for a, b in j.get("line") or [] if in_sg(num(a), num(b))]
        if len(ln) < 2:
            continue
        lv = int(j.get("level") or 0)
        out.append({"id": "waze:" + stable_id(round(ln[0][0], 4), round(ln[0][1], 4), round(ln[-1][0], 4), round(ln[-1][1], 4)), "source": "Waze", "provider": "waze",
                    "src_key": "waze", "kind": "road_closed" if lv >= 5 else "jam", "label": "Road closed" if lv >= 5 else "Jam", "desc": f"Waze jam level {lv}",
                    "road": j.get("street") or "", "from": "", "to": "", "line": ln, "pt": ln[len(ln) // 2], "geom": "line", "dir_known": True,
                    "delay_s": num(j.get("delay_s")), "length_m": round(Line(ln).length), "magnitude": TT_MAG.get(WAZE_MAG.get(lv, 0), "unknown"),
                    "mag": WAZE_MAG.get(lv, 0), "start": None, "end": None, "passable": lv < 5, "pass_text": BUS_PASS["road_closed" if lv >= 5 else "jam"][1],
                    "observed": now, "validity": "present (Waze)"})
    for a in alerts or []:
        la, lo = num(a.get("lat")), num(a.get("lon"))
        if not in_sg(la, lo):
            continue
        t = str(a.get("type") or "").lower()
        kind = "accident" if "accident" in t else "road_closed" if "closed" in t else "hazard"
        out.append({"id": "waze:" + str(a.get("key") or stable_id(la, lo, t)), "source": "Waze", "provider": "waze", "src_key": "waze", "kind": kind,
                    "label": str(a.get("type") or "Waze report").replace("Waze: ", ""), "desc": a.get("message") or "", "road": a.get("road") or "", "from": "", "to": "",
                    "line": [], "pt": (la, lo), "geom": "point", "delay_s": None, "length_m": None, "magnitude": "unknown", "mag": None, "start": None, "end": None,
                    "passable": kind != "road_closed", "pass_text": BUS_PASS[kind][1], "observed": now, "validity": "present (Waze user report)"})
    return out
