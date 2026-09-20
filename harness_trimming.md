# Harness Routing Removal and Trimming Analysis

## Implementation status — 2026-08-17

The staged removal described in this document is now implemented in both
`funda_agent_exp.py` and `deployment/funda_agent_exp.py`.

- Stage 1 forced the existing strong GPT-5.5 model and full prompt on every LLM turn while
  leaving routing state dormant. All five gated live cases scored 5/5 for tool use and 5/5 for
  output quality, so Stage 2 proceeded.
- Stage 2 replaced the two model objects with canonical `MODEL_NAME` and
  `MODEL_REASONING_EFFORT` configuration, removed the lean-prompt runner path and all
  routing-only graph state, and simplified `call_tool()` to retain only tool execution,
  progress-ledger, stall, and zero-row behavior.
- The focused cleanup then removed `more_sql_expected` from the SQL tool schema and every active
  instruction/feedback path, and removed lean-prompt construction and export from both active
  `agent_instructions.py` copies. Multi-query planning remains represented by
  `information_still_needed`.
- `STRONG_MODEL_NAME` and `STRONG_REASONING_EFFORT` are accepted by the deployment runner only
  as temporary fallback environment variables. There is no weak-model fallback.
- Persona detection remains solely for attaching persona evidence. Active instruction modules
  now build and export only the complete schema-bearing prompt; historical `_old` files were
  intentionally left untouched.

Stage 2's five live runs used GPT-5.5 and the full prompt on every one of 12 LLM turns. Four
outputs scored 5/5; the broad free-text case scored 4/5 because one defensible mouthfeel coding
group included oiliness and therefore reported five texture-problem cues rather than the
reference's narrower count of four. Tool use scored 5/5 in all five cases. This is a stochastic
synthesis variation, not a Stage 2 routing difference: Stage 1 and Stage 2 have the same model,
prompt, tools, and reasoning setting at every LLM call.

The focused cleanup gate added persona and feature-influence cases to the original five. All
seven post-cleanup live runs used GPT-5.5 and the complete prompt on every turn. Tool use did not
regress; critically, the dependent free-text case still issued its second aggregate SQL query
after `more_sql_expected` was removed. See `routing_removal_results/summary.md`.

## Purpose

This document records the removed model/prompt routing harness, the implemented replacement and
the boundary used to preserve the existing last-K conversational memory and agent behavior.

The implemented architecture is:

```text
one model + one stable prompt + the existing tools + the existing last-K memory
```

The retained model is GPT-5.5 with low reasoning and the full prompt on every ordinary LLM turn.
The final step-budget turn uses the same model without tools bound.

## Removed Routing Protocol (historical)

Before this change, the implementation was not a strict pre-call router that classified each
requested tool and assigned it to a model. It was a state machine centered on `sql_done`.

Every new question, including a conversational follow-up, resets:

```text
number_of_steps = 0
sql_done = false
analysis_packet_done = false
feature_influence_done = false
```

The effective selection protocol is:

| State | Exception | Model | Prompt |
|---|---|---|---|
| First turn or `sql_done = false` | Any | Strong | Full |
| `sql_done = true` | None | Weak | Lean |
| `sql_done = true` | Persona request | Strong | Lean |
| `sql_done = true` | Completed analysis packet | Strong | Lean |
| `sql_done = true` | Completed feature-influence analysis | Strong | Lean |

On the final allowed LLM turn, the same selection is applied to the unbound model so that another tool call cannot be emitted.

### Important consequences

- The first turn always uses the strong model and full prompt, even when no SQL or other tool is needed.
- Before `sql_done` latches, the strong model can call any tool, not only `nl2sql_tool`.
- Both models have all tools bound. After latching, the weak model can issue another SQL call.
- If a weak-authored SQL call reports an error, zero rows, or another open gap, only the following turn is promoted back to strong+full. The SQL call itself was still authored by the weak model.
- `run_survey_stats` has no direct routing effect. It is normally strong before the latch and weak after it.
- A standalone successful analysis packet or feature-influence call can close retrieval while deliberately keeping strong+lean for synthesis.
- Persona requests keep the strong writer even after SQL closes.

For these reasons, “retrieval-completion routing” describes the harness more accurately than “SQL tool authorization.”

## How the SQL Latch Worked (historical)

Every `nl2sql_tool` call used to contain:

```text
information_gathered
information_still_needed
more_sql_expected
```

Those values did not control SQL execution. `call_tool()` read the original model-authored
arguments to maintain a progress ledger and update routing.

