"""Open-data feeds for Running Time Analytics (V13.6). Pure parsing / aggregation, no I/O.

Sources (all public, no operator data needed):
  * LTA DataMall      - live bus positions (the running time itself), speed bands, incidents, road works (captured when each trip is stored),
                        Passenger Volume by Bus Stops (monthly, tap-in / tap-out per stop per hour, weekday vs weekend).
  * data.gov.sg       - rainfall every 5 min at ~60 stations (v2 real-time API, past dates allowed -> can be backfilled),
                        Singapore public holidays (MOM, consolidated dataset).
  * MOE               - school vacation periods (published yearly; seeded below, editable in the app).
"""
import csv
import io
import math
import re
import zipfile

RAIN_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
HOLIDAY_URL = "https://data.gov.sg/api/action/datastore_search"
HOLIDAY_DATASETS = ["d_8ef23381f9417e4d4254ee8b4dcdb176",          # Singapore Public Holidays (consolidated), MOM
                    "d_149b61ad0a22f61c09dc80f2df5bbec8",          # Public Holidays for 2026
                    "d_0ba69fc6d56717ff0bf8083c6af3bb84"]          # Public Holidays for 2027
PV_PATH = "PV/Bus"                                                  # DataMall Passenger Volume by Bus Stops (returns a short-lived zip link)

# MOE school vacations 2026 (primary & secondary; MOE press release "School Terms and Holidays for 2026"). Editable in the app; add later years there.
SCHOOL_HOLIDAYS_SEED = [
    ("2026-03-14", "2026-03-22", "March school holidays 2026"),
    ("2026-05-30", "2026-06-28", "June school holidays 2026"),
    ("2026-09-05", "2026-09-13", "September school holidays 2026"),
    ("2026-11-21", "2026-12-31", "Year-end school holidays 2026"),
]


def hav_km(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


# --------------------------------------------------------------------------- rainfall (data.gov.sg v2)
def parse_rain_page(j):
    """-> (stations {id: (lat, lon, name)}, readings [(iso_ts, {station_id: mm})], next_token)"""
    d = (j or {}).get("data") or {}
    st = {}
    for s in d.get("stations") or []:
        loc = s.get("location") or s.get("labelLocation") or {}
        if loc.get("latitude") is not None:
            st[s.get("id") or s.get("deviceId")] = (float(loc["latitude"]), float(loc["longitude"]), s.get("name") or "")
    rd = []
    for r in d.get("readings") or []:
        vals = {x.get("stationId"): float(x.get("value") or 0.0) for x in r.get("data") or [] if x.get("stationId")}
        rd.append((r.get("timestamp"), vals))
    return st, rd, d.get("paginationToken")


def iso_to_min(ts):
    """'2026-09-23T14:05:00+08:00' -> minutes after midnight SGT"""
    m = re.search(r"T(\d{2}):(\d{2})", ts or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def stations_near(line_pts, stations, k=3, max_km=4.0):
    """the rain gauges nearest to the route (by distance to sample points along it)."""
    best = {}
    for sid, (la, lo, _) in stations.items():
        d = min(hav_km((la, lo), p) for p in line_pts)
        if d <= max_km:
            best[sid] = d
    return [s for s, _ in sorted(best.items(), key=lambda x: x[1])[:k]]


def rain_for_trip(day_readings, station_ids, start_min, end_min):
    """mm of rain in the trip's window, averaged over the gauges near the route (5-min readings summed)."""
    if not station_ids or not day_readings:
        return None
    tot, n = 0.0, 0
    for ts, vals in day_readings:
        m = iso_to_min(ts)
        if m is None or not (start_min - 5 <= m <= end_min):
            continue
        v = [vals[s] for s in station_ids if s in vals]
        if v:
            tot += sum(v) / len(v)
            n += 1
    return round(tot, 2) if n else None


# --------------------------------------------------------------------------- public holidays
def parse_holidays(j):
    out = {}
    for r in ((j or {}).get("result") or {}).get("records") or []:
        d = str(r.get("date") or r.get("Date") or "")[:10]
        if re.match(r"\d{4}-\d{2}-\d{2}$", d):
            out[d] = r.get("holiday") or r.get("Holiday") or "Public holiday"
    return out


# --------------------------------------------------------------------------- passenger volume by bus stop (DataMall monthly zip)
def parse_pv_zip(raw, keep_stops=None):
    """-> {(day_type, hour, stop): (tap_in, tap_out)}; keep_stops limits it to the stops of services being analysed."""
    out = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if not name:
            return out
        with z.open(name) as f:
            rd = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
            for r in rd:
                code = (r.get("PT_CODE") or "").strip().zfill(5)
                if keep_stops and code not in keep_stops:
                    continue
                try:
                    hr = int(r.get("TIME_PER_HOUR") or -1)
                    tin, tout = int(float(r.get("TOTAL_TAP_IN_VOLUME") or 0)), int(float(r.get("TOTAL_TAP_OUT_VOLUME") or 0))
                except ValueError:
                    continue
                dt = "Weekday" if "WEEKDAY" in (r.get("DAY_TYPE") or "").upper() else "Weekend/PH"
                k = (dt, hr, code)
                a, b = out.get(k, (0, 0))
                out[k] = (a + tin, b + tout)
    return out


def school_holiday_on(date_s, ranges):
    for a, b, label in ranges:
        if a <= date_s <= b:
            return label
    return None
