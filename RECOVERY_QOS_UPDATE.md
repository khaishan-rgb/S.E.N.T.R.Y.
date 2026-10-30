# Recovery Decision Engine: consistent scenarios + QoS 1.2 (30 October 2026)

## What was wrong
The trip-by-trip squares (headway view) showed **Trip adjustment worse than No action** (final avg 18.3 against 15.1). Three causes:

1. **"No action" was not really no action.** On every later trip (DOWN 1, UP 2, DOWN 2) the engine still evened out the departures at the terminals, in *both* scenarios.
2. **"Trip adjustment" could not adjust the later trips.** Each bus captain was allowed 8 min of holding over the *whole* horizon. The first departure (UP 1) used it up, so the later trips ran with no adjustment and their gaps grew.
3. **"Final avg" averaged the headway values themselves.** A short gap (bunching, e.g. 2.8 min) pulled the average down towards 12, so heavy bunching made No action *look* closer to timetable.

## What it does now (`recovery.py` V13.10, model `recovery-2.1-ewt-qos`)
* **No action:** no trip is adjusted, neither the next departure nor any later one. Every bus leaves at the later of its schedule and arrival + 7 min.
* **Trip adjustment (and Halfway):** the later trips are adjusted too, at every terminal, with the same per-trip limits as the first departure:
  * hold ≤ 8 min, early ≤ 3 min;
  * never under the 7-min layover;
  * departed trips locked.

  The 8-min *total* per bus captain is now off by default (setting "BC welfare: total hold per BC", 0 = off). Any value you set is still respected.
* **QoS 1.2 (new hard rule):** an adjusted departure headway may be at most **6 min longer than scheduled** (scheduled 12 → at most 18).
  * It is checked at the interchange and at every later terminal.
  * The engine holds the bus in front of a long gap, or releases the bus behind it, within the limits above.
  * If no allowed plan can meet it (e.g. the bus in front has already departed), the result says so, naming the gap and the two trips.
  * Setting: "QoS 1.2: max headway over schedule" (default 6).
* **Decision:** Trip adjustment is used when it lowers the Average EWT by ≥ 0.05 min, **or when No action breaks QoS 1.2 and the adjustment meets it** (or reduces the breach). Halfway also has to pass "QoS 1.2 not worse".

## Page (`halfway.html`, Recovery Decision Engine)
* **Headway view:** each square still shows the headway most off schedule. The trip and final averages are now **minutes off schedule (±)**, so bunching counts as badly as a gap: lower = closer to timetable.
* **QoS 1.2 badge** on each option: "✓ met" or "✗ n over, worst +x".
* **Hard rules box and formula text:** these now say No action adjusts no trip, and show QoS 1.2.

## Example (12-min headway, trip 6 arrives 16 min late, 4 balance trips)

| | Average EWT | Avg minutes off schedule | QoS 1.2 |
|---|---|---|---|
| No action (now truly none) | 1.03 min | 12.0 | ✗ 24-min gap (+12) |
| Trip adjustment | 0.19 min | 4.3 | ✓ met |
| Halfway | 0.15 min | 3.1 | ✓ met (not recommended: saves only 0.04 min EWT, rule needs 0.10) |

Before this fix, the same case gave Trip adjustment 0.33 against No action 0.62, with the later trips left unadjusted.

## Files
`recovery.py`, `halfway.py` (defaults), `app.py` (passes the QoS setting, one-time removal of the old stored 8-min total), `halfway.html`, new `test_recovery_qos.py` (8 tests).

---

# Update 2 (30 Oct, 16:00): each option behaves the way the OCC reads it (model `recovery-2.2-pattern`)

**The question (service 147):** why do headways of 0 and 4.4 min appear? Can each trip show **average · max** headway for the 30 min before and after the long headway?

**Why 0 min:** trip 6's bus arrived 15 min late (17:05). With the 7-min rest it is ready at 17:12, exactly when trip 7 is due. Under No action both leave together and run as a pair (0-min headway) behind a 24-min gap. This happens on the road too.

**Why 4.4 min (before this update):** the adjustment sent trip 7 (ready 17:09) ahead of the late trip 6. Trip 6 followed 6 min behind and, carrying fewer passengers, closed up to 4.4 min.

**Now:**

| Option | Later trips | What you see (case above) |
|---|---|---|
| No action | nothing adjusted | every trip: avg 12, max 25.6 → 24.1 → 22.4 → 20.4. The gap carries on and shrinks only by the 3 min of layover above the 7-min minimum. Shortest 0 (the pair). |
| Trip adjustment | every bus keeps its adjusted first-trip slot on every later trip (hold ≤ 8, early ≤ 3 per departure, ≥ 7-min rest, QoS 1.2) | **the same on every trip:** avg 12, max 15.4, shortest 7 |
| Halfway | no other trip adjusted | 1st trip: avg 12.5, max 24.2 (the stops before the halfway start). 2nd trip onwards: scheduled 12 / 12 |

* **Headway view:**
  * Each square = the **longest headway** at that AITP, from 30 min before the long headway to 30 min after it.
  * A white dot = two buses within 3 min of each other.
  * Each trip ends with **avg · max**, and "All trips" gives the overall avg · max and the shortest headway.
  * Tap a square to see every bus's headway and the window figures.
* **Settings** (Recovery settings):
  * "Trip adjustment keeps its spacing on later trips" (1 = new, 0 = former even spacing);
  * "Halfway also adjusts the trips around it" (0 = new, 1 = former);
  * "Average / max headway window" (30 min).
* **Tests:** `test_recovery_qos.py` now has 13 tests, including this service 147 case.
