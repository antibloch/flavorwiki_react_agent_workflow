# FlavorAI PostgreSQL Database Schema Reference

## Purpose and authority

This is an LLM-oriented map of the live `gpi_sample_db` PostgreSQL database used by FlavorAI. It is designed for agents that must produce accurate, efficient, survey-scoped SQL.

- **Source of truth:** live PostgreSQL catalogs and live, aggregate-only data profiling on `127.0.0.1:5433`.
- **Server:** PostgreSQL 15, database `gpi_sample_db`, schema `public`.
- **Coverage:** every public table/materialized view, column, data type, nullability/default, primary/foreign/unique/check constraint, index, non-internal trigger, public enum, application function signature, and materialized-view definition.
- **Snapshot boundary:** object definitions are authoritative at generation time. Row counts, sizes, value frequencies, and population observations are only a current-data snapshot and may drift.
- **Privacy:** examples below describe shapes and aggregate counts. Do not expose passwords, tokens, private keys, emails, phone numbers, bank data, raw audit payloads, or respondent-level records unless explicitly authorized.

## Critical SQL rules

1. Scope ordinary analysis to the requested `survey.id`. Scope cross-survey work to the authorized `organization_id`, and verify the client through the tenancy chain below.
2. Preserve quoted camelCase identifiers exactly, for example `question."surveyId"`, `question."typeOfQuestion"`, `answer."isSkipped"`, and `answered_question_options."answerData"`. The table `"user"` must be quoted.
3. Never use `SELECT *`. Select only the fields needed for the answer.
4. Treat `enrollment.id` as the default participation/respondent key. Use `COUNT(DISTINCT e.id)` after one-to-many joins.
5. Answers have no `survey_id`; scope them through `answer.enrollment_id -> enrollment.id -> enrollment.survey_id`.
6. Determine the question's exact `"typeOfQuestion"` before choosing an answer value path. Most ratings and choices are not in `answer.value`.
7. `answered_question_options` is one-to-many from `answer`; joining it can multiply rows. Aggregate to the intended grain first.
8. Use raw `answer` data for response analysis. `charts`, `aggregations`, reports, and exports are saved configurations/artifacts, not observations.
9. Do not infer participation from panel membership. Use `enrollment` for actual survey entry/completion and `survey_panel_stats` only for fieldwork lifecycle detail.
10. A zero-row result is diagnostic, not automatically the answer. Verify the survey, question match, question type, and stored value shape before concluding no data exists.

## Tenancy and survey scope

The verified ownership chain is:

```text
client.id
  <- account.client_id
  <- organization.account_id
  <- survey.organization_id
```

There is no direct `survey.client_id`. Verify all supplied IDs together:

```sql
SELECT s.id, s.title, s.state
FROM survey AS s
JOIN organization AS o ON o.id = s.organization_id
JOIN account AS a ON a.id = o.account_id
WHERE s.id = :survey_id
  AND o.id = :organization_id
  AND a.client_id = :client_id;
```

For cross-survey comparisons, find candidate surveys with `s.organization_id = :organization_id` and the same verified account/client. Prefer the candidate with actual enrollments/answers when duplicate, copied, or draft survey titles exist, and report the exact selected title/ID.

## Core relationship map and grain

```text
survey (one survey)
  +-- question_section (one ordered section)
  |    +-- question_screen (one ordered screen)
  |         +-- question (one question definition)
  |              +-- question_option (one possible option)
  |              +-- question_group (one option group)
  |              +-- question_set (one tray/set/combination)
  |              +-- question_pair (one configured comparison pair)
  +-- product (one configured product/sample)
  +-- enrollment (one participation instance)
       +-- answer (one question response header, optionally per product)
            +-- answered_question_options (one structured response component)

survey +-- survey_panel -- panel -- panel_panelist -- panelist
       +-- product_display_order -- product_display_order_item -- product
       +-- logic -- logic_rule -- logic_rule_condition
       +-- charts_reports -- charts_tabs -- charts
```

Important physical-name exceptions:

- Design tables generally use camelCase survey links: `question."surveyId"`, `product."surveyId"`, `question_screen."surveyId"`, `question_section."surveyId"`.
- Runtime tables use snake_case: `enrollment.survey_id`, `answer.enrollment_id`, `answer.question_id`, `answer.product_id`.
- `answer.product_id` is semantically a `product.id` reference but has **no database foreign-key constraint** in this snapshot. Join it explicitly and use `LEFT JOIN` when retaining orphan/legacy answers matters.
- `survey.organization_id` has two redundant foreign-key constraints to `organization(id)` in this snapshot; this does not imply two relationships.

## Response storage model

Current live population confirms that structured storage is the default: most `answer.value` values are null/blank, while `answered_question_options` contains one or more response components. Route by type:

| `question."typeOfQuestion"` | Authoritative response path | Notes |
|---|---|---|
| `open-answer`, `email`, `upload-multimedia` | `answer.value` | Scalar text/URL. Validate and redact sensitive values. |
| `multiple-choice` | `answered_question_options.question_option_id -> question_option` | Use `label` for display and `analytical_value` for numeric coding. Multi-select yields multiple child rows. `answer.value` is commonly blank. |
| `line-scale`, `vertical-rating` | `answered_question_options."answerData" ->> 'optionAnswer'` | JSON number; cast after checking key/type. |
| `time-intensity-slider` | `answerData.optionAnswer`, `answerData.t_ms` | Multiple time/value observations per answer. |
| `multiple-open-answer`, `contact-information` | `answerData.optionAnswer` | One structured text component per option/field; contact data is sensitive. |
| `matrix` | `matrix_row_option_id` + `question_option_id` | Resolve both IDs to `question_option`; `answerData.optionLabel` is a display snapshot and occasional `optionValue` may exist. |
| `ranking` | `question_option_id` + `answerData.rank` | One item/rank component; optional `justificationText`. |
| `paired-questions` | `question_pair_id` + `answerData.optionAnswer` | Resolve pair through `question_pair`; `responseType` is also stored in JSON. |
| `triangle-test` | `question_option_id`, `answerData.optionAnswer`, `sampleLabel` | Discrimination response; use helper stats function where appropriate. |
| `tetrad-test` | `question_option_id`, `answerData.optionAnswer`, `sampleLabel`, `group` | Multiple grouped components per answer. |
| `tds`, `tcata` | `question_option_id`, `answerData.action`, `answerData.t_ms` | Temporal event stream; preserve event order/time grain. |
| `individual-balloting` | `question_option_id`, `answerData.optionAnswer`, `sectionCommentAnswer` | Structured ballot/attribute values; some scalar comments also occur. |
| `question_set`-based designs | `answered_question_options.question_set_id -> question_set` | `question_set.setData`, `combination`, and `tray_id` describe the design. |

Observed structured JSON key types are stable in this snapshot: rating `optionAnswer` values are JSON numbers for scale questions; most other `optionAnswer` values are strings; ranking `rank` and temporal `t_ms` are JSON numbers.

### Respondent identity and demographics

- One `enrollment` is one participation instance. It may be anonymous, code-based, panelist-based, or registered-user based.
- In this snapshot, `enrollment.user_id` is unpopulated and only a small fraction has `panelist_id`. Never count respondents by `user_id`.
- Gender, age, country, and similar demographics are usually ordinary survey questions. Find the exact question in the same survey, then join its answers by `enrollment_id` to the requested metric.
- `"user".gender/country/city/language` is only a registered-user fallback after confirming the survey contains no corresponding question. `panelist` contains contact/status fields, not normalized demographics.

### Completion and skipping

- `enrollment.enrollment_status` is `active`, `completed`, or `expired` (enum). Filter it only when the wording requires a status.
- `answer."isSkipped"` marks a stored skipped answer. Missing answer rows and skipped answer rows are different conditions.
- `answer."timeToAnswer"` is a bigint duration field; confirm the application unit before labeling it in output. `answeredAt` is bigint legacy/event data, while `createdAt`/`updatedAt` are timestamps.

## Canonical query patterns

### Survey overview without Cartesian multiplication

```sql
SELECT s.id, s.title, s.state, s.type, s."isActive",
       (SELECT count(*) FROM enrollment e WHERE e.survey_id = s.id) AS enrollments,
       (SELECT count(*) FROM enrollment e WHERE e.survey_id = s.id AND e.enrollment_status = 'completed') AS completed,
       (SELECT count(*) FROM question q WHERE q."surveyId" = s.id) AS questions,
       (SELECT count(*) FROM product p WHERE p."surveyId" = s.id) AS products,
       (SELECT count(*) FROM answer a JOIN enrollment e ON e.id = a.enrollment_id WHERE e.survey_id = s.id) AS answers
FROM survey s
WHERE s.id = :survey_id;
```

### Resolve a question by meaning

Use separate semantic keywords rather than one guessed phrase, inspect every candidate, and carry the matched prompt into the final result:

```sql
SELECT q.id, q.prompt, q."typeOfQuestion", q."order"
FROM question q
WHERE q."surveyId" = :survey_id
  AND q.prompt ILIKE '%overall%'
  AND q.prompt ILIKE '%lik%'
ORDER BY q."order";
```

### Categorical distribution

```sql
SELECT qo.label, qo.analytical_value,
       COUNT(DISTINCT a.enrollment_id) AS respondents
FROM answer a
JOIN enrollment e ON e.id = a.enrollment_id
JOIN answered_question_options aqo ON aqo.answer_id = a.id
JOIN question_option qo ON qo.id = aqo.question_option_id
WHERE e.survey_id = :survey_id
  AND a.question_id = :question_id
  AND NOT a."isSkipped"
GROUP BY qo.id, qo.label, qo.analytical_value
ORDER BY qo."order";
```

### Numeric rating by product

```sql
WITH values AS (
  SELECT a.enrollment_id, a.product_id,
         (aqo."answerData" ->> 'optionAnswer')::numeric AS score
  FROM answer a
  JOIN enrollment e ON e.id = a.enrollment_id
  JOIN answered_question_options aqo ON aqo.answer_id = a.id
  WHERE e.survey_id = :survey_id
    AND a.question_id = :question_id
    AND aqo."answerData" ? 'optionAnswer'
    AND jsonb_typeof(aqo."answerData" -> 'optionAnswer') = 'number'
    AND NOT a."isSkipped"
)
SELECT p.id, p.name, count(*) AS n, avg(v.score) AS mean,
       min(v.score) AS min, max(v.score) AS max
FROM values v
LEFT JOIN product p ON p.id = v.product_id
GROUP BY p.id, p.name
ORDER BY p."productIndex" NULLS LAST, p.name;
```

### Join a demographic answer to a metric at respondent grain

Resolve both question IDs first. Aggregate each side before joining so multi-select/structured rows do not create a cross product:

```sql
WITH demographic AS (
  SELECT a.enrollment_id, string_agg(DISTINCT qo.label, ', ' ORDER BY qo.label) AS segment
  FROM answer a
  JOIN enrollment e ON e.id = a.enrollment_id
  JOIN answered_question_options aqo ON aqo.answer_id = a.id
  JOIN question_option qo ON qo.id = aqo.question_option_id
  WHERE e.survey_id = :survey_id AND a.question_id = :demographic_question_id
  GROUP BY a.enrollment_id
), metric AS (
  SELECT a.enrollment_id, a.product_id,
         avg((aqo."answerData" ->> 'optionAnswer')::numeric) AS score
  FROM answer a
  JOIN enrollment e ON e.id = a.enrollment_id
  JOIN answered_question_options aqo ON aqo.answer_id = a.id
  WHERE e.survey_id = :survey_id AND a.question_id = :metric_question_id
    AND jsonb_typeof(aqo."answerData" -> 'optionAnswer') = 'number'
  GROUP BY a.enrollment_id, a.product_id
)
SELECT d.segment, m.product_id, count(*) AS n, avg(m.score) AS mean
FROM metric m JOIN demographic d USING (enrollment_id)
GROUP BY d.segment, m.product_id;
```

### Paired values for correlation or paired tests

```sql
SELECT a.enrollment_id, a.product_id,
       max((aqo."answerData" ->> 'optionAnswer')::numeric)
         FILTER (WHERE a.question_id = :question_1) AS value_1,
       max((aqo."answerData" ->> 'optionAnswer')::numeric)
         FILTER (WHERE a.question_id = :question_2) AS value_2
FROM answer a
JOIN enrollment e ON e.id = a.enrollment_id
JOIN answered_question_options aqo ON aqo.answer_id = a.id
WHERE e.survey_id = :survey_id
  AND a.question_id IN (:question_1, :question_2)
  AND jsonb_typeof(aqo."answerData" -> 'optionAnswer') = 'number'
GROUP BY a.enrollment_id, a.product_id;
```

Use PostgreSQL `corr()`/`regr_*()` for simple descriptive relationships. The database functions `get_stats_discrimination_tests`, `get_survey_answers`, and `get_sensory_test_answers` provide application-specific flattened/statistical routes; inspect their signatures below and still scope inputs explicitly.

## Efficiency and correctness checklist

- Start from the smallest table bundle that owns the requested fact.
- Apply survey/tenant predicates before large joins, especially before joining `answer` or `answered_question_options`.
- Prefer UUID equality predicates over title/prompt text after resolving IDs.
- Use `EXPLAIN (ANALYZE, BUFFERS)` only for performance diagnosis and never on mutating SQL.
- Do not cast arbitrary answer text. Restrict to a known question/type and validate JSON/text shape before numeric casts.
- Use option IDs for joins and labels only for display. Do not invent meanings for stored codes.
- Include the exact matched survey title, question prompt, option label, product name, observation count, and applied status filter needed to make an aggregate auditable.
- Treat all `legacy_*` identifiers as migration/interoperability fields, not preferred join keys.
- Soft-deletion fields (`deleted_at`, `archived_at`, `deactivated_at`) must be filtered when the request means currently active business entities.

## Live snapshot summary

Generated at **2026-08-06 19:17:10 UTC** from PostgreSQL **15.18 (Debian 15.18-1.pgdg13+1)**.

- Public tables: **112**
- Public partitioned tables: **0**
- Public ordinary views: **0**
- Public materialized views: **1**
- Public enum types: **42**
- Public application functions (extension-owned functions excluded): **9**
- Public sequences: **1**
- Row-level security policies: **0**; RLS-enabled listed objects: **0**

Largest analytical tables in this snapshot:

| Object | Exact rows | Total size |
|---|---:|---:|
| `answered_question_options` | 508,180 | 186 MB |
| `answer` | 275,168 | 144 MB |
| `jobs_meta` | 333 | 83 MB |
| `audit_log` | 38,920 | 38 MB |
| `question_option` | 38,629 | 12 MB |
| `question` | 9,138 | 11 MB |
| `enrollment` | 39,097 | 8232 kB |
| `product_display_order_item` | 34,041 | 7520 kB |
| `question_set` | 19,631 | 6632 kB |
| `question_screen` | 7,174 | 2584 kB |
| `charts` | 5,792 | 1776 kB |
| `product_display_order` | 4,675 | 1720 kB |
| `question_pair` | 4,460 | 1216 kB |
| `survey` | 1,347 | 864 kB |
| `product` | 2,527 | 664 kB |

### Response population profile

- Enrollments: **39,097**; with registered `user_id`: **0**; with `panelist_id`: **134**.
- Answers: **275,168**; non-null `value`: **30,706**; nonblank `value`: **17,822**; with `product_id`: **210,511**.
- Structured child rows: **508,180**, belonging to **258,025** distinct answers.

These figures explain why `answer.value` must not be the default value source for ratings, choices, matrices, or sensory-test responses.

### Observed unconstrained vocabularies

These values are observed, not database-enforced allowed sets. New application values can appear because several fields are `varchar`.

| Field | Observed value | Rows |
|---|---|---:|
| `enrollment.enrollment_status` | `completed` | 31,118 |
| `enrollment.enrollment_status` | `active` | 7,979 |
| `question.typeOfQuestion` | `multiple-choice` | 3,097 |
| `question.typeOfQuestion` | `line-scale` | 1,164 |
| `question.typeOfQuestion` | `open-answer` | 985 |
| `question.typeOfQuestion` | `vertical-rating` | 640 |
| `question.typeOfQuestion` | `multiple-open-answer` | 565 |
| `question.typeOfQuestion` | `info` | 558 |
| `question.typeOfQuestion` | `triangle-test` | 345 |
| `question.typeOfQuestion` | `email` | 335 |
| `question.typeOfQuestion` | `tetrad-test` | 322 |
| `question.typeOfQuestion` | `matrix` | 189 |
| `question.typeOfQuestion` | `contact-information` | 184 |
| `question.typeOfQuestion` | `time-intensity-slider` | 178 |
| `question.typeOfQuestion` | `individual-balloting` | 153 |
| `question.typeOfQuestion` | `tcata` | 143 |
| `question.typeOfQuestion` | `tds` | 141 |
| `question.typeOfQuestion` | `upload-multimedia` | 70 |
| `question.typeOfQuestion` | `ranking` | 39 |
| `question.typeOfQuestion` | `paired-questions` | 30 |
| `survey.productDisplayState` | `unactive` | 1,347 |
| `survey.productDisplayType` | `none` | 1,347 |
| `survey.state` | `draft` | 580 |
| `survey.state` | `active` | 411 |
| `survey.state` | `deprecated` | 258 |
| `survey.state` | `archived` | 60 |
| `survey.state` | `suspended` | 36 |
| `survey.state` | `closed` | 2 |
| `survey.type` | `sample-evaluation-survey-product-study` | 806 |
| `survey.type` | `simple-questionnaire` | 178 |
| `survey.type` | `sensory-descriptive-analysis-test` | 167 |
| `survey.type` | `sensory-discrimination-tetrad-test` | 79 |
| `survey.type` | `sensory-discrimination-triangle-test` | 78 |
| `survey.type` | `sensory-discrimination-individual-balloting-test` | 22 |
| `survey.type` | `sensory-temporal-tds` | 10 |
| `survey.type` | `sensory-temporal-tcata` | 4 |
| `survey.type` | `sensory-temporal-time-intensity-slider` | 3 |

### Observed structured response JSON keys

The following is an aggregate shape inventory of `answered_question_options."answerData"`; it contains no raw response values.

| Question type | JSON key | JSON type | Child rows |
|---|---|---|---:|
| `contact-information` | `optionAnswer` | `string` | 837 |
| `individual-balloting` | `optionAnswer` | `string` | 729 |
| `individual-balloting` | `sectionCommentAnswer` | `string` | 729 |
| `line-scale` | `optionAnswer` | `number` | 14,379 |
| `matrix` | `optionLabel` | `string` | 116,609 |
| `matrix` | `optionValue` | `string` | 216 |
| `matrix` | `rowLabel` | `string` | 1 |
| `multiple-choice` | `optionAnswer` | `string` | 155,863 |
| `multiple-open-answer` | `optionAnswer` | `string` | 28,952 |
| `paired-questions` | `optionAnswer` | `string` | 7,895 |
| `paired-questions` | `responseType` | `string` | 7,895 |
| `ranking` | `optionLabel` | `string` | 209 |
| `ranking` | `rank` | `number` | 209 |
| `ranking` | `justificationText` | `string` | 41 |
| `tcata` | `action` | `string` | 547 |
| `tcata` | `t_ms` | `number` | 547 |
| `tds` | `action` | `string` | 490 |
| `tds` | `t_ms` | `number` | 490 |
| `tetrad-test` | `group` | `string` | 1,611 |
| `tetrad-test` | `optionAnswer` | `string` | 1,611 |
| `tetrad-test` | `sampleLabel` | `string` | 1,611 |
| `time-intensity-slider` | `optionAnswer` | `number` | 7,292 |
| `time-intensity-slider` | `t_ms` | `number` | 7,292 |
| `triangle-test` | `optionAnswer` | `string` | 223 |
| `triangle-test` | `sampleLabel` | `string` | 223 |
| `vertical-rating` | `optionAnswer` | `number` | 164,800 |

## Object inventory by domain

### Survey design

`logic`, `logic_rule`, `logic_rule_condition`, `piping_references`, `product`, `product_display_order`, `product_display_order_item`, `question`, `question_group`, `question_library`, `question_library_item`, `question_option`, `question_pair`, `question_screen`, `question_section`, `question_set`, `survey`.

### Responses

`answer`, `answer_failed_job`, `answered_question_options`, `enrollment`, `enrollment_recaptcha_verification`.

### Panels and fieldwork

`panel`, `panel_code`, `panel_code_format`, `panel_panelist`, `panelist`, `survey_panel`, `survey_panel_code`, `survey_panel_stats`.

### Reporting and analysis

`aggregation_report_tabs`, `aggregations`, `benchmark_registry`, `chart_report_filter`, `chart_settings_template`, `charts`, `charts_reports`, `charts_tabs`, `conversation_history`, `exports`, `external_api_stats`.

### Nomenclature

`nomenclature_category`, `nomenclature_client`, `nomenclature_formula_fw_sequence`, `nomenclature_type_of_test`, `survey_nomenclature`.

### Tenancy and licensing

`account`, `account_licenses`, `application`, `bank_account`, `client`, `client_cost_centers`, `client_info`, `cost_center`, `cost_center_counterparty_bank`, `counterparty`, `customer`, `invoice_portal`, `license`, `license_app_data`, `license_app_data_metadata`, `license_clients`, `license_payment_milestone`, `module_contact`, `organization`, `organization_applications`, `service_clients`.

### Users and authorization

`access_group`, `access_group_user`, `assigned_stakeholder`, `assigned_stakeholder_users`, `onboarding`, `organization_roles_role`, `permission`, `permission_closure`, `permission_operation`, `personnels_csv_upload_history`, `role`, `role_organizations_organization`, `role_permissions_permission`, `role_users_user`, `user`, `user_contact_type`, `user_organization_role_permission`, `user_organizations_organization`, `user_private_keys`, `user_roles_role`, `user_session`, `user_tag`, `user_tags_users`, `welcome_questionaire_answer`.

### Sharing

`shared_access`, `shared_access_group`, `shared_access_log`, `shared_access_organization`, `shared_access_user`, `user_shared_access`.

### Files and folders

`branding`, `branding_domains`, `document`, `folder`, `folder_closure`, `image_doc`, `tiny_url`.

### Audit and operations

`audit_log`, `audit_log_archive`, `feature_flag_scopes`, `feature_flag_users`, `feature_flags`, `jobs`, `jobs_meta`.

### System metadata

`alembic_version`, `migrations`.

## Public enum types

Enum columns accept exactly the values shown. Plain `varchar` status/type fields are not constrained by these enums.

- `aggregations_aggregationtype_enum`: `mean-cross-tab`, `mean-and-group`, `kano-analysis`, `penalty-line-chart`, `penalty-scatterplot`, `penalty-table-by-attributes`, `proportion-test-advance`
- `aggregations_grouping_type_enum`: `fisher`, `cochran-q`, `duncan`, `friedman`, `kruskal`, `none`, `tukey`, `dunnett`
- `aggregations_kruskal_method_enum`: `dunn`, `conover`, `dscf`
- `aggregations_significance_representation_enum`: `standard`, `by-groups`
- `application_state_enum`: `active`, `inactive`
- `assigned_stakeholder_module_type_enum`: `customer`, `client`, `account`, `license`, `organization`, `counterparty`, `cost_center`
- `assigned_stakeholder_type_enum`: `operator`, `stakeholder`
- `chart_report_filter_type_enum`: `temporary`, `permanent`
- `charts_charttype_enum`: `bar-chart`, `bar-chart-mean`, `column-chart`, `column-chart-mean`, `pie-chart`, `table-chart`, `attribute-ranking`, `pair-comparison-matrix`, `alternative-response`, `line-chart`, `word-cloud`, `spider-chart`, `triangle-discrimination-pie`, `triangle-answer-analysis`, `triangle-discrimination-table`, `triangle-d-prime-table`, `test-line-chart`, `stacked-column-chart`, `stacked-bar-chart`, `stacked-column-bar-chart`, `intensity-curve`, `intensity-max`, `intensity-time`, `intensity-auc`, `pca`, `pca-biplot`, `anova`, `rm-anova`, `tukey`, `duncan`, `fisher`, `dunnett`, `friedman`, `kruskal`, `proportion-classic`, `proportion-advance`, `fisher-exact`, `z-test-proportion`, `penalty`, `pearson`, `spearman`, `dominance-over-time`, `panelist-score-summary-chart`, `panelist-score-summary-table`, `dot-plot`, `histogram`
- `client_info_type_enum`: `billing`, `finance`
- `enrollment_enrollment_status_enum`: `active`, `completed`, `expired`
- `feature_flags_status_enum`: `open`, `closed`, `restricted`
- `folder_state_enum`: `active`, `deleted`
- `license_payment_type_enum`: `absolute`, `percentage`
- `license_state_enum`: `active`, `inactive`, `expired`, `suspended`
- `license_type_enum`: `paid`, `free`, `trial`
- `logic_rule_actiontype_enum`: `reject`, `jump_to_screen`, `jump_to_next_screen`, `jump_to_design_section`, `jump_to_end_section`, `end_survey`
- `logic_rule_condition_condition_enum`: `equal`, `not_equal`, `contains`, `not_contains`, `greater_than`, `less_than`, `greater_than_or_equal`, `less_than_or_equal`, `in`, `not_in`, `is_empty`, `is_not_empty`, `answered`, `not_answered`, `is_correct`, `is_incorrect`, `option_not_selected`, `option_selected`
- `logic_rule_condition_nextoperator_enum`: `and`, `or`
- `logic_rule_ruletype_enum`: `if`, `else`
- `logic_rule_targettype_enum`: `screen`, `section`, `question`, `end_survey`
- `module_contact_contact_type_enum`: `billing`, `financial`
- `module_contact_module_type_enum`: `customer`, `client`, `account`, `license`, `organization`, `counterparty`, `cost_center`
- `organization_type_enum`: `legal`, `commercial`
- `panel_access_mode_enum`: `anonymous_code`, `panelist_code`, `email_only`, `panelist_code_email`
- `panel_code_status_enum`: `available`, `in_use`, `used`, `exhausted`, `expired`, `suspended`
- `panel_status_enum`: `active`, `inactive`, `archived`
- `panelist_status_enum`: `active`, `inactive`
- `product_display_order_displaytype_enum`: `default`, `randomized`
- `question_library_type_enum`: `my`, `shared`, `gpi`
- `question_option_type_enum`: `other`, `justify`, `custom`, `skipped`, `unknown`, `correct`, `wrong`, `row`, `column`
- `question_set_status_enum`: `pending`, `in-progress`, `answered`
- `recaptcha_action_enum`: `survey_submit`
- `recaptcha_verification_status_enum`: `passed`, `failed`, `error`
- `shared_access_global_access_level_enum`: `viewer`, `editor`
- `shared_access_group_access_level_enum`: `viewer`, `editor`
- `shared_access_module_type_enum`: `survey`, `template`, `folder`, `report`
- `shared_access_module_type_enum_old`: `survey`, `folder`, `report`
- `shared_access_organization_access_level_enum`: `viewer`, `editor`
- `shared_access_user_access_level_enum`: `viewer`, `editor`
- `survey_panel_stats_status_enum`: `planned`, `not_started`, `in_progress`, `completed`, `screened_out`, `expired`, `exhausted`, `suspended`, `available`, `used`
- `tiny_url_module_type_enum`: `survey`, `folder`, `report`

## Complete object dictionary

Column syntax is `name type [NOT NULL] [DEFAULT ...]`. Constraints are copied from `pg_get_constraintdef`; indexes are copied from `pg_get_indexdef`. Exact row counts and sizes are snapshot observations.

### `access_group`

**Kind/grain:** table; Named access-sharing group.

**Snapshot:** 8 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"created_by" uuid
"deleted_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_6a36f88265e6c07bd59939df05c`: `PRIMARY KEY (id)`
- FK `FK_d264a4aa1af3669fa48b951f73f`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `access_group_user` via `FK_44507b9cdbb42fc00fc08c3da48`
  - `shared_access_group` via `FK_92b7d67f1f1aa3155142e05701a`

**Indexes**

- `PK_6a36f88265e6c07bd59939df05c`: `CREATE UNIQUE INDEX "PK_6a36f88265e6c07bd59939df05c" ON public.access_group USING btree (id)`

### `access_group_user`

**Kind/grain:** table; Membership join between access groups and users.

