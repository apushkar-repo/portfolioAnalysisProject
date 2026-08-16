"""Tests for downloadable PDF report generation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.report import render_markdown_report, render_pdf_report
from tests.fixture_market import run_fixture_analysis


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "csv"


def test_pdf_report_has_valid_signature_and_content() -> None:
    data = (FIXTURES / "portfolio_a.csv").read_bytes()
    bundle = run_fixture_analysis(
        [("readable-name.csv", data)],
        as_of=date(2026, 8, 14),
    )

    pdf = render_pdf_report(bundle)

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1_000
    assert b"%%EOF" in pdf[-1_024:]


def test_report_data_includes_source_comparison_and_net_growth() -> None:
    data = (FIXTURES / "portfolio_a.csv").read_bytes()
    bundle = run_fixture_analysis(
        [("readable-name.csv", data)],
        as_of=date(2026, 8, 14),
    )

    report = render_markdown_report(bundle)

    assert "### Source comparison" in report
    assert "Net Growth" in report
    assert "Portfolio file 1" in report
    assert "readable-name.csv" not in report
    assert "### By industry" in report
    assert "## Today’s summary" in report
    assert "diversification health" in report
    assert "Gains already locked in: **Tracking needed**" in report
    assert "not a dividend forecast" in report

