"""Reusable MCP prompts that turn the read-only tools into guided workflows.

Prompts contain no model calls. Each one is a recipe the client's agent can
follow: which tools to call, in what order, and which coverage rules to obey
while doing so. The server still only reads public data.
"""

import re

# Shared rules appended to every workflow so agents honour the coverage
# contract described in the README, whatever workflow they are running.
COVERAGE_RULES = """\
Rules for every tool result:
1. Read `coverage_audit.status` on every response before using it.
   - `complete`: use as reported.
   - `partial` / `truncated`: use, but say which fields or rows are missing.
   - `unavailable`: report the data as unavailable. An empty result is never
     a measured zero.
   - `not_supported`: say the tool cannot supply this; do not estimate it.
2. If a response has a `fallback_recommendation`, do NOT fetch the suggested
   source yourself. Relay its `suggested_user_prompt` to the user and wait for
   an explicit yes.
3. Never blend Alpha Vantage (`*_av` tools) values into Yahoo Finance values.
   Report them separately, labelled as supplementary.
4. Cite the provider and retrieval date from `coverage_audit.primary_source`
   for every figure you report.
5. Treat arXiv results as preprints that are not peer reviewed.
6. Do not give investment advice or buy/sell recommendations."""

_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


def _clean_ticker(raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if not _TICKER_PATTERN.fullmatch(symbol):
        raise ValueError(f"Invalid ticker '{raw}'. Provide a symbol such as AAPL or D05.SI.")
    return symbol


def _clean_text(raw: str, name: str, max_length: int = 200) -> str:
    text = " ".join((raw or "").split())
    if not text:
        raise ValueError(f"Please provide {name}.")
    if len(text) > max_length:
        raise ValueError(f"{name.capitalize()} must be at most {max_length} characters.")
    return text


def company_research_brief(ticker: str, focus: str = "") -> str:
    """Build a step-by-step research brief workflow for one company."""
    symbol = _clean_ticker(ticker)
    focus_text = " ".join((focus or "").split())[:200]
    focus_line = f"\nGive particular attention to: {focus_text}." if focus_text else ""
    return f"""\
Prepare a factual research brief on {symbol}.{focus_line}

Workflow:
1. `get_stock_quote` for {symbol}: current price, previous close, market cap.
2. `get_historical_prices` for {symbol} with period `1y`, interval `1wk`:
   describe the 52-week range and trend.
3. `get_financial_statements` for {symbol}: `income` annual, then `balance`
   annual, then `cashflow` annual. Summarise revenue, net income, total debt
   and free cash flow across the periods returned.
4. `get_corporate_actions` for {symbol}: recent dividends and splits.
5. `get_holders` for {symbol} with holder_type `major`.
6. `get_news` for {symbol}: the main recent themes, with links.
7. `search_finance_research` with the company name and limit 5: any academic
   work about the company or its industry.

Output: a brief with sections for Snapshot, Price history, Financials,
Corporate actions, Ownership, News, Research, and a final "Data gaps" section
listing every result whose coverage status was not `complete`.

{COVERAGE_RULES}"""


def finance_literature_review(topic: str, year_from: int = 0, include_preprints: bool = True) -> str:
    """Build a literature review workflow across OpenAlex, arXiv and DOAJ."""
    subject = _clean_text(topic, "a research topic")
    year = int(year_from or 0)
    year_arg = f", year_from {year}" if year else ""
    preprint_step = (
        f"3. `search_finance_preprints` for \"{subject}\" with limit 10. Keep these in a\n"
        "   separate \"Preprints (not peer reviewed)\" section.\n"
        if include_preprints
        else "3. Skip preprints; use published work only.\n"
    )
    return f"""\
Prepare a short literature review on: {subject}.

Workflow:
1. `search_finance_research` for "{subject}" with limit 15{year_arg}.
2. Pick the 3 most cited or most relevant works and call
   `get_research_paper` with each DOI or OpenAlex ID for full abstracts.
{preprint_step}4. For each journal named in steps 1-2, call `check_journal_legitimacy`
   once. Note open-access journals that DOAJ does not list; absence from DOAJ
   does not by itself mean a journal is illegitimate (many subscription
   journals are not in DOAJ).

Output: key themes, main findings with citations (authors, year, journal,
DOI), points of disagreement, gaps for further research, and a "Data gaps"
section for any result whose coverage status was not `complete`.

{COVERAGE_RULES}"""


def peer_comparison(tickers: str) -> str:
    """Build a side-by-side comparison workflow for 2-10 companies."""
    symbols = [_clean_ticker(part) for part in (tickers or "").replace(";", ",").split(",") if part.strip()]
    symbols = list(dict.fromkeys(symbols))
    if not 2 <= len(symbols) <= 10:
        raise ValueError("Provide 2 to 10 comma-separated tickers, e.g. AAPL, MSFT, GOOGL.")
    joined = ", ".join(symbols)
    return f"""\
Compare these companies side by side: {joined}.

Workflow:
1. `get_market_snapshot` with tickers [{joined}]: price and market cap, ranked.
2. For each ticker, `get_financial_statements` with statement `income` and
   period `annual`. Use only the most recent period each company reports, and
   state its period-ending date, since fiscal years can differ.
3. For each ticker, `get_historical_prices` with period `1y`, interval `1mo`:
   compute the 1-year price change from the first and last closes returned.

Output: one comparison table (rows = companies; columns = market cap, latest
revenue, net income, net margin, 1-year price change, fiscal period end), then
three to five plain observations. Mark any cell you could not fill as
"unavailable", never as 0. Show your margin and price-change arithmetic.

{COVERAGE_RULES}"""


def register_prompts(mcp) -> None:
    """Attach the workflow prompts to a FastMCP server."""
    mcp.prompt(
        name="company_research_brief",
        title="Company Research Brief",
        description="Guided multi-tool research brief for one ticker, with coverage checks.",
    )(company_research_brief)
    mcp.prompt(
        name="finance_literature_review",
        title="Finance Literature Review",
        description="Guided review of published research and preprints on a finance topic.",
    )(finance_literature_review)
    mcp.prompt(
        name="peer_comparison",
        title="Peer Comparison",
        description="Side-by-side comparison of 2-10 tickers on size, profitability and price change.",
    )(peer_comparison)
