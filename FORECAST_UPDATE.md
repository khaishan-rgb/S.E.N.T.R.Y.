Forecast filter update

Upload app.py, forecast.py and forecast.html, replacing existing files.
Map events, popup service buttons, probes, selected route, alerts, risk rows and forecast cards now respect service/operator/minimum delay. Events without verified affected services are hidden. Broad traffic-flow tiles are hidden while filters are active. Weather and executive totals remain network-wide, as labelled.
Minimum delay is the largest predicted extra running time at 15, 30 or 60 minutes, not timetable lateness.
Validation: Python compilation, JavaScript syntax, and map data-source tests for operator/service intersection, minimum delay, unaffected/unverified removal, probe filtering and background flow suppression passed. Full browser/live-data validation has not been run.
