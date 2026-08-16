Portfolio Analysis Tool 

## Overview

You want an app that ingests one or more portfolio CSV exports, cleanses/validates them (including PII/NPI masking), deduplicates uploads, then computes performance metrics (YTD calendar, last 3 years, last 5 years) using multiple return notions (price total return, dividends/bonuses total return, net total return, IRR). It also segments by **sector/industry** derived from tickers, and produces **web-sourced news/analyst sentiment** plus **short/medium/long term** trading recommendations (buy/sell/hold) with rationale.

Below is a detailed, implementation-ready design: data model, pipeline stages, calculations, algorithms, and output schemas. Where exact inputs are missing, I specify required assumptions and fallback behavior.

---

## Assumptions & Required Inputs (Clarify/Enforce)

### CSV columns (minimum)
Each CSV (per “source”) must contain, per row/ticker:
- `ticker` (string)
- `avg_purchase_price` (number) — average purchase price
- `shares` (number) — number of shares held

### Optional but strongly recommended
- `purchase_date` or `transaction_date` per lot (or at least year/month)
- `cost_basis` (if avg purchase price isn’t enough)
- `currency` (otherwise assume a default currency)
- `security_type` (equity/ETF/bond/etc.) if you have it; otherwise infer from ticker universe

### If purchase dates are missing (for IRR)
You previously asked: “IRR requires dates; if dates are missing, use a best-available approximation.” Your app should implement a configurable fallback, e.g.:

**IRR fallback strategy options**
1. **Uniform date assumption (default):** assume all shares were purchased at the earliest available date in the dataset (or at `YYYY-01-01` of the earliest year present).
2. **Average-date assumption:** if the CSV includes multiple rows per ticker/lots (even without explicit dates), assume equal distribution across the time range derived from dataset.
3. **User-provided policy:** app UI asks: “What date should be used for IRR if purchase dates are absent?” with default `earliest_year-01-01`.

Your app must persist the assumption used and display it in the report.

---

## Stage 1 — Ingestion, Sanitization, Deduplication, Source Differentiation

### 1.1 File ingestion
- User uploads 1..N CSV files.
- Assign each uploaded file a `source_id` = hash of file bytes + sequential index, e.g.:
  - `source_id = SHA256(file_content_bytes)[:16] + "_" + upload_index`

### 1.2 PII/NPI masking (robust)
Even if your schema is portfolio-related, CSVs sometimes include:
- names, emails, phone numbers, addresses (PII)
- account numbers, tax IDs, SSNs, policy IDs (NPI)
- any other sensitive strings

Implement:
1. **Column-level detection**
   - Scan header + sample values with regex patterns:
     - Emails, phone numbers, addresses heuristics, SSN patterns
     - Credit card patterns, passport patterns
     - National ID patterns (configurable per country)
2. **Value-level detection**
   - Replace detected strings with placeholders:
     - `"[REDACTED_EMAIL]"`, `"[REDACTED_PHONE]"`, `"[REDACTED_ID]"`
3. **Hashing strategy for non-reversible identifiers**
   - If you need consistent identification, replace with `SHA256(value)[:10]` rather than redaction.

**Important:** Masking must occur before storing any extracted raw rows in logs/storage.

### 1.3 Parse & normalize tickers
- Normalize ticker symbol:
  - uppercase, remove whitespace
  - map common variants (e.g., “BRK B” vs “BRK.B”) to a standardized format
- Normalize numeric fields:
  - shares: float -> store as Decimal
  - avg_purchase_price: float -> store as Decimal
- Validate:
  - shares > 0 (or allow negative for sells; if sells exist, you’ll need transaction ledger logic—see “extensions”)
  - avg_purchase_price > 0

### 1.4 Detect duplicate uploads (same CSV uploaded twice)
Deduplicate by:
- **Exact byte match:** identical file hash -> duplicates detected.
- Optionally **semantic match:** if hashes differ but content is identical (rare), compute a normalized dataframe fingerprint:
  - sort rows by ticker, stringify with normalized numeric formatting; hash.

**Behavior:**
- If duplicates detected, keep only one physical file instance but preserve differentiation metadata:
  - mark as `duplicate_of = source_id_original`
- The requirement says “no duplication and differentiation based on source csv.”
  - Practical compromise: treat duplicates as the same source for computation but report them as duplicates in “Stage 1 diagnostics”.

### 1.5 Differentiation by source
You must keep source-specific holding records:
- `Holdings[source_id][ticker] = {shares, avg_purchase_price}`
- If the same ticker appears in multiple sources, you treat them as separate “lots” by source for IRR (if dates exist) and cost basis calculations.

### 1.6 Map tickers to sector/industry
Create a `TickerMetadata` table:
- `ticker`
- `company_name` (optional)
- `sector`
- `industry`
- `source` of classification (e.g., your data provider)
- `as_of_date`

If you lack a provider, you’ll need web scraping or a licensed dataset; you should formalize this.

---

