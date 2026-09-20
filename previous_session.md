# Session Handoff

## Objective and Current State

`funda_agent_exp.py` is a LangGraph ReAct survey analyst with a production mirror under
`deployment/`. This session fixed a reported regression: after a "summarize this survey" turn, a
follow-up that should produce a `gpi-nested-table` often produced a flat table instead, while the
same follow-up asked cold always nested correctly. Root cause was the `KEEP SUMMARIES SIMPLE`
block added in deployment commit `b53e689`. It is fixed, A/B verified, and shipped to
`deployment/`.

An earlier read-only review of the chart/plot harness in the same session found seven defects.
**None were fixed.** They are listed under Known Issues and are the largest piece of open work.

## Repository and Runtime

- Workspace: `/home/junaid/codework/Flavorwiki/flavorai_v2/final_agent_work_v5_base_2_optim3_exp`.
- `deployment/` is its **own git repo** nested inside the project, not a subdirectory of the outer
  repo. Its history (`b53e689`, `0ef4012`) is invisible to `git log` at the outer root.
- Main files: `funda_agent_exp.py`, `api_funda_agent_exp.py`, `agent_instructions.py`,
  `tool_prompts.py`, `docs/API_CONTRACT.md`, `tests/test_agent_optimizations.py`.
- Use `.venv/bin/python` for tests and compilation. LLM provider is **OpenAI**
  (`langchain_openai`, `MODEL_NAME = "gpt-5.5"` hardcoded at `funda_agent_exp.py:126`).
- Local DB: `docker start gpi-db` (postgres on `localhost:5433/gpi_sample_db`). Env comes from
  `../BE/.env`, resolved as `Path(__file__).resolve().parent.parent / "BE" / ".env"`.
- Outer worktree contains extensive user-owned staged/untracked changes. Preserve unrelated
  changes and do not assume anything is committed.
- Do not run the full regression workbook playbook unless explicitly requested.

## User Decisions and Preferences

- Summaries must be simple and accessible to a non-technical audience, and must NOT use nested
  tables — nesting signals complexity, which is what the user does not want in a summary.
- Nested tables must still work everywhere else, including on a follow-up turn after a summary.
- PCA defaults to a 2D biplot. 3D is an explicit option and may be suggested when PC3 exists.
- PCA charts and word clouds are trusted tool-owned artifacts; generic model-authored charts are
  bounded/sanitized and must use the `gpi-chart` JSON contract.
- Never report or rank products using pooled multi-attribute means unless explicitly asked.
- Keep respondent/submission counts attached to their specific question or measure.
- Keep suggestions scoped to the requested subject.
- Working style: smallest correct diff, one approved substep at a time, no adjacent cleanup.

## Completed Work This Session

### Diagnosis of the summary/nesting regression

`b53e689` added 19 lines of prompt text and no code or tests. Problems found:

1. The trigger was topic-scoped, not request-scoped, so it carried across turns.
2. `"or product comparison"` pulled in the request class that most needs nesting.
3. `"survey summaries and survey-summary requests"` was unbounded and self-referential.
4. It claimed to override `NESTED_RESULT_RULES` while being emitted *before* it
   (`build_system_prompt`: `TASK_CONFIG["expected_output"] + NESTED_RESULT_RULES + ...`).
5. `POOLED MEANS`, added in the same commit, forces per-attribute breakout — more dimensions —
   while the flat-table ban forbids nesting them. The two rules pull in opposite directions.
6. It contradicted `INVENTORY_PREAMBLE`'s pre-existing "apply the adaptive hierarchical JSON
   rules", which is pinned at message 0 by `_trim_history` and therefore live on every turn.
7. The escape hatch ("explicitly requests hierarchical output") was narrower than the trigger.

