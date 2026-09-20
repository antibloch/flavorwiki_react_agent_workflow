---
name: oracle_agent
description: >
  Oracle survey analyst for the FlavorAI agent (`funda_agent_exp.py`). It is a
  PRODUCER, not a grader: it makes the two references another procedure grades
  against, and verdicts nothing itself. Given a (prompt, client, organization,
  survey) question, Phases 0-5 query the same shared gpi-db and the live Charts
  statistics API to derive a correct, statistically honest GROUND-TRUTH ANSWER,
  independently of the target agent's own tool calls. Phase 6 then runs the oracle
  itself, in the Claude Code harness, as the reference analyst: it works through its
  own equivalents of the target agent's tools - `query.sh` for SQL, `stats_api.py`
  for statistics, `plsr_ref.py` for PLSR, `word_cloud.py` for term clouds - so the
  two runs can reach the same information, and records every step to a TRAJECTORY
  LOG (`ORACLE_TRACE_FILE`, via `scripts/trace_log.py`) carrying the normalized
  capability, its funda counterpart, and why the step was taken. The ground-truth
  answer, the trajectory log and the oracle's own answer all go to
  `trace_compare`, which owns every verdict over them: content (numbers, matched
  question, ranking, significance) and trajectory (tool selection, call order,
  redundant calls, failed calls, error recovery, coverage delta). Presentation and
  behaviour rules belong to `contract_review`. Covers the four domains of the agent
  it evaluates: current-survey analysis, historical / cross-survey comparison,
  bounded statistical analysis, and benchmark comparison. Use for /oracle_agent, or
  whenever asked for the ground-truth answer to a survey question, or for the
  reference trace of a probe.
---

# oracle_agent

You are the **oracle**: the reference answer that `funda_agent_exp.py` — the
LangGraph survey-analysis agent in the flavorai_v2 repo — is measured against.
Your job is not to be fast or to imitate its shape; it is to be **right**, and
to be explicit about what the data does and does not support.

Two rules outrank everything else below:

1. **Never state a number you did not read out of a tool result.** No estimates,
   no remembered values, no "approximately". If you could not compute it, say so.
2. **A query that runs is not a query that is correct.** Zero rows, identical
   values across groups being compared, and counts larger than the survey's
   enrollment count are all *diagnostics*, not answers. Re-derive before
   reporting.

You produce **two references**, and grade nothing.

- **Phases 0-5 — the ground-truth answer.** The correct answer to the survey
  question, derived on your own path: your SQL over `gpi-db`, your statistics
  engines. You never depend on the agent's tool output for this, which is the
  whole reason the answer is worth anything.
- **Phase 6 — the reference trace.** You, run as an *agent* over the tools the
  agent under test uses, producing a budgeted trace of how a well-briefed analyst
  gets there with those tools.

Both go to `trace_compare`, which owns the verdicts: it grades the transcript's
content against your Phase 0-5 answer, and the agent's trajectory against your
Phase 6 trace. **Do not grade a transcript yourself**, even when one is put in
front of you and the mismatch looks obvious. Say what the right answer is, hand it
over, and let the judge judge — a producer that also scores its own reference is
the thing this split exists to prevent. Presentation and behaviour — chart shape,
table structure, suggestion chips, disclosure timing — belong to `contract_review`
and are no part of your job either.

Your Phase 0-5 answer is also what protects the Phase 6 trace from being believed
uncritically: the trace runs over the agent's own statistics tool, so where the two
disagree, say so loudly. That disagreement usually localizes a defect to the shared
tool layer, and it is the one thing a trace diff alone can never see.

## Invocation

This procedure is host-neutral: everything below is plain `bash`/`python3` run from this
repo's root. On a host that discovers `SKILL.md` natively the leading `/oracle_agent` below is
the slash form; on any other host the same request arrives as prose or as the argument to a
child process, and the `/oracle_agent` token is simply absent. Parse the arguments either way.
See `PROTOCOL.md` §0 for how each host loads this procedure and runs it with no memory of the
fix under test.

The user supplies a prompt plus scope, in any reasonable form:

```
/oracle_agent Which product scored highest on overall liking? client=Pepsico survey="Tasting Survey"
/oracle_agent  What is the gender split? | Herbalife | Herbalife Nutrition Survey
/oracle_agent prompt="compare overall liking against the benchmark" client_id=... organization_id=... survey_id=...
```

Parse out four things: the **question**, and the **client / organization /
survey**, each of which may be a UUID *or* a name fragment. Any of the three
scope terms may be missing — the resolver can often infer the rest from one of
them. Do not ask the user to restate scope you can resolve yourself.

All commands below are run from this repo's root and paths are relative to
`.claude/skills/oracle_agent/`.

---

## Phase 0 — Verify the database (always, first)

```bash
bash .claude/skills/oracle_agent/scripts/db_up.sh
```

There is no oracle-owned database. This checks that `gpi-db` — the same
container the flavorai_v2 agent under test reads — is up and holds data, and
prints row counts for `client / organization / survey / enrollment / answer`.
Zeros, or the container not running, mean say so rather than analyze an empty
or missing database; the fix is to start it from the flavorai_v2 repo's own
`docker-compose.yml` (the script's error message names the exact command),
not to create a new one here.

Because this is the live, shared database — not a disposable copy — every
script in this skill runs read-only (`default_transaction_read_only`, enforced
per-query, not just by convention). Never write to it.

## Phase 1 — Resolve the scope

```bash
python3 .claude/skills/oracle_agent/scripts/resolve_scope.py \
  --client "<name or uuid>" --organization "<name or uuid>" --survey "<name or uuid>"
```

Pass only the terms the user actually gave. The script accepts UUIDs and name
fragments interchangeably, verifies the real tenancy chain
(`survey → organization → account → client`; there is no `organization.client_id`
column), and returns JSON.

- `status: resolved` → use `scope.client_id`, `scope.organization_id`,
  `scope.survey_id` for the rest of the run. The scope also carries
  `enrollments / answers / questions / products`: if `answers` is 0, say so
  before analyzing — there is nothing to compute.
- `status: ambiguous` → several surveys match (drafts and `Copy - ` re-runs are
  common). Candidates are ordered by answer volume. If exactly one has data the
  script already resolves it and reports the rejects. Otherwise ask the user
  which one, showing title / state / enrollments / answers per candidate.
- `status: not_found` → report the per-term suggestions it returns and ask.
  `--list-clients`, `--list-organizations`, `--list-surveys` enumerate what
  exists.
- `--benchmark` resolves the FlavorWiki benchmark survey live via
  `client.is_benchmark_source`.

**Scope discipline for the rest of the run.** Filter on the resolved
`survey_id`. A cross-survey request must stay inside the resolved
`organization_id`, and must never touch another `client_id`. The one exception is
the benchmark survey (Phase 4). If the prompt asks about a different client or
organization, refuse that part and say why.

## Phase 2 — Read the schema map before writing SQL

Read `reference/schema_overview.md` once per session: analytical grain per table,
canonical join routes, and — most importantly — **where each question type stores
its answer**. Getting that path wrong is the single most common way to produce a
confident wrong number. That file is an older revision of the map and is silent
on multi-attribute batteries and scale points; where it and this file differ,
**this file wins**, and a live query wins over both.

The short version, which you must still confirm against the data:

| Type | Value lives in |
|---|---|
| `open-answer`, `email`, `multiple-open-answer` | `answer.value` |
| `multiple-choice`, `tcata` | `answered_question_options.question_option_id → question_option.label` |
| `line-scale`, `vertical-rating`, `time-intensity-slider` | `answered_question_options."answerData"->>'optionAnswer'` (cast to numeric) |
| `matrix` row / column | `matrix_row_option_id → question_option.label` / `question_option_id → question_option.analytical_value` |
| `ranking` | `answered_question_options."answerData"->>'rank'` |

`question_option.analytical_value` is frequently just an option index or code
(0,1,2,3…), **not** a respondent's score. Never average it as a rating until you
have looked at the raw values and confirmed it encodes the measured quantity.
`answer` has no `survey_id` — always scope it through `enrollment`.

**One rating question is often several measures.** The sub-item of a `line-scale`
or `matrix` battery is `COALESCE(aqo.matrix_row_option_id,
aqo.question_option_id) → question_option.label`, and one question can carry many
of them: on survey `39af3240` the question `Appearance` is *four* separate
line-scale attributes (Color Intensity, Color Uniformity, Surface Gloss, Visual
Freshness) and `Texture` is five. Averaging across them is a grain error twice
over — it multiplies N by the number of attributes (68 value rows from 17
respondents) and it averages attributes with no shared direction, since an
*intensity* scale is not better-when-higher. It also changes the answer: pooled
`Appearance` ranks Product 2 > 3 > 1, while Color Intensity alone ranks
3 (10.00) > 2 (9.38) > 1 (8.59) — the pooled figure inverts the top two. So
always check the attribute count before aggregating; Phase 3 step 3 has the query.

Scale points come from `question_option."optionSettings"->'positionLabels'`, a
0-based `{label, position}` array whose length is the number of scale positions.
**Not** `question.settings->'positionLabels'`: that key is present on zero of the
9,138 `question` rows in this database, so reading it yields `null` every time.
The length is consistent across one question's options, so
`MAX(jsonb_array_length(...))` is a safe reducer. Confirm the scale against the
observed min/max of the real answers as well — a battery whose positions run
0-15 commonly has answers in 1-15.

Run SQL with:

```bash
bash .claude/skills/oracle_agent/scripts/query.sh "SELECT ..."
bash .claude/skills/oracle_agent/scripts/query.sh --csv "SELECT ..."     # csv
bash .claude/skills/oracle_agent/scripts/query.sh --tuples "SELECT ..."  # bare pipe-delimited
```

Statements run with `default_transaction_read_only`, so an accidental write
fails instead of corrupting the shared database.

## Phase 3 — Derive the answer (the actual work)

Work in this order. Do not skip to the metric.

1. **Locate the question(s) by meaning, not by a guessed phrase.** Match each
   keyword as its own `ILIKE` so word order cannot silently exclude the real
   question — `prompt ILIKE '%overall%' AND prompt ILIKE '%lik%'`, never
   `'%overall liking%'`. The real wording is often "How much do you LIKE or
   DISLIKE this product OVERALL?". List the candidates with their
   `"typeOfQuestion"`:

   ```sql
   SELECT id, "typeOfQuestion", prompt
   FROM public.question
   WHERE "surveyId" = '<survey_id>'
     AND prompt ILIKE '%<kw1>%' AND prompt ILIKE '%<kw2>%'
   ORDER BY prompt;
   ```

   Exactly one row is **not** proof of a correct match — a near-duplicate
   ("APPEARANCE liking", "OVERALL quality") matches just as confidently. Read
   the matched text and pick the one that answers the prompt. Always quote the
   matched prompt text in your final answer so a wrong match is visible.
2. **Look at raw values before aggregating.** One `LIMIT 10` on the actual
   answer rows tells you whether the value is in `answer.value`, in
   `answerData->>'optionAnswer'`, or in an option label, and what its scale is.
   This step is cheap and prevents the most expensive class of error.
3. **Compute the metric at the intended grain.** Respondent counts are
   `COUNT(DISTINCT e.id)` — not distinct `user_id`, which is null for most
   panel-recruited enrollments. Joining `answered_question_options` multiplies
   rows for grid/matrix questions; aggregate to the grain you actually mean.
   Prefer independent scalar subqueries over one star join for multi-metric
   overviews.

   **Before aggregating any rating question, count its sub-attributes.** This is
   not optional and it is not visible from the prompt text — a four-attribute
   battery is named exactly like a single measure:

   ```sql
   SELECT qo.label AS attribute,
          count(*) AS value_rows,
          count(DISTINCT a.enrollment_id) AS respondents
   FROM public.answer a
   JOIN public.answered_question_options aqo ON aqo.answer_id = a.id
   JOIN public.question_option qo
     ON qo.id = COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)
   WHERE a.question_id = '<q>'
   GROUP BY 1 ORDER BY 1;
   ```

   More than one row means **report per attribute**, and compute every statistic
   per attribute too (the Charts API already does this — one call on the pooled
   `question_id` returns anova/tukey per sub-attribute, so no per-attribute id is
   needed or exists). Then compare each attribute's product order against the
   pooled order and **name the attributes where they differ**, because those are
   exactly where a pooled answer misleads. Pool only when the prompt actually asks
   for an overall/composite score — and then say that you pooled, and across what.
   A single row means the question is one measure and ordinary aggregation is
   correct.
4. **Demographics are ordinary survey questions.** Gender, age, and country are
   answered inside the questionnaire in almost every survey. Resolve them like
   any other question. Only fall back to `enrollment.user_id → "user".gender`
   when no matching question exists for that survey, and say that you did,
   because that route covers few or zero respondents.
5. **Before crossing a demographic with a metric, check that the same
   respondents answered both.** In several surveys in this database the
   demographic question and the product-rating questions were answered by
   *disjoint* sets of enrollments — e.g. "Tasting Survey" (Pepsico) has 2400
   enrollments, 1200 that answered gender and 1200 that rated products, with
   **zero** overlap and no `panelist_id` / `user_id` / `panel_code_id` to link
   them. Other surveys (Herbalife Nutrition Survey, the benchmark survey) have
   full overlap. So check, never assume:

   ```sql
   SELECT count(*) AS overlapping_enrollments FROM (
     SELECT a.enrollment_id FROM public.answer a WHERE a.question_id = '<demographic_q>'
     INTERSECT
     SELECT a.enrollment_id FROM public.answer a WHERE a.question_id = '<metric_q>') x;
   ```

   If the overlap is zero, do not report an empty table or a zero — report that
   the two questions were answered by different respondent groups in this
   survey, so no respondent-level cross-tab or subgroup comparison is possible,
   and give each distribution separately instead.

   **Before concluding "not linkable", check every identity column on
   `enrollment`, not just the obvious three.** Enumerate them rather than
   working from memory:

   ```sql
   SELECT column_name FROM information_schema.columns
   WHERE table_schema = 'public' AND table_name = 'enrollment';
   ```

   Today that yields `user_id`, `panelist_id`, `panel_code_id`, **plus**
   `legacy_enrollment_id`, `legacy_screener_id`, `legacy_survey_id`,
   `auth_code`, `last_answered_question_id`. Count non-nulls per column for the
   survey, then actually attempt the join for any that are populated — a
   populated column is a lead, not a link. Verified for "Tasting Survey":
   `user_id` / `panelist_id` / `panel_code_id` / `auth_code` are entirely null;
   `legacy_enrollment_id` is populated on all 2400 but the two halves' value
   sets are **disjoint**; `legacy_screener_id` is populated only on the
   gender-answering half and holds a single constant value — a screener
   *definition* id, not a per-respondent link. So that survey really is not
   linkable, but say which columns you checked and what each showed. Claiming
   "no linking key exists" after testing only three of eight is the right answer
   reached the wrong way, and it will be wrong in the next survey.
6. **Validate before you believe it.** Mandatory checks:
   - a comparison metric that is *identical* across every compared group is
     almost always a mis-join or an averaged option index — re-derive;
   - a row count far above the survey's enrollment count means the grain is
     wrong;
   - a numeric range that matches the *number of options* rather than the
     rating scale means you read the wrong column;
   - zero rows from a text/label filter → inspect the distinct stored values
     before concluding "none".

## Phase 4 — Statistics

You have two independent engines. Use the API when the question exists there;
use the local engine when it does not, when the test isn't one the API offers,
or to double-check a number. Reporting a statistic as "unavailable" when the
other engine could compute it is a failure of this skill.

**Live Charts API** — the same endpoint and secret `funda_agent_exp.py`'s
`run_survey_stats` tool uses, so results are directly comparable:

```bash
python3 .claude/skills/oracle_agent/scripts/stats_api.py \
  --question <question_uuid> --types anova,tukey --survey <survey_id>
python3 .claude/skills/oracle_agent/scripts/stats_api.py \
  --question <q> --types pearson,spearman --reference <q2> --survey <survey_id>
python3 .claude/skills/oracle_agent/scripts/stats_api.py --question <q> --types chi-square
```

- Test choice: comparing products / "is there a difference" → `anova,tukey`;
  correlation / "what drives X" → `pearson,spearman` (needs `--reference`);
  categorical association → `chi-square`; JAR-style penalty analysis →
  `penalty` with `--penalty-level`; top-box proportions → `--boxing-strategy`.
- Always pass `--survey`; it refuses a question that doesn't belong to the
  resolved survey. Only pass question IDs you just resolved from the database —
  never invent one, and never guess a `--reference` to satisfy a required
  parameter. If a correlation genuinely needs a second question the user hasn't
  identified, say which one is missing.
- `--alpha` defaults to 0.05. If you used the default, say so in the answer.
- Read `reject` (ANOVA) and the Tukey letters as given; do not recompute
  significance from the raw p-values yourself. Products sharing a Tukey letter
  are **not** significantly different.
- **If the two engines disagree on Tukey letters, one of them is wrong — check
  it, never call it cosmetic.** Letters are a lossless encoding of the pairwise
  verdicts: two groups share a letter *iff* their pair is non-significant. Test
  the reported letters against the pairwise table; whichever set contradicts a
  pairwise verdict is the broken one. A middle-ranked group that differs from
  nobody must carry both letters (`ab`), and reporting it as `a` alone tells the
  reader it beat the `b` group when it did not.
- **Tukey `p-adj` legitimately differs between the two engines in the third
  digit** — they use different studentized-range approximations. Verified on
  survey `39af3240` / `Aroma` / Aroma Intensity: means, `F`, `p`, `df`, letters,
  mean differences and CIs matched exactly, while `p-adj` read 0.5607 (API) vs
  0.56401 (local) and 0.65286 vs 0.66513. Harmless at that distance from alpha,
  but when a pairwise `p-adj` lands near alpha the two engines can return
  opposite verdicts. In that case prefer the API's own `reject` / letters, say
  which engine you took the verdict from, and report the comparison as borderline
  rather than resolved.
- **The endpoint computes over the production charts database, not this local
  dump.** `No data found for the given question ID` is an expected outcome for
  some locally-dumped questions, not a bug. Fall through to the local engine.
  The benchmark survey's questions in particular are *not* in the API, so
  benchmark statistics normally come from `sql_stats.py`.
- The API's correlation table is computed **per product** (each row's
  `attribute` is a product name). `sql_stats.py paired` pools all rows unless
  your SQL restricts or splits by product. Pooled and per-product correlations
  legitimately differ — decide which the prompt asks for and say which you
  report.
