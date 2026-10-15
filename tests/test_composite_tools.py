import asyncio
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import server

TODAY = date.today()

# UOB (U11.SI) FY2025 figures as reported by Yahoo Finance, including the
# mostly blank older column that used to trigger false "partial" statuses.
STATEMENTS = {
    "U11.SI": {
        "income": [
            {"period_ending": "2025-12-31", "Total Revenue": 13807e6, "Net Income": 4682e6,
             "Net Income Common Stockholders": 4589e6},
            {"period_ending": "2021-12-31", "Total Revenue": None, "Net Income": None},
        ],
        "balance": [
            {"period_ending": "2025-12-31", "Total Debt": 45263e6, "Stockholders Equity": 51248e6},
            {"period_ending": "2021-12-31", "Total Debt": None, "Stockholders Equity": None},
        ],
        "cashflow": [{"period_ending": "2025-12-31", "Free Cash Flow": 46e6}],
    },
    "D05.SI": {
        "income": [{"period_ending": "2025-12-31", "Total Revenue": 22895e6, "Net Income": 10934e6}],
        "balance": [{"period_ending": "2025-12-31", "Total Debt": 1e9, "Common Stock Equity": 60000e6}],
        "cashflow": [{"period_ending": "2025-12-31", "Free Cash Flow": 1e9}],
    },
    # Latest period lacks revenue; an older period has it but must not be used.
    "GAP.SI": {
        "income": [
            {"period_ending": "2025-12-31", "Total Revenue": None, "Net Income": 100.0},
            {"period_ending": "2024-12-31", "Total Revenue": 900.0, "Net Income": 90.0},
        ],
        "balance": [{"period_ending": "2025-12-31", "Total Debt": 10.0, "Stockholders Equity": 1000.0}],
        "cashflow": [{"period_ending": "2025-12-31", "Free Cash Flow": 5.0}],
    },
    # Balance sheet year differs from the income statement year.
    "MISMATCH": {
        "income": [{"period_ending": "2025-06-30", "Total Revenue": 100.0, "Net Income": 10.0}],
        "balance": [{"period_ending": "2024-06-30", "Total Debt": 5.0, "Stockholders Equity": 50.0}],
        "cashflow": [{"period_ending": "2025-06-30", "Free Cash Flow": 1.0}],
    },
}

QUOTES = {
    "U11.SI": {"current_price": 41.63, "market_cap": 68.65e9},
    "D05.SI": {"current_price": 73.65, "market_cap": 209.46e9},
    "GAP.SI": {"current_price": 10.0, "market_cap": 1e9},
    "MISMATCH": {"current_price": 20.0, "market_cap": 2e9},
}

# (traded start, traded end, adjusted start, adjusted end). Adjusted start
# prices are lower because later dividends are deducted from them.
PRICES = {
    "U11.SI": (34.10, 41.63, 32.6246, 41.63),
    "D05.SI": (54.00, 73.65, 52.1405, 73.65),
    "GAP.SI": (10.0, 10.0, 10.0, 10.0),
    "MISMATCH": (10.0, 20.0, 10.0, 20.0),
}


def fake_quote(symbol):
    if symbol not in QUOTES:
        return {"error": f"Invalid ticker '{symbol}': no market data was found.",
                "coverage_audit": {"status": "unavailable"}}
    return {"ticker": symbol, **QUOTES[symbol], "coverage_audit": {"status": "complete"}}


def fake_statements(symbol, statement, period):
    rows = STATEMENTS.get(symbol, {}).get(statement)
    if rows is None:
        return {"error": f"Invalid ticker '{symbol}': no market data was found.",
                "coverage_audit": {"status": "unavailable"}}
    status = "partial" if any(v is None for row in rows for v in row.values()) else "complete"
    return {"ticker": symbol, "data": rows, "coverage_audit": {"status": status}}


