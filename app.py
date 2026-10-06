"""A.I. Options Command Centre - web app (Render-ready).

Run locally:   python app.py            (http://localhost:8000)
Production:    gunicorn app:app --workers 1 --threads 8 --timeout 120
"""
import datetime as dt
import os

from flask import Flask, jsonify, request, send_from_directory

from core import config, db, services, worker
from core.market import market_status, overview

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.json.sort_keys = False
services.setup()
if config.RUN_WORKER:
    worker.start()


def body():
    return request.get_json(silent=True) or {}


def err(msg, code=400):
    return jsonify(error=msg), code


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/health")
def health():
    try:
        db.one("SELECT 1 AS ok")
        dbok = True
    except Exception:
        dbok = False
    return jsonify(status="ok" if dbok else "degraded", db=dbok, mode=config.DATA_MODE,
                   worker=worker.STATE["current"], time=dt.datetime.utcnow().isoformat() + "Z")


# ---------------------------------------------------------------- dashboard / market
@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(services.dashboard())


@app.get("/api/status")
def api_status():
    return jsonify(market=market_status(), data=services.data_status(), worker=worker.STATE)


@app.get("/api/market")
def api_market():
    macro, _ = services.macro_ctx()
    return jsonify(market=market_status(), indices=overview(),
                   macro=dict(score=macro["score"], label=macro["label"], notes=macro["notes"], flags=macro["flags"],
                              r=macro["r"], r_source=macro["r_source"], as_of=macro["as_of"]),
                   source=services._MACRO["meta"])


# ---------------------------------------------------------------- scanning
@app.post("/api/scan")
def api_scan():
    b = body()
    mode = (b.get("mode") or "GROWTH").upper()
    if mode not in config.MODES:
        return err("mode must be FAST, GROWTH or LEAPS")
    uni = [t.strip().upper() for t in (b.get("universe") or []) if t.strip()] or None
    running = db.one("SELECT id FROM scan_jobs WHERE status IN ('QUEUED','RUNNING')")
    if running:
        return jsonify(job_id=running["id"], note="A scan is already queued or running")
    return jsonify(job_id=services.create_scan(mode, uni))


@app.get("/api/jobs/latest")
def api_job_latest():
    return jsonify(db.one("SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 1"))


@app.get("/api/jobs/<int:jid>")
def api_job(jid):
    return jsonify(db.one("SELECT * FROM scan_jobs WHERE id=?", (jid,)))


@app.get("/api/opportunities")
def api_opps():
    job, rows = services.latest_scan_rows(request.args.get("mode"))
    return jsonify(job=job, rows=rows, no_trade=bool(job) and not any(
        r.get("state") in ("BUY NOW", "BUY IF TRIGGERED") for r in rows))


@app.get("/api/report/<int:rid>")
def api_report(rid):
    r = db.one("SELECT payload FROM results WHERE id=?", (rid,))
    if not r or not r["payload"]:
        return err("report not found", 404)
    p = db.jload(r["payload"])
    p["result_id"] = rid
    return jsonify(p)


# ---------------------------------------------------------------- watchlist
@app.get("/api/watchlist")
def api_watch():
    return jsonify(services.watchlist_rows())


@app.post("/api/watchlist")
def api_watch_add():
    t = (body().get("ticker") or "").upper().strip()
    if not t.replace("-", "").replace(".", "").isalnum() or len(t) > 10:
        return err("invalid ticker")
    services.watch_add(t, body().get("mode"))
    w = db.one("SELECT * FROM watchlist WHERE ticker=?", (t,))
    worker.run_now(services.refresh_watch_item, w)
    return jsonify(ok=True)


@app.delete("/api/watchlist/<int:wid>")
def api_watch_del(wid):
    db.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    return jsonify(ok=True)


@app.post("/api/watchlist/<int:wid>/favourite")
def api_watch_fav(wid):
    w = db.one("SELECT favourite FROM watchlist WHERE id=?", (wid,))
    db.update("watchlist", wid, dict(favourite=0 if w["favourite"] else 1))
    return jsonify(ok=True)


@app.post("/api/watchlist/refresh")
def api_watch_refresh():
    worker.run_now(services.refresh_watchlist)
    return jsonify(ok=True, note="Re-running the decision engine in the background")


# ---------------------------------------------------------------- real portfolio
@app.get("/api/portfolio")
def api_portfolio():
    return jsonify(summary=services.portfolio_summary(), positions=services.position_rows("real"),
                   closed=db.all("SELECT * FROM positions WHERE account='real' AND status='CLOSED' ORDER BY id DESC"))


