"""Orchestrate portfolio ingestion and performance into an AnalysisBundle."""

from __future__ import annotations

from datetime import date
from typing import BinaryIO, Callable

import pandas as pd

from app.data_market import (
    compute_interval_returns,
    fetch_yfinance_history,
    interval_windows,
)
from app.insights import build_interval_insights
from app.models import IRR_BASELINE_POLICY, AnalysisBundle, Holding
from app.sector_meta import resolve_sector_industry
from app.stage1 import process_uploads


def run_analysis(
    files: list[tuple[str, bytes | BinaryIO]],
    as_of: date | None = None,
    history_provider: Callable[
        [list[str], date, date], dict[str, pd.DataFrame]
    ]
    | None = None,
    metadata_provider: Callable[[list[str]], dict[str, dict]] | None = None,
) -> AnalysisBundle:
    holdings, diagnostics = process_uploads(files)
    as_of = as_of or date.today()
    intervals = interval_windows(as_of)

    tickers = sorted({h.ticker for h in holdings})
    resolve_metadata = metadata_provider or resolve_sector_industry
    sector_map = resolve_metadata(tickers) if tickers else {}

    start_min = min(iv.t_start for iv in intervals) if intervals else as_of
    fetch_history = history_provider or fetch_yfinance_history
    prices = fetch_history(tickers, start_min, as_of) if tickers else {}

    (
        ticker_metrics,
        portfolio_metrics,
        by_source,
        by_sector,
        by_industry,
    ) = compute_interval_returns(
        holdings=holdings,
        prices=prices,
        intervals=intervals,
        sector_map=sector_map,
        as_of=as_of,
    )

    # Missing market data warnings
    missing = [t for t in tickers if t not in prices]
    for t in missing:
        diagnostics.warnings.append(f"No market data for {t}; metrics skipped.")

    insights = build_interval_insights(
        holdings=holdings,
        prices=prices,
        intervals=intervals,
        ticker_metrics=ticker_metrics,
        portfolio_metrics=portfolio_metrics,
        as_of=as_of,
    )

    return AnalysisBundle(
        diagnostics=diagnostics,
        holdings=holdings,
        intervals=intervals,
        ticker_metrics=ticker_metrics,
        portfolio_metrics=portfolio_metrics,
        by_source=by_source,
        by_sector=by_sector,
        by_industry=by_industry,
        insights=insights,
        irr_policy=IRR_BASELINE_POLICY,
        as_of=as_of,
    )
