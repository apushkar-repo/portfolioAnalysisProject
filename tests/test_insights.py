"""Tests for plain-language portfolio insights."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.fixture_market import run_fixture_analysis


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "csv"


def test_insights_cover_ranges_concentration_risk_and_drivers() -> None:
    files = [
        ("portfolio_a.csv", (FIXTURES / "portfolio_a.csv").read_bytes()),
        ("portfolio_b.csv", (FIXTURES / "portfolio_b.csv").read_bytes()),
    ]

    bundle = run_fixture_analysis(files, as_of=date(2026, 8, 14))

    assert [insight.interval for insight in bundle.insights] == [
        "1M",
        "3M",
        "YTD",
        "1Y",
        "3Y",
    ]
    ytd = next(insight for insight in bundle.insights if insight.interval == "YTD")
    assert ytd.portfolio_value > 0
    assert 0 <= ytd.top_three_concentration_pct <= 1
    assert 0 <= ytd.diversification_score <= 100
    assert ytd.annualized_volatility_pct is not None
    assert ytd.max_drawdown_pct is not None
    assert ytd.max_drawdown_pct >= 0
    assert ytd.contributors or ytd.detractors
    assert abs(ytd.estimated_unrealized_gain) > 0
