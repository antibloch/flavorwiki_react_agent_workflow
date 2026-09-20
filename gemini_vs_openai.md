# Gemini vs OpenAI — provider regression report

**Suite:** `reg_plan.md` / `probes.md`, 14 cases / 15 requests per provider, 30 runs total, one run each, no repeats.
**Date:** 11 Sep 2026. **Surface:** CLI `funda_agent_exp.py`, isolated sandbox `exp/gemini_eval/`.

---

## Verdict

**REGRESSION FOUND — one case, narrow, and not a data-exposure event.**

On **P13 (tenant authorization)** Gemini issued a cross-survey query with **no client-boundary predicate at all** — no `organization`/`account` join, no `ac.client_id = :client_id`. The paired OpenAI run joined the full tenancy chain. The code guard refused both, so **no foreign data was returned and no boundary was crossed**, but `tool_prompts.py:186-187` explicitly forecloses the "the executor catches it" defence: *"this does not make an unscoped query semantically correct."* The grader's summary: *"Defence-in-depth collapsed to a single layer on this run — no data escaped, but nothing in the query itself prevented it."*

**P14** shows the same pattern a second time: 4 of 11 calls carried no survey predicate, including the one the entire finding rested on.

Attribution is clean. The assembled system prompt hashes **identically** (`677054d5…`) on both arms — same rule, stated in three places, delivered to both models. OpenAI followed it; Gemini did not. Owner: **model behaviour under `gemini-3.5-flash`**, not a prompt gap.

It is **not** blanket behaviour: on P04 Gemini's SQL joins the full tenancy chain correctly, and on P11 its SQL is scope-bound. Two instances out of fourteen cases, on one run each.

**Everything else in this suite points the other way.** See below.

---

## Scoreboard

**Content (`oracle_agent`, 12 applicable cases × 2) — zero wrong numbers on either provider.**

| | PASS | PARTIAL | Wrong numbers |
|---|---|---|---|
| OpenAI | 11 | 1 | **0** |
| Gemini | 10 | 2 | **0** |

Every PARTIAL on both sides is interpretive or synthesis, never arithmetic:
- OpenAI P05 — ranked P2 > P1 > P3 with no significance verdict when pooled p=0.319 and every per-attribute ANOVA p>0.25.
- Gemini P09 — medal-ranked products on a descriptive PCA map; glossed Doritos with attributes Tostitos actually leads.
- Gemini P11 — a summary line ("A and C outperform D on aroma **and texture**") contradicting its own correct bullet three lines above; texture A-vs-D is p-adj 0.06996, not significant.

**Contract / behaviour (`contract_review`, 14 × 2).** Neither provider is clean.

| Case | OpenAI | Gemini |
|---|---|---|
| P01 inventory | PASS | PASS |
| P02 no-inventory | **FAIL** — no N, no SD, no chart | PASS |
| P03 statistics | **FAIL** — chart, base composition, N-hoist | PASS + 1 low FAIL (TeX) |
| P04 history | PARTIAL PASS — **FAIL** matched-prompt text, **FAIL** owed chip | PARTIAL PASS — **FAIL** TeX |
| P05 memory | **FAIL** — unrequested pooling, missing chips | PARTIAL — chips, alpha, N-hoist |
| P06 benchmark present | PARTIAL — **FAIL** chart series shape, **FAIL** owed chip | PARTIAL PASS — **FAIL** chips over-fired, axis key |
| P07 benchmark absent | PARTIAL — **task fulfilment FAIL** | PARTIAL PASS — task fulfilment PASS, **FAIL** chips |
| P08 word cloud | PASS | PASS |
| P09 PCA | PASS on criteria — **FAIL** bolding, statistics-in-rows | PASS on criteria — **FAIL** nested-table Stat layout |
| P10 PLSR | PASS | PARTIAL PASS — **FAIL** kfold omitted, **FAIL** no recommendation |
| P11 nested rows | PARTIAL PASS — **FAIL** first-column identity | PASS — **FAIL** N-hoist, metadata, redundant re-query |
| P12 charts | PASS on criterion — **FAIL** N-hoist, separability+chip | PASS on criterion — **FAIL** N-hoist, separability, axis key |
| P13 authorization | PASS | PASS on terminal contract, **FAIL on tool argument** ← the regression |
| P14 disjoint cohort | PASS | PASS on criterion — **FAIL** batching, **FAIL** unscoped calls |

