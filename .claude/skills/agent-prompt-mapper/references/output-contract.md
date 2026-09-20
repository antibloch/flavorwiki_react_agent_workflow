# Output contract

Use this as the default contract when the repository has no established prompt-map format. Simplify or extend it only when the discovered architecture requires it.

## Authoritative source

Production implementation and static prompt files remain authoritative. Prompt-map artifacts are derived documentation whether produced by a repository generator or directly by this skill. Direct skill-guided updates are allowed; ordinary unsourced hand edits are not.

Build Markdown and JSON from the same logical records so their provenance and relationships cannot drift independently. In direct update mode, preserve unchanged records and patch both artifacts when a changed fact appears in both.

## Machine-checkable schema

[agent-prompt-map.schema.json](agent-prompt-map.schema.json) defines the portable structural contract for instruction records, semantic facets, delivery paths, source references, and kind-specific requirements. Validate changed JSON against it when a compatible JSON Schema 2020-12 validator is available.

JSON Schema does not prove source-to-model reachability, source-hash freshness, canonical array ordering, or cross-record reference resolution. Check those relationships once from the affected records; do not compensate with compile-all or broad application tests.

## Compact graph-traversable JSON

The default JSON needs two top-level collections and may include compact retrieval catalogues and one coverage record:

- instructions: model-facing static instructions/templates and composed instructions;
- delivery_paths: ordered sink recipes with reverse links to instructions and dynamic builders.
- concepts: optional canonical user-language aliases mapped to facets and workflows;
- workflows: optional descriptions of model-facing behaviors referenced by instruction workflow IDs;
- coverage: optional scope, sharding, and partial-completion metadata for large repositories.

Use `coverage` only when scope boundaries or incomplete work would otherwise be ambiguous:

~~~json
{
  "coverage": {
    "status": "partial",
    "scope": "packages/survey-agent",
    "strategy": "shard",
    "included_roots": ["packages/survey-agent"],
    "excluded_roots": ["packages/support-bot"],
    "unresolved_scopes": [
      {"scope": "packages/support-bot", "reason": "Not inspected in this pass"}
    ]
  }
}
~~~

For `complete`, `unresolved_scopes` must be empty. Coverage reports inspection scope, not permission to omit a known model-facing path silently.

### Concept and workflow catalogues

Add these optional catalogues when concept queries are expected and repository terminology alone may not match user wording:

~~~json
{
  "concepts": [
    {
      "id": "concept:benchmarking",
      "summary": "Comparison against an approved benchmark or baseline.",
      "aliases": ["baseline comparison", "benchmark", "benchmarks"],
      "facet_labels": ["benchmarking", "cross-survey-analysis"],
      "workflow_ids": ["workflow:survey-benchmarking"]
    }
  ],
  "workflows": [
    {
      "id": "workflow:survey-benchmarking",
      "summary": "Resolves benchmark scope, retrieves comparable survey data, and handles fallbacks.",
      "entry_facets": ["benchmarking"],
      "related_facets": ["cross-survey-analysis", "scope-enforcement", "survey-analysis-packet"]
    }
  ]
}
~~~

Concept IDs use `concept:` plus lowercase kebab-case. Keep summaries near 160 characters, aliases lowercase and normally no more than eight, and facet/workflow references sorted and unique. Derive aliases from repository terminology and common unambiguous user wording; do not use aliases to invent a concept unsupported by complete static content.

Workflow IDs use `workflow:` plus lowercase kebab-case. `entry_facets` identify direct routes into the workflow and `related_facets` describe supporting concerns. Keep membership only in each instruction's `semantic.workflow_ids`; do not duplicate instruction member lists in the workflow record.

When prompts have distinct responsibilities inside a workflow, add `semantic.workflow_roles`. Store one role per workflow membership on the instruction, never an all-pairs prompt matrix. `strength` describes how central the prompt is to that workflow; `confidence` describes how directly the evidence supports the assigned role.

Adjacency fields make this a traversable graph without a separate graph representation:

instruction -> used_by -> delivered_via -> delivery path -> sink

composed instruction -> composed_from -> component instructions

delivery path -> instruction_ids -> contributing instructions

Concepts and workflows add retrieval entrypoints:

concept -> facet labels or workflow IDs -> instructions

workflow -> instruction semantic.workflow_ids -> delivery path -> sink

Use these relationship meanings when explaining traversal: `matches` for concept-to-facet routing, `member-of` for instruction-to-workflow membership, `composed-from` for composition, `used-by` for direct consumers, `delivered-via` for message/schema/result paths, and `reaches` for the final sink. Follow only relevant one- or two-hop graph context by default; broader impact analysis must be explicitly requested.

For concept retrieval, resolve exact IDs, labels, and aliases first; collect direct facets; expand matching workflows; then add only relevant graph context. Deduplicate by stable instruction ID and rank explicit, inferred, workflow-supporting, then graph-context records without omitting lower-ranked matches. Preserve the relationship path used to justify every result.

### Instruction record

Use fields only when they answer content, location, delivery, composition, or condition questions:

~~~json
{
  "id": "instruction:agent_instructions.AGENT_GOAL",
  "kind": "system_fragment",
  "source": {
    "path": "agent_instructions.py",
    "symbol": "AGENT_GOAL",
    "start_line": 27,
    "end_line": 29
  },
  "excerpt": "Answer what the user asks about...",
  "definition_sha256": "sha256-of-defining-source-slice",
  "used_by": ["agent_instructions.build_system_prompt"],
  "delivered_via": ["system_message"],
  "condition": "present in both full and lean system variants"
}
~~~

Optional fields:

- composed_from: ordered instruction IDs;
- dynamic_content_from: runtime builder symbol;
- schema_constraints: model-visible field defaults/constraints;
- agent_scope: only when multiple real agents share an index and the association cannot be understood from delivery paths;
- uncertainty: only when a relationship must remain inferred or runtime-only.
- semantic: optional bounded metadata for concept-level discovery, following the contract below.

Do not store:

- full static instruction content;
- fully composed prompt content;
- captured runtime values;
- arbitrary file/function/model nodes;
- duplicated forward and reverse edge objects when adjacency fields suffice;
- timestamps, absolute local paths, dependency versions, embeddings, hidden vectors, or unbounded semantic prose.

### Semantic metadata

Use semantic metadata when users need to find prompt families by meaning and excerpts may omit decisive text. Keep it small enough to remain an index rather than a second copy of the prompt.

~~~json
{
  "semantic": {
    "summary": "Provides survey inventory together with resolved benchmark context.",
    "facets": [
      {"label": "benchmarking", "confidence": "explicit", "basis": ["source_text"]},
      {"label": "survey-analysis-packet", "confidence": "inferred", "basis": ["used_by"], "refs": ["runtime.get_survey_analysis_packet"]}
    ],
    "workflow_ids": ["workflow:survey-benchmarking"],
    "workflow_roles": [
      {
        "workflow_id": "workflow:survey-benchmarking",
        "role": "benchmark-context-provider",
        "strength": "core",
        "confidence": "explicit",
        "basis": ["source_text"]
      }
    ]
  }
}
~~~

The optional `semantic` object contains:

- `summary`: a source-grounded purpose statement capped near 160 characters. It must not quote or compress the whole prompt, include runtime values, or repeat provenance already represented elsewhere.
- `facets`: normally no more than eight controlled repository-specific labels. Each facet contains `label`, `confidence`, `basis`, and optional `refs`.
- `workflow_ids`: stable repository-specific group labels shared by instructions participating in one model-facing behavior.
- `workflow_roles`: optional role records corresponding exactly to `workflow_ids`. Each contains `workflow_id`, a lowercase kebab-case `role`, `strength` (`core`, `supporting`, or `adjacent`), `confidence` (`explicit` or `inferred`), `basis`, and optional `refs`.

