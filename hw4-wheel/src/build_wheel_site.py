"""
build_wheel_site.py -- data/wheel_*.json -> ../index.html (GitHub Pages) + CSVs.

    python3 build_wheel_site.py AAPL MP

A tabbed page: each section is its own view, picked from the bar at the top.
Every number is read from the books.
"""
import csv
import datetime as dt
import json
import math
import os
import sys

from build_site import money, pct, num, esc, table, CSS as BASE_CSS, JS as BASE_JS

NAMES = {"AAPL": "Apple (AAPL)", "MP": "MP Materials (MP)"}
TABS = [("summary", "Summary"), ("theory", "Thesis &amp; theory"), ("algorithm", "Algorithm"), ("results", "Results"),
        ("portfolio", "Portfolio on any date"), ("data", "Data &amp; fills"), ("runs", "All runs"), ("reproduce", "Reproduce")]


def csvs(root, W):
    os.makedirs("data", exist_ok=True)
    out = []
    for part in ("blotter", "ledger", "weeks"):
        rows = W["base"][part]
        keys = []
        for r in rows:
            keys += [k for k in r if k not in keys]
        path = f"data/wheel_{part}_{root}.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        out.append((part, path))
    rows = W["roll"]["blotter"]
    keys = list(rows[0])
    path = f"data/wheel_blotter_roll_{root}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    out.append(("blotter, roll variant", path))
    return out


def curve(ledger, cap):
    pts = {}
    for l in ledger:
        pts[l["d"]] = round(100 * l["nav"] / cap, 2)
    return [[d, v] for d, v in pts.items()]


def bands(W):
    """Date ranges when the wheel held shares (the covered-call phase)."""
    out, start = [], None
    for l in W["base"]["ledger"]:
        if l["shares"] and start is None:
            start = l["d"]
        if not l["shares"] and start is not None:
            out.append([start, l["d"]])
            start = None
    if start:
        out.append([start, W["base"]["ledger"][-1]["d"]])
    return out


def chart_data(W):
    m = W["base"]["metrics"]
    eq = [dict(name="Wheel", short="Wheel", slot="s1", pts=curve(W["base"]["ledger"], m["capital"])),
          dict(name="Covered call", short="Covered call", slot="s3",
               pts=curve(W["bench"]["cc"]["ledger"], W["bench"]["cc"]["metrics"]["capital"])),
          dict(name="Buy and hold", short="Buy & hold", slot="ref",
               pts=curve(W["bench"]["bh"]["ledger"], W["bench"]["bh"]["metrics"]["capital"]))]
    prem, pts = 0.0, []
    for w in W["base"]["weeks"]:
        prem += 100 * w.get("premium", 0)
        pts.append([w["expiry"], round(prem, 2)])
    st = [dict(name="Premium collected (puts and calls)", short="Premium", slot="s1", pts=pts)]
    view = [dict(d=l["d"], S=l["S"], cash=l["cash"], shares=l["shares"], basis=l["basis"], put=l["short_put"],
                 call=l["covered_call"], mark=l["opt_mark"], coll=l["collateral"], nav=l["nav"], eq=l["equity"])
            for l in W["base"]["ledger"]]
    trades = {}
    for r in W["base"]["blotter"]:
        trades.setdefault(r["t"][:10], []).append([r["t"][:16], r["action"], r["side"], r["inst"], r["qty"], r["px"], r["cash_flow"]])
    return dict(eq=eq, st=st, bands=bands(W), view=view, trades=trades, cap=m["capital"])


def summary_table(W):
    rows = []
    m, cc, bh = W["base"]["metrics"], W["bench"]["cc"]["metrics"], W["bench"]["bh"]["metrics"]
    for label, slot, x in (("Wheel", "s1", m), ("Covered call", "s3", cc), ("Buy and hold", "ref", bh)):
        rows.append([f'<span class="key {slot}"></span>{label}', money(x["capital"]), money(x["pnl"]), pct(x["ret"]),
                     pct(x["vol"]), pct(x["max_dd"]), num(x["sharpe"])])
    rows.append(["T-bills (3-month)", "&ndash;", "&ndash;", pct(W["tbill"]["ret"]), "&asymp; 0", "0", "&ndash;"])
    return table(["Strategy", "Capital", "P&amp;L", "Return on capital", "Volatility (ann.)", "Max drawdown", "Sharpe"], rows)


