# SG Transport Pulse V5.1 — Route Traffic + Pre-emptive Departure Adjustment

Live bus positions, live LTA traffic drawn directly on the bus route, and next arrivals per stop.
FastAPI backend + a single-page Leaflet frontend (OneMap basemap, OpenStreetMap fallback).

## NEW in V5.1: Headway Control page (`/control`) - pre-emptive departure adjustment
For each selected **Service + Direction** (up to 8 services, 16 service/direction pairs) it predicts every bus and its
terminal arrival, then runs five checks and suggests a departure-headway (HW) change:

| # | Check | Fires when | Suggestion |
|---|---|---|---|
| 1 | En-route | a leading bus is well ahead of the bus behind **and** that gap is growing | Comm BC / regulate spacing |
| 2 | Traffic | congestion ahead of 2+ buses **and** terminal arrivals predicted late | temporarily longer HW (10 -> 11/12/13) |
| 3 | Last 15% recovery | a longer-HW adjustment is active **and** affected buses are in the last 15% / clear and near on-time | cancel, restore original HW (13 -> 10) |
| 4 | Early arrival | 2+ consecutive buses in the last 15% **and** predicted > 5 min early | temporarily shorter HW (10 -> 9) |
| 5 | Normalisation | an adjustment is active but service is back to normal | restore scheduled HW |

The page shows a risk table (Critical / Developing / Stable / Insufficient data), a route strip (buses, gaps, congestion,
final-15% zone, predicted terminal arrival times), the five check results, a suggested action with the re-spaced
next-departure ladder, a draft message for the Bus Captain, a session headway-trend chart, and CSV export.

**Table columns (modelled on the "Early Gap Detection" mock):** risk, service/dir, scheduled HW, departure HW now, route-vs-plan,
**Predicted HW at +10 / +20 / +30 min** (gap between the terminal arrivals that straddle each horizon), **Time to critical**
(minutes until predicted HW leaves 0.5x-1.5x of scheduled), **Likely cause** (from the triggered check), suggestion, status.
There is a search box, a risk filter, and CSV export. The detail view adds **Next arrivals at terminal**: each bus's terminal ETA,
its predicted gap to the previous arrival, and a suggested hold (+) / advance (-) of its next departure toward the scheduled HW.

**Not in this version (compared with the mock):** Control Point filter (En-route / Interchange), Map View toggle, bus registration
numbers (LTA does not publish them; buses are #1, #2...), and a language-model explanation.

**The suggestions are deterministic rules, not an AI/LLM call**, so every recommendation is explainable and every
threshold is listed on the page (`headway.CFG`, returned by the API). Use as decision support only.

### What the data can and cannot say (please read)
LTA DataMall has **no operator timetable or dispatch times**, so:
* **Scheduled HW** = midpoint of LTA `BusServices` dispatch frequency for the current time band (AM peak 06:30-08:30, AM off-peak
  08:31-16:59, PM peak 17:00-19:00, PM off-peak). If unavailable, the observed median gap is used and labelled.
* **Gaps** = differences of LTA's estimated arrival times at the same stop; "growing" compares a nearer and a farther stop.
* **Late / early** = predicted full-route running time (LTA speed bands + dwell) minus a **planned running time**. The plan is
  **estimated** (free-flow drive x 1.15 + dwell). **Enter your real timetable running time** in the service's detail panel to replace it -
  rules 2-4 are only as good as this number.
* Buses are only seen if LTA lists them among the next 3 at a sampled stop; a bus far from every sampled stop can be missed.
* "Apply / Undo" are stored in the browser (localStorage) to drive rules 3-5. **Nothing is sent to any dispatch system.**
* With fewer than 2 live buses the service shows "Insufficient data" (never a false "Stable"). If the traffic feed is down,
  checks 2-4 show n/a and no delay is guessed.

### New API
`GET /api/control?services=32,145&direction=0|1|2&adj=32:1:13&plan=32:1:45`
(`adj` = HW already applied; `plan` = timetable running time in minutes). New files: `headway.py` (engine), `control.html` (page).

## Fix: "TRAFFIC FEED ERROR ... 404 Not Found"
LTA's current DataMall guide (v6.9, 3 Aug 2026) serves speed bands from **`v4/TrafficSpeedBands`**.
V4 of this app called `v3/TrafficSpeedBands`, which no longer exists. Also corrected:
`BusArrivalv3` -> **`v3/BusArrival`**.

* `v4/TrafficSpeedBands` is tried first; older paths are tried only if LTA answers 404.
* If LTA bumps the version again, set env var `LTA_SPEED_PATH` (e.g. `v5/TrafficSpeedBands`) - no code change.
* Paging is parallel and no longer capped (old code stopped at 30,000 speed bands / 50 pages of routes).
* Open **`/api/traffic-test`** after deploying: it shows the endpoint that worked and the segment count.

## What's in the UI
* Route line coloured by LTA speed band: green >= 40 km/h (bands 5-8), amber 20-39 (3-4), red < 20 (1-2),
  blue = no LTA reading on that stretch (nothing is invented).
* Layers: Bus Stops, Live Buses, Traffic (on route), Incidents (nearby), Rain (NEA).
* Route Traffic Summary: % smooth/moderate/slow by length, route length, stops, estimated travel time
  (from live speed bands + ~20 s per stop; shows "--" if the traffic feed is down).
* Live Buses: found by polling Bus Arrival at up to 14 stops along the route and de-duplicating.
  LTA gives no plate number or on-time status, so buses are #1, #2... with a Load column (Seats/Standing/Limited).
* Tap any stop for next arrivals (popup + right-hand panel). Auto-refresh: buses 20 s, traffic 5 min.
* Stop-only search (leave Service empty) lists all services at that stop.

## Deploy (Render)
1. Push these files to a Git repo, create a Render *Web Service* (or use `render.yaml`).
2. Set env var `LTA_ACCOUNT_KEY` (keep it on Render only, never in code).
3. First load after a cold start takes longer (LTA speed bands are ~100+ pages); later loads use a 5-minute cache.

Run locally: `pip install -r requirements.txt` then `LTA_ACCOUNT_KEY=... uvicorn app:app --reload`

## Optional environment variables
| Variable | Purpose |
|---|---|
| `LTA_ACCOUNT_KEY` | **Required.** DataMall key |
| `LTA_SPEED_PATH` | Override speed-band endpoint path if LTA changes it |
| `LTA_ARRIVAL_PATH` | Override bus-arrival endpoint path (default `v3/BusArrival`) |
| `OSRM_URL` | Road-snapping server (default public demo `https://router.project-osrm.org`). If unreachable the route is drawn stop-to-stop and traffic matching is less precise |
| `DATA_GOV_SG_KEY` | Optional, only raises data.gov.sg rate limits for the Rain layer |

## Known limits
* Bus positions are LTA's *estimates* for buses approaching the sampled stops; a bus far from any sampled stop may not appear.
* Traffic is matched by geometry (within ~45 m, same direction preferred). Small roads without LTA links show as blue.
* Not for operational use.
