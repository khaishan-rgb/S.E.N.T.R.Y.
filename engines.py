"""All analysis engines (spec 2-20). Agents are transparent rule-based evaluators, not black boxes."""
import datetime as dt
import math

import numpy as np
import pandas as pd

from . import config
from .pricing import bs_price, greeks, binomial_american, implied_vol
from .simulate import cal_to_td, simulate_regimes, evaluate, time_path, profit_table

NAN = float("nan")


def clip(x, lo=0.0, hi=100.0):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 50.0
    return float(max(lo, min(hi, x)))


def ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def mean_ok(vals, default=50.0):
    v = [x for x in vals if ok(x)]
    return float(np.mean(v)) if v else default


# =========================================================== 2. BUFFETT ENGINE
def fundamental_engine(td):
    f, info = td.fundamentals, td.info
    if td.is_etf or not f:
        return dict(score=50.0, growth=50.0, valuation=50.0, quality=50.0, balance=50.0, leverage_flag=False,
                    coverage="NONE", notes=["No company fundamentals (ETF or not in EDGAR): neutral 50"])
    notes = []
    rev, fcf = f.get("revenue"), f.get("fcf")
    growth = mean_ok([clip(50 + 200 * f["rev_growth"]) if ok(f.get("rev_growth")) else None,
                      clip(50 + 60 * min(f["ni_growth"], 1.5)) if ok(f.get("ni_growth")) else None])
    if ok(f.get("rev_growth")):
        notes.append(f"Revenue growth {f['rev_growth']:+.0%} (latest fiscal year)")
    margin = f["net_income"] / rev if rev and ok(f.get("net_income")) else None
    fcf_m = fcf / rev if rev and ok(fcf) else None
    roe = f["net_income"] / f["equity"] if f.get("equity") and f["equity"] > 0 and ok(f.get("net_income")) else None
    quality = mean_ok([clip(40 + 200 * margin) if ok(margin) else None,
                       clip(40 + 250 * fcf_m) if ok(fcf_m) else None,
                       clip(40 + 150 * roe) if ok(roe) else None])
    if ok(margin):
        notes.append(f"Net margin {margin:.0%}, FCF margin {fcf_m:.0%}" if ok(fcf_m) else f"Net margin {margin:.0%}")
    cash, debt = f.get("cash", 0) or 0, f.get("debt", 0) or 0
    leverage_flag = False
    if cash >= debt:
        balance = 85.0
        notes.append("Net cash balance sheet")
    elif ok(fcf) and fcf > 0:
        nd = (debt - cash) / fcf
        balance = clip(80 - 10 * nd)
        leverage_flag = nd > 4
        notes.append(f"Net debt = {nd:.1f}x free cash flow")
    else:
        balance, leverage_flag = 25.0, True
        notes.append("Debt exceeds cash and free cash flow is negative")
    mcap = (f.get("shares") or 0) * td.price or info.get("marketCap")
    val_parts = []
    if mcap and ok(fcf):
        fy = fcf / mcap
        val_parts.append(clip(30 + 1000 * fy))
        notes.append(f"FCF yield {fy:.1%}")
    pe = info.get("forwardPE")
    if pe and pe > 0:
        val_parts.append(clip(95 - 1.5 * pe))
    if mcap and rev:
        ps = mcap / rev
        val_parts.append(clip(90 - 3 * ps))
    valuation = mean_ok(val_parts)
    score = 0.3 * growth + 0.3 * quality + 0.2 * balance + 0.2 * valuation
    return dict(score=score, growth=growth, valuation=valuation, quality=quality, balance=balance,
                leverage_flag=leverage_flag, coverage="EDGAR annual", notes=notes)


