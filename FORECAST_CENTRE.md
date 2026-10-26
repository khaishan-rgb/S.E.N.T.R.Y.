# S.E.N.T.R.Y. 3.0 – Forecast Centre

The Forecast Centre replaces the Command Centre as the home page (`/command`, also `/forecast`). The previous page is still at `/command/classic`. It answers one question for the OCC: **which bus services will be hit in the next 15, 30 and 60 minutes, and how sure are we?**

## 1. Source audit (phase 1): what was found

| Area | Finding | Action |
|---|---|---|
| Stack | FastAPI + vanilla JS pages, Leaflet, MapLibre GL 4.7.1 loaded on demand (`map3d.js`), SQLite (`BUNCHING_DB`), Render free/paid web service. No build step. | Kept. The Forecast Centre uses the same stack, so it deploys exactly like before (GitHub → Render). |
| Bug | `app.py` defined `TT` twice. The TomTom state (about line 5621) overwrote the planned-timetable store, so `tt_summary()`, timetable upload/clear and timetable-based checks raised `KeyError: 'trips'`. | **Fixed:** the timetable store is renamed `PTT`, and nothing else changes. Test: `t_forecast.py`, under "EXISTING MODULES". |
| Cost risk | TomTom's free tier is now **monthly per API**: Incident Details 2,500/month, Flow Segment Data 20,000/month, flow/incident tiles 200,000/month ([TomTom pricing](https://docs.tomtom.com/pricing)). The traffic engine polls Incident Details every 2–5 min (720–288 calls/day), so the free allowance runs out within days. | **New optional setting** `TOMTOM_INCIDENTS_EVERY_SEC` (e.g. `1800`). The default is unchanged. The Forecast Centre re-uses this call and makes no extra incident calls. |
| Data limits | LTA TrafficIncidents gives a point and a message, with no line or direction. LTA RoadWorks gives a road name and dates, usually with no coordinates. | By policy these can only ever be **UNVERIFIED**. They become verified only when a TomTom record of the same event, which has a line, is grouped with them. |
| Speed bands | LTA Traffic Speed Bands are used by Route Traffic and Traffic-Aware. | **Not used** by the Forecast Centre. It has a test that fails if the forecast cycle touches them. |
| Dependencies | `fastapi 0.116.1`, `uvicorn 0.35.0`, `httpx 0.28.1`, `numpy`, `openpyxl`, all current and maintained. | No new dependency. The geometry engine is numpy only. Shapely / PostGIS were not added: see section 7. |

## 2. Architecture

```
 TomTom Incident Details ─┐ (the same call the traffic engine makes; full geometry now kept: TT["raw"])
 TomTom / HERE Flow ──────┤ provider abstraction (FcTomTom / FcHere), own daily budget + reserve, retry + backoff
 LTA incidents, road works┤ shared caches with the existing pages
 NEA rain gauges + 2-h ───┤
 busrouter.sg lines ──────┤ TR_LINES (all service-directions on the real road) -> RouteIndex (500 m grid)
 rt_trip (running time) ──┘
            │  app.py  fc_world() -> forecast.evaluate()  (thread)  -> AlertBook.update()  -> SQLite (Store)
            ▼
 /api/forecast/state | service | services | alerts/{id}/ack | audit | recovery | health
            ▼
 forecast.html  (MapLibre GL + Leaflet fallback, no external chart library)
```

* **`forecast.py`** is the pure engine: no I/O and no web framework. It holds:
  * the parsers;
  * the geometry and strict matching;
  * rainfall exposure;
  * baselines and forecasts;
  * the service impact and risk rules;
  * the alert book;
  * the store (SQLite, migrations);
  * the demo world.
* **`app.py`** (V17 block at the end) collects data, stores it and serves it. The collector runs every `FORECAST_REFRESH_SEC` (default 120 s) while somebody has the page open in the last 10 minutes, or always with `FORECAST_ALWAYS_ON=1`. Requests never trigger more than one evaluation at a time (lock + cached state). This is the inbound rate limit.
* **Live and demo are separate code paths.** `mode=demo` builds a fictional world (`forecast.demo_world`) and a separate alert book. The live payload never contains demo data. Every demo screen shows DEMO in the following places:
  * the status pill;
  * the map watermark and badge;
  * a tag on each KPI;
  * a tag on each alert;
  * the clock, labelled "fictional time".

## 3. Strict route matching (mandatory rules)

An event affects a service-direction only when **its own line** runs along the service's route line:

