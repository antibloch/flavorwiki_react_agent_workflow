# Plan: rework `schema_overview.md` into an analytical decision procedure

**Scope: `final_agent_work/` only.** Every path below is relative to that directory.
Point 7 of the source proposal (the compile/build pipeline) is excluded by request.

Every number in this document was measured against the live `gpi_sample_db` container
(`docker exec gpi-db psql -U gpi -d gpi_sample_db`) on 2026-08-06, not assumed.

---

## 0. Task 1 — is `docs/db_schema.md` up to date? **Yes, exactly.**

Verified by dumping the live catalog and diffing it against the document
programmatically (three parsers: §1 bullet lists, §3 relationship graph, §4 markdown
tables).

| Check | Live | `db_schema.md` | Diff |
|---|---:|---:|---|
| Base tables (`public`) | 112 | 112 | **none** |
| Columns (all tables, §4) | 1,214 | 1,214 | **0 missing, 0 extra** |
| Column data types (§4) | — | — | **0 mismatches** |
| Declared foreign keys (§3) | 334 | 334 | **0 unbacked claims, 0 omissions** |
| Core-table columns (§1: `survey`, `question`, `question_option`, `product`, `enrollment`, `answer`, `answered_question_options`) | 132 | 132 | **0 diffs** |

`db_schema.md` needs no regeneration. It is a faithful catalog and is safe to use as the
**column-existence oracle** for this rework.

Two caveats that bound how it may be used:

1. **It is not loaded by the agent** and should not be. 97,619 chars ≈ 24k tokens, three
   times `schema_overview.md`, and ~90% of it is tables the four agent routes never touch.
2. **Its generated query patterns must not be copied.** The proposal's criticism is
   correct and I confirmed it in the file:
   - `db_schema.md:296` "Pattern 3: Get product scores" uses `LEFT JOIN answer a` and then
     puts `a.question_id = :question_id` in the `WHERE`, which null-rejects the outer side
     and silently converts it to an inner join.
   - The same pattern casts `optionAnswer` to numeric with no `~ '^-?[0-9]+(\.[0-9]+)?$'`
     guard, and returns `AVG` with no `COUNT`/`STDDEV`.
   - `db_schema.md:321` "Pattern 4" divides an option distribution by **all survey
     enrollments**, not by the people who answered that question.

   These are exactly the errors `schema_overview.md` exists to prevent. Catalog = facts.
   Curated file = semantics.

Reproduce with the scripts left in the session scratchpad, or:

```bash
docker exec gpi-db psql -U gpi -d gpi_sample_db -At -F'|' -c \
  "select table_name, column_name, data_type from information_schema.columns
   where table_schema='public' order by 1,2;"
```

---

## 1. Verified baseline for `schema_overview.md`

### 1.1 One consumer, two entry points

```
funda_agent_exp.py:202   SCHEMA_OVERVIEW_PATH = Path("schema_overview.md")
funda_agent_exp.py:203   AGENTS_YAML = yaml.safe_load(Path("agents.yaml") ...)
funda_agent_exp.py:204   TASKS_YAML  = yaml.safe_load(Path("tasks.yaml") ...)

api_funda_agent_exp.py:51   import funda_agent_exp as agent
```

`funda_agent_exp.py` is the only reader; the FastAPI variant imports it and inherits the
same prompt. One file to change, one prompt to regress. Paths are CWD-relative — everything
must be run from `final_agent_work/`.

### 1.2 The file today

`schema_overview.md` = **32,501 chars ≈ 8,100 tokens**, sent on every turn where
`sql_done == False`.

| Section | Chars | Share |
|---|---:|---:|
| 5. Query-shape rules | 11,725 | 36.1% |
| 3. Exact core schema and canonical survey routes | 7,620 | 23.4% |
| 2. Semantic domains | 4,172 | 12.8% |
| Stable rules | 3,838 | 11.8% |
| 4. Minimal table bundles | 1,330 | 4.1% |
| 1. Analytical grain | 1,245 | 3.8% |
| Execution contract | 1,158 | 3.6% |
| 6. Stability boundary | 746 | 2.3% |

The proposal's 37% / 24% split is confirmed.

### 1.3 The route taxonomy is real and the rows are contiguous

`regression.xlsx` has **69 populated rows**, indexed 1-based by
`scripts/bench_regression_exp.py --row`:

| Route | `--row` values | n |
|---|---|---:|
| Survey type (current) | **1–10** | 10 |
| Historical comparison | **11–17** | 7 |
| Statistical analysis | **18–23** | 6 |
| Benchmark comparison | **24–33** | 10 |
| Semantic-match / absence traps | **34–38** | 5 |
| "Isolates" traps | **40–42** | 3 |
| Infrastructure (condenser, memory, routing, buttons) | 39, 43–69 | 28 |

The proposal's four routes `{current, statistics, benchmark, historical}` are exactly the
taxonomy this suite already encodes. This is the strongest idea in the proposal and it is
empirically grounded.

### 1.4 The regression gate is currently unusable as a gate — fix it first

`scripts/run_regression_exp.sh`:

```bash
PY="${PY:-BE/.venv/bin/python}"     # (1) no BE/ under final_agent_work — path does not exist
for R in $(seq 1 18); do            # (2) covers rows 1-18 only: misses benchmark + traps
  for V in none bindings prefetch_fixed both; do   # (3) 4 variants of a different experiment
```

Gating on rows 1–18 would let a benchmark (24–33) or semantic-match (34–38) regression ship
unnoticed. **Fix in Phase 0**, before any content change.

---

## 2. Verdict on the proposal, idea by idea

### 2.1 Confirmed — ship these

| Idea | Evidence |
|---|---|
| **`survey_nomenclature` country claim is wrong** (§2.1) | Its 23 columns hold `survey_date`, `*_snapshot` labels, `generated_name/title`, `unique_name`, `survey_url`, audit cols — **no country**. `survey.country` exists, non-null on **31 / 1,347** surveys (2.3%), non-blank on **23** (1.7%). `survey_nomenclature` holds **16 rows for 1,347 surveys** (1.2%). `schema_overview.md:142` is live-wrong today. |
| **`answer.product_id` is not an enforced FK** (§2.3) | `answer` declares exactly two FKs: `question_id → question.id`, `enrollment_id → enrollment.id`. But the relation *holds*: **0 orphan `product_id`**, and **0 rows** where `product."surveyId" <> enrollment.survey_id`. So this is a documentation-correctness fix, not a data-quality problem. Keep the cross-check for cross-survey work where it earns its cost. |
| **Benchmark architecture exists** (§2.2) | `survey.is_benchmark_source`, `survey.benchmark_category_label`, `client.is_benchmark_source`, and `benchmark_registry(id, category_label, survey_id, product_id, internal_label, is_active, created_at)` all exist. |
| **Denominator contract** (§3) | Genuinely additive: `schema_overview.md` contains **zero** occurrences of `STDDEV` and has no denominator vocabulary at all. The four-denominator distinction is currently unstated anywhere in the stack. |
| **Grain-first / duplication factor `d`** (§6) | See §3.2 below — the measured multiplicities make this concrete rather than advisory. |
| **Historical candidate scoring** (§2.4) | Strongly supported: only **330 of 1,347 surveys have any answers** (24.5%), **165 (org, title) pairs are duplicated**, 177 templates, 60 archived, 580 drafts. "Has responses" must outweigh title similarity. |
| **Replace worked queries with invariants** (§4) | Supported, and §3.1 below shows the file's flagship worked example is actively wrong. |
| **Four-route state machine at the top** (§5) | Matches the regression taxonomy exactly (§1.3). |
| **Keep the question-type storage matrix** (§5, phase 5) | Keep it — but it is **incomplete**, see §3.1. This is the proposal's one significant blind spot. |

### 2.2 Corrections — the evidence contradicts the recommendation

#### (a) The benchmark registry is one synthetic row. Document the route; do not build on it.

```
benchmark_registry: 1 row
  category_label = 'Protein Bars'
  survey_id      = 4ec4b648-99bd-4d72-89ee-9cf1b7626e4c
  product_id     = d11af4ab-d207-4f57-9dcd-eb560ee0163b
  internal_label = "Synthetic protein bar concept test v1 — top scorer 'Product B'
                    (mean 6.00), n=150/product"
  is_active      = true

surveys with is_benchmark_source = true : 1
clients with is_benchmark_source = true : 1
distinct benchmark_category_label       : 1  ('Protein Bars')
```

