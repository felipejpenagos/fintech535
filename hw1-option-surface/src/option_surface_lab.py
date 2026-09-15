"""
Assignment 1.1 - Option Surface Lab
Fetches expired-options data from LSEG, parses RICs, builds a 3D price surface,
compares MID_PRICE vs TRDPRC_1, computes sparsity stats, and renders a single
static index.html suitable for GitHub Pages.

Run this once locally (with LSEG Workspace open and logged in) to (re)generate
the cache and the HTML. Then push index.html to your repo root.
"""

import os
import re
import pickle
import datetime
import warnings

import numpy as np
import pandas as pd
import lseg.data as ld
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

warnings.filterwarnings("ignore", category=FutureWarning, module="lseg.data")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
TICKER_STOCK = "UUUU.K"
TICKER_ROOT = "UUUU"
WEEKS_BACK = 12
STRIKE_STEP = 0.50
BATCH_SIZE = 25
CACHE_FILE = "option_pipeline_data.pkl"
OUTPUT_HTML = "index.html"

CALL_CODES = "ABCDEFGHIJKL"   # A-L = Jan-Dec calls
PUT_CODES = "MNOPQRSTUVWX"    # M-X = Jan-Dec puts

# Monokai color scheme
BG_COLOR = "#272822"
PANEL_COLOR = "#3E3D32"
GRID_COLOR = "#49483E"
TEXT_COLOR = "#F8F8F2"
CALL_COLOR = "#A6E22E"   # monokai green
PUT_COLOR = "#F92672"    # monokai pink
ACCENT_COLOR = "#66D9EF"  # monokai blue


# -----------------------------------------------------------------------------
# RIC parsing (Appendix A scheme)
# {ROOT}{M}{DD}{YY}{SSSSS}.U^{M}{YY}
# -----------------------------------------------------------------------------
def parse_option_ric(ric: str):
    """
    Parse a synthetic OPRA-style RIC into its components.
    Returns a dict {underlying, expiry (date), type ('call'/'put'), strike (float)}
    or None if the RIC does not match the expected pattern.
    """
    m = re.match(r"^([A-Z]+)([A-X])(\d{2})(\d{2})(\d{5})\.U\^([A-X])(\d{2})$", ric)
    if not m:
        return None

    root, month_code, day, yr, strike_str, suffix_code, suffix_yr = m.groups()

    if month_code in CALL_CODES:
        opt_type = "call"
        month_num = CALL_CODES.index(month_code) + 1
    elif month_code in PUT_CODES:
        opt_type = "put"
        month_num = PUT_CODES.index(month_code) + 1
    else:
        return None

    try:
        year = 2000 + int(yr)
        expiry = datetime.date(year, month_num, int(day))
    except ValueError:
        return None

    strike = int(strike_str) / 100.0

    return {
        "ric": ric,
        "underlying": root,
        "expiry": expiry,
        "type": opt_type,
        "strike": strike,
    }


