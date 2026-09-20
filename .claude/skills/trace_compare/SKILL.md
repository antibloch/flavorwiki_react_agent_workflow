---
name: trace_compare
description: >
  The judge over `oracle_agent`'s references, for the FlavorAI agent
  (`funda_agent_exp.py`). `oracle_agent` produces and verdicts nothing; this
  procedure verdicts and produces nothing. Given the agent's budgeted trace, the
  oracle's TRAJECTORY LOG and final answer (its Phase 6, run in the Claude Code
  harness over equivalent tools), and the oracle's independently derived
  GROUND-TRUTH ANSWER (its Phases 0-5), it returns
  two separately labelled verdict blocks. The oracle's trajectory and answer are
  the IDEAL the agent is measured against; because the two run in different
  harnesses and share no tool names, steps are matched on normalized CAPABILITY
  (`sql_retrieval` <-> `nl2sql_tool`, `survey_statistics` <-> `run_survey_stats`,
  `plsr_analysis` <-> `analyze_plsr`, `word_cloud` <-> `generate_word_cloud`) and
  on the purpose each step recorded, never on mechanism. CONTENT: whether the transcript's
  numbers, matched question, product ranking and significance verdicts match the
  ground truth, plus how the agent's and the reference's final answers agree —
  where both agree and both contradict ground truth, that is a shared tool-layer
  defect, which a plain answer-vs-answer comparison would pass. TRAJECTORY: tool selection, call order, redundant calls, failed
  calls, error recovery, the coverage delta between the two final answers, and
  efficiency — judged on unbatched batchable work, budget exhaustion and unused
  retrieval, never on a raw call-count delta. CONTENT is graded on every probe;
  TRAJECTORY only when an oracle trajectory log is supplied.
  It does not verdict presentation or this repo's written output contract - chart
  shape, table structure, suggestion chips, disclosure timing - which is
  `contract_review`'s, and which needs an accepted-exception history this
  procedure does not have. The oracle's trajectory and answer are the ideal the
  agent is measured against, matched on normalized CAPABILITY and stated purpose
  rather than tool name, because the two run in different harnesses; both runs are
  single stochastic samples, so no finding may be stated as a rate; and content is
  decided by the ground-truth answer, never by the two runs agreeing. Use when PROTOCOL.md
  §6 step 4 calls for a content or trajectory verdict.
---

# trace_compare

You are the **judge**. `oracle_agent` derives the references and grades nothing; you grade
them and derive nothing. Two questions are yours, and they stay in **separately labelled
verdict blocks** — a correct number attached to a wasteful trajectory is two findings, not one,
and merging them is how the useful half gets lost:

- **CONTENT** — did the answer state the right numbers, match the right question, rank the
  products correctly, and read significance correctly? Graded against `oracle_agent`'s
  **Phases 0-5 ground-truth answer**, never by the oracle's and the agent's answers agreeing.
- **TRAJECTORY** — was the way it got there sensible? Graded against the **Phase 6 reference
  trace**.

The content block runs on every probe you are handed, unconditionally — no blast radius
excuses skipping it. The trajectory block runs only when an oracle trajectory log was
supplied; if one was not, say so, return the content block alone, and do not improvise a
trajectory verdict out of the agent's trace by itself.

You do not grade this repo's written output contract — chart shape, `NESTED_RESULT_RULES`,
benchmark banner, suggestion chips, disclosure timing, base-composition disclosure. That is
`contract_review`'s, and grading it needs the accepted-exception history in
`probe_scratchpad.md` that you do not have. Where you see one, name it and route it.

Four rules outrank everything else below.

1. **The oracle's trajectory and answer are the ideal; the agent is measured against them.**
   Where the agent did something the oracle did not need to do, or failed to do something the
   oracle did, that is a finding by default. But **compare at the level of information, not
   mechanism**: the two run in different harnesses and share no tool names, so a step matches
   when it reached the same information for the same stated reason — the oracle's `query.sh`
   against the agent's `nl2sql_tool`, its `stats_api.py` against `run_survey_stats`. Sequencing
   that differs while reaching the same evidence in the same order of dependency is not a
   finding; say so under **observed differences** and move on. Every finding still names what
   it violates, because an unattributed finding is one the next session cannot act on.
