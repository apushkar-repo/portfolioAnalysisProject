"""Streamlit UI for Portfolio Pulse."""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

# Ensure project root is importable for `streamlit run app/ui.py`
# and for sibling imports when the package is not installed editable.
_APP_DIR = Path(__file__).resolve().parent
_ROOT = _APP_DIR.parent
_LOGO_PATH = _ROOT / "assets" / "northstar-portfolio-logo.png"
for _p in (str(_ROOT), str(_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import altair as alt
import pandas as pd
import streamlit as st
from streamlit import config as st_config

try:
    from app.llm_chat import (
        LLMConfigurationError,
        LLMRequestError,
        ask_portfolio_assistant,
        load_llm_config,
    )
    from app.models import DISCLAIMER, IRR_BASELINE_POLICY
    from app.pipeline import run_analysis
    from app.report import render_pdf_report
except ModuleNotFoundError:
    from llm_chat import (
        LLMConfigurationError,
        LLMRequestError,
        ask_portfolio_assistant,
        load_llm_config,
    )
    from models import DISCLAIMER, IRR_BASELINE_POLICY
    from pipeline import run_analysis
    from report import render_pdf_report

st.set_page_config(
    page_title="Portfolio Pulse",
    page_icon=str(_LOGO_PATH),
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _init_session() -> None:
    if "disclaimer_accepted" not in st.session_state:
        st.session_state.disclaimer_accepted = False
    if "dark_mode" not in st.session_state:
        st.session_state.dark_mode = st_config.get_option("theme.base") == "dark"
    if "additional_upload_generation" not in st.session_state:
        st.session_state.additional_upload_generation = 0
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []


def _sync_theme() -> None:
    """Drive Streamlit's native theme so every widget adapts.

    Styling the app with injected CSS cannot work: Streamlit bakes theme colors
    into generated classes and paints dataframes onto a canvas, so widgets like
    dataframes, tabs and dropdowns would keep light colors. Switching the native
    theme re-themes all of them.

    The theme reaches the browser in the NewSession message, which is rebuilt
    from config when a run starts, so a change only lands on the following run.

    ``theme.base`` is process-wide rather than per-session, so this re-asserts
    the current session's choice on every run to limit bleed between sessions.
    """
    desired = "dark" if st.session_state.dark_mode else "light"
    if st_config.get_option("theme.base") != desired:
        st_config.set_option("theme.base", desired)

    # Tell the browser which scheme to paint its own controls in. Native
    # elements (button, summary, scrollbars, form fields) are drawn by the
    # browser, not Streamlit, so without this they stay light-on-light in dark
    # mode regardless of the Streamlit theme.
    st.markdown(
        f"<style>:root {{ color-scheme: {desired}; }}</style>",
        unsafe_allow_html=True,
    )

    if st.session_state.get("theme_delivered") != desired:
        st.session_state.theme_delivered = desired
        st.rerun()


def _app_background_color() -> str:
    """Resolve the painted app background.

    The sticky header has to hide the content scrolling beneath it, and
    Streamlit exposes no CSS variable for its background, so fall back to the
    built-in defaults for whichever base theme is active.
    """
    configured = st_config.get_option("theme.backgroundColor")
    if configured:
        return configured
    return "#0e1117" if st.session_state.dark_mode else "#ffffff"


def _render_app_header() -> None:
    st.markdown(
        f"""
        <style>
        /* A sticky element only sticks within its parent's box, and Streamlit
           wraps each container in a short block of its own height. Pinning that
           wrapper instead lets the header stay put for the whole page scroll. */
        .stVerticalBlock > div:has(> .st-key-app_header) {{
            position: sticky;
            /* Streamlit paints its own 60px toolbar bar over the top of the
               viewport, so pin below it rather than at 0. */
            top: 3.75rem;
            z-index: 999;
            background-color: {_app_background_color()};
        }}
        .st-key-app_header {{
            background-color: inherit;
            padding-top: 0.45rem;
        }}
        .st-key-app_header hr {{
            margin: 0.35rem 0 0.75rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="app_header"):
        logo, title, controls = st.columns([0.65, 6.85, 2.5])
        with logo:
            st.image(str(_LOGO_PATH), width=54)
        with title:
            st.markdown("## Portfolio Pulse")
            st.caption("Clear direction for every holding.")
        with controls:
            new_action, theme_action = st.columns([2.2, 1])
            with new_action:
                if st.session_state.get("bundle") is not None:
                    if st.button(
                        "New analysis",
                        key="header_new_analysis",
                        width="stretch",
                    ):
                        st.session_state.pop("bundle", None)
                        st.session_state.pop("analysis_files", None)
                        st.session_state.pop("csv_uploads", None)
                        st.session_state.chat_messages = []
                        st.session_state.pop("last_llm_call_at", None)
                        st.session_state.additional_upload_generation += 1
                        st.rerun()
            with theme_action:
                icon = "☀️" if st.session_state.dark_mode else "🌙"
                target = "light" if st.session_state.dark_mode else "dark"
                if st.button(
                    icon,
                    key="theme_icon",
                    help=f"Switch to {target} mode",
                    width="stretch",
                ):
                    st.session_state.dark_mode = not st.session_state.dark_mode
                    st.rerun()
        st.divider()


def _pie_chart(data: pd.DataFrame, category: str, value: str) -> alt.Chart:
    """Build a pie chart with each slice's share of the total in the tooltip."""
    total = data[value].sum()
    plotted = data.copy()
    plotted["Share"] = plotted[value] / total if total else 0.0
    return (
        alt.Chart(plotted)
        .mark_arc()
        .encode(
            theta=alt.Theta(f"{value}:Q", stack=True),
            color=alt.Color(f"{category}:N", title=category),
            tooltip=[
                alt.Tooltip(f"{category}:N", title=category),
                alt.Tooltip(f"{value}:Q", title=value, format=",.2f"),
                alt.Tooltip("Share:Q", title="Share", format=".1%"),
            ],
        )
    )


def _top_n_with_other(
    data: pd.DataFrame, category: str, value: str, top_n: int = 10
) -> pd.DataFrame:
    """Keep the largest slices and roll the tail into a single "Other" slice.

    A pie is unreadable past a handful of slices, and collapsing the tail keeps
    the slices summing to the whole portfolio rather than an arbitrary subset.
    """
    ranked = data.sort_values(value, ascending=False)
    if len(ranked) <= top_n:
        return ranked
    head = ranked.head(top_n)
    other = pd.DataFrame(
        {category: ["Other"], value: [ranked[value].iloc[top_n:].sum()]}
    )
    return pd.concat([head, other], ignore_index=True)


def _source_name_map(bundle) -> dict[str, str]:
    return {source.source_id: source.filename for source in bundle.diagnostics.sources}


def _metrics_table(rows, label_map: dict[str, str] | None = None) -> pd.DataFrame:
    label_map = label_map or {}
    return pd.DataFrame(
        [
            {
                "Group": label_map.get(m.label, m.label),
                "Period": m.interval,
                "Starting Market Value": m.mv_start,
                "Ending Market Value": m.mv_end,
                "Price Return": m.return_price_pct,
                "Dividend Return": m.return_div_pct,
                "Net Return": m.return_net_pct,
                "Net Growth": m.pnl_net,
                "IRR": m.irr_pct,
                "Number of Tickers": m.num_tickers,
            }
            for m in rows
        ]
    )


def _holdings_table(bundle) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source_id": h.source_id,
                "source_name": h.source_name,
                "ticker": h.ticker,
                "shares": float(h.shares),
                "avg_purchase_price": float(h.avg_purchase_price),
                "cost_basis": float(h.shares * h.avg_purchase_price),
                "purchase_date": h.purchase_date,
                "security_type": h.security_type,
            }
            for h in bundle.holdings
        ]
    )


def _ticker_performance_table(bundle) -> pd.DataFrame:
    rows = pd.DataFrame(
        [
            {
                "ticker": m.ticker,
                "interval": m.interval,
                "sector": m.sector,
                "industry": m.industry,
                "MV_start": m.mv_start,
                "MV_end": m.mv_end,
                "PNL_net": m.pnl_net,
                "PNL_price": m.pnl_price,
                "PNL_div": m.pnl_div,
            }
            for m in bundle.ticker_metrics
        ]
    )
    if rows.empty:
        return rows

    grouped = (
        rows.groupby(["ticker", "interval", "sector", "industry"], as_index=False)
        .agg(
            {
                "MV_start": "sum",
                "MV_end": "sum",
                "PNL_net": "sum",
                "PNL_price": "sum",
                "PNL_div": "sum",
            }
        )
    )
    denominator = grouped["MV_start"].replace(0, pd.NA)
    grouped["Return_price_pct"] = grouped["PNL_price"] / denominator
    grouped["Return_div_pct"] = grouped["PNL_div"] / denominator
    grouped["Return_net_pct"] = grouped["PNL_net"] / denominator
    return grouped


def _render_disclaimer_gate() -> None:
    st.subheader("Before you begin")
    st.caption("Please read and accept the disclaimer.")

    with st.container(border=True):
        st.subheader("Disclaimer")
        st.write(DISCLAIMER)

    agreed = st.checkbox(
        "I have read this disclaimer and understand that this app does not provide "
        "financial or investment advice.",
        key="disclaimer_checkbox",
    )
    continue_clicked = st.button(
        "I agree — continue",
        type="primary",
        disabled=not agreed,
        key="disclaimer_agree_btn",
    )
    if continue_clicked and agreed:
        st.session_state.disclaimer_accepted = True
        st.rerun()

    st.info("You must accept the disclaimer to upload portfolios and run analysis.")


def _render_analysis_app() -> None:
    bundle = st.session_state.get("bundle")
    if bundle is not None:
        _render_results(bundle)
        return

    st.caption("Upload one or more portfolio CSVs to review and analyze performance.")

    with st.expander("Disclaimer (accepted)", expanded=False):
        st.write(DISCLAIMER)

    with st.expander("CSV requirements", expanded=False):
        st.markdown(
            """
            **Required columns:** `ticker`, `avg_purchase_price`, `shares`

            Optional: `purchase_date`, `currency`, `security_type`

            Maximum **2 MB per CSV**. Files containing spreadsheet formulas,
            macros, or macro-like executable content are rejected. Combined
            uploads are limited to 2,000 holdings rows.
            """
        )

    uploads = st.file_uploader(
        "Upload portfolio CSV file(s)",
        type=["csv"],
        accept_multiple_files=True,
        key="csv_uploads",
    )

    run = st.button(
        "Run analysis",
        type="primary",
        disabled=not uploads,
        key="run_analysis_btn",
    )

    if run:
        files = [
            (f"portfolio_{index + 1}.csv", upload.getvalue())
            for index, upload in enumerate(uploads)
        ]
        try:
            with st.spinner("Preparing portfolio analysis..."):
                st.session_state.bundle = run_analysis(files, as_of=date.today())
                st.session_state.analysis_files = files
                st.session_state.chat_messages = []
        except ValueError as exc:
            # Unusable input (unparseable CSV, missing required columns) is a
            # user-fixable problem, so explain it instead of failing the script.
            st.session_state.pop("bundle", None)
            st.error(f"Could not analyze the uploaded file(s): {exc}")
            return
        st.rerun()

    st.markdown(
        '<div data-testid="analysis-idle">Upload CSV files, then click '
        "<strong>Run analysis</strong>.</div>",
        unsafe_allow_html=True,
    )


def _render_add_files() -> None:
    """Append CSVs to the current inputs and rebuild the complete analysis."""
    existing_files = st.session_state.get("analysis_files", [])
    if not existing_files:
        return

    with st.expander("Add more portfolio files", expanded=False):
        st.caption(
            "Upload additional CSV files, then update the analysis. "
            "Existing files will be retained. Maximum 2 MB per CSV."
        )
        additions = st.file_uploader(
            "Additional portfolio CSV file(s)",
            type=["csv"],
            accept_multiple_files=True,
            key=(
                "additional_csv_uploads_"
                f"{st.session_state.additional_upload_generation}"
            ),
        )
        if st.button(
            "Add files and update analysis",
            type="primary",
            disabled=not additions,
            key="update_analysis_btn",
        ):
            added_files = [
                (
                    f"portfolio_{len(existing_files) + index + 1}.csv",
                    upload.getvalue(),
                )
                for index, upload in enumerate(additions)
            ]
            combined_files = [*existing_files, *added_files]
            try:
                with st.spinner("Updating portfolio analysis..."):
                    updated_bundle = run_analysis(
                        combined_files,
                        as_of=date.today(),
                    )
            except ValueError as exc:
                st.error(f"Could not update the analysis: {exc}")
                return

            st.session_state.analysis_files = combined_files
            st.session_state.bundle = updated_bundle
            st.session_state.chat_messages = []
            st.session_state.pop("last_llm_call_at", None)
            st.session_state.additional_upload_generation += 1
            st.rerun()


def _render_data_overview(bundle, selected_interval: str) -> None:
    _render_add_files()

    d = bundle.diagnostics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Files uploaded", d.files_uploaded)
    c2.metric("Unique sources", d.unique_sources)
    c3.metric("Tickers", d.tickers_parsed)
    c4.metric("PII/NPI redacted", d.redacted_field_count)

    if d.duplicates:
        st.warning(f"Duplicates detected: {len(d.duplicates)}")
        names = _source_name_map(bundle)
        duplicate_rows = pd.DataFrame(
            [
                {
                    "File Name": duplicate["filename"],
                    "Duplicate Of": names.get(
                        duplicate["duplicate_of"], duplicate["duplicate_of"]
                    ),
                    "Duplicate Type": duplicate["kind"].title(),
                }
                for duplicate in d.duplicates
            ]
        )
        st.dataframe(duplicate_rows, width="stretch", hide_index=True)
    if d.warnings:
        with st.expander("Validation warnings", expanded=False):
            for w in d.warnings:
                st.write(f"- {w}")

    holdings_df = _holdings_table(bundle)
    insight = next(
        (
            item
            for item in getattr(bundle, "insights", [])
            if item.interval == selected_interval
        ),
        None,
    )
    mover_tickers = {
        item.ticker
        for item in (
            [*(insight.contributors if insight else [])]
            + [*(insight.detractors if insight else [])]
        )
    }
    holding_filter = st.radio(
        "Show holdings",
        ["All", "ETFs", "Stocks", "Top movers"],
        horizontal=True,
        key="holdings_filter",
        help="Filters the charts, timeline, and holdings table below.",
    )
    filtered_holdings = holdings_df
    normalized_types = holdings_df["security_type"].str.lower()
    if holding_filter == "ETFs":
        filtered_holdings = holdings_df[normalized_types.str.contains("etf", na=False)]
    elif holding_filter == "Stocks":
        filtered_holdings = holdings_df[
            normalized_types.str.contains("stock|equity", na=False)
        ]
    elif holding_filter == "Top movers":
        filtered_holdings = holdings_df[holdings_df["ticker"].isin(mover_tickers)]

    if filtered_holdings.empty and not holdings_df.empty:
        st.info(f"No holdings match the “{holding_filter}” filter.")

    if insight:
        st.subheader("How diversified is this portfolio?")
        st.progress(
            insight.diversification_score / 100,
            text=f"Diversification health: {insight.diversification_score}/100",
        )
        st.caption(
            f"Your top 3 holdings make up "
            f"{insight.top_three_concentration_pct:.0%} of the portfolio. "
            "The score combines the number of holdings with how evenly money is spread."
        )
        if insight.sector_weights:
            sector_data = pd.DataFrame(
                [
                    {"Sector": sector, "Share": share}
                    for sector, share in insight.sector_weights.items()
                ]
            )
            st.altair_chart(
                alt.Chart(sector_data)
                .mark_bar()
                .encode(
                    x=alt.X("Share:Q", axis=alt.Axis(format="%"), title="Portfolio share"),
                    y=alt.Y("Sector:N", sort="-x"),
                    tooltip=[
                        alt.Tooltip("Sector:N"),
                        alt.Tooltip("Share:Q", format=".1%"),
                    ],
                ),
                width="stretch",
            )

    if not filtered_holdings.empty:
        # Chart field names must avoid "." and "[]": Vega-Lite reads those as
        # nested field access, which silently renders an empty chart.
        allocation = _top_n_with_other(
            filtered_holdings.groupby("ticker", as_index=False)["cost_basis"]
            .sum()
            .rename(columns={"ticker": "Ticker", "cost_basis": "Amount Invested"}),
            "Ticker",
            "Amount Invested",
        )
        st.subheader("Positions by what you paid (approx.)")
        st.caption(
            "This is shares × average purchase price. It is an estimate of the "
            "amount invested, not the current value."
        )
        st.altair_chart(
            _pie_chart(allocation, "Ticker", "Amount Invested"),
            width="stretch",
        )

        timeline = filtered_holdings.dropna(subset=["purchase_date"]).copy()
        st.subheader("Portfolio investment over time")
        st.caption(
            "Running total of what you have put in, based on purchase dates. "
            "Per-ticker purchase history is on the Ticker Details tab."
        )
        if timeline.empty:
            st.info(
                "No purchase dates are available. Add a `purchase_date` column "
                "to plot how your investment built up over time."
            )
        else:
            timeline["purchase_date"] = pd.to_datetime(timeline["purchase_date"])
            invested_over_time = (
                timeline.groupby("purchase_date", as_index=False)["cost_basis"]
                .sum()
                .sort_values("purchase_date")
                .rename(
                    columns={
                        "purchase_date": "Purchase Date",
                        "cost_basis": "Invested That Day",
                    }
                )
            )
            invested_over_time["Total Invested"] = invested_over_time[
                "Invested That Day"
            ].cumsum()
            st.altair_chart(
                alt.Chart(invested_over_time)
                .mark_area(line=True, opacity=0.3, interpolate="step-after")
                .encode(
                    x=alt.X("Purchase Date:T", title="Purchase date"),
                    y=alt.Y(
                        "Total Invested:Q",
                        title="Total invested to date",
                        axis=alt.Axis(format="$,.0f"),
                    ),
                    tooltip=[
                        alt.Tooltip("Purchase Date:T", format="%b %d, %Y"),
                        alt.Tooltip(
                            "Invested That Day:Q",
                            title="Added that day",
                            format="$,.2f",
                        ),
                        alt.Tooltip("Total Invested:Q", format="$,.2f"),
                    ],
                ),
                width="stretch",
            )

            missing_dates = len(filtered_holdings) - len(timeline)
            if missing_dates:
                st.caption(
                    f"{missing_dates} holding(s) without a purchase date are not shown."
                )

    st.subheader("Normalized holdings preview")
    holdings_display = filtered_holdings.drop(
        columns=["source_id", "cost_basis"]
    ).rename(
        columns={
            "source_name": "File Name",
            "ticker": "Ticker",
            "shares": "Shares",
            "avg_purchase_price": "Average Purchase Price",
            "purchase_date": "Purchase Date",
            "security_type": "Security Type",
        }
    )
    st.dataframe(
        holdings_display,
        width="stretch",
        hide_index=True,
        column_config={
            "Average Purchase Price": st.column_config.NumberColumn(
                "Average price paid",
                format="$%.2f",
                help="Approximate average amount paid per share.",
            ),
            "Shares": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def _render_performance(bundle, selected_interval: str) -> None:
    port = bundle.portfolio_metrics
    insight = next(
        (
            item
            for item in getattr(bundle, "insights", [])
            if item.interval == selected_interval
        ),
        None,
    )
    selected_metric = next(
        (metric for metric in port if metric.interval == selected_interval),
        None,
    )
    if selected_metric and insight:
        value_col, change_col, diversity_col, risk_col = st.columns(4)
        value_col.metric(
            "Portfolio value",
            f"${insight.portfolio_value:,.2f}",
            help="Estimated current value of holdings with available market data.",
        )
        change_col.metric(
            f"Change over {selected_interval}",
            f"${insight.net_growth:,.2f}",
            delta=f"{insight.net_return_pct:.2%}",
            help="Estimated dollar and percentage change over the selected time range.",
        )
        diversity_col.metric(
            "Diversification health",
            f"{insight.diversification_score}/100",
            help="Combines how many holdings you have with how evenly money is spread.",
        )
        volatility = insight.annualized_volatility_pct
        swing_label = (
            "Not enough data"
            if volatility is None
            else "Lower swing"
            if volatility < 0.15
            else "Moderate swing"
            if volatility < 0.30
            else "Higher swing"
        )
        risk_col.metric(
            "Typical swinginess",
            swing_label,
            help=(
                "Based on annualized day-to-day movement. It describes past "
                "movement, not future risk."
            ),
        )

        st.subheader(f"What drove the change over {selected_interval}?")
        contributor_col, detractor_col = st.columns(2)

        def _mover_rows(items):
            return pd.DataFrame(
                [
                    {
                        "Ticker": item.ticker,
                        "Dollar Impact": item.pnl_net,
                        "Ticker Return": item.return_net_pct,
                    }
                    for item in items
                ]
            )

        with contributor_col:
            st.caption("Top contributors")
            contributors = _mover_rows(insight.contributors)
            if contributors.empty:
                st.write("No positive contributors in this period.")
            else:
                st.dataframe(
                    contributors,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Dollar Impact": st.column_config.NumberColumn(format="$%.2f"),
                        "Ticker Return": st.column_config.NumberColumn(format="percent"),
                    },
                )
        with detractor_col:
            st.caption("Top detractors")
            detractors = _mover_rows(insight.detractors)
            if detractors.empty:
                st.write("No negative contributors in this period.")
            else:
                st.dataframe(
                    detractors,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Dollar Impact": st.column_config.NumberColumn(format="$%.2f"),
                        "Ticker Return": st.column_config.NumberColumn(format="percent"),
                    },
                )

        st.subheader("Risk, income, and gains in plain language")
        concentration_col, drawdown_col, income_col = st.columns(3)
        concentration_col.metric(
            "Top 3 holdings",
            f"{insight.top_three_concentration_pct:.0%}",
            help="Share of current portfolio value held in the three largest positions.",
        )
        concentration_col.caption(
            f"Largest: {insight.largest_holding} "
            f"({insight.largest_holding_pct:.0%})"
        )
        drawdown_col.metric(
            "Worst past drop",
            (
                f"{insight.max_drawdown_pct:.1%}"
                if insight.max_drawdown_pct is not None
                else "Not enough data"
            ),
            help="Largest peak-to-low decline observed during the selected range.",
        )
        income_col.metric(
            "Estimated income component",
            f"${insight.estimated_income_component:,.2f}",
            help=(
                "Estimated from adjusted-price effects. This is not a dividend "
                "forecast or cash-income statement."
            ),
        )
        income_col.caption(
            "A 12-month dividend forecast needs payout history that is not in "
            "the uploaded holdings snapshot."
        )

        paper_col, locked_col, timing_col = st.columns(3)
        paper_col.metric(
            "Estimated gains on paper",
            f"${insight.estimated_unrealized_gain:,.2f}",
            help="Current estimated value minus what you paid (approx.).",
        )
        locked_col.metric("Gains already locked in", "Tracking needed")
        locked_col.caption("Sell/trade history is required to calculate realized gains.")
        timing_col.metric("Recently added lots", insight.recent_lot_count)
        timing_col.caption(
            "Purchases in the last 90 days can make short-range results look different."
        )

    portfolio_df = _metrics_table(port)
    if not portfolio_df.empty:
        returns_chart = portfolio_df[
            ["Period", "Price Return", "Dividend Return", "Net Return"]
        ].melt(
            id_vars="Period",
            var_name="Return Component",
            value_name="Return",
        )
        st.subheader("Portfolio return trend by interval")
        st.altair_chart(
            alt.Chart(returns_chart)
            .mark_line(point=True, strokeWidth=3)
            .encode(
                x=alt.X(
                    "Period:N",
                    sort=["1M", "3M", "YTD", "1Y", "3Y"],
                    title="Interval",
                ),
                y=alt.Y("Return:Q", axis=alt.Axis(format="%"), title="Return"),
                color=alt.Color("Return Component:N"),
                tooltip=[
                    alt.Tooltip("Period:N"),
                    alt.Tooltip("Return Component:N"),
                    alt.Tooltip("Return:Q", format=".2%"),
                ],
            ),
            width="stretch",
        )

        value_chart = portfolio_df[
            ["Period", "Starting Market Value", "Ending Market Value"]
        ].set_index("Period")
        st.subheader("Start and end market value")
        st.line_chart(
            value_chart, y=["Starting Market Value", "Ending Market Value"]
        )

    source_df = _metrics_table(bundle.by_source, _source_name_map(bundle))
    industry_df = _metrics_table(bundle.by_industry)
    selected_source_df = (
        source_df
        if source_df.empty
        else source_df[source_df["Period"] == selected_interval]
    )
    selected_industry_df = (
        industry_df
        if industry_df.empty
        else industry_df[industry_df["Period"] == selected_interval]
    )
    if not source_df.empty or not industry_df.empty:
        st.subheader(f"Source and industry comparison · {selected_interval}")
        st.caption("Compare net return rates; hover each bar for net growth dollars.")
        source_col, industry_col = st.columns(2)
        with source_col:
            st.caption("Net return by source")
            source_returns = (
                alt.Chart(selected_source_df)
                .mark_bar()
                .encode(
                    x=alt.X("Group:N", title="File Name", sort="-y"),
                    y=alt.Y("Net Return:Q", axis=alt.Axis(format="%")),
                    color=alt.Color("Group:N", title="File Name"),
                    tooltip=[
                        alt.Tooltip("Group:N", title="File Name"),
                        alt.Tooltip("Period:N"),
                        alt.Tooltip("Net Return:Q", format=".2%"),
                        alt.Tooltip("Net Growth:Q", format="$,.2f"),
                    ],
                )
            )
            st.altair_chart(source_returns, width="stretch")
        with industry_col:
            st.caption("Net return by industry")
            industry_returns = (
                alt.Chart(selected_industry_df)
                .mark_bar()
                .encode(
                    x=alt.X("Group:N", title="Industry", sort="-y"),
                    y=alt.Y("Net Return:Q", axis=alt.Axis(format="%")),
                    color=alt.Color("Group:N", title="Industry"),
                    tooltip=[
                        alt.Tooltip("Group:N", title="Industry"),
                        alt.Tooltip("Period:N"),
                        alt.Tooltip("Net Return:Q", format=".2%"),
                        alt.Tooltip("Net Growth:Q", format="$,.2f"),
                    ],
                )
            )
            st.altair_chart(industry_returns, width="stretch")

    breakdown = st.radio(
        "Performance breakdown",
        ["Portfolio", "By source", "By industry"],
        index=0,
        horizontal=True,
        key="performance_breakdown",
    )
    if breakdown == "By source":
        display_df = selected_source_df
    elif breakdown == "By industry":
        display_df = selected_industry_df
    else:
        display_df = portfolio_df[portfolio_df["Period"] == selected_interval]
    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Starting Market Value": st.column_config.NumberColumn(format="$%.2f"),
            "Ending Market Value": st.column_config.NumberColumn(format="$%.2f"),
            "Net Growth": st.column_config.NumberColumn(format="$%.2f"),
            "Price Return": st.column_config.NumberColumn(format="percent"),
            "Dividend Return": st.column_config.NumberColumn(format="percent"),
            "Net Return": st.column_config.NumberColumn(format="percent"),
            "IRR": st.column_config.NumberColumn(format="percent"),
        },
    )

    if insight:
        st.subheader("Things you may want to review")
        suggestions: list[str] = []
        if insight.largest_holding_pct >= 0.30:
            suggestions.append(
                f"{insight.largest_holding} is "
                f"{insight.largest_holding_pct:.0%} of the portfolio. "
                "Consider whether spreading future contributions would better "
                "match your comfort level."
            )
        if insight.diversification_score < 50:
            suggestions.append(
                "This portfolio is concentrated in relatively few positions. "
                "If stability matters to you, review how much each holding can "
                "influence the whole portfolio."
            )
        if (
            insight.annualized_volatility_pct is not None
            and insight.annualized_volatility_pct >= 0.30
        ):
            suggestions.append(
                "These holdings have moved around a lot historically. Expect "
                "larger ups and downs and check whether that fits your comfort level."
            )
        if not suggestions:
            suggestions.append(
                "No major concentration or swinginess flags were found for this "
                "range. Periodically review whether the mix still matches your goals."
            )
        for suggestion in suggestions:
            st.info(suggestion)
        st.caption(
            "Educational prompts only—not recommendations to buy, sell, or hold."
        )

    with st.expander("Methodology (plain English)", expanded=False):
        st.markdown(
            f"""
            - **Time ranges** are 1 month, 3 months, calendar year-to-date,
              1 year, and 3 years.
            - **Total change** uses adjusted prices, including split/dividend effects.
            - **Estimated income component** is total change minus price-only change;
              it is not a forward dividend forecast.
            - **Annualized return estimate (IRR)** uses a 2-point approximation
              compared with what you paid, with baseline
              **{IRR_BASELINE_POLICY}** when purchase dates are missing.
            - **Swinginess** annualizes past day-to-day moves. **Worst past drop**
              is the largest decline from a prior peak in the selected range.
            """
        )