**Snapshot:** 67 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" uuid NOT NULL
"group_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_2291c831e02d70c9ec43b492104`: `PRIMARY KEY (id)`
- FK `FK_44507b9cdbb42fc00fc08c3da48`: `FOREIGN KEY (group_id) REFERENCES access_group(id) ON DELETE CASCADE`
- FK `FK_e949597afc2c20c650f961b2ae9`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`

**Indexes**

- `PK_2291c831e02d70c9ec43b492104`: `CREATE UNIQUE INDEX "PK_2291c831e02d70c9ec43b492104" ON public.access_group_user USING btree (id)`

### `account`

**Kind/grain:** table; Account under customer/client; parent of organizations through organization.account_id.

**Snapshot:** 25 rows; 88 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(128) NOT NULL
"code" character varying(128) NOT NULL
"customer_id" uuid
"client_id" uuid
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"is_draft" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_54115ee388cdb6d86bb4bf5b2ea`: `PRIMARY KEY (id)`
- UNIQUE `UQ_ACCOUNT_CODE`: `UNIQUE (code)`
- FK `FK_14f88efd4d31eafab6a62cc62cc`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_45a1670bb3c856a6ba08ec3c737`: `FOREIGN KEY (client_id) REFERENCES client(id)`
- FK `FK_651c60b8e28ceb49880f5be0b3b`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- FK `FK_92f552371b42ca6b8cc50f7a3f7`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_977b5abdf1370566eaade16eaa9`: `FOREIGN KEY (customer_id) REFERENCES customer(id)`
- FK `FK_9f12e8ffb17cc4b4deeadb93401`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_f6e3fba2c8b88432e56d4268f13`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `account_licenses` via `FK_77338f473f9bf06e37513065ae0`
  - `branding` via `FK_branding_account`
  - `branding_domains` via `FK_branding_domains_account`
  - `feature_flag_scopes` via `FK_feature_flag_scopes_account`
  - `image_doc` via `FK_8430698496843370df9966742a8`
  - `organization` via `FK_5865830635bbaf5c95135a5fdce`

**Indexes**

- `PK_54115ee388cdb6d86bb4bf5b2ea`: `CREATE UNIQUE INDEX "PK_54115ee388cdb6d86bb4bf5b2ea" ON public.account USING btree (id)`
- `UQ_ACCOUNT_CODE`: `CREATE UNIQUE INDEX "UQ_ACCOUNT_CODE" ON public.account USING btree (code)`
- `IDX_ACCOUNT_CLIENT_ID`: `CREATE INDEX "IDX_ACCOUNT_CLIENT_ID" ON public.account USING btree (client_id)`
- `IDX_ACCOUNT_CUSTOMER_ID`: `CREATE INDEX "IDX_ACCOUNT_CUSTOMER_ID" ON public.account USING btree (customer_id)`
- `IDX_ACCOUNT_NAME`: `CREATE INDEX "IDX_ACCOUNT_NAME" ON public.account USING btree (name)`

**Non-internal triggers**

- `audit_account_delete_trigger`: `CREATE TRIGGER audit_account_delete_trigger AFTER DELETE ON account FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_account_insert_trigger`: `CREATE TRIGGER audit_account_insert_trigger AFTER INSERT ON account FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_account_update_trigger`: `CREATE TRIGGER audit_account_update_trigger AFTER UPDATE ON account FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `account_licenses`

**Kind/grain:** table; Assignment join between accounts and licenses.

**Snapshot:** 25 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"account_id" uuid NOT NULL
"license_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_de21b65742990ccc711b0421b19`: `PRIMARY KEY (account_id, license_id)`
- FK `FK_04bb208f34ea057ef127e0fa0d1`: `FOREIGN KEY (license_id) REFERENCES license(id)`
- FK `FK_77338f473f9bf06e37513065ae0`: `FOREIGN KEY (account_id) REFERENCES account(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_de21b65742990ccc711b0421b19`: `CREATE UNIQUE INDEX "PK_de21b65742990ccc711b0421b19" ON public.account_licenses USING btree (account_id, license_id)`
- `IDX_04bb208f34ea057ef127e0fa0d`: `CREATE INDEX "IDX_04bb208f34ea057ef127e0fa0d" ON public.account_licenses USING btree (license_id)`
- `IDX_77338f473f9bf06e37513065ae`: `CREATE INDEX "IDX_77338f473f9bf06e37513065ae" ON public.account_licenses USING btree (account_id)`

### `aggregation_report_tabs`

**Kind/grain:** table; Many-to-many join between aggregations and report tabs.

**Snapshot:** 86 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"aggregation_id" uuid NOT NULL
"report_tab_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_aggregation_report_tabs`: `PRIMARY KEY (aggregation_id, report_tab_id)`
- FK `FK_aggregation_report_tabs_aggregation`: `FOREIGN KEY (aggregation_id) REFERENCES aggregations(id) ON DELETE CASCADE`
- FK `FK_aggregation_report_tabs_report_tab`: `FOREIGN KEY (report_tab_id) REFERENCES charts_tabs(id) ON DELETE CASCADE`

**Indexes**

- `PK_aggregation_report_tabs`: `CREATE UNIQUE INDEX "PK_aggregation_report_tabs" ON public.aggregation_report_tabs USING btree (aggregation_id, report_tab_id)`

### `aggregations`

**Kind/grain:** table; One saved statistical aggregation configuration, not computed respondent-level data.

**Snapshot:** 166 rows; 104 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(128) NOT NULL
"survey_id" uuid NOT NULL
"question_ids" jsonb NOT NULL
"reference_question_id" character varying
"aggregation_type" aggregations_aggregationtype_enum NOT NULL
"grouping_type" aggregations_grouping_type_enum
"significance_representation" aggregations_significance_representation_enum
"alpha_value" double precision
"penalty_level" double precision
"boxing_strategy" character varying(128)
"allowed_enrollment_ids" jsonb
"product_ids" jsonb
"filter_ids" jsonb
"control_product_id" uuid
"kruskal_method" aggregations_kruskal_method_enum
```

**Constraints and relationships**

- PK `PK_7eb92e005781dd7e03dddc852f1`: `PRIMARY KEY (id)`
- FK `FK_f3a3e1b1c9ddd4af3730ad76879`: `FOREIGN KEY (survey_id) REFERENCES survey(id) ON DELETE CASCADE`
- Incoming foreign keys:
  - `aggregation_report_tabs` via `FK_aggregation_report_tabs_aggregation`

**Indexes**

- `PK_7eb92e005781dd7e03dddc852f1`: `CREATE UNIQUE INDEX "PK_7eb92e005781dd7e03dddc852f1" ON public.aggregations USING btree (id)`

### `alembic_version`

**Kind/grain:** table; Alembic migration revision marker.

**Snapshot:** 1 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"version_num" character varying(32) NOT NULL
```

**Constraints and relationships**

- PK `alembic_version_pkc`: `PRIMARY KEY (version_num)`

**Indexes**

- `alembic_version_pkc`: `CREATE UNIQUE INDEX alembic_version_pkc ON public.alembic_version USING btree (version_num)`

### `answer`

**Kind/grain:** table; One answer header for one enrollment/question and optionally one product; scalar text lives in value.

**Snapshot:** 275,168 rows; 144 MB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"isSkipped" boolean NOT NULL DEFAULT false
"timeToAnswer" bigint
"browserInfo" jsonb
"value" text
"startedAt" character varying
"enrollment_id" uuid
"product_id" uuid
"question_id" uuid
"legacy_enrollment_id" character varying
"legacy_product_id" character varying
"legacy_question_id" character varying
"legacy_answer_id" character varying
"answeredAt" bigint
"s3_object_should_be_deleted_on" timestamp with time zone
"s3_obj_deleted" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_9232db17b63fb1e94f97e5c224f`: `PRIMARY KEY (id)`
- FK `FK_c3d19a89541e4f0813f2fe09194`: `FOREIGN KEY (question_id) REFERENCES question(id)`
- FK `FK_ed1203afd9e56d1e6319a0e6475`: `FOREIGN KEY (enrollment_id) REFERENCES enrollment(id)`
- Incoming foreign keys:
  - `answered_question_options` via `FK_7ca87463deeaafb78690a2c85a7`

**Indexes**

- `PK_9232db17b63fb1e94f97e5c224f`: `CREATE UNIQUE INDEX "PK_9232db17b63fb1e94f97e5c224f" ON public.answer USING btree (id)`
- `uq_answer_enrollment_question`: `CREATE UNIQUE INDEX uq_answer_enrollment_question ON public.answer USING btree (enrollment_id, question_id) WHERE (product_id IS NULL)`
- `uq_answer_enrollment_question_product`: `CREATE UNIQUE INDEX uq_answer_enrollment_question_product ON public.answer USING btree (enrollment_id, question_id, product_id) WHERE (product_id IS NOT NULL)`
- `idx_answer_enrollment_id`: `CREATE INDEX idx_answer_enrollment_id ON public.answer USING btree (enrollment_id)`
- `idx_answer_performance`: `CREATE INDEX idx_answer_performance ON public.answer USING btree (question_id, enrollment_id, "isSkipped") WHERE (COALESCE("isSkipped", false) = false)`
- `idx_answer_question_enrollment`: `CREATE INDEX idx_answer_question_enrollment ON public.answer USING btree (question_id, enrollment_id)`
- `idx_answer_question_id`: `CREATE INDEX idx_answer_question_id ON public.answer USING btree (question_id)`
- `idx_answer_question_product`: `CREATE INDEX idx_answer_question_product ON public.answer USING btree (question_id, product_id, enrollment_id)`
- `idx_answer_sorting`: `CREATE INDEX idx_answer_sorting ON public.answer USING btree (question_id, enrollment_id)`

### `answer_failed_job`

**Kind/grain:** table; One failed asynchronous answer-processing job with retry/error payload.

**Snapshot:** 26 rows; 224 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"job_id" character varying NOT NULL
"job_name" character varying
"queue_name" character varying
"payload" jsonb
"error_message" text
"error_stack" text
"failed_at" timestamp with time zone NOT NULL
"attempts" integer NOT NULL DEFAULT 0
```

**Constraints and relationships**

- PK `PK_a80a92d33a79ffa4c39f3415c55`: `PRIMARY KEY (id)`

**Indexes**

- `PK_a80a92d33a79ffa4c39f3415c55`: `CREATE UNIQUE INDEX "PK_a80a92d33a79ffa4c39f3415c55" ON public.answer_failed_job USING btree (id)`

### `answered_question_options`

**Kind/grain:** table; One structured component of an answer: selected option, scale value, matrix cell, pair, set, or temporal event.

**Snapshot:** 508,180 rows; 186 MB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"question_option_id" uuid
"answerData" jsonb NOT NULL DEFAULT '{}'::jsonb
"answer_id" uuid
"question_set_id" uuid
"matrix_row_option_id" uuid -- For matrix question, store the selected row option id here
"question_pair_id" uuid
```

**Constraints and relationships**

- PK `PK_0d091b145e793bfb1804827a5af`: `PRIMARY KEY (id)`
- FK `FK_5aea890c0da849dac85d28a3fd2`: `FOREIGN KEY (question_set_id) REFERENCES question_set(id) ON DELETE CASCADE`
- FK `FK_7ca87463deeaafb78690a2c85a7`: `FOREIGN KEY (answer_id) REFERENCES answer(id) ON DELETE CASCADE`
- FK `FK_faef381e6b0b68a7aca90470223`: `FOREIGN KEY (question_option_id) REFERENCES question_option(id) ON DELETE CASCADE`
- FK `FK_matrix_row_option_id`: `FOREIGN KEY (matrix_row_option_id) REFERENCES question_option(id) ON DELETE CASCADE`
- FK `FK_question_pair_id`: `FOREIGN KEY (question_pair_id) REFERENCES question_pair(id) ON DELETE CASCADE`

**Indexes**

- `PK_0d091b145e793bfb1804827a5af`: `CREATE UNIQUE INDEX "PK_0d091b145e793bfb1804827a5af" ON public.answered_question_options USING btree (id)`
- `idx_aqo_answer_id`: `CREATE INDEX idx_aqo_answer_id ON public.answered_question_options USING btree (answer_id)`
- `idx_aqo_answer_matrix`: `CREATE INDEX idx_aqo_answer_matrix ON public.answered_question_options USING btree (answer_id, question_option_id, matrix_row_option_id) WHERE (matrix_row_option_id IS NOT NULL)`
- `idx_aqo_answer_option_composite`: `CREATE INDEX idx_aqo_answer_option_composite ON public.answered_question_options USING btree (answer_id, question_option_id)`
- `idx_aqo_answer_question_set`: `CREATE INDEX idx_aqo_answer_question_set ON public.answered_question_options USING btree (answer_id, question_set_id)`
- `idx_aqo_answerdata_gin`: `CREATE INDEX idx_aqo_answerdata_gin ON public.answered_question_options USING gin ("answerData")`
- `idx_aqo_answerdata_group`: `CREATE INDEX idx_aqo_answerdata_group ON public.answered_question_options USING btree ((("answerData" ->> 'group'::text)))`
- `idx_aqo_answerdata_label`: `CREATE INDEX idx_aqo_answerdata_label ON public.answered_question_options USING btree ((("answerData" ->> 'sampleLabel'::text)))`
- `idx_aqo_answerdata_samplelabel`: `CREATE INDEX idx_aqo_answerdata_samplelabel ON public.answered_question_options USING btree ((("answerData" ->> 'sampleLabel'::text)))`
- `idx_aqo_answerid_id_desc`: `CREATE INDEX idx_aqo_answerid_id_desc ON public.answered_question_options USING btree (answer_id, id DESC)`
- `idx_aqo_matrix_row_option_id`: `CREATE INDEX idx_aqo_matrix_row_option_id ON public.answered_question_options USING btree (matrix_row_option_id) WHERE (matrix_row_option_id IS NOT NULL)`
- `idx_aqo_qo_id`: `CREATE INDEX idx_aqo_qo_id ON public.answered_question_options USING btree (question_option_id)`
- `idx_aqo_question_pair_id`: `CREATE INDEX idx_aqo_question_pair_id ON public.answered_question_options USING btree (question_pair_id)`
- `idx_aqo_question_set_id`: `CREATE INDEX idx_aqo_question_set_id ON public.answered_question_options USING btree (question_set_id)`

### `application`

**Kind/grain:** table; Licensed application/module catalog entry.

**Snapshot:** 6 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"link" character varying(512)
"image_url" character varying(512)
"state" application_state_enum NOT NULL DEFAULT 'active'::application_state_enum
"is_admin" boolean NOT NULL DEFAULT false
"is_migrated" boolean NOT NULL DEFAULT false
"migrated_to_id" uuid
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"slug" character varying(256)
```

**Constraints and relationships**

- PK `PK_569e0c3e863ebdf5f2408ee1670`: `PRIMARY KEY (id)`
- UNIQUE `UQ_2b25e20c70b5908ad52a496d684`: `UNIQUE (slug)`
- UNIQUE `UQ_608bb41e7e1ef5f6d7abb07e394`: `UNIQUE (name)`
- FK `FK_019adac3496bb860afbd6c7ec95`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_4e1da9fc8efdc8733cfa3668be6`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_4f1b123d8e4cd1a380f807a5fd1`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_6505365b085bd70593fbd4c88e3`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- FK `FK_701c8a754b03a9ff5719f32d26d`: `FOREIGN KEY (migrated_to_id) REFERENCES application(id)`
- FK `FK_c8a899cc24e7c6aac5f7f4daeae`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `application` via `FK_701c8a754b03a9ff5719f32d26d`
  - `assigned_stakeholder` via `FK_cc07eee8dbdb35e966df367d324`
  - `feature_flags` via `FK_c89fad6113d040bbd572b28bbc6`
  - `license_app_data` via `FK_ba6a4c9b9c52d60c18593b28033`
  - `organization_applications` via `FK_59884a47823f64d334801caa6fb`
  - `permission` via `FK_b14d41d1e6ee3340a7109c34a97`
  - `role` via `FK_89fe3003757abd864d4ad720a1e`

**Indexes**

- `PK_569e0c3e863ebdf5f2408ee1670`: `CREATE UNIQUE INDEX "PK_569e0c3e863ebdf5f2408ee1670" ON public.application USING btree (id)`
- `UQ_2b25e20c70b5908ad52a496d684`: `CREATE UNIQUE INDEX "UQ_2b25e20c70b5908ad52a496d684" ON public.application USING btree (slug)`
- `UQ_608bb41e7e1ef5f6d7abb07e394`: `CREATE UNIQUE INDEX "UQ_608bb41e7e1ef5f6d7abb07e394" ON public.application USING btree (name)`

**Non-internal triggers**

- `audit_application_delete_trigger`: `CREATE TRIGGER audit_application_delete_trigger AFTER DELETE ON application FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_application_insert_trigger`: `CREATE TRIGGER audit_application_insert_trigger AFTER INSERT ON application FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_application_update_trigger`: `CREATE TRIGGER audit_application_update_trigger AFTER UPDATE ON application FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `assigned_stakeholder`

**Kind/grain:** table; Operator/stakeholder assignment to a typed tenancy module.

**Snapshot:** 53 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"module_type" assigned_stakeholder_module_type_enum NOT NULL
"type" assigned_stakeholder_type_enum NOT NULL DEFAULT 'stakeholder'::assigned_stakeholder_type_enum
"application_id" uuid
"module_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_cc7f0a22f6e419436afc8774dd8`: `PRIMARY KEY (id)`
- FK `FK_6c9997232ff0867f8948b315389`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_c5000948800ae9733f99e88a347`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_cc07eee8dbdb35e966df367d324`: `FOREIGN KEY (application_id) REFERENCES application(id)`
- FK `FK_f0dbd44431caedc781a884b860c`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `assigned_stakeholder_users` via `FK_2cb67e985295f61f7d3fa967834`

**Indexes**

- `PK_cc7f0a22f6e419436afc8774dd8`: `CREATE UNIQUE INDEX "PK_cc7f0a22f6e419436afc8774dd8" ON public.assigned_stakeholder USING btree (id)`

**Non-internal triggers**

- `audit_assigned_stakeholder_delete_trigger`: `CREATE TRIGGER audit_assigned_stakeholder_delete_trigger AFTER DELETE ON assigned_stakeholder FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_assigned_stakeholder_insert_trigger`: `CREATE TRIGGER audit_assigned_stakeholder_insert_trigger AFTER INSERT ON assigned_stakeholder FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_assigned_stakeholder_update_trigger`: `CREATE TRIGGER audit_assigned_stakeholder_update_trigger AFTER UPDATE ON assigned_stakeholder FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `assigned_stakeholder_users`

**Kind/grain:** table; Users attached to an assigned stakeholder record.

**Snapshot:** 278 rows; 144 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
"assigned_stakeholder_id" uuid NOT NULL
"user_id" uuid NOT NULL
"role_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_8f8c9050d8a07dc31b9394c3807`: `PRIMARY KEY (id)`
- FK `FK_2cb67e985295f61f7d3fa967834`: `FOREIGN KEY (assigned_stakeholder_id) REFERENCES assigned_stakeholder(id) ON DELETE CASCADE`
- FK `FK_316aa39eba1b9be5c669ebcc891`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_3f8a6e4ce419d2b6c2aa7b16d1c`: `FOREIGN KEY (role_id) REFERENCES role(id) ON DELETE CASCADE`
- FK `FK_5f0eccf261c268f673a28d082e4`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_94bb544239c1b112e2b74eb584e`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_c031a4acc39ae71b8c09bca8762`: `FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE`

**Indexes**

- `PK_8f8c9050d8a07dc31b9394c3807`: `CREATE UNIQUE INDEX "PK_8f8c9050d8a07dc31b9394c3807" ON public.assigned_stakeholder_users USING btree (id)`
- `IDX_assigned_stakeholder_user_active_unique`: `CREATE UNIQUE INDEX "IDX_assigned_stakeholder_user_active_unique" ON public.assigned_stakeholder_users USING btree (assigned_stakeholder_id, user_id, role_id) WHERE (deleted_at IS NULL)`

**Non-internal triggers**

- `audit_assigned_stakeholder_users_delete_trigger`: `CREATE TRIGGER audit_assigned_stakeholder_users_delete_trigger AFTER DELETE ON assigned_stakeholder_users FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_assigned_stakeholder_users_insert_trigger`: `CREATE TRIGGER audit_assigned_stakeholder_users_insert_trigger AFTER INSERT ON assigned_stakeholder_users FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_assigned_stakeholder_users_update_trigger`: `CREATE TRIGGER audit_assigned_stakeholder_users_update_trigger AFTER UPDATE ON assigned_stakeholder_users FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `audit_log`

**Kind/grain:** table; Current audit trail of row changes captured by database triggers.

**Snapshot:** 38,920 rows; 38 MB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"table_name" character varying(255) NOT NULL
"record_id" uuid NOT NULL
"old_data" jsonb
"new_data" jsonb
"changed_by" uuid
"changed_at" timestamp without time zone DEFAULT CURRENT_TIMESTAMP
"ip_address" character varying(45)
"user_agent" text
"impersonated_by" uuid
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"operation" character varying(32) NOT NULL
```

**Constraints and relationships**

- PK `audit_log_pkey`: `PRIMARY KEY (id)`

**Indexes**

- `audit_log_pkey`: `CREATE UNIQUE INDEX audit_log_pkey ON public.audit_log USING btree (id)`
- `idx_audit_log_changed_at`: `CREATE INDEX idx_audit_log_changed_at ON public.audit_log USING btree (changed_at)`
- `idx_audit_log_changed_by`: `CREATE INDEX idx_audit_log_changed_by ON public.audit_log USING btree (changed_by)`
- `idx_audit_log_operation`: `CREATE INDEX idx_audit_log_operation ON public.audit_log USING btree (operation)`
- `idx_audit_log_record_id`: `CREATE INDEX idx_audit_log_record_id ON public.audit_log USING btree (record_id)`
- `idx_audit_log_table_name`: `CREATE INDEX idx_audit_log_table_name ON public.audit_log USING btree (table_name)`
- `idx_audit_log_table_record_changed_at`: `CREATE INDEX idx_audit_log_table_record_changed_at ON public.audit_log USING btree (table_name, record_id, changed_at DESC)`

### `audit_log_archive`

**Kind/grain:** table; Archived audit trail rows.

**Snapshot:** 0 rows; 32 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"table_name" character varying(255) NOT NULL
"record_id" uuid NOT NULL
"old_data" jsonb
"new_data" jsonb
"changed_by" uuid
"changed_at" timestamp without time zone DEFAULT CURRENT_TIMESTAMP
"ip_address" character varying(45)
"user_agent" text
"impersonated_by" uuid
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"operation" character varying(32) NOT NULL
```

**Constraints and relationships**

- No database constraints declared.

**Indexes**

- `idx_audit_log_archive_id`: `CREATE UNIQUE INDEX idx_audit_log_archive_id ON public.audit_log_archive USING btree (id)`
- `idx_audit_log_archive_changed_at`: `CREATE INDEX idx_audit_log_archive_changed_at ON public.audit_log_archive USING btree (changed_at)`
- `idx_audit_log_archive_table_record_changed_at`: `CREATE INDEX idx_audit_log_archive_table_record_changed_at ON public.audit_log_archive USING btree (table_name, record_id, changed_at DESC)`

### `bank_account`

**Kind/grain:** table; Bank account metadata; sensitive financial data.

**Snapshot:** 24 rows; 32 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"bank_country" character varying(128)
"payment_method" character varying(128)
"account_holder_name" character varying(256)
"bank_name" character varying(256)
"currency" character varying(128)
"account_number" character varying(128)
"routing_number" character varying(128)
"account_type" character varying(128)
"swift_code" character varying(128)
"user_id" uuid
"counterparty_id" uuid
"cost_center_id" uuid
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_f3246deb6b79123482c6adb9745`: `PRIMARY KEY (id)`
- FK `FK_0e0f902f249f980e19adf79d98f`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_1245a87919ce95638c54f9c4ded`: `FOREIGN KEY (cost_center_id) REFERENCES cost_center(id) ON DELETE CASCADE`
- FK `FK_984f14dc58850cf4198a50a58b1`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_9dd66dbf034ade89c0dbbf2a8e8`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_bf924d4054a5b368ec69d0aa546`: `FOREIGN KEY (counterparty_id) REFERENCES counterparty(id) ON DELETE CASCADE`
- FK `FK_c8d57e8df596573a617476fdff2`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `cost_center_counterparty_bank` via `FK_e7b7ea67725360e83f1a14b35b9`
  - `license_clients` via `FK_8ef331db35b306dc6e543b670c8`

**Indexes**

- `PK_f3246deb6b79123482c6adb9745`: `CREATE UNIQUE INDEX "PK_f3246deb6b79123482c6adb9745" ON public.bank_account USING btree (id)`

### `benchmark_registry`

**Kind/grain:** table; Registry entry identifying benchmark source/category configuration.

**Snapshot:** 1 rows; 40 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"category_label" character varying(200) NOT NULL
"survey_id" uuid NOT NULL
"product_id" uuid NOT NULL
"internal_label" character varying(200)
"is_active" boolean NOT NULL DEFAULT true
"created_at" timestamp with time zone NOT NULL DEFAULT now()
```

**Constraints and relationships**

- PK `benchmark_registry_pkey`: `PRIMARY KEY (id)`
- FK `benchmark_registry_product_id_fkey`: `FOREIGN KEY (product_id) REFERENCES product(id)`
- FK `benchmark_registry_survey_id_fkey`: `FOREIGN KEY (survey_id) REFERENCES survey(id)`

**Indexes**

- `benchmark_registry_pkey`: `CREATE UNIQUE INDEX benchmark_registry_pkey ON public.benchmark_registry USING btree (id)`
- `idx_benchmark_registry_category`: `CREATE INDEX idx_benchmark_registry_category ON public.benchmark_registry USING btree (category_label) WHERE is_active`

### `branding`

**Kind/grain:** table; Brand/theme configuration.

**Snapshot:** 7 rows; 96 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT gen_random_uuid()
"createdAt" timestamp without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp without time zone NOT NULL DEFAULT now()
"account_id" uuid
"organization_id" uuid
"brand_name" character varying(128)
"logo_url" text
"favicon_url" text
"logo_display_mode" character varying(32)
"primary_color" character varying(32)
"secondary_color" character varying(32)
"page_bg_color" character varying(32)
"card_bg_color" character varying(32)
"text_color" character varying(32)
"button_text_color" character varying(32)
"font_family" character varying(64)
"button_style" character varying(16)
"show_header" boolean
"header_layout" character varying(64)
"header_background" character varying(32)
"show_progress_bar" character varying(16)
"show_footer" boolean
"footer_text" text
"show_powered_by_gpi" boolean
"builder_logo_url" text
"builder_app_bar_color" character varying(32)
"builder_sidebar_color" character varying(32)
"builder_nav_text_color" character varying(32)
"email_logo_url" text
"email_from_name" character varying(128)
"email_reply_to" character varying(256)
"email_footer_text" text
"report_logo_url" text
"report_footer_text" text
"report_accent_color" character varying(32)
"chart_palettes" jsonb
"created_by" uuid
"updated_by" uuid
```

**Constraints and relationships**

- PK `PK_branding`: `PRIMARY KEY (id)`
- UNIQUE `UQ_branding_account_id`: `UNIQUE (account_id)`
- UNIQUE `UQ_branding_organization_id`: `UNIQUE (organization_id)`
- FK `FK_branding_account`: `FOREIGN KEY (account_id) REFERENCES account(id) ON DELETE CASCADE`
- FK `FK_branding_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- CHECK `CHK_branding_single_owner`: `CHECK (account_id IS NOT NULL AND organization_id IS NULL OR account_id IS NULL AND organization_id IS NOT NULL)`

**Indexes**

- `PK_branding`: `CREATE UNIQUE INDEX "PK_branding" ON public.branding USING btree (id)`
- `UQ_branding_account_id`: `CREATE UNIQUE INDEX "UQ_branding_account_id" ON public.branding USING btree (account_id)`
- `UQ_branding_organization_id`: `CREATE UNIQUE INDEX "UQ_branding_organization_id" ON public.branding USING btree (organization_id)`
- `IDX_BRANDING_ACCOUNT_ID`: `CREATE INDEX "IDX_BRANDING_ACCOUNT_ID" ON public.branding USING btree (account_id)`
- `IDX_BRANDING_ORGANIZATION_ID`: `CREATE INDEX "IDX_BRANDING_ORGANIZATION_ID" ON public.branding USING btree (organization_id)`

### `branding_domains`

**Kind/grain:** table; Domain assignment to branding configuration.

**Snapshot:** 1 rows; 104 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT gen_random_uuid()
"createdAt" timestamp without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp without time zone NOT NULL DEFAULT now()
"account_id" uuid
"organization_id" uuid
"kind" character varying(16) NOT NULL DEFAULT 'convention'::character varying
"slug" character varying(63)
"host" character varying(255)
"app" character varying(16)
"status" character varying(16) NOT NULL DEFAULT 'active'::character varying
```

**Constraints and relationships**

