import asyncio
import re
import unittest

import prompts
import server


def _render(name, arguments):
    result = asyncio.run(server.mcp.get_prompt(name, arguments))
    return "\n".join(message.content.text for message in result.messages)


class PromptRegistrationTests(unittest.TestCase):
    def test_prompts_are_listed(self):
        listed = {prompt.name for prompt in asyncio.run(server.mcp.list_prompts())}
        self.assertEqual(
            listed,
            {"company_research_brief", "finance_literature_review", "peer_comparison"},
        )

    def test_prompts_only_reference_existing_tools(self):
        tool_names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        rendered = [
            _render("company_research_brief", {"ticker": "AAPL"}),
            _render("finance_literature_review", {"topic": "momentum"}),
            _render("peer_comparison", {"tickers": "AAPL, MSFT"}),
        ]
        for text in rendered:
            for name in re.findall(r"`([a-z_]+)`", text):
                if "_" in name and not name.startswith(("coverage", "fallback", "suggested", "primary", "not_", "holder", "period", "year", "limit")):
                    self.assertIn(name, tool_names, f"Prompt references unknown tool {name}")

    def test_every_prompt_carries_coverage_rules(self):
        for name, arguments in [
            ("company_research_brief", {"ticker": "AAPL"}),
            ("finance_literature_review", {"topic": "momentum"}),
            ("peer_comparison", {"tickers": "AAPL, MSFT"}),
        ]:
            text = _render(name, arguments)
            self.assertIn("coverage_audit.status", text)
            self.assertIn("do NOT fetch the suggested", text)
            self.assertIn("Never blend Alpha Vantage", text)


class PromptContentTests(unittest.TestCase):
    def test_brief_normalises_ticker_and_focus(self):
        text = prompts.company_research_brief("  d05.si ", focus="  dividend   policy ")
        self.assertIn("research brief on D05.SI", text)
        self.assertIn("Give particular attention to: dividend policy.", text)

    def test_brief_rejects_bad_ticker(self):
        for bad in ["", "   ", "AAPL; rm -rf", "x" * 20]:
            with self.assertRaises(ValueError):
                prompts.company_research_brief(bad)

    def test_literature_review_options(self):
        with_preprints = prompts.finance_literature_review("ESG returns", year_from=2020)
        self.assertIn("year_from 2020", with_preprints)
        self.assertIn("search_finance_preprints", with_preprints)
        without = prompts.finance_literature_review("ESG returns", include_preprints=False)
        self.assertNotIn("search_finance_preprints", without)
        with self.assertRaises(ValueError):
            prompts.finance_literature_review("  ")

    def test_peer_comparison_dedupes_and_bounds(self):
        text = prompts.peer_comparison("aapl, MSFT; aapl")
        self.assertIn("[AAPL, MSFT]", text)
        with self.assertRaises(ValueError):
            prompts.peer_comparison("AAPL")
        with self.assertRaises(ValueError):
            prompts.peer_comparison(",".join(f"T{i}" for i in range(11)))


if __name__ == "__main__":
    unittest.main()
