"""Unit tests for Stage 1 ingestion."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

import pytest

from app.stage1 import (
    MAX_CSV_BYTES,
    MAX_TOTAL_ROWS,
    MAX_UPLOAD_FILES,
    normalize_ticker,
    process_uploads,
    read_csv_lenient,
    sanitize_pii_npi,
    semantic_fingerprint,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "csv"


def test_normalize_ticker_share_class():
    assert normalize_ticker("brk b") == "BRK.B"
    assert normalize_ticker("  aapl ") == "AAPL"


def test_sanitize_pii_masks_email_and_phone():
    df = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "avg_purchase_price": [180.0],
            "shares": [10],
            "contact": ["john.doe@example.com / 555-123-4567"],
        }
    )
    out, count = sanitize_pii_npi(df)
    assert count >= 2
    assert "[REDACTED_EMAIL]" in str(out.loc[0, "contact"])
    assert "[REDACTED_PHONE]" in str(out.loc[0, "contact"])


def test_ragged_row_is_skipped_not_fatal():
    # An extra field in one row must not abort the whole upload.
    data = (
        b"ticker,avg_purchase_price,shares,purchase_date,currency,security_type\n"
        b"AAPL,172.35,12,2021-03-14,USD,stock\n"
        b"O,59.20,45,2019-06-10,USD,REIT,stock\n"
        b"SCHD,75.15,25,2020-07-16,USD,ETF\n"
    )
    df, warnings = read_csv_lenient(data, "sample.csv")
    assert df["ticker"].tolist() == ["AAPL", "SCHD"]
    assert len(warnings) == 1
    assert "skipped malformed row" in warnings[0]

    holdings, diag = process_uploads([("sample.csv", data)])
    assert {h.ticker for h in holdings} == {"AAPL", "SCHD"}
    assert next(h for h in holdings if h.ticker == "AAPL").purchase_date == date(
        2021, 3, 14
    )
    assert any("skipped malformed row" in w for w in diag.warnings)


def test_well_formed_csv_reports_no_parse_warnings():
    data = (FIXTURES / "portfolio_a.csv").read_bytes()
    df, warnings = read_csv_lenient(data, "portfolio_a.csv")
    assert warnings == []
    assert len(df) > 0


def test_invalid_purchase_date_keeps_holding_with_timeline_warning():
    data = (
        b"ticker,avg_purchase_price,shares,purchase_date\n"
        b"AAPL,180,2,not-a-date\n"
    )

    holdings, diagnostics = process_uploads([("invalid-date.csv", data)])

    assert len(holdings) == 1
    assert holdings[0].purchase_date is None
    assert any("excluded from investment timeline" in w for w in diagnostics.warnings)


def test_empty_csv_raises_readable_error():
    with pytest.raises(ValueError, match="empty or has no readable header"):
        read_csv_lenient(b"", "blank.csv")


@pytest.mark.parametrize(
    "cell",
    [
        "=2+2",
        "+SUM(A1:A2)",
        "-10",
        "@SUM(A1:A2)",
    ],
)
def test_spreadsheet_formulas_are_rejected(cell: str):
    data = (
        "ticker,avg_purchase_price,shares\n"
        f"AAPL,180,\"{cell}\"\n"
    ).encode()

    with pytest.raises(ValueError, match="spreadsheet formula"):
        process_uploads([("formula.csv", data)])


def test_macro_like_content_is_rejected():
    data = (
        b"ticker,avg_purchase_price,shares,security_type\n"
        b"AAPL,180,2,CreateObject\n"
    )

    with pytest.raises(ValueError, match="macro-like executable content"):
        process_uploads([("macro.csv", data)])


def test_csv_over_two_megabytes_is_rejected():
    data = b"ticker,avg_purchase_price,shares\n" + b"A" * MAX_CSV_BYTES

    with pytest.raises(ValueError, match="2 MB"):
        process_uploads([("large.csv", data)])


def test_non_csv_extension_is_rejected():
    data = b"ticker,avg_purchase_price,shares\nAAPL,180,2\n"

    with pytest.raises(ValueError, match=r"\.csv extension"):
        process_uploads([("portfolio.txt", data)])


def test_upload_count_is_limited():
    data = b"ticker,avg_purchase_price,shares\nAAPL,180,2\n"
    files = [(f"portfolio-{index}.csv", data) for index in range(MAX_UPLOAD_FILES + 1)]

    with pytest.raises(ValueError, match="no more than"):
        process_uploads(files)


def test_total_row_count_is_limited():
    header = "ticker,avg_purchase_price,shares\n"
    rows = "AAPL,180,2\n" * (MAX_TOTAL_ROWS + 1)

    with pytest.raises(ValueError, match="cannot exceed 2,000 rows"):
        process_uploads([("many-lots.csv", (header + rows).encode())])


def test_filename_is_replaced_with_non_identifying_label():
    data = b"ticker,avg_purchase_price,shares\nAAPL,180,2\n"

    holdings, diagnostics = process_uploads(
        [("jane-doe-account-123.csv", data)]
    )

    assert holdings[0].source_name == "Portfolio file 1"
    assert diagnostics.sources[0].filename == "Portfolio file 1"
    assert "jane-doe" not in str(diagnostics)


def test_malformed_row_warning_does_not_echo_sensitive_content():
    data = (
        b"ticker,avg_purchase_price,shares\n"
        b"AAPL,180,2\n"
        b"MSFT,200,3,person@example.com\n"
    )

    _, diagnostics = process_uploads([("portfolio.csv", data)])

    assert any("skipped malformed row" in warning for warning in diagnostics.warnings)
    assert "person@example.com" not in str(diagnostics.warnings)


def test_invalid_ticker_and_free_text_security_type_are_not_retained():
    data = (
        b"ticker,avg_purchase_price,shares,security_type\n"
        b"../../SECRET,180,2,Jane Doe account 12345\n"
        b"AAPL,180,2,Jane Doe account 12345\n"
    )

    holdings, diagnostics = process_uploads([("portfolio.csv", data)])

    assert [holding.ticker for holding in holdings] == ["AAPL"]
    assert holdings[0].security_type == ""
    assert "Jane Doe" not in str(holdings)
    assert any("invalid ticker format" in warning for warning in diagnostics.warnings)
    assert any("unsupported security type" in warning for warning in diagnostics.warnings)


def test_process_uploads_dedupes_exact():
    data = (FIXTURES / "portfolio_a.csv").read_bytes()
    holdings, diag = process_uploads(
        [("a.csv", data), ("a_copy.csv", data)]
    )
    assert diag.files_uploaded == 2
    assert diag.unique_sources == 1
    assert len(diag.duplicates) == 1
    assert diag.duplicates[0]["kind"] == "exact"
    assert {h.ticker for h in holdings} == {"AAPL", "MSFT"}


def test_process_uploads_semantic_duplicate():
    # Same content, different whitespace/row order after normalization
    a = b"ticker,avg_purchase_price,shares\nAAPL,180.25,12.5\nMSFT,300.00,5\n"
    b = b"ticker,avg_purchase_price,shares\nMSFT,300.000000,5.000000\nAAPL,180.250000,12.500000\n"
    # fingerprints should match after canonical numeric formatting
    import io
    from app.stage1 import _canonicalize_columns

    dfa = _canonicalize_columns(pd.read_csv(io.BytesIO(a)))
    dfb = _canonicalize_columns(pd.read_csv(io.BytesIO(b)))
    assert semantic_fingerprint(dfa) == semantic_fingerprint(dfb)

    holdings, diag = process_uploads([("a.csv", a), ("b.csv", b)])
    assert diag.unique_sources == 1
    assert diag.duplicates[0]["kind"] == "semantic"


def test_pii_file_redacts_before_holdings():
    data = (FIXTURES / "portfolio_pii.csv").read_bytes()
    holdings, diag = process_uploads([("pii.csv", data)])
    assert diag.redacted_field_count >= 1
    assert len(holdings) == 1
    assert holdings[0].ticker == "AAPL"
