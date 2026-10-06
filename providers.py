"""Data provider adapters (spec 21, 22).

Each category is a separate adapter so a premium API can replace a free one without touching the engines:
  StockData   -> quotes + history          (YahooStock | DemoData)
  OptionsData -> expiries + chains         (YahooOptions | DemoData)
  CompanyInfo -> profile, earnings dates   (YahooInfo | DemoData)
  Fundamentals-> audited financials        (EdgarFundamentals | DemoData)
  Macro       -> rates, credit, VIX, USD   (FredMacro | DemoData)
  News        -> headlines, 2nd price      (FinnhubNews | NullNews | DemoData)

Every response carries meta: source, timestamp, freshness, confidence. Freshness is one of
LIVE, 15-MIN DELAY, CACHED, STALE, UNAVAILABLE, SYNTHETIC. Delayed data is never labelled LIVE.
Missing data is reported as missing - never invented.
"""
import datetime as dt
import hashlib
import io
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

from . import config
from .market import market_status
from .pricing import bs_price


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts=None):
    return (ts or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def meta(source, freshness, confidence, note="", ts=None):
    return dict(source=source, timestamp=iso(ts), freshness=freshness, confidence=confidence, note=note)


# ------------------------------------------------------------------ cache + rate limit
class TTLCache:
    def __init__(self):
        self._d, self._lock = {}, threading.Lock()

    def get(self, key, ttl):
        with self._lock:
            v = self._d.get(key)
        if v and time.time() - v[1] < ttl:
            return v[0], time.time() - v[1]
        return None, None

    def set(self, key, value):
        with self._lock:
            self._d[key] = (value, time.time())


class RateLimiter:
    def __init__(self, min_interval):
        self.min_interval, self.last, self.lock = min_interval, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            gap = self.min_interval - (time.time() - self.last)
            if gap > 0:
                time.sleep(gap)
            self.last = time.time()


CACHE = TTLCache()


class ChainStore:
    """Hook set by the app so the last real chain snapshot can be persisted in the database.
    When the market is closed, that snapshot is served - clearly labelled CACHED with its time - and
    the decision engine refuses to give actionable BUY NOW prices from it."""
    save = staticmethod(lambda sym, exp, chain: None)
    load = staticmethod(lambda sym, exp: None)      # -> (chain_dict, iso_ts) or None


def cached(key, ttl, fn):
    """Return (value, meta) - marks served-from-cache results as CACHED with their age."""
    val, age = CACHE.get(key, ttl)
    if val is not None:
        v, m = val
        if age > 90 and m["freshness"] not in ("SYNTHETIC", "UNAVAILABLE"):
            m = dict(m, freshness="CACHED", note=f"cached {int(age)}s ago; originally {m['freshness']}")
        return v, m
    v, m = fn()
    if v is not None:
        CACHE.set(key, (v, m))
    return v, m


@dataclass
class TickerData:
    ticker: str
    name: str = ""
    price: float = None
    prev_close: float = None
    price_time: str = None
    hist: pd.DataFrame = None
    chains: dict = field(default_factory=dict)
    chain_status: str = "UNAVAILABLE"
    info: dict = field(default_factory=dict)
    fundamentals: dict = field(default_factory=dict)
    earnings_date: dt.date = None
    past_earnings_moves: list = field(default_factory=list)
    news: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)      # (SEVERE|MINOR, message)
    sources: list = field(default_factory=list)    # meta dicts
    is_etf: bool = False
    dividend_yield: float = 0.0

    @property
    def change_pct(self):
        return self.price / self.prev_close - 1 if self.price and self.prev_close else None


def _delay_label():
    st = market_status()["status"]
    return "15-MIN DELAY", ("" if st == "OPEN" else f"market {st.lower()}: last session values")


