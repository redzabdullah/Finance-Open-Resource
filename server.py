"""A small MCP server that exposes public Yahoo Finance market data."""

import os
import json
import re
import asyncio
from functools import wraps
from inspect import isawaitable, signature
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from math import isnan
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import yfinance as yf
from pydantic import BaseModel, Field
from yfinance.const import SECTOR_INDUSTY_MAPPING_LC
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations

from alphavantage_client import request as alphavantage_request
from crunchbase_client import api_key_configured as crunchbase_key_configured
from crunchbase_client import entity_path as crunchbase_path
from crunchbase_client import request as crunchbase_request
from markets import market_fallback, market_for
from prompts import WORKFLOWS, register_prompts, render_workflow


mcp = FastMCP(
    "finance-data-server",
    host="0.0.0.0",
    port=int(os.getenv("PORT", 8000)),
)


FALLBACK_SOURCES = {
    "financial_statements": {
        "recommended_source": "SEC EDGAR Companyfacts and issuer filings",
        "reason": "The primary source did not supply all requested reported accounting data.",
        "suggested_sources": [
            {"source": "SEC Companyfacts", "purpose": "Standardized reported accounting facts"},
            {"source": "10-K/10-Q filings", "purpose": "Authoritative filed statements and notes"},
            {"source": "Investor relations", "purpose": "Official annual reports and earnings releases"},
        ],
    },
    "ownership": {
        "recommended_source": "SEC EDGAR",
        "reason": "The primary source returned no usable ownership observations.",
        "suggested_filings": [
            {"form": "DEF 14A", "purpose": "Major beneficial owners, directors and executive ownership"},
            {"form": "13F-HR", "purpose": "Quarterly institutional-manager positions"},
            {"form": "13D/13G", "purpose": "Reportable beneficial ownership positions and changes"},
            {"form": "3/4/5", "purpose": "Insider ownership changes and transactions"},
        ],
    },
    "corporate_actions": {
        "recommended_source": "Issuer and exchange disclosures",
        "reason": "The primary source did not supply all requested corporate actions.",
        "suggested_sources": [
            {"source": "SEC 8-K", "purpose": "Material corporate-action disclosures"},
            {"source": "Investor relations", "purpose": "Official issuer announcements"},
            {"source": "Exchange notices", "purpose": "Official exchange action notices"},
        ],
    },
    "literature": {
        "recommended_source": "Crossref or publisher records",
        "reason": "The primary literature source did not supply all requested records.",
        "suggested_sources": [
            {"source": "Crossref", "purpose": "DOI and publication metadata"},
            {"source": "Publisher record", "purpose": "Authoritative article metadata"},
            {"source": "Recognised repository", "purpose": "Stable manuscript or preprint record"},
        ],
    },
    "prices": {
        "recommended_source": "Official exchange data",
        "reason": "The primary source did not supply all requested price observations.",
        "suggested_sources": [
            {"source": "Official exchange", "purpose": "Authoritative trade and quote history"},
            {"source": "Reputable market-data vendor", "purpose": "Price history with adjustment methodology stated"},
        ],
    },
    "private_funding": {
        "recommended_source": "Company filings and business registries",
        "reason": "Crunchbase did not supply all requested private-market data (amounts, valuations or deals may be undisclosed).",
        "suggested_sources": [
            {"source": "Company filings", "purpose": "Share allotments, capital changes and audited accounts filed by the company"},
            {"source": "Business registries (e.g. ACRA, Companies House, SEC Form D)", "purpose": "Registered share capital, allotments and exempt-offering notices"},
            {"source": "Press releases", "purpose": "Announced rounds, investors and acquisition terms from the company or investors"},
        ],
    },
}


def _fallback_recommendation(data_type: str, tickers=None) -> dict:
    """Build the confirmation-gated fallback recommendation for a data type."""
    fallback = market_fallback(data_type, FALLBACK_SOURCES.get(data_type), tickers)
    if fallback:
        return {
            "available": True,
            "requires_user_confirmation": True,
            "source_status": "supplementary",
            **fallback,
            "suggested_user_prompt": (
                "Finance Data and Lit could not fully supply the requested "
                f"{data_type.replace('_', ' ')} data. Would you like me to continue "
                f"using {fallback['recommended_source']} as a supplementary source?"
            ),
        }
    return {
        "available": False,
        "requires_user_confirmation": True,
        "source_status": "supplementary",
        "recommended_source": None,
        "reason": "No narrower authoritative supplementary source is configured for this data type.",
    }


def _coverage_tool(data_type: str, provider: str, collection: str | None = None, limit: int | None = None):
    """Add a uniform coverage contract without changing existing response fields."""
    def decorate(function):
        function_signature = signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = function_signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            requested = {key: value for key, value in bound.arguments.items() if value is not None}
            if "ticker" in requested:
                requested["tickers"] = [str(requested.pop("ticker")).strip().upper()]
            requested["data_type"] = data_type

            response = function(*args, **kwargs)
            if not isinstance(response, dict):
                return response

            # Tools whose row limit depends on arguments or spans several
            # provider calls report truncation through this private flag.
            truncated = bool(response.pop("_truncated", False))
            error = response.get("error")
            validation = bool(error and error.startswith(("Please ", "Invalid ", "Too many ")))
            unsupported = bool(error and error.startswith("Unsupported "))
            rows = response.get(collection) if collection else None
            if data_type == "corporate_actions":
                rows = (response.get("dividends") or []) + (response.get("splits") or [])
            row_count = len(rows) if isinstance(rows, (list, dict)) else (1 if not error else 0)
            missing_value_fields = [
                key for key, value in response.items()
                if value is None and key not in {"message"}
            ]
            if isinstance(rows, list):
                missing_value_fields.extend(
                    key
                    for row in rows if isinstance(row, dict)
                    for key, value in row.items() if value is None
                )
            missing_value_fields = sorted(set(missing_value_fields))
            status = "complete"
            if unsupported:
                status = "not_supported"
            elif error:
                status = "unavailable"
            elif isinstance(rows, (list, dict)) and not rows:
                status = "unavailable"
            elif isinstance(rows, list) and any(isinstance(row, dict) and row.get("error") for row in rows):
                status = "partial" if any(isinstance(row, dict) and not row.get("error") for row in rows) else "unavailable"
            elif truncated or (limit is not None and isinstance(rows, list) and len(rows) >= limit):
                status = "truncated"
            elif missing_value_fields:
                status = "partial"
            if data_type == "journal_directory" and response.get("found") is False:
                status = "unavailable"
            if data_type == "corporate_actions" and not error:
                action_type = requested.get("action_type", "all")
                requested_fields = ["dividends", "splits"] if action_type == "all" else [action_type]
                present_fields = [field for field in requested_fields if response.get(field)]
                if not present_fields:
                    status = "unavailable"
                elif len(present_fields) < len(requested_fields):
                    status = "partial"
                if any(len(response.get(field) or []) >= 100 for field in requested_fields):
                    status = "truncated"

            returned = {"row_count": row_count}
            if response.get("ticker"):
                returned["tickers"] = [response["ticker"]]
            if isinstance(rows, list):
                periods = [row.get("period_ending") or row.get("date") for row in rows if isinstance(row, dict)]
                returned["periods"] = [value for value in periods if value]
            missing = {}
            if status in {"unavailable", "not_supported"}:
                missing["fields"] = [data_type]
                if data_type == "ownership":
                    holder_type = requested.get("holder_type", "major")
                    missing["fields"] = [
                        f"{holder_type}_holders"
                        if holder_type in {"major", "institutional"}
                        else holder_type
                    ]
                if requested.get("tickers"):
                    missing["tickers"] = requested["tickers"]
            elif status == "partial" and data_type == "corporate_actions":
                missing["fields"] = [
                    field for field in requested_fields if not response.get(field)
                ]
            elif status == "partial":
                missing["fields"] = missing_value_fields
            elif status == "truncated":
                missing["reason"] = "The connector limit may have excluded additional observations."
            if response.get("error_type") == "quota_exhausted":
                missing["reason"] = (
                    f"{provider} {response.get('quota', 'shared')} limit was hit; "
                    f"capacity resets at {response.get('resets_at', 'an unknown time')}."
                )
            elif response.get("error_type") == "auth_error":
                # A rejected key says nothing about the data; never imply "none exist".
                missing["reason"] = response["error"]

            response["coverage_audit"] = {
                "status": status,
                "requested": requested,
                "returned": returned,
                "missing": missing,
                "primary_source": {
                    "provider": provider,
                    "retrieval_method": "Finance Data and Lit MCP",
                    "status": "primary_workflow_source",
                    "retrieval_date": date.today().isoformat(),
                },
            }
            if validation:
                response["error_type"] = "validation"
            # A rejected API key needs a new key, not a supplementary source.
            if status != "complete" and response.get("error_type") != "auth_error":
                response["fallback_recommendation"] = _fallback_recommendation(
                    data_type, requested.get("tickers")
                )
            return response

        return wrapped
    return decorate


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy scalar values into JSON-friendly Python values."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    converted = value.item() if hasattr(value, "item") else value
    if isinstance(converted, float) and isnan(converted):
        return None
    return converted


