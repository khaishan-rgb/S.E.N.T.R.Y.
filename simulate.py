"""Monte Carlo future simulator (spec 13).

Fat tails (Student-t), jump diffusion, per-path random volatility, scheduled earnings jumps,
11 regimes, IV that moves with regime and spot, bid/ask costs on entry and exit.
Exits inside the simulation follow the trade plan: main target, thesis invalidation (stock level),
and the maximum-risk rule - NOT an arbitrary -20%/-30% stop.
"""
import numpy as np
from . import config
from .pricing import bs_price


def cal_to_td(cal_days):
    return max(1, int(round(cal_days * 252 / 365)))


def simulate_regimes(S0, sigma, checkpoints_td, n, earn_td=None, earn_sd=0.05, seed=config.SEED):
    rng = np.random.default_rng(seed)
    cps = sorted({int(c) for c in checkpoints_td if c >= 1})
    want, maxd = set(cps), cps[-1]
    dt, sq = 1 / 252, np.sqrt(1 / 252)
    out = {}
    for name, g in config.REGIMES.items():
        sig = sigma * g["vol_mult"] * np.exp(0.25 * rng.standard_normal(n) - 0.03125)
        drift = (g["mu"] - 0.5 * sig ** 2) * dt
        logS = np.full(n, np.log(S0))
        store = {}
        for d in range(1, maxd + 1):
            step = drift + sig * sq * rng.standard_t(4, n) / np.sqrt(2.0)
            nj = rng.poisson(g["jump_lambda"] * dt, n)
            hit = nj > 0
            if hit.any():
                k = nj[hit]
                step[hit] += g["jump_mean"] * k + g["jump_sd"] * np.sqrt(k) * rng.standard_normal(k.size)
            if earn_td is not None and d == earn_td and earn_sd > 0:
                b = g["earn_bias"]
                step += rng.normal(b * 0.8 * earn_sd, 0.6 * earn_sd, n) if b else rng.normal(0, earn_sd, n)
            logS += step
            if d in want:
                store[d] = np.exp(logS)
        out[name] = store
    return out


def _earn_iv_drop(dte):
    return min(0.25, 0.10 * 60 / max(dte, 1))


def position_value(trade, S_t, t_cal, ctx, regime="normal", exit_side=True):
    """Per-share value of the position t_cal calendar days from now."""
    g = config.REGIMES[regime]
    S_t = np.asarray(S_t, dtype=float)
    total = np.zeros_like(S_t)
    for leg in trade["legs"]:
        T = max(leg["dte"] - t_cal, 0) / 365
        frac = min(1.0, t_cal / 90)
        iv = leg["iv"] * (1 + (g["iv_mult"] - 1) * frac) * (S_t / ctx["S0"]) ** (-config.SPOT_VOL_BETA)
        ec = ctx.get("earn_cal")
        if ec is not None and ec <= t_cal < leg["dte"]:
            iv = iv * (1 - _earn_iv_drop(leg["dte"]))
        iv = np.clip(iv, 0.05, 3.0)
        v = bs_price(S_t, leg["K"], T, ctx["r"], iv, leg["kind"], ctx["q"])
        if exit_side:
            v = v * (1 - leg["sign"] * leg["hs"])
        total = total + leg["sign"] * v
    return np.maximum(total, 0.0)


def _exit_hit(trade, S, value):
    hit = np.zeros(len(S), dtype=bool)
    if trade.get("target_value"):
        hit |= value >= trade["target_value"]
    st = trade.get("stop_stock")
    if st:
        hit |= (S <= st) if trade["legs"][0]["kind"] == "call" else (S >= st)
    ml = trade.get("max_loss_rule")
    if ml:
        hit |= value <= trade["cost"] * (1 - ml)
    return hit


def pooled(trade, sims, ctx, td_idx, t_cal, exit_side=True, exits=False):
    """Pool regimes by weight; optionally apply the trade plan's exits at monitoring dates."""
    R, P = [], []
    for name, w in ctx["weights"].items():
        S_all = sims[name][td_idx]
        m = int(round(w * len(S_all)))
        if m <= 0:
            continue
        S = S_all[:m]
        r_end = position_value(trade, S, t_cal, ctx, name, exit_side) / trade["cost"] - 1
        if exits:
            out = np.full(m, np.nan)
            for d in trade["monitor_td"]:
                if d >= td_idx or d not in sims[name]:
                    continue
                Sd = sims[name][d][:m]
                v = position_value(trade, Sd, d * 365 / 252, ctx, name, True)
                hit = np.isnan(out) & _exit_hit(trade, Sd, v)
                out[hit] = v[hit] / trade["cost"] - 1
            r_end = np.where(np.isnan(out), r_end, out)
        R.append(r_end)
        P.append(S)
    return np.concatenate(R), np.concatenate(P)


