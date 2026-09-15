"""
LSEG data pull. Run this locally with Workspace open and logged in.

Writes data/raw_{ROOT}.json. Everything downstream reads that file, so the
backtest and the site can be rebuilt without touching the API again -- which
matters because LSEG enforces a daily request budget.

    python3 fetch.py AAPL.O --weeks 10
    python3 fetch.py MPWR.O --weeks 10 --strike-step 10

Uses the Content layer (historical_pricing.summaries) rather than get_history
because it gives explicit interval control, which is what LSEG's own expired-
options examples do.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import warnings
from collections import defaultdict

import pandas as pd

# lseg.data calls .fillna/.ffill on object arrays internally; the deprecation
# warning is theirs, fires on every request, and drowns the progress output.
warnings.filterwarnings("ignore", category=FutureWarning, module="lseg")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")

import lseg.data as ld
from lseg.data.content import historical_pricing

from rics import build_ric, occ_label

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# The Content layer rejects interval strings -- it wants the Intervals enum.
# get_history accepts "hourly"; summaries.Definition does not.
INTERVALS = {
    "minute": historical_pricing.Intervals.MINUTE,
    "10min": historical_pricing.Intervals.TEN_MINUTES,
    "30min": historical_pricing.Intervals.THIRTY_MINUTES,
    "hourly": historical_pricing.Intervals.HOURLY,
    "daily": historical_pricing.Intervals.DAILY,
}

OPTION_FIELDS = ["BID", "ASK", "TRDPRC_1", "OPEN_PRC", "HIGH_1", "LOW_1", "ACVOL_UNS", "NUM_MOVES"]
STOCK_FIELDS = ["OPEN_PRC", "HIGH_1", "LOW_1", "TRDPRC_1", "BID", "ASK"]

# LSEG's own examples batch IPA at 90 and sleep between calls.
IPA_BATCH = 90
SLEEP_BETWEEN_CALLS = 0.4


# ---------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def fetch_bars(universe, fields, start, end, interval):
    """
    One Content-layer request. Returns a DataFrame or None.

    Fails soft: a synthetic RIC that never existed raises, and that is an
    expected outcome, not an error worth stopping for.
    """
    try:
        resp = historical_pricing.summaries.Definition(
            universe=universe,
            start=start,
            end=end,
            interval=INTERVALS.get(interval, interval),
            fields=fields,
        ).get_data()
        df = resp.data.df
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


def weekly_sessions(stock_df):
    """
    One (first session, last session) pair per ISO week, taken from real bars
    rather than the calendar. If Friday is a holiday the last session is
    Thursday, and the weekly option expires then too -- walking the calendar
    would invent an expiry that never listed.
    """
    idx = pd.to_datetime(stock_df.index)
    by_week = defaultdict(list)
    for ts in idx:
        iso = ts.isocalendar()
        by_week[(iso[0], iso[1])].append(ts)

    weeks = []
    for key in sorted(by_week):
        stamps = sorted(by_week[key])
        days = sorted({s.date() for s in stamps})
        if len(days) < 2:
            continue  # need an entry day and a distinct expiry day
        first_day, last_day = days[0], days[-1]
        entry_bars = [s for s in stamps if s.date() == first_day]
        exit_bars = [s for s in stamps if s.date() == last_day]
        weeks.append({
            "iso_week": f"{key[0]}-W{key[1]:02d}",
            "entry_ts": entry_bars[0],      # first bar of the first session
            "exit_ts": exit_bars[-1],       # last bar of the last session
            "expiry": last_day,
        })
    return weeks


def third_friday(year, month):
    d = dt.date(year, month, 1)
    fridays = [d + dt.timedelta(days=i) for i in range(31)
               if (d + dt.timedelta(days=i)).month == month
               and (d + dt.timedelta(days=i)).weekday() == 4]
    return fridays[2]


def monthly_sessions(stock_df):
    """
    One period per monthly expiry, for names that list no weeklies.

    A period runs from the first session after the previous monthly expiry to
    the next monthly expiry, so coverage is continuous exactly as in the weekly
    version -- only the cadence changes. Expiry is the third Friday, snapped
    back to the last session actually present in the tape if that Friday was a
    holiday.
    """
    idx = sorted(pd.to_datetime(stock_df.index))
    if not idx:
        return []
    days = sorted({t.date() for t in idx})
    day_set = set(days)

    # third Fridays inside the window, snapped to a real session
    expiries = []
    seen = set()
    for d in days:
        tf = third_friday(d.year, d.month)
        if tf in seen:
            continue
        seen.add(tf)
        if tf > days[-1]:
            continue   # expiry is beyond the tape -- not expired yet, skip it
        actual = tf
        steps = 0
        while actual not in day_set and steps < 5:
            actual -= dt.timedelta(days=1)   # holiday -> previous session
            steps += 1
        if actual in day_set:
            expiries.append(actual)
    expiries = sorted(set(expiries))

    periods = []
    prev_exp = None
    for exp in expiries:
        candidates = [d for d in days if d < exp and (prev_exp is None or d > prev_exp)]
        if not candidates:
            prev_exp = exp
            continue
        entry_day = candidates[0]
        entry_bars = [t for t in idx if t.date() == entry_day]
        exit_bars = [t for t in idx if t.date() == exp]
        if not entry_bars or not exit_bars:
            prev_exp = exp
            continue
        periods.append({
            "iso_week": exp.strftime("%Y-%b"),
            "entry_ts": entry_bars[0],
            "exit_ts": exit_bars[-1],
            "expiry": exp,
        })
        prev_exp = exp
    return periods


def strike_grid(spot, step, n_below=2, n_above=6):
    """Candidate strikes bracketing spot, snapped to the step."""
    base = round(spot / step) * step
    out = []
    for i in range(-n_below, n_above + 1):
        k = round(base + i * step, 2)
        if k > 0:
            out.append(k)
    return sorted(set(out))


def col(df, ric, field):
    """Pull one field for one RIC out of whatever shape LSEG returned."""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        for key in ((ric, field), (field, ric)):
            if key in df.columns:
                return df[key]
        return None
    if field in df.columns:
        return df[field]
    return None


def first_valid_at_or_after(series, ts):
    """Value at the first bar at or after ts. None if nothing usable."""
    if series is None:
        return None
    s = series.dropna()
    if s.empty:
        return None
    s.index = pd.to_datetime(s.index)
    after = s[s.index >= pd.to_datetime(ts)]
    if after.empty:
        return None
    try:
        return float(after.iloc[0])
    except (TypeError, ValueError):
        return None


def fetch_deltas(rics, valuation_date):
    """
    Call deltas from IPA, batched. Returns {ric: delta} for whatever resolves.

    Exercise style is AMER: these are US listed equity options. LSEG's sample
    helper hardcodes EURO, which is wrong for this instrument class.
    """
    from lseg.data.content.ipa.financial_contracts import option

    out = {}
    for i in range(0, len(rics), IPA_BATCH):
        chunk = rics[i:i + IPA_BATCH]
        universe = [
            option.Definition(
                underlying_type=option.UnderlyingType.ETI,
                instrument_code=r,
                pricing_parameters=option.PricingParameters(
                    valuation_date=f"{valuation_date}T00:00:00Z",
                    volatility_type="Implied",
                ),
            )
            for r in chunk
        ]
        try:
            from lseg.data.content.ipa import financial_contracts
            resp = financial_contracts.Definitions(
                universe=universe,
                fields=["InstrumentCode", "DeltaPercent", "ErrorMessage"],
            ).get_data()
            df = resp.data.df
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = row.get("InstrumentCode")
                    d = row.get("DeltaPercent")
                    if code and pd.notna(d):
                        out[str(code)] = float(d)
        except Exception as e:
            log(f"    IPA batch failed ({e.__class__.__name__}) -- falling back for these")
        time.sleep(SLEEP_BETWEEN_CALLS)
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("underlying", help="e.g. AAPL.O")
    ap.add_argument("--weeks", type=int, default=10)
    ap.add_argument("--interval", default="hourly", choices=list(INTERVALS))
    # Strike increments are per-name and per-price-band. AAPL at $333 uses $5;
    # MPWR at $1,150 uses $20. Guessing wrong silently generates strikes that
    # were never listed, and the resolve rate collapses.
    ap.add_argument("--strike-step", type=float, default=5.0)
    ap.add_argument("--cadence", choices=["weekly", "monthly"], default="weekly",
                    help="monthly for names that list no weekly expiries")
    ap.add_argument("--want-deltas", action="store_true",
                    help="also pull IPA deltas (needed for the delta strike rule)")
    args = ap.parse_args()

    root = args.underlying.split(".")[0].upper()
    os.makedirs(DATA_DIR, exist_ok=True)

    end = dt.date.today()
    start = end - dt.timedelta(weeks=args.weeks)
    log(f"opening session; pulling {root} {start} -> {end} at {args.interval}")
    ld.open_session()

    # -- stock ------------------------------------------------------------
    stock_df = fetch_bars(args.underlying, STOCK_FIELDS,
                          start.isoformat(), end.isoformat(), args.interval)
    if stock_df is None:
        log("FATAL: no stock data returned. Check the RIC and that Workspace is logged in.")
        ld.close_session()
        sys.exit(1)

    stock_df.index = pd.to_datetime(stock_df.index)
    log(f"  stock: {len(stock_df)} bars, {stock_df.index[0]} -> {stock_df.index[-1]}")

    px_col = col(stock_df, args.underlying, "TRDPRC_1")
    if px_col is None:
        log("FATAL: no TRDPRC_1 on the stock.")
        ld.close_session()
        sys.exit(1)

    weeks_raw = (monthly_sessions(stock_df) if args.cadence == "monthly"
                 else weekly_sessions(stock_df))
    log(f"  {len(weeks_raw)} {args.cadence} periods with at least two sessions")

    # -- options ----------------------------------------------------------
    weeks_out = []
    scatter = []
    attempted = resolved = 0

    for w in weeks_raw:
        entry_ts, expiry = w["entry_ts"], w["expiry"]
        if expiry >= end:
            continue  # not expired yet; no caret form, and the week is unfinished

        spot = first_valid_at_or_after(px_col, entry_ts)
        exit_close = None
        exit_series = px_col.dropna()
        exit_series.index = pd.to_datetime(exit_series.index)
        same_day = exit_series[exit_series.index.date == expiry]
        if not same_day.empty:
            exit_close = float(same_day.iloc[-1])

        if spot is None or exit_close is None:
            log(f"  {w['iso_week']}: no usable stock print, skipping")
            continue

        candidates = []
        for k in strike_grid(spot, args.strike_step):
            ric = build_ric(root, expiry, k, "C", as_of=end)
            attempted += 1
            df = fetch_bars(ric, OPTION_FIELDS,
                            entry_ts.isoformat(), (expiry + dt.timedelta(days=1)).isoformat(),
                            args.interval)
            time.sleep(SLEEP_BETWEEN_CALLS)
            if df is None:
                continue
            resolved += 1

            bid_s = col(df, ric, "BID")
            ask_s = col(df, ric, "ASK")
            trd_s = col(df, ric, "TRDPRC_1")

            # every bar where both a quote and a print exist feeds the scatter
            if bid_s is not None and ask_s is not None and trd_s is not None:
                pairs = pd.DataFrame({"bid": bid_s, "ask": ask_s, "trd": trd_s}).dropna()
                for ts, r in pairs.iterrows():
                    b, a, t = float(r["bid"]), float(r["ask"]), float(r["trd"])
                    if b > 0 and a > 0 and a >= b:
                        scatter.append({
                            "ric": ric, "t": str(ts),
                            "mid": (b + a) / 2.0, "trade": t,
                            "strike": k, "moneyness": k / spot,
                        })

            # Keep the whole quote series, not just the entry bar: the ledger
            # has to mark the short call on every bar it is open, and we do
            # not know which strike the rule will pick until the backtest runs.
            bars = []
            if bid_s is not None and ask_s is not None:
                q = pd.DataFrame({"bid": bid_s, "ask": ask_s})
                if trd_s is not None:
                    q["trd"] = trd_s
                q = q.dropna(subset=["bid", "ask"])
                for ts, r in q.iterrows():
                    b, a = float(r["bid"]), float(r["ask"])
                    if b <= 0 or a <= 0 or a < b:
                        continue
                    bars.append({
                        "t": str(ts),
                        "bid": b,
                        "ask": a,
                        "trd": (float(r["trd"]) if "trd" in q.columns and pd.notna(r.get("trd")) else None),
                    })

            candidates.append({
                "strike": k,
                "ric": ric,
                "occ": occ_label(root, expiry, k, "C"),
                "bid": first_valid_at_or_after(bid_s, entry_ts),
                "ask": first_valid_at_or_after(ask_s, entry_ts),
                "delta": None,
                "bars": bars,
            })

        if not candidates:
            log(f"  {w['iso_week']}: no option RICs resolved")
            continue

        if args.want_deltas:
            dmap = fetch_deltas([c["ric"] for c in candidates], expiry.isoformat())
            for c in candidates:
                if c["ric"] in dmap:
                    c["delta"] = dmap[c["ric"]]

        weeks_out.append({
            "iso_week": w["iso_week"],
            "entry_ts": str(entry_ts),
            "exit_ts": str(w["exit_ts"]),
            "expiry": expiry.isoformat(),
            "entry_spot": spot,
            "exit_close": exit_close,
            "candidates": candidates,
        })
        quoted = sum(1 for c in candidates if c["bid"] is not None and c["ask"] is not None)
        log(f"  {w['iso_week']}: spot {spot:.2f} -> {exit_close:.2f}, "
            f"{len(candidates)} strikes, {quoted} quoted at entry")

    ld.close_session()

    # -- stock bars for the NAV path --------------------------------------
    stock_bars = []
    clean = px_col.dropna()
    clean.index = pd.to_datetime(clean.index)
    for ts, v in clean.items():
        stock_bars.append({"t": str(ts), "close": float(v)})

    payload = {
        "underlying": args.underlying,
        "root": root,
        "interval": args.interval,
        "cadence": args.cadence,
        "strike_step": args.strike_step,
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "stock_bars": stock_bars,
        "weeks": weeks_out,
        "scatter": scatter,
        "diagnostics": {
            "rics_attempted": attempted,
            "rics_resolved": resolved,
            "resolve_rate": (resolved / attempted) if attempted else 0.0,
            "scatter_points": len(scatter),
        },
    }

    path = os.path.join(DATA_DIR, f"raw_{root}.json")
    with open(path, "w") as f:
        json.dump(payload, f)

    log(f"\nwrote {path}")
    log(f"  RICs: {resolved}/{attempted} resolved ({100*resolved/max(attempted,1):.1f}%)")
    log(f"  weeks usable: {len(weeks_out)}")
    log(f"  scatter points: {len(scatter)}")
    if resolved == 0:
        log("\n  Nothing resolved. The strike step is probably wrong -- it varies "
            "by name and price band.")
    elif len(weeks_out) < 5 and args.cadence == "weekly":
        log(f"\n  Only {len(weeks_out)} of {len(weeks_raw)} periods resolved. If the "
            "ones that worked all contain a third Friday, this name lists monthly "
            "expiries only -- rerun with --cadence monthly and a longer --weeks.")


if __name__ == "__main__":
    main()
