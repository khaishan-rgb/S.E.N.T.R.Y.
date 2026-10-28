"""Pure presentation policies; no provider calls or proximity-based impact inference."""
import math
from datetime import datetime, timezone, timedelta


def delay_threshold(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 180:
        raise ValueError('Delay threshold must be between 0 and 180 minutes')
    return value


def meets_delay(row, minimum):
    return bool(row.get('blocked') or row.get('kind') == 'road_closed' or
                isinstance(row.get('delay'), (float, int)) and row['delay'] > 0 and row['delay'] >= minimum)


NOISE_WITHOUT_DELAY = {'jam', 'hazard', 'other'}


def event_shown(event, minimum):
    """The Traffic delay filter applies to every event with a measured delay (jams, roadworks, incidents).
    Closures always show. Incidents / roadworks with NO delay figure still show (delay not reported, cannot be judged)."""
    if event.get('kind') == 'road_closed' or event.get('blocked'):
        return True
    delay = event.get('delay')
    if not isinstance(delay, (float, int)) or math.isnan(delay):
        # No delay figure: keep physical obstructions and approved works; drop low-information point
        # reports (LTA road blocks / obstacles / misc.) that cannot be judged against the filter.
        return event.get('kind') not in NOISE_WITHOUT_DELAY
    if minimum <= 0:
        return delay > 0 or event.get('kind') not in NOISE_WITHOUT_DELAY
    return delay >= minimum


def filter_traffic(snapshot, minimum):
    """Threshold verified impacts, retaining safety closures independently of delay."""
    minimum = delay_threshold(minimum)
    snapshot['events'] = list({str(e['id']): e for e in snapshot['events'] if event_shown(e, minimum)}.values())
    # Incidents / works remain visible as source evidence. Their unknown / small
    # delays do not become qualifying congestion or inflate the congestion KPI.
    for event in snapshot['events']:
        event['congestion_eligible'] = meets_delay(event, minimum)
        event.setdefault('delay_basis', 'SIMULATED' if snapshot['mode'] == 'demo' else 'Provider-reported / free-flow baseline; typical-time history unavailable')
    snapshot['alerts'] = [a for a in snapshot['alerts'] if a['category'] != 'traffic' or meets_delay(a, minimum)]
    for row in snapshot['services']:
        row['traffic_eligible'] = meets_delay(row, minimum)
        row['unfiltered_delay'] = row.get('delay')
        if not row['traffic_eligible']:
            row['delay'] = 0 if row.get('delay') == 0 else None
            row['building'] = False
        row['traffic_details'] = [t for t in row.get('traffic_details', [])
                                  if row.get('blocked') or meets_delay(t, minimum)]
    snapshot['delay_filter'] = {'minimum': minimum, 'yellow': .8, 'orange': 2, 'red': 5,
                              'baseline': 'SIMULATED' if snapshot['mode'] == 'demo' else 'Provider-reported / free-flow; historical typical time only where supplied',
                              'closures_retained': True}
    return snapshot


def select_cameras(cameras, point=None, now=None, limit=6):
    """Nearest (default six) visual-context cameras, never evidence of a route impact."""
    now = now or datetime.now(timezone.utc).timestamp()
    unique = {}
    for camera in cameras:
        if not camera.get('id') or not camera.get('link') or camera.get('lat') is None or camera.get('lon') is None:
            continue
        age = None
        try:
            captured = datetime.fromisoformat(str(camera.get('taken')).replace('Z', '+00:00'))
            if captured.tzinfo is not None:
                age = max(0, (now - captured.timestamp()) / 60)
        except (TypeError, ValueError):
            pass
        distance = None
        if point:
            lon, lat = point
            p1, p2 = math.radians(lat), math.radians(camera['lat'])
            a = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(camera['lon']-lon)/2)**2
            distance = 6371 * 2 * math.asin(min(1, math.sqrt(a)))
        row = {'id': str(camera['id']), 'image': camera['link'], 'lat': camera['lat'], 'lon': camera['lon'],
               'label': camera.get('road') or camera.get('desc') or 'Camera '+str(camera['id']),
               'taken': camera.get('taken'), 'age_minutes': round(age, 1) if age is not None else None,
               'stale': age is None or age > 10, 'source': camera.get('src'),
               'distance_km': round(distance, 2) if distance is not None else None}
        unique[row['id']] = row
    rows = list(unique.values())
    if point:
        rows.sort(key=lambda c: c['distance_km'])
    else:
        rows.sort(key=lambda c: (c['stale'], c['age_minutes'] if c['age_minutes'] is not None else math.inf, c['id']))
    return rows[:limit]


def master_schedule(masters, keys, now):
    """Expand configured time windows only; never reuse a peak band all day."""
    sgt = timezone(timedelta(hours=8))
    local = datetime.fromtimestamp(now, sgt)
    start = local.replace(hour=5, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    rows = []
    for index in range(96):
        at = start + timedelta(minutes=index*15+7.5)
        day = 'Weekday' if at.weekday() < 5 else 'Saturday' if at.weekday() == 5 else 'Sunday'
        minute = at.hour*60+at.minute
        for key in keys:
            matches = []
            for master in masters:
                if (master.get('service'), master.get('direction')) != key or master.get('day_type') not in ('All', day):
                    continue
                try:
                    begin, end = (sum(int(v)*m for v,m in zip(master[field].split(':'), (60,1))) for field in ('from','to'))
                    value = float(master['hw'])
                    if begin <= minute < end and math.isfinite(value) and value > 0:
                        matches.append((end-begin, value))
                except (KeyError, ValueError, TypeError):
                    continue
            if matches:
                rows.append({'svc':key[0], 'dir':key[1], 'at':at.timestamp(), 'headway':min(matches)[1],
                             'verified':True, 'source':'Configured Service Headway Master'})
    return rows
