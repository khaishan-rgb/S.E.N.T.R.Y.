# TV operations board — 27 October 2026

TV mode is now a three-column board. It answers three questions and nothing else:

| Column | Shows for each case |
|---|---|
| **Heavy loading** | Service · direction · bus stop where the heavy-load (LSD) buses are · heavy buses / buses with known readings · bus numbers |
| **Incidents · traffic** | One card per road closure, accident, roadwork or traffic build-up: road, delay (or BLOCKED), and every affected service · direction · stop stretch |
| **Bunching · long gaps** | Three-bus bunching (stop, bus order, minutes apart) and long headway gaps (stop, gap minutes, scheduled headway, excess) |

The map, risk cards, alerts, cameras, headway chart, "Top services to watch" and "AI operational advice" are no longer shown in TV mode. They still appear in the normal (controller) view. Category and service filters are hidden in TV mode and TV always shows the full network.

## Behaviour
- Ordering: road closures first, then incidents, roadworks and build-ups, sorted by delay. Bunching cases come before gaps. Loading is sorted by the number of heavy buses.
- When a column has more cases than fit, it pages every 12 s, and only whole cards are shown. The footer shows "Page n/N".
- A case that appears after the board has loaded is tagged **NEW** for 5 minutes.
- If a column has no data source, it reads "Data unavailable · not a clearance". It never reads "None". Observed passenger load appears only at NOW; at future horizons it reads unavailable, so it is not presented as a forecast.

## Upload / replace
`forecast.html`, `forecast2026.css`, `forecast2026.js`, `forecast2026-map.js`, **new** `forecast2026-tv.js`, `forecast2026.py`, `app.py` (one added asset route), `forecast2026_server.py`, plus tests `test_forecast2026_tv.py`, `test_forecast2026_controls.cjs`, `test_forecast2026_api.py`, **new** `test_forecast2026_tvboard.cjs`.
Asset version is `20261027-tv-board`. Hard-refresh once after deploying.

## Backend additions (forecast2026.py)
- `services[].load_details`: each LSD bus with its nearest route stop (live, NOW only; demo simulated).
- `events[].impacts`: `{svc, dir, stops, delay}` for each service-direction an incident affects. The service and category filters apply to it.
- Demo traffic details now carry their real kind and delay, so they survive the delay threshold.

## Validation
All 5 Node suites pass, including the new board unit test. The Python suites `test_forecast2026`, geometry, group-metadata and tiles pass. A real-Chromium TV regression at 1280×720, 1366×768, 1920×1080, 2560×1440 and 3840×2160 checked for: no page scroll, no column overlap, no cut-off cards, a closure that lists service/direction/stop, no observed load at forecast horizons, read-only behaviour, and exit/re-entry. The browser test ran against synthetic fixtures with a stdlib static server (`SENTRY_TV_TEST_EXTERNAL=1`). Live provider rendering is unverified. The FastAPI-dependent tests (`*_amendments.py`, `*_live.py`, `*_api.py`) were not run in this environment.
