# Portfolio Analysis App — Implementation Plan

## Current state

Greenfield build from [PRD.md](PRD.md). Implementation delivered per phases below.

## Phase 0 — Software availability assessment

| Requirement | Status | Action |
|---|---|---|
| Python runtime | Done — Homebrew Python 3.12 | Project venv uses 3.12 |
| pip / venv | Done | `.venv` created |
| uv / poetry / conda | N/A | stdlib venv + pip |
| Homebrew | Available | Used for Python 3.12 |
| Node / npm | Available | Playwright e2e |
| Playwright | Done | Chromium + viewport projects |
| Git | Available | Repo initialized |
| App deps | Done | Installed in `.venv` |

**Locked product defaults**

- Market data: **yfinance** (Close + Adj Close; residual `R_div = R_net - R_price`)
- IRR when dates missing: baseline = **YTD_start** (Jan 1); 2-point annualized formula
- Currency: **single-currency (USD)** for MVP
- News: **RSS + GDELT** (no API key); `APP_ENV=test` uses fixtures
- Sentiment: FinBERT in prod; heuristic scorer in test mode
- Shorts / multi-lot ledgers: out of MVP (`shares <= 0` rejected/warned)

## Phase 1 — Bootstrap toolchain

- [x] Install Python 3.12, create `.venv`, pin dependencies
- [x] Initialize git, `.gitignore`, `README.md`, package layout

## Phase 2 — Stage 1 (ingestion)

- [x] Multi-file upload, `source_id`, PII/NPI masking
- [x] Exact + semantic dedupe, ticker normalization, validation, diagnostics

## Phase 3 — Stage 2 (performance)

- [x] yfinance history, trading-day alignment, returns / residual div / IRR
- [x] Breakdowns by source and sector; test fixtures for `APP_ENV=test`

## Phase 4 — Stage 3 (news, sentiment, recommendations)

- [x] `NewsProvider` ABC + RSS/GDELT + fixture provider
- [x] FinBERT scoring, horizon aggregation, keyword signals, Buy/Sell/Hold rules

## Phase 5 — UI + report

- [x] Streamlit UI with disclaimer, staged panels, tabs, methodology
- [x] Markdown report export

## Phase 6 — Tests + hardening

- [x] Pytest unit tests for Stage 1/2/3 logic (13 passed)
- [x] Playwright e2e with viewport screenshots under `APP_ENV=test` (3 passed)

## Implementation order

Phase 0 findings → Plan.md → bootstrap → Stage 1 → Stage 2 → Stage 3 → UI → tests.

Do not start Stage 3 live network work until fixture/`APP_ENV=test` path exists so Playwright stays deterministic.