That `survey_id` is **byte-identical to the one hardcoded at `agents.yaml:21`**. A registry
lookup today spends a query to learn a value already in the prompt, and adds a failure mode
(empty/inactive registry → no benchmark at all) for zero information gain.

Worse, the three signals **disagree with each other today**:

| survey | `is_benchmark_source` | `benchmark_category_label` | in registry |
|---|---|---|---|
| `4ec4b648…` "Confidential benchmark product…" | **true** | *null* | **yes** |
| `6263cf71…` "Herbalife Nutrition Survey" | false | **'Protein Bars'** | no |

An agent told to "resolve the benchmark through the flags" could land on the Herbalife
survey. The proposal says to verify population before switching. It is verified: **no**.

**Do:** describe the registry route in `schema_overview.md` so benchmark identity reads as
*configurable* rather than intrinsic, stating that it holds one active category today and
that `agents.yaml`'s fixed IDs remain authoritative.
**Don't:** touch `agents.yaml`'s benchmark carve-out (lines 87–107). Revisit when
`SELECT count(DISTINCT category_label) FROM benchmark_registry WHERE is_active` ≥ 2.

#### (b) "Move operational rules to the base prompt" is backwards for cost.

The most consequential correction. Three prompt slots with different send frequencies:

| Slot | Content | Sent when |
|---|---|---|
| **A** tool schema | `nl2sql_tool` / `run_survey_stats` docstrings | every turn with tools bound |
| **B** lean system prompt | `agents.yaml` role/goal/backstory + `PG_DIALECT_RULES` + `tasks.yaml` expected_output + `REPORTING_RULES` | **every turn** |
| **C** schema block | `schema_overview.md` | only when `sql_done == False` |

`funda_agent_exp.py:1656-1685` puts C last so B is a literal prefix of B+C, and asserts it:

```python
assert SYSTEM_PROMPT.content.startswith(SYSTEM_PROMPT_LEAN.content), (
    "SYSTEM_PROMPT_LEAN must stay a literal prefix of SYSTEM_PROMPT, or turn 1 loses the cache"
)
```

**C is the only slot that ever gets dropped.** Moving a SQL-only rule from C into B makes
the prose turn start paying for a rule it cannot use — the exact regression the schema-last
layout was built to prevent.

Corrected destination rule:

| Content type | Destination | Why |
|---|---|---|
| Needed only while writing SQL | **stay in C**, or move to A | C is dropped once SQL is done |
| Needed while writing prose | **B** (`REPORTING_RULES` / `tasks.yaml`) | must survive the lean switch |
| Already enforced deterministically | **delete** — let the rejection teach | `_SEMANTIC_FILTER_REASON`, `_reject_reason`, `_ZERO_ROW_MSG` fire at the moment of the mistake |
| Duplicated across layers | one canonical owner, delete the rest | §5 |

The reduction comes from **deduplication and compression, not relocation.** Only two blocks
genuinely belong elsewhere, and both are already fully owned there:

- §5 "Statistical analysis" (~1,100 chars) → `agents.yaml:41-52` and `:66-85` already carry
  the complete `run_survey_stats` routing policy. Delete from C.
- §5 "Summary versus raw response data" (~1,400 chars) → `tasks.yaml` expected_output +
  `REPORTING_RULES` own answer shape. Delete from C.

#### (c) 13,000–16,000 chars is not reachable, and the target should go *up*, not down.

The proposal asks for a 50–60% cut *and* five new sections. §3 below adds a sixth
(complete type coverage) that is larger and more valuable than any of them.

```
32,501  current
-17,600  cuts   (§3 →4,000, §5 →5,000, §2+§4 merged →1,800,
                 Stable rules →2,200, Execution contract →300, §6 →400)
+ 5,200  proposal additions (state machine, denominators, signature, coverage, benchmark)
+ 2,600  type-coverage + multiplicity additions (§3)
─────────
~22,700  target  (30% reduction)
```

**Set the target at 21,000–23,000 chars.** Char count is a **budget, not the gate**. Every
rule in this file has a measured failure behind it — `funda_agent_exp.py`'s comments record
accuracy numbers for most of them. The gate is the regression score.

#### (d) Minor: two of the proposal's suggested discriminators are inert.

`survey.is_container` is **false on all 1,347 rows** — adding `NOT is_container` to the
historical scoring query is a no-op predicate. `survey."openedAt"` is populated on **23**
surveys; `publishedAt` on 598. Rank on `publishedAt`/`createdAt`, treat `openedAt` as a
bonus signal only.

