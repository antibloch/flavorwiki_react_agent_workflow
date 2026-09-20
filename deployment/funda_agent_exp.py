import argparse
import hashlib
import os
import json
import math
import numbers
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Annotated, Literal, NamedTuple, NotRequired, Sequence, TypedDict

import requests
from pydantic import BaseModel, Field, field_validator
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from sqlalchemy import create_engine, text


from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool


from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI

import dotenv

import numpy as np

from clustering import DEFAULT_K, DEFAULT_SEED, segment_profiles
from plsr import DEFAULT_COMPONENTS, aggregate_by_unit, max_kfold_folds, plsr_analysis

# The isolated attribute-to-KPI workflow. The import goes ONE WAY on purpose: the workflow and its
# instruction module import nothing from here, so the main prompt, the main tool descriptions and
# the main conversation have no path into an isolated run.
# Model-facing text has exactly two sources: agent instructions and tool prompts.
from agent_instructions import (
    INVENTORY_PREAMBLE,
    PERSONA_RULES,
    REPEATED_SQL_WARNING,
    STEP_BUDGET_NOTICE,
    SYSTEM_PROMPT_TEXT,
    scoped_query,
    zero_row_streak_warning,
)
from tool_prompts import (
    ANALYZE_PLSR_DESCRIPTION,
    CLUSTER_RATING_PROFILES_DESCRIPTION,
    CLUSTER_RESULT_HEAD,
    PLSR_RESULT_HEAD,
    cluster_bad_k,
    cluster_bad_question_id,
    cluster_mixed_scales,
    cluster_multi_product,
    cluster_no_data,
    cluster_summary_measure,
    cluster_too_few_questions,
    cluster_unsupported_type,
    GENERATE_WORD_CLOUD_DESCRIPTION,
    GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION,
    NL2SQL_TOOL_DESCRIPTION,
    NON_SELECT_RESULT,
    PACKET_SCOPE_CHECK_FAILED,
    PACKET_SCOPE_UNAVAILABLE,
    REJECT_BATCH,
    REJECT_NOT_SELECT,
    RUN_SURVEY_STATS_DESCRIPTION,
    SCOPE_WARNING,
    SEMANTIC_FILTER_REASON,
    STATS_TIMEOUT,
    UNTRUSTED_WORD_CLOUD_BLOCK,
    WORD_CLOUD_SCOPE_UNAVAILABLE,
    ZERO_ROW_HEAD,
    ZERO_ROW_MSG,
    analysis_packet_preamble,
    multi_statement_error,
    packet_bad_survey_id,
    packet_not_available,
    packet_survey_out_of_scope,
    packet_unknown_survey,
    scope_repair_note,
    sql_failure,
    stats_bad_question_id,
    stats_bad_reference_id,
    stats_http_error,
    stats_missing_reference,
    stats_request_failed,
    tool_error,
    unknown_tool,
    wide_result_note,
    word_cloud_bad_question_id,
    word_cloud_question_unavailable,
    word_cloud_ready,
)

# ========== Runtime configuration ==========
dotenv.load_dotenv()

# Default CLI scope.
CLIENT_ID = "c0b3b212-bc35-45ef-b69d-8257d3735a90"
SURVEY_ID = "39af3240-42a8-4e35-8c7d-c61703d5ce3f"
ORG_ID = "46671273-0b12-49a5-b9de-60f81d192818"
DEFAULT_PROMPT = (
    "What are the products tested in this survey? "
    "Provide comparison of aroma of these products"
)

# Service configuration. Deployment credentials must come from .env/the process environment;
# there are deliberately no source-code fallbacks in this deployable copy.
def _required_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    raise RuntimeError(f"Missing required environment variable: {' or '.join(names)}")


OPENAI_API_KEY = _required_env("OPENAI_API_KEY")
DATABASE_URI = _required_env("DATABASE_URL", "DATABASE_URI")
CHARTS_API_BASE_URL = os.getenv(
    "CHARTS_API_BASE_URL", "https://charts-api.gpisurveys.com"
)
CHARTS_STATS_EXTERNAL_ACCESS_SECRET = _required_env(
    "CHARTS_STATS_EXTERNAL_ACCESS_SECRET"
)
# MODEL_NAME is canonical. STRONG_MODEL_NAME remains a one-cycle migration fallback.
MODEL_NAME = os.getenv("MODEL_NAME", os.getenv("STRONG_MODEL_NAME", "gpt-5.5"))
MODEL_TEMPERATURE = 0.0
# Agent and tool limits.
MAX_LLM_STEPS = 12  # hard bound on LLM turns; unproductive tool loops end here
STATEMENT_TIMEOUT_MS = 20000  # server-side kill switch for runaway queries
HISTORY_MAX_ROUNDS = int(os.getenv("HISTORY_MAX_ROUNDS", "5"))
HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "47"))
# CLI transcript limits. These clip what is PRINTED only -- the model still receives every
# character. 0 disables clipping for that block.
LOG_TOOL_CALL_CHARS = int(os.getenv("LOG_TOOL_CALL_CHARS", "500"))
LOG_TOOL_OUTPUT_CHARS = int(os.getenv("LOG_TOOL_OUTPUT_CHARS", "1000"))
CONVERSATION_DB = Path(os.getenv("CONVERSATION_DB_PATH", "conversation.sqlite3"))
PROGRESS_DELTA_MAX_CHARS = 2000
PROGRESS_DELTA_MAX_BULLETS = 12
INVENTORY_MAX_CHARS = int(os.getenv("INVENTORY_MAX_CHARS", "120000"))
INVENTORY_CACHE_ACTIVE_TTL_S = int(os.getenv("INVENTORY_CACHE_ACTIVE_TTL_S", "300"))
INVENTORY_CACHE_CLOSED_TTL_S = int(os.getenv("INVENTORY_CACHE_CLOSED_TTL_S", "86400"))
INVENTORY_CACHE_BENCHMARK_TTL_S = int(
    os.getenv("INVENTORY_CACHE_BENCHMARK_TTL_S", "86400")
)
INVENTORY_CACHE_MAX_ENTRIES = int(os.getenv("INVENTORY_CACHE_MAX_ENTRIES", "128"))
MAX_PARALLEL_SQL_CALLS = max(1, int(os.getenv("MAX_PARALLEL_SQL_CALLS", "6")))
MAX_PARALLEL_TOOL_CALLS = max(
    1,
    int(os.getenv("MAX_PARALLEL_TOOL_CALLS", str(MAX_PARALLEL_SQL_CALLS))),
)
DB_CONNECT_TIMEOUT_S = max(1, int(os.getenv("DB_CONNECT_TIMEOUT_S", "10")))
_SCOPE_REPAIR_MAX_EDITS = 4
_CHARTS_TIMEOUT_S = 30


def _conversation_db() -> sqlite3.Connection:
    CONVERSATION_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CONVERSATION_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            survey_id TEXT NOT NULL,
            turn INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_events_thread_survey "
        "ON conversation_events(thread_id, survey_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_events_created_at "
        "ON conversation_events(created_at, id)"
    )
    conn.commit()
    return conn


def begin_conversation_turn(thread_id: str, survey_id: str) -> int:
    """Reserve the next durable-log turn for one thread and survey."""
    conn = _conversation_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        (turn,) = conn.execute(
            "SELECT COALESCE(MAX(turn), 0) + 1 FROM conversation_events "
            "WHERE thread_id = ? AND survey_id = ?",
            (thread_id, survey_id),
        ).fetchone()
        conn.commit()
        return int(turn)
    finally:
        conn.close()


def append_conversation_event(
    thread_id: str,
    survey_id: str,
    turn: int,
    kind: str,
    content: str,
    name: str | None = None,
) -> None:
    """Persist one event immediately, timestamping it at append time."""
    conn = _conversation_db()
    try:
        conn.execute(
            "INSERT INTO conversation_events "
            "(thread_id, survey_id, turn, kind, name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, survey_id, turn, kind, name, content,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def load_conversation_events(
    thread_id: str, survey_id: str, detailed: bool = False
) -> list[dict[str, Any]]:
    """Read only events belonging to the requested thread and survey."""
    kinds = (
        "user", "llm", "tool_call", "tool_output", "final",
        "round_timing", "client_timing",
    ) if detailed else (
        "user", "final"
    )
    conn = _conversation_db()
    try:
        rows = conn.execute(
            "SELECT survey_id, turn, kind, name, content, created_at FROM conversation_events "
            f"WHERE thread_id = ? AND survey_id = ? AND kind IN "
            f"({','.join('?' * len(kinds))}) ORDER BY id",
            (thread_id, survey_id, *kinds),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_conversations_for_date(
    day_text: str, detailed: bool = False
) -> list[dict[str, Any]]:
    """Return all logged conversations having an event on one UTC calendar date."""
    day = date_type.fromisoformat(day_text)
    start = datetime.combine(day, datetime.min.time(), timezone.utc).isoformat()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc).isoformat()
    kinds = (
        "user", "llm", "tool_call", "tool_output", "final",
        "round_timing", "client_timing",
    ) if detailed else (
        "user", "final"
    )
    conn = _conversation_db()
    try:
        rows = conn.execute(
            "SELECT thread_id, survey_id, turn, kind, name, content, created_at "
            "FROM conversation_events WHERE created_at >= ? AND created_at < ? "
            "ORDER BY id",
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    conversations: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["thread_id"], row["survey_id"])
        conversation = conversations.setdefault(key, {
            "thread_id": row["thread_id"],
            "survey_id": row["survey_id"],
            "first_event_at": row["created_at"],
            "last_event_at": row["created_at"],
            "event_count": 0,
            "turn_count": 0,
            "events": [],
        })
        conversation["last_event_at"] = row["created_at"]
        conversation["event_count"] += 1
        conversation["turn_count"] = max(conversation["turn_count"], row["turn"])
        if row["kind"] in kinds:
            conversation["events"].append({
                "survey_id": row["survey_id"],
                "turn": row["turn"],
                "kind": row["kind"],
                "name": row["name"],
                "content": row["content"],
                "created_at": row["created_at"],
            })
    return list(conversations.values())

# LLM request tuning. The strong-model and shared variables remain fallbacks for one
# deployment migration cycle; MODEL_REASONING_EFFORT is the canonical setting.
def _reasoning_effort(
    env_name: str, default: str, legacy_env_name: str | None = None
) -> str:
    legacy = os.getenv(legacy_env_name) if legacy_env_name else None
    shared = os.getenv("OPENAI_REASONING_EFFORT")
    fallback = legacy if legacy is not None else shared if shared is not None else default
    value = os.getenv(env_name, fallback).strip().lower()
    return "" if value in {"off", "default"} else value


def _llm_tuning(reasoning_effort: str) -> dict[str, Any]:
    tuning: dict[str, Any] = {}
    if reasoning_effort:
        tuning["reasoning_effort"] = reasoning_effort
        if reasoning_effort != "none":
            tuning["use_responses_api"] = True
    verbosity = os.getenv("OPENAI_VERBOSITY", "")
    if verbosity:
        tuning["verbosity"] = verbosity
    return tuning


MODEL_REASONING_EFFORT = _reasoning_effort(
    "MODEL_REASONING_EFFORT", "low", "STRONG_REASONING_EFFORT"
)
_LLM_TUNING = _llm_tuning(MODEL_REASONING_EFFORT)

#============ Building Tools for the Agent ========================

_ENGINE = None
_STATS_TIMING_LOG_LOCK = threading.Lock()


def _sql_engine():
    """Return the process-wide engine, building its pool on first use."""
    global _ENGINE
    if not DATABASE_URI:
        raise RuntimeError("DATABASE_URI is not configured.")
    if _ENGINE is None:
        _ENGINE = create_engine(
            DATABASE_URI,
            connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_S},
        )
    return _ENGINE



# Rejects a query that decides MEANING inside SQL. Why that matters, and what the model is
# told instead, is tool_prompts.SEMANTIC_FILTER_REASON -- this is only the detector.
#
# The operator must sit directly on the column (optional cast, closing parens from
# LOWER(...)/TRIM(...), optional NOT) so this cannot fire on an unrelated ILIKE elsewhere in
# the statement -- e.g. `SELECT q.prompt, ... WHERE p.name ILIKE ...` is untouched. Equality
# is deliberately allowed: `prompt = '<exact text>'` is how step 2 pins a question the model
# already read in step 1, which is the behaviour we want, not a shortcut around it.
_SEMANTIC_FILTER_RE = re.compile(
    r'\b(?:prompt|promptHtml|label|labelHtml|internal_name|optionDefinition)"?\s*'
    r'(?:::\s*\w+\s*)?\)*\s*(?:NOT\s+)?(?:I?LIKE\b|SIMILAR\s+TO\b|~~?\*?)',
    re.IGNORECASE,
)


def _reject_reason(sql: str) -> str | None:
    """Return why `sql` is not exactly one SELECT/WITH statement, else None.

    psycopg2 happily executes `SELECT a; SELECT b` and returns only the LAST
    result set, so a batched query silently discards earlier results. Scan with
    string literals, quoted identifiers, dollar-quotes and comments blanked out,
    so only structural semicolons are seen.

    `ident` is the same scan with quoted identifiers kept (unquoted) instead of blanked,
    because the semantic-filter check below needs to see the column name: blanking turns
    `"promptHtml" SIMILAR TO ...` into `  SIMILAR TO ...` and the check would miss it.
    It is used for that check only -- the statement-count and leading-keyword checks stay
    on `body`, where an identifier containing a semicolon still cannot be mistaken for a
    batch separator.
    """
    out, ident, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":  # string literal, '' and \' escapes
            i += 1
            while i < n:
                if sql[i] == "\\":
                    i += 2
                elif sql[i] == "'":
                    if sql[i : i + 2] == "''":
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            out.append(" ")
            ident.append(" ")
        elif ch == '"':  # quoted identifier
            start = i + 1
            i += 1
            while i < n and sql[i] != '"':
                i += 1
            ident.append(sql[start:i])
            i += 1
            out.append(" ")
        elif ch == "$" and (m := re.match(r"\$\w*\$", sql[i:])):  # dollar-quote
            close = sql.find(m.group(0), i + len(m.group(0)))
            i = n if close == -1 else close + len(m.group(0))
            out.append(" ")
            ident.append(" ")
        elif sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            out.append(" ")
            ident.append(" ")
        elif sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            i = n if close == -1 else close + 2
            out.append(" ")
            ident.append(" ")
        else:
            out.append(ch)
            ident.append(ch)
            i += 1

    body = "".join(out).strip()
    while body.endswith(";"):
        body = body[:-1].strip()

    if ";" in body:
        return REJECT_BATCH
    head = body.lstrip("( \t\r\n").split(None, 1)
    if not head or head[0].upper() not in {"SELECT", "WITH"}:
        return REJECT_NOT_SELECT
    if _SEMANTIC_FILTER_RE.search("".join(ident)):
        return SEMANTIC_FILTER_REASON
    return None


#---------- (1) the model must never have to transcribe a scope id ----------
# Measured over this repo's run artifacts: the client_id appeared 82 times correctly and 7
# times corrupted (~8%), and the corruption is systematic rather than random -- the true id
# contains "e5e5" and the model collapses it to "e5", a repeated-substring elision. Exact
# copying of high-entropy strings is a known weak axis, so the harness stops asking for it:
# the ids can be written as bound placeholders, and any near-miss literal is repaired here.
_SCOPE: dict[str, str] = {}  # populated by main(); empty means "repair disabled"

# Deliberately looser than a strict UUID pattern: a CORRUPTED id is what has to be caught,
# and the observed corruptions are 33-34 chars, so a 36-char-only pattern would miss them.
_UUIDISH_RE = re.compile(r"[0-9a-fA-F]{4,12}(?:-[0-9a-fA-F]{2,16}){2,5}")
_UUID_LITERAL_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

# Names in this namespace are executor-owned. A model query may not bind or refer to them.
_SCOPE_PARAM_PREFIX = "__fw_scope_"
_AUTHORIZED_SCOPE_SQL = f"""
WITH initial AS (
  SELECT s.id AS survey_id, s.organization_id, a.client_id,
         COALESCE(NULLIF(BTRIM(s.benchmark_category_label), ''),
                  NULLIF(BTRIM(sn.category_label_snapshot), '')) AS benchmark_category
  FROM survey s
  LEFT JOIN organization o ON o.id = s.organization_id
  LEFT JOIN account a ON a.id = o.account_id
  LEFT JOIN survey_nomenclature sn ON sn.survey_id = s.id
  WHERE s.id = CAST(:{_SCOPE_PARAM_PREFIX}initial_survey_id AS uuid)
)
SELECT i.survey_id::text AS survey_id,
       i.organization_id::text AS organization_id,
       i.client_id::text AS client_id,
       ARRAY(
         SELECT candidate.id::text
         FROM survey candidate
         WHERE candidate.id = i.survey_id
            OR (
              i.client_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM organization candidate_org
                JOIN account candidate_account ON candidate_account.id = candidate_org.account_id
                WHERE candidate_org.id = candidate.organization_id
                  AND candidate_account.client_id = i.client_id
              )
            )
            OR (
              i.client_id IS NOT NULL
              AND i.benchmark_category IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM benchmark_registry br
                WHERE br.is_active
                  AND br.survey_id = candidate.id
                  AND LOWER(BTRIM(br.category_label)) = LOWER(i.benchmark_category)
              )
            )
         ORDER BY candidate.id
       ) AS authorized_survey_ids
FROM initial i
"""

_SURVEY_LITERAL_LOOKUP_SQL = f"""
SELECT id::text AS survey_id
FROM survey
WHERE id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}candidate_ids AS uuid[]))
"""

_QUESTION_SCOPE_SQL = f"""
SELECT q.id::text AS question_id, q."surveyId"::text AS survey_id
FROM question q
WHERE q.id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}question_ids AS uuid[]))
"""


class AuthorizedScope(TypedDict):
    survey_id: str
    organization_id: str | None
    client_id: str | None
    authorized_survey_ids: tuple[str, ...]


class SurveyBoundaryError(RuntimeError):
    """A statement cannot be proven to stay inside the run's survey boundary."""


def _resolve_authorized_scope(conn: Any) -> AuthorizedScope:
    """Derive the boundary from the initial survey, never from caller-supplied tenant ids."""
    initial_survey_id = _SCOPE.get("survey_id")
    if not initial_survey_id or not _UUID_LITERAL_RE.fullmatch(initial_survey_id):
        raise SurveyBoundaryError("the initial survey scope is not a valid UUID")
    row = conn.execute(
        text(_AUTHORIZED_SCOPE_SQL),
        {
            f"{_SCOPE_PARAM_PREFIX}initial_survey_id": initial_survey_id,
        },
    ).mappings().first()
    if row is None:
        raise SurveyBoundaryError("the initial survey does not exist")
    authorized_ids = tuple(str(value).lower() for value in (row["authorized_survey_ids"] or ()))
    if initial_survey_id.lower() not in authorized_ids:
        raise SurveyBoundaryError("the initial survey was not present in its authorized set")
    return {
        "survey_id": str(row["survey_id"]),
        "organization_id": str(row["organization_id"]) if row["organization_id"] else None,
        "client_id": str(row["client_id"]) if row["client_id"] else None,
        "authorized_survey_ids": authorized_ids,
    }


def _scope_params(scope: AuthorizedScope) -> dict[str, Any]:
    """Publish trusted public binds plus the private row-filter bind."""
    params: dict[str, Any] = {
        "survey_id": scope["survey_id"],
        f"{_SCOPE_PARAM_PREFIX}authorized_survey_ids": list(scope["authorized_survey_ids"]),
    }
    if scope["organization_id"] is not None:
        params["organization_id"] = scope["organization_id"]
    if scope["client_id"] is not None:
        params["client_id"] = scope["client_id"]
    return params


def _direct_scope(table: str, column: str) -> str:
    return (
        f'SELECT base.* FROM public.{table} base '
        f'WHERE base.{column} = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[]))'
    )