# ------------------------------------------------------------------ Yahoo (free, unofficial, delayed)
class Yahoo:
    name = "Yahoo Finance"
    limiter = RateLimiter(config.YAHOO_MIN_INTERVAL)

    def _t(self, sym):
        import yfinance as yf
        self.limiter.wait()
        return yf.Ticker(sym)

    def history(self, sym, period="5y"):
        def f():
            try:
                h = self._t(sym).history(period=period, auto_adjust=True)
                if h is None or h.empty:
                    return None, meta(self.name, "UNAVAILABLE", "NONE", "no history")
                h.index = pd.DatetimeIndex(h.index).tz_localize(None)
                return h, meta(self.name, "15-MIN DELAY", "MEDIUM", "daily bars")
            except Exception as ex:
                return None, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("hist", sym, period), 3600, f)

    def quote(self, sym):
        def f():
            try:
                t = self._t(sym)
                fi = t.fast_info
                last, prev = float(fi.last_price), float(fi.previous_close)
                if not last or last <= 0:
                    raise ValueError("no price")
                fr, note = _delay_label()
                return (last, prev), meta(self.name, fr, "MEDIUM", note)
            except Exception as ex:
                return None, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("quote", sym), 60, f)

    def expiries(self, sym):
        def f():
            try:
                return list(self._t(sym).options or []), meta(self.name, "15-MIN DELAY", "MEDIUM")
            except Exception as ex:
                return None, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("exp", sym), 3600, f)

    def chain(self, sym, exp):
        def f():
            try:
                ch = self._t(sym).option_chain(exp)
                calls, puts = ch.calls.copy(), ch.puts.copy()
                live = ((calls["bid"].fillna(0) > 0) & (calls["ask"].fillna(0) > 0)).mean() if len(calls) else 0
                if live < 0.2:
                    snap = ChainStore.load(sym, exp)
                    if snap:
                        return snap[0], meta(self.name, "CACHED", "LOW", f"last-session snapshot taken {snap[1]}")
                    return {"calls": calls, "puts": puts}, meta(self.name, "UNAVAILABLE", "LOW",
                                                                "no live bid/ask (market closed?)")
                fr, note = _delay_label()
                ChainStore.save(sym, exp, {"calls": calls, "puts": puts})
                return {"calls": calls, "puts": puts}, meta(self.name, fr, "MEDIUM", note)
            except Exception as ex:
                return None, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("chain", sym, exp), 300, f)

    def info(self, sym):
        def f():
            try:
                return (self._t(sym).info or {}), meta(self.name, "15-MIN DELAY", "MEDIUM", "profile/targets")
            except Exception as ex:
                return {}, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("info", sym), 86400, f)

    def earnings(self, sym, hist):
        def f():
            nxt, moves = None, []
            today = dt.date.today()
            try:
                t = self._t(sym)
                cal = t.calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    fut = [d for d in dates if d >= today]
                    nxt = fut[0] if fut else None
                ed = t.get_earnings_dates(limit=16)
                closes = hist["Close"].copy()
                idx = [d.date() for d in closes.index]
                for ts in ed.index:
                    d = ts.date()
                    if d >= today:
                        continue
                    pos = next((i for i, x in enumerate(idx) if x >= d), None)
                    if pos and 0 < pos < len(idx) - 1:
                        moves.append(float(abs(closes.iloc[pos + 1] / closes.iloc[pos - 1] - 1)))
            except Exception:
                pass
            return (nxt, moves), meta(self.name, "15-MIN DELAY", "LOW", "earnings calendar (unofficial)")
        return cached(("earn", sym), 43200, f)


