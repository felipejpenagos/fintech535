"""
wheel.py -- the wheel on cached LSEG data, plus buy-and-hold and covered-call benchmarks.

    python3 wheel.py AAPL
    python3 wheel.py MP

Reads data/raw_{ROOT}.json (stock, calls, rates, dividends) and data/puts_{ROOT}.json,
writes data/wheel_{ROOT}.json. No LSEG calls.

The wheel (Schwab's four steps, run weekly):
  1. no shares    -> sell a cash-secured put: out of the money, |delta| closest to the target
  2. put assigned -> buy 100 shares at the strike with the reserved cash
  3. own shares   -> sell a covered call: out of the money, delta closest to the target
  4. call assigned-> shares sold at the strike, back to 1
Assignment is the point of the strategy, so it is allowed on both sides. A "roll"
variant instead buys back an in-the-money put on Friday and sells a new one Monday.

Clock and fills are the same as backtest.py: decide on a bar, fill on a later bar at
mid +/- h x half-spread; the official close settles expiries.
"""
import json
import math
import sys
import datetime as dt

import pandas as pd

import backtest as bt
import bs

BASE = dict(put_delta=0.30, call_delta=0.30, h=0.0, fee=0.65, basis=False, roll=False, interest=True)
# basis    : covered call strike must be >= the price the shares were assigned at
# roll     : buy back an in-the-money put on Friday instead of taking assignment
# interest : the cash securing a short put earns the 3-month T-bill rate (Schwab notes the
#            collateral's opportunity cost; brokers let it sit in T-bills / a sweep)


def load(root):
    D = bt.load(root)
    puts = json.load(open(f"data/puts_{root}.json"))
    by_exp = {w["expiry"]: w for w in puts["weeks"]}
    for w in D["weeks"]:
        pw = by_exp.get(str(w["expiry"]), {"puts": {}})
        w["puts"] = {float(k): (v["ric"], bt.hourly(v["bars"])) for k, v in pw["puts"].items()}
    return D


def select(D, w, kind, t, S, target, ok=lambda K: True):
    """kind 'P' or 'C': out-of-the-money contract whose |delta| is closest to target."""
    r, q = bt.rate(D, t.date()), bt.div_yield(D, t.date(), S)
    T = bt.years_to(t, w["expiry"])
    chain = w["puts"] if kind == "P" else w["calls"]
    cands = []
    for K, (ric, df) in chain.items():
        qt = bt.quote(df, t)
        if qt is None or qt.BID <= 0 or not ok(K):
            continue
        if (kind == "P" and K >= S) or (kind == "C" and K <= S):
            continue
        if kind == "P":
            v = bs.implied_vol_put(qt.MID, S, K, T, r, q)
            d = None if v is None else bs.put_delta(S, K, T, r, q, v)
        else:
            v = bs.implied_vol(qt.MID, S, K, T, r, q)
            d = None if v is None else bs.delta(S, K, T, r, q, v)
        if d is not None:
            cands.append(dict(K=K, ric=ric, mid=float(qt.MID), delta=d, iv=v, S=S))
    return min(cands, key=lambda c: abs(abs(c["delta"]) - target)) if cands else None


class Wheel:
    def __init__(self, p, stock):
        self.p, self.stock = p, stock
        self.cash, self.shares, self.basis = 0.0, 0, None
        self.short = None                  # dict(kind, K, ric, exp, px, df)
        self.blotter, self.ledger, self.log = [], [], []
        self.fees = self.divs = self.interest = 0.0
        self.accrued = 0.0

    def book(self, t, action, side, inst, qty, px, mult, fee=0.0, flow=None):
        sign = {"BUY": -1, "SELL": 1}.get(side, 0)
        f = sign * qty * px * mult - fee if flow is None else flow
        self.cash += f
        self.fees += fee
        self.blotter.append(dict(t=str(t), action=action, side=side, inst=inst, qty=qty, px=round(px, 4),
                                 fee=fee, cash_flow=round(f, 2), cash=round(self.cash, 2)))

    def collateral(self):
        return 100 * self.short["K"] if self.short and self.short["kind"] == "P" else 0.0

    def mark(self, d, S, opt_px):
        sv = 100 * opt_px if self.short else 0.0
        nav = self.cash + self.shares * S - sv
        self.ledger.append(dict(
            d=str(d), cash=round(self.cash, 2), shares=self.shares, S=round(S, 4),
            basis=self.basis,
            short_put=f"{self.short['K']:g}P {self.short['exp']}" if self.short and self.short["kind"] == "P" else None,
            covered_call=f"{self.short['K']:g}C {self.short['exp']}" if self.short and self.short["kind"] == "C" else None,
            opt_mark=round(opt_px, 4) if self.short else None,
            collateral=round(self.collateral(), 2), nav=round(nav, 2),
            deposit=round(self.collateral() - self.cash, 2)))   # cash the owner must have put in


