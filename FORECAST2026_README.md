# S.E.N.T.R.Y — Forecast Centre 2026

A working public-API prototype and Singapore demonstration command centre, rebuilt around the supplied reference. The map occupies about 60% of the main row. Four risk cards, a network-status rail, operational alerts, camera images and five analytics panels share a single forecast horizon and filter state.

**This is not a validated production prediction system.** Statistical baselines are used until adequate training and outcome data exist. Demo values, coordinates, passengers, buses, incidents and counterfactual assumptions are simulated and labelled. No bus regulation or dispatch action is executed.

## Install into the existing GitHub / Render deployment

Replace or add these files in the repository root:

1. `app.py` — retains existing routes and collectors; adds the centre adapter and journal endpoints.
2. `forecast.html` — replaces the old Forecast Centre page.
3. `forecast2026.py` — independent model/view-model functions and namespaced SQLite alert journal.
4. `forecast2026.css` — responsive reference-inspired TV/control-room layout.
5. `forecast2026.js` — controller, analytics, playback, alerts and source UI.
6. `forecast2026-core.js` — pure frontend formatting, query, freshness and chart helpers.
7. `forecast2026-map.js` — MapLibre renderer and explicit offline schematic fallback.

Keep the existing `forecast.py`, `bunching.py`, `map3d.js`, `basemap.js`, other application modules and `requirements.txt`. Their provider/route-matching functionality remains in use. Restart/redeploy with the existing `uvicorn app:app` command.

Open `/forecast` or the existing `/command` page. Demo is the default for the showcase. For public-source observations choose LIVE, or use `/forecast?mode=live`. The existing application environment must already have its valid LTA/TomTom credentials. No credentials are embedded or changed by this update.

Examples:

- `/forecast?mode=demo&scenario=combined&h=30`
- `/forecast?mode=demo&scenario=bunching&h=30&present=1`
- `/forecast?mode=live&h=15&present=1`

TV mode requests fullscreen, hides controller-only filters, keeps forecast/source labels, and rotates prioritised alert/watch pages every 12 seconds. If fullscreen is denied, the TV layout still works. A single click may be required for browser fullscreen permission.

## Standalone demonstration

The ZIP also contains a minimal server that **does not import the existing main application or require internal systems**:

```bash
python -m pip install -r requirements.txt
uvicorn forecast2026_server:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080/`. For the standalone route include the supplied `forecast2026_server.py`, `map3d.js` and `basemap.js` alongside the seven centre files. The standalone LIVE option explicitly reports unavailable live providers; use the existing `app.py` deployment for real integration.

`SENTRY2026_DB` selects the standalone journal file; it defaults to `sentry2026.db`. In the main application, the journal uses the existing `FORECAST_DB` / `BUNCHING_DB` file in its own `fc26_alert` and `fc26_audit` tables. Existing tables are not dropped or changed.

## Interface and reference layout

- Deep midnight navy, electric cyan edging, understated glass panels, Barlow Condensed headings and Inter/system text.
- Four selectable risk cards: heavy loading, traffic buildup, **exactly three-bus** bunching and excessive headway gaps.
- Left rail: sampled bus positions, passenger-load coverage, engine availability, projected headway and a 120-minute risk timeline.
- Centre: Singapore vector map, route-direction arrows, bus positions, incident/cone/closure symbols, rainfall gauges, loading, bunching/gap markers and green-to-red forecast heat.
- Right: priority-scored operational alerts with root cause, service/location, evidence quality and occurrence time; source camera stills when available. Demo does not invent camera images.
- Bottom: load outlook, headway evolution, separate 2BB/3BB analysis, rotating top-ten services and advisory recommendations.
- Multi-service checkbox picker, operator intersection and CBS exclusion (950, 160, 170, 170X) remain available in controller mode. Risk cards filter map/alerts. Clear filters restores the network scope.
- NOW / NEXT 15M / NEXT 30M / NEXT 60M / NEXT 2H are **lead times**, not additional delay. The selected horizon drives one consistent backend view.
- MapLibre uses the existing OpenFreeMap/OpenStreetMap vector style and attribution. External Leaflet/MapLibre libraries and Google Fonts need outbound access. System-font fallbacks work without Google Fonts. If vector libraries/tiles fail, the centre uses a clearly labelled approximate Singapore route schematic rather than a fabricated map screenshot. Schematic routes can be clicked to select a service; 3D is disabled when unavailable.

