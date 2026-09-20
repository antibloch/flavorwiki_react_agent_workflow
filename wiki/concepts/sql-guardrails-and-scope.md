---
type: concept
title: "SQL guardrails and tenancy scope"
tags: [db-schema, agent-variant, open-question]
sources: [funda-agent-exp-architecture-doc, client-boundary-session-handoff, final-agent-work-bundle-readme]
created: 2026-08-14
updated: 2026-08-14
---

How [[funda-agent-exp]] keeps a model-authored SQL statement safe and inside its tenant. Two layers exist: a validation pipeline applied to every statement, and an authorization envelope defining which surveys are reachable at all.

**The strength of the second layer is contested between sources.** See [[survey-authorization-scope-contradiction]] before relying on this page for a security claim.

## Validation pipeline for `nl2sql_tool`

```python
nl2sql_tool(sql_query: str) -> str
```

Execution order:

1. **Repair near-miss literals** within four edits of an authoritative scope ID. Repairs are disclosed in the returned tool text rather than applied silently.
2. **Warn** when the query contains no scope placeholder, scope literal or UUID — advisory only.
3. **Reject** semicolon-separated batches.
4. **Reject** statements whose leading operation is not `SELECT` or `WITH`.
5. **Reject semantic pattern matching** on question/option meaning columns — `prompt`, `promptHtml`, `label`, `labelHtml`, `internal_name`, `optionDefinition`.
6. **Bind** the current `_SCOPE` dictionary as SQLAlchemy parameters.
7. **Execute** inside a read-only transaction with a statement timeout (`STATEMENT_TIMEOUT_MS`, default 20,000 ms).
8. **Return** all rows as a JSON array, or structured feedback for zero rows or failure.

Query failures are returned to the model as strings rather than raised through the graph, so a bad query becomes a repairable observation instead of a crash.

### Why semantic filtering is banned

Rule 5 is the analytically interesting one. Structural filters are permitted; keyword filtering on what a question *means* is not. Exact equality on a full prompt **is** permitted once the model has inspected candidates. The effect is to force measure selection to happen in the model's semantic judgment over complete prompt text — the same principle that makes [[survey-inventory-packet]] complete-or-absent — rather than through a `LIKE '%liking%'` heuristic that would silently exclude the measure that actually means "overall liking". [[regression-workbook]]'s "needle in haystack" suite exists to test exactly this.

PostgreSQL enforces read-only execution even if the model attempts a mutation, so rule 4 is defence in depth rather than the only barrier.

## Authorization envelope

Authority derives from the **initial survey**, never from a caller-supplied identifier: `survey → organization → account.client_id`. [[client-boundary-session-handoff]] is explicit that a caller-supplied client ID is never trusted as authorization.

When the initial survey has no resolvable client, the authorized set narrows to that survey alone — the guard is tightened, not disabled. [[api-funda-agent-exp]] publishes survey-only scope in that case and switches to a prompt that forbids unbound client/org placeholders; the fixed benchmark survey stays reachable.

`get_survey_analysis_packet` validates UUID syntax, requires a published run scope, resolves the survey, checks the authorization envelope server-side, and refuses an unknown or out-of-scope survey. `run_survey_stats` enforces the same envelope on question ownership. **Both fail closed** if the ownership lookup cannot run. Feature-influence question IDs use the same scope.

For reference-based tests both questions must belong to the same survey; cross-survey correlation is rejected outright because enrollments provide no usable cross-survey respondent key.

## What is in the authorized set

This is the contested part. [[funda-agent-exp-architecture-doc]] (audited 2026-08-10) and [[final-agent-work-bundle-readme]] both describe packet targets as the current survey, another survey **in the current organization**, or the configured benchmark. [[client-boundary-session-handoff]] (2026-08-12) describes the authorized set as the initial survey, all same-client surveys **across organizations**, and the benchmark.

The contradiction is unresolved in the wiki and analysed in [[survey-authorization-scope-contradiction]].

## Is SQL tenancy enforced or advisory?

Also contested, and consequentially so:

- [[funda-agent-exp-architecture-doc]] §16.3 states SQL scoping is **advisory** for `nl2sql_tool` — a statement with no scope ID is warned about but still executes — and that hard enforcement "would require query rewriting, database RLS or a scope-aware database view/role."
- [[client-boundary-session-handoff]] states that `sqlglot` parsing and AST rewriting were added so **every exposed physical table is wrapped with a verified survey-ownership filter**, with multiple statements, unsafe functions and schemas, reserved scope parameters, foreign survey literals, and tables lacking an ownership route all refused. Both `requirements.txt` files pin `sqlglot==30.16.0`.

The second describes precisely the query rewriting the first calls absent. Do not cite the advisory framing as current without checking the code.

## Terminal refusal

A literal foreign-survey reference is made **terminal** at both the standing-prompt and tool-result levels: no retry, no current-side partial query, no substitute survey, no table or UUID repetition, no unprompted benchmark discussion, and no clickable suggestion. Other SQL parser and guard errors remain repairable — the distinction is deliberate, separating "you may not ask this" from "that query was malformed."

A recorded live trace shows the intended path: the guard refused before execution, the agent made no second tool call, and returned one concise sentence in two LLM calls total.

## Response-count grain

An unqualified survey response count means `COUNT(DISTINCT a.enrollment_id)` through answers, aliased `answered_people`. Enrollment counts, completed-person counts and answer-row counts each require explicit distinct names, and a generic `response_count` is prohibited. This is the same class of precision as the `submissions` versus `answers` split in [[survey-inventory-packet]].

## Maintenance obligation

Any newly exposed database table needs a verified entry in the tenant table-scope map, or queries against it will correctly fail closed. This is a standing cost of the fail-closed design, recorded in [[client-boundary-session-handoff]].

## Related

[[funda-agent-exp]] · [[survey-authorization-scope-contradiction]] · [[progress-ledger]] · [[known-limitations-and-risks]]
