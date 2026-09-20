---
type: concept
title: "LangGraph ReAct topology and graph state"
tags: [agent-variant]
sources: [funda-agent-exp-architecture-doc]
created: 2026-08-14
updated: 2026-08-14
---

The control-flow skeleton of [[funda-agent-exp]]: a two-node LangGraph cycle with a hard turn budget. Its notable property is how little it contains — the sophistication lives in routing ([[model-and-prompt-routing]]) and in the tool layer, not in the graph shape.

## The graph

```text
                     tool calls present
      ┌────────────┐ ───────────────────► ┌────────────┐
START │    LLM     │                      │   tools    │
 ───► │ call_model │ ◄─────────────────── │ call_tool  │
      └──────┬─────┘      unconditional   └────────────┘
             │
             │ no tool calls, or step budget exhausted
             ▼
            END
```

`build_graph()` defines exactly two nodes — `LLM` → `call_model` and `tools` → `call_tool` — with `LLM` as the entry point. `should_continue()` routes to `tools` when the LLM response contains tool calls and the turn budget is not exhausted. The tool node returns to the LLM node unconditionally.

Termination is decided by one condition: the most recent AI message has no tool calls. This is independent of the `sql_done` latch, which is a routing signal and explicitly **not** an end-of-graph signal.

## The step budget

`MAX_LLM_STEPS = 12` counts **LLM invocations** — not graph nodes and not individual tool calls. The CLI sets `recursion_limit = 2 * MAX_LLM_STEPS + 2`, leaving LangGraph enough headroom for twelve full LLM/tool cycles.

On the last permitted turn the agent does something deliberate: **tools are unbound**, and a step-budget message instructs the model to answer from the evidence it already has and to disclose what remains unresolved. The budget therefore degrades into a forced, honest summary rather than a truncation or an error.

## Graph state

`AgentState` is a `TypedDict`:

| Field | Reducer | Meaning |
|---|---|---|
| `messages` | `add_messages` | Accumulated human, AI and tool messages |
| `thread_survey_id` | replace | Survey anchor used by the API to reject cross-survey thread reuse |
| `inventory_attached` | replace | Whether the thread's first message carries an inventory |
| `inventory_chars` | replace | Size of that initial inventory |
| `number_of_steps` | replace | LLM turns used by the current question |
| `last_sql_observations` | replace | Canonical SQL/result fingerprints from the previous SQL batch |
| `zero_row_streak` | replace | Consecutive SQL attempts returning zero rows |
| `word_cloud_artifact` / `word_cloud_intro` | replace | Trusted terminal word-cloud output |

Only `messages` accumulates through a reducer; everything else is replaced.

`_run_round()` resets every per-question scratch field before each new question, **including a follow-up**. Without that overwrite a resumed thread would inherit the previous question's exhausted step count and SQL observations.

`thread_survey_id`, `inventory_attached` and `inventory_chars` are checkpoint metadata rather than routing inputs: the two-node graph never routes on them, but [[api-funda-agent-exp]] reads them before starting a follow-up.

## Node-level effects

`call_model()` changes only two things — it appends one AI response and increments `number_of_steps`.

`call_tool()` appends one `ToolMessage` per call, recomputes `zero_row_streak`, stores the latest SQL fingerprints, and may retain a trusted word-cloud artifact outside model-authored text. Progress deltas are emitted by the next `AIMessage`, so they remain part of ordinary append-only message history.

The tool functions themselves return **strings** and mutate no graph state. `call_tool()` interprets their calls and results and returns the state update — keeping state transitions in one place rather than distributed across tool implementations.

## Per-question start conditions

Every question begins from:

```text
number_of_steps = 0
last_sql_observations = {}
zero_row_streak = 0
zero_row_streak = 0
persona_request = route detector result
```

A consequence worth noting: because no latch is set, the **first decision of every question always uses the strong model with the full schema prompt**. If the startup inventory ([[survey-inventory-packet]]) already answers the question, the model can respond immediately and the graph ends with no tool call at all.

## Related

[[funda-agent-exp]] · [[model-and-prompt-routing]] · [[progress-ledger]] · [[parallel-tool-calling]] · [[conversation-memory-and-trimming]]
