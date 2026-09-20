# PROTOCOL.md — agent components and the local ↔ deployment sync contract

Read this before changing anything under `funda_agent_exp.py` or `deployment/`.
It describes (1) what each file in the agent does, (2) how the two trees relate, and
(3) exactly what must be synced between them and what must never be.

---

## 0. Host adapter — capabilities, not hosts

This protocol is written for **any coding agent**. Where a step needs something only the host
can provide, it names a **capability** in `SMALL-CAPS`, and this table says how each supported
host supplies it. Nothing else in this document names a host or a host-specific tool; if you
find one, it is a defect — fix it here rather than working around it.

| Capability | What it must provide | Claude Code | Codex CLI |
|---|---|---|---|
| `LOAD-PROCEDURE <name>` | The full text of `.claude/skills/<name>/SKILL.md`, followed literally | `/name` — native `SKILL.md` discovery | Native skill discovery via `.codex/skills/` (see below); name the procedure or write `$name` |
| `ISOLATED-GRADER <name>` | The procedure run in a context that has **no memory of the fix** and did not write it | Subagent (Task tool) | `codex exec` as a child process — a new process, empty context |
| `PREAPPROVE-CMDS` | The DB/stats helper scripts runnable without a prompt per call | `.claude/settings.json` `permissions.allow` | `~/.codex/config.toml` sandbox + approval policy |

`ISOLATED-GRADER` is the one that carries a correctness property rather than a convenience:
§2 and §6 step 4 require the grader to not be the session that wrote the fix. A separate OS
process satisfies that at least as strictly as an in-session subagent — what matters is the
empty context, not the mechanism. **Never satisfy it by grading inline**, on any host.

On Codex specifically, `codex exec` is not a fallback but the only correct mechanism, for two
independent reasons: `codex exec` exposes no delegation tool, and Codex's own skill rules
forbid delegating the reading or interpreting of a `SKILL.md` to a subagent — so a grader must
be a fresh *main* agent that reads the procedure itself.

**One canonical copy, two discovery roots.** The four procedure documents live under
`.claude/skills/` and are plain host-neutral Markdown plus `bash`/`python3` scripts.
`.codex/skills/` holds **relative symlinks** to them — `oracle_agent`, `contract_review`,
`trace_compare`, `agent-prompt-mapper` — which is a root Codex scans; it follows the links and
lists all four natively. Symlinks, not copies, so there is exactly one canonical text and nothing can drift.
Do not fork per-host variants. (Codex also scans `.agents/skills/`, which works identically
but is gitignored at the repository root and so would not survive a clone.) `AGENTS.md` in this directory is a thin pointer to this
file for hosts that auto-load it, and carries no rules of its own.

**Host requirements.** Both supported hosts run **locally on this machine**, because
`oracle_agent` queries the `gpi-db` Docker container on port 5433 and the live Charts API
(§3). A host without them cannot run the content grader at all; see §6 step 4 for what to
record when a grader cannot run.

---

## 1. Two trees, one agent

This directory — `final_agent_work_v5_base_2_optim3_exp/` — is the whole working area. Nothing
above it is in scope: do not read, edit, commit or reason about anything outside it.

| | path | role |
|---|---|---|
| **Working tree** | this directory | where all development and testing happens, against the local Docker DB. **Every change starts and stays here.** |
| **Deployment** | `deployment/` | `git@github.com:Flavorwiki-development/herbalife_agent.git`, branch `main`. The deployable copy that is built into the Docker image. |

The order is fixed: **change here → the user runs it and approves → then, and only then, sync into
`deployment/`** so the user can push it to GitHub, from where it is deployed to the server (§6, §7).
Nothing is written into `deployment/` before that approval.

The two trees hold the **same agent code** with **deliberately different configuration
handling**. Agent behaviour must stay identical; configuration must stay divergent.

---

## 2. Component map

### Main agent
| file | contains |
|---|---|
| `funda_agent_exp.py` | Everything runtime: LangGraph graph, `AgentState`, tool implementations, tool registration, DB access, scope/authorization, history trimming, CLI `main()`. |
| `api_funda_agent_exp.py` | FastAPI wrapper (`/health`, streaming chat). Entry point for the container. |

Graph shape (`build_graph`):

```
StateGraph(AgentState)
  ├── node "LLM"   → call_model
  ├── node "tools" → call_tool
  ├── conditional edge  LLM → tools | END
  └── edge              tools → LLM
compile(checkpointer=InMemorySaver())
```

### Model-facing text — primary instruction modules
Keep behavioural rules and tool descriptions in these two modules, imported by
`funda_agent_exp.py`. Model-visible argument schemas and some runtime feedback also live in
the runtime module; the prompt map includes those sources and their delivery paths.

| file | contains |
|---|---|
| `agent_instructions.py` | The system prompt and every behavioural rule block; `build_system_prompt()` assembles: role/goal/backstory → `PG_DIALECT_RULES` → `PROGRESS_MEMORY_RULES` → expected-output + `NESTED_RESULT_RULES` + `REPORTING_RULES` + `PERSONA_RULES` + `WORD_CLOUD_RULES` + `PLSR_RULES` → `SCHEMA_OVERVIEW`. Also `INVENTORY_PREAMBLE`, `scoped_query()`, `scoped_query_survey_only()`, `zero_row_streak_warning()`, `STEP_BUDGET_NOTICE`. |
| `tool_prompts.py` | One description constant per tool (`NL2SQL_TOOL_DESCRIPTION`, `RUN_SURVEY_STATS_DESCRIPTION`, `ANALYZE_PLSR_DESCRIPTION`, `GENERATE_WORD_CLOUD_DESCRIPTION`, `GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION`), the `*_RESULT_HEAD` markers, and tool-feedback strings (`SQL_ERROR_HINTS`, `SEMANTIC_FILTER_REASON`, rejection messages). |

Rule of thumb: **what the tool is for** → `tool_prompts.py`. **How to render the answer /
when to segment / what never to claim** → `agent_instructions.py`.

### Prompt map — `docs/generated/` (find the prompt before you touch it)

`docs/generated/agent-prompt-map.md` and `docs/generated/agent-prompt-map.json` are a
generated inventory of every model-facing instruction in `agent_instructions.py` and
`tool_prompts.py`: what it says, where it is defined (file, symbol, start/end line), how it
reaches the model (`delivery_paths`), and under what condition it is included. They are
produced and kept current with the **`agent-prompt-mapper`** procedure, vendored in-repo at
`.claude/skills/agent-prompt-mapper/` so it runs from a fresh clone on either host
(`LOAD-PROCEDURE agent-prompt-mapper`, §0) — they are generated artifacts, not hand-written.

**Use the map during diagnosis too.** When step 0 implicates model-facing behaviour, traverse
the relevant records to identify candidate instructions, delivery conditions and interacting
fragments. The map establishes static relevance and reachability; the run's assembled context
or a controlled check establishes delivery on that path; the discrimination probe tests the
causal explanation. A matching facet, a shared workflow or a present rule alone does not prove
which instruction caused the behaviour. Check competing rules and worked examples as well as
the suspected fragment. Keep prompt IDs and evidence in §6's case record.

**Before editing any model-facing text**, traverse `agent-prompt-map.json` to localize the
exact instruction record for what you intend to change — its `source.symbol`,
`source.start_line`/`end_line`, `used_by`, `delivered_via`, `condition`, and, for a composed
prompt, its `composed_from` order — instead of grepping the raw file or eyeballing a
paragraph. Use that record to make a **precision-strike edit**: touch only the lines the
record identifies, and check `used_by`/`composed_from` for any other consumer of the same
fragment before changing its meaning. `agent-prompt-map.md` is the human-readable walk of
the same data — read it when a record needs the plain-English summary or the full rendered
content of a fragment.

**Scope the map from both entrypoints.** The import closure must start at `funda_agent_exp.py`
**and** `api_funda_agent_exp.py`. A CLI-only map can omit `scoped_query_survey_only`, the degraded
SCOPE preamble the API sends whenever the client/org lookup fails, while reporting complete
coverage of that narrower scope. Check
`coverage.included_roots` on any regenerated map, and treat an empty `used_by` as a question
about the map's scope before treating it as a fact about the code.

Check the affected records' source locations, hashes and delivery paths before relying on
them. If stale or missing, inspect the named source symbols and record the mapping gap rather
than treating absence from the map as absence from the agent. Editing either prompt file, a
model-visible schema, runtime feedback or its delivery path requires refreshing the affected
records with `agent-prompt-mapper` before the next traversal. The implementation still wins on
any conflict. `docs/generated/` is tracked in this tree — the
map must be readable from a fresh clone — but it is local-only for sync purposes (see below):
never sync it into `deployment/`.

### Tools — all defined in `funda_agent_exp.py`, registered by `build_tools()`

`build_tools()` is the authoritative list of what is bound. A tool named in a prompt but absent
from it is a dead promise the model will try to use — check both directions when a tool
description changes.

| tool | description constant |
|---|---|
| `nl2sql_tool` | `NL2SQL_TOOL_DESCRIPTION` |
| `get_survey_analysis_packet` | `GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION` |
| `run_survey_stats` | `RUN_SURVEY_STATS_DESCRIPTION` |
| `analyze_plsr` | `ANALYZE_PLSR_DESCRIPTION` |
| `generate_word_cloud` | `GENERATE_WORD_CLOUD_DESCRIPTION` |