# =========================================================== 3. SOROS ENGINE
def macro_engine(fred):
    s, notes, flags = 50.0, [], []

    def last(sid):
        ser = fred.get(sid)
        return (float(ser.iloc[-1]), ser.index[-1]) if ser is not None and len(ser) else (None, None)

    def change(sid, days, pct=False):
        ser = fred.get(sid)
        if ser is None or len(ser) < 5:
            return None
        past = ser[ser.index <= ser.index[-1] - pd.Timedelta(days=days)]
        if past.empty:
            return None
        return ser.iloc[-1] / past.iloc[-1] - 1 if pct else ser.iloc[-1] - past.iloc[-1]

    vix, vd = last("VIXCLS")
    if vix is not None:
        adj = 10 if vix < 16 else 0 if vix < 25 else -12 if vix < 35 else -25
        s += adj
        notes.append(f"VIX {vix:.1f}")
    hy, _ = last("BAMLH0A0HYM2")
    if hy is not None:
        s += 8 if hy < 3.5 else -12 if hy > 5 else 0
        notes.append(f"High-yield spread {hy:.2f}%")
        c = change("BAMLH0A0HYM2", 30)
        if c is not None and c > 0.5:
            s -= 10
            notes.append("Credit spreads widening fast (risk-off)")
    c10 = change("DGS10", 30)
    if c10 is not None:
        if c10 > 0.35:
            s -= 8
            notes.append(f"10y yield up {c10:+.2f} pts in a month (rate-shock risk)")
        elif c10 < -0.35:
            s += 3
    curve, _ = last("T10Y2Y")
    if curve is not None and curve < 0:
        s -= 5
        notes.append("Yield curve inverted")
    usd = change("DTWEXBGS", 90, pct=True)
    if usd is not None and usd > 0.04:
        s -= 5
        notes.append("Dollar up >4% in 3 months (headwind)")
    oil = change("DCOILWTICO", 90, pct=True)
    if oil is not None and oil > 0.25:
        s -= 4
        notes.append("Oil up >25% in 3 months (inflation risk)")
    for sid, ser in fred.items():
        if len(ser) and (pd.Timestamp.now() - ser.index[-1]).days > 10:
            flags.append(f"FRED {sid} last value {ser.index[-1].date()} (stale)")
    if not fred:
        flags.append("No macro data (FRED unreachable): macro neutral 50")
    r3, _ = last("DGS3MO")
    s = clip(s)
    label = "RISK-ON" if s >= 60 else "RISK-OFF" if s <= 40 else "NEUTRAL"
    weights = dict(config.REGIME_WEIGHTS)
    if s <= 40:
        weights["bull"] -= 0.05; weights["bear"] += 0.05
    elif s >= 65:
        weights["bull"] += 0.05; weights["bear"] -= 0.05
    return dict(score=s, label=label, notes=notes, flags=flags, r=(r3 / 100 if r3 else 0.04),
                r_source="FRED DGS3MO" if r3 else "ASSUMED 4% (FRED unavailable)", weights=weights,
                as_of=str(vd.date()) if vd is not None else "n/a")


# =========================================================== 6. TECHNICAL ENGINE
def _rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def realized_vol(c, window):
    lr = np.log(c / c.shift(1)).dropna()
    return float(lr.tail(window).std() * np.sqrt(252)) if len(lr) >= window else NAN


