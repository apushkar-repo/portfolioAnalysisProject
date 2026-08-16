"""Market data fetch and performance / IRR calculations."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from app.models import (
    AggregateMetrics,
    Holding,
    IntervalWindow,
    TickerIntervalMetrics,
)

SAFE_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}(?:\.[A-Z0-9]{1,2})?$")
MAX_MARKET_TICKERS = 100


def interval_windows(as_of: date | None = None) -> list[IntervalWindow]:
    today = as_of or date.today()
    return [
        IntervalWindow("1M", today - timedelta(days=30), today),
        IntervalWindow("3M", today - timedelta(days=90), today),
        IntervalWindow("YTD", date(today.year, 1, 1), today),
        IntervalWindow("1Y", today - timedelta(days=365), today),
        IntervalWindow("3Y", today - timedelta(days=365 * 3), today),
    ]


def irr_baseline_date(as_of: date | None = None) -> date:
    today = as_of or date.today()
    return date(today.year, 1, 1)


def _align_prices(hist: pd.DataFrame, t_start: date, t_end: date) -> tuple[pd.Series, pd.Series] | None:
    if hist is None or hist.empty:
        return None
    hist = hist.copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    start_rows = hist[hist.index.date >= t_start]
    end_rows = hist[hist.index.date <= t_end]
    if start_rows.empty or end_rows.empty:
        return None
    return start_rows.iloc[0], end_rows.iloc[-1]


def two_point_irr(cf0: float, cf1: float, years: float) -> float | None:
    """Annualized 2-point IRR: (CF1 / -CF0)^(1/years) - 1."""
    if years <= 0 or cf0 >= 0 or cf1 <= 0:
        return None
    try:
        return (cf1 / (-cf0)) ** (1.0 / years) - 1.0
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def years_between(start: date, end: date) -> float:
    return max((end - start).days / 365.25, 1e-6)


def fetch_yfinance_history(
    tickers: Iterable[str],
    start: date,
    end: date,
) -> dict[str, pd.DataFrame]:
    tickers = sorted(
        {
            t.upper()
            for t in tickers
            if SAFE_TICKER_RE.fullmatch(str(t).upper())
        }
    )[:MAX_MARKET_TICKERS]
    out: dict[str, pd.DataFrame] = {}

    import yfinance as yf

    # One bounded batch request only. Per-ticker fallbacks would let one upload
    # amplify into up to 100 additional network calls.
    if not tickers:
        return out
    start_s = start.isoformat()
    end_s = (end + timedelta(days=1)).isoformat()
    try:
        data = yf.download(
            tickers=tickers,
            start=start_s,
            end=end_s,
            group_by="ticker",
            auto_adjust=False,
            threads=False,
            progress=False,
            timeout=10,
        )
    except Exception:
        data = None

    for t in tickers:
        hist = None
        try:
            if data is not None and not data.empty:
                if len(tickers) == 1:
                    hist = data.copy()
                elif t in data.columns.get_level_values(0):
                    hist = data[t].dropna(how="all")
            if hist is not None and not hist.empty:
                # Normalize column names
                cols = {c: str(c).title().replace(" ", "") for c in hist.columns}
                hist = hist.rename(columns=cols)
                if "Adjclose" in hist.columns and "Adj Close" not in hist.columns:
                    hist = hist.rename(columns={"Adjclose": "Adj Close"})
                if "Close" in hist.columns:
                    if "Adj Close" not in hist.columns:
                        hist["Adj Close"] = hist["Close"]
                    out[t] = hist[["Close", "Adj Close"]].dropna(how="all")
        except Exception:
            continue
    return out


def aggregate_holdings_by_ticker(holdings: list[Holding]) -> dict[str, dict]:
    """Combine lots across sources for portfolio-level ticker view."""
    agg: dict[str, dict] = {}
    for h in holdings:
        shares = float(h.shares)
        price = float(h.avg_purchase_price)
        if h.ticker not in agg:
            agg[h.ticker] = {"shares": 0.0, "cost": 0.0}
        agg[h.ticker]["shares"] += shares
        agg[h.ticker]["cost"] += shares * price
    for t, v in agg.items():
        v["avg_purchase_price"] = v["cost"] / v["shares"] if v["shares"] else 0.0
    return agg


def _metrics_for_lot(
    ticker: str,
    shares: float,
    avg_price: float,
    interval: IntervalWindow,
    hist: pd.DataFrame,
    baseline: date,
    source_id: str | None = None,
    sector: str = "Unknown",
    industry: str = "Unknown",
) -> TickerIntervalMetrics | None:
    aligned = _align_prices(hist, interval.t_start, interval.t_end)
    if aligned is None:
        return None
    start_row, end_row = aligned
    close_start = float(start_row["Close"])
    close_end = float(end_row["Close"])
    adj_start = float(start_row.get("Adj Close", close_start))
    adj_end = float(end_row.get("Adj Close", close_end))

    if close_start == 0 or adj_start == 0:
        return None

    r_price = (close_end - close_start) / close_start
    r_net = (adj_end - adj_start) / adj_start
    r_div = r_net - r_price

    pnl_price = shares * (close_end - close_start)
    pnl_net = shares * (adj_end - adj_start)
    pnl_div = pnl_net - pnl_price

    mv_start = shares * adj_start
    mv_end = shares * adj_end

    # Interval IRR uses cost basis at YTD baseline policy (global),
    # valuing at interval end adj close.
    cf0 = -shares * avg_price
    cf1 = shares * adj_end
    irr = two_point_irr(cf0, cf1, years_between(baseline, interval.t_end))

    return TickerIntervalMetrics(
        ticker=ticker,
        interval=interval.name,
        t_start=interval.t_start,
        t_end=interval.t_end,
        close_start=close_start,
        close_end=close_end,
        adj_start=adj_start,
        adj_end=adj_end,
        shares=shares,
        mv_start=mv_start,
        mv_end=mv_end,
        pnl_price=pnl_price,
        pnl_div=pnl_div,
        pnl_net=pnl_net,
        return_price_pct=r_price,
        return_div_pct=r_div,
        return_net_pct=r_net,
        irr_pct=irr,
        source_id=source_id,
        sector=sector,
        industry=industry,
    )


def _aggregate(
    label: str,
    interval: str,
    rows: list[TickerIntervalMetrics],
    top_n: int = 5,
) -> AggregateMetrics:
    if not rows:
        return AggregateMetrics(
            label=label,
            interval=interval,
            mv_start=0.0,
            mv_end=0.0,
            pnl_price=0.0,
            pnl_div=0.0,
            pnl_net=0.0,
            return_price_pct=0.0,
            return_div_pct=0.0,
            return_net_pct=0.0,
            irr_pct=None,
            num_tickers=0,
        )
    mv_start = sum(r.mv_start for r in rows)
    mv_end = sum(r.mv_end for r in rows)
    pnl_price = sum(r.pnl_price for r in rows)
    pnl_div = sum(r.pnl_div for r in rows)
    pnl_net = sum(r.pnl_net for r in rows)
    r_price = pnl_price / mv_start if mv_start else 0.0
    r_div = pnl_div / mv_start if mv_start else 0.0
    r_net = pnl_net / mv_start if mv_start else 0.0

    irr = None
    if rows and all(r.irr_pct is not None for r in rows):
        weights = [abs(r.mv_start) for r in rows]
        wsum = sum(weights) or 1.0
        irr = sum((r.irr_pct or 0.0) * w for r, w in zip(rows, weights)) / wsum

    ranked = sorted(rows, key=lambda r: r.pnl_net, reverse=True)
    top = [r.ticker for r in ranked[:top_n]]
    return AggregateMetrics(
        label=label,
        interval=interval,
        mv_start=mv_start,
        mv_end=mv_end,
        pnl_price=pnl_price,
        pnl_div=pnl_div,
        pnl_net=pnl_net,
        return_price_pct=r_price,
        return_div_pct=r_div,
        return_net_pct=r_net,
        irr_pct=irr,
        num_tickers=len({r.ticker for r in rows}),
        top_n_by_net_pnl=top,
    )


def portfolio_irr_from_holdings(
    holdings: list[Holding],
    prices: dict[str, pd.DataFrame],
    interval: IntervalWindow,
    baseline: date,
) -> float | None:
    cf0 = 0.0
    cf1 = 0.0
    for h in holdings:
        hist = prices.get(h.ticker)
        if hist is None:
            continue
        aligned = _align_prices(hist, interval.t_start, interval.t_end)
        if aligned is None:
            continue
        _, end_row = aligned
        adj_end = float(end_row.get("Adj Close", end_row["Close"]))
        shares = float(h.shares)
        cf0 += -shares * float(h.avg_purchase_price)
        cf1 += shares * adj_end
    return two_point_irr(cf0, cf1, years_between(baseline, interval.t_end))


def compute_interval_returns(
    holdings: list[Holding],
    prices: dict[str, pd.DataFrame],
    intervals: list[IntervalWindow] | None = None,
    sector_map: dict[str, dict] | None = None,
    as_of: date | None = None,
) -> tuple[
    list[TickerIntervalMetrics],
    list[AggregateMetrics],
    list[AggregateMetrics],
    list[AggregateMetrics],
    list[AggregateMetrics],
]:
    intervals = intervals or interval_windows(as_of)
    baseline = irr_baseline_date(as_of)
    sector_map = sector_map or {}

    ticker_metrics: list[TickerIntervalMetrics] = []

    # Per-source lots
    for h in holdings:
        hist = prices.get(h.ticker)
        if hist is None:
            continue
        meta = sector_map.get(h.ticker, {})
        for iv in intervals:
            m = _metrics_for_lot(
                ticker=h.ticker,
                shares=float(h.shares),
                avg_price=float(h.avg_purchase_price),
                interval=iv,
                hist=hist,
                baseline=baseline,
                source_id=h.source_id,
                sector=meta.get("sector", "Unknown"),
                industry=meta.get("industry", "Unknown"),
            )
            if m:
                ticker_metrics.append(m)

    portfolio_metrics: list[AggregateMetrics] = []
    by_source: list[AggregateMetrics] = []
    by_sector: list[AggregateMetrics] = []
    by_industry: list[AggregateMetrics] = []

    for iv in intervals:
        rows = [m for m in ticker_metrics if m.interval == iv.name]
        # Aggregate by ticker first for portfolio (sum lots)
        by_ticker: dict[str, list[TickerIntervalMetrics]] = {}
        for m in rows:
            by_ticker.setdefault(m.ticker, []).append(m)
        combined: list[TickerIntervalMetrics] = []
        for t, group in by_ticker.items():
            # Sum dollar fields; recompute returns from sums
            shares = sum(g.shares for g in group)
            mv_start = sum(g.mv_start for g in group)
            mv_end = sum(g.mv_end for g in group)
            pnl_price = sum(g.pnl_price for g in group)
            pnl_div = sum(g.pnl_div for g in group)
            pnl_net = sum(g.pnl_net for g in group)
            g0 = group[0]
            combined.append(
                TickerIntervalMetrics(
                    ticker=t,
                    interval=iv.name,
                    t_start=g0.t_start,
                    t_end=g0.t_end,
                    close_start=g0.close_start,
                    close_end=g0.close_end,
                    adj_start=g0.adj_start,
                    adj_end=g0.adj_end,
                    shares=shares,
                    mv_start=mv_start,
                    mv_end=mv_end,
                    pnl_price=pnl_price,
                    pnl_div=pnl_div,
                    pnl_net=pnl_net,
                    return_price_pct=pnl_price / mv_start if mv_start else 0.0,
                    return_div_pct=pnl_div / mv_start if mv_start else 0.0,
                    return_net_pct=pnl_net / mv_start if mv_start else 0.0,
                    irr_pct=group[0].irr_pct,
                    sector=g0.sector,
                    industry=g0.industry,
                )
            )
        port = _aggregate("Portfolio", iv.name, combined)
        port.irr_pct = portfolio_irr_from_holdings(holdings, prices, iv, baseline)
        portfolio_metrics.append(port)

        sources = {m.source_id for m in rows if m.source_id}
        for sid in sorted(sources):
            by_source.append(_aggregate(sid or "unknown", iv.name, [m for m in rows if m.source_id == sid]))

        sectors = {m.sector or "Unknown" for m in combined}
        for sec in sorted(sectors):
            by_sector.append(
                _aggregate(sec, iv.name, [m for m in combined if (m.sector or "Unknown") == sec])
            )

        industries = {m.industry or "Unknown" for m in combined}
        for industry in sorted(industries):
            by_industry.append(
                _aggregate(
                    industry,
                    iv.name,
                    [m for m in combined if (m.industry or "Unknown") == industry],
                )
            )

    return ticker_metrics, portfolio_metrics, by_source, by_sector, by_industry


def momentum_returns(
    hist: pd.DataFrame, as_of: date | None = None
) -> dict[str, float]:
    """3M and 12M adj-close momentum."""
    today = as_of or date.today()
    out = {"m3": 0.0, "m12": 0.0}
    if hist is None or hist.empty:
        return out
    for key, days in (("m3", 90), ("m12", 365)):
        start = today - timedelta(days=days)
        aligned = _align_prices(hist, start, today)
        if not aligned:
            continue
        s, e = aligned
        a0 = float(s.get("Adj Close", s["Close"]))
        a1 = float(e.get("Adj Close", e["Close"]))
        if a0:
            out[key] = (a1 - a0) / a0
    return out
