TV alignment and passenger-load update

Replace app.py and forecast.html and redeploy/restart. forecast.py is unchanged from the previous upgrade.

TV: compact single-row header, aligned KPI baselines, no oversized rings, and a full-width service watch table. Duplicate forecast cards are hidden on TV; forecast lead times and average delay stay visible on the map horizon controls. Service rows rotate in pages of four.

Map: dedicated clickable incident and roadwork symbols remain visible in tilted WebGL views. Primary traffic, incident and works layers are enabled on entering TV mode; service/operator/CBS/minimum filters remain in effect. Road closures show Blocked instead of +0 minutes.

Service watch: traffic delay, groups of exactly three buses, and current passenger crowding. SEA=seats available, SDA=standing, LSD=heavy load (limited standing space). Ratios count known samples at the highest reported crowding category, not total fleet coverage. Example Heavy load 2/4 means two of four buses with known load readings report LSD. Heavy-load services appear even with no predicted traffic delay.

The dashboard keeps its requested services watched by the existing bunching collector, subject to its existing service-pair limit. Readings become available after collection; stale or missing observations remain Unavailable. Current crowding is observed data, not a future occupancy forecast. Forecasting future occupancy requires demand history not available here.

Validation: map/filter, control/averaging, TV pagination/loading/typed-marker regressions, five bunching/load endpoint tests and six tile-parser tests passed. Python/JavaScript syntax checks passed. A browser viewport check was prepared but did not complete; real screen and live-feed rendering remain unverified.