1. within **15 m** of the route line;
2. in the **same direction of travel**, with bearings within 35°;
3. for at least **40 m**, or half the event's length when it is shorter;
4. **in force**: start/end time, and road-work approved window.

Everything else is rejected with a written reason, which is shown in the service panel and in the alert ("Why these services"):

| Reason | Example |
|---|---|
| Opposite direction / carriageway | westbound jam vs an eastbound route, same road or the other carriageway up to 45 m away |
| Parallel road | a service road 15–60 m from the route, same direction |
| Crossing | a flyover or junction: the line meets the route at an angle |
| Nearby | within 300 m but not on the route |
| Too little overlap | touches the route for less than the minimum |
| Not in force | ended, or starts after the 60-min horizon |

**Point-only reports** (LTA incidents, LTA road works) have no line and no direction. When they lie within 12 m of a route line they are shown as **"Unverified – not counted"**: a dashed ring on the map, and a line in the service's evidence list. They raise **no alert**, do **not** change any service's risk level, and are **never** counted in "Verified route incidents" or "Bus services potentially affected" (`PARAMS["alert_unverified"] = False`). Anything further than 12 m from the route line is simply "nearby" and ignored.

Tests prove that **300 random parallel or nearby roads 21–330 m from a route never confirm**, and that 200 random stretches on the route (with ±9 m geometry noise) always do.

## 4. Rainfall (estimated)

* **Interpolation:** NEA gauge 5-min totals (× 12 = mm/h) are interpolated by inverse distance (power 2) onto points every 200 m of each route. No estimate is made further than 5 km from every gauge.
* **Confidence:** set by the nearest gauge: high ≤ 2 km, medium ≤ 3.5 km, low ≤ 5 km.
* **Intensity classes (WMO):** light < 2.5 mm/h, moderate < 10, heavy < 50, violent ≥ 50.
* **Counted only at the route:** a stretch of route counts as raining heavily only when a gauge **within 2 km of that stretch itself reports heavy rain** (`rain_count_km`). An estimate that is heavy only because of a gauge further away is shown, but **not counted**.
* **Severe exposure:** at least 20 % of a route in counted heavy rain.
* **Trend:** compares the previous reading set with the current one.
* **+30 and +60 min:** use the NEA 2-hour area forecast.
* **Labelling:** everything is labelled *ESTIMATED from NEA rain gauges – not observed at road level*.

## 5. Forecasts (explainable, rule-based)

* **Speed** at a probe point for t+h:
  * `baseline(t+h) + (current − baseline(t))·e^(−h/30) + damped trend`.
  * The trend is never more than half the current anomaly.
  * The baseline is the median speed on the same day type, ±15 min, over at least 3 days of collected history. Until then the free-flow speed is used, and the page says so.
* **Uncertainty:** an 80 % band from the measured spread of anomalies, or a wide default while history builds.
* **"Recurring" vs "abnormal":** a road that is normally slow at this time is **recurring, not abnormal**. Recurring congestion is already in the timetable, so only the abnormal part counts as extra delay.
* **Jams:**
  * Growth is measured from the jam's own history.
  * Building jams grow at a damped rate; others decay (τ = 40 min).
  * Recovery = time until the delay is under 1 min. No estimate is given while the jam is still building.
* **Service extra running time:** sum of verified jam delays × the share of each jam on the route, plus probe stretches that no jam already explains. An incident inside a verified jam is not counted twice.
* **Risk rules** (labelled rules, not ML):

  | Level | Rule |
  |---|---|
  | Critical | road closed on the route, or extra ≥ max(10 min, 0.8 × headway) |
  | High | extra ≥ max(5 min, 0.5 × headway) |
  | Watch | extra ≥ 2 min, or ≥ 20 % of the route in counted heavy rain (unverified reports never raise a level) |
* **Accuracy is measured, never claimed.** Every probe forecast is stored (`fc_fc`) and settled against the next observation at its target time. The "Forecast model confidence" KPI shows the measured band coverage and mean error only after 30 checks; until then it says **Not measured yet**.
* **Machine learning** (LightGBM etc.) is not used. The snapshots it needs (`fc_probe`, `fc_jam`, `fc_cover`) are being collected. Train only once there are several weeks of data, using a time-based split (no future leakage), and compare against this baseline on the same `fc_fc` checks.

## 6. Alerts

