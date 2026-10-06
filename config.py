"""Static configuration. User-editable thresholds live in DEFAULT_SETTINGS and are stored in the database."""
import os

# ---------------- Environment (API keys stay server-side, never sent to the browser) ----------------
DATA_MODE = os.getenv("DATA_MODE", "live").lower()          # live | demo (synthetic, for testing)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/sentry.db")
RUN_WORKER = os.getenv("RUN_WORKER", "1") == "1"
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "SentryOptions research admin@example.com")
HTTP_TIMEOUT = 20
YAHOO_MIN_INTERVAL = 0.35        # seconds between Yahoo calls (rate limit)

# ---------------- Universe: liquid US names with deep option chains ----------------
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO",
    "NFLX", "JPM", "BAC", "XOM", "UNH", "LLY", "COST", "WMT", "MU", "CRM", "ORCL", "PLTR",
    "UBER", "TSM", "CAT", "BA", "DIS", "INTC", "QCOM", "ADBE", "COIN", "SHOP", "V", "MA",
]
BENCHMARK = "SPY"
INDEXES = {"S&P 500": "^GSPC", "NASDAQ": "^IXIC", "VIX": "^VIX", "10Y Yield": "^TNX", "USD Index": "DX-Y.NYB"}

MODES = {
    "FAST":   dict(min_dte=7,   max_dte=60,  label="1-8 weeks"),
    "GROWTH": dict(min_dte=60,  max_dte=185, label="2-6 months"),
    "LEAPS":  dict(min_dte=180, max_dte=545, label="6-18 months"),
}

# ---------------- Contract selection ----------------
MAX_EXPIRIES = 6
MIN_PREMIUM = 0.10
TARGET_DELTAS = [0.70, 0.55, 0.40, 0.30]     # slightly ITM, ATM, slightly OTM, OTM
MIN_DELTA = 0.20                             # below = lottery ticket, rejected
STALE_QUOTE_DAYS = 3
SEED = 7
SPOT_VOL_BETA = 0.4
INVESTMENT = 1000.0

# annual drift, vol multiplier, jumps/yr, jump mean, jump sd, IV multiplier, earnings bias
REGIMES = {
    "normal":        dict(mu=0.07,  vol_mult=1.0, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.00, earn_bias=0),
    "bull":          dict(mu=0.25,  vol_mult=0.9, jump_lambda=1.0, jump_mean=0.01,  jump_sd=0.04, iv_mult=0.95, earn_bias=0),
    "bear":          dict(mu=-0.20, vol_mult=1.2, jump_lambda=2.0, jump_mean=-0.04, jump_sd=0.06, iv_mult=1.15, earn_bias=0),
    "high_vol":      dict(mu=0.00,  vol_mult=1.6, jump_lambda=3.0, jump_mean=-0.03, jump_sd=0.08, iv_mult=1.30, earn_bias=0),
    "low_vol":       dict(mu=0.10,  vol_mult=0.7, jump_lambda=0.5, jump_mean=0.00,  jump_sd=0.03, iv_mult=0.85, earn_bias=0),
    "crash":         dict(mu=-0.10, vol_mult=2.0, jump_lambda=6.0, jump_mean=-0.08, jump_sd=0.10, iv_mult=1.80, earn_bias=0),
    "iv_expansion":  dict(mu=0.05,  vol_mult=1.1, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.35, earn_bias=0),
    "iv_crush":      dict(mu=0.05,  vol_mult=0.9, jump_lambda=0.5, jump_mean=0.00,  jump_sd=0.03, iv_mult=0.70, earn_bias=0),
    "rate_shock":    dict(mu=-0.08, vol_mult=1.3, jump_lambda=2.0, jump_mean=-0.03, jump_sd=0.06, iv_mult=1.20, earn_bias=0),
    "earnings_beat": dict(mu=0.07,  vol_mult=1.0, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.00, earn_bias=1),
    "earnings_miss": dict(mu=0.07,  vol_mult=1.0, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.00, earn_bias=-1),
}
REGIME_WEIGHTS = {
    "normal": 0.30, "bull": 0.12, "bear": 0.12, "high_vol": 0.08, "low_vol": 0.08, "crash": 0.03,
    "iv_expansion": 0.06, "iv_crush": 0.08, "rate_shock": 0.05, "earnings_beat": 0.04, "earnings_miss": 0.04,
}
SCORE_WEIGHTS = {
    "fundamentals": 0.15, "growth": 0.10, "valuation": 0.10, "catalysts": 0.10, "macro": 0.10,
    "technical": 0.10, "options_pricing": 0.15, "mc_ev": 0.10, "liquidity": 0.05, "risk_reward": 0.05,
}
RISK_BUDGET_PCT = {"A+": 5.0, "A": 4.0, "B": 2.5, "C": 1.0, "D": 0.0}

ALERT_TYPES = {
    "wait_to_buy": "WAIT → BUY", "buy_to_hold": "BUY → HOLD", "hold_to_take_profit": "HOLD → TAKE PROFIT",
    "hold_to_sell": "HOLD → SELL", "buy_trigger": "Price reaches buy trigger", "profit_target": "Profit target reached",
    "risk_threshold": "Risk threshold reached", "catalyst": "Major catalyst", "earnings": "Earnings approaching",
    "iv_spike": "IV spike", "dte_warning": "DTE warning", "new_90": "New 90+ opportunity",
}

DEFAULT_SETTINGS = dict(
    # BUY trigger (spec 16) - all must pass
    entry_min_score=80, wait_min_score=60, min_pop=0.35, min_rr=1.2, min_ev=0.10,
    max_spread_pct=0.10, min_open_interest=100, max_iv_rv=1.5, max_atm_iv=1.0, fair_tolerance=0.05,
    # exits (spec 17/18)
    max_daily_theta_pct=0.012, max_loss_rule=0.75,
    # simulation (spec 13)
    mc_paths=10000, high_accuracy=False,
    # demo / auto paper trading (spec 7/8)
    demo_starting_cash=10000.0, auto_demo=False, auto_risk_pct=5.0, auto_max_positions=5,
    # real portfolio
    real_cash=0.0,
    # refresh
    quote_refresh_sec=60, analysis_refresh_min=20, watch_mode="GROWTH",
    universe=",".join(DEFAULT_UNIVERSE),
    alerts={k: True for k in ALERT_TYPES},
)
