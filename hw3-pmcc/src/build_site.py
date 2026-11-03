"""
build_site.py -- turn data/book_*.json into ../index.html (GitHub Pages) and CSVs.

    python3 build_site.py AAPL MP

Every number on the page is read from the books, so the page cannot drift from
the backtest. Charts are plain SVG drawn by a small script; equations use KaTeX
and diagrams use Mermaid (both from a CDN).
"""
import csv
import datetime as dt
import html
import json
import math
import os
import sys

NAMES = {"AAPL": "Apple (AAPL)", "MP": "MP Materials (MP)"}
SHORT = {"pmcc": "PMCC textbook", "pmcc_none": "PMCC covering", "cc": "Covered call", "bh": "Buy & hold"}
STRATS = [  # key in book["base"], label, css series slot
    ("pmcc", "PMCC, textbook strike rule", "s1"),
    ("pmcc_none", "PMCC, covering rule only", "s2"),
    ("cc", "Covered call", "s3"),
    ("bh", "Buy and hold", "ref"),
]


def money(x, d=0):
    s = f"{abs(x):,.{d}f}"
    return f"&minus;${s}" if x < 0 else f"${s}"


def pct(x, d=1):
    if x is None:
        return "&ndash;"
    return f"{'&minus;' if x < 0 else ''}{abs(100 * x):.{d}f}%"


def num(x, d=2):
    return "&ndash;" if x is None else (f"&minus;{abs(x):,.{d}f}" if x < 0 else f"{x:,.{d}f}")


def esc(s):
    return html.escape(str(s))


def table(head, rows, cls=""):
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def write_csvs(root, book):
    os.makedirs("data", exist_ok=True)
    links = []
    for k, label, _ in STRATS:
        b = book["base"][k]
        for part in ("blotter", "ledger", "weeks"):
            rows = b[part]
            path = f"data/{part}_{root}_{k}.csv"
            keys = sorted({kk for r in rows for kk in r}, key=lambda c: list(rows[0]).index(c) if c in rows[0] else 99)
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(rows)
            links.append((k, part, path))
    return links


def series_for(book):
    """Return on capital (%) over time for each headline strategy."""
    out = []
    for k, label, slot in STRATS:
        b = book["base"][k]
        cap = b["metrics"]["capital"]
        pts, seen = [], set()
        for l in b["ledger"]:
            if l["d"] in seen:
                pts[-1] = [l["d"], round(100 * l["nav"] / cap, 2)]
                continue
            seen.add(l["d"])
            pts.append([l["d"], round(100 * l["nav"] / cap, 2)])
        out.append(dict(name=label, short=SHORT[k], slot=slot, pts=pts))
    return out


def staircase(book):
    """Cumulative premium collected and cumulative buyback cost, by week, for the covered call."""
    prem = buy = 0.0
    p, q = [], []
    for w in book["base"]["cc"]["weeks"]:
        prem += 100 * w.get("premium", 0)
        buy += 100 * w.get("buyback", 0)
        p.append([w["expiry"], round(prem, 2)])
        q.append([w["expiry"], round(buy, 2)])
    return [dict(name="Premium collected", short="Premium in", slot="s3", pts=p),
            dict(name="Paid to buy back", short="Buybacks out", slot="s3d", pts=q)]


def week_table(b):
    rows = []
    for w in b["weeks"]:
        if "K" not in w:
            rows.append([w["expiry"], "&ndash;", "&ndash;", "&ndash;", esc(w.get("note", "")), "&ndash;", "&ndash;"])
            continue
        rows.append([w["expiry"], num(w["K"]), num(w["delta"], 2), num(w["premium"]),
                     esc(w.get("outcome", "")), num(w.get("S_fri")), num(w.get("buyback")) if w.get("buyback") else "&ndash;"])
    return table(["Expiry", "Strike", "&Delta; at sale", "Premium", "Outcome", "Stock, Fri 14:00 bar", "Buyback"], rows, "small")


def blotter_table(b, n=None):
    rows = [[esc(r["t"][:16]), r["side"], esc(r["inst"]), r["qty"], num(r["px"], 3), num(r["fee"]), num(r["cash_flow"]), num(r["cash"])]
            for r in b["blotter"][:n]]
    return table(["Time (ET)", "Side", "Instrument (RIC)", "Qty", "Price", "Fee", "Cash flow", "Cash"], rows, "small")


def ledger_table(b):
    rows = []
    for l in b["ledger"]:
        if dt.date.fromisoformat(l["d"]).weekday() != 4 and l is not b["ledger"][-1]:
            continue                                   # Fridays only on the page; CSV has every day
        rows.append([l["d"], num(l["S"]), l["shares"], num(l["leap_mark"]) if l["leap_K"] else "&ndash;",
                     num(l["short_mark"]) if l["short_K"] else "&ndash;", num(l["cash"], 0), num(l["nav"], 0),
                     num(l["equity"], 0), num(l["init"], 0), num(l["avail"], 0), num(l["excess"], 0)])
    return table(["Date", "Stock", "Shares", "LEAP mark", "Short call", "Cash", "NAV", "Equity", "Reg T initial",
                  "Available", "Excess"], rows, "small")


def run_label(r, base):
    diff = {k: v for k, v in r["params"].items() if base[k] != v}
    names = {"leap_delta": "LEAP &Delta;", "short_delta": "short &Delta;", "h": "fill h", "floor": "rule",
             "leap_exp": "LEAP expiry"}
    return ", ".join(f"{names.get(k, k)} {v}" for k, v in diff.items())


