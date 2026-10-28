# Stop-by-Stop Running Time — 28 October 2026

A new engine in **Analytics** (sidebar, App Launcher, Ctrl/⌘+K "stop") at **/stop-running-time**.

## What it does
- **Measuring list:** add up to 8 services (`RT_MAX_SERVICES`). Each is followed round the clock in both directions. When a bus completes its route, HORIZON stores the time it passed every stop, interpolated between live LTA position polls. This is the existing recorder (`rt_trip`); history starts when measuring starts, because LTA publishes no historical running times.
- **Report on screen:**
  - **Stop to stop:** for every consecutive pair of stops — km, trips, median, P85, median km/h, plus the median for each time period (15 / 30 / 60 / 120 min). Cells are coloured against that stretch's own median (green faster, amber 10–30 % slower, red 30 %+ slower); grey italics mean too few trips. The table ends with the sum of the stop-to-stop medians.
  - **Time from first stop:** cumulative minutes to every stop, per period.
  - **Headline figures:** trips measured, median route time, number of stop pairs, slowest stretch.
  - **Filters:** service, direction, day type (public holidays count as Sunday/PH), date range, and periods by **trip start time** (TPR convention) or by **time the bus left the stop**.
- **Download Excel** (5 sheets):
  - **Summary**
  - **Stop-to-stop**, with colour scale and SUM row
  - **Cumulative**
  - **Trips:** every trip's passing time at every stop
  - **Segment data:** one row per trip × stop pair, up to 25,000 rows
- **Download CSV:** every stop-to-stop record (trip, date, day type, period, from/to stop, seconds, km), for pivot tables, Power BI or any tool.
- **Preview with demo data:** simulated trips on service 14's real stops, clearly labelled DEMO.

## Important — make the history last
On the current Render setup the page shows a warning, because:
1. **The database is wiped** on every upload, restart and free-plan sleep. Fix: a Render paid instance with a persistent disk mounted at `/var/data`, and the environment variable `BUNCHING_DB=/var/data/bunching.db` (see the comments in render.yaml).
2. **Recording pauses when nobody has HORIZON open.** Fix: environment variable `BUNCHING_ALWAYS_ON=1`.
Without both, stop-by-stop history only covers the times someone had the site open since the last restart.

## Accuracy
- **Measurement:** a passing time is interpolated between polls (every 30–60 s), so one short stretch on one trip can be off by tens of seconds. Medians over many trips are reliable.
- **Dropped values:** impossible values are removed — a single stop-to-stop time over 60 min, or faster than 90 km/h.
- **Dwell:** stop-to-stop time includes the dwell at the first stop.

## Files
New: `stop_report.py`, `stop_rt.html`, `test_stop_report.py`, `STOP_RUNNING_TIME.md`. Changed: `app.py` (page + 4 API routes), `app_shell.js` (new engine + icon), `test_horizon_theme.py`.
API: `/api/stop-rt/services`, `/api/stop-rt/report`, `/api/stop-rt/report.xlsx`, `/api/stop-rt/segments.csv`. All take `service`, `direction`, `day_type`, `date_from`, `date_to`, `period`, `basis` and `demo=1`.
