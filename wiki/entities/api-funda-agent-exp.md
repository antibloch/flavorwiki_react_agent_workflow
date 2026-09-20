---
type: entity
kind: module
title: "api_funda_agent_exp.py"
aliases: ["api_funda_agent_exp", "FlavorAI Survey Analyst API"]
tags: [agent-variant, deployment]
sources: [survey-analyst-api-contract, funda-agent-exp-architecture-doc, final-agent-work-bundle-readme]
created: 2026-08-14
updated: 2026-08-14
---

The FastAPI wrapper that exposes [[funda-agent-exp]] over HTTP with token-by-token NDJSON streaming. It owns API-side scope resolution, the checkpoint lifecycle, and the one-run-at-a-time lock.

## Surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ask` | Ask a question; streams the answer |
| `GET` | `/health` | Liveness and active configuration |
| `DELETE` | `/threads/{thread_id}` | Free a conversation's memory |

Callers supply `prompt` and `survey_id`. `client_id` and `organization_id` are documented as "do not send" — the API derives them, and an explicit value overrides the lookup.

## Scope resolution

The API attempts the chain `survey.organization_id → organization.account_id → account.client_id`. If it is unavailable, the API publishes **survey-only scope** and switches to a special prompt that forbids unbound client/org placeholders; historical sibling lookup is then unavailable but the fixed benchmark remains reachable. The `scope` status event reports `resolved: false` in that case.

Before every request `_SCOPE` is cleared and rebuilt, so a previous tenant's value cannot survive into the next run. Authorization semantics themselves are covered in [[sql-guardrails-and-scope]].

## Streaming protocol

NDJSON — one JSON object per line, flushed as it happens. Not SSE, not a JSON array.

| Event | Meaning |
|---|---|
| `status` | Stages `scope`, `inventory`, `running` with their metadata |
| `token` | A fragment of candidate AI text, appended in order |
| `reset` | The streamed turn issued tools — discard accumulated text |
| `tool_result` | Tool name and output character count |
| `done` | Terminal: `answer`, `thread_id`, `turns`, `wall_s`, `tokens{}` |
| `error` | Terminal in-stream failure |

Only `AIMessageChunk` content is emitted as tokens; `ToolMessage` content is never streamed to the client. Persona and ordinary analytical responses both stream token-by-token, and persona presentation is best-effort under the prompt contract rather than buffered for validation or repair.

### The `reset` rule

Because the agent is a reasoning loop, prose from a turn that ends in a tool call is not the answer, and whether a turn is final is unknowable until it finishes. Tokens therefore stream live and a `reset` is emitted retroactively. Clients must clear both buffer and rendered output on `reset`, or skip token rendering entirely and use `done.answer`. [[survey-analyst-api-contract]] states that a partial implementation of this is not acceptable.

### Failure shape

A missing or invalid field yields HTTP **422** before the stream opens. Anything failing mid-run yields HTTP **200** followed by an `error` event, because the status code is committed before the failure can occur. A client checking only `response.ok` will silently render nothing.

## Thread rules

For a new thread the API adds scope and optionally inventory to message 0; for a follow-up it reuses that anchor and does not re-fetch or duplicate the inventory. A thread created without inventory stays inventory-free. A repeated `thread_id` whose stored `thread_survey_id` differs from the request's survey is rejected with `thread_survey_mismatch` — the thread must be deleted or a new id chosen. Memory behaviour in [[conversation-memory-and-trimming]].

## Concurrency

Because the agent's `_SCOPE` is a process global, the API serializes complete runs behind a single `asyncio.Lock`; concurrent requests queue rather than interleave tenant state. `GET /health` reports `busy: true` during a run.

Multiple Uvicorn worker processes give process-level concurrency, but each worker has its own checkpoint, scope cache and inventory cache, so a follow-up must reach the same worker unless shared state is introduced. True in-process concurrency would require moving `_SCOPE` into request/graph state and passing scope explicitly to every tool.

## Timing expectations

5–90 seconds per request, most of it before the first token (7 s typical, 30 s for a multi-step question). First `status` in ~100 ms; a silent 5–25 s gap between `running` and the first `token` is normal. Client timeouts should be at least 120 s.

## Security posture

**No authentication.** Anything that can reach the port can query the database through it. CORS is open by default with credentials disallowed, lockable via `CORS_ORIGINS`. There is also no cancellation, no pagination, no survey listing, and no request id in errors. Collected in [[known-limitations-and-risks]].

## Related

[[funda-agent-exp]] · [[survey-analyst-api-contract]] · [[suggestion-button-contract]] · [[survey-inventory-packet]]