def run(D, p):
    p = dict(BASE, **p)
    b = Wheel(p, D["stock"])
    h, fee = p["h"], p["fee"]
    prev_day = None
    for w in D["weeks"]:
        e, x = w["entry"], w["expiry"]
        row = dict(entry=str(e), expiry=str(x), phase="put" if b.shares == 0 else "call")
        for d in w["days"]:
            # interest on the cash securing an open put, for the calendar days since the last session
            if p["interest"] and b.short and b.short["kind"] == "P" and prev_day:
                b.accrued += b.collateral() * bt.rate(D, d) * (d - prev_day).days / 365
            prev_day = d
            for xd, amt in D["divs"]:
                if xd == d and b.shares:
                    b.divs += b.shares * amt
                    b.book(d, "dividend", "DIV", D["stock"], b.shares, amt, 1, flow=b.shares * amt)

            if d == e and b.short is None:
                clock = 9
                t = bt.stamp(d, clock)
                S = bt.stock_px(D, t)
                if S is not None:
                    if b.shares == 0:
                        sel = select(D, w, "P", t, S, p["put_delta"])
                        kind = "P"
                    else:
                        ok = (lambda K: K >= b.basis) if p["basis"] else (lambda K: True)
                        sel = select(D, w, "C", t, S, p["call_delta"], ok)
                        kind = "C"
                    if sel:
                        chain = w["puts"] if kind == "P" else w["calls"]
                        df = chain[sel["K"]][1]
                        fh, fq = bt.next_quote(df, d, clock, 15)
                        if fq is not None:
                            px = bt.fill_px(fq, "SELL", h)
                            b.book(bt.stamp(d, fh), "open put" if kind == "P" else "open call", "SELL",
                                   sel["ric"], 1, px, 100, fee)
                            b.short = dict(kind=kind, K=sel["K"], ric=sel["ric"], exp=x, px=px, df=df)
                            row.update(kind=kind, K=sel["K"], delta=round(sel["delta"], 3), iv=round(sel["iv"], 4),
                                       premium=round(px, 4), S_sale=round(S, 4))
                    if b.short is None:
                        row["note"] = "no strike passed the rules" if not sel else "no fill quote"

            if d == x and b.short:
                ch = bt.close_hour(x)
                K = b.short["K"]
                # roll variant: buy back an in-the-money put before the close
                if p["roll"] and b.short["kind"] == "P":
                    S_f = bt.stock_px(D, bt.stamp(x, ch - 2))
                    fq = bt.quote(b.short["df"], bt.stamp(x, ch - 1))
                    if S_f is not None and S_f < K and fq is not None:
                        px = bt.fill_px(fq, "BUY", h)
                        b.book(bt.stamp(x, ch - 1), "roll: close put", "BUY", b.short["ric"], 1, px, 100, fee)
                        row.update(outcome="rolled", close_px=round(px, 4))
                        b.short = None
                if b.short:
                    S_c = float(D["close"][x])
                    row["S_close"] = round(S_c, 4)
                    if b.short["kind"] == "P" and S_c < K:
                        b.book(x, "put assigned", "ASSIGN", b.short["ric"], 1, K, 0)
                        b.shares += 100
                        b.basis = K
                        b.book(x, "buy shares at strike", "BUY", D["stock"], 100, K, 1)
                        row["outcome"] = "assigned: bought 100"
                    elif b.short["kind"] == "C" and S_c > K:
                        b.book(x, "call assigned", "ASSIGN", b.short["ric"], 1, K, 0)
                        b.shares -= 100
                        b.book(x, "shares called away", "SELL", D["stock"], 100, K, 1)
                        b.basis = None
                        row["outcome"] = "called away: sold 100"
                    else:
                        b.book(x, "expired", "EXPIRE", b.short["ric"], 1, 0.0, 0)
                        row["outcome"] = "expired"
                    b.short = None
                if b.accrued:
                    b.interest += b.accrued
                    b.book(x, "T-bill interest on collateral", "INT", "cash", 1, round(b.accrued, 2), 1, flow=b.accrued)
                    b.accrued = 0.0

            S = float(D["close"][d])
            op = None
            if b.short:
                op = bt.last_mark(b.short["df"], d)
                if op is None:
                    op = max(b.short["K"] - S, 0) if b.short["kind"] == "P" else max(S - b.short["K"], 0)
            b.mark(d, S, op)
        b.log.append(row)

    # end of test: sell any shares at the last close
    d = D["weeks"][-1]["days"][-1]
    if b.shares:
        b.book(d, "end of test: sell shares", "SELL", D["stock"], b.shares, float(D["close"][d]), 1)
        b.shares = 0
        b.mark(d, float(D["close"][d]), None)
    cap = max(l["deposit"] for l in b.ledger)
    for l in b.ledger:
        l["equity"] = round(cap + l["nav"], 2)
    b.capital = cap
    return b