- PK `PK_branding_domains`: `PRIMARY KEY (id)`
- FK `FK_branding_domains_account`: `FOREIGN KEY (account_id) REFERENCES account(id) ON DELETE CASCADE`
- FK `FK_branding_domains_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- CHECK `CHK_branding_domains_app`: `CHECK (app IS NULL OR (app::text = ANY (ARRAY['platform'::character varying::text, 'charts'::character varying::text, 'builder'::character varying::text, 'admin'::character varying::text, 'tester'::character varying::text])))`
- CHECK `CHK_branding_domains_kind`: `CHECK (kind::text = ANY (ARRAY['convention'::character varying::text, 'arbitrary'::character varying::text, 'custom'::character varying::text]))`
- CHECK `CHK_branding_domains_single_owner`: `CHECK (account_id IS NOT NULL AND organization_id IS NULL OR account_id IS NULL AND organization_id IS NOT NULL)`
- CHECK `CHK_branding_domains_status`: `CHECK (status::text = ANY (ARRAY['pending'::character varying::text, 'active'::character varying::text, 'disabled'::character varying::text]))`
- CHECK `CHK_branding_domains_target`: `CHECK (slug IS NOT NULL OR host IS NOT NULL)`

**Indexes**

- `PK_branding_domains`: `CREATE UNIQUE INDEX "PK_branding_domains" ON public.branding_domains USING btree (id)`
- `UQ_branding_domains_account_convention`: `CREATE UNIQUE INDEX "UQ_branding_domains_account_convention" ON public.branding_domains USING btree (account_id) WHERE (((kind)::text = 'convention'::text) AND (account_id IS NOT NULL))`
- `UQ_branding_domains_host`: `CREATE UNIQUE INDEX "UQ_branding_domains_host" ON public.branding_domains USING btree (lower((host)::text)) WHERE (host IS NOT NULL)`
- `UQ_branding_domains_org_convention`: `CREATE UNIQUE INDEX "UQ_branding_domains_org_convention" ON public.branding_domains USING btree (organization_id) WHERE (((kind)::text = 'convention'::text) AND (organization_id IS NOT NULL))`
- `UQ_branding_domains_slug`: `CREATE UNIQUE INDEX "UQ_branding_domains_slug" ON public.branding_domains USING btree (lower((slug)::text)) WHERE (slug IS NOT NULL)`
- `IDX_BRANDING_DOMAINS_ACCOUNT_ID`: `CREATE INDEX "IDX_BRANDING_DOMAINS_ACCOUNT_ID" ON public.branding_domains USING btree (account_id)`
- `IDX_BRANDING_DOMAINS_ORGANIZATION_ID`: `CREATE INDEX "IDX_BRANDING_DOMAINS_ORGANIZATION_ID" ON public.branding_domains USING btree (organization_id)`

### `chart_report_filter`

**Kind/grain:** table; One permanent or temporary JSON filter definition for a report.

**Snapshot:** 40 rows; 128 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"type" chart_report_filter_type_enum NOT NULL DEFAULT 'permanent'::chart_report_filter_type_enum
"filters" jsonb NOT NULL DEFAULT '{}'::jsonb
"deleted_at" timestamp without time zone
"chart_report_id" uuid NOT NULL
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_a47ce347ed1c8dfca75aeac328e`: `PRIMARY KEY (id)`
- FK `FK_1ae03850312b953cb623c42fd00`: `FOREIGN KEY (chart_report_id) REFERENCES charts_reports(id)`
- FK `FK_2def8ed7c177ab9f64a24901657`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_4a69854b34cc727ae67869a6263`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_6b6d873aac7ead4292a633342f7`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_a47ce347ed1c8dfca75aeac328e`: `CREATE UNIQUE INDEX "PK_a47ce347ed1c8dfca75aeac328e" ON public.chart_report_filter USING btree (id)`

### `chart_settings_template`

**Kind/grain:** table; Reusable saved chart settings/configuration template.

**Snapshot:** 71 rows; 128 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying NOT NULL
"description" text
"reportSettings" jsonb
"owner_id" uuid
```

**Constraints and relationships**

- PK `PK_e8cb81e5f58c80a3d0c4d8b5d0f`: `PRIMARY KEY (id)`
- FK `FK_eefae7a71d5aa769ae68d225521`: `FOREIGN KEY (owner_id) REFERENCES "user"(id) ON DELETE SET NULL`

**Indexes**

- `PK_e8cb81e5f58c80a3d0c4d8b5d0f`: `CREATE UNIQUE INDEX "PK_e8cb81e5f58c80a3d0c4d8b5d0f" ON public.chart_settings_template USING btree (id)`

**Non-internal triggers**

- `audit_chart_settings_template_delete_trigger`: `CREATE TRIGGER audit_chart_settings_template_delete_trigger AFTER DELETE ON chart_settings_template FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_chart_settings_template_insert_trigger`: `CREATE TRIGGER audit_chart_settings_template_insert_trigger AFTER INSERT ON chart_settings_template FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_chart_settings_template_update_trigger`: `CREATE TRIGGER audit_chart_settings_template_update_trigger AFTER UPDATE ON chart_settings_template FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `charts`

**Kind/grain:** table; One saved chart definition linked to a report tab and optionally a question.

**Snapshot:** 5,792 rows; 1776 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"title" character varying
"chartType" charts_charttype_enum NOT NULL
"tab_id" uuid
"question_id" uuid
"settings" jsonb
```

**Constraints and relationships**

- PK `PK_fa7124425552d2d37725307008b`: `PRIMARY KEY (id)`
- FK `FK_34fe8ba9779c07769f6d451a997`: `FOREIGN KEY (tab_id) REFERENCES charts_tabs(id) ON DELETE CASCADE`
- FK `FK_5f1eb36c6c815de9f992a26fdf9`: `FOREIGN KEY (question_id) REFERENCES question(id)`

**Indexes**

- `PK_fa7124425552d2d37725307008b`: `CREATE UNIQUE INDEX "PK_fa7124425552d2d37725307008b" ON public.charts USING btree (id)`

### `charts_reports`

**Kind/grain:** table; One saved report definition for a survey; configuration, not respondent observations.

**Snapshot:** 337 rows; 216 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"photo" character varying DEFAULT ''::character varying -- cover photo
"photoStyle" character varying DEFAULT ''::character varying -- cover photo
"name" character varying NOT NULL
"reportSettings" jsonb
"folderId" uuid -- ChartsReport folder id
"creatorId" uuid -- ChartsReport creator id.
"survey_id" uuid
"description" text
"includePartialEnrollments" boolean NOT NULL DEFAULT false
"organization_id" uuid
"settings" jsonb DEFAULT '{}'::jsonb
"archived_at" timestamp without time zone
"archived_by" uuid
```

**Constraints and relationships**

- PK `PK_c3da30254b535e0e5d8cc92c3b5`: `PRIMARY KEY (id)`
- FK `FK_6cb35d5dcd56c280d814274af9a`: `FOREIGN KEY ("folderId") REFERENCES folder(id)`
- FK `FK_806306c3c01620f97e553560c43`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_a1f184f703a838e67272a3c6f3b`: `FOREIGN KEY ("creatorId") REFERENCES "user"(id)`
- FK `FK_a8b81ebd3f40f284625c71f69b6`: `FOREIGN KEY (survey_id) REFERENCES survey(id)`
- FK `FK_ea2b471ab4507643af0735b8d0d`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- Incoming foreign keys:
  - `chart_report_filter` via `FK_1ae03850312b953cb623c42fd00`
  - `charts_tabs` via `FK_33a7cb504b8824b4cc38bad39e8`

**Indexes**

- `PK_c3da30254b535e0e5d8cc92c3b5`: `CREATE UNIQUE INDEX "PK_c3da30254b535e0e5d8cc92c3b5" ON public.charts_reports USING btree (id)`
- `idx_charts_reports_folder`: `CREATE INDEX idx_charts_reports_folder ON public.charts_reports USING btree ("folderId")`
- `idx_charts_reports_survey`: `CREATE INDEX idx_charts_reports_survey ON public.charts_reports USING btree (survey_id)`

### `charts_tabs`

**Kind/grain:** table; One ordered tab/layout within a saved report.

**Snapshot:** 1,202 rows; 424 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying NOT NULL
"order" integer DEFAULT 0
"layout" json
"report_id" uuid
"organization_id" uuid
```

**Constraints and relationships**

- PK `PK_6c3ea2d7359fec3ee1da6b051bb`: `PRIMARY KEY (id)`
- FK `FK_33a7cb504b8824b4cc38bad39e8`: `FOREIGN KEY (report_id) REFERENCES charts_reports(id) ON DELETE CASCADE`
- FK `FK_975b4a90d3e8ff936aa5bda4667`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- Incoming foreign keys:
  - `aggregation_report_tabs` via `FK_aggregation_report_tabs_report_tab`
  - `charts` via `FK_34fe8ba9779c07769f6d451a997`

**Indexes**

- `PK_6c3ea2d7359fec3ee1da6b051bb`: `CREATE UNIQUE INDEX "PK_6c3ea2d7359fec3ee1da6b051bb" ON public.charts_tabs USING btree (id)`

### `client`

**Kind/grain:** table; Client entity under a customer; account.client_id is the client link used in tenant verification.

**Snapshot:** 23 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(128)
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"customer_id" uuid
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"code" character varying(128)
"is_draft" boolean NOT NULL DEFAULT false
"is_benchmark_source" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_96da49381769303a6515a8785c7`: `PRIMARY KEY (id)`
- UNIQUE `UQ_3331ba630bece961ed0e0ab1dc2`: `UNIQUE (code)`
- FK `FK_46d6988875665c576a9c283d466`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_9a4f34ecfaea49e406fa2cca2a8`: `FOREIGN KEY (customer_id) REFERENCES customer(id)`
- FK `FK_b9012705b3b257eac64edd92a54`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_dfe832d3ba6908918f5446b5c7e`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_e1eb16471c8e431c63885b9c5d5`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_f1bebb11f4584e3ea8ce25f7093`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `account` via `FK_45a1670bb3c856a6ba08ec3c737`
  - `client_cost_centers` via `FK_cee8eb68a72533f2d0547975397`
  - `client_info` via `FK_5efe707d00b01f9eaffd744511d`
  - `image_doc` via `FK_7eca881b4d7eff0727fd237949c`
  - `license_clients` via `FK_aa6f9b553b672dfa268c3865506`

**Indexes**

- `PK_96da49381769303a6515a8785c7`: `CREATE UNIQUE INDEX "PK_96da49381769303a6515a8785c7" ON public.client USING btree (id)`
- `UQ_3331ba630bece961ed0e0ab1dc2`: `CREATE UNIQUE INDEX "UQ_3331ba630bece961ed0e0ab1dc2" ON public.client USING btree (code)`
- `IDX_CLIENT_NAME`: `CREATE INDEX "IDX_CLIENT_NAME" ON public.client USING btree (name)`

**Non-internal triggers**

- `audit_client_delete_trigger`: `CREATE TRIGGER audit_client_delete_trigger AFTER DELETE ON client FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_client_insert_trigger`: `CREATE TRIGGER audit_client_insert_trigger AFTER INSERT ON client FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_client_update_trigger`: `CREATE TRIGGER audit_client_update_trigger AFTER UPDATE ON client FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `client_cost_centers`

**Kind/grain:** table; Assignment join between clients and cost centers.

**Snapshot:** 20 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"client_id" uuid NOT NULL
"cost_center_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_56c135e31bbb7db7b2f29b07301`: `PRIMARY KEY (client_id, cost_center_id)`
- FK `FK_1e5321612c818ba0de5e7c801ba`: `FOREIGN KEY (cost_center_id) REFERENCES cost_center(id)`
- FK `FK_cee8eb68a72533f2d0547975397`: `FOREIGN KEY (client_id) REFERENCES client(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_56c135e31bbb7db7b2f29b07301`: `CREATE UNIQUE INDEX "PK_56c135e31bbb7db7b2f29b07301" ON public.client_cost_centers USING btree (client_id, cost_center_id)`
- `IDX_1e5321612c818ba0de5e7c801b`: `CREATE INDEX "IDX_1e5321612c818ba0de5e7c801b" ON public.client_cost_centers USING btree (cost_center_id)`
- `IDX_cee8eb68a72533f2d054797539`: `CREATE INDEX "IDX_cee8eb68a72533f2d054797539" ON public.client_cost_centers USING btree (client_id)`

### `client_info`

**Kind/grain:** table; Typed client billing/finance information.

**Snapshot:** 1 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" uuid NOT NULL
"type" client_info_type_enum NOT NULL
"client_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_09bdc12b41c346ad56afee8d6cc`: `PRIMARY KEY (id)`
- FK `FK_3c4e527c8c37345efc91a11aa7b`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_5efe707d00b01f9eaffd744511d`: `FOREIGN KEY (client_id) REFERENCES client(id) ON DELETE CASCADE`
- FK `FK_9c2cf45d2070dfb5ff6a3230b08`: `FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE`
- FK `FK_c57956bd79b37f813440429b908`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_d4e75fec3beaf73f1fdf74a1f1d`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`

**Indexes**

- `PK_09bdc12b41c346ad56afee8d6cc`: `CREATE UNIQUE INDEX "PK_09bdc12b41c346ad56afee8d6cc" ON public.client_info USING btree (id)`

**Non-internal triggers**

- `audit_client_info_trigger`: `CREATE TRIGGER audit_client_info_trigger AFTER INSERT OR DELETE OR UPDATE ON client_info FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`

### `conversation_history`

**Kind/grain:** table; Stored assistant/conversation history; currently outside the survey response model.

**Snapshot:** 0 rows; 40 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT gen_random_uuid()
"session_id" character varying(128) NOT NULL
"survey_id" character varying(128) NOT NULL
"organization_id" character varying(128) NOT NULL
"role" character varying(20) NOT NULL
"content" text NOT NULL
"metadata" jsonb DEFAULT '{}'::jsonb
"created_at" timestamp with time zone DEFAULT now()
"expires_at" timestamp with time zone
```

**Constraints and relationships**

- PK `conversation_history_pkey`: `PRIMARY KEY (id)`
- CHECK `conversation_history_role_check`: `CHECK (role::text = ANY (ARRAY['user'::character varying::text, 'assistant'::character varying::text]))`

**Indexes**

- `conversation_history_pkey`: `CREATE UNIQUE INDEX conversation_history_pkey ON public.conversation_history USING btree (id)`
- `idx_conversation_history_expires_at`: `CREATE INDEX idx_conversation_history_expires_at ON public.conversation_history USING btree (expires_at)`
- `idx_conversation_history_session_created`: `CREATE INDEX idx_conversation_history_session_created ON public.conversation_history USING btree (session_id, created_at DESC)`
- `idx_conversation_history_session_id`: `CREATE INDEX idx_conversation_history_session_id ON public.conversation_history USING btree (session_id)`

### `cost_center`

**Kind/grain:** table; Cost-center entity in the billing hierarchy.

**Snapshot:** 21 rows; 48 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" uuid
"registration_country" character varying(128)
"registration_number" character varying(256)
"tax_id" character varying(256)
"billing_currency" character varying(128)
"invoice_net_days" character varying(128)
"invoice_delivery_method" character varying(128)
"invoice_delivery_method_instruction" character varying(128)
"invoice_email" character varying(128)
"invoice_additional_instructions" character varying(1024)
"code" character varying(120) NOT NULL
"is_draft" boolean NOT NULL DEFAULT false
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"recipient_id" uuid
"sender_id" uuid
"created_by" uuid
"updated_by" uuid
"archived_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_814d737123e3a42d0a37e97b393`: `PRIMARY KEY (id)`
- UNIQUE `UQ_COST_CENTER_CODE`: `UNIQUE (code)`
- FK `FK_10ca08806566762ead94b140696`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_43de2c7154803f634c70b1f0a0b`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`
- FK `FK_84ae71506ca71aca2d4453efc89`: `FOREIGN KEY (sender_id) REFERENCES "user"(id)`
- FK `FK_9c3792fbdc8eeaf6f11d456bce6`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_c1c205453040e3e2f438c1d45ed`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_db2d5e3b9345440265ae5273533`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_f11f0fc853e5a4c9ab776013986`: `FOREIGN KEY (recipient_id) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `bank_account` via `FK_1245a87919ce95638c54f9c4ded`
  - `client_cost_centers` via `FK_1e5321612c818ba0de5e7c801ba`
  - `cost_center_counterparty_bank` via `FK_a0b90b23e27e60f666cfaf85214`
  - `image_doc` via `FK_a9717e8bf977a125d5b23eebfd6`
  - `invoice_portal` via `FK_1841161ff517d93b87ae94c7508`
  - `license_clients` via `FK_da0e6f529b87dab061f6388dad8`

**Indexes**

- `PK_814d737123e3a42d0a37e97b393`: `CREATE UNIQUE INDEX "PK_814d737123e3a42d0a37e97b393" ON public.cost_center USING btree (id)`
- `UQ_COST_CENTER_CODE`: `CREATE UNIQUE INDEX "UQ_COST_CENTER_CODE" ON public.cost_center USING btree (code)`

### `cost_center_counterparty_bank`

**Kind/grain:** table; Join/configuration connecting cost center, counterparty, and bank data.

**Snapshot:** 21 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"cost_center_id" uuid NOT NULL
"bank_account_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_e93151a1020b27c4a95a83f731b`: `PRIMARY KEY (id)`
- FK `FK_5624ae3958c3521e48d6a5db271`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_a0b90b23e27e60f666cfaf85214`: `FOREIGN KEY (cost_center_id) REFERENCES cost_center(id) ON DELETE CASCADE`
- FK `FK_e4aaa4bf098b5e0aec2f7bbaa2f`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_e7b7ea67725360e83f1a14b35b9`: `FOREIGN KEY (bank_account_id) REFERENCES bank_account(id) ON DELETE CASCADE`
- FK `FK_f2dd73ee9da279e2c4bd9b65b6c`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_e93151a1020b27c4a95a83f731b`: `CREATE UNIQUE INDEX "PK_e93151a1020b27c4a95a83f731b" ON public.cost_center_counterparty_bank USING btree (id)`

### `counterparty`

**Kind/grain:** table; Billing/payment counterparty entity.

**Snapshot:** 21 rows; 48 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"registration_country" character varying(128)
"registration_number" character varying(256)
"tax_id" character varying(256)
"billing_currency" character varying(128)
"code" character varying(120) NOT NULL
"is_draft" boolean NOT NULL DEFAULT false
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"user_id" uuid
"created_by" uuid
"updated_by" uuid
"archived_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_7c8af5f1b9f320f986d2a5a43ae`: `PRIMARY KEY (id)`
- UNIQUE `UQ_COUNTERPARTY_CODE`: `UNIQUE (code)`
- FK `FK_0d1b1d1d2a3028ffc97109c92d4`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_10b3bd35f4d86646491db9a19c9`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`
- FK `FK_291d01c45189f6933e07a3101d0`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_816913cf48c2d9e1c7ebc9b6078`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_c09c7c105edbda5378fe4652dce`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `bank_account` via `FK_bf924d4054a5b368ec69d0aa546`
  - `license_clients` via `FK_82dc6220a0b5704bf34f088c77e`

**Indexes**

- `PK_7c8af5f1b9f320f986d2a5a43ae`: `CREATE UNIQUE INDEX "PK_7c8af5f1b9f320f986d2a5a43ae" ON public.counterparty USING btree (id)`
- `UQ_COUNTERPARTY_CODE`: `CREATE UNIQUE INDEX "UQ_COUNTERPARTY_CODE" ON public.counterparty USING btree (code)`

### `customer`

**Kind/grain:** table; Top-level commercial customer entity.

**Snapshot:** 21 rows; 48 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"code" character varying(128) NOT NULL
"about" text
"attachment_id" uuid
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"is_draft" boolean NOT NULL DEFAULT false
"user_id" uuid
```

**Constraints and relationships**

- PK `PK_a7a13f4cacb744524e44dfdad32`: `PRIMARY KEY (id)`
- UNIQUE `UQ_CUSTOMER_CODE`: `UNIQUE (code)`
- FK `FK_16adddc7ee23a8be8198c3f5a0d`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_4b9208bd48abfe9a809a60e9797`: `FOREIGN KEY (attachment_id) REFERENCES image_doc(id)`
- FK `FK_5d1f609371a285123294fddcf3a`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`
- FK `FK_81f42e2cdfa36f5bf134e4f9ae5`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_84235f5c9358c7de7a8cdc5da48`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_a0fa0161e4f246c12d88257a99b`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- FK `FK_e146e7fa4df2cd2817bac7b2c53`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `account` via `FK_977b5abdf1370566eaade16eaa9`
  - `client` via `FK_9a4f34ecfaea49e406fa2cca2a8`

**Indexes**

- `PK_a7a13f4cacb744524e44dfdad32`: `CREATE UNIQUE INDEX "PK_a7a13f4cacb744524e44dfdad32" ON public.customer USING btree (id)`
- `UQ_CUSTOMER_CODE`: `CREATE UNIQUE INDEX "UQ_CUSTOMER_CODE" ON public.customer USING btree (code)`

**Non-internal triggers**

- `audit_customer_delete_trigger`: `CREATE TRIGGER audit_customer_delete_trigger AFTER DELETE ON customer FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_customer_insert_trigger`: `CREATE TRIGGER audit_customer_insert_trigger AFTER INSERT ON customer FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_customer_update_trigger`: `CREATE TRIGGER audit_customer_update_trigger AFTER UPDATE ON customer FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `document`

**Kind/grain:** table; Generic document storage metadata.

**Snapshot:** 0 rows; 16 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"clientGeneratedId" character varying NOT NULL DEFAULT ''::character varying -- Client generated id that helps identify the document before it is assigned a unique id
"documentUrl" character varying -- Document URL
"surveyId" uuid
```

**Constraints and relationships**

- PK `PK_e57d3357f83f3cdc0acffc3d777`: `PRIMARY KEY (id)`
- FK `FK_0f79cd5864eb064ad9f760665f1`: `FOREIGN KEY ("surveyId") REFERENCES survey(id)`

**Indexes**

- `PK_e57d3357f83f3cdc0acffc3d777`: `CREATE UNIQUE INDEX "PK_e57d3357f83f3cdc0acffc3d777" ON public.document USING btree (id)`

### `enrollment`

**Kind/grain:** table; One participation instance (the default respondent grain) in a survey.

**Snapshot:** 39,097 rows; 8232 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"finished_time" timestamp without time zone
"enrollment_status" enrollment_enrollment_status_enum NOT NULL DEFAULT 'active'::enrollment_enrollment_status_enum
"user_id" uuid
"last_answered_question_id" uuid
"survey_id" uuid
"legacy_enrollment_id" character varying
"legacy_survey_id" character varying
"legacy_screener_id" character varying
"auth_code" character varying
"panel_code_id" uuid
"panelist_id" uuid
```

**Constraints and relationships**

- PK `PK_7e200c699fa93865cdcdd025885`: `PRIMARY KEY (id)`
- FK `FK_3629439ed429cc65146f2422391`: `FOREIGN KEY (panel_code_id) REFERENCES panel_code(id)`
- FK `FK_3a32bd4f71ca65546cf7a751b8e`: `FOREIGN KEY (survey_id) REFERENCES survey(id)`
- FK `FK_c5f24b40db5f5d9f459dcc4638c`: `FOREIGN KEY (last_answered_question_id) REFERENCES question(id)`
- FK `FK_cd79fafd31d1d02722a4047ba67`: `FOREIGN KEY (panelist_id) REFERENCES panelist(id)`
- FK `FK_fc17c7e94154a17e767b7674f12`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `answer` via `FK_ed1203afd9e56d1e6319a0e6475`
  - `enrollment_recaptcha_verification` via `FK_enrollment_recaptcha_verification_enrollment`
  - `survey_panel_stats` via `FK_sps_enrollment`

**Indexes**

- `PK_7e200c699fa93865cdcdd025885`: `CREATE UNIQUE INDEX "PK_7e200c699fa93865cdcdd025885" ON public.enrollment USING btree (id)`
- `idx_enrollment_createdat`: `CREATE INDEX idx_enrollment_createdat ON public.enrollment USING btree ("createdAt")`
- `idx_enrollment_survey_status`: `CREATE INDEX idx_enrollment_survey_status ON public.enrollment USING btree (survey_id, enrollment_status)`
- `idx_enrollment_user_id`: `CREATE INDEX idx_enrollment_user_id ON public.enrollment USING btree (user_id)`

### `enrollment_recaptcha_verification`

**Kind/grain:** table; One reCAPTCHA verification attempt associated with enrollment submission.

**Snapshot:** 6 rows; 48 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"enrollment_id" uuid NOT NULL
"recaptcha_enabled" boolean NOT NULL DEFAULT false
"recaptcha_provider" character varying(32)
"recaptcha_status" recaptcha_verification_status_enum
"recaptcha_score" double precision
"recaptcha_action" recaptcha_action_enum
"recaptcha_verified_at" timestamp with time zone
"is_suspicious" boolean NOT NULL DEFAULT false
"failure_reason" text
```

**Constraints and relationships**

- PK `PK_enrollment_recaptcha_verification`: `PRIMARY KEY (id)`
- UNIQUE `UQ_enrollment_recaptcha_verification_enrollment_id`: `UNIQUE (enrollment_id)`
- FK `FK_enrollment_recaptcha_verification_enrollment`: `FOREIGN KEY (enrollment_id) REFERENCES enrollment(id) ON DELETE CASCADE`

**Indexes**

- `PK_enrollment_recaptcha_verification`: `CREATE UNIQUE INDEX "PK_enrollment_recaptcha_verification" ON public.enrollment_recaptcha_verification USING btree (id)`
- `UQ_enrollment_recaptcha_verification_enrollment_id`: `CREATE UNIQUE INDEX "UQ_enrollment_recaptcha_verification_enrollment_id" ON public.enrollment_recaptcha_verification USING btree (enrollment_id)`

### `exports`

**Kind/grain:** table; One export job/artifact metadata record.

**Snapshot:** 314 rows; 152 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL
"survey_name" character varying NOT NULL
"export_type" character varying NOT NULL
"report_names" json NOT NULL
"send_to" json
"send_to_type" character varying NOT NULL
"time_and_date" timestamp with time zone DEFAULT now()
"s3_url" character varying
"user_id" uuid NOT NULL
"status" character varying(50) NOT NULL DEFAULT 'Active'::character varying
"expiry_date" timestamp with time zone NOT NULL DEFAULT (now() + '7 days'::interval)
"s3_key" character varying
"survey_id" uuid NOT NULL
"survey_type" character varying
"encrypted_password" text
"password_enabled" boolean NOT NULL DEFAULT false
"password_updated_at" timestamp with time zone
```

**Constraints and relationships**

- PK `exports_pkey`: `PRIMARY KEY (id)`

**Indexes**

- `exports_pkey`: `CREATE UNIQUE INDEX exports_pkey ON public.exports USING btree (id)`
- `ix_exports_user_id`: `CREATE INDEX ix_exports_user_id ON public.exports USING btree (user_id)`

### `external_api_stats`

**Kind/grain:** table; Usage/status statistics for an external API integration.

**Snapshot:** 1 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"date" timestamp without time zone NOT NULL
"user_private_key_id" uuid NOT NULL
"api_request_url" character varying(1024) NOT NULL
"data" jsonb
```

**Constraints and relationships**

- PK `PK_external_api_stats_id`: `PRIMARY KEY (id)`
- FK `FK_external_api_stats_user_private_key`: `FOREIGN KEY (user_private_key_id) REFERENCES user_private_keys(id) ON DELETE CASCADE`

**Indexes**

- `PK_external_api_stats_id`: `CREATE UNIQUE INDEX "PK_external_api_stats_id" ON public.external_api_stats USING btree (id)`
- `IDX_external_api_stats_date`: `CREATE INDEX "IDX_external_api_stats_date" ON public.external_api_stats USING btree (date)`
- `IDX_external_api_stats_user_private_key_id`: `CREATE INDEX "IDX_external_api_stats_user_private_key_id" ON public.external_api_stats USING btree (user_private_key_id)`

### `feature_flag_scopes`

**Kind/grain:** table; Scope assignments for feature flags.

**Snapshot:** 3 rows; 104 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT gen_random_uuid()
"createdAt" timestamp without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp without time zone NOT NULL DEFAULT now()
"feature_flag_id" uuid NOT NULL
"account_id" uuid
"organization_id" uuid
"is_enabled" boolean NOT NULL
"created_by" uuid
"updated_by" uuid
```

**Constraints and relationships**

