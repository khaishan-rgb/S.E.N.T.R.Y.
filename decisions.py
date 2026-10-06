"""Three separate decision engines (spec 4) + trade plan / trigger / fair value (spec 11, 14, 16-18).

ENTRY  - "If I had cash today, should I open this option?"   -> BUY NOW | BUY IF TRIGGERED | WAIT | AVOID
HOLD   - "Given I own it, is remaining upside worth remaining downside?" -> HOLD | HOLD / WATCH | TAKE 25%/50% | SELL
EXIT PRESSURE - "Has the trade deteriorated enough that exiting is mathematically preferable?" -> 0-100

All levels are derived from volatility, technical structure, model fair value and the simulation -
no fixed +50% / -30% rules. Hold decisions are measured from the price you could sell at TODAY
(the bid), never from what you paid, to avoid sunk-cost bias.
"""
import datetime as dt
import math

import numpy as np

from .simulate import position_value, cal_to_td

ENTRY_COLORS = {"BUY NOW": "green", "BUY IF TRIGGERED": "green", "WAIT": "yellow", "AVOID": "black"}


def ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def fair_class(market, fair):
    if not ok(fair) or fair <= 0.01 or not ok(market):
        return "UNKNOWN", None
    d = market / fair - 1
    lab = ("CHEAP" if d <= -0.15 else "ATTRACTIVE" if d <= -0.05 else "FAIR" if d <= 0.08
           else "EXPENSIVE" if d <= 0.20 else "VERY EXPENSIVE")
    return lab, d


# ------------------------------------------------------------------ time exit (from theta)
def time_exit_cal(trade, ctx, settings):
    """Hold until flat-stock daily decay would exceed the configured % of premium (theta cliff)."""
    dte, lim = trade["dte"], settings["max_daily_theta_pct"]
    S = np.array([ctx["S0"]])
    step = 1 if dte <= 60 else 3
    prev = position_value(trade, S, 0, ctx, "normal", False)[0]
    t, te = step, None
    while t < dte - 3:
        v = position_value(trade, S, t, ctx, "normal", False)[0]
        if prev <= 0.01 or (prev - v) / prev / step > lim:
            te = t - step
            break
        prev, t = v, t + step
    if te is None:
        te = dte - 7
    te = max(te, int(0.3 * dte), 3)
    return int(min(te, dte - 1))


# ------------------------------------------------------------------ trade plan
def apply_plan(trade, tech, sigma, ctx, settings):
    """Derive time exit, thesis level, first profit, main target and cut level for a contract."""
    te = time_exit_cal(trade, ctx, settings)
    trade["horizon_cal"], trade["horizon_td"] = te, cal_to_td(te)
    trade["exit_date"] = str(dt.date.today() + dt.timedelta(days=te))
    step = 5 if trade["horizon_td"] <= 60 else 10 if trade["horizon_td"] <= 150 else 21
    trade["monitor_td"] = list(range(step, trade["horizon_td"], step))
    S0, call = ctx["S0"], trade["legs"][0]["kind"] == "call"
    Th = te / 365
    sd = sigma * math.sqrt(Th)
    s50 = tech["sma"].get(50)

    # thesis invalidation: technical structure if sensible, otherwise a 1-sigma (<=3 month) move
    vol_stop = S0 * math.exp((-1 if call else 1) * sigma * math.sqrt(min(Th, 0.25)))
    if call:
        tl = max(tech["low60"], s50 * 0.97) if ok(s50) else tech["low60"]
        stop = tl if S0 * math.exp(-2 * sd) < tl < S0 * 0.98 else vol_stop
        target_stock = S0 * math.exp(0.84 * sd)
        res = [x for x in (tech["high60"], tech["hi252"]) if S0 * 1.02 < x < target_stock]
        first_stock = min(res) if res else S0 * math.exp(0.42 * sd)
    else:
        tl = min(tech["high60"], s50 * 1.03) if ok(s50) else tech["high60"]
        stop = tl if S0 * 1.02 < tl < S0 * math.exp(2 * sd) else vol_stop
        target_stock = S0 * math.exp(-0.84 * sd)
        sup = [x for x in (tech["low60"],) if target_stock < x < S0 * 0.98]
        first_stock = max(sup) if sup else S0 * math.exp(-0.42 * sd)

    pv = lambda S, t: float(position_value(trade, np.array([S]), t, ctx, "normal", True)[0])
    c = trade["cost"]
    main = pv(target_stock, te / 2)
    first = pv(first_stock, te / 3)
    cut = pv(stop, te / 4)
    main = max(main, c * 1.10)
    first = max(first, c + 0.4 * (main - c))            # first profit must be worth taking after costs
    first = min(first, c + 0.75 * (main - c))
    cut = min(cut, c * 0.95)
    trade.update(target_value=main, stop_stock=stop, max_loss_rule=settings["max_loss_rule"])
    trade["plan"] = dict(first_price=first, first_pct=first / c - 1, first_stock=first_stock,
                         main_price=main, main_pct=main / c - 1, target_stock=target_stock,
                         cut_price=cut, cut_pct=cut / c - 1, stop_stock=stop, time_exit=trade["exit_date"],
                         time_exit_days=te,
                         thesis=(f"Stock closes {'below' if call else 'above'} ${stop:,.2f}"))
    return trade


