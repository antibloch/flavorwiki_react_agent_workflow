# Repository workflow

Use this workflow when building or substantially updating a prompt map. Adapt paths and names to the repository; the sequence is sink-to-source, not filename-to-guess.

## 1. Establish scope and repository rules

1. Read applicable repository instruction files and existing architecture/design documentation.
2. Inventory languages, dependency manifests, entrypoints, agent frameworks, model providers, and test/CI conventions.
3. Check for an existing prompt map, generator, schema, or freshness command. Update the established system when sound instead of creating a competing one.
4. Preserve unrelated worktree changes. Mapping does not authorize prompt refactors or agent-runtime changes.

The target is model-facing instruction provenance, not a complete software architecture graph.

### Large repositories and monorepos

Start with a cheap sink/entrypoint inventory, not a full-file read. Group findings by independently runnable agent or package, then process one group at a time.

- Use one map when packages share prompt composition or model-call paths.
- Use one map per package/agent when each has independent sinks, prompts, and runtime ownership. Add a small root Markdown index linking the shard artifacts.
- Do not shard by arbitrary file count. A shard boundary must match a real execution/ownership boundary.
- Keep each shard self-contained; avoid unresolved cross-shard instruction IDs.
- For unfinished work, set JSON `coverage.status` to `partial`, list included/excluded roots and unresolved scopes, and mirror that status prominently in Markdown. Never label a partial shard or index complete.

Resume from the coverage record and existing source references instead of rescanning completed shards.

## 2. Find actual model-facing sinks

Search broadly, then prove execution paths:

- provider calls such as invoke/generate/chat/completions/responses;
- framework model wrappers and bound/unbound model variants;
- message constructors and role conversion;
- tool/function/schema registration;
- graph nodes, routers, planners, sub-agent/delegation calls;
- API/CLI/background entrypoints that initiate runs;
- prompt/template/config/file loaders used along those paths.

For each sink, record:

- entrypoint and calling symbol;
- provider/framework boundary;
- messages/instructions/tool schema arguments;
- model selection and tool availability conditions;
- whether a value is static, statically templated, inferred, or runtime-only.

Do not stop at an obvious prompt file. Trace imports, aliases, wrappers, builders, callbacks, decorators, configuration lookups, and message history until the definition or runtime boundary is known.

## 3. Trace sink to source

For every sink, walk backward and classify only facts that help answer the core questions.

### Static instructions

Include:

- literal constants and adjacent/plus-concatenated strings;
- statically recoverable templates and f-string layouts;
- prompt fragments selected from constant dictionaries/configuration;
- file-loaded static prompt content;
- model-visible tool descriptions;
- model-visible argument/field descriptions, relevant defaults, and constraints;
- static tool-result repair or behavioral guidance;
- composed prompt layouts and ordered components.

Expose parameterized templates with placeholders for runtime substitutions. Resolve only substitutions proven static.

### Dynamic content

Record the builder and attachment condition, not its current value:

- user/request input and scope identifiers;
- database inventories and query results;
- API responses and exceptions;
- model-generated tool arguments;
- conversation/checkpoint history;
- environment-dependent values;
- dynamically generated summaries or analysis payloads.

### Exclusions

Exclude unless proven to reach a model:

- unused prompt-looking constants;
- comments and ordinary docstrings;
- HTTP/OpenAPI/Pydantic request descriptions;
- CLI help and logs;
- SQL/query text;
- benchmarks, evaluation prompts, fixtures, and expected answers;
- README prose;
- model names, temperatures, and operational configuration.

Document important misleading exclusions, such as a configured task description that is never consumed.

## 4. Build the repository-specific internal model

For a first implementation, read [worked-example.md](worked-example.md) only if the relationship between exact Markdown content, compact JSON, composed prompts, and inferred facets is not already clear. Do not load the example for ordinary lookup or incremental updates.

Use the smallest useful concepts found in the repository. Usually these are:

- instruction: a static fragment or parameterized template;
- composed instruction: an ordered assembly without duplicated expansion;
- delivery path: system/developer/user message, tool schema, or tool result to a model sink;
- dynamic builder reference: runtime content producer plus condition;
- source reference: file, qualified symbol, and line range.
- concept catalogue: bounded user-language aliases routed to canonical facets and workflows;
- workflow catalogue: short descriptions and facet entrypoints for shared model-facing behaviors.

Relationships normally needed:

- composed_from: ordered component instruction IDs;
- used_by: direct builder/tool consumer symbols;
- delivered_via: delivery-path IDs;
- delivery path instruction_ids: reverse lookup from sink to instructions.

When concept-level discovery matters, also add compact semantic metadata to instruction records:

- summary: one short source-grounded statement of purpose, not an extractive replacement for the prompt;
- facets: controlled concept labels with explicit or inferred confidence and auditable evidence;
- workflow_ids: stable repository-specific group labels shared by instructions participating in one behavior.
- workflow_roles: one bounded role, strength, confidence, and evidence record for each workflow membership when role-aware retrieval is enabled.

Create concept and workflow catalogues only when they improve concept-level retrieval. They remain routing metadata: concepts do not duplicate prompt summaries, and workflow records do not duplicate instruction member lists.

These adjacency fields are a graph traversal interface. Do not add a graph database or a broad node/edge ontology by default.

Keep branch variants as conditions on the same instruction or delivery path. Create separate records only when the static content or provenance is genuinely distinct.

Distinguish:

- proven statically;
- inferred from a constrained repository-specific rule;
- runtime-only and unresolved.

If uncertainty matters, say so. Never turn an inference into a source fact.

### Build a deterministic semantic index

Classify semantics only after complete static content and graph relationships are available in memory. Emitted JSON remains compact and never includes that complete content.

Use five bounded passes:

1. Assign explicit facets when complete static content directly names or unambiguously describes the concept. Evidence basis is `source_text`.
2. Assign inferred facets or workflow membership only from proven structural relationships such as `composed_from`, `used_by`, shared tool ownership, or a delivery path. Record the relationship and keep confidence `inferred`.
3. Create canonical concepts for durable query families. Add no more than eight lowercase aliases drawn from repository terminology and common unambiguous user wording; map each concept only to existing facets and workflows.
4. Create one workflow record for each emitted workflow ID, with a bounded summary, direct entry facets, and supporting related facets. Keep membership in instruction `semantic.workflow_ids` only.
5. When workflow members have distinct responsibilities, assign each membership one lowercase role plus `core`, `supporting`, or `adjacent` strength. Keep evidence confidence separate as `explicit` or `inferred`; do not create numeric probabilities or all-pairs prompt relationships.

Use a small repository-specific controlled vocabulary. Prefer durable domain concepts and behaviors such as `benchmarking`, `cross-survey-analysis`, `retrieve-analysis-packet`, or `scope-enforcement`; do not emit every noun as a tag. A generic word such as `comparison` is not enough by itself to imply benchmarking.

The generator must not call an LLM. An LLM may help propose the vocabulary during implementation, but emitted summaries, facets, workflow IDs, and classification rules must be encoded in deterministic repository-local data or source-derived rules. Sort all labels and evidence deterministically. Make unknown or unsupported classifications fail or remain omitted rather than inventing plausible semantics.

For concept queries over an existing map:

1. resolve exact concept IDs, aliases, facet labels, and workflow IDs;
2. collect all explicit and inferred facet matches, preserving confidence and evidence;
3. expand every matching workflow through instruction `semantic.workflow_ids`, using `workflow_roles` to explain each member and rank core, supporting, then adjacent participation;
4. follow only relevant one- or two-hop `composed_from`, `used_by`, and delivery-path relationships;
5. deduplicate by stable instruction ID and rank explicit, inferred, workflow-supporting, then graph-context records without dropping matches;
6. report the relationship path that produced each result and use `kind` and `delivered_via` to distinguish guidance, parameters, results, and guardrails;
7. state total counts, partial coverage, and when compact metadata cannot prove exhaustiveness.

Retrieve progressively: use JSON for discovery and graph evidence, load only matching Markdown details for complete wording, and inspect source only for freshness, missing facts, or implementation detail.

## 5. Choose the update mechanism

Use the least expensive mechanism that preserves source evidence. For an existing map, prefer a direct skill-guided incremental update: load its compact JSON and Markdown, follow recorded source references only for affected records, then patch the artifacts canonically. Do not reread the entire codebase when source hashes, symbols, delivery paths, and existing full-content details are sufficient.

Use a deterministic repository-local generator when the user requests automation, the repository relies on it for CI freshness, or repeated unattended regeneration is part of scope. A generator that originally created an artifact is not automatically part of every later update.

### Static mechanism

Use the simplest native parser that preserves required facts:

- Python: standard-library ast is usually enough for literals, templates, calls, imports, decorators, assignments, and source ranges. Use LibCST only when concrete syntax or safe rewriting is genuinely required.
- TypeScript/JavaScript: TypeScript compiler API, Babel parser, or the repository's existing AST tooling.
- Other languages: use the repository's parser/compiler APIs or a focused structured parser before relying on regex.

Text search is useful for discovery and validation, not as the sole provenance engine when structured parsing is available.

Support only constructs used on proven sink-to-source paths. Examples:

- literal/container evaluation;
- adjacent or plus string concatenation;
- simple constant dictionary lookups;
- statically recoverable f-string/template returns;
- known format/substitution calls;
- direct imports/references;
- tool description assignment and wrapping;
- schema-field descriptions/defaults/constraints;
- message construction and model invocation branches.

