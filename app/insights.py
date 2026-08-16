"""Plain-language portfolio insight calculations."""

from __future__ import annotations

from datetime import date, timedelta
from math import sqrt

import pandas as pd

from app.models import (
    AggregateMetrics,
    Holding,
    IntervalInsights,
    IntervalWindow,
    TickerContribution,
    TickerIntervalMetrics,
)


def _portfolio_path(
    holdings: list[Holding],
    prices: dict[str, pd.DataFrame],
    window: IntervalWindow,
) -> pd.Series:
    shares_by_ticker: dict[str, float] = {}
    for holding in holdings:
        shares_by_ticker[holding.ticker] = (
            shares_by_ticker.get(holding.ticker, 0.0) + float(holding.shares)
        )

    values: list[pd.Series] = []
    for ticker, shares in shares_by_ticker.items():
        history = prices.get(ticker)
        if history is None or history.empty:
            continue
        column = "Adj Close" if "Adj Close" in history.columns else "Close"
        series = history[column].copy()
        series.index = pd.to_datetime(series.index).tz_localize(None)
        series = series[
            (series.index.date >= window.t_start)
            & (series.index.date <= window.t_end)
        ]
        if not series.empty:
            values.append(series.rename(ticker) * shares)

    if not values:
        return pd.Series(dtype=float)
    return pd.concat(values, axis=1).sort_index().ffill().dropna().sum(axis=1)


def _risk_measures(path: pd.Series) -> tuple[float | None, float | None]:
    if len(path) < 2:
        return None, None
    daily_returns = path.pct_change().dropna()
    volatility = (
        float(daily_returns.std() * sqrt(252))
        if len(daily_returns) >= 2
        else None
    )
    drawdown = path / path.cummax() - 1
    return volatility, abs(float(drawdown.min()))


def build_interval_insights(
    holdings: list[Holding],
    prices: dict[str, pd.DataFrame],
    intervals: list[IntervalWindow],
    ticker_metrics: list[TickerIntervalMetrics],
    portfolio_metrics: list[AggregateMetrics],
    as_of: date,
) -> list[IntervalInsights]:
    """Build understandable portfolio facts for every available time range."""
    portfolio_by_interval = {metric.interval: metric for metric in portfolio_metrics}
    total_cost = sum(
        float(holding.shares * holding.avg_purchase_price) for holding in holdings
    )
    recent_cutoff = as_of - timedelta(days=90)
    recent_lot_count = sum(
        1
        for holding in holdings
        if holding.purchase_date and recent_cutoff <= holding.purchase_date <= as_of
    )
    insights: list[IntervalInsights] = []

    for window in intervals:
        portfolio = portfolio_by_interval.get(window.name)
        if portfolio is None:
            continue
        interval_rows = [
            metric for metric in ticker_metrics if metric.interval == window.name
        ]
        grouped: dict[str, list[TickerIntervalMetrics]] = {}
        for metric in interval_rows:
            grouped.setdefault(metric.ticker, []).append(metric)

        contributions: list[TickerContribution] = []
        ending_values: dict[str, float] = {}
        sectors: dict[str, float] = {}
        for ticker, rows in grouped.items():
            pnl = sum(row.pnl_net for row in rows)
            start_value = sum(row.mv_start for row in rows)
            end_value = sum(row.mv_end for row in rows)
            contributions.append(
                TickerContribution(
                    ticker=ticker,
                    pnl_net=pnl,
                    return_net_pct=pnl / start_value if start_value else 0.0,
                )
            )
            ending_values[ticker] = end_value
            sector = rows[0].sector or "Unknown"
            sectors[sector] = sectors.get(sector, 0.0) + end_value

        ranked = sorted(contributions, key=lambda item: item.pnl_net, reverse=True)
        contributors = [item for item in ranked if item.pnl_net >= 0][:3]
        detractors = sorted(
            (item for item in ranked if item.pnl_net < 0),
            key=lambda item: item.pnl_net,
        )[:3]

        portfolio_value = sum(ending_values.values())
        weights = sorted(
            (
                (ticker, value / portfolio_value)
                for ticker, value in ending_values.items()
                if portfolio_value
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        top_three = sum(weight for _, weight in weights[:3])
        largest_ticker, largest_weight = weights[0] if weights else ("n/a", 0.0)
        hhi = sum(weight**2 for _, weight in weights)
        breadth = min(len(weights) / 10, 1.0)
        diversification_score = round(
            max(0.0, min(1.0, 0.4 * breadth + 0.6 * (1 - hhi))) * 100
        )

        path = _portfolio_path(holdings, prices, window)
        volatility, max_drawdown = _risk_measures(path)
        sector_weights = {
            sector: value / portfolio_value
            for sector, value in sorted(
                sectors.items(), key=lambda item: item[1], reverse=True
            )
            if portfolio_value
        }

        insights.append(
            IntervalInsights(
                interval=window.name,
                portfolio_value=portfolio_value,
                net_growth=portfolio.pnl_net,
                net_return_pct=portfolio.return_net_pct,
                contributors=contributors,
                detractors=detractors,
                top_three_concentration_pct=top_three,
                largest_holding=largest_ticker,
                largest_holding_pct=largest_weight,
                diversification_score=diversification_score,
                annualized_volatility_pct=volatility,
                max_drawdown_pct=max_drawdown,
                estimated_unrealized_gain=portfolio_value - total_cost,
                estimated_income_component=portfolio.pnl_div,
                recent_lot_count=recent_lot_count,
                sector_weights=sector_weights,
            )
        )

    return insights
