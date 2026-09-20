# Empirical findings — `funda_agent_v1` vs `funda_agent` vs `funda_agent_exp`

Consolidated reference for every measurement taken on the three FlavorAI survey-analyst agents.
Self-contained: test definitions, ground truth, methodology, all raw comparisons, and the
conclusions drawn from them.

**Measured:** 2026-08-04, against the docker Postgres sample DB (`gpi-db`, port 5433).
**Total runs:** 185, all preserved as JSON under `needle_haystack_results/` (149 for §3–§9, 36 for the memory experiment in §10.3).
**Models:** `gpt-5.5` (strong) / `gpt-5.4-mini` (weak), `reasoning_effort=low`.

> **Cost figures are illustrative.** They assume list rates of $1.25/$10.00 per MTok (strong) and
> $0.25/$2.00 (weak), cached input at 10%. Tokens are measured; dollars are derived.

---

## 0. The three agents

| file | architecture |
|---|---|
| `funda_agent_v1.py` | Bare LangGraph ReAct loop. No scope-id binding (the model retypes literal UUIDs), no SQL guardrails, no result store, no drill. Tools: `nl2sql_tool`, `run_survey_stats`. |
| `funda_agent.py` | v1 + scope binding & repair, planner preflight, dialect hints, zero-row diagnostic, ledger fields, lean tool bindings, result parking, and a **single-turn digest drill workflow**. |
| `funda_agent_exp.py` | The above + PROGRESS TRACKER echo, shape-based inline bypasses, and a **tool-using drill agent** (4 tools, ≤4 turns). Later: pre-fetched inventory, stats-binding fix, significance gate. |

All three load the same `agents.yaml` / `tasks.yaml` / `schema_overview.md` and share
`MAX_LLM_STEPS = 12`, so results are directly comparable.

---

## 1. Methodology

**Instrumentation.** Harnesses patch `ChatOpenAI.invoke` and `BaseTool.invoke` at **class level**.
This gives one definition of "a turn" across all three variants and captures every LLM call on
every path — main loop, drill sub-agent, any variant-specific node. The per-module `TURN_LOG`s do
not agree on what they record, so they were not used. Verified against `funda_agent_v1`'s own
`[route]` log: identical turn counts.

**Isolation.** Each (agent, case) pair runs in a **fresh process**, so `PARKED_RESULT_IDS`, the
in-process result store and module state cannot leak between cases.

**Fairness.** Within a case, all variants run **concurrently**, so they see identical API
conditions. Within an agent, case order is fixed, so each gets the same cold→warm prompt-cache
pattern.

**Ground truth** was derived independently — direct SQL against the DB, or the Charts API — never
taken from an agent's own output. Every reference value in §2 was re-verified before scoring.

### 1.1 Variance — read this before trusting any single number

A spot re-run of `funda_agent_v1` on T4 gave **9 turns / 45,200 input tokens** where the benchmark
run gave **4 turns / 32,485** — same agent, same case, +39%. Wall time was nearly identical
(27.7 s vs 28.6 s) because the extra turns were cheap weak-model ones.

Consequently:

- **Accuracy findings are robust.** They are categorical failures reproducible from the
  transcripts — a pooled scale, a fabricated product ordering, a distractor question selected, a
  statistic invented — not close numeric calls.
- **Efficiency rankings are directional.** Differences under ~10% are inside single-run noise.
  Large gaps (3×+) survive it.
- Everything here is **n=1 per cell**. Nothing was repeated.

---

## 2. Test material and ground truth

### 2.1 Needle-in-a-haystack set (`needle_haystack_testset.md`) — 6 cases

The task is identical in all six: the user names a concept ("overall liking") and the agent must
find which question measures it, then compute the answer or say plainly that no such measure
exists. The **needle** is the correct question; the **haystack** is every other scorable question,
including distractors sharing the concept's keywords. Keyword search cannot solve these.

| # | survey | measures | needle | tests |
|---|---|---:|---|---|
| T1 | Herbalife | 6 | yes | baseline semantic match against 3 "OVERALL …" distractors |
| T2 | PepsiCo | 83 | yes | 24.5 KB inventory; discriminating words at char ~150 |
| T3 | Consumer Qualification | 8 | yes ×2 | **scale trap** — same prompt, two different scales |
| T4 | Sensory Descriptive | 3 | **no** | absence — must refuse, not average intensity scales |
| T5 | Beef Jerky | 4 | **no** | absence + a ranking whose labels don't match products |
| T6 | Herbalife + benchmark | 6+6 | yes | cross-tenant benchmark carve-out |

**Verified reference values** (re-derived from the DB during this work):

- **T1** — Product A 5.95 (SD 2.23), C 5.86 (2.21), D 5.57 (2.21), N=150 each. Separability
  threshold ≈ 0.51, largest gap 0.38 → **not separable**.
- **T2** — Doritos 8.32 (0.89), Lay's Classic 8.14 (0.84), Lay's BBQ 8.11 (0.82), Tostitos 8.02
  (1.02), N=300 each. ANOVA F=5.8655, p=0.000564 (significant). **Tukey: Doritos `a`, Classic
  `ab`, BBQ `b`, Tostitos `b`** — Doritos separates from BBQ and Tostitos but **not** from Classic.
  The hand rule `2·SD·√(2/N)` ≈ 0.145 wrongly calls the 0.18 gap real; it ignores the
  multiple-comparison correction across 6 pairs.
- **T3** — two question rows, identical prompts, different scales.
  `09de3be1` (0–14): P2 10.25 / P1 9.25 / P3 9.00. `31c1f069` (0–4): P2 3.50 / P1 2.50 / P3 1.75.
  N=4 each. Pooling gives 6.88 / 5.88 / 5.38 — right order, **meaningless magnitudes**; scored as
  a failure.
- **T4** — no overall-liking measure. Only Appearance / Aroma / Texture *intensity* line-scales
  (1–15, 17 respondents) plus two open-text like/dislike items.
