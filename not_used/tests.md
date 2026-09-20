# tests.md — copy-pasteable agent probes

Manual probes for the LangGraph survey-analytics agent (`funda_agent_exp.py`).
Local-only, like `tests/` — **do not mirror to `deployment/`** (PROTOCOL.md §4).
PROTOCOL.md §6 remains the governing workflow: fix → probe → you approve → then sync.

Every id, title, count and threshold below was read from the local DB, not derived.

---

## 0. Setup — run once per shell

```bash
cd /home/junaid/codework/Flavorwiki/flavorai_v2/final_agent_work_v5_base_2_optim3_exp
source .venv/bin/activate
docker ps --format '{{.Names}}' | grep -q gpi-db || docker start gpi-db

HERB=(   # "Herbalife Nutrition Survey"
  --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672
  --client-id 37cbf852-a2b7-4f8b-96b4-7f67432f88cd
  --org-id    c75d846a-e265-4f69-92c1-91308e0697f6
)
TAST=(   # "Tasting Survey"
  --survey-id e14528a9-01ac-4dff-844e-0914dbdb3759
  --client-id f6b05cb4-cea2-4855-816e-c92e5e5d22ff
  --org-id    17b6da66-3c2e-42f1-bb64-2a09bdfbc183
)
PLSR=(   # "High Protein Bar Tasting Survey" -- NULL org/client; empty strings are correct
  --survey-id d67ed874-fb40-446f-a645-6edfbfbdef53
  --client-id ""
  --org-id    ""
)
GENFB=(  # "General Feedback Survey" -- NULL org/client; empty strings are correct
  --survey-id d49a19d9-4cfa-4c1d-889b-5cb2b60d2fdc
  --client-id ""
  --org-id    ""
)
```

### Survey reference

| alias | survey title | survey_id | enrollments | products | benchmark category |
|---|---|---|---|---|---|
| `HERB` | Herbalife Nutrition Survey | `6263cf71-…` | 450, all `completed` | 3 | **Protein Bars** |
| `TAST` | Tasting Survey | `e14528a9-…` | 2400 = 1200 `active` + 1200 `completed` | 4 | *(none)* |
| `PLSR` | High Protein Bar Tasting Survey | `d67ed874-…` | 200 (100 `completed`) | 4 | *(none)* |
| `GENFB` | General Feedback Survey | `d49a19d9-…` | 11 = 5 `completed` + 6 `active` | 3 | *(none)* |

`GENFB` is the only survey in this DB with **pooled** scored attributes, so it is the only
one that can exercise the nested Domain → Attribute → Stat shape. Its four pooled measures
are Appearance (3 attributes), Aroma (4), Packaging (4) and Taste (5). Products are named
`Product` / `Product 2` / `Product 3` with blinding codes 106 / 209 / 917 — the first is
literally named `Product`, and an answer calling it "Product 1" has invented a label.

⚠️ `GENFB` is small and **live**: it held 12 enrollments on 19 Aug 2026 and 11 on 21 Aug.
Regenerate the numbers below with §10's `query.sh` before treating a mismatch as a defect.

The benchmark source is **"Confidential benchmark product - not for direct display"**,
`4ec4b648-99bd-4d72-89ee-9cf1b7626e4c` (client `9156afbe-7010-4f40-9f00-98f68499baf1`,
org `f8fa2d32-7d76-4ad4-84d2-a8c49d3d393c`, 150 respondents). It is the only row in
`benchmark_registry`, active, category `Protein Bars`. Never scope to it directly — the
agent must reach it from HERB by category match.

---

## 1. PCA

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id pca3d \
  --prompt "Show a 3D PCA plot of the statements about your experience with the product"
```

Pass: the tool call carries `pca_plot_dimensions: "3d"`; the output line reads
`Plot: trusted 3D PCA chart prepared.` with **no** `;` clause after it; the artifact is
`pca_biplot_3d` and carries `z_axis`; the answer ends with the 2D suggestion chip; the
string `retained usable component` appears nowhere.

**Regression probe** for any PCA change:

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id pca2d \
  --prompt "Run a PCA on the statements about your experience with the product"
```

Pass: artifact is `pca_biplot_2d`, points carry no `z`, answer ends with the 3D chip.