* **Ranking:** escalated first, then critical → high → watch → info, then score.
* **Grouping and duplicates:**
  * One alert per event, listing all verified services.
  * Duplicate reports of the same event (e.g. an LTA point 2 m from a TomTom line) are grouped into one event with both sources.
* **No repeats:** an unchanged condition does not re-alert.
* **Re-alert** when the condition materially worsens: the level rises, the 30-min delay grows by max(3 min, 50 %), or 3 more services are affected. A re-alert clears the acknowledgement.
* **Acknowledge:** records the name and time, and fades the card.
* **Cleared only when verified:** the alert is absent in 2 consecutive cycles with fresh data. While the source is stale the alert is kept and marked "Source stale".
* **Escalation:** critical and unacknowledged for 15 min.
* **Reopen:** an alert that returns within 30 min reopens with the same id.
* **Audit:** everything goes to `fc_audit`, shown on the Audit tab and at `/api/forecast/audit`.
* **Notification bell:** high and critical forecast alerts also appear in the platform bell.

Each alert has:
* event type, location, affected road segment (route km), verified services, direction;
* predicted delay at +15 / +30 / +60 and the horizon it peaks at;
* confidence / data quality, source and times;
* recommended action (advisory), links to Headway Control / Recovery Decision Engine / Halfway Planner / Bunching / Route Traffic / Diversion Maps, carrying `svc` and `dir`;
* Acknowledge.

Recovery options are **advisory only**. They are checked against:
* the 8-min maximum adjustment;
* the 7-min layover;
* departed trips locked;
* approved halfway points;
* subsequent trips.

The Recovery Decision Engine does the full EWT comparison. Nothing is sent without SC approval.

## 7. Database (SQLite, schema version 1)

Tables are created by `forecast.Store.migrate()` (versioned in `fc_meta.schema_version`):

| Table | Holds | Kept |
|---|---|---|
| `fc_probe(key, ts, svc, dir, lat, lon, cur, ff, conf, closure, provider)` | every probe reading (speed history) | 35 days (`FORECAST_KEEP_DAYS`) |
| `fc_jam(ts, id, cell, mag, delay_s, length_m, road)` | jam snapshots every 10 min (recurrence) | 35 days |
| `fc_cover(day, slot, dt, n)` | when the collector was running (recurrence denominators) | 35 days |
| `fc_fc(issued, key, h, target, pred, lo, hi, obs, err)` | forecasts and their later observations (calibration) | 14 days |
| `fc_alert(id, status, updated, body)` | alerts (reloaded after a restart) | resolved: 30 days |
| `fc_audit(id, ts, alert_id, action, by_name, level, detail, title)` | audit trail | 90 days |

It uses the same file as the rest of the platform (`BUNCHING_DB`) unless `FORECAST_DB` is set.

**Render free plan:** the file is wiped on every deploy, restart and sleep, so history and calibration start again. Use a paid instance with a persistent disk (see the README "Keeping 30 days on Render").

**Why not PostgreSQL/PostGIS now:**
* The matching works on about 1,100 route lines with a numpy grid index: built once a day in about 7 s, about 40 MB; an island-wide evaluation takes about 2–3 s in a thread.
* PostGIS would need a managed database (an extra cost) and a migration of every module that uses SQLite.

**Path if needed:** the same tables map one-to-one. Route lines become `geometry(LineString, 3414)` (SVY21), matching becomes `ST_DWithin` + `ST_Length(ST_Intersection(ST_Buffer(route, 15), event))` + a bearing check.

## 8. Traffic providers (abstraction: `FcProvider` → `FcTomTom`, `FcHere`)

