import unittest

import markets
import server


def _partial_statements(**kwargs):
    @server._coverage_tool("financial_statements", "Yahoo Finance", "data", 8)
    def retrieve(ticker="", statement="income", period="annual"):
        return {"ticker": ticker, "data": [{"period_ending": "2025-12-31", "Total Revenue": None}]}

    return retrieve(**kwargs)


class MarketDetectionTests(unittest.TestCase):
    def test_market_for(self):
        cases = {
            "AAPL": "US",
            "BRK.B": "US",
            "D05.SI": "SGX",
            "d05.si": "SGX",
            "0700.HK": "HKEX",
            "VOD.L": "LSE",
            "7203.T": "JPX",
            "BHP.AX": "ASX",
            "RY.TO": "TSX",
            "RELIANCE.NS": "INDIA",
            "005930.KS": "KRX",
            "SAP.DE": "OTHER",
            "^STI": "NON_EQUITY",
            "SGD=X": "NON_EQUITY",
        }
        for ticker, expected in cases.items():
            with self.subTest(ticker=ticker):
                self.assertEqual(markets.market_for(ticker), expected)


class MarketFallbackTests(unittest.TestCase):
    def test_us_ticker_keeps_sec_recommendation(self):
        result = _partial_statements(ticker="AAPL")
        fallback = result["fallback_recommendation"]
        self.assertEqual(fallback["recommended_source"], "SEC EDGAR Companyfacts and issuer filings")
        self.assertNotIn("market", fallback)

    def test_singapore_bank_points_to_sgx_not_sec(self):
        result = _partial_statements(ticker="O39.SI")
        fallback = result["fallback_recommendation"]
        self.assertEqual(fallback["market"], "SGX")
        self.assertIn("SGXNet", fallback["recommended_source"])
        self.assertIn("SGXNet", fallback["suggested_user_prompt"])
        self.assertNotIn("SEC", fallback["suggested_user_prompt"])
        self.assertTrue(fallback["requires_user_confirmation"])

    def test_ownership_uses_market_disclosure_regime(self):
        @server._coverage_tool("ownership", "Yahoo Finance", "data", 20)
        def retrieve(ticker="", holder_type="major"):
            return {"ticker": ticker, "holder_type": holder_type, "data": []}

        fallback = retrieve(ticker="0700.HK")["fallback_recommendation"]
        self.assertIn("HKEXnews", fallback["recommended_source"])
        self.assertNotIn("suggested_filings", fallback)
        sources = [item["source"] for item in fallback["suggested_sources"]]
        self.assertIn("HKEX Disclosure of Interests", sources)

    def test_mixed_markets_are_split(self):
        fallback = markets.market_fallback(
            "financial_statements",
            server.FALLBACK_SOURCES["financial_statements"],
            ["AAPL", "D05.SI", "U11.SI", "^STI"],
        )
        self.assertEqual(fallback["recommended_source"], "Each issuer's home-market regulatory filings")
        self.assertEqual(set(fallback["by_market"]), {"US", "SGX"})
        self.assertEqual(fallback["by_market"]["SGX"]["tickers"], ["D05.SI", "U11.SI"])
        self.assertIn("SEC", fallback["by_market"]["US"]["recommended_source"])

    def test_unknown_suffix_gets_generic_home_exchange(self):
        fallback = markets.market_fallback(
            "corporate_actions", server.FALLBACK_SOURCES["corporate_actions"], ["SAP.DE"]
        )
        self.assertEqual(fallback["market"], "OTHER")
        self.assertIn("home-exchange", fallback["recommended_source"])

    def test_literature_and_defaults_are_untouched(self):
        literature = server.FALLBACK_SOURCES["literature"]
        self.assertIs(markets.market_fallback("literature", literature, ["D05.SI"]), literature)
        self.assertIsNone(markets.market_fallback("financial_statements", None, ["D05.SI"]))

    def test_default_table_is_not_mutated(self):
        before = dict(server.FALLBACK_SOURCES["ownership"])
        _ = markets.market_fallback("ownership", server.FALLBACK_SOURCES["ownership"], ["D05.SI"])
        self.assertEqual(server.FALLBACK_SOURCES["ownership"], before)


if __name__ == "__main__":
    unittest.main()