- **T5** — no overall-liking measure. A preference ranking exists but its option labels
  `251, 352, 709, 873, 928` **do not match** the product blinding numbers
  `944, 551, 815, 601, 332` (zero overlap, verified). Correct answer names *both* facts.
- **T6** — same needle as T1; benchmark survey `4ec4b648…`, Product B mean 6.00 (SD 2.11, N=150).
  All three products within noise of the benchmark.

### 2.2 `regression.xlsx` — 18 populated rows

A broader mix than the needle set, used to test whether needle-set wins generalise.

| type | rows |
|---|---:|
| Benchmark Comparison | 8 |
| Historical comparison (cross-survey) | 4 |
| Statistical analysis (explicit ANOVA/Tukey/Pearson/Spearman) | 3 |
| Survey metadata | 3 |

Six rows are duplicate prompts (R11/R14, R12/R15, R13/R16), so effective distinct coverage is 15.

**Charts API reference values** for the statistical rows:

- **R08** Vertical rating Tasting 1 — F=1.068966, p=0.446169, **not significant**; Tukey all
  group `a` (P3 8.0, P2 7.5, P1 5.0), N=2 respondents.
- **R09** Vertical rating Tasting 3 — F=2.068493, p=0.272527, **not significant**; Tukey all
  group `a` (P3 20.0, P1 17.5, P2 13.0), N=2.
- **R10** intensity × sensory-experience correlation, N=9 —
  Pearson: P1 −0.61552 (p=0.0776), P2 0.61605 (p=0.0773), P3 −0.21579 (p=0.5771).
  Spearman: P1 **−0.68413 (p=0.0421, significant)**, P2 0.36668 (p=0.3317), P3 −0.11614 (p=0.7660).
- **R13/R16** overall *flavor* liking (Herbalife) — A 6.37, C 6.35, D 6.24; benchmark B 6.10.
- **R17** Gpiexperience — **no overall-liking question exists** (absence case in benchmark form).

### 2.3 Large-output edge cases — 4 cases

Built specifically to force very large SQL results, since ordinary questions never produce them
(largest needle-set result: 5,266 chars). Each demands row-level detail that cannot be aggregated
away. Payloads measured against the DB before writing the cases:

| case | survey | demand | payload |
|---|---|---|---|
| E3 | PepsiCo | per-question × per-product audit | 356 rows / **97 KB** |
| E2 | PepsiCo | respondent-level scores, one question | 1,200 rows / **148 KB** |
| E1 | Herbalife | verbatim open-ended comments | 900 rows / **210 KB** |
| E4 | PepsiCo | respondent-level matrix detail | 33,600 rows / **6.2 MB** |

References: E1 = 900 verbatims (3 products × 2 questions × 150). E3 = 92 product-linked questions,
4 products, 103,222 answers, 1,200 respondents. E4 = 3 product-linked matrix questions,
28 statements, 33,600 rows — of which the two *packaging* matrices are 11 statements / 13,200 rows.

---

## 3. Experiment 1 — three agents, needle set (18 runs)

### 3.1 Accuracy

Scored on the testset's own dimensions; each case normalised to 1.0.

| case | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|
| T1 baseline match | 0.67 | **1.00** | 0.00 |
| T2 83-measure haystack | 0.67 | 0.67 | 0.67 |
| T3 scale trap | 0.25 | **1.00** | 0.25 |
| T4 absence | **1.00** | **1.00** | **1.00** |
| T5 absence + broken join | 0.00 | **0.50** | 0.00 |
| T6 benchmark carve-out | 0.75 | **1.00** | 0.75 |
| **total** | **3.33 / 6 — 56%** | **5.17 / 6 — 86%** | **2.67 / 6 — 44%** |

By dimension (passes / applicable cases):

| dimension | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|
| needle found | 4 / 4 | 4 / 4 | 3 / 4 |
| numbers correct | 3 / 4 | 4 / 4 | 2 / 4 |
| **statistical verdict** | **0 / 4** | **3 / 4** | **0 / 4** |
| scale integrity (T3) | 0 / 1 | 1 / 1 | 0 / 1 |
| honesty on absence | 1 / 2 | 1.5 / 2 | 1 / 2 |
| tenancy (T6) | 1 / 1 | 1 / 1 | 1 / 1 |

### 3.2 Speed and tokens

| agent | wall s | turns | SQL | input | cached | output | $ |
|---|--:|--:|--:|--:|--:|--:|--:|
| `funda_agent` | **125.6** | 27 | 21 | 225,390 | 173,056 | **7,491** | 0.1166 |
| `funda_agent_exp` | 128.0 | **21** | **13** | 208,508 | 157,696 | 8,687 | 0.1206 |
| `funda_agent_v1` | 138.9 | **21** | 14 | **140,932** | 106,880 | 10,408 | 0.1223 |

Per case (wall / turns):

| case | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|
| T1 | 9.8s / 2 | 17.2s / 3 | 15.3s / 2 |
| T2 | 10.4s / 2 | 24.8s / 5 | 13.0s / 2 |
| T3 | 19.0s / 5 | 20.0s / 3 | 21.6s / 4 |
| T4 | 25.1s / 5 | 25.4s / 4 | 28.6s / 4 |
| T5 | 35.0s / 8 | 10.9s / 2 | 27.0s / 4 |
| T6 | 26.4s / 5 | 29.6s / 4 | 33.5s / 5 |

### 3.3 Findings

**Speed and cost are not the differentiator.** All three land within **11% on wall-clock** and
**5% on cost**. Cache hit rates are near-identical (77% / 76% / 76%). Accuracy spans 44–86%.

**All the latency engineering has converged.** Whatever `funda_agent` and `exp` bought with lean
bindings, cache-stable prefixes, ledger caps and the latch, it did not show up as speed against a
bare ReAct loop. What it bought was correctness.

