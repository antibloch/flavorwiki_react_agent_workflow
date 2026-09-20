# FlavorAI DB Map

> **How this file applies to the oracle.** Below is a map of the same database
> `funda_agent_exp.py` analyses, so the oracle reasons about the data the same
> way. Four notes:
>
> - Where it says "the SQL tool", use `scripts/query.sh`. Where it says
>   `run_survey_stats`, use `scripts/stats_api.py` — the same endpoint.
> - Unlike `funda_agent_exp.py`, you **do** have a local statistics engine
>   (`scripts/sql_stats.py`), so the line further down about there being "no
>   Python execution tool" does not apply to you: a test the Charts API cannot
>   run is still computable. Never report a statistic as unavailable without
>   trying both.
> - `{survey_id}` / `{organization_id}` / `{client_id}` are the values
>   `scripts/resolve_scope.py` returned in Phase 1.
> - **This is an older revision of the map.** Everything it does say is still
>   accurate, but it is silent on two facts that were established later and that
>   decide answers: multi-attribute `line-scale` / `matrix` batteries (one
>   question, many measures) and the real source of a question's scale points.
>   SKILL.md Phase 2 and Phase 3 step 3 carry both and take precedence. Silence
>   here is not permission to pool.
>
> Its closing instruction holds absolutely: trust a live query over anything
> written here, including this map, whenever the two disagree.

This file is a compact, executable map for choosing tables, preserving
analytical grain, and producing survey-scoped SQL.

It contains the exact core columns needed for ordinary survey analysis, and is
the only schema reference available to the agent. If a table or column is
genuinely needed but not listed here, query `information_schema.columns` (or
`information_schema.tables`) directly with the SQL tool rather than assuming
a name or waiting for a schema-detail tool — none is wired into this agent.

## Execution contract

- When the answer depends on database data, execute the active SQL database tool immediately.
- Do not return a plan, proposed query, or future-tense statement such as
  “I will query” or “I first need to identify...”.
- Prefer one SQL query that both resolves the requested concept and computes
  the result.
- Use an additional query only after an executed query returns no rows,
  exposes genuine ambiguity, or lacks information required by the user.
- The final answer must be based on actual tool output.
- If no SQL execution tool is available, state that database execution is
  unavailable; do not pretend that a query will be run later.
- If an exact table or column needed by the SQL is absent from this file,
  query `information_schema.columns`/`information_schema.tables` to confirm
  it rather than guessing a name.

## Stable rules

- `{survey_id}` is the default scope for an ordinary question: restrict to it
  unless the user explicitly asks to compare against, or reference, another
  survey. A cross-survey lookup must stay within `{organization_id}` and must
  never cross into a different `organization_id` or `client_id`.
- Never use `SELECT *`.
- Preserve quoted camelCase identifiers exactly.
- Do not rely on row counts, seed IDs, current table emptiness, or
  deployment-specific survey counts.
- Compute survey answers from response tables (`answer`, `answered_question_options`).
  Respondent demographics (gender, age, country, etc.) are almost always
  collected as ordinary survey questions and must be resolved the same way —
  see "Respondent demographics" in section 2. `enrollment.user_id` is
  populated for only a small, registered-user minority of enrollments in
  production data; do not default to it. Saved charts and reports are
  configurations, not respondent-level result data, unless a query against
  their actual columns shows otherwise.
- A syntactically successful query returning zero is not automatically a valid
  answer. Reconsider the selected data source and inspect stored values before
  concluding that no matching respondents exist.
- This also applies to survey selection itself: when a cross-survey request
  resolves a named survey to zero enrollments/answers, and other surveys in
  the same `organization_id` share a near-identical title (e.g. a plain title
  vs. a "Copy - " prefixed one, or several drafts of the same test), check
  those sibling candidates for one that actually has enrollments/answers
  before reporting no historical data. Prefer the candidate with real
  response data over the one with the more literal title match.