- PK `PK_feature_flag_scopes`: `PRIMARY KEY (id)`
- FK `FK_feature_flag_scopes_account`: `FOREIGN KEY (account_id) REFERENCES account(id) ON DELETE CASCADE`
- FK `FK_feature_flag_scopes_flag`: `FOREIGN KEY (feature_flag_id) REFERENCES feature_flags(id) ON DELETE CASCADE`
- FK `FK_feature_flag_scopes_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- CHECK `CHK_feature_flag_scope_single_owner`: `CHECK (account_id IS NOT NULL AND organization_id IS NULL OR account_id IS NULL AND organization_id IS NOT NULL)`

**Indexes**

- `PK_feature_flag_scopes`: `CREATE UNIQUE INDEX "PK_feature_flag_scopes" ON public.feature_flag_scopes USING btree (id)`
- `UQ_feature_flag_scope_account`: `CREATE UNIQUE INDEX "UQ_feature_flag_scope_account" ON public.feature_flag_scopes USING btree (feature_flag_id, account_id) WHERE (account_id IS NOT NULL)`
- `UQ_feature_flag_scope_organization`: `CREATE UNIQUE INDEX "UQ_feature_flag_scope_organization" ON public.feature_flag_scopes USING btree (feature_flag_id, organization_id) WHERE (organization_id IS NOT NULL)`
- `IDX_FEATURE_FLAG_SCOPE_ACCOUNT_ID`: `CREATE INDEX "IDX_FEATURE_FLAG_SCOPE_ACCOUNT_ID" ON public.feature_flag_scopes USING btree (account_id)`
- `IDX_FEATURE_FLAG_SCOPE_FLAG_ID`: `CREATE INDEX "IDX_FEATURE_FLAG_SCOPE_FLAG_ID" ON public.feature_flag_scopes USING btree (feature_flag_id)`
- `IDX_FEATURE_FLAG_SCOPE_ORGANIZATION_ID`: `CREATE INDEX "IDX_FEATURE_FLAG_SCOPE_ORGANIZATION_ID" ON public.feature_flag_scopes USING btree (organization_id)`

### `feature_flag_users`

**Kind/grain:** table; User assignments for restricted feature flags.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" character varying NOT NULL
"featureFlagId" uuid
"userId" uuid
```

**Constraints and relationships**

- PK `PK_3c9d77726a357c09c5bc94941c9`: `PRIMARY KEY (id)`
- UNIQUE `UQ_34cefbef8802634b1272795f44f`: `UNIQUE ("featureFlagId", user_id)`
- FK `FK_c8dcf0891354c33a13f2f209eac`: `FOREIGN KEY ("userId") REFERENCES "user"(id) ON DELETE CASCADE`
- FK `FK_e93e3d4d9f0e2b4b1a829f30862`: `FOREIGN KEY ("featureFlagId") REFERENCES feature_flags(id) ON DELETE CASCADE`

**Indexes**

- `PK_3c9d77726a357c09c5bc94941c9`: `CREATE UNIQUE INDEX "PK_3c9d77726a357c09c5bc94941c9" ON public.feature_flag_users USING btree (id)`
- `UQ_34cefbef8802634b1272795f44f`: `CREATE UNIQUE INDEX "UQ_34cefbef8802634b1272795f44f" ON public.feature_flag_users USING btree ("featureFlagId", user_id)`

### `feature_flags`

**Kind/grain:** table; Feature flag definition and open/closed/restricted state.

**Snapshot:** 1 rows; 48 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"unique_name" character varying NOT NULL
"application_id" uuid NOT NULL
"parent_route" character varying
"status" feature_flags_status_enum NOT NULL DEFAULT 'open'::feature_flags_status_enum
"is_enabled" boolean NOT NULL DEFAULT true
"description" text
```

**Constraints and relationships**

- PK `PK_db657d344e9caacfc9d5cf8bbac`: `PRIMARY KEY (id)`
- UNIQUE `UQ_119347b8923799cf009174508c8`: `UNIQUE (unique_name)`
- FK `FK_c89fad6113d040bbd572b28bbc6`: `FOREIGN KEY (application_id) REFERENCES application(id)`
- Incoming foreign keys:
  - `feature_flag_scopes` via `FK_feature_flag_scopes_flag`
  - `feature_flag_users` via `FK_e93e3d4d9f0e2b4b1a829f30862`

**Indexes**

- `PK_db657d344e9caacfc9d5cf8bbac`: `CREATE UNIQUE INDEX "PK_db657d344e9caacfc9d5cf8bbac" ON public.feature_flags USING btree (id)`
- `UQ_119347b8923799cf009174508c8`: `CREATE UNIQUE INDEX "UQ_119347b8923799cf009174508c8" ON public.feature_flags USING btree (unique_name)`

### `folder`

**Kind/grain:** table; Folder/tree node for surveys, templates, and reports.

**Snapshot:** 382 rows; 136 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"photo" character varying DEFAULT ''::character varying -- cover photo
"photoStyle" character varying DEFAULT ''::character varying -- cover photo
"name" character varying NOT NULL
"state" folder_state_enum DEFAULT 'active'::folder_state_enum
"type" character varying DEFAULT 'folder'::character varying
"organization_id" uuid -- Folder owner id
"lastModifierId" uuid -- Survey last modifier id
"parentId" uuid -- Parent folder id
"creatorId" uuid -- Folder creator id
"legacy_folder_id" character varying
"archived_at" timestamp without time zone
"archived_by" uuid
```

**Constraints and relationships**

- PK `PK_6278a41a706740c94c02e288df8`: `PRIMARY KEY (id)`
- FK `FK_10670d70f3e474c3de3ce53cfb7`: `FOREIGN KEY ("lastModifierId") REFERENCES "user"(id)`
- FK `FK_4d6ef3409b06099753ed80f08f9`: `FOREIGN KEY ("creatorId") REFERENCES "user"(id)`
- FK `FK_9ee3bd0f189fb242d488c0dfa39`: `FOREIGN KEY ("parentId") REFERENCES folder(id) ON DELETE CASCADE`
- FK `FK_d008e5cb74c1e1f272038e4db49`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_e09b8e7d4818dd263dde45bbecb`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_fd89c6b76b9f8bdd18c3b8aa30a`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- Incoming foreign keys:
  - `charts_reports` via `FK_6cb35d5dcd56c280d814274af9a`
  - `folder` via `FK_9ee3bd0f189fb242d488c0dfa39`
  - `folder_closure` via `FK_073b49e13e4ebe5c294443a16b4`, `FK_fe732379b449e3dc89f52b8b441`
  - `survey` via `FK_7982db0113f0d01664e9e79a43a`

**Indexes**

- `PK_6278a41a706740c94c02e288df8`: `CREATE UNIQUE INDEX "PK_6278a41a706740c94c02e288df8" ON public.folder USING btree (id)`
- `idx_folder_parent`: `CREATE INDEX idx_folder_parent ON public.folder USING btree ("parentId")`

**Non-internal triggers**

- `audit_folder_delete_trigger`: `CREATE TRIGGER audit_folder_delete_trigger AFTER DELETE ON folder FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_folder_insert_trigger`: `CREATE TRIGGER audit_folder_insert_trigger AFTER INSERT ON folder FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_folder_update_trigger`: `CREATE TRIGGER audit_folder_update_trigger AFTER UPDATE ON folder FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `folder_closure`

**Kind/grain:** table; Ancestor/descendant transitive closure for the folder hierarchy.

**Snapshot:** 792 rows; 200 kB total relation size; RLS not enabled.

**Columns**

```text
"id_ancestor" uuid NOT NULL
"id_descendant" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_ea15eee69fbb40458fe66545447`: `PRIMARY KEY (id_ancestor, id_descendant)`
- FK `FK_073b49e13e4ebe5c294443a16b4`: `FOREIGN KEY (id_ancestor) REFERENCES folder(id) ON DELETE CASCADE`
- FK `FK_fe732379b449e3dc89f52b8b441`: `FOREIGN KEY (id_descendant) REFERENCES folder(id) ON DELETE CASCADE`

**Indexes**

- `PK_ea15eee69fbb40458fe66545447`: `CREATE UNIQUE INDEX "PK_ea15eee69fbb40458fe66545447" ON public.folder_closure USING btree (id_ancestor, id_descendant)`
- `IDX_073b49e13e4ebe5c294443a16b`: `CREATE INDEX "IDX_073b49e13e4ebe5c294443a16b" ON public.folder_closure USING btree (id_ancestor)`
- `IDX_fe732379b449e3dc89f52b8b44`: `CREATE INDEX "IDX_fe732379b449e3dc89f52b8b44" ON public.folder_closure USING btree (id_descendant)`

### `image_doc`

**Kind/grain:** table; Uploaded image/file attachment metadata referenced by survey design entities.

**Snapshot:** 961 rows; 576 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"url" character varying NOT NULL DEFAULT ''::character varying -- The image file name on AWS s3
"name" character varying NOT NULL DEFAULT ''::character varying -- The image name
"userId" uuid -- ImageDoc user id
"type" character varying NOT NULL DEFAULT 'image'::character varying -- Attachment type
"module_name" character varying -- Module of the attachment
"organization_id" uuid
"surveyId" uuid -- Survey id
"license_id" uuid
"client_id" uuid
"cost_center_id" uuid
"account_id" uuid
"mapping_key" character varying(256)
```

**Constraints and relationships**

- PK `PK_177ae50bcb9a11b774b38798936`: `PRIMARY KEY (id)`
- FK `FK_3610c56c67e1431596970004b26`: `FOREIGN KEY ("surveyId") REFERENCES survey(id)`
- FK `FK_4a79e3bfd7d9929be1a3d93a978`: `FOREIGN KEY (license_id) REFERENCES license(id)`
- FK `FK_732c76e4433ec7a5c1ee1927802`: `FOREIGN KEY ("userId") REFERENCES "user"(id)`
- FK `FK_7eca881b4d7eff0727fd237949c`: `FOREIGN KEY (client_id) REFERENCES client(id)`
- FK `FK_8354425de271870c32fef9b708a`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_8430698496843370df9966742a8`: `FOREIGN KEY (account_id) REFERENCES account(id)`
- FK `FK_a9717e8bf977a125d5b23eebfd6`: `FOREIGN KEY (cost_center_id) REFERENCES cost_center(id) ON DELETE CASCADE`
- Incoming foreign keys:
  - `customer` via `FK_4b9208bd48abfe9a809a60e9797`
  - `invoice_portal` via `FK_c48a278a10cfe87b49b90af84b7`
  - `product` via `FK_238e58f118aff7973203fb2e5e6`, `FK_2947d6b1afa89f4ee818d7237fa`
  - `question` via `FK_9cdb3476db9b11dde0d64a5387b`
  - `question_option` via `FK_31b4e24cc0b2ee1b54450e56bd1`

**Indexes**

- `PK_177ae50bcb9a11b774b38798936`: `CREATE UNIQUE INDEX "PK_177ae50bcb9a11b774b38798936" ON public.image_doc USING btree (id)`
- `IDX_image_doc_name`: `CREATE INDEX "IDX_image_doc_name" ON public.image_doc USING btree (name)`
- `IDX_image_doc_url`: `CREATE INDEX "IDX_image_doc_url" ON public.image_doc USING btree (url)`
- `IDX_image_doc_userId_type`: `CREATE INDEX "IDX_image_doc_userId_type" ON public.image_doc USING btree ("userId", type)`
- `IDX_image_doc_userId_type_createdAt`: `CREATE INDEX "IDX_image_doc_userId_type_createdAt" ON public.image_doc USING btree ("userId", type, "createdAt" DESC)`

### `invoice_portal`

**Kind/grain:** table; Invoice portal/integration configuration.

**Snapshot:** 0 rows; 16 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"cost_center_id" uuid NOT NULL
"portal_name" character varying(256) NOT NULL
"portal_url" character varying(1024) NOT NULL
"portal_auth_method" character varying(128) NOT NULL
"portal_credentials" json NOT NULL
"portal_auth_image_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_3e1baee826fc5853b84141f4d6f`: `PRIMARY KEY (id)`
- FK `FK_12b243212fef046049da4ebb866`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_1841161ff517d93b87ae94c7508`: `FOREIGN KEY (cost_center_id) REFERENCES cost_center(id) ON DELETE CASCADE`
- FK `FK_89d642fb0d780fbffaefe9f52c3`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_bf64573dd03cb32867c945621f9`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_c48a278a10cfe87b49b90af84b7`: `FOREIGN KEY (portal_auth_image_id) REFERENCES image_doc(id) ON DELETE RESTRICT`

**Indexes**

- `PK_3e1baee826fc5853b84141f4d6f`: `CREATE UNIQUE INDEX "PK_3e1baee826fc5853b84141f4d6f" ON public.invoice_portal USING btree (id)`

### `jobs`

**Kind/grain:** table; Asynchronous job queue/status records.

**Snapshot:** 27 rows; 32 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"jobs_started" integer NOT NULL DEFAULT 0 -- Number of jobs started
"jobs_finished" integer NOT NULL DEFAULT 0 -- Number of jobs finished
"jobs_successful" integer NOT NULL DEFAULT 0 -- Number of successful jobs
"jobs_failed" integer NOT NULL DEFAULT 0 -- Number of failed jobs
"status" character varying NOT NULL DEFAULT 'in-progress'::character varying -- Jobs status
"model" character varying NOT NULL -- Model name
"model_id" character varying -- Model id
"started_by_id" character varying -- Started by user id
"is_archived" boolean NOT NULL DEFAULT false -- Whether the job is archived
"startedById" uuid
```

**Constraints and relationships**

- PK `PK_cf0a6c42b72fcc7f7c237def345`: `PRIMARY KEY (id)`
- FK `FK_a2282bfdb21805a8ed2e8c73a48`: `FOREIGN KEY ("startedById") REFERENCES "user"(id)`
- Incoming foreign keys:
  - `jobs_meta` via `FK_e86e48ee1b85bc6b06578c8f354`

**Indexes**

- `PK_cf0a6c42b72fcc7f7c237def345`: `CREATE UNIQUE INDEX "PK_cf0a6c42b72fcc7f7c237def345" ON public.jobs USING btree (id)`

### `jobs_meta`

**Kind/grain:** table; Large job payload/metadata storage.

**Snapshot:** 333 rows; 83 MB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"job_id" character varying NOT NULL -- Job id
"model" character varying NOT NULL -- Model name
"model_id" character varying -- Model id
"meta_data" jsonb -- Meta data
"status" character varying NOT NULL DEFAULT 'completed'::character varying -- JobsMeta status
"is_archived" boolean NOT NULL DEFAULT false -- Whether the job meta is archived
"jobId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_fd8b332bbc4fb98a8017557adf0`: `PRIMARY KEY (id)`
- FK `FK_e86e48ee1b85bc6b06578c8f354`: `FOREIGN KEY ("jobId") REFERENCES jobs(id)`

**Indexes**

- `PK_fd8b332bbc4fb98a8017557adf0`: `CREATE UNIQUE INDEX "PK_fd8b332bbc4fb98a8017557adf0" ON public.jobs_meta USING btree (id)`

### `license`

**Kind/grain:** table; Commercial license definition and lifecycle state.

**Snapshot:** 25 rows; 96 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"type" license_type_enum DEFAULT 'free'::license_type_enum
"organization_count" integer NOT NULL DEFAULT 0
"code" character varying(128) NOT NULL
"start_date" date NOT NULL
"end_date" date NOT NULL
"suspended_at" date
"state" license_state_enum NOT NULL DEFAULT 'active'::license_state_enum
"total_amount" numeric(12,2) NOT NULL DEFAULT '0'::numeric
"preferred_currency" character varying(128)
"payment_type" license_payment_type_enum NOT NULL DEFAULT 'absolute'::license_payment_type_enum
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"is_draft" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_f168ac1ca5ba87286d03b2ef905`: `PRIMARY KEY (id)`
- UNIQUE `UQ_LICENSE_CODE`: `UNIQUE (code)`
- FK `FK_011731184e18caf95bd64a22c8c`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_0b5f567c5ea73dab9b53ca81476`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_169fb04ae48a2a1c76f9ef59f38`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_452a101bd9746d3fb5fe9a50a5b`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_75717689fe0c9c881774af91a53`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `account_licenses` via `FK_04bb208f34ea057ef127e0fa0d1`
  - `image_doc` via `FK_4a79e3bfd7d9929be1a3d93a978`
  - `license_app_data` via `FK_116ca60b28d105dc1c0145f6c61`
  - `license_clients` via `FK_50b8446563d0c1245b03726efa6`
  - `license_payment_milestone` via `FK_649e50472be983c7296bdc7f148`
  - `organization` via `FK_a8c9e6549f981f0b7df9c096e2b`

**Indexes**

- `PK_f168ac1ca5ba87286d03b2ef905`: `CREATE UNIQUE INDEX "PK_f168ac1ca5ba87286d03b2ef905" ON public.license USING btree (id)`
- `UQ_LICENSE_CODE`: `CREATE UNIQUE INDEX "UQ_LICENSE_CODE" ON public.license USING btree (code)`
- `IDX_LICENSE_NAME`: `CREATE INDEX "IDX_LICENSE_NAME" ON public.license USING btree (name)`
- `IDX_LICENSE_STATE`: `CREATE INDEX "IDX_LICENSE_STATE" ON public.license USING btree (state)`
- `IDX_LICENSE_TYPE`: `CREATE INDEX "IDX_LICENSE_TYPE" ON public.license USING btree (type)`

**Non-internal triggers**

- `audit_license_delete_trigger`: `CREATE TRIGGER audit_license_delete_trigger AFTER DELETE ON license FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_insert_trigger`: `CREATE TRIGGER audit_license_insert_trigger AFTER INSERT ON license FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_update_trigger`: `CREATE TRIGGER audit_license_update_trigger AFTER UPDATE ON license FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `license_app_data`

**Kind/grain:** table; Per-license application entitlement/configuration.

**Snapshot:** 46 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"application_id" uuid NOT NULL
"license_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_0b40b09aaf633b5c74459479485`: `PRIMARY KEY (id)`
- FK `FK_116ca60b28d105dc1c0145f6c61`: `FOREIGN KEY (license_id) REFERENCES license(id)`
- FK `FK_2a7a3ba46ea75368905b3a974f6`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_50c828b05a8db6fe9bbf5bb6628`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_7ae2613d95368cc19d29571a021`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_ba6a4c9b9c52d60c18593b28033`: `FOREIGN KEY (application_id) REFERENCES application(id)`
- Incoming foreign keys:
  - `license_app_data_metadata` via `FK_d5f12c0fba72c196de4ba402012`

**Indexes**

- `PK_0b40b09aaf633b5c74459479485`: `CREATE UNIQUE INDEX "PK_0b40b09aaf633b5c74459479485" ON public.license_app_data USING btree (id)`

**Non-internal triggers**

- `audit_license_app_data_delete_trigger`: `CREATE TRIGGER audit_license_app_data_delete_trigger AFTER DELETE ON license_app_data FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_app_data_insert_trigger`: `CREATE TRIGGER audit_license_app_data_insert_trigger AFTER INSERT ON license_app_data FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_app_data_update_trigger`: `CREATE TRIGGER audit_license_app_data_update_trigger AFTER UPDATE ON license_app_data FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `license_app_data_metadata`

**Kind/grain:** table; Metadata entries attached to license application data.

**Snapshot:** 172 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"license_app_data_id" uuid NOT NULL
"role_id" uuid
"user_count" integer
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_008d8ad16503bb7504de6c08b53`: `PRIMARY KEY (id)`
- FK `FK_1a1f21ba8e49cde85f5373260ba`: `FOREIGN KEY (role_id) REFERENCES role(id) ON DELETE CASCADE`
- FK `FK_430cc63ef1500c66cb21968006d`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_49a1fee82e0e556c2691741cf21`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_c49bb7b57a4a0a7ec302428c6d5`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_d5f12c0fba72c196de4ba402012`: `FOREIGN KEY (license_app_data_id) REFERENCES license_app_data(id) ON DELETE CASCADE`

**Indexes**

- `PK_008d8ad16503bb7504de6c08b53`: `CREATE UNIQUE INDEX "PK_008d8ad16503bb7504de6c08b53" ON public.license_app_data_metadata USING btree (id)`

**Non-internal triggers**

- `audit_license_app_data_metadata_delete_trigger`: `CREATE TRIGGER audit_license_app_data_metadata_delete_trigger AFTER DELETE ON license_app_data_metadata FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_app_data_metadata_insert_trigger`: `CREATE TRIGGER audit_license_app_data_metadata_insert_trigger AFTER INSERT ON license_app_data_metadata FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_app_data_metadata_update_trigger`: `CREATE TRIGGER audit_license_app_data_metadata_update_trigger AFTER UPDATE ON license_app_data_metadata FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `license_clients`

**Kind/grain:** table; Assignment join between licenses and clients.

**Snapshot:** 22 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"license_id" uuid
"client_id" uuid
"amount" numeric(12,2) NOT NULL DEFAULT '0'::numeric
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
"counterparty_id" uuid
"costcenter_id" uuid
"bank_account_id" uuid
```

**Constraints and relationships**

- PK `PK_e25418515f6ad37408b14544f56`: `PRIMARY KEY (id)`
- FK `FK_50b8446563d0c1245b03726efa6`: `FOREIGN KEY (license_id) REFERENCES license(id) ON DELETE CASCADE`
- FK `FK_7fa5a8c8cd296feacf56b8293f3`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_82dc6220a0b5704bf34f088c77e`: `FOREIGN KEY (counterparty_id) REFERENCES counterparty(id) ON DELETE CASCADE`
- FK `FK_84f7cbbfe7bbc2832030f18c44a`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_8ef331db35b306dc6e543b670c8`: `FOREIGN KEY (bank_account_id) REFERENCES bank_account(id) ON DELETE CASCADE`
- FK `FK_aa6f9b553b672dfa268c3865506`: `FOREIGN KEY (client_id) REFERENCES client(id) ON DELETE CASCADE`
- FK `FK_da0e6f529b87dab061f6388dad8`: `FOREIGN KEY (costcenter_id) REFERENCES cost_center(id) ON DELETE CASCADE`
- FK `FK_dc57695e911d4fa7b96b7d68f2f`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_e25418515f6ad37408b14544f56`: `CREATE UNIQUE INDEX "PK_e25418515f6ad37408b14544f56" ON public.license_clients USING btree (id)`
- `IDX_LICENSE_CLIENT_CLIENT_ID`: `CREATE INDEX "IDX_LICENSE_CLIENT_CLIENT_ID" ON public.license_clients USING btree (client_id)`
- `IDX_LICENSE_CLIENT_LICENSE_ID`: `CREATE INDEX "IDX_LICENSE_CLIENT_LICENSE_ID" ON public.license_clients USING btree (license_id)`

**Non-internal triggers**

- `audit_license_clients_delete_trigger`: `CREATE TRIGGER audit_license_clients_delete_trigger AFTER DELETE ON license_clients FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_clients_insert_trigger`: `CREATE TRIGGER audit_license_clients_insert_trigger AFTER INSERT ON license_clients FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_clients_update_trigger`: `CREATE TRIGGER audit_license_clients_update_trigger AFTER UPDATE ON license_clients FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `license_payment_milestone`

**Kind/grain:** table; Payment milestone configuration for a license.

**Snapshot:** 5 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256)
"milestone_amount" numeric(12,2) NOT NULL DEFAULT '0'::numeric
"invoice_date" date
"deleted_at" timestamp without time zone
"license_id" uuid NOT NULL
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_806c2b1dc843013227d9280275b`: `PRIMARY KEY (id)`
- FK `FK_11f9907596c6161706932e479d3`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_649e50472be983c7296bdc7f148`: `FOREIGN KEY (license_id) REFERENCES license(id)`
- FK `FK_7b850551b4ac033779806db2bdc`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_ba87d5299e96b551bf4d07097a4`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`

**Indexes**

- `PK_806c2b1dc843013227d9280275b`: `CREATE UNIQUE INDEX "PK_806c2b1dc843013227d9280275b" ON public.license_payment_milestone USING btree (id)`

**Non-internal triggers**

- `audit_license_payment_milestone_delete_trigger`: `CREATE TRIGGER audit_license_payment_milestone_delete_trigger AFTER DELETE ON license_payment_milestone FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_payment_milestone_insert_trigger`: `CREATE TRIGGER audit_license_payment_milestone_insert_trigger AFTER INSERT ON license_payment_milestone FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_license_payment_milestone_update_trigger`: `CREATE TRIGGER audit_license_payment_milestone_update_trigger AFTER UPDATE ON license_payment_milestone FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `logic`

**Kind/grain:** table; One survey routing-logic container, normally anchored to a survey design element.

**Snapshot:** 405 rows; 152 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"logicName" character varying(255) NOT NULL
"isActive" boolean NOT NULL DEFAULT true
"screenId" uuid
"surveyId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_403f68a66121c590caae92d62ec`: `PRIMARY KEY (id)`
- FK `FK_0a77835a7754fc242a0711e4492`: `FOREIGN KEY ("surveyId") REFERENCES survey(id) ON DELETE CASCADE`
- FK `FK_0d5ce05d0e29ea6ed61cce50dcb`: `FOREIGN KEY ("screenId") REFERENCES question_screen(id) ON DELETE SET NULL`
- Incoming foreign keys:
  - `logic_rule` via `FK_cc99569be279e7072e7535b92a6`

**Indexes**

- `PK_403f68a66121c590caae92d62ec`: `CREATE UNIQUE INDEX "PK_403f68a66121c590caae92d62ec" ON public.logic USING btree (id)`
- `idx_logic_screen`: `CREATE INDEX idx_logic_screen ON public.logic USING btree ("screenId")`
- `idx_logic_survey`: `CREATE INDEX idx_logic_survey ON public.logic USING btree ("surveyId")`

**Non-internal triggers**

- `audit_logic_delete_trigger`: `CREATE TRIGGER audit_logic_delete_trigger AFTER DELETE ON logic FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_logic_insert_trigger`: `CREATE TRIGGER audit_logic_insert_trigger AFTER INSERT ON logic FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_logic_update_trigger`: `CREATE TRIGGER audit_logic_update_trigger AFTER UPDATE ON logic FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `logic_rule`

**Kind/grain:** table; One ordered IF/ELSE routing rule and its action target.

**Snapshot:** 517 rows; 184 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"ruleName" character varying(255) NOT NULL
"sourceQuestionId" uuid
"actionType" logic_rule_actiontype_enum NOT NULL DEFAULT 'jump_to_screen'::logic_rule_actiontype_enum
"targetType" logic_rule_targettype_enum NOT NULL DEFAULT 'screen'::logic_rule_targettype_enum
"targetScreenId" uuid
"isActive" boolean NOT NULL DEFAULT true
"order" integer NOT NULL DEFAULT 1
"logicId" uuid NOT NULL
"messageText" character varying
"ruleType" logic_rule_ruletype_enum NOT NULL DEFAULT 'if'::logic_rule_ruletype_enum
```

**Constraints and relationships**

- PK `PK_493bc9a145ed55b0f6c4d3b19c0`: `PRIMARY KEY (id)`
- FK `FK_5fdd2164ffedada9a20bc0bea0b`: `FOREIGN KEY ("sourceQuestionId") REFERENCES question(id) ON DELETE CASCADE`
- FK `FK_cc99569be279e7072e7535b92a6`: `FOREIGN KEY ("logicId") REFERENCES logic(id) ON DELETE CASCADE`
- FK `FK_de8005fcadf5269b340010b4dc2`: `FOREIGN KEY ("targetScreenId") REFERENCES question_screen(id) ON DELETE SET NULL`
- Incoming foreign keys:
  - `logic_rule_condition` via `FK_550da85b9c978ce59d46359919d`

**Indexes**

- `PK_493bc9a145ed55b0f6c4d3b19c0`: `CREATE UNIQUE INDEX "PK_493bc9a145ed55b0f6c4d3b19c0" ON public.logic_rule USING btree (id)`
- `idx_logic_rule_logic`: `CREATE INDEX idx_logic_rule_logic ON public.logic_rule USING btree ("logicId")`

**Non-internal triggers**

- `audit_logic_rule_delete_trigger`: `CREATE TRIGGER audit_logic_rule_delete_trigger AFTER DELETE ON logic_rule FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_logic_rule_insert_trigger`: `CREATE TRIGGER audit_logic_rule_insert_trigger AFTER INSERT ON logic_rule FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_logic_rule_update_trigger`: `CREATE TRIGGER audit_logic_rule_update_trigger AFTER UPDATE ON logic_rule FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `logic_rule_condition`

**Kind/grain:** table; One condition within a logic rule, linked to a question/option and AND/OR chaining.

**Snapshot:** 519 rows; 168 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"condition" logic_rule_condition_condition_enum NOT NULL
"isActive" boolean NOT NULL DEFAULT true
"order" integer NOT NULL DEFAULT 1
"nextOperator" logic_rule_condition_nextoperator_enum
"ruleId" uuid NOT NULL
"attributeId" uuid
"value" jsonb
"questionGroupId" uuid
```

**Constraints and relationships**

- PK `PK_4f7643cebd988d9b33ef4aec986`: `PRIMARY KEY (id)`
- FK `FK_550da85b9c978ce59d46359919d`: `FOREIGN KEY ("ruleId") REFERENCES logic_rule(id) ON DELETE CASCADE`
- FK `FK_8c20b975b8a4e705bebfd925444`: `FOREIGN KEY ("questionGroupId") REFERENCES question_group(id) ON DELETE CASCADE`
- FK `FK_ad31557cef294342312ee1be540`: `FOREIGN KEY ("attributeId") REFERENCES question_option(id) ON DELETE CASCADE`

