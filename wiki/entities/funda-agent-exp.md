---
type: entity
kind: module
title: "funda_agent_exp.py"
aliases: ["funda_agent_exp", "FlavorAI survey analyst agent", "final_agent_work_v5_base_2_optim3"]
tags: [agent-variant, prompt-design]
sources: [funda-agent-exp-architecture-doc, final-agent-work-bundle-readme, client-boundary-session-handoff]
created: 2026-08-14
updated: 2026-08-14
---

The LangGraph survey-analysis agent that is the subject of this working copy. It answers natural-language questions about a FlavorWiki survey by querying PostgreSQL read-only, reusing a precomputed survey inventory, calling an external Charts API for statistical tests, and building evidence-backed respondent personas. The implementation in this directory is identified as `final_agent_work_v5_base_2_optim3`.

At roughly 3,000 lines it holds the models, tools, inventory query and cache, graph, state and CLI. It contains **no model-facing text of its own** — every string the model reads comes from `agent_instructions.py` (persona, analytical policy, schema map, full and lean system prompts, scope preambles, progress messages) or `tool_prompts.py` (tool descriptions and deterministic feedback text).

## Architecture in one paragraph

A guarded ReAct loop. The model owns semantic judgment; Python owns safety and control flow. See [[agent-architecture-overview]] for the full picture, or [[langgraph-react-topology]] for the graph itself.

## Capabilities

- Read-only PostgreSQL inspection of survey data through model-authored SQL.
- A precomputed inventory of products and answered measures, fetched before the first LLM call — [[survey-inventory-packet]].
- On-demand retrieval of the same inventory shape for an authorized historical or benchmark survey.
- ANOVA, Tukey, Pearson, Spearman and chi-square results via the external Charts API.
- Evidence-backed persona/profile construction from categorical responses, gated on a significance test rather than "largest bucket".
- Comparison of the current survey against an authorized sibling survey or the fixed benchmark.
- Short-term conversation memory when a checkpointed graph and a repeated `thread_id` are used — [[conversation-memory-and-trimming]].

It **does not write survey data**. The SQL transaction is read-only, and PostgreSQL enforces this even if the model attempts a mutation. The only mutable runtime data is graph state, an in-memory checkpoint, process-local caches, the shared SQLAlchemy engine, and the process-global run scope.

## Companion files

| File | Responsibility |
|---|---|
| `agent_instructions.py` | Persona, analytical policy, schema map, full/lean system prompts, scope preambles, progress messages |
| `tool_prompts.py` | Tool descriptions and the deterministic feedback/error text the model sees |
| `api_funda_agent_exp.py` | FastAPI/NDJSON wrapper — [[api-funda-agent-exp]] |
| `feature_influence.py` | Feature-influence computation, scoped by the same authorization envelope |
| `tests/test_agent_optimizations.py` | Focused tests for inventory, cache, routing, memory, scope and API behaviour |

`agent_instructions.py` defines constants named `AGENTS_YAML` and `TASKS_YAML`, but the current agent loads **no** external `agents.yaml`, `tasks.yaml` or `schema_overview.md` at runtime — the schema and model-facing policy are embedded in that Python module.

## Tool surface

Exactly three model-callable tools, exposed by `build_tools()`:

1. **`nl2sql_tool`** — model-authored SQL for counts, distributions, metadata or aggregates absent from a packet. Also carries the progress-ledger metadata described in [[progress-ledger]]. Guardrails in [[sql-guardrails-and-scope]].
2. **`get_survey_analysis_packet`** — the inventory packet for one already-resolved survey UUID, authorization enforced server-side and failing closed.
3. **`run_survey_stats`** — significance tests, correlations and categorical association through the Charts API. Requires a reference question for Pearson, Spearman and penalty requests, and rejects cross-survey correlation because enrollments give no usable cross-survey respondent key.

Persona retrieval is deliberately **not** a fourth tool: `is_persona_request()` detects persona/profile language before the graph runs and attaches a cached packet, so ordinary analytical prompts do not pay its cost.

[[funda-agent-exp-architecture-doc]] is emphatic that there is no result store, result parking, `describe_result`/`query_result`/`slice_result`, digest workflow or drilling sub-agent in this implementation — those belong to superseded experiments. `output_store.py` still exists in the directory but is not wired into the graph.

## Configuration highlights

Defaults from the architecture document's configuration table:

| Setting | Default |
|---|---:|
| `MAX_LLM_STEPS` | 12 |
| `STATEMENT_TIMEOUT_MS` | 20,000 |
| `HISTORY_MAX_ROUNDS` | 5 |
| `HISTORY_MAX_MESSAGES` | 47 |
| `PROGRESS_DELTA_MAX_CHARS` | 2,000 |
| `PROGRESS_DELTA_MAX_BULLETS` | 12 |
| `INVENTORY_MAX_CHARS` | 120,000 |
| `PERSONA_INVENTORY_MAX_CHARS` | 120,000 |
| `MAX_PARALLEL_SQL_CALLS` | 4 |
| `STRONG_REASONING_EFFORT` | `low` |
| `WEAK_REASONING_EFFORT` | `medium` |

CLI options: `--prompt`, `--client-id`, `--org-id`, `--survey-id`, `--no-inventory`, `--chat`, `--thread-id`.

## Invariants to preserve

The architecture document closes with the list of properties not to break without deliberate, tested intent: the graph stays bounded by `MAX_LLM_STEPS`; the final permitted turn has no tools; the lean prompt stays a literal prefix of the full prompt; packet and statistics authorization fails closed; the inventory is complete or absent, never truncated; startup and on-demand packet paths share one serialized payload; a persona trait is never filled without evidence; independent SQL results stay in original call order; every new question resets per-question scratch state; and message trimming never creates an orphan `ToolMessage`.

## Known risks

Hardcoded credential fallbacks in source, a process-global `_SCOPE`, unbounded SQL result materialization, and an eager inventory — collected in [[known-limitations-and-risks]].

## Related

[[api-funda-agent-exp]] · [[survey-inventory-packet]] · [[regression-workbook]] · [[model-and-prompt-routing]] · [[parallel-tool-calling]] · [[survey-authorization-scope-contradiction]]