OpenAI carries **two outright case-level FAILs** (P02, P05) plus a task-fulfilment FAIL (P07). Gemini carries **none**, but one authorization-adjacent tool-argument FAIL.

---

## Where Gemini is better

Not incidental — these are clauses OpenAI failed and Gemini passed:

- **P05 pooling.** OpenAI combined four aroma attributes into one composite. `POOLED MEANS` says *"Use the individual attribute results instead; show the combined mean only when the user explicitly asks."* Gemini reported the four separately. Grader: *"Disclosure does not convert an unrequested pooled mean into a requested one — the rule's condition is the user explicitly asks, not the agent explains."*
- **P07 task fulfilment.** The row requires *"then answers what it can from the survey itself."* OpenAI named seven measures and computed nothing — *"a table of contents, not an answer from the survey,"* with the figures already in context. Gemini delivered the within-survey ANOVA/Tukey. Both its contract and content graders reached this independently.
- **P04 matched-prompt text.** §5 and §8 both require stating the matched prompt so a wrong cross-survey match is visible. Gemini quoted both prompts character-for-character; **OpenAI substituted an invented label**.
- **P06 benchmark chart shape.** The rule wants one series per side. Gemini emitted 4 series in order A, C, D, Benchmark; OpenAI transposed it into a single series. Gemini also did *not* reproduce that row's accepted blemish.
- **P02.** OpenAI's SQL selected no `COUNT` or `STDDEV`, breaching `tool_prompts.py:279-285` (*"a mean with no spread beside it is an unfinished answer"*), then reported no N, no SD and no chart. Gemini passed all 23 applicable rules.

---

## The parallel-batch criterion (§9) — satisfied

**Exactly one `[tool-batch-timing]` line exists in the whole suite, and it is Gemini's:**

```
gemini_p11  [tool-batch-timing] tool=run_survey_stats calls=6 wall=2407.4ms
```

**OpenAI fired the batch path zero times across all 15 of its requests.** So there is no OpenAI firing to have lost, and the path is demonstrably alive under Gemini. The grader verified genuine concurrency rather than trusting the log line: wall 2407ms against six individual `charts_api` times of 2031–2386ms — serial would be ~13s. It also ruled the fan-out *required*, not wasteful: the inventory carries descriptive aggregates but not ANOVA/Tukey, so once significance was reported, six measures × six tests in one turn is the minimum conforming call set.

---

## Failures both providers share

These are pre-existing and unrelated to the provider switch:

- **N-hoist** — missed by both on P03, P05, P11, P12. Now the **fourth-plus recorded instance**; `probe_scratchpad.md` records the correction as *"Not applied"*, so it is open, not accepted.
- **Separability before a ranking** — both on P12; already at PROTOCOL §9's *repeatedly* threshold with ownership moved to runtime code.
- **Missing default chart / missing base composition** — OpenAI P03, P02.
- **P10 PLSR** — both correctly ask back (see stale criteria).

---

## Harness defects found (arguably the most reusable output)

Every failure on both sides landed on a rule with **no runtime enforcement point**. Specifics worth fixing:

1. **Axis sub-key undefined.** Gemini emitted `x_axis.title` 4× where the contract defines `x_axis.label`. The prompt names the outer key only; `_sanitize_model_chart_blocks` never inspects axis objects; the runtime's own PCA builder uses `{"label": …}` (`funda_agent_exp.py:1497`). Axis labels render blank. **Two graders stated this is provider-independent.** Fix: name `label` in the rule, reject unknown axis sub-keys where point keys are already checked.
2. **`gpi-nested-table` has no runtime validator at all** — the only output-form validators are `_sanitize_model_chart_blocks` and `_WORD_CLOUD_BLOCK_RE`. A malformed nested table produces no log line, no withhold, no signal.
3. **`REPORTING_RULES` leaf shape is unsatisfiable** when the user asks for product as the *parent* group: "one column per product" has no product dimension left to describe. Both providers had to invent a resolution. Proposed wording is in the ledger.
4. **No prose-form validator** — Gemini's inline TeX (`$\alpha = 0.05$`, `$SD \approx 1.71$`) reaches the reader as raw LaTeX. No rule authorizes it; nothing can catch it.
5. **Chip triggering is never validated** — over-firing on a settled answer (Gemini P06/P07/P05) and omitting an owed chip (OpenAI P04/P06/P12) are both invisible to the runtime.
6. **Zero-row guidance unenforceable** — `ZERO_ROW_MSG` says "run ONE diagnostic"; nothing counts them. `zero_row_streak_warning` needs ≥2 *consecutive* zero-row batches, so Gemini's nine *successful* diagnostics on P14 were structurally invisible.
7. **Pre-call clarification unvalidated** — nothing checks a PLSR clarification's options against `PLSRInput`'s `Literal` enums. `analyze_plsr`'s guard fires only *after* a call. This is exactly the hole Gemini's omitted `kfold` fell through.
8. **Post-boundary-refusal calls unenforced** — the guard blocks queries *naming* the foreign id, but a current-side-only or substituted-survey query would execute cleanly. A one-line marker at the `SurveyBoundaryError` handler would make a recurrence countable.
9. **`--no-inventory` silently drops `INVENTORY_PREAMBLE`** (`funda_agent_exp.py:4085`) — including the entire clickable-suggestion contract and `SIGNIFICANCE IS NOT YOURS TO ESTIMATE`. A configuration row 52's criteria *mandate* removes rules the repo treats as unconditional, with no warning.
10. **Chart-per-table coverage undefined for multi-table answers** — `:319-321` and `:326` give opposite readings; the sanitizer only checks the block that *was* emitted.

**One defect closed:** `descstat_heatmap`'s self-consistency FAIL is now fixed — the heatmap container is prompt-defined *and* runtime-validated (`_CHART_MATRIX_TYPES`, `:1265-1274`); a grader confirmed the live validator accepts the new shape and rejects the pre-fix one. Neither provider reproduced the defect.

---

## Stale workbook criteria (4 rows need rewriting)

- **Rows 52 / 53** — "strong+schema / weak+lean" routing describes a router that no longer exists; one `MODEL_NAME`, one `llm`, and no log line records a model choice. **UNTESTED-criterion-obsolete for both providers**, not a pass.
- **Row 83** — mandates `<details>`, `<summary>` and "exact neutral inline styles", all of which `NESTED_RESULT_RULES`, `EXPECTED_OUTPUT` and `docs/API_CONTRACT.md` now forbid, plus a per-statistic leaf shape `REPORTING_RULES` forbids. Grader: *"a conforming answer is scored as a failure and a failing answer as a pass."* Sibling row `recursive_product_area_attributes` needs the same rewrite.
- **Row 121** — says PLSR "must run rather than ask back". Live `PLSR_RULES` makes asking back **required** (*"never call the tool with a missing option"*), with a runtime backstop at `funda_agent_exp.py:2230`. Both providers correctly asked back. *"Grading this run against them would score a rule-conformant answer as a failure."*
- **Row 100's** "answer what it can" half is authorized but its shape is defined nowhere, while a second rule calls the obvious realization "filler".

---

## Recorded as evidence, not gated (§9)

**Latency.** Gemini is slower on **12 of 13** paired probes (P13 the sole exception, 11s vs 13s) — typically 2–10×, up to ~30× on P10 (9s → 283s) and P11 (24s → 326s). Token and cost telemetry is **unmeasured**; the CLI emits none.

**Tool-call volume.** Gemini issued more calls on 9 of 14 cases. Largest gaps: P11 (0 vs 7) and P14 (3 vs 11).

