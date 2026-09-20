# `funda_agent_exp.py` — current architecture and execution flow

Reference documentation for the FlavorAI LangGraph survey-analysis agent in this directory.
This document describes the current `final_agent_work_v5_base_2_optim3` implementation, not
earlier result-parking or drilling-agent experiments.

**Last source audit:** 2026-08-17  
**Primary implementation:** [`../funda_agent_exp.py`](../funda_agent_exp.py)

---

## 1. Purpose and boundaries

The agent answers natural-language questions about a FlavorWiki survey. It can:

- inspect survey data through read-only PostgreSQL queries;
- reuse a precomputed inventory of products and answered measures;
- retrieve the same inventory for an authorized historical or benchmark survey;
- call the Charts API for ANOVA, Tukey, Pearson, Spearman and chi-square results;
- build an evidence-backed persona/profile from the current survey's categorical responses;
- compare the current survey with an authorized sibling survey or fixed benchmark; and
- retain short-term conversation history when a checkpointed graph and repeated `thread_id`
  are used.

The agent is a guarded ReAct loop. The model decides what the question means, which measure is
semantically appropriate, what evidence is missing and how to explain the result. Python code
deterministically controls scope checks, SQL safety, tool execution, state updates and
termination.

The implementation does **not** write survey data. Its SQL transaction is read-only. The only
mutable runtime data is graph state, an in-memory checkpoint, process-local caches, the shared
SQLAlchemy engine, and the process-global run scope.

## 2. Source layout

| File | Responsibility |
|---|---|
| [`../funda_agent_exp.py`](../funda_agent_exp.py) | Models, tools, inventory query/cache, graph, state and CLI |
| [`../agent_instructions.py`](../agent_instructions.py) | Persona, analytical policy, schema map, the complete system prompt, scope preambles and progress messages |
| [`../tool_prompts.py`](../tool_prompts.py) | Tool descriptions and deterministic feedback/error text shown to the model |
| [`../api_funda_agent_exp.py`](../api_funda_agent_exp.py) | FastAPI/NDJSON wrapper, API scope resolution and checkpoint lifecycle |
| [`../tests/test_agent_optimizations.py`](../tests/test_agent_optimizations.py) | Focused tests for inventory, cache, single-model/full-prompt behavior, memory, scope and API behavior |

`agent_instructions.py` contains Python constants named `AGENTS_YAML` and `TASKS_YAML`; the
current agent does not load external `agents.yaml`, `tasks.yaml` or `schema_overview.md` files at
runtime. The schema and model-facing policies are embedded in that Python module.

## 3. Quick start

From this directory:

```bash
# Use the default prompt and default scope constants.
../BE/.venv/bin/python funda_agent_exp.py

# Ask one scoped question.
../BE/.venv/bin/python funda_agent_exp.py \
  --client-id 37cbf852-a2b7-4f8b-96b4-7f67432f88cd \
  --org-id c75d846a-e265-4f69-92c1-91308e0697f6 \
  --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672 \
  --prompt "Sort the products by overall liking."

# Force the model to discover the current survey through SQL.
../BE/.venv/bin/python funda_agent_exp.py --no-inventory --prompt "..."

# Continue with follow-up questions on one checkpointed thread.
../BE/.venv/bin/python funda_agent_exp.py \
  --chat --thread-id session-1 --prompt "Which product leads on Aroma?"
```

HTTP wrapper:

```bash
uvicorn api_funda_agent_exp:app --host 0.0.0.0 --port 8000

curl -N -X POST localhost:8000/ask \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "What products were tested?",
    "survey_id": "39af3240-42a8-4e35-8c7d-c61703d5ce3f"
  }'
```

`dotenv.load_dotenv()` runs during import. Configure at least `OPENAI_API_KEY` and
`DATABASE_URL`; configure the Charts API host and secret when statistical tools are required.
The source currently contains fallback credentials. See §16 before deploying it.

## 4. Runtime construction

Module import performs the following setup:

1. Loads environment variables.
2. Creates lazy process-wide SQLAlchemy engine state (`_ENGINE` starts as `None`).
3. Wraps the three Python tool functions as LangChain tools.
4. Creates one `ChatOpenAI` instance.
5. Binds all tools to that model with `parallel_tool_calls=True`.
6. Creates a process-wide SQL thread pool.
7. Builds one complete `SystemMessage`, including the database schema.

The CLI `main()` then:

1. Parses the prompt, scope, inventory, chat and thread options.
2. Clears and publishes the authoritative `_SCOPE` dictionary.
3. Compiles the graph with an `InMemorySaver`.
4. Creates the scoped first user message.
5. Pre-fetches and appends the current-survey inventory unless `--no-inventory` was used.
6. For a persona/profile prompt, additionally fetches the on-demand persona inventory.
7. Streams one graph round.
8. With `--chat`, repeats graph rounds using the same checkpoint and `thread_id`; a later
   persona follow-up receives the persona inventory in that follow-up message.

The SQL engine is created on first use without a checkout health ping, then reused for inventory,
packet ownership checks and model-authored SQL.

## 5. LangGraph topology

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

`build_graph()` contains only two nodes:

- `LLM` → `call_model`
- `tools` → `call_tool`

The entry point is `LLM`. `should_continue()` routes an LLM response to `tools` when it contains
tool calls and the LLM-turn budget has not been exhausted. The tool node always returns to the
LLM node.

`MAX_LLM_STEPS = 12` counts LLM invocations, not graph nodes or individual tool calls. The CLI
sets `recursion_limit = 2 * MAX_LLM_STEPS + 2`, leaving enough LangGraph headroom for twelve
LLM/tool cycles.

On the last permitted LLM turn, tools are deliberately unbound and a step-budget message tells
the model to answer from the evidence already retrieved and disclose unresolved parts.

## 6. Graph state

`AgentState` is a `TypedDict`:

| Field | Reducer | Meaning |
|---|---|---|
| `messages` | `add_messages` | Accumulated human, AI and tool messages |
| `thread_survey_id` | replace | Survey anchor used by the API to reject cross-survey thread reuse |
| `inventory_attached` | replace | Whether the thread's first message contains an inventory |
| `inventory_chars` | replace | Size of that initial inventory |
| `number_of_steps` | replace | LLM turns used by the current question |
| `last_sql_observations` | replace | Canonical SQL/result fingerprints from the previous SQL batch |
| `zero_row_streak` | replace | Consecutive SQL retrieval attempts for which every query returned zero rows |
| `word_cloud_artifact` | replace | Trusted fenced chart produced by `generate_word_cloud` for this question |
| `word_cloud_intro` | replace | Deterministic fallback sentence naming the selected question and non-blank N |

Only `messages` accumulates through a reducer. `_run_round()` resets all per-question scratch
fields before every new question, including a follow-up. Without this overwrite, a resumed
thread would inherit the previous question's exhausted step count and SQL diagnostics.

`thread_survey_id`, `inventory_attached` and `inventory_chars` are checkpoint metadata. The core
two-node graph does not route on them; the API wrapper reads them before starting a follow-up.

### 6.1 Node-level state changes

`call_model()` changes only:

```text
messages        += one AI response
number_of_steps += 1
```

`call_tool()` can change:

```text
messages             += one ToolMessage per call
zero_row_streak       = recomputed for the SQL batch
last_sql_observations = canonical SQL/result fingerprints for the latest SQL batch
word_cloud_artifact   = exact successful word-cloud tool artifact, when present
word_cloud_intro      = deterministic fallback introduction, when present
```

The tool functions themselves return strings. They do not directly mutate graph state;
`call_tool()` interprets their calls/results and returns the state update.

## 7. Per-question execution flow

### 7.1 First turn

Every question starts with:

```text
number_of_steps = 0
last_sql_observations = {}
zero_row_streak = 0
word-cloud scratch = empty
```

Every decision uses the same GPT-5.5 model and complete schema-bearing system prompt. If the
startup inventory already answers the question, the model can respond immediately and the graph
ends without a tool call.

