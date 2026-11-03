"""
probe.py -- what does LSEG have for a PMCC backtest on this stock?

    python3 probe.py AAPL.O
    python3 probe.py MP.N

Run on your Mac with LSEG Workspace open. Read-only, about 80 requests.
Answers five questions before we design the backtest:
  1. Stock history: how far back, last price
  2. Weekly expiries: does every week have one, or monthly only?
  3. Strike grid near the money: $0.50 / $1 / $2.50 / $5?
  4. How far back expired-option prices go (daily and hourly)
  5. Which January LEAPs have data, over what dates, and how wide their spread is

Reuses rics.py from hw2-covered-call (the verified RIC rules).
"""
import sys
import warnings
import datetime as dt

import pandas as pd
import lseg.data as ld
from lseg.data.content import historical_pricing as hp

from rics import build_ric

warnings.filterwarnings("ignore")

DAILY, HOURLY = hp.Intervals.DAILY, hp.Intervals.HOURLY
FIELDS = ["BID", "ASK", "TRDPRC_1"]
TODAY = dt.date.today()


def bars(ric, start, end, interval=DAILY, fields=FIELDS):
    """One request. None if the RIC never existed or has no data."""
    try:
        df = hp.summaries.Definition(
            universe=ric, start=start.isoformat(), end=end.isoformat(),
            interval=interval, fields=fields).get_data().data.df
        return None if df is None or df.empty else df
    except Exception:
        return None


def rows(df):
    return 0 if df is None else len(df)


def round_to(x, step):
    return round(x / step) * step


def third_friday(year, month):
    d = dt.date(year, month, 15)                      # 3rd Friday is the first Friday on/after the 15th
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def main(stock):
    root = stock.split(".")[0]
    ld.open_session()

    print("RIC check:", build_ric("AAPL", dt.date(2026, 8, 7), 335, "C", as_of=dt.date(2026, 9, 1)),
          " (want AAPLH072633500.U^H26)")

    # 1. stock ------------------------------------------------------------
    px = bars(stock, TODAY - dt.timedelta(days=3 * 365), TODAY, fields=["TRDPRC_1"])
    if px is None:
        sys.exit(f"No stock data for {stock}. Check the RIC.")
    close = pd.to_numeric(px["TRDPRC_1"], errors="coerce").dropna()
    close.index = pd.to_datetime(close.index).date
    days = list(close.index)
    print(f"\n1. STOCK {stock}: {len(close)} sessions, {days[0]} to {days[-1]}, last {close.iloc[-1]:.2f}")

    # sessions grouped by week; the last session of a week is that week's expiry
    # (taken from the real tape, so a Friday holiday becomes Thursday automatically)
    this_monday = TODAY - dt.timedelta(days=TODAY.weekday())
    by_week = {}
    for d in days:
        by_week.setdefault(d.isocalendar()[:2], []).append(d)
    weeks = [(v[0], v[-1]) for _, v in sorted(by_week.items()) if v[-1] < this_monday]

    # 2. weekly cadence ---------------------------------------------------
    print("\n2. WEEKLY EXPIRIES (call, strike = Monday spot rounded to $5)")
    last_hit = None
    for entry, expiry in weeks[-12:]:
        spot = close[entry]
        ric = build_ric(root, expiry, round_to(spot, 5), "C")
        n = rows(bars(ric, entry, expiry))
        if n:
            last_hit = (entry, expiry)
        print(f"   {entry} -> {expiry}  spot {spot:8.2f}  {ric:26s} {n} rows")

    # 3. strike grid near the money --------------------------------------
    if last_hit:
        entry, expiry = last_hit
        spot = close[entry]
        base = round_to(spot, 5)
        print(f"\n3. STRIKE GRID, expiry {expiry} (spot {spot:.2f}; base {base:.2f} exists)")
        for off in (2.5, 1.0, 0.5):
            ric = build_ric(root, expiry, base + off, "C")
            print(f"   strike {base + off:8.2f}  {ric:26s} {rows(bars(ric, entry, expiry))} rows")
    else:
        print("\n3. STRIKE GRID skipped: no weekly resolved")

    # 4. history depth of expired options (monthly expiries) -------------
    print("\n4. HOW FAR BACK EXPIRED-OPTION PRICES GO (monthly call near the money, expiry week)")
    print(f"   {'expiry':10s}  {'RIC':26s} daily hourly")
    for m in (1, 3, 6, 9, 12, 18, 24):
        y, mo = TODAY.year, TODAY.month - m
        while mo <= 0:
            y, mo = y - 1, mo + 12
        prior = [d for d in days if d <= third_friday(y, mo)]
        if not prior:
            continue
        exp = prior[-1]
        start = exp - dt.timedelta(days=7)
        spot = close[[d for d in days if d >= start][0]]
        ric = build_ric(root, exp, round_to(spot, 5), "C")
        print(f"   {exp}  {ric:26s} {rows(bars(ric, start, exp)):5d} {rows(bars(ric, start, exp, HOURLY)):6d}")

    # 5. January LEAPs ----------------------------------------------------
    spot_1y = close[[d for d in days if d >= TODAY - dt.timedelta(days=365)][0]]
    k0 = round_to(0.75 * spot_1y, 5)
    print(f"\n5. JANUARY LEAPs (deep ITM call, strike near 0.75 x spot a year ago = {k0:.2f})")
    for y in range(TODAY.year - 1, TODAY.year + 3):
        exp = third_friday(y, 1)
        for k in (k0, k0 + 5, k0 - 5, k0 + 10, k0 - 10):
            ric = build_ric(root, exp, k, "C")
            df = bars(ric, TODAY - dt.timedelta(days=3 * 365), min(exp, TODAY))
            if df is None:
                continue
            q = df[["BID", "ASK"]].apply(pd.to_numeric, errors="coerce").dropna()
            spread = ((q.ASK - q.BID) / ((q.ASK + q.BID) / 2)).median() if len(q) else float("nan")
            print(f"   Jan {y}  K={k:8.2f}  {ric:26s} {len(df)} rows  "
                  f"{df.index[0].date()} to {df.index[-1].date()}  median spread {spread:.1%}")
            break
        else:
            print(f"   Jan {y}  no strike found near {k0:.2f}")

    ld.close_session()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "AAPL.O")
