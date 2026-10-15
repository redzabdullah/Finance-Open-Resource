# Finance Data MCP Server

A read-only remote Model Context Protocol (MCP) server for public market data
and finance literature. Yahoo Finance is the primary market-data provider;
OpenAlex, arXiv, and DOAJ provide research and journal-directory records. The
server uses Streamable HTTP transport.

## Agentic flow at a glance

This server is the tool layer for an AI agent such as ChatGPT or Claude. It
contains no AI model itself. The agent decides what to do; the server gives it
**tools** to fetch data, **recipes** for multi-step research tasks (company
briefs, literature reviews, peer comparisons), and **guardrails** in every
response that flag missing data and require your approval before any
supplementary source is used.

```
You ──► AI agent ──► get_research_workflow ──► recipe (which tools, in what order)
            │
            ├──► tool calls ──► data + coverage_audit (+ fallback_recommendation)
            │
            └──► answer with gaps labelled ──► "Use <source> as a supplement?" ──► You decide
```

See [How the agentic flow works](docs/AGENTIC_FLOW.md) for a step-by-step
diagram, a worked example and the roadmap.

## Data sources

| Source | Purpose |
| --- | --- |
| Yahoo Finance | Primary public market quotes, history, company data, news, and screeners |
| Alpha Vantage | Separate opt-in quotes, technical indicators, FX, crypto, macroeconomic data, and news sentiment |
| OpenAlex | Published finance, business, economics, and accounting research |
| arXiv | Quantitative-finance and economics preprints |
| DOAJ | Vetted open-access journal directory checks |

## Canonical repository

