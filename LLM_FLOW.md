# LLM-flow design for reliable raw output

This document covers cases where the LLM should directly produce the desired answer or
raw structured output. It complements `CONTROL_FLOW.md`: deterministic runtime control is
not always the right owner, especially for explanation, synthesis, and flexible language.

## Choose a recipe from the diagnosed mechanism

Use the path, state and prompt IDs in `PROTOCOL.md` §6's case record. Evidence shown to be
missing from model context calls for grounding or delivery repair; missing log evidence calls
for a better capture, not a prompt fix. A conflicting rule or worked example calls for resolving
that conflict; correct evidence rendered incorrectly calls for a constrained format,
verification or runtime ownership. Rule presence alone does not distinguish these causes.
Choose separately the output producer, factual authority and enforcement mechanism. LLM
output can require harness changes; this document is not limited to instruction edits.
Record the predicted effect and a check that could disprove it in the same case record.

## Define the raw-output contract

Before asking the model to produce output, define the smallest useful contract:

- required fields and types;
- allowed values and units;
- source evidence required for each factual claim;
- omission, uncertainty, and unavailable-data behavior;
- whether ordering, rounding, totals, or significance labels are meaningful;
- the exact rendering format expected by the consumer.

Separate factual fields from narrative fields where possible. The model can write the
narrative, but each factual field should have an identifiable evidence span or tool
reference.

## Ground the model before generation

Provide the model with the relevant tool output, not a vague instruction to be accurate.
Keep question identity, scope, units, sample size, filters, and limitations adjacent to
the values they constrain. Use stable markers or schemas so the model can distinguish
evidence from instructions and from prior conversation.

Require the model to say when evidence is absent, conflicting, underpowered, or outside
scope. Do not encourage completion of missing fields by inference unless that inference
is explicitly part of the task.

## Constrain generation appropriately

Use the least restrictive mechanism that satisfies the contract:

- structured output or JSON schema for machine-consumed fields;
- enums and numeric bounds for controlled values;
- tool calls for retrieval or computation rather than recalled facts;
- explicit units and rounding rules;
- a compact answer template for repeated reporting tasks.

Constraints reduce format and omission errors, but a syntactically valid response can
still contain false values. Schema validation is necessary for structure, not sufficient
for truth.

## Verify the generated answer

After generation, validate independently where feasible:

- parse the structure and check types, ranges, required fields, and cross-field totals;
- compare cited numbers against the tool result or evidence record;
- check that the selected question/product/population matches the request;
- detect unsupported causal, significance, or certainty language;
- reject or repair only from authoritative evidence, never by asking the model to guess
  again without new evidence.

A second model can help classify or critique language, but it is not an authority source
for database facts. Deterministic checks should own checks that can be expressed exactly.
For an instruction correction, verify delivery on the affected path and check the interacting
rules/examples identified by the prompt map. Compare the predicted effect with the probe's
observed result; preserve the original task criterion when changing the rule being graded.

## Handle uncertainty and failure explicitly

The raw-output contract should define safe outcomes for missing, malformed, contradictory,
or unauthorized evidence. Prefer `unavailable`, `inconclusive`, or a targeted clarification
over a plausible-looking completion. Preserve the evidence and failure reason for the
next step without turning an error message into a fact.

## When to switch to control flow

Move a field or block to the control-flow pattern when repeated evaluation shows that the
LLM is still changing exact values, producing invalid renderable payloads, or making a
decision that the application can determine from authoritative data. A hybrid boundary
is valid: the LLM returns prose and references, while runtime code supplies exact values,
charts, links, or status blocks.

## Evaluation

Evaluate raw LLM output on separate dimensions: factual agreement with the evidence,
question/scope match, completeness, uncertainty honesty, schema validity, and usefulness
of the prose. Include adversarial cases involving missing data, near-matching questions,
rounding, contradictory tool results, and attempts to inject formatting or instructions.
Use the results to decide whether to improve grounding, constrain generation, add a
verifier, or transfer ownership to control flow.