**Nothing mechanical is failing.** Across 18 runs: 0–1 SQL errors per run, no truncation, no
timeouts, and **no tenancy violations** — all 26 distinct UUIDs appearing in SQL resolve to the
case's own survey, its scope ids, or (on T6) the sanctioned benchmark survey. The harness
problems are solved; what remains is judgment.

**T2 does not discriminate.** The 83-measure / 24.5 KB haystack cost nobody more than a normal
case, and all three found the needle. Only `exp` parked a result there.

#### Specific failures

- **`v1` on T1** filtered `prompt ILIKE '%overall%' AND prompt ILIKE '%lik%'`, got four
  candidates, and reported the first row — the **APPEARANCE** question — as overall liking:
  7.12 / 7.06 / 6.89, with Product C above Product A. Both numbers and order wrong. It still
  passed T2, where the needle has no conjunctive keyword overlap, so this is a *selection*
  weakness, not a retrieval one.
- **T3 pooling.** `funda_agent` and `v1` both pooled the two scales into 6.88 / 5.88 / 5.38.
  `funda_agent` noticed the fan-out ("8 recorded values" from 4 respondents) but drew no
  conclusion. Only `exp` detected the split.
- **T5 fabrication.** `funda_agent` and `v1` both printed the ranking codes as products
  (`1. 709  2. 251  3. 928  4. 873  5. 352`). `v1` additionally labelled it "overall liking".
  **`funda_agent`'s trace is damning:** its 5th query returned `name: null, blindingNumber: null`
  and its 6th returned `product_id: null` for every ranking answer — it *observed* the broken join
  and reported the codes as a product ordering anyway. Its mean ranks were numerically correct
  (709 = 1.00, N=6), so the arithmetic was fine and the attribution was invented.
- **`run_survey_stats` was called once in 18 runs** — by `v1` on T6, returning
  `Error (400): No data found for the given question ID`. Every statistical claim in every run is
  hand-rolled prose arithmetic. This is what makes `exp`'s T2 verdict wrong: it claimed Doritos
  separates from Lay's Classic where Tukey says `a`/`ab`.

---

## 4. Experiment 2 — drilling ablation (24 runs)

At their defaults the two drill implementations almost never fire: **1 of 12** measured runs
(`exp` on T2 only). The largest inline result in the whole baseline was 5,266 chars — a quarter of
`funda_agent`'s 20,000 threshold. The baseline was therefore effectively *drill-off for both*, and
the two agents were not even on the same threshold (20,000 vs 8,000). Both confounds are removed
here by pinning each arm explicitly.

| agent | arm | accuracy | wall s | turns | drills | drill turns | input | output |
|---|---|---|--:|--:|--:|--:|--:|--:|
| `funda_agent` | off | 39% | 120.4 | 25 | 0 | 0 | 209,995 | 7,215 |
| `funda_agent` | **on** | **67%** | 156.4 | 40 | 14 | 14 | 271,738 | 11,450 |
| `funda_agent_exp` | off | **87%** | 175.8 | 23 | 0 | 0 | 280,176 | 10,146 |
| `funda_agent_exp` | on | 82% | 203.8 | 41 | 13 | 19 | 378,960 | 16,703 |

All 27 firings succeeded; **zero fell back to the manifest**.

### 4.1 Findings

**Drilling helps the weaker agent and does nothing for the stronger one.** `funda_agent` gains 28
points, entirely from the two absence cases: drill-off it fabricated on both (T4 it averaged the
three intensity attributes into a composite 9.71 and ranked products by it; T5 it printed the
ranking codes again), drill-on it refused correctly on both. Its T5 refusal was the **best answer
any configuration produced on that case**, naming the ranking question *and* that it "is not
linked to product IDs in the same way as the rating questions."

The mechanism is visible in the drill output: the digest hands the model explicit named zero
counts — `n_matching_questions = 0`, `n_liking_answers = 0` — which close the absence question
outright, where raw rows invited another round of probing. The drill also discards raw rows on
success, so the option-label codes both fabrications were built from were unreachable.

`exp`'s 5-point loss is inside run-to-run noise. Its one clear gain is T3: with drilling on it
reported **both** instruments side by side (10.25/9.25/9.00 and 3.50/2.50/1.75, exact) — the only
run in the entire benchmark to fully satisfy that case.

**Cost.** Drilling costs +30% wall / +29% input on `funda_agent`, +16% / +35% on `exp`. The two
drills are not the same price: `funda_agent`'s **workflow** averages **3.2k input tokens per
firing** (1 turn, digest only); `exp`'s **agent** averages **10.6k** (1.5 turns, tool-using) —
**3.3× more expensive**, without proportional accuracy.

### 4.2 A real drill defect — the digest `n` collision

On T2, `funda_agent` with drilling on reported *"Each product had N=1 respondent/value."* True N
is 300, and **the SQL had it right** — it selected `COUNT(score) AS n_values,
COUNT(DISTINCT enrollment_id) AS n_respondents`, both 300.

The corruption is in the digest. For a low-cardinality column `digest_result` emits
`f"{v} (n={n})"` where `n` is the **number of rows carrying that value**
(`output_store.py:247`). On a per-product aggregate that is 1 row per product, so the digest says
`n=1`. The weak drill model read that group-frequency as the sample size, dropped the query's own
`n_respondents` column, and the main loop reported it as fact. The drill-off arm of the same case
got N=300 right.

**This is a naming collision that silently destroys sample sizes.** Renaming the digest's field to
`rows=` would fix it. Still open.

---

## 5. Experiment 3 — pre-fetched inventory (18 runs, `exp`, needle set)

**Observation that motivated it:** every run opened with the same boilerplate inventory query —
id / prompt / type / scale — then a second query for per-product aggregates. That first query is
not a decision, it is a fixed prelude, and the model paid a full strong turn (5.7–7.8 s, 325–470
output tokens) to regenerate it every time.

**DB-wide feasibility**, measured across 132 surveys with product-attributable measures:

| combined inventory+aggregates payload ≤ | surveys |
|---|---|
| 8,000 chars | **128 (97%)** |
| 40,000 chars | 131 |
| 60,000 chars | **132 (100%)** |

Worst case 46,797 chars / 80 measures (the PepsiCo T2 survey).

| variant | accuracy | wall s | turns | SQL | input | output | $ |
|---|---|--:|--:|--:|--:|--:|--:|
| none | 86% | 133.7 | 21 | 13 | 214,201 | 8,962 | 0.1248 |
| prefetch (section A only) | 82% | 74.3 | 8 | 2 | 111,749 | 4,372 | 0.0994 |
| **prefetch + join fix** | **94%** | **66.8** | **7** | **1** | 112,961 | **3,637** | **0.0923** |

Per case (turns, accuracy):

| case | none | prefetch | prefetch_fixed |
|---|---|---|---|
| T1 | 3 turns, 1.00 | 1 turn, 1.00 | 1 turn, 1.00 |
| T2 | 5 turns, 0.67 | 1 turn, 0.67 | 1 turn, 0.67 |
| T3 | 3 turns, 1.00 | 1 turn, 1.00 | 1 turn, 1.00 |
| T4 | 2 turns, 1.00 | 1 turn, 1.00 | 1 turn, 1.00 |
| T5 | 3 turns, 0.50 | 1 turn, 0.50 | 1 turn, **1.00** |
| T6 | 5 turns, 1.00 | 3 turns, 0.75 | 2 turns, 1.00 |

### 5.1 Findings

**Five of six cases answer in a single LLM call.** T6 needs a second only because the benchmark
survey is outside the pre-fetch scope.

**The join gap was real, and fixing it is what produced the only complete T5 answer in the entire
benchmark.** Section A alone (`JOIN product` + numeric `optionAnswer`) silently drops ranking
questions, matrix questions (values live in `optionLabel`) and non-product-linked scales. Under
plain `prefetch` the ranking question is *absent from the payload*, so the model **cannot** name
it — that 0.50 → 1.00 is a fixed defect, not variance. With section B it quoted the exact
mismatch: labels `251, 352, 709, 873, 928` against blinding numbers `944, 551, 815, 601, 332`,
then declined to map them.

**Exposing distractors did not hurt.** `prefetch_fixed` newly surfaces T3's matrix question whose
options are literally labelled `Flavor/Taste Liking`, `Appearance Liking`. The model still picked
the correct vertical-rating needle and still reported both instruments separately.

**What pre-fetch does not fix:** T2's verdict. All three variants still claim Doritos separates
from Lay's Classic.

---

## 6. Experiment 4 — `regression.xlsx` validation (72 runs)

The needle set is entirely measure-selection questions — exactly what pre-fetch optimises for.
This is the overfit check.

| variant | accuracy | wall s | turns | SQL | stats calls | tool errors | input | output | $ |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| none | 43% | 470.6 | 67 | 49 | 0 | 11 | 794,174 | 33,409 | 0.5066 |
| bindings only | 68% | 468.2 | 71 | 47 | 4 | 10 | 845,020 | 31,568 | 0.4799 |
| prefetch only | 75% | 426.0 | 51 | 33 | 0 | 5 | 758,601 | 27,655 | 0.4396 |
| **both** | **96%** | **342.1** | **48** | **23** | 4 | **0** | **743,030** | **21,393** | **0.3816** |

Accuracy = ground-truth figures present in the answer, over the 9 rows with derived references.
`both`'s single miss is a rounding artifact (`F = 2.07` vs reference `2.068`).

By query type (turns / input):

| type (n) | none | both |
|---|---|---|
| Survey metadata (3) | 6 / 46,537 | 4 / 43,039 |
| Historical comparison (4) | 18 / 228,907 | 15 / 181,633 |
| Statistical analysis (3) | 10 / 113,141 | 6 / 80,759 |
| Benchmark comparison (8) | 33 / 405,589 | 23 / **437,599** |

### 6.1 The most serious defect found anywhere: fabricated statistics

On the three rows that **explicitly demand** ANOVA / Tukey / Pearson / Spearman, the shipped agent
called `run_survey_stats` **zero times** — it was unbound — and did not say so. It invented the
numbers instead:

| row | shipped agent | reference (Charts API) |
|---|---|---|
| R08 | `F = 2.138`, no p-value | `F = 1.069, p = 0.446` |
| R09 | `F = 4.137` + **a Tukey grouping table it never computed** | `F = 2.068, p = 0.273` |
| R10 | pooled across products → `r = -0.019`, "no meaningful relationship" | Spearman P1 `ρ = -0.684, p = 0.042` — **significant** |

R09 printed a three-row Tukey letter table having never run Tukey. R10 is the most misleading:
pooling across products cancelled a real +0.62 and a real −0.62 to approximately zero, and that
was reported as evidence of no relationship.

**Root cause.** `model_lean` / `weak_model_lean` bound **only** `nl2sql_tool`, and they are used
whenever nothing has parked — i.e. for the whole of most runs. Corroboration: `v1` has no lean
bindings and is the only sibling that ever called the tool.

### 6.2 Pre-fetch does not generalise on tokens — correction to the needle-set result

On the needle set pre-fetch cut input **47%**. Here it cuts it **6% overall and *increases* it 8%
on benchmark comparisons**, where the payload averages 20,854 chars and the benchmark survey still
needs its own query. **The input-token win was an artifact of the needle set.**

What does generalise: turns −28%, output −36%, wall −27%, SQL −53%, tool errors 11 → 0.

### 6.3 An unadvertised benefit: answer grain

Without pre-fetch the agent frequently answers benchmark comparisons at *survey* level rather than
per product — R11 returned a single pooled mean of 5.80 over N=450; R12 returned 8.15 over
N=1,200 — losing the per-product breakdown the question implies. With per-product statistics
arriving pre-grouped, it reports per product. That mechanism, not luck, is most of the 43% → 75%.