def technical_engine(hist, bench=None):
    c, v = hist["Close"].dropna(), hist["Volume"]
    last = float(c.iloc[-1])
    sma = {n: c.rolling(n).mean() for n in (20, 50, 100, 200)}
    s = {n: float(sma[n].iloc[-1]) if len(c) >= n else NAN for n in sma}
    rsi_s = _rsi(c)
    rsi = float(rsi_s.iloc[-1])
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    macd_hist = float((macd - macd.ewm(span=9, adjust=False).mean()).iloc[-1])
    rel_vol = float(v.iloc[-1] / v.tail(20).mean()) if v.tail(20).mean() else NAN
    rs = NAN
    if bench is not None and len(bench) > 63 and len(c) > 63:
        rs = (c.iloc[-1] / c.iloc[-64] - 1) - (bench["Close"].iloc[-1] / bench["Close"].iloc[-64] - 1)
    hi252, low60, hi60 = float(c.tail(252).max()), float(c.tail(60).min()), float(c.tail(60).max())
    gt = lambda a, b: ok(a) and ok(b) and a > b
    p, notes = 15, []
    p += 10 if gt(last, s[20]) else 0
    p += 10 if gt(last, s[50]) else 0
    p += 15 if gt(last, s[200]) else 0
    p += 10 if gt(s[50], s[200]) else 0
    if 50 <= rsi <= 70:
        p += 10
    elif rsi > 78:
        p -= 10; notes.append(f"RSI {rsi:.0f}: overbought")
    elif rsi < 30:
        notes.append(f"RSI {rsi:.0f}: oversold")
    p += 10 if macd_hist > 0 else 0
    p += 10 if gt(rs, 0) else 0
    if ok(rel_vol) and rel_vol > 1.2 and c.iloc[-1] > c.iloc[-2]:
        p += 5; notes.append(f"Up day on {rel_vol:.1f}x volume (accumulation)")
    if last >= 0.95 * hi252:
        p += 5; notes.append("Within 5% of 52-week high (breakout zone)")
    trend = "UP" if gt(last, s[50]) and gt(s[50], s[200]) else "DOWN" if gt(s[50], last) and gt(s[200], s[50]) else "MIXED"
    notes.insert(0, f"Trend {trend}; price vs 50d {last / s[50] - 1:+.1%}" if ok(s[50]) else f"Trend {trend}")
    rv20_series = (np.log(c / c.shift(1)).rolling(20).std() * np.sqrt(252)).dropna().tail(252)
    feats = pd.DataFrame({"close": c, "rsi": rsi_s, "dist50": c / sma[50] - 1,
                          "up": (c > sma[50]) & (sma[50] > sma[200]), "down": (c < sma[50]) & (sma[50] < sma[200])})
    return dict(score=clip(p), trend=trend, rsi=rsi, macd_hist=macd_hist, rel_vol=rel_vol, rel_strength=rs,
                sma=s, low60=low60, high60=hi60, hi252=hi252, last=last, notes=notes,
                rv20=realized_vol(c, 20), rv60=realized_vol(c, 60), rv252=realized_vol(c, 252),
                rv20_series=rv20_series, feats=feats)


# =========================================================== 11. BACKTEST ENGINE
def backtest_engine(tech, direction, horizon_td):
    """'When this stock setup appeared before, what did the STOCK do next?' (options history isn't free)."""
    f = tech["feats"].dropna()
    now = f.iloc[-1]
    fwd = f["close"].shift(-horizon_td) / f["close"] - 1
    trend_col = "up" if tech["trend"] == "UP" else "down" if tech["trend"] == "DOWN" else None
    mask = ((f["rsi"] - now["rsi"]).abs() <= 8) & ((f["dist50"] - now["dist50"]).abs() <= 0.04)
    if trend_col:
        mask &= f[trend_col]
    sample = fwd[mask].iloc[:-horizon_td if horizon_td < len(f) else None].dropna().iloc[::5]  # thin overlap
    if direction == "BEARISH":
        sample = -sample
    n = len(sample)
    if n == 0:
        return dict(n=0, confidence="LOW CONFIDENCE", note="No comparable historical setups")
    gains, losses = sample[sample > 0].sum(), -sample[sample < 0].sum()
    return dict(n=n, win_rate=float((sample > 0).mean()), median=float(sample.median()), mean=float(sample.mean()),
                worst=float(sample.min()), profit_factor=float(gains / losses) if losses > 0 else float("inf"),
                confidence="LOW CONFIDENCE" if n < 30 else "MODERATE",
                note="Stock-level backtest on ~5y single-ticker history; survivorship bias possible (today's universe)")


# =========================================================== 5. MARKET EXPECTATION ENGINE
def _mid(row):
    b, a = row.get("bid"), row.get("ask")
    if not (ok(b) and ok(a)) or b <= 0 or a <= 0 or a < b:
        return None
    return (b + a) / 2


def _atm_iv(td, exp, r, q):
    T = max((dt.date.fromisoformat(exp) - dt.date.today()).days, 1) / 365
    ivs = []
    for kind, key in (("call", "calls"), ("put", "puts")):
        df = td.chains[exp][key]
        if df.empty:
            continue
        row = df.iloc[(df["strike"] - td.price).abs().argsort().iloc[0]]
        m = _mid(row)
        iv = implied_vol(m, td.price, row["strike"], T, r, kind, q) if m else None
        if iv:
            ivs.append((iv, m))
    if not ivs:
        return None, None
    return float(np.mean([i for i, _ in ivs])), sum(m for _, m in ivs) / td.price  # iv, straddle as % of spot


