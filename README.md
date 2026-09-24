# SG Transport Pulse V13.6 — Route Traffic + Departure Adjustment + Bunching, Gap & Alerts + AI Halfway Optimiser

Live bus positions, live LTA traffic drawn directly on the bus route, and next arrivals per stop.
FastAPI backend + a single-page Leaflet frontend (OneMap basemap, OpenStreetMap fallback).




## V13.6 - Running Time Analytics fully automated from open data

Everything runs on the server by itself; no uploads and no manual timetable are needed.

* **Round-the-clock measurement.** Services added under "Services measured round the clock" are stored in `rt_watch` and polled even with every page closed (every `RT_POLL_SEC`, default 60 s, when nobody is watching; the normal page rate when someone is). Up to `RT_MAX_SERVICES` (default 8) services, each about 15 cached DataMall Bus Arrival calls per direction per poll.
* **Live conditions captured per trip** (these feeds have no history, so they are recorded as each trip ends, `rt_cond`): average speed-band traffic speed along the route and the share of slow road, LTA incidents and road works within 60 m of the route.
* **Scheduled open-data jobs** (`rt_data_loop`, every 10 min, new module `rtdata.py`):
  * **Rainfall** - data.gov.sg v2 real-time rainfall API with `?date=` (past days allowed), 5-minute readings from the three gauges nearest the route, summed over each trip's window; cached per day in `weather_day`; backfills up to 10 days per run. Optional `DATAGOV_API_KEY` for higher rate limits.
  * **Public holidays** - data.gov.sg MOM datasets (consolidated + 2026 + 2027), daily; trips on a PH are analysed as Sunday/PH.
  * **School holidays** - MOE 2026 vacation periods seeded (14-22 Mar, 30 May-28 Jun, 5-13 Sep, 21 Nov-31 Dec); later years are added on the page when MOE publishes them.
  * **Passenger volume** - DataMall *Passenger Volume by Bus Stops* (monthly zip, last 3 months kept by LTA), limited to the stops of the measured services (`pv_stop`); each trip gets the tap-ins along its route in its hour and day type as its demand.
* **Contributors panel** now uses these real per-trip conditions: traffic speed, incidents, road works, rain, passenger demand, school / public holiday - still worded as associations.
* **Page:** "Automated data sources" replaces the collection box: always-on service list (add / remove), a feed status table (source, status, last run, "Fetch now"), school-holiday editor; the timetable entry is folded into an optional section.
* New endpoints: `GET /api/rt/sources`, `POST /api/rt/refresh-data`, `POST /api/rt/school`; `POST /api/rt/watch` now persists (`remove: true` to stop).
* **Remember:** a persistent disk is required on Render for the history to survive restarts.

## V13.5 - the timetable is optional, and 6 months of history

* **Compare against** selector: *Auto* (timetable where entered, otherwise the measured baseline), *Measured off-peak baseline only* - no timetable entry needed anywhere - or *Entered timetable only*. The baseline is each service + direction + day type's own quiet-period P50 (outside 07:00-09:30 and 17:30-19:30, at least 8 trips, else the all-day P50), so "gap" reads as **how much longer than a quiet trip that period needs**. A banner states which basis is in use.
* **Service can be chosen before any data exists:** with nothing measured, the page shows a "Start measuring this service" box that adds it to the collector immediately, and explains that no back-history exists to load.
* **Retention raised to 180 days** (`RT_KEEP_DAYS`, default 180). `/api/rt/status` now reports total trips stored and the approximate bytes used (about 400 bytes per trip, so roughly 25-80 MB for six months of a handful of services).
* **Important:** SQLite lives on the instance disk. On a Render free instance the disk is wiped on every redeploy / restart, so attach a persistent disk (or point `BUNCHING_DB` at one) before relying on six months of history.

## V13.4 - Running Time Analytics is measured from LTA DataMall (CSV upload removed)

Page: **Running Time Analytics** (`/running-time`, `/insight` still works). There is no historical file to upload, so the page now builds its own data.

* **Actual running time is measured, not imported.** The bunching collector already tracks every bus along the route; it now also records each tracked bus's position, and when a bus completes the route the trip is stored in `rt_trip` with its crossing time at **every stop** (interpolated between polls, extrapolated up to 0.45 km at the terminals). A trip is kept only if it was observed over at least 82% of the route and started near the origin; partial trips are scaled and flagged by their coverage. History therefore grows while any page is open, or continuously with `BUNCHING_ALWAYS_ON`. `RT_KEEP_DAYS` (default 120) prunes old trips.
* **Collection panel** on the page: add a service to the collector, see trips measured per service and direction, the average measured RT, the period covered and the collector state.
* **Scheduled running time** is not published by DataMall, so it is entered per service / direction / day type / period (`rt_sched`, admin token) and matched to each measured trip. Without it the page still shows the measured distribution (P50 / P85 / P90) and says the gap cannot be computed.
* **Sections** still work on live data: DataMall has no stop-to-stop timetable, so each section is compared with **its own typical level (P50)** and the column is labelled "Extra vs typical" with a banner explaining it - it shows where and when time is lost, not a timetable shortfall. If stop-level scheduled times ever exist, the timetable basis is used automatically.
* **CSV upload removed** (`/api/insight/upload` deleted). A clearly labelled **modelled sample** dataset can still be added for training and is marked MODELLED SAMPLE - TRAINING ONLY.
* New endpoints: `GET /api/rt/status`, `POST /api/rt/watch`, `GET|POST /api/rt/sched`. Everything else (percentile filters, management summary, D1/D2 charts and period reports, important-stop auto-paired sections, waterfall, heatmap with drill-down, contributors, RT options, bootstrap scenarios, quantile model, data quality) is unchanged.

## V13.3 - Running Time Analytics (`/insight`)

"Smarter Planning, Better Journeys" - a separate page answering which services lack running time, which direction, at what time of day, on which section of the route, and how much time should reasonably be provided.