> Say **"experience"**, not "expectations". The near-identical `fdebd104` question has no
> usable PCA and will legitimately fall back — that is not a bug.

---

## 2. PLSR

```bash
python funda_agent_exp.py "${PLSR[@]}" --thread-id plsr1 \
  --prompt "Compute PLSR between the sensory attributes and overall liking"
```

Pass: `PLSR ANALYSIS COMPLETE`; the attribute table; the MSE/RMSE/R2 table.

**Regression probe** — ambiguity must not auto-run:

```bash
python funda_agent_exp.py "${PLSR[@]}" --thread-id plsrq --prompt "Do PLSR"
```

Pass: the tool is **not** called; the answer lists candidate KPIs and attributes and asks
which to use.

*Known-open, deliberately unfixed:* where the model narrows the attribute set itself it is
supposed to state how many measures it left out, and it omits that. The rule never defines
whether the count includes the KPI. Left alone on purpose — a prior fix attempt here
produced a wrong number and was reverted.

---

## 3. Personas

**Regression probe** — clean single cohort, must keep working:

```bash
python funda_agent_exp.py "${HERB[@]}" --thread-id persona1 \
  --prompt "What are the personas of this survey?"
```

Pass: roster vs single snapshot argued from counts rather than asserted; candidate overlap
counted at respondent grain; the match-nothing remainder stated. Verified reference values:
450 respondents, 318 with children, 271 female / 179 male, all income `$100k+`.

**Defect probe** — currently FAILING, no fix in either tree:

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id persona2 \
  --prompt "Give me personas of this survey"
```

Two failures to look for.

*Fused cohorts.* TAST is two disjoint blocks of 1200 with **intersection 0**:

```
enrollment_status | enrollments | age_q | buyreason_q
active            |        1200 |  1200 |           0
completed         |        1200 |     0 |        1091
```

Fail if profile rows on the 1,200 base sit beside a buy-reason row on the 1,091 base as one
persona — especially if the 1,091 are called "willing buyers", which asserts a nesting that
does not exist. Fail on any `100%` coverage presented as the whole survey; the survey is 2,400.

*Candidate selection.* Fail if the dividing candidates are only multi-select questions.
The two questions that cleanly split all 1,200 are:

| question_id | question | shape |
|---|---|---|
| `23d698ab-2f17-42b7-9d80-cb2bf7b95bb1` | Bumo / Non-Bumo | 580 / 620, one option per person |
| `adf03347-4cc9-4e2b-82fb-ef3bc0985174` | purchase frequency | 597 / 345 / 104 / 55 / 40 / 32 / 14 / 13 |

`d1fe2a79-c436-445f-b8d9-60ce8f4b8c53` (favourite flavours) is multi-select and overlaps by
construction — only **13 of 1200** people picked a single flavour, so "exclusive" subsets of
it must never be named "Loyalists" or "Purists".

Three prompt-only fixes were tried here and all three failed; the next attempt belongs in the
prefetch packet, not in rule text (PROTOCOL.md §9).

---

## 4. Benchmarking

```bash
# has a benchmark -- must discover 4ec4b648 on its own
python funda_agent_exp.py "${HERB[@]}" --thread-id bench1 \
  --prompt "Compare this survey with the benchmark"
```

Pass: reaches the benchmark by category match; no hardcoded survey/client/org id appears in
any generated SQL.

**Paired probe** — no benchmark assigned:

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id bench0 \
  --prompt "Compare this survey with the benchmark"
```

Pass: says in plain language that no benchmark has been set up for this survey, then answers
what it can from the survey itself.

Fail on any of: `has_assigned_benchmark`, `benchmark_context`, other field names, printed
`true`/`false` values, the word "configuration" — the reader is a non-technical survey
stakeholder. Also fail if it names or hints at the Protein Bars benchmark: that belongs to a
different study.

### 4.1 KPI banner — describe vs compare

Two branches of one rule (`agent_instructions.py:255-268`): a benchmark answer that reports
figures opens with a banner, and its shape depends on what was asked. Added 21 Aug 2026 after
"Tell me about the benchmark" was answered with a comparison table and no overview of the
benchmark itself.

**Fix probe** — describe branch:

```bash
python funda_agent_exp.py "${HERB[@]}" --thread-id bmdesc1 \
  --prompt "Tell me about the benchmark for this survey"
```

