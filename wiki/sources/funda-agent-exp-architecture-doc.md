---
type: source
title: "funda_agent_exp.py — current architecture and execution flow"
authors: ["FlavorAI project (internal document)"]
raw: "docs/agent_exp_doc.md"
ingested: 2026-08-14
source_audit: 2026-08-10
tags: [agent-variant, prompt-design, db-schema]
entities: [funda-agent-exp, api-funda-agent-exp, survey-inventory-packet]
concepts: [langgraph-react-topology, model-and-prompt-routing, sql-guardrails-and-scope, progress-ledger, conversation-memory-and-trimming, parallel-tool-calling]
created: 2026-08-14
updated: 2026-08-14
---

The primary reference document for the agent in this working copy: 806 lines covering purpose, runtime construction, graph topology, state, routing, tools, the inventory packet, prompt ownership, memory, the HTTP wrapper, configuration defaults, known risks, and the invariants to preserve when changing the code. It explicitly scopes itself to the `final_agent_work_v5_base_2_optim3` implementation and disclaims earlier result-parking and drilling-agent experiments.

The document carries its own **last source audit date of 2026-08-10**. That date matters: [[client-boundary-session-handoff]] describes authorization work verified 2026-08-12 that contradicts two of this document's claims. See [[survey-authorization-scope-contradiction]].

## Key claims

**The agent is a guarded ReAct loop, not an autonomous planner.** The division of labour is the document's central architectural claim: the model owns semantic judgment (what the question means, which measure is appropriate, which test to run, how to explain the result) while Python deterministically owns scope checks, SQL safety, tool execution, routing, state updates and termination. The stated rationale is that this prevents the model from overriding security boundaries while preserving judgment where deterministic keyword logic would hide evidence.

**The graph is deliberately minimal** — two nodes, `LLM` and `tools`, with a hard budget of `MAX_LLM_STEPS = 12` LLM invocations. Detail in [[langgraph-react-topology]].

**Routing is state-driven, not model-chosen.** Two `ChatOpenAI` instances at temperature zero (strong `gpt-5.5`, weak `gpt-5.4-mini`) are selected by latch state, along with a full-or-lean system prompt. Detail in [[model-and-prompt-routing]].

**Exactly three tools exist:** `nl2sql_tool`, `get_survey_analysis_packet`, `run_survey_stats`. The document is emphatic that there is no result store, result parking, `describe_result`/`query_result`/`slice_result`, digest workflow or drilling sub-agent in the current implementation — those belong to superseded experiments.

**Computation is pushed into PostgreSQL.** The startup inventory is one statement (`_INV_SQL`) returning products, per-product scored measures with means and sample SDs, and remaining answered measures — so the model reads statistics rather than raw rows. Detail in [[survey-inventory-packet]].

**Packets are complete or absent, never truncated.** A partial candidate list could silently change which measure the model selects as semantically appropriate, so an oversized packet falls back to SQL discovery instead of being trimmed. The same rule governs the persona packet.

**Authorization fails closed for packets and statistics, but is advisory for model-authored SQL.** This is the document's §16.3 position and it is the claim most directly contradicted by the later handoff.

## Notable design details

- On the last permitted LLM turn, tools are deliberately **unbound** and a step-budget message instructs the model to answer from evidence already retrieved and disclose what is unresolved.
- The lean prompt is asserted at import time to be a literal **prefix** of the full prompt; per-run IDs live in the user message so the system prefix stays stable across surveys.
- `sql_done` is a routing latch only — it does not mean the graph has ended. `should_continue()` terminates on an AI message with no tool calls, irrespective of `sql_done`.
- Persona/profile turns retain the strong model after SQL completion, and their formatting contract is prompt-guided rather than deterministically enforced — no hidden format-repair call.
- `dominance.significant=true` is a deterministic gate: without it, a largest category is reported as mixed rather than dominant. Multi-select questions get no dominance verdict at all because selections overlap.
- Cross-survey correlation is rejected outright because enrollments provide no usable cross-survey respondent key.

## Open questions and stale wording flagged by the document itself

- The `nl2sql_tool` model-facing description still mentions a stored-result manifest for oversized output, but no such storage path is wired in. The document flags its own tool description as stale.
- `_SCOPE` is a process global, so the core agent is not re-entrant; true in-process concurrency would require moving scope into graph state and passing it explicitly to every tool.
- The API scope-resolution cache is unbounded with no TTL, assuming survey ownership metadata is stable for the worker's lifetime.

Full list in [[known-limitations-and-risks]].

## Where this fits

Describes [[funda-agent-exp]] and its HTTP wrapper [[api-funda-agent-exp]]. Its mechanisms are broken out into [[langgraph-react-topology]], [[model-and-prompt-routing]], [[sql-guardrails-and-scope]], [[progress-ledger]], [[parallel-tool-calling]], [[conversation-memory-and-trimming]] and [[survey-inventory-packet]]. Its §16 feeds [[known-limitations-and-risks]]. Two of its claims are contested by [[client-boundary-session-handoff]] — see [[survey-authorization-scope-contradiction]]. Reading order guidance is corroborated by [[final-agent-work-bundle-readme]].