The canonical GitHub repository is
[`redzabdullah/Finance-Open-Resource`](https://github.com/redzabdullah/Finance-Open-Resource).

Clone it with:

```bash
git clone https://github.com/redzabdullah/Finance-Open-Resource.git
```

## Tools

Market tools: `get_stock_quote`, `get_historical_prices`,
`get_financial_statements`, `get_corporate_actions`, `get_holders`,
`get_sector_data`, `get_stock_screener`, `get_news`, `get_sustainability`, and
`get_market_snapshot`.

Research tools: `search_finance_research`, `get_research_paper`,
`search_finance_preprints`, `check_journal_legitimacy`, and
`search_finance_research_batch`.

Composite tools: `compare_peers` (2-10 tickers) and `get_company_profile` (one
ticker). They chain the market tools above and compute net margin, ROE,
debt-to-equity, one-year price change (traded prices, excluding dividends),
one-year total return (dividend-adjusted) and dividend yield in code, using each
company's latest annual period. Their `coverage_audit` only checks the fields
they report, so blank line items in older periods do not mark a result
partial; the underlying tool audits remain visible in `component_audits`.

`get_historical_prices` returns traded prices (adjusted for splits only) in
`open`/`high`/`low`/`close`, and Yahoo's dividend-adjusted close in `adj_close`.

Workflow tool: `get_research_workflow` (see Workflow prompts below).

### Alpha Vantage tools

Alpha Vantage is exposed through distinct tools and is never blended into the
Yahoo Finance tools automatically:

- `get_stock_quote_av`
- `get_technical_indicator_av` (SMA, EMA, RSI, MACD, and BBANDS)
- `get_forex_rate_av`
- `get_crypto_quote_av`
- `get_economic_indicator_av`
- `get_news_sentiment_av`

All Alpha Vantage tools share one free-tier quota tracker: at most 25 requests
per day and five requests in a rolling 60-second window. The server rejects an
exhausted request before any HTTP call and reports when quota capacity resets.
Daily usage persists across server restarts in `.av_quota_state.json`, a
git-ignored local runtime-state file; the rolling minute window remains in
memory.

## Workflow prompts

The server also exposes MCP prompts: ready-made workflows a client's agent can
follow step by step. They contain no model calls; each one tells the agent
which tools to call, in what order, and restates the coverage rules below
(check every `coverage_audit`, never fetch fallbacks without confirmation,
never blend Alpha Vantage into Yahoo Finance values).

| Prompt                      | Arguments                                   | Workflow                                                                 |
| --------------------------- | ------------------------------------------- | ------------------------------------------------------------------------ |
| `company_research_brief`    | `ticker`, optional `focus`                  | `get_company_profile`, price history, actions, holders, news, research   |
| `finance_literature_review` | `topic`, `year_from`, `include_preprints`   | OpenAlex search, full records, arXiv preprints, DOAJ journal checks       |
| `peer_comparison`           | `tickers` (2-10, comma-separated)           | `compare_peers`, then a table and observations using server-computed ratios |

Clients that only support tools (for example ChatGPT) can use the same
workflows through the `get_research_workflow` tool: pass `workflow` plus its
arguments and it returns the identical instructions. It fetches no data itself.

Prompts live in `prompts.py`; a test fails if a prompt names a tool the server
does not provide.

## Coverage contract

Every tool response includes a `coverage_audit` without changing its original
result fields. Its `status` is one of:

- `complete`: all requested observations were returned;
- `partial`: only some requested entities or fields were returned;
- `unavailable`: the primary source returned no usable observations or failed;
- `truncated`: the connector limit may have excluded observations;
- `not_supported`: the requested data type is outside the tool capability;
- `conflicting`: reserved for unreconciled observations from multiple sources.

Empty collections mean unavailable data, never a measured zero. Malformed
parameters retain an `error` and include `error_type: "validation"` so clients
can distinguish invalid requests from legitimate empty results.

When coverage is incomplete, `fallback_recommendation` identifies the narrowest
configured authoritative supplementary source. It always includes
`requires_user_confirmation: true`. The server does not retrieve supplementary
data automatically. Clients must obtain explicit user confirmation before using
the recommendation, must label supplementary observations separately, and must
never overwrite primary-source values while differences remain unreconciled.

Recommendations follow the listing market of the requested tickers, read from
the Yahoo Finance suffix (see `markets.py`). US tickers keep the SEC EDGAR
recommendations; SGX (`.SI`), HKEX (`.HK`), LSE (`.L`), JPX (`.T`), ASX (`.AX`),
TSX (`.TO`, `.V`), NSE/BSE (`.NS`, `.BO`) and KRX (`.KS`, `.KQ`) tickers point to
that market's disclosure system and issuer annual reports. Other suffixes get a
generic home-exchange recommendation, and requests spanning several markets
return a `by_market` breakdown.

The audit records the primary provider, retrieval method, and retrieval date.
Future supplementary observations should also retain their provider, underlying
source, stable URL/accession/DOI, reporting or measurement date, retrieval date,
retrieval method, source status, and any transformation or reconciliation note.

Historical ownership is not supplied by `get_holders`; requesting it returns
`not_supported` and recommends the appropriate SEC EDGAR filings (or the
home-market disclosures for non-US listings). A DEF 14A
table must retain its stated measurement date, 13F positions must not be treated
as a complete shareholder register, and truncated holder lists must not be used
to calculate total institutional ownership. Any later SEC workflow must verify
issuer identity (including CIK and CUSIP), account for amendments and duplicate
manager filings, and flag predecessor, SPAC, reverse-merger, and pre-listing
periods where relevant.

## Set up locally

Python 3.10 or newer is recommended.

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

On macOS or Linux, activate it with:

```bash
source .venv/bin/activate
```

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

To use the separate Alpha Vantage tools, obtain an Alpha Vantage API key and
set it in the server environment. It is read only when an `_av` tool is called:

```powershell
$env:ALPHA_VANTAGE_API_KEY = "your-api-key"
```

Do not commit API keys or `.env` files; both `.env` and `.env.*` are ignored by
Git.

### Optional Yahoo-to-Alpha-Vantage fallback

When `get_stock_quote` receives partial or unavailable Yahoo Finance coverage,
it may offer a separate Alpha Vantage lookup. Clients that declare MCP form
elicitation support receive an accept/decline prompt explaining that the lookup
uses one of the 25 daily requests. Alpha Vantage is called only after acceptance.
Declining or cancelling leaves the Yahoo response unchanged. Clients without
elicitation support receive the same prompt in `coverage_audit.suggestion` so
they can relay it conversationally.

## Run the server

Start it locally with:

```bash
python server.py
```

The server binds to `0.0.0.0` on port `8000` by default and exposes its
Streamable HTTP endpoint at `http://localhost:8000/mcp`. Set the `PORT`
environment variable to use a different port (Render supplies this variable
automatically).

No authentication, API key, or OAuth configuration is needed because the
tool only reads public Yahoo Finance data.
