# Needle-in-a-haystack test set — "overall liking"

Six cases for measuring **accuracy, speed and token usage** of a survey-analyst agent.

The task in every case is the same: the user names a *concept* ("overall liking"), and the
agent must find which survey question measures it — then either compute the answer or say
plainly that no such measure exists. The **needle** is the correct question. The **haystack**
is every other scorable question in that survey, including distractors that share the concept's
keywords. Keyword search cannot solve these; the discrimination has to be semantic.

All ground truth below was computed directly against the docker Postgres DB
(`postgresql://gpi:gpi_local@localhost:5433/gpi_sample_db`) and re-verified.

---

## The six cases

| # | client_id | organization_id | survey_id | prompt |
|---|---|---|---|---|
| T1 | `37cbf852-a2b7-4f8b-96b4-7f67432f88cd` | `c75d846a-e265-4f69-92c1-91308e0697f6` | `6263cf71-23b7-4462-9ccf-4a00a7267672` | Sort the products in order top to bottom based on overall liking. |
| T2 | `f6b05cb4-cea2-4855-816e-c92e5e5d22ff` | `17b6da66-3c2e-42f1-bb64-2a09bdfbc183` | `e14528a9-01ac-4dff-844e-0914dbdb3759` | Sort the products in order top to bottom based on overall liking. |
| T3 | `7b445d7f-3fc1-4154-8a23-586e09c781a3` | `8dd4fd29-cd84-46a1-abcd-1459c547023b` | `dedf24e1-5c82-4aeb-b0c6-d6bb4e2d104e` | Sort the products from most to least liked overall. |
| T4 | `c0b3b212-bc35-45ef-b69d-8257d3735a90` | `46671273-0b12-49a5-b9de-60f81d192818` | `39af3240-42a8-4e35-8c7d-c61703d5ce3f` | Sort the products in order top to bottom based on overall liking. |
| T5 | `26ebcbaf-4850-491b-9064-2c8b7f11aaac` | `ff890ed1-b484-4ebe-b102-6ebfd40ca275` | `e84bde45-40ca-4863-b000-a9905c8644bb` | Sort the products in order top to bottom based on overall liking. |
| T6 | `37cbf852-a2b7-4f8b-96b4-7f67432f88cd` | `c75d846a-e265-4f69-92c1-91308e0697f6` | `6263cf71-23b7-4462-9ccf-4a00a7267672` | Sort the products by overall liking and compare each against the benchmark. |

## Haystack size

"Measures" = questions of type `line-scale` / `vertical-rating` / `matrix` / `ranking` that
have answers — i.e. the candidate set the agent must choose from.

| # | survey | total questions | measures (haystack) | inventory size | needle present |
|---|---|---:|---:|---:|---|
| T1 | Herbalife Nutrition Survey | 21 | 6 | ~1.4 KB | yes |
| T2 | PepsiCo Superiority Competitive TEA H2 | 143 | **83** | **~24.5 KB** | yes |
| T3 | Consumer Qualification Survey | 10 | 8 | ~2.1 KB | yes (×2, different scales) |
| T4 | Sensory Descriptive Analysis | 8 | 3 | ~0.5 KB | **no — absence case** |
| T5 | Beef Jerky 2026 | 4 | 4 | ~0.9 KB | **no — absence case** |
| T6 | Herbalife + benchmark survey | 21 + 21 | 6 + 6 | ~2.7 KB | yes, in both |

T2 is the real haystack: 83 measures, and 13 of the 143 questions contain "lik". It is also the
only case in this database where the inventory exceeds a typical 8 KB tool-output limit, so it
doubles as a test of whether the harness truncates or summarises the candidate list.

---

## Per-case detail and expected answer

### T1 — baseline semantic match

**Needle:** `How much do you LIKE or DISLIKE this product OVERALL?` (vertical-rating, 9-pt hedonic)

**Distractors:** five other LIKE/DISLIKE questions, three of which also contain the word
OVERALL — `OVERALL AROMA`, `OVERALL FLAVOR`, `OVERALL TEXTURE`, plus `APPEARANCE` and
`SWEETNESS`. A naive `ILIKE '%overall%'` returns four candidates.

| product | mean | SD | N |
|---|---:|---:|---:|
| Product A | 5.95 | 2.23 | 150 |
| Product C | 5.86 | 2.21 | 150 |
| Product D | 5.57 | 2.21 | 150 |

**Correct verdict:** the ordering is **not** statistically separable. At SD ≈ 2.2 and N = 150 the
~95% separability threshold is ≈ 0.51 and the largest gap is 0.38.

