# Attribute-to-KPI Analysis as an Isolated Workflow

Goal, verified schema reference, and implementation plan. Self-contained: a new session needs
only this file.

Written 2026-08-19 from a read-only investigation against the local sample DB plus three live
agent runs. **No code was changed.** Every claim below is either a verified fact (with the query
or `file:line` that shows it) or a design decision with its reason.

Companion files in this repo:

- `drilling_workflow.md` — the isolation methodology this plan follows.
- `persona_clustering_reference.py` — the analyst-supplied K-means + overlap code that
  `cluster_attributes` / `label_persona_clusters` start from, with its three defects annotated
  inline (§13). Nothing imports it yet.
- `previous_session.md` — unrelated in-flight work and known issues.

## Scope of this work — LOCAL FIRST

**Implement in the root repo only: `funda_agent_exp.py` and the new modules beside it.**
Do **not** touch `deployment/` until the local agent is built and verified end-to-end against the
local DB. `deployment/` is a separate git repo; mirroring is Step 8 (§20) and is deliberately last.

Reading order for an implementer: **§2** (what exists and what does not) → **§20** (build order and
acceptance tests) → the design section for the step you are on. §3–§5 and §12–§13 are the evidence
behind the decisions; read them when a decision looks arbitrary.

> **Line numbers drift.** Every `file:line` in this document was correct on 2026-08-19 against the
> unmodified `funda_agent_exp.py`. After the first edit they shift. **Locate code by symbol name**
> (`_INV_SQL`, `call_tool`, `_trim_history`, `_numeric_value_map`, …); treat the line numbers as
> hints, not addresses.

This project's working style (from the user's global instructions) is smallest-correct-diff with
approval per meaningful substep. The build order in §20 is written to be executed one step at a
time, not in one pass.

---

## 1. Goal

Run a **predefined, ordered analysis pipeline** inside an **isolated workflow** whose LLM context is
completely separate from the main agent's — not even the main system prompt or the main tool
descriptions are shared. Only the final R² and correlation figures are appended to main history, as
one tool observation. The main agent draws the inferences.

The pipeline:

```
A  Survey data / responses         (all identified attributes + KPI metrics)
   |
B  Normalize                       deterministic; design-range then z-score
   |
   +-- C  Attributes ----------------------------------+
   |                                                   |
   |   distributions of normalized attributes          |
   |   (mean, sd, quantiles per attribute)             |
   |            |                                      |
   |   G  Predefined sections  <-- MODEL TURN 2         |
   |      = named axis-aligned boxes in normalized     |
   |        attribute space ("spicy lover",            |
   |        "sweet lover", ...)  + choice of K         |
   |            |                                      |
   |            |         D  K-means (K = 4 or 5) <----+
   |            |            |
   |            |         F  Anonymous clusters
   |            |            |
   |            +---> I  Maximum-overlap analysis
   |                     (fraction of each cluster's points inside each box)
   |                        |
   |                     J  Persona clusters  (cluster -> persona label)
   |                        |
   |                     K  Centroids (per persona, normalized attributes)
   |                        |
   +-- E  KPI metrics ------+   mean normalized KPI per persona
                            |
                         L  PLSR:  centroids -> mean KPI
                            +--> M  R² / Q²  (goodness of fit)
                            +--> N  Correlation + per-persona alignment sign
```

**Control flow: a bounded ReAct loop over a FIXED stage order**, not a free-form loop and not a
rigid DAG. The stage sequence is predefined; the loop exists only so a **validation failure can send
the model back one step** — an empty persona region, a many-to-one collapse, or an unattainable
quantile all require re-proposing, which a rigid DAG cannot do (§13, §14.3). Budget: **6 model steps
with a tool-free final turn**. All numerics are deterministic Python; the model never sees a data row.

**What the analysis answers:** which persona segments align **positively** with a KPI and which
align negatively — i.e. whether a product's sensory profile fits that kind of person — plus whether
the relationship is linear enough to be worth reporting.

**Division of labour.** The main agent does **selection only** — read the catalog, name the KPI
metrics and the attribute set. The pipeline owns everything else. The respondent-level matrix, the
cluster assignments, the box definitions and every deliberation stay out of main context. The main
agent receives R² / Q² / correlation per persona per KPI and reasons about *meaning*, not method.

**The workflow runs on the same model as the main agent** (`MODEL_NAME`, line 126). See §9.1.

**Fixed decisions:** do NOT extend `analyze_feature_influence`. The regression is **PLSR**, not OLS.
K-means runs on **all attributes at the row grain the data actually has** — detected, not assumed
(§11.5) — with **K chosen by the model** (4 or 5), not by an information criterion. **Q² (leave-one-out cross-validated R²) is the primary
goodness-of-relationship measure**, not R² — see §11.2 for the measurement that settles this.
Persona regions are specified in **quantile terms**, not raw, normalized or z-score units — see
§11.4.

## 2. Current state — read this before planning work

### 2.1 The isolation harness does not exist yet

`drilling_workflow.md` describes the pattern as belonging to "the reference
`funda_agent_exp.py`". **It is not in this repository.** Verified:

| Component | Status |
|---|---|
| `output_store.py` | **Complete and correct**, but **imported by nothing.** Orphaned. |
| `DRILL_SYSTEM_PROMPT`, `DrillState`, `build_drill_graph`, `run_drill`, `_run_drill_agent`, `_run_digest_workflow`, `_parked_result_id` | **Absent** from `funda_agent_exp.py`, `funda_agent_exp_old.py`, and `deployment/funda_agent_exp.py` |
| `parked_ids`, `drill_missing`, `progress_gathered`, `progress_needed` in `AgentState` | **Absent** |
| Oversized-result manifest in `nl2sql_tool` | **Absent** — it does `json.dumps(rows, default=str)` on the full row list with no size cap |
| Second model binding for an isolated graph | Only `MODEL_NAME = "gpt-5.5"` (line 126) and one `llm` (line 2387) exist. The workflow uses the **same model** (§9.1), so only a second `bind_tools()` binding is needed — not a second model. |

So this plan builds **two** things: the isolation harness (§8), and the ordered pipeline that runs
inside it (§10-§11). Budget accordingly.

### 2.2 What `output_store.py` already gives you

Complete, and it is exactly the layer this workflow needs. Do not rewrite it.

```python
store_rows(rows: list[dict], sql_query: str = "") -> str   # returns the manifest
load_rows(result_id) -> list[dict] | None
digest_result(result_id) -> str | None
discard_result(result_id) -> bool
discard_all() -> int
store_stats() -> dict
# LLM-facing tools:
describe_result(result_id)
aggregate_result(result_id, group_by, metrics, filters, sort_by, descending, limit, offset)
query_result(result_id, columns, filters, sort_by, descending, limit)
slice_result(result_id, offset, limit)
RESULT_TOOLS = [describe_result, query_result, slice_result]
DRILL_TOOLS  = [describe_result, aggregate_result, query_result, slice_result]
```

Constants: `SAMPLE_ROWS = 2`, `MAX_RETURN_ROWS = 50`, `MAX_GROUP_ROWS = 200`,
`MAX_STORED_RESULTS = 32` (env-overridable), `NUMERIC_TYPES = {"int","float","Decimal"}`.

Two properties that matter for this work:

- **Rows are stored by reference, values unconverted** — `Decimal`/`int`/`float` survive, so exact
  numeric aggregation is possible. Do not stringify before storing.
- The manifest text `RESULT STORED (too large to inline). result_id=<id>` is **load-bearing**;
  detection matches on it. Keep the wording.

### 2.3 Files

| Path | Role |
|---|---|
| `funda_agent_exp.py` | LangGraph ReAct agent, tools, all prefetch SQL |
| `output_store.py` | Result store + reader tools (orphaned; wire it up) |
| `agent_instructions.py` | System prompt blocks incl. `INVENTORY_PREAMBLE` |
| `tool_prompts.py` | Tool descriptions |
| `feature_influence.py` | Statistics module; clustered wild resampling lives here |
| `deployment/` | **Its own git repo.** Production mirror. |
| `tests/test_agent_optimizations.py` | Unit suite; holds a `SYSTEM_HASH` prompt fixture |

`agent_instructions.py` and `tool_prompts.py` are byte-identical between root and `deployment/`
and must stay so. `funda_agent_exp.py` / `api_funda_agent_exp.py` diverge intentionally.
**`_INV_SQL` and `_PERSONA_SQL` are byte-identical in both** — verified — so catalog changes ship
to production.

### 2.4 Key line numbers (root `funda_agent_exp.py` unless noted)

| Line | What |
|---|---|
| 126 | `MODEL_NAME = "gpt-5.5"`; single `llm` at 2387 |
| 136 | `INVENTORY_MAX_CHARS = int(os.getenv("INVENTORY_MAX_CHARS", "120000"))` |
| 860 | `_REQUIRES_REFERENCE` — includes `penalty` (wired but unadvertised) |
| 864 | `_MODEL_CHART_TYPES` — includes `intensity_max/_time/_auc`, `dominance_over_time`, `penalty_scatterplot` |
| 1522 | `class InfluenceVariable` — label-keyed (`component_label`) |
| 1603 | influence SQL: `aqo.question_option_id::text AS answer_component_id` — **missing the matrix COALESCE** |
| 1604 | influence SQL: `aqo."answerData" ->> 'optionAnswer' AS raw_value` — **reads only optionAnswer** |
| 1770 | `number = float(row.get("raw_value"))` in try/except → **silently drops non-numeric** |
| 2360 | `build_tools()` — the main tool roster |
| 2727 | `_INV_SQL` (deployment: 2662) |
| 2763 | `scale_points` — keeps only `jsonb_array_length(positionLabels)`, discards the anchors |
| 2807 | Section B `'sample_option_labels', to_jsonb(labels)` |
| 2814 | `FILTER (WHERE aqo."answerData" ? 'optionLabel'))[1:12]` — **root-cause bug, see §3** |
| 2818 | Section B scope: `q.id NOT IN (SELECT DISTINCT qid FROM scored)` |
| 2882 | `_PERSONA_SQL` — on-demand persona packet, regex-gated |
| 3089 | `_survey_analysis_packet_payload` — **returns `""` on five distinct conditions** |
| 3159 | the over-cap branch: `return ""` (whole inventory lost) |
| 3364 | `call_tool` — where the workflow trigger goes |
| 3607 | `call_model` — where conditional tool binding goes |
| `agent_instructions.py:1535` | `INVENTORY_PREAMBLE`; line 1536 opens with "do not re-query it" |
| `tool_prompts.py:263` | `RUN_SURVEY_STATS_DESCRIPTION` — omits `penalty` |

### 2.5 What the packet contains today

`_INV_SQL` returns one JSON packet injected into the **user turn** of message 0. `_trim_history`
pins message 0, so **the packet is re-sent every turn** — size is a per-turn token cost.

- `products` — roster + blinding numbers.
- `scored_measures_by_product` (Section A) — numeric **and** product-linked only: per-product
  n / respondents / mean / sd, `by_attribute` when `attributes_pooled > 1`, `order_differs` flip
  detection. This part works well.
- `other_answered_measures` (Section B) — everything else: prompt, type, counts, up to 12
  `sample_option_labels`.
- `benchmark_context` — configuration only.

---

### 2.6 Statistical dependencies — numpy only

Verified in `.venv`:

| package | status |
|---|---|
| `numpy` | **2.5.2** installed (`requirements.txt` pins `2.5.1`) |
| `scipy` | **not installed** |
| `scikit-learn` | **not installed** |
| `pandas` | **not installed** |
| `statsmodels` | **not installed** |
| `stepmix` (LCA) | **not installed** |