def expectation_engine(td, tech, r):
    q = td.dividend_yield
    term = []
    for e in sorted(td.chains):
        iv, straddle = _atm_iv(td, e, r, q)
        if iv:
            term.append(dict(expiry=e, dte=(dt.date.fromisoformat(e) - dt.date.today()).days, iv=iv, straddle=straddle))
    rv_ref = mean_ok([tech["rv20"], tech["rv60"]], default=NAN)
    out = dict(term=term, rv_ref=rv_ref, notes=[])
    if not term:
        out.update(atm_iv=NAN, iv_rv=NAN, label="UNKNOWN", iv_pct_proxy=NAN)
        return out
    ref = min(term, key=lambda x: abs(x["dte"] - 45))
    atm = ref["iv"]
    iv_rv = atm / rv_ref if ok(rv_ref) and rv_ref > 0 else NAN
    pct = float((tech["rv20_series"] < atm).mean() * 100) if len(tech["rv20_series"]) else NAN
    label = "UNDERPRICED" if ok(iv_rv) and iv_rv < 0.9 else "OVERPRICED" if ok(iv_rv) and iv_rv > 1.25 else "FAIRLY PRICED"
    out.update(atm_iv=atm, iv_rv=iv_rv, iv_pct_proxy=pct, label=label,
               term_slope=(term[-1]["iv"] - term[0]["iv"]) if len(term) > 1 else 0.0)
    out["notes"].append(f"ATM IV {atm:.0%} vs realised {rv_ref:.0%} (IV/RV {iv_rv:.2f}) -> options {label}")
    out["notes"].append(f"IV percentile PROXY {pct:.0f} (vs past-year realised vol; true IV history isn't free)")
    out["implied_move_ref"] = ref["straddle"]
    hist_moves = td.past_earnings_moves
    out["hist_earn_move"] = float(np.mean(hist_moves)) if hist_moves else NAN
    out["earn_priced_in"] = None
    if td.earnings_date:
        after = [t for t in term if dt.date.fromisoformat(t["expiry"]) >= td.earnings_date]
        if after and ok(out["hist_earn_move"]):
            imp = after[0]["straddle"]
            out["earn_priced_in"] = imp >= 1.2 * out["hist_earn_move"]
            out["notes"].append(f"Options price ±{imp:.1%} by {after[0]['expiry']} (incl. earnings); "
                                f"avg past earnings move ±{out['hist_earn_move']:.1%}")
    tgt = td.info.get("targetMeanPrice")
    out["analyst_upside"] = tgt / td.price - 1 if tgt else NAN
    if tgt:
        out["notes"].append(f"Analyst mean target {tgt:.2f} ({out['analyst_upside']:+.0%}, "
                            f"{td.info.get('numberOfAnalystOpinions', '?')} analysts)")
    return out


# =========================================================== 4. CATALYST ENGINE
def catalyst_engine(td, exp, direction):
    sgn = 1 if direction == "BULLISH" else -1
    s, cats = 50.0, []
    if td.earnings_date:
        days = (td.earnings_date - dt.date.today()).days
        mv = exp.get("hist_earn_move", NAN)
        cats.append(dict(event="Earnings", date=str(td.earnings_date), probability="Scheduled (~100%)",
                         impact=f"±{mv:.1%} avg historically" if ok(mv) else "unknown",
                         priced_in={True: "Likely yes (options price a bigger move than usual)",
                                    False: "Not fully (options price a smaller move than usual)",
                                    None: "Unknown"}[exp.get("earn_priced_in")], days=days))
        s += 5 if exp.get("earn_priced_in") is False else -5 if exp.get("earn_priced_in") else 0
    up = exp.get("analyst_upside", NAN)
    if ok(up):
        s += sgn * clip(up * 50, -15, 15)
        cats.append(dict(event="Analyst targets", date="ongoing", probability="n/a", impact=f"{up:+.0%} to mean target",
                         priced_in="Partly: consensus is public"))
    if td.news.get("count"):
        se = td.news["sentiment"]
        s += sgn * 12 * se
        cats.append(dict(event=f"News flow ({td.news['count']} headlines, 14d)", date="recent", probability="n/a",
                         impact=f"keyword sentiment {se:+.2f} (crude)", priced_in="Probably: news is fast"))
    return dict(score=clip(s), catalysts=cats)


