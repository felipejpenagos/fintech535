"""
Tests for the covered-call engine.

Every expected number here is computed by hand in the comments so a failure
tells you which piece of the accounting is wrong, not just that something is.
"""

import sys
from engine import (
    CoveredCallBacktest, Position, reg_t_snapshot, mid_price,
    pick_nearest_otm, pick_by_delta,
)

PASS, FAIL = 0, 0


def check(label, got, want, tol=1e-6):
    global PASS, FAIL
    ok = abs(got - want) < tol if isinstance(want, (int, float)) else got == want
    if ok:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_raises(label, fn):
    global PASS, FAIL
    try:
        fn()
    except Exception:
        PASS += 1
        print(f"  ok   {label}")
        return
    FAIL += 1
    print(f"  FAIL {label}: expected an exception, none raised")


# ---------------------------------------------------------------------------
print("\nmid_price")
# ---------------------------------------------------------------------------
check("normal", mid_price(1.10, 1.30), 1.20)
check("missing bid", mid_price(None, 1.30), None)
check("missing ask", mid_price(1.10, None), None)
check("both missing", mid_price(None, None), None)
check("crossed book rejected", mid_price(1.40, 1.30), None)
check("zero bid rejected", mid_price(0.0, 0.05), None)


# ---------------------------------------------------------------------------
print("\nstrike selection")
# ---------------------------------------------------------------------------
strikes = [180.0, 185.0, 190.0, 195.0, 200.0]
check("nearest OTM above spot", pick_nearest_otm(187.30, strikes), 190.0)
check("spot exactly on strike -> ATM", pick_nearest_otm(190.0, strikes), 190.0)
check("spot above all strikes", pick_nearest_otm(210.0, strikes), None)
check("spot below all strikes", pick_nearest_otm(100.0, strikes), 180.0)

deltas = {185.0: 0.62, 190.0: 0.44, 195.0: 0.29, 200.0: 0.17}
check("delta target 0.30", pick_by_delta(deltas, 0.30), 195.0)
check("delta target 0.50", pick_by_delta(deltas, 0.50), 190.0)
check("delta target 0.15", pick_by_delta(deltas, 0.15), 200.0)


# ---------------------------------------------------------------------------
print("\nReg T arithmetic")
# ---------------------------------------------------------------------------
# 100 shares @ 190, cash 6000, short call marked at 1.20
#   LMV   = 100 * 190           = 19,000
#   optMV = -(1.20 * 100)       = -120
#   NAV   = 6000 + 19000 - 120  = 24,880
#   IM    = 0.50 * 19000        = 9,500
#   MM    = 0.25 * 19000        = 4,750
#   avail = 24880 - 9500        = 15,380
#   excess= 24880 - 4750        = 20,130
p = Position(cash=6000.0, shares=100, short_call_ric="X", short_call_strike=195.0)
s = reg_t_snapshot(p, spot=190.0, option_mark=1.20)
check("LMV", s["lmv"], 19000.0)
check("option MV is negative", s["option_mv"], -120.0)
check("NAV", s["nav"], 24880.0)
check("initial margin", s["initial_margin"], 9500.0)
check("maintenance margin", s["maintenance_margin"], 4750.0)
check("available funds", s["available_funds"], 15380.0)
check("excess equity", s["excess_equity"], 20130.0)

# Flat account: everything keys off LMV, so margin requirements are zero.
s0 = reg_t_snapshot(Position(cash=25000.0), spot=190.0, option_mark=None)
check("flat NAV is just cash", s0["nav"], 25000.0)
check("flat initial margin", s0["initial_margin"], 0.0)
check("flat available = NAV", s0["available_funds"], 25000.0)

# Selling an option is not free money: cash +premium, liability -premium.
# NAV must be unchanged at the instant of sale.
before = reg_t_snapshot(Position(cash=6000.0, shares=100), 190.0, None)["nav"]
after = reg_t_snapshot(
    Position(cash=6000.0 + 120.0, shares=100, short_call_ric="X"), 190.0, 1.20
)["nav"]
check("NAV unchanged when the call is written", after, before)


