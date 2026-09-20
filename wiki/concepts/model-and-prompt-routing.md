---
type: concept
title: "Single-model prompt execution and cache routing"
tags: [prompt-design, cache-prefix, agent-variant]
sources: [funda-agent-exp]
created: 2026-08-14
updated: 2026-08-18
---

Every ordinary LLM turn uses the same GPT-5.5 `ChatOpenAI` instance and the complete
`SYSTEM_PROMPT_TEXT`. There is no strong/weak model routing, full/lean prompt routing, SQL-complete
latch, or analysis-packet latch.

Tools remain bound until the last permitted LLM turn. On that turn the same model is invoked
without tools and receives a local step-budget notice, forcing a final answer from evidence already
retrieved.

## Stable prefix

The system prompt is built once at import time. Per-run scope IDs and inventory data live in the
first user message, so the system block is byte-identical across surveys. Within a thread, message 0
is permanently retained as its scope/inventory anchor.

ReAct history is append-only. After a tool batch, any assistant response that makes another tool
call includes an internal evidence delta in its persisted content. No changing tracker is appended
to the next request, so request `N+1` extends request `N` exactly until history trimming activates.

Each invocation also sends `prompt_cache_key=funda-thread:<sha256(thread_id)>` when LangGraph has a
conversation `thread_id`. The hash keeps caller-supplied thread identifiers out of provider-facing
routing metadata while giving every turn in one conversation a stable cache-routing key.

Provider caching still requires exact prefix matches and is not guaranteed. Cached-input-token and
latency telemetry are the acceptance measures; structural prefix tests prove only eligibility.

## Tool and terminal behavior

- `nl2sql_tool`, analysis packets, statistics, feature influence and word cloud are available on
  ordinary turns.
- `should_continue()` ends when the latest assistant message contains no tool call or the hard turn
  budget is exhausted.
- A successful word-cloud tool stores a trusted artifact in graph state and `call_model()` returns
  it deterministically without another OpenAI invocation.
- SQL repetition and zero-row diagnostics affect tool feedback, not model selection.

## Related

[[conversation-memory-and-trimming]] · [[progress-ledger]] · [[langgraph-react-topology]]
