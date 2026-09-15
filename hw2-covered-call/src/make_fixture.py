"""
Generate a synthetic raw_{ROOT}.json shaped exactly like fetch.py's output.

Stock follows GBM; options are priced with Black-Scholes and given a spread
that widens as liquidity drops. This lets the full pipeline be exercised --
and the regression validated against a known answer -- without the API.

    python3 make_fixture.py TESTLIQ --liquidity high
    python3 make_fixture.py TESTILQ --liquidity low
"""

import argparse
import datetime as dt
import json
import math
import os
import random

from rics import build_ric, occ_label

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, r, sigma):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_delta(S, K, T, r, sigma):
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--spot", type=float, default=190.0)
    ap.add_argument("--vol", type=float, default=0.28)
    ap.add_argument("--drift", type=float, default=0.10)
    ap.add_argument("--weeks", type=int, default=10)
    ap.add_argument("--strike-step", type=float, default=2.5)
    ap.add_argument("--liquidity", choices=["high", "low"], default="high")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    root = args.root.upper()
    os.makedirs(DATA_DIR, exist_ok=True)

    # liquidity knobs: spread width, chance a bar has a print, noise around mid
    if args.liquidity == "high":
        half_spread_frac, print_prob, print_noise, quote_prob = 0.006, 0.80, 0.004, 0.99
    else:
        half_spread_frac, print_prob, print_noise, quote_prob = 0.060, 0.18, 0.055, 0.72

    r = 0.04
    BARS_PER_DAY = 7
    # The GBM advances one step per bar; a Mon-Fri week is 34 steps of
    # 1/(252*7) yr, while the calendar says 4/365 yr. Scale the pricing
    # clock to match the diffusion clock or realized moves outrun the model.
    CLOCK_RATIO = (34 / (252 * BARS_PER_DAY)) / (4 / 365.0)
    today = dt.date.today()
    # land the window so the last week is fully expired
    end = today - dt.timedelta(days=7)
    start = end - dt.timedelta(weeks=args.weeks)

    # -- stock path -------------------------------------------------------
    stock_bars = []
    S = args.spot
    dt_step = 1.0 / (252 * BARS_PER_DAY)
    day = start
    trading_days = []
    while day <= end:
        if day.weekday() < 5:
            trading_days.append(day)
        day += dt.timedelta(days=1)

    spot_by_ts = {}
    for d in trading_days:
        for b in range(BARS_PER_DAY):
            z = random.gauss(0, 1)
            S *= math.exp((args.drift - 0.5 * args.vol ** 2) * dt_step
                          + args.vol * math.sqrt(dt_step) * z)
            ts = f"{d.isoformat()} {9 + b:02d}:30:00"
            stock_bars.append({"t": ts, "close": round(S, 2)})
            spot_by_ts[ts] = S

    # -- weeks ------------------------------------------------------------
    by_week = {}
    for sb in stock_bars:
        d = dt.date.fromisoformat(sb["t"][:10])
        key = d.isocalendar()[:2]
        by_week.setdefault(key, []).append(sb)

    weeks_out, scatter = [], []
    attempted = resolved = 0

    for key in sorted(by_week):
        bars = by_week[key]
        days = sorted({b["t"][:10] for b in bars})
        if len(days) < 2:
            continue
        entry_bars = [b for b in bars if b["t"][:10] == days[0]]
        exit_bars = [b for b in bars if b["t"][:10] == days[-1]]
        entry_ts, exit_ts = entry_bars[0]["t"], exit_bars[-1]["t"]
        expiry = dt.date.fromisoformat(days[-1])
        if expiry >= today:
            continue
        spot = entry_bars[0]["close"]
        exit_close = exit_bars[-1]["close"]

        base = round(spot / args.strike_step) * args.strike_step
        strikes = [round(base + i * args.strike_step, 2) for i in range(-2, 7)]

        candidates = []
        for K in strikes:
            attempted += 1
            # a thin name simply does not list every strike
            if args.liquidity == "low" and random.random() < 0.25:
                continue
            resolved += 1
            ric = build_ric(root, expiry, K, "C", as_of=today)

            week_bars = [b for b in bars]
            obars = []
            for sb in week_bars:
                ts = sb["t"]
                S_t = sb["close"]
                d_t = dt.date.fromisoformat(ts[:10])
                T = max((expiry - d_t).days, 0) / 365.0 * CLOCK_RATIO
                theo = bs_call(S_t, K, T, r, args.vol)
                if theo < 0.01:
                    theo = 0.01
                hs = max(theo * half_spread_frac, 0.01)
                if random.random() > quote_prob:
                    continue
                bid = round(max(theo - hs, 0.01), 2)
                ask = round(theo + hs, 2)
                trd = None
                if random.random() < print_prob:
                    mid = (bid + ask) / 2.0
                    trd = round(max(mid * (1 + random.gauss(0, print_noise)), 0.01), 2)
                    scatter.append({
                        "ric": ric, "t": ts, "mid": mid, "trade": trd,
                        "strike": K, "moneyness": K / S_t,
                    })
                obars.append({"t": ts, "bid": bid, "ask": ask, "trd": trd})

            entry_quote = next((b for b in obars if b["t"] >= entry_ts), None)
            T0 = max((expiry - dt.date.fromisoformat(entry_ts[:10])).days, 0) / 365.0 * CLOCK_RATIO
            candidates.append({
                "strike": K,
                "ric": ric,
                "occ": occ_label(root, expiry, K, "C"),
                "bid": entry_quote["bid"] if entry_quote else None,
                "ask": entry_quote["ask"] if entry_quote else None,
                "delta": round(bs_delta(spot, K, T0, r, args.vol), 4),
                "bars": obars,
            })

        if not candidates:
            continue

        weeks_out.append({
            "iso_week": f"{key[0]}-W{key[1]:02d}",
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "expiry": expiry.isoformat(),
            "entry_spot": spot,
            "exit_close": exit_close,
            "candidates": candidates,
        })

    payload = {
        "underlying": f"{root}.O",
        "root": root,
        "interval": "hourly",
        "strike_step": args.strike_step,
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "stock_bars": stock_bars,
        "weeks": weeks_out,
        "scatter": scatter,
        "diagnostics": {
            "rics_attempted": attempted,
            "rics_resolved": resolved,
            "resolve_rate": resolved / max(attempted, 1),
            "scatter_points": len(scatter),
            "SYNTHETIC": True,
        },
    }

    path = os.path.join(DATA_DIR, f"raw_{root}.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    print(f"wrote {path}: {len(weeks_out)} weeks, {len(stock_bars)} stock bars, "
          f"{len(scatter)} scatter points, liquidity={args.liquidity}")


if __name__ == "__main__":
    main()
