"""AI Recovery Scenario Optimiser (V12.7) - trip adjustment vs halfway deployment, decided on the whole trip chain.

What it does (the controller's thinking, made explicit):
    current situation -> generate feasible actions -> simulate the chain over the selected BALANCE TRIPS -> stress test under uncertainty
    -> compare Continue full trip (no action / + adjustment) against Halfway + necessary adjustment ON EWT -> recommend + explain.

V13.9 - EWT-based decision over a user-selected Balance Trips horizon
  * Balance Trips (N, `balance_trips`, default 6) replaces the fixed "3 UP + 3 DOWN" chain: every bus runs N subsequent trips
    (UP 1, DOWN 1, UP 2, ... alternating) and every one of them is assessed. N = 6 reproduces the old horizon; any N from 1 to 16 works.
  * Evaluation points = the timing points of every balance trip (the UP stops, including every halfway candidate, and 7 points along each DOWN trip).
  * LEVEL 1 - EWT at ONE evaluation point, from the consecutive headways of the simulated buses passing that point on that balance trip:
        AWT = sum(h_actual^2) / (2 sum(h_actual)),  SWT = sum(h_sched^2) / (2 sum(h_sched)),  EWT = AWT - SWT.
    Scheduled headways are taken individually from the scheduled passing times (ctx["sched_dep"] when a timetable is given); only when every
    scheduled headway equals H does SWT reduce to H / 2 - the result says which basis was used. Headways of different points are never pooled.
  * LEVEL 2 - route / scenario figure: Average EWT = sum(EWT_point) / number of valid points (simple mean), plus the maximum and the worst point.
  * Decision: Halfway is recommended only when its Average EWT beats the Continue scenario by at least `ewt_gain_min` min AND `ewt_gain_pct` %,
    the stress test does not reverse that (P85 of the average EWT), and - under mileage priority - the gain is worth the km lost
    (`ewt_gain_per_km`). Delay is only an INPUT to the simulation; no delay threshold decides.

Model
  * Trips 1..n leave the interchange (first stop) on the scheduled headway H. Each trip's bus then runs its own chain:
      UP 1 -> (far terminal, layover) -> DOWN 1 -> (interchange, layover) -> UP 2 -> DOWN 2 -> ... for N balance trips.
    At every terminal a bus leaves at max(scheduled departure, arrival + minimum layover). So lateness that a full trip keeps is carried into the
    BC's later trips (only the layover slack above the minimum absorbs it) - this is the BC finishing-time impact.
  * The AI only intervenes at the next departure point (UP 1 at the interchange): hold / release full trips (artificial adjustment capped at +/-8 min
    against the bus's natural departure, never below arrival + 7 min layover, departed trips locked) and / or start ONE late trip halfway at a stop
    (the bus runs off-service to it; the trip's first section is lost mileage) and then continues its chain on time.
  * Buses may leave the interchange in a different order from the timetable (a bus that is ready runs ahead of a very late one); along the route no
    bus overtakes another; a bus with a longer gap ahead picks up more passengers and runs slower (load sensitivity).
  * The trips just outside the simulated block (0 and n+1) run on schedule and bound the headways.
  * stress test: every short-listed plan is re-run under `sims` sampled futures (traffic per leg, bus-to-bus running time, dwell / load
    sensitivity, a random incident, uncertainty of predicted arrivals, off-service running time) giving P50 / P85 / P90 distributions.

V13.10 (30 Oct 2026) - consistent scenarios + QoS 1.2
  * NO ACTION really is no action: no trip is adjusted, neither UP 1 nor any later trip (every bus leaves at max(schedule, arrival + 7)).
  * TRIP ADJUSTMENT adjusts the later trips too: every terminal departure may be held / released within the same per-trip limits as UP 1
    (+/-8 min, never under the 7-min layover). The old 8-min TOTAL per BC over the whole horizon is off by default (term_total_max = 0),
    because UP 1 used it up and the later trips could then not be adjusted at all.
  * QoS 1.2: an adjusted departure headway may be at most `qos_over_max` (6) min longer than its scheduled headway. Plans that break it
    are only kept when nothing within the limits can meet it, and every plan reports its QoS 1.2 result.

V13.11 (30 Oct 2026) - each option behaves the way the OCC reads it
  * TRIP ADJUSTMENT (V13.13, 9 Nov 2026) first evens out the UP 1 departures (least squares: every gap as close to scheduled as the limits allow,
    up to `even_side` trips either side of the late trip) when that is at least as good for passengers, then
  * TRIP ADJUSTMENT keeps its adjusted pattern on every later trip: each bus leaves at its UP 1 slot + the scheduled cycle (term_mode
    "pattern"), when ready, within hold <= 8 / early <= 3 min per departure - so the spacing (average / max headway) is the same on every trip.
    term_mode "even" = the former even-spacing regulation at each terminal.
  * HALFWAY starts the late trip at the halfway stop and leaves every other trip alone (halfway_regulate = 0): the first trip shows the gap
    before the halfway stop, and from the second trip the buses run at the scheduled headway. halfway_regulate = 1 = former "halfway + regulation".
  * Window figures: at every AITP, the headways from 30 min (win_min) before the longest headway to 30 min after it -> average and max.
  * V13.12: HALFWAY + ADJUSTMENT again, but only what the bus captains can win back in their layover (scheduled layover - minimum
    layover, 10 - 7 = 3 min): the trips around the lost trip are held / released so the departure gap meets QoS 1.2 (the lost trip's
    gap counts against the normal scheduled headway: 24 -> at most 18), and from the second trip the timetable runs again. Only when
    QoS 1.2 cannot be met within that slack may they move up to +/-8 min.

V13.14 (9 Nov 2026) - one steady headway, easy to explain
  * TRIP ADJUSTMENT (adjust_style "steady"): every trip from trip 2 to the last trip leaves the SAME whole-minute headway h apart, in the
    order the buses are ready. h = the smallest whole minute that lets every bus keep its place (no bus before arrival + 7 min, QoS 1.2:
    h <= scheduled + qos_over_max). The buses ahead of the late bus are held, the ones behind it too, and the next trip after the block
    keeps h behind the last one. Every later trip keeps the same h (each bus = its UP 1 time + the scheduled cycle, or when it is ready).
    Holds can pass +/-adj_max: that is the price of one number (steady_hold_max caps it; 0 = no cap). "least_wait" = V13.13.
  * HALFWAY: as V13.12 - the first trip may hold / release the trips around the lost one, from the second trip the timetable runs again.

V13.16 (10 Nov 2026) - EVEN HEADWAY (the steady headway with a +/-1 min band)
  * adjust_style "steady" is shown as "Even headway": every trip from trip 2 leaves h min after the one before it, give or take steady_tol
    (1) min - h = 15 means each gap is 14, 15 or 16. h = the smallest whole minute for which that band lets every bus keep its place
    (no departure before arrival + 7 min): h >= (earliest time of trip j - trip 1's time) / j - steady_tol, for every trip j.
  * Inside the band each bus leaves as EARLY as it can (least holding): x_k = max(x_(k-1) + h - tol, the time it needs so the buses
    after it can still keep up within h + tol). The next trip after the block keeps at least h - tol behind the last one.

V13.17 (10 Nov 2026) - EVEN HEADWAY over 2 duties above and 2 below (steady_side)
  * Only the late trip and steady_side (2) duties above and below it move; the trip before and the trip after that window keep the
    timetable. Inside the window the gaps are even on each side of the late trip (least squares, rounded to whole minutes, so they
    differ by at most 1 min): e.g. Trip 2 -> Trip 5 = 14, 15, 14 and Trip 5 -> Trip 8 = 10, 9, 10. Every bus within +/-adj_max (from
    when it may leave), 7-min layover, run order. Later trips keep the same gaps.
  * The page also gets 1..5 duties each side with their EWT and largest hold (more duties = lower EWT, more buses held).

V13.18 (10 Nov 2026) - EVEN HEADWAY with no big jumps (even_step)
  * Every departure gap differs from the gap before it by at most even_step (2) min - 16, 14, 12 is fine, 16, 21, 6 is not - from the
    timetable gap before the first trip to the timetable gap after the next trip (Trip n+1 keeps its time). Inside that, the lowest
    waiting: sum of (gap - scheduled)^2 is the least, then the least holding. Solved exactly in whole minutes (dynamic programming over
    each trip's departure time and its gap). Every bus: not before arrival + 7 min, at most adj_max early, at most even_hold_max late,
    run order. When the trips simulated are too few for even_step, the smallest step that works is used and the page says so.
  * The page shows steps 1..6 with their EWT and largest hold.

V13.15 (9 Nov 2026) - run order kept (no_overtake)
  * Trip adjustment, halfway regulation and the steady headway never let a trip leave before the trip in front of it: T6, T7, T8 in that
    order, never T7, T8 and then the late T6. A bus that is ready before the late bus in front of it waits and leaves min_dep_gap (2 min)
    after it; that wait counts from its queued time, so the +/-adj_max limit is measured from there (the page says "waits for Trip 6").
    Later trips keep the run order at every terminal. NO ACTION is unchanged: every bus leaves when it is ready.

Pure computation (numpy), no I/O. Decision support only.
"""
import math
import numpy as np

MODEL_VERSION = "recovery-2.9-even-smooth"
EPS = 1e-6

PARAMS = {
    "n_trips": 10,
    "layover_min": 10.0,          # scheduled layover (both terminals)
    "min_layover_min": 7.0,       # HARD: BC minimum layover before a full trip
    "adj_max": 8.0,               # HARD: artificial adjustment of a departure, +/- this many min against its natural time
    "min_dep_gap": 2.0,           # two departures closer than this at the interchange = simultaneous departure
    "horizon_side": 3,            # regulate at least 3 trips before + 3 after the late trips
    "balance_trips": 6,           # BALANCE TRIPS: subsequent trips of every bus assessed (UP 1, DOWN 1, UP 2 ...); user-editable 1..16
    "ewt_gain_min": 0.10,         # halfway must cut the Average EWT by at least this many min ...
    "ewt_gain_pct": 5.0,          # ... and by at least this % of the Continue scenario's Average EWT
    "ewt_gain_per_km": 0.02,      # mileage priority: min of Average EWT saved per km not operated
    "ewt_adjust_min": 0.05,
    "allow_adjacent_halfway": 0,
    # ---- V13.9 terminal regulation on the later balance trips (two-way / even-spacing holding)
    "term_reg": 1,                # 1 = the AI also regulates every terminal departure after UP 1 (both scenarios); 0 = buses just leave at max(schedule, arrival + 7)
    "term_hold_max": 8.0,         # HARD: hold at a terminal at most this many min beyond the natural departure
    "term_early_max": 3.0,        # a bus may leave at most this many min before its natural departure (never before arrival + minimum layover)
    "term_min_adj": 1.0,          # dead band: changes smaller than this are not instructed (the bus keeps its natural time)
    "term_tol_pct": 20.0,         # a terminal headway within +/- this % of scheduled is normal: only buses near an abnormal headway are regulated
    "term_zone": 2,               # ... and at most this many buses either side of an abnormal headway take part
    "term_total_max": 0.0,        # optional cap on the TOTAL artificial hold per BC over the horizon (UP 1 included); 0 = off - each departure is capped by term_hold_max
    "qos_over_max": 6.0,
    "term_mode": "pattern",       # later trips of an adjusted plan: "pattern" = every bus keeps its adjusted UP 1 slot (same spacing every trip); "even" = even spacing at each terminal
    "halfway_regulate": 1,        # 1 = a halfway plan also holds / releases the trips around the lost trip (within halfway_adj_max); 0 = no other trip adjusted
    "halfway_adj_max": -1.0,      # max hold / release of those trips; -1 = auto = scheduled layover - minimum layover (what the BC wins back before trip 2)
    "adjust_even": 1,             # 1 = Trip adjustment evens out the gaps (every interchange headway as close to scheduled as the limits allow) when that is at least as good for passengers; 0 = the cost search only
    "even_side": 6,               # ... moving up to this many trips either side of the late trip(s)
    "adjust_style": "least_wait", # V13.14 Trip adjustment: "least_wait" = the least-waiting plan within +/-adj_max (V13.13); "steady" = every trip from trip 2
                                  # leaves ONE whole-minute headway apart and keeps it on every later trip (holds may pass adj_max: the price of one number)
    "no_overtake": 1,             # V13.15 HARD: the AI's plans keep the run order - no trip leaves before the trip in front of it (T6, T7, T8 - never T7, T8, then T6).
                                  # A bus that has to wait behind a late bus leaves min_dep_gap after it; that wait may pass adj_max. No action is not changed.
    "steady_tol": 1.0,            # V13.16 (former one-number even headway, kept for steady_plan_all): each gap h +/- this many min
    "steady_side": 2,             # V13.17 (former window version, kept for steady_plan_window): duties above and below the late trip(s)
    "even_step": 2,               # V13.18 EVEN HEADWAY: a departure gap may differ from the gap before it by at most this many min (16 -> 14 -> 12); when the
                                  # trips simulated are too few for that, the smallest step that works is used (and said)
    "even_hold_max": 20.0,        # ... a bus may be held up to this many min for it (past adj_max: the page says so)
    "steady_hold_max": 0.0,       # optional cap on a steady-headway hold (min); 0 = no cap. Over the cap -> the least-waiting plan is used instead
    "win_min": 30.0,              # window for the average / max headway: this many min before and after the longest headway at each AITP          # QoS 1.2 (HARD): an adjusted departure headway may be at most this many min longer than the scheduled headway
    "w_qos": 40.0,                # search cost per minute a departure headway is over the QoS 1.2 limit  # 0 = two back-to-back trips (e.g. T3 + T4) may NOT both start halfway; 1 = allowed       # a departure adjustment is used (instead of no action) only if it cuts the Average EWT by at least this
    "down_run_factor": 1.0,       # DOWN running time = UP running time x this
    "halfway_min_late": 1.0,      # V13.9: every trip that would leave late gets a Halfway scenario simulated (EWT decides, not the delay)
    "halfway_skip_layover": 1.0,  # 1 = the halfway bus leaves the interchange straight away (starts downstream, no interchange layover)
    "offsvc_factor": 0.7,         # off-service running = this x in-service running time (only when no road-routed time is available)
    "prep_min": 2.0,              # operational preparation at the halfway stop before entering passenger service
    "start_early_max": 5.0,       # a halfway start may be this much before the lost trip's slot at that stop ...
    "start_late_max": 10.0,       # ... or this much after it
    "max_mileage_km": 15.0,
    "min_halfway_km": 2.0,        # a "halfway" start this close to the interchange is just a departure without layover: not allowed
    "w_ic_short": 1.5,            # cost per minute that an interchange departure headway is below half the scheduled headway (dispatching buses 1 min apart)
    "min_remaining_pct": 20.0,
    "max_stops_tested": 10,
    "bunch_min": 3.0,
    "rec_hi_factor": 1.5,         # "normal headway" for recovery: between bunch_min and this x H
    "beta_det": 0.025,             # load sensitivity in the deterministic run
    "sims": 1000, "seed": 20260922,
    "mc_traffic_sd": 0.03, "mc_bus_sd": 0.015, "mc_beta_lo": 0.01, "mc_beta_hi": 0.04,
    "mc_incident_p": 0.05, "mc_incident_lo": 3.0, "mc_incident_hi": 8.0, "mc_eta_sd": 1.0,
}

MODES = ("balanced", "headway", "mileage")
WEIGHTS = {  # deterministic cost used to SEARCH each scenario's best adjustment; the Halfway vs Continue choice itself is made on EWT (decide)
    "balanced": dict(ewt=8.0, mx=1.0, wmx=2.0, rms=1.0, bunch=1.5, rec=0.4, km=0.6, bc=0.25, bcsum=0.2, adj=0.05),
    "headway":  dict(ewt=10.0, mx=1.5, wmx=3.0, rms=1.5, bunch=2.0, rec=0.6, km=0.2, bc=0.10, bcsum=0.1, adj=0.03),
    "mileage":  dict(ewt=6.0, mx=1.0, wmx=2.0, rms=1.0, bunch=1.5, rec=0.3, km=2.0, bc=0.20, bcsum=0.2, adj=0.05),
}
MAX_BALANCE = 16


def leg_name(L):
    """balance trip L (0-based) -> 'UP 1', 'DOWN 1', 'UP 2' ..."""
    return f"{'UP' if L % 2 == 0 else 'DOWN'} {L // 2 + 1}"


def horizon_text(n):
    span = leg_name(0) + (" \u2192 " + leg_name(n - 1) if n > 1 else "")
    return f"{n} balance trip{'s' if n != 1 else ''} ({span})"


def ewt_parts(h_act, h_sch):
    """LEVEL 1: EWT at one evaluation point from its own headways. Returns dict or None when fewer than 2 headways."""
    a = [float(x) for x in h_act if x is not None and not math.isnan(x)]
    s_ = [float(x) for x in h_sch if x is not None and not math.isnan(x)]
    if len(a) < 2 or len(s_) < 2 or sum(a) <= 0 or sum(s_) <= 0:
        return None
    awt = sum(x * x for x in a) / (2.0 * sum(a))
    swt = sum(x * x for x in s_) / (2.0 * sum(s_))
    return {"awt": awt, "swt": swt, "ewt": awt - swt, "n": len(a), "sum_h": sum(a), "sum_h2": sum(x * x for x in a), "sum_hs": sum(s_), "sum_hs2": sum(x * x for x in s_)}


