# Needle-in-a-haystack benchmark — `funda_agent` vs `funda_agent_exp` vs `funda_agent_v1`

Six cases from `needle_haystack_testset.md` (T1–T6), one run per agent per case, 18 runs total.

**Headline: speed is not the differentiator — accuracy is.** All three agents finish within 11%
of each other on mean wall-clock (20.9 / 21.3 / 23.2 s) and within 5% on estimated cost, but
accuracy spans 44% to 86%. `funda_agent_exp` is the only variant that reliably reports a
*statistical verdict* rather than a bare ranking, and the only one that survives the T3 scale trap.

---

## Method

`scripts/bench_needle.py` patches `ChatOpenAI.invoke` and `BaseTool.invoke` at **class level**, as
the testset prescribes. That gives one definition of "a turn" across all three variants and
captures every LLM call on every path — main loop, the drill sub-agent, and any variant-specific
node — which the per-module `TURN_LOG`s do not agree on. Verified against `funda_agent_v1`'s own
`[route]` log: identical turn counts.

Each (agent, case) pair runs in a **fresh process**, so `PARKED_RESULT_IDS`, the in-process result
store and module state cannot leak between cases. Per case, the three agents run **concurrently**,
so every variant sees the same API conditions on the same case; within an agent, case order is
fixed, so each gets the same cold→warm prompt-cache pattern.

Two notes on the testset's own preamble:

- The "Before you benchmark" warning is **stale** — all three files already carry the same working
  key (`…3lES7uaGAA`). No sync was needed and none was done.
- Ground truth was re-verified directly against the docker Postgres DB before scoring: T1 means
  (5.95 / 5.86 / 5.57, SD 2.2, N=150), T3's two instruments (10.25/9.25/9.00 and 3.50/2.50/1.75,
  N=4), T6's benchmark (Product B, 6.00, SD 2.11, N=150), and T5's label mismatch (ranked labels
  `251/352/709/873/928` vs blinding numbers `332/551/601/815/944` — zero overlap). All matched.

---

## Accuracy

Scored on the testset's own dimensions. 1 = pass, 0.5 = partial, 0 = fail; each case is normalised
to 1.0 and the parenthesised digits are the per-dimension scores in the order listed in the
dimension table below.

| case | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|
| T1 baseline semantic match | 0.67 | **1.00** | 0.00 |
| T2 83-measure haystack | 0.67 | 0.67 | 0.67 |
| T3 scale trap | 0.25 | **1.00** | 0.25 |
| T4 absence | **1.00** | **1.00** | **1.00** |
| T5 absence + broken join | 0.00 | **0.50** | 0.00 |
| T6 benchmark carve-out | 0.75 | **1.00** | 0.75 |
| **total** | **3.33 / 6 — 56%** | **5.17 / 6 — 86%** | **2.67 / 6 — 44%** |

### By dimension (passes / applicable cases)

| dimension | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|
| needle found | 4 / 4 | 4 / 4 | 3 / 4 |
| numbers correct | 3 / 4 | 4 / 4 | 2 / 4 |
| **statistical verdict** | **0 / 4** | **3 / 4** | **0 / 4** |
| scale integrity (T3) | 0 / 1 | 1 / 1 | 0 / 1 |
| honesty on absence (T4/T5) | 1 / 2 | 1.5 / 2 | 1 / 2 |
| tenancy (T6) | 1 / 1 | 1 / 1 | 1 / 1 |

---

## Speed and tokens

| metric | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|
| wall-clock total (s) | **125.6** | 128.0 | 138.9 |
| wall-clock mean (s) | **20.9** | 21.3 | 23.2 |
| wall-clock median (s) | **19.0** | 20.0 | 21.6 |
| LLM turns | 27 | **21** | **21** |
| SQL calls | 21 | **13** | 14 |
| input tokens | 225,390 | 208,508 | **140,932** |
| — cached | 173,056 | 157,696 | 106,880 |
| — uncached (billed full) | 52,334 | 50,812 | **34,052** |
| output tokens | **7,491** | 8,687 | 10,408 |
| — reasoning | **961** | 1,636 | 2,070 |