### 2.3 Rejected for now

**Physical route-specific schema injection** (proposal §6 "route-specific injection",
6,000–10,000 chars/request). Deferred — see Phase 5. The route must be chosen *before* the
first LLM call, so the classifier is a keyword matcher: the same "decide meaning with a
pattern" failure mode `_SEMANTIC_FILTER_RE` exists to reject, one layer up. Misclassification
is silent and unrecoverable — an agent handed the `current` slice cannot discover it needed
`historical`.

---

## 3. What the proposal missed — the highest-value findings

These came out of profiling actual response storage, and they outrank most of the
proposal's items. The proposal says the type→storage matrix is "arguably the most important
part of the file" and should be kept "almost unchanged". It is the most important part —
**and it is wrong or silent on the majority of question types.**

### 3.1 The dispatch table covers 8 of 18 question types

`question."typeOfQuestion"` distribution, live:

| type | questions | surveys | in `schema_overview.md`? |
|---|---:|---:|---|
| `multiple-choice` | 3,097 | 852 | yes |
| `line-scale` | 1,164 | 514 | yes — **but see §3.3** |
| `open-answer` | 985 | 462 | yes |
| `vertical-rating` | 640 | 124 | yes |
| `multiple-open-answer` | 565 | 260 | yes |
| `info` | 558 | 333 | **no** |
| `triangle-test` | 345 | 237 | **no** |
| `email` | 335 | 208 | yes |
| `tetrad-test` | 322 | 230 | **no** |
| `matrix` | 189 | 98 | yes |
| `contact-information` | 184 | 132 | yes |
| `time-intensity-slider` | 178 | 126 | yes — **but see §3.4** |
| `individual-balloting` | 153 | 121 | **no** |
| `tcata` | 143 | 111 | listed, **described wrongly** |
| `tds` | 141 | 110 | **no** |
| `upload-multimedia` | 70 | 40 | **no** |
| `ranking` | 39 | 31 | yes — partial |
| `paired-questions` | 30 | 20 | yes — partial |

**1,332 question definitions across ~700 survey-appearances are undocumented.** A grep of
`schema_overview.md` returns 0 hits for `triangle-test`, `tetrad-test`, `tds`,
`individual-balloting`, `upload-multimedia`, `sectionCommentAnswer`, `optionLabel`,
`hasPiping`.

Measured `answerData` keys per type (the ground truth the dispatch table should encode):

| type | `answerData` keys | aqo FK used | value path |
|---|---|---|---|
| `vertical-rating` | `optionAnswer` (100% numeric, 0–9) | `question_option_id` | numeric, 1 row/answer |
| `line-scale` | `optionAnswer` (100% numeric, 0–99.6) | `question_option_id` | numeric, **N rows/answer** |
| `multiple-choice` | `optionAnswer` (**text label**, 155,863 rows) | `question_option_id` | label is in `answerData` *and* via `question_option.label` |
| `matrix` | `optionLabel` (116,609) | `question_option_id` + `matrix_row_option_id` | row = `matrix_row_option_id → label`; value = selected option's `analytical_value` |
| `multiple-open-answer` | `optionAnswer` (text) | `question_option_id` | text per sub-field |
| `paired-questions` | `optionAnswer`, `responseType` | `question_pair_id` (+ `question_option_id`) | pair via `question_pair` |
| `triangle-test` | `optionAnswer` (numeric sample code), `sampleLabel` | **`question_set_id`** | discrimination test; set via `question_set` |
| `tetrad-test` | `optionAnswer`, `sampleLabel`, `group` | **`question_set_id`** | discrimination test |
| `tcata` | **`action`, `t_ms`** | `question_option_id` | temporal CATA — an event stream, **not** a selected option |
| `tds` | **`action`, `t_ms`** | `question_option_id` | temporal dominance — event stream |
| `time-intensity-slider` | `optionAnswer`, `t_ms` | `question_option_id` | **time series**, 53.6 rows/answer |
| `individual-balloting` | `optionAnswer`, `sectionCommentAnswer` | `question_option_id` | numeric + free-text comment |
| `ranking` | `rank`, `optionLabel`, `justificationText` | `question_option_id` | rank int + optional justification |
| `open-answer`, `email`, `upload-multimedia` | — (no aqo rows at all) | — | `answer.value` |
| `contact-information` | `optionAnswer` (text) | `question_option_id` | ~5 rows/answer, one per field |
| `info` | — | — | **never answered** — display-only |

