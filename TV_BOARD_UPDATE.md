# TV bottom row update — 27 October 2026

The TV layout is unchanged except for the bottom row: header, risk cards, network status, risk outlook, map, operational alerts, cameras and the spotlight tour all stay as before.

In TV mode, the old bottom row (Hourly headways / Passenger loading / Three-bus analysis, Top services to watch, AI operational advice) is replaced by three panels:

| Panel | Each case shows |
|---|---|
| **Heavy loading** | Service · direction · stop where the LSD buses are · heavy / known buses · bus numbers |
| **Incident · traffic** | Road closure / accident / roadworks / traffic build-up · road · delay or BLOCKED · each affected service · direction · stop stretch |
| **Bunching · long gaps** | 3-bus bunching (stop, bus order, minutes apart) and long gaps (stop, gap minutes, scheduled headway, excess) |

The controller (non-TV) view still has the old bottom row.

When a panel has more cases than fit, it pages every 12 s with whole cards only. New cases are tagged NEW for 5 min. A panel with no data source shows "Data unavailable · not a clearance". Observed load shows only at NOW and is never presented as a forecast.

## Upload / replace
`forecast.html`, `forecast2026.css`, `forecast2026.js`, `forecast2026-map.js`, **new** `forecast2026-tv.js`, `forecast2026.py`, `app.py` (adds the asset route for forecast2026-tv.js), `forecast2026_server.py`. Tests: `test_forecast2026_tv.py`, `test_forecast2026_controls.cjs`, `test_forecast2026_api.py`, **new** `test_forecast2026_tvboard.cjs`. Asset version `20261027-tv-board`; hard-refresh once.

## Backend additions (forecast2026.py)
- `services[].load_details`: each LSD bus with its nearest route stop (live, NOW only).
- `events[].impacts`: `{svc, dir, stops, delay}` for each service-direction an incident affects.
- Demo traffic details now carry their kind and delay.

## Validation
All five Node suites pass. Python `test_forecast2026`, geometry, group-metadata and tiles pass. The original TV browser regression passes at 720p, 1366×768, 1080p, 1440p and 4K, with added checks that the three bottom panels are visible, the old panels are hidden and no card is cut off. It ran against synthetic fixtures. The FastAPI-dependent tests (`*_amendments.py`, `*_live.py`, `*_api.py`) could not be run here.
