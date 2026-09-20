"""HTTP wrapper around funda_agent_exp.py: POST a question, stream the answer token by token.

    uvicorn api_funda_agent_exp:app --host 0.0.0.0 --port 8000

    curl -N -X POST localhost:8000/ask -H 'content-type: application/json' -d '{
      "prompt":    "What are the products tested in this survey?",
      "survey_id": "39af3240-42a8-4e35-8c7d-c61703d5ce3f"}'

survey_id is the only id the caller needs: client_id and organization_id are derived from it
(see resolve_scope). Passing them explicitly still works and overrides the lookup.

The response is NDJSON -- one JSON object per line, flushed as it happens. Not SSE: this is a
POST, so a browser EventSource cannot consume it anyway, and every client that can read a POST
body incrementally can read newline-delimited JSON with no extra dependency.

Two things about this agent shape the wrapper, and each is load-bearing:

1. THE AGENT KEEPS ITS QUERY SCOPE IN A MODULE GLOBAL -- `_SCOPE`, whose ids nl2sql_tool binds.
   Two requests in flight at once would read each other's scope and query the wrong survey. Runs
   are therefore SERIALISED behind a lock. See the note on _RUN_LOCK before raising throughput.

2. THE AGENT CALLS `.invoke()`, NOT `.stream()`, so by default no tokens are emitted at all.
   Setting `streaming=True` on the ChatOpenAI instance makes LangChain fetch over the
   streaming endpoint and fire per-token callbacks, which LangGraph surfaces as
   `stream_mode="messages"` -- without touching funda_agent_exp.py.

Only the last turn is the answer. The agent is a ReAct loop: earlier turns may emit prose
   alongside a tool call, and that prose is not the answer. Whether a turn is final is not
   knowable until it ends, so tokens stream live and a `reset` event is emitted if the turn that
   produced them turns out to have called a tool. A client that renders tokens as they arrive
   must clear its buffer on `reset`. Clients that only want the finished text can ignore every
   event except `done`, which always carries the complete answer.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
import uuid
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import funda_agent_exp as agent
from agent_instructions import scoped_query_survey_only
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

_SECRET_ENV_NAME = "HERBALIFE_EXTERNAL_ACCESS_SECRET"
_EXTERNAL_ACCESS_SECRET = os.getenv(_SECRET_ENV_NAME, "")
if len(_EXTERNAL_ACCESS_SECRET) < 32:
    raise RuntimeError(
        f"{_SECRET_ENV_NAME} is required and must contain at least 32 characters"
    )

_bearer_scheme = HTTPBearer(auto_error=False)


def require_external_access(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
) -> None:
    """Reject callers before any database or model work begins."""
    valid = (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and secrets.compare_digest(
            credentials.credentials.encode("utf-8"),
            _EXTERNAL_ACCESS_SECRET.encode("utf-8"),
        )
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


# See point 2 above. Set before the graph is built; the tool-bound variants wrap these same
# instances by reference, so they inherit it.
agent.llm.streaming = True

# One checkpointer for the process, so a caller that repeats a thread_id gets short-term memory
# (funda_agent_exp §3.4). It is in-memory and grows with the conversation -- see /threads.
CHECKPOINTER = InMemorySaver()
GRAPH = agent.build_graph(CHECKPOINTER)

# The agent's scope lives in a module global (point 1). Until that is refactored, one run at a
# time is the only correct setting. Requests queue here rather than being rejected; a run is
# commonly several seconds, so set a client timeout accordingly. To serve real concurrency, give
# each worker its own process (uvicorn --workers N). Making the agent itself re-entrant means
# moving _SCOPE into AgentState.
_RUN_LOCK = asyncio.Lock()

# The agent module owns the shared SQLite logger so CLI and API traces have one schema and
# one survey-scoped read path.
CONVERSATION_DB = agent.CONVERSATION_DB

app = FastAPI(title="FlavorAI survey analyst", version="1.0")

# Without this a browser front-end on any other origin is blocked before the request ever
# reaches the server -- which is every front-end, since this runs on its own host/port (and
# through an ngrok domain in dev). Backend callers authenticate with a Bearer secret; keep
# CORS_ORIGINS empty for backend-only use or set an exact browser-origin allow-list.
#
# allow_credentials stays False on purpose: the CORS spec forbids "*" together with credentials,
# and browsers reject the combination outright. If cookie auth is ever added, replace "*" with
# explicit origins and flip this to True -- one without the other silently breaks.
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------------------
# Scope resolution: the caller supplies survey_id only.
#
# client_id and organization_id are NOT free-standing inputs -- both are functions of the
# survey, reachable as survey.organization_id -> organization.account_id -> account.client_id.
# Verified against all four surveys used in the benchmarks: the derived pair equals the
# hardcoded constants exactly. survey.id is globally unique (1,347 of 1,347 distinct), so the
# survey alone identifies the row set.
#
# They are still derived rather than dropped, because the agent genuinely uses them: 115 of 311
# recorded SQL statements join `question -> survey -> organization -> account` and filter
# `ac.client_id = :client_id`. For a single-survey question that predicate is redundant (it can
# only narrow an already-unique survey), but for a benchmark or historical question it is how
# the agent finds SIBLING surveys of the same client. Dropping the ids would quietly remove that
# capability; deriving them keeps behaviour identical to the CLI.
#
# The chain does not always exist. Measured DB-wide: only 470 of 1,347 surveys resolve, and the
# other 877 have `organization_id IS NULL` outright -- among them 220 surveys holding 129,259
# answers, i.e. MORE data-bearing surveys than the 110 that do resolve. So an unresolved scope
# cannot be an error. Those requests run with survey_id alone, and _scope_text tells the model
# so; binding only the keys that exist also keeps the prompt from advertising unavailable ids.
_SCOPE_SQL = """
SELECT o.id::text AS organization_id, ac.client_id::text AS client_id
FROM survey s
JOIN organization o ON o.id = s.organization_id
JOIN account ac ON ac.id = o.account_id
WHERE s.id = :survey_id
"""
_scope_cache: dict[str, tuple[str, str] | None] = {}


def resolve_scope(survey_id: str) -> tuple[str, str] | None:
    """(client_id, organization_id) for a survey, or None when the chain does not exist."""
    if survey_id in _scope_cache:
        return _scope_cache[survey_id]
    out = None
    try:
        from sqlalchemy import text as _text
        with agent._sql_engine().connect() as conn:
            row = conn.execute(_text(_SCOPE_SQL), {"survey_id": survey_id}).mappings().first()
        if row and row["client_id"] and row["organization_id"]:
            out = (row["client_id"], row["organization_id"])
    except Exception:  # noqa: BLE001 - a lookup failure degrades to survey-only scope
        out = None
    _scope_cache[survey_id] = out
    return out


def _scope_text(prompt: str, survey_id: str, scope: tuple[str, str] | None) -> str:
    """The SCOPE preamble. Identical to the CLI's when the ids resolved."""
    if scope:
        return agent.scoped_query(prompt, scope[0], scope[1], survey_id)
    # Degraded: advertise only what is bound, and say plainly that the others do not exist for
    # this survey, so the model does not reach for a placeholder that would fail to bind.
    return scoped_query_survey_only(prompt, survey_id)