Persona/profile turns use the same model and prompt as every other turn. Their preferred headings,
tables and per-row source/N/coverage contract remain prompt-guided rather than deterministically
enforced: the first final draft is returned without a hidden format-repair model call.

### 7.2 Tool selection

The model can choose:

- `nl2sql_tool` for counts, distributions, metadata or aggregates absent from a packet;
- `get_survey_analysis_packet` after resolving an authorized historical survey, or directly for
  the configured benchmark; or
- `generate_word_cloud` for deterministic word-frequency artifacts from an open-ended question;
- `run_survey_stats` for significance tests, correlations and categorical association.

Persona retrieval is not a model tool. A persona-like current-survey prompt is detected
before the graph runs and receives a cached demographic/behavior candidate packet keyed by
`survey_id`. If `--no-inventory` is active or the packet cannot be built completely, the model
must use scoped SQL or explain that the evidence is insufficient.

The model may issue several independent SQL calls in one response. Dependent calls must remain
sequential: if query B needs an ID returned by query A, query A completes in the current cycle
and query B is authored on the next LLM turn.

### 7.3 Tool execution and progress update

`call_tool()` dispatches calls by name and converts every result into a `ToolMessage` paired with
the original `tool_call_id`.

For SQL calls it also updates the zero-row streak, fingerprints each canonical query/result pair,
warns on an exact repeat from the previous SQL batch or current parallel batch, and after two
all-empty SQL batches instructs the model to diagnose both join sides and shared keys.

If the next assistant response continues with tools, it persists one internal
`<progress_gathered>` block in the same `AIMessage` as those tool calls. This is an append-only
delta over the immediately preceding batch, including the union of useful parallel-tool evidence.
Raw `ToolMessage` results remain authoritative. No transient tracker message is appended, so the
next model request extends the prior request as an exact prefix until history trimming activates.

### 7.4 Continue or finish

After tool results, the graph returns to `call_model()`. The model can issue another tool call or
write the final response. `should_continue()` ends when the most recent AI message has no tool
calls or the LLM-turn budget is exhausted.

## 8. Single model and complete prompt

Every ordinary decision and synthesis turn uses one temperature-zero `ChatOpenAI` instance:

| Setting | Value |
|---|---|
| Model | `MODEL_NAME` (`gpt-5.5` in the development runner) |
| Reasoning | `MODEL_REASONING_EFFORT` (`low` by default) |
| System prompt | `SYSTEM_PROMPT_TEXT`, always including the full schema |
| Tool availability | All tools bound, except on the final step-budget turn |

There is no strong/weak model selector, lean prompt, SQL-completion latch, packet-completion
override or persona-based model override. Tool choice changes neither
the next model nor the next prompt. On the twelfth and final permitted LLM turn, `call_model()`
uses the same underlying model without tools bound and appends the step-budget notice so the turn
must synthesize from evidence already collected.

## 9. Parallel tool calling

The agent has parallel tool-call capability at two layers:

1. The single tool-bound model sets `parallel_tool_calls=True`, allowing one AI response to contain several
   tool calls.
2. `call_tool()` executes a homogeneous batch concurrently when it contains at least two calls
   and every call is either `nl2sql_tool` or `run_survey_stats`.

Execution uses a process-wide `ThreadPoolExecutor` with
`MAX_PARALLEL_TOOL_CALLS` workers (defaulting to `MAX_PARALLEL_SQL_CALLS`, whose default is 6).
Each SQL call uses its own pooled connection.

Results are consumed in original call order, not completion order. This preserves deterministic
message order and a stable model-facing history.

Mixed batches and packet batches run sequentially. A homogeneous
statistics batch may run concurrently; SQL + statistics, SQL + packet and multiple packet calls
do not, even if the provider emitted them in one assistant message.

Parallel SQL calls should describe the same batch-level completion state. The ledger merger
keeps distinct facts/gaps conservatively if the calls disagree.

## 10. Tools

`build_tools()` exposes exactly five tools. There is no general result store, result parking,
`describe_result`, `query_result`, `slice_result`, digest workflow or drilling sub-agent in the
current implementation.

