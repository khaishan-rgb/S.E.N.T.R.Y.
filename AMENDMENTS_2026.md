# S.E.N.T.R.Y Forecast Centre — 27 October 2026 amendments

Apply this update to the existing project. The navy/cyan visual identity, four risk modules, existing map and provider collectors are preserved.

## Upload to GitHub

Extract `SENTRY-2026-amendments.zip`. Upload its files into the same repository directory as your existing `app.py`, replacing matching files. Keep existing environment variables, authentication, SQLite databases and other project files.

Required dashboard files:

- `app.py`
- `forecast.html`
- `forecast2026.py`
- **`forecast2026_amendments.py` — NEW; required**
- `forecast2026.js`
- `forecast2026-core.js`
- `forecast2026-map.js`
- `forecast2026.css`

The archive also includes the compatible `bunching.py`, `basemap.js`, `map3d.js`, optional standalone `forecast2026_server.py`, tests and documentation. Keep all files together. No new production dependencies are required beyond your existing project requirements.

Restart the existing application with `uvicorn app:app --host 0.0.0.0 --port 8000` (or retain your existing Render start command). Open `/forecast` or `/command` and hard refresh once. Asset versions are updated to invalidate older cached dashboard modules.

**Live deployment uses `app:app`.** `forecast2026_server:app` is the separate demonstration server, without the main application's live provider integrations.

## What changed

- Larger type: desktop KPI values 36px, headings 18px, body 14–16px and secondary labels at least 13px. Separate larger sizing at 4K; no document zoom. Smaller screens reorganise and scroll vertically rather than shrink operational text.
- Searchable multiple-service selection, removable chips, All Services, CLEAR SERVICE and RESET ALL. Selecting an already selected service removes it. Switching risk modules retains service selections. RESET ALL returns to NOW, all services/operators, CBS included and the default 0.8-minute delay threshold, then fits Singapore. CLEAR SERVICE retains other filters.
- Shared All / 0.8 / 1 / 2 / 3 / 5 / custom-minute threshold. Changes affect traffic counts, alerts, map flow segments, affected services, average extra time and every forecast horizon. Yellow 0.8–<2, orange 2–<5 and red 5+ minutes. Threshold policy is centralised and configurable. Safety closures and incident/roadwork evidence remain visible independently of unknown or small congestion delay.
- HOURLY HEADWAY INTELLIGENCE replaces the small headway graph. Network/service/direction scope, hourly/15-minute intervals, three distinct series, observed/forecast boundary, abnormal-gap highlighting, hover/keyboard values, scheduled-versus-actual difference, and an expanded table/chart. Passenger loading and three-bus analysis remain available through adjacent tabs.
- Camera views reuse the existing cached LTA Traffic Images v2 and data.gov.sg adapters. They no longer require a camera within 350m of direction 1, or discard all older images. Selecting a traffic alert/segment/marker requests the nearest four available cameras. Capture timestamps, stale/unknown warnings, source diagnostics, and image enlargement are provided. TV mode rotates two images at a time on smaller displays, retaining all four nearest results.
- Map routes use cyan for selected routes and faded context routes. Excluded CBS routes are removed. Road labels and popups are larger. Event markers are reused across refreshes and offset at nearby positions; threshold changes do not reset pan/zoom. Traffic popups show service/direction, road, extra time, baseline and update time. Geometry/direction matching is unchanged.
- Alerts show service/direction, risk/location, condition/forecast impact, extra time, bus references where available, action and timestamp. Priority ordering, acknowledgement, monitoring, resolution and audit history remain intact. Filtering cannot resolve an alert.

## Data limitations and source integrity

### Traffic

When an adapter explicitly supplies `current_travel_time_s` and `typical_travel_time_s`, current additional delay is their difference, in minutes. Otherwise the existing provider-reported / free-flow and statistical baselines are used and labelled accordingly. Historical typical travel time is **not** claimed when unavailable. The existing strict route overlap and direction checks remain responsible for impact eligibility; camera proximity never establishes impact.

