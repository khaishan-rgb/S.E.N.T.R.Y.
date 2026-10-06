"""Option pricing models (spec section 8). All model outputs are ESTIMATES, not market data."""
import math
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


def bs_price(S, K, T, r, sigma, kind="call", q=0.0):
    """Vectorised Black-Scholes. S, T, sigma may be numpy arrays."""
    S = np.asarray(S, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    intrinsic = np.maximum(S - K, 0.0) if kind == "call" else np.maximum(K - S, 0.0)
    Tm = np.maximum(T, 1e-8)
    sm = np.maximum(sigma, 1e-6)
    sqT = np.sqrt(Tm)
    d1 = (np.log(S / K) + (r - q + 0.5 * sm ** 2) * Tm) / (sm * sqT)
    d2 = d1 - sm * sqT
    if kind == "call":
        v = S * np.exp(-q * Tm) * norm.cdf(d1) - K * np.exp(-r * Tm) * norm.cdf(d2)
    else:
        v = K * np.exp(-r * Tm) * norm.cdf(-d2) - S * np.exp(-q * Tm) * norm.cdf(-d1)
    out = np.where(T <= 1e-6, intrinsic, np.maximum(v, 0.0))
    return out if out.ndim else float(out)


def greeks(S, K, T, r, sigma, kind="call", q=0.0):
    """Delta, gamma, theta (per calendar day), vega (per 1 IV point), rho (per 1% rate)."""
    T = max(T, 1e-6)
    sqT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    pdf = norm.pdf(d1)
    eq, er = math.exp(-q * T), math.exp(-r * T)
    gamma = eq * pdf / (S * sigma * sqT)
    vega = S * eq * pdf * sqT / 100
    if kind == "call":
        delta = eq * norm.cdf(d1)
        theta = (-S * eq * pdf * sigma / (2 * sqT) - r * K * er * norm.cdf(d2) + q * S * eq * norm.cdf(d1)) / 365
        rho = K * T * er * norm.cdf(d2) / 100
    else:
        delta = -eq * norm.cdf(-d1)
        theta = (-S * eq * pdf * sigma / (2 * sqT) + r * K * er * norm.cdf(-d2) - q * S * eq * norm.cdf(-d1)) / 365
        rho = -K * T * er * norm.cdf(-d2) / 100
    return dict(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def binomial_american(S, K, T, r, sigma, kind="call", q=0.0, steps=200):
    """Cox-Ross-Rubinstein tree with early exercise (US equity options are American)."""
    if T <= 0:
        return max(S - K, 0) if kind == "call" else max(K - S, 0)
    dt = T / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1 / u
    p = (math.exp((r - q) * dt) - d) / (u - d)
    p = min(max(p, 0.0), 1.0)
    disc = math.exp(-r * dt)
    j = np.arange(steps + 1)
    prices = S * u ** (steps - j) * d ** j
    vals = np.maximum(prices - K, 0) if kind == "call" else np.maximum(K - prices, 0)
    for i in range(steps - 1, -1, -1):
        j = np.arange(i + 1)
        prices = S * u ** (i - j) * d ** j
        cont = disc * (p * vals[:-1] + (1 - p) * vals[1:])
        exer = np.maximum(prices - K, 0) if kind == "call" else np.maximum(K - prices, 0)
        vals = np.maximum(cont, exer)
    return float(vals[0])


def implied_vol(price, S, K, T, r, kind="call", q=0.0):
    """Solve IV from an observed price. Returns None if no valid solution."""
    if price is None or T <= 0 or price <= 0:
        return None
    intrinsic = max(S - K, 0) if kind == "call" else max(K - S, 0)
    if price < intrinsic * 0.999 or price > S:
        return None
    f = lambda s: bs_price(S, K, T, r, s, kind, q) - price
    try:
        if f(0.01) > 0 or f(5.0) < 0:
            return None
        return brentq(f, 0.01, 5.0, xtol=1e-5)
    except Exception:
        return None