def week_table(W):
    rows = []
    for w in W["base"]["weeks"]:
        if "K" not in w:
            rows.append([w["expiry"], w["phase"], "&ndash;", "&ndash;", "&ndash;", "&ndash;", esc(w.get("note", "")), "&ndash;"])
            continue
        rows.append([w["expiry"], "put" if w["kind"] == "P" else "call", num(w["K"]), num(w["delta"], 2), pct(w["iv"]),
                     num(w["premium"]), esc(w.get("outcome", "")), num(w.get("S_close"))])
    return table(["Expiry", "Sold", "Strike", "&Delta;", "IV at sale", "Premium", "Outcome", "Close at expiry"], rows, "small")


def blotter_table(W, key="base"):
    rows = [[esc(r["t"][:16]), esc(r["action"]), r["side"], esc(r["inst"]), r["qty"], num(r["px"], 3), num(r["fee"]),
             num(r["cash_flow"]), num(r["cash"])] for r in W[key]["blotter"]]
    return table(["Time (ET)", "Action", "Side", "Instrument", "Qty", "Price", "Fee", "Cash flow", "Cash"], rows, "small")


def runs_table(W):
    base = W["base_params"]
    rows = []
    for r in W["runs"]:
        p, m = r["params"], r["metrics"]
        rows.append([num(p["put_delta"]), num(p["call_delta"]), num(p["h"], 1), "yes" if p["basis"] else "no",
                     "roll" if p["roll"] else "assign", "yes" if p["interest"] else "no",
                     money(m["capital"]), money(m["pnl"]), pct(m["ret"]), pct(m["vol"]), pct(m["max_dd"]), num(m["sharpe"]),
                     f"{m['puts_sold']}/{m['put_assigned']}", f"{m['calls_sold']}/{m['called_away']}"])
    return table(["Put |&Delta;|", "Call &Delta;", "Fill h", "Call &ge; basis", "ITM put", "Collateral interest", "Capital",
                  "P&amp;L", "Return", "Vol", "Max DD", "Sharpe", "Puts sold/assigned", "Calls sold/called"], rows, "small sens")


def find(W, **kw):
    for r in W["runs"]:
        if all(r["params"][k] == v for k, v in kw.items()) and all(
                r["params"][k] == W["base_params"][k] for k in W["base_params"] if k not in kw):
            return r["metrics"]
    return None