# ------------------------------------------------------------------ SEC EDGAR (free, official)
class EdgarFundamentals:
    name = "SEC EDGAR"
    limiter = RateLimiter(0.15)
    _cik = None

    def _get(self, url):
        self.limiter.wait()
        r = requests.get(url, headers={"User-Agent": config.SEC_USER_AGENT}, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _cik_for(self, sym):
        if EdgarFundamentals._cik is None:
            data = self._get("https://www.sec.gov/files/company_tickers.json")
            EdgarFundamentals._cik = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        return EdgarFundamentals._cik.get(sym.upper().replace("-", "."))

    @staticmethod
    def _annual(facts, tags, unit="USD", duration=True):
        best = []
        for tag in tags:
            node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
            if not node:
                continue
            vals = {}
            for r in node.get("units", {}).get(unit, []):
                if r.get("form") not in ("10-K", "10-K/A", "20-F", "40-F"):
                    continue
                if duration:
                    if not r.get("start"):
                        continue
                    days = (pd.Timestamp(r["end"]) - pd.Timestamp(r["start"])).days
                    if not 330 <= days <= 400:
                        continue
                vals[r["end"]] = r["val"]
            if vals:
                ser = sorted(vals.items())
                if not best or ser[-1][0] > best[-1][0]:
                    best = ser
        return best

    def parse(self, facts):
        A = self._annual
        rev = A(facts, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                        "RevenueFromContractWithCustomerIncludingAssessedTax"])
        ni, ocf = A(facts, ["NetIncomeLoss"]), A(facts, ["NetCashProvidedByUsedInOperatingActivities"])
        capex = A(facts, ["PaymentsToAcquirePropertyPlantAndEquipment"])
        cash = A(facts, ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], duration=False)
        debt = A(facts, ["LongTermDebt", "LongTermDebtNoncurrent"], duration=False)
        eq = A(facts, ["StockholdersEquity"], duration=False)
        f = {}
        if len(rev) >= 2 and rev[-2][1]:
            f.update(revenue=rev[-1][1], rev_growth=rev[-1][1] / rev[-2][1] - 1, fiscal_year_end=rev[-1][0])
        elif rev:
            f["revenue"] = rev[-1][1]
        if ni:
            f["net_income"] = ni[-1][1]
            if len(ni) >= 2 and ni[-2][1] > 0:
                f["ni_growth"] = ni[-1][1] / ni[-2][1] - 1
        if ocf:
            f["fcf"] = ocf[-1][1] - (capex[-1][1] if capex else 0)
        f["cash"], f["debt"] = (cash[-1][1] if cash else 0.0), (debt[-1][1] if debt else 0.0)
        if eq:
            f["equity"] = eq[-1][1]
        try:
            sh = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
            f["shares"] = sorted(sh, key=lambda r: r["end"])[-1]["val"]
        except Exception:
            pass
        return f

    def fundamentals(self, sym):
        def f():
            try:
                cik = self._cik_for(sym)
                if not cik:
                    return {}, meta(self.name, "UNAVAILABLE", "NONE", "not an SEC filer (ETF/foreign?)")
                data = self.parse(self._get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
                return data, meta(self.name, "LIVE", "HIGH", f"latest annual filing {data.get('fiscal_year_end', '')}")
            except Exception as ex:
                return {}, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("edgar", sym), 86400, f)


# ------------------------------------------------------------------ FRED (free, official)
class FredMacro:
    name = "FRED (St. Louis Fed)"
    SERIES = ["DGS10", "DGS3MO", "T10Y2Y", "BAMLH0A0HYM2", "VIXCLS", "DTWEXBGS", "DCOILWTICO"]

    def series(self):
        def f():
            out = {}
            for sid in self.SERIES:
                try:
                    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
                    df = pd.read_csv(io.StringIO(requests.get(url, timeout=config.HTTP_TIMEOUT).text))
                    out[sid] = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                                         index=pd.to_datetime(df.iloc[:, 0])).dropna()
                except Exception:
                    pass
            if not out:
                return {}, meta(self.name, "UNAVAILABLE", "NONE", "FRED unreachable")
            return out, meta(self.name, "LIVE", "HIGH", "daily official series (1-day lag)")
        return cached(("fred",), 21600, f)


# ------------------------------------------------------------------ Finnhub (optional free key)
POS = ("beat", "raise", "upgrade", "record", "surge", "approval", "approved", "wins", "contract", "buyback", "partnership")
NEG = ("miss", "cut", "downgrade", "lawsuit", "probe", "recall", "plunge", "investigation", "delay", "warning", "fraud")


