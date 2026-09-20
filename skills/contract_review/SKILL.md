---
name: contract_review
description: >
  Behavioral / presentation-contract grader for the FlavorAI agent
  (`funda_agent_exp.py`). Given a transcript (tool calls, tool outputs, and
  rendered answer) and the area of the change, checks it against the live
  rule text in `agent_instructions.py` / `tool_prompts.py` — chart shape,
  `NESTED_RESULT_RULES` table structure, benchmark banner, suggestion-chip
  contract, base-composition disclosure timing, and the tool calls themselves
  (tool chosen, arguments, batching, order) — and against
  `probe_scratchpad.md`'s already-accepted blemishes and partial fixes, so a
  known, accepted gap isn't re-reported as a new defect. Also judges the two
  things no written rule covers: whether the answer fulfilled the task it was
  given, and whether every form it emitted is one the harness itself defines
  — a form the prompt authorizes but neither the prompt nor the runtime
  validates is a harness defect, not an untestable gap. Does not check whether
  numbers or statistics are correct — that is `trace_compare`'s content
  verdict, graded against `oracle_agent`'s derived ground-truth answer.
  On a PROTOCOL.md §6 step-0 discrimination probe it additionally returns the
  causal-localization verdict — SUPPORTS / CONTRADICTS / INCONCLUSIVE for the
  proposed owning boundary, read off step 0's own boundary chain.
  On post-fix probes it separately verifies the recorded correction or
  preservation obligation against execution evidence for the evaluated revision.
  Use when PROTOCOL.md §6 step 4 calls for grading a discrimination, fix or
  regression probe's transcript against this repo's own output contract, or
  whenever asked to check whether a transcript followed a specific
  rendering/behavior rule.
---

# contract_review

You grade one thing: whether a `funda_agent_exp.py` transcript followed this
repo's own presentation/behavior contract. You do not compute or re-derive
survey statistics, matched questions, or product rankings. `oracle_agent`
derives those (its Phases 0-5) and `trace_compare` verdicts them. If asked about
content correctness, route it to `trace_compare` instead of guessing.

Two rules outrank everything else below:

1. **Quote the rule, don't paraphrase it.** Read the exact text out of
   `agent_instructions.py` / `tool_prompts.py` at grading time. These rules
   change independently of this skill and have already been rewritten more
   than once for the exact defects this check exists to catch (see
   `probe_scratchpad.md`) — grading from memory reproduces the bug you're
   checking for. For a pre-fix discrimination run, use the preserved rule excerpts in
   its §6 case record for rules changed since that run; mark missing historical rule
   evidence UNTESTED rather than applying the new wording retroactively.
2. **A rule with no transcript evidence either way is UNTESTED, not a
   pass.** Say what evidence would have settled it.

## Inputs

You need the transcript being graded (tool calls, tool outputs, rendered
answer) and which `Suite`/`Case` (from `regression.xlsx`) or area of
`agent_instructions.py`/`tool_prompts.py` the change touched. If neither is
given, infer the area from what the transcript is actually doing — a chart,
a nested table, a benchmark comparison, a suggestion chip — rather than
checking every rule in both files against every transcript.

**Which probe is this?** `PROTOCOL.md` §6 runs three, and they ask different
questions of you:

| probe | from | what you return |
|---|---|---|
| discrimination | step 0, before any fix | the rule verdicts **and** the causal-localization verdict below |
| fix | step 3, after the fix | rule verdicts and correction verdict for the required change |
| regression | step 3, after the fix | rule verdicts and correction verdict for the required preservation |

If nobody says which, ask, or say which one you assumed. A discrimination
probe graded as an ordinary fix probe silently drops the verdict step 0
depends on.

**What the packet contains, and what it does not.** `PROTOCOL.md` §6's
transcript budget keeps the user prompt, scope and rendered final answer
intact, truncates each serialized tool call to 500 characters and each tool
output to 1000, and marks every cut (`... [log-truncated: 500 of N chars]`).
Two consequences you must respect:

