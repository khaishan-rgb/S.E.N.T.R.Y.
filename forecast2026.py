"""SENTRY 2026 view models and explainable baselines. No network or credentials.

Demo demand/geometry is simulated. Live categorical loads never become invented
passenger counts. Existing strict route matching remains the traffic authority.
"""
import math
import time
import json
import sqlite3
from datetime import datetime, timezone, timedelta

HORIZONS = (0, 15, 30, 60, 120)
CATEGORIES = ("loading", "traffic", "bunching", "gap")
CBS = {"950", "160", "170", "170X"}
THRESHOLDS = {"load_elevated":70,"load_heavy":85,"load_critical":95,"bunching_minutes":3,"gap_excess":5}
SCENARIOS = [
    ("combined", "Network pressure"), ("loading", "Evening passenger surge"),
    ("accident", "AYE accident"), ("roadworks", "Orchard roadworks"),
    ("bunching", "Thomson Rd three-bus convergence"), ("gap", "Bedok departure gap"),
    ("rain", "Eastern rain front"),
]
# Approximate demonstration corridors, not licensed operational route geometry.
CORRIDORS = [
    ("14", "Bedok", [[103.85,1.302],[103.872,1.308],[103.9,1.317],[103.93,1.324],[103.945,1.326]]),
    ("166", "Bishan", [[103.817,1.279],[103.835,1.31],[103.846,1.337],[103.849,1.36],[103.838,1.39]]),
    ("12", "Pasir Ris", [[103.842,1.285],[103.875,1.31],[103.91,1.33],[103.945,1.35],[103.96,1.375]]),
    ("43", "Punggol", [[103.932,1.31],[103.91,1.34],[103.897,1.37],[103.905,1.398]]),
    ("190", "Orchard Rd", [[103.745,1.386],[103.776,1.34],[103.819,1.304],[103.835,1.3],[103.834,1.276]]),
    ("24", "Tampines", [[103.85,1.37],[103.889,1.35],[103.928,1.347],[103.963,1.345],[103.986,1.358]]),
    ("97", "AYE", [[103.742,1.333],[103.76,1.311],[103.79,1.294],[103.83,1.28],[103.86,1.285]]),
    ("853", "Yishun", [[103.835,1.426],[103.849,1.397],[103.85,1.365],[103.856,1.335]]),
    ("156", "Clementi", [[103.766,1.316],[103.8,1.332],[103.833,1.35],[103.858,1.364]]),
    ("80", "Sengkang", [[103.839,1.287],[103.86,1.32],[103.882,1.36],[103.9,1.392]]),
]


def fresh(observed, now, ttl=150):
    return isinstance(observed, (int, float)) and 0 <= now - observed <= ttl


def mean(values):
    good = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return sum(good) / len(good) if good else None


def demand_forecast(onboard, capacity, board_per_min, alight_per_min, minutes):
    """Conservation baseline; only call when counts/capacity/rates are supplied."""
    if any(x is None for x in (onboard, capacity, board_per_min, alight_per_min)) or capacity <= 0:
        return None
    passengers = max(0, min(capacity, onboard + (board_per_min-alight_per_min)*minutes))
    return {"passengers": round(passengers), "occupancy": round(passengers/capacity*100, 1), "kind": "estimated"}


def headway_risks(arrivals, scheduled, bunch_minutes=None, gap_excess=None):
    """Ordered downstream arrival times, not geographic distance. 2BB != 3BB."""
    bunch_minutes=THRESHOLDS["bunching_minutes"] if bunch_minutes is None else bunch_minutes
    gap_excess=THRESHOLDS["gap_excess"] if gap_excess is None else gap_excess
    ordered = sorted(arrivals, key=lambda x: x["eta"])
    pairs = [(ordered[i+1]["eta"]-ordered[i]["eta"]) for i in range(len(ordered)-1)]
    groups, run = [], [ordered[0]] if ordered else []
    for i, gap in enumerate(pairs):
        if gap <= bunch_minutes:
            run.append(ordered[i+1])
        else:
            if len(run) >= 2:
                groups.append(run)
            run = [ordered[i+1]]
    if len(run) >= 2:
        groups.append(run)
    return {"three": [g for g in groups if len(g) == 3], "two": [g for g in groups if len(g) == 2],
            "four_plus": [g for g in groups if len(g) >= 4],
            "gaps": [{"lead": ordered[i]["id"], "follow": ordered[i+1]["id"], "headway": round(g,1),
                      "excess": round(g-scheduled,1)} for i,g in enumerate(pairs) if scheduled and g-scheduled >= gap_excess],
            "headways": [round(g,1) for g in pairs]}


# Real road geometry for the demo services (busrouter.sg / LTA-derived, baked offline). When the
# file is missing the straight CORRIDORS above are used and labelled approximate.
def _metres(line):
    cum=[0.0]
    for a,b in zip(line,line[1:]):
        cum.append(cum[-1]+math.hypot((b[0]-a[0])*111320*math.cos(math.radians(a[1])),(b[1]-a[1])*111320))
    return cum


def point_at_m(line, cum, m):
    m=max(0,min(cum[-1],m))
    for i in range(len(cum)-1):
        if cum[i+1]>=m:
            t=0 if cum[i+1]==cum[i] else (m-cum[i])/(cum[i+1]-cum[i])
            return [round(line[i][0]+(line[i+1][0]-line[i][0])*t,6),round(line[i][1]+(line[i+1][1]-line[i][1])*t,6)]
    return list(line[-1])


def slice_m(line, cum, a, b):
    """The road stretch between two distances along the route, following every bend."""
    a,b=sorted((max(0,a),min(cum[-1],b)))
    return [point_at_m(line,cum,a)]+[list(p) for p,c in zip(line,cum) if a<c<b]+[point_at_m(line,cum,b)]


