"""
Run the covered-call backtest over cached LSEG data and write the book.

    python3 run_backtest.py AAPL --rule nearest_otm
    python3 run_backtest.py MPWR --rule delta --target-delta 0.30

Reads  data/raw_{ROOT}.json   (written by fetch.py)
Writes data/book_{ROOT}.json  (consumed by build_site.py)

No API calls here, so this is cheap to iterate on.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import datetime as dt

from engine import (
    CoveredCallBacktest, reg_t_snapshot, mid_price,
    pick_nearest_otm, pick_by_delta, CONTRACT_MULTIPLIER, SHARES_PER_CONTRACT,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------------------------------------------------------------------
def linreg(xs, ys):
    """
    OLS of y on x plus R^2.

        y = alpha + beta*x + eps
        R^2 = 1 - SS_res/SS_tot

    For the fill-assumption test, x = mid and y = the actual trade print.
    beta near 1 with alpha near 0 and high R^2 says trades happen at the mid,
    which is what makes modelling fills at the mid defensible.
    """
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None
    beta = sxy / sxx
    alpha = my - beta * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else None
    resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
    rmse = math.sqrt(sum(r * r for r in resid) / n)
    return {"n": n, "alpha": alpha, "beta": beta, "r2": r2, "rmse": rmse,
            "mean_abs_resid": sum(abs(r) for r in resid) / n}


def max_drawdown(values):
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst


def mark_at(bars, ts_iso):
    """Mid of the last quote bar at or before ts_iso."""
    best = None
    for b in bars:
        if b["t"] <= ts_iso:
            best = b
        else:
            break
    if best is None:
        return None
    return mid_price(best["bid"], best["ask"])


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--rule", choices=["nearest_otm", "delta"], default="nearest_otm")
    ap.add_argument("--target-delta", type=float, default=0.30)
    ap.add_argument("--cash", type=float, default=None,
                    help="starting cash; defaults to 1.6x the first stock print")
    args = ap.parse_args()

    root = args.root.upper()
    raw_path = os.path.join(DATA_DIR, f"raw_{root}.json")
    if not os.path.exists(raw_path):
        print(f"missing {raw_path} -- run fetch.py first")
        sys.exit(1)

    with open(raw_path) as f:
        raw = json.load(f)

    weeks = raw["weeks"]
    if not weeks:
        print("no usable weeks in the cache")
        sys.exit(1)

    # Enough cash to buy 100 shares outright with room to spare, so the
    # account is unlevered and available funds stay positive.
    first_spot = weeks[0]["entry_spot"]
    starting_cash = args.cash or round(first_spot * SHARES_PER_CONTRACT * 1.6, -3)

    bt = CoveredCallBacktest(
        underlying=raw["underlying"],
        starting_cash=starting_cash,
        strike_rule=args.rule,
        target_delta=args.target_delta,
    )

    stock_bars = raw["stock_bars"]
    open_call = None          # {"bars": [...], "strike":..., "ric":..., "occ":...}
    week_results = []

    for w in weeks:
        entry_ts, exit_ts = w["entry_ts"], w["exit_ts"]
        spot, exit_close = w["entry_spot"], w["exit_close"]

        # --- Monday: buy if flat -----------------------------------------
        if bt.pos.state == "FLAT":
            bt.buy_stock(entry_ts, spot)

        # --- Monday: pick a strike and write -----------------------------
        quotable = [c for c in w["candidates"]
                    if mid_price(c["bid"], c["ask"]) is not None]

        chosen = None
        if quotable:
            if args.rule == "delta":
                deltas = {c["strike"]: c["delta"] for c in quotable if c["delta"] is not None}
                k = pick_by_delta(deltas, args.target_delta) if deltas else None
                if k is None:  # no deltas came back; fall back rather than skip
                    k = pick_nearest_otm(spot, [c["strike"] for c in quotable])
            else:
                k = pick_nearest_otm(spot, [c["strike"] for c in quotable])
            chosen = next((c for c in quotable if c["strike"] == k), None)

        if chosen is None:
            bt.skipped_weeks.append({
                "time": entry_ts, "ric": None,
                "reason": "no strike with a two-sided quote at entry",
            })
            week_results.append({"iso_week": w["iso_week"], "outcome": "SKIPPED",
                                 "spot": spot, "exit_close": exit_close})
            open_call = None
        else:
            filled = bt.write_call(
                time=entry_ts, ric=chosen["ric"], occ=chosen["occ"],
                strike=chosen["strike"], expiry=w["expiry"],
                bid=chosen["bid"], ask=chosen["ask"],
            )
            if filled:
                open_call = {
                    "bars": sorted(chosen.get("bars", []), key=lambda b: b["t"]),
                    "strike": chosen["strike"], "ric": chosen["ric"],
                    "occ": chosen["occ"],
                }
            else:
                open_call = None

        # --- mark the ledger daily while the position is open -------------
        day_seen = set()
        for sb in stock_bars:
            if not (entry_ts <= sb["t"] <= exit_ts):
                continue
            day = sb["t"][:10]
            if day in day_seen:
                continue
            day_seen.add(day)
            om = mark_at(open_call["bars"], sb["t"]) if open_call else None
            bt._mark(sb["t"], sb["close"], om)

        # --- Friday: settle ----------------------------------------------
        if open_call is not None and bt.pos.state == "COVERED":
            k = open_call["strike"]
            assigned = exit_close > k
            bt.settle_expiry(exit_ts, open_call["ric"], open_call["occ"], k, exit_close)
            premium = next(
                (r.cash_delta for r in reversed(bt.blotter)
                 if r.side == "SELL" and r.instrument == open_call["ric"]), 0.0
            )
            week_results.append({
                "iso_week": w["iso_week"],
                "outcome": "ASSIGNED" if assigned else "EXPIRED",
                "strike": k, "spot": spot, "exit_close": exit_close,
                "premium": premium,
                "moneyness": k / spot if spot else None,
                "delta": next((c["delta"] for c in w["candidates"]
                               if c["strike"] == k), None),
            })
            open_call = None

        bt._mark(exit_ts, exit_close, None)

    # -- final mark ---------------------------------------------------------
    last_spot = stock_bars[-1]["close"] if stock_bars else weeks[-1]["exit_close"]
    final = reg_t_snapshot(bt.pos, last_spot, None)

    # -- regression ---------------------------------------------------------
    scatter = raw.get("scatter", [])
    reg = linreg([p["mid"] for p in scatter], [p["trade"] for p in scatter])

    # near-the-money subset: the assignment asks specifically for NTM calls
    ntm = [p for p in scatter if 0.95 <= p.get("moneyness", 0) <= 1.05]
    reg_ntm = linreg([p["mid"] for p in ntm], [p["trade"] for p in ntm])

    # -- performance --------------------------------------------------------
    navs = [r.nav for r in bt.ledger]
    premiums = [r.cash_delta for r in bt.blotter if r.side == "SELL" and r.qty == 1]
    settled = [r for r in week_results if r["outcome"] in ("ASSIGNED", "EXPIRED")]
    n_assigned = sum(1 for r in settled if r["outcome"] == "ASSIGNED")

    first_spot_px = weeks[0]["entry_spot"]
    bh_nav = starting_cash + SHARES_PER_CONTRACT * (last_spot - first_spot_px)

    perf = {
        "starting_cash": starting_cash,
        "final_nav": final["nav"],
        "total_return_pct": 100.0 * (final["nav"] - starting_cash) / starting_cash,
        "buy_hold_nav": bh_nav,
        "buy_hold_return_pct": 100.0 * (bh_nav - starting_cash) / starting_cash,
        "premium_collected": sum(premiums),
        "n_weeks": len(weeks),
        "n_traded": len(settled),
        "n_skipped": len(bt.skipped_weeks),
        "n_assigned": n_assigned,
        "assignment_rate": (n_assigned / len(settled)) if settled else None,
        "max_drawdown_pct": 100.0 * max_drawdown(navs),
        "margin_breaches": len(bt.margin_breaches),
    }
    perf["excess_vs_buy_hold_pct"] = perf["total_return_pct"] - perf["buy_hold_return_pct"]

    book = bt.to_dict()
    book.update({
        "root": root,
        "interval": raw.get("interval"),
        "cadence": raw.get("cadence", "weekly"),
        "window": raw.get("window"),
        "fetched_at": raw.get("fetched_at"),
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "final": final,
        "final_spot": last_spot,
        "weeks": week_results,
        "regression_all": reg,
        "regression_ntm": reg_ntm,
        "scatter": scatter,
        "performance": perf,
        "diagnostics": raw.get("diagnostics", {}),
    })

    out = os.path.join(DATA_DIR, f"book_{root}.json")
    with open(out, "w") as f:
        json.dump(book, f)

    # -- console summary ----------------------------------------------------
    print(f"\n{root}  rule={args.rule}" +
          (f" target={args.target_delta}" if args.rule == "delta" else ""))
    print(f"  weeks {perf['n_traded']} traded / {perf['n_skipped']} skipped")
    print(f"  assigned {n_assigned}" +
          (f" ({100*perf['assignment_rate']:.0f}%)" if settled else ""))
    print(f"  premium collected  ${perf['premium_collected']:,.2f}")
    print(f"  NAV {starting_cash:,.0f} -> {final['nav']:,.2f} "
          f"({perf['total_return_pct']:+.2f}%)")
    print(f"  buy & hold        {perf['buy_hold_return_pct']:+.2f}%  "
          f"(covered call {perf['excess_vs_buy_hold_pct']:+.2f} pts)")
    print(f"  max drawdown      {perf['max_drawdown_pct']:.2f}%")
    print(f"  margin breaches   {perf['margin_breaches']}")
    if reg:
        print(f"  mid->trade (all)  n={reg['n']} beta={reg['beta']:.4f} "
              f"alpha={reg['alpha']:+.4f} R2={reg['r2']:.4f}")
    if reg_ntm:
        print(f"  mid->trade (NTM)  n={reg_ntm['n']} beta={reg_ntm['beta']:.4f} "
              f"alpha={reg_ntm['alpha']:+.4f} R2={reg_ntm['r2']:.4f}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
