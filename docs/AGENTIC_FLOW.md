# How the agentic flow works

This server is the tool layer of an agentic research system. It does not
contain an AI model and makes no decisions of its own. The **agent** is the AI
assistant you connect it to (for example ChatGPT or Claude). The server's job
is to give that agent three things:

1. **Tools** that fetch public market data and finance literature.
2. **Recipes** (workflows) that tell the agent which tools to call, in what
   order, for common research tasks.
3. **Guardrails** in every response that tell the agent what data is missing
   and when it must stop and ask you before going further.

The agent supplies the judgment: it decides which tools to use, reads the
results, adapts when something is missing, writes the answer, and asks for
your approval when a guardrail requires it.

## Who does what

| | The agent (ChatGPT, Claude, …) | This server |
| --- | --- | --- |
| Understands your question | Yes | No |
| Decides which tools to call and in what order | Yes, optionally guided by a recipe | Supplies the recipes |
| Fetches data | Asks for it | Yes, read-only |
| Calculates ratios (margins, ROE, price changes) | Reports them | Yes, in code, through the composite tools |
| Checks data completeness | Reads the audit | Writes a `coverage_audit` on every response |
| Suggests other sources when data is missing | Relays the suggestion to you | Writes a `fallback_recommendation` |
| Uses another source | Only after you say yes | Never on its own |
| Writes the final answer | Yes | No |

## The flow, step by step

```mermaid
sequenceDiagram
    actor You
    participant Agent as AI agent (ChatGPT / Claude)
    participant Server as Finance Data and Lit MCP server
    participant Data as Yahoo Finance, OpenAlex, arXiv, DOAJ

    You->>Agent: "Compare DBS, OCBC and UOB"
    Agent->>Server: get_research_workflow(peer_comparison, tickers)
    Server-->>Agent: Step-by-step recipe + coverage rules
    loop For each step in the recipe
        Agent->>Server: Call the next tool (e.g. compare_peers, get_news)
        Server->>Data: Fetch public data
        Data-->>Server: Raw data
        Server-->>Agent: Data + coverage_audit (+ fallback_recommendation if incomplete)
        Agent->>Agent: Check the audit and decide what to do next
    end
    Agent->>You: Comparison, with gaps labelled
    Agent->>You: "Data was incomplete. Use SGXNet and annual reports as a supplementary source?"
    You-->>Agent: Yes / No
    Note over Agent,Server: Supplementary data is only fetched after an explicit yes,<br/>and is always labelled separately from primary data.
```

## The building blocks

### Recipes: workflow prompts and the workflow tool

Three recipes live in [`prompts.py`](../prompts.py):

| Workflow | What the agent is told to do |
| --- | --- |
| `company_research_brief` | Company profile with server-computed ratios, one-year price history, dividends and splits, major holders, news, and related academic research, then a "Data gaps" section |
| `finance_literature_review` | Search published research (OpenAlex), read the top papers, search preprints (arXiv) separately, and check journals against DOAJ |
| `peer_comparison` | Call `compare_peers`, report its server-computed figures without recalculating, and explain any gaps |

The recipes reach the agent in two ways, because AI clients differ:

- **As MCP prompts**, for clients that support them (Claude, VS Code). They
  appear as selectable workflows.
- **As the `get_research_workflow` tool**, for tool-only clients such as
  ChatGPT. The tool returns exactly the same text; a test guarantees the two
  never drift apart.

