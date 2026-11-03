"""
fetch.py -- pull everything the PMCC backtest needs from LSEG, once.

    python3 fetch.py AAPL.O --step 2.5 --far-step 5 --otm 0.08 --leap-step 5 --leap-lo 0.5
    python3 fetch.py MP.N   --step 0.5 --far-step 1 --otm 0.15 --leap-step 2.5

Writes data/raw_{ROOT}.json. Everything downstream reads only that file, so the
backtest reruns without LSEG (and survives LSEG's rolling ~1-year hourly window).
Resumable: rerun after a crash and it skips weeks already fetched.

What it pulls (strike steps come from probe.py):
  stock     hourly BID/ASK/TRDPRC_1 for the last ~13 months, daily for 18 months
  weeklies  every week: calls from one strike below the Monday spot up to +otm,
            hourly, Monday through expiry
  LEAPs     January calls (default 2027 and 2028), strikes from leap-lo x to 1.0x
            the start spot: daily for the whole window, hourly for the first 3 weeks
            (entry) and the last week (exit)
  rates     3-month T-bill (US3MT=RR), daily, for Black-Scholes delta
  dividends ex-dates and amounts (shareholders get them, LEAP holders do not)
"""
import argparse
import json
import math
import os
import time
import warnings
import datetime as dt

import numpy as np
import pandas as pd
import lseg.data as ld
from lseg.data.content import historical_pricing as hp

from rics import build_ric

warnings.filterwarnings("ignore")

DAILY, HOURLY = hp.Intervals.DAILY, hp.Intervals.HOURLY
FIELDS = ["BID", "ASK", "TRDPRC_1"]
TODAY = dt.date.today()
DAY = dt.timedelta(days=1)
SLEEP = 0.1


def log(*a):
    print(*a, flush=True)


def to_et(t):
    """LSEG timestamps -> US/Eastern (naive ones are UTC)."""
    t = pd.Timestamp(t)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert("America/New_York")


def bars(ric, start, end, interval, fields=FIELDS):
    """One request -> {"t": [...ISO ET...], "BID": [...], ...}, or None if no data.
    `end` is exclusive in practice for hourly, so callers pass the day after."""
    time.sleep(SLEEP)
    try:
        df = hp.summaries.Definition(
            universe=ric, start=start.isoformat(), end=end.isoformat(),
            interval=interval, fields=fields).get_data().data.df
    except Exception:
        return None
    if df is None or df.empty:
        return None
    out = {"t": [to_et(t).isoformat() for t in df.index]}
    for f in fields:
        col = pd.to_numeric(df[f], errors="coerce") if f in df else pd.Series(np.nan, index=df.index)
        out[f] = [None if pd.isna(v) else float(v) for v in col]
    return out


