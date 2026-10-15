import asyncio
import json
import unittest
from unittest.mock import patch

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

import server

PUBLISHED = {
    "results": [
        {"title": "ESG and bank returns", "authors": ["A. Tan"], "year": 2023,
         "journal": "Journal of Banking & Finance", "doi": "https://doi.org/10.1/x",
         "abstract_snippet": "Higher ESG scores are associated with lower risk.", "cited_by_count": 12},
        {"title": "Green lending", "authors": ["B. Lim"], "year": 2024,
         "journal": "Finance Research Letters", "doi": None,
         "abstract_snippet": "Green loans show mixed performance.", "cited_by_count": 3},
    ],
    "coverage_audit": {"status": "complete"},
}
PREPRINTS = {
    "results": [
        {"title": "Climate risk in Asian banks", "authors": ["C. Ng"],
         "submission_date": "2025-03-01T00:00:00Z", "arxiv_id": "2503.00001",
         "abstract_snippet": "We find climate exposure raises funding costs."},
    ],
    "coverage_audit": {"status": "complete"},
}

GOOD_SYNTHESIS = (
    "Key themes\nESG links to lower bank risk [P1], while green lending is mixed [P2].\n"
    "Main findings\nA preprint suggests climate exposure raises funding costs [R1]."
)


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    def __init__(self, supports=True, reply=GOOD_SYNTHESIS, error=None):
        self.supports, self.reply, self.error, self.calls = supports, reply, error, []

    def check_client_capability(self, capability):
        return self.supports

    async def create_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return types.CreateMessageResult(
            role="assistant", content=types.TextContent(type="text", text=self.reply), model="test-model"
        )


class FakeContext:
    def __init__(self, session):
        self.session = session


class SynthesisTestCase(unittest.TestCase):
    def setUp(self):
        for item in [
            patch.object(server, "search_finance_research", return_value=PUBLISHED),
            patch.object(server, "search_finance_preprints", return_value=PREPRINTS),
        ]:
            item.start()
            self.addCleanup(item.stop)


class SamplingTests(SynthesisTestCase):
    def test_generates_with_client_model_and_checks_citations(self):
        session = FakeSession()
        result = run(server.synthesize_research("ESG banks", ctx=FakeContext(session)))
        self.assertEqual(result["synthesis_status"], "generated")
        self.assertEqual(result["synthesis"], GOOD_SYNTHESIS)
        self.assertEqual(result["synthesis_by"]["model"], "test-model")
        self.assertIn("no paid API", result["synthesis_by"]["method"])
        check = result["citation_check"]
        self.assertTrue(check["passed"])
        self.assertEqual(check["cited_ids"], ["P1", "P2", "R1"])
        # The request carries the guardrails and only the supplied sources.
        call = session.calls[0]
        self.assertIn("Use only the numbered sources", call["system_prompt"])
        self.assertEqual(call["include_context"], "none")
        request_text = call["messages"][0].content.text
        self.assertIn("[R1] Climate risk in Asian banks", request_text)
        self.assertIn("PREPRINT, not peer reviewed", request_text)

    def test_citation_check_flags_invented_and_unlabelled_sources(self):
        reply = "Banks benefit [P1, P9]. Funding costs rise [R1]."
        result = run(server.synthesize_research("ESG banks", ctx=FakeContext(FakeSession(reply=reply))))
        check = result["citation_check"]
        self.assertFalse(check["passed"])
        self.assertEqual(check["unknown_ids"], ["P9"])
        self.assertTrue(any("preprints without" in warning for warning in check["warnings"]))
        self.assertEqual(check["uncited_ids"], ["P2"])

    def test_no_citations_is_flagged(self):
        result = run(server.synthesize_research("ESG banks", ctx=FakeContext(FakeSession(reply="ESG is good."))))
        self.assertIn("no source citations", result["citation_check"]["warnings"][0])


class FallbackTests(SynthesisTestCase):
    def test_client_without_sampling_gets_sources_and_instructions(self):
        session = FakeSession(supports=False)
        result = run(server.synthesize_research("ESG banks", ctx=FakeContext(session)))
        self.assertIsNone(result["synthesis"])
        self.assertEqual(result["synthesis_status"], "client_does_not_support_sampling")
        self.assertIn("Use only the numbered sources", result["instructions_for_agent"])
        self.assertEqual([s["id"] for s in result["sources"]], ["P1", "P2", "R1"])
        self.assertEqual(session.calls, [])

    def test_no_context_behaves_like_no_sampling(self):
        result = run(server.synthesize_research("ESG banks"))
        self.assertEqual(result["synthesis_status"], "client_does_not_support_sampling")

    def test_declined_sampling_falls_back(self):
        session = FakeSession(error=RuntimeError("User rejected sampling request"))
        result = run(server.synthesize_research("ESG banks", ctx=FakeContext(session)))
        self.assertEqual(result["synthesis_status"], "sampling_declined_or_failed")
        self.assertIn("rejected", result["sampling_error"])
        self.assertIn("instructions_for_agent", result)

    def test_preprints_can_be_excluded(self):
        result = run(server.synthesize_research("ESG banks", include_preprints=False))
        self.assertEqual([s["id"] for s in result["sources"]], ["P1", "P2"])
        server.search_finance_preprints.assert_not_called()


class CoverageTests(unittest.TestCase):
    def test_no_sources_is_unavailable_with_fallback(self):
        with patch.object(server, "search_finance_research", return_value={"error": "No finance research found.", "coverage_audit": {"status": "unavailable"}}), \
             patch.object(server, "search_finance_preprints", return_value={"error": "No preprints found.", "coverage_audit": {"status": "unavailable"}}):
            session = FakeSession()
            result = run(server.synthesize_research("nothing", ctx=FakeContext(session)))
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")
        self.assertEqual(result["synthesis_status"], "no_sources")
        self.assertTrue(result["fallback_recommendation"]["requires_user_confirmation"])
        self.assertEqual(session.calls, [])

    def test_validation(self):
        for kwargs in [{"topic": " "}, {"topic": "x", "limit": 0}, {"topic": "x", "limit": 99}]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(run(server.synthesize_research(**kwargs))["error_type"], "validation")


class EndToEndTests(SynthesisTestCase):
    """Real MCP round trip: the test client plays the role of the AI app."""

    def _call(self, sampling_callback=None):
        async def go():
            async with create_connected_server_and_client_session(
                server.mcp, sampling_callback=sampling_callback
            ) as client:
                result = await client.call_tool("synthesize_research", {"topic": "ESG banks"})
                return json.loads(result.content[0].text)
        return asyncio.run(go())

    def test_round_trip_with_sampling_client(self):
        seen = []

        async def app_model(context, params):
            seen.append(params)
            return types.CreateMessageResult(
                role="assistant", content=types.TextContent(type="text", text=GOOD_SYNTHESIS), model="app-model"
            )

        result = self._call(app_model)
        self.assertEqual(result["synthesis_status"], "generated")
        self.assertEqual(result["synthesis_by"]["model"], "app-model")
        self.assertTrue(result["citation_check"]["passed"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].maxTokens, server.SYNTHESIS_MAX_TOKENS)

    def test_round_trip_without_sampling_client(self):
        result = self._call(None)
        self.assertEqual(result["synthesis_status"], "client_does_not_support_sampling")
        self.assertEqual(len(result["sources"]), 3)


if __name__ == "__main__":
    unittest.main()