`requirements.txt` states the versions are "as installed in BE/.venv at the time this bundle was
cut, where every figure in docs/ was measured" — pinned deliberately. `feature_influence.py` is 504
lines of hand-rolled numpy (OLS, wild bootstrap, Holm adjustment, VIF, CV folds), so **numpy-only
statistics is the house style**, not an accident.

Consequences for this pipeline: k-means, LCA (EM), NIPALS PLSR, cross-validation, and the overlap
assignment must all be written in numpy. Adding scipy or scikit-learn is a **dependency decision
requiring explicit approval**, not an implementation detail.

---

## 3. The problem (verified)

**Visibility is decided by the authoring question type, not by what the measure is.**

| KPI | Survey | Authored as | In packet |
|---|---|---|---|
| "How likely would you be to purchase this product" | Tasting Survey | `vertical-rating` | mean, sd, n per product |
| "How likely would you be to recommend this product" | Herbalife | `multiple-choice` | prompt + "450 submissions", nothing else |

**Root cause.** Section B filters labels on `aqo."answerData" ? 'optionLabel'` (line 2814), but
`multiple-choice` and `multiple-open-answer` do not use that key. **1,120 questions** DB-wide
report `sample_option_labels: NULL` while their labels sit in `question_option` — 696 of them
`multiple-choice` (334 product-linked).

Product-linked categorical measures are therefore in **no** prefetch: excluded from Section A
(non-numeric), NULL in Section B, excluded from `_PERSONA_SQL` (which requires
non-product-linked).

**Scale, over 330 surveys with response data:**

- **172 (52%)** have ≥1 product-linked categorical measure with no values in the packet.
- **95 (29%)** have such measures *and zero* numeric ones — **no product KPI values at all**.
- **601** hidden product-linked categorical measures.
- **7,182 of 8,966** attribute sub-items unnamed. Matrix rows 526/526 hidden, TCATA 155/155,
  TDS 171/171.
- Scale **anchors** discarded (line 2763 keeps only the count), so polarity is unknown:
  `Aroma Intensity: Very Weak → Very Strong` (higher ≠ better) is indistinguishable from
  `Visual Freshness: Not Fresh → Extremely Fresh` (higher = better).
- `vertical-rating` range lives in `question.settings->'settingSlider'`, never read — 12 measures
  report `scale_points: null`, including all 6 Herbalife hedonics, whose liking prints as "5.95"
  with no indication it is out of 9.

**Worst observed case** — `e0ccef3c-4541-45e5-89d8-50e3b0c211af`: aroma / flavour / texture
liking, JAR sweetness, expectation, availability, channel and frequency all invisible. Only TASTE
surfaces, because it alone was a line-scale.

**Corroboration.** `question_library` (80 rows, 301 items; templates with `surveyId IS NULL`)
splits exactly along the gap: `Consumer Hedonic` and `Descriptive Analysis` are `line-scale` →
Section A works; `Consumer Diagnostics` (JAR, purchase intent, consumption frequency),
`Market Insights` (purchase behaviour, channel, brand repertoire) and `CATA Screening` are all
`multiple-choice` → invisible. There is **no** link from a survey question to a library category
(`parent_question_id` matches 0 rows; prompt matching gives 1,744 false positives on
`Shared 2026`), so it cannot be used as a classifier. It only proves the gap is systematic.

**ReAct today: capable, but discovery-blind.** Three live runs:

| Prompt | Tool calls | Outcome |
|---|---|---|
| "How did respondents describe the sweetness of each product?" | 1 | correct JAR distribution, right `question_option` join |
| "Key KPIs: purchase intent, purchase frequency, and recommendation" | 1 | correct — found the hidden MCQ, blended with Section A numerics |
| "What are all the measures and KPIs, and what do they show?" | **0** | listed all 21, then for all 8 KPIs: *"Distribution detail is available, but not included in the pre-fetched KPI means"* |

Naming a concept works; a broad request does not fire. A `NULL` label field reads as *"this
measure has no labels"*, not *"not fetched"*. **Fix the data, do not loosen "do not re-query it"**
— that headline cut 21 LLM turns to 7.

Note also: in one live run the agent wrote a correct-looking extraction join but keyed on
`aqo.question_option_id` alone, which is **wrong for matrix**. This is why extraction must live in
code, not in the prompt.

---

## 4. Verified schema reference

The most durable part of this document. Every row measured.

### 4.1 Where the answer value lives, per question type

| `typeOfQuestion` | option row present | value carried by |
|---|---|---|
| `vertical-rating` | `question_option_id` 100% | `optionAnswer`, numeric |
| `line-scale` | `question_option_id` 100% | `optionAnswer`, numeric; **option = attribute** |
| `time-intensity-slider` | `question_option_id` 100% | `optionAnswer` numeric + `t_ms` (time series) |
| `individual-balloting` | `question_option_id` 100% | `optionAnswer` numeric + `sectionCommentAnswer` |
| `multiple-choice` | `question_option_id` 100% | `optionAnswer` on only **155,863 of 163,607** rows (95%). **Read the category from the joined `question_option.label`.** |
| `multiple-open-answer` | `question_option_id` | `optionAnswer` (text); option = sub-prompt |
| `matrix` | `question_option_id` **and** `matrix_row_option_id`, both 100% | `optionLabel` (116,609 rows); `optionValue` on only 216. **Row = attribute, column = value.** |
| `ranking` | `question_option_id` 100% | `rank` + `optionLabel` (no `optionAnswer`) |
| `tcata`, `tds` | `question_option_id` 100% | **neither** `optionAnswer` nor `optionLabel` — only `action` + `t_ms` |
| `triangle-test`, `tetrad-test` | **no option row at all** (223 / 1,611 rows) | `optionAnswer` + `sampleLabel` (+ `group` for tetrad) |
| `paired-questions` | option row on 7,882 of 7,895 | `optionAnswer` + `responseType` |
| open text | n/a | **`answer.value`** (column verified) |

**Component key is always `COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)`** joined to
`question_option`. Keyed on `question_option_id` alone, a matrix reports scale points as
attributes. `NULL` for triangle/tetrad — valid, not an error.

### 4.2 `question_option` columns that matter

| Column | Population | Notes |
|---|---|---|
| `label` | — | The name. Unique within a question for **all 294** answered `line-scale` and **all 702** answered `multiple-choice` questions. Duplicates in only **11** questions DB-wide (8 `multiple-open-answer`, 3 `matrix`). |
| `order` | — | 0-based on some questions, 1-based on others, non-dense on at least one. **Does not encode direction.** |
| `analytical_value` | **100%** (38,629/38,629) | A **storage/position code, not a score.** Equals `order` on 30,319 rows; elsewhere differs only by base offset. Does **not** carry polarity. **50 corrupt rows** in matrix questions hold `666`, `888`, `999`, `1010`, `7777` (labels literally `"6".."10"`). Those options are referenced by ~2 answer rows DB-wide. |
| `optionSettings->'positionLabels'` | 7,032 rows | `{label, position}` array, 0-based. Length = scale points. First/last non-empty labels are the **anchors** = the only polarity signal. Raw blob is 45,114 chars on the largest survey — endpoints only. |
| `type` | `row` 1,430 / `column` 1,104 / `other` 488 / `custom` 133 / **blank 35,474 (92%)** | Matrix row-vs-column discriminator. **Disagrees with observed usage in 79 cases** (45 `row`-typed used as values, 34 `column`-typed used as rows). |
| `internal_name` | non-empty on 1,437 (3.7%) | Not a reliable canonical name. |
| `optionDefinition` | non-empty on 189 (0.5%) | Same. |

**Polarity proof — `analytical_value` and `order` both fail:**

```
Dislike Extremely=1 ... Like Extremely=9     (ascending = better)
Like Extremely=0   ... Dislike Extremely=8   (ascending = worse)
```

Both are 9-point hedonics in this DB. A mechanical code inverts the sign of liking on the second.

**Ordinality is not inferable from structure:**

```
What is your gender?     Female#0 | Male#1 | Prefer not to say#2 | Prefer to self-describe#3
How often do you eat ..? More than once a week#1 | Once a week#2 | ... | Less than once a month#5
```

Identical dense `order`; one nominal, one ordinal. That frequency scale's 6th category is
`I do not eat this product` — a **non-consumer**, not a lower frequency point.

### 4.3 Structural context

| Source | Fact |
|---|---|
| `question.settings->>'answer-type'` | `one` vs `multiple`. **Authoritative for multi-select.** 0 of 475 answered `one` questions were ever observed multi; 40 of 227 `multiple` questions were observed single (respondents happened to pick one) — **declared beats observed**: a CATA stays a CATA. Matrix uses `single`/`multiple`/`open_ended`/`dropdown`. |
| `question_section` | The screener axis. `productSection` boolean + `name`. Generic skeleton `Start questions` (false) / `Design questions` (true) / `End questions` (false); clients also author custom names (Herbalife: `Demographics` / `Product Ratings`). Tasting Survey `Start questions` holds 37 screener Qs. **2,043 of 2,362** answered questions resolve a `sectionId`; `product_linked` is the fallback for the other 319. |
| `question_screen` | **Useless.** `screenType` blank for all 2,362 answered questions; `name` is authoring noise across 308 values (`"1"`, `"non product loop"`). Do not include. |
| `logic_rule` | Screen-out signal: `actionType IN ('reject','end_survey')` (33 + 40 active rules) with `sourceQuestionId`. A **question-level branching property**. |
| `enrollment.enrollment_status` | Only `completed` (31,118) and `active` (7,979). **No respondent-level screened-out state.** `last_answered_question_id` + answer-row presence cannot separate "screened out" from "abandoned". |
| `question.settings->>'chartTitle'` | Short attribute label on 199 of 530 Section-A measures. Of the 74 Section-A measures with a prompt duplicated inside one survey, 0 have a chartTitle — and all 74 are placeholders (`Line Scale 1`, `Enter question text`) in test surveys. Real prompts carry the attribute **at the end** (`"...This product will... Taste great"`), so **never truncate a prompt from the tail.** |
| unanswered questions | **289 real + 150 `info`** questions sit inside surveys that DO have responses. Invisible today. |
| `question.language` | Surveys are multilingual (English, German, Spanish, Portuguese). Labels are language-specific. |

---

## 5. Measured budget

`INVENTORY_MAX_CHARS = 120000` (env-overridable, line 136). ~30k tokens, re-sent every turn.

### Encoding comparison (catalog only)

| encoding | e14528a9 | f5ab9358 | 76fba647 |
|---|---:|---:|---:|
| row-oriented, `component_id` + `order` | 128,244 | 165,862 | 107,260 |
| row-oriented, `component_id` + label | 121,721 | 149,782 | 97,320 |
| row-oriented, labels only | 86,211 | 61,342 | 42,380 |
| **columnar, full label lists, no ids** | **59,029** | **48,479** | **27,437** |

**The dominant cost is repeated JSON key names, not data.** Prompts are only 17,696 of 71,405
chars on the largest survey (25%) and 2,652 of 35,542 on another (7%). `component_id` alone costs
42,033–104,520 chars.

### Chosen shape

| survey | lean columnar Section A | columnar catalog | total | of 120,000 | today |
|---|---:|---:|---:|---:|---:|
| `e14528a9` (worst) | 33,208 | 59,029 | **92,237** | 77% | 76,423 |
| `f5ab9358` | 194 | 48,479 | 48,673 | 41% | 25,208 |
| `76fba647` | 10,814 | 27,437 | 38,251 | 32% | 33,051 |
| `6263cf71` (Herbalife) | 1,576 | 5,796 | 7,372 | 6% | 7,955 |

A complete variable dictionary costs **+21% on the worst survey**, less everywhere else, and
Herbalife actually shrinks.

