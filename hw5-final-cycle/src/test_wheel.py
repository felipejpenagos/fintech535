"""
test_wheel.py -- scenario tests for the nightly bot. Runs with plain python:

    python3 hw5-final-cycle/src/test_wheel.py

Each test sets up a situation (book, vols, dates) and checks what the robot does.
"""
import datetime as dt
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
import wheel_algo as w   # noqa: E402
import sanji_check       # noqa: E402
import bs                # noqa: E402

NY = ZoneInfo("America/New_York")
MON = dt.date(2026, 11, 23)
CONID = 208813720
passed = 0


def ok(cond, name):
    global passed
    if not cond:
        raise AssertionError(name)
    passed += 1
    print(f"  pass  {name}")


def only(orders):
    assert len(orders) == 1, orders
    assert sanji_check.check(orders[0]) == [], sanji_check.check(orders[0])
    return orders[0]


print("decisions")
o = only(w.decide((0, 0, 0, None), 0.45, 0.30, 264.0, MON, CONID)[0])
ok(o["action"] == "SHT" and o["contract"] == "put", "flat + rich options -> sell a put")
ok(o["order_type"] == "lmt" and o["lmt_prc"] == "mid", "limit at mid, like the spec's wheel example")
ok(o["for"] == "2026-11-23" and o["expiry"] == "5DTE+", "dated for the session, weekly expiry")

orders, text, det = w.decide((0, 0, 0, None), 0.25, 0.30, 264.0, MON, CONID)
ok(orders == [] and det["filter"].startswith("skip"), "flat + cheap options (IV <= RV) -> no order")

o = only(w.decide((0, 0, 0, None), None, 0.30, 264.0, MON, CONID)[0])
ok(o["contract"] == "put", "IV unavailable -> plain wheel still sells the put")

o = only(w.decide((100, 0, 0, 250.0), 0.45, 0.30, 264.0, MON, CONID)[0])
ok(o["contract"] == "call" and o["strike"].startswith("pct:"), "own 100 shares, cost below target -> covered call by delta")

o = only(w.decide((100, 0, 0, 300.0), 0.45, 0.30, 264.0, MON, CONID)[0])
ok(o["contract"] == "call" and o["strike"] == "300", "own shares bought at 300 -> call never below cost basis")

o = only(w.decide((0, 0, 0, None), 0.25, 0.30, 264.0, MON, CONID, mode="off")[0])
ok(o["contract"] == "put", "filter switched off -> plain wheel sells even when IV <= RV")
o = only(w.decide((100, 0, 0, 250.0), 0.25, 0.30, 264.0, MON, CONID, mode="puts")[0])
ok(o["contract"] == "call", "puts-only filter -> covered call still sold when IV <= RV")
ok(w.decide((0, 1, 0, None), 0.45, 0.30, 264.0, MON, CONID)[0] == [], "short put open -> wait")
ok(w.decide((100, 0, 1, 250.0), 0.45, 0.30, 264.0, MON, CONID)[0] == [], "covered call open -> wait")

print("strike = 30 delta")
T = 7 / 365
for kind in ("put", "call"):
    pct = w.delta_strike_pct(0.40, T, kind)
    K = 100 * (1 + pct / 100)
    d = bs.put_delta(100, K, T, w.R, 0, 0.40) if kind == "put" else bs.delta(100, K, T, w.R, 0, 0.40)
    ok(abs(abs(d) - 0.30) < 0.02, f"{kind} pct:{pct} has |delta| {abs(d):.3f} ~ 0.30")
ok(w.delta_strike_pct(0.80, T, "put") < w.delta_strike_pct(0.40, T, "put"), "higher vol -> put strike further out")

print("session dates")
ok(w.plan_session(dt.datetime(2026, 11, 23, 22, 0, tzinfo=NY)) == dt.date(2026, 11, 24), "Monday 10pm -> plans Tuesday")
ok(w.plan_session(dt.datetime(2026, 11, 23, 5, 0, tzinfo=NY)) == dt.date(2026, 11, 23), "Monday 5am -> plans Monday")
ok(w.plan_session(dt.datetime(2026, 11, 25, 22, 0, tzinfo=NY)) == dt.date(2026, 11, 27), "Wednesday night -> skips Thanksgiving")
ok(w.plan_session(dt.datetime(2026, 11, 27, 22, 0, tzinfo=NY)) == dt.date(2026, 11, 30), "Friday night -> Monday")
ok(w.weekly_expiry(dt.date(2026, 11, 24)) == dt.date(2026, 12, 4), "Tuesday session -> 5DTE+ weekly is Friday Dec 4")

print("realized vol")
bars = {f"2026-10-{d:02d}": {"close": 100 * math.exp(0.01 * (-1) ** d)} for d in range(1, 31)}
rv = w.realized_vol(bars)
ok(rv is not None and 0.2 < rv < 0.4, f"alternating +-1% days -> RV {rv:.2f} (about 0.32)")

print("positions.json, read defensively")
ok(w.read_book([], CONID) == (0, 0, 0, None), "empty book -> flat")
ok(w.read_book([{"underlying": CONID, "contract": "underlying", "qty": 100, "avg_cost": 250}], CONID)[0] == 100, "long shares read")
ok(w.read_book([{"underlying": CONID, "contract": "put", "qty": -1}], CONID)[1] == 1, "short put read (negative qty)")
ok(w.read_book([{"underlying": CONID, "contract": "put", "qty": 1, "side": "SHT"}], CONID)[1] == 1, "short put read (side field)")
ok(w.read_book({"positions": [{"underlying": 1, "contract": "underlying", "qty": 100}]}, CONID)[0] == 0, "other stock ignored")

print("order checker")
good = dict(id="x", **{"for": "2026-11-24"}, order_type="lmt", action="SHT", qty=1, underlying=CONID,
            contract="put", expiry="5DTE+", strike="otm:1", ref="open", lmt_prc="mid")
ok(sanji_check.check(good) == [], "spec's own example passes")
for bad, why in [(dict(good, qty=0), "qty 0"), (dict(good, **{"for": "next"}), "for = next"),
                 (dict(good, strike="vega:0.15"), "vega token"), (dict(good, order_type="mkt"), "mkt with lmt_prc"),
                 (dict(good, color="red"), "unknown field"), (dict(good, underlying="GOOGL"), "ticker not conid"),
                 (dict(good, expiry="tomorrow"), "bad expiry token")]:
    ok(sanji_check.check(bad) != [], f"rejects {why}")
ok(sanji_check.check_book([good, good]) != [], "rejects duplicate ids")

print(f"\nall {passed} checks passed")