## Stage 2 — Performance Metrics (YTD, Last 3Y, Last 5Y) + Breakdowns

### 2.1 What you must fetch from market data
To compute returns you need time series:
- **Adjusted close prices** (or close + total return factors)
- **Dividend distributions**
- **Bonus distributions** if applicable (often “stock splits” and “stock dividends” are represented via corporate actions)
- **Risk-free rate** for IRR interpretation? (optional; IRR itself doesn’t require it, but reporting “annualized IRR” and comparisons do.)

Use a market data provider or compute from corporate actions feed.

**Key concept:** “Total Return (Price)” vs “Total Return on Dividends/Bonus” vs “Net Total Return”
Your earlier definitions are a bit ambiguous because “price total return” usually means price change only, “total return” includes dividends/splits. You asked for multiple components anyway, so we define:

#### Definitions used in app (recommended, explicit)
Let:
- \( P_t \) = adjusted price including splits and dividends (provider-adjusted).
- \( C_t \) = close price (unadjusted), if you store it.
- \( D_{i} \) = dividend/bonus cash or equivalent at time \( i \).

Then define:
1. **Price Total Return (Price-only):**
   - \( R^{price} = \frac{C_{end}-C_{start}}{C_{start}} \)
   - Note: if you don’t have “split-adjusted close,” then splits distort. Use split-adjusted close for consistency.
2. **Dividend/Bonus Total Return:**
   - \( R^{div} = \frac{DIV\_value}{C_{start}} \)
   - where \( DIV\_value = \sum_i \frac{D_i}{\text{split factor at } start} \) in currency terms per share.
3. **Net Total Return (recommended to match portfolio P&L):**
   - \( R^{net} = R^{price} + R^{div} \)
   - (i.e., includes dividends/bonuses and price change. If you have fees/taxes, subtract them for “net”. Since fees/taxes aren’t in CSV, keep “net” as “after dividends/bonuses”, not after taxes.)

If you want a more provider-native metric, you can use “Adjusted close return” as total return and decompose using corporate action components if the provider provides them.

### 2.2 Portfolio holdings valuation over time
You have:
- average purchase price and shares
- but no transaction dates, unless provided

For performance over intervals (YTD/3Y/5Y) we need start and end valuation:
- **Start market value:** \( MV_{start} = shares \times C_{start} \)
- **End market value:** \( MV_{end} = shares \times C_{end} \)

But you also need cost basis for IRR and potentially “gain/loss”.

#### Value over time (for return ratios)
- Total return can be computed purely from market prices:
  - \( R_{interval} = \frac{MV_{end} - MV_{start} + DIV\_value}{MV_{start}} \)
- This avoids needing purchase price.

However, you need **IRR** and **net return vs cost**:
- Cost at baseline:
  - \( Cost = shares \times avg\_purchase\_price \)
- Then compute cash flow series for IRR.

### 2.3 Time window definitions
Let:
- `today` = current date (in app runtime)
- `YTD_start` = Jan 1 of current calendar year
- `YTD_end` = today (or last market close)
- `last3Y_start` = today minus 3 years
- `last5Y_start` = today minus 5 years
- Ensure all start/end map to valid trading days (use nearest previous close).

### 2.4 Required computations (per ticker, per interval)

For each ticker \( j \) and interval \([t_s, t_e]\):

#### 2.4.1 Price-only return
\[
R^{price}_{j} = \frac{C_{j,t_e} - C_{j,t_s}}{C_{j,t_s}}
\]

#### 2.4.2 Dividends/bonus return
If you can compute total dividend cash per share in interval:
- \( div\_per\_share = \sum_{k \in [t_s, t_e]} D_{j,k} \) (in currency/share at that time)
Then:
\[
R^{div}_{j} = \frac{div\_per\_share}{C_{j,t_s}}
\]

#### 2.4.3 Net total return
\[
R^{net}_{j} = R^{price}_{j} + R^{div}_{j}
\]

#### 2.4.4 Dollar P&L components (for portfolio aggregation)
Let shares = \( s_j \).
- Price P&L: \( PNL^{price}_j = s_j (C_{t_e} - C_{t_s}) \)
- Div P&L: \( PNL^{div}_j = s_j \cdot div\_per\_share \)
- Net P&L: \( PNL^{net}_j = PNL^{price}_j + PNL^{div}_j \)

### 2.5 Portfolio aggregation
For total portfolio:
- \( MV_{start} = \sum_j s_j \cdot C_{j,t_s} \)
- \( PNL^{net} = \sum_j PNL^{net}_j \)

Thus:
\[
R^{net}_{portfolio} = \frac{PNL^{net}}{MV_{start}}
\]
Similarly for price-only and dividend-only.

### 2.6 Performance by “source CSV”
You must compute the same metrics but aggregated only across tickers from a given source:
- For each `source_id`, compute:
  - \( MV^{(source)}_{start} \)
  - \( PNL^{(source)} \)
  - returns