Two corrections the file needs beyond adding rows:

- **`tcata` is misdescribed.** `schema_overview.md:707` lists `tcata` alongside
  `multiple-choice` as "selected option". It is a time-stamped `action`/`t_ms` event
  stream (547 rows). Counting it as a selection produces a wrong distribution.
- **`info` questions never carry answers.** 558 of them across 333 surveys. Any
  "how many questions does this survey have" / "which questions were unanswered" answer
  that does not exclude `info` reports a false skip rate.

### 3.2 Row multiplication is measurable per type — publish the numbers

The file's guidance is qualitative ("joining it may multiply answer rows"). The proposal
asks for a duplication factor `d`. Here it is, measured:

| type | aqo rows per `answer` | meaning of the extra rows |
|---|---:|---|
| `vertical-rating` | **1.00** | none — 1:1, the safe overall-liking instrument |
| `triangle-test` | 1.00 | none |
| `multiple-choice` | 2.18 | multi-select |
| `tetrad-test` | 3.98 | samples in the set |
| `tds` | 4.62 | dominance events over time |
| `ranking` | 4.64 | one row per ranked item |
| `contact-information` | 4.98 | one row per field |
| `line-scale` | **5.18** | **one row per attribute slider** — see §3.3 |
| `tcata` | 5.36 | CATA events over time |
| `individual-balloting` | 7.84 | ballot items |
| `paired-questions` | 9.63 | pairs shown |
| `matrix` | **9.80** | one row per matrix row (statement) |
| `multiple-open-answer` | **20.77** | one row per sub-field |
| `time-intensity-slider` | **53.62** | **time-series samples**, not repeat ratings |

This turns "check for multiplication" into an **expected-multiplicity assertion**: if
`joined_rows / distinct answers` for a `vertical-rating` question is not 1.00, the join is
wrong; if it is 53 for a `time-intensity-slider`, it is correct and the aggregate must
collapse the series first.

### 3.3 The file's flagship cross-survey example contains the pooling bug it warns about

`schema_overview.md:320-348` — the canonical cross-survey comparison — filters
`q."typeOfQuestion" = 'line-scale'`, then computes

```sql
AVG((aqo."answerData" ->> 'optionAnswer')::numeric) AS avg_score
... GROUP BY m.survey_id, m.prompt;
```

**A `line-scale` question is not one scale — it is N sliders**, each a `question_option`
with its own label and its own `optionAnswer`:

```
prompt      | label | optionAnswer
------------+-------+----
Line scale  | One   | 6
Line scale  | Two   | 10
Line scale  | Three | 13
Line scale  | Four  | 6
Line scale  | Five  | 9      -- one answer, five attributes
```

Distribution of sliders per `line-scale` question: **only 247 of 1,164 (21%) have a single
option.** 252 have 4, 181 have 5, and some have up to 13. At answer level, only 547 of 2,783
line-scale answers are single-attribute.

So that example silently averages *different attributes* into one unitless number, and does
it in the section the model reads when doing historical comparison (regression rows 11–17).
`vertical-rating` — exactly 1.00 option per answer — is the type that is safe to average by
`question_id` alone.

**Fix:** for any `line-scale` / `matrix` / `time-intensity-slider` measure, the grain is
`question_id × question_option_id` (× `product_id`), never `question_id` alone. This is the
single highest-value correction in the whole rework.

### 3.4 Scale-signature metadata is type-conditional

`schema_overview.md:403` states flatly that the scale lives in
`question_option."optionSettings"->'positionLabels'`. Measured, that key exists only for:

| type | questions | with `positionLabels` |
|---|---:|---:|
| `line-scale` | 1,164 | 1,162 |
| `vertical-rating` | 640 | 628 |
| `time-intensity-slider` | 178 | 178 |
| everything else | 3,000+ | ≤ 1 |

For `matrix`, `multiple-choice`, `tcata`, `tds`, `ranking`, the scale signature must come
from `COUNT(question_option)` plus the observed `analytical_value` range. Note also that
`analytical_value` is populated for **every** option of every scored type (0 nulls), but
carries sentinels for matrix (max observed 7,777) — validate the range, don't assume it is
the scale.

