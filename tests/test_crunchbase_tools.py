"""Crunchbase tests. All payloads are synthetic and fictitious: Crunchbase data
is licensed, so no real responses may be committed to this repository."""

import asyncio
import io
import os
import re
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from mcp.server.fastmcp import FastMCP

import crunchbase_client
import prompts
import server


CB_TOOL_NAMES = set(server.CB_TOOLS)


def _identifier(name, permalink=None):
    return {"value": name, "permalink": permalink or name.lower().replace(" ", "-"), "uuid": f"uuid-{name}"}


def _card(card_id, rows, name="Example Robotics"):
    return {"properties": {"identifier": _identifier(name)}, "cards": {card_id: rows}}


def _mock_response(body: bytes):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


class RegistrationTests(unittest.TestCase):
    def test_tools_not_registered_without_key(self):
        target = FastMCP("test")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(server.register_crunchbase_tools(target))
        listed = {tool.name for tool in asyncio.run(target.list_tools())}
        self.assertFalse(listed & CB_TOOL_NAMES)

    def test_blank_key_counts_as_unset(self):
        target = FastMCP("test")
        with patch.dict(os.environ, {"CRUNCHBASE_API_KEY": "   "}):
            self.assertFalse(server.register_crunchbase_tools(target))

    def test_server_registration_matches_key_at_import(self):
        listed = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        expected = CB_TOOL_NAMES if server.CRUNCHBASE_ENABLED else set()
        self.assertEqual(listed & CB_TOOL_NAMES, expected)

    def test_tools_registered_read_only_open_world_with_key(self):
        target = FastMCP("test")
        with patch.dict(os.environ, {"CRUNCHBASE_API_KEY": "test-key"}):
            self.assertTrue(server.register_crunchbase_tools(target))
        tools = {tool.name: tool for tool in asyncio.run(target.list_tools())}
        self.assertEqual(set(tools), CB_TOOL_NAMES)
        for tool in tools.values():
            with self.subTest(tool=tool.name):
                self.assertIs(tool.annotations.readOnlyHint, True)
                self.assertIs(tool.annotations.destructiveHint, False)
                self.assertIs(tool.annotations.openWorldHint, True)


class LifecycleWorkflowTests(unittest.TestCase):
    _tool_pattern = re.compile(r"`((?:get|search|check|compare|synthesize)_[a-z_]+)`")

    def test_enabled_workflow_references_only_registered_tools(self):
        target = FastMCP("test")
        with patch.dict(os.environ, {"CRUNCHBASE_API_KEY": "test-key"}):
            server.register_crunchbase_tools(target)
        prompts.register_prompts(target, crunchbase_enabled=True)
        available = {tool.name for tool in asyncio.run(target.list_tools())}
        available |= {tool.name for tool in asyncio.run(server.mcp.list_tools())}

        result = asyncio.run(target.get_prompt("company_lifecycle", {"company_or_ticker": "Example Robotics"}))
        text = "\n".join(message.content.text for message in result.messages)
        referenced = set(self._tool_pattern.findall(text))
        self.assertTrue({"search_organization_cb", "get_funding_rounds_cb", "get_acquisitions_cb"} <= referenced)
        self.assertTrue({"get_company_profile", "get_historical_prices"} <= referenced)
        self.assertLessEqual(referenced, available)
        for heading in ("Private-market history (Crunchbase)", "Public-market performance (Yahoo Finance)", "Data gaps"):
            self.assertIn(heading, text)
        self.assertIn("not disclosed", text)

    def test_disabled_workflow_says_module_not_enabled(self):
        text = prompts.company_lifecycle("AAPL", crunchbase_enabled=False)
        self.assertIn("Crunchbase module is not enabled", text)
        self.assertNotRegex(text, r"_cb`")
        self.assertIn("`get_company_profile`", text)
        self.assertIn("Data gaps", text)

    def test_workflow_tool_follows_server_registration(self):
        result = server.get_research_workflow("company_lifecycle", company_or_ticker="AAPL")
        enabled_text = "search_organization_cb" in result["instructions"]
        self.assertEqual(enabled_text, server.CRUNCHBASE_ENABLED)