Then show portfolio totals + source contributions:
- Contribution by source in dollars:
  - \( \text{contrib}^{source} = PNL^{net}_{source} \)
- Weight by source at start:
  - \( w^{source} = \frac{MV^{(source)}_{start}}{MV_{portfolio,start}} \)

### 2.7 Performance by sector/industry
Map each ticker to sector/industry.
Then group:
- For sector \( S \):
  - \( MV_{start,S} = \sum_{j \in S} s_j C_{j,t_s} \)
  - \( PNL_{S}^{net} = \sum_{j \in S} PNL_{j}^{net} \)
  - \( R^{net}_{S} = PNL_{S}^{net}/MV_{start,S} \)

Also include:
- top movers by dollars and by percent
- share concentration:
  - each sector’s weight at start and end.

### 2.8 IRR calculation (portfolio and per ticker)

#### What IRR needs
Cash flows:
- At purchase time(s): negative cash outflow
- At end time: positive cash inflow equal to liquidation value (or market value)

If you have **transaction dates**:
- Build cash flow schedule per ticker across all lots:
  - For each lot \( l \): purchase date \( t_{l} \), amount \( -avg\_purchase\_price_l \cdot shares_l \)
- End cash flow \( +shares\_total \cdot C_{t_e} \)
Then compute IRR:
- Annualized IRR using NPV=0:
\[
0 = \sum_i \frac{CF_i}{(1+r)^{\Delta t_i}}
\]
Where \( \Delta t_i \) is years from first cash flow.

In implementation:
- Use IRR solver (e.g., Newton-Raphson or bisection) with bounds like [-0.99, 10.0] and handle non-convergence.

#### If you *don’t* have dates (your current CSV)
Apply fallback purchase date \( t_{purchase} \) per ticker:
- Default: earliest start date among the interval being reported OR earliest year in dataset.

To produce IRR for each interval:
- It’s common to use cash flows at beginning of the interval, not actual historical purchase.
So your app should define IRR horizon in a consistent way:

**Recommended for holdings snapshots:**
- “IRR since baseline” = IRR that transforms:
  - initial outflow = shares * avg_purchase_price (at baseline date)
  - final inflow = shares * C_end
- baseline date can be:
  - earliest among YTD_start/last3Y_start/last5Y_start, depending on which IRR you’re computing.

But this would make IRR depend on baseline date choice.

Therefore, your app should produce:
- **IRR (approx.) since purchase** using fallback purchase date once (global)
- and/or **IRR over each interval** using interval baseline date.

To keep outputs stable, pick one and document.

### 2.9 Metrics to output (recommended fields)

For each interval (YTD, 3Y, 5Y) and for each breakdown (Total, by source, by sector):
- `MV_start`
- `MV_end`
- `PNL_price`
- `PNL_div`
- `PNL_net`
- `Return_price_pct`
- `Return_div_pct`
- `Return_net_pct`
- `IRR_pct` (if computable with fallback)
- `Num_tickers`
- `Top_n_by_net_pnl` (ticker list)

---

## Stage 3 — Sentiment Analysis (Web-sourced) + Short/Medium/Long Recommendations

### 3.1 Input universe
Sentiment is per ticker, based on:
- ticker-specific news and analyst reports
- broader market + macro + geopolitical context that impacts the ticker’s sector/industry and the market

### 3.2 Web sourcing pipeline
Implement a news ingestion module that can:
- query per ticker:
  - “{ticker} analyst upgrade downgrade”
  - “{ticker} earnings guidance”
  - “{ticker} acquisition”
  - “{sector} outlook”
- scrape or use an API and store only derived text and timestamps.

### 3.3 Text processing + sentiment model
Because “sentiment analysis” must be explainable, do:

1. **Entity resolution**
   - Confirm article relevance to ticker
2. **Sentiment extraction**
   - Use a classifier that outputs:
     - polarity (positive/neutral/negative)
     - confidence
   - Also extract **signals**:
     - “earnings beat/miss”
     - “guidance raised/lowered”
     - “macro headwind/tailwind”
     - “regulatory/geopolitical risk”
3. **Time decay weighting**
   - More recent articles get higher weights:
     - weight \( w_i = e^{-(t_{now}-t_i)/\tau} \)

4. **Aggregate sentiment score**
   - Let each article have score in [-1,1].
   - Weighted average:
\[
S_j = \frac{\sum_i w_i \cdot score_i}{\sum_i w_i}
\]
Then map to bands:
- `S_j > +0.25` => Bullish
- `S_j < -0.25` => Bearish
- else Neutral

### 3.4 Combine sentiment with valuation/technical signals (optional but useful)
Your CSV doesn’t have valuation ratios, but your app can compute:
- momentum (price trend over 3/6/12 months)
- volatility (ATR or stdev)
- drawdown
- relative strength vs sector or benchmark

If you want to keep scope minimal, you can base recommendations primarily on sentiment + trend.

### 3.5 Recommendation logic for time horizons
Define three horizon types:

- **Short term (3–6 months):** driven by near-term catalysts (earnings, guidance, macro surprises)
- **Medium (1–3 years):** driven by business trajectory, analyst targets, sector cycle
- **Long (3+ years):** driven by fundamentals, structural trends, and long-run risk factors

#### A rule-based decision framework (practical to implement)
For each ticker \( j \), compute:

- `Sentiment_short` from articles in last ~3-6 months
- `Sentiment_medium` from last ~6-24 months
- `Sentiment_long` from last ~18-36+ months and persistent themes
- `Trend_score` (price momentum)
- `Risk_score` (volatility + negative geopolitical/regulatory signals)

Then produce:
- `Action_short`: Buy/Sell/Hold
- `Action_medium`: Buy/Sell/Hold
- `Action_long`: Buy/Sell/Hold

Example decision mapping:
- If sentiment strongly bullish and risk low and trend positive:
  - Buy (short/medium)
- If bearish sentiment or major negative catalyst:
  - Sell (short) / Hold or Sell (medium)
- If mixed/neutral:
  - Hold or small buy only if risk low

### 3.6 Connect recommendations to the user’s current holdings
Your recommendation should consider portfolio exposure:
- If user already holds a high concentration in a sector/ticker:
  - reduce “Buy” aggressiveness
- If user has zero holdings:
  - “Buy” means initiate position
- If you include tax considerations later, you’ll need more data.

### 3.7 Required output content per ticker
For each ticker in the portfolio universe:
- `current_position`:
  - shares total
  - avg purchase price (per source and total)
- `sentiment_summary`:
  - short/medium/long sentiment band
  - key positives (2–4 bullets derived from article signals)
  - key negatives (2–4 bullets)
- `recommendation`:
  - action_short/medium/long (Buy/Sell/Hold)
  - rationale paragraphs:
    - cite themes (earnings, guidance, macro, geopolitics)
    - avoid making guarantees

---

## Output Format (Markdown) + API Response Schema

### 4.1 Report sections
Your app should render a single report with markdown:

1. **Disclaimer**
2. **Stage 1 Diagnostics**
   - uploaded file list
   - duplicate detection results
   - schema summary (tickers parsed, rows counts)
   - PII/NPI masking counts (number of redacted fields)
3. **Stage 2 Performance**
   - Summary table: Total portfolio returns for YTD / 3Y / 5Y
   - Breakdown by source:
     - each source’s returns + contribution
   - Breakdown by sector/industry:
     - returns + weights + top contributors
   - Top movers:
     - tickers with largest net P&L
4. **Stage 3 Sentiment + Recommendations**
   - For each ticker:
     - sentiment band by horizon
     - recommendation by horizon
     - rationale + key catalysts
   - Portfolio-level themes:
     - sectors with bullish/bearish tilt

### 4.2 Suggested data structures

#### Internal normalized holdings
```json
{
  "source_id": "....",
  "ticker": "AAPL",
  "shares": 12.5,
  "avg_purchase_price": 180.25
}
```

#### Market time series store
```json
{
  "ticker": "AAPL",
  "interval": "YTD",
  "t_start": "2026-01-01",
  "t_end": "2026-08-11",
  "close_start": 180.1,
  "close_end": 220.3,
  "dividend_per_share_sum": 1.98,
  "price_total_return": 0.223,
  "dividend_total_return": 0.011,
  "net_total_return": 0.234
}
```

#### Recommendation output
```json
{
  "ticker": "AAPL",
  "position": { "shares_total": 12.5, "avg_purchase_price_weighted": 180.25 },
  "sentiment": {
    "short": { "band": "Bullish", "score": 0.41 },
    "medium": { "band": "Neutral", "score": 0.08 },
    "long": { "band": "Bullish", "score": 0.29 }
  },
  "recommendation": {
    "short": { "action": "Buy", "rationale": "Earnings..." },
    "medium": { "action": "Hold", "rationale": "..." },
    "long": { "action": "Buy", "rationale": "..." }
  },
  "key_signals": {
    "positives": ["..."],
    "negatives": ["..."]
  }
}
```

---

## Detailed Calculation Examples (for correctness)

Assume one ticker `XYZ`, shares = 100.

### Example interval (YTD)
- \( C_{start} = 50 \)
- \( C_{end} = 60 \)
- Price-only:
  - \( R^{price} = (60-50)/50 = 0.20 = 20\% \)
- Dividends/bonus total per share:
  - \( div\_per\_share = 2 \)
- Dividend-only return:
  - \( R^{div} = 2/50 = 0.04 = 4\% \)
- Net total:
  - \( R^{net} = 24\% \)

Dollar P&L:
- Price P&L = \(100(60-50)=1000\)
- Div P&L = \(100*2=200\)
- Net P&L = 1200
- MV_start = 100*50=5000
- Net return = 1200/5000=24%

### Portfolio aggregation
If portfolio has two tickers:
- MV_start = MV_start1 + MV_start2
- Net P&L = P&L1 + P&L2
- Return = Net P&L / MV_start