### 10.1 `nl2sql_tool`

Signature:

```python
nl2sql_tool(sql_query: str) -> str
```

Execution order:

1. Repair near-miss literals that are within four edits of an authoritative scope ID. Repairs
   are disclosed in the returned tool text.
2. Add an advisory warning when a query contains no scope placeholder, scope literal or UUID.
3. Reject semicolon-separated batches.
4. Reject statements whose leading operation is not `SELECT` or `WITH`.
5. Reject semantic pattern matching on question/option meaning columns such as `prompt`,
   `promptHtml`, `label`, `labelHtml`, `internal_name` and `optionDefinition`.
6. Bind the current `_SCOPE` dictionary as SQLAlchemy parameters.
7. Execute the statement inside a read-only transaction with the statement timeout.
8. Fetch all rows and return a JSON array; return structured feedback for zero rows or failure.

Important distinctions:

- The no-scope detector is advisory. It warns the model but does not block execution.
- Exact equality on a full prompt is permitted after the model has inspected candidates.
- Structural filters are permitted; semantic keyword filtering is not.
- PostgreSQL enforces read-only execution even if the model attempts mutation.
- `STATEMENT_TIMEOUT_MS` defaults to 20,000 ms.
- Query failures are returned to the model as strings rather than raised through the graph.

The tool currently uses `fetchall()` and serializes the complete result. Although its model-facing
description still mentions a stored-result manifest for oversized output, no such storage path is
wired into this implementation. See §16.

### 10.2 `get_survey_analysis_packet`

Input: one already-resolved survey UUID.

The tool:

1. Validates UUID syntax.
2. Requires a published run scope.
3. Resolves the survey and checks the authorization envelope server-side.
4. Refuses an unknown or out-of-scope survey.
5. Retrieves the shared cached packet payload.
6. Returns a recognizable success header plus the packet, or explicit fallback feedback.

Authorization fails closed if the ownership lookup cannot run.

Allowed packet targets are:

- the current survey;
- another survey in the current organization; or
- the configured fixed benchmark survey.

### 10.3 `generate_word_cloud`

Inputs are one inventory-resolved open-ended question UUID, `group_by_product` (default false),
and `max_terms` from 30 through 60 (default 50). The tool scope-checks the question, accepts only
`open-answer` and `multiple-open-answer`, retrieves raw text internally, redacts email/URL material,
tokenizes Unicode letters, applies fixed filtering, and counts each term at most once per answer.
Raw answers are never returned to the model.

The tool serializes the `gpi-chart` `word_cloud` version-1 payload with deterministic tie ordering.
`call_tool()` removes that payload from the model-visible tool result and stores it in
`word_cloud_artifact`. `call_model()` then returns the tool-owned deterministic introduction and
trusted artifact directly, without another model invocation. A model-authored fenced block without
a successful tool artifact still fails closed and is withheld.

### 10.4 `run_survey_stats`

Inputs include:

```text
question_id
stats_types
alpha_value = 0.05
reference_question_id = ""
penalty_level = ""
boxing_strategy = ""
```

The tool validates UUIDs and requires a reference question for Pearson, Spearman and penalty
requests. When scope is published, it resolves question ownership and enforces the same tenancy
envelope as the packet tool. Authorization lookup failures fail closed.

For reference-based tests, both questions must belong to the same survey. Cross-survey
correlation is rejected because enrollments do not provide a usable cross-survey respondent key.

After authorization, the tool performs an HTTP GET to:

```text
{CHARTS_API_BASE_URL}/chartingAPI/charts/public/stats/{question_id}
```

with a 30-second client timeout and secret header. It formats ANOVA, Tukey, correlation and
chi-square output into model-readable text. When a successful result belongs to a historical or
benchmark survey, a provenance notice is prepended so the final answer attributes it correctly.

## 11. One-statement survey inventory

Before the first LLM call, `survey_inventory(survey_id)` normally builds a complete analytical
packet with one PostgreSQL statement, `_INV_SQL`.

### 11.1 Packet shape

