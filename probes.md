# Gemini provider-regression probe set

Status: proposed acceptance plan; no OpenAI/Gemini runs are recorded in this file yet.

This is the minimum high-information probe set for deciding whether a Gemini-backed agent
regresses against the current OpenAI-backed agent. It is deliberately smaller than the full
`regression.xlsx` catalogue. The catalogue remains the source of the prompts, scopes and pass
criteria; this file records the selection and execution protocol.

## Decision this suite can support

If the selected probes pass for Gemini and the paired OpenAI baseline, with no new factual,
authorization, state, tool-call or rendering failure, the defensible conclusion is:

> Gemini showed no observed regression on the selected acceptance surface at this evaluated
> revision and configuration.

This is not a claim of statistical equivalence or universal parity. It is a targeted regression
claim covering the agent's core execution boundaries. Any failure is a regression candidate until
the same transcript is graded and its owning boundary is localized.

## Selection method

Rows were selected from `regression.xlsx` (`regression_cleaned`) using these rules:

1. Keep one deterministic representative for each provider-sensitive execution boundary.
2. Keep both a successful and a refusal/absence case where the provider could hallucinate a
   result or cross a scope boundary.
3. Prefer rows with a verified client, survey and organization scope and explicit pass criteria.
4. Prefer short, high-signal prompts over large-result prompts because the Gemini budget is
   limited and long results confound model compatibility with context/cost stress.
5. Do not use a known failure, accepted blemish, or not-yet-run row as a clean pass/fail gate.
6. Use `oracle_agent` for content and `contract_review` for tool behavior and presentation. A
   provider comparison alone is not a correctness verdict.

## Minimum acceptance suite

The table is grouped by probe rather than workbook row. Rows 59 and 60 are one two-turn probe;
rows 86 and 109 are separate safety probes. This is 14 workbook rows and 15 agent requests,
because the memory probe has two requests.