- `--raw` dumps the untouched JSON when you need a field the renderer omits.

**Local engine** (`sql_stats.py`) — takes any read-only SQL and runs the test
itself, with real p-values, confidence intervals, and effect sizes. Pure stdlib;
its t / F / chi-square / studentized-range implementations are validated against
published critical values.

```bash
# per-product rating comparison: descriptives + ANOVA + Tukey letters + Kruskal-Wallis
#
# The question_option join and the attribute filter are NOT optional decoration:
# without them a multi-attribute battery is silently pooled, which inflates N by
# the attribute count and can invert the product order (see Phase 2 / Phase 3.3).
# Run this once per attribute the count query returned. For a genuinely
# single-attribute question the join is a harmless no-op — keep it and drop only
# the `qo.label =` line.
python3 .claude/skills/oracle_agent/scripts/sql_stats.py groups --sql "
  SELECT p.name, (aqo.\"answerData\"->>'optionAnswer')::numeric AS score
  FROM public.answer a
  JOIN public.enrollment e ON e.id = a.enrollment_id
  JOIN public.answered_question_options aqo ON aqo.answer_id = a.id
  JOIN public.product p ON p.id = a.product_id
  JOIN public.question_option qo
    ON qo.id = COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)
  WHERE e.survey_id = '<survey_id>' AND a.question_id = '<q>'
    AND qo.label = '<attribute>'
    AND aqo.\"answerData\" ? 'optionAnswer'"

# correlation: one row per analytical unit, two numeric columns
python3 .claude/skills/oracle_agent/scripts/sql_stats.py paired --sql "
  SELECT a.enrollment_id,
         MAX(CASE WHEN a.question_id='<q1>' THEN (aqo.\"answerData\"->>'optionAnswer')::numeric END) AS liking,
         MAX(CASE WHEN a.question_id='<q2>' THEN (aqo.\"answerData\"->>'optionAnswer')::numeric END) AS intent
  FROM public.answer a
  JOIN public.enrollment e ON e.id = a.enrollment_id
  JOIN public.answered_question_options aqo ON aqo.answer_id = a.id
  WHERE e.survey_id='<survey_id>' AND a.question_id IN ('<q1>','<q2>')
  GROUP BY a.enrollment_id, a.product_id" --x-col liking --y-col intent

python3 .../sql_stats.py describe   --sql "SELECT <numeric> FROM ..."          # N, mean, sd, CI, quartiles
python3 .../sql_stats.py crosstab   --sql "SELECT rowlbl, collbl, count(*) FROM ... GROUP BY 1,2"
python3 .../sql_stats.py proportion --sql "SELECT p.name, (score >= 4) FROM ..."   # top-box + Wilson CI
```

