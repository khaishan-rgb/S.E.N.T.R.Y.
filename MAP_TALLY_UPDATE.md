# Cards ⇄ map tally and card filter — 27 October 2026

## What was wrong
- Selecting a card did not filter the map. With "Three-bus bunching" selected, the server still sent every traffic incident touching those services, so incident markers stayed on the map.
- The cards did not tally with the map:
  - **Traffic card** counted only jams above the delay threshold, while the map also drew accidents, roadworks and closures.
  - **Bunching / gap / loading markers** were placed at the service's incident location (or its first bus), not at the stop with the problem. Several stacked on one spot or went missing when no bus position was known.
  - **Heavy loading card** counted buses, but the map drew one marker per service.

## What changed (forecast2026.py, forecast2026-map.js)
- **Selecting a card** shows only that category on the map, in the alerts and on the TV panels: no incidents unless Traffic is selected, and no heavy-load markers unless Heavy loading is selected. Click the card again to show everything.
- **Traffic card** = the incidents, roadworks, closures and jams drawn on the map. "Services" = the services those incidents hit. The delay threshold still filters jams; closures and incidents always show.
- **Bunching and gap markers** sit at the stop where the bunch or gap is (LTA route stop), falling back to the bus position.
- **Heavy loading:** one marker per heavy-load (LSD) bus, at its stop, so the map count equals the card number.

## Upload / replace
`forecast2026.py`, `forecast2026-map.js`, **new** `test_forecast2026_tally.py`. This sits on top of the TV bottom-row and map-roads updates; the changed-files zip contains all three.

## Validation
- `test_forecast2026_tally.py` checks live and demo: with no card selected, every card number equals the markers of that kind; with each card selected, that card still tallies and every other kind has zero markers.
- A browser check clicked each card in live and demo and counted the map markers; all matched (e.g. demo 9/3/2/2, live 5/2/1/1).
- All other Python/Node suites and the 5-viewport TV regression pass.
- On the real vector map the same rules apply. It could not be viewed here (basemap tiles blocked).
