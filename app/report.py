"""Downloadable report generation."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models import DISCLAIMER, AnalysisBundle

_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "northstar-portfolio-logo.png"
_NAVY = colors.HexColor("#0B1739")
_BLUE = colors.HexColor("#4776E6")
_CORAL = colors.HexColor("#FF4B4B")
_LIGHT_BLUE = colors.HexColor("#EAF0FF")


def _pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.2f}%"


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _markdown_text(value: object) -> str:
    """Escape untrusted labels for safe rendering in Markdown clients."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", str(value))


def _default_insight(bundle: AnalysisBundle):
    insights = getattr(bundle, "insights", [])
    return next(
        (insight for insight in insights if insight.interval == "YTD"),
        insights[0] if insights else None,
    )


def _swing_label(volatility: float | None) -> str:
    if volatility is None:
        return "Not enough data"
    if volatility < 0.15:
        return "Lower swing"
    if volatility < 0.30:
        return "Moderate swing"
    return "Higher swing"


def render_markdown_report(bundle: AnalysisBundle) -> str:
    d = bundle.diagnostics
    source_names = {source.source_id: source.filename for source in d.sources}
    lines: list[str] = []
    lines.append("# Portfolio Pulse Report")
    lines.append("")
    lines.append("## Disclaimer")
    lines.append(DISCLAIMER)
    lines.append("")
    lines.append("## Data Overview")
    lines.append(f"- Files uploaded: **{d.files_uploaded}**")
    lines.append(f"- Unique sources used: **{d.unique_sources}**")
    lines.append(f"- Rows parsed: **{d.rows_parsed}**")
    lines.append(f"- Tickers parsed: **{d.tickers_parsed}**")
    lines.append(f"- PII/NPI fields redacted: **{d.redacted_field_count}**")
    lines.append(f"- IRR baseline policy: **{bundle.irr_policy}**")
    if d.duplicates:
        lines.append("- Duplicates:")
        for dup in d.duplicates:
            lines.append(
                f"  - `{_markdown_text(dup['filename'])}` "
                f"({_markdown_text(dup['kind'])}) → duplicate of "
                f"`{_markdown_text(source_names.get(dup['duplicate_of'], dup['duplicate_of']))}`"
            )
    if d.warnings:
        lines.append("- Warnings:")
        for w in d.warnings[:20]:
            lines.append(f"  - {_markdown_text(w)}")
    lines.append("")

    insight = _default_insight(bundle)
    if insight:
        direction = "up" if insight.net_growth >= 0 else "down"
        contributor = (
            f"{insight.contributors[0].ticker} "
            f"(+${_money(insight.contributors[0].pnl_net)})"
            if insight.contributors
            else "none"
        )
        drag = (
            f"{insight.detractors[0].ticker} "
            f"(-${_money(abs(insight.detractors[0].pnl_net))})"
            if insight.detractors
            else "none"
        )
        lines.append("## Today’s summary")
        lines.append(
            f"Your portfolio is {direction} ${_money(abs(insight.net_growth))} "
            f"({_pct(insight.net_return_pct)}) over {insight.interval}. "
            f"Biggest contributor: {contributor}. Biggest drag: {drag}."
        )
        lines.append("")
        lines.append("## What this means")
        lines.append(
            f"- Portfolio value: **${_money(insight.portfolio_value)}**"
        )
        lines.append(
            f"- Top 3 holdings: **{_pct(insight.top_three_concentration_pct)}**; "
            f"diversification health: **{insight.diversification_score}/100**"
        )
        lines.append(
            f"- Swinginess: **{_swing_label(insight.annualized_volatility_pct)}**; "
            f"worst past drop: **{_pct(insight.max_drawdown_pct)}**"
        )
        lines.append(
            f"- Estimated gains on paper: "
            f"**${_money(insight.estimated_unrealized_gain)}**"
        )
        lines.append(
            "- Gains already locked in: **Tracking needed** "
            "(sell/trade history is required)"
        )
        lines.append(
            f"- Estimated income/corporate-action component: "
            f"**${_money(insight.estimated_income_component)}** "
            "(not a dividend forecast)"
        )
        lines.append("")

    lines.append("## Performance")
    lines.append("")
    lines.append("### Portfolio summary")
    lines.append(
        "| Period | Starting Market Value | Ending Market Value | Net Growth | Price Return | Dividend Return | Net Return | IRR |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for m in bundle.portfolio_metrics:
        lines.append(
            f"| {m.interval} | {_money(m.mv_start)} | {_money(m.mv_end)} | "
            f"{_money(m.pnl_net)} | "
            f"{_pct(m.return_price_pct)} | {_pct(m.return_div_pct)} | "
            f"{_pct(m.return_net_pct)} | {_pct(m.irr_pct)} |"
        )
    lines.append("")

    lines.append("### Source comparison")
    lines.append(
        "| File Name | Period | Starting Market Value | Ending Market Value | Net Return | Net Growth | Top Contributors |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---|")
    for m in bundle.by_source:
        lines.append(
            f"| `{_markdown_text(source_names.get(m.label, m.label))}` | "
            f"{_markdown_text(m.interval)} | "
            f"{_money(m.mv_start)} | {_money(m.mv_end)} | "
            f"{_pct(m.return_net_pct)} | {_money(m.pnl_net)} | "
            f"{', '.join(_markdown_text(ticker) for ticker in m.top_n_by_net_pnl)} |"
        )
    lines.append("")

    lines.append("### By industry")
    lines.append("| Industry | Period | Net Return | Net Growth | Top Tickers |")
    lines.append("|---|---|---:|---:|---|")
    for m in bundle.by_industry:
        lines.append(
            f"| {_markdown_text(m.label)} | {_markdown_text(m.interval)} | "
            f"{_pct(m.return_net_pct)} | {_money(m.pnl_net)} | "
            f"{', '.join(_markdown_text(ticker) for ticker in m.top_n_by_net_pnl)} |"
        )
    lines.append("")

    lines.append("### Top movers (YTD)")
    ytd = next((m for m in bundle.portfolio_metrics if m.interval == "YTD"), None)
    if ytd and ytd.top_n_by_net_pnl:
        for t in ytd.top_n_by_net_pnl:
            lines.append(f"- {t}")
    else:
        lines.append("- n/a")
    lines.append("")

    return "\n".join(lines)


def _pdf_table(rows: list[list[str]], widths: list[float]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("LEADING", (0, 0), (-1, -1), 9),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_BLUE]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B9C3D7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def render_pdf_report(bundle: AnalysisBundle) -> bytes:
    """Render the analysis as a styled, portable PDF document."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch,
        title="Portfolio Pulse Report",
        author="Portfolio Pulse",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "NorthstarTitle",
        parent=styles["Title"],
        textColor=_NAVY,
        fontSize=22,
        leading=25,
        alignment=TA_LEFT,
    )
    section_style = ParagraphStyle(
        "NorthstarSection",
        parent=styles["Heading2"],
        textColor=_BLUE,
        fontSize=14,
        leading=17,
        spaceBefore=10,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "NorthstarBody",
        parent=styles["BodyText"],
        fontSize=8.5,
        leading=11,
        textColor=_NAVY,
    )
    story = []

    heading = []
    if _LOGO_PATH.exists():
        heading.append(Image(str(_LOGO_PATH), width=0.52 * inch, height=0.52 * inch))
    heading.append(
        Paragraph(
            "Portfolio Pulse Report"
            f"<br/><font size='8' color='#65728A'>Analysis as of "
            f"{escape(str(bundle.as_of or 'n/a'))}</font>",
            title_style,
        )
    )
    story.append(Table([heading], colWidths=[0.65 * inch, 8.8 * inch][: len(heading)]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Disclaimer", section_style))
    story.append(Paragraph(escape(DISCLAIMER), body_style))

    d = bundle.diagnostics
    source_names = {source.source_id: source.filename for source in d.sources}
    story.append(Paragraph("Data Overview", section_style))
    overview = [
        ["Files Uploaded", "Unique Sources", "Rows Parsed", "Tickers", "PII/NPI Redacted"],
        [
            str(d.files_uploaded),
            str(d.unique_sources),
            str(d.rows_parsed),
            str(d.tickers_parsed),
            str(d.redacted_field_count),
        ],
    ]
    story.append(_pdf_table(overview, [1.35 * inch] * 5))
    story.append(Spacer(1, 7))
    story.append(
        Paragraph(f"<b>IRR baseline policy:</b> {escape(bundle.irr_policy)}", body_style)
    )

    if d.duplicates:
        story.append(Spacer(1, 5))
        story.append(Paragraph("<b>Duplicates</b>", body_style))
        for duplicate in d.duplicates:
            original = source_names.get(
                duplicate["duplicate_of"], duplicate["duplicate_of"]
            )
            story.append(
                Paragraph(
                    f"• {escape(duplicate['filename'])} ({escape(duplicate['kind'])}) "
                    f"— duplicate of {escape(original)}",
                    body_style,
                )
            )
    if d.warnings:
        story.append(Spacer(1, 5))
        story.append(Paragraph("<b>Validation warnings</b>", body_style))
        for warning in d.warnings[:20]:
            story.append(Paragraph(f"• {escape(warning)}", body_style))

    insight = _default_insight(bundle)
    if insight:
        direction = "up" if insight.net_growth >= 0 else "down"
        contributor = (
            f"{insight.contributors[0].ticker} "
            f"(+${_money(insight.contributors[0].pnl_net)})"
            if insight.contributors
            else "none"
        )
        drag = (
            f"{insight.detractors[0].ticker} "
            f"(-${_money(abs(insight.detractors[0].pnl_net))})"
            if insight.detractors
            else "none"
        )
        story.append(Paragraph("Today’s Summary", section_style))
        story.append(
            Paragraph(
                f"Your portfolio is {direction} "
                f"<b>${_money(abs(insight.net_growth))} "
                f"({_pct(insight.net_return_pct)})</b> over "
                f"{escape(insight.interval)}. Biggest contributor: "
                f"{escape(contributor)}. Biggest drag: {escape(drag)}.",
                body_style,
            )
        )
        story.append(Paragraph("What This Means", section_style))
        insight_rows = [
            [
                "Portfolio Value",
                "Top 3 Holdings",
                "Diversification",
                "Swinginess",
                "Worst Past Drop",
                "Gains on Paper",
            ],
            [
                f"${_money(insight.portfolio_value)}",
                _pct(insight.top_three_concentration_pct),
                f"{insight.diversification_score}/100",
                _swing_label(insight.annualized_volatility_pct),
                _pct(insight.max_drawdown_pct),
                f"${_money(insight.estimated_unrealized_gain)}",
            ],
        ]
        story.append(_pdf_table(insight_rows, [1.25 * inch] * 6))
        story.append(Spacer(1, 5))
        story.append(
            Paragraph(
                "Gains already locked in: <b>tracking needed</b> (sell/trade "
                "history is required). Estimated income/corporate-action "
                f"component: <b>${_money(insight.estimated_income_component)}</b>; "
                "this is not a dividend forecast.",
                body_style,
            )
        )

    story.append(Paragraph("Portfolio Performance", section_style))
    portfolio_rows = [
        [
            "Period",
            "Starting Market Value",
            "Ending Market Value",
            "Net Growth",
            "Price Return",
            "Dividend Return",
            "Net Return",
            "IRR",
        ]
    ]
    portfolio_rows.extend(
        [
            m.interval,
            f"${_money(m.mv_start)}",
            f"${_money(m.mv_end)}",
            f"${_money(m.pnl_net)}",
            _pct(m.return_price_pct),
            _pct(m.return_div_pct),
            _pct(m.return_net_pct),
            _pct(m.irr_pct),
        ]
        for m in bundle.portfolio_metrics
    )
    story.append(
        _pdf_table(
            portfolio_rows,
            [
                0.55 * inch,
                1.15 * inch,
                1.15 * inch,
                0.95 * inch,
                0.8 * inch,
                0.9 * inch,
                0.8 * inch,
                0.7 * inch,
            ],
        )
    )

    story.append(Paragraph("Source Comparison", section_style))
    source_rows = [
        [
            "File Name",
            "Period",
            "Starting Value",
            "Ending Value",
            "Net Return",
            "Net Growth",
            "Top Contributors",
        ]
    ]
    source_rows.extend(
        [
            source_names.get(m.label, m.label),
            m.interval,
            f"${_money(m.mv_start)}",
            f"${_money(m.mv_end)}",
            _pct(m.return_net_pct),
            f"${_money(m.pnl_net)}",
            ", ".join(m.top_n_by_net_pnl) or "n/a",
        ]
        for m in bundle.by_source
    )
    story.append(
        _pdf_table(
            source_rows,
            [
                1.55 * inch,
                0.55 * inch,
                1.0 * inch,
                1.0 * inch,
                0.8 * inch,
                0.9 * inch,
                2.0 * inch,
            ],
        )
    )

    story.append(Paragraph("Performance by Industry", section_style))
    sector_rows = [
        ["Industry", "Period", "Net Return", "Net Growth", "Top Tickers"]
    ]
    sector_rows.extend(
        [
            m.label,
            m.interval,
            _pct(m.return_net_pct),
            f"${_money(m.pnl_net)}",
            ", ".join(m.top_n_by_net_pnl) or "n/a",
        ]
        for m in bundle.by_industry
    )
    story.append(
        _pdf_table(
            sector_rows,
            [2.1 * inch, 0.65 * inch, 0.9 * inch, 1.15 * inch, 2.75 * inch],
        )
    )

    ytd = next((m for m in bundle.portfolio_metrics if m.interval == "YTD"), None)
    story.append(Paragraph("Top YTD Contributors", section_style))
    story.append(
        Paragraph(
            escape(", ".join(ytd.top_n_by_net_pnl) if ytd and ytd.top_n_by_net_pnl else "n/a"),
            body_style,
        )
    )

    doc.build(story)
    return buffer.getvalue()