Use `core` when the workflow depends on the prompt's instruction, input, guardrail, or composition; `supporting` for validation, reporting, fallback, or recovery that assists the workflow; and `adjacent` for useful context that is not required for the workflow's primary behavior. Use `explicit` only when complete static content supports the role and include `source_text` in `basis`. For `inferred`, cite proven composition, consumer, or delivery references in `refs`.

Keep `workflow_roles` sorted by `workflow_id`, emit exactly one record for every `workflow_id` when the field is present, and keep role labels repository-specific but stable. Retrieval groups prompts by workflow and compares these roles on demand. Do not store pairwise similarities or numeric probabilities: similarity is not a functional relationship, and strength is not evidence confidence.

Facet labels use lowercase kebab-case and represent durable concepts or behaviors, not an arbitrary keyword dump. `confidence` is `explicit` when complete static content directly supports the label and `inferred` when a proven structural relationship supplies it. `basis` uses a small vocabulary such as `source_text`, `symbol`, `composed_from`, `used_by`, or `delivery_path`.

For inferred facets, `refs` is required when the supporting relationship is not already obvious from the record. References must name existing stable instruction IDs, workflow IDs, delivery-path IDs, or qualified consumer symbols. Workflow IDs support retrieving multiple prompts without pretending every member contains the queried words.

Derive semantic metadata from complete static content plus established graph relationships. In direct skill-guided mode, the LLM may author the bounded metadata from inspected evidence. If a runtime generator is used, it must remain deterministic and must not call a model. Never emit embeddings, store hidden vectors, or infer a domain concept solely from a vague term.

Sort facets by label and workflow roles by `workflow_id`; sort each `basis`, `refs`, and `workflow_ids` list. Validate labels, confidence values, references, limits, duplicate facets, and exact agreement between workflow-role IDs and `workflow_ids`. Preserve the distinction between exact text evidence and inferred workflow membership in every consumer-facing answer.

Use a short deterministic excerpt, commonly capped near 180 characters. Hash the defining source slice, not a runtime-rendered prompt. Hashes exist for freshness, not semantic impact analysis.

### Composed instruction

~~~json
{
  "id": "instruction:agent_instructions.SYSTEM_PROMPT_TEXT",
  "kind": "composed_system",
  "source": {
    "path": "agent_instructions.py",
    "symbol": "build_system_prompt",
    "start_line": 1092,
    "end_line": 1125
  },
  "excerpt": "Full system instruction assembled from ordered components.",
  "definition_sha256": "sha256-of-builder-source",
  "used_by": ["runtime.SYSTEM_PROMPT"],
  "delivered_via": ["system_message"],
  "condition": "selected while further tool/SQL work may be required",
  "composed_from": [
    "instruction:agent_instructions.AGENT_ROLE",
    "instruction:agent_instructions.AGENT_GOAL",
    "instruction:agent_instructions.SCHEMA_OVERVIEW"
  ]
}
~~~

The example component list is illustrative. Extract the repository's actual order.

### Delivery path

~~~json
{
  "id": "system_message",
  "condition": "every model call; runtime state selects a composed variant",
  "branch_expressions": [
    "messages = [LEAN if done else FULL] + history"
  ],
  "sink": "runtime.call_model -> active_model.invoke",
  "steps": [
    {
      "source": "agent_instructions.build_system_prompt",
      "action": "composes ordered static fragments"
    },
    {
      "source": "runtime.call_model",
      "action": "prepends one SystemMessage variant"
    },
    {
      "source": "runtime.call_model",
      "action": "invokes the selected model"
    }
  ],
  "instruction_ids": [
    "instruction:agent_instructions.SYSTEM_PROMPT_TEXT"
  ],
  "dynamic_builders": []
}
~~~

Keep delivery-path count small. Common paths are system/developer message, user message, tool schema, and tool result, but use the repository's actual roles and framework. Put first-turn/follow-up/persona/final-step variants in condition or branch_expressions rather than creating path variants.

Dynamic builder entries contain:

- builder qualified symbol;
- source reference when useful;
- delivered_via path;
- static attachment condition;
- plain description of the runtime content category;
- no value captured from a real run.