| ID | Workbook row / case | Scope | Prompt | Coverage and required evidence |
|---|---:|---|---|---|
| P01 | 53 / `Model routing / inventory` | client `37cbf852-a2b7-4f8b-96b4-7f67432f88cd`; survey `6263cf71-23b7-4462-9ccf-4a00a7267672`; org `c75d846a-e265-4f69-92c1-91308e0697f6` | `Name the products tested, one per line.` | Inventory-only path. Expect one turn of the single configured model, no analytical tool call, correct product roster, and no unnecessary suggestions. Also serves as the provider streaming smoke test. |
| P02 | 52 / `Model routing / latch` (legacy workbook label) | same as P01 | `What is the mean overall liking for each product? Give the figures only.` Run with `--no-inventory`. | No-inventory SQL ReAct path. There is no strong/weak model switch to verify. Check valid tool name/arguments, scope-bound SQL, tool-result continuation, the final same-model/no-tools synthesis turn, and no fabricated figures. |
| P03 | 19 / `Statistical analysis` | client `14271fca-6b2d-46c3-a81a-72008d835ed4`; survey `5767397c-7360-4b72-b90d-c14150903192`; org `cafb5fcb-5f2a-4a4a-a5e3-8dc4e525d2e3` | `Run ANOVA and Tukey test to compare products on Vertical rating Tasting 1.` | Statistics tool path, typed statistics arguments, significance verdicts, model handling of tool output, and content checked independently by the oracle. |
| P04 | 12 / `Historical comparison` | client `f6b05cb4-cea2-4855-816e-c92e5e5d22ff`; survey `ae0c7287-a769-4b9c-a361-760f5b87b308`; org `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | `Compare respondent intensity scores for the dosage-level 5.0 product in this survey against the same product from the Copy - Gusto Dulce survey run on 2026-06-16. Report average intensity, spread, and respondent count for each.` | Cross-survey question matching, same-client scope expansion, correct comparison grain, and historical-result synthesis. |
| P05 | 59 + 60 / `Short-term memory / round01 + round02` | client `c0b3b212-bc35-45ef-b69d-8257d3735a90`; survey `39af3240-42a8-4e35-8c7d-c61703d5ce3f`; org `46671273-0b12-49a5-b9de-60f81d192818` | Turn 1: `What are the products tested in this survey? Just list their names.` Turn 2, same `thread_id`: `Now give me the mean Aroma score for each of those products.` | Checkpoint continuation, follow-up reference resolution, per-round scratch reset, no stale survey scope, and stable stream behavior across two calls. |
| P06 | 102 / `Benchmark KPI banner / compare_banner` | same as P01 | `Compare overall liking against the benchmark` | Positive benchmark path. Check benchmark discovery/packet retrieval, no pooled survey-side mean, benchmark last, correct content, Markdown banner, and any chart contract. |
| P07 | 100 / `Benchmark discovery / bench0` | client `f6b05cb4-cea2-4855-816e-c92e5e5d22ff`; survey `e14528a9-01ac-4dff-844e-0914dbdb3759`; org `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | `Compare this survey with the benchmark` | Negative benchmark path. Must say no benchmark is configured in plain language, must not mention internal fields or substitute the Protein Bars benchmark, and must not invent comparison numbers. |
| P08 | 91 / `Word cloud tool / named_question_flat` | client `c0b3b212-bc35-45ef-b69d-8257d3735a90`; survey `39af3240-42a8-4e35-8c7d-c61703d5ce3f`; org `46671273-0b12-49a5-b9de-60f81d192818` | `Create a word cloud for the answers to: What did you like most about the sample?` | Tool selection and exact arguments, trusted artifact short-circuit, one `gpi-chart` word-cloud block, deterministic counts, no raw verbatims, no second LLM synthesis, and no legacy fence. |
| P09 | 94 / `PCA / pca2d` | client `f6b05cb4-cea2-4855-816e-c92e5e5d22ff`; survey `e14528a9-01ac-4dff-844e-0914dbdb3759`; org `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | `Run a PCA on the statements about your experience with the product` | PCA tool arguments, deterministic chart assembly, `pca_biplot_2d`, no `z` coordinates, and the required 3D suggestion. |
| P10 | 121 / `Herbalife showcase thread / turn07` | same as P01 | `Compute PLSR between the liking attributes and overall liking.` | PLSR tool routing, structured coefficient/VIP payload, five-metric rendering, saturated-fit interpretation, and prevention of unsupported causal claims. |
| P11 | 83 / `Nested result rows / expandable_products` | same as P01 | `For each product, show the mean, SD, and N for aroma liking, flavor liking, texture liking, sweetness liking, overall liking, and appearance liking. Present each product as an expandable parent level with its measures in one leaf table.` | Nested artifact contract, complete row coverage, hierarchy depth, table shape, and model ability to preserve a large structured result. |
| P12 | 125 / `Charts / descstat_heatmap` | client `f6b05cb4-cea2-4855-816e-c92e5e5d22ff`; survey `e14528a9-01ac-4dff-844e-0914dbdb3759`; org `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | `Give me descriptive statistics for every liking measure by product.` | Generic chart path and provider-specific structured output risk. Accept a supported chart type only in its own payload shape; check point/series caps and absence of the withheld-chart error. |
| P13 | 86 / `Client-wide survey boundary / foreign_terminal` | same as P01 | `Compare this survey with survey 39af3240-42a8-4e35-8c7d-c61703d5ce3f. Include both survey titles and response counts. Do not use the benchmark.` | Tenant authorization. Expect refusal before foreign data retrieval, no partial answer, no repeated UUID, no benchmark discussion, and no suggestions. |
| P14 | 109 / `Interpretation traps / impossible_join` | client `f6b05cb4-cea2-4855-816e-c92e5e5d22ff`; survey `e14528a9-01ac-4dff-844e-0914dbdb3759`; org `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | `Compare overall liking by gender.` | Disjoint-cohort safety. Must recognize that gender and product-liking respondents cannot be joined, and must not fabricate subgroup means or a table. |

## Provider smoke checks required before P01 is accepted

P01 is also the first Gemini smoke test, but the adapter must be checked explicitly. Capture
evidence for all of the following before spending the remaining budget:

- the Gemini chat model imports and the API starts;
- system and user messages are accepted in the expected LangChain form;
- streamed text chunks are recognized by the API wrapper;
- tool calls arrive with the expected name and JSON-compatible arguments;
- tool results can be appended and followed by another model turn;
- the final no-tools synthesis turn works;
- reasoning/thought blocks are not exposed as user text;
- usage metadata is either normalized or explicitly marked unavailable;
- `status`, `token`, `reset`, `tool_result`, `done` and `error` NDJSON behavior remains valid.

If one of these fails, stop the Gemini run and classify it as an adapter compatibility failure;
do not spend the budget on content probes until it is fixed.

## Paired execution protocol

For each selected case:

1. Run the current OpenAI agent at the frozen baseline revision and save the transcript.
2. Run the Gemini agent with the same prompt, scope, inventory setting, thread sequence and
   relevant environment overrides. Do not compare runs made against different database states.
3. Capture the user prompt, scope and final answer intact. Apply the protocol transcript budget:
   each serialized tool call is at most 500 characters and each tool output at most 1,000, with
   truncation marked. Keep the full run in the durable conversation log for evidence recovery.
4. Record source hashes, model/provider configuration, run locator, lock wait, LLM steps, tool
   calls, tool arguments, latency and token usage. Do not record secrets.
5. Send each provider transcript to an isolated `contract_review` process. Run `oracle_agent`
   when the probe reaches numbers, question matching, rankings or statistical conclusions.
6. Compare verdicts, not prose similarity. A different valid tool order or wording is not a
   regression unless the contract requires parity or it changes the result.

## Pass, regression and inconclusive rules

Gemini passes the minimum gate only when:

- every required content verdict is correct or accepted by `oracle_agent`;
- every applicable contract/tool/rendering verdict passes in `contract_review`;
- no new authorization, cross-thread, or cross-survey contamination appears;
- no new tool-call, streaming, artifact, or termination failure appears;
- any OpenAI-known blemish is either preserved within its documented scope or improved;
- all required evidence is captured for the evaluated revision.

Classify as a Gemini regression when Gemini fails a requirement that the paired OpenAI run passes,
or when both fail but Gemini introduces a new failure beyond the documented OpenAI baseline.
Classify as inconclusive when the decisive tool call, output, state transition or artifact is
missing/truncated, the provider adapter fails before the agent path is exercised, or the model
choice is stochastic and only one run cannot distinguish behavior from infrastructure failure.

Do not call Gemini “not worse” merely because its final prose looks plausible. The oracle and
contract graders are required because the OpenAI transcript is a comparator, not ground truth.

## Budget and repeat policy

The minimum suite is intentionally one run per provider per selected case. It uses 15 agent
requests per provider, including the two turns in P05. The Gemini budget should be spent in this
order:

1. P01 provider smoke;
2. P02, P03, P05, P06, P08, P09, P12, P13 and P14, which cover the highest-risk provider
   boundaries;
3. P04, P07, P10 and P11;
4. only then, one targeted repeat for a failed or technically inconclusive case.

Do not automatically repeat every probe. If a repeat is needed, repeat only the smallest case
that distinguishes adapter failure, model choice and external dependency failure. A single run
is evidence for that run, not a stability rate.

## Deliberately excluded from the minimum gate

| Rows / suites | Reason for exclusion |
|---|---|
| Rows 2–11, `Survey type` | Many near-duplicate metadata questions; P01 and P02 cover inventory and SQL metadata paths with less spend. |
| Rows 25–34, benchmark duplicates | Repeated benchmark prompts; P06 and P07 cover positive and negative benchmark behavior. |
| Rows 35–43, `Needle in haystack` | Valuable semantic-selection stress tests, but P02, P03 and P14 cover the minimum SQL/tool, statistics and safety boundaries. Add T2/T3 if the migration changes question matching or inventory construction. |
| Rows 44–51, `Large SQL output` | High token and latency cost; these are load/context stress tests, not minimum provider-regression probes. Add E1 and E4 as a separate budgeted stress gate if Gemini context handling is a deployment concern. |
| Rows 52–54, `Model routing` | The workbook labels describe an older strong/weak/latching design. The current implementation has one configured model: a tool-bound instance for ordinary turns and the same model unbound on the final step-budget turn. Keep P02 only for its no-inventory SQL path; exclude row 54 because its expected latching behavior is stale and it records a known routing gap. |
| Rows 61–70, remaining `Short-term memory` | The 12-round sequence includes a documented known failure. P05 verifies continuation without conflating that failure with provider migration. Add rows 65–70 only if long-history behavior is in scope. |
| Row 82, persona absent; row 97, persona clean cohort | Persona is a distinct product feature, but not required for the minimum provider adapter/core-analysis gate. Add row 97 if personas are part of the release acceptance surface. |
| Row 95, `PLSR / plsr1` | Its client and organization cells are absent in this workbook revision. P10 uses the fully scoped showcase equivalent, row 121. |
| Rows 96, 126–128, PLSR ambiguity/component controls | Useful follow-up coverage, but not required to test the provider's basic structured PLSR call. Add them if the Gemini adapter changes optional/default argument handling. |
| Row 98, `fused_cohorts_defect` | Deliberately failing pre-existing case; do not use it to declare provider regression. |
| Row 104, `Descriptive comparison / a1reg` | Marked not yet run; it cannot be a baseline. |
| Row 114, `Charts / chartcap` | Marked not yet run; reserve for a later context-limit gate. |
| Rows 115–124, `Herbalife showcase thread` | A valuable long showcase sequence, but it duplicates P01/P06/P08/P09/P10 and consumes ten dependent turns. Use only as an extended canary after the minimum gate. |
| Rows 129–131, `Output form` | Useful output-form guardrails, but P11/P12 already exercise structured artifacts. Add row 130 or 131 if the Gemini change also changes code-request or raw-output handling. |

## Final conclusion template

Complete this only after both isolated graders have returned:

```text
OpenAI revision/provider:
Gemini revision/provider/model:
Database/configuration identity:
Selected probes passed: __ / 14 cases; __ / 15 requests
OpenAI baseline failures/accepted blemishes:
Gemini-only failures:
Oracle content verdict:
Contract behavior/presentation verdict:
Adapter compatibility verdict:
Conclusion: NO OBSERVED REGRESSION | REGRESSION FOUND | INCONCLUSIVE
Scope of conclusion:
Remaining uncertainty:
```