# ---------------------------------------------------------------------------
print("\nfull week: OTM expiry")
# ---------------------------------------------------------------------------
# Start 25,000 cash. Buy 100 @ 190.00 -> cash 6,000.
# Write 195C at mid 1.20 -> cash 6,120.
# Friday close 192.00 <= 195 -> expires, keep shares.
#   final cash 6,120; shares 100
#   NAV = 6120 + 19200 = 25,320  (vs 25,000 start -> +320)
#   of which +200 is stock appreciation and +120 is kept premium
bt = CoveredCallBacktest("AAPL.O", starting_cash=25000.0)
bt.buy_stock("2026-06-01T09:30", 190.00)
check("cash after stock buy", bt.pos.cash, 6000.0)
check("state after buy", bt.pos.state, "LONG_ONLY")

filled = bt.write_call("2026-06-01T09:31", "AAPLF526119500.U", "AAPL 5Jun26 195C",
                       195.0, "2026-06-05", bid=1.10, ask=1.30)
check("call filled", filled, True)
check("cash after premium", bt.pos.cash, 6120.0)
check("state after write", bt.pos.state, "COVERED")

bt.settle_expiry("2026-06-05T16:00", "AAPLF526119500.U", "AAPL 5Jun26 195C",
                 195.0, stock_close=192.00)
check("cash unchanged on expiry", bt.pos.cash, 6120.0)
check("shares kept", bt.pos.shares, 100)
check("state after expiry", bt.pos.state, "LONG_ONLY")
check("blotter rows", len(bt.blotter), 3)
check("expire side", bt.blotter[-1].side, "EXPIRE")

final_nav = reg_t_snapshot(bt.pos, 192.00, None)["nav"]
check("final NAV", final_nav, 25320.0)


# ---------------------------------------------------------------------------
print("\nfull week: ITM assignment")
# ---------------------------------------------------------------------------
# Same setup, Friday close 201.00 > 195 -> assigned.
#   cash 6,120 + 100*195 = 25,620; shares 0
#   NAV = 25,620 (vs 25,000 start -> +620)
#   Upside above 195 is forfeited: stock went to 201 but we sold at 195.
#   Unhedged we'd have +1,100. Cap cost us 480, premium gave back 120.
bt2 = CoveredCallBacktest("AAPL.O", starting_cash=25000.0)
bt2.buy_stock("2026-06-01T09:30", 190.00)
bt2.write_call("2026-06-01T09:31", "AAPLF526119500.U", "AAPL 5Jun26 195C",
               195.0, "2026-06-05", bid=1.10, ask=1.30)
bt2.settle_expiry("2026-06-05T16:00", "AAPLF526119500.U", "AAPL 5Jun26 195C",
                  195.0, stock_close=201.00)
check("cash after assignment", bt2.pos.cash, 25620.0)
check("shares delivered", bt2.pos.shares, 0)
check("state after assignment", bt2.pos.state, "FLAT")
check("assignment books 4 rows", len(bt2.blotter), 4)
check("ASSIGN row present", bt2.blotter[2].side, "ASSIGN")
check("stock SELL at strike", bt2.blotter[3].fill, 195.0)

nav2 = reg_t_snapshot(bt2.pos, 201.00, None)["nav"]
check("final NAV after assignment", nav2, 25620.0)
# Sanity: buy-and-hold would have been 25,000 + 100*(201-190) = 26,100.
check("covered call underperforms in a rip", nav2 < 26100.0, True)