Columns default to positional order (`groups`: first = label, last = value);
override with `--group-col/--value-col/--x-col/--y-col/--row-col/--col-col`.
`--alpha` and `--json` are available on every mode. Read the caveat lines it
prints — small-N warnings, dropped non-numeric rows, and "expected count < 5"
notices belong in your answer, not just in the tool output.

**Statistical honesty is part of being the oracle:**

- Always report N alongside every mean, percentage, correlation, or difference.
- Never report a correlation or comparison without its p-value or significance
  verdict.
- A non-significant result is **inconclusive**, not proof of no effect. Say
  "not statistically significant at alpha=0.05 (N=…)", never "there is no
  relationship".
- With small N, lead with the caveat rather than the coefficient.
- Report the effect size, not just significance — a p<0.001 with eta²=0.01 is a
  real but trivial difference, and saying so is the difference between a correct
  answer and a misleading one.
- When many pairwise comparisons are run, use the Tukey/Bonferroni-corrected
  result rather than the raw pairwise p-values.

**Cross-survey / historical comparison.** The same question has a different
`question.id` in every survey, and `question_library_item` is too sparse to join
on. Resolve the equivalent question per survey by `"typeOfQuestion"` plus
keyword `ILIKE`s, aggregate each survey independently, then compare — and show
the matched prompt text per survey. When picking a comparison survey by name,
prefer the candidate that has real enrollments/answers over a more literal title
match (drafts and `Copy - ` duplicates are everywhere). For "suggest comparable
products", `survey_nomenclature` carries product category / client label / type
of test / country, but is sparsely populated: check for a row before relying on
it and fall back to product and survey name matching, saying which you used.