def win_stats(gaps, win=30.0, H=None, off=1.0):
    """The disturbed period at one point: from `win` min before the first headway more than `off` min away from the scheduled H to `win` min
    after the last one (around the longest headway when none is off). -> average / max / min of the headways inside it."""
    g = [float(x) for x in gaps if x is not None and not math.isnan(x)]
    if not g:
        return None
    t = [0.0]
    for x in g:
        t.append(t[-1] + x)
    bad = [k for k in range(len(g)) if H is not None and abs(g[k] - H) > off + 1e-9]
    if bad:
        a, b = t[bad[0]] - win, t[bad[-1] + 1] + win
    else:
        m = max(range(len(g)), key=lambda k: (g[k], -k))
        a, b = t[m] - win, t[m + 1] + win
    W = [g[k] for k in range(len(g)) if t[k] >= a - 1e-9 and t[k + 1] <= b + 1e-9]
    return {"avg": _r(sum(W) / len(W), 1), "max": _r(max(W), 1), "min": _r(min(W), 1), "n": len(W)}


def hm(m):
    if m is None or (isinstance(m, float) and math.isnan(m)):
        return "--:--"
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _r(x, n=1):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else round(x, n)


def _pct(a, q):
    return _r(np.percentile(a, q), 1)


# ============================================================================ the chain simulator (vectorised over rows = plans or stress test draws)
class Chain:
    def __init__(self, ctx, P):
        self.P = P
        self.H = H = float(ctx["H"])
        self.n = n = int(P["n_trips"])
        self.tau = [float(x) for x in ctx["tau"]]
        self.n_st = len(self.tau)
        self.stop_s = ctx.get("stop_s") or [0.0] * self.n_st
        self.route_km = float(ctx.get("route_km") or (self.stop_s[-1] if self.stop_s else 0.0))
        self.names = ctx.get("stop_names") or [""] * self.n_st
        self.codes = ctx.get("stop_codes") or [""] * self.n_st
        self.t0 = float(ctx["t0"])
        lay, ml = float(P["layover_min"]), float(P["min_layover_min"])
        self.lay, self.ml = lay, ml
        late = list(ctx.get("late") or [])
        late = [float(x or 0.0) for x in late] + [0.0] * n
        self.late = late[:n]
        self.B = n + 2                                             # bus 0 and n+1 = on-schedule neighbours
        self.S = np.array([self.t0 + (b - 1) * H for b in range(self.B)])
        # individual scheduled departures (timetable) when given; otherwise the constant target headway H
        sd = [float(x) for x in (ctx.get("sched_dep") or []) if x is not None]
        self.sched_basis = "constant"
        if len(sd) >= n and all(sd[i + 1] > sd[i] for i in range(n - 1)):
            self.S[1:n + 1] = sd[:n]
            self.S[0] = self.S[1] - ((self.S[2] - self.S[1]) if n > 1 else H)
            self.S[n + 1] = self.S[n] + ((self.S[n] - self.S[n - 1]) if n > 1 else H)
            hs = np.diff(self.S)
            self.sched_basis = "individual" if float(hs.max() - hs.min()) > 0.01 else "constant"
            self.t0 = float(self.S[1])
        self.act_arr = np.array([self.S[b] - lay + (self.late[b - 1] if 1 <= b <= n else 0.0) for b in range(self.B)])
        self.ready = self.act_arr + ml
        self.nat = np.maximum(self.S, self.ready)
        now = ctx.get("now")
        now = float(now) if now is not None else None
        late_b = [b for b in range(1, n + 1) if self.nat[b] - self.S[b] >= 1.0 - EPS]
        first_late = min((self.nat[b] for b in late_b), default=None)
        # LIVE only while the disruption is still ahead of us (the first late trip has not left yet). If the entered timetable is
        # already in the past (a what-if / replay), locking by the clock would freeze every trip and the AI could do nothing:
        # plan from the first departure instead (only trip 1 counts as departed).
        self.live = bool(now is not None and self.t0 - 180 <= now and (first_late is None or now < first_late - EPS))
        self.now = now if self.live else self.t0
        self.locked = np.zeros(self.B, bool)
        if self.now is not None:
            for b in range(1, n + 1):
                self.locked[b] = self.nat[b] <= self.now + EPS
        self.ref = self.now if self.now is not None else self.t0
        R_up = self.tau[-1]
        R_dn = R_up * float(P["down_run_factor"])
        self.NL = int(max(1, min(MAX_BALANCE, round(float(P.get("balance_trips") or 6)))))
        self.R = [R_up if L % 2 == 0 else R_dn for L in range(self.NL)]
        self.off = [sum(self.R[l] + lay for l in range(L)) for L in range(self.NL)]
        # timing points
        self.cand_j = sorted({int(c["j"]) for c in (ctx.get("candidates") or []) if 0 < int(c["j"]) < self.n_st - 1})
        base = {0, self.n_st - 1} | {int(round(i * (self.n_st - 1) / 8)) for i in range(9)}
        # AITP: the stops the user ticked for the EWT (codes of this direction = UP trips, of the return direction = DOWN trips)
        code_j = {c: j for j, c in enumerate(self.codes) if c}
        a_up = [str(c) for c in (ctx.get("aitp_up") or [])]
        a_dn = [str(c) for c in (ctx.get("aitp_dn") or [])]
        self.aitp_on = bool(a_up or a_dn)
        self.aitp_up_j = sorted({code_j[c] for c in a_up if c in code_j})
        self.aitp_missing = [c for c in a_up if c not in code_j]
        self.pts_up = sorted(base | set(self.cand_j) | set(self.aitp_up_j))
        self.prof_up = np.array([self.tau[j] for j in self.pts_up])
        cnt = [0] * len(self.pts_up)
        for s in range(self.n_st):                               # each stop counts for its nearest timing point
            k = min(range(len(self.pts_up)), key=lambda q: (abs(self.pts_up[q] - s), q))
            cnt[k] += 1
        self.w_up = np.array(cnt, float)
        dn = ctx.get("down_stops") or []                          # [(name, code, km)] of the return direction: labels + AITP positions
        km_dn = (dn[-1][2] or 0.0) if dn else 0.0
        dn_code = {x[1]: k for k, x in enumerate(dn)}
        aitp_f = set()
        for c in a_dn:
            if c in dn_code:
                k = dn_code[c]
                aitp_f.add(round((dn[k][2] or 0.0) / km_dn if km_dn > 0 else k / max(1, len(dn) - 1), 6))
            else:
                self.aitp_missing.append(c)
        self.frac_dn = sorted({round(float(f), 6) for f in np.linspace(0, 1, 7)} | aitp_f)   # DOWN running time assumed proportional to distance
        self.aitp_dn_f = aitp_f
        self.prof_dn = np.array([R_dn * f for f in self.frac_dn])
        self.dn_lbl = []
        for f in self.frac_dn:
            if dn:
                km_tot = dn[-1][2] or 0.0
                k = min(range(len(dn)), key=lambda q: abs((dn[q][2] or 0.0) - f * km_tot)) if km_tot > 0 else int(round(f * (len(dn) - 1)))
                self.dn_lbl.append((dn[k][0], dn[k][1]))
            else:
                self.dn_lbl.append((None, None))
        self.w_dn = np.ones(len(self.prof_dn))
        self.pidx = {j: k for k, j in enumerate(self.pts_up)}
        # which points count for the EWT: every point, or only the AITP stops when any are ticked
        if self.aitp_on:
            self.ewt_mask_up = np.array([j in self.aitp_up_j for j in self.pts_up])
            self.ewt_mask_dn = np.array([f in aitp_f for f in self.frac_dn])
        else:
            self.ewt_mask_up = np.ones(len(self.pts_up), bool)
            self.ewt_mask_dn = np.ones(len(self.frac_dn), bool)
        self.offsvc = {int(k): float(v) for k, v in (ctx.get("offsvc_min") or {}).items() if v is not None}
        self.chain_len = self.off[-1] + self.R[-1] + (self.S[-1] - self.ref)

    # ---------------------------------------------------------------- one leg
    def _leg(self, D0, L, joins, nz, rec_all):
        H, P = self.H, self.P
        Sn, B = D0.shape
        up = L % 2 == 0
        prof = self.prof_up if up else self.prof_dn
        w = self.w_up if up else self.w_dn
        key = D0.copy()
        for (b, pj, st) in joins:
            key[:, b] = st - prof[pj]
        order = np.argsort(key, axis=1, kind="stable")
        T = np.take_along_axis(D0, order, 1)
        fb = np.take_along_axis(nz["bus"][:, L, :], order, 1) if nz else None
        rows = np.arange(Sn)[:, None]
        m = len(prof)
        maxg = np.zeros((Sn, m)); ming = np.zeros((Sn, m)); sq = np.zeros(Sn); cnt = np.zeros(Sn)
        hrv = np.full((Sn, m), np.nan); hrn = np.zeros((Sn, m))   # hourly average headway: the worst 60-min window at each evaluation point
        rec = np.full(Sn, -np.inf)
        beta = nz["beta"][:, None] if nz else P["beta_det"]
        legf = nz["leg"][:, L][:, None] if nz else 1.0
        idx = np.arange(B)[None, :]
        pts_store = []
        # scheduled passing times of every bus at every point of this balance trip (the halfway / lost trip keeps its scheduled slot)
        sch_T = np.sort(self.S + self.off[L])
        ewt = np.full((Sn, m), np.nan)
        awt_ = np.full((Sn, m), np.nan)
        swt_ = np.zeros(m)
        sch_h = np.diff(sch_T)

        def gaps(T_):
            act = ~np.isnan(T_)
            ii = np.where(act, idx, -1)
            ii = np.maximum.accumulate(ii, axis=1)
            prev_i = np.concatenate([np.full((Sn, 1), -1), ii[:, :-1]], axis=1)
            prev = np.take_along_axis(T_, np.maximum(prev_i, 0), 1)
            g = np.where((prev_i >= 0) & act, T_ - prev, np.nan)
            return g

        for p in range(m):
            if p > 0:
                seg = prof[p] - prof[p - 1]
                g0 = gaps(T)
                h = np.where(np.isnan(g0), H, g0)
                f = np.clip(1.0 + beta * (h - H) / H, 0.7, 1.5) * legf
                if fb is not None:
                    f = f * fb
                Tn = T + seg * f
                if nz is not None and nz["inc"] is not None:
                    il, ip, it, iv = nz["inc"]
                    hit = (il == L) & (ip == p)
                    if hit.any():
                        win = (T >= it[:, None]) & (T < it[:, None] + 30.0) & hit[:, None]
                        Tn = Tn + np.where(win, iv[:, None], 0.0)
                cm = np.fmax.accumulate(Tn, axis=1)
                T = np.where(np.isnan(Tn), np.nan, cm)
            if joins:
                changed = False
                for (b, pj, st) in joins:
                    if pj == p:
                        pos = np.argmax(order == b, axis=1)
                        T[np.arange(Sn), pos] = st
                        changed = True
                if changed:
                    o2 = np.argsort(np.where(np.isnan(T), np.inf, T), axis=1, kind="stable")
                    T = np.take_along_axis(T, o2, 1)
                    order = np.take_along_axis(order, o2, 1)
                    if fb is not None:
                        fb = np.take_along_axis(fb, o2, 1)
                    cm = np.fmax.accumulate(T, axis=1)
                    T = np.where(np.isnan(T), np.nan, cm)
            g = gaps(T)
            with np.errstate(all="ignore"):
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    maxg[:, p] = np.nanmax(g, axis=1)
                    ming[:, p] = np.nanmin(g, axis=1)
            # LEVEL 1 EWT at this point: AWT from the simulated headways, SWT from the scheduled headways (never pooled with other points)
            gs = np.where(np.isnan(g), 0.0, g)
            n_h = (~np.isnan(g)).sum(1)
            s1, s2 = gs.sum(1), (gs * gs).sum(1)
            swt_[p] = float((sch_h ** 2).sum() / (2.0 * sch_h.sum())) if sch_h.sum() > 0 else 0.0
            with np.errstate(all="ignore"):
                aw = np.where((n_h >= 2) & (s1 > 0), s2 / (2.0 * np.maximum(s1, EPS)), np.nan)
            awt_[:, p] = aw
            ewt[:, p] = aw - swt_[p]
            hrv[:, p], hrn[:, p] = _hourly(T)
            dev = np.where(np.isnan(g), 0.0, (g - H) ** 2)
            sq += w[p] * dev.sum(1)
            cnt += w[p] * (~np.isnan(g)).sum(1)
            if rec_all or p == 0:
                bad = (g > H * P["rec_hi_factor"] + EPS) | (g < P["bunch_min"] - EPS)
                stamp = np.where(bad, T, -np.inf)
                rec = np.maximum(rec, stamp.max(1))
            if p == 0:
                first_T, first_order, first_g = T.copy(), order.copy(), g.copy()
            if Sn == 1:
                pts_store.append((T[0].copy(), order[0].copy(), g[0].copy()))
        A = np.empty_like(T)
        A[rows, order] = T
        nonpt = ~(self.ewt_mask_up if up else self.ewt_mask_dn)
        ewt[:, nonpt] = np.nan          # not an AITP: simulated, but not an EWT evaluation point
        hrv[:, nonpt] = np.nan
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                hr_leg = np.nanmax(hrv, 1)
        hr_at = np.argmax(np.where(np.isnan(hrv), -np.inf, hrv), 1)
        hr_n = hrn[np.arange(Sn), hr_at]
        return {"hr": hr_leg, "hr_n": hr_n, "hr_p": hr_at, "end": A, "maxg": maxg, "ming": ming, "sq": sq, "cnt": cnt, "rec": rec, "w": w,
                "dep_T": first_T, "dep_order": first_order, "dep_g": first_g, "pts": pts_store,
                "ewt": ewt, "awt": awt_, "swt": swt_, "sch_h": sch_h}

    # ---------------------------------------------------------------- full chain
    def run(self, dep1, halfway=None, nz=None, keep=False, reg=True, end=None, steady=False):
        """dep1: (S, B) planned interchange departures for UP 1 (anchors / halfway columns ignored).
        halfway: list of (trip b, stop j, start (S,)) - start = the time the halfway bus starts at stop j.
        nz: stress test noise (see noise()), None = deterministic.
        end: (S,) departure of the next trip after the block (None = on schedule). steady: later trips keep each bus's UP 1 slot exactly
        (the steady headway), leaving later only when not ready."""
        P, H = self.P, self.H
        Sn = dep1.shape[0]
        D = dep1.astype(float).copy()
        ready = np.tile(self.ready, (Sn, 1))
        if nz is not None:
            ready = ready + nz["eta"]
        D = np.maximum(D, ready)
        lk = self.locked
        D[:, lk] = self.nat[lk]
        D[:, 0], D[:, -1] = self.S[0], self.S[-1]
        if end is not None:
            D[:, -1] = np.maximum(self.S[-1], np.asarray(end, float))
        joins = []
        for (b, j, st) in (halfway or []):
            D[:, b] = np.nan
            joins.append((b, self.pidx[j], np.asarray(st, float) + self.tau[j]))
        legs = []
        term = []
        used = np.maximum(0.0, np.nan_to_num(D - np.maximum(self.nat[None, :], ready)))     # artificial hold already given at UP 1
        keepk = np.nan_to_num(D - self.S[None, :], nan=0.0)                         # each bus's UP 1 slot against its timetable (halfway bus: 0 = back on its slot)
        qos = [self._qos_dep(D, self.S, 0)]                                         # QoS 1.2 at the UP 1 interchange departures
        rec = np.full(Sn, -np.inf)
        late_irr = np.zeros(Sn, bool)
        cur = D
        for L in range(self.NL):
            lg = self._leg(cur, L, joins if L == 0 else [], nz, L == 0)
            rec = np.maximum(rec, lg["rec"])
            if L >= self.NL - 2:
                late_irr |= np.isfinite(lg["rec"])
            legs.append(lg)
            if L + 1 < self.NL:
                sched = self.S + self.off[L + 1]
                rdy = lg["end"] + self.ml
                nxt = np.maximum(sched[None, :], rdy)
                nxt[:, 0], nxt[:, -1] = sched[0], sched[-1]
                if steady and reg:                                       # STEADY HEADWAY: every bus keeps its UP 1 slot + the cycle (same h every trip)
                    nat_ = nxt.copy()
                    nat_[:, -1] = np.maximum(sched[-1], rdy[:, -1])
                    tgt = sched[None, :] + keepk
                    nxt = np.maximum(tgt, np.where(np.isnan(rdy), tgt, rdy))
                    nxt[:, 0] = sched[0]
                    nxt = self.run_order(nxt)
                    used = used + np.maximum(0.0, nxt - nat_)
                    if keep:
                        term.append((L + 1, nat_[0].copy(), nxt[0].copy()))
                elif P.get("term_reg") and reg and P.get("term_mode", "pattern") == "pattern":
                    rg = self.run_order(self.keep_pattern(nxt, rdy, sched, keepk, used))   # TRIP ADJUSTMENT: the adjusted spacing is kept on every later trip
                    used = used + np.maximum(0.0, rg - nxt)
                    if keep:
                        term.append((L + 1, nxt[0].copy(), rg[0].copy()))
                    nxt = rg
                elif P.get("term_reg") and reg:                          # NO ACTION (reg=False): later trips are not adjusted either
                    rg = self.run_order(self.term_regulate(nxt, rdy, used, sched=sched))
                    used = used + np.maximum(0.0, rg - nxt)
                    if keep:
                        term.append((L + 1, nxt[0].copy(), rg[0].copy()))
                    nxt = rg
                qos.append(self._qos_dep(nxt, sched, L + 1))
                cur = nxt
        sched_end = self.S + self.off[-1] + self.R[-1]
        bc = legs[-1]["end"][:, 1:-1] - sched_end[None, 1:-1]
        # UP1 bus lateness at the far terminal (the first BC impact)
        up1_late = legs[0]["end"][:, 1:-1] - (self.S + self.R[0])[None, 1:-1]
        wsum = lambda lg: (lg["maxg"] * lg["w"][None, :]).sum(1) / lg["w"].sum()
        with np.errstate(all="ignore"):
            leg_max = np.stack([np.nanmax(lg["maxg"], 1) for lg in legs], 1)
            leg_wmax = np.stack([wsum(lg) for lg in legs], 1)
            leg_min = np.stack([np.nanmin(lg["ming"], 1) for lg in legs], 1)
        # LEVEL 2: Average EWT = simple mean of the valid point EWTs over every balance trip; also the maximum
        E = np.concatenate([lg["ewt"] for lg in legs], axis=1)
        ok_ = ~np.isnan(E)
        n_ok = ok_.sum(1)
        ewt_avg = np.where(n_ok > 0, np.where(ok_, E, 0.0).sum(1) / np.maximum(n_ok, 1), 0.0)
        ewt_max = np.where(ok_, E, -np.inf).max(1)
        ewt_max = np.where(np.isfinite(ewt_max), ewt_max, 0.0)
        sq = sum(lg["sq"] for lg in legs); cnt = sum(lg["cnt"] for lg in legs)
        bunch_pts = sum(((lg["ming"] < P["bunch_min"] - EPS) * lg["w"][None, :]).sum(1) for lg in legs)
        bunch_tot = sum(lg["w"].sum() for lg in legs)
        g0 = legs[0]["dep_g"]
        ic_short = np.nansum(np.maximum(0.0, 0.5 * H - g0), 1)
        # HOURLY AVERAGE HEADWAY (worst hour): at every evaluation point, the 60-min window with the highest average gap, over every balance trip
        hr_all = np.stack([np.where(np.isnan(lg["hr"]), -np.inf, lg["hr"]) for lg in legs], 1)
        hr_L = hr_all.argmax(1)
        hr_hw = hr_all.max(1)
        hr_hw = np.where(np.isfinite(hr_hw), hr_hw, np.nan)
        hr_n = np.array([legs[L]["hr_n"][i] for i, L in enumerate(hr_L)])
        hr_p = np.array([legs[L]["hr_p"][i] for i, L in enumerate(hr_L)])
        out = {"hr_hw": hr_hw, "hr_n": hr_n, "hr_L": hr_L, "hr_p": hr_p, "ic_short": ic_short, "leg_min": leg_min, "max": np.nanmax(leg_max, 1), "ic_max": np.nanmax(g0, 1), "ic_sq": np.nansum((g0 - H) ** 2, 1), "wmax": leg_wmax.mean(1), "leg_max": leg_max, "leg_wmax": leg_wmax,
               "rms": np.sqrt(sq / np.maximum(cnt, 1)), "bunch": bunch_pts / bunch_tot, "any_bunch": (leg_min < P["bunch_min"] - EPS).any(1),
               "rec": np.where(np.isinf(rec), 0.0, np.maximum(0.0, rec - self.ref)), "unrec": late_irr, "bc": bc, "bc_max": np.maximum(bc, 0).max(1),
               "bc_sum": np.maximum(bc, 0).sum(1), "up1_late": up1_late, "D": D, "ewt_avg": ewt_avg, "ewt_max": ewt_max, "ewt_n": n_ok,
               "qos_ex": sum(q["ex"] for q in qos), "qos_over": np.max(np.stack([q["over"] for q in qos], 1), 1), "qos_n": sum(q["n"] for q in qos)}
        if keep:
            out["qos_legs"] = qos
        if keep:
            out["legs"] = legs
            out["term"] = term
        return out

    def _qos_dep(self, D, sched, L):
        """Departure headways at the start of balance trip L against the positional scheduled headways. A halfway trip (NaN at the
        interchange) does not depart: the gap it leaves is judged against the normal scheduled headway (QoS 1.2: 24 -> at most 18)."""
        add = float(self.P.get("qos_over_max") or 0.0)
        ok = ~np.isnan(D[0])
        Dv = np.sort(D[:, ok], axis=1)
        sa = np.asarray(sched, float)
        sv = np.sort(sa[ok])
        g = np.diff(Dv, axis=1)
        sall = np.sort(sa)
        sg = np.array([[max(np.diff(sall[(sall >= sv[k] - EPS) & (sall <= sv[k + 1] + EPS)]).tolist() or [sv[k + 1] - sv[k]]) for k in range(len(sv) - 1)]])
        over = g - sg
        ex = np.maximum(0.0, over - add) if add > 0 else np.zeros_like(over)
        return {"L": L, "over": over.max(1) if over.shape[1] else np.zeros(D.shape[0]), "ex": ex.sum(1), "n": (ex > EPS).sum(1),
                "g": g, "sg": sg[0], "order": np.argsort(D[:, ok], axis=1, kind="stable"), "ids": np.nonzero(ok)[0]}

    # ---------------------------------------------------------------- terminal regulation (later balance trips)
    def term_regulate(self, N, R, used=None, iters=40, alpha=0.5, sched=None):
        """Two-way even-spacing holding at a terminal. N: natural departures (Sn, B), R: arrival + minimum layover.
        Each bus moves towards the midpoint of its neighbours, d_i* = (d_(i-1) + d_(i+1)) / 2, inside
        max(R_i, N_i - early) <= d_i <= N_i + hold; the on-schedule buses 0 and n+1 stay fixed; no overtaking;
        a change under the dead band is dropped. The same rule runs in every scenario and every stress-test future."""
        P = self.P
        Sn, B = N.shape
        R = np.where(np.isnan(R), N, R)
        lo = np.maximum(R, N - P["term_early_max"])
        tot = float(P.get("term_total_max") or 0.0)
        room = P["term_hold_max"] if (used is None or tot <= 0) else np.clip(tot - used, 0.0, P["term_hold_max"])
        hi = N + room
        lo[:, 0], hi[:, 0], lo[:, -1], hi[:, -1] = N[:, 0], N[:, 0], N[:, -1], N[:, -1]
        order = np.argsort(N, axis=1, kind="stable")
        T = np.take_along_axis(N, order, 1)
        lo_s, hi_s = np.take_along_axis(lo, order, 1), np.take_along_axis(hi, order, 1)
        # only buses within `term_zone` of an abnormal headway (outside +/- tol of H) take part; the rest keep their natural time
        g = np.diff(T, axis=1)
        bad = np.abs(g - self.H) > P["term_tol_pct"] / 100.0 * self.H + EPS
        M = np.arange(B - 1)
        dist = np.full(T.shape, 99)
        for k in range(B):                                               # headway m touches positions m and m+1 (distance 0)
            d = np.where(M >= k, M - k, k - 1 - M)
            dist[:, k] = np.where(bad, d[None, :], 99).min(1)
        zone = dist <= int(P["term_zone"])
        lo_s = np.where(zone, lo_s, T)
        hi_s = np.where(zone, hi_s, T)
        for _ in range(iters):
            mid = 0.5 * (T[:, :-2] + T[:, 2:])
            T[:, 1:-1] = np.clip(T[:, 1:-1] + alpha * (mid - T[:, 1:-1]), lo_s[:, 1:-1], hi_s[:, 1:-1])
            T = np.maximum.accumulate(T, axis=1)                          # no overtaking at the terminal
            T = np.minimum(T, hi_s)
        D = np.empty_like(T)
        D[np.arange(Sn)[:, None], order] = T
        D = np.where(np.abs(D - N) >= P["term_min_adj"] - EPS, np.round(D), N)
        D = np.clip(D, lo, hi)
        D[:, 0], D[:, -1] = N[:, 0], N[:, -1]
        if sched is not None:
            D = self.qos_fix(D, lo, hi, sched)
        return D

    def run_order(self, D):
        """Run order at a terminal (no_overtake): a regulated departure never goes before the trip in front of it."""
        if not self.P.get("no_overtake", 1):
            return D
        D = D.copy()
        for b in range(2, self.n + 1):
            D[:, b] = np.fmax(D[:, b], D[:, b - 1])
        return D

    def keep_pattern(self, N, R, sched, k, used=None):
        """Later trip of an adjusted plan: every bus leaves at its own UP 1 slot shifted by the scheduled cycle (sched + k), i.e. the adjusted
        spacing is kept. Never before it is ready (arrival + minimum layover), hold <= term_hold_max and early <= term_early_max against its
        natural departure N; the on-schedule buses 0 and n+1 stay fixed; changes under the dead band are dropped; then QoS 1.2."""
        P = self.P
        R = np.where(np.isnan(R), N, R)
        lo = np.maximum(R, N - P["term_early_max"])
        tot = float(P.get("term_total_max") or 0.0)
        hi = N + (P["term_hold_max"] if (used is None or tot <= 0) else np.clip(tot - used, 0.0, P["term_hold_max"]))
        T = np.clip(np.asarray(sched, float)[None, :] + k, lo, hi)
        T = np.where(np.abs(T - N) >= P["term_min_adj"] - EPS, np.round(T), N)
        T = np.clip(T, lo, hi)
        T[:, 0], T[:, -1] = N[:, 0], N[:, -1]
        return self.qos_fix(T, lo, hi, sched)

    def qos_fix(self, D, lo, hi, sched):
        """QoS 1.2: no departure headway more than qos_over_max over its scheduled headway, where the limits allow it. Holds the bus in front
        of a long gap (no later than hi) and releases the bus behind it (no earlier than lo = arrival + 7 min); keeps the order."""
        P = self.P
        add = float(P.get("qos_over_max") or 0.0)
        if add <= 0:
            return D
        Sn, B = D.shape
        order = np.argsort(D, axis=1, kind="stable")
        T = np.take_along_axis(D, order, 1).astype(float)
        los, his = np.take_along_axis(lo, order, 1), np.take_along_axis(hi, order, 1)
        lim = np.diff(np.sort(np.asarray(sched, float))) + add                       # positional scheduled headway + 6
        for _ in range(3):
            for k in range(B - 2, -1, -1):                                         # hold the bus in front of a long gap
                need = T[:, k + 1] - lim[k]
                T[:, k] = np.where(T[:, k] < need - EPS, np.minimum(his[:, k], np.ceil(need - EPS)), T[:, k])
            for k in range(B - 1):                                                 # release the bus behind it
                cap = T[:, k] + lim[k]
                T[:, k + 1] = np.where(T[:, k + 1] > cap + EPS, np.maximum(los[:, k + 1], np.floor(cap + EPS)), T[:, k + 1])
            T = np.maximum.accumulate(T, axis=1)
        out = np.empty_like(T)
        out[np.arange(Sn)[:, None], order] = T
        return out

    # ---------------------------------------------------------------- stress test noise
    def noise(self, Sn, rng):
        P = self.P
        nz = {"leg": np.clip(rng.normal(1.0, P["mc_traffic_sd"], (Sn, self.NL)), 0.8, 1.3),
              "bus": np.clip(rng.normal(1.0, P["mc_bus_sd"], (Sn, self.NL, self.B)), 0.85, 1.2),
              "beta": rng.uniform(P["mc_beta_lo"], P["mc_beta_hi"], Sn),
              "off": rng.uniform(0.9, 1.15, Sn)}
        nz["bus"][:, :, 0] = 1.0
        nz["bus"][:, :, -1] = 1.0
        eta = np.zeros((Sn, self.B))
        for b in range(1, self.n + 1):
            if self.now is not None and self.act_arr[b] <= self.now:
                continue                                        # already arrived: known
            ahead = max(0.0, self.act_arr[b] - self.ref)
            eta[:, b] = rng.normal(0.0, P["mc_eta_sd"] * (1.0 + ahead / 30.0), Sn)
        nz["eta"] = eta
        hit = rng.random(Sn) < P["mc_incident_p"]
        il = np.where(hit, rng.integers(0, self.NL, Sn), -1)
        ip = rng.integers(1, len(self.prof_dn), Sn)
        span = self.off[-1] + self.R[-1] + self.n * self.H
        it = self.t0 + rng.uniform(0, span, Sn)
        iv = rng.uniform(P["mc_incident_lo"], P["mc_incident_hi"], Sn)
        nz["inc"] = (il, ip, it, iv)
        return nz