def _load_demo_geometry():
    try:
        from pathlib import Path
        data=json.loads((Path(__file__).resolve().parent/"forecast2026_demo_routes.json").read_text())
        return {g["svc"]:dict(g,cum=_metres(g["line"])) for g in data["services"]}
    except (OSError, ValueError, KeyError):
        return {}


DEMO_GEOMETRY=_load_demo_geometry()
DEMO_SOURCE="busrouter.sg / LTA DataMall route data"


def point_on(line, fraction):
    f = max(0, min(.99999, fraction))*(len(line)-1)
    i = int(f); t = f-i
    return [line[i][0]+(line[i+1][0]-line[i][0])*t, line[i][1]+(line[i+1][1]-line[i][1])*t]


def priority(severity, confidence, exposure, onset):
    """Heuristic triage score, not a calibrated probability."""
    return round(min(100, severity*confidence*(1+min(exposure,200)/200)*(1/(1+max(0,onset)/60))*30))


def alert(category, service, location, impact, horizon, point, cause, confidence=.65, direction=1, **extra):
    severity = 3 if category in ("bunching", "gap") else 2
    return dict(id=f"{category}:{service}:{direction}:{extra.get('event_id', '')}", category=category, svc=service, dir=direction,
                location=location or "Location not supplied", impact=impact, onset=horizon, point=point,
                cause=cause, confidence=confidence, score=priority(severity,confidence,80,horizon),
                status="new", evidence="estimated", **extra)


def recommendation(a):
    choices = {
        "traffic": [("Monitor / verify", "Confirm the affected carriageway and approach conditions with cameras or a bus captain.")],
        "loading": [("Assess extra capacity", "Check spare vehicles, available duties and downstream demand before deploying."), ("Regulate departure", "Review headways; keep layover, meal breaks and departed trips protected.")],
        "bunching": [("Review controlled holding", "Compare a short hold with headway balancing. Validate duty, layover and meal-break constraints.")],
        "gap": [("Compare recovery options", "Assess regulated departure, a permitted halfway entry or an available spare bus.")],
    }
    title, text = choices[a["category"]][0]
    return {"id": a["id"], "svc": a["svc"], "action": title, "reason": a["cause"], "explanation": text,
            "confidence": a["confidence"], "approval": "Controller approval required", "constraints": "Not validated — duty / vehicle data required",
            "options": [{"name": "No intervention", "headway": a.get("headway"), "feasible": "Baseline"},
                        {"name": title, "headway": None, "feasible": "Requires constraint validation"}],
            "improvement": "Not quantified without operational constraints"}


def compare_options(a, constraints=None):
    """Prototype counterfactual, never dispatch. Equal split is a demo assumption."""
    baseline={"name":"No intervention","headway":a.get("headway"),"feasible":"Baseline"}
    if constraints is None:
        return [baseline,{"name":"Additional bus / regulated departure","headway":None,"feasible":"Duty, vehicle and break data required"}]
    safe=(constraints.get("spare_buses",0)>=1 and constraints.get("layover",0)>=constraints.get("minimum_layover",7)
          and constraints.get("meal_protected") is True and not a.get("blocked"))
    return [baseline,{"name":"Demo: insert one spare at control point","headway":round(a["headway"]/2,1) if safe and a.get("headway") else None,
                      "feasible":"DEMO only: assumed spare, equal gap split, protected break" if safe else "Infeasible under supplied constraints"}]


def outcome_metrics(outcomes):
    """Calibration utility for future verified outcomes; no invented accuracy."""
    if not outcomes:
        return {"n":0,"mae":None,"precision":None,"recall":None,"false_alarm_rate":None,"miss_rate":None,"brier":None}
    labelled=[x for x in outcomes if isinstance(x.get("actual_event"),bool) and isinstance(x.get("alert"),bool)]
    tp=sum(x["alert"] and x["actual_event"] for x in labelled);fp=sum(x["alert"] and not x["actual_event"] for x in labelled)
    fn=sum(not x["alert"] and x["actual_event"] for x in labelled);tn=sum(not x["alert"] and not x["actual_event"] for x in labelled)
    ratio=lambda a,b:a/b if b else None
    return {"n":len(outcomes),"mae":mean([abs(x["predicted"]-x["actual"]) for x in outcomes if x.get("predicted") is not None and x.get("actual") is not None]),
            "precision":ratio(tp,tp+fp),"recall":ratio(tp,tp+fn),"false_alarm_rate":ratio(fp,fp+tn),"miss_rate":ratio(fn,fn+tp),
            "brier":mean([(x["probability"]-int(x["actual_event"]))**2 for x in labelled if isinstance(x.get("probability"),(int,float)) and 0<=x["probability"]<=1])}