This ensures correct weighting.

---

## Implementation Architecture (Recommended)

### Services/modules
1. **Upload + Storage**
   - store raw files temporarily (encrypted), then discard after parsing if policy allows
2. **CSV Parser + Validator**
   - schema inference + strict mode
3. **Sanitizer (PII/NPI)**
   - regex + classification
4. **Deduplicator**
   - file hash + fingerprint
5. **Holdings Normalizer**
   - per-source holdings table
6. **Market Data Fetcher**
   - prices + corporate actions/dividends per ticker for time windows
7. **Return Calculator**
   - per ticker per interval decompositions
8. **Metadata Resolver**
   - ticker -> sector/industry
9. **Web News Fetcher**
   - per ticker queries + time windows
10. **NLP Extraction**
   - relevance + sentiment scoring + key signal extraction
11. **Recommendation Engine**
   - rule-based + weighted sentiment + trend and risk
12. **Report Generator (Markdown)**
   - produce the final output

### Data persistence
Store:
- sanitized/normalized holdings (PII-free)
- computed metrics
- cached market data and news results (key by ticker+date range)

---

## Edge Cases & Required Handling

- **Tickers not found in metadata provider:** mark as `Unknown sector`, exclude from sector breakdown or group under “Unknown”.
- **Corporate action complexity:** dividends vs stock splits should be handled via adjusted prices and corporate actions feed.
- **Non-US tickers / FX:** if the provider returns prices in local currency, you must either:
  - convert using FX rates, or
  - restrict to a single currency dataset.
- **Negative shares / short positions:** if shares can be negative, IRR and returns must treat sign carefully; you may need a ledger approach.
- **Currency mismatch:** require `currency` column or app setting.
- **Market closed days:** use last available close before `t_end` and closest close at/after `t_start` depending on your convention.

---

## Security & Privacy Controls (App-level)
Even though you asked to mask PII/NPI from CSVs:
- don’t write raw uploads to logs
- sanitize before persistence
- encrypt at rest
- role-based access to reports
- if you cache news data, store only derived features + minimal quotes

(If you want, I can specify a concrete threat model and which logs must be redacted.)

---

## Questions to finalize before implementation (only the essential ones)

1. **Market data provider plan:** Will the app use an external API (and which one), or do you want to supply prices/dividends yourself?
2. **Currency handling:** Are all tickers in the same currency? If not, do you want base currency conversion (e.g., to USD)?
3. **IRR policy:** When dates are missing, which fallback should be default?
   - earliest year’s `Jan 1`, or
   - YTD_start / 3Y_start / 5Y_start per interval (interval-specific baseline)?
4. **Corporate action decomposition feasibility:** Do you require strict decomposition into price vs dividend components, or is “net total return” from adjusted prices acceptable with only approximate dividend attribution?

Answer these and I’ll lock the spec into an exact computation plan and then give you the implementation blueprint (API endpoints, database schema, and pseudocode for each stage).



# Portfolio Analysis App — Product + Technical Design (Streamlit, Python, yfinance, News + Sentiment, Playwright)

## 1) Purpose & User Outcomes
The app ingests one or more user-uploaded CSV files describing a stock portfolio (ticker, average purchase price, shares), then produces:

1. **Stage 1: Data Review & Sanitization**
   - Validate CSV schema
   - Mask PII/NPI values if detected
   - Detect duplicate uploads
   - Preserve **source differentiation** (each uploaded CSV treated as a different “source” for reporting)
2. **Stage 2: Portfolio Performance**
   - Compute portfolio performance using:
     - **Price return**
     - **Dividend/bonus component**
     - **Net return**
     - **IRR**
   - Time windows:
     - **YTD (calendar)**: Jan 1 → today
     - **Last 3 years** and **Last 5 years**
   - Breakdowns:
     - **By source CSV**
     - **By sector/industry** (from ticker metadata resolution)
3. **Stage 3: News-based Sentiment & Recommendations**
   - Fetch web news / analyst coverage for each ticker (open-source approach)
   - Run local **open-source sentiment scoring**
   - Summarize sentiment by horizon:
     - Short: **3–6 months**
     - Medium: **1–3 years**
     - Long: **3+ years**
   - Provide **buy/sell/hold** guidance per horizon with rationale tied to fetched signals

---

## 2) Non-Financial-Advice Disclaimer (Mandatory UI)
The report and UI must always include:

- “This analysis is for informational and educational purposes only and is not targeted to provide financial advice or investment advice. You should rely on your own research and/or consult a qualified financial professional.”

---

## 3) Core Definitions (So Metrics Are Consistent)

### 3.1 Returns & Decomposition (Implemented with yfinance)
yfinance provides **Adj Close** which includes corporate action adjustments (including splits and often dividend effects via total return adjustment).

To produce your required breakdown in a robust, internally consistent way:

For a ticker `j` and interval `[t_start, t_end]` with shares `s_j`:

- `Close_start` and `Close_end`: unadjusted close
- `Adj_start` and `Adj_end`: adjusted close

**A) Price Total Return (Price-only)**
\[
R^{price}_{j} = \frac{Close_{end} - Close_{start}}{Close_{start}}
\]

**B) Net Total Return (using Adjusted Close)**
\[
R^{net}_{j} = \frac{Adj_{end} - Adj_{start}}{Adj_{start}}
\]

**C) Dividend/Bonus Component (Residual)**
\[
R^{div}_{j} = R^{net}_{j} - R^{price}_{j}
\]

This avoids brittle attribution while still providing the “dividends/bonus component” your UI expects.

**Dollar components**
- Price P&L: \( PNL^{price}_{j} = s_j (Close_{end}-Close_{start}) \)
- Net P&L: \( PNL^{net}_{j} = s_j (Adj_{end}-Adj_{start}) \)
- Div/bonus P&L component:
  \[
  PNL^{div}_{j} = PNL^{net}_{j} - PNL^{price}_{j}
  \]

**Portfolio aggregation**
\[
MV_{start} = \sum_j s_j \cdot Adj_{start,j}
\]
\[
PNL^{net}_{portfolio} = \sum_j PNL^{net}_{j}
\]
\[
R^{net}_{portfolio} = \frac{PNL^{net}_{portfolio}}{MV_{start}}
\]
(and analogous for price + div components)

### 3.2 IRR (With Missing Purchase Dates)
Because CSV includes `avg_purchase_price` and `shares` but not lot dates, IRR must use a **fallback baseline purchase date**.

**Default IRR baseline policy (configurable in UI):**
- Let the earliest interval start among enabled windows be the “baseline date”
  - If you compute IRR for the whole run, baseline could be `min(YTD_start, last3_start, last5_start)` depending on which IRR you report.
- Default choice if ambiguous:
  - **Use YTD_start as IRR baseline** (Jan 1 of current year)

**Cashflows per ticker for interval IRR** (baseline → interval end):
- `CF0 = -shares * avg_purchase_price` at baseline date
- `CF1 = shares * AdjClose_end` at `t_end`
- 2-point annualized return approximation:
  - years = (t_end - baseline)/365.25
\[
IRR \approx \left(\frac{CF1}{-CF0}\right)^{1/years} - 1
\]

**Portfolio IRR**
- Compute an IRR at portfolio level with aggregated `CF0` and `CF1` using the same 2-point approximation:
  - `CF0_port = sum(CF0_j)`
  - `CF1_port = sum(CF1_j)`

> This is an approximation consistent with the limited input schema. If you later add transaction dates, you can upgrade to true multi-cashflow IRR.

---

## 4) CSV Schema Requirements & Validation

### 4.1 Required columns
Minimum per row:
- `ticker` (string)
- `avg_purchase_price` (number)
- `shares` (number)

### 4.2 Optional columns (future-proof)
- `purchase_date` / `transaction_date`
- `currency`
- `security_type`

### 4.3 Validation Rules
- Ticker: non-empty, convertible to standardized format
- shares: must be numeric, and (default) `shares > 0`
  - If negative shares appear, treat as short positions; you must then handle signs in market value and IRR logic (can be a later feature).
- avg_purchase_price: numeric > 0

### 4.4 Normalization
- Uppercase tickers
- Trim whitespace
- Numeric fields parsed as `Decimal` for accuracy

---

## 5) Stage 1: Data Review, PII/NPI Masking, Deduplication, Source Differentiation

### 5.1 Source differentiation requirement
Each uploaded file is assigned a `source_id`:
- `source_id = SHA256(file_bytes)[:16] + "_" + index`

We maintain:
- `holdings[source_id]` grouped by ticker

### 5.2 Deduplication logic
Detect duplicates to prevent double counting.

**A) Exact duplicate**
- If `SHA256(file_bytes)` matches, mark `duplicate_of` the original `source_id`.

**B) Optional semantic duplicate**
- Fingerprint dataframe after normalization (ticker uppercase, numeric canonical formatting).
- If similarity exceeds threshold, mark as semantic duplicate.

**Reporting**
- In Stage 1 diagnostics show:
  - number of files uploaded
  - duplicates found
  - number of unique sources used for computation (deduplicated)

### 5.3 PII/NPI masking
Implement:
1. Detect suspicious columns:
   - Scan headers and sampled values
2. Detect sensitive values:
   - regex patterns for emails, phone numbers, SSNs, credit cards, addresses
3. Mask in-memory before:
   - logging
   - caching to disk
   - persisting to session state

**Masking behavior**
- Replace detected values with stable tokens:
  - `[REDACTED_EMAIL]`, `[REDACTED_PHONE]`, `[REDACTED_ID]`
- For non-sensitive identifiers that need stable grouping, use hashing.

**Storage rule**
- Never store raw sensitive strings in logs or cached artifacts.

---

## 6) Stage 2: Market Data + Performance Metrics