# =========================================================== 7/8/15/16. OPTIONS EDGE + OPTIMISER
def _rows(td, exp, kind, r, q, rv_fair):
    """All quotable contracts of one expiry with IV, Greeks, model fair value."""
    df = td.chains[exp]["calls" if kind == "call" else "puts"]
    dte = (dt.date.fromisoformat(exp) - dt.date.today()).days
    T = dte / 365
    out, rej = [], {"no_quote": 0, "illiquid": 0, "lottery": 0, "stale": 0}
    now = pd.Timestamp.now(tz="UTC")
    for _, row in df.iterrows():
        m = _mid(row)
        if not m or m < config.MIN_PREMIUM:
            rej["no_quote"] += 1
            continue
        K = float(row["strike"])
        iv = implied_vol(m, td.price, K, T, r, kind, q)
        if not iv:
            rej["no_quote"] += 1
            continue
        ltd = row.get("lastTradeDate")
        if ltd is not None and not (isinstance(ltd, float) and math.isnan(ltd)):
            try:
                lt = pd.Timestamp(ltd)
                lt = lt.tz_localize("UTC") if lt.tzinfo is None else lt
                if (now - lt).days > config.STALE_QUOTE_DAYS:
                    rej["stale"] += 1
                    continue
            except Exception:
                pass
        g = greeks(td.price, K, T, r, iv, kind, q)
        oi = float(row.get("openInterest") or 0) if ok(row.get("openInterest")) else 0.0
        vol = float(row.get("volume") or 0) if ok(row.get("volume")) else 0.0
        spread = (row["ask"] - row["bid"]) / m
        fair_bs = bs_price(td.price, K, T, r, rv_fair, kind, q)
        fair_bin = binomial_american(td.price, K, T, r, rv_fair, kind, q, steps=150)
        fair = (fair_bs + fair_bin) / 2
        chg = row.get("change")
        out.append(dict(kind=kind, K=K, expiry=exp, dte=dte, bid=float(row["bid"]), ask=float(row["ask"]), mid=m,
                        change=float(chg) if ok(chg) else None,
                        iv=iv, oi=oi, volume=vol, spread_pct=spread, hs=spread / 2, fair=fair,
                        mispricing=(m - fair) / fair if fair > 0.01 else NAN, **g))
    return out, rej


def build_candidates(td, direction, r, rv_fair, settings):
    kind = "call" if direction == "BULLISH" else "put"
    sgn = 1 if kind == "call" else -1
    q = td.dividend_yield
    trades, rejected = [], {"no_quote": 0, "illiquid": 0, "lottery": 0, "stale": 0}
    for exp in sorted(td.chains):
        rows, rej = _rows(td, exp, kind, r, q, rv_fair)
        for k in rej:
            rejected[k] += rej[k]
        liquid = []
        for x in rows:
            if abs(x["delta"]) < config.MIN_DELTA:
                rejected["lottery"] += 1
            elif x["oi"] < settings["min_open_interest"] or x["spread_pct"] > settings["max_spread_pct"]:
                rejected["illiquid"] += 1
            else:
                liquid.append(x)
        chosen = []
        for tgt in config.TARGET_DELTAS:
            if liquid:
                best = min(liquid, key=lambda x: abs(abs(x["delta"]) - tgt))
                if abs(abs(best["delta"]) - tgt) < 0.12 and best not in chosen:
                    chosen.append(best)
        for c in chosen:
            label = "slightly ITM" if abs(c["delta"]) > 0.62 else "ATM" if abs(c["delta"]) > 0.47 else \
                    "slightly OTM" if abs(c["delta"]) > 0.33 else "OTM"
            trades.append(_make_trade(td, direction, f"Long {kind.title()}", [c], label))
            # debit spread: sell a further-OTM strike, same expiry
            if abs(c["delta"]) >= 0.45:
                shorts = [x for x in rows if sgn * (x["K"] - c["K"]) >= 0.05 * td.price and x["bid"] > 0
                          and abs(x["delta"]) <= abs(c["delta"]) - 0.10
                          and x["oi"] >= settings["min_open_interest"] / 2 and x["spread_pct"] <= 0.15]
                if shorts:
                    sh = min(shorts, key=lambda x: abs(abs(x["delta"]) - (abs(c["delta"]) - 0.25)))
                    trades.append(_make_trade(td, direction, f"{kind.title()} Debit Spread", [c, sh], label))
    return trades, rejected