A recipe is used when the agent picks it. Asking explicitly ("use the peer
comparison workflow for D05.SI, O39.SI, U11.SI") makes that certain; natural
requests such as "compare DBS and OCBC" will often trigger it, because the tool
description tells the agent when to use it, but the choice is the agent's.

### Guardrails: the coverage contract

Every tool response, whether or not a recipe is in use, carries a
`coverage_audit` (see [Coverage contract](../README.md#coverage-contract)). Its
`status` tells the agent whether the data is `complete`, `partial`,
`truncated`, `unavailable` or `not_supported`, so it can say "unavailable"
rather than report a missing figure as zero.

When data is incomplete, a `fallback_recommendation` names the most
authoritative supplementary source for that company's listing market (for
example SEC EDGAR for US tickers, SGXNet and annual reports for `.SI` tickers;
see [`markets.py`](../markets.py)). It always sets
`requires_user_confirmation: true` and includes a ready-made question for the
agent to ask you. The server never fetches supplementary data by itself.

Every recipe also restates these rules in plain language, so the agent sees
them before it starts.

### Human in the loop

Two decisions always come back to you:

- **Using a supplementary source** suggested by a `fallback_recommendation`.
- **Spending an Alpha Vantage request** when a Yahoo Finance quote is
  incomplete. Clients that support MCP elicitation show an accept/decline
  prompt; others receive the offer as text for the agent to relay.

## Composite tools: reliable building blocks

`compare_peers` and `get_company_profile` run a fixed chain of the
single-purpose tools inside the server and calculate the ratios in code, so
the agent reports figures instead of doing arithmetic. They:

- use each company's **latest annual period** only, and never fill a missing
  figure from an earlier year;
- name the **line item and date** behind every figure (`line_items_used`,
  `fiscal_period_end`, `price_window`);
- refuse to compute ROE when the income statement and balance sheet are from
  different years;
- return one **combined coverage audit** that checks only the fields they
  report, while keeping each underlying tool's audit in `component_audits`.

This is deliberately *less* agentic than letting the model do everything:
mechanical steps move into code, and the agent keeps the judgment.

## Worked example

Request: *"Use the peer comparison workflow for D05.SI, O39.SI, U11.SI."*

1. The agent calls `get_research_workflow` and receives the peer comparison
   recipe.
2. It calls `compare_peers` for the three banks. The server fetches each
   bank's quote, latest annual income statement and balance sheet, and one
   year of weekly prices, then computes net margin, ROE and price change.
3. The combined `coverage_audit` is `complete`, because every reported
   figure is present for FY2025. Blank fields in older years are ignored,
   though the underlying statement audits still show them as `partial`.
4. The agent builds the table from the returned figures, quotes the fiscal
   period and price window, and notes that ROE is the better measure for
   banks.
5. Had a figure been missing, it would appear in
   `coverage_audit.missing.gaps` with a reason, and the agent would end by
   asking: *"Would you like me to continue using SGXNet company announcements
   and issuer annual reports as a supplementary source?"* Nothing is fetched
   until you answer.

## What this server deliberately does not do

- It does not call an AI model or make decisions.
- It does not fetch supplementary sources without your confirmation.
- It does not merge Alpha Vantage figures into Yahoo Finance figures.
- It does not give investment advice. Recipes instruct the agent not to
  either.

## Roadmap

The agentic capability is being added in small steps:

| Step | Change | Status |
| --- | --- | --- |
| 1 | Workflow recipes as MCP prompts | Done |
| 1b | `get_research_workflow` tool for tool-only clients; correct read-only annotations on all tools | Done |
| – | Market-aware fallback recommendations (SGX, HKEX, LSE, JPX, ASX, TSX, NSE/BSE, KRX) | Done |
| 2 | Composite tools (`compare_peers`, `get_company_profile`) that run fixed multi-step chains and compute ratios in code, with a combined audit that only flags gaps in the data actually used | Done |
| 3 | First model-in-the-loop step: the server asks the client's model to summarise research results (MCP sampling), with a plain-data fallback for clients that cannot | Planned |
| 4 | Bounded decision loop: the model chooses the next tool within fixed step and quota limits, with every fallback still confirmed by the user | Planned |
| 5 | A separate orchestrator agent that uses this server as its tool layer | Planned |
| 6 | Evaluations that check the guardrails hold (no unconfirmed fallbacks, no blended sources, empty never reported as zero) | Planned |

Steps 1 and 2 make the agent more reliable by giving it good recipes and
dependable building blocks. Steps 3 and 4 are where the server itself begins
to use a model.