```json
{
  "products": [],
  "scored_measures_by_product": [],
  "other_answered_measures": []
}
```

`products` contains product names and blinding numbers.

`scored_measures_by_product` contains numeric, product-attributable measures with:

- question ID, full prompt and question type;
- scale-point count and observed score range;
- per-product value count, respondent count, mean and sample SD;
- pooled-attribute detection;
- per-attribute results when a question contains multiple attributes; and
- `order_differs`, listing attributes whose product order differs from the pooled order.

`other_answered_measures` keeps every remaining answered question, including open-text,
ranking, matrix and non-product-linked measures. It reports:

- `answers`: expanded option/value-row count after the left join;
- `submissions`: distinct `answer.id` count, used for the number of submitted answers;
- `respondents`: distinct enrollment count;
- product linkage; and
- a sample of stored option labels.

The separate `submissions` field prevents multi-option questions from being reported as if every
option/value row were a separate submitted answer.

### 11.2 Query structure

`scored AS MATERIALIZED` is shared by the scored summary and the exclusion used to build the
other-measure summary. Products, survey lifecycle metadata and both summaries return from the
same SQL statement, giving the packet one statement-level database snapshot and one driver round
trip.

The top-level object is rebuilt in Python after reading PostgreSQL JSONB so serialized key order
remains deterministic and startup/on-demand packet payloads are byte-compatible.

### 11.3 Fail-soft behavior

The inventory returns an empty string when:

- the survey is unknown;
- the database query fails;
- neither answered-measure section contains data; or
- the complete JSON payload exceeds `INVENTORY_MAX_CHARS`.

A survey containing products but no answered measures is deliberately not considered an
analysis packet. The agent falls back to scoped SQL instead of treating a products-only packet as
complete analytical evidence.

Packets are never truncated. A partial candidate list could silently change semantic measure
selection, so oversized packets fall back to SQL discovery.

### 11.4 Cache

The serialized payload is cached in a process-local, lock-protected `OrderedDict` keyed by survey
ID. It is a bounded LRU with lifecycle-sensitive TTLs:

| Survey state | Default TTL |
|---|---:|
| Active/mutable survey | 300 seconds |
| Inactive `closed` or `archived` survey | 86,400 seconds |
| Fixed benchmark survey | 86,400 seconds |

The default maximum is 128 entries. Errors, missing/empty packets and oversized packets are not
cached. Workers do not share cache entries.

### 11.5 Persona inventory

`survey_persona_inventory(survey_id)` is a separate, on-demand one-statement packet. It is
attached only when `is_persona_request()` recognizes persona/profile language or respondent-level
demographics, motivations, loyalty, behavior and attitude requests, so ordinary analytical and
product-specific motivation questions do not pay its database or context cost.

The query returns every answered, non-product `multiple-choice` question with its full prompt;
it deliberately does not classify demographics by SQL keywords. Python post-processing adds:

- survey enrollment and answered-respondent totals;
- question respondent denominator and survey coverage percentage;
- respondent count and percentage for every category;
- a Wilson 95% confidence interval for each category rate;
- observed single-select versus multi-select mode; and
- for single-select questions, a top-versus-runner-up equal-share test with a Bonferroni-adjusted
  p-value across the question's alternative categories.

`dominance.significant=true` is the deterministic gate for describing a category as dominant or
representative. A merely largest bucket is reported as mixed/not clearly separated. Multi-select
questions receive no dominance verdict because their category selections overlap.

This is a complete-or-absent packet with its own process-local bounded LRU, using the same
lifecycle TTL policy as the general inventory. Its default size ceiling is 120,000 characters.

## 12. Prompt stack and decision ownership

### 12.1 Complete system prompt

`agent_instructions.py` builds one stable `SYSTEM_PROMPT_TEXT` at import time. It contains the
persona, scope policy, tool choice, statistical/reporting rules, PostgreSQL dialect guidance and
the complete schema map. There is no lean prompt export or runtime prompt selector. Per-run IDs
are placed in the user message rather than interpolated into the system prompt, keeping the
system prefix stable across surveys and LLM turns.