**Indexes**

- `PK_4f7643cebd988d9b33ef4aec986`: `CREATE UNIQUE INDEX "PK_4f7643cebd988d9b33ef4aec986" ON public.logic_rule_condition USING btree (id)`
- `idx_logic_rule_condition_rule`: `CREATE INDEX idx_logic_rule_condition_rule ON public.logic_rule_condition USING btree ("ruleId")`

**Non-internal triggers**

- `audit_logic_rule_condition_delete_trigger`: `CREATE TRIGGER audit_logic_rule_condition_delete_trigger AFTER DELETE ON logic_rule_condition FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_logic_rule_condition_insert_trigger`: `CREATE TRIGGER audit_logic_rule_condition_insert_trigger AFTER INSERT ON logic_rule_condition FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_logic_rule_condition_update_trigger`: `CREATE TRIGGER audit_logic_rule_condition_update_trigger AFTER UPDATE ON logic_rule_condition FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `migrations`

**Kind/grain:** table; Application migration history.

**Snapshot:** 170 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" integer NOT NULL DEFAULT nextval('migrations_id_seq'::regclass)
"timestamp" bigint NOT NULL
"name" text NOT NULL
```

**Constraints and relationships**

- PK `migrations_pkey`: `PRIMARY KEY (id)`

**Indexes**

- `migrations_pkey`: `CREATE UNIQUE INDEX migrations_pkey ON public.migrations USING btree (id)`

### `module_contact`

**Kind/grain:** table; Typed billing/financial contact assignment to a tenancy module.

**Snapshot:** 11 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" uuid NOT NULL
"is_primary" boolean NOT NULL DEFAULT false
"module_type" module_contact_module_type_enum NOT NULL
"contact_type" module_contact_contact_type_enum NOT NULL DEFAULT 'financial'::module_contact_contact_type_enum
"module_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_40b1ed800214acd12b099bd164e`: `PRIMARY KEY (id)`
- FK `FK_75a9e3ea9b22260ebf974159c30`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_93496c248d1d51687c5ac2271f6`: `FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE`
- FK `FK_b2067091cccf721f5b056dafb1c`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_b45b4f81ff525806d7f0dae6da3`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_40b1ed800214acd12b099bd164e`: `CREATE UNIQUE INDEX "PK_40b1ed800214acd12b099bd164e" ON public.module_contact USING btree (id)`

### `nomenclature_category`

**Kind/grain:** table; Controlled nomenclature category lookup.

**Snapshot:** 7 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"organization_id" uuid NOT NULL
"code" character varying(100) NOT NULL
"label" character varying(100) NOT NULL
"created_by" uuid
"updated_by" uuid
"createdAt" timestamp(6) without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp(6) without time zone NOT NULL DEFAULT now()
```

**Constraints and relationships**

- PK `PK_nomenclature_category`: `PRIMARY KEY (id)`
- UNIQUE `UQ_NOMENCLATURE_CATEGORY_ORG_CODE`: `UNIQUE (organization_id, code)`
- FK `FK_nomenclature_category_created_by`: `FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL`
- FK `FK_nomenclature_category_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- FK `FK_nomenclature_category_updated_by`: `FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL`
- Incoming foreign keys:
  - `survey_nomenclature` via `FK_survey_nomenclature_category`

**Indexes**

- `PK_nomenclature_category`: `CREATE UNIQUE INDEX "PK_nomenclature_category" ON public.nomenclature_category USING btree (id)`
- `UQ_NOMENCLATURE_CATEGORY_ORG_CODE`: `CREATE UNIQUE INDEX "UQ_NOMENCLATURE_CATEGORY_ORG_CODE" ON public.nomenclature_category USING btree (organization_id, code)`
- `IDX_NOMENCLATURE_CATEGORY_ORG`: `CREATE INDEX "IDX_NOMENCLATURE_CATEGORY_ORG" ON public.nomenclature_category USING btree (organization_id)`

### `nomenclature_client`

**Kind/grain:** table; Controlled nomenclature client lookup.

**Snapshot:** 6 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"organization_id" uuid NOT NULL
"name" character varying(100) NOT NULL
"created_by" uuid
"updated_by" uuid
"createdAt" timestamp(6) without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp(6) without time zone NOT NULL DEFAULT now()
```

**Constraints and relationships**

- PK `PK_nomenclature_client`: `PRIMARY KEY (id)`
- UNIQUE `UQ_NOMENCLATURE_CLIENT_ORG_NAME`: `UNIQUE (organization_id, name)`
- FK `FK_nomenclature_client_created_by`: `FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL`
- FK `FK_nomenclature_client_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- FK `FK_nomenclature_client_updated_by`: `FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL`
- Incoming foreign keys:
  - `survey_nomenclature` via `FK_survey_nomenclature_client`

**Indexes**

- `PK_nomenclature_client`: `CREATE UNIQUE INDEX "PK_nomenclature_client" ON public.nomenclature_client USING btree (id)`
- `UQ_NOMENCLATURE_CLIENT_ORG_NAME`: `CREATE UNIQUE INDEX "UQ_NOMENCLATURE_CLIENT_ORG_NAME" ON public.nomenclature_client USING btree (organization_id, name)`
- `IDX_NOMENCLATURE_CLIENT_ORG`: `CREATE INDEX "IDX_NOMENCLATURE_CLIENT_ORG" ON public.nomenclature_client USING btree (organization_id)`

### `nomenclature_formula_fw_sequence`

**Kind/grain:** table; Sequence/configuration used to generate FlavorWiki formula nomenclature.

**Snapshot:** 4 rows; 40 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"organization_id" uuid NOT NULL
"last_value" integer NOT NULL DEFAULT 0
"createdAt" timestamp(6) without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp(6) without time zone NOT NULL DEFAULT now()
```

**Constraints and relationships**

- PK `PK_nomenclature_formula_fw_sequence`: `PRIMARY KEY (id)`
- UNIQUE `UQ_NOMENCLATURE_FW_SEQ_ORG`: `UNIQUE (organization_id)`
- FK `FK_nomenclature_formula_fw_sequence_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`

**Indexes**

- `PK_nomenclature_formula_fw_sequence`: `CREATE UNIQUE INDEX "PK_nomenclature_formula_fw_sequence" ON public.nomenclature_formula_fw_sequence USING btree (id)`
- `UQ_NOMENCLATURE_FW_SEQ_ORG`: `CREATE UNIQUE INDEX "UQ_NOMENCLATURE_FW_SEQ_ORG" ON public.nomenclature_formula_fw_sequence USING btree (organization_id)`

### `nomenclature_type_of_test`

**Kind/grain:** table; Controlled nomenclature test-type lookup.

**Snapshot:** 6 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"organization_id" uuid NOT NULL
"code" character varying(100) NOT NULL
"label" character varying(100) NOT NULL
"created_by" uuid
"updated_by" uuid
"createdAt" timestamp(6) without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp(6) without time zone NOT NULL DEFAULT now()
```

**Constraints and relationships**

- PK `PK_nomenclature_type_of_test`: `PRIMARY KEY (id)`
- UNIQUE `UQ_NOMENCLATURE_TYPE_OF_TEST_ORG_CODE`: `UNIQUE (organization_id, code)`
- FK `FK_nomenclature_type_of_test_created_by`: `FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL`
- FK `FK_nomenclature_type_of_test_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- FK `FK_nomenclature_type_of_test_updated_by`: `FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL`
- Incoming foreign keys:
  - `survey_nomenclature` via `FK_survey_nomenclature_type_of_test`

**Indexes**

- `PK_nomenclature_type_of_test`: `CREATE UNIQUE INDEX "PK_nomenclature_type_of_test" ON public.nomenclature_type_of_test USING btree (id)`
- `UQ_NOMENCLATURE_TYPE_OF_TEST_ORG_CODE`: `CREATE UNIQUE INDEX "UQ_NOMENCLATURE_TYPE_OF_TEST_ORG_CODE" ON public.nomenclature_type_of_test USING btree (organization_id, code)`
- `IDX_NOMENCLATURE_TYPE_OF_TEST_ORG`: `CREATE INDEX "IDX_NOMENCLATURE_TYPE_OF_TEST_ORG" ON public.nomenclature_type_of_test USING btree (organization_id)`

### `onboarding`

**Kind/grain:** table; Per-user onboarding progress/state.

**Snapshot:** 10 rows; 32 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"code" character varying NOT NULL
```

**Constraints and relationships**

- PK `PK_b8b6cfe63674aaee17874f033cf`: `PRIMARY KEY (id)`

**Indexes**

- `PK_b8b6cfe63674aaee17874f033cf`: `CREATE UNIQUE INDEX "PK_b8b6cfe63674aaee17874f033cf" ON public.onboarding USING btree (id)`

**Non-internal triggers**

- `audit_onboarding_trigger`: `CREATE TRIGGER audit_onboarding_trigger AFTER INSERT OR DELETE OR UPDATE ON onboarding FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`

### `organization`

**Kind/grain:** table; Operational tenant/organization that directly owns surveys.

**Snapshot:** 27 rows; 88 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"type" organization_type_enum NOT NULL DEFAULT 'legal'::organization_type_enum
"account_id" uuid NOT NULL
"license_id" uuid NOT NULL
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"name" character varying(128) NOT NULL
"code" character varying(128) NOT NULL
```

**Constraints and relationships**

- PK `PK_472c1f99a32def1b0abb219cd67`: `PRIMARY KEY (id)`
- UNIQUE `UQ_aa6e74e96ed2dddfcf09782110a`: `UNIQUE (code)`
- FK `FK_3540851b5121e812cd15da62560`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_5865830635bbaf5c95135a5fdce`: `FOREIGN KEY (account_id) REFERENCES account(id)`
- FK `FK_727fc946c971786a7c9fa6629aa`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- FK `FK_a8c9e6549f981f0b7df9c096e2b`: `FOREIGN KEY (license_id) REFERENCES license(id)`
- FK `FK_b0c596f429ff2966d03516ef1ca`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_d348286667373cbcec5463cda5b`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_edd3d199e09845da9c76636170a`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `branding` via `FK_branding_organization`
  - `branding_domains` via `FK_branding_domains_organization`
  - `charts_reports` via `FK_ea2b471ab4507643af0735b8d0d`
  - `charts_tabs` via `FK_975b4a90d3e8ff936aa5bda4667`
  - `feature_flag_scopes` via `FK_feature_flag_scopes_organization`
  - `folder` via `FK_e09b8e7d4818dd263dde45bbecb`, `FK_fd89c6b76b9f8bdd18c3b8aa30a`
  - `image_doc` via `FK_8354425de271870c32fef9b708a`
  - `nomenclature_category` via `FK_nomenclature_category_organization`
  - `nomenclature_client` via `FK_nomenclature_client_organization`
  - `nomenclature_formula_fw_sequence` via `FK_nomenclature_formula_fw_sequence_organization`
  - `nomenclature_type_of_test` via `FK_nomenclature_type_of_test_organization`
  - `organization_applications` via `FK_917eb727b0a810b858dee402e64`
  - `organization_roles_role` via `FK_43f75ae0b396e553e3a9ef452da`
  - `panel` via `FK_2f2fbd4915bfbfe80b0565c2f85`
  - `panelist` via `FK_panelist_organization_id`
  - `question_library` via `FK_5f5771a85b0a2ce3530d48118c8`
  - `role_organizations_organization` via `FK_d055935ccb809b70d89e757dfa0`
  - `shared_access_organization` via `FK_4ce8205d9573435f5ddeb43df39`
  - `survey` via `FK_51124fdaa8350196ac22d109697`, `FK_a2e6e9ab8f1ff04cbf31da646e7`
  - `survey_nomenclature` via `FK_survey_nomenclature_organization`
  - `user_organization_role_permission` via `FK_c5aaa1dfe1a78191181623be2c5`
  - `user_organizations_organization` via `FK_8d7c566d5a234be0a6461013269`

**Indexes**

- `PK_472c1f99a32def1b0abb219cd67`: `CREATE UNIQUE INDEX "PK_472c1f99a32def1b0abb219cd67" ON public.organization USING btree (id)`
- `UQ_aa6e74e96ed2dddfcf09782110a`: `CREATE UNIQUE INDEX "UQ_aa6e74e96ed2dddfcf09782110a" ON public.organization USING btree (code)`
- `IDX_ORGANIZATION_ACCOUNT_ID`: `CREATE INDEX "IDX_ORGANIZATION_ACCOUNT_ID" ON public.organization USING btree (account_id)`
- `IDX_ORGANIZATION_NAME`: `CREATE INDEX "IDX_ORGANIZATION_NAME" ON public.organization USING btree (name)`
- `idx_org_id`: `CREATE INDEX idx_org_id ON public.organization USING btree (id)`

**Non-internal triggers**

- `audit_organization_delete_trigger`: `CREATE TRIGGER audit_organization_delete_trigger AFTER DELETE ON organization FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_organization_insert_trigger`: `CREATE TRIGGER audit_organization_insert_trigger AFTER INSERT ON organization FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_organization_update_trigger`: `CREATE TRIGGER audit_organization_update_trigger AFTER UPDATE ON organization FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `organization_applications`

**Kind/grain:** table; Many-to-many assignment of applications to organizations.

**Snapshot:** 48 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"organization_id" uuid NOT NULL
"application_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_f182c645abeb02f660956c1e47e`: `PRIMARY KEY (organization_id, application_id)`
- FK `FK_59884a47823f64d334801caa6fb`: `FOREIGN KEY (application_id) REFERENCES application(id)`
- FK `FK_917eb727b0a810b858dee402e64`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_f182c645abeb02f660956c1e47e`: `CREATE UNIQUE INDEX "PK_f182c645abeb02f660956c1e47e" ON public.organization_applications USING btree (organization_id, application_id)`
- `IDX_59884a47823f64d334801caa6f`: `CREATE INDEX "IDX_59884a47823f64d334801caa6f" ON public.organization_applications USING btree (application_id)`
- `IDX_917eb727b0a810b858dee402e6`: `CREATE INDEX "IDX_917eb727b0a810b858dee402e6" ON public.organization_applications USING btree (organization_id)`

### `organization_roles_role`

**Kind/grain:** table; Organization-to-role join table.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"organizationId" uuid NOT NULL
"roleId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_be4dad7f2dd3a324e76b59ef4d1`: `PRIMARY KEY ("organizationId", "roleId")`
- FK `FK_43f75ae0b396e553e3a9ef452da`: `FOREIGN KEY ("organizationId") REFERENCES organization(id) ON UPDATE CASCADE ON DELETE CASCADE`
- FK `FK_b99e8125f26dec2b96d6c0cba31`: `FOREIGN KEY ("roleId") REFERENCES role(id)`

**Indexes**

- `PK_be4dad7f2dd3a324e76b59ef4d1`: `CREATE UNIQUE INDEX "PK_be4dad7f2dd3a324e76b59ef4d1" ON public.organization_roles_role USING btree ("organizationId", "roleId")`
- `IDX_43f75ae0b396e553e3a9ef452d`: `CREATE INDEX "IDX_43f75ae0b396e553e3a9ef452d" ON public.organization_roles_role USING btree ("organizationId")`
- `IDX_b99e8125f26dec2b96d6c0cba3`: `CREATE INDEX "IDX_b99e8125f26dec2b96d6c0cba3" ON public.organization_roles_role USING btree ("roleId")`

### `panel`

**Kind/grain:** table; One respondent panel definition and access-mode configuration.

