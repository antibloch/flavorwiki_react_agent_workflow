---
type: concept
title: "The {{suggestion}} button contract"
tags: [prompt-design, agent-variant]
sources: [survey-analyst-api-contract, client-boundary-session-handoff]
created: 2026-08-14
updated: 2026-08-14
---

A text-level protocol between the agent's prompts and the UI: when a genuine choice remains, the answer ends with one suggestion per line wrapped in **double braces**, and each is simultaneously the button label and the exact message to send back.

```
Which stats should I run? This survey has several testable measures…

{{Run ANOVA and Tukey on overall liking}}
{{Run ANOVA and Tukey on overall flavor liking}}
{{Run ANOVA and Tukey on overall aroma liking}}
```

Clicking a button is an ordinary `/ask` call with the text sent **verbatim and unmodified** as the next `prompt`, on the same `thread_id`.

## Guarantees the parser may rely on

| Rule | |
|---|---|
| One suggestion per brace pair | `{{a}} {{b}}`, never `{{a, b}}` |
| Plain text only | No newline, no nested braces, no markdown, no UUIDs inside |
| Each on its own line | Never inside a table or mid-sentence |
| Always at the very end | A contiguous block on the answer's last lines |
| Count | 1–5, or 1–3 for feature influence |
| Order | Strongest remaining alternative first; if no analysis ran, the recommendation leads |
| `{{ }}` used for nothing else | Any brace pair is a suggestion |

Each suggestion is written to be a **complete, self-contained question** naming both the test and the measure, so it resolves the ambiguity even if the thread is lost. Passing `thread_id` anyway keeps the exchange coherent and lets the agent reuse question IDs it already resolved.

## When they appear — and when they must not

Only when a real choice exists: a materially underspecified request the agent has answered under a stated assumption, a required input it cannot safely infer, or an analysis it could not complete where a retry or a different measure is sensible.

An assumption-based answer offers only the *other* genuine readings, never the reading it already answered. One button is valid when only one alternative exists. **An unambiguous complete factual answer carries none**, so a UI must not expect suggestions on every response.

[[client-boundary-session-handoff]] records the matching prompt policy for unclear intent: softly name exactly what was not supplied, make the strongest supported framing assumption, answer, and offer self-contained suggestions only for genuine remaining alternatives — never inventing a required tool parameter. It also makes suggestions **prohibited** on a foreign-survey refusal, which is terminal (see [[sql-guardrails-and-scope]]).

## Parse positionally, not by pattern

The critical implementation detail. **Some survey questions contain their own `{{placeholder}}` piping** — measured at 62 questions across 9 surveys, e.g. `How often do you eat {{selectedOption_1}}?`. The agent is instructed to rewrite those as `[selectedOption_1]` when quoting them, but [[survey-analyst-api-contract]] warns against relying on that alone.

The prescribed approach walks **backwards from the end** of the answer, collecting suggestion lines until the first non-suggestion line, tolerating blank lines between them:

```ts
const SUGGESTION = /^[-*\s]*\{\{(.+?)\}\}[\s.]*$/;
```

This yields `[]` for an answer with no suggestions and ignores any `{{...}}` embedded in prose or inside a table. A pattern-matching parser that scanned the whole answer would turn survey piping into phantom buttons.

## Testing

[[regression-workbook]]'s "Suggestions / ambiguity" suite covers this contract in 4 rows, testing both the brace protocol and the cases where buttons must not appear. `scripts/check_suggestions.py` asserts the contract the UI parses.

## Related

[[api-funda-agent-exp]] · [[survey-analyst-api-contract]] · [[regression-workbook]] · [[sql-guardrails-and-scope]]
