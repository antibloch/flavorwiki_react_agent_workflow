# FlavorAI Survey Analyst — API contract

Integration reference for the front-end. Ask a natural-language question about one survey; the
answer streams back token by token.

**Base URL (dev tunnel):** `https://cling-barcode-overhang.ngrok-free.dev`
**Base URL (local):** `http://localhost:8000`
**Machine-readable schema:** `GET /openapi.json` (also committed as `openapi.json`)
**Interactive docs:** `GET /docs`

> **Read [§6 ngrok](#6-ngrok-two-things-that-will-break-you-first) before your first request.**
> Two ngrok behaviours will make a correct integration look broken.

---

## 1. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ask` | Ask a question. Streams the answer. **The only endpoint you need.** |
| `GET` | `/health` | Liveness + active configuration. |
| `GET` | `/admin/conversations?date=YYYY-MM-DD` | List conversations with events on a UTC date; add `&detailed=true` for the agent trace. |
| `GET` | `/threads/{thread_id}/messages?survey_id=SURVEY_UUID` | Read durable conversation turns for one survey; add `&detailed=true` for the agent trace. |
| `DELETE` | `/threads/{thread_id}` | Free a conversation's memory. Call when a chat session ends. |

---

## 2. `POST /ask` — request

`Content-Type: application/json`

| field | type | required | notes |
|---|---|---|---|
| `prompt` | string | **yes** | The question. Min length 1. |
| `survey_id` | string (uuid) | **yes** | Which survey to analyse. |
| `thread_id` | string \| null | no | Reuse the same value to continue a conversation (§5). Omit for one-shot. |
| `no_inventory` | boolean | no | Default `false`. Diagnostics only — leave it alone. |
| `client_id` | string \| null | no | **Do not send.** Derived from `survey_id`. Overrides the lookup if present. |
| `organization_id` | string \| null | no | **Do not send.** Same as above. |

```json
{
  "prompt": "Sort the products by overall liking.",
  "survey_id": "6263cf71-23b7-4462-9ccf-4a00a7267672"
}
```

`client_id` and `organization_id` are *derived server-side* from the survey
(`survey → organization → account`). Not every survey has that chain; when it is missing the
request still succeeds and the `scope` event reports `resolved: false`.

---

## 3. `POST /ask` — response

`200` + `Content-Type: application/x-ndjson`.

**One JSON object per line**, flushed as it happens. Not SSE, not a JSON array — do **not**
`await response.json()`, it will never resolve until the run ends and will fail to parse.

### Event types

| `type` | fields | meaning |
|---|---|---|
| `status` | `stage` | Progress. See stages below. |
| `token` | `text` | A fragment of the answer. **Append in order.** |
| `reset` | `reason`, `tools[]` | **Discard every token received so far.** See §4. |
| `tool_result` | `name`, `chars` | A database/stats tool returned. Useful for a spinner label. |
| `done` | `answer`, `thread_id`, `turns`, `wall_s`, `tokens{}` | Terminal. `answer` is the complete text. |
| `error` | `error`, optional `code` | Terminal failure; `thread_survey_mismatch` means the thread belongs to another survey. |

`status` stages, in order:

| `stage` | extra fields |
|---|---|
| `scope` | `survey_id`, `client_id`, `organization_id`, `resolved` (bool) |
| `inventory` | `chars`, `reused`; follow-ups report `chars: 0` because message 0 already carries the inventory |
| `running` | `thread_id` — the id to reuse for a follow-up |

### Real capture

```json
{"type":"status","stage":"scope","survey_id":"39af3240-…","client_id":"c0b3b212-…","organization_id":"46671273-…","resolved":true}
{"type":"status","stage":"running","thread_id":"req-6cdab7a2-27c8-43b3-ae32-cb5b6c75cc7a"}
{"type":"reset","reason":"tool_call","tools":["nl2sql_tool"]}
{"type":"tool_result","name":"nl2sql_tool","chars":1829}
{"type":"token","text":"On"}
{"type":"token","text":" Aroma"}
{"type":"done","answer":"On Aroma, **Product 2** has the highest mean score: **9.19** …","thread_id":"req-6cdab7a2-…","turns":3,"wall_s":27.5,"tokens":{"input":28310,"cached_input":25088,"output":937}}
```

The answer is Markdown plus optional `gpi-nested-table` fenced JSON artifacts. Ordinary headings,
paragraphs, lists, emphasis and genuinely non-hierarchical flat tables remain Markdown. Standalone
`{{...}}` suggestions remain plain text at the end. Hierarchical results use one recursive JSON
object per logical result instead of model-authored HTML or CSS:

````text
```gpi-nested-table
{"type":"table","columns":["Group","Summary","Details"],"rows":[{"Group":"Appearance","Summary":"Descriptive comparison","Details":{"type":"table","columns":["Attribute","Product 1","Product 2"],"rows":[{"Attribute":"Color intensity","Product 1":"8.59","Product 2":"9.38"}]}}]}
```
````

Every table node has exactly `type`, `columns` and `rows`; `type` is `table`, columns are unique
human-readable strings, and rows are objects whose keys match the columns. Intermediate nodes use
`Details` as the final column and store another table object there. Terminal cells are JSON scalars.
Positional row arrays, HTML, Markdown inside cells, comments and trailing commas are not part of the
contract.

The hierarchy is adaptive rather than threshold-driven. The agent normalizes wide headers, long
rows and nested tool objects into semantic dimensions, then judges repeated column families,
horizontal comparison burden, repeated labels and the user's requested groups at every node. It
nests only when a useful semantic dimension materially improves readability, reassesses every child
independently and stops when that child is readable. There is no mandatory minimum depth; uneven
branches are valid, and the maximum is four grouping levels. Width never licenses invented groups,
and nesting never licenses omitted facts.

The Markdown client should recognize a completed `gpi-nested-table` fence, parse its contents as
JSON, validate the recursive node shape above and render it with trusted components. Styling,
expansion state, responsive layout and accessibility belong entirely to the client. The backend
still streams ordinary text tokens and does not parse, repair or style model-authored table JSON;
clients may wait for the closing fence before rendering the artifact. Malformed artifacts must be
shown as escaped text or an explicit unavailable-table state, never inserted as HTML.

Raw HTML support is no longer needed for hierarchical tables. If the client also renders the
existing persona-card markup or limited `<u>` prose emphasis, continue to sanitize those paths and
style `.persona-card` in trusted client CSS. Do not re-enable `details`, `summary`, inline table CSS,
scripts, event handlers, links, images or document-level tags for nested-table rendering.

A `gpi-word-cloud` fenced block is a trusted backend artifact, not model-authored chart data. The
model chooses an inventory question ID, while `generate_word_cloud` owns scoped retrieval,
tokenization, distinct-answer counts, ordering and version-1 JSON serialization. After success, the
graph returns the tool-owned introduction and block without another model invocation; an untrusted
model-authored block is withheld.

### PCA chart artifacts — `gpi-chart`

Every plottable PCA answer contains one trusted backend-generated chart block. The model selects the
authorized multi-attribute question and interprets the result, but it never authors or edits chart
JSON. The frontend waits for the completed fence, parses the single JSON object, validates it and
renders the supported PCA chart type. Raw HTML, JavaScript, SVG, canvas code, iframes, secrets,
hidden traces and respondent PII are forbidden.

````text
```gpi-chart
{
  "version": 1,
  "type": "pca_biplot_2d",
  "title": "PCA biplot — Appearance",
  "subtitle": "Product-mean profiles; analysis N = 3 products; source N = 17 respondents",
  "x_axis": {"label": "PC1 (74.9986%)"},
  "y_axis": {"label": "PC2 (25.0014%)"},
  "metadata": {
    "source": "Charts API survey statistics",
    "question_id": "QUESTION_UUID",
    "question_label": "Appearance",
    "base_size": 17,
    "products": ["Product 1", "Product 2", "Product 3"],
    "method": "PCA biplot on product mean profiles"
  },
  "products": [
    {"label": "Product 1", "x": -1.27146, "y": -0.89135}
  ],
  "attributes": [
    {"label": "Surface Gloss", "x": 0.98326, "y": -0.18219}
  ]
}
```
````

`pca_biplot_3d` has the same shape plus `z_axis` and a numeric `z` on every product and
attribute. PCA requests default to 2D. When PC3 is available, the response includes a trailing
`{{Show a 3D PCA plot of [measure]}}` suggestion; an explicit `pca_plot_dimensions=3d` request emits
3D. A 3D request falls back to `pca_biplot_2d` when PC3 is not a retained usable component; it never
invents zero Z coordinates. If PC1 or PC2 is unavailable,
no invalid chart is emitted. Chart payloads contain at most 300 product-plus-attribute points.

The chart block appears after explanatory prose and before the trailing `{{...}}` suggestion block.
Every actual 3D result includes a final self-contained suggestion for its equivalent 2D plot. The
runtime may emit a `reset` immediately before the finalized answer when deterministic artifact
insertion changes already streamed model text; tokens since the last reset still equal
`done.answer` exactly.

### Other UI chart payloads — `gpi-chart`

For an explicit non-PCA chart request, the agent may emit a complete `gpi-chart` JSON block when
tool evidence contains structured chart data. Supported examples include `scatterplot`,
`bar_chart`, `column_chart`, `line_chart`, `pie_chart`, `heatmap`, `dot_plot`, `histogram`, and
`penalty_scatterplot`. Every payload must use `version: 1`, numeric values/coordinates, concise
labels, and no more than 300 points. Scatterplots use `series[].data[]` with numeric `x` and `y`.
The runtime rejects unsupported types, malformed JSON, unsafe markup, non-numeric scatter points,
and oversized payloads. PCA and word-cloud blocks remain deterministic tool-owned artifacts.

### Guarantees

- Exactly one terminal event per request: `done` **or** `error`, never both, always last.
- `done.answer` always holds the complete final text. **A client that does not want live
  typing can ignore every other event.**
- Concatenating all `token` events since the last `reset` equals `done.answer`. Verified.

---

## 4. The one rule that is easy to get wrong: `reset`

The agent is a reasoning loop. It may write prose, decide it needs to query the database, and
then start over. Prose from a turn that ends in a tool call **is not the answer**.

Whether a turn is final is not knowable until it finishes, so tokens stream live and a `reset`
is sent if that turn turned out to be a tool call.

> **On `reset`, clear the accumulated answer buffer and whatever you have rendered.**

Ignore this and users will see a half-written thought, then the real answer appended to it.

If that is not worth implementing, do not render tokens at all — just show a spinner and print
`done.answer`. Both are correct; a partial implementation is not.

---

## 4b. Suggestion buttons — `{{...}}`

When the agent offers a choice of next action it ends the answer with suggestions wrapped in
**double braces**, one per line:

```
Which stats should I run? This survey has several testable measures…

{{Run ANOVA and Tukey on overall liking}}
{{Run ANOVA and Tukey on overall flavor liking}}
{{Run ANOVA and Tukey on overall aroma liking}}
```

Render each as a button. **The text inside is both the label and the message to POST back on
click** — send it verbatim as the next `prompt`, on the same `thread_id`, and the agent resolves
it (it maps the plain-English measure name to the right question id from the conversation).

### Guarantees the parser can rely on

| rule | |
|---|---|
| one suggestion per brace pair | `{{a}} {{b}}`, never `{{a, b}}` |
| plain text only | no newline, no nested braces, no markdown, no uuids inside |
| each on its own line | never inside a table or mid-sentence |
| always at the very end | contiguous block, last lines of the answer |
| count | 1–5 (feature influence: 1–3) |
| order | strongest remaining alternative first; if no analysis ran, the recommendation leads |
| `{{ }}` used for nothing else | any brace pair is a suggestion |

### When they appear

Only when there is a real choice to make — a materially underspecified request the agent has
answered under a stated assumption, a required input it cannot safely infer, or an analysis it
could not complete where a retry or different measure is sensible. An assumption-based answer
offers only the other genuine readings, not the reading it already answered; one button is valid
when only one alternative exists. **An unambiguous complete factual answer carries none**, so do
not build UI that expects suggestions every time.

### Parsing — take the trailing block only

Some **survey questions contain their own `{{placeholder}}` piping** (measured: 62 questions
across 9 surveys, e.g. `How often do you eat {{selectedOption_1}}?`). The agent is instructed to
rewrite those as `[selectedOption_1]` when quoting them, but do not rely on that alone — parse
positionally, so a stray brace mid-answer can never become a button.

```ts
const SUGGESTION = /^[-*\s]*\{\{(.+?)\}\}[\s.]*$/;

function splitSuggestions(answer: string) {
  const lines = answer.trimEnd().split("\n");
  const buttons: string[] = [];
  // Walk backwards from the end: suggestions are always the last lines.
  let i = lines.length;
  while (i > 0) {
    const line = lines[i - 1].trim();
    if (!line) { i--; continue; }               // tolerate blank lines between them
    const m = line.match(SUGGESTION);
    if (!m) break;                              // first non-suggestion line ends the block
    buttons.unshift(m[1].trim());
    i--;
  }
  return { prose: lines.slice(0, i).join("\n").trimEnd(), buttons };
}
```

This yields `[]` for an answer with no suggestions, and ignores any `{{...}}` embedded in prose
or inside a table.

Clicking a button is an ordinary `/ask` call — send the text **verbatim and unmodified**:

```ts
await ask(base, { prompt: buttonText, survey_id, thread_id });
```

Each suggestion is written to be a **complete, self-contained question** that resolves the
ambiguity on its own — it names both the test and the measure, so it works even if the thread is
lost. Pass `thread_id` anyway: it keeps the exchange coherent and lets the agent reuse the
question ids it already resolved.

---

## 5. Conversations (optional)

Send the same `thread_id` on subsequent requests and the agent remembers the exchange, so
follow-ups can be elliptical — *"and what was its standard deviation?"* works.

```json
{"prompt": "What is the mean Aroma per product?", "survey_id": "…", "thread_id": "chat-abc123"}
{"prompt": "Which was highest, and its SD?",      "survey_id": "…", "thread_id": "chat-abc123"}
```

Generate the id client-side (any stable string). Omit it and every request is independent.
The survey is fixed when the thread starts: reusing that id with another `survey_id` returns
`thread_survey_mismatch`. Use a new id or delete the old thread first.

Scope and the pre-fetched inventory live in the thread's first message and are not appended
again on follow-ups. A thread started with `no_inventory: true` therefore remains
inventory-free for its lifetime.

**Call `DELETE /threads/{thread_id}` when a chat ends.** Memory is held in the server process
and grows with the conversation (~24 MB at 10 rounds). Nothing expires it automatically.

Memory is **process-local**: it does not survive a server restart, and it is not shared between
workers. Treat it as a session convenience, not storage.

### Durable conversation log

The agent also writes a SQLite conversation log incrementally as events occur. It is a recovery
and audit trace, not the source of context for the live agent. Each event includes `survey_id`,
`thread_id`, `turn`, `kind`, optional `name`, `content`, and its event-time `created_at`.

Read the log with the survey filter required:

```http
GET /threads/chat-abc123/messages?survey_id=SURVEY_UUID
GET /threads/chat-abc123/messages?survey_id=SURVEY_UUID&detailed=true
```

The default response includes only `user` and `final` events. `detailed=true` additionally
includes intermediate `llm`, `tool_call`, and `tool_output` events. Events already written remain
available if a client disconnects during a request. In deployment, the SQLite file is stored in
the named Docker volume configured by Compose; deleting that volume deletes the log.

For reviewing activity across conversations, use the authenticated date query:

```http
GET /admin/conversations?date=2026-09-04&detailed=true
```

The date is a UTC calendar date. The response groups events by `thread_id` and `survey_id`, so a
local log browser can show every conversation active that day together with its survey ID. The
endpoint is intended for a protected administrator tool, not a public browser route.

---

## 6. Timing and concurrency — design for this

**A request takes 5–90 seconds.** Most of that is before the first token: the model reasons,
queries the database, sometimes several times. Measured: 7 s typical, 30 s for a multi-step
question.

- Set client timeouts to **at least 120 s**. Default HTTP-client timeouts will cut runs off.
- The first `status` event arrives in ~100 ms, so you can show "thinking…" immediately, then
  swap to the streamed answer at the first `token`.
- There can be a silent 5–25 s gap between `running` and the first `token`. That is normal.
  Do not treat silence as a dropped connection.

**The server runs one request at a time.** A second request queues until the first finishes — it
is not rejected, just slow. Two users at once means the second waits for the first. `GET /health`
reports `busy: true` while a run is in flight.

Do not fire parallel requests expecting parallel answers. If concurrency is needed, that is a
back-end scaling change (more worker processes), not something the client can work around.

---

## 7. Errors

| Situation | How it surfaces |
|---|---|
| Missing/invalid field | **HTTP 422** before the stream opens. Handle as a normal HTTP error. |
| Anything failing mid-run | **HTTP 200**, then an `error` event in the stream. |

The second case is the one to design for: the status code is already committed to 200 by the time
anything can go wrong, so **a failure is a line in the body, not a status code**. A client that
only checks `response.ok` will silently show a blank answer.

Treat a stream that ends **without** a `done` or `error` event as a dropped connection.

The 422 body is FastAPI's standard shape:

```json
{"detail":[{"type":"missing","loc":["body","survey_id"],"msg":"Field required","input":{"prompt":"x"}}]}
```

---

## 9. Reference client (TypeScript)

Handles the two things that are easy to get wrong: partial lines and `reset`.

```ts
type Ask = {
  prompt: string;
  survey_id: string;
  thread_id?: string;
};

export async function ask(
  base: string,
  body: Ask,
  onToken: (full: string) => void,   // called with the answer SO FAR, after every token
): Promise<string> {
  const res = await fetch(`${base}/ask`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "ngrok-skip-browser-warning": "true",   // required through ngrok — see §6
    },
    body: JSON.stringify(body),
  });

  // Validation errors arrive here, before the stream opens.
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";   // partial NDJSON line, NOT the answer
  let answer = "";
  let finished = false;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    // A chunk is not a line. It can hold several lines, or half of one.
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";          // keep the incomplete tail for the next chunk

    for (const line of lines) {
      if (!line.trim()) continue;
      const ev = JSON.parse(line);

      switch (ev.type) {
        case "token":
          answer += ev.text;
          onToken(answer);
          break;

        case "reset":
          // That turn was a database query, not the answer. Throw it away.
          answer = "";
          onToken(answer);
          break;

        case "done":
          answer = ev.answer;            // authoritative
          onToken(answer);
          finished = true;
          break;

        case "error":
          throw new Error(ev.error);
      }
    }
  }

  if (!finished) throw new Error("stream ended without a done event");
  return answer;
}
```

Usage:

```ts
const answer = await ask(
  process.env.NEXT_PUBLIC_API_BASE!,
  { prompt: "Sort the products by overall liking.", survey_id: SURVEY_ID },
  (partial) => setAnswer(partial),   // re-render as it types
);
```

### curl, for a quick check

```bash
curl -N -X POST https://cling-barcode-overhang.ngrok-free.dev/ask \
  -H 'content-type: application/json' \
  -H 'ngrok-skip-browser-warning: true' \
  -d '{"prompt":"Name the products tested.","survey_id":"39af3240-42a8-4e35-8c7d-c61703d5ce3f"}'
```

`-N` is required; without it curl buffers and the streaming is invisible.

---

## 10. CORS

Open by default (`Access-Control-Allow-Origin: *`), so any origin can call it. Preflight is
handled. Credentials are **not** allowed — do not send cookies; there is no auth.

Lock it down with `CORS_ORIGINS=https://app.example.com,https://staging.example.com` on the
server when it leaves development.

---

## 11. `GET /health`

```json
{
  "status": "ok",
  "busy": false,
  "model_strong": "gpt-5.5",
  "model_weak": "gpt-5.4-mini",
  "streaming": true,
  "max_llm_steps": 12,
  "history_max_rounds": 5,
  "history_max_messages": 47,
  "tool_char_limit": 250000,
  "enable_digest": false
}
```

`busy` tells you a run is in flight (§7). If `streaming` is ever `false`, no `token` events will
be emitted and only `done` will arrive — that is a server misconfiguration, not a client bug.

---

## 12. Not in this API yet

Say so early if you need any of these; none are client-side workarounds.

- **No authentication.** Anyone who can reach the URL can query the database.
- **No cancellation.** Closing the connection does not stop the run server-side.
- **No pagination or partial results** — one question, one answer.
- **No survey listing.** The client must already know `survey_id`.
- **No request id in errors**, so correlating a failure with server logs is manual.
