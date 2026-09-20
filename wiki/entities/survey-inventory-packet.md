---
type: entity
kind: mechanism
title: "Survey inventory packet"
aliases: ["survey_inventory", "_INV_SQL", "analysis packet", "persona inventory"]
tags: [agent-variant, db-schema]
sources: [funda-agent-exp-architecture-doc, final-agent-work-bundle-readme]
created: 2026-08-14
updated: 2026-08-14
---

A precomputed analytical summary of one survey, built by a **single PostgreSQL statement** and attached to the model's first message before any LLM call. It is the mechanism by which [[funda-agent-exp]] pushes computation into the database so the model reads statistics rather than raw rows.

Two variants exist: the general inventory (`survey_inventory`) and a route-specific persona inventory (`survey_persona_inventory`). Both share a completeness rule, a cache design and a TTL policy.

## Packet shape

```json
{
  "products": [],
  "scored_measures_by_product": [],
  "other_answered_measures": []
}
```

`products` carries product names and blinding numbers.

`scored_measures_by_product` carries numeric, product-attributable measures with question ID, full prompt and question type; scale-point count and observed score range; per-product value count, respondent count, mean and sample SD; pooled-attribute detection; per-attribute results for multi-attribute questions; and `order_differs`, listing attributes whose product order departs from the pooled order.

`other_answered_measures` keeps every remaining answered question — open-text, ranking, matrix and non-product-linked measures — reporting `answers` (expanded option/value rows after the left join), `submissions` (distinct `answer.id`), `respondents` (distinct enrollments), product linkage, and a sample of stored option labels.

The separate `submissions` field exists to stop a multi-option question being reported as though every option row were its own submitted answer. This distinction is of a piece with the response-count grain work recorded in [[client-boundary-session-handoff]].

## One statement, one snapshot

`scored AS MATERIALIZED` is shared by the scored summary and by the exclusion that builds the other-measure summary. Products, survey lifecycle metadata and both summaries return from the same statement, giving the packet a single statement-level database snapshot and one driver round trip. The top-level object is then rebuilt in Python after reading PostgreSQL JSONB so that serialized key order stays deterministic and the startup and on-demand packet payloads remain byte-compatible.

## Complete or absent, never truncated

The inventory returns an empty string when the survey is unknown, the query fails, neither answered-measure section has data, or the payload exceeds `INVENTORY_MAX_CHARS` (120,000).

The reasoning is explicit and worth preserving: a partial candidate list could silently change which measure the model judges semantically appropriate, so an oversized packet falls back to SQL discovery rather than being trimmed. For the same reason a survey with products but **no answered measures** is deliberately not treated as an analysis packet — the agent falls back to scoped SQL instead of latching a products-only packet as complete evidence.

Inventory failure is silent to the graph. The agent must discover the reason or the data through SQL if the question needs it.

## Cache

A process-local, lock-protected `OrderedDict` keyed by survey ID — a bounded LRU with lifecycle-sensitive TTLs:

| Survey state | Default TTL | Setting |
|---|---:|---|
| Active / mutable | 300 s | `INVENTORY_CACHE_ACTIVE_TTL_S` |
| `closed` or `archived` | 86,400 s | `INVENTORY_CACHE_CLOSED_TTL_S` |
| Fixed benchmark | 86,400 s | `INVENTORY_CACHE_BENCHMARK_TTL_S` |

Default ceiling `INVENTORY_CACHE_MAX_ENTRIES` = 128. Errors, missing or empty packets and oversized packets are **not** cached. Workers do not share entries, which is part of why follow-ups must reach the same Uvicorn worker — see [[api-funda-agent-exp]].

## Persona inventory

A separate one-statement packet attached only when `is_persona_request()` recognises persona/profile language or respondent-level demographic, motivation, loyalty, behaviour or attitude requests — so ordinary analytical and product-specific motivation questions do not pay its database or context cost.

The query returns every answered, non-product `multiple-choice` question with its full prompt, and deliberately **does not classify demographics by SQL keywords** — that semantic judgment is left to the model. Python post-processing then adds survey enrollment and answered-respondent totals; each question's respondent denominator and survey coverage percentage; respondent count and percentage per category; a **Wilson 95% confidence interval** for each category rate; the observed single- versus multi-select mode; and, for single-select questions, a top-versus-runner-up equal-share test with a **Bonferroni-adjusted** p-value across the question's alternative categories.

`dominance.significant=true` is the deterministic gate for calling a category dominant or representative. A merely largest bucket must be reported as mixed or not clearly separated, and multi-select questions receive no dominance verdict at all because their selections overlap.

Its ceiling is `PERSONA_INVENTORY_MAX_CHARS` (120,000), and it uses the same complete-or-absent rule and lifecycle TTL policy.

Two limits are recorded on the persona route: coverage is **categorical only** — free-text occupation and similar open-ended fields need explicit scoped SQL and careful coding, and must never be fabricated into a category distribution; and dominance is **descriptive, not representativeness** — the intervals and adjusted test quantify sampling uncertainty within this survey, and do not make a convenience sample representative of a market or prove that marginal traits co-occur in one segment.

## Cost

The inventory is eager: a fresh current-survey thread pays for it before the model decides whether it needs analytical measures. The TTL cache mitigates the database work but not the user-message token cost. Noted in [[known-limitations-and-risks]].

## Related

[[funda-agent-exp]] · [[api-funda-agent-exp]] · [[model-and-prompt-routing]] (the packet latch) · [[sql-guardrails-and-scope]] (packet authorization)