Each tool follows the same three-part pattern:

```python
class XInput(BaseModel): ...          # pydantic args schema
def analyze_x(...) -> str: ...        # returns RESULT_HEAD + "\n" + json.dumps(payload)
analyze_x.__doc__ = X_DESCRIPTION     # from tool_prompts.py
analyze_x = tool(args_schema=XInput)(analyze_x)
```

`InfluenceVariable` is `analyze_plsr`'s variable schema — it strips whitespace from
`question_id` (a copied UUID with a trailing space otherwise costs a whole model turn).
It and the `_*_influence_*` row-fetch/spec-resolution helpers keep their names from the
removed feature-influence tool; they are PLSR's now.

### Rule blocks — `agent_instructions.py`

Locations come from the prompt map, not from here (see "Prompt map" above).

| block | governs |
|---|---|
| `NESTED_RESULT_RULES` | when a wide tool result becomes a `gpi-nested-table` artifact |
| `SCHEMA_OVERVIEW` | the DB map appended last (stable cache suffix) |
| `PG_DIALECT_RULES` | Postgres constructs that are rejected outright |
| `REPORTING_RULES` | general answer shape, counts, suggestions |
| `PERSONA_RULES` | persona/roster/segmentation and its output contract |
| `PLSR_RULES` | PLSR table rendering + attribute-selection disclosure |
| `WORD_CLOUD_RULES` | when a word-cloud block may be emitted |
| `PROGRESS_MEMORY_RULES` | the `<progress_gathered>` block |

### Agent architecture — the turn lifecycle

| stage | symbol |
|---|---|
| scope published (`_SCOPE`) before any tool can bind ids | `funda_agent_exp._SCOPE` |
| prompt scoping — run ids injected into the user turn, not the system prompt | `agent_instructions.scoped_query()`, and `scoped_query_survey_only()` on the API's degraded path |
| inventory prefetch — survey catalog attached to turn 0 | `survey_inventory()` |
| LLM node | `call_model` |
| routing (tools vs END) | `should_continue` |
| tool node | `call_tool` |
| graph assembly | `build_graph` |
| CLI entry | `main` (`--chat` continues a thread) |
| HTTP entry | `api_funda_agent_exp._run` |

The system prompt is a pure function of config, so it is byte-identical across surveys and
stays a stable prompt-cache prefix. Per-run ids arrive in the **user** message.

### Harness elements

**Parallel tool calling.** `call_tool` fans homogeneous batches of `nl2sql_tool` or
`run_survey_stats` calls in one assistant message across `_TOOL_EXECUTOR` via
`_invoke_tool_call`, then logs `[tool-batch-timing] tool=… calls=N wall=…`.
Mixed batches and other tools execute sequentially. Same-turn calls buy parallelism only on
the supported batch path; check that branch before diagnosing a batching defect.

All defined at the top of `funda_agent_exp.py`; the defaults there win over this table.

| constant | default |
|---|---|
| `MAX_PARALLEL_SQL_CALLS` | 6 (env) |
| `MAX_PARALLEL_TOOL_CALLS` | inherits the above (env) |
| `STATEMENT_TIMEOUT_MS` | 20000 |
| `_CHARTS_TIMEOUT_S` | 30 |
| `INVENTORY_CACHE_MAX_ENTRIES` | 128 (env) |

**Model.** `MODEL_NAME`; `ChatOpenAI` built once at module level; reasoning effort resolved by
`_reasoning_effort`/`_llm_tuning`. Streaming is on.

**SQL guardrails** (all in `funda_agent_exp.py`): `sqlglot` parse + statement-type checks,
`_reject_reason`, the semantic-filter detector `_SEMANTIC_FILTER_RE`
whose model-facing explanation is `tool_prompts.SEMANTIC_FILTER_REASON`, read-only
transactions with a server-side statement timeout, and scope repair bounded by
`_SCOPE_REPAIR_MAX_EDITS`. Each of these is a step-0 boundary in its own right: a wrong value
may be a rejection, a repair, or an authorization narrowing rather than a bad answer.

**Trusted artifacts.** Word-cloud and PCA payloads never pass through model-authored text.
They are parked in `AgentState` and spliced in at final assembly —
`_WORD_CLOUD_ARTIFACT_PREFIX`, `_assemble_pca_final`. Never let the
model re-emit or re-round these numbers.

**Output-reliability reasoning — connect diagnosis to correction.** Use §6 step 0's actual
path and state trace to localize the failure, and the prompt map to investigate relevant
instructions. Then use `LLM_FLOW.md` and `CONTROL_FLOW.md` to choose a correction tied to the
observed mechanism. These are output-ownership recipes, not execution maps of this agent.

Note the name: `CONTROL_FLOW.md` is about deterministic **output ownership**, not this agent's
control flow. For routing, step budget, retry and termination behaviour there is no document —
read `should_continue`, `call_model`'s step-budget branch, and `_trim_history`, and see step 0
on reading the halt reason off a transcript.

Choose separately who **produces** the output, what provides its factual **authority**, and
who **enforces** its contract. LLM-produced output may still need harness grounding, schemas
or verification; runtime-produced output still needs valid source data. Put the choice, its
causal rationale and its verification in §6's case record. Follow only the relevant recipe;
no additional design document or whole-agent branch map is required.

### Numeric engines — no LangChain, no DB
Kept separate so synthetic tests can drive them directly.

| file | contains |
|---|---|
| `plsr.py` | NIPALS PLS1, VIP scores, product-mean aggregation. Answers *ranking* on wide/collinear designs. Reproduces sklearn's `PLSRegression`; equivalence pinned by `tests/plsr_sklearn_fixture.json`. |

**numpy only.** scipy / scikit-learn / statsmodels / pandas are *not* installed and adding
them needs explicit approval — see §5.

### Memory management
Three independent mechanisms; do not confuse them.

1. **Checkpointed conversation** — `InMemorySaver` keyed by `--thread-id`. Lives for the
   process only. `messages` uses the `add_messages` reducer; it is the only reducer on the
   state.
2. **History trimming** — `_trim_history()`. Keeps the first message plus at
   most `HISTORY_MAX_ROUNDS` recent user rounds; `HISTORY_MAX_MESSAGES` is a secondary
   ceiling that drops additional *complete* old rounds. Both are env-overridable.
   `MAX_LLM_STEPS` bounds turns within one round.
3. **Progress memory** — `PROGRESS_MEMORY_RULES` in `agent_instructions.py`. The model
   writes a `<progress_gathered>` block into its own message before any further tool call.
   It is an *index* to tool results, never a substitute for them. `_progress_delta_status()`
   parses it for the `[progress-delta]` log line.

All three at the top of `funda_agent_exp.py`.

| constant | default |
|---|---|
| `MAX_LLM_STEPS` | 12 |
| `HISTORY_MAX_ROUNDS` | 5 (env) |
| `HISTORY_MAX_MESSAGES` | 47 (env) |

`AgentState` also carries out-of-band slots so trusted artifacts never pass through
model-authored text, alongside the per-round scratch channels that let a follow-up round start
clean. The `AgentState` TypedDict in `funda_agent_exp.py` is the list — read it there; a copy
here goes stale silently and the comments on each field say what it is for.

### `tests/` — what it is for
Offline unit tests for the parts of the agent that can be checked **without an LLM and without
the DB**: the numeric engines, and the harness invariants around them. They are the fast gate in
§6 step 2 — they prove the arithmetic and the plumbing, and say nothing about whether the model
follows a prompt rule. Run with `.venv/bin/python -m unittest` (no `pytest` here, §3).

| file | covers |
|---|---|
| `test_plsr.py` | `plsr.py` reproduces scikit-learn's `PLSRegression` — coefficients, VIP, MSE/RMSE/R2 — pinned against `plsr_sklearn_fixture.json`, plus input guards and `aggregate_by_unit`. This is why sklearn is not a runtime dependency (§5). |
| `plsr_sklearn_fixture.json` | The committed reference output, generated once with scikit-learn 1.9.0 in a throwaway venv. Regenerating it needs sklearn; the test bundle never imports it. |
| `test_clustering.py` | `clustering.py` — k-means over respondent attribute-rating profiles, the engine behind `cluster_rating_profiles`. Passes. |
| `test_agent_optimizations.py` | Harness-level invariants (nl2sql latency, PCA stats tool, chart sanitising, …). ⚠️ Fails to import — `ModuleNotFoundError: attribute_kpi_workflow`, which lives in `not_used/` (out of scope) and is not restored. **Restoring the import would not make it green:** while a stray root copy of that module was making it importable (16 Sep 2026), it ran 324 tests with **22 failures and 60 errors**, most asserting prompt text and symbols that no longer exist — `attribute_kpi_analysis`, `_isolated_workflow_llm`, `_result_reader_model`, `persona_profile_block`, `is_persona_cluster_request`. It targets the retired isolated-workflow / attribute-KPI architecture. Treat it as retired-pending-rewrite, not as a gate. Unrelated to the agent modules. |
| `live_optimization_regression.py` | Not a unit test — live instrumentation for specific `regression.xlsx` rows. Needs the BE environment. |