def _latest_close(bundle, ticker: str) -> tuple[float, date] | None:
    """Most recent close price observed for a ticker across all intervals."""
    rows = [m for m in bundle.ticker_metrics if m.ticker == ticker]
    if not rows:
        return None
    latest = max(rows, key=lambda m: m.t_end)
    return latest.close_end, latest.t_end


def _render_ticker_details(bundle, selected_interval: str) -> None:
    ticker_df = _ticker_performance_table(bundle)
    if ticker_df.empty:
        st.info("No ticker-level market data is available.")
        return

    selected = st.selectbox(
        "Select ticker",
        sorted(ticker_df["ticker"].unique()),
        key="ticker_detail",
    )
    detail = ticker_df[ticker_df["ticker"] == selected].copy()
    first = detail.iloc[0]
    holdings = _holdings_table(bundle)
    position = holdings[holdings["ticker"] == selected]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Shares", f"{position['shares'].sum():,.2f}")
    weighted_price = (
        (position["avg_purchase_price"] * position["shares"]).sum()
        / position["shares"].sum()
        if position["shares"].sum()
        else 0
    )
    c2.metric(
        "Average price paid",
        f"${weighted_price:,.2f}",
        help="Approximate purchase price weighted by the number of shares.",
    )
    latest = _latest_close(bundle, selected)
    if latest is None:
        c3.metric("Current price", "Not available")
    else:
        current_price, price_date = latest
        c3.metric(
            "Current price",
            f"${current_price:,.2f}",
            delta=(
                f"{(current_price - weighted_price) / weighted_price:+.2%} vs paid"
                if weighted_price
                else None
            ),
            help=f"Latest closing price available ({price_date}).",
        )
    c4.metric("Sector", str(first["sector"]))
    st.caption(f"Industry: {first['industry']}")

    selected_detail = detail[detail["interval"] == selected_interval]
    if not selected_detail.empty:
        period_row = selected_detail.iloc[0]
        dominant_driver = (
            "price movement"
            if abs(period_row["PNL_price"]) >= abs(period_row["PNL_div"])
            else "estimated income/corporate-action effects"
        )
        st.info(
            f"Over {selected_interval}, {selected} changed by "
            f"${period_row['PNL_net']:,.2f}. The larger driver was {dominant_driver}."
        )

    st.subheader(f"When you bought {selected}")
    lots = position.dropna(subset=["purchase_date"]).copy()
    if lots.empty:
        st.info(
            f"No purchase dates are available for {selected}. Add a "
            "`purchase_date` column to see when each lot was bought."
        )
    else:
        lots["purchase_date"] = pd.to_datetime(lots["purchase_date"])
        lots = lots.rename(
            columns={
                "purchase_date": "Purchase Date",
                "source_name": "File Name",
                "shares": "Shares",
                "avg_purchase_price": "Price Paid",
                "cost_basis": "Amount Invested",
            }
        )
        purchases = (
            alt.Chart(lots)
            .mark_circle(opacity=0.85)
            .encode(
                x=alt.X("Purchase Date:T", title="Purchase date"),
                y=alt.Y(
                    "Price Paid:Q",
                    title="Price paid per share",
                    axis=alt.Axis(format="$,.2f"),
                    scale=alt.Scale(zero=False),
                ),
                size=alt.Size("Shares:Q", title="Shares"),
                color=alt.Color("File Name:N", title="File Name"),
                tooltip=[
                    alt.Tooltip("File Name:N"),
                    alt.Tooltip("Purchase Date:T", format="%b %d, %Y"),
                    alt.Tooltip("Shares:Q", format=",.2f"),
                    alt.Tooltip("Price Paid:Q", format="$,.2f"),
                    alt.Tooltip("Amount Invested:Q", format="$,.2f"),
                ],
            )
        )
        if latest is not None:
            price_rule = (
                alt.Chart(pd.DataFrame({"Current Price": [latest[0]]}))
                .mark_rule(strokeDash=[6, 4], color="#FF4B4B")
                .encode(
                    y=alt.Y("Current Price:Q"),
                    tooltip=[alt.Tooltip("Current Price:Q", format="$,.2f")],
                )
            )
            purchases = purchases + price_rule
        st.altair_chart(purchases, width="stretch")
        if latest is not None:
            st.caption(
                "Each dot is one purchase lot; the dashed line is the current price."
            )

    return_chart = detail[
        ["interval", "Return_price_pct", "Return_div_pct", "Return_net_pct"]
    ].rename(
        columns={
            "interval": "Period",
            "Return_price_pct": "Price Return",
            "Return_div_pct": "Dividend Return",
            "Return_net_pct": "Net Return",
        }
    )
    return_chart.iloc[:, 1:] = return_chart.iloc[:, 1:] * 100
    st.subheader(f"{selected} return components by interval (%)")
    st.bar_chart(
        return_chart.set_index("Period"),
        y=["Price Return", "Dividend Return"],
    )

    st.subheader(f"{selected} market value")
    market_value_chart = detail[["interval", "MV_start", "MV_end"]].rename(
        columns={
            "interval": "Period",
            "MV_start": "Starting Market Value",
            "MV_end": "Ending Market Value",
        }
    )
    st.line_chart(
        market_value_chart.set_index("Period"),
        y=["Starting Market Value", "Ending Market Value"],
    )
    detail_display = detail.rename(
        columns={
            "ticker": "Ticker",
            "interval": "Period",
            "sector": "Sector",
            "industry": "Industry",
            "MV_start": "Starting Market Value",
            "MV_end": "Ending Market Value",
            "PNL_net": "Net Profit / Loss",
            "PNL_price": "Price Profit / Loss",
            "PNL_div": "Dividend Profit / Loss",
            "Return_price_pct": "Price Return",
            "Return_div_pct": "Dividend Return",
            "Return_net_pct": "Net Return",
        }
    )
    st.dataframe(detail_display, width="stretch", hide_index=True)