### T2 — needle buried in a long prompt, 83-measure haystack

**Needle** (`c5a603a7-c581-4767-99f9-60c8bd9d5194`):
> Now eat what you would consider to be a regular serving of the product.  Thinking about
> EVERYTHING ALL TOGETHER (appearance, flavor, texture, etc.), how much do you LIKE or DISLIKE
> this snack OVERALL?

The discriminating words sit at **character ~150**, so any pipeline that truncates prompts
destroys this case.

**Distractors, in descending nastiness:**
- `3387dd20-…` "Before beginning this tasting, how much do you **expect** you are going to like this product?" — *expected* liking, pre-tasting. Wrong construct, and it contains no "overall", so a conjunctive keyword filter never even retrieves it.
- `24d1c8fe-…` "Considering all aspects of the packaging …, how much do you like the **OVERALL PACKAGING** of this product?" — has both keywords, wrong subject.
- "Below is a list of packaging statements about overall impressions … Makes the product look appetizing / looks like it would taste great."
- ~10 attribute-level likings (APPEARANCE / AROMA / FLAVOR / TEXTURE / AFTERTASTE).
- ~40 near-identical `Look at the statements below…` matrix rows as bulk noise.

| product | mean | SD | N |
|---|---:|---:|---:|
| Doritos Nacho Cheese 9.25 oz | 8.32 | 0.89 | 300 |
| Lay's Classic Potato Chips 8 oz (2025) | 8.14 | 0.84 | 300 |
| Lay's BBQ Potato Chips 7.75 oz | 8.11 | 0.82 | 300 |
| Tostitos Restaurant Style Tortilla Chips 12 oz | 8.02 | 1.02 | 300 |

**Correct verdict** — from the Charts API (ANOVA F = 5.8655, p = 0.000564, significant; Tukey at
α = 0.05):

| product | Tukey group |
|---|---|
| Doritos | a |
| Lay's Classic | ab |
| Lay's BBQ | b |
| Tostitos | b |

Doritos separates from Lay's BBQ and Tostitos but **not** from Lay's Classic. Note that the
hand rule `2·SD·√(2/N)` ≈ 0.145 wrongly calls the 0.18 Doritos-vs-Classic gap real, because it
ignores the multiple-comparison correction across 6 pairwise tests. Score against Tukey.

**Reference values for the two main distractors** (for prompt variants that ask for them):
expected liking 8.30 / 8.13 / 8.00 / 8.00; overall packaging liking 7.88 / 7.77 / 7.75 / 7.69.

### T3 — no lexical overlap, and a scale trap

**Needle:** `Overall, how do you feel about this sample? Please mark your reaction on the
vertical scale below.` — contains **no form of the word "like"**, so keyword search cannot reach
it at all.

**Distractor:** a matrix, `Please rate your liking of each product characteristic below.`, whose
option labels are literally `Flavor/Taste Liking`, `Texture/Mouthfeel Liking`,
`Appearance Liking`, `Aroma/Odor Liking` — the only verbatim occurrences of "Liking" in the
survey, and the wrong (attribute-level) answer.

**The trap:** the needle exists as **two question rows with identical prompts and different
scales** (`positionLabels` 6 anchors vs 4; observed ranges 0–14 vs 0–4). They must be reported
separately — averaging across them produces a number with no unit.

| product | `09de3be1-…` (0–14) | `31c1f069-…` (0–4) |
|---|---:|---:|
| Product 2 | 10.25 | 3.50 |
| Product 1 | 9.25 | 2.50 |
| Product 3 | 9.00 | 1.75 |

N = 4 per product per question. Rank order is P2 > P1 > P3 on **both**, so the ordering is
robust while the magnitudes are not comparable.

**Scoring:** pooling the two yields P2 6.88 / P1 5.88 / P3 5.38 — right order, meaningless
magnitudes. Treat pooled means as a **failure**, not a pass. N = 4 must also be called out.

### T4 — absence case: must refuse, not invent

There is **no overall-liking measure**. The three product-scored questions are `Appearance`,
`Aroma`, `Texture` (line-scale, observed 1–15, 17 respondents), and their sub-attributes are all
*intensity* descriptors: `Color Intensity`, `Color Uniformity`, `Surface Gloss`,
`Visual Freshness`, `Aroma Intensity`, `Cooked Aroma`, `Dairy Aroma`, `Fresh Aroma`,
`Chewiness`, `Creaminess`, `Firmness`, `Juiciness`, `Smoothness`. Intensity is not hedonic.

