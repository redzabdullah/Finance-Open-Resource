"""Market-aware supplementary sources for the coverage contract.

Yahoo Finance tickers carry an exchange suffix (D05.SI, 0700.HK, VOD.L).
SEC EDGAR only covers US filers, so fallback recommendations for issuers
listed elsewhere should point to their home-market disclosure systems.
Tickers without a suffix are treated as US listings and keep the default
SEC-based recommendations in ``server.FALLBACK_SOURCES``.
"""

from copy import deepcopy

# Data types whose authoritative source depends on the listing market.
MARKET_DATA_TYPES = {"financial_statements", "ownership", "corporate_actions", "prices"}

MARKETS = {
    "SGX": {
        "suffixes": ["SI"],
        "name": "Singapore Exchange (SGX)",
        "filings": "SGXNet company announcements",
        "ownership": [
            {"source": "Annual report", "purpose": "Statistics of shareholdings, top 20 shareholders and substantial shareholders"},
            {"source": "SGXNet substantial shareholder notices", "purpose": "Changes in substantial shareholdings and director interests"},
        ],
    },
    "HKEX": {
        "suffixes": ["HK"],
        "name": "Hong Kong Exchanges and Clearing (HKEX)",
        "filings": "HKEXnews listed company disclosures",
        "ownership": [
            {"source": "HKEX Disclosure of Interests", "purpose": "Substantial shareholder and director interest notices"},
            {"source": "Annual report", "purpose": "Substantial shareholders' interests as at the reporting date"},
        ],
    },
    "LSE": {
        "suffixes": ["L"],
        "name": "London Stock Exchange (LSE)",
        "filings": "RNS regulatory announcements",
        "ownership": [
            {"source": "RNS holding notifications (TR-1)", "purpose": "Major shareholding notifications"},
            {"source": "Annual report", "purpose": "Significant shareholders as at the reporting date"},
        ],
    },
    "JPX": {
        "suffixes": ["T"],
        "name": "Japan Exchange Group (JPX)",
        "filings": "TDnet timely disclosures and EDINET filings",
        "ownership": [
            {"source": "EDINET large shareholding reports", "purpose": "Holdings above the 5% reporting threshold"},
            {"source": "Annual securities report (EDINET)", "purpose": "Major shareholders as at the reporting date"},
        ],
    },
    "ASX": {
        "suffixes": ["AX"],
        "name": "Australian Securities Exchange (ASX)",
        "filings": "ASX market announcements",
        "ownership": [
            {"source": "ASX substantial holder notices", "purpose": "Holdings and changes at or above 5%"},
            {"source": "Annual report", "purpose": "Top 20 holders and substantial holders"},
        ],
    },
    "TSX": {
        "suffixes": ["TO", "V"],
        "name": "Toronto Stock Exchange (TSX / TSXV)",
        "filings": "SEDAR+ filings",
        "ownership": [
            {"source": "SEDAR+ early warning reports", "purpose": "Holdings at or above the 10% reporting threshold"},
            {"source": "SEDI insider reports", "purpose": "Insider holdings and transactions"},
        ],
    },
    "INDIA": {
        "suffixes": ["NS", "BO"],
        "name": "National Stock Exchange / BSE (India)",
        "filings": "NSE and BSE corporate filings",
        "ownership": [
            {"source": "Quarterly shareholding pattern (NSE/BSE)", "purpose": "Promoter, institutional and public holdings"},
        ],
    },
    "KRX": {
        "suffixes": ["KS", "KQ"],
        "name": "Korea Exchange (KRX)",
        "filings": "DART electronic disclosures",
        "ownership": [
            {"source": "DART large shareholding reports", "purpose": "Holdings above the 5% reporting threshold"},
        ],
    },
}

_SUFFIX_TO_MARKET = {suffix: key for key, market in MARKETS.items() for suffix in market["suffixes"]}


def market_for(ticker: str) -> str:
    """Return a market key: 'US', a key from MARKETS, 'OTHER', or 'NON_EQUITY'."""
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return "US"
    # Indices (^STI), FX (SGD=X) and futures (CL=F) are not issuer securities.
    if symbol.startswith("^") or "=" in symbol:
        return "NON_EQUITY"
    if "." not in symbol:
        return "US"
    suffix = symbol.rsplit(".", 1)[1]
    # US share classes such as BRK.B use a single-letter suffix that is not
    # a Yahoo exchange code; treat unknown one-letter suffixes as US.
    if suffix in _SUFFIX_TO_MARKET:
        return _SUFFIX_TO_MARKET[suffix]
    return "US" if len(suffix) == 1 else "OTHER"


def _market_sources(market_key: str, data_type: str) -> tuple[str, list[dict]]:
    """Recommended source and suggested sources for one non-US market."""
    if market_key == "OTHER":
        name, filings = "the home exchange", "home-exchange regulatory filings"
        ownership = [{"source": "Annual report", "purpose": "Major shareholders as at the reporting date"}]
    else:
        market = MARKETS[market_key]
        name, filings, ownership = market["name"], market["filings"], market["ownership"]

    if data_type == "financial_statements":
        return f"{filings} and issuer annual reports", [
            {"source": filings, "purpose": "Filed financial statements and results announcements"},
            {"source": "Investor relations", "purpose": "Official annual reports and earnings releases"},
        ]
    if data_type == "ownership":
        return f"{filings} and issuer annual reports", ownership
    if data_type == "corporate_actions":
        return f"{filings}", [
            {"source": filings, "purpose": "Dividend, split and other corporate-action announcements"},
            {"source": "Investor relations", "purpose": "Official issuer announcements"},
        ]
    return f"Official {name} market data", [
        {"source": f"{name}", "purpose": "Authoritative trade and closing-price history"},
        {"source": "Reputable market-data vendor", "purpose": "Price history with adjustment methodology stated"},
    ]


def market_fallback(data_type: str, default: dict | None, tickers) -> dict | None:
    """Adapt a default (US) fallback recommendation to the tickers' markets."""
    if not default or data_type not in MARKET_DATA_TYPES:
        return default
    if isinstance(tickers, str):
        tickers = [tickers]
    symbols = [str(t).strip().upper() for t in (tickers or []) if str(t or "").strip()]
    markets = {}
    for symbol in symbols:
        markets.setdefault(market_for(symbol), []).append(symbol)
    issuer_markets = {key: value for key, value in markets.items() if key != "NON_EQUITY"}
    if not issuer_markets or set(issuer_markets) == {"US"}:
        return default

    fallback = deepcopy(default)
    fallback.pop("suggested_filings", None)
    fallback.pop("suggested_sources", None)
    if len(issuer_markets) == 1:
        market_key = next(iter(issuer_markets))
        recommended, suggested = _market_sources(market_key, data_type)
        fallback["recommended_source"] = recommended
        fallback["suggested_sources"] = suggested
        fallback["market"] = market_key
        return fallback

    by_market = {}
    for market_key, symbols_in_market in issuer_markets.items():
        if market_key == "US":
            recommended = default["recommended_source"]
            suggested = default.get("suggested_filings") or default.get("suggested_sources") or []
        else:
            recommended, suggested = _market_sources(market_key, data_type)
        by_market[market_key] = {
            "tickers": symbols_in_market,
            "recommended_source": recommended,
            "suggested_sources": suggested,
        }
    fallback["recommended_source"] = "Each issuer's home-market regulatory filings"
    fallback["by_market"] = by_market
    return fallback
