"""
backtest_filter.py -- does the IV > RV filter help? Replays the HW4 wheel on the cached
AAPL and MP data (Nov 2025 - Oct 2026), with and without the filter.

    python3 hw5-final-cycle/src/backtest_filter.py

Uses the HW4 engine unchanged (hw4-wheel/src/wheel.py: same fills, fees, assignment,
T-bill interest). The only change is a gate in front of strike selection:

    ATM IV  = average Black-Scholes IV of the put and call nearest the money, from the
              same Monday quote the engine trades on
    RV      = 20-day realized vol from the closes before that Monday
    gate    = sell only if ATM IV > RV, otherwise sit out the week

Writes hw5-final-cycle/backtest_filter.json (metrics and weekly equity only, no quotes).
"""
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HW4 = HERE.parents[1] / "hw4-wheel" / "src"
sys.path.insert(0, str(HW4))
os.chdir(HW4)                       # the HW4 loaders read data/ relative to their folder
import backtest as bt  # noqa: E402
import bs              # noqa: E402
import wheel as wh     # noqa: E402

ORIGINAL = wh.select
STATE = dict(mode="off", log=[])


def rv20(D, day, n=20):
    px = [float(v) for d, v in D["close"].items() if d < day][-(n + 1):]
    r = [math.log(b / a) for a, b in zip(px, px[1:])]
    if len(r) < 5:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1) * 252)


def atm_iv(D, w, t, S):
    r, q = bt.rate(D, t.date()), bt.div_yield(D, t.date(), S)
    T = bt.years_to(t, w["expiry"])
    vals = []
    for chain, inv in ((w["puts"], bs.implied_vol_put), (w["calls"], bs.implied_vol)):
        best = None
        for K, (_, df) in chain.items():
            qt = bt.quote(df, t)
            if qt is None or qt.BID <= 0:
                continue
            if best is None or abs(K - S) < abs(best[0] - S):
                best = (K, float(qt.MID))
        if best:
            v = inv(best[1], S, best[0], T, r, q)
            if v:
                vals.append(v)
    return sum(vals) / len(vals) if vals else None


def gated_select(D, w, kind, t, S, target, ok=lambda K: True):
    mode = STATE["mode"]
    if mode == "all" or (mode == "puts" and kind == "P"):
        iv, rv = atm_iv(D, w, t, S), rv20(D, t.date())
        sell = iv is None or rv is None or iv > rv
        STATE["log"].append(dict(week=str(w["entry"]), kind=kind, iv=iv and round(iv, 4), rv=rv and round(rv, 4), sell=sell))
        if not sell:
            return None
    return ORIGINAL(D, w, kind, t, S, target, ok)


wh.select = gated_select


def one(root):
    D = wh.load(root)
    out = {}
    for mode, label in (("off", "wheel (HW4)"), ("puts", "filter on puts"), ("all", "filter on puts and calls")):
        STATE.update(mode=mode, log=[])
        b = wh.run(D, {})
        m = wh.metrics(D, b)
        gate = STATE["log"]
        out[mode] = dict(label=label, metrics={k: m[k] for k in ("capital", "pnl", "ret", "vol", "max_dd", "sharpe",
                                                                  "puts_sold", "calls_sold", "put_assigned", "weeks_holding")},
                         gated=len(gate), skipped=sum(1 for g in gate if not g["sell"]),
                         equity=[[l["d"], l["equity"]] for l in b.ledger][::5])
    hw4 = json.load(open(f"data/wheel_{root}.json"))
    bh = hw4["bench"]["bh"]["metrics"]
    out["bh"] = dict(label="buy and hold", metrics={k: bh.get(k) for k in ("capital", "pnl", "ret", "vol", "max_dd", "sharpe")})
    # the gate's own record: how often options were rich at the Monday decision
    STATE.update(mode="all", log=[])
    wh.run(D, {})
    g = [x for x in STATE["log"] if x["iv"] and x["rv"]]
    out["gate"] = dict(weeks=len(g), rich=sum(1 for x in g if x["iv"] > x["rv"]),
                       iv_mean=round(sum(x["iv"] for x in g) / len(g), 4), rv_mean=round(sum(x["rv"] for x in g) / len(g), 4),
                       series=[[x["week"], x["iv"], x["rv"]] for x in g])
    return out


def main():
    res = {root: one(root) for root in ("AAPL", "MP")}
    path = HERE.parent / "backtest_filter.json"
    path.write_text(json.dumps(res, indent=1))
    for root, r in res.items():
        g = r["gate"]
        print(f"\n{root}: options rich (ATM IV > RV20) on {g['rich']} of {g['weeks']} Mondays "
              f"(mean IV {g['iv_mean']:.0%}, mean RV {g['rv_mean']:.0%})")
        print(f"  {'run':28s} {'return':>7s} {'vol':>6s} {'maxDD':>6s} {'Sharpe':>6s} {'puts':>5s} {'calls':>5s} {'skipped':>7s}")
        for k in ("off", "puts", "all", "bh"):
            m = r[k]["metrics"]
            print(f"  {r[k]['label']:28s} {m['ret']:7.1%} {m['vol']:6.1%} {m['max_dd']:6.1%} {str(m['sharpe']):>6s} "
                  f"{str(m.get('puts_sold', '')):>5s} {str(m.get('calls_sold', '')):>5s} {str(r[k].get('skipped', '')):>7s}")
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
