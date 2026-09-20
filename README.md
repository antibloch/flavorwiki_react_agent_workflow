# final_agent_work

Everything needed to run, serve, benchmark and understand **`funda_agent_exp.py`** — the LangGraph
survey-analyst agent — copied out of `flavorai_v2` as a self-contained bundle.

---

## ⚠️ Before this leaves your machine

`funda_agent_exp.py` **hardcodes live credentials** at lines 56–61: an OpenAI API key, the
`DATABASE_URI`, and the Charts API secret. `os.getenv` still wins, so a real `.env` overrides
them, but the literals are in the file and are now in this bundle too.

They are also already in the git history of the source repo, so **rotating them is required
before this is shared, deployed, or pushed anywhere** — deleting the lines is not sufficient.

Related: `api_funda_agent_exp.py` has **no authentication**. Anything that can reach the port can
query the database through it. Do not expose it publicly without putting auth in front.

---

## Layout

```
funda_agent_exp.py            the agent — LangGraph ReAct loop, tools and progress memory
api_funda_agent_exp.py        FastAPI wrapper, token-by-token NDJSON streaming

agent_instructions.py         system/schema/inventory/scope/loop instructions
tool_prompts.py               tool descriptions and tool-returned feedback

openapi.json                  exported API schema (hand to the frontend with docs/API_CONTRACT.md)
requirements.txt              pinned versions the measurements in docs/ were taken on
regression.xlsx               90 rows: the original 33, plus 57 debugging/benchmark cases
funda_agent_exp_oracle_duo.py reference build — the pre-memory baseline.  DO NOT EDIT.

docs/     agent_exp_doc.md          the main documentation — architecture, prompts, guardrails
          API_CONTRACT.md           frontend integration: endpoints, events, suggestion buttons
          emp_findings.md           empirical findings across all experiments
          needle_haystack_*.md      benchmark test set and write-up

scripts/  ask_stream.py             streaming client — POSTs to the API, prints tokens as they arrive
          find_bigoutput.py         which survey+question combinations park (no LLM calls)
          bench_bigoutput.py        one large-output case, one arm
          run_condenser_ab.sh       workflow vs agent condenser A/B
          score_condenser_ab.py     scores that A/B against live SQL ground truth
          evaluate_routing_removal.py validates the single-model/full-prompt migration
          check_suggestions.py      asserts the {{suggestion}} contract the UI parses
          bench_memory*.py/.sh      short-term-memory latency and faithfulness
          bench_prefetch.py         inventory pre-fetch ablation
          bench_api_overhead.py     streaming on/off A/B through the HTTP layer
          bench_regression_exp.py   regression.xlsx sweep
```

## Where the prompts live

Every string the model reads comes from exactly two structured modules. `agent_instructions.py`
contains the persona, task, schema, system prompt, inventory preamble, scope text and loop
instructions. `tool_prompts.py` contains both tool descriptions and the feedback tools return.
The agent imports both directly and contains no model-facing text of its own.

The current survey's startup inventory and the typed `get_survey_analysis_packet(survey_id)`
tool share one packet contract and process-local LRU cache. The on-demand tool is restricted to
the current survey, a survey in the current organization, or the configured fixed benchmark;
historical survey discovery still happens through scoped SQL. Mutable packets use
`INVENTORY_CACHE_ACTIVE_TTL_S`, closed/archived packets use
`INVENTORY_CACHE_CLOSED_TTL_S`, and the fixed benchmark uses
`INVENTORY_CACHE_BENCHMARK_TTL_S` (24 hours by default).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

The agent needs **PostgreSQL** at `DATABASE_URI`. In the source repo that is a docker container
on port 5433 restored from `BE/db/updated_dump.sql`:

```bash
docker compose up -d postgres        # from the flavorai_v2 repo; ~5 min to restore
```

**Redis is not required.** An older comment in `funda_agent_exp.py:72` says parked results go to
Redis; the module actually imported is `output_store.py`, which keeps them in an in-process
`OrderedDict` bounded by `MAX_STORED_RESULTS=32`. Nothing here connects to Redis.

## Usage

**One question:**

```bash
python funda_agent_exp.py --prompt "Which product scored highest on overall liking?" \
  --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672
```

**Multi-turn conversation** (memory is per `--thread-id`):

```bash
python funda_agent_exp.py --chat --thread-id demo \
  --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672
```

**Server:**

```bash
uvicorn api_funda_agent_exp:app --host 0.0.0.0 --port 8000
```

