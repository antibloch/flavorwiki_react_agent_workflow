# Wiki Log

Append-only chronological record of operations on the wiki. Each entry begins with `## [YYYY-MM-DD] <op> | <description>` so it's parseable with `grep "^## \[" log.md | tail -N`.

Operations:
- `ingest` — a source was processed into the wiki.
- `query` — a question was answered against the wiki (typically only logged when the answer was filed back as synthesis).
- `lint` — a health check was run.
- `schema` — the schema was modified.
- `shard` — an index was sharded.

---

## [2026-08-14] schema | Initialized wiki; seeded tag taxonomy and ingest scope rules
   Six tags seeded; `db/` and `deployment/*.py` excluded from ingest; in-repo sources referenced in place rather than copied to `raw/`.

## [2026-08-14] ingest | funda_agent_exp.py — current architecture and execution flow
   +[[funda-agent-exp-architecture-doc]]; created [[funda-agent-exp]], [[survey-inventory-packet]], [[langgraph-react-topology]], [[model-and-prompt-routing]], [[progress-ledger]], [[parallel-tool-calling]], [[conversation-memory-and-trimming]], [[sql-guardrails-and-scope]].

## [2026-08-14] ingest | final_agent_work — bundle README
   +[[final-agent-work-bundle-readme]]; created [[regression-workbook]]. Flagged hardcoded credentials and stale section pointers.

## [2026-08-14] ingest | Session Handoff — code-enforced client boundary
   +[[client-boundary-session-handoff]]. Contradicts the architecture doc on tenancy scope and SQL enforcement.

## [2026-08-14] ingest | FlavorAI Survey Analyst — API contract
   +[[survey-analyst-api-contract]]; created [[api-funda-agent-exp]], [[suggestion-button-contract]].

## [2026-08-14] query | What is the architecture of the current agent?
   Filed back as [[agent-architecture-overview]]. Also produced [[survey-authorization-scope-contradiction]] (contradiction resolved against `funda_agent_exp.py`) and [[known-limitations-and-risks]].

## [2026-08-14] query | What is the agent's memory? / What is in the 10-message window? / Is there cache-prefix reuse at round 3?
   Answered from [[conversation-memory-and-trimming]]; the last two exceeded what the documents state, so both were resolved against `funda_agent_exp.py:2756-2830` and filed back.
   Updated [[conversation-memory-and-trimming]]: verified trim algorithm (`[msgs[0]] + msgs[start:]`, message-object unit, `> HISTORY_MAX_MESSAGES + 1` threshold, `round_start` clamp, backward walk over orphan `ToolMessage`s, injected messages never entering state) and a new cross-round prompt-cache section.
   Updated [[model-and-prompt-routing]]: corrected the cached-token capture's scope — `turns: 3` is three LLM turns in one request, not three conversation rounds.
