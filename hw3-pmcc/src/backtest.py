"""
backtest.py -- PMCC vs covered call vs buy-and-hold on the cached LSEG data.

    python3 backtest.py AAPL
    python3 backtest.py MP

Reads data/raw_{ROOT}.json (from fetch.py), writes data/book_{ROOT}.json.
No LSEG calls: anyone can rerun this from the repo.

Bars: LSEG hourly bars are stamped at their START (the 09:00 bar is 09:00-10:00
and its BID/ASK are the last quotes before 10:00).
Clock (every decision uses only quotes at or before the decision bar, and every
fill uses a LATER bar's quotes -- no look-ahead):
  Monday  decide on the 09:00 bar (quotes at ~10:00), fill on the next bar with a quote
  Friday  decide on the 14:00 bar (quotes at ~15:00), fill on the 15:00 bar (~16:00 close)
          (13:00 closes: 11:00 and 12:00)
  Expiry  the official daily close settles anything still open
Fills: mid +/- h x half-spread (h = 0 is the mid; buys pay up, sells give up).
"""
import json
import math
import sys
import datetime as dt

import numpy as np
import pandas as pd

import bs

ET = "America/New_York"
EARLY_CLOSE = {dt.date(2025, 7, 3), dt.date(2025, 11, 28), dt.date(2025, 12, 24),
               dt.date(2026, 11, 27), dt.date(2026, 12, 24)}      # NYSE 1 pm closes
YEAR = 365 * 86400.0

BASE = dict(leap_delta=0.80, short_delta=0.30, leap_exp="2028-01-21", h=0.0,
            fee=0.65, floor="paid")
# floor: which weekly strikes the PMCC may sell
#   paid : K_s >= K_L + D   (D = what we paid for the LEAP; assignment can never lock a loss)
#   jump : K_s >= PV(K_L) + C_L(now) - p   (a rally through K_s cannot lose money: after a
#          big rally the LEAP is worth ~ S - K_L e^{-rT} and the short call ~ S - K_s)
#   none : no extra rule -- only the course's covering rule K_s >= K_L (the Friday buy-back
#          is the only other protection)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def hourly(b):
    if not b:
        return None
    df = pd.DataFrame({k: b[k] for k in ("BID", "ASK", "TRDPRC_1")},
                      index=pd.to_datetime(b["t"], utc=True).tz_convert(ET)).astype(float)
    df = df[~df.index.duplicated(keep="last")]
    ok = (df.BID >= 0) & (df.ASK > 0) & (df.ASK >= df.BID)   # a zero bid is still a market to buy from
    df["MID"] = np.where(ok, (df.BID + df.ASK) / 2, np.nan)
    return df


def daily(b):
    """LSEG daily bars are dated at midnight UTC; fetch.py stored them in ET, so
    convert back to UTC to recover the session date."""
    if not b:
        return None
    df = hourly(b)
    df.index = [t.tz_convert("UTC").date() for t in df.index]
    return df[~df.index.duplicated(keep="last")]


def load(root):
    raw = json.load(open(f"data/raw_{root}.json"))
    D = {"root": root, "stock": raw["stock"]}
    D["sh"] = hourly(raw["stock_hourly"])
    D["sd"] = daily(raw["stock_daily"])
    reg = D["sh"][(D["sh"].index.weekday < 5) & (D["sh"].index.hour >= 9) & (D["sh"].index.hour <= 15)]
    sessions = sorted(set(reg.index.date) & set(D["sd"].index))
    D["sessions"] = sessions
    D["close"] = D["sd"].TRDPRC_1.fillna(D["sd"].MID)
    D["weeks"] = []
    for w in raw["weeks"]:
        if not w["calls"]:
            continue
        e, x = dt.date.fromisoformat(w["entry"]), dt.date.fromisoformat(w["expiry"])
        D["weeks"].append(dict(entry=e, expiry=x, days=[d for d in sessions if e <= d <= x],
                               calls={float(k): (v["ric"], hourly(v["bars"])) for k, v in w["calls"].items()}))
    D["leaps"] = {}
    for exp, ks in raw["leaps"].items():
        D["leaps"][exp] = {}
        for k, v in ks.items():
            parts = [hourly(v.get("hourly_entry")), hourly(v.get("hourly_exit"))]
            parts = [p for p in parts if p is not None]
            D["leaps"][exp][float(k)] = dict(ric=v["ric"], daily=daily(v["daily"]),
                                             hourly=pd.concat(parts).sort_index() if parts else None)
    # 3-month T-bill (percent) -> decimal, last value strictly before the date
    rates = raw.get("rates") or {}
    col = next((c for c in ("B_YLD_1", "MID_YLD_1", "TRDPRC_1", "BID_YIELD", "ASK_YIELD")
                if c in rates and any(v is not None for v in rates[c])), None)
    D["rate_col"] = col
    D["rates"] = (pd.Series(rates[col], index=[dt.date.fromisoformat(t) for t in rates["t"]]).dropna() / 100
                  if col else pd.Series(dtype=float))
    D["divs"] = [(dt.date.fromisoformat(a), b) for a, b in (raw.get("dividends") or [])]
    return D


