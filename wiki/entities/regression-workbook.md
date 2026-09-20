---
type: entity
kind: dataset
title: "regression.xlsx — the regression workbook"
aliases: ["regression.xlsx", "regression_cleaned"]
tags: [benchmark, agent-variant]
sources: [final-agent-work-bundle-readme, client-boundary-session-handoff]
created: 2026-08-14
updated: 2026-08-14
---

The consolidated test-case workbook for [[funda-agent-exp]]: 69 rows in sheet `regression_cleaned`, 7 columns. It exists because the prompts that actually surfaced defects were previously scattered across six scripts and two markdown files with no single list.

## Composition

Two appended columns, **Suite** and **Case**, partition the rows:

| Suite | Rows | What it catches |
|---|--:|---|
| *(blank — the original set)* | 33 | The pre-existing regression set |
| Needle in haystack | 9 | Picking the question that *means* "overall liking", and refusing when none exists (T4/T5 are absence cases) |
| Large SQL output | 8 | Payloads that park and fire a condenser — E1–E4 and the condenser A/B set L1–L4 |
| Model routing | 3 | The strong→weak latch and the lean tool binding |
| Suggestions / ambiguity | 4 | The `{{...}}` button contract, and when buttons must *not* appear |
| Short-term memory | 12 | One 12-round conversation on a single `thread_id`, in order |

Regenerate or extend with `scripts/add_debug_cases_to_regression.py`, which is idempotent and supports `--dry-run`.

## Four things to know before using it

1. **`run_regression_exp.sh` still sweeps `seq 1 18`.** The appended rows do not change what an existing regression run does. Widen the sequence, or target a single row with `bench_regression_exp.py --row N`.
2. **This is not the repo's `regression.xlsx`.** The repository copy is sheet `Sheet1` — 18 populated rows across 27 columns, carrying scoring columns and roughly 968 trailing blank formatted rows. This workbook's 33 original rows are a strict **superset** of the repo's 18, so nothing was lost, but `seq 1 18` covers only the first 18 of 69 here.
3. **The `Type of Query` column carries the expectation, not just a label** — which rows must refuse, expected payload size, expected routing, and two known failures: memory round 12 dropping rounds 1–6, and the stats-only run never latching. The second of these is a [[model-and-prompt-routing]] behaviour; the first belongs to [[conversation-memory-and-trimming]].
4. **The 12 memory rows are one conversation**, not 12 independent prompts. Running them standalone tests nothing about memory.

`bench_regression_exp.py::load_rows` reads by column *position*, so Suite and Case were appended at H and I where it ignores them; the README records this as verified — all rows still parse and rows 1–18 are unchanged.

## Cross-agent benchmarks are not runnable here

Four scripts were deliberately left out of this bundle because they compare this agent against sibling agents and need files outside it: `bench_needle.py` and `run_needle_bench.sh` (need `funda_agent_v1.py`), `run_drill_ablation.sh` (needs `funda_agent.py`), and `summarize_drill_ablation.py` (needs their output). Their results are written up in `docs/emp_findings.md` and `docs/needle_haystack_benchmark.md` — neither yet ingested into this wiki.

`funda_agent_exp_oracle_duo.py` **is** included and must stay byte-identical: it is the pre-memory baseline arm the memory benchmarks A/B against.

## Current testing status

[[client-boundary-session-handoff]] records a focused automated run passing **79/79** tests after the client-boundary implementation, but that run predates the later response-count and terminal-refusal refinements, and no automated rerun has happened since. Two user-supplied live traces stand in as behavioural evidence. The handoff also records a standing instruction that tests are to be run by the user personally rather than by an agent.

## Related

[[funda-agent-exp]] · [[model-and-prompt-routing]] · [[suggestion-button-contract]] · [[conversation-memory-and-trimming]]