@patch.dict(os.environ, {"CRUNCHBASE_API_KEY": "test-key"})
class ClientTests(unittest.TestCase):
    def setUp(self):
        crunchbase_client.reset_quota_for_tests()

    def tearDown(self):
        crunchbase_client.reset_quota_for_tests()

    def test_sends_key_header_to_v4_data_base(self):
        with patch.object(crunchbase_client, "urlopen", return_value=_mock_response(b'{"entities": []}')) as opened:
            crunchbase_client.request("GET", "/autocompletes", {"query": "example"})
        sent = opened.call_args.args[0]
        self.assertTrue(sent.full_url.startswith("https://api.crunchbase.com/v4/data/autocompletes?"))
        self.assertEqual(sent.get_header("X-cb-user-key"), "test-key")

    def test_order_space_is_percent_encoded(self):
        with patch.object(crunchbase_client, "urlopen", return_value=_mock_response(b"{}")) as opened:
            crunchbase_client.request("GET", "/x", {"order": "announced_on desc", "card_field_ids": "a,b"})
        url = opened.call_args.args[0].full_url
        self.assertIn("order=announced_on%20desc", url)
        self.assertIn("card_field_ids=a,b", url)

    def test_entity_path_cannot_escape_segment(self):
        self.assertEqual(crunchbase_client.entity_path("entities", "../x"), "/entities/..%2Fx")

    def test_missing_key_is_checked_only_when_called(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CRUNCHBASE_API_KEY"):
                crunchbase_client.request("GET", "/autocompletes")

    def test_rate_limit_blocks_before_http(self):
        with (
            patch.object(crunchbase_client, "MINUTE_LIMIT", 2),
            patch.object(crunchbase_client, "urlopen", side_effect=lambda *a, **k: _mock_response(b"{}")) as opened,
        ):
            results = [crunchbase_client.request("GET", "/x") for _ in range(3)]

        self.assertEqual(opened.call_count, 2)
        self.assertNotIn("error", results[0])
        self.assertEqual(results[2]["error_type"], "quota_exhausted")
        self.assertEqual(results[2]["quota"], "per_minute")
        self.assertEqual(results[2]["limit"], 2)
        self.assertIn("retry_after_seconds", results[2])
        self.assertIn("resets_at", results[2])

    def test_default_limit_is_200_per_rolling_minute(self):
        self.assertEqual(crunchbase_client.MINUTE_LIMIT, 200)
        self.assertEqual(crunchbase_client.WINDOW_SECONDS, 60)

    def test_rate_limit_window_rolls(self):
        clock = [1000.0]
        with (
            patch.object(crunchbase_client, "MINUTE_LIMIT", 1),
            patch.object(crunchbase_client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(crunchbase_client, "urlopen", side_effect=lambda *a, **k: _mock_response(b"{}")),
        ):
            self.assertNotIn("error", crunchbase_client.request("GET", "/x"))
            clock[0] += 59
            self.assertEqual(crunchbase_client.request("GET", "/x")["error_type"], "quota_exhausted")
            clock[0] += 1
            self.assertNotIn("error", crunchbase_client.request("GET", "/x"))

    def test_http_error_is_provider_error(self):
        body = b'{"error": "there was an internal server error", "code": "CS500"}'
        error = HTTPError("https://api.crunchbase.com/v4/data/x", 500, "Server Error", {}, io.BytesIO(body))
        with patch.object(crunchbase_client, "urlopen", side_effect=error):
            result = crunchbase_client.request("GET", "/x")
        self.assertEqual(result["error_type"], "provider_error")
        self.assertEqual(result["http_status"], 500)
        self.assertIn("internal server error (CS500)", result["error"])

    def test_401_and_403_are_auth_errors(self):
        # Gateway 401s arrive as text/plain JSON arrays; 403s may have no body.
        bodies = {401: b'[{"status":401,"code":"LA401","message":"Unauthorized user_key"}]', 403: b""}
        for code, body in bodies.items():
            with self.subTest(code=code):
                error = HTTPError("https://api.crunchbase.com/v4/data/x", code, "Denied", {}, io.BytesIO(body))
                with patch.object(crunchbase_client, "urlopen", side_effect=error):
                    result = crunchbase_client.request("GET", "/x")
                self.assertEqual(result, {
                    "error": "Crunchbase API key rejected (likely expired or rotated). Update CRUNCHBASE_API_KEY in Render.",
                    "error_type": "auth_error",
                })

    def test_network_error_is_provider_error(self):
        with patch.object(crunchbase_client, "urlopen", side_effect=URLError("offline")):
            result = crunchbase_client.request("GET", "/x")
        self.assertEqual(result["error_type"], "provider_error")


class FundingRoundTests(unittest.TestCase):
    def _rounds(self, rows):
        payload = _card("raised_funding_rounds", rows)
        with patch.object(server, "crunchbase_request", return_value=payload) as mocked:
            result = server.get_funding_rounds_cb("example-robotics")
        return result, mocked

    def test_undisclosed_amounts_are_not_zero(self):
        result, _ = self._rounds([
            {
                "identifier": _identifier("Seed Round - Example Robotics"),
                "announced_on": "2019-03-01",
                "investment_type": "seed",
                "lead_investor_identifiers": [],
            },
        ])
        round_ = result["funding_rounds"][0]
        self.assertIsNone(round_["amount"])
        self.assertEqual(round_["amount_status"], "not disclosed")
        self.assertIsNone(round_["post_money_valuation"])
        self.assertEqual(round_["post_money_valuation_status"], "not disclosed")
        self.assertNotIn(0, [round_["amount"], round_["post_money_valuation"]])

        audit = result["coverage_audit"]
        self.assertEqual(audit["status"], "partial")
        self.assertIn("amount", audit["missing"]["fields"])
        self.assertEqual(audit["primary_source"]["provider"], "Crunchbase")
        fallback = result["fallback_recommendation"]
        self.assertTrue(fallback["requires_user_confirmation"])
        self.assertEqual(fallback["recommended_source"], "Company filings and business registries")

    def test_disclosed_rounds_complete_and_ordered(self):
        result, mocked = self._rounds([
            {
                "identifier": _identifier("Series A - Example Robotics"),
                "announced_on": "2021-06-01",
                "investment_type": "series_a",
                "money_raised": {"value": 12000000, "currency": "SGD", "value_usd": 9000000},
                "lead_investor_identifiers": [_identifier("Sample Ventures")],
                "post_money_valuation": {"value": 60000000, "currency": "SGD", "value_usd": 45000000},
            },
            {
                "identifier": _identifier("Seed Round - Example Robotics"),
                "announced_on": "2019-03-01",
                "investment_type": "seed",
                "money_raised": {"value": 1000000, "currency": "USD", "value_usd": 1000000},
                "lead_investor_identifiers": [_identifier("Demo Angels")],
                "post_money_valuation": {"value": 5000000, "currency": "USD", "value_usd": 5000000},
            },
        ])
        self.assertEqual([r["date"] for r in result["funding_rounds"]], ["2019-03-01", "2021-06-01"])
        series_a = result["funding_rounds"][1]
        self.assertEqual(series_a["amount"], {"value": 12000000, "currency": "SGD", "value_usd": 9000000})
        self.assertEqual(series_a["lead_investors"], ["Sample Ventures"])
        self.assertEqual(result["coverage_audit"]["status"], "complete")
        self.assertNotIn("fallback_recommendation", result)
        self.assertNotIn("_truncated", result)
        method, path, params, _ = mocked.call_args.args
        self.assertEqual((method, path), ("GET", "/entities/organizations/example-robotics/cards/raised_funding_rounds"))
        self.assertIn("money_raised", params["card_field_ids"])

    def test_card_limit_marks_truncated(self):
        row = {
            "identifier": _identifier("Round"),
            "announced_on": "2020-01-01",
            "investment_type": "seed",
            "money_raised": {"value": 1, "currency": "USD", "value_usd": 1},
            "post_money_valuation": {"value": 2, "currency": "USD", "value_usd": 2},
        }
        result, _ = self._rounds([row] * server.CB_CARD_LIMIT)
        self.assertEqual(result["coverage_audit"]["status"], "truncated")

    def test_empty_rounds_unavailable(self):
        result, _ = self._rounds([])
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")
        self.assertIn("fallback_recommendation", result)

    def test_invalid_identifier_is_validation_without_http(self):
        with patch.object(server, "crunchbase_request") as mocked:
            for bad in ["", "../etc", "a b"]:
                with self.subTest(bad=bad):
                    result = server.get_funding_rounds_cb(bad)
                    self.assertEqual(result["error_type"], "validation")
        mocked.assert_not_called()


class ProviderErrorTests(unittest.TestCase):
    def test_provider_error_is_unavailable_with_fallback(self):
        error = {"error": "Crunchbase returned HTTP 500: internal error (CS500)", "error_type": "provider_error", "http_status": 500}
        with patch.object(server, "crunchbase_request", return_value=error):
            result = server.get_funding_rounds_cb("example-robotics")
        self.assertEqual(result["error_type"], "provider_error")
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")
        self.assertTrue(result["fallback_recommendation"]["requires_user_confirmation"])

    def test_quota_error_reason_names_crunchbase(self):
        quota = {
            "error": "Crunchbase per-minute rate limit reached.",
            "error_type": "quota_exhausted",
            "quota": "per_minute",
            "limit": 200,
            "resets_at": "2026-10-16T10:00:00+08:00",
        }
        with patch.object(server, "crunchbase_request", return_value=quota):
            result = server.search_organization_cb("Example")
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")
        self.assertIn("Crunchbase per_minute", result["coverage_audit"]["missing"]["reason"])

    @patch.dict(os.environ, {"CRUNCHBASE_API_KEY": "expired-key"})
    def test_rejected_key_is_unavailable_never_empty_result(self):
        crunchbase_client.reset_quota_for_tests()
        tools = [
            ("get_funding_rounds_cb", "funding_rounds"),
            ("get_acquisitions_cb", "acquisitions"),
            ("get_investor_portfolio_cb", "investments"),
            ("search_organization_cb", "results"),
        ]
        for code in (401, 403):
            for tool_name, collection in tools:
                with self.subTest(code=code, tool=tool_name):
                    error = HTTPError("https://api.crunchbase.com/v4/data/x", code, "Denied", {}, io.BytesIO(b""))
                    with patch.object(crunchbase_client, "urlopen", side_effect=error) as opened:
                        result = getattr(server, tool_name)("example-robotics")
                    self.assertEqual(result["error_type"], "auth_error")
                    self.assertIn("Update CRUNCHBASE_API_KEY in Render", result["error"])
                    # No empty collection that could be read as "no funding rounds".
                    self.assertNotIn(collection, result)
                    audit = result["coverage_audit"]
                    self.assertEqual(audit["status"], "unavailable")
                    self.assertIn("key rejected", audit["missing"]["reason"])
                    self.assertNotIn("fallback_recommendation", result)
                    # Stop at the first rejection: no second card or person retry.
                    self.assertEqual(opened.call_count, 1)
        crunchbase_client.reset_quota_for_tests()

    def test_unexpected_exception_becomes_provider_error(self):
        with patch.object(server, "crunchbase_request", side_effect=RuntimeError("boom")):
            result = server.get_acquisitions_cb("example-robotics")
        self.assertEqual(result["error_type"], "provider_error")
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")


class SearchTests(unittest.TestCase):
    def test_public_candidate_carries_symbol_private_does_not(self):
        payload = {
            "count": 2,
            "entities": [
                {
                    "uuid": "uuid-public",
                    "properties": {
                        "identifier": _identifier("Example Robotics"),
                        "short_description": "Fictitious robotics maker.",
                        "location_identifiers": [
                            {"value": "Singapore", "location_type": "city"},
                            {"value": "Singapore", "location_type": "country"},
                        ],
                        "founded_on": {"value": "2015-01-01", "precision": "year"},
                        "ipo_status": "public",
                        "stock_symbol": {"value": "EXRB"},
                        "stock_exchange_symbol": "sgx",
                    },
                },
                {
                    "uuid": "uuid-private",
                    "properties": {
                        "identifier": _identifier("Example Robotics Labs"),
                        "short_description": "Fictitious research spin-off.",
                        "location_identifiers": [{"value": "Malaysia", "location_type": "country"}],
                        "founded_on": {"value": "2020-05-01", "precision": "month"},
                        "ipo_status": "private",
                    },
                },
            ],
        }
        with patch.object(server, "crunchbase_request", return_value=payload) as mocked:
            result = server.search_organization_cb("  Example Robotics ")

        public, private = result["results"]
        self.assertEqual(public["stock_symbol"], "EXRB")
        self.assertEqual(public["stock_exchange"], "sgx")
        self.assertEqual(public["country"], "Singapore")
        self.assertEqual(public["founded_on"], "2015-01-01")
        self.assertEqual(public["permalink"], "example-robotics")
        self.assertNotIn("stock_symbol", private)
        self.assertIn("Yahoo", result["note"])
        self.assertEqual(result["coverage_audit"]["status"], "complete")

        method, path, _, body = mocked.call_args.args
        self.assertEqual((method, path), ("POST", "/searches/organizations"))
        self.assertEqual(body["query"][0]["values"], ["Example Robotics"])
        self.assertIn("stock_symbol", body["field_ids"])

    def test_no_matches_unavailable(self):
        with patch.object(server, "crunchbase_request", return_value={"count": 0, "entities": []}):
            result = server.search_organization_cb("Nonexistent Example")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")


class AcquisitionTests(unittest.TestCase):
    def test_both_roles_and_undisclosed_price(self):
        def respond(method, path, params, body):
            if path.endswith("/acquirer_acquisitions"):
                return _card("acquirer_acquisitions", [{
                    "announced_on": {"value": "2022-02-01", "precision": "day"},
                    "acquirer_identifier": _identifier("Example Robotics"),
                    "acquiree_identifier": _identifier("Tiny Sensors"),
                    "acquisition_type": "acquisition",
                    "status": "complete",
                }])
            return _card("acquiree_acquisitions", [{
                "announced_on": {"value": "2024-08-01", "precision": "day"},
                "acquirer_identifier": _identifier("Big Holding Example"),
                "acquiree_identifier": _identifier("Example Robotics"),
                "acquisition_type": "acquisition",
                "status": "pending",
                "price": {"value": 300000000, "currency": "USD", "value_usd": 300000000},
            }])

        with patch.object(server, "crunchbase_request", side_effect=respond):
            result = server.get_acquisitions_cb("example-robotics")

        roles = [(row["role"], row["date"]) for row in result["acquisitions"]]
        self.assertEqual(roles, [("acquirer", "2022-02-01"), ("acquiree", "2024-08-01")])
        bought = result["acquisitions"][0]
        self.assertIsNone(bought["price"])
        self.assertEqual(bought["price_status"], "not disclosed")
        self.assertEqual(result["acquisitions"][1]["price"]["value"], 300000000)
        self.assertEqual(result["coverage_audit"]["status"], "partial")
        self.assertIn("price", result["coverage_audit"]["missing"]["fields"])

    def test_one_failed_card_is_partial(self):
        def respond(method, path, params, body):
            if path.endswith("/acquirer_acquisitions"):
                return {"error": "Crunchbase returned HTTP 500: x", "error_type": "provider_error", "http_status": 500}
            return _card("acquiree_acquisitions", [{
                "announced_on": {"value": "2024-08-01", "precision": "day"},
                "acquirer_identifier": _identifier("Big Holding Example"),
                "acquiree_identifier": _identifier("Example Robotics"),
                "acquisition_type": "acquisition",
                "status": "complete",
                "price": {"value": 1, "currency": "USD", "value_usd": 1},
            }])

        with patch.object(server, "crunchbase_request", side_effect=respond):
            result = server.get_acquisitions_cb("example-robotics")
        self.assertEqual(result["coverage_audit"]["status"], "partial")
        self.assertEqual(result["acquisitions"][-1]["role"], "acquirer")
        self.assertIn("error", result["acquisitions"][-1])


class InvestorPortfolioTests(unittest.TestCase):
    @staticmethod
    def _investment(index, amount=None):
        row = {
            "announced_on": f"2023-01-{index + 1:02d}",
            "organization_identifier": _identifier(f"Portfolio Co {index}"),
            "funding_round_identifier": _identifier(f"Series A - Portfolio Co {index}"),
            "funding_round_investment_type": "series_a",
            "is_lead_investor": True,
        }
        if amount is not None:
            row["money_invested"] = {"value": amount, "currency": "USD", "value_usd": amount}
        return row

    def test_person_fallback_after_organization_404(self):
        not_found = {"error": "Crunchbase returned HTTP 404: not found (CS404)", "error_type": "provider_error", "http_status": 404}

        def respond(method, path, params, body):
            if path.startswith("/entities/organizations/"):
                return not_found
            return _card("participated_investments", [self._investment(0)], name="Example Angel")

        with patch.object(server, "crunchbase_request", side_effect=respond) as mocked:
            result = server.get_investor_portfolio_cb("example-angel", limit=5)

        self.assertEqual(result["investor"]["investor_kind"], "person")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(mocked.call_args.args[2]["order"], "announced_on desc")
        investment = result["investments"][0]
        self.assertIsNone(investment["amount"])
        self.assertEqual(investment["amount_status"], "not disclosed")
        self.assertEqual(result["coverage_audit"]["status"], "partial")

    def test_limit_hit_is_truncated(self):
        rows = [self._investment(i, 1000) for i in range(3)]
        payload = _card("participated_investments", rows, name="Sample Ventures")
        with patch.object(server, "crunchbase_request", return_value=payload):
            result = server.get_investor_portfolio_cb("sample-ventures", limit=3)
        self.assertEqual(result["investor"]["investor_kind"], "organization")
        self.assertEqual(result["coverage_audit"]["status"], "truncated")

    def test_invalid_limit(self):
        with patch.object(server, "crunchbase_request") as mocked:
            for bad in [0, 101, "10", True]:
                with self.subTest(limit=bad):
                    result = server.get_investor_portfolio_cb("sample-ventures", limit=bad)
                    self.assertEqual(result["error_type"], "validation")
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