A SQL call or homogeneous parallel SQL batch latches `sql_done = true` only when:

1. The minimum latch step has been reached.
2. Every call explicitly reports `more_sql_expected is False`.
3. Every call supplies a non-empty closed `information_still_needed` value.
4. No recognized SQL/tool error occurred.
5. No SQL call returned zero rows.

Closed-gap wording includes leading forms such as `nothing`, `none`, `n/a`, `nil`, `no more`, `complete`, and `done`.

Missing or contradictory metadata leaves the strong+full route active. If the route is already latched, an error, open gap, or zero-row result reopens it.

This routing logic was intertwined with useful non-routing behavior:

- the cumulative gathered/needed progress ledger;
- unchanged-gap stall warnings;
- consecutive zero-row diagnostics;
- parallel SQL result merging;
- SQL error recovery.

The implementation preserved those protections while removing routing.

## Full and Lean Prompts (historical)

`SYSTEM_PROMPT_TEXT` was the full prompt. `SYSTEM_PROMPT_LEAN_TEXT` contained the same behavioral,
reporting, formatting, scope and tool-use instructions but omitted the DB schema appended to the
end of the full prompt.

The old code asserted that lean was a literal text prefix of full. This minimized textual
divergence, but was not proof of a cache hit. API prompt caching depends on the complete rendered
prefix, including:

- model;
- message roles and boundaries;
- prompt content;
- tool definitions and their order;
- structured-output configuration;
- relevant request settings.

The former normal path was:

```text
Turn 1: strong model + full prompt
Turn 2: weak model   + lean prompt
```

The strong/full request did not provide a useful direct cache handoff to a different weak model.
In practice, the former architecture formed separate effective cache lineages:

```text
strong/full requests
weak/lean requests
strong/lean exception requests
```

The lean route saved schema input on weak synthesis turns and could develop its own cache across
repeated weak/lean traffic. Its primary benefit was input reduction, not reuse of the immediately
preceding strong/full request.

## Cache Shape With One Model and One Prompt

With one stable model, full prompt, and tool list, a multi-step round becomes append-only:

```text
Request 1:
FULL → User question

Request 2:
FULL → User question → Assistant tool call → Tool result

Request 3:
FULL → User question → Assistant tool call → Tool result → next tool call/result
```

Each later request preserves the earlier request as an exact prefix and appends new messages. This is the favorable shape for prompt caching.

Expected benefits are:

- better consecutive-turn cache continuity;
- one cache lineage instead of model/prompt forks;
- more predictable cache-hit behavior;
- simpler control flow;
- elimination of latch and promotion edge cases.

This does not automatically guarantee lower total cost or latency. A single full-prompt strong-model route also means:

- the schema remains in every request, even during final synthesis;
- the stronger model writes every answer;
- cached input still exists in the request and counts toward rate limits;
- actual cache reuse remains dependent on cache availability and exact rendered-prefix equality.

Therefore, cache topology and overall economics must be measured separately.

## Interaction With the Current Last-K Memory

The last-K memory system is independent of model and prompt routing.

`_trim_history()` currently preserves:

1. Message zero, which contains the initial scope and pre-fetched inventory.
2. The last `HISTORY_MAX_MESSAGES` messages, default 10.
3. The complete active question round, even when it exceeds K.
4. The assistant tool-call owner of any retained `ToolMessage`, preventing invalid orphan tool results.

The checkpointer retains the complete thread state. Trimming is applied only when assembling the next model request; it does not destructively delete checkpointed messages.

Removing model/prompt routing does not require changing any of this behavior.

### Next-round caching when trimming does not activate

Suppose the final input used to generate round one's answer is:

```text
FULL → Question 1 → Tool call → Tool result
```

The first request of round two becomes:

```text
FULL → Question 1 → Tool call → Tool result → Answer 1 → Question 2
```

Most of round one is an exact reusable prefix. `Answer 1` is new input because it was output—not input—on the preceding request. `Question 2` is also new.

For ordinary rounds that remain within the history window, the expectation that round two ingests mostly cached round-one history is correct.

### Next-round caching when last-K trimming activates

Prompt caching is prefix-based, not a cache of independent messages. If the prior request was:

```text
FULL → M0 → M1 → M2 → M3 → M4 → M5
```

and trimming assembles:

```text
FULL → M0 → M3 → M4 → M5 → new question
```

then `M3–M5` are not reusable in their previous cached positions. Removing `M1–M2` changes the prefix immediately after `M0`. Usually only the stable full prompt plus `M0` remains an exact matching prefix; the retained suffix must be processed again.