| | TomTom (primary, implemented) | HERE Traffic v7 (secondary, implemented, not validated live) | Google Routes (not implemented) |
|---|---|---|---|
| Use here | incidents & jams (lines in travel direction), Flow Segment Data probes (segment geometry), raster flow tiles | flow by shape (`/v7/flow`, `locationReferencing=shape`) | travel-time validation of selected routes only |
| Direction | yes (incident line order; flow segment geometry) | yes (shape order) | per requested route |
| Free tier | monthly per API: Incidents 2,500, Flow Segment 20,000, tiles 200,000 ([pricing](https://docs.tomtom.com/pricing)) | see [HERE pricing](https://www.here.com/developer) | billed per request; check Google Maps Platform pricing |
| Switch | `FORECAST_TRAFFIC_PROVIDER=tomtom` | `FORECAST_TRAFFIC_PROVIDER=here` + `HERE_API_KEY` | – |

Before relying on HERE, compare it on a week of probes: run both on the same points and compare the `fc_fc` errors. Google Routes is worth adding only for a handful of route travel-time checks, because of cost and licensing.

## 9. Configuration (Render → Environment; never in code or GitHub)

See `.env.example`. The settings for the Forecast Centre are:

| Variable | Default | What |
|---|---|---|
| `TOMTOM_API_KEY` | – | TomTom key (incidents, flow, tiles). Without it, incident / congestion KPIs say **Unavailable**. |
| `TOMTOM_INCIDENTS_EVERY_SEC` | 120 / 300 | Slows the shared incident call (`1800` ≈ 1,440 calls a month, inside the free 2,500). |
| `FORECAST_PROBE_CAP` | 200 | Flow probe calls a day for the forecast (~6,000 a month). |
| `FORECAST_TOMTOM_RESERVE` | 800 | Never take TomTom's shared daily counter (`TOMTOM_DAILY_CAP`) below this. |
| `FORECAST_SERVICES` | `BUNCHING_SERVICES` | Services probed continuously (max 8); selected services are probed on demand. |
| `FORECAST_PROBES_PER_ROUTE` | 6 | Probe points per service-direction (one every ~2 km). |
| `FORECAST_PROBE_EVERY_MIN` | 15 | Minimum minutes between probes of one point. |
| `FORECAST_REFRESH_SEC` | 120 | Evaluation cycle. |
| `FORECAST_ALWAYS_ON` | off | `1` = collect even when nobody has the page open (builds history faster; uses more quota). |
| `FORECAST_KEEP_DAYS` | 35 | History kept. |
| `FORECAST_DB` | `BUNCHING_DB` | Separate SQLite file if wanted. |
| `FORECAST_TRAFFIC_PROVIDER` | tomtom | `tomtom` or `here`. |
| `HERE_API_KEY` | – | Optional HERE key. |

## 10. API

| Endpoint | Purpose |
|---|---|
| `GET /api/forecast/state?mode=live\|demo&step=0-6` | everything on the page: KPIs, events with matches, zones, services, comparison, alerts, audit, sources, config |
| `GET /api/forecast/service?svc=&dir=&mode=&step=` | service intelligence (route, stops, verified / unverified / rejected evidence with reasons, probes, rain, extra time, recovery) |
| `GET /api/forecast/services` | services for the search |
| `POST /api/forecast/alerts/{id}/ack` `{by, note}` | acknowledge (demo acknowledgements are not stored) |
| `GET /api/forecast/audit?alert=&limit=` | audit trail |
| `GET /api/forecast/recovery?svc=&dir=&extra=&hw=&layover=` | advisory recovery options |
| `GET /api/forecast/health` | sources, budgets, calibration, parameters |

## 11. Showcase mode

* **Start it:** press **Demo**, then **Play** (or **Present** for full screen). It can also be opened directly with `/command?demo=1&step=0&present=1&play=1`.
* **Content:** seven fictional steps, deterministic and fully offline (fictional services X101–X410). Each runs through the real engine and alert book:
  1. normal operations;
  2. traffic builds up;
  3. deterioration detected;
  4. route impact verified;
  5. prediction generated;
  6. OCC alerted;
  7. recovery option evaluated.
* **Step 4 shows the matching rules:**
  * Verified: X101 eastbound and X305.
  * Rejected: X101 westbound (opposite carriageway), X102 (parallel road), X201 (flyover).
* **Presenter keys:** ← → change step, Space plays or pauses, Esc leaves.

## 12. Tests

| Test | What |
|---|---|
| `tests/t_forecast.py` (82 checks) | parsing (TomTom, HERE, LTA, NEA samples in the documented formats); route intersection; direction; parallel / nearby / crossing rejection (incl. 300 random cases); road-work windows; rainfall classes and exposure; forecast maths; alert de-duplication, re-alert, acknowledge, clearance with fresh vs stale data, escalation, reopen, audit; store and calibration; demo determinism and separation; the live cycle on a fake HTTP client (budget, stale data, API failure → Unavailable); no API key in payloads; no LTA speed bands; JSON safety; the timetable fix |
| `r3d/t_forecast_page.js` (53 checks) | real MapLibre on an offline city: 1920×1080, 2560×1440, 3840×2160, 1366×768 and phone layouts; KPIs; layers; horizon scrubber; alert acknowledgement and audit; service evidence; demo story; presentation mode; Leaflet fallback; no page errors |

Run the engine tests from the project folder (no network, no keys, no extra packages beyond numpy):

```
python3 tests/t_forecast.py . tests/stubs
```

`tests/stubs` holds tiny stand-ins for FastAPI and httpx, so the test runs without a server. The browser test runs in the development harness (Playwright + offline MapLibre).

## 13. Feature completion report

| Feature | Status |
|---|---|
| Forecast Centre as the home page, futuristic OCC UI, 1080p / 1440p / 4K / laptop / phone, presentation mode | **Done**, tested |
| Command bar: branding, title, SGT time, API health, Live / Demo, last refresh, critical count, NEA weather, notifications and settings (platform bar), full screen | **Done** |
| 8 KPIs from real data, Unavailable when missing, stale marking | **Done**, tested |
| Singapore map (MapLibre): real route geometry, stops, TomTom flow tiles, congestion heat, incidents, road works, rainfall, predicted delays, arrows, risk markers, tooltips, layer control, search, route selection, 15 / 30 / 60 slider | **Done** (2D Leaflet fallback) |
| Traffic provider abstraction, TomTom live | **Done**, tested on documented sample responses; **live keys not exercised from the build environment** |
| HERE Traffic v7 | **Implemented, not validated** against a live response (no key) |
| Historical snapshots, baseline, deterioration %, build-up rate, travel-time increase, persistence, recovery trend, abnormal vs recurring | **Done**. Baselines need ≥ 3 days of collected history; free-flow is used, and labelled, until then. |
| Strict route matching (direction, carriageway, parallel, crossing, validity, bus passability), explanations, unverified class | **Done**, tested |
| Rainfall exposure model (estimated, confidence, trend, NEA 2-h forecast) | **Done**, tested |
| 15 / 30 / 60 forecasts with uncertainty, service risk rules | **Done** (rule-based, labelled) |
| Measured accuracy / calibration | **Done**: shown only after 30 checks |
| ML (LightGBM) | **Not done**: needs weeks of collected data; the data is now being collected |
| Smart alert centre (rank, group, no repeats, re-alert, fade, history, clearance, escalation, audit) | **Done**, tested |
| Bus service intelligence (route, direction, stops, traffic, events, rain, running time hist / predicted, extra delay, critical stretches, recovery time) | **Done**. Running-time history only for services measured by Running Time Analytics. |
| CFMS (bus positions, trips, layover risk, bunching / EWT / mileage-loss forecasts) | **Not available**: needs internal CFMS data |
| Recovery integration (deep links, advisory options checked against the rules) | **Done** |
| Showcase mode (deterministic fictional scenario, works offline) | **Done**, tested |
| SQLite schema + migrations, retention | **Done** |
| PostgreSQL / PostGIS, React / TypeScript / Vite rewrite | **Not done, deliberately**: see sections 1 and 7 |
| Dockerfile + docker-compose | **Added, not built here** (no Docker daemon in the build environment) |

## 13b. V17.1 changes (strict counting, control-room look)

* **Strict counting:**
  * Unverified point reports no longer create alerts or affect risk.
  * Rain is counted only where a gauge within 2 km of the route reports heavy rain.
* **Look:** the page has the control-room style of an OCC forecast wall:
  * electric-blue grid backdrop;
  * an angled, glowing title plate;
  * corner-bracket panels and angled panel tabs;
  * a ring gauge on every KPI. Each ring is a real share, for example verified out of all reports near routes, or wet gauges out of all gauges. Its meaning is in the tooltip.

## 14. Remaining limitations and next priorities

1. **Run it live with the Render keys** and check the first day:
   * `/api/forecast/health`;
   * the Data integrity panel;
   * that TomTom incident lines are in the travel direction (the explanations show the bearing difference);
   * HERE, if used.
2. **Set `TOMTOM_INCIDENTS_EVERY_SEC`** for the free tier, or move to a paid TomTom plan, because the current 2-minute polling exceeds 2,500 calls a month.
3. **Move to a persistent disk** (paid Render instance), so baselines, recurrence and calibration accumulate.
4. **After 4–6 weeks of history:** train and compare a LightGBM model on `fc_probe` / `fc_jam` with a time-based split. Switch only if it beats the rule-based baseline on `fc_fc`.
5. **When CFMS data is available:** add trip-level layover risk, bunching and EWT forecasts.
6. **LTA road works** have no geometry. Treat them as verified only with a TomTom "road works" line, or add a road-name → OneMap road-geometry lookup later.
