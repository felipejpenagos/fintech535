"""
wheel_algo.py -- nightly volatility-filtered wheel on the stock Sanji tracks.

An algorithm is a function: (prices, book, implied vol) -> open orders.

Inputs (never modified here)
    price_history.json        daily OHLCV per IBKR conid, pushed by Sanji
    mkt/positions.json        end-of-session book from Sanji (root copy read as a fallback)
    LSEG (headless)           near-the-money option quotes -> implied vol
Outputs
    mkt/open_orders.json      the order file Sanji reads (spec: order.html)
    open_orders.json          identical copy at the repo root (assignment wording)
    hw5-final-cycle/status.json   tonight's decision and the numbers behind it

The rule
    1. Sell options only when they are rich: ATM implied vol (IV) > 20-day realized vol (RV).
    2. Flat                  -> SHT 1 put  at the ~30-delta strike, weekly (5DTE+), limit at mid.
       Own >= 100 shares     -> SHT 1 call at the ~30-delta strike, never below cost basis.
       Short option open     -> nothing; let it expire or be assigned.
    3. IV <= RV              -> no new option this session (insurance is cheap, don't sell it).
    If IV can't be read (LSEG down), the plain wheel runs and the page says so.
"""
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "hw5-final-cycle" / "src"))
import sanji_check  # noqa: E402

NY = ZoneInfo("America/New_York")
CONID_DEFAULT = 208813720                 # ALPHABET INC, seeded by Sanji in this repo
TICKERS = {208813720: "GOOGL", 76792991: "TSLA"}
TARGET_DELTA = 0.30                       # same target as HW4
LOOKBACK = 20                             # trading days of realized vol
R, Q = 0.04, 0.003
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
            "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}


# ---------- small pure helpers (tested in hw5-final-cycle/src/test_wheel.py) ----------

