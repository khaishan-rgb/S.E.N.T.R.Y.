# S.E.N.T.R.Y — A.I. Options Command Centre (V2)

A web app that answers two questions in plain language:

1. **What option should I consider buying now?** → 🟢 BUY NOW · 🟢 BUY IF TRIGGERED · 🟡 WAIT · ⚫ AVOID
2. **For options I own: hold, take profit or sell?** → 🟢 ADD/BUY · 🟢 HOLD · 🟡 HOLD/WATCH · 🟠 TAKE 25%/50% · 🔴 SELL · 🔴 EXIT NOW

It never places real trades. The demo account uses virtual money only. "NO TRADE TODAY" is a valid answer.

## Run it on your computer (5 minutes)
```bash
pip install -r requirements.txt
DATA_MODE=demo python app.py      # synthetic test data, works offline -> http://localhost:8000
python app.py                     # real (free, ~15-min delayed) market data
```
On Windows PowerShell use `$env:DATA_MODE="demo"; python app.py`.

## Put it on GitHub
1. Create an empty repository on github.com (no README), e.g. `sentry-options`.
2. In this folder:
```bash
git init
git add .
git commit -m "Options Command Centre V2"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/sentry-options.git
git push -u origin main
```
`.gitignore` already keeps your database and any `.env` file out of the repository. Never commit API keys.

## Deploy on Render
1. Render dashboard → **New → Blueprint** → choose the GitHub repo. `render.yaml` creates the web service and a PostgreSQL database.
2. Set `SEC_USER_AGENT` (your name + email) and, optionally, `FINNHUB_API_KEY`.
3. Health check: `/health`.

Notes: use one gunicorn worker (as configured) so there is exactly one background job runner.
Free Render web services sleep when idle, which pauses background refreshes; free databases have limits — check Render's current terms.
Use PostgreSQL in production: Render's disk is wiped on every deploy, so SQLite data would be lost.

## Environment variables
| Variable | Purpose |
|---|---|
| `DATA_MODE` | `live` (default) or `demo` (synthetic data, clearly labelled) |
| `DATABASE_URL` | `sqlite:///data/sentry.db` locally, PostgreSQL URL in production |
| `SEC_USER_AGENT` | Required politeness header for SEC EDGAR ("Name email") |
| `FINNHUB_API_KEY` | Optional: second price source (cross-check) + news |
| `RUN_WORKER` | `1` (default) runs the background worker in the web process |

Keys stay on the server. The browser only talks to this app's `/api`.

## How it maps to the V2 spec
| Spec | Where |
|---|---|
| Navigation, dark command-centre UI, mobile order | `static/` |
| Market status, data freshness labels (never "LIVE" for delayed data) | `core/market.py`, `core/providers.py` |
| Interchangeable provider adapters, caching, rate limits | `core/providers.py` |
| Entry score / Hold score / Exit pressure (3 engines) | `core/decisions.py` |
| Buy trigger price range, trade plan levels (derived, not fixed %) | `decisions.apply_plan`, `decisions.buy_trigger` |
| Would AI buy it today? (no sunk-cost bias) | `analysis.evaluate_position` |
| Monte Carlo 10k / 50k, 11 regimes, fat tails, jumps | `core/simulate.py` |
| Fair value CHEAP → VERY EXPENSIVE | `decisions.fair_class` |
| Committee, contrarian, risk vetoes | `core/engines.py` |
| Scanner modes FAST / GROWTH / LEAPS, background jobs | `services.run_scan`, `core/worker.py` |
| Watchlist with hysteresis (no flip on small price moves) | `services.refresh_watch_item` |
| Demo account, partial sells, auto paper trading + decision log | `services.paper_*`, `services.auto_*` |
| Alerts + Action Required queue | `services.alert`, `services.action_queue` |
| Trade journal, outcomes, calibration | `services.journal_*` |
| Database persistence (SQLite / PostgreSQL) | `core/db.py` |

## Honest limits
- Free data is delayed ~15 minutes and Yahoo access is unofficial; it can break or rate-limit.
- When the market is closed, Yahoo often shows no bid/ask. The app then serves the last real snapshot it saved,
  labelled **CACHED**, and will not issue BUY NOW from it — at most BUY IF TRIGGERED, to confirm at the open.
  A fresh install at night has no snapshot yet, so expect "no liquid contract" until the next session.
- Probabilities are model estimates. The journal measures whether they are calibrated — trust them only once
  enough outcomes have accumulated.
- Default thresholds are strict on purpose (Entry Score ≥ 80 etc.). Expect many WAIT/AVOID results. Adjust in Settings.