The first user message contains:

```text
SCOPE
QUESTION
PRE-FETCHED INVENTORY (when available)
PRE-FETCHED PERSONA INVENTORY (only for a persona/profile request)
```

Ordinary follow-up messages contain only the follow-up text because the first message remains the
conversation anchor. A follow-up that newly needs persona evidence also contains the cached
persona packet, keeping that evidence inside the current round retained by history trimming.

### 12.2 Model decisions

The LLM is responsible for:

- interpreting user intent;
- selecting a measure by meaning from complete prompt text;
- checking construct, scale and attribution compatibility across surveys;
- deciding which tool and query shape can answer the question;
- emitting concise sourced progress deltas when another tool call follows;
- choosing an appropriate statistical test; and
- producing the final explanation, caveats and recommendation; and
- semantically identifying which persona-packet prompts genuinely measure demographics,
  behavior, attitudes or preferences.

Persona instructions prohibit filling absent fields, inferring motivations or socioeconomic
traits from stereotypes, or combining separate marginal leaders as though they describe the same
respondents. A multi-trait persona must either retrieve a respondent-level intersection/cross-tab
or explicitly identify itself as a marginal composite.

### 12.3 Deterministic decisions

Python is responsible for:

- binding and repairing scope IDs;
- packet authorization;
- SQL validation, read-only mode and timeout;
- concurrent versus sequential execution;
- merging progress metadata;
- stall and zero-row diagnostics;
- checkpoint updates; and
- the hard LLM-turn limit.

This separation prevents the model from overriding security boundaries or database mutation
rules while preserving semantic judgment where deterministic keyword logic would hide evidence.

## 13. Conversation memory

`build_graph(checkpointer=None)` remains usable without memory for benchmark harnesses. The CLI
passes an `InMemorySaver`; the API owns one process-wide `InMemorySaver`.

### 13.1 Checkpoint semantics

The same `thread_id` resumes accumulated `messages`. Each new question resets per-question
scratch state. Checkpoints are process-local and do not survive restart.

### 13.2 Request-history trimming

`_trim_history()` changes only the messages sent to the model. It does not delete checkpointed
history or summarize data.

When trimming is needed, it preserves:

1. Message 0, containing the authoritative scope and initial inventory.
2. The current question and its complete in-progress tool exchange.
3. Up to `HISTORY_MAX_ROUNDS` recent user rounds.
4. A secondary `HISTORY_MAX_MESSAGES` ceiling, enforced only at complete-round boundaries.

The boundary is selected from genuine `HumanMessage` round openers and remains fixed throughout
the active ReAct round. It therefore cannot orphan a tool result or shift the cache prefix while
assistant/tool messages are being appended. A long active round may temporarily exceed the
message ceiling; it is never cut in progress.

`HISTORY_MAX_ROUNDS=0` disables the round cap, and `HISTORY_MAX_MESSAGES=0` disables the secondary
message ceiling. Setting both to `0` disables trimming.

### 13.3 Limitations

Because trimming is positional and does not summarize, an old fact can fall outside the model's
visible window even though it remains checkpointed. `InMemorySaver` also snapshots the growing
state and has no built-in retention policy; long-lived services should delete inactive threads
or use a persistent checkpointer with explicit retention.

## 14. HTTP wrapper

`api_funda_agent_exp.py` provides:

- `POST /ask`
- `GET /health`
- `DELETE /threads/{thread_id}`

### 14.1 Scope resolution

The caller must provide `survey_id`. The API attempts to derive:

```text
survey.organization_id
    → organization.account_id
    → account.client_id
```

Explicit `client_id` and `organization_id` override that lookup. If the chain is unavailable,
the API publishes survey-only scope and uses a special prompt that forbids unbound client/org
placeholders. Historical sibling lookup is then unavailable, but the fixed benchmark remains
available.

Before every request, `_SCOPE` is cleared and rebuilt so a previous tenant value cannot survive.

### 14.2 Thread rules

