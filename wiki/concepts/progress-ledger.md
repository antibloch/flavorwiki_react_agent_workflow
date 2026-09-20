---
type: concept
title: "Append-only progress memory"
tags: [prompt-design, cache-prefix, short-term-memory]
sources: [funda-agent-exp]
created: 2026-08-14
updated: 2026-08-18
---

Progress memory is stored as model-authored, append-only evidence deltas inside persisted
`AIMessage.content`. It is no longer metadata on `nl2sql_tool` calls and there is no transient
tracker `HumanMessage`.

## Message contract

After a tool batch, an assistant turn that makes another tool call begins with one internal block:

```text
<progress_gathered>
- [source: tool or human-readable dataset] newly established fact
</progress_gathered>
```

Each block summarizes only the immediately preceding batch. Parallel results are unioned into the
same block. The limits are 12 bullets and 2,000 characters. Plans, open gaps, SQL, identifiers and
facts copied from older blocks are excluded.

The block is an index, not a source of truth: raw `ToolMessage` results remain authoritative. The
final answer closes the round and contains no progress block. If the model omits or malforms a
block, execution continues from raw tool history and logs compliance telemetry; the runtime never
invents replacement evidence.

## Cache-prefix behavior

The state grows naturally as:

```text
system → user → assistant/tool call → tool result
       → assistant(progress delta + next tool call) → tool result → final answer
```

Because no mutable tracker is appended to one request and removed from the next, each request in a
ReAct round extends the previous request exactly. This maximizes prompt-cache eligibility until
`_trim_history()` deliberately removes completed old rounds. It does not guarantee a provider cache
hit; cached-token and latency telemetry remain the operational test.

CLI and API presentation remove internal blocks without modifying checkpointed messages. The API
filter is incremental so opening and closing tags split across streamed chunks cannot leak.

## Loop diagnostics

`call_tool()` stores canonical-query/result fingerprints for the most recent SQL batch. The same
canonical query returning the same result in the next SQL batch, or twice in one parallel batch,
receives an objective repeat warning. `zero_row_streak` independently prompts join diagnosis after
two consecutive all-empty SQL batches. Both fields reset for every genuine user round.

## Related

[[langgraph-react-topology]] · [[parallel-tool-calling]] · [[sql-guardrails-and-scope]] · [[funda-agent-exp]]