Cache hit rate is high and similar for all three (77% / 76% / 76%), so the stable-prefix design is
working in every variant. At list rates of $1.25/$10.00 per MTok for the strong model and
$0.25/$2.00 for the mini (cached input at 10%), the six cases cost **$0.117 / $0.121 / $0.122** —
a 5% spread. **Cost is effectively identical across the three; only accuracy differs.**

### Per case

| case | metric | `funda_agent` | `funda_agent_exp` | `funda_agent_v1` |
|---|---|---|---|---|
| T1 | wall s / turns / SQL | 9.8 / 2 / 1 | 17.2 / 3 / 2 | 15.3 / 2 / 1 |
| T1 | in (cached) / out | 14,345 (0) / 542 | 28,631 (10,752) / 1,088 | 13,088 (0) / 1,051 |
| T2 | wall s / turns / SQL | 10.4 / 2 / 1 | 24.8 / 5 / 2 | 13.0 / 2 / 1 |
| T2 | in (cached) / out | 14,618 (12,288) / 460 | 41,626 (29,184) / 1,732 | 13,019 (10,496) / 988 |
| T3 | wall s / turns / SQL | 19.0 / 5 / 4 | 20.0 / 3 / 2 | 21.6 / 4 / 3 |
| T3 | in (cached) / out | 40,065 (35,328) / 1,085 | 29,564 (23,552) / 1,518 | 27,209 (22,016) / 1,983 |
| T4 | wall s / turns / SQL | 25.1 / 5 / 4 | 25.4 / 4 / 3 | 28.6 / 4 / 3 |
| T4 | in (cached) / out | 47,476 (43,008) / 1,161 | 42,087 (36,352) / 1,715 | 32,485 (29,184) / 1,743 |
| T5 | wall s / turns / SQL | **35.0 / 8 / 7** | 10.9 / 2 / 1 | 27.0 / 4 / 3 |
| T5 | in (cached) / out | 71,864 (61,952) / 1,952 | 22,585 (21,504) / 571 | 26,003 (20,480) / 1,882 |
| T6 | wall s / turns / SQL | 26.4 / 5 / 4 | 29.6 / 4 / 3 | 33.5 / 5 / 3 |
| T6 | in (cached) / out | 37,022 (20,480) / 2,291 | 44,015 (36,352) / 2,063 | 29,128 (24,704) / 2,761 |

T2 — the 24.5 KB / 83-measure haystack — cost nobody more than a normal case. Only
`funda_agent_exp` parked a result (1 park, drill fired, then a `query_result` call); the other two
aggregated in SQL and never came close to the limit. No agent truncated the inventory, and all
three found the needle. **T2 does not discriminate between these variants.**

T5 is where the cost blows out for `funda_agent`: 8 turns and 7 SQL calls chasing a
question that does not exist, 71.9k input tokens — 3.2× `funda_agent_exp` on the same case — and
it still answered wrongly.

---

## What actually separates them

### 1. Nobody runs the statistics (the single biggest gap)

`run_survey_stats` — the ANOVA/Tukey tool — is bound in all three agents. Across all 18 runs it was
called **once**: `funda_agent_v1` on T6, where it returned `Error (400): No data found for the
given question ID`. Every statistical claim in every run is therefore a hand-rolled
`2·SD·√(2/N)`-style threshold, computed by the model in prose.

That is exactly the trap the testset flags on T2. `funda_agent_exp` did produce a verdict there and
got the headline pair **wrong**: it claimed Doritos separates from Lay's Classic (gap 0.18 > its
noise threshold), where Tukey assigns Doritos `a` and Lay's Classic `ab` — *not* separable once the
6-way multiple-comparison correction is applied. It got the rest right (the middle three
indistinguishable). `funda_agent` and `funda_agent_v1` gave no verdict at all on any case.

**This is a shared architectural gap, not a variant difference — fixing the stats-tool path lifts
all three.**

### 2. T5: two of three fabricated a product ordering

`funda_agent` and `funda_agent_v1` both answered T5 by presenting the raw ranking codes as products:

> 1. 709  2. 251  3. 928  4. 873  5. 352

`funda_agent_v1` went further and labelled it "overall liking as measured by the preference ranking
question" — the ranking is a preference rank, not a liking score, and the labels are not products.

