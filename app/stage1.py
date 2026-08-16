"""Stage 1: CSV ingestion, PII/NPI masking, deduplication, normalization."""

from __future__ import annotations

import hashlib
import io
import csv
import re
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

import pandas as pd

from app.models import Holding, SourceFileInfo, Stage1Diagnostics

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
SENSITIVE_HEADER_RE = re.compile(
    r"(email|e-mail|phone|ssn|social|account|tax.?id|passport|address|name|dob)",
    re.I,
)
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_TOTAL_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ROWS = 2_000
MAX_UNIQUE_TICKERS = 100
TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}(?:\.[A-Z0-9]{1,2})?$")
FORMULA_PREFIXES = ("=", "+", "-", "@")
MACRO_RE = re.compile(
    r"\b(?:auto_open|workbook_open|document_open|createobject|wscript\.shell|"
    r"powershell|cmd\.exe|sub\s+\w+|function\s+\w+)\b",
    re.IGNORECASE,
)
ALLOWED_COLUMNS = {
    "ticker",
    "avg_purchase_price",
    "shares",
    "purchase_date",
    "currency",
    "security_type",
}
SECURITY_TYPES = {
    "stock": "Stock",
    "equity": "Stock",
    "etf": "ETF",
    "reit": "REIT",
    "fund": "Fund",
    "bond": "Bond",
    "cash": "Cash",
    "option": "Option",
}