def rate(D, d):
    s = D["rates"][D["rates"].index < d]
    return float(s.iloc[-1]) if len(s) else 0.04


def div_yield(D, d, S):
    """Trailing 12-month dividends / price, known on date d."""
    return sum(a for x, a in D["divs"] if d - dt.timedelta(days=365) < x <= d) / S


def stamp(d, hour):
    return pd.Timestamp(dt.datetime(d.year, d.month, d.day, hour), tz=ET)


def close_hour(d):
    return 13 if d in EARLY_CLOSE else 16


def quote(df, t):
    if df is None or t not in df.index:
        return None
    r = df.loc[t]
    return None if pd.isna(r.MID) else r


def stock_px(D, t):
    r = D["sh"].loc[t] if t in D["sh"].index else None
    if r is None:
        return None
    return float(r.MID) if not pd.isna(r.MID) else (None if pd.isna(r.TRDPRC_1) else float(r.TRDPRC_1))


def next_quote(df, d, after_hour, last_hour):
    """First bar after `after_hour` (and up to `last_hour`) on day d with a valid quote."""
    for h in range(after_hour + 1, last_hour + 1):
        q = quote(df, stamp(d, h))
        if q is not None:
            return h, q
    return None, None


def years_to(t, expiry):
    end = stamp(expiry, close_hour(expiry))
    return max((end - t).total_seconds() / YEAR, 1 / (365 * 24))


def fill_px(q, side, h):
    half = (q.ASK - q.BID) / 2
    return float(q.MID + h * half) if side == "BUY" else float(q.MID - h * half)


def last_mark(df, d):
    """Latest valid mid on day d up to the bar that ends at the close."""
    if df is None:
        return None
    day = df[(df.index.date == d) & (df.index.hour <= close_hour(d) - 1)].MID.dropna()
    return float(day.iloc[-1]) if len(day) else None


def daily_mark(df, d):
    if df is None:
        return None
    s = df.MID[[i <= d for i in df.index]].dropna()
    return float(s.iloc[-1]) if len(s) else None


# ---------------------------------------------------------------------------
# selection by delta (read off market prices with Black-Scholes)
# ---------------------------------------------------------------------------
def pick(cands, target):
    return min(cands, key=lambda c: abs(c["delta"] - target)) if cands else None


def greeks(mid, S, K, T, r, q):
    v = bs.implied_vol(mid, S, K, T, r, q)
    return None if v is None else dict(iv=v, delta=bs.delta(S, K, T, r, q, v))


def select_leap(D, p, d, h):
    t = stamp(d, h)
    S = stock_px(D, t)
    exp = dt.date.fromisoformat(p["leap_exp"])
    r, q = rate(D, d), div_yield(D, d, S) if S else 0
    cands = []
    for K, L in D["leaps"].get(p["leap_exp"], {}).items():
        qt = quote(L["hourly"], t)
        if S is None or qt is None or qt.BID <= 0:
            continue
        g = greeks(qt.MID, S, K, years_to(t, exp), r, q)
        if g:
            cands.append(dict(K=K, ric=L["ric"], mid=float(qt.MID), S=S, **g))
    return pick(cands, p["leap_delta"])