def fake_history(symbol, period, interval):
    if symbol not in PRICES:
        return {"error": f"Invalid ticker '{symbol}': no market data was found.",
                "coverage_audit": {"status": "unavailable"}}
    start, end, adj_start, adj_end = PRICES[symbol]
    return {
        "ticker": symbol,
        "prices": [
            {"date": (TODAY - timedelta(days=364)).isoformat(), "close": start, "adj_close": adj_start},
            {"date": (TODAY - timedelta(days=180)).isoformat(), "close": None, "adj_close": None},
            {"date": TODAY.isoformat(), "close": end, "adj_close": end},
        ],
        "coverage_audit": {"status": "complete"},
    }


def fake_actions(symbol, action_type):
    if symbol == "U11.SI":
        dividends = [
            {"date": (TODAY - timedelta(days=30)).isoformat(), "amount": 0.85},
            {"date": (TODAY - timedelta(days=200)).isoformat(), "amount": 0.85},
            {"date": (TODAY - timedelta(days=400)).isoformat(), "amount": 0.80},
        ]
    else:
        dividends = []
    return {"ticker": symbol, "dividends": dividends, "splits": None,
            "coverage_audit": {"status": "complete" if dividends else "unavailable"}}


class CompositeToolTestCase(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(server, "_get_stock_quote_yahoo", side_effect=fake_quote),
            patch.object(server, "get_financial_statements", side_effect=fake_statements),
            patch.object(server, "get_historical_prices", side_effect=fake_history),
            patch.object(server, "get_corporate_actions", side_effect=fake_actions),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)


class ComparePeersTests(CompositeToolTestCase):
    def test_computes_ratios_in_code(self):
        result = server.compare_peers(["D05.SI", "U11.SI"])
        uob = next(c for c in result["companies"] if c["ticker"] == "U11.SI")
        self.assertEqual(uob["net_margin_pct"], 33.91)
        self.assertEqual(uob["roe_pct"], 8.95)  # 4,589m / 51,248m
        self.assertEqual(uob["price_change_1y_pct"], 22.08)  # traded prices only
        self.assertEqual(uob["total_return_1y_pct"], 27.6)  # dividend-adjusted, not 27.61
        self.assertEqual(uob["price_window"]["start_close"], 34.10)
        self.assertEqual(uob["price_window"]["start_adj_close"], 32.6246)
        self.assertEqual(uob["fiscal_period_end"], "2025-12-31")
        self.assertEqual(uob["line_items_used"]["revenue"], "Total Revenue")
        dbs = next(c for c in result["companies"] if c["ticker"] == "D05.SI")
        self.assertEqual(dbs["net_margin_pct"], 47.76)
        self.assertEqual(dbs["line_items_used"]["stockholders_equity"], "Common Stock Equity")

    def test_blank_older_periods_do_not_make_it_partial(self):
        result = server.compare_peers(["D05.SI", "U11.SI"])
        audit = result["coverage_audit"]
        self.assertEqual(audit["status"], "complete")
        self.assertTrue(audit["computed_by_server"])
        self.assertNotIn("fallback_recommendation", result)
        # The underlying statement audit was partial, and that stays visible.
        component = next(
            c for c in audit["component_audits"]
            if c["ticker"] == "U11.SI" and c.get("statement") == "income"
        )
        self.assertEqual(component["status"], "partial")

    def test_rankings(self):
        result = server.compare_peers("U11.SI, D05.SI")
        self.assertEqual(result["rankings"]["market_cap"], ["D05.SI", "U11.SI"])
        self.assertEqual(result["rankings"]["price_change_1y_pct"], ["D05.SI", "U11.SI"])

    def test_missing_latest_field_is_a_gap_not_backfilled(self):
        result = server.compare_peers(["D05.SI", "GAP.SI"])
        gap_company = next(c for c in result["companies"] if c["ticker"] == "GAP.SI")
        self.assertIsNone(gap_company["revenue"])
        self.assertIsNone(gap_company["net_margin_pct"])
        audit = result["coverage_audit"]
        self.assertEqual(audit["status"], "partial")
        fields = {(g["ticker"], g["field"]) for g in audit["missing"]["gaps"]}
        self.assertEqual(fields, {("GAP.SI", "revenue"), ("GAP.SI", "net_margin_pct")})
        fallback = result["fallback_recommendation"]
        self.assertTrue(fallback["requires_user_confirmation"])
        self.assertIn("SGXNet", fallback["recommended_source"])
        self.assertNotIn("GAP.SI", result["rankings"]["net_margin_pct"])

    def test_invalid_ticker_is_unavailable_not_zero(self):
        result = server.compare_peers(["D05.SI", "NOPE"])
        nope = next(c for c in result["companies"] if c["ticker"] == "NOPE")
        self.assertTrue(all(nope[field] is None for field in server.COMPARE_PEERS_FIELDS))
        self.assertEqual(result["coverage_audit"]["status"], "partial")

    def test_mismatched_periods_block_roe(self):
        result = server.compare_peers(["D05.SI", "MISMATCH"])
        company = next(c for c in result["companies"] if c["ticker"] == "MISMATCH")
        self.assertIsNone(company["roe_pct"])
        reasons = {g["field"]: g["reason"] for g in result["coverage_audit"]["missing"]["gaps"]}
        self.assertIn("periods differ", reasons["roe_pct"])

    def test_mixed_markets_add_currency_note(self):
        result = server.compare_peers(["D05.SI", "MISMATCH"])
        self.assertTrue(any("different markets" in note for note in result["notes"]))
        same = server.compare_peers(["D05.SI", "U11.SI"])
        self.assertFalse(any("different markets" in note for note in same["notes"]))

    def test_validation(self):
        for bad in [None, [], ["D05.SI"], ",".join(f"T{i}" for i in range(11)), ["D05.SI", "d05.si"]]:
            with self.subTest(tickers=bad):
                result = server.compare_peers(bad)
                self.assertEqual(result["error_type"], "validation")
                self.assertEqual(result["coverage_audit"]["status"], "unavailable")


