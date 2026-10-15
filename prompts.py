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
1. `get_company_profile` for {symbol}: price, market cap, latest annual
   revenue, net income, net margin, ROE, debt, equity, free cash flow,
   one-year price change and trailing dividends, all computed by the server.
   Report these figures as returned; do not recalculate them.
2. `get_historical_prices` for {symbol} with period `1y`, interval `1wk`:
   describe the 52-week range and trend.
3. `get_corporate_actions` for {symbol}: recent dividends and splits.
4. `get_holders` for {symbol} with holder_type `major`.
5. `get_news` for {symbol}: the main recent themes, with links.
6. `search_finance_research` with the company name and limit 5: any academic
   work about the company or its industry.

Output: a brief with sections for Snapshot and key ratios, Price history,
Corporate actions, Ownership, News, Research, and a final "Data gaps" section
listing every result whose coverage status was not `complete`.

{COVERAGE_RULES}"""


def finance_literature_review(topic: str, year_from: int = 0, include_preprints: bool = True) -> str:
    """Build a literature review workflow across OpenAlex, arXiv and DOAJ."""
    subject = _clean_text(topic, "a research topic")
    year = int(year_from or 0)
    year_arg = f", year_from {year}" if year else ""
    preprint_arg = "" if include_preprints else ", include_preprints false"
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
5. `synthesize_research` for "{subject}"{year_arg}{preprint_arg}. If
   `synthesis_status` is `generated` and `citation_check.passed` is true, use
   its synthesis as your draft and replace its source IDs with full
   citations. If it was not generated, or the citation check failed, write
   the review yourself from its `sources`, following `instructions_for_agent`.

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
1. `compare_peers` with tickers [{joined}]. The server returns market cap,
   latest annual revenue and net income, net margin, ROE, one-year price
   change (traded prices, excluding dividends) and one-year total return
   (dividend-adjusted) for each company, with rankings and the line items
   and dates used.
2. Report the figures exactly as returned; do not recalculate them. Quote
   each company's `fiscal_period_end` and the `price_window` dates.
3. For any gap listed in `coverage_audit.missing.gaps`, say "unavailable"
   and give the stated reason. Only if the user asks why a figure is
   missing, call `get_financial_statements` for that ticker to show what
   the source reported.

Output: one comparison table (rows = companies; columns = market cap,
revenue, net income, net margin, ROE, 1-year price change, 1-year total
return, fiscal period end). Label price change as excluding dividends and
total return as dividend-adjusted, then three to five plain observations based on the rankings. Repeat
any notes the tool returns (for example about currencies). For banks and
other financial firms, say that net margin is a weak measure and ROE is
more meaningful.

{COVERAGE_RULES}"""


CRUNCHBASE_RULES = """\
Crunchbase rules:
- Crunchbase (`*_cb` tools) is a licensed private-market source. Report its
  figures in their own section, labelled "Crunchbase", and never merge them
  into Yahoo Finance figures.
- An amount or valuation marked "not disclosed" is unknown, not zero.
- Quote only the rounds and deals needed for the answer; do not reproduce
  Crunchbase data in bulk."""


def company_lifecycle(company_or_ticker: str, crunchbase_enabled: bool = False) -> str:
    """Build a private-to-public lifecycle workflow for one company."""
    subject = _clean_text(company_or_ticker, "a company name or ticker")
    if not crunchbase_enabled:
        return f"""\
Trace the lifecycle of {subject}: private-market history and public-market
performance.

The Crunchbase module is not enabled on this server, so funding rounds,
investors and acquisitions are unavailable. Tell the user this first, then
continue with Yahoo Finance data only.

Workflow:
1. If "{subject}" is not a Yahoo Finance ticker, ask the user for the ticker
   (with the exchange suffix for non-US listings, for example D05.SI). If the
   company is not listed, say that no public-market data applies and stop.
2. `get_company_profile` for the ticker. Report the figures as returned.
3. `get_historical_prices` for the ticker with period `max`, interval `1mo`:
   describe performance since listing.

Output: a "Private-market history" section stating that the Crunchbase
module is not enabled, a "Public-market performance (Yahoo Finance)"
section, and a final "Data gaps" section listing every result whose
coverage status was not `complete`.

{COVERAGE_RULES}"""

    return f"""\
Trace the lifecycle of {subject}: private-market history and public-market
performance.

Workflow:
1. `search_organization_cb` with query "{subject}". Pick the candidate that
   matches; if several plausibly match, ask the user which one. If the input
   looks like a ticker and nothing matches, search again by company name.
2. `get_funding_rounds_cb` with the candidate's permalink: rounds with date,
   type, amount, lead investors and post-money valuation.
3. `get_acquisitions_cb` with the same permalink: deals as acquirer and as
   acquiree.
4. If the candidate has a `stock_symbol` (it is public or was listed),
   convert it to a Yahoo Finance ticker (add the exchange suffix for non-US
   listings, for example D05.SI for SGX) and:
   a. `get_company_profile` for the ticker. Report the figures as returned.
   b. `get_historical_prices` for the ticker with period `max`, interval
      `1mo`: describe performance since listing.
   If it has no stock symbol, say the company is private and skip this step.

Output, as separate, clearly labelled sections:
- "Private-market history (Crunchbase)": a timeline of funding rounds and
  acquisitions.
- "Public-market performance (Yahoo Finance)": the profile and price history,
  or a statement that the company is not listed.
- "Data gaps": every result whose coverage status was not `complete`,
  including undisclosed amounts and valuations.

{CRUNCHBASE_RULES}

{COVERAGE_RULES}"""


WORKFLOWS = {
    "company_research_brief": "Multi-tool research brief for one ticker (needs ticker; optional focus).",
    "finance_literature_review": "Review of published research and preprints (needs topic; optional year_from, include_preprints).",
    "peer_comparison": "Side-by-side comparison of 2-10 tickers with server-computed ratios (needs tickers, comma-separated).",
    "company_lifecycle": "Private-market history (Crunchbase, if enabled) and public-market performance for one company (needs company_or_ticker).",
}


def render_workflow(
    workflow: str,
    ticker: str = "",
    tickers: str = "",
    topic: str = "",
    focus: str = "",
    year_from: int = 0,
    include_preprints: bool = True,
    company_or_ticker: str = "",
    crunchbase_enabled: bool = False,
) -> str:
    """Render a named workflow; raises ValueError on unknown names or bad arguments."""
    name = (workflow or "").strip().lower()
    if name == "company_research_brief":
        return company_research_brief(ticker, focus)
    if name == "finance_literature_review":
        return finance_literature_review(topic, year_from, include_preprints)
    if name == "peer_comparison":
        return peer_comparison(tickers)
    if name == "company_lifecycle":
        return company_lifecycle(company_or_ticker, crunchbase_enabled)
    raise ValueError(
        f"Invalid workflow '{workflow}'. Choose one of: {', '.join(sorted(WORKFLOWS))}."
    )


def register_prompts(mcp, crunchbase_enabled: bool = False) -> None:
    """Attach the workflow prompts to a FastMCP server.

    ``crunchbase_enabled`` must match whether the server registered the
    ``_cb`` tools, so the lifecycle prompt never names a missing tool.
    """

    def lifecycle_prompt(company_or_ticker: str) -> str:
        return company_lifecycle(company_or_ticker, crunchbase_enabled)

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
    mcp.prompt(
        name="company_lifecycle",
        title="Company Lifecycle",
        description="Private-market history (Crunchbase, when enabled) and public-market performance for one company.",
    )(lifecycle_prompt)