def _streamlit_secrets() -> dict:
    """Return server-only secrets without failing when no secrets file exists."""
    try:
        return st.secrets.to_dict()
    except Exception:
        return {}


def _render_chat(bundle, selected_interval: str) -> None:
    st.subheader("Ask Pulse")
    st.caption("Ask questions about this analysis in plain language.")

    try:
        config = load_llm_config(_streamlit_secrets())
    except LLMConfigurationError:
        st.info("Pulse is currently offline. Please try again in some time.")
        return

    consent = st.checkbox(
        "I agree to send normalized portfolio metrics to OpenAI to answer my questions.",
        key="llm_data_transfer_consent",
        help=(
            "Pulse sends ticker-level holdings and calculated metrics. It does "
            "not send raw CSV files, filenames, arbitrary columns, or API keys."
        ),
    )
    if not consent:
        st.info("Confirm data sharing above to use Ask Pulse.")
        return

    if st.button("Clear chat", key="clear_portfolio_chat"):
        st.session_state.chat_messages = []
        st.rerun()

    for message in st.session_state.chat_messages:
        with st.chat_message(message["role"]):
            st.text(message["content"])

    question = st.chat_input(
        "Ask a question about your portfolio analysis",
        key="portfolio_chat_input",
        max_chars=2_000,
    )
    if not question:
        return

    now = time.monotonic()
    last_call = st.session_state.get("last_llm_call_at", 0.0)
    if now - last_call < 2.0:
        st.warning("Please wait a moment before sending another question.")
        return
    st.session_state.last_llm_call_at = now

    messages = st.session_state.chat_messages
    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.text(question)

    try:
        with st.chat_message("assistant"):
            with st.spinner("Reviewing your analysis..."):
                answer = ask_portfolio_assistant(
                    config=config,
                    bundle=bundle,
                    selected_interval=selected_interval,
                    question=question,
                    history=messages[:-1],
                )
            st.text(answer)
        messages.append({"role": "assistant", "content": answer})
        st.session_state.chat_messages = messages[-20:]
    except (LLMRequestError, ValueError) as exc:
        # These exception messages are intentionally sanitized in llm_chat.py.
        st.error(str(exc))