### Stable identities and ordering

- Base IDs on module/file plus qualified symbol.
- Do not include line numbers or hashes in IDs.
- Treat a rename as removal plus addition; do not infer renames in V1.
- Sort instruction records by stable ID and delivery paths by a fixed repository-specific order.
- Sort concepts and workflows by stable ID; sort aliases, facet references, and workflow references inside them.
- Sort semantic facets, evidence lists, and workflow IDs canonically.
- Sort workflow roles by `workflow_id` and never duplicate a workflow membership as a prompt-pair edge.
- Use canonical JSON formatting, UTF-8, and newline at EOF.
- Validate every composed_from, delivered_via, and instruction_ids reference.

## Human- and LLM-friendly Markdown

The Markdown is the primary discovery interface and the progressive-retrieval entrypoint. A useful order is:

1. generated/no-edit warning and source-of-truth statement;
2. one-screen message/sink diagram;
3. compact instruction-phase index;
4. delivery-path summary;
5. detailed delivery recipes with source-derived branch expressions;
6. at least three representative real flows;
7. composed prompt layouts and ordered component links;
8. static instruction details grouped by delivery phase;
9. dynamic builder contracts;
10. source-symbol index;
11. deliberate exclusions, limitations, and regeneration commands.

### Static instruction detail

Every static fragment or statically recoverable parameterized template gets one collapsible section:

~~~html
<details id="stable-anchor">
<summary><code>QUALIFIED_SYMBOL</code> — system_fragment</summary>

- Stable ID: <code>instruction:module.SYMBOL</code>
- Source: linked/file.py:123 (qualified symbol)
- Delivered via: <code>system_message</code>
- Condition: ...
- Direct consumers: ...

Full static content:

<pre><code>HTML-ESCAPED COMPLETE STATIC CONTENT</code></pre>

</details>
~~~

HTML-escape the complete content so embedded Markdown, angle brackets, and code fences cannot break the document. Preserve newlines. For parameterized templates, show placeholders for runtime expressions and label them as substitutions.

### Composed prompt detail

Do not place an expanded large system/developer prompt in Markdown. Show:

- builder source;
- inclusion condition;
- static assembly layout with placeholders;
- ordered composed_from links;
- a note that full component contents are available in their individual details.

This prevents duplication while still making the final instruction explainable.

### Dynamic payloads

Use a table of builder, delivery path, condition, and runtime content category. Never include example live payloads unless they are synthetic fixtures explicitly needed to explain a stable contract; do not mistake fixtures for production prompt content.

### Source links

Use relative links from generated documentation to repository files with line anchors when supported. Always include qualified symbols because line numbers move.

## Freshness and testing

For direct skill-guided updates, parse the JSON and validate compactness, canonical ordering, stable IDs, semantic limits/evidence, adjacency references, and changed Markdown/JSON consistency. Compare recorded definition hashes only for affected or suspected-stale records. Do not run compile-all or broad application tests for artifact-only changes.

When a repository generator is in scope, provide a normal generation command and a check mode:

~~~text
generator
generator --check
tests
~~~

Check mode renders in memory and byte-compares committed artifacts. It does not require Git or generate semantic diffs.

Tests should verify meaningful invariants:

- known model sink and tool/message wiring;
- exact ordered prompt composition;
- static literal/template extraction;
- model-visible tool descriptions and field guidance;
- complete Markdown content for every static record;
- absence of expanded composed prompts;
- absence of full content and runtime values from JSON;
- bounded deterministic semantic summaries/facets derived from complete static content;
- controlled-vocabulary and semantic-reference validation;
- explicit-versus-inferred semantic evidence and workflow expansion;
- resolvable adjacency IDs;
- source-derived delivery conditions;
- deterministic repeated generation;
- stale/missing output detection;
- focused failure on unsupported sink/source shapes;
- deliberate exclusion of prompt-looking but non-reaching text.

Run only checks that directly validate the changed prompt-map paths. Run generator checks when the generator is part of the requested workflow.