def _make_trade(td, direction, strategy, contracts, label):
    long_leg = contracts[0]
    legs = [dict(kind=long_leg["kind"], K=long_leg["K"], dte=long_leg["dte"], sign=1, iv=long_leg["iv"], hs=long_leg["hs"])]
    cost = long_leg["ask"]
    net = {g: long_leg[g] for g in ("delta", "gamma", "theta", "vega")}
    width = None
    if len(contracts) == 2:
        s = contracts[1]
        legs.append(dict(kind=s["kind"], K=s["K"], dte=s["dte"], sign=-1, iv=s["iv"], hs=s["hs"]))
        cost -= s["bid"]
        for g in net:
            net[g] -= s[g]
        width = abs(s["K"] - long_leg["K"])
    sgn = 1 if long_leg["kind"] == "call" else -1
    dte = long_leg["dte"]
    horizon_cal = max(5, int(dte * 0.65))     # provisional; decisions.apply_plan derives the real time exit
    horizon_td = cal_to_td(horizon_cal)
    return dict(ticker=td.ticker, direction=direction, strategy=strategy, moneyness=label, expiry=long_leg["expiry"],
                dte=dte, legs=legs, contracts=contracts, cost=cost, width=width,
                breakeven=long_leg["K"] + sgn * cost, max_loss=cost * 100,
                max_gain=(width - cost) * 100 if width else None, iv=long_leg["iv"], oi=long_leg["oi"],
                spread_pct=max(c["spread_pct"] for c in contracts), mispricing=long_leg["mispricing"],
                fair=long_leg["fair"] - (contracts[1]["fair"] if width else 0), **net,
                horizon_cal=horizon_cal, horizon_td=horizon_td, expiry_td=cal_to_td(dte),
                monitor_td=[], kind=long_leg["kind"], bid=long_leg["bid"] - (contracts[1]["ask"] if width else 0),
                ask=cost, mid=long_leg["mid"] - (contracts[1]["mid"] if width else 0),
                exit_date=str(dt.date.today() + dt.timedelta(days=horizon_cal)))


def objective(st):
    """Probability-adjusted return on capital: Sortino-style EV / downside, nudged by chance of profit."""
    return st["sortino"] * st["p_profit"] * (1 - st["p_total_loss_no_stop"])