- **Truncated is not absent.** Never read a cut payload as evidence that
  behaviour did not happen. If the decisive evidence lies past a cutoff, the
  item is UNTESTED (or INCONCLUSIVE, for a causal verdict) and you name the
  exact field or line that would settle it. First recover that excerpt from the
  same run's full conversation records or diagnostic logs, with its run locator;
  CLI/API conversation records retain full arguments and public tool outputs,
  but not every internal transition. Do not replace missing evidence with a
  different run or infer state from these records. The
  captures are env-controlled — `LOG_TOOL_CALL_CHARS` and
  `LOG_TOOL_OUTPUT_CHARS`, `0` meaning uncapped — so say so when the fix is
  to re-capture, subject to §6.1's one-run budget. An evidence gap does not
  authorize repeated stochastic trials.
- **Elided inventory is unavailable evidence, not proof of the answer's source.**
  `[pre-fetched inventory elided from log: N chars]` means that inventory is absent
  from the log. A tool-free answer can also use retained evidence or a trusted artifact;
  establish the actual path before attributing its claims to inventory. Grade against
  the evidence available in the packet and mark only checks needing omitted evidence
  UNTESTED. Neither absent tool calls nor an elision marker proves invention by the model.

**The run log's third band is evidence, not noise.** Where you are given the
whole log rather than the message transcript, the harness's own lines carry
behaviour nothing else records — `[analysis-packet] degraded to N chars` /
`cache hit` / `empty` (what the model could possibly have known),
`[sql] repaired scope id(s)` (a corrupted id the harness silently fixed),
`[sql] REFUSED by survey boundary` and the other `REFUSED` lines (a tool
declined and the model continued), `[tool-batch-timing] calls=N` (whether
batching actually happened), `[progress-delta] status=…` (whether progress
memory was written). For a causal verdict these are usually the lines that
decide it.

## Before grading — check for an accepted exception

Read `probe_scratchpad.md`'s status table for the `Suite`/`Case` under test.
Some rows are deliberately not full passes — "known blemish, not a fail",
"partially fixed, residual accepted" — with a stated reason. Grade against
that stated boundary, not a hypothetical fully-clean transcript: matching the
accepted residual exactly is a PASS; a regression *past* it is a FAIL.
Skipping this step is how an already-known, accepted gap gets reported as a
new defect.

## Grading

Pick the rule set that matches what the transcript is doing:

| Transcript shows | Check against |
|---|---|
| a chart | chart-shape / axis / series-alignment / point-cap rules, **plus the two checks below** |
| a nested table | `NESTED_RESULT_RULES` structure and its worked examples |
| a benchmark comparison | benchmark banner disclosure and comparison-table rules |
| suggested next questions | the clickable-suggestion-chip contract |
| a base-size disclosure | base-composition disclosure-timing rules |
| tool calls | the call itself — tool chosen, arguments, batching, order — against that tool's description in `tool_prompts.py`, any stated ordering constraint, and the supported parallel-batch conditions in `PROTOCOL.md` §2 |

For each applicable rule: quote the rule text you read, quote or describe the
matching part of the transcript, and give PASS / FAIL / UNTESTED (checked
against any accepted exception from the step above). List every rule you
checked, not only the ones that failed — a report with only failures cannot
be told apart from one that checked nothing else.

On tool calls, judge the **arguments**, not only the order: the question id or
measure the call actually carried, the scope it filtered on, and any flag the
probe's own pass string names (`pca_plot_dimensions`, a product filter, a
`question_id`). You are the only check on the argument against the written
contract: `trace_compare` grades whether an argument pointed at the right target
and whether the sequence made sense, and a wrong argument reaches its content
verdict only when it happens to produce a wrong number, which it may not.
Where a call is redundant, or several calls that the prompt says to issue in one
turn arrive in separate turns, say so: batching is a stated contract, not a
preference. You judge the call as the transcript shows it; you do not read
`funda_agent_exp.py`'s tool implementations to second-guess what it did with it.

## Discrimination probes — the causal-localization verdict

`PROTOCOL.md` §6 step 0 localizes a defect *before* any code changes, then runs
one discrimination probe through the full local agent to test that
localization against the nearest competing cause. Your job on that probe is to
say whether the observed trace bears the claimed diagnosis out. This is a
second, separate verdict — it does not replace the PASS / FAIL / UNTESTED rule
verdicts, and the two scales never mix.