There are also two open-text items — `What did you like most about the sample?` /
`What did you dislike most about the sample?` — which contain the keyword but yield no number.

**Correct answer:** state that this survey has no overall-liking question, name what it does
have, and stop. **Failure mode to detect:** silently averaging intensity ratings and calling the
result liking.

### T5 — absence case with an attractive wrong proxy and a broken join

Four questions, none measuring liking. Two independent traps:

1. `Rate the Taste 0 to 10` *looks* hedonic but is a multi-attribute line-scale: each answer
   carries **7** `answered_question_options` rows labelled `Tangy or Acid`, `Sweetness`, `Soapy`,
   `Fat`, `Spice Level`, `Clean`, `Umami`. Averaging them is meaningless as liking — and a naive
   `COUNT(*)` reads 42 where there are only 30 answers, so N comes out 7× wrong.
2. `Rank the samples in order from most to least preferred` is the nearest legitimate proxy, but
   its `answerData->>'optionLabel'` values are `709, 352, 873, 251, 928` while the products'
   `blindingNumber`s are `944, 551, 815, 601, 332` — **they do not match**, so the ranking cannot
   be attributed to products.

Mean ranks (N = 6 each, lower = more preferred): `709` 1.00, `251` 3.00, `928` 3.33, `873` 3.50,
`352` 4.17.

**Correct answer:** no overall-liking measure exists; the preference ranking exists but cannot
be linked to product identities — say **both**. Do not apply an interval separability test to
mean ranks (ranks within a respondent are a forced permutation). Do not invent a code→product
mapping.

Verified DB-wide: **no** ranking question in this database yields a valid per-product ordering.
Across all 9 surveys with ranking questions, zero option labels match any product
`blindingNumber`; and where ranking answers *do* carry a `product_id`, the ranked labels are
sub-items (`Item 1`…`Item 6`, `aa`, `bb`) rather than products.

### T6 — same needle in two tenants, plus the benchmark carve-out

Same needle wording as T1, matched in **both** the client survey and the fixed benchmark survey
(`client_id=9156afbe-7010-4f40-9f00-98f68499baf1`,
`organization_id=f8fa2d32-7d76-4ad4-84d2-a8c49d3d393c`,
`survey_id=4ec4b648-99bd-4d72-89ee-9cf1b7626e4c`). Reading that one survey from outside the
run's tenant is the sanctioned exception; every other cross-tenant read is a violation.

Benchmark: `Product B`, mean **6.00**, SD 2.11, N = 150.

| product | mean | vs benchmark |
|---|---:|---:|
| Product A | 5.95 | −0.05 |
| Product C | 5.86 | −0.14 |
| Product D | 5.57 | −0.43 |

**Correct verdict:** all three are within noise of the benchmark (threshold ≈ 0.50 at SD ≈ 2.2,
N = 150) — none is distinguishable from it.

---

## Metrics to record per run

**Accuracy** — score these independently, they fail independently:
1. **Needle found** — did it select the correct question (and reject the distractors)?
2. **Numbers correct** — do means / SD / N match the tables above?
3. **Statistical verdict** — separable vs within-noise, judged against Tukey where available (T2) or the threshold otherwise.
4. **Honesty on absence** — T4/T5: refused without fabricating; T5 additionally names the unusable ranking and why it cannot be used.
5. **Scale integrity** — T3: reported the two instruments separately rather than pooling.
6. **Tenancy** — T6: read the benchmark survey and nothing else outside the tenant.

**Speed** — wall-clock seconds per case, and LLM turns (a turn is the unit that costs latency).

**Tokens** — input, cached input, output, reasoning. Count **every** LLM call, including
sub-agents: a variant that offloads work to a summariser sub-agent looks cheap if you only read
the main loop's counters. Patching `ChatOpenAI.invoke` at class level captures all paths
uniformly across variants; per-module `TURN_LOG`s do not agree on what they record.

**Also worth logging** — SQL calls per case, and whether the candidate inventory was truncated,
summarised or parked. On T2 that is the difference between the needle being visible and not.

## Machine-readable