@app.post("/api/positions")
def api_pos_add():
    b = body()
    try:
        kind = b["kind"].lower()
        assert kind in ("call", "put")
        dt.date.fromisoformat(b["expiry"])
        pid = services.add_position("real", b["ticker"], kind, float(b["strike"]), b["expiry"], float(b["qty"]),
                                    float(b["premium"]), b.get("entry_date") or None)
    except Exception as ex:
        return err(f"Check the fields: {ex}")
    worker.run_now(services.evaluate_positions, "real")
    return jsonify(id=pid)


@app.post("/api/positions/<int:pid>/close")
def api_pos_close(pid):
    try:
        services.close_position(pid, float(body()["exit_premium"]), body().get("reason") or "Manual close")
    except Exception as ex:
        return err(str(ex))
    return jsonify(ok=True)


@app.delete("/api/positions/<int:pid>")
def api_pos_del(pid):
    p = db.one("SELECT account FROM positions WHERE id=?", (pid,))
    if not p or p["account"] != "real":
        return err("Only real positions can be deleted (demo history is never rewritten)")
    db.execute("DELETE FROM positions WHERE id=?", (pid,))
    return jsonify(ok=True)


@app.post("/api/positions/refresh")
def api_pos_refresh():
    worker.run_now(services.evaluate_positions)
    return jsonify(ok=True)


@app.get("/api/positions/<int:pid>/report")
def api_pos_report(pid):
    p = db.one("SELECT * FROM positions WHERE id=?", (pid,))
    if not p:
        return err("not found", 404)
    ev = db.jload(p["last_eval"], {})
    rep = ev.pop("report", None)
    if not rep:
        return err("Not evaluated yet - try again in a minute", 404)
    rep["position"] = dict(ev, entry_premium=p["entry_premium"], qty=p["qty"], account=p["account"], id=pid)
    return jsonify(rep)


# ---------------------------------------------------------------- demo account
@app.get("/api/demo")
def api_demo():
    return jsonify(metrics=services.demo_metrics(), positions=services.position_rows("demo"),
                   trades=db.all("SELECT * FROM demo_trades ORDER BY id DESC LIMIT 200"),
                   auto_log=db.all("SELECT * FROM auto_log ORDER BY id DESC LIMIT 100"))


@app.post("/api/demo/buy")
def api_demo_buy():
    b = body()
    rec = None
    if b.get("result_id"):
        r = db.one("SELECT payload FROM results WHERE id=?", (int(b["result_id"]),))
        rec = db.jload(r["payload"]) if r else None
        if not rec:
            return err("recommendation not found")
        c = rec["contract"]
        if len(c["legs"]) != 1:
            return err("Paper trading supports single-leg contracts")
        t, kind, strike, exp = rec["ticker"], c["kind"], c["legs"][0]["K"], c["expiry"]
    else:
        t, kind, strike, exp = b.get("ticker", ""), b.get("kind", "call"), float(b.get("strike", 0)), b.get("expiry", "")
    pid, e = services.paper_buy(t, kind, strike, exp, int(b.get("qty", 1)), b.get("reason") or "Manual paper buy", rec=rec)
    if e:
        return err(e)
    worker.run_now(services.evaluate_positions, "demo")
    return jsonify(position_id=pid)


@app.post("/api/demo/sell")
def api_demo_sell():
    b = body()
    pnl, e = services.paper_sell(int(b["position_id"]), b.get("qty"), b.get("reason") or "Manual paper sell")
    return err(e) if e else jsonify(pnl=pnl)


# ---------------------------------------------------------------- alerts / actions / journal
@app.get("/api/alerts")
def api_alerts():
    return jsonify(alerts=db.all("SELECT * FROM alerts ORDER BY id DESC LIMIT 300"), types=config.ALERT_TYPES,
                   enabled=db.get_settings()["alerts"])


@app.post("/api/alerts/read")
def api_alerts_read():
    db.execute("UPDATE alerts SET is_read=1")
    return jsonify(ok=True)


@app.get("/api/actions")
def api_actions():
    return jsonify(services.action_queue())


@app.get("/api/journal")
def api_journal():
    rows = db.all("SELECT id, ts, source, ticker, direction, mode, strategy, contract, state, score, pop, ev, entry_ask, "
                  "trigger_upper, time_exit, regime, outcome_status, outcome_return, outcome_note FROM journal "
                  "ORDER BY id DESC LIMIT 300")
    for r in rows:
        r["contract"] = (db.jload(r["contract"], {}) or {}).get("name")
    return jsonify(stats=services.journal_stats(), entries=rows)


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def api_settings():
    from core.providers import providers
    return jsonify(settings=db.get_settings(), modes=config.MODES, alert_types=config.ALERT_TYPES,
                   providers=providers().describe(), database="PostgreSQL" if db.PG else "SQLite")


@app.post("/api/settings")
def api_settings_save():
    return jsonify(settings=db.save_settings(body()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