class FinnhubNews:
    name = "Finnhub"
    limiter = RateLimiter(1.1)

    def _get(self, path, **params):
        self.limiter.wait()
        params["token"] = config.FINNHUB_KEY
        return requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=config.HTTP_TIMEOUT).json()

    def quote(self, sym):
        def f():
            try:
                c = self._get("quote", symbol=sym).get("c")
                return (c if c else None), meta(self.name, "LIVE", "MEDIUM", "cross-check price")
            except Exception as ex:
                return None, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("fhq", sym), 60, f)

    def news(self, sym):
        def f():
            try:
                to = dt.date.today()
                items = self._get("company-news", symbol=sym, **{"from": str(to - dt.timedelta(days=14)), "to": str(to)})
                heads = [i.get("headline", "") for i in items[:60]]
                low = [h.lower() for h in heads]
                p = sum(any(w in h for w in POS) for h in low)
                n = sum(any(w in h for w in NEG) for h in low)
                return dict(count=len(heads), positive=p, negative=n, sentiment=(p - n) / max(1, p + n),
                            headlines=heads[:5]), meta(self.name, "LIVE", "LOW", "keyword sentiment")
            except Exception as ex:
                return {}, meta(self.name, "UNAVAILABLE", "NONE", str(ex)[:120])
        return cached(("fhn", sym), 1800, f)


class NullNews:
    name = "News"

    def quote(self, sym):
        return None, meta(self.name, "UNAVAILABLE", "NONE", "set FINNHUB_API_KEY for a 2nd price source")

    def news(self, sym):
        return {}, meta(self.name, "UNAVAILABLE", "NONE", "set FINNHUB_API_KEY for news")


