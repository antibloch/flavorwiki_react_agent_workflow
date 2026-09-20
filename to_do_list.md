# To-do list

## Digest preprocessing (`output_store.py::digest_result`)

The digest is the deterministic Python pass that runs on every parked SQL result
(`funda_agent_exp.py:1769`, inside `run_drill`) and is the sole input to the digest workflow
(`ENABLE_DIGEST=1`) and the turn-0 observation for the drill agent (`ENABLE_DIGEST=0`). It is
already fast — ~12 passes over the rows, milliseconds against an 8-25s LLM turn — so nothing
below is a speed fix. Every item is a coverage fix: a figure the analyst is required to report
but the digest cannot supply, or a cap that spends its budget on the wrong facts.

Both condensers forbid the model computing anything itself, so whatever the digest omits is
unrecoverable for that turn. That is what makes these worth doing.

### Tier 1 — additive, no behavioural risk

Each of these only adds facts or removes waste; none changes which facts get selected. Safe to
apply without re-scoring.

- [ ] **1. Add spread to `_stats` (`output_store.py:199`).** It returns `n/min/max/mean` only.
  `REPORTING_RULES` (`funda_agent_exp.py:1390`) mandate SD with every mean *and* the
  `2*SD*sqrt(2/N)` separability check, and `nl2sql_tool`'s docstring calls a mean without spread
  "an unfinished answer" — but the digest prompt forbids the model deriving it. So on any parked
  result the required arithmetic is structurally impossible; the analyst can only quote bare
  means or say it could not tell. Add `sd` (sample SD, matching `STDDEV_SAMP` used elsewhere),
  plus `median`/`p25`/`p75` since mean+min+max hides skew. ~4 lines.
  Partially masked today by the pre-fetched inventory already carrying `sd` per product — but
  that covers only this survey's scored measures, not an ad-hoc parked query.

- [ ] **2. Let a low-cardinality numeric column be a key as well as a measure
  (`output_store.py:223`).** `if col in numeric_cols: continue` skips numerics before
  cardinality is considered, so `blindingNumber`, a 1-9 scale point, `scale_points`, an integer
  wave/period — any numeric *dimension* — can never be grouped by. "Count of answers at each
  scale point" is a core survey figure this digest cannot produce. Admit a numeric column to
  `key_cols` when its distinct count is `<= DIGEST_KEY_MAX_DISTINCT` and its values are
  integral, while leaving it in `numeric_cols` so it is still aggregated.

- [ ] **3. Drop constant columns from `key_cols` (`output_store.py:226`).** A column with <=1
  distinct value qualifies as a key and then pairs with everything under `combinations()`,
  burning slots in the 12-grouping budget on cross-tabs that carry zero information.

- [ ] **7. Truncate at a line boundary and say what was lost (`output_store.py:322`).**
  `digest[:DIGEST_CHAR_CAP]` can slice mid-row or mid-number, and the notice says only
  "TRUNCATED" — not which grouping was cut or how many lines are missing. This is the one place
  the function breaks its own gap-disclosure invariant (contrast `NOT GROUPABLE`, `NOT COMPUTED`,
  `Z NOT shown`), and that invariant is what the digest prompt's "say plainly if the digest
  reports ... truncation" instruction depends on.

### Tier 2 — changes which facts reach the analyst; re-score before trusting

These alter selection, not just volume. Re-run `scripts/run_condenser_ab.sh` +
`scripts/score_condenser_ab.py` against SQL ground truth before adopting.

- [ ] **4. Rank groupings before truncating at `DIGEST_MAX_GROUPINGS`
  (`output_store.py:262-271`).** `combinations()` emits in column order and the list is then cut
  at 12, so with 5 key columns you get 5 singles + 10 pairs = 15 and pairs of the *earliest*
  columns win while anything involving the last column is dropped — with no relevance signal
  involved. Rank instead: prefer moderate cardinality, and boost keys whose names appear in
  `information_still_needed`. That field is already at the call site
  (`funda_agent_exp.py:1911`) and is the one available relevance signal the digest never sees —
  it would need threading into `digest_result`'s signature.

- [ ] **5. Sample text verbatims per group, not globally (`output_store.py:301-319`).** A
  high-cardinality text column is emitted as a globally distinct, alphabetically sorted value
  list, which destroys the row-level link between a comment and its product/question — so "what
  did respondents say about product X" is unanswerable from the digest. This is very likely the
  mechanism behind the 210 KB regression recorded in `funda_agent_exp.py:88-100` (real verbatims
  recovered with the condenser OFF, lost with it ON). Sample a few values per group of the first
  key column instead. Also drop the `sorted()`: it makes the shown subset systematically biased
  toward values starting with "A", and picks the same biased slice every run.

- [ ] **6. Give high-cardinality non-UUID columns a top-K instead of nothing
  (`output_store.py:236`).** Above `DIGEST_KEY_MAX_DISTINCT` a column is merely *named* under
  `NOT GROUPABLE` and gets no figures at all — a hard cliff at 26 distinct values. Always report
  top-K value counts plus a tail count, so the analyst gets the shape of the column even when a
  full cross-tab is refused.

### Cross-cutting, do in the same pass

- [ ] **Revisit `DIGEST_CHAR_CAP` and the per-group line budget.** Items 1 and 5 both grow the
  digest. Left alone, the increase is absorbed by truncation — i.e. by silently dropping the
  tail of the cross-tabs — which converts an accuracy gain into an accuracy loss. Size the cap
  deliberately rather than letting item 7's notice paper over it.

- [ ] **Minor: `left = DIGEST_CHAR_CAP - len("\n".join(out)) - DIGEST_TEXT_RESERVE`
  (`output_store.py:302`)** re-joins the whole output on every text column, O(n^2) in output
  size. Track a running length instead. Negligible today; free to fix while touching the block.
