# Selected-route visibility and repeating TV spotlight

## Install this update

Apply to the existing amended S.E.N.T.R.Y project. Replace these **six application files** in the same GitHub directory:

1. `app.py`
2. `forecast.html`
3. `forecast2026.css`
4. `forecast2026.js`
5. `forecast2026-core.js`
6. `forecast2026-map.js`

The ZIP includes the optional updated `forecast2026_server.py` for the standalone demonstration server, regression tests and this guide. Keep existing database files, environment variables and API keys. No new production dependencies are needed. Redeploy/restart the existing `app:app` application, then hard refresh the browser once. Asset version: `20261027-route-spotlight`.

## Brighter selected routes

Selecting a service now displays a thick illuminated route border with a dark contrasting casing. Direction 1 uses bright cyan; direction 2 uses cool blue. Larger white arrows show travel direction, with service/direction labels and endpoint dots. A visible ROUTE badge identifies the highlighted service and directions. Other network routes fade while a route is selected.

Traffic delay colours remain visible in the narrower centre of the route, so the selection highlight does not hide congestion severity. The same stronger route treatment is available in the explicitly labelled geographic fallback when vector tiles cannot load.

Choosing a service through the picker also fits the map to its route. Multiple selected services remain highlighted. Routine data refreshes and delay-threshold changes retain the map position. A TV spotlight temporarily focuses one service/direction and restores the manual selection afterwards.

The existing bounded route-context cache now prioritises selected and monitored services, rather than potentially omitting their geometry after the first 250 network routes. Existing cached route geometry is reused. If geometry is unavailable, the route badge says so; no road alignment is invented. Route-end dots identify geometry endpoints, not confirmed bus terminus stops.

## Repeating TV spotlight

The spotlight starts automatically in TV mode. After an initial six-second delay, views rotate about every twelve seconds:

- Network overview.
- Eligible operational alerts with service/direction and map focus.
- Other matching activity, using a varied order after the latest detected alert.
- Available nearby traffic-camera context, or another activity when images are unavailable.
- Loading, bunching or headway analytics, then the overview again.

The cycle repeats continuously. Highlighted alerts and KPI cards get a controlled illuminated border. An in-map activity banner explains the service, location and impact. Headway analytics temporarily focus the highlighted service and direction; the original chart scope is restored afterwards. New alert IDs from a refresh enter the upcoming spotlight queue. Existing alert detection timestamps help identify newly detected activity; these are not presented as the provider's incident creation time.

The cycle respects the current service, operator, CBS, delay and horizon filters. It does not change those filters or switch between LIVE and DEMO. Resolved alerts, stale retained history and unmatched services are excluded. When live source observations are older than 150 seconds, the tour shows the delayed-data overview instead of presenting old activity as fresh. DEMO scenarios remain labelled simulated.

**Pause spotlight / Resume spotlight** controls the cycle. A real mouse/touch/keyboard interaction pauses automatic focus changes for thirty seconds. The countdown is shown in the button. Open dialogs also prevent automatic scene changes. Leaving TV mode restores the controller's chart scope and selected-route view.

The spotlight is presentation-only: it performs no acknowledgement, monitoring/resolution transition, dispatch, regulation or approval action. Existing provider caching and normal data refreshes remain in place. Nearby camera views use the existing cached backend and are visual context, not evidence that a route is affected.

## Validation

- **56 Python tests passed**, including forecast/geometry regressions, current alert detection timestamps and selected geometry beyond the context-cache cap.
- **Four Node suites passed**, covering filters, chart rendering, latest-first/randomised eligible activity, stale/resolved exclusions, route-layer order/width/directions, selection restoration, viewport stability, real UI event wiring and spotlight pause controls.
- **Chromium checks passed at 1280×720, 1366×768, 1920×1080, 2560×1440 and 3840×2160.** They cover selected-route visibility, spotlight repetition, preserved filters, automatic resumption after manual interaction, populated cameras, chart modules, TV re-entry, and no page scrolling, overlapping panels or cropped panel bodies. No write requests are issued by the automated cycle.

Browser tests deliberately use explicit synthetic API/image fixtures and the labelled geographic fallback. MapLibre layer construction and selected-route data are also unit tested. These checks do not validate external provider availability, live traffic truth or production model accuracy.

```bash
python -m unittest test_forecast2026_amendments.py test_forecast2026_live.py test_forecast2026_group_metadata.py test_forecast2026.py test_forecast2026_api.py test_forecast2026_geometry.py test_forecast_tiles.py test_forecast_bunching.py
node test_forecast2026_ui.cjs
node test_forecast2026_amendments.cjs
node test_forecast2026_spotlight.cjs
node test_forecast2026_controls.cjs
python test_forecast2026_tv.py
```

Playwright/Chromium are optional development dependencies for the last command. The test runs the existing standalone demo server locally and does not require API credentials.