def results_section(root, W, links):
    m = W["base"]["metrics"]
    a = m["attribution"]
    att = table(["Puts (net premium)", "Calls (net premium)", "Shares (bought on assignment, sold when called)", "Dividends",
                 "Collateral interest", "Fees", "P&amp;L"],
                [[money(a["puts"]), money(a["calls"]), money(a["shares"]), money(a["dividends"]), money(a["interest"]),
                  money(a["fees"]), money(sum(a.values()))]])
    h5, h10, noint = find(W, h=0.5), find(W, h=1.0), find(W, interest=False)
    bh, cc = W["bench"]["bh"]["metrics"], W["bench"]["cc"]["metrics"]
    verdict = []
    verdict.append(f"Return: wheel {pct(m['ret'])} vs buy-and-hold {pct(bh['ret'])} and covered call {pct(cc['ret'])}: "
                   + ("<b>beat buy-and-hold</b>" if m["ret"] > bh["ret"] else "<b>lagged buy-and-hold</b>") + ".")
    verdict.append(f"Risk: volatility {pct(m['vol'])} vs {pct(bh['vol'])}; max drawdown {pct(m['max_dd'])} vs {pct(bh['max_dd'])}.")
    verdict.append(f"Sharpe: {num(m['sharpe'])} vs {num(bh['sharpe'])}: "
                   + ("<b>improved</b>" if (m["sharpe"] or -9) > (bh["sharpe"] or -9) else "<b>not improved</b>") + ".")
    verdict.append(f"The premium was rich: options sold at a mean implied volatility of {pct(m['iv_mean'])}; the stock then "
                   f"realized {pct(m['rv_mean'])} in those weeks; implied was above realized in {pct(m['iv_above_rv'], 0)} of them.")
    verdict.append(f"Fills: at the mid {pct(m['ret'])}; half the spread {pct(h5['ret'] if h5 else None)}; the full spread "
                   f"{pct(h10['ret'] if h10 else None)}. Without collateral interest: {pct(noint['ret'] if noint else None)}.")
    dl = " &middot; ".join(f'<a href="src/{p}">{part}.csv</a>' for part, p in links)
    return f"""
<div class="tab" data-root="{root}">
<h3>{NAMES[root]}: {W['window'][0]} to {W['window'][1]}</h3>
{summary_table(W)}
<ul class="notes">{''.join(f'<li>{v}</li>' for v in verdict)}</ul>
<figure class="chart" id="eq-{root}"></figure>
<p class="cap">Return on capital at each daily close. Shaded weeks: the wheel owned the shares (covered-call phase);
unshaded: it held cash behind a short put.</p>
<h4>Where the P&amp;L came from</h4>
{att}
<p class="small">{m['puts_sold']} puts sold ({m['put_assigned']} assigned) &middot; {m['calls_sold']} calls sold
({m['called_away']} called away) &middot; {m['expired']} expired worthless &middot; {m['weeks_holding']} of {m['weeks']} weeks
holding shares.</p>
<figure class="chart short" id="st-{root}"></figure>
<p class="cap">Cumulative premium collected: the staircase.</p>
<details><summary>Every week</summary>{week_table(W)}</details>
<details><summary>Blotter: every trade (opens, assignments, expiries, dividends, interest)</summary>{blotter_table(W)}</details>
<details><summary>Blotter, roll variant: an in-the-money put is closed on Friday and a new one sold Monday
({pct(W['roll']['metrics']['ret'])} return, {W['roll']['metrics']['rolled']} rolls)</summary>{blotter_table(W, "roll")}</details>
<p class="small">CSV: {dl} (the ledger CSV has the full portfolio every day)</p>
</div>"""


