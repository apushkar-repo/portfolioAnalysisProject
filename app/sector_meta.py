"""Ticker sector / industry metadata resolution."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, wait
from functools import lru_cache

SAFE_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}(?:\.[A-Z0-9]{1,2})?$")
MAX_METADATA_TICKERS = 100
METADATA_WORKERS = 8
METADATA_BATCH_TIMEOUT_SECONDS = 15


def _unknown_metadata(ticker: str) -> dict:
    return {
        "sector": "Unknown",
        "industry": "Unknown",
        "company_name": ticker,
        "source": "fallback",
    }


@lru_cache(maxsize=512)
def _fetch_metadata(ticker: str) -> dict:
    import yfinance as yf

    try:
        info = yf.Ticker(ticker).info or {}
        return {
            "sector": info.get("sector") or "Unknown",
            "industry": info.get("industry") or "Unknown",
            "company_name": info.get("shortName") or info.get("longName") or ticker,
            "source": "yfinance",
        }
    except Exception:
        return _unknown_metadata(ticker)


def resolve_sector_industry(tickers: list[str]) -> dict[str, dict]:
    """Return mapping ticker -> {sector, industry, company_name, source}."""
    out: dict[str, dict] = {}
    uniq = sorted(
        {
            t.upper()
            for t in tickers
            if SAFE_TICKER_RE.fullmatch(str(t).upper())
        }
    )[:MAX_METADATA_TICKERS]

    executor = ThreadPoolExecutor(
        max_workers=METADATA_WORKERS,
        thread_name_prefix="market-metadata",
    )
    future_to_ticker = {
        executor.submit(_fetch_metadata, ticker): ticker for ticker in uniq
    }
    done, pending = wait(
        future_to_ticker,
        timeout=METADATA_BATCH_TIMEOUT_SECONDS,
    )
    for future in done:
        ticker = future_to_ticker[future]
        try:
            out[ticker] = future.result()
        except Exception:
            out[ticker] = _unknown_metadata(ticker)
    for future in pending:
        ticker = future_to_ticker[future]
        future.cancel()
        out[ticker] = _unknown_metadata(ticker)
    executor.shutdown(wait=False, cancel_futures=True)
    return out