def third_friday(year, month):
    d = dt.date(year, month, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def grid(lo, hi, step):
    k = math.ceil(lo / step - 1e-9) * step
    out = []
    while k <= hi + 1e-9:
        out.append(round(k, 2))
        k += step
    return out


def weekly_strikes(s0, step, far_step, near, otm):
    """One strike in the money, `step` apart up to +near, `far_step` apart up to +otm."""
    lo = math.floor(s0 / step) * step - step
    return sorted(set(grid(lo, s0 * (1 + near), step) + grid(s0 * (1 + near), s0 * (1 + otm), far_step)))


def save(raw, path):
    with open(path, "w") as f:
        json.dump(raw, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stock")
    ap.add_argument("--step", type=float, required=True, help="weekly strike step near the money")
    ap.add_argument("--far-step", type=float, required=True, help="weekly strike step further out")
    ap.add_argument("--near", type=float, default=0.05, help="fine-step region, fraction above spot")
    ap.add_argument("--otm", type=float, required=True, help="highest weekly strike, fraction above spot")
    ap.add_argument("--leap-step", type=float, required=True)
    ap.add_argument("--leap-lo", type=float, default=0.35, help="lowest LEAP strike, fraction of start spot")
    ap.add_argument("--leap-years", type=int, nargs="+", default=[2027, 2028])
    ap.add_argument("--days", type=int, default=400, help="how far back to ask for hourly bars")
    args = ap.parse_args()

    root = args.stock.split(".")[0]
    os.makedirs("data", exist_ok=True)
    path = f"data/raw_{root}.json"
    raw = json.load(open(path)) if os.path.exists(path) else {}
    raw.update(stock=args.stock, root=root, params=vars(args), fetched=str(TODAY))
    ld.open_session()

    # stock -----------------------------------------------------------------
    if "stock_hourly" not in raw:
        log(f"stock hourly {args.stock} ...")
        raw["stock_hourly"] = bars(args.stock, TODAY - dt.timedelta(days=args.days), TODAY + DAY, HOURLY)
        if raw["stock_hourly"] is None:
            raise SystemExit("no hourly stock data")
        first = dt.date.fromisoformat(raw["stock_hourly"]["t"][0][:10])
        raw["stock_daily"] = bars(args.stock, first - dt.timedelta(days=180), TODAY + DAY, DAILY)
        save(raw, path)
    sh = raw["stock_hourly"]
    log(f"  hourly {sh['t'][0][:10]} to {sh['t'][-1][:10]}, {len(sh['t'])} bars")

    # weeks from the real tape: first session = entry, last session = expiry
    # (a Friday holiday makes Thursday the expiry automatically)
    sessions = {}
    for t, px in zip(sh["t"], sh["TRDPRC_1"]):
        ts = pd.Timestamp(t)
        if ts.weekday() >= 5 or not 9 <= ts.hour <= 15:   # regular hours only: drops weekend,
            continue                                      # pre/after-market and overnight bars
        sessions.setdefault(ts.date(), []).append(px)
    by_week = {}
    for d in sorted(sessions):
        by_week.setdefault(d.isocalendar()[:2], []).append(d)
    this_monday = TODAY - dt.timedelta(days=TODAY.weekday())
    weeks = [(v[0], v[-1]) for _, v in sorted(by_week.items()) if v[-1] < this_monday]

    # weekly calls ------------------------------------------------------------
    valid = {(str(e), str(x)) for e, x in weeks}            # drop weeks dated by the old logic
    raw["weeks"] = [w for w in raw.get("weeks", []) if (w["entry"], w["expiry"]) in valid]
    done = {w["expiry"] for w in raw["weeks"]}
    for entry, expiry in weeks:
        if str(expiry) in done:
            continue
        s0 = next((p for p in sessions[entry] if p is not None), None)   # first trade of the week
        if s0 is None:
            continue
        calls = {}
        for k in weekly_strikes(s0, args.step, args.far_step, args.near, args.otm):
            ric = build_ric(root, expiry, k, "C")
            b = bars(ric, entry, expiry + DAY, HOURLY)
            if b:
                calls[str(k)] = {"ric": ric, "bars": b}
        raw["weeks"].append({"entry": str(entry), "expiry": str(expiry), "spot0": s0, "calls": calls})
        raw["weeks"].sort(key=lambda w: w["expiry"])
        save(raw, path)
        log(f"  week {entry} -> {expiry}  spot {s0:8.2f}  {len(calls)} strikes with data")

    # LEAPs -------------------------------------------------------------------
    have = [w for w in raw["weeks"] if w["calls"]]
    if not have:
        raise SystemExit("no weekly option data at all")
    w0, w_last = have[0], have[-1]
    w_entry_end = have[min(2, len(have) - 1)]["expiry"]
    s_start = w0["spot0"]
    raw.setdefault("leaps", {})
    for y in args.leap_years:
        exp = third_friday(y, 1)
        if str(exp) in raw["leaps"]:
            continue
        found = {}
        for k in grid(args.leap_lo * s_start, 1.0 * s_start, args.leap_step):
            ric = build_ric(root, exp, k, "C")
            daily = bars(ric, dt.date.fromisoformat(w0["entry"]) - dt.timedelta(days=10), min(exp, TODAY) + DAY, DAILY)
            if daily is None:
                continue
            found[str(k)] = {
                "ric": ric,
                "daily": daily,
                "hourly_entry": bars(ric, dt.date.fromisoformat(w0["entry"]),
                                     dt.date.fromisoformat(w_entry_end) + DAY, HOURLY),
                "hourly_exit": bars(ric, dt.date.fromisoformat(w_last["entry"]),
                                    dt.date.fromisoformat(w_last["expiry"]) + DAY, HOURLY),
            }
        raw["leaps"][str(exp)] = found
        save(raw, path)
        log(f"  LEAP {exp}: {len(found)} strikes with data")

    # rates and dividends ------------------------------------------------------
    start = dt.date.fromisoformat(raw["stock_daily"]["t"][0][:10])
    if "rates" not in raw:
        try:
            r = ld.get_history("US3MT=RR", start=str(start), end=str(TODAY), interval="daily")
            num = r.apply(pd.to_numeric, errors="coerce")
            raw["rates"] = {"t": [str(pd.Timestamp(t).date()) for t in r.index],
                            **{c: [None if pd.isna(v) else float(v) for v in num[c]] for c in num.columns}}
        except Exception as e:
            raw["rates"] = None
            log("  rates: failed --", e)
    if "dividends" not in raw:
        try:
            d = ld.get_data(universe=[args.stock], fields=["TR.DivExDate", "TR.DivUnadjustedGross"],
                            parameters={"SDate": str(start), "EDate": str(TODAY)})
            raw["dividends"] = [[str(pd.Timestamp(x).date()), float(a)]
                                for x, a in zip(d.iloc[:, 1], d.iloc[:, 2]) if pd.notna(x) and pd.notna(a)]
        except Exception as e:
            raw["dividends"] = None
            log("  dividends: failed --", e)
    save(raw, path)
    ld.close_session()

    # summary -----------------------------------------------------------------
    n = sum(len(w["calls"]) for w in raw["weeks"])
    log(f"\nSUMMARY {args.stock}")
    log(f"  weeks: {len(raw['weeks'])} total, {len(have)} with option data, {have[0]['entry']} to {have[-1]['expiry']}")
    log(f"  weekly call series with data: {n}")
    for exp, ks in raw["leaps"].items():
        log(f"  LEAP {exp}: strikes {', '.join(ks) if ks else 'none'}")
    rc = [c for c in (raw["rates"] or {}) if c != "t"]
    log(f"  rates: {len(raw['rates']['t']) if raw['rates'] else 0} days, columns {rc}")
    log(f"  dividends: {raw['dividends']}")
    log(f"  saved {path} ({os.path.getsize(path) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