**Streaming client** — this is the `stream.py` you asked for, `scripts/ask_stream.py`:

```bash
python scripts/ask_stream.py "Rank the products by overall liking" \
  --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672

python scripts/ask_stream.py --health
FLAVORAI_API=https://your-tunnel.ngrok-free.dev python scripts/ask_stream.py "..."
```

Only `prompt` and `survey_id` are required — `client_id` and `organization_id` are derived
server-side from the survey (`docs/API_CONTRACT.md` §4, and §14.4 of `docs/agent_exp_doc.md`).

## regression.xlsx — the test cases that found the bugs

The prompts that actually surfaced defects lived scattered across six scripts and two markdown
files, so there was no single list. They are now all in the spreadsheet, in two appended columns
**Suite** and **Case**:

| Suite | rows | what it catches |
|---|--:|---|
| *(blank — the original 33)* | 33 | the pre-existing regression set |
| Needle in haystack | 9 | picking the question that *means* "overall liking", and refusing when none exists (T4/T5 are absence cases) |
| Large SQL output | 8 | payloads that park and fire a condenser — E1–E4 and the condenser-A/B set L1–L4 |
| Routing-removal history | 3 | historical strong→weak/lean cases retained as regression inputs |
| Suggestions / ambiguity | 4 | the `{{...}}` button contract, and when buttons must *not* appear |
| Short-term memory | 12 | one 12-round conversation on a single `thread_id`, in order |
| Optimal SQL edge cases | 4 | SQL planning and retrieval edge cases |
| Inventory lifecycle + external packet | 4 | cache lifecycle and authorized packet retrieval |
| Demographic linkage edge cases | 1 | demographic-answer linkage |
| Persona reference | 3 | evidence-grounded persona/profile behavior |
| Feature influence reference | 3 | adjusted feature-influence analysis |
| Nested result rows | 2 | complete nested result presentation |
| Client-wide survey boundary | 2 | cross-survey authorization boundaries |
| Unclear intent handling | 2 | clarification behavior |

Regenerate or extend with `scripts/add_debug_cases_to_regression.py` (idempotent; `--dry-run`
to preview). Three things to know before using them:

- **`run_regression_exp.sh` still sweeps `seq 1 18`.** The appended rows do not change what the
  existing regression run does — widen the seq, or target one with
  `bench_regression_exp.py --row N`.
- **This workbook is not the parent repo's `regression.xlsx`.** The parent copy is sheet `Sheet1`,
  18 populated rows across 27 columns (it carries scoring columns and ~968 trailing blank
  formatted rows). This one is sheet `regression_cleaned`, 9 columns, and its 33 original rows
  are a strict **superset** of the repo's 18 — nothing was lost, and `seq 1 18` covers only the
  first 18 of 90 here.
- **The `Type of Query` column carries the expectation**, not just a label: which rows must
  refuse, what the payload size should be, historical routing expectations, and historical
  behavior notes (including the former stats-only latch behavior).
- **The 12 memory rows are one conversation**, not 12 independent prompts. Running them
  standalone tests nothing about memory.

`bench_regression_exp.py::load_rows` reads by column *position*, so Suite and Case were appended
at H and I where it ignores them. Verified: it still parses all rows and rows 1–18 are unchanged.

## Deliberately not copied

Four scripts were left behind because they benchmark **`funda_agent_exp` against its sibling
agents** and need files outside this bundle — copying them would have shipped scripts that
cannot run:

| script | needs |
|---|---|
| `bench_needle.py` | `funda_agent_v1.py` |
| `run_needle_bench.sh` | `funda_agent_v1.py` |
| `run_drill_ablation.sh` | `funda_agent.py` (via `bench_needle.py`) |
| `summarize_drill_ablation.py` | output of the above |

`docs/agent_exp_doc.md` §13 still lists them. Their results are already written up in
`docs/emp_findings.md` and `docs/needle_haystack_benchmark.md`, so nothing is lost but the
ability to re-run those two cross-agent comparisons here.

`funda_agent_exp_oracle_duo.py` **is** included — it is the baseline arm the memory benchmarks
A/B against, so `scripts/run_memory_bench*.sh` need it. Keep it byte-identical.

## Where to start reading

`docs/agent_exp_doc.md` is the main document. §3 is the architecture, §5 the tools (§5.4 covers
how computation is pushed into Postgres so the model reads statistics rather than rows), §7 result
parking and the two condensers, §12 known limitations. For frontend work, `docs/API_CONTRACT.md`
plus `openapi.json`.