For a new thread, the API adds scope and optionally inventory to message 0. For a follow-up, it
reuses the original anchor and does not re-fetch or duplicate the inventory. A thread created
without inventory remains inventory-free.

The API rejects a repeated `thread_id` when its stored `thread_survey_id` differs from the new
request's survey. Deleting the thread or choosing a new ID is required to change surveys.

### 14.3 Serialization and streaming

The API sets the model to streaming and emits newline-delimited JSON. Event types are:

| Event | Meaning |
|---|---|
| `status` | Scope, inventory and running-stage metadata |
| `token` | Candidate AI text fragment |
| `reset` | The streamed AI turn issued tools; discard its candidate text |
| `tool_result` | Tool name and output character count |
| `done` | Final answer and thread ID |
| `error` | Terminal in-stream failure |

Only `AIMessageChunk` content is emitted as tokens; `ToolMessage` content is not streamed to the
client. Persona and ordinary analytical responses both stream token-by-token. Persona presentation
remains best-effort under the prompt contract and is not buffered for validation or repair. A
client that only needs the final response can ignore everything except `done.answer`.

### 14.4 Concurrency

The agent's `_SCOPE` is a process global. The API therefore serializes complete runs with one
`asyncio.Lock`; concurrent requests queue instead of interleaving tenant state. Multiple Uvicorn
worker processes can provide process-level concurrency, but each worker has its own checkpoint,
scope cache and inventory cache, so a follow-up must reach the same worker unless shared state is
introduced.

True concurrent runs inside one process require moving `_SCOPE` into request/graph state and
passing scope explicitly to every tool.

## 15. Configuration reference

| Setting | Default | Effect |
|---|---:|---|
| `OPENAI_API_KEY` | source fallback | OpenAI authentication |
| `DATABASE_URL` | local Postgres URL | SQLAlchemy connection URL |
| `CHARTS_API_BASE_URL` | production Charts host | Statistical API base URL |
| `CHARTS_STATS_EXTERNAL_ACCESS_SECRET` | source fallback | Charts API header secret |
| `MODEL_NAME` | `gpt-5.5` in deployment | Canonical deployment model setting; the development runner fixes this value in source |
| `STRONG_MODEL_NAME` | unset | One-cycle deployment fallback used only when `MODEL_NAME` is absent |
| `MODEL_REASONING_EFFORT` | `low` | Canonical reasoning effort; `off`/`default` omit it |
| `STRONG_REASONING_EFFORT` | unset | One-cycle fallback used only when `MODEL_REASONING_EFFORT` is absent |
| `OPENAI_REASONING_EFFORT` | unset | Older shared fallback used only when neither canonical nor strong fallback is set |
| `OPENAI_VERBOSITY` | unset | Passed to `ChatOpenAI` when provided |
| `MAX_LLM_STEPS` | 12 | Hard LLM-turn budget; source constant |
| `STATEMENT_TIMEOUT_MS` | 20,000 | PostgreSQL planner/execution timeout; source constant |
| `HISTORY_MAX_ROUNDS` | 5 | Recent user rounds retained in addition to permanent message 0 |
| `HISTORY_MAX_MESSAGES` | 47 | Secondary message-object ceiling, applied at round boundaries |
| `PROGRESS_DELTA_MAX_CHARS` | 2,000 | Internal progress-block ceiling |
| `PROGRESS_DELTA_MAX_BULLETS` | 12 | Internal progress-block bullet ceiling |
| `INVENTORY_MAX_CHARS` | 120,000 | Complete-packet size ceiling |
| `PERSONA_INVENTORY_MAX_CHARS` | 120,000 | Complete persona-packet size ceiling |
| `INVENTORY_CACHE_ACTIVE_TTL_S` | 300 | Active survey cache TTL |
| `INVENTORY_CACHE_CLOSED_TTL_S` | 86,400 | Closed/archived cache TTL |
| `INVENTORY_CACHE_BENCHMARK_TTL_S` | 86,400 | Benchmark cache TTL |
| `INVENTORY_CACHE_MAX_ENTRIES` | 128 | Process-local inventory LRU bound |
| `MAX_PARALLEL_SQL_CALLS` | 6 | Compatibility/default source for the tool executor size |
| `MAX_PARALLEL_TOOL_CALLS` | same as SQL setting | Homogeneous SQL/statistics batch executor size |