class CompanyProfileTests(CompositeToolTestCase):
    def test_profile_metrics(self):
        result = server.get_company_profile(" u11.si ")
        self.assertEqual(result["ticker"], "U11.SI")
        self.assertEqual(result["debt_to_equity"], 0.88)
        self.assertEqual(result["free_cash_flow"], 46e6)
        self.assertEqual(result["dividends_ttm"], 1.7)  # the 400-day-old dividend is excluded
        self.assertEqual(result["dividend_yield_pct"], 4.08)
        self.assertEqual(result["coverage_audit"]["status"], "complete")
        self.assertNotIn("optional_fields_unavailable", result)

    def test_no_dividend_records_is_reported_not_zero(self):
        result = server.get_company_profile("D05.SI")
        self.assertIsNone(result["dividends_ttm"])
        self.assertEqual(
            {item["field"] for item in result["optional_fields_unavailable"]},
            {"dividends_ttm", "dividend_yield_pct"},
        )
        self.assertEqual(result["coverage_audit"]["status"], "complete")

    def test_validation(self):
        result = server.get_company_profile("  ")
        self.assertEqual(result["error_type"], "validation")


class PriceBasisTests(unittest.TestCase):
    def test_historical_prices_report_traded_and_adjusted_closes(self):
        import pandas as pd

        frame = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5],
             "Adj Close": [10.1], "Volume": [100]},
            index=pd.to_datetime(["2026-10-14"]),
        )
        with patch.object(server.yf, "Ticker") as ticker:
            ticker.return_value.history.return_value = frame
            result = server.get_historical_prices("D05.SI", "5d", "1d")
        ticker.return_value.history.assert_called_with(period="5d", interval="1d", auto_adjust=False)
        self.assertEqual(result["prices"][0]["close"], 10.5)
        self.assertEqual(result["prices"][0]["adj_close"], 10.1)
        self.assertIn("splits only", result["price_basis"])

    def test_definitions_no_longer_mislabel_adjusted_prices(self):
        self.assertIn("excludes dividends", server._METRIC_DEFINITIONS["price_change_1y_pct"])
        self.assertIn("traded closes", server._METRIC_DEFINITIONS["price_change_1y_pct"])
        self.assertIn("dividend", server._METRIC_DEFINITIONS["total_return_1y_pct"])


class RegistrationTests(unittest.TestCase):
    def test_tools_registered(self):
        names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        self.assertTrue({"compare_peers", "get_company_profile"} <= names)


if __name__ == "__main__":
    unittest.main()
