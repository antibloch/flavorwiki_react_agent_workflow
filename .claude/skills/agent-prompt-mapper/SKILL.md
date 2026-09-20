---
name: agent-prompt-mapper
description: Inspect agent or LLM codebases to locate model-facing prompts, trace them from definitions to model-call sinks, and create or update deterministic human Markdown plus compact semantically searchable, graph-traversable JSON. Use for prompt inventories, provenance, composition, semantic discovery, delivery conditions, impact paths, and freshness automation; do not use merely to write a prompt or debug unrelated runtime behavior.
---

# Agent Prompt Mapper

Make an unfamiliar agent codebase answer four questions reliably:

1. What model-facing instructions exist?
2. What is their complete static content?
3. Where are they defined and how do they reach a model call?
4. Under what static or runtime conditions are they included?

Also make compact maps answer concept-level discovery questions, such as which instructions participate in benchmarking, without storing full prompt bodies in JSON.

Treat implementation code as authoritative. Do not trust proposed filenames, architecture, agent names, framework terminology, or documentation until the repository proves them.

## Host compatibility

The core `SKILL.md` and references follow the Agent Skills directory format and are compatible with Claude Code when this skill folder is installed or symlinked at `~/.claude/skills/agent-prompt-mapper/` for personal use or `.claude/skills/agent-prompt-mapper/` for one project. Claude Code discovers `SKILL.md` directly and invokes it as `/agent-prompt-mapper`; it does not require or consume an `agents/claude-code.yaml` adapter.

Prefer one canonical skill folder with host discovery links over duplicated host-specific copies that can drift. Do not claim support for another host until its documented skill-loading convention is satisfied.

**In this repository** (vendored copy, diverges here from the personal copy at `~/.claude/skills/agent-prompt-mapper/`): this folder is the canonical home, and Codex CLI reaches it through `AGENTS.md` in the repo root, which instructs the agent to read this `SKILL.md` in full before acting. There is no `agents/openai.yaml` adapter and none is needed. See `PROTOCOL.md` §0.

## Modes

### Build or update a map

Read [references/workflow.md](references/workflow.md) and [references/output-contract.md](references/output-contract.md) completely, then follow them. Use [references/agent-prompt-map.schema.json](references/agent-prompt-map.schema.json) when creating or changing JSON. Read [references/worked-example.md](references/worked-example.md) only for a first map or when composition and explicit-versus-inferred semantics remain unclear. Inspect applicable repository instructions before changing files.

Use a hybrid approach:

- Let the LLM discover the repository's real architecture and select the narrow extraction rules.
- For an existing map, begin with its JSON/Markdown records and inspect only source references needed for the requested change, stale hashes, missing content, or unresolved relationships. Do not rescan the whole codebase when the existing provenance remains adequate.
- Direct skill-guided artifact updates are valid: the LLM may author canonical Markdown/JSON from source evidence and update existing records incrementally. A repository-local generator is optional, not required.
- Use or change a repository generator only when the user requests it or automated CI freshness is part of scope. Do not modify an existing generator merely because it originally created the artifacts.
- For large repositories, inventory model-call sinks cheaply, then shard only at independently runnable agent/package boundaries. Record complete versus partial coverage explicitly; never imply an unfinished shard covers the monorepo.
- When concept-level retrieval matters, emit bounded concept and workflow catalogues from complete source-grounded semantics. Map common user wording to canonical facets and workflows without embeddings or duplicated prompt content.
- When prompts play different parts in the same workflow, add one bounded workflow-role record per instruction membership. Record the prompt's role, `core`/`supporting`/`adjacent` strength, `explicit`/`inferred` confidence, and evidence once; derive prompt-to-prompt relationships during retrieval instead of storing pairwise scores.
- Use runtime verification only when static provenance cannot establish an effective framework-generated prompt or tool schema. Runtime evidence supplements source provenance; it never replaces it.

### Explain an existing map

Start with compact JSON. Resolve the user's wording through the optional concept catalogue, then collect direct facet matches, expand matching workflows, and follow only relevant one- or two-hop composition, consumer, and delivery relationships. Deduplicate by stable instruction ID. Rank explicit matches before inferred, workflow-supporting, and graph-adjacent context, but never let ranking suppress a match. Within workflow-expanded results, use recorded roles and rank `core`, then `supporting`, then `adjacent`; explain how the roles cooperate rather than inventing a similarity probability.

For each result, preserve the relationship path that explains the match, such as query alias -> concept -> facet -> instruction or query -> workflow -> instruction -> delivery path -> sink. Distinguish explicit source-text evidence from inferred or workflow relationships. State total counts and any partial coverage that limits exhaustiveness.

Use progressive retrieval: JSON for discovery and provenance, the matching Markdown detail when complete prompt wording is requested, and production source only when the map is stale, incomplete, or implementation detail is required. Do not regenerate or edit files unless the user asked for a change.