## Evidence states and availability

Source selection has DEMO and LIVE. Within LIVE, observations are labelled LIVE/current and future baselines ESTIMATED. ESTIMATED is a derived forecast, not a third invented raw feed.

| Engine | Demo | Live integration and limits |
|---|---|---|
| Passenger load | Boarding-minus-alighting conservation baseline with a simulated capacity; 0/15/30/60/120 min | Current LTA categories SEA/SDA/LSD only. Categories never become fabricated percentage occupancy or passenger counts. Future occupancy stays unavailable without demand/capacity/history. |
| Traffic | Simulated accident, roadwork, congestion and rain scenarios | Existing TomTom incident/flow and NEA pipeline, strict geometry/direction matching. Existing 15/30/60 estimates retained. 120 min is a lower-confidence, explicitly estimated decay/trend baseline. |
| Three-bus bunching | Consecutive downstream arrival times compress; all three simulated buses converge on the map | Existing collector arrival-time groups; exactly three kept separate from 2BB and 4BB+. Current and supported near-term predictions (up to 30 min). 60/120 min remain unavailable. |
| Long gaps | Scheduled versus projected arrival separation; initial excess threshold five minutes | Existing collector gap models, including scheduled headway and onset. Only supported horizons are exposed. No projected passenger-exposure number is invented. |

Default thresholds are in `forecast2026.THRESHOLDS`: 70/85/95% design load levels, 3-minute bunching threshold and 5-minute excess gap. They are prototype defaults, not official operator standards. Live bunching still respects the existing collector's configured thresholds and horizon.

Direction and geometry are authoritative. A nearby or opposite-carriageway point report is not promoted to verified route impact. The new adapter consumes existing verified matches; it does not replace them with proximity tests. Live bus coordinates remain observations at future horizons; the map explicitly says that projected live bus coordinates are unavailable. Future positions are simulated only in DEMO.

Data freshness uses provider state and a 150-second observation window in the new adapter. Invalid/stale/no-data collector samples do not become zero-risk reports. Unavailable modules show a dash and coverage warnings. API errors retain the last successful view with an advancing age and degraded/delayed status. No stale source is silently converted to LIVE.

## Demo scenarios

Seven scenario selectors: simultaneous network pressure, evening demand surge, AYE accident, Orchard roadworks, Bishan three-bus convergence, Bedok gap and eastern rainfall. Real Singapore place/service names provide context; vehicle IDs use `SIM-`, route shapes are approximate demonstration corridors and stops are simulated. These are not claims about current operators or actual service alignments.

Play cycles the five forecast horizons every seven seconds. Stop playback to inspect a horizon. The model recomputes scenario demand, headways, risk counts and bus positions. Simulated AYE delays create downstream headway gaps; roadworks recovery compresses consecutive arrival separations into three-bus clusters. These causal examples are simulation assumptions, not validated live causal models. It is not a static image.

## APIs and module boundaries

```text
Existing TomTom / LTA / NEA collectors
          ↓
Existing forecast.py: line intersection, direction, rain exposure, forecasts
Existing bunching.py: consecutive arrival-time groups and gaps
          ↓
app.py /api/forecast/centre adapter
          ↓
forecast2026.py → filtered view model + namespaced alert journal
          ↓
forecast2026.js → four cards / map / alerts / charts / advisory comparison
```

- `GET /api/forecast/centre?mode=demo&horizon=30&scenario=combined`
- Filters: `services=14,166`, `exclude_cbs=1`, `category=loading|traffic|bunching|gap|all`.
- `tick` advances simulated bus positions during playback.
- `POST /api/forecast/centre/alerts`: `{namespace, id, status}` where status is `acknowledged`, `monitoring` or `resolved`.
- Existing `/api/cameras` supplies real still images, never simulated photographs.
- Existing `/api/forecast/service` and all legacy provider/engine endpoints remain available.

