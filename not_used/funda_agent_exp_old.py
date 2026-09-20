import argparse
import os
import json
import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Annotated, NotRequired, Sequence, TypedDict

import requests
from pydantic import BaseModel, Field

from langchain_core.messages import (
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

from feature_influence import feature_influence, holm_adjust

# Model-facing text has exactly two sources: agent instructions and tool prompts.
from agent_instructions_old import (
    BENCHMARK_SCOPE,
    INVENTORY_PREAMBLE,
    PERSONA_INVENTORY_PREAMBLE,
    PERSONA_RULES,
    STALL_WARNING,
    STEP_BUDGET_NOTICE,
    SYSTEM_PROMPT_LEAN_TEXT,
    SYSTEM_PROMPT_TEXT,
    progress_tracker_message,
    scoped_query,
    zero_row_streak_warning,
)
from tool_prompts_old import (
    ANALYZE_FEATURE_INFLUENCE_DESCRIPTION,
    ANALYSIS_PACKET_RESULT_HEAD,
    FEATURE_INFLUENCE_RESULT_HEAD,
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

# Service configuration. Explicit environment values win. This local baseline otherwise reuses
# the working development runner's runtime values so credentials are not copied into another file.
# Importing funda_agent_exp defines its objects but cannot execute main() or make an agent call.
def _env_or_current(current_name: str, *names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    import funda_agent_exp as current_agent

    return getattr(current_agent, current_name)


OPENAI_API_KEY = _env_or_current("OPENAI_API_KEY", "OPENAI_API_KEY")
# Keep the comparison on exactly the same database configuration as the current runner.
# Unlike API credentials, an exported legacy DATABASE_URL must not silently change the
# reference dataset or connection behavior for this old-prompt baseline.
DATABASE_URI = _env_or_current("DATABASE_URI")
CHARTS_API_BASE_URL = os.getenv(
    "CHARTS_API_BASE_URL", "https://charts-api.gpisurveys.com"
)
CHARTS_STATS_EXTERNAL_ACCESS_SECRET = _env_or_current(
    "CHARTS_STATS_EXTERNAL_ACCESS_SECRET", "CHARTS_STATS_EXTERNAL_ACCESS_SECRET"
)
STRONG_MODEL_NAME = os.getenv("STRONG_MODEL_NAME", "gpt-5.5")
WEAK_MODEL_NAME = os.getenv("WEAK_MODEL_NAME", "gpt-5.4-mini")
MODEL_TEMPERATURE = 0.0

# Agent and tool limits.
MAX_LLM_STEPS = 12  # hard bound on LLM turns; unproductive tool loops end here
STATEMENT_TIMEOUT_MS = 20000  # server-side kill switch for runaway queries
HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "10"))
LATCH_MIN_STEP = int(os.getenv("LATCH_MIN_STEP", "1"))
# High safety ceiling: preserve complete multi-step SQL planning context while still guarding
# against a runaway tool argument. Normal ledgers should remain far below this value.
LEDGER_MAX_CHARS = int(os.getenv("LEDGER_MAX_CHARS", "4000"))
INVENTORY_MAX_CHARS = int(os.getenv("INVENTORY_MAX_CHARS", "120000"))
PERSONA_INVENTORY_MAX_CHARS = int(
    os.getenv("PERSONA_INVENTORY_MAX_CHARS", "120000")
)
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
_SCOPE_REPAIR_MAX_EDITS = 4
_CHARTS_TIMEOUT_S = 30

# LLM request tuning. The legacy shared variable remains a fallback for existing deployments.
def _reasoning_effort(env_name: str, default: str) -> str:
    shared = os.getenv("OPENAI_REASONING_EFFORT")
    value = os.getenv(env_name, shared if shared is not None else default).strip().lower()
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


STRONG_REASONING_EFFORT = _reasoning_effort("STRONG_REASONING_EFFORT", "low")
WEAK_REASONING_EFFORT = _reasoning_effort("WEAK_REASONING_EFFORT", "medium")
_STRONG_LLM_TUNING = _llm_tuning(STRONG_REASONING_EFFORT)
_WEAK_LLM_TUNING = _llm_tuning(WEAK_REASONING_EFFORT)

#============ Building Tools for the Agent ========================

_ENGINE = None
_STATS_TIMING_LOG_LOCK = threading.Lock()


def _sql_engine():
    """Return the process-wide engine, building its pool on first use."""
    global _ENGINE
    if not DATABASE_URI:
        raise RuntimeError("DATABASE_URI is not configured.")
    if _ENGINE is None:
        # Match the current reference runner's connection behavior. In particular, do
        # not impose the deployment copy's separate 10-second libpq connect timeout.
        _ENGINE = create_engine(DATABASE_URI)
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


# Anchored at the start but NOT at the end, deliberately. The end anchor was why the latch
# guard had to be removed once before: the model never writes the bare token "nothing", it
# writes "Nothing; this query adds the combined average" or "nothing if query returns one row",
# and a full-string match reads every one of those as an open gap -- so the guard blocked every
# latch and every run paid strong + full schema for its closing paragraph.
# A leading nothing-phrase is therefore treated as closed. The residual risk -- a real gap
# smuggled in after the word "nothing" -- needs more_sql_expected to be wrong at the same time
# to cause a bad latch, and the promote-back rule in call_tool catches it on the next SQL call.
_NOTHING_RE = re.compile(
    r"^(nothing|none|n/?a|nil|no (more|further|additional)\b[a-z ]*|complete|done)\b",
    re.I,
)

def _normalize_gap(s: Any) -> str:
    """Case/whitespace-insensitive form of a ledger field, for stall comparison.

    Byte comparison let a gap survive by being retyped with different spacing; this
    still misses a genuine paraphrase, but it costs nothing and catches the common case.
    """
    return " ".join(str(s or "").lower().split()).strip(" .;:-")


def _clip_ledger(s: Any) -> str:
    """Apply a high safety ceiling without clipping normal multi-step planning context."""
    text_value = str(s or "").strip()
    return text_value[:LEDGER_MAX_CHARS] if len(text_value) > LEDGER_MAX_CHARS else text_value


def _gap_open(s: Any) -> bool:
    """True if `information_still_needed` names a real gap."""
    normalized = _normalize_gap(s)
    return bool(normalized) and not _NOTHING_RE.match(normalized)


def _merge_ledger_values(values: Sequence[Any], *, gaps: bool = False) -> str:
    """Deterministically merge parallel calls' running ledgers.

    Parallel calls should report the same batch-level state, but this remains conservative
    when they do not: distinct gathered facts and open gaps are retained in original call
    order. A longer entry replaces a shorter entry it contains, avoiding duplicated copies of
    the same running ledger.
    """
    saw_report = False
    merged: list[tuple[str, str]] = []
    for value in values:
        clipped = _clip_ledger(value)
        normalized = _normalize_gap(clipped)
        if not normalized:
            continue
        saw_report = True
        if gaps and not _gap_open(clipped):
            continue

        replaced = False
        for i, (prior_normalized, _prior_text) in enumerate(merged):
            if normalized == prior_normalized or normalized in prior_normalized:
                replaced = True
                break
            if prior_normalized in normalized:
                merged[i] = (normalized, clipped)
                replaced = True
                break
        if not replaced:
            merged.append((normalized, clipped))

    if not merged:
        return "nothing" if gaps and saw_report else ""
    if len(merged) == 1:
        return _clip_ledger(merged[0][1])
    return _clip_ledger("\n".join(f"- {item[1]}" for item in merged))


def _scope_note(sql: str) -> str:
    """Warn when a statement is constrained to nothing at all.

    Scoping is instructed but not enforced: the ids are bound as :client_id/:organization_id/
    :survey_id, and SQLAlchemy simply ignores the ones a query never mentions. So a statement
    that forgets the filter does not error -- it aggregates every survey in the database and
    returns confident, wrong numbers. That is the one failure class here with no symptom, so it
    gets a deterministic check rather than a prompt line.

    Deliberately narrow, to stay quiet on legitimate queries: any scope placeholder or literal
    clears it, and so does ANY uuid literal, because a query filtering by ids resolved on an
    earlier turn is already constrained (and cross-survey comparison within the organization is
    a supported thing to do). What is left -- no scope, no ids at all -- is a whole-table scan,
    which is exactly the leak. Advisory only: it never blocks the query.
    """
    if not _SCOPE:
        return ""
    if any(f":{name}" in sql for name in _SCOPE) or any(v in sql for v in _SCOPE.values()):
        return ""
    if _UUIDISH_RE.search(sql):
        return ""
    print("[sql] scope warning: statement references no scope id and no uuid")
    return SCOPE_WARNING

# The description the model reads -- the ledger contract, the structure-not-meaning rule,
# the dialect and scoping requirements -- is tool_prompts.NL2SQL_TOOL_DESCRIPTION.
# It is assigned to __doc__ below, before tool() wraps the function and captures it.
def nl2sql_tool(
    sql_query: str,
    information_gathered: str,
    information_still_needed: str,
    more_sql_expected: bool = True,
) -> str:
    # The three planning fields are intentionally not consumed by the SQL executor itself.
    # They remain in the tool schema so the model emits them with each call; call_tool() reads
    # the original arguments to update the progress ledger and the strong/weak-model routing.
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
        # Named placeholders are bound here, so a scope id the model referenced by name can
        # never be corrupted in transit. Extra keys are ignored when unused.
        params = dict(_SCOPE)

        with engine.connect() as conn, conn.begin():
            # Postgres rejects any INSERT/UPDATE/DELETE/DDL in this transaction.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            result = conn.execute(text(sql_query), params)

            if not result.returns_rows:
                return NON_SELECT_RESULT

            columns = list(result.keys())
            rows = [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
            if not rows:
                return repair_note + ZERO_ROW_MSG

            # default=str keeps Decimal/date/UUID serializable and compact.
            return repair_note + json.dumps(rows, default=str, ensure_ascii=False)
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
# The question ids are UUID-validated here and must come from the scoped inventory/discovery
# contract. The Charts API request itself does not depend on a PostgreSQL ownership lookup.

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_REQUIRES_REFERENCE = frozenset({"pearson", "spearman", "penalty"})


# Authorization remains for explicit cross-survey packet retrieval. Statistics calls do not
# use this query: their ids are sent directly to the Charts API for maximum concurrency.
_SURVEY_OWNER_SQL = """
SELECT s.id::text AS survey_id, s.title,
       (s.id = CAST(:survey_id AS uuid)
        OR s.id = CAST(:benchmark_id AS uuid)
        OR (CAST(:organization_id AS uuid) IS NOT NULL
            AND s.organization_id = CAST(:organization_id AS uuid))) AS in_scope
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
    """Resolve a packet target and enforce the same envelope as the statistics tool."""
    try:
        with _sql_engine().connect() as conn:
            row = conn.execute(text(_SURVEY_OWNER_SQL), {
                "target_survey_id": survey_id,
                "survey_id": _SCOPE["survey_id"],
                "benchmark_id": BENCHMARK_SCOPE["survey_id"],
                "organization_id": _SCOPE.get("organization_id"),
            }).mappings().first()
    except Exception as exc:  # noqa: BLE001 - converted into fail-closed tool feedback
        raise ScopeLookupUnavailable(str(exc)) from exc
    if row is None:
        return None
    return {
        "survey_id": row["survey_id"],
        "title": row["title"],
        "in_scope": bool(row["in_scope"]),
    }


class SurveyAnalysisPacketInput(BaseModel):
    survey_id: str = Field(
        ...,
        description=(
            "UUID of the already-resolved current, same-organization historical, or fixed "
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

    payload = _survey_analysis_packet_payload(survey_id)
    if not payload:
        return packet_not_available(owner["title"])
    return analysis_packet_preamble(owner["title"]) + payload


get_survey_analysis_packet.__doc__ = GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION
get_survey_analysis_packet = tool(
    args_schema=SurveyAnalysisPacketInput
)(get_survey_analysis_packet)


def _format_stats_response(data: dict) -> str:
    lines = [
        f"Question type: {data.get('questionType', 'unknown')}, "
        f"respondents: {data.get('respondentCount', '?')}"
    ]
    stats = data.get("stats", {})

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
            else:
                lines.append(f"\n{test_name.upper()} [alpha={alpha}]: {json.dumps(result)}")

    for alpha, result in stats.get("chi-square", {}).items():
        if "error" in result:
            lines.append(f"\nChi-square [alpha={alpha}]: Not available - {result['error']}")
            continue
        lines.append(f"\nChi-square [alpha={alpha}]: {result}")

    return "\n".join(lines)


# Description: tool_prompts.RUN_SURVEY_STATS_DESCRIPTION, assigned below.
def run_survey_stats(
    question_id: str,
    stats_types: str,
    alpha_value: float = 0.05,
    reference_question_id: str = "",
    penalty_level: str = "",
    boxing_strategy: str = "",
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

    requested_types = {t.strip().lower() for t in stats_types.split(",") if t.strip()}
    if requested_types & _REQUIRES_REFERENCE and not reference_question_id:
        return stats_missing_reference(stats_types)
    if reference_question_id and not _UUID_RE.match(reference_question_id):
        return stats_bad_reference_id(reference_question_id)

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
    return _format_stats_response(response.json())


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


class FeatureInfluenceInput(BaseModel):
    target: InfluenceVariable
    features: list[InfluenceVariable] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Candidate questions/components. Leave empty to use every other compatible "
            "numeric measure in the target survey"
        ),
    )
    alpha: float = Field(
        0.05, gt=0.0, lt=1.0,
        description=(
            "Significance level. Omit unless the user explicitly supplies one; the server "
            "automatically uses 0.05."
        ),
    )


_INFLUENCE_RESAMPLES = 999
_MAX_EXPANDED_INFLUENCE_FEATURES = 30


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
         (s.id = CAST(:scope_survey_id AS uuid)
          OR s.id = CAST(:benchmark_id AS uuid)
          OR (:organization_id IS NOT NULL
              AND s.organization_id = CAST(:organization_id AS uuid)))
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
    params: dict[str, Any] = {
        "scope_survey_id": _SCOPE["survey_id"],
        "organization_id": _SCOPE.get("organization_id"),
        "benchmark_id": BENCHMARK_SCOPE["survey_id"],
        "target_qid": target.question_id,
        "include_all_features": not features,
    }
    for index, variable in enumerate(variables):
        params[f"slot_{index}"] = index
        params[f"role_{index}"] = "target" if index == 0 else "feature"
        params[f"qid_{index}"] = variable.question_id
        params[f"component_{index}"] = variable.component_label or None
    with _sql_engine().connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
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


def _round_influence_output(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _round_influence_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_influence_output(item) for item in value]
    return value


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


def analyze_feature_influence(
    target: InfluenceVariable,
    features: list[InfluenceVariable] | None = None,
    alpha: float = 0.05,
) -> str:
    """Fetch aligned survey ratings and test each feature in one joint model per target."""
    target = InfluenceVariable.model_validate(target)
    features = [InfluenceVariable.model_validate(feature) for feature in (features or [])]
    variables = [target, *features]
    invalid_ids = [variable.question_id for variable in variables
                   if not _UUID_RE.match(variable.question_id)]
    if invalid_ids:
        return f"Feature influence unavailable: invalid question UUID(s): {invalid_ids}."
    if not _scope_published():
        return "Feature influence unavailable: the run's survey scope is not established."

    try:
        rows = _fetch_influence_rows(target, features)
        target_specs, feature_specs, excluded = _resolve_influence_specs(
            rows, target, features
        )
        if len(feature_specs) > _MAX_EXPANDED_INFLUENCE_FEATURES:
            raise ValueError(
                f"The requested questions expand to {len(feature_specs)} candidate features; "
                f"the maximum is {_MAX_EXPANDED_INFLUENCE_FEATURES}. Name a smaller feature set."
            )
    except PermissionError as exc:
        print(f"[feature-influence] REFUSED: {exc}")
        return f"Feature influence refused: {exc}"
    except ValueError as exc:
        return f"Feature influence unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 - returned as repairable tool feedback
        print(f"[feature-influence] database failure: {exc}")
        return f"Feature influence unavailable: database retrieval failed ({exc})."

    multiple_targets = len(target_specs) > 1

    def run_target(item: dict[str, Any]) -> dict[str, Any]:
        x, y, groups, products, names, provenance = _prepare_target_data(item, feature_specs)
        target_result: dict[str, Any] = {
            "target": provenance["target"],
            "provenance": provenance,
        }
        try:
            report, info, correlated_groups = feature_influence(
                x, y, groups=groups, products=products, feature_names=names,
                alpha=alpha,
                n_resamples=_INFLUENCE_RESAMPLES,
                max_workers=1 if multiple_targets else 4,
            )
        except ValueError as exc:
            target_result.update({
                "status": "insufficient_data",
                "reason": str(exc),
                "conclusion": {"kind": "insufficient_data", "features": []},
            })
            return target_result

        warnings = []
        if info.get("max_abs_quadratic_residual_corr") is not None \
                and info["max_abs_quadratic_residual_corr"] >= 0.20:
            warnings.append(
                "Residual curvature warning: a purely linear model may miss part of the pattern."
            )
        if any(row["collinearity_flag"] for row in report):
            warnings.append(
                "Some features overlap strongly, so their individual contributions are not "
                "reliably separable."
            )
        target_result.update({
            "status": "ok",
            "model": info,
            "features": report,
            "correlated_feature_sets": correlated_groups,
            "warnings": warnings,
            # Filled after all target components finish so one Holm family covers the
            # complete response rather than correcting each component independently.
            "conclusion": None,
        })
        return target_result

    if multiple_targets:
        with ThreadPoolExecutor(
            max_workers=min(len(target_specs), 4),
            thread_name_prefix="influence-target",
        ) as executor:
            analyses = list(executor.map(run_target, target_specs))
    else:
        analyses = [run_target(target_specs[0])]

    successful = [analysis for analysis in analyses if analysis["status"] == "ok"]
    global_tests = [
        analysis for analysis in successful
        if analysis["model"].get("global_raw_p_value") is not None
    ]
    for analysis, adjusted in zip(
        global_tests,
        holm_adjust([
            analysis["model"]["global_raw_p_value"] for analysis in global_tests
        ]),
        strict=True,
    ):
        analysis["model"]["global_adjusted_p_value"] = adjusted
        analysis["model"]["global_significant"] = adjusted <= alpha

    feature_tests = [
        (analysis, row)
        for analysis in successful
        for row in analysis["features"]
        if row.get("raw_p_value") is not None
    ]
    for (analysis, row), adjusted in zip(
        feature_tests,
        holm_adjust([row["raw_p_value"] for _analysis, row in feature_tests]),
        strict=True,
    ):
        row["adjusted_p_value"] = adjusted
        row["significant"] = bool(
            analysis["model"].get("global_significant") and adjusted <= alpha
        )
        # Retained as a compatibility alias for consumers of the earlier payload.
        row["influential"] = row["significant"]

    for analysis in successful:
        analysis["features"].sort(key=lambda row: (
            not row["significant"],
            row["adjusted_p_value"] if row["adjusted_p_value"] is not None else 1.0,
            -row.get("partial_r2", 0.0),
        ))
        significant_features = [
            row["feature"] for row in analysis["features"] if row["significant"]
        ]
        global_significant = bool(analysis["model"].get("global_significant"))
        if significant_features:
            kind = "influential_subset"
        elif global_significant and analysis["features"]:
            kind = "attribution_unresolved"
            analysis["warnings"].append(
                "The candidates are jointly associated with the target, but no individual "
                "feature remains significant after correction; attribution is unresolved."
            )
        else:
            kind = "none_qualified"
        analysis["conclusion"] = {
            "kind": kind,
            "significant_features": significant_features,
            "independent_features": significant_features,
            "attribution_unresolved": kind == "attribution_unresolved",
        }

    qualified = [
        {
            "target": analysis["target"]["label"],
            "significant_features": analysis["conclusion"].get(
                "significant_features", []
            ),
            "independent_features": analysis["conclusion"].get("independent_features", []),
        }
        for analysis in analyses
        if analysis["conclusion"]["kind"] == "influential_subset"
    ]
    none_qualified = [
        analysis["target"]["label"] for analysis in analyses
        if analysis["conclusion"]["kind"] == "none_qualified"
    ]
    unresolved = [
        analysis["target"]["label"] for analysis in analyses
        if analysis["conclusion"]["kind"] == "attribution_unresolved"
    ]
    insufficient = [
        analysis["target"]["label"] for analysis in analyses
        if analysis["conclusion"]["kind"] == "insufficient_data"
    ]
    if qualified:
        overall_kind = "influential_by_target"
    elif unresolved:
        overall_kind = "attribution_unresolved"
    elif len(insufficient) == len(analyses):
        overall_kind = "insufficient_data"
    else:
        overall_kind = "none_qualified"
    status = (
        "insufficient_data" if len(insufficient) == len(analyses)
        else "partial" if insufficient else "ok"
    )
    payload = {
        "status": status,
        "alpha": alpha,
        "multiple_testing_correction": "holm_fwer",
        "n_resamples": _INFLUENCE_RESAMPLES,
        "model_family": "additive_linear_with_product_controls_when_applicable",
        "feature_selection": "provided" if features else "all_other_compatible_numeric",
        "candidate_features": [
            {"question_id": item["question_id"], "label": item["name"]}
            for item in feature_specs
        ],
        "excluded_candidates": excluded,
        "analyses": analyses,
        "conclusion": {
            "kind": overall_kind,
            "qualifying_by_target": qualified,
            "targets_with_no_qualifying_features": none_qualified,
            "targets_with_unresolved_attribution": unresolved,
            "targets_with_insufficient_data": insufficient,
        },
    }
    return FEATURE_INFLUENCE_RESULT_HEAD + "\n" + json.dumps(
        _round_influence_output(payload), ensure_ascii=False
    )


analyze_feature_influence.__doc__ = ANALYZE_FEATURE_INFLUENCE_DESCRIPTION
analyze_feature_influence = tool(
    args_schema=FeatureInfluenceInput
)(analyze_feature_influence)


def build_tools():
    return [
        nl2sql_tool,
        get_survey_analysis_packet,
        run_survey_stats,
        analyze_feature_influence,
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
# The strong planner remains at the measured low default. The weak writer uses medium by default
# so final synthesis gets more reasoning without slowing the strong SQL-planning turn. Either can
# be tuned independently through its model-specific environment variable.
llm_strong = ChatOpenAI(
            model=STRONG_MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            api_key=OPENAI_API_KEY,
            **_STRONG_LLM_TUNING
        )

llm_weak = ChatOpenAI(
            model=WEAK_MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            api_key=OPENAI_API_KEY,
            **_WEAK_LLM_TUNING
        )


model = llm_strong.bind_tools(tools=tools, parallel_tool_calls=True)
# Tools stay bound on the weak model too: if it turns out more SQL is needed after the
# switch, weak can still call nl2sql_tool. call_tool promotes back to strong when the
# cheap path reports an error or another open SQL gap.
weak_model = llm_weak.bind_tools(tools=tools, parallel_tool_calls=True)




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


def message_to_text(msg: BaseMessage) -> str:
    """Return a clean string from LangChain message content across providers."""
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


def print_message(msg: BaseMessage) -> None:
    """Print one message with a role label, splitting tool calls from text."""
    kind = {"human": "HUMAN MESSAGE", "system": "SYSTEM MESSAGE",
            "ai": "AI MESSAGE", "tool": "TOOL OUTPUT"}.get(msg.type, msg.type.upper())

    text_part = message_to_text(msg)
    tool_calls = getattr(msg, "tool_calls", None) or []

    if msg.type == "tool":
        print(f"\n===== TOOL OUTPUT ({msg.name}) =====")
        print(text_part)
        return

    print(f"\n===== {kind} =====")
    print(text_part if text_part else "(no text — tool call only)")

    for tc in tool_calls:
        print(f"\n===== TOOL CALL ({tc['name']}) =====")
        args = tc.get("args")
        print(args if isinstance(args, str) else json.dumps(args, indent=2, ensure_ascii=False))




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
    # Persona turns keep the strong writer for evidence synthesis and presentation.
    persona_request: NotRequired[bool]
    # Successful on-demand packets use the lean prompt but retain the strong model for the
    # cross-survey semantic match and arithmetic in the closing turn.
    analysis_packet_done: NotRequired[bool]
    # A definitive influence analysis also closes retrieval but needs the strong model to turn
    # diagnostics into an accurate, general-audience conclusion.
    feature_influence_done: NotRequired[bool]
    number_of_steps: int
    sql_done: bool  # set from nl2sql_tool's more_sql_expected metadata
    # Running progress ledger, carried turn-to-turn outside the raw message history so
    # call_model can show it back to the LLM explicitly (see PROGRESS TRACKER below)
    # instead of relying on the model to notice its own prior tool_call args buried in
    # the conversation. Updated in call_tool from each nl2sql_tool call's own arguments.
    progress_gathered: str
    progress_needed: str
    # Consecutive nl2sql_tool calls that returned 0 rows. Two in a row means the query
    # *shape* is wrong, not its filters, so the guardrail stops asking for another variant.
    zero_row_streak: int



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
# scale_points reads question_option."optionSettings"->'positionLabels', NOT
# question.settings->'positionLabels'. The latter was used here and is wrong: across all 9,138
# question rows in this database, ZERO have a 'positionLabels' key in settings, so
# jsonb_array_length(...) was NULL and every inventory ever built reported scale_points=null.
# 7,032 question_option rows do carry it, as an array of {label, position} with position
# 0-based -- so its length is the number of scale positions (verified: 16 on the default
# survey's Appearance/Aroma/Texture, whose observed answers run 1-15, i.e. position 0 unused).
# The value is consistent across the options of one question (checked: 1 distinct length per
# question), so MAX is a safe reducer rather than a guess between differing values.
#
# It is computed in the outer SELECT, not in `scored`. In `scored` the correlated subquery
# would run once per answered option row; here it runs once per question.
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
# Keep the three inventory sections in one statement: `scored` is shared with Section B, and
# PostgreSQL gives the products and both measure summaries one statement-level snapshot.
_INV_SQL = (
"""WITH scored AS MATERIALIZED (
  SELECT q.id qid,q.prompt,q."typeOfQuestion" qtype,p.name product,
         COALESCE(qo.label,'(unlabelled)') attr,
         (aqo."answerData"->>'optionAnswer')::numeric score,a.enrollment_id
  FROM question q JOIN answer a ON a.question_id=q.id
  JOIN answered_question_options aqo ON aqo.answer_id=a.id
  JOIN product p ON p.id=a.product_id
  LEFT JOIN question_option qo
         ON qo.id=COALESCE(aqo.matrix_row_option_id,aqo.question_option_id)
  WHERE q."surveyId"=:survey_id
    AND aqo."answerData"->>'optionAnswer' ~ '^-?[0-9]+(\\.[0-9]+)?$'
), per AS (
  SELECT qid,prompt,qtype,product,COUNT(score) n,COUNT(DISTINCT enrollment_id) r,
         ROUND(AVG(score),2) mean,ROUND(STDDEV_SAMP(score),2) sd,MIN(score) lo,MAX(score) hi,
         CASE WHEN COUNT(score)>COUNT(DISTINCT enrollment_id)
              THEN COUNT(DISTINCT attr) ELSE 1 END na
  FROM scored GROUP BY qid,prompt,qtype,product
), attr_m AS (
  SELECT qid,attr,product,COUNT(score) n,ROUND(AVG(score),2) mean,
         ROUND(STDDEV_SAMP(score),2) sd
  FROM scored GROUP BY qid,attr,product
), attr_agg AS (
  SELECT qid,jsonb_agg(jsonb_build_object('attribute',attr,'product',product,'n',n,
           'mean',mean,'sd',sd) ORDER BY attr,mean DESC,product) ja
  FROM attr_m GROUP BY qid
), flips AS (
  SELECT a.qid,jsonb_agg(a.attr ORDER BY a.attr) fa
  FROM (SELECT qid,attr,array_agg(product ORDER BY mean DESC,product) ao
        FROM attr_m GROUP BY qid,attr) a
  JOIN (SELECT qid,array_agg(product ORDER BY mean DESC,product) po
        FROM per GROUP BY qid) p ON p.qid=a.qid
  WHERE a.ao <> p.po GROUP BY a.qid
), scored_summary AS (
SELECT jsonb_agg(x ORDER BY x->>'prompt') AS measures FROM (
  SELECT jsonb_build_object('qid',per.qid,'prompt',prompt,'type',qtype,
         'scale_points',(SELECT MAX(jsonb_array_length(qo."optionSettings"->'positionLabels'))
                         FROM question_option qo WHERE qo.question_id=per.qid),
         'attributes_pooled',MAX(na),
         'observed',jsonb_build_array(MIN(lo),MAX(hi)),
         'by_product',jsonb_agg(jsonb_build_object('product',product,'n',n,
           'respondents',r,'mean',mean,'sd',sd) ORDER BY mean DESC,product),
         'by_attribute',CASE WHEN MAX(na)>1 THEN aa.ja END)
         || CASE WHEN MAX(na)>1
                 THEN jsonb_build_object('order_differs',COALESCE(f.fa,'[]'::jsonb))
                 ELSE '{}'::jsonb END x
  FROM per LEFT JOIN attr_agg aa ON aa.qid=per.qid
             LEFT JOIN flips f ON f.qid=per.qid
  GROUP BY per.qid,prompt,qtype,aa.ja,f.fa) y
),
"""

# Section B exists because of a measured failure. An earlier version had section A only, and
# `JOIN product` plus the numeric-optionAnswer filter silently dropped ranking questions, matrix
# questions (whose values live in optionLabel, not optionAnswer) and any non-product-linked
# scale. On a survey whose only preference measure is an unusable ranking, that made the ranking
# invisible -- so the agent could not report that it existed and could not be attributed, which
# was the correct answer. An inventory that hides evidence is worse than no inventory: a measure
# removed upstream is indistinguishable from a measure that does not exist.
#
# The join to answered_question_options is LEFT for exactly that reason, and it is the fourth
# instance of the same bug class. An open-answer question has an `answer` row but NO option row --
# the text lives in the answer itself -- so an inner join dropped every free-text question in the
# database. Measured on survey 39af3240 (open-answer: 32 answer rows, 0 option rows): "What did
# you like most about the sample?" and "...dislike most..." each have 16 answers and neither
# appeared, so the agent reported the three demographics as if they were the only other questions
# and had no way to know otherwise -- the preamble tells it not to re-query. Across all 1,347
# surveys the fix reveals hidden questions in 150 of them, and 17 surveys whose "other measures"
# list was entirely EMPTY now return one to three questions. Cost is +22% on this section only
# (+433 chars on the survey above, +1,516 on the largest); INVENTORY_MAX_CHARS still has
# ample headroom.
#
# COUNT(*) remains the legacy expanded value-row count: an open answer contributes one row, while
# an option-backed answer can contribute several. `submissions` separately counts answer records,
# so the model can report how many answers were actually submitted without discarding the older
# value-grain evidence or silently redefining an established field.
"""other_summary AS (
SELECT jsonb_agg(jsonb_build_object('qid',qid,'prompt',prompt,'type',qtype,
       'product_linked',prod_linked,'answers',answers,'submissions',submissions,
       'respondents',resp,
       'sample_option_labels',to_jsonb(labels)) ORDER BY prompt) AS measures
FROM (
  SELECT q.id qid,q.prompt,q."typeOfQuestion" qtype,
         bool_or(a.product_id IS NOT NULL) prod_linked,
         COUNT(*) answers,COUNT(DISTINCT a.id) submissions,
         COUNT(DISTINCT a.enrollment_id) resp,
         (array_agg(DISTINCT aqo."answerData"->>'optionLabel')
            FILTER (WHERE aqo."answerData" ? 'optionLabel'))[1:12] labels
  FROM question q JOIN answer a ON a.question_id=q.id
  LEFT JOIN answered_question_options aqo ON aqo.answer_id=a.id
  WHERE q."surveyId"=:survey_id
    AND q.id NOT IN (SELECT DISTINCT qid FROM scored)
  GROUP BY q.id,q.prompt,q."typeOfQuestion") t
)
SELECT s.state,s."isActive" AS is_active,
       jsonb_build_object(
         'products',(SELECT jsonb_agg(jsonb_build_object('product',p.name,
                     'blindingNumber',p."blindingNumber") ORDER BY p.name)
                     FROM product p WHERE p."surveyId"=s.id),
         'scored_measures_by_product',scored_summary.measures,
         'other_answered_measures',other_summary.measures,
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
CROSS JOIN scored_summary CROSS JOIN other_summary
WHERE s.id=:survey_id
"""
)


# Persona retrieval is deliberately separate from _INV_SQL. Most questions do not ask for a
# respondent profile, and attaching every categorical distribution to every run would make the
# eager inventory materially larger. A persona-like prompt activates this one-statement route.
#
# The query does not decide which prompts "mean demographics". It returns the complete set of
# answered, non-product multiple-choice questions with full prompt text; semantic classification
# remains the model's job. Filtering prompts in SQL would reproduce the evidence-destroying
# semantic-filter problem that nl2sql_tool rejects.
_PERSONA_SQL = r"""
WITH survey_base AS (
  SELECT s.state,s."isActive" AS is_active,
         (SELECT COUNT(*) FROM enrollment e WHERE e.survey_id=s.id) AS survey_enrollments,
         (SELECT COUNT(DISTINCT a.enrollment_id) FROM answer a
            JOIN enrollment e ON e.id=a.enrollment_id WHERE e.survey_id=s.id) AS survey_respondents
  FROM survey s WHERE s.id=:survey_id
), eligible_questions AS (
  SELECT q.id AS qid,q.prompt,q."typeOfQuestion" AS qtype,q.settings,
         COUNT(DISTINCT a.enrollment_id) AS respondents
  FROM question q JOIN answer a ON a.question_id=q.id
  WHERE q."surveyId"=:survey_id AND q."typeOfQuestion"='multiple-choice'
    AND NOT COALESCE(a."isSkipped",false)
  GROUP BY q.id,q.prompt,q."typeOfQuestion",q.settings
  HAVING NOT COALESCE(BOOL_OR(a.product_id IS NOT NULL),false)
), selections AS (
  SELECT eq.qid,a.id AS answer_id,a.enrollment_id,
         COALESCE(NULLIF(BTRIM(qo.label),''),
                  NULLIF(BTRIM(aqo."answerData"->>'optionAnswer'),''),
                  '(unlabelled)') AS label
  FROM eligible_questions eq JOIN answer a ON a.question_id=eq.qid
  JOIN answered_question_options aqo ON aqo.answer_id=a.id
  LEFT JOIN question_option qo ON qo.id=aqo.question_option_id
  WHERE NOT COALESCE(a."isSkipped",false)
), answer_shapes AS (
  SELECT qid,answer_id,COUNT(DISTINCT label) AS selections_per_answer
  FROM selections GROUP BY qid,answer_id
), question_shapes AS (
  SELECT qid,MAX(selections_per_answer) AS max_selections_per_answer
  FROM answer_shapes GROUP BY qid
), category_counts AS (
  SELECT qid,label,COUNT(DISTINCT enrollment_id) AS category_respondents
  FROM selections GROUP BY qid,label
)
SELECT sb.state,sb.is_active,sb.survey_enrollments,sb.survey_respondents,
       eq.qid::text AS qid,eq.prompt,eq.qtype,
       LOWER(eq.settings->>'answer-type') AS configured_answer_type,
       COALESCE(qs.max_selections_per_answer,0) AS max_selections_per_answer,
       eq.respondents,cc.label,COALESCE(cc.category_respondents,0) AS category_respondents
FROM survey_base sb
LEFT JOIN eligible_questions eq ON true
LEFT JOIN question_shapes qs ON qs.qid=eq.qid
LEFT JOIN category_counts cc ON cc.qid=eq.qid
ORDER BY eq.prompt,cc.category_respondents DESC,cc.label
"""


_PERSONA_REQUEST_RE = re.compile(
    r"\b(?:consumer\s+|respondent\s+|audience\s+|customer\s+)?personas?\b|"
    r"\b(?:consumer|respondent|audience)\s+profile\b|"
    r"\bprofile\s+(?:of\s+)?(?:this\s+)?survey(?:'s)?\s+respondents\b|"
    r"\b(?:survey|respondent|consumer|audience)\s+demographics?\b|"
    r"\bdemographics?\s+(?:of|for)\s+(?:this\s+)?survey\b|"
    r"\bdemographic\s+(?:profile|breakdown|distribution)\b|"
    r"\b(?:respondent|consumer|audience)\s+(?:motivations?|loyalty|behaviors?|attitudes?)\b|"
    r"\b(?:motivations?\s+and\s+loyalty|loyalty\s+and\s+motivations?)\b",
    re.IGNORECASE,
)


def is_persona_request(prompt: str) -> bool:
    """Whether a question should receive the route-specific persona inventory."""
    return bool(_PERSONA_REQUEST_RE.search(prompt or ""))


def _wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float]:
    """Two-sided Wilson score interval as percentages, rounded for model-facing evidence."""
    if total <= 0:
        return [0.0, 0.0]
    p = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total) / denominator
    return [round(max(0.0, center - margin) * 100.0, 1),
            round(min(1.0, center + margin) * 100.0, 1)]


def _equal_share_p_value(first: int, second: int) -> tuple[float, str]:
    """Two-sided p-value for equal top/runner shares, conditional on either category.

    This is an exact central binomial test for ordinary survey sizes. A continuity-corrected
    normal approximation keeps pathological very-large packets from spending O(N) CPU solely
    on post-processing.
    """
    total = first + second
    if total <= 0:
        return 1.0, "unavailable"
    smaller = min(first, second)
    if total > 5000:
        z = max(0.0, abs(first - second) - 1.0) / math.sqrt(total)
        return min(1.0, math.erfc(z / math.sqrt(2.0))), "normal approximation"

    log_probabilities = [
        math.lgamma(total + 1)
        - math.lgamma(k + 1)
        - math.lgamma(total - k + 1)
        - total * math.log(2.0)
        for k in range(smaller + 1)
    ]
    largest = max(log_probabilities)
    log_cdf = largest + math.log(sum(math.exp(value - largest) for value in log_probabilities))
    return min(1.0, 2.0 * math.exp(log_cdf)), "exact binomial"


def _persona_dominance(distribution: list[dict[str, Any]], selection_mode: str) -> dict[str, Any]:
    """Conservative, explicit evidence for whether one single-select category leads."""
    if selection_mode not in {"single", "single_observed"}:
        return {
            "available": False,
            "significant": False,
            "reason": "overlapping multi-select categories cannot use a top-vs-runner test",
        }
    if len(distribution) < 2:
        return {
            "available": False,
            "significant": False,
            "reason": "fewer than two observed categories",
        }

    top, runner = distribution[0], distribution[1]
    raw_p, method = _equal_share_p_value(top["respondents"], runner["respondents"])
    comparisons = max(1, len(distribution) - 1)
    adjusted_p = min(1.0, raw_p * comparisons)
    return {
        "available": True,
        "top_label": top["label"],
        "runner_up_label": runner["label"],
        "alpha": 0.05,
        "raw_p_value": round(raw_p, 6),
        "adjusted_p_value": round(adjusted_p, 6),
        "significant": bool(top["respondents"] > runner["respondents"] and adjusted_p < 0.05),
        "method": (
            f"two-sided {method} for top vs runner-up among either category; "
            f"Bonferroni adjusted across {comparisons} comparison(s)"
        ),
    }
# The instruction block in front of the payload is agent_instructions.INVENTORY_PREAMBLE:
# how to read the inventory, how to answer a thin request, which measure to test, and
# the clickable-suggestion contract.


# Process-local and deliberately bounded: workers do not coordinate cache state. Cache the exact
# serialized JSON packet, which is shared by startup inventory and the on-demand tool; each caller
# adds its own stable preamble without re-querying or re-serializing the data.
_INVENTORY_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
_INVENTORY_CACHE_LOCK = threading.Lock()
_PERSONA_INVENTORY_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
_PERSONA_INVENTORY_CACHE_LOCK = threading.Lock()


def _inventory_cache_get(survey_id: str) -> str | None:
    now = time.monotonic()
    with _INVENTORY_CACHE_LOCK:
        cached = _INVENTORY_CACHE.get(survey_id)
        if cached is None:
            return None
        expires_at, inventory = cached
        if expires_at <= now:
            del _INVENTORY_CACHE[survey_id]
            return None
        _INVENTORY_CACHE.move_to_end(survey_id)
        return inventory


def _inventory_cache_put(survey_id: str, inventory: str, ttl_s: int) -> None:
    if not inventory or INVENTORY_CACHE_MAX_ENTRIES <= 0 or ttl_s <= 0:
        return
    with _INVENTORY_CACHE_LOCK:
        _INVENTORY_CACHE[survey_id] = (time.monotonic() + ttl_s, inventory)
        _INVENTORY_CACHE.move_to_end(survey_id)
        while len(_INVENTORY_CACHE) > INVENTORY_CACHE_MAX_ENTRIES:
            _INVENTORY_CACHE.popitem(last=False)


def _clear_inventory_cache() -> None:
    """Clear the process-local inventory cache (tests and explicit operational refreshes)."""
    with _INVENTORY_CACHE_LOCK:
        _INVENTORY_CACHE.clear()
    with _PERSONA_INVENTORY_CACHE_LOCK:
        _PERSONA_INVENTORY_CACHE.clear()


def _persona_inventory_cache_get(survey_id: str) -> str | None:
    now = time.monotonic()
    with _PERSONA_INVENTORY_CACHE_LOCK:
        cached = _PERSONA_INVENTORY_CACHE.get(survey_id)
        if cached is None:
            return None
        expires_at, inventory = cached
        if expires_at <= now:
            del _PERSONA_INVENTORY_CACHE[survey_id]
            return None
        _PERSONA_INVENTORY_CACHE.move_to_end(survey_id)
        return inventory


def _persona_inventory_cache_put(survey_id: str, inventory: str, ttl_s: int) -> None:
    if not inventory or INVENTORY_CACHE_MAX_ENTRIES <= 0 or ttl_s <= 0:
        return
    with _PERSONA_INVENTORY_CACHE_LOCK:
        _PERSONA_INVENTORY_CACHE[survey_id] = (time.monotonic() + ttl_s, inventory)
        _PERSONA_INVENTORY_CACHE.move_to_end(survey_id)
        while len(_PERSONA_INVENTORY_CACHE) > INVENTORY_CACHE_MAX_ENTRIES:
            _PERSONA_INVENTORY_CACHE.popitem(last=False)


def _survey_analysis_packet_payload(survey_id: str) -> str:
    """Return the shared serialized packet for `survey_id`, or "" if unavailable.

    Fails soft on every path: any DB error, an empty survey, or a payload over
    INVENTORY_MAX_CHARS returns "". Returning a TRUNCATED packet is deliberately not an option --
    a partial candidate list is the one outcome that silently changes the answer.
    """
    cached = _inventory_cache_get(survey_id)
    if cached is not None:
        print(f"[analysis-packet] cache hit ({len(cached)} chars)")
        return cached

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
                print("[analysis-packet] skipped (survey not found)")
                return ""
            packet = survey_row["packet"] or {}
            # jsonb objects have their own key order. Rebuild the established Python order so
            # startup and on-demand packets remain byte-identical to the established contract.
            block = {
                "products": packet.get("products"),
                "scored_measures_by_product": packet.get("scored_measures_by_product"),
                "other_answered_measures": packet.get("other_answered_measures"),
                "benchmark_context": packet.get("benchmark_context"),
            }
    except Exception as exc:  # noqa: BLE001 - the agent works without this
        print(f"[analysis-packet] skipped ({type(exc).__name__}: {exc})")
        return ""

    if not (
        block["scored_measures_by_product"]
        or block["other_answered_measures"]
        or block["benchmark_context"]
    ):
        print("[analysis-packet] skipped (survey has no inventory data)")
        return ""

    payload = json.dumps(block, ensure_ascii=False, default=str)
    if len(payload) > INVENTORY_MAX_CHARS:
        print(f"[analysis-packet] skipped ({len(payload)} chars > {INVENTORY_MAX_CHARS} limit) "
              "-- the agent will discover this survey via SQL")
        return ""

    measures = len(block["scored_measures_by_product"] or [])
    other = len(block["other_answered_measures"] or [])
    print(f"[analysis-packet] {len(payload)} chars: {measures} scored measure(s), {other} other")
    state = str(survey_row["state"] or "").lower()
    closed = not bool(survey_row["is_active"]) and state in {"closed", "archived"}
    if survey_id == BENCHMARK_SCOPE["survey_id"]:
        ttl_s = INVENTORY_CACHE_BENCHMARK_TTL_S
    else:
        ttl_s = INVENTORY_CACHE_CLOSED_TTL_S if closed else INVENTORY_CACHE_ACTIVE_TTL_S
    _inventory_cache_put(survey_id, payload, ttl_s)
    return payload


def survey_inventory(survey_id: str) -> str:
    """The startup inventory block for `survey_id`, or "" if unavailable."""
    payload = _survey_analysis_packet_payload(survey_id)
    return INVENTORY_PREAMBLE + payload if payload else ""


def _survey_persona_packet_payload(survey_id: str) -> str:
    """Return a complete demographic/behavior candidate packet, or "" if unavailable."""
    cached = _persona_inventory_cache_get(survey_id)
    if cached is not None:
        print(f"[persona-inventory] cache hit ({len(cached)} chars)")
        return cached

    try:
        with _sql_engine().connect() as conn:
            rows = conn.execute(text(_PERSONA_SQL), {"survey_id": survey_id}).mappings().all()
    except Exception as exc:  # noqa: BLE001 - persona requests can fall back to model-authored SQL
        print(f"[persona-inventory] skipped ({type(exc).__name__}: {exc})")
        return ""
    if not rows:
        print("[persona-inventory] skipped (survey not found)")
        return ""

    first = rows[0]
    if first.get("qid") is None:
        print("[persona-inventory] skipped (no answered non-product categorical questions)")
        return ""

    questions: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in rows:
        qid = row.get("qid")
        if not qid:
            continue
        question = questions.setdefault(qid, {
            "qid": qid,
            "prompt": row.get("prompt"),
            "type": row.get("qtype"),
            "selection_mode": "",
            "respondents": int(row.get("respondents") or 0),
            "coverage_pct": 0.0,
            "distribution": [],
        })
        label = row.get("label")
        if label is not None:
            category_n = int(row.get("category_respondents") or 0)
            denominator = question["respondents"]
            question["distribution"].append({
                "label": label,
                "respondents": category_n,
                "pct": round(100.0 * category_n / denominator, 1) if denominator else 0.0,
                "ci95_pct": _wilson_interval(category_n, denominator),
            })

        configured = str(row.get("configured_answer_type") or "").lower()
        max_selections = int(row.get("max_selections_per_answer") or 0)
        if max_selections <= 1:
            question["selection_mode"] = "single" if configured == "one" else "single_observed"
        else:
            question["selection_mode"] = "multiple"

    survey_respondents = int(first.get("survey_respondents") or 0)
    for question in questions.values():
        denominator = question["respondents"]
        question["coverage_pct"] = (
            round(100.0 * denominator / survey_respondents, 1) if survey_respondents else 0.0
        )
        question["distribution"].sort(
            key=lambda item: (-item["respondents"], str(item["label"]))
        )
        question["dominance"] = _persona_dominance(
            question["distribution"], question["selection_mode"]
        )

    packet = {
        "survey_enrollments": int(first.get("survey_enrollments") or 0),
        "survey_respondents": survey_respondents,
        "categorical_questions": list(questions.values()),
    }
    payload = json.dumps(packet, ensure_ascii=False, default=str)
    if len(payload) > PERSONA_INVENTORY_MAX_CHARS:
        print(
            f"[persona-inventory] skipped ({len(payload)} chars > "
            f"{PERSONA_INVENTORY_MAX_CHARS} limit) -- the agent will use scoped SQL"
        )
        return ""

    print(
        f"[persona-inventory] {len(payload)} chars: "
        f"{len(packet['categorical_questions'])} categorical question(s)"
    )
    state = str(first.get("state") or "").lower()
    closed = not bool(first.get("is_active")) and state in {"closed", "archived"}
    if survey_id == BENCHMARK_SCOPE["survey_id"]:
        ttl_s = INVENTORY_CACHE_BENCHMARK_TTL_S
    else:
        ttl_s = INVENTORY_CACHE_CLOSED_TTL_S if closed else INVENTORY_CACHE_ACTIVE_TTL_S
    _persona_inventory_cache_put(survey_id, payload, ttl_s)
    return payload


def survey_persona_inventory(survey_id: str) -> str:
    """Route-specific persona evidence for `survey_id`, including its model instructions."""
    payload = _survey_persona_packet_payload(survey_id)
    return PERSONA_INVENTORY_PREAMBLE + payload if payload else ""


# PG_DIALECT_RULES, REPORTING_RULES and build_system_prompt() are in agent_instructions.py.
# The assembly order is documented there; the assert below is the invariant it exists for.
SYSTEM_PROMPT = SystemMessage(content=SYSTEM_PROMPT_TEXT)
# Used once the agent is done querying: same instructions, no schema.
SYSTEM_PROMPT_LEAN = SystemMessage(content=SYSTEM_PROMPT_LEAN_TEXT)
# The whole point of putting the schema last -- if this ever stops holding, the prose turn
# silently starts paying full prefill again, which is exactly the bug that was invisible before.
assert SYSTEM_PROMPT.content.startswith(SYSTEM_PROMPT_LEAN.content), (
    "SYSTEM_PROMPT_LEAN must stay a literal prefix of SYSTEM_PROMPT, or turn 1 loses the cache"
)

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

    sql_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "nl2sql_tool"]
    sql_calls = [tool_calls[i] for i in sql_indices]
    sql_results = [results[i] for i in sql_indices]
    sql_args = [tc.get("args") if isinstance(tc.get("args"), dict) else {} for tc in sql_calls]

    # Classify the untouched results before adding batch-level warnings.
    heads = [result[:800] for result in sql_results]
    saw_sql_error = any("SQL error" in head or "Tool error:" in head for head in heads)
    zero_flags = [ZERO_ROW_HEAD in head for head in heads]
    zero_streak = state.get("zero_row_streak", 0)
    if sql_calls:
        # Simultaneous empty results are one retrieval attempt, not consecutive retries.
        zero_streak = zero_streak + 1 if all(zero_flags) else 0

    gathered = _merge_ledger_values(
        [args.get("information_gathered", "") for args in sql_args]
    )
    needed = _merge_ledger_values(
        [args.get("information_still_needed", "") for args in sql_args], gaps=True
    )

    warnings: list[str] = []
    if sql_calls:
        prev_needed = _normalize_gap(state.get("progress_needed"))
        if prev_needed and _normalize_gap(needed) == prev_needed:
            warnings.append(STALL_WARNING)
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

    # A complete packet closes the fixed candidate+aggregate prelude in one tool call. Route the
    # next turn through the lean writer just as a final SQL call would. Only a standalone,
    # positively identified packet result can latch; refusals/errors stay on strong+schema. If a
    # weak turn attempted a packet and it failed, reopen the strong route for the SQL fallback.
    packet_calls = [
        (tc, result) for tc, result in zip(tool_calls, results, strict=False)
        if tc.get("name") == "get_survey_analysis_packet"
    ]
    packet_complete = (
        len(tool_calls) == 1
        and len(packet_calls) == 1
        and packet_calls[0][1].startswith(ANALYSIS_PACKET_RESULT_HEAD)
    )
    if packet_complete and not state["sql_done"] and state["number_of_steps"] >= LATCH_MIN_STEP:
        update["sql_done"] = True
        update["analysis_packet_done"] = True
    elif packet_calls and state["sql_done"] and not packet_complete:
        update["sql_done"] = False
        update["analysis_packet_done"] = False
        print("[route] analysis packet unavailable -- promoting back to strong + schema")

    influence_calls = [
        (tc, result) for tc, result in zip(tool_calls, results, strict=False)
        if tc.get("name") == "analyze_feature_influence"
    ]
    influence_complete = (
        len(tool_calls) == 1
        and len(influence_calls) == 1
        and influence_calls[0][1].startswith(FEATURE_INFLUENCE_RESULT_HEAD)
    )
    if (influence_complete and not state["sql_done"]
            and state["number_of_steps"] >= LATCH_MIN_STEP):
        update["sql_done"] = True
        update["feature_influence_done"] = True
    elif influence_calls and state["sql_done"] and not influence_complete:
        update["sql_done"] = False
        update["feature_influence_done"] = False
        print("[route] feature influence unavailable -- promoting back to strong + schema")

    if sql_calls:
        update["progress_gathered"] = gathered
        update["progress_needed"] = needed

        # Missing metadata fails safe. The weak+lean closing route is selected only when every
        # query in the batch explicitly agrees that the whole batch closes every SQL gap.
        all_no_more = all(args.get("more_sql_expected") is False for args in sql_args)
        all_gaps_closed = all(
            bool(_normalize_gap(args.get("information_still_needed")))
            and not _gap_open(args.get("information_still_needed"))
            for args in sql_args
        )
        # A zero-row result invalidates the model's pre-call forecast that this query closes
        # the task. Keep (or restore) strong+schema routing until the requested join diagnostic
        # returns a real row establishing whether the two sides can be linked.
        saw_zero_rows = any(zero_flags)
        batch_complete = (
            all_no_more and all_gaps_closed and not saw_sql_error and not saw_zero_rows
        )
        batch_needs_more = not all_no_more or not all_gaps_closed or saw_zero_rows

        if (not state["sql_done"] and state["number_of_steps"] >= LATCH_MIN_STEP
                and batch_complete):
            update["sql_done"] = True
        elif state["sql_done"] and (saw_sql_error or batch_needs_more):
            update["sql_done"] = False
            why = (
                "SQL error" if saw_sql_error else
                "zero-row result" if saw_zero_rows else
                "still reporting an open gap"
            )
            print(f"[route] latched weak turn {why} -- promoting back to strong + schema")
    return update



# Short-term memory, trim side. Applied when the request is ASSEMBLED, not to the state: the
# checkpoint keeps the whole thread, and only what call_model re-sends is bounded. That is the
# non-destructive half of the trade -- a later round can still be replayed or inspected in full,
# and nothing is summarised, so no figure is ever restated by a model rather than retrieved.
#
# What is kept is the first message plus the last HISTORY_MAX_MESSAGES. The first is not
# sentiment about "the system message": message 0 here carries the SCOPE ids and the pre-fetched
# inventory, and the system prompt orders every query to use those ids, so a window that drops
# it produces SQL scoped to nothing. It is also why a follow-up question needs no preamble of
# its own -- see main().
def _trim_history(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    msgs = list(messages)
    # +1 for the always-kept first message: at that length the window IS the whole history.
    if HISTORY_MAX_MESSAGES <= 0 or len(msgs) <= HISTORY_MAX_MESSAGES + 1:
        return msgs

    # The window may never cut into the round in progress. Only round openers are HumanMessages
    # in this state -- call_model appends PROGRESS TRACKER and the step-budget notice to its
    # local list, never to state -- so the last one marks where the current question began.
    #
    # This is what keeps the patch additive. MAX_LLM_STEPS allows 12 turns, and one question can
    # therefore hold far more than HISTORY_MAX_MESSAGES messages on its own: measured over the
    # 101 recorded runs in needle_haystack_results/, 7 exceeded 11 messages and one reached 21.
    # Without this clamp those runs would have been trimmed MID-QUESTION, so the agent would have
    # answered from less evidence than the pre-memory build did -- a behaviour change smuggled in
    # under a memory feature. With it, a single question is assembled byte-for-byte as before and
    # trimming can only ever drop rounds that have already been answered.
    round_start = 0
    for i in range(len(msgs) - 1, 0, -1):
        if isinstance(msgs[i], HumanMessage):
            round_start = i
            break

    start = max(1, min(len(msgs) - HISTORY_MAX_MESSAGES, round_start))
    # A tool result whose AI tool_call fell outside the window is an orphan, and OpenAI rejects
    # the whole request rather than ignoring it ("messages with role 'tool' must be a response
    # to a preceding message with tool_calls"). Landing mid-round-trip is the normal case here,
    # not the rare one, since one SQL call is always two messages.
    #
    # So the boundary moves BACK to the AI turn that owns those results, rather than forward
    # past them. Both restore a valid request; only this one keeps the evidence. Dropping
    # forward at HISTORY_MAX_MESSAGES=1 would strip the ToolMessage the model was just woken up
    # to read, and it would answer -- or re-issue the same query -- having never seen its own
    # rows. The overshoot is one AI tool_call message, typically under 100 tokens.
    # start > 1 is not a paranoia guard: msgs[0] is the question and msgs[1] the first AI reply,
    # so the walk always halts on a real tool_call owner.
    while start > 1 and isinstance(msgs[start], ToolMessage):
        start -= 1
    return [msgs[0]] + msgs[start:]


def call_model(state: AgentState, config: RunnableConfig):
    # Step 0 always uses the strong model: the first turn almost always writes SQL.
    # After that this is a plain boolean read -- no router, no extra inference.
    sql_done = state["sql_done"] and state["number_of_steps"] > 0
    analysis_packet_done = bool(state.get("analysis_packet_done"))
    feature_influence_done = bool(state.get("feature_influence_done"))
    persona_request = bool(state.get("persona_request"))

    # No more SQL coming, so the schema is dead weight for writing prose.
    messages = [SYSTEM_PROMPT_LEAN if sql_done else SYSTEM_PROMPT] + _trim_history(state["messages"])

    # Explicit, by-design visibility into the running progress ledger -- rather than
    # relying on the model to notice its own information_gathered/information_still_needed
    # from a previous tool_call buried in the raw message history, state it back to the
    # model plainly before every decision it makes after the first. nl2sql_tool's
    # docstring tells the model to treat this as what it must update, not just restate.
    if state["number_of_steps"] > 0 and (state.get("progress_gathered") or state.get("progress_needed")):
        messages.append(
            HumanMessage(
                content=progress_tracker_message(
                    state.get("progress_gathered"), state.get("progress_needed")
                )
            )
        )

    # On the final allowed turn, call the LLM *without* tools bound: it cannot emit
    # another tool call, so it must answer from whatever it has already retrieved.
    if state["number_of_steps"] >= MAX_LLM_STEPS - 1:
        active_model = (
            llm_strong
            if persona_request or analysis_packet_done or feature_influence_done or not sql_done
            else llm_weak
        )
        messages.append(HumanMessage(content=STEP_BUDGET_NOTICE))
    else:
        # A packet replaces two mechanical SQL turns but leaves the hardest judgement -- choosing
        # a semantically compatible cross-survey instrument and comparing its figures -- for the
        # writer. Keep the lean prompt, but do not demote that closing work to the weak model.
        active_model = (
            model
            if persona_request or analysis_packet_done or feature_influence_done or not sql_done
            else weak_model
        )

    response = active_model.invoke(messages, config=config)
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
    persona_request: bool,
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
        "persona_request": persona_request,
        "analysis_packet_done": False,
        "feature_influence_done": False,
        # Everything below is per-QUESTION scratch, and every one of these channels is a plain
        # value with no reducer, so passing it here overwrites what the checkpoint holds. That
        # is what makes a second round work at all: resumed as-is, number_of_steps would still
        # be round 1's final count, should_continue would see the budget already spent, and the
        # follow-up would end without a single LLM turn. Only `messages` accumulates, because
        # add_messages is the one reducer on this state.
        "number_of_steps": 0,
        "sql_done": False,
        "progress_gathered": "",
        "progress_needed": "",
        "zero_row_streak": 0,
    }
    for st in agent_graph.stream(inputs, stream_mode="values", config=run_config):
        msgs = st["messages"]
        for msg in msgs[printed:]:
            print_message(msg)
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
    persona_request = is_persona_request(args.prompt)
    if not args.no_inventory and persona_request:
        question += survey_persona_inventory(args.survey_id)

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
        persona_request=persona_request,
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
        # _trim_history keeps regardless of how long the thread grows. The smaller persona
        # packet is attached on demand when a follow-up first activates that route.
        follow_up_question = follow_up
        follow_up_persona_request = is_persona_request(follow_up)
        if not args.no_inventory and follow_up_persona_request:
            # Persona evidence is route-specific rather than part of every eager inventory.
            # The packet cache makes repeated persona turns cheap while the current HumanMessage
            # keeps the evidence visible even when this is a late follow-up.
            follow_up_question += survey_persona_inventory(args.survey_id)
        printed = _run_round(
            agent_graph,
            follow_up_question,
            run_config,
            printed,
            thread_survey_id=args.survey_id,
            inventory_chars=len(inventory),
            persona_request=follow_up_persona_request,
        )


    # result = agent_graph.invoke(inputs)
    # print("\n--- AGENT FINAL RESPONSE---")
    # print(message_to_text(result["messages"][-1]))


if __name__ == "__main__":
    main()