2. **Correctness is settled by the ground-truth answer, never by agreement between the two
   runs.** The reference agent binds the *same five tools* as the agent under test — full
   parity — so where that tool layer is wrong, both runs are wrong together and their agreement
   proves nothing. The Phases 0-5 answer is the only reference that does not share those tools:
   it is derived over `query.sh` and `sql_stats.py`, outside them entirely. So compare all
   three, and read the pattern:
   - agent agrees with reference, both match ground truth → content PASS;
   - agent disagrees with reference, ground truth sides with the reference → agent defect;
   - agent disagrees with reference, ground truth sides with the agent → the *reference* went
     wrong; report it, and grade no trajectory point against it on that claim;
   - **agent and reference agree and BOTH contradict ground truth → a shared tool-layer
     defect**, owner `tool payload` or `external dependency`, never `prompt rule`. This is the
     single most valuable thing this procedure can find, and the one a two-way answer-vs-answer
     comparison would silently pass.
3. **Two single runs.** `PROTOCOL.md` §6.1 budgets one run per probe on both sides. Every
   finding is evidence for the observed runs only. Never state a rate, never say "the agent
   usually", never recommend re-running to see if it reproduces. And **truncated is not
   absent**: the packet caps each tool call at 500 characters and each tool output at 1000,
   marking cuts (`... [log-truncated: N of M chars]`). A payload cut off is UNTESTED, and you
   name the field that would settle it.
4. **A claim you have no derived value to check against is UNVERIFIED, not a pass.** Say which
   figure is unchecked and what would have settled it. Never infer a content mismatch from text
   that was truncated or elided: a number missing from a cut tool output is missing from the
   *log*, not from the agent's evidence.

## Inputs

You need five things. If any is missing, say which, and grade only what the rest supports.

| input | what it is for |
|---|---|
| agent trace | `funda_agent_exp.py`'s budgeted run — the thing under test |
| oracle trajectory log | `ORACLE_TRACE_FILE` JSON lines from the oracle's Phase 6, rendered by `scripts/trace_log.py` — the TRAJECTORY standard. Each step carries `capability`, its `counterpart` funda tool, the stated `purpose`, arguments, outcome and error |
| oracle final answer | the oracle's own rendered answer — the COVERAGE standard |
| oracle ground-truth answer | `oracle_agent` Phases 0–5 — the CONTENT standard, and what checks the oracle's own answer (rule 2) |
| case record | `PROTOCOL.md` §6's record — requirement, diagnosis, blast radius |
| probe kind | discrimination / fix / regression |

Without the ground-truth answer there is no content verdict at all: say so, mark CONTENT
UNVERIFIED, and grade trajectory only. Do not derive the answer yourself — that is
`oracle_agent`'s job, and a judge that manufactures its own standard is no check on anything.

Both traces carry a manifest header. Check them before grading: **same prompt, same scope,
same survey**. Different scope means you were handed two different probes — stop and say so.
Record both run locators and the source hashes in your report; a verdict not bound to a
revision goes stale silently (§6 step 3).

## Harness asymmetries that are NEVER findings

The oracle runs in the Claude Code harness and the agent in its own LangGraph loop. These
differences follow from that and are never defects:

- **No shared tool names.** Match on `capability` / `counterpart`, never on the literal name.
  "The oracle never called `nl2sql_tool`" is not a finding.
- **Different step granularity.** One oracle `query.sh` call may cover what took the agent two
  `nl2sql_tool` calls, or the reverse. Compare information reached, not step counts.
- **No presentation contract on the oracle side.** Its answer has no suggestion chips, no
  `gpi-chart`, no `gpi-nested-table`, no benchmark banner. Never compare presentation between
  the two — that is `contract_review`'s, against rule text.
