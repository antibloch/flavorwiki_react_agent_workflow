---
type: source
title: "Session Handoff — code-enforced client boundary"
authors: ["FlavorAI project (internal document)"]
raw: "SESSION_HANDOFF.md"
ingested: 2026-08-14
source_audit: 2026-08-12
tags: [agent-variant, db-schema, deployment, open-question]
entities: [funda-agent-exp, regression-workbook]
concepts: [sql-guardrails-and-scope]
created: 2026-08-14
updated: 2026-08-14
---

A handoff note recording the state of the survey-authorization work as of **2026-08-12**, two days after [[funda-agent-exp-architecture-doc]]'s last source audit. It is the most recent statement in the wiki about how tenancy is enforced, and it describes a mechanism the architecture document says does not exist.

## The stated objective

Keep the survey analyst useful across surveys while enforcing a hard client boundary. Ordinary work stays on the survey the run started with; the agent may move to another survey only when the user explicitly asks or a comparison requires it. Same-client surveys are reachable **across any of that client's organizations**. A foreign-client survey must be refused in code and treated as terminal by the agent.

## Key claims

**Authorization is derived, not supplied.** The authoritative client comes from the initial survey through `survey → organization → account.client_id`. A caller-supplied client ID is never trusted as authorization. When the initial survey has no resolvable client, the authorized set narrows to that survey alone rather than the guard being disabled.

**SQL tenancy is enforced by AST rewriting.** The note records that `sqlglot` parsing and AST rewriting were added so that every exposed physical table is wrapped with a verified survey-ownership filter. Refused outright: multiple statements, unsafe functions and schemas, reserved scope parameters, foreign survey literals, and any table with no ownership route. Both root and deployment `requirements.txt` pin `sqlglot==30.16.0`.

This directly contradicts [[funda-agent-exp-architecture-doc]] §16.3, which states that SQL scoping is advisory and that hard enforcement "would require query rewriting" — describing as absent the mechanism this note describes as implemented. See [[survey-authorization-scope-contradiction]].

**The same scope applies beyond SQL** — analysis packets, statistics, and feature-influence question IDs all use the authoritative scope, and clientless runs remain initial-survey-only.

**Response counts were given an explicit grain.** An unqualified survey response count now means `COUNT(DISTINCT a.enrollment_id)` through answers, aliased `answered_people`. Enrollment counts, completed-person counts and answer-row counts each get explicit distinct names, and a generic `response_count` is prohibited.

**Foreign-survey refusal is terminal.** Made terminal at both the standing-prompt and tool-result levels. No retry, no current-side partial query, no substitute survey, no table or UUID repetition, no unprompted benchmark discussion, no clickable suggestion. Other SQL parser and guard errors remain repairable.

## Recorded user decisions

These read as standing constraints on future work rather than observations:

- Security enforcement must be **code-level**; prompts are supplementary and are never the boundary.
- Explicit navigation and comparison to any survey owned by the same client, across organizations, is allowed — work is not restricted to the initial survey.
- The configured benchmark exception stays, but is not used when the user excludes it.
- For unclear ordinary intent: softly name what was not supplied, make the strongest supported framing assumption, answer, and offer self-contained suggestions only for genuine remaining alternatives. Never invent a required tool parameter. Relates to [[suggestion-button-contract]].
- Prefer minimal patches. The user wants to run behavioral tests personally — do not run test plans or live agent tests unless asked.

## Verification status

Deliberately incomplete, and the note says so. A focused automated run passed **79/79** tests after the main boundary implementation, but that run **predates** the later response-count and terminal-refusal prompt refinements, and no automated rerun has happened since. Two user-supplied live traces are offered as behavioral evidence: a same-client success (one SQL call, two LLM calls, selecting the most recently published other same-client survey across organizations, returning 450 versus 2 answered people) and a foreign-client refusal (guard refused before execution, no second tool call, one concise sentence — the intended two-LLM-call terminal path).

The note is explicit that these traces are "strong behavioral examples, not a complete regression suite."

## Open questions

1. The latest refinements have no automated rerun.
2. Any newly exposed database table needs a verified entry in the tenant table-scope map, or it will correctly fail closed — a maintenance obligation, not a bug.
3. A cosmetic wording issue was noted and left unfixed: an answer said "225× larger" for 450 versus 2, where "225 times as many" is mathematically clearer. No code change was requested.

## Repository state recorded

Deployment is a nested git repository at `deployment/`, clean at commit `327da16` ("Compare surveys within the same client"). Root and deployment copies of `agent_instructions.py`, `tool_prompts.py`, `feature_influence.py` and `requirements.txt` were byte-identical at audit time. Runner files differ **intentionally**: the deployment runner requires environment-provided credentials, supports configurable model names and a DB connection timeout, and must not be replaced wholesale with the development runner or receive its credential defaults.

## Where this fits

Updates the tenancy account in [[sql-guardrails-and-scope]] and contests [[funda-agent-exp-architecture-doc]] — the contradiction is analysed in [[survey-authorization-scope-contradiction]]. Its unverified-refinement status feeds [[known-limitations-and-risks]]. Its testing constraint is relevant to [[regression-workbook]]. Concerns [[funda-agent-exp]].