### 6.1 yfinance data fetching
For each ticker:
- Fetch historical daily data for at least:
  - `t_start_min = min(YTD_start, last3_start, last5_start)`
  - to `t_end = today`
- Use:
  - `Adj Close` series for net return
  - `Close` series for price-only return

Dividends:
- Optionally fetch `Ticker(t).dividends` and compute dividend per share.
- However, the app’s metric decomposition uses the residual approach:
  - `R_div = R_net - R_price`
- That ensures internal consistency regardless of dividends feed completeness.

### 6.2 Trading day alignment
Because markets close on weekends/holidays:
- Pick:
  - `idx_start` = first date `>= t_start`
  - `idx_end` = last date `<= t_end`

If data is missing:
- show warnings in diagnostics and skip metrics for that ticker.

### 6.3 Portfolio metrics computed per interval
For each interval (YTD, 3Y, 5Y):
- Portfolio:
  - `R_price`, `R_div`, `R_net`
  - `IRR` (baseline policy dependent)
- Per source:
  - same metrics
- Per sector/industry:
  - group tickers by resolved metadata

### 6.4 Sector/industry metadata resolution
Resolution options:
- Best: a metadata source (provider or open dataset)
- Fallback:
  - if sector cannot be resolved, label `Unknown`

Implementation:
- `get_sector_industry(ticker) -> {sector, industry}` cached
- Classification “source” (provider name) stored for audit

---

## 7) Stage 3: Web News + Sentiment + Recommendations

### 7.1 Open-source “news retrieval” component
There is no single universally “best” open-news API. Design a provider interface so you can plug in an open provider:

**Interface**
- `search_news(ticker, date_from, date_to, limit) -> [Article]`
Where:
- `Article = {title, snippet, url(optional), published_at, source(optional), text(optional)}`
- You should store only derived features when possible.

**Caching**
- Cache articles by `(ticker, date_from, date_to, limit)` key.
- Store minimal fields: title/snippet/published_at/source/url.

### 7.2 Local open-source sentiment scoring
Use a local transformer sentiment model (open-source) to score:
- title + snippet (+ optional content if available)

**Output per article**
- `label`: positive/neutral/negative
- `confidence`: probability-like score
- Convert to numeric:
  - positive: +confidence
  - neutral: 0
  - negative: -confidence

### 7.3 Horizon windows (time-decay aggregation)
For each ticker, define three horizon lookback windows relative to “now”:
- Short: last 6 months (cap to 3–6 months; implement 6m default)
- Medium: last 24 months
- Long: last 36+ months (implement 36 months default with long labeling)

Compute time decay:
\[
w_i=e^{-(t_{now}-t_i)/\tau}
\]
with e.g. `tau_short`, `tau_medium`, `tau_long`.

Aggregate:
\[
S=\frac{\sum_i w_i \cdot score_i}{\sum_i w_i}
\]

Bands:
- `S > +0.25` => Bullish
- `S < -0.25` => Bearish
- else Neutral

### 7.4 Signal extraction for rationale
Without relying on an LLM, extract lightweight categories from text:

- Earnings/guidance: keywords like “earnings”, “EPS”, “guidance raised/lowered”
- M&A: “acquire”, “merger”, “deal”
- Regulatory: “SEC”, “regulation”, “antitrust”
- Geopolitical: “sanctions”, “tariffs”, “embargo”, country mentions
- Macro: “inflation”, “rates”, “Fed”, “recession”
- Market risk: “lawsuit”, “fraud”, “bankruptcy”

For each horizon:
- list top positives: categories where sentiment score is highest
- list top negatives: categories where sentiment score is lowest

### 7.5 Recommendations logic (Buy/Sell/Hold)
Rules are deterministic and explainable.

Inputs per ticker:
- `Sentiment_short`, `Sentiment_medium`, `Sentiment_long`
- Trend proxy (optional): compute price momentum from yfinance:
  - e.g., 3M and 12M returns using Adj Close

Recommendation mapping example:
- Short (3–6 months):
  - Buy if sentiment short bullish AND momentum non-negative
  - Sell if sentiment short bearish AND momentum negative
  - Otherwise Hold
- Medium (1–3 years):
  - Buy if sentiment medium bullish
  - Sell if sentiment medium bearish
  - Otherwise Hold
- Long (3+ years):
  - Buy if sentiment long bullish and no strong negative regulatory/geopolitical signals
  - Sell if sentiment long bearish
  - Otherwise Hold

Include in rationale:
- 2–4 bullet positives/negatives based on extracted signals
- mention whether trend supports/contradicts sentiment

---

## 8) App UI/UX Design in Streamlit (Responsive + Naive-Friendly)

### 8.1 Visual flow (single page with staged content)
**Top**
- Title + short instruction
- Upload widget

**Step indicator**
- Stage 1, Stage 2, Stage 3 (based on completion state)

**After upload**
- “Run analysis” button
- If clicked:
  - Stage 1 shows quickly:
    - file counts
    - duplicates
    - masking summary
    - preview table

