# TV fullscreen layout fix — 27 October 2026

This update applies to the existing amended S.E.N.T.R.Y dashboard. It fixes the vertical page scrolling and bottom-right panel overlap reported in TV mode.

## Upload these five files to GitHub

Replace the matching files in the existing repository:

1. `forecast.html`
2. `forecast2026.css`
3. `forecast2026.js`
4. `forecast2026-core.js`
5. `forecast2026-map.js`

All five files are included at the root of `SENTRY-TV-fullscreen-fix.zip`. Restart/redeploy the application, hard refresh the dashboard once, then select **TV mode**. The updated asset version is `20261027-tv-fit`. Keep the existing backend, API keys and database files.

## What changed

- TV mode occupies the available browser/fullscreen viewport at every screen height. Fixed desktop row minimums no longer push content below the screen.
- Map, alerts, cameras and lower analytics use bounded grid tracks with separate panel boundaries. The main page and TV panels require no vertical scrolling.
- Bottom analytics columns can shrink within their actual available width; long table cells and recommendation text cannot widen or overlap adjacent panels.
- Long alert, service-watch and recommendation queues rotate within their panels. Page size is selected from available height and checked against rendered content. Text is not scaled using CSS zoom.
- Cameras show two contained image slots and rotate through the four relevant results. Camera images and captions remain inside their allocated panel height.
- Network status uses shorter labels in TV mode. Controller-only chart controls are hidden; the selected analytics module stays visible. Desktop controls return when leaving TV mode.
- The headway chart measures its available plotting area, keeping labels readable while the screen height changes.
- Fullscreen changes and window resizing recalculate panel content and map dimensions automatically.
- Below 850px height, the brand strapline and secondary weather text are omitted to keep the title and clock fully inside the header. Weather/source details remain in controller mode.

## Validation

Real Chromium checks cover 1280×720, 1366×768, 1920×1080, 2560×1440 and 3840×2160. They assert:

- No page scrollbars or panels outside the viewport.
- No overlapping sibling panels, including the bottom-right analytics.
- No cropped content in status, outlook, alerts, headway, watch, recommendation and camera bodies.
- Containment of populated camera images and a long traffic alert.
- Consistent layout across horizon changes and after leaving/re-entering TV mode.
- Containment when selecting loading, bunching or headway analytics.

Browser tests use explicitly synthetic API/image fixtures and do not verify real provider freshness or camera availability. No live-data adapter behavior changes in this fix.

The three existing Node UI suites also pass. The included browser regression can be run with:

```bash
pip install playwright
playwright install chromium
python test_forecast2026_tv.py
```

It uses the existing `forecast2026_server.py` test/demo server. Set `CHROMIUM_PATH` if needed for a particular installed Chromium executable.
