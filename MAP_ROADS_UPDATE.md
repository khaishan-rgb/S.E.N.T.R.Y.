# Map roads update — 27 October 2026

## What was wrong
- DEMO routes were 4–5 hand-typed points per service, so they cut straight across the island.
- The traffic layer coloured the whole route red or green by delay. It did not colour the road where the problem was.
- Two scenarios named roads their service never uses: "Orchard roadworks" was on svc 147 and "AYE accident" was on svc 10.

## What changed
- **Real roads.** Each demo service now uses its actual bus path and its real LTA stops, with codes, names and roads. The data comes from busrouter.sg / sgbusdata (derived from LTA DataMall), which is the same source live mode already uses. It is baked into `forecast2026_demo_routes.json` (107 KB). Every stop lies within 83 m of its path.
- **Road by road.** An incident, roadwork or build-up now colours only the affected road stretch (0.6–1.6 km, following every bend). The rest of the route keeps the route colour. This applies to both the vector map and the fallback map. Stretch lines are thicker and scale with zoom.
- **Places match roads.** Orchard Rd roadworks now use svc **190** and the AYE accident uses svc **97**; both services actually run on those roads. Each demo event sits on a real road the service uses (e.g. 14 Bedok Nth Rd, 166 Thomson Rd, 156 Clementi Ave 2). The TV bottom panels and alerts therefore show real stop names and codes.
- Simulated buses are placed on the real route.
- Live mode is unchanged. It already draws live routes from the route index and colours only the incident/jam geometry.
- If `forecast2026_demo_routes.json` is missing, demo falls back to the old straight corridors, labelled "approximate".

## Upload / replace
`forecast2026.py`, `forecast2026-map.js`, `forecast2026.js`, **new** `forecast2026_demo_routes.json`, **new** `test_forecast2026_demo_roads.py`, **new** `bake_demo_routes.py` (regenerates the JSON when routes change). Hard-refresh once after deploy.

These files sit on top of the TV bottom-row update (see TV_BOARD_UPDATE.md). The full repo zip contains both updates.

## Validation
- The new demo-roads tests check: dense real geometry for all 10 services; each traffic stretch is 0.3–2 km, lies within 5 m of its route and lists real stop codes on the named road; scenario roads match their service; buses lie on their route.
- All other Python/Node suites pass, and the TV browser regression passes at 5 viewports.
- Vector-tile rendering could not be viewed here because basemap tiles are blocked. The same lines were checked on the fallback map.