def select_short(D, w, p, t, S, floor_fn):
    r, q = rate(D, t.date()), div_yield(D, t.date(), S)
    cands = []
    for K, (ric, df) in w["calls"].items():
        qt = quote(df, t)
        if qt is None or qt.BID <= 0 or K <= S or not floor_fn(K, float(qt.MID)):   # need a bid to sell into
            continue
        g = greeks(qt.MID, S, K, years_to(t, w["expiry"]), r, q)
        if g:
            cands.append(dict(K=K, ric=ric, mid=float(qt.MID), S=S, **g))
    return pick(cands, p["short_delta"])


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------
class Book:
    def __init__(self, kind, p):
        self.kind, self.p = kind, p
        self.cash, self.shares = 0.0, 0
        self.leap = self.short = self.leap_open = None
        self.blotter, self.ledger, self.log = [], [], []
        self.peak_out = 0.0
        self.premium = self.buyback = self.fees = self.divs = 0.0
        self.entry = {}

    def book(self, t, side, inst, qty, px, mult, fee=0.0):
        sign = {"BUY": -1, "SELL": 1}.get(side, 0)
        flow = sign * qty * px * mult - fee
        self.cash += flow
        self.fees += fee
        self.peak_out = max(self.peak_out, -self.cash)
        self.blotter.append(dict(t=str(t), side=side, inst=inst, qty=qty, px=round(px, 4),
                                 fee=fee, cash_flow=round(flow, 2), cash=round(self.cash, 2)))

    def stock(self, t, side, px):
        self.shares += 100 if side == "BUY" else -100
        self.book(t, side, self.p["stock"], 100, px, 1)

    def mark(self, d, S, leap_px, short_px):
        lmv, smv = max(self.shares, 0) * S, -min(self.shares, 0) * S
        lv = 100 * leap_px if self.leap else 0.0
        sv = 100 * short_px if self.short else 0.0
        nav = self.cash + lmv - smv + lv - sv
        # Reg T (course rules): long stock 50% / 25%; short stock 50% / 30%;
        # LEAP paid in full and a covered short call add nothing.
        init, maint = 0.5 * lmv + 0.5 * smv, 0.25 * lmv + 0.30 * smv
        self.ledger.append(dict(d=str(d), cash=round(self.cash, 2), shares=self.shares, S=round(S, 4),
                                leap_K=self.leap["K"] if self.leap else None,
                                leap_mark=round(leap_px, 4) if self.leap else None,
                                short_K=self.short["K"] if self.short else None,
                                short_mark=round(short_px, 4) if self.short else None,
                                nav=round(nav, 2), init=round(init, 2), maint=round(maint, 2),
                                avail=round(nav - init, 2), excess=round(nav - maint, 2)))


