LIVE MAP / LOAD / STOP-SEQUENCE FIX

Upload all nine code files from this ZIP to the existing repository root, replacing their versions, then redeploy/restart. Keep the Render start command uvicorn app:app (plus your usual host/port flags). forecast2026_server.py is the optional standalone demo server, not the live-provider host. Existing LTA/TomTom keys remain unchanged.

Fixed:
- Main app defaults to Live / Now. Demo remains explicit; standalone demo server retains Demo as its default.
- LTA collection wakes even without a traffic incident. Bus positions and current load survive missing headway-model prerequisites.
- Map container is pinned against MapLibre CSS overriding its height; it is resized with the dashboard. Hidden Demo watermark is explicitly removed in Live. Entry assets have cache-busting version tags. Basemap tiles are independent of the LTA feed; an explicit geographic fallback remains available if tiles fail.
- Live passenger panel shows current SEA (seats), SDA (standing), LSD (limited standing/heavy) counts. Zero LSD is a valid zero heavy count, not an API failure. Bar proportions refer to sampled buses, not percentage passenger occupancy. Future load forecasting still needs demand/capacity history.
- Selected headway plot is labelled service + direction + ordered focus pair. Network mean is a separate choice and averages available focus pairs within the filters. No missing values become zero; collector 10/20-minute models can be interpolated to 15, but unsupported longer horizons are not extrapolated.
- Bunching detail includes service, direction, original LTA StopSequence, stop code/name, bus order and headways. Traffic watch shows the road and stops on/bracketing the verified affected stretch. Unavailable/ambiguous sequences remain unavailable.

Restart allows the collector to populate the new route-stop metadata on its next normal cycles. No extra public-API polling stream or new credentials are added.

Validation: 45 Python tests, frontend helper checks and Python/JavaScript syntax checks passed. Live-provider and actual browser/TV rendering remain unverified in this workspace. The optional browser test now explicitly checks map-host dimensions and Live watermark removal.
