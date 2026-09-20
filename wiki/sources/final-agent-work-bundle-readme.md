---
type: source
title: "final_agent_work — bundle README"
authors: ["FlavorAI project (internal document)"]
raw: "README.md"
ingested: 2026-08-14
tags: [agent-variant, benchmark, prompt-design]
entities: [funda-agent-exp, api-funda-agent-exp, regression-workbook, survey-inventory-packet]
concepts: [sql-guardrails-and-scope]
created: 2026-08-14
updated: 2026-08-14
---

The operator-facing README for this working copy, describing it as a self-contained bundle copied out of `flavorai_v2` containing everything needed to run, serve, benchmark and understand [[funda-agent-exp]]. Its most load-bearing content is a security warning, an inventory of what was deliberately left behind, and a precise account of the regression workbook.

## The security warning

The README leads with it, before the layout. `funda_agent_exp.py` **hardcodes live credentials** — an OpenAI API key, the `DATABASE_URI`, and the Charts API secret. `os.getenv` still takes precedence so a real `.env` overrides them, but the literals are in the file, and they are also already in the git history of the source repository. The README's conclusion is that **rotation is required** before this is shared, deployed or pushed anywhere, and that deleting the lines is not sufficient.

Separately: `api_funda_agent_exp.py` has **no authentication**. Anything that can reach the port can query the database through it. Corroborated independently by [[survey-analyst-api-contract]] §12 and [[funda-agent-exp-architecture-doc]] §16.1. Collected in [[known-limitations-and-risks]].

Per this wiki's schema rule, the credential values are not reproduced here.

## Where the prompts live

Every string the model reads comes from exactly two structured modules: `agent_instructions.py` (persona, task, schema map, system prompt, inventory preamble, scope text, loop instructions) and `tool_prompts.py` (tool descriptions plus the feedback tools return). The agent imports both and contains **no model-facing text of its own**. Corroborated by [[funda-agent-exp-architecture-doc]] §2.

## Runtime facts that correct stale comments

- **Redis is not required.** A comment at `funda_agent_exp.py:72` says parked results go to Redis; the module actually imported is `output_store.py`, which keeps them in an in-process `OrderedDict` bounded by `MAX_STORED_RESULTS=32`. Nothing connects to Redis. Note that [[funda-agent-exp-architecture-doc]] states no result store is wired into the current graph at all, so `output_store.py` appears to be vestigial rather than active — the README documents the module's existence, not its use by the graph.
- PostgreSQL is required at `DATABASE_URI` — in the source repo a docker container on port 5433 restored from `BE/db/updated_dump.sql`, taking roughly five minutes.
- Inventory caching TTLs are named directly: `INVENTORY_CACHE_ACTIVE_TTL_S`, `INVENTORY_CACHE_CLOSED_TTL_S`, and `INVENTORY_CACHE_BENCHMARK_TTL_S` (24 hours by default). Detail in [[survey-inventory-packet]].
- The on-demand packet tool is described as restricted to the current survey, a survey in the current organization, or the configured fixed benchmark — the **organization**-scoped framing that [[client-boundary-session-handoff]] later contradicts. See [[survey-authorization-scope-contradiction]].

## Invocation

Three entry points: a single `--prompt` run, a `--chat` multi-turn session where memory is per `--thread-id`, and `uvicorn api_funda_agent_exp:app` for the server. A streaming client lives at `scripts/ask_stream.py`. Only `prompt` and `survey_id` are required — `client_id` and `organization_id` are derived server-side from the survey.

## The regression workbook

Documented in detail, including three warnings about using it. Covered in [[regression-workbook]].

## Deliberately not copied

Four scripts were left out because they benchmark this agent against **sibling** agents and need files outside the bundle — `bench_needle.py` and `run_needle_bench.sh` (need `funda_agent_v1.py`), `run_drill_ablation.sh` (needs `funda_agent.py`), and `summarize_drill_ablation.py` (needs the output of those). The README's judgment: their results are already written up in `docs/emp_findings.md` and `docs/needle_haystack_benchmark.md`, so nothing is lost except the ability to re-run those two cross-agent comparisons here.

`funda_agent_exp_oracle_duo.py` **is** included — it is the baseline arm the memory benchmarks A/B against, and the README instructs keeping it byte-identical.

## Suggested reading order

The README nominates `docs/agent_exp_doc.md` as the main document, pointing at its architecture, tools, and known-limitations sections, plus `docs/API_CONTRACT.md` and `openapi.json` for frontend work. Note that the section numbers it cites (§3 architecture, §5 tools, §7 parking, §12 limitations) **do not match** the current numbering of that document (§5 topology, §10 tools, §16 limitations), and it references result parking, which the architecture document says no longer exists — so the README's pointers appear to predate a restructuring of the document it points at.

## Where this fits

Introduces [[funda-agent-exp]], [[api-funda-agent-exp]] and [[regression-workbook]]. Its security warning and Redis correction feed [[known-limitations-and-risks]]. Its packet-scope description is one side of [[survey-authorization-scope-contradiction]]. Corroborates the prompt-module structure documented in [[funda-agent-exp-architecture-doc]].