COLUMN_ALIASES = {
    "symbol": "ticker",
    "Ticker": "ticker",
    "avg_price": "avg_purchase_price",
    "average_purchase_price": "avg_purchase_price",
    "purchase_price": "avg_purchase_price",
    "qty": "shares",
    "quantity": "shares",
    "share_count": "shares",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_source_id(content_hash: str, index: int) -> str:
    return f"{content_hash[:16]}_{index}"


def normalize_ticker(raw: str) -> str:
    t = str(raw).strip().upper()
    t = re.sub(r"\s+", " ", t)
    # Common share-class variants: "BRK B" / "BRK-B" -> "BRK.B"
    t = t.replace("-", ".")
    if " " in t:
        parts = t.split(" ")
        if len(parts) == 2 and len(parts[1]) <= 2:
            t = f"{parts[0]}.{parts[1]}"
        else:
            t = "".join(parts)
    return t


def validate_csv_safety(data: bytes, label: str) -> None:
    """Reject executable spreadsheet content before pandas parses any rows."""
    if len(data) > MAX_CSV_BYTES:
        raise ValueError(f"{label} exceeds the 2 MB CSV file limit.")
    if b"\x00" in data:
        raise ValueError(f"{label} is not a plain-text CSV file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be a UTF-8 plain-text CSV file.") from exc

    try:
        rows = csv.reader(io.StringIO(text))
        for row_number, row in enumerate(rows, start=1):
            for cell in row:
                value = cell.lstrip()
                if value.startswith(FORMULA_PREFIXES):
                    raise ValueError(
                        f"{label} was rejected because row {row_number} contains "
                        "a spreadsheet formula."
                    )
                if MACRO_RE.search(value):
                    raise ValueError(
                        f"{label} was rejected because row {row_number} contains "
                        "macro-like executable content."
                    )
    except csv.Error as exc:
        raise ValueError(f"{label} is not a valid CSV file.") from exc


def _mask_value(value: object) -> tuple[object, int]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return value, 0
    text = str(value)
    count = 0
    new = text
    for pattern, token in (
        (EMAIL_RE, "[REDACTED_EMAIL]"),
        (SSN_RE, "[REDACTED_ID]"),
        (PHONE_RE, "[REDACTED_PHONE]"),
        (CC_RE, "[REDACTED_ID]"),
    ):
        matches = pattern.findall(new)
        if matches:
            count += len(matches)
            new = pattern.sub(token, new)
    return (new if count else value), count


def sanitize_pii_npi(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Mask PII/NPI in-memory before any persistence or logging."""
    out = df.copy()
    redacted = 0
    for col in out.columns:
        header_hit = bool(SENSITIVE_HEADER_RE.search(str(col)))
        series = out[col]
        if header_hit and series.dtype == object:
            out[col] = "[REDACTED_COLUMN]"
            redacted += int(series.notna().sum())
            continue
        if series.dtype == object or header_hit:
            masked_vals = []
            for v in series:
                mv, c = _mask_value(v)
                redacted += c
                masked_vals.append(mv)
            out[col] = masked_vals
    return out, redacted


def read_csv_lenient(data: bytes, source_name: str = "") -> tuple[pd.DataFrame, list[str]]:
    """Parse CSV bytes, skipping malformed rows instead of failing the upload.

    A single ragged row (for example an extra comma in one line) would otherwise
    abort the whole analysis. Bad rows are dropped and reported the same way
    invalid values are, so the user sees what was ignored in Stage 1 diagnostics.
    """
    label = source_name or "file"
    validate_csv_safety(data, label)
    try:
        return pd.read_csv(io.BytesIO(data)), []
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"{label} is empty or has no readable header row.") from exc
    except pd.errors.ParserError:
        pass

    bad_lines: list[list[str]] = []

    def _collect(line: list[str]) -> None:
        bad_lines.append(line)
        return None

    try:
        # The callable form of on_bad_lines needs the Python engine, which is
        # slower but lets us report exactly which rows were dropped.
        df = pd.read_csv(io.BytesIO(data), engine="python", on_bad_lines=_collect)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        raise ValueError(f"Could not parse {label} as CSV.") from exc

    warnings = [
        f"{label}: skipped malformed row with {len(line)} fields."
        for line in bad_lines
    ]
    return df, warnings


def _canonicalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for c in df.columns:
        key = str(c).strip()
        lower = key.lower().strip()
        if lower in COLUMN_ALIASES:
            rename[c] = COLUMN_ALIASES[lower]
        elif lower in ("ticker", "avg_purchase_price", "shares"):
            rename[c] = lower
        else:
            rename[c] = lower
    return df.rename(columns=rename)


def semantic_fingerprint(df: pd.DataFrame) -> str:
    cols = [c for c in ("ticker", "avg_purchase_price", "shares") if c in df.columns]
    if not cols:
        return sha256_bytes(b"empty")
    tmp = df[cols].copy()
    tmp["ticker"] = tmp["ticker"].map(normalize_ticker)
    tmp["avg_purchase_price"] = pd.to_numeric(tmp["avg_purchase_price"], errors="coerce").round(6)
    tmp["shares"] = pd.to_numeric(tmp["shares"], errors="coerce").round(6)
    tmp = tmp.sort_values(cols).reset_index(drop=True)
    payload = tmp.to_csv(index=False).encode("utf-8")
    return sha256_bytes(payload)


def normalize_holdings(
    df: pd.DataFrame, source_id: str, source_name: str = ""
) -> tuple[list[Holding], list[str]]:
    warnings: list[str] = []
    required = {"ticker", "avg_purchase_price", "shares"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns for {source_name or source_id}: {sorted(missing)}")

    holdings: list[Holding] = []
    for idx, row in df.iterrows():
        try:
            ticker = normalize_ticker(row["ticker"])
            if not ticker or ticker == "NAN":
                warnings.append(f"Row {idx}: empty ticker skipped")
                continue
            if not TICKER_RE.fullmatch(ticker):
                warnings.append(f"Row {idx}: invalid ticker format skipped")
                continue
            shares = Decimal(str(row["shares"]))
            price = Decimal(str(row["avg_purchase_price"]))
        except (InvalidOperation, TypeError, ValueError):
            warnings.append(f"Row {idx}: invalid numeric values")
            continue
        if shares <= 0:
            warnings.append(f"Row {idx} ({ticker}): shares must be > 0; skipped")
            continue
        if price <= 0:
            warnings.append(f"Row {idx} ({ticker}): avg_purchase_price must be > 0; skipped")
            continue

        purchase_date = None
        raw_purchase_date = row.get("purchase_date")
        if pd.notna(raw_purchase_date) and str(raw_purchase_date).strip():
            parsed_date = pd.to_datetime(raw_purchase_date, errors="coerce")
            if pd.isna(parsed_date):
                warnings.append(
                    f"Row {idx} ({ticker}): invalid purchase_date; "
                    "excluded from investment timeline"
                )
            else:
                purchase_date = parsed_date.date()

        raw_security_type = (
            str(row.get("security_type", "")).strip().lower()
            if pd.notna(row.get("security_type"))
            else ""
        )
        security_type = SECURITY_TYPES.get(raw_security_type, "")
        if raw_security_type and not security_type:
            warnings.append(
                f"Row {idx} ({ticker}): unsupported security type ignored"
            )

        holdings.append(
            Holding(
                source_id=source_id,
                ticker=ticker,
                shares=shares,
                avg_purchase_price=price,
                source_name=source_name,
                purchase_date=purchase_date,
                security_type=security_type,
            )
        )
    return holdings, warnings


def process_uploads(
    files: list[tuple[str, bytes | BinaryIO]],
) -> tuple[list[Holding], Stage1Diagnostics]:
    """
    Process 1..N uploaded CSVs.

    Each item is (filename, file_bytes_or_stream).
    """
    if not files:
        raise ValueError("Upload at least one CSV file.")
    if len(files) > MAX_UPLOAD_FILES:
        raise ValueError(f"Upload no more than {MAX_UPLOAD_FILES} CSV files at once.")

    parsed: list[tuple[SourceFileInfo, pd.DataFrame, str]] = []
    total_redacted = 0
    all_warnings: list[str] = []
    hash_to_source: dict[str, str] = {}
    fingerprint_to_source: dict[str, str] = {}
    total_rows = 0

    total_bytes = 0
    for index, (filename, raw) in enumerate(files):
        if not str(filename).lower().endswith(".csv"):
            raise ValueError(f"Portfolio file {index + 1} must use the .csv extension.")
        data = raw.read() if hasattr(raw, "read") else raw
        assert isinstance(data, (bytes, bytearray))
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise ValueError("Combined CSV uploads cannot exceed 10 MB.")
        display_name = f"Portfolio file {index + 1}"
        content_hash = sha256_bytes(bytes(data))
        source_id = make_source_id(content_hash, index)

        df, parse_warnings = read_csv_lenient(
            bytes(data),
            source_name=display_name,
        )
        all_warnings.extend(parse_warnings)
        df = _canonicalize_columns(df)
        df, redacted = sanitize_pii_npi(df)
        total_redacted += redacted
        df = df[[column for column in df.columns if column in ALLOWED_COLUMNS]]
        total_rows += len(df)
        if total_rows > MAX_TOTAL_ROWS:
            raise ValueError(
                f"Combined CSV uploads cannot exceed {MAX_TOTAL_ROWS:,} rows."
            )

        duplicate_of = None
        is_semantic = False
        if content_hash in hash_to_source:
            duplicate_of = hash_to_source[content_hash]
        else:
            hash_to_source[content_hash] = source_id
            fp = semantic_fingerprint(df)
            if fp in fingerprint_to_source:
                duplicate_of = fingerprint_to_source[fp]
                is_semantic = True
            else:
                fingerprint_to_source[fp] = source_id

        info = SourceFileInfo(
            source_id=source_id,
            filename=display_name,
            content_hash=content_hash,
            row_count=len(df),
            duplicate_of=duplicate_of,
            is_semantic_duplicate=is_semantic,
        )
        parsed.append((info, df, display_name))

    holdings: list[Holding] = []
    duplicates_report: list[dict] = []
    unique_sources = 0

    for info, df, filename in parsed:
        if info.duplicate_of:
            duplicates_report.append(
                {
                    "source_id": info.source_id,
                    "filename": filename,
                    "duplicate_of": info.duplicate_of,
                    "kind": "semantic" if info.is_semantic_duplicate else "exact",
                }
            )
            continue
        unique_sources += 1
        hs, warns = normalize_holdings(df, info.source_id, source_name=filename)
        holdings.extend(hs)
        all_warnings.extend(warns)

    tickers = {h.ticker for h in holdings}
    if len(tickers) > MAX_UNIQUE_TICKERS:
        raise ValueError(
            f"Portfolio contains more than {MAX_UNIQUE_TICKERS} unique tickers."
        )
    diagnostics = Stage1Diagnostics(
        files_uploaded=len(files),
        unique_sources=unique_sources,
        duplicates=duplicates_report,
        redacted_field_count=total_redacted,
        tickers_parsed=len(tickers),
        rows_parsed=len(holdings),
        warnings=all_warnings,
        sources=[p[0] for p in parsed],
    )
    return holdings, diagnostics
