# PROMPT_STRUCTS.md — the model-facing text, decomposed

> **Historical snapshot, not current workflow guidance.** Use
> `docs/generated/agent-prompt-map.{md,json}` for current instructions and provenance, and
> `PROTOCOL.md` for the working order. The locations, dependency encoding and findings below
> describe an earlier state; they are preserved for history and are not maintained in parallel.

Every string that reaches the model lives in exactly two files (PROTOCOL.md §2). This is their
catalog: one row per addressable block, with its line range, what it governs, a trimmed excerpt
and what it depends on.

Each block carries a **prime**. A block's `deps` product is the product of its dependencies'
primes, so the edge set factors back out of one integer and a dependency can be checked by
divisibility: `deps % 13 == 0` means "depends on AI-06". A leaf has product `1`.

Sizes are the raw constant length in characters. Line numbers are from the working tree on
**24 Aug 2026**; `agent_instructions.py` is 2,076 lines and `tool_prompts.py` is 591. Both files
are byte-identical to their `deployment/` copies.

> ⚠️ `PROTOCOL.md` §2's rule-block line map is stale — every entry is 45-90 lines low
> (`NESTED_RESULT_RULES` is at 391, not 345; `PERSONA_RULES` at 1414, not 1326) and it lists a
> `BENCHMARK_SCOPE` constant that does not exist. The numbers below were read from the AST.

---

## 1. Assembly order

`build_system_prompt()` (AI-16, `:1742`) composes the system prompt in this fixed order. The
schema goes last so everything before it stays a stable prompt-cache prefix:

```
You are the {AI-01 role}.
{AI-02 goal}                      ← .format(**SCOPE_REF)
--- {AI-03 backstory}             ← .format(**SCOPE_REF)
--- {AI-10 PG_DIALECT_RULES}
--- {AI-15 PROGRESS_MEMORY_RULES}
--- When you give your final answer: {AI-05 EXPECTED_OUTPUT}
        + {AI-06 NESTED_RESULT_RULES}
        + {AI-11 REPORTING_RULES}
        + {AI-12 PERSONA_RULES}
        + {AI-14 WORD_CLOUD_RULES}
        + {AI-13 PLSR_RULES}
--- {AI-08 SCHEMA_OVERVIEW}       ← last: stable cache suffix
```

Three blocks never enter the system prompt — they are injected at runtime into the **user** or
tool channel: AI-17 `INVENTORY_PREAMBLE` (turn 0, ahead of the inventory payload), AI-18/AI-19
(appended to `nl2sql_tool` results), AI-20 `STEP_BUDGET_NOTICE` (a `HumanMessage` at the step
cap). AI-04 `TASK_DESCRIPTION` is the user-turn template.

---

## 2. Node catalog

