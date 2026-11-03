"""
bs.py -- Black-Scholes(-Merton) call price, delta and implied volatility.

    d1 = [ln(S/K) + (r - q + sigma^2/2) T] / (sigma sqrt(T)),   d2 = d1 - sigma sqrt(T)
    C  = S e^{-qT} N(d1) - K e^{-rT} N(d2)
    delta = e^{-qT} N(d1)

q is the dividend yield (0 for MP). N is the standard normal CDF.
Used only to read delta off market prices (strike selection) -- fills always
come from real bid/ask quotes, never from this model.
"""
from math import erf, exp, log, sqrt


def N(x):
    return 0.5 * (1 + erf(x / sqrt(2)))


def d1(S, K, T, r, q, v):
    return (log(S / K) + (r - q + 0.5 * v * v) * T) / (v * sqrt(T))


def call(S, K, T, r, q, v):
    if T <= 0 or v <= 0:
        return max(S * exp(-q * T) - K * exp(-r * T), 0.0)
    a = d1(S, K, T, r, q, v)
    return S * exp(-q * T) * N(a) - K * exp(-r * T) * N(a - v * sqrt(T))


def delta(S, K, T, r, q, v):
    return exp(-q * T) * N(d1(S, K, T, r, q, v))


def implied_vol(price, S, K, T, r, q, lo=1e-3, hi=5.0):
    """Bisection. None if the price is outside what any volatility in [lo, hi] can produce
    (e.g. a mid below the no-arbitrage floor on a deep in-the-money call)."""
    if T <= 0 or price <= 0:
        return None
    if not call(S, K, T, r, q, lo) <= price <= call(S, K, T, r, q, hi):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if call(S, K, T, r, q, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