Do not build a reusable general-purpose static-analysis framework inside the target repository.

### Expected-shape validation

Encode classification and expected source shapes, never copied prompt bodies. Fail generation with a focused error when:

- a known model sink disappears or multiplies;
- prompt composition becomes unsupported;
- a registered tool loses or changes description wiring;
- a tool list/binding route changes;
- message construction no longer matches the mapped path;
- a referenced instruction/path ID dangles;
- an unhandled loader/dynamic form enters a known sink.

### Runtime verification

Use only if the framework materially transforms prompts/tool schemas in a way static source cannot establish. If used:

- isolate it from credentials, networks, production data, and side effects;
- compare effective structure/schema, not request payload values;
- keep static source locations as provenance;
- make runtime checking optional or supplementary unless determinism requires it.

## 6. Implement in the target repository

### Direct skill-guided update

For an existing map:

1. parse the current JSON and use stable IDs to select affected records;
2. read full content from the paired Markdown detail when it is current, otherwise inspect only the recorded source symbol;
3. derive semantic facets from complete content, then use composition, consumers, and delivery paths for clearly marked inferred relationships;
4. update affected concept aliases and workflow descriptions without duplicating instruction membership;
5. update workflow roles for affected memberships without creating pairwise prompt edges;
6. patch JSON and Markdown directly while preserving canonical ordering and untouched records;
7. validate JSON against [agent-prompt-map.schema.json](agent-prompt-map.schema.json) when a compatible validator is available;
8. perform one focused cross-record check for ID uniqueness, catalogue references, adjacency references, workflow-role membership agreement, and changed Markdown/JSON consistency, which JSON Schema cannot fully express.

Do not add a repository validator script by default. Use an available JSON Schema validator or JSON parser, then perform only the focused relationship checks proportional to the records changed. Do not run compile-all, import production modules, or execute broad application tests for an artifact-only update.

### Generator-backed update

Follow repository conventions. When no convention exists, sensible defaults are:

- generator: scripts/generate_agent_prompt_map.py or the language-equivalent scripts location;
- human artifact: docs/generated/agent-prompt-map.md;
- machine artifact: docs/generated/agent-prompt-map.json;
- focused tests beside the repository's tests;
- a README link;
- a CI command that runs tests and generator --check.

The generator should:

1. parse only the proven production files;
2. build one in-memory index;
3. render Markdown and JSON from the same index;
4. sort deterministically;
5. use UTF-8 and stable newline behavior;
6. omit timestamps and absolute machine paths;
7. generate normally or byte-compare with committed outputs in --check mode;
8. avoid importing production modules with side effects.
9. derive bounded semantic metadata from complete static content and proven graph relationships before rendering truncated JSON excerpts.
10. derive bounded concepts and workflow descriptions from emitted facets and workflow memberships without model calls at generation time.

Implementation code remains the authority for prompt content. In direct mode, the LLM may author bounded semantic summaries and facets from inspected evidence, but must not paraphrase full content in place of preserving the exact Markdown detail.

If the user requested a plan before implementation, create the plan in the repository's design/docs location and stop at the requested boundary.

## 7. Validate representative flows

Trace at least three real flows that exercise different delivery mechanisms. Prefer examples such as:

1. initial request: static system composition plus scoped user context to the first model call;
2. tool loop: model-visible tool description to tool call, result guidance to ToolMessage, then subsequent model call;
3. conditional/final/sub-agent route: optional injected instruction, alternate composed prompt/model, tool removal, router, or delegated model sink.

For each, verify:

definition -> composition/import -> consuming builder/tool -> message/schema -> model sink

Then verify:

- every static record's complete content appears in exactly one intended Markdown detail;
- composed large prompts are not expanded again;
- JSON contains excerpts, not full content;
- semantic summaries and facets are bounded, controlled, evidence-backed, and deterministic;
- explicit semantic evidence is derived from complete content rather than the truncated excerpt;
- inferred workflow facets are never reported as exact text matches;
- concept aliases resolve only to emitted facet/workflow IDs and every emitted workflow has one catalogue record;
- workflow roles cover exactly the recorded workflow memberships and keep strength separate from evidence confidence;
- all adjacency IDs resolve;
- branch expressions/conditions match source;
- runtime data is represented only by builder contracts;
- prompt text, composition, and condition changes make freshness checking fail;
- unsupported sink/source shapes fail loudly;
- direct edits preserve canonical ordering and pass focused artifact/schema/reference checks;
- when a generator is in scope, repeated generation is byte-identical and its focused check passes.

## 8. Report

Lead with the result. Include:

- generated artifact and generator paths;
- number of instructions and delivery paths;
- major architecture findings;
- tests and check commands run;
- deliberate exclusions;
- unresolved dynamic/static patterns;
- deviations from the agreed contract;
- environmental limitations such as missing Git metadata.