Also relevant: the only data-attached nesting cue is `_wide_result_hint`
(`funda_agent_exp.py:765`, thresholds `2 <= rows <= 12`, `>= 7` numeric measures, `>= 1` label
column). It is attached **only** to `nl2sql_tool` results (`funda_agent_exp.py:823`), never to the
analysis packet or pre-fetched inventory, and it does not fire on long/tidy result shapes.

### Changes applied

- **Part 1 + 2** — rewrote `KEEP SUMMARIES SIMPLE` (`agent_instructions.py:283`): classify the
  CURRENT user message, explicit exit list (by/per product, attribute, domain, question, segment,
  wave; break down, split, detail, drill into, cross-tabulate), bounded precedence
  ("governs THIS answer only"), dropped `"or product comparison"`, kept a summary-scoped audience
  clause.
- **Part 4** — added `test_summary_rule_is_scoped_to_the_current_request`
  (`tests/test_agent_optimizations.py:1815`) with 6 assertions, deliberately in its own method.
- **Substep 2** — moved the `SYSTEM_HASH` `assertEqual` from the FIRST line of
  `test_system_prompt_hash_matches_intentional_prompt_update` to the LAST. Previously all 104
  content assertions in that method were unreachable whenever the hash moved, so a failed hash
  would be bumped and any clause dropped in the same edit went unreported.

### Tried and reverted