def buy_trigger(trade, st, settings, tech, sigma, S0):
    """Highest premium at which the trade still meets the EV, probability and fair-value rules."""
    c_ev = st["mean_v"] / (1 + settings["min_ev"])
    c_pop = st["v_pct"][int(round(100 * (1 - settings["min_pop"])))]
    c_fair = trade["fair"] * (1 + settings["fair_tolerance"]) if ok(trade["fair"]) and trade["fair"] > 0.01 else float("inf")
    upper = max(0.0, min(c_ev, c_pop, c_fair))
    daily = abs(trade["delta"]) * S0 * sigma / math.sqrt(252)
    lower = max(0.01, upper - daily)
    breakout = None
    if trade["legs"][0]["kind"] == "call" and tech["high60"] > S0 * 1.01:
        breakout = f"or stock breakout > ${tech['high60']:,.2f}"
    elif trade["legs"][0]["kind"] == "put" and tech["low60"] < S0 * 0.99:
        breakout = f"or stock breakdown < ${tech['low60']:,.2f}"
    binding = min((("expected value", c_ev), ("probability", c_pop), ("fair value", c_fair)), key=lambda x: x[1])[0]
    return dict(lower=lower, upper=upper, ok_now=trade["ask"] <= upper + 1e-9, breakout=breakout,
                binding=binding, limits=dict(ev=c_ev, pop=c_pop, fair=None if c_fair == float("inf") else c_fair))


def simulator(st):
    k = 1000
    return dict(
        buckets=[dict(label="Worst realistic", value=k * (1 + st["q05"]), note="1 in 20 worse"),
                 dict(label="Weak", value=k * (1 + st["q25"]), note="1 in 4 worse"),
                 dict(label="Most likely", value=k * (1 + st["median"]), note="median"),
                 dict(label="Good", value=k * (1 + st["q75"]), note="1 in 4 better"),
                 dict(label="Exceptional", value=k * (1 + st["q95"]), note="1 in 20 better")],
        chances=dict(profit=st["p_profit"], plus50=st["p_50"], plus100=st["p_100"], plus200=st["p_200"],
                     lose50=st["p_loss50"], lose_most=st["p_lose_most"]),
        ev=st["ev_dollars"], n_paths=st["n_paths"],
        label="Simulated estimates following the trade plan - not guaranteed outcomes")


def risk_level(st):
    x = st["p_total_loss_no_stop"]
    return "Low" if x < 0.15 else "Medium" if x < 0.30 else "High" if x < 0.45 else "Very High"