Eager per-product KPI distributions measured at 27,316 / 53,731 / 16,288 / 5,560. Adding them to
the worst survey gives 119,553 — 99.6% of cap, zero headroom. **Therefore: lazy.**

Rejected for size: full `question.settings` (40,839 chars on the worst survey vs 4,272 trimmed)
and raw `optionSettings` (45,114). Trimmed only.

---

## 6. Design — catalog section in `_INV_SQL`

### 6.1 Encoding

Columnar: `{"columns":[...], "rows":[[...], ...]}`.

### 6.2 Columns

```
qid, prompt, type, section_name, product_section, product_linked,
may_screen_out, screen_out_actions, answered, respondents, submissions,
multi_select, components[], response_categories[],
scale{points, anchor_lo, anchor_hi, slider_min, slider_max},
derived_from, role_conflict, categories_omitted
```

Omit `component_id`, `order`, `analytical_value`, `screen_id`. Ids and codes are resolved inside
the workflow, where they are free.

### 6.3 Classification rules

**Configuration first, observed usage as validation.** Observed data is absent for unanswered
questions and thin for partially answered ones.

1. Primary: `typeOfQuestion` + `settings->>'answer-type'`.
2. Matrix row/column: `question_option.type`.
3. Observed usage in `answered_question_options`: **validation only**.

Roles:

- **components** (sub-items / attributes): `line-scale`, `tcata`, `tds`,
  `time-intensity-slider`, `multiple-open-answer`, `individual-balloting`, `ranking`; and
  `matrix` where `type='row'`.
- **response_categories**: `multiple-choice`, `vertical-rating`; and `matrix` where
  `type='column'`.
- Matrix keeps both lists **separate** — collapsing them confuses attributes with scale values.

Emit `derived_from: "config" | "config+observed"` and `role_conflict: true` where declared and
observed disagree (79 cases DB-wide). **Flag, never silently pick.**

Other fields:

- `multi_select` from `settings->>'answer-type'`. Decides the model family, so it must be eager.
- `may_screen_out` / `screen_out_actions` from active `logic_rule` where
  `actionType IN ('reject','end_survey')`, keyed on `sourceQuestionId`. A **question-level
  branching property** — never a claim about which respondents were screened out.
- `answered = respondents > 0`. **Include configured-but-unanswered questions** so an attribute
  nobody answered reads as "configured, no responses", not "does not exist".
- `scale.points` = `MAX(jsonb_array_length(optionSettings->'positionLabels'))`;
  `anchor_lo`/`anchor_hi` = first and last non-empty `positionLabels[].label`;
  `slider_min`/`slider_max` from `settings->'settingSlider'`. Trimmed, never raw.

### 6.4 The catalog REPLACES Section B

`other_answered_measures` is **removed**, not kept alongside. The catalog covers every question —
answered and configured-but-unanswered, product-linked and not — so retaining Section B would
duplicate every prompt and type and blow the budget measured in §5. The only fields Section B had
that the catalog must carry forward are `answers` (legacy expanded option/value-row count) and
`submissions` (distinct answer records); keep both names and their documented grain distinction,
because `INVENTORY_PREAMBLE` currently teaches the model what they mean.

`INVENTORY_PREAMBLE` (`agent_instructions.py`) must be updated in the same step, since it names
`other_answered_measures` and `sample_option_labels` explicitly. That is a prompt change — follow
the `SYSTEM_HASH` discipline in §20.

### 6.5 Lean Section A

The catalog becomes the single source of `prompt`, `type`, `scale`. Section A drops those, keeps
`qid` + aggregates, also columnar. Measured 33,208 vs 60,630 today on the worst survey. Keep
`order_differs` and the `by_attribute`-only-when-pooled rule.

---

## 7. Design — overflow and result status

### 7.1 Replace the overloaded `""`

`_survey_analysis_packet_payload` returns `""` for **five** indistinguishable conditions (its
docstring calls this intentional). Return a discriminated result:

| condition | result |
|---|---|
| DB connection or query failure | `unavailable` — retryable data-layer error |
| survey row not found | `not_found` |
| survey has no responses | `empty` — a real, reportable fact |
| over budget | `degraded` + tier reached + what was omitted |
| normal | `ok` |

### 7.2 Degradation tiers

```
drop optional distributions
-> reduce oversized category lists     (set categories_omitted: true)
-> reduce metadata for unanswered questions
-> retain qid + prompt + type          (always)
-> return explicit degraded status
```

**`categories_omitted: true` means NOT FETCHED, never ABSENT.** The workflow fetches those labels
itself in its seed step. This flag exists specifically to prevent recreating the original bug,
where a `NULL` label field read as "no labels exist". The preamble must say so.

The >15-distinct-label guard excluded 54 of 1,457 questions (3.7%) — wide CATA lists up to 48
options and open-ended sub-question batteries. Those degrade to labels-only and must be logged.

---

## 8. Design — isolation for an ordered pipeline

`drilling_workflow.md` describes two condensers behind `ENABLE_DIGEST`: a tool-driven ReAct drill,
and a **digest workflow** that runs one model turn over deterministic Python statistics. **This
pipeline follows the digest shape** — the tasks are predefined, so there is nothing for a loop to
decide.

### 8.1 Context isolation is stricter than the drill's

The drill is told not to receive the main agent's *messages*. This pipeline additionally must not
receive the main agent's **instructions**:

| must NOT reach the workflow | where it lives today |
|---|---|
| main system prompt (`build_system_prompt`, goal/backstory, schema block) | `agent_instructions.py` |
| `INVENTORY_PREAMBLE`, `PERSONA_INVENTORY_PREAMBLE` | `agent_instructions.py:1535`, `:1781` |
| main tool descriptions | `tool_prompts.py` (`NL2SQL_TOOL_DESCRIPTION`, `RUN_SURVEY_STATS_DESCRIPTION`, ...) |
| main `messages`, checkpointer, thread id | `AgentState` |
| the prefetched inventory packet | message 0 of the main thread |

The workflow gets its **own** system prompt and its **own** tool docstrings, in a dedicated module
**`isolated_workflow_instructions.py`** that must not import from `agent_instructions.py` or
`tool_prompts.py`. Mirror it into `deployment/` like the other instruction modules. Define
`PIPELINE_TOOLS` with independent descriptions; do **not** reuse `build_tools()`.

Two tests make the isolation auditable: (1) assert `isolated_workflow_instructions` has no import
edge to the main instruction modules; (2) assert no main-prompt constant appears in the workflow's
rendered messages.

The module must state explicitly that the workflow: works only on the supplied survey and variables;
cannot use the main conversation or ask the main agent anything; may use only the dedicated tools;
must never compute a statistic by counting or averaging rows itself; must distinguish ordinary R²
from pseudo-R²; must treat centroid-level results built on 4–5 points as **descriptive**; and must
return `RESULT`, `DIAGNOSTICS`, and `STILL MISSING`.

The catalog rows the pipeline needs are **re-fetched from the DB by `qid`**, not passed through from
the main agent's context. That is what makes `categories_omitted` (§7.2) recoverable and keeps the
seed truthful.

### 8.2 What is kept from the drilling methodology, and what differs

| | drill | this pipeline |
|---|---|---|
| Control flow | LLM loop, `should_continue` on `tool_calls` | **bounded loop over a fixed stage order**, retry only on validation failure |
| Step bound | `DRILL_MAX_STEPS = 4` + tool-free final turn | **6 steps + tool-free final turn** |
| Trigger | *reactive* — detects a `RESULT STORED` manifest | ***intentional*** — main agent calls the tool |
| Model turns | up to 4, unconstrained | up to 6, each bound to a named stage (§9) |
| Parked payload | the SQL rows | **every** stage artifact |
| Prompts shared | no main messages | **no main messages, prompts, or tool descriptions**; dedicated `isolated_workflow_instructions.py` (§8.1) |
| Numerics | deterministic tools only | identical |
| Isolated state | `DrillState` | `PipelineState` |
| Output contract | findings + `ALSO AVAILABLE` + `STILL MISSING` | identical |
| Return | one `ToolMessage` | identical |
| Failure | return manifest, keep rows | **stage-named** diagnostic, keep artifacts |
| Model tier | weaker/cheaper | **same as main** (§9.1) |

The four isolation mechanisms (`drilling_workflow.md` §7) are mandatory and unchanged: separate
state object, fresh invocation, one main tool observation, explicit cleanup.

### 8.3 State and graph

```python
class AttributeKPIState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    steps: int                              # bounded at 6, tool-free final turn
    stage: str                              # current stage, for diagnostics
    dataset_id: NotRequired[str]            # parked in output_store
    variable_specs: NotRequired[dict]       # validated encodings + directions
    distribution_report: NotRequired[dict]  # ECDF grid per attribute
    persona_sections: NotRequired[dict]     # quantile specs + resolved bounds
    analysis_result: NotRequired[dict]
    still_missing: NotRequired[str]
    artifacts: dict[str, str]               # stage -> result_id
    log: list[str]                          # surfaced only in a failure diagnostic
```

One node per stage in DAG order; each may short-circuit to the report node with a stage-named
failure. `artifacts` holds **result ids only**, never data.

```python
final = PIPELINE_GRAPH.invoke(
    {"messages": [seed], "stage": "S0", "artifacts": {}, "decisions": {}, "log": []},
    config={"recursion_limit": 64},
)
```

### 8.4 Durable main-side state

Add to `AgentState`: `parked_ids: set[str]`, `analysis_missing: str`, and
`persona_profiles: NotRequired[dict]`. The last is the durable copy of the workflow's persona block —
`_trim_history` drops the `ToolMessage` after `HISTORY_MAX_ROUNDS = 5` rounds (§11.7c). It holds the
workflow's *output*, never its transcript, so isolation is preserved.

### 8.5 Every stage artifact is parked

Each stage writes through `output_store.store_rows` and passes only the `result_id` forward. **No
stage output is ever serialized into a prompt.** The model turns receive deterministic *summaries*
(`describe_result`, `aggregate_result`), never rows.

`MAX_STORED_RESULTS = 32` must cover the per-run stage count plus concurrency; a run parks ~8
artifacts, so the default allows ~4 concurrent runs. Discard each artifact as soon as its consumers
have run, and all of them on success.

---

## 9. Design — the model steps

### 9.1 Model

**Same model as the main agent** — `MODEL_NAME` (line 126), the same `ChatOpenAI` configuration as
`llm` (line 2387). Do not introduce a weaker tier. This departs from `drilling_workflow.md`'s
cheaper-model suggestion because all three turns are language judgments a weaker tier gets wrong.

Make it configurable but default to the main model: read `ISOLATED_WORKFLOW_MODEL` and fall back to
`MODEL_NAME` when unset. It needs its **own** binding, `llm.bind_tools(PIPELINE_TOOLS)`, with its own
tool descriptions (§8.1). Same model by default, separate prompt, separate state.

### 9.2 Step — encoding resolution (before stage B)

**Input:** catalog rows re-fetched by `qid`, plus `describe_result` on the stage-A artifact.

**Output into `decisions`:** per variable — encoding (`numeric` / `ordinal` / `nominal` / `jar` /
`binary`), the ordered sequence for an ordinal, the JAR ideal, categories to exclude, the design
range to normalize against, and the **direction** read from the anchors.

**Rules** (§4.2): decide from **labels and anchors**, never from `question_option.order` or
`analytical_value`. Default to nominal. Report the labels each decision rests on.

**Direction is not optional here.** Verified on the reference fixture: the purchase-frequency KPI
`adf03347` runs `Once a day=1 … Never=8`, so a **higher** value means a **lower** purchase
frequency. Without a recorded direction, "positively correlated with purchase frequency" reports the
opposite of the truth. The turn must state, per variable, which end is "more".

