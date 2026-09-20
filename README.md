# SG Transport Pulse V3.2 — Traffic Fixed

Traffic fix:
- Uses the official `v3/TrafficSpeedBands` endpoint.
- Traffic is fetched live on every corridor request (not cached as static data).
- Paginates the full live feed.
- Corridor matching widened to 800 m to account for approximate bus-route geometry.
- Adds `/api/traffic-test` diagnostic endpoint.
- UI now shows both raw LTA segment count and matched corridor segment count, so a zero result cannot silently masquerade as working traffic.
- LTA bands 1–2 = red, 3–4 = yellow, 5–8 = green.

Keep `LTA_ACCOUNT_KEY` only in Render environment variables.