def sens_table(book):
    base = book["base_params"]
    rule = {"paid": "textbook", "none": "covering only", "jump": "jump-safe"}
    rows = []
    for r in book["runs"]:
        p, m = r["params"], r["metrics"]
        if not m:
            continue
        kind = {"pmcc": "PMCC", "cc": "Covered call", "bh": "Buy and hold"}[r["kind"]]
        rows.append([kind, rule[p["floor"]] if r["kind"] == "pmcc" else "&ndash;", num(p["leap_delta"]) if r["kind"] == "pmcc" else "&ndash;",
                     num(p["short_delta"]) if r["kind"] != "bh" else "&ndash;", num(p["h"], 1),
                     p["leap_exp"] if r["kind"] == "pmcc" else "&ndash;",
                     money(m["capital"]), money(m["pnl"]), pct(m["ret"]), pct(m["max_dd"]), num(m["sharpe"]),
                     f'{m["written"]}/{m["weeks"]}' if r["kind"] != "bh" else "&ndash;"])
    return table(["Strategy", "Strike rule", "LEAP &Delta;", "Short &Delta;", "Fill h", "LEAP expiry", "Capital", "P&amp;L",
                  "Return", "Max DD", "Sharpe", "Calls written"], rows, "small sens")


def find_run(book, kind, **kw):
    for r in book["runs"]:
        if r["kind"] == kind and all(r["params"][k] == v for k, v in kw.items()):
            return r["metrics"]
    return None


def summary_rows(book):
    rows = []
    for k, label, slot in STRATS:
        m = book["base"][k]["metrics"]
        rows.append([f'<span class="key {slot}"></span>{label}', money(m["capital"]), money(m["pnl"]), pct(m["ret"]),
                     pct(m["max_dd"]), num(m["sharpe"]),
                     f'{m["written"]}/{m["weeks"]}' if k != "bh" else "&ndash;"])
    return rows


def attribution_table(book):
    rows = []
    for k, label, slot in STRATS:
        a = book["base"][k]["metrics"]["attribution"]
        rows.append([f'<span class="key {slot}"></span>{label}', money(a["long_leg"]), money(a["short_calls"]),
                     money(a["stock"]), money(a["dividends"]), money(a["fees"]), money(sum(a.values()))])
    return table(["Strategy", "Long leg (LEAP or shares)", "Weekly calls (net)", "Assignment trades", "Dividends", "Fees", "P&amp;L"],
                 rows)


def stock_section(root, book, links):
    c = book["checks"]
    pm, nr, cc, bh = (book["base"][k]["metrics"] for k in ("pmcc", "pmcc_none", "cc", "bh"))
    L = book["base"]["pmcc"]["leap"]
    first = next((w for w in book["base"]["pmcc"]["weeks"] if "K" in w), None)
    asg = [w for w in book["base"]["pmcc"]["weeks"] + book["base"]["pmcc_none"]["weeks"] if w.get("outcome") == "assigned"]
    regt = min(book["base"]["pmcc_none"]["ledger"], key=lambda l: l["avail"])
    h05 = find_run(book, "pmcc", floor="none", h=0.5, leap_delta=0.8, short_delta=0.3, leap_exp=book["base_params"]["leap_exp"])
    h10 = find_run(book, "pmcc", floor="none", h=1.0, leap_delta=0.8, short_delta=0.3, leap_exp=book["base_params"]["leap_exp"])
    notes = [
        f"The textbook rule allowed a weekly call in {pm['written']} of {pm['weeks']} weeks"
        + (f" (first one on {first['entry']})" if first else "")
        + f". It needs the strike at or above K<sub>L</sub> + D = {num(L['K'])} + {num(L['cost'])} = {num(L['K'] + L['cost'])}, "
        f"while the stock started at {num(L['S'])}.",
        f"With only the covering rule, the PMCC sold the same calls as the covered call whenever the stock was above the "
        f"LEAP strike: {nr['written']} weeks, {nr['bought_back']} bought back, {nr['assigned']} assigned.",
        f"Fills: at the mid the covering-rule PMCC returned {pct(nr['ret'])}; paying half the spread, {pct(h05['ret'] if h05 else None)}; "
        f"the full spread, {pct(h10['ret'] if h10 else None)}. Median weekly-call spread: {pct(c['weekly_spread_med'])} of the mid; "
        f"LEAP: {pct(c['leap_spread_med'])}.",
    ]
    if regt["avail"] < 0:
        notes.append(f"Reg T: on {regt['d']} an assignment left the account short 100 shares over the weekend; equity "
                     f"{money(regt['equity'])} against {money(regt['init'])} initial margin is a Reg T call "
                     f"(maintenance excess was {money(regt['excess'])}). The algorithm buys the shares back Monday morning.")
    dl = " &middot; ".join(f'<a href="src/{p}">{part} ({lab})</a>'
                           for k, part, p in links for kk, lab, _ in STRATS if kk == k)
    return f"""
<section class="tab" id="tab-{root}" data-root="{root}">
  <h3>{NAMES[root]}: {book['window'][0]} to {book['window'][1]}, {c['n_weeks']} weekly expiries</h3>
  {table(["Strategy", "Capital", "P&amp;L", "Return on capital", "Max drawdown", "Sharpe", "Calls written"], summary_rows(book))}
  <figure class="chart" id="eq-{root}" data-unit="%"></figure>
  <p class="cap">Return on capital, marked at each daily close. Capital differs by strategy, so the common base is the
  percent of what each one tied up.</p>
  <h4>Where the P&amp;L came from</h4>
  {attribution_table(book)}
  <ul class="notes">{''.join(f'<li>{n}</li>' for n in notes)}</ul>
  <figure class="chart short" id="st-{root}" data-unit="$"></figure>
  <p class="cap">The covered call's weekly-call program (the covering-rule PMCC sells the same calls while the stock is above
  its LEAP strike). Premium is a staircase that never steps down; buybacks are the cost of the weeks the stock ran
  through the strike.</p>
  <details><summary>Every week: PMCC, textbook rule</summary>{week_table(book['base']['pmcc'])}</details>
  <details><summary>Every week: PMCC, covering rule only</summary>{week_table(book['base']['pmcc_none'])}</details>
  <details><summary>Every week: covered call</summary>{week_table(book['base']['cc'])}</details>
  <details><summary>Blotter: PMCC, covering rule only (every fill)</summary>{blotter_table(book['base']['pmcc_none'])}</details>
  <details><summary>Ledger: PMCC, covering rule only (Fridays; NAV, Reg T)</summary>{ledger_table(book['base']['pmcc_none'])}</details>
  <p class="small">CSV, every row: {dl}</p>
</section>"""


