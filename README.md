# SG Transport Pulse V5.3 — Route Traffic + Pre-emptive Departure Adjustment

Live bus positions, live LTA traffic drawn directly on the bus route, and next arrivals per stop.
FastAPI backend + a single-page Leaflet frontend (OneMap basemap, OpenStreetMap fallback).

## V5.3 - All services, planned timetable, and "call the BC to slow down"

**Are there planned and actual timetables in the data? Not from LTA.** DataMall publishes only dispatch-frequency bands, first/last bus
times and live arrival *estimates* - no per-stop planned times and no per-stop actual times. So:
* **Planned** = a timetable **you upload** (Headway Control page -> *Upload timetable*; *Template* downloads an example). Two CSV formats:
  `service,direction,departure,run_min` (stops interpolated by distance) or `service,direction,trip,stop_code,time` (planned time per stop).
* **Actual** = LTA's live estimated arrival at each stop still ahead of the bus (scaled along the route by current traffic). LTA does not
  record what already happened, so past stops cannot be judged.

**Call the BC rule** (needs an uploaded timetable): a bus is flagged when **all** are true
1. it is predicted >= 2 min ahead of plan at **more than 50% of its remaining stops**;
2. the headway behind it is long (>= scheduled HW + max(40%, 3 min)); and
3. that long headway has **lasted 5+ min** (so a single noisy refresh never triggers it).

It is then highlighted: an orange **"Call the Bus Captain to slow down"** banner at the top, the table row turns orange with a **CALL BC**
badge, the bus is orange on the route strip, the *Planned vs predicted* table flags it, and *Copy BC message* drafts the call. Without a
timetable, the older "gap growing" check (Comm BC / regulate spacing) still applies. Thresholds: `headway.CFG` (`EARLY_STOP_MIN`,
`EARLY_STOP_PCT`, `HW_PROLONG_MIN`).

**All services mode** (Scope -> *All services*): lists every service direction, stable ones included, with counts for Critical / Developing /
Stable / Insufficient data, an operator filter and 12-row pages. The page scans in batches (about a minute to a few minutes per lap) and
repeats every 3 minutes while Auto refresh is on. Keep the page open: the 10-minute "prolonged" confirmation needs repeat visits.
* Default cap is **160 service directions** (`CONTROL_ALL_CAP`) to protect your LTA quota; the page says when it is capped. Use the operator
  filter to cover more. Raising the cap makes laps longer, so confirmations get slower - raise it gradually.
* Not verified against the live LTA network: full-network laps and their timing depend on LTA's response speed and any rate limiting.

**Uploads are shared:** one timetable serves everyone using the site and is stored in `timetable.json` (git-ignored). Set `TIMETABLE_TOKEN`
on Render so only people who know it can upload/clear. On Render's free tier the disk can reset on restart/redeploy: re-upload if the
timetable bar says none is loaded.

## Headway Control page (`/control`) - V5.2 rules - pre-emptive departure adjustment (V5.2: quieter, layover-aware)
For each selected **Service + Direction** (up to 8 services, 16 pairs) it predicts every bus and its terminal arrival, then runs five
checks and suggests a departure-headway (HW) change. **V5.2 is deliberately quiet: it only recommends an adjustment for prolonged
congestion that terminal layover cannot absorb.**

| # | Check | Fires when (all conditions) | Suggestion |
|---|---|---|---|
| 1 | En-route | a leading bus is > max(40%, 3 min) ahead of the bus behind, that gap grew >= 2 min downstream, **and it is still true 3 min later** | Comm BC / regulate spacing |
| 2 | Traffic | **prolonged** congestion ahead of 2+ buses (a slow stretch of >= 1.5 km below 20 km/h costing >= 3 min each) **held for 10+ min** (two LTA speed-band updates) **and** the delay exceeds what layover absorbs | temporarily longer HW (10 -> 11/12/13) |
| 3 | Last 15% recovery | a longer-HW adjustment is active, affected buses are in the last 15% or clear, remaining delay is within layover slack, **for 5+ min** | cancel, restore original HW |
| 4 | Early arrival | 2+ consecutive buses in the last 15% and > 5 min early vs **your timetable running time** (not judged without one) | temporarily shorter HW (10 -> 9) |
| 5 | Normalisation | an adjustment is active, gaps normal and traffic clear **for 5+ min** | restore scheduled HW |

