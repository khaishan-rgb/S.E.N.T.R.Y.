# SG Transport Pulse V6.0 — Route Traffic + Departure Adjustment + Bunching, Gap & Alerts + AI Halfway Optimiser

Live bus positions, live LTA traffic drawn directly on the bus route, and next arrivals per stop.
FastAPI backend + a single-page Leaflet frontend (OneMap basemap, OpenStreetMap fallback).

## V6.0 - AI Halfway Optimiser (Bunching & Gap page -> **Halfway Optimiser** tab)

A bus is severely late. **Continue the trip, or take it off service, run it to an approved halfway point and start its next service from there? If halfway,
where?** The tab simulates *every* approved halfway point against "no halfway" and shows the evidence. Nothing is hard-coded to one location, and it is
**decision support only: there is no Deploy button, nothing is sent to buses or SCS.** Deep link: `/bunching#halfway`.

**How it works**

1. Pick a service / direction; the delayed bus is auto-detected (same bus ids **B1, B2 ...** as the Bunching tab) or chosen from the list.
2. For each approved point the bus's off-service route is found with OSRM (`alternatives=true`) and **re-timed with the LTA speed bands** along that road
   geometry. The route with the **shortest travel time under current traffic** is used, not the fewest km (the other options are listed).
3. The engine (`halfway.py`) takes the arrival times of the bus ahead (A), the delayed bus (X) and the bus behind (B) at every downstream stop. "No halfway":
   headways A->X and X->B. "Halfway at stop j": X leaves service, arrives at j after off-service time + preparation buffer (+ optional layover), and from
   there follows the segment times X would have had; the headways are A->H and H->B (re-sorted, so it can also land behind B). Before j the stops lose bus X.