**Snapshot:** 54 rows; 80 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"description" character varying(1024)
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"organization_id" uuid
"created_by" uuid NOT NULL
"updated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"panel_id" character varying(256)
"tag" jsonb DEFAULT '[]'::jsonb
"access_mode" panel_access_mode_enum NOT NULL DEFAULT 'anonymous_code'::panel_access_mode_enum
"settings" jsonb DEFAULT '{}'::jsonb
"status" panel_status_enum NOT NULL DEFAULT 'active'::panel_status_enum
"inactivated_by" uuid
"inactivated_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_bbd5674b69f7448974aa41ab347`: `PRIMARY KEY (id)`
- FK `FK_1131f721af1a23d44014ef5b535`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_2f2fbd4915bfbfe80b0565c2f85`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_da77649fad2e5fe3444b631485e`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_e73f6691aef99955b85ac6ec77e`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_ee4651ea7f7ff951a17fddd4483`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_panel_inactivated_by`: `FOREIGN KEY (inactivated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `panel_code` via `FK_cf93e4313b9e7cec39a2e59e206`
  - `panel_panelist` via `FK_panel_panelist_panel_id`
  - `survey_panel` via `FK_survey_panel_panel_id`
  - `survey_panel_code` via `FK_fe16355d0335bdaa7dc4a5d2083`
  - `survey_panel_stats` via `FK_sps_panel`

**Indexes**

- `PK_bbd5674b69f7448974aa41ab347`: `CREATE UNIQUE INDEX "PK_bbd5674b69f7448974aa41ab347" ON public.panel USING btree (id)`
- `IDX_PANEL_NAME`: `CREATE INDEX "IDX_PANEL_NAME" ON public.panel USING btree (name)`

### `panel_code`

**Kind/grain:** table; One panel access code with lifecycle status and usage limits.

**Snapshot:** 783 rows; 312 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"code" character varying(32) NOT NULL
"deleted_at" timestamp without time zone
"panel_id" uuid NOT NULL
"created_by" uuid NOT NULL
"archived_by" uuid
"deleted_by" uuid
"status" panel_code_status_enum NOT NULL DEFAULT 'available'::panel_code_status_enum
"last_used_at" timestamp without time zone
"valid_until" timestamp without time zone
"archived_at" timestamp without time zone
"settings" jsonb DEFAULT '{}'::jsonb
"inactivated_by" uuid
"inactivated_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_f53fceaa5eedfe8c1ca539ad95a`: `PRIMARY KEY (id)`
- UNIQUE `UQ_PANEL_CODE_CODE`: `UNIQUE (code)`
- FK `FK_9a676d3578eb44d875e69380e44`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_cf93e4313b9e7cec39a2e59e206`: `FOREIGN KEY (panel_id) REFERENCES panel(id)`
- FK `FK_d5a4e1f59ca127a4cfb02aa5033`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_e9d6fb5ec27b1e4d5fc9ea9d785`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_panel_code_inactivated_by`: `FOREIGN KEY (inactivated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `enrollment` via `FK_3629439ed429cc65146f2422391`
  - `survey_panel_code` via `FK_1089eaede6f136cdc851824c67d`
  - `survey_panel_stats` via `FK_sps_panel_code`

**Indexes**

- `PK_f53fceaa5eedfe8c1ca539ad95a`: `CREATE UNIQUE INDEX "PK_f53fceaa5eedfe8c1ca539ad95a" ON public.panel_code USING btree (id)`
- `UQ_PANEL_CODE_CODE`: `CREATE UNIQUE INDEX "UQ_PANEL_CODE_CODE" ON public.panel_code USING btree (code)`
- `IDX_PANEL_CODE_CODE`: `CREATE INDEX "IDX_PANEL_CODE_CODE" ON public.panel_code USING btree (code)`
- `idx_panel_code_panel`: `CREATE INDEX idx_panel_code_panel ON public.panel_code USING btree (panel_id)`

### `panel_code_format`

**Kind/grain:** table; Code-generation format/settings for panel codes.

**Snapshot:** 1 rows; 48 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"organization_id" uuid NOT NULL
"panel_id_format" jsonb NOT NULL DEFAULT '{"prefix": "", "padding": 4, "code_type": "numeric", "separator": "hyphen"}'::jsonb
"panelist_code_format" jsonb NOT NULL DEFAULT '{"prefix": "", "padding": 4, "code_type": "numeric", "separator": "hyphen"}'::jsonb
"created_by" uuid
```

**Constraints and relationships**

- PK `PK_d7d84f9c4afb448d227f8b09b07`: `PRIMARY KEY (id)`
- UNIQUE `UQ_PANEL_CODE_FORMAT_ORG`: `UNIQUE (organization_id)`
- FK `FK_PANEL_CODE_FORMAT_CREATED_BY_USER`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_d7d84f9c4afb448d227f8b09b07`: `CREATE UNIQUE INDEX "PK_d7d84f9c4afb448d227f8b09b07" ON public.panel_code_format USING btree (id)`
- `UQ_PANEL_CODE_FORMAT_ORG`: `CREATE UNIQUE INDEX "UQ_PANEL_CODE_FORMAT_ORG" ON public.panel_code_format USING btree (organization_id)`

### `panel_panelist`

**Kind/grain:** table; Membership join between a panel and panelist.

**Snapshot:** 129 rows; 88 kB total relation size; RLS not enabled.

**Columns**

```text
"panelist_id" uuid NOT NULL
"panel_id" uuid NOT NULL
"assigned_at" timestamp without time zone NOT NULL DEFAULT now()
```

**Constraints and relationships**

- PK `PK_panel_panelist`: `PRIMARY KEY (panelist_id, panel_id)`
- FK `FK_panel_panelist_panel_id`: `FOREIGN KEY (panel_id) REFERENCES panel(id) ON DELETE CASCADE`
- FK `FK_panel_panelist_panelist_id`: `FOREIGN KEY (panelist_id) REFERENCES panelist(id) ON DELETE CASCADE`

**Indexes**

- `PK_panel_panelist`: `CREATE UNIQUE INDEX "PK_panel_panelist" ON public.panel_panelist USING btree (panelist_id, panel_id)`
- `IDX_panel_panelist_panel_id`: `CREATE INDEX "IDX_panel_panelist_panel_id" ON public.panel_panelist USING btree (panel_id)`
- `IDX_panel_panelist_panelist_id`: `CREATE INDEX "IDX_panel_panelist_panelist_id" ON public.panel_panelist USING btree (panelist_id)`

### `panelist`

**Kind/grain:** table; One panel contact/person record; this table has contact/status data, not normalized demographics.

**Snapshot:** 457 rows; 272 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"organization_id" uuid
"full_name" character varying(256) NOT NULL
"panelist_code" character varying(256) NOT NULL
"external_id" character varying(256)
"status" panelist_status_enum NOT NULL DEFAULT 'active'::panelist_status_enum
"email" character varying(256)
"phone_number" character varying(32)
"notes" character varying(1024)
"created_by" uuid
"updated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"inactivated_by" uuid
"inactivated_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_panelist`: `PRIMARY KEY (id)`
- FK `FK_panelist_archived_by`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_panelist_created_by`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_panelist_deleted_by`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_panelist_inactivated_by`: `FOREIGN KEY (inactivated_by) REFERENCES "user"(id)`
- FK `FK_panelist_organization_id`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_panelist_updated_by`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `enrollment` via `FK_cd79fafd31d1d02722a4047ba67`
  - `panel_panelist` via `FK_panel_panelist_panelist_id`
  - `survey_panel_stats` via `FK_sps_panelist`

**Indexes**

- `PK_panelist`: `CREATE UNIQUE INDEX "PK_panelist" ON public.panelist USING btree (id)`
- `IDX_PANELIST_CODE_ORG`: `CREATE UNIQUE INDEX "IDX_PANELIST_CODE_ORG" ON public.panelist USING btree (organization_id, panelist_code)`
- `IDX_panelist_archived_by`: `CREATE INDEX "IDX_panelist_archived_by" ON public.panelist USING btree (archived_by)`
- `IDX_panelist_created_by`: `CREATE INDEX "IDX_panelist_created_by" ON public.panelist USING btree (created_by)`
- `IDX_panelist_deleted_by`: `CREATE INDEX "IDX_panelist_deleted_by" ON public.panelist USING btree (deleted_by)`
- `IDX_panelist_organization_id`: `CREATE INDEX "IDX_panelist_organization_id" ON public.panelist USING btree (organization_id)`
- `IDX_panelist_updated_by`: `CREATE INDEX "IDX_panelist_updated_by" ON public.panelist USING btree (updated_by)`

### `permission`

**Kind/grain:** table; Authorization permission node.

**Snapshot:** 57 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"unique_name" character varying(256)
"associated_app_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
"name" character varying(128)
"code" character varying(128)
"category" character varying(128)
```

**Constraints and relationships**

- PK `PK_3b8b97af9d9d8807e41e6f48362`: `PRIMARY KEY (id)`
- FK `FK_8a5117703b89f1cb83a44d895f3`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_974c86657cc409ff59ddb20b48b`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_9c4a4f3953767dfc5d1a3b63d10`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_b14d41d1e6ee3340a7109c34a97`: `FOREIGN KEY (associated_app_id) REFERENCES application(id)`
- Incoming foreign keys:
  - `permission_closure` via `FK_13672a2e7e3e11c7edb147298a4`, `FK_5a9be34c7833062e6cceb562d54`
  - `permission_operation` via `FK_a18ded36ed9dbbccc75285a33a5`
  - `role_permissions_permission` via `FK_2d3e8e7c82bdee8553b6f1e3325`
  - `user_organization_role_permission` via `FK_29e5a49d08c3e10571ce87a5943`

**Indexes**

- `PK_3b8b97af9d9d8807e41e6f48362`: `CREATE UNIQUE INDEX "PK_3b8b97af9d9d8807e41e6f48362" ON public.permission USING btree (id)`

**Non-internal triggers**

- `audit_permission_delete_trigger`: `CREATE TRIGGER audit_permission_delete_trigger AFTER DELETE ON permission FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_permission_insert_trigger`: `CREATE TRIGGER audit_permission_insert_trigger AFTER INSERT ON permission FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_permission_update_trigger`: `CREATE TRIGGER audit_permission_update_trigger AFTER UPDATE ON permission FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `permission_closure`

**Kind/grain:** table; Transitive closure rows for hierarchical permissions.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id_ancestor" uuid NOT NULL
"id_descendant" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_17ddd758cee77e49c62fb6c80d3`: `PRIMARY KEY (id_ancestor, id_descendant)`
- FK `FK_13672a2e7e3e11c7edb147298a4`: `FOREIGN KEY (id_descendant) REFERENCES permission(id) ON DELETE CASCADE`
- FK `FK_5a9be34c7833062e6cceb562d54`: `FOREIGN KEY (id_ancestor) REFERENCES permission(id) ON DELETE CASCADE`

**Indexes**

- `PK_17ddd758cee77e49c62fb6c80d3`: `CREATE UNIQUE INDEX "PK_17ddd758cee77e49c62fb6c80d3" ON public.permission_closure USING btree (id_ancestor, id_descendant)`
- `IDX_13672a2e7e3e11c7edb147298a`: `CREATE INDEX "IDX_13672a2e7e3e11c7edb147298a" ON public.permission_closure USING btree (id_descendant)`
- `IDX_5a9be34c7833062e6cceb562d5`: `CREATE INDEX "IDX_5a9be34c7833062e6cceb562d5" ON public.permission_closure USING btree (id_ancestor)`

### `permission_operation`

**Kind/grain:** table; Allowed operation attached to a permission.

**Snapshot:** 249 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"permission_id" uuid NOT NULL
"operation" character varying(128) NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_4b40748dc270eda0bdba9e9c5de`: `PRIMARY KEY (id)`
- FK `FK_0169d1680c543d2c0ebe3789016`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_3dfcca3bb02a7ef507580470d97`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_a18ded36ed9dbbccc75285a33a5`: `FOREIGN KEY (permission_id) REFERENCES permission(id)`
- FK `FK_f970e5f6954ea19613cce9cd9c1`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`

**Indexes**

- `PK_4b40748dc270eda0bdba9e9c5de`: `CREATE UNIQUE INDEX "PK_4b40748dc270eda0bdba9e9c5de" ON public.permission_operation USING btree (id)`

**Non-internal triggers**

- `audit_permission_operation_delete_trigger`: `CREATE TRIGGER audit_permission_operation_delete_trigger AFTER DELETE ON permission_operation FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_permission_operation_insert_trigger`: `CREATE TRIGGER audit_permission_operation_insert_trigger AFTER INSERT ON permission_operation FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_permission_operation_update_trigger`: `CREATE TRIGGER audit_permission_operation_update_trigger AFTER UPDATE ON permission_operation FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `personnels_csv_upload_history`

**Kind/grain:** table; Audit/history of personnel CSV uploads.

**Snapshot:** 7 rows; 32 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"file_name" character varying NOT NULL
"success_count" integer NOT NULL
"failed_count" integer NOT NULL
"duplicate_count" integer NOT NULL
"stakeholder_type" character varying NOT NULL DEFAULT 'individual'::character varying
"failed_rows" jsonb
"uploaded_by" uuid NOT NULL
"is_internal" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_85ff7de43537376ac7915db2045`: `PRIMARY KEY (id)`
- FK `FK_dbe16c98a6b54a1d6c0bd86291c`: `FOREIGN KEY (uploaded_by) REFERENCES "user"(id)`

**Indexes**

- `PK_85ff7de43537376ac7915db2045`: `CREATE UNIQUE INDEX "PK_85ff7de43537376ac7915db2045" ON public.personnels_csv_upload_history USING btree (id)`

**Non-internal triggers**

- `audit_personnels_csv_upload_history_delete_trigger`: `CREATE TRIGGER audit_personnels_csv_upload_history_delete_trigger AFTER DELETE ON personnels_csv_upload_history FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_personnels_csv_upload_history_insert_trigger`: `CREATE TRIGGER audit_personnels_csv_upload_history_insert_trigger AFTER INSERT ON personnels_csv_upload_history FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_personnels_csv_upload_history_update_trigger`: `CREATE TRIGGER audit_personnels_csv_upload_history_update_trigger AFTER UPDATE ON personnels_csv_upload_history FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `piping_references`

**Kind/grain:** table; One parsed source-to-target text-piping reference within a survey.

**Snapshot:** 40 rows; 144 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT gen_random_uuid()
"createdAt" timestamp without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp without time zone NOT NULL DEFAULT now()
"survey_id" uuid NOT NULL
"target_question_id" uuid NOT NULL
"target_option_id" uuid
"source_question_id" uuid
"source_question_type" character varying(64) NOT NULL
"variable" character varying(128) NOT NULL
"fallback_text" text
"source_screen_id" uuid
"target_screen_id" uuid
"source_screen_order" integer NOT NULL DEFAULT 0
"target_screen_order" integer NOT NULL DEFAULT 0
"is_broken" boolean NOT NULL DEFAULT false
"broken_reason" character varying(128)
"source_attribute_id" uuid
```

**Constraints and relationships**

- PK `PK_piping_references`: `PRIMARY KEY (id)`
- FK `FK_piping_refs_source_attribute`: `FOREIGN KEY (source_attribute_id) REFERENCES question_option(id) ON DELETE SET NULL`
- FK `FK_piping_refs_source_question`: `FOREIGN KEY (source_question_id) REFERENCES question(id) ON DELETE SET NULL`
- FK `FK_piping_refs_source_screen`: `FOREIGN KEY (source_screen_id) REFERENCES question_screen(id) ON DELETE SET NULL`
- FK `FK_piping_refs_survey`: `FOREIGN KEY (survey_id) REFERENCES survey(id) ON DELETE CASCADE`
- FK `FK_piping_refs_target_option`: `FOREIGN KEY (target_option_id) REFERENCES question_option(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED`
- FK `FK_piping_refs_target_question`: `FOREIGN KEY (target_question_id) REFERENCES question(id) ON DELETE CASCADE`
- FK `FK_piping_refs_target_screen`: `FOREIGN KEY (target_screen_id) REFERENCES question_screen(id) ON DELETE SET NULL`

**Indexes**

- `PK_piping_references`: `CREATE UNIQUE INDEX "PK_piping_references" ON public.piping_references USING btree (id)`
- `idx_piping_refs_broken`: `CREATE INDEX idx_piping_refs_broken ON public.piping_references USING btree (survey_id, is_broken)`
- `idx_piping_refs_source_attribute`: `CREATE INDEX idx_piping_refs_source_attribute ON public.piping_references USING btree (source_attribute_id)`
- `idx_piping_refs_source_question`: `CREATE INDEX idx_piping_refs_source_question ON public.piping_references USING btree (source_question_id)`
- `idx_piping_refs_survey`: `CREATE INDEX idx_piping_refs_survey ON public.piping_references USING btree (survey_id)`
- `idx_piping_refs_target_question`: `CREATE INDEX idx_piping_refs_target_question ON public.piping_references USING btree (target_question_id)`

### `product`

**Kind/grain:** table; One product/sample configured in a survey, including internal name and blinding number.

**Snapshot:** 2,527 rows; 664 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"clientGeneratedId" character varying NOT NULL DEFAULT ''::character varying -- Client generated id that helps identify the document before it is assigned a unique id
"name" character varying NOT NULL DEFAULT ''::character varying -- Product name
"internalName" character varying DEFAULT ''::character varying -- Product internal name
"description" character varying DEFAULT ''::character varying -- Product description
"sortingOrderId" integer NOT NULL DEFAULT 0 -- Product order
"surveyId" uuid -- Product attached to survey
"documentId" uuid -- Document attached to product
"color" character varying DEFAULT ''::character varying -- Product color
"notes" text DEFAULT ''::text -- Product notes
"instructions" text DEFAULT ''::text -- Preparation instructions
"blindingNumber" character varying NOT NULL DEFAULT ''::character varying -- Product blinding number
"attachment_id" uuid -- Attachment ID for product
"expirationDate" timestamp without time zone
"productionDate" timestamp without time zone
"productIndex" integer NOT NULL DEFAULT 0 -- Product index
"legacy_product_id" character varying
"legacy_survey_id" character varying
```

**Constraints and relationships**

- PK `PK_bebc9158e480b949565b4dc7a82`: `PRIMARY KEY (id)`
- FK `FK_238e58f118aff7973203fb2e5e6`: `FOREIGN KEY ("documentId") REFERENCES image_doc(id)`
- FK `FK_2947d6b1afa89f4ee818d7237fa`: `FOREIGN KEY (attachment_id) REFERENCES image_doc(id)`
- FK `FK_8cdec03af52a31338c8cc3c3d62`: `FOREIGN KEY ("surveyId") REFERENCES survey(id)`
- Incoming foreign keys:
  - `benchmark_registry` via `benchmark_registry_product_id_fkey`
  - `product_display_order_item` via `FK_db55c5b3123ca8acf0cd0f280d1`

**Indexes**

- `PK_bebc9158e480b949565b4dc7a82`: `CREATE UNIQUE INDEX "PK_bebc9158e480b949565b4dc7a82" ON public.product USING btree (id)`
- `idx_product_survey_sorting`: `CREATE INDEX idx_product_survey_sorting ON public.product USING btree ("surveyId", "sortingOrderId")`

### `product_display_order`

**Kind/grain:** table; One participant/panel-specific product presentation sequence for a survey.

**Snapshot:** 4,675 rows; 1720 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"surveyId" uuid NOT NULL -- Survey ID this display config belongs to
"participantId" character varying -- Participant ID
"authCode" character varying -- Auth code for participant
"displayType" product_display_order_displaytype_enum NOT NULL DEFAULT 'default'::product_display_order_displaytype_enum -- Display type used for this order
"isActive" boolean NOT NULL DEFAULT true -- Whether this display config is active
"settings" jsonb NOT NULL DEFAULT '{}'::jsonb -- Product display settings
"legacy_display_id" character varying
"panel_assignment_id" uuid
```

**Constraints and relationships**

- PK `PK_4d426e89eadaaa07d688a991871`: `PRIMARY KEY (id)`
- FK `FK_303d78760a7b6002628091c6364`: `FOREIGN KEY ("surveyId") REFERENCES survey(id) ON DELETE CASCADE`
- Incoming foreign keys:
  - `product_display_order_item` via `FK_6fd2b45006982727514eb4c6546`
  - `survey_panel_code` via `FK_a437dec231f65c41c407db01898`

**Indexes**

- `PK_4d426e89eadaaa07d688a991871`: `CREATE UNIQUE INDEX "PK_4d426e89eadaaa07d688a991871" ON public.product_display_order USING btree (id)`
- `IDX_d054584ddf2f776e5107d118f5`: `CREATE UNIQUE INDEX "IDX_d054584ddf2f776e5107d118f5" ON public.product_display_order USING btree ("surveyId", "participantId")`

### `product_display_order_item`

**Kind/grain:** table; One product position within a product_display_order.

**Snapshot:** 34,041 rows; 7520 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"displayOrderId" uuid NOT NULL -- Display order ID this item belongs to
"productId" uuid NOT NULL -- Product ID in the display order
"position" integer NOT NULL -- Display order position (1-based)
"isFixed" boolean NOT NULL DEFAULT false -- Whether this product has a fixed position that should not be randomized
```

**Constraints and relationships**

- PK `PK_275e9be7c0110a22feb22c10eeb`: `PRIMARY KEY (id)`
- FK `FK_6fd2b45006982727514eb4c6546`: `FOREIGN KEY ("displayOrderId") REFERENCES product_display_order(id) ON DELETE CASCADE`
- FK `FK_db55c5b3123ca8acf0cd0f280d1`: `FOREIGN KEY ("productId") REFERENCES product(id) ON DELETE CASCADE`

**Indexes**

- `PK_275e9be7c0110a22feb22c10eeb`: `CREATE UNIQUE INDEX "PK_275e9be7c0110a22feb22c10eeb" ON public.product_display_order_item USING btree (id)`
- `IDX_4bba0c2cdff75f190a379d4c02`: `CREATE UNIQUE INDEX "IDX_4bba0c2cdff75f190a379d4c02" ON public.product_display_order_item USING btree ("displayOrderId", "position")`
- `IDX_a4630abce4898b091d874d871e`: `CREATE UNIQUE INDEX "IDX_a4630abce4898b091d874d871e" ON public.product_display_order_item USING btree ("displayOrderId", "productId")`

### `question`

**Kind/grain:** table; One question definition, including prompt, type, hierarchy, placement, settings, and piping flags.

**Snapshot:** 9,138 rows; 11 MB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"clientGeneratedId" character varying NOT NULL DEFAULT ''::character varying -- Client generated id that helps identify the document before it is assigned a unique id
"order" integer DEFAULT 0 -- Question order
"typeOfQuestion" character varying NOT NULL DEFAULT 'info'::character varying -- Question type
"prompt" character varying DEFAULT ''::character varying -- Question prompt
"promptHtml" character varying DEFAULT ''::character varying
"isRequired" boolean NOT NULL DEFAULT true -- Is the question mandatory?
"settings" jsonb NOT NULL DEFAULT '[]'::jsonb -- The question settings
"surveyId" uuid
"screenId" uuid
"sectionId" uuid
"documentId" uuid
"legacy_question_id" character varying
"legacy_survey_id" character varying
"legacy_screener_id" character varying
"parent_question_id" uuid
"language" character varying(32) NOT NULL DEFAULT 'en'::character varying
"textSegments" jsonb
"hasPiping" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_21e5786aa0ea704ae185a79b2d5`: `PRIMARY KEY (id)`
- FK `FK_9cdb3476db9b11dde0d64a5387b`: `FOREIGN KEY ("documentId") REFERENCES image_doc(id)`
- FK `FK_a1188e0f702ab268e0982049e5c`: `FOREIGN KEY ("surveyId") REFERENCES survey(id)`
- FK `FK_a149f8ccbb15d12b5ded62751d5`: `FOREIGN KEY ("screenId") REFERENCES question_screen(id)`
- FK `FK_c0dcb2fbd1522ea83d4750de69d`: `FOREIGN KEY ("sectionId") REFERENCES question_section(id)`
- FK `FK_question_parent_question_id`: `FOREIGN KEY (parent_question_id) REFERENCES question(id) ON DELETE CASCADE`
- Incoming foreign keys:
  - `answer` via `FK_c3d19a89541e4f0813f2fe09194`
  - `charts` via `FK_5f1eb36c6c815de9f992a26fdf9`
  - `enrollment` via `FK_c5f24b40db5f5d9f459dcc4638c`
  - `logic_rule` via `FK_5fdd2164ffedada9a20bc0bea0b`
  - `piping_references` via `FK_piping_refs_source_question`, `FK_piping_refs_target_question`
  - `question` via `FK_question_parent_question_id`
  - `question_group` via `FK_d6e699788a600d6a440509e4a47`
  - `question_library_item` via `FK_ed22e1b29b822651f1194333268`
  - `question_option` via `FK_747190c37a39feced5efcbb303f`
  - `question_pair` via `FK_6011f8c08e763971008de92b719`
  - `question_set` via `FK_c31653c6a9fb1bc6854f04f22b8`

**Indexes**

- `PK_21e5786aa0ea704ae185a79b2d5`: `CREATE UNIQUE INDEX "PK_21e5786aa0ea704ae185a79b2d5" ON public.question USING btree (id)`
- `IDX_question_parent_question_id`: `CREATE INDEX "IDX_question_parent_question_id" ON public.question USING btree (parent_question_id)`
- `idx_question_screen_id`: `CREATE INDEX idx_question_screen_id ON public.question USING btree ("screenId")`
- `idx_question_section_id`: `CREATE INDEX idx_question_section_id ON public.question USING btree ("sectionId")`
- `idx_question_survey_id`: `CREATE INDEX idx_question_survey_id ON public.question USING btree ("surveyId")`
- `idx_question_survey_type_performance`: `CREATE INDEX idx_question_survey_type_performance ON public.question USING btree ("surveyId", "typeOfQuestion", id) WHERE (("typeOfQuestion")::text = ANY (ARRAY[('triangle-test'::character varying)::text, ('tetrad-test'::character varying)::text]))`
- `idx_question_type`: `CREATE INDEX idx_question_type ON public.question USING btree ("typeOfQuestion")`

**Non-internal triggers**

- `audit_question_delete_trigger`: `CREATE TRIGGER audit_question_delete_trigger AFTER DELETE ON question FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_insert_trigger`: `CREATE TRIGGER audit_question_insert_trigger AFTER INSERT ON question FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_update_trigger`: `CREATE TRIGGER audit_question_update_trigger AFTER UPDATE ON question FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `question_group`

**Kind/grain:** table; One named ordered option group owned by a question.

**Snapshot:** 258 rows; 104 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"title" character varying(255) NOT NULL
"order" integer NOT NULL DEFAULT 1
"settings" jsonb
"question_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_3b2789ae1c494ff1bf8dd4d4607`: `PRIMARY KEY (id)`
- FK `FK_d6e699788a600d6a440509e4a47`: `FOREIGN KEY (question_id) REFERENCES question(id) ON DELETE CASCADE`
- Incoming foreign keys:
  - `logic_rule_condition` via `FK_8c20b975b8a4e705bebfd925444`
  - `question_option` via `FK_4fc7cbb6a87f2b956baecc1a5a3`

**Indexes**

- `PK_3b2789ae1c494ff1bf8dd4d4607`: `CREATE UNIQUE INDEX "PK_3b2789ae1c494ff1bf8dd4d4607" ON public.question_group USING btree (id)`
- `idx_question_group_question_id`: `CREATE INDEX idx_question_group_question_id ON public.question_group USING btree (question_id)`

**Non-internal triggers**

- `audit_question_group_delete_trigger`: `CREATE TRIGGER audit_question_group_delete_trigger AFTER DELETE ON question_group FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_group_insert_trigger`: `CREATE TRIGGER audit_question_group_insert_trigger AFTER INSERT ON question_group FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_group_update_trigger`: `CREATE TRIGGER audit_question_group_update_trigger AFTER UPDATE ON question_group FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `question_library`

**Kind/grain:** table; One reusable question library and its visibility/ownership metadata.

**Snapshot:** 80 rows; 72 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"title" character varying(255) NOT NULL
"type" question_library_type_enum NOT NULL DEFAULT 'my'::question_library_type_enum
"deleted_at" timestamp without time zone
"organization_id" uuid
"created_by" uuid
"order" integer NOT NULL DEFAULT 0
"language" character varying(32) NOT NULL DEFAULT 'en'::character varying
```

**Constraints and relationships**

- PK `PK_5319280778e805df03d729108ff`: `PRIMARY KEY (id)`
- FK `FK_03da259bffa5642b5bb6e378d5c`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_5f5771a85b0a2ce3530d48118c8`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- Incoming foreign keys:
  - `question_library_item` via `FK_661e22a3906971f38b6c5b66953`

**Indexes**

- `PK_5319280778e805df03d729108ff`: `CREATE UNIQUE INDEX "PK_5319280778e805df03d729108ff" ON public.question_library USING btree (id)`
- `IDX_question_library_type_language_order`: `CREATE INDEX "IDX_question_library_type_language_order" ON public.question_library USING btree (type, language, "order")`

### `question_library_item`

**Kind/grain:** table; One question placed in a reusable question library.

**Snapshot:** 301 rows; 128 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"title" character varying(255) NOT NULL
"question_library_id" uuid NOT NULL
"question_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_6f716258e18ca3e4aac0e92b723`: `PRIMARY KEY (id)`
- FK `FK_661e22a3906971f38b6c5b66953`: `FOREIGN KEY (question_library_id) REFERENCES question_library(id) ON DELETE CASCADE`
- FK `FK_ed22e1b29b822651f1194333268`: `FOREIGN KEY (question_id) REFERENCES question(id) ON DELETE CASCADE`

**Indexes**

- `PK_6f716258e18ca3e4aac0e92b723`: `CREATE UNIQUE INDEX "PK_6f716258e18ca3e4aac0e92b723" ON public.question_library_item USING btree (id)`
- `IDX_f2c4395347c81fbf5ae89a1510`: `CREATE UNIQUE INDEX "IDX_f2c4395347c81fbf5ae89a1510" ON public.question_library_item USING btree (question_library_id, question_id)`

### `question_option`

**Kind/grain:** table; One selectable/rating/matrix option for a question; analytical_value is the numeric code.

**Snapshot:** 38,629 rows; 12 MB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"label" text NOT NULL
"labelHtml" text DEFAULT ''::text
"type" question_option_type_enum
"order" integer DEFAULT 0 -- Question order
"optionSettings" jsonb DEFAULT '{}'::jsonb
"analytical_value" numeric NOT NULL DEFAULT 0
"question_id" uuid
"attachment_id" uuid
"group_id" uuid
"optionDefinition" text DEFAULT ''::text -- Individual balloting option definition
"legacy_question_id" character varying
"internal_name" character varying(255) DEFAULT ''::character varying -- Internal identifier for the option (e.g. matrix rows/columns)
"labelSegments" jsonb
"hasPiping" boolean NOT NULL DEFAULT false
```

**Constraints and relationships**

- PK `PK_64f8e42188891f2b0610017c8f9`: `PRIMARY KEY (id)`
- FK `FK_31b4e24cc0b2ee1b54450e56bd1`: `FOREIGN KEY (attachment_id) REFERENCES image_doc(id)`
- FK `FK_4fc7cbb6a87f2b956baecc1a5a3`: `FOREIGN KEY (group_id) REFERENCES question_group(id) ON DELETE CASCADE`
- FK `FK_747190c37a39feced5efcbb303f`: `FOREIGN KEY (question_id) REFERENCES question(id)`
- Incoming foreign keys:
  - `answered_question_options` via `FK_faef381e6b0b68a7aca90470223`, `FK_matrix_row_option_id`
  - `logic_rule_condition` via `FK_ad31557cef294342312ee1be540`
  - `piping_references` via `FK_piping_refs_source_attribute`, `FK_piping_refs_target_option`
  - `question_pair` via `FK_9f42b595fe44fb80a942b34d38c`, `FK_d27872a460267a7ea5ca70dbb4e`, `FK_question_pair_middle_option_id`

**Indexes**

- `PK_64f8e42188891f2b0610017c8f9`: `CREATE UNIQUE INDEX "PK_64f8e42188891f2b0610017c8f9" ON public.question_option USING btree (id)`
- `idx_question_option_group`: `CREATE INDEX idx_question_option_group ON public.question_option USING btree (question_id, group_id)`
- `idx_question_option_matrix_type`: `CREATE INDEX idx_question_option_matrix_type ON public.question_option USING btree (question_id, type)`

**Non-internal triggers**

- `audit_question_option_delete_trigger`: `CREATE TRIGGER audit_question_option_delete_trigger AFTER DELETE ON question_option FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_option_insert_trigger`: `CREATE TRIGGER audit_question_option_insert_trigger AFTER INSERT ON question_option FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_option_update_trigger`: `CREATE TRIGGER audit_question_option_update_trigger AFTER UPDATE ON question_option FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `question_pair`

**Kind/grain:** table; One left/right (optionally middle) option combination for a paired question.

**Snapshot:** 4,460 rows; 1216 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"left_option_id" uuid NOT NULL
"right_option_id" uuid NOT NULL
"question_id" uuid NOT NULL
"legacy_question_id" character varying
"legacy_pair_id" character varying
"legacyData" jsonb
"middle_option_id" uuid
```

**Constraints and relationships**

- PK `PK_7acf301b8d928ae0d1995e9d0a7`: `PRIMARY KEY (id)`
- FK `FK_6011f8c08e763971008de92b719`: `FOREIGN KEY (question_id) REFERENCES question(id)`
- FK `FK_9f42b595fe44fb80a942b34d38c`: `FOREIGN KEY (left_option_id) REFERENCES question_option(id)`
- FK `FK_d27872a460267a7ea5ca70dbb4e`: `FOREIGN KEY (right_option_id) REFERENCES question_option(id)`
- FK `FK_question_pair_middle_option_id`: `FOREIGN KEY (middle_option_id) REFERENCES question_option(id)`
- Incoming foreign keys:
  - `answered_question_options` via `FK_question_pair_id`

**Indexes**

- `PK_7acf301b8d928ae0d1995e9d0a7`: `CREATE UNIQUE INDEX "PK_7acf301b8d928ae0d1995e9d0a7" ON public.question_pair USING btree (id)`
- `idx_question_pair_question_id`: `CREATE INDEX idx_question_pair_question_id ON public.question_pair USING btree (question_id)`

### `question_screen`

**Kind/grain:** table; One ordered screen in a survey section.

**Snapshot:** 7,174 rows; 2584 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"clientGeneratedId" character varying DEFAULT ''::character varying -- Client generated id that helps identify the document before it is assigned a unique id
"name" character varying DEFAULT ''::character varying -- Screen name
"order" integer DEFAULT 0 -- The index at which the screen is sorted at
"surveyId" uuid -- Screen attached to survey
"sectionId" uuid -- Screen attached to section
"content" character varying DEFAULT ''::character varying
"screenType" character varying DEFAULT ''::character varying
"settings" jsonb DEFAULT '{}'::jsonb
```

**Constraints and relationships**

- PK `PK_dcf4a9ad8d3aa5e6e14f014c0a8`: `PRIMARY KEY (id)`
- FK `FK_29b9f3062b135f23672ea6b995e`: `FOREIGN KEY ("sectionId") REFERENCES question_section(id)`
- FK `FK_584e63fd36246affee152b10547`: `FOREIGN KEY ("surveyId") REFERENCES survey(id)`
- Incoming foreign keys:
  - `logic` via `FK_0d5ce05d0e29ea6ed61cce50dcb`
  - `logic_rule` via `FK_de8005fcadf5269b340010b4dc2`
  - `piping_references` via `FK_piping_refs_source_screen`, `FK_piping_refs_target_screen`
  - `question` via `FK_a149f8ccbb15d12b5ded62751d5`

**Indexes**

- `PK_dcf4a9ad8d3aa5e6e14f014c0a8`: `CREATE UNIQUE INDEX "PK_dcf4a9ad8d3aa5e6e14f014c0a8" ON public.question_screen USING btree (id)`
- `idx_question_screen_survey_type_order`: `CREATE INDEX idx_question_screen_survey_type_order ON public.question_screen USING btree ("surveyId", "screenType", "order")`

**Non-internal triggers**

- `audit_question_screen_delete_trigger`: `CREATE TRIGGER audit_question_screen_delete_trigger AFTER DELETE ON question_screen FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_screen_insert_trigger`: `CREATE TRIGGER audit_question_screen_insert_trigger AFTER INSERT ON question_screen FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_screen_update_trigger`: `CREATE TRIGGER audit_question_screen_update_trigger AFTER UPDATE ON question_screen FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `question_section`

**Kind/grain:** table; One ordered section of a survey; productSection distinguishes product tasting sections.

**Snapshot:** 2,935 rows; 536 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"clientGeneratedId" character varying NOT NULL DEFAULT ''::character varying -- Client generated id that helps identify the document before it is assigned a unique id
"name" character varying(120) DEFAULT ''::character varying -- Section name
"order" integer DEFAULT 0 -- The index at which the section is sorted
"productSection" boolean NOT NULL DEFAULT true -- Is the section a product loop
"surveyId" uuid -- Section attached to survey
```

**Constraints and relationships**

- PK `PK_981630dff92c9c1a0d7efac02d4`: `PRIMARY KEY (id)`
- FK `FK_1baa50611af0c075d937c7b7040`: `FOREIGN KEY ("surveyId") REFERENCES survey(id)`
- Incoming foreign keys:
  - `question` via `FK_c0dcb2fbd1522ea83d4750de69d`
  - `question_screen` via `FK_29b9f3062b135f23672ea6b995e`

**Indexes**

- `PK_981630dff92c9c1a0d7efac02d4`: `CREATE UNIQUE INDEX "PK_981630dff92c9c1a0d7efac02d4" ON public.question_section USING btree (id)`
- `idx_question_section_survey`: `CREATE INDEX idx_question_section_survey ON public.question_section USING btree ("surveyId")`

**Non-internal triggers**

- `audit_question_section_delete_trigger`: `CREATE TRIGGER audit_question_section_delete_trigger AFTER DELETE ON question_section FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_section_insert_trigger`: `CREATE TRIGGER audit_question_section_insert_trigger AFTER INSERT ON question_section FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_section_update_trigger`: `CREATE TRIGGER audit_question_section_update_trigger AFTER UPDATE ON question_section FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `question_set`

**Kind/grain:** table; One tray/set/combination configured for a discrimination or set-based question.

**Snapshot:** 19,631 rows; 6632 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"tray_id" character varying NOT NULL
"combination" character varying NOT NULL
"setData" jsonb NOT NULL DEFAULT '{}'::jsonb
"status" question_set_status_enum NOT NULL DEFAULT 'pending'::question_set_status_enum
"question_id" uuid
```

**Constraints and relationships**

- PK `PK_384a616ea05ec06da55c844430b`: `PRIMARY KEY (id)`
- FK `FK_c31653c6a9fb1bc6854f04f22b8`: `FOREIGN KEY (question_id) REFERENCES question(id)`
- Incoming foreign keys:
  - `answered_question_options` via `FK_5aea890c0da849dac85d28a3fd2`
  - `survey_panel_code` via `FK_ce42b7bf6811a2fdb4adc52f791`

**Indexes**

- `PK_384a616ea05ec06da55c844430b`: `CREATE UNIQUE INDEX "PK_384a616ea05ec06da55c844430b" ON public.question_set USING btree (id)`
- `idx_qs_id`: `CREATE INDEX idx_qs_id ON public.question_set USING btree (id)`
- `idx_qs_question_id`: `CREATE INDEX idx_qs_question_id ON public.question_set USING btree (question_id)`
- `idx_question_set_question_id`: `CREATE INDEX idx_question_set_question_id ON public.question_set USING btree (question_id)`
- `idx_question_set_question_status`: `CREATE INDEX idx_question_set_question_status ON public.question_set USING btree (question_id, status)`
- `idx_question_set_survey`: `CREATE INDEX idx_question_set_survey ON public.question_set USING btree (question_id)`

**Non-internal triggers**

- `audit_question_set_delete_trigger`: `CREATE TRIGGER audit_question_set_delete_trigger AFTER DELETE ON question_set FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_set_insert_trigger`: `CREATE TRIGGER audit_question_set_insert_trigger AFTER INSERT ON question_set FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_question_set_update_trigger`: `CREATE TRIGGER audit_question_set_update_trigger AFTER UPDATE ON question_set FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `role`

**Kind/grain:** table; Authorization role definition.

**Snapshot:** 14 rows; 88 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"application_id" uuid
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"name" character varying(128) NOT NULL
"code" character varying(128) NOT NULL
```

**Constraints and relationships**

- PK `PK_b36bcfe02fc8de3c57a8b2391c2`: `PRIMARY KEY (id)`
- UNIQUE `UQ_ROLE_APPLICATION_NAME`: `UNIQUE (application_id, name)`
- UNIQUE `UQ_ee999bb389d7ac0fd967172c41f`: `UNIQUE (code)`
- FK `FK_04a09925beea59e864e921db4a1`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_253ae6ba531cf922f4cb1580a95`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- FK `FK_66c5a07f8a92982db8c909ad4c6`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_7b5b6fdd043b90e1d461b4b681d`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_858c871a036f61e56e2740c7cda`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_89fe3003757abd864d4ad720a1e`: `FOREIGN KEY (application_id) REFERENCES application(id)`
- Incoming foreign keys:
  - `assigned_stakeholder_users` via `FK_3f8a6e4ce419d2b6c2aa7b16d1c`
  - `license_app_data_metadata` via `FK_1a1f21ba8e49cde85f5373260ba`
  - `organization_roles_role` via `FK_b99e8125f26dec2b96d6c0cba31`
  - `role_organizations_organization` via `FK_6ae15e217913dbf33369490b5ad`
  - `role_permissions_permission` via `FK_0167acb6e0ccfcf0c6c140cec4a`
  - `role_users_user` via `FK_ed6edac7184b013d4bd58d60e54`
  - `user_organization_role_permission` via `FK_0381d94d24c4a28a393a1890daf`
  - `user_roles_role` via `FK_4be2f7adf862634f5f803d246b8`

**Indexes**

- `PK_b36bcfe02fc8de3c57a8b2391c2`: `CREATE UNIQUE INDEX "PK_b36bcfe02fc8de3c57a8b2391c2" ON public.role USING btree (id)`
- `UQ_ROLE_APPLICATION_NAME`: `CREATE UNIQUE INDEX "UQ_ROLE_APPLICATION_NAME" ON public.role USING btree (application_id, name)`
- `UQ_ee999bb389d7ac0fd967172c41f`: `CREATE UNIQUE INDEX "UQ_ee999bb389d7ac0fd967172c41f" ON public.role USING btree (code)`
- `IDX_ROLE_APPLICATION_ID`: `CREATE INDEX "IDX_ROLE_APPLICATION_ID" ON public.role USING btree (application_id)`
- `IDX_ROLE_NAME`: `CREATE INDEX "IDX_ROLE_NAME" ON public.role USING btree (name)`

**Non-internal triggers**

- `audit_role_delete_trigger`: `CREATE TRIGGER audit_role_delete_trigger AFTER DELETE ON role FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_role_insert_trigger`: `CREATE TRIGGER audit_role_insert_trigger AFTER INSERT ON role FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_role_update_trigger`: `CREATE TRIGGER audit_role_update_trigger AFTER UPDATE ON role FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `role_organizations_organization`

**Kind/grain:** table; Alternate role-to-organization join table.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"roleId" uuid NOT NULL
"organizationId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_9d35631931c0718295090020cc1`: `PRIMARY KEY ("roleId", "organizationId")`
- FK `FK_6ae15e217913dbf33369490b5ad`: `FOREIGN KEY ("roleId") REFERENCES role(id) ON UPDATE CASCADE ON DELETE CASCADE`
- FK `FK_d055935ccb809b70d89e757dfa0`: `FOREIGN KEY ("organizationId") REFERENCES organization(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_9d35631931c0718295090020cc1`: `CREATE UNIQUE INDEX "PK_9d35631931c0718295090020cc1" ON public.role_organizations_organization USING btree ("roleId", "organizationId")`
- `IDX_6ae15e217913dbf33369490b5a`: `CREATE INDEX "IDX_6ae15e217913dbf33369490b5a" ON public.role_organizations_organization USING btree ("roleId")`
- `IDX_d055935ccb809b70d89e757dfa`: `CREATE INDEX "IDX_d055935ccb809b70d89e757dfa" ON public.role_organizations_organization USING btree ("organizationId")`

### `role_permissions_permission`

**Kind/grain:** table; Many-to-many join between roles and permissions.

**Snapshot:** 57 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"role_id" uuid NOT NULL
"permission_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_32d63c82505b0b1d565761ae201`: `PRIMARY KEY (role_id, permission_id)`
- FK `FK_0167acb6e0ccfcf0c6c140cec4a`: `FOREIGN KEY (role_id) REFERENCES role(id) ON UPDATE CASCADE ON DELETE CASCADE`
- FK `FK_2d3e8e7c82bdee8553b6f1e3325`: `FOREIGN KEY (permission_id) REFERENCES permission(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_32d63c82505b0b1d565761ae201`: `CREATE UNIQUE INDEX "PK_32d63c82505b0b1d565761ae201" ON public.role_permissions_permission USING btree (role_id, permission_id)`
- `IDX_0167acb6e0ccfcf0c6c140cec4`: `CREATE INDEX "IDX_0167acb6e0ccfcf0c6c140cec4" ON public.role_permissions_permission USING btree (role_id)`
- `IDX_2d3e8e7c82bdee8553b6f1e332`: `CREATE INDEX "IDX_2d3e8e7c82bdee8553b6f1e332" ON public.role_permissions_permission USING btree (permission_id)`

### `role_users_user`

**Kind/grain:** table; Legacy/alternate role-to-user join table.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"roleId" uuid NOT NULL
"userId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_46403d6ce64cde119287c876ca3`: `PRIMARY KEY ("roleId", "userId")`
- FK `FK_a88fcb405b56bf2e2646e9d4797`: `FOREIGN KEY ("userId") REFERENCES "user"(id) ON UPDATE CASCADE ON DELETE CASCADE`
- FK `FK_ed6edac7184b013d4bd58d60e54`: `FOREIGN KEY ("roleId") REFERENCES role(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_46403d6ce64cde119287c876ca3`: `CREATE UNIQUE INDEX "PK_46403d6ce64cde119287c876ca3" ON public.role_users_user USING btree ("roleId", "userId")`
- `IDX_a88fcb405b56bf2e2646e9d479`: `CREATE INDEX "IDX_a88fcb405b56bf2e2646e9d479" ON public.role_users_user USING btree ("userId")`
- `IDX_ed6edac7184b013d4bd58d60e5`: `CREATE INDEX "IDX_ed6edac7184b013d4bd58d60e5" ON public.role_users_user USING btree ("roleId")`

### `service_clients`

**Kind/grain:** table; OAuth/service-client credentials and configuration; sensitive system data.

**Snapshot:** 1 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"client_id" character varying(100) NOT NULL
"client_secret_hash" text NOT NULL
"name" character varying(255)
"scopes" text[] NOT NULL DEFAULT '{}'::text[]
"active" boolean NOT NULL DEFAULT true
"rotated_at" timestamp with time zone
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
```

**Constraints and relationships**

- PK `service_clients_pkey`: `PRIMARY KEY (id)`
- UNIQUE `service_clients_client_id_key`: `UNIQUE (client_id)`

**Indexes**

- `service_clients_pkey`: `CREATE UNIQUE INDEX service_clients_pkey ON public.service_clients USING btree (id)`
- `service_clients_client_id_key`: `CREATE UNIQUE INDEX service_clients_client_id_key ON public.service_clients USING btree (client_id)`
- `idx_service_clients_client_id`: `CREATE INDEX idx_service_clients_client_id ON public.service_clients USING btree (client_id)`

### `shared_access`

**Kind/grain:** table; Root sharing grant for a survey/template/folder/report module.

**Snapshot:** 138 rows; 72 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"module_type" shared_access_module_type_enum NOT NULL DEFAULT 'survey'::shared_access_module_type_enum
"module_id" character varying NOT NULL
"public_meta" jsonb DEFAULT '{}'::jsonb
"deleted_at" timestamp without time zone
"created_by" uuid NOT NULL
"updated_by" uuid
"global_access_level" shared_access_global_access_level_enum
```

**Constraints and relationships**

- PK `PK_6e7c5d6651d739c2c0b31ad3774`: `PRIMARY KEY (id)`
- FK `FK_47809ed2925bb364f7941b3cbf9`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_d919a2da734ca453c0915845944`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `shared_access_group` via `FK_71201a7b30bf775297ae44a1e20`
  - `shared_access_log` via `FK_be8da11b57eac64ba8709fca6fe`
  - `shared_access_organization` via `FK_8d862a762126281e92891b70362`
  - `shared_access_user` via `FK_cb9b7530404cea116dd0ef9dbfd`

**Indexes**

- `PK_6e7c5d6651d739c2c0b31ad3774`: `CREATE UNIQUE INDEX "PK_6e7c5d6651d739c2c0b31ad3774" ON public.shared_access USING btree (id)`

### `shared_access_group`

**Kind/grain:** table; Per-access-group access level for a sharing grant.

**Snapshot:** 17 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"access_level" shared_access_group_access_level_enum NOT NULL DEFAULT 'viewer'::shared_access_group_access_level_enum
"shared_access_id" uuid NOT NULL
"group_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid NOT NULL
"updated_by" uuid
```

**Constraints and relationships**

- PK `PK_1375ba1afa7d51ff656ddc59d52`: `PRIMARY KEY (id)`
- FK `FK_49c5d6eb2daed9f2cf6ef2d8565`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_71201a7b30bf775297ae44a1e20`: `FOREIGN KEY (shared_access_id) REFERENCES shared_access(id)`
- FK `FK_92b7d67f1f1aa3155142e05701a`: `FOREIGN KEY (group_id) REFERENCES access_group(id)`
- FK `FK_948d9ae39d430c3f13e62fcaa5f`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_1375ba1afa7d51ff656ddc59d52`: `CREATE UNIQUE INDEX "PK_1375ba1afa7d51ff656ddc59d52" ON public.shared_access_group USING btree (id)`

### `shared_access_log`

**Kind/grain:** table; History/events for shared access.

**Snapshot:** 30 rows; 32 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_email" character varying
"visited_at" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"device_info" jsonb DEFAULT '{}'::jsonb
"shared_access_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_41a5310b13d37e9f53b5d801883`: `PRIMARY KEY (id)`
- FK `FK_be8da11b57eac64ba8709fca6fe`: `FOREIGN KEY (shared_access_id) REFERENCES shared_access(id)`

**Indexes**

- `PK_41a5310b13d37e9f53b5d801883`: `CREATE UNIQUE INDEX "PK_41a5310b13d37e9f53b5d801883" ON public.shared_access_log USING btree (id)`

### `shared_access_organization`

**Kind/grain:** table; Per-organization access level for a sharing grant.

**Snapshot:** 23 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"access_level" shared_access_organization_access_level_enum NOT NULL DEFAULT 'viewer'::shared_access_organization_access_level_enum
"organization_id" uuid NOT NULL
"shared_access_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid NOT NULL
"updated_by" uuid
```

**Constraints and relationships**

- PK `PK_79e6675a91ea1d5144c44c0ee03`: `PRIMARY KEY (id)`
- FK `FK_4ce8205d9573435f5ddeb43df39`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_6333b4cbb6f4a4b2c57070c180a`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_8d862a762126281e92891b70362`: `FOREIGN KEY (shared_access_id) REFERENCES shared_access(id)`
- FK `FK_bb1a9016031407322d221ec8e82`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`

**Indexes**

- `PK_79e6675a91ea1d5144c44c0ee03`: `CREATE UNIQUE INDEX "PK_79e6675a91ea1d5144c44c0ee03" ON public.shared_access_organization USING btree (id)`

### `shared_access_user`

**Kind/grain:** table; Per-user access level for a sharing grant.

**Snapshot:** 176 rows; 80 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"access_level" shared_access_user_access_level_enum NOT NULL DEFAULT 'viewer'::shared_access_user_access_level_enum
"message" character varying(512)
"user_id" uuid NOT NULL
"shared_access_id" uuid NOT NULL
"deleted_at" timestamp without time zone
"created_by" uuid NOT NULL
"updated_by" uuid
```

**Constraints and relationships**

- PK `PK_5240d3580a4c2cc75be0fe3692b`: `PRIMARY KEY (id)`
- FK `FK_56125612987f970b1999f37da6c`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_cb9b7530404cea116dd0ef9dbfd`: `FOREIGN KEY (shared_access_id) REFERENCES shared_access(id)`
- FK `FK_dc9c67566acaed58f335fcb35f1`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- FK `FK_f09097ab9216668ddb66b2b2511`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`

**Indexes**

- `PK_5240d3580a4c2cc75be0fe3692b`: `CREATE UNIQUE INDEX "PK_5240d3580a4c2cc75be0fe3692b" ON public.shared_access_user USING btree (id)`

### `survey`

**Kind/grain:** table; One survey or survey template/container; root of questionnaire design and fieldwork scope.

**Snapshot:** 1,347 rows; 864 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"photo" character varying DEFAULT ''::character varying -- cover photo
"photoStyle" character varying DEFAULT ''::character varying -- cover photo
"internalName" character varying NOT NULL DEFAULT ''::character varying -- Survey's internal name
"title" character varying NOT NULL DEFAULT ''::character varying -- Survey's title
"uniqueName" character varying NOT NULL DEFAULT ''::character varying -- Survey's unique name
"openedAt" timestamp without time zone -- Survey last opened at
"publishedAt" timestamp without time zone -- Survey last published at
"state" character varying NOT NULL DEFAULT 'draft'::character varying -- Survey state
"type" character varying NOT NULL DEFAULT 'simple-questionnaire'::character varying
"isActive" boolean NOT NULL DEFAULT false -- Survey isActive
"panelCount" integer DEFAULT 0 -- SurveySettings's panelCount
"productDisplayCount" integer DEFAULT 0 -- SurveySettings's productDisplayCount
"isTemplate" boolean NOT NULL DEFAULT false -- SurveySettings's isTemplate
"panelListViewCompletedResponsesBeforeTasting" boolean NOT NULL DEFAULT true -- allow panel list to view responses before tasting
"productDisplayType" character varying NOT NULL DEFAULT 'none'::character varying -- SurveySettings's productDisplayType
"productDisplayState" character varying NOT NULL DEFAULT 'unactive'::character varying -- SurveySettings's productDisplayState
"lastModifierId" uuid -- Survey last modifier id
"organization_id" uuid -- Survey owner id
"creatorId" uuid -- Survey creator id
"folderId" uuid -- Survey folder id
"legacy_survey_id" character varying
"legacy_screener_id" character varying
"settings" jsonb DEFAULT '{}'::jsonb
"is_container" boolean NOT NULL DEFAULT false
"archived_at" timestamp without time zone
"archived_by" uuid
"packing_slips_instructions_text" text
"country" character varying(128)
"is_benchmark_source" boolean NOT NULL DEFAULT false
"benchmark_category_label" character varying(200)
```

**Constraints and relationships**

- PK `PK_f0da32b9181e9c02ecf0be11ed3`: `PRIMARY KEY (id)`
- FK `FK_286c488d686d7b45280e51d29c0`: `FOREIGN KEY ("lastModifierId") REFERENCES "user"(id)`
- FK `FK_445c4d061fabdcd15bdd7e6ce2f`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_51124fdaa8350196ac22d109697`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_7982db0113f0d01664e9e79a43a`: `FOREIGN KEY ("folderId") REFERENCES folder(id)`
- FK `FK_a2e6e9ab8f1ff04cbf31da646e7`: `FOREIGN KEY (organization_id) REFERENCES organization(id)`
- FK `FK_cdb7a72b955b60b44f84d9e97f7`: `FOREIGN KEY ("creatorId") REFERENCES "user"(id)`
- Incoming foreign keys:
  - `aggregations` via `FK_f3a3e1b1c9ddd4af3730ad76879`
  - `benchmark_registry` via `benchmark_registry_survey_id_fkey`
  - `charts_reports` via `FK_a8b81ebd3f40f284625c71f69b6`
  - `document` via `FK_0f79cd5864eb064ad9f760665f1`
  - `enrollment` via `FK_3a32bd4f71ca65546cf7a751b8e`
  - `image_doc` via `FK_3610c56c67e1431596970004b26`
  - `logic` via `FK_0a77835a7754fc242a0711e4492`
  - `piping_references` via `FK_piping_refs_survey`
  - `product` via `FK_8cdec03af52a31338c8cc3c3d62`
  - `product_display_order` via `FK_303d78760a7b6002628091c6364`
  - `question` via `FK_a1188e0f702ab268e0982049e5c`
  - `question_screen` via `FK_584e63fd36246affee152b10547`
  - `question_section` via `FK_1baa50611af0c075d937c7b7040`
  - `survey_nomenclature` via `FK_survey_nomenclature_survey`
  - `survey_panel` via `FK_survey_panel_survey_id`
  - `survey_panel_code` via `FK_e16ec36caf95df396db13998b2e`
  - `survey_panel_stats` via `FK_sps_survey`

**Indexes**

- `PK_f0da32b9181e9c02ecf0be11ed3`: `CREATE UNIQUE INDEX "PK_f0da32b9181e9c02ecf0be11ed3" ON public.survey USING btree (id)`
- `IDX_SURVEY_ORGANIZATION_ID`: `CREATE INDEX "IDX_SURVEY_ORGANIZATION_ID" ON public.survey USING btree (organization_id)`
- `IDX_SURVEY_UNIQUE_NAME`: `CREATE INDEX "IDX_SURVEY_UNIQUE_NAME" ON public.survey USING btree ("uniqueName")`
- `idx_survey_creator`: `CREATE INDEX idx_survey_creator ON public.survey USING btree ("creatorId")`
- `idx_survey_folder`: `CREATE INDEX idx_survey_folder ON public.survey USING btree ("folderId")`

**Non-internal triggers**

- `audit_survey_delete_trigger`: `CREATE TRIGGER audit_survey_delete_trigger AFTER DELETE ON survey FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_survey_insert_trigger`: `CREATE TRIGGER audit_survey_insert_trigger AFTER INSERT ON survey FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_survey_update_trigger`: `CREATE TRIGGER audit_survey_update_trigger AFTER UPDATE ON survey FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `survey_nomenclature`

**Kind/grain:** table; Sparse naming/tag snapshot for a survey: client, category, test type, formula, date, and generated names.

**Snapshot:** 16 rows; 80 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"survey_id" uuid NOT NULL
"nomenclature_client_id" uuid NOT NULL
"client_label_snapshot" character varying(100) NOT NULL
"formula_fw" character varying(128)
"sensorik_code" character varying(50) NOT NULL
"survey_date" date NOT NULL
"nomenclature_category_id" uuid NOT NULL
"category_code_snapshot" character varying(100) NOT NULL
"category_label_snapshot" character varying(100) NOT NULL
"nomenclature_type_of_test_id" uuid NOT NULL
"type_of_test_code_snapshot" character varying(100) NOT NULL
"type_of_test_label_snapshot" character varying(100) NOT NULL
"note" character varying(200)
"generated_name" character varying(512) NOT NULL
"generated_title" character varying(512) NOT NULL
"unique_name" character varying(512)
"survey_url" character varying(1024)
"created_by" uuid
"updated_by" uuid
"createdAt" timestamp(6) without time zone NOT NULL DEFAULT now()
"updatedAt" timestamp(6) without time zone NOT NULL DEFAULT now()
"organization_id" uuid
```

**Constraints and relationships**

- PK `PK_survey_nomenclature`: `PRIMARY KEY (id)`
- UNIQUE `UQ_SURVEY_NOMENCLATURE_SURVEY_ID`: `UNIQUE (survey_id)`
- FK `FK_survey_nomenclature_category`: `FOREIGN KEY (nomenclature_category_id) REFERENCES nomenclature_category(id) ON DELETE RESTRICT`
- FK `FK_survey_nomenclature_client`: `FOREIGN KEY (nomenclature_client_id) REFERENCES nomenclature_client(id) ON DELETE RESTRICT`
- FK `FK_survey_nomenclature_created_by`: `FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL`
- FK `FK_survey_nomenclature_organization`: `FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE`
- FK `FK_survey_nomenclature_survey`: `FOREIGN KEY (survey_id) REFERENCES survey(id) ON DELETE CASCADE`
- FK `FK_survey_nomenclature_type_of_test`: `FOREIGN KEY (nomenclature_type_of_test_id) REFERENCES nomenclature_type_of_test(id) ON DELETE RESTRICT`
- FK `FK_survey_nomenclature_updated_by`: `FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL`

**Indexes**

- `PK_survey_nomenclature`: `CREATE UNIQUE INDEX "PK_survey_nomenclature" ON public.survey_nomenclature USING btree (id)`
- `UQ_SURVEY_NOMENCLATURE_ORG_FORMULA_FW`: `CREATE UNIQUE INDEX "UQ_SURVEY_NOMENCLATURE_ORG_FORMULA_FW" ON public.survey_nomenclature USING btree (organization_id, formula_fw) WHERE (formula_fw IS NOT NULL)`
- `UQ_SURVEY_NOMENCLATURE_SURVEY_ID`: `CREATE UNIQUE INDEX "UQ_SURVEY_NOMENCLATURE_SURVEY_ID" ON public.survey_nomenclature USING btree (survey_id)`
- `IDX_survey_nomenclature_survey_id`: `CREATE INDEX "IDX_survey_nomenclature_survey_id" ON public.survey_nomenclature USING btree (survey_id)`

### `survey_panel`

**Kind/grain:** table; Assignment join between a survey and a panel.

**Snapshot:** 62 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"survey_id" uuid NOT NULL
"panel_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_survey_panel`: `PRIMARY KEY (survey_id, panel_id)`
- FK `FK_survey_panel_panel_id`: `FOREIGN KEY (panel_id) REFERENCES panel(id) ON DELETE CASCADE`
- FK `FK_survey_panel_survey_id`: `FOREIGN KEY (survey_id) REFERENCES survey(id) ON DELETE CASCADE`

**Indexes**

- `PK_survey_panel`: `CREATE UNIQUE INDEX "PK_survey_panel" ON public.survey_panel USING btree (survey_id, panel_id)`
- `IDX_survey_panel_panel_id`: `CREATE INDEX "IDX_survey_panel_panel_id" ON public.survey_panel USING btree (panel_id)`
- `IDX_survey_panel_survey_id`: `CREATE INDEX "IDX_survey_panel_survey_id" ON public.survey_panel USING btree (survey_id)`

### `survey_panel_code`

**Kind/grain:** table; Assignment of a panel code to a survey/panel and optional set/display order.

**Snapshot:** 0 rows; 8192 bytes total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"deleted_at" timestamp without time zone
"survey_id" uuid NOT NULL
"panel_id" uuid NOT NULL
"panel_code_id" uuid
"question_set_id" uuid
"product_display_order_id" uuid
"deleted_by" uuid
```

**Constraints and relationships**

- PK `PK_260bf72c512a74aec288e119848`: `PRIMARY KEY (id)`
- FK `FK_1089eaede6f136cdc851824c67d`: `FOREIGN KEY (panel_code_id) REFERENCES panel_code(id)`
- FK `FK_680ff0740b0cdb2b5b4f9e2cb7a`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_a437dec231f65c41c407db01898`: `FOREIGN KEY (product_display_order_id) REFERENCES product_display_order(id)`
- FK `FK_ce42b7bf6811a2fdb4adc52f791`: `FOREIGN KEY (question_set_id) REFERENCES question_set(id)`
- FK `FK_e16ec36caf95df396db13998b2e`: `FOREIGN KEY (survey_id) REFERENCES survey(id)`
- FK `FK_fe16355d0335bdaa7dc4a5d2083`: `FOREIGN KEY (panel_id) REFERENCES panel(id)`
- CHECK `survey_panel_code_question_set_or_display_order_check`: `CHECK (panel_code_id IS NULL OR question_set_id IS NOT NULL OR product_display_order_id IS NOT NULL)`

**Indexes**

- `PK_260bf72c512a74aec288e119848`: `CREATE UNIQUE INDEX "PK_260bf72c512a74aec288e119848" ON public.survey_panel_code USING btree (id)`

### `survey_panel_stats`

**Kind/grain:** table; Fieldwork status timeline for a survey-panel-panelist/code/enrollment combination.

**Snapshot:** 755 rows; 312 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"survey_id" uuid NOT NULL
"panel_id" uuid NOT NULL
"panelist_id" uuid
"panel_code_id" uuid
"enrollment_id" uuid
"status" survey_panel_stats_status_enum NOT NULL DEFAULT 'not_started'::survey_panel_stats_status_enum
"status_started_at" timestamp without time zone
"started_at" timestamp without time zone
"completed_at" timestamp without time zone
"last_activity_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_survey_panel_stats_id`: `PRIMARY KEY (id)`
- FK `FK_sps_enrollment`: `FOREIGN KEY (enrollment_id) REFERENCES enrollment(id)`
- FK `FK_sps_panel`: `FOREIGN KEY (panel_id) REFERENCES panel(id)`
- FK `FK_sps_panel_code`: `FOREIGN KEY (panel_code_id) REFERENCES panel_code(id)`
- FK `FK_sps_panelist`: `FOREIGN KEY (panelist_id) REFERENCES panelist(id)`
- FK `FK_sps_survey`: `FOREIGN KEY (survey_id) REFERENCES survey(id)`

**Indexes**

- `PK_survey_panel_stats_id`: `CREATE UNIQUE INDEX "PK_survey_panel_stats_id" ON public.survey_panel_stats USING btree (id)`
- `idx_sps_unique_participant`: `CREATE UNIQUE INDEX idx_sps_unique_participant ON public.survey_panel_stats USING btree (survey_id, panel_id, panelist_id, panel_code_id)`
- `idx_sps_panel_status`: `CREATE INDEX idx_sps_panel_status ON public.survey_panel_stats USING btree (panel_id, status)`
- `idx_sps_survey_status`: `CREATE INDEX idx_sps_survey_status ON public.survey_panel_stats USING btree (survey_id, status)`

### `tiny_url`

**Kind/grain:** table; Short URL mapping to a survey/folder/report module.

**Snapshot:** 24 rows; 96 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"unique_code" character varying(32) NOT NULL
"short_url" character varying(512) NOT NULL
"module_type" tiny_url_module_type_enum
"module_id" character varying
"long_url" character varying(2048) NOT NULL
"device_info" jsonb DEFAULT '[]'::jsonb
"expires_at" timestamp without time zone
"deleted_at" timestamp without time zone
"created_by" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_4bcfe746dc3ce856f454336107f`: `PRIMARY KEY (id)`
- UNIQUE `UQ_TINY_URL_UNIQUE_CODE`: `UNIQUE (unique_code)`
- FK `FK_72cbd34de406d7f47a72c8ce75a`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`

**Indexes**

- `PK_4bcfe746dc3ce856f454336107f`: `CREATE UNIQUE INDEX "PK_4bcfe746dc3ce856f454336107f" ON public.tiny_url USING btree (id)`
- `UQ_TINY_URL_UNIQUE_CODE`: `CREATE UNIQUE INDEX "UQ_TINY_URL_UNIQUE_CODE" ON public.tiny_url USING btree (unique_code)`
- `IDX_TINY_URL_UNIQUE_CODE`: `CREATE INDEX "IDX_TINY_URL_UNIQUE_CODE" ON public.tiny_url USING btree (unique_code)`

### `user`

**Kind/grain:** table; Registered platform/login user; not the general respondent population.

**Snapshot:** 215 rows; 144 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"email" character varying -- User's email
"password" character varying -- User's password
"firstName" character varying -- User's first name
"lastName" character varying -- User's last name
"username" character varying -- User's username
"country" character varying DEFAULT 'US'::character varying -- User's country
"city" character varying -- User's city
"language" character varying NOT NULL DEFAULT 'en'::character varying -- User's language
"dialCode" character varying -- User's mobile number country code
"mobileNumber" character varying -- User's mobile number
"gender" character varying DEFAULT 'male'::character varying -- User's gender
"verified" boolean NOT NULL DEFAULT false
"inactive" boolean DEFAULT false
"archiveNote" character varying DEFAULT ''::character varying -- Archive note
"completedQuestionaire" boolean NOT NULL DEFAULT false -- Check if user completed the Welcome Questionaire.
"completedProductTour" json -- Track user progress in the Product Tour
"agreeWithPolicies" boolean NOT NULL DEFAULT false
"agreeWithSubscriptions" boolean NOT NULL DEFAULT false
"passwordResetToken" character varying -- A unique token used to verify the user's identity when recovering a password.  Expires after 1 use, or after a set amount of time has elapsed.
"passwordResetTokenExpiresAt" character varying -- A JS timestamp (epoch ms) representing the moment when this user's `passwordResetToken` will expire (or 0 if the user currently has no such token).
"jobPosition" character varying -- User's jobPosition
"company" character varying -- User's company
"website" character varying -- Company website
"linkedIn" character varying -- User's linkedIn
"interest" character varying -- User's interest
"appPlan" character varying -- User's App Plan
"photo" character varying DEFAULT ''::character varying
"state" character varying -- User state
"code" character varying(120)
"postal_code" character varying(120)
"department" character varying -- User department
"stakeholder_type" character varying NOT NULL DEFAULT 'individual'::character varying
"created_by" uuid
"updated_by" uuid
"deactivated_by" uuid
"archived_by" uuid
"deleted_by" uuid
"deactivated_at" timestamp without time zone
"archived_at" timestamp without time zone
"deleted_at" timestamp without time zone
"has_global_access" boolean NOT NULL DEFAULT false
"personal_address" character varying
"company_address" character varying
"is_invited" boolean NOT NULL DEFAULT false
"last_logged_in" timestamp without time zone
"is_internal" boolean NOT NULL DEFAULT false
"appartment" character varying
"industry" character varying
"employee_count" character varying
"annual_revenue" character varying
"user_contact_type_id" uuid
"sandbox_key" character varying(1024) DEFAULT NULL::character varying
"whitelisted_domains" character varying
```

**Constraints and relationships**

- PK `PK_cace4a159ff9f2512dd42373760`: `PRIMARY KEY (id)`
- FK `FK_032a89031d8949615c0a8d77939`: `FOREIGN KEY (user_contact_type_id) REFERENCES user_contact_type(id) ON DELETE SET NULL`
- FK `FK_5296196de182cc661b110c20f7a`: `FOREIGN KEY (deactivated_by) REFERENCES "user"(id)`
- FK `FK_6bfae5ab9f39212d5b6ad0276b1`: `FOREIGN KEY (updated_by) REFERENCES "user"(id)`
- FK `FK_7dda804b73a73af1c4fcab9a5bc`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- FK `FK_8380406413c086702397176cdbe`: `FOREIGN KEY (archived_by) REFERENCES "user"(id)`
- FK `FK_d2f5e343630bd8b7e1e7534e82e`: `FOREIGN KEY (created_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `access_group` via `FK_d264a4aa1af3669fa48b951f73f`
  - `access_group_user` via `FK_e949597afc2c20c650f961b2ae9`
  - `account` via `FK_14f88efd4d31eafab6a62cc62cc`, `FK_651c60b8e28ceb49880f5be0b3b`, `FK_92f552371b42ca6b8cc50f7a3f7`, `FK_9f12e8ffb17cc4b4deeadb93401`, `FK_f6e3fba2c8b88432e56d4268f13`
  - `application` via `FK_019adac3496bb860afbd6c7ec95`, `FK_4e1da9fc8efdc8733cfa3668be6`, `FK_4f1b123d8e4cd1a380f807a5fd1`, `FK_6505365b085bd70593fbd4c88e3`, `FK_c8a899cc24e7c6aac5f7f4daeae`
  - `assigned_stakeholder` via `FK_6c9997232ff0867f8948b315389`, `FK_c5000948800ae9733f99e88a347`, `FK_f0dbd44431caedc781a884b860c`
  - `assigned_stakeholder_users` via `FK_316aa39eba1b9be5c669ebcc891`, `FK_5f0eccf261c268f673a28d082e4`, `FK_94bb544239c1b112e2b74eb584e`, `FK_c031a4acc39ae71b8c09bca8762`
  - `bank_account` via `FK_0e0f902f249f980e19adf79d98f`, `FK_984f14dc58850cf4198a50a58b1`, `FK_9dd66dbf034ade89c0dbbf2a8e8`, `FK_c8d57e8df596573a617476fdff2`
  - `chart_report_filter` via `FK_2def8ed7c177ab9f64a24901657`, `FK_4a69854b34cc727ae67869a6263`, `FK_6b6d873aac7ead4292a633342f7`
  - `chart_settings_template` via `FK_eefae7a71d5aa769ae68d225521`
  - `charts_reports` via `FK_806306c3c01620f97e553560c43`, `FK_a1f184f703a838e67272a3c6f3b`
  - `client` via `FK_46d6988875665c576a9c283d466`, `FK_b9012705b3b257eac64edd92a54`, `FK_dfe832d3ba6908918f5446b5c7e`, `FK_e1eb16471c8e431c63885b9c5d5`, `FK_f1bebb11f4584e3ea8ce25f7093`
  - `client_info` via `FK_3c4e527c8c37345efc91a11aa7b`, `FK_9c2cf45d2070dfb5ff6a3230b08`, `FK_c57956bd79b37f813440429b908`, `FK_d4e75fec3beaf73f1fdf74a1f1d`
  - `cost_center` via `FK_10ca08806566762ead94b140696`, `FK_43de2c7154803f634c70b1f0a0b`, `FK_84ae71506ca71aca2d4453efc89`, `FK_9c3792fbdc8eeaf6f11d456bce6`, `FK_c1c205453040e3e2f438c1d45ed`, `FK_db2d5e3b9345440265ae5273533`, `FK_f11f0fc853e5a4c9ab776013986`
  - `cost_center_counterparty_bank` via `FK_5624ae3958c3521e48d6a5db271`, `FK_e4aaa4bf098b5e0aec2f7bbaa2f`, `FK_f2dd73ee9da279e2c4bd9b65b6c`
  - `counterparty` via `FK_0d1b1d1d2a3028ffc97109c92d4`, `FK_10b3bd35f4d86646491db9a19c9`, `FK_291d01c45189f6933e07a3101d0`, `FK_816913cf48c2d9e1c7ebc9b6078`, `FK_c09c7c105edbda5378fe4652dce`
  - `customer` via `FK_16adddc7ee23a8be8198c3f5a0d`, `FK_5d1f609371a285123294fddcf3a`, `FK_81f42e2cdfa36f5bf134e4f9ae5`, `FK_84235f5c9358c7de7a8cdc5da48`, `FK_a0fa0161e4f246c12d88257a99b`, `FK_e146e7fa4df2cd2817bac7b2c53`
  - `enrollment` via `FK_fc17c7e94154a17e767b7674f12`
  - `feature_flag_users` via `FK_c8dcf0891354c33a13f2f209eac`
  - `folder` via `FK_10670d70f3e474c3de3ce53cfb7`, `FK_4d6ef3409b06099753ed80f08f9`, `FK_d008e5cb74c1e1f272038e4db49`
  - `image_doc` via `FK_732c76e4433ec7a5c1ee1927802`
  - `invoice_portal` via `FK_12b243212fef046049da4ebb866`, `FK_89d642fb0d780fbffaefe9f52c3`, `FK_bf64573dd03cb32867c945621f9`
  - `jobs` via `FK_a2282bfdb21805a8ed2e8c73a48`
  - `license` via `FK_011731184e18caf95bd64a22c8c`, `FK_0b5f567c5ea73dab9b53ca81476`, `FK_169fb04ae48a2a1c76f9ef59f38`, `FK_452a101bd9746d3fb5fe9a50a5b`, `FK_75717689fe0c9c881774af91a53`
  - `license_app_data` via `FK_2a7a3ba46ea75368905b3a974f6`, `FK_50c828b05a8db6fe9bbf5bb6628`, `FK_7ae2613d95368cc19d29571a021`
  - `license_app_data_metadata` via `FK_430cc63ef1500c66cb21968006d`, `FK_49a1fee82e0e556c2691741cf21`, `FK_c49bb7b57a4a0a7ec302428c6d5`
  - `license_clients` via `FK_7fa5a8c8cd296feacf56b8293f3`, `FK_84f7cbbfe7bbc2832030f18c44a`, `FK_dc57695e911d4fa7b96b7d68f2f`
  - `license_payment_milestone` via `FK_11f9907596c6161706932e479d3`, `FK_7b850551b4ac033779806db2bdc`, `FK_ba87d5299e96b551bf4d07097a4`
  - `module_contact` via `FK_75a9e3ea9b22260ebf974159c30`, `FK_93496c248d1d51687c5ac2271f6`, `FK_b2067091cccf721f5b056dafb1c`, `FK_b45b4f81ff525806d7f0dae6da3`
  - `nomenclature_category` via `FK_nomenclature_category_created_by`, `FK_nomenclature_category_updated_by`
  - `nomenclature_client` via `FK_nomenclature_client_created_by`, `FK_nomenclature_client_updated_by`
  - `nomenclature_type_of_test` via `FK_nomenclature_type_of_test_created_by`, `FK_nomenclature_type_of_test_updated_by`
  - `organization` via `FK_3540851b5121e812cd15da62560`, `FK_727fc946c971786a7c9fa6629aa`, `FK_b0c596f429ff2966d03516ef1ca`, `FK_d348286667373cbcec5463cda5b`, `FK_edd3d199e09845da9c76636170a`
  - `panel` via `FK_1131f721af1a23d44014ef5b535`, `FK_da77649fad2e5fe3444b631485e`, `FK_e73f6691aef99955b85ac6ec77e`, `FK_ee4651ea7f7ff951a17fddd4483`, `FK_panel_inactivated_by`
  - `panel_code` via `FK_9a676d3578eb44d875e69380e44`, `FK_d5a4e1f59ca127a4cfb02aa5033`, `FK_e9d6fb5ec27b1e4d5fc9ea9d785`, `FK_panel_code_inactivated_by`
  - `panel_code_format` via `FK_PANEL_CODE_FORMAT_CREATED_BY_USER`
  - `panelist` via `FK_panelist_archived_by`, `FK_panelist_created_by`, `FK_panelist_deleted_by`, `FK_panelist_inactivated_by`, `FK_panelist_updated_by`
  - `permission` via `FK_8a5117703b89f1cb83a44d895f3`, `FK_974c86657cc409ff59ddb20b48b`, `FK_9c4a4f3953767dfc5d1a3b63d10`
  - `permission_operation` via `FK_0169d1680c543d2c0ebe3789016`, `FK_3dfcca3bb02a7ef507580470d97`, `FK_f970e5f6954ea19613cce9cd9c1`
  - `personnels_csv_upload_history` via `FK_dbe16c98a6b54a1d6c0bd86291c`
  - `question_library` via `FK_03da259bffa5642b5bb6e378d5c`
  - `role` via `FK_04a09925beea59e864e921db4a1`, `FK_253ae6ba531cf922f4cb1580a95`, `FK_66c5a07f8a92982db8c909ad4c6`, `FK_7b5b6fdd043b90e1d461b4b681d`, `FK_858c871a036f61e56e2740c7cda`
  - `role_users_user` via `FK_a88fcb405b56bf2e2646e9d4797`
  - `shared_access` via `FK_47809ed2925bb364f7941b3cbf9`, `FK_d919a2da734ca453c0915845944`
  - `shared_access_group` via `FK_49c5d6eb2daed9f2cf6ef2d8565`, `FK_948d9ae39d430c3f13e62fcaa5f`
  - `shared_access_organization` via `FK_6333b4cbb6f4a4b2c57070c180a`, `FK_bb1a9016031407322d221ec8e82`
  - `shared_access_user` via `FK_56125612987f970b1999f37da6c`, `FK_dc9c67566acaed58f335fcb35f1`, `FK_f09097ab9216668ddb66b2b2511`
  - `survey` via `FK_286c488d686d7b45280e51d29c0`, `FK_445c4d061fabdcd15bdd7e6ce2f`, `FK_cdb7a72b955b60b44f84d9e97f7`
  - `survey_nomenclature` via `FK_survey_nomenclature_created_by`, `FK_survey_nomenclature_updated_by`
  - `survey_panel_code` via `FK_680ff0740b0cdb2b5b4f9e2cb7a`
  - `tiny_url` via `FK_72cbd34de406d7f47a72c8ce75a`
  - `user` via `FK_5296196de182cc661b110c20f7a`, `FK_6bfae5ab9f39212d5b6ad0276b1`, `FK_7dda804b73a73af1c4fcab9a5bc`, `FK_8380406413c086702397176cdbe`, `FK_d2f5e343630bd8b7e1e7534e82e`
  - `user_contact_type` via `FK_56de556de5bf351ce03db2bbd5a`
  - `user_organization_role_permission` via `FK_c1ea6c3a81d2c83900a3781a9d3`
  - `user_organizations_organization` via `FK_7ad3d8541fbdb5a3d137c50fb40`
  - `user_private_keys` via `FK_user_private_keys_user`
  - `user_roles_role` via `FK_5f9286e6c25594c6b88c108db77`
  - `user_session` via `FK_user_session_user_id`
  - `user_tags_users` via `FK_a348f7f842b4ee9f92b75b74c71`
  - `welcome_questionaire_answer` via `FK_b53a676c8109ef531a66da6d51b`

**Indexes**

- `PK_cace4a159ff9f2512dd42373760`: `CREATE UNIQUE INDEX "PK_cace4a159ff9f2512dd42373760" ON public."user" USING btree (id)`
- `idx_user_id`: `CREATE INDEX idx_user_id ON public."user" USING btree (id)`

**Non-internal triggers**

- `audit_user_delete_trigger`: `CREATE TRIGGER audit_user_delete_trigger AFTER DELETE ON "user" FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_user_insert_trigger`: `CREATE TRIGGER audit_user_insert_trigger AFTER INSERT ON "user" FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`
- `audit_user_update_trigger`: `CREATE TRIGGER audit_user_update_trigger AFTER UPDATE ON "user" FOR EACH ROW WHEN ((to_jsonb(old.*) - 'updatedAt'::text - 'updated_at'::text) IS DISTINCT FROM (to_jsonb(new.*) - 'updatedAt'::text - 'updated_at'::text)) EXECUTE FUNCTION audit_trigger_function()`

### `user_contact_type`

**Kind/grain:** table; Lookup for user contact classifications.

**Snapshot:** 6 rows; 40 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"deleted_by" uuid
"deleted_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_860182f03505b229f86fb10bdd8`: `PRIMARY KEY (id)`
- UNIQUE `UQ_35cfae3b5a02e540d039a150814`: `UNIQUE (name)`
- FK `FK_56de556de5bf351ce03db2bbd5a`: `FOREIGN KEY (deleted_by) REFERENCES "user"(id)`
- Incoming foreign keys:
  - `user` via `FK_032a89031d8949615c0a8d77939`

**Indexes**

- `PK_860182f03505b229f86fb10bdd8`: `CREATE UNIQUE INDEX "PK_860182f03505b229f86fb10bdd8" ON public.user_contact_type USING btree (id)`
- `UQ_35cfae3b5a02e540d039a150814`: `CREATE UNIQUE INDEX "UQ_35cfae3b5a02e540d039a150814" ON public.user_contact_type USING btree (name)`

### `user_organization_role_permission`

**Kind/grain:** table; Denormalized/direct user-organization-role-permission assignment.

**Snapshot:** 0 rows; 8192 bytes total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"operation_create" boolean NOT NULL DEFAULT false -- create permision
"operation_read" boolean NOT NULL DEFAULT false -- read permission
"operation_update" boolean NOT NULL DEFAULT false -- update permission
"operation_delete" boolean NOT NULL DEFAULT false -- delete permission
"userId" uuid -- User id
"organizationId" uuid -- Organization Id
"roleId" uuid NOT NULL -- role Id
"permissionId" uuid NOT NULL -- permission Id
```

**Constraints and relationships**

- PK `PK_58056faf70b9269f28915c20b69`: `PRIMARY KEY (id)`
- FK `FK_0381d94d24c4a28a393a1890daf`: `FOREIGN KEY ("roleId") REFERENCES role(id)`
- FK `FK_29e5a49d08c3e10571ce87a5943`: `FOREIGN KEY ("permissionId") REFERENCES permission(id)`
- FK `FK_c1ea6c3a81d2c83900a3781a9d3`: `FOREIGN KEY ("userId") REFERENCES "user"(id)`
- FK `FK_c5aaa1dfe1a78191181623be2c5`: `FOREIGN KEY ("organizationId") REFERENCES organization(id)`

**Indexes**

- `PK_58056faf70b9269f28915c20b69`: `CREATE UNIQUE INDEX "PK_58056faf70b9269f28915c20b69" ON public.user_organization_role_permission USING btree (id)`

**Non-internal triggers**

- `audit_user_organization_role_permission_trigger`: `CREATE TRIGGER audit_user_organization_role_permission_trigger AFTER INSERT OR DELETE OR UPDATE ON user_organization_role_permission FOR EACH ROW EXECUTE FUNCTION audit_trigger_function()`

### `user_organizations_organization`

**Kind/grain:** table; User-to-organization membership join table.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"userId" uuid NOT NULL
"organizationId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_d89fbba617c90c71e2fc0bee26f`: `PRIMARY KEY ("userId", "organizationId")`
- FK `FK_7ad3d8541fbdb5a3d137c50fb40`: `FOREIGN KEY ("userId") REFERENCES "user"(id) ON UPDATE CASCADE ON DELETE CASCADE`
- FK `FK_8d7c566d5a234be0a6461013269`: `FOREIGN KEY ("organizationId") REFERENCES organization(id)`

**Indexes**

- `PK_d89fbba617c90c71e2fc0bee26f`: `CREATE UNIQUE INDEX "PK_d89fbba617c90c71e2fc0bee26f" ON public.user_organizations_organization USING btree ("userId", "organizationId")`
- `IDX_7ad3d8541fbdb5a3d137c50fb4`: `CREATE INDEX "IDX_7ad3d8541fbdb5a3d137c50fb4" ON public.user_organizations_organization USING btree ("userId")`
- `IDX_8d7c566d5a234be0a646101326`: `CREATE INDEX "IDX_8d7c566d5a234be0a646101326" ON public.user_organizations_organization USING btree ("organizationId")`

### `user_private_keys`

**Kind/grain:** table; Private/encrypted key material associated with users; never use for survey analysis.

**Snapshot:** 129 rows; 112 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" uuid NOT NULL
"encrypted_key" character varying(1024) NOT NULL
"decrypted_key" character varying(256) NOT NULL
"invalidatedAt" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_user_private_keys_id`: `PRIMARY KEY (id)`
- FK `FK_user_private_keys_user`: `FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE`
- Incoming foreign keys:
  - `external_api_stats` via `FK_external_api_stats_user_private_key`

**Indexes**

- `PK_user_private_keys_id`: `CREATE UNIQUE INDEX "PK_user_private_keys_id" ON public.user_private_keys USING btree (id)`
- `IDX_user_private_keys_decrypted_key`: `CREATE INDEX "IDX_user_private_keys_decrypted_key" ON public.user_private_keys USING btree (decrypted_key)`
- `IDX_user_private_keys_user_id`: `CREATE INDEX "IDX_user_private_keys_user_id" ON public.user_private_keys USING btree (user_id)`

### `user_roles_role`

**Kind/grain:** table; User-to-role join table.

**Snapshot:** 0 rows; 24 kB total relation size; RLS not enabled.

**Columns**

```text
"userId" uuid NOT NULL
"roleId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_b47cd6c84ee205ac5a713718292`: `PRIMARY KEY ("userId", "roleId")`
- FK `FK_4be2f7adf862634f5f803d246b8`: `FOREIGN KEY ("roleId") REFERENCES role(id)`
- FK `FK_5f9286e6c25594c6b88c108db77`: `FOREIGN KEY ("userId") REFERENCES "user"(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_b47cd6c84ee205ac5a713718292`: `CREATE UNIQUE INDEX "PK_b47cd6c84ee205ac5a713718292" ON public.user_roles_role USING btree ("userId", "roleId")`
- `IDX_4be2f7adf862634f5f803d246b`: `CREATE INDEX "IDX_4be2f7adf862634f5f803d246b" ON public.user_roles_role USING btree ("roleId")`
- `IDX_5f9286e6c25594c6b88c108db7`: `CREATE INDEX "IDX_5f9286e6c25594c6b88c108db7" ON public.user_roles_role USING btree ("userId")`

### `user_session`

**Kind/grain:** table; Authentication/session records; sensitive operational data.

**Snapshot:** 519 rows; 152 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"user_id" uuid NOT NULL
"hashedRt" text NOT NULL
"device_info" text
"ip_address" text
```

**Constraints and relationships**

- PK `PK_4b62262d2f4024eaa57be858dc7`: `PRIMARY KEY (id)`
- FK `FK_user_session_user_id`: `FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE`

**Indexes**

- `PK_4b62262d2f4024eaa57be858dc7`: `CREATE UNIQUE INDEX "PK_4b62262d2f4024eaa57be858dc7" ON public.user_session USING btree (id)`
- `IDX_user_session_user_id`: `CREATE INDEX "IDX_user_session_user_id" ON public.user_session USING btree (user_id)`

### `user_shared_access`

**Kind/grain:** materialized view; Materialized effective user access across direct, group, and organization grants.

**Snapshot:** 305 rows; 96 kB total relation size; RLS not enabled.

**Columns**

```text
"user_id" uuid
"module_id" character varying
"module_type" shared_access_module_type_enum
"access_level" text
```

**Constraints and relationships**

- No database constraints declared.

**Indexes**

- `user_shared_access_unique_idx`: `CREATE UNIQUE INDEX user_shared_access_unique_idx ON public.user_shared_access USING btree (user_id, module_id, module_type)`

### `user_tag`

**Kind/grain:** table; User tag definition.

**Snapshot:** 15 rows; 40 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"name" character varying(256) NOT NULL
"last_used_at" timestamp without time zone
```

**Constraints and relationships**

- PK `PK_ca37acfc991123d4cba963e75bf`: `PRIMARY KEY (id)`
- UNIQUE `UQ_a8ddabee22b66dfc9c93b17333e`: `UNIQUE (name)`
- Incoming foreign keys:
  - `user_tags_users` via `FK_a9c5b9b87ba3b556f6fd221d9db`

**Indexes**

- `PK_ca37acfc991123d4cba963e75bf`: `CREATE UNIQUE INDEX "PK_ca37acfc991123d4cba963e75bf" ON public.user_tag USING btree (id)`
- `UQ_a8ddabee22b66dfc9c93b17333e`: `CREATE UNIQUE INDEX "UQ_a8ddabee22b66dfc9c93b17333e" ON public.user_tag USING btree (name)`

### `user_tags_users`

**Kind/grain:** table; Many-to-many join between tags and users.

**Snapshot:** 12 rows; 56 kB total relation size; RLS not enabled.

**Columns**

```text
"user_tag_id" uuid NOT NULL
"user_id" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_d4bf62ea536d57db62dd47f476e`: `PRIMARY KEY (user_tag_id, user_id)`
- FK `FK_a348f7f842b4ee9f92b75b74c71`: `FOREIGN KEY (user_id) REFERENCES "user"(id)`
- FK `FK_a9c5b9b87ba3b556f6fd221d9db`: `FOREIGN KEY (user_tag_id) REFERENCES user_tag(id) ON UPDATE CASCADE ON DELETE CASCADE`

**Indexes**

- `PK_d4bf62ea536d57db62dd47f476e`: `CREATE UNIQUE INDEX "PK_d4bf62ea536d57db62dd47f476e" ON public.user_tags_users USING btree (user_tag_id, user_id)`
- `IDX_a348f7f842b4ee9f92b75b74c7`: `CREATE INDEX "IDX_a348f7f842b4ee9f92b75b74c7" ON public.user_tags_users USING btree (user_id)`
- `IDX_a9c5b9b87ba3b556f6fd221d9d`: `CREATE INDEX "IDX_a9c5b9b87ba3b556f6fd221d9d" ON public.user_tags_users USING btree (user_tag_id)`

### `welcome_questionaire_answer`

**Kind/grain:** table; Answers to the platform welcome questionnaire, not survey respondent answers.

**Snapshot:** 166 rows; 64 kB total relation size; RLS not enabled.

**Columns**

```text
"id" uuid NOT NULL DEFAULT uuid_generate_v4()
"createdAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"updatedAt" timestamp without time zone NOT NULL DEFAULT ('now'::text)::timestamp(6) with time zone
"questionId" character varying NOT NULL
"answerId" character varying NOT NULL
"answerValue" character varying
"userId" uuid NOT NULL
```

**Constraints and relationships**

- PK `PK_8b15af2f3d9a02d8e75f3ea65c4`: `PRIMARY KEY (id)`
- FK `FK_b53a676c8109ef531a66da6d51b`: `FOREIGN KEY ("userId") REFERENCES "user"(id)`

**Indexes**

- `PK_8b15af2f3d9a02d8e75f3ea65c4`: `CREATE UNIQUE INDEX "PK_8b15af2f3d9a02d8e75f3ea65c4" ON public.welcome_questionaire_answer USING btree (id)`

## Application functions

Extension-owned functions are omitted here. `SECURITY DEFINER` is shown explicitly; all listed functions are invoker-security unless stated otherwise.

- `archive_audit_log_batch(p_cutoff timestamp without time zone, p_batch_size integer) -> integer`; `plpgsql`, `VOLATILE`, `SECURITY INVOKER`.
- `audit_log_retention_diagnostics() -> TABLE(audit_month date, table_name character varying, operation character varying, row_count bigint)`; `plpgsql`, `STABLE`, `SECURITY INVOKER`.
- `audit_trigger_function() -> trigger`; `plpgsql`, `VOLATILE`, `SECURITY INVOKER`.
- `erfinv(p double precision) -> double precision`; `plpgsql`, `IMMUTABLE`, `SECURITY INVOKER`.
- `get_audit_changes(p_table_name character varying, p_record_id uuid, p_start_time timestamp without time zone, p_end_time timestamp without time zone) -> TABLE(id uuid, operation character varying, old_data jsonb, new_data jsonb, changed_by uuid, changed_at timestamp without time zone, impersonated_by uuid, ip_address character varying, user_agent text)`; `plpgsql`, `VOLATILE`, `SECURITY INVOKER`.
- `get_audit_history(p_table_name character varying, p_record_id uuid) -> TABLE(id uuid, table_name character varying, record_id uuid, operation character varying, old_data jsonb, new_data jsonb, changed_by uuid, changed_at timestamp without time zone, impersonated_by uuid, ip_address character varying, user_agent text)`; `plpgsql`, `STABLE`, `SECURITY INVOKER`.
- `get_sensory_test_answers(p_survey_id uuid, p_question_type text) -> TABLE(answer_id uuid, answer_skipped boolean, question_id uuid, question text, question_order integer, question_type text, survey_id uuid, survey_name text, combination text, set_data jsonb, tray_id text, answer_data jsonb, answer_data_array jsonb, enrollment_id uuid, unique_identifier bigint, survey_creator text, survey_owner text, published_at timestamp without time zone, status text, answered_status text, questions_count integer, respondent_count integer, screen_order integer, result_value integer)`; `plpgsql`, `STABLE`, `SECURITY INVOKER`.
- `get_stats_discrimination_tests(p_question_id uuid, p_beta numeric, p_similarity_pd numeric) -> TABLE(survey_id uuid, question_id uuid, respondent_count integer, question_type text, correct_count integer, incorrect_count integer, prop_correct numeric, chance_probability numeric, z_stat numeric, prop_discriminators numeric, standard_deviation_pc numeric, z_value numeric, d_prime numeric, p_value_normal numeric, p_value numeric, standard_deviation_pd numeric, standard_deviation_d_prime numeric, similarity_pd numeric, similarity_beta numeric, similarity_threshold numeric, similarity_critical_value integer, similarity_p_value numeric, similarity_reject boolean)`; `sql`, `STABLE`, `SECURITY INVOKER`.
- `get_survey_answers(p_survey_id uuid) -> TABLE(answer_id uuid, answered_at timestamp without time zone, time_to_answer integer, is_skipped boolean, free_text_value text, enrollment_id uuid, unique_identifier integer, question_id uuid, question_order integer, screen_order integer, question_client_id text, quest_prompt text, settings jsonb, question_type text, child_q_id uuid, product_id uuid, product_name text, product_internal_name text, product_order integer, section_name text, section_order integer, survey_id uuid, survey_name text, username text, user_email text, user_id uuid, survey_creator text, survey_owner text, published_at timestamp without time zone, status text, group_name text, group_order numeric, questions_count integer, respondent_count integer, question_prompt text, option_id uuid, option_type text, option_order integer, option_settings jsonb, answered_option_row_id uuid, option_answer_data jsonb, is_selected integer, option_label text, option_analytical_value numeric, result_value integer, matrix_row_order integer)`; `plpgsql`, `STABLE`, `SECURITY INVOKER`.

## Materialized view definitions

### `user_shared_access`

```sql
 SELECT t.user_id,
    t.module_id,
    t.module_type,
        CASE max(
            CASE t.access_level
                WHEN 'editor'::text THEN 2
                WHEN 'viewer'::text THEN 1
                ELSE 0
            END)
            WHEN 2 THEN 'editor'::text
            WHEN 1 THEN 'viewer'::text
            ELSE NULL::text
        END AS access_level
   FROM ( SELECT sau.user_id,
            sa.module_id,
            sa.module_type,
            (sau.access_level)::text AS access_level
           FROM (shared_access sa
             JOIN shared_access_user sau ON ((sau.shared_access_id = sa.id)))
          WHERE ((sa.deleted_at IS NULL) AND (sau.deleted_at IS NULL))
        UNION ALL
         SELECT agu.user_id,
            sa.module_id,
            sa.module_type,
            (sag.access_level)::text AS access_level
           FROM ((shared_access sa
             JOIN shared_access_group sag ON ((sag.shared_access_id = sa.id)))
             JOIN access_group_user agu ON ((agu.group_id = sag.group_id)))
          WHERE ((sa.deleted_at IS NULL) AND (sag.deleted_at IS NULL))
        UNION ALL
         SELECT asu.user_id,
            sa.module_id,
            sa.module_type,
            (sao.access_level)::text AS access_level
           FROM (((shared_access sa
             JOIN shared_access_organization sao ON ((sao.shared_access_id = sa.id)))
             JOIN assigned_stakeholder ass ON (((ass.module_id = sao.organization_id) AND (ass.module_type = 'organization'::assigned_stakeholder_module_type_enum) AND (ass.type = 'operator'::assigned_stakeholder_type_enum) AND (ass.deleted_at IS NULL))))
             JOIN assigned_stakeholder_users asu ON ((asu.assigned_stakeholder_id = ass.id)))
          WHERE ((sa.deleted_at IS NULL) AND (sao.deleted_at IS NULL))) t
  GROUP BY t.user_id, t.module_id, t.module_type;
```

## Extensions and sequences

Extensions: `pgcrypto 1.3`, `plpgsql 1.0`, `uuid-ossp 1.1`.

Public sequences: `migrations_id_seq`.

## Drift and validation

Before generating SQL against a different deployment or after migrations, validate uncertain names/types live:

```sql
SELECT table_name, ordinal_position, column_name, data_type, udt_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name IN ('answer', 'question')
ORDER BY table_name, ordinal_position;
```

Trust the live catalog over snapshot counts or assumptions. Do not treat current emptiness, current row counts, or current ID values as stable application behavior.
