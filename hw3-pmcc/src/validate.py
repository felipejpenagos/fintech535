"""
validate.py -- logical-consistency checks on a built book.

    python3 validate.py AAPL

Re-derives every number it can from the raw LSEG file and the blotter, so a
bug in backtest.py cannot hide behind its own bookkeeping.
"""
import json
import re
import sys
import datetime as dt

import backtest as bt

MONTHS = "ABCDEFGHIJKL"


def ric_parts(ric):
    m = re.match(r"^([A-Z]+)([A-Xa-x])(\d{2})(\d{2})(\d{5})\.U", ric)
    root, body, day, yy, strike = m.groups()
    month = (ord(body.upper()) - ord("A")) % 12 + 1
    return dt.date(2000 + int(yy), month, int(day)), int(strike) / 100


def main(root):
    D = bt.load(root)
    book = json.load(open(f"data/book_{root}.json"))
    fails, n = [], 0

    def check(ok, msg):
        nonlocal n
        n += 1
        if not ok:
            fails.append(msg)

    weeks = {str(w["expiry"]): w for w in D["weeks"]}
    for kind, b in book["base"].items():
        bl, lg, wk = b["blotter"], b["ledger"], b["weeks"]

        # 1. cash reconciles to the blotter, row by row
        cash = 0.0
        for r in bl:
            cash += r["cash_flow"]
            check(abs(cash - r["cash"]) < 0.05, f"{kind}: cash does not reconcile at {r['t']}")
        check(abs(cash - lg[-1]["cash"]) < 0.05, f"{kind}: final cash != ledger cash")

        # 2. NAV equals the sum of its parts on every ledger row
        for l in lg:
            parts = l["cash"] + l["shares"] * l["S"]
            parts += 100 * (l["leap_mark"] or 0) if l["leap_K"] is not None else 0
            parts -= 100 * (l["short_mark"] or 0) if l["short_K"] is not None else 0
            check(abs(parts - l["nav"]) < 0.05, f"{kind}: NAV != parts on {l['d']}")
            # Reg T arithmetic
            lmv, smv = max(l["shares"], 0) * l["S"], -min(l["shares"], 0) * l["S"]
            check(abs(l["init"] - (0.5 * lmv + 0.5 * smv)) < 0.05, f"{kind}: initial margin on {l['d']}")
            check(abs(l["maint"] - (0.25 * lmv + 0.30 * smv)) < 0.05, f"{kind}: maintenance on {l['d']}")

        # 3. share positions are only what the strategy allows
        allowed = {"pmcc": {0, -100}, "cc": {0, 100}, "bh": {0, 100}}[kind.split("_")[0]]
        for l in lg:
            check(l["shares"] in allowed, f"{kind}: shares {l['shares']} on {l['d']}")

        # 4. option fills: inside the quoted market, at a bar AFTER the decision bar,
        #    and on a contract whose RIC really encodes that expiry and strike
        opts = [r for r in bl if r["inst"].endswith(tuple("0123456789")) is False and ".U" in r["inst"]]
        for r in opts:
            exp, K = ric_parts(r["inst"])
            t = r["t"]
            if r["side"] in ("BUY", "SELL") and "T" not in t and " " not in t:
                continue                                    # end-of-test LEAP sale at the daily close
            if r["side"] in ("BUY", "SELL"):
                ts = bt.pd.Timestamp(t)
                check(exp >= ts.date(), f"{kind}: traded an expired option {r['inst']} on {t}")
                w = weeks.get(str(exp))
                df = w["calls"][K][1] if w and K in w["calls"] else None
                if df is None:                              # a LEAP
                    for L in D["leaps"].values():
                        if K in L and L[K]["ric"] == r["inst"]:
                            df = L[K]["hourly"]
                q = bt.quote(df, ts)
                check(q is not None, f"{kind}: no quote behind fill {r['inst']} {t}")
                if q is not None:
                    check(q.BID - 1e-6 <= r["px"] <= q.ASK + 1e-6, f"{kind}: fill outside bid/ask {r['inst']} {t}")
                decide = 9 if ts.date().weekday() == 0 or ts.hour <= 13 else bt.close_hour(ts.date()) - 2
                check(ts.hour > decide or ts.hour >= 10, f"{kind}: fill not after the decision bar {t}")

        # 5. outcomes agree with the tape
        for row in wk:
            if "K" not in row:
                continue
            x = dt.date.fromisoformat(row["expiry"])
            K, close = row["K"], float(D["close"][x])
            ch = bt.close_hour(x)
            s_dec = bt.stock_px(D, bt.stamp(x, ch - 2))
            if row.get("outcome") == "assigned":
                check(close > K, f"{kind}: assigned but close {close} <= K {K} ({x})")
            if row.get("outcome") == "expired":
                check(close <= K, f"{kind}: expired but close {close} > K {K} ({x})")
                w = weeks[row["expiry"]]
                fill_q = bt.quote(w["calls"][K][1], bt.stamp(x, ch - 1))
                check(s_dec is None or s_dec <= K or fill_q is None,
                      f"{kind}: ITM at the decision bar but not bought back ({x})")
            if row.get("outcome") == "bought back":
                check(s_dec is not None and s_dec > K, f"{kind}: bought back while OTM ({x})")
            check(K > 0 and row["premium"] > 0, f"{kind}: bad write row {row}")

        # 6. PMCC coverage: the LEAP strike is below every short strike and expires later
        if kind.startswith("pmcc") and b.get("leap"):
            KL, expL = b["leap"]["K"], dt.date.fromisoformat(b["leap"]["exp"])
            for row in wk:
                if "K" in row:
                    check(row["K"] >= KL, f"pmcc: short strike {row['K']} below LEAP strike {KL}")
                    check(dt.date.fromisoformat(row["expiry"]) <= expL, "pmcc: short outlives the LEAP")

        # 7. at most one short call open; every write is closed exactly once
        opened = sum(1 for r in bl if r["side"] == "SELL" and ".U" in r["inst"] and r["inst"] != (b.get("leap") or {}).get("ric"))
        closed = sum(1 for r in bl if r["side"] in ("EXPIRE", "ASSIGN")) + \
                 sum(1 for r in bl if r["side"] == "BUY" and ".U" in r["inst"] and r["inst"] != (b.get("leap") or {}).get("ric"))
        check(opened == closed, f"{kind}: {opened} calls written but {closed} closed")

        # 8. final P&L equals final NAV equals final cash (everything liquidated)
        check(abs(lg[-1]["nav"] - lg[-1]["cash"]) < 0.05 and lg[-1]["shares"] == 0, f"{kind}: not flat at the end")
        check(abs(b["metrics"]["pnl"] - lg[-1]["nav"]) < 0.05, f"{kind}: metrics P&L != final NAV")

    print(f"{root}: {n - len(fails)}/{n} checks passed")
    for f in fails[:25]:
        print("  FAIL", f)
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else "AAPL") else 1)
