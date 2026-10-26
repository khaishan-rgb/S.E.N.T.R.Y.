Forecast map update

Replace forecast.html and forecast.py in the repository root and redeploy/restart the app. app.py is unchanged from the previous road-context update.

Changes:
- Purple bus icons; service details remain inside their popups.
- Traffic icons at incident points or line midpoints, including jams that previously appeared only as lines.
- Separate accident, roadworks, closure, breakdown, traffic and unspecified symbols, stronger contrast, clearer road labels and a compact map key.
- Independent traffic, incident and roadworks controls with counts; secondary layers are under More layers.
- Nearby reports is optional and off by default. It shows point reports near matching routes with unverified symbols. Reports are excluded from delay/risk totals; events with no matching route stay hidden. A nonzero minimum delay excludes unverified reports because no delay can be attributed to them.
- Service/operator/minimum delay filters are preserved.
- TomTom tile parser now reads indexed icon_category_N and description_N fields in both geometry and direction-marker features. It uses the highest known indexed priority, retaining flat-field compatibility and one event per geometry. Unspecified reports remain unspecified.

Reference: https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-incidents/vector-incident-tiles

Validation: 7 no-network map regression checks, 6 tile-parser tests, Python compilation and JavaScript syntax check passed. Live feed/map rendering was not tested; browser launch was blocked by this environment.