def demo_snapshot(h, scenario="combined", tick=0, now=None):
    now = now or time.time(); h = h if h in HORIZONS else 30
    alerts, buses, services, events, routes, stops = [], [], [], [], [], []
    combined = scenario == "combined"
    for i, (svc, area, line) in enumerate(CORRIDORS):
        active = combined or (scenario == "loading" and i in (0,3,5)) or (scenario == "bunching" and i == 1) or (scenario == "gap" and i == 0)
        traffic_active = combined and i in (4,6,8) or scenario == "accident" and i == 6 or scenario == "roadworks" and i == 4 or scenario == "rain" and i in (0,2,5)
        delay = round((8+i%3)*math.exp(-h/100)*(1+min(h,30)/70),1) if traffic_active else 0
        loading = demand_forecast(58+i%4*3, 95, 1.5 if active and i in (0,3,5) else .55, .6, h)
        gaps = [max(1,8-.23*h), max(1,9-.27*h)] if active and i == 1 else [10+min(18,h*.45),10] if active and i == 0 else [9,10]
        # Simulated causal propagation: an approaching follower meets the incident,
        # while works recovery compresses consecutive downstream arrival separations.
        if traffic_active and i==6:
            gaps=[9+delay*min(h/15,1),10]
        elif traffic_active and i==4:
            compression=delay*min(h/25,1)
            gaps=[max(1,9-compression),max(1,10-compression)]
        elif traffic_active and scenario=="rain":
            gaps=[9+delay*.75*min(h/20,1),10]
        arrivals=[{"id":f"SIM-{svc}-1", "eta":2}, {"id":f"SIM-{svc}-2","eta":2+gaps[0]}, {"id":f"SIM-{svc}-3","eta":2+sum(gaps)}]
        hw = headway_risks(arrivals,10)
        geo = DEMO_GEOMETRY.get(svc)
        if geo:   # real roads: the route, its LTA stops, and the actual road stretch the event sits on
            line, cum, area = geo["line"], geo["cum"], geo["focus_road"]
            focus_m = geo["focus_m"]; a_m, b_m = geo["stretch_m"]
            route_stops = geo["stops"]
            focus = next(s for s in route_stops if s["code"]==geo["focus_stop"])
            demo_stop = {"name":focus["name"],"code":focus["code"],"sequence":focus["sequence"],"road":focus["road"]}
            stretch_stops = [{"name":s["name"],"code":s["code"],"sequence":s["sequence"],"road":s["road"]} for s in route_stops if a_m<=s["route_m"]<=b_m] or [demo_stop]
            stretch = slice_m(line, cum, a_m, b_m)
            route_mode = "demo"
        else:     # approximate straight corridor fallback
            cum = _metres(line); focus_m = cum[-1]*.5
            route_stops = [{"code":None,"name":area+" · demo stop","point":p,"sequence":j+1} for j,p in enumerate(line)]
            demo_stop = {"name":area+" · demo stop","code":None,"sequence":3}
            stretch_stops = [demo_stop]; stretch = line[1:3]; route_mode = "approximate"
        position = point_at_m(line, cum, focus_m)
        kind = ("roadworks" if i==4 else "rain" if scenario=="rain" else "accident" if i==6 else "jam") if delay else None
        routes.append({"svc":svc,"dir":1,"line":line,"mode":route_mode})
        for j,st in enumerate(route_stops):
            stops.append({"id":st["code"] or f"SIM-STOP-{i}-{j}","name":st["name"],"point":st["point"],"svc":svc,"dir":1,"seq":st.get("sequence"),
                          "km":round(st["route_m"]/1000,1) if st.get("route_m") is not None else None,"road":st.get("road"),"dest":route_stops[-1]["name"]})
        for j,a in enumerate(arrivals):
            # Simulated buses sit on the real route around the focus stop, about 320 m per minute of headway apart.
            point=point_at_m(line, cum, max(cum[-1]*.02,min(cum[-1]*.98,focus_m+250+(tick%30)*15+h*30-sum(gaps[:j])*320)))
            buses.append({"id":a["id"],"svc":svc,"dir":1,"point":point,"occupancy":loading["occupancy"],
                          "load":"LSD" if loading["occupancy"]>=THRESHOLDS["load_heavy"] else "SDA" if loading["occupancy"]>=THRESHOLDS["load_elevated"] else "SEA", "predicted":h>0,"mode":"demo"})
        service={"svc":svc,"dir":1,"operator":"SMRT" if svc=="853" else "SBST","location":area,"delay":delay,
                 "occupancy":loading["occupancy"],"heavy_buses":3 if loading["occupancy"]>=THRESHOLDS["load_heavy"] else 0,
                 "three":len(hw["three"]),"two":len(hw["two"]),"gaps":len(hw["gaps"]),
                 "headway":max(hw["headways"]),"focus_headway":hw["headways"][0],"focus_pair":{"lead":arrivals[0]["id"],"foll":arrivals[1]["id"]},"load_counts":{"SEA":3 if loading["occupancy"]<70 else 0,"SDA":3 if 70<=loading["occupancy"]<85 else 0,"LSD":3 if loading["occupancy"]>=85 else 0,"UNKNOWN":0},"bunching_details":[{"bus_order":[a["id"] for a in arrivals],"location":area,"stop":demo_stop,"headways":hw["headways"]}] if hw["three"] else [],"traffic_details":[{"event_id":f"SIM-{kind}-{svc}","road":area,"stops":stretch_stops,"kind":kind,"delay":delay}] if delay else [],"load_details":[{"bus":a["id"],"load":"LSD","stop":demo_stop,"near":area,"point":next(b["point"] for b in buses if b["id"]==a["id"] and b["svc"]==svc)} for a in arrivals] if loading["occupancy"]>=THRESHOLDS["load_heavy"] else [],"gap_details":[{"location":area,"stop":demo_stop}] if hw["gaps"] else [],"scheduled":10,"headways":hw["headways"],"point":position,"confidence":.78,"blocked":False,"building":bool(traffic_active and h<=30)}
        services.append(service)
        if loading["occupancy"]>=THRESHOLDS["load_heavy"]:
            alerts.append(alert("loading",svc,area,f'{loading["occupancy"]:.0f}% occupancy',h,position,"Simulated boarding demand exceeds available capacity.",.78,occupancy=loading["occupancy"]))
        if delay:
            event_id=f"SIM-{kind}-{svc}"
            events.append({"id":event_id,"kind":kind,"road":area,"line":stretch,"point":position,"svc":svc,"delay":delay,"confirmed":True,"mode":"demo",
                           "building":bool(h<=30),"impacts":[{"svc":svc,"dir":1,"stops":stretch_stops,"delay":delay}]})
            alerts.append(alert("traffic",svc,events[-1]["road"],f"+{delay:.1f} min",h,position,f"Simulated {kind}: same-direction route overlap.",.78,event_id=event_id,delay=delay))
        if hw["three"]:
            alerts.append(alert("bunching",svc,area,"3 consecutive buses converging",h,position,"Uneven traffic delays compress two consecutive headways.",.78,buses=[x["id"] for x in hw["three"][0]],headways=hw["headways"],persistence=7))
        if hw["gaps"]:
            gap=hw["gaps"][0]
            alerts.append(alert("gap",svc,area,f'{gap["headway"]:.0f} min headway',h,position,"Simulated departure gap plus downstream traffic delay.",.78,headway=gap["headway"],scheduled=10,excess=gap["excess"]))
    for a in alerts:
        a["evidence"]="demo"
    return {"services":services,"alerts":alerts,"buses":buses,"events":events,"routes":routes,"stops":stops,
            "rain":[{"point":[103.92,1.34],"mmh":18},{"point":[103.95,1.37],"mmh":26}] if scenario=="rain" else [],"coverage":{"loading":True,"traffic":True,"bunching":True,"gap":True},"updated":now,"mode":"demo",
            "weather":{"text":"Simulated rain" if scenario=="rain" else "Partly cloudy · simulated","temperature":None},
            "sources":[{"name":"Scenario simulator","status":"demo","detail":"Buses, occupancy and incidents are simulated. "+("Routes and stops are real road geometry ("+DEMO_SOURCE+")." if DEMO_GEOMETRY else "Route corridors are approximate straight lines.")}]}


