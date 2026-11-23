"""
live_iv.py -- at-the-money implied volatility for tonight's decision, from LSEG.

    iv = atm_iv("GOOGL", spot=264.0, session=date(2026, 11, 24))

How: build the OPRA RICs for the near-the-money put and call on the weekly expiry
the order will trade (the first Friday at least 5 days after the session), pull the
last few daily bars (BID / ASK) with the headless platform session, take the latest
midpoint, and back out Black-Scholes implied vol for each. The ATM IV is the average
of the put and call IV at the strike closest to the spot.

Only derived numbers leave this module (IV, strike, expiry, contract count). Raw LSEG
quotes are never printed or saved, per the library's rule for this course.
"""
import datetime as dt
import math
import os
from concurrent.futures import ThreadPoolExecutor

import bs
from rics import build_ric

R = 0.04          # risk-free rate used for the IV inversion (3-month T-bill level)
Q = 0.003         # Alphabet dividend yield (about $0.84 a year)
STEP = 2.5        # Alphabet lists $2.50 strikes near the money


def open_session():
    import lseg.data as ld
    key, user, pw = (os.environ.get(k) for k in ("LSEG_APP_KEY", "LSEG_USERNAME", "LSEG_PASSWORD"))
    if not (key and user and pw):
        raise RuntimeError("LSEG secrets not set")
    s = ld.session.platform.Definition(
        app_key=key, grant=ld.session.platform.GrantPassword(username=user, password=pw),
        signon_control=True).get_session()
    s.open()
    ld.session.set_default(s)
    return s


def target_expiries(session, n=2):
    """The first n Fridays at least 5 calendar days after the session (the 5DTE+ weekly)."""
    d = session + dt.timedelta(days=5)
    d += dt.timedelta(days=(4 - d.weekday()) % 7)
    return [d + dt.timedelta(days=7 * i) for i in range(n)]


def last_mid(ric, start, end):
    """Latest (date, mid) from daily bars, or None. Nothing is printed."""
    from lseg.data.content import historical_pricing as hp
    try:
        df = hp.summaries.Definition(universe=ric, start=start.isoformat(), end=end.isoformat(),
                                     interval=hp.Intervals.DAILY, fields=["BID", "ASK"]).get_data().data.df
    except Exception:
        return None
    if df is None or df.empty or "BID" not in df or "ASK" not in df:
        return None
    for t, row in df.sort_index(ascending=False).iterrows():
        try:
            b, a = float(row["BID"]), float(row["ASK"])
        except (TypeError, ValueError):
            continue
        if b > 0 and a >= b:
            return (t.date() if hasattr(t, "date") else t), (b + a) / 2
    return None


def atm_iv(root, spot, session, today=None):
    """Returns dict(iv, strike, expiry, n) or dict(iv=None, reason=...)."""
    today = today or dt.date.today()
    start, end = today - dt.timedelta(days=8), today + dt.timedelta(days=1)
    k0 = round(spot / STEP) * STEP
    strikes = [k0 - STEP, k0, k0 + STEP]
    for expiry in target_expiries(session):
        jobs = [(K, cp, build_ric(root, expiry, K, cp, as_of=today)) for K in strikes for cp in ("C", "P")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            got = list(pool.map(lambda j: last_mid(j[2], start, end), jobs))
        ivs = {}
        for (K, cp, _), res in zip(jobs, got):
            if not res:
                continue
            asof, mid = res
            T = max((expiry - asof).days, 1) / 365
            v = (bs.implied_vol if cp == "C" else bs.implied_vol_put)(mid, spot, K, T, R, Q)
            if v and 0.03 < v < 3:
                ivs.setdefault(K, {})[cp] = v
        if ivs:
            K = min(ivs, key=lambda k: abs(k - spot))
            vals = list(ivs[K].values())
            return dict(iv=round(sum(vals) / len(vals), 4), strike=K, expiry=str(expiry),
                        n=sum(len(v) for v in ivs.values()))
    return dict(iv=None, reason="no near-the-money quotes found")
