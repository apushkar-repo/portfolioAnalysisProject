# Portfolio Pulse

Streamlit app that ingests portfolio CSV exports, sanitizes/deduplicates them,
and explains portfolio performance over 1M / 3M / YTD / 1Y / 3Y ranges in
plain language. It highlights dollar changes, contributors and detractors,
concentration, diversification, historical swinginess and drawdown, estimated
gains on paper, educational review prompts, and a server-side LLM assistant
across Data Overview, Performance, Ticker Details, and Ask Northstar tabs.

See [PRD.md](PRD.md) and [Plan.md](Plan.md).

## Requirements

- Python 3.11+ (3.12 recommended)
- Node.js 18+ (for Playwright e2e only)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
cp .env.example .env
```

## Secure LLM configuration

The chat integration runs on the Streamlit server. The browser never receives
the provider API key. Configure one of these server-side options:

```bash
# Deployment environment (recommended)
export LLM_API_KEY="..."
export LLM_MODEL="gpt-4.1-mini"

# OPENAI_API_KEY is also supported.
```

For local Streamlit development, copy the ignored secrets template:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Then replace the placeholder in `.streamlit/secrets.toml`. That file and `.env`
are excluded by `.gitignore`; never put real keys in `.env.example`,
`secrets.toml.example`, source code, browser JavaScript, or a CSV.

The integration connects only to OpenAI's SDK endpoint; custom base URLs are
not accepted. The assistant sends normalized holdings and analysis metrics
only—not raw CSV bytes, source filenames, arbitrary uploaded columns, or
credentials. Requests have prompt/history limits, a server timeout, a
per-session cooldown, and a process-wide quota.

`requirements.lock` pins and hashes the complete dependency tree. Regenerate it
after intentionally updating `requirements.txt` or `requirements-dev.txt`:

```bash
pip-compile --generate-hashes --allow-unsafe \
  --output-file=requirements.lock requirements-dev.txt
```

## Run

From the project root (with the venv active):

```bash
source .venv/bin/activate
streamlit run run_app.py
```

Or:

```bash
streamlit run app/ui.py
```

If you previously hit `No module named 'app'`, reinstall the package into the venv once:

```bash
pip install -e .
```

## Tests

```bash
# Unit
pytest tests/ -q

# E2E (the test harness injects fixtures; the app has no test mode)
cd e2e && npm install && npx playwright install chromium
npx playwright test
```

## CSV schema

Required columns: `ticker`, `avg_purchase_price`, `shares`

Optional columns: `purchase_date`, `security_type`, `currency`

`security_type` enables the ETF/stock holdings filters. Realized gains and
forward dividend forecasts require transaction and income data that this
holdings-snapshot schema does not currently collect; the app labels those
figures as tracking needed rather than estimating them.

Uploads are restricted to UTF-8 `.csv` files, 2 MB per file, 10 files,
10 MB combined, 2,000 rows, and 100 unique tickers. Any spreadsheet formula,
macro, or macro-like executable content causes the entire file to be rejected.
Original filenames and unsupported/free-text columns are not retained.