def metrics(D, b):
    nav = pd.Series([l["nav"] for l in b.ledger], index=[dt.date.fromisoformat(l["d"]) for l in b.ledger])
    nav = nav[~nav.index.duplicated(keep="last")]
    cap = b.capital
    eq = cap + nav
    fri = [w["expiry"] for w in D["weeks"] if w["expiry"] in eq.index]
    wk = eq.loc[fri].pct_change().dropna()
    rf = float(D["rates"].mean())
    out = [r.get("outcome") for r in b.log]
    att = dict(puts=0.0, calls=0.0, shares=0.0, dividends=round(b.divs, 2), interest=round(b.interest, 2), fees=-b.fees)
    for r in b.blotter:
        g = r["cash_flow"] + r["fee"]
        if r["side"] in ("DIV", "INT"):
            continue
        if ".U" in r["inst"]:
            body = r["inst"].split(".")[0][len(D["root"])]
            att["puts" if body.upper() >= "M" else "calls"] += g
        else:
            att["shares"] += g
    att = {k: round(v, 2) for k, v in att.items()}
    sold = [r for r in b.log if "K" in r]
    # thesis check: implied vol at sale vs the volatility the stock then realized that week
    vrp = []
    for r in sold:
        days = [d for d in D["sessions"] if dt.date.fromisoformat(r["entry"]) - dt.timedelta(days=7) < d <= dt.date.fromisoformat(r["expiry"])]
        px = [float(D["close"][d]) for d in days]
        rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if days[i] >= dt.date.fromisoformat(r["entry"])]
        if len(rets) >= 2:
            rv = math.sqrt(252 * sum(x * x for x in rets) / len(rets))
            vrp.append((r["iv"], rv))
    return dict(
        capital=round(cap, 2), pnl=round(float(nav.iloc[-1]), 2), ret=round(float(nav.iloc[-1]) / cap, 4),
        max_dd=round(float(((eq.cummax() - eq) / eq.cummax()).max()), 4),
        vol=round(float(wk.std() * math.sqrt(52)), 4),
        sharpe=round(float((wk.mean() - rf / 52) / wk.std() * math.sqrt(52)), 2) if wk.std() > 0 else None,
        weeks=len(b.log), puts_sold=sum(1 for r in sold if r["kind"] == "P"), calls_sold=sum(1 for r in sold if r["kind"] == "C"),
        put_assigned=out.count("assigned: bought 100"), called_away=out.count("called away: sold 100"),
        expired=out.count("expired"), rolled=out.count("rolled"),
        weeks_holding=sum(1 for r in b.log if r["phase"] == "call"),
        attribution=att,
        iv_mean=round(sum(a for a, _ in vrp) / len(vrp), 4) if vrp else None,
        rv_mean=round(sum(c for _, c in vrp) / len(vrp), 4) if vrp else None,
        iv_above_rv=round(sum(1 for a, c in vrp if a > c) / len(vrp), 3) if vrp else None,
        start=str(nav.index[0]), end=str(nav.index[-1]))


def bench_metrics(D, b):
    """Benchmarks from backtest.py, scored the same way (weekly vol, Sharpe)."""
    m = bt.metrics(D, b)
    nav = pd.Series([l["nav"] for l in b.ledger], index=[dt.date.fromisoformat(l["d"]) for l in b.ledger])
    nav = nav[~nav.index.duplicated(keep="last")]
    eq = m["capital"] + nav
    fri = [w["expiry"] for w in D["weeks"] if w["expiry"] in eq.index]
    m["vol"] = round(float(eq.loc[fri].pct_change().dropna().std() * math.sqrt(52)), 4)
    return m