# ============================================================================ plans
def _hourly(T, win=60.0):
    """Hourly average headway at one point: for every bus, the buses passing in the next 60 min; average gap = (last - first) / (buses - 1).
    Returns (the worst window's average gap, number of gaps in it) per row. A gap longer than an hour counts as its own window."""
    Ts = np.sort(T, axis=1)                               # missing (halfway-skipped) buses -> NaN, sorted to the end
    B = Ts.shape[1]
    if B < 2:
        return np.full(Ts.shape[0], np.nan), np.zeros(Ts.shape[0])
    val = Ts[:, 1:] - Ts[:, :-1]
    kk = np.ones_like(val)
    for k in range(2, B):
        d = Ts[:, k:] - Ts[:, :-k]
        ok = d <= win + EPS
        val[:, :B - k] = np.where(ok, d / k, val[:, :B - k])
        kk[:, :B - k] = np.where(ok, k, kk[:, :B - k])
    v = np.where(np.isnan(val), -np.inf, val)
    i = v.argmax(1)
    best = v[np.arange(len(i)), i]
    return np.where(np.isfinite(best), best, np.nan), kk[np.arange(len(i)), i]


def _cost(m, w, H, km, adj, w_short=1.5, w_qos=40.0):
    return (w_qos * m.get("qos_ex", 0.0) + w.get("ewt", 0.0) * m["ewt_avg"] + w_short * m["ic_short"] + w["mx"] * np.maximum(0.0, m["max"] - H) + w["wmx"] * np.maximum(0.0, m["wmax"] - H) + w["rms"] * m["rms"] + w["bunch"] * 10.0 * m["bunch"]
            + w["rec"] * m["rec"] / H + w["km"] * km + w["bc"] * m["bc_max"] + w["bcsum"] * m["bc_sum"] / 10.0 + w["adj"] * adj)


def _local_cost(m, H):
    """what a 'next departure only' controller looks at: the interchange headways of UP 1."""
    return np.maximum(0.0, m["ic_max"] - H) * 3.0 + 0.3 * m["ic_sq"] / H + 0.02 * m["adj"]


def _off(C, P, j):
    """off-service minutes from the interchange to stop j: road-routed if known, else a share of the in-service running time."""
    return C.offsvc.get(j, P["offsvc_factor"] * C.tau[j])


