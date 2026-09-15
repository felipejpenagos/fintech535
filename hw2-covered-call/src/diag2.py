"""
Find AAPL option RICs that actually resolve.

The assignment's worked example (AAPLF52619000.U^F26) returns "universe is not
found", so we stop trusting the handout and go look at what LSEG really has.

    python3 diag2.py
"""

import datetime as dt

import pandas as pd
import lseg.data as ld
from lseg.data.content import historical_pricing

from rics import build_ric

UND = "AAPL.O"
INTR = historical_pricing.Intervals


def try_ric(ric, interval=None, start=None, end=None):
    """Returns (rows, note). rows=0 means it resolved but was empty."""
    try:
        if interval is None:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"])
        else:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"],
                                interval=interval, start=start, end=end)
        if df is None or df.empty:
            return 0, "empty"
        return len(df), "ok"
    except Exception as e:
        return -1, str(e).split("(")[-1].rstrip(")")[:60]


print("opening session...")
ld.open_session()

# ---------------------------------------------------------------------------
print("\n1. where has AAPL actually been trading?")
hist = ld.get_history(universe=UND, fields=["TRDPRC_1", "HIGH_1", "LOW_1"],
                      start=(dt.date.today() - dt.timedelta(weeks=12)).isoformat(),
                      end=dt.date.today().isoformat(), interval="daily")
print(f"   last close {float(hist['TRDPRC_1'].dropna().iloc[-1]):.2f}")
print(f"   12-week low {float(hist['LOW_1'].min()):.2f}  high {float(hist['HIGH_1'].max()):.2f}")
spot = float(hist["TRDPRC_1"].dropna().iloc[-1])

# ---------------------------------------------------------------------------
print("\n2. what do LIVE AAPL option RICs look like? (search works on live only)")
try:
    res = ld.discovery.search(
        view=ld.discovery.Views.SEARCH_ALL,
        query="AAPL options",
        filter="SearchAllCategory eq 'Options' and UnderlyingQuoteRIC eq 'AAPL.O'",
        select="RIC,DocumentTitle,ExpiryDate,StrikePrice,CallPutOption,ExchangeCode",
        top=25,
    )
    if res is not None and not res.empty:
        pd.set_option("display.width", 200)
        print(res.head(25).to_string(index=False))
    else:
        print("   search returned nothing")
except Exception as e:
    print(f"   search failed: {type(e).__name__}: {str(e)[:140]}")

# ---------------------------------------------------------------------------
print("\n3. probe expired Fridays around the money, 2.5 and 5 steps")
today = dt.date.today()
fridays = []
d = today - dt.timedelta(days=1)
while len(fridays) < 6:
    if d.weekday() == 4:
        fridays.append(d)
    d -= dt.timedelta(days=1)

for step in (2.5, 5.0):
    print(f"\n   --- step {step} ---")
    for fri in fridays[:3]:
        base = round(spot / step) * step
        hits = []
        for i in range(-3, 4):
            k = round(base + i * step, 2)
            ric = build_ric("AAPL", fri, k, "C", as_of=today)
            n, note = try_ric(ric)
            if n > 0:
                hits.append(f"{k:g}({n})")
        print(f"   {fri}  " + (", ".join(hits) if hits else "nothing resolved"))

# ---------------------------------------------------------------------------
print("\n4. does the caret matter? same contract with and without")
fri = fridays[1]
base = round(spot / 5.0) * 5.0
for k in (base, base + 5):
    with_caret = build_ric("AAPL", fri, k, "C", as_of=today)
    without = with_caret.split("^")[0]
    n1, e1 = try_ric(with_caret)
    n2, e2 = try_ric(without)
    print(f"   {k:g}C {fri}")
    print(f"      {with_caret:<28} -> {n1 if n1>=0 else 'ERR '+e1}")
    print(f"      {without:<28} -> {n2 if n2>=0 else 'ERR '+e2}")

# ---------------------------------------------------------------------------
print("\n5. if something resolved, does it have hourly BID/ASK?")
found = None
for fri in fridays[:3]:
    for step in (2.5, 5.0):
        base = round(spot / step) * step
        for i in range(-3, 4):
            k = round(base + i * step, 2)
            ric = build_ric("AAPL", fri, k, "C", as_of=today)
            n, _ = try_ric(ric)
            if n > 0:
                found = (ric, fri, k)
                break
        if found:
            break
    if found:
        break

if not found:
    print("   nothing resolved above -- skipping")
else:
    ric, fri, k = found
    print(f"   using {ric}  ({k:g}C expiring {fri})")
    s = (fri - dt.timedelta(days=7)).isoformat()
    e = (fri + dt.timedelta(days=1)).isoformat()
    for label, iv in [("daily", INTR.DAILY), ("hourly", INTR.HOURLY),
                      ("30min", INTR.THIRTY_MINUTES), ("minute", INTR.MINUTE)]:
        try:
            df = historical_pricing.summaries.Definition(
                universe=ric, start=s, end=e, interval=iv,
                fields=["BID", "ASK", "TRDPRC_1"]).get_data().data.df
            if df is None or df.empty:
                print(f"      {label:<8} empty")
                continue
            bid_n = int(df["BID"].notna().sum()) if "BID" in df.columns else 0
            ask_n = int(df["ASK"].notna().sum()) if "ASK" in df.columns else 0
            trd_n = int(df["TRDPRC_1"].notna().sum()) if "TRDPRC_1" in df.columns else 0
            both = int((df["BID"].notna() & df["ASK"].notna()).sum()) if bid_n and ask_n else 0
            print(f"      {label:<8} {len(df):>4} rows | BID {bid_n} ASK {ask_n} "
                  f"two-sided {both} | TRDPRC_1 {trd_n}")
        except Exception as ex:
            print(f"      {label:<8} ERROR {str(ex).split('(')[-1][:50]}")

ld.close_session()
print("\ndone")
