"""
fetch_puts.py -- weekly PUT quotes for the wheel, on the same weeks as raw_{ROOT}.json.

    python3 fetch_puts.py AAPL.O --step 2.5 --far-step 5 --otm 0.08
    python3 fetch_puts.py MP.N   --step 0.5 --far-step 1 --otm 0.15

Reads the weeks (entry, expiry, Monday price) from data/raw_{ROOT}.json, so no new
stock data is needed. Writes data/puts_{ROOT}.json. Resumable: rerun after a
crash and it skips weeks already fetched.
Strikes: one strike above the Monday price, `step` apart down to -near,
`far_step` apart down to -otm. Requests run 8 at a time (like the course starter code).
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
import math
import os
import datetime as dt

import lseg.data as ld

from fetch import bars, grid, HOURLY, DAY, log, save
from rics import build_ric


def put_strikes(s0, step, far_step, near, otm):
    hi = math.ceil(s0 / step) * step + step
    return sorted(set(grid(s0 * (1 - near), hi, step) + grid(s0 * (1 - otm), s0 * (1 - near), far_step)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stock")
    ap.add_argument("--step", type=float, required=True)
    ap.add_argument("--far-step", type=float, required=True)
    ap.add_argument("--near", type=float, default=0.05)
    ap.add_argument("--otm", type=float, required=True)
    args = ap.parse_args()

    root = args.stock.split(".")[0]
    raw = json.load(open(f"data/raw_{root}.json"))
    path = f"data/puts_{root}.json"
    out = json.load(open(path)) if os.path.exists(path) else {"stock": args.stock, "weeks": []}
    out["params"] = vars(args)
    done = {w["expiry"] for w in out["weeks"]}
    ld.open_session()

    for w in raw["weeks"]:
        if w["expiry"] in done:
            continue
        entry, expiry = dt.date.fromisoformat(w["entry"]), dt.date.fromisoformat(w["expiry"])
        ks = put_strikes(w["spot0"], args.step, args.far_step, args.near, args.otm)
        rics = [build_ric(root, expiry, k, "P") for k in ks]
        with ThreadPoolExecutor(max_workers=8) as pool:
            got = list(pool.map(lambda r: bars(r, entry, expiry + DAY, HOURLY), rics))
        puts = {str(k): {"ric": r, "bars": b} for k, r, b in zip(ks, rics, got) if b}
        out["weeks"].append({"entry": w["entry"], "expiry": w["expiry"], "spot0": w["spot0"], "puts": puts})
        out["weeks"].sort(key=lambda x: x["expiry"])
        save(out, path)
        log(f"  week {w['entry']} -> {w['expiry']}  spot {w['spot0']:8.2f}  {len(puts)} puts with data")

    ld.close_session()
    n = sum(len(w["puts"]) for w in out["weeks"])
    empty = [w["expiry"] for w in out["weeks"] if not w["puts"]]
    log(f"\nSUMMARY {args.stock}: {len(out['weeks'])} weeks, {n} put series, weeks with no puts: {empty or 'none'}")
    log(f"  saved {path} ({os.path.getsize(path) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