def theory(books):
    A = books.get("AAPL") or next(iter(books.values()))
    L = A["base"]["pmcc"]["leap"]
    cc_entry = A["base"]["cc"]["entry"]
    S, K, C, d, iv = L["S"], L["K"], L["cost"], L["delta"], L["iv"]
    T = (dt.date.fromisoformat(L["exp"]) - dt.date.fromisoformat(L["t"][:10])).days / 365
    lam = d * S / C
    intr = S - K
    led = A["base"]["pmcc"]["ledger"]
    low = min((l for l in led if l["leap_mark"] is not None), key=lambda l: l["S"])
    ds, dc = low["S"] / S - 1, low["leap_mark"] / C - 1
    bbw = next(w for w in A["base"]["cc"]["weeks"] if w.get("outcome") == "bought back" and w.get("S_fri"))
    rate_note = "3-month T-bill"
    r_ = 0.0382
    carry = K * (1 - math.exp(-r_ * T))
    return f"""
<h3>2.1 Covered call</h3>
<p>Own 100 shares at S<sub>0</sub>, sell one weekly call with strike K<sub>s</sub> for premium p. At expiry the buyer
takes any gain above K<sub>s</sub>:</p>
$$\\text{{Capital}}_{{CC}} = 100\\,S_0, \\qquad \\text{{P\\&L}} = 100\\,\\big[\\min(S_T, K_s) - S_0 + p\\big]$$
<p class="ex">AAPL, 3 Nov 2025: 100 &times; {num(cc_entry['S'])} = {money(100 * cc_entry['S'])}.</p>

<h3>2.2 PMCC: replace the shares with a LEAP</h3>
<p>A LEAP is a call that expires a year or more out. Buy one deep in the money (strike K<sub>L</sub> well below S) and
sell the same weekly call against it. Its price is two pieces:</p>
$$\\text{{Capital}}_{{PMCC}} = 100\\,C_L, \\qquad C_L = \\underbrace{{(S-K_L)}}_{{\\text{{intrinsic}}}} + \\underbrace{{\\text{{time value}}}}_{{\\text{{the rent}}}}$$
<p class="ex">AAPL, 3 Nov 2025, S = {num(S)}: the Jan-2028 {num(K, 0)} call cost {num(C)} = {num(intr)} intrinsic +
{num(C - intr)} time value, so {money(100 * C)} instead of {money(100 * cc_entry['S'])} ({pct(C / cc_entry['S'], 0)} of the shares).
About {num(carry)} of that time value is interest: you pay K<sub>L</sub> in 2028, not today, worth
K<sub>L</sub>(1 &minus; e<sup>&minus;rT</sup>) = {num(K, 0)} &times; (1 &minus; e<sup>&minus;{r_} &times; {T:.2f}</sup>).</p>

<h3>2.3 Delta: how many shares the LEAP acts like</h3>
$$\\Delta = e^{{-qT}}N(d_1), \\qquad d_1 = \\frac{{\\ln(S/K) + (r - q + \\tfrac12\\sigma^2)\\,T}}{{\\sigma\\sqrt{{T}}}}$$
<p>N is the standard normal CDF: the area under the bell curve to the left of d<sub>1</sub>. d<sub>1</sub> is roughly a
z-score: how many units of &sigma;&radic;T the stock sits above the strike. q is the dividend yield, r the
{rate_note} rate. Delta is the option's price change per $1 in the stock, so 100&Delta; is its share-equivalent.</p>
<p class="ex">AAPL LEAP: &sigma; = {pct(iv)} (implied from its mid), T = {T:.2f} years, &Delta; = {num(d)}, so it moves like
{100 * d:.0f} shares. With a 0.30-delta weekly sold against it, the position is
100(&Delta;<sub>L</sub> &minus; &Delta;<sub>s</sub>) &asymp; {100 * d:.0f} &minus; 30 = {100 * d - 30:.0f} shares.</p>
<p>Delta changes with S, but how fast depends on &sigma;&radic;T, the denominator of d<sub>1</sub>. For this LEAP
&sigma;&radic;T = {iv * math.sqrt(T):.2f}, so a 5% stock move shifts d<sub>1</sub> by about 0.05/{iv * math.sqrt(T):.2f}
&asymp; {0.049 / (iv * math.sqrt(T)):.2f} and delta barely moves. A one-week call at the same volatility has
&sigma;&radic;T &asymp; {iv * math.sqrt(5 / 365):.3f}: the same 5% move swings it from near 0 to near 1.</p>

<h3>2.4 Leverage</h3>
$$\\frac{{dC_L}}{{C_L}} \\approx \\lambda\\,\\frac{{dS}}{{S}}, \\qquad \\lambda = \\frac{{\\Delta\\,S}}{{C_L}}$$
<p class="ex">AAPL: &lambda; = {num(d)} &times; {num(S)} / {num(C)} = {lam:.2f}. In the data, AAPL fell
{pct(abs(ds))} from 3 Nov 2025 to {low['d']}; the LEAP fell {pct(abs(dc))}, from {num(C)} to {num(low['leap_mark'])}. In dollars
the LEAP lost less than 100 shares would have (&Delta; &lt; 1); as a percent of its capital it lost {abs(dc / ds):.1f}&times; as
much (more than &lambda;, because implied volatility also fell).</p>

<h3>2.5 The strike rule</h3>
<p>Worst case at a weekly expiry: you are assigned at K<sub>s</sub> and exercise the LEAP to deliver the shares.
Per share:</p>
{table(["Step", "Cash"], [["Bought the LEAP", "&minus;D"], ["Sold the weekly call", "+p"], ["Assigned: sell at K<sub>s</sub>", "+K<sub>s</sub>"], ["Exercise the LEAP: buy at K<sub>L</sub>", "&minus;K<sub>L</sub>"]], "narrow")}
$$\\text{{P\\&L}} = K_s - K_L - D + p \\;\\ge\\; 0 \\;\\Longrightarrow\\; K_s \\ge K_L + D - p \\;\\;\\xrightarrow{{\\;p\\,\\to\\,0\\;}}\\;\\; K_s \\ge K_L + D$$
<p>The textbook version drops p, because premiums from earlier weeks may already have gone to buybacks.</p>
<p class="ex">AAPL: K<sub>L</sub> + D = {num(K, 0)} + {num(C)} = {num(K + C)}, which is {pct((K + C) / S - 1)} above the stock on
day one. No one-week call that far out pays anything, so this PMCC sold its first call only after AAPL rallied
(section 5). The rule makes the strategy a leveraged long call most of the time.</p>
<p>Two refinements. The cost D is sunk, so a forward-looking rule uses today's LEAP price C<sub>L</sub>. And you would
sell the LEAP rather than exercise it; deep in the money it is worth S &minus; K<sub>L</sub>e<sup>&minus;rT</sup>.
After a large rally the short call is worth S &minus; K<sub>s</sub>, so the week cannot lose money if</p>
$$K_s \\;\\ge\\; K_L e^{{-rT}} + C_L - p \\qquad \\text{{(jump-safe rule, tested in section 6)}}$$
<p>The minimal rule is the course's covering rule: a long call covers a short call only if
K<sub>L</sub> &le; K<sub>s</sub> and the LEAP expires later. Without it the position is a call spread that needs margin.</p>

<h3>2.6 Friday: buy back, don't get assigned</h3>
<p>Near Friday's close an in-the-money weekly is worth about its intrinsic value:</p>
$$C_s \\approx S - K_s \\quad\\Rightarrow\\quad \\text{{buyback cost}} \\approx \\text{{what assignment costs}}$$
<p>Same expected cost, but assignment adds risk for nothing: you are short 100 shares over the weekend (a Monday gap
hits you), that short needs Reg T margin, and AAPL call holders may exercise early ahead of an ex-dividend date.
So the algorithm buys back.</p>
<p class="ex">AAPL covered call, week of {bbw['entry']}: sold the {num(bbw['K'])} call for {num(bbw['premium'])}. Friday's
14:00 bar showed AAPL at {num(bbw['S_fri'])}; it bought the call back for {num(bbw['buyback'])}
&asymp; {num(bbw['S_fri'])} &minus; {num(bbw['K'])} = {num(bbw['S_fri'] - bbw['K'])}. The shares rose by the same amount, so the
week gave up the gain above the strike but did not lose money.</p>

<h3>2.7 Accounting</h3>
$$\\text{{NAV}} = \\text{{cash}} + 100\\,C_L - 100\\,C_s + n_{{\\text{{shares}}}}\\,S$$
<p>A short option's mark is subtracted. Reg T: long stock needs 50% initial and 25% maintenance margin; the LEAP is paid in
full; a covered short call adds nothing; short stock after an assignment needs 50% and 30%.
<b>Capital</b> is the most cash a strategy ever tied up (the long leg, plus any buybacks not yet paid for by premiums).
Return on capital = P&amp;L / capital.</p>
"""


