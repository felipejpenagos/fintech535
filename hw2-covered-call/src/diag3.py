"""
Settle the day-padding question.

The handout says the expiry day is NOT zero-padded ("5 not 05"). The probe in
diag2 resolved every Friday except 2026-09-04 -- the only one with a
single-digit day. Test both forms across several such Fridays.

    python3 diag3.py
"""

import datetime as dt

import lseg.data as ld

ROOT = "AAPL"
CALL = "ABCDEFGHIJKL"


def ric(root, expiry, strike, pad):
    day = f"{expiry.day:02d}" if pad else str(expiry.day)
    letter = CALL[expiry.month - 1]
    strike_s = f"{int(round(strike * 100)):05d}"
    return (f"{root}{letter}{day}{expiry.year % 100:02d}{strike_s}.U"
            f"^{letter}{expiry.year % 100:02d}")


def probe(r):
    try:
        df = ld.get_history(universe=r, fields=["BID", "ASK", "TRDPRC_1"])
        if df is None or df.empty:
            return "empty"
        return f"{len(df)} rows"
    except Exception:
        return "not found"


ld.open_session()

today = dt.date.today()
hist = ld.get_history(universe="AAPL.O", fields=["TRDPRC_1"],
                      start=(today - dt.timedelta(weeks=14)).isoformat(),
                      end=today.isoformat(), interval="daily")
spot = float(hist["TRDPRC_1"].dropna().iloc[-1])
base = round(spot / 5.0) * 5.0
print(f"AAPL {spot:.2f}, probing strike {base:g}\n")

# every Friday in the last 14 weeks
fridays = []
d = today - dt.timedelta(days=1)
while len(fridays) < 14:
    if d.weekday() == 4:
        fridays.append(d)
    d -= dt.timedelta(days=1)
fridays.reverse()

print(f"{'expiry':<12}{'day':>4}  {'unpadded':<26}{'result':<12}"
      f"{'padded':<26}{'result'}")
single, double = [], []
for fri in fridays:
    r_un, r_pad = ric(ROOT, fri, base, False), ric(ROOT, fri, base, True)
    a, b = probe(r_un), probe(r_pad)
    same = (r_un == r_pad)
    print(f"{str(fri):<12}{fri.day:>4}  {r_un:<26}{a:<12}"
          f"{('(same)' if same else r_pad):<26}{'' if same else b}")
    (single if fri.day < 10 else double).append((fri, a, b))

print("\nsingle-digit days")
for fri, a, b in single:
    print(f"  {fri}  unpadded={a:<12} padded={b}")
print("double-digit days (control)")
for fri, a, b in double:
    print(f"  {fri}  {a}")

un_ok = sum(1 for _, a, _ in single if a != "not found")
pad_ok = sum(1 for _, _, b in single if b != "not found")
print(f"\nof {len(single)} single-digit Fridays: unpadded resolved {un_ok}, "
      f"padded resolved {pad_ok}")
if pad_ok > un_ok:
    print("-> the day IS zero-padded; the handout is wrong")
elif un_ok > pad_ok:
    print("-> the day is NOT zero-padded; the handout is right and something else broke 09-04")
else:
    print("-> inconclusive; those Fridays may simply have no listed weekly")

ld.close_session()