- A query that filters `question.prompt` by a guessed phrase and returns
  exactly one row is not automatically the right question — it can be a
  confident wrong match just as easily as a confident right one, since a
  literal phrase pattern silently excludes the true match whenever the real
  wording puts the words in a different order (e.g. "How much do you LIKE or
  DISLIKE this product OVERALL?" contains "overall" and "like" but not the
  adjacent phrase "overall liking"). When matching a question by meaning
  rather than by exact ID:
  - Match on the individual keywords as separate `ILIKE` conditions
    (`prompt ILIKE '%overall%' AND prompt ILIKE '%lik%'`), not as one
    concatenated phrase pattern — this matches regardless of word order.
  - State the exact matched `prompt` text in the final answer, so a wrong
    match is visible rather than silently trusted.
  - If more than one plausible candidate exists for the same concept (e.g. an
    "overall liking" question and a similarly-worded "appearance liking" or
    "overall quality" question), say so and pick the one whose full text
    actually matches the concept asked about — do not default to whichever
    row the pattern happened to return first.

---

## 1. Analytical grain

Choose the intended result grain before joining tables.

| Table | One row represents |
|---|---|
| `survey` | one survey |
| `question` | one question definition |
| `question_option` | one possible option for one question |
| `product` | one product configured in a survey |
| `enrollment` | one participation instance in a survey |
| `answer` | one stored answer linked to an enrollment and question, optionally a product |
| `answered_question_options` | one structured option/detail component of an answer |
| `question_pair` | one configured paired comparison |
| `question_set` | one configured set/tray/combination |
| `survey_panel` | one survey-to-panel assignment |
| `charts` | one saved chart definition, not one respondent observation |
| `aggregations` | one saved analysis configuration, not one respondent observation |

Common result grains:

- one row per enrollment/respondent;
- one row per enrollment and product;
- one row per question and option;
- one row per product;
- one row per survey or panel assignment.

`answered_question_options` is one-to-many from `answer`; joining it may
multiply answer rows. Aggregate it first or use distinct entity IDs when the
requested grain is coarser.

---

## 2. Semantic domains

### Survey structure

Use for survey metadata, screens, sections, questions, options, products,
question sets, paired questions, display ordering, nomenclature, and piping.

`survey`, `question_screen`, `question_section`, `question`,
`question_option`, `question_group`, `question_set`, `question_pair`,
`question_library`, `question_library_item`, `product`,
`product_display_order`, `product_display_order_item`,
`survey_nomenclature`, `nomenclature_category`, `nomenclature_client`,
`nomenclature_type_of_test`, `nomenclature_formula_fw_sequence`,
`piping_references`

`survey_nomenclature` stores one naming/tagging record per survey (product
category, nomenclature client label, type of test, country, survey date) as
label snapshots plus FKs into `nomenclature_category`, `nomenclature_client`,
and `nomenclature_type_of_test`. It is sparsely populated — most surveys have
no row here. Do not assume category/country tagging exists; check for a row
before relying on it, and fall back to product/survey name matching when
absent.

### Respondent demographics