```json
[
  {"case":"T1","client_id":"37cbf852-a2b7-4f8b-96b4-7f67432f88cd","organization_id":"c75d846a-e265-4f69-92c1-91308e0697f6","survey_id":"6263cf71-23b7-4462-9ccf-4a00a7267672","prompt":"Sort the products in order top to bottom based on overall liking.","haystack_measures":6,"needle_exists":true,"expected":{"Product A":5.95,"Product C":5.86,"Product D":5.57},"n":150,"separable":false},
  {"case":"T2","client_id":"f6b05cb4-cea2-4855-816e-c92e5e5d22ff","organization_id":"17b6da66-3c2e-42f1-bb64-2a09bdfbc183","survey_id":"e14528a9-01ac-4dff-844e-0914dbdb3759","prompt":"Sort the products in order top to bottom based on overall liking.","haystack_measures":83,"needle_exists":true,"expected":{"Doritos Nacho Cheese 9.25 oz":8.32,"Lay's Classic Potato Chips 8 oz (2025)":8.14,"Lay's BBQ Potato Chips 7.75 oz":8.11,"Tostitos Restaurant Style Tortilla Chips 12 oz":8.02},"n":300,"tukey_groups":{"Doritos Nacho Cheese 9.25 oz":"a","Lay's Classic Potato Chips 8 oz (2025)":"ab","Lay's BBQ Potato Chips 7.75 oz":"b","Tostitos Restaurant Style Tortilla Chips 12 oz":"b"}},
  {"case":"T3","client_id":"7b445d7f-3fc1-4154-8a23-586e09c781a3","organization_id":"8dd4fd29-cd84-46a1-abcd-1459c547023b","survey_id":"dedf24e1-5c82-4aeb-b0c6-d6bb4e2d104e","prompt":"Sort the products from most to least liked overall.","haystack_measures":8,"needle_exists":true,"must_not_pool":true,"expected":{"09de3be1":{"Product 2":10.25,"Product 1":9.25,"Product 3":9.00},"31c1f069":{"Product 2":3.50,"Product 1":2.50,"Product 3":1.75}},"n":4,"separable":false},
  {"case":"T4","client_id":"c0b3b212-bc35-45ef-b69d-8257d3735a90","organization_id":"46671273-0b12-49a5-b9de-60f81d192818","survey_id":"39af3240-42a8-4e35-8c7d-c61703d5ce3f","prompt":"Sort the products in order top to bottom based on overall liking.","haystack_measures":3,"needle_exists":false,"expected":"refuse: no overall-liking question; only Appearance/Aroma/Texture intensity scales"},
  {"case":"T5","client_id":"26ebcbaf-4850-491b-9064-2c8b7f11aaac","organization_id":"ff890ed1-b484-4ebe-b102-6ebfd40ca275","survey_id":"e84bde45-40ca-4863-b000-a9905c8644bb","prompt":"Sort the products in order top to bottom based on overall liking.","haystack_measures":4,"needle_exists":false,"expected":"refuse: no overall-liking question; preference ranking exists but its labels (709/352/873/251/928) do not match product blindingNumbers (944/551/815/601/332)"},
  {"case":"T6","client_id":"37cbf852-a2b7-4f8b-96b4-7f67432f88cd","organization_id":"c75d846a-e265-4f69-92c1-91308e0697f6","survey_id":"6263cf71-23b7-4462-9ccf-4a00a7267672","prompt":"Sort the products by overall liking and compare each against the benchmark.","haystack_measures":6,"needle_exists":true,"benchmark":{"survey_id":"4ec4b648-99bd-4d72-89ee-9cf1b7626e4c","product":"Product B","mean":6.00,"sd":2.11,"n":150},"expected":{"Product A":5.95,"Product C":5.86,"Product D":5.57},"separable":false}
]
```

## Optional extras

Three follow-up probes that isolate single failure modes the six above can mask:

| # | scope | prompt | isolates |
|---|---|---|---|
| T2a | T2's ids | Which snack scored highest on overall liking, and how does that compare to how much people expected to like it before tasting? | forces the pre-tasting expectation question into scope — a `%overall%` filter never retrieves it |
| T2b | T2's ids | Rank the products by overall liking and separately by overall packaging liking. | punishes collapsing product liking into packaging liking |
| T3a | T3's ids | Which product has the highest overall liking score? | re-tests the scale-pooling trap under a different phrasing; a variant that pools on one wording and not the other is unstable |

## Before you benchmark

`funda_agent.py` and `funda_agent_v1.py` still carry the **old, revoked** hardcoded
`OPENAI_API_KEY` (ends `…Vi07rPGxUA`) and return HTTP 401. `funda_agent_exp.py` has the working
key (ends `…3lES7uaGAA`). Sync the key across the three before comparing, or the comparison
measures nothing.
