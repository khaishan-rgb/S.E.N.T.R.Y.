"""Glue between data, engines and decisions. Produces JSON-safe payloads for the UI."""
import copy
import datetime as dt
import math

import numpy as np

from . import config, engines, decisions as D
from .providers import load_ticker, iso
from .simulate import simulate_regimes, evaluate, time_path, profit_table, cal_to_td

ok = engines.ok


def _num(x, nd=4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


def context(td, macro, bench):
    r, q = macro["r"], td.dividend_yield
    tech = engines.technical_engine(td.hist, bench)
    fund = engines.fundamental_engine(td)
    exp = engines.expectation_engine(td, tech, r)
    rv_fair = engines.mean_ok([0.6 * tech["rv20"] + 0.4 * tech["rv252"] if ok(tech["rv252"]) else tech["rv20"]], 0.3)
    sigma = engines.mean_ok([rv_fair, exp.get("atm_iv")], 0.3)
    earn_cal = (td.earnings_date - dt.date.today()).days if td.earnings_date else None
    earn_sd = 0.0 if td.is_etf else (exp["hist_earn_move"] * 1.25 if ok(exp.get("hist_earn_move")) else 0.05)
    ctx = dict(S0=td.price, r=r, q=q, earn_cal=earn_cal, weights=macro["weights"])
    flags = list(td.flags) + [("MINOR", f) for f in macro["flags"]]
    return dict(tech=tech, fund=fund, exp=exp, rv_fair=rv_fair, sigma=sigma, ctx=ctx, earn_cal=earn_cal,
                earn_sd=earn_sd, flags=flags, r=r, q=q)


def _sims(cx, trades, n_paths):
    cps = {1, 5, 21, 63, 126, 252}
    for t in trades:
        cps |= {t["horizon_td"], t["expiry_td"], *t["monitor_td"]}
    cps = {c for c in cps if c <= max(t["expiry_td"] for t in trades)}
    ec = cx["earn_cal"]
    earn_td = cal_to_td(ec) if ec is not None and ec >= 1 else None
    return simulate_regimes(cx["ctx"]["S0"], cx["sigma"], cps, n_paths, earn_td, cx["earn_sd"])


def score_and_decide(td, cx, trade, st, settings, macro, direction):
    """Entry score (spec 4/20), committee (12/15), contrarian (13), buy trigger (16), entry state."""
    tech, fund, exp = cx["tech"], cx["fund"], cx["exp"]
    bull = direction == "BULLISH"
    fund_dir = fund["score"] if bull else 100 - fund["score"]
    val_dir = fund["valuation"] if bull else 100 - fund["valuation"]
    growth_dir = fund["growth"] if bull else 100 - fund["growth"]
    macro_dir = macro["score"] if bull else 100 - macro["score"]
    tech_dir = tech["score"] if bull else 100 - tech["score"]
    cat = engines.catalyst_engine(td, exp, direction)
    bt = engines.backtest_engine(tech, direction, trade["horizon_td"])
    dq = engines.data_quality(cx["flags"])
    ec = cx["earn_cal"]
    binary = ec is not None and ec < trade["horizon_cal"] and trade["dte"] < 60
    iv_rv = exp.get("iv_rv") if ok(exp.get("iv_rv")) else 1.0
    fair_lab, fair_diff = D.fair_class(trade["mid"], trade["fair"])
    mis = fair_diff if ok(fair_diff) else 0.0
    comp = dict(fundamentals=fund_dir, growth=growth_dir, valuation=val_dir, catalysts=cat["score"], macro=macro_dir,
                technical=tech_dir, options_pricing=engines.clip(60 - 100 * (iv_rv - 1) - 40 * mis),
                mc_ev=engines.clip(50 + 100 * st["mean"]),
                liquidity=engines.clip(100 - 500 * trade["spread_pct"] - (30 if trade["oi"] < 500 else 0)),
                risk_reward=engines.clip(50 + 25 * (st["rr"] - 1)))
    raw = sum(config.SCORE_WEIGHTS[k] * comp[k] for k in comp)
    pen = []
    atm = exp.get("atm_iv") or 0
    if atm > 1.0 or iv_rv > 1.6: pen.append(("Extreme IV", 10))
    if trade["oi"] < 250: pen.append(("Poor liquidity", 5))
    if trade["spread_pct"] > 0.08: pen.append(("Wide bid/ask spread", 8))
    if dq == "LOW": pen.append(("Weak data quality", 10))
    elif dq == "MEDIUM": pen.append(("Delayed/partial data", 4))
    if trade["cost"] and abs(trade["theta"]) / trade["cost"] > 0.015: pen.append(("Excessive theta", 6))
    if binary: pen.append(("Binary-event dependence", 6))
    if fund["leverage_flag"] and bull: pen.append(("Excessive leverage", 5))
    score = engines.clip(raw - sum(p for _, p in pen))
    com = engines.committee(dict(stats=st, trade=trade, exp=exp, fund_dir=fund_dir, val_dir=val_dir, macro_dir=macro_dir,
                                 tech_dir=tech_dir, binary=binary, dq=dq, backtest=bt, S0=td.price,
                                 min_oi=settings["min_open_interest"]))
    trig = D.buy_trigger(trade, st, settings, tech, cx["sigma"], td.price)
    state, head, conds = D.entry_decision(score, st, trade, exp, com, trig, settings, td.chain_status, dq)
    conf = engines.clip(score * {"HIGH": 1.0, "MEDIUM": 0.85, "LOW": 0.6}[dq] - (5 if bt["n"] < 30 else 0))
    return dict(score=score, comp=comp, pen=pen, com=com, trig=trig, state=state, head=head, conds=conds, dq=dq,
                cat=cat, bt=bt, binary=binary, fair=(fair_lab, fair_diff), conf=conf,
                dirs=dict(fund=fund_dir, macro=macro_dir, tech=tech_dir))


def contract_name(t):
    legs = t["legs"]
    k = legs[0]["kind"].upper()
    d = dt.date.fromisoformat(t["expiry"]).strftime("%d %b %Y").upper()
    if len(legs) == 2:
        return f"{d} ${legs[0]['K']:g}/${legs[1]['K']:g} {k} SPREAD"
    return f"{d} ${legs[0]['K']:g} {k}"


def _chart(td, n=260):
    h = td.hist.tail(n)
    o = h["Open"] if "Open" in h else h["Close"]
    hi = h["High"] if "High" in h else h["Close"]
    lo = h["Low"] if "Low" in h else h["Close"]
    return dict(d=[x.strftime("%Y-%m-%d") for x in h.index], o=[_num(x, 2) for x in o], h=[_num(x, 2) for x in hi],
                l=[_num(x, 2) for x in lo], c=[_num(x, 2) for x in h["Close"]])


def _key_catalyst(td, exp):
    parts = []
    if td.earnings_date:
        parts.append(f"Earnings {td.earnings_date.strftime('%d %b')}")
    if ok(exp.get("analyst_upside")):
        parts.append(f"Analysts {exp['analyst_upside']:+.0%}")
    if td.news.get("count"):
        parts.append(f"News {td.news['sentiment']:+.1f}")
    return ", ".join(parts) or "No scheduled catalyst"


def payload(td, cx, trade, st, dec, macro, mode, alternatives=(), sims=None):
    """JSON-safe report used by every UI view (spec 10-14, 21, 25)."""
    tech, fund, exp, com = cx["tech"], cx["fund"], cx["exp"], dec["com"]
    plan = trade["plan"]
    trig = dec["trig"]
    n = _num
    why = [txt.rstrip(".") + "." for _, txt in sorted(com["bull"], key=lambda x: -x[0])[:2]]
    fl, fd = dec["fair"]
    why.append(f"Option premium is {fl.lower()} vs model fair value ({fd:+.0%})." if ok(fd) else "Fair value unavailable.")
    risks = [txt for _, txt in sorted(com["bear"], key=lambda x: -x[0])[:3]] or ["You can lose the whole premium."]
    sections = {
        "Fundamentals": dict(score=fund["score"], notes=fund["notes"]),
        "Earnings": dict(score=fund["growth"], notes=[f"Next earnings: {td.earnings_date or 'not scheduled / unknown'}"]
                         + [x for x in exp["notes"] if "earnings" in x.lower()]),
        "Valuation": dict(score=fund["valuation"], notes=[x for x in fund["notes"] if "yield" in x.lower()]
                          + [f"Forward P/E {td.info.get('forwardPE'):.1f}" if td.info.get("forwardPE") else "Forward P/E n/a"]),
        "Catalysts": dict(score=dec["cat"]["score"], notes=[f"{c['event']} ({c['date']}): {c['impact']}; priced in? {c['priced_in']}"
                                                           for c in dec["cat"]["catalysts"]] or ["No catalysts found"]),
        "Macro": dict(score=macro["score"], notes=[f"{macro['label']}"] + macro["notes"]),
        "Technical": dict(score=tech["score"], notes=tech["notes"] + [f"RSI {tech['rsi']:.0f}",
                          f"60-day range ${tech['low60']:,.2f} - ${tech['high60']:,.2f}"]),
        "Option Pricing": dict(score=dec["comp"]["options_pricing"], notes=exp["notes"][:2] + [
            f"Market ${trade['mid']:.2f} vs model ${trade['fair']:.2f} -> {fl}"]),
        "Monte Carlo": dict(score=dec["comp"]["mc_ev"], notes=[
            f"{st['n_paths']:,} pooled paths, {len(config.REGIMES)} regimes, vol {cx['sigma']:.0%}",
            f"Profit chance {st['p_profit']:.0%} following the plan; {st['p_profit_expiry']:.0%} if held to expiry"]),
        "Liquidity": dict(score=dec["comp"]["liquidity"], notes=[f"Open interest {trade['oi']:,.0f}",
                          f"Spread {trade['spread_pct']:.1%} of mid"]),
        "Risk/Reward": dict(score=dec["comp"]["risk_reward"], notes=[f"Upside/downside {st['rr']:.2f}",
                            f"Chance of losing most: {st['p_lose_most']:.0%}"]),
    }
    sec_out = {k: dict(score=n(v["score"], 1), notes=v["notes"]) for k, v in sections.items()}
    c0 = trade["contracts"][0]
    return dict(
        ticker=td.ticker, name=td.name, direction=trade["direction"], mode=mode, ts=iso(),
        price=n(td.price, 2), change_pct=n(td.change_pct), price_time=td.price_time, chain_status=td.chain_status,
        state=dec["state"], headline=dec["head"], score=n(dec["score"], 1), confidence=n(dec["conf"], 0),
        grade=engines.grade(dec["score"]), dq=dec["dq"], risk_level=D.risk_level(st),
        contract=dict(name=contract_name(trade), strategy=trade["strategy"], kind=trade["kind"], moneyness=trade["moneyness"],
                      legs=[dict(K=l["K"], kind=l["kind"], sign=l["sign"]) for l in trade["legs"]],
                      expiry=trade["expiry"], dte=trade["dte"], ask=n(trade["ask"], 2), bid=n(trade["bid"], 2),
                      mid=n(trade["mid"], 2), iv=n(trade["iv"]), delta=n(trade["delta"], 3), gamma=n(trade["gamma"], 4),
                      theta=n(trade["theta"], 3), vega=n(trade["vega"], 3), oi=n(trade["oi"], 0),
                      volume=n(c0.get("volume"), 0), spread_pct=n(trade["spread_pct"]), breakeven=n(trade["breakeven"], 2),
                      max_loss=n(trade["max_loss"], 2), max_gain=n(trade["max_gain"], 2),
                      per_1000=int(config.INVESTMENT // (trade["cost"] * 100)) if trade["cost"] > 0 else 0),
        fair=dict(market=n(trade["mid"], 2), model=n(trade["fair"], 2), diff=n(fd), label=fl),
        trigger=dict(lower=n(trig["lower"], 2), upper=n(trig["upper"], 2), ok_now=trig["ok_now"],
                     breakout=trig["breakout"], binding=trig["binding"]),
        plan={k: (n(v, 4) if isinstance(v, (int, float)) else v) for k, v in plan.items()},
        sim=D.simulator(st), stats={k: n(v) for k, v in st.items() if k != "v_pct"},
        conditions=dec["conds"], why=why[:3], risks=risks, mind_changers=D.mind_changers(trade, trig, dec["state"]),
        components={k: n(v, 1) for k, v in dec["comp"].items()}, penalties=dec["pen"], sections=sec_out,
        committee=dict(votes=com["votes"], bull=[t for _, t in com["bull"]], bear=[t for _, t in com["bear"]],
                       vetoes=com["vetoes"], contrarian_pass=com["contrarian_pass"], contrarian=com["contrarian"],
                       bull_w=com["bull_w"], bear_w=com["bear_w"]),
        time_path=[{k: (n(v, 3) if not isinstance(v, str) else v) for k, v in r.items()} for r in (time_path(trade, sims, cx["ctx"]) if sims else [])],
        profit_table=[{k: n(v, 3) for k, v in r.items()} for r in profit_table(trade, cx["ctx"])],
        alternatives=[dict(name=contract_name(a), moneyness=a["moneyness"], strategy=a["strategy"], cost=n(a["cost"] * 100, 2),
                           pop=n(a["stats"]["p_profit"]), ev=n(a["stats"]["mean"]), tl=n(a["stats"]["p_total_loss_no_stop"]))
                      for a in alternatives],
        backtest={k: (n(v) if isinstance(v, (int, float)) else v) for k, v in dec["bt"].items()},
        flags=[dict(severity=s, message=m) for s, m in cx["flags"]], sources=td.sources,
        key_catalyst=_key_catalyst(td, exp), earnings_date=str(td.earnings_date) if td.earnings_date else None,
        iv_rv=n(exp.get("iv_rv")), atm_iv=n(exp.get("atm_iv")), regime=macro["label"],
        chart=_chart(td),
    )


def analyse(td, macro, bench, settings, mode="GROWTH"):
    """Opportunity analysis for one ticker in one mode. Returns list of payloads (per direction)."""
    if td.price is None or td.hist is None or not td.chains:
        return [dict(ticker=td.ticker, state="AVOID", error="; ".join(m for _, m in td.flags) or "no data")]
    cx = context(td, macro, bench)
    composite = 0.5 * cx["tech"]["score"] + 0.25 * cx["fund"]["score"] + 0.25 * macro["score"]
    dirs = (["BULLISH"] if composite >= 45 else []) + (["BEARISH"] if composite <= 55 else [])
    n_paths = 50000 if settings.get("high_accuracy") else int(settings["mc_paths"])
    out = []
    for direction in dirs:
        cands, rejected = engines.build_candidates(td, direction, cx["r"], cx["rv_fair"], settings)
        if not cands:
            out.append(dict(ticker=td.ticker, direction=direction, state="AVOID",
                            error="No liquid contract passed filters" + (" - option quotes unavailable (market closed?)"
                                                                         if td.chain_status == "UNAVAILABLE" else ""),
                            rejected=rejected))
            continue
        for t in cands:
            D.apply_plan(t, cx["tech"], cx["sigma"], cx["ctx"], settings)
        sims = _sims(cx, cands, n_paths)
        for t in cands:
            t["stats"] = evaluate(t, sims, cx["ctx"])
        cands.sort(key=lambda t: engines.objective(t["stats"]), reverse=True)
        best = cands[0]
        dec = score_and_decide(td, cx, best, best["stats"], settings, macro, direction)
        p = payload(td, cx, best, best["stats"], dec, macro, mode, cands[1:4], sims)
        p["rejected"] = rejected
        out.append(p)
    return out


def scan_ticker(sym, macro, bench, settings, mode):
    w = config.MODES[mode]
    td = load_ticker(sym, w["min_dte"], w["max_dte"])
    return analyse(td, macro, bench, settings, mode)


# ------------------------------------------------------------------ owned positions (spec 5, 6, 17, 18)
def evaluate_position(pos, macro, bench, settings):
    td = load_ticker(pos["ticker"], expiries=[pos["expiry"]])
    base = dict(id=pos.get("id"), ticker=pos["ticker"], ts=iso(), chain_status=td.chain_status,
                price=_num(td.price, 2), change_pct=_num(td.change_pct))
    if td.price is None or td.hist is None or pos["expiry"] not in td.chains:
        return dict(base, action="DATA UNAVAILABLE", reason="No option chain for this expiry right now.",
                    flags=[dict(severity=s, message=m) for s, m in td.flags])
    cx = context(td, macro, bench)
    rows, _ = engines._rows(td, pos["expiry"], pos["kind"], cx["r"], cx["q"], cx["rv_fair"])
    row = next((x for x in rows if abs(x["K"] - float(pos["strike"])) < 1e-6), None)
    if row is None:
        return dict(base, action="DATA UNAVAILABLE",
                    reason="No current bid/ask for this contract (market closed or illiquid). Nothing is estimated.",
                    flags=[dict(severity=s, message=m) for s, m in cx["flags"]])
    direction = "BULLISH" if pos["kind"] == "call" else "BEARISH"
    lab = "slightly ITM" if abs(row["delta"]) > 0.62 else "ATM" if abs(row["delta"]) > 0.47 else \
          "slightly OTM" if abs(row["delta"]) > 0.33 else "OTM"
    t_buy = engines._make_trade(td, direction, f"Long {pos['kind'].title()}", [row], lab)
    D.apply_plan(t_buy, cx["tech"], cx["sigma"], cx["ctx"], settings)
    t_hold = copy.deepcopy(t_buy)
    t_hold["cost"] = max(row["bid"], 0.01)          # value TODAY if sold - no sunk-cost anchoring
    D.apply_plan(t_hold, cx["tech"], cx["sigma"], cx["ctx"], settings)
    t_hold["target_value"] = t_buy["target_value"]
    n_paths = 50000 if settings.get("high_accuracy") else int(settings["mc_paths"])
    sims = _sims(cx, [t_buy, t_hold], n_paths)
    st_buy, st_hold = evaluate(t_buy, sims, cx["ctx"]), evaluate(t_hold, sims, cx["ctx"])
    dec = score_and_decide(td, cx, t_buy, st_buy, settings, macro, direction)
    would = {"BUY NOW": "YES", "BUY IF TRIGGERED": "ONLY AT LOWER PRICE"}.get(dec["state"], "NO")
    hold = D.hold_decision(pos, row, st_hold, cx["tech"], dec["dirs"]["fund"], dec["dirs"]["macro"],
                           dec["dirs"]["tech"], td, would, t_buy["plan"])
    qty = float(pos["qty"])
    entry = float(pos["entry_premium"])
    report = payload(td, cx, t_buy, st_buy, dec, macro, "POSITION", (), sims)
    return dict(base, action=hold["action"], reason=hold["reason"], hold=hold,
                would_buy=would, would_buy_price=_num(dec["trig"]["upper"], 2),
                mark=_num(row["mid"], 2), bid=_num(row["bid"], 2), ask=_num(row["ask"], 2),
                value=_num(row["mid"] * qty * 100, 2), cost_basis=_num(entry * qty * 100, 2),
                pnl=_num((row["mid"] - entry) * qty * 100, 2), pnl_pct=_num(row["mid"] / entry - 1 if entry else None),
                day_change=_num(row["change"] * qty * 100 if row.get("change") is not None else None, 2), dte=row["dte"], theta_day=_num(row["theta"] * qty * 100, 2),
                iv=_num(row["iv"]), delta=_num(row["delta"], 3), report=report,
                entry_score=pos.get("entry_score"), sim_hold=D.simulator(st_hold))
