# SG Transport Pulse V2

A mobile-first public transport operations intelligence prototype using public online data.

## Live/public capabilities
- LTA Bus Arrival v3: ETA, estimated bus location and load
- LTA Traffic Incidents, Traffic Speed Bands, Road Works, Faulty Traffic Lights, Flood Alerts, VMS/EMAS, Estimated Travel Times
- NEA/data.gov.sg rainfall + 2-hour forecast
- OpenStreetMap basemap; backend is ready for a future OneMap routing layer

## Smart-bus framework coverage
Direct/partial public-data coverage: dispatch/command monitoring, mobile bus information/electronic-sign style ETA, historical passenger-volume analysis, remote road monitoring, safety/disruption alerts and inferred traffic-risk intelligence.

Not available from public APIs: payment system, onboard ADAS, 360-degree vehicle vision, station access/perimeter/AR control, bus-lane enforcement camera feeds, passenger facial recognition. Those require operator/hardware/internal-system integrations.

## Render
Set `LTA_ACCOUNT_KEY` as a Render environment variable. Never commit API keys.

Build: `pip install -r requirements.txt`
Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