4. Metrics from the halfway stop to the terminal, with "no halfway" recomputed over the **same stops**: average / max / min / std headway, % >= 1.5x and
   >= 2x scheduled, % <= 0.5x scheduled, % bunched (< the Bunching page's 3 min), **simulated EWT** (AWT - SWT), mileage loss (skipped revenue km and %),
   off-service time and km. **Recovery time** (every headway within +/-20% of scheduled at 3 consecutive key stops) is measured over the whole section, so a
   skipped stretch that stays irregular counts against a candidate.
5. **Score** = 30% headway regularity + 25% max-gap reduction + 20% recovery time - 10% off-service time - 10% mileage loss - 5% new bunching (all
   configurable). *Balanced / Faster headway recovery / Minimise mileage loss* re-weight it. The card shows the score parts, not a "confidence %".
6. A point is rejected (and says why in the table) if: off-service time or mileage loss is over its limit, less than 20% of the route remains, the bus is
   not faster than staying in service, it would start ahead of the bus in front, or **inserting it creates bunching**. It is not recommended if average
   headway improves < 10% or the score is not positive. Halfway is not considered at all unless the bus is at least 15 min late **and** the service would
   not recover by itself within 30 min. All numbers are configurable.

**Screen:** service/bus selector with status, preference, *Run AI Optimisation*, a collapsible **Scenario simulator** (choose an approved point and an
Auto or Manual start time), map (blue = normal route, purple dotted = off-service, red = existing large headway, star = halfway start, buses), the
**recommendation card** ("Recommended - Highest Simulation Score"), results table for all options (click a row: map, card, chart, heatmap and takeaways all
update), headway profile, route travel-time comparison, stop-by-stop heatmap, key takeaways and traffic/incident conditions.

**Settings tab:** all parameters with ranges and defaults; the **approved halfway points** table (upload CSV with the spec's columns - `service, direction,
stop_code, sequence, enabled, min_late, min_remaining_pct, max_offservice_min, max_mileage_km`, header optional, last four optional per-point limits; or
add / remove one point); and the **audit log** (every run stored: time, input-data time, service, direction, bus, estimated delay, preference, mode,
recommended stop, all scenarios, parameters, model version `halfway-1.0`). Same admin token as the other settings. Tables live in the same SQLite file.

**API:** `GET /api/halfway/buses?service=&direction=`, `GET /api/halfway/run?service=&direction=&bus=&pref=balanced|recovery|mileage[&stop=&start=]`,
`GET /api/halfway/runs`, `GET /api/halfway/runs/{id}`, `GET|POST /api/halfway/config`, `POST /api/halfway/points` (+ `/add`, `/delete`, `/clear`).

**Read this before relying on it**

* **Lateness is estimated**, not read from a timetable (LTA publishes none): *gap to the bus ahead minus scheduled headway*. There are no registration
  numbers in the LTA feed, so buses are B1, B2 ...; the first bus on the route has nothing ahead and cannot be measured.
* Everything is **simulated from LTA arrival estimates and speed bands**. EWT is a simulated figure, not LTA's actual EWT. When a stop has already been
  passed by the bus ahead, its passing time is back-estimated from the traffic model (LTA keeps no history). If no bus is tracked behind X, the next
  bus is assumed one scheduled headway behind (and the page says so).
* **Off-service routing uses OSRM** (`OSRM_URL`, public demo by default: no SLA, not for heavy use). If it does not answer, off-service time falls back
  to a **labelled estimate** (straight line x 1.35 at 30 km/h, marked "estimate" in the table, card and traffic panel). Without speed-band readings the
  route's free-flow time is used and labelled as such. Consider a self-hosted OSRM/OneMap routing source for production. The OSRM calls were verified
  against a simulator that mimics its response format, not against the live service.
* The **scenario date/time is live only**: LTA offers no history to replay. The Scenario simulator overrides the halfway point and its start time.
* Bunching in this module follows the Bunching page (< 3 min absolute); the table also reports the spec's "<= 0.5x scheduled" share.
* Weights and limits are starting points from the specification; tune them against operational experience.

## V5.6 - Alerts (Bunching & Gap page -> **Alerts** tab)

Each case that reaches the logging threshold raises alerts that **escalate while it lasts**:

| Bus stops the case has lasted | Alerts raised |
|---|---|
| 15 | **1** |
| 20 (next 5) | **2** |
| 25 (next 5) | **3** |
| 30, 35 ... (every 5 more) | 4, 5 ... |

* Same rules as the event log: bunching = headway **< 3 min**, long headway = **scheduled + 10 min**, counted over **observed** bus stops (start stop ->
  where the leading bus is now). A case that clears before 15 stops never raises an alert. The first threshold is `confirm_stops` (bunching) /
  `gap_stops` (long headway); the step is the new `alert_step` setting (default 5). If a refresh is missed and the case jumps past several steps, all the
  alerts are raised.
* The tab lists one row per active case: badge (**BUNCHING 3BB** / **LONG HEADWAY**), service and direction, stops so far, start -> current bus stop,
  **"N alerts"** with when the next one is due, **Map**, **ACT** and an arrow that opens the alert history (time and stop of each alert). Rows with
  3+ alerts are styled as escalated. Most alerts first. The tab shows a badge with the number of cases not yet acknowledged, and the browser title shows
  it too. The list follows the Services / Direction filter on the Dashboard tab.
* **Map** opens the Dashboard with that service selected (live map, bus sequence, headway path). **ACT = acknowledge**: it marks the alert "ACKED" so
  the team can see it has been seen; it **does not** send anything to buses or SCS and gives no intervention advice (that is the V2 layer in the spec).
  An acknowledged alert becomes un-acknowledged again when a further alert is raised (the case got worse).
* When a case clears (2 clean refreshes) its alerts leave the list; the **event log** keeps a case's start/end time, start/end bus stop, stops and the
  **number of alerts** it raised.
* Limits: alerts and acknowledgements live in the server's memory (a restart clears them; the event log is what persists). Acknowledging needs no admin
  token, so anyone who can open the page can do it. There is no sound, SMS, e-mail or push notification: the page has to be open (Alerts tab or Dashboard).
* **Fix (V5.6.2): alerts can no longer arrive in a burst.** A second way to get "4 alerts at once, same time, same stop": a case dropped out of the
  group list for two refreshes (so its event closed) while the tracker still remembered the pair as bunched from many stops earlier; when the case
  came back, the new event inherited that history and started at 30+ stops. Now **every event counts stops only from where it is first tracked**
  (never from the tracker's older memory), **at most one alert is raised per refresh**, and an open event that nobody has updated for several refresh
  intervals is closed instead of being silently continued. `GET /api/bunching/debug?service=32&direction=1` shows what the tracker and each event
  currently believe (send this output if alerts ever look wrong). To check which build is running, look at the page footer or `/api/health`: it must say **V5.6.2 or newer**.
* **Fix (V5.6.1): "9 alerts at once, all at the same time and stop".** After nobody had the page open for a while, the server still remembered the buses
  and the "first seen" stops from before, so buses now on the road inherited old ids and the case was counted as already 55 stops long. Now: bus tracks
  are expired **before** matching; after a pause in polling (no update for 150 s, or 3x the refresh interval) all bus ids and pair history are dropped; a
  case can never move along the route faster than buses do (a jump is treated as a different case); a group only continues an event if it overlaps who is in
  the case **now**; and every alert records the stop where its own threshold was crossed (the 15th, 20th, 25th stop...), not just where the bus is now.
  **After a pause, counting starts again from when a case is next seen**, so a case that was already under way when the page was reopened needs a further
  15 observed stops before it is logged or raises alert 1.
* API: `GET /api/bunching` now also returns `alerts`; `POST /api/bunching/alerts/ack` with `{"id": ...}`.

## V5.5 - Bus Bunching & Headway Gap page (`/bunching`): bunching < 3 min, long headway = scheduled + 10 min, 15 stops

A third page, built to the V1 spec. It only **detects and predicts**; it does not recommend interventions (that is a later layer).
Header buttons link all three pages.

**Rules (all editable in Settings, no code change):**

| State | Rule |
|---|---|
| **Bunching** | headway between two consecutive buses **< 3 min** (absolute, not a fraction of scheduled headway; exactly 3.0 is not bunching) |
| **Long headway (gap)** | headway **>= scheduled headway + 10 min** |
| **Confirmed** | the state holds for **15 consecutive bus stops** (bunching and long headway each have their own stop count) |
| Developing | in the state now but for fewer than 15 stops, or **predicted** to get into it within the horizon (early warning) |
| 2BB / 3BB / 4BB+ | buses travelling as **one group**. A-B and B-C both bunched is reported once as **3BB**, never as two 2BBs |
| Closed | the state ends; the event closes after 2 clean refreshes (no flapping) |

**How it works (`bunching.py`, no I/O):** tracks every consecutive bus (stable ids B1, B2... between polls), estimates each bus's ETA at
every downstream stop, takes the headway between neighbours at each stop, projects it to NOW / +10 / +20 / +30 min and counts the
**consecutive stops** it stays in the state (stops already travelled since first seen + predicted stops ahead).

**Risk ranking** (0-100) = gap severity 30% + bunching level 25% + persistence 20% + deterioration rate 15% + time-to-occur 10%
(weights configurable). Gap severity is the minutes above scheduled headway, full at scheduled + 10 min. Red = confirmed bunching / confirmed
long headway / a developing 3BB+; Orange = developing; Yellow = early drift; Green = normal.

**Event log (Settings tab):** one row per event with **start time, end time, start bus stop, end bus stop, number of stops**, buses, peak
level and min/max headway. **An event is logged only if it lasted at least 15 bus stops** (`confirm_stops` for bunching, `gap_stops` for long
headway). Stops are counted from where the leading bus was when the state was **first seen** to where it was when it was **last seen**
(predicted stops do not count), so a case that clears sooner is dropped and never appears in the log. Start and end time are the first and last
refresh at which the state was seen. Open cases appear in the log (as "still on") only once they have reached 15 stops.
* The start is where **this server first saw** the state. If the server restarted, or was asleep, while a bunch was already under way, counting
  starts from when it woke - so that event can be logged with fewer stops than it really had, or not at all.
* The collector only polls while someone has the page open (and for 10 min after). For an unattended log set `BUNCHING_ALWAYS_ON=1`
  (uses more LTA quota; Render's free tier still sleeps when idle, so it needs a paid always-on instance to be truly unattended).
  When the collector goes idle, open events are closed at the time they were last seen, so nothing is lost.

**Scheduled headway:** the **Service Headway Master** (Settings -> upload CSV `service,direction,day_type,from,to,target_hw`) wins over
LTA's BusServices dispatch-frequency band. The narrowest matching time window is used. No code change is needed to edit it.

**Traffic and incidents are shown as context only** ("Possible contributing factor"), never as the cause (spec section 18).

**What the data can and cannot say**
* LTA gives **no registration numbers**, trip IDs or BC identity, so buses are labelled B1, B2... by position tracking (the mock's `SG7219L`
  column cannot be filled from DataMall). Ids are stable while the server keeps running.
* LTA only publishes estimates for buses approaching each stop; a bus far from any sampled stop may be missing, and +20/+30 min values show
  "-" when there are too few downstream samples. Long horizons are less reliable than NOW.
* The **"vs 30 min ago"** KPI deltas and the **trend chart** are built from the server's own history, so they start empty after every
  restart and fill over the following 30+ minutes. The page says "history builds" rather than faking numbers.
* The mock's **Area / Corridor** filter is not built: LTA does not tag services with a corridor.
* Road works are not available from DataMall with coordinates; incidents are.

**Collector and storage:** a background loop polls LTA every `refresh_sec` (default 30) for the watched services and **stops after 10
minutes with nobody viewing** to save quota. Settings, the Service Headway Master and the bunching / gap **event log** (start, end, buses,
max BB level, start/end stop, consecutive stops, minimum headway) live in SQLite (`bunching.db`, git-ignored). On Render's free tier the
disk resets on restart or redeploy, so the log and settings return to defaults - attach a persistent disk (or point `BUNCHING_DB` at one)
if you want history. If the database is unavailable the page still works and says so.

**API:** `GET /bunching` (page) - `GET /api/bunching?services=32,145&direction=0` (0 = both) - `GET /api/bunching/detail?service=32&direction=1`
(route geometry, buses, incidents) - `GET|POST /api/bunching/settings` - `POST /api/bunching/master` (CSV upload) and
`POST /api/bunching/master/clear` - `GET /api/bunching/events?limit=100` (JSON; the page's Export button makes the CSV). Saving settings or the
master needs `BUNCHING_ADMIN_TOKEN` (falls back to `TIMETABLE_TOKEN`) if one is set.

**Phase-1 acceptance test (spec section 23)** is automated in `test_bunching.py` with the new numbers: Normal -> Early warning (predicted < 3 min) ->
Developing -> Confirmed 2BB at 15 stops -> 3BB upgrade -> Recovering -> event closed and logged only if it reached 15 stops.

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
| `BUNCHING_SERVICES` | Default watch list for the Bunching page (default `32,145,65,33,51,74,89,200,27,157`) |
| `BUNCHING_MAX_PAIRS` | Max bus pairs analysed per request (default 40) |
| `BUNCHING_DB` | SQLite path for settings / headway master / event log (default `bunching.db`) |
| `BUNCHING_ALWAYS_ON` | `1` = keep collecting when nobody has the Bunching page open (default: stop after 10 idle minutes) |
| `BUNCHING_ADMIN_TOKEN` | If set, saving Bunching settings / master requires it (falls back to `TIMETABLE_TOKEN`) |
| `DATA_GOV_SG_KEY` | Optional, only raises data.gov.sg rate limits for the Rain layer |

## Known limits
* Bus positions are LTA's *estimates* for buses approaching the sampled stops; a bus far from any sampled stop may not appear.
* Traffic is matched by geometry (within ~45 m, same direction preferred). Small roads without LTA links show as blue.
* Not for operational use.
