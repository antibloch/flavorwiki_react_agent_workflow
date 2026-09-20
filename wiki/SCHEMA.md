# Wiki Schema

This file is the configuration for this wiki. It documents the conventions, page types, tag taxonomy, and any workflow customizations. The LLM reads this first when entering the wiki, and its conventions override the defaults documented in the `llm-wiki` skill.

This file is **co-evolved with the user**. When the LLM notices a recurring pattern in your edits or feedback that isn't here, it will propose adding it. When something here stops fitting, prune it.

## Wiki location

- Wiki root: `wiki/`
- Raw sources: `raw/`
- Asset/image storage: `raw/assets/`

## Page types

This wiki uses these page types, each with a dedicated subdirectory:

- `source` (in `wiki/sources/`) — one summary page per ingested source.
- `entity` (in `wiki/entities/`) — pages about specific things: people, papers, products, places, organizations.
- `concept` (in `wiki/concepts/`) — pages about ideas, methods, frameworks, abstractions.
- `synthesis` (in `wiki/synthesis/`) — cross-cutting analyses, comparisons, query answers filed back.

Add additional types here as the wiki evolves.

## Tag taxonomy

Keep this list small and disciplined — a wiki with 200 tags has effectively no tags. Propose an addition only when a real page needs it, and prune tags that stop earning their place.

- `agent-variant` — a specific `run_agent` / `funda_agent` working copy and its behaviour.
- `db-schema` — `gpi_sample_db` structure and column semantics.
- `benchmark` — regression, needle-haystack, and scoring runs.
- `deployment` — server, CI, and live-DB configuration.
- `prompt-design` — `agent_instructions.py` / `tool_prompts.py` technique.
- `open-question` — pages or sections that flag unresolved questions.

## Page sizing

- Soft cap: 400 lines / ~2,000 words. Consider splitting beyond this.
- Hard cap: 800 lines. Must split.

## Frontmatter requirements

Every page must have:
- `type`
- `title`
- `tags`
- `created`
- `updated`

Plus type-specific:
- `source` pages: `authors`, `url` (if applicable), `raw`, `ingested`
- Non-source pages: `sources` listing the source-summary pages drawn from

## Optional graph metadata

Pages may declare typed graph metadata under a top-level `graph:` key. This is the source of truth for the compiled knowledge graph under `wiki/graph/`. Markdown remains canonical; the graph is a regenerable index. Pages without `graph:` still appear as nodes (derived from `type`/`kind`) and still contribute `mentions` edges from body `[[wikilinks]]`.

```yaml
graph:
  node_id: person:praney-behl       # optional; default <node_type>:<slug>
  node_type: person                  # optional; default mapped from type/kind via ontology
  canonical: true                    # mark as canonical when multiple slugs alias the same entity
  aliases: [Praney, praney@example.com]
  relationships:
    - predicate: founded
      object: company:seedblocks
      source: praney-founder-context-dump   # source-page slug
      evidence: "Solo technical founder and sole director..."
      confidence: high               # high | medium | low
      status: current                # current | historical | proposed | disputed | superseded
      # optional:
      # valid_from: 2025-01-15
      # valid_to: 2026-03-01
      # notes: "..."
      # raw_ref: "raw/founder-dump.md#L42"
      # contradicts: edge-id-or-source-slug
      # supersedes: edge-id-or-source-slug
```

Required fields on every relationship: `predicate`, `object`, `source`, `evidence`, `confidence`, `status`. Predicates and the subject/object types they accept are declared in `wiki/graph/ontology.yaml`. Typed semantic edges must be supported by an explicit source — never emit one inferred from training data alone.

## Index structure

(Update this section when sharding.)

Currently flat: a single `wiki/index.md` listing all pages.

When the wiki passes ~150 pages or `index.md` exceeds 300 lines, shard into `wiki/indexes/<type>.md` and update this section.

## Retrieval