Gender, age, country, and similar respondent attributes are almost always
answered as ordinary questions inside the survey itself (e.g. a
`multiple-choice` question with prompt "What is your gender?" or "What is
your age group?"), not stored on a normalized profile column. Resolve them
exactly like any other survey response: find the matching `question` row for
the survey, then read `answer`/`answered_question_options` for it (see
"Participation and survey answers" below and section 5's worked query).

`enrollment.user_id → "user".id → "user".gender/country/city/language` exists
but is a rare fallback: in production data the large majority of enrollments
have a null `user_id` (panel-recruited respondents are not registered
`"user"` rows), so this route silently returns zero for most surveys. Only
use it when the demographic question is confirmed absent from `question` for
that survey. `panelist` (joined via `enrollment.panelist_id`) has no
demographic columns — it only holds name/email/phone/status.

`"user"` is primarily the platform's registered-user/login table (survey
creators, account holders), not a general respondent table — this is another
reason it rarely matches survey enrollments.

### Participation and survey answers

Use for enrollment counts, completion, answer distributions, skipped
responses, response time, product scores, correlations, cross-tabs, and
statistical-test input.

`enrollment`, `answer`, `answered_question_options`, `question`,
`question_option`, `product`

### Panels and fieldwork

Use for panel definitions, panel membership, codes, survey assignments, and
fieldwork status.

`panel`, `panelist`, `panel_panelist`, `panel_code`, `panel_code_format`,
`survey_panel`, `survey_panel_code`, `survey_panel_stats`

Panel membership is not proof of participation. Use `enrollment` to determine
who actually entered or answered a survey.

### Survey routing logic

Use for rejection, conditional branches, option conditions, screen jumps,
section jumps, and survey termination.

`logic`, `logic_rule`, `logic_rule_condition`

### Saved reporting

Use for saved report definitions, chart configuration, aggregation
configuration, filters, and exported artifacts.

`charts_reports`, `charts_tabs`, `charts`, `chart_report_filter`,
`chart_settings_template`, `aggregations`, `aggregation_report_tabs`, `exports`

Do not use reporting configuration instead of raw responses when the user asks
for a result calculated from participants' answers.

### Tenancy and scoping

`client_id` and `organization_id` are supplied on every request.
`client`/`account` sit above `organization` in the tenancy chain and are used
to verify that requested surveys remain inside the authorized tenant — see
"Tenancy scoping chain" in section 3 for the exact columns and a worked query.

`client`, `account`, `organization`, `customer`

### Sharing, files, billing, and system tables

Use only when explicitly requested and authorized. Their exact columns are
not listed here — confirm via `information_schema.columns` before querying.

---

## 3. Exact core schema and canonical survey routes

### Survey definition

```text
survey:
  id
  title
  "internalName"
  organization_id
  state
  type
```

The current survey row is scoped by:

```sql
survey.id = '{survey_id}'
```

### Tenancy scoping chain

`{client_id}` and `{organization_id}` are supplied on every request. The real
chain from survey up to client is:

```text
survey.organization_id
  → organization.id
organization.account_id
  → account.id
account.client_id
  → client.id
```

There is no direct `organization.clientId` or `survey.ownerId` column —
tenancy must go through `account`. Verify a survey belongs to the given
organization and client with:

```sql
SELECT s.id
FROM survey AS s
JOIN organization AS o
  ON o.id = s.organization_id
JOIN account AS a
  ON a.id = o.account_id
WHERE s.id = '{survey_id}'
  AND o.id = '{organization_id}'
  AND a.client_id = '{client_id}';
```

To find other surveys under the same client/organization (e.g. for an
explicit cross-survey comparison request):

```sql
SELECT s.id, s.title, s."createdAt"
FROM survey AS s
WHERE s.organization_id = '{organization_id}'
ORDER BY s."createdAt";
```

Titles are not unique identifiers: the same test is frequently duplicated as
a draft, a "Copy - " prefixed re-run, or a deprecated shell alongside the
survey that was actually completed. When a name/date match returns more than
one candidate, do not pick the first or most literal title match — check
which candidate has real response data and prefer that one:

```sql
SELECT s.id, s.title, s.state,
       (SELECT count(*) FROM enrollment e WHERE e.survey_id = s.id) AS enrollments,
       (SELECT count(*) FROM answer a
          JOIN enrollment e ON e.id = a.enrollment_id
          WHERE e.survey_id = s.id) AS answers
FROM survey AS s
WHERE s.organization_id = '{organization_id}'
  AND s.title ILIKE '%<matched name>%'
ORDER BY answers DESC;
```

If every same-named candidate has zero enrollments/answers, only then report
that no comparable historical data exists.

`client.name` holds the client's display name (not `client.title`).

### Cross-survey metric comparison

Once two or more surveys are resolved, the same question has a *different*
`question.id` in each survey — `question_library_item` links questions back
to a shared library template, but is sparsely populated and cannot be relied
on as a general cross-survey join key. Match the equivalent question
per survey by `"typeOfQuestion"` and `prompt` text instead, then aggregate
independently per survey before comparing:

```sql
WITH matched AS (
    SELECT id AS question_id, "surveyId" AS survey_id, prompt
    FROM question
    WHERE "surveyId" IN ('{survey_id}', '<other_survey_id>')
      AND "typeOfQuestion" = 'line-scale'
      AND prompt ILIKE '%overall%'
      AND prompt ILIKE '%lik%'
)
SELECT
    m.survey_id,
    m.prompt AS matched_prompt,
    COUNT(DISTINCT a.enrollment_id) AS respondents,
    AVG((aqo."answerData" ->> 'optionAnswer')::numeric) AS avg_score
FROM matched AS m
JOIN answer AS a ON a.question_id = m.question_id
JOIN answered_question_options AS aqo ON aqo.answer_id = a.id
GROUP BY m.survey_id, m.prompt;
```

The two separate `ILIKE` conditions above are deliberate, not one concatenated
`'%overall%lik%'` pattern — the real wording is often "How much do you LIKE or
DISLIKE this product OVERALL?", where "like" comes *before* "overall", so a
single ordered pattern (whether `'%overall liking%'` or `'%overall%lik%'`)
silently fails to match it and can instead match an unrelated question that
happens to satisfy the literal order (e.g. an *appearance*-liking question
worded "Overall, how much do you LIKE or DISLIKE the APPEARANCE..."). Matching
each keyword as its own `ILIKE` condition is order-independent and avoids
this.

Before trusting the comparison, confirm each survey actually matched exactly
one question with the expected `typeOfQuestion`, and always include the
matched `prompt` text per survey alongside the result — an exact-one-row match
is not proof of a *correct* match, since near-duplicate wording (e.g. an
appearance/quality/overall variant of the same "liking" concept, or a
rephrased question in a later wave) can produce a confident wrong match, not
just a false non-match. If a survey has zero or multiple candidates, or the
matched prompt text does not actually correspond to the concept asked about,
say so explicitly instead of silently picking one.

### Questions

```text
question:
  id
  prompt
  "typeOfQuestion"
  "surveyId"
  "screenId"
  "sectionId"
  "isRequired"
  settings
```

Scope directly:

```sql
question."surveyId" = '{survey_id}'
```

### Question options

```text
question_option:
  id
  question_id
  label
  type
  "order"
  analytical_value
  "optionSettings"
```

Canonical route:

```text
question_option.question_id
  → question.id
  → question."surveyId"
```

### Products

```text
product:
  id
  name
  "surveyId"
  "blindingNumber"
  "productIndex"
```

Scope directly:

```sql
product."surveyId" = '{survey_id}'
```

### Enrollments

```text
enrollment:
  id
  survey_id
  user_id
  panelist_id
  panel_code_id
  enrollment_status
```

One enrollment is the default respondent/participation unit.

Scope directly:

```sql
enrollment.survey_id = '{survey_id}'
```


### Registered users

```text
"user":
  id
  gender
  country
  city
  language
```

`user` is a PostgreSQL keyword-like identifier in this schema; quote the table
name as `"user"`.

Canonical route for registered respondent attributes:

```text
enrollment.user_id
  → "user".id
```

A profile attribute is available only for enrollments with a non-null
`user_id`. When profile coverage matters, return both the number of matched
registered users and the total number of survey enrollments.

### Answers

```text
answer:
  id
  enrollment_id
  question_id
  product_id
  value
  "isSkipped"
  "timeToAnswer"
```

`answer` has no `survey_id`. Always scope it through enrollment:

```text
answer.enrollment_id
  → enrollment.id
  → enrollment.survey_id
```

### Structured option answers

```text
answered_question_options:
  id
  answer_id
  question_option_id
  question_set_id
  question_pair_id
  matrix_row_option_id
  "answerData"
```

Canonical route:

```text
answered_question_options.answer_id
  → answer.id
  → enrollment.id
  → enrollment.survey_id
```

Selected option label route:

```text
answered_question_options.question_option_id
  → question_option.id
```

### Other canonical routes

```text
question_group    → question → survey
question_set      → question → survey
question_pair     → question → survey

charts
  → charts_tabs
  → charts_reports
  → survey

logic_rule_condition
  → logic_rule
  → logic
  → survey

survey
  → survey_panel
  → panel
  → panel_panelist
  → panelist
```

---

## 4. Minimal table bundles

Use the smallest bundle that can answer the request.

- Survey metadata: `survey`
- Tenancy verification / cross-survey lookup: `survey`, `organization`, `account`, `client`
- Respondent demographics (gender, age, country, etc. as answered in-survey):
  `question`, `answer`, `answered_question_options`, `enrollment`
- Registered-user profile fallback (rare — see section 2): `enrollment`, `user`
- Questionnaire: `question`, `question_option`
- Questionnaire hierarchy: add `question_screen`, `question_section`
- Basic scalar response analysis:
  `enrollment`, `answer`, `question`, optionally `product`
- Categorical, multi-select, matrix, or ranking analysis:
  `enrollment`, `answer`, `answered_question_options`,
  `question`, `question_option`, optionally `product`
- Paired/set questions: add `question_pair`, `question_set`
- Panel participation:
  `survey_panel`, `panel`, `panel_panelist`, `panelist`,
  `panel_code`, `survey_panel_stats`, `enrollment`
- Logic:
  `logic`, `logic_rule`, `logic_rule_condition`,
  `question`, `question_option`, `question_screen`
- Saved reports:
  `charts_reports`, `charts_tabs`, `charts`, `aggregations`, `exports`

Query `information_schema.columns` once for all referenced tables only when
this file does not provide the required exact column.

---

## 5. Query-shape rules

### Choose the authoritative source before matching text

Classify the requested concept before writing joins.

1. **Survey response, including demographics:** liking, purchase intent,
   selected option, open text, matrix score, ranking, gender, age, or
   anything the respondent answered inside the survey. Query `answer` and,
   when required, `answered_question_options`. This is the default for
   respondent attributes — see "Respondent demographics" in section 2.
2. **Direct entity attribute:** enrollment status, or another documented
   normalized column that is not answered inside the questionnaire. Query the
   owning table directly.
3. **Configuration:** questionnaire structure, logic, products, charts, or
   panels. Query the corresponding definition tables.

For “How many respondents are female?”, first resolve the matching question,
then aggregate its answers:

```sql
WITH gender_q AS (
    SELECT id
    FROM question
    WHERE "surveyId" = '{survey_id}'
      AND prompt ILIKE '%gender%'
),
scoped AS (
    SELECT
        a.enrollment_id,
        LOWER(TRIM(COALESCE(aqo."answerData" ->> 'optionAnswer', a.value))) AS gender
    FROM answer AS a
    JOIN enrollment AS e ON e.id = a.enrollment_id
    LEFT JOIN answered_question_options AS aqo ON aqo.answer_id = a.id
    WHERE e.survey_id = '{survey_id}'
      AND a.question_id IN (SELECT id FROM gender_q)
)
SELECT
    COUNT(DISTINCT enrollment_id)
        FILTER (WHERE gender = 'female') AS female_respondents,
    COUNT(DISTINCT enrollment_id) AS answered_enrollments
FROM scoped;
```

If no question matches (`gender_q` is empty), only then fall back to
`enrollment.user_id → "user".gender`, and report that the fallback route was
used since it will typically cover few or zero respondents.

For an unfamiliar option encoding, inspect the distribution in one
survey-scoped query before assuming a label:

```sql
SELECT
    COALESCE(NULLIF(LOWER(TRIM(COALESCE(aqo."answerData" ->> 'optionAnswer', a.value))), ''), '<missing>') AS value,
    COUNT(DISTINCT a.enrollment_id) AS enrollments
FROM answer AS a
JOIN enrollment AS e ON e.id = a.enrollment_id
LEFT JOIN answered_question_options AS aqo ON aqo.answer_id = a.id
WHERE e.survey_id = '{survey_id}'
  AND a.question_id = '<demographic_question_id>'
GROUP BY 1
ORDER BY 1;
```

Prefer one query that resolves the candidate question/option and returns the
requested aggregate together with the matched prompt or label.

A zero result from a free-text or option-label filter is a diagnostic signal:
inspect distinct stored values or reconsider the source table before returning
zero as the final answer.

### Summary versus raw response data

When the user asks to summarize, describe, compare, average, count, score, or
analyze responses, return aggregates rather than raw answer rows. Do not return
full JSON arrays of `answer`, `answered_question_options`, UUIDs, or per-
respondent records unless the user explicitly asks for raw data, an export, or
individual responses.

For response summaries, choose the grouping grain that matches the question:
survey/product summaries group by product and question; structured attribute
questions group by product, question, and `question_option.label`; paired
statistics group internally by `enrollment_id` or `(enrollment_id, product_id)`
but report only the final statistic and observation count. Include useful
aggregate fields such as response count, skipped count, numeric count, min,
average, max, and labels/prompts needed to interpret the result.

### Respondent counts

Default to:

```sql
COUNT(DISTINCT e.id)
```

Do not default to distinct `user_id`; an enrollment may be associated with a
panelist or code and may have no user.

Filter `enrollment_status` only when the user specifically means completed,
active, or expired participants.

### Scalar versus structured answers

Route by `question."typeOfQuestion"` — each type stores its response value in
a different place. Resolve the question's type first, then read the matching
path:

| Question type (examples) | Where the value lives | How to read it |
|---|---|---|
| `open-answer`, `email`, `contact-information`, `multiple-open-answer` | `answer.value` | text, read directly |
| `multiple-choice`, `tcata` (selected option) | `answered_question_options.question_option_id → question_option` | `question_option.label` for display; `question_option.analytical_value` for a numeric code |
| `line-scale`, `vertical-rating`, `time-intensity-slider` (numeric rating) | `answered_question_options."answerData" ->> 'optionAnswer'` | `CAST(NULLIF(... , '') AS numeric)`; do not use `answer.value` when it is blank |
| `matrix` row (which statement/row) | `answered_question_options.matrix_row_option_id → question_option` | `question_option.label` |
| `matrix` column (the score for that row) | `answered_question_options.question_option_id → question_option.analytical_value` | already numeric, no cast needed |
| `ranking` (position of a ranked item) | `answered_question_options."answerData" ->> 'rank'` | cast to integer |
| `paired-questions` | `answered_question_options.question_pair_id → question_pair` | join for the compared pair |
| question sets/trays (e.g. MaxDiff-style combinations) | `answered_question_options.question_set_id → question_set` | join for the set/tray definition |

For response summaries, distributions, averages, min/max, correlations, and
product-level scoring, check both scalar and structured answer storage before
reporting that numeric values are unavailable — a question's stored path is
determined by its type, not by assumption.

`answer.product_id → product.id` gives product-specific response identity; do
not infer a product from prompt or option text when `product_id` is present.

### Paired respondent data

Prefer `run_survey_stats` (pearson/spearman) for this — see "Statistical
analysis" above. Use the raw-SQL approach below only when `run_survey_stats`
doesn't support the needed test or errors.

For correlation, paired tests, or comparisons between two questions, produce
one row per analytical unit, usually `enrollment_id` or
`(enrollment_id, product_id)`.

First resolve the question IDs and their `typeOfQuestion`. Use conditional
aggregation rather than joining unaggregated answer rows. When pairing
structured rating questions, join `answered_question_options` and extract
`"answerData" ->> 'optionAnswer'`:

```sql
SELECT
    a.enrollment_id,
    a.product_id,
    MAX(CASE WHEN a.question_id = '<q1>'
             THEN (aqo."answerData" ->> 'optionAnswer')::numeric END) AS value_1,
    MAX(CASE WHEN a.question_id = '<q2>'
             THEN (aqo."answerData" ->> 'optionAnswer')::numeric END) AS value_2
FROM answer AS a
JOIN enrollment AS e
  ON e.id = a.enrollment_id
JOIN answered_question_options AS aqo
  ON aqo.answer_id = a.id
WHERE e.survey_id = '{survey_id}'
  AND a.question_id IN ('<q1>', '<q2>')
  AND aqo."answerData" ? 'optionAnswer'
GROUP BY a.enrollment_id, a.product_id;
```

For scalar numeric questions, use `answer.value` instead, after validating that
the selected question's stored values are numeric and nonblank.

Remove `product_id` from the grouping only when the questions are not
product-specific.

### Demographic filters on survey answers

For prompts combining demographics with survey-response metrics, such as age
plus liking, correlation, impact, purchase intent, or product scoring, do not
assume demographic stored values have literal meaning.

Before computing the final statistic, resolve:

- the demographic question ID;
- the metric question IDs;
- the distinct stored values for the demographic question;
- and any available option/code mapping.

If no code mapping exists, report the stored values used rather than inventing
labels. For age filters such as "over 18", only apply stored values like `'2'`,
`'3'`, etc. after confirming they represent adult age groups or after clearly
stating that the database only exposes stored age-group codes.

### Categorical encodings

Do not assume stored value `'1'`, `'2'`, etc. has a natural-language meaning.
Resolve it using `question_option`, `analytical_value`, documented JSONB, or a
survey-specific codebook.

If no mapping exists, report the stored values without inventing labels.

### Numeric conversion

Numeric responses may be stored as text in `answer.value` or as structured
JSONB in `answered_question_options."answerData"`. Restrict to the intended
question before casting, validate that relevant values are numeric, and do not
silently discard malformed values. If `answer.value` is blank for a rating or
scored question, inspect `answerData` keys such as `optionAnswer` before
concluding that no numeric values exist.

### Prevent row multiplication

- Aggregate one-to-many child rows before joining when possible.
- Count distinct entity IDs after joins that may duplicate rows.
- Use separate aggregate CTEs for independent survey-level metrics instead of
  a large star join.
- A result count much larger than the survey's enrollment count is a warning
  that the query grain is wrong.

For a multi-metric survey overview, use independent scalar subqueries rather
than one join across `enrollment`/`question`/`product`/`answer` — a star join
Cartesian-multiplies rows and can silently return an inflated or zeroed count:

```sql
SELECT
    s.title, s.state, s."isActive", o.name AS organization,
    (SELECT count(*) FROM enrollment e WHERE e.survey_id = s.id) AS enrollments,
    (SELECT count(*) FROM enrollment e
       WHERE e.survey_id = s.id AND e.enrollment_status = 'completed') AS completed,
    (SELECT count(*) FROM question q WHERE q."surveyId" = s.id) AS questions,
    (SELECT count(*) FROM product p WHERE p."surveyId" = s.id) AS products,
    (SELECT count(*) FROM answer a JOIN enrollment e ON a.enrollment_id = e.id
       WHERE e.survey_id = s.id) AS answers
FROM survey s
JOIN organization o ON s.organization_id = o.id
WHERE s.id = '{survey_id}';
```

### Statistical analysis

Use SQL for filtering, joining, grouping, contingency counts, and simple
aggregates (counts, averages, distributions).

For correlation (Pearson/Spearman), product comparison (ANOVA/Tukey), or
significance testing (chi-square), prefer the `run_survey_stats` tool over
raw SQL: resolve the question(s) by meaning first with a SQL query scoped to
`question."surveyId" = '{survey_id}'`, then call the tool with that
`question_id` (and `reference_question_id` for pearson/spearman/penalty). It
returns respondent counts and p-values directly — do not recompute
significance by hand when the tool already provides it.

There is no Python execution tool available to this agent. Fall back to raw
SQL for a statistical test only when `run_survey_stats` doesn't cover it, it
errors, or the request is a simple descriptive aggregate — Postgres itself
exposes aggregate functions such as `corr()` and `regr_slope()` for that case.

If `run_survey_stats` reports every requested test as unsupported or
insufficient for the matched question(s), do not stop at reporting the test
as uncomputable — query the underlying counts or contingency table with SQL
so the user still gets the real numbers, with a note that the requested test
itself could not be run.

---

## 6. Stability boundary

This file intentionally excludes row counts, seed-specific information,
claims that a table is empty, and current data-population assumptions — those
drift with the data and must be checked live, not assumed from this map.

This file is the only schema reference the agent has; there is no separate
detail tool or catalog to fall back on. For an uncommon table, column,
foreign key, nullability rule, enum, or JSONB shape not covered here, query
`information_schema.columns`/`information_schema.tables` (or a scoped `SELECT
... LIMIT 1` on the target table) directly rather than guessing — and trust
that live result over any assumption, including anything in this file that
may have drifted from the actual schema.
