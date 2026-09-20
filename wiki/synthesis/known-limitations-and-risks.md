---
type: synthesis
title: "Known limitations and operational risks"
question: "What is known to be broken, risky, or unfinished in the current agent?"
tags: [agent-variant, deployment, open-question]
sources: [funda-agent-exp-architecture-doc, final-agent-work-bundle-readme, survey-analyst-api-contract, client-boundary-session-handoff]
sources_consulted: [funda-agent-exp-architecture-doc, final-agent-work-bundle-readme, survey-analyst-api-contract, client-boundary-session-handoff]
created: 2026-08-14
updated: 2026-08-14
---

Consolidated from all four ingested sources. Grouped by what a reader would act on, not by source. Items corroborated by more than one document are marked, since independent agreement raises confidence.

## Blocking before any shared deployment

**Credentials are hardcoded in source, and rotation — not deletion — is the fix.** `funda_agent_exp.py` carries fallback literals for an OpenAI API key, the `DATABASE_URI`, and the Charts API secret. `os.getenv` still wins, so a real `.env` overrides them, but the literals are in the file **and already in the git history of the source repository**, which is why deleting the lines is insufficient. *Corroborated by [[final-agent-work-bundle-readme]] and [[funda-agent-exp-architecture-doc]] §16.1.* Environment-only configuration should be mandatory before shared deployment. (Per this wiki's schema rule, the values are not reproduced anywhere in the wiki.)

**The API has no authentication.** Anything that can reach the port can query the database through it. CORS is open by default (`Access-Control-Allow-Origin: *`), lockable with `CORS_ORIGINS`. *Corroborated by [[final-agent-work-bundle-readme]] and [[survey-analyst-api-contract]] §12.* See [[api-funda-agent-exp]].

## Concurrency and scaling

**`_SCOPE` is a process global, so the core agent is not re-entrant.** The CLI is naturally single-run and [[api-funda-agent-exp]] serializes complete runs behind one `asyncio.Lock`, so correctness holds — at the cost of one request at a time per process. True in-process concurrency requires moving `_SCOPE` into request/graph state and passing scope explicitly to every tool. *Corroborated by [[funda-agent-exp-architecture-doc]] §16.2 and [[survey-analyst-api-contract]] §6.*

**Workers share nothing.** Each Uvicorn worker has its own checkpoint, scope cache and inventory cache, so a follow-up must reach the same worker. Reliable multi-worker conversations need sticky routing or a shared checkpointer.

**The API scope-resolution cache is unbounded with no TTL.** It assumes survey ownership metadata is stable for the worker's lifetime.

## Memory and context

**SQL results are unbounded in memory and context.** `nl2sql_tool` uses `fetchall()` and `json.dumps()` to materialize the complete result. No result store or condenser is wired in this version, **despite stale manifest wording in the tool's own model-facing description**. A large model-authored query can exhaust process memory or the model's context.

**Conversation memory is process-local and unsummarized.** Restarts lose conversations. `InMemorySaver` snapshots growing state with no retention policy — measured at roughly **24 MB at 10 rounds** — and nothing expires it automatically, so `DELETE /threads/{thread_id}` matters. Because trimming is positional rather than summarizing, an old fact can remain checkpointed yet fall outside the model's visible window. [[regression-workbook]] tracks this concretely: **memory round 12 drops rounds 1–6.** See [[conversation-memory-and-trimming]].

**The inventory is eager.** A fresh current-survey thread pays for the packet before the model decides whether it needs analytical measures. The TTL cache mitigates database work but not the user-message token cost. See [[survey-inventory-packet]].

## Silent-failure modes

**Inventory failure is deliberately invisible to the graph.** A database failure, empty measure sections, and an oversized payload all produce the same result — no startup inventory — and the agent must discover the reason through SQL if the question needs it. Intentional, but it means "no inventory" is not diagnostic on its own.

**The Charts API is external.** It can time out, reject a question, or lack data that exists in the local database. The prompt requires descriptive SQL fallback when a formal test cannot run.

**Sanitizers fail silently on the persona card.** Stripping `open` collapses every result block; stripping `details`/`summary` flattens the parent → child hierarchy into an undifferentiated run of tables. Because sanitizers strip rather than error, a wrong allowlist degrades presentation with no signal. See [[survey-analyst-api-contract]].

**A mid-run failure arrives as HTTP 200 plus an `error` event**, since the status code is committed before anything can go wrong. A client checking only `response.ok` shows a blank answer.

## Analytical caveats

**Persona coverage is categorical only.** The eager persona packet covers answered, non-product multiple-choice questions. Free-text occupation and similar open-ended fields require explicit scoped SQL and careful coding, and the agent must not fabricate a category distribution.

**Persona dominance is descriptive, not representativeness.** The Wilson intervals and the Bonferroni-adjusted top-category test quantify sampling uncertainty *within this survey*. They do not make a convenience sample representative of a broader market, and they do not show that marginal traits co-occur in one segment. A multi-trait persona must either retrieve a respondent-level intersection or label itself a marginal composite.

**Cross-survey correlation is unavailable by design** — enrollments provide no usable cross-survey respondent key.

## Testing gaps

**The most recent boundary refinements have no automated rerun.** The 79/79 focused run recorded in [[client-boundary-session-handoff]] predates the response-count and terminal-refusal prompt changes. Two user-supplied live traces stand in as behavioural evidence, and the handoff is explicit that these are examples, not a regression suite.

**The default regression sweep covers 18 of 69 rows.** `run_regression_exp.sh` still sweeps `seq 1 18`, so appended suites — needle-in-haystack, large SQL output, model routing, suggestions, memory — do not run unless the sequence is widened or rows are targeted individually. The 12 memory rows are also **one conversation**; run standalone they test nothing about memory. See [[regression-workbook]].

**Two known failures are tracked as expectations rather than fixed:** memory round 12 dropping rounds 1–6, and the stats-only run never latching. See [[model-and-prompt-routing]] for the second.

**Cross-agent benchmarks cannot be re-run here.** The four scripts comparing this agent against `funda_agent_v1.py` and `funda_agent.py` were deliberately not copied into this bundle.

## Documentation drift

**The primary architecture document is stale on tenancy.** Its §10.2 and §16.3 describe an organization-scoped boundary and advisory SQL scoping, both superseded — see [[survey-authorization-scope-contradiction]]. The README additionally cites section numbers that no longer match the document it points at, and refers to result parking that no longer exists.

**Two configuration fields have no documented counterpart.** `GET /health` reports `tool_char_limit: 250000` and `enable_digest: false`, neither of which appears in the architecture document's configuration table. Given that document's insistence that no digest path is wired in, `enable_digest` is plausibly vestigial, but no ingested source confirms this. **Uncorroborated — worth a code check.**

## Related

[[agent-architecture-overview]] · [[funda-agent-exp]] · [[api-funda-agent-exp]] · [[sql-guardrails-and-scope]] · [[survey-authorization-scope-contradiction]]