Unknown delay is not zero. Closures remain visible even if their numeric delay is unknown. An incident or roadwork below the selected congestion threshold can remain visible as source evidence without increasing the congestion KPI.

### Hourly headways

The live collector's downstream ETA separations are estimates of selected bus pairs, **not** recorded actual stop-arrival headways or an all-fleet census. Forecast averages use the available focus pairs within the collector's supported 30-minute coverage. Missing later forecasts remain blank.

Live actual hourly history is currently unavailable unless an upstream adapter provides verified arrival records. The UI explicitly displays **ACTUAL DATA UNAVAILABLE** rather than inventing a historical line. Verified records can be supplied as `headway_history` rows:

```json
{"svc":"14","dir":1,"at":1793080800,"headway":12.5,"kind":"actual","verified":true}
```

Configured Service Headway Master time windows supply scheduled values in their applicable day/direction/time slots. A single currently known scheduled headway is only plotted in its current interval; it is not copied into unverified hours. The chart covers a 05:00–04:59 Singapore operating-day window; blank periods do not imply a service operates then. A validated timetable adapter can supply additional `headway_schedule` rows with `svc`, `dir`, `at`, `headway` and `verified:true`.

DEMO has explicitly simulated hourly data. No simulated observations are inserted into LIVE. Evidence-quality percentages are heuristic confidence indicators, not measured forecast accuracy.

### Cameras

Main-app endpoint: `/api/forecast/centre/cameras`, optionally `?lat=1.30&lon=103.80`. This calls the existing shared camera cache, avoiding a separate polling stream or static-route download. Backend source integrations:

- LTA: `https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2`
- Fallback: `https://api.data.gov.sg/v1/transport/traffic-images`

Keep `LTA_ACCOUNT_KEY` in server environment variables. Optional `DATAGOV_API_KEY` / `DATA_GOV_SG_KEY` raises the fallback's rate limit. Missing LTA configuration and provider failures appear in camera diagnostics. A source image URL stays paired with its own capture timestamp; unknown times are unverified. Images older than 10 minutes are marked stale. Failure retains available last-known images with warnings. Nearest cameras provide visual context and may face a different road or carriageway.

No private deployment credentials were available for an authenticated live-provider verification in this workspace. The API adapter/cache contracts were tested using fixtures; successful external responses still depend on deployed credentials, provider availability and network access.

## Validation

Passed: 54 Python tests plus three Node suites, covering forecast/geometry regressions, exact delay boundaries, typical-time calculation, duplicate counts, closures, synchronized horizons, filter-safe alert lifecycle, nearest-camera selection/timestamp freshness, cached camera API wiring, scheduled time windows, verified actual-only history, UI startup/event wiring, chips/reset, direction/interval changes, analytics tabs, and map marker reuse without viewport resets.

```bash
python -m unittest test_forecast2026_amendments.py test_forecast2026_live.py test_forecast2026_group_metadata.py test_forecast2026.py test_forecast2026_api.py test_forecast2026_geometry.py test_forecast_tiles.py test_forecast_bunching.py
node test_forecast2026_ui.cjs
node test_forecast2026_amendments.cjs
node test_forecast2026_controls.cjs
```

`test_forecast2026_controls.cjs` executes the real UI module against a minimal DOM and an explicitly simulated fixture. This checks interaction wiring, not browser layout.

Browser/TV validation script (optional development dependency):

```bash
pip install playwright
playwright install chromium
python test_forecast2026_browser.py
```

Set `CHROMIUM_PATH` if using a particular installed browser. The script checks 1366×768, 1920×1080, 3840×2160 and mobile, plus filters, tabs, chart values and lifecycle interactions. Chromium execution was blocked by this workspace's socket sandbox, so **browser screenshots and TV alignment have not been visually verified here**. The script blocks external map requests deliberately to exercise the labelled geographic fallback; verify real vector tiles and live camera images separately in the deployed app.