### 9.3 Step — choose K, then specify the predefined sections (before stage D)

This is the turn that makes personas possible, and it runs **before** clustering.

**Input — a quantile grid, not mean/sd.** For each normalized attribute the tool supplies: the
list of **distinct values with their cumulative mass** (the ECDF grid), the largest tie-group share,
skew, and the pairwise correlation matrix. Mean and sd are included but are **not** the basis for
region bounds — §11.4 measures why they cannot be.

**Output into `decisions`:**

1. `K` — 4 or 5, with a one-line rationale.
2. `predefined_sections`: an ordered mapping of **persona name → quantile specification**, e.g.

```json
{"spicy_lover":  {"AROMA": [0.667, null], "SWEETNESS": [null, 0.333]},
 "sweet_lover":  {"SWEETNESS": [0.667, null], "AROMA": [null, 0.667]},
 "enthusiast":   {"AROMA": [0.667, null], "FLAVOR": [0.667, null]}}
```

   Bounds are **quantiles in [0,1]**, `null` meaning unbounded. Attributes absent from a
   specification are unbounded. The model never emits a raw or normalized number.
3. A one-line rationale per persona naming the attributes and quantile levels that define it.

It must not see cluster assignments at this point — defining regions after seeing the clusters would
make the overlap analysis circular.

### 9.4 Step — report composition (after M and N)

**Input:** the structured outputs of I, J, K, L, M, N.

```text
<per persona per KPI: R², Q², correlation, alignment sign, n, grain, encoding, direction>

ALSO AVAILABLE: <confirmed facts beside the requested answer>

STILL MISSING: <unresolved requested facts, or "nothing">
```

**Every number must come from a stage output.** It must never compute, average, round or re-derive
a figure. It must state the KPI's direction in words, not just its sign. `STILL MISSING` lands in
`analysis_missing` on the main side.

---

## 10. Design — the pipeline stages

### A — Survey data / responses

Extract respondent-level rows for every selected attribute and KPI. Type-to-value-path rules from
**§4.1** live here, in code:

- Multiple-choice: resolve the category from the joined `question_option.label`. Do **not** depend on
  `answerData->>'optionAnswer'` — absent on 5% of rows for valid answers.
- Matrix: row label = attribute, column label = value, column `analytical_value` = numeric scale.
- Time-intensity: reduce `(t_ms, optionAnswer)` per (respondent, product, component) to Imax / Tmax
  / AUC.
- Triangle / tetrad: component is `NULL`.
- Filter `NOT COALESCE(a."isSkipped", false)`; expose `enrollment_status`, never silently filter.

`analytical_value` validation — **never clamp**: out-of-range → mark invalid, exclude from numeric
encoding, report count and raw values (50 corrupt rows DB-wide hold `666`/`888`/`999`/`1010`/`7777`).

**Parks:** long-form rows keyed `(enrollment_id, product_id, qid, component_id)`.

### B — Normalize

Two steps, both recorded:

1. **Design-range scaling** to `[0,1]` using `scale.points` / `slider_min` / `slider_max` from the
   catalog. Never the observed range — `observed` is response data, not scale metadata.
2. **Per-attribute z-scoring** before distance-based clustering and before PLSR.

Step 2 is not cosmetic. Measured on the reference fixture (§12): design-range scaling alone leaves
`CRISPINESS` (a 1–5 scale) at sd **0.063** while `AFTERTASTE` (1–9) is at sd **0.155** — a ~6×
difference in squared-distance contribution, so K-means effectively ignores the low-variance
attribute. Whichever is chosen must be **stated**, because it changes the clustering.

**Parks:** wide matrix at `(enrollment_id, product_id)` grain plus column metadata (encoding,
direction, design range, normalization, n_valid, n_excluded).

### C — Attributes / E — KPI metrics

Deterministic column split of B, using the catalog's `components[]` for attributes and
`response_categories[]` + `multi_select` for KPIs (**§6.2**). This split is what the extended catalog
exists to enable — 601 product-linked categorical KPIs are invisible today (**§3**).

### D — K-means → F Clusters

Respondent × product rows, **all** attributes, `K` from turn 2. The reference implementation lives in
**`persona_clustering_reference.py`** in this repo (k-means++ init, `n_init=10`, empty-cluster repair,
best-inertia selection). It is sound and reproduces the measured cluster sizes; adopt it **with the
§13 fixes**, which are annotated inline in that file as `DEFECT-1/2/3`. **Pass an explicit `random_state`** and report it — verified reproducible with a
fixed seed, and 0.998 / 0.993 best-permutation agreement across different seeds on the fixture.

Report cluster sizes and within-cluster dispersion. Detect and report **near-constant attributes**
before clustering (`CRISPINESS` sd 0.063 on the fixture); `feature_influence.py` already drops
constant features, so there is precedent.

### G — Predefined sections (specification, then deterministic resolution)

Two parts:

1. Turn 2 emits **quantile specifications** per persona (§9.3) — never numbers.
2. A deterministic **resolver** converts them to value thresholds in the normalized domain using the
   tie-aware ECDF (§11.4), producing `dict[persona_name, (lower[], upper[])]` for stage I.

The resolver must report, per persona per bounded attribute: requested quantile, achieved cumulative
mass, and the resolved threshold. Where they diverge — an attribute with a large tie atom, e.g.
`CRISPINESS` at 95.4% single-value mass — say so rather than absorbing it.

### I — Maximum-overlap analysis → J Persona clusters

For each cluster, the fraction of its points contained in each box — inclusive, conjunctive across
all bounded dimensions. Emit the **full overlap matrix**, the assignment, and the unassigned set.
See §13 for two defects that must be fixed first.

### K — Centroids

Mean normalized attribute vector per persona, with cluster size and within-cluster dispersion.

### L / M / N — PLSR, R², correlation

See §11.

---

## 11. Design — PLSR, fit, and the per-persona readout

### 11.1 Why PLSR

Attribute batteries are collinear by construction and the centroid design is **wide**: p attributes
vs K observations. OLS is unstable or undefined there. PLSR is the standard tool for this shape and
is what external preference mapping uses. NIPALS in numpy is ~40 lines; no scipy/sklearn available
(§2.6).

### 11.2 R² at n = K is not a goodness-of-fit measure — measured

On the reference fixture (7 attributes, purchase frequency, `random_state=20260819`):

| grain | n | R² @1 comp | R² @2 | R² @K−1 | LOO Q² @1 | LOO Q² @2 |
|---|---:|---:|---:|---:|---:|---:|
| centroids, K=4 | 4 | 0.9665 | 0.9981 | **1.0000** | 0.4411 | 0.5655 |
| centroids, K=5 | 5 | 0.9447 | 0.9998 | **1.0000** | 0.8327 | 0.9014 |
| cluster × product, K=4 | 16 | 0.8855 | 0.9169 | 0.9219 | — | — |
| cluster × product, K=5 | 20 | 0.8237 | 0.9004 | 0.9084 | — | — |

**At the centroid grain R² saturates to exactly 1.0000 at K−1 components.** It measures how many
components were used, not the strength of the relationship. Reporting it as "goodness of fit" would
be actively misleading.

Therefore, mandatory:

- Cap components at `min(n − 2, p)` at the centroid grain, and **never** report R² without n, the
  grain, and the component count beside it.
- **Q² (leave-one-out cross-validated R²) is THE primary goodness-of-relationship measure.** R² is
  reported only as a diagnostic beside it, never as the answer. On the fixture Q² shows the
  relationship is real but far weaker than R² implies (0.44 at K=4, 0.83 at K=5).
- Report Q² at **both** grains where products ≥ 2, so the reader can see whether the relationship
  survives the extra observations.
- Emit the **cluster × product grain as a second fit** whenever products ≥ 2. It does not saturate
  (0.89 at 1 component, n=16) and is the defensible R² to quote.

### 11.3 The per-persona readout — this is what the question actually asks

"Which persona aligns positively with the KPI" is **not** a PLSR loading question; loadings are
per-attribute. The per-persona answer is the **PLS X-score** `t₁` for that persona, signed against
the Y-loading `q₁`:

```
alignment(persona i) = sign(t₁ᵢ · q₁)
```

Verified on the fixture, K=4 — the sign tracks mean KPI monotonically:

| persona cluster | n | t₁ | mean normalized KPI | alignment |
|---|---:|---:|---:|---|
| c0 | 455 | −0.970 | 0.297 | negative |
| c1 | 355 | −2.660 | 0.247 | negative |
| c2 | 160 | +3.041 | 0.427 | positive |
| c3 | 230 | +0.589 | 0.320 | positive |

**And the sign must be reported in words, not symbols.** On this fixture the KPI is coded
`Once a day=1 … Never=8`, so "positive alignment with the KPI variable" means **buys less often**.
c1 — the highest-liking cluster — is *negatively* aligned with the coded variable and therefore the
**most frequent** purchaser. A report that says "c2 positively correlates with purchase frequency"
is exactly backwards.

Also report per-attribute Pearson/Spearman at the fit grain with n attached, and PLSR **VIP** scores
as the attribute-importance answer.

For a non-numeric KPI, R² is undefined: report a **named** pseudo-R² and never label it R².

### 11.4 Which domain the persona regions are defined in — measured

The model's competence is **relative position** ("top third on aroma"), not absolute thresholds.
Four candidate domains were tested on the reference fixture with the same clusters (K=4) and the
same tercile-style intent:

| domain the bounds are written in | box masses | unassigned clusters | distinct personas |
|---|---|---|---|
| normalized `[0,1]`, round thresholds 0.75 / 0.50 | 0.015 / 0.060 / **0.001** | none | 2/4 |
| z-score, ±0.5 sd | 0.003 / **0.000** / 0.002 | **[0, 3]** | 2/4 |
| z-score, ±1.0 sd | **0.000** / 0.172 / 0.002 | **[1, 3]** | 3/4 |
| rank-percentile terciles (ties split) | 0.055 / 0.027 / 0.052 | none | 3/4 |
| **quantile terciles, tie-aware** | **0.266 / 0.155 / 0.226** | **none** | **3/4** |

**Z-score is unusable on this data.** The normalized attributes are strongly left-skewed
(skew −0.85 to −1.98) with large ceiling mass — `FRESHNESS` has **74%** of respondents at its
maximum, `FLAVOR` 39%. For `FLAVOR` and `FRESHNESS`, `P(x > mean + 1sd) = 0.000`: the ceiling sits
*below* mean+1sd, so a "high on flavour" region at +1 sd is **empty by construction**. A model asked
to reason in sd units will confidently emit empty regions.

**Raw or normalized fixed thresholds are distribution-blind.** `0.75` reads like "high" but 87% of
the mass lies above it; `0.50` as "low" captures 0.1% of respondents.

**Rank-percentiles split tied respondents.** The rating scales are coarse: 5–9 distinct values across
1,200 rows, with largest tie groups of 35–95% (`CRISPINESS` 95.4%, `FRESHNESS` 74.0%). At
`FRESHNESS`'s 2/3 quantile, a rank transform selects 401 rows while the value threshold selects 888 —
the 888 tied respondents are split 401 in / 487 out **by stable-sort order alone**. Two respondents
with identical answers would land in different personas.

**Therefore: quantile specifications, resolved tie-aware to value thresholds.** The resolver
computes the ECDF `P(x ≤ v)` on the distinct-value grid and picks the first value meeting the
requested quantile. Verified: this keeps every tie group whole. Because the transform is monotone
per axis, the result is an **exact axis-aligned box in the normalized domain**, so the reference
`maximum_overlap_analysis` runs unchanged.

Two required behaviours of the resolver:

- **Report achieved mass beside requested quantile.** With `CRISPINESS` at 95.4% single-value mass, a
  tercile boundary is unattainable — you cannot cut a 95% atom into thirds. Divergence between
  requested and achieved must be surfaced, not absorbed.
- **Bound few dimensions, not all.** Bounding all 7 dims at terciles gave balanced masses
  (0.27 / 0.16 / 0.23) and no unassigned clusters; bounding only 3 "defining" dims gave high overlaps
  but collapsed to **2 distinct personas of 4** because the regions overlapped each other. Report the
  distinctness count and treat a collapse as a turn-2 revision signal (§13.2).

---

### 11.5 Grain — detect the regime, never assume it

Measured DB-wide: **an enrollment usually covers exactly one product.** 4,161 enrollments span 1
product; only 565 span 2–10, across ~110 surveys. Two regimes exist and the workflow must detect
which applies from `max(products per enrollment)`:

| regime | consequence |
|---|---|
| **single-product enrollments** (common case) | respondent-level and respondent × product **coincide**; one respondent gets exactly one persona; no error clustering needed |
| **multi-product enrollments** | repeated measures: cluster SEs by `enrollment_id`, and a respondent can land in several personas — which breaks persona semantics and must be reported |

Report the detected regime, `max(products per enrollment)`, rows and respondents separately, and — in
the multi-product regime — the share of respondents assigned to more than one persona.

**Limitation:** the multi-product path **cannot be validated on this DB.** The largest
repeated-measures survey has 43 enrollments (`542f1787`); the rest have 1–18 — far too small to
cluster. Treat that path as unverified until a real multi-product survey is available.

Detect and fail on duplicate `(enrollment_id, product_id, component)` observations; never silently
average them. Respondent-level screeners may be broadcast across products only when explicitly
supplied, and must be labelled respondent-level covariates.

---

### 11.6 Individual-level validation alongside the centroid fit

Report **two** models with different purposes, clearly separated:

| level | n | model | purpose |
|---|---|---|---|
| **centroid** | K (4–5) | PLSR + Q² | the persona-level answer; R² saturates so Q² is the measure (§11.2) |
| **individual** | rows (1,200 on the fixture) | model by declared KPI encoding | validation that the centroid relationship is not an artefact of 4–5 points |

Individual-level model family follows the declared KPI encoding: numeric → linear regression with
ordinary R² and Pearson; binary → binary logistic with pseudo-R²; nominal → multinomial logistic with
pseudo-R²; ordinal → ordinal logistic with Spearman; JAR → below/just-right/above contrast;
multi-select → one binary model per option.

**Never label a pseudo-R² as R².** And label every centroid-level figure as **descriptive**, since it
rests on 4–5 points.

---

### 11.7 Persona profiles — what each persona must carry, and how it survives

The main agent must be able to answer *"what defines persona 3?"* on a later turn from what is
already in its history, without re-running anything. That imposes two requirements.

**(a) The profile must be exhaustive enough to answer unanticipated questions.**

Measured on Herbalife (4 clusters × 5 demographic questions, 450 joinable rows):

| profile form | rows | serialized |
|---|---:|---:|
| exhaustive per-persona distributions | 20 cells | **2,574 chars** |
| differential only (≥10pp deviation from base) | **1 row** | 104 chars |

Differential-only is nearly empty here — these personas barely differ demographically, which is
itself an honest finding, but it answers almost nothing. So: **emit exhaustive distributions** and add
a `distinguishing` list on top. Extrapolated worst case — the Tasting Survey's 37 screener questions
× 5 personas ≈ **24 KB**, which is why it belongs in durable state rather than re-sent prose (see (c)).

Each persona entry carries:

- `persona`, `clusters[]`, `n`, `share_of_respondents`
- `region`: the quantile spec, the resolved bounds, and the overlap score
- `centroid`: normalized **and** raw units (`AROMA 8.1 of 9`) — raw is what a human reads
- `kpi_summary`: mean or category distribution per KPI, with n
- `demographics`: full per-question distributions with counts and shares, plus base shares
- `distinguishing[]`: the facts where the persona deviates most from the survey base
- `joinability`: see (b)

**(b) Demographics are not always joinable — detect it.**

Measured DB-wide: of 82 surveys holding both product answers and non-product multiple-choice, **76
are joinable by `enrollment_id` and 6 are not**. The primary fixture is one of the 6: the Tasting
Survey's 1,200 attribute enrollments answered only `matrix` and `open-answer` questions outside the
product sections, while its 37 screener multiple-choice questions were answered by a **disjoint** set
of 1,200 enrollments — overlap **0**. Herbalife, by contrast, joins 450/450 = 100%.

There is no fallback key: `enrollment.user_id` and `panelist_id` are **null for all 2,400** enrollments
on that survey. So when the sets are disjoint the workflow must report
`joinability: "disjoint", demographics: null` with the counts — never silently omit demographics, and
never join a different set of people.

**(c) A ToolMessage does not survive — the profile needs durable state.**

`_trim_history` (`funda_agent_exp.py:3498`) keeps `msgs[0]` permanently and otherwise trims whole
rounds; `HISTORY_MAX_ROUNDS = 5` (line 132). So the `ToolMessage` carrying the persona profile is
**dropped from main history after five user rounds**, and a later "tell me about persona 3" would find
nothing.

Fix, mirroring a pattern that already exists: keep the profile in a durable `AgentState` field and
re-attach it to the user turn on demand, exactly as `survey_persona_inventory()` is re-attached when
`is_persona_request(follow_up)` fires (lines 3821, 3826). Concretely:

- `persona_profiles: NotRequired[dict]` on `AgentState` — survives trimming because it is state, not a
  message.
- A `is_persona_profile_request()` predicate on the follow-up, reusing `_PERSONA_REQUEST_RE`
  (line 2929) extended with the emitted persona names.
- On a match, append the stored profile to the current `HumanMessage`, so the evidence is visible in
  the live turn regardless of how old the analysis is.

This keeps the workflow's isolation intact — the profile is still the workflow's *output*, not its
transcript — while making it durable.

---

### 11.8 The three questions the output must answer

The workflow's output is designed against three named queries. Each maps to a specific mechanism, and
one of the intended mappings does not survive measurement.

| user question | answered from | mechanism |
|---|---|---|
| *"What are the personas of this survey?"* | `persona_profiles[]` roster | persona name, n, share, region spec, centroid in raw units, one-line rationale |
| *"Give more details on this particular persona"* | that persona's full profile (§11.7) | exhaustive demographics + KPI summaries + region bounds + overlap, re-injected from durable state |
| *"Which persona is a better fit for this product?"* | the persona × KPI × product relationship table | **"better" → correlation**; **"fit" → Q², but see below** |

**"better" → per-persona correlation works.** Computed within each persona across products
(n = number of products), correlating the persona's first PLS score against its mean KPI. Measured on
the fixture (4 products): c0 **+0.704**, c1 **+0.713**, c2 **+0.897**, c3 **+0.954**. Sensible
magnitudes, all reportable — with n=4 attached and labelled descriptive.

**"fit" → a per-persona Q² does NOT work, measured.** With 4 products and 7 attributes, leave-one-out
Q² inside a persona trains on 3 points:

| persona | n | in-sample R² | **LOO Q²** |
|---|---:|---:|---:|
| c0 | 4 | 0.496 | **−10.150** |
| c1 | 4 | 0.509 | **−2.832** |
| c2 | 4 | 0.804 | **−0.466** |
| c3 | 4 | 0.911 | **−0.091** |

Every per-persona Q² is **negative** — the model predicts worse than the persona's own mean, i.e. no
predictive validity whatsoever. Ranking personas on that number would be ranking noise.

**So "fit" resolves to two figures instead:**

1. **Q² at the model level** — one number per KPI, the honest goodness-of-relationship measure
   (§11.2). It says whether the attribute→KPI relationship holds at all, not which persona fits best.
2. **Per-persona prediction error from the global model** — the per-persona question. Measured at the
   cluster × product grain (global n=16, R² 0.885): mean |actual − predicted| = c0 **0.0241**,
   c1 **0.0116**, c2 **0.0236**, c3 **0.0215**. Small and comparable, so the global relationship
   explains every persona about equally well here — which is itself the answer to "which fits best".

`per_persona_q2` must be emitted as `null` with `reason: "n_products too small (4); needs >= 6"`,
never computed silently. Compute it only when a persona spans ≥ 6 products.

**Ranking for "which persona is better/best for this product"** therefore combines, per (persona,
product): mean KPI in the KPI's own direction, alignment sign from `sign(t₁·q₁)`, per-persona
correlation with n, and prediction error. The report must name the KPI direction in words — on the
fixture a *lower* code means buying more often (§11.3).

---

### 11.9 Trigger — a persona question activates the workflow

A persona-shaped request must run the **full flow** (§10 A→N plus profiling), not be answered from the
prefetched packet. The packet contains no cluster structure, so answering from it would fabricate
personas.

There is a name collision to resolve. `is_persona_request()` / `_PERSONA_REQUEST_RE`
(`funda_agent_exp.py:2929`) already matches `personas?`, `demographic profile|breakdown|distribution`,
`respondent motivations|loyalty|behaviors|attitudes`, and today it attaches the cheap
`_PERSONA_SQL` demographic inventory. Under this design the same word must route two different ways:

| request shape | route | cost |
|---|---|---|
| survey-level demographics — *"what are this survey's demographics?"* | existing `_PERSONA_SQL` inventory | one query |
| persona **clusters** — *"what are the personas?"*, *"which persona fits this product?"*, *"details on persona X"* | the isolated workflow, full flow | clustering + PLSR + profiling |
| follow-up naming an **already-computed** persona | durable `AgentState.persona_profiles` re-injection (§11.7c) | free |

So split the predicate: keep `is_persona_request()` for the demographic inventory, add
`is_persona_cluster_request()` for the workflow, and check the durable-state path **first** so a
follow-up never re-runs the pipeline. Changing `_PERSONA_REQUEST_RE` touches prompt-adjacent
behaviour covered by the `SYSTEM_HASH` fixture — follow the discipline in §20 before editing it.

---

## 12. Validated on real data

Everything in §11 and §13 was run end-to-end against the local DB using the supplied
`kmeans_clustering` / `maximum_overlap_analysis` code, on Tasting Survey
`e14528a9-01ac-4dff-844e-0914dbdb3759`: 7 attributes × purchase frequency, **1,200 complete rows**,
zero partial.

**Correction to an earlier reading of this fixture.** Those 1,200 rows are **1,200 distinct
enrollments, each covering exactly one product** — not 300 respondents × 4 products. Verified:
`products per enrollment = 1` for all 1,200, and one row per enrollment per attribute. The survey has
2,400 enrollments and 4 products. So this fixture has **no repeated-measures structure**, and every
respondent received exactly one persona at K=4 and K=5 (0% multi-assignment) — a property of the
design, not evidence about repeated measures. See §11.5.

**Design ranges differ within one battery** — five attributes are 1–9, two are 1–5. Raw sds range
0.251 (`CRISPINESS`) to 1.237 (`AFTERTASTE`), a ~5× disparity from scale choice alone. This is the
concrete proof that design-range normalization is required before distance-based clustering.

**Normalized attributes are ceiling-compressed:** means 0.81–0.92, `q3 = 1.000` on four of seven.
`CRISPINESS` is near-constant (mean 0.503, sd 0.063, q1 = median = q3 = 0.500).

**The clusters separate by level, not by type.** K=4 centroids: c1 high on everything (0.95–0.99),
c2 low on everything (0.59–0.72), c0/c3 between. Within-respondent centering did **not** expose
profile structure either — 3 of 4 centred clusters share the same high/low pattern.

