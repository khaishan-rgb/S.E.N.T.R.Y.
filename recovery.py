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

Pure computation (numpy), no I/O. Decision support only.
"""
import math
import numpy as np

MODEL_VERSION = "recovery-2.0-ewt"
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
    "term_total_max": 8.0,        # HARD (BC welfare): total artificial hold per BC over the whole horizon, UP 1 included  # 0 = two back-to-back trips (e.g. T3 + T4) may NOT both start halfway; 1 = allowed       # a departure adjustment is used (instead of no action) only if it cuts the Average EWT by at least this
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
        self.pts_up = sorted(base | set(self.cand_j))
        self.prof_up = np.array([self.tau[j] for j in self.pts_up])
        cnt = [0] * len(self.pts_up)
        for s in range(self.n_st):                               # each stop counts for its nearest timing point
            k = min(range(len(self.pts_up)), key=lambda q: (abs(self.pts_up[q] - s), q))
            cnt[k] += 1
        self.w_up = np.array(cnt, float)
        self.frac_dn = [float(f) for f in np.linspace(0, 1, 7)]
        self.prof_dn = np.array([R_dn * f for f in self.frac_dn])
        dn = ctx.get("down_stops") or []                          # [(name, code, km)] of the return direction, for labels only
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
        return {"end": A, "maxg": maxg, "ming": ming, "sq": sq, "cnt": cnt, "rec": rec, "w": w,
                "dep_T": first_T, "dep_order": first_order, "dep_g": first_g, "pts": pts_store,
                "ewt": ewt, "awt": awt_, "swt": swt_, "sch_h": sch_h}

    # ---------------------------------------------------------------- full chain
    def run(self, dep1, halfway=None, nz=None, keep=False):
        """dep1: (S, B) planned interchange departures for UP 1 (anchors / halfway columns ignored).
        halfway: list of (trip b, stop j, start (S,)) - start = the time the halfway bus starts at stop j.
        nz: stress test noise (see noise()), None = deterministic."""
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
        joins = []
        for (b, j, st) in (halfway or []):
            D[:, b] = np.nan
            joins.append((b, self.pidx[j], np.asarray(st, float) + self.tau[j]))
        legs = []
        term = []
        used = np.maximum(0.0, np.nan_to_num(D - np.maximum(self.nat[None, :], ready)))     # artificial hold already given at UP 1
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
                if P.get("term_reg"):
                    reg = self.term_regulate(nxt, rdy, used)
                    used = used + np.maximum(0.0, reg - nxt)
                    if keep:
                        term.append((L + 1, nxt[0].copy(), reg[0].copy()))
                    nxt = reg
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
        out = {"ic_short": ic_short, "leg_min": leg_min, "max": np.nanmax(leg_max, 1), "ic_max": np.nanmax(g0, 1), "ic_sq": np.nansum((g0 - H) ** 2, 1), "wmax": leg_wmax.mean(1), "leg_max": leg_max, "leg_wmax": leg_wmax,
               "rms": np.sqrt(sq / np.maximum(cnt, 1)), "bunch": bunch_pts / bunch_tot, "any_bunch": (leg_min < P["bunch_min"] - EPS).any(1),
               "rec": np.where(np.isinf(rec), 0.0, np.maximum(0.0, rec - self.ref)), "unrec": late_irr, "bc": bc, "bc_max": np.maximum(bc, 0).max(1),
               "bc_sum": np.maximum(bc, 0).sum(1), "up1_late": up1_late, "D": D, "ewt_avg": ewt_avg, "ewt_max": ewt_max, "ewt_n": n_ok}
        if keep:
            out["legs"] = legs
            out["term"] = term
        return out

    # ---------------------------------------------------------------- terminal regulation (later balance trips)
    def term_regulate(self, N, R, used=None, iters=40, alpha=0.5):
        """Two-way even-spacing holding at a terminal. N: natural departures (Sn, B), R: arrival + minimum layover.
        Each bus moves towards the midpoint of its neighbours, d_i* = (d_(i-1) + d_(i+1)) / 2, inside
        max(R_i, N_i - early) <= d_i <= N_i + hold; the on-schedule buses 0 and n+1 stay fixed; no overtaking;
        a change under the dead band is dropped. The same rule runs in every scenario and every stress-test future."""
        P = self.P
        Sn, B = N.shape
        R = np.where(np.isnan(R), N, R)
        lo = np.maximum(R, N - P["term_early_max"])
        room = P["term_hold_max"] if used is None else np.clip(P["term_total_max"] - used, 0.0, P["term_hold_max"])
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
        return D

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
def _cost(m, w, H, km, adj, w_short=1.5):
    return (w.get("ewt", 0.0) * m["ewt_avg"] + w_short * m["ic_short"] + w["mx"] * np.maximum(0.0, m["max"] - H) + w["wmx"] * np.maximum(0.0, m["wmax"] - H) + w["rms"] * m["rms"] + w["bunch"] * 10.0 * m["bunch"]
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
    def _bounds(self, b):
        C, P = self.C, self.P
        lo = max(C.ready[b], C.nat[b] - P["adj_max"])
        hi = C.nat[b] + P["adj_max"]
        return math.ceil(lo - 1e-9), math.floor(hi + 1e-9)

    def _check(self, dep, hw):
        """dep: {trip: target}, hw: [(b, j, start)] -> list of rejection reasons (empty = feasible)."""
        C, P = self.C, self.P
        why = []
        for b, x in dep.items():
            if C.locked[b] and abs(x - C.nat[b]) > 0.49:
                why.append("Alters an already-departed trip")
                continue
            if x < C.ready[b] - 0.01:
                why.append("BC gets less than the 7-min minimum layover")
            if abs(x - C.nat[b]) > P["adj_max"] + 1e-6:
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
        full[0], full[C.n + 1] = C.S[0], C.S[-1]
        seq = sorted(full.items(), key=lambda kv: (kv[1], kv[0]))
        mv = lambda b: b in dep and abs(dep[b] - C.nat[b]) > 0.49
        for (a, xa), (c, xc) in zip(seq, seq[1:]):
            if xc - xa < P["min_dep_gap"] - 1e-6 and (mv(a) or mv(c)):
                why.append("Creates simultaneous departures at the interchange")
                break
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
        m = C.run(D, hw, nz)
        km = np.array([sum(C.stop_s[j] for (_, j, _) in pl["hw"]) for pl in plans])
        hwb = [{h[0] for h in pl["hw"]} for pl in plans]
        adj = np.array([sum(abs(x - C.nat[b]) for b, x in pl["dep"].items() if b not in hwb[r]) for r, pl in enumerate(plans)])
        m["km"], m["adj"] = km, adj
        m["cost"] = _cost(m, self.W, C.H, km, adj, self.P["w_ic_short"])
        m["local"] = _local_cost(m, C.H)
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
        C = self.C
        buses = [0] + [b for b in range(1, C.n + 1) if b not in drop] + [C.n + 1]
        seq = sorted(buses, key=lambda b: (C.nat[b], b))
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

    def _project(self, dep, drop=()):
        """clip a raw plan into the feasible box and push apart simultaneous departures (repaired variant)."""
        C, P = self.C, self.P
        d = {}
        for b, x in dep.items():
            if C.locked[b] or b in drop:
                continue
            lo, hi = self._bounds(b)
            d[b] = float(min(max(x, lo), hi)) if lo <= hi else float(math.ceil(C.nat[b]))
        return d

    def generate(self):
        C, P = self.C, self.P
        lateb, focus = self._focus()
        self.lateb, self.focus = lateb, focus
        feas = {"adjust": [], "halfway": {}}
        feas["adjust"].append({"dep": {}, "hw": [], "fam": "No intervention"})
        self._count("No intervention", [])
        # one trip at a time
        for b in focus:
            for sh in (-10, -8, -6, -4, -2, 2, 4, 6, 8, 10):
                dep = {b: float(round(C.nat[b] + sh))}
                why = self._check(dep, [])
                self._count("Adjust one trip", why)
                if not why:
                    feas["adjust"].append({"dep": dep, "hw": [], "fam": "Adjust one trip"})
        # spread departure timings (raw and repaired)
        for dep in self._spreads(focus):
            why = self._check(dep, [])
            fam = "Spread departure timings" if len(dep) >= 3 else "Adjust several trips (\u00b18 min)"
            self._count(fam, why)
            if not why:
                feas["adjust"].append({"dep": dep, "hw": [], "fam": fam})
            pr = self._project(dep)
            if pr and pr != dep:
                why2 = self._check(pr, [])
                self._count("Full trip + departure regulation", why2)
                if not why2:
                    feas["adjust"].append({"dep": pr, "hw": [], "fam": "Full trip + departure regulation"})
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
        for combo in combos:
            regs = [{}] + [self._project(d, combo) for d in self._spreads(focus, combo)[:12]]
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
                        dep = {b: x for b, x in rg.items() if b not in combo}
                        why = self._check(dep, hw)
                        fam = ("Halfway one trip + regulate the others" if len(combo) == 1 and dep else
                               "Halfway one trip, others run full trips" if len(combo) == 1 else "Two halfway starts + regulation")
                        self._count(fam, why)
                        if not why:
                            lst.append({"dep": dep, "hw": hw, "fam": fam})
                if lst:
                    out[key] = lst
        return out

    # -------------------------------------------------- local search (coordinate descent, batched)
    def refine(self, plan, objective="cost", sweeps=3):
        C, P = self.C, self.P
        hwb = {h[0] for h in plan["hw"]}
        vars_ = [b for b in self.focus if not C.locked[b] and b not in hwb]
        cur = {"dep": dict(plan["dep"]), "hw": list(plan["hw"]), "fam": plan.get("fam")}
        best = self._eval([cur])[objective][0]
        for _ in range(sweeps):
            improved = False
            for b in vars_:
                lo, hi = self._bounds(b)
                if lo > hi:
                    continue
                trial = []
                for x in range(lo, hi + 1):
                    d = dict(cur["dep"]); d[b] = float(x)
                    if abs(x - C.nat[b]) < 0.49:
                        d.pop(b, None)
                    if self._check(d, cur["hw"]):
                        continue
                    trial.append({"dep": d, "hw": cur["hw"], "fam": cur["fam"]})
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
                    trial.append({"dep": cur["dep"], "hw": hw, "fam": cur["fam"]})
                if trial:
                    m = self._eval(trial)[objective]
                    k = int(np.argmin(m))
                    if m[k] < best - 1e-6:
                        best, cur, improved = float(m[k]), trial[k], True
            if not improved:
                break
        return cur, best

    # -------------------------------------------------- describe a plan
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
            rows.append({"n": b, "type": "locked" if C.locked[b] else "full", "dep": _r(x, 1), "dep_clock": hm(x), "sch": hm(C.S[b]),
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
        ewt_pts = self.ewt_points(det)
        valid = [q for q in ewt_pts if q["ewt"] is not None]
        worst = max(valid, key=lambda q: q["ewt"]) if valid else None
        ewt_blk = {"avg": _r(det["ewt_avg"][0], 3), "max": _r(det["ewt_max"][0], 2), "n_points": len(valid), "n_points_all": len(ewt_pts),
                   "worst": {"label": worst["label"], "leg": worst["leg"], "ewt": worst["ewt"]} if worst else None, "points": ewt_pts}
        out = {"key": key, "label": label, "fam": plan.get("fam"), "rows": rows, "chain": chain, "ewt": ewt_blk, "term_adj": term_adj,
               "det": {"max": _r(det["max"][0]), "ic_max": _r(det["ic_max"][0]), "wmax": _r(det["wmax"][0]), "rms": _r(det["rms"][0], 2),
                       "bunch_pct": _r(100 * det["bunch"][0], 0), "rec": _r(det["rec"][0], 0), "recovered": bool(not det["unrec"][0]), "min_hw": _r(float(np.nanmin(det["leg_min"][0])) if "leg_min" in det else None, 1), "km": _r(det["km"][0], 2), "adj": _r(det["adj"][0], 0),
                       "bc_max": _r(det["bc_max"][0], 1), "bc_sum": _r(det["bc_sum"][0], 1), "cost": _r(det["cost"][0], 1),
                       "bc": [_r(x, 1) for x in det["bc"][0]], "up1_late": [_r(x, 1) for x in det["up1_late"][0]]},
               "ic_hw": ic, "down_hw": down, "at_j": at_j, "up1_times": {str(k): v for k, v in up1.items()}, "n_adjusted": sum(1 for r in rows if r["type"] == "full" and abs(r["shift"]) >= 0.5),
               "n_halfway": len(hwb), "n_full": sum(1 for r in rows if r["type"] != "halfway")}
        if mc is not None:
            out["mc"] = {"n": int(len(mc["max"])), "max_mean": _r(mc["max"].mean()), "max_p50": _pct(mc["max"], 50), "max_p85": _pct(mc["max"], 85), "max_p90": _pct(mc["max"], 90),
                         "wmax_p50": _pct(mc["wmax"], 50), "wmax_p85": _pct(mc["wmax"], 85),
                         "rec_p50": _pct(mc["rec"], 50), "rec_p85": _pct(mc["rec"], 85), "p_recovered": _r(100.0 * (~mc["unrec"]).mean(), 0), "p_bunch": _r(100.0 * mc["any_bunch"].mean(), 0),
                         "bc_p50": _pct(mc["bc_max"], 50), "bc_p85": _pct(mc["bc_max"], 85), "km": _r(det["km"][0], 2),
                         "cost_mean": _r(mc["cost"].mean(), 1),
                         "ewt_mean": _r(float(mc["ewt_avg"].mean()), 3), "ewt_p50": _r(float(np.percentile(mc["ewt_avg"], 50)), 3),
                         "ewt_p85": _r(float(np.percentile(mc["ewt_avg"], 85)), 3), "ewt_p90": _r(float(np.percentile(mc["ewt_avg"], 90)), 3),
                         "hist": _hist(mc["max"])}
        return out

    # -------------------------------------------------- LEVEL 1 table: EWT at every evaluation point of every balance trip (deterministic run)
    def ewt_points(self, det):
        C = self.C
        out = []
        for L, lg in enumerate(det["legs"]):
            up = L % 2 == 0
            sch_h = [float(x) for x in lg["sch_h"]]
            for p, (T_, o_, g_) in enumerate(lg["pts"]):
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
                e = lg["ewt"][0, p]
                out.append({"L": L, "leg": leg_name(L), "p": p, "label": nm + (" (dep.)" if kind == "dep" else " (arr.)" if kind == "arr" else ""), "code": cd,
                            "pct": _r(pct, 0), "ewt": None if math.isnan(e) else _r(float(e), 3),
                            "awt": _r(prt["awt"], 3) if prt else None, "swt": _r(prt["swt"], 3) if prt else None, "n_hw": len(h),
                            "sum_h": _r(prt["sum_h"], 2) if prt else None, "sum_h2": _r(prt["sum_h2"], 1) if prt else None,
                            "sum_hs": _r(prt["sum_hs"], 2) if prt else None, "sum_hs2": _r(prt["sum_hs2"], 1) if prt else None,
                            "h": [[b, _r(g, 2)] for b, g in h], "hs": [_r(x, 2) for x in sch_h]})
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
        ma = self._eval(adj)
        base_cost = float(ma["cost"][0])
        base_down = float(np.nanmax(ma["leg_max"][0, 1:])) if C.NL > 1 else float(ma["max"][0])
        keep = []
        for k, pl in enumerate(adj):
            pl["_cost"] = float(ma["cost"][k])
            dn = float(np.nanmax(ma["leg_max"][k, 1:])) if C.NL > 1 else float(ma["max"][k])
            if k > 0 and dn > base_down + 2.0 and pl["_cost"] >= base_cost * 0.97:
                self.reject["Worse downstream headway without enough benefit"] = self.reject.get("Worse downstream headway without enough benefit", 0) + 1
                continue
            keep.append(pl)
        seeds_a = sorted(keep, key=lambda p: p["_cost"])[:3]
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
        for s in seeds_a + [none_plan]:
            r, c = self.refine(s)
            if c < ca:
                best_a, ca = r, c
        best_h, ch = None, 1e18
        for s in sorted(hbest, key=lambda p: p["_cost"])[:4]:
            r, c = self.refine(s)
            if c < ch:
                best_h, ch = r, c
        local, _ = self.refine(none_plan, objective="local", sweeps=2)
        # ---- stress test
        rng = np.random.default_rng(int(P["seed"]))
        sims = int(max(50, min(5000, P["sims"])))
        nz = C.noise(sims, rng)
        res = {}
        cand = [("none", none_plan, "No action"), ("adjust", best_a, "Full trip + adjustment")]
        if best_h is not None:
            cand.append(("halfway", best_h, "Halfway + regulation"))
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
        m = C.run(D, hw, None, keep=True)
        km = np.array([sum(C.stop_s[j] for (_, j, _) in pl["hw"])])
        hwb = {h[0] for h in pl["hw"]}
        adj = np.array([sum(abs(x - C.nat[b]) for b, x in pl["dep"].items() if b not in hwb)])
        m["km"], m["adj"] = km, adj
        m["cost"] = _cost(m, self.W, C.H, km, adj, self.P["w_ic_short"])
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
        m = C.run(D, hw, nz)
        km = np.full(sims, sum(C.stop_s[j] for (_, j, _) in pl["hw"]))
        hwb = {h[0] for h in pl["hw"]}
        adj = np.full(sims, sum(abs(x - C.nat[b]) for b, x in pl["dep"].items() if b not in hwb))
        m["km"] = km
        m["cost"] = _cost(m, self.W, C.H, km, adj, self.P["w_ic_short"])
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
        full = "adjust" if (a_useful and A["n_adjusted"] > 0) else "none"
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
            h_ok = all(t["pass"] for t in tests)
        prio = {"headway": "Headway priority (EWT gain threshold halved)", "mileage": "Mileage priority (EWT gain must also be worth the km lost)"}.get(self.mode, "Balanced")
        rule = (f"{prio}: Continue full trip vs Halfway compared on Average EWT across {self._npts(F)} evaluation points over {horizon_text(C.NL)}. "
                f"Halfway only if it lowers the Average EWT by \u2265 {P['ewt_gain_min']:g} min and \u2265 {P['ewt_gain_pct'] * (0.5 if self.mode == 'headway' else 1.0):g}% and the stress test agrees. "
                f"Delay ({delay:.0f} min) is an input to the simulation, not a decision threshold.")
        choice = "halfway" if (Hw is not None and h_ok) else full
        return {"choice": choice, "full_choice": full, "halfway_ok": h_ok, "benefit": benefit, "rule": rule, "delay": delay, "adjust_useful": a_useful,
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
            instr.append({"n": r["n"], "kind": "hold" if sh >= 0.5 else ("release" if sh <= -0.5 else "full"), "time": r["dep_clock"],
                          "text": f"Trip {r['n']} \u2014 Full trip | {act}Depart {r['dep_clock']}", "sub": sub})
        for a in sorted(ch.get("term_adj") or [], key=lambda a: (a["dep"], a["n"])):
            act = f"Hold {round(a['shift']):d} min" if a["shift"] > 0 else f"Release {round(-a['shift']):d} min early"
            instr.append({"n": a["n"], "kind": "hold" if a["shift"] > 0 else "release", "time": a["dep_clock"], "later": True,
                          "text": f"Trip {a['n']}'s bus \u2014 {a['leg']} from {a['where']} | {act} | Depart {a['dep_clock']}",
                          "sub": f"Later trip \u00b7 natural departure {a['nat_clock']} \u00b7 even-spacing regulation at the terminal"})
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
                "generated": self._gen_info(), "live": C.live, "veh": self.veh, "needs_standby": self.needs_standby, "not_recovered_min": _r(C.chain_len, 0), "now": _r(C.now, 1), "now_clock": hm(C.now) if C.now is not None else None,
                "locked": [b for b in range(1, C.n + 1) if C.locked[b]], "first_stop": C.names[0], "pts_up": C.pts_up, "pts_km": [_r(C.stop_s[j], 3) for j in C.pts_up], "prep_min": P["prep_min"], "disp_j": disp_j, "disp_name": C.names[disp_j] if disp_j is not None else None,
                "trips": [{"n": b, "sch": hm(C.S[b]), "act_arr": hm(C.act_arr[b]), "late": C.late[b - 1], "ready": hm(C.ready[b]), "nat": hm(C.nat[b])} for b in range(1, C.n + 1)],
                "params": {k: P[k] for k in ("bunch_min", "adj_max", "min_layover_min", "balance_trips", "horizon_side", "sims", "halfway_min_late", "offsvc_factor", "prep_min",
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
               "swt_basis": C.sched_basis, "H": C.H, "none": blk(res["none"]) if ck != "none" else None}
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
                out.append(f"The full trip is kept: adjusting departures lowers the Average EWT over {hz} from {N['ewt']['avg']:.2f} to {F['ewt']['avg']:.2f} min without losing mileage.")
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
            out.append(f"On the later balance trips both scenarios get the same terminal regulation (each bus moves towards the midpoint of the buses either side, "
                       f"hold \u2264 {self.P['term_hold_max']:g} min, early \u2264 {self.P['term_early_max']:g} min, \u2265 {self.P['min_layover_min']:g} min layover, "
                       f"\u2264 {self.P['term_total_max']:g} min total hold per BC); the chosen plan needs {n_l} such terminal instruction{'s' if n_l != 1 else ''}.")
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