class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="the question to answer")
    survey_id: str
    # Derived from survey_id. Supply them only to override the lookup.
    client_id: str | None = None
    organization_id: str | None = None
    # Optional. Repeat a thread_id to continue a conversation: follow-ups may then be elliptical
    # ("and its standard deviation?"). Omitted means a fresh thread, i.e. stateless.
    thread_id: str | None = None
    # Skip the pre-fetched inventory and make the agent discover the survey by SQL. Slower and
    # less accurate; here because the benchmarks need it.
    no_inventory: bool = False


class ClientTelemetryRequest(BaseModel):
    """Timing-only client report; never accepts prompt or answer content."""

    request_id: str = Field(..., min_length=1, max_length=128)
    turn: int = Field(..., ge=1)
    response_headers_ms: float | None = Field(default=None, ge=0)
    first_byte_ms: float | None = Field(default=None, ge=0)
    first_status_ms: float | None = Field(default=None, ge=0)
    first_token_ms: float | None = Field(default=None, ge=0)
    last_event_ms: float | None = Field(default=None, ge=0)
    done_ms: float | None = Field(default=None, ge=0)
    client_total_ms: float | None = Field(default=None, ge=0)
    last_event_type: str | None = Field(default=None, max_length=64)
    client_error_type: str | None = Field(default=None, max_length=128)
    client_error_message: str | None = Field(default=None, max_length=500)


