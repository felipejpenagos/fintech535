"""
Find out what LSEG actually accepts. Prints the real exception for each variant
instead of failing soft.

    python3 diag.py
"""

import datetime as dt
import traceback

import lseg.data as ld
from lseg.data.content import historical_pricing

UNDERLYING = "AAPL.O"
end = dt.date.today()
start = end - dt.timedelta(days=21)


def show(label, fn):
    try:
        df = fn()
        if df is None or (hasattr(df, "empty") and df.empty):
            print(f"  EMPTY   {label}")
            return None
        n = len(df)
        print(f"  OK      {label}  -> {n} rows")
        print(f"            cols: {list(df.columns)[:6]}")
        print(f"            span: {df.index[0]}  ..  {df.index[-1]}")
        return df
    except Exception as e:
        msg = str(e).replace("\n", " ")[:160]
        print(f"  ERROR   {label}\n            {type(e).__name__}: {msg}")
        return None


print("opening session...")
ld.open_session()
cfg = ld.get_config()
try:
    print("session:", cfg.get("sessions.default"))
except Exception:
    pass

print("\n1. does the stock resolve at all (daily, get_history)?")
show("get_history daily", lambda: ld.get_history(
    universe=UNDERLYING, fields=["TRDPRC_1"],
    start=start.isoformat(), end=end.isoformat(), interval="daily"))

print("\n2. stock intraday via get_history")
show('get_history interval="hourly"', lambda: ld.get_history(
    universe=UNDERLYING, fields=["TRDPRC_1", "BID", "ASK"],
    start=start.isoformat(), end=end.isoformat(), interval="hourly"))
show('get_history interval="1h"', lambda: ld.get_history(
    universe=UNDERLYING, fields=["TRDPRC_1", "BID", "ASK"],
    start=start.isoformat(), end=end.isoformat(), interval="1h"))

print("\n3. stock intraday via the Content layer (what fetch.py uses)")
show('Content interval="hourly" (string), date-only bounds',
     lambda: historical_pricing.summaries.Definition(
         universe=UNDERLYING, start=start.isoformat(), end=end.isoformat(),
         interval="hourly", fields=["TRDPRC_1", "BID", "ASK"]).get_data().data.df)

show("Content Intervals.HOURLY enum, date-only bounds",
     lambda: historical_pricing.summaries.Definition(
         universe=UNDERLYING, start=start.isoformat(), end=end.isoformat(),
         interval=historical_pricing.Intervals.HOURLY,
         fields=["TRDPRC_1", "BID", "ASK"]).get_data().data.df)

show("Content Intervals.HOURLY enum, full ISO timestamps",
     lambda: historical_pricing.summaries.Definition(
         universe=UNDERLYING,
         start=f"{start.isoformat()}T00:00:00Z", end=f"{end.isoformat()}T23:59:59Z",
         interval=historical_pricing.Intervals.HOURLY,
         fields=["TRDPRC_1", "BID", "ASK"]).get_data().data.df)

show("Content Intervals.HOURLY enum, no fields (let it return everything)",
     lambda: historical_pricing.summaries.Definition(
         universe=UNDERLYING, start=start.isoformat(), end=end.isoformat(),
         interval=historical_pricing.Intervals.HOURLY).get_data().data.df)

print("\n4. what intervals does this build expose?")
try:
    print("  ", [a for a in dir(historical_pricing.Intervals) if not a.startswith("_")])
except Exception as e:
    print("   could not list:", e)

print("\n5. an expired option, daily (proves the RIC scheme works)")
show("AAPL 5Jun26 190C daily", lambda: ld.get_history(
    universe="AAPLF52619000.U^F26", fields=["BID", "ASK", "TRDPRC_1"],
    interval="daily"))

print("\n6. the same expired option, hourly -- the key question")
show("AAPL 5Jun26 190C hourly via get_history", lambda: ld.get_history(
    universe="AAPLF52619000.U^F26", fields=["BID", "ASK", "TRDPRC_1"],
    interval="hourly", start="2026-06-01", end="2026-06-06"))
show("AAPL 5Jun26 190C hourly via Content",
     lambda: historical_pricing.summaries.Definition(
         universe="AAPLF52619000.U^F26", start="2026-06-01", end="2026-06-06",
         interval=historical_pricing.Intervals.HOURLY,
         fields=["BID", "ASK", "TRDPRC_1"]).get_data().data.df)

print("\n7. how far back does intraday reach?")
for days in (7, 30, 60, 90, 180):
    s = (end - dt.timedelta(days=days)).isoformat()
    show(f"stock hourly, {days}d back", lambda s=s: ld.get_history(
        universe=UNDERLYING, fields=["TRDPRC_1"],
        start=s, end=end.isoformat(), interval="hourly"))

ld.close_session()
print("\ndone")