### 6.4 Binding the tool is necessary but not sufficient

Tested directly: with all tools bound from turn 0 and nothing else changed, the agent still made
**0 stats calls** on T1/T2 and still got T2 wrong. It will not elect the tool for a verdict it
thinks it can do in its head. That is what forced the significance gate (§7.3).

---

## 7. The three fixes applied to `funda_agent_exp`

### 7.1 Stats binding (2 lines)

`model_lean` / `weak_model_lean` now bind `run_survey_stats` alongside `nl2sql_tool`. The original
optimisation is preserved — the three result-store tools stay deferred, since they genuinely
cannot act before a park. Cost ~250 input tokens/turn (+6% input).

### 7.2 Pre-fetched inventory + join-gap fix

`survey_inventory(survey_id)` runs one SQL statement before the first LLM call and appends
`products` / `scored_measures_by_product` / `other_answered_measures` to the user turn. The
statement materializes scored answers once, reuses them for both measure summaries, and returns
one transactionally consistent snapshot. It fails soft on DB error, empty survey, or payload over
`INVENTORY_MAX_CHARS` (120,000). **Truncation is
deliberately not an option** — a partial candidate list is the one outcome that silently changes
the answer.

For `other_answered_measures`, `submissions` is the distinct `answer.id` count used for submitted
answer totals. The legacy `answers` field remains the joined option/value-row count, which can be
larger for matrix, ranking, multi-option, and temporal questions and must be labelled at that grain.

### 7.3 Significance gate

A rule appended to the inventory preamble requiring `run_survey_stats` before any separability
claim, using the `qid` already in the payload. **The fallback clause is mandatory, and that was
learned the hard way:** a hard gate made T1 answer "I cannot tell", because the Charts API has no
data for the Herbalife tenant. It now reports the tool failure and gives a means/SD read labelled
approximate and uncorrected.

### 7.4 Post-fix verification

| case | tools called | outcome |
|---|---|---|
| T1 | `run_survey_stats` | API has no data → reported, labelled approximate verdict ✓ |
| T2 | `run_survey_stats` | **Tukey-correct**: F=5.87, p=0.000564, "not significantly different from Lay's Classic" ✓ |
| T3 | `run_survey_stats` ×2 | ANOVA/Tukey on **both** instruments (p=0.955, p=0.244) ✓ |
| T4 | none | correct refusal; gate does not over-fire ✓ |
| T5 | none | full pass — ranking named, label mismatch quoted ✓ |
| T6 | `run_survey_stats`, `nl2sql_tool` ×2 | benchmark comparison, labelled approximate ✓ |
| R08 | `run_survey_stats` | F=1.068966, p=0.446169 — exact ✓ |

---

## 8. Experiment 5 — patched `exp` vs `v1` head-to-head (6 runs)

T1, T2, T5 — chosen because between them they exercise all three fixes.

| case | `v1` | patched `exp` |
|---|---|---|
| T1 | **0.00** — 15 rows mixing every like/dislike question × product | **1.00** |
| T2 | **0.67** — needle and numbers right, no verdict | **1.00** — exact Tukey `a/ab/b/b` |
| T5 | **0.00** — fabricated "Product 709, Product 251…" | **1.00** |
| **total** | **0.67 / 3 — 22%** | **3.00 / 3 — 100%** |

| agent | wall s | turns | SQL | input | cached | **uncached** | output | $ |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| `v1` | 52.2 | 11 | 8 | 59,233 | 47,872 | **11,361** | 4,316 | 0.0370 |
| patched `exp` | **34.4** | **5** | **0** | 113,115 | 84,480 | 28,635 | **1,536** | 0.0617 |

**3× the accuracy and 34% faster, for 1.7× the cost** — not a free win. The cost is concentrated:
$0.0439 of exp's $0.0617 is T2 alone, whose 57 KB inventory is the largest in the database.
Excluding T2, exp costs $0.0178 against v1's $0.0235 — cheaper *and* faster *and* correct.

---

## 9. Experiment 6 — large SQL output, drilling on vs off (8 runs)

| case | arm | wall s | turns | parked | max tool out | input | output | outcome |
|---|---|--:|--:|--:|--:|--:|--:|---|
| E1 | off | 16.6 | 2 | 0 | 209,808 | 73,098 | 1,541 | answered, real verbatims |
| E1 | **on** | 33.0 | 5 | 1 | 16,914 | 65,022 | 3,937 | **50 of 900 rows, rest discarded** |
| E2 | off | 14.4 | 2 | 0 | 133,893 | 110,354 | 793 | answered |
| E2 | on | 80.5 | 10 | 2 | 5,693 | 340,144 | 9,257 | answered, 250 rows |
| E3 | off | 30.1 | 2 | 0 | 163,670 | 109,307 | 3,851 | answered |
| E3 | on | 79.0 | 6 | 2 | 26,996 | 232,002 | 12,542 | answered |
| E4 | off | 26.8 | 2 | 0 | **6,525,147** | 36,426 | 515 | **`context_length_exceeded` — no answer** |
| E4 | **on** | 38.0 | 4 | 1 | 22,888 | 144,813 | 3,107 | **answered correctly** |
| **total** | off | **87.8** | **8** | 0 | | **329,185** | **6,700** | 3 of 4 |
| **total** | on | 230.6 | 25 | 6 | | 781,981 | 28,843 | 4 of 4 |

Accuracy: **off 61%, on 70%**. Excluding E4, which `drill_off` could not complete at all, the
three completable cases score **off 82% vs on 60%**.

### 9.1 Finding — drilling has a threshold, and the old default sat far below it

- **Below the context ceiling it is a net loss.** E1/E2/E3 inlined fine at 97–210 KB; drilling
  made them 2–5.6× slower for 2.4× input and 4.3× output. On E1 it *destroyed the answer* — the
  retrieval agent surfaced 50 of 900 verbatim rows, all one product and one question, then
  discarded the raw rows, making them unrecoverable. Same evidence-destruction failure the
  `choice_set` bypass exists to prevent, now measured on the drill's own output.