# ------------------------------------------------------------------ ENTRY engine
def entry_decision(score, st, trade, exp, com, trigger, settings, chain_status, dq):
    iv_rv = exp.get("iv_rv")
    atm = exp.get("atm_iv")
    iv_ok = (not ok(iv_rv) or iv_rv <= settings["max_iv_rv"]) and (not ok(atm) or atm <= settings["max_atm_iv"])
    chain_ok = chain_status in ("LIVE", "15-MIN DELAY", "SYNTHETIC")
    C = lambda name, passed, detail: dict(name=name, passed=bool(passed), detail=detail)
    conds = [
        C(f"Entry Score ≥ {settings['entry_min_score']}", score >= settings["entry_min_score"], f"{score:.0f}"),
        C("Expected value > 0", st["mean"] > 0, f"{st['mean']:+.0%}"),
        C(f"Probability of profit ≥ {settings['min_pop']:.0%}", st["p_profit"] >= settings["min_pop"], f"{st['p_profit']:.0%}"),
        C(f"Reward/Risk ≥ {settings['min_rr']:.1f}", st["rr"] >= settings["min_rr"], f"{st['rr']:.2f}"),
        C(f"Liquidity (OI ≥ {settings['min_open_interest']})", trade["oi"] >= settings["min_open_interest"], f"{trade['oi']:,.0f}"),
        C(f"Bid/ask spread ≤ {settings['max_spread_pct']:.0%}", trade["spread_pct"] <= settings["max_spread_pct"], f"{trade['spread_pct']:.1%}"),
        C("Implied volatility acceptable", iv_ok, f"IV/RV {iv_rv:.2f}" if ok(iv_rv) else "n/a"),
        C("Risk manager: no vetoes", not com["vetoes"], "; ".join(com["vetoes"]) or "clear"),
        C("Bull case stronger than bear case", com["contrarian_pass"], f"{com['bull_w']:.1f} vs {com['bear_w']:.1f}"),
        C("Premium at or below buy trigger", trigger["ok_now"], f"ask ${trade['ask']:.2f} vs ${trigger['upper']:.2f}"),
        C("Current option chain", chain_ok, chain_status),
    ]
    by = {c["name"]: c["passed"] for c in conds}
    nonprice = all(c["passed"] for c in conds if c["name"].startswith(("Liquidity", "Bid/ask", "Implied")))
    if all(by.values()):
        state = "BUY NOW"
        head = f"BUY NOW at ≤ ${trigger['upper']:.2f}"
    elif (nonprice and dq != "LOW" and trigger["upper"] >= 0.5 * trade["ask"] and trigger["upper"] > 0.05
          and score >= settings["entry_min_score"] - 12 and com["contrarian_pass"]):
        state = "BUY IF TRIGGERED"
        head = f"BUY IF ${trigger['upper']:.2f} OR BELOW"
        if all(v for k, v in by.items() if k != "Current option chain"):
            head += " (confirm with a current quote)"
    elif score >= settings["wait_min_score"]:
        state, head = "WAIT", "WAIT - not good enough yet"
    else:
        state, head = "AVOID", "AVOID"
    return state, head, conds


def mind_changers(trade, trigger, state):
    p = trade["plan"]
    out = [f"{p['thesis']} -> thesis invalidated",
           f"Re-run shows expected value below zero or probability of profit falls materially"]
    if state in ("BUY IF TRIGGERED", "WAIT"):
        out.insert(0, f"Premium falls to ${trigger['upper']:.2f} or below with the score intact -> BUY")
    if state == "BUY NOW":
        out.insert(0, f"Premium rises above ${trigger['upper']:.2f} -> no longer worth buying")
    out.append("Implied volatility jumps (options get expensive) or the bid/ask spread widens")
    return out