def theory(Ws):
    A = Ws.get("AAPL") or next(iter(Ws.values()))
    first = next(w for w in A["base"]["weeks"] if "K" in w)
    K, p, S = first["K"], first["premium"], first["S_sale"]
    rs = {r: W["base"]["metrics"] for r, W in Ws.items()}
    vrp_rows = [[NAMES[r], pct(m["iv_mean"]), pct(m["rv_mean"]), pct(m["iv_above_rv"], 0)] for r, m in rs.items()]
    return f"""
<h2>Thesis</h2>
<p class="lead">Options on these stocks are priced for more movement than the stocks deliver (implied volatility above
realized). The wheel is a seller of options every week, so it should collect that gap. It holds the stock only part of
the time and caps its upside, so it should be <b>less volatile than buy-and-hold with a similar return</b>: a higher
Sharpe ratio. It should lag in a strong rally (calls get called away) and still lose in a crash (puts get assigned on the
way down).</p>
<p>That is three testable claims: (1) implied &gt; realized on the options it sold; (2) lower volatility than holding;
(3) higher Sharpe. The <a href="#results">Results</a> tab checks each one.</p>

<h2>Theory</h2>
<h3>1. The four steps</h3>
<pre class="mermaid">stateDiagram-v2
  direction LR
  Cash: Cash, sell a put
  Shares: Own 100 shares, sell a call
  Cash --> Cash: put expires (keep premium)
  Cash --> Shares: put assigned (buy 100 at K)
  Shares --> Shares: call expires (keep premium)
  Shares --> Cash: call assigned (sell 100 at K)</pre>

<h3>2. The cash-secured put</h3>
<p>Sell one put with strike K for premium p and set aside the cash to buy the shares:</p>
$$\\text{{collateral}} = 100\\,K, \\qquad \\text{{P\\&L at expiry}} = 100\\,\\big[\\,p - \\max(K - S_T,\\,0)\\,\\big]$$
<p class="ex">AAPL, first week: stock {num(S)}, sold the {num(K)} put for {num(p)}: collateral {money(100 * K)}, premium
{money(100 * p)}, or {pct(p / K, 2)} of the collateral in one week. If AAPL closes Friday below {num(K)}, the wheel buys 100
shares at {num(K)}; its effective cost is K &minus; p = {num(K - p)}.</p>

<h3>3. Why a cash-secured put is a covered call in disguise</h3>
$$C - P = S - K e^{{-rT}} \\quad\\Longrightarrow\\quad \\underbrace{{K e^{{-rT}} - P}}_{{\\text{{cash + short put}}}} \\;=\\; \\underbrace{{S - C}}_{{\\text{{shares + short call}}}}$$
<p>Put-call parity says the two phases of the wheel have the same payoff. The wheel is a covered call that sometimes holds
cash instead of shares: after a call is assigned it re-enters through a put, buying back in below the current price if the
stock falls. Both phases are short an option, so the whole strategy is short volatility.</p>

<h3>4. Where the money comes from: implied vs realized</h3>
<p>A short option, hedged for direction, earns or loses each day</p>
$$d\\Pi \\;\\approx\\; \\tfrac12\\,\\Gamma\\,S^2\\left(\\sigma_{{\\text{{implied}}}}^2 - \\sigma_{{\\text{{realized}}}}^2\\right)dt$$
<p>&Gamma; is the option's gamma. If the stock moves less than the option price assumed (realized &lt; implied), the seller
keeps more premium than the moves cost. That gap is the <b>variance risk premium</b>: investors overpay for protection.
The wheel does not hedge its delta, so it also carries the stock's direction (about 0.3 of a share in the put phase, about
0.7 in the call phase).</p>
{table(["Stock", "Mean implied vol at sale", "Mean realized vol that week", "Weeks implied &gt; realized"], vrp_rows)}

<h3>5. Choosing the strike: delta</h3>
$$\\Delta_{{\\text{{put}}}} = e^{{-qT}}\\big(N(d_1) - 1\\big), \\qquad \\Delta_{{\\text{{call}}}} = e^{{-qT}}N(d_1), \\qquad d_1 = \\frac{{\\ln(S/K) + (r - q + \\tfrac12\\sigma^2)T}}{{\\sigma\\sqrt T}}$$
<p>Each Monday the algorithm sells the out-of-the-money option whose |&Delta;| is closest to 0.30, with &sigma; implied
from that option's own mid. A fixed delta keeps the chance of assignment roughly constant (around 25&ndash;30% a week)
whether the stock is at $40 or $340, which a fixed dollar distance would not. In this sample
{', '.join(f"{NAMES[r].split(' (')[0]} {pct((m['put_assigned'] + m['called_away']) / max(1, m['puts_sold'] + m['calls_sold']), 0)}" for r, m in rs.items())}
of the options sold were assigned.</p>

<h3>6. Capital and the collateral</h3>
<p><b>Capital</b> is the most cash the strategy ever needed: 100 &times; K while a put is open, the cost of the shares while
holding them, net of premiums collected. Return on capital = P&amp;L / capital. While a put is open the collateral sits in
3-month T-bills and earns their rate (Schwab flags this opportunity cost; brokers allow it); a variant without interest is
in <a href="#runs">All runs</a>.</p>
"""


