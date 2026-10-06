"""Application services. Called by the API (fast reads/writes) and by the background worker (slow jobs)."""
import datetime as dt
import math
import time
import traceback

import numpy as np

from . import config, db, engines
from .analysis import scan_ticker, evaluate_position, analyse, contract_name
from .decisions import ACTION_URGENCY
from .market import market_status, overview
from .providers import providers, load_ticker, iso, ChainStore

STATE_RANK = {"BUY NOW": 0, "BUY IF TRIGGERED": 1, "WAIT": 2, "AVOID": 3}


def setup():
    db.init()
    ChainStore.save = staticmethod(db.save_chain)
    ChainStore.load = staticmethod(db.load_chain)


# ------------------------------------------------------------------ shared market context (cached)
_MACRO = {"ts": 0, "macro": None, "bench": None, "meta": None}


def macro_ctx():
    if time.time() - _MACRO["ts"] > 900 or _MACRO["macro"] is None:
        P = providers()
        series, m = P.macro.series()
        _MACRO["macro"] = engines.macro_engine(series or {})
        _MACRO["meta"] = m
        bench, _ = P.stock.history(config.BENCHMARK)
        _MACRO["bench"] = bench
        _MACRO["ts"] = time.time()
    return _MACRO["macro"], _MACRO["bench"]


# ------------------------------------------------------------------ results + journal
def row_of(p):
    """Compact row for tables (spec 2)."""
    if "error" in p:
        return dict(ticker=p["ticker"], state="AVOID", error=p["error"], direction=p.get("direction"))
    c, st = p["contract"], p["stats"]
    return dict(ticker=p["ticker"], name=p["name"], direction=p["direction"], mode=p["mode"], price=p["price"],
                change_pct=p["change_pct"], score=p["score"], state=p["state"], headline=p["headline"],
                contract=c["name"], expiry=c["expiry"], strike=c["legs"][0]["K"], kind=c["kind"],
                strategy=c["strategy"], premium=c["ask"], trigger_lower=p["trigger"]["lower"],
                trigger_upper=p["trigger"]["upper"], pop=st["p_profit"], p2x=st["p_100"], ev=st["mean"],
                risk=p["risk_level"], catalyst=p["key_catalyst"], dq=p["dq"], chain_status=p["chain_status"],
                iv_rv=p.get("iv_rv"), earnings_date=p.get("earnings_date"), ts=p["ts"], fair_label=p["fair"]["label"])


def save_result(p, source, job_id=None):
    rid = db.insert("results", dict(ts=p.get("ts", iso()), job_id=job_id, source=source, ticker=p["ticker"],
                                    direction=p.get("direction"), mode=p.get("mode"), state=p["state"],
                                    score=p.get("score"), row=db.jdump(row_of(p)),
                                    payload=db.jdump(p) if "error" not in p else None))
    return rid


def journal_record(p, source):
    if "error" in p:
        return
    c, st = p["contract"], p["stats"]
    db.insert("journal", dict(
        ts=p["ts"], source=source, ticker=p["ticker"], direction=p["direction"], mode=p["mode"],
        strategy=c["strategy"], contract=db.jdump(c), kind=c["kind"], strike=c["legs"][0]["K"], expiry=c["expiry"],
        state=p["state"], score=p["score"], pop=st["p_profit"], ev=st["mean"], entry_ask=c["ask"],
        trigger_upper=p["trigger"]["upper"], time_exit=p["plan"]["time_exit"], regime=p["regime"],
        market=db.jdump(dict(price=p["price"], iv=c["iv"], atm_iv=p.get("atm_iv"), chain=p["chain_status"])),
        sim=db.jdump(dict(q05=st["q05"], q25=st["q25"], median=st["median"], q75=st["q75"], q95=st["q95"],
                          p50=st["p_50"], p100=st["p_100"], lose_most=st["p_lose_most"])),
        outcome_status="OPEN"))


