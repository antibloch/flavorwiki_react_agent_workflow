# Wiki Index

The catalog of all pages in this wiki. Each entry: a wikilink to the page and a one-line summary. The LLM reads this first when answering queries to identify candidate pages.

Keep summaries tight — one line each. The index is engineered to be cheap to read; a fat index defeats its purpose.

When this file exceeds ~300 lines or the wiki passes ~150 pages, shard into `wiki/indexes/<type>.md` and replace this file with a directory of shards. See the `scaling-playbook.md` reference in the `llm-wiki` skill for the migration procedure.

**Start here:** [[agent-architecture-overview]] for how the agent works, [[known-limitations-and-risks]] for what to watch out for.

---

## Sources

- [[funda-agent-exp-architecture-doc]] — the 806-line primary architecture reference (`docs/agent_exp_doc.md`); audited 2026-08-10 and now stale on tenancy.
- [[client-boundary-session-handoff]] — 2026-08-12 handoff recording the code-enforced client boundary; the current word on authorization.
- [[final-agent-work-bundle-readme]] — operator README: hardcoded-credential warning, prompt module layout, what was deliberately not copied.
- [[survey-analyst-api-contract]] — frontend integration contract: NDJSON events, the `reset` rule, sanitization allowlist, timing, and what the API does not do.

## Entities

- [[funda-agent-exp]] — the LangGraph survey-analysis agent itself: capabilities, tool surface, configuration defaults, invariants.
- [[api-funda-agent-exp]] — the FastAPI/NDJSON wrapper: endpoints, scope resolution, thread rules, one-run-at-a-time concurrency.
- [[survey-inventory-packet]] — the one-statement analytical packet and its persona variant; complete-or-absent, lifecycle-TTL cached.
- [[regression-workbook]] — `regression.xlsx`: 69 rows across six suites, and the four traps in using it.

## Concepts

- [[langgraph-react-topology]] — the two-node graph, `AgentState`, and the 12-turn budget that ends in an unbound-tools summary.
- [[model-and-prompt-routing]] — strong/weak models, full/lean prompts, and the two latches that decide when retrieval is done.
- [[sql-guardrails-and-scope]] — the SQL validation pipeline, why semantic filtering is banned, and the fail-closed tenancy envelope.
- [[progress-ledger]] — model-authored evidence tracking that drives the completion latch and the stall diagnostics.
- [[parallel-tool-calling]] — provider-level parallelism narrowed by Python to SQL-only batches, in deterministic call order.
- [[conversation-memory-and-trimming]] — checkpointing versus model-visible history, the verified trim algorithm, and cross-round prompt-cache behaviour.
- [[suggestion-button-contract]] — the `{{...}}` protocol, its guarantees, and why it must be parsed positionally.

## Synthesis

- [[agent-architecture-overview]] — **answers "what is the architecture of the current agent?"** — the eight layers and the model/Python division of authority.
- [[survey-authorization-scope-contradiction]] — two sources disagree on tenancy scope and enforcement; resolved against the code, with the stale claims recorded.
- [[known-limitations-and-risks]] — every known gap, risk and tracked failure, grouped by what a reader would act on.