What `tests/` cannot tell you: whether the agent *behaves*. Every prompt-rule and rendering change
is verified by running the agent against the local DB. `regression.xlsx` (§6.1) is the primary
source for probes — pick or adapt a `Prompt`/`Suite`/`Case` row that already exercises the changed
behaviour. When no row fits, drill the local DB directly (§3's `query.sh`) to find real scope and
build a new probe. `probe_scratchpad.md` tracks the current and recently-run probes and their
status.

### Local-only support
`tests/`, `scripts/`, `db/`, `docker-compose.yml`, `pyproject.toml`, `uv.lock`,
`AGENTS.md`, `*.md` design notes and probes (`probe_scratchpad.md`, …), `docs/` (including
the generated prompt map, `docs/generated/agent-prompt-map.{md,json}` — see §2 above),
and `.claude/` (the `oracle_agent`, `contract_review`, `trace_compare` and
`agent-prompt-mapper` procedures, plus `settings.json`), `.codex/skills/` (symlinks to the same
four, for Codex discovery —
§0) and the root `.env` `oracle_agent` reads for DB/Charts-API config.
**None of these belong in `deployment/`.** The evaluation harness under
`.claude/skills/oracle_agent/scripts/` — `capture_agent.py` (the agent under test, with a
manifest), `probe_manifest.py`, `trace_log.py` (the oracle's trajectory log) and the oracle's
own tool counterparts `query.sh`, `stats_api.py`, `sql_stats.py`, `plsr_ref.py`,
`word_cloud.py`, `resolve_scope.py`. Only `capture_agent.py` imports `funda_agent_exp.py`, and
only to run its CLI; nothing edits it, so none of this adds a sync obligation or moves §8's
parity figures.

### Independent verification — one producer, two judges
All three run as an `ISOLATED-GRADER` (§0) in this repo — the same model, the same
`gpi-db`, the same tools available to any session here. What each one adds is not a
different AI; it is a tested asset the fixing session shouldn't have to redo from memory
every time: `oracle_agent` packages a from-scratch statistics engine
(`sql_stats.py`, validated against published critical values) plus a checklist of this
dataset's specific traps (multi-attribute batteries pooled under one question label,
`Tasting Survey`'s disjoint 1200/1200 cohorts, the `positionLabels` JSON path);
`contract_review` packages the running cross-check against `probe_scratchpad.md`'s
already-accepted exceptions; `trace_compare` packages the list of structural asymmetries
between the two runs that are by design and must never be reported as defects.

**The producer does not judge, and the judges do not derive.** `oracle_agent` makes the two
references and verdicts nothing; `trace_compare` and `contract_review` verdict and derive
nothing. A procedure that produced its own standard would be checking its own work, which is
the same failure `ISOLATED-GRADER` exists to prevent, one level up. Grading a
probe's transcript is split across the two judges, each
covering the area it is actually equipped for and neither touching the other's:

- **`.claude/skills/oracle_agent/`** — the **producer**. It grades nothing at all. Given a
  `(prompt, client, organization, survey)` question it makes two references: **Phases 0-5**
  re-derive the correct answer from `gpi-db` and the live Charts API, independent of the
  target agent's own tool calls — that is the **content standard**; **Phase 6** runs the
  oracle itself, in the Claude Code harness, over its own counterparts of the agent's tools,
  recording every step to a trajectory log — that log plus its own answer is the **trajectory
  and coverage standard**, and it is treated as the ideal. All three are handed to
  `trace_compare`. It has no
  memory of this repo's prompt-engineering history, so it must never be asked about
  presentation or behavior either — it lacks the context to tell an accepted exception from a
  regression (see `contract_review` below).
- **`.claude/skills/contract_review/`** — presentation/behavior correctness only. Reads the
  live rule text in `agent_instructions.py`/`tool_prompts.py` — chart shape,
  `NESTED_RESULT_RULES`, benchmark banner, suggestion-chip contract, disclosure timing, and
  the tool calls themselves (tool chosen, arguments, batching, order) — and checks a
  transcript against it, cross-checked against
  `probe_scratchpad.md`'s already-accepted blemishes and partial fixes so a known, accepted
  gap isn't re-reported as a new defect. For a discrimination probe, it also owns the
  **causal-localization verdict** from the observed full-agent trace: whether the evidence
  supports, contradicts, or cannot distinguish the proposed owning boundary. On post-fix probes
  it separately verifies the recorded correction obligations (§6 step 4). It also owns the
  two checks no written rule covers (§6 step 4): whether the answer fulfilled the task it was
  given, and whether every form it emitted is one the harness itself defines — a form authorized
  but undefined is a harness defect, not an untestable gap. It does not verdict content; a wrong
  number found along the way gets named, not graded, and routed to `trace_compare`.
- **`.claude/skills/trace_compare/`** — the judge over both of `oracle_agent`'s references,
  returning two **separately labelled** verdict blocks. **CONTENT** — numbers, matched
  question, ranking, significance, and the agreement reading between the agent's and the
  reference's final answers — all decided against the Phases 0-5 ground-truth answer. The two
  runs agreeing is not a pass and disagreeing is not a failure until ground truth says which is
  right; both agreeing and both contradicting it is a shared tool-layer defect.
  **TRAJECTORY** — tool selection and arguments, call order, redundant calls, failed calls,
  error recovery, the coverage delta between the two final answers, and efficiency — graded
  against the Phase 6 reference trace, and the one verdict still gated on the blast radius,
  because it alone needs a second full agent run (§6 step 4). Content, like `contract_review`,
  runs on every probe. It verdicts no written
  output rule; it names and routes those to `contract_review`. Two properties keep the
  trajectory half honest: the reference trace is **evidence, not a standard** — a
  different-but-valid path is a PASS, and only a difference that violates a stated criterion is
  a finding — and that trace is itself checked against the Phases 0-5 answer before anything is
  graded against it, so a reference that went wrong is reported rather than believed. The two
  blocks never merge: a right answer reached wastefully and a wrong answer reached efficiently
  are different problems with different owners.

Whichever runs must run as an **`ISOLATED-GRADER` — a context with no memory of the fix**
(§0), never inline in the session that wrote it. Inline grading carries the memory of having
written the fix into the verdict, which defeats the one property that makes a second check
worth having (§9).
Their independence is not equal, and §6 step 4 says which way each verdict fails.
`trace_compare`'s **content** verdict rests on an answer derived on a path that never reads
`funda_agent_exp.py`'s tool code, so it can **false-FAIL** on a number that is correct under a
documented tool convention. Its **trajectory** verdict rests on one stochastic reference run,
so it can manufacture a finding out of ordinary run-to-run variance — that is what its
difference-is-not-a-defect rule and its list of by-design asymmetries exist to prevent, and why
every one of its findings must name the criterion it violates. `contract_review` grades against
rule text the fixing session may itself have just written, and can **false-PASS**. §6 step 4 is
where all of this fits into the working order, and which of them a given change calls for.

**Two harnesses, symmetric information.** The oracle runs in the **Claude Code harness**, not
the agent's LangGraph loop — a genuinely different solver, which is what makes its trajectory
worth comparing against. What the two share is the *information* each can reach: `query.sh`,
`stats_api.py` and `word_cloud.py` are independent counterparts to `nl2sql_tool`,
`run_survey_stats` and `generate_word_cloud`, while `plsr_ref.py` invokes `analyze_plsr`
itself — the same tool on both sides, by design.

Because the harnesses share no tool names, trajectories are compared on **normalized capability
plus stated purpose**, never on mechanism. `scripts/trace_log.py` records every oracle step to
`ORACLE_TRACE_FILE` — capability, funda counterpart, why the step was taken, arguments,
outcome, error — and that log, not a narrated summary, is what `trace_compare` grades against.
The logging happens inside the scripts, so it never depends on the oracle reporting its own
behaviour.

Three things are deliberate and must never be read as defects: the oracle has no reporting
contract (no chips, charts or nested tables); it holds capabilities the agent lacks —
`resolve_scope.py`, and `sql_stats.py`, whose figures the agent has no route to produce at all;
and PLSR runs through the agent's own tool on both sides.

**Know the shared blind spots.** Two paths are common to both runs and therefore invisible to
comparison: the **Charts API** — `stats_api.py` is an independent client of the same endpoint,
so it catches a client-side defect but never one in the API's own output, which is exactly how
the positional product relabel escaped — and **PLSR**, one shared tool end to end. The content
verdict rests on Phases 0-5 precisely because those go through `query.sh` and `sql_stats.py`,
outside both. Where a figure comes from a shared path and Phases 0-5 cannot re-derive it, it is
UNVERIFIED, not agreed.

### `not_used/` — retired, out of scope
`not_used/` holds material moved out of active use: `isolated_workflow.md`, `new_tool.md`,
`drilling_workflow.md`, `isolated_workflow_instructions.py`, `attribute_kpi_workflow.py`,
the superseded agent revisions `funda_agent_exp_old.py`, `agent_instructions_old.py`,
`tool_prompts_old.py` and `schema_overview_old.md`, the retired feature-influence tool
`feature_influence.py` with its `test_feature_influence.py`, and anything else relocated there
later. **It is the single canonical location for retired material** — on 16 Sep 2026 four of
these (`attribute_kpi_workflow.py`, `new_tool.md`, `isolated_workflow_instructions.py`,
`drilling_workflow.md`) were found byte-identical in BOTH the root and here, which is how the
stale test-import claim above survived. If you retire something, move it; never copy it. Nothing in it is read, run, referenced, or kept in
sync as part of this protocol — treat it the same as "nothing above this directory is in
scope" (§1). If a change appears to need something from `not_used/`, that is a signal to
ask the user before pulling it back in, not to restore it unilaterally.

---

## 3. Local environment

There is **no bare `python`** on this machine. Always use `.venv/bin/python` (uv-managed,
Python 3.12). `pytest` is not installed — run tests with `python -m unittest`.

### Local DB

- Docker container `gpi-db`, database `gpi_sample_db`, user `gpi`, host port **5433**.
- Connection string lives in `../BE/.env` as `DATABASE_URL`; the local agent reads that
  file directly and falls back to a hardcoded localhost URI.
- Read-only query helper:
  `../.claude/skills/regression_test_langraph/scripts/query.sh "SELECT …"`
  (wraps `docker exec … psql`; lives one directory level above this repo — read-only, and the
  only path outside this directory §1 permits). `oracle_agent` ships its own in-scope
  equivalent at `.claude/skills/oracle_agent/scripts/query.sh`; prefer it.
- The deployment copy has **no** local-DB knowledge and must never gain any.

---

## 4. Sync contract

### Always sync — byte-identical in both trees
These are pure agent behaviour. A change to any of them in root is incomplete until
mirrored into `deployment/`.

```
agent_instructions.py    tool_prompts.py
plsr.py
requirements.txt
```

### Sync the agent parts only — never the config
`funda_agent_exp.py` and `api_funda_agent_exp.py` **legitimately differ** and must stay
that way:

| | local root | `deployment/` |
|---|---|---|
| secrets/config | reads `../BE/.env` via `dotenv`, hardcoded fallbacks for `OPENAI_API_KEY`, `DATABASE_URI`, charts secret | `_required_env(...)` — raises at import if absent, **no source-code fallbacks** |
| DB target | local Docker `gpi_sample_db` on :5433 | whatever `DATABASE_URL` supplies |

Current known divergence is a few dozen lines per file, all of it inside that config band.
§8's diff-count snippet prints today's figure. If it jumps beyond what the config band can
explain, agent code has drifted — investigate rather than blindly copying.

**Method:** never `cp` these two files. Apply the *same anchored edit* to both, e.g.

```python
for path in ("funda_agent_exp.py", "deployment/funda_agent_exp.py"):
    src = open(path, encoding="utf-8").read()
    assert src.count(OLD) == 1, path        # fail loudly if the anchor moved
    open(path, "w", encoding="utf-8").write(src.replace(OLD, NEW))
```

### Never sync — deployment-owned, do not touch
```
Dockerfile   compose.yaml   compose.production.yaml   start.sh   ops/
```
…except for the one narrow case in §5. Also never copy local-only paths (§2) into
`deployment/`, and never introduce a `.env`, DB URI, or credential into the deployment
tree.

### Never sync — local-only
`tests/` is **not** mirrored, and neither are the `*.md` probe files. The tests exercise
`plsr.py`, which is byte-identical in both trees, so running it here
covers the deployed copy too. See §2 for what each test file covers.

---

## 5. Adding a new file or a new dependency

This is the one place `deployment/`'s own config legitimately changes.

**New Python module** (e.g. adding `plsr.py`):
1. Create it in root; write tests under `tests/`.
2. Copy the module verbatim to `deployment/` (it has no config in it).
3. **Add it to the Dockerfile `COPY` line.** The Dockerfile lists source files *by name*:
   ```
   COPY --chown=10001:10001 funda_agent_exp.py api_funda_agent_exp.py \
        agent_instructions.py tool_prompts.py plsr.py ./
   ```
   A module missing here builds fine and then dies at container start with
   `ModuleNotFoundError`. This is the single easiest thing to forget.
4. Verify: `docker run --rm --entrypoint sh <image> -c "ls /app"`.

**New third-party library:**
1. Confirm it is genuinely needed. scipy / scikit-learn / statsmodels / pandas are
   deliberately absent — `numpy==2.5.1` is pinned and the numeric code is hand-rolled.
   Adding any of them **requires explicit user approval**; it is a recorded project
   decision, not a default.
2. Add a **pinned** entry (`pkg==x.y.z`) to `requirements.txt` in **both** trees — they
   must stay identical.
3. Rebuild the image; the Dockerfile installs from `requirements.txt` before copying
   sources, so a dependency change invalidates that cache layer.

**Preferred alternative:** if a library is needed only to *validate* hand-rolled code,
install it in a throwaway venv, generate a fixture, and commit the fixture — not the
dependency. `tests/plsr_sklearn_fixture.json` was produced this way from scikit-learn
1.9.0; sklearn is never a runtime dependency.

---

## 6. Working order — a gated workflow

**The gate is the user's approval after a local run. Nothing reaches `deployment/` before
it.** Steps 0-4 are the coding agent's; step 5 is the user acceptance gate; step 6 only
happens once step 5 says so.

For edits confined to workflow/procedure documentation, check consistency across the affected
instructions and obtain an independent read-only review using §0's isolation mechanism.
Report that as a workflow review, not an agent-behaviour verdict; live survey probes apply when
agent behaviour changes. Documentation remains local-only under §2 and §4.

**Transcript budget.** For any agent transcript retained in `probe_scratchpad.md` or handed to a
grader, keep the user prompt, scope, and rendered final answer intact, but truncate **each serialized
tool call to 500 characters** and **each tool output to 1000 characters**, marking truncation
explicitly. If decisive evidence lies beyond a cutoff, extract only the needed field/lines
separately rather than passing the full payload.

**One case record connects the abstractions.** In the existing `probe_scratchpad.md` entry,
record the following compactly, filling diagnosis before editing and results as they arrive.
Reference symbols, prompt IDs and evidence rather than copying maps or writing another document.

```text
Case / expected behaviour and its requirement or contract:
Actual path / relevant state before and after, including lifetime:
First observed divergence / suspected origin and owning component(s):
Hypothesis / nearest alternative / predicted difference between them:
Relevant symbols / prompt IDs, delivery evidence and mapping gaps:
Discrimination evidence and verdict:
Correction: producer / factual authority / enforcement / why it addresses the cause:
Correction obligation / required execution conditions / observable evidence and source:
Evaluated source files and hashes / command, entrypoint and run locator / relevant config overrides:
Fix and regression evidence / blast radius / remaining uncertainty / approval state:
```

0. **Localize the defect before touching code.** Reproduce the reported failure once with the
   exact prompt/scope when available, then trace the earliest boundary at which behaviour becomes
   wrong: assembled prompt/rule presence → model decision → tool chosen → tool arguments/question
   id → *[inside the tool: guardrail rejection → scope repair → authorization → DB / Charts API →
   normalization]* → tool output → state/memory → final model response → *artifact sanitizer /
   final assembly → API transport*. Do not infer ownership from the
   final symptom. A valid localization looks like: *the rule is present; the correct tool was
   selected; the correct question id reached it; the tool returned the correct value; the wrong
   transformation first appears in the final response → owner: model-facing reporting behaviour*.

   Trace the **executed path**, not an assumed traversal of every boundary. Note the entrypoint,
   relevant branch conditions and skipped stages (inventory-only answer, tool loop, trusted
   artifact shortcut, API follow-up). For state-dependent failures, follow only the implicated
   fields across turns: what should persist, reset or expire, and what actually did. A stale
   artifact can first appear in final assembly but originate in an earlier reset failure; keep
   the first observed divergence separate from the causal origin until evidence links them.
   Mark a code-read prediction separately from a transition observed in a run. If the trace
   cannot expose a boundary, use a focused code read or component check and name what remains
   unverified; do not infer hidden state from the final answer.

   **Two parts of that chain are easy to skip.** *Inside the tool* is five boundaries, not one: a
   value can be wrong because the guardrail rejected the query, because scope repair rewrote it,
   because the authorization envelope narrowed it, because the DB or the **external Charts API**
   returned it that way, or because normalization changed it afterwards. And the chain is
   data-shaped — it says nothing about **why the loop stopped**, which is a separate axis and the
   first thing to check for *gave up early*, *looped on a wrong query shape*, or *answered less
   than it retrieved*. Check `call_model` and `should_continue` against the round's trace:
   distinguish a model-selected answer, budget-forced synthesis with tools removed, a trusted
   artifact returned without invoking the model, and an execution failure. A tool-free final
   AI message alone does not identify the branch. If the evidence cannot distinguish them,
   record the halt reason as unverified. The `zero_row_streak` warning is prepended to the tool
   result, so it is in the transcript too.

   Record the **first divergent boundary** and its owning component in `probe_scratchpad.md` before
   editing, with a predicted observable difference between the hypothesis and its nearest
   alternative. Use the prompt map here when instructions could explain the divergence (§2).
   Preserve the relevant pre-fix rule excerpts with this evidence so later grading does not
   judge the old execution against rewritten rules. Then run one **discrimination probe**
   through the **full local agent** that distinguishes
   that diagnosis from the nearest plausible competing cause — this answers *"Was our causal
   diagnosis correct?"*. Capture its transcript using the transcript-budget rule above: prompt,
   scope, and final answer remain intact; each serialized tool call is capped at 500 characters and
   each tool output at 1000 characters. The discrimination probe must exercise the normal ReAct path,
   not bypass it through direct tool/DB invocation. If the discriminator contradicts the diagnosis,
   relocalize; do not patch the originally suspected component. If inconclusive, extract the
   missing evidence or perform a focused diagnostic check; keep the causal claim unverified
   until the alternatives are distinguished. Component checks can support the diagnosis but
   do not replace this full-agent probe or prove model behaviour.

1. **Make the change in root, and make it minimal.** See §9 — a fix is scoped to the
   defect that was actually demonstrated, nothing adjacent. For output defects, use the relevant
   `LLM_FLOW.md` / `CONTROL_FLOW.md` recipe and fill the correction fields in the case record
   before editing: why this mechanism, which source is authoritative, and which check would
   reveal that it failed. For every correction, record its **correction obligation** before
   editing: the observable change required by the original requirement and diagnosis, the
   execution conditions needed to exercise it, and the evidence source that will settle it.
   Name the fix probe's obligation and the regression probe's preservation obligation separately;
   the latter need not execute the changed branch. Do not weaken either after seeing results
   unless the user changes the requirement. For prompt changes, require delivery on the relevant
   path and observable behaviour, not proof of the model's private reasoning or single-run
   causality. For preventive fixes, avoiding the forbidden action under the triggering conditions
   can satisfy the obligation; do not require entry into the branch the fix should prevent.
   If the change touches model-facing text or delivery (§2), first traverse
   `docs/generated/agent-prompt-map.json` to localize the exact instruction record, then
   make a precision-strike edit — see §2, "Prompt map." Once that edit is done, regenerate
   or update `docs/generated/agent-prompt-map.{md,json}` via
   `LOAD-PROCEDURE agent-prompt-mapper` (§0) before moving to step 2 — a prompt change is not
   finished while the map still describes the old text.

   Before moving on, state the change's **blast radius** in one line: which of *numbers /
   question match / which tool supplies a number / tool-call behaviour / rendered artifact*
   this change can move. It goes on the `probe_scratchpad.md` row, and step 4 uses it for two
   things: deciding whether the trajectory verdict runs at all, and telling every grader where
   to look first. It no longer decides whether content is graded — content and the contract are
   unconditional on all three probes, because a blast radius is the fixing session's own
   estimate of its reach, and not relying on that estimate is the whole point of an isolated
   grader.
2. **Run whatever unit tests cover the change.**
   ```
   .venv/bin/python -m unittest tests.test_plsr -q
   ```
   A passing unit test says nothing about whether the model follows a new prompt rule.
3. **When the WHOLE fix is finished — not before — prepare and execute the post-fix probes
   locally.** If the change has several parts, complete every part first; never treat a partial fix
   as the finished fix. Build the **complete copy-pasteable block** — scope array,
   full `python funda_agent_exp.py ...` invocation, and the exact strings to look for. Never a bare
   prompt the user has to assemble a command around. Run each once:
   - a **fix probe** — the prompt that reproduced the defect, which must now behave;
   - a **regression probe** — a nearby prompt that already worked and must still work.
   The **discrimination probe** was already run in step 0 to establish causal ownership; retain its
   command, truncated trace, and full final answer with the evidence packet. Ready-made post-fix
   blocks are in §6.1.

   **Where the blast radius reaches tool-call behaviour, each probe also gets a reference run.**
   `oracle_agent`'s Phase 6 (§6.1) answers the *same prompt and scope* in the Claude Code
   harness with `ORACLE_TRACE_FILE` set, producing a trajectory log and its own answer for
   `trace_compare` in step 4. It is one solve, not many — §6.1's one-run budget applies to both
   sides. Run it on the same revision as the probe it references; a reference produced before a
   behaviour-affecting edit is stale for the same reasons the probe itself would be.

   Capture each post-fix transcript before grading using the transcript-budget rule above.
   Record each probe's required execution conditions and evidence of the expected correction or
   preserved behaviour. A successful answer through an unrelated path does not verify the fix.
   For a deterministic correction, include a focused executable component check when the live
   trace cannot establish the obligation; a pre-fix failure and post-fix pass on the same input
   can strengthen it. Component evidence establishes only that component's behaviour and does
   not replace required full-agent evidence. A code read is a prediction, not execution proof.

   **Bind evidence to the evaluated revision.** Record hashes of the implicated source files
   (including relevant prompt/delivery sources), the command, entrypoint, run locator and
   relevant configuration overrides without secrets. Compute hashes when capturing the run;
   do not attach today's hashes to historical evidence. Later edits to an implicated file make
   its verdict stale pending an applicability check. An independent reviewer may retain evidence
   for a demonstrated behaviour-neutral edit, such as formatting, with the diff and reason
   recorded. A behaviour-affecting edit requires fresh applicable execution evidence and grading;
   weaker prompt wording alone does not justify retaining an older model-behaviour verdict.

   **Recover before re-running.** If a decisive field is clipped, first extract it from the same
   run's full conversation records or diagnostic logs, retaining its run locator. The current
   CLI/API conversation log stores full tool arguments and public tool outputs, but not every
   internal transition; it is not a substitute for missing state or repair evidence. If the field
   was never captured, name the smallest focused check or capture needed and keep the obligation
   unverified. Missing evidence or a bypassed branch does not authorize repeated stochastic
   trials; §6.1's one-run budget still applies.
4. **Grade all three already-executed probes before reporting.** Hand the discrimination
   transcript from step 0 and each fix/regression transcript
   from step 3 to an **`ISOLATED-GRADER`** (§0, §2) — never grade inline in this session, which
   would grade the probe carrying the fixing session's assumptions. `contract_review` and
   `trace_compare`'s content verdict run on every probe unconditionally; only the trajectory
   verdict follows step 1's blast radius:
   - `contract_review` — **always**, for presentation/behaviour; on the discrimination probe it
     additionally gives the causal-localization verdict: **SUPPORTS**, **CONTRADICTS**, or
     **INCONCLUSIVE** for the proposed owning boundary. On each fix/regression probe it also
     returns a **CORRECTION VERDICT: VERIFIED / CONTRADICTED / UNVERIFIED** for that probe's
     recorded obligation, using the procedure's evidence criteria. This is separate from rule,
     content, task-fulfilment and harness-self-consistency verdicts. VERIFIED requires evidence
     of the required execution conditions and correction (or preservation) on the evaluated
     revision; CONTRADICTED means observed violation; missing, bypassed or stale evidence is
     UNVERIFIED. It establishes only the stated checks on the observed run, not causal proof
     of a prompt edit or stability. Semantic expectations such as the right question/measure
     must come from an independently justified reference; route unresolved content to
     `oracle_agent`, not the contract reviewer's guess.
   - `trace_compare` — for the **content** and **trajectory** verdicts, in two separately
     labelled blocks, each gated on its own half of the blast radius.

     **Content runs on all three probes, always** — like `contract_review`, and for the same
     reason. It needs `oracle_agent`'s Phases 0-5 ground-truth answer and returns
     **NUMBERS / MATCHED QUESTION / RANKING / SIGNIFICANCE / AGREEMENT**, each PASS, FAIL or
     UNVERIFIED. AGREEMENT reports how the agent's and the reference's final answers line up
     against ground truth: agent wrong, reference wrong, or — the case worth the whole exercise
     — both agreeing and both wrong, which is a tool-layer defect attributed to `tool payload`
     or `external dependency`, never to a prompt rule.
     There is no `content unaffected` skip: whether a change *could* move a number is a
     judgement by the session that wrote it, and an isolated grader exists precisely so that
     judgement is not what decides whether anyone checks. The blast radius still says where to
     look first; it no longer licenses not looking. The only honest way for a probe to carry no
     content verdict is the host being unable to produce the reference at all, which is
     UNVERIFIED and not a pass (below).

     **Trajectory** is the one verdict still gated, because it alone costs a second full agent
     run per probe (`oracle_agent` Phase 6). It runs whenever the blast radius includes
     **tool-call behaviour**, or the defect class is *gave up early*, *looped on a wrong query
     shape*, or *answered less than it retrieved* — the halt-reason axis step 0 names, which nothing else is equipped to
     settle. It needs the Phase 6 reference trace from step 3, and returns **TOOL SELECTION AND
     ARGUMENTS / CALL ORDER / REDUNDANT CALLS / FAILED CALLS / ERROR RECOVERY / OUTPUT COVERAGE
     DELTA / EFFICIENCY**, each PASS, FINDING or UNTESTED, plus the halt reason for both runs.
     Efficiency is judged on unbatched batchable work, budget exhaustion and unused retrieval —
     never on a raw call-count delta, which between two single runs is variance. Where the blast radius
     does not reach it, record `trajectory unaffected — <reason>` on the row instead.

     A trajectory verdict skipped with a stated reason is honest; a mandated verdict skipped
     silently is how this step decays into ceremony. Tool-call behaviour on its own — the tool chosen, its
     arguments, batching, order — measured against the *written contract* remains
     `contract_review`'s; `trace_compare` asks the different question of whether the sequence
     was a sensible way to answer at all.

     Three limits are part of the verdict, not caveats on it. A **difference from the reference
     is not a defect** — the reference shows what these tools make possible, not what the agent
     must do; only a difference that violates a stated criterion is a finding. Both traces are
     **single stochastic runs**, so no finding may be stated as a rate and none justifies a
     re-run to see whether it reproduces. And the structural asymmetries are **by design** —
     no `nl2sql_tool` on the reference side, so no `[tool-batch-timing]` and no zero-row-streak
     warning on its SQL, and no presentation contract at all — so none of them is ever a
     finding. Its `SKILL.md` carries the full list; a session that reports one of them has
     graded the harness, not the agent.

   Supply the same case record to the graders: the requirement establishes expected behaviour,
   the trace and map support diagnosis, the FLOW recipe explains the correction, and probes
   supply execution evidence. Keep the original acceptance criterion unless the user changed
   the requirement. Grade the discrimination run against its preserved pre-fix rule excerpts;
   missing excerpts make affected historical rule checks UNTESTED, not evidence for the new rule.

   **When the host cannot produce a reference.** `oracle_agent` needs `gpi-db` and the live
   Charts API (§0, §3) for either reference; a host without them cannot produce a content
   verdict at all, because `trace_compare` is forbidden to derive its own standard. That is
   not a pass and not a skip-with-reason: record `content UNVERIFIED — reference unavailable on
   <host>` on the `probe_scratchpad.md` row, do not report the change as verified, and say so
   to the user. Never substitute an inline reading of the numbers for the missing reference —
   that is the exact failure `ISOLATED-GRADER` exists to prevent.

   **Task fulfilment and harness self-consistency — no verdict above covers either.** The
   verdicts above check written rules and derived references; none asks whether the answer did
   the job it was given. Two
   questions close that, and both are answered from inside the harness — prompts, tool
   payloads, runtime code, memory — with no knowledge of any consumer:
   - **Did the answer fulfil the task as asked?** Judge it against the harness's own
     completeness rules — `REPORTING_RULES`, and `NESTED_RESULT_RULES`' requirement to report
     every returned measure or name each omission and why — not against taste. An answer that
     quietly drops measures, silently substitutes a nearby question, or answers something
     narrower than the prompt asked is a FAIL even when every number in it is right.
   - **Is every artifact and claim in a form the harness itself defines?** A form the system
     prompt authorizes while neither the prompt nor the runtime validator defines its shape is
     a **harness defect**, not an untestable gap: the model was free to invent the shape and
     the invention was never checked. `Charts / descstat_heatmap` was exactly that — a chart
     type `_MODEL_CHART_TYPES` accepted while nothing shape-checked its payload, so the model
     filled the gap with keys of its own and nothing objected. The sanitizer has since closed
     it; §8's check is what tells you whether any type is still in that state, and the same
     question applies to every model-authored form, not only charts.
   `contract_review` owns both; `trace_compare` owns neither.

   **Every FAIL names the component it belongs to** — prompt rule, tool payload,
   runtime code, memory mechanism, or **external dependency**. That last one is real and has no
   harness fix: `gpi-db` and the live Charts API are outside this repo, and where one disagrees
   with the other — as in the positional product relabel recorded in `probe_scratchpad.md` — the
   disagreement is the owner, and no prompt or runtime edit is the remedy. A FAIL sends the next
   iteration back through step 0 to
   relocalize before step 1 acts, and this repo has already paid to learn it twice: a prompt rule
   reaches only
   inventory-derived figures while a tool-derived fact has to be put into the tool's own output
   (`Base composition / interp1`, `product_bases`), and a rule that keeps failing belongs in
   code (§9). A FAIL with no component attributed sends the next session back to guessing.

   Report every verdict verbatim, including anything marked UNVERIFIED or UNTESTED — that is
   not a pass. The graders fail in different directions, and each needs saying:
   - A **content FAIL from `trace_compare` means investigate, not automatic reject**: the
     ground-truth answer it grades against was derived without reading
     `funda_agent_exp.py`'s own tool code, so a transcript number that's correct under a real,
     documented tool convention (a rounding rule, a default
     parameter) can surface as a false mismatch — confirm against the tool's own contract in
     `tool_prompts.py` before treating a mismatch as a real defect. The same paragraph's other
     half: a statistic the oracle computed on its local engine is one the agent had no route to
     produce, which is a capability gap, not an agent error.
   - A **PASS from `contract_review` means the output matched the rule as written**, which is
     not evidence that the rule is adequate. Where the change under test edited that same rule
     text, the fix defined the standard it was graded against; only the two checks above —
     task fulfilment and harness self-consistency — carry independent weight there.
   - A **trajectory FINDING from `trace_compare` is a claim about one pair of runs**, and its
     reference is one stochastic run of a different policy. Read it as evidence about the
     observed trajectories, never as a rate; check the criterion it names before acting on it.
     Its trajectory **PASS** is correspondingly narrow: it says nothing about whether the
     numbers are right — that is the content block, a separate verdict on a separate
     reference — and nothing about the written rules, which are `contract_review`'s.

   **A recipe's own pass strings are evidence for the grader, not a self-check.** §6.1's
   per-probe assertions — `Pass: tool call carries pca_plot_dimensions: "3d"`, `artifact is
   pca_biplot_2d`, and the like — are the cheapest evidence that a tool call was well formed,
   and they are the one place tool arguments get checked at all. Grepping them yourself and
   calling the probe passed is the fixing session grading its own work (§9). Run them, then
   hand the grep output with the transcript to `contract_review` and let the verdict come from
   there.

   A cross-domain observation one judge names but does not verdict — a wrong number spotted by
   `contract_review`, an odd chart spotted by `trace_compare` — must be re-put to the judge that
   owns it before the probe is closed. Unrouted, that item is UNVERIFIED, not a pass. The one
   thing no grader can reach is something with no trace at all in the transcript (e.g.
   `Short-term memory`'s exact truncation mechanism, prompt-caching internals) — say so plainly
   when a probe falls in that gap, the same as any other unverified claim.
5. **Hand over the already-run commands, then wait for the user to run them and approve.**
   Report the internal probe/grader results with the commands so the user is reproducing a tested
   fix, not performing its first complete execution. Report correction status separately: a
   passing answer or rule verdict does not establish correction verification. Any required
   correction obligation that is CONTRADICTED, UNVERIFIED or stale prevents describing the patch
   as verified. CONTRADICTED returns to step 0; for UNVERIFIED, name the missing check and the
   remaining uncertainty. Accepted exceptions retain their stated scope and must be reported;
   they cannot silently excuse failure of a new correction obligation. Do not mirror, commit, or push in the
   meantime. If the user's run shows a problem, go back to step 0 — do not patch forward on top
   of an unapproved change.
6. **Only after approval:** mirror to `deployment/` (§4), verify parity (§8), then commit and
   push `deployment/` (§7) — that push is what deploys. Ask before pushing; it is outward-facing.

### 6.1 Test recipes — copy-pasteable

**Where regression probes come from.** `regression.xlsx` (`regression_cleaned` sheet)
is this project's catalogue of regression prompts — the primary source, before touching the
DB. Each row pairs a `Prompt` with its own proven `Client-ID`/`Survey-ID`/`Organization-ID` scope,
a `Suite` naming the topic (`Persona reference`, `Word cloud tool`,
`Needle in haystack`, `Nested result rows`, `Client-wide survey boundary`, …), and a `Case` slug
that is its ID (e.g. `liking_construct_gate`, `named_question_flat`). `live_optimization_regression.py`
(§2) drives specific rows from it for BE-side instrumentation.

**Source 0 — a failure the user reports from the running product.** It outranks both sources
below, being the only one already proven to matter to a user. Get the agent's own final answer
text for that turn (`done.answer`), not a screenshot of it: the answer is what the agent
produced and can be graded, while a screenshot is one rendering of it and supports only a
shape-match. Where the failing survey has no counterpart in the local DB, record the substitute
scope that reproduces the *shape* of the failure and mark the row *reproduced by shape;
original answer text not seen* until it arrives. A live failure still becomes a
`regression.xlsx` row like any other probe (see "Promote before you close").

When designing the step-0 discrimination probe and the fix/regression probes for §6 step 3,
prefer picking — or lightly adapting — a prompt from the `Suite` that matches the area you changed:
its scope is
already proven to exercise that behaviour, and the `Case` ID lets you name the probe
precisely instead of restating it. Only fetch fresh scope from the DB (secondary source,
`query.sh`, §3) and hand-craft a new prompt when no existing row's `Suite`/prompt actually
exercises the changed behaviour — say so explicitly, rather than silently inventing scope IDs
that haven't been vetted against this agent. Record what you ran and its current status in
`probe_scratchpad.md` — a fix probe that stays unrun past this session is otherwise invisible
to the next one.

**One run per probe — budget rule.** Run each discrimination, fix, or regression probe once by
default, including model-chosen branches such as chart type, nesting depth, or roster vs single
snapshot. For stochastic behaviour, a single pass/fail is evidence only for that observed run, not
for stability or a failure-rate estimate; record it explicitly as a **single-run result**. Do not
repeat automatically. Additional trials happen only if the user explicitly asks for them or the
single run is technically inconclusive (for example, infrastructure failure rather than agent
behaviour).

**Promote before you close.** A reproduced defect gets its `Prompt`, scope, `Suite`/`Case` and
pass criteria into `regression.xlsx` at fix time, not later. `probe_scratchpad.md` holds
in-flight status and gets reseeded from a full pass; a case living only there is lost at the
next seed.

**Independent grading.** Once any discrimination, fix, or regression probe runs, its transcript
packet — full prompt/scope/final answer, with each tool call truncated to 500 characters and each
tool output to 1000 — is graded by one `ISOLATED-GRADER` (§0) per grader the blast radius calls
for (§2, §6 step 4), not by re-reading the output yourself — each owns exactly one question,
never another's:
- **content claims** (the number, the matched question, the ranking, the significance
  verdict) and the agreement between the two runs' final answers → `trace_compare`, decided
  against `oracle_agent`'s Phases 0-5 ground-truth answer, never by the two runs agreeing;
- **presentation/behaviour rules** (chart shape, `NESTED_RESULT_RULES`, benchmark banner,
  suggestion chips, disclosure timing, and the tool calls themselves — tool chosen, arguments,
  batching, order) → `contract_review`, checked against
  the live `agent_instructions.py`/`tool_prompts.py` text and cross-checked against
  `probe_scratchpad.md`'s already-accepted exceptions;
- **the trajectory** (tool selection and arguments, call order, redundant calls, failed calls,
  error recovery, the coverage delta between the two final answers, and efficiency) →
  `trace_compare`, against
  the Phase 6 reference trace, and only where the blast radius reaches tool-call behaviour.
  Note the boundary with the bullet above: `contract_review` asks whether a tool call obeyed
  the written contract, `trace_compare` asks whether the sequence of calls was a sensible way
  to answer the question at all. The first has rule text to quote; the second has only the
  reference run, which is why a difference alone is never its finding.

`contract_review` runs on **all three probes**. On the discrimination probe it owns the
causal-localization verdict from the full-agent trace; on fix/regression probes it owns the normal
presentation/behaviour verdict and the separate correction verdict against each recorded obligation
(§6 steps 1, 3-5), with the evaluated revision and evidence gaps identified. `trace_compare`'s
**content** verdict runs on all three probes unconditionally, alongside `contract_review`; its
**trajectory** verdict runs when the blast radius reaches tool-call behaviour. `oracle_agent`
produces whichever reference each half needs — the ground-truth answer for
content, the Phase 6 reference run on the same prompt and scope for trajectory (§6 steps 3-4). Neither rule verdict covers **task fulfilment** or **harness
self-consistency**, the two checks `contract_review` also owns — a form the harness authorizes
but never defines fails there rather than passing untested. A probe testing something with no trace in the transcript at all — e.g. `Short-term
memory`'s exact truncation mechanism — still needs a code-level read, not any of the three. Record
against each `probe_scratchpad.md` row the blast radius, the verdict source (`trace_compare`
content / `trace_compare` trajectory /
`contract_review` / code-level), and any grader deliberately skipped with its reason — a bare
"PASS" cannot be told apart from "not checked".

Activate the venv once (`source .venv/bin/activate`), then paste the scope array for the
survey you need followed by the command for the area you changed.

```bash
HERB=(
  --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672
  --client-id 37cbf852-a2b7-4f8b-96b4-7f67432f88cd
  --org-id    c75d846a-e265-4f69-92c1-91308e0697f6
)   # 450 respondents, rich open-text — personas, theme coding

TAST=(
  --survey-id e14528a9-01ac-4dff-844e-0914dbdb3759
  --client-id f6b05cb4-cea2-4855-816e-c92e5e5d22ff
  --org-id    17b6da66-3c2e-42f1-bb64-2a09bdfbc183
)   # 4 products, 79 numeric qs, matrix qs — PCA / 3D PCA

PLSR=(
  --survey-id d67ed874-fb40-446f-a645-6edfbfbdef53
  --client-id ""
  --org-id    ""
)   # 4 products, 100 respondents, 28 vertical-rating qs.
    # NULL org/client. The empty strings do bind scope, but they produce a preamble
    # production never sends for this survey: scoped_query() interpolates them, so the model
    # is handed `client_id = ` and `organization_id = ` as *authoritative* blanks. The API
    # resolves no chain here and sends scoped_query_survey_only() instead, which adds four
    # rules this recipe omits -- do not write :client_id/:organization_id (unbound, the query
    # fails), a historical comparison is impossible, and the cross-client benchmark is
    # unavailable. Read a PLSR verdict as graded on the CLI shape, not the deployed one.
```

**Oracle solve** — the reference trajectory, for any probe whose blast radius reaches
tool-call behaviour (§6 steps 3-4). Run `oracle_agent` on the same prompt and scope with the
trajectory log enabled:

```bash
export ORACLE_TRACE_FILE=/tmp/oracle-<case>.trace.jsonl
# work the question with the oracle's own tools; every call carries --why:
bash .claude/skills/oracle_agent/scripts/query.sh --why "<reason>" --tuples "SELECT ..."
python3 .claude/skills/oracle_agent/scripts/stats_api.py --question <uuid> --types anova,tukey --why "<reason>"
# render the step table for the grader:
python3 .claude/skills/oracle_agent/scripts/trace_log.py /tmp/oracle-<case>.trace.jsonl
```

Hand `trace_compare` three things: the ground-truth answer, the rendered trajectory log with
the oracle's own final answer, and the agent's captured trace.

**3D PCA**
```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id pca3d \
  --prompt "Show a 3D PCA plot of the statements about your experience with the product"
```
Pass: tool call carries `pca_plot_dimensions: "3d"`; the tool output line reads
`Plot: trusted 3D PCA chart prepared.` with **no** `;` clause after it; artifact is
`pca_biplot_3d` and carries `z_axis`; the answer ends with the 2D suggestion chip; the
string `retained usable component` appears nowhere.

**2D PCA** — the regression probe for any PCA change
```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id pca2d \
  --prompt "Run a PCA on the statements about your experience with the product"
```
Pass: artifact is `pca_biplot_2d`, points carry no `z`, and the answer ends with the 3D
suggestion chip.

**PLSR**
```bash
python funda_agent_exp.py "${PLSR[@]}" --thread-id plsr1 \
  --prompt "Compute PLSR between the sensory attributes and overall liking"
```
Pass: before the tool call, the agent asks for any missing normalization and cross-validation
choices; after the user answers, `PLSR ANALYSIS COMPLETE` includes those choices in the tool
arguments and method line. The attribute table and the five-column
`MSE | RMSE | R2 Score | RMSE (CV) | R2 (CV)` table carry the payload's `mse`, `rmse`, `r2`, `rmse_cv`, `r2_cv` unaltered
(with CV headers labeled `LOOCV` or the selected K-fold count); if no CV was selected, both CV
cells say `N/A`. On the reference-style 4-product run `R2 (LOOCV)` is strongly negative and must
be printed as returned, never clipped or dropped, with one line saying the fit does not predict
held-out products; where the model narrowed the attribute set itself it must say which measures
it sent and how many it left out.

**PLSR ambiguity** — the regression probe for any PLSR change
```bash
python funda_agent_exp.py "${PLSR[@]}" --thread-id plsrq \
  --prompt "Do PLSR"
```
Pass: the tool is **not** called; the answer lists candidate KPIs and attributes and asks
which to use.

**Personas**
```bash
python funda_agent_exp.py "${HERB[@]}" --thread-id persona1 \
  --prompt "What are the personas of this survey?"
```
Pass: roster vs single snapshot is argued from counts, not asserted; candidate overlap
counted at respondent grain; the match-nothing remainder stated.

**Capture with a manifest.** Every recipe above shows the bare `python funda_agent_exp.py`
invocation, which is what the agent actually runs — but a trace captured that way carries no
header, and a grader then cannot bind it to a revision or confirm which flags were in force
(both were UNVERIFIED on the first `trace_compare` calibration). For any run you intend to
retain or hand to a grader, put the same flags through the capture wrapper instead:

```bash
.venv/bin/python .claude/skills/oracle_agent/scripts/capture_agent.py "${TAST[@]}" \
  --thread-id pca3d --prompt "..." 2>&1 | tee /tmp/agent-pca3d.log
```

It changes nothing about the run — it publishes scope, prints the manifest, then calls
`funda_agent_exp.main()` with your arguments. Both captures use one emitter
(`probe_manifest.py`), so the agent and reference headers compare field for field; the thread
id must NOT start with `oracle`, which the reference runner reserves.

**Capture and grep**
```bash
... --prompt "..." 2>&1 | tee /tmp/run.log
grep -nE "pca_plot_dimensions|Plot: trusted|pca_biplot|retained usable|fallback" /tmp/run.log
```
For a prompt-rule change, also confirm the rule reached the assembled prompt:
```bash
.venv/bin/python -c "import agent_instructions as ai; print('<marker>' in ai.build_system_prompt())"
```

Say **"experience"**, not "expectations" — the near-identical `fdebd104` question has no
usable PCA and will legitimately fall back.

A long-running agent process holds the pre-fix module in memory — restart it before
concluding a fix did not work. The `gpi-db` container also stops on its own; check
`docker ps` and `docker start gpi-db` first.

**The degraded-scope path has no probe and no grader coverage.** `main()` calls
`scoped_query()` unconditionally, so no recipe here can produce `scoped_query_survey_only()` —
the preamble the API sends whenever a survey's organization/account chain does not resolve. It
is model-facing text that has no live probe/grader coverage recorded here. Its static delivery
path is included in the prompt map (§2, "Scope the map from both entrypoints"); mapping is not
execution evidence. The comment on
`scoped_query_survey_only` in `agent_instructions.py` says why it exists and how common that
shape is in this database. Exercising it needs the API, not the CLI — a second run surface and
transcript format — so until an applicable probe is run and graded, treat any claim about this path as verified by code
read only, and say so on the `probe_scratchpad.md` row.

`tests/test_agent_optimizations.py` fails to import
(`ModuleNotFoundError: attribute_kpi_workflow`) — that module lives in `not_used/` (§2) and is
not restored. It targets the retired isolated-workflow / attribute-KPI architecture and would
still fail on 82 of 324 tests if the import were fixed (§2); unrelated to the agent modules
above. The live offline gates are `test_plsr.py` and `test_clustering.py`.

---

## 7. Git topology and publishing

```
final_agent_work_v5_base_2_optim3_exp/     ← the working tree; nothing above it is in scope
  └── deployment/  ──▶  GitHub herbalife_agent (branch main)  ──▶  server
```

`deployment/` is the only publishable tree here, and pushing its `main` is what deploys.
The working tree itself publishes nothing.

Once the user has approved a local run (§6 step 5) and the mirror is done (§4):

```bash
cd deployment
git status --porcelain          # confirm only the intended files changed
git add <explicit paths>        # never `git add -A`
git commit -m "..."
git push origin main            # ← this is what deploys. ASK FIRST: it is outward-facing.
```

- **Pushing is the user's call.** Prepare the commit, show what it contains, and ask.
- Publish only files §4 says are shareable — never a `.env`, a DB URI or a credential.
- ⚠️ `funda_agent_exp.py` in this directory contains a **hardcoded OpenAI API key** as a fallback
  default. It is not in the deployment copy. Do not propagate it, and prefer rotating it
  and reducing the line to a plain `os.getenv(...)`.

### Runtime environment the container requires
All hard-required at import; missing any one crashes startup.

```
OPENAI_API_KEY
DATABASE_URL  (or DATABASE_URI)
CHARTS_API_BASE_URL
CHARTS_STATS_EXTERNAL_ACCESS_SECRET
HERBALIFE_EXTERNAL_ACCESS_SECRET   # must be ≥ 32 characters
```

---

## 8. Verification snippets

```bash
# parity of the always-sync files
for f in agent_instructions.py tool_prompts.py plsr.py requirements.txt; do
  diff -q "$f" "deployment/$f" >/dev/null && echo "$f identical" || echo "$f DIFFERS"
done

# the two config-divergent files: check size of the gap, not equality
diff funda_agent_exp.py deployment/funda_agent_exp.py | grep -c '^[<>]'   # config band only

# every local module the deployment entrypoints import is in the Dockerfile COPY list
cd deployment
COPIED=$(grep '^COPY --chown' Dockerfile | sed 's/^COPY --chown=10001:10001 //; s| \./$||' | tr ' ' '\n')
grep -hoE "^(from|import) [a-z_]+" funda_agent_exp.py api_funda_agent_exp.py |
  awk '{print $2}' | sort -u | while read -r m; do
    [ -f "$m.py" ] || continue
    echo "$COPIED" | grep -qx "$m.py" || echo "MISSING FROM IMAGE: $m.py"
  done

# the committed deployment/ tree really loads (not just its working files)
PY="$PWD/.venv/bin/python"; SECRET=$(head -c 32 /dev/zero | tr '\0' 'x')
T=$(mktemp -d); git -C deployment archive HEAD | tar -x -C "$T"
(cd "$T" && OPENAI_API_KEY=x DATABASE_URL='postgresql+psycopg2://u:p@h:5432/d' \
   CHARTS_STATS_EXTERNAL_ACCESS_SECRET="$SECRET" CHARTS_API_BASE_URL=http://c \
   HERBALIFE_EXTERNAL_ACCESS_SECRET="$SECRET" \
   "$PY" -c "import funda_agent_exp as a; print([t.name for t in a.build_tools()])")
rm -rf "$T"

# deployment/ publish state
cd deployment && git status --porcelain && \
  git rev-parse HEAD && git ls-remote origin refs/heads/main
```

Where the harness authorizes a form it does not define, the model invents one and nothing
checks the invention (§6 step 4). This tests that gap for charts by running the live sanitizer
against a payload of invented keys, one accepted type at a time:

```bash
.venv/bin/python - <<'EOF'
import json, os
os.environ.setdefault("OPENAI_API_KEY", "unused-for-import")
import funda_agent_exp as a

def kept(chart_type, point):
    payload = {"version": 1, "type": chart_type}
    if chart_type in a._CHART_MATRIX_TYPES:
        payload["data"] = [point]
    else:
        payload["series"] = [{"data": [point]}]
    _, withheld = a._sanitize_model_chart_blocks(
        "```gpi-chart\n" + json.dumps(payload) + "\n```")
    return not withheld

invented = {"foo": "A", "bar": 5}
unchecked = [t for t in sorted(a._MODEL_CHART_TYPES) if kept(t, invented)]
print('accepted by _MODEL_CHART_TYPES  :', len(a._MODEL_CHART_TYPES))
print('KEEP a payload of invented keys :', ', '.join(unchecked) or 'none')
EOF
```

Every accepted type should reject invented keys, so the second line should read `none`. Any type
it lists is one the harness authorizes and nothing shape-checks — the model is free to invent
that payload and no validator objects. Close it in one of two directions: state the shape in
`agent_instructions.py` and enforce it in `_sanitize_model_chart_blocks`, or drop the type from
`_MODEL_CHART_TYPES` and from the allowed-types sentence in the prompt.

Note what makes this check trustworthy: it executes the sanitizer instead of describing it. The
version this replaced scraped type names out of the function body, and silently inverted its own
verdict when those names were hoisted into `_CHART_SCATTER_TYPES`/`_CHART_MATRIX_TYPES` —
reporting a gap of 18 where the real gap was zero. Judge the same way for any other
model-authored form the harness admits, not only charts: run the validator, do not read it. The
general form of the question is *what the prompt promises* against *what the runtime binds or
validates* — `build_tools()` versus the tools named in `tool_prompts.py` is the same check.

---

## 9. Standing conventions

- **Minimal, never over-engineered.** This is the first rule, not a preference. Fix the
  defect that was actually demonstrated and stop. No adjacent cleanup, no speculative
  guards, no "while I'm here" refactors, no abstraction introduced for a single caller, no
  new file where an edit to an existing one does the job. Match surrounding style, comment
  density, and naming. If a smaller fix and a more general one both work, take the smaller
  one and say the larger option exists.
- **Scope creep is the user's call, not yours.** Where a fix uncovers a second problem,
  report it and let them decide; do not fold it in silently.
- **No new dependencies, abstractions, or modules without explicit approval.**
- **No derived facts in this file.** Anything that changes when the code changes — a line
  number, a symbol's location, a file's length, a count of rows or accepted types — is
  generated, printed by a §8 command, or left out. Never typed here. A symbol name is
  greppable and does not drift; a line number is wrong within the week. This is why the tables
  in §2 name symbols and not lines, and why counts that used to sit in this file now come from
  a command.
- **No unobservable boundaries.** Every boundary §6 step 0 names must leave a trace in the
  transcript a grader reads, or step 0 must say plainly that it needs a code-level read
  instead. A boundary nobody can see produces a FAIL nobody can attribute, and step 4 requires
  attribution.
- Prompt text is load-bearing. A rule added in one place can be defeated by an easier
  branch elsewhere in the same block — check how the new wording interacts with the rules
  already there before adding another paragraph.
- When a prompt rule fails repeatedly, stop adding paragraphs and consider moving the
  constraint into code.
- Tool payloads carry raw numbers; rendering rules live in `agent_instructions.py`. Do not
  format numbers inside a tool for display reasons.
- **The fixing session does not grade any of the three probes itself.** Run `contract_review`
  (§2, §6 step 4) as an `ISOLATED-GRADER` (§0) for the discrimination, fix, and regression
  probes, and run `trace_compare` the same way wherever content or tool-call behaviour is in
  scope. Nor does it produce its own references: `oracle_agent` derives those, in its own
  isolated context. Inline grading
  carries the fixing session's assumptions into the verdict and defeats the point of a second
  check — on every host, whatever the isolation mechanism.
- **The producer never judges, and the judges never derive.** `oracle_agent` makes both
  references — the ground-truth answer and the reference trace — and verdicts nothing.
  `trace_compare` verdicts content against the first and trajectory against the second, in two
  separately labelled blocks, and derives neither. `contract_review` verdicts
  presentation/behavior only, using this repo's own accepted-exception history the others
  don't have. A procedure that notices another's kind of problem names it, but does not
  verdict it — and that named item goes back to the owning judge before the probe is closed
  (§6 step 4).
- **Content and trajectory share a procedure but never a verdict.** They are graded against
  different references and fail in different ways; a right answer reached wastefully and a
  wrong answer reached efficiently are different problems with different owners. Report the two
  blocks separately even when both are clean.
- **A reference trajectory is evidence, not a standard.** The Phase 6 run shows one competent
  path through these tools on one stochastic run. The agent taking a different path is not a
  defect, and neither is taking more or fewer calls than the reference. Only a difference that
  violates a criterion someone can state — a tool that could not answer the question, a wrong
  id, a failure left unaddressed, a requested measure silently dropped — is a finding. A
  trajectory verdict that reports plain variance costs the next session more time than it
  saves.
- **Conformance to a rule is not the same as fulfilling the task.** The two rule-checking
  graders check written rules; where a rule is missing, both pass. §6 step 4's two extra checks
  are what keep a
  missing rule from reading as a pass: did the answer do the job, and is every form it emitted
  one the harness defines. `trace_compare`'s coverage delta is a third way the same gap shows
  up — a measure the reference retrieved and the answer silently dropped is visible there
  whether or not any rule names it. A form the harness authorizes but never defines is a defect of the
  harness — attribute it to the component that owns it (prompt rule, tool payload, runtime
  code, memory mechanism, external dependency) so the next fix lands there rather than in more
  prompt text.