The file's warning that `positionLabels` is *not* on `question.settings` is correct and
should stay; `question.settings` keys are `follow_up_settings`, `answer-type`,
`settingSlider`, `sampleA`/`sampleB`/`trial_no`/`number_of_sets` (discrimination tests), etc.

### 3.5 `answer.value` is blank for every structured type

| type | answers | `value` non-blank |
|---|---:|---:|
| `open-answer` | 16,708 | 16,651 |
| `email` | 241 | 233 |
| `upload-multimedia` | 41 | 39 |
| `individual-balloting` | 93 | 52 |
| `multiple-choice` | 75,141 | 845 |
| `vertical-rating`, `matrix`, `line-scale`, `paired-questions`, `tds`, `tcata`, `time-intensity-slider`, `contact-information`, `multiple-open-answer` | 179,769 | **0** |

`COALESCE(aqo."answerData"->>'optionAnswer', a.value)` (as used in the file's gender example)
is safe. Any path that reads `answer.value` *alone* for a non-text type returns nothing —
and returns it silently.

### 3.6 Product attribution varies sharply by type

`answer.product_id` non-null rate: `vertical-rating` 100.0%, `line-scale` 95.0%,
`paired-questions` 98.7%, `matrix` 61.0%, `multiple-choice` 32.1%, `triangle-test`/
`tetrad-test` **0%**. This is why the proposal's `product_linked_n / answered_n` coverage
ratio matters: a product breakdown of a `multiple-choice` question drops two-thirds of
responses by default, and a product breakdown of a discrimination test drops all of them.

---

## 4. Target structure

```
 1. Purpose and scope                                          ~400
 2. Retrieval state machine (phases, compact)                ~1,800
 3. Core analytical graph + grain                            ~2,000
 4. Grain, multiplicity and denominator invariants   [NEW]   ~2,000   (§3.2 table lives here)
 5. Question resolution and instrument signature     [NEW]   ~1,600
 6. Response-storage dispatch table            [EXPANDED]    ~3,400   (18 types, §3.1)
 7. Route playbooks
      7.1 Current survey                                      ~700
      7.2 Statistics                                          ~600
      7.3 Benchmark (incl. registry route)          [NEW]      ~900
      7.4 Historical (incl. candidate scoring)                 ~900
 8. Validation and zero-result diagnostics                   ~1,500
 9. Exact core columns                                       ~4,000
10. information_schema fallback                                ~300
                                                            ────────
                                                            ~22,700
```

### Preserved essentially intact

1. **The analytical-grain table** (§1) plus the row-multiplication warning — now backed by
   measured factors.
2. **The two-stage question-resolution rule** (structural candidate list → pick from full
   prompts → aggregate chosen IDs). The extra query is justified whenever
   `P(wrong question) × cost(wrong answer) > cost(one query)`, and a confidently wrong
   measure is expensive. The `_SEMANTIC_FILTER_RE` rejection enforces it deterministically.
3. **The `positionLabels`-is-on-`question_option` correction** — now qualified by type.

### Worked queries → invariants

Full SQL examples get copied along with their accidents — §3.3 is the proof. Per operation,
keep the invariant:

```text
Numeric product score
  Grain:    question_id × question_option_id × product_id
            (× question_option_id is REQUIRED for line-scale / matrix /
             time-intensity-slider; vertical-rating is 1:1 and may omit it)
  Route:    question → answer → enrollment;  answer.product_id = product.id (logical)
            answer → answered_question_options
  Value:    NULLIF(aqo."answerData"->>'optionAnswer','')::numeric
            guarded by  ~ '^-?[0-9]+(\.[0-9]+)?$'
  Scope:    enrollment.survey_id = :survey_id
  Return:   question_id, full prompt, "typeOfQuestion", attribute label, product,
            numeric_n, answered_n, respondent_n, product_linked_n,
            AVG, STDDEV_SAMP, MIN, MAX
  Validate: numeric_n <= answered_n
            aqo_rows / answers matches the type's expected multiplicity (§4 table)
            product."surveyId" = enrollment.survey_id
```

Keep at most **two** full worked queries: the tenancy-verification join (non-obvious, three
hops through `account`) and the historical candidate-scoring query. Delete the rest,
including the broken cross-survey example.

### The denominator contract (new, §4)

| Requested quantity | Default denominator |
|---|---|
| Participation / completion rate | enrolled (or eligible) participants |
| Distribution of answers | respondents who answered *that question* |
| Product score | valid product-attributable numeric responses |
| Missing / skipped rate | respondents presented with the question (**exclude `info`**) |
| Multi-select percentages | question answerers; totals may exceed 100% |

Paired with the coverage diagnostics every aggregate must return — `answered_n`,
`numeric_n`, `respondent_n`, `product_linked_n` — so `numeric_n / answered_n` and
`product_linked_n / answered_n` are visible and reported when materially below 1.

---

## 5. Canonical-owner table (drives every deletion)

Measured duplication across the four layers:

| Canary | schema_overview.md | agents.yaml | funda_agent_exp.py |
|---|---:|---:|---:|
| `:survey_id` | 14 | 0 | 11 |
| `typeOfQuestion` | 12 | 0 | 8 |
| `COUNT(DISTINCT` | 7 | 0 | 12 |
| `run_survey_stats` | 5 | 3 | 12 |
| `positionLabels` | 2 | 0 | 6 |
| `ILIKE` | 2 | 0 | 5 |
| `STDDEV` | **0** | 0 | 5 |

One owner per rule; delete elsewhere:

| Rule | Canonical owner | Action |
|---|---|---|
| No `LIKE`/`ILIKE` on `prompt`/`label` | `_SEMANTIC_FILTER_REASON` (fires at the mistake) | shrink in C to the *why* + 2-line pointer |
| Scope placeholders `:client_id` etc. | `PG_DIALECT_RULES` "SCOPE IDS" | cut C's 14 mentions to the SQL examples only |
| PostgreSQL dialect limits | `PG_DIALECT_RULES` | C keeps a one-line pointer (already true) |
| `run_survey_stats` routing / fallbacks | `agents.yaml:41-52, 66-85` | delete §5 "Statistical analysis" from C |
| Answer shape, aggregates-not-raw-rows | `tasks.yaml` expected_output + `REPORTING_RULES` | delete §5 "Summary versus raw" from C |
| N + SD with every mean, separability | `REPORTING_RULES` | C references, does not restate |
| No UUIDs in prose | `REPORTING_RULES` | — |
| Zero-row diagnostic procedure | `_ZERO_ROW_MSG` | C keeps the *concept* in §8, not the wording |
| Grain, multiplicity, denominators, storage paths, routes | **`schema_overview.md`** | this is what C is for |

Out-of-scope domains (logic, panel admin, saved reports, sharing, files, billing, system
tables) collapse to one line:

> For a request outside survey responses, products, statistical analysis, benchmark
> comparison, or historical comparison, inspect `information_schema` before using an
> undocumented table.

---

## 6. Phases, each gated

Gate command (after the Phase 0 fix), run from `final_agent_work/`:

```bash
PY=/path/to/python scripts/run_regression_exp.sh
```

`instrumentation_logs_funda_exp.jsonl` already records per-turn `input_tokens` /
`cached_input_tokens` / `output_tokens` / `latency_s`, so the token effect needs no new
instrumentation.

### Phase 0 — Make the gate real, then baseline

- Fix `scripts/run_regression_exp.sh`: correct the `PY` default; extend `seq 1 18` to cover
  **rows 1–42**; drop the four-variant fan-out in favour of one variant × 3 reps.
- Baseline: per-route scores, mean turn-0 input tokens, cache-hit %, wall time. **Three
  reps** — run-to-run wall spread is wide enough to swallow a 4% effect, so prefer token
  counts, which are far lower-variance.

**Gate:** baseline recorded. No content change ships in this phase.

### Phase 1 — Correctness fixes (highest value, lowest risk)

1. **§3.3 line-scale grain** — rewrite/delete the broken cross-survey example; state that
   `line-scale` / `matrix` / `time-intensity-slider` require `question_option_id` in the
   grain, and that `vertical-rating` is the 1:1 type.
2. **§3.1 tcata correction** — move it out of the "selected option" row into the temporal
   event-stream row alongside `tds`.
3. `survey_nomenclature` → remove the country claim; point country at `survey.country`; add
   the 2.3% / 1.2% sparsity figures.
4. `answer.product_id` → restate as a logical join, not an enforced FK; note 0 orphans and
   0 survey mismatches observed; keep the `product."surveyId" = enrollment.survey_id`
   cross-check for cross-survey work.
5. `positionLabels` → qualify as line-scale / vertical-rating / time-intensity-slider only.

**Gate:** rows 1–42 flat or better. Historical rows **11–17** are where fix (1) should show
up as an improvement; a *drop* anywhere means something else broke.

### Phase 2 — Complete the semantics (additive; the file grows here)

6. **Expand the dispatch table to all 18 types** (§3.1), including `triangle-test`,
   `tetrad-test`, `tds`, `individual-balloting`, `upload-multimedia`, and `info`
   (display-only, never answered).
7. **Expected-multiplicity table** (§3.2) as an assertable invariant.
8. Grain + denominator contract; coverage diagnostics `numeric_n` / `answered_n` /
   `product_linked_n` / `respondent_n`, with the per-type product-attribution rates (§3.6).
9. Instrument signature `σ(q) = (construct, type, scale, attribution, grain)` plus the
   cross-survey compatibility rule. `question.language` and `question.hasPiping` exist and
   are useful discriminators (neither is mentioned today).
10. Historical candidate scoring — rank on `has answers` (highest weight), `NOT isTemplate`,
    `archived_at IS NULL`, name similarity, `publishedAt`/`createdAt` proximity, `country`,
    `uniqueName`. Drop `is_container` (inert, §2.2d). A perfectly-named empty draft must
    lose to a messily-named survey with responses.
11. Benchmark: document `benchmark_registry` + the three flag columns, **including that the
    flags currently disagree** (§2.2a), and that `agents.yaml`'s fixed IDs remain
    authoritative.

**Gate:** rows **11–17**, **18–23**, **24–33** improve or hold. Accept a token increase;
Phase 3 pays it back.

### Phase 3 — Deduplicate (where the reduction comes from)

12. Execute the canonical-owner table (§5).
13. Merge §2 "Semantic domains" + §4 "Minimal table bundles" into one intent→table matrix.
14. Replace worked queries with invariants; keep the two named exceptions.
15. Collapse out-of-scope domains to the one `information_schema` fallback line.

**Gate:** rows 1–42 flat **and** char count within 21,000–23,000. A route that drops
identifies content that was load-bearing — restore that specifically rather than reverting
the phase.

### Phase 4 — Front-load the retrieval state machine

16. Add the phase procedure at the top **as reasoning phases, not mandated SQL calls** — the
    existing "prefer one query" rule and its two-query question-resolution exception must
    survive intact. Mechanical phases combine once identifiers are resolved.
17. Add the four route playbooks as sections in the same always-loaded file.

**Gate:** rows 1–42, 3 reps. Watch trap rows **34–38** and **40–42** hardest — a restructure
is likeliest to break semantic matching and absence detection.
`docs/needle_haystack_testset.md` documents what those rows test.

### Phase 5 — Physical route-specific injection (DEFERRED)

Preconditions: Phase 4 green; a route classifier measured on all 69 rows; and an escape
hatch letting the agent request another slice mid-run. Only then is the 6,000–10,000 char
per-request figure worth chasing. See §2.3 for why it is not safe today.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Cutting a rule that silently prevented a bug | Every cut maps to a canonical owner (§5); per-route gates; restore specifically, don't revert wholesale |
| Restructure breaks semantic matching | Phase 4 gates on rows 34–38 and 40–42 — the canaries |
| Rules moved into `agents.yaml` inflate the prose turn | §2.2b destination rule — SQL-only content stays in the schema block |
| Gate misses the routes most at risk | Phase 0 extends coverage from rows 1–18 to 1–42 before anything ships |
| Wall-clock noise hides a regression | 3 reps per gate; prefer token counts |
| Benchmark route rebuilt on a 1-row table with self-contradicting flags | §2.2a — document, don't switch |
| New type coverage (§3.1) is measured on this dump's data, not the schema | Types and `answerData` keys are structural, but state observed counts as *observed*, honouring §6's stability boundary |

---

## 8. Recommended order

Phase 0 → 1 → 2 → 3 → 4.

Phase 0 is not optional: gating on rows 1–18 would let a benchmark or semantic-match
regression ship unnoticed. **Phase 1 is worth landing on its own** — the line-scale pooling
bug (§3.3) is live today, sits in the example the model reads for historical comparison, and
produces a confident wrong number rather than a visible failure. The `survey_nomenclature`
country error is live today too.