* **Data (`insight.py`).** Upload trip-level OR stop-level CSV/TSV (12 MB max); headers are matched loosely (Service / Svc / ServiceNo, ScheduledDeparture / SchDep ...). Trips are rebuilt from stop times when terminal times are missing, after-midnight times unwrapped, day type taken from the column or derived from the date, incomplete trips dropped and counted. A **modelled demo dataset** can be generated to see the page working - labelled MODELLED DEMO DATA everywhere, never presented as actual observations. Datasets are stored gzipped in SQLite (`insight_dataset`).
* **Adequacy engine.** Never judged from one trip: every service + direction + day type + time band (15 / 30 / 60 min) is summarised as mean, P50, P75, P85, P90, P95, SD and n against the scheduled running time, with gaps at P50 / P85 / P90 and at a **configurable planning percentile** (P85 is the default, not a hard-coded truth). Outliers are removed by MAD *within service + direction + hour*, so genuinely slow peak trips are not discarded.
* **Management summary** of every service: D1/D2 scheduled, P50, planning percentile, gap, periods short, largest shortage, direction affected, suggested review period, sample size - sortable.
* **Per service:** KPI cards, D1 and D2 running-time charts (scheduled, P50, P85, P90, short periods shaded, hover/tap detail) and the full time-period report with reliability % and an assessment (Adequate / Marginal / Short / Clearly short / Too few trips).
* **Important bus stops -> sections.** Every stop of the service + direction is listed in route sequence with its 5-digit code in a searchable tick-box multi-select. Selected stops are **auto-paired along the sequence** (1>5, 5>10, 10>16 ...), never 1>5, 6>10, and never reversed. Each section gets the same distribution treatment plus its share of the route shortage, a **waterfall** of where the time accumulates, and a **section x time-period heatmap**; tapping a cell lists the trips behind it.
* **Contributors** for the worst section: the slowest trips (at or above the planning percentile) compared with the rest on whatever condition columns exist (traffic speed, dwell, road works, incident, rain, demand, events). Always worded as associations, never as proven causes.
* **Recommendation:** Keep current / Option A (P50) / Option B (P85) / Option C (P90), each with the measured share of trips that would finish within it - management decides.
* **Supporting analyses:** historical **bootstrap** scenarios (normal weekday, AM peak, PM peak, rain, road works, heavy traffic, high demand) with a 90% interval on the percentile, and a small **linear quantile regression** (pinball loss, numpy) reported with its pinball loss and achieved coverage and labelled MODELLED - stress test is deliberately not the main engine.
* **Data quality** always on screen: trips analysed vs trips in the file, date range, dropped rows, outliers removed, missing columns, configurable minimum sample per period with small samples flagged.
* Works on desktop and mobile (stacked cards, horizontally scrolling tables, touch-friendly multi-select). Added to the left launcher and to every menu.

## V13.0 - Halfway Deployment Planner (`/planner`): proactive, standalone

Until now halfway information only appeared after the optimiser decided a halfway start was needed. This page lets a controller open it at any time, pick a service and direction, and explore halfway points independently.