def _chunk_text(content: Any) -> str:
    """Text out of one streamed chunk, WITHOUT stripping.

    Deliberately not message_to_text(): that strips each part, which would eat the leading space
    on tokens like ' from' and silently run words together. Responses-API chunks arrive as a list
    of typed blocks -- `reasoning` (opaque, often a KB of base64) carries no "text" key and is
    skipped here by construction.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    return ""


def _line(**obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and len(candidate) <= 128 and all(ch.isalnum() or ch in "-_.:" for ch in candidate):
        return candidate
    return str(uuid.uuid4())


def _usage_from_message(message: Any) -> dict[str, int]:
    """Normalize LangChain/OpenAI usage metadata without logging message content."""
    usage = getattr(message, "usage_metadata", None) or {}
    response = getattr(message, "response_metadata", None) or {}
    provider = response.get("token_usage") or response.get("usage") or {}
    details = usage.get("input_token_details") or provider.get("prompt_tokens_details") or {}
    input_tokens = usage.get("input_tokens", provider.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", provider.get("completion_tokens", 0))
    cached = details.get("cache_read", details.get("cached_tokens", 0))
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cached_input_tokens": int(cached or 0),
    }


async def _run(
    req: AskRequest,
    request: Request | None = None,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    thread_id = req.thread_id or f"req-{uuid.uuid4()}"
    request_id = _request_id(request_id)
    request_started = time.perf_counter()
    lock_started = request_started
    lock_wait_ms = 0.0
    timing_written = False
    telemetry: dict[str, Any] = {
        "type": "round_timing",
        "request_id": request_id,
        "thread_id": thread_id,
        "survey_id": req.survey_id,
        "turn": None,
        "lock_wait_ms": 0.0,
        "conversation_log_ms": 0.0,
        "scope_ms": 0.0,
        "inventory_ms": 0.0,
        "inventory_cache_hit": None,
        "graph_ms": 0.0,
        "llm_total_ms": 0.0,
        "llm_turn_count": 0,
        "llm_first_chunk_ms": None,
        "llm_final_turn_ms": None,
        "final_response_ttft_ms": None,
        "final_response_ttft_from_final_turn_ms": None,
        "final_response_generation_ms": None,
        "first_llm_input_tokens": None,
        "first_llm_cached_input_tokens": None,
        "first_llm_uncached_input_tokens": None,
        "first_llm_input_token_source": "unavailable",
        "final_response_output_tokens": None,
        "final_response_visible_chars": 0,
        "total_agent_output_tokens": 0,
        "token_usage_available": False,
        "tool_total_ms": 0.0,
        "tool_call_count": 0,
        "server_stream_total_ms": None,
        "max_silent_gap_ms": 0.0,
        "completed": False,
        "client_disconnected": False,
        "last_event_type": None,
        "error_type": None,
    }
    last_emitted = request_started
    current_llm_started: float | None = None
    current_llm_first_chunk: float | None = None
    current_llm_first_visible: float | None = None
    current_llm_visible_chars = 0
    current_tool_started: float | None = None
    llm_usages: list[dict[str, int]] = []

    async def emit(event: dict[str, Any]) -> str:
        nonlocal last_emitted
        now = time.perf_counter()
        telemetry["max_silent_gap_ms"] = max(
            float(telemetry["max_silent_gap_ms"]), (now - last_emitted) * 1000
        )
        last_emitted = now
        telemetry["last_event_type"] = event.get("type")
        return _line(**event)

    async def write_timing() -> None:
        nonlocal timing_written
        if timing_written or telemetry["turn"] is None:
            return
        telemetry["server_stream_total_ms"] = (time.perf_counter() - request_started) * 1000
        await asyncio.to_thread(
            agent.append_conversation_event,
            thread_id, req.survey_id, int(telemetry["turn"]), "round_timing",
            json.dumps(telemetry, ensure_ascii=False, sort_keys=True),
        )
        timing_written = True

    try:
        lock_started = time.perf_counter()
        async with _RUN_LOCK:
            lock_wait_ms = (time.perf_counter() - lock_started) * 1000
            telemetry["lock_wait_ms"] = round(lock_wait_ms, 3)
            config = {"recursion_limit": 2 * agent.MAX_LLM_STEPS + 2,
                      "configurable": {"thread_id": thread_id}}
            previous = GRAPH.get_state(config).values or {}
            is_follow_up = bool(previous.get("messages"))
            previous_survey = previous.get("thread_survey_id")
            if is_follow_up and previous_survey != req.survey_id:
                yield await emit({
                    "type": "error",
                    "code": "thread_survey_mismatch",
                    "error": (f"Thread {thread_id!r} belongs to survey {previous_survey!r}; "
                               f"use a new thread_id or delete the existing thread before using "
                               f"survey {req.survey_id!r}."),
                })
                return

            log_started = time.perf_counter()
            turn = await asyncio.to_thread(
                agent.begin_conversation_turn, thread_id, req.survey_id
            )
            await asyncio.to_thread(
                agent.append_conversation_event,
                thread_id, req.survey_id, turn, "user", req.prompt,
            )
            telemetry["turn"] = turn
            telemetry["conversation_log_ms"] += (time.perf_counter() - log_started) * 1000

            # survey_id is the only required input; the other two are looked up from it.
            # An explicit value in the request still wins, so the old contract keeps working.
            if req.client_id and req.organization_id:
                scope = (req.client_id, req.organization_id)
            else:
                scope_started = time.perf_counter()
                scope = await asyncio.to_thread(resolve_scope, req.survey_id)
                telemetry["scope_ms"] = (time.perf_counter() - scope_started) * 1000

            # Exactly what main() does, in the same order: publish the scope so nl2sql_tool can
            # bind :client_id/:organization_id/:survey_id and repair a near-miss literal, then
            # pre-fetch the inventory, then run. _SCOPE is REPLACED, not updated -- it is a
            # process global, and leaving the previous request's client_id in it would let the
            # model bind a stale id on a survey that has none of its own.
            agent._SCOPE.clear()
            agent._SCOPE["survey_id"] = req.survey_id
            if scope:
                agent._SCOPE["client_id"], agent._SCOPE["organization_id"] = scope
            yield await emit({"type": "status", "stage": "scope", "survey_id": req.survey_id,
                              "client_id": scope[0] if scope else None,
                              "organization_id": scope[1] if scope else None,
                              "resolved": bool(scope)})

            inventory_attached = bool(previous.get("inventory_attached")) if is_follow_up else False
            inventory_chars = int(previous.get("inventory_chars") or 0) if is_follow_up else 0
            # Message 0 is the cacheable conversation anchor and _trim_history always retains it.
            # Reconstructing scope or inventory on a follow-up would both duplicate tokens and
            # shift the prefix that OpenAI can reuse.
            question = req.prompt if is_follow_up else _scope_text(req.prompt, req.survey_id, scope)
            if not is_follow_up and not req.no_inventory:
                # Blocking DB work: off the event loop so other requests can still be served
                # their queue position and heartbeats.
                inventory_started = time.perf_counter()
                inventory = await asyncio.to_thread(agent.survey_inventory, req.survey_id)
                telemetry["inventory_ms"] = (time.perf_counter() - inventory_started) * 1000
                question += inventory
                inventory_attached = bool(inventory)
                inventory_chars = len(inventory)
                telemetry["inventory_cache_hit"] = False
                yield await emit({"type": "status", "stage": "inventory",
                                  "chars": len(inventory), "reused": False})
            elif is_follow_up and not req.no_inventory:
                telemetry["inventory_cache_hit"] = inventory_attached
                yield await emit({"type": "status", "stage": "inventory", "chars": 0,
                                  "inventory_chars": inventory_chars,
                                  "reused": inventory_attached})

            yield await emit({"type": "status", "stage": "running", "thread_id": thread_id})

            inputs = {
                "messages": [HumanMessage(content=question)],
                "thread_survey_id": req.survey_id,
                "inventory_attached": inventory_attached,
                "inventory_chars": inventory_chars,
                # Per-question scratch, overwritten every request. On a repeated thread_id this
                # is what stops round 2 inheriting round 1's spent step budget.
                "number_of_steps": 0,
                "last_sql_observations": {}, "zero_row_streak": 0,
                "word_cloud_artifact": "", "word_cloud_intro": "",
                "pca_chart_artifacts": [],
            }
            answer = ""
            turn_visible = ""
            progress_filter = agent.ProgressDeltaStreamFilter()
            brace_filter = agent.BraceDeltaStreamFilter()
            # "messages" carries the token deltas; "updates" tells us, after each node, whether
            # the turn we just streamed was a tool call (discard) or the answer (keep).
            graph_started = time.perf_counter()
            stream_iter = GRAPH.astream(
                inputs, stream_mode=["messages", "updates"], config=config
            ).__aiter__()
            next_event = asyncio.create_task(stream_iter.__anext__())
            try:
                while True:
                    done, _ = await asyncio.wait({next_event}, timeout=15.0)
                    if not done:
                        if request is not None and await request.is_disconnected():
                            telemetry["client_disconnected"] = True
                            telemetry["error_type"] = "client_disconnected"
                            break
                        continue
                    try:
                        mode, payload = next_event.result()
                    except StopAsyncIteration:
                        break
                    next_event = asyncio.create_task(stream_iter.__anext__())

                    if mode == "messages":
                        chunk, _meta = payload
                        # AI chunks ONLY. Tool messages are deliberately not streamed as answer text.
                        if not isinstance(chunk, AIMessageChunk):
                            continue
                        if current_llm_started is None:
                            model_timing = agent.model_timing_snapshot()
                            current_llm_started = (
                                model_timing.get("active_started_at")
                                or model_timing.get("started_at")
                                or time.perf_counter()
                            )
                            telemetry["llm_turn_count"] += 1
                        if current_llm_first_chunk is None:
                            current_llm_first_chunk = time.perf_counter()
                            if telemetry["llm_first_chunk_ms"] is None:
                                telemetry["llm_first_chunk_ms"] = round(
                                    (current_llm_first_chunk - request_started) * 1000, 3
                                )
                        text = _chunk_text(getattr(chunk, "content", ""))
                        if not text:
                            continue
                        visible = progress_filter.feed(text)
                        visible = brace_filter.feed(visible)
                        if visible:
                            if current_llm_first_visible is None:
                                current_llm_first_visible = time.perf_counter()
                            turn_visible += visible
                            current_llm_visible_chars += len(visible)
                            yield await emit({"type": "token", "text": visible})

                    elif mode == "updates":
                        for node, upd in (payload or {}).items():
                            msgs = (upd or {}).get("messages") or []
                            if node == "LLM" and msgs:
                                tail = progress_filter.finish()
                                tail = brace_filter.feed(tail)
                                brace_filter.finish()
                                if tail:
                                    if current_llm_first_visible is None:
                                        current_llm_first_visible = time.perf_counter()
                                    turn_visible += tail
                                    current_llm_visible_chars += len(tail)
                                    yield await emit({"type": "token", "text": tail})
                                progress_filter = agent.ProgressDeltaStreamFilter()
                                brace_filter = agent.BraceDeltaStreamFilter()
                                last = msgs[-1]
                                usage = _usage_from_message(last)
                                llm_usages.append(usage)
                                if usage["input_tokens"] or usage["output_tokens"]:
                                    telemetry["token_usage_available"] = True
                                telemetry["total_agent_output_tokens"] += usage["output_tokens"]
                                if len(llm_usages) == 1:
                                    telemetry["first_llm_input_tokens"] = usage["input_tokens"]
                                    telemetry["first_llm_cached_input_tokens"] = usage["cached_input_tokens"]
                                    telemetry["first_llm_uncached_input_tokens"] = max(
                                        0, usage["input_tokens"] - usage["cached_input_tokens"]
                                    )
                                    telemetry["first_llm_input_token_source"] = (
                                        "provider" if usage["input_tokens"] else "unavailable"
                                    )
                                turn_started = current_llm_started or time.perf_counter()
                                turn_finished = time.perf_counter()
                                model_timing = agent.model_timing_snapshot()
                                turn_duration_ms = model_timing.get("duration_ms") or (
                                    (turn_finished - turn_started) * 1000
                                )
                                telemetry["llm_total_ms"] += turn_duration_ms
                                has_tools = bool(getattr(last, "tool_calls", None))
                                if has_tools:
                                    current_tool_started = turn_finished
                                    telemetry["tool_call_count"] += len(last.tool_calls)
                                    prose = agent.message_to_text(last)
                                    if prose:
                                        await asyncio.to_thread(
                                            agent.append_conversation_event,
                                            thread_id, req.survey_id, turn, "llm", prose,
                                        )
                                    for tc in last.tool_calls:
                                        await asyncio.to_thread(
                                            agent.append_conversation_event,
                                            thread_id, req.survey_id, turn, "tool_call",
                                            json.dumps(tc.get("args") or {}, default=str,
                                                       ensure_ascii=False),
                                            tc.get("name") or "",
                                        )
                                    yield await emit({
                                        "type": "reset", "reason": "tool_call",
                                        "tools": [tc["name"] for tc in last.tool_calls],
                                    })
                                else:
                                    telemetry["llm_final_turn_ms"] = round(
                                        turn_duration_ms, 3
                                    )
                                    telemetry["final_response_visible_chars"] = current_llm_visible_chars
                                    telemetry["final_response_output_tokens"] = usage["output_tokens"] or None
                                    answer = agent.message_to_text(last)
                                    await asyncio.to_thread(
                                        agent.append_conversation_event,
                                        thread_id, req.survey_id, turn, "final", answer,
                                    )
                                    streamed = turn_visible.strip()
                                    if answer != streamed:
                                        if not streamed or answer.startswith(streamed):
                                            suffix = answer[len(streamed):]
                                            if suffix:
                                                current_llm_visible_chars += len(suffix)
                                                yield await emit({"type": "token", "text": suffix})
                                        else:
                                            yield await emit({"type": "reset", "reason": "finalized_artifact"})
                                            if answer:
                                                current_llm_visible_chars += len(answer)
                                                yield await emit({"type": "token", "text": answer})
                                    telemetry["final_response_visible_chars"] = current_llm_visible_chars
                                    telemetry["final_response_generation_ms"] = round(
                                        turn_duration_ms, 3
                                    )
                                    final_visible_at = current_llm_first_visible or turn_finished
                                    telemetry["final_response_ttft_ms"] = round(
                                        (final_visible_at - request_started) * 1000, 3
                                    )
                                    telemetry["final_response_ttft_from_final_turn_ms"] = round(
                                        (final_visible_at - turn_started) * 1000, 3
                                    )
                                current_llm_started = None
                                current_llm_first_chunk = None
                                current_llm_first_visible = None
                                current_llm_visible_chars = 0
                                turn_visible = ""
                            elif node == "tools":
                                if current_tool_started is not None:
                                    telemetry["tool_total_ms"] += (
                                        time.perf_counter() - current_tool_started
                                    ) * 1000
                                    current_tool_started = None
                                for tm in msgs:
                                    tool_output = agent.message_to_text(tm)
                                    await asyncio.to_thread(
                                        agent.append_conversation_event,
                                        thread_id, req.survey_id, turn, "tool_output",
                                        tool_output, getattr(tm, "name", ""),
                                    )
                                    yield await emit({
                                        "type": "tool_result", "name": getattr(tm, "name", ""),
                                        "chars": len(tool_output),
                                    })
            finally:
                if not next_event.done():
                    next_event.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await next_event
                with contextlib.suppress(Exception):
                    await stream_iter.aclose()

            telemetry["graph_ms"] = (time.perf_counter() - graph_started) * 1000
            telemetry["completed"] = not telemetry["client_disconnected"]
            telemetry["server_stream_total_ms"] = (time.perf_counter() - request_started) * 1000
            yield await emit({"type": "done", "answer": answer, "thread_id": thread_id})
    except asyncio.CancelledError:
        telemetry["error_type"] = telemetry["error_type"] or "stream_cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        telemetry["error_type"] = type(exc).__name__
        yield await emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        await write_timing()


@app.post("/ask", dependencies=[Depends(require_external_access)])
async def ask(req: AskRequest, request: Request) -> StreamingResponse:
    request_id = _request_id(request.headers.get("x-request-id"))
    return StreamingResponse(
        _run(req, request=request, request_id=request_id),
        media_type="application/x-ndjson",
        # Without this an nginx/ingress in front will buffer the whole body and destroy the
        # streaming the endpoint exists to provide.
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID": request_id,
        },
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "busy": _RUN_LOCK.locked(),
            "model": agent.llm.model_name,
            "reasoning_effort": agent.MODEL_REASONING_EFFORT,
            "streaming": bool(agent.llm.streaming),
            "max_llm_steps": agent.MAX_LLM_STEPS,
            "history_max_rounds": agent.HISTORY_MAX_ROUNDS,
            "history_max_messages": agent.HISTORY_MAX_MESSAGES}


@app.get("/admin/conversations", dependencies=[Depends(require_external_access)])
async def conversations_for_date(
    date: str = Query(..., description="UTC calendar date in YYYY-MM-DD format"),
    detailed: bool = Query(False, description="Include the full agent trace"),
) -> dict:
    """List all conversations that wrote an event on the requested UTC date."""
    try:
        conversations = await asyncio.to_thread(
            agent.list_conversations_for_date, date, detailed
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    return {"date": date, "detailed": detailed, "conversations": conversations}


@app.get("/threads/{thread_id}/messages", dependencies=[Depends(require_external_access)])
async def thread_messages(
    thread_id: str,
    survey_id: str = Query(..., description="Only return this survey's events"),
    detailed: bool = False,
) -> dict:
    """Return a thread's UI conversation, or its full agent trace when requested."""
    return {"thread_id": thread_id, "detailed": detailed,
            "messages": await asyncio.to_thread(
                agent.load_conversation_events, thread_id, survey_id, detailed
            )}


