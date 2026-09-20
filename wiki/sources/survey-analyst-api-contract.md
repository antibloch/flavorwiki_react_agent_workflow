---
type: source
title: "FlavorAI Survey Analyst — API contract"
authors: ["FlavorAI project (internal document)"]
raw: "docs/API_CONTRACT.md"
ingested: 2026-08-14
tags: [agent-variant, deployment]
entities: [api-funda-agent-exp, funda-agent-exp]
concepts: [suggestion-button-contract, conversation-memory-and-trimming]
created: 2026-08-14
updated: 2026-08-14
---

The frontend integration reference for the HTTP surface of [[api-funda-agent-exp]]: three endpoints, the NDJSON event stream, the `reset` rule, the `{{suggestion}}` button contract, HTML sanitization requirements, timing expectations, and an explicit list of what the API does not do. Written adversarially — it repeatedly names the specific mistakes that make a correct integration look broken.

## Endpoints

`POST /ask` (described as the only endpoint a client needs), `GET /health`, and `DELETE /threads/{thread_id}`.

Only `prompt` and `survey_id` are required. `client_id` and `organization_id` are marked **"Do not send"** — they are derived server-side from `survey → organization → account`, and sending them overrides that lookup. When the chain is missing the request still succeeds and the `scope` event reports `resolved: false`. `no_inventory` is diagnostics-only.

## The response is NDJSON, not JSON and not SSE

`200` with `Content-Type: application/x-ndjson`, one JSON object per line, flushed as it happens. The document warns explicitly against `await response.json()` — it will not resolve until the run ends and will then fail to parse.

Event types: `status` (with stages `scope`, `inventory`, `running`), `token`, `reset`, `tool_result`, `done`, `error`.

Three guarantees the document says a client may rely on:

1. Exactly one terminal event per request — `done` **or** `error`, never both, always last.
2. `done.answer` always holds the complete final text, so a client that does not want live typing can ignore every other event.
3. Concatenating all `token` events since the last `reset` equals `done.answer` — marked "Verified."

## The `reset` rule

Singled out as "the one rule that is easy to get wrong." The agent is a reasoning loop: it may write prose, decide it needs to query the database, and start over. Prose from a turn that ends in a tool call is not the answer. Because finality is not knowable until a turn finishes, tokens stream live and a `reset` is emitted if that turn turned out to be a tool call. On `reset` the client must clear both the accumulated buffer and whatever it has rendered.

The document's stance on partial implementation is firm: either handle `reset`, or don't render tokens at all and just print `done.answer` on completion. Both are correct; a partial implementation is not.

## The answer is hybrid Markdown/HTML

Headings, paragraphs, lists and emphasis are Markdown; tables are raw semantic HTML; a persona response is one self-contained `section.persona-card`. Clients must enable raw HTML in the renderer and then sanitize, and the document specifies the allowlist precisely: a fixed tag set, a fixed `persona-card__*` class set, table attributes `scope`/`colspan`/`rowspan`, `open` on `details`, and the `style` attribute permitted **only** on table-related elements and `details`/`summary` with a named list of allowed declarations.

Two sanitization traps are called out because they fail silently:

- `details`, `summary` and `open` carry the parent → child result hierarchy. Dropping `open` collapses every block; dropping either tag flattens the hierarchy into an undifferentiated run of tables — silently, because sanitizers strip tags rather than fail.
- `br` is deliberately never emitted; multi-line cell content uses `<p>` or `<ul><li>` so that stripping unsupported break tags cannot collapse presentation.

## Timing and concurrency

A request takes **5–90 seconds**, most of it before the first token (measured: 7 s typical, 30 s for a multi-step question). Client timeouts must be at least 120 s. The first `status` arrives in ~100 ms, but there can be a silent 5–25 s gap between `running` and the first `token`, which is normal and must not be treated as a dropped connection.

**The server runs one request at a time.** A second request queues rather than being rejected, and `GET /health` reports `busy: true` while a run is in flight. The document states plainly that concurrency is a backend scaling change, not something a client can work around — consistent with the process-global `_SCOPE` explanation in [[funda-agent-exp-architecture-doc]] §14.4.

## Error surface

Two distinct shapes, and the second is the one to design for: a missing or invalid field produces **HTTP 422 before the stream opens**, but anything failing mid-run produces **HTTP 200 followed by an `error` event in the body**. Because the status code is already committed to 200 by the time anything can go wrong, a failure is a line in the body rather than a status code, and a client that only checks `response.ok` will silently show a blank answer. A stream ending without `done` or `error` should be treated as a dropped connection.

## Conversations and memory

Reusing a `thread_id` makes follow-ups elliptical. The survey is fixed when the thread starts — reusing the id with a different `survey_id` returns `thread_survey_mismatch`. Scope and the pre-fetched inventory live in message 0 and are not re-appended, so a thread started with `no_inventory: true` stays inventory-free for its lifetime.

`DELETE /threads/{thread_id}` should be called when a chat ends: memory is held in the server process, grows with the conversation (**~24 MB at 10 rounds**), and nothing expires it automatically. It is process-local, does not survive restart, and is not shared between workers — "a session convenience, not storage." Detail in [[conversation-memory-and-trimming]].

## Configuration visible through `/health`

`GET /health` reports `busy`, `model_strong: gpt-5.5`, `model_weak: gpt-5.4-mini`, `streaming`, `max_llm_steps: 12`, `history_max_messages: 10`, `tool_char_limit: 250000`, and `enable_digest: false`. If `streaming` is ever `false`, no `token` events are emitted and only `done` arrives — a server misconfiguration, not a client bug.

Two of these fields have no counterpart in [[funda-agent-exp-architecture-doc]]'s configuration table: `tool_char_limit` and `enable_digest`. Given that document's insistence that no digest or result-store path is wired in, `enable_digest: false` is plausibly a vestigial flag, but the wiki has no source confirming that.

## Not in this API

Listed as gaps with no client-side workaround: **no authentication** (anyone who can reach the URL can query the database), no cancellation (closing the connection does not stop the run), no pagination or partial results, no survey listing (the client must already know `survey_id`), and no request id in errors, so correlating a failure with server logs is manual.

CORS is open by default (`Access-Control-Allow-Origin: *`) with credentials not allowed, lockable via `CORS_ORIGINS`.

## Where this fits

The authoritative account of [[api-funda-agent-exp]]'s external surface. Its suggestion rules are detailed in [[suggestion-button-contract]]; its memory account in [[conversation-memory-and-trimming]]. Its "no authentication" gap corroborates [[final-agent-work-bundle-readme]] and feeds [[known-limitations-and-risks]]. Concerns [[funda-agent-exp]].
