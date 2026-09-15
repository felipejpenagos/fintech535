# Covered call backtest — FINTECH 535

Buy 100 shares Monday, write that Friday's call, wait through expiry. Publishes
a blotter, ledger, Reg T account, NAV path and a mid-versus-print regression as
a static site for GitHub Pages.

## Running it

Four steps. Only the first touches LSEG.

```bash
# 1. pull the tape (Workspace must be open and signed in)
python3 fetch.py AAPL.O --weeks 10 --strike-step 5
python3 fetch.py MPWR.O --weeks 10 --strike-step 10

# 2. run the backtest (no API calls — iterate freely)
python3 run_backtest.py AAPL --rule nearest_otm
python3 run_backtest.py MPWR --rule nearest_otm

# 3. check the book before trusting it
python3 validate.py AAPL MPWR

# 4. build the site
python3 build_site.py AAPL MPWR
```

Then copy `site/index.html`, `site/book.js` and `site/data.html` to the repo
root and push.

The fetch caches to `data/raw_{ROOT}.json`, so steps 2–4 can run as many times
as you like without spending more of the daily request budget. LSEG enforces
one, and a wide strike grid across ten weeks burns through it quickly.

### Strike rules

```bash
python3 run_backtest.py AAPL --rule nearest_otm          # lowest strike at or above spot
python3 run_backtest.py AAPL --rule delta --target-delta 0.30
```

The delta rule needs `--want-deltas` on the fetch, which adds an IPA call per
batch of strikes. Delta is roughly the chance of finishing in the money, so
targeting it fixes assignment probability week to week; a fixed dollar distance
does not, and quietly takes more risk in volatile weeks.

If deltas do not come back, the delta rule falls back to nearest-OTM for that
week rather than skipping it.

## Testing

```bash
python3 test_engine.py    # 58 checks on the accounting
python3 test_rics.py      # 34 checks on RIC construction
```

The engine has no LSEG dependency, so it is tested against hand-computed
positions and against synthetic fixtures:

```bash
python3 make_fixture.py FAKE --spot 190 --vol 0.28 --liquidity high
python3 run_backtest.py FAKE --rule delta --target-delta 0.30
```

Stock follows GBM, options are Black-Scholes with a spread that widens as
liquidity drops. Across 30 seeded paths the delta rule delivered realised
assignment rates of 0.152, 0.306 and 0.476 against targets of 0.15, 0.30 and
0.45 — the strike selection does what it claims.

## Three things that will bite you

**The expired-contract suffix uses call letters even for puts.** The handout
says the suffix repeats the month letter, which is only true for calls. LSEG's
own reference implementation keeps a separate `exp` key that is always the A–L
series:

```
AAPLM212216000.U^A22    body M = January put, suffix A = January
```

Using `^M22` there returns "universe is not found". Verified against the API:
for UUUU June puts, `^R26` fails and `^F26` returns data. Homework 1's starter
code has this bug, so anyone who used it unmodified got zero puts and probably
concluded that puts do not trade on that name.

**The day IS zero-padded, whatever the handout says.** The spec says "5 not
05". Verified against the API on AAPL at the 335 strike:

```
AAPLH72633500.U^H26   not found      AAPLH072633500.U^H26   20 rows
AAPLI42633500.U^I26   not found      AAPLI042633500.U^I26   20 rows
```

Every Friday in a 14-week window resolved on the padded form; none of the
single-digit days resolved unpadded. `rics.py` pads.

**Two Fridays have no weekly at all**, and they are market holidays: 19 June
(Juneteenth) and 3 July (Independence Day observed). This is why expiries are
derived from the last session actually present in the stock tape rather than
from the calendar — a calendar walk would generate RICs for days the market
never opened.

**The Content layer will not take an interval string.** `get_history` accepts
`interval="hourly"`; `historical_pricing.summaries.Definition` raises
`AttributeError` and wants `Intervals.HOURLY`. `fetch.py` maps between them.

**The strike field is five characters, but not always five digits.** Below
$1,000 it is strike × 100 zero-padded. From 1,000 to 9,999 it is the integer
part plus a `0`, and the body month letter goes lowercase. Above that it is a
letter prefix plus four digits. The "always five digits" rule breaks silently
on index options and high-priced names.

## Layout

```
engine.py         position state machine, Reg T, fills — no LSEG import
rics.py           OPRA RIC construction and parsing
fetch.py          the only file that talks to LSEG
run_backtest.py   weekly loop, regression, performance
validate.py       logical-consistency checks on a built book
build_site.py     static site generator
make_fixture.py   synthetic data for testing without the API
```

`engine.py` deliberately knows nothing about LSEG. That is what makes the
accounting testable without an entitlement, and it is why the 58 engine checks
can run anywhere.

## Before pushing

`validate.py` checks what the rubric calls logical consistency: cash that
reconciles to the blotter, no shares sold that were never bought, no more than
one short call open, assignment only when the close is above the strike, NAV
equal to its parts, margin at the right rates, fills at the limit. It also
warns when the regression suggests the midpoint is not a safe fill assumption
on that name, and when available funds go negative — in which case the site
has to say the trade could not have been put on.