def run(D, kind, p):
    """kind: pmcc | cc | bh"""
    p = dict(p, stock=D["stock"])
    b = Book(kind, p)
    h, fee = p["h"], p["fee"]
    started = False
    leap_exp = dt.date.fromisoformat(p["leap_exp"])

    for w in D["weeks"]:
        e, x = w["entry"], w["expiry"]
        row = dict(entry=str(e), expiry=str(x))
        for d in w["days"]:
            # dividends: whoever holds (or is short) the shares at the prior close
            for xd, amt in D["divs"]:
                if xd == d and b.shares:
                    b.cash += b.shares * amt
                    b.divs += b.shares * amt
                    b.blotter.append(dict(t=str(d), side="DIV", inst=p["stock"], qty=b.shares, px=amt,
                                          fee=0, cash_flow=round(b.shares * amt, 2), cash=round(b.cash, 2)))

            if d == e:
                clock = 9                                    # Monday decision bar
                # 1. open the long leg once
                if not started:
                    if kind == "pmcc":
                        sel = None
                        for hh in (9, 10, 11):
                            sel = select_leap(D, p, d, hh)
                            if sel:
                                clock = hh
                                break
                        if sel:
                            fh, fq = next_quote(D["leaps"][p["leap_exp"]][sel["K"]]["hourly"], d, clock, 15)
                            if fq is not None:
                                px = fill_px(fq, "BUY", h)
                                b.book(stamp(d, fh), "BUY", sel["ric"], 1, px, 100, fee)
                                b.leap = dict(K=sel["K"], ric=sel["ric"], cost=px, exp=leap_exp,
                                              delta=sel["delta"], iv=sel["iv"], S=sel["S"])
                                b.entry = dict(date=str(d), K=sel["K"], ric=sel["ric"], cost=round(px, 4),
                                               mid=round(sel["mid"], 4), delta=round(sel["delta"], 4),
                                               iv=round(sel["iv"], 4), S=round(sel["S"], 4),
                                               expiry=str(leap_exp))
                                b.leap_open = dict(t=str(stamp(d, fh)), K=sel["K"], ric=sel["ric"], cost=round(px, 4),
                                                   exp=str(leap_exp), delta=round(sel["delta"], 4),
                                                   iv=round(sel["iv"], 4), S=round(sel["S"], 4),
                                                   share_equiv=round(100 * sel["delta"], 1))
                                started, clock = True, fh
                        if not started:
                            row["note"] = "no LEAP quote"
                    else:
                        fh, fq = next_quote(D["sh"], d, clock, 15)
                        if fq is not None:
                            b.stock(stamp(d, fh), "BUY", fill_px(fq, "BUY", h))
                            b.entry = dict(date=str(d), S=round(fill_px(fq, "BUY", h), 4))
                            started, clock = True, fh
                # 2. after an assignment: the PMCC is short 100 shares -> buy them back;
                #    the covered call delivered its shares -> buy them again
                if started and (b.shares < 0 or (kind == "cc" and b.shares == 0)):
                    fh, fq = next_quote(D["sh"], d, clock, 15)
                    if fq is not None:
                        b.stock(stamp(d, fh), "BUY", fill_px(fq, "BUY", h))
                        clock = fh
                # 3. write the weekly call
                sel = None
                if started and kind in ("pmcc", "cc") and b.short is None:
                    t = stamp(d, clock)
                    S = stock_px(D, t)
                    if S is not None:
                        if kind == "pmcc" and p["floor"] == "paid":
                            floor = lambda K, mid: K >= b.leap["K"] + b.leap["cost"]
                        elif kind == "pmcc" and p["floor"] == "jump":
                            # yesterday's LEAP close (known now) and PV of its strike
                            lm = daily_mark(D["leaps"][p["leap_exp"]][b.leap["K"]]["daily"], d - dt.timedelta(days=1))
                            lm = b.leap["cost"] if lm is None else lm
                            pv_kl = b.leap["K"] * math.exp(-rate(D, d) * years_to(t, leap_exp))
                            floor = lambda K, mid: K >= pv_kl + lm - mid
                        elif kind == "pmcc":
                            floor = lambda K, mid: K >= b.leap["K"]       # course rule: the LEAP must cover
                        else:
                            floor = lambda K, mid: True
                        sel = select_short(D, w, p, t, S, floor)
                        if sel:
                            fh, fq = next_quote(w["calls"][sel["K"]][1], d, clock, 15)
                            if fq is not None:
                                px = fill_px(fq, "SELL", h)
                                b.book(stamp(d, fh), "SELL", sel["ric"], 1, px, 100, fee)
                                b.premium += 100 * px
                                b.short = dict(K=sel["K"], ric=sel["ric"], exp=x, px=px,
                                               delta=sel["delta"], iv=sel["iv"])
                                row.update(K=sel["K"], delta=round(sel["delta"], 3), premium=round(px, 4))
                    if b.short is None:
                        row["note"] = "no strike passed the rules" if sel is None else "no fill quote"

            if d == x and b.short:
                ch = close_hour(x)
                df = w["calls"][b.short["K"]][1]
                # 4. Friday: buy back if in the money
                S_f = stock_px(D, stamp(x, ch - 2))
                row["S_fri"] = None if S_f is None else round(S_f, 4)
                if S_f is not None and S_f > b.short["K"]:
                    fq = quote(df, stamp(x, ch - 1))
                    if fq is not None:
                        px = fill_px(fq, "BUY", h)
                        b.book(stamp(x, ch - 1), "BUY", b.short["ric"], 1, px, 100, fee)
                        b.buyback += 100 * px
                        row.update(outcome="bought back", buyback=round(px, 4))
                        b.short = None
                # 5. expiry at the official close
                if b.short:
                    S_c = float(D["close"][x])
                    row["S_close"] = round(S_c, 4)
                    if S_c > b.short["K"]:
                        b.book(x, "ASSIGN", b.short["ric"], 1, b.short["K"], 0)
                        b.stock(x, "SELL", b.short["K"])
                        row["outcome"] = "assigned"
                    else:
                        b.book(x, "EXPIRE", b.short["ric"], 1, 0.0, 0)
                        row["outcome"] = "expired"
                    b.short = None

            # 6. end-of-day marks
            if started:
                S = float(D["close"][d])
                lp = None
                if b.leap:
                    lp = daily_mark(D["leaps"][p["leap_exp"]][b.leap["K"]]["daily"], d)
                    lp = b.leap["cost"] if lp is None else lp
                sp = None
                if b.short:
                    sp = last_mark(w["calls"][b.short["K"]][1], d)
                    sp = max(S - b.short["K"], 0.0) if sp is None else sp
                b.mark(d, S, lp, sp)
        b.log.append(row)

    # liquidate at the last session's close
    if started and D["weeks"]:
        d = D["weeks"][-1]["days"][-1]
        if b.leap:
            L = D["leaps"][p["leap_exp"]][b.leap["K"]]["daily"]
            r_ = L.loc[d] if d in L.index and not pd.isna(L.loc[d].MID) else None
            px = fill_px(r_, "SELL", h) if r_ is not None else daily_mark(L, d)
            b.book(d, "SELL", b.leap["ric"], 1, px, 100, fee)
            b.leap = None
        if b.shares > 0:
            b.stock(d, "SELL", float(D["close"][d]))
        elif b.shares < 0:
            b.stock(d, "BUY", float(D["close"][d]))
        b.mark(d, float(D["close"][d]), None, None)
    # Reg T is judged on account equity = cash deposited + P&L. The deposit is the
    # capital the strategy actually tied up (its peak cash outlay).
    for l in b.ledger:
        eq = b.peak_out + l["nav"]
        l.update(equity=round(eq, 2), avail=round(eq - l["init"], 2), excess=round(eq - l["maint"], 2))
    return b


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def metrics(D, b):
    if not b.ledger:
        return {}
    nav = pd.Series([l["nav"] for l in b.ledger], index=[dt.date.fromisoformat(l["d"]) for l in b.ledger])
    nav = nav[~nav.index.duplicated(keep="last")]
    cap = b.peak_out
    eq = cap + nav
    fri = [w["expiry"] for w in D["weeks"] if w["expiry"] in eq.index]
    wk = eq.loc[fri].pct_change().dropna()
    rf = float(D["rates"].mean()) if len(D["rates"]) else 0.04
    days = (nav.index[-1] - nav.index[0]).days or 1
    out = [r.get("outcome") for r in b.log]
    # P&L attribution from the blotter (cash flows before fees, then fees)
    lr = b.leap_open["ric"] if b.leap_open else None
    att = dict(long_leg=0.0, short_calls=0.0, stock=0.0, dividends=0.0, fees=-b.fees)
    for r in b.blotter:
        gross = r["cash_flow"] + r.get("fee", 0)
        if r["side"] == "DIV":
            att["dividends"] += gross
        elif r["inst"] == lr:
            att["long_leg"] += gross
        elif ".U" in r["inst"]:
            att["short_calls"] += gross
        elif b.kind == "pmcc":
            att["stock"] += gross                     # assignment + cover trades
        else:
            att["long_leg"] += gross                  # the 100 shares (incl. re-buys after assignment)
    att = {k: round(v, 2) for k, v in att.items()}
    return dict(attribution=att,
        capital=round(cap, 2), pnl=round(float(nav.iloc[-1]), 2),
        ret=round(float(nav.iloc[-1]) / cap, 4) if cap else None,
        ann=round((1 + float(nav.iloc[-1]) / cap) ** (365 / days) - 1, 4) if cap else None,
        max_dd=round(float(((eq.cummax() - eq) / eq.cummax()).max()), 4),
        sharpe=round(float((wk.mean() - rf / 52) / wk.std() * math.sqrt(52)), 2) if wk.std() > 0 else None,
        weeks=len(b.log), written=sum(1 for r in b.log if "K" in r),
        expired=out.count("expired"), bought_back=out.count("bought back"), assigned=out.count("assigned"),
        premium=round(b.premium, 2), buyback=round(b.buyback, 2), fees=round(b.fees, 2), divs=round(b.divs, 2),
        min_avail=round(min(l["avail"] for l in b.ledger), 2),
        min_cash=round(min(l["cash"] for l in b.ledger), 2),
        start=str(nav.index[0]), end=str(nav.index[-1]))


