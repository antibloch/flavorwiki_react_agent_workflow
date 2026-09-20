# Reduced Frontend Chat Contract

This contract contains the minimum HTTP and JSON details needed to send a chat message and display the agent’s answer. Telemetry and timing are excluded. Heartbeat and progress events may still arrive on the stream, but are not chat content and must be ignored.

## HTTP request

```http
POST /ask HTTP/1.1
Authorization: Bearer <API_SECRET>
Content-Type: application/json
Accept: application/x-ndjson
```

The API base URL is deployment-specific; combine it with `/ask`.

## Request body

Send the following JSON body to `POST /ask`:

```json
{
  "prompt": "Summarize the results of this survey",
  "survey_id": "c07b2733-c9fa-4a28-aa41-362d3f952517",
  "thread_id": "<optional existing thread ID>",
  "no_inventory": false
}
```

Required fields:

- `prompt`: the user’s message.
- `survey_id`: the survey being discussed.

Optional fields:

- `thread_id`: returned by a previous completed response; use it to continue that conversation.
- `no_inventory`: set to `true` only when inventory loading should be skipped.

The frontend may also send an `X-Request-ID` header containing its own correlation ID. If omitted, the server generates one and returns it in the response headers.

## HTTP response

For an accepted request, the server returns:

```http
HTTP/1.1 200 OK
Content-Type: application/x-ndjson
```

The response body is a streaming sequence of JSON objects separated by newline characters. It is not one JSON object, JSON array, or SSE response. The frontend must read the response incrementally and parse each complete non-empty line independently.

For a non-`2xx` response, no chat stream is guaranteed. Treat the response as an HTTP error and display an appropriate failure message. Do not attempt to parse it as a successful chat response unless the server explicitly returns a supported error body.

## Response body

The response body is newline-delimited JSON (NDJSON). Each non-empty line is one JSON event. Parse each line independently as it arrives.

### `token`

Contains a streamed piece of the answer.

```json
{
  "type": "token",
  "text": "The survey shows ..."
}
```

Append `text` to the visible answer.

### `reset`

Signals that the agent is replacing an earlier intermediate answer.

```json
{
  "type": "reset"
}
```

Clear the current answer buffer before processing later `token` events.

### `done`

Signals a successful final answer.

```json
{
  "type": "done",
  "answer": "The survey shows ...",
  "thread_id": "<thread ID>"
}
```

Render `answer` as the final response and stop reading the stream. Save `thread_id` if the conversation will continue.

### `error`

Signals that the agent could not complete the request.

```json
{
  "type": "error",
  "error": "A human-readable error message"
}
```

Display the error and stop reading.

## Client behavior

1. Send the request with the required authorization and JSON/NDJSON headers.
2. Parse the response incrementally; do not wait for one complete JSON document.
3. Append only `token.text` to the answer.
4. Clear the answer on `reset`.
5. Use `done.answer` as the authoritative final answer.
6. Save `done.thread_id` when continuing the conversation.
7. Stop on `done` or `error`.
8. Ignore any event type not listed in this reduced contract. In particular, do not display or treat `heartbeat`, `status`, `tool_result`, or telemetry fields as chat content.
9. If the stream ends without `done` or `error`, treat it as an incomplete response.

The frontend does not need to send or consume telemetry for this reduced chat-only interface.