- **Above the ceiling it is the only thing that works.** E4's 6.5 MB result produced a hard
  `OpenAIContextOverflowError` with drilling off.

*Caveat:* the two E4 arms wrote different SQL (6.5 MB vs 13,200 rows), so the crash reflects the
model's broader query as well as the arm. The mechanism holds regardless — under `drill_on` a
6.5 MB payload parks rather than inlining.

Also noted: `exp`'s E4 answer was **better than the initial reference** — it read "matrix packaging
statements" as the two packaging matrices (11 statements, 13,200 rows) and correctly excluded the
unrelated 17-statement matrix. The reference had lumped all three together.

### 9.2 Applied consequence — overflow guard + `ENABLE_DIGEST` (3 verification runs)

`TOOL_CHAR_LIMIT` raised **8,000 → 250,000**, and `ENABLE_DIGEST` added to select the condenser.

| run | payload | routing | outcome |
|---|---|---|---|
| E1, defaults | 246,024 chars | **INLINE**, condenser not fired | full verbatims (was: 50 of 900) |
| E4, `ENABLE_DIGEST=0` | 896,124 chars | PARKED → drill agent, 3 turns, 13.9 s | answered, 31.7 s / 5 turns |
| E4, `ENABLE_DIGEST=1` | 5,303,724 chars | PARKED → digest, 1 turn, 4.6 s | answered, 23.7 s / 3 turns |

**250,000 is not bisected.** 209,808 chars demonstrably inlines and 6.5 MB is fatal — a 31× band.
The threshold also ignores how many turns carry the payload afterwards. The two E4 condenser runs
wrote different SQL, so digest-vs-agent is **not** a clean A/B yet.

---

## 10. Prompt-cache measurements

**Pre-fix** (`exp`, per turn): strong turn 0 reached **98% cached** (10,752 / 10,963) once warm;
subsequent strong turns 83–92%. Prompt sizes: full **9,399 tokens**, lean **2,489**, schema block
**6,910**.

**Post-fix** (inventory payload now in the user turn):

| turn | input | cached | rate |
|---|--:|--:|--:|
| T1 t0 | 13,474 | 10,752 | 79% |
| T3 t0 | 12,730 | 9,216 | 72% |
| T3 t1 | 13,590 | 12,288 | 90% |
| T5 t0 | 12,259 | 11,776 | 96% |

**The cacheable prefix still caches at ~100%.** The lower rate is arithmetic — the per-survey
inventory is inherently uncacheable and grows the denominator.

### 10.1 A documented saving that does not exist

The comment on the `SYSTEM_PROMPT_LEAN` assertion claims the prose turn's system block is covered
by the SQL turn's cache. **It is not:** the prose turn runs on `gpt-5.4-mini` while SQL turns run
on `gpt-5.5`, and prompt caches are per-model. The first weak turn in a process measured
`cached = 0`; later weak turns reached only ~3,584 tokens (46–55%) against strong's 98%.
Schema-last is still correct — it keeps *consecutive strong* turns cached — just not for the
stated reason.

### 10.2 The latch's saving comes from the schema, not the tier

The latch flips `sql_done` once and cuts **both** the model tier and the schema block. A model
swap changes **zero** input tokens — token count is a function of the text sent, not the model
receiving it. The measured strong→weak input drops match schema removal alone:

| case | last strong turn | first weak turn | drop |
|---|--:|--:|--:|
| T1 | 11,957 | 5,709 | −6,248 |
| T3 | 12,113 | 6,488 | −5,625 |
| T4 | 12,667 | 6,789 | −5,878 |
| T6 | 13,486 | 7,693 | −5,793 |

Schema removal is −6,910; history grows ~1,000 between those turns; net ≈ −5,900. Matches every
row. The tier change buys price-per-token and generation speed, not input volume.

Also: **the tier is not what drives accuracy.** `exp` writes its final answer on the weak model in
5 of 6 cases and gets 3/4 verdicts right; `funda_agent` does the same and gets 0/4. Same routing,
same YAML config — the difference is prompt content.

Incidentally, with pre-fetch most runs make no SQL call, so `sql_done` never flips and the latch
never fires. The cross-model cache split has largely stopped costing anything.

---

## 10.3 Experiment 7 — short-term memory, cost against the pre-memory build (36 runs)

`funda_agent_exp.py` gained multi-turn memory: an `InMemorySaver` checkpointer keyed by
`--thread-id`, plus `_trim_history`, which sends message 0 + the round in progress in full + at
most `HISTORY_MAX_MESSAGES` (default 10) messages of prior rounds. `funda_agent_exp_oracle_duo.py`
is the untouched pre-memory build and is the control throughout.
Harness `scripts/bench_memory.py`; records in `needle_haystack_results/memory/`.

### 10.3.1 A defect the message-count data caught before it shipped

The first implementation trimmed to "message 0 + last N" with no regard for round boundaries.
`MAX_LLM_STEPS` is 12, so one question can exceed N by itself. Across the 101 recorded runs
carrying turn/tool counts, **7 exceeded 11 messages and one reached 21** — those runs would have
been trimmed *mid-question*, answering from less evidence than the pre-memory build. The window is
now clamped so it can never cut into the round in progress. Verified exhaustively: for any
single-question history up to 22 messages, at every N from 1 to 24, `_trim_history` is the
identity function, so a single question is assembled byte-for-byte as before.

### 10.3.2 Overhead, isolated from LLM noise

| source | cost |
|---|---|
| checkpointer | **+0.32 ms per superstep** (+3.9 ms over a 12-turn run) |
| `_trim_history` | **≤6.5 µs** per call (120-message thread: 2.5 µs) |
| one LLM turn, for scale | 5,760–25,000 ms |

### 10.3.3 A/B against the pre-memory build