def checks(D):
    """Fill-model check (mid vs last trade, like HW2) and spread sizes."""
    mids, trades, spr = [], [], []
    for w in D["weeks"]:
        for K, (ric, df) in w["calls"].items():
            ok = df.dropna(subset=["MID", "TRDPRC_1"])
            mids += ok.MID.tolist()
            trades += ok.TRDPRC_1.tolist()
            q = df.dropna(subset=["MID"])
            spr += ((q.ASK - q.BID) / q.MID).tolist()
    m, t = np.array(mids), np.array(trades)
    if len(m) > 2:
        slope, icpt = np.polyfit(m, t, 1)
        r2 = 1 - ((t - (slope * m + icpt)) ** 2).sum() / ((t - t.mean()) ** 2).sum()
    else:
        slope = icpt = r2 = float("nan")
    leap_spr = []
    for exp, ks in D["leaps"].items():
        for K, L in ks.items():
            q = L["daily"].dropna(subset=["MID"]) if L["daily"] is not None else pd.DataFrame()
            if len(q):
                leap_spr += ((q.ASK - q.BID) / q.MID).tolist()
    return dict(n=int(len(m)), slope=round(float(slope), 4), intercept=round(float(icpt), 4), r2=round(float(r2), 4),
                weekly_spread_med=round(float(np.median(spr)), 4) if spr else None,
                leap_spread_med=round(float(np.median(leap_spr)), 4) if leap_spr else None,
                rate_col=D["rate_col"], n_weeks=len(D["weeks"]), n_divs=len(D["divs"]))