> **Consequence for the persona premise.** On this survey there is no "spicy lover vs sweet lover"
> structure to find; the dominant variance is a general liking factor. The method is sound, but the
> pipeline must **report the level-vs-shape decomposition** and say plainly when clusters differ only
> in degree — otherwise turn 2 will invent type-based persona names the data does not support.

**Overlap analysis works when boxes are level-based.** Terciles bounded on all 7 dimensions gave
usable overlaps (0.83, 0.86, 0.41) — my earlier concern that conjunctive containment would collapse
to zero in 7 dimensions was **wrong**, because the clusters are level-ordered. But **two of four
clusters mapped to the same persona** (`all_low`), and a subset-bounded box set produced only 3
distinct personas for 4 clusters.

---

## 13. Defects in the reference implementation

Three, all reproduced. Fix before wiring stage I.

**1. All-zero overlap still returns a full assignment (silent misassignment).** `np.argmax` on an
all-zero row returns index 0, so every cluster is assigned the *first* persona. Reproduced with two
deliberately empty boxes: overlap matrix all zeros, and the returned
`cluster_to_persona` was `{0: 'p_first', 1: 'p_first', 2: 'p_first', 3: 'p_first'}`.
**Fix:** if `overlap_matrix[row].max() == 0`, mark the cluster `unassigned` and report it. An empty
box is a turn-2 error — a threshold above the observed maximum — and must surface as one.

**2. No injectivity, and it fires on real data.** Each row's `argmax` is independent, so several
clusters can collapse onto one persona. Observed: with tercile boxes at K=4, clusters 2 and 3 both
mapped to `all_low`; with subset boxes, only 3 distinct personas covered 4 clusters. **Fix:** either
allow many-to-one **explicitly and report it**, or run a one-to-one assignment maximizing total
overlap. Do not let it happen silently — two clusters sharing a persona label makes the PLSR readout
uninterpretable.

**3. `k-means++` "already chosen" test is float-equality and allocates an n×k×d array.**
`np.any(np.all(X[:, None, :] == centroids[None, :ci, :], axis=2), axis=1)` materializes
`n × ci × d`. At n = 1,200 × d = 7 this is fine, but it grows with the respondent count, and
float `==` is a fragile way to ask "is this point already a centroid". **Fix:** track chosen indices
in a set. Not a correctness bug at current scale — flagged so it is not discovered at 50k rows.

Otherwise the clustering code is sound: `n_init=10` with best-inertia selection, empty-cluster repair
in both the inner loop and after convergence, and `stable` argsort. Verified reproducible with a
fixed `random_state`, and 0.998 / 0.993 best-permutation agreement across seeds at K=4 / K=5.
**`random_state=None` must not be used** — pass and report a seed.

---

## 14. Open decisions

**1. Normalization for *clustering* — design-range only, or design-range then z-score?** Note this
is a separate question from the domain the persona regions are written in, which §11.4 settles as
quantile specifications either way. Measured consequence (§12): design-range only leaves
`CRISPINESS` at sd 0.063 vs `AFTERTASTE` at 0.155, so K-means effectively ignores it. Z-scoring
equalizes contribution but promotes a near-constant attribute — 95.4% of respondents on one value —
to equal weight. **Recommendation:** z-score for the distance metric, **and** drop attributes below a
stated variance floor, reporting the drop. On this fixture that floor removes `CRISPINESS`.

**2. PLSR grain for the reported R².** Centroids alone saturate to R² = 1.0000 (§11.2).
**Recommendation:** report **Q² at the centroid grain** as the headline, plus **R² at the
cluster × product grain** (n = K × products, 16–20 on the fixture) as the quotable fit. Quote a bare
centroid R² nowhere.

**3. Many-to-one persona assignment: permitted or rejected?** See §13.2. **Recommendation:** permit,
but report it prominently and refuse to emit a per-persona alignment for a persona holding more than
one cluster until they are merged or the boxes revised.

**4. Does the survey have profile structure at all?** §12 shows this fixture does not.
**Recommendation:** add a deterministic level-vs-shape check before turn 2 — the share of total
variance on the first principal component of the normalized attributes — and require turn 2 to use
level-based persona names when that share is dominant.

---
## 15. Design — the main-agent-facing tool

The main agent does **selection**. Everything else belongs to the pipeline, so every methodology
field is an **optional override**.

```python
class ModelVariable(BaseModel):
    question_id: str
    component_label: str | None = None      # pipeline resolves the id internally
    role: Literal["attribute", "kpi", "screener"]
    # --- optional overrides; omit and the pipeline decides from the labels ---
    encoding: Literal["auto", "numeric", "ordinal", "nominal", "jar", "binary"] = "auto"
    ordered_categories: list[str] | None = None
    jar_ideal: str | None = None
    exclude_categories: list[str] | None = None
```

```python
attribute_kpi_analysis(
    survey_id: str,
    attributes: list[ModelVariable],
    kpis: list[ModelVariable],
    k: int | None = None,                 # None -> workflow evaluates K=4 and K=5, picks, reports why
    persona_sections: dict | None = None, # optional: pin quantile specs for cross-survey comparability
    requested_metrics: list[str] | None = None,
    normalization: Literal["design_range", "design_range_zscore"] = "design_range_zscore",
    random_state: int = 20260819,         # never None; echoed in the output
)
```

`kpis` is a list because the analyst usually wants several KPIs against one persona structure — the
clustering runs once and each KPI gets its own fit, Q², correlation and per-persona alignment.
`persona_sections` lets the analyst pin the quantile specs so personas stay comparable across
surveys or waves; when supplied the workflow **validates** rather than proposes them. Default
`requested_metrics`: cluster assignments, persona centroids, KPI means, fit, Q²/R², correlation.
Screeners are never auto-included — they must be named with `role="screener"`.

The main agent must resolve variables from the catalog **before** calling. An ambiguous label must
prevent the call and produce a clarification response, not a guessed component.

**`component_label`, not `component_id`.** Labels are unique within every answered `line-scale`
(0/294) and every answered `multiple-choice` (0/702) question. For the 11 duplicate-label questions
the pipeline **fails explicitly and returns the matching component ids**. This keeps ids out of a
prefetch where they cost 42,033–104,520 chars (§5).

**`encoding="auto"` means *the pipeline reads the labels and decides* — NOT structural inference.**
An earlier draft rejected `auto`; that rejection stands for the **structural** shortcut, since
ordinality is not derivable from `order` or `analytical_value` (§4.2). What changed is *who decides*:
turn 1 runs on the full-strength main model with complete ordered category lists and anchors in front
of it. A caller-supplied ordering always wins, and both are echoed in `encoding_decisions`.

The description in `tool_prompts.py` must state that question ids come from the catalog or a prior
structural result, **never from memory** — and that encoding, K, boxes, normalization and component
count are the tool's job, so the main agent does not reason about them in its own turn.

---

## 16. Design — the dedicated workflow tools

Only these are bound inside the workflow (§8.1). Every number in the final report must come from one
of them.

| tool | responsibility |
|---|---|
| `inspect_attribute_kpi_variables` | validate ownership/survey scope, question type, component label, product linkage, section metadata, role compatibility, response categories and scale, grain regime (§11.5), missingness, screen-out metadata. A gate: nothing proceeds on an invalid spec. |
| `extract_attribute_kpi_dataset` | typed extraction per §10 A; parks the dataset and returns only a `DATASET STORED / dataset_id=… / grain=… / rows=… / respondents=… / products=… / columns=…` manifest, mirroring the drilling manifest form |
| `summarize_attribute_distribution` | deterministic per-attribute valid N, missing/excluded N, mean, variance, sd, min/max, **ECDF quantile grid**, tie-group shares, skew, normalization parameters, direction/encoding metadata |
| `propose_or_validate_persona_sections` | validates quantile specs — dimension count, bound ordering, non-empty after resolution, pairwise section overlap, coverage, requested-vs-achieved mass — and resolves them tie-aware (§10 G). Returns structured bounds + labels; the final result echoes the exact bounds used |
| `cluster_attributes` | deterministic K-means: fixed seed, configurable `n_init`/`max_iter`/`tol`, empty-cluster repair, sizes, inertia, silhouette where valid, **K=4 vs K=5 comparison when `k` is omitted**, deterministic cluster ordering in the output |
| `label_persona_clusters` | maximum-overlap mapping with the §13 fixes: **minimum overlap threshold**, tie handling, `unclassified` fallback, cluster/persona coverage, full overlap matrix. Anonymous K-means indices are never used as persona names |
| `fit_persona_kpi_model` | centroids per persona, KPI mean or category distribution per persona, centroid-level PLSR + Q², **and individual-level validation at row grain** (§11.6) |
| `profile_personas` | per-persona size and share, centroid in normalized **and raw** units, KPI summaries, **full screener/demographic distributions with base shares**, a `distinguishing` list, and a `joinability` verdict. Deterministic; detects the disjoint-enrollment case (§11.7b) |

**Unsupported, with an explicit response rather than a silent skip:** open-text questions;
`triangle-test` / `tetrad-test` (no option row exists — §4.1); `time-intensity` reduction unless
explicitly requested.

---

## 17. Output contract

```json
{
  "pipeline": "A->B->C/E->G->D->F->I->J->K->L->M/N",
  "random_state": 20260819,
  "normalization": {"method": "design_range_zscore", "per_attribute": []},
  "attributes": [{"label": "AROMA", "design_range": [1, 9], "mean": 0.830, "sd": 0.152,
                  "near_constant": false, "direction": "higher = more liked"}],
  "level_vs_shape": {"pc1_variance_share": 0.68, "verdict": "level-dominated"},
  "k": 4, "k_rationale": "...",
  "predefined_sections": [{"persona": "top_third", "spec": {"AROMA": [0.667, null]},
                           "resolved": {"AROMA": [0.875, null]},
                           "requested_vs_achieved_mass": {"AROMA": [0.333, 0.263]},
                           "rationale": "..."}],
  "clusters": [{"cluster": 0, "n": 455, "dispersion": 0.11}],
  "overlap": {"matrix": [], "assignment": {}, "unassigned": [], "many_to_one": []},
  "personas": [{"persona": "all_low", "clusters": [2, 3], "n": 390, "centroid": []}],
  "targets": [{
     "kpi": "PURCHASE_FREQ",
     "encoding": "ordinal", "direction": "higher code = LESS often",
     "excluded_categories": [{"label": "Never", "n": 13}],
     "centroid_grain":        {"n": 4,  "n_components": 1, "r2": 0.9665, "q2": 0.4411},
     "cluster_product_grain": {"n": 16, "n_components": 1, "r2": 0.8855},
     "primary_measure": "q2",
     "q2": {"centroid_grain": 0.4411, "cluster_product_grain": null},
     "model_level_q2": 0.4411,
     "per_persona": [{"persona": "all_low", "t1": 3.041, "q1": 0.031,
                      "alignment": "positive", "in_words": "buys less often",
                      "mean_kpi_normalized": 0.427,
                      "correlation_across_products": {"r": 0.897, "n_products": 4,
                                                      "basis": "t1 vs mean KPI", "descriptive": true},
                      "prediction_error": {"mean_abs": 0.0236, "grain": "cluster_product"},
                      "per_persona_q2": null,
                      "per_persona_q2_reason": "n_products too small (4); needs >= 6",
                      "by_product": [{"product": "Doritos Nacho Cheese 9.25 oz",
                                      "mean_kpi_normalized": 0.41, "predicted": 0.43,
                                      "rank_for_product": 4}]}],
     "correlations": [{"attribute": "AROMA", "pearson": -0.61, "spearman": -0.58, "n": 16}],
     "vip": []
  }],
  "persona_profiles": [{
     "persona": "all_low", "clusters": [2, 3], "n": 390, "share": 0.325,
     "region": {"spec": {"AROMA": [null, 0.333]}, "resolved": {"AROMA": [null, 0.75]},
                "overlap": 0.856},
     "centroid": {"normalized": {"AROMA": 0.59}, "raw": {"AROMA": "5.7 of 9"}},
     "kpi_summary": [{"kpi": "PURCHASE_FREQ", "mean_normalized": 0.427,
                      "distribution": {"Once a week": 41}, "n": 390}],
     "joinability": "joined",
     "demographics": [{"question": "What is your age group?",
                       "distribution": {"75+": 36, "65-74": 22},
                       "shares": {"75+": 0.44}, "base_shares": {"75+": 0.30}, "n": 81}],
     "distinguishing": [{"question": "What is your age group?", "label": "75+",
                         "share": 0.44, "base": 0.30, "delta": 0.14}]
  }],
  "analysis_n": 1200, "excluded_n": 13,
  "unique_respondents": 300, "unique_products": 4,
  "encoding_decisions": [], "invalid_numeric_values": [], "warnings": []
}
```