The damning detail is in `funda_agent`'s trace: its 5th query returned `name: null,
blindingNumber: null` and its 6th returned `product_id: null` for every ranking answer. **It
observed the broken join directly and then reported the codes as a product ordering anyway.** It
also got the mean ranks numerically right (709 = 1.00, N=6), so the arithmetic was fine and the
attribution was invented.

`funda_agent_exp` refused correctly and named the attribute questions, but never surfaced the
ranking question at all — so it misses the second half of the expected answer ("the ranking exists
but cannot be linked to products"). Partial credit, and the only variant not to fabricate.

### 3. T3: only `funda_agent_exp` noticed there were two instruments

`funda_agent` and `funda_agent_v1` both pooled the two scales and reported 6.88 / 5.88 / 5.38 —
the exact pooled figures the testset says to score as a **failure**, because the magnitudes have no
unit. `funda_agent` did notice the fan-out ("8 recorded values" from 4 respondents) but drew no
conclusion from it.

`funda_agent_exp` detected the split, reported the 0–4 instrument separately (3.50 / 2.50 / 1.75,
exact), named the 0–14 instrument as a separate measure, and called out N=4 and the
within-noise verdict.

### 4. T1: `funda_agent_v1` fell into the distractor

`funda_agent_v1` filtered with `prompt ILIKE '%overall%' AND prompt ILIKE '%lik%'`, got four
candidate questions back, and reported the first row — the **APPEARANCE** question — as overall
liking: 7.12 / 7.06 / 6.89, with Product C ranked above Product A. Both the numbers and the order
are wrong. This is the pure keyword-search failure the case was built to catch.

Notably `funda_agent_v1` still passed T2, where the needle has *no* conjunctive keyword overlap —
so the T1 failure is a ranking/selection weakness, not a retrieval one.

### 5. Tenancy is clean everywhere

All 26 distinct UUIDs appearing in SQL across all 18 runs resolve to the case's own survey, its
scope ids, or (on T6) the sanctioned benchmark survey and its question. **No cross-tenant reads by
any agent on any case.**

### 6. T4 is the one case all three pass

All three refused honestly and named the Appearance/Aroma/Texture intensity scales. `funda_agent_v1`
gave the best refusal — it also surfaced the two open-text like/dislike items. `funda_agent_exp`
refused but then computed and displayed the full intensity rankings anyway (clearly captioned as
"not overall liking"), which is honest but is extra work adjacent to the failure mode.

---

## Recommendation

**Ship `funda_agent_exp`.** It wins accuracy 86% vs 56% vs 44% at a 2% wall-clock and 4% cost
premium over `funda_agent` — the difference is free in every dimension that isn't correctness. It
is also the only variant that does not fabricate on T5 and the only one that survives T3.

`funda_agent_v1` is the cheapest on input tokens in this run (−37% vs `funda_agent`) but is the
slowest and least accurate; its keyword-conjunction question selection is the specific defect.
Note the token edge is not solid — see the variance caveat below.

Highest-value fix for whichever variant ships, in order:

1. **Make `run_survey_stats` actually fire** on ranking/comparison prompts. It is 0-for-18 today,
   and it is the only path to a Tukey-correct verdict. The one call that was attempted failed with
   a 400 — worth checking whether the question-id/plumbing works at all before assuming the model
   simply isn't choosing it.
2. **Guard the ranking→product join.** When `product_id IS NULL` and option labels do not match any
   `blindingNumber`, the answer must say so rather than print the labels. Two of three agents
   fabricated here with the evidence in front of them.
3. **Detect duplicate prompts with different scales** before aggregating (T3). Two of three pooled.

---

## Appendix: drilling ablation

### Did drilling fire in the baseline runs? Almost never — 1 of 12.

| agent | drill design | default threshold | fired in baseline |
|---|---|---|---|
| `funda_agent` | **workflow** — one weak-model turn, no tools, sees only a Python-computed digest | `TOOL_CHAR_LIMIT=20000` | **0 / 6** |
| `funda_agent_exp` | **agent** — weak model with 4 result-store tools, up to `DRILL_MAX_STEPS=4` turns | `TOOL_CHAR_LIMIT=8000` + two shape bypasses | **1 / 6** (T2 only) |

Largest inline SQL result in the whole baseline was 5,266 chars — a quarter of `funda_agent`'s
park threshold. So the baseline comparison was effectively **drill-off for both agents**, and the
two agents were not even on the same threshold (20,000 vs 8,000). Both confounds are removed below.

### Ablation setup

2 agents × 6 cases × 2 arms = **24 runs** (`scripts/run_drill_ablation.sh`, results in
`needle_haystack_results/drill_ablation/`). Arms are pinned explicitly rather than left at defaults:

- **drill_off** — all limits set to 10⁸, so nothing can ever park; the drill is unreachable.
- **drill_on** — `TOOL_CHAR_LIMIT=0`, and for `funda_agent_exp` also `SMALL_RESULT_ROWS=0`,
  `SMALL_RESULT_CHARS=0`, `CANDIDATE_LIST_CHARS=0`, because its inline test is
  `chars <= TOOL_CHAR_LIMIT or small_shape or choice_set` — the char limit alone does not force it.

The harness now wraps each module's `run_drill`, so drill firings, their turns and their tokens are
counted separately from the main loop. **All 27 firings succeeded — zero fell back to the manifest.**

### Cost of drilling

| agent | arm | wall s | LLM turns | drills | drill turns | input | output | drill input | drill output |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `funda_agent` | off | 120.4 | 25 | 0 | 0 | 209,995 | 7,215 | — | — |
| `funda_agent` | **on** | 156.4 | 40 | 14 | 14 | 271,738 | 11,450 | 44,512 | 3,477 |
| `funda_agent_exp` | off | 175.8 | 23 | 0 | 0 | 280,176 | 10,146 | — | — |
| `funda_agent_exp` | **on** | 203.8 | 41 | 13 | 19 | 378,960 | 16,703 | 138,279 | 6,784 |

Drilling costs **+30% wall / +29% input** for `funda_agent` and **+16% wall / +35% input** for
`funda_agent_exp`. The two drills are not the same price: `funda_agent`'s workflow averages
**3.2k input tokens per firing** (1 turn, digest only), `funda_agent_exp`'s agent averages
**10.6k per firing** (1.5 turns, tool-using) — **3.3× more expensive per drill**.

Note this is drilling applied to results that never needed it — the largest was ~5 KB. It is a
measurement of the drill's overhead, not of its compression benefit.

### Accuracy: drilling helps the weaker agent and slightly hurts the stronger one

| case | `funda_agent` off → on | `funda_agent_exp` off → on |
|---|---|---|
| T1 | 0.67 → 0.67 | 1.00 → 1.00 |
| T2 | 0.67 → **0.33** ▼ | 0.83 → **0.67** ▼ |
| T3 | 0.25 → 0.25 | 0.88 → **1.00** ▲ |
| T4 | 0.00 → **1.00** ▲▲ | 1.00 → 1.00 |
| T5 | 0.00 → **1.00** ▲▲ | 0.50 → 0.50 |
| T6 | 0.75 → 0.75 | 1.00 → **0.75** ▼ |
| **total** | **2.34 (39%) → 4.00 (67%)** | **5.21 (87%) → 4.92 (82%)** |

**`funda_agent` gains 28 points, driven entirely by the two absence cases.** With drilling off it
fabricated on both: on T4 it averaged the three intensity attributes into a composite 9.71 and
ranked products by it, and on T5 it again printed the ranking codes as products. With drilling on
it refused correctly on both — and its T5 refusal is the **best answer any configuration produced
on that case**, naming the ranking question *and* that it "is not linked to product IDs in the same
way as the rating questions."

The mechanism is visible in the drill output. The digest hands the model explicit named zero
counts — `n_matching_questions = 0`, `n_liking_answers = 0` — which close the absence question
outright, where raw rows invited another round of probing. The drill also discards the raw rows on
success, so the option-label codes that both fabrications were built from were simply not reachable.

**`funda_agent_exp` loses 5 points**, which is inside the run-to-run noise documented above and
should not be read as a real regression. Its one clear gain is T3: with drilling on it reported
**both** instruments side by side (10.25 / 9.25 / 9.00 and 3.50 / 2.50 / 1.75, exact) — the only
run in this entire benchmark to fully satisfy the case as specified.

### One real drill defect, worth fixing

On T2, `funda_agent` with drilling on reported *"Each product had N=1 respondent/value in this
result"*. True N is 300, and **the SQL had it right** — the query selected
`COUNT(score) AS n_values, COUNT(DISTINCT enrollment_id) AS n_respondents`, both 300.

The corruption happens in the digest. For a low-cardinality column `digest_result` emits
`f"{v} (n={n})"` where `n` is the **number of rows carrying that value** (`output_store.py:247`).
On a per-product aggregate that is 1 row per product, so the digest says `n=1`. The weak drill model
read that group-frequency as the sample size, dropped the query's own `n_respondents` column, and
the main loop reported it as fact. The drill-off arm of the same case got N=300 right.

**This is a naming collision between the digest's group-frequency `n` and the analyst's `n`
columns, and it silently destroys sample sizes.** Renaming the digest's field to `rows=` would fix
it. This matters more than the token cost: it is the one place where drilling made a correct number
wrong.

### Recommendation on drilling

- **Do not enable drilling globally on small results.** It costs 16–30% wall-clock and buys nothing
  when the payload already fits.
- **`funda_agent` should keep a drill path for absence/diagnostic queries specifically.** Two
  fabrications became correct refusals. That is a large accuracy gain from a cheap 1-turn workflow,
  and it is the single most effective fix seen for its worst failure mode.
- **Fix the digest `n` collision first** (`output_store.py:247`) — otherwise widening drilling
  trades fabricated refusals for corrupted sample sizes.
- **`funda_agent_exp`'s drill agent is 3.3× the cost of `funda_agent`'s drill workflow per firing**
  and did not buy proportional accuracy here. Its shape-based bypasses (`small_shape`, `choice_set`)
  are doing real work — keep them.

---

## Appendix: pre-fetch ablation on `funda_agent_exp`

Every exp run opens with the same boilerplate inventory query, regenerated from scratch at
325–470 output tokens and 5.7–7.8 s per run. It is a fixed prelude, not a decision — so this
tests running it in Python before the first LLM call and injecting the result into turn 0.

Measured DB-wide first: of 132 surveys with product-attributable measures, **128 (97%) fit the
combined inventory+aggregates payload under 8,000 chars** and all 132 fit under 60,000. Worst
case is 46,797 chars / 80 measures (the T2 survey).

Three variants, 6 cases, run concurrently per case (`scripts/run_prefetch_bench.sh`, results in
`needle_haystack_results/prefetch/`):

- **none** — the agent as it ships.
- **prefetch** — inject per-product stats for numeric, product-linked measures.
- **prefetch_fixed** — same, plus the product roster with blinding numbers and a section listing
  every *other* answered measure (ranking, matrix, non-product-linked) with counts and sample
  option labels.

The join gap is real: `JOIN product` plus a numeric-`optionAnswer` filter silently drops ranking
questions, matrix questions (values live in `optionLabel`) and unlinked scales — which is exactly
T5's evidence.

| variant | accuracy | wall s | LLM turns | SQL calls | input | uncached input | output |
|---|---|---:|---:|---:|---:|---:|---:|
| none | 5.17 / 6 — 86% | 133.7 | 21 | 13 | 214,201 | 53,433 | 8,962 |
| prefetch | 4.92 / 6 — 82% | 74.3 | 8 | 2 | 111,749 | 44,165 | 4,372 |
| **prefetch_fixed** | **5.67 / 6 — 94%** | **66.8** | **7** | **1** | 112,961 | **37,185** | **3,637** |

Pre-fetch SQL costs 0.08–0.61 s per case (0.99 s across all six).

### Per case

| case | none | prefetch | prefetch_fixed |
|---|---|---|---|
| T1 | 3 turns, 1.00 | 1 turn, 1.00 | 1 turn, 1.00 |
| T2 | 5 turns, 0.67 | 1 turn, 0.67 | 1 turn, 0.67 |
| T3 | 3 turns, 1.00 | 1 turn, 1.00 | 1 turn, 1.00 |
| T4 | 2 turns, 1.00 | 1 turn, 1.00 | 1 turn, 1.00 |
| T5 | 3 turns, 0.50 | 1 turn, 0.50 | 1 turn, **1.00** |
| T6 | 5 turns, 1.00 | 3 turns, 0.75 | 2 turns, 1.00 |

### Findings

**`prefetch_fixed` wins on every axis** — 94% accuracy (vs 86% baseline), half the wall-clock,
a third of the turns, 47% less input and 59% less output. It is also *cheaper than plain
`prefetch`* despite carrying a larger payload, because it needs fewer follow-up turns.

**T5 is the headline, and it is structural rather than lucky.** `prefetch_fixed` produced the
only complete T5 answer in this entire benchmark: it named the ranking question, stated it is not
linked to the product roster, and quoted the exact mismatch — labels `251, 352, 709, 873, 928`
against blinding numbers `944, 551, 815, 601, 332` — then declined to map them. Under plain
`prefetch` the ranking question is *absent from the payload*, so the model cannot name it; that
0.50 → 1.00 is a fixed defect, not variance.

**Exposing distractors did not hurt.** `prefetch_fixed` newly surfaces T3's matrix question whose
options are literally labelled `Flavor/Taste Liking`, `Appearance Liking` etc. The model still
picked the correct vertical-rating needle and still reported both scale instruments separately.

**Turn collapse did not cost accuracy anywhere.** Five of six cases answer in a single LLM call.
T6 needs a second only because the benchmark survey sits outside the pre-fetch scope.

**What pre-fetch does not fix: T2's verdict.** All three variants still claim Doritos separates
from Lay's Classic, where Tukey has `a` / `ab` — not separable. Same hand-rolled-threshold error
throughout, consistent with `run_survey_stats` never being called. Confirms the statistics pass
must become a deterministic stage, not a tool the loop may elect.

### Caveats

- n=1 per cell. The turn collapse (21 → 7) and the T5 fix are far outside the variance documented
  above; the 82% vs 86% gap between `prefetch` and `none` is **not** — it is one borderline
  threshold call on T6 (gap 0.43 against a ~0.50 threshold) and should be read as noise.
- Cache hit rate falls from 75% to 67%, since the injected payload is per-survey and uncacheable.
  Absolute uncached input still drops 30%, so this is a win, not a regression.
- Pre-fetch is paid on every question, including ones that never needed the inventory.

---

## Appendix: validating the fixes against `regression.xlsx`

The needle set is entirely measure-selection questions — exactly what pre-fetch optimises for.
`regression.xlsx` has 18 populated rows with a broader mix (8 benchmark comparisons, 4 historical
cross-survey comparisons, 3 explicit statistical tests, 3 survey-metadata), so it can separate a
general win from an overfit. Four variants × 18 rows = 72 runs, variants concurrent per row
(`scripts/run_regression_exp.sh`, results in `needle_haystack_results/regression_exp/`).

| variant | figure accuracy | wall s | turns | SQL | stats calls | tool errors | input | output |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| none | 14/32 — 43% | 471 | 67 | 49 | 0 | 11 | 794,174 | 33,409 |
| bindings | 22/32 — 68% | 468 | 71 | 47 | 4 | 10 | 845,020 | 31,568 |
| prefetch_fixed | 24/32 — 75% | 426 | 51 | 33 | 0 | 5 | 758,601 | 27,655 |
| **both** | **31/32 — 96%** | **342** | **48** | **23** | 4 | **0** | **743,030** | **21,393** |

"Figure accuracy" = expected ground-truth values (from the Charts API and direct DB queries)
present in the final answer, over the 9 rows where reference values were derived.
`both`'s one miss is a rounding artifact — it wrote `F = 2.07` where the reference is `2.068`.

### The most serious defect in the whole exercise: fabricated statistics

On the three rows that *explicitly demand* ANOVA / Tukey / Pearson / Spearman, the shipped agent
called `run_survey_stats` **zero times** — it is unbound, per the lean-bindings gap — and did not
say so. It made the numbers up instead:

| row | shipped agent (`none`) | reference (Charts API) |
|---|---|---|
| R08 | `F = 2.138`, no p-value | `F = 1.069, p = 0.446` |
| R09 | `F = 4.137` plus a **Tukey grouping table it never computed** | `F = 2.068, p = 0.273` |
| R10 | pooled across products → `r = -0.019`, "no meaningful relationship" | per-product; Spearman P1 `ρ = -0.684, p = 0.042` — **significant** |

R09 is the worst: it printed a three-row Tukey letter table having never run Tukey. R10 is the
most misleading: pooling across products cancelled a real positive and a real negative correlation
to approximately zero, and it reported that as evidence of no relationship.

With bindings fixed, all three return exact Charts API figures. **This alone justifies the
one-line change**, even though it *costs* tokens (+6% input: more tool schemas, slightly more
turns).

### Pre-fetch generalises on turns, latency and errors — not on input tokens

| query type (n) | metric | none | both |
|---|---|--:|--:|
| Survey metadata (3) | turns / input | 6 / 46,537 | 4 / 43,039 |
| Historical comparison (4) | turns / input | 18 / 228,907 | 15 / 181,633 |
| Statistical analysis (3) | turns / input | 10 / 113,141 | 6 / 80,759 |
| Benchmark comparison (8) | turns / input | 33 / 405,589 | 23 / **437,599** |

On the needle set pre-fetch cut input 47%. Here it cuts it 6% overall and **increases** it 8% on
benchmark comparisons, where the inventory payload averages 20,854 chars but the benchmark survey
still has to be queried separately. **The input-token win was an artifact of the needle set.** What
does generalise: turns −28%, output −36%, wall −27%, SQL calls −53%, tool errors 11 → 0.

### A second, unadvertised benefit of pre-fetch: answer grain

Without pre-fetch the agent frequently answers benchmark comparisons at *survey* level rather than
per product — R11 returned a single pooled mean of 5.80 over N=450, R12 returned 8.15 over N=1,200
— losing the per-product breakdown the question implies. With per-product statistics arriving
pre-grouped, it reports per product. That mechanism, not luck, is most of the 43% → 75% jump.

### Caveats

- n=1 per cell, 18 rows, and 6 of them are duplicate prompts (R11/R14, R12/R15, R13/R16), so
  effective distinct coverage is 15 questions.
- Figure-matching is a proxy for accuracy: it rewards correct numbers appearing, and does not
  fully judge interpretation.
- `both` reaching exactly 0 tool errors is partly luck, though the direction (11 → 10 → 5 → 0) is
  consistent.

---

## Reproducing

```bash
# one run
BE/.venv/bin/python scripts/bench_needle.py --agent funda_agent_exp --case T2 --out out.json