The core is pure Python, the browser is modular native ES modules and the host is existing FastAPI. This avoids introducing a React build/deploy requirement into the supplied application. React/TypeScript, PostGIS, PostgreSQL, Redis and WebSockets are migration targets for a larger deployment, not dependencies that this prototype pretends to have deployed. Isolate them behind the current view-model/collector contracts.

## Alert lifecycle and recommendations

New risks are prioritised by a documented heuristic severity × evidence-quality × exposure × urgency score. The score is **not a calibrated event probability**. Duplicate stable IDs update the existing record. Acknowledged/monitoring alerts stay in the queue at reduced emphasis. Material score deterioration re-alerts. Demo absence resolves simulated risks. Live resolution is conservative and controller-driven until adapters expose complete per-service evidence; missing or partial data never auto-clears a live risk.

Journals are separated by source mode, demo scenario and forecast horizon. Demo acknowledgements cannot change live alerts. Applying filters cannot resolve hidden alerts. History is searchable and audit records retain actor, action and timestamp.

Recommendations explain the supporting risk and require controller approval. No regulation command or bus-deployment API exists. Live duty/vehicle/layover/meal constraints are not available, so action feasibility and numeric improvements remain unvalidated. Demo comparisons can show an explicitly assumed spare-bus equal-gap split with a simulated 10-minute layover, 7-minute minimum and protected break. These assumptions are not presented as live authority.

## Validation and launch checks

Run the tested, dependency-free model and ASGI checks:

```bash
python -m unittest test_forecast2026.py test_forecast2026_api.py test_forecast2026_geometry.py test_forecast_tiles.py test_forecast_bunching.py
node test_forecast2026_ui.cjs
node --check forecast2026.js
node --check forecast2026-map.js
node --check forecast2026-core.js
python -m py_compile app.py forecast2026.py forecast2026_server.py
```

37 Python tests passed at delivery, plus frontend helper checks and syntax checks. Coverage includes demand conservation/capacity, missing data, time-based 2BB/3BB/4BB separation, headway thresholds, all scenarios/horizons, opposite/parallel/crossing rejection, real ASGI API responses/assets, stale-source handling, categorical-load integrity, filters, lifecycle/ack/history, deterioration re-alerting and constraint-aware counterfactuals. `outcome_metrics()` computes MAE, precision, recall, false-alarm/miss rates and Brier score when verified outcomes are supplied; empty outcomes never produce an invented accuracy percentage.

Optional real viewport checks:

```bash
python -m pip install playwright
python -m playwright install chromium
python test_forecast2026_browser.py
```

This starts the standalone demo, checks 1366×768 / 1920×1080 / 3840×2160 / mobile, blocks external resources to verify the explicit offline fallback, exercises controls and writes screenshots. **It was not completed here:** the execution sandbox blocks Chromium's local socket operations. The screenshots have not been claimed as verified. Real TV/browser appearance, provider licensing/coverage and live deployments still need a visual/source check after redeployment.

The older `test_forecast_map.cjs`, `test_forecast_tv.cjs` and `test_forecast_controls.cjs` describe the previous HTML implementation. They are superseded by the new 2026 tests; do not treat their removed function names as current contracts.

## Next validation gates

1. Verify the reference-inspired TV layout on the target screen and source credentials in the actual deployment.
2. Collect matched, timestamped passenger counts/capacity, ETA and departure history before training or forecasting live occupancy.
3. Backtest each service/horizon with held-out outcomes; calibrate confidence and operational thresholds and measure missed/false alarms.
4. Add transactional multi-worker persistence, retention, authenticated/CSRF-protected controller actions and outcome ingestion for production scale.
5. Integrate actual duty, layover, vehicle availability and protected breaks before marking operational actions feasible.
6. Only then introduce a trained LightGBM/XGBoost model and future CFMS/control adapters. No production-readiness claim is made.