# ------------------------------------------------------------------ Synthetic demo data (clearly labelled)
class DemoData:
    """Deterministic synthetic market for testing the app offline. Every value is labelled SYNTHETIC."""
    name = "DEMO synthetic data"

    IDX = {"IDXS&P 500": (5760, 0.15), "IDXNASDAQ": (18250, 0.2), "IDXVIX": (17.4, 0.9), "IDX10Y Yield": (4.02, 0.2),
           "IDXUSD Index": (105.2, 0.06)}

    def _p(self, sym):
        h = int(hashlib.md5(sym.encode()).hexdigest(), 16)
        if sym in self.IDX:
            S, v = self.IDX[sym]
            return dict(seed=h % (2 ** 32), S=S, vol=v, drift=0.05, ivr=1.0, skew=0.2, prof="average", earn=None)
        rnd = np.random.default_rng(h % (2 ** 32))
        return dict(seed=h % (2 ** 32), S=float(rnd.uniform(25, 900)), vol=float(rnd.uniform(0.2, 0.6)),
                    drift=float(rnd.uniform(-0.3, 0.6)), ivr=float(rnd.uniform(0.7, 1.35)),
                    skew=float(rnd.uniform(0.15, 0.35)), prof=["quality", "average", "weak"][h % 3],
                    earn=[None, 12, 25, 40, 70][h % 5])

    def _m(self, note=""):
        return meta(self.name, "SYNTHETIC", "NONE", note or "not real market data")

    def history(self, sym, period="5y"):
        p = self._p(sym)
        rng = np.random.default_rng(p["seed"])
        n = 1260
        r = rng.normal(p["drift"] / 252, p["vol"] / np.sqrt(252), n)
        close = np.exp(np.cumsum(r))
        close = close / close[-1] * p["S"]
        op = close * np.exp(rng.normal(0, p["vol"] / np.sqrt(252) / 3, n))
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=n)
        return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close,
                             "Volume": rng.integers(5e6, 2e7, n).astype(float)}, index=idx), self._m("daily bars")

    def quote(self, sym):
        p = self._p(sym)
        minute = int(time.time() // 60)
        wiggle = 0.004 * np.sin(minute / 7 + p["seed"] % 10) + 0.002 * np.sin(minute / 3.1)
        return (p["S"] * (1 + wiggle), p["S"]), self._m()

    def expiries(self, sym):
        today = dt.date.today()
        out = []
        for d in (10, 24, 38, 52, 80, 115, 150, 185, 240, 300, 365, 450, 530):
            e = today + dt.timedelta(days=d)
            e += dt.timedelta(days=(4 - e.weekday()) % 7)      # Friday
            out.append(str(e))
        return sorted(set(out)), self._m()

    def chain(self, sym, exp):
        p = self._p(sym)
        S = self.quote(sym)[0][0]
        rng = np.random.default_rng(p["seed"] + int(hashlib.md5(exp.encode()).hexdigest(), 16) % 1000)
        T = max((dt.date.fromisoformat(exp) - dt.date.today()).days, 1) / 365
        base_iv = p["vol"] * p["ivr"]
        step = 1 if S < 60 else 2.5 if S < 150 else 5 if S < 400 else 10
        out = {}
        for kind in ("call", "put"):
            rows = []
            for K in np.arange(round(S * 0.6 / step) * step, S * 1.5, step):
                iv = base_iv * (1 + p["skew"] * (-np.log(K / S))) * (1 + 0.04 * np.sqrt(T))
                mid = float(bs_price(S, K, T, 0.04, iv, kind))
                if mid < 0.05:
                    continue
                w = max(0.02, mid * rng.uniform(0.015, 0.06))
                oi = int(8000 * np.exp(-(np.log(K / S) / 0.2) ** 2) * rng.uniform(0.5, 1.5))
                rows.append(dict(contractSymbol=f"{sym}{exp}{kind[0]}{K}", strike=float(K),
                                 bid=round(mid - w / 2, 2), ask=round(mid + w / 2, 2), lastPrice=round(mid, 2),
                                 change=round(mid * rng.normal(0, 0.04), 2), volume=int(oi * 0.1),
                                 openInterest=oi, lastTradeDate=pd.Timestamp.now(tz="UTC")))
            out[kind + "s"] = pd.DataFrame(rows)
        return out, self._m()

    def info(self, sym):
        p = self._p(sym)
        return {"longName": f"{sym} (synthetic)", "quoteType": "ETF" if sym in ("SPY", "QQQ", "IWM") else "EQUITY",
                "targetMeanPrice": p["S"] * (1.15 if p["prof"] == "quality" else 0.97),
                "numberOfAnalystOpinions": 20, "forwardPE": 25}, self._m()

    def earnings(self, sym, hist):
        p = self._p(sym)
        if p["earn"] is None:
            return (None, []), self._m()
        return (dt.date.today() + dt.timedelta(days=p["earn"]), [0.04, 0.06, 0.03, 0.07]), self._m()

    def fundamentals(self, sym):
        prof = self._p(sym)["prof"]
        F = {"quality": dict(revenue=50e9, rev_growth=0.22, net_income=12e9, ni_growth=0.3, fcf=13e9, cash=30e9,
                             debt=10e9, equity=60e9, shares=1.0e9),
             "average": dict(revenue=20e9, rev_growth=0.06, net_income=2e9, ni_growth=0.05, fcf=2.2e9, cash=3e9,
                             debt=8e9, equity=15e9, shares=0.6e9),
             "weak": dict(revenue=5e9, rev_growth=-0.08, net_income=-0.4e9, fcf=-0.6e9, cash=0.5e9, debt=4e9,
                          equity=2e9, shares=0.3e9)}
        return dict(F[prof]), self._m()

    def series(self):
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
        f = lambda b, d: pd.Series(b + np.linspace(0, d, 300), index=idx)
        return {"DGS10": f(4.2, 0.1), "DGS3MO": f(4.0, -0.2), "T10Y2Y": f(0.3, 0.1), "BAMLH0A0HYM2": f(3.2, -0.1),
                "VIXCLS": f(17, -2), "DTWEXBGS": f(120, 1), "DCOILWTICO": f(75, 3)}, self._m()

    def news(self, sym):
        return {}, self._m("no news in demo")


# ------------------------------------------------------------------ registry
class Providers:
    def __init__(self, mode=config.DATA_MODE):
        self.mode = mode
        if mode == "demo":
            d = DemoData()
            self.stock = self.options = self.info = self.fundamentals = self.macro = self.news = d
        else:
            y = Yahoo()
            self.stock = self.options = self.info = y
            self.fundamentals = EdgarFundamentals()
            self.macro = FredMacro()
            self.news = FinnhubNews() if config.FINNHUB_KEY else NullNews()

    def describe(self):
        return dict(mode=self.mode, stock=self.stock.name, options=self.options.name, fundamentals=self.fundamentals.name,
                    macro=self.macro.name, news=self.news.name, finnhub_key_set=bool(config.FINNHUB_KEY))


_PROV = None


def providers():
    global _PROV
    if _PROV is None:
        _PROV = Providers()
    return _PROV


def load_ticker(sym, min_dte=7, max_dte=545, expiries=None, max_expiries=config.MAX_EXPIRIES, full=True):
    """Assemble everything the engines need for one ticker. Problems become flags, never invented values."""
    P = providers()
    sym = sym.upper().strip()
    td = TickerData(sym)
    hist, m = P.stock.history(sym)
    td.sources.append(dict(m, item="Price history"))
    if hist is None:
        td.flags.append(("SEVERE", "No price history"))
        return td
    td.hist = hist
    q, m = P.stock.quote(sym)
    td.sources.append(dict(m, item="Stock quote"))
    if q:
        td.price, td.prev_close = q
    else:
        td.price, td.prev_close = float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2])
        td.flags.append(("MINOR", "Quote unavailable - using last daily close"))
    td.price_time = m["timestamp"]
    if m["freshness"] in ("15-MIN DELAY", "CACHED"):
        td.flags.append(("MINOR", "Prices are delayed ~15 min (free data) - never real-time"))

    info, m = P.info.info(sym)
    td.info = info or {}
    td.name = td.info.get("longName") or td.info.get("shortName") or sym
    td.is_etf = td.info.get("quoteType") == "ETF"
    dy = td.info.get("dividendYield") or 0.0
    td.dividend_yield = float(dy) / 100 if dy and dy > 0.2 else float(dy or 0.0)

    # option chains
    if expiries is None:
        exps, m = P.options.expiries(sym)
        today = dt.date.today()
        exps = [e for e in (exps or []) if min_dte <= (dt.date.fromisoformat(e) - today).days <= max_dte]
        if len(exps) > max_expiries:
            idx = np.linspace(0, len(exps) - 1, max_expiries).round().astype(int)
            exps = [exps[i] for i in sorted(set(idx))]
    else:
        exps = expiries
    statuses = []
    for e in exps:
        ch, m = P.options.chain(sym, e)
        if ch is not None:
            td.chains[e] = ch
            statuses.append(m["freshness"])
            last_chain_meta = m
    if td.chains:
        td.sources.append(dict(last_chain_meta, item=f"Option chains ({len(td.chains)} expiries)"))
        usable = [s for s in statuses if s != "UNAVAILABLE"]
        td.chain_status = usable[0] if usable else "UNAVAILABLE"
        if td.chain_status == "UNAVAILABLE":
            td.flags.append(("SEVERE", "Option chain has no live bid/ask (market closed?) - no actionable entry prices"))
        elif td.chain_status == "CACHED":
            td.flags.append(("MINOR", "Option quotes are a CACHED last-session snapshot - confirm at the open"))
    else:
        td.flags.append(("SEVERE", "No option chains for this window"))

    if not full:
        return td
    (nxt, moves), m = P.info.earnings(sym, hist)
    td.earnings_date, td.past_earnings_moves = nxt, moves
    td.sources.append(dict(m, item="Earnings calendar"))
    if not td.is_etf:
        fu, m = P.fundamentals.fundamentals(sym)
        td.fundamentals = fu or {}
        td.sources.append(dict(m, item="Fundamentals"))
        if not fu:
            td.flags.append(("MINOR", "No fundamentals available"))
    xq, m = P.news.quote(sym)
    if xq and P.mode != "demo":
        td.sources.append(dict(m, item="Price cross-check"))
        if abs(xq / td.price - 1) > 0.015:
            td.flags.append(("SEVERE", f"PRICE CONFLICT: {td.price:.2f} vs {xq:.2f} (second source)"))
    elif P.mode != "demo":
        td.flags.append(("MINOR", "Price not cross-validated (no second source configured)"))
    nw, m = P.news.news(sym)
    td.news = nw or {}
    td.sources.append(dict(m, item="News"))
    if P.mode == "demo":
        td.flags.append(("MINOR", "DEMO MODE: synthetic data, not real prices"))
    last = pd.Timestamp(hist.index[-1])
    if (pd.Timestamp.now() - last).days > 5:
        td.flags.append(("SEVERE", f"STALE price history (last bar {last.date()})"))
    return td