def traffic_extra(s,h):
    extra=s.get("extra") or {}
    if h==0:
        return None  # live incident delay is distinct from future service running time
    sample=extra.get(h,extra.get(str(h)))
    if isinstance(sample,dict):
        return sample.get("mid")
    # Explicit longer-horizon statistical baseline, never called an observation.
    if h==120:
        x=extra.get(60,extra.get("60")) or {}
        if x.get("mid") is not None:
            return round(x["mid"]*(1.15 if s.get("building") else math.exp(-60/90)),1)
    return None


def stop_context(stops, location=None, index=None):
    """Use the route's actual LTA sequence; never substitute array index + 1."""
    chosen=None
    if isinstance(index,int) and 0<=index<len(stops):chosen=stops[index]
    elif location:
        found=[s for s in stops if s.get('name')==location or (s.get('code') and str(s['code']) in str(location))]
        if len(found)==1:chosen=found[0]
    if chosen is None:return None
    return {'code':str(chosen['code']) if chosen.get('code') is not None else None,'name':chosen.get('name'),
            'sequence':chosen.get('seq'), 'road':chosen.get('road'),
            'point':[chosen['lon'],chosen['lat']] if chosen.get('lat') is not None and chosen.get('lon') is not None else None}


def road_missing(road):
    return not road or str(road).strip().lower().startswith("road name not supplied")


def nearest_stop(stops, point, max_m=400):
    """The route's own LTA stop closest to an event point (used when the provider gives no road / stretch)."""
    if not point:return None
    best=None
    for i,s in enumerate(stops):
        if s.get('lat') is None or s.get('lon') is None:continue
        d=math.hypot((s['lon']-point[0])*111320*math.cos(math.radians(point[1])),(s['lat']-point[1])*111320)
        if best is None or d<best[0]:best=(d,i)
    return stop_context(stops,index=best[1]) if best and best[0]<=max_m else None


def impact_stops(stops, start, end):
    if start is None or end is None:return []
    a,b=sorted((start,end));valid=[(i,s) for i,s in enumerate(stops) if s.get('route_m') is not None]
    inside=[i for i,s in valid if a<=s['route_m']<=b]
    if not inside:
        before=[(i,s) for i,s in valid if s['route_m']<a and a-s['route_m']<=500]
        after=[(i,s) for i,s in valid if s['route_m']>b and s['route_m']-b<=500]
        inside=([max(before,key=lambda p:p[1]['route_m'])[0]] if before else [])+([min(after,key=lambda p:p[1]['route_m'])[0]] if after else [])
    return [stop_context(stops,index=i) for i in inside[:8]]


def headway_at(series, horizon):
    """Interpolate inside collector coverage (e.g. 10/20 -> 15); never extrapolate."""
    points=sorted((float(k),v) for k,v in (series or {}).items() if isinstance(v,(int,float)) and math.isfinite(v))
    for t,v in points:
        if t==horizon:return v
    for (a,x),(b,y) in zip(points,points[1:]):
        if a<horizon<b:return round(x+(y-x)*(horizon-a)/(b-a),1)
    return None


def group_headways(group, horizon):
    if horizon==0:return group.get('headways')
    forecasts=group.get('headway_forecasts') or {}
    count=max([len(v) for v in forecasts.values() if isinstance(v,list)] or [0])
    return [headway_at({h:v[i] for h,v in forecasts.items() if isinstance(v,list) and i<len(v)},horizon) for i in range(count)] or None


