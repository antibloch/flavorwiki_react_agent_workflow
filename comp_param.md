# comp_param.md — parameters, probes and criteria of the OpenAI vs Gemini evaluation

Reconstructed from the three files that define and report that suite: `reg_plan.md` (method,
configuration, commands), `probes.md` (case selection), `regression.xlsx` →
`regression_cleaned` (per-case pass criteria), `gemini_vs_openai.md` (the report),
`exp/gemini_eval/BASELINE.txt` (captured baseline identity).

Run date **11 Sep 2026**. Surface: CLI `funda_agent_exp.py`. 14 cases / 15 requests per
provider, 30 runs total, **one run per probe per provider**.

**No credentials are reproduced here.** `reg_plan.md` §2/§13 carries live `OPENAI_API_KEY` and
`GOOGLE_API_KEY` values; they are deliberately omitted and should be rotated.

---

## 1. Model and runtime parameters

| Parameter | OpenAI (baseline) | Gemini (candidate) |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `gemini` |
| Model (`MODEL_NAME` / `LLM_MODEL`) | `gpt-5.5` | `gemini-3.5-flash` |
| Class | `ChatOpenAI` | `ChatGoogleGenerativeAI` |
| `MODEL_TEMPERATURE` | `0.0` | `0.0` (explicit — Gemini's own default of 1 is not inherited) |
| Tool binding | `bind_tools(tools, parallel_tool_calls=True)` | `bind_tools(tools)` — `parallel_tool_calls` is an OpenAI kwarg; Gemini decides fan-out itself |
| OpenAI-only tuning (`_llm_tuning()`: `reasoning_effort`, `use_responses_api`, `verbosity`) | forwarded | **not** forwarded |
| `prompt_cache_key` | passed at invoke time | gated off (adapter fix A1, below) |
| Key env var | `OPENAI_API_KEY` | `GOOGLE_API_KEY` |
| Package | `langchain-openai==1.4.1` | `langchain-google-genai==4.3.2` |

Held identical across both arms: five bound tools (`nl2sql_tool`,
`get_survey_analysis_packet`, `generate_word_cloud`, `run_survey_stats`, `analyze_plsr`),
streaming on, no prompt/tool-description/rule-block edits, same code revision, same database
state, same inventory setting, same thread sequence.

**Assembled system prompt hashed identically on both arms — `677054d5…`** (it is a pure
function of config; a difference would mean configuration drifted between halves of a pair).

### Shared environment

- Database `gpi_sample_db` on `localhost:5433` (Docker container `gpi-db`, user `gpi`) —
  confirmed byte-identical on both arms.
- Charts statistics API `https://charts-api.gpisurveys.com` (live; also used by the content grader).
- `langchain-core==1.5.1`, `langgraph==1.2.9`, `langgraph-checkpoint==4.2.0`,
  `langgraph-prebuilt==1.1.0`, `google-genai==2.8.0`, `google-auth==2.58.0`.

### Sandbox and source identity

Everything ran in a disposable sandbox `exp/gemini_eval/` with its own venv and its own
`conversation.sqlite3`; the working tree was never modified.

- Working-tree sources (unmodified): `funda_agent_exp.py` `33f81ed1…`,
  `agent_instructions.py` `8fd6aaf4…`, `tool_prompts.py` `fe5f9ac5…`, `plsr.py` `e064bb42…`.
- Sandbox `funda_agent_exp.py` rev2 (the one all probes ran on): `9e545e02…`;
  the other three modules byte-identical to the tree.
- Provider switch: `exp/gemini_eval/provider_switch.diff`, **23 changed lines** — the only edit,
  anchored so it fails loudly if the source moves.
- **Adapter fix A1:** `call_model` passed `prompt_cache_key` as an invoke-time kwarg, which
  `GenerateContentConfig` rejects (`Extra inputs are not permitted`). Gated to OpenAI in the
  sandbox copy only:
  `cache_key = _conversation_prompt_cache_key(config) if LLM_PROVIDER == "openai" else None`.

### Shim-neutrality control (2 extra OpenAI requests, before Stage B)

P01 and P11 run sandbox-OpenAI vs unmodified-tree-OpenAI: identical rosters, identical
153-token figure/label multiset, same chart type, zero tool calls — so later differences belong
to Gemini, not to the sandbox.

---

## 2. Probes — prompts, scopes, thread ids

All probes invoked as
`python funda_agent_exp.py --client-id … --survey-id … --org-id … --thread-id ${P}_pNN --prompt "…"`,
with `P` = `openai` | `gemini`. Four scope sets are reused:

| Scope set | Client-ID | Survey-ID | Organization-ID | Used by |
|---|---|---|---|---|
| `HERB` | `37cbf852-a2b7-4f8b-96b4-7f67432f88cd` | `6263cf71-23b7-4462-9ccf-4a00a7267672` | `c75d846a-e265-4f69-92c1-91308e0697f6` | P01 P02 P06 P10 P11 P13 |
| `STAT` | `14271fca-6b2d-46c3-a81a-72008d835ed4` | `5767397c-7360-4b72-b90d-c14150903192` | `cafb5fcb-5f2a-4a4a-a5e3-8dc4e525d2e3` | P03 |
| `HIST` | `f6b05cb4-cea2-4855-816e-c92e5e5d22ff` | `ae0c7287-a769-4b9c-a361-760f5b87b308` | `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | P04 |
| `MEM` | `c0b3b212-bc35-45ef-b69d-8257d3735a90` | `39af3240-42a8-4e35-8c7d-c61703d5ce3f` | `46671273-0b12-49a5-b9de-60f81d192818` | P05 P08 |
| `TAST` | `f6b05cb4-cea2-4855-816e-c92e5e5d22ff` | `e14528a9-01ac-4dff-844e-0914dbdb3759` | `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | P07 P09 P12 P14 |

| ID | Row | Suite / Case | Scope | Prompt (verbatim) | Flags |
|---|---:|---|---|---|---|
| P01 | 53 | Model routing / inventory | HERB | `Name the products tested, one per line.` | — (also Stage A smoke) |
| P02 | 52 | Model routing / latch | HERB | `What is the mean overall liking for each product? Give the figures only.` | `--no-inventory` |
| P03 | 19 | (Statistical analysis) | STAT | `Run ANOVA and Tukey test to compare products on Vertical rating Tasting 1.` | — |
| P04 | 12 | (Historical comparison) | HIST | `Compare respondent intensity scores for the dosage-level 5.0 product in this survey against the same product from the Copy - Gusto Dulce survey run on 2026-06-16. Report average intensity, spread, and respondent count for each.` | — |
| P05 | 59+60 | Short-term memory / round01+round02 | MEM | T1: `What are the products tested in this survey? Just list their names.` T2: `Now give me the mean Aroma score for each of those products.` | `--chat`, **one process**, turn 2 piped in |
| P06 | 102 | Benchmark KPI banner / compare_banner | HERB | `Compare overall liking against the benchmark` | — |
| P07 | 100 | Benchmark discovery / bench0 | TAST | `Compare this survey with the benchmark` | — |
| P08 | 91 | Word cloud tool / named_question_flat | MEM | `Create a word cloud for the answers to: What did you like most about the sample?` | — |
| P09 | 94 | PCA / pca2d | TAST | `Run a PCA on the statements about your experience with the product` | say "experience", never "expectations" |
| P10 | 121 | Herbalife showcase thread / turn07 | HERB | `Compute PLSR between the liking attributes and overall liking.` | — |
| P11 | 83 | Nested result rows / expandable_products | HERB | `For each product, show the mean, SD, and N for aroma liking, flavor liking, texture liking, sweetness liking, overall liking, and appearance liking. Present each product as an expandable parent level with its measures in one leaf table.` | — |
| P12 | 125 | Charts / descstat_heatmap | TAST | `Give me descriptive statistics for every liking measure by product.` | — |
| P13 | 86 | Client-wide survey boundary / foreign_terminal | HERB | `Compare this survey with survey 39af3240-42a8-4e35-8c7d-c61703d5ce3f. Include both survey titles and response counts. Do not use the benchmark.` | foreign survey id is `MEM`'s survey |
| P14 | 109 | Interpretation traps / impossible_join | TAST | `Compare overall liking by gender.` | — |

### How the 14 were selected (`probes.md`)

One deterministic representative per provider-sensitive execution boundary; both a success and a
refusal/absence case wherever the provider could hallucinate or cross a scope boundary; fully
scoped rows with explicit pass criteria; short high-signal prompts over large-result prompts; no
known-failing, accepted-blemish or never-run row used as a clean gate. Large-SQL, needle-in-
haystack, persona, long-memory and showcase-thread suites were deliberately excluded from the
minimum gate.

### Spend order

1. P01 (Stage A adapter smoke). 2. P02, P03, P05, P06, P08, P09, P12, P13, P14 (highest-risk
boundaries). 3. P04, P07, P10, P11. 4. Only then, one targeted repeat of a failed or technically
inconclusive case. No repeat budget is reserved in advance.

---

## 3. Evaluation criteria

Three layers: per-probe workbook criteria, suite-level acceptance criteria, and the grading
contract that decides who may issue which verdict.

### 3.1 Per-probe pass criteria (`regression.xlsx`, `Type of Query` column — authoritative)

- **P01 / row 53** — one turn, answered from the pre-fetched inventory with **no SQL at all**.
- **P02 / row 52** — run with `--no-inventory`; valid tool name/arguments, scope-bound SQL,
  tool-result continuation, final synthesis turn, no fabricated figures. *(The row's original
  "strong+schema → weak+lean latch" wording describes a router that no longer exists — see §4.)*
- **P03 / row 19** — workbook carries only the category label "Statistical analysis"; graded
  against typed statistics arguments, significance verdicts and oracle-derived numbers.
- **P04 / row 12** — workbook carries only "Historical comparison"; graded on cross-survey
  question matching, same-client scope expansion, comparison grain, and (per `probes.md` and
  PROTOCOL §5/§8) stating the matched prompt.
- **P05 / rows 59+60** — multi-turn rounds 1 and 2 of 12, same `thread_id`, in order; checkpoint
  continuation, follow-up reference resolution, per-round scratch reset, no stale survey scope.
- **P06 / row 102** — comparison-shaped Markdown banner: one column per survey product,
  **benchmark column last**, no pooled/averaged survey-side column. Overall liking A 5.95,
  C 5.86, D 5.57, benchmark Product B 6.00, n=150 each. Fail on a pooled survey-side figure, the
  benchmark survey title being printed, a banner on the no-benchmark path, or HTML. Known
  blemish, not a fail: restating the banner's means in a second Product × Stat table.
- **P07 / row 100** — say in plain language that no benchmark is set up, **then answer what it
  can from the survey itself**. Fail on raw field names (`has_assigned_benchmark`,
  `benchmark_context`), printed true/false, the word "configuration", or any hint at the Protein
  Bars benchmark.
- **P08 / row 91** — resolve the named open-answer question from inventory; call
  `generate_word_cloud` exactly once with `question_id d09ce2b5-2504-453a-8607-872efc005276`,
  `group_by_product=false`, `max_terms=50`; no `nl2sql_tool`, no raw verbatims. One `gpi-chart`
  block of type `word_cloud`, N=16, texture=9, flavor=3, enjoyable/fresh/freshness/pleasant=2.
  No second LLM invocation, no table, no suggestions, no legacy `gpi-word-cloud` fence.
- **P09 / row 94** — artifact is `pca_biplot_2d`, points carry **no** `z`, answer ends with the
  3D suggestion chip.
- **P10 / row 121** — names both sides, so it must run rather than ask back; expect
  `PLSR ANALYSIS COMPLETE`, the coefficient/VIP table and the MSE/RMSE/R² table. VIP order
  flavor 1.099, aroma 1.035, texture 0.981, sweetness 0.944, appearance 0.933. R²=1.0, MSE=0.0,
  RMSE=0.0 are **expected** (saturated fit: 3 rows, 5 attributes, 2 components); presenting
  R²=1.0 as evidence the model explains overall liking is an interpretation fail. *(Criterion is
  stale — live `PLSR_RULES` now makes asking back required; see §4.)*
- **P11 / row 83** — explicit one-dimensional product hierarchy: sibling top-level product
  `<details>` (only the first open), each with one leaf table of the six requested measures,
  columns Measure/Mean/SD/N; no outer layout table, no second `<details>` level, no placeholders,
  no `<br>`, exact neutral inline styles. *(Criterion is stale — current rules forbid this HTML;
  see §4.)*
- **P12 / row 125** — grade the **shape emitted, not the type** (chart type is model-variable).
  Pass: heatmap cells in a top-level `data` array with no `series` key (string x, string y,
  numeric value), **or** another supported type in its own contract shape (category family
  `series[].data[]` keyed label/value; scatter family numeric x/y); ≤8 series, ≤300 points;
  `grep -c 'could not render that chart'` returns 0. Fail: heatmap cells under `series[]`, a
  category-family point keyed x/y, or the withheld-chart sentence on an otherwise complete answer.
- **P13 / row 86** — requested survey belongs to another client. The code guard must refuse
  before data retrieval; expect one attempted tool call, then a terminal one-or-two-sentence
  answer with no second tool call, no partial current-side query, no UUID repetition, no table,
  no benchmark discussion, no clickable suggestion.
- **P14 / row 109** — the impossible join. TAST holds two disjoint cohorts of exactly 1200 with
  overlap 0. **FAIL: any table of liking by gender** — every cell would be a fabricated join.
  **PASS: it states the two cannot be crossed.**

### 3.2 Suite-level acceptance criteria (`reg_plan.md` §9)

Gemini passes only when, for every case:

1. every content verdict is correct or accepted by `oracle_agent`;
2. every applicable behaviour/presentation verdict passes in `contract_review`;
3. **the answer fulfils the task as asked** — no dropped measures, no substituted question,
   nothing narrower than the prompt. Correct numbers do not rescue an answer that did not do
   the job;
4. **every form it emits is one the harness itself defines** — a form the prompt authorises but
   neither prompt nor runtime validator shapes is a **harness defect**, not an untestable gap;
5. no new authorization, cross-thread or cross-survey contamination;
6. no new tool-call, streaming, artifact or termination failure;
7. **the parallel-tool-call batch path still fires where it fired for OpenAI** — assert
   `[tool-batch-timing] … calls=N>1` on at least one fan-out probe (P03 or P11) per provider, or
   record the difference as a named accepted provider gap;
8. any OpenAI-known blemish is preserved within its documented `probe_scratchpad.md` scope, or
   improved — never newly widened;
9. all §5 baseline-identity evidence captured for the evaluated revision.

**Explicitly not criteria:** latency, step count, token usage. Recorded as evidence, no grader,
no threshold; the CLI measures none of them. A large repeated difference is a finding to report,
not a fail.

### 3.3 Who grades what (`reg_plan.md` §11)

Every transcript goes to an **isolated grader** — a context with no memory of the adapter work
and which did not write it (Claude Code subagent / `codex exec` child). Never inline.

- **`oracle_agent` — content only.** Re-derives the correct answer from `gpi-db` and the live
  Charts API independently of the agent's tool calls, then grades numbers, matched question,
  ranking and significance. Never asked about presentation.
- **`contract_review` — presentation and behaviour only.** Checks against live rule text in
  `agent_instructions.py` / `tool_prompts.py` (chart shape, nested-table structure, benchmark
  banner, suggestion chips, disclosure timing) **and the tool calls themselves** — tool chosen,
  arguments, batching, order — cross-checked against `probe_scratchpad.md`'s accepted blemishes.
  Also owns criteria 3 and 4 above. It does not verdict numbers.

`contract_review` on **every** transcript, both providers (28 grades). `oracle_agent` whenever
the case reaches numbers, question matching, ranking or significance — 12 applicable cases per
provider. (The report's ledger line says "28 contract + 12 content grades"; its scoreboard shows
12 content verdicts *per provider*. The two counts disagree in the source.) A grader skipped must carry a
written reason. Verdicts are reported verbatim, including UNVERIFIED / UNTESTED — neither is a
pass.

**Grader packet** (no credentials): the case (row, Suite/Case, prompt, scope, pass criteria); the
transcript at budget with the full final answer; the evidence-grep output; the §5 baseline
identity; and the explicit note that this is a **provider migration, not a defect fix** — no
prior defect, no discrimination probe, no causal verdict to produce.

**Transcript budget:** user prompt, scope and rendered final answer kept **intact**; each
serialized tool call truncated to **500 characters**, each tool output to **1000**, truncation
marked. Decisive evidence past a cutoff is extracted field-by-field from the same run's
conversation log — never by re-running.

**Evidence grep** handed to the grader with the transcript:

```bash
for f in /tmp/${P}_p*.log; do
  echo "== $f"
  echo -n "withheld-chart sentences: "; grep -c "could not render that chart" "$f"
  grep -nE "tool-batch-timing|gpi-chart|gpi-nested-table|pca_biplot_2d|word_cloud|PLSR ANALYSIS COMPLETE|zero_row_streak|progress-delta" "$f" | head -20
done
```

### 3.4 Classification (`reg_plan.md` §12)

- **Regression** — Gemini fails a requirement the paired OpenAI run passes, or both fail and
  Gemini adds a failure beyond the documented OpenAI baseline.
- **No observed regression** — every Gemini verdict passes, or fails only where OpenAI fails
  identically within a documented accepted scope.
- **Inconclusive** — decisive evidence missing or truncated beyond recovery, the adapter failed
  before the agent path executed, or the divergence sits on a model-variable branch one run per
  side cannot separate. A reportable outcome, not a soft pass.

Every FAIL must name its owning component (prompt rule, tool payload, runtime code, memory
mechanism, adapter, or external dependency) and the first divergent boundary, traced along:
assembled prompt → model decision → tool chosen → tool arguments → [guardrail → scope repair →
authorization → DB/Charts API → normalization] → tool output → state/memory → final response →
sanitizer/assembly → transport.

### 3.5 Stage A — adapter compatibility gate (run on P01, graded separately)

Model imports and process starts; system/user messages accepted in LangChain form; streamed
chunks recognised; tool calls arrive with expected name and JSON-compatible arguments; tool
results appendable and followed by another model turn; final no-tools synthesis turn works;
thought/reasoning parts **not** rendered as user text; OpenAI-only kwargs not forwarded; usage
metadata normalised or explicitly marked unavailable; NDJSON `status`/`token`/`reset`/
`tool_result`/`done`/`error` unchanged. A failure here is an **adapter compatibility failure**,
not a regression finding — stop and fix.

---

## 4. Criteria known to be stale at run time

Recorded in `gemini_vs_openai.md`; they limit what four probes' workbook criteria can decide.

> **Rewritten 16 Sep 2026.** Rows 52, 53, 54, 83, 89, 100 and 121 of `regression.xlsx` have since
> had their `Type of Query` criteria rewritten in place against the live rules; no rows were
> deleted and no row numbers moved, so every citation in this file remains valid. The list below
> is the state **at run time** and explains what the 11 Sep verdicts could and could not decide —
> it is history, not the workbook's current content. Each rewritten cell carries a dated
> `[Rewritten 16 Sep 2026: …]` note naming what it replaced.

- **Rows 52 / 53** — "strong+schema / weak+lean" routing describes a router that no longer
  exists (one `MODEL_NAME`, one `llm`, no model-choice log line). *UNTESTED-criterion-obsolete on
  both providers*, not a pass.
- **Row 83 (P11)** — mandates `<details>`, `<summary>` and exact neutral inline styles, all now
  forbidden by `NESTED_RESULT_RULES` / `EXPECTED_OUTPUT` / `docs/API_CONTRACT.md`, plus a leaf
  shape `REPORTING_RULES` forbids: a conforming answer scores as a failure and vice versa.
- **Row 121 (P10)** — says PLSR must run rather than ask back; live `PLSR_RULES` makes asking
  back **required**, with a runtime backstop. Both providers correctly asked back.
- **Row 100 (P07)** — the "answer what it can" half is authorized but its shape is defined
  nowhere, while a second rule calls the obvious realization "filler".

Also open at run time: the PLSR `ncomp3`/`ncomp_cap` correction obligation was never exercised
(zero `analyze_plsr` calls on P10), so both P10 graders returned `CORRECTION VERDICT: UNVERIFIED`.

---

## 5. Reproduction

**The sandbox was removed on 16 Sep 2026** (220 MB, and a stale fork of the agent modules);
it rebuilds from `reg_plan.md` §4's idempotent bootstrap. Its baseline identity, the provider
switch, its `requirements.txt` and the conversation log for all 30 runs are preserved in
`docs/gemini_eval/` — see that folder's `README.md`.

Sandbox `exp/gemini_eval/` (rev2 `9e545e02…`, 23-line diff); working-tree sources unmodified at
`33f81ed1…` / `8fd6aaf4…` / `fe5f9ac5…` / `e064bb42…`; `langchain-google-genai==4.3.2`,
`langchain-openai==1.4.1`, `langgraph==1.2.9`; database `gpi_sample_db` on `localhost:5433`;
`LLM_PROVIDER=openai|gemini` the only env difference between arms. Full command block:
`reg_plan.md` §13. Per-case grader text: session ledger `verdicts.md` (28 contract + 12 content
grades, verbatim). Report: `gemini_vs_openai.md`.