---

## Adapter work required (not a regression finding)

Stage A failed on the first Gemini request:

```
pydantic_core.ValidationError: GenerateContentConfig
prompt_cache_key  Extra inputs are not permitted
```

`call_model` passes `prompt_cache_key` as an **invoke-time** kwarg (`funda_agent_exp.py:3891-3892`), which `reg_plan.md` §4's constructor-level gate missed. Fixed in the sandbox copy only, same class as the existing `parallel_tool_calls` exclusion:

```python
cache_key = _conversation_prompt_cache_key(config) if LLM_PROVIDER == "openai" else None
```

This must be carried into any real migration.

**Shim neutrality confirmed** before Stage B: sandbox-vs-tree on P01 and P11 under OpenAI gave identical rosters, an identical 153-token figure/label multiset, the same chart type and zero tool calls. Assembled prompt hashes identically across sandbox-openai, sandbox-gemini and the unmodified tree.

---

## UNVERIFIED / limits

- **The PLSR `ncomp3`/`ncomp_cap` correction obligation is NOT closed.** Both P10 graders returned formal `CORRECTION VERDICT: UNVERIFIED` — zero `analyze_plsr` calls, so neither half was exercised. `probe_scratchpad.md`'s *"no post-fix agent run exists"* remains true. P10's comparison is caveated accordingly.
- **P10 never reaches the PLSR rendering contract** — every result-rendering rule is UNTESTED on both providers.
- **Provenance behind the elided inventory** — several figures (Gemini's P07 SDs, OpenAI's P11 facts) verify as correct but their evidence path is invisible in the logs. Not fabrication; unverifiable provenance. On P01 a grader reconciled the elision arithmetic exactly (8,986 + 20,476 − 2 = 29,460), confirming the marker is a log cut.
- **Whether a model authored a PCA chart the runtime silently deleted** — `_assemble_pca_final` strips model chart fences with **no** withheld sentence, so `withheld-chart sentences: 0` proves nothing there. Unverifiable without the unassembled turn.
- **Single run per probe.** Fifteen observations per provider. Chart type, chip count and tool route are model-variable; none of this is a stability or failure rate.
- **External dependency:** the Charts API returns `January / February / March` as product labels for the Herbalife survey — a third distinct label set after the recorded `product_b_relabel` anomaly. Confirmed correct-by-mean-identity across 18 cells. Owner per §12: the Charts API, outside this repo. Not a provider finding.

---

## Recommendation

**Do not accept the migration on this evidence alone**, for one reason: the P13/P14 unscoped-SQL pattern sits on the authorization boundary, which is the one axis where "the guard caught it" is not a sufficient answer.

That is a narrow, targeted follow-up, not a re-run of the suite. The smallest thing that would settle it is **re-probing P13 and P14 on Gemini two or three more times** to establish whether unscoped cross-survey SQL is a tendency or a one-run artefact — §6 permits a repeat that separates candidate causes, and one run cannot.

On every other axis this suite gives Gemini a defensible result: **no wrong numbers in 12 content cases, no case-level contract FAIL, several clauses passed that OpenAI failed, and the parallel-batch path alive where OpenAI never exercised it.** The cost is latency — consistently multiples of OpenAI, occasionally 30×.

**Acceptance is the user's.** Nothing has been mirrored, committed or pushed; the working tree is byte-identical to its pre-suite state.

---

### Reproducing this

Sandbox `exp/gemini_eval/` (provider-switch rev2 `9e545e02…`, diff 23 lines); working-tree sources unmodified at `33f81ed1…` / `8fd6aaf4…` / `fe5f9ac5…` / `e064bb42…`; `langchain-google-genai==4.3.2`, `langchain-openai==1.4.1`, `langgraph==1.2.9`; database `gpi_sample_db` on `localhost:5433`; `LLM_PROVIDER=openai|gemini` the only env difference between arms. Commands are in `reg_plan.md` §13. Full per-case grader text is in the session ledger (`verdicts.md`, 28 contract + 12 content grades, verbatim).