Hard requirements:

- **Never a bare R².** Every R² carries n, grain and component count. The headline is Q² at the
  centroid grain (§11.2).
- **Alignment in words**, derived from the recorded KPI direction (§9.2). `"positive"` alone is a
  trap on a reverse-coded scale.
- Report the **full** overlap matrix, the unassigned set, and any many-to-one collapse (§13.2).
- Report `level_vs_shape` so the reader knows whether type-based persona names are supported (§12).
- Report `near_constant` per attribute and any variance-floor drops (§14.1).
- Report `random_state`; it is never `None`.
- `encoding_decisions` carries, per variable: encoding, **derived** or **supplied**, the labels the
  decision rests on, the ordinal sequence, the JAR ideal, and the direction.
- Every excluded, unmapped or invalid value listed with its row count. Nothing dropped silently.
- Correlations and loadings are **exploratory, not causal**.
- **Persona profiles are exhaustive, not differential** (§11.7a) — a later follow-up must be
  answerable from them alone. Include `joinability` even when it is `"disjoint"`.
- The profile is also written to `AgentState.persona_profiles`, because the `ToolMessage` is trimmed
  after `HISTORY_MAX_ROUNDS = 5` rounds (§11.7c).
- **`per_persona_q2` is `null` with a reason unless the persona spans ≥ 6 products** — measured
  negative for every persona at 4 products (§11.8).
- The output must be sufficient to answer all three named queries in §11.8 **without re-running**.

---

## 18. Failure and cleanup rules

- Any stage exception → short-circuit to the report node with a **stage-named** diagnostic
  (`"stage D (kmeans, K=4) failed: ..."`), keep every parked artifact, surface `log`.
- Stage A returns 0 usable rows → name the variable and filter that emptied it; never substitute.
- A box captures 0% of every cluster → report `unassigned`, never assign persona[0] (§13.1).
- Two or more clusters collapse onto one persona → report and withhold that persona's alignment
  until resolved (§14.3).
- K-means cannot form K clusters → report the reason and the attempted K.
- PLSR cannot fit (n < 3, singular after deflation) → report n, p and grain.
- Model turn returns empty text → diagnostic naming the turn.
- Unknown tool → error message back into the pipeline, never a crash.
- Success → discard **all** parked artifacts, clear `parked_ids`, return the report.
- Shutdown → `output_store.discard_all()`.

---

## 19. Explicitly rejected, with reasons

| Option | Why not |
|---|---|
| Extend `analyze_feature_influence` | User decision. Its fetch reads only `optionAnswer` (1604), keys components without the matrix COALESCE (1603), silently drops non-numerics (1770). |
| OLS instead of PLSR | Attributes are collinear; the centroid design is wide (p > n). |
| A ReAct loop | Tasks are predefined and ordered. Digest shape, three fixed turns (§8.2). |
| Sharing the main system prompt or `tool_prompts.py` strings with the workflow | Explicit requirement: the workflow's LLM context is fully separate (§8.1). |
| Running the pipeline in the main graph | Every stage artifact and deliberation would enter main history. |
| Size-detection trigger | The analysis is requested, not stumbled into (§8.2). |
| A weaker model for the workflow | All three turns are language judgments (§9.1). |
| Main agent declaring encoding, K, or boxes in its own turn | Puts methodology reasoning into main history. Overrides remain possible (§15). |
| `encoding="auto"` as **structural** inference from `order`/`analytical_value` | Still rejected. Not derivable from structure (§4.2). |
| Choosing K by BIC/silhouette | Explicit requirement: K ∈ {4, 5}, chosen by the model with a stated rationale (§9.3). |
| Defining the boxes after seeing cluster assignments | Circular — the overlap analysis would validate itself (§9.3). |
| **Quoting R² as the goodness-of-relationship answer** | Q² is primary; centroid-grain R² saturates to exactly 1.0000 at K−1 components. Measured (§11.2). |
| Persona regions in **z-score** units | Left-skew −0.85…−1.98 with up to 74% ceiling mass; `P(x > mean+1sd) = 0.000` for two attributes, so "high" regions are empty by construction. Measured (§11.4). |
| Persona regions as fixed **raw or normalized** thresholds | Distribution-blind: 0.75 leaves 87% of mass above it, 0.50 captures 0.1%. Measured (§11.4). |
| **Rank-based** percentiles for region bounds | Splits tied respondents by sort order — 888 tied `FRESHNESS` rows split 401/487. Use tie-aware ECDF (§11.4). |
| Letting the section step emit numeric bounds at all | It emits quantile specifications; a deterministic resolver produces numbers (§10 G). |
| Transforming cluster points back to raw units to test containment | Unnecessary: a quantile spec selects identical points in raw, design-range and z-score domains — verified on four specs (§11.4). Raw units appear only in the report. |
| OLS at the centroid level | Undefined when p ≥ K (7 attributes vs 4–5 personas). PLSR there; the per-encoding regression table applies at the **individual** level (§11.6). |
| Assuming a respondent × product grain | Measured: 4,161 enrollments cover 1 product, 565 cover 2–10. Detect the regime (§11.5). |
| Reusing the main agent's instruction modules | A dedicated `isolated_workflow_instructions.py` with no import edge to `agent_instructions.py` / `tool_prompts.py`, asserted by test (§8.1). |
| A rigid DAG with no retry | An empty region, a many-to-one collapse or an unattainable quantile all need the section step re-run. Bounded loop, 6 steps (§1, §8.2). |
| Relying on the `ToolMessage` alone to keep persona profiles queryable | `_trim_history` drops it after `HISTORY_MAX_ROUNDS = 5` rounds. Mirror the `survey_persona_inventory` re-injection pattern with durable state (§11.7c). |
| Differential-only persona profiles | Measured 1 row / 104 chars on Herbalife — answers almost nothing. Exhaustive distributions cost 2,574 chars there (§11.7a). |
| **Per-persona Q²** at typical product counts | Measured **negative for all four personas** (−10.15, −2.83, −0.47, −0.09) at 4 products / 7 attributes — worse than predicting the mean. Emit `null` + reason unless ≥ 6 products (§11.8). |
| Ranking personas by "fit" using a single model-level Q² | Q² is one number for the model, not per persona. Rank on per-persona prediction error, correlation and alignment instead (§11.8). |
| Answering a persona question from the prefetched packet | The packet holds no cluster structure; answering from it fabricates personas. Persona-cluster requests must run the full flow (§11.9). |
| Reusing `is_persona_request()` unchanged as the workflow trigger | It already routes survey-level demographics to the cheap `_PERSONA_SQL` inventory. Split the predicate (§11.9). |
| Assuming demographics join to personas | 6 of 82 surveys have **disjoint** screener and product enrollment sets, including the primary fixture (overlap 0, and `user_id`/`panelist_id` null). Detect and report (§11.7b). |
| Reporting per-persona alignment from PLSR **loadings** | Loadings are per-attribute. The per-persona quantity is the X-score `t₁` signed by `q₁` (§11.3). |
| Reporting an alignment sign without the KPI's direction | On the fixture, "positive" means *buys less often*. Backwards without direction (§11.3). |
| Clamping corrupt `analytical_value` | Converts bad data into plausible data. Reject and report. |
| Normalizing against the **observed** range | `observed` is response data, not scale metadata. |
| `random_state=None` in K-means | Non-reproducible. Pass and echo a seed (§13). |
| Adding scipy / sklearn / statsmodels | Not installed; `numpy==2.5.1` pinned; `feature_influence.py` is 504 lines of hand-rolled numpy. Needs approval (§2.6). |
| Prefetching `component_id` | 42,033–104,520 chars (§5). |
| Eager per-product distributions | 119,553 of 120,000 on the worst survey. |
| Raw `question.settings` / `optionSettings` | 40,839 + 45,114 chars; ~90% display config. |
| `question_screen` / `screen_id` | `screenType` blank on all 2,362 answered questions. |
| `question_library` as a KPI classifier | No link to templates; 1,744 false positives. |
| Loosening `INVENTORY_PREAMBLE`'s "do not re-query it" | Bought the 21→7 turn reduction. |
| Truncating prompts from the tail | The attribute name is at the end. |

---

## 20. Build order and acceptance tests

Resolve §14 first — decisions 1 and 2 change the reported numbers.

**Step 1 — catalog + lean Section A + discriminated result + overflow tiers (§6, §7).**
*Accept when:* worst-survey payload ≤ 100,000 chars; Herbalife's 8 previously-invisible KPIs carry
their `response_categories`; matrix `components` and `response_categories` are separate lists; no
survey in §20 returns `unavailable`; the 289 configured-but-unanswered questions appear with
`answered: false`.

**Step 2 — isolation harness (§8).** Import `output_store.py` (imported by nothing today); add
`parked_ids` and `analysis_missing` to `AgentState`; add `PipelineState`, the DAG skeleton with stub
stages, the deterministic seed builder, the `call_tool` trigger, conditional `RESULT_TOOLS` binding.
*Accept when:* a stub pipeline returning fixed text produces exactly four main messages
(Human / AI tool_call / ToolMessage / AI); **no main-prompt or `tool_prompts.py` string appears in
the pipeline's rendered messages** (assert on the constants, §8.1); an exception in any stage returns
a stage-named diagnostic with every artifact still loadable via `load_rows`; `parked_ids` empty after
success.

**Step 3 — stages A and B (§10).**
*Accept when:* the fixture extracts **1,200 complete** rows (1,200 single-product enrollments) with 0 partials; the grain regime is detected and reported as single-product;
the 1–9 and 1–5 attributes normalize onto a common range using **design** endpoints; `CRISPINESS` is
flagged `near_constant` (sd 0.063); the 50 corrupt `analytical_value` rows are reported invalid, not
clamped; no matrix appears in any transcript.

**Step 4 — the encoding step (§9.2)**, run with `encoding="auto"` throughout.
*Accept when:* `adf03347` is `ordinal` with the sequence stated **and** direction recorded as
"higher code = less often"; `Never` is excluded with n=13 reported; `Female | Male | Prefer not to
say` stays **nominal** despite a dense order; a reversed hedonic yields the sign the **labels** imply,
not `analytical_value`'s; every decision names the labels it read.

**Step 5 — the section step + stages G, D, F, I, J, K.**
*Accept when:* two runs at the same seed give byte-identical assignments; K ∈ {4,5} with a rationale;
turn 2 emits **quantile specifications only** — a numeric bound in its output is a hard error; the
resolver keeps every tie group whole and reports requested-vs-achieved mass (`CRISPINESS` must show
divergence); tercile specs on all 7 dims reproduce masses ≈ 0.266 / 0.155 / 0.226 and overlap
≈ [0.409, 0.825, 0.856, 0.396]; an empty region yields `unassigned`, **not** persona[0] (§13.1); a
many-to-one collapse is reported (§13.2); `level_vs_shape` is emitted and turn 2 uses level-based
names when PC1 dominates; every centroid carries size and dispersion.