def evaluate(trade, sims, ctx):
    R, P = pooled(trade, sims, ctx, trade["horizon_td"], trade["horizon_cal"], exits=True)
    Rn, _ = pooled(trade, sims, ctx, trade["horizon_td"], trade["horizon_cal"])
    Re, _ = pooled(trade, sims, ctx, trade["expiry_td"], trade["dte"], exit_side=False)
    up, dn = np.mean(np.maximum(R, 0)), np.mean(np.maximum(-R, 0))
    down = np.sqrt(np.mean(np.minimum(R, 0) ** 2)) or 1e-9
    V = (Rn + 1) * trade["cost"]            # exit proceeds if held to the time exit (independent of entry price)
    q = lambda a, p: float(np.percentile(a, p))
    return dict(
        p_profit=float(np.mean(R > 0)), p_25=float(np.mean(R >= 0.25)), p_50=float(np.mean(R >= 0.5)),
        p_100=float(np.mean(R >= 1.0)), p_200=float(np.mean(R >= 2.0)),
        p_loss50=float(np.mean(R <= -0.5)), p_lose_most=float(np.mean(R <= -0.8)),
        p_total_loss=float(np.mean(R <= -0.95)), p_total_loss_no_stop=float(np.mean(Rn <= -0.95)),
        p_profit_no_rules=float(np.mean(Rn > 0)), p_profit_expiry=float(np.mean(Re > 0)),
        mean=float(np.mean(R)), median=q(R, 50), q05=q(R, 5), q10=q(R, 10), q25=q(R, 25),
        q75=q(R, 75), q95=q(R, 95), upside=float(up), downside=float(dn), rr=float(up / dn) if dn > 0 else 99.0,
        sortino=float(np.mean(R) / down), ev_dollars=float(np.mean(R) * config.INVESTMENT),
        stock_q10=q(P, 10), stock_q50=q(P, 50), stock_q80=q(P, 80), stock_q90=q(P, 90),
        mean_v=float(np.mean(V)), v_pct=[float(x) for x in np.percentile(V, np.arange(101))], n_paths=int(len(R)),
    )


def time_path(trade, sims, ctx):
    rows = []
    days = [d for d in (1, 5, 21, 63, 126, 252) if d < trade["expiry_td"]] + [trade["expiry_td"]]
    labels = {1: "1 day", 5: "1 week", 21: "1 month", 63: "3 months", 126: "6 months", 252: "12 months"}
    for d in days:
        if d not in sims["normal"]:
            continue
        t_cal = trade["dte"] if d == trade["expiry_td"] else d * 365 / 252
        R, P = pooled(trade, sims, ctx, d, t_cal, exit_side=False)
        smed = float(np.median(P))
        flat = float(position_value(trade, np.array([ctx["S0"]]), t_cal, ctx, "normal", False)[0])
        up = position_value(trade, np.array([smed * 1.01]), t_cal, ctx, "normal", False)[0]
        dn = position_value(trade, np.array([smed * 0.99]), t_cal, ctx, "normal", False)[0]
        rows.append(dict(when="Expiry" if d == trade["expiry_td"] else labels[d], stock_median=smed,
                         option_median=float(np.median((R + 1) * trade["cost"])), flat_stock_value=flat,
                         delta=float((up - dn) / (0.02 * smed))))
    return rows


def profit_table(trade, ctx, moves=(-0.30, -0.10, 0.0, 0.10, 0.20, 0.40, 0.70)):
    rows = []
    for m in moves:
        S = ctx["S0"] * (1 + m)
        v = float(position_value(trade, np.array([S]), trade["horizon_cal"], ctx, "normal", True)[0])
        val = config.INVESTMENT * v / trade["cost"]
        rows.append(dict(move=m, stock=S, option=v, value=val, ret=val / config.INVESTMENT - 1))
    return rows