def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def is_session(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def plan_session(now):
    """Today if it is a trading day and before the 9:30 open, else the next trading day."""
    d = now.date()
    if is_session(d) and (now.hour, now.minute) < (9, 30):
        return d
    d += dt.timedelta(days=1)
    while not is_session(d):
        d += dt.timedelta(days=1)
    return d


def weekly_expiry(session):
    """First Friday at least 5 calendar days after the session (what 5DTE+ resolves to)."""
    d = session + dt.timedelta(days=5)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def realized_vol(bars, n=LOOKBACK):
    closes = [bars[k]["close"] for k in sorted(bars)][-(n + 1):]
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
    if len(r) < 5:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


def delta_strike_pct(vol, T, kind, target=TARGET_DELTA):
    """% distance from the open to the strike with |delta| = target (Black-Scholes).
    put:  N(d1) - 1 = -target  -> d1 = N^-1(1 - target)
    call: N(d1)     =  target  -> d1 = N^-1(target)
    and ln(S/K) = d1 * vol * sqrt(T) - (r - q + vol^2/2) T."""
    z = NormalDist().inv_cdf(1 - target if kind == "put" else target)
    ln_s_over_k = z * vol * math.sqrt(T) - (R - Q + vol * vol / 2) * T
    return round(100 * (math.exp(-ln_s_over_k) - 1), 1)


def read_book(pos, conid):
    """positions.json has no published schema yet, so read it defensively.
    Returns (shares, short_puts, short_calls, cost_basis)."""
    if isinstance(pos, dict):
        rows = pos.get("positions", pos.get("book", list(pos.values())))
    else:
        rows = pos or []
    shares = puts = calls = 0
    cost = None
    for p in rows if isinstance(rows, list) else []:
        if not isinstance(p, dict) or str(p.get("underlying", p.get("conid", conid))) != str(conid):
            continue
        q = int(p.get("qty", p.get("quantity", p.get("position", 0))) or 0)
        if str(p.get("side", p.get("action", ""))).upper() in ("SHT", "SHORT", "S"):
            q = -abs(q)
        kind = str(p.get("contract", p.get("type", "underlying"))).lower()
        if kind in ("underlying", "stock", "shares"):
            shares += q
            cost = p.get("avg_cost", p.get("cost_basis", p.get("price", cost)))
        elif kind == "put" and q < 0:
            puts += -q
        elif kind == "call" and q < 0:
            calls += -q
    return shares, puts, calls, cost


def decide(book, iv, rv, spot, session, conid, ticker="GOOGL", mode="all"):
    """The algorithm. Returns (orders, decision text, details).
    mode: "all" gates puts and calls on IV > RV, "puts" gates only puts, "off" is the plain wheel."""
    shares, short_puts, short_calls, cost = book
    T = (weekly_expiry(session) - session).days / 365
    vol = iv if iv else rv
    det = dict(iv=iv, rv=rv, vol_for_strike=vol, T_days=round(T * 365))
    if short_puts or short_calls:
        return [], "A short option is still open: wait for it to expire or be assigned.", det
    gated = mode == "all" or (mode == "puts" and shares < 100)
    rich = (not gated) or iv is None or rv is None or iv > rv
    det["filter"] = ("off" if not gated else "no IV: plain wheel" if iv is None
                     else "pass: IV > RV" if rich else "skip: IV <= RV")
    if not rich:
        return [], f"Options are cheap (IV {iv:.0%} <= RV {rv:.0%}): sell nothing this session.", det
    base = {"for": session.isoformat(), "order_type": "lmt", "qty": 1, "underlying": int(conid),
            "expiry": "5DTE+", "ref": "open", "lmt_prc": "mid"}
    t = ticker.lower()
    if shares >= 100:
        pct = delta_strike_pct(vol, T, "call")
        strike = f"pct:{pct}"
        if cost and spot and float(cost) > spot * (1 + pct / 100):
            strike = str(int(math.ceil(float(cost) / 5) * 5))       # never sell below cost basis
        det["strike"] = strike
        return ([dict(id=f"{t}-cc-{session}", action="SHT", contract="call", strike=strike, **base)],
                f"Own {shares} shares: sell 1 covered call, strike {strike}, limit at mid.", det)
    pct = delta_strike_pct(vol, T, "put")
    det["strike"] = f"pct:{pct}"
    return ([dict(id=f"{t}-csp-{session}", action="SHT", contract="put", strike=f"pct:{pct}", **base)],
            f"Flat: sell 1 cash-secured put, strike pct:{pct}, limit at mid.", det)


# ---------- nightly run ----------

def main():
    now = dt.datetime.now(NY)
    hist = load(ROOT / "price_history.json", {})
    tracked = [k for k, v in hist.items() if isinstance(v, dict) and v.get("tracked", True)]
    conid = int(os.environ.get("WHEEL_CONID") or (tracked[0] if tracked else CONID_DEFAULT))
    ticker = os.environ.get("WHEEL_TICKER") or TICKERS.get(conid, "STOCK")
    rec = hist.get(str(conid), {})
    bars = rec.get("price_history", {})
    spot = bars[max(bars)]["close"] if bars else None
    session = plan_session(now)
    rv = realized_vol(bars) if bars else None

    iv_info = dict(iv=None, reason="LSEG not configured")
    if spot and os.environ.get("LSEG_APP_KEY"):
        try:
            import live_iv
            s = live_iv.open_session()
            try:
                iv_info = live_iv.atm_iv(ticker, spot, session, today=now.date())
            finally:
                s.close()
        except Exception as e:                     # never let a data hiccup kill the order file
            iv_info = dict(iv=None, reason=f"LSEG error: {type(e).__name__}")

    pos = load(ROOT / "mkt" / "positions.json", None)
    if pos is None:
        pos = load(ROOT / "positions.json", [])
    book = read_book(pos, conid)
    mode = os.environ.get("WHEEL_FILTER", "all")
    orders, decision, det = decide(book, iv_info.get("iv"), rv, spot, session, conid, ticker, mode)

    # keep orders Sanji may not have processed yet; re-plan this session from scratch
    old = load(ROOT / "mkt" / "open_orders.json", None) or load(ROOT / "open_orders.json", [])
    keep = [o for o in old if isinstance(o, dict) and o.get("for", "") >= now.date().isoformat()
            and o.get("for") != session.isoformat()]
    out = keep + orders
    problems = sanji_check.check_book(out)
    if problems:
        sys.exit("refusing to write invalid orders:\n  " + "\n  ".join(problems))

    text = json.dumps(out, indent=2) + "\n"
    (ROOT / "mkt").mkdir(exist_ok=True)
    (ROOT / "mkt" / "open_orders.json").write_text(text)
    (ROOT / "open_orders.json").write_text(text)

    status = dict(run_at=now.isoformat(timespec="minutes"), session=session.isoformat(),
                  conid=conid, ticker=ticker, name=rec.get("name"), last_close=spot,
                  realized_vol_20d=round(rv, 4) if rv else None,
                  implied_vol_atm=iv_info.get("iv"), iv_detail={k: v for k, v in iv_info.items() if k != "iv"},
                  filter_mode=mode, filter=det.get("filter"), strike=det.get("strike"), target_expiry=str(weekly_expiry(session)),
                  book=dict(shares=book[0], short_puts=book[1], short_calls=book[2], cost_basis=book[3]),
                  decision=decision, validation="all orders pass the Sanji spec check", open_orders=out)
    (ROOT / "hw5-final-cycle" / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps({k: status[k] for k in ("session", "ticker", "realized_vol_20d", "implied_vol_atm",
                                             "iv_detail", "filter", "strike", "decision", "validation")}, indent=2))


if __name__ == "__main__":
    main()
