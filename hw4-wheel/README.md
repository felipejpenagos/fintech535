# HW4: Wheel strategy backtest (AAPL, MP)

Live page: https://felipejpenagos.github.io/fintech535/hw4-wheel/

Sell a weekly cash-secured put (|delta| ~0.30); if assigned, sell weekly covered calls (delta ~0.30) until called
away; repeat. Nov 2025 to Oct 2026 on expired-option quotes from LSEG Workspace (hourly), against buy-and-hold,
the HW3 covered call, and T-bills.

| File | What it does |
|---|---|
| `src/fetch.py` | Stock, weekly calls, LEAPs, rates, dividends -> `data/raw_{ROOT}.json` (from HW3) |
| `src/fetch_puts.py` | Weekly puts on the same weeks -> `data/puts_{ROOT}.json` |
| `src/bs.py` | Black-Scholes-Merton price, delta, implied vol (calls and puts) |
| `src/backtest.py` | Shared data loading, clock, fills; the HW3 benchmarks |
| `src/wheel.py` | The wheel and its parameter grid -> `data/wheel_{ROOT}.json` |
| `src/validate_wheel.py` | Re-derives cash, NAV, positions, fills and assignments from the raw data |
| `src/build_wheel_site.py` | Builds `index.html` (tabbed) and the CSV blotter/ledger |

```bash
cd src
python3 wheel.py AAPL && python3 wheel.py MP
python3 validate_wheel.py AAPL && python3 validate_wheel.py MP
python3 build_wheel_site.py AAPL MP
```