def _render_results(bundle) -> None:
    pdf = render_pdf_report(bundle)
    header, action = st.columns([5, 1])
    with header:
        st.caption(f"Analysis results as of {bundle.as_of or date.today()}")
    with action:
        st.download_button(
            "Download PDF report",
            data=pdf,
            file_name="northstar_portfolio_report.pdf",
            mime="application/pdf",
            key="download_pdf",
            width="stretch",
        )

    interval_order = ["1M", "3M", "YTD", "1Y", "3Y"]
    available_intervals = [
        interval
        for interval in interval_order
        if any(metric.interval == interval for metric in bundle.portfolio_metrics)
    ]
    if not available_intervals:
        available_intervals = ["YTD"]
    default_index = (
        available_intervals.index("YTD") if "YTD" in available_intervals else 0
    )
    selected_interval = st.radio(
        "Time range",
        available_intervals,
        index=default_index,
        horizontal=True,
        key="selected_interval",
        help="Changes every insight below. YTD means January 1 through today.",
    )

    insight = next(
        (
            item
            for item in getattr(bundle, "insights", [])
            if item.interval == selected_interval
        ),
        None,
    )
    if insight:
        direction = "up" if insight.net_growth >= 0 else "down"
        contributor = (
            f"{insight.contributors[0].ticker} "
            f"(+\\${insight.contributors[0].pnl_net:,.2f})"
            if insight.contributors
            else "none"
        )
        drag = (
            f"{insight.detractors[0].ticker} "
            f"(-\\${abs(insight.detractors[0].pnl_net):,.2f})"
            if insight.detractors
            else "none"
        )
        st.success(
            f"**Today’s summary · {selected_interval}:** Your portfolio is {direction} "
            f"\\${abs(insight.net_growth):,.2f} ({insight.net_return_pct:+.2%}). "
            f"Biggest contributor: {contributor}. Biggest drag: {drag}."
        )

    data_tab, performance_tab, ticker_tab, chat_tab = st.tabs(
        ["Data Overview", "Performance", "Ticker Details", "Ask Pulse"]
    )
    with data_tab:
        _render_data_overview(bundle, selected_interval)
    with performance_tab:
        _render_performance(bundle, selected_interval)
    with ticker_tab:
        _render_ticker_details(bundle, selected_interval)
    with chat_tab:
        _render_chat(bundle, selected_interval)


def main() -> None:
    _init_session()
    _render_app_header()
    _sync_theme()

    if not st.session_state.disclaimer_accepted:
        _render_disclaimer_gate()
        return

    _render_analysis_app()


if __name__ == "__main__":
    main()