- **Oracle-only capabilities.** `scope_resolution` (the agent is handed its scope) and
  `sql_stats.py` (the agent's only statistics route is the Charts API). A figure the oracle
  computed locally is a capability gap, not an agent error.
- **PLSR is one shared tool, not two.** `plsr_ref.py` invokes `analyze_plsr` itself, so both
  runs get the identical payload. Agreeing PLSR figures are **one** derivation and prove
  nothing about it; a PLSR defect is invisible to this procedure by construction. Mark any
  load-bearing PLSR figure UNVERIFIED for content and name §5's scikit-learn fixture check as
  what would settle it. A *difference* in PLSR output between the two runs can then only come
  from different arguments — which is a real trajectory finding, and worth saying so.
- **The oracle does not emit artifacts.** No word-cloud block, no chart payload — it prints
  terms and figures. Compare the *terms and figures*, not the rendering.
- **The agent starts with a pre-fetched inventory; the oracle does not.** `funda_agent_exp.py`
  attaches `get_survey_analysis_packet`'s payload to turn 0 automatically. The oracle has no
  counterpart for it and must retrieve those facts itself. Two consequences, both never
  findings: the agent legitimately answering an inventory-covered question with **zero or few
  tool calls**, and the oracle spending **extra steps** to reach what the agent was handed.
  Where the agent's manifest shows `inventory=0` (`--no-inventory`), this asymmetry is absent
  and the two start level — check the manifest before reasoning about it either way.
- **Only the agent can be refused.** `nl2sql_tool` carries a semantic filter, a survey-boundary
  check and scope repair; `query.sh` has none of them and cannot refuse. So `B4` is structurally
  one-sided: a guardrail refusal on the agent side has no oracle counterpart and is not evidence
  the agent did worse. Judge it on whether the agent *recovered*, never on the oracle not having
  hit one.
- **Only the agent has a step budget.** `MAX_LLM_STEPS` bounds the agent; the oracle's harness
  has no equivalent. `B7`'s budget-exhaustion criterion is therefore a one-sided defect check on
  the agent, not a comparison — say so when it fires, so "the oracle finished and the agent did
  not" is not read as a like-for-like result.
- **Different models.** The oracle runs on the Claude Code harness's model, the agent on
  `MODEL_NAME`. A trajectory difference can be a model difference, and one pair cannot tell
  that from a policy difference. Never attribute a finding to the agent's *prompt* on trajectory
  evidence alone.

## Build the trajectory table first

Before any verdict, lay both runs side by side. This table is the evidence every verdict below
cites, and it is the part a reader will reuse.

```
step | agent: tool(args digest) -> outcome        | reference: tool(args digest) -> outcome
-----+--------------------------------------------+----------------------------------------
```

For each call record: tool name, the identifying arguments (question id, measure, sql shape —
not the whole payload), and the outcome (rows / figures / `SQL ERROR` / `REFUSED` / zero rows /
timeout / truncated-in-log). Close the table with, for each side:

- **total calls**, and calls by tool;
- **halt reason** — model-selected answer, budget-forced synthesis with tools removed (the
  `STEP_BUDGET_NOTICE` turn), trusted-artifact short circuit, or execution failure. A
  tool-free final message alone does not identify the branch; if the trace cannot distinguish
  them, record the halt reason as **unverified**. `PROTOCOL.md` §6 step 0 treats this as an
  axis of its own, and it is usually the fastest explanation of a coverage gap.

## Block A — the CONTENT verdict

Graded against the **ground-truth answer**. The reference agent's own answer is compared too,
but only as corroboration — it never overrides ground truth (rule 2). Five checks, each
**PASS**, **FAIL** or **UNVERIFIED**:

**A1. Numbers.** Every figure the answer states — means, counts, percentages, N, test
statistics, p-values — against the derived value. Quote the transcript's claim and your
reference value side by side for any mismatch.

**A2. Matched question.** Did it answer the measure the prompt asked about? A near-duplicate
matched confidently ("APPEARANCE liking" for "OVERALL liking", "describe the SWEETNESS" for
"LIKE or DISLIKE the SWEETNESS") is a FAIL even when every number under it is arithmetically
right — and it is the most common content defect in this dataset.

**A3. Ranking.** Product or segment order, and any "highest / lowest / leads" claim.

**A4. Significance.** Whether a difference was called significant, non-significant or
inconclusive, and whether that reading is licensed by the test actually run. "Not significant"
asserted from an underpowered split is a FAIL; "inconclusive" on the same evidence is not.

**A5. Agreement with the reference answer.** State, for each contested figure, whether the
agent and the reference agree, and what ground truth says about the pair — the four-way reading
in rule 2. This is a *comparison*, and the verdict still comes from ground truth: two runs
agreeing is not a pass and disagreeing is not a failure until ground truth decides which one is
right. Where both agree and both are wrong, name it a shared tool-layer defect and attribute it
to `tool payload` or `external dependency`.

Three things constrain this block, all of them earned the hard way in this repo:

- **A content FAIL means investigate, not automatic reject.** The ground truth was derived
  without reading `funda_agent_exp.py`'s own tool code, so a transcript number that is correct
  under a real, documented tool convention — a rounding rule, a default parameter, a fixed
  alpha — can surface as a false mismatch. Confirm against the tool's own contract in
  `tool_prompts.py` before calling it a defect.
- **A statistic the oracle computed locally is one the agent could not have produced.** The
  agent's only statistics route is the Charts API; `sql_stats.py` is the oracle's alone. Where
  the ground-truth answer flags a locally-computed figure, its absence from the transcript is a
  capability gap, not an agent error.
- **Never let the two answers agreeing settle content.** The oracle reaches the same Charts
  API the agent does, so a wrong figure there appears in both (rule 2).

## Block B — the seven TRAJECTORY verdicts

Each is **PASS**, **FINDING**, or **UNTESTED** (evidence cut off or never captured). Every
FINDING quotes both sides and names the criterion it violates.

**B1. TOOL SELECTION AND ARGUMENTS.** Did the agent reach a tool that can actually answer what
was asked, with arguments that address the question asked?
FINDING when it used a tool that cannot answer it (a SQL pull where only the statistics engine
has the test), skipped the only tool that can and answered anyway, or called the right tool
with the wrong target — a statistics call on a question id that is not the matched measure, a
PLSR run on an attribute set the prompt did not ask for, a scope id that is not this run's.
Argument *form* against the written contract (bound `:survey_id`, camelCase quoting) is
`contract_review`'s; what is graded here is whether the arguments point at the right thing. Not a finding: a different valid tool that got correct
evidence, or zero calls where the inventory already held the facts.

**B2. CALL ORDER.** FINDING when an ordering made a later call act on evidence it did not yet
have — statistics issued before the question id was resolved, a PLSR run before the attribute
set was known — and the trace shows the consequence. Not a finding: any order that arrived at
correct evidence, or more/fewer round trips than the reference.

**B3. REDUNDANT CALLS.** FINDING when a call retrieved nothing the run did not already hold:
an identical repeated query (the agent's own `[sql]` repeat warning is direct evidence), a
re-query of facts the pre-fetched packet already supplied, or a second call whose result the
final answer never uses. Not a finding: exploratory calls that narrowed the question, or a
deliberate cross-check of one source against another.

**B4. FAILED CALLS.** Classify every failure on both sides — SQL error, guardrail `REFUSED`,
zero rows, timeout, tool-internal failure. FINDING when a failure was left unaddressed and the
answer proceeded as though it had not happened. A bare count difference is not a finding —
both runs face the same guardrail and can provoke it differently — but with full tool parity a
failure one side hit and the other did not is now a real signal about the query each wrote, so
say which call differed and how.

**B5. ERROR RECOVERY.** FINDING when the agent retried an identical failing call unchanged,
silently abandoned a measure after a failure, or stated a value it never successfully
retrieved. PASS when it corrected the call and re-ran, or stopped and said plainly what it
could not get. Compare with how the reference handled the same failure where both hit one.

**B6. OUTPUT COVERAGE DELTA.** Strictly about *coverage*: what the
reference answer covers that the agent's does not, and the reverse — measures, products,
subgroups, caveats that change the reading. Both runs can reach the same information — that is
what the oracle's tool set is for — so a coverage gap is a real asymmetry, unless the oracle
recorded that something was unretrievable for both. FINDING when the agent silently dropped something
the prompt asked for and the reference retrieved. Keep it about coverage: a figure that is
present but *wrong* is Block A's, not this one. Route out, do not verdict: a rendering, chip or
table-shape difference (→ `contract_review`), and absolute task fulfilment (→
`contract_review`, which owns it under §6 step 4).

**B7. EFFICIENCY.** Not a call count, and never "the reference did it in fewer". Raw round-trip
deltas between two single stochastic runs are variance, and grading them manufactures findings
(rule 1). Three things are observable and are not variance:

- **Unbatched batchable work.** `call_tool` fans a homogeneous batch of `nl2sql_tool` or
  `run_survey_stats` calls emitted in ONE assistant message, logging `[tool-batch-timing]
  tool=… calls=N`. FINDING when the agent issued several homogeneous calls across separate
  turns with no data dependency between them — each one waiting on the last for no reason —
  and no `[tool-batch-timing]` line appears. Not a finding when a later call genuinely needed
  an earlier result, which is most of the time; say which dependency you checked.
- **Budget exhaustion.** FINDING when the run hit `MAX_LLM_STEPS` and was forced into synthesis
  with tools removed (the `STEP_BUDGET_NOTICE` turn) while the reference answered the same
  question inside the budget. This is the *gave up early* / *answered less than it retrieved*
  class, and the halt reason in the trajectory table is the evidence.
- **Retrieval the answer never used.** Overlaps B3 by design; record it once, in whichever
  place you found it, and cross-reference rather than reporting it twice.

Where none of the three applies, B7 is a PASS even if the agent took twice the calls — and say
so explicitly, with both counts, so the next reader knows the difference was seen and dismissed
rather than missed.

## Report format

```
TRACE COMPARE — <Suite / Case>, <probe kind>, single run each
agent run     : <thread / locator / hashes>
oracle run    : <trajectory log path / source hashes>
reference validated against oracle ground truth: YES / NO (<which claims are compromised>)

<trajectory table>
agent    : N calls (<by tool>), halt=<reason>
reference: N calls (<by tool>), halt=<reason>

CONTENT VERDICT (vs ground-truth answer)
A1. NUMBERS          : PASS | FAIL | UNVERIFIED — <claim vs derived value>
A2. MATCHED QUESTION : ...
A3. RANKING          : ...
A4. SIGNIFICANCE     : ...
A5. AGREEMENT        : agent vs reference vs ground truth — <pattern, per rule 2>

TRAJECTORY VERDICT (vs oracle trajectory log)
B1. TOOL SELECTION   : PASS | FINDING | UNTESTED — <evidence>
B2. CALL ORDER       : ...
B3. REDUNDANT CALLS  : ...
B4. FAILED CALLS     : ...
B5. ERROR RECOVERY   : ...
B6. OUTPUT COVERAGE  : ...
B7. EFFICIENCY       : ...

OBSERVED DIFFERENCES, NOT DEFECTS: <the ones a reader would otherwise ask about>
ROUTED TO contract_review : <rule / rendering / task-fulfilment items>
UNVERIFIED / UNTESTED     : <what was cut off or never derived, and what would settle it>
```

Report the two blocks separately even when both are clean, and never collapse them into one
verdict: a right answer reached wastefully and a wrong answer reached efficiently are
different problems with different owners.

Every FINDING or FAIL names the component it belongs to, the same five `PROTOCOL.md` §6 step 4 uses:
**prompt rule**, **tool payload**, **runtime code**, **memory mechanism**, or **external
dependency**. A finding with no component attributed sends the next session back to guessing.
Where the agent and the reference diverge and the *reference* matches ground truth while the
agent does not, say which boundary the divergence first appears at — that is the trajectory
evidence §6 step 0 came here for.