MERMAID_LOOP = """flowchart LR
  A["Monday 09:00 bar"] --> B["Week 1: buy LEAP<br/>Jan 2028, delta ~0.80"]
  A --> C["Short shares from<br/>an assignment?<br/>buy 100 back"]
  B --> D["Sell 1 weekly call<br/>delta ~0.30, out of the money,<br/>passes the strike rule"]
  C --> D
  D --> E{"Friday 14:00 bar<br/>stock above strike?"}
  E -- yes --> F["Buy the call back<br/>on the 15:00 bar"]
  E -- no --> G{"Official close<br/>above strike?"}
  G -- no --> H["Expires worthless"]
  G -- yes --> I["Assigned:<br/>short 100 shares"]
  F --> A
  H --> A
  I --> A"""

MERMAID_PIPE = """flowchart LR
  P["probe.py: what LSEG has"] --> F["fetch.py: LSEG Workspace, one request per contract"]
  F --> R[("data/raw_*.json: every quote used")]
  R --> B["backtest.py: strategies + grid"]
  B --> K[("data/book_*.json")]
  K --> V["validate.py: re-derives the numbers"]
  K --> S["build_site.py"] --> H["index.html"]"""


CSS = """
:root{--bg:#fcfcfb;--fg:#0b0b0b;--fg2:#52514e;--fg3:#8a8984;--line:#e4e3df;--panel:#f4f3f0;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--ref:#8a8984;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--fg:#f0efec;--fg2:#c3c2b7;--fg3:#8f8e88;--line:#383835;--panel:#232321;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--ref:#8f8e88;--accent:#3987e5}}
*{box-sizing:border-box}html{background:var(--bg)}
body{margin:0 auto;max-width:980px;padding:28px 20px 80px;color:var(--fg);background:var(--bg);
font:15.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
h1{font-size:28px;line-height:1.2;margin:0 0 6px}h2{font-size:21px;margin:44px 0 10px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:17px;margin:26px 0 6px}h4{font-size:15px;margin:22px 0 6px}
p,li{color:var(--fg)}.sub{color:var(--fg2);margin:0 0 18px}.small{font-size:13px;color:var(--fg2)}
a{color:var(--accent)}code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel);padding:1px 5px;border-radius:4px}
pre{background:var(--panel);padding:12px 14px;border-radius:8px;overflow:auto;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.answer{background:var(--panel);border-radius:10px;padding:14px 18px;margin:16px 0}.answer li{margin:6px 0}
.ex{background:var(--panel);border-left:3px solid var(--accent);padding:8px 12px;border-radius:4px;font-size:14.5px}
.tw{overflow-x:auto;margin:10px 0}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--fg2);font-weight:600;font-size:12.5px}th:first-child,td:first-child{text-align:left}
table.small{font-size:12.5px}table.small td,table.small th{padding:4px 7px}table.narrow{width:auto}
table.sens td:nth-child(-n+2){text-align:left}
table.spec td{text-align:left;white-space:normal}table.spec td:first-child{white-space:nowrap;color:var(--fg2)}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;margin:0 8px 3px 0;background:var(--fg3)}
.key.s1{background:var(--s1)}.key.s2{background:var(--s2)}.key.s3{background:var(--s3)}
.key.ref{background:repeating-linear-gradient(90deg,var(--ref) 0 4px,transparent 4px 7px)}
.tabs{display:flex;gap:6px;margin:14px 0 4px}.tabs button{font:inherit;font-size:14px;padding:6px 14px;border:1px solid var(--line);
border-radius:999px;background:transparent;color:var(--fg2);cursor:pointer}.tabs button[aria-selected=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.tab{display:none}.tab.on{display:block}
.chart{margin:14px 0 2px;position:relative}.chart svg{display:block;width:100%;height:300px;overflow:visible}.chart.short svg{height:220px}
.cap{font-size:13px;color:var(--fg2);margin:2px 0 14px}.notes li{font-size:14px;margin:4px 0}
.tip{position:absolute;pointer-events:none;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 9px;
font-size:12.5px;box-shadow:0 2px 8px rgba(0,0,0,.08);white-space:nowrap;display:none;z-index:2}
.tip b{font-weight:600}.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12.5px;color:var(--fg2);margin:4px 0 0}
details{margin:8px 0;border:1px solid var(--line);border-radius:8px;padding:6px 12px}summary{cursor:pointer;font-size:14px;color:var(--fg2)}
.mermaid{background:var(--panel);border-radius:10px;padding:10px;margin:12px 0;text-align:center;overflow-x:auto}
@media (max-width:600px){.mermaid svg{min-width:720px}}
.katex-display{overflow-x:auto;overflow-y:hidden;padding:4px 0}
"""

