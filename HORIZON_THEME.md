# HORIZON theme — 28 October 2026

Every engine (Forecast Centre, OCC Live, Route Traffic, Headway Control, Bunching & Gap, Recovery Decision Engine, Halfway Planner, Diversion Maps, Traffic-Aware Regulation, Running Time, Cameras, Settings, Login and both TV walls) now shares one look, taken from the SBS Transit HORIZON logo.

## What changed
- **One palette, one place.** `horizon-tokens.css` holds every colour, font and logo reference. `design_system.css` takes all of its tokens from it, and that file already re-skins every page, so changing a token changes the whole platform.
- **Look:** deep-space navy with a perspective grid "horizon floor" and a faint star field.
  - **Panels:** frosted HUD glass with corner brackets.
  - **Lines and type:** cyan hairlines and a lit top edge. Orbitron is used for titles and big numbers, Rajdhani for labels, IBM Plex Sans for reading text.
  - **Motion:** the logo's lens flare travels along the command bar, and a light beam runs down the sidebar.
  - **Status colours** keep their meaning everywhere (green ok, amber attention, red intervention, purple AI).
- **Brand:**
  - **Sidebar and command bar:** SBS Transit + HORIZON lockup in the sidebar (the ring mark when collapsed); "HORIZON" in the breadcrumb and mobile bar.
  - **Login:** full lockup.
  - **TV walls:** logo in the header.
  - **Old names:** "SG Transport Pulse" and "S.E.N.T.R.Y" are removed from all 18 page files.
- **Launch any engine:**
  - **Ctrl/⌘ + K** (or the **Launch engine** button) opens a search box. Type part of a name (e.g. "bunch", "diversion", "tv") and press Enter. Ctrl+Enter opens it in a new tab.
  - The App Launcher grid and sidebar also list every engine.
  - TV walls are included as launch targets.
- **Forecast Centre joined the platform.** It now has the shared sidebar, command bar, launcher and mobile navigation. In TV mode all navigation hides and the header shows SBS Transit + HORIZON.
- **Start-up sequence:** the HORIZON lockup and a light line show once per browser session (about 1.5 s; click to skip). It never plays on TV walls or with Reduce motion.
- **Reduce motion:** Settings › Accessibility or the device setting turns off all motion (floor, flare, beam and boot).

## Reusing the theme in another engine / system
1. Load the tokens: `<link rel="stylesheet" href="https://<your-horizon-host>/horizon-tokens.css">`. CORS is open, so another app on a different host can load it.
2. Use the variables, e.g. `background: var(--hz-void); color: var(--hz-ice); border-color: var(--hz-edge); font-family: var(--hz-font-display)`.
3. For non-CSS tools (Power BI, Grafana, Figma, Python plots), read the same values from `/horizon-tokens.json`.
4. Logos: `/brand/horizon-lockup.webp`, `/brand/horizon-wordmark.webp`, `/brand/horizon-word.webp`, `/brand/sbs-transit.webp` and `/brand/horizon-mark.svg` (icon / favicon). All have transparent backgrounds.
5. A new page inside this app needs two lines to get the full theme, sidebar and launcher:
   `<link rel="stylesheet" href="/design-system.css">` and `<script src="/app-shell.js" data-module="<id>"></script>`. Add the engine to `MODULES` in `app_shell.js` and it appears in the sidebar, launcher, quick launch and mobile menus.

## Files
New: `horizon-tokens.css`, `horizon-tokens.json`, `brand/` (5 files), `test_horizon_theme.py`, `HORIZON_THEME.md`.
Changed: `design_system.css`, `app_shell.js`, `app.py` (brand/token/favicon routes, no-cache theme), `forecast.html`, `forecast2026.css`, `forecast2026*.js` (version tag), `login.html`, `diversion_tv.html`, the other page files (product name), `test_forecast2026_tv.py`.
Asset version `20261028-hz1`. The theme files are served with no-cache, so a redeploy shows on the next page load.

## Validation
- All Python and Node suites pass, including the new theme guard tests (tokens CSS = JSON, brand assets exist, every engine page loads the theme and shell, old name gone, quick launch wired, brand route rejects path tricks).
- The TV regression passes at 720p, 1366×768, 1080p, 1440p and 4K. The theme had wrapped the TV control strip to two lines at 1280 px; this was fixed.
- Every engine page was screenshotted at desktop size, plus four at phone size, with the real fonts loaded.
- Live data feeds, basemap tiles and Google Fonts were not reachable in the test environment; in production the fonts load from Google Fonts.
