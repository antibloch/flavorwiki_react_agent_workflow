# test_cases.md — Herbalife survey, one chat thread

Paste these in order into a single conversation on the **Herbalife Nutrition Survey**
(450 respondents, all completed · 3 products A / C / D · 6 liking measures on a 1-9 scale ·
benchmark category **Protein Bars**, n = 150).

Every prompt is self-contained on purpose — history is trimmed to 5 rounds
(`HISTORY_MAX_ROUNDS`, `funda_agent_exp.py:133`), so by prompt 8 the opening turns are gone from
context. No prompt says "that table" or "the one you just showed". Keep it that way if you reorder.

---

#`## 1 — Orientation
```
What can you tell me about this survey — how many people took it, which products were tested, and what was measured?
```
Answers from the inventory prefetch, no tool call. Expect 450, 3 products, the six liking measures.
**No base-composition split** should appear — nothing here is mixed, so a disclosure means the rule
is over-triggering.

### 2 — Nested table
```
Compare all three products across every descriptive attribute in this survey. Group the attributes so the table is readable.
```
8 descriptive questions × 3 products, with parent groups asked for explicitly. Expect one
`gpi-nested-table`; `Stat` as the FIRST column of each terminal table, Mean/SD/N one per row; no
`Winner` column; **no better/worse direction on denseness, aftertaste or sweetness intensity**.

### 3 — Chart, and type discrimination
```
Now show me the same comparison as a chart.
```
Expect exactly one `gpi-chart`, `version=1`, a type suited to attributes × products (`heatmap`) or
categories on one measure (`bar_chart` / `column_chart`) — not everything collapsed into one type.
Point count = attributes × products, none dropped. `I could not render that chart` is a fail.

### 4 — Benchmark banner, describe branch
```
Tell me about the benchmark for this survey.
```
Must OPEN with a benchmark-only banner — one row per measure with mean, base and scale, and **no
Product A / C / D columns**; the product comparison goes below it. All six at n = 150, scale 1-9:
aroma 6.71 · flavor 6.10 · texture 6.59 · sweetness 6.74 · overall 6.00 · appearance 7.27.
Fail if the title `Confidential benchmark product - not for direct display` is printed.

### 5 — Differential bar chart
```
Compare this survey's liking scores against the benchmark.
```
The headline case. Expect the compare-branch banner (one column per product, benchmark column
LAST, no survey-side average), then one `gpi-chart` `type=bar_chart` with the benchmark series
**last** and categories sorted **by gap**: sweetness 0.77 · aroma 0.61 · texture 0.52 · overall
0.43 · appearance 0.38 · flavor 0.27. Series must share category order.

### 6 — Reading the differential
```
Which single measure has the widest gap to the benchmark, and which product is furthest behind?
```
Sweetness, Product D, 0.77 below. **Product D beats the benchmark on flavor (6.24 vs 6.10)** — any
"trails on every measure" claim is a fail. Check every *every / all / none* against all six rows.

### 7 — PLSR
```
Compute PLSR between the liking attributes and overall liking.
```
Names both sides, so it must run rather than ask back. Expect `PLSR ANALYSIS COMPLETE`, the
attribute table with PLS coefficients and VIP scores, and the MSE/RMSE/R2 table. VIP order:
flavor 1.099 · aroma 1.035 · texture 0.981 · sweetness 0.944 · appearance 0.933.

⚠️ `R2 = 1.0`, `MSE = 0.0`, `RMSE = 0.0` are **expected here and not a bug** — PLSR aggregates to
product means, so this survey gives it 3 rows for 5 attributes at 2 components: a saturated fit.
The rendering is the showcase. Presenting R² = 1.0 as evidence the model explains overall liking
is an interpretation fail. For metrics that mean something, use the High Protein Bar survey
(4 products, 28 attributes).

### 8 — PCA
```
Show a 3D PCA plot of the product liking measures.
```
⚠️ **This survey cannot produce a PCA.** PCA decomposes the sub-items *inside* one question, and
Herbalife has no matrix question — each liking measure is its own single-variable question. All 21
questions were swept against the charts API:

| questions | `statsTypes=pca` response |
|---|---|
| the 6 vertical-rating liking measures | `200` + `DATA_INSUFFICIENT` — *"PCA requires at least 2 attributes (variables) with data"* |
| the other 15 (multiple-choice, open-answer) | `400` — *"Product-level descriptive stats … only supported for line-scale, vertical-rating…"* |

The prompt says **liking measures** deliberately, to land on the clean `DATA_INSUFFICIENT` path; a
descriptive question returns the HTTP 400 instead and may cost a retry turn.

So this prompt tests the failure path: expect plain language that there is nothing to decompose,
and **no** component table, loadings or variance-explained figure — all of it would be invented.
No `pca_biplot_2d` / `pca_biplot_3d` artifact.

For the real 2D and 3D biplot, run the Tasting Survey in its own thread:
`Show a 3D PCA plot of the statements about your experience with the product`, then
`Run a PCA on the statements about your experience with the product`.

### 9 — Word cloud
```
What did people particularly like about these products? Show it as a word cloud.
```
Herbalife's two open-answer questions across 450 respondents are the richest open text available.
The block is a trusted runtime artifact — its weights must not be restated, re-rounded or re-ranked
in prose. The intro should name the question and base.
`
### 10 — Personas
```
What are the personas of this survey?
```
Clean single cohort. Roster vs single snapshot argued from counts rather than asserted; candidate
overlap counted at respondent grain; the match-nothing remainder stated. Reference: 450
respondents, 318 with children, 271 female / 179 male, all income `$100k+`.
No chart and no not-plottable clause is acceptable — the persona contract wins.

---

## Coverage

| feature | prompt | on this survey |
|---|---|---|
| inventory answer, no tool call | 1 | yes |
| nested table (`gpi-nested-table`) | 2 | yes |
| chart default + type discrimination | 3 | yes |
| benchmark banner — describe branch | 4 | yes |
| benchmark differential `bar_chart`, gap-sorted | 5 | yes |
| sweeping-claim check on the differential | 6 | yes |
| PLSR tables + VIP ranking | 7 | renders; metrics degenerate (3 products) |
| word cloud | 9 | yes |
| personas | 10 | yes |
| base-composition silence | 1 | yes (nothing to disclose) |
| **2D / 3D PCA biplot** | 8 | **no — impossible here; failure path only** |

Grading detail and the DB queries behind every number above: `tests.md`. Before calling any turn a
failure, `tests.md` §11 — a lost space in a log (`ProductD`) is display-only, and a long-running
process holds the pre-change module in memory.
