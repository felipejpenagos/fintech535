"""
wheel_algo.py -- nightly wheel on the stock Sanji tracks in price_history.json.

An algorithm is a function: (prices, book) -> open orders.

Inputs  (written by Sanji / Luffy, never modified here)
    price_history.json          daily OHLCV per IBKR conid
    positions.json or mkt/positions.json   end-of-session book (optional; empty on first run)
Output
    open_orders.json            at the repo root, Sanji order language
    hw5-final-cycle/status.json what tonight's run decided (shown on the website)

Rule (the wheel from HW4, strike distance scaled by realized volatility):
    no shares, no short option  -> SHT 1 put,  ~0.5 sigma below the open, nearest weekly (5DTE+)
    >=100 shares, no short call -> SHT 1 call, ~0.5 sigma above the open, never below cost basis
    a short option still open   -> no new order (let it expire or be assigned)
sigma = 20-day realized vol scaled to one week: RV * sqrt(7/365). 0.5 sigma is roughly a 30-delta option.
"""
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONID_DEFAULT = 208813720        # ALPHABET INC (what Sanji seeded in this repo)
Z = 0.5                          # strike distance in weekly sigmas (~30 delta)
LOOKBACK = 20                    # trading days for realized vol
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15"}


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def next_session(today):
    d = today + dt.timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in HOLIDAYS:
        d += dt.timedelta(days=1)
    return d


def realized_vol(bars):
    closes = [bars[k]["close"] for k in sorted(bars)][-(LOOKBACK + 1):]
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
    if len(r) < 5:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


def read_book(pos, conid):
    """positions.json has no published schema yet, so read it defensively.
    Returns (shares, short_puts, short_calls, cost_basis)."""
    rows = pos if isinstance(pos, list) else pos.get("positions", list(pos.values()) if isinstance(pos, dict) else [])
    shares = puts = calls = 0
    cost = None
    for p in rows:
        if not isinstance(p, dict) or str(p.get("underlying", conid)) != str(conid):
            continue
        q = int(p.get("qty", p.get("quantity", 0)) or 0)
        side = str(p.get("side", p.get("action", ""))).upper()
        if side in ("SHT", "SHORT", "S"):
            q = -abs(q)
        kind = p.get("contract", "underlying")
        if kind == "underlying":
            shares += q
            cost = p.get("avg_cost", p.get("cost_basis", p.get("price", cost)))
        elif kind == "put" and q < 0:
            puts += -q
        elif kind == "call" and q < 0:
            calls += -q
    return shares, puts, calls, cost


def main():
    hist = load(ROOT / "price_history.json", {})
    tracked = [k for k, v in hist.items() if isinstance(v, dict) and v.get("tracked", True)]
    conid = int(os.environ.get("WHEEL_CONID") or (tracked[0] if tracked else CONID_DEFAULT))
    rec = hist.get(str(conid), {})
    bars = rec.get("price_history", {})
    pos = load(ROOT / "positions.json", None) or load(ROOT / "mkt" / "positions.json", [])

    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    session = next_session(today).isoformat()
    last_close = bars[max(bars)]["close"] if bars else None
    rv = realized_vol(bars) if bars else None
    rv = rv if rv else 0.35                       # fallback if history is too short
    move = Z * rv * math.sqrt(7 / 365)            # fractional strike distance
    pct = round(100 * move, 1)

    shares, short_puts, short_calls, cost = read_book(pos, conid)
    base = dict(**{"for": session}, order_type="mkt", qty=1, underlying=conid, expiry="5DTE+", ref="open")
    orders, decision = [], ""
    if short_puts or short_calls:
        decision = "A short option is still open: wait for expiry or assignment."
    elif shares >= 100:
        strike = f"pct:{pct}"
        if cost and last_close and cost > last_close * (1 + move):
            strike = str(int(math.ceil(float(cost) / 5) * 5))      # never sell below cost basis
        orders.append(dict(base, id=f"googl-cc-{session}", action="SHT", contract="call", strike=strike))
        decision = f"Own {shares} shares: sell 1 covered call, strike {strike}."
    else:
        orders.append(dict(base, id=f"googl-csp-{session}", action="SHT", contract="put", strike=f"pct:-{pct}"))
        decision = f"Flat: sell 1 cash-secured put, strike pct:-{pct}."

    # keep orders Sanji has not processed yet (for >= today); replace any with the same id
    old = load(ROOT / "open_orders.json", [])
    keep = [o for o in old if isinstance(o, dict) and o.get("for", "") >= today.isoformat()
            and o.get("id") not in {n["id"] for n in orders}]
    book = keep + orders
    (ROOT / "open_orders.json").write_text(json.dumps(book, indent=2) + "\n")

    status = dict(run_at=dt.datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="minutes"),
                  session=session, conid=conid, name=rec.get("name"), last_close=last_close,
                  realized_vol_20d=round(rv, 4), strike_distance_pct=pct,
                  book=dict(shares=shares, short_puts=short_puts, short_calls=short_calls, cost_basis=cost),
                  decision=decision, open_orders=book)
    (ROOT / "hw5-final-cycle").mkdir(exist_ok=True)
    (ROOT / "hw5-final-cycle" / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