**Step 6 — stages L, M, N (§11).**
*Accept when:* centroid-grain components are capped at `min(n−2, p)`; **R² is never emitted without n,
grain and component count**; **Q² is the primary reported measure** and R² is labelled a diagnostic; the cluster × product fit is
emitted whenever products ≥ 2; per-persona alignment comes from `sign(t₁·q₁)` and is rendered in
words using the recorded direction; a non-numeric KPI reports a **named** pseudo-R².
*Regression fixture:* the fixture must reproduce §11.2's table — centroid K=4 R² 0.9665 @1 comp,
1.0000 @3, Q² 0.4411 @1; cluster × product K=4 R² 0.8855 @1 — and §11.3's sign pattern
(c1 negative / c2 positive).

**Step 7 — persona profiling (§11.7), the three queries (§11.8), the trigger (§11.9), and the report step.**
*Accept when:* every persona carries size, share, region spec + resolved bounds + overlap, centroid in
normalized **and raw** units, KPI summaries, and exhaustive demographic distributions with base shares;
on Herbalife the profile joins 450/450 and reproduces `age 75+ = 44% vs base 30%` for its smallest
cluster; on the Tasting Survey `joinability` is **disjoint** with `demographics: null` and both
enrollment counts, and demographics are **not** fabricated from the other 1,200 enrollments; the
profile is written to `AgentState.persona_profiles`.

*And, for the three named queries:* "what are the personas" is answerable from the roster alone;
"details on persona X" is answerable after **6+ rounds**, proving re-injection; "which persona is a
better fit for this product" returns per-persona correlation with `n_products`, alignment in words,
per-persona prediction error, and `per_persona_q2: null` with its reason — reproducing correlations
≈ +0.70 / +0.71 / +0.90 / +0.95 and prediction errors ≈ 0.024 / 0.012 / 0.024 / 0.022.

*And, for the trigger:* a persona-cluster request runs the full flow; a survey-level demographic
request still uses the cheap `_PERSONA_SQL` inventory; a follow-up naming an existing persona hits
durable state and does **not** re-run the pipeline.

**Step 8 — mirror into `deployment/`. DO NOT START until Steps 1–7 pass locally.** `isolated_workflow_instructions.py` and the workflow module
must land in the deployment repo alongside the existing instruction modules, and a parity test must
cover them the way `agent_instructions.py` / `tool_prompts.py` are covered today.

**Additional cases to test** (from the isolation and statistical failure modes above): main
checkpointer/thread never reused; numeric attribute × numeric KPI; numeric × JAR; numeric ×
categorical purchase-frequency; multi-attribute; respondent-level screener with product-level
attributes; missing and sentinel values; duplicate observations; zero-variance attributes; K=4 vs K=5
selection; empty or ambiguous sections; no-overlap mapping; tied overlaps; unclassified clusters;
insufficient cluster count; convergence failure; dataset cleanup after success; dataset **preserved**
after failure; step-budget termination; tool-free final turn; exact `STILL MISSING` propagation;
deployment/local instruction parity.

**Before any prompt change:** update `SYSTEM_HASH` in `tests/test_agent_optimizations.py`, then run
the full unit suite, `py_compile`, `git diff --check`, and the root/`deployment` parity check on
`agent_instructions.py` and `tool_prompts.py`. Do not run the regression workbook playbook unless
explicitly requested.

**Test isolation separately from statistics**, per `drilling_workflow.md` §10.

**Separate small fix, unrelated:** `penalty` is wired (`funda_agent_exp.py:860`,
`penalty_scatterplot`) but omitted from the test list in `tool_prompts.py:263`. Charts API behaviour
for `penalty`/`chi-square` on categorical questions is **unverified** — `CHARTS_API_BASE_URL` points
at production while the local DB is a sample copy.

---
## 21. Reproduction

### Environment

```bash
docker start gpi-db && sleep 3     # postgres localhost:5433/gpi_sample_db, user gpi
# agent env comes from ../BE/.env  (resolved as parent.parent / "BE" / ".env")
# use .venv/bin/python for everything
```

Query helper:

```bash
docker exec -e PGPASSWORD=gpi_local gpi-db psql -U gpi -d gpi_sample_db -At -F'|' -c "<sql>"
```

Extract and run the current prefetch SQL standalone:

```bash
.venv/bin/python - <<'PY'
import ast, pathlib
tree = ast.parse(pathlib.Path("funda_agent_exp.py").read_text())
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if getattr(t, "id", None) in ("_INV_SQL", "_PERSONA_SQL"):
                pathlib.Path(f"/tmp/{t.id}.sql").write_text(ast.literal_eval(node.value))
PY
sed "s/:survey_id/'<SURVEY_ID>'/g" /tmp/_INV_SQL.sql > /tmp/run.sql
docker cp /tmp/run.sql gpi-db:/tmp/run.sql
docker exec -e PGPASSWORD=gpi_local gpi-db psql -U gpi -d gpi_sample_db -At -f /tmp/run.sql | cut -d'|' -f3- | wc -c
```

Run the agent against a specific survey:

```bash
.venv/bin/python funda_agent_exp.py \
  --survey-id <SURVEY> --client-id <CLIENT> --org-id <ORG> \
  --thread-id t1 --prompt "<question>"
# flags: --chat (multi-turn, follow-ups on stdin), --no-inventory (suppress the packet)
```

### Test fixtures

| Survey | id | client_id | org_id | why |
|---|---|---|---|---|
| Herbalife Nutrition Survey | `6263cf71-23b7-4462-9ccf-4a00a7267672` | `37cbf852-a2b7-4f8b-96b4-7f67432f88cd` | `c75d846a-e265-4f69-92c1-91308e0697f6` | 6 numeric hedonics + 8 invisible categorical KPIs. **Primary case.** |
| Tasting Survey (Frito-Lay) | `e14528a9-01ac-4dff-844e-0914dbdb3759` | `f6b05cb4-cea2-4855-816e-c92e5e5d22ff` | `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | **Budget worst case.** 143 questions, 80 Section-A measures, 37 screeners. |
| Product evaluation survey | `e0ccef3c-4541-45e5-89d8-50e3b0c211af` | — | — | Worst coverage: only 1 of 6 liking measures numeric. **No `organization_id`** — the scope resolver may refuse it; query the DB directly. |
| Stress Testing | `76fba647-1568-479e-9804-8a5460a4d1c6` | — | — | All 18 question types; placeholder prompts. |
| (largest catalog) | `f5ab9358-04fd-46b1-ad22-0f757f301a50` | — | — | Largest category/label volume. |
| Discrimination and Temporal | `8d795e0c-8044-4727-9171-835aeda10c81` | — | — | tcata / tds / triangle / tetrad / time-intensity. |
| default sensory, 13 attributes | `39af3240-42a8-4e35-8c7d-c61703d5ce3f` | `c0b3b212-bc35-45ef-b69d-8257d3735a90` | `46671273-0b12-49a5-b9de-60f81d192818` | Pooled line-scale batteries with full anchors. Code defaults. |

Verified question ids for extraction and fit regression tests:

| Question | qid | type |
|---|---|---|
| Herbalife — liking of SWEETNESS | `dc5a2ca7-2d64-4b5f-909a-9226d71d2406` | `vertical-rating`, numeric |
| Herbalife — JAR SWEETNESS (`Level 1 (low)`..`Level 5 (high)`) | `f1c9d37e-275f-4ffe-86b8-c386199d5a72` | `multiple-choice`, **no `optionAnswer` key** |
| Herbalife — recommend intent | `60a9435e-c409-4041-a0bf-97db565c7021` | `multiple-choice` |
| Herbalife — expectation fit | `d50c83de-9856-4e54-b53d-84c4d9757c5b` | `multiple-choice` |
| Tasting — purchase frequency | `adf03347-4cc9-4e2b-82fb-ef3bc0985174` | `multiple-choice`, product-linked |

**Canonical end-to-end fixture — and the regression baseline.** Tasting Survey
`e14528a9-01ac-4dff-844e-0914dbdb3759`. The main agent passes **selection only**:

```python
attribute_kpi_analysis(
    targets=[{"question_id": "adf03347-4cc9-4e2b-82fb-ef3bc0985174", "role": "target"}],
    attributes=[
        {"question_id": "8397f6e4-7d8d-44b4-8623-3a379df0b38c", "role": "attribute"},  # APPEARANCE  1-9
        {"question_id": "4e40f7f4-8662-47f1-b94e-11da2fcf1865", "role": "attribute"},  # AROMA       1-9
        {"question_id": "9b4547dd-c5d3-437f-ba59-b2f870e485e6", "role": "attribute"},  # FLAVOR      1-9
        {"question_id": "ffe5b05d-0402-4986-8ecd-bf2de5e5f384", "role": "attribute"},  # TEXTURE     1-9
        {"question_id": "bf89c26a-727f-426f-bb3a-c0788b728f80", "role": "attribute"},  # AFTERTASTE  1-9
        {"question_id": "60652a5e-82c1-4493-b6cd-453d38099790", "role": "attribute"},  # CRISPINESS  1-5
        {"question_id": "6c3268c0-ae29-4a19-9ed6-0f7de4e46e65", "role": "attribute"},  # FRESHNESS   1-5
    ],
    random_state=20260819,
)
```

No `encoding`, no `k`, no boxes. Measured baseline the implementation must reproduce
(`random_state=20260819`, design-range normalization to `[0,1]`):

| stage | measured expectation |
|---|---:|
| **A** | **1,200** complete rows = 1,200 single-product enrollments, **0** partial; regime detected `single_product` |
| **9.2** | `adf03347` -> `ordinal`, direction "higher code = less often", `Never` excluded (**n=13**) |
| **B** | five attributes design range 1–9, two 1–5; raw sd 0.251 (`CRISPINESS`) … 1.237 (`AFTERTASTE`) |
| **B** | normalized means 0.503–0.922; `CRISPINESS` flagged `near_constant` (mean 0.503, sd 0.063) |
| **level/shape** | PC1-dominated; clusters separate by **level**, not type — turn 2 must use level-based names |
| **D** K=4 | sizes **[455, 355, 160, 230]**; same-seed reproducible; cross-seed agreement **0.998** |
| **D** K=5 | sizes **[81, 317, 412, 171, 219]**; cross-seed agreement **0.993** |
| **I** | tercile boxes on all 7 dims -> overlaps 0.83 / 0.86 / 0.41; **clusters 2 and 3 both map to `all_low`** -> many-to-one must be reported |
| **L/M** K=4 centroids | R² 0.9665 @1 comp, 0.9981 @2, **1.0000 @3**; LOO Q² **0.4411** @1, 0.5655 @2 |
| **L/M** K=4 cluster × product | n=**16**, R² **0.8855** @1, 0.9169 @2 — does not saturate |
| **N** K=4 | `t₁` = −0.970 / −2.660 / **+3.041** / +0.589 for c0…c3; c1 negative, c2 positive |
| **N** wording | c2 "positive" alignment = **buys less often**; c1 is the most frequent purchaser |

Two further invariants: no stage artifact appears in any transcript, and main history holds exactly
four messages.

**Smaller extraction-only fixture** (steps 3–4, before clustering exists): Herbalife `dc5a2ca7`
(numeric liking of sweetness) × `f1c9d37e` (JAR sweetness). 450 rows, 0 duplicates at respondent ×
product, and `f1c9d37e` carries **no `optionAnswer` key at all** — the case that proves the
label-first rule.
