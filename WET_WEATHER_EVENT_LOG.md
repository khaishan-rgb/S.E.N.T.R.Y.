# Wet weather impact and event log - 12 Nov 2026

A new page, **/weather-events** (menu: ANALYTICS > Wet Weather & Event Log), and a stored history of operational events.

## What it shows

**Raining now.** Every rain area on a bus route right now (NEA rain gauges, moderate rain or heavier, the same rain areas the
traffic engine already detects), the services and directions inside each one, and how many minutes rain usually adds to each
of them. Refreshes every minute.

**What rain adds.** For each measured service and direction: the extra running time on wet trips, also for heavy rain.
- A trip is **wet** when 0.2 mm or more of rain fell near its route during the trip (the same rule as Running Time Analytics),
  **heavy** at 2 mm or more.
- Each wet trip is compared with **dry trips of the same service, direction and day type that started within an hour of it**.
  The figure is the median of those differences, so it is not thrown off by one bad trip.
- Running times are **measured** from live bus positions (Running Time Analytics), not taken from the timetable. Only the
  services measured there have a figure (up to RT_MAX_SERVICES, default 8, round the clock).
- A figure needs at least 3 wet trips. Under 8 wet trips it is marked "few trips". A service in rain with no measured trips
  shows "no measured trips"; nothing is guessed.
- Choose the last 30, 60 or 120 days.

**Event history.** Bus bunching events, long headways, traffic alerts (congestion, incidents, road works) and rain areas that
touched a bus route, with when they started and ended, the services and directions, where, and their peak.

What is stored (so short, repeating alerts do not fill the log):
- **Traffic, road works and wet weather:** only those that lasted **15 minutes or more** (`EVENTLOG_MIN_MINUTES`). One that is
  still going is stored when it reaches 15 minutes; one that clears sooner is never stored.
- **Bus bunching and long headway:** only those that ran over **10 bus stops or more** (`EVENTLOG_MIN_STOPS`).
- Raising a limit later also removes stored rows below it.

Peaks:
- Bus bunching (2BB / 3BB...): closest headway. From the bunching collector, logged once it lasted the configured stops.
- Long headway: largest headway and how many times the scheduled headway.
- Traffic (congestion, incidents, road works): estimated extra minutes through the congested stretch.
- Wet weather: the most rain at a gauge (mm).

Filter by type, dates, service and place; **Export to Excel** gives the filtered events plus the wet-weather table.

## HORIZON AI

On the AI Operations Centre, HORIZON AI now also gets:
- the rain areas on bus routes now, and the minutes rain usually adds to each service in them;
- for a service you name: whether it is in rain now, the minutes rain adds to it (heavy rain too) and how many trips that is
  based on, plus its stored history in the time window (bunching, long headway, traffic, wet weather).

So "Is it raining on 147, and how much will it slow down?" or "What happened to 65 yesterday?" are answered from data.

## Storage (important)

Everything is in the SQLite file `BUNCHING_DB`. `render.yaml` now sets:

| Setting | Value | Why |
|---|---|---|
| plan | starter (about US$7/month) | free instances lose the database on every upload, restart and 15-min sleep, and cannot have a disk |
| disk | 1 GB at /var/data (US$0.25/month) | the history lives here |
| BUNCHING_DB | /var/data/bunching.db | the database file on that disk |
| BUNCHING_ALWAYS_ON | 1 | keeps bunching and long-headway collection going with nobody watching |
| EVENTLOG_KEEP_DAYS | optional, default 365 | how long the event log is kept |
| EVENTLOG_MIN_MINUTES | optional, default 15 | traffic, road works and wet weather shorter than this are not stored |
| EVENTLOG_MIN_STOPS | optional, default 10 | bus bunching and long headway over fewer stops are not stored |

If your Render service was **not** created from this Blueprint, make the same changes by hand in the Render dashboard:
Settings > Instance type: Starter; Disks > Add disk: mount path `/var/data`, 1 GB; Environment: `BUNCHING_DB=/var/data/bunching.db`
and `BUNCHING_ALWAYS_ON=1`. The page shows **Saved on disk** once it works, **Temporary storage** before.

Traffic and rain events are logged round the clock (the traffic engine always runs). Measured trips (for the minutes) are kept
RT_KEEP_DAYS (default 180).

## Files

| File | New / changed | What |
|---|---|---|
| `opslog.py` | new | Wet-weather impact maths, event-log rows, Excel export |
| `app.py` | changed | `ops_event` table; filled after every traffic cycle (traffic, rain) and from bunching / long-headway events; `GET /api/weather/impact`, `GET /api/events/log`, `GET /api/events/export.xlsx`, `/weather-events`; HORIZON AI gets the new data; page id `wxlog` can be hidden in Settings > Pages |
| `weather_events.html` | new | The page |
| `app_shell.js` | changed | Menu entry with a rain icon |
| `render.yaml` | changed | Paid plan, disk, BUNCHING_DB, BUNCHING_ALWAYS_ON |
| `test_opslog.py` | new | Offline tests (wet/dry matching, too few trips, event rows, Excel) |
