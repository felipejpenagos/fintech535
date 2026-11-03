"""
validate_wheel.py -- re-derive the wheel book from the raw LSEG data.

    python3 validate_wheel.py AAPL
"""
import json
import re
import sys
import datetime as dt

import backtest as bt
import wheel as wh


def ric_parts(ric):
    m = re.match(r"^([A-Z]+?)([A-Xa-x])(\d{2})(\d{2})(\d{5})\.U", ric)
    root, body, day, yy, strike = m.groups()
    kind = "P" if body.upper() >= "M" else "C"
    month = (ord(body.upper()) - ord("A")) % 12 + 1
    return kind, dt.date(2000 + int(yy), month, int(day)), int(strike) / 100


def main(root, key="base"):
    D = wh.load(root)
    B = json.load(open(f"data/wheel_{root}.json"))[key]
    fails, n = [], 0

    def check(ok, msg):
        nonlocal n
        n += 1
        if not ok:
            fails.append(msg)

    weeks = {str(w["expiry"]): w for w in D["weeks"]}
    bl, lg, wk = B["blotter"], B["ledger"], B["weeks"]

    # 1. cash reconciles row by row
    cash = 0.0
    for r in bl:
        cash += r["cash_flow"]
        check(abs(cash - r["cash"]) < 0.05, f"cash breaks at {r['t']}")

    # 2. NAV = cash + shares x S - short option mark; positions are only 0 or 100 shares, one option
    for l in lg:
        parts = l["cash"] + l["shares"] * l["S"] - (100 * l["opt_mark"] if l["opt_mark"] is not None else 0)
        check(abs(parts - l["nav"]) < 0.05, f"NAV != parts on {l['d']}")
        check(l["shares"] in (0, 100), f"shares {l['shares']} on {l['d']}")
        check(not (l["short_put"] and l["covered_call"]), f"two options open on {l['d']}")
        # a put only while flat, a call only while holding: the wheel's two phases
        if l["short_put"]:
            check(l["shares"] == 0, f"put sold while holding shares on {l['d']}")
            check(abs(l["collateral"] - 100 * float(l["short_put"].split("P")[0])) < 0.01, f"collateral on {l['d']}")
        if l["covered_call"]:
            check(l["shares"] == 100, f"naked call on {l['d']}")

    # 3. option trades: inside the quoted market of the fill bar, real contract, not expired
    for r in bl:
        if ".U" not in r["inst"] or r["side"] not in ("BUY", "SELL"):
            continue
        kind, exp, K = ric_parts(r["inst"])
        ts = bt.pd.Timestamp(r["t"])
        check(exp >= ts.date(), f"traded an expired contract {r['inst']}")
        w = weeks[str(exp)]
        chain = w["puts"] if kind == "P" else w["calls"]
        q = bt.quote(chain[K][1], ts)
        check(q is not None and q.BID - 1e-6 <= r["px"] <= q.ASK + 1e-6, f"fill outside bid/ask {r['inst']} {r['t']}")
        if r["side"] == "SELL":
            check(ts.hour >= 10, f"sold on the decision bar {r['t']}")

    # 4. outcomes agree with the official close; assignment trades happen at the strike
    for row in wk:
        if "K" not in row or "outcome" not in row:
            continue
        x = dt.date.fromisoformat(row["expiry"])
        close = float(D["close"][x])
        if row["outcome"].startswith("assigned"):
            check(row["kind"] == "P" and close < row["K"], f"bad put assignment {x}")
        if row["outcome"].startswith("called away"):
            check(row["kind"] == "C" and close > row["K"], f"bad call assignment {x}")
        if row["outcome"] == "expired":
            check((row["kind"] == "P" and close >= row["K"]) or (row["kind"] == "C" and close <= row["K"]),
                  f"expired but in the money {x}")
    for i, r in enumerate(bl):
        if r["side"] == "ASSIGN":
            nxt = bl[i + 1]
            check(nxt["inst"] == B["blotter"][0]["inst"] or nxt["side"] in ("BUY", "SELL"), "assignment without stock leg")
            check(abs(nxt["px"] - r["px"]) < 1e-6, f"assignment not at the strike {r['t']}")

    # 5. flat at the end and P&L = final NAV = final cash
    check(lg[-1]["shares"] == 0 and lg[-1]["opt_mark"] is None, "not flat at the end")
    check(abs(B["metrics"]["pnl"] - lg[-1]["nav"]) < 0.05 and abs(lg[-1]["nav"] - lg[-1]["cash"]) < 0.05, "P&L mismatch")
    # 6. attribution adds up
    check(abs(sum(B["metrics"]["attribution"].values()) - B["metrics"]["pnl"]) < 0.1, "attribution does not sum to P&L")

    print(f"{root} wheel ({key}): {n - len(fails)}/{n} checks passed")
    for f in fails[:20]:
        print("  FAIL", f)
    return not fails


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    sys.exit(0 if all([main(root, "base"), main(root, "roll")]) else 1)
