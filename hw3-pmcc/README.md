# HW3: Poor Man's Covered Call backtest (AAPL, MP)

Live page: https://felipejpenagos.github.io/fintech535/hw3-pmcc/

Weekly ~0.30-delta calls sold against a Jan-2028 ~0.80-delta LEAP, Nov 2025 to Oct 2026, on expired-option
quotes from LSEG Workspace (hourly). Compared with a covered call and buy-and-hold on the same weeks.

## Files

| File | What it does |
|---|---|
| `src/probe.py` | Asks LSEG what exists: weekly expiries, strike grid, history depth, LEAPs |
| `src/fetch.py` | Pulls every quote used, once, into `src/data/raw_{ROOT}.json` (needs LSEG Workspace) |
| `src/rics.py` | OPRA RIC construction (from HW2) |
| `src/bs.py` | Black-Scholes-Merton price, delta, implied volatility |
| `src/backtest.py` | PMCC, covered call, buy-and-hold, and the full parameter grid -> `src/data/book_{ROOT}.json` |
| `src/validate.py` | Re-derives cash, NAV, Reg T, fills and outcomes from the raw data |
| `src/build_site.py` | Builds `index.html` and the CSV blotters/ledgers |

## Rerun (no LSEG needed)

```bash
cd src
python3 backtest.py AAPL && python3 backtest.py MP
python3 validate.py AAPL && python3 validate.py MP
python3 build_site.py AAPL MP
```

## Re-fetch (LSEG Workspace open)

```bash
cd src
python3 fetch.py AAPL.O --step 2.5 --far-step 5 --otm 0.08 --leap-step 5 --leap-lo 0.5
python3 fetch.py MP.N   --step 0.5 --far-step 1 --otm 0.15 --leap-step 2.5
```

LSEG keeps about a year of hourly option history, so a later re-fetch covers a later window.