def main(root):
    D = load(root)
    runs, seen = [], set()

    def go(**kw):
        p = dict(BASE, **kw)
        key = tuple(sorted(p.items()))
        if key in seen:
            return None
        seen.add(key)
        b = run(D, p)
        m = metrics(D, b)
        runs.append(dict(params=p, metrics=m, nav=[[l["d"], l["nav"]] for l in b.ledger]))
        return b

    base = go()
    for pd_ in (0.15, 0.30, 0.45):                 # every combination is reported
        for cd in (0.15, 0.30, 0.45):
            go(put_delta=pd_, call_delta=cd)
    for kw in (dict(h=0.5), dict(h=1.0), dict(basis=True), dict(interest=False)):
        go(**kw)
    rolled = go(roll=True)                         # kept in full: its blotter shows closes and rolls

    bh = bt.run(D, "bh", bt.BASE)
    cc = bt.run(D, "cc", bt.BASE)
    bench = {"bh": dict(metrics=bench_metrics(D, bh), ledger=bh.ledger), "cc": dict(metrics=bench_metrics(D, cc), ledger=cc.ledger)}
    rf = float(D["rates"].mean())
    days = (D["weeks"][-1]["expiry"] - D["weeks"][0]["entry"]).days
    tbill = dict(ret=round((1 + rf) ** (days / 365) - 1, 4), rate=round(rf, 4))

    out = dict(root=root, stock=D["stock"], base_params=BASE, window=[str(D["weeks"][0]["entry"]), str(D["weeks"][-1]["expiry"])],
               checks=bt.checks(D), put_checks=put_checks(D),
               base=dict(blotter=base.blotter, ledger=base.ledger, weeks=base.log, metrics=metrics(D, base)),
               roll=dict(blotter=rolled.blotter, ledger=rolled.ledger, weeks=rolled.log, metrics=metrics(D, rolled)),
               bench=bench, tbill=tbill, runs=runs)
    json.dump(out, open(f"data/wheel_{root}.json", "w"), default=str)

    m = out["base"]["metrics"]
    print(f"\n{D['stock']} wheel {out['window'][0]} -> {out['window'][1]}")
    print(f"  puts fill check: {out['put_checks']}")
    for name, mm in (("wheel", m), ("buy & hold", bench["bh"]["metrics"]), ("covered call", bench["cc"]["metrics"])):
        print(f"  {name:13s} capital {mm['capital']:10.0f}  P&L {mm['pnl']:9.0f}  return {mm['ret']:7.1%}  vol {mm['vol']:6.1%}  "
              f"maxDD {mm['max_dd']:6.1%}  Sharpe {mm['sharpe']}")
    print(f"  T-bills       {tbill['ret']:.1%}")
    print(f"  wheel: {m['puts_sold']} puts ({m['put_assigned']} assigned), {m['calls_sold']} calls ({m['called_away']} called away), "
          f"{m['expired']} expired, {m['weeks_holding']} weeks holding shares")
    print(f"  attribution {m['attribution']}")
    print(f"  thesis: mean IV at sale {m['iv_mean']}, mean realized {m['rv_mean']}, IV > RV in {m['iv_above_rv']} of weeks")
    print(f"  {'run':40s} {'capital':>9s} {'P&L':>8s} {'return':>7s} {'maxDD':>6s} {'Sharpe':>6s}")
    for r in runs:
        diff = {k: v for k, v in r["params"].items() if BASE[k] != v}
        mm = r["metrics"]
        print(f"  {(', '.join(f'{k}={v}' for k, v in diff.items()) or 'baseline'):40s} {mm['capital']:9.0f} {mm['pnl']:8.0f} "
              f"{mm['ret']:7.1%} {mm['max_dd']:6.1%} {str(mm['sharpe']):>6s}")
    print(f"  saved data/wheel_{root}.json")


def put_checks(D):
    import numpy as np
    m, t, spr = [], [], []
    for w in D["weeks"]:
        for K, (ric, df) in w["puts"].items():
            ok = df.dropna(subset=["MID", "TRDPRC_1"])
            m += ok.MID.tolist()
            t += ok.TRDPRC_1.tolist()
            q = df.dropna(subset=["MID"])
            spr += ((q.ASK - q.BID) / q.MID).tolist()
    m, t = np.array(m), np.array(t)
    slope, icpt = np.polyfit(m, t, 1)
    r2 = 1 - ((t - (slope * m + icpt)) ** 2).sum() / ((t - t.mean()) ** 2).sum()
    return dict(n=int(len(m)), slope=round(float(slope), 4), intercept=round(float(icpt), 4), r2=round(float(r2), 4),
                spread_med=round(float(np.median(spr)), 4))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