Pass: the answer OPENS with a benchmark-only banner — one row per benchmark measure carrying
its mean, base size and scale, and **no Product A / C / D columns**. The product comparison
appears BELOW the banner, not inside it. All six means, read from the benchmark packet
(`n = 150` and scale 1-9 for every one):

| benchmark measure | mean |
|---|---|
| overall aroma liking | 6.71 |
| overall flavor liking | 6.10 |
| overall texture liking | 6.59 |
| sweetness liking | 6.74 |
| overall product liking | 6.00 |
| appearance liking | 7.27 |

The `scale` column must be the catalog's `slider_min`/`slider_max` — 1-9 for all six. The
*observed* ranges differ (2-9 sweetness, 3-9 appearance); reporting an observed range as the
scale is a fail. An extra SD column is allowed.

**Regression probe** — compare branch, which must keep its original shape:

```bash
python funda_agent_exp.py "${HERB[@]}" --thread-id bmcomp1 \
  --prompt "Compare overall liking against the benchmark"
```

Pass: comparison-shaped banner — one column per survey product, the benchmark column LAST,
and no survey-side average column. Overall liking is Product A **5.95**, Product C **5.86**,
Product D **5.57**, benchmark Product B **6.00**, `n = 150` each.

Fail on any of: a pooled or averaged survey-side figure across the three products; the
benchmark survey title "Confidential benchmark product - not for direct display" printed; a
banner on the no-benchmark path (§4's TAST probe); HTML — `<div>`, `<table>`, `style=`, CSS.
The banner is Markdown, and `agent_instructions.py:290` still forbids HTML.

Known blemish, not a fail: the compare answer restates the banner's means in a second
Product x Stat table below it. Observed 21 Aug 2026, fix drafted but not applied.

---

## 5. Descriptive comparison and separability

The original defect: seven attribute rankings presented as findings when nothing separated
the products, plus a `Winner` column, a direction read off intensity attributes, `Mean; SD;
N` packed per cell, and the product named `Product` reported as "Product 1". Fixed by
replacing the worked example in `NESTED_RESULT_RULES`, not by adding rules.

**Status: verified passing.**

```bash
python funda_agent_exp.py "${GENFB[@]}" --thread-id a1fix \
  --prompt "Compare all products across every descriptive attribute in Appearance, Aroma, and Texture. Present Appearance, Aroma, and Texture"
```

Pass, all of:
- no `Winner` column, no "strongest"/"best" claim anywhere
- `Stat` as the FIRST column of each terminal table, Mean/SD/N one per **row**
- the first product printed as `Product` or `Product (106)` — never "Product 1"
- **no better/worse direction on Off-Aroma Intensity or Overall Aroma Intensity**
- every attribute carries a Tukey verdict or is stated as within noise
- Texture reported unavailable, with Taste and Packaging named and explicitly *not* substituted
- scales given as the configured slider range — Appearance `0-9`, Aroma `1-5`. Reporting
  "10 configured positions" is the `labelled_positions` bug (`agent_instructions.py`)

Reference means (`N` = 7 / 5 / 5). All seven attributes are non-significant, ANOVA
p = 0.19–0.92, every product in Tukey group `a`:

| attribute | Product (106) | Product 2 (209) | Product 3 (917) |
|---|---|---|---|
| Color Intensity | 4.86 | 6.80 | 5.00 |
| Color Uniformity | 5.57 | 5.60 | 6.40 |
| Visual Freshness | 6.00 | 6.00 | 7.40 |
| Characteristic Aroma | 3.86 | 3.60 | 3.80 |
| Fresh Aroma | 3.86 | 3.60 | 3.60 |
| Off-Aroma Intensity | 2.71 | 3.20 | 3.80 |
| Overall Aroma Intensity | 3.43 | 3.40 | 3.80 |

**Regression probe — NOT YET RUN.** A different pooled measure on the same survey, to test
whether the exemplar fix generalises rather than fitting one prompt. Taste is the harder
case: 4 of its 5 attributes are pure intensity scales where "higher is better" is
meaningless, and the inventory flags order flips on **all five**.

```bash
python funda_agent_exp.py "${GENFB[@]}" --thread-id a1reg \
  --prompt "Compare the products on every taste attribute."
```

Reference means, 1–5 scale, bases 7 / 5 / 5: Bitterness 2.43 / 2.80 / 2.60 · Freshness
3.86 / 4.20 / 3.80 · Saltiness 3.14 / 2.60 / 3.00 · Sourness 3.29 / 2.80 / 3.00 ·
Sweetness 3.29 / 3.60 / 3.80.

Fail on any claim that more or less bitterness, saltiness, sourness or sweetness is better.

---

## 6. Base composition

The original defect, and the one the user actually complained about: a base reported as a
bare `n` that silently mixed people who finished the survey with people who stopped
part-way, disclosed only when challenged.

**Status: verified passing.** Took four attempts. Three prompt placements failed; what
worked was `_base_composition_hint()`, which appends the split to the `nl2sql_tool` result.
See the memory note *where-to-place-agent-constraints* — prompt rules only reach
inventory-derived figures.

**Stability under challenge.** `--chat` reads follow-ups from stdin, and a blank line ends
the loop. This is the automated form of a user asking "wait, what's that number?"

```bash
printf 'Who exactly is in that base?\n\n' | python funda_agent_exp.py "${GENFB[@]}" \
  --thread-id interp1 --chat \
  --prompt "Have you ever tried a product of any flavor before? (Pie Chart)"
```

Reference: 8 answered = **5 completed + 3 in progress**; distribution 3 / 3 / 2.

Pass: the `[base]` line appears in the tool output, **and turn 1 already states the 5/3
split**. Turn 2 should *add* information — a respondent-level roster — not reveal a
qualifier turn 1 withheld.

Fail: turn 1 gives a bare "8 people". If the `[base]` line fired and turn 1 still ignored
it, the model is ignoring a fact placed directly in its tool result — escalate, do not add
another prompt rule.

**Product bases.** Same probe as §5; check the base note.

Reference: `Product (106)` was rated by **7** people — **5 completed + 2 in progress**;
`Product 2 (209)` and `Product 3 (917)` by **5** each, **all completed**.

Pass: 106's split attached to 106, 209/917 stated as all-completed, and the bases said to
differ. Fail: one hoisted base for all three products — `REPORTING_RULES` permits hoisting
only where the figure is identical everywhere.

**Regression probe — must stay silent.** `TAST` has 1200 `active` beside 1200 `completed`,
but none of the active ones submitted a product answer, so no scored base is mixed:

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id b1reg \
  --prompt "Run a PCA on the statements about your experience with the product"
```

Pass: the §1 2D-PCA criteria, **and no base-composition disclosure at all** — there is
nothing to disclose, and a rule that fires here is over-triggering.

---

## 7. Interpretation traps

A wrong *interpretation* of correct numbers cannot be caught by checking figures against the
DB — every number in the original failing transcript was exact. These probes are built so a
wrong reading produces something **impossible**, making the grade binary instead of a
judgment call.

### 7.1 Cross-question share — the dropout ladder

`GENFB` drops respondents as it runs, so an early question and a late one have different
bases by construction:

| question | answered | completed | in progress |
|---|---|---|---|
| Did you know anything about this product before this study | 9 | 5 | 4 |
| Have you ever tried a product of any flavor before | 8 | 5 | 3 |
| *(each Design-section question)* | 7 | 5 | 2 |
| Would you change your rating if you evaluated the sample again | 5 | 5 | 0 |

Force two rungs into one sentence:

```bash
python funda_agent_exp.py "${GENFB[@]}" --thread-id interp2 \
  --prompt "Of the people who answered 'Did you know anything about this product before this study', what share would change their rating if they evaluated the sample again?"
```

Reference: 9 answered the awareness question; only **5** answered the rating-change
question, all completers; of those 5, **4 said Yes**. So the defensible figure is
**4 of 5 = 80%**, and the 4 who never reached the question cannot be counted as negatives.

**Status: partially fixed, residual accepted.** The SQL now computes both bases and returns
`share_of_both_answerers_pct`, the 80% is present and correct, and the earlier "4 of 6"
error is gone. The answer still *leads* with `4 of 9 — 44.4%`, which the denominator rule
forbids, and still renders `0.0%` for the never-asked subgroup where the tool returned
`null`. Fail only on a regression past that: the 80% figure disappearing, "4 of 6"
returning, or the 4 non-answerers going unmentioned.

### 7.2 The impossible join — NOT YET RUN

`TAST` holds two disjoint cohorts of exactly 1200 with **overlap 0**: the in-progress cohort
answered only demographics (gender, age, state, allergies, screener), the completed cohort
only the product questions. Since D2 this is visible in the payload without a query —
`What is your gender?` reads `respondents=1200, completed=0` while every product statement
reads `1200 / 1200`.

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id interp3 \
  --prompt "Compare overall liking by gender."
```

**FAIL: any table of liking by gender.** No respondent answered both, so every cell would be
a fabricated join. **PASS:** it states the two cannot be crossed — which `REPORTING_RULES`
already requires ("'Not found' and 'found but unusable' are different answers").

This is the strongest probe in the file: grading needs no judgment. It also exercises base
interpretation at 2× scale, since "how many respondents" is 2400 answering vs 1200 completed.

### 7.3 Building new interpretation probes

Find two populations that genuinely differ — **nested** via dropout, or **disjoint** via
cohort — then ask for something that forces both into one sentence: a share, a cross-tab, a
"how many of the X did Y". Prefer the widest divergence available; 2400-vs-1200 is impossible
to miss where 9-vs-8 hides in rounding. Run it multi-turn, because base defects surface on
the follow-up.

Two queries find the candidates:

```bash
Q=../.claude/skills/regression_test_langraph/scripts/query.sh
# nesting: per-question bases, split by status
$Q "SELECT left(q.prompt,46) AS question,
 count(DISTINCT a.enrollment_id) AS answered,
 count(DISTINCT CASE WHEN e.enrollment_status='completed' THEN a.enrollment_id END) AS completed
FROM answer a JOIN enrollment e ON e.id=a.enrollment_id JOIN question q ON q.id=a.question_id
WHERE e.survey_id='<survey>' GROUP BY 1 ORDER BY 2 DESC;"

# disjointness: overlap between any two question cohorts
$Q "WITH x AS (SELECT DISTINCT enrollment_id FROM answer WHERE question_id='<qid_a>'),
      y AS (SELECT DISTINCT enrollment_id FROM answer WHERE question_id='<qid_b>')
SELECT (SELECT count(*) FROM x), (SELECT count(*) FROM y),
       (SELECT count(*) FROM x JOIN y USING (enrollment_id)) AS overlap;"
```

---

## 8. Table formatting

`ONE CELL HOLDS ONE FIGURE` has no probe of its own — check it in whatever answer produces a
statistics table, in practice the PLSR and persona runs above.

Pass: no cell packs `N=10, Mean=5, SD=2.3`; statistics appear as **rows**, not one column
each; stat labels stay bold on every row; a `gpi-nested-table` artifact carries `Stat` as its
FIRST column and never as a nesting dimension.

Exempt: the `analyze_plsr` tables, which have their own exact-rendering contract.
Also allowed: a count stated against its own base — `18 of 20 people` is one figure.

---

## 9. Charts

Charts became part of the default answer on **21 Aug 2026**. `agent_instructions.py:335` used to
fire only on an explicit chart request; it now demands a possibility judgment on every answer and
treats a table you are about to print as chartable evidence. Three prompt changes ship together --
the default (`:335`), the benchmark two-sided bar chart (`:270`) and the pinned point key
(`:357`) -- plus removal of the chart-only suggestion filter in `funda_agent_exp.py:3870`.

Out of scope here: PCA and word clouds. The runtime owns those artifacts and the rule excludes
them by name; their probes stay in §1.

### 9.0 Shape extractor — write once per machine

Several probes below read the payload instead of eyeballing it, because the failures that matter
(wrong key, misaligned series, silently dropped points) are invisible in a log skim.

```bash
cat > /tmp/chart_shape.py <<'PY'
"""Print the shape of every gpi-chart block in an agent log: python chart_shape.py run.log"""
import json, re, sys

BLOCK = re.compile(r"```gpi-chart\s*(.*?)```", re.DOTALL | re.IGNORECASE)
for path in sys.argv[1:]:
    text = open(path, encoding="utf-8", errors="replace").read()
    blocks = BLOCK.findall(text)
    print(f"\n{path}: {len(blocks)} gpi-chart block(s)")
    for i, body in enumerate(blocks, 1):
        try:
            p = json.loads(body)
        except json.JSONDecodeError as exc:
            print(f"  [{i}] UNPARSEABLE JSON: {exc}")
            continue
        series = p.get("series") or []
        pts = sum(len(s.get("data", [])) for s in series if isinstance(s.get("data"), list))
        first = next((s["data"][0] for s in series
                      if isinstance(s.get("data"), list) and s["data"]), None)
        if isinstance(first, dict):
            shape = "keys=" + ",".join(first)
            nums = all(isinstance(c.get("value"), (int, float))
                       for s in series for c in s.get("data", []) if isinstance(c, dict))
            shape += f" all-value-numeric={nums}"
        elif first is not None:
            shape = f"bare {type(first).__name__}[]"
        else:
            shape = "NO DATA POINTS"
        print(f"  [{i}] type={p.get('type')} version={p.get('version')} "
              f"series={len(series)} points={pts} axes={'x_axis' in p},{'y_axis' in p}")
        print(f"      {shape}")
        orders = []
        for s in series:
            cats = [c.get("label") or c.get("category") or c.get("y") or c.get("x")
                    for c in s.get("data", []) if isinstance(c, dict)]
            orders.append(cats)
            print(f"      series {str(s.get('name'))[:28]!r:<30} {cats}")
        if len(orders) > 1:
            aligned = all(o == orders[0] for o in orders[1:])
            print(f"      CATEGORY ORDER ALIGNED ACROSS SERIES: {aligned}")
    print(f"  suggestion chips: {len(re.findall(r'^\{\{.+\}\}$', text, re.M))}")
PY
```

### 9.1 Chart appears without being asked — fix probe

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id chartdef \
  --prompt "Compare the products on the statements about your experience with the product" \
  2>&1 | tee /tmp/chartdef.log
.venv/bin/python /tmp/chart_shape.py /tmp/chartdef.log
```

Pass: one `gpi-chart` block after the prose, `version=1`, a type suited to a 17-statement ×
4-product matrix (`heatmap`, `bar_chart` or `column_chart`), and `points=68` — every statement
charted. Observed 21 Aug 2026: `heatmap`, 68 points, `keys=x,y,value`, alongside a
`gpi-nested-table`.

Fail on: no chart **and** no clause saying why the table is not plottable; a point count short of
statements × products, which means measures were silently dropped to fit; the string
`I could not render that chart`, which means the runtime withheld the payload (see 9.6).

**Regression probes for the default.** Both must stay unchanged:

- §1's 2D PCA. The one `gpi-chart` block must still be the runtime artifact `pca_biplot_2d` with
  x/y only, and the answer's two Markdown tables must NOT gain a second chart. Verified 21 Aug
  2026 — the trailing 3D chip proves `_assemble_pca_final` ran, and it strips model-authored
  blocks first (`funda_agent_exp.py:3802`).
- §3's persona roster. No chart block, roster contract intact. Verified 21 Aug 2026. A persona
  answer that skips both the chart and the not-plottable clause is **acceptable**: the persona
  output contract wins, and no carve-out was added to the chart rule.

### 9.2 Benchmark comparison chart — fix probe

`agent_instructions.py:270`: a reported benchmark comparison carries a `bar_chart` with one
series per side and categories sorted by gap. Added 21 Aug 2026.

```bash
python funda_agent_exp.py "${HERB[@]}" --thread-id bmchart1 \
  --prompt "Compare this survey's liking scores against the benchmark" 2>&1 | tee /tmp/bmchart1.log
.venv/bin/python /tmp/chart_shape.py /tmp/bmchart1.log
```

Pass: the §4.1 compare-branch banner still opens the answer, then exactly one `gpi-chart` with
`type=bar_chart`, one series per survey product plus the benchmark series **last**,
`CATEGORY ORDER ALIGNED ACROSS SERIES: True`, and categories in this order — read from the
inventory packets on 21 Aug 2026, `n = 150` per cell, scale 1-9:

| liking measure | Product A | Product C | Product D | benchmark B | widest gap |
|---|---|---|---|---|---|
| Sweetness liking | 6.19 | 6.48 | 5.97 | 6.74 | **0.77** (D) |
| Overall aroma liking | 6.84 | 6.82 | 6.10 | 6.71 | 0.61 (D) |
| Overall texture liking | 6.55 | 6.61 | 6.07 | 6.59 | 0.52 (D) |
| Overall product liking | 5.95 | 5.86 | 5.57 | 6.00 | 0.43 (D) |
| Appearance liking | 7.06 | 7.12 | 6.89 | 7.27 | 0.38 (D) |
| Overall flavor liking | 6.37 | 6.35 | 6.24 | 6.10 | 0.27 (A) |

The prose must name the decisive gap (Product D, sweetness, 0.77 below).

Fail on: alphabetical or by-score category order instead of gap order; `ALIGNED: False`, which
mislabels bars if the renderer zips series by index; one series averaging the three products into
a single survey-side bar; the benchmark series anywhere but last.

⚠️ **Product D leads the benchmark on flavor liking (6.24 vs 6.10).** Any prose claiming D trails
on *every* measure is a fail — observed 21 Aug 2026 in one of two runs, still uncharacterised.
Check every `every`/`all`/`none` claim against all six rows.

### 9.3 No benchmark assigned — regression probe

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id bmchart0 \
  --prompt "Compare this survey's liking scores against the benchmark" 2>&1 | tee /tmp/bmchart0.log
grep -niE "gpi-chart|protein bar|product b\b|has_assigned_benchmark|benchmark_context" /tmp/bmchart0.log
```

Pass: plain language that no benchmark has been set up, then what the survey itself can answer,
and `grep` finds nothing. Verified 21 Aug 2026. The reference table of current-survey liking
correctly gets **no** chart — `:270` ends with "emit no chart where the banner itself is absent",
which currently suppresses any chart on that path, not only a benchmark one.

### 9.4 Type discrimination

The type map must not collapse every table into one chart type.

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id charttype \
  --prompt "Compare the products on the purchase and repeat-purchase questions" \
  2>&1 | tee /tmp/charttype.log
.venv/bin/python /tmp/chart_shape.py /tmp/charttype.log
```

Pass: `bar_chart` or `column_chart` with two series of four points — not `heatmap`, which belongs
to a wide attribute matrix. Observed 21 Aug 2026: `bar_chart`, 2 series, 8 points.

Fail on: `heatmap` for a two-measure table; `ALIGNED: False`. The 21 Aug run **did** fail the
alignment check — each series was sorted descending on its own, so slot 1 was Lay's Classic in one
series and Doritos in the other. Only benchmark charts are currently protected against this
(9.2); the general clause was not added.

### 9.5 Point-key stability

`agent_instructions.py:357` pins bar/column/line/pie points to `label` + numeric `value`. Added
21 Aug 2026 after the same prompt emitted `{"label","value"}` on one run and `{"category","value"}`
on another — one chart type, two key names, which no renderer can code against.

Re-run 9.2 and read the extractor line: `keys=label,value` with `all-value-numeric=True` passes;
`keys=category,value` means the clause did not take. Verified 21 Aug 2026 on the run that had
previously emitted `category`.

### 9.6 What the runtime checks — deterministic, no LLM

`_sanitize_model_chart_blocks` (`funda_agent_exp.py:1074`) is the only gate between a
model-authored chart and the frontend. Run this to see exactly how little it enforces:

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
import json, funda_agent_exp as a
def verdict(p):
    _, withheld = a._sanitize_model_chart_blocks("x\n\n```gpi-chart\n" + json.dumps(p) + "\n```")
    return "WITHHELD" if withheld else "PASSED"
cell = {"x": "A", "y": "S1", "value": 4.5}
print("heatmap x/y/value      ", verdict({"version":1,"type":"heatmap","series":[{"data":[cell]}]}))
print("value as string        ", verdict({"version":1,"type":"heatmap","series":[{"data":[{**cell,"value":"4.5"}]}]}))
print("no value key           ", verdict({"version":1,"type":"heatmap","series":[{"data":[{"x":"A","y":"S1"}]}]}))
print("empty series           ", verdict({"version":1,"type":"heatmap","series":[]}))
print("over 300 points        ", verdict({"version":1,"type":"heatmap","series":[{"data":[dict(cell,y=f"S{i}") for i in range(400)]}]}))
print("unsupported type name  ", verdict({"version":1,"type":"heat_map","series":[{"data":[cell]}]}))
print("scatter, string x/y    ", verdict({"version":1,"type":"scatterplot","series":[{"data":[cell]}]}))
PY
```

Observed 21 Aug 2026: only the last three are WITHHELD. A heatmap cell with a string `value`, a
cell with no `value` at all, and an empty `series` all reach the client. Per-point numeric
validation exists for `scatterplot`/`penalty_scatterplot` only (`:1105`), so **every other type's
point shape is enforced by prompt text alone** — which is why 9.5 exists.

### 9.7 The 300-point cap — NOT YET RUN

Default-on charts make `_MAX_CHART_POINTS = 300` (`funda_agent_exp.py:930`) reachable in ordinary
use for the first time. `TAST` has 80 scored measures × 4 products = 320 cells.

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id chartcap \
  --prompt "Compare the products across every attribute measured in this survey" \
  2>&1 | tee /tmp/chartcap.log
.venv/bin/python /tmp/chart_shape.py /tmp/chartcap.log
grep -c "could not render that chart" /tmp/chartcap.log
```

Pass: the model subsets under 300 and says which measures it charted; `grep` returns 0.
Fail: `grep` returns 1 — the runtime deleted the chart and appended *"I could not render that
chart. Ask for it again, naming the measure you want on each axis"* to an otherwise complete
answer, which reads as a malfunction to the reader.

### 9.8 Known variance — not fails

- **A small single-measure table may get no chart.** Three runs of *"Which product scores highest
  on is great tasting?"* on `TAST` produced a chart once. The two chartless runs were the ones
  that called `run_survey_stats` and closed on a Tukey readout; the charted run answered from the
  inventory with no tool call. Deliberately left alone 21 Aug 2026 — forcing it competes for
  attention in exactly the answers carrying the most statistics.
- **Suggestion chips are absent from most chart answers.** Correct: `agent_instructions.py:1984`
  gives a settled factual answer no braces. The filter removal at `:3870` matters only where chips
  are mandatory and not chart-worded — an unclear-intent answer, or the failed-influence path.
- **Four point shapes have been observed** across types (`x,y,value` / `label,value` /
  `category,value` / `x,y`). `docs/API_CONTRACT.md` pins only the scatter one, and the GPI chart
  renderer is not in this repo — the local `FE/` is a `react-markdown` test client with no chart
  code. Which shapes actually render is an open question for the renderer owner.
- `docs/API_CONTRACT.md:183` still reads "For an explicit non-PCA chart request", which the
  21 Aug 2026 change made false. Doc only, no behaviour.

---

## 10. Capture and grep

```bash
python funda_agent_exp.py "${TAST[@]}" --thread-id t1 --prompt "..." 2>&1 | tee /tmp/run.log
grep -nE "pca_plot_dimensions|Plot: trusted|pca_biplot|retained usable|fallback" /tmp/run.log
```

Confirm a prompt rule actually reached the assembled prompt:

```bash
.venv/bin/python -c "import agent_instructions as ai; print('<marker>' in ai.build_system_prompt())"
```

Query the DB directly to verify an expected number rather than computing it:

```bash
../.claude/skills/regression_test_langraph/scripts/query.sh "SELECT ..."
```

---

## 11. Before concluding a fix failed

- **Do not grade exact strings off the transcript.** `_raw_message_to_text`
  (`funda_agent_exp.py:2635`) strips each content part and joins on `\n`, so a space is lost
  wherever the provider splits text mid-sentence. Proven display-only: a logged
  `WHEREe.survey_id` query returned rows, and a data label lost a space the model never saw.
  `Product2 (209)` in a log is this, not a model error.
- **Restart any long-running agent process** — it holds the pre-fix module in memory.
- `docker ps` and `docker start gpi-db` — the container stops on its own.
- Grep the *prose*, not the placeholder, and watch for source line-wraps: a multi-word
  marker split across two lines gives a false negative.
- Derive every pass criterion from a DB query. A criterion reached by arithmetic can certify
  a defect as fixed — that has happened here.
- State the forbidden form as well as the wanted one. A prompt rule can be satisfied in a
  shape that defeats its intent, and a criterion that only describes success reads a dodge
  as ambiguous.

`tests/test_agent_optimizations.py` currently fails to import
(`ModuleNotFoundError: attribute_kpi_workflow`) — pre-existing, unrelated to the above.
