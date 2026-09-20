# Isolated drilling workflow for oversized ReAct tool results

This document describes the drilling design used by the reference
`funda_agent_exp.py`. It is intended to be sufficient for implementing the
same pattern in another repository or ReAct/LangGraph agent.

The design solves one specific problem: a main SQL-capable agent executes a
read-only query whose result is too large to put into the main model context.
The complete result is stored outside the conversation, and a short-lived,
separate ReAct agent retrieves only the facts needed by the main agent.

The drill is not a second conversational participant. It is an internal
retrieval pass. Its output is inserted into the main agent as one tool result.

## 1. High-level architecture

```text
main ReAct graph
    |
    | nl2sql_tool(sql_query, ...)
    v
SQL database
    |
    | result too large for inline context
    v
result store: result_id -> complete rows
    |
    | main tool node detects RESULT STORED manifest
    v
isolated drill ReAct graph
    |
    | describe / aggregate / filter / page stored rows
    v
refined facts + STILL MISSING report
    |
    | returned as the original nl2sql_tool ToolMessage
    v
main ReAct graph continues with its own history only
```

There are two possible condensers in the reference:

1. The default digest workflow performs one weak-model turn over deterministic
   Python-computed statistics for the entire stored result.
2. The actual drilling agent performs up to four ReAct turns over the stored
   result using result-store tools.

The selector is `ENABLE_DIGEST`. The reference defaults it to enabled, so set
`ENABLE_DIGEST=0` to use the tool-driven drill described in this document.
An implementation may keep only the drill agent and omit the digest branch.

## 2. When drilling is triggered

Drilling is triggered by the main tool-execution node, not by a separate user
request and not by a free-standing background process.

The SQL tool must return a compact manifest when its serialized result exceeds
a configured context threshold. The manifest must contain stable marker text:

```text
RESULT STORED (too large to inline). result_id=<opaque-id>
rows=<integer> backend=<backend-name>
columns=<column/type description>
first_<n>_rows=<small sample>
The full rows are NOT in this message.
```

The main tool node applies this logic:

```python
result = nl2sql_tool.invoke(sql_args)

if tool_name == "nl2sql_tool":
    result_id = parse_result_id_if_result_stored(result)
    if result_id:
        parked_ids.add(result_id)
        result = run_drill(
            user_query=last_user_question(main_state.messages),
            information_gathered=sql_args.get("information_gathered", ""),
            information_still_needed=sql_args.get("information_still_needed", ""),
            sql_query=sql_args.get("sql_query", ""),
            manifest=result,
            result_id=result_id,
        )

        if result_store.load_rows(result_id) is None:
            parked_ids.discard(result_id)

return ToolMessage(content=result, name=tool_name, tool_call_id=call.id)
```

Only an oversized `nl2sql_tool` result triggers drilling. Inline SQL results,
SQL errors, zero-row results, and calls to result-store tools pass through
unchanged.

The reference also avoids triggering merely because a result has many rows.
It uses a serialized-character ceiling, plus optional shape exceptions for
small aggregate tables and candidate/choice lists. Choose the threshold based
on the target model's context window. The important invariant is that the
complete rows are parked before the oversized payload reaches the model.

## 3. Result store requirements

The result store is process-local in the reference, but Redis or another
external store is also valid. The store must support:

```python
result_id = store_rows(rows: list[dict], sql_query: str = "") -> str
rows = load_rows(result_id) -> list[dict] | None
digest = digest_result(result_id) -> str | None
discard_result(result_id) -> bool
```

`store_rows` should preserve the original Python values where possible. This
matters for exact numeric aggregation; converting database numerics to strings
too early can cause incorrect statistics.

The store should bound retained results in a long-running process, for example
with a maximum number of stored results or a TTL. Do not discard a result until
the drill has either succeeded or the main agent no longer needs it.

The four reader tools exposed to the drill are:

### `describe_result(result_id)`

Returns row count, column names/types, non-null counts, numeric min/max/mean,
and low-cardinality distinct values. The drill should normally call this first
if the digest is insufficient.

