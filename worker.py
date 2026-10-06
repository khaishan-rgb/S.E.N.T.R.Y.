"""Background worker (spec 23): scans and Monte Carlo never block browser requests."""
import threading
import time
import traceback

from . import db, services

_started = False
_busy = threading.Lock()
STATE = dict(running=False, last_quotes=0, last_analysis=0, last_journal=0, current="idle")


def tick():
    """One pass of scheduled work. Safe to call repeatedly."""
    s = db.get_settings()
    now = time.time()
    job = db.one("SELECT id FROM scan_jobs WHERE status='QUEUED' ORDER BY id LIMIT 1")
    if job:
        STATE["current"] = f"scan #{job['id']}"
        services.run_scan(job["id"])
    if now - STATE["last_quotes"] > s["quote_refresh_sec"]:
        STATE["current"] = "quotes"
        services.refresh_quotes()
        STATE["last_quotes"] = now
    if now - STATE["last_analysis"] > s["analysis_refresh_min"] * 60:
        STATE["current"] = "watchlist + positions"
        services.refresh_watchlist()
        services.evaluate_positions()
        STATE["last_analysis"] = time.time()
    if now - STATE["last_journal"] > 6 * 3600:
        STATE["current"] = "journal outcomes"
        services.journal_update_outcomes()
        STATE["last_journal"] = time.time()
    STATE["current"] = "idle"


def loop():
    STATE["running"] = True
    while True:
        try:
            with _busy:
                tick()
        except Exception:
            traceback.print_exc()
        time.sleep(3)


def start():
    global _started
    if _started:
        return
    _started = True
    db.execute("UPDATE scan_jobs SET status='QUEUED', message='Re-queued after restart' WHERE status='RUNNING'")
    threading.Thread(target=loop, daemon=True, name="sentry-worker").start()


def run_now(fn, *args):
    """Run an on-demand job (e.g. re-evaluate one position) in the background."""
    def go():
        with _busy:
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()
    threading.Thread(target=go, daemon=True).start()