**Filter panel (top):** service -> direction (labelled `UP - first -> last`) -> current location / starting point (first stop, any 5-digit stop code, or the device's map location) -> **important bus stops** (searchable multi-select by code or name, select all / clear all, shows "Important Bus Stops: n selected") -> max time to halfway -> max off-service distance -> minimum headway gain -> bus type and optional vehicle height / width / weight -> scheduled headway, how late the bus is, the trip's scheduled departure and when the bus is free to move -> **Find possible halfway points**.

**Search (`/api/planner/search`):** every stop on the direction is tested. One routing request gives the road time and distance from where the bus is now to every stop. For each stop: off-service time and distance, stops remaining and omitted, important stops served / missed, when the bus could enter service, and the headway gain (largest gap at that stop if the bus runs the full trip vs if it starts there, assuming the buses around it are on time). Stops the bus would reach after the following bus are not candidates and are counted separately.

**Ranking is never by distance alone:** headway gain minus off-service time and distance, the share of stops omitted and important stops missed; green / amber / red bands; and a written "why the closest is not always the best" comparison of the top three.

**Selected candidate (`/api/planner/route`):** the V12.9 routing and suitability engine for that stop - up to three road routes (fastest / shortest / preferred), live congestion, restriction and structure checks for the chosen bus type, findings list, VERIFIED / REQUIRES REVIEW / UNSUITABLE, road-by-road timeline linked to the map, and the map itself (revenue route in purple, off-service dashed blue, section not served dotted, red halfway pin, green / red important-stop markers, incidents and road works). A "bus stops affected" strip shows every stop as a dot (grey = not served, green = served, ringed = important). If the detailed road time differs from the screening estimate, the page says so and shows both headway figures.

**V13.2:** the Halfway Planner is in the navigation everywhere - the left launcher on desktop, the top menu and the "More" sheet on phones - and the page itself now carries the same left launcher, hamburger menu and bottom bar as the other pages.

**V13.1:** the candidate list carries an **AI recommendation** panel (the pick, plus why it beats the quickest-to-reach point, the largest-gain point and any point that keeps every important stop) and is **sortable** - AI rank, headway gain, off-service time, distance, stops remaining or omitted, important stops served, or stop sequence - ascending or descending, from the dropdown or by tapping a column heading. The AI pick is starred and selected automatically.

**Note:** the headway figure here is a quick estimate for one delayed trip. The full stress test comparison of No action / Full trip + adjustment / Halfway + regulation stays on the Halfway Optimiser page.

## V12.9 - Halfway Deployment & Off-Service Route Planner

Answers, for the best halfway deployment: **where** the bus starts service, **which roads** it takes off-service, **whether this bus type can use them**, and **how much headway** it earns.

* **Real road times for every candidate stop.** One OSRM `table` request gives the off-service time from the interchange to every candidate halfway stop (x 1.25 for a bus); the optimiser now uses it instead of 0.7 x running time. If routing is unavailable the old estimate is used and the page says so. A 2-min preparation time is added before the bus enters service.
* **Several off-service routes, not just the shortest.** OSRM alternatives plus "along the service route (no stops)". Travel time from LTA speed bands when they cover at least half the route, otherwise OSRM time x 1.25. Tags: **A Fastest**, **B Shortest**, **C Preferred** (balances time, congestion, turns / sharp turns, items needing review, road works, and how much of it follows the service's own road path). If the detailed route time differs from the screening estimate by more than 1.5 min, the optimisation is re-run once with it.
* **Route suitability per bus type** (Single deck / Double deck / Articulated, plus optional vehicle height / width / weight from your fleet data):
  * checks OpenStreetMap (Overpass) along the route for height / width / weight limits, bus / motor-vehicle / heavy-vehicle / access restrictions, tunnels and covered roads without a recorded clearance, bridges and rail viaducts over or beside the route; OSRM manoeuvres for sharp turns / U-turns; LTA road works and incidents on the route;
  * **UNSUITABLE** only when map data explicitly shows a limit below the entered vehicle size or no bus access; **VERIFIED** only when a controller has recorded this exact road sequence for this stop and bus type; everything else is **REQUIRES REVIEW** ("Double-decker suitability not verified - operational review required"). No bridge height, clearance or restriction is ever assumed.
* **Headway maths at the halfway stop:** Scenario A (continue full service: arrival -> layover -> next departure -> largest gap) vs Scenario B (off-service departure + travel = arrival; + preparation = insertion; placed between the surrounding buses) -> **HEADWAY EARNED = largest gap A - largest gap B**, plus the comparison with regulate-only, average gap, mileage operated / lost / off-service, BC finishing delay.
* **Page:** new card above the comparison: large map (normal route with direction arrows, off-service route dashed over live congestion colours, recovered service, section not operated, interchange / halfway / final stop, other buses at the insertion time, incidents, road works; click the route for roads, distance, time, stop and insertion time); recommendation panel; route options (table on desktop, cards on phones); passenger time-axis for both scenarios; route timeline synchronised with the map; "why this improves headway" and "why this route"; suitability findings; controller approval.
* **Controller records** (table `offservice_record`, admin token as other admin actions): "Record route as verified for <bus type>" (requires a confirmation tick) and "Approve this deployment" (audit only - nothing is dispatched). `GET /api/halfway/offservice/records`.
* **Settings:** `OVERPASS_URL` (default public overpass-api.de) and the existing `OSRM_URL`. For heavy use, run your own OSRM / Overpass or pass their URLs.
* **Limits to know:** OSRM car profile (no bus-specific turning rules); OpenStreetMap is community data, incomplete for clearances; the halfway bus starts from the interchange (not a live GPS position); other buses' positions come from the simulation.

## V12.7 - AI Recovery Scenario Optimiser: full trip + adjustment vs halfway, judged on the whole trip chain

**Why:** the earlier regulation only moved the 3 trips before the gap and put one trip at the midpoint (e.g. 17 | 17 | 7 | 21 | 7 | 8), ignored a second late bus, and only looked at the interchange departure.

**New engine `recovery.py`** (numpy, pure computation) and endpoint **`/api/halfway/recovery`**:
* **Generate** ~2,000-3,000 plans: no intervention, adjust one trip, spread departures, adjust several trips (+/-8 min), full trip + regulation, halfway one trip + regulate the others, halfway one trip while others run full, two halfway starts (both >= 20 min late).
* **Reject** automatically: BC layover < 7 min, adjustment > +/-8 min, altering a departed trip (trips whose departure is before "now" are locked), simultaneous departures, worse downstream headway without enough benefit. Counts are shown.
* **Simulate the chain** UP 1 -> DOWN 1 -> UP 2 -> DOWN 2 -> UP 3 -> DOWN 3 for every bus: at each terminal a bus leaves at max(schedule, arrival + 7 min), so a full trip's lateness is carried into the BC's later trips (BC finishing delay). Buses may leave the interchange out of timetable order (a ready bus runs ahead of a very late one); no overtaking along the route; a bus with a long gap ahead runs slower (load sensitivity).
* **Refine** the best plans by coordinate search (whole minutes), then **stress test**: 1,000 futures per short-listed plan (traffic per trip, bus-to-bus running time, dwell / load, incidents, arrival-prediction error, off-service time) -> P50 / P85 / P90 max headway, chance of settling, bunching risk, BC finishing delay.
* **Decide:** Headway priority (delay > 20: Halfway -> Adjustment -> Regulation), Mileage priority (< 30: adjust; >= 30: halfway only if >= 0.25 min of P85 headway per km lost), Balanced (lowest expected cost). Halfway must beat adjustment by >= 2 min P85 max headway, >= 10 min recovery or >= 30 pp bunching risk. If the late bus cannot reach any stop in time, a standby bus is proposed.
* **Halfway page:** new panel "AI Recovery Scenario Optimiser - Full Trip vs Halfway" (instructions per trip, No action / Full trip + adjustment / Halfway + regulation cards with headway patterns, BC finishing delay and trade-off bars, whole-chain table incl. a "next-departure-only fix" for comparison, 1,000-future distribution, why the AI chose it, plans generated / rejected). The trip table's AI plan columns now show this plan; the earlier per-stop engine is kept (collapsed) for the map and heatmap and is re-run on the same decision.
* **New page `/recovery-guide`:** management infographic "How AI optimisation makes trip adjustment & halfway deployment decisions" (7 stages); shows the last run from the same browser when available.
* **Assumptions to validate:** DOWN running time = UP running time; halfway bus runs off-service at 0.7 x running time and skips the interchange layover; stress test spreads are starting values (see `recovery.PARAMS`). `numpy` added to requirements.

## V11.3 - Standardized filters across every page: Transport Operator, consistently placed and labelled

* **Transport Operator, everywhere.** Every analysis page now has the same **Transport Operator** filter (SBS Transit / SMRT / Tower Transit / Go-Ahead, or All operators), with the same label and the same four
  operator codes (SBST / SMRT / TTS / GAS), placed as the **first** field in the filter row on every page:
  - **Route Traffic** and **Halfway Optimiser** - narrows a new Service datalist (`/api/traffic/services`), so typing a service number now offers real suggestions instead of a blank text box.
  - **Bunching & Gap** - filters the dashboard table client-side to the services run by the chosen operator.
  - **Headway Control** - the existing Operator field (used for *All services* scanning) is now always visible instead of hidden, just greyed out and explained by a tooltip when Scope is *Selected* (it only applies
    to *All services* mode).
  - **Traffic-Aware** - unchanged multi-select behaviour (it can watch several operators' services at once), just relabelled and moved to the first position to match the other pages.
  A service with no recorded operator in the data is never hidden by this filter on any page - we only filter what we actually know, never guess.
* **Direction, one consistent control.** Traffic-Aware's Direction filter was a dropdown; every other page used a Both / Dir 1 / Dir 2 toggle-button group. It's now the same toggle-button group everywhere, sending
  the same values (0 / 1 / 2) as before.
* **No calculation, backend, or API changes.** This was pure front-end restructuring - reusing the already-existing `/api/traffic/services` endpoint (and Headway Control's own `/api/control/services`), reordering
  and relabelling existing filter fields, and adding a datalist where none existed. Nothing about how a route, headway, bunching, halfway plan, or traffic alert is calculated has changed.

## V11.2 - Less noise, a sortable delay column, a clearer route line, and LTA traffic camera images

* **Noise: only material delays by default.** A congestion alert is hidden if its estimated traffic delay is under `min_delay_min` (default **3 min**; 0 shows every alert); this is a Settings value, editable per
  deployment, and can be overridden per request (`min_delay=0` to see everything for a moment). Alerts with no delay estimate (incidents, road works, weather - the specification never invents one for these) are
  unaffected, since there is nothing to threshold. The summary cards and totals count the same filtered set as the table, so the numbers always agree with what is listed.
* **Sortable "Est. delay".** Click the column header to sort the work queue by estimated delay (a second click reverses it; a small arrow shows the direction); click "Risk" to go back to the default order
  (unacknowledged, then severity, then number of services, then duration).
* **Clearer route line.** The selected service's route on the map is now drawn with a dark casing, a white halo, then the blue line on top, with small direction arrows along it - legible over any basemap or
  speed-band colour instead of a thin dashed line that blended into OneMap's blue tiles.
* **LTA traffic camera images.** The Selected Alert Details panel now shows the nearest LTA traffic camera image (within `camera_km`, default 3 km, of the disruption's location) as a periodic snapshot, with the
  camera id and its distance; captioned as a snapshot, not live video, since DataMall does not push a live feed. If the image link has expired or no camera is close enough, the panel says so instead of showing a
  broken image. New setting `camera_km`.
* **API.** `/api/traffic/overview` gains `min_delay` (float, optional); the result's `params` include `min_delay_min`. `/api/traffic/detail` gains `camera` ({id, image, dist_km}) and `camera_error`. Model `traffic-1.2`.

**What I could not verify:** the camera endpoint path. LTA DataMall's traffic-image dataset has been named differently across guide versions, so the code tries `Traffic-Imagesv2`, then `v3/Traffic-Images`, then
`TrafficImages` (override with `LTA_CAMERAS_PATH` if none of those match your account's guide) - the same fallback pattern already used for `TrafficSpeedBands`. I have no network access here to confirm which one
LTA currently serves, so please check the camera photo appears after deploying, and tell me the working path if it needs a fourth candidate.

## V11.1 - Faster refresh, a browsable service dropdown, transport operator filter

* **Performance fix (the slow refresh).** Route matching was running on every candidate congestion stretch, not just the ones that had actually persisted into an alert - with a large
  real speed-band feed most of that work was thrown away every cycle. It now only matches events once they are ACTIVE. `build_stretches` (scanning the whole speed-band feed for
  jams) was also being recomputed from scratch every `refresh_s` (60 s) even though LTA only publishes a new speed-band snapshot every ~5 min; the result is now cached against that
  snapshot and reused until it actually changes. Together these cut a warm refresh from over a second to well under 100 ms in testing at ~4x the real number of services, and the
  first (cold) refresh dropped by about 4x. A refresh that is already stale when the person's request arrives, or the very first one after a restart, is still the one that pays
  the real cost; everything after that is fast because the background loop (`tr_loop`) keeps the data warm.
* **Service filter is a real dropdown.** The arrow button (or focusing the box) opens every service as a checklist (service, its directions, its operator), not just what has been
  typed; picking one leaves the list open so several can be picked in a row, with a running count and a "clear selected" link. Typing still narrows the list live. Works the same
  on a phone.
* **Transport operator filter.** A new filter row next to Risk type lists every operator present in the data (SBST, SMRT, TTS, GAS, ...) as toggle buttons; picking one narrows both
  the work queue and the service dropdown to that operator's services, and combines with a typed/picked service list (their intersection). Selection is saved like the other filters.
  API: `/api/traffic/overview` takes `operators=SBST,SMRT`; `/api/traffic/services` now also returns each service's `operators` and the overall `operators` list.
* **Model:** `traffic-1.1`.

## V11.0 - Traffic-Aware Regulation (new page **/traffic**, nav button *Traffic-Aware*)

Early-warning for the controller: **detect** congestion / incidents / road works / rain -> **which service and direction** they affect -> **acknowledge** -> monitor until clear, and (for the selected alert)
**predict the headway** and **simulate a regulation**. Code layers (specification 29): `traffic.py` = detection, service impact, headway impact, regulation (pure functions, no network); `app.py` = feeds + `/api/traffic/*`; `traffic.html` = the page.

* **Whole-stretch congestion.** Adjacent slow LTA links (< `congest_kmh` 30 km/h) become ONE stretch (links on the same road within 120 m, on different roads only where they meet), reported with road, start -> end
  (named from the nearest bus stops), length, average / lowest speed, normal speed by road category (A 70 ... F 30), duration. Under 500 m is ignored. Drawn on the map as the whole stretch, coloured by speed
  (Very slow < 20 red, Slow orange, Moderate yellow, Normal green). All thresholds are in Settings (stored in the database).
* **One alert per event, stable id.** A jam must persist 3 updates before it is an alert (`persist_updates`) and is cleared after 3 normal updates (`clear_updates`). Its id (`CONGESTION-ORCHARD-001`) is given
  when the alert is raised and never changes while it lasts, so refreshes never create duplicates; jams that come and go use no id numbers. A feed that fails is skipped (never read as "all clear").
  Incidents, road works and rain are reported by the source, so they alert at once.
* **Service AND direction.** Each stretch is matched to the bus routes running along it **the same way** (route within 70 m, bearing within 45 deg), giving affected route length and % of the route
  (e.g. 2.8 km of 18.2 km = 15.4%). Incidents: routes within 300 m, both directions listed (the source gives no direction). Road works: by coordinates if the feed has them, else by the stops on that road name.
  Rain: route sections within 3 km of a rain area; moderate rain from 0.5 mm, heavy from 1.5 mm per gauge reading; weather is a *potential operational impact*, never a delay. Noise (an event that touches no
  bus route) is dropped.
* **Work queue.** Risk, Svc, Dir, type, location + length / speed / duration, estimated delay (distance / current speed - distance / normal speed, labelled as an estimated traffic delay), status, **Acknowledge** and
  **Acknowledge All** (only the alerts in the current view, with a confirmation). Sorted: unacknowledged, severity, number of services, duration. Priority score (traffic severity 25, route length 20, buses 20,
  headway impact 25, duration 10; unknown parts are left out and the weights re-normalised) -> critical / high / monitor / normal, all configurable.
* **Lifecycle.** NEW -> ACKNOWLEDGED (time and name recorded, the alert stays) -> MONITORING -> IMPROVING -> CLEARED. If it gets clearly worse after the acknowledgement (speed 25% lower or 1 km longer; rain
  moderate -> heavy) it is raised again as **CONDITION WORSENED**. Acknowledgements are logged (`alert_acknowledgement` table) and survive a restart.
* **Filters.** Services (multi-select: type `32, 33, 51`), direction, time horizon (Current / next 30 / 60 / 120 min: adds road works that start inside it), risk type, status (unacknowledged / acknowledged /
  cleared), search; the summary cards filter the map and the table when clicked.
* **Selected alert.** Facts of the disruption (only what the source gives: incidents show the source's message and "report time / direction not given"; road works show "End time unavailable" when the feed has no end),
  the affected services, and the **buses of that service relative to it** (approaching with the time to impact, inside, cleared) from LTA Bus Arrival positions. **Impact on headway:** predicted arrivals at the
  interchange with and without the estimated delay (a delayed bus holds up those behind it), the resulting headways vs the scheduled headway (Service Headway Master, else the LTA frequency band), and
  *HEADWAY DETERIORATION EXPECTED* when the largest predicted headway is 3 min above scheduled. **Recommended actions** are rule / simulation based, each with its numbers (never a confidence percentage): stretch the next
  departures (the simulator tries 0 ... 6 min on up to 3 on-time departures and keeps the best only if it lowers the largest headway by 1 min), monitor, and - if you enter the temporary headway in force - the
  **restore original headway** check (buses in the last 15% of the trip back within the tolerance).
* **API.** `/api/traffic/overview` (filters: `services, direction, horizon, types, status`), `/detail?alert=&current_hw=`, `/services`, `/route`, `POST /ack`, `/log`, `/settings` (GET / POST / reset). New tables:
  `traffic_setting`, `traffic_state` (the event book), `alert_acknowledgement`. A background loop refreshes every `refresh_s` (60 s) when the LTA key is set; the page also refreshes.

**What is approximate or not in this version**
* **Road works feed:** the DataMall path is `RoadWorks` (override with `LTA_ROADWORKS_PATH`); I could not check it against the live API from here, so verify the path and field names (`RoadName`, `StartDate`, `EndDate`). If it fails,
  the page shows the feed as unavailable and the other alerts carry on.
* **Route geometry** for matching is the straight line between consecutive bus stops (70 m tolerance), not the road-snapped path, so a very curved road can be missed or over-matched. The speed-band feed changes about every
  5 min, so 3 "updates" at a 60 s refresh is mostly a debounce; set `persist_new_data_only` = 1 to count only new LTA snapshots.
* **Bus positions** come from LTA Bus Arrival (sampled stops, up to ~14 calls per selected service); the terminal arrival uses LTA's ETA when the bus is among the next three there, else an estimate at `bus_run_kmh`.
  Fewer than 3 placed buses = no headway forecast (the page says so).
* **Not built:** section 28 (temporary *shorter* headway for early running: it needs the timetable), NEA 2-hour forecast areas (the rain layer uses the live rain gauges), a watchlist, and any automatic notification.
  Halfway deployment stays on its own page (linked from the recommendations). Decision support only: nothing is sent to buses.

## V10.3 - Run AI Optimisation decides: tick a disrupted trip + halfway + adjust, or adjust and continue service

* **Only Run AI Optimisation changes the plan.** Editing the headway, the first departure, the layover or any trip's lateness runs nothing: the plan on the screen stays as it was, the message says
  *Inputs changed - press Run AI Optimisation to update the plan* and the Run button pulses. (The trip table's own columns - scheduled / actual arrival and departure - still refresh while you type.)
* **The AI suggests and ticks.** With **AI decides which trips to disrupt** on (the default) you only type the lateness. On Run the AI looks at every trip that would leave late on its own
  (`auto_late_min`, default 1 min; Settings) and compares, on one common measure of the resulting headways at every stop:
  1. **continue service as it is** (nothing changes),
  2. **adjust the trips and continue service** - no trip is disrupted, no halfway bus; the trips around the late ones are held / released at the interchange,
  3. **tick ONE late trip as Disrupted and deploy a halfway bus for it**, at the best stop, and adjust the trips around it (the other late trips keep running their full route).
  Two trips are never disrupted together (too heavy: the gap doubles). The card says which: *Tick trip N as Disrupted -> deploy halfway at X -> adjust the trips around it*, or *No disruption: adjust
  the trips and continue service*, or *Continue service as it is*; the disrupted checkbox is ticked for you, and a small table shows **what the AI compared** (largest headway, spread, skipped km,
  quality) with its choice marked. **Faster headway recovery** = the largest headway counts most; **Minimise mileage loss** = the km a halfway bus skips count most; Balanced = the score weights.
  API: `disrupted=auto` (the result has `ai` {decision: halfway | adjust | none, suggest, candidates, alternatives}); `disrupted=3` or `3,5` still simulates exactly those trips.
* **Manual mode** (untick *AI decides*, or tick a trip yourself): Run simulates exactly the ticked trips (up to `max_disrupted`, default 4) and the AI adjusts around them. *Clear ticks* unticks all.
* **Removed:** the *Suggest from lateness* button (the AI now decides) and the *Maximum headway gain* preference (it was the same as *Faster headway recovery*).
* **Continue-service screens.** With nothing disrupted the page shows the adjusted plan (holds / releases, the AI plan columns, the simple and street views) without a lost-trip ghost or halfway bus.
  Fixed a page error when the AI chose to continue service (the street map tried to read a lost-trip path that does not exist).
* **Settings you may notice** (defaults in `halfway.py`): minimum layover 7 min (the mandatory break: no trip leaves earlier than its actual arrival + 7), hold up to 20 min, regulation window 5 trips
  (at least 4 each side), `auto_late_min` 1 min. All editable. The comparison table on the card now fits the card. Model `halfway-10.3`.

## V10.0 - Several disrupted trips, chosen from lateness, and the halfway plan with the most headway gain

* **The number of disrupted trips is flexible.** The *Disrupted* column of the trip table is now a set of checkboxes: mark **1 to `max_disrupted` trips** (default 4, setting range 1-6; never the first or
  last trip, which need a trip before and after). API: `disrupted=3` or `disrupted=3,5,6`; the result has `disrupted_all`, `n_lost` and `disrupted` (the first one, for single-trip readers).
* **Suggest from lateness.** Trips whose **arrival is at least `late_disrupt_min` late** (default 15 min, the specification's *MinimumLateForHalfway*) are offered: with nothing marked the card says
  *trips 3, 8 arrive >= 15 min late* and the **Suggest from lateness** button marks them (the latest first, up to the maximum) and simulates. The list is in the result as `late_trips`. Clear removes every mark.
* **One halfway bus per lost trip, each timed by its own lateness.** Each disrupted trip's bus leaves the first stop at its arrival + minimum layover, runs off-service (`offsvc_factor`) and joins at its slot at
  the halfway stop. A bus that cannot get there within *AI replacement start: latest* of its slot is **left out** (*Trip N's bus cannot reach the stop in time; its slot gets no halfway bus and is shared by the
  regulated trips*), so the number of halfway buses follows the lateness. The AI tests every eligible stop and every plan (regulate only / halfway / halfway + regulation) for the whole set.
* **Even share for several lost trips.** The interchange departures share **all** the lost slots: (trips + lost trips) x headway / trips, e.g. two lost trips, 6 regulated trips, 12 min:
  8 slots x 12 / 6 = **16 min** (outside the +-20% band, so a halfway bus is then usually needed). The window covers the 3 trips before the first lost trip and 3 after the last; trips between
  non-adjacent lost trips are regulated too. Bunching / the picture / the plan / the timeline / the map callouts / the trip table show every lost trip and every halfway bus (`R5`, `R6` in the data).
* **Most headway gain.** The AI marks the plan with the **largest fall in average headway** (`best_gain`, `option.gain`): the card shows *Most headway gain: <plan> - the largest headway falls by X min on
  average over every stop (Y%)* with a **View** button, even when the score recommends another plan. A new preference **Maximum headway gain** re-weights the score (regularity 40, largest gap 40,
  recovery 15, cost about 1) and the card then reads *Recommended - Most Headway Gain*.
* **Fixes.** *View simulation* on the recommendation card did nothing (its id clashed with the Simple / Street toggle); it now scrolls to the heatmap.
* **API / model.** Model `halfway-8.0`; options have `repl` (one entry per halfway bus: trip, id, start, lateness of that bus, off-service minutes), `skipped_trips` and `gain`; the audit log stores
  the disrupted trips as a comma list (a single trip stays a number). New settings: `late_disrupt_min`, `max_disrupted`.

**Limits:** the halfway buses of several lost trips all start at the same stop (the AI picks it); running two buses to different stops is not modelled. Lateness is the arrival lateness you type; nothing is
read from live buses. Decision support only: nothing is deployed.

## V9.2 - The interchange departures always share the lost trip's slot (7 slots x 12 min / 6 trips = 14 min)

* **Fix: "AI plan HW 24, never regulates".** Before, the halfway bus was treated as one more trip *inside* the interchange regulation, so the trips around the gap were arranged around it
  and the interchange kept its long gap (24 min; even later departures for the trips after it). Now the interchange departures are regulated **first and on their own**: the trips
  before the gap (3 up) and after it (3 down) are held / released so the headways between them are **equal** - *(trips + 1) slots x scheduled headway / trips*, e.g. 7 x 12 / 6 = **14 min**
  (within +-20% of 12). The trip before the window and the **last** trip of the window stay on their own times, so the correction starts and ends on schedule. The halfway bus is
  placed afterwards, in the middle of the regulated gap (as far as its start window and the bus's arrival allow). Setting `reg_even_share` = 0 restores the older joint calculation.
* **Limits that make it possible.** Defaults are now hold up to **6** min, early release up to **5** min (**6** for future trips when the trips above the gap are too few) and a
  window of **3 + 3** trips (was 5). A 12-min headway needs about 6 min of hold on the last trip before the gap and 4-5 min of early release after it. All are editable; with tighter
  limits the gap cannot be shared fully and the AI says so (`reg_enough` = false) and can prefer halfway + regulation.
* **Scoring.** The *largest-gap* part of the score is now averaged over **every stop** of the route (a halfway bus cannot fix the stops before it starts), like the regularity part.
  With **AI regulation on**, a plain halfway bus that leaves the interchange gap alone is listed as **Reference: no interchange regulation** and is never the recommendation; the choice is
  between *Regulate only* and *Halfway + regulation* (which regulates the interchange exactly as regulate-only does and adds the halfway bus). If regulation alone keeps every headway
  within tolerance the card says so (*a halfway bus is not needed for this gap*); a halfway bus is recommended when regulation cannot close the gap (large headways, tight limits, larger
  delays). Switch **AI regulates headway** off to see the plain halfway plan.
* **Screen.** The card shows *Even share at the interchange: 7 slots x 12 min / 6 trips = 14 min* and the plan ends with *Trip N leaves on its own time: the correction ends here*; the
  **AI plan: HW** column shows the regulated headways; the simple picture shows the 14-min headways with the held / early buses. Model version `halfway-7.0`; the result has `share`
  {slots, trips, hw} and `reg_enough`, options have `ref_only`. Saved old defaults of the regulation limits are moved to the new ones once (migration `mig_v92`).

## V9.1 - Simple before / after picture, and headway regulation of at least 3 + 3 trips at the interchange

* **Simple view (default).** The middle card of the halfway page is now a road strip like the "halfway insertion" picture: buses placed by the time they pass one stop (the halfway stop,
  or the first stop for *regulate only*), the gap in **minutes** between neighbouring buses (green = within +-20% of scheduled, amber / red = long or bunched), **Before - no action** (the lost
  trip is a dashed ghost in the hole), an arrow, **After - the selected option** (the inserted bus is the green star bus; held buses amber, early-released buses cyan, with the minutes),
  *Before / After* result boxes, *Why it works* and a summary banner. The detailed **Street map** (route, off-service arrow, callouts, clock playback) is one click away (*Street map*
  button; the choice is remembered). The time-against-route chart stays below.
* **Interchange regulation over at least 6 trips (3 up + 3 down).** The AI now regulates (holds / releases at the first stop) **at least `reg_min_side` = 3 trips before and 3 after** the
  gap, even if the regulation window is smaller. If the sequence has fewer than 3 trips **above** the disrupted trip, more **future** trips are adjusted instead (2 above -> 4 below, so
  at least 6 in total), and those future trips may **leave early** by up to `reg_early_future` = 5 min (never before arrival + minimum layover) to close the gap. The same holds at the end
  of the sequence (few trips after -> more before). A trip before the first / after the last simulated one is assumed to run on schedule, so trip 1 and the last trip can be regulated too.
  The recommendation card states how many trips are adjusted above and below the gap, and why future trips are used. Settings: *Regulate at least this many trips each side* (0 = off) and
  *Future trips may leave early by up to*. The picture shows the same 3 + 3 (2 + 4 when the top is short). Model version `halfway-6.0`; the result has `span` {up, dn, min_side, short_up,
  show_up, show_dn}.

## V9.0 - AI halfway deployment plan: no approved list needed, the AI picks the stop from timing and headway

* **No approved stops required.** By default (*AI tests every stop*) the optimiser scans every eligible stop of the route (from the 2nd stop, keeping at least *minimum remaining route*
  = 20% of the route and within the *mileage loss limit*; up to 60 stops, with a stride on longer routes) and picks the best by the simulation score. The approved-stops list in
  Settings is now **optional**: *Approved stops list only* restricts the AI to it; API `scope=auto` (default) uses the list if the service has one and scans everything otherwise;
  `scope=all` scans everything and flags the approved stops; `scope=approved` never scans.
* **Timing decides where the bus can join.** The halfway bus (by default the **delayed bus itself**: it can leave the first stop at its arrival + the minimum layover, or at a
  **ready time you type**) runs **off-service** to the halfway stop in `offsvc_factor` x the in-service running time (default **0.7**: no dwell, no stopping - an *unvalidated
  assumption*, editable in Settings; 1 = same as in service). It reaches stop *j* at `ready + factor x running time`; the lost trip's slot there is `scheduled + running time`, so a
  later stop lets the bus catch up. A stop where the bus would be more than *AI replacement start: latest* (10 min) behind the slot is rejected with the reason. The AI weighs how
  much headway a start there recovers against the mileage lost, so the recommended stop moves with the delay, the headway and the running times. If no stop works it says so
  (and suggests a spare bus) instead of inventing one. *Spare bus already at the stop* (`veh=standby`) restores the V8 assumption (starts on the slot).
* **AI Halfway Deployment Plan** (recommendation card): timed steps in order - *send the bus off-service from the first stop at hh:mm*, *it reaches the halfway stop at hh:mm after ~N min /
  km*, *start the trip at hh:mm (waits N min / +N min against the lost trip's slot)*, then each *hold / release* of the neighbouring trips - plus the **start window** at that stop (the
  start times for which the option keeps the headways: outside it the gap is not filled or a bunch appears). If the bus is ready before the slot the plan notes it could simply run the
  whole trip. The map's deployment line and off-service arrow use the same numbers.
* **Full scans stay light.** Every option gets its score and metrics, but the heavy detail (stop-by-stop series, per-trip times) is sent for the best 10 options + regulate-only only; the
  options table shows the best first with *Show all N tested options*; clicking a lower-ranked row fetches its detail (`pick=<stop code>`).
* **API** `/api/halfway/simulate` new parameters: `scope=auto|all|approved`, `veh=own|standby` (default own), `ready=HH:MM`, `pick=<codes>`; new result fields `scan`, `candidates`,
  `ready_note`, and per option `veh, leave_first, off_min, own_arrive, late_start, wait_min, start_window, detail, auto`. Model version `halfway-5.0`; new setting `offsvc_factor` (0.3 - 1.0).

**Limits:** the off-service factor is an assumption, not routing: the run time is not computed from a road route (OSRM/OneMap) and the bus's real position is not used - the delayed bus is
assumed to be at the first stop from its arrival + layover (or your ready time). Change the factor or the ready time to test what-ifs. Decision support only: nothing is deployed.

## V8.1 - Halfway Optimiser: deployment map, timeline, wider headway regulation

* **Map fix (the important one).** The V8.0 halfway page drew on Leaflet panes (`route`, `stops`) it never created. Real Leaflet throws on that, so the route,
  the red / green sections and the star did not draw at all. The panes are now created, and the browser test stub fails the same way real Leaflet does, so this
  cannot pass unnoticed again (all pages were re-checked with the strict stub).
* **Deployment map.** Route, a dot for every stop, the section without the trip (red), where the replacement resumes (green), the halfway start (star) with a
  *Replacement starts hh:mm* callout, a *Trip N disrupted* callout at the first stop, and a dashed purple **off-service move** from the first stop to the halfway
  start (an indicative curve, not a routed road path). Every other approved stop is a numbered square: click it to test that halfway point. A **deployment line**
  above the map states the start time, the off-service run time / km, and whether the disrupted trip's own vehicle could reach the stop in time or a standby bus is needed.
* **Playback.** A clock slider and play / pause draw every trip on the route at that time (green = on time, amber = held, cyan = released early, purple star = the
  replacement); *Show no-action trips* overlays the do-nothing positions in grey. The clock starts when the replacement appears.
* **Deployment timeline** (new card): time against position along the route, one line per trip. The horizontal distance between two lines is the headway; the
  shaded area is the hole left by the lost trip; the terminal headways of the option and of *no action* are printed on the bottom line; a cursor and markers follow the
  map clock. Two bar charts show the headways between consecutive trips (no action vs the option) at the first stop or at the terminal, against the +-20% band.
* **Regulation over 5 or more trips.** The regulation window default is now **5 trips each side** of the disrupted trip (setting range 1-9; was 2). The AI ramps the correction
  over many trips (each held / released by a little) instead of shocking the two nearest ones; the recommendation card says how many trips are regulated and, if the
  sequence is short on one side of the disrupted trip, tells you to raise *Trips simulated* or mark a trip nearer the middle. Average affected headway and EWT still use the
  2 trips either side of the gap, so a wide window does not dilute them; max / min / regularity / bunching use every trip in the window, so the side effects of the ramp are counted.
  The holding-cost normaliser is fixed, so a wider window does not make holding look cheaper. An old stored window of 2 is moved to 5 once (migration `mig_v81`).
* **API.** `/api/halfway/simulate` now also returns `map.stops` (lat, lon, seq, name, code for every stop), `baseline.tsd` / `baseline.dis_path` and, per option, `tsd`
  (every trip's time at every stop, key `R` = the replacement) and `reg_trips`. Model version `halfway-4.0`.

Everything below (V8.0 description) still applies except the regulation window default.

## V8.0 - AI Halfway Optimiser (its own page: **/halfway**, nav button *Halfway Optimiser*)

**What it answers:** *one trip of the sequence is lost or badly late - should we do nothing, regulate the headway, or start a replacement trip from a halfway
stop, and if so where?* V8.0 keeps the V7.0 scenario model (the scheduled timing at the **first stop** is the common reference and the same 10-trip
sequence is simulated) and **adds back the AI layer**: it simulates every option, scores them, regulates the headway of the neighbouring trips, and recommends
the best one with the evidence underneath. **No bus is selected or identified and no live bus data is used** (LTA has no schedule/duty data, so matching a
physical bus to a schedule is unreliable). It is decision support only: nothing is deployed.

**The 10-trip table (at the first stop)** - inputs: service, direction, first scheduled departure, scheduled headway (default from the Service Headway
Master / LTA frequency band), layover, and each trip's arrival lateness; one trip is marked **Disrupted**. For every trip: Scheduled Arrival, Actual Arrival,
Lateness, Next Trip Scheduled Departure, Next Trip Actual Departure and Departure Headway. *Actual departure = the later of the scheduled departure and the
actual arrival + minimum layover.* New in V8.0: **AI plan: Dep / AI plan: HW** columns show the regulated departure of each trip (e.g. `08:24 (+4)` = hold
4 min, `08:37 (-3)` = release 3 min early) and the resulting headway, so a long departure gap (e.g. 30 min) is visibly addressed.

**Options tested on the same trips** (A is the reference, the others are scored):
* **A - No action:** the trip stays missing, nothing is regulated. Baseline only, never recommended.
* **B - Regulate only:** the AI holds / releases the trips within `reg_window` (default 2) either side of the gap so the departure headways even out,
  limited by *max hold* (5 min), *max early release* (3 min) and *minimum departure gap* (2 min). (V8.1: the window is now 5 trips each side by default.) No bus leaves the service.
* **C - Halfway at each approved stop:** a replacement trip starts at the disrupted trip's scheduled time at that stop (+ optional delay) and runs to the terminal.
* **D - Halfway + regulation:** as C, and the AI also **re-times the replacement start** (within *start early / start late* limits, default -5 / +10 min)
  and regulates the neighbouring trips. This is what fixes a late replacement that would otherwise arrive bunched behind the next trip.

**Recommendation score** (0-100, "*Recommended - Highest Simulation Score*", not a % confidence): weighted sum of headway-regularity improvement (30), maximum-gap
reduction (25), recovery-time improvement (20), holding / off-service time (10), mileage loss (10) and new-bunching risk (5). All weights are settings.
An option that creates new bunching (any headway below the bunching limit) is rejected; so are options that leave less than the minimum remaining route,
exceed the mileage limit, or improve the average affected headway by less than the minimum. The best option is the highest positive score among the viable ones.
**Optimisation preference:** *Balanced* (default weights), *Faster headway recovery* (recovery + max-gap weights x1.6) or *Minimise mileage loss* (mileage weight x3).
*Recovery time* = how long the headways stay outside +-20% of scheduled for 3 consecutive points (both configurable).

**Output panels:** AI Recommendation card (action plan with hold / release / replacement start, *No action vs With this option*, score parts, simulated EWT);
options table (score, holding, mileage, avg / max headway, recovery); map (red = section without the trip, green = section where the replacement resumes,
star = halfway start); **Route Travel Time Comparison** (could the disrupted vehicle itself reach the halfway stop in time, or is a standby bus needed);
**Traffic Condition** (LTA speed bands and incidents on the section that is resumed vs skipped); stop-by-stop **heatmap** and *All stops* table; Key Takeaways.

**Settings (on the halfway page):** 25 parameters (trips, layovers, regulation limits and window, start-time window, remaining-route / improvement / mileage
thresholds, recovery tolerance, the six score weights, preference boosts, headway propagation, fallback speed); the **approved halfway stops** (CSV with
`service, direction, stop_code, sequence, enabled`, or add / remove one); and an **audit log**: each press of *Run AI optimisation* stores the scenario, preference,
recommended option, score, holding minutes, every option's metrics, parameters and model version (`halfway-4.0` from V8.1). Same admin token (if set) as the other settings pages.

**API:** `GET /api/halfway/setup?service=&direction=`,
`GET /api/halfway/simulate?service=&direction=&ref=08:10&hw=&layover=&delay=&late=2,3,22,...&disrupted=3&pref=balanced|recovery|mileage&reg=1|0[&stop=&save=1]`,
`GET /api/halfway/runs`, `GET /api/halfway/runs/{id}`, `GET|POST /api/halfway/config`, `POST /api/halfway/points` (+ `/add`, `/delete`, `/clear`).

**Read this before relying on it**
* Everything is **simulated from the numbers you enter** plus the current LTA traffic running times. It shows the operational effect of each option, not what is
  happening on the road now.
* **Regulation limits, score weights and thresholds are starting points**, not validated values - tune them with your operations team. The score ranks options
  within one scenario; it is not a probability.
* The replacement is assumed to be available at the halfway stop at the time shown. The travel-time panel indicates whether the disrupted vehicle could get there;
  whether a standby bus and driver exist is an operational question the simulator does not answer.
* EWT is a simulated waiting-time penalty (`sum h^2 / 2 sum h` minus the scheduled equivalent), not LTA's actual EWT.
* Travel times are estimated from the LTA speed-band running times along the service route. The optional OSRM call only snaps the route drawing to roads;
  there is no separate off-service road-routing calculation, so treat the travel-time panel and the purple off-service arrow as indicative.
* Regulation is limited by the hold / early-release limits and by when each bus actually arrives: on its own it rarely closes a gap the size of a lost trip. Compare it with *halfway + regulation*.


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

## V13.8 - Hourly Time Period Report (zero trip history)

Page: **Running Time Analytics** (`/running-time`) - new card "Hourly Time Period Report".

* Pick service, direction, day type, and **TPR periods** (22 operator slots) or **Hourly** (05:00-00:59).
* **Important bus stops** filter: tick the timing points; first and last stop are always kept. Skipped stops are merged into the pair around them.
* Output: TPR heat table (travel + dwell per stop pair per period, totals, recommended RT), line graph per stop pair through the day, stop-to-stop bar graph for any one period.
* **Download Excel (both directions)**: `TPR D1/D2` sheets in the operator layout (Travel time / Dwell Time / Prop RunTime per pair, totals as Excel formulas), `Graph D1/D2` sheets with native Excel line + bar charts, and a `Method & assumptions` sheet.

Formula per stop-to-stop link, per hour (`tpr_engine.py`):

    driving(h) = live LTA speed-band time x speed_profile[now] / speed_profile[h]   (never below free-flow)
    signals    = km x 2.5 junctions/km x 3 s
    dwell(h)   = P(stop) x (6.06 s + 8.8 s + 0.085 x 8 s) + 1.52 s x pax per bus,  P(stop) = 1 - exp(-pax)
    pax per bus = DataMall passenger volume (tap-in + tap-out, stop, hour) / days in month / services at stop / buses of the service in that hour

The hourly speed profile (`SPEED_PROFILE` in `tpr_engine.py`) is an engineering assumption; edit it or replace it with calibrated values once measured trips exist.
Passenger volume for a service nobody is collecting is downloaded on first use into its own table (`tpr_pv`).

API: `GET /api/insight/tpr?service=54&direction=1&day_type=Weekday&scheme=tpr|hourly&stops=53009,51089,...&pctl=85&recovery=7&pax=3`
and `GET /api/insight/tpr.xlsx?service=54&direction=0&stops1=...&stops2=...` (direction 0 = both).
New dependency: `openpyxl`.