# ------------------------------------------------------------------ HOLD + EXIT PRESSURE engines
def hold_decision(pos, row, st_hold, tech, fund_dir, macro_dir, tech_dir, td, would_buy, plan):
    S0, call = td.price, pos["kind"] == "call"
    entry = float(pos["entry_premium"])
    bid, mid = row["bid"], row["mid"]
    gain = mid / entry - 1 if entry > 0 else 0.0
    ev, pop, rr = st_hold["mean"], st_hold["p_profit"], st_hold["rr"]
    p_recover = float(np.mean(np.array(st_hold["v_pct"]) >= entry))
    dte = row["dte"]
    thesis_level = pos.get("thesis_level") or plan["stop_stock"]
    broken = (S0 <= thesis_level) if call else (S0 >= thesis_level)
    theta_burn = abs(row["theta"]) / mid if mid > 0 else 1.0

    ep, why_ep = 0.0, []
    def add(pts, why):
        nonlocal ep
        ep += pts
        why_ep.append(f"{why} (+{pts:.0f})")
    if broken: add(30, f"thesis level ${thesis_level:,.2f} broken")
    if ev < 0: add(min(25, -ev * 100), f"remaining EV {ev:+.0%}")
    if pop < 0.2: add(15, f"probability of gaining from here only {pop:.0%}")
    elif pop < 0.3: add(10, f"probability of gaining from here {pop:.0%}")
    if dte < 14: add(15, f"only {dte} days left")
    elif dte < 30: add(7, f"{dte} days left")
    if theta_burn > 0.02: add(10, f"time decay {theta_burn:.1%}/day")
    if tech_dir < 40: add(8, "price trend has turned against the position")
    if macro_dir < 40: add(5, "macro backdrop against the position")
    if rr < 0.7: add(10, f"remaining upside/downside {rr:.2f}")
    if fund_dir < 40: add(5, "fundamentals argue the other way")
    ep = min(100.0, ep)
    hold = max(0.0, min(100.0, 55 + 60 * math.tanh(2 * ev) + 40 * (pop - 0.4) - 0.6 * ep + 8 * min(1.5, rr - 1)))

    loss_kind = None
    if gain < 0:
        loss_kind = "THESIS FAILURE" if broken or ev < -0.1 else "PRICE LOSS - thesis intact"
    if ep >= 75 or dte <= 3:
        action, reason = "EXIT NOW", "Exit pressure is extreme: " + "; ".join(why_ep[:3])
    elif ep >= 60:
        action, reason = "SELL", "Exiting is now mathematically preferable: " + "; ".join(why_ep[:3])
    elif gain > 0 and (rr < 0.8 or ev < 0):
        action = "TAKE 50% PROFIT" if gain >= 0.5 else "TAKE 25% PROFIT"
        reason = f"Remaining upside/downside is only {rr:.2f} with EV {ev:+.0%} from today."
    elif gain >= 0.5 and rr < 1.3:
        action, reason = "TAKE 25% PROFIT", f"Up {gain:+.0%}; remaining reward ({rr:.2f}x risk) has fallen relative to downside."
    elif would_buy == "YES" and hold >= 65:
        action, reason = "ADD / BUY", "AI would buy this exact contract today; thesis and EV remain strong."
    elif hold >= 55:
        action, reason = "HOLD", f"Thesis intact; EV from today {ev:+.0%}, upside/downside {rr:.2f}."
    else:
        action, reason = "HOLD / WATCH", f"Mixed: EV from today {ev:+.0%}, probability {pop:.0%}. Watch the thesis level."
    if loss_kind == "PRICE LOSS - thesis intact" and action in ("HOLD", "HOLD / WATCH"):
        reason += " Loss is price-only; the thesis has not failed."
    return dict(action=action, reason=reason, hold_score=hold, exit_pressure=ep, exit_reasons=why_ep,
                ev=ev, pop=pop, rr=rr, p_recover=p_recover, gain=gain, loss_kind=loss_kind,
                thesis_level=thesis_level, thesis_broken=bool(broken), dte=dte, theta_burn=theta_burn,
                upside=st_hold["upside"], downside=st_hold["downside"])


ACTION_URGENCY = {"EXIT NOW": 100, "SELL": 90, "TAKE 50% PROFIT": 80, "TAKE 25% PROFIT": 70,
                  "BUY TRIGGERED": 60, "ADD / BUY": 50, "HOLD / WATCH": 30, "HOLD": 10, "DATA UNAVAILABLE": 20}
