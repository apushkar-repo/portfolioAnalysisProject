"""Unit tests for returns and IRR math."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from app.data_market import (
    compute_interval_returns,
    interval_windows,
    two_point_irr,
)
from app.models import Holding
from tests.fixture_market import fixture_history


def test_prd_example_returns():
    """PRD example: start 50, end 60, div residual via adj."""
    # Price return 20%; if adj goes 50 -> 62, net=24%, div=4%
    hist = pd.DataFrame(
        {
            "Close": [50.0, 60.0],
            "Adj Close": [50.0, 62.0],
        },
        index=pd.to_datetime(["2026-01-02", "2026-08-11"]),
    )
    holdings = [
        Holding(
            source_id="s_0",
            ticker="XYZ",
            shares=Decimal("100"),
            avg_purchase_price=Decimal("50"),
        )
    ]
    prices = {"XYZ": hist}
    as_of = date(2026, 8, 11)
    intervals = [iv for iv in interval_windows(as_of) if iv.name == "YTD"]
    metrics, port, _, _, _ = compute_interval_returns(
        holdings, prices, intervals=intervals, as_of=as_of
    )
    assert len(metrics) == 1
    m = metrics[0]
    assert abs(m.return_price_pct - 0.20) < 1e-9
    assert abs(m.return_net_pct - 0.24) < 1e-9
    assert abs(m.return_div_pct - 0.04) < 1e-9
    assert abs(m.pnl_price - 1000) < 1e-6
    assert abs(m.pnl_div - 200) < 1e-6
    assert abs(m.pnl_net - 1200) < 1e-6
    assert abs(port[0].return_net_pct - 0.24) < 1e-9


def test_two_point_irr():
    # Double money in 1 year => 100% IRR
    assert abs(two_point_irr(-100, 200, 1.0) - 1.0) < 1e-12
    assert two_point_irr(0, 100, 1.0) is None


def test_user_facing_time_ranges_are_available():
    assert [window.name for window in interval_windows(date(2026, 8, 11))] == [
        "1M",
        "3M",
        "YTD",
        "1Y",
        "3Y",
    ]


def test_fixture_market_fetch():
    prices = fixture_history(
        ["AAPL", "MSFT"],
        date(2021, 1, 1),
        date(2026, 8, 11),
    )
    assert "AAPL" in prices
    assert "Close" in prices["AAPL"].columns
    assert "Adj Close" in prices["AAPL"].columns