### `aggregate_result(result_id, group_by=None, metrics=None, filters=None,
sort_by=None, descending=False, limit=50, offset=0)`

Computes exact counts and numeric min/max/mean/sum over all matching stored
rows. This is the only trusted way for the drill to produce grouped numbers.
It must report `groups_total`, `groups_returned`, and `next_offset`, so the drill
can page through more than the result limit.

### `query_result(result_id, columns=None, filters=None, sort_by=None,
descending=False, limit=20)`

Filters/projects/sorts rows and returns a bounded page. It must report the exact
number of matching rows in addition to the number returned.

### `slice_result(result_id, offset=0, limit=20)`

Returns a deterministic page in original row order with `total_rows` and
`next_offset`.

The reference caps ordinary row retrieval at 50 rows and grouped retrieval at
200 groups per call. These limits prevent the drill from recreating the
original context overflow.

The main agent may receive only a subset of these tools after the result is
parked. The reference's main loop has access to result readers for fallback,
while the isolated drill additionally receives `aggregate_result`.

## 4. Drill agent and graph

The drill is a bounded ReAct graph with its own state:

```python
class DrillState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    steps: int
```

The graph has two nodes:

```python
workflow.add_node("LLM", drill_llm)
workflow.add_node("tools", drill_tool)
workflow.set_entry_point("LLM")
workflow.add_conditional_edges(
    "LLM",
    should_continue,
    {"continue": "tools", "end": END},
)
workflow.add_edge("tools", "LLM")
```

The routing rule is:

```python
def should_continue(state):
    if state["steps"] >= DRILL_MAX_STEPS:
        return "end"
    last = state["messages"][-1]
    return "continue" if last.tool_calls else "end"
```

The reference uses `DRILL_MAX_STEPS = 4`. On the final allowed turn, invoke
the weak model without tools and append an instruction to write the final drill
message. This guarantees termination and prevents a final unexecuted tool call.

The drill tool node executes every tool call in the last AI message, converts
the output to text/JSON, and appends `ToolMessage`s to the drill state. Tool
errors are returned to the drill as text rather than crashing the main run.

Use a separate model binding for the drill, for example:

```python
drill_model = weak_llm.bind_tools(DRILL_TOOLS)
```

The drill model can be weaker/cheaper than the main model because all numerical
operations are performed by deterministic tools.

## 5. Drill prompt contract

The drill's system prompt must establish these rules:

- It works over exactly one stored SQL result.
- It cannot run SQL, see the conversation, or ask another agent questions.
- It does not write the user-facing answer.
- It returns only the facts the main analyst needs.
- Every reported figure must come directly from a tool response or digest.
- It must never count, average, sum, or re-round fetched rows itself.
- It must use `aggregate_result` for totals and per-group figures.
- It must not derive a coarser mean by averaging subgroup means.
- It must close relevant gaps identified by the digest or main agent.
- If a response is paged, it must follow `next_offset` until complete or state
  exactly how many groups/rows were reported.

The final message format should be machine- and model-readable:

```text
<refined findings, with every figure attached to its group/column and n>

ALSO AVAILABLE: <confirmed facts beside the requested answer>

STILL MISSING: <unresolved requested facts, or "nothing">
```

`STILL MISSING` is important. The main agent copies it into per-run state and
uses it to prevent itself from incorrectly declaring the question complete.

For a choice set—rows containing an ID plus prompt/label—the drill must return
every candidate row and the complete prompt/label text. It may project fewer
columns, but it must not silently drop candidates because the main agent may
need to choose among them.

## 6. Building the isolated seed message

When the main tool node calls `run_drill`, build one seed `HumanMessage`:

```text
USER QUESTION:
<last user question>

ALREADY GATHERED (per the SQL author):
<information_gathered or (nothing reported)>

STILL NEEDED (per the SQL author):
<information_still_needed or (nothing reported)>

SQL THAT PRODUCED THIS RESULT:
<sql_query or (not reported)>

STORED RESULT: result_id=<id>
Pass this result_id to every result-store tool call.

<deterministic digest, if available>
```

Invoke the drill with a fresh state:

```python
final = DRILL_GRAPH.invoke(
    {"messages": [seed_message], "steps": 0},
    config={"recursion_limit": 2 * DRILL_MAX_STEPS + 2},
)
refined = last_nonempty_ai_text(final["messages"])
```

Do not pass the main agent's message list, checkpointer, thread ID, system
prompt, schema, previous tool messages, or prior conversation turns. The seed
contains only the minimum facts needed to retrieve from this one result.

If the drill fails or returns empty text, return the original manifest unchanged.
This preserves a usable `result_id` so the main agent can recover by paging the
stored rows or re-running SQL. Do not discard the stored rows on a failed drill.

On success, replace the manifest with a compact tool-result message such as:

```text
REFINED SQL RESULT.
The result was too large to inline. An isolated retrieval agent extracted the
facts below from exact stored rows. The raw rows are not in this message.
If another fact is needed, re-run nl2sql_tool with GROUP BY/aggregates.

Source: <row_count> rows from the query above.

