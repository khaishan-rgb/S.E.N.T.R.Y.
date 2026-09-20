# SG Transport Pulse V4 — Route = Traffic

## What changed
- The bus route itself is now the traffic display.
- Green = LTA bands 5–8 (40+ km/h)
- Yellow = LTA bands 3–4 (20–39 km/h)
- Red = LTA bands 1–2 (<20 km/h)
- Cyan = no traffic-band match; never invents traffic.
- Direction 1/2 remains isolated.
- Every bus stop is tappable; tap **Monitor this stop** to query Bus Arrival and show approaching bus positions.
- Traffic endpoint auto-detection tries the currently documented `v3/TrafficSpeedBands`, then legacy `TrafficSpeedBandsv2` and `TrafficSpeedBands` for compatibility.
- `/api/traffic-test` reports the actual endpoint and segment count.

Keep `LTA_ACCOUNT_KEY` in Render only.