# ------------------------------------------------------------------ alerts
def alert(kind, ticker, title, message, urgency=50, ref=""):
    s = db.get_settings()
    if not s["alerts"].get(kind, True):
        return
    since = (dt.datetime.utcnow() - dt.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if db.one("SELECT id FROM alerts WHERE type=? AND ticker=? AND ref=? AND ts>?", (kind, ticker, ref, since)):
        return
    db.insert("alerts", dict(ts=iso(), type=kind, ticker=ticker, title=title, message=message, urgency=urgency,
                             ref=ref, is_read=0))


# ------------------------------------------------------------------ scans (background)
def create_scan(mode, universe=None):
    s = db.get_settings()
    uni = universe or [u.strip().upper() for u in s["universe"].split(",") if u.strip()]
    return db.insert("scan_jobs", dict(created=iso(), mode=mode, universe=",".join(uni), status="QUEUED",
                                       progress=0, total=len(uni), message="Queued"))


def run_scan(job_id):
    job = db.one("SELECT * FROM scan_jobs WHERE id=?", (job_id,))
    s = db.get_settings()
    db.update("scan_jobs", job_id, dict(status="RUNNING", started=iso(), message="Loading market context"))
    macro, bench = macro_ctx()
    uni = [u for u in job["universe"].split(",") if u]
    good = 0
    for i, sym in enumerate(uni, 1):
        db.update("scan_jobs", job_id, dict(progress=i - 1, message=f"Analysing {sym}"))
        try:
            for p in scan_ticker(sym, macro, bench, s, job["mode"]):
                p.setdefault("mode", job["mode"])
                rid = save_result(p, "scan", job_id)
                journal_record(p, "scan")
                if p["state"] in ("BUY NOW", "BUY IF TRIGGERED"):
                    good += 1
                if p.get("score") and p["score"] >= 90:
                    alert("new_90", p["ticker"], f"{p['ticker']} new {p['score']:.0f} opportunity", p["headline"], 65, f"r{rid}")
                if p["state"] == "BUY NOW":
                    auto_buy(p, rid)
        except Exception as ex:
            save_result(dict(ticker=sym, state="AVOID", error=f"{ex.__class__.__name__}: {ex}"), "scan", job_id)
            traceback.print_exc()
    msg = (f"{good} qualifying trade(s) found" if good else "NO HIGH-QUALITY TRADE FOUND - cash is a position")
    db.update("scan_jobs", job_id, dict(status="DONE", finished=iso(), progress=len(uni), message=msg))


def latest_scan_rows(mode=None):
    q = "SELECT * FROM scan_jobs WHERE status='DONE'" + (" AND mode=?" if mode else "") + " ORDER BY id DESC LIMIT 1"
    job = db.one(q, (mode,) if mode else ())
    if not job:
        return None, []
    rows = []
    for r in db.all("SELECT id, row FROM results WHERE job_id=?", (job["id"],)):
        x = db.jload(r["row"], {})
        x["result_id"] = r["id"]
        rows.append(x)
    rows.sort(key=lambda x: (STATE_RANK.get(x.get("state"), 9), -(x.get("score") or 0)))
    return job, rows


# ------------------------------------------------------------------ watchlist (spec 2) with hysteresis
def watch_add(sym, mode=None):
    sym = sym.upper().strip()
    if not sym or db.one("SELECT id FROM watchlist WHERE ticker=?", (sym,)):
        return
    db.insert("watchlist", dict(ticker=sym, favourite=0, added_ts=iso(), mode=mode or db.get_settings()["watch_mode"],
                                state=None, pending_count=0))


def refresh_quotes():
    P = providers()
    for w in db.all("SELECT id, ticker FROM watchlist"):
        q, m = P.stock.quote(w["ticker"])
        if q:
            last, prev = q
            db.update("watchlist", w["id"], dict(price=last, change_pct=(last / prev - 1) if prev else None,
                                                 quote_ts=m["timestamp"], quote_fresh=m["freshness"]))


def refresh_watch_item(w, settings=None, macro=None, bench=None):
    s = settings or db.get_settings()
    if macro is None:
        macro, bench = macro_ctx()
    res = [p for p in scan_ticker(w["ticker"], macro, bench, s, w["mode"] or s["watch_mode"]) if "error" not in p]
    if not res:
        db.update("watchlist", w["id"], dict(eval_ts=iso(), state=w["state"] or "AVOID"))
        return
    best = sorted(res, key=lambda p: (STATE_RANK[p["state"]], -p["score"]))[0]
    rid = save_result(best, "watchlist")
    new, old = best["state"], w["state"]
    upd = dict(result_id=rid, score=best["score"], eval_ts=iso(), price=best["price"], change_pct=best["change_pct"])
    # Hysteresis: a state change needs two consecutive engine runs agreeing, unless the score moved decisively.
    if old is None:
        upd.update(state=new, pending_state=None, pending_count=0)
        journal_record(best, "watchlist")
    elif new != old:
        decisive = w["score"] is None or abs(best["score"] - w["score"]) >= 8
        cnt = (w["pending_count"] or 0) + 1 if w["pending_state"] == new else 1
        if decisive or cnt >= 2:
            upd.update(state=new, pending_state=None, pending_count=0)
            journal_record(best, "watchlist")
            if new == "BUY NOW" and old == "BUY IF TRIGGERED":
                alert("buy_trigger", w["ticker"], f"{w['ticker']} BUY TRIGGERED", "Premium entered the target range.", 60, best["contract"]["name"])
            elif new == "BUY NOW":
                alert("wait_to_buy", w["ticker"], f"{w['ticker']} {old} → BUY NOW", best["headline"], 60, best["contract"]["name"])
            if new == "BUY NOW":
                auto_buy(best, rid)
        else:
            upd.update(pending_state=new, pending_count=cnt)
    else:
        upd.update(pending_state=None, pending_count=0)
    if best.get("earnings_date"):
        days = (dt.date.fromisoformat(best["earnings_date"]) - dt.date.today()).days
        if 0 <= days <= 7:
            alert("earnings", w["ticker"], f"{w['ticker']} earnings in {days} day(s)", "Expect an IV move around the report.", 40, best["earnings_date"])
    db.update("watchlist", w["id"], upd)


def refresh_watchlist():
    s = db.get_settings()
    macro, bench = macro_ctx()
    for w in db.all("SELECT * FROM watchlist"):
        try:
            refresh_watch_item(w, s, macro, bench)
        except Exception:
            traceback.print_exc()


def watchlist_rows():
    out = []
    for w in db.all("SELECT * FROM watchlist ORDER BY favourite DESC, ticker"):
        r = db.one("SELECT row FROM results WHERE id=?", (w["result_id"],)) if w["result_id"] else None
        row = db.jload(r["row"], {}) if r else {}
        row.update(ticker=w["ticker"], id=w["id"], favourite=bool(w["favourite"]), state=w["state"] or "PENDING",
                   pending_state=w["pending_state"], price=w["price"] if w["price"] is not None else row.get("price"),
                   change_pct=w["change_pct"], eval_ts=w["eval_ts"], quote_fresh=w["quote_fresh"],
                   result_id=w["result_id"], mode=w["mode"])
        out.append(row)
    return out


# ------------------------------------------------------------------ positions (real + demo)
def add_position(account, ticker, kind, strike, expiry, qty, premium, entry_date=None, **extra):
    row = dict(account=account, ticker=ticker.upper().strip(), kind=kind.lower(), strike=float(strike), expiry=expiry,
               qty=float(qty), entry_premium=float(premium), entry_date=entry_date or str(dt.date.today()),
               status="OPEN", realised=0.0, auto=0)
    row.update({k: v for k, v in extra.items() if v is not None})
    return db.insert("positions", row)


def evaluate_positions(account=None):
    s = db.get_settings()
    macro, bench = macro_ctx()
    q = "SELECT * FROM positions WHERE status='OPEN'" + (" AND account=?" if account else "")
    for pos in db.all(q, (account,) if account else ()):
        try:
            ev = evaluate_position(pos, macro, bench, s)
            prev = db.jload(pos["last_eval"], {})
            _position_alerts(pos, ev, prev)
            db.update("positions", pos["id"], dict(last_eval=db.jdump(ev), last_eval_ts=iso(), last_action=ev["action"]))
            if pos["account"] == "demo" and pos["auto"] and ev["action"] != (pos["last_action"] or ""):
                auto_manage(pos, ev)
        except Exception:
            traceback.print_exc()
    snapshot_demo_equity()


def _position_alerts(pos, ev, prev):
    t, a, pa = pos["ticker"], ev["action"], prev.get("action")
    ref = f"p{pos['id']}:{a}"
    if a != pa and a in ("TAKE 25% PROFIT", "TAKE 50% PROFIT"):
        alert("hold_to_take_profit", t, f"{t} {a}", ev["reason"], ACTION_URGENCY[a], ref)
    if a != pa and a in ("SELL", "EXIT NOW"):
        alert("hold_to_sell", t, f"{t} {a}", ev["reason"], ACTION_URGENCY[a], ref)
    h = ev.get("hold") or {}
    if h.get("thesis_broken"):
        alert("risk_threshold", t, f"{t} thesis level broken", f"Stock crossed ${h['thesis_level']:,.2f}.", 85, f"p{pos['id']}:thesis")
    if ev.get("dte") is not None and ev["dte"] <= 21:
        alert("dte_warning", t, f"{t} {ev['dte']} days to expiry", "Time decay accelerates now.", 55, f"p{pos['id']}:dte")
    if pos.get("target_price") and ev.get("mark") and ev["mark"] >= pos["target_price"]:
        alert("profit_target", t, f"{t} reached profit target ${pos['target_price']:.2f}", "Re-check: hold or take profit?", 75, f"p{pos['id']}:tgt")
    if prev.get("iv") and ev.get("iv") and ev["iv"] > prev["iv"] * 1.25:
        alert("iv_spike", t, f"{t} IV spike", f"IV {prev['iv']:.0%} → {ev['iv']:.0%}", 45, f"p{pos['id']}:iv:{ev['ts'][:10]}")


def position_rows(account):
    out = []
    for p in db.all("SELECT * FROM positions WHERE account=? AND status='OPEN' ORDER BY id", (account,)):
        ev = db.jload(p["last_eval"], {})
        ev.pop("report", None)
        out.append(dict(id=p["id"], ticker=p["ticker"], kind=p["kind"], strike=p["strike"], expiry=p["expiry"],
                        qty=p["qty"], entry_premium=p["entry_premium"], entry_date=p["entry_date"], auto=bool(p["auto"]),
                        contract=f"{dt.date.fromisoformat(p['expiry']).strftime('%d %b %Y').upper()} ${p['strike']:g} {p['kind'].upper()}",
                        eval=ev, eval_ts=p["last_eval_ts"]))
    return out


def portfolio_summary():
    s = db.get_settings()
    rows = position_rows("real")
    val = sum((r["eval"].get("value") or 0) for r in rows)
    basis = sum(r["qty"] * r["entry_premium"] * 100 for r in rows)
    unreal = sum((r["eval"].get("pnl") or 0) for r in rows)
    day = sum((r["eval"].get("day_change") or 0) for r in rows)
    realised = sum((p["realised"] or 0) for p in db.all("SELECT realised FROM positions WHERE account='real'"))
    cash = float(s["real_cash"])
    missing = sum(1 for r in rows if r["eval"].get("value") is None)
    return dict(value=cash + val, options_value=val, cash=cash, day_pl=day, unrealised=unreal, realised=realised,
                total_pl=unreal + realised, total_pl_pct=(unreal + realised) / basis if basis else None,
                at_risk=val, positions=len(rows), unpriced=missing)


def close_position(pid, exit_premium, reason="Manual close"):
    p = db.one("SELECT * FROM positions WHERE id=?", (pid,))
    pnl = (float(exit_premium) - p["entry_premium"]) * p["qty"] * 100
    db.update("positions", pid, dict(status="CLOSED", exit_premium=float(exit_premium), exit_ts=iso(), exit_reason=reason,
                                     realised=(p["realised"] or 0) + pnl))


# ------------------------------------------------------------------ demo / paper account (spec 7, 8)
def _quote_contract(ticker, kind, strike, expiry):
    td = load_ticker(ticker, expiries=[expiry], full=False)
    if expiry not in td.chains:
        return None, "No option chain for that expiry"
    if td.chain_status not in ("LIVE", "15-MIN DELAY", "SYNTHETIC"):
        return None, f"Needs a current option quote (chain is {td.chain_status}). Try during market hours."
    df = td.chains[expiry]["calls" if kind == "call" else "puts"]
    r = df[(df["strike"] - float(strike)).abs() < 1e-6]
    if r.empty:
        return None, "Strike not found"
    bid, ask = float(r["bid"].iloc[0] or 0), float(r["ask"].iloc[0] or 0)
    if bid <= 0 or ask <= 0:
        return None, "No live bid/ask for this contract"
    return dict(bid=bid, ask=ask, status=td.chain_status), None


def demo_cash():
    s = db.get_settings()
    t = db.all("SELECT side, qty, premium FROM demo_trades")
    flow = sum((-1 if x["side"] == "BUY" else 1) * x["qty"] * x["premium"] * 100 for x in t)
    return float(s["demo_starting_cash"]) + flow


def paper_buy(ticker, kind, strike, expiry, qty, reason="Manual paper buy", auto=False, rec=None):
    q, err = _quote_contract(ticker, kind, strike, expiry)
    if err:
        return None, err
    qty = int(qty)
    cost = q["ask"] * qty * 100
    if qty < 1:
        return None, "Quantity must be at least 1"
    if cost > demo_cash() + 1e-6:
        return None, f"Not enough demo cash (${demo_cash():,.2f}) for ${cost:,.2f}"
    rec = rec or {}
    plan = rec.get("plan", {})
    pid = add_position("demo", ticker, kind, strike, expiry, qty, q["ask"], auto=1 if auto else 0,
                       entry_score=rec.get("score"), entry_pop=(rec.get("stats") or {}).get("p_profit"),
                       entry_ev=(rec.get("stats") or {}).get("mean"), signal_ts=rec.get("ts"),
                       thesis_level=plan.get("stop_stock"), target_price=plan.get("main_price"))
    db.insert("demo_trades", dict(ts=iso(), position_id=pid, side="BUY", ticker=ticker.upper(),
                                  contract=f"{expiry} {strike:g} {kind.upper()}", qty=qty, premium=q["ask"], reason=reason,
                                  score=rec.get("score"), pop=(rec.get("stats") or {}).get("p_profit"),
                                  ev=(rec.get("stats") or {}).get("mean"), pnl=None, holding_days=None, auto=1 if auto else 0))
    snapshot_demo_equity()
    return pid, None


def paper_sell(pid, qty=None, reason="Manual paper sell", auto=False):
    p = db.one("SELECT * FROM positions WHERE id=? AND account='demo'", (pid,))
    if not p or p["status"] != "OPEN":
        return None, "Position not open"
    q, err = _quote_contract(p["ticker"], p["kind"], p["strike"], p["expiry"])
    if err:
        return None, err
    qty = int(qty or p["qty"])
    qty = max(1, min(qty, int(p["qty"])))
    pnl = (q["bid"] - p["entry_premium"]) * qty * 100
    days = (dt.date.today() - dt.date.fromisoformat(p["entry_date"])).days
    db.insert("demo_trades", dict(ts=iso(), position_id=pid, side="SELL", ticker=p["ticker"],
                                  contract=f"{p['expiry']} {p['strike']:g} {p['kind'].upper()}", qty=qty, premium=q["bid"],
                                  reason=reason, score=None, pop=None, ev=None, pnl=pnl, holding_days=days, auto=1 if auto else 0))
    left = p["qty"] - qty
    upd = dict(qty=left, realised=(p["realised"] or 0) + pnl)
    if left <= 0:
        upd.update(status="CLOSED", exit_premium=q["bid"], exit_ts=iso(), exit_reason=reason)
    db.update("positions", pid, upd)
    snapshot_demo_equity()
    return pnl, None


def demo_equity():
    rows = position_rows("demo")
    val = 0.0
    for r in rows:
        m = r["eval"].get("mark")
        val += (m if m is not None else r["entry_premium"]) * r["qty"] * 100
    return demo_cash() + val, val, rows


def snapshot_demo_equity():
    eq, _, _ = demo_equity()
    db.insert("demo_equity", dict(ts=iso(), equity=eq, cash=demo_cash()))


def demo_metrics():
    s = db.get_settings()
    start = float(s["demo_starting_cash"])
    eq, val, rows = demo_equity()
    unreal = sum((r["eval"].get("mark", r["entry_premium"]) - r["entry_premium"]) * r["qty"] * 100
                 for r in rows if r["eval"].get("mark") is not None)
    closed = db.all("SELECT realised FROM positions WHERE account='demo' AND status='CLOSED'")
    realised = sum((p["realised"] or 0) for p in db.all("SELECT realised FROM positions WHERE account='demo'"))
    wins = [p["realised"] for p in closed if (p["realised"] or 0) > 0]
    losses = [p["realised"] for p in closed if (p["realised"] or 0) <= 0]
    curve = [r["equity"] for r in db.all("SELECT equity FROM demo_equity ORDER BY id")] or [start]
    peak, mdd = start, 0.0
    for e in [start] + curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    today = dt.date.today().isoformat()
    before = db.one("SELECT equity FROM demo_equity WHERE ts<? ORDER BY id DESC LIMIT 1", (today,))
    day_ref = before["equity"] if before else start
    n_trades = db.one("SELECT COUNT(*) AS n FROM demo_trades WHERE side='BUY'")["n"]
    return dict(equity=eq, cash=demo_cash(), options_value=val, realised=realised, unrealised=unreal,
                total_return=eq / start - 1, day_pl=eq - day_ref, day_pl_pct=eq / day_ref - 1 if day_ref else None,
                win_rate=len(wins) / len(closed) if closed else None, avg_winner=float(np.mean(wins)) if wins else None,
                avg_loser=float(np.mean(losses)) if losses else None,
                profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
                max_drawdown=mdd, trades=n_trades, closed=len(closed), start=start, auto=s["auto_demo"],
                curve=curve[-120:])


def auto_buy(p, rid):
    s = db.get_settings()
    if not s["auto_demo"]:
        return
    log = lambda dec, why: db.insert("auto_log", dict(ts=iso(), ticker=p["ticker"], decision=dec, reason=why,
                                                       details=db.jdump(dict(result_id=rid, score=p["score"], contract=p["contract"]["name"]))))
    c = p["contract"]
    if len(c["legs"]) != 1:
        return log("SKIP", "Auto mode only paper-trades single-leg contracts")
    if p["score"] < s["entry_min_score"]:
        return log("SKIP", "Score below auto threshold")
    open_demo = db.all("SELECT ticker FROM positions WHERE account='demo' AND status='OPEN'")
    if len(open_demo) >= s["auto_max_positions"]:
        return log("SKIP", f"Max {s['auto_max_positions']} open demo positions")
    if any(o["ticker"] == p["ticker"] for o in open_demo):
        return log("SKIP", "Already holding this ticker")
    eq, _, _ = demo_equity()
    budget = eq * s["auto_risk_pct"] / 100
    qty = int(budget // (c["ask"] * 100))
    if qty < 1:
        return log("SKIP", f"Risk budget ${budget:,.0f} below one contract (${c['ask'] * 100:,.0f})")
    pid, err = paper_buy(p["ticker"], c["kind"], c["legs"][0]["K"], c["expiry"], qty,
                         reason=f"AUTO: {p['headline']}", auto=True, rec=p)
    log("BUY" if pid else "SKIP", err or f"Paper-bought {qty} x {c['name']} at ask")


def auto_manage(pos, ev):
    a = ev["action"]
    qty = int(pos["qty"])
    n = qty if a in ("SELL", "EXIT NOW") else max(1, qty // 2) if a == "TAKE 50% PROFIT" else \
        max(1, math.ceil(qty * 0.25)) if a == "TAKE 25% PROFIT" else 0
    if not n:
        return
    pnl, err = paper_sell(pos["id"], n, reason=f"AUTO {a}: {ev['reason']}", auto=True)
    db.insert("auto_log", dict(ts=iso(), ticker=pos["ticker"], decision="SELL" if pnl is not None else "SKIP",
                               reason=err or f"{a}: sold {n}", details=db.jdump(dict(position_id=pos["id"]))))


# ------------------------------------------------------------------ journal outcomes + calibration (spec 20)
def journal_update_outcomes():
    today = dt.date.today()
    P = providers()
    for j in db.all("SELECT * FROM journal WHERE outcome_status='OPEN'"):
        try:
            te, ex = dt.date.fromisoformat(j["time_exit"]), dt.date.fromisoformat(j["expiry"])
            if today < te or not j["entry_ask"]:
                continue
            c = db.jload(j["contract"], {})
            legs = c.get("legs", [])
            if today > ex:
                h, _ = P.stock.history(j["ticker"])
                if h is None:
                    continue
                Sx = float(h[h.index <= str(ex)]["Close"].iloc[-1])
                val = sum(l["sign"] * (max(Sx - l["K"], 0) if l["kind"] == "call" else max(l["K"] - Sx, 0)) for l in legs)
                note = f"expired; stock ${Sx:,.2f}"
            else:
                td = load_ticker(j["ticker"], expiries=[j["expiry"]], full=False)
                if td.chain_status not in ("LIVE", "15-MIN DELAY", "SYNTHETIC") or j["expiry"] not in td.chains:
                    continue
                val = 0.0
                for l in legs:
                    df = td.chains[j["expiry"]]["calls" if l["kind"] == "call" else "puts"]
                    r = df[(df["strike"] - l["K"]).abs() < 1e-6]
                    if r.empty:
                        raise ValueError("strike gone")
                    val += float(r["bid"].iloc[0]) if l["sign"] > 0 else -float(r["ask"].iloc[0])
                note = "marked at bid on time-exit date"
            ret = max(val, 0) / j["entry_ask"] - 1
            db.update("journal", j["id"], dict(outcome_status="CLOSED", outcome_return=ret, outcome_ts=iso(), outcome_note=note))
        except Exception:
            traceback.print_exc()


def journal_stats():
    closed = db.all("SELECT * FROM journal WHERE outcome_status='CLOSED'")
    total = db.one("SELECT COUNT(*) AS n FROM journal")["n"]
    def summary(rows):
        r = [x["outcome_return"] for x in rows if x["outcome_return"] is not None]
        if not r:
            return dict(n=0)
        g, l = sum(x for x in r if x > 0), -sum(x for x in r if x <= 0)
        eq, peak, mdd = 1.0, 1.0, 0.0
        for x in r:
            eq *= 1 + 0.05 * x          # equal 5% risk per trade, sequential
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        return dict(n=len(r), win_rate=float(np.mean([x > 0 for x in r])), avg=float(np.mean(r)), median=float(np.median(r)),
                    profit_factor=(g / l) if l > 0 else None, max_drawdown=mdd)
    buys = [x for x in closed if x["state"] == "BUY NOW"]
    def group(key):
        out = {}
        for x in closed:
            out.setdefault(key(x), []).append(x)
        return {k: summary(v) for k, v in sorted(out.items())}
    bucket = lambda s: "90-100" if s >= 90 else "80-89" if s >= 80 else "70-79" if s >= 70 else "60-69" if s >= 60 else "<60"
    calib = []
    for lo in np.arange(0, 1, 0.1):
        rows = [x for x in closed if x["pop"] is not None and lo <= x["pop"] < lo + 0.1]
        if rows:
            calib.append(dict(bucket=f"{lo:.0%}-{lo + 0.1:.0%}", predicted=float(np.mean([x["pop"] for x in rows])),
                              actual=float(np.mean([(x["outcome_return"] or 0) > 0 for x in rows])), n=len(rows)))
    return dict(total=total, closed=len(closed), open=total - len(closed), ai_buys=summary(buys), all=summary(closed),
                by_score=group(lambda x: bucket(x["score"] or 0)), by_strategy=group(lambda x: x["strategy"] or "?"),
                by_holding=group(lambda x: x["mode"] or "?"), by_regime=group(lambda x: x["regime"] or "?"),
                by_state=group(lambda x: x["state"]), calibration=calib)


# ------------------------------------------------------------------ action queue + dashboard (spec 1, 9)
def action_queue():
    items = []
    for r in position_rows("real"):
        ev = r["eval"]
        if not ev:
            continue
        items.append(dict(kind="position", id=r["id"], ticker=r["ticker"], contract=r["contract"], action=ev["action"],
                          reason=ev.get("reason"), mark=ev.get("mark"), pnl_pct=ev.get("pnl_pct"), ts=r["eval_ts"],
                          urgency=ACTION_URGENCY.get(ev["action"], 0)))
    for w in db.all("SELECT * FROM watchlist WHERE state='BUY NOW'"):
        r = db.one("SELECT row FROM results WHERE id=?", (w["result_id"],)) if w["result_id"] else None
        row = db.jload(r["row"], {}) if r else {}
        items.append(dict(kind="opportunity", id=w["result_id"], ticker=w["ticker"], contract=row.get("contract"),
                          action="BUY TRIGGERED", reason=row.get("headline"), mark=row.get("premium"), pnl_pct=None,
                          ts=w["eval_ts"], urgency=ACTION_URGENCY["BUY TRIGGERED"]))
    items.sort(key=lambda x: -x["urgency"])
    return items


def data_status():
    P = providers()
    last = db.one("SELECT MAX(eval_ts) AS t FROM watchlist")["t"]
    last_pos = db.one("SELECT MAX(last_eval_ts) AS t FROM positions")["t"]
    last_scan = db.one("SELECT MAX(finished) AS t FROM scan_jobs")["t"]
    ts = max([x for x in (last, last_pos, last_scan) if x] or [None]) if any((last, last_pos, last_scan)) else None
    label = "SYNTHETIC" if P.mode == "demo" else "15-MIN DELAY"
    return dict(mode=P.mode, label=label, last_update=ts, providers=P.describe())


def dashboard():
    job, rows = latest_scan_rows()
    return dict(market=market_status(), data=data_status(), indices=overview(), portfolio=portfolio_summary(),
                demo=demo_metrics(), actions=action_queue()[:8],
                opportunities=[r for r in rows if "error" not in r][:12],
                scan=dict(job) if job else None, no_trade=bool(job) and not any(
                    r.get("state") in ("BUY NOW", "BUY IF TRIGGERED") for r in rows),
                alerts_unread=db.one("SELECT COUNT(*) AS n FROM alerts WHERE is_read=0")["n"])
