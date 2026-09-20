# Control-flow design for reliable agent output

This document is a general decision framework for reducing hallucination and unsupported
claims in an agent. It abstracts the concrete patterns documented in
`anti_hallucination.md`; it is not a rule that every problem must be solved with
deterministic code.

Use `PROTOCOL.md` §6's case record to connect these recipes to the diagnosed path and state.
Choose separately who produces the output, which source supplies its factual authority, and
which mechanism enforces the contract. A validator addresses invalid form; faithful assembly
addresses alteration of valid values; neither corrects a wrong question or scope upstream.

## First question: who should own the output?

For each output field or block, identify its authoritative source:

- **Tool/data-owned:** a number, row, label, coordinate, count, ranking, or chart payload
  that can be reproduced from a database, API, or deterministic computation.
- **Policy-owned:** a refusal, warning, scope decision, validation result, or status that
  must follow an application rule.
- **LLM-owned:** interpretation, explanation, wording, prioritization, or synthesis where
  useful language judgment is the actual task.
- **Mixed:** an LLM explanation containing tool-owned facts. Split it into independently
  controlled fields where practical.

The owner should be determined by authority and verifiability, not by convenience. A
prompt is not an authority source for a fact that a tool already knows.
Deterministic output is not automatically correct: establish question/product identity, scope,
units and computation convention before treating source values as trusted. Distinguish source
correctness from faithful copying, and preserve any unresolved source disagreement.

## When control flow is a good solution

Consider a deterministic control-flow solution when one or more of these are true:

- the output has an exact machine-readable contract;
- incorrect values would materially mislead the user;
- the value can be computed or copied faithfully from a trusted tool result;
- the model is likely to round, omit, reorder, reinterpret, or fabricate the value;
- the frontend treats the output as executable, renderable, or structured data;
- the application must fail closed when data is missing, unauthorized, malformed, or stale.

Typical design: validate the tool result, store trusted fields outside model-authored
messages, and assemble the final response at the runtime boundary. The LLM may still
provide prose around the result when that is useful.

## When another solution may be better

Do not add a deterministic assembler merely because it is possible. Prefer an LLM-flow
solution when the output is primarily explanatory, open-ended, or requires contextual
language judgment, and exact reproduction is not the core contract. A hybrid solution is
often best: code owns facts and structure while the model owns explanation.

Also consider a schema/constrained-generation, retrieval-grounding, verifier, or
post-generation repair approach when the model must produce the complete raw output and
runtime assembly would damage usability or flexibility. Compare complexity, latency,
maintainability, observability, and failure behavior before choosing.

## Required reasoning for a proposed fix

For a hallucination or overclaiming problem, fill the existing §6 case record with:

1. the first observed divergence and, if different, the evidenced causal origin;
2. the authoritative source and whether it is reproducible;
3. the proposed owner for each risky output field;
4. at least one control-flow option and one LLM-flow option when both are plausible;
5. why the selected option has the better correctness/flexibility tradeoff;
6. what remains LLM-dependent and therefore still needs evaluation.

Do not infer that a bad final answer is necessarily a prompt defect. Trace the path:
source → tool result → validation/normalization → state or model context → final
assembly/rendering. Fix the earliest boundary that introduced the divergence.

## Control-flow patterns

- **Validate:** reject malformed, unsafe, non-finite, unauthorized, or incomplete data.
- **Normalize:** convert equivalent tool shapes into one stable internal contract.
- **Park:** keep high-risk trusted values outside model-authored text.
- **Assemble:** combine trusted blocks with model prose at a final runtime boundary.
- **Short-circuit:** bypass another LLM turn when the desired response is already a
  complete trusted artifact.
- **Fail closed:** emit an explicit unavailable/refusal result instead of guessing.
- **Audit:** retain provenance, validation outcomes, and enough trace data to diagnose
  whether the problem arose in selection, retrieval, transformation, or rendering.

For parked artifacts, identify the owning request/turn and the required reset or expiry.
Check what later assembly, sanitizing, transport or history replay can change. If the defect
depends on a prior turn, verify the implicated state transition as well as the final artifact.

## Verification

Tests and probes should check more than whether a prompt sounds persuasive. Where control
flow is used, verify exact artifact fidelity, preservation of source values, rejection of
model replacements, malformed-input behavior, authorization boundaries, and the remaining
LLM prose separately. The concrete PCA and word-cloud examples are in
`anti_hallucination.md`.
Use the case record's predicted effect to check that the chosen control addresses the diagnosed
mechanism. A correct final block alone does not establish that an earlier state reset worked.