# -----------------------------------------------------------------------------
# Data pipeline
# -----------------------------------------------------------------------------
def load_or_fetch_pipeline_data(
    ticker_stock: str = TICKER_STOCK,
    ticker_root: str = TICKER_ROOT,
    weeks_back: int = WEEKS_BACK,
    strike_step: float = STRIKE_STEP,
    batch_size: int = BATCH_SIZE,
) -> dict:
    if os.path.exists(CACHE_FILE):
        print(f"Loading cached dataset from {CACHE_FILE}...")
        with open(CACHE_FILE, "rb") as f:
            return pickle.load(f)

    print("Cache not found. Initializing LSEG data pull...")
    ld.open_session()

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(weeks=weeks_back)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # 1. Underlying stock OHLC
    df_stock = ld.get_history(
        universe=[ticker_stock],
        fields=["OPEN_PRC", "HIGH_1", "LOW_1", "TRDPRC_1"],
        start=start_str,
        end=end_str,
        interval="daily",
    )

    low_price = float(df_stock["LOW_1"].min())
    high_price = float(df_stock["HIGH_1"].max())

    # 2. Candidate expired OPRA RICs
    min_strike = np.floor(low_price / strike_step) * strike_step
    max_strike = np.ceil(high_price / strike_step) * strike_step
    strikes = np.arange(min_strike, max_strike + strike_step, strike_step)
    friday_dates = pd.date_range(start=start_str, end=end_str, freq="W-FRI")

    candidate_rics = []
    for dt in friday_dates:
        year_str = dt.strftime("%y")
        day_str = dt.strftime("%d")
        month_num = dt.month
        call_code = CALL_CODES[month_num - 1]
        put_code = PUT_CODES[month_num - 1]

        for strike in strikes:
            strike_str = f"{int(round(strike * 100)):05d}"
            call_base = f"{ticker_root.upper()}{call_code}{day_str}{year_str}{strike_str}.U"
            candidate_rics.append(f"{call_base}^{call_code}{year_str}")

            # NOTE: the expired-contract caret suffix always uses the A-L month
            # series, even for puts. Only the RIC body uses the M-X put codes.
            # Using ^{put_code} here silently returns "universe is not found"
            # for every put.
            put_base = f"{ticker_root.upper()}{put_code}{day_str}{year_str}{strike_str}.U"
            candidate_rics.append(f"{put_base}^{call_code}{year_str}")

    # 3. Batch query options history -- MID_PRICE + TRDPRC_1 (NOT SETTLE)
    batches = [candidate_rics[i:i + batch_size] for i in range(0, len(candidate_rics), batch_size)]
    history_frames = []
    fields = ["TRDPRC_1", "MID_PRICE"]

    for batch in batches:
        try:
            df_batch = ld.get_history(
                universe=batch, fields=fields, start=start_str, end=end_str, interval="daily"
            )
            if df_batch is not None and not df_batch.empty:
                df_clean = df_batch.dropna(how="all", axis=1)
                if not df_clean.empty:
                    history_frames.append(df_clean)
        except Exception:
            for single_ric in batch:
                try:
                    df_single = ld.get_history(
                        universe=[single_ric], fields=fields, start=start_str, end=end_str, interval="daily"
                    )
                    if df_single is not None and not df_single.empty and not df_single.dropna(how="all").empty:
                        history_frames.append(df_single)
                except Exception:
                    continue

    ld.close_session()

    df_options = pd.DataFrame()
    if history_frames:
        df_options = pd.concat(history_frames, axis=1)
        df_options = df_options.loc[:, ~df_options.columns.duplicated()]

    data_payload = {
        "stock": df_stock,
        "options": df_options,
        "ticker": ticker_root,
        "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with open(CACHE_FILE, "wb") as f:
        pickle.dump(data_payload, f)
    print(f"Data pipeline complete. Results cached to {CACHE_FILE}.")

    return data_payload


# -----------------------------------------------------------------------------
# Reshape: wide -> tidy long table
# -----------------------------------------------------------------------------
def build_long_table(df_options: pd.DataFrame) -> pd.DataFrame:
    """
    df_options has a MultiIndex on columns: level 0 = RIC, level 1 = field
    (this is the standard lseg.data.get_history shape for multi-instrument,
    multi-field pulls). Index = date.

    Returns a tidy long dataframe:
    date, ric, underlying, expiry, type, strike, TRDPRC_1, MID_PRICE
    """
    if df_options.empty:
        return pd.DataFrame(
            columns=["date", "ric", "underlying", "expiry", "type", "strike", "TRDPRC_1", "MID_PRICE"]
        )

    # Normalize to a (ric, field) MultiIndex regardless of which way lseg returned it
    cols = df_options.columns
    if isinstance(cols, pd.MultiIndex):
        # Figure out which level holds field names
        level0_vals = set(cols.get_level_values(0))
        if {"TRDPRC_1", "MID_PRICE"} & level0_vals:
            df_options = df_options.swaplevel(0, 1, axis=1)
        df_options.columns = df_options.columns.set_names(["ric", "field"])
    else:
        # Single-field-per-column fallback: assume columns are RICs, field unknown
        raise ValueError("Unexpected column shape from LSEG; expected MultiIndex (ric, field).")

    records = []
    rics = df_options.columns.get_level_values("ric").unique()

    for ric in rics:
        parsed = parse_option_ric(ric)
        if parsed is None:
            continue
        try:
            sub = df_options[ric]
        except KeyError:
            continue

        trd = sub["TRDPRC_1"] if "TRDPRC_1" in sub.columns else pd.Series(index=sub.index, dtype=float)
        mid = sub["MID_PRICE"] if "MID_PRICE" in sub.columns else pd.Series(index=sub.index, dtype=float)

        for dt in sub.index:
            t_val = trd.loc[dt] if dt in trd.index else np.nan
            m_val = mid.loc[dt] if dt in mid.index else np.nan
            if pd.isna(t_val) and pd.isna(m_val):
                continue
            records.append({
                "date": dt,
                "ric": ric,
                "underlying": parsed["underlying"],
                "expiry": parsed["expiry"],
                "type": parsed["type"],
                "strike": parsed["strike"],
                "TRDPRC_1": t_val,
                "MID_PRICE": m_val,
            })

    long_df = pd.DataFrame(records)
    return long_df


# -----------------------------------------------------------------------------
# Stats required by the assignment
# -----------------------------------------------------------------------------
def compute_stats(long_df: pd.DataFrame):
    if long_df.empty:
        return 0.0, 0.0

    has_mid = long_df["MID_PRICE"].notna()
    has_trade = long_df["TRDPRC_1"].notna()

    mid_no_trade = (has_mid & ~has_trade).sum()
    total_with_mid_or_trade = len(long_df)
    pct_mid_no_trade = 100.0 * mid_no_trade / total_with_mid_or_trade if total_with_mid_or_trade else 0.0

    both = long_df[has_mid & has_trade]
    if not both.empty:
        median_abs_diff = float((both["MID_PRICE"] - both["TRDPRC_1"]).abs().median())
    else:
        median_abs_diff = float("nan")

    return pct_mid_no_trade, median_abs_diff


# -----------------------------------------------------------------------------
# Figures
# -----------------------------------------------------------------------------
def _empty_figure(message: str):
    """Themed placeholder figure so 'no data' states don't render as a blank white chart."""
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font=dict(color=TEXT_COLOR, size=14))
    fig.update_layout(
        paper_bgcolor=BG_COLOR,
        plot_bgcolor=PANEL_COLOR,
        font=dict(color=TEXT_COLOR, family="monospace"),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


def build_surface_figure(long_df: pd.DataFrame, option_type: str = "call"):
    df_t = long_df[long_df["type"] == option_type].dropna(subset=["MID_PRICE"]).copy()
    if df_t.empty:
        return _empty_figure(f"No {option_type} MID_PRICE data available")

    # Pick the as-of date with the richest grid, not just the most recent date --
    # the most recent date is often the sparsest one (fewer expiries have printed yet).
    counts = df_t.groupby("date").agg(
        n_strikes=("strike", "nunique"),
        n_expiries=("expiry", "nunique"),
    )
    viable = counts[(counts["n_strikes"] >= 2) & (counts["n_expiries"] >= 2)]
    if viable.empty:
        return _empty_figure(
            f"No single date has enough breadth (>=2 strikes and >=2 expiries) "
            f"to draw a {option_type} surface"
        )

    # among viable dates, pick the one with the most total combinations
    as_of_date = (viable["n_strikes"] * viable["n_expiries"]).idxmax()
    df_asof = df_t[df_t["date"] == as_of_date]

    pivot = df_asof.pivot_table(index="strike", columns="expiry", values="MID_PRICE", aggfunc="mean")

    fig = go.Figure(data=[go.Surface(
        z=pivot.values,
        x=[str(c) for c in pivot.columns],
        y=pivot.index,
        colorscale=[[0, PUT_COLOR], [1, CALL_COLOR]] if option_type == "call" else [[0, CALL_COLOR], [1, PUT_COLOR]],
        colorbar=dict(title="MID_PRICE"),
        connectgaps=True,
    )])

    fig.update_layout(
        title=f"{option_type.title()} MID_PRICE Surface — as of {as_of_date}",
        scene=dict(
            xaxis_title="Expiry",
            yaxis_title="Strike",
            zaxis_title="MID_PRICE",
            xaxis=dict(backgroundcolor=PANEL_COLOR, gridcolor=GRID_COLOR),
            yaxis=dict(backgroundcolor=PANEL_COLOR, gridcolor=GRID_COLOR),
            zaxis=dict(backgroundcolor=PANEL_COLOR, gridcolor=GRID_COLOR),
        ),
        paper_bgcolor=BG_COLOR,
        font=dict(color=TEXT_COLOR, family="monospace"),
        margin=dict(l=0, r=0, t=60, b=0),
    )
    return fig


def build_mid_vs_trade_figure(long_df: pd.DataFrame):
    fig = go.Figure()

    df_sorted = long_df.dropna(subset=["MID_PRICE"]).sort_values("date")
    fig.add_trace(go.Scatter(
        x=df_sorted["date"], y=df_sorted["MID_PRICE"],
        mode="markers", name="MID_PRICE",
        marker=dict(color=CALL_COLOR, size=5, opacity=0.6),
    ))

    df_trd = long_df.dropna(subset=["TRDPRC_1"]).sort_values("date")
    fig.add_trace(go.Scatter(
        x=df_trd["date"], y=df_trd["TRDPRC_1"],
        mode="markers", name="TRDPRC_1",
        marker=dict(color=PUT_COLOR, size=6, opacity=0.8, symbol="x"),
    ))

    fig.update_layout(
        title="MID_PRICE vs TRDPRC_1 — All Contracts, All Dates",
        xaxis_title="Date",
        yaxis_title="Price ($)",
        paper_bgcolor=BG_COLOR,
        plot_bgcolor=PANEL_COLOR,
        font=dict(color=TEXT_COLOR, family="monospace"),
        xaxis=dict(gridcolor=GRID_COLOR),
        yaxis=dict(gridcolor=GRID_COLOR),
        legend=dict(bgcolor=PANEL_COLOR),
        margin=dict(l=40, r=40, t=60, b=40),
    )
    return fig


# -----------------------------------------------------------------------------
# Static HTML export
# -----------------------------------------------------------------------------
def render_html(fig_surface_calls, fig_surface_puts, fig_compare, pct_mid_no_trade, median_abs_diff, ticker: str):
    surface_calls_div = pio.to_html(fig_surface_calls, include_plotlyjs="cdn", full_html=False)
    surface_puts_div = pio.to_html(fig_surface_puts, include_plotlyjs=False, full_html=False)
    compare_div = pio.to_html(fig_compare, include_plotlyjs=False, full_html=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>{ticker} Option Surface Lab</title>
<style>
  body {{
    background-color: {BG_COLOR};
    color: {TEXT_COLOR};
    font-family: 'Courier New', monospace;
    margin: 0;
    padding: 2rem;
  }}
  h1 {{ color: {CALL_COLOR}; letter-spacing: 2px; margin-bottom: 0.25rem; }}
  .subtitle {{ color: #75715E; margin-bottom: 1.5rem; }}
  .card {{
    background-color: {PANEL_COLOR};
    border: 1px solid {GRID_COLOR};
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 2rem;
  }}
  .surface-row {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    margin-bottom: 2rem;
  }}
  @media (max-width: 900px) {{
    .surface-row {{ grid-template-columns: 1fr; }}
  }}
  .meta-panel {{
    background-color: {PANEL_COLOR};
    border: 1px solid {GRID_COLOR};
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 2rem;
    line-height: 1.6;
  }}
  .meta-panel h2 {{ color: {ACCENT_COLOR}; font-size: 1rem; margin-top: 0; }}
  .stats {{
    display: flex;
    gap: 2rem;
    margin: 1rem 0;
  }}
  .stat-box {{
    background-color: {BG_COLOR};
    border: 1px solid {GRID_COLOR};
    border-radius: 8px;
    padding: 1rem 1.5rem;
    flex: 1;
  }}
  .stat-label {{ color: #75715E; font-size: 0.85rem; }}
  .stat-value {{ color: {CALL_COLOR}; font-size: 1.8rem; font-weight: bold; }}
  .field-def {{ color: #a6a29c; font-size: 0.9rem; }}
  .writeup {{
    background-color: {PANEL_COLOR};
    border: 1px solid {GRID_COLOR};
    border-radius: 8px;
    padding: 1.5rem;
    line-height: 1.6;
  }}
</style>
</head>
<body>
  <h1>OPTION SURFACE LAB — {ticker}</h1>
  <div class="subtitle">
    Energy Fuels Inc. ({ticker}) — a US uranium and rare-earth element miner.
    This page pulls expired {ticker} option contracts from LSEG and plots where real price data exists and where it doesn't.
  </div>

  <div class="meta-panel">
    <h2>What this page shows</h2>
    <p>
      Every listed {ticker} option (calls and puts, multiple strikes and expirations) is queried for two
      fields, per trading day, over a 12-week window: <strong>TRDPRC_1</strong> (the last actual trade
      price that day — often missing, since most contracts don't trade every day) and
      <strong>MID_PRICE</strong> (the closing bid/ask midpoint — the closest available "mark," since LSEG
      does not expose a true settlement price for expired US equity options). Price is quoted per share;
      one contract = 100 shares.
    </p>
    <div class="stats">
      <div class="stat-box">
        <div class="stat-label">% of contract-days with a MID but no trade</div>
        <div class="stat-value">{pct_mid_no_trade:.1f}%</div>
        <div class="field-def">Nearly a quarter of quoted series never actually printed a trade that day.</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Median |MID_PRICE − TRDPRC_1| (when both exist)</div>
        <div class="stat-value">${median_abs_diff:.3f}</div>
        <div class="field-def">When a trade did happen, it landed very close to the quoted mid.</div>
      </div>
    </div>
  </div>

  <div class="surface-row">
    <div class="card">{surface_calls_div}</div>
    <div class="card">{surface_puts_div}</div>
  </div>

  <div class="card">{compare_div}</div>

  <div class="writeup">
    <strong>Analysis</strong>
    <p>
      <strong>Where the data is dense, and where it isn't.</strong>
      Prices cluster around at-the-money strikes in near-dated expiries and thin out fast moving
      deep in- or out-of-the-money. MID_PRICE covers far more of the grid than TRDPRC_1:
      {pct_mid_no_trade:.1f}% of contract-days have a quoted mid with no trade behind it. The trade
      cloud is a sparse subset sitting inside a much denser quote cloud.
    </p>
    <p>
      <strong>Why interpolating the gaps is dangerous here.</strong>
      On a $0.50 grid for a name this thin, an empty cell usually means nobody quoted or traded that
      contract — not that a price existed and went unrecorded. Interpolating invents a market that
      wasn't there, and on a ~$14 underlying each $0.50 step is a real move in moneyness, so a smooth
      surface can imply a fill you never could have gotten.
    </p>
    <p>
      <strong>Mark vs. evidence of a trade.</strong>
      MID_PRICE is the mark — it's the closing NBBO midpoint, it exists on far more series, and LSEG
      exposes no true settlement price for expired US equity options. TRDPRC_1 is evidence someone
      actually transacted: one print, not a valuation. Median gap between them is ${median_abs_diff:.3f}
      where both exist, so the mid is a reasonable stand-in — but I'll price off the mid and use trades
      to check it, not the other way around.
    </p>
  </div>
</body>
</html>
"""
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Wrote static site to {OUTPUT_HTML}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    payload = load_or_fetch_pipeline_data()
    long_df = build_long_table(payload["options"])

    print(f"Parsed {len(long_df)} (contract, date) rows.")

    pct_mid_no_trade, median_abs_diff = compute_stats(long_df)
    print(f"% series with MID and no trade: {pct_mid_no_trade:.2f}%")
    print(f"Median |MID_PRICE - TRDPRC_1|: {median_abs_diff:.4f}")

    for opt_type in ("call", "put"):
        n = long_df[(long_df["type"] == opt_type) & long_df["MID_PRICE"].notna()].shape[0]
        print(f"  {opt_type} rows with MID_PRICE: {n}")

    fig_surface_calls = build_surface_figure(long_df, option_type="call")
    fig_surface_puts = build_surface_figure(long_df, option_type="put")
    fig_compare = build_mid_vs_trade_figure(long_df)

    render_html(fig_surface_calls, fig_surface_puts, fig_compare, pct_mid_no_trade, median_abs_diff, payload["ticker"])
