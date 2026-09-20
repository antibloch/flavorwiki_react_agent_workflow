---
type: concept
title: "Parallel tool calling"
tags: [agent-variant]
sources: [funda-agent-exp-architecture-doc]
created: 2026-08-14
updated: 2026-08-14
---

[[funda-agent-exp]] has parallel tool-call capability at two independent layers, and the narrower one governs. Understanding the gap between them explains why an assistant turn containing several tool calls may still execute serially.

## Layer 1 — the provider

Both LLM bindings set `parallel_tool_calls=True`, so one AI response may contain several tool calls.

## Layer 2 — Python execution policy

`call_tool()` executes a batch concurrently **only when it contains at least two calls and every call is `nl2sql_tool`.** Execution uses a process-wide `ThreadPoolExecutor` with `MAX_PARALLEL_SQL_CALLS` workers (default 4), each call taking its own pooled connection.

Everything else runs sequentially. Specifically, these do **not** execute concurrently even when the provider emitted them in one message:

- SQL + statistics
- SQL + packet
- multiple packet calls
- multiple statistics calls

So the only parallelised operation is read-only SQL against the local database — the case where concurrency is both safe and worth having. The external Charts API call and the packet path, which carry authorization checks and network dependencies, stay serial.

## Deterministic ordering

Results are consumed in **original call order, not completion order**. This preserves a stable model-facing message history: the same batch produces the same transcript regardless of which query happened to finish first. "Independent SQL results remain in original tool-call order" is one of the agent's stated invariants.

## Dependent queries must be sequential across turns

The model may issue several *independent* SQL calls in one response, but a dependent call must wait for the next turn: if query B needs an ID returned by query A, then A completes in the current cycle and B is authored on the following LLM turn. There is no intra-batch data flow.

## Interaction with the ledger

Parallel calls in one batch are expected to describe the same batch-level completion state. Where they disagree, the merger in [[progress-ledger]] keeps facts and gaps conservatively, and the latch conditions in [[model-and-prompt-routing]] require *every* call to signal completion — so a single cautious call in a parallel batch prevents the batch from latching.

## Related

[[langgraph-react-topology]] · [[progress-ledger]] · [[model-and-prompt-routing]] · [[sql-guardrails-and-scope]]