| ID | prime | symbol | lines | chars | deps | product |
|---|---|---|---|---|---|---|
| **AI-01** | 2 | `AGENT_ROLE` | `18` | 23 | — | `1` |
| **AI-02** | 3 | `AGENT_GOAL` | `21-22` | 1,473 | AI-09 (23) | `23` |
| **AI-03** | 5 | `AGENT_BACKSTORY` | `24-232` | 17,707 | AI-09 (23) · AI-17 (59) · TP-07 (103) | `139771` |
| **AI-04** | 7 | `TASK_DESCRIPTION` | `234` | 27 | — | `1` |
| **AI-05** | 11 | `EXPECTED_OUTPUT` | `237-343` | 9,573 | AI-06 (13) | `13` |
| **AI-06** | 13 | `NESTED_RESULT_RULES` | `345-482` | 9,720 | TP-13 (137) | `137` |
| **AI-07** | 17 | `AGENTS_YAML / TASKS_YAML / AGENT_CONFIG / TASK_CONFIG` | `484, 492, 1168, 1169` | dict | AI-01 (2) · AI-02 (3) · AI-03 (5) · AI-04 (7) · AI-05 (11) | `2310` |
| **AI-08** | 19 | `SCHEMA_OVERVIEW` | `500-1166` | 37,746 | — | `1` |
| **AI-09** | 23 | `SCOPE_REF / scoped_query / scoped_query_survey_only` | `1176, 1183, 1198` | fn | — | `1` |
| **AI-10** | 29 | `PG_DIALECT_RULES` | `1220-1255` | 2,636 | AI-08 (19) · AI-09 (23) | `437` |
| **AI-11** | 31 | `REPORTING_RULES` | `1280-1365` | 6,962 | AI-06 (13) | `13` |
| **AI-12** | 37 | `PERSONA_RULES` | `1368-1577` | 17,946 | AI-06 (13) · AI-11 (31) · AI-17 (59) | `23777` |
| **AI-13** | 41 | `PLSR_RULES` | `1580-1624` | 2,967 | TP-09 (109) | `109` |
| **AI-14** | 43 | `WORD_CLOUD_RULES` | `1627-1670` | 3,523 | AI-17 (59) · TP-04 (89) · TP-05 (97) | `509347` |
| **AI-15** | 47 | `PROGRESS_MEMORY_RULES` | `1673-1693` | 1,496 | — | `1` |
| **AI-16** | 53 | `build_system_prompt / SYSTEM_PROMPT_TEXT` | `1696, 2053` | fn | AI-07 (17) · AI-06 (13) · AI-11 (31) · AI-12 (37) · AI-14 (43) · AI-13 (41) · AI-10 (29) · AI-15 (47) · AI-08 (19) | `11573306655157` |
| **AI-17** | 59 | `INVENTORY_PREAMBLE` | `1734-2013` | 20,476 | AI-06 (13) · AI-11 (31) · TP-01 (73) | `29419` |
| **AI-18** | 61 | `REPEATED_SQL_WARNING` | `2023-2027` | 271 | TP-06 (101) | `101` |
| **AI-19** | 67 | `zero_row_streak_warning` | `2032` | fn | TP-06 (101) · TP-12 (131) | `13231` |
| **AI-20** | 71 | `STEP_BUDGET_NOTICE` | `2045-2049` | 160 | — | `1` |
| **TP-01** | 73 | `GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION` | `16-42` | 1,950 | AI-17 (59) | `59` |
| **TP-02** | 79 | `ANALYSIS_PACKET_RESULT_HEAD / analysis_packet_preamble` | `45, 48` | fn | AI-17 (59) | `59` |
| **TP-03** | 83 | `PACKET_SCOPE_UNAVAILABLE / PACKET_SCOPE_CHECK_FAILED / packet_* helpers` | `57-114` | ~600 | AI-09 (23) | `23` |
| **TP-04** | 89 | `GENERATE_WORD_CLOUD_DESCRIPTION` | `115-131` | 1,220 | AI-14 (43) | `43` |
| **TP-05** | 97 | `WORD_CLOUD_SCOPE_UNAVAILABLE / UNTRUSTED_WORD_CLOUD_BLOCK / word_cloud_* helpers` | `134-163` | ~450 | AI-14 (43) | `43` |
| **TP-06** | 101 | `NL2SQL_TOOL_DESCRIPTION` | `165-282` | 9,416 | AI-08 (19) · AI-10 (29) · TP-10 (113) · TP-11 (127) · TP-12 (131) · TP-13 (137) · TP-14 (139) · TP-15 (149) | `2939183458346117` |
| **TP-07** | 103 | `RUN_SURVEY_STATS_DESCRIPTION` | `287-299` | 829 | AI-03 (5) · AI-17 (59) | `295` |
| **TP-09** | 109 | `ANALYZE_PLSR_DESCRIPTION / PLSR_RESULT_HEAD` | `302-326, 329` | 1,902 | AI-13 (41) · AI-17 (59) | `2419` |
| **TP-10** | 113 | `SEMANTIC_FILTER_REASON` | `348-363` | 1,037 | — | `1` |
| **TP-11** | 127 | `SQL_ERROR_HINTS / error_hint / sql_failure / BIG_SQL_CHARS` | `371-426` | tuple+fn | — | `1` |
| **TP-12** | 131 | `ZERO_ROW_HEAD / ZERO_ROW_MSG` | `429, 436-447` | 880 | — | `1` |
| **TP-13** | 137 | `wide_result_note` | `456` | fn | AI-06 (13) | `13` |
| **TP-14** | 139 | `scope_repair_note / SCOPE_WARNING` | `472, 480-486` | fn+415 | AI-09 (23) | `23` |
| **TP-15** | 149 | `REJECT_BATCH / REJECT_NOT_SELECT / multi_statement_error / NON_SELECT_RESULT` | `491, 492, 497, 501` | ~270 | — | `1` |
| **TP-16** | 151 | `stats_* refusals / STATS_TIMEOUT` | `512-530` | ~400 | AI-17 (59) | `59` |
| **TP-17** | 157 | `unknown_tool / tool_error` | `545-549` | fn | — | `1` |

