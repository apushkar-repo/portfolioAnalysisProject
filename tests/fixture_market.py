"""Deterministic market providers used only by tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from app.pipeline import run_analysis

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "market"


def fixture_history(
    tickers: Iterable[str],
    start: date,
    end: date,
) -> dict[str, pd.DataFrame]:
    del start, end
    output: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        path = FIXTURES_DIR / f"{ticker.upper()}.json"
        if not path.exists():
            path = FIXTURES_DIR / "GENERIC.json"
        payload = json.loads(path.read_text())
        frame = pd.DataFrame(payload["rows"])
        frame["Date"] = pd.to_datetime(frame["Date"])
        output[ticker.upper()] = frame.set_index("Date").sort_index()
    return output


def fixture_metadata(tickers: list[str]) -> dict[str, dict]:
    payload = json.loads((FIXTURES_DIR / "sectors.json").read_text())
    return {
        ticker: payload.get(
            ticker,
            {
                "sector": "Unknown",
                "industry": "Unknown",
                "company_name": ticker,
                "source": "fixture",
            },
        )
        for ticker in tickers
    }


def run_fixture_analysis(files, as_of: date):
    return run_analysis(
        files,
        as_of=as_of,
        history_provider=fixture_history,
        metadata_provider=fixture_metadata,
    )