**Layover (your rule):** a delayed bus still gets a **minimum 7 min layover**. Delay up to (scheduled layover - 7 min) + 3 min margin is
absorbed at the terminal and is **not** adjusted. A duty with **15+ min layover is "heavy" and is never adjusted for traffic**. Enter each
service's scheduled layover in its detail panel (assumed 10 min if you have not; LTA does not publish it). Stored in your browser.

**Why it was noisy before:** every bus counted as "affected" by any 1-min slowdown, and "late" was measured against an *estimated*
free-flow plan that is always too tight, so nearly every service looked late. Brief slow spots, delays inside the layover, and
congestion that has not persisted are now ignored; a service whose congestion is still being confirmed shows status **Confirming**
(risk stays Stable) and a held adjustment is shown as **In progress**, not as a problem.

**Predicted arrival & next departure:** per bus - predicted terminal arrival (LTA ETA, else modelled), next trip direction (opposite
direction; same for loop services), earliest departure (arrival + 7 min layover) and a suggested departure that keeps the scheduled HW
spacing. Holds are capped at 5 min (`MAX_HOLD`); bunching bigger than that needs other action. Heavy-layover duties keep their own layover.

The page also shows: risk table (Critical / Developing / Stable / Insufficient data), predicted HW at +10/+20/+30 min, time to critical
(widening gaps only), delay ahead, likely cause, a route strip, a draft message for the Bus Captain, a session headway-trend chart and CSV export.

**The suggestions are deterministic rules, not an AI/LLM call**, so every recommendation is explainable and every threshold is listed on
the page (`headway.CFG`, returned by the API). Decision support only.

### What the data can and cannot say (please read)
* LTA DataMall has **no operator timetable, dispatch times or layover data**. Scheduled HW = midpoint of LTA `BusServices` frequency for
  the current time band (AM peak 06:30-08:30, AM off-peak 08:31-16:59, PM peak 17:00-19:00, PM off-peak).
* Gaps = differences of LTA's estimated arrival times at the same stop; "growing" compares a nearer and a farther stop.
* **Persistence is measured by the server while the page is open and refreshing.** It is kept in memory: after a restart (or a Render
  free-tier sleep) the 10-minute confirmation clock starts again, so leave the page open for a few minutes before trusting "Confirming".
* Buses are only seen if LTA lists them among the next 3 at a sampled stop; a bus far from every sampled stop can be missed.
* "Apply / Undo" are stored in the browser (localStorage) to drive checks 3-5. **Nothing is sent to any dispatch system.**
* With fewer than 2 live buses a service shows "Insufficient data" (never a false "Stable"). If the traffic feed is down, checks 2-4
  show n/a and no delay is guessed.

### New API
`GET /api/control?services=32,145&direction=0|1|2&adj=32:1:13&plan=32:1:45&lay=32:1:12`
(`adj` = HW already applied; `plan` = timetable running time in min (enables check 4); `lay` = scheduled terminal layover in min).
Files: `headway.py` (engine), `control.html` (page). All thresholds are in `headway.CFG`.

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
| `TIMETABLE_TOKEN` | If set, uploading/clearing the planned timetable requires this token (recommended) |
| `CONTROL_ALL_CAP` | Max service directions scanned in All-services mode (default 160) |
| `DATA_GOV_SG_KEY` | Optional, only raises data.gov.sg rate limits for the Rain layer |

## Known limits
* Bus positions are LTA's *estimates* for buses approaching the sampled stops; a bus far from any sampled stop may not appear.
* Traffic is matched by geometry (within ~45 m, same direction preferred). Small roads without LTA links show as blue.
* Not for operational use.
