"""Zero-history engineering running-time estimate.

This engine does not require completed historical trips. It builds a trip time from
route geometry + live road speed + explicit engineering assumptions, then applies
day-to-day variation. Observed trips remain useful later for calibration only.
"""
import math, random

VERSION = "zero-history-1.0"


def percentile(values, p):
    if not values: return None
    a = sorted(values); x = (len(a)-1)*p/100.0; i = int(math.floor(x)); j = min(i+1, len(a)-1)
    return a[i] + (a[j]-a[i])*(x-i)


def simulate(*, drive_min, stops, route_km, pax_per_stop=3.0, junctions=None,
             recovery_min=7.0, incidents=0, roadworks=0, rain=False,
             pctl=85, draws=1000, seed=202646):
    """Component model: driving + dwell + signals + road friction + recovery.

    Parameters intentionally stay explicit/editable. Defaults are engineering assumptions,
    not claims of measured local truth. Traffic speed is expected to come from live LTA bands.
    """
    stops = max(2, int(stops or 2)); route_km = max(.1, float(route_km or .1)); drive_min = max(1., float(drive_min or 1.))
    pax = max(0., float(pax_per_stop or 0)); recovery = max(0., float(recovery_min or 0))
    # If junction count is not supplied, use a transparent engineering estimate; UI labels it as estimated.
    j_est = junctions is None
    junctions = max(0, int(round(max(stops/3.0, route_km*2.5))) if junctions is None else int(junctions))
    rnd = random.Random(seed)
    vals, comps = [], []
    for _ in range(max(100, min(10000, int(draws)))):
        # Live driving time is the centre; +/- variability represents day-to-day traffic uncertainty.
        drive = drive_min * max(.72, rnd.normalvariate(1.0, .075))
        # Engineering parameters: base dwell 5.6/6.52 sec, 1.52 sec/pax, deceleration about 8.8 sec, queue probability 8-9%.
        base_sec = rnd.uniform(5.6, 6.52)
        pax_sec = max(0., rnd.normalvariate(1.52, .12)) * max(0., rnd.normalvariate(pax, max(.5, pax*.18)))
        decel_sec = max(0., rnd.normalvariate(8.8, .8))
        queue_prob = rnd.uniform(.08, .09)
        # Queue event adds a short extra stop interaction; assumption is explicit rather than hidden.
        queue_sec = (rnd.uniform(4., 12.) if rnd.random() < queue_prob else 0.)
        dwell = (stops-2) * (base_sec + pax_sec + decel_sec + queue_sec) / 60.0
        lights = junctions * max(0., rnd.normalvariate(3.0, .55)) / 60.0
        friction = incidents * rnd.uniform(.8, 2.5) + roadworks * rnd.uniform(1.0, 3.5)
        if rain: friction += drive_min * rnd.uniform(.025, .075)
        rec = max(0., rnd.normalvariate(recovery, max(.35, recovery*.10)))
        total = drive + dwell + lights + friction + rec
        vals.append(total); comps.append((drive,dwell,lights,friction,rec))
    def cp(i,p=50): return round(percentile([x[i] for x in comps],p),1)
    return {
        "draws": len(vals), "p50": round(percentile(vals,50),1), "p85": round(percentile(vals,85),1),
        "p90": round(percentile(vals,90),1), "p95": round(percentile(vals,95),1),
        "recommended": round(percentile(vals,pctl),1), "pctl": int(pctl),
        "components_p50": {"driving":cp(0),"dwell":cp(1),"signals":cp(2),"road_friction":cp(3),"recovery":cp(4)},
        "inputs": {"route_km":round(route_km,2),"stops":stops,"pax_per_stop":round(pax,1),"junctions":junctions,
                   "junctions_estimated":j_est,"recovery_min":round(recovery,1),"incidents":int(incidents),"roadworks":int(roadworks),"rain":bool(rain)},
        "label":"MODELLED - zero-history engineering estimate",
        "note":"Initial planning estimate only. No completed-trip history is required; measured trips should later be used for calibration/validation."
    }