16 single-turn + 12 multi-turn paired runs, alternating which build goes first.

| question | build | order | n | wall median | input tokens |
|---|---|---|---|---|---|
| single-turn | oracle | oracle-first | 4 | 11.46 s | 12,329 |
| single-turn | patched | oracle-first | 4 | 6.58 s | 12,329 |
| single-turn | patched | patched-first | 4 | 7.40 s | 12,329 |
| single-turn | oracle | patched-first | 4 | 7.13 s | 12,329 |
| multi-turn (2 turns) | oracle | both | 6 | 12.6–13.1 s | 15,525–15,543 |
| multi-turn (2 turns) | patched | both | 6 | 10.9–12.8 s | 15,525–15,533 |

**Input tokens are identical**, which is the direct evidence that prompt assembly is unchanged.
Wall clock tracks **run order, not build**: whichever runs second inherits the warm prompt cache
and wins, and reversing the order reverses the winner. The within-build noise floor is
2.57–13.88 s (5.4×) on byte-identical input, so no wall difference here is attributable to the
patch. The ±18-token multi-turn spread is the model writing slightly different SQL.

### 10.3.4 Multi-round behaviour (12 rounds, one thread, ×2)

| round | messages sent | messages in state | input tokens | trimmed |
|---|---|---|---|---|
| 1 | 2 | 2 | 12,329 | — |
| 6 | 12 | 12 | 12,614 | — |
| 7 | 12 | 14 | 12,612 | yes |
| 12 | 12 | 24 | 12,545 | yes |

Messages sent plateau at 12 while state doubles to 24; input rises **+1.8% across 12 rounds**.
Both reps identical in shape. Every figure reported was correct against the database, and anaphora
resolved *across* the trim boundary ("What was the Texture mean for that same product?" → Product
3, the referent from round 6).

**But round 12 shows the cost of a positional window, reproducibly.** Asked to "summarise, in one
line each, every figure you have given me so far", both reps returned **only rounds 7–11** and
silently dropped rounds 1–6 — the product list, the per-product Aroma means (9.19 / 8.55 / 8.45
with SDs 3.02 / 2.54 / 3.64), and the SD answer from round 4. The omission boundary is exactly the
trim window: at round 12 the request holds message 0 plus roughly rounds 7–11.

Nothing was fabricated — every figure it did give was right — but the answer was **incomplete and
did not say so**. That is the honest shape of this design: `_trim_history` retains by *position*,
not by relevance, and the model cannot report the absence of context it was never sent. A question
that reaches back past the window returns a confidently partial answer. Raising
`HISTORY_MAX_MESSAGES` moves the boundary; it does not remove it. Summarisation would, and was
explicitly out of scope.

### 10.3.5 What the trim is worth, and what memory is worth

Same 12 rounds with `HISTORY_MAX_MESSAGES=0`: **151,562 input tokens vs 150,174** trimmed —
**−0.9%**, widening to −2.9% by round 12. Small, because message 0 (the ~12 KB inventory) is
always kept and dominates every request. The trim's value is the guarantee that a request cannot
grow without limit, not a large saving on short chat turns.

Memory itself is where the win is. The same six questions without it (fresh thread each, so every
round re-sends scope + inventory and re-runs SQL):

| | input tokens | wall | failures |
|---|---|---|---|
| with memory | 74,936 | 42.9 s | 0 |
| without | 112,646 | 96.1 s | round 4 unanswerable |
| delta | **−33%** | **−55%** | — |

Round 4 without memory: *"I don't have the earlier referent for 'its' in this conversation."*

### 10.3.6 Does memory survive a condenser firing mid-conversation?

Both condensers rewrite what the main loop sees, so they were tested against memory directly:
`scripts/bench_memory_drill.py`, `TOOL_CHAR_LIMIT=2000` to force a park every round, 3 rounds
where rounds 2–3 are deliberately elliptical, run once per condenser.

**Message shape: unaffected, structurally.** `run_drill` returns a `str` that *replaces* the tool
result and becomes the content of the single `ToolMessage` `call_tool` was appending anyway.
Neither condenser appends a message, and neither injects a `HumanMessage` — the thing
`_trim_history` uses to locate the round boundary.

| condenser | drills | r1 | r2 | r3 | shape OK | current round intact | orphan tool |
|---|---:|---|---|---|---|---|---|
| drill agent | 4 | 1 drill | 3 drills | 0 | 3/3 rounds | 3/3 | never |
| digest workflow | 2 | 2 drills | 0 | 0 | 3/3 rounds | 3/3 | never |

Human-message count equalled the round number in every round of both runs. Round 3 of the
drill-agent run is the load-bearing case: 18 state messages trimmed to a 12-message window that
dropped part of round 2 while keeping message 0 and all of round 3, with a valid leading pair.

**One real gap — the drill's question degrades in follow-up rounds.** `call_tool` passes
`_last_user_query(state["messages"])`, the *last* `HumanMessage`, and the drill's context is
isolated by design (no main-loop history). Measured:

| drill | round | chars handed to the drill | content |
|---|---|---:|---|
| 1 | 1 | 4,186 | full scoped preamble + question |
| 2–4 | 2 | **53** | *"Now do the same for Texture, for those same products."* |

The main loop resolves "those same products" from history; the drill cannot. It is not blind — it
also gets the `sql_query` (naming the resolved question id) and the analyst's ledger notes, both
written by a model that did have the history — but the question itself is elliptical, and this is
new with multi-turn.

**Not yet demonstrable as harm.** The drill-agent's round 2 (3 drills on the 53-char question)
answered poorly; the digest workflow's round 2 (0 drills — both landed in round 1) answered well.
Consistent with the mechanism, but confounded by condenser identity, n=1, and a
`TOOL_CHAR_LIMIT` of 2,000 that §9 already showed is harmful on its own. At the shipped 250,000 a
follow-up must return a very large result to drill at all. The one-line fix, if wanted, is to hand
the drill the round-1 question alongside the follow-up — measure at a realistic limit first.