### What each one is about

**AI-01** · `AGENT_ROLE` · `:18` — The one-line identity: *Current Survey Analyst*. Interpolated as `You are the {role}.`

**AI-02** · `AGENT_GOAL` · `:21-22` — Tenancy boundary. Default scope is `survey_id`; when to leave it; the single benchmark exception that lets one foreign survey be read.

**AI-03** · `AGENT_BACKSTORY` · `:24-232` — Five behavioural sections — see the sub-map. The largest single block after the schema.

**AI-04** · `TASK_DESCRIPTION` · `:234` — `The user asked: "{prompt}"` — the user-turn template, not part of the system prompt.

**AI-05** · `EXPECTED_OUTPUT` · `:237-343` — Answer shape: format, Markdown-only, chart-by-default, chart recovery. Seven sub-rules.

**AI-06** · `NESTED_RESULT_RULES` · `:345-482` — When a wide result becomes a `gpi-nested-table`, and the recursive JSON grammar for it. No fixed row/column threshold — judged per node.

**AI-07** · `AGENTS_YAML` · `:484` — Config indirection: the five text blocks above are read back out of two dicts. Kept so the wording is addressable by key.

**AI-08** · `SCHEMA_OVERVIEW` · `:500-1166` — The DB map. Ten numbered sections; appended LAST so the instruction prefix stays a stable prompt-cache prefix.

**AI-09** · `SCOPE_REF` · `:1176` — Per-run ids go in the USER message, never the system prompt. `SCOPE_REF` supplies the `{client_id}` placeholders the goal and backstory format against.

**AI-10** · `PG_DIALECT_RULES` · `:1220-1255` — Postgres constructs rejected outright, plus scope-id placeholders, aggregate-N, compact formatting and query-count discipline.

**AI-11** · `REPORTING_RULES` · `:1280-1365` — Requested-construct gate · a base is not just its size · ONE CELL HOLDS ONE FIGURE.

**AI-12** · `PERSONA_RULES` · `:1368-1577` — Persona/roster contract: roster vs single, evidence from counts, respondent-grain overlap, the required table shapes.

**AI-13** · `PLSR_RULES` · `:1580-1624` — Exact rendering of the `analyze_plsr` payload, plus the attribute-selection disclosure and the ambiguity-before-the-call rule.

**AI-14** · `WORD_CLOUD_RULES` · `:1627-1670` — When a word-cloud block may be emitted, and that the runtime owns the artifact.

**AI-15** · `PROGRESS_MEMORY_RULES` · `:1673-1693` — The `<progress_gathered>` delta block: an append-only index to tool results, never a substitute for them.

**AI-16** · `build_system_prompt` · `:1696` — The assembler. Pure function of config, so byte-identical across surveys.

**AI-17** · `INVENTORY_PREAMBLE` · `:1734-2013` — Instructions in front of the turn-0 inventory payload: how to read the columnar sections, then the thin-request policy, plain language, suggestion chips and the significance rule.

**AI-18** · `REPEATED_SQL_WARNING` · `:2023-2027` — Appended when identical canonical SQL returns an identical result — stop re-running it.

