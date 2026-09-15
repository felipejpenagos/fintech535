"""
Covered-call backtest engine.

Pure logic: no LSEG dependency. Takes bar data in, produces a blotter and
ledger out. This separation exists so the accounting can be tested against
synthetic data without burning API quota.

Position state machine
----------------------
    FLAT        no shares, no short call
    LONG_ONLY   100 shares, no short call   (call expired OTM last Friday)
    COVERED     100 shares, 1 short call

    Monday:  FLAT      -> buy 100 shares -> LONG_ONLY
             LONG_ONLY -> write 1 call   -> COVERED
             (no bid/ask on any candidate strike -> skip the week)

    Friday:  COVERED and S_close >  K -> ASSIGN -> FLAT
             COVERED and S_close <= K -> EXPIRE -> LONG_ONLY
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Optional

CONTRACT_MULTIPLIER = 100
SHARES_PER_CONTRACT = 100

# Reg T / FINRA
INITIAL_MARGIN_RATE = 0.50
MAINTENANCE_MARGIN_RATE = 0.25


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class BlotterRow:
    """One booked trade. Working orders never appear here."""
    time: str
    instrument: str          # stock RIC or option RIC
    occ: str                 # human-readable subtitle, e.g. "AAPL 26Sep25 250C"
    side: str                # BUY | SELL | EXPIRE | ASSIGN
    qty: int
    limit: Optional[float]   # None for EXPIRE/ASSIGN (not orders)
    fill: Optional[float]
    cash_delta: float
    note: str                # which rule fired


@dataclass
class LedgerRow:
    """Account state at a point in time."""
    time: str
    cash: float
    shares: int
    short_call_ric: Optional[str]
    short_call_strike: Optional[float]
    short_call_expiry: Optional[str]
    spot: float
    option_mark: Optional[float]   # per-share mid of the short call
    lmv: float
    option_mv: float               # negative when short
    nav: float
    initial_margin: float
    maintenance_margin: float
    available_funds: float
    excess_equity: float


@dataclass
class Position:
    cash: float
    shares: int = 0
    short_call_ric: Optional[str] = None
    short_call_strike: Optional[float] = None
    short_call_expiry: Optional[str] = None

    @property
    def state(self) -> str:
        if self.shares == 0 and self.short_call_ric is None:
            return "FLAT"
        if self.shares > 0 and self.short_call_ric is None:
            return "LONG_ONLY"
        if self.shares > 0 and self.short_call_ric is not None:
            return "COVERED"
        return "INVALID"


# ---------------------------------------------------------------------------
# Reg T
# ---------------------------------------------------------------------------
def reg_t_snapshot(pos: Position, spot: float, option_mark: Optional[float]) -> dict:
    """
    Reg T account arithmetic for a covered call.

    NAV       = cash + stock MV + option MV      (short call is negative MV)
    LMV       = shares x spot
    Initial   = 50% of stock LMV; a COVERED short call adds $0
    Maint     = 25% of stock LMV (FINRA)
    Available = NAV - initial
    Excess    = NAV - maintenance
    """
    lmv = pos.shares * spot

    if pos.short_call_ric is not None and option_mark is not None:
        option_mv = -(option_mark * CONTRACT_MULTIPLIER)
    else:
        option_mv = 0.0

    nav = pos.cash + lmv + option_mv

    # The short call is covered by the shares, so it adds no initial or
    # maintenance requirement. A naked call would.
    initial = INITIAL_MARGIN_RATE * lmv
    maintenance = MAINTENANCE_MARGIN_RATE * lmv

    return {
        "lmv": lmv,
        "option_mv": option_mv,
        "nav": nav,
        "initial_margin": initial,
        "maintenance_margin": maintenance,
        "available_funds": nav - initial,
        "excess_equity": nav - maintenance,
    }


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------
def pick_nearest_otm(spot: float, strikes: list[float]) -> Optional[float]:
    """
    Lowest strike strictly above spot. If spot sits exactly on a strike,
    that strike is ATM and we take it (per the assignment spec).
    """
    if not strikes:
        return None
    exact = [k for k in strikes if abs(k - spot) < 1e-9]
    if exact:
        return exact[0]
    above = sorted(k for k in strikes if k > spot)
    return above[0] if above else None


def pick_by_delta(deltas: dict[float, float], target: float = 0.30) -> Optional[float]:
    """
    Strike whose call delta is closest to `target`.

    Delta is roughly the probability of finishing in the money, so targeting a
    delta fixes assignment probability across volatility regimes. "Nearest OTM"
    does not: in a calm week it might be 25-delta, in a wild week 45-delta.

    `deltas` maps strike -> call delta in [0, 1].
    """
    valid = {k: d for k, d in deltas.items() if d is not None}
    if not valid:
        return None
    return min(valid, key=lambda k: abs(valid[k] - target))


# ---------------------------------------------------------------------------
# Fill logic
# ---------------------------------------------------------------------------
def mid_price(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """
    (BID + ASK) / 2. Returns None if either side is missing -- no bid/ask
    means no fill. Inventing a print is the one thing the assignment
    explicitly forbids.
    """
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


# ---------------------------------------------------------------------------
# The backtest
# ---------------------------------------------------------------------------
class CoveredCallBacktest:
    def __init__(
        self,
        underlying: str,
        starting_cash: float,
        strike_rule: str = "nearest_otm",   # or "delta"
        target_delta: float = 0.30,
    ):
        self.underlying = underlying
        self.strike_rule = strike_rule
        self.target_delta = target_delta
        self.pos = Position(cash=starting_cash)
        self.blotter: list[BlotterRow] = []
        self.ledger: list[LedgerRow] = []
        self.skipped_weeks: list[dict] = []
        self.margin_breaches: list[dict] = []

    # -- blotter helpers ---------------------------------------------------
    def _book(self, time, instrument, occ, side, qty, limit, fill, cash_delta, note):
        self.pos.cash += cash_delta
        self.blotter.append(BlotterRow(
            time=time, instrument=instrument, occ=occ, side=side, qty=qty,
            limit=limit, fill=fill, cash_delta=cash_delta, note=note,
        ))

    def _mark(self, time: str, spot: float, option_mark: Optional[float]):
        snap = reg_t_snapshot(self.pos, spot, option_mark)
        if snap["available_funds"] < 0:
            self.margin_breaches.append({"time": time, **snap})
        self.ledger.append(LedgerRow(
            time=time,
            cash=self.pos.cash,
            shares=self.pos.shares,
            short_call_ric=self.pos.short_call_ric,
            short_call_strike=self.pos.short_call_strike,
            short_call_expiry=self.pos.short_call_expiry,
            spot=spot,
            option_mark=option_mark,
            **snap,
        ))

    # -- events ------------------------------------------------------------
    def buy_stock(self, time: str, price: float):
        if self.pos.state != "FLAT":
            raise RuntimeError(f"buy_stock called while {self.pos.state} at {time}")
        cost = price * SHARES_PER_CONTRACT
        self.pos.shares += SHARES_PER_CONTRACT
        self._book(
            time=time, instrument=self.underlying, occ=f"{self.underlying} common",
            side="BUY", qty=SHARES_PER_CONTRACT, limit=price, fill=price,
            cash_delta=-cost,
            note="Entry rule: flat on Monday -> buy 100 shares at the stock print",
        )

    def write_call(self, time, ric, occ, strike, expiry, bid, ask):
        """Returns True if filled, False if no bid/ask (week skipped)."""
        if self.pos.state != "LONG_ONLY":
            raise RuntimeError(f"write_call called while {self.pos.state} at {time}")
        mid = mid_price(bid, ask)
        if mid is None:
            self.skipped_weeks.append({
                "time": time, "ric": ric, "reason": "no bid/ask -> no fill",
            })
            return False
        premium = mid * CONTRACT_MULTIPLIER
        self.pos.short_call_ric = ric
        self.pos.short_call_strike = strike
        self.pos.short_call_expiry = expiry
        rule_text = (f"target {self.target_delta:g} delta"
                     if self.strike_rule == "delta" else "nearest strike at or above spot")
        self._book(
            time=time, instrument=ric, occ=occ, side="SELL", qty=1,
            limit=mid, fill=mid, cash_delta=+premium,
            note=f"Entry rule: write 1 call, {rule_text}; limit at mid = (BID+ASK)/2",
        )
        return True

    def settle_expiry(self, time: str, ric: str, occ: str, strike: float, stock_close: float):
        """Friday. ITM -> assigned and flat. OTM -> expires, keep shares."""
        if self.pos.state != "COVERED":
            return

        if stock_close > strike:
            # Call is exercised against us: deliver 100 shares, receive strike.
            proceeds = strike * SHARES_PER_CONTRACT
            self._book(
                time=time, instrument=ric, occ=occ, side="ASSIGN", qty=1,
                limit=None, fill=None, cash_delta=0.0,
                note=f"Exit rule: close {stock_close:.2f} > strike {strike:.2f} -> assigned",
            )
            self.pos.shares -= SHARES_PER_CONTRACT
            self._book(
                time=time, instrument=self.underlying, occ=f"{self.underlying} common",
                side="SELL", qty=SHARES_PER_CONTRACT, limit=strike, fill=strike,
                cash_delta=+proceeds,
                note="Assignment: shares delivered at the strike",
            )
        else:
            self._book(
                time=time, instrument=ric, occ=occ, side="EXPIRE", qty=1,
                limit=None, fill=None, cash_delta=0.0,
                note=f"Exit rule: close {stock_close:.2f} <= strike {strike:.2f} -> expires worthless",
            )

        self.pos.short_call_ric = None
        self.pos.short_call_strike = None
        self.pos.short_call_expiry = None

    # -- output ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "strike_rule": self.strike_rule,
            "target_delta": self.target_delta if self.strike_rule == "delta" else None,
            "blotter": [asdict(r) for r in self.blotter],
            "ledger": [asdict(r) for r in self.ledger],
            "skipped_weeks": self.skipped_weeks,
            "margin_breaches": self.margin_breaches,
        }