def _through_question(table: str, question_column: str = "question_id") -> str:
    return (
        f'SELECT base.* FROM public.{table} base WHERE EXISTS ('
        f'SELECT 1 FROM public.question scope_q WHERE scope_q.id = base.{question_column} '
        f'AND scope_q."surveyId" = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    )


# Every public table exposed to model SQL must have a deterministic route to survey ownership.
# Unknown tables fail closed. Trusted wrappers are inserted after parsing and are not themselves
# revisited by the transformer.
_TENANT_TABLE_SCOPES: dict[str, str] = {
    "survey": _direct_scope("survey", "id"),
    "question": _direct_scope("question", '"surveyId"'),
    "product": _direct_scope("product", '"surveyId"'),
    "enrollment": _direct_scope("enrollment", "survey_id"),
    "question_screen": _direct_scope("question_screen", '"surveyId"'),
    "question_section": _direct_scope("question_section", '"surveyId"'),
    "logic": _direct_scope("logic", '"surveyId"'),
    "survey_nomenclature": _direct_scope("survey_nomenclature", "survey_id"),
    "benchmark_registry": _direct_scope("benchmark_registry", "survey_id"),
    "survey_panel": _direct_scope("survey_panel", "survey_id"),
    "survey_panel_code": _direct_scope("survey_panel_code", "survey_id"),
    "survey_panel_stats": _direct_scope("survey_panel_stats", "survey_id"),
    "piping_references": _direct_scope("piping_references", "survey_id"),
    "product_display_order": _direct_scope("product_display_order", '"surveyId"'),
    "charts_reports": _direct_scope("charts_reports", "survey_id"),
    "aggregations": _direct_scope("aggregations", "survey_id"),
    "question_option": _through_question("question_option"),
    "question_group": _through_question("question_group"),
    "question_pair": _through_question("question_pair"),
    "question_set": _through_question("question_set"),
    "answer": (
        'SELECT base.* FROM public.answer base WHERE EXISTS ('
        'SELECT 1 FROM public.enrollment scope_e JOIN public.question scope_q '
        'ON scope_q.id = base.question_id WHERE scope_e.id = base.enrollment_id '
        f'AND scope_e.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])) '
        f'AND scope_q."surveyId" = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "answered_question_options": (
        'SELECT base.* FROM public.answered_question_options base WHERE EXISTS ('
        'SELECT 1 FROM public.answer scope_a JOIN public.enrollment scope_e '
        'ON scope_e.id = scope_a.enrollment_id JOIN public.question scope_q '
        'ON scope_q.id = scope_a.question_id WHERE scope_a.id = base.answer_id '
        f'AND scope_e.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])) '
        f'AND scope_q."surveyId" = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "product_display_order_item": (
        'SELECT base.* FROM public.product_display_order_item base WHERE EXISTS ('
        'SELECT 1 FROM public.product_display_order scope_o '
        'WHERE scope_o.id = base."displayOrderId" '
        f'AND scope_o."surveyId" = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "charts_tabs": (
        'SELECT base.* FROM public.charts_tabs base WHERE EXISTS ('
        'SELECT 1 FROM public.charts_reports scope_r WHERE scope_r.id = base.report_id '
        f'AND scope_r.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "charts": (
        'SELECT base.* FROM public.charts base WHERE EXISTS ('
        'SELECT 1 FROM public.question scope_q WHERE scope_q.id = base.question_id '
        f'AND scope_q."surveyId" = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[]))) '
        'OR EXISTS (SELECT 1 FROM public.charts_tabs scope_t '
        'JOIN public.charts_reports scope_r ON scope_r.id = scope_t.report_id '
        'WHERE scope_t.id = base.tab_id '
        f'AND scope_r.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "chart_report_filter": (
        'SELECT base.* FROM public.chart_report_filter base WHERE EXISTS ('
        'SELECT 1 FROM public.charts_reports scope_r WHERE scope_r.id = base.chart_report_id '
        f'AND scope_r.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "aggregation_report_tabs": (
        'SELECT base.* FROM public.aggregation_report_tabs base WHERE EXISTS ('
        'SELECT 1 FROM public.aggregations scope_a WHERE scope_a.id = base.aggregation_id '
        f'AND scope_a.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "organization": (
        'SELECT base.* FROM public.organization base WHERE EXISTS ('
        'SELECT 1 FROM public.survey scope_s '
        f'WHERE scope_s.organization_id = base.id AND scope_s.id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "account": (
        'SELECT base.* FROM public.account base WHERE EXISTS ('
        'SELECT 1 FROM public.organization scope_o JOIN public.survey scope_s '
        'ON scope_s.organization_id = scope_o.id WHERE scope_o.account_id = base.id '
        f'AND scope_s.id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "client": (
        'SELECT base.* FROM public.client base WHERE EXISTS ('
        'SELECT 1 FROM public.account scope_a JOIN public.organization scope_o '
        'ON scope_o.account_id = scope_a.id JOIN public.survey scope_s '
        'ON scope_s.organization_id = scope_o.id WHERE scope_a.client_id = base.id '
        f'AND scope_s.id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "panel": (
        'SELECT base.* FROM public.panel base WHERE EXISTS ('
        'SELECT 1 FROM public.survey_panel scope_sp WHERE scope_sp.panel_id = base.id '
        f'AND scope_sp.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "panel_panelist": (
        'SELECT base.* FROM public.panel_panelist base WHERE EXISTS ('
        'SELECT 1 FROM public.survey_panel scope_sp WHERE scope_sp.panel_id = base.panel_id '
        f'AND scope_sp.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "panelist": (
        'SELECT base.* FROM public.panelist base WHERE EXISTS ('
        'SELECT 1 FROM public.enrollment scope_e WHERE scope_e.panelist_id = base.id '
        f'AND scope_e.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
    "user": (
        'SELECT base.* FROM public."user" base WHERE EXISTS ('
        'SELECT 1 FROM public.enrollment scope_e WHERE scope_e.user_id = base.id '
        f'AND scope_e.survey_id = ANY(CAST(:{_SCOPE_PARAM_PREFIX}authorized_survey_ids AS uuid[])))'
    ),
}

_SAFE_INFORMATION_SCHEMA_TABLES = frozenset({"columns", "tables"})
_DANGEROUS_SQL_FUNCTIONS = frozenset({
    "dblink", "lo_export", "lo_import", "pg_ls_dir", "pg_read_binary_file",
    "pg_read_file", "query_to_xml",
})
# MEMBERSHIP RULE -- a name belongs here only if it is a PURE, IN-PROCESS, READ-ONLY computation
# over values already retrieved. Never add anything that reaches the filesystem, the network,
# large objects, server configuration, backend control, or the catalog: this set is the backstop
# for the gaps in _DANGEROUS_SQL_FUNCTIONS above, which is a denylist and therefore incomplete.
# `lo_get`, `pg_stat_file`, `set_config` and `pg_terminate_backend` are all absent from that
# denylist and are refused only because they are absent from here.
#
# The list is deliberately longer than what sqlglot currently routes through this check. Only
# functions parsed as `exp.Anonymous` are tested against it, and which functions those are is a
# property of the installed sqlglot version, not of Postgres -- measured on 30.16.0, 43 of 53
# common functions bypass this set entirely because they have dedicated AST nodes. Naming the
# safe ones anyway keeps a parser upgrade from silently starting to reject working queries.
_SAFE_ANONYMOUS_SQL_FUNCTIONS = frozenset({
    # text
    "ascii", "btrim", "char_length", "chr", "concat", "concat_ws", "format", "initcap",
    "left", "length", "lower", "lpad", "ltrim", "overlay", "position", "quote_ident",
    "quote_literal", "repeat", "replace", "reverse", "right", "rpad", "rtrim", "split_part",
    "starts_with", "string_agg", "strpos", "substr", "to_ascii", "translate", "upper",
    # regular expressions
    "regexp_count", "regexp_instr", "regexp_like", "regexp_matches", "regexp_replace",
    "regexp_split_to_array", "regexp_split_to_table", "regexp_substr", "similar_to_escape",
    # arrays and set-returning helpers
    "array_agg", "array_append", "array_cat", "array_dims", "array_length", "array_lower",
    "array_ndims", "array_position", "array_positions", "array_prepend", "array_remove",
    "array_replace", "array_to_string", "array_upper", "cardinality", "generate_series",
    "generate_subscripts", "string_to_array", "unnest",
    # json / jsonb
    "json_agg", "json_array_elements", "json_array_elements_text", "json_array_length",
    "json_build_array", "json_build_object", "json_each", "json_each_text", "json_extract_path",
    "json_extract_path_text", "json_object", "json_object_agg", "json_object_keys", "json_typeof",
    "jsonb_agg", "jsonb_array_elements", "jsonb_array_elements_text", "jsonb_array_length",
    "jsonb_build_array", "jsonb_build_object", "jsonb_each", "jsonb_each_text",
    "jsonb_extract_path", "jsonb_extract_path_text", "jsonb_insert", "jsonb_object",
    "jsonb_object_agg", "jsonb_object_keys", "jsonb_path_exists", "jsonb_path_query",
    "jsonb_path_query_array", "jsonb_path_query_first", "jsonb_pretty", "jsonb_set",
    "jsonb_strip_nulls", "jsonb_typeof", "row_to_json", "to_json", "to_jsonb",
    # date and time
    "age", "clock_timestamp", "date_bin", "date_part", "date_trunc", "extract", "isfinite",
    "justify_days", "justify_hours", "justify_interval", "make_date", "make_interval",
    "make_time", "make_timestamp", "now", "statement_timestamp", "to_char", "to_date",
    "to_number", "to_timestamp",
    # numeric and statistical
    "abs", "bool_and", "bool_or", "cbrt", "ceil", "ceiling", "corr", "covar_pop", "covar_samp",
    "div", "exp", "floor", "greatest", "least", "ln", "log", "log10", "mod", "mode",
    "numeric_scale", "percentile_cont", "percentile_disc", "power", "regr_avgx", "regr_avgy",
    "regr_count", "regr_intercept", "regr_r2", "regr_slope", "regr_sxx", "regr_sxy", "regr_syy",
    "round", "sign", "sqrt", "stddev", "stddev_pop", "stddev_samp", "trunc", "var_pop",
    "var_samp", "variance", "width_bucket",
    # window
    "cume_dist", "dense_rank", "first_value", "lag", "last_value", "lead", "nth_value",
    "ntile", "percent_rank", "rank", "row_number",
    # conditional and introspection of values already fetched
    "coalesce", "nullif", "num_nonnulls", "num_nulls", "pg_typeof",
})


def _repair_setop_order_by(statement: exp.Expression) -> exp.Expression:
    """Move an expression ORDER BY off a set operation and onto a wrapping SELECT.

    Postgres accepts only output column names in the ORDER BY of a UNION/INTERSECT/EXCEPT.
    `ORDER BY CASE WHEN ... END` is legal on a plain SELECT and rejected here, and it is the
    shape the model reaches for whenever it wants a rollup row sorted ahead of its detail rows
    -- the single most common way it loses a turn to a dialect error. The rewrite is the server's
    own HINT ("move the UNION into a FROM clause") and fires only on a statement Postgres would
    reject outright, so nothing that runs today is touched. A prompt rule for this measured 0
    for 1; doing it here is deterministic and costs no turn at all.
    """
    node = statement.this if isinstance(statement, exp.With) else statement
    if not isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
        return statement
    order = node.args.get("order")
    if not order or all(
        isinstance(ordered.this, (exp.Column, exp.Literal)) for ordered in order.expressions
    ):
        return statement
    node.set("order", None)
    wrapped = parse(
        f"SELECT * FROM ({statement.sql(dialect='postgres')}) AS __setop", read="postgres"
    )[0]
    wrapped.set("order", order)
    print("[sql] repaired ORDER BY on a set operation (wrapped in a subquery)")
    return wrapped


def _guard_and_scope_sql(sql: str, conn: Any, scope: AuthorizedScope) -> str:
    """Parse one query, reject unsafe sources, and wrap every physical table in a scope view."""
    try:
        statements = parse(sql, read="postgres")
    except ParseError as exc:
        raise SurveyBoundaryError(f"SQL could not be parsed safely: {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise SurveyBoundaryError("exactly one SQL statement is required")
    # Before any inspection, so the guards below and the executor see the same statement.
    statement = _repair_setop_order_by(statements[0])

    for placeholder in statement.find_all(exp.Placeholder):
        if str(placeholder.name).lower().startswith(_SCOPE_PARAM_PREFIX):
            raise SurveyBoundaryError("the query uses a reserved scope parameter")
    for dot in statement.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            raise SurveyBoundaryError("schema-qualified function calls are not permitted")
    for function in statement.find_all(exp.Func):
        function_name = (
            str(function.name).lower()
            if isinstance(function, exp.Anonymous)
            else str(function.sql_name()).lower()
        )
        if function_name in _DANGEROUS_SQL_FUNCTIONS:
            raise SurveyBoundaryError(f"function {function_name} is not permitted")
        if isinstance(function, exp.Anonymous) \
                and function_name not in _SAFE_ANONYMOUS_SQL_FUNCTIONS:
            raise SurveyBoundaryError(f"function {function_name} is not an approved SQL function")

    def identifier_key(identifier: exp.Identifier) -> str:
        value = str(identifier.this)
        return value if identifier.args.get("quoted") else value.lower()

    cte_names = {
        identifier_key(cte.args["alias"].this)
        for cte in statement.find_all(exp.CTE)
    }
    physical_tables = list(statement.find_all(exp.Table))
    for table in physical_tables:
        table_name = table.name.lower()
        schema_name = table.db.lower()
        if not schema_name and identifier_key(table.this) in cte_names:
            continue
        if table.catalog:
            raise SurveyBoundaryError("cross-database table references are not permitted")

        alias = table.args.get("alias") or exp.TableAlias(
            this=exp.to_identifier(table.name, quoted=bool(table.this.args.get("quoted")))
        )
        if schema_name == "information_schema":
            if table_name not in _SAFE_INFORMATION_SCHEMA_TABLES:
                raise SurveyBoundaryError(
                    f"information_schema.{table_name} is not an approved metadata source"
                )
            wrapper_sql = (
                f"SELECT base.* FROM information_schema.{table_name} base "
                "WHERE base.table_schema = 'public'"
            )
        else:
            if schema_name not in {"", "public"}:
                raise SurveyBoundaryError(f"schema {schema_name} is not permitted")
            wrapper_sql = _TENANT_TABLE_SCOPES.get(table_name, "")
            if not wrapper_sql:
                raise SurveyBoundaryError(
                    f"table {table_name} has no verified survey-ownership route"
                )
        wrapper = exp.Subquery(this=parse(wrapper_sql, read="postgres")[0], alias=alias.copy())
        table.replace(wrapper)

    literal_ids = {value.lower() for value in _UUID_LITERAL_RE.findall(sql)}
    if literal_ids:
        rows = conn.execute(
            text(_SURVEY_LITERAL_LOOKUP_SQL),
            {f"{_SCOPE_PARAM_PREFIX}candidate_ids": sorted(literal_ids)},
        ).mappings().all()
        survey_literals = {str(row["survey_id"]).lower() for row in rows}
        foreign = sorted(survey_literals.difference(scope["authorized_survey_ids"]))
        if foreign:
            raise SurveyBoundaryError(
                "survey id(s) outside the initial survey's client boundary: " + ", ".join(foreign)
            )

    rendered = statement.sql(dialect="postgres")
    return re.sub(r"%\(([_A-Za-z][_A-Za-z0-9]*)\)s", r":\1", rendered)


def _lev(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, abandoned as soon as it provably exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _repair_scope_ids(sql: str) -> tuple[str, list[str]]:
    """Replace near-miss scope-id literals with the authoritative value.

    Returns the corrected SQL and a list of human-readable repair notes. Repairs are always
    disclosed to the model: silently running something other than what it wrote would make
    every later inference about the result unsound.
    """
    if not _SCOPE:
        return sql, []
    exact = set(_SCOPE.values())
    notes: list[str] = []

    def _fix(match: re.Match) -> str:
        token = match.group(0)
        if token in exact:
            return token
        best_name, best_val, best_d = None, None, _SCOPE_REPAIR_MAX_EDITS + 1
        for name, val in _SCOPE.items():
            d = _lev(token, val, _SCOPE_REPAIR_MAX_EDITS)
            if d < best_d:
                best_name, best_val, best_d = name, val, d
        if best_val is None:
            return token
        notes.append(f"{token} -> {best_val} ({best_name}, {best_d} char(s) off)")
        return best_val

    return _UUIDISH_RE.sub(_fix, sql), notes


#---------- (2)+(3) dialect rejections ----------
# The hint table and failure-message assembly are in tool_prompts.py.
# and sql_failure(): every one of those patterns was reproduced against this database, and
# each names the fix rather than sending the model back to the schema.


def _scope_note(sql: str) -> str:
    """Legacy compatibility hook; row scoping is now mandatory in the SQL executor."""
    return ""


# Deliberately narrow. The bounds describe a result a reader can navigate as a hierarchy, not
# every wide table: above roughly a dozen entities the nested form stops shortening the answer
# and starts burying it, and a result with no text column has no entity label to head a block.
_WIDE_RESULT_MAX_ENTITIES = 12
_WIDE_RESULT_MIN_MEASURES = 7


def _wide_result_hint(rows: Sequence[dict[str, Any]]) -> str:
    """One line marking a result as parent/child, carried by the data instead of the prompt.

    Booleans are not measures and neither is a column that never holds a number, so an id or
    label column cannot inflate the width. Every row is scanned rather than just the first,
    because a NULL in row 0 would otherwise hide a real measure column.
    """
    if not _WIDE_RESULT_MAX_ENTITIES >= len(rows) >= 2:
        return ""
    measures = {
        key
        for row in rows
        for key, value in row.items()
        if isinstance(value, numbers.Number) and not isinstance(value, bool)
    }
    labels = {key for row in rows for key in row if key not in measures}
    if len(measures) < _WIDE_RESULT_MIN_MEASURES or not labels:
        return ""
    return wide_result_note(len(rows), len(measures))

_COUNT_PEOPLE_RE = re.compile(
    r"count\s*\(\s*distinct\s+[a-z_.\"]*enrollment_id", re.IGNORECASE
)
# _UUID_RE is anchored for validation, so it cannot find ids inside a statement.
_SQL_UUID_LITERAL_RE = re.compile(
    r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", re.IGNORECASE
)


def _base_composition_hint(sql_query: str) -> str:
    """One line splitting a respondent base the model just counted into completed / in progress.

    The catalog carries `respondents` and `completed` per question, but a base the model derived
    from its OWN query never passed through the inventory, so an inventory-side rule cannot reach
    it. Measured over repeated runs on the default survey: turn 1 reported a bare `8 people` every
    time and produced the 5-completed/3-in-progress split only when the user challenged it, and
    then re-queried for a number already sitting in its context. Three prompt placements failed
    -- reporting rules, the same text with the column beside it, then the catalog field block --
    so the fact is carried beside the rows it describes instead, where the base sentence is written.

    Fires only when the statement counts distinct enrollments AND names a question whose base is
    genuinely mixed, so a query that is not reporting a respondent base pays nothing. Reads the
    in-process packet cache only: never a second round trip, and silent when it is cold.
    """
    if not _COUNT_PEOPLE_RE.search(sql_query):
        return ""
    cached = _inventory_cache_get(_SCOPE.get("survey_id") or "")
    if cached is None:
        return ""
    wanted = {m.group(1).lower() for m in _SQL_UUID_LITERAL_RE.finditer(sql_query)}
    if not wanted:
        return ""
    try:
        catalog = json.loads(cached[1]).get("catalog") or {}
        columns = catalog["columns"]
        qid_i, prompt_i = columns.index("qid"), columns.index("prompt")
        resp_i, done_i = columns.index("respondents"), columns.index("completed")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return ""
    notes = []
    for row in catalog.get("rows") or []:
        if str(row[qid_i]).lower() not in wanted:
            continue
        resp, done = row[resp_i], row[done_i]
        if not resp or resp == done:
            continue
        notes.append(
            f'"{str(row[prompt_i])[:60]}": {resp} respondents = {done} completed '
            f"+ {resp - done} still in progress"
        )
    if not notes:
        return ""
    return "[base] " + "; ".join(notes) + ". State this split with that base.\n"


# The description the model reads -- the ledger contract, the structure-not-meaning rule,
# the dialect and scoping requirements -- is tool_prompts.NL2SQL_TOOL_DESCRIPTION.
# It is assigned to __doc__ below, before tool() wraps the function and captures it.
def nl2sql_tool(sql_query: str) -> str:
    # Repair mistyped scope ids before anything else looks at the SQL, so validation, the
    # executor sees the same corrected statement as local validation.
    sql_query, repairs = _repair_scope_ids(sql_query)
    repair_note = scope_repair_note(repairs) if repairs else ""
    if repairs:
        print(f"[sql] repaired scope id(s): {'; '.join(repairs)}")
    # Same prefix channel as the repair note, so every return path below carries it.
    repair_note += _scope_note(sql_query)

    reason = _reject_reason(sql_query)
    if reason:
        return multi_statement_error(reason)

    try:
        engine = _sql_engine()
        with engine.connect() as conn, conn.begin():
            # Postgres rejects any INSERT/UPDATE/DELETE/DDL in this transaction.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            scope = _resolve_authorized_scope(conn)
            guarded_sql = _guard_and_scope_sql(sql_query, conn, scope)
            result = conn.execute(text(guarded_sql), _scope_params(scope))

            if not result.returns_rows:
                return NON_SELECT_RESULT

            columns = list(result.keys())
            rows = [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
            if not rows:
                return repair_note + ZERO_ROW_MSG

            # default=str keeps Decimal/date/UUID serializable and compact.
            return (
                repair_note
                + _wide_result_hint(rows)
                + _base_composition_hint(sql_query)
                + json.dumps(rows, default=str, ensure_ascii=False)
            )
    except SurveyBoundaryError as exc:
        print(f"[sql] REFUSED by survey boundary: {exc}")
        if not str(exc).startswith("survey id(s) outside the initial survey's client boundary"):
            return repair_note + f"Query refused by survey boundary: {exc}."
        return (
            repair_note
            + f"Query refused by survey boundary: {exc}.\n"
            "TERMINAL: the requested cross-survey task cannot be served. Make no further tool "
            "call, do not fetch only the current side or substitute another survey, and answer "
            "in one or two sentences without repeating the UUID, adding a table, or mentioning "
            "the benchmark unless the user asked about it."
        )
    except Exception as exc:  # noqa: BLE001 - surfaced back to the LLM, not raised
        return repair_note + sql_failure(str(exc), sql_query)



nl2sql_tool.__doc__ = NL2SQL_TOOL_DESCRIPTION
nl2sql_tool = tool(nl2sql_tool)



#=========== Collecting All Tools ==========
#=========== Charts API stats tool ========================
# Ported from run_agent_langraph.py, which in turn mirrors
# flavor_ai/tools/fw_stats_tool.py::FlavorWikiStatsTool. Inputs, the Charts API call and
# the response formatting are kept identical to those originals.
#
# Question ids are UUID-validated and ownership-checked against the authoritative client boundary
# before any request is sent to the Charts API.

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_REQUIRES_REFERENCE = frozenset({"pearson", "spearman", "penalty"})
_PCA_TOOL_RESULT_PREFIX = "__FW_PCA_TOOL_RESULT_V1__"
_GPI_CHART_BLOCK_RE = re.compile(r"```gpi-chart\s*.*?```", re.IGNORECASE | re.DOTALL)
_MAX_CHART_POINTS = 300
_MODEL_CHART_TYPES = {
    "bar_chart", "column_chart", "stacked_bar_chart", "stacked_column_chart",
    "stacked_column_bar_chart", "line_chart", "pie_chart", "scatterplot", "heatmap",
    "dot_plot", "histogram", "test_line_chart", "intensity_curve", "dominance_over_time",
    "intensity_max", "intensity_time", "intensity_auc", "panelist_score_summary_chart",
    "penalty_line_chart", "penalty_scatterplot",
}
# Payload shape families, per chatbot-backend-chart-payload-contract.md: the scatter family
# carries numeric x/y points, a heatmap carries its cells in a top-level `data` array, and
# every other accepted type carries label/value points. Cells in the wrong container render
# as an empty chart, so the shape is checked here rather than left to the prompt alone.
_CHART_SCATTER_TYPES = {"scatterplot", "penalty_scatterplot"}
_CHART_MATRIX_TYPES = {"heatmap"}
_MAX_CHART_SERIES = 8
_MODEL_CHART_ALIASES = {
    "bar-chart": "bar_chart", "column-chart": "column_chart",
    "line-chart": "line_chart", "pie-chart": "pie_chart", "dot-plot": "dot_plot",
    "test-line-chart": "test_line_chart", "difference-test-line-chart": "test_line_chart",
    "difference_test_line_chart": "test_line_chart", "penalty-line-chart": "penalty_line_chart",
    "penalty-scatterplot": "penalty_scatterplot",
}


_SURVEY_OWNER_SQL = """
SELECT s.id::text AS survey_id, s.title
FROM survey s
WHERE s.id = CAST(:target_survey_id AS uuid)
"""


class ScopeLookupUnavailable(RuntimeError):
    """The ownership lookup could not run, so authorization could not be established."""


class SurveyOwner(TypedDict):
    survey_id: str
    title: str
    in_scope: bool


def _scope_published() -> bool:
    """True once main()/the API have published this run's envelope. See _SCOPE."""
    return bool(_SCOPE.get("survey_id"))


def _survey_owner(survey_id: str) -> SurveyOwner | None:
    """Resolve a packet target against the client derived from the run's initial survey."""
    try:
        with _sql_engine().connect() as conn:
            scope = _resolve_authorized_scope(conn)
            row = conn.execute(text(_SURVEY_OWNER_SQL), {
                "target_survey_id": survey_id,
            }).mappings().first()
    except Exception as exc:  # noqa: BLE001 - converted into fail-closed tool feedback
        raise ScopeLookupUnavailable(str(exc)) from exc
    if row is None:
        return None
    return {
        "survey_id": row["survey_id"],
        "title": row["title"],
        "in_scope": str(row["survey_id"]).lower() in scope["authorized_survey_ids"],
    }


def _authorize_question_ids(question_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (unknown, outside-boundary) ids using the same authoritative scope resolver."""
    normalized = [question_id.lower() for question_id in question_ids]
    try:
        with _sql_engine().connect() as conn:
            scope = _resolve_authorized_scope(conn)
            rows = conn.execute(
                text(_QUESTION_SCOPE_SQL),
                {f"{_SCOPE_PARAM_PREFIX}question_ids": normalized},
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001 - normalized to a fail-closed tool result
        raise ScopeLookupUnavailable(str(exc)) from exc
    by_id = {str(row["question_id"]).lower(): str(row["survey_id"]).lower() for row in rows}
    unknown = [question_id for question_id in normalized if question_id not in by_id]
    outside = [
        question_id for question_id in normalized
        if question_id in by_id and by_id[question_id] not in scope["authorized_survey_ids"]
    ]
    return unknown, outside


def _authorized_question_label(question_id: str) -> str:
    """Return a trusted label after the caller has authorized this question id."""
    try:
        with _sql_engine().connect() as conn:
            row = conn.execute(
                text('SELECT prompt FROM question WHERE id = CAST(:question_id AS uuid)'),
                {"question_id": question_id},
            ).mappings().first()
    except Exception as exc:  # noqa: BLE001 - chart grounding is optional, stats are not
        print(f"[pca-chart] question label unavailable: {exc}")
        return ""
    return str(row["prompt"] or "").strip() if row else ""


class SurveyAnalysisPacketInput(BaseModel):
    survey_id: str = Field(
        ...,
        description=(
            "UUID of the already-resolved current, same-client historical, or fixed "
            "benchmark survey"
        ),
    )


def get_survey_analysis_packet(survey_id: str) -> str:
    """Return a complete cached analysis packet for one authorized survey."""
    if not _UUID_RE.match(survey_id):
        return packet_bad_survey_id(survey_id)
    if not _scope_published():
        return PACKET_SCOPE_UNAVAILABLE

    try:
        owner = _survey_owner(survey_id)
    except ScopeLookupUnavailable as exc:
        print(f"[analysis-packet] scope lookup unavailable -- refusing ({exc})")
        return PACKET_SCOPE_CHECK_FAILED
    if owner is None:
        return packet_unknown_survey(survey_id)
    if not owner["in_scope"]:
        print(f"[analysis-packet] REFUSED survey {survey_id}: outside run scope")
        return packet_survey_out_of_scope(survey_id)

    result = _survey_analysis_packet_payload(survey_id)
    if not result.payload:
        return packet_not_available(owner["title"], result.status)
    return analysis_packet_preamble(owner["title"]) + result.payload


get_survey_analysis_packet.__doc__ = GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION
get_survey_analysis_packet = tool(
    args_schema=SurveyAnalysisPacketInput
)(get_survey_analysis_packet)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _safe_chart_label(value: Any, fallback: str) -> str:
    label = re.sub(r"[\r\n{}]+", " ", str(value or "")).strip()
    return label or fallback


def _sanitize_model_chart_blocks(value: str) -> tuple[str, bool]:
    """Keep bounded JSON-only generic chart payloads; PCA/word-cloud remain tool-owned."""
    withheld = False

    def replace(match: re.Match[str]) -> str:
        nonlocal withheld
        raw = match.group(0)
        body = re.sub(r"^```gpi-chart\s*|```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            payload = json.loads(body)
            chart_type = payload.get("type") if isinstance(payload, dict) else None
            canonical_type = _MODEL_CHART_ALIASES.get(chart_type, chart_type)
            if not isinstance(payload, dict) or payload.get("version") != 1 \
                    or canonical_type not in _MODEL_CHART_TYPES:
                raise ValueError("unsupported chart type or version")
            if "<script" in body.lower() or "javascript:" in body.lower():
                raise ValueError("unsafe chart content")
            series = payload.get("series")
            data = payload.get("data")
            if not any(isinstance(item, list) for item in (series, data)):
                raise ValueError("chart has no data")
            point_count = 0
            if isinstance(series, list):
                point_count += sum(
                    len(item.get("data", [])) for item in series
                    if isinstance(item, dict) and isinstance(item.get("data"), list)
                )
            if isinstance(data, list):
                point_count += len(data)
            if point_count > _MAX_CHART_POINTS:
                raise ValueError("chart exceeds point limit")
            if isinstance(series, list) and len(series) > _MAX_CHART_SERIES:
                raise ValueError("chart exceeds series limit")
            points = [
                point for item in (series or []) if isinstance(item, dict)
                for point in (item.get("data") or [])
            ]
            if canonical_type in _CHART_MATRIX_TYPES:
                if not isinstance(data, list) or not data or any(
                    not isinstance(cell, dict)
                    or not str(cell.get("x") or "").strip()
                    or not str(cell.get("y") or "").strip()
                    or _finite_float(cell.get("value")) is None
                    for cell in data
                ):
                    raise ValueError("heatmap cells require top-level x, y and numeric value")
            elif canonical_type in _CHART_SCATTER_TYPES:
                if not points or any(
                    not isinstance(point, dict)
                    or _finite_float(point.get("x")) is None
                    or _finite_float(point.get("y")) is None
                    for point in points
                ):
                    raise ValueError("scatterplot points require numeric x and y")
            elif not points or any(
                not isinstance(point, dict)
                or not str(point.get("label") or "").strip()
                or _finite_float(point.get("value")) is None
                for point in points
            ):
                raise ValueError("category points require a label and numeric value")
            return raw
        except (TypeError, ValueError, json.JSONDecodeError):
            withheld = True
            return ""

    return _GPI_CHART_BLOCK_RE.sub(replace, value), withheld


def _normalize_pca_analysis(
    data: dict[str, Any], result: dict[str, Any], *, question_id: str, question_label: str
) -> dict[str, Any]:
    analysis = result.get("pcaAnalysis")
    if not isinstance(analysis, dict):
        raise ValueError("the API response did not contain pcaAnalysis")

    attributes = [str(value) for value in analysis.get("attributes", []) if value is not None]
    products = [str(value) for value in analysis.get("productLabels", []) if value is not None]
    components: dict[str, dict[str, Any]] = {}
    for component in analysis.get("principalComponents", []):
        if not isinstance(component, dict) or not component.get("pc"):
            continue
        components[str(component["pc"]).upper()] = component

    biplot = analysis.get("biplotData") if isinstance(analysis.get("biplotData"), dict) else {}
    product_points = biplot.get("productScores", [])
    attribute_points = biplot.get("attributeLoadings", [])
    if not isinstance(product_points, list) or not isinstance(attribute_points, list):
        raise ValueError("the API response contained malformed biplot coordinates")
    # biplotData holds PC1/PC2 only -- it is the two-dimensional biplot. Every retained component
    # lives in pcScores and in each component's own loadings, so a 3D request has to take its third
    # axis from there. Measured on a live response, the two sources agree to 4e-06 (biplotData is
    # the copy rounded to five decimals), so filling in only the ABSENT keys leaves the existing
    # PC1/PC2 values untouched and keeps one scale across x, y and z.
    supplementary_products: dict[str, dict[str, Any]] = {}
    for product in analysis.get("pcScores", []):
        if not isinstance(product, dict) or product.get("product") is None:
            continue
        supplementary_products[str(product["product"])] = {
            str(score["pc"]).upper(): score.get("score")
            for score in product.get("scores", [])
            if isinstance(score, dict) and score.get("pc")
        }
    supplementary_attributes: dict[str, dict[str, Any]] = {}
    for pc, component in components.items():
        for loading in component.get("loadings", []):
            if not isinstance(loading, dict) or loading.get("attribute") is None:
                continue
            supplementary_attributes.setdefault(str(loading["attribute"]), {})[pc] = (
                loading.get("correlationLoading", loading.get("loading"))
            )

    def _with_missing_components(
        rows: list[Any], label_key: str, supplement: dict[str, dict[str, Any]]
    ) -> list[Any]:
        filled: list[Any] = []
        for row in rows:
            if not isinstance(row, dict) or row.get(label_key) is None:
                filled.append(row)
                continue
            point = dict(row)
            for pc, value in supplement.get(str(row[label_key]), {}).items():
                point.setdefault(pc, value)
            filled.append(point)
        return filled

    if product_points:
        product_points = _with_missing_components(
            product_points, "product", supplementary_products
        )
    else:
        product_points = [
            {"product": label, **scores} for label, scores in supplementary_products.items()
        ]
    if attribute_points:
        attribute_points = _with_missing_components(
            attribute_points, "attribute", supplementary_attributes
        )
    else:
        attribute_points = [
            {"attribute": label, **scores}
            for label, scores in supplementary_attributes.items()
        ]

    descriptive = [
        item for item in analysis.get("descriptiveStats", []) if isinstance(item, dict)
    ]
    analysis_ns = {
        int(item["analysisN"])
        for item in descriptive
        if isinstance(item.get("analysisN"), numbers.Integral)
    }
    analysis_n = next(iter(analysis_ns)) if len(analysis_ns) == 1 else len(products)

    return {
        "question_id": question_id or str(data.get("questionId") or ""),
        "question_label": question_label,
        "respondent_count": data.get("respondentCount"),
        "analysis_n": analysis_n,
        "attributes": attributes,
        "products": products,
        "components": components,
        "product_points": product_points,
        "attribute_points": attribute_points,
        "analysis": analysis,
    }


def _pca_points(
    rows: list[Any], *, label_key: str, pcs: Sequence[str]
) -> list[dict[str, Any]] | None:
    points: list[dict[str, Any]] = []
    axes = ("x", "y", "z")
    for row in rows:
        if not isinstance(row, dict) or row.get(label_key) is None:
            return None
        point: dict[str, Any] = {"label": str(row[label_key])}
        for axis, pc in zip(axes, pcs, strict=False):
            coordinate = _finite_float(row.get(pc))
            if coordinate is None:
                return None
            point[axis] = coordinate
        points.append(point)
    return points


def _variance_label(component: dict[str, Any], pc: str) -> str:
    variance = _finite_float(component.get("varianceExplained"))
    return f"{pc} ({variance:g}%)" if variance is not None else pc


def _build_pca_chart(
    normalized: dict[str, Any], *, requested_dimensions: Literal["2d", "3d"]
) -> dict[str, Any]:
    components = normalized["components"]

    def complete_points(pcs: Sequence[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        products = _pca_points(normalized["product_points"], label_key="product", pcs=pcs)
        attributes = _pca_points(
            normalized["attribute_points"], label_key="attribute", pcs=pcs
        )
        if products is None or attributes is None or not products or not attributes:
            return None
        return products, attributes

    fallback_reason = ""
    dimensions = requested_dimensions
    points = None
    pc3 = components.get("PC3")
    if requested_dimensions == "3d":
        pc3_variance = _finite_float(pc3.get("varianceExplained")) if pc3 else None
        # Name the actual cause. Reporting a missing coordinate as "not retained" told readers the
        # component had been dropped when the analysis had in fact returned it.
        if pc3 is None:
            fallback_reason = "the analysis returned no third component"
        elif pc3_variance is None or pc3_variance <= 0:
            fallback_reason = "PC3 explains none of the variance"
        else:
            points = complete_points(("PC1", "PC2", "PC3"))
            if points is None:
                fallback_reason = (
                    "PC3 coordinates are missing for some products or attributes"
                )
        if points is None:
            dimensions = "2d"

    if dimensions == "2d":
        if "PC1" not in components or "PC2" not in components:
            raise ValueError("at least PC1 and PC2 are required by the chart contract")
        points = complete_points(("PC1", "PC2"))
        if points is None:
            raise ValueError("PC1/PC2 biplot coordinates are incomplete or non-finite")

    assert points is not None
    product_points, attribute_points = points
    if len(product_points) > _MAX_CHART_POINTS:
        raise ValueError(
            f"{len(product_points)} products exceed the {_MAX_CHART_POINTS}-point chart limit"
        )
    displayed_attribute_count = len(attribute_points)
    total_attribute_count = len(attribute_points)
    remaining = _MAX_CHART_POINTS - len(product_points)
    if remaining < 1:
        raise ValueError("the chart limit leaves no room for attribute coordinates")
    if len(attribute_points) > remaining:
        axes = ("x", "y", "z")[: 3 if dimensions == "3d" else 2]
        attribute_points = sorted(
            attribute_points,
            key=lambda point: (
                -sum(float(point[axis]) ** 2 for axis in axes),
                str(point["label"]).casefold(),
            ),
        )[:remaining]
        displayed_attribute_count = len(attribute_points)

    label = _safe_chart_label(normalized.get("question_label"), "Selected measure")
    source_n = normalized.get("respondent_count")
    base_size = int(source_n) if isinstance(source_n, numbers.Integral) \
        and not isinstance(source_n, bool) else None
    subtitle_parts = [
        f"Product-mean profiles; analysis N = {normalized['analysis_n']} products",
        f"source N = {source_n} respondents" if source_n is not None else "source N unavailable",
    ]
    if displayed_attribute_count < total_attribute_count:
        subtitle_parts.append(
            f"showing {displayed_attribute_count} of {total_attribute_count} attributes"
        )
    if fallback_reason:
        subtitle_parts.append("2D fallback because PC3 is unavailable")

    payload: dict[str, Any] = {
        "version": 1,
        "type": f"pca_biplot_{dimensions}",
        "title": f"{'3D PCA map' if dimensions == '3d' else 'PCA biplot'} — {label}",
        "subtitle": "; ".join(subtitle_parts),
        "x_axis": {"label": _variance_label(components["PC1"], "PC1")},
        "y_axis": {"label": _variance_label(components["PC2"], "PC2")},
        "metadata": {
            "source": "Charts API survey statistics",
            "question_id": normalized.get("question_id", ""),
            "question_label": label,
            "base_size": base_size,
            "products": [point["label"] for point in product_points],
            "method": "PCA biplot on product mean profiles",
        },
        "products": product_points,
        "attributes": attribute_points,
    }
    if dimensions == "3d":
        payload["z_axis"] = {"label": _variance_label(components["PC3"], "PC3")}
    if base_size is None:
        payload["metadata"].pop("base_size")
    if not payload["metadata"]["question_id"]:
        payload["metadata"].pop("question_id")

    artifact = "```gpi-chart\n" + json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False
    ) + "\n```"
    two_d_suggestion = f"Show a 2D PCA plot of {label}" if dimensions == "3d" else ""
    three_d_suggestion = ""
    if dimensions == "2d" and pc3 and _finite_float(pc3.get("varianceExplained")) not in (None, 0.0):
        three_d_suggestion = f"Show a 3D PCA plot of {label}"
    return {
        "artifact": artifact,
        "payload": payload,
        "dimensions": dimensions,
        "requested_dimensions": requested_dimensions,
        "fallback_reason": fallback_reason,
        "question_label": label,
        "two_d_suggestion": two_d_suggestion,
        "three_d_suggestion": three_d_suggestion,
    }


def _format_pca_analysis(normalized: dict[str, Any], chart: dict[str, Any] | None) -> list[str]:
    analysis = normalized["analysis"]
    lines = [
        "\nPCA (product-mean profile analysis):",
        f"  Source respondents={normalized.get('respondent_count', '?')}; "
        f"analysis observations={normalized['analysis_n']} products",
        f"  Attributes: {', '.join(normalized['attributes']) or 'not reported'}",
        f"  Products: {', '.join(normalized['products']) or 'not reported'}",
        "  Components:",
    ]
    cumulative = analysis.get("cumulativeVariance", [])
    for index, component in enumerate(analysis.get("principalComponents", [])):
        if not isinstance(component, dict):
            continue
        cumulative_value = cumulative[index] if index < len(cumulative) else None
        lines.append(
            f"    {component.get('pc', '?')}: eigenvalue={component.get('eigenvalue')}, "
            f"variance={component.get('varianceExplained')}%, cumulative={cumulative_value}%"
        )
        for loading in component.get("loadings", []):
            if isinstance(loading, dict):
                lines.append(
                    f"      {loading.get('attribute', '?')}: loading={loading.get('loading')}, "
                    f"correlation_loading={loading.get('correlationLoading')}"
                )
    lines.append("  Product scores:")
    for product in analysis.get("pcScores", []):
        if not isinstance(product, dict):
            continue
        scores = ", ".join(
            f"{score.get('pc')}={score.get('score')}"
            for score in product.get("scores", []) if isinstance(score, dict)
        )
        lines.append(f"    {product.get('product', '?')}: {scores}")
    lines.append(
        f"  Retention: components={analysis.get('numberOfComponents')}, "
        f"matrix_rank={analysis.get('matrixRank')}, policy={analysis.get('retentionPolicy')}"
    )
    diagnostics = analysis.get("numericalDiagnostics")
    if diagnostics:
        lines.append(f"  Numerical diagnostics: {json.dumps(diagnostics, ensure_ascii=False)}")
    warnings = analysis.get("warnings") or []
    dropped = analysis.get("droppedZeroVarianceAttributes") or []
    lines.append(f"  Warnings: {json.dumps(warnings, ensure_ascii=False)}")
    lines.append(f"  Zero-variance attributes excluded: {json.dumps(dropped, ensure_ascii=False)}")
    if chart:
        if chart["fallback_reason"]:
            lines.append(
                "  Plot: trusted 2D PCA chart prepared; "
                f"{chart['fallback_reason']}."
            )
        else:
            lines.append(f"  Plot: trusted {chart['dimensions'].upper()} PCA chart prepared.")
        lines.append("  Do not emit or reproduce chart JSON; the runtime appends it.")
    return lines


def _prepare_stats_response(
    data: dict[str, Any], *, question_id: str = "", question_label: str = "",
    pca_plot_dimensions: Literal["2d", "3d"] = "2d",
) -> tuple[str, list[dict[str, Any]]]:
    lines = [
        f"Question type: {data.get('questionType', 'unknown')}, "
        f"respondents: {data.get('respondentCount', '?')}"
    ]
    stats = data.get("stats", {})
    artifacts: list[dict[str, Any]] = []

    for alpha, result in stats.get("anova", {}).items():
        if "error" in result:
            lines.append(f"\nANOVA [alpha={alpha}]: Not available - {result['error']}")
            continue
        for attr, r in result.get("anovaResults", {}).items():
            sig = "significant" if r.get("reject") else "not significant"
            attr_label = f" ({attr})" if attr != "Value" else ""
            lines.append(f"\nANOVA [alpha={alpha}]{attr_label}: F={r.get('F')}, p={r.get('pVal')} - {sig}")

    for alpha, result in stats.get("tukey", {}).items():
        if "error" in result:
            lines.append(f"\nTukey [alpha={alpha}]: Not available - {result['error']}")
            continue
        hsd = result.get("tukeyHSD", {})
        for attr, letters in hsd.get("assignedLetters", {}).items():
            attr_label = f" ({attr})" if attr != "Value" else ""
            lines.append(f"\nTukey grouping{attr_label}:")
            for product, letter in letters.items():
                mean = hsd.get("meansByAttribute", {}).get(attr, {}).get(product)
                lines.append(f"  {product}: group {letter} (mean={mean})")
        lines.append("(products sharing a letter are NOT significantly different)")

    correlation_p_values: list[tuple[float, float]] = []
    for test_name in ("pearson", "spearman"):
        for alpha, result in stats.get(test_name, {}).items():
            if "error" in result:
                lines.append(f"\n{test_name.upper()} [alpha={alpha}]: Not available - {result['error']}")
                continue
            # Real API shape: {correlationAnalysis: {table: {dataSource: [{attribute, coefficient, "p-val"}]}}}
            # - "table" is a single dict, not a dict of named sub-tables.
            corr = result.get("correlationAnalysis", {})
            table = corr.get("table", {})
            rows = table.get("dataSource", [])
            if rows:
                for row in rows:
                    lines.append(
                        f"\n{test_name.upper()} [alpha={alpha}] {row.get('attribute', '?')}: "
                        f"coefficient={row.get('coefficient')}, p={row.get('p-val')}"
                    )
                    try:
                        correlation_p_values.append((float(row["p-val"]), float(alpha)))
                    except (KeyError, TypeError, ValueError):
                        pass
            else:
                lines.append(f"\n{test_name.upper()} [alpha={alpha}]: {json.dumps(result)}")

    if len(correlation_p_values) >= 2 and all(p >= a for p, a in correlation_p_values):
        lines.append(
            "\n(No correlation above reached its alpha. These are SEPARATE tests on product "
            "subsets, not one test of the overall relationship, and a real effect can miss alpha "
            "in every subset. Report this as INCONCLUSIVE at this split -- explicitly not as "
            "evidence that no relationship exists, and not as \"X is not a driver\". State that "
            "the per-product split cannot settle the question. This tool cannot test the pooled "
            "relationship across products.)"
        )

    for alpha, result in stats.get("chi-square", {}).items():
        if "error" in result:
            lines.append(f"\nChi-square [alpha={alpha}]: Not available - {result['error']}")
            continue
        lines.append(f"\nChi-square [alpha={alpha}]: {result}")

    pca_results = stats.get("pca", {})
    for _bucket, result in pca_results.items():
        if not isinstance(result, dict):
            lines.append("\nPCA: Not available - malformed API result")
            continue
        if "error" in result:
            lines.append(f"\nPCA: Not available - {result['error']}")
            continue
        try:
            normalized = _normalize_pca_analysis(
                data, result, question_id=question_id, question_label=question_label
            )
        except (TypeError, ValueError) as exc:
            lines.append(f"\nPCA: Not available - malformed API payload ({exc})")
            continue
        chart = None
        try:
            chart = _build_pca_chart(
                normalized, requested_dimensions=pca_plot_dimensions
            )
            artifacts.append(chart)
        except ValueError as exc:
            lines.append(f"\nPCA plot: Not available - {exc}")
        lines.extend(_format_pca_analysis(normalized, chart))
        break

    handled = {"anova", "tukey", "pearson", "spearman", "chi-square", "pca"}
    unhandled = sorted(str(key) for key in stats if key not in handled)
    if unhandled:
        lines.append(f"\nUnhandled statistics returned: {', '.join(unhandled)}")

    return "\n".join(lines), artifacts


def _format_stats_response(data: dict) -> str:
    text_value, _artifacts = _prepare_stats_response(data)
    return text_value


# Description: tool_prompts.RUN_SURVEY_STATS_DESCRIPTION, assigned below.
def run_survey_stats(
    question_id: str,
    stats_types: str,
    alpha_value: float = 0.05,
    reference_question_id: str = "",
    penalty_level: str = "",
    boxing_strategy: str = "",
    pca_plot_dimensions: Literal["2d", "3d"] = "2d",
) -> str:
    stats_started = time.perf_counter()

    def log_timing(status: str, api_started: float | None = None) -> None:
        now = time.perf_counter()
        api_ms = 0.0 if api_started is None else (now - api_started) * 1000
        with _STATS_TIMING_LOG_LOCK:
            print(
                f"[stats-timing] charts_api={api_ms:.1f}ms "
                f"total={(now - stats_started) * 1000:.1f}ms "
                f"status={status} question_id={question_id}"
            )

    if not _UUID_RE.match(question_id):
        return stats_bad_question_id(question_id)

    canonical_types: list[str] = []
    for raw_type in stats_types.split(","):
        normalized_type = raw_type.strip().lower()
        if normalized_type in {"pca-biplot", "pca-biplot-3d"}:
            normalized_type = "pca"
        if normalized_type and normalized_type not in canonical_types:
            canonical_types.append(normalized_type)
    stats_types = ",".join(canonical_types)
    requested_types = set(canonical_types)
    if requested_types & _REQUIRES_REFERENCE and not reference_question_id:
        return stats_missing_reference(stats_types)
    if reference_question_id and not _UUID_RE.match(reference_question_id):
        return stats_bad_reference_id(reference_question_id)

    if not _scope_published():
        return "Statistics unavailable: the run's survey scope is not established."
    question_ids = [question_id] + ([reference_question_id] if reference_question_id else [])
    try:
        unknown_ids, outside_ids = _authorize_question_ids(question_ids)
    except ScopeLookupUnavailable as exc:
        print(f"[stats] scope lookup unavailable -- refusing ({exc})")
        return "Statistics unavailable: survey ownership could not be verified."
    if outside_ids:
        print(f"[stats] REFUSED question id(s) outside run boundary: {outside_ids}")
        return "Statistics refused: a question is outside the initial survey's client boundary."
    if unknown_ids:
        return f"Statistics unavailable: unknown question UUID(s): {unknown_ids}."

    url = f"{CHARTS_API_BASE_URL}/chartingAPI/charts/public/stats/{question_id}"
    params: dict[str, Any] = {"statsTypes": stats_types, "alphaValue": alpha_value}
    if reference_question_id:
        params["referenceQuestionId"] = reference_question_id
    if penalty_level:
        params["penaltyLevel"] = penalty_level
    if boxing_strategy:
        params["boxingStrategy"] = boxing_strategy

    api_started = time.perf_counter()
    try:
        response = requests.get(
            url,
            params=params,
            headers={"x-charts-stats-secret": CHARTS_STATS_EXTERNAL_ACCESS_SECRET},
            timeout=_CHARTS_TIMEOUT_S,
        )
    except requests.Timeout:
        log_timing("timeout", api_started)
        return STATS_TIMEOUT
    except requests.RequestException as exc:
        log_timing("request_error", api_started)
        return stats_request_failed(exc)

    if response.status_code != 200:
        log_timing(f"http_{response.status_code}", api_started)
        try:
            body = response.json()
            return stats_http_error(response.status_code, body.get("message", response.text))
        except ValueError:
            return stats_http_error(response.status_code, response.text)

    log_timing("ok", api_started)
    data = response.json()
    question_label = _authorized_question_label(question_id) if "pca" in requested_types else ""
    formatted, artifacts = _prepare_stats_response(
        data,
        question_id=question_id,
        question_label=question_label,
        pca_plot_dimensions=pca_plot_dimensions,
    )
    if artifacts:
        return _PCA_TOOL_RESULT_PREFIX + json.dumps(
            {"text": formatted, "artifacts": artifacts}, ensure_ascii=False, allow_nan=False
        )
    return formatted


run_survey_stats.__doc__ = RUN_SURVEY_STATS_DESCRIPTION
run_survey_stats = tool(run_survey_stats)


class InfluenceVariable(BaseModel):
    question_id: str = Field(..., description="Resolved UUID of the survey question")
    component_label: str = Field(
        "",
        description=(
            "Exact question_option label for one line-scale component. Omit to use every "
            "component separately; always omit for a vertical-rating"
        ),
    )

    # A copied UUID often arrives with surrounding whitespace, which _UUID_RE rejects and costs a
    # whole model turn to retry. component_label is left alone: it is matched against the stored
    # option label, which may legitimately carry its own spacing.
    @field_validator("question_id")
    @classmethod
    def _strip_question_id(cls, value: str) -> str:
        return value.strip()


def _influence_rows_sql(variable_count: int) -> str:
    values = ",\n       ".join(
        f"(:slot_{i}, :role_{i}, CAST(:qid_{i} AS uuid), :component_{i})"
        for i in range(variable_count)
    )
    return f"""
WITH explicit_requested(slot, request_role, question_id, requested_component) AS (
  VALUES {values}
), target_survey AS (
  SELECT q."surveyId" AS survey_id
  FROM question q
  WHERE q.id = CAST(:target_qid AS uuid)
), implicit_requested AS (
  SELECT 1000 + ROW_NUMBER() OVER (ORDER BY q.id) AS slot,
         'feature'::text AS request_role, q.id AS question_id,
         NULL::text AS requested_component
  FROM question q
  JOIN target_survey ts ON ts.survey_id = q."surveyId"
  WHERE :include_all_features
    AND q.id <> CAST(:target_qid AS uuid)
    AND q."typeOfQuestion" IN ('vertical-rating', 'line-scale')
), requested AS (
  SELECT * FROM explicit_requested
  UNION ALL
  SELECT * FROM implicit_requested
), configured_options AS (
  SELECT qo.question_id,
         COALESCE(
           jsonb_agg(
             jsonb_build_object('id', qo.id::text, 'label', COALESCE(qo.label, ''))
             ORDER BY qo."order", qo.id
           ) FILTER (WHERE qo.id IS NOT NULL),
           '[]'::jsonb
         ) AS components
  FROM question_option qo
  WHERE qo.question_id IN (SELECT question_id FROM requested)
  GROUP BY qo.question_id
)
SELECT r.slot, r.request_role, r.requested_component,
       q.id::text AS question_id, q.prompt, q."typeOfQuestion" AS question_type,
       s.id::text AS survey_id, s.title AS survey_title,
       CASE WHEN s.id IS NULL THEN false ELSE
         (s.id = ANY(CAST(:authorized_survey_ids AS uuid[])))
       END AS in_scope,
       COALESCE(co.components, '[]'::jsonb) AS components,
       a.enrollment_id::text AS enrollment_id,
       a.product_id::text AS product_id,
       aqo.question_option_id::text AS answer_component_id,
       aqo."answerData" ->> 'optionAnswer' AS raw_value
FROM requested r
LEFT JOIN question q ON q.id = r.question_id
LEFT JOIN survey s ON s.id = q."surveyId"
LEFT JOIN configured_options co ON co.question_id = q.id
LEFT JOIN answer a ON a.question_id = q.id AND a."isSkipped" IS NOT TRUE
LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
ORDER BY r.slot, a.enrollment_id, a.product_id, aqo.id
"""


def _fetch_influence_rows(
    target: InfluenceVariable, features: list[InfluenceVariable]
) -> list[dict[str, Any]]:
    variables = [target, *features]
    with _sql_engine().connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        scope = _resolve_authorized_scope(conn)
        question_rows = conn.execute(
            text(_QUESTION_SCOPE_SQL),
            {
                f"{_SCOPE_PARAM_PREFIX}question_ids": [
                    variable.question_id.lower() for variable in variables
                ],
            },
        ).mappings().all()
        question_surveys = {
            str(row["question_id"]).lower(): str(row["survey_id"]).lower()
            for row in question_rows
        }
        for variable in variables:
            question_survey = question_surveys.get(variable.question_id.lower())
            if question_survey is None:
                raise ValueError(f"Unknown question_id: {variable.question_id}.")
            if question_survey not in scope["authorized_survey_ids"]:
                raise PermissionError(
                    f"Question {variable.question_id} is outside the initial survey's client boundary."
                )

        target_survey = question_surveys[target.question_id.lower()]
        if any(
            question_surveys[feature.question_id.lower()] != target_survey
            for feature in features
        ):
            raise ValueError("Target and features must belong to the same survey.")

        params: dict[str, Any] = {
            "authorized_survey_ids": list(scope["authorized_survey_ids"]),
            "target_qid": target.question_id,
            "include_all_features": not features,
        }
        for index, variable in enumerate(variables):
            params[f"slot_{index}"] = index
            params[f"role_{index}"] = "target" if index == 0 else "feature"
            params[f"qid_{index}"] = variable.question_id
            params[f"component_{index}"] = variable.component_label or None
        result = conn.execute(text(_influence_rows_sql(len(variables))), params)
        return [dict(row) for row in result.mappings().all()]


def _components_from_row(row: dict[str, Any]) -> list[dict[str, str]]:
    components = row.get("components") or []
    if isinstance(components, str):
        components = json.loads(components)
    return [
        {"id": str(component.get("id", "")), "label": str(component.get("label", ""))}
        for component in components if isinstance(component, dict)
    ]


def _variable_name(row: dict[str, Any], selected_component: dict[str, str] | None) -> str:
    prompt = re.sub(r"<[^>]+>", " ", str(row.get("prompt") or ""))
    prompt = re.sub(r"\s+", " ", prompt).strip()
    if selected_component and selected_component["label"].strip():
        return f"{prompt} — {selected_component['label'].strip()}"
    return prompt


def _question_specs(
    slot_rows: list[dict[str, Any]],
    variable: InfluenceVariable,
    *,
    implicit: bool,
) -> list[dict[str, Any]]:
    first = slot_rows[0] if slot_rows else {}
    if not first.get("question_id"):
        raise ValueError(f"Unknown question_id: {variable.question_id}.")
    if not bool(first.get("in_scope")):
        raise PermissionError(f"Question {variable.question_id} is outside the current scope.")
    question_type = str(first.get("question_type") or "")
    if question_type not in {"vertical-rating", "line-scale"}:
        raise ValueError(
            f"Unsupported question type {question_type!r} for {variable.question_id}; "
            "use a numeric vertical-rating or line-scale."
        )

    components = _components_from_row(first)
    if question_type == "vertical-rating":
        if variable.component_label:
            raise ValueError(
                f"component_label must be omitted for vertical-rating question "
                f"{variable.question_id}."
            )
        selected_components: list[dict[str, str] | None] = [None]
    elif variable.component_label:
        selected_components = [
            component for component in components
            if component["label"] == variable.component_label
        ]
        if len(selected_components) != 1:
            available = [component["label"] for component in components]
            raise ValueError(
                f"Line-scale component {variable.component_label!r} is not uniquely "
                f"available for {variable.question_id}. Available labels: {available}."
            )
    else:
        selected_components = list(components)
        if not selected_components:
            raise ValueError(
                f"Line-scale question {variable.question_id} has no configured components."
            )

    answered = [row for row in slot_rows if row.get("enrollment_id")]
    has_product = any(row.get("product_id") is not None for row in answered)
    has_no_product = any(row.get("product_id") is None for row in answered)
    if has_product and has_no_product:
        raise ValueError(
            f"Question {variable.question_id} mixes product-linked and respondent-level "
            "answers and cannot be analyzed safely."
        )
    grain = "respondent_product" if has_product else "respondent"
    return [{
        "rows": slot_rows,
        "component": component,
        "name": _variable_name(first, component),
        "question_id": variable.question_id,
        "survey_id": str(first["survey_id"]),
        "grain": grain,
        "implicit": implicit,
    } for component in selected_components]


def _numeric_value_map(
    item: dict[str, Any], product_grain: bool
) -> dict[tuple[str, ...], float]:
    values: dict[tuple[str, ...], float] = {}
    selected_id = item["component"]["id"] if item["component"] else None
    for row in item["rows"]:
        if not row.get("enrollment_id"):
            continue
        if selected_id and row.get("answer_component_id") != selected_id:
            continue
        try:
            number = float(row.get("raw_value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        entity = (
            (str(row["enrollment_id"]), str(row["product_id"]))
            if product_grain else (str(row["enrollment_id"]),)
        )
        if entity in values:
            raise ValueError(
                f"Duplicate numeric observations found for {item['name']!r} at entity "
                f"{entity}; values were not averaged."
            )
        values[entity] = number
    return values


def _resolve_influence_specs(
    rows: list[dict[str, Any]],
    target: InfluenceVariable,
    features: list[InfluenceVariable],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    by_slot: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_slot.setdefault(int(row["slot"]), []).append(row)
    target_specs = _question_specs(by_slot.get(0, []), target, implicit=False)
    target_survey = target_specs[0]["survey_id"]
    target_grain = target_specs[0]["grain"]

    feature_specs: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    explicit_slots = {index + 1: variable for index, variable in enumerate(features)}
    for slot in sorted(key for key in by_slot if key != 0):
        slot_rows = by_slot[slot]
        first = slot_rows[0]
        explicit = slot in explicit_slots
        variable = explicit_slots.get(slot) or InfluenceVariable(
            question_id=str(first.get("question_id") or "")
        )
        try:
            expanded = _question_specs(slot_rows, variable, implicit=not explicit)
        except ValueError as exc:
            if explicit:
                raise
            excluded.append({
                "label": _variable_name(first, None), "reason": str(exc),
            })
            continue
        for item in expanded:
            if item["survey_id"] != target_survey:
                if explicit:
                    raise ValueError("Target and features must belong to the same survey.")
                excluded.append({"label": item["name"], "reason": "different survey"})
            elif item["grain"] != target_grain:
                if explicit:
                    raise ValueError(
                        "Target and features use different respondent/product grains; v1 does "
                        "not replicate respondent-level values across products."
                    )
                excluded.append({"label": item["name"], "reason": "incompatible grain"})
            else:
                feature_specs.append(item)

    target_refs = {
        (item["question_id"], item["component"]["id"] if item["component"] else "")
        for item in target_specs
    }
    seen_features: set[tuple[str, str]] = set()
    unique_features = []
    for item in feature_specs:
        ref = (item["question_id"], item["component"]["id"] if item["component"] else "")
        if ref in target_refs or ref in seen_features:
            if not item["implicit"]:
                raise ValueError("Target and feature references must be distinct.")
            continue
        seen_features.add(ref)
        unique_features.append(item)

    product_grain = target_grain == "respondent_product"
    for item in target_specs:
        item["values"] = _numeric_value_map(item, product_grain)
    usable_features = []
    for item in unique_features:
        item["values"] = _numeric_value_map(item, product_grain)
        if item["values"] or not item["implicit"]:
            usable_features.append(item)
        else:
            excluded.append({"label": item["name"], "reason": "no numeric responses"})
    if not usable_features:
        raise ValueError("No compatible numeric feature responses are available.")
    return target_specs, usable_features, excluded


def _prepare_target_data(
    target: dict[str, Any], features: list[dict[str, Any]]
) -> tuple[list[list[float]], list[float], list[str], list[str] | None,
           list[str], dict[str, Any]]:
    value_maps = [target["values"], *[feature["values"] for feature in features]]
    complete_entities = set(value_maps[0])
    for values in value_maps[1:]:
        complete_entities.intersection_update(values)
    entities = sorted(complete_entities)
    product_grain = target["grain"] == "respondent_product"
    feature_values = [
        [value_maps[slot][entity] for slot in range(1, len(value_maps))]
        for entity in entities
    ]
    target_values = [value_maps[0][entity] for entity in entities]
    respondent_groups = [entity[0] for entity in entities]
    products = [entity[1] for entity in entities] if product_grain else None
    provenance = {
        "survey_id": target["survey_id"],
        "grain": target["grain"],
        "target": {"question_id": target["question_id"], "label": target["name"]},
        "entities_with_target": len(value_maps[0]),
        "entities_with_any_requested_value": len(set().union(*map(set, value_maps))),
        "complete_entities": len(entities),
        "rows_removed_by_alignment": len(value_maps[0]) - len(entities),
    }
    return (
        feature_values, target_values, respondent_groups, products,
        [feature["name"] for feature in features], provenance,
    )


# PLSR ranks attributes by VIP on product means; it tests no attribute for significance.
class PLSRInput(BaseModel):
    kpi: InfluenceVariable
    attributes: list[InfluenceVariable] = Field(
        default_factory=list,
        max_length=100,
        description=(
            "Attribute questions/components to project onto the KPI. Leave empty to use every "
            "other compatible numeric measure in the survey"
        ),
    )
    normalization: Literal["reference", "zscore_once", "pls_internal", "none"] | None = Field(
        default=None,
        description=(
            "Required user choice: reference (outer z-score plus PLS internal scaling), "
            "zscore_once (outer predictor z-score only), pls_internal (PLS internal scaling "
            "only), or none (centering only)"
        ),
    )
    cv_method: Literal["loo", "kfold", "none"] | None = Field(
        default=None,
        description="Required user choice: loo, kfold, or none",
    )
    cv_folds: int | None = Field(
        default=None,
        ge=2,
        le=20,
        description=(
            "Required only for kfold; number of deterministic unshuffled folds. Each fold is "
            "scored on its own held-out products, so it must be at most half the product "
            "count -- ask the user for a fold count, never assume one"
        ),
    )
    n_components: int | None = Field(
        default=None,
        ge=1,
        description=(
            f"Optional, unlike the choices above; omit it and the fit uses {DEFAULT_COMPONENTS}. "
            "Number of PLS components to extract. Pass it only when the user names a component "
            "count, and never ask for one -- omitting it is what records that they did not. A "
            "count above what the data supports is fitted at that ceiling rather than refused; "
            "the model block reports both"
        ),
    )


# PLS is built for wide designs, so this cap is generous; it exists only to bound the fetch.
_MAX_EXPANDED_PLSR_ATTRIBUTES = 100


def _round_plsr_output(value: Any) -> Any:
    # PLS coefficients on standardised attributes land around 1e-2, so rounding to four
    # decimals would flatten the tail of the ranking into ties.
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 9)
    if isinstance(value, dict):
        return {key: _round_plsr_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_plsr_output(item) for item in value]
    return value


def analyze_plsr(
    kpi: InfluenceVariable,
    attributes: list[InfluenceVariable] | None = None,
    normalization: Literal["reference", "zscore_once", "pls_internal", "none"] | None = None,
    cv_method: Literal["loo", "kfold", "none"] | None = None,
    cv_folds: int | None = None,
    n_components: int | None = None,
) -> str:
    """Fetch aligned survey ratings, average them per product, and fit PLS onto the KPI."""
    if normalization is None or cv_method is None:
        return (
            "PLSR options required before analysis: ask the user to choose predictor "
            "normalization (reference, zscore_once, pls_internal, or none) and cross-validation "
            "(loo, kfold with a fold count, or none), then call analyze_plsr with those choices."
        )
    if cv_method == "kfold" and cv_folds is None:
        return "PLSR options incomplete: kfold requires a fold count (for example, 5)."
    if cv_method != "kfold" and cv_folds is not None:
        return "PLSR options invalid: cv_folds is only allowed with kfold."
    # None means the user named no count, which the tool arguments now record; the fit still
    # runs at the default.
    if n_components is None:
        n_components = DEFAULT_COMPONENTS

    kpi = InfluenceVariable.model_validate(kpi)
    attributes = [
        InfluenceVariable.model_validate(attribute) for attribute in (attributes or [])
    ]
    variables = [kpi, *attributes]
    invalid_ids = [variable.question_id for variable in variables
                   if not _UUID_RE.match(variable.question_id)]
    if invalid_ids:
        return f"PLSR unavailable: invalid question UUID(s): {invalid_ids}."
    if not _scope_published():
        return "PLSR unavailable: the run's survey scope is not established."

    try:
        rows = _fetch_influence_rows(kpi, attributes)
        kpi_specs, attribute_specs, excluded = _resolve_influence_specs(
            rows, kpi, attributes
        )
        if len(attribute_specs) > _MAX_EXPANDED_PLSR_ATTRIBUTES:
            raise ValueError(
                f"The requested questions expand to {len(attribute_specs)} attributes; the "
                f"maximum is {_MAX_EXPANDED_PLSR_ATTRIBUTES}. Name a smaller attribute set."
            )
    except PermissionError as exc:
        print(f"[plsr] REFUSED: {exc}")
        return f"PLSR refused: {exc}"
    except ValueError as exc:
        return f"PLSR unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 - returned as repairable tool feedback
        print(f"[plsr] database failure: {exc}")
        return f"PLSR unavailable: database retrieval failed ({exc})."

    def run_kpi(item: dict[str, Any]) -> dict[str, Any]:
        x, y, _groups, products, names, provenance = _prepare_target_data(
            item, attribute_specs
        )
        result: dict[str, Any] = {"kpi": provenance["target"], "provenance": provenance}
        if products is None:
            result.update({
                "status": "not_product_linked",
                "reason": (
                    "PLSR runs on product means, but these responses are not linked to "
                    "products, so there is nothing to average over."
                ),
            })
            return result
        unit_x, unit_y, unit_ids, row_counts = aggregate_by_unit(x, y, products)
        # A fold holding one product has no variance, so its R2 -- the number the reference's
        # cross_val_score averages -- is undefined. The fold count is the user's choice, so
        # this comes back as a repairable option error rather than a data verdict.
        if cv_method == "kfold" and cv_folds is not None and (
            cv_folds > max_kfold_folds(len(unit_ids))
        ):
            result.update({
                "status": "invalid_cv_folds",
                "n_products": len(unit_ids),
                "max_cv_folds": max_kfold_folds(len(unit_ids)),
                "reason": (
                    f"{cv_folds}-fold cross-validation needs at least two products per fold, "
                    f"but this KPI has only {len(unit_ids)} product(s); "
                    + (
                        f"the largest usable fold count is {max_kfold_folds(len(unit_ids))}. "
                        f"Ask the user for a fold count no larger than that, or for loo, then "
                        f"call analyze_plsr again."
                        if max_kfold_folds(len(unit_ids)) >= 2 else
                        "kfold cannot run on this KPI at all. Ask the user for loo or for no "
                        "cross-validation, then call analyze_plsr again."
                    )
                ),
            })
            return result
        try:
            features, metrics = plsr_analysis(
                unit_x,
                unit_y,
                feature_names=names,
                n_components=n_components,
                normalization=normalization,
                cv_method=cv_method,
                cv_folds=cv_folds,
            )
        except ValueError as exc:
            result.update({"status": "insufficient_data", "reason": str(exc)})
            return result
        metrics["n_products"] = len(unit_ids)
        metrics["respondent_rows_per_product"] = row_counts
        metrics["respondent_rows_total"] = int(sum(row_counts))
        result.update({"status": "ok", "model": metrics, "features": features})
        return result

    analyses = [run_kpi(item) for item in kpi_specs]
    successful = [analysis for analysis in analyses if analysis["status"] == "ok"]
    if successful:
        overall_status = "ok"
    elif analyses and all(item["status"] == "invalid_cv_folds" for item in analyses):
        overall_status = "invalid_cv_folds"
    else:
        overall_status = "insufficient_data"
    payload = {
        "status": overall_status,
        "method": "pls_regression",
        "grain": "product_means_from_respondent_responses",
        "n_components_requested": n_components,
        "normalization": normalization,
        "cv_method": cv_method,
        "cv_folds": cv_folds,
        "attribute_selection": "provided" if attributes else "all_other_compatible_numeric",
        "candidate_attributes": [
            {"question_id": item["question_id"], "label": item["name"]}
            for item in attribute_specs
        ],
        "excluded_candidates": excluded,
        "analyses": analyses,
    }
    return PLSR_RESULT_HEAD + "\n" + json.dumps(
        _round_plsr_output(payload), ensure_ascii=False
    )


analyze_plsr.__doc__ = ANALYZE_PLSR_DESCRIPTION
analyze_plsr = tool(args_schema=PLSRInput)(analyze_plsr)


# Respondent clustering. Like PLSR, the model passes question ids and the tool fetches the
# respondent-level rows itself: a 450-respondent profile matrix cannot cross the context window
# twice without the numbers being retyped by a language model on the way.
_MIN_CLUSTER_K = 2
_MAX_CLUSTER_K = 8
_MIN_CLUSTER_RESPONDENTS = 10

# A summary measure restates the attributes it summarises, so clustering on it invents a split the
# attributes do not support (measured: it produced a cluster defined by "overall sits 2.7 points
# below this respondent's own attribute average", which is an artifact of the composition, not a
# group of people). Detection only has the prompt text to go on, so it is deliberately narrow --
# an overall/general cue AND no attribute named. It steers a refusal the model can recover from by
# re-calling with overall_question_id; it never silently drops a question from the basis.
_SUMMARY_CUE_RE = re.compile(
    r"\b(overall|in general|everything all together|all together)\b", re.IGNORECASE
)
_ATTRIBUTE_CUE_RE = re.compile(
    r"\b(appearance|look|looks|colou?r|aroma|odou?r|smell|flavou?r|taste|texture|mouthfeel"
    r"|sweet\w*|salt\w*|sour\w*|bitter\w*|aftertaste|packaging|crunch\w*|crisp\w*|juic\w*"
    r"|size|shape|thickness|denseness|consistency)\b",
    re.IGNORECASE,
)


def _is_summary_measure(prompt: str) -> bool:
    return bool(_SUMMARY_CUE_RE.search(prompt)) and not _ATTRIBUTE_CUE_RE.search(prompt)


def _cluster_rows_sql(question_count: int) -> str:
    values = ",\n       ".join(
        f"(:slot_{index}, CAST(:qid_{index} AS uuid))" for index in range(question_count)
    )
    return f"""
WITH requested(slot, question_id) AS (
  VALUES {values}
)
SELECT r.slot,
       q.id::text AS question_id,
       q.prompt,
       q."typeOfQuestion" AS question_type,
       q."surveyId"::text AS survey_id,
       COALESCE(q.settings -> 'settingSlider' ->> 'min', '?') || '..' ||
       COALESCE(q.settings -> 'settingSlider' ->> 'max', '?') AS scale,
       a.enrollment_id::text AS enrollment_id,
       a.product_id::text AS product_id,
       p.name AS product_name,
       aqo."answerData" ->> 'optionAnswer' AS raw_value
FROM requested r
LEFT JOIN question q ON q.id = r.question_id
LEFT JOIN answer a ON a.question_id = q.id AND a."isSkipped" IS NOT TRUE
LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
LEFT JOIN product p ON p.id = a.product_id
ORDER BY r.slot, a.enrollment_id
"""


def _fetch_cluster_rows(question_ids: list[str]) -> list[dict[str, Any]]:
    """Respondent-level rows for every named question, inside the run's own survey boundary."""
    with _sql_engine().connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        scope = _resolve_authorized_scope(conn)
        question_rows = conn.execute(
            text(_QUESTION_SCOPE_SQL),
            {f"{_SCOPE_PARAM_PREFIX}question_ids": [qid.lower() for qid in question_ids]},
        ).mappings().all()
        question_surveys = {
            str(row["question_id"]).lower(): str(row["survey_id"]).lower()
            for row in question_rows
        }
        for question_id in question_ids:
            question_survey = question_surveys.get(question_id.lower())
            if question_survey is None:
                raise ValueError(f"Unknown question_id: {question_id}.")
            if question_survey not in scope["authorized_survey_ids"]:
                raise PermissionError(
                    f"Question {question_id} is outside the initial survey's client boundary."
                )
        if len(set(question_surveys.values())) > 1:
            raise ValueError("Every clustered question must belong to the same survey.")

        params: dict[str, Any] = {}
        for index, question_id in enumerate(question_ids):
            params[f"slot_{index}"] = index
            params[f"qid_{index}"] = question_id
        result = conn.execute(text(_cluster_rows_sql(len(question_ids))), params)
        return [dict(row) for row in result.mappings().all()]


def _cluster_question_values(
    slot_rows: list[dict[str, Any]], question_id: str
) -> tuple[str, str, dict[str, float], dict[str, str]]:
    """One question's label, scale, respondent values and their products."""
    first = slot_rows[0]
    question_type = str(first.get("question_type") or "")
    if question_type != "vertical-rating":
        raise ValueError(cluster_unsupported_type(question_id, question_type))

    values: dict[str, float] = {}
    products: dict[str, str] = {}
    for row in slot_rows:
        enrollment_id = row.get("enrollment_id")
        if not enrollment_id:
            continue
        try:
            number = float(row.get("raw_value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        enrollment_id = str(enrollment_id)
        if enrollment_id in values:
            # One profile per respondent: a second rating means they evaluated more than one
            # product, which is a grain this version does not split.
            raise ValueError(cluster_multi_product(question_id))
        values[enrollment_id] = number
        products[enrollment_id] = str(row.get("product_name") or "(no product)")
    return _variable_name(first, None), str(first.get("scale") or "?"), values, products


class ClusterInput(BaseModel):
    question_ids: list[str] = Field(
        ...,
        description=(
            "Resolved UUIDs of two or more attribute-level numeric rating questions, all on the "
            "same scale. Never include an overall/summary measure here"
        ),
    )
    k: int | None = Field(
        None,
        description=(
            "Number of clusters. Omit unless the user named a number -- omitting it fits the "
            "default of 3 and records that the count was not theirs"
        ),
    )
    overall_question_id: str = Field(
        "",
        description=(
            "Optional UUID of the overall/summary liking question. Excluded from the clustering "
            "basis and reported as a mean per cluster"
        ),
    )

    @field_validator("question_ids")
    @classmethod
    def _strip_question_ids(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value]

    @field_validator("overall_question_id")
    @classmethod
    def _strip_overall(cls, value: str) -> str:
        return value.strip()


# Description: tool_prompts.CLUSTER_RATING_PROFILES_DESCRIPTION, assigned below.
def cluster_rating_profiles(
    question_ids: list[str],
    k: int | None = None,
    overall_question_id: str = "",
) -> str:
    """Cluster respondents on the shape of their ratings across several attribute questions."""
    # None means the user named no cluster count, which the tool arguments now record; the fit
    # still runs at the default. EXPECTED_OUTPUT requires an assumed default to be stated in the
    # answer, and the model can only state it if the payload says which of the two happened.
    k_source = "default" if k is None else "requested"
    if k is None:
        k = DEFAULT_K
    # Sorted, not caller-ordered: the basis questions are the matrix columns, and k-means++ seeds
    # its draws from the data layout, so the same five questions listed in a different order
    # otherwise settle on a different one of several near-tied optima. Observed live -- the model
    # named them aroma-first where a check had named them appearance-first, and the partition
    # moved. Row order is canonicalised inside clustering.fit_kmeans for the same reason.
    basis_ids = sorted({question_id for question_id in question_ids})
    if len(basis_ids) < 2:
        return cluster_too_few_questions()
    if not _MIN_CLUSTER_K <= k <= _MAX_CLUSTER_K:
        return cluster_bad_k(k)

    requested = basis_ids + ([overall_question_id] if overall_question_id else [])
    invalid_ids = [qid for qid in requested if not _UUID_RE.match(qid)]
    if invalid_ids:
        return cluster_bad_question_id(invalid_ids)
    if overall_question_id in basis_ids:
        return cluster_summary_measure(overall_question_id, "overall_question_id")
    if not _scope_published():
        return "Clustering unavailable: the run's survey scope is not established."

    try:
        rows = _fetch_cluster_rows(requested)
    except PermissionError as exc:
        print(f"[cluster] REFUSED: {exc}")
        return f"Clustering refused: {exc}"
    except ValueError as exc:
        return f"Clustering unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 - surfaced back to the LLM, not raised
        print(f"[cluster] failed: {exc}")
        return f"Clustering failed: {exc}"

    by_slot: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_slot.setdefault(int(row["slot"]), []).append(row)

    labels: list[str] = []
    scales: list[str] = []
    value_maps: list[dict[str, float]] = []
    products: dict[str, str] = {}
    survey_id = ""
    try:
        for index, question_id in enumerate(requested):
            slot_rows = by_slot.get(index) or []
            if not slot_rows or not slot_rows[0].get("question_id"):
                return f"Clustering unavailable: unknown question UUID {question_id}."
            label, scale, values, slot_products = _cluster_question_values(slot_rows, question_id)
            survey_id = survey_id or str(slot_rows[0].get("survey_id") or "")
            if index < len(basis_ids) and _is_summary_measure(label):
                return cluster_summary_measure(question_id, label)
            labels.append(label)
            scales.append(scale)
            value_maps.append(values)
            products.update(slot_products)
    except ValueError as exc:
        return str(exc)

    basis_scales = sorted(set(scales[: len(basis_ids)]))
    if len(basis_scales) > 1:
        return cluster_mixed_scales(basis_scales)

    respondents = set(value_maps[0])
    for values in value_maps[1:]:
        respondents &= set(values)
    complete = sorted(respondents)
    answered_any = len(set().union(*(set(values) for values in value_maps)))
    if len(complete) < _MIN_CLUSTER_RESPONDENTS:
        return cluster_no_data(len(complete))
    if k >= len(complete):
        return cluster_bad_k(k, len(complete))

    matrix = np.array(
        [[value_maps[slot][entity] for slot in range(len(basis_ids))] for entity in complete],
        dtype=float,
    )
    companion = None
    if overall_question_id:
        companion = {
            "label": "overall_liking_mean",
            "values": np.array([value_maps[-1][entity] for entity in complete], dtype=float),
        }

    payload = segment_profiles(
        matrix,
        labels[: len(basis_ids)],
        k=k,
        seed=DEFAULT_SEED,
        companion=companion,
        products=[products.get(entity, "(no product)") for entity in complete],
    )
    payload.update({
        "status": "ok",
        "method": "kmeans_on_standardized_ratings",
        "k_source": k_source,
        "grain": "respondent",
        "survey_id": survey_id,
        "scale": basis_scales[0],
        "basis_questions": [
            {"question_id": question_id, "label": label}
            for question_id, label in zip(basis_ids, labels, strict=False)
        ],
        "overall_question": (
            {"question_id": overall_question_id, "label": labels[-1]}
            if overall_question_id else None
        ),
        "respondents_excluded_incomplete": answered_any - len(complete),
    })
    return CLUSTER_RESULT_HEAD + "\n" + json.dumps(payload, ensure_ascii=False)


cluster_rating_profiles.__doc__ = CLUSTER_RATING_PROFILES_DESCRIPTION
cluster_rating_profiles = tool(args_schema=ClusterInput)(cluster_rating_profiles)



# Word-cloud counts are code-owned. The model selects a question from the inventory; it never
# receives raw verbatims, writes tokenization SQL, chooses stop words, or serializes chart data.
_WORD_CLOUD_ARTIFACT_PREFIX = "__FW_WORD_CLOUD_ARTIFACT_V1__"
_WORD_CLOUD_BLOCK_RE = re.compile(
    r"```gpi-word-cloud\s*.*?```"
    r"|```gpi-chart\s*(?=[^`]*\"type\"\s*:\s*\"word[_-]cloud\").*?```",
    re.IGNORECASE | re.DOTALL,
)
_WORD_CLOUD_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", re.IGNORECASE)
_WORD_CLOUD_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_WORD_CLOUD_STOP_WORDS = frozenset("""
    a about above after again against all am an and any are aren't as at be because been before
    being below between both but by can can't cannot could couldn't did didn't do does doesn't
    doing don't down during each few for from further had hadn't has hasn't have haven't having
    he he'd he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm i've
    if in into is isn't it it's its itself just like liked dislike disliked me more most my myself
    no nor not now of off on once only or other ought our ours ourselves out over own really same
    she she'd she'll she's should shouldn't so some such than that that's the their theirs them
    themselves then there there's these they they'd they'll they're they've this those through to
    too under until up very was wasn't we we'd we'll we're we've were weren't what what's when
    when's where where's which while who who's whom why why's with won't would wouldn't you you'd
    you'll you're you've your yours yourself yourselves also quite extremely much many don doesn
    didn hadn hasn haven isn shouldn wasn weren wouldn
""".split())
_WORD_CLOUD_GENERIC_SUBJECT_TERMS = frozenset({
    "product", "products", "sample", "samples", "concept", "concepts",
})

_WORD_CLOUD_QUESTION_SQL = """
SELECT q.id::text AS question_id,
       q."surveyId"::text AS survey_id,
       q.prompt,
       q."typeOfQuestion" AS question_type,
       q.language,
       s.title AS survey_title,
       s.benchmark_category_label AS category
FROM question q
JOIN survey s ON s.id = q."surveyId"
WHERE q.id = CAST(:question_id AS uuid)
"""

_WORD_CLOUD_ANSWERS_SQL = """
SELECT a.id::text AS answer_id,
       a.value AS answer_value,
       aqo."answerData" ->> 'optionAnswer' AS option_answer,
       p.name AS product_name
FROM answer a
LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
LEFT JOIN product p ON p.id = a.product_id
                   AND p."surveyId" = CAST(:survey_id AS uuid)
WHERE a.question_id = CAST(:question_id AS uuid)
ORDER BY a.id, aqo.id
"""


class WordCloudInput(BaseModel):
    question_id: str = Field(
        ...,
        description="UUID of one open-answer or multiple-open-answer question from the inventory",
    )
    group_by_product: bool = Field(
        False,
        description="True only when the user explicitly requests separate product word clouds",
    )
    max_terms: int = Field(
        50,
        ge=30,
        le=60,
        description="Maximum terms in the flat cloud or in each product cloud (30-60)",
    )


def _word_cloud_display_text(value: Any) -> str:
    """Keep prompts single-line and prevent survey piping braces becoming suggestion widgets."""
    text_value = " ".join(str(value or "").split())
    return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", r"[\1]", text_value)


def _word_cloud_tokens(value: str, subject_terms: set[str]) -> set[str]:
    """Return the distinct eligible terms contributed by one answer component."""
    redacted = _WORD_CLOUD_EMAIL_RE.sub(" ", value)
    redacted = _WORD_CLOUD_URL_RE.sub(" ", redacted)
    normalized = unicodedata.normalize("NFKC", redacted).casefold()
    letters_only = "".join(
        character if unicodedata.category(character).startswith("L") else " "
        for character in normalized
    )
    return {
        token for token in letters_only.split()
        if len(token) >= 3
        and token not in _WORD_CLOUD_STOP_WORDS
        and token not in subject_terms
    }


def _fetch_word_cloud_source(question_id: str) -> dict[str, Any]:
    """Fetch one authorized question and all of its text components without exposing them."""
    with _sql_engine().connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        scope = _resolve_authorized_scope(conn)
        question = conn.execute(
            text(_WORD_CLOUD_QUESTION_SQL), {"question_id": question_id}
        ).mappings().first()
        if question is None:
            raise ValueError("no question exists for that id")
        survey_id = str(question["survey_id"]).lower()
        if survey_id not in scope["authorized_survey_ids"]:
            raise PermissionError("the selected question is outside the authorized survey boundary")
        question_type = str(question["question_type"] or "")
        if question_type not in {"open-answer", "multiple-open-answer"}:
            raise ValueError(
                f"the selected question has type {question_type!r}, not an open-ended type"
            )
        rows = conn.execute(text(_WORD_CLOUD_ANSWERS_SQL), {
            "question_id": question_id,
            "survey_id": survey_id,
        }).mappings().all()
    return {
        "question_id": str(question["question_id"]),
        "survey_id": str(question["survey_id"]),
        "prompt": str(question["prompt"] or "Open-ended responses"),
        "question_type": question_type,
        "language": str(question["language"] or ""),
        "survey_title": str(question["survey_title"] or "Survey"),
        "category": str(question["category"] or ""),
        "rows": [dict(row) for row in rows],
    }


def _rank_word_cloud_terms(counts: Counter[str], max_terms: int) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:max_terms]
        if count > 0
    ]


def _build_word_cloud_artifact(
    source: dict[str, Any], *, group_by_product: bool, max_terms: int
) -> dict[str, Any]:
    """Aggregate source rows and serialize the exact chart artifact deterministically."""
    question_type = source["question_type"]
    answers: dict[str, dict[str, Any]] = {}
    product_terms: set[str] = set()
    for row in source["rows"]:
        answer_id = str(row["answer_id"])
        record = answers.setdefault(answer_id, {"texts": [], "product": None})
        product = str(row.get("product_name") or "").strip()
        if product:
            record["product"] = product
            product_terms.update(_word_cloud_tokens(product, set()))
        raw_value = row.get("answer_value") if question_type == "open-answer" \
            else row.get("option_answer")
        if raw_value is not None and str(raw_value).strip():
            record["texts"].append(str(raw_value))

    category_terms = _word_cloud_tokens(str(source.get("category") or ""), set())
    subject_terms = set(_WORD_CLOUD_GENERIC_SUBJECT_TERMS) | category_terms | product_terms
    eligible = [
        record for record in answers.values()
        if record["texts"] and (not group_by_product or record["product"])
    ]
    if not eligible:
        if group_by_product and any(record["texts"] for record in answers.values()):
            raise ValueError("the open-ended answers are not linked to products")
        raise ValueError("the selected question has no non-blank answers")

    if group_by_product:
        grouped_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for record in eligible:
            terms: set[str] = set()
            for component in record["texts"]:
                terms.update(_word_cloud_tokens(component, subject_terms))
            grouped_counts[record["product"]].update(terms)
        data = [
            {"group": product, "values": _rank_word_cloud_terms(grouped_counts[product], max_terms)}
            for product in sorted(grouped_counts, key=str.casefold)
            if grouped_counts[product]
        ]
    else:
        counts: Counter[str] = Counter()
        for record in eligible:
            terms: set[str] = set()
            for component in record["texts"]:
                terms.update(_word_cloud_tokens(component, subject_terms))
            counts.update(terms)
        data = _rank_word_cloud_terms(counts, max_terms)

    if not data:
        raise ValueError("no eligible terms remain after deterministic filtering")

    prompt = _word_cloud_display_text(source["prompt"])
    payload = {
        "version": 1,
        "type": "word_cloud",
        "title": "Most common words by product" if group_by_product else "Most common words",
        "subtitle": prompt,
        "survey_id": source["survey_id"],
        "question_id": source["question_id"],
        "data": data,
        "settings": {"display": {"bigrams": False}},
        "meta": {
            "answer_count": len(answers),
            "filtered_answer_count": len(eligible),
            "count_method": "answers_containing_term",
        },
    }
    artifact = "```gpi-chart\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    ) + "\n```"
    return {
        "artifact": artifact,
        "intro": f'Word cloud for “{prompt}” (N = {len(eligible)} non-blank answers).',
        "question": prompt,
        "filtered_answer_count": len(eligible),
        "grouped": group_by_product,
        "payload": payload,
    }


def generate_word_cloud(
    question_id: str,
    group_by_product: bool = False,
    max_terms: int = 50,
) -> str:
    """Build one deterministic word-cloud artifact from an authorized open-ended question."""
    if not _UUID_RE.match(question_id):
        return word_cloud_bad_question_id(question_id)
    if not _scope_published():
        return WORD_CLOUD_SCOPE_UNAVAILABLE
    try:
        source = _fetch_word_cloud_source(question_id.lower())
        result = _build_word_cloud_artifact(
            source, group_by_product=group_by_product, max_terms=max_terms
        )
    except PermissionError as exc:
        print(f"[word-cloud] REFUSED: {exc}")
        return word_cloud_question_unavailable(str(exc))
    except ValueError as exc:
        return word_cloud_question_unavailable(str(exc))
    except Exception as exc:  # noqa: BLE001 - fail closed without exposing raw response text
        print(f"[word-cloud] database/aggregation failure: {exc}")
        return word_cloud_question_unavailable("database retrieval or aggregation failed")
    return _WORD_CLOUD_ARTIFACT_PREFIX + json.dumps(result, ensure_ascii=False)


generate_word_cloud.__doc__ = GENERATE_WORD_CLOUD_DESCRIPTION
generate_word_cloud = tool(args_schema=WordCloudInput)(generate_word_cloud)


def build_tools():
    return [
        nl2sql_tool,
        get_survey_analysis_packet,
        generate_word_cloud,
        run_survey_stats,
        analyze_plsr,
        cluster_rating_profiles,
    ]

tools = build_tools()
tools_by_name = {t.name: t for t in tools}
_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_TOOL_CALLS,
    thread_name_prefix="flavorai-tool",
)



#=========== Defining the underlying LLM ========================
# Turn 0 is where this agent's time goes: measured 11.0-25.0s on byte-identical input, 60-85% of
# total agent time, emitting ~1,100-1,300 output tokens while its ~10.3k input runs 96% cached.
# At ~10-20 ms per output token, generation IS the latency, so the levers are "write fewer
# tokens" (the ledger cap and the SQL FORMATTING rule) and reasoning effort.
#
# Every turn uses one model and one reasoning profile. Tool availability, not model identity,
# is the only distinction on the final step-budget turn.
llm = ChatOpenAI(
            model=MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            api_key=OPENAI_API_KEY,
            **_LLM_TUNING
        )

model = llm.bind_tools(tools=tools, parallel_tool_calls=True)

_MODEL_TIMING_LOCK = threading.Lock()
_ACTIVE_MODEL_STARTED: float | None = None
_LAST_MODEL_TIMING: dict[str, float] = {}


def model_timing_snapshot() -> dict[str, float]:
    """Return model-call timing markers for the serialized API run."""
    with _MODEL_TIMING_LOCK:
        snapshot = dict(_LAST_MODEL_TIMING)
        if _ACTIVE_MODEL_STARTED is not None:
            snapshot["active_started_at"] = _ACTIVE_MODEL_STARTED
        return snapshot

#=========== Defining Auxillary Functions ========================
def _normalize_tool_args(args: Any):
    """
    Tool-call args may arrive as:
      - dict with "__arg1" (common for single-input tools)
      - dict with named fields (args_schema tools)
      - raw string
    Normalize so `.invoke(...)` works reliably.
    """
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        if "__arg1" in args:
            return args["__arg1"]
        return args
    return args


_PROGRESS_OPEN = "<progress_gathered>"
_PROGRESS_CLOSE = "</progress_gathered>"
_PROGRESS_BLOCK_RE = re.compile(
    r"<progress_gathered>.*?</progress_gathered>", re.IGNORECASE | re.DOTALL
)


def _raw_message_to_text(msg: BaseMessage) -> str:
    """Return message text across providers, including internal progress memory."""
    c: Any = getattr(msg, "content", "")

    if isinstance(c, str):
        return c.strip()

    if isinstance(c, list):
        chunks = []
        for part in c:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                if "text" in part and isinstance(part["text"], str):
                    chunks.append(part["text"])
                elif part.get("type"):
                    # A structured Responses-API block, not prose: `reasoning` (opaque
                    # encrypted_content) or `function_call` (the tool call, which
                    # print_message already renders from msg.tool_calls). str()-ing these
                    # dumps a KB of base64 or a raw arguments blob into the transcript.
                    # A typed block is never the answer text, so skip it; anything without
                    # a type still falls through to str() below.
                    continue
                else:
                    chunks.append(str(part))
            else:
                chunks.append(str(part))
        return "\n".join(x.strip() for x in chunks if x and x.strip())

    return str(c).strip()


def strip_progress_gathered(value: str) -> str:
    """Remove internal progress memory from completed user-visible text.

    A dangling opening tag suppresses the remainder. That fail-closed behavior keeps a malformed
    internal block out of API and CLI output while leaving the checkpointed AIMessage untouched.
    """
    cleaned = _PROGRESS_BLOCK_RE.sub("", str(value or ""))
    cleaned = re.sub(
        r"<progress_gathered>.*\Z", "", cleaned, flags=re.IGNORECASE | re.DOTALL
    )
    cleaned = re.sub(
        r"</?progress_gathered>", "", cleaned, flags=re.IGNORECASE
    )
    return cleaned.strip()


def message_to_text(msg: BaseMessage) -> str:
    """Return public text, excluding internal progress-memory blocks."""
    return strip_progress_gathered(_raw_message_to_text(msg))


def _partial_tag_suffix(value: str, tag: str) -> int:
    lowered = value.lower()
    tag = tag.lower()
    for size in range(min(len(value), len(tag) - 1), 0, -1):
        if lowered.endswith(tag[:size]):
            return size
    return 0


class ProgressDeltaStreamFilter:
    """Incrementally remove progress blocks even when tags span streamed chunks."""

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, value: str) -> str:
        self._buffer += str(value or "")
        visible: list[str] = []
        while self._buffer:
            lowered = self._buffer.lower()
            if self._inside:
                close_at = lowered.find(_PROGRESS_CLOSE)
                if close_at >= 0:
                    self._buffer = self._buffer[close_at + len(_PROGRESS_CLOSE):]
                    self._inside = False
                    continue
                keep = _partial_tag_suffix(self._buffer, _PROGRESS_CLOSE)
                self._buffer = self._buffer[-keep:] if keep else ""
                break

            open_at = lowered.find(_PROGRESS_OPEN)
            if open_at >= 0:
                visible.append(self._buffer[:open_at])
                self._buffer = self._buffer[open_at + len(_PROGRESS_OPEN):]
                self._inside = True
                continue
            keep = _partial_tag_suffix(self._buffer, _PROGRESS_OPEN)
            if keep:
                visible.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            else:
                visible.append(self._buffer)
                self._buffer = ""
            break
        return "".join(visible)

    def finish(self) -> str:
        """Flush visible buffered text; discard a malformed unclosed internal block."""
        visible = "" if self._inside else self._buffer
        self._buffer = ""
        self._inside = False
        return visible


class BraceDeltaStreamFilter:
    """Hold a brace-delimited span until its closing brace has streamed.

    Chart payloads are JSON and commonly arrive over many token events.  Emitting the
    opening brace immediately lets clients attempt to parse incomplete JSON, so keep
    the complete balanced span atomic while leaving surrounding prose streaming.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._depth = 0

    def feed(self, value: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        if not self._depth:
            start = text.find("{")
            if start < 0:
                return text
            prefix = text[:start]
            self._buffer = text[start:]
            self._depth = self._buffer.count("{") - self._buffer.count("}")
            if self._depth <= 0:
                complete, self._buffer = self._buffer, ""
                self._depth = 0
                return prefix + complete
            return prefix

        self._buffer += text
        self._depth += text.count("{") - text.count("}")
        if self._depth > 0:
            return ""
        complete = self._buffer
        self._buffer = ""
        self._depth = 0
        # A chunk may contain prose after the closing brace; feed that tail normally.
        close = complete.rfind("}")
        return complete[:close + 1] + self.feed(complete[close + 1:])

    def finish(self) -> str:
        """Discard an incomplete span when the streamed turn ends."""
        self._buffer = ""
        self._depth = 0
        return ""


# First line of the pre-fetched inventory block, taken from the preamble itself so the two
# cannot drift apart. Everything from here to the end of the user turn is survey-invariant
# boilerplate that would otherwise bury the transcript.
_INVENTORY_LOG_MARKER = INVENTORY_PREAMBLE.strip().splitlines()[0]


def _clip_for_log(text: str, limit: int) -> str:
    """Shorten one printed block. Printing only -- the model still gets the whole value."""
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [log-truncated: {limit} of {len(text)} chars]"


def print_message(msg: BaseMessage) -> None:
    """Print one message with a role label, splitting tool calls from text."""
    kind = {"human": "HUMAN MESSAGE", "system": "SYSTEM MESSAGE",
            "ai": "AI MESSAGE", "tool": "TOOL OUTPUT"}.get(msg.type, msg.type.upper())

    text_part = message_to_text(msg)
    tool_calls = getattr(msg, "tool_calls", None) or []

    if msg.type == "tool":
        print(f"\n===== TOOL OUTPUT ({msg.name}) =====")
        print(_clip_for_log(text_part, LOG_TOOL_OUTPUT_CHARS))
        return

    if msg.type == "human":
        cut = text_part.find(_INVENTORY_LOG_MARKER)
        if cut != -1:
            text_part = (
                text_part[:cut].rstrip()
                + f"\n[pre-fetched inventory elided from log: {len(text_part) - cut} chars]"
            )

    print(f"\n===== {kind} =====")
    print(text_part if text_part else "(no text — tool call only)")

    for tc in tool_calls:
        print(f"\n===== TOOL CALL ({tc['name']}) =====")
        args = tc.get("args")
        rendered = args if isinstance(args, str) else json.dumps(
            args, indent=2, ensure_ascii=False
        )
        print(_clip_for_log(rendered, LOG_TOOL_CALL_CHARS))




#=========== Defining Agent State and System Prompt ========================
# ----------------------------
# Graph state
# ----------------------------
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    # Conversation anchor used by the API before resuming a checkpoint. It prevents one
    # thread from retaining survey A's inventory while SQL is rebound to survey B.
    thread_survey_id: NotRequired[str]
    inventory_attached: NotRequired[bool]
    inventory_chars: NotRequired[int]
    number_of_steps: int
    # Query/result fingerprints from the previous SQL batch in the active user round.
    # Progress memory itself lives append-only in persisted AIMessage content.
    last_sql_observations: dict[str, str]
    # Consecutive SQL batches in which every call returned 0 rows. Two in a row means the query
    # *shape* is wrong, not its filters, so the guardrail stops asking for another variant.
    zero_row_streak: int
    # A generate_word_cloud result is kept outside model-authored text. call_model short-circuits
    # on these fields and returns the deterministic introduction plus trusted artifact directly.
    word_cloud_artifact: NotRequired[str]
    word_cloud_intro: NotRequired[str]
    # Trusted PCA artifacts stay outside model-authored text until final assembly.
    pca_chart_artifacts: NotRequired[list[dict[str, Any]]]



#=========== Building Based System Prompt ========================
# The system prompt, SCOPE preamble and SCOPE_REF are in agent_instructions.py.
# scoped_query() is imported at the top of this file and re-exported for the API wrapper.
#
# Why the ids are not in the system prompt: they used to be interpolated into
# `goal`/`backstory`, which sit BEFORE the ~7k-token schema block -- so the cached prefix
# diverged at char ~79 and the whole schema was re-sent uncached for every survey. They go
# in the user turn instead, which keeps the system block byte-identical across surveys.


#=========== Pre-fetched survey inventory ========================
# Measured: every run opened with the same boilerplate inventory query -- id / prompt / type /
# scale, then a second query for per-product aggregates. That first query is not a decision, it
# is a fixed prelude, and the model paid a full strong turn (5.7-7.8s, 325-470 output tokens) to
# regenerate it every time. Running it here instead collapsed 21 LLM turns to 7 on the needle
# set and 67 to 48 across regression.xlsx, with no accuracy loss on either.
#
# Two things this is NOT: it is not a token optimisation (input fell 47% on the needle set but
# ROSE 8% on benchmark-comparison prompts, where the payload is large and the benchmark survey
# still needs its own packet; net -6%), and it is not a substitute for SQL -- nl2sql_tool stays
# bound for survey discovery and aggregates the packets do not contain.
#
# The payload goes in the USER turn, after the cacheable system prefix, so it never disturbs the
# ~98% prompt-cache hit rate on the system block.
# Section A: numeric, product-attributable measures, already aggregated per product.
# prompt, type and scale are NOT repeated here -- the catalog below is their single source, and
# it covers every question rather than only the numeric ones. Section A keeps qid plus the
# aggregates, columnar. Measured on the largest survey: 60,630 chars -> 25,030.
# The scale metadata that used to live here as `scale_points` moved into the catalog under the
# name `labelled_positions`, which is what the value actually counts; see the catalog comment.
# It reads question_option."optionSettings"->'positionLabels', NOT
# question.settings->'positionLabels'. The latter was used here and is wrong: across all 9,138
# question rows in this database, ZERO have a 'positionLabels' key in settings, so
# jsonb_array_length(...) was NULL and every inventory ever built reported scale_points=null.
# 7,032 question_option rows do carry it, as an array of {label, position} with position 0-based.
# The value is consistent across the options of one question (checked: 1 distinct length per
# question), so picking one array per question is a safe reducer rather than a guess.
#
# attributes_pooled / by_attribute exist because a "measure" here is often not one measure. On
# the default survey, `Appearance` is FOUR line-scale attributes (Color Intensity, Color
# Uniformity, Surface Gloss, Visual Freshness) and `Texture` is five, so the per-product mean
# averaged across all of them and the breakdown was invisible -- the analyst saw one number per
# product and ranked on it. That is not cosmetic: pooled Appearance ranks Product 2 > 3 > 1,
# while Color Intensity alone ranks 3 (10.00) > 2 (9.38) > 1 (8.59). The pooled figure inverts
# the top two. Several of these attributes are also INTENSITY scales, where a higher value is
# not better, so averaging them with a freshness/liking attribute yields a number with no
# direction at all.
#
# Two details the obvious implementation gets wrong:
#   1. The sub-item is COALESCE(matrix_row_option_id, question_option_id). For a line-scale
#      battery the attribute IS question_option_id, but for a matrix the value lives there and
#      the ROW (the statement) is matrix_row_option_id -- keying on question_option_id alone
#      would report scale points as if they were attributes. LEFT JOIN, so an option row that
#      does not resolve cannot silently drop the value.
#   2. attributes_pooled is guarded by COUNT(score) > COUNT(DISTINCT enrollment_id). Distinct
#      labels ALONE is a false positive: a single-select question ("How many stores did you
#      visit", options 1-4) has four distinct labels but ONE value per respondent, and was
#      flagged as 4 pooled attributes before the guard. Pooling means several values per
#      respondent, so that is what gets tested.
# by_attribute is omitted (NULL) when attributes_pooled = 1, which is 79 of 80 measures on the
# largest survey -- so the cost is paid only where there is something to disclose. Measured
# section-A growth: 1.06x on the largest survey (56,975 -> 60,655 chars), 1.10x on Herbalife,
# 4.11x on the small default survey but that is 1,168 -> 4,798 chars in absolute terms. DB-wide
# worst case is 28 sub-items on one question, so INVENTORY_MAX_CHARS is never threatened and no
# truncation is needed -- which matters, because a silently dropped attribute is the same
# evidence-destroying failure this file guards against everywhere else.
# order_differs was added after by_attribute alone proved insufficient. Told to "name every
# attribute whose product order differs", the model would not do the comparison: across repeated
# runs on the Gpiexperience sensory survey it either ranked on the pooled mean anyway or promised
# to "check the attribute-level orders as well" without checking any. The comparison is exact
# arithmetic over data already in the CTE, so Postgres does it: order_differs lists precisely the
# attributes whose product ordering disagrees with the pooled ordering (7 of 13 on that survey,
# 495 of 1,155 attribute blocks DB-wide). The key is CONCATENATED in only when attributes_pooled
# > 1, so a survey with nothing pooled -- 80 of 80 measures on the largest one -- pays 0 extra
# bytes, and the prompt loses a derivation instead of gaining one.
# order_differs is a means comparison, NOT a significance test, and the two disagree here: the
# Charts API returns anova/tukey per sub-attribute from a single call on the pooled question_id
# (verified live -- one call on `Appearance` reports all four attributes separately), and on this
# survey every attribute comes back non-significant, all products in Tukey group a, p=0.29-0.37.
# So all 7 flagged flips are noise-level reorderings. That is why the preamble sends the model to
# that one call rather than letting a flip stand as a finding, and why it says no per-attribute id
# exists: an earlier run invented one, telling the reader to "test that construct instead" when
# the pooled call already had the per-attribute verdict.
# Ordering is now tie-broken on product name in three places (by_product, by_attribute, and the
# two arrays compared for flips). Without it the tie order came from the query plan: `Fresh Aroma`
# has Product 1 and Product 3 both at 9.41, and one survey has EIGHT products tied at 9.0, so
# restructuring the CTE silently reshuffled them. Same rows and same values either way, but a
# deterministic order is what makes the flip comparison trustworthy and the payload cacheable.
# Verified over all 132 surveys holding a scored product measure: every non-pooled measure is
# byte-identical to the previous query except that one 8-way tie, output is stable across repeat
# runs, and total time went from 1,297ms to 1,239ms because attr_m is now aggregated once and
# shared rather than recomputed.
# The pooled by_product block is otherwise unchanged, byte for byte, on all three surveys checked.
# Keep the three inventory sections in one statement: PostgreSQL gives the products, Section A
# and the catalog one statement-level snapshot, so the two measure views cannot disagree about
# which questions the survey has.
_INV_SQL = (
"""WITH scored AS MATERIALIZED (
  SELECT q.id qid,q.prompt,q."typeOfQuestion" qtype,p.name product,
         COALESCE(qo.label,'(unlabelled)') attr,
         (aqo."answerData"->>'optionAnswer')::numeric score,a.enrollment_id,
         e.enrollment_status estatus
  FROM question q JOIN answer a ON a.question_id=q.id
  JOIN enrollment e ON e.id=a.enrollment_id
  JOIN answered_question_options aqo ON aqo.answer_id=a.id
  JOIN product p ON p.id=a.product_id
  LEFT JOIN question_option qo
         ON qo.id=COALESCE(aqo.matrix_row_option_id,aqo.question_option_id)
  WHERE q."surveyId"=:survey_id
    AND aqo."answerData"->>'optionAnswer' ~ '^-?[0-9]+(\\.[0-9]+)?$'
), per AS (
  SELECT qid,prompt,qtype,product,COUNT(score) n,COUNT(DISTINCT enrollment_id) r,
         COUNT(DISTINCT CASE WHEN estatus='completed' THEN enrollment_id END) rc,
         ROUND(AVG(score),2) mean,ROUND(STDDEV_SAMP(score),2) sd,MIN(score) lo,MAX(score) hi,
         CASE WHEN COUNT(score)>COUNT(DISTINCT enrollment_id)
              THEN COUNT(DISTINCT attr) ELSE 1 END na
  FROM scored GROUP BY qid,prompt,qtype,product
), attr_m AS (
  SELECT qid,attr,product,COUNT(score) n,ROUND(AVG(score),2) mean,
         ROUND(STDDEV_SAMP(score),2) sd
  FROM scored GROUP BY qid,attr,product
), attr_agg AS (
  SELECT qid,jsonb_agg(jsonb_build_array(attr,product,n,mean,sd)
           ORDER BY attr,mean DESC,product) ja
  FROM attr_m GROUP BY qid
), flips AS (
  SELECT a.qid,jsonb_agg(a.attr ORDER BY a.attr) fa
  FROM (SELECT qid,attr,array_agg(product ORDER BY mean DESC,product) ao
        FROM attr_m GROUP BY qid,attr) a
  JOIN (SELECT qid,array_agg(product ORDER BY mean DESC,product) po
        FROM per GROUP BY qid) p ON p.qid=a.qid
  WHERE a.ao <> p.po GROUP BY a.qid
), scored_summary AS (
SELECT jsonb_build_object(
  'columns',jsonb_build_array('qid','attributes_pooled','observed','by_product',
    'by_attribute','order_differs'),
  'by_product_columns',jsonb_build_array('product','n','respondents','completed','mean','sd'),
  'by_attribute_columns',jsonb_build_array('attribute','product','n','mean','sd'),
  'rows',jsonb_agg(x ORDER BY prompt,qid)) AS measures FROM (
  SELECT per.prompt,per.qid,jsonb_build_array(per.qid,MAX(na),
         jsonb_build_array(MIN(lo),MAX(hi)),
         jsonb_agg(jsonb_build_array(product,n,r,rc,mean,sd) ORDER BY mean DESC,product),
         CASE WHEN MAX(na)>1 THEN aa.ja END,
         CASE WHEN MAX(na)>1 THEN COALESCE(f.fa,'[]'::jsonb) END) x
  FROM per LEFT JOIN attr_agg aa ON aa.qid=per.qid
             LEFT JOIN flips f ON f.qid=per.qid
  GROUP BY per.qid,per.prompt,aa.ja,f.fa) y
),
"""

# The catalog REPLACES the former Section B (`other_answered_measures`), which existed for a
# measured reason -- an inventory that hides evidence is worse than no inventory, because a
# measure removed upstream is indistinguishable from a measure that does not exist -- but which
# decided visibility by AUTHORING TYPE rather than by what the measure is. It filtered labels on
# `aqo."answerData" ? 'optionLabel'`, and multiple-choice / multiple-open-answer never set that
# key: 1,120 questions DB-wide reported sample_option_labels NULL while their labels sat in
# question_option. A NULL label field reads as "this measure has no labels", not "not fetched",
# so the model reported hidden KPIs as unavailable rather than querying for them. On Herbalife
# that hid 8 product KPIs -- recommendation intent, expectation fit and six JAR descriptors --
# while its 6 numeric hedonics came through Section A.
#
# Measured over the 330 surveys holding response data: questions visible 1,438 -> 2,651, and
# questions carrying named option labels 55 -> 1,738.
#
# Section B's two count fields are carried forward unchanged, names included, because the
# preamble teaches the model what they mean: COUNT(*) remains the legacy expanded value-row
# count (an open answer contributes one row, an option-backed answer several) and `submissions`
# separately counts answer records. The LEFT JOIN to answered_question_options is what keeps
# open-answer questions -- an `answer` row but NO option row -- from vanishing; an inner join
# dropped every free-text question in the database.
#
# Encoding is columnar (`columns` + positional `rows`). Measured on the three largest catalogs,
# repeated JSON key names -- not the data -- were the dominant cost: row-oriented with ids ran
# 107,260-165,862 chars against 27,437-59,029 columnar. Ids and codes (component_id, `order`,
# analytical_value) are omitted; the workflow resolves those itself, where they are free.
#
# Four fields need their reasons recorded:
#   1. `answered` is COALESCE(respondents,0)>0 and configured-but-unanswered questions are
#      INCLUDED (289 of them, DB-wide, inside surveys that do have responses). An attribute
#      nobody answered must read as "configured, no responses" rather than "does not exist".
#      `info` questions are excluded -- 150 more -- because they are display copy, never a
#      measure, and can never carry an answer.
#   2. components vs response_categories are SEPARATE lists and a matrix keeps both. Collapsing
#      them confuses attributes with scale values: keyed on question_option_id alone a matrix
#      reports its scale points as if they were attributes. Roles come from configuration
#      (typeOfQuestion, then question_option.type for matrix row/column) because observed data
#      is absent for unanswered questions and thin for partly answered ones. Observed usage is
#      VALIDATION only: where a declared row was used as a value or a declared column as a row,
#      role_conflict flags it and the config role still stands -- flag, never silently pick.
#      Measured: 45 row-typed used as values, 34 column-typed used as rows, over 5 questions.
#      derived_from says which of the two rules actually ran.
#   3. `labelled_positions` is NOT the scale point count, which is why it is not called that.
#      It counts entries in optionSettings->'positionLabels', i.e. positions that carry a label.
#      Measured against settings->'settingSlider' on the 1,749 questions holding both, it
#      disagrees with slider_max-slider_min+1 on 923 (53%), and on 238 of the 534 ANSWERED such
#      questions the largest observed answer exceeds it -- an anchors-only 1-5 scale has 2
#      labelled positions. All 534 fall inside [slider_min, slider_max], so the slider carries
#      the range and the anchors carry polarity. Those anchors are the only polarity signal
#      there is: `analytical_value` and `order` both fail, because this database holds
#      Dislike Extremely=1..Like Extremely=9 AND Like Extremely=0..Dislike Extremely=8.
#   4. may_screen_out is not emitted separately: screen_out_actions IS NULL carries it. Both is
#      920 redundant chars on the largest survey. It is a question-level branching property from
#      active logic_rule rows, never a claim about which respondents were screened out --
#      enrollment_status holds only completed/active, so "screened out" and "abandoned" cannot
#      be separated.
#
# cused is restricted to matrix questions on purpose. It is the only consumer of the row/column
# distinction, and scanning every answered option row for every survey cost 155ms of the 742ms
# total on the largest survey.
"""cq AS (
  SELECT qu.id qid,qu.prompt,qu."typeOfQuestion" qtype,qu."sectionId" sect,qu.settings
  FROM question qu
  WHERE qu."surveyId"=:survey_id AND qu."typeOfQuestion"<>'info'
), cobs AS (
  SELECT a.question_id qid,COUNT(DISTINCT a.enrollment_id) resp,
         COUNT(DISTINCT CASE WHEN e.enrollment_status='completed'
                             THEN a.enrollment_id END) rcomp,
         COUNT(DISTINCT a.id) subs,COUNT(*) answers,
         bool_or(a.product_id IS NOT NULL) prod_linked
  FROM answer a JOIN enrollment e ON e.id=a.enrollment_id
  LEFT JOIN answered_question_options aqo ON aqo.answer_id=a.id
  WHERE a.question_id IN (SELECT qid FROM cq) GROUP BY a.question_id
), cused AS (
  SELECT opt_id,bool_or(as_row) used_row,bool_or(NOT as_row) used_col FROM (
    SELECT aqo.matrix_row_option_id opt_id,true as_row FROM answered_question_options aqo
    JOIN answer a ON a.id=aqo.answer_id
    WHERE a.question_id IN (SELECT qid FROM cq WHERE qtype='matrix')
      AND aqo.matrix_row_option_id IS NOT NULL
    UNION ALL
    SELECT aqo.question_option_id,false FROM answered_question_options aqo
    JOIN answer a ON a.id=aqo.answer_id
    WHERE a.question_id IN (SELECT qid FROM cq WHERE qtype='matrix')
      AND aqo.question_option_id IS NOT NULL) t
  GROUP BY opt_id
), copt AS (
  SELECT cq.qid,qo.id opt_id,qo.label,qo."order" ord,
         qo."optionSettings"->'positionLabels' pl,
         CASE WHEN cq.qtype='matrix' THEN CASE WHEN qo.type::text='column' THEN 'v' ELSE 'c' END
              WHEN cq.qtype IN ('multiple-choice','vertical-rating') THEN 'v'
              ELSE 'c' END role,
         CASE WHEN cq.qtype='matrix' AND qo.type::text='row' AND u.used_col AND NOT u.used_row
                THEN true
              WHEN cq.qtype='matrix' AND qo.type::text='column' AND u.used_row AND NOT u.used_col
                THEN true
              ELSE false END conflict
  FROM cq JOIN question_option qo ON qo.question_id=cq.qid
  LEFT JOIN cused u ON u.opt_id=qo.id
), clab AS (
  SELECT qid,role,label,MIN(ord) ord FROM copt
  WHERE NULLIF(BTRIM(label),'') IS NOT NULL GROUP BY qid,role,label
), clists AS (
  SELECT qid,
    jsonb_agg(label ORDER BY ord,label) FILTER (WHERE role='c') comps,
    jsonb_agg(label ORDER BY ord,label) FILTER (WHERE role='v') cats
  FROM clab GROUP BY qid
), cconf AS (
  SELECT copt.qid,bool_or(copt.conflict) conflict,
         bool_or(u.opt_id IS NOT NULL) observed
  FROM copt LEFT JOIN cused u ON u.opt_id=copt.opt_id
  JOIN cq ON cq.qid=copt.qid AND cq.qtype='matrix'
  GROUP BY copt.qid
), cpl AS (
  SELECT DISTINCT ON (qid) qid,pl FROM copt
  WHERE pl IS NOT NULL AND jsonb_array_length(pl)>0
  ORDER BY qid,jsonb_array_length(pl) DESC,opt_id
), cscale AS (
  SELECT cpl.qid,jsonb_array_length(pl) pts,
    (SELECT e.v->>'label' FROM jsonb_array_elements(cpl.pl) WITH ORDINALITY e(v,i)
      WHERE NULLIF(BTRIM(e.v->>'label'),'') IS NOT NULL ORDER BY e.i LIMIT 1) lo,
    (SELECT e.v->>'label' FROM jsonb_array_elements(cpl.pl) WITH ORDINALITY e(v,i)
      WHERE NULLIF(BTRIM(e.v->>'label'),'') IS NOT NULL ORDER BY e.i DESC LIMIT 1) hi
  FROM cpl
), cso AS (
  SELECT lr."sourceQuestionId" qid,jsonb_agg(DISTINCT lr."actionType"::text) acts
  FROM logic_rule lr
  WHERE lr."isActive" AND lr."actionType"::text IN ('reject','end_survey')
    AND lr."sourceQuestionId" IN (SELECT qid FROM cq) GROUP BY 1
), catalog AS (
SELECT jsonb_build_object(
  'columns',jsonb_build_array('qid','prompt','type','section_name','product_section',
    'product_linked','screen_out_actions','answered','respondents','completed','submissions','answers',
    'multi_select','components','response_categories','scale','derived_from','role_conflict',
    'categories_omitted'),
  'scale_columns',jsonb_build_array('labelled_positions','anchor_lo','anchor_hi',
    'slider_min','slider_max'),
  'rows',COALESCE(jsonb_agg(r ORDER BY prompt,qid),'[]'::jsonb)) AS questions
FROM (
  SELECT cq.prompt,cq.qid,jsonb_build_array(
    cq.qid,cq.prompt,cq.qtype,qs.name,qs."productSection",cobs.prod_linked,
    cso.acts,COALESCE(cobs.resp,0)>0,COALESCE(cobs.resp,0),COALESCE(cobs.rcomp,0),
    COALESCE(cobs.subs,0),COALESCE(cobs.answers,0),
    CASE lower(cq.settings->>'answer-type') WHEN 'multiple' THEN true
         WHEN 'one' THEN false WHEN 'single' THEN false WHEN 'dropdown' THEN false END,
    clists.comps,clists.cats,
    CASE WHEN cscale.pts IS NOT NULL OR cq.settings ? 'settingSlider'
         THEN jsonb_build_array(cscale.pts,cscale.lo,cscale.hi,
           cq.settings->'settingSlider'->'min',
           cq.settings->'settingSlider'->'max') END,
    CASE WHEN cconf.observed THEN 'config+observed' ELSE 'config' END,
    COALESCE(cconf.conflict,false),false) r
  FROM cq
  LEFT JOIN question_section qs ON qs.id=cq.sect
  LEFT JOIN cobs ON cobs.qid=cq.qid
  LEFT JOIN clists ON clists.qid=cq.qid
  LEFT JOIN cconf ON cconf.qid=cq.qid
  LEFT JOIN cscale ON cscale.qid=cq.qid
  LEFT JOIN cso ON cso.qid=cq.qid) y
)
SELECT s.state,s."isActive" AS is_active,
       jsonb_build_object(
         'products',(SELECT jsonb_agg(jsonb_build_object('product',p.name,
                     'blindingNumber',p."blindingNumber") ORDER BY p.name)
                     FROM product p WHERE p."surveyId"=s.id),
         'scored_measures_by_product',scored_summary.measures,
         'catalog',catalog.questions,
         'benchmark_context',jsonb_build_object(
           'current_survey_is_benchmark_source',s.is_benchmark_source,
           'current_survey_category',COALESCE(NULLIF(BTRIM(s.benchmark_category_label),''),
                                              NULLIF(BTRIM(sn.category_label_snapshot),'')),
           'has_assigned_benchmark',EXISTS (
             SELECT 1 FROM benchmark_registry br
             WHERE br.is_active
               AND LOWER(BTRIM(br.category_label))=LOWER(COALESCE(
                     NULLIF(BTRIM(s.benchmark_category_label),''),
                     NULLIF(BTRIM(sn.category_label_snapshot),'')))
           ),
           'assigned_benchmark',(
             SELECT jsonb_build_object(
                      'category',br.category_label,'survey_id',br.survey_id,
                      'product_id',br.product_id,'internal_label',br.internal_label,
                      'survey_title',bs.title,'product',bp.name)
             FROM benchmark_registry br
             JOIN survey bs ON bs.id=br.survey_id
             JOIN product bp ON bp.id=br.product_id
             WHERE br.is_active
               AND LOWER(BTRIM(br.category_label))=LOWER(COALESCE(
                     NULLIF(BTRIM(s.benchmark_category_label),''),
                     NULLIF(BTRIM(sn.category_label_snapshot),'')))
             ORDER BY br.created_at DESC LIMIT 1
           ),
           'active_benchmarks',COALESCE((
             SELECT jsonb_agg(jsonb_build_object(
                      'category',br.category_label,'survey_id',br.survey_id,
                      'product_id',br.product_id,'internal_label',br.internal_label,
                      'survey_title',bs.title,'product',bp.name)
                    ORDER BY br.category_label,br.created_at DESC)
             FROM benchmark_registry br
             JOIN survey bs ON bs.id=br.survey_id
             JOIN product bp ON bp.id=br.product_id
             WHERE br.is_active
           ),'[]'::jsonb)
         )
       ) AS packet
FROM survey s
LEFT JOIN survey_nomenclature sn ON sn.survey_id=s.id
CROSS JOIN scored_summary CROSS JOIN catalog
WHERE s.id=:survey_id
"""
)


# The instruction block in front of the payload is agent_instructions.INVENTORY_PREAMBLE:
# how to read the inventory, how to answer a thin request, which measure to test, and
# the clickable-suggestion contract.


# Process-local and deliberately bounded: workers do not coordinate cache state. Cache the exact
# serialized JSON packet, which is shared by startup inventory and the on-demand tool; each caller
# adds its own stable preamble without re-querying or re-serializing the data.
_INVENTORY_CACHE: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
_INVENTORY_CACHE_LOCK = threading.Lock()


def _inventory_cache_get(survey_id: str) -> tuple[str, str] | None:
    """Return the cached (status, payload) for `survey_id`, or None."""
    now = time.monotonic()
    with _INVENTORY_CACHE_LOCK:
        cached = _INVENTORY_CACHE.get(survey_id)
        if cached is None:
            return None
        expires_at, status, inventory = cached
        if expires_at <= now:
            del _INVENTORY_CACHE[survey_id]
            return None
        _INVENTORY_CACHE.move_to_end(survey_id)
        return status, inventory


def _inventory_cache_put(survey_id: str, status: str, inventory: str, ttl_s: int) -> None:
    # The status is cached alongside the bytes: a `degraded` packet must not come back from the
    # cache reading as a complete one.
    if not inventory or INVENTORY_CACHE_MAX_ENTRIES <= 0 or ttl_s <= 0:
        return
    with _INVENTORY_CACHE_LOCK:
        _INVENTORY_CACHE[survey_id] = (time.monotonic() + ttl_s, status, inventory)
        _INVENTORY_CACHE.move_to_end(survey_id)
        while len(_INVENTORY_CACHE) > INVENTORY_CACHE_MAX_ENTRIES:
            _INVENTORY_CACHE.popitem(last=False)


def _clear_inventory_cache() -> None:
    """Clear the process-local inventory cache (tests and explicit operational refreshes)."""
    with _INVENTORY_CACHE_LOCK:
        _INVENTORY_CACHE.clear()


class _PacketResult(NamedTuple):
    """What happened, and the bytes if there are any.

    `status` is the reason, `payload` is what is safe to inject -- they are separate because five
    very different outcomes used to share one empty string, and the caller could not tell a
    database outage from a survey nobody has answered yet:

      ok           complete packet
      degraded     over budget; option-label lists were trimmed and the affected rows carry
                   categories_omitted=true. `payload` is empty in the one case where even the
                   fully-trimmed packet does not fit, because there is then nothing safe to send.
      empty        the survey exists and holds no response data at all. A real, reportable fact,
                   and the packet still lists every CONFIGURED question with answered=false.
      not_found    no such survey row
      unavailable  the data layer failed -- retryable, and says nothing about the survey
    """

    status: str
    payload: str


# Tier 2 of the degradation ladder, and the only tier built: option-label lists are the largest
# compressible thing in the packet (16,452 of 70,466 chars on the worst survey) while prompts,
# which are larger still, must never be truncated -- a real prompt carries its attribute at the
# END ("...This product will... Taste great"), so cutting the tail renames the measure.
# Measured over all 330 surveys holding response data, the worst packet is 88,931 chars against
# a 120,000 cap and none reaches 75% of it, so every tier here is unreachable on this database.
# It exists for production, which holds surveys this sample copy does not.
_PACKET_LABEL_TIERS = (15, 5, 0)


def _degrade_catalog_labels(catalog: dict, keep: int) -> int:
    """Trim every option-label list in `catalog` to `keep` entries, in place.

    Returns the number of labels this call dropped. The tiers are cumulative -- a list trimmed to
    15 gets trimmed again to 5 -- so the caller sums these to report the true total; reporting
    only the last step under-counts, and this flag exists precisely to be honest about what went.

    Marks each trimmed row `categories_omitted=true`, which the preamble defines as NOT FETCHED
    rather than ABSENT -- the distinction the original bug turned on, where a NULL label field
    read as "this measure has no labels".
    """
    columns = catalog.get("columns") or []
    rows = catalog.get("rows") or []
    try:
        label_idx = [columns.index("components"), columns.index("response_categories")]
        omitted_idx = columns.index("categories_omitted")
    except ValueError:
        return 0
    labels_dropped = 0
    for row in rows:
        for idx in label_idx:
            labels = row[idx]
            if isinstance(labels, list) and len(labels) > keep:
                labels_dropped += len(labels) - keep
                row[idx] = labels[:keep] or None
                row[omitted_idx] = True
    return labels_dropped


def _catalog_omitted_count(catalog: dict) -> int:
    """How many catalog rows are flagged categories_omitted."""
    columns = catalog.get("columns") or []
    if "categories_omitted" not in columns:
        return 0
    omitted_idx = columns.index("categories_omitted")
    return sum(1 for row in (catalog.get("rows") or []) if row[omitted_idx])


def _survey_analysis_packet_payload(survey_id: str) -> _PacketResult:
    """Return the shared serialized packet for `survey_id` as a discriminated result.

    Fails soft on every path, but no longer silently: see `_PacketResult` for what each status
    means. Prompts are never truncated; only option-label lists degrade, and every row they were
    taken from says so.
    """
    cached = _inventory_cache_get(survey_id)
    if cached is not None:
        print(f"[analysis-packet] cache hit ({len(cached[1])} chars, status={cached[0]})")
        return _PacketResult(*cached)

    try:
        params = {"survey_id": survey_id}
        connection_started = time.perf_counter()
        try:
            conn = _sql_engine().connect()
        except Exception:
            connection_ms = (time.perf_counter() - connection_started) * 1000
            print(
                f"[inventory-db-timing] connection_acquire={connection_ms:.1f}ms "
                f"status=error survey_id={survey_id}"
            )
            raise
        connection_ms = (time.perf_counter() - connection_started) * 1000
        print(
            f"[inventory-db-timing] connection_acquire={connection_ms:.1f}ms "
            f"status=ok survey_id={survey_id}"
        )

        with conn:
            query_started = time.perf_counter()
            try:
                survey_row = conn.execute(text(_INV_SQL), params).mappings().first()
            except Exception:
                query_ms = (time.perf_counter() - query_started) * 1000
                print(
                    f"[inventory-db-timing] query_and_fetch={query_ms:.1f}ms "
                    f"status=error survey_id={survey_id}"
                )
                raise
            query_ms = (time.perf_counter() - query_started) * 1000
            print(
                f"[inventory-db-timing] query_and_fetch={query_ms:.1f}ms "
                f"status=ok survey_id={survey_id}"
            )
            if survey_row is None:
                print("[analysis-packet] not_found (no such survey)")
                return _PacketResult("not_found", "")
            packet = survey_row["packet"] or {}
            # jsonb objects have their own key order. Rebuild the established Python order so
            # startup and on-demand packets remain byte-identical to the established contract.
            # `result` leads, and is always present: a status the model only sees when something
            # went wrong is the same "absent means fine" trap the catalog exists to close.
            block = {
                "result": "ok",
                "products": packet.get("products"),
                "scored_measures_by_product": packet.get("scored_measures_by_product"),
                "catalog": packet.get("catalog"),
                "benchmark_context": packet.get("benchmark_context"),
            }
    except Exception as exc:  # noqa: BLE001 - the agent works without this
        print(f"[analysis-packet] unavailable ({type(exc).__name__}: {exc})")
        return _PacketResult("unavailable", "")

    # Both measure sections are now columnar objects, which are truthy even when they hold no
    # rows -- test the row lists, not the containers.
    catalog = block["catalog"] or {}
    scored_rows = (block["scored_measures_by_product"] or {}).get("rows") or []
    catalog_rows = catalog.get("rows") or []
    if not (scored_rows or catalog_rows or block["benchmark_context"]):
        print("[analysis-packet] empty (survey holds no questions and no benchmark config)")
        return _PacketResult("empty", "")

    # A survey with questions but no answers is a REPORTABLE fact, not a failure, and the packet
    # is still worth sending: it names every configured question with answered=false. Only a
    # survey holding nothing at all returns no bytes.
    answered_column = (catalog.get("columns") or []).index("answered") \
        if "answered" in (catalog.get("columns") or []) else None
    has_responses = bool(scored_rows) or (
        answered_column is not None
        and any(row[answered_column] for row in catalog_rows)
    )
    if not has_responses:
        block["result"] = "empty"

    payload = json.dumps(block, ensure_ascii=False, default=str)
    if len(payload) > INVENTORY_MAX_CHARS:
        # Degrade rather than discard. The old behaviour dropped the whole inventory, which on
        # the largest surveys is precisely where the model most needs a candidate list.
        labels_dropped = 0
        for keep in _PACKET_LABEL_TIERS:
            labels_dropped += _degrade_catalog_labels(catalog, keep)
            questions = _catalog_omitted_count(catalog)
            block["result"] = "degraded"
            block["omitted"] = {
                "option_labels_kept_per_question": keep,
                "questions": questions,
                "labels_dropped": labels_dropped,
            }
            payload = json.dumps(block, ensure_ascii=False, default=str)
            if len(payload) <= INVENTORY_MAX_CHARS:
                print(f"[analysis-packet] degraded to {len(payload)} chars "
                      f"(limit {INVENTORY_MAX_CHARS}): kept {keep} option label(s) per question, "
                      f"trimmed {questions} question(s), dropped {labels_dropped} label(s)")
                break
        else:
            print(f"[analysis-packet] degraded but still {len(payload)} chars > "
                  f"{INVENTORY_MAX_CHARS} limit with every option label dropped "
                  "-- the agent will discover this survey via SQL")
            return _PacketResult("degraded", "")

    status = block["result"]
    print(f"[analysis-packet] {len(payload)} chars, status={status}: "
          f"{len(scored_rows)} scored measure(s), {len(catalog_rows)} catalogued question(s)")
    state = str(survey_row["state"] or "").lower()
    closed = not bool(survey_row["is_active"]) and state in {"closed", "archived"}
    if (block.get("benchmark_context") or {}).get("current_survey_is_benchmark_source"):
        ttl_s = INVENTORY_CACHE_BENCHMARK_TTL_S
    else:
        ttl_s = INVENTORY_CACHE_CLOSED_TTL_S if closed else INVENTORY_CACHE_ACTIVE_TTL_S
    _inventory_cache_put(survey_id, status, payload, ttl_s)
    return _PacketResult(status, payload)


def survey_inventory(survey_id: str) -> str:
    """The startup inventory block for `survey_id`, or "" if there is nothing safe to inject.

    `unavailable` and `not_found` inject nothing, deliberately: the preamble opens with "do not
    re-query it", and asserting that over data we failed to fetch is how a fetch failure turns
    into a confident wrong answer. `empty` and `degraded` DO ship, because both are facts the
    packet states about itself.
    """
    result = _survey_analysis_packet_payload(survey_id)
    return INVENTORY_PREAMBLE + result.payload if result.payload else ""


# PG_DIALECT_RULES, REPORTING_RULES and build_system_prompt() are in agent_instructions.py.
# This runner consumes the single complete assembled prompt.
SYSTEM_PROMPT = SystemMessage(content=SYSTEM_PROMPT_TEXT)

#----------defining auxillary functions (acting as nodes or edges in the graph)

def _invoke_tool_call(tc: dict[str, Any]) -> str:
    """Invoke one tool call and normalize its result without changing call order."""
    name = tc["name"]
    tool_obj = tools_by_name.get(name)
    if tool_obj is None:
        return unknown_tool(name)
    try:
        result = tool_obj.invoke(_normalize_tool_args(tc.get("args")))
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - returned to the model as tool feedback
        return tool_error(exc)


def _canonical_sql_fingerprint(sql: Any) -> str:
    """Hash a formatting-insensitive PostgreSQL representation, with a safe fallback."""
    source = str(sql or "").strip()
    try:
        statements = parse(source, read="postgres")
        canonical = (
            statements[0].sql(dialect="postgres", pretty=False, normalize=True)
            if len(statements) == 1 else " ".join(source.rstrip(";").split())
        )
    except Exception:  # noqa: BLE001 - fingerprinting must not break tool-error feedback
        canonical = " ".join(source.rstrip(";").split())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_fingerprint(result: Any) -> str:
    return hashlib.sha256(str(result or "").encode("utf-8")).hexdigest()


def _progress_delta_status(text_value: str) -> tuple[str, int, int]:
    """Classify model compliance for telemetry; never rewrite model-authored evidence."""
    matches = _PROGRESS_BLOCK_RE.findall(text_value)
    open_count = text_value.lower().count(_PROGRESS_OPEN)
    close_count = text_value.lower().count(_PROGRESS_CLOSE)
    if not open_count and not close_count:
        return "missing", 0, 0
    if len(matches) != 1 or open_count != 1 or close_count != 1:
        return "malformed", 0, 0
    block = matches[0]
    bullet_lines = [
        line.lstrip()
        for line in block.splitlines()
        if line.lstrip().startswith("- ")
    ]
    bullets = len(bullet_lines)
    begins_with_block = text_value.lstrip().lower().startswith(_PROGRESS_OPEN)
    if (
        not begins_with_block
        or not bullets
        or any(not line.lower().startswith("- [source:") for line in bullet_lines)
    ):
        return "malformed", len(block), bullets
    status = (
        "oversized"
        if len(block) > PROGRESS_DELTA_MAX_CHARS or bullets > PROGRESS_DELTA_MAX_BULLETS
        else "ok"
    )
    return status, len(block), bullets


def _conversation_prompt_cache_key(config: RunnableConfig) -> str | None:
    """Stable, non-identifying cache-routing key for one checkpointed conversation."""
    configurable = config.get("configurable") if isinstance(config, dict) else None
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if thread_id is None or str(thread_id) == "":
        return None
    # Responses API limits prompt_cache_key to 64 characters. A 48-hex-character
    # prefix retains 192 bits of the thread hash while keeping this key at 61 chars.
    digest = hashlib.sha256(str(thread_id).encode("utf-8")).hexdigest()[:48]
    return f"funda-thread:{digest}"


def call_tool(state: AgentState):
    last = state["messages"][-1]
    tool_calls = list(getattr(last, "tool_calls", None) or [])

    # A model emits all calls in one response before any result exists. Homogeneous SQL and
    # statistics batches are independent and can run concurrently. Mixed batches stay
    # sequential because a later tool may depend on an earlier result.
    batch_tool = tool_calls[0].get("name") if tool_calls else None
    parallel_batch = (
        len(tool_calls) >= 2
        and batch_tool in {"nl2sql_tool", "run_survey_stats"}
        and all(tc.get("name") == batch_tool for tc in tool_calls)
    )
    if parallel_batch:
        batch_started = time.perf_counter()
        futures = [_TOOL_EXECUTOR.submit(_invoke_tool_call, tc) for tc in tool_calls]
        # Consume in call order, not completion order: message order is model-facing state and
        # part of the cacheable prefix on the next turn.
        results = [future.result() for future in futures]
        print(
            f"[tool-batch-timing] tool={batch_tool} calls={len(tool_calls)} "
            f"wall={(time.perf_counter() - batch_started) * 1000:.1f}ms"
        )
    else:
        results = [_invoke_tool_call(tc) for tc in tool_calls]

    word_cloud_artifact = ""
    word_cloud_intro = ""
    pca_chart_artifacts = list(state.get("pca_chart_artifacts") or [])
    for index, tc in enumerate(tool_calls):
        result = results[index]
        if tc.get("name") == "run_survey_stats" \
                and result.startswith(_PCA_TOOL_RESULT_PREFIX):
            try:
                prepared = json.loads(result[len(_PCA_TOOL_RESULT_PREFIX):])
                text_value = str(prepared["text"])
                prepared_artifacts = prepared["artifacts"]
                if not isinstance(prepared_artifacts, list) or not all(
                    isinstance(item, dict) and isinstance(item.get("artifact"), str)
                    for item in prepared_artifacts
                ):
                    raise ValueError("artifacts must be valid chart envelopes")
                results[index] = text_value
                pca_chart_artifacts.extend(prepared_artifacts)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                print(f"[pca-chart] invalid internal artifact envelope: {exc}")
                results[index] = "PCA plot unavailable: internal chart validation failed."
            continue
        if tc.get("name") != "generate_word_cloud" \
                or not result.startswith(_WORD_CLOUD_ARTIFACT_PREFIX):
            continue
        try:
            prepared = json.loads(result[len(_WORD_CLOUD_ARTIFACT_PREFIX):])
            word_cloud_artifact = str(prepared["artifact"])
            word_cloud_intro = str(prepared["intro"])
            results[index] = word_cloud_ready(
                str(prepared["question"]),
                int(prepared["filtered_answer_count"]),
                bool(prepared["grouped"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[word-cloud] invalid internal artifact envelope: {exc}")
            results[index] = word_cloud_question_unavailable(
                "the generated artifact failed internal validation"
            )

    sql_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "nl2sql_tool"]
    sql_calls = [tool_calls[i] for i in sql_indices]
    sql_results = [results[i] for i in sql_indices]

    # Classify empty SQL results before adding batch-level warnings.
    heads = [result[:800] for result in sql_results]
    zero_flags = [ZERO_ROW_HEAD in head for head in heads]
    zero_streak = state.get("zero_row_streak", 0)
    if sql_calls:
        # Simultaneous empty results are one retrieval attempt, not consecutive retries.
        zero_streak = zero_streak + 1 if all(zero_flags) else 0

    warnings: list[str] = []
    if sql_calls:
        previous = state.get("last_sql_observations") or {}
        observations: dict[str, str] = {}
        repeated = False
        for tc, result in zip(sql_calls, sql_results, strict=False):
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            query_digest = _canonical_sql_fingerprint(args.get("sql_query", ""))
            result_digest = _result_fingerprint(result)
            if previous.get(query_digest) == result_digest \
                    or observations.get(query_digest) == result_digest:
                repeated = True
            observations[query_digest] = result_digest
        if repeated:
            warnings.append(REPEATED_SQL_WARNING)
        if zero_streak >= 2:
            warnings.append(zero_row_streak_warning(zero_streak))
    if warnings:
        first_sql_index = sql_indices[0]
        results[first_sql_index] = "\n".join(warnings) + "\n" + results[first_sql_index]

    outputs = [
        ToolMessage(
            content=result,
            name=tc["name"],
            tool_call_id=tc.get("id", ""),
        )
        for tc, result in zip(tool_calls, results, strict=False)
    ]

    # number_of_steps counts LLM turns only, so the tool node leaves it alone.
    update: dict[str, Any] = {"messages": outputs, "zero_row_streak": zero_streak}

    if word_cloud_artifact:
        update["word_cloud_artifact"] = word_cloud_artifact
        update["word_cloud_intro"] = word_cloud_intro
    if pca_chart_artifacts:
        update["pca_chart_artifacts"] = pca_chart_artifacts
    if sql_calls:
        update["last_sql_observations"] = observations
    return update



# Short-term memory, trim side. Applied when the request is ASSEMBLED, not to the state: the
# checkpoint keeps the whole thread, and only what call_model re-sends is bounded. That is the
# non-destructive half of the trade -- a later round can still be replayed or inspected in full,
# and nothing is summarised, so no figure is ever restated by a model rather than retrieved.
#
# What is kept is the first message plus at most HISTORY_MAX_ROUNDS recent user rounds.
# HISTORY_MAX_MESSAGES is a secondary ceiling that may drop additional *complete* old rounds.
# The first message is not sentiment about "the system message": message 0 here carries the
# SCOPE ids and the pre-fetched inventory, and the system prompt orders every query to use those
# ids, so a window that drops it produces SQL scoped to nothing. It is also why a follow-up
# question needs no preamble of its own -- see main().
def _trim_history(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    msgs = list(messages)
    if len(msgs) <= 1:
        return msgs

    # Only genuine round openers are HumanMessages in state; progress deltas are persisted
    # inside AIMessage content and the step-budget notice is appended only to the local request.
    # Selecting only HumanMessage boundaries therefore keeps tool-call/result pairs intact and
    # can never create an orphan ToolMessage.
    round_starts = [
        index for index, message in enumerate(msgs)
        if isinstance(message, HumanMessage)
    ]
    if not round_starts:
        return msgs

    current_round_start = round_starts[-1]
    start = 1

    # Keep the most recent configured number of rounds. Message 0 remains as the permanent
    # scope/inventory anchor even after the rest of its old round has been trimmed.
    if HISTORY_MAX_ROUNDS > 0 and len(round_starts) > HISTORY_MAX_ROUNDS:
        start = max(start, round_starts[-HISTORY_MAX_ROUNDS])

    # The message ceiling is secondary to the round cap. Calculate it from the state at the
    # active round's opener rather than the ever-growing current length. This freezes the trim
    # boundary throughout a ReAct round, so later assistant/tool messages extend the previous
    # request exactly instead of shifting the cache prefix on every turn. The active round may
    # temporarily exceed the ceiling; it is deliberately never cut.
    if HISTORY_MAX_MESSAGES > 0:
        target = max(1, current_round_start + 1 - HISTORY_MAX_MESSAGES)
        if target > 1:
            message_boundary = next(
                (round_start for round_start in round_starts if round_start >= target),
                current_round_start,
            )
            start = max(start, message_boundary)

    if start <= 1:
        return msgs

    # The selected boundary is a completed-round opener and is stable until the next genuine
    # HumanMessage arrives. A long active round may therefore exceed HISTORY_MAX_MESSAGES, but
    # it remains byte-for-byte intact and later ReAct turns retain an append-only cache prefix.
    return [msgs[0]] + msgs[start:]


_TRAILING_SUGGESTION_RE = re.compile(r"^[-*\s]*\{\{(.+?)\}\}[\s.]*$")


def _split_trailing_suggestions(value: str) -> tuple[str, list[str]]:
    lines = value.rstrip().splitlines()
    suggestions: list[str] = []
    index = len(lines)
    while index > 0:
        line = lines[index - 1].strip()
        if not line:
            index -= 1
            continue
        match = _TRAILING_SUGGESTION_RE.match(line)
        if not match:
            break
        suggestions.insert(0, match.group(1).strip())
        index -= 1
    return "\n".join(lines[:index]).rstrip(), suggestions


_CHART_SUGGESTION_RE = re.compile(
    r"\b(plot|chart|scatterplot|biplot|heatmap|bar|column|line|pie|histogram|dot\s*plot)\b",
    re.IGNORECASE,
)


def _filter_chart_suggestions(value: str) -> str:
    """Keep trailing suggestions scoped to another chart action."""
    prose, suggestions = _split_trailing_suggestions(value)
    suggestions = [suggestion for suggestion in suggestions if _CHART_SUGGESTION_RE.search(suggestion)]
    return "\n\n".join(
        part for part in [prose, *(f"{{{{{suggestion}}}}}" for suggestion in suggestions)] if part
    )


def _assemble_pca_final(value: str, artifacts: list[dict[str, Any]]) -> str:
    clean = _GPI_CHART_BLOCK_RE.sub("", value).strip()
    prose, model_suggestions = _split_trailing_suggestions(clean)
    required = []
    for item in artifacts:
        if item.get("dimensions") == "2d":
            required.append(str(item.get("three_d_suggestion") or "").strip())
        elif item.get("dimensions") == "3d":
            required.append(str(item.get("two_d_suggestion") or "").strip())
    suggestions: list[str] = []
    for suggestion in [*required, *model_suggestions]:
        normalized = _safe_chart_label(suggestion, "")
        if normalized and normalized.casefold() not in {
            existing.casefold() for existing in suggestions
        }:
            suggestions.append(normalized)
        if len(suggestions) == 5:
            break
    parts = [part for part in [prose, *(str(item["artifact"]) for item in artifacts)] if part]
    final_text = "\n\n".join(parts)
    if suggestions:
        final_text += ("\n\n" if final_text else "") + "\n".join(
            f"{{{{{suggestion}}}}}" for suggestion in suggestions
        )
    return final_text


def call_model(state: AgentState, config: RunnableConfig):
    global _ACTIVE_MODEL_STARTED, _LAST_MODEL_TIMING
    trusted_artifact = state.get("word_cloud_artifact", "")
    if trusted_artifact:
        intro = state.get("word_cloud_intro", "").strip()
        final_text = (intro + "\n\n" if intro else "") + trusted_artifact
        return {
            "messages": [AIMessage(content=final_text)],
            "number_of_steps": state["number_of_steps"] + 1,
        }

    # Every decision and synthesis turn uses the same model and complete instruction set.
    messages = [SYSTEM_PROMPT] + _trim_history(state["messages"])

    # On the final allowed turn, call the LLM *without* tools bound: it cannot emit
    # another tool call, so it must answer from whatever it has already retrieved.
    if state["number_of_steps"] >= MAX_LLM_STEPS - 1:
        active_model = llm
        messages.append(HumanMessage(content=STEP_BUDGET_NOTICE))
    else:
        active_model = model

    cache_key = _conversation_prompt_cache_key(config)
    invoke_options = {"prompt_cache_key": cache_key} if cache_key else {}
    model_started = time.perf_counter()
    with _MODEL_TIMING_LOCK:
        _ACTIVE_MODEL_STARTED = model_started
    try:
        response = active_model.invoke(messages, config=config, **invoke_options)
    finally:
        model_finished = time.perf_counter()
        with _MODEL_TIMING_LOCK:
            _LAST_MODEL_TIMING = {
                "started_at": model_started,
                "finished_at": model_finished,
                "duration_ms": (model_finished - model_started) * 1000,
            }
            _ACTIVE_MODEL_STARTED = None
    response_tool_calls = getattr(response, "tool_calls", None) or []
    if response_tool_calls and isinstance(state["messages"][-1], ToolMessage):
        status, chars, bullets = _progress_delta_status(_raw_message_to_text(response))
        print(
            f"[progress-delta] status={status} chars={chars} bullets={bullets} "
            f"next_tools={len(response_tool_calls)}"
        )
    if not response_tool_calls:
        response_text = _raw_message_to_text(response)
        if _PROGRESS_OPEN in response_text.lower() or _PROGRESS_CLOSE in response_text.lower():
            print("[progress-delta] status=unexpected-final")
        if _WORD_CLOUD_BLOCK_RE.search(response_text):
            # Fail closed if the model ignored the required tool. The frontend never receives
            # model-generated word-cloud data; only the tool-owned artifact carries that type.
            prose = _WORD_CLOUD_BLOCK_RE.sub("", response_text).strip()
            response_text = (prose + "\n\n" if prose else "") + UNTRUSTED_WORD_CLOUD_BLOCK
            response = response.model_copy(update={"content": response_text})
        pca_artifacts = state.get("pca_chart_artifacts") or []
        if pca_artifacts:
            response = response.model_copy(
                update={"content": _assemble_pca_final(response_text, pca_artifacts)}
            )
        elif _GPI_CHART_BLOCK_RE.search(response_text):
            sanitized, chart_withheld = _sanitize_model_chart_blocks(response_text)
            # Charts are now a default part of a table answer, not only an explicit chart
            # request, so trailing suggestions are no longer narrowed to chart actions.
            prose = sanitized.strip()
            # User-facing, so it names no internal contract: the reader cannot act on
            # "gpi-chart JSON", only on being told to ask again.
            withheld = (
                "I could not render that chart. Ask for it again, naming the measure you want "
                "on each axis, and I will rebuild it from the analysis."
                if chart_withheld else ""
            )
            response = response.model_copy(
                update={"content": (prose + "\n\n" if prose and withheld else prose) + withheld}
            )
    return {
        "messages": [response],
        "number_of_steps": state["number_of_steps"] + 1,
    }


def should_continue(state: AgentState):
    if state["number_of_steps"] >= MAX_LLM_STEPS:
        return "end"
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    return "continue" if tool_calls else "end"




#=========== Building Agent Graph ========================
# checkpointer defaults to None, which is exactly what compile() assumes on its own -- so a
# bare build_graph() is byte-for-byte the graph it always was, and the benchmark harnesses in
# scripts/ (which invoke without a thread_id, and would fail outright against a checkpointed
# graph) keep working untouched. main() is the only caller that passes one.
def build_graph(checkpointer=None):
    workflow = StateGraph(AgentState)

    workflow.add_node("LLM", call_model)
    workflow.add_node("tools", call_tool)

    workflow.set_entry_point("LLM")
    workflow.add_conditional_edges(
        "LLM",
        should_continue,
        {"continue": "tools", "end": END},
    )




    workflow.add_edge("tools", "LLM")

    return workflow.compile(checkpointer=checkpointer)




#=========== Building Agent ========================
def _parse_args():
    """CLI over the run's scope. Defaults are the constants at the top of this file, so
    plain `python funda_agent_exp.py` behaves exactly as it did before."""
    parser = argparse.ArgumentParser(description="Query one survey with the analyst agent.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="the question to answer")
    parser.add_argument("--client-id", default=CLIENT_ID, help="client_id to scope to")
    parser.add_argument("--org-id", default=ORG_ID, help="organization_id to scope to")
    parser.add_argument("--survey-id", default=SURVEY_ID, help="survey_id to scope to")
    parser.add_argument("--no-inventory", action="store_true",
                        help="skip the pre-fetched survey inventory and discover it via SQL")
    parser.add_argument("--chat", action="store_true",
                        help="stay open for follow-up questions on the same thread")
    parser.add_argument("--thread-id", default="default",
                        help="conversation thread; the same id resumes the same history")
    return parser.parse_args()


def _run_round(
    agent_graph,
    question: str,
    run_config: dict,
    printed: int,
    *,
    thread_survey_id: str,
    inventory_chars: int,
    conversation_turn: int,
) -> int:
    """Stream one question through the graph, printing only what this round added.

    Returns the new print offset. It has to be carried across rounds because a checkpointed
    stream replays the whole thread every time, so round 2 starts where round 1 stopped.
    """
    inputs = {
        "messages": [HumanMessage(content=question)],
        "thread_survey_id": thread_survey_id,
        "inventory_attached": inventory_chars > 0,
        "inventory_chars": inventory_chars,
        # Everything below is per-QUESTION scratch, and every one of these channels is a plain
        # value with no reducer, so passing it here overwrites what the checkpoint holds. That
        # is what makes a second round work at all: resumed as-is, number_of_steps would still
        # be round 1's final count, should_continue would see the budget already spent, and the
        # follow-up would end without a single LLM turn. Only `messages` accumulates, because
        # add_messages is the one reducer on this state.
        "number_of_steps": 0,
        "last_sql_observations": {},
        "zero_row_streak": 0,
        "word_cloud_artifact": "",
        "word_cloud_intro": "",
        "pca_chart_artifacts": [],
    }
    for st in agent_graph.stream(inputs, stream_mode="values", config=run_config):
        msgs = st["messages"]
        for msg in msgs[printed:]:
            print_message(msg)
            if msg.type == "ai":
                tool_calls = getattr(msg, "tool_calls", None) or []
                if tool_calls:
                    prose = message_to_text(msg)
                    if prose:
                        append_conversation_event(
                            run_config["configurable"]["thread_id"],
                            thread_survey_id, conversation_turn, "llm", prose,
                        )
                    for tool_call in tool_calls:
                        append_conversation_event(
                            run_config["configurable"]["thread_id"],
                            thread_survey_id, conversation_turn, "tool_call",
                            json.dumps(tool_call.get("args") or {}, default=str,
                                       ensure_ascii=False),
                            tool_call.get("name") or "",
                        )
                else:
                    append_conversation_event(
                        run_config["configurable"]["thread_id"],
                        thread_survey_id, conversation_turn, "final", message_to_text(msg),
                    )
            elif msg.type == "tool":
                append_conversation_event(
                    run_config["configurable"]["thread_id"],
                    thread_survey_id, conversation_turn, "tool_output",
                    message_to_text(msg), getattr(msg, "name", ""),
                )
        printed = len(msgs)
    return printed


def main():
    args = _parse_args()
    # Publish the run's scope so nl2sql_tool can bind :client_id/:organization_id/:survey_id
    # and repair any near-miss literal. Without this the repair is a no-op (fail open).
    # clear() before update(): _SCOPE is a process global and organization_id now decides an
    # AUTHORIZATION envelope, not just a bind param. Every current entry point happens to write
    # all three keys, so nothing leaks today -- but a future caller that writes fewer would
    # inherit the previous run's organization, which is the one stale value that must never
    # survive. The API already does this; matched here.
    _SCOPE.clear()
    _SCOPE.update({
        "client_id": args.client_id,
        "organization_id": args.org_id,
        "survey_id": args.survey_id,
    })
    # Short-term memory. InMemorySaver holds each thread's messages for the life of this
    # process -- no summarisation, no store, nothing that outlives the run; --chat is what
    # makes it visible, and a single-shot run simply never asks it for a second round.
    agent_graph = build_graph(InMemorySaver())
    # Pre-fetch runs after _SCOPE is published and before the first LLM call, so turn 0 already
    # holds this survey's measures. Returns "" and changes nothing if it cannot be built.
    question = scoped_query(args.prompt, args.client_id, args.org_id, args.survey_id)
    inventory = "" if args.no_inventory else survey_inventory(args.survey_id)
    question += inventory

    conversation_turn = begin_conversation_turn(args.thread_id, args.survey_id)
    append_conversation_event(
        args.thread_id, args.survey_id, conversation_turn, "user", args.prompt,
    )

    # Headroom so MAX_LLM_STEPS is what stops the loop, not LangGraph's own limit.
    # thread_id is what the checkpointer keys history on: same id, same conversation.
    run_config = {
        "recursion_limit": 2 * MAX_LLM_STEPS + 2,
        "configurable": {"thread_id": args.thread_id},
    }

    printed = _run_round(
        agent_graph,
        question,
        run_config,
        0,
        thread_survey_id=args.survey_id,
        inventory_chars=len(inventory),
        conversation_turn=conversation_turn,
    )
    while args.chat:
        try:
            follow_up = input("\n>>> follow-up (blank or 'exit' to quit): ").strip()
        except (EOFError, KeyboardInterrupt):  # piped stdin runs out, or Ctrl-C / Ctrl-D
            print()
            break
        if not follow_up or follow_up.lower() in {"exit", "quit"}:
            break
        # No repeated SCOPE or general-inventory fetch: both live in message 0, which
        # _trim_history keeps regardless of how long the thread grows.
        follow_up_question = follow_up

        conversation_turn = begin_conversation_turn(args.thread_id, args.survey_id)
        append_conversation_event(
            args.thread_id, args.survey_id, conversation_turn, "user", follow_up,
        )

        printed = _run_round(
            agent_graph,
            follow_up_question,
            run_config,
            printed,
            thread_survey_id=args.survey_id,
            inventory_chars=len(inventory),
            conversation_turn=conversation_turn,
        )


    # result = agent_graph.invoke(inputs)
    # print("\n--- AGENT FINAL RESPONSE---")
    # print(message_to_text(result["messages"][-1]))


if __name__ == "__main__":
    main()
