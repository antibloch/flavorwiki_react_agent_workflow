---
type: synthesis
title: "Contradiction: how far does survey authorization reach, and is SQL tenancy enforced?"
question: "Which sources correctly describe the agent's survey authorization scope and SQL tenancy enforcement?"
tags: [db-schema, agent-variant, open-question]
sources: [funda-agent-exp-architecture-doc, client-boundary-session-handoff, final-agent-work-bundle-readme]
sources_consulted: [funda-agent-exp-architecture-doc, client-boundary-session-handoff, final-agent-work-bundle-readme]
created: 2026-08-14
updated: 2026-08-14
---

Two documents in this working copy disagree about the agent's tenancy model on two separate points. Both disagreements matter, because the weaker reading of each is a security claim.

**Resolution: the newer document is correct on both counts, verified against `funda_agent_exp.py`. The architecture document's tenancy sections are stale.** The contradiction is recorded here rather than erased, because the stale document remains the project's primary architecture reference and a future reader will hit the same conflict.

## Disagreement 1 — organization-scoped or client-scoped?

**[[funda-agent-exp-architecture-doc]]** (§10.2, last source audit 2026-08-10) lists allowed packet targets as: the current survey; another survey **in the current organization**; or the configured fixed benchmark survey. **[[final-agent-work-bundle-readme]]** independently repeats this organization-scoped framing.

**[[client-boundary-session-handoff]]** (2026-08-12) states the authorized set is the initial survey, all **same-client surveys across organizations**, and the benchmark exception — and records the underlying decision explicitly: "Allow explicit navigation/comparisons to any survey owned by that client across organizations."

### Verified against code

`funda_agent_exp.py` builds `authorized_survey_ids` in the scope-resolution statement (near line 300) as the union of:

1. the initial survey itself;
2. any survey whose organization's account carries the **same `client_id`** — joined `survey → organization → account`, with no restriction to the initial `organization_id`; and
3. the benchmark survey, when a client resolved.

The organization ID is selected and retained in `AuthorizedScope`, but it does not constrain the authorized set. The handoff's client-wide account is correct; the architecture document and README describe a narrower boundary than the code implements.

The code also confirms the clientless rule: both the same-client branch and the benchmark branch are guarded by `i.client_id IS NOT NULL`, so a survey with no resolvable client yields an authorized set of exactly itself — the guard tightens rather than disabling, as the handoff states.

## Disagreement 2 — is SQL tenancy enforced or merely advisory?

This is the more consequential of the two.

**[[funda-agent-exp-architecture-doc]]** §16.3 lists as a known risk: "SQL scoping is advisory for `nl2sql_tool`. … A model-authored SQL statement with no scope ID is warned about but still executes; hard SQL tenancy enforcement would require query rewriting, database RLS or a scope-aware database view/role."

**[[client-boundary-session-handoff]]** states that `sqlglot` parsing and AST rewriting were added so that **every exposed physical table is wrapped with a verified survey-ownership filter**, with multiple statements, unsafe functions and schemas, reserved scope parameters, foreign survey literals, and tables lacking an ownership route all refused.

The second describes exactly the query rewriting the first names as the missing prerequisite.

### Verified against code

The rewriting mechanism is present:

- `funda_agent_exp.py:16` imports `from sqlglot import exp, parse`.
- `_guard_and_scope_sql(sql, conn, scope)` exists as the guard entry point, alongside `AuthorizedScope`, `SurveyBoundaryError`, `_resolve_authorized_scope()` and `_repair_scope_ids()`.
- `_TENANT_TABLE_SCOPES` is an explicit per-table ownership map — `survey` by `id`, `question`/`product`/`question_screen`/`question_section`/`logic` by `"surveyId"`, `enrollment`/`survey_nomenclature`/`benchmark_registry`/`survey_panel` by `survey_id` — built through `_direct_scope()` and `_through_question()` helpers.
- Both `requirements.txt` and `deployment/requirements.txt` pin `sqlglot==30.16.0`.

The per-table map is also the mechanism behind the maintenance obligation the handoff records: a newly exposed table absent from this map has no ownership route and will correctly fail closed.

**Scope of this verification:** the presence and shape of the guard are confirmed by reading the module. Its full runtime behaviour — every rejection path, every rewrite case — was not exercised. The advisory framing in §16.3 should be treated as stale, not as describing a live gap.

## Why the documents diverged

The most economical explanation is chronology. The architecture document's own audit stamp is 2026-08-10; the handoff records work verified 2026-08-12 whose entire subject is replacing an advisory boundary with a code-enforced one. The document was not re-audited after that work landed.

[[final-agent-work-bundle-readme]] shows independent signs of the same drift: it cites architecture-document section numbers that no longer match (§3 architecture, §5 tools, §7 parking, §12 limitations, against the current §5, §10, §16) and refers to result parking, which the current document says no longer exists.

## What a reader should do

- For tenancy questions, treat [[client-boundary-session-handoff]] as current and the architecture document's §10.2 and §16.3 as superseded.
- Do not cite "SQL scoping is advisory" as a live risk. It reads as an open vulnerability and is not one.
- Note that `funda_agent_exp.py` still carries a **separate** advisory check that is genuinely advisory: the no-scope-placeholder detector warns the model without blocking, per [[sql-guardrails-and-scope]]. That is a prompt-level nudge sitting *in front of* the AST guard, not the tenancy boundary itself. Conflating the two is the likely route to misreading §16.3 as still accurate.
- The unverified-refinement caveat still stands on its own terms: the handoff's 79/79 test run predates its own latest response-count and terminal-refusal changes, and no automated rerun has happened. Enforcement existing is not the same as enforcement being regression-tested.

## Sources consulted

[[client-boundary-session-handoff]] (2026-08-12, current) · [[funda-agent-exp-architecture-doc]] (2026-08-10, stale on these two points) · [[final-agent-work-bundle-readme]] · plus direct reading of `funda_agent_exp.py` lines 16, 300–423 and `requirements.txt`.