JS = r"""
const DATA = __DATA__;
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}
function draw(el, series, unit){
  el.innerHTML='';
  const W=el.clientWidth, H=el.classList.contains('short')?220:300;
  const small=W<600, m={l:48,r:small?8:132,t:10,b:26};
  const all=series.flatMap(s=>s.pts); if(!all.length) return;
  const xs=[...new Set(all.map(p=>p[0]))].sort(); const xi=Object.fromEntries(xs.map((d,i)=>[d,i]));
  let lo=Math.min(0,...all.map(p=>p[1])), hi=Math.max(...all.map(p=>p[1]));
  const pad=(hi-lo)*0.06||1; lo-=pad; hi+=pad;
  const X=i=>m.l+(W-m.l-m.r)*i/Math.max(1,xs.length-1), Y=v=>m.t+(H-m.t-m.b)*(1-(v-lo)/(hi-lo));
  const ns='http://www.w3.org/2000/svg', svg=document.createElementNS(ns,'svg'); el.appendChild(svg);
  const mk=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e;};
  const fmt=v=>{v=Math.abs(v)<1e-9?0:v; return unit==='%'?(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(0)+'%':(v<0?'−$':'$')+Math.abs(Math.round(v)).toLocaleString();};
  const step=niceStep((hi-lo)/5);
  for(let v=Math.ceil(lo/step)*step; v<=hi; v+=step){
    mk('line',{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:css(Math.abs(v)<1e-9?'--fg3':'--line'),'stroke-width':1});
    const t=mk('text',{x:m.l-8,y:Y(v)+4,'text-anchor':'end','font-size':11,fill:css('--fg3')});t.textContent=fmt(v);}
  let lastM='';xs.forEach((d,i)=>{const mo=d.slice(0,7); if(mo!==lastM && (d.slice(5,7)%2===1||i===0)){lastM=mo;
    const t=mk('text',{x:X(i),y:H-6,'text-anchor':'middle','font-size':11,fill:css('--fg3')});
    t.textContent=new Date(d+'T12:00').toLocaleDateString('en-US',{month:'short',year:'2-digit'});} else if(mo!==lastM) lastM=mo;});
  const labels=[];
  series.forEach(s=>{
    const col=css(s.slot==='s3d'?'--s3':'--'+s.slot);
    const d=s.pts.map((p,k)=>(k?'L':'M')+X(xi[p[0]]).toFixed(1)+','+Y(p[1]).toFixed(1)).join('');
    mk('path',{d,fill:'none',stroke:col,'stroke-width':2,'stroke-linejoin':'round','stroke-dasharray':(s.slot==='ref'||s.slot==='s3d')?'5 4':''});
    const p=s.pts[s.pts.length-1]; labels.push({y:Y(p[1]),txt:s.short+' '+fmt(p[1]),col,x:X(xi[p[0]])});
  });
  labels.sort((a,b)=>a.y-b.y); for(let i=1;i<labels.length;i++) if(labels[i].y-labels[i-1].y<14) labels[i].y=labels[i-1].y+14;
  if(!small) labels.forEach(l=>{const t=mk('text',{x:l.x+6,y:l.y+4,'font-size':11.5,fill:css('--fg2')});t.textContent=l.txt;});
  const leg=document.createElement('div');leg.className='legend';
  leg.innerHTML=series.map(s=>`<span><span class="key ${s.slot==='s3d'?'ref':s.slot}" style="${s.slot==='s3d'?'background:repeating-linear-gradient(90deg,'+css('--s3')+' 0 4px,transparent 4px 7px)':''}"></span>${s.name}</span>`).join('');
  el.appendChild(leg);
  const tip=document.createElement('div');tip.className='tip';el.appendChild(tip);
  const cross=mk('line',{y1:m.t,y2:H-m.b,stroke:css('--fg3'),'stroke-width':1,visibility:'hidden'});
  const hit=mk('rect',{x:m.l,y:m.t,width:W-m.l-m.r,height:H-m.t-m.b,fill:'transparent'});
  const idx=series.map(s=>Object.fromEntries(s.pts.map(p=>[p[0],p[1]])));
  hit.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();const i=Math.round((e.clientX-r.left-m.l)/(W-m.l-m.r)*(xs.length-1));
    const k=Math.max(0,Math.min(xs.length-1,i)), d=xs[k]; cross.setAttribute('x1',X(k));cross.setAttribute('x2',X(k));cross.setAttribute('visibility','visible');
    tip.innerHTML='<b>'+d+'</b><br>'+series.map((s,j)=>idx[j][d]!==undefined?`<span class="key ${s.slot==='s3d'?'ref':s.slot}"></span>${s.name}: ${fmt(idx[j][d])}`:'').filter(Boolean).join('<br>');
    tip.style.display='block'; const tx=X(k)+12; tip.style.left=(tx+180>W?X(k)-190:tx)+'px'; tip.style.top='8px';});
  hit.addEventListener('mouseleave',()=>{tip.style.display='none';cross.setAttribute('visibility','hidden');});
}
function niceStep(x){const p=Math.pow(10,Math.floor(Math.log10(x)));const f=x/p;return (f<1.5?1:f<3?2:f<7?5:10)*p;}
function show(root){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.root===root));
  document.querySelectorAll('.tabs button').forEach(b=>b.setAttribute('aria-selected',b.dataset.root===root));
  draw(document.getElementById('eq-'+root),DATA[root].eq,'%'); draw(document.getElementById('st-'+root),DATA[root].st,'$');
}
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>show(b.dataset.root)));
let cur=Object.keys(DATA)[0]; show(cur);
window.addEventListener('resize',()=>{const on=document.querySelector('.tab.on');if(on)show(on.dataset.root);});
"""