<refined drill output>
```

Then discard the result and remove its ID from the set of active parked IDs.

## 7. Why the drill history does not pollute main history

There are four separate isolation mechanisms:

### Separate state object

The drill uses `DrillState`, not the main agent's `AgentState`. Its `messages`
channel contains only the drill system prompt, seed, drill AI messages, and
drill tool messages.

### Fresh graph invocation

Each drill starts with a new invocation containing only:

```python
{"messages": [seed_message], "steps": 0}
```

Do not use the main graph's checkpointer or conversation thread for this
invocation. If persistence is required for debugging, use a separate drill
thread namespace and delete it after the run; do not merge it into the main
thread.

### Result returned as one main tool observation

The drill's internal messages are never appended to the main `messages` list.
Only the final `refined` text is wrapped in the `ToolMessage` corresponding to
the original `nl2sql_tool` call. From the main agent's perspective, the event
is equivalent to a tool returning a smaller result.

### Explicit cleanup

On successful refinement, discard the stored rows and remove the ID from the
main loop's `parked_ids`. On failure, preserve the rows and manifest. This
prevents both memory leaks and stale result IDs while retaining recovery data
when the drill did not complete.

The resulting main history is conceptually:

```text
main HumanMessage(question)
main AIMessage(tool_call: nl2sql_tool)
main ToolMessage(content: refined facts)
main AIMessage(next SQL call or final answer)
```

It does **not** contain:

```text
drill system prompt
drill seed
drill AI tool calls
drill ToolMessages
drill intermediate reasoning
```

This matters for context size, prompt caching, privacy, and preventing the
main agent from treating the drill's intermediate retrieval decisions as user
conversation history.

## 8. Main-agent integration state

The main ReAct state needs only lightweight routing metadata in addition to its
normal messages:

```python
class AgentState(TypedDict):
    messages: list[BaseMessage]
    number_of_steps: int
    parked_ids: set[str]       # or a process-local equivalent
    drill_missing: str         # latest STILL MISSING body
    progress_gathered: str
    progress_needed: str
```

`parked_ids` controls whether the main model is given fallback result-reader
tools. It is not conversation history. `drill_missing` is a short control
signal, not the drill transcript.

After a successful drill, parse `STILL MISSING:` from the returned text and
store only that text in `drill_missing`. On the next SQL call, if the model
reports `information_still_needed` as empty while `drill_missing` is non-empty,
prepend a warning such as:

```text
You reported nothing still needed, but the retrieval pass reported:
<drill_missing>
Close that gap or explicitly explain why it does not bear on the question.
```

This preserves the useful completion signal without importing the drill's
history.

## 9. Failure and cleanup rules

Implement all of the following:

- Unknown drill tool: return an error `ToolMessage` to the drill.
- Result-store tool exception: return the exception type/message to the drill.
- Missing `result_id`: return a clear message telling the main agent to rerun
  SQL; do not raise out of the user request.
- Drill exception: return the original manifest to the main agent and keep the
  rows stored.
- Empty drill response: same fallback as an exception.
- Successful drill: discard rows, remove active ID, and return refined facts.
- Digest failure: fall back to the manifest or run the drill directly, while
  preserving the result ID.
- Process shutdown: clear all parked results.

Never silently turn a capped page into a complete result. The tool response
must expose exact totals and offsets, and the drill must report incomplete
coverage under `STILL MISSING`.

## 10. Minimal implementation checklist

For a faithful ReAct implementation in another repository:

- [ ] Add a bounded result store keyed by opaque `result_id`.
- [ ] Make the SQL tool park oversized results and return a manifest.
- [ ] Detect manifests only in the main tool node after SQL execution.
- [ ] Add `describe_result`, `aggregate_result`, `query_result`, and
      `slice_result` tools.
- [ ] Build a separate drill graph/state with a fresh invocation per result.
- [ ] Use a four-turn maximum and a no-tools final turn.
- [ ] Seed the drill with question, SQL metadata, manifest ID, and digest only.
- [ ] Never pass main messages or checkpointer state into the drill.
- [ ] Require every numeric claim to come from a tool/digest.
- [ ] Require `ALSO AVAILABLE` and `STILL MISSING` in the drill output.
- [ ] Return only the refined text as one main `ToolMessage`.
- [ ] Preserve the manifest on drill failure; discard rows only on success.
- [ ] Remove successful IDs from active parked-result state.
- [ ] Feed only the latest missing-gap text back into main control state.
- [ ] Test inline, oversized, empty, failed, paginated, grouped, and choice-set
      results separately.

## 11. Reference source locations

The original implementation is split conceptually across these areas of
`funda_agent_exp.py`:

- `DRILL_SYSTEM_PROMPT`, `DrillState`, `_drill_llm`, `_drill_tool`,
  `_drill_should_continue`, and `build_drill_graph`: isolated ReAct graph.
- `_parked_result_id`, `_last_user_query`, and `_last_ai_text`: bridge helpers.
- `run_drill`, `_run_drill_agent`, and `_run_digest_workflow`: dispatch and
  success/failure handling.
- `call_tool`: main-agent trigger and reinsertion as a `ToolMessage`.
- `call_model`: main-agent tool binding based on whether rows remain parked.

The result-store implementation is in `output_store.py`:

- `store_rows`, `load_rows`, `discard_result`, and `digest_result`.
- `describe_result`, `aggregate_result`, `query_result`, and `slice_result`.
- `DRILL_TOOLS` and `RESULT_TOOLS` tool lists.

