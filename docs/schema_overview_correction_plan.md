# `schema_overview.md` Correction Plan

**Goal: make the agent's SQL correct on the first attempt, and make a wrong answer visible rather than silent.**

Scope: `final_agent_work/schema_overview.md`. Token reduction is a secondary benefit, not
the objective — every change below is justified by an accuracy failure it prevents or a
discovery query it removes.

All measurements were taken against the live `gpi_sample_db` Postgres 15 container
(`docker exec gpi-db psql -U gpi -d gpi_sample_db`) on 2026-08-06, and against
`docs/db_schema.md`, which was verified byte-exact to that database on the same date
(112/112 tables, 1,214/1,214 columns, 334/334 foreign keys, 0 type mismatches).

---

## Table of contents

1. [Context: what this file is and how it is consumed](#1-context)
2. [Failure inventory — ranked by accuracy impact](#2-failure-inventory)
3. [Ready-to-apply corrections (C1–C12)](#3-corrections)
4. [Additive content the file lacks (A1–A5)](#4-additions)
5. [Deletions and their canonical owners (D1–D7)](#5-deletions)
6. [Target structure and budget](#6-target-structure)
7. [Phased execution with gates](#7-phases)
8. [Risks](#8-risks)
9. [Verification log — every prescription executed against the live DB](#9-verification)
10. [Appendix: measurement queries](#10-appendix)

---

<a name="1-context"></a>
## 1. Context: what this file is and how it is consumed

### 1.1 One consumer

```
funda_agent_exp.py:202   SCHEMA_OVERVIEW_PATH = Path("schema_overview.md")
funda_agent_exp.py:205   SCHEMA_OVERVIEW = SCHEMA_OVERVIEW_PATH.read_text(...)
api_funda_agent_exp.py:51   import funda_agent_exp as agent      # inherits the same prompt
```

`funda_agent_exp.py` is the only reader. Paths are CWD-relative — all commands must run from
`final_agent_work/`.

### 1.2 Three prompt slots with different send frequencies

| Slot | Content | Sent when |
|---|---|---|
| **A** tool schema | `nl2sql_tool` / `run_survey_stats` docstrings | every turn with tools bound |
| **B** lean system prompt | `agents.yaml` role/goal/backstory + `PG_DIALECT_RULES` + `tasks.yaml` expected_output + `REPORTING_RULES` | **every turn** |
| **C** schema block | `schema_overview.md` | only when `sql_done == False` |

`funda_agent_exp.py:1656-1685` puts C last so that B is a literal prefix of B+C, and asserts it:

```python
assert SYSTEM_PROMPT.content.startswith(SYSTEM_PROMPT_LEAN.content), (
    "SYSTEM_PROMPT_LEAN must stay a literal prefix of SYSTEM_PROMPT, or turn 1 loses the cache"
)
```

**Consequence that governs every relocation decision below:** C is the only slot ever
dropped. Moving a SQL-only rule out of C into B makes the prose turn pay for a rule it
cannot use. SQL-only content stays in C.

### 1.3 The file today

32,501 chars ≈ 8,100 tokens.

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

### 1.4 The gate: `regression.xlsx`, 69 rows, contiguous by route

Indexed 1-based by `scripts/bench_regression_exp.py --row`:

| Route | Rows | n |
|---|---|---:|
| Survey type (current) | **1–10** | 10 |
| Historical comparison | **11–17** | 7 |
| Statistical analysis | **18–23** | 6 |
| Benchmark comparison | **24–33** | 10 |
| Semantic-match / absence traps | **34–38** | 5 |
| "Isolates" traps | **40–42** | 3 |
| Infrastructure | 39, 43–69 | 28 |

Rows 1–42 are the accuracy gate. `docs/needle_haystack_testset.md` documents what 34–38 and
40–42 test.

### 1.5 What `docs/db_schema.md` may and may not be used for

**May:** column-existence and FK oracle. Verified exact against the live DB.

**May not:** an instruction source, and it must never be loaded into the agent (97,619 chars
≈ 24k tokens, 3× this file, ~90% tables the four routes never touch). Its generated query
patterns reproduce the exact errors this file exists to prevent:

- `db_schema.md:296` — `LEFT JOIN answer a` with `a.question_id = :question_id` in the
  `WHERE`, which null-rejects the outer side and silently makes it an inner join;
- same block — unguarded `::numeric` cast on `optionAnswer`, `AVG` with no `COUNT`/`STDDEV`;
- `db_schema.md:321` — option distribution divided by **all survey enrollments** rather than
  by the people who answered the question.

Catalog = facts. This file = semantics.

---

<a name="2-failure-inventory"></a>
## 2. Failure inventory — ranked by accuracy impact

Each row is a way the current file causes a **confidently wrong** answer, an unnecessary
discovery query, or a false "no data" verdict.

| # | Failure | Severity | Evidence | Fix |
|---|---|---|---|---|
| F1 | **`line-scale` averages pool unrelated attributes.** The file's own flagship cross-survey example groups by `question_id` and averages `optionAnswer`, but one `line-scale` question is N independent sliders. | **Critical** — silent wrong number, in the section read for historical comparison (rows 11–17) | Only 247 / 1,164 `line-scale` questions have a single option; 252 have 4, 181 have 5, up to 13. At answer level only 547 / 2,783 are single-attribute. | [C1](#c1) |
| F2 | **10 of 18 question types are undocumented or misdescribed**, so their responses read as "no data". | **Critical** — false absence verdicts, and rows 34–38 test exactly this | 1,332 question definitions. Zero grep hits in the file for `triangle-test`, `tetrad-test`, `tds`, `individual-balloting`, `upload-multimedia`, `sectionCommentAnswer`, `optionLabel`. | [C2](#c2), [C3](#c3) |
| F3 | **`tcata` is filed as "selected option"** but stores an `action`/`t_ms` event stream. | High — wrong distribution reported as correct | 547 aqo rows, keys `action` + `t_ms`, 5.36 rows/answer | [C3](#c3) |
| F4 | **No expected row-multiplicity anywhere.** "May multiply rows" is advisory; the model cannot check it. | High — inflated `n`, wrong means | Measured factors 1.00 → 53.62 by type | [A1](#a1) |
| F5 | **`survey_nomenclature` claimed to store country.** It does not. | High — sends the agent to a table that cannot answer, then to a false "not available" | 23 columns, no country. `survey.country` is the home (31/1347 non-null, 23 non-blank). `survey_nomenclature` = 16 rows for 1,347 surveys. | [C4](#c4) |
| F6 | **`positionLabels` presented as the universal scale source.** | Medium-high — NULL scale silently, then a wasted discovery query | Exists only for `line-scale` (1162/1164), `vertical-rating` (628/640), `time-intensity-slider` (178/178); ≤1 elsewhere | [C5](#c5) |
| F7 | **No denominator contract.** Percentages get whatever denominator is convenient. | High | `STDDEV` appears **0 times** in the file; no denominator vocabulary at all | [A2](#a2) |
| F8 | **No coverage diagnostics.** `numeric_n` vs `answered_n` vs `product_linked_n` never surfaced. | High — dropped rows are invisible | Product attribution ranges 100% (`vertical-rating`) to 0% (discrimination tests) | [A2](#a2), [C6](#c6) |
| F9 | **`info` questions never carry answers** but are counted as questions. | Medium | 558 `info` questions across 333 surveys, 0 answers | [C7](#c7) |
| F10 | **Historical survey resolution ranks on title first.** | Medium — picks an empty draft over the real run | Only 330 / 1,347 surveys have any answers (24.5%); 165 duplicated (org, title) pairs; 177 templates; 580 drafts | [C8](#c8) |
| F11 | **`answer.product_id` documented as a foreign key.** It is not declared. | Low (doc correctness) | `answer` declares only `question_id`, `enrollment_id`. But 0 orphans and 0 `product."surveyId" <> enrollment.survey_id` — the relation holds in practice. | [C9](#c9) |
| F12 | **Benchmark identity reads as intrinsic**, and the registry/flag route is undocumented. | Low today, high on the next category | `benchmark_registry` has 1 row whose `survey_id` is byte-identical to the ID hardcoded at `agents.yaml:21` — **and the flags disagree with it** (see [C10](#c10)) | [C10](#c10) |
| F13 | **`answer.value` implied to be a general fallback.** | Low — the file's `COALESCE` order is already safe | `value` is non-blank 0 times across 179,769 structured-type answers | [C11](#c11) |
| F14 | **No cross-survey instrument-compatibility rule.** Prompt similarity is treated as sufficient. | Medium — wrong comparison across waves (rows 11–17) | `question.language` and `question.hasPiping` exist; neither is mentioned | [A3](#a3) |
| F15 | **No route-first structure.** The agent searches 32k chars for whichever rule looks locally relevant. | Medium — extra turns, missed rules | Four routes are already the regression taxonomy | [A4](#a4) |

---

<a name="3-corrections"></a>
## 3. Ready-to-apply corrections

Each correction gives the current text, the evidence, and replacement text.

<a name="c1"></a>
### C1 — `line-scale` grain (fixes F1) · **highest priority**

**Current** — `schema_overview.md:320-348`, the canonical cross-survey comparison:

```sql
-- step 1 filters:  AND q."typeOfQuestion" = 'line-scale'
-- step 2 computes:
    AVG((aqo."answerData" ->> 'optionAnswer')::numeric) AS avg_score
FROM matched AS m
JOIN answer AS a ON a.question_id = m.question_id
JOIN answered_question_options AS aqo ON aqo.answer_id = a.id
GROUP BY m.survey_id, m.prompt;
```

**Evidence.** A `line-scale` question is not one scale; it is N sliders, each a
`question_option` with its own label and its own `optionAnswer`:

```
prompt      | label | optionAnswer
------------+-------+----
Line scale  | One   | 6
Line scale  | Two   | 10
Line scale  | Three | 13
Line scale  | Four  | 6
Line scale  | Five  | 9      -- ONE answer, five different attributes
```

Sliders per `line-scale` question: 1 → 247 questions, 2 → 86, 3 → 115, **4 → 252**,
**5 → 181**, 6 → 81, 7 → 54, 8 → 24, 9 → 18, 10 → 28, 11 → 8, 13 → 2. Only 21% are
single-slider. `vertical-rating`, by contrast, is exactly 1.00 option per answer across all
164,800 rows.

So the example above averages unrelated attributes into a unitless number, and it does so in
the section the model reads when performing historical comparison.

**Replacement.** Delete the worked query; replace with an invariant plus the grain rule:

```text
### Rating grain: one question is not always one scale

A rating question's grain depends on its type:

| type | options per answer | analytical grain |
|---|---|---|
| vertical-rating       | exactly 1  | question_id (× product_id) — safe to average directly |
| line-scale            | 1..13, median 4 | question_id × question_option_id (× product_id) |
| matrix                | ~10        | question_id × matrix_row_option_id (× product_id) |
| time-intensity-slider | ~54 (time series) | collapse the series per (answer, option) FIRST |

A `line-scale` question carries one slider PER `question_option`; the option's `label` is
the attribute name (sweetness, texture, ...). Averaging `optionAnswer` grouped by
`question_id` alone pools different attributes into a number with no unit. Only 21% of
line-scale questions in this database have a single slider, so this is the common case,
not the edge case.

ALWAYS include `question_option.label` in the SELECT and the GROUP BY for line-scale,
matrix and time-intensity-slider measures, and report the attribute alongside the mean.
If the user asked for "the" score of a multi-slider question, return the per-attribute
means and say the question measures several attributes — do not pick one silently and do
not average across them.
```

<a name="c2"></a>
### C2 — Complete the question-type inventory (fixes F2)

**Evidence.** Live `question."typeOfQuestion"` distribution vs. coverage in the file:

| type | questions | surveys | documented? |
|---|---:|---:|---|
| `multiple-choice` | 3,097 | 852 | yes |
| `line-scale` | 1,164 | 514 | yes (grain wrong — C1) |
| `open-answer` | 985 | 462 | yes |
| `vertical-rating` | 640 | 124 | yes |
| `multiple-open-answer` | 565 | 260 | yes |
| `info` | 558 | 333 | **no** |
| `triangle-test` | 345 | 237 | **no** |
| `email` | 335 | 208 | yes |
| `tetrad-test` | 322 | 230 | **no** |
| `matrix` | 189 | 98 | yes |
| `contact-information` | 184 | 132 | yes |
| `time-intensity-slider` | 178 | 126 | yes (grain wrong — C1) |
| `individual-balloting` | 153 | 121 | **no** |
| `tcata` | 143 | 111 | **misdescribed** (C3) |
| `tds` | 141 | 110 | **no** |
| `upload-multimedia` | 70 | 40 | **no** |
| `ranking` | 39 | 31 | partial |
| `paired-questions` | 30 | 20 | partial |

**1,332 question definitions across ~700 survey-appearances are invisible to the agent.**
When the agent cannot find the storage path for a type, the observable outcome is a
"this survey has no such measure" answer — which is the exact failure mode regression rows
34–38 test.

<a name="c3"></a>
### C3 — Replace the response-storage dispatch table (fixes F2, F3)

**Current** — `schema_overview.md:704-713`, 8 rows, with `tcata` filed under "selected option".

**Evidence** — measured `answerData` keys and `answered_question_options` FK usage per type:

```
type                  | answerData keys                        | aqo FK populated
----------------------+----------------------------------------+---------------------------
vertical-rating       | optionAnswer (100% numeric, 0–9)       | question_option_id
line-scale            | optionAnswer (100% numeric, 0–99.6)    | question_option_id
multiple-choice       | optionAnswer (TEXT label, 155,863)     | question_option_id
matrix                | optionLabel (116,609)                  | question_option_id + matrix_row_option_id
multiple-open-answer  | optionAnswer (text)                    | question_option_id
paired-questions      | optionAnswer, responseType             | question_pair_id (+ question_option_id)
triangle-test         | optionAnswer (numeric code), sampleLabel | question_set_id
tetrad-test           | optionAnswer, sampleLabel, group       | question_set_id
tcata                 | action, t_ms                           | question_option_id
tds                   | action, t_ms                           | question_option_id
time-intensity-slider | optionAnswer, t_ms                     | question_option_id
individual-balloting  | optionAnswer, sectionCommentAnswer     | question_option_id
ranking               | rank, optionLabel, justificationText   | question_option_id
contact-information   | optionAnswer (text)                    | question_option_id
open-answer / email / upload-multimedia | (no aqo rows at all) | —
info                  | (never answered)                       | —
```

**Replacement table** for §5 "Scalar versus structured answers":

```text
| Question type | Where the value lives | How to read it | Grain note |
|---|---|---|---|
| `open-answer`, `email`, `upload-multimedia` | `answer.value` | text, read directly; no `answered_question_options` rows exist | 1 row/answer |
| `multiple-choice`, `multiple-open-answer` | **`aqo.question_option_id → question_option.label` (authoritative)**; `aqo."answerData" ->> 'optionAnswer'` carries the same text but **is absent on 7,744 of 163,607 mc rows (4.7%)** — VERIFIED, see V1 | use the join, not the JSONB, or you silently drop 4.7% of selections; `question_option.analytical_value` gives the numeric code | multi-select: 2.2 (mc) / 20.8 (moa) rows per answer on average — but 507 of 689 answered mc questions are single-select, see A1 |
| `vertical-rating` | `aqo."answerData" ->> 'optionAnswer'` | `NULLIF(...,'')::numeric` guarded by `~ '^-?[0-9]+(\.[0-9]+)?$'`. **Do NOT read `analytical_value` here** — on answered rows it is 0 on a blank placeholder option and agrees with the rating on only 47 of 164,800 rows (VERIFIED, V1d) | exactly 1 row/answer — the safe overall-liking instrument |
| `line-scale` | same as vertical-rating | same | **N rows/answer, one per attribute slider — group by `question_option.label`** (C1) |
| `time-intensity-slider` | `aqo."answerData" ->> 'optionAnswer'` plus `t_ms` | a TIME SERIES, ~54 rows/answer — collapse per (answer, option) before averaging | not repeat ratings |
| `matrix` | row = `aqo.matrix_row_option_id → question_option.label`; value = `aqo.question_option_id → question_option.analytical_value` (numeric); `answerData ->> 'optionLabel'` duplicates the **column** label (VERIFIED: matches the column on 116,501/116,609 rows, the row on 0) | sentinels exist in the option catalog (max 7,777) but reach only **3 of 116,609 answered rows** — a MIN/MAX range check is cheap insurance, not a blocker | ~9.8 rows/answer, one per matrix row |
| `ranking` | `aqo."answerData" ->> 'rank'` (int); `optionLabel` names the item; `justificationText` optional | ranks are ordinal — report order + mean rank, never a t-test on mean ranks | ~4.6 rows/answer |
| `paired-questions` | `aqo.question_pair_id → question_pair`; `answerData ->> 'optionAnswer'`, `responseType` | join for the compared pair | ~9.6 rows/answer |
| `triangle-test`, `tetrad-test` | `aqo.question_set_id → question_set`; `answerData ->> 'optionAnswer'` is the chosen sample CODE, `sampleLabel` its A/B letter | discrimination tests — the metric is a correct-identification RATE, not a mean. The full derivation is [C3b](#c3b) — it is workable and verified, but needs three sources joined | 1.0 (triangle) / 4.0 (tetrad) rows/answer; **`product_id` is NULL on 100%** |
| `tcata`, `tds` | `aqo."answerData" ->> 'action'` and `->> 't_ms'` | a TIME-STAMPED EVENT STREAM, **not** a selected option. `tcata` carries BOTH `selected` and `deselected` (343 / 204) — **counting `selected` rows over-counts endorsement: 122 of 255 (answer, option) pairs contain a deselect and 97 of 255 (38%) net to zero.** Net per (answer, option) before counting, or take the last event by `t_ms`. `tds` carries `selected` only. | ~5 rows/answer |
| `individual-balloting` | `answerData ->> 'optionAnswer'` (numeric) + `->> 'sectionCommentAnswer'` (free text) | both present on the same row | ~7.8 rows/answer |
| `contact-information` | `answerData ->> 'optionAnswer'` (text) | one row per contact field | ~5 rows/answer |
| `info` | — | **display-only; never has answers** (VERIFIED: 0 answer rows). Exclude from question counts and from any skip/completion denominator. | 0 rows |
```

<a name="c3b"></a>
### C3b — Discrimination tests: the correct-identification derivation

Asserting "the metric is a correct-identification rate" is not enough for the agent to
write the query. The path exists and I verified every hop:

```text
question.settings->'sampleA'->'codes'   -- e.g. ["988","336"]  which 3-digit codes are sample A
question.settings->'sampleB'->'codes'   -- e.g. ["521","914"]  which are sample B
question_set.combination                -- e.g. 'BBA' — the letter in each served position
question_set."setData"->>'sample1_code' -- the code served in position 1 (…2, …3, and
                                        --   sample4_code for tetrad)
aqo."answerData"->>'optionAnswer'       -- the code the respondent picked
aqo."answerData"->>'sampleLabel'        -- the A/B letter of the picked sample

Correct = the respondent picked the sample whose letter is the MINORITY letter in
`combination` (in 'BBA' the odd sample is the A).

Join key: aqo.question_set_id → question_set.id.
```

Verified: `question_set.question_id = answer.question_id` on **1,834 / 1,834** rows, and the
picked code is one of the served codes on **1,834 / 1,834** (223 triangle + 1,611 tetrad)
once `sample4_code` is included for tetrad. So the join is sound and complete.

Report the rate with its N and the chance level (1/3 triangle, 1/4 tetrad) — a 40%
identification rate is not "40% could tell them apart".

<a name="c4"></a>
### C4 — `survey_nomenclature` country claim (fixes F5)

**Current** — `schema_overview.md:141-147`:

> `survey_nomenclature` stores one naming/tagging record per survey (product category,
> nomenclature client label, type of test, **country**, survey date) …

**Evidence.** Its 23 columns are `id`, `survey_id`, `nomenclature_client_id`,
`client_label_snapshot`, `formula_fw`, `sensorik_code`, `survey_date`,
`nomenclature_category_id`, `category_code_snapshot`, `category_label_snapshot`,
`nomenclature_type_of_test_id`, `type_of_test_code_snapshot`, `type_of_test_label_snapshot`,
`note`, `generated_name`, `generated_title`, `unique_name`, `survey_url`, `created_by`,
`updated_by`, `createdAt`, `updatedAt`, `organization_id`. **No country column.**

Sparsity: `survey.country` non-null on 31 / 1,347 (2.3%), non-blank on 23 (1.7%);
`survey_nomenclature` holds 16 rows for 1,347 surveys (1.2%).

**Replacement:**

```text
Survey country is on `survey.country` — sparse (31 of 1,347 surveys; 23 non-blank).
`survey_nomenclature` holds historical naming and tagging only: survey date, category /
client / type-of-test label snapshots, `generated_name`, `generated_title`, `unique_name`,
`survey_url`, plus FKs into `nomenclature_category`, `nomenclature_client`,
`nomenclature_type_of_test`. It has NO country column, and is sparser still (16 rows for
1,347 surveys). Check for a row before relying on either; fall back to product/survey name
matching when absent.
```

<a name="c5"></a>
### C5 — Qualify `positionLabels` by type (fixes F6)

**Current** — `schema_overview.md:403-406`: the scale "lives here", unconditionally.

**Evidence.** `question_option."optionSettings" ? 'positionLabels'` by type:
`line-scale` 1,162 / 1,164 · `vertical-rating` 628 / 640 · `time-intensity-slider` 178 / 178 ·
everything else ≤ 1 of 3,000+.

`analytical_value` is populated on **every** option of every scored type (0 nulls), with
observed maxima: `multiple-choice` 63, `line-scale` 40, `matrix` **7777** (sentinels),
`vertical-rating` 9, `tcata` 29, `ranking` 6.

**Replacement:**

```text
Scale signature by type:
- `line-scale`, `vertical-rating`, `time-intensity-slider`:
  `jsonb_array_length(question_option."optionSettings" -> 'positionLabels')` = number of
  scale positions. This key is on `question_option`, NOT on `question.settings` — reading
  it there returns NULL silently.
- every other type: `positionLabels` does not exist. Derive the signature from
  `COUNT(question_option)` for the question plus the observed MIN/MAX of
  `analytical_value`. Validate the range: matrix options carry sentinel values (max
  observed 7777), so `analytical_value` is not automatically a scale point.
```

<a name="c6"></a>
### C6 — Product attribution is type-dependent (fixes F8)

**Evidence.** `answer.product_id` non-null rate by type:

| type | answers | product-linked | rate |
|---|---:|---:|---:|
| `vertical-rating` | 164,802 | 164,762 | 100.0% |
| `paired-questions` | 820 | 809 | 98.7% |
| `line-scale` | 2,783 | 2,644 | 95.0% |
| `matrix` | 11,905 | 7,266 | 61.0% |
| `open-answer` | 16,708 | 9,258 | 55.4% |
| `multiple-choice` | 75,141 | 24,086 | 32.1% |
| `triangle-test` | 229 | 0 | **0%** |
| `tetrad-test` | 421 | 0 | **0%** |

**Text to add:**

```text
Not every answer is attributable to a product. A per-product breakdown of a
`multiple-choice` question drops ~68% of responses by default, and a per-product breakdown
of a `triangle-test`/`tetrad-test` drops ALL of them (product_id is NULL on 100%).
Always return `product_linked_n` beside `answered_n` and report the ratio when it is
materially below 1, rather than letting the join drop rows silently.
```

<a name="c7"></a>
### C7 — Exclude `info` from question and skip denominators (fixes F9)

```text
`info` questions (558 across 333 surveys) are display-only and NEVER carry answers.
Exclude `"typeOfQuestion" = 'info'` from "how many questions" counts, from unanswered /
skipped rates, and from any completion denominator — otherwise the survey reports a false
skip rate.
```

<a name="c8"></a>
### C8 — Historical survey resolution: rank on responses, not title (fixes F10)

**Current** — `schema_overview.md:291-304` ranks `s.title ILIKE '%…%'` by answer count as a
tie-break.

**Evidence.** 1,347 surveys; only **367 have enrollments** and **330 have answers** (24.5%).
177 are templates, 60 archived, 580 draft, 258 deprecated. **165 (organization_id, title)
pairs are duplicated.** So a title match is very likely to hit an empty duplicate.

Available discriminators, with population: `uniqueName` 1,347/1,347 · `publishedAt` 598 ·
`archived_at` 60 · `isTemplate` 177 · `country` 31 · `openedAt` **23** ·
`is_container` **0 (inert — do not use)**.

**Replacement — the one worked query worth keeping in full:**

```sql
SELECT s.id, s.title, s."uniqueName", s.state, s.country,
       s."publishedAt", s."createdAt", s."isTemplate", s.archived_at,
       (SELECT count(*) FROM enrollment e WHERE e.survey_id = s.id) AS enrollments,
       (SELECT count(*) FROM answer a
          JOIN enrollment e ON e.id = a.enrollment_id
          WHERE e.survey_id = s.id) AS answers
FROM survey AS s
WHERE s.organization_id = :organization_id
  AND s.title ILIKE '%<matched name>%'
ORDER BY answers DESC, s."publishedAt" DESC NULLS LAST;
```

```text
Rank candidates in this order of weight:
  1. has answers            (dominant — a perfectly named empty draft is useless)
  2. NOT "isTemplate"
  3. archived_at IS NULL
  4. name / uniqueName similarity
  5. publishedAt (fall back to createdAt) proximity to the period asked about
  6. country / category match
`openedAt` is populated on 23 surveys and `is_container` on none — treat the first as a
bonus signal and ignore the second.
Only when EVERY same-named candidate has zero answers may you report that no comparable
historical data exists.
```

Note: `ILIKE` on `survey.title` is legitimate and is **not** rejected by
`_SEMANTIC_FILTER_RE` — that guard covers `prompt` / `promptHtml` / `label` / `labelHtml` /
`internal_name` / `optionDefinition` only.

<a name="c9"></a>
### C9 — `answer.product_id` is a logical join (fixes F11)

**Current** — `schema_overview.md:720`: “`answer.product_id → product.id`”.

**Evidence.** `answer` declares exactly two foreign keys:
`question_id → question.id`, `enrollment_id → enrollment.id`. No FK on `product_id`.
In practice the relation holds: **0 orphan `product_id`** and **0 rows** where
`product."surveyId" <> enrollment.survey_id`.

**Replacement:**

```text
`answer.product_id = product.id` is the logical product join for response analysis, but it
is NOT an enforced foreign key in this schema (`answer` declares FKs only on `question_id`
and `enrollment_id`). No orphans are present today. For cross-survey work, where a wrong
product would be invisible, also assert `product."surveyId" = enrollment.survey_id`.
Do not infer a product from prompt or option text when `product_id` is present.
```

<a name="c10"></a>
### C10 — Document the benchmark route; do not switch to it (fixes F12)

**Evidence.**

```
benchmark_registry: 1 row
  category_label = 'Protein Bars'
  survey_id      = 4ec4b648-99bd-4d72-89ee-9cf1b7626e4c   <- byte-identical to agents.yaml:21
  product_id     = d11af4ab-d207-4f57-9dcd-eb560ee0163b
  is_active      = true
```

And the three signals **currently disagree**:

| survey | `is_benchmark_source` | `benchmark_category_label` | in registry |
|---|---|---|---|
| `4ec4b648…` "Confidential benchmark product…" | **true** | *null* | **yes** |
| `6263cf71…` "Herbalife Nutrition Survey" | false | **'Protein Bars'** | no |

An agent told to "resolve the benchmark through the flags" can land on the Herbalife survey.
A registry lookup today spends a query to learn a value already in the prompt and adds a
failure mode (empty/inactive registry → no benchmark at all) for zero information gain.

**Do:** document the route so benchmark identity reads as configurable rather than
intrinsic:

```text
Benchmark identity is configuration, not a property of the data. Three signals exist:
`benchmark_registry(category_label, survey_id, product_id, is_active)`,
`survey.is_benchmark_source` / `survey.benchmark_category_label`, and
`client.is_benchmark_source`. Today the registry holds ONE active category ('Protein
Bars') and the flags are inconsistent with it, so the fixed benchmark IDs in the agent
configuration remain authoritative. Do not resolve the benchmark by scanning the flags.
```

**Don't:** touch `agents.yaml`'s benchmark carve-out (lines 87–107). Revisit when
`SELECT count(DISTINCT category_label) FROM benchmark_registry WHERE is_active` ≥ 2.

<a name="c11"></a>
### C11 — State where `answer.value` is and is not populated (fixes F13)

**Evidence.** `answer.value` non-blank counts:

| type | answers | value non-blank |
|---|---:|---:|
| `open-answer` | 16,708 | 16,651 |
| `email` | 241 | 233 |
| `upload-multimedia` | 41 | 39 |
| `individual-balloting` | 93 | 52 |
| `multiple-choice` | 75,141 | 845 |
| all other structured types combined | 179,769 | **0** |

```text
`answer.value` is populated only for `open-answer`, `email`, `upload-multimedia` and
partially for `individual-balloting`. It is blank on all 179,769 answers of the structured
types (`vertical-rating`, `matrix`, `line-scale`, `paired-questions`, `tds`, `tcata`,
`time-intensity-slider`, `contact-information`, `multiple-open-answer`). A query that reads
`answer.value` alone for a structured type returns nothing — silently.
`COALESCE(aqo."answerData" ->> 'optionAnswer', a.value)` is the safe order.
```

<a name="c12"></a>
### C12 — Minor drift sweep

- `enrollment_status` is an enum with labels `active`, `completed`, `expired` — the file's
  §5 wording is correct at schema level; keep it. (`expired` has 0 rows in this dump; do
  not state row counts, per the file's own stability boundary.)
- `question.language` and `question.hasPiping` exist and are not mentioned — see [A3](#a3).
- `question_set` is described only as "MaxDiff-style combinations". It is also, and mostly,
  the discrimination-test sample set (`triangle-test`, `tetrad-test`). Columns:
  `tray_id`, `combination`, `setData`, `status`, `question_id`.

---

<a name="4-additions"></a>
## 4. Additive content the file lacks

<a name="a1"></a>
### A1 — Expected row multiplicity (fixes F4)

The file says joins "may multiply rows". The agent cannot act on that. Publish the measured
factor so it becomes an assertion:

**Framing matters here, and my first draft got it wrong.** These are *population averages
across all questions of the type*, not per-question constants. Verified counter-example:
**507 of 689 answered `multiple-choice` questions are exactly 1.00** (single-select); the
2.18 average comes from 123 multi-select questions (59 at 1.01–2.00, 84 at 2.01–5.00, 39
above 5). An agent that treats 2.18 as an assertion would flag a correct single-select
query as broken.

So the rule is: **compute the ratio for your question, and compare it to the type's
expected shape** — not to the population mean.

```text
aqo rows per answer, by question type (population average; answers observed in brackets):

  vertical-rating       1.00   [164,800]  fixed 1:1 — any deviation IS a join bug
  triangle-test         1.00   [223]      fixed 1:1
  multiple-choice       2.18   [75,053]   VARIES: 1.00 for single-select (507 of 689
                                          questions), up to max_selection for multi.
                                          question.settings holds min_selection /
                                          max_selection — read it before asserting.
  tetrad-test           3.98   [405]      ≈ samples in the set
  tds                   4.62   [106]      small n — dominance events over time
  ranking               4.64   [45]       small n — = number of ranked items
  contact-information   4.98   [168]      small n — = number of fields
  line-scale            5.18   [2,778]    = number of attribute sliders on THAT question
                                          (1 to 41; median 4) — read n_options first
  tcata                 5.36   [102]      small n — CATA events, incl. deselects
  individual-balloting  7.84   [93]       small n — ballot items
  paired-questions      9.63   [820]      = pairs shown
  matrix                9.80   [11,902]   = number of matrix rows on THAT question (6-41)
  multiple-open-answer 20.77   [1,394]    = number of sub-fields
  time-intensity-slider 53.62  [136]      small n — TIME-SERIES samples, not repeat ratings

Two are hard invariants: vertical-rating and triangle-test are 1:1, so any other ratio
means the join is wrong. The rest are shape expectations — the ratio should equal that
question's own option/row/field count, which you already have from the candidate
inventory. A result count much larger than the survey's enrollment count is a grain error
until proven otherwise.

Types marked "small n" are measured on under 200 answers in this dump; treat their figures
as indicative, and derive the expected ratio from the question's own structure instead.
```

<a name="a2"></a>
### A2 — Denominator contract and coverage diagnostics (fixes F7, F8)

Genuinely new: the file has no denominator vocabulary and `STDDEV` appears zero times.

```text
Name the denominator before writing the percentage.

| Requested quantity | Default denominator |
|---|---|
| Participation / completion rate | enrolled (or eligible) participants |
| Distribution of answers | respondents who answered THAT question |
| Product score | valid product-attributable numeric responses |
| Missing / skipped rate | respondents presented with the question (exclude `info`) |
| Multi-select percentages | question answerers; totals may exceed 100% |

Every numeric aggregate returns, alongside the statistic:
  answered_n, numeric_n, respondent_n, product_linked_n, AVG, STDDEV_SAMP, MIN, MAX
Report numeric_n / answered_n and product_linked_n / answered_n whenever either is
materially below 1 — a dropped row that is never counted is indistinguishable from a row
that does not exist.

Both sides of a coverage ratio must count the SAME unit. Use COUNT(DISTINCT answer.id) for
answered_n, numeric_n and product_linked_n, and COUNT(DISTINCT enrollment.id) only for
respondent_n. Mixing them produces ratios above 1 on any multi-row type — e.g. a
product-linked count of answers over a respondent count reads as 27/9 = 3.0.
```

<a name="a3"></a>
### A3 — Instrument signature for cross-survey comparison (fixes F14)

```text
Before comparing a measure across surveys, build its signature:

  σ(q) = (construct, "typeOfQuestion", scale signature, attribution mode, grain)

  construct       — what it measures, YOU decide it from the FULL prompt
  typeOfQuestion  — must match; a line-scale intensity is not a vertical-rating liking
  scale signature — position count + anchor labels (C5)
  attribute count — COUNT(question_option) for the question. This is what tells you the
                    GRAIN before you aggregate: n_options = 1 means the mean is meaningful
                    per question; n_options > 1 on a line-scale/matrix means it is not (C1).
                    Two questions with the same construct but different attribute counts
                    are not the same instrument.
  attribution     — product-linked vs survey-level (C6)
  grain           — question_id, or question_id × question_option_id (C1)

Compare two questions only when construct, type, scale and attribution all match.
Prompt similarity alone is NOT sufficient: the same question has a different
`question.id` in every survey, `question_library_item` is too sparse to join on, and
`question.language` / `question.hasPiping` mean two rows can be the same instrument worded
differently or a dynamically worded one. State the matched prompt text per survey in the
answer, so a wrong match is visible.
```

<a name="a4"></a>
### A4 — Route-first retrieval procedure at the top (fixes F15)

Reasoning phases, **not** mandated SQL calls — the existing "prefer one query" rule and its
two-query question-resolution exception must survive intact.

```text
1. CLASSIFY   route ∈ {current, statistics, benchmark, historical}
              info  ∈ {metadata, configuration, response, participation}
2. SCOPE      current/statistics → :survey_id
              historical         → surveys within :organization_id (tenancy via
                                   organization → account → client)
              benchmark          → the configured benchmark IDs (C10)
              Never let one query cross these scope boundaries.
3. RESOLVE    only what is unknown: survey, question, product, comparison side.
              Semantic question match = 2 queries: structural candidate list with FULL
              prompts → you pick → aggregate the chosen ids. Never ILIKE on prompt/label.
4. SIGNATURE  σ(q) (A3) — required for benchmark and historical, cheap elsewhere.
5. VALUE PATH dispatch by "typeOfQuestion" (C3).
6. GRAIN      declare target grain, entity key, value expression, denominator, expected
              multiplicity (A1, A2) before writing the join.
7. AGGREGATE  with coverage diagnostics (A2).
8. TERMINAL   current → answer from the aggregate
              statistics → run_survey_stats with the resolved question ids
              benchmark → report both sides independently, never pooled
              historical → one compatible question per survey, aggregated independently
9. STOP       before another query, name the unresolved variable: missing survey? missing
              question identity? unknown encoding? unknown denominator? missing comparison
              side? no shared join key? An empty result is not itself a reason to re-query.
```

<a name="a5"></a>
### A5 — Query invariants instead of worked SQL

Full examples get copied along with their accidents — C1 is the proof. Per operation keep:

```text
Numeric product score
  Grain:    question_id × question_option_id × product_id
            (question_option_id REQUIRED for line-scale / matrix / time-intensity-slider;
             vertical-rating is 1:1 and may omit it)
  Route:    question → answer → enrollment;  answer.product_id = product.id (logical, C9)
            answer → answered_question_options
  Value:    NULLIF(aqo."answerData" ->> 'optionAnswer','')::numeric
            guarded by  ~ '^-?[0-9]+(\.[0-9]+)?$'
  Scope:    enrollment.survey_id = :survey_id
  Return:   question_id, full prompt, "typeOfQuestion", attribute label, product,
            numeric_n, answered_n, respondent_n, product_linked_n,
            AVG, STDDEV_SAMP, MIN, MAX
  Validate: numeric_n <= answered_n
            aqo_rows / answers matches the type's expected multiplicity (A1)
            product."surveyId" = enrollment.survey_id
```

Keep exactly **two** full worked queries: the tenancy-verification join (three non-obvious
hops through `account`) and the historical candidate-scoring query (C8). Delete the rest.

---

<a name="5-deletions"></a>
## 5. Deletions and their canonical owners

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

| # | Content | Canonical owner | Action in `schema_overview.md` |
|---|---|---|---|
| D1 | `run_survey_stats` routing and fallbacks (§5 "Statistical analysis", ~1,100 chars) | `agents.yaml:41-52, 66-85` — already complete there | delete |
| D2 | Answer shape / aggregates-not-raw-rows (§5 "Summary versus raw", ~1,400 chars) | `tasks.yaml` expected_output + `REPORTING_RULES` | delete |
| D3 | No `LIKE`/`ILIKE` on `prompt`/`label` | `_SEMANTIC_FILTER_REASON` — fires at the moment of the mistake | keep the *why* + the two-query procedure; cut the restated rule |
| D4 | Scope placeholder mechanics (14 mentions) | `PG_DIALECT_RULES` "SCOPE IDS" | keep only inside SQL examples |
| D5 | PostgreSQL dialect limits | `PG_DIALECT_RULES` | one-line pointer (already true) |
| D6 | N + SD with every mean, separability, no UUIDs in prose | `REPORTING_RULES` | reference, do not restate |
| D7 | Semantic domains (§2) + Minimal table bundles (§4) — same table lists twice | — | merge into one intent→table matrix |

Out-of-scope domains (survey logic, panel administration, saved reports, sharing, files,
billing, system tables) collapse to one line:

> For a request outside survey responses, products, statistical analysis, benchmark
> comparison, or historical comparison, inspect `information_schema` before using an
> undocumented table.

**What must NOT move to `agents.yaml` or `PG_DIALECT_RULES`:** anything needed only while
writing SQL. Per §1.2, the schema block is the only slot ever dropped; relocating SQL-only
rules makes the prose turn pay for them on every request.

---

<a name="6-target-structure"></a>
## 6. Target structure and budget

```
 1. Purpose and scope                                          ~400
 2. Retrieval procedure (A4)                                 ~1,800
 3. Core analytical graph + grain                            ~2,000
 4. Grain, multiplicity and denominator invariants  (A1,A2)  ~2,000
 5. Question resolution and instrument signature    (A3)     ~1,600
 6. Response-storage dispatch table, all 18 types    (C3)    ~3,400
 7. Route playbooks
      7.1 Current survey                                      ~700
      7.2 Statistics                                          ~600
      7.3 Benchmark (incl. registry route, C10)               ~900
      7.4 Historical (incl. candidate scoring, C8)            ~900
 8. Validation and zero-result diagnostics                   ~1,500
 9. Exact core columns                                       ~4,000
10. information_schema fallback                                ~300
                                                            ────────
                                                            ~22,700
```

```
32,501  current
-17,600  deletions/compression (§3 →4,000, §5 →5,000, §2+§4 merged →1,800,
                                Stable rules →2,200, Execution contract →300, §6 →400)
+ 5,200  A2–A5
+ 2,600  C3 type coverage + A1 multiplicity
─────────
~22,700  target — a 30% reduction
```

**Set the budget at 21,000–23,000 chars.** A 50–60% cut is only reachable by dropping the
additions, and the additions are where the accuracy comes from. Char count is a budget; the
**gate is the regression score**.

### Preserved essentially intact

1. The analytical-grain table (§1) — now backed by measured multiplicity.
2. The two-stage question-resolution rule (structural candidate list → pick from full
   prompts → aggregate chosen ids). Justified whenever
   `P(wrong question) × cost(wrong answer) > cost(one query)`; a confidently wrong measure
   is expensive, and `_SEMANTIC_FILTER_RE` enforces it deterministically.
3. The `positionLabels`-is-on-`question_option`-not-`question.settings` correction, now
   qualified by type (C5).
4. The tenancy chain `survey → organization → account → client` (there is no direct
   `organization.clientId`).

---

<a name="7-phases"></a>
## 7. Phased execution with gates

Gate command, from `final_agent_work/`:

```bash
PY=/path/to/python scripts/run_regression_exp.sh
```

`instrumentation_logs_funda_exp.jsonl` already records per-turn `input_tokens`,
`cached_input_tokens`, `output_tokens`, `latency_s` — no new instrumentation needed.

### Phase 0 — Make the gate real, then baseline · *no content change ships*

`scripts/run_regression_exp.sh` cannot gate this work as written:

```bash
PY="${PY:-BE/.venv/bin/python}"     # (1) no BE/ under final_agent_work — path does not exist
for R in $(seq 1 18); do            # (2) rows 1-18 only: misses benchmark 24-33 and traps 34-42
  for V in none bindings prefetch_fixed both; do   # (3) 4 variants of an unrelated experiment
```

- Fix the `PY` default; extend to **rows 1–42**; drop the four-variant fan-out for one
  variant × 3 reps.
- Record baseline: per-route scores, mean turn-0 input tokens, cache-hit %, wall time.
  Three reps — wall-clock spread is wide enough to swallow a 4% effect, so prefer token
  counts.

**Gate:** baseline recorded.

### Phase 1 — Correctness fixes · *highest value, lowest risk*

C1 (line-scale grain) · C3 `tcata` row only · C4 (`survey_nomenclature` country) ·
C5 (`positionLabels`) · C9 (`answer.product_id`) · C11 (`answer.value`) · C12 (drift sweep).

**Gate:** rows 1–42 flat or better. C1 should show up as an improvement on historical rows
**11–17**; any drop means something else broke.

*Land this phase on its own.* C1 and C4 are live-wrong today, and C1 produces a confident
wrong number rather than a visible failure.

### Phase 2 — Complete the semantics · *the file grows here*

C2 + C3 (full 18-type dispatch table) · C6 (product attribution) · C7 (`info`) ·
C8 (historical scoring) · C10 (benchmark route) · A1 (multiplicity) · A2 (denominators +
coverage) · A3 (instrument signature).

**Gate:** rows **11–17**, **18–23**, **24–33** improve or hold; rows **34–38** (absence
traps) are the ones C2/C3 should most improve. Accept a token increase — Phase 3 repays it.

### Phase 3 — Deduplicate · *where the reduction comes from*

D1–D7 · A5 (worked queries → invariants) · out-of-scope collapse.

**Gate:** rows 1–42 flat **and** 21,000–23,000 chars. A route that drops identifies content
that was load-bearing — restore that specifically rather than reverting the phase.

### Phase 4 — Front-load the retrieval procedure

A4 at the top; the four route playbooks as sections of the same always-loaded file.

**Gate:** rows 1–42, 3 reps. Watch **34–38** and **40–42** hardest — a restructure is
likeliest to break semantic matching and absence detection.

### Phase 5 — Route-specific physical injection · **DEFERRED**

Splitting the file into four route slices (~6,000–10,000 chars/request) is the largest token
win and the largest risk:

- the route must be chosen **before the first LLM call**, so the classifier is a keyword
  matcher — the same "decide meaning with a pattern" failure `_SEMANTIC_FILTER_RE` exists to
  reject, one layer up;
- misclassification is silent and unrecoverable: an agent handed the `current` slice cannot
  discover it needed `historical`;
- it fragments the prompt cache into four prefixes, and `--chat` follow-ups change route
  mid-thread while message 0 stays pinned by `_trim_history`.

Preconditions: Phase 4 green, a classifier measured on all 69 rows, and an escape hatch
letting the agent request another slice mid-run.

**Order: 0 → 1 → 2 → 3 → 4.** Phase 0 is not optional; gating on rows 1–18 would let a
benchmark or semantic-match regression ship unnoticed.

---

<a name="8-risks"></a>
## 8. Risks

| Risk | Mitigation |
|---|---|
| Cutting a rule that silently prevented a bug | Every deletion maps to a canonical owner (§5); per-route gates; restore specifically, never revert wholesale |
| Restructure breaks semantic matching | Phase 4 gates on rows 34–38 and 40–42, the canaries |
| Rules relocated into `agents.yaml` inflate every prose turn | §1.2 — SQL-only content stays in the schema block |
| Gate misses the routes most at risk | Phase 0 extends coverage from rows 1–18 to 1–42 before anything ships |
| Wall-clock noise hides a regression | 3 reps per gate; prefer token counts |
| Benchmark route rebuilt on a 1-row table whose flags contradict it | C10 — document, do not switch |
| New type coverage is measured on this dump, not guaranteed by the schema | Types and `answerData` keys are structural; state observed counts as *observed*, honouring the file's stability boundary (§6) |
| The file grows before it shrinks | Phases 2 and 3 are adjacent; do not ship 2 without 3 |

---

<a name="9-verification"></a>
## 9. Verification log — every prescription executed against the live DB

The corrections in §3–§4 prescribe SQL. They were not left as derivations: each was
executed against `gpi_sample_db` and checked. Sixteen checks (V1–V16). **Four found errors
in this plan's own first draft, which are now fixed above.**

### 9.1 Errors this pass found and fixed

| ID | Plan claim as first written | What execution showed | Fixed in |
|---|---|---|---|
| **V1** | multiple-choice: `answerData ->> 'optionAnswer'` and the `question_option` join "both give the label" | **False.** 7,744 of 163,607 rows (4.7%) have **no `optionAnswer` key** while `question_option_id` is populated. The JSONB path silently undercounts selections by 4.7%. The join is authoritative. | C3 |
| **V16 + V15b** | multiplicity table read as a per-type assertion | **Misleading.** They are population averages. **507 of 689 answered multiple-choice questions are exactly 1.00**; the 2.18 mean comes from 123 multi-select questions. Asserting 2.18 would flag correct queries as broken. Only `vertical-rating` and `triangle-test` are hard 1:1 invariants. | A1 |
| **V14** | tcata/tds described only as "an event stream" | **Incomplete, and a real correctness gap.** `tcata` carries `deselected` as well as `selected` (204 / 343). **122 of 255 (answer, option) pairs contain a deselect and 97 of 255 (38%) net to zero** — counting `selected` rows over-counts attribute endorsement badly. | C3 |
| **V13** | matrix `analytical_value` sentinels (max 7,777) presented as a live hazard | **Overstated.** Only **3 of 116,609** answered matrix rows exceed 100. Softened to a cheap range check rather than a blocker. | C3 |

Two further gaps were closed: `analytical_value` is **not** the rating for `vertical-rating`
(agrees on 47 of 164,800 rows — it is 0 on a blank placeholder option, V1d), and the
triangle/tetrad correct-identification claim now carries its full derivation (C3b) instead
of being asserted.

### 9.2 Prescriptions confirmed correct by execution

| ID | Check | Result |
|---|---|---|
| **V9** | The A5 numeric-product-score invariant, run verbatim | Executes clean; returns 33 interpretable rows with `numeric_n`, `answered_n`, `respondent_n`, `product_linked_n`, AVG, SD, MIN, MAX |
| **V10** | C1 demonstrated side by side on survey `a79dad5e…` | Attribute-grained: 12 cells, e.g. Salt/Product 1 = 5.67 (n=9), Tangy/Product 2 = 7.00 (n=9). Pooled by `question_id` only: **one number, "Flavor attributes = 6.27, n=108"** — merging four attributes across three products, with an n inflated 4× over the 27 real observations. **C1 is the highest-value correction in the plan, and this is the proof.** |
| **V1g** | line-scale `question_option.label` really is the attribute name | Confirmed: `Color Intensity \| Color Uniformity \| Surface Gloss \| Visual Freshness`. Grouping by it is exactly right. |
| **V2** | matrix row/column mapping | `question_option_id` is the **column** (value), `matrix_row_option_id` the **row**: `answerData ->> 'optionLabel'` matches the column label on 116,501/116,609 and the row label on **0**. |
| **V2a–c** | vertical-rating "safe to average by question_id" | Holds. Multi-option vertical-rating questions exist (their options are scale points, `Strongly disagree`…`Strongly agree`), but **they have zero answers** in this dump; every answered one is single-option, 1.00 row/answer. |
| **V3** | `info` never answered | 0 answer rows. Confirmed. |
| **V4b–f** | triangle/tetrad derivation path | `question_set.question_id = answer.question_id` on **1,834/1,834**; picked code is one of the served codes on **1,834/1,834** once `sample4_code` is included for tetrad. Join is sound. |
| **V6** | ranking `rank` values | Integers 1–7, well formed. |
| **V7** | C8's `s.title ILIKE` is not rejected by `_SEMANTIC_FILTER_RE` | Tested against the actual regex from `funda_agent_exp.py:243`: `s.title ILIKE` **allowed**, `"uniqueName" ILIKE` **allowed**, `q.prompt ILIKE` **rejected**, `qo.label ILIKE` **rejected**, `LOWER(q.prompt) LIKE` **rejected**, `q.prompt = '<exact>'` **allowed**. The C8 query is safe to prescribe. |
| **V11** | The C8 historical candidate query, run verbatim | Executes; correctly surfaces `Herbalife Nutrition Survey` with 450 enrollments / 9,318 answers and its discriminator columns. |
| **V12** | The two-query step-1 candidate inventory, run verbatim | Executes; returns full prompts, type, `language`, `hasPiping`, scale positions, **`n_options`**, respondents, product-linked count. `n_options` (5, 4, 1, 1) is what separates the multi-attribute line-scales from the single vertical-ratings **before** any aggregation — now added to A3. |
| **V16** | The published multiplicity figures | Reconfirmed to the decimal, all 14 types. |
| — | C4, C5, C6, C7, C9, C11 | All re-derived unchanged from the measurements in §3. |

### 9.3 Residual limits

- Six types are measured on fewer than 200 answers (`tcata` 102, `individual-balloting` 93,
  `tds` 106, `ranking` 45, `contact-information` 168, `time-intensity-slider` 136). Their
  multiplicity figures are indicative; A1 now says to derive the expected ratio from the
  question's own structure instead.
- Multi-option `vertical-rating` questions are unexercised (0 answers). If they ever get
  answers, their value may come from `analytical_value` rather than `optionAnswer` — worth
  a re-check before trusting a vertical-rating mean on a survey that uses them.
- All figures are from this dump. Types and `answerData` keys are structural; counts are
  observed. State them as observed, per the file's own stability boundary.

---

<a name="10-appendix"></a>
## 10. Appendix: measurement queries

Every figure in this plan is reproducible with these, run as
`docker exec gpi-db psql -U gpi -d gpi_sample_db -c "<sql>"`.

```sql
-- Question-type inventory (C2)
SELECT "typeOfQuestion", count(*) AS questions, count(DISTINCT "surveyId") AS surveys
FROM question GROUP BY 1 ORDER BY 2 DESC;

-- answerData keys per type (C3)
SELECT q."typeOfQuestion", k.key, count(*)
FROM answered_question_options aqo
JOIN answer a  ON a.id = aqo.answer_id
JOIN question q ON q.id = a.question_id,
LATERAL jsonb_object_keys(aqo."answerData") k(key)
GROUP BY 1,2 ORDER BY 1,3 DESC;

-- aqo FK usage and row multiplicity (A1)
SELECT q."typeOfQuestion", count(*) AS aqo_rows,
       count(aqo.question_option_id)   AS qopt,
       count(aqo.matrix_row_option_id) AS mrow,
       count(aqo.question_pair_id)     AS qpair,
       count(aqo.question_set_id)      AS qset,
       round(count(*)::numeric / count(DISTINCT a.id), 2) AS rows_per_answer
FROM answered_question_options aqo
JOIN answer a  ON a.id = aqo.answer_id
JOIN question q ON q.id = a.question_id
GROUP BY 1 ORDER BY 2 DESC;

-- line-scale is N sliders (C1)
WITH x AS (
  SELECT q.id, count(DISTINCT qo.id) AS opts
  FROM question q JOIN question_option qo ON qo.question_id = q.id
  WHERE q."typeOfQuestion" = 'line-scale' GROUP BY 1)
SELECT opts, count(*) AS questions FROM x GROUP BY 1 ORDER BY 1;

-- answer.value population and product attribution (C6, C11)
SELECT q."typeOfQuestion",
       count(*) AS answers,
       count(*) FILTER (WHERE a.value IS NOT NULL AND a.value <> '') AS value_nonblank,
       count(*) FILTER (WHERE a.product_id IS NOT NULL)              AS product_linked
FROM answer a JOIN question q ON q.id = a.question_id
GROUP BY 1 ORDER BY 2 DESC;

-- positionLabels by type (C5)
SELECT q."typeOfQuestion", count(DISTINCT q.id) AS questions,
       count(DISTINCT q.id) FILTER (WHERE qo."optionSettings" ? 'positionLabels') AS with_poslabels
FROM question q LEFT JOIN question_option qo ON qo.question_id = q.id
GROUP BY 1 ORDER BY 2 DESC;

-- survey_nomenclature / country (C4)
SELECT column_name FROM information_schema.columns
WHERE table_name = 'survey_nomenclature' ORDER BY ordinal_position;
SELECT count(*) FILTER (WHERE country IS NOT NULL) AS notnull,
       count(*) FILTER (WHERE country IS NOT NULL AND country <> '') AS nonblank,
       count(*) AS total FROM survey;

-- answer.product_id integrity (C9)
SELECT count(*) FROM answer a
WHERE a.product_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM product p WHERE p.id = a.product_id);
SELECT count(*) FROM answer a
JOIN enrollment e ON e.id = a.enrollment_id
JOIN product p    ON p.id = a.product_id
WHERE p."surveyId" <> e.survey_id;

-- benchmark signals (C10)
SELECT * FROM benchmark_registry;
SELECT id, title, is_benchmark_source, benchmark_category_label FROM survey
WHERE is_benchmark_source OR benchmark_category_label IS NOT NULL;

-- historical discriminators (C8)
SELECT count(*) AS total,
       count(*) FILTER (WHERE "isTemplate")            AS templates,
       count(*) FILTER (WHERE is_container)            AS containers,
       count(*) FILTER (WHERE archived_at IS NOT NULL) AS archived,
       count(*) FILTER (WHERE "publishedAt" IS NOT NULL) AS published,
       count(*) FILTER (WHERE "openedAt" IS NOT NULL)    AS opened
FROM survey;
SELECT count(*) FROM (
  SELECT organization_id, title FROM survey GROUP BY 1,2 HAVING count(*) > 1) x;
```