def main(roots):
    books = {r: json.load(open(f"data/book_{r}.json")) for r in roots}
    links = {r: write_csvs(r, b) for r, b in books.items()}
    data = {r: dict(eq=series_for(b), st=staircase(b)) for r, b in books.items()}

    # the answer, from the numbers
    ans = []
    for r, b in books.items():
        pm, nr, cc, bh = (b["base"][k]["metrics"] for k in ("pmcc", "pmcc_none", "cc", "bh"))
        stock_move = bh["ret"]
        ans.append(f"<b>{NAMES[r]}</b> (buy-and-hold {pct(stock_move)}): covered call "
                   f"{money(cc['pnl'])} on {money(cc['capital'])} ({pct(cc['ret'])}); PMCC {money(nr['pnl'])} on "
                   f"{money(nr['capital'])} ({pct(nr['ret'])}) with the covering rule, {money(pm['pnl'])} "
                   f"({pct(pm['ret'])}) with the textbook rule. Max drawdown: covered call {pct(cc['max_dd'], 0)}, "
                   f"PMCC {pct(nr['max_dd'], 0)} / {pct(pm['max_dd'], 0)}.")
    a0 = books.get("AAPL")
    capshare = ", ".join(f"{NAMES[r].split(' (')[0]} {pct(b['base']['pmcc_none']['metrics']['capital'] / b['base']['cc']['metrics']['capital'], 0)}"
                         for r, b in books.items())
    answer = f"""
<div class="answer"><ul>
<li><b>Less capital, as advertised, but leveraged.</b> The PMCC tied up {capshare} of the covered call's capital.
A ~0.80-delta LEAP moves like 80 shares on 30&ndash;50% of the money, so returns and drawdowns on that capital are
larger in both directions.</li>
{''.join(f'<li>{x}</li>' for x in ans)}
<li><b>The strike rule decides what the strategy is.</b> The textbook rule (short strike &ge; LEAP strike + LEAP cost)
almost never allows a weekly call against a two-year LEAP, so that PMCC is mostly a leveraged long call: it won in AAPL's
rally and lost {pct(abs(books['MP']['base']['pmcc']['metrics']['ret'])) if 'MP' in books else ''} in MP's fall. With only
the covering rule it is an income strategy: in MP the weekly premium more than paid for the LEAP's loss at mid fills
(but not after paying half the spread); in AAPL's rally it gave up the upside every week the stock ran through the
strike.</li>
<li><b>Verdict:</b> a PMCC is not a cheaper covered call; it is a covered call on margin. Use it when you want covered-call
exposure on less cash and can stomach drawdowns of 20&ndash;80% of that cash. On a wide-spread name like MP the edge
depends on getting filled near the mid (section 5).</li>
</ul></div>"""

    c0 = next(iter(books.values()))
    facts = []
    for r, b in books.items():
        c = b["checks"]
        nleap = len(b["base"]["pmcc"]["leap"] and [1]) if b["base"]["pmcc"]["leap"] else 0
        facts.append([NAMES[r], f"{b['window'][0]} to {b['window'][1]}", c["n_weeks"], f"{c['n']:,}",
                      f"{c['slope']} &times; mid + {c['intercept']}", f"{c['r2']:.4f}", pct(c["weekly_spread_med"]),
                      pct(c["leap_spread_med"]), c["n_divs"]])
    facts_t = table(["Stock", "Window", "Weeks", "Option bars with a trade", "Last trade vs mid", "R&sup2;",
                     "Median weekly spread", "Median LEAP spread", "Dividends on file"], facts)

    bp = c0["base_params"]
    spec = table(["Item", "Rule"], [
        ["Long leg (PMCC)", f"1 call expiring {bp['leap_exp']}, delta closest to {bp['leap_delta']}, bought the first Monday and held"],
        ["Long leg (covered call)", "100 shares bought the first Monday; re-bought the Monday after any assignment"],
        ["Weekly call", f"every Monday: out of the money, delta closest to {bp['short_delta']}, passes the strike rule"],
        ["Strike rule (PMCC)", "textbook: K<sub>s</sub> &ge; K<sub>L</sub> + D &middot; covering only: K<sub>s</sub> &ge; K<sub>L</sub>"],
        ["Friday", "decide on the 14:00 bar (quotes at about 15:00): if the stock is above the strike, buy the call back on the 15:00 bar"],
        ["Expiry", "anything still open settles at the official close: assigned if the close is above the strike, else expires"],
        ["After an assignment", "PMCC: buy the 100 shorted shares back Monday; covered call: buy 100 shares again"],
        ["Delta", "Black-Scholes-Merton implied from each contract's own mid, r = 3-month T-bill (LSEG US3MT=RR), q = trailing dividends"],
        ["Fills", "the mid of a real LSEG quote on the bar after the decision; tested at mid + &frac12; and 1 &times; the half-spread"],
        ["Costs", f"${bp['fee']:.2f} per option contract, $0 per stock trade; dividends paid to holders and charged to shorts"],
        ["End", "everything sold at the last session's close (LEAP at its closing quote)"],
        ["Scorecard", "return on capital, judged with max drawdown and Sharpe (weekly, excess of T-bill), against the covered call and buy-and-hold on the same weeks"],
    ], "spec")

    tabs = "".join(f'<button data-root="{r}">{NAMES[r]}</button>' for r in books)
    sections = "".join(stock_section(r, b, links[r]) for r, b in books.items())
    sens = "".join(f"<h3>{NAMES[r]}</h3>" + sens_table(b) for r, b in books.items())

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Poor Man's Covered Call: a backtest on AAPL and MP</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
 onload="renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}}]}})"></script>
