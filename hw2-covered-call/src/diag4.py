"""
Why did most MPWR weeks come back empty?

Hypothesis: MPWR lists monthly expiries only (third Friday), not weeklies. The
two weeks that resolved both contain a third Friday.

Also checks the strike increment, since a $1,300 stock does not use $10 strikes
everywhere.

    python3 diag4.py
"""

import datetime as dt

import lseg.data as ld
from rics import build_ric

ROOT = "MPWR"
UND = "MPWR.O"


def probe(r):
    try:
        df = ld.get_history(universe=r, fields=["BID", "ASK", "TRDPRC_1"])
        return 0 if (df is None or df.empty) else len(df)
    except Exception:
        return -1


def third_friday(y, m):
    d = dt.date(y, m, 1)
    fridays = [d + dt.timedelta(days=i) for i in range(31)
               if (d + dt.timedelta(days=i)).month == m
               and (d + dt.timedelta(days=i)).weekday() == 4]
    return fridays[2]


ld.open_session()

hist = ld.get_history(universe=UND, fields=["TRDPRC_1", "HIGH_1", "LOW_1"],
                      start=(dt.date.today() - dt.timedelta(weeks=12)).isoformat(),
                      end=dt.date.today().isoformat(), interval="daily")
spot = float(hist["TRDPRC_1"].dropna().iloc[-1])
lo, hi = float(hist["LOW_1"].min()), float(hist["HIGH_1"].max())
print(f"MPWR {spot:.2f}   12-week range {lo:.2f} - {hi:.2f}\n")

today = dt.date.today()

# --- 1. every Friday, at a strike we know exists -------------------------
print("1. every Friday in the window (strike rounded to nearest 50)")
k = round(spot / 50) * 50
fridays = []
d = today - dt.timedelta(days=1)
while len(fridays) < 11:
    if d.weekday() == 4:
        fridays.append(d)
    d -= dt.timedelta(days=1)
fridays.reverse()

thirds = {third_friday(f.year, f.month) for f in fridays}
for f in fridays:
    r = build_ric(ROOT, f, k, "C", as_of=today)
    n = probe(r)
    tag = "  <- third Friday" if f in thirds else ""
    status = f"{n} rows" if n > 0 else ("empty" if n == 0 else "not found")
    print(f"   {f}  {r:<30} {status:<12}{tag}")

# --- 2. strike increment on a month that works ---------------------------
print("\n2. strike increment on the nearest monthly expiry")
target = max(t for t in thirds if t < today)
print(f"   probing {target}")
found = []
for step in (5, 10, 20, 25, 50):
    base = round(spot / step) * step
    hits = 0
    for i in range(-4, 5):
        kk = base + i * step
        if kk <= 0:
            continue
        if probe(build_ric(ROOT, target, kk, "C", as_of=today)) > 0:
            hits += 1
    print(f"   step {step:>3}: {hits}/9 resolved")
    found.append((step, hits))

# --- 3. what strikes actually exist near the money -----------------------
print("\n3. scanning every $5 from spot-80 to spot+80")
base = round(spot / 5) * 5
live = []
for i in range(-16, 17):
    kk = base + i * 5
    if probe(build_ric(ROOT, target, kk, "C", as_of=today)) > 0:
        live.append(kk)
print(f"   {live}")
if len(live) > 1:
    gaps = sorted({live[i+1] - live[i] for i in range(len(live)-1)})
    print(f"   observed increments: {gaps}")

# --- 4. compare against a name the assignment recommends -----------------
print("\n4. control: does NVDA list weeklies?")
nvda = ld.get_history(universe="NVDA.O", fields=["TRDPRC_1"],
                      start=(today - dt.timedelta(days=10)).isoformat(),
                      end=today.isoformat(), interval="daily")
nspot = float(nvda["TRDPRC_1"].dropna().iloc[-1])
nk = round(nspot / 5) * 5
print(f"   NVDA {nspot:.2f}, probing {nk}")
for f in fridays[-4:]:
    n = probe(build_ric("NVDA", f, nk, "C", as_of=today))
    print(f"   {f}  {'ok' if n > 0 else 'not found'}")

ld.close_session()
print("\ndone")