def _iso_datetime(value: Any) -> str | None:
    """Convert Yahoo timestamps or date strings to an ISO-formatted string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _normalize_text(value: Any) -> str:
    """Normalize a string parameter without raising on malformed input."""
    return value.strip() if isinstance(value, str) else ""


def _ticker_validation_error(stock: yf.Ticker, symbol: str) -> dict | None:
    """Return a structured error when Yahoo has no market data for a symbol."""
    try:
        if stock.history(period="5d").empty:
            return {"error": f"Invalid ticker '{symbol}': no market data was found."}
    except Exception as exc:
        return {"error": f"Could not validate ticker '{symbol}': {exc}"}
    return None


OPENALEX_MAILTO = "redzuana@smu.edu.sg"
RESEARCH_HTTP_TIMEOUT = 15


class _ResearchAPIError(Exception):
    """Represent an expected failure from an external research API."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _research_http_get(url: str, params: dict | None = None) -> bytes:
    """Fetch an external research API response with a bounded timeout."""
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "finance-research-mcp/1.0"})
    try:
        with urlopen(request, timeout=RESEARCH_HTTP_TIMEOUT) as response:
            return response.read()
    except HTTPError as exc:
        raise _ResearchAPIError(f"HTTP {exc.code}", status=exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise _ResearchAPIError(str(exc)) from exc


def _research_http_get_json(url: str, params: dict | None = None) -> dict:
    """Fetch and decode a JSON response from a research API."""
    try:
        return json.loads(_research_http_get(url, params).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ResearchAPIError("the service returned invalid JSON") from exc


def _openalex_abstract(inverted_index: Any) -> str | None:
    """Reconstruct OpenAlex's inverted-index abstract representation."""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return None
    positioned_words = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            positioned_words.extend((position, word) for position in positions)
    return " ".join(word for _, word in sorted(positioned_words)) or None


def _snippet(text: str | None, length: int = 500) -> str | None:
    """Return a compact single-line text preview."""
    if not text:
        return None
    compact = " ".join(text.split())
    return compact if len(compact) <= length else f"{compact[: length - 1].rstrip()}…"


def _openalex_authors(work: dict) -> list[str]:
    """Extract display names from an OpenAlex work."""
    return [
        author.get("author", {}).get("display_name")
        for author in work.get("authorships", [])
        if author.get("author", {}).get("display_name")
    ]


# All tools on this server are read-only data lookups. Use this annotations
# pattern for every tool added here so clients classify them correctly.
@_coverage_tool("prices", "Yahoo Finance")
def _get_stock_quote_yahoo(ticker: str = "") -> dict:
    """Return the latest public Yahoo Finance quote for a ticker symbol."""
    symbol = _normalize_text(ticker).upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        history = stock.history(period="5d")

        # Yahoo returns an empty history for unknown or delisted symbols.
        if history.empty:
            return {"error": f"Invalid ticker '{symbol}': no market data was found."}

        latest = history.iloc[-1]
        fast_info = stock.fast_info

        current_price = _json_value(latest["Close"])
        previous_close = fast_info.get("regularMarketPreviousClose")
        if previous_close is None:
            previous_close = fast_info.get("previousClose")
        if previous_close is None and len(history) > 1:
            previous_close = history.iloc[-2]["Close"]

        market_cap = fast_info.get("marketCap")
        if market_cap is None:
            shares = fast_info.get("shares")
            if shares is not None and current_price is not None:
                market_cap = shares * current_price

        # Use the latest trading row for intraday fields and fast_info for
        # quote metadata that is not included in the history table.
        return {
            "ticker": symbol,
            "current_price": current_price,
            "previous_close": _json_value(previous_close),
            "day_high": _json_value(latest["High"]),
            "day_low": _json_value(latest["Low"]),
            "volume": _json_value(latest["Volume"]),
            "market_cap": _json_value(market_cap),
        }
    except Exception as exc:
        # Keep network errors and malformed Yahoo responses from crashing MCP.
        return {"error": f"Could not fetch data for ticker '{symbol}': {exc}"}


class AlphaVantageFallbackConfirmation(BaseModel):
    """Form returned when a user accepts an Alpha Vantage fallback."""

    use_alpha_vantage: bool = Field(
        default=True,
        description="Use one Alpha Vantage request to supplement the Yahoo Finance result.",
    )


def _client_supports_form_elicitation(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        capability = mcp_types.ClientCapabilities(
            elicitation=mcp_types.ElicitationCapability(
                form=mcp_types.FormElicitationCapability()
            )
        )
        return ctx.session.check_client_capability(capability)
    except (AttributeError, RuntimeError):
        return False


async def _offer_av_fallback(
    result: dict,
    ctx: Context | None,
    fallback,
    gap: str,
) -> dict:
    """Offer an explicit, reusable Alpha Vantage supplementary lookup."""
    audit = result.get("coverage_audit", {})
    if audit.get("status") not in {"partial", "unavailable"}:
        return result

    message = (
        f"Yahoo Finance doesn't have {gap}. Check Alpha Vantage instead? "
        "Uses 1 of your 25 daily requests."
    )
    offer = {
        "provider": "Alpha Vantage",
        "message": message,
        "requires_user_confirmation": True,
    }
    if not _client_supports_form_elicitation(ctx):
        audit["suggestion"] = offer
        return result

    elicitation = await ctx.elicit(message, AlphaVantageFallbackConfirmation)
    if (
        elicitation.action != "accept"
        or not getattr(elicitation, "data", None)
        or not elicitation.data.use_alpha_vantage
    ):
        audit["alpha_vantage_offer"] = {
            **offer,
            "outcome": "declined" if elicitation.action == "decline" else "cancelled",
        }
        return result

    supplementary = fallback()
    if supplementary.get("error_type") == "quota_exhausted":
        audit["status"] = "unavailable"
        audit.setdefault("missing", {})["reason"] = supplementary["coverage_audit"][
            "missing"
        ]["reason"]
        audit["alpha_vantage_offer"] = {**offer, "outcome": "accepted_quota_exhausted"}
        result["alpha_vantage"] = supplementary
        return result

    if supplementary.get("error"):
        audit["alpha_vantage_offer"] = {**offer, "outcome": "accepted_unavailable"}
        result["alpha_vantage"] = supplementary
        return result

    result.pop("error", None)
    result.pop("error_type", None)
    for key, value in supplementary.items():
        if key not in {"coverage_audit", "fallback_recommendation"} and result.get(key) is None:
            result[key] = value
    result["alpha_vantage"] = supplementary
    audit["status"] = "complete"
    audit["supplementary_source"] = {
        "provider": "Alpha Vantage",
        "source_status": "supplementary",
        "filled_gap": gap,
    }
    audit["alpha_vantage_offer"] = {**offer, "outcome": "accepted_filled"}
    return result


@mcp.tool(
    title="Get Stock Quote",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_stock_quote(ticker: str = "", ctx: Context | None = None) -> dict:
    """Return a Yahoo quote and optionally offer Alpha Vantage for missing fields."""
    result = _get_stock_quote_yahoo(ticker)
    symbol = _normalize_text(ticker).upper()
    missing = result.get("coverage_audit", {}).get("missing", {}).get("fields", [])
    gap = ", ".join(missing) if missing else f"complete quote data for {symbol}"
    return await _offer_av_fallback(
        result,
        ctx,
        lambda: get_stock_quote_av(symbol),
        gap,
    )


AV_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _av_request(params: dict[str, str]) -> dict:
    try:
        return alphavantage_request(params)
    except Exception as exc:
        return {"error": f"Could not fetch Alpha Vantage data: {exc}"}


def _av_number(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, "None", "-"} else None
    except (TypeError, ValueError):
        return None


@mcp.tool(title="Get Stock Quote (Alpha Vantage)", annotations=AV_TOOL_ANNOTATIONS)
@_coverage_tool("prices", "Alpha Vantage")
def get_stock_quote_av(symbol: str = "") -> dict:
    """Return a distinct Alpha Vantage global quote; consumes one shared AV request."""
    ticker = _normalize_text(symbol).upper()
    if not ticker:
        return {"error": "Please provide a symbol."}
    payload = _av_request({"function": "GLOBAL_QUOTE", "symbol": ticker})
    if payload.get("error"):
        return payload
    quote = payload.get("Global Quote") or {}
    if not quote:
        return {"error": f"Alpha Vantage returned no quote for '{ticker}'."}
    return {
        "ticker": ticker,
        "current_price": _av_number(quote.get("05. price")),
        "previous_close": _av_number(quote.get("08. previous close")),
        "day_high": _av_number(quote.get("03. high")),
        "day_low": _av_number(quote.get("04. low")),
        "volume": _av_number(quote.get("06. volume")),
        "latest_trading_day": quote.get("07. latest trading day"),
        "change": _av_number(quote.get("09. change")),
        "change_percent": quote.get("10. change percent"),
    }


@mcp.tool(title="Get Technical Indicator (Alpha Vantage)", annotations=AV_TOOL_ANNOTATIONS)
@_coverage_tool("technical_indicator", "Alpha Vantage", "values")
def get_technical_indicator_av(
    symbol: str = "",
    indicator: str = "SMA",
    interval: str = "daily",
    time_period: int = 20,
) -> dict:
    """Return SMA, EMA, RSI, MACD, or BBANDS from Alpha Vantage."""
    ticker = _normalize_text(symbol).upper()
    selected = _normalize_text(indicator).upper()
    valid = {"SMA", "EMA", "RSI", "MACD", "BBANDS"}
    valid_intervals = {"1min", "5min", "15min", "30min", "60min", "daily", "weekly", "monthly"}
    if not ticker:
        return {"error": "Please provide a symbol."}
    if selected not in valid:
        return {"error": f"Invalid indicator. Choose one of: {', '.join(sorted(valid))}."}
    if interval not in valid_intervals:
        return {"error": "Invalid interval for Alpha Vantage technical indicators."}
    if isinstance(time_period, bool) or not isinstance(time_period, int) or time_period < 1:
        return {"error": "Invalid time_period. Provide a positive integer."}
    params = {"function": selected, "symbol": ticker, "interval": interval, "series_type": "close"}
    if selected != "MACD":
        params["time_period"] = str(time_period)
    payload = _av_request(params)
    if payload.get("error"):
        return payload
    key = next((key for key in payload if key.startswith("Technical Analysis")), None)
    observations = payload.get(key, {}) if key else {}
    if not observations:
        return {"error": f"Alpha Vantage returned no {selected} data for '{ticker}'."}
    values = [
        {"date": timestamp, **{name.lower().replace(" ", "_"): _av_number(value) for name, value in row.items()}}
        for timestamp, row in list(observations.items())[:250]
    ]
    return {"ticker": ticker, "indicator": selected, "interval": interval, "values": values}


@mcp.tool(title="Get Forex Rate (Alpha Vantage)", annotations=AV_TOOL_ANNOTATIONS)
@_coverage_tool("forex_rate", "Alpha Vantage")
def get_forex_rate_av(from_currency: str = "", to_currency: str = "") -> dict:
    """Return an Alpha Vantage currency exchange rate."""
    source = _normalize_text(from_currency).upper()
    target = _normalize_text(to_currency).upper()
    if not source or not target:
        return {"error": "Please provide from_currency and to_currency."}
    payload = _av_request({"function": "CURRENCY_EXCHANGE_RATE", "from_currency": source, "to_currency": target})
    if payload.get("error"):
        return payload
    data = payload.get("Realtime Currency Exchange Rate") or {}
    if not data:
        return {"error": f"Alpha Vantage returned no exchange rate for {source}/{target}."}
    return {
        "from_currency": source,
        "to_currency": target,
        "exchange_rate": _av_number(data.get("5. Exchange Rate")),
        "bid_price": _av_number(data.get("8. Bid Price")),
        "ask_price": _av_number(data.get("9. Ask Price")),
        "last_refreshed": data.get("6. Last Refreshed"),
    }


@mcp.tool(title="Get Crypto Quote (Alpha Vantage)", annotations=AV_TOOL_ANNOTATIONS)
@_coverage_tool("crypto_quote", "Alpha Vantage")
def get_crypto_quote_av(symbol: str = "", market: str = "USD") -> dict:
    """Return a crypto-to-market exchange quote from Alpha Vantage."""
    crypto = _normalize_text(symbol).upper()
    target = _normalize_text(market).upper()
    if not crypto or not target:
        return {"error": "Please provide symbol and market."}
    payload = _av_request({"function": "CURRENCY_EXCHANGE_RATE", "from_currency": crypto, "to_currency": target})
    if payload.get("error"):
        return payload
    data = payload.get("Realtime Currency Exchange Rate") or {}
    if not data:
        return {"error": f"Alpha Vantage returned no crypto quote for {crypto}/{target}."}
    return {
        "symbol": crypto,
        "market": target,
        "exchange_rate": _av_number(data.get("5. Exchange Rate")),
        "bid_price": _av_number(data.get("8. Bid Price")),
        "ask_price": _av_number(data.get("9. Ask Price")),
        "last_refreshed": data.get("6. Last Refreshed"),
    }


@mcp.tool(title="Get Economic Indicator (Alpha Vantage)", annotations=AV_TOOL_ANNOTATIONS)
@_coverage_tool("economic_indicator", "Alpha Vantage", "values")
def get_economic_indicator_av(indicator: str = "") -> dict:
    """Return a supported US macroeconomic series from Alpha Vantage."""
    selected = _normalize_text(indicator).upper()
    valid = {"REAL_GDP", "CPI", "UNEMPLOYMENT", "FEDERAL_FUNDS_RATE", "TREASURY_YIELD"}
    if selected not in valid:
        return {"error": f"Invalid indicator. Choose one of: {', '.join(sorted(valid))}."}
    params = {"function": selected}
    if selected == "TREASURY_YIELD":
        params.update({"interval": "monthly", "maturity": "10year"})
    payload = _av_request(params)
    if payload.get("error"):
        return payload
    values = [
        {"date": row.get("date"), "value": _av_number(row.get("value"))}
        for row in payload.get("data", [])[:250]
    ]
    if not values:
        return {"error": f"Alpha Vantage returned no data for {selected}."}
    return {"indicator": selected, "name": payload.get("name"), "interval": payload.get("interval"), "unit": payload.get("unit"), "values": values}


@mcp.tool(title="Get News Sentiment (Alpha Vantage)", annotations=AV_TOOL_ANNOTATIONS)
@_coverage_tool("news_sentiment", "Alpha Vantage", "news", 50)
def get_news_sentiment_av(tickers_or_topics: str = "") -> dict:
    """Return Alpha Vantage news sentiment for comma-separated tickers or topics."""
    query = _normalize_text(tickers_or_topics)
    if not query:
        return {"error": "Please provide tickers_or_topics."}
    parameter = "topics" if query.lower().startswith("topics:") else "tickers"
    value = query.split(":", 1)[1].strip() if parameter == "topics" else query.upper()
    payload = _av_request({"function": "NEWS_SENTIMENT", parameter: value, "limit": "50"})
    if payload.get("error"):
        return payload
    feed = payload.get("feed") or []
    if not feed:
        return {"error": f"Alpha Vantage returned no news sentiment for '{query}'."}
    news = [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("time_published"),
            "source": item.get("source"),
            "summary": item.get("summary"),
            "overall_sentiment_score": _av_number(item.get("overall_sentiment_score")),
            "overall_sentiment_label": item.get("overall_sentiment_label"),
        }
        for item in feed[:50]
    ]
    return {"query": query, "news": news}


# Crunchbase is an opt-in, licensed private-market source. Its tools are
# registered only when CRUNCHBASE_API_KEY is set (see register_crunchbase_tools)
# and, like Alpha Vantage, are never blended into Yahoo Finance values.
CB_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
CB_CARD_LIMIT = 100  # Crunchbase card lookups return at most 100 rows.
CB_SEARCH_LIMIT = 10
NOT_DISCLOSED = "not disclosed"
_CB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,199}$")
_CB_ORGANIZATION_FIELDS = [
    "identifier",
    "short_description",
    "location_identifiers",
    "founded_on",
    "ipo_status",
    "stock_symbol",
    "stock_exchange_symbol",
]
_CB_ROUND_FIELDS = [
    "identifier",
    "announced_on",
    "investment_type",
    "money_raised",
    "lead_investor_identifiers",
    "post_money_valuation",
]
_CB_ACQUISITION_FIELDS = [
    "identifier",
    "acquirer_identifier",
    "acquiree_identifier",
    "announced_on",
    "acquisition_type",
    "status",
    "price",
]
_CB_INVESTMENT_FIELDS = [
    "identifier",
    "announced_on",
    "organization_identifier",
    "funding_round_identifier",
    "funding_round_investment_type",
    "money_invested",
    "is_lead_investor",
]


def _cb_request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    try:
        return crunchbase_request(method, path, params, body)
    except Exception as exc:
        return {"error": f"Could not fetch Crunchbase data: {exc}", "error_type": "provider_error"}


def _cb_name(identifier: Any) -> str | None:
    return identifier.get("value") if isinstance(identifier, dict) else None


def _cb_date(value: Any) -> str | None:
    """Read a plain date string or a DateWithPrecision object."""
    if isinstance(value, dict):
        return value.get("value")
    return value if isinstance(value, str) and value else None


def _cb_money(value: Any, name: str) -> dict:
    """Return a money field and its disclosure status; missing is never zero."""
    if isinstance(value, dict) and value.get("value") is not None:
        return {
            name: {
                "value": value.get("value"),
                "currency": value.get("currency"),
                "value_usd": value.get("value_usd"),
            },
            f"{name}_status": "disclosed",
        }
    return {name: None, f"{name}_status": NOT_DISCLOSED}


def _cb_entity_id(raw: Any, label: str) -> tuple[str | None, dict | None]:
    value = _normalize_text(raw)
    if not value:
        return None, {"error": f"Please provide {label} as a Crunchbase permalink or UUID (see search_organization_cb)."}
    if not _CB_ID_PATTERN.fullmatch(value):
        return None, {"error": f"Invalid {label} '{value}'. Use the permalink or UUID returned by search_organization_cb."}
    return value.lower(), None


def _cb_card(collection: str, entity_id: str, card_id: str, field_ids: list[str], limit: int = CB_CARD_LIMIT, order: str | None = None):
    """Fetch one card of an entity; returns (entity properties, rows, error)."""
    params = {"card_field_ids": ",".join(field_ids), "limit": str(limit)}
    if order:
        params["order"] = order
    payload = _cb_request("GET", crunchbase_path("entities", collection, entity_id, "cards", card_id), params)
    if payload.get("error"):
        return None, None, payload
    rows = (payload.get("cards") or {}).get(card_id) or []
    return payload.get("properties") or {}, [row for row in rows if isinstance(row, dict)], None


def _cb_entity_summary(properties: dict, fallback_id: str) -> dict:
    identifier = properties.get("identifier") or {}
    return {
        "name": identifier.get("value") or fallback_id,
        "permalink": identifier.get("permalink") or fallback_id,
    }


def _cb_sorted_by_date(rows: list[dict]) -> list[dict]:
    """Oldest first; undated rows last."""
    return sorted(rows, key=lambda row: (row.get("date") is None, row.get("date") or ""))


@_coverage_tool("private_funding", "Crunchbase", "results", CB_SEARCH_LIMIT)
def search_organization_cb(query: str = "") -> dict:
    """Find Crunchbase organizations by name; licensed, opt-in private-market data.

    Returns up to 10 candidates with permalink and UUID (pass either to the
    other ``_cb`` tools), short description, country, founded date and IPO
    status. Public or delisted companies also carry ``stock_symbol`` and
    ``stock_exchange`` for hand-off to the Yahoo Finance tools; Yahoo tickers
    for non-US listings need the exchange suffix (for example D05.SI).
    Never merge Crunchbase figures into Yahoo Finance figures.
    """
    name = _normalize_text(query)
    if not name:
        return {"error": "Please provide an organization name to search for."}
    if len(name) > 200:
        return {"error": "Invalid query: use at most 200 characters."}
    payload = _cb_request("POST", "/searches/organizations", body={
        "field_ids": _CB_ORGANIZATION_FIELDS,
        "query": [{
            "type": "predicate",
            "field_id": "identifier",
            "operator_id": "contains",
            "values": [name],
        }],
        "order": [{"field_id": "rank_org", "sort": "asc", "nulls": "last"}],
        "limit": CB_SEARCH_LIMIT,
    })
    if payload.get("error"):
        return payload

    results = []
    for entity in (payload.get("entities") or [])[:CB_SEARCH_LIMIT]:
        if not isinstance(entity, dict):
            continue
        properties = entity.get("properties") or {}
        identifier = properties.get("identifier") or {}
        country = next(
            (
                location.get("value")
                for location in properties.get("location_identifiers") or []
                if isinstance(location, dict) and location.get("location_type") == "country"
            ),
            None,
        )
        candidate = {
            "name": identifier.get("value"),
            "permalink": identifier.get("permalink"),
            "uuid": entity.get("uuid") or identifier.get("uuid"),
            "short_description": properties.get("short_description"),
            "country": country,
            "founded_on": _cb_date(properties.get("founded_on")),
            "ipo_status": properties.get("ipo_status"),
        }
        symbol = _cb_name(properties.get("stock_symbol"))
        if symbol:
            candidate["stock_symbol"] = symbol
            candidate["stock_exchange"] = properties.get("stock_exchange_symbol")
        results.append(candidate)

    response = {"query": name, "results": results}
    if any("stock_symbol" in candidate for candidate in results):
        response["note"] = (
            "stock_symbol is Crunchbase's listing symbol. Use it with the Yahoo "
            "Finance tools (adding the exchange suffix for non-US listings) and "
            "report Yahoo and Crunchbase figures separately."
        )
    return response


@_coverage_tool("private_funding", "Crunchbase", "funding_rounds")
def get_funding_rounds_cb(organization: str = "") -> dict:
    """Return an organization's Crunchbase funding rounds, oldest first.

    ``organization`` is a permalink or UUID from ``search_organization_cb``.
    Each round has its announced date, round type, amount and currency, lead
    investors and post-money valuation. Undisclosed amounts or valuations are
    ``null`` with ``*_status: "not disclosed"``; they are never zero.
    """
    entity_id, error = _cb_entity_id(organization, "an organization")
    if error:
        return error
    properties, rows, error = _cb_card("organizations", entity_id, "raised_funding_rounds", _CB_ROUND_FIELDS)
    if error:
        return error

    rounds = [
        {
            "date": _cb_date(row.get("announced_on")),
            "round": _cb_name(row.get("identifier")),
            "round_type": row.get("investment_type"),
            **_cb_money(row.get("money_raised"), "amount"),
            "lead_investors": [
                name for name in (_cb_name(item) for item in row.get("lead_investor_identifiers") or []) if name
            ],
            **_cb_money(row.get("post_money_valuation"), "post_money_valuation"),
        }
        for row in rows
    ]
    return {
        "organization": _cb_entity_summary(properties, entity_id),
        "funding_rounds": _cb_sorted_by_date(rounds),
        "_truncated": len(rows) >= CB_CARD_LIMIT,
    }


@_coverage_tool("private_funding", "Crunchbase", "acquisitions")
def get_acquisitions_cb(organization: str = "") -> dict:
    """Return Crunchbase acquisitions where the organization was acquirer or acquiree.

    Each row has a ``role`` (``acquirer`` or ``acquiree``), the counterparty
    names, announced date, type, status and price. An undisclosed price is
    ``null`` with ``price_status: "not disclosed"``, never zero.
    """
    entity_id, error = _cb_entity_id(organization, "an organization")
    if error:
        return error

    properties = {}
    acquisitions = []
    errors = []
    truncated = False
    for role, card_id in (("acquirer", "acquirer_acquisitions"), ("acquiree", "acquiree_acquisitions")):
        card_properties, rows, card_error = _cb_card("organizations", entity_id, card_id, _CB_ACQUISITION_FIELDS)
        if card_error:
            if card_error.get("error_type") == "auth_error":
                return card_error
            errors.append((role, card_error))
            continue
        properties = properties or card_properties
        truncated = truncated or len(rows) >= CB_CARD_LIMIT
        acquisitions.extend(
            {
                "role": role,
                "date": _cb_date(row.get("announced_on")),
                "acquirer": _cb_name(row.get("acquirer_identifier")),
                "acquiree": _cb_name(row.get("acquiree_identifier")),
                "acquisition_type": row.get("acquisition_type"),
                "status": row.get("status"),
                **_cb_money(row.get("price"), "price"),
            }
            for row in rows
        )

    if len(errors) == 2:
        return errors[0][1]
    acquisitions = _cb_sorted_by_date(acquisitions)
    # A failed card is kept as an error row so the audit reports "partial".
    acquisitions.extend({"role": role, "error": card_error["error"]} for role, card_error in errors)
    return {
        "organization": _cb_entity_summary(properties, entity_id),
        "acquisitions": acquisitions,
        "_truncated": truncated,
    }


@_coverage_tool("private_funding", "Crunchbase", "investments")
def get_investor_portfolio_cb(investor: str = "", limit: int = 25) -> dict:
    """Return an investor's most recent Crunchbase investments (1-100).

    ``investor`` is an organization or person permalink or UUID. Each row has
    the announced date, investee, funding round, round type, whether the
    investor led it, and the amount invested; undisclosed amounts are
    ``null`` with ``amount_status: "not disclosed"``, never zero.
    """
    entity_id, error = _cb_entity_id(investor, "an investor")
    if error:
        return error
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= CB_CARD_LIMIT:
        return {"error": f"Invalid limit. Provide an integer from 1 to {CB_CARD_LIMIT}."}

    investor_kind = "organization"
    properties, rows, error = _cb_card(
        "organizations", entity_id, "participated_investments", _CB_INVESTMENT_FIELDS, limit, "announced_on desc"
    )
    if error and error.get("http_status") == 404:
        investor_kind = "person"
        properties, rows, error = _cb_card(
            "people", entity_id, "participated_investments", _CB_INVESTMENT_FIELDS, limit, "announced_on desc"
        )
    if error:
        return error

    investments = [
        {
            "date": _cb_date(row.get("announced_on")),
            "organization": _cb_name(row.get("organization_identifier")),
            "organization_permalink": (row.get("organization_identifier") or {}).get("permalink"),
            "funding_round": _cb_name(row.get("funding_round_identifier")),
            "round_type": row.get("funding_round_investment_type"),
            "is_lead_investor": row.get("is_lead_investor"),
            **_cb_money(row.get("money_invested"), "amount"),
        }
        for row in rows
    ]
    return {
        "investor": {**_cb_entity_summary(properties, entity_id), "investor_kind": investor_kind},
        "investments": investments,
        "_truncated": len(rows) >= limit,
    }


CB_TOOLS = {
    "search_organization_cb": ("Search Organization (Crunchbase)", search_organization_cb),
    "get_funding_rounds_cb": ("Get Funding Rounds (Crunchbase)", get_funding_rounds_cb),
    "get_acquisitions_cb": ("Get Acquisitions (Crunchbase)", get_acquisitions_cb),
    "get_investor_portfolio_cb": ("Get Investor Portfolio (Crunchbase)", get_investor_portfolio_cb),
}


def register_crunchbase_tools(target: FastMCP) -> bool:
    """Register the _cb tools only when CRUNCHBASE_API_KEY is configured."""
    if not crunchbase_key_configured():
        return False
    for name, (title, function) in CB_TOOLS.items():
        target.tool(name=name, title=title, annotations=CB_TOOL_ANNOTATIONS)(function)
    return True


CRUNCHBASE_ENABLED = register_crunchbase_tools(mcp)


@mcp.tool(
    title="Get Historical Prices",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("prices", "Yahoo Finance", "prices", 250)
def get_historical_prices(
    ticker: str = "", period: str = "1mo", interval: str = "1d"
) -> dict:
    """Return historical OHLCV prices from Yahoo Finance.

    ``open``, ``high``, ``low`` and ``close`` are traded prices, adjusted for
    stock splits but not for dividends. ``adj_close`` is Yahoo's
    dividend-and-split-adjusted close, suitable for total-return estimates.
    Results are limited to the most recent 250 rows, so older data may be
    truncated when the requested range and interval would exceed that limit.
    """
    valid_periods = {
        "1d",
        "5d",
        "1mo",
        "3mo",
        "6mo",
        "1y",
        "2y",
        "5y",
        "10y",
        "ytd",
        "max",
    }
    valid_intervals = {"1d", "1wk", "1mo"}

    symbol = _normalize_text(ticker).upper()
    selected_period = _normalize_text(period).lower()
    selected_interval = _normalize_text(interval).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_period not in valid_periods:
        return {
            "error": (
                f"Invalid period '{period}'. Valid periods are: "
                f"{', '.join(sorted(valid_periods))}."
            )
        }
    if selected_interval not in valid_intervals:
        return {
            "error": (
                f"Invalid interval '{interval}'. Valid intervals are: "
                f"{', '.join(sorted(valid_intervals))}."
            )
        }

    try:
        # auto_adjust=False keeps traded closes in "Close" and puts the
        # dividend-adjusted series in "Adj Close", so both can be reported.
        history = yf.Ticker(symbol).history(
            period=selected_period, interval=selected_interval, auto_adjust=False
        )
        if history.empty:
            return {"error": f"Invalid ticker '{symbol}': no market data was found."}

        prices = []
        for timestamp, row in history.tail(250).iterrows():
            prices.append(
                {
                    "date": timestamp.date().isoformat(),
                    "open": _json_value(row["Open"]),
                    "high": _json_value(row["High"]),
                    "low": _json_value(row["Low"]),
                    "close": _json_value(row["Close"]),
                    "adj_close": _json_value(row.get("Adj Close")),
                    "volume": _json_value(row["Volume"]),
                }
            )

        return {
            "ticker": symbol,
            "period": selected_period,
            "interval": selected_interval,
            "price_basis": "open/high/low/close are traded prices adjusted for splits only; adj_close is also adjusted for dividends",
            "prices": prices,
        }
    except Exception as exc:
        return {
            "error": f"Could not fetch historical data for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Get Financial Statements",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("financial_statements", "Yahoo Finance", "data", 8)
def get_financial_statements(
    ticker: str = "", statement: str = "income", period: str = "annual"
) -> dict:
    """Return financial statement periods and line items from Yahoo Finance.

    Results are limited to the 8 most recent reporting periods, so older
    statement periods may be truncated.
    """
    statement_attributes = {
        ("income", "annual"): "income_stmt",
        ("income", "quarterly"): "quarterly_income_stmt",
        ("balance", "annual"): "balance_sheet",
        ("balance", "quarterly"): "quarterly_balance_sheet",
        ("cashflow", "annual"): "cashflow",
        ("cashflow", "quarterly"): "quarterly_cashflow",
    }
    valid_statements = {"income", "balance", "cashflow"}
    valid_periods = {"annual", "quarterly"}

    symbol = _normalize_text(ticker).upper()
    selected_statement = _normalize_text(statement).lower()
    selected_period = _normalize_text(period).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_statement not in valid_statements:
        return {
            "error": (
                f"Invalid statement '{statement}'. Valid statements are: "
                f"{', '.join(sorted(valid_statements))}."
            )
        }
    if selected_period not in valid_periods:
        return {
            "error": (
                f"Invalid period '{period}'. Valid periods are: "
                f"{', '.join(sorted(valid_periods))}."
            )
        }

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        attribute = statement_attributes[(selected_statement, selected_period)]
        statement_data = getattr(stock, attribute)
        data = []
        if statement_data is not None and not statement_data.empty:
            periods = statement_data.T.sort_index(ascending=False).head(8)
            for period_ending, row in periods.iterrows():
                record = {"period_ending": period_ending.date().isoformat()}
                record.update(
                    {str(line_item): _json_value(value) for line_item, value in row.items()}
                )
                data.append(record)

        return {
            "ticker": symbol,
            "statement": selected_statement,
            "period": selected_period,
            "data": data,
        }
    except Exception as exc:
        return {
            "error": f"Could not fetch financial statements for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Get Corporate Actions",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("corporate_actions", "Yahoo Finance", limit=200)
def get_corporate_actions(ticker: str = "", action_type: str = "all") -> dict:
    """Return dividends and stock splits from Yahoo Finance.

    Each requested action type is limited to its 100 most recent entries, so
    older corporate actions may be truncated.
    """
    valid_action_types = {"all", "dividends", "splits"}
    symbol = _normalize_text(ticker).upper()
    selected_action_type = _normalize_text(action_type).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_action_type not in valid_action_types:
        return {
            "error": (
                f"Invalid action_type '{action_type}'. Valid action types are: "
                f"{', '.join(sorted(valid_action_types))}."
            )
        }

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        dividends = None
        splits = None

        if selected_action_type in {"all", "dividends"}:
            dividend_series = stock.dividends
            dividends = []
            if dividend_series is not None and not dividend_series.empty:
                dividends = [
                    {
                        "date": timestamp.date().isoformat(),
                        "amount": _json_value(amount),
                    }
                    for timestamp, amount in dividend_series.sort_index(ascending=False)
                    .head(100)
                    .items()
                ]

        if selected_action_type in {"all", "splits"}:
            split_series = stock.splits
            splits = []
            if split_series is not None and not split_series.empty:
                splits = [
                    {"date": timestamp.date().isoformat(), "ratio": _json_value(ratio)}
                    for timestamp, ratio in split_series.sort_index(ascending=False)
                    .head(100)
                    .items()
                ]

        return {"ticker": symbol, "dividends": dividends, "splits": splits}
    except Exception as exc:
        return {
            "error": f"Could not fetch corporate actions for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Get Holders",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("ownership", "Yahoo Finance", "data", 20)
def get_holders(ticker: str = "", holder_type: str = "major") -> dict:
    """Return holder or insider-transaction data from Yahoo Finance.

    Institutional holders and insider transactions are limited to the top 20
    rows, so additional rows may be truncated.
    """
    holder_attributes = {
        "major": "major_holders",
        "institutional": "institutional_holders",
        "insider_transactions": "insider_transactions",
    }
    symbol = _normalize_text(ticker).upper()
    selected_holder_type = _normalize_text(holder_type).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_holder_type in {"historical", "historical_ownership"}:
        return {
            "error": "Unsupported holder_type: Yahoo Finance does not provide point-in-time historical ownership through this tool."
        }
    if selected_holder_type not in holder_attributes:
        return {
            "error": (
                f"Invalid holder_type '{holder_type}'. Valid holder types are: "
                f"{', '.join(sorted(holder_attributes))}."
            )
        }

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        holder_data = getattr(stock, holder_attributes[selected_holder_type])
        data = []
        if holder_data is not None and not holder_data.empty:
            if selected_holder_type in {"institutional", "insider_transactions"}:
                holder_data = holder_data.head(20)
            for row_index, row in holder_data.iterrows():
                record = {
                    str(column): _json_value(value) for column, value in row.items()
                }
                # Current yfinance versions store major-holder categories in
                # the dataframe index rather than in a regular column.
                if selected_holder_type == "major":
                    record = {"Breakdown": str(row_index), **record}
                data.append(record)

        return {"ticker": symbol, "holder_type": selected_holder_type, "data": data}
    except Exception as exc:
        return {"error": f"Could not fetch holder data for ticker '{symbol}': {exc}"}


@mcp.tool(
    title="Get Sector Data",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("market_classification", "Yahoo Finance", "companies", 15)
def get_sector_data(key: str = "", level: str = "sector") -> dict:
    """Return a Yahoo Finance sector or industry overview and top companies.

    Valid sector key examples include ``technology``, ``financial-services``,
    and ``healthcare``. Valid industry examples include ``semiconductors``,
    ``computer-hardware``, and ``solar``. Companies are limited to the top 15,
    so additional companies may be truncated.
    """
    valid_levels = {"sector", "industry"}
    sector_keys = set(SECTOR_INDUSTY_MAPPING_LC)
    industry_keys = {
        industry
        for industries in SECTOR_INDUSTY_MAPPING_LC.values()
        for industry in industries
    }
    selected_key = _normalize_text(key).lower()
    selected_level = _normalize_text(level).lower()

    if not selected_key:
        return {"error": "Please provide a sector or industry key."}
    if selected_level not in valid_levels:
        return {
            "error": (
                f"Invalid level '{level}'. Valid levels are: "
                f"{', '.join(sorted(valid_levels))}."
            )
        }

    valid_keys = sector_keys if selected_level == "sector" else industry_keys
    if selected_key not in valid_keys:
        examples = (
            "technology, financial-services, healthcare"
            if selected_level == "sector"
            else "semiconductors, computer-hardware, solar"
        )
        return {
            "error": (
                f"Invalid {selected_level} key '{key}'. Examples of valid keys are: "
                f"{examples}."
            )
        }

    try:
        domain = (
            yf.Sector(selected_key)
            if selected_level == "sector"
            else yf.Industry(selected_key)
        )
        overview = {
            "name": domain.name,
            **{str(field): _json_value(value) for field, value in domain.overview.items()},
        }
        companies = []
        top_companies = domain.top_companies
        if top_companies is not None and not top_companies.empty:
            for ticker_symbol, row in top_companies.head(15).iterrows():
                companies.append(
                    {
                        "ticker": str(ticker_symbol),
                        "name": _json_value(row.get("name")),
                        "market_weight": _json_value(row.get("market weight")),
                    }
                )

        return {
            "key": selected_key,
            "level": selected_level,
            "overview": overview,
            "companies": companies,
        }
    except Exception as exc:
        return {
            "error": f"Could not fetch {selected_level} data for key '{selected_key}': {exc}"
        }


@mcp.tool(
    title="Get Stock Screener",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("market_screen", "Yahoo Finance", "results", 25)
def get_stock_screener(screen_name: str = "day_gainers") -> dict:
    """Return results from a predefined Yahoo Finance stock screener.

    Results are limited to the top 25 entries, so additional matches may be
    truncated.
    """
    selected_screen = _normalize_text(screen_name).lower()
    valid_screens = set(yf.PREDEFINED_SCREENER_QUERIES)
    if selected_screen not in valid_screens:
        return {
            "error": (
                f"Invalid screen_name '{screen_name}'. Valid screens are: "
                f"{', '.join(sorted(valid_screens))}."
            )
        }

    try:
        response = yf.screen(selected_screen, count=25)
        results = []
        for quote in response.get("quotes", [])[:25]:
            results.append(
                {
                    "ticker": quote.get("symbol"),
                    "name": quote.get("longName") or quote.get("shortName"),
                    "price": _json_value(quote.get("regularMarketPrice")),
                    "change": _json_value(quote.get("regularMarketChange")),
                    "percent_change": _json_value(
                        quote.get("regularMarketChangePercent")
                    ),
                    "volume": _json_value(quote.get("regularMarketVolume")),
                    "market_cap": _json_value(quote.get("marketCap")),
                }
            )
        return {"screen_name": selected_screen, "results": results}
    except Exception as exc:
        return {"error": f"Could not run stock screener '{selected_screen}': {exc}"}


@mcp.tool(
    title="Get News",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("news", "Yahoo Finance", "news", 10)
def get_news(ticker: str = "") -> dict:
    """Return the 10 most recent Yahoo Finance news items for a ticker.

    Results are limited to 10 items, so older news may be truncated.
    """
    symbol = _normalize_text(ticker).upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        items = []
        for raw_item in (stock.news or [])[:10]:
            content = raw_item.get("content") or raw_item
            provider = content.get("provider") or {}
            link_data = content.get("canonicalUrl") or content.get("clickThroughUrl")
            link = link_data.get("url") if isinstance(link_data, dict) else link_data
            items.append(
                {
                    "title": content.get("title"),
                    "publisher": provider.get("displayName")
                    or content.get("publisher"),
                    "link": link or content.get("link"),
                    "published_time": _iso_datetime(
                        content.get("pubDate") or content.get("providerPublishTime")
                    ),
                }
            )
        return {"ticker": symbol, "news": items}
    except Exception as exc:
        return {"error": f"Could not fetch news for ticker '{symbol}': {exc}"}


@mcp.tool(
    title="Get Sustainability",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("sustainability", "Yahoo Finance", "data")
def get_sustainability(ticker: str = "") -> dict:
    """Return available Yahoo Finance ESG and sustainability scores."""
    symbol = _normalize_text(ticker).upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        sustainability = stock.sustainability
        if sustainability is None or sustainability.empty:
            return {
                "ticker": symbol,
                "message": f"No sustainability data is available for ticker '{symbol}'.",
                "data": {},
            }

        scores = {}
        if len(sustainability.columns) == 1:
            value_column = sustainability.columns[0]
            scores = {
                str(score): _json_value(value)
                for score, value in sustainability[value_column].items()
            }
        else:
            for score, row in sustainability.iterrows():
                for column, value in row.items():
                    scores[f"{score} ({column})"] = _json_value(value)

        return {"ticker": symbol, "data": scores}
    except Exception as exc:
        return {
            "error": f"Could not fetch sustainability data for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Search Finance Research",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("literature", "OpenAlex", "results", 50)
def search_finance_research(
    query: str = "", year_from: int = None, limit: int = 10
) -> dict:
    """Search OpenAlex for business, economics, finance, and accounting works.

    Results are limited to at most 50 works, so additional matches may be
    truncated. Replace ``OPENALEX_MAILTO`` with a deployment contact email.
    """
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return {"error": "Please provide a research search query."}
    if year_from is not None and (
        isinstance(year_from, bool)
        or not isinstance(year_from, int)
        or year_from < 1000
        or year_from > datetime.now().year
    ):
        return {
            "error": (
                f"Invalid year_from '{year_from}'. Provide a year from 1000 through "
                f"{datetime.now().year}."
            )
        }
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return {"error": "Invalid limit. Provide an integer from 1 through 50."}

    filters = ["topics.field.id:14|20"]
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from}-01-01")
    try:
        payload = _research_http_get_json(
            "https://api.openalex.org/works",
            {
                "search": normalized_query,
                "filter": ",".join(filters),
                "per-page": limit,
                "mailto": OPENALEX_MAILTO,
            },
        )
        works = payload.get("results") or []
        if not works:
            return {"error": f"No finance research found for query '{normalized_query}'."}

        results = []
        for work in works[:limit]:
            location = work.get("primary_location") or {}
            source = location.get("source") or {}
            oa_location = work.get("best_oa_location") or {}
            abstract = _openalex_abstract(work.get("abstract_inverted_index"))
            results.append(
                {
                    "title": work.get("title") or work.get("display_name"),
                    "authors": _openalex_authors(work),
                    "year": work.get("publication_year"),
                    "journal": source.get("display_name"),
                    "doi": work.get("doi"),
                    "open_access_pdf_url": oa_location.get("pdf_url")
                    or location.get("pdf_url"),
                    "abstract_snippet": _snippet(abstract),
                    "cited_by_count": work.get("cited_by_count", 0),
                }
            )
        return {"query": normalized_query, "results": results}
    except Exception as exc:
        return {"error": f"Could not search OpenAlex: {exc}"}


@mcp.tool(
    title="Get Research Paper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("literature", "OpenAlex")
def get_research_paper(doi_or_openalex_id: str = "") -> dict:
    """Return full OpenAlex details for one DOI or OpenAlex work ID."""
    identifier = _normalize_text(doi_or_openalex_id)
    if not identifier:
        return {"error": "Please provide a DOI or OpenAlex work ID."}

    if identifier.lower().startswith("doi:"):
        identifier = identifier[4:].strip()
    if identifier.lower().startswith("https://doi.org/"):
        identifier = f"https://doi.org/{identifier.split('/', 3)[-1]}"
    elif re.fullmatch(r"10\.\d{4,9}/\S+", identifier, flags=re.IGNORECASE):
        identifier = f"https://doi.org/{identifier}"
    elif identifier.lower().startswith("https://openalex.org/"):
        identifier = identifier.rstrip("/").rsplit("/", 1)[-1]
    elif not re.fullmatch(r"W\d+", identifier, flags=re.IGNORECASE):
        return {
            "error": (
                f"Invalid paper identifier '{doi_or_openalex_id}'. Provide a DOI or "
                "an OpenAlex work ID such as W2741809807."
            )
        }

    try:
        work = _research_http_get_json(
            f"https://api.openalex.org/works/{quote(identifier, safe=':/')}",
            {"mailto": OPENALEX_MAILTO},
        )
        topics = []
        for topic in work.get("topics") or []:
            topics.append(
                {
                    "name": topic.get("display_name"),
                    "score": topic.get("score"),
                    "subfield": (topic.get("subfield") or {}).get("display_name"),
                    "field": (topic.get("field") or {}).get("display_name"),
                    "domain": (topic.get("domain") or {}).get("display_name"),
                }
            )
        concepts = [
            {"name": concept.get("display_name"), "score": concept.get("score")}
            for concept in (work.get("concepts") or [])
        ]
        location = work.get("primary_location") or {}
        source = location.get("source") or {}
        return {
            "openalex_id": work.get("id"),
            "doi": work.get("doi"),
            "title": work.get("title") or work.get("display_name"),
            "authors": _openalex_authors(work),
            "year": work.get("publication_year"),
            "journal": source.get("display_name"),
            "abstract": _openalex_abstract(work.get("abstract_inverted_index")),
            "topics": topics,
            "concepts": concepts,
            "cited_by_count": work.get("cited_by_count", 0),
            "referenced_works_count": len(work.get("referenced_works") or []),
        }
    except _ResearchAPIError as exc:
        if exc.status == 404:
            return {"error": f"No OpenAlex work found for '{doi_or_openalex_id}'."}
        return {"error": f"Could not fetch the OpenAlex work: {exc}"}
    except Exception as exc:
        return {"error": f"Could not fetch the OpenAlex work: {exc}"}


@mcp.tool(
    title="Search Finance Preprints",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("literature", "arXiv", "results", 50)
def search_finance_preprints(query: str = "", limit: int = 10) -> dict:
    """Search arXiv quantitative-finance and economics preprints.

    Results are limited to at most 50 preprints, so additional matches may be
    truncated.
    """
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return {"error": "Please provide a preprint search query."}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return {"error": "Invalid limit. Provide an integer from 1 through 50."}

    try:
        xml_payload = _research_http_get(
            "https://export.arxiv.org/api/query",
            {
                "search_query": (
                    f'(all:"{normalized_query}") AND (cat:q-fin.* OR cat:econ.*)'
                ),
                "start": 0,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        root = ElementTree.fromstring(xml_payload)
        atom = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", atom)
        if not entries:
            return {"error": f"No finance preprints found for query '{normalized_query}'."}

        results = []
        for entry in entries[:limit]:
            entry_id = entry.findtext("atom:id", default="", namespaces=atom)
            pdf_link = None
            for link in entry.findall("atom:link", atom):
                if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                    pdf_link = link.get("href")
                    break
            results.append(
                {
                    "title": " ".join(
                        entry.findtext("atom:title", default="", namespaces=atom).split()
                    ),
                    "authors": [
                        author.findtext("atom:name", default="", namespaces=atom)
                        for author in entry.findall("atom:author", atom)
                    ],
                    "submission_date": entry.findtext(
                        "atom:published", default=None, namespaces=atom
                    ),
                    "arxiv_id": entry_id.rstrip("/").rsplit("/", 1)[-1],
                    "abstract_snippet": _snippet(
                        entry.findtext("atom:summary", default=None, namespaces=atom)
                    ),
                    "pdf_link": pdf_link,
                    "review_status": "preprint - not peer reviewed",
                }
            )
        return {"query": normalized_query, "results": results}
    except ElementTree.ParseError:
        return {"error": "Could not search arXiv: the service returned invalid XML."}
    except Exception as exc:
        return {"error": f"Could not search arXiv: {exc}"}


@mcp.tool(
    title="Check Journal Legitimacy",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("journal_directory", "DOAJ")
def check_journal_legitimacy(journal_name_or_issn: str = "") -> dict:
    """Check whether a journal is listed in DOAJ's vetted directory."""
    search_value = _normalize_text(journal_name_or_issn)
    if not search_value:
        return {"error": "Please provide a journal name or ISSN."}

    compact_issn = search_value.replace("-", "")
    is_issn = bool(re.fullmatch(r"\d{7}[\dXx]", compact_issn))
    query_field = "index.issn.exact" if is_issn else "bibjson.title"
    doaj_query = f'{query_field}:"{search_value.replace(chr(34), "")}"'
    try:
        payload = _research_http_get_json(
            f"https://doaj.org/api/search/journals/{quote(doaj_query, safe='')}",
            {"pageSize": 1},
        )
        results = payload.get("results") or []
        if not results:
            return {"query": search_value, "found": False}

        journal = results[0].get("bibjson") or {}
        issns = [
            identifier.get("id")
            for identifier in (journal.get("identifier") or [])
            if identifier.get("type") in {"pissn", "eissn"} and identifier.get("id")
        ]
        for issn_field in ("pissn", "eissn"):
            if journal.get(issn_field) and journal[issn_field] not in issns:
                issns.append(journal[issn_field])
        subjects = []
        for subject in journal.get("subject") or []:
            value = subject.get("term") or subject.get("code")
            if value and value not in subjects:
                subjects.append(value)
        return {
            "query": search_value,
            "found": True,
            "title": journal.get("title"),
            "issn": issns,
            "subject_areas": subjects,
        }
    except Exception as exc:
        return {"error": f"Could not check the DOAJ directory: {exc}"}


def _market_snapshot_for_ticker(raw_ticker: Any) -> dict:
    """Fetch one market snapshot entry without failing its surrounding batch."""
    symbol = _normalize_text(raw_ticker).upper()
    if not symbol:
        return {
            "ticker": raw_ticker if isinstance(raw_ticker, str) else None,
            "error": "Please provide a non-empty ticker symbol.",
        }
    try:
        quote_result = get_stock_quote(symbol)
        if isawaitable(quote_result):
            quote_result = asyncio.run(quote_result)
        if quote_result.get("error"):
            return {"ticker": symbol, "error": quote_result["error"]}
        return {
            "ticker": symbol,
            "current_price": quote_result.get("current_price"),
            "market_cap": quote_result.get("market_cap"),
        }
    except Exception as exc:
        return {"ticker": symbol, "error": f"Could not fetch market data: {exc}"}


@mcp.tool(
    title="Get Market Snapshot",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("prices", "Yahoo Finance", "results")
def get_market_snapshot(tickers: list[str] = None) -> dict:
    """Return concurrent price and market-cap snapshots for up to 25 tickers.

    Results are limited to 25 tickers. Successful entries are ranked by market
    cap descending, followed by any per-ticker error entries.
    """
    if not isinstance(tickers, list) or not tickers:
        return {"error": "Please provide a non-empty list of ticker symbols."}
    if len(tickers) > 25:
        return {"error": "Too many tickers. Provide at most 25 ticker symbols."}

    entries = []
    with ThreadPoolExecutor(max_workers=len(tickers)) as executor:
        futures = {
            executor.submit(_market_snapshot_for_ticker, ticker): index
            for index, ticker in enumerate(tickers)
        }
        indexed_results = {}
        for future in as_completed(futures):
            index = futures[future]
            try:
                indexed_results[index] = future.result()
            except Exception as exc:
                raw_ticker = tickers[index]
                indexed_results[index] = {
                    "ticker": _normalize_text(raw_ticker).upper() or None,
                    "error": f"Could not fetch market data: {exc}",
                }
        entries = [indexed_results[index] for index in range(len(tickers))]

    successful = [entry for entry in entries if "error" not in entry]
    failed = [entry for entry in entries if "error" in entry]
    successful.sort(
        key=lambda entry: (
            entry["market_cap"]
            if isinstance(entry.get("market_cap"), (int, float))
            else float("-inf")
        ),
        reverse=True,
    )
    return {"results": successful + failed}


def _finance_research_for_entity(entity: Any, limit: int) -> dict:
    """Run one existing OpenAlex search without failing its surrounding batch."""
    normalized_entity = _normalize_text(entity)
    if not normalized_entity:
        return {
            "entity": entity if isinstance(entity, str) else None,
            "error": "Please provide a non-empty company name or topic.",
        }
    try:
        result = search_finance_research(normalized_entity, limit=limit)
        if result.get("error"):
            return {"entity": normalized_entity, "error": result["error"]}
        return {
            "entity": normalized_entity,
            "results": (result.get("results") or [])[:limit],
        }
    except Exception as exc:
        return {"entity": normalized_entity, "error": f"Could not search OpenAlex: {exc}"}


@mcp.tool(
    title="Search Finance Research Batch",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@_coverage_tool("literature", "OpenAlex", "results")
def search_finance_research_batch(
    entities: list[str] = None, limit_per_entity: int = 10
) -> dict:
    """Search OpenAlex concurrently for up to 10 companies or topics.

    Each entity is limited to at most 50 results according to
    ``limit_per_entity``, so additional matches may be truncated.
    """
    if not isinstance(entities, list) or not entities:
        return {"error": "Please provide a non-empty list of companies or topics."}
    if len(entities) > 10:
        return {"error": "Too many entities. Provide at most 10 companies or topics."}
    if (
        isinstance(limit_per_entity, bool)
        or not isinstance(limit_per_entity, int)
        or not 1 <= limit_per_entity <= 50
    ):
        return {
            "error": "Invalid limit_per_entity. Provide an integer from 1 through 50."
        }

    with ThreadPoolExecutor(max_workers=len(entities)) as executor:
        futures = {
            executor.submit(_finance_research_for_entity, entity, limit_per_entity): index
            for index, entity in enumerate(entities)
        }
        indexed_results = {}
        for future in as_completed(futures):
            index = futures[future]
            try:
                indexed_results[index] = future.result()
            except Exception as exc:
                raw_entity = entities[index]
                indexed_results[index] = {
                    "entity": _normalize_text(raw_entity) or None,
                    "error": f"Could not search OpenAlex: {exc}",
                }

    return {"results": [indexed_results[index] for index in range(len(entities))]}


# ---------------------------------------------------------------------------
# Composite tools: fixed multi-step chains whose ratios are computed in code.
# They reuse the single-purpose tools above, use only each company's latest
# annual period, and audit only the fields they actually report.

_LINE_ITEMS = {
    "revenue": ("income", ["Total Revenue", "Operating Revenue"]),
    "net_income": ("income", ["Net Income", "Net Income Common Stockholders"]),
    "net_income_common": ("income", ["Net Income Common Stockholders", "Net Income"]),
    "total_debt": ("balance", ["Total Debt"]),
    "stockholders_equity": ("balance", ["Stockholders Equity", "Common Stock Equity"]),
    "free_cash_flow": ("cashflow", ["Free Cash Flow"]),
}

_METRIC_DATA_TYPE = {
    "current_price": "prices",
    "market_cap": "prices",
    "price_change_1y_pct": "prices",
    "total_return_1y_pct": "prices",
    "dividends_ttm": "corporate_actions",
    "dividend_yield_pct": "corporate_actions",
}

_METRIC_DEFINITIONS = {
    "net_margin_pct": "net_income / revenue x 100, latest annual period",
    "roe_pct": "net income to common stockholders / period-end stockholders' equity x 100, latest annual period (not average equity)",
    "debt_to_equity": "total_debt / stockholders_equity, latest annual balance sheet",
    "price_change_1y_pct": "(last weekly close - first weekly close) / first weekly close x 100 over the past year, using traded closes adjusted for splits only; excludes dividends",
    "total_return_1y_pct": "the same calculation using Yahoo's dividend-and-split-adjusted closes; approximates price change plus dividends reinvested",
    "dividends_ttm": "sum of dividends per share with ex-dates in the last 365 days",
    "dividend_yield_pct": "dividends_ttm / current_price x 100",
}

COMPARE_PEERS_FIELDS = [
    "market_cap",
    "revenue",
    "net_income",
    "net_margin_pct",
    "roe_pct",
    "price_change_1y_pct",
    "total_return_1y_pct",
]

COMPANY_PROFILE_FIELDS = [
    "current_price",
    "market_cap",
    "revenue",
    "net_income",
    "net_margin_pct",
    "roe_pct",
    "total_debt",
    "stockholders_equity",
    "debt_to_equity",
    "free_cash_flow",
    "price_change_1y_pct",
    "total_return_1y_pct",
]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if isnan(value) else value


def _ratio(numerator: Any, denominator: Any, scale: float = 100.0, digits: int = 2) -> float | None:
    numerator, denominator = _number(numerator), _number(denominator)
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator * scale, digits)


def _company_metrics(symbol: str, sections: set[str]) -> dict:
    """Run the single-purpose tools for one ticker and compute metrics in code."""
    metrics: dict[str, Any] = {}
    line_items: dict[str, str] = {}
    periods: dict[str, str] = {}
    reasons: dict[str, str] = {}
    components = []

    quote = _get_stock_quote_yahoo(symbol)
    components.append({"tool": "get_stock_quote", "status": quote.get("coverage_audit", {}).get("status")})
    metrics["current_price"] = _number(quote.get("current_price"))
    metrics["market_cap"] = _number(quote.get("market_cap"))
    if quote.get("error"):
        reasons["current_price"] = reasons["market_cap"] = quote["error"]

    latest_rows = {}
    for statement in [name for name in ("income", "balance", "cashflow") if name in sections]:
        result = get_financial_statements(symbol, statement, "annual")
        components.append({
            "tool": "get_financial_statements",
            "statement": statement,
            "status": result.get("coverage_audit", {}).get("status"),
        })
        rows = result.get("data") or []
        if rows:
            latest_rows[statement] = rows[0]
            periods[statement] = rows[0].get("period_ending")
        else:
            latest_rows[statement] = None
            reasons[statement] = result.get("error") or f"No annual {statement} statement periods were returned."

    for field, (statement, names) in _LINE_ITEMS.items():
        if statement not in sections:
            continue
        row = latest_rows.get(statement)
        if row is None:
            metrics[field] = None
            reasons[field] = reasons[statement]
            continue
        metrics[field] = None
        for name in names:
            value = _number(row.get(name))
            if value is not None:
                metrics[field], line_items[field] = value, name
                break
        if metrics[field] is None:
            reasons[field] = (
                f"None of {names} is reported for the latest annual period "
                f"({periods.get(statement)}); earlier periods are not substituted."
            )

    if "income" in sections:
        metrics["net_margin_pct"] = _ratio(metrics.get("net_income"), metrics.get("revenue"))
        if metrics["net_margin_pct"] is None:
            reasons["net_margin_pct"] = "Requires both net_income and a non-zero revenue."
    if "income" in sections and "balance" in sections:
        if periods.get("income") and periods.get("income") != periods.get("balance"):
            metrics["roe_pct"] = None
            reasons["roe_pct"] = (
                f"Income statement ({periods.get('income')}) and balance sheet "
                f"({periods.get('balance')}) periods differ."
            )
        else:
            metrics["roe_pct"] = _ratio(metrics.get("net_income_common"), metrics.get("stockholders_equity"))
            if metrics["roe_pct"] is None:
                reasons.setdefault("roe_pct", "Requires net income and non-zero stockholders' equity.")
        metrics["debt_to_equity"] = _ratio(metrics.get("total_debt"), metrics.get("stockholders_equity"), 1.0)
        if metrics["debt_to_equity"] is None:
            reasons.setdefault("debt_to_equity", "Requires total_debt and non-zero stockholders' equity.")
    metrics.pop("net_income_common", None)
    line_items.pop("net_income_common", None)

    history = get_historical_prices(symbol, "1y", "1wk")
    components.append({"tool": "get_historical_prices", "status": history.get("coverage_audit", {}).get("status")})
    rows = history.get("prices") or []
    price_window = None
    for field, column in (("price_change_1y_pct", "close"), ("total_return_1y_pct", "adj_close")):
        points = [row for row in rows if _number(row.get(column)) is not None]
        if len(points) >= 2:
            first, last = points[0], points[-1]
            metrics[field] = _ratio(last[column] - first[column], first[column])
            if price_window is None:
                price_window = {"start_date": first["date"], "end_date": last["date"]}
            price_window[f"start_{column}"] = first[column]
            price_window[f"end_{column}"] = last[column]
        else:
            metrics[field] = None
            reasons[field] = history.get("error") or f"Fewer than two weekly {column} values were returned."

    if "dividends" in sections:
        actions = get_corporate_actions(symbol, "dividends")
        components.append({"tool": "get_corporate_actions", "status": actions.get("coverage_audit", {}).get("status")})
        dividends = actions.get("dividends") or []
        if dividends:
            cutoff = date.today().toordinal() - 365
            recent = [
                _number(item.get("amount")) or 0.0
                for item in dividends
                if date.fromisoformat(item["date"]).toordinal() >= cutoff
            ]
            metrics["dividends_ttm"] = round(sum(recent), 6)
            metrics["dividend_yield_pct"] = _ratio(metrics["dividends_ttm"], metrics.get("current_price"))
        else:
            metrics["dividends_ttm"] = metrics["dividend_yield_pct"] = None
            reasons["dividends_ttm"] = reasons["dividend_yield_pct"] = (
                actions.get("error")
                or "No dividend records were returned; this may mean none were paid or that data is unavailable."
            )

    return {
        "ticker": symbol,
        "metrics": metrics,
        "periods": periods,
        "line_items_used": line_items,
        "price_window": price_window,
        "reasons": reasons,
        "components": components,
    }


def _composite_audit(entries: list[dict], required: list[str], requested: dict) -> tuple[dict, dict | None]:
    """Audit only the reported fields; return (coverage_audit, fallback_recommendation)."""
    gaps = [
        {
            "ticker": entry["ticker"],
            "field": field,
            "reason": entry["reasons"].get(field, "Not reported by the primary source."),
        }
        for entry in entries
        for field in required
        if entry["metrics"].get(field) is None
    ]
    expected = len(entries) * len(required)
    if not gaps:
        status = "complete"
    elif len(gaps) == expected:
        status = "unavailable"
    else:
        status = "partial"
    audit = {
        "status": status,
        "scope": (
            "Only the fields listed in requested.fields, for each company's latest "
            "annual period, are checked. Blank line items in older periods or "
            "unused fields do not affect this status."
        ),
        "requested": requested,
        "returned": {
            "tickers": [entry["ticker"] for entry in entries],
            "fields_returned": expected - len(gaps),
            "fields_requested": expected,
        },
        "missing": {"gaps": gaps} if gaps else {},
        "computed_by_server": True,
        "component_audits": [
            {"ticker": entry["ticker"], **component} for entry in entries for component in entry["components"]
        ],
        "primary_source": {
            "provider": "Yahoo Finance",
            "retrieval_method": "Finance Data and Lit MCP",
            "status": "primary_workflow_source",
            "retrieval_date": date.today().isoformat(),
        },
    }
    if not gaps:
        return audit, None
    data_types = {_METRIC_DATA_TYPE.get(gap["field"], "financial_statements") for gap in gaps}
    data_type = next(
        (name for name in ("financial_statements", "prices", "corporate_actions") if name in data_types),
        "financial_statements",
    )
    gap_tickers = list(dict.fromkeys(gap["ticker"] for gap in gaps))
    return audit, _fallback_recommendation(data_type, gap_tickers)


def _validation_response(message: str, requested: dict) -> dict:
    return {
        "error": message,
        "error_type": "validation",
        "coverage_audit": {
            "status": "unavailable",
            "requested": requested,
            "returned": {"row_count": 0},
            "missing": {"reason": message},
            "primary_source": {
                "provider": "Yahoo Finance",
                "retrieval_method": "Finance Data and Lit MCP",
                "status": "primary_workflow_source",
                "retrieval_date": date.today().isoformat(),
            },
        },
    }


def _parse_tickers(tickers: Any) -> list[str]:
    raw = tickers.replace(";", ",").split(",") if isinstance(tickers, str) else (tickers or [])
    symbols = [_normalize_text(item).upper() for item in raw if _normalize_text(item)]
    return list(dict.fromkeys(symbols))


def _rank(entries: list[dict], field: str) -> list[str]:
    ranked = [entry for entry in entries if entry["metrics"].get(field) is not None]
    ranked.sort(key=lambda entry: entry["metrics"][field], reverse=True)
    return [entry["ticker"] for entry in ranked]


@mcp.tool(
    title="Compare Peers",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def compare_peers(tickers: list[str] = None) -> dict:
    """Compare 2-10 companies with metrics computed by the server.

    Returns market cap, latest annual revenue and net income, net margin,
    return on equity, one-year price change (traded prices, excluding
    dividends) and one-year total return (dividend-adjusted) for each ticker,
    plus rankings.
    Report the figures as returned rather than recalculating them. The
    coverage audit only checks the fields reported here, for each company's
    latest annual period.
    """
    symbols = _parse_tickers(tickers)
    requested = {"tickers": symbols, "fields": COMPARE_PEERS_FIELDS, "data_type": "peer_comparison"}
    if not 2 <= len(symbols) <= 10:
        return _validation_response("Please provide 2 to 10 ticker symbols.", requested)

    with ThreadPoolExecutor(max_workers=len(symbols)) as executor:
        futures = [
            executor.submit(_company_metrics, symbol, {"income", "balance"}) for symbol in symbols
        ]
        entries = [future.result() for future in futures]

    audit, fallback = _composite_audit(entries, COMPARE_PEERS_FIELDS, requested)
    issuer_markets = {market_for(symbol) for symbol in symbols} - {"NON_EQUITY"}
    response = {
        "tickers": symbols,
        "companies": [
            {
                "ticker": entry["ticker"],
                **{field: entry["metrics"].get(field) for field in COMPARE_PEERS_FIELDS},
                "fiscal_period_end": entry["periods"].get("income"),
                "line_items_used": entry["line_items_used"],
                "price_window": entry["price_window"],
            }
            for entry in entries
        ],
        "rankings": {field: _rank(entries, field) for field in ("market_cap", "net_margin_pct", "roe_pct", "price_change_1y_pct", "total_return_1y_pct")},
        "definitions": {key: _METRIC_DEFINITIONS[key] for key in ("net_margin_pct", "roe_pct", "price_change_1y_pct", "total_return_1y_pct")},
        "notes": [
            "Figures are computed by the server; report them as returned.",
            "Statement figures use each company's latest annual period; check fiscal_period_end before comparing companies with different year ends.",
        ],
        "coverage_audit": audit,
    }
    if len(issuer_markets) > 1:
        response["notes"].append(
            "These companies are listed in different markets; market cap, revenue and net income are in each company's own currency and are not directly comparable. Percentages are comparable."
        )
    if fallback:
        response["fallback_recommendation"] = fallback
    return response


@mcp.tool(
    title="Get Company Profile",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def get_company_profile(ticker: str = "") -> dict:
    """Return a one-company profile with metrics computed by the server.

    Includes price, market cap, latest annual revenue, net income, net margin,
    ROE, total debt, equity, debt-to-equity, free cash flow, one-year price
    change and total return, and trailing-twelve-month dividends and yield. Report the figures
    as returned rather than recalculating them.
    """
    symbol = _normalize_text(ticker).upper()
    requested = {"tickers": [symbol] if symbol else [], "fields": COMPANY_PROFILE_FIELDS, "data_type": "company_profile"}
    if not symbol:
        return _validation_response("Please provide a ticker symbol.", requested)

    entry = _company_metrics(symbol, {"income", "balance", "cashflow", "dividends"})
    audit, fallback = _composite_audit([entry], COMPANY_PROFILE_FIELDS, requested)
    extra_gaps = [
        {"ticker": symbol, "field": field, "reason": entry["reasons"][field]}
        for field in ("dividends_ttm", "dividend_yield_pct")
        if entry["metrics"].get(field) is None and field in entry["reasons"]
    ]
    response = {
        "ticker": symbol,
        **entry["metrics"],
        "periods": entry["periods"],
        "line_items_used": entry["line_items_used"],
        "price_window": entry["price_window"],
        "definitions": _METRIC_DEFINITIONS,
        "notes": [
            "Figures are computed by the server; report them as returned.",
            "Statement figures are in the issuer's reporting currency, which can differ from the trading currency of the price and dividends.",
        ],
        "coverage_audit": audit,
    }
    if extra_gaps:
        response["optional_fields_unavailable"] = extra_gaps
    if fallback:
        response["fallback_recommendation"] = fallback
    return response


# ---------------------------------------------------------------------------
# Step 3: model-in-the-loop via MCP sampling. The server never calls a paid AI
# API. When the connected client supports sampling, the client's own model
# writes the synthesis (and the client may ask the user to approve that
# request); otherwise the tool returns the sources for the agent to summarise.

SYNTHESIS_MAX_TOKENS = 1200

SYNTHESIS_SYSTEM_PROMPT = """\
You summarise finance research for a researcher. Follow these rules exactly:
1. Use only the numbered sources provided. Do not add papers, data or facts
   from memory.
2. Cite every claim with its source ID in square brackets, e.g. [P1] or
   [P2, R1]. Only use IDs that appear in the source list.
3. IDs starting with R are arXiv preprints that are not peer reviewed. Say
   "preprint" whenever you rely on one.
4. Abstracts are snippets; do not overstate findings beyond what they say.
5. Do not give investment advice.
6. If the sources are too few or off-topic to answer, say so plainly."""


def _client_supports_sampling(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        capability = mcp_types.ClientCapabilities(sampling=mcp_types.SamplingCapability())
        return ctx.session.check_client_capability(capability)
    except (AttributeError, RuntimeError):
        return False


def _synthesis_sources(published: dict, preprints: dict | None) -> list[dict]:
    sources = []
    for index, work in enumerate(published.get("results") or [], start=1):
        sources.append({
            "id": f"P{index}",
            "type": "published",
            "title": work.get("title"),
            "authors": (work.get("authors") or [])[:5],
            "year": work.get("year"),
            "venue": work.get("journal"),
            "doi": work.get("doi"),
            "cited_by_count": work.get("cited_by_count"),
            "abstract_snippet": work.get("abstract_snippet"),
        })
    for index, paper in enumerate((preprints or {}).get("results") or [], start=1):
        sources.append({
            "id": f"R{index}",
            "type": "preprint",
            "review_status": "preprint - not peer reviewed",
            "title": paper.get("title"),
            "authors": (paper.get("authors") or [])[:5],
            "year": (paper.get("submission_date") or "")[:4] or None,
            "arxiv_id": paper.get("arxiv_id"),
            "abstract_snippet": paper.get("abstract_snippet"),
        })
    return sources


def _synthesis_request(topic: str, sources: list[dict]) -> str:
    lines = [f"Research topic: {topic}", "", "Sources:"]
    for source in sources:
        authors = ", ".join(source["authors"]) or "Unknown authors"
        label = "PREPRINT, not peer reviewed" if source["type"] == "preprint" else source.get("venue") or "Published"
        lines.append(f"[{source['id']}] {source['title']} - {authors} ({source.get('year') or 'n.d.'}; {label})")
        lines.append(f"    Abstract snippet: {source.get('abstract_snippet') or 'No abstract available.'}")
    lines += [
        "",
        "Write at most 400 words with these headings: Key themes; Main findings; "
        "Points of disagreement; Gaps for further research. Cite source IDs for every claim.",
    ]
    return "\n".join(lines)


def _sampled_text(result: Any) -> str:
    content = getattr(result, "content", None)
    parts = content if isinstance(content, list) else [content]
    return "\n".join(part.text for part in parts if getattr(part, "type", None) == "text").strip()


def _check_citations(text: str, sources: list[dict]) -> dict:
    """Verify the model only cited source IDs the server supplied."""
    valid = {source["id"] for source in sources}
    cited = []
    for group in re.findall(r"\[([^\[\]]+)\]", text):
        for token in re.split(r"[,;\s]+", group):
            if re.fullmatch(r"[PR]\d+", token):
                cited.append(token)
    cited_set = set(cited)
    warnings = []
    unknown = sorted(cited_set - valid)
    if unknown:
        warnings.append(f"Cites IDs that were not supplied: {', '.join(unknown)}.")
    if not cited_set:
        warnings.append("The synthesis contains no source citations.")
    if any(token.startswith("R") for token in cited_set) and "preprint" not in text.lower():
        warnings.append("Cites preprints without saying they are preprints.")
    return {
        "passed": not warnings,
        "cited_ids": sorted(cited_set & valid, key=lambda value: (value[0], int(value[1:]))),
        "uncited_ids": sorted(valid - cited_set, key=lambda value: (value[0], int(value[1:]))),
        "unknown_ids": unknown,
        "warnings": warnings,
    }


@mcp.tool(
    title="Synthesize Research",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def synthesize_research(
    topic: str = "",
    year_from: int = 0,
    include_preprints: bool = True,
    limit: int = 8,
    ctx: Context | None = None,
) -> dict:
    """Find finance research on a topic and return a cited synthesis.

    Searches OpenAlex (published) and optionally arXiv (preprints), then asks
    the connected AI app's own model to write a short synthesis citing source
    IDs such as [P1] and [R1], and checks those citations. No paid AI API is
    used. If the app does not support MCP sampling, the sources are returned
    with instructions for you to write the synthesis yourself.
    """
    subject = _normalize_text(topic)
    requested = {"topic": subject, "data_type": "research_synthesis"}
    if not subject:
        return _validation_response("Please provide a research topic.", requested)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 15:
        return _validation_response("Invalid limit. Provide an integer from 1 through 15.", requested)
    year = year_from or None

    published = search_finance_research(subject, year_from=year, limit=limit)
    preprints = search_finance_preprints(subject, limit=max(1, limit // 2)) if include_preprints else None
    sources = _synthesis_sources(published, preprints)

    components = [{"tool": "search_finance_research", "status": published.get("coverage_audit", {}).get("status")}]
    errors = [published["error"]] if published.get("error") else []
    if preprints is not None:
        components.append({"tool": "search_finance_preprints", "status": preprints.get("coverage_audit", {}).get("status")})
        if preprints.get("error"):
            errors.append(preprints["error"])
    status = "unavailable" if not sources else ("partial" if errors else "complete")
    audit = {
        "status": status,
        "requested": requested,
        "returned": {"row_count": len(sources)},
        "missing": {"reasons": errors} if errors else {},
        "component_audits": components,
        "primary_source": {
            "provider": "OpenAlex" + (" and arXiv" if include_preprints else ""),
            "retrieval_method": "Finance Data and Lit MCP",
            "status": "primary_workflow_source",
            "retrieval_date": date.today().isoformat(),
        },
    }
    response: dict[str, Any] = {"topic": subject, "sources": sources, "coverage_audit": audit}
    if status != "complete":
        response["fallback_recommendation"] = _fallback_recommendation("literature")
    if not sources:
        response["synthesis"] = None
        response["synthesis_status"] = "no_sources"
        return response

    agent_instructions = (
        "Write the synthesis yourself from these sources only, following these rules:\n"
        + SYNTHESIS_SYSTEM_PROMPT
        + "\nUse the headings: Key themes; Main findings; Points of disagreement; Gaps for further research."
    )
    if not _client_supports_sampling(ctx):
        response.update({
            "synthesis": None,
            "synthesis_status": "client_does_not_support_sampling",
            "instructions_for_agent": agent_instructions,
        })
        return response

    try:
        result = await ctx.session.create_message(
            messages=[
                mcp_types.SamplingMessage(
                    role="user",
                    content=mcp_types.TextContent(type="text", text=_synthesis_request(subject, sources)),
                )
            ],
            max_tokens=SYNTHESIS_MAX_TOKENS,
            system_prompt=SYNTHESIS_SYSTEM_PROMPT,
            include_context="none",
            temperature=0.2,
        )
        text = _sampled_text(result)
    except Exception as exc:  # declined by the user, or the client failed
        response.update({
            "synthesis": None,
            "synthesis_status": "sampling_declined_or_failed",
            "sampling_error": str(exc),
            "instructions_for_agent": agent_instructions,
        })
        return response

    if not text:
        response.update({
            "synthesis": None,
            "synthesis_status": "sampling_returned_no_text",
            "instructions_for_agent": agent_instructions,
        })
        return response

    response.update({
        "synthesis": text,
        "synthesis_status": "generated",
        "synthesis_by": {
            "method": "MCP sampling: written by the connected app's own model; no paid API was called by this server",
            "model": getattr(result, "model", None),
        },
        "citation_check": _check_citations(text, sources),
    })
    return response


@mcp.tool(
    title="Get Research Workflow",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@_coverage_tool("workflow", "Finance Data and Lit MCP")
def get_research_workflow(
    workflow: str = "",
    ticker: str = "",
    tickers: str = "",
    topic: str = "",
    focus: str = "",
    year_from: int = 0,
    include_preprints: bool = True,
    company_or_ticker: str = "",
) -> dict:
    """Return step-by-step instructions for a multi-tool research workflow.

    Call this first when the user asks for a company research brief, a finance
    literature review, a peer comparison, or a company's lifecycle (funding
    history through to public-market performance), then follow the returned
    instructions exactly, including the coverage rules. Workflows:
    ``company_research_brief`` (ticker, optional focus),
    ``finance_literature_review`` (topic, optional year_from and
    include_preprints), ``peer_comparison`` (tickers, comma-separated) and
    ``company_lifecycle`` (company_or_ticker).
    This tool fetches no market data itself.
    """
    name = _normalize_text(workflow).lower()
    if not name:
        return {
            "error": "Please provide a workflow name.",
            "available_workflows": WORKFLOWS,
        }
    try:
        instructions = render_workflow(
            name,
            ticker=ticker,
            tickers=tickers,
            topic=topic,
            focus=focus,
            year_from=year_from,
            include_preprints=include_preprints,
            company_or_ticker=company_or_ticker,
            crunchbase_enabled=CRUNCHBASE_ENABLED,
        )
    except (TypeError, ValueError) as exc:
        message = str(exc)
        if not message.startswith(("Please ", "Invalid ")):
            message = f"Invalid arguments: {message}"
        return {"error": message, "available_workflows": WORKFLOWS}
    return {"workflow": name, "instructions": instructions}


register_prompts(mcp, crunchbase_enabled=CRUNCHBASE_ENABLED)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