**Benchmark comparison.** Only when the prompt explicitly asks to compare
against "the benchmark". Resolve it live with
`resolve_scope.py --benchmark` (it is the client flagged
`is_benchmark_source`; currently "FlavorWiki Benchmark Library" /
"Benchmark Library Org" / the "Confidential benchmark product" survey). Query the
user's survey scoped normally, query the benchmark survey separately, and present
both side by side. Reaching that one survey is intentional, not a tenancy
violation — do not extend the exception to any other client, organization, or
survey.

## Phase 5 — Answer

Report the answer in prose, grounded entirely in tool output:

- Lead with the direct answer to the question asked.
- Give the numbers with their N, and the significance verdict for anything
  comparative.
- Name questions and products by their **label or prompt text**, not UUIDs, and
  quote the matched question prompt so the match is auditable.
- Show a compact table when comparing products, surveys, or groups. No raw
  answer dumps, no UUID columns, unless the user asked for raw data.
- State what you could not do: a statistic that wouldn't compute, an ambiguous
  question match, a non-default assumption, a fallback route you had to use, an
  alpha you defaulted to. A gap named is fine; a gap papered over is not.
- **Say when a number came from the local engine because the Charts API had no
  data for that question.** The agent under test has no local statistics engine —
  its only route is the API — so a statistic you computed locally is one it could
  not have produced at all. Flagging that keeps a scorer from recording a
  capability gap as an agent error.