Then Stage 2:
- Metric cards for portfolio returns
- Tabs:
  - Overview
  - By Source
  - By Sector/Industry
  - Top Movers

Then Stage 3:
- Global ticker table (sortable):
  - ticker, sector, sentiment short/medium/long, action short/medium/long
- Ticker detail panel via dropdown/selectbox:
  - sentiment summary
  - positives/negatives
  - rationale per horizon

### 8.2 Responsive behavior
Streamlit automatically reflows columns on smaller screens, but keep layout simple:
- Use `st.columns()` with minimal complexity
- Use `st.expander()` for verbose sections (methodology, diagnostics, rationale)
- Avoid fixed widths; rely on container width

### 8.3 User trust and comprehension
Add “Methodology (plain English)” expander:
- what YTD means
- what “net return” means (Adj Close)
- how dividend component is computed (residual)
- IRR baseline policy (if dates missing)

---

## 9) Technical Architecture

### 9.1 Recommended modules (clean separation)
- `ui.py` — Streamlit layout and user interaction
- `stage1.py`
  - `read_csv(file)`
  - `sanitize_pii_npi(df)`
  - `deduplicate(files)`
  - `normalize_holdings(df)`
- `data_market.py`
  - `fetch_yfinance_history(tickers, start, end)`
  - `compute_interval_returns(holdings, prices, intervals)`
- `sector_meta.py`
  - `resolve_sector_industry(tickers)`
- `news_provider.py`
  - `class NewsProvider: search_news(...)`
- `sentiment.py`
  - `load_sentiment_model()`
  - `score_articles(articles)`
  - `aggregate_sentiment(articles, horizon)`
- `recommendations.py`
  - `action_short/medium/long(sentiment, trend, signal_rules)`
- `report.py`
  - `render_markdown_report(bundle)`

### 9.2 Caching strategy
Use Streamlit caching:
- `@st.cache_data` for pure functions:
  - market data fetch results
  - resolved metadata
  - news search results
  - sentiment aggregation outputs
- `@st.cache_resource` for model loading (sentiment model)

Cache keys must include:
- ticker list (sorted)
- date ranges
- app version / model version

### 9.3 Deterministic test harness
For Playwright:
- inject fixed market and metadata providers from test-only harness code
- keep fixture selection outside the production application
- ensure consistent screenshots without a runtime test-mode branch.

---

## 10) Playwright Testing & Screenshot/Recording Plan

### 10.1 What to test
- Upload works for 1..3 CSVs
- Run analysis button enabling/disabling
- Stage order appears:
  - Stage 1 diagnostics
  - Stage 2 metrics + tabs
  - Stage 3 ticker table + ticker detail
- Responsive layout across viewports:
  - Phone: 390×844
  - iPad: 768×1024
  - Desktop: 1280×720

### 10.2 Test artifacts
At each checkpoint capture:
- Screenshot: `artifacts/<test>/<viewport>/stage1.png`, etc.
- Video recording (optional): Playwright config `recordVideo`

### 10.3 Example checkpoint flow
1. Navigate to app URL
2. Upload fixture CSV(s)
3. Click “Run analysis”
4. Wait for selectors indicating Stage 1 complete
5. Screenshot Stage 1
6. Wait Stage 2 metric cards
7. Screenshot Stage 2
8. Wait Stage 3 ticker table
9. Screenshot Stage 3

---

## 11) Data Outputs (Report Schema)

### 11.1 Markdown report sections
- Disclaimer
- Stage 1 diagnostics
  - duplicates
  - masking summary
  - parsed tickers count
- Stage 2 performance
  - portfolio summary table (YTD, 3Y, 5Y)
  - by source table
  - by sector table
  - top movers (net P&L, net return)
- Stage 3 sentiment + recommendations
  - ticker table (actions per horizon)
  - ticker details for top N tickers

### 11.2 Tabular fields (recommended)
Performance tables:
- interval (YTD/3Y/5Y)
- MV_start
- MV_end
- Return_price_pct
- Return_div_pct
- Return_net_pct
- IRR_pct (if computed)
- top tickers by net contribution

Sentiment tables:
- ticker
- sector/industry
- sentiment_short_score, band, action
- sentiment_medium_score, band, action
- sentiment_long_score, band, action
- top positives / negatives (short list)

---

## 12) Deployment Considerations
- Use a virtual environment and pin dependencies
- Ensure sentiment model files downloaded at startup (or bundled)
- Add rate limiting / concurrency control:
  - market fetches: yfinance can rate limit
  - news API calls: rate limit and caching essential
- Provide secrets through the deployment platform or Streamlit secrets.

---

## 13) Implementation Dependencies (Summary)
- **Streamlit**: UI + caching + report rendering
- **Python**: pipeline + computation
- **yfinance**: market data (Adj Close and Close history)
- **Open-source news retrieval**: via a pluggable `NewsProvider`
- **Open-source sentiment model**: local transformer pipeline
- **Playwright**: browser automation, screenshots, optional video