def detail(b):
    return dict(blotter=b.blotter, ledger=b.ledger, weeks=b.log, leap=b.leap_open)


def main(root):
    D = load(root)
    if not D["weeks"]:
        raise SystemExit("no weeks with option data")
    if BASE["leap_exp"] not in D["leaps"] or not D["leaps"][BASE["leap_exp"]]:
        raise SystemExit(f"no LEAP data for {BASE['leap_exp']}")

    runs = []

    seen = set()

    def go(kind, **kw):
        p = dict(BASE, **kw)
        key = (kind, tuple(sorted(p.items())))
        if key in seen:
            return None, None
        seen.add(key)
        b = run(D, kind, p)
        m = metrics(D, b)
        runs.append(dict(kind=kind, params={k: p[k] for k in BASE}, metrics=m,
                         nav=[[l["d"], l["nav"]] for l in b.ledger]))
        return b, m

    base = {k: go(k)[0] for k in ("pmcc", "cc", "bh")}
    base["pmcc_none"] = go("pmcc", floor="none")[0]      # same weekly-call rule as the covered call
    # every combination is reported, under both strike rules
    for fl in ("paid", "none"):
        for ld_ in (0.70, 0.80, 0.90):
            for sd in (0.15, 0.30, 0.45):
                go("pmcc", floor=fl, leap_delta=ld_, short_delta=sd)
        for kw in (dict(h=0.5), dict(h=1.0), dict(leap_exp="2027-01-15")):
            if kw.get("leap_exp", BASE["leap_exp"]) in D["leaps"]:
                go("pmcc", floor=fl, **kw)
    go("pmcc", floor="jump")
    for kw in (dict(short_delta=0.15), dict(short_delta=0.45), dict(h=0.5), dict(h=1.0)):
        go("cc", **kw)

    out = dict(root=root, stock=D["stock"], base_params=BASE, checks=checks(D),
               window=[str(D["weeks"][0]["entry"]), str(D["weeks"][-1]["expiry"])],
               base={k: dict(detail(b), metrics=metrics(D, b), entry=b.entry) for k, b in base.items()},
               runs=runs)
    json.dump(out, open(f"data/book_{root}.json", "w"), default=str)

    # console summary -------------------------------------------------------
    c = out["checks"]
    print(f"\n{D['stock']}  {out['window'][0]} -> {out['window'][1]}  ({c['n_weeks']} weeks)")
    print(f"  fill check: last trade = {c['slope']} x mid + {c['intercept']}, R^2 {c['r2']} (n={c['n']})")
    print(f"  median spread: weekly {c['weekly_spread_med']}, LEAP {c['leap_spread_med']};  rate column {c['rate_col']}")
    hdr = f"  {'run':42s} {'capital':>10s} {'P&L':>9s} {'return':>7s} {'maxDD':>6s} {'Sharpe':>6s} {'wrote':>5s} {'bb':>3s} {'as':>3s}"
    print(hdr)
    for r in runs:
        p, m = r["params"], r["metrics"]
        if not m:
            continue
        diff = {k: v for k, v in p.items() if BASE[k] != v}
        name = f"{r['kind']} " + (", ".join(f"{k}={v}" for k, v in diff.items()) or "baseline")
        print(f"  {name:42s} {m['capital']:10.0f} {m['pnl']:9.0f} {m['ret']:7.1%} {m['max_dd']:6.1%} "
              f"{str(m['sharpe']):>6s} {m['written']:5d} {m['bought_back']:3d} {m['assigned']:3d}")
    print(f"\n  saved data/book_{root}.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
