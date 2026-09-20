---
type: synthesis
title: "Architecture of the current agent"
question: "What is the architecture of the current agent?"
tags: [agent-variant, prompt-design]
sources: [funda-agent-exp-architecture-doc, final-agent-work-bundle-readme, client-boundary-session-handoff, survey-analyst-api-contract]
sources_consulted: [funda-agent-exp-architecture-doc, final-agent-work-bundle-readme, client-boundary-session-handoff, survey-analyst-api-contract]
created: 2026-08-14
updated: 2026-08-14
---

**Question:** what is the architecture of the current agent?

The agent in this working copy — [[funda-agent-exp]], identified as `final_agent_work_v5_base_2_optim3` — is a **guarded ReAct loop built on LangGraph**, wrapped in a streaming FastAPI service. Its organising principle is a strict division of authority: the model owns semantic judgment, and Python owns everything that must not be negotiable.

## The organising principle

| The model decides | Python decides |
|---|---|
| What the question means | Scope binding and repair |
| Which measure is semantically appropriate | Packet and statistics authorization |
| Construct/scale/attribution compatibility across surveys | SQL validation, read-only mode, timeout |
| Which tool and query shape can answer | Concurrent vs sequential execution |
| Writing a sourced evidence delta when more tools follow | Filtering internal deltas and fingerprinting SQL repeats |
| Which statistical test fits | Stall and zero-row diagnostics |
| The final explanation, caveats, recommendation | Strong/weak and full/lean routing |
| Which persona prompts genuinely measure demographics | Checkpoint updates and the hard turn limit |

The stated rationale: this prevents the model from overriding security boundaries or mutation rules, while preserving judgment in the places where deterministic keyword logic would *hide* evidence. That second half is the non-obvious part, and it recurs as a design motif — semantic filtering on question-meaning columns is banned, the persona query refuses to classify demographics by SQL keyword, and packets are never truncated. Each is a refusal to let a heuristic silently narrow what the model can see.

## Layer 1 — the graph

Two nodes, `LLM` and `tools`, cycling until an AI message arrives with no tool calls. Bounded at `MAX_LLM_STEPS = 12` LLM invocations. On the final permitted turn, tools are **unbound** and the model is told to answer from what it has and disclose what is unresolved — the budget degrades into a forced honest summary rather than an error.

State is a `TypedDict` where only `messages` accumulates; every other field is replaced, and all per-question scratch state resets on each new question, including follow-ups. Full detail in [[langgraph-react-topology]].

## Layer 2 — routing

Two models at temperature zero (strong `gpt-5.5`, weak `gpt-5.4-mini`) and two prompts (full with schema map, lean without). Python selects both from latch state; the model never chooses.

The economics: retrieval *planning* is expensive judgment, writing up a *completed* retrieval is not. Once `sql_done` latches, the schema map is dead weight and the smaller model closes the answer. Two deliberate exceptions keep the strong model on the lean prompt — a successful cross-survey analysis packet, and persona/profile turns — because both involve high-judgment work after retrieval ends.

The lean prompt is asserted to be a literal **prefix** of the full prompt and per-run IDs live in the user message, keeping the system prefix byte-stable for prompt caching. Full detail in [[model-and-prompt-routing]].

## Layer 3 — three tools

`nl2sql_tool` (model-authored SQL plus ledger metadata), `get_survey_analysis_packet` (one survey's inventory, authorization enforced server-side), `run_survey_stats` (ANOVA, Tukey, Pearson, Spearman, chi-square via an external Charts API).

Persona retrieval is deliberately not a fourth tool — a route detector fires before the graph runs and attaches a cached packet, so ordinary prompts don't pay its cost.

There is **no result store, result parking, digest workflow or drilling sub-agent** in this implementation. Those belong to superseded experiments, and [[funda-agent-exp-architecture-doc]] flags its own `nl2sql_tool` description as stale for still mentioning a manifest that isn't wired up.

## Layer 4 — evidence before the first token

Before any LLM call, one PostgreSQL statement builds a complete analytical packet: products, per-product scored measures with means and sample SDs, and every remaining answered measure. The model reads statistics, not rows.

The packet is **complete or absent, never truncated** — a partial candidate list could silently change which measure the model picks. Cached in a process-local bounded LRU with lifecycle-sensitive TTLs (300 s active, 24 h closed/archived/benchmark). Full detail in [[survey-inventory-packet]].

## Layer 5 — self-monitoring

After a tool batch, an assistant turn that continues with more tools stores a compact
`<progress_gathered>` evidence delta in its persisted content. Parallel tool evidence is unioned;
raw tool results remain authoritative. There is no mutable tracker message or SQL ledger metadata,
so later ReAct requests extend earlier requests as an exact cacheable prefix.

Python warns only on an objective repeat of the same canonical SQL and result, and independently
prompts join diagnosis after two all-empty SQL batches. Full detail in [[progress-ledger]].

## Layer 6 — the tenant boundary

Authority derives from the initial survey via `survey → organization → account.client_id`; a caller-supplied client ID is never trusted. Packet and statistics authorization **fails closed**. A foreign-survey reference is terminal — no retry, no substitute, no suggestion — while ordinary parser errors stay repairable.

**One important caveat:** how strongly SQL tenancy is enforced is contested between sources. See [[survey-authorization-scope-contradiction]] — this is the single thing to verify in code before relying on this section. Mechanism detail in [[sql-guardrails-and-scope]].

## Layer 7 — the service

[[api-funda-agent-exp]] exposes `POST /ask`, `GET /health` and `DELETE /threads/{id}`, streaming NDJSON. Because `_SCOPE` is a process global, the API serializes complete runs behind one `asyncio.Lock` — **one request at a time per process**, with `_SCOPE` cleared and rebuilt before each.

The protocol's defining quirk is `reset`: since a turn's finality isn't knowable until it ends, tokens stream live and a `reset` event retroactively invalidates them if the turn turned out to be a tool call. Clients must either honour it or ignore tokens entirely and use `done.answer`.

## Layer 8 — memory

An `InMemorySaver` keyed by `thread_id`, with a thread bound to its survey. Trimming affects only what is sent to the model, always preserving message 0 (scope + inventory) and the current in-progress exchange, and never cutting into an orphan `ToolMessage`.

Because trimming is positional rather than summarizing, an old fact can be checkpointed yet invisible to the model — a tracked known failure. Full detail in [[conversation-memory-and-trimming]].

## What to be careful about

Four things a reader should carry away alongside the architecture: credentials have hardcoded source fallbacks and need rotation; the API has **no authentication**; SQL results are materialized unbounded via `fetchall()`; and the process-global `_SCOPE` makes the core agent non-re-entrant. Collected in [[known-limitations-and-risks]].

## Sources consulted

[[funda-agent-exp-architecture-doc]] (primary, audited 2026-08-10) · [[client-boundary-session-handoff]] (2026-08-12, supersedes the tenancy account) · [[final-agent-work-bundle-readme]] · [[survey-analyst-api-contract]]