def live_snapshot(raw, samples, h, now=None):
    now=now or time.time(); alerts=[]; services=[]; buses=[]
    age=raw.get("age_s",0); traffic_ok=bool(raw.get("services")) and age<=150 and not raw.get("error") and any((raw.get("sources",{}).get(k) or {}).get("status")=="live" for k in ("tomtom_incidents","tomtom_tiles","tomtom_flow","waze"))
    events=[]
    for e in raw.get("events") or []:
        if e.get("status")!="confirmed" or not traffic_ok:
            continue
        line=e.get("line") or []; point=e.get("pt") or (line[len(line)//2] if line else None)
        touched=e.get("services") or []
        events.append({"id":e["id"],"kind":e["kind"],"road":e.get("road") or "Road name not supplied","point":point,"line":line,
                       "services":[x["svc"] for x in touched],"service_dirs":[[x["svc"],x["dir"]] for x in touched],"matches":touched,"delay":((e.get("delay_s") or 0)/60 if h==0 else ((e.get("fc") or {}).get(h,(e.get("fc") or {}).get(str(h))) or {}).get("mid",0)/60 if h<=60 else (e.get("delay_s") or 0)/60*math.exp(-120/90)),"confirmed":True,"mode":"live"})
    for event, source in zip(events, [e for e in raw.get('events') or [] if e.get('status')=='confirmed' and traffic_ok]):
        if h==0:
            if source.get('current_travel_time_s') is not None and source.get('typical_travel_time_s') is not None:
                event['delay']=max(0,(source['current_travel_time_s']-source['typical_travel_time_s'])/60)
                event['delay_basis']='Current minus historical typical travel time'
            elif source.get('delay_s') is None:
                event['delay']=None
    keys={(s["svc"],s["dir"]):s for s in raw.get("services") or []}
    samples_by={(str(s.get("service") or "").upper(),s.get("direction")):s for s in samples}
    for key in set(keys)|set(samples_by):
        s=keys.get(key,{"svc":key[0],"dir":key[1]}); sample=samples_by.get(key,{})
        observed=fresh(sample.get("at"),now) and not sample.get("error")
        valid=observed and sample.get("issue")!="No data"
        seq=(sample.get("seq") or sample.get("observed_buses") or []) if observed else []
        known_loads=[b for b in seq if b.get("load") in ("SEA","SDA","LSD")]
        heavy=sum(b.get("load")=="LSD" for b in known_loads)
        point=next(([b["lon"],b["lat"]] for b in seq if b.get("lat") and b.get("lon")),None)
        ev=next((e for e in events if list(key) in e.get("service_dirs",[])),None)
        point=ev.get("point") if ev and ev.get("point") else point
        delay=traffic_extra(s,h) if traffic_ok else None
        if h==0:
            linked=[e for e in events if list(key) in e.get("service_dirs",[])]
            values=[e["delay"] for e in linked if e["delay"] is not None]
            delay=(max(values) if values else None if linked else 0) if traffic_ok else None
        if s.get("blocked") and not delay:
            delay=None  # a closure without a quantified delay is not zero extra time
        # Forecast categories use collector arrival-time models within their actual coverage.
        # Same rules and counts as the Bunching & Gap page (one engine: bunching.py, its settings apply to both pages):
        # 3BB now = groups of exactly 3 buses that are bunched now (confirmed or developing) = the page's "3BB" count;
        # long headway now = gaps that are long NOW (scheduled + gap_add_min, first gap_skip_stops stops ignored) = the
        # Bunching & Gap page's live "Long headway" card (bunching_live.py).
        # Forecast horizons: the engine's predicted onset (t_occur) within its 30-min horizon.
        groups=[g for g in sample.get("groups",[]) if g.get("size")==3 and valid and
                ((h==0 and g.get("status") in ("confirmed","developing")) or (h>0 and h<=30 and g.get("t_occur") is not None and g["t_occur"]<=h))]
        gaps=[g for g in sample.get("gaps",[]) if valid and ((h==0 and g.get("active")) or (0<h<=30 and g.get("t_occur") is not None and g["t_occur"]<=h))]
        stops=sample.get('route_stops') or (raw.get('route_context') or {}).get(f'{key[0]}:{key[1]}',[])
        traffic_details=[]
        for event in events:
            match=next((m for m in event.get('matches',[]) if (m['svc'],m['dir'])==key),None)
            if match is not None:
                affected=impact_stops(stops,match.get('a_m'),match.get('b_m'))
                if not affected:   # no verified stretch: name the route's nearest stop to the event instead of "unavailable"
                    near=nearest_stop(stops,event.get('point'))
                    affected=[near] if near else []
                road=event['road']
                if road_missing(road):road=match.get('road')
                if road_missing(road):road=next((x.get('road') for x in affected if x and not road_missing(x.get('road'))),None)
                if road_missing(road):road=('Near '+affected[0]['name']) if affected and affected[0].get('name') else event['road']
                traffic_details.append({'event_id':event['id'],'road':road,'kind':event['kind'],
                                        'stops':affected,
                                        'relation':'Stops on / bracketing the verified affected route stretch','delay':event['delay']})
                event.setdefault('impacts',[]).append({'svc':key[0],'dir':key[1],'stops':affected,'delay':event['delay']})
        bunch_details=[{'bus_order':g.get('ids') or [],'stop':stop_context(stops,g.get('location'),g.get('stop_j')),
                        'location':g.get('location'),'headways':group_headways(g,h),
                        'status':g.get('status'),'onset':g.get('t_occur'),'persistence':g.get('stops')} for g in groups]
        gap_details=[{'stop':stop_context(stops,g.get('location'),g.get('stop_j')),'location':g.get('location'),
                      'headway':g.get('max_hw'),'lead':g.get('lead'),'follow':g.get('foll')} for g in gaps]
        counts={c:sum(b.get('load')==c for b in seq) for c in ('SEA','SDA','LSD')}
        load_details=[{'bus':b.get('id'),'load':'LSD','near':b.get('near'),'stop':stop_context(stops,b.get('near')),'bus_point':[b['lon'],b['lat']] if b.get('lat') and b.get('lon') else None} for b in seq if b.get('load')=='LSD'] if h==0 and observed else []
        for d in load_details:d['point']=(d['stop'] or {}).get('point') or d['bus_point'] or point
        counts['UNKNOWN']=len(seq)-sum(counts.values())
        location=(traffic_details[0]["road"] if traffic_details else None) or (ev or {}).get("road") or sample.get("location") or "Location not supplied"
        row={"svc":key[0],"dir":key[1],"location":location,"delay":delay,"blocked":False,"building":bool(s.get("building")),"occupancy":None,
             "heavy_buses":heavy if h==0 and observed and known_loads else None,"load_label":"Heavy" if heavy else "Standing" if any(b.get("load")=="SDA" for b in seq) else "Seats" if known_loads else "Unavailable",
             "three":len(groups) if valid and h<=30 else None,"two":sum(g.get("size")==2 for g in sample.get("groups",[])) if valid and h==0 else None,
             "gaps":len(gaps) if valid and h<=30 else None,"headway":(max([g.get("max_hw") or 0 for g in gaps] or [0]) or headway_at(sample.get("hw"),h)) if valid and h<=30 else None,
             "scheduled":sample.get("sched_hw"),"headways":sample.get("hw") or {},"focus_headway":headway_at(sample.get("hw"),h) if valid and h<=30 else None,"focus_pair":sample.get("focus") or {},"load_counts":counts,"load_details":load_details,"bunching_details":bunch_details,"traffic_details":traffic_details,"gap_details":gap_details,"point":point,"confidence":.55 if h==120 else .7}
        services.append(row)
        if delay and delay>0 or row["blocked"]:
            alerts.append(alert("traffic",key[0],location,"Route blocked" if row["blocked"] else f"+{delay:.1f} min",h,point,("Verified same-direction incident/route match. Statistical travel-time baseline." if ev else "Route-specific speed or rainfall estimate; incident association not verified."),row["confidence"],direction=key[1],affected_stops=[stop for detail in traffic_details for stop in detail["stops"]],delay=delay,blocked=row["blocked"],event_id=(ev or {}).get("id","")))
        def at(stop=None,bus=None):
            if stop and stop.get("point"):return stop["point"]
            if bus and bus.get("lat") and bus.get("lon"):return [bus["lon"],bus["lat"]]
            return point
        heavy_bus=next((b for b in seq if b.get("load")=="LSD"),None)
        if heavy and h==0:
            alerts.append(alert("loading",key[0],(load_details[0]["stop"] or {}).get("name") or load_details[0].get("near") or location if load_details else location,f"{heavy} sampled buses: limited standing",0,at((load_details[0]["stop"] if load_details else None),heavy_bus),"Observed LTA categorical load; exact occupancy is unavailable.",.7,direction=key[1]))
        for group in groups:
            alerts.append(alert("bunching",key[0],group.get("location"),f"Three-bus bunching ({group.get('status') or 'predicted'})",group.get("t_occur") or 0,at(stop_context(stops,group.get("location"),group.get("stop_j"))),"Collector downstream arrival-time headways, not bus distance.",.7,direction=key[1],event_id="bb:"+":".join(map(str,group.get("ids") or [])),stop=stop_context(stops,group.get("location"),group.get("stop_j")),buses=group.get("ids"),persistence=group.get("stops")))
        for gap in gaps:
            scheduled=sample.get("sched_hw")
            if scheduled and (gap.get("max_hw") or 0)-scheduled>=5:
                alerts.append(alert("gap",key[0],gap.get("location"),f'{gap.get("max_hw")} min headway',gap.get("t_occur") or 0,at(stop_context(stops,gap.get("location"),gap.get("stop_j"))),f"Bunching & Gap engine: headway at or above scheduled + {(raw.get('bb_params') or {}).get('gap_add_min',10):g} min ({gap.get('state') or 'predicted'}).",.7,direction=key[1],event_id=f"gap:{gap.get('lead')}:{gap.get('foll')}",stop=stop_context(stops,gap.get("location"),gap.get("stop_j")),headway=gap.get("max_hw"),scheduled=scheduled))
        # Live coordinates remain live coordinates at future horizons: no fabricated positions.
        for b in seq:
            if b.get("lat") and b.get("lon"):
                buses.append({"id":f"{key[0]}:{key[1]}:{b['id']}","svc":key[0],"dir":key[1],"point":[b["lon"],b["lat"]],"load":b.get("load"),"occupancy":None,"predicted":False,"mode":"live"})
    for event in events:
        if road_missing(event.get("road")):
            named=[t["road"] for s_ in services for t in s_["traffic_details"] if t.get("event_id")==event["id"] and not road_missing(t.get("road"))]
            if named:event["road"]=named[0]
    return {"mode":"live","services":services,"alerts":alerts,"buses":buses,"events":events,"routes":[],"stops":[],
            "coverage":{"traffic":traffic_ok,"loading":h==0 and any(s["heavy_buses"] is not None for s in services),"bunching":h<=30 and any(s["three"] is not None for s in services),"gap":h<=30 and any(s["gaps"] is not None for s in services)},
            "updated":now-age,"weather":{"text":((raw.get("rain") or {}).get("summary") or "NEA observations in source panel"),"temperature":None},
            "sources":[{"name":k,"status":v.get("status","unavailable"),"detail":v.get("detail","")} for k,v in (raw.get("sources") or {}).items()],
            "rain":[{"point":[s["lon"],s["lat"]],"mmh":s.get("mmh",0)} for s in (raw.get("rain") or {}).get("stations",[]) if s.get("lat") and s.get("lon") and s.get("mmh",0)>0],"calibration":raw.get("calibration") or {},"error":raw.get("error")}


from forecast2026_amendments import filter_traffic, delay_threshold, meets_delay, master_schedule


BB_DEFAULT_RULES={"bunch_min":3.0,"confirm_stops":15,"gap_add_min":10.0,"gap_stops":15,"gap_skip_stops":3,"horizon_min":30}


def build_centre(mode="demo", horizon=30, scenario="combined", tick=0, raw=None, samples=None, services=None, exclude_cbs=False, category="all", now=None, delay_min=.8):
    if horizon not in HORIZONS:
        raise ValueError("Unsupported forecast horizon")
    now=now or time.time(); chosen={s.upper() for s in services or []}; samples=samples or []
    snapshots={h:demo_snapshot(h,scenario,tick,now) if mode=="demo" else live_snapshot(raw or {},samples,h,now) for h in HORIZONS}
    rules={k:(raw or {}).get("bb_params",{}).get(k,BB_DEFAULT_RULES[k]) for k in BB_DEFAULT_RULES}
    for snap in snapshots.values():
        snap["rules"]=rules   # Bunching & Gap engine settings, shown on the cards
        snap["context_routes"]=[r for r in snap["routes"] if not exclude_cbs or r["svc"] not in CBS]
        filter_traffic(snap,delay_min)
        keep=lambda s:(not chosen or s["svc"] in chosen) and (not exclude_cbs or s["svc"] not in CBS)
        snap["services"]=[s for s in snap["services"] if keep(s)]
        included={s["svc"] for s in snap["services"]}
        snap["alerts"]=[a for a in snap["alerts"] if a["svc"] in included]
        snap["buses"]=[b for b in snap["buses"] if b["svc"] in included]
        snap["routes"]=[r for r in snap["routes"] if r["svc"] in included]
        snap["stops"]=[r for r in snap["stops"] if r["svc"] in included]
        snap["events"]=[e for e in snap["events"] if e.get("svc") in included or any(s in included for s in e.get("services",[]))]
        for event in snap['events']:
            if event.get('services'):
                event['services']=[s for s in event['services'] if s in included]
                event['service_dirs']=[p for p in event.get('service_dirs',[]) if p[0] in included]
            if event.get('impacts'):
                event['impacts']=[p for p in event['impacts'] if p['svc'] in included]
        snap["kpis"]={c:{"count":sum((s.get({"loading":"heavy_buses","bunching":"three","gap":"gaps"}.get(c,"delay")) or 0) for s in snap["services"]) if c!="traffic" else sum(1 for e in snap["events"] if e.get("point") or e.get("line")),
                           "services":len({a["svc"] for a in snap["alerts"] if a["category"]==c}) if c!="traffic" else len({x for e in snap["events"] for x in ([e["svc"]] if e.get("svc") else e.get("services",[]))}),"earliest":min([a["onset"] for a in snap["alerts"] if a["category"]==c] or [0]),"available":snap["coverage"][c]} for c in CATEGORIES}
        snap["kpis"]["loading"]["occupancy"]=max([s["occupancy"] for s in snap["services"] if s.get("occupancy") is not None] or [0]) or None
        snap["kpis"]["traffic"]["delay"]=mean([s["delay"] for s in snap["services"] if s.get("delay") and not s.get("blocked")])
        snap["kpis"]["gap"]["headway"]=max([s.get("headway") or 0 for s in snap["services"] if s.get("gaps")] or [0]) or None
    current=snapshots[horizon]
    current["alerts"].sort(key=lambda a:(-a["score"],a["onset"],a["svc"]))
    if category in CATEGORIES:
        # A selected card shows only its own category, on the map as well as in the lists.
        current["alerts"]=[a for a in current["alerts"] if a["category"]==category]
        ids={a["svc"] for a in current["alerts"]}
        if category=="traffic":   # every incident the traffic card counts, and the services it hits
            ids|={x for e in current["events"] for x in ([e["svc"]] if e.get("svc") else e.get("services",[]))}
        else:
            current["events"]=[]
        for key in ("services","buses","routes","stops"):
            current[key]=[x for x in current[key] if x["svc"] in ids]
        if category!="loading":   # heavy-load bus markers belong to the loading card only
            current["services"]=[dict(x,load_details=[]) for x in current["services"]]
    advice=[recommendation(a) for a in current["alerts"][:4]]
    if mode=="demo":
        for a,r in zip(current["alerts"][:4],advice):
            r["options"]=compare_options(a,{"spare_buses":1,"layover":10,"minimum_layover":7,"meal_protected":True})
            if a.get("headway") and r["options"][1]["headway"] is not None:
                r["improvement"]=f"DEMO equal-split headway: {a['headway']:.1f} to {r['options'][1]['headway']:.1f} min; assumes spare insertion at control point."
            r["constraints"]="DEMO assumptions: one spare; 10-min layover; 7-min minimum; meal protected. No live duty validation."
    return dict(current,horizon=horizon,generated=now,scenarios=[{"id":k,"label":v} for k,v in SCENARIOS],
                timeline=[{"h":h,**snapshots[h]["kpis"]} for h in HORIZONS],
                service_series=[{"h":h,"services":[{k:s.get(k) for k in ("svc","dir","headway","focus_headway","scheduled","occupancy","delay","confidence")} for s in snapshots[h]["services"]]} for h in HORIZONS],
                recommendations=advice,
                headway_history=(raw or {}).get("headway_history",[]),
                headway_schedule=(raw or {}).get("headway_schedule",[]),
                model={"name":"Explainable statistical baselines","trained":False,"validated":False,"confidence_note":"Heuristic evidence quality, not measured forecast accuracy.",
                       "thresholds":THRESHOLDS,"outcome_metrics":outcome_metrics([]),
                       "limitations":["Live occupancy percentages need demand/capacity data.","Live bunching/gap predictions use the collector's supported arrival-time horizon (up to 30 min).","120-min traffic is an explicitly estimated decay baseline.","Operational recommendations require duty and welfare constraint validation."]})


class AlertLedger:
    """Isolated namespaces prevent demo/live, filters and horizon changes resolving each other."""
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS fc26_alert(ns TEXT,id TEXT,status TEXT,score INTEGER,first REAL,last REAL,body TEXT,PRIMARY KEY(ns,id))")
            db.execute("CREATE TABLE IF NOT EXISTS fc26_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,ns TEXT,id TEXT,action TEXT,who TEXT,ts REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS fc26_ack(key TEXT PRIMARY KEY,who TEXT,ts REAL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def sync(self, ns, alerts, now, can_resolve=False):
        with self.connect() as db:
            for a in alerts:
                old = db.execute("SELECT status,score FROM fc26_alert WHERE ns=? AND id=?", (ns,a["id"])).fetchone()
                action = "new" if not old else "reopened" if old[0]=="resolved" else "worsened" if a["score"]>=old[1]+15 else None
                status = "new" if action else old[0]
                db.execute("INSERT INTO fc26_alert VALUES(?,?,?,?,?,?,?) ON CONFLICT(ns,id) DO UPDATE SET status=excluded.status,score=excluded.score,last=excluded.last,body=excluded.body",
                           (ns,a["id"],status,a["score"],now,now,json.dumps(a)))
                if action:
                    db.execute("INSERT INTO fc26_audit(ns,id,action,who,ts) VALUES(?,?,?,?,?)",(ns,a["id"],action,"engine",now))
            if can_resolve:
                seen={a["id"] for a in alerts}
                for aid, status in db.execute("SELECT id,status FROM fc26_alert WHERE ns=?",(ns,)).fetchall():
                    if aid not in seen and status!="resolved":
                        db.execute("UPDATE fc26_alert SET status='resolved' WHERE ns=? AND id=?",(ns,aid))
                        db.execute("INSERT INTO fc26_audit(ns,id,action,who,ts) VALUES(?,?,?,?,?)",(ns,aid,"resolved","engine",now))

    def transition(self, ns, aid, status, who, now):
        if status not in ("acknowledged","monitoring","resolved"):
            raise ValueError("Unsupported alert transition")
        with self.connect() as db:
            old=db.execute("SELECT status FROM fc26_alert WHERE ns=? AND id=?",(ns,aid)).fetchone()
            if not old:
                return False
            if old[0]==status:
                return True
            db.execute("UPDATE fc26_alert SET status=? WHERE ns=? AND id=?",(status,ns,aid))
            db.execute("INSERT INTO fc26_audit(ns,id,action,who,ts) VALUES(?,?,?,?,?)",(ns,aid,status,who,now))
        return True

    ACK_TTL = 12 * 3600     # an acknowledgement is kept 12 h; the card itself disappears when the problem clears

    def ack(self, key, who, now, undo=False):
        """Acknowledge one board card (shared by every controller). key = the card key, e.g. '3bb:89:1:B1-B2-B3:0'."""
        key = str(key)[:200]
        with self.connect() as db:
            if undo:
                db.execute("DELETE FROM fc26_ack WHERE key=?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO fc26_ack VALUES(?,?,?)", (key, str(who)[:80], now))
            db.execute("INSERT INTO fc26_audit(ns,id,action,who,ts) VALUES(?,?,?,?,?)", ("board", key, "unacknowledged" if undo else "acknowledged", str(who)[:80], now))
        return True

    def acks(self, now):
        with self.connect() as db:
            db.execute("DELETE FROM fc26_ack WHERE ts<?", (now - self.ACK_TTL,))
            return {k: {"who": w, "ts": t} for k, w, t in db.execute("SELECT key,who,ts FROM fc26_ack")}

    def view(self, ns):
        with self.connect() as db:
            records=db.execute("SELECT body,status,first,last FROM fc26_alert WHERE ns=? ORDER BY score DESC,last DESC LIMIT 250",(ns,)).fetchall()
            audit=db.execute("SELECT id,action,who,ts FROM fc26_audit WHERE ns=? ORDER BY seq DESC LIMIT 100",(ns,)).fetchall()
        return [dict(json.loads(b),status=s,first=f,last=l) for b,s,f,l in records], [dict(id=i,action=a,who=w,ts=t) for i,a,w,t in audit]


def simplify_latlon(pts, tol_m=5.0, nd=5):
    """[(lat, lon)] -> [[lon, lat]] keeping every bend (Douglas-Peucker, metres). Far fewer points than uniform sampling."""
    pts=[(float(a),float(b)) for a,b in pts]
    if len(pts)<3:return [[round(b,nd),round(a,nd)] for a,b in pts]
    lat0=math.radians(sum(p[0] for p in pts)/len(pts));P=[(p[1]*111320*math.cos(lat0),p[0]*111320) for p in pts]
    keep=[False]*len(P);keep[0]=keep[-1]=True;stack=[(0,len(P)-1)]
    while stack:
        a,b=stack.pop();ax,ay=P[a];bx,by=P[b];dx,dy=bx-ax,by-ay;L=dx*dx+dy*dy;best,bi=-1.0,None
        for i in range(a+1,b):
            px,py=P[i];t=0 if L==0 else max(0,min(1,((px-ax)*dx+(py-ay)*dy)/L));d=math.hypot(px-ax-t*dx,py-ay-t*dy)
            if d>best:best,bi=d,i
        if bi is not None and best>tol_m:keep[bi]=True;stack+=[(a,bi),(bi,b)]
    return [[round(p[1],nd),round(p[0],nd)] for p,k in zip(pts,keep) if k]