When answering from JSON, use a Markdown table unless the user requests another format. Include the prompt ID or symbol, semantic summary, relationship type, evidence or basis, relationship path, exact source path/file name, start line, end line, delivery path, and condition; add other columns relevant to the question. Label a direct matching facet by its recorded `explicit` or `inferred` confidence, records found only through workflow expansion as `workflow-supporting`, and optional adjacent records as `graph-context`. State the total match count and return every match. Never silently truncate results or invent missing fields; split unusually large results into clearly numbered tables or continuations.

### Check freshness

Check recorded source hashes, source references, JSON/Markdown consistency, and relevant delivery relationships. Validate changed JSON against the shipped schema when a compatible validator is available; otherwise parse it and report that schema validation was unavailable. Use a repository freshness command only when it is in scope. Do not run compile-all or broad unrelated test suites for artifact-only updates.

## Non-negotiable boundaries

- Start at actual model-facing sinks and trace backward. Filenames containing prompt, agent, or instruction are candidates, not proof.
- Include system/developer/user instruction builders, tool descriptions and model-visible field guidance, tool-result guidance, composed fragments, and conditional injected messages only when data flow reaches a model.
- Exclude comments, HTTP/OpenAPI descriptions, CLI help, evaluation inputs, SQL text, and unused configuration unless source flow proves they are model-facing.
- Represent request-specific variants as one instruction plus conditions. Do not create a node for every possible message payload or runtime branch.
- Show every statically recoverable instruction/template in full inside the human Markdown.
- Do not duplicate a large fully composed prompt. Show its layout and ordered component links; show each component's full content once.
- Keep JSON compact: excerpts, bounded semantic metadata, and adjacency/provenance only; never full prompt bodies or captured runtime values.
- Keep concept aliases and workflow descriptions bounded and canonical. They route retrieval to existing facets and relationships; they are not a second prompt index or permission to infer unsupported concepts.
- Keep workflow roles bounded and auditable. Every emitted role must correspond to an existing `workflow_id`; strength describes participation importance, while confidence describes evidence quality. Do not store all-pairs prompt correlations, numeric relationship probabilities, or duplicated pair edges.
- Make semantic metadata canonical, source-grounded, and auditable. Use a short summary, controlled facets, workflow IDs, and evidence/confidence as defined in the output contract. In direct skill-guided mode, the LLM may author this metadata from inspected evidence; a runtime generator must not call a model.
- Classify explicit concepts from complete static content before truncating excerpts, then add inferred workflow facets only from proven composition, consumer, or delivery-path relationships. Never present an inferred facet as an exact text match.
- Represent dynamic content by builder symbol, source, and attachment condition. Never snapshot user input, database rows, API responses, tool arguments, exception text, or conversation state.
- Prefer stable IDs based on module/file plus qualified symbol. Never base identity only on line numbers.
- Use one definition digest only when needed for deterministic freshness. Defer semantic diffs, rename inference, dependency hashes, embeddings, graph databases, and generalized call-graph frameworks unless the repository demonstrates a concrete need. Semantic facets are a retrieval index, not a semantic-diff system.
- Make expected source-shape changes fail loudly. A plausible but incomplete generated map is worse than a focused unsupported-pattern error.
- Do not import production modules when imports require credentials, initialize services, mutate state, or contact networks. Parse source instead.
- Generated artifacts are derived and never become authoritative over implementation code. Do not casually hand-edit them, but direct updates performed by this skill are an approved artifact-generation path.

## Completion standard

Before handoff:

- Trace at least three real definition-to-model flows.
- Confirm every mapped static instruction has one full-content Markdown detail.
- Confirm composed prompts are represented without expanded duplication.
- Confirm JSON references resolve and contains no full instruction content.
- Confirm semantic summaries/facets are bounded, deterministic, evidence-backed, and preserve explicit-versus-inferred distinctions.
- Confirm concept aliases resolve only to existing facet/workflow identifiers and workflow catalogue IDs match instruction memberships.
- Confirm workflow-role IDs exactly match their instruction's workflow memberships and that every role has valid strength, confidence, and evidence.
- Confirm dynamic payloads have builders/conditions but no captured values.
- Confirm repeated canonical rendering would not reorder or duplicate records. When a deterministic generator is in scope, regenerate twice.
- Run one focused schema/parse and reference check for changed artifacts. Run repository tests or freshness commands only when they directly validate the changed mapping path.
- Report paths, record/path counts, deliberate exclusions, deviations, and unresolved source patterns.

If the user requested planning only, stop after the plan. If they requested implementation, complete the requested direct artifact update or generator-backed workflow without refactoring agent runtime behavior or expanding validation beyond the agreed scope.