# ---------------------------------------------------------------------------
print("\nskipped week: no bid/ask")
# ---------------------------------------------------------------------------
bt3 = CoveredCallBacktest("MPWR.O", starting_cash=100000.0)
bt3.buy_stock("2026-06-01T09:30", 700.00)
filled = bt3.write_call("2026-06-01T09:31", "MPWRF526170000.U", "MPWR 5Jun26 700C",
                        700.0, "2026-06-05", bid=None, ask=None)
check("not filled", filled, False)
check("still LONG_ONLY", bt3.pos.state, "LONG_ONLY")
check("no premium booked", bt3.pos.cash, 100000.0 - 70000.0)
check("skip recorded", len(bt3.skipped_weeks), 1)
check("only the stock buy is on the blotter", len(bt3.blotter), 1)


# ---------------------------------------------------------------------------
print("\nstate machine guards")
# ---------------------------------------------------------------------------
bt4 = CoveredCallBacktest("AAPL.O", starting_cash=25000.0)
bt4.buy_stock("2026-06-01T09:30", 190.00)
check_raises("cannot buy stock twice", lambda: bt4.buy_stock("2026-06-01T09:32", 190.0))

bt4.write_call("2026-06-01T09:31", "R", "occ", 195.0, "2026-06-05", 1.10, 1.30)
check_raises(
    "cannot write a second call while covered",
    lambda: bt4.write_call("2026-06-01T09:33", "R2", "occ", 200.0, "2026-06-05", 1.0, 1.2),
)


# ---------------------------------------------------------------------------
print("\nmulti-week run, cash conservation")
# ---------------------------------------------------------------------------
# Three weeks, hand-traced:
#   W1 buy 100 @ 100 -> cash 15,000. write 105C @ 2.00 -> 15,200.
#      close 102 -> expire. cash 15,200, shares 100.
#   W2 already long. write 106C @ 1.50 -> 15,350.
#      close 108 > 106 -> assign: +10,600 -> 25,950, shares 0.
#   W3 flat -> buy 100 @ 107 -> 15,250. write 110C @ 1.00 -> 15,350.
#      close 109 -> expire. cash 15,350, shares 100.
#   NAV at 109 = 15,350 + 10,900 = 26,250
bt5 = CoveredCallBacktest("TEST.O", starting_cash=25000.0)

bt5.buy_stock("W1-mon", 100.00)
bt5.write_call("W1-mon", "C105", "TEST 105C", 105.0, "W1-fri", 1.90, 2.10)
bt5.settle_expiry("W1-fri", "C105", "TEST 105C", 105.0, 102.00)
check("W1 cash", bt5.pos.cash, 15200.0)

bt5.write_call("W2-mon", "C106", "TEST 106C", 106.0, "W2-fri", 1.40, 1.60)
bt5.settle_expiry("W2-fri", "C106", "TEST 106C", 106.0, 108.00)
check("W2 cash after assignment", bt5.pos.cash, 25950.0)
check("W2 flat", bt5.pos.state, "FLAT")

bt5.buy_stock("W3-mon", 107.00)
bt5.write_call("W3-mon", "C110", "TEST 110C", 110.0, "W3-fri", 0.90, 1.10)
bt5.settle_expiry("W3-fri", "C110", "TEST 110C", 110.0, 109.00)
check("W3 cash", bt5.pos.cash, 15350.0)
check("W3 shares", bt5.pos.shares, 100)

check("final NAV", reg_t_snapshot(bt5.pos, 109.0, None)["nav"], 26250.0)

# Cash must equal starting cash plus the sum of every booked cash delta.
total_deltas = sum(r.cash_delta for r in bt5.blotter)
check("cash reconciles to blotter", bt5.pos.cash, 25000.0 + total_deltas)

# Every EXPIRE/ASSIGN row on the option itself must move zero cash.
zero_cash_sides = [r for r in bt5.blotter if r.side in ("EXPIRE", "ASSIGN")]
check("expire/assign rows move no cash on the option leg",
      all(r.cash_delta == 0.0 for r in zero_cash_sides), True)


# ---------------------------------------------------------------------------
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
