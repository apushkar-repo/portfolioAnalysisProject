"""Tests for bounded outbound market-data behavior."""

from __future__ import annotations

import sys
import time
from datetime import date
from types import SimpleNamespace

import pandas as pd

import app.sector_meta as sector_meta
from app.data_market import fetch_yfinance_history


def test_market_history_uses_one_batch_without_per_ticker_fallback(
    monkeypatch,
) -> None:
    calls = {"download": 0, "ticker": 0}

    def download(**kwargs):
        calls["download"] += 1
        return pd.DataFrame()

    def ticker(*args, **kwargs):
        calls["ticker"] += 1
        raise AssertionError("Per-ticker network fallback must not run")

    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(download=download, Ticker=ticker),
    )

    result = fetch_yfinance_history(
        ["AAPL", "MSFT"],
        date(2026, 1, 1),
        date(2026, 8, 1),
    )

    assert result == {}
    assert calls == {"download": 1, "ticker": 0}


def test_metadata_batch_returns_after_global_timeout(monkeypatch) -> None:
    def slow_metadata(ticker: str):
        time.sleep(0.2)
        return {"sector": "Late"}

    monkeypatch.setattr(sector_meta, "_fetch_metadata", slow_metadata)
    monkeypatch.setattr(sector_meta, "METADATA_BATCH_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    result = sector_meta.resolve_sector_industry(["AAPL", "MSFT"])
    elapsed = time.monotonic() - started

    assert elapsed < 0.15
    assert result["AAPL"]["sector"] == "Unknown"
    assert result["MSFT"]["sector"] == "Unknown"