@app.post("/threads/{thread_id}/telemetry", dependencies=[Depends(require_external_access)])
async def append_client_telemetry(
    thread_id: str,
    survey_id: str = Query(..., description="Survey belonging to the thread"),
    telemetry: ClientTelemetryRequest = ...,
) -> dict[str, Any]:
    """Append browser/caller timing without accepting conversation content."""
    if _request_id(telemetry.request_id) != telemetry.request_id:
        raise HTTPException(status_code=400, detail="request_id is invalid")
    payload = telemetry.model_dump()
    payload.update({"type": "client_timing", "thread_id": thread_id, "survey_id": survey_id})
    await asyncio.to_thread(
        agent.append_conversation_event,
        thread_id, survey_id, telemetry.turn, "client_timing",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    return {"stored": True, "request_id": telemetry.request_id, "thread_id": thread_id}


@app.delete("/threads/{thread_id}", dependencies=[Depends(require_external_access)])
async def drop_thread(thread_id: str) -> dict:
    """Release a conversation's memory.

    Worth calling. The checkpointer snapshots the whole state every superstep, so retention on
    one thread grows quadratically with rounds -- measured ~24 MB at 10 rounds, ~92 MB at 20,
    with a 30 KB inventory in message 0. A long-lived server that never drops threads will grow
    without bound.
    """
    try:
        CHECKPOINTER.delete_thread(thread_id)
        return {"deleted": thread_id}
    except Exception as exc:  # noqa: BLE001
        return {"deleted": None, "error": f"{type(exc).__name__}: {exc}"}
