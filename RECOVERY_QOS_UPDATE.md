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