- **Substep 1** — an always-on `WRITE FOR THE READER` rule, to give jargon control when no
  inventory is attached. **Measured inert and reverted.** With inventory both arms were 6/6
  jargon-clean (`INVENTORY_PREAMBLE`'s `PLAIN LANGUAGE` already covered it); with
  `--no-inventory` both arms were **0/6** clean. `line-scale` leaked in 11/12 runs regardless.
  It was reverted for being dead weight, NOT for causing a regression. If revisited, the rule
  needs substitutions (`line-scale (say sliding scale)`), not prohibitions — the one term that
  has a substitution (`pooled (say combined)`) behaved better than the ones that do not.

## Verification

- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`: **117 tests passed**.
- `py_compile` clean; `git diff --check` clean; root/deployment `agent_instructions.py` parity OK.
- Prompt hash fixture is current: `0400faab926d7f31c31c94ff06a451cc36a005738518619b46edd461ca9f60c8`.
- **Live A/B, 18 runs per arm**, isolated copies differing only in `agent_instructions.py`
  (arm A = `b53e689`, arm B = fixed), same DB, env and model:

  | | turn-2 nested | turn-1 nested (unwanted) |
  |---|---|---|
  | A (pre-fix) | 11/18 (61%) | 0/18 |
  | B (fixed) | **18/18 (100%)** | 1/18 |
  | one-sided Fisher | **p = 0.0038** | p = 0.50 |

  Note the original bug was **intermittent, not deterministic** — pre-fix still nested 61% of the
  time. The 1/18 summary that nested is the cost of bounding the override; not significant.
- Regression-test value confirmed twice by simulation: reverting the prompt block, or dropping
  `CHART RECOVERY` / `NORMALIZE BEFORE CHOOSING DEPTH`, now fails **by clause name** instead of
  only by digest. Dropping 3 clauses yields 2 failures — one per test *method*, not per clause.
- SD-column variation between runs is intra-arm noise (arm B: 15/18 with an identical prompt),
  not attributable to any edit.

## Reproducing the tests

Two-turn regression check (turn 1 is `--prompt`, turn 2 is piped stdin, one thread):

```bash
docker start gpi-db && sleep 3
echo "Break down the mean score for every attribute of every scored measure, by product and by attribute." | \
  .venv/bin/python funda_agent_exp.py --thread-id t1 --prompt "Summarize this survey" --chat > /tmp/run.txt 2>&1
grep -n '===== AI MESSAGE =====\|gpi-nested-table' /tmp/run.txt
```

Pass = two `AI MESSAGE` blocks with `gpi-nested-table` only after the second.

For an A/B, copy `funda_agent_exp.py agent_instructions.py tool_prompts.py feature_influence.py
output_store.py` into two scratch dirs, swap `agent_instructions.py` in one, and symlink a `BE`
directory into the scratch parent so `../BE/.env` still resolves. 12 parallel runs take ~60s.

## Known Issues and Next Steps

### Chart/plot harness — 7 defects found, none fixed

1. **`funda_agent_exp.py:3647,3658,3661`** (deployment `:3582,3593,3596`) — a rejected `gpi-chart`
   block is resurrected when a word-cloud block is also present. The word-cloud branch is `if`,
   not `elif`, and rebuilds from the original `response_text` rather than the sanitized content.
   Reproduced with `"type": "totally_made_up"`. Highest priority.
2. **`:1019`** — chart type aliases are validated but not normalized, so `"type": "bar-chart"`
   reaches the frontend un-normalized. The prompt (`agent_instructions.py:303`) also points at
   "the documented aliases", a list that exists nowhere in the prompt or `API_CONTRACT.md`.
3. **`:3565`** — `_filter_chart_suggestions` silently deletes valid suggestions
   (`{{Graph ...}}`, `{{Visualize ...}}`, `{{Show a PCA map ...}}`), while `CHART RECOVERY` says a
   corrected chart MUST end with suggestions. Compliant answers can ship with zero buttons.
4. The 300-point cap is shape-dependent: a heatmap matrix of 100x300 counts as 100;
   `series[].points[]` counts as 0. `docs/API_CONTRACT.md:173` states the cap as a guarantee.
5. Numeric validation only covers scatterplots. `bar_chart` with `"value": "not a number"` and
   `line_chart` with `"y": null` both pass.
6. **`:1023`** — the unsafe-markup check tests only `<script` and `javascript:`. Verified passing:
   `<iframe>`, `<svg onload=>`, `<img onerror=>`, `data:text/html`. `API_CONTRACT.md:138`
   forbids all of them.
7. `CHART RECOVERY` contradicts itself on suggestion count: `agent_instructions.py:320` says
   "1-3", `:323-325` says "up to 4 / choose four".

### Other open items

8. **Part 3 (deferred by user decision)** — summaries still enumerate every attribute because
   `POOLED MEANS` forces per-attribute breakout. Sizing: `INVENTORY_PREAMBLE` already mandates
   "ALWAYS name the attributes listed in `order_differs`", and on survey
   `39af3240-42a8-4e35-8c7d-c61703d5ce3f` that is 7 of 13 attributes — so the realistic gain is
   13 -> 7, not a short summary. It is the only change that removes information from an answer.
9. `funda_agent_exp.py:112` has a live-looking `OPENAI_API_KEY` hardcoded as a fallback default
   in committed source.
10. The 104 content assertions still surface one failure at a time (one per test method).
    Splitting them into themed methods is a 157-line restructure, judged not worth it.
11. Frontend chart/nested-table rendering is documented but not browser-verified here.

## Commit State

- `deployment/`: clean, committed as `0ef4012` and **already pushed** to `origin/main`. Contains
  Parts 1+2, without substep 1. Verified against `origin/main:agent_instructions.py`.
  Part 4 is test-only and the deployment repo has no `tests/`, so nothing is owed there.
- Outer repo: `agent_instructions.py` and `tests/test_agent_optimizations.py` are **uncommitted**.

## Resume Instructions

Continue from this file. Preserve root/deployment parity for `agent_instructions.py` and
`tool_prompts.py` (both currently byte-identical); `funda_agent_exp.py` and
`api_funda_agent_exp.py` diverge intentionally for production. Before changing prompts, update
`SYSTEM_HASH` in `tests/test_agent_optimizations.py`; then run the full unit suite, `py_compile`,
`git diff --check`, and the parity check. Keep the summary rule request-scoped — that scoping is
what the A/B measured. Avoid the regression playbook unless explicitly requested.
