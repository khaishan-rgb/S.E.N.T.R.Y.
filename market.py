"""US market clock + market overview."""
import datetime as dt
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
HOLIDAYS = {  # NYSE full-day closures
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}


def market_status(now=None):
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
    t = now.hour * 60 + now.minute
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in HOLIDAYS:
        st = "CLOSED"
    elif 4 * 60 <= t < 9 * 60 + 30:
        st = "PRE-MARKET"
    elif 9 * 60 + 30 <= t < 16 * 60:
        st = "OPEN"
    elif 16 * 60 <= t < 20 * 60:
        st = "AFTER-HOURS"
    else:
        st = "CLOSED"
    return dict(status=st, ny_time=now.strftime("%a %d %b %Y %H:%M ET"))


def overview():
    from .providers import providers
    from . import config
    P = providers()
    out = []
    for label, sym in config.INDEXES.items():
        q, m = P.stock.quote(sym if P.mode != "demo" else "IDX" + label)
        h, _ = P.stock.history(sym if P.mode != "demo" else "IDX" + label)
        if q is None:
            out.append(dict(label=label, value=None, change=None, spark=[], meta=m))
            continue
        last, prev = q
        scale = 0.1 if sym == "^TNX" and P.mode != "demo" and last > 20 else 1.0   # old Yahoo ^TNX = yield x10
        spark = [] if h is None else [round(float(x) * scale, 4) for x in h["Close"].tail(22)]
        out.append(dict(label=label, value=last * scale, change=(last / prev - 1) if prev else None, spark=spark, meta=m))
    return out
