<div align="center">

# FINTECH 535: Options Data and a Covered-Call Book

### Expired-options data from LSEG, an implied price surface, and a margin-aware covered-call backtest

[![Live: option surface](https://img.shields.io/badge/Live-Option%20surface%20lab-2EA44F?style=flat-square)](https://felipejpenagos.github.io/fintech535/hw1-option-surface/)
[![Live: covered call](https://img.shields.io/badge/Live-Covered--call%20book-2EA44F?style=flat-square)](https://felipejpenagos.github.io/fintech535/hw2-covered-call/)
[![Duke](https://img.shields.io/badge/Duke-FINTECH%20535-00539B?style=flat-square)](#about)
[![Data](https://img.shields.io/badge/Data-LSEG%20Workspace-001EFF?style=flat-square)](#how-it-is-built)
[![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)](#how-it-is-built)

**Felipe Jaramillo Penagos** · Duke University · Fall 2026

</div>

---

Two projects that pull historical option data from LSEG, reconstruct it into something tradable, and publish the results as static sites on GitHub Pages.

| Project | What it does | Live site |
|---|---|---|
| **[Option Surface Lab](hw1-option-surface/)** | Builds OPRA RICs for expired UUUU options, pulls their history from LSEG, renders 3D call and put price surfaces, and tests how far trade prints sit from the bid-ask midpoint. | [Open](https://felipejpenagos.github.io/fintech535/hw1-option-surface/) |
| **[Covered-Call Book](hw2-covered-call/)** | Buys 100 shares each Monday and writes that Friday's call on AAPL and MPWR, then keeps a full blotter, ledger, Reg T margin account, and NAV path through expiry. | [Open](https://felipejpenagos.github.io/fintech535/hw2-covered-call/) |

## How it is built

**Covered-call book**

```
hw2-covered-call/src/
├── fetch.py          the only module that calls LSEG; caches raw data to data/
├── rics.py           OPRA RIC construction and parsing
├── engine.py         position state machine, Reg T margin, fills (no LSEG import)
├── run_backtest.py   weekly loop, mid-versus-print regression, performance
├── validate.py       reconciliation checks on a finished book
├── build_site.py     static site generator
└── make_fixture.py   synthetic GBM and Black-Scholes data for testing offline
```

- **Testable without data access.** `engine.py` never imports LSEG, so the accounting is tested against hand-computed positions and synthetic fixtures. There are 58 engine checks and 34 RIC checks.
- **Two strike rules.** One picks the nearest out-of-the-money strike; the other targets a call delta. On 30 seeded synthetic paths, the delta rule's realized assignment rates (0.152, 0.306, 0.476) tracked their targets (0.15, 0.30, 0.45).
- **Validated before publishing.** `validate.py` checks that cash reconciles to the blotter, that no shares are sold before they are bought, that assignment happens only above the strike, and that NAV equals the sum of its parts.
- **Fill assumption tested, not assumed.** A regression of trade prints on midpoints shows, name by name, whether filling at the mid is defensible.

Details, including two places where the course handout's RIC rules disagree with LSEG's API, are in the [covered-call README](hw2-covered-call/src/README.md).

**Option Surface Lab**

`hw1-option-surface/src/option_surface_lab.py` parses RICs, fetches expired contracts in batches, reshapes them into a long table, and renders the call and put surfaces, the mid-versus-trade comparison, and sparsity statistics into a single `index.html`.

## Reproduce

Data pulls need an LSEG Workspace session. Everything downstream runs from the cache:

```bash
cd hw2-covered-call/src
python3 run_backtest.py AAPL --rule nearest_otm
python3 validate.py AAPL
python3 build_site.py AAPL
python3 test_engine.py && python3 test_rics.py
```

## About

Coursework for FINTECH 535, Advanced Design and Testing of Algorithmic Trading Systems, at Duke University.