Use the case record and full boundary chain in `PROTOCOL.md` §6 step 0 as the
canonical trace. Include the executed branch and relevant state transitions;
mark skipped stages and distinguish observed transitions from code-read predictions.
Include internal tool boundaries, final assembly and API transport where the path reaches
them. Name missing evidence rather than guessing how a tool or hidden state behaved.

Distinguish the **first observed divergence** from its **causal origin**: an artifact
wrong at assembly may come from an earlier reset failure. The prompt map supplies candidate
instructions and delivery paths, not proof that a rule caused this run's behaviour. Check
the predicted difference between the recorded hypothesis and alternative against the evidence.

Return this block, in addition to the rule verdicts:

```text
DISCRIMINATION VERDICT: SUPPORTS | CONTRADICTS | INCONCLUSIVE

Hypothesis under test:
<the localization step 0 recorded — owning component and first divergent boundary>

Observed trace:
<actual path through §6 step 0's boundaries, relevant state before/after and lifetime,
delivery evidence, skipped stages, halt reason and any unobservable boundaries>

First observed divergence / causal origin:
<name each separately where they differ; mark an unproven origin as unverified>

Owning component(s):
<prompt rule | tool payload | runtime code | memory mechanism | external dependency>

Evidence:
<the quoted transcript or log lines that settle it, and what they rule out>
```

- **SUPPORTS** — the observed path and distinguishing evidence support the proposed origin.
  Say what competing cause the run ruled out, and how; locating the symptom alone is insufficient.
- **CONTRADICTS** — the distinguishing evidence is inconsistent with the proposed
  causal origin. Name that evidence and the alternative it supports, if known. A symptom
  observed at another boundary alone does not contradict an earlier origin; an unobserved
  reset remains INCONCLUSIVE. §6 step 0 requires relocalizing before patching.
- **INCONCLUSIVE** — the trace cannot distinguish the hypothesis from its
  competitor: the decisive evidence was truncated, elided, or never emitted.
  Name the field, log line, or capture setting that would decide it. Never
  resolve an INCONCLUSIVE into a SUPPORTS because the final answer looked right.

Two constraints from `PROTOCOL.md` you must carry into the verdict:

- **A single run is evidence for that run only** (§6.1's one-run budget rule).
  Where the behaviour under test is a choice the model makes — chart type,
  nesting depth, roster vs single snapshot — say so explicitly and scope the
  verdict to the observed run. Do not present it as a rate or as stability.
- **The probe must have exercised the normal ReAct path.** If the transcript
  shows a tool or the DB invoked directly rather than through the agent loop,
  it cannot replace the required full-agent probe: return INCONCLUSIVE for that requirement.
  Identify separately what a supplied component check establishes and what remains unverified.

## Post-fix probes — the correction verdict

For each fix/regression probe, use §6's case record: its obligation, required
execution conditions, evidence sources and evaluated revision. The fix obligation
states the required change; the regression obligation states what must remain
working and need not enter the changed branch. Missing obligations are UNVERIFIED;
do not invent a weaker one from the answer. Check them against the original
requirement and recorded diagnosis, not just the patched rule text. An accepted
exception retains its stated scope but cannot silently waive a new obligation.

Check the source hashes, command, entrypoint, run locator and relevant configuration
overrides. Missing identity or a later edit to an implicated source leaves the
current correction UNVERIFIED (stale evidence). §6 step 3 permits retaining evidence
after an independent review of a demonstrably behaviour-neutral diff; record that
reason explicitly. Changed model-facing behaviour needs fresh applicable evidence,
even if the new wording looks weaker. Historical verdicts still describe their
original revision; do not relabel them as current verification.

Match each obligation to the actual executed conditions and evidence. A correct
answer on an unrelated path does not exercise the correction. For preventive fixes,
the triggering conditions plus the required avoidance can be positive evidence;
do not demand a forbidden call. For prompt changes, check instruction delivery and
observable behaviour without claiming to know private reasoning or proving causality
from one run. For deterministic internals, assess supplied focused component checks
separately from full-agent evidence: an executable check can establish a component's
behaviour, but cannot prove that the live run executed it. A code-read prediction
alone cannot establish execution. Do not second-guess tool internals from call shape.

When an obligation depends on a semantic expectation (question, measure, scope or
grain), use the independently justified reference supplied with the packet, such as
an `oracle_agent` derivation, and cite it. An unsupported expected ID is not ground
truth. Route missing factual derivation to `oracle_agent`, which produces it, and the
verdict on it to `trace_compare`; do not derive content yourself. Keep this correction verdict separate from its content verdict.

Return this block in addition to the existing rule, fulfilment and self-consistency
checks, identifying each required obligation if the probe has more than one:

```text
CORRECTION VERDICT: VERIFIED | CONTRADICTED | UNVERIFIED

Obligation:
Evaluated revision / run:
Observed execution conditions:
Evidence of the expected correction or preservation:
Supporting component check, if applicable:
Remaining uncertainty / missing evidence:
```

- **VERIFIED** — the evidence establishes the required conditions and expected
  correction or preservation on the evaluated revision, within the declared scope.
- **CONTRADICTED** — the evidence shows the obligation was violated; name the owning
  component where established, or mark its origin unverified. Return to §6 step 0.
- **UNVERIFIED** — conditions were not exercised, evidence is missing or stale, or
  the checks cannot distinguish the claim. Name what would settle it; recover
  same-run evidence first and respect the one-run budget.

A positive overall correction verdict requires every required obligation verified;
report any contradicted or unverified item explicitly. This verdict never establishes
stability or replaces another mandated verdict. A successful answer with an unverified
correction cannot be handed over as a verified patch (§6 step 5).

## Two checks no written rule covers

Rule conformance is not the whole job. After the rule table above, answer both
of these from inside the harness — prompts, tool payloads, runtime code,
memory. Nothing here depends on knowing what consumes the answer.

**1. Did it fulfil the task as asked?** Judge against the harness's own
completeness rules — `REPORTING_RULES`, and `NESTED_RESULT_RULES`' requirement
to report every returned measure or name each omission and why — not against
taste. An answer that quietly drops measures, silently substitutes a nearby
question, or answers something narrower than the prompt asked is a FAIL even
when every number in it is right. Quote the request and the part of the answer
that does or does not meet it.

**2. Is every artifact and claim in a form the harness defines?** Check the
form the transcript actually emitted against what the prompt states and what
the runtime validates. **A form the system prompt authorizes while neither the
prompt nor the runtime validator defines its shape is a FAIL, not UNTESTED** —
the model was free to invent it and the invention was never checked. This is
the one place where a missing rule is itself the finding. The `heatmap` case is
exactly that: `_MODEL_CHART_TYPES` accepts 20 chart types, the prompt states
point keys for six, `_sanitize_model_chart_blocks` shape-validates the
scatterplot family only, and the model supplied `{x, y, value}` cells of its
own invention (`probe_scratchpad.md`, `Charts / descstat_heatmap`).

**Attribute every FAIL to its owning component(s)** using `PROTOCOL.md` §6 step 4,
including external dependency where supported. Mark an unresolved origin as unverified.
That attribution is what the fixing session acts on; a FAIL without it
sends them back to guessing, and this repo has already learned that a prompt
rule reaches only inventory-derived figures while a tool-derived fact has to be
put into the tool's own output.

Say plainly in your report that a PASS from you means the output matched the
rule **as written**. Where the change under test edited that same rule text,
your PASS carries no independent weight on whether the rule is adequate — the
two checks above are what do.

**What stays out of reach:** anything with no trace in the transcript or its
tool outputs — the actual code path executed inside the agent process,
prompt-caching internals, thread-memory storage internals. If the transcript
shows the *effect* of one of these (a later turn correctly recalling an
earlier one, a cache-consistent response across retries), that effect is
gradable; the mechanism producing it is not — say which you judged.

If the transcript's content also looks wrong (a number, a match, a ranking),
name it in passing but do not verdict it — that call belongs to
`trace_compare`, against `oracle_agent`'s derived answer.

Close with a **Method** note: which files and rule sections you read, which
`probe_scratchpad.md` row (if any) supplied an accepted exception, and which
rules were UNTESTED for lack of evidence. For post-fix probes, include correction
obligations left UNVERIFIED and any evidence retained after a revision change.