class Optimiser:
    def __init__(self, ctx):
        P = {**PARAMS, **{k: v for k, v in (ctx.get("params") or {}).items() if k in PARAMS}}
        self.P = P
        self.ctx = ctx
        self.C = Chain(ctx, P)
        self.mode = ctx.get("mode") if ctx.get("mode") in MODES else "balanced"
        self.W = WEIGHTS[self.mode]
        self.veh = ctx.get("veh") if ctx.get("veh") in ("own", "standby") else "own"
        self.reject = {}
        self.generated = 0
        self.families = {}

    # -------------------------------------------------- feasibility of a raw plan
    def _queue(self, drop=()):
        """Run order (no_overtake): the earliest each trip may leave without passing the trip in front of it = max(its natural time,
        the queued time of the trip in front + min_dep_gap). Halfway trips (drop) do not leave the interchange and are skipped."""
        C, P = self.C, self.P
        key = tuple(sorted(int(b) for b in drop))
        cache = self.__dict__.setdefault("_qc", {})
        if key in cache:
            return cache[key]
        gap, q, prev = float(P["min_dep_gap"]), {}, None
        for b in range(1, C.n + 1):
            if b in drop:
                continue
            v = float(C.nat[b])
            if P.get("no_overtake", 1) and prev is not None and not C.locked[b]:
                v = max(v, prev + gap)
            q[b], prev = v, v
        cache[key] = q
        return q

    def _order_fix(self, dep, drop=()):
        """Run order (no_overtake): every trip that would leave before the trip in front of it is held until min_dep_gap after it."""
        C, P = self.C, self.P
        if not P.get("no_overtake", 1):
            return dep
        gap, out, prev, prev_mv = float(P["min_dep_gap"]), dict(dep), None, False
        for b in range(1, C.n + 1):
            if b in drop:
                continue
            x = out.get(b, float(C.nat[b]))
            mv = abs(x - float(C.nat[b])) > 0.49
            if prev is not None and not C.locked[b] and x < prev + gap - EPS and (x < prev - EPS or mv or prev_mv):
                x, mv = float(math.ceil(prev + gap - EPS)), True          # passing, or squeezed next to a moved bus: wait behind it
                out[b] = x
            prev, prev_mv = x, mv
        return out

    def _bounds(self, b, lim=None, drop=()):
        C, P = self.C, self.P
        a = P["adj_max"] if lim is None else min(float(lim), P["adj_max"])
        lo = max(C.ready[b], C.nat[b] - a)
        hi = max(float(C.nat[b]), self._queue(drop).get(b, float(C.nat[b]))) + a      # the wait behind a late trip in front does not count
        if lo > hi:                                                  # not ready within the tighter limit: its natural departure only
            lo = hi = C.nat[b]
        return math.ceil(lo - 1e-9), math.floor(hi + 1e-9)

    def _hw_lim(self):
        """How far a halfway plan may move the other trips: what the BC wins back in the layover before trip 2 (auto)."""
        P = self.P
        v = float(P.get("halfway_adj_max", -1))
        if v < 0:
            v = max(0.0, float(P["layover_min"]) - float(P["min_layover_min"]))
        return min(v, float(P["adj_max"]))

    def _check(self, dep, hw, cap=True, end=None):
        """dep: {trip: target}, hw: [(b, j, start)] -> list of rejection reasons (empty = feasible).
        cap=False: the +/-adj_max limit does not apply (steady headway). end: departure of the next trip after the block."""
        C, P = self.C, self.P
        why = []
        qd = self._queue(tuple(h[0] for h in hw))
        for b, x in dep.items():
            if C.locked[b] and abs(x - C.nat[b]) > 0.49:
                why.append("Alters an already-departed trip")
                continue
            if x < C.ready[b] - 0.01:
                why.append("BC gets less than the 7-min minimum layover")
            if cap and (x < C.nat[b] - P["adj_max"] - 1e-6 or x > max(float(C.nat[b]), qd.get(b, float(C.nat[b]))) + P["adj_max"] + 1e-6):
                why.append(f"Adjustment exceeds \u00b1{P['adj_max']:g} min")
        hwb = {h[0] for h in hw}
        for (b, j, st) in hw:
            if C.locked[b]:
                why.append("Alters an already-departed trip")
            slot = C.S[b] + C.tau[j]
            if not (slot - P["start_early_max"] - 1e-6 <= st + C.tau[j] <= slot + P["start_late_max"] + 1e-6):
                why.append("Halfway start is outside the lost trip's slot window")
            if self.veh == "own":
                leave = C.act_arr[b] + (0.0 if P["halfway_skip_layover"] else C.ml)
                if leave + _off(C, P, j) + P["prep_min"] > st + C.tau[j] + 1e-6:
                    why.append("The late bus cannot reach the halfway stop in time")
        # simultaneous departures: only a pair that the PLAN creates (one of the two was moved) counts - buses that arrive together on their own
        # must stay adjustable, otherwise the AI could never spread them apart
        full = {b: dep.get(b, C.nat[b]) for b in range(1, C.n + 1) if b not in hwb}
        full[0], full[C.n + 1] = C.S[0], (C.S[-1] if end is None else max(float(C.S[-1]), float(end)))
        seq = sorted(full.items(), key=lambda kv: (kv[1], kv[0]))
        mv = lambda b: b in dep and abs(dep[b] - C.nat[b]) > 0.49
        for (a, xa), (c, xc) in zip(seq, seq[1:]):
            if xc - xa < P["min_dep_gap"] - 1e-6 and (mv(a) or mv(c)):
                why.append("Creates simultaneous departures at the interchange")
                break
        if P.get("no_overtake", 1):                                    # run order: no trip leaves before the trip in front of it
            o = [b for b in range(1, C.n + 1) if b not in hwb]
            if any(not C.locked[c] and full[c] < full[a] - 1e-6 for a, c in zip(o, o[1:])):
                why.append("A trip leaves before the trip in front of it (run order)")
            # a halfway start sits between the trip in front (already past the halfway stop) and the trip behind (not there yet)
            t_at = dict(full)
            for (b_, j_, st_) in hw:
                t_at[b_] = float(st_)                                    # start time, referred back to the interchange
            for (b_, j_, st_) in hw:
                fr = [x for x in range(b_ - 1, -1, -1) if x in t_at]
                bk = [x for x in range(b_ + 1, C.n + 2) if x in t_at]
                if (fr and t_at[fr[0]] > float(st_) - 1.0 + 1e-6) or (bk and t_at[bk[0]] < float(st_) + 1.0 - 1e-6):
                    why.append("Halfway start out of run order (the trip in front has not passed the stop yet)")
                    break
            if o and mv(o[-1]) and full[o[-1]] > full[C.n + 1] + 1e-6:
                why.append("Pushes the last trip behind the next trip (run order)")
        km = sum(C.stop_s[j] for (_, j, _) in hw)
        if hw and km > P["max_mileage_km"] + 1e-6:
            why.append(f"Mileage loss over the {P['max_mileage_km']:g} km limit")
        return sorted(set(why))

    def _count(self, fam, why):
        self.generated += 1
        self.families[fam] = self.families.get(fam, 0) + 1
        for w in why:
            self.reject[w] = self.reject.get(w, 0) + 1

    # -------------------------------------------------- batch evaluation
    def _eval(self, plans, nz=None):
        """plans: list of dict(dep={b:x}, hw=[(b,j,st)]) that share the same halfway structure (same b, j). Returns metrics + cost arrays."""
        C = self.C
        Sn = len(plans)
        D = np.tile(C.nat, (Sn, 1)).astype(float)
        for r, pl in enumerate(plans):
            for b, x in pl["dep"].items():
                D[r, b] = x
        hw = None
        if plans[0]["hw"]:
            hw = [(b, j, np.array([pl["hw"][k][2] for pl in plans])) for k, (b, j, _) in enumerate(plans[0]["hw"])]
        end = np.array([float(pl.get("end") or C.S[-1]) for pl in plans]) if any(pl.get("end") for pl in plans) else None
        m = C.run(D, hw, nz, reg=plans[0].get("reg", True), end=end, steady=bool(plans[0].get("steady")))
        km = np.array([sum(C.stop_s[j] for (_, j, _) in pl["hw"]) for pl in plans])
        hwb = [{h[0] for h in pl["hw"]} for pl in plans]
        adj = np.array([sum(abs(x - C.nat[b]) for b, x in pl["dep"].items() if b not in hwb[r]) for r, pl in enumerate(plans)])
        m["km"], m["adj"] = km, adj
        m["cost"] = _cost(m, self.W, C.H, km, adj, self.P["w_ic_short"], self.P["w_qos"])
        m["local"] = _local_cost(m, C.H) + self.P["w_qos"] * m["qos_ex"]
        return m

    # -------------------------------------------------- candidate generation
    def _focus(self):
        C, P = self.C, self.P
        lateb = [b for b in range(1, C.n + 1) if C.nat[b] - C.S[b] >= 1.0 - EPS]
        if not lateb:
            return [], []
        s = int(P["horizon_side"])
        lo, hi = max(1, min(lateb) - s), min(C.n, max(lateb) + s)
        return lateb, list(range(lo, hi + 1))

    def _spreads(self, focus, drop=()):
        """even spacing between two anchors, for every anchor pair (raw, may be infeasible) -> list of {b: x}."""
        C, P = self.C, self.P
        buses = [0] + [b for b in range(1, C.n + 1) if b not in drop] + [C.n + 1]
        seq = buses if P.get("no_overtake", 1) else sorted(buses, key=lambda b: (C.nat[b], b))
        pos = [k for k, b in enumerate(seq) if b in focus or b in (0, C.n + 1)]
        out = []
        if not pos:
            return out
        lo_k, hi_k = max(0, min(pos) - 1), min(len(seq) - 1, max(pos) + 1)
        for a in range(lo_k, hi_k + 1):
            for c in range(a + 2, min(hi_k, a + 8) + 1):
                xa, xc = C.nat[seq[a]], C.nat[seq[c]]
                dep = {}
                for q in range(a + 1, c):
                    dep[seq[q]] = float(round(xa + (xc - xa) * (q - a) / (c - a)))
                out.append(dep)
        return out

    def _project(self, dep, drop=(), lim=None):
        """clip a raw plan into the feasible box and push apart simultaneous departures (repaired variant)."""
        C, P = self.C, self.P
        d = {}
        for b, x in dep.items():
            if C.locked[b] or b in drop:
                continue
            lo, hi = self._bounds(b, lim, drop)
            d[b] = float(min(max(x, lo), hi)) if lo <= hi else float(math.ceil(C.nat[b]))
        return self._order_fix(d, drop)

    def _qos_repair(self, dep, drop=(), lim_adj=None):
        """QoS 1.2 at the interchange: hold the bus in front of a departure gap longer than scheduled + qos_over_max, or release the bus
        behind it, within each trip's limits (+/-8 min, 7-min layover, departed trips locked). -> repaired {b: x} (may still break it)."""
        C, P = self.C, self.P
        add = float(P.get("qos_over_max") or 0.0)
        if add <= 0:
            return dict(dep)
        x = {b: dep.get(b, float(C.nat[b])) for b in range(1, C.n + 1) if b not in drop}
        x[0], x[C.n + 1] = float(C.S[0]), float(C.S[-1])
        slots = sorted(float(C.S[b]) for b in x)
        sall = sorted(float(v) for v in C.S)
        def ref(a_, c_):                                              # normal scheduled headway across [a_, c_] (a lost slot does not double it)
            inside = [v for v in sall if a_ - EPS <= v <= c_ + EPS]
            return max([inside[i + 1] - inside[i] for i in range(len(inside) - 1)] or [c_ - a_])
        lim = [ref(slots[k], slots[k + 1]) + add for k in range(len(slots) - 1)]
        def box(b):
            if b in (0, C.n + 1) or C.locked[b]:
                return x[b], x[b]
            lo, hi = self._bounds(b, lim_adj, drop)
            return float(lo), float(hi)
        for _ in range(4):
            seq = sorted(x, key=lambda b: (x[b], b))
            changed = False
            for k in range(len(seq) - 2, -1, -1):                       # hold the bus in front
                a, c = seq[k], seq[k + 1]
                if x[c] - x[a] > lim[k] + EPS:
                    lo, hi = box(a)
                    nx = min(hi, float(math.ceil(x[c] - lim[k] - EPS)))
                    if nx > x[a] + EPS:
                        x[a], changed = nx, True
            seq = sorted(x, key=lambda b: (x[b], b))
            for k in range(len(seq) - 1):                               # release the bus behind
                a, c = seq[k], seq[k + 1]
                if x[c] - x[a] > lim[k] + EPS:
                    lo, hi = box(c)
                    nx = max(lo, float(math.floor(x[a] + lim[k] + EPS)))
                    if nx < x[c] - EPS:
                        x[c], changed = nx, True
            if not changed:
                break
        return self._order_fix({b: v for b, v in x.items() if 1 <= b <= C.n and not C.locked[b] and abs(v - C.nat[b]) > 0.49 and b not in drop}, drop)

    def _qos_ok(self, dep, drop=()):
        """Does an UP 1 plan meet QoS 1.2 at the interchange (lost slots judged against the normal headway)?"""
        C, P = self.C, self.P
        add = float(P.get("qos_over_max") or 0.0)
        if add <= 0:
            return True
        x = {b: dep.get(b, float(C.nat[b])) for b in range(1, C.n + 1) if b not in drop}
        x[0], x[C.n + 1] = float(C.S[0]), float(C.S[-1])
        d = sorted(x.values())
        H = float(np.median(np.diff(np.sort(C.S))))
        return all(d[k + 1] - d[k] <= H + add + EPS for k in range(len(d) - 1))

    def generate(self):
        C, P = self.C, self.P
        lateb, focus = self._focus()
        self.lateb, self.focus = lateb, focus
        feas = {"adjust": [], "halfway": {}}
        feas["adjust"].append({"dep": {}, "hw": [], "fam": "No intervention", "reg": False})      # NO ACTION: no trip adjusted, later trips neither
        self._count("No intervention", [])
        q_ = self._order_fix({})                                     # run order: trips ready before the late trip in front of them wait behind it
        feas["adjust"].append({"dep": q_, "hw": [], "fam": "Regulate the later trips only" if not q_ else "Keep the run order (wait behind the late trip)", "reg": True})
        self._count("Regulate the later trips only", [])
        qn = self._queue()
        q0 = self._qos_repair({})
        if q0 and not self._check(q0, []):
            feas["adjust"].append({"dep": q0, "hw": [], "fam": "QoS 1.2 repair (hold / release to the 6-min limit)", "reg": True})
            self._count("QoS 1.2 repair (hold / release to the 6-min limit)", [])
        # one trip at a time
        for b in focus:
            for sh in (-10, -8, -6, -4, -2, 2, 4, 6, 8, 10):
                dep = self._order_fix({b: float(round(qn.get(b, C.nat[b]) + sh))} if P.get("no_overtake", 1) else {b: float(round(C.nat[b] + sh))})
                why = self._check(dep, [])
                self._count("Adjust one trip", why)
                if not why:
                    feas["adjust"].append({"dep": dep, "hw": [], "fam": "Adjust one trip", "reg": True})
        # spread departure timings (raw and repaired)
        for dep in self._spreads(focus):
            why = self._check(dep, [])
            fam = "Spread departure timings" if len(dep) >= 3 else "Adjust several trips (\u00b18 min)"
            self._count(fam, why)
            if not why:
                feas["adjust"].append({"dep": dep, "hw": [], "fam": fam, "reg": True})
            pr = self._project(dep)
            if pr and pr != dep:
                why2 = self._check(pr, [])
                self._count("Full trip + departure regulation", why2)
                if not why2:
                    feas["adjust"].append({"dep": pr, "hw": [], "fam": "Full trip + departure regulation", "reg": True})
            qr = self._qos_repair(pr or dep)
            if qr and qr != (pr or dep):
                why3 = self._check(qr, [])
                self._count("QoS 1.2 repair (hold / release to the 6-min limit)", why3)
                if not why3:
                    feas["adjust"].append({"dep": qr, "hw": [], "fam": "Full trip + departure regulation", "reg": True})
        feas["halfway"] = self._gen_halfway(lateb, focus)
        return feas

    def _gen_halfway(self, lateb, focus):
        C, P = self.C, self.P
        out = {}
        hk = [b for b in lateb if C.late[b - 1] >= P["halfway_min_late"] - EPS and 2 <= b <= C.n - 1 and not C.locked[b]]
        js = [j for j in C.cand_j if C.route_km <= 0 or 100.0 * (C.route_km - C.stop_s[j]) / C.route_km >= P["min_remaining_pct"] - EPS]
        js = [j for j in js if P["min_halfway_km"] - EPS <= C.stop_s[j] <= P["max_mileage_km"] + EPS]
        if len(js) > P["max_stops_tested"]:
            st = math.ceil(len(js) / P["max_stops_tested"])
            js = js[::st]
        self.hk, self.js = hk, js
        combos = [(b,) for b in hk]
        sev = [b for b in hk if C.late[b - 1] >= 20.0 - EPS]
        for x in range(len(sev)):
            for y in range(x + 1, len(sev)):
                if abs(sev[x] - sev[y]) == 1 and not P.get("allow_adjacent_halfway"):
                    k_ = "Two back-to-back trips halfway (not allowed)"
                    self.reject[k_] = self.reject.get(k_, 0) + 1
                    continue
                combos.append((sev[x], sev[y]))
        hreg = bool(P.get("halfway_regulate"))
        hl = self._hw_lim()
        for combo in combos:
            regs = [{}]
            if hreg:
                regs += [self._project(d, combo, hl) for d in self._spreads(focus, combo)[:12]]
                q_in = self._qos_repair({}, combo, hl)                    # QoS 1.2 within the layover the BC wins back
                regs.append(q_in)
                if not self._qos_ok(q_in, combo):
                    regs.append(self._qos_repair({}, combo))              # only if that cannot meet it: up to the +/-8 min limit
                regs = [r_ for k_, r_ in enumerate(regs) if r_ not in regs[:k_]]
            for j in js:
                key = (combo, j)
                lst = []
                starts = []
                for b in combo:
                    slot = C.S[b]
                    own = C.act_arr[b] + (0.0 if P["halfway_skip_layover"] else C.ml) + _off(C, P, j) + P["prep_min"] - C.tau[j]
                    cand = [slot - 5, slot, slot + 5, slot + 10, slot + 12]
                    if self.veh == "own":
                        cand.append(math.ceil(own))
                    starts.append(sorted(set(float(round(x)) for x in cand)))
                for s_idx in range(max(len(s) for s in starts)):
                    hw = [(b, j, starts[k][min(s_idx, len(starts[k]) - 1)]) for k, b in enumerate(combo)]
                    for rg in regs:
                        dep = self._order_fix({b: x for b, x in rg.items() if b not in combo}, combo)
                        why = self._check(dep, hw)
                        fam = ("Halfway one trip + regulate the others" if len(combo) == 1 and dep else
                               "Halfway one trip, others run full trips" if len(combo) == 1 else "Two halfway starts + regulation")
                        self._count(fam, why)
                        if not why:
                            lst.append({"dep": dep, "hw": hw, "fam": fam, "reg": False, "lim": None if (dep and any(abs(x_ - C.nat[b_]) > hl + 0.49 for b_, x_ in dep.items())) else hl})
                            # later trips of a halfway plan run their natural times: the timetable from trip 2
                if lst:
                    out[key] = lst
        return out

    # -------------------------------------------------- even headway over a small window (V13.17)
    def steady_plan(self, step=None, exact=False):
        """Trip adjustment with an EVEN HEADWAY and no big jumps: each departure gap differs from the gap before it by at most `even_step`
        (2) min, from the timetable gap before the first trip to the timetable gap after the next trip; inside that, the lowest waiting
        (least sum of (gap - scheduled gap)^2), then the least holding. Exact, in whole minutes (dynamic programming). When `even_step`
        cannot be met with the trips simulated, the smallest step that can is used (exact=True: that step or nothing). -> plan or None."""
        C, P = self.C, self.P
        self.steady_why = None
        if not self.lateb:
            return None
        ask = int(P.get("even_step", 2) if step is None else step)
        res = None
        for st in ([ask] if exact else range(max(1, ask), 13)):
            res = self._smooth_dp(st)
            if res is not None:
                break
        if res is None:
            self.steady_why = f"no even headway fits the {C.n} trips simulated" + ("" if exact else " with gaps changing by up to 12 min")
            return None
        xs, used = res
        dep = {b: float(xs[b]) for b in range(1, C.n + 1) if not C.locked[b] and abs(xs[b] - C.nat[b]) > 0.49}
        if not dep:
            self.steady_why = "no departure needs to move"
            return None
        why = self._check(dep, [], cap=False)
        if why:
            self.steady_why = "; ".join(why)
            return None
        Si = [int(round(float(v))) for v in C.S]
        gaps = [int(xs[b] - xs[b - 1]) for b in range(1, C.n + 2)]
        moved = sorted(dep)
        first, last = moved[0], moved[-1]
        qn = self._queue()
        holds = {b: xs[b] - float(C.nat[b]) for b in dep}
        over = [b for b in dep if xs[b] > max(float(C.nat[b]), qn.get(b, float(C.nat[b]))) + P["adj_max"] + 0.49]
        lateb = [b for b in self.lateb if 1 <= b <= C.n]
        L0 = lateb[0] if lateb else first
        info = {"mode": "smooth", "step": used, "step_asked": ask, "first": int(first), "last": int(last), "before": int(first - 1), "after": int(last + 1),
                "after_clock": hm(xs[last + 1]), "gaps": gaps, "gaps_from": int(first - 1),                 # gaps[k] = trip k -> trip k+1 (k = 0 is the bus before trip 1)
                "show": [int(g) for g in gaps[first - 1:last + 1]],                                        # the gaps that change: trip first-1 -> ... -> trip last+1
                "gap_min": min(gaps[first - 1:last + 1]), "gap_max": max(gaps[first - 1:last + 1]),
                "late": [{"trip": int(b), "arr_clock": hm(C.act_arr[b]), "earliest_clock": hm(xs[b]), "late": _r(xs[b] - Si[b], 0)} for b in lateb],
                "H": _r(float(np.median(np.diff(np.sort(np.asarray(C.S, float))))), 1), "hold_max": _r(max(holds.values(), default=0.0), 0),
                "release_max": _r(-min(holds.values(), default=0.0), 0), "n_moved": len(dep), "n_over_cap": len(over), "over": [int(b) for b in over],
                "adj_max": P["adj_max"], "hold_limit": P.get("even_hold_max", 20.0), "n_trips": C.n, "from_trip": int(first), "start_trip": int(first - 1),
                "next_trip": C.n + 1, "next_hold": 0, "min_layover": _r(C.ml, 1), "late_trip": int(L0)}
        return {"dep": dep, "hw": [], "fam": "Even headway", "reg": True, "steady": info, "end": None}

    def _smooth_dp(self, step):
        """Whole-minute departures x_1..x_n (bus 0 and n+1 on their timetable times) with |d_k - d_(k-1)| <= step, d = gap - scheduled gap,
        d = 0 before bus 0 and after bus n+1; least sum d^2, then least holding. Departed trips are fixed.
        -> ({b: x}, step) or None."""
        C, P = self.C, self.P
        n = C.n
        Si = [int(round(float(v))) for v in C.S]
        hmax, rel, gmin = float(P.get("even_hold_max", 20.0)), float(P["adj_max"]), int(math.ceil(float(P["min_dep_gap"])))
        box = {0: (Si[0], Si[0]), n + 1: (Si[n + 1], Si[n + 1])}
        for b in range(1, n + 1):
            if C.locked[b]:
                v = int(round(float(C.nat[b])))
                box[b] = (v, v)
            else:
                lo = int(math.ceil(max(float(C.ready[b]), float(C.nat[b]) - rel) - 1e-6))
                hi = int(math.floor(float(C.nat[b]) + hmax + 1e-6))
                if lo > hi:
                    return None
                box[b] = (lo, hi)
        cur = {(Si[0], 0): (0.0, None)}
        hist = []
        for k in range(1, n + 2):
            gs = Si[k] - Si[k - 1]
            lo, hi = box[k]
            new = {}
            for (x, d), (c, _) in cur.items():
                rng = range(d - step, d + step + 1)                       # also next to a departed trip: no jump from the gap it already made
                for d2 in rng:
                    x2 = x + gs + d2
                    if x2 < lo or x2 > hi or gs + d2 < gmin:
                        continue
                    c2 = c + d2 * d2 + 0.01 * abs(x2 - float(C.nat[k]))
                    key = (x2, d2)
                    if key not in new or c2 < new[key][0] - 1e-12:
                        new[key] = (c2, (x, d))
            if not new:
                return None
            hist.append(new)
            cur = new
        fin = [(v[0], key) for key, v in cur.items() if abs(key[1]) <= step]
        if not fin:
            return None
        _, key = min(fin)
        xs = {n + 1: key[0]}
        for k in range(n + 1, 0, -1):
            key = hist[k - 1][key][1]
            xs[k - 1] = key[0]
        return xs, step

    def steady_plan_window(self, side=None):
        """V13.17 (not used by the page): Trip adjustment with an EVEN HEADWAY over a small window: the late trip(s) and `steady_side` (2) duties above and below move, the
        trip before and the trip after the window keep the timetable. Inside, each side of the late trip gets even gaps (least squares,
        whole minutes: they differ by at most 1 min), within +/-adj_max, the 7-min layover and the run order. -> plan or None (steady_why)."""
        C, P = self.C, self.P
        self.steady_why = None
        if not self.lateb:
            return None
        side = int(P.get("steady_side", 2) if side is None else side)
        c_ = self._even_core(side)
        if c_ is None or not c_["dep"]:
            self.steady_why = "no departure can move" if c_ is None else "no departure needs to move"
            return None
        dep = c_["dep"]
        why = self._check(dep, [])
        if why:
            self.steady_why = "; ".join(why)
            return None
        t = {b: float(dep.get(b, C.nat[b])) for b in range(1, C.n + 1)}
        t[0], t[C.n + 1] = float(C.S[0]), float(C.S[-1])
        win = [b for b in range(c_["lo_b"], c_["hi_b"] + 1) if not C.locked[b]]
        a_ = win[0] - 1 if win else 0                                     # the trip before the window (on its own time)
        z_ = win[-1] + 1 if win else C.n + 1                              # the trip after the window (back on the timetable)
        chain = list(range(a_, z_ + 1))
        lateb = set(self.lateb)
        pin = [a_] + [b for b in win if b in lateb and t[b] <= c_["box"].get(b, (t[b], t[b]))[0] + 0.5] + [z_]   # late trips leaving as soon as they may
        pin = sorted(set(pin))
        segs = []
        for i in range(len(pin) - 1):
            f_, to_ = pin[i], pin[i + 1]
            gp = [_r(t[chain[k + 1]] - t[chain[k]], 0) for k in range(chain.index(f_), chain.index(to_))]
            seg = {"from": int(f_), "to": int(to_), "from_clock": hm(t[f_]), "to_clock": hm(t[to_]), "span": _r(t[to_] - t[f_], 0), "n_gaps": len(gp),
                   "exact": _r((t[to_] - t[f_]) / max(1, len(gp)), 1), "gaps": gp, "trips": [int(x) for x in range(f_ + 1, to_)]}
            if to_ in lateb and 1 <= to_ <= C.n:
                seg["to_late"] = {"arr_clock": hm(C.act_arr[to_]), "earliest_clock": hm(t[to_]), "late": _r(t[to_] - C.S[to_], 0)}
            if to_ == z_:
                seg["to_on_time"] = bool(abs(t[z_] - (C.S[z_] if z_ <= C.n + 1 else t[z_])) < 0.5)
            segs.append(seg)
        gaps = [g for sg in segs for g in sg["gaps"]]
        holds = {b: t[b] - float(C.nat[b]) for b in win}
        sch = np.sort(np.asarray(C.S, float))
        info = {"mode": "window", "side": side, "first": int(win[0]), "last": int(win[-1]), "late": sorted(int(b) for b in lateb if b in win),
                "before": int(a_), "after": int(z_), "after_clock": hm(t[z_]), "segments": segs, "gap_min": _r(min(gaps), 0), "gap_max": _r(max(gaps), 0),
                "H": _r(float(np.median(np.diff(sch))), 1), "from_trip": int(win[0]), "start_trip": int(a_), "hold_max": _r(max(holds.values(), default=0.0), 0),
                "release_max": _r(-min(holds.values(), default=0.0), 0), "n_moved": sum(1 for v in holds.values() if abs(v) >= 0.5),
                "n_over_cap": 0, "next_trip": C.n + 1, "next_hold": 0, "min_layover": _r(C.ml, 1)}
        return {"dep": dep, "hw": [], "fam": "Even headway", "reg": True, "steady": info, "end": None}

    def steady_options(self, steps=(1, 2, 3, 4, 5, 6)):
        """The even headway with each largest change between neighbouring gaps: deterministic EWT, largest hold and the gaps (page table)."""
        out, plans = [], []
        for st in steps:
            pl = self.steady_plan(st, exact=True)
            if pl is None:
                out.append({"step": st, "ok": False})
            else:
                plans.append((st, pl))
        self.steady_why = None
        if plans:
            m = self._eval([pl for _, pl in plans])
            for i, (st, pl) in enumerate(plans):
                x = pl["steady"]
                out.append({"step": st, "ok": True, "ewt": _r(float(m["ewt_avg"][i]), 3), "hold_max": x["hold_max"], "n_over_cap": x["n_over_cap"], "first": x["first"],
                            "last": x["last"], "show": x["show"], "gap_min": x["gap_min"], "gap_max": x["gap_max"]})
        return sorted(out, key=lambda o: o["step"])

    # -------------------------------------------------- former one-number even headway (V13.14, V13.16; not used by the page)
    def steady_plan_all(self, h_try=None):
        """Trip adjustment with an EVEN HEADWAY: from trip 2 to the last trip every bus leaves h min after the one before it, give or take
        steady_tol (1) min, in run order; trip 1 (or the last departed trip) stays where it is. h = the smallest whole minute (>= the scheduled
        headway) for which the band lets every bus keep its place: no departure before arrival + the minimum layover, none more than adj_max
        before its natural time. Inside the band each bus leaves as early as it can (least holding). The next trip after the block keeps at
        least h - tol behind the last one. -> plan or None (with self.steady_why)."""
        C, P = self.C, self.P
        self.steady_why = None
        if not self.lateb:
            return None
        tol = max(0.0, float(P.get("steady_tol", 1.0) or 0.0))
        buses = list(range(1, C.n + 1))
        seq = buses if P.get("no_overtake", 1) else sorted(buses, key=lambda b: (C.nat[b], b))
        k0 = max([k for k, b in enumerate(seq) if C.locked[b]], default=0)    # the block starts after trip 1 / the last departed trip
        if k0 >= len(seq) - 1:
            self.steady_why = "every trip has already left"
            return None
        t0 = float(round(C.nat[seq[k0]]))
        sch = np.sort(np.asarray(C.S, float))
        H0 = float(np.median(np.diff(sch)))
        blk = seq[k0 + 1:]
        lo = {b: float(math.ceil(max(float(C.ready[b]), float(C.nat[b]) - P["adj_max"]) - 1e-6)) for b in blk}
        h, bind = max(1, int(math.floor(H0 + 0.5))), None
        for j, b in enumerate(blk, 1):                                     # trip b is bus number j after the start: t0 + j * (h + tol) >= its earliest time
            need = int(math.ceil((lo[b] - t0) / j - tol - 1e-6))
            if need > h or (bind is None and need >= h):
                h, bind = max(h, need), (b, j)
        if h_try is not None:
            h = int(h_try)
        lo_g, hi_g = h - tol, h + tol
        L = [0.0] * len(blk)                                               # latest-needed time: the buses after it must still keep up within h + tol
        for i in range(len(blk) - 1, -1, -1):
            L[i] = lo[blk[i]] if i == len(blk) - 1 else max(lo[blk[i]], L[i + 1] - hi_g)
        x, prev = {}, t0
        for i, b in enumerate(blk):
            v = float(math.ceil(max(prev + lo_g, L[i]) - 1e-6))
            if v - prev > hi_g + 1e-6:                                      # cannot keep up even with the longest gap: h too small
                self.steady_why = f"an even headway of {h} min (gaps {lo_g:g}-{hi_g:g}) cannot get Trip {b} away by its earliest time"
                return None
            x[b], prev = v, v
        dep = {b: v for b, v in x.items() if abs(v - C.nat[b]) > 0.49}
        last = prev
        end = max(float(C.S[-1]), last + lo_g)
        gaps = [x[blk[0]] - t0] + [x[blk[i]] - x[blk[i - 1]] for i in range(1, len(blk))]
        qn = self._queue()
        holds = [v - C.nat[b] for b, v in x.items()]
        over = [v - max(float(C.nat[b]), qn.get(b, float(C.nat[b]))) for b, v in x.items()]
        cap = float(P.get("steady_hold_max") or 0.0)
        info = {"h": h, "tol": tol, "gap_min": _r(min(gaps), 0), "gap_max": _r(max(gaps), 0), "H": _r(H0, 1), "from_trip": int(blk[0]), "start_trip": int(seq[k0]),
                "start": _r(t0, 1), "start_clock": hm(t0), "order": [int(b) for b in blk], "hold_max": _r(max(holds, default=0.0), 0),
                "n_over_cap": sum(1 for v in over if v > P["adj_max"] + 0.49),
                "next_trip": C.n + 1, "next_hold": _r(end - float(C.S[-1]), 0), "next_clock": hm(end), "next_sch": hm(C.S[-1]),
                # WHY this number: the trip that sets it, and every trip's earliest time, so the page can test any other headway
                "bind": None if bind is None else {"trip": int(bind[0]), "k": int(bind[1]), "earliest": _r(lo[bind[0]], 1), "earliest_clock": hm(lo[bind[0]]),
                                                   "arr_clock": hm(C.act_arr[bind[0]]), "late": _r(C.late[bind[0] - 1], 1), "min_layover": _r(C.ml, 1),
                                                   "span": _r(lo[bind[0]] - t0, 1), "exact": _r((lo[bind[0]] - t0) / bind[1], 2)},
                "need": [[int(b), j, _r(lo[b], 2), _r(float(C.nat[b]), 2), hm(C.act_arr[b])] for j, b in enumerate(blk, 1)],
                "next_sch_min": _r(float(C.S[-1]), 2), "min_layover": _r(C.ml, 1)}
        if h_try is not None:
            return {"dep": dep, "steady": info, "end": end}
        qmax = float(P.get("qos_over_max") or 0.0)
        if qmax > 0 and max(gaps) > H0 + qmax + EPS:
            self.steady_why = f"an even headway of {h} min needs gaps of up to {max(gaps):g} min, over QoS 1.2 ({H0:g} + {qmax:g})"
            return None
        if cap > 0 and info["hold_max"] > cap + 0.49:
            self.steady_why = f"an even headway of {h} min needs a hold of +{info['hold_max']:.0f} min, over the {cap:g}-min cap"
            return None
        why = self._check(dep, [], cap=False, end=end)
        if why:
            self.steady_why = "; ".join(why)
            return None
        return {"dep": dep, "hw": [], "fam": "Even headway", "reg": True, "steady": info, "end": end if end > float(C.S[-1]) + 0.49 else None}

    # -------------------------------------------------- local search (coordinate descent, batched)
    def even_plan(self):
        """Trip adjustment, evened out: departure times that make every interchange gap as close to its scheduled headway as the limits allow
        (least squares, solved bus by bus). The late bus leaves when it is ready, the buses ahead are held a little more each, and the buses
        behind are spaced out too - within +/-adj_max, the 7-min minimum layover, no overtaking, departed trips untouched. -> plan or None."""
        c_ = self._even_core(int(self.P.get("even_side", 6)))
        if c_ is None:
            return None
        dep = c_["dep"]
        if not dep or self._check(dep, []) or not self._qos_ok(dep):
            return None
        return {"dep": dep, "hw": [], "fam": "Even out the gaps (least squares)", "reg": True}

    def _even_core(self, s_):
        """Least squares on the trips from s_ duties above the first late trip to s_ below the last one: each gap as close to its scheduled
        headway as the limits allow, whole minutes. -> {dep, lo_b, hi_b, box} or None."""
        C, P = self.C, self.P
        if not self.lateb:
            return None
        lo_b, hi_b = max(1, min(self.lateb) - s_), min(C.n, max(self.lateb) + s_)
        seq = list(range(C.n + 2)) if P.get("no_overtake", 1) else sorted(range(C.n + 2), key=lambda b: (C.nat[b], b))
        slots = sorted(float(v) for v in C.S)
        g = [slots[k + 1] - slots[k] for k in range(len(slots) - 1)]          # the k-th scheduled gap
        qn = self._queue()
        x = [qn.get(b, float(C.nat[b])) for b in seq]                       # run order: start from the queued times
        x[0], x[-1] = float(C.S[seq[0]]), float(C.S[seq[-1]])
        mov, box = [], {}
        for k in range(1, len(seq) - 1):
            b = seq[k]
            if lo_b <= b <= hi_b and not C.locked[b]:
                lo, hi = self._bounds(b)
                if lo <= hi:
                    mov.append(k)
                    box[k] = (float(lo), float(hi))
        if not mov:
            return None
        gap = float(P["min_dep_gap"])
        for _ in range(600):
            moved = 0.0
            for k in mov:
                lo, hi = box[k]
                want = ((x[k - 1] + g[k - 1]) + (x[k + 1] - g[k])) / 2.0
                lo2, hi2 = max(lo, x[k - 1] + gap), min(hi, x[k + 1] - gap)
                if lo2 > hi2:
                    lo2 = hi2 = min(max(want, lo), hi)
                nx = min(max(want, lo2), hi2)
                moved, x[k] = max(moved, abs(nx - x[k])), nx
            if moved < 1e-4:
                break
        dep = {}
        for k in mov:
            b, lo, hi = seq[k], box[k][0], box[k][1]
            v = float(min(max(round(x[k]), lo), hi))
            if abs(v - C.nat[b]) > 0.49:
                dep[b] = v
        dep = self._order_fix(dep)
        return {"dep": dep, "lo_b": lo_b, "hi_b": hi_b, "box": {seq[k]: box[k] for k in mov}}

    def refine(self, plan, objective="cost", sweeps=3):
        C, P = self.C, self.P
        hwb = {h[0] for h in plan["hw"]}
        vars_ = [b for b in self.focus if not C.locked[b] and b not in hwb]
        if plan["hw"] and not P.get("halfway_regulate"):
            vars_ = []                                                  # halfway only: just the halfway start time is tuned
        blim = plan.get("lim") if plan["hw"] else None
        cur = {"dep": dict(plan["dep"]), "hw": list(plan["hw"]), "fam": plan.get("fam"), "reg": plan.get("reg", True), "lim": plan.get("lim")}
        best = self._eval([cur])[objective][0]
        for _ in range(sweeps):
            improved = False
            for b in vars_:
                lo, hi = self._bounds(b, blim, tuple(hwb))
                if lo > hi:
                    continue
                trial = []
                for x in range(lo, hi + 1):
                    d = dict(cur["dep"]); d[b] = float(x)
                    if abs(x - C.nat[b]) < 0.49:
                        d.pop(b, None)
                    if self._check(d, cur["hw"]):
                        continue
                    trial.append({"dep": d, "hw": cur["hw"], "fam": cur["fam"], "reg": cur["reg"], "lim": cur["lim"]})
                if not trial:
                    continue
                m = self._eval(trial)[objective]
                k = int(np.argmin(m))
                if m[k] < best - 1e-6:
                    best, cur, improved = float(m[k]), trial[k], True
            for q, (b, j, st) in enumerate(list(cur["hw"])):
                slot = C.S[b]
                trial = []
                for x in range(int(slot - P["start_early_max"]), int(slot + P["start_late_max"]) + 1):
                    hw = list(cur["hw"]); hw[q] = (b, j, float(x))
                    if self._check(cur["dep"], hw):
                        continue
                    trial.append({"dep": cur["dep"], "hw": hw, "fam": cur["fam"], "reg": cur["reg"], "lim": cur["lim"]})
                if trial:
                    m = self._eval(trial)[objective]
                    k = int(np.argmin(m))
                    if m[k] < best - 1e-6:
                        best, cur, improved = float(m[k]), trial[k], True
            if not improved:
                break
        return cur, best

    # -------------------------------------------------- describe a plan
    def _hr_where(self, L, p):
        """Where the worst hourly headway is: '<stop> · <balance trip>'."""
        C = self.C
        try:
            if L % 2 == 0:
                nm = C.names[C.pts_up[p]]
            else:
                nm = C.dn_lbl[p][0]
                if nm is None:
                    nm = "Far terminal" if p == 0 else (C.names[0] if p == len(C.frac_dn) - 1 else f"{100.0 * C.frac_dn[p]:.0f}% along the return trip")
        except Exception:
            nm = ""
        return f"{nm} \u00b7 {leg_name(L)}" if nm else leg_name(L)

    def describe(self, plan, det, mc, label, key, disp_j=None):
        C, P = self.C, self.P
        hwb = {h[0]: h for h in plan["hw"]}
        D = det["D"][0]
        rows = []
        order = sorted([b for b in range(1, C.n + 1) if b not in hwb], key=lambda b: (D[b], b))
        for b in range(1, C.n + 1):
            if b in hwb:
                _, j, st = hwb[b]
                leave = C.act_arr[b] + (0.0 if P["halfway_skip_layover"] else C.ml)
                rows.append({"n": b, "type": "halfway", "stop": C.names[j], "code": C.codes[j], "j": j, "start": _r(st + C.tau[j], 1),
                             "start_clock": hm(st + C.tau[j]), "slot_clock": hm(C.S[b] + C.tau[j]), "shift": _r(st - C.S[b], 1), "leave": _r(leave, 1),
                             "leave_clock": hm(leave), "km_lost": _r(C.stop_s[j], 2), "late": C.late[b - 1], "sch": hm(C.S[b]),
                             "off_min": _r(_off(C, P, j), 1), "off_routed": j in C.offsvc, "arrive": _r(leave + _off(C, P, j), 1), "arrive_clock": hm(leave + _off(C, P, j)),
                             "act_arr": _r(C.act_arr[b], 1), "nat_dep": _r(C.nat[b], 1), "tau_j": _r(C.tau[j], 1)})
                continue
            x = float(D[b])
            qd = self._queue(tuple(hwb))
            rows.append({"n": b, "type": "locked" if C.locked[b] else "full", "dep": _r(x, 1), "dep_clock": hm(x), "sch": hm(C.S[b]),
                         "wait": _r(max(0.0, qd.get(b, float(C.nat[b])) - float(C.nat[b])), 1),      # run order: minutes it must wait behind a late trip in front
                         "shift": _r(x - C.nat[b], 1), "vs_sch": _r(x - C.S[b], 1), "nat_clock": hm(C.nat[b]), "late": C.late[b - 1],
                         "layover": _r(x - C.act_arr[b], 1), "rank": order.index(b)})
        # order swaps at the interchange
        full = [r for r in rows if r["type"] != "halfway"]
        for r in full:
            ahead = [q["n"] for q in full if q["n"] > r["n"] and q["dep"] < r["dep"] - 0.01]
            r["overtaken_by"] = ahead
        legs = det["legs"]
        chain = []
        for L, lg in enumerate(legs):
            g = lg["dep_g"][0]
            o = lg["dep_order"][0]
            pat = [(int(o[k]), _r(g[k], 1)) for k in range(len(o)) if not math.isnan(g[k])]
            chain.append({"leg": leg_name(L), "max": _r(det["leg_max"][0, L], 1), "wmax": _r(det["leg_wmax"][0, L], 1),
                          "dep_hw": [x for x in pat if 1 <= x[0] <= C.n or True]})
        for L, lg in enumerate(legs):                           # per balance trip: average EWT of its points
            e = lg["ewt"][0]
            v = e[~np.isnan(e)]
            chain[L]["ewt_avg"] = _r(float(v.mean()), 2) if len(v) else None
            chain[L]["ewt_max"] = _r(float(v.max()), 2) if len(v) else None
        ic = chain[0]["dep_hw"]
        down, at_j = None, None
        if disp_j is not None and disp_j in C.pidx:
            T_, o_, g_ = legs[0]["pts"][C.pidx[disp_j]]
            down = [(int(o_[k]), _r(g_[k], 1)) for k in range(len(o_)) if not math.isnan(g_[k])]
            at_j = [(int(o_[k]), _r(T_[k], 2)) for k in range(len(o_)) if not math.isnan(T_[k])]
        up1 = {}
        for (T_, o_, g_) in legs[0]["pts"]:
            for k in range(len(o_)):
                up1.setdefault(int(o_[k]), []).append(None if math.isnan(T_[k]) else _r(T_[k], 2))
        term_adj = []
        for (L, nat, reg) in det.get("term") or []:
            where = C.names[-1] if L % 2 == 1 else C.names[0]
            for b in range(1, C.n + 1):
                sh = float(reg[b] - nat[b])
                if abs(sh) >= 0.5:
                    term_adj.append({"n": b, "L": L, "leg": leg_name(L), "where": where, "nat_clock": hm(nat[b]), "dep_clock": hm(reg[b]), "dep": _r(float(reg[b]), 1),
                                     "shift": _r(sh, 1), "halfway_bus": b in hwb})
        qos = self.qos_report(det)
        ewt_pts = self.ewt_points(det)
        valid = [q for q in ewt_pts if q["ewt"] is not None]
        worst = max(valid, key=lambda q: q["ewt"]) if valid else None
        wins = [q["win"] for q in ewt_pts if q.get("win")]
        for c_ in chain:
            ws = [q["win"] for q in ewt_pts if q.get("win") and q["leg"] == c_["leg"]]
            c_["win"] = {"avg": _r(sum(w["avg"] for w in ws) / len(ws), 1), "max": _r(max(w["max"] for w in ws), 1), "min": _r(min(w["min"] for w in ws), 1)} if ws else None
        win_all = {"avg": _r(sum(w["avg"] for w in wins) / len(wins), 1), "max": _r(max(w["max"] for w in wins), 1), "min": _r(min(w["min"] for w in wins), 1),
                   "window_min": float(P.get("win_min") or 30.0)} if wins else None
        ewt_blk = {"avg": _r(det["ewt_avg"][0], 3), "max": _r(det["ewt_max"][0], 2), "n_points": len(valid), "n_points_all": len(ewt_pts),
                   "worst": {"label": worst["label"], "leg": worst["leg"], "ewt": worst["ewt"]} if worst else None, "points": ewt_pts}
        out = {"key": key, "label": label, "fam": plan.get("fam"), "rows": rows, "chain": chain, "ewt": ewt_blk, "term_adj": term_adj, "qos": qos, "win": win_all,
               "later_adjusted": bool(plan.get("reg", True) and (self.P.get("term_reg") or plan.get("steady"))), "steady": plan.get("steady"),
               "det": {"hr_hw": _r(float(det["hr_hw"][0]), 1) if np.isfinite(det["hr_hw"][0]) else None, "hr_buses": int(det["hr_n"][0]) + 1 if np.isfinite(det["hr_hw"][0]) else None,
                       "hr_where": self._hr_where(int(det["hr_L"][0]), int(det["hr_p"][0])) if np.isfinite(det["hr_hw"][0]) else None, "max": _r(det["max"][0]), "ic_max": _r(det["ic_max"][0]), "wmax": _r(det["wmax"][0]), "rms": _r(det["rms"][0], 2),
                       "bunch_pct": _r(100 * det["bunch"][0], 0), "rec": _r(det["rec"][0], 0), "recovered": bool(not det["unrec"][0]), "min_hw": _r(float(np.nanmin(det["leg_min"][0])) if "leg_min" in det else None, 1), "km": _r(det["km"][0], 2), "adj": _r(det["adj"][0], 0),
                       "bc_max": _r(det["bc_max"][0], 1), "bc_sum": _r(det["bc_sum"][0], 1), "cost": _r(det["cost"][0], 1),
                       "bc": [_r(x, 1) for x in det["bc"][0]], "up1_late": [_r(x, 1) for x in det["up1_late"][0]]},
               "ic_hw": ic, "down_hw": down, "at_j": at_j, "up1_times": {str(k): v for k, v in up1.items()}, "n_adjusted": sum(1 for r in rows if r["type"] == "full" and abs(r["shift"]) >= 0.5),
               "n_halfway": len(hwb), "n_full": sum(1 for r in rows if r["type"] != "halfway")}
        if mc is not None:
            out["mc"] = {"n": int(len(mc["max"])), "hr_hw_p50": _pct(mc["hr_hw"][np.isfinite(mc["hr_hw"])], 50) if np.isfinite(mc["hr_hw"]).any() else None,
                         "hr_hw_p85": _pct(mc["hr_hw"][np.isfinite(mc["hr_hw"])], 85) if np.isfinite(mc["hr_hw"]).any() else None, "max_mean": _r(mc["max"].mean()), "max_p50": _pct(mc["max"], 50), "max_p85": _pct(mc["max"], 85), "max_p90": _pct(mc["max"], 90),
                         "wmax_p50": _pct(mc["wmax"], 50), "wmax_p85": _pct(mc["wmax"], 85),
                         "rec_p50": _pct(mc["rec"], 50), "rec_p85": _pct(mc["rec"], 85), "p_recovered": _r(100.0 * (~mc["unrec"]).mean(), 0), "p_bunch": _r(100.0 * mc["any_bunch"].mean(), 0),
                         "bc_p50": _pct(mc["bc_max"], 50), "bc_p85": _pct(mc["bc_max"], 85), "km": _r(det["km"][0], 2),
                         "cost_mean": _r(mc["cost"].mean(), 1),
                         "ewt_mean": _r(float(mc["ewt_avg"].mean()), 3), "ewt_p50": _r(float(np.percentile(mc["ewt_avg"], 50)), 3),
                         "ewt_p85": _r(float(np.percentile(mc["ewt_avg"], 85)), 3), "ewt_p90": _r(float(np.percentile(mc["ewt_avg"], 90)), 3),
                         "hist": _hist(mc["max"])}
        return out

    def qos_report(self, det):
        """QoS 1.2 for one plan: every departure (UP 1 at the interchange, every later terminal) against scheduled headway + qos_over_max."""
        C, P = self.C, self.P
        add = float(P.get("qos_over_max") or 0.0)
        name = lambda b: "the bus before" if b == 0 else ("the next bus" if b == C.n + 1 else f"Trip {b}")
        br = []
        for q in det.get("qos_legs") or []:
            g, sg, o, ids = q["g"][0], q["sg"], q["order"][0], q["ids"]
            where = C.names[0] if q["L"] % 2 == 0 else C.names[-1]
            for k in range(len(g)):
                if g[k] - sg[k] > add + EPS:
                    br.append({"leg": leg_name(q["L"]), "where": where, "headway": _r(float(g[k]), 1), "scheduled": _r(float(sg[k]), 1),
                               "over": _r(float(g[k] - sg[k]), 1), "between": f"{name(int(ids[o[k]]))} and {name(int(ids[o[k + 1]]))}"})
        br.sort(key=lambda x: -x["over"])
        mx = float(det["qos_over"][0]) if "qos_over" in det else 0.0
        return {"rule": f"QoS 1.2: a departure headway may be at most {add:g} min longer than scheduled", "limit_add": add, "ok": not br,
                "n_breaches": len(br), "max_over": _r(mx, 1), "breaches": br[:8]}

    # -------------------------------------------------- LEVEL 1 table: EWT at every evaluation point of every balance trip (deterministic run)
    def ewt_points(self, det):
        C = self.C
        out = []
        for L, lg in enumerate(det["legs"]):
            up = L % 2 == 0
            sch_h = [float(x) for x in lg["sch_h"]]
            for p, (T_, o_, g_) in enumerate(lg["pts"]):
                if not (C.ewt_mask_up if up else C.ewt_mask_dn)[p]:
                    continue
                if up:
                    j = C.pts_up[p]
                    nm, cd, pct = C.names[j], C.codes[j], 100.0 * (C.stop_s[j] / C.route_km if C.route_km else j / max(1, C.n_st - 1))
                    kind = "dep" if p == 0 else ("arr" if p == len(C.pts_up) - 1 else "")
                else:
                    f = C.frac_dn[p]
                    nm, cd = C.dn_lbl[p]
                    pct = 100.0 * f
                    if nm is None:
                        nm = "Far terminal" if p == 0 else (C.names[0] if p == len(C.frac_dn) - 1 else f"{pct:.0f}% along the return trip")
                    kind = "dep" if p == 0 else ("arr" if p == len(C.frac_dn) - 1 else "")
                h = [(int(o_[k]), float(g_[k])) for k in range(len(o_)) if not math.isnan(g_[k])]
                prt = ewt_parts([x[1] for x in h], sch_h)
                wn = win_stats([x[1] for x in h], float(self.P.get("win_min") or 30.0), C.H)
                e = lg["ewt"][0, p]
                out.append({"L": L, "leg": leg_name(L), "p": p, "label": nm + (" (dep.)" if kind == "dep" else " (arr.)" if kind == "arr" else ""), "code": cd,
                            "pct": _r(pct, 0), "ewt": None if math.isnan(e) else _r(float(e), 3),
                            "awt": _r(prt["awt"], 3) if prt else None, "swt": _r(prt["swt"], 3) if prt else None, "n_hw": len(h),
                            "sum_h": _r(prt["sum_h"], 2) if prt else None, "sum_h2": _r(prt["sum_h2"], 1) if prt else None,
                            "sum_hs": _r(prt["sum_hs"], 2) if prt else None, "sum_hs2": _r(prt["sum_hs2"], 1) if prt else None,
                            "h": [[b, _r(g, 2)] for b, g in h], "hs": [_r(x, 2) for x in sch_h], "win": wn})
        return out

    # -------------------------------------------------- the whole run
    def run(self):
        C, P = self.C, self.P
        feas = self.generate()
        self.needs_standby = False
        if not feas["halfway"] and self.veh == "own" and self.lateb and self.hk:
            self.veh = "standby"                       # the late bus cannot reach any stop in time: a spare bus is the realistic halfway option
            feas["halfway"] = self._gen_halfway(self.lateb, self.focus)
            if feas["halfway"]:
                self.needs_standby = True
            else:
                self.veh = "own"
        if not self.lateb:
            return {"ok": True, "model": MODEL_VERSION, "decision": "none", "full_choice": "none", "benefit": {}, "message": "No trip would leave late: nothing to adjust or deploy.",
                    "plans": {}, "instructions": [], "why": ["No trip would leave the interchange late, so the schedule is kept as it is."], "generated": self._gen_info(), "H": C.H}
        # ---- evaluate every feasible plan (deterministic)
        adj = feas["adjust"]
        m0 = self._eval(adj[:1])                                       # no action: nothing adjusted on any trip
        ma = self._eval(adj[1:])
        base_cost = float(m0["cost"][0])
        base_down = float(np.nanmax(m0["leg_max"][0, 1:])) if C.NL > 1 else float(m0["max"][0])
        adj[0]["_cost"] = base_cost
        keep = []
        for k, pl in enumerate(adj[1:]):
            pl["_cost"] = float(ma["cost"][k])
            dn = float(np.nanmax(ma["leg_max"][k, 1:])) if C.NL > 1 else float(ma["max"][k])
            if dn > base_down + 2.0 and pl["_cost"] >= base_cost * 0.97:
                self.reject["Worse downstream headway without enough benefit"] = self.reject.get("Worse downstream headway without enough benefit", 0) + 1
                continue
            keep.append(pl)
        seeds_a = sorted(keep, key=lambda p: p["_cost"])[:4]
        hbest = []
        for key, lst in feas["halfway"].items():
            mh = self._eval(lst)
            for k, pl in enumerate(lst):
                pl["_cost"] = float(mh["cost"][k])
            dn = np.nanmax(mh["leg_max"][:, 1:], 1) if C.NL > 1 else mh["max"]
            ok = [pl for k, pl in enumerate(lst) if not (dn[k] > base_down + 2.0 and pl["_cost"] >= base_cost * 0.97)]
            nrej = len(lst) - len(ok)
            if nrej:
                self.reject["Worse downstream headway without enough benefit"] = self.reject.get("Worse downstream headway without enough benefit", 0) + nrej
            if ok:
                hbest.append(min(ok, key=lambda p: p["_cost"]))
        self.feasible = len(keep) + sum(len(v) for v in feas["halfway"].values()) - sum(0 for _ in [])
        # ---- refine the best seeds
        none_plan = adj[0]
        best_a, ca = None, 1e18
        for s in seeds_a + [adj[1]]:
            r, c = self.refine(s)
            if c < ca:
                best_a, ca = r, c
        # ---- Trip adjustment evened out: used when it is at least as good for passengers (EWT) and leaves no longer gap
        self.even_used = False
        if P.get("adjust_even", 1) and best_a is not None:
            ev = self.even_plan()
            if ev is not None:
                me, mb = self._eval([ev]), self._eval([best_a])
                if me["ewt_avg"][0] <= mb["ewt_avg"][0] + 0.02 + EPS and me["max"][0] <= mb["max"][0] + 0.5 + EPS:
                    best_a, ca, self.even_used = ev, float(me["cost"][0]), True
        # ---- V13.14 Trip adjustment = ONE steady headway (easy to explain), unless the controller chose the least-waiting style
        # (always worked out, so the page can show what one steady headway would cost against the least-waiting plan)
        self.steady_used, self.steady_alt, self.lw_qos, self.lw_ewt = False, None, None, None
        st_ = self.steady_plan() if best_a is not None else None
        if st_ is not None:
            ms, ml_ = self._eval([st_]), self._eval([best_a])
            self.lw_ewt = float(ml_["ewt_avg"][0])
            self.steady_alt = dict(st_["steady"], ewt=_r(float(ms["ewt_avg"][0]), 3), max=_r(float(ms["max"][0]), 0), least_wait_ewt=_r(self.lw_ewt, 3),
                                   least_wait_max=_r(float(ml_["max"][0]), 0), options=self.steady_options())
            if str(P.get("adjust_style", "least_wait")) == "steady":
                self.lw_qos = self.qos_report(self._eval_keep(best_a))     # what the limits allow without one steady headway (decision reference)
                best_a, ca, self.steady_used = st_, float(ms["cost"][0]), True
        best_h, ch = None, 1e18
        for s in sorted(hbest, key=lambda p: p["_cost"])[:4]:
            r, c = self.refine(s)
            if c < ch:
                best_h, ch = r, c
        local, _ = self.refine(dict(none_plan, reg=False, fam="Next-departure-only fix"), objective="local", sweeps=2)   # UP 1 only, later trips not adjusted
        # ---- stress test
        rng = np.random.default_rng(int(P["seed"]))
        sims = int(max(50, min(5000, P["sims"])))
        nz = C.noise(sims, rng)
        res = {}
        cand = [("none", none_plan, "No action"), ("adjust", best_a, "Full trip + adjustment")]
        if best_h is not None:
            cand.append(("halfway", best_h, "Halfway + adjustment" if P.get("halfway_regulate") else "Halfway (other trips unchanged)"))
        cand.append(("local", local, "Next-departure-only fix"))
        disp_j = best_h["hw"][0][1] if best_h is not None else min(C.pts_up, key=lambda j: abs(j - (C.n_st - 1) / 2))
        self.disp_j = disp_j
        for key, pl, label in cand:
            det = self._eval_keep(pl)
            mc = self._eval_mc(pl, nz, sims)
            res[key] = self.describe(pl, det, mc, label, key, disp_j)
            res[key]["_mc"] = mc
        dec = self.decide(res)
        return self.package(res, dec, sims)

    def _eval_keep(self, pl):
        C = self.C
        D = np.tile(C.nat, (1, 1)).astype(float)
        for b, x in pl["dep"].items():
            D[0, b] = x
        hw = [(b, j, np.array([st])) for (b, j, st) in pl["hw"]] or None
        m = C.run(D, hw, None, keep=True, reg=pl.get("reg", True), end=None if pl.get("end") is None else np.array([float(pl["end"])]), steady=bool(pl.get("steady")))
        km = np.array([sum(C.stop_s[j] for (_, j, _) in pl["hw"])])
        hwb = {h[0] for h in pl["hw"]}
        adj = np.array([sum(abs(x - C.nat[b]) for b, x in pl["dep"].items() if b not in hwb)])
        m["km"], m["adj"] = km, adj
        m["cost"] = _cost(m, self.W, C.H, km, adj, self.P["w_ic_short"], self.P["w_qos"])
        return m

    def _eval_mc(self, pl, nz, sims):
        C, P = self.C, self.P
        D = np.tile(C.nat, (sims, 1)).astype(float)
        for b, x in pl["dep"].items():
            D[:, b] = x
        hw = None
        if pl["hw"]:
            hw = []
            for (b, j, st) in pl["hw"]:
                s = np.full(sims, float(st))
                if self.veh == "own":
                    leave = C.act_arr[b] + nz["eta"][:, b] + (0.0 if P["halfway_skip_layover"] else C.ml)
                    arrive = leave + _off(C, P, j) * nz["off"] + P["prep_min"]
                    s = np.maximum(s, arrive - C.tau[j])
                hw.append((b, j, s))
        m = C.run(D, hw, nz, reg=pl.get("reg", True), end=None if pl.get("end") is None else np.full(sims, float(pl["end"])), steady=bool(pl.get("steady")))
        km = np.full(sims, sum(C.stop_s[j] for (_, j, _) in pl["hw"]))
        hwb = {h[0] for h in pl["hw"]}
        adj = np.full(sims, sum(abs(x - C.nat[b]) for b, x in pl["dep"].items() if b not in hwb))
        m["km"] = km
        m["cost"] = _cost(m, self.W, C.H, km, adj, self.P["w_ic_short"], self.P["w_qos"])
        return m

    # -------------------------------------------------- the decision: EWT over the Balance Trips horizon
    def decide(self, res):
        """A = CONTINUE FULL TRIP (no action, or + departure adjustment when that lowers the Average EWT) vs B = HALFWAY + necessary adjustment.
        Compared on Average EWT across every evaluation point of the N balance trips. The delay is an input only: no delay threshold decides."""
        C, P = self.C, self.P
        N, A, Hw = res["none"], res["adjust"], res.get("halfway")
        delay = max(C.late) if C.late else 0.0
        e_n, e_a = N["ewt"]["avg"], A["ewt"]["avg"]
        a_gain = (e_n or 0.0) - (e_a or 0.0)
        a_useful = a_gain >= P["ewt_adjust_min"] - EPS
        q_n, q_a = N.get("qos") or {}, A.get("qos") or {}
        st_over = bool((A.get("steady") or {}).get("n_over_cap"))                # one steady headway that holds buses past the +/-adj_max limit:
        a_qos = (not st_over) and (not q_n.get("ok", True)) and (q_a.get("ok") or (q_a.get("max_over") or 0) < (q_n.get("max_over") or 0) - EPS)   # QoS 1.2 needs the adjustment
        # (it is chosen on EWT only, and halfway's QoS 1.2 test is measured against what the limits allow - the least-waiting plan)
        a_any = A["n_adjusted"] > 0 or bool(A.get("term_adj"))
        full = "adjust" if (a_any and (a_useful or a_qos)) else "none"
        F = res[full]
        e_c = F["ewt"]["avg"] or 0.0
        h_ok, benefit, tests = False, {}, []
        if Hw is not None:
            e_h = Hw["ewt"]["avg"] or 0.0
            diff = e_c - e_h                                              # + = halfway lowers the Average EWT
            pct = 100.0 * diff / e_c if e_c > 0.005 else (0.0 if abs(diff) < 1e-9 else (100.0 if diff > 0 else -100.0))
            km = Hw["det"]["km"] or 0.0
            p85_c, p85_h = F["mc"]["ewt_p85"], Hw["mc"]["ewt_p85"]
            fm, hm_ = F["mc"], Hw["mc"]
            benefit = {"ewt": _r(diff, 3), "ewt_pct": _r(pct, 1), "ewt_p85": _r((p85_c or 0) - (p85_h or 0), 3), "ewt_per_km": _r(diff / max(km, 0.1), 3) if km else None,
                       "max_p85": _r(fm["max_p85"] - hm_["max_p85"]), "rec_p50": _r(fm["rec_p50"] - hm_["rec_p50"]), "bunch_pp": _r(fm["p_bunch"] - hm_["p_bunch"]),
                       "bc_p85": _r(fm["bc_p85"] - hm_["bc_p85"]), "km": Hw["det"]["km"]}
            pct_need = P["ewt_gain_pct"] * (0.5 if self.mode == "headway" else 1.0)
            tests = [
                {"test": f"Average EWT lower by \u2265 {P['ewt_gain_min']:g} min", "value": f"{diff:+.2f} min", "pass": diff >= P["ewt_gain_min"] - EPS},
                {"test": f"Average EWT lower by \u2265 {pct_need:g}%", "value": f"{pct:+.1f}%", "pass": pct >= pct_need - EPS},
                {"test": "Stress test does not reverse it (P85 of Average EWT)", "value": f"{p85_c:.2f} \u2192 {p85_h:.2f} min", "pass": (p85_h or 0) <= (p85_c or 0) + EPS},
            ]
            if self.mode == "mileage":
                tests.append({"test": f"Worth the mileage: \u2265 {P['ewt_gain_per_km']:g} min EWT per km not operated", "value": f"{diff / max(km, 0.1):.3f} min/km",
                              "pass": diff / max(km, 0.1) >= P["ewt_gain_per_km"] - EPS})
            q_f, q_h = F.get("qos") or {}, Hw.get("qos") or {}
            if full == "adjust" and st_over and self.lw_qos is not None:
                q_f = self.lw_qos
            tests.append({"test": f"QoS 1.2 not worse (departure headway \u2264 scheduled + {P['qos_over_max']:g} min)",
                          "value": ("met" if q_h.get("ok") else f"{q_h.get('n_breaches', 0)} over the limit, worst +{q_h.get('max_over')} min"),
                          "pass": bool(q_h.get("ok")) or (q_h.get("max_over") or 0) <= (q_f.get("max_over") or 0) + EPS})
            h_ok = all(t["pass"] for t in tests)
        prio = {"headway": "Headway priority (EWT gain threshold halved)", "mileage": "Mileage priority (EWT gain must also be worth the km lost)"}.get(self.mode, "Balanced")
        rule = (f"{prio}: Continue full trip vs Halfway compared on Average EWT across {self._npts(F)} evaluation points over {horizon_text(C.NL)}. "
                f"Halfway only if it lowers the Average EWT by \u2265 {P['ewt_gain_min']:g} min and \u2265 {P['ewt_gain_pct'] * (0.5 if self.mode == 'headway' else 1.0):g}% and the stress test agrees. "
                f"Delay ({delay:.0f} min) is an input to the simulation, not a decision threshold.")
        choice = "halfway" if (Hw is not None and h_ok) else full
        return {"choice": choice, "full_choice": full, "halfway_ok": h_ok, "benefit": benefit, "rule": rule, "delay": delay, "adjust_useful": a_useful, "adjust_for_qos": bool(a_qos),
                "adjust_unacceptable": bool(F["mc"]["max_p85"] >= max(30.0, 2.0 * C.H) - EPS), "tests": tests}

    @staticmethod
    def _npts(pl):
        return (pl.get("ewt") or {}).get("n_points", 0)

    # -------------------------------------------------- output for the page
    def package(self, res, dec, sims):
        C, P = self.C, self.P
        ch = res[dec["choice"]]
        instr = []
        for r in sorted([r for r in ch["rows"] if r["n"] in self.focus or r["type"] == "halfway" or abs(r.get("shift") or 0) >= 0.5],
                        key=lambda r: (r.get("start") if r["type"] == "halfway" else r["dep"])):
            if r["type"] == "halfway":
                instr.append({"n": r["n"], "kind": "halfway", "time": r["start_clock"],
                              "text": f"Trip {r['n']} \u2014 Start halfway at {r['stop']} ({r['code']}) | {r['start_clock']}",
                              "sub": (f"Leave {C.names[0]} off-service {r['leave_clock']} (no interchange layover), about {r['off_min']:.0f} min {'by road' if r['off_routed'] else '(estimated)'} \u00b7 " if self.veh == "own" else "Standby bus \u00b7 ")
                              + f"slot {r['slot_clock']} \u00b7 {r['km_lost']:.1f} km not operated"})
                continue
            if r["type"] == "locked":
                instr.append({"n": r["n"], "kind": "locked", "time": r["dep_clock"], "text": f"Trip {r['n']} \u2014 Departed {r['dep_clock']} (locked)", "sub": ""})
                continue
            sh = r["shift"]
            act = "Hold %d min | " % round(sh) if sh >= 0.5 else ("Release %d min early | " % round(-sh) if sh <= -0.5 else "")
            swap = [q for q in ch["rows"] if q["type"] != "halfway" and r["n"] in (q.get("overtaken_by") or [])]
            sub = f"Sch {r['sch']} \u00b7 layover {r['layover']:.0f} min"
            if r["vs_sch"] >= 0.5:
                sub += f" \u00b7 BC +{r['vs_sch']:.0f} min vs schedule"
            if swap:
                sub += f" \u00b7 runs ahead of late Trip {', '.join(str(q['n']) for q in swap)}"
            behind = ""
            if P.get("no_overtake", 1) and sh >= 0.5:                      # held because the trip in front of it leaves late (run order)
                fr = [q for q in ch["rows"] if q["type"] != "halfway" and q["n"] < r["n"]]
                fr = max(fr, key=lambda q: q["n"]) if fr else None
                if fr is not None and C.nat[r["n"]] < fr["dep"] + P["min_dep_gap"] - 0.5:
                    sub += f" \u00b7 waits for Trip {fr['n']} in front (run order)"
                    behind = f" | After Trip {fr['n']}"
            if ch.get("steady"):
                qd_ = self._queue(tuple(q["n"] for q in ch["rows"] if q["type"] == "halfway"))
                sub += " \u00b7 even headway" + (f" (over the \u00b1{P['adj_max']:g} limit)" if r["dep"] > max(float(C.nat[r["n"]]), qd_.get(r["n"], 0.0)) + P["adj_max"] + 0.49 else "")
            instr.append({"n": r["n"], "kind": "hold" if sh >= 0.5 else ("release" if sh <= -0.5 else "full"), "time": r["dep_clock"],
                          "text": f"Trip {r['n']} \u2014 Full trip | {act}Depart {r['dep_clock']}{behind}", "sub": sub})
        st = ch.get("steady")
        if st and (st.get("next_hold") or 0) >= 0.5:                      # the next trip after the block keeps the steady headway too
            instr.append({"n": C.n + 1, "kind": "hold", "time": st["next_clock"], "next": True,
                          "text": f"Trip {C.n + 1} (next trip) \u2014 Full trip | Hold {st['next_hold']:.0f} min | Depart {st['next_clock']}",
                          "sub": f"Sch {st['next_sch']} \u00b7 keeps {st['gap_min']:g} min behind Trip {st['order'][-1] if st.get('order') else C.n}"})
        for a in sorted(ch.get("term_adj") or [], key=lambda a: (a["dep"], a["n"])):
            act = f"Hold {round(a['shift']):d} min" if a["shift"] > 0 else f"Release {round(-a['shift']):d} min early"
            instr.append({"n": a["n"], "kind": "hold" if a["shift"] > 0 else "release", "time": a["dep_clock"], "later": True,
                          "text": f"Trip {a['n']}'s bus \u2014 {a['leg']} from {a['where']} | {act} | Depart {a['dep_clock']}",
                          "sub": f"Later trip \u00b7 natural departure {a['nat_clock']} \u00b7 " + ("keeps the even headway" if st else "even-spacing regulation at the terminal")})
        why = self.explain(res, dec)
        for k in res:
            res[k].pop("_mc", None)
        # trade-off (0..100, higher = better)
        horizon = max(1.0, C.off[-1] + C.R[-1])
        for k, pl in res.items():
            m = pl["mc"]
            pl["trade"] = {"headway": int(round(100 * max(0.0, min(1.0, 1 - (m["max_p85"] - C.H) / (2 * C.H))))),
                           "mileage": int(round(100 * max(0.0, 1 - (pl["det"]["km"] or 0) / max(P["max_mileage_km"], 1)))),
                           "bc": int(round(100 * max(0.0, 1 - (m["bc_p85"] or 0) / 30.0))),
                           "recovery": int(round(0.5 * (100 * max(0.0, 1 - (pl["det"]["rec"] or 0) / 240.0) if pl["det"]["recovered"] else 0.0) + 0.5 * (m["p_recovered"] or 0)))}
        disp_j = self.disp_j
        return {"ok": True, "model": MODEL_VERSION, "mode": self.mode, "H": C.H, "decision": dec["choice"], "full_choice": dec["full_choice"], "rule": dec["rule"],
                "benefit": dec["benefit"], "halfway_ok": dec["halfway_ok"], "adjust_unacceptable": dec["adjust_unacceptable"], "delay": dec["delay"],
                "instructions": instr, "why": why, "plans": res, "sims": sims, "legs": [leg_name(L) for L in range(C.NL)], "balance_trips": C.NL, "horizon": horizon_text(C.NL), "ewt": self.ewt_summary(res, dec), "tests": dec.get("tests") or [], "focus": self.focus, "late_trips": self.lateb,
                "halfway_trips_tested": self.hk, "stops_tested": [{"j": j, "name": C.names[j], "code": C.codes[j]} for j in self.js],
                "adjust_style": "steady" if self.steady_used else "least_wait", "adjust_style_asked": str(P.get("adjust_style", "least_wait")),
                "steady_alt": self.steady_alt, "steady_why": self.steady_why,
                "generated": self._gen_info(), "live": C.live, "veh": self.veh, "needs_standby": self.needs_standby, "not_recovered_min": _r(C.chain_len, 0), "now": _r(C.now, 1), "now_clock": hm(C.now) if C.now is not None else None,
                "locked": [b for b in range(1, C.n + 1) if C.locked[b]], "first_stop": C.names[0], "pts_up": C.pts_up, "pts_km": [_r(C.stop_s[j], 3) for j in C.pts_up], "prep_min": P["prep_min"], "disp_j": disp_j, "disp_name": C.names[disp_j] if disp_j is not None else None,
                "trips": [{"n": b, "sch": hm(C.S[b]), "act_arr": hm(C.act_arr[b]), "late": C.late[b - 1], "ready": hm(C.ready[b]), "nat": hm(C.nat[b])} for b in range(1, C.n + 1)],
                "qos": {k: res[k].get("qos") for k in res}, "params": {k: P[k] for k in ("adjust_style", "even_step", "even_hold_max", "steady_side", "even_side", "steady_tol", "steady_hold_max", "qos_over_max", "term_mode", "halfway_regulate", "halfway_adj_max", "win_min", "layover_min", "bunch_min", "adj_max", "min_layover_min", "balance_trips", "horizon_side", "sims", "halfway_min_late", "offsvc_factor", "prep_min",
                                                   "ewt_gain_min", "ewt_gain_pct", "ewt_gain_per_km", "ewt_adjust_min", "allow_adjacent_halfway",
                                                   "term_reg", "term_hold_max", "term_early_max", "term_min_adj", "term_tol_pct", "term_zone", "term_total_max")}}

    def ewt_summary(self, res, dec):
        """Management view: Continue full trip vs Halfway on EWT, point by point, per balance trip, and averaged (LEVEL 2)."""
        C = self.C
        ck = dec["full_choice"]
        Cn, Hw = res[ck], res.get("halfway")
        ce = Cn["ewt"]

        def blk(pl):
            e = pl["ewt"]
            return {"avg": e["avg"], "max": e["max"], "worst": e["worst"], "n_points": e["n_points"], "p50": pl["mc"]["ewt_p50"], "p85": pl["mc"]["ewt_p85"],
                    "per_trip": [{"leg": c["leg"], "avg": c.get("ewt_avg"), "max": c.get("ewt_max")} for c in pl["chain"]]}
        out = {"continue_key": ck, "continue_label": "Continue full trip" + (" + adjustment" if ck == "adjust" else " (no action)"), "continue": blk(Cn),
               "halfway": blk(Hw) if Hw is not None else None, "balance_trips": C.NL, "horizon": horizon_text(C.NL),
               "swt_basis": C.sched_basis, "H": C.H,
               "aitp": {"on": C.aitp_on, "up": len(C.aitp_up_j), "dn": len(C.aitp_dn_f), "missing": C.aitp_missing}, "none": blk(res["none"]) if ck != "none" else None}
        if Hw is not None:
            he = Hw["ewt"]
            d = (ce["avg"] or 0) - (he["avg"] or 0)
            out["diff"] = _r(-d, 3)                                           # Halfway - Continue (negative = halfway better), as in the management table
            out["improvement_pct"] = _r(100.0 * d / ce["avg"], 1) if (ce["avg"] or 0) > 0.005 else None
            hp = {(q["L"], q["p"]): q for q in he["points"]}
            rows = []
            for q in ce["points"]:
                h = hp.get((q["L"], q["p"]))
                rows.append({"L": q["L"], "leg": q["leg"], "p": q["p"], "label": q["label"], "code": q["code"], "pct": q["pct"], "cont": q["ewt"], "half": h["ewt"] if h else None,
                             "diff": _r(h["ewt"] - q["ewt"], 3) if h and h["ewt"] is not None and q["ewt"] is not None else None})
            out["rows"] = rows
        else:
            out["rows"] = [{"L": q["L"], "leg": q["leg"], "p": q["p"], "label": q["label"], "code": q["code"], "pct": q["pct"], "cont": q["ewt"], "half": None, "diff": None} for q in ce["points"]]
        return out

    def _gen_info(self):
        return {"generated": self.generated, "rejected": dict(sorted(self.reject.items(), key=lambda x: -x[1])), "families": self.families,
                "rejected_total": sum(self.reject.values())}

    def explain(self, res, dec):
        C = self.C
        ch = dec["choice"]
        N, A = res["none"], res["adjust"]
        Hw = res.get("halfway")
        n_, a_ = N["mc"], A["mc"]
        hz = horizon_text(C.NL)
        out = []
        full_ref = res[dec["full_choice"]]
        fe = full_ref["ewt"]
        cont_lbl = "departure adjustment" if dec["full_choice"] == "adjust" else "running every trip in full"
        if ch == "halfway":
            h = Hw["mc"]
            he = Hw["ewt"]
            b = dec["benefit"]
            r = next(r for r in Hw["rows"] if r["type"] == "halfway")
            f = full_ref["mc"]
            out.append(f"Halfway was selected on EWT: over {hz}, {cont_lbl} gives an Average EWT of {fe['avg']:.2f} min across {fe['n_points']} evaluation points; "
                       f"halfway + adjustment gives {he['avg']:.2f} min ({b['ewt']:+.2f} min saved, {b['ewt_pct']:.1f}% better). In the stress test the P85 Average EWT is {f['ewt_p85']:.2f} \u2192 {h['ewt_p85']:.2f} min.")
            if fe.get("worst") and he.get("worst"):
                out.append(f"Worst point: {fe['worst']['label']} on {fe['worst']['leg']} at {fe['worst']['ewt']:.2f} min EWT when continuing, "
                           f"against {he['worst']['label']} on {he['worst']['leg']} at {he['worst']['ewt']:.2f} min with halfway.")
            off = r["shift"] or 0.0
            slot_txt = "on its slot" if abs(off) < 0.5 else f"{abs(off):.0f} min {'after' if off > 0 else 'before'} its slot"
            out.append(f"Starting Trip {r['n']} halfway at {r['stop']} ({r['start_clock']}, {slot_txt}) gets that bus back into its timetable while the other trips complete the full route; "
                       f"the stops before {r['stop']} lose that trip on UP 1, which is already inside the EWT figures above.")
            out.append(f"It sacrifices {r['km_lost']:.1f} km of operated mileage; the late BC's finishing delay goes from +{f['bc_p85']:.0f} to +{h['bc_p85']:.0f} min (P85).")
            if self.needs_standby:
                out.append(f"Trip {r['n']}'s own bus cannot reach any halfway stop in time, so this needs a STANDBY bus at {r['stop']}; the late bus becomes the spare when it arrives.")
        else:
            F = res[ch]
            f = F["mc"]
            if ch == "adjust":
                out.append(f"The full trip is kept: adjusting departures lowers the Average EWT over {hz} from {N['ewt']['avg']:.2f} to {F['ewt']['avg']:.2f} min without losing mileage"
                           + (" and is needed for QoS 1.2 (no action leaves a departure headway more than " + f"{self.P['qos_over_max']:g} min over schedule)." if dec.get("adjust_for_qos") else "."))
            else:
                out.append(f"No intervention: the natural departures give an Average EWT of {F['ewt']['avg']:.2f} min over {hz}; no departure adjustment lowers it by the "
                           f"{self.P['ewt_adjust_min']:g} min needed to be worth making.")
            if Hw is not None:
                b = dec["benefit"]
                failed = [t for t in (dec.get("tests") or []) if not t["pass"]]
                out.append(f"Halfway + adjustment was simulated too: Average EWT {Hw['ewt']['avg']:.2f} min ({b['ewt']:+.2f} min, {b['ewt_pct']:+.1f}% against continuing). "
                           + ("Not recommended because: " + "; ".join(f"{t['test'].lower()} ({t['value']})" for t in failed) + "." if failed else ""))
                if (f["bc_p85"] or 0) > (Hw["mc"]["bc_p85"] or 0) + 2:
                    out.append(f"Trade-off: the late BC finishes about +{f['bc_p85']:.0f} min late (P85) with the full trip, against +{Hw['mc']['bc_p85']:.0f} min with halfway.")
            else:
                out.append(f"No halfway start is feasible here (no stop can be reached inside the slot window, even with a standby bus), so the AI balances the trips around the late bus.")
        if self.P.get("term_reg"):
            n_l = sum(1 for a in (res[ch].get("term_adj") or []))
            tot = float(self.P.get("term_total_max") or 0.0)
            pat = self.P.get("term_mode", "pattern") == "pattern"
            out.append(f"No action adjusts no trip: every later trip leaves at the later of its schedule and arrival + {self.P['min_layover_min']:g} min, so the gap "
                       f"stays almost the same on every trip (the late bus only catches up the layover above the {self.P['min_layover_min']:g}-min minimum). "
                       + (f"Trip adjustment keeps its adjusted spacing on every later trip: each bus leaves at its adjusted UP 1 time plus the scheduled cycle "
                          f"(hold \u2264 {self.P['term_hold_max']:g}, early \u2264 {self.P['term_early_max']:g} min, \u2265 {self.P['min_layover_min']:g} min layover, QoS 1.2). "
                          if pat else
                          f"Trip adjustment evens out the later trips at each terminal (hold \u2264 {self.P['term_hold_max']:g}, early \u2264 {self.P['term_early_max']:g} min, "
                          f"\u2265 {self.P['min_layover_min']:g} min layover" + (f", \u2264 {tot:g} min total hold per BC" if tot > 0 else "") + ", QoS 1.2). ")
                       + ("Halfway adjusts no other trip: from the second trip the buses run their timetable. " if not self.P.get("halfway_regulate") else
                          f"Halfway also holds / releases the trips around the lost trip, at most {self._hw_lim():g} min (what each bus captain wins back in the layover), "
                          f"so the departure gap meets QoS 1.2 and from the second trip the timetable runs again. ")
                       + f"The chosen plan needs {n_l} later-trip instruction{'s' if n_l != 1 else ''}.")
        sa = self.steady_alt
        if sa:
            lim_txt = f" (over the ±{self.P['adj_max']:g}-min limit)" if sa.get("n_over_cap") else ""
            nxt = f", and Trip {sa['next_trip']} must be held +{sa['next_hold']:.0f} min too" if (sa.get("next_hold") or 0) >= 0.5 else ""
            gl = ", ".join(str(int(x)) for x in sa["show"])
            stp = f"gaps change by at most {sa['step']} min from one bus to the next" + (f" ({sa['step_asked']} is not possible with {sa['n_trips']} trips)" if sa["step"] > sa["step_asked"] else "")
            if self.steady_used:
                out.append(f"Trip adjustment uses an EVEN HEADWAY: {stp}. Trips {sa['first']}-{sa['last']} move; gaps from Trip {sa['before']} to Trip {sa['after']}: {gl} min. "
                           f"Largest hold +{sa['hold_max']:.0f} min{lim_txt}. Average EWT {sa['ewt']:.2f} min, against {sa['least_wait_ewt']:.2f} min with the least-waiting spacing.")
            else:
                out.append(f"An even headway ({stp}) would move Trips {sa['first']}-{sa['last']} with gaps {gl} min; largest hold +{sa['hold_max']:.0f} min{lim_txt}; "
                           f"Average EWT {sa['ewt']:.2f} min against {sa['least_wait_ewt']:.2f} min with the least-waiting spacing used here.")
        q = res[ch].get("qos") or {}
        if q and not q.get("ok"):
            w = (q.get("breaches") or [{}])[0]
            out.append(f"QoS 1.2 cannot be fully met within the limits (\u00b1{self.P['adj_max']:g} min, {self.P['min_layover_min']:g}-min layover, departed trips locked): "
                       f"worst {w.get('headway')} min against {w.get('scheduled')} scheduled on {w.get('leg')} at {w.get('where')}, between {w.get('between')}.")
        elif q:
            out.append(f"QoS 1.2 met: every departure headway is at most {self.P['qos_over_max']:g} min over its scheduled headway.")
        # is the biggest interchange gap physically unavoidable? (bus before it already held to the cap, bus after it leaves as soon as it is ready)
        best = res[ch]
        full = sorted([r for r in best["rows"] if r["type"] != "halfway"], key=lambda r: r["dep"])
        if len(full) > 1:
            gaps = [(full[k + 1]["dep"] - full[k]["dep"], full[k], full[k + 1]) for k in range(len(full) - 1)]
            g, a, c = max(gaps, key=lambda x: x[0])
            a_cap = a["type"] == "locked" or (a["shift"] or 0) >= self.P["adj_max"] - 0.5 or a["late"] >= 1
            c_ready = abs((c["dep"] or 0) - C.ready[c["n"]]) < 0.6 and C.ready[c["n"]] > C.S[c["n"]]
            if g >= 1.5 * C.H and a_cap and c_ready:
                out.append(f"The {g:.0f}-min gap at {C.names[0]} between {a['dep_clock']} (Trip {a['n']}) and {c['dep_clock']} (Trip {c['n']}) cannot be closed by holding: "
                           f"Trip {a['n']} is already at its {self.P['adj_max']:g}-min limit and no other bus is ready before {c['dep_clock']}. "
                           + ("The halfway start covers it downstream; " if ch == "halfway" else "")
                           + "only a standby bus at the interchange would close it for the first stops.")
        loc = res.get("local")
        if loc is not None:
            best = res[ch]
            l1, b1 = loc["chain"][0]["max"], best["chain"][0]["max"]
            ld = max(((c["max"] or 0) for c in loc["chain"][1:]), default=0)
            bd = max(((c["max"] or 0) for c in best["chain"][1:]), default=0)
            if ld > bd + 1:
                out.append(f"Fixing only the next departure looks good at UP 1 ({l1:.0f} min) but pushes the later trips to a {ld:.0f} min headway; the chosen plan keeps them at {bd:.0f} min.")
        return out


def _hist(a, lo=None, hi=None):
    lo = math.floor(float(np.min(a))) if lo is None else lo
    hi = math.ceil(float(np.max(a))) if hi is None else hi
    if hi <= lo:
        hi = lo + 1
    edges = list(range(lo, hi + 1))
    if len(edges) > 41:
        step = math.ceil((hi - lo) / 40)
        edges = list(range(lo, hi + step, step))
    cnt, _ = np.histogram(a, bins=edges)
    return {"edges": edges, "counts": [int(c) for c in cnt]}


def optimise(ctx):
    if not ctx.get("H") or float(ctx["H"]) <= 0:
        return {"ok": False, "error": "Scheduled headway unknown - enter it in the Headway box."}
    if not ctx.get("tau") or len(ctx["tau"]) < 3:
        return {"ok": False, "error": "Route has too few stops to simulate."}
    return Optimiser(ctx).run()
