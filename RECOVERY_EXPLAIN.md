# Recovery Decision Engine: management view (8 November 2026)

**What it is:** a one-page "Halfway or continue?" for management. It lives at `/recovery/explain`, and the Recovery Decision Engine page has a new **"Explain to management ↗"** button under the recommendation.

**What it shows:**
* Each option with **one headway that holds for every balance trip**, so the numbers stay put:
  * **No adjustment:** the long gap stays on every trip.
  * **Adjustment:** one adjusted headway all the way.
  * **Halfway + adjustment:** a gap before the halfway stop on trip 1, then the timetable.
* **Extra wait per passenger (EWT)** in the hour around the disruption (30 min before to 30 min after), for each trip and as the average of all trips. This is shown as a bar chart with the best option marked, plus "extra minutes per 100 passengers".
* **One picture per trip:** a triangle is one gap. Its height is how long someone arriving then waits, so its area is the total waiting. The normal timetable is shown dotted.
* **"Why one long gap costs so much":** a gap twice as long holds 4 × the waiting.
* **The arithmetic:** average wait = Σ gap² ÷ (2 × Σ gap), normal wait = headway ÷ 2, extra wait = the difference.
* **QoS 1.2 check** for each option (longest gap ≤ scheduled + 6).
* **"Try other numbers":** inputs for the headways, balance trips and halfway stop position.

**Example (opens by default):** headway 15 min, 3 balance trips, halfway stop at 50 % of the route.

| Option | Headways | Extra wait per trip | Average |
|---|---|---|---|
| No adjustment | 15, **30**, 15 on every trip | 3.75 · 3.75 · 3.75 | **3.75 min** |
| Adjustment | 18 all the way | 1.50 · 1.50 · 1.50 | **1.50 min** |
| Halfway + adjustment | 20 before the halfway stop, then 15 all the way | trip 1: 2.50 before / 0 after = 1.25 · 0 · 0 | **0.42 min** |

**From the engine:** when opened from the button, the page shows that run instead. It uses the planned departure gaps of each option on each balance trip, and for Halfway, the gaps at the halfway stop. It names the service, the late trip and the engine's recommendation. The case is handed over through the browser's own storage (`rde.explain`), so no server change is needed beyond the page route.

**Files:**
* `recovery_explain.html` (new; it is built from the same source as the shared claude.ai page);
* `halfway.html` (button and hand-over; "Halfway + regulation" is now called "Halfway + adjustment");
* `app.py` (route `/recovery/explain`).