**AI-19** · `zero_row_streak_warning` · `:2032` — Escalating notice after consecutive 0-row queries.

**AI-20** · `STEP_BUDGET_NOTICE` · `:2045-2049` — Injected as a HumanMessage when `MAX_LLM_STEPS` is hit: answer from what you have.

**TP-01** · `GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION` · `:16-42` — On-demand packet for a *resolved other* survey. Same JSON contract as the startup inventory.

**TP-02** · `ANALYSIS_PACKET_RESULT_HEAD` · `:45` — Result marker + the do-not-re-query preamble stamped on the packet.

**TP-03** · `PACKET_SCOPE_UNAVAILABLE` · `:57-114` — Packet refusals: bad id, unknown survey, out of scope, unavailable-reason map.

**TP-04** · `GENERATE_WORD_CLOUD_DESCRIPTION` · `:115-131` — What the word-cloud tool is for; `group_by_product` only on an explicit product-comparison ask.

**TP-05** · `WORD_CLOUD_SCOPE_UNAVAILABLE` · `:134-163` — Word-cloud runtime feedback, including the notice shown when a model-authored cloud is withheld.

**TP-06** · `NL2SQL_TOOL_DESCRIPTION` · `:165-282` — The SQL tool: one SELECT, batch calls in one turn, dialect pointer, zero-row diagnosis, return shape.

**TP-07** · `RUN_SURVEY_STATS_DESCRIPTION` · `:287-299` — Deliberately short. Names the test list and the PCA parent-question requirement.

**TP-09** · `ANALYZE_PLSR_DESCRIPTION` · `:302-326` — PLSR only on an explicit ask; product-mean grain stated up front.

**TP-10** · `SEMANTIC_FILTER_REASON` · `:348-363` — Why a `LIKE` on `question.prompt` / `question_option.label` is refused, and what to do instead.

**TP-11** · `SQL_ERROR_HINTS` · `:371-426` — Error-to-hint table turning a Postgres failure into a repair instruction.

**TP-12** · `ZERO_ROW_HEAD` · `:429` — The 0-row diagnosis script: an empty JOIN is as likely as a bad filter.

**TP-13** · `wide_result_note` · `:456` — The `[shape candidate: R rows x M measures]` note — the trigger that hands a wide SQL result to the nesting rules.

**TP-14** · `scope_repair_note` · `:472` — Told after a near-miss scope id was silently repaired; the unscoped-query warning.

**TP-15** · `REJECT_BATCH` · `:491` — The two structural SQL rejections, phrased as the instruction to follow.

**TP-16** · `stats_* refusals` · `:512-530` — Stats-tool feedback: bad question id, missing/bad reference id, timeout, HTTP error.

**TP-17** · `unknown_tool` · `:545-549` — Generic dispatch failures.

---

## 3. Trimmed text

First ~180 characters of each block, whitespace collapsed. The opening line is where the rule
announces its own scope, which is what makes these greppable.

**AI-01** `18`  
> Current Survey Analyst

**AI-02** `21-22`  
> Answer what the user asks about client_id {client_id}. Treat survey_id {survey_id} as the default scope: an ordinary question stays in that survey and does not search neighboring s…

**AI-03** `24-232`  
> GENERAL UNCLEAR-INTENT POLICY -- ASSUME, ANSWER, OFFER REFINEMENT. Apply this on every turn, whether or not a pre-fetched inventory is available. A missing detail is material only …

**AI-04** `234`  
> The user asked: "{prompt}

