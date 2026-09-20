---
type: concept
title: "Conversation memory and history trimming"
tags: [agent-variant, cache-prefix, short-term-memory]
sources: [funda-agent-exp, survey-analyst-api-contract]
created: 2026-08-14
updated: 2026-08-18
---

Checkpointed history and model-visible history are different. The checkpoint retains the complete
thread; `_trim_history()` bounds only what is sent to the model.

## Checkpointing and round state

The CLI and API use `InMemorySaver`. Reusing a `thread_id` resumes accumulated `messages`, while
every new question resets `number_of_steps`, `last_sql_observations`, `zero_row_streak`, and
word-cloud scratch. API threads are bound to their original survey.

Progress memory is part of ordinary persisted history. An assistant response that continues after
a tool batch places a sourced `<progress_gathered>` delta in the same `AIMessage` as its next tool
calls. No synthetic `HumanMessage` or `ToolMessage` is created. This keeps round-start detection
unambiguous and preserves the provenance role of raw tool results.

## Request-history trimming

The assembled request is:

```python
[SYSTEM_PROMPT] + _trim_history(state["messages"])
```

On the final allowed turn only, a local step-budget `HumanMessage` is appended. It never enters
state.

`_trim_history()` keeps message 0 plus the five most recent user rounds by default. Message 0 is
the permanent scope/inventory anchor. Boundaries are genuine `HumanMessage` round openers, so tool
results cannot be orphaned. The selected boundary remains fixed during the active ReAct round,
preserving append-only cache growth until the next user question.

`HISTORY_MAX_ROUNDS` defaults to `5`. `HISTORY_MAX_MESSAGES` defaults to `47` and is a secondary
message-object ceiling applied at complete-round boundaries. A long active round may temporarily
exceed it because in-progress evidence is never removed. `0` disables the corresponding limit;
both must be `0` to disable trimming. Trimming does not summarize: old facts can remain
checkpointed while no longer being model-visible.

## Cache-prefix behavior

Inside a ReAct round, later requests append persisted assistant/tool messages to the earlier
request. The progress block is therefore cache-friendly: unlike the former transient tracker, it
does not appear in one request and disappear in the next.

Across rounds, the final answer and historical progress deltas remain available until their
completed round falls outside the trim window. Once trimming removes an old round, the exact prefix
necessarily diverges after the permanent system + message-0 anchor. That anchor contains the
dominant static context, so the most expensive prefix remains stable.

This structure maximizes cache eligibility but does not guarantee a provider cache hit. Operational
validation uses cached-input-token and latency telemetry.

## Presentation boundary

Internal progress blocks remain in checkpointed model context but are removed from CLI output and
API `token`/`done` output. The streaming filter recognizes tags across chunk boundaries and fails
closed on an unclosed block.

## Related

[[progress-ledger]] · [[langgraph-react-topology]] · [[api-funda-agent-exp]]