# all 18 (three agents concurrently per case)
bash scripts/run_needle_bench.sh

# drilling ablation: 2 agents x 6 cases x {drill_off, drill_on} = 24 runs
bash scripts/run_drill_ablation.sh

# pre-fetch ablation on exp: 6 cases x {none, prefetch, prefetch_fixed} = 18 runs
bash scripts/run_prefetch_bench.sh
```

Per-run records — wall time, per-turn token/latency detail, every SQL statement, tool outputs and
the full message transcript — are in `needle_haystack_results/`.

### Read the speed/token numbers as indicative, not precise

Single run per cell. A spot re-run of `funda_agent_v1` on T4 afterwards took **9 turns / 45,200
input tokens** where the benchmark run took **4 turns / 32,485** — same agent, same case, +39%
input. So turn counts and token totals carry substantial run-to-run variance too, not just
wall-clock. (Wall time was nearly identical across those two runs, 27.7 s vs 28.6 s, because the
extra turns were cheap weak-model ones.)

What that means for reading this report:

- The **accuracy findings are robust** — they are qualitative failures reproducible from the
  transcripts (a pooled scale, a fabricated product ordering, a distractor question selected), not
  close numeric calls.
- The **efficiency ranking is directional**. The 5% cost spread between the three agents is well
  inside single-run noise; treat "cost is effectively identical" as the finding, and do not read
  the per-case token deltas as reliable. The large gaps — `funda_agent`'s 8-turn/71.9k T5 versus
  `funda_agent_exp`'s 2-turn/22.6k — are big enough to survive this variance, but confirm with
  repeated runs before optimising against any of the smaller ones.

The three agents ran concurrently within each case, which holds API conditions constant across
variants but means absolute latencies are not serial-execution numbers.
