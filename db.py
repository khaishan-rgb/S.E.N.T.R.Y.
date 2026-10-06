"""Persistence (spec 23). SQLite for local use, PostgreSQL in production (set DATABASE_URL).

Queries are written with '?' placeholders and translated for psycopg2. JSON is stored as TEXT.
History tables (journal, demo trades, alerts, auto log) are append-only: results are never rewritten.
"""
import json
import os
import sqlite3
import threading

from . import config

URL = config.DATABASE_URL
PG = URL.startswith(("postgres://", "postgresql://"))
_lock = threading.RLock()
_conn = None

SCHEMA = {
    "settings": "key TEXT PRIMARY KEY, value TEXT",
    "watchlist": ("id {pk}, ticker TEXT UNIQUE, favourite INTEGER DEFAULT 0, added_ts TEXT, mode TEXT, state TEXT, "
                  "pending_state TEXT, pending_count INTEGER DEFAULT 0, score {real}, result_id INTEGER, eval_ts TEXT, "
                  "price {real}, change_pct {real}, quote_ts TEXT, quote_fresh TEXT"),
    "scan_jobs": ("id {pk}, created TEXT, started TEXT, finished TEXT, mode TEXT, universe TEXT, status TEXT, "
                  "progress INTEGER DEFAULT 0, total INTEGER DEFAULT 0, message TEXT"),
    "results": ("id {pk}, ts TEXT, job_id INTEGER, source TEXT, ticker TEXT, direction TEXT, mode TEXT, state TEXT, "
                "score {real}, row TEXT, payload TEXT"),
    "positions": ("id {pk}, account TEXT, ticker TEXT, kind TEXT, strike {real}, expiry TEXT, qty {real}, "
                  "entry_premium {real}, entry_date TEXT, status TEXT DEFAULT 'OPEN', exit_premium {real}, exit_ts TEXT, "
                  "exit_reason TEXT, entry_score {real}, entry_pop {real}, entry_ev {real}, signal_ts TEXT, "
                  "thesis_level {real}, auto INTEGER DEFAULT 0, last_action TEXT, last_eval TEXT, last_eval_ts TEXT, "
                  "realised {real} DEFAULT 0, target_price {real}"),
    "demo_trades": ("id {pk}, ts TEXT, position_id INTEGER, side TEXT, ticker TEXT, contract TEXT, qty {real}, "
                    "premium {real}, reason TEXT, score {real}, pop {real}, ev {real}, pnl {real}, holding_days {real}, "
                    "auto INTEGER DEFAULT 0"),
    "demo_equity": "id {pk}, ts TEXT, equity {real}, cash {real}",
    "alerts": ("id {pk}, ts TEXT, type TEXT, ticker TEXT, title TEXT, message TEXT, urgency INTEGER, ref TEXT, "
               "is_read INTEGER DEFAULT 0"),
    "journal": ("id {pk}, ts TEXT, source TEXT, ticker TEXT, direction TEXT, mode TEXT, strategy TEXT, contract TEXT, "
                "kind TEXT, strike {real}, expiry TEXT, state TEXT, score {real}, pop {real}, ev {real}, "
                "entry_ask {real}, trigger_upper {real}, time_exit TEXT, regime TEXT, market TEXT, sim TEXT, "
                "outcome_status TEXT DEFAULT 'OPEN', outcome_return {real}, outcome_ts TEXT, outcome_note TEXT"),
    "auto_log": "id {pk}, ts TEXT, ticker TEXT, decision TEXT, reason TEXT, details TEXT",
    "chain_cache": "id {pk}, ticker TEXT, expiry TEXT, ts TEXT, data TEXT",
}


def _connect():
    global _conn
    if _conn is not None:
        return _conn
    if PG:
        import psycopg2
        _conn = psycopg2.connect(URL.replace("postgres://", "postgresql://", 1))
        _conn.autocommit = True
    else:
        path = URL.replace("sqlite:///", "")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def _sql(q):
    return q.replace("?", "%s") if PG else q


def init():
    pk = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if PG else "REAL"
    for name, cols in SCHEMA.items():
        execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols.format(pk=pk, real=real)})")
    execute("CREATE INDEX IF NOT EXISTS ix_results_job ON results(job_id)")
    execute("CREATE INDEX IF NOT EXISTS ix_chain ON chain_cache(ticker, expiry)")


def execute(q, params=(), commit=True):
    with _lock:
        c = _connect()
        cur = c.cursor()
        cur.execute(_sql(q), params)
        if commit and not PG and not q.lstrip().upper().startswith("SELECT"):
            c.commit()
        return cur


def insert(table, row):
    cols = list(row)
    q = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) RETURNING id"
    with _lock:
        cur = execute(q, [row[c] for c in cols], commit=False)
        rid = cur.fetchone()[0]
        if not PG:
            _connect().commit()
        return rid


def update(table, rid, row):
    sets = ",".join(f"{k}=?" for k in row)
    execute(f"UPDATE {table} SET {sets} WHERE id=?", list(row.values()) + [rid])


def all(q, params=()):
    with _lock:
        cur = execute(q, params)
        rows = cur.fetchall()
        if PG:
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in rows]
        return [dict(r) for r in rows]


def one(q, params=()):
    r = all(q, params)
    return r[0] if r else None


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def jdump(o):
    return json.dumps(o, default=str)


# ------------------------------------------------------------------ settings
def get_settings():
    s = json.loads(json.dumps(config.DEFAULT_SETTINGS))
    for r in all("SELECT key, value FROM settings"):
        v = jload(r["value"])
        if r["key"] == "alerts" and isinstance(v, dict):
            s["alerts"].update(v)
        elif r["key"] in s:
            s[r["key"]] = v
    return s


def save_settings(new):
    cur = get_settings()
    for k, v in new.items():
        if k not in config.DEFAULT_SETTINGS:
            continue
        d = config.DEFAULT_SETTINGS[k]
        try:
            if isinstance(d, bool):
                v = v in (True, "true", "1", 1, "on")
            elif isinstance(d, int):
                v = int(float(v))
            elif isinstance(d, float):
                v = float(v)
            elif isinstance(d, dict):
                v = {**cur[k], **{kk: bool(vv) for kk, vv in v.items()}}
        except (TypeError, ValueError):
            continue
        execute("DELETE FROM settings WHERE key=?", (k,))
        execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, jdump(v)))
    return get_settings()


# ------------------------------------------------------------------ last-session chain snapshots
def save_chain(sym, exp, chain):
    data = {k: df.drop(columns=[c for c in df.columns if c == "lastTradeDate"]).to_dict("list")
            for k, df in chain.items()}
    from .providers import iso
    execute("DELETE FROM chain_cache WHERE ticker=? AND expiry=?", (sym, exp))
    execute("INSERT INTO chain_cache (ticker, expiry, ts, data) VALUES (?,?,?,?)", (sym, exp, iso(), jdump(data)))


def load_chain(sym, exp):
    import pandas as pd
    r = one("SELECT ts, data FROM chain_cache WHERE ticker=? AND expiry=?", (sym, exp))
    if not r:
        return None
    age_days = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(r["ts"])).days
    if age_days > 4:
        return None
    d = jload(r["data"], {})
    return {k: pd.DataFrame(v) for k, v in d.items()}, r["ts"]