CLI options:

```text
--prompt
--client-id
--org-id
--survey-id
--no-inventory
--chat
--thread-id
```

## 16. Known limitations and operational risks

1. **Credentials have source-code fallbacks.** The OpenAI key and Charts secret in particular
   must be removed from source and rotated if valid. Environment-only configuration should be
   mandatory before shared deployment.
2. **`_SCOPE` is process-global.** The CLI is naturally single-run and the API serializes runs,
   but the core agent is not re-entrant or safe for concurrent tenants in one process.
3. **SQL scoping is advisory for `nl2sql_tool`.** Packet and statistics authorization is enforced
   server-side and fails closed. A model-authored SQL statement with no scope ID is warned about
   but still executes; hard SQL tenancy enforcement would require query rewriting, database RLS
   or a scope-aware database view/role.
4. **SQL results are unbounded in memory/context.** `fetchall()` and `json.dumps()` materialize the
   complete result. No result store or condenser is wired in this version, despite stale manifest
   wording in the tool description. Large model-authored queries can exhaust process memory or
   the model context.
5. **The inventory is eager.** A fresh current-survey thread pays for the inventory before the
   model decides whether it needs analytical measures. The TTL cache mitigates database work but
   not the user-message token cost.
6. **Inventory failure is deliberately silent to the graph.** Database failure, empty measures
   and oversize all produce no startup inventory; the agent must discover the reason/data through
   SQL if the question needs it.
7. **The Charts API is external.** It can time out, reject a question or lack data that exists in
   the local database. The prompt requires descriptive SQL fallback when formal tests cannot run.
8. **Memory is process-local and unsummarized.** Restarts lose conversations; long threads grow
   checkpoint storage; trimmed old facts can become invisible to the model.
9. **The API scope-resolution cache is unbounded and has no TTL.** It assumes survey ownership
   metadata is stable for the life of the worker.
10. **Process-level API workers do not share threads or caches.** Sticky load balancing or a shared
    checkpointer is necessary for reliable multi-worker conversations.
11. **Persona coverage is categorical.** The eager persona packet covers answered, non-product
    multiple-choice questions. Free-text occupation or other open-ended fields require explicit
    scoped SQL and careful coding; the agent must not fabricate a category distribution.
12. **Persona dominance is descriptive, not population representativeness.** Confidence intervals
    and the adjusted top-category test quantify sampling uncertainty within this survey. They do
    not make a convenience sample representative of a broader market or prove that marginal
    traits co-occur in one segment.

## 17. Validation

Focused validation for structural changes:

```bash
# Syntax without writing bytecode into the source tree.
PYTHONPYCACHEPREFIX=/tmp/flavorai-optim3-pyc \
  python3 -m py_compile \
  funda_agent_exp.py agent_instructions.py tool_prompts.py api_funda_agent_exp.py

# Optimization, single-model/full-prompt, cache, scope, memory and API tests.
../BE/.venv/bin/python -m unittest discover -s tests -v
```

Behavioral accuracy must be checked against live database references, not inferred from unit
tests. Follow the repository-level regression playbook in
[`../../AGENTS.md`](../../AGENTS.md) when release-level regression scoring is required.

When changing this agent, preserve these key invariants unless the change is deliberate and
tested:

- the graph remains bounded by `MAX_LLM_STEPS`;
- the final permitted LLM turn has no tools;
- every LLM turn uses the complete `SYSTEM_PROMPT_TEXT`;
- the SQL tool schema remains exactly the query plus the two progress-ledger fields;
- packet/statistics authorization fails closed;
- the inventory is complete or absent, never truncated;
- startup and on-demand packet paths share the same serialized payload;
- persona packets are complete or absent and a persona trait is never filled without evidence;
- independent SQL results remain in original tool-call order;
- every new question resets per-question scratch state; and
- message trimming never creates an orphan `ToolMessage`.