Consequently:

| Round-one history at next-round assembly | Expected cache reuse |
|---|---|
| No trimming | Excellent: most prior input is reusable |
| Middle history removed by last-K | Partial: stable prompt and message zero remain reusable |
| Model, prompt, tools, or relevant settings change | Additional cache fragmentation |

One model/prompt improves caching around the last-K system, but it does not change this fundamental trimming boundary.

## Implemented Safe Removal Boundary

The implementation removed routing decisions while preserving adjacent behavioral controls.

### Removed or simplified

- `llm_strong`/`llm_weak` selection in favor of one LLM.
- `model`/`weak_model` selection in favor of one tool-bound model.
- Full/lean prompt selection in favor of one stable prompt.
- `sql_done` latch and promotion logic.
- `analysis_packet_done` and `feature_influence_done` model-selection overrides.
- Persona-based model-selection override.
- Per-question initialization fields that become unused after the above removal.

Persona detection and persona-inventory attachment were preserved; only their model-selection
role was removed.

### Preserved

- Tool definitions and ordering.
- Tool execution, batching, and result ordering.
- `information_gathered` and `information_still_needed` ledger behavior.
- Progress-tracker message injection.
- Stall warnings.
- Zero-row streak tracking and join-diagnostic guidance.
- SQL validation, scoping, authorization, and repair.
- `_trim_history()`.
- `HISTORY_MAX_MESSAGES` configuration.
- Message-zero scope/inventory anchor.
- `InMemorySaver`, thread IDs, and follow-up history.
- Per-question step reset and `MAX_LLM_STEPS`.
- The final unbound-model turn and step-budget notice.
- Persona, analysis-packet, feature-influence, and statistics functionality.

### `more_sql_expected` cleanup — completed

Stage 2 first stopped consuming `more_sql_expected`, then the focused gate verified its safe
removal from the tool signature, tool description, zero-row feedback, system instructions and
tests. The planning contract is now minimal:

```text
information_gathered
information_still_needed
```

The gathered/needed ledger, progress-tracker injection, stall warning and zero-row diagnostics
remain. Dependent-query guidance now tells the model to record the next query explicitly in
`information_still_needed`; the live row-27 gate confirmed that the second query still occurs.

## Implemented Simplified Model Node (design sketch)

The implemented model node follows this shape:

```python
def call_model(state, config):
    messages = [SYSTEM_PROMPT] + _trim_history(state["messages"])

    if progress_ledger_is_available(state):
        messages.append(build_progress_tracker(state))

    if state["number_of_steps"] >= MAX_LLM_STEPS - 1:
        active_model = llm            # same model, tools unbound
        messages.append(STEP_BUDGET_NOTICE)
    else:
        active_model = model          # same model, tools bound

    response = active_model.invoke(messages, config=config)
    return {
        "messages": [response],
        "number_of_steps": state["number_of_steps"] + 1,
    }
```

This was the design sketch used for the implemented node. The active implementation also covers
the CLI, API, deployment mirror and focused tests.

## Required Verification

Release-level follow-up testing can extend the completed focused gate to identical traces covering:

- direct answers requiring no tool;
- a single successful SQL call;
- multiple dependent SQL calls;
- parallel SQL calls;
- SQL syntax/error recovery;
- zero-row recovery and repeated-zero diagnostics;
- foreign-survey terminal refusal;
- statistics calls before and after SQL;
- analysis packet success and failure;
- feature-influence success and failure;
- persona requests;
- final step-budget behavior;
- short follow-ups that do not trigger trimming;
- long follow-ups that trigger last-K trimming;
- resumed thread behavior through the API.

Measure, per request and per complete run:

```text
model
prompt variant
input tokens
cached input tokens
uncached input tokens
output tokens
time to first token
total LLM latency
total agent latency
estimated cost
behavioral score
```

The success condition is not merely a higher cache-hit percentage. It is no material behavioral regression with an acceptable combined latency and cost profile.

## Implemented Conclusion

One model and one stable prompt now provide a single cache topology within a question and across
untrimmed conversational rounds. The existing last-K memory system was not changed.

The progress ledger, SQL repair loop, persona inventory, specialized analysis tools, parallel
tool ordering and final-step protection were preserved. Active prompt/tool contracts were
simplified; only historical `_old` sources retain the removed contracts.
The main cache limitation that remains is prefix-breaking last-K trimming once completed-round
messages are removed from the middle of the assembled history.