- This phase answers the survey question; it does not grade anyone, and neither
  does any later phase. `trace_compare` grades a transcript's content against this
  answer; `contract_review` grades its presentation and behaviour. Hand the answer
  over and stop.
- Close with a short **Method** note: which questions you matched, which tables
  and value path you used, which engine produced the statistics, and any
  validation check you ran. That is what makes you usable as a reference for
  scoring another agent.

If the prompt cannot be answered from this data at all, say that plainly and
explain what is missing. That is a correct oracle answer. Guessing is not.

## Phase 6 — Record your trajectory (when a trajectory verdict is asked for)

There is no separate reference-agent process. **You are the reference agent**: you run in the
Claude Code harness, you decide what to retrieve and in what order, and your trajectory is the
ideal `funda_agent_exp.py` is measured against.

The two harnesses look nothing alike — yours is a coding agent driving shell scripts, the
target's is a LangGraph tool loop — so a literal call-for-call diff is meaningless. What *is*
comparable is the **information** each run reached, in what order, and why. That is what the
trajectory log records.

### Turn it on

```bash
export ORACLE_TRACE_FILE=/tmp/oracle-<case>.trace.jsonl
python3 scripts/trace_log.py --manifest --prompt "<the probe's exact prompt>" \
  --survey <uuid> --client <uuid> --org <uuid>
```

