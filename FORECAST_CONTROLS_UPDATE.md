Replace forecast.html in the repository root and redeploy. Backend files are unchanged from the map upgrade.

- Clicking a Services at risk row sets the service filter, draws its selected-direction route, and scrolls to the map. Other filters remain in effect.
- Clear all filters resets service, operator, minimum delay and the selected route/service details. Layer visibility choices and the Live buses toggle are retained.
- Clear buses removes bus markers and invalidates in-flight bus requests. Markers stay hidden until Live buses is enabled again.
- Service intelligence replaces the oversized chart with average extra running time (arithmetic mean of available 15/30/60 minute forecasts). Individual forecasts remain in expandable details. Average total running time is shown only when a measured usual running time exists. Blocked routes do not show zero as a reliable estimate.
- Forecast comparison and horizon labels show average extra minutes per matching affected service-direction instead of network sums; blocked routes are excluded from averages.

Validation: control/average regression checks, seven map regression checks, six parser tests, and JavaScript syntax check passed. Browser/live-data rendering remains unverified.