<style>{CSS}</style></head><body>
<h1>Poor Man's Covered Call: a backtest</h1>
<p class="sub">Apple and MP Materials, weekly calls against a Jan-2028 LEAP, {c0['window'][0]} to {c0['window'][1]}.
Data: LSEG Workspace (expired option quotes, hourly). FINTECH 535 &middot; Felipe Jaramillo.</p>

<h2>1. The answer</h2>
{answer}
{table(["", *[NAMES[r] for r in books]], [[lab] + [f"{money(b['base'][k]['metrics']['pnl'])} on {money(b['base'][k]['metrics']['capital'])} &middot; {pct(b['base'][k]['metrics']['ret'])}" for b in books.values()] for k, lab, _ in STRATS])}

<h2>2. The strategy, from first principles</h2>
{theory(books)}

<h2>3. The algorithm</h2>
<pre class="mermaid">{MERMAID_LOOP}</pre>
{spec}
<p class="small">Why a fixed delta rather than a fixed strike: delta pins the chance of the call finishing in the money,
so the rule means the same thing at $40 and at $340.</p>

<h2>4. Data and method</h2>
<pre class="mermaid">{MERMAID_PIPE}</pre>
<p>Expired options are not in any chain, so each contract is requested by its RIC (LSEG's code for one instrument):</p>
<pre>MP  H  14  26  05500  .U  ^H26
│   │  │   │   │      │   └ expired contract, Aug 2026 (always the call month letter)
│   │  │   │   └ strike $55.00 (strike x 100, 5 digits)
│   │  │   └ year 2026
│   │  └ day 14 (zero-padded)
│   └ month H = August call (A-L calls, M-X puts)
└ underlying</pre>
<p>Each week the fetch pulls every listed strike from one below the Monday price to +8% (AAPL) or +15% (MP), so the rule
can pick by delta without knowing the strike in advance. The LEAP chain is pulled at every listed strike.</p>
{facts_t}
<p><b>No look-ahead.</b> LSEG hourly bars are stamped at their start; a bar's bid/ask are the last quotes before it ends.
Every decision uses one bar and every fill uses a later bar. The LEAP and the weekly call are chosen from that Monday's
quotes; the floor rules use yesterday's LEAP close; deltas use the trailing dividend and the prior day's T-bill rate.
Expiries come from the real trading calendar (Good Friday, Juneteenth and July 3 weeks expire Thursday). The two stocks
were picked by hand, which is a hindsight choice; nothing else is.</p>
<p><b>Fill check.</b> Across every option bar that also printed a trade, the last trade regressed on the mid has slope
&asymp; 1, intercept &asymp; 0 and R&sup2; &gt; 0.99 (table above), so the mid is a fair fill on average. Spreads are what
the sensitivity tests.</p>
<p><b>Validation.</b> <code>validate.py</code> re-derives the books: cash reconciles to the blotter row by row, NAV equals
its parts, Reg T arithmetic, every option fill sits inside the quoted bid/ask of a later bar on a contract whose RIC
encodes that expiry and strike, outcomes agree with the tape (assigned only if the close is above the strike, bought back
only if the stock was above it), the LEAP always covers the short call, and every position is flat at the end. All
checks pass on both stocks.</p>

<h2>5. Results</h2>
<div class="tabs">{tabs}</div>
{sections}

<h2>6. Every run (nothing dropped)</h2>
<p class="small">Fill h: 0 = mid, 0.5 = halfway to the far side, 1 = pay the full spread. Rule: textbook
K<sub>s</sub> &ge; K<sub>L</sub> + D; covering only K<sub>s</sub> &ge; K<sub>L</sub>; jump-safe
K<sub>s</sub> &ge; K<sub>L</sub>e<sup>&minus;rT</sup> + C<sub>L</sub> &minus; p.</p>
{sens}

<h2>7. Reproduce</h2>
<pre>git clone https://github.com/felipejpenagos/fintech535 && cd fintech535/hw3-pmcc/src
pip install numpy pandas                     # lseg-data only needed to re-fetch
python3 backtest.py AAPL && python3 backtest.py MP      # cached LSEG data -> books (seconds)
python3 validate.py AAPL && python3 validate.py MP
python3 build_site.py AAPL MP                           # -> ../index.html

# re-fetch from LSEG (Workspace open; ~25 min per stock):
python3 probe.py AAPL.O
python3 fetch.py AAPL.O --step 2.5 --far-step 5 --otm 0.08 --leap-step 5 --leap-lo 0.5
python3 fetch.py MP.N   --step 0.5 --far-step 1 --otm 0.15 --leap-step 2.5</pre>
<p class="small">LSEG keeps about one year of hourly history, so a re-fetch later will cover a later window; the cached
<code>raw_*.json</code> files in the repo are what this page was built from.</p>

<h2>8. Limits</h2>
<ul>
<li>One year and two stocks: one rising path and one falling path, not a distribution.</li>
<li>Hourly bars: the fill is the quote at the end of the next bar, not a guaranteed execution at that price.</li>
<li>Early exercise is modeled only at expiry; a holder could exercise an in-the-money AAPL call the day before an
ex-dividend date. The Friday buy-back limits this but does not rule it out.</li>
<li>Delta comes from a European model on American options; it is used only to rank strikes, never to price fills.</li>
<li>Margin is Reg T as taught in the course; a broker's house rules or portfolio margin would differ.</li>
<li>One LEAP held all year, no rolls; a LEAP that falls far out of the money (MP) is not replaced.</li>
</ul>

<script>{JS.replace('__DATA__', json.dumps(data))}</script>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
mermaid.initialize({{startOnLoad:true, theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'neutral'}});
</script>
</body></html>"""
    open("../index.html", "w").write(page)
    print("wrote ../index.html", f"({len(page) / 1e3:.0f} kB)")


if __name__ == "__main__":
    main(sys.argv[1:] or ["AAPL", "MP"])
