# Five fixes — 27 October 2026 (night)

1. **Map icons jumping around.** Icons were re-spread by screen position after every pan and zoom, so they moved each time the map moved. Now only icons sharing the same spot (within ~15 m) fan out, in a fixed pattern, and they stay fixed while you pan, zoom or refresh. (`forecast2026-map.js`)

2. **Slow loading.** (`app.py`, `forecast2026.py`, `forecast2026.js`, `forecast2026-core.js`)
   - A request no longer waits for a full new collection cycle (LTA, TomTom, Waze, rain). It answers at once from the last completed evaluation and refreshes in the background when due.
   - Up to 250 background route lines were re-simplified on the main server thread on every request. They are now simplified once and cached, using a method that keeps every bend with far fewer points.
   - The browser keeps the background route set and the server resends it only when it changes.
   - The response is gzip-compressed.
   - The second filtered build runs off the main thread.

3. **Card toggle.** Card selection is now applied in the browser to the full data, so pressing a card filters instantly and pressing it again un-filters instantly, with no server wait. The browser filter is tested to match the server filter exactly.

4. **"Road name not supplied" / "Bus stop / sequence unavailable".** When the provider gives no road or no affected stretch, the road and stop now come from the service's own LTA route stops: the stretch stops, or else the nearest stop on that route within 400 m. Incidents further than 400 m from the route are not attached to a stop. (`forecast2026.py`)

5. **Map has data but panels below are empty.** The three TV panels were leaking into the normal (non-TV) view, where they are never filled. They now appear only in TV mode; the normal view keeps its original bottom row. (`forecast2026.css`)

## Upload / replace
`app.py`, `forecast2026.py`, `forecast2026.js`, `forecast2026-core.js`, `forecast2026-map.js`, `forecast2026.css`, plus tests `test_forecast2026_tally.py` (extended) and new `test_forecast2026_markers.cjs`. The changed-files zip contains every file changed today. Hard-refresh once.

## Validation
- All Python and Node suites pass, including new tests for browser/server filter parity (live + demo, all four cards), road-name fill, route simplification and icon stability.
- The 5-viewport TV regression passes.
- In the browser, two card presses toggled filtered/unfiltered in about 0.2 s with no server request.
- `app.py` could not be run here (FastAPI unavailable); its changes were syntax-checked and reuse the existing payload builder.

## Follow-up — map icons still jumping (28 Oct, 00:20)
**Root cause:** on every refresh the map code overwrote each icon's CSS classes (`el.className = 'event-pin …'`). That removed the `maplibregl-marker` class the map engine adds to pin an icon to its coordinates. The icons then fell back to normal page layout, stacked one under another, and "flew" around the map as it moved or refreshed.

**Fix:** the code now only adds or removes its own classes (`event-pin`, `estimated`, `spotlight-pin`) and leaves MapLibre's classes alone.

**Verified with the real MapLibre 4.7.1 engine** at desktop size and at phone size (412×900, 2.6× pixel ratio), measuring every icon against its true map location:
- before the fix, icons were 568–832 px off;
- after the fix, they are 0 px off after zoom, after three refresh-and-pan cycles, and in 3D tilt.

`test_forecast2026_markers.cjs` now fails if the class overwrite ever returns.

## Old version still showing after upload
Every update used the same asset version (`20261027-tv-board`), so browsers and caches kept the old CSS/JS. The version is now `20261028-fix2`, and pages are served with `Cache-Control: no-cache`, so each redeploy is picked up on the next page load.

Make sure the deploy includes **every** file in the changed-files zip, especially `forecast.html`, `forecast2026.css`, `forecast2026.js`, `forecast2026-map.js`, `forecast2026-core.js`, `forecast2026-tv.js`, `forecast2026.py`, `forecast2026_demo_routes.json` and `app.py`. Check the deploy log shows the new commit.

**How to confirm the new version is live:** on the dashboard, the normal (non-TV) view must not show the empty "Heavy loading / Incident · traffic / Bunching · long gaps" panels. If they still appear, the browser is on an old copy or the files weren't all uploaded.