- Search is section-level hybrid by default: `uv run --script skills/llm-wiki/scripts/wiki_search.py "query" --json`.
- Semantic backend: local FastEmbed + sqlite-vec (`BAAI/bge-small-en-v1.5`, 384 dimensions). No wiki or query text leaves the machine.
- First semantic use downloads model artifacts to `~/.cache/llm-wiki/fastembed/`; set `FASTEMBED_CACHE_PATH` to override the model cache.
- Semantic setup verified: 2026-08-14
- `wiki/.wiki-cache/` holds regenerable retrieval artifacts: `search-index.json` (parse cache) and `embeddings.sqlite` (section metadata + sqlite-vec vectors). Safe to delete; never edit by hand; gitignored.
- The vector index is content-hashed: only new or changed sections are re-embedded, deleted sections are removed, and model/schema changes rebuild it automatically.
- Dependency-free lexical path: `python skills/llm-wiki/scripts/wiki_search.py "query" --no-embed` (direct Python bypasses PEP 723 dependency resolution). A missing or failed local backend also falls back to lexical search without failing the command.

## Graph layer

The wiki has an optional compiled graph layer under `wiki/graph/`:

- `wiki/graph/ontology.yaml` — declares node types and predicates. **Tracked.** Edit this when you introduce new predicates or domain types.
- `wiki/graph/nodes.jsonl`, `wiki/graph/edges.jsonl` — generated. Track in git only if you want graph diffs in PRs.
- `wiki/graph/graph.sqlite` — generated. Gitignored by default.
- `wiki/graph/graph.graphml` — generated. Track only if you want to diff it.

Generation is reproducible from markdown via `scripts/wiki_graph_extract.py`. The graph can be deleted at any time and rebuilt without losing knowledge — markdown is canonical.

## Workflow customizations

### Ingest scope

This wiki is scoped to the `final_agent_work_v5_base_2_optim3_exp/` working copy only. Do not ingest from the parent `flavorai_v2/` tree or from sibling `final_agent_work_v*` copies; each is a separate experiment.

- **Never ingest `db/`.** `db/updated_dump.sql` is a ~110 MB Postgres dump that `docker-compose.yml` loads into the local `gpi_sample_db` container. It is data, not prose — ingesting it would bloat the parse cache and vector index for no retrieval value. Capture schema knowledge on `db-schema` pages written from `docs/db_schema.md` instead.
- **`deployment/` — ops docs only.** That directory is its own git repo holding the server-deployed build of this code, with the local DB replaced by a live DB URI. Ingest only its unique operational knowledge — `deployment/docs/hetzner-deployment.md`, `.gitlab-ci.yml`, `compose.production.yaml`, `.env.example` — and tag those pages `deployment`. Skip its `.py` files: they are near-duplicate versions of this directory's `agent_instructions.py`, `funda_agent_exp.py`, and `tool_prompts.py`, so ingesting both trees would create duplicate pages that semantic lint flags.
- **Never copy secrets into the wiki.** `deployment/.env.example` is a safe template; real `.env` / `app.env` / `.compose.env` values must never be quoted on a page. `funda_agent_exp.py` carries hardcoded credential fallbacks; describe that as a risk, never reproduce the values.

### Raw sources that already live in the repo

The default workflow copies each source into `raw/`. For documents that are **already tracked, living files in this working copy** (`docs/*.md`, `README.md`, `SESSION_HANDOFF.md`, source code), do not copy them — point `raw:` at the real in-repo path instead, relative to this directory:

```yaml
raw: "docs/agent_exp_doc.md"
```

Copying would duplicate ~470 KB and the copy would silently drift from the maintained original. `raw/` stays reserved for genuinely external sources — papers, articles, transcripts, PDFs — which are immutable once captured and have no in-repo home.

Because in-repo sources are living documents, a source page can go stale without the wiki changing. Record the source's own audit date on the page when it states one, and treat a newer document as evidence about an older one rather than silently overwriting.

## User preferences

(Empty initially. As the user expresses style preferences — "always include a 'Why this matters' section on concept pages", "never use bullet lists in summaries", "prefer comparative tables for synthesis pages" — capture them here so they persist across sessions.)

## Lint cadence

- Structural lint: after every 5 ingests.
- Semantic lint: weekly or after every 20 ingests.
- Gap-finding: monthly.
- Graph lint + extract: after every ingest that adds typed `graph.relationships`.

Adjust based on the wiki's growth rate.