**AI-05** `237-343`  
> Give answer based on the tool call outputs. Report the sample size (N) alongside any statistic. If a correlation, comparison, or test is not statistically significant (or N is smal…

**AI-06** `345-482`  
> ADAPTIVE HIERARCHICAL JSON RESULTS -- apply this to factual tabular material from EVERY tool: SQL rows in wide or long form, analysis packets and statistics results. The word-cloud…

**AI-07** `530`  
> AGENTS_YAML = {'role':…, 'goal':…, 'backstory':…}; AGENT_CONFIG = AGENTS_YAML['survey_analyst']

**AI-08** `500-1166`  
> # FlavorAI DB Map ## 1. Purpose and scope This file remains in context on every turn. It is the analytical map: which tables answer which request, at what grain, with which denomin…

**AI-09** `1222`  
> SCOPE_REF = {'client_id':'[the client_id in the SCOPE block]', …} — placeholder text, not real ids

**AI-10** `1220-1255`  
> PostgreSQL rejects the following outright. The fix is given; use it. - `COUNT(DISTINCT x) OVER (...)` -- NOT SUPPORTED. Note the schema examples below use `COUNT(DISTINCT enrollmen…

**AI-11** `1280-1365`  
> REQUESTED-CONSTRUCT GATE -- apply this before selecting or formatting any measure. Match the construct the user named, not merely its subject or the fact that it is numeric and pro…

**AI-12** `1368-1577`  
> PERSONA REQUESTS -- "persona", "consumer persona", "respondent profile" and similar requests describe the people represented in the current survey; they are not permission to write…

**AI-13** `1580-1624`  
> PLSR RESULTS -- and only these. When analyze_plsr returns, render its payload exactly as below and add nothing the tool did not compute. Give one block per KPI analysed. Head the a…

**AI-14** `1627-1670`  
> WORD CLOUD REQUESTS -- and only these. Emit a word cloud block when the user asks for a "word cloud", "wordcloud", "tag cloud", "most common/frequent words", or "word frequencies".…

**AI-15** `1673-1693`  
> INTERNAL PROGRESS MEMORY -- tool results are the authoritative evidence. The `<progress_gathered>` block is only a compact index to that evidence; never treat a model-written progr…

**AI-16** `1742`  
> return f'You are the {role}.\n\n{goal}\n\n---\n\n{backstory}…{schema_block}'

**AI-17** `1734-2013`  
> PRE-FETCHED INVENTORY OF THIS SURVEY (plus resolved benchmark configuration; do not re-query it). Both measure sections are COLUMNAR: a `columns` header naming each position, then …

**AI-18** `2023-2027`  
> [query-repeat warning: the same canonical SQL produced the same result as in the previous SQL batch (or another call in this batch). Do not run another formatting or filter variant…

**AI-19** `2032`  
> [zero-row streak: N consecutive queries returned no rows …]

**AI-20** `2045-2049`  
> Step budget reached - no more tool calls are available. Answer now using only the data already retrieved, and state plainly which parts you could not determine.

**TP-01** `16-42`  
> Return the complete analysis packet for one resolved survey. Use this after another survey's id has been resolved, instead of spending one SQL turn listing candidate questions and …

**TP-02** `45`  
> ANALYSIS PACKET FOR SURVEY

**TP-03** `57-114`  
> Analysis packet unavailable: survey ownership could not be verified. Do not retry with a different id; use only a survey resolved inside the current scope.

**TP-04** `115-131`  
> Build the word-cloud artifact for one resolved open-ended question. Use this tool for every word-cloud, tag-cloud, most-common-words or word-frequency request. Do not use nl2sql_to…

**TP-05** `134-163`  
> A word-cloud block was withheld because it was not produced by the deterministic word-cloud tool.

**TP-06** `165-282`  
> Run one read-only SQL query against the survey PostgreSQL database. Pass a single complete SELECT statement (CTEs are fine; no semicolon-separated batches). The transaction is read…

**TP-07** `287-299`  
> Run statistical tests (anova, tukey, pearson, spearman, chi-square, pca) on a survey question. question_id: a UUID from the pre-fetched inventory or an nl2sql_tool result, never fr…

**TP-09** `337-362`  
> Run partial least squares regression from survey attributes onto one KPI. Use this ONLY when the user explicitly asks for PLSR/PLS regression, VIP scores, or partial least squares …

**TP-10** `348-363`  
> do not pattern-match question.prompt or question_option.label against the concept you are looking for (no LIKE/ILIKE/SIMILAR TO/~ on those columns) -- that decides meaning inside S…

**TP-11** `371-426`  
> ('duplicate key','…'), ('syntax error at or near','…') — matched against the driver message

**TP-12** `465`  
> Query succeeded but matched 0 rows

**TP-13** `456`  
> [shape candidate: {entities} returned rows x {measures} numeric measure columns. …]

**TP-14** `508`  
> [scope warning: this query references none of :client_id / :organization_id / :survey_id (nor any id literal), so nothing constrains it to this run's survey -- every figure it retu…

**TP-15** `527`  
> pass a single read-only SELECT (or WITH ... SELECT) statement

**TP-16** `512-530`  
> Error: Charts API timed out. Try again.

**TP-17** `545-549`  
> f'Unknown tool: {name}' / f'Tool {name} failed: {exc}'

---

## 4. Reverse index — what depends on each block

| ID | prime | depended on by |
|---|---|---|
| AI-01 | 2 | AI-07 |
| AI-02 | 3 | AI-07 |
| AI-03 | 5 | AI-07 · TP-07 |
| AI-04 | 7 | AI-07 |
| AI-05 | 11 | AI-07 |
| AI-06 | 13 | AI-05 · AI-11 · AI-12 · AI-16 · AI-17 · TP-13 |
| AI-07 | 17 | AI-16 |
| AI-08 | 19 | AI-10 · AI-16 · TP-06 |
| AI-09 | 23 | AI-02 · AI-03 · AI-10 · TP-03 · TP-14 |
| AI-10 | 29 | AI-16 · TP-06 |
| AI-11 | 31 | AI-12 · AI-16 · AI-17 |
| AI-12 | 37 | AI-16 |
| AI-13 | 41 | AI-16 · TP-09 |
| AI-14 | 43 | AI-16 · TP-04 · TP-05 |
| AI-15 | 47 | AI-16 |
| AI-16 | 53 | — (nothing) |
| AI-17 | 59 | AI-03 · AI-12 · AI-14 · TP-01 · TP-02 · TP-07 · TP-09 · TP-16 |
| AI-18 | 61 | — (nothing) |
| AI-19 | 67 | — (nothing) |
| AI-20 | 71 | — (nothing) |
| TP-01 | 73 | AI-17 |
| TP-02 | 79 | — (nothing) |
| TP-03 | 83 | — (nothing) |
| TP-04 | 89 | AI-14 |
| TP-05 | 97 | AI-14 |
| TP-06 | 101 | AI-18 · AI-19 |
| TP-07 | 103 | AI-03 |
| TP-09 | 109 | AI-13 |
| TP-10 | 113 | TP-06 |
| TP-11 | 127 | TP-06 |
| TP-12 | 131 | AI-19 · TP-06 |
| TP-13 | 137 | AI-06 · TP-06 |
| TP-14 | 139 | TP-06 |
| TP-15 | 149 | TP-06 |
| TP-16 | 151 | — (nothing) |
| TP-17 | 157 | — (nothing) |

Four blocks are depended on by nobody and depend on nothing — they are self-contained leaves:
**AI-20**, **TP-17**.
---

## 5. Sub-maps of the five oversized blocks

Four blocks are large enough that a line reference to the constant is useless. These are their
internal sections, at the granularity `tests.md` and `PROTOCOL.md` actually cite.

### AI-03 `AGENT_BACKSTORY` — 21,627 chars, `:24-278`

| lines | section |
|---|---|
| 24-62 | `GENERAL UNCLEAR-INTENT POLICY -- ASSUME, ANSWER, OFFER REFINEMENT` |
| 63-114 | classify the requested information before choosing tables |
| 115-156 | statistical honesty — always state N, never assert significance untested |
| 157-206 | when `run_survey_stats` cannot run as requested |
| **207-219** | `PCA RE-PLOT AND ATTRIBUTE SUBSETS` — the 2D↔3D re-call rule |
| 220-254 | benchmark comparison — no fixed benchmark id; reach it by category |
| **255-268** | the **KPI banner**, both branches: describe vs compare |
| **269-274** | the benchmark **two-sided bar chart**, categories sorted by gap |

### AI-05 `EXPECTED_OUTPUT` — 9,796 chars, `:283-389`

| lines | section |
|---|---|
| 283-284 | answer from tool output; report N beside any statistic |
| 285-294 | `UNCLEAR INTENT IN THE FINAL ANSWER` |
| 295-314 | `FORMAT` — Markdown, selective emphasis |
| **310** | never emit HTML tables, `<details>`, `<summary>` |
| 315-332 | `KEEP SUMMARIES SIMPLE` |
| 333-336 | `COUNT CAREFULLY` |
| 337-340 | `POOLED MEANS` — never rank products on a pooled mean |
| **341-366** | `UI CHART PAYLOADS` — a chart is part of the **default** answer |
| **359** | the pinned point key: `label` + numeric `value`, never `category` |
| 367-389 | `CHART RECOVERY (MANDATORY)` — substitute an axis rather than decline |

### AI-06 `NESTED_RESULT_RULES` — 9,739 chars, `:391-528`

| lines | section |
|---|---|
| 398-420 | `NORMALIZE BEFORE CHOOSING DEPTH` — grouping vs measure-label vs leaf roles |
| 421-427 | `JUDGE COMPLEXITY AT EVERY NODE; THERE IS NO FIXED ROW OR COLUMN THRESHOLD` |
| 428-435 | `CHOOSE THE HIERARCHY SEMANTICALLY` |
| 436-449 | `BUILD AN ADAPTIVE FORM` — max four grouping levels |
| 450-457 | `RECURSE AT EVERY NODE` |
| 458-468 | `RECURSIVE JSON GRAMMAR` — `{"type":"table","columns":[…],"rows":[…]}` |
| 469-503 | `EXAMPLE SHAPE` — the worked exemplar that fixed the §5 defect |
| 504-520 | `BUILD EVERY TERMINAL TABLE SEMANTICALLY` |
| 521-528 | `JSON SELF-CHECK BEFORE SENDING` |

### AI-08 `SCHEMA_OVERVIEW` — 37,746 chars, `:546-1212`

Ten numbered sections: 1 purpose `:548` · 2 retrieval procedure `:564` · 3 core analytical graph
`:657` · 4 grain/multiplicity/denominator `:734` · 5 resolving a question + instrument signature
`:846` · 6 where each question type stores its response `:914` · 7 route playbooks `:974`
(7.1 current survey `:976` · 7.2 statistics `:1002` · 7.3 benchmark `:1046` · 7.4 historical
`:1061`) · 8 validation and zero-result diagnostics `:1142` · 9 exact core columns `:1170` ·
10 stability boundary and fallback `:1202`.

### AI-12 `PERSONA_RULES` — 17,946 chars, `:1414-1623`

| lines | section |
|---|---|
| 1414-1433 | what counts as a persona request; no persona evidence is pre-fetched |
| **1434-1477** | `ONE PERSONA OR SEVERAL IS YOUR CALL` — a finding, not a formatting choice |
| 1478-1507 | plan the retrieval wide rather than serial |
| 1508-1523 | report lead strength from counts, never from an unrun test |
| 1524-1545 | construct only from supported fields |
| 1546-1556 | map every trait to its source question |
| **1557-1576** | roster output contract (`> **PERSONAS**`) vs the single-persona shape |
| 1577-1597 | the `## 💡 MOTIVATIONS & BEHAVIORS` table; follow-ups scoped to one persona |
| **1608-1617** | marginal distributions do not prove traits belong to the same people |

### AI-17 `INVENTORY_PREAMBLE` — 20,778 chars, `:1780-2061`

Two halves. **How to read the payload** — `result` `:1786` · `products` `:1792` ·
`scored_measures_by_product` `:1793` · `catalog` `:1820` · `benchmark_context` `:1850`. Then
**how to answer** — `A THIN REQUEST IS STILL A REQUEST` `:1883` · `THIS HOLDS ON EVERY TURN`
`:1898` · `PLAIN LANGUAGE` `:1949` · `WHICH MEASURE TO TEST` `:1962` ·
`END WITH CLICKABLE SUGGESTIONS` `:1985` · `WHAT GOES INSIDE IS A COMPLETE FOLLOW-UP PROMPT`
`:1999` · `DOABILITY DECIDES WHAT MAY BE OFFERED` `:2012` ·
`WHEN THE CATEGORY THE READER NAMED IS NOT DELIVERABLE, OFFER NOTHING FROM IT` `:2018` ·
`SIGNIFICANCE IS NOT YOURS TO ESTIMATE` `:2049`.

---

## 6. Dependency notes

**Two mutual pairs.** The graph is not a DAG, and both cycles are deliberate:

```
AI-06 NESTED_RESULT_RULES  ⇄  TP-13 wide_result_note
      the rule cites the "[shape: …]" note as its trigger; the note cites the rule as its action

AI-14 WORD_CLOUD_RULES     ⇄  TP-04 GENERATE_WORD_CLOUD_DESCRIPTION
      the rule says when a cloud may be emitted; the description says what the tool does
```

**The split that governs edits** (PROTOCOL.md §2): *what a tool is for* → `tool_prompts.py`;
*how to render the answer, when to segment, what never to claim* → `agent_instructions.py`.
Every TP→AI edge above is a case of a tool description pointing at a rendering rule it must not
restate, and every AI→TP edge is a rule naming the tool marker it keys off
(`PLSR ANALYSIS COMPLETE`, `[shape candidate: …]`).

**Highest fan-in:** AI-17 `INVENTORY_PREAMBLE` (prime 59) is depended on by 8 blocks — all five tool
descriptions that say "a UUID from the pre-fetched inventory" or share its JSON contract, plus
AI-03, AI-12 and AI-14. It is
also the block PROTOCOL.md §2 does not list in the assembly order, because it never enters the
system prompt. A change to the inventory contract touches more model-facing text than a change to
any single rule block.

**Highest fan-out:** TP-06 `NL2SQL_TOOL_DESCRIPTION` (prime 101) depends on 8 blocks — the schema,
the dialect rules and six runtime-feedback strings. Its dependency product is
`19 × 29 × 113 × 127 × 131 × 137 × 139 × 149 = 2,939,183,458,346,117`.

---

## 7. Two findings from building this

1. **`PROTOCOL.md` §2's line map is stale by 45-90 lines** on every rule block, and names a
   `BENCHMARK_SCOPE` constant that no longer exists. §2 also puts `build_system_prompt()` at
   `:1654`; it is at `:1742`. Anyone navigating by those numbers lands mid-block.

2. **`PERSONA_RULES` is imported into `funda_agent_exp.py:49` and never used.** It already reaches
   the model inside `SYSTEM_PROMPT_TEXT`, so the import is dead — a leftover from the unmerged
   persona routing (the `is_persona_*` / `survey_persona_inventory` symbols that
   `tests/test_agent_optimizations.py` still expects). The other four named imports from
   `agent_instructions` are all live: `INVENTORY_PREAMBLE` at `:3503`, `REPEATED_SQL_WARNING` at
   `:3678`, `zero_row_streak_warning` at `:3680`, `STEP_BUDGET_NOTICE` at `:3845`.

---

## 8. Regenerating this file

Line numbers drift with every prompt edit. To re-read them from the AST rather than by eye:

```bash
.venv/bin/python - <<'PY'
import ast
for path in ("agent_instructions.py", "tool_prompts.py"):
    tree = ast.parse(open(path, encoding="utf-8").read())
    print(f"### {path}")
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
           and isinstance(node.value.value, str):
            name = node.targets[0].id
            print(f"{node.lineno:6d}-{node.end_lineno:<6d} {name:42s} {len(node.value.value)} chars")
        elif isinstance(node, ast.FunctionDef):
            print(f"{node.lineno:6d}        def {node.name}")
PY
```

Confirm a rule actually reached the assembled prompt:

```bash
.venv/bin/python -c "import agent_instructions as ai; print('<marker>' in ai.build_system_prompt())"
```
