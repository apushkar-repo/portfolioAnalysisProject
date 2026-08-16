"""Shared data models and constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


DISCLAIMER = (
    "This analysis is for informational and educational purposes only and is not "
    "targeted to provide financial advice or investment advice. You should rely on "
    "your own research and/or consult a qualified financial professional."
)

REQUIRED_COLUMNS = ("ticker", "avg_purchase_price", "shares")
IRR_BASELINE_POLICY = "YTD_start (Jan 1 of current year)"


@dataclass
class Holding:
    source_id: str
    ticker: str
    shares: Decimal
    avg_purchase_price: Decimal
    source_name: str = ""
    purchase_date: date | None = None
    security_type: str = ""


@dataclass
class SourceFileInfo:
    source_id: str
    filename: str
    content_hash: str
    row_count: int
    duplicate_of: str | None = None
    is_semantic_duplicate: bool = False


@dataclass
class Stage1Diagnostics:
    files_uploaded: int
    unique_sources: int
    duplicates: list[dict[str, Any]]
    redacted_field_count: int
    tickers_parsed: int
    rows_parsed: int
    warnings: list[str] = field(default_factory=list)
    sources: list[SourceFileInfo] = field(default_factory=list)


@dataclass
class IntervalWindow:
    name: str  # YTD | 3Y | 5Y
    t_start: date
    t_end: date


@dataclass
class TickerIntervalMetrics:
    ticker: str
    interval: str
    t_start: date
    t_end: date
    close_start: float
    close_end: float
    adj_start: float
    adj_end: float
    shares: float
    mv_start: float
    mv_end: float
    pnl_price: float
    pnl_div: float
    pnl_net: float
    return_price_pct: float
    return_div_pct: float
    return_net_pct: float
    irr_pct: float | None
    source_id: str | None = None
    sector: str = "Unknown"
    industry: str = "Unknown"


@dataclass
class AggregateMetrics:
    label: str
    interval: str
    mv_start: float
    mv_end: float
    pnl_price: float
    pnl_div: float
    pnl_net: float
    return_price_pct: float
    return_div_pct: float
    return_net_pct: float
    irr_pct: float | None
    num_tickers: int
    top_n_by_net_pnl: list[str] = field(default_factory=list)


@dataclass
class TickerContribution:
    ticker: str
    pnl_net: float
    return_net_pct: float


@dataclass
class IntervalInsights:
    interval: str
    portfolio_value: float
    net_growth: float
    net_return_pct: float
    contributors: list[TickerContribution]
    detractors: list[TickerContribution]
    top_three_concentration_pct: float
    largest_holding: str
    largest_holding_pct: float
    diversification_score: int
    annualized_volatility_pct: float | None
    max_drawdown_pct: float | None
    estimated_unrealized_gain: float
    estimated_income_component: float
    recent_lot_count: int
    sector_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class AnalysisBundle:
    diagnostics: Stage1Diagnostics
    holdings: list[Holding]
    intervals: list[IntervalWindow]
    ticker_metrics: list[TickerIntervalMetrics]
    portfolio_metrics: list[AggregateMetrics]
    by_source: list[AggregateMetrics]
    by_sector: list[AggregateMetrics]
    by_industry: list[AggregateMetrics] = field(default_factory=list)
    insights: list[IntervalInsights] = field(default_factory=list)
    irr_policy: str = IRR_BASELINE_POLICY
    as_of: date | None = None