### 10.3.7 Caveats

1. **`InMemorySaver` retention is quadratic in rounds** on one thread — it snapshots the whole
   state every superstep and the state grows each round. With a 30 KB message 0: 0.4 MB after 1
   round, 24 MB after 10, **92 MB after 20**. `_trim_history` does not help; it bounds the
   request, not the state. Acceptable for a CLI session; a long-lived server needs a checkpointer
   with eviction.
2. **`PARKED_RESULT_IDS` persists across rounds** in a `--chat` session, so a round following a
   park opens on the full tool bindings (~250 input tokens/turn) rather than the lean ones.
   Deliberate — the parked rows are still in the store — and single-shot runs are unaffected.
3. n=4 per A/B cell, n=2 per multi-round cell, one conversation script, one small survey. The
   token identity is exact and reproducible; the wall-clock comparisons are noise-bound.

---

## 11. Consolidated conclusions

1. **Accuracy, not speed, separates these agents.** Baseline: 86% / 56% / 44% within 11% wall and
   5% cost. Latency engineering has converged; further cache/latch work is low-yield.
2. **The worst defect is invented statistics.** With `run_survey_stats` unreachable, the agent
   fabricated F-statistics, a whole Tukey table, and a "no relationship" conclusion that was the
   opposite of the truth. Fixed by 2 lines + a gate.
3. **Statistics must be forced, not offered.** Binding the tool was necessary but insufficient —
   the model will not elect it for a verdict it believes it can estimate.
4. **Pre-fetching the survey inventory is the highest-yield structural change**: 21 → 7 turns on
   the needle set, 67 → 48 on regression, accuracy 43% → 96% combined with the binding fix. Its
   token saving does *not* generalise; its turn/error/grain benefits do.
5. **An inventory or digest that hides evidence is worse than none.** **Four** independent
   instances: the `choice_set` bypass, the pre-fetch join gap, the drill surfacing 50 of 900 rows,
   and — found 2026-08-04 by reviewing a live `--chat` transcript — inventory section B's INNER
   join to `answered_question_options`, which made **every open-answer question in the database
   invisible** (an open-answer has an `answer` row but no option row). On survey `39af3240` two
   free-text questions with 16 answers each never appeared, so the agent presented three
   demographics as the complete set of other questions. Fixed to `LEFT JOIN`: 150 of 1,347 surveys
   gain questions, 17 of them from an entirely empty list, at +22% on that section. This failure
   mode keeps recurring because it is silent by construction — nothing downstream can distinguish
   a filtered-out measure from one that does not exist, so it is only ever caught by checking the
   inventory against the raw table.
6. **Drilling is insurance, not compression.** Below the context ceiling it costs 2–5.6× and can
   destroy data; above it, it is the only thing that answers at all.
7. **The hand-rolled separability rule is wrong where it matters.** It ignores multiple-comparison
   correction and misjudges exactly the near-miss cases a verdict is asked about.

### 11.1 Open items

1. **Credentials hardcoded** in `funda_agent_exp.py` (lines 55–60) — OpenAI key unconditional.
   Rotate and move to environment before any shared deployment.
2. **Charts API has no data for some surveys** (Herbalife), so statistics degrade to the labelled
   approximate fallback there. Worth auditing coverage.
3. **The significance gate ships only with the inventory** — `--no-inventory` and oversized
   surveys lose it.
4. **Digest `n` collision** (`output_store.py:247`) — group frequency read as sample size. Still
   unfixed.
5. **Drill discards raw rows even on partial coverage**, which is what made E1 unrecoverable. Less
   likely to bite at the raised limit, but still live above 250,000.
6. **`TOOL_CHAR_LIMIT = 250000` is un-bisected** (209,808 safe, 6.5 MB fatal).
7. **Digest vs drill agent has not been A/B'd** on large payloads — `ENABLE_DIGEST` exists to make
   that a one-line experiment.
8. **The 72-run regression sweep predates the significance gate.**
9. **Pre-fetch is paid on every question.** Gating it on measure count (~40) would remove most of
   the token cost; untested.

---

## 12. Run inventory and reproduction

| set | runs | location |
|---|---:|---|
| E1 baseline, 3 agents × 6 needle cases | 18 | `needle_haystack_results/*.json` |
| E2 drilling ablation, 2 agents × 6 × 2 arms | 24 | `needle_haystack_results/drill_ablation/` |
| E3 pre-fetch ablation, 6 × 3 variants | 18 | `needle_haystack_results/prefetch/` |
| E4 regression.xlsx, 18 rows × 4 variants | 72 | `needle_haystack_results/regression_exp/` |
| E5 head-to-head, 3 cases × 2 agents | 6 | `needle_haystack_results/head2head/` |
| E6 large output, 4 cases × 2 arms | 8 | `needle_haystack_results/bigoutput/` |
| E7 overflow guard verification | 3 | `needle_haystack_results/overflow_guard/` |
| **total** | **149** | |

Each record holds wall time, per-turn token and latency detail, every SQL statement, tool outputs,
drill detail, the final answer, and the full message transcript.

```bash
bash scripts/run_needle_bench.sh      # 3 agents x 6 needle cases
bash scripts/run_drill_ablation.sh    # 2 agents x 6 cases x {drill_off, drill_on}
bash scripts/run_prefetch_bench.sh    # exp: 6 cases x {none, prefetch, prefetch_fixed}
bash scripts/run_regression_exp.sh    # exp: 18 rows x {none, bindings, prefetch_fixed, both}
bash scripts/run_bigoutput_bench.sh   # 4 large-output cases x {drill_off, drill_on}
```

**Related documents:** `needle_haystack_testset.md` (test definitions and ground truth),
`needle_haystack_benchmark.md` (per-case write-up), `agent_exp_doc.md` (architecture reference for
`funda_agent_exp.py`).