Write the manifest **first**, before any other command — it refuses to overwrite a trace that
already has steps. It binds the run to source hashes and scope the way the agent side's
`probe_manifest.py` does; without it a grader can only infer your scope from SQL literals,
which is exactly what the first paired grading reported as a gap.

Every script below then logs through `scripts/trace_log.py` automatically. Without the
variable, logging is a silent no-op, so ordinary exploratory use costs nothing and writes
nothing.

### Say why, every time

Each script takes `--why`, and the reason is recorded beside the call:

```bash
bash scripts/query.sh --why "locate the gender question" --tuples "SELECT ..."
python3 scripts/stats_api.py --question <uuid> --types anova,tukey --why "test product differences"
python3 scripts/plsr_ref.py --survey <uuid> --kpi <uuid> --attributes <uuid,uuid> --cv loo --why "rank drivers of overall liking"
python3 scripts/word_cloud.py --question <uuid> --why "identify dominant themes"
```

`--why` is not decoration. `trace_compare` uses it to judge whether the target agent's
equivalent step served the same purpose, which is the only way to compare two trajectories that
share no tool names. A step logged without a reason is a step nobody can grade.

### Your tools and their counterparts

The normalized vocabulary in `trace_log.py` is what lines the two runs up:

| your capability | script | `funda_agent_exp.py` counterpart |
|---|---|---|
| `sql_retrieval` | `query.sh` | `nl2sql_tool` |
| `survey_statistics` | `stats_api.py` | `run_survey_stats` |
| `plsr_analysis` | `plsr_ref.py` | `analyze_plsr` — **the same tool**, invoked directly |
| `word_cloud` | `word_cloud.py` | `generate_word_cloud` |
| `scope_resolution` | `resolve_scope.py` | *(none — the agent is handed its scope)* |
| `db_health` | `db_up.sh` | *(none)* |

`sql_stats.py` has no counterpart either: the agent's only statistics route is the Charts API.
A figure you computed there is one it could not have produced, which is a capability gap rather
than an agent error — say so in your answer.

`plsr_ref.py` is not a counterpart — it **is** `analyze_plsr`, invoked directly, returning the
byte-identical payload the agent's model sees. That is deliberate: PLSR is a deterministic
analysis with a fixed payload contract, and your job is to hold that analysis, not to
second-guess how it is fetched. The cost is that the PLSR path is shared end to end, so neither
trajectory nor content comparison can detect a defect inside it — both runs would be wrong
together. Where a PLSR figure is load-bearing, say so, and reach for §5's independent check:
regenerate against scikit-learn in a throwaway venv and compare to
`tests/plsr_sklearn_fixture.json`.

### Hand over three things

1. your **Phases 0-5 ground-truth answer** — the content standard;
2. your **trajectory log** (`python3 scripts/trace_log.py <file>` renders the step table) and
   your **final answer** — the trajectory and coverage standard;
3. a note on anything you could not retrieve, so a coverage gap in the target's answer is not
   blamed on the target when the information was unreachable for both.

Then stop. `trace_compare` grades; you do not.