def page(Ws):
    links = {r: csvs(r, W) for r, W in Ws.items()}
    data = {r: chart_data(W) for r, W in Ws.items()}
    W0 = next(iter(Ws.values()))
    bp = W0["base_params"]

    # summary bullets
    bullets = []
    for r, W in Ws.items():
        m, bh = W["base"]["metrics"], W["bench"]["bh"]["metrics"]
        bullets.append(f"<b>{NAMES[r]}</b>: wheel {money(m['pnl'])} on {money(m['capital'])} ({pct(m['ret'])}, Sharpe "
                       f"{num(m['sharpe'])}, max drawdown {pct(m['max_dd'], 0)}) vs buy-and-hold {pct(bh['ret'])} (Sharpe "
                       f"{num(bh['sharpe'])}, drawdown {pct(bh['max_dd'], 0)}).")
    claims = []
    iv_ok = all(W["base"]["metrics"]["iv_mean"] > W["base"]["metrics"]["rv_mean"] for W in Ws.values())
    vol_ok = all(W["base"]["metrics"]["vol"] < W["bench"]["bh"]["metrics"]["vol"] for W in Ws.values())
    sh_ok = [r for r, W in Ws.items() if (W["base"]["metrics"]["sharpe"] or -9) > (W["bench"]["bh"]["metrics"]["sharpe"] or -9)]
    claims.append(("Implied vol above realized on the options sold", "held on both stocks" if iv_ok else "did not hold on every stock"))
    claims.append(("Lower volatility than buy-and-hold", "held on both stocks" if vol_ok else "did not hold on every stock"))
    claims.append(("Higher Sharpe than buy-and-hold", "held on " + (", ".join(NAMES[r] for r in sh_ok) if sh_ok else "neither stock")))

    summary = f"""
<h2>Summary</h2>
<p class="lead">The wheel sells a cash-secured put each week; if assigned it owns the shares and sells covered calls until
they are called away, then starts over. Backtested on every weekly expiry from {W0['window'][0]} to {W0['window'][1]} with
LSEG option quotes, on a stock that rose (Apple) and one that fell (MP Materials).</p>
<div class="answer"><ul>{''.join(f'<li>{b}</li>' for b in bullets)}</ul></div>
{table(["Thesis claim", "Result"], [[a, b] for a, b in claims], "narrow")}
<div class="cards">{''.join(f'<a class="card" href="#{k}"><b>{v}</b></a>' for k, v in TABS[1:])}</div>
"""

    spec = table(["Item", "Rule"], [
        ["Weekly cycle", "every Monday, if no option is open: no shares &rarr; sell 1 put; 100 shares &rarr; sell 1 call"],
        ["Strike", f"out of the money, |&Delta;| closest to {bp['put_delta']} (puts) and {bp['call_delta']} (calls); &sigma; implied from the contract's own mid"],
        ["Expiry", "the same week's Friday (Thursday in holiday weeks); settled at the official close"],
        ["Assignment", "put: close below K &rarr; buy 100 at K; call: close above K &rarr; sell 100 at K. Both allowed: that is the wheel"],
        ["Clock", "decide on the 09:00 bar (quotes at ~10:00), fill on the next bar with a quote"],
        ["Fill", "mid of the real LSEG quote; tested at half and the full spread"],
        ["Costs", f"${bp['fee']:.2f} per option contract; $0 stock trades (assignment); dividends paid to the holder"],
        ["Collateral", "cash securing a put earns the 3-month T-bill rate (LSEG US3MT=RR)"],
        ["End", "shares still held are sold at the last close"],
        ["Benchmarks", "buy-and-hold 100 shares; the HW3 covered call (same delta rule, but it buys back in-the-money calls on Friday instead of being assigned); 3-month T-bills"],
        ["Roll variant", "on Friday's 14:00 bar, if the put is in the money, buy it back on the 15:00 bar (close) and sell a new put Monday (open): the shares are never taken"],
    ], "spec")
    algorithm = f"""
<h2>Algorithm</h2>
<pre class="mermaid">flowchart LR
  M["Monday 09:00 bar"] --> Q{{"Own shares?"}}
  Q -- no --> P["Sell put: OTM, |delta| ~0.30 (next bar)"]
  Q -- yes --> C["Sell call: OTM, delta ~0.30 (next bar)"]
  P --> FP{{"Friday close below K?"}}
  FP -- yes --> AP["Assigned: buy 100 at K"]
  FP -- no --> EP["Expires: keep premium"]
  C --> FC{{"Friday close above K?"}}
  FC -- yes --> AC["Called away: sell 100 at K"]
  FC -- no --> EC["Expires: keep premium and shares"]
  AP --> M
  EP --> M
  AC --> M
  EC --> M</pre>
{spec}"""

    tabs = "".join(f'<button data-root="{r}">{NAMES[r]}</button>' for r in Ws)
    results = f'<h2>Results</h2><div class="tabs">{tabs}</div>' + "".join(results_section(r, W, links[r]) for r, W in Ws.items())

    portfolio = f"""
<h2>Portfolio on any date</h2>
<p>Pick a stock and drag the slider (or type a date). The card shows the exact position at that day's close, from the
ledger; the table lists that day's trades from the blotter.</p>
<div class="tabs">{tabs.replace('data-root', 'data-pf')}</div>
<div class="pf">
  <div class="pfbar"><input type="range" id="pf-slider" min="0" max="0" value="0"><input type="date" id="pf-date"></div>
  <div id="pf-card" class="pfcard"></div>
  <div id="pf-trades"></div>
</div>"""

    data_rows = []
    for r, W in Ws.items():
        c, pc = W["checks"], W["put_checks"]
        data_rows.append([NAMES[r], "calls", f"{c['n']:,}", f"{c['slope']} &times; mid + {c['intercept']}", f"{c['r2']:.4f}", pct(c["weekly_spread_med"])])
        data_rows.append([NAMES[r], "puts", f"{pc['n']:,}", f"{pc['slope']} &times; mid + {pc['intercept']}", f"{pc['r2']:.4f}", pct(pc["spread_med"])])
    datasec = f"""
<h2>Data &amp; fills</h2>
<pre class="mermaid">flowchart LR
  A["fetch.py (HW3): stock, weekly calls, rates, dividends"] --> R[("raw_*.json")]
  B["fetch_puts.py: weekly puts, same weeks"] --> P[("puts_*.json")]
  R --> W["wheel.py"]
  P --> W
  W --> K[("wheel_*.json")] --> V["validate_wheel.py"]
  K --> S["build_wheel_site.py"] --> H["index.html"]</pre>
<p>Expired weekly options are not in any LSEG chain, so every contract is requested by its RIC, e.g.
<code>MPW072505200.U^K25</code>: MP, <b>W</b> = November put (puts use M&ndash;X), day 07, 2025, strike $52.00, expired
(<code>^K25</code>, always the call letter). Each week: the strikes from one above the Monday price down to &minus;8% (AAPL) or
&minus;15% (MP) for puts, and up to +8% / +15% for calls; hourly bid, ask and last trade, Monday to expiry.</p>
<h3>Fill assumption: the mid, and why</h3>
<p>For every hourly option bar that also printed a trade, the last trade regressed on the bar's mid:</p>
{table(["Stock", "Options", "Bars with a trade", "Last trade vs mid", "R&sup2;", "Median bid-ask spread (% of mid)"], data_rows)}
<p>Slope &asymp; 1, intercept &asymp; 0 and R&sup2; &gt; 0.99: trades print at the mid on average, so the mid is an unbiased fill.
It is not a guaranteed fill, so <a href="#runs">All runs</a> repeats the backtest paying half the spread and the full spread.
MP's spreads are wide (cheap options), which is where that matters.</p>
<h3>No look-ahead</h3>
<p>Each Monday's strike is chosen from that morning's quotes; the fill uses a later bar; the delta uses the prior day's
T-bill rate and trailing dividends; expiries come from the trading calendar (Thursday in Good Friday, Juneteenth and July 3
weeks). The two stocks were chosen by hand, which is the one hindsight choice.</p>
<h3>Validation</h3>
<p><code>validate_wheel.py</code> re-derives the book: cash reconciles to the blotter row by row; NAV equals cash + shares
&minus; the short option's mark; shares are only 0 or 100; a put is only open while flat and a call only while holding
(never naked); every option fill sits inside the quoted bid/ask of its bar on a contract whose RIC encodes that strike and
expiry; assignments match the official close and happen at the strike; the attribution sums to the P&amp;L.</p>"""

    runs = "<h2>All runs</h2><p class=\"small\">Every combination tried, nothing dropped. Fill h: 0 = mid, 0.5 = halfway to the far side, 1 = full spread.</p>" + \
           "".join(f"<h3>{NAMES[r]}</h3>" + runs_table(W) for r, W in Ws.items())

    reproduce = """
<h2>Reproduce</h2>
<pre>git clone https://github.com/felipejpenagos/fintech535 && cd fintech535/hw4-wheel/src
pip install numpy pandas                       # lseg-data only needed to re-fetch
python3 wheel.py AAPL && python3 wheel.py MP    # cached LSEG data -> books (seconds)
python3 validate_wheel.py AAPL && python3 validate_wheel.py MP
python3 build_wheel_site.py AAPL MP             # -> ../index.html

# re-fetch (LSEG Workspace open):
python3 fetch.py AAPL.O --step 2.5 --far-step 5 --otm 0.08 --leap-step 5 --leap-lo 0.5
python3 fetch_puts.py AAPL.O --step 2.5 --far-step 5 --otm 0.08
python3 fetch.py MP.N --step 0.5 --far-step 1 --otm 0.15 --leap-step 2.5
python3 fetch_puts.py MP.N --step 0.5 --far-step 1 --otm 0.15</pre>
<h3>Limits</h3>
<ul>
<li>One year, two stocks: one up path and one down path, not a distribution.</li>
<li>Assignment is modeled only at expiry; an in-the-money call can be exercised early before an AAPL ex-dividend date.</li>
<li>Fills are the quote at the end of the next hourly bar, not a guaranteed execution.</li>
<li>Delta comes from a European model on American options and is used only to rank strikes.</li>
<li>One contract: the capital follows the stock's price, so it is larger in AAPL's second half than its first.</li>
</ul>"""

    sections = dict(summary=summary, theory=theory(Ws), algorithm=algorithm, results=results, portfolio=portfolio,
                    data=datasec, runs=runs, reproduce=reproduce)
    nav = "".join(f'<a href="#{k}" data-page="{k}">{v}</a>' for k, v in TABS)
    body = "".join(f'<section class="page" id="page-{k}">{sections[k]}</section>' for k, _ in TABS)

    css = BASE_CSS + """
body{padding-top:0}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);margin:0 -20px 18px;padding:14px 20px 0}
.top h1{font-size:20px;margin:0 0 2px}.top .sub{font-size:13px;margin:0 0 8px}
nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}nav::-webkit-scrollbar{display:none}
nav a{padding:8px 12px;font-size:14px;color:var(--fg2);text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
nav a.on{color:var(--fg);border-bottom-color:var(--s2);font-weight:600}
.page{display:none}.page.on{display:block}.page h2:first-child{border-top:0;margin-top:8px;padding-top:0}
.lead{font-size:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:18px 0}
.card{border:1px solid var(--line);border-radius:10px;padding:14px;text-decoration:none;color:var(--fg)}.card:hover{border-color:var(--accent)}
.pfbar{display:flex;gap:12px;align-items:center;margin:12px 0}.pfbar input[type=range]{flex:1}
.pfbar input[type=date]{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.pfcard{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:10px 0}
.pfcard div{background:var(--panel);border-radius:8px;padding:10px 12px}.pfcard span{display:block;font-size:12px;color:var(--fg2)}
.pfcard b{font-size:17px}
"""
    js_charts = BASE_JS.split("function show(")[0].replace("__DATA__", json.dumps(data))
    js = js_charts + r"""
// shaded bands (weeks holding shares) drawn under the lines
const _draw = draw;
draw = function(el, series, unit, bands){
  if(!el || el.clientWidth < 100) return;
  _draw(el, series, unit);
  if(!bands || !bands.length) return;
  const svg = el.querySelector('svg'); const W=el.clientWidth, H=300, small=W<600, m={l:48,r:small?8:132,t:10,b:26};
  const xs=[...new Set(series.flatMap(s=>s.pts.map(p=>p[0])))].sort(); const xi=Object.fromEntries(xs.map((d,i)=>[d,i]));
  const X=i=>m.l+(W-m.l-m.r)*i/Math.max(1,xs.length-1);
  const ns='http://www.w3.org/2000/svg';
  bands.forEach(([a,b])=>{ if(!(a in xi)||!(b in xi)) return; const r=document.createElementNS(ns,'rect');
    r.setAttribute('x',X(xi[a])); r.setAttribute('width',Math.max(1,X(xi[b])-X(xi[a]))); r.setAttribute('y',m.t);
    r.setAttribute('height',H-m.t-m.b); r.setAttribute('fill',css('--panel')); svg.insertBefore(r, svg.firstChild); });
};
function showRoot(root){
  document.querySelectorAll('#page-results .tab').forEach(t=>t.classList.toggle('on',t.dataset.root===root));
  document.querySelectorAll('#page-results .tabs button').forEach(b=>b.setAttribute('aria-selected',b.dataset.root===root));
  draw(document.getElementById('eq-'+root),DATA[root].eq,'%',DATA[root].bands);
  draw(document.getElementById('st-'+root),DATA[root].st,'$');
}
document.querySelectorAll('#page-results .tabs button').forEach(b=>b.addEventListener('click',()=>showRoot(b.dataset.root)));
const fmt$=v=>(v<0?'−$':'$')+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:0});
let pfRoot=Object.keys(DATA)[0];
function pfRender(i){
  const v=DATA[pfRoot].view, L=v[i]; if(!L) return;
  document.getElementById('pf-date').value=L.d;
  const opt=L.put?`Short put ${L.put}`:(L.call?`Covered call ${L.call}`:'none');
  const cells=[['Date',L.d],['Stock close','$'+L.S.toFixed(2)],['Cash',fmt$(L.eq-(L.nav-L.cash))],
    ['Shares',L.shares+(L.basis?` (assigned at $${L.basis})`:'')],['Option open',opt],
    ['Option mark',L.mark==null?'–':'$'+L.mark.toFixed(2)+' x 100'],['Cash securing the put',L.coll?fmt$(L.coll):'–'],
    ['Account value',fmt$(L.eq)],['P&L to date',fmt$(L.nav)]];
  document.getElementById('pf-card').innerHTML=cells.map(([a,b])=>`<div><span>${a}</span><b>${b}</b></div>`).join('');
  const tr=DATA[pfRoot].trades[L.d]||[];
  document.getElementById('pf-trades').innerHTML=tr.length?'<div class="tw"><table class="small"><thead><tr><th>Time</th><th>Action</th><th>Side</th><th>Instrument</th><th>Qty</th><th>Price</th><th>Cash flow</th></tr></thead><tbody>'+
    tr.map(r=>'<tr>'+r.map(c=>`<td>${c}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>':'<p class="small">No trades this day.</p>';
}
function pfSet(root){
  pfRoot=root; const s=document.getElementById('pf-slider'); s.max=DATA[root].view.length-1;
  document.querySelectorAll('#page-portfolio .tabs button').forEach(b=>b.setAttribute('aria-selected',b.dataset.pf===root));
  pfRender(+s.value>+s.max?+s.max:+s.value);
}
document.getElementById('pf-slider').addEventListener('input',e=>pfRender(+e.target.value));
document.getElementById('pf-date').addEventListener('change',e=>{const v=DATA[pfRoot].view;let i=v.findIndex(l=>l.d>=e.target.value);
  if(i<0)i=v.length-1; document.getElementById('pf-slider').value=i; pfRender(i);});
document.querySelectorAll('#page-portfolio .tabs button').forEach(b=>b.addEventListener('click',()=>pfSet(b.dataset.pf)));
function route(){
  const k=(location.hash||'#summary').slice(1);
  document.querySelectorAll('.page').forEach(p=>p.classList.toggle('on',p.id==='page-'+k));
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.page===k));
  if(k==='results'){const on=document.querySelector('#page-results .tab.on');showRoot(on?on.dataset.root:Object.keys(DATA)[0]);}
  if(k==='portfolio') pfSet(pfRoot);
  if(window.mermaid) window.mermaid.run({querySelector:'#page-'+k+' pre.mermaid:not([data-processed])'}).catch(()=>{});
  window.scrollTo(0,0);
}
window.addEventListener('hashchange',route);
window.addEventListener('resize',()=>{if(location.hash==='#results'){const on=document.querySelector('#page-results .tab.on');if(on)showRoot(on.dataset.root);}});
route();
"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Wheel: a backtest on AAPL and MP</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
 onload="renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}}]}})"></script>
<style>{css}</style></head><body>
<header class="top"><h1>The Wheel: a backtest</h1>
<p class="sub">Apple and MP Materials &middot; weekly options, {W0['window'][0]} to {W0['window'][1]} &middot; LSEG data &middot; FINTECH 535 &middot; Felipe Jaramillo</p>
<nav>{nav}</nav></header>
{body}
<script>{js}</script>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
mermaid.initialize({{startOnLoad:false, theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'neutral'}});
window.mermaid = mermaid;
const k=(location.hash||'#summary').slice(1);
mermaid.run({{querySelector:'#page-'+k+' pre.mermaid'}});
</script>
</body></html>"""


def main(roots):
    Ws = {r: json.load(open(f"data/wheel_{r}.json")) for r in roots}
    html = page(Ws)
    open("../index.html", "w").write(html)
    print(f"wrote ../index.html ({len(html) / 1e3:.0f} kB)")


if __name__ == "__main__":
    main(sys.argv[1:] or ["AAPL", "MP"])