# =========================================================== 12/13. COMMITTEE + CONTRARIAN
def committee(c):
    st, tr, ex = c["stats"], c["trade"], c["exp"]
    v = {}
    v["A Buffett"] = "BUY" if c["fund_dir"] >= 60 and c["val_dir"] >= 40 else "AVOID" if c["fund_dir"] < 40 else "WATCH"
    v["B Soros"] = "BUY" if c["macro_dir"] >= 55 else "AVOID" if c["macro_dir"] < 40 else "WATCH"
    v["C Quant"] = "BUY" if st["mean"] > 0.05 and st["p_profit"] >= 0.35 else "AVOID" if st["mean"] < 0 else "WATCH"
    theta_burn = abs(tr["theta"]) / tr["cost"] if tr["cost"] else 1
    v["D Options"] = ("AVOID" if ex["label"] == "OVERPRICED" or tr["spread_pct"] > 0.08 else
                      "BUY" if theta_burn < 0.012 and tr["spread_pct"] <= 0.06 else "WATCH")
    v["E Momentum"] = "BUY" if c["tech_dir"] >= 60 else "AVOID" if c["tech_dir"] < 40 else "WATCH"

    bull, bear = [], []   # (weight, text)
    if st["mean"] > 0: bull.append((2, f"Simulated average return {st['mean']:+.0%} after spreads"))
    else: bear.append((2, f"Simulated average return {st['mean']:+.0%}: negative expected value"))
    if c["tech_dir"] >= 60: bull.append((1, "Price trend supports the direction"))
    elif c["tech_dir"] < 45: bear.append((1, "Price trend works against the trade"))
    if c["fund_dir"] >= 60: bull.append((1, "Business fundamentals support the direction"))
    elif c["fund_dir"] < 45: bear.append((1, "Fundamentals argue the other way"))
    if c["macro_dir"] >= 55: bull.append((1, "Macro backdrop is a tailwind"))
    elif c["macro_dir"] < 45: bear.append((1, "Macro backdrop is a headwind"))
    if ex["label"] == "UNDERPRICED": bull.append((1.5, "Options look cheap vs realised volatility"))
    elif ex["label"] == "OVERPRICED": bear.append((1.5, "Options look expensive vs realised volatility"))
    if st["p_total_loss_no_stop"] > 0.35:
        bear.append((1.5, f"{st['p_total_loss_no_stop']:.0%} chance of (near) total loss without the stop"))
    if theta_burn > 0.015: bear.append((1, f"Time decay {theta_burn:.1%} of premium per day"))
    if c["binary"]: bear.append((1, "Trade leans on a single earnings event"))
    if c["dq"] != "HIGH": bear.append((0.5 if c["dq"] == "MEDIUM" else 2, f"Data quality {c['dq']}"))
    bt = c["backtest"]
    if bt["n"] >= 30 and bt["win_rate"] > 0.55: bull.append((1, f"Similar past setups won {bt['win_rate']:.0%} ({bt['n']} cases)"))
    if bt["n"] >= 30 and bt["win_rate"] < 0.45: bear.append((1, f"Similar past setups won only {bt['win_rate']:.0%}"))
    bw, brw = sum(w for w, _ in bull), sum(w for w, _ in bear)
    v["F Bear"] = "AVOID" if brw >= bw else "WATCH"

    vetoes = []
    if st["p_total_loss_no_stop"] > 0.45: vetoes.append("Total-loss probability above 45% (no stop)")
    if st["p_loss50"] > 0.65: vetoes.append("Losing half or more is the most likely outcome")
    if c["dq"] == "LOW": vetoes.append("Data quality LOW")
    if tr["oi"] < c.get("min_oi", 100): vetoes.append("Not enough open interest")
    v["G Risk"] = "AVOID" if vetoes else "BUY"

    move_needed = abs(tr["breakeven"] / c["S0"] - 1)
    contrarian = {
        "Why the market may already be right": f"Options imply ±{ex.get('implied_move_ref', NAN):.0%} by "
            f"~45 days; IV/RV {ex.get('iv_rv', NAN):.2f} ({ex['label']}). Public news and analyst targets are already known.",
        "What I could be missing": "No insider-transaction, institutional-flow or true IV-history data in the free "
            "stack; news sentiment is keyword-based; geopolitical risk is not quantified.",
        "Why someone sells me this option": f"The seller collects ~${abs(tr['theta']) * 100:.2f}/day per contract in time "
            "decay plus the usual volatility risk premium; most bought options lose money.",
        "How it fails even if direction is right": f"Stock must pass ${tr['breakeven']:.2f} ({move_needed:+.1%} from now) "
            f"by expiry, or move fast enough before {tr['exit_date']}; IV crush after events can offset gains.",
    }
    return dict(votes=v, bull=bull, bear=bear, bull_w=bw, bear_w=brw, vetoes=vetoes,
                contrarian_pass=bw > brw, contrarian=contrarian)


# =========================================================== 19/20. SCORE + DECISION
def grade(score):
    return "A+" if score >= 90 else "A" if score >= 80 else "B" if score >= 70 else "C" if score >= 60 else "D"


def data_quality(flags):
    sev = [m for s, m in flags if s == "SEVERE"]
    return "LOW" if sev else "MEDIUM" if flags else "HIGH"


