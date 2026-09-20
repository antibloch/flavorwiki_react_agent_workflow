"""The isolated attribute-to-KPI analysis workflow.

A WORKFLOW, not a ReAct agent. The stage order is fixed in code and the model never decides what
runs next -- it is called at three predetermined points for three language judgments that no rule
can make (new_tool.md §9). The alternative shape, an LLM loop deciding its own path, was rejected
because the tasks here are predefined: there is nothing for a loop to decide, and a loop that can
reorder statistical stages can also invalidate them.

Where the model IS called, and why each one cannot be a rule:
  - encoding (§9.2, before stage B) -- ordinality is not derivable from structure. This database
    holds `Dislike Extremely=1..Like Extremely=9` AND `Like Extremely=0..Dislike Extremely=8`, so
    a mechanical read of `order` or `analytical_value` inverts the sign of liking on the second.
    Only the labels say which end is which.
  - persona sections (§9.3, before stage D) -- naming and bounding the regions of attribute space
    worth calling personas. It runs BEFORE clustering on purpose: specifying regions after seeing
    cluster assignments makes the overlap analysis circular.
  - report (§9.4, after M/N) -- composing the result from stage outputs, computing nothing.

Isolation (§8.1). This module imports neither `agent_instructions` nor `tool_prompts`, and it does
NOT import `funda_agent_exp` either -- that would be circular, since the agent imports the
main-facing tool from here, and it would transitively pull in both main instruction modules. The
model name and the database accessor are therefore passed IN by the caller rather than reached for.
All model-facing text lives in `isolated_workflow_instructions`.

Every stage parks its output through `output_store` and passes only a result id forward, so no
stage output is ever serialized into a prompt (§8.5). Artifacts are discarded on success and
deliberately PRESERVED on failure, because a failed run is exactly when someone needs to look at
what the stages produced.

Stage skeleton status: the stage bodies are stubs. The graph, state, seed, bounds, failure routing
and artifact lifecycle are real and tested; the statistics arrive in later steps.
"""

import json
import math
import os
import re
from itertools import combinations
from typing import Annotated, Any, Callable, NamedTuple, NotRequired, Sequence, TypedDict

import numpy as np
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy import text

import output_store
from isolated_workflow_instructions import (
    ENCODING_RETRY_TEMPLATE,
    ENCODING_STEP_TEMPLATE,
    REPORT_RETRY_TEMPLATE,
    REPORT_STEP_TEMPLATE,
    SECTIONS_RETRY_TEMPLATE,
    SECTIONS_STEP_TEMPLATE,
    WORKFLOW_SEED_TEMPLATE,
    WORKFLOW_SYSTEM_PROMPT,
    workflow_stage_failure,
)


# Model turns are bounded at 6 plus a tool-free final turn (§8.2), the same shape as the drill's
# 4 + 1: on the last turn the model is invoked without tools bound, so it cannot emit another
# call and must compose from what it already has.
WORKFLOW_MAX_STEPS = int(os.getenv("ISOLATED_WORKFLOW_MAX_STEPS", "6"))
# 13 stage nodes plus the report node; the limit only has to exceed the longest path, and a
# generous bound costs nothing because the graph has no cycles.
WORKFLOW_RECURSION_LIMIT = int(os.getenv("ISOLATED_WORKFLOW_RECURSION_LIMIT", "64"))

# `result_id=` in the store manifest is load-bearing and documented as such in output_store; this
# reads the public contract rather than the module's private dict.
_RESULT_ID_RE = re.compile(r"result_id=([0-9a-f]+)")


# Runtime dependencies -- a database connection factory and the model name -- arrive through
# LangGraph's `configurable` channel rather than as module state or as fields on PipelineState.
#
# Why not import them: this module must keep no import edge to funda_agent_exp (see the module
# docstring), so the engine cannot be reached for; it has to be handed in.
# Why config and not state: a per-run callable in state would be checkpointed if a checkpointer
# were ever attached, and module-level state would not survive two concurrent runs. `configurable`
# is the channel LangGraph provides for exactly this, and it is already how the main agent's
# call_model receives its prompt-cache key.
def _runtime(config: RunnableConfig | None, key: str):
    """Return one runtime dependency, or raise naming what the caller failed to supply."""
    configurable = (config or {}).get("configurable") or {}
    value = configurable.get(key)
    if value is None:
        raise RuntimeError(
            f"the isolated workflow was invoked without {key!r}; the caller must supply it "
            "because this module deliberately imports nothing from the agent"
        )
    return value


class PipelineState(TypedDict):
    """State for one isolated run.

    Separate from the main agent's `AgentState` -- the first of the four isolation mechanisms.
    Its `messages` channel holds only this workflow's own prompt, seed and turns, so the main
    conversation cannot leak in and this run's reasoning cannot leak out.

    `artifacts` holds result IDS ONLY, never data (§8.3). `log` is surfaced only inside a failure
    diagnostic.
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]
    steps: int
    stage: str
    decisions: dict[str, Any]
    artifacts: dict[str, str]
    log: list[str]
    dataset_id: NotRequired[str]
    variable_specs: NotRequired[dict]
    distribution_report: NotRequired[dict]
    persona_sections: NotRequired[dict]
    analysis_result: NotRequired[dict]
    still_missing: NotRequired[str]
    failed_stage: NotRequired[str]
    failure_detail: NotRequired[str]


class WorkflowRun(NamedTuple):
    """What one run hands back to the caller.

    `text` is the only thing that may enter the main conversation. `artifacts` is returned so the
    caller can track parked ids and know what survived a failure; `still_missing` feeds the main
    side's `analysis_missing`.
    """

    text: str
    artifacts: dict[str, str]
    still_missing: str
    failed_stage: str
    # The persona profiles, for the caller to keep in durable state. Still the workflow's OUTPUT,
    # never its transcript, so isolation holds -- but it has to outlive the ToolMessage that
    # delivers it, which main history drops after HISTORY_MAX_ROUNDS rounds.
    persona_profiles: dict[str, Any] | None = None


# ============================================================================
# Stage order
# ============================================================================
#
# §17's emitted contract string is "A->B->C/E->G->D->F->I->J->K->L->M/N", and that is the order
# here. Node keys are identifier-safe; the labels are what appears in diagnostics and in the
# emitted pipeline string, because "stage ce failed" is less useful to read than "stage C/E".
#
# The three model steps sit at their specified points rather than at the end: encoding before B
# because normalization needs to know what each value MEANS, sections before D so regions are
# specified without seeing the clusters, and report after M/N when every number exists.
_STAGE_LABELS: dict[str, str] = {
    "stage_a": "A (extract survey responses)",
    "step_encoding": "9.2 (encoding resolution)",
    "stage_b": "B (normalize)",
    "stage_ce": "C/E (attribute and KPI metrics)",
    "step_sections": "9.3 (choose K, specify persona sections)",
    "stage_g": "G (resolve persona sections)",
    "stage_d": "D (k-means)",
    "stage_f": "F (clusters)",
    "stage_i": "I (maximum-overlap analysis)",
    "stage_j": "J (persona clusters)",
    "stage_k": "K (centroids)",
    "stage_l": "L (PLSR)",
    "stage_mn": "M/N (fit and correlation)",
}

_STAGE_SEQUENCE: tuple[str, ...] = tuple(_STAGE_LABELS)

REPORT_NODE = "step_report"

# The stage-only pipeline string echoed in the output contract (§17). The model steps are not in
# it: it describes the computation, not the turns.
PIPELINE_CONTRACT = "A->B->C/E->G->D->F->I->J->K->L->M/N"


# ============================================================================
# Seed
# ============================================================================


def _render_variables(variables: Sequence[dict[str, Any]] | None) -> str:
    """Render a variable list deterministically.

    Caller order is preserved because it is meaningful (the analyst's ordering), while each
    variable's keys are sorted, so the same request always produces a byte-identical seed. That
    matters for reproducibility and for prompt caching -- a seed that reorders itself run to run
    is a cache miss and an unreproducible run.
    """
    if not variables:
        return "(none)"
    return json.dumps(list(variables), sort_keys=True, ensure_ascii=False, default=str)


def build_seed(
    survey_id: str,
    attributes: Sequence[dict[str, Any]],
    kpis: Sequence[dict[str, Any]],
    screeners: Sequence[dict[str, Any]] | None = None,
    k: int | None = None,
    normalization: str = "design_range_zscore",
    random_state: int = 20260819,
    requested_metrics: Sequence[str] | None = None,
    persona_sections: dict[str, Any] | None = None,
) -> HumanMessage:
    """Build the one seed message this run receives.

    Deliberately assembled from the caller's ARGUMENTS only. The main agent's messages, system
    prompt, checkpointer, thread id and inventory packet are not parameters here, so there is no
    path by which they could be interpolated -- the isolation is structural rather than a rule
    someone has to remember.
    """
    return HumanMessage(content=WORKFLOW_SEED_TEMPLATE.format(
        survey_id=survey_id,
        attributes=_render_variables(attributes),
        kpis=_render_variables(kpis),
        screeners=_render_variables(screeners),
        k="decide (evaluate 4 and 5)" if k is None else k,
        normalization=normalization,
        random_state=random_state,
        requested_metrics=(
            ", ".join(requested_metrics) if requested_metrics else "(defaults)"
        ),
        persona_sections=(
            json.dumps(persona_sections, sort_keys=True, ensure_ascii=False)
            if persona_sections else "(propose them)"
        ),
        stage_order=PIPELINE_CONTRACT,
    ))


# ============================================================================
# Stage A -- extraction
# ============================================================================
#
# One statement, returning RAW answer rows with every candidate value field, and the per-type
# value path resolved in Python below. Two reasons for that split rather than per-type SQL:
# one statement gives every variable a single snapshot, and the type rules are the part most
# likely to be read and argued with, so they belong in code a person can follow.
#
# The join shape does the matrix work. `comp` resolves the COMPONENT on
# COALESCE(matrix_row_option_id, question_option_id) and `col` resolves the VALUE on
# question_option_id alone. For a matrix those differ -- the row is the attribute, the column is
# the value -- and for everything else they land on the same option row, which is exactly what a
# multiple-choice needs, since there the option's own label IS the answer. Keyed on
# question_option_id alone a matrix would report its scale points as attributes; that was observed
# in a live agent run writing its own join, and is why this lives here instead of in a prompt.
#
# LEFT JOIN throughout, deliberately: an open-answer question has an `answer` row and no option
# row at all, and an inner join silently deleted every free-text question in an earlier version of
# the inventory query. Same bug class, same fix.
_STAGE_A_SQL = """
SELECT a.enrollment_id::text            AS enrollment_id,
       a.product_id::text               AS product_id,
       p.name                           AS product_name,
       a.question_id::text              AS question_id,
       q."typeOfQuestion"               AS question_type,
       e.enrollment_status              AS enrollment_status,
       COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)::text AS component_id,
       comp.label                       AS component_label,
       col.label                        AS value_label,
       col.analytical_value             AS value_code,
       aqo."answerData"->>'optionAnswer' AS option_answer,
       aqo."answerData"->>'optionLabel'  AS option_label,
       aqo."answerData"->>'rank'         AS option_rank,
       a.value                          AS text_value
FROM answer a
JOIN question q ON q.id = a.question_id
JOIN enrollment e ON e.id = a.enrollment_id
LEFT JOIN product p ON p.id = a.product_id
LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
LEFT JOIN question_option comp
       ON comp.id = COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)
LEFT JOIN question_option col ON col.id = aqo.question_option_id
WHERE q."surveyId" = CAST(:survey_id AS uuid)
  AND a.question_id = ANY(CAST(:question_ids AS uuid[]))
  AND NOT COALESCE(a."isSkipped", false)
ORDER BY a.question_id, a.enrollment_id, a.product_id, component_id
"""

# Value paths per §4.1, measured per type. Grouped by WHERE THE VALUE LIVES, because that is the
# only thing that varies -- and getting it wrong is silent, not loud.
#
#   numeric_answer  the value is a number in answerData->>'optionAnswer', and the OPTION is the
#                   sub-item. A line-scale battery's option IS the attribute.
#   option_label    the value is the joined question_option.label. multiple-choice carries
#                   optionAnswer on only 155,863 of 163,607 rows (95%), so reading the payload
#                   loses 5% of valid answers -- the label is always there.
#   matrix          row label = attribute, column label = value, column analytical_value = scale.
#   ranking         the value is answerData->>'rank'; there is no optionAnswer at all.
#   open_text       the value is answer.value, and there is no option row.
_NUMERIC_ANSWER_TYPES = frozenset({
    "vertical-rating", "line-scale", "individual-balloting",
})
_OPTION_LABEL_TYPES = frozenset({"multiple-choice", "multiple-open-answer"})
_MATRIX_TYPES = frozenset({"matrix"})
_RANKING_TYPES = frozenset({"ranking"})
_OPEN_TEXT_TYPES = frozenset({"open-answer", "email", "upload-multimedia", "contact-information"})

# Refused with a named reason rather than skipped, per §16. Silence here is the failure mode this
# whole pipeline exists to avoid: a measure dropped upstream is indistinguishable from one that
# never existed.
_UNSUPPORTED_TYPES: dict[str, str] = {
    "triangle-test": "no option row exists for a triangle test, so there is no component to key on",
    "tetrad-test": "no option row exists for a tetrad test, so there is no component to key on",
    "tcata": "TCATA records only action and t_ms, with no value to analyse per component",
    "tds": "TDS records only action and t_ms, with no value to analyse per component",
    "time-intensity-slider": (
        "time-intensity needs an explicit Imax/Tmax/AUC reduction, which must be requested rather "
        "than assumed"
    ),
    "paired-questions": "paired comparisons carry a responseType this pipeline does not model",
    "info": "an info block is display copy and can never carry an answer",
}

# analytical_value is a storage/position code, not a score, and 50 option rows DB-wide are
# corrupt: values 666/888/999/1010/7777 sitting on options labelled "6".."10". Anything at or
# above this bound is treated as corrupt and reported with its raw value. NEVER clamped -- a
# clamped 7777 becomes a plausible-looking scale point and silently moves a mean.
_ANALYTICAL_VALUE_MAX = 100


def _numeric(value: Any) -> float | None:
    """Parse a value as a float, or return None. Never raises, never guesses."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _resolve_value(row: dict[str, Any]) -> tuple[str, Any, str]:
    """Resolve one raw answer row to (kind, value, note) following §4.1.

    `kind` is "numeric", "category" or "invalid"; `note` explains an invalid row. The caller keeps
    invalid rows and reports them -- discarding one here would be the silent drop this pipeline
    is built to prevent.
    """
    question_type = row.get("question_type") or ""

    if question_type in _MATRIX_TYPES:
        label = row.get("value_label")
        code = row.get("value_code")
        if code is not None and abs(float(code)) >= _ANALYTICAL_VALUE_MAX:
            return "invalid", label, (
                f"analytical_value {code} is out of range for a scale point "
                f"(option labelled {label!r}); reported, not clamped"
            )
        numeric = _numeric(code)
        if numeric is not None:
            return "numeric", numeric, ""
        return ("category", label, "") if label else ("invalid", None, "matrix cell has no value")

    if question_type in _OPTION_LABEL_TYPES:
        label = row.get("value_label") or row.get("option_label")
        if label:
            return "category", label, ""
        return "invalid", None, "no option label joined for a category answer"

    if question_type in _NUMERIC_ANSWER_TYPES:
        numeric = _numeric(row.get("option_answer"))
        if numeric is not None:
            return "numeric", numeric, ""
        raw = row.get("option_answer")
        return "invalid", raw, (
            f"optionAnswer {raw!r} is not numeric" if raw is not None
            else "no optionAnswer recorded"
        )

    if question_type in _RANKING_TYPES:
        numeric = _numeric(row.get("option_rank"))
        if numeric is not None:
            return "numeric", numeric, ""
        return "invalid", row.get("option_rank"), "rank is missing or not numeric"

    if question_type in _OPEN_TEXT_TYPES:
        text_value = (row.get("text_value") or "").strip()
        return ("category", text_value, "") if text_value else (
            "invalid", None, "open answer is empty"
        )

    return "invalid", None, f"unhandled question type {question_type!r}"


def extract_dataset(
    connect: Callable[[], Any],
    survey_id: str,
    variables: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Stage A: extract respondent-level rows for the requested variables.

    Returns the long-form rows keyed (enrollment_id, product_id, question_id, component_id) plus
    the report the diagnostics need: the detected grain regime, invalid values with their raw
    contents, unsupported variables with reasons, and any duplicate observation.
    """
    specs = {str(v.get("question_id")): dict(v) for v in variables}
    with connect() as conn:
        raw = conn.execute(
            text(_STAGE_A_SQL),
            {"survey_id": survey_id, "question_ids": list(specs)},
        ).mappings().all()

    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    unsupported: dict[str, str] = {}
    seen_types: dict[str, str] = {}
    products_by_enrollment: dict[str, set[str]] = {}
    duplicates: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str, str]] = set()

    for record in raw:
        row = dict(record)
        question_id = row["question_id"]
        question_type = row.get("question_type") or ""
        seen_types[question_id] = question_type
        if question_type in _UNSUPPORTED_TYPES:
            unsupported[question_id] = _UNSUPPORTED_TYPES[question_type]
            continue

        # Requested a specific sub-item? Keep only that one, and keep it by LABEL: labels are
        # unique within every answered line-scale (0 of 294 duplicated) and every answered
        # multiple-choice (0 of 702), and carrying component ids in the prefetch would have cost
        # 42,033-104,520 chars.
        wanted = specs[question_id].get("component_label")
        if wanted and row.get("component_label") != wanted:
            continue

        products_by_enrollment.setdefault(row["enrollment_id"], set()).add(
            row["product_id"] or ""
        )
        kind, value, note = _resolve_value(row)
        entry = {
            "enrollment_id": row["enrollment_id"],
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "question_id": question_id,
            "question_type": question_type,
            "enrollment_status": row["enrollment_status"],
            "component_id": row["component_id"],
            "component_label": row["component_label"],
            "role": specs[question_id].get("role", "attribute"),
            "kind": kind,
            "value": value,
        }
        if kind == "invalid":
            invalid.append({**entry, "reason": note})
            continue

        key = (
            entry["enrollment_id"], entry["product_id"] or "",
            question_id, entry["component_id"] or "",
        )
        if key in keys:
            # Never averaged. Two observations at one key mean the grain assumption is wrong, and
            # quietly collapsing them would hide that.
            duplicates.append(entry)
            continue
        keys.add(key)
        rows.append(entry)

    missing = [qid for qid in specs if qid not in seen_types]
    max_products = max((len(v) for v in products_by_enrollment.values()), default=0)
    return {
        "rows": rows,
        "grain": {
            # Detected, never assumed (§11.5). In the single-product regime respondent level and
            # respondent x product coincide, so one respondent gets exactly one persona; in the
            # multi-product regime the same respondent can land in several, which breaks persona
            # semantics and has to be reported. That second path cannot be validated on this
            # database -- its largest repeated-measures survey has 43 enrollments.
            "regime": "single_product" if max_products <= 1 else "multi_product",
            "max_products_per_enrollment": max_products,
            "rows": len(rows),
            "respondents": len(products_by_enrollment),
            "products": len({r["product_id"] for r in rows if r["product_id"]}),
        },
        "invalid_values": invalid,
        "unsupported": [
            {"question_id": qid, "question_type": seen_types.get(qid), "reason": reason}
            for qid, reason in unsupported.items()
        ],
        "missing_variables": missing,
        "duplicate_observations": duplicates,
        "enrollment_statuses": sorted({
            str(r["enrollment_status"]) for r in rows if r["enrollment_status"]
        }),
    }


# ============================================================================
# Step 9.2 -- encoding resolution
# ============================================================================

ENCODINGS = ("numeric", "ordinal", "nominal", "jar", "binary")
# Encodings whose whole point is an ordering, so a stated sequence is mandatory.
_SEQUENCED_ENCODINGS = ("ordinal", "jar")
# Encodings that assert a direction, which is the field that flips every downstream sign.
_DIRECTIONAL_ENCODINGS = ("numeric", "ordinal", "jar", "binary")

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


# What the encoding turn is shown, re-fetched by qid. Labels come from the CONFIGURATION, not from
# the observed rows, so a configured category nobody chose still appears -- a model shown only
# observed labels would place a scale's unused endpoint outside the sequence, or miss that a
# non-consumer option exists at all. `order` is used solely to sequence the labels for display and
# is not emitted; `analytical_value` is not selected at all.
_ENCODING_INPUT_SQL = """
SELECT q.id::text                                    AS question_id,
       q."typeOfQuestion"                            AS question_type,
       q.settings->>'answer-type'                    AS answer_type,
       COALESCE(NULLIF(BTRIM(q.settings->>'chartTitle'), ''), q.prompt) AS label,
       q.prompt                                      AS prompt,
       (q.settings->'settingSlider'->>'min')::numeric AS slider_min,
       (q.settings->'settingSlider'->>'max')::numeric AS slider_max,
       COALESCE((
         SELECT jsonb_agg(qo.label ORDER BY qo."order", qo.label)
         FROM question_option qo
         WHERE qo.question_id = q.id AND NULLIF(BTRIM(qo.label), '') IS NOT NULL
       ), '[]'::jsonb)                               AS labels,
       (SELECT jsonb_build_array(
          (SELECT e.v->>'label' FROM jsonb_array_elements(pl.settings->'positionLabels')
             WITH ORDINALITY e(v, i)
           WHERE NULLIF(BTRIM(e.v->>'label'), '') IS NOT NULL ORDER BY e.i LIMIT 1),
          (SELECT e.v->>'label' FROM jsonb_array_elements(pl.settings->'positionLabels')
             WITH ORDINALITY e(v, i)
           WHERE NULLIF(BTRIM(e.v->>'label'), '') IS NOT NULL ORDER BY e.i DESC LIMIT 1))
        FROM (
          SELECT qo."optionSettings" AS settings FROM question_option qo
          WHERE qo.question_id = q.id
            AND jsonb_array_length(qo."optionSettings"->'positionLabels') > 0
          ORDER BY jsonb_array_length(qo."optionSettings"->'positionLabels') DESC, qo.id
          LIMIT 1
        ) pl)                                        AS anchors
FROM question q
WHERE q.id = ANY(CAST(:question_ids AS uuid[]))
"""


def build_encoding_inputs(
    connect: Callable[[], Any],
    variables: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble what the §9.2 turn sees, one entry per requested variable.

    Configuration supplies the labels, the anchors and the numeric range; the parked stage-A rows
    supply the observed counts, so the counts describe the dataset actually being analysed rather
    than every answer ever recorded.
    """
    specs = {str(v["question_id"]): dict(v) for v in variables}
    with connect() as conn:
        fetched = conn.execute(
            text(_ENCODING_INPUT_SQL), {"question_ids": list(specs)}
        ).mappings().all()
    metadata = {row["question_id"]: dict(row) for row in fetched}

    observed: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.get("kind") != "category":
            continue
        counts = observed.setdefault(str(row["question_id"]), {})
        label = str(row["value"])
        counts[label] = counts.get(label, 0) + 1

    inputs = []
    for question_id, spec in specs.items():
        meta = metadata.get(question_id, {})
        anchors = [a for a in (meta.get("anchors") or []) if a]
        labels = list(meta.get("labels") or [])
        counts = observed.get(question_id, {})
        entry: dict[str, Any] = {
            "question_id": question_id,
            "role": spec.get("role", "attribute"),
            "question_type": meta.get("question_type"),
            "measure": meta.get("label"),
            "prompt": meta.get("prompt"),
            "multi_select": (meta.get("answer_type") or "").lower() == "multiple",
            "labels": labels,
            "anchors": anchors or None,
            "configured_range": (
                [float(meta["slider_min"]), float(meta["slider_max"])]
                if meta.get("slider_min") is not None and meta.get("slider_max") is not None
                else None
            ),
            # Only when this variable actually appears in the extracted dataset. An all-zeros
            # count table would read as "nobody chose any of these", when the truth is that the
            # variable is not part of this dataset -- a screener, say, or a numeric scale with no
            # categories at all.
            "respondents_per_label": (
                {label: counts.get(label, 0) for label in labels}
                if question_id in observed and labels else None
            ),
            # Echoed so the turn can see what the caller already pinned; an override still wins
            # afterwards, but the turn should not argue with a decision already made.
            "supplied": {
                key: spec[key] for key in
                ("encoding", "ordered_categories", "jar_ideal", "exclude_categories")
                if spec.get(key) not in (None, "auto", [])
            } or None,
        }
        inputs.append(entry)
    return inputs


def excluded_category_counts(
    encodings: dict[str, dict[str, Any]], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per excluded category, the respondent count it removes.

    Reported, never silently dropped: an exclusion that does not state its size is indistinguishable
    from a category that was never there.
    """
    observed: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("kind") == "category":
            key = (str(row["question_id"]), str(row["value"]))
            observed[key] = observed.get(key, 0) + 1
    report = []
    for question_id, decision in encodings.items():
        for label in decision.get("excluded_categories") or []:
            report.append({
                "question_id": question_id,
                "label": label,
                "n": observed.get((question_id, str(label)), 0),
                "reason": "excluded by the encoding step as not a point on the scale",
            })
    return report


def _parse_decisions(raw: str) -> dict[str, Any]:
    """Parse the turn's JSON, tolerating a code fence but nothing else."""
    stripped = _JSON_FENCE_RE.sub("", raw or "").strip()
    if not stripped:
        raise ValueError("the encoding turn returned no text")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the encoding turn did not return JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("decisions"), list):
        raise ValueError("expected an object with a 'decisions' list")
    return parsed


def validate_encodings(
    decisions: Sequence[dict[str, Any]], variables: Sequence[dict[str, Any]]
) -> list[str]:
    """Return every contract violation in `decisions`, as messages the model can act on.

    Strict on purpose. This turn's output decides what every later number means, and the two
    fields most easily left vague -- `direction` and `labels_read` -- are exactly the two that
    make a result checkable. A missing direction turns "positively aligned" into a coin flip, and
    a missing labels_read makes the decision unauditable.
    """
    errors: list[str] = []
    by_id = {str(v["question_id"]): v for v in variables}
    seen: set[str] = set()

    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            errors.append(f"decision {index} is not an object")
            continue
        question_id = str(decision.get("question_id") or "")
        if question_id not in by_id:
            errors.append(f"decision {index} names unknown question_id {question_id!r}")
            continue
        if question_id in seen:
            errors.append(f"question_id {question_id} appears more than once")
            continue
        seen.add(question_id)

        variable = by_id[question_id]
        labels = [str(label) for label in (variable.get("labels") or [])]
        encoding = decision.get("encoding")
        if encoding not in ENCODINGS:
            errors.append(
                f"{question_id}: encoding {encoding!r} is not one of {list(ENCODINGS)}"
            )
            continue

        excluded = [str(label) for label in (decision.get("excluded_categories") or [])]
        unknown_excluded = [label for label in excluded if label not in labels]
        if unknown_excluded:
            errors.append(
                f"{question_id}: excluded_categories names labels this variable does not have: "
                f"{unknown_excluded}"
            )

        sequence = [str(label) for label in (decision.get("ordered_categories") or [])]
        # A variable with no category labels is measured on a numeric scale -- a slider with only
        # endpoint anchors -- so there is nothing to sequence. The reference fixture's JAR
        # attribute is exactly this: a 1-5 slider anchored "Not crispy/crunchy enough" to "Much
        # too crispy/crunchy", with no per-point labels.
        if encoding in _SEQUENCED_ENCODINGS and labels:
            expected = [label for label in labels if label not in excluded]
            if sorted(sequence) != sorted(expected):
                errors.append(
                    f"{question_id}: ordered_categories must list every non-excluded label "
                    f"exactly once. Expected these {len(expected)}: {expected}. Got: {sequence}"
                )
        elif sequence and (encoding not in _SEQUENCED_ENCODINGS or not labels):
            errors.append(
                f"{question_id}: ordered_categories must be empty for encoding {encoding!r}"
                + ("" if labels else " on a variable with no category labels")
            )

        if encoding == "jar":
            ideal = decision.get("jar_ideal")
            if labels:
                if not ideal or str(ideal) not in sequence:
                    errors.append(
                        f"{question_id}: a jar encoding needs jar_ideal naming one of its ordered "
                        f"categories, got {ideal!r}"
                    )
            else:
                # Numeric JAR: the ideal is a point on the configured range, not a label.
                numeric_ideal = _numeric(ideal)
                configured = variable.get("configured_range") or []
                if numeric_ideal is None:
                    errors.append(
                        f"{question_id}: this variable has no category labels, so jar_ideal must "
                        f"be a NUMBER on its configured range {configured or 'unknown'}, "
                        f"got {ideal!r}"
                    )
                elif len(configured) == 2 and not (
                    float(configured[0]) <= numeric_ideal <= float(configured[1])
                ):
                    errors.append(
                        f"{question_id}: jar_ideal {numeric_ideal} is outside the configured "
                        f"range {configured}"
                    )
        elif decision.get("jar_ideal"):
            errors.append(f"{question_id}: jar_ideal is only valid for a jar encoding")

        if encoding in _DIRECTIONAL_ENCODINGS and not str(decision.get("direction") or "").strip():
            errors.append(
                f"{question_id}: direction is mandatory for encoding {encoding!r} -- state which "
                "end means MORE, in words"
            )

        labels_read = [str(label) for label in (decision.get("labels_read") or [])]
        if not labels_read:
            errors.append(
                f"{question_id}: labels_read is empty -- name the labels the decision rests on"
            )
        else:
            unknown_read = [label for label in labels_read if label not in labels]
            if unknown_read and labels:
                errors.append(
                    f"{question_id}: labels_read names text that is not among this variable's "
                    f"labels: {unknown_read}"
                )

    missing = [qid for qid in by_id if qid not in seen]
    if missing:
        errors.append(f"no decision for {missing}")
    return errors


def _apply_supplied_overrides(
    decision: dict[str, Any], variable: dict[str, Any]
) -> dict[str, Any]:
    """A caller-supplied encoding always wins, and both readings are recorded (§15)."""
    supplied_encoding = variable.get("encoding")
    resolved = dict(decision)
    sources: dict[str, str] = {}
    if supplied_encoding and supplied_encoding != "auto":
        sources["encoding"] = "supplied"
        resolved["derived_encoding"] = decision.get("encoding")
        resolved["encoding"] = supplied_encoding
    if variable.get("ordered_categories"):
        sources["ordered_categories"] = "supplied"
        resolved["derived_ordered_categories"] = decision.get("ordered_categories")
        resolved["ordered_categories"] = list(variable["ordered_categories"])
    if variable.get("jar_ideal"):
        sources["jar_ideal"] = "supplied"
        resolved["jar_ideal"] = variable["jar_ideal"]
    if variable.get("exclude_categories"):
        sources["excluded_categories"] = "supplied"
        resolved["derived_excluded_categories"] = decision.get("excluded_categories")
        resolved["excluded_categories"] = list(variable["exclude_categories"])
    resolved["source"] = sources or {"encoding": "derived"}
    return resolved


def resolve_encodings(
    model: Any,
    variables: Sequence[dict[str, Any]],
    system_prompt: str = WORKFLOW_SYSTEM_PROMPT,
) -> dict[str, Any]:
    """Run the §9.2 encoding turn and return validated per-variable decisions.

    One retry, and only on validation failure -- the single loop §8.2 permits. The retry is given
    the specific errors rather than a generic "try again", because a model that cannot see what
    was wrong tends to change something else.
    """
    payload = json.dumps(list(variables), indent=2, ensure_ascii=False, default=str)
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=ENCODING_STEP_TEMPLATE.format(variables=payload)),
    ]

    attempts: list[dict[str, Any]] = []
    for attempt in range(2):
        response = model.invoke(messages)
        raw = _message_text(response)
        try:
            parsed = _parse_decisions(raw)
            errors = validate_encodings(parsed["decisions"], variables)
        except ValueError as exc:
            parsed, errors = {"decisions": []}, [str(exc)]
        attempts.append({"attempt": attempt + 1, "errors": errors})
        if not errors:
            break
        if attempt == 0:
            messages.extend([
                AIMessage(content=raw),
                HumanMessage(content=ENCODING_RETRY_TEMPLATE.format(
                    errors="\n".join(f"- {error}" for error in errors)
                )),
            ])
    else:
        raise RuntimeError(
            "the encoding turn did not satisfy the contract after a retry: "
            + "; ".join(attempts[-1]["errors"])
        )

    by_id = {str(v["question_id"]): v for v in variables}
    resolved = {}
    for decision in parsed["decisions"]:
        question_id = str(decision["question_id"])
        variable = by_id[question_id]
        entry = _apply_supplied_overrides(decision, variable)
        # A numeric JAR's ideal is a point on the scale, so store it as a number even when the
        # turn wrote it as "3.0". Downstream this is arithmetic, not a label.
        if entry.get("encoding") == "jar" and not (variable.get("labels") or []):
            entry["jar_ideal"] = _numeric(entry.get("jar_ideal"))
        resolved[question_id] = entry
    return {"encodings": resolved, "attempts": attempts}


def _message_text(response: Any) -> str:
    """The text of a model response, whether it came back as a string or as content blocks."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


# ============================================================================
# Stage B -- normalization
# ============================================================================
#
# The scale metadata is RE-FETCHED here by qid rather than taken from anything the main agent
# held (§8.1). That is what keeps a budget-trimmed `categories_omitted` list recoverable and the
# design range truthful: the packet the main agent saw may have been degraded, and this stage
# cannot tell from the outside.
_CATALOG_SQL = """
SELECT q.id::text                                        AS question_id,
       q."typeOfQuestion"                                AS question_type,
       COALESCE(NULLIF(BTRIM(q.settings->>'chartTitle'), ''), q.prompt) AS label,
       (q.settings->'settingSlider'->>'min')::numeric     AS slider_min,
       (q.settings->'settingSlider'->>'max')::numeric     AS slider_max,
       (SELECT MAX(jsonb_array_length(qo."optionSettings"->'positionLabels'))
          FROM question_option qo WHERE qo.question_id = q.id) AS labelled_positions
FROM question q
WHERE q.id = ANY(CAST(:question_ids AS uuid[]))
"""

# The flag exists because K-means weights dimensions by SQUARED distance, so sd is the operative
# quantity -- an attribute with a sixth of another's sd contributes a thirty-sixth of the pull and
# is effectively ignored. The floor is a stated, configurable number rather than a discovered
# constant, and on the reference fixture it sits in an empty band: the seven attributes normalize
# to sds of 0.0629, 0.1191, 0.1219, 0.1224, 0.1434, 0.1515, 0.1546, so 0.10 separates exactly the
# one attribute that is 95.4% a single value from the six that are not.
#
# Flagging is NOT dropping. Whether a near-constant attribute is excluded from clustering is a
# separate decision with its own trade-off (it removes a real, if narrow, measure), so this stage
# reports and the clustering stage decides.
NEAR_CONSTANT_SD = float(os.getenv("ISOLATED_WORKFLOW_NEAR_CONSTANT_SD", "0.10"))
NEAR_CONSTANT_MODAL_SHARE = float(
    os.getenv("ISOLATED_WORKFLOW_NEAR_CONSTANT_MODAL_SHARE", "0.90")
)


def fetch_scale_metadata(
    connect: Callable[[], Any], question_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Re-fetch the design-scale metadata for `question_ids`, keyed by question id."""
    with connect() as conn:
        rows = conn.execute(
            text(_CATALOG_SQL), {"question_ids": list(question_ids)}
        ).mappings().all()
    return {row["question_id"]: dict(row) for row in rows}


def _design_range(meta: dict[str, Any]) -> tuple[float, float] | None:
    """The configured numeric range, or None when the configuration does not state one.

    Only `settingSlider` is trusted for this. `labelled_positions` counts labels, not scale
    points -- measured, it disagrees with the configured range on 53% of the questions carrying
    both -- so it is never used to infer endpoints. And the OBSERVED range is never used at all:
    observed values are response data, and scaling by them makes the busiest respondent set
    define the scale.
    """
    low, high = meta.get("slider_min"), meta.get("slider_max")
    if low is None or high is None:
        return None
    low, high = float(low), float(high)
    return (low, high) if high > low else None


def normalize_dataset(
    rows: Sequence[dict[str, Any]],
    scale_metadata: dict[str, dict[str, Any]],
    normalization: str = "design_range_zscore",
) -> dict[str, Any]:
    """Stage B: design-range scale, then optionally z-score. Both recorded.

    Returns the wide matrix at (enrollment_id, product_id) grain plus per-column metadata. Rows
    are kept LISTWISE COMPLETE: a key missing any attribute is excluded and counted, never
    imputed, because an imputed attribute value would enter a distance metric as if it had been
    measured.
    """
    attribute_rows = [r for r in rows if r.get("role") == "attribute"]
    columns: dict[tuple[str, str], dict[str, Any]] = {}
    for row in attribute_rows:
        key = (row["question_id"], row.get("component_label") or "")
        if key not in columns:
            meta = scale_metadata.get(row["question_id"], {})
            columns[key] = {
                "question_id": row["question_id"],
                "component_label": row.get("component_label") or None,
                "label": _column_label(meta, row),
                "question_type": row.get("question_type"),
                "design_range": _design_range(meta),
            }

    # Wide matrix, listwise complete.
    cells: dict[tuple[str, str], dict[tuple[str, str], float]] = {}
    non_numeric: list[dict[str, Any]] = []
    for row in attribute_rows:
        if row.get("kind") != "numeric":
            non_numeric.append(row)
            continue
        grain_key = (row["enrollment_id"], row["product_id"] or "")
        cells.setdefault(grain_key, {})[
            (row["question_id"], row.get("component_label") or "")
        ] = float(row["value"])

    order = list(columns)
    complete = [key for key, values in cells.items() if len(values) == len(order)]
    incomplete = [key for key, values in cells.items() if len(values) != len(order)]
    complete.sort()

    matrix = np.array(
        [[cells[key][column] for column in order] for key in complete], dtype=float
    ) if complete else np.zeros((0, len(order)))

    missing_design_range = [
        columns[key]["label"] for key in order if columns[key]["design_range"] is None
    ]
    if missing_design_range:
        raise RuntimeError(
            "no configured design range for " + ", ".join(missing_design_range)
            + " -- the observed range is response data and must never stand in for scale "
            "metadata, so normalization cannot proceed for these attributes"
        )

    # 1. Design-range scaling. This is what puts a 1-9 and a 1-5 attribute on one range; without
    #    it the 1-9 attributes dominate purely because of scale choice.
    lows = np.array([columns[key]["design_range"][0] for key in order], dtype=float)
    highs = np.array([columns[key]["design_range"][1] for key in order], dtype=float)
    scaled = (matrix - lows) / (highs - lows) if matrix.size else matrix

    # 2. Per-attribute z-scoring. Not cosmetic: after step 1 the fixture's CRISPINESS still sits
    #    at sd 0.063 against AFTERTASTE's 0.155, a ~6x difference in squared-distance
    #    contribution, so K-means would effectively ignore it.
    #    ddof=1 throughout, matching the sample sd the design figures were measured with.
    scaled_means = scaled.mean(axis=0) if scaled.size else np.zeros(len(order))
    scaled_sds = scaled.std(axis=0, ddof=1) if len(complete) > 1 else np.zeros(len(order))
    zscored = None
    if normalization == "design_range_zscore" and scaled.size:
        safe = np.where(scaled_sds > 0, scaled_sds, 1.0)
        zscored = (scaled - scaled_means) / safe

    column_report = []
    for index, key in enumerate(order):
        column = dict(columns[key])
        values = matrix[:, index] if matrix.size else np.zeros(0)
        column_values = scaled[:, index] if scaled.size else np.zeros(0)
        modal_share = _modal_share(values)
        column.update({
            "n_valid": int(len(complete)),
            "n_excluded": int(sum(1 for r in attribute_rows
                                  if r["question_id"] == column["question_id"]
                                  and r.get("kind") != "numeric")),
            "raw_mean": _round(values.mean()) if values.size else None,
            "raw_sd": _round(values.std(ddof=1)) if values.size > 1 else None,
            "normalized_mean": _round(scaled_means[index]) if scaled.size else None,
            "normalized_sd": _round(scaled_sds[index]) if scaled.size else None,
            "modal_share": _round(modal_share),
            "near_constant": bool(
                scaled.size
                and (scaled_sds[index] < NEAR_CONSTANT_SD
                     or modal_share >= NEAR_CONSTANT_MODAL_SHARE)
            ),
        })
        column_report.append(column)

    applied = normalization if zscored is not None else "design_range"
    return {
        "grain_keys": complete,
        "columns": column_report,
        "design_range_matrix": scaled,
        "zscored_matrix": zscored,
        # Clustering runs on the DESIGN-RANGE matrix, not the z-scored one, and that is a decision
        # with a stated reason rather than an oversight. The measured baseline this pipeline is
        # verified against -- cluster sizes, region masses, cluster-region overlaps -- was taken on
        # the design-range matrix, so clustering the z-scored one would silently invalidate every
        # figure without changing any code that looks wrong. Z-scores are still computed and
        # reported per column, because the disparity they would correct is real: CRISPINESS sits at
        # sd 0.063 against AFTERTASTE's 0.155, so the design range does under-weight it. Moving
        # clustering to the z-scored domain is a deliberate change that needs its own baseline.
        "clustering_domain": "design_range",
        "normalization": {
            # Stated, because it changes the clustering: design-range alone leaves a
            # low-variance attribute effectively unweighted, and z-scoring equalises every
            # attribute's pull including a near-constant one.
            "requested": normalization,
            "applied": applied,
            "sd_ddof": 1,
            "near_constant_sd_floor": NEAR_CONSTANT_SD,
            "near_constant_modal_share_floor": NEAR_CONSTANT_MODAL_SHARE,
        },
        "excluded": {
            "incomplete_keys": len(incomplete),
            "non_numeric_rows": len(non_numeric),
        },
    }


def _column_label(meta: dict[str, Any], row: dict[str, Any]) -> str:
    """A readable column name: the question's short label, plus the sub-item when there is one."""
    base = str(meta.get("label") or row["question_id"])
    component = row.get("component_label")
    return f"{base} / {component}" if component else base


def _modal_share(values: np.ndarray) -> float:
    """The share of observations sitting on the single most common value."""
    if values.size == 0:
        return 0.0
    _unique, counts = np.unique(values, return_counts=True)
    return float(counts.max() / values.size)


def _round(value: Any, places: int = 4) -> float | None:
    """Round for reporting, keeping None as None."""
    if value is None:
        return None
    number = float(value)
    return round(number, places) if math.isfinite(number) else None


# ============================================================================
# Stage G -- resolving persona regions from quantile specifications
# ============================================================================
#
# Why quantiles and not numbers, measured four ways on the reference fixture (§11.4):
#
#   domain the bounds are written in      box masses            unassigned clusters
#   normalized [0,1], round thresholds    0.015 / 0.060 / 0.001    none
#   z-score, +-0.5 sd                     0.003 / 0.000 / 0.002    [0, 3]
#   z-score, +-1.0 sd                     0.000 / 0.172 / 0.002    [1, 3]
#   rank-percentile terciles              0.055 / 0.027 / 0.052    none
#   quantile terciles, TIE-AWARE          0.266 / 0.155 / 0.226    none
#
# Z-scores are unusable here: the normalized attributes are strongly left-skewed with large
# ceiling mass -- FRESHNESS has 74% of respondents at its maximum -- so for FLAVOR and FRESHNESS
# P(x > mean + 1sd) is 0.000 and a "high on flavour" region at +1 sd is EMPTY BY CONSTRUCTION.
# Fixed normalized thresholds are distribution-blind: 0.75 reads as "high" while 87% of the mass
# sits above it. And rank-percentiles SPLIT TIED RESPONDENTS: at FRESHNESS's 2/3 quantile a rank
# transform takes 401 rows where the value threshold takes 888, so 888 identical answers are cut
# 401-in / 487-out by stable-sort order alone -- two respondents who answered the same land in
# different personas.
#
# Hence: the model states a relative position, and this resolver converts it to a value threshold
# on the distinct-value grid, which keeps every tie group whole. Because the transform is monotone
# per axis the result is still an exact axis-aligned box, so the overlap analysis is unchanged.


def _ecdf(column: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The distinct values of `column` and the cumulative mass P(x <= v) at each."""
    values, counts = np.unique(column, return_counts=True)
    return values, np.cumsum(counts) / column.size


def _resolve_threshold(column: np.ndarray, quantile: float) -> tuple[float, float]:
    """The first distinct value whose cumulative mass reaches `quantile`, and that mass.

    Returning the achieved mass alongside is the point: where a single value holds a large atom
    the requested quantile is unattainable, and the caller must be able to say so rather than
    quietly pretending the cut happened where it was asked for.
    """
    values, ecdf = _ecdf(column)
    index = min(int(np.searchsorted(ecdf, quantile, side="left")), len(values) - 1)
    return float(values[index]), float(ecdf[index])


# A quantile and a normalized value both live in [0,1], so a number inside that range cannot be
# told apart from a misplaced value -- the only detectable violation is a bound OUTSIDE it, which
# a raw or normalized number usually is. Saying this plainly rather than claiming a check that
# cannot exist: the prompt is what keeps bounds quantile-shaped, and this catches the giveaways.
def validate_section_specs(
    specs: dict[str, Any], column_labels: Sequence[str]
) -> list[str]:
    """Return every violation in a set of persona region specifications."""
    errors: list[str] = []
    if not isinstance(specs, dict) or not specs:
        return ["persona_sections must be a non-empty object of persona name -> bounds"]
    known = set(column_labels)
    for persona, bounds in specs.items():
        if not isinstance(bounds, dict) or not bounds:
            errors.append(f"{persona}: bounds must be a non-empty object of attribute -> [lo, hi]")
            continue
        for attribute, pair in bounds.items():
            if attribute not in known:
                errors.append(
                    f"{persona}: {attribute!r} is not one of the attributes "
                    f"{sorted(known)}"
                )
                continue
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                errors.append(
                    f"{persona}.{attribute}: bounds must be a two-item list [lower, upper], "
                    f"got {pair!r}"
                )
                continue
            lower, upper = pair
            for name, bound in (("lower", lower), ("upper", upper)):
                if bound is None:
                    continue
                number = _numeric(bound)
                if number is None:
                    errors.append(
                        f"{persona}.{attribute}: {name} bound {bound!r} is not a number or null"
                    )
                elif not 0.0 <= number <= 1.0:
                    errors.append(
                        f"{persona}.{attribute}: {name} bound {number} is outside [0,1], so it is "
                        "a raw or normalized value rather than a QUANTILE. Bounds state relative "
                        "position only"
                    )
            low, high = _numeric(lower), _numeric(upper)
            if low is not None and high is not None and low > high:
                errors.append(
                    f"{persona}.{attribute}: lower quantile {low} is above upper {high}"
                )
        if all(pair == [None, None] for pair in bounds.values()):
            errors.append(f"{persona}: every bound is null, so the region is the whole space")
    return errors


def resolve_persona_sections(
    specs: dict[str, dict[str, Any]],
    X: np.ndarray,
    columns: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Stage G: turn quantile specifications into value-threshold boxes, tie-aware.

    Returns the boxes stage I consumes plus, per persona per bounded attribute, the requested
    quantile, the resolved threshold and the mass actually achieved.
    """
    labels = [column["label"] for column in columns]
    errors = validate_section_specs(specs, labels)
    if errors:
        raise RuntimeError(
            "the persona section specifications are not usable: " + "; ".join(errors)
        )

    index_of = {label: position for position, label in enumerate(labels)}
    sections: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    report: list[dict[str, Any]] = []

    for persona, bounds in specs.items():
        lower = np.full(len(labels), -np.inf)
        upper = np.full(len(labels), np.inf)
        resolved: dict[str, Any] = {}
        divergences: list[str] = []
        for attribute, (low_q, high_q) in bounds.items():
            position = index_of[attribute]
            column = X[:, position]
            entry: dict[str, Any] = {}
            for name, quantile, target in (
                ("lower", _numeric(low_q), lower), ("upper", _numeric(high_q), upper)
            ):
                if quantile is None:
                    continue
                threshold, achieved = _resolve_threshold(column, quantile)
                target[position] = threshold
                entry[name] = {
                    "requested_quantile": _round(quantile),
                    "resolved_threshold": _round(threshold),
                    "achieved_mass": _round(achieved),
                }
                # A tie atom larger than the tercile itself makes the requested cut impossible.
                if abs(achieved - quantile) > 0.05:
                    divergences.append(
                        f"{attribute} {name}: requested {quantile:.3f}, achieved "
                        f"{achieved:.3f} -- a single value holds too much mass to cut there"
                    )
            if entry:
                resolved[attribute] = entry
        inside = np.all((X >= lower) & (X <= upper), axis=1)
        sections[persona] = (lower, upper)
        report.append({
            "persona": persona,
            "bounded_attributes": sorted(resolved),
            "resolved": resolved,
            "mass": _round(float(inside.mean())),
            "n": int(inside.sum()),
            "empty": bool(inside.sum() == 0),
            "divergences": divergences,
        })

    # Pairwise overlap and coverage (§16). Regions that overlap each other are a measured failure
    # mode: bounding only a few "defining" dimensions gave high cluster overlaps but collapsed
    # four clusters onto two distinct personas, because the regions were not distinct to begin
    # with. Nesting is the sharpest form of it -- if one region contains another, the clusters
    # inside the inner one match both, and which persona a cluster gets depends on tie-breaking
    # rather than on the data. Surfaced here, BEFORE clustering, so it reads as a specification
    # problem rather than a mysterious assignment later.
    membership = {
        persona: np.all((X >= lower) & (X <= upper), axis=1)
        for persona, (lower, upper) in sections.items()
    }
    pairwise = []
    for first, second in combinations(sections, 2):
        both = int(np.sum(membership[first] & membership[second]))
        if both == 0:
            continue
        smaller = min(int(membership[first].sum()), int(membership[second].sum()))
        nested = (
            first if np.all(membership[first] <= membership[second])
            else second if np.all(membership[second] <= membership[first])
            else None
        )
        pairwise.append({
            "personas": [first, second],
            "n_in_both": both,
            "share_of_smaller": _round(both / smaller) if smaller else None,
            "nested": nested,
            "note": (
                f"{nested!r} is entirely contained in the other region, so no cluster can match "
                "one without matching both" if nested else
                "these regions overlap, so a cluster can match both"
            ),
        })
    covered = np.any(np.vstack(list(membership.values())), axis=0) if membership else np.zeros(0)
    return {
        "sections": sections,
        "report": report,
        "attribute_order": labels,
        "pairwise_overlap": pairwise,
        "coverage": _round(float(covered.mean())) if covered.size else 0.0,
        "distinct_regions": len(sections) - sum(1 for p in pairwise if p["nested"]),
    }


# ============================================================================
# Step 9.3 -- choosing K and specifying the persona regions
# ============================================================================
#
# This turn runs BEFORE clustering and never sees a cluster assignment. Specifying regions after
# seeing the clusters would make the overlap analysis circular -- the regions would be fitted to
# the very partition they are then used to name.
#
# It is given a QUANTILE GRID rather than mean and sd, because mean and sd cannot locate a region
# on this data: the attributes are strongly left-skewed with large ceiling atoms, so "one sd above
# the mean" is empty for two of the seven attributes (§11.4). Mean and sd are still supplied as
# context, labelled as such.

ALLOWED_K = (4, 5)


def _skew(column: np.ndarray) -> float | None:
    """Fisher-Pearson skewness. Negative means a long left tail with mass piled at the top."""
    if column.size < 3:
        return None
    centred = column - column.mean()
    sd = column.std(ddof=0)
    if sd == 0:
        return 0.0
    return float(np.mean(centred ** 3) / sd ** 3)


def build_sections_inputs(
    X: np.ndarray, columns: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """What the §9.3 turn sees: the distribution's shape, and nothing about any clustering."""
    attributes = []
    for position, column in enumerate(columns):
        values, ecdf = _ecdf(X[:, position])
        _unique, counts = np.unique(X[:, position], return_counts=True)
        attributes.append({
            "attribute": column["label"],
            # The grid is the basis for a region bound: a quantile names a row of this table.
            "quantile_grid": [
                {"value": _round(float(value)), "cumulative_mass": _round(float(mass))}
                for value, mass in zip(values, ecdf, strict=False)
            ],
            "largest_tie_share": _round(float(counts.max() / X.shape[0])),
            "skew": _round(_skew(X[:, position])),
            "near_constant": bool(column.get("near_constant")),
            # Context only. Stated as such because a bound written in sd units is empty by
            # construction on a ceiling-heavy attribute.
            "context_mean": column.get("normalized_mean"),
            "context_sd": column.get("normalized_sd"),
        })
    correlations = np.corrcoef(X, rowvar=False) if X.shape[1] > 1 else np.ones((1, 1))
    labels = [column["label"] for column in columns]
    return {
        "observations": int(X.shape[0]),
        "attributes": attributes,
        "pairwise_correlations": {
            labels[i]: {
                labels[j]: _round(float(correlations[i, j])) for j in range(len(labels))
            }
            for i in range(len(labels))
        },
        "level_vs_shape": level_vs_shape(X),
        "allowed_k": list(ALLOWED_K),
    }


def _spec_direction(bounds: Sequence[Any]) -> str:
    """Classify one attribute's bound as high, low or band."""
    lower, upper = _numeric(bounds[0]), _numeric(bounds[1])
    if lower is not None and upper is None:
        return "high"
    if upper is not None and lower is None:
        return "low"
    return "band"


def validate_sections_turn(
    payload: dict[str, Any], column_labels: Sequence[str], level_verdict: str
) -> list[str]:
    """Return every violation in the §9.3 turn's output."""
    errors: list[str] = []
    k = payload.get("k")
    if k not in ALLOWED_K:
        errors.append(f"k must be one of {list(ALLOWED_K)}, got {k!r}")
    if not str(payload.get("k_rationale") or "").strip():
        errors.append("k_rationale is required -- say why this K rather than the other")

    specs = payload.get("persona_sections")
    errors.extend(validate_section_specs(specs, column_labels))
    if not isinstance(specs, dict):
        return errors

    rationales = payload.get("persona_rationales") or {}
    for persona in specs:
        if not str(rationales.get(persona) or "").strip():
            errors.append(
                f"{persona}: a rationale is required naming the attributes and quantile levels "
                "that define this persona"
            )

    # The structural form of §20's "turn 2 uses level-based names when PC1 dominates". A persona
    # that bounds one attribute HIGH and another LOW is a shape region -- a "spicy lover" -- and
    # under level dominance the data does not carry the profile structure such a region asserts.
    # Checked on the SHAPE of the spec rather than on the words in the name, because a name is not
    # verifiable and a mixed-direction box is.
    if level_verdict == "level-dominated":
        for persona, bounds in specs.items():
            if not isinstance(bounds, dict):
                continue
            directions = {
                _spec_direction(pair) for pair in bounds.values()
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            }
            if len({d for d in directions if d in {"high", "low"}}) > 1:
                errors.append(
                    f"{persona}: this region bounds some attributes HIGH and others LOW, which "
                    "describes a profile type. The first principal component dominates this "
                    "data, so the clusters differ in LEVEL, not type -- use level-based regions "
                    "(all high, all low, middle band) instead"
                )
    return errors


def resolve_sections(
    model: Any,
    inputs: dict[str, Any],
    column_labels: Sequence[str],
    supplied_sections: dict[str, Any] | None = None,
    supplied_k: int | None = None,
    system_prompt: str = WORKFLOW_SYSTEM_PROMPT,
) -> dict[str, Any]:
    """Run the §9.3 turn, or validate what the caller pinned instead.

    A caller who supplies `persona_sections` gets them VALIDATED rather than replaced (§15) -- that
    is how personas stay comparable across surveys and waves.
    """
    level_verdict = (inputs.get("level_vs_shape") or {}).get("verdict", "")
    if supplied_sections:
        errors = validate_section_specs(supplied_sections, column_labels)
        if errors:
            raise RuntimeError(
                "the caller-supplied persona_sections are not usable: " + "; ".join(errors)
            )
        return {
            "k": supplied_k or ALLOWED_K[0],
            "k_rationale": (
                f"K={supplied_k} supplied by the caller" if supplied_k
                else f"K={ALLOWED_K[0]} defaulted; the caller pinned the regions but not K"
            ),
            "persona_sections": supplied_sections,
            "persona_rationales": {
                persona: "supplied by the caller" for persona in supplied_sections
            },
            "source": "supplied",
            "attempts": [{"attempt": 0, "errors": []}],
        }

    payload_text = json.dumps(inputs, indent=2, ensure_ascii=False, default=str)
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=SECTIONS_STEP_TEMPLATE.format(
            distribution=payload_text,
            allowed_k=" or ".join(str(k) for k in ALLOWED_K),
        )),
    ]
    attempts: list[dict[str, Any]] = []
    parsed: dict[str, Any] = {}
    for attempt in range(2):
        raw = _message_text(model.invoke(messages))
        try:
            stripped = _JSON_FENCE_RE.sub("", raw or "").strip()
            parsed = json.loads(stripped)
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
            errors = validate_sections_turn(parsed, column_labels, level_verdict)
        except (ValueError, json.JSONDecodeError) as exc:
            parsed, errors = {}, [f"the sections turn did not return usable JSON: {exc}"]
        attempts.append({"attempt": attempt + 1, "errors": errors})
        if not errors:
            break
        if attempt == 0:
            messages.extend([
                AIMessage(content=raw),
                HumanMessage(content=SECTIONS_RETRY_TEMPLATE.format(
                    errors="\n".join(f"- {error}" for error in errors)
                )),
            ])
    else:
        raise RuntimeError(
            "the sections turn did not satisfy the contract after a retry: "
            + "; ".join(attempts[-1]["errors"])
        )

    return {
        "k": int(parsed["k"]),
        "k_rationale": parsed["k_rationale"],
        "persona_sections": parsed["persona_sections"],
        "persona_rationales": parsed.get("persona_rationales") or {},
        "source": "derived",
        "attempts": attempts,
    }


# ============================================================================
# Stages D and F -- clustering
# ============================================================================
#
# Adopted from `persona_clustering_reference.py`, the analyst's supplied implementation, which is
# kept verbatim in the repo as the statement of intent. Its algorithm is sound -- k-means++ init,
# n_init=10 with best-inertia selection, empty-cluster repair in both the inner loop and after
# convergence, stable argsort -- and it reproduces the measured cluster sizes exactly at both K.
#
# DEFECT-3 (§13.3) is fixed here. The reference asked "is this row already a centroid" with
#   np.any(np.all(X[:, None, :] == centroids[None, :ci, :], axis=2), axis=1)
# which materializes an n x ci x d array (42,000 elements at the fixture's size, and growing with
# the respondent count). `closest_sq == 0` says the identical thing: closest_sq is the minimum
# squared distance to any chosen centroid, so it is zero exactly when the row equals one of them.
# Verified mask-for-mask identical across 21 init steps at three seeds, and the cluster sizes are
# unchanged at K=4 and K=5.
#
# random_state is REQUIRED, not optional. The reference allows None; the workflow must not, because
# an unreported seed makes a run unreproducible and the assignment is what every later number
# rests on.
KMEANS_N_INIT = int(os.getenv("ISOLATED_WORKFLOW_KMEANS_N_INIT", "10"))
KMEANS_MAX_ITER = int(os.getenv("ISOLATED_WORKFLOW_KMEANS_MAX_ITER", "300"))
KMEANS_TOL = float(os.getenv("ISOLATED_WORKFLOW_KMEANS_TOL", "1e-4"))


def _repair_empty_clusters(
    labels: np.ndarray, errors: np.ndarray, counts: np.ndarray, k: int
) -> None:
    """Move the worst-fitting donatable point into each empty cluster, in place.

    Without this an empty cluster persists and K silently becomes K-1 -- the run would report K
    personas while only K-1 exist.
    """
    for empty_cluster in np.flatnonzero(counts == 0):
        for point_index in np.argsort(-errors, kind="stable"):
            donor = labels[point_index]
            if counts[donor] > 1:
                labels[point_index] = empty_cluster
                counts[donor] -= 1
                counts[empty_cluster] += 1
                break


def kmeans(
    X: np.ndarray,
    k: int,
    *,
    random_state: int,
    n_init: int = KMEANS_N_INIT,
    max_iter: int = KMEANS_MAX_ITER,
    tol: float = KMEANS_TOL,
) -> np.ndarray:
    """Deterministic K-means. Returns one anonymous cluster label per row of X."""
    if random_state is None:
        raise RuntimeError(
            "random_state is required: an unreported seed makes the assignment, and therefore "
            "every number derived from it, unreproducible"
        )
    X = np.asarray(X, dtype=float)
    n, d = X.shape
    if k < 2 or k > n:
        raise RuntimeError(f"K={k} is not usable for {n} observation(s)")
    rng = np.random.default_rng(random_state)
    best_labels: np.ndarray | None = None
    best_inertia = np.inf

    for _ in range(n_init):
        centroids = np.empty((k, d))
        centroids[0] = X[rng.integers(n)]
        closest_sq = np.sum((X - centroids[0]) ** 2, axis=1)

        for centroid_index in range(1, k):
            # See DEFECT-3 above: zero distance means this row already IS a chosen centroid.
            candidates = np.flatnonzero(closest_sq > 0)
            if candidates.size == 0:
                candidates = np.arange(n)
            weights = closest_sq[candidates]
            if np.max(weights) == 0:
                point_index = rng.choice(candidates)
            else:
                weights = weights / np.max(weights)
                point_index = rng.choice(candidates, p=weights / weights.sum())
            centroids[centroid_index] = X[point_index]
            closest_sq = np.minimum(
                closest_sq, np.sum((X - centroids[centroid_index]) ** 2, axis=1)
            )

        labels = np.zeros(n, dtype=int)
        for _iteration in range(max_iter):
            distances = np.sum((X[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
            labels = np.argmin(distances, axis=1)
            errors = distances[np.arange(n), labels]
            counts = np.bincount(labels, minlength=k)
            _repair_empty_clusters(labels, errors, counts, k)
            updated = np.vstack([np.mean(X[labels == c], axis=0) for c in range(k)])
            movement = np.max(np.linalg.norm(updated - centroids, axis=1))
            centroids = updated
            if movement <= tol:
                break

        distances = np.sum((X[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
        labels = np.argmin(distances, axis=1)
        errors = distances[np.arange(n), labels]
        counts = np.bincount(labels, minlength=k)
        _repair_empty_clusters(labels, errors, counts, k)
        final_centroids = np.vstack([np.mean(X[labels == c], axis=0) for c in range(k)])
        inertia = float(np.sum((X - final_centroids[labels]) ** 2))
        if inertia < best_inertia:
            best_labels, best_inertia = labels.copy(), inertia

    return best_labels


def cluster_report(
    X: np.ndarray, labels: np.ndarray, columns: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Stage F: per-cluster size, dispersion and centroid.

    Dispersion is the mean distance from a cluster's own centroid -- reported because a size alone
    says nothing about whether a cluster is a tight group or a loose scatter, and a persona built
    on a loose cluster deserves less confidence.
    """
    labels = np.asarray(labels)
    clusters = []
    for cluster_id in sorted({int(label) for label in labels}):
        points = X[labels == cluster_id]
        centroid = points.mean(axis=0)
        distances = np.linalg.norm(points - centroid, axis=1)
        clusters.append({
            "cluster": cluster_id,
            "n": int(points.shape[0]),
            "share": _round(points.shape[0] / X.shape[0]),
            "dispersion": _round(float(distances.mean())),
            "dispersion_sd": _round(float(distances.std(ddof=1))) if points.shape[0] > 1 else None,
            "centroid": {
                columns[index]["label"]: _round(float(centroid[index]))
                for index in range(len(columns))
            },
        })
    total = np.vstack([X[labels == c["cluster"]].mean(axis=0) for c in clusters])
    return {
        "clusters": clusters,
        "inertia": _round(float(np.sum((X - total[np.searchsorted(
            [c["cluster"] for c in clusters], labels)]) ** 2)), 2),
    }


def level_vs_shape(X: np.ndarray) -> dict[str, Any]:
    """The share of total variance on the first principal component (§14 decision 4).

    Emitted so the sections turn knows whether type-based persona names are supported at all. On
    the reference fixture the dominant variance is a general liking factor: the clusters differ in
    DEGREE, not in profile, so "spicy lover vs sweet lover" names would describe structure the data
    does not contain. Computed by SVD on the centred matrix -- numpy only, no scipy.
    """
    centred = X - X.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    variance = singular ** 2
    total = float(variance.sum())
    share = float(variance[0] / total) if total > 0 else 0.0
    return {
        "pc1_variance_share": _round(share),
        "verdict": "level-dominated" if share >= 0.5 else "shape-bearing",
        "component_shares": [_round(float(v / total)) for v in variance[:3]] if total else [],
        "naming_guidance": (
            "clusters differ mainly in LEVEL: use level-based persona names (all_high, all_low, "
            "middle) and do not invent type-based names the data does not support"
            if share >= 0.5 else
            "clusters carry profile shape: type-based persona names may be supported"
        ),
    }


# ============================================================================
# Stages I, J and K -- overlap, persona assignment, centroids
# ============================================================================
#
# Two defects in the reference implementation are fixed here. Both were reproduced on real data,
# and both fail SILENTLY, which is why they matter more than their size suggests.
#
# DEFECT-1 (§13.1): np.argmax on an all-zero row returns index 0, so a cluster that no region
# contains was assigned the FIRST persona -- and with two deliberately empty boxes, EVERY cluster
# came back labelled with the first persona name. An empty region is a section-proposal error (a
# threshold above the observed maximum) and has to surface as one, so a cluster whose best overlap
# does not clear the threshold is `unassigned` and reported.
#
# DEFECT-2 (§13.2): each row's argmax is independent, so several clusters can collapse onto one
# persona. Observed on the fixture: tercile boxes at K=4 put clusters 2 AND 3 on `all_low`.
# Permitted here and REPORTED prominently, per §14 decision 3 -- but a persona holding more than
# one cluster gets its per-persona alignment WITHHELD, because "the all_low persona buys less
# often" is uninterpretable when all_low is two different groups of respondents.
#
# The threshold defaults to 0.0: a cluster is unassigned only when literally none of its points
# fall in any region. That is the minimal rule that fixes DEFECT-1 without inventing a cut. Raise
# it to demand a stronger match; every cluster's best overlap is reported either way, so a weak
# assignment is visible without the pipeline having to judge it.
MIN_CLUSTER_OVERLAP = float(os.getenv("ISOLATED_WORKFLOW_MIN_CLUSTER_OVERLAP", "0.0"))


def overlap_analysis(
    X: np.ndarray,
    labels: np.ndarray,
    sections: dict[str, tuple[np.ndarray, np.ndarray]],
    min_overlap: float = MIN_CLUSTER_OVERLAP,
) -> dict[str, Any]:
    """Stages I and J: containment overlap per cluster per region, then the assignment."""
    X = np.asarray(X, dtype=float)
    labels = np.asarray(labels)
    cluster_ids = [int(cluster) for cluster in np.unique(labels)]
    persona_ids = list(sections)
    matrix = np.zeros((len(cluster_ids), len(persona_ids)))

    for row, cluster_id in enumerate(cluster_ids):
        points = X[labels == cluster_id]
        for column, persona in enumerate(persona_ids):
            lower, upper = sections[persona]
            # Inclusive and conjunctive across every bounded dimension: a point is in the region
            # only if it is inside on all of them.
            inside = np.all((points >= lower) & (points <= upper), axis=1)
            matrix[row, column] = float(np.mean(inside)) if points.size else 0.0

    assignment: dict[int, str] = {}
    unassigned: list[dict[str, Any]] = []
    for row, cluster_id in enumerate(cluster_ids):
        best = float(matrix[row].max()) if persona_ids else 0.0
        if best <= min_overlap:
            assignment[cluster_id] = "unassigned"
            unassigned.append({
                "cluster": cluster_id,
                "best_overlap": _round(best),
                "reason": (
                    "no region contains any of this cluster's points; the regions were specified "
                    "above the observed range, which is a specification error rather than a "
                    "property of the respondents"
                    if best == 0.0 else
                    f"best overlap {best:.4f} does not clear the minimum {min_overlap}"
                ),
            })
            continue
        assignment[cluster_id] = persona_ids[int(np.argmax(matrix[row]))]

    holders: dict[str, list[int]] = {}
    for cluster_id, persona in assignment.items():
        if persona != "unassigned":
            holders.setdefault(persona, []).append(cluster_id)
    many_to_one = [
        {
            "persona": persona,
            "clusters": sorted(clusters),
            "n": int(sum((labels == cluster).sum() for cluster in clusters)),
            "consequence": (
                "this persona holds more than one cluster, so its per-persona readout is withheld: "
                "a single alignment for two different groups of respondents is uninterpretable. "
                "Merge the clusters or revise the regions"
            ),
        }
        for persona, clusters in holders.items() if len(clusters) > 1
    ]

    return {
        "cluster_ids": cluster_ids,
        "persona_ids": persona_ids,
        # The FULL matrix, always -- the assignment is one number per row out of this table, and
        # the rest of the row is what shows whether the choice was close.
        "overlap_matrix": [
            {
                "cluster": cluster_id,
                "n": int((labels == cluster_id).sum()),
                "overlaps": {
                    persona_ids[column]: _round(float(matrix[row, column]))
                    for column in range(len(persona_ids))
                },
                "best_overlap": _round(float(matrix[row].max())) if persona_ids else 0.0,
                "assigned": assignment[cluster_id],
            }
            for row, cluster_id in enumerate(cluster_ids)
        ],
        "assignment": assignment,
        "unassigned": unassigned,
        "many_to_one": many_to_one,
        "withhold_alignment": sorted(entry["persona"] for entry in many_to_one),
        "distinct_personas": len(holders),
        "min_overlap": min_overlap,
        "personas_matched_by_no_cluster": sorted(set(persona_ids) - set(holders)),
    }


def persona_centroids(
    X: np.ndarray,
    labels: np.ndarray,
    assignment: dict[int, str],
    columns: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stage K: the mean normalized attribute vector per persona, with size and dispersion.

    A persona built from several clusters gets one centroid over their union, and the member
    clusters are named -- so the reader can see that the centroid averages groups the overlap
    analysis could not separate.
    """
    labels = np.asarray(labels)
    grouped: dict[str, list[int]] = {}
    for cluster_id, persona in assignment.items():
        grouped.setdefault(persona, []).append(int(cluster_id))

    centroids = []
    for persona in sorted(grouped):
        clusters = sorted(grouped[persona])
        mask = np.isin(labels, clusters)
        points = X[mask]
        centroid = points.mean(axis=0)
        distances = np.linalg.norm(points - centroid, axis=1)
        centroids.append({
            "persona": persona,
            "clusters": clusters,
            "n": int(points.shape[0]),
            "share": _round(points.shape[0] / X.shape[0]),
            "dispersion": _round(float(distances.mean())),
            "centroid": {
                columns[index]["label"]: _round(float(centroid[index]))
                for index in range(len(columns))
            },
            "spans_multiple_clusters": len(clusters) > 1,
        })
    return centroids


# ============================================================================
# Stages L, M and N -- PLSR, fit, correlation
# ============================================================================
#
# PLSR rather than OLS because the design is WIDE at the centroid grain: p attributes against K
# observations, with an attribute battery that is collinear by construction. OLS is unstable or
# undefined there. NIPALS is about forty lines of numpy, which matters because scipy and
# scikit-learn are not installed (§2.6) and adding them is a dependency decision, not an
# implementation detail.
#
# The measured fact that shapes everything reported here: at the centroid grain R² SATURATES TO
# EXACTLY 1.0000 at K-1 components. It counts components, not relationship strength. On the
# reference fixture, K=4:
#
#   grain               n    R² @1    @2    @K-1     LOO Q² @1
#   centroids           4   0.9665  0.9981  1.0000     0.45
#   cluster x product  16   0.8855  0.9169  0.9219       --
#
# So: components capped at min(n-2, p); R² never emitted without n, grain and component count
# beside it; Q² is the primary measure and R² a diagnostic; and the cluster x product grain is
# emitted as a second fit whenever there are at least two products, because it does not saturate
# and is the defensible R² to quote.


def component_cap(n: int, p: int) -> int:
    """The most components worth fitting for n observations of p variables.

    min(n-2, p) at the centroid grain, where n is K. Two fewer than the observation count rather
    than one, because at n-1 the fit is already saturated and reports 1.0000 whatever the data.
    """
    return max(1, min(n - 2, p))


def _pls1_nipals(X: np.ndarray, y: np.ndarray, n_components: int) -> dict[str, np.ndarray]:
    """NIPALS PLS regression for a single response. X and y must already be centred/scaled."""
    residual_x, residual_y = X.copy(), y.copy()
    n, p = X.shape
    scores = np.zeros((n, n_components))
    weights = np.zeros((p, n_components))
    loadings = np.zeros((p, n_components))
    y_loadings = np.zeros(n_components)
    used = 0
    for component in range(n_components):
        weight = residual_x.T @ residual_y
        norm = np.linalg.norm(weight)
        if norm == 0:
            break
        weight = weight / norm
        score = residual_x @ weight
        score_ss = float(score @ score)
        if score_ss == 0:
            break
        loading = (residual_x.T @ score) / score_ss
        y_loading = float((residual_y @ score) / score_ss)
        residual_x = residual_x - np.outer(score, loading)
        residual_y = residual_y - y_loading * score
        scores[:, component] = score
        weights[:, component] = weight
        loadings[:, component] = loading
        y_loadings[component] = y_loading
        used = component + 1
    return {
        "scores": scores[:, :used], "weights": weights[:, :used],
        "loadings": loadings[:, :used], "y_loadings": y_loadings[:used], "used": used,
    }


def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Column means and sds for standardizing the PLSR input.

    X columns are standardized, not merely centred. Verified against the analyst's measured table:
    standardizing reproduces every R² figure at both grains and both K, while centring alone does
    not -- so this is the configuration those numbers were taken with.
    """
    mean = X.mean(axis=0)
    sd = X.std(axis=0, ddof=1) if X.shape[0] > 1 else np.ones(X.shape[1])
    return mean, np.where(sd > 0, sd, 1.0)


def _vip(model: dict[str, np.ndarray], total_ss: float) -> np.ndarray:
    """Variable importance in projection -- the per-attribute importance answer (§11.3)."""
    scores, weights, y_loadings = model["scores"], model["weights"], model["y_loadings"]
    if model["used"] == 0 or total_ss == 0:
        return np.zeros(weights.shape[0])
    explained = np.array([
        float(y_loadings[c] ** 2 * (scores[:, c] @ scores[:, c])) for c in range(model["used"])
    ])
    if explained.sum() == 0:
        return np.zeros(weights.shape[0])
    p = weights.shape[0]
    return np.sqrt(p * (weights ** 2 @ explained) / explained.sum())


def plsr_fit(X: np.ndarray, y: np.ndarray, n_components: int) -> dict[str, Any]:
    """Fit PLSR and return the fit with everything needed to report it honestly."""
    mean, sd = _standardize(X)
    y_mean = float(y.mean())
    model = _pls1_nipals((X - mean) / sd, y - y_mean, n_components)
    predicted = y_mean + model["scores"] @ model["y_loadings"]
    total_ss = float(((y - y_mean) ** 2).sum())
    residual_ss = float(((y - predicted) ** 2).sum())
    return {
        "n": int(X.shape[0]),
        "n_components": int(model["used"]),
        "r2": _round(1 - residual_ss / total_ss) if total_ss else None,
        "predicted": predicted,
        "scores": model["scores"],
        "y_loadings": model["y_loadings"],
        "vip": _vip(model, total_ss),
        "mean_absolute_error": _round(float(np.abs(y - predicted).mean())),
    }


def loo_q2(X: np.ndarray, y: np.ndarray, n_components: int) -> float | None:
    """Leave-one-out cross-validated R². The primary measure at the centroid grain.

    Q² is what says whether the relationship is real, and it is routinely far below R² here: on
    the fixture R² reads 0.97 at one component while Q² reads 0.45. Reporting R² alone would
    overstate the finding by roughly a factor of two.
    """
    n = X.shape[0]
    if n < 4:
        return None
    press = 0.0
    for held_out in range(n):
        keep = np.ones(n, dtype=bool)
        keep[held_out] = False
        mean, sd = _standardize(X[keep])
        y_mean = float(y[keep].mean())
        model = _pls1_nipals((X[keep] - mean) / sd, y[keep] - y_mean, n_components)
        residual = (X[held_out] - mean) / sd
        prediction = y_mean
        for component in range(model["used"]):
            score = float(residual @ model["weights"][:, component])
            prediction += model["y_loadings"][component] * score
            residual = residual - score * model["loadings"][:, component]
        press += float((y[held_out] - prediction) ** 2)
    total_ss = float(((y - y.mean()) ** 2).sum())
    return _round(1 - press / total_ss) if total_ss else None


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    """Spearman correlation via Pearson on ranks. Average ranks for ties -- numpy only."""
    if a.size < 3:
        return None
    def ranks(values: np.ndarray) -> np.ndarray:
        order = np.argsort(values, kind="stable")
        result = np.empty(values.size, dtype=float)
        result[order] = np.arange(values.size, dtype=float)
        # Average the ranks within each tie group, or tied values would be ordered arbitrarily.
        for value in np.unique(values):
            tied = values == value
            result[tied] = result[tied].mean()
        return result
    return _pearson(ranks(a), ranks(b))


def _pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    if a.size < 2 or a.std() == 0 or b.std() == 0:
        return None
    return _round(float(np.corrcoef(a, b)[0, 1]))


def alignment_in_words(
    sign: float, encoding: dict[str, Any], measure: str
) -> dict[str, Any]:
    """Turn an alignment sign into a claim a reader can check (§11.3).

    Derived from the stated ordered sequence, not from parsing the direction prose: the LAST
    category of the sequence is the high end by construction, so a positive alignment means the
    persona sits toward that label. This is the step that stops a sign being reported backwards --
    on a scale whose codes run "Once a day"=1 to "Never"=8, "positively aligned with the KPI
    variable" means buying LESS often, and a report that says otherwise is exactly wrong.
    """
    sequence = encoding.get("ordered_categories") or []
    if sequence:
        low_end, high_end = str(sequence[0]), str(sequence[-1])
    else:
        anchors = encoding.get("anchors") or []
        low_end = str(anchors[0]) if anchors else "the low end of the scale"
        high_end = str(anchors[-1]) if len(anchors) > 1 else "the high end of the scale"
    if sign > 0:
        label, toward = "positive", high_end
    elif sign < 0:
        label, toward = "negative", low_end
    else:
        return {
            "alignment": "none",
            "in_words": f"no measurable alignment with {measure}",
            "direction_recorded": encoding.get("direction"),
        }
    return {
        "alignment": label,
        "in_words": f"sits toward {toward!r} on {measure}",
        "high_end": high_end,
        "low_end": low_end,
        "direction_recorded": encoding.get("direction"),
    }


# KPI encodings this pipeline can turn into a single number per respondent, and therefore fit at
# the centroid grain. Everything else is reported as not fitted, WITH ITS REASON -- §16's rule that
# an unsupported case gets an explicit response rather than a silent skip. A wrong number here
# would be worse than an absent one, because a fit implies the KPI was a scale.
_FITTABLE_ENCODINGS = frozenset({"numeric", "ordinal"})
_UNFITTABLE_REASONS = {
    "nominal": (
        "a nominal KPI has no order, so there is no single value to average per persona. The "
        "per-persona category distribution is reported instead, and the individual-level model "
        "uses a named pseudo-R-squared"
    ),
    "jar": (
        "a JAR KPI is worse in both directions, so its mean is not interpretable as a level. It "
        "needs a below/just-right/above contrast, which this build does not compute"
    ),
    "binary": (
        "a binary KPI is fitted at the individual level with a named pseudo-R-squared rather than "
        "at the centroid grain, where a mean would read as a rate"
    ),
}


def kpi_values(
    rows: Sequence[dict[str, Any]],
    grain_keys: Sequence[tuple[str, str]],
    question_id: str,
    encoding: dict[str, Any],
    scale_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one KPI to a number per grain key, following its resolved encoding.

    Excluded categories are dropped and counted, never mapped to an extreme value: a
    non-consumer answer is not the lowest frequency, and ranking it as one silently invents an
    observation at the end of a scale it is not on.
    """
    kind = encoding.get("encoding")
    excluded = {str(label) for label in (encoding.get("excluded_categories") or [])}
    by_key: dict[tuple[str, str], Any] = {}
    for row in rows:
        if str(row.get("question_id")) != str(question_id):
            continue
        by_key[(row["enrollment_id"], row["product_id"] or "")] = row["value"]

    values: dict[tuple[str, str], float] = {}
    dropped: dict[str, int] = {}
    if kind == "ordinal":
        sequence = [str(label) for label in (encoding.get("ordered_categories") or [])]
        rank = {label: position for position, label in enumerate(sequence)}
        span = max(len(sequence) - 1, 1)
        for key, value in by_key.items():
            label = str(value)
            if label in excluded or label not in rank:
                dropped[label] = dropped.get(label, 0) + 1
                continue
            values[key] = rank[label] / span
    elif kind == "numeric":
        low, high = None, None
        if scale_metadata:
            bounds = _design_range(scale_metadata)
            if bounds:
                low, high = bounds
        for key, value in by_key.items():
            number = _numeric(value)
            if number is None:
                dropped[str(value)] = dropped.get(str(value), 0) + 1
                continue
            values[key] = (number - low) / (high - low) if low is not None else number

    ordered = [key for key in grain_keys if key in values]
    return {
        "encoding": kind,
        "fittable": kind in _FITTABLE_ENCODINGS and bool(ordered),
        "reason": None if kind in _FITTABLE_ENCODINGS else _UNFITTABLE_REASONS.get(
            kind, f"encoding {kind!r} is not supported for a KPI fit"
        ),
        "keys": ordered,
        "values": np.array([values[key] for key in ordered], dtype=float),
        "categories": {
            str(value): sum(1 for other in by_key.values() if str(other) == str(value))
            for value in {str(v) for v in by_key.values()}
        },
        "excluded_counts": dropped,
        "n": len(ordered),
    }


def _logistic_mcfadden(X: np.ndarray, y: np.ndarray) -> dict[str, Any] | None:
    """Binary logistic regression by Newton-Raphson, returning McFadden's pseudo-R-squared.

    Named explicitly wherever it is reported. A pseudo-R-squared is NOT an R-squared -- it does not
    measure explained variance and the two are not comparable -- and labelling it as one would
    invite exactly that comparison.
    """
    n, p = X.shape
    if n <= p + 1 or len(np.unique(y)) != 2:
        return None
    design = np.column_stack([np.ones(n), X])
    beta = np.zeros(design.shape[1])
    for _iteration in range(50):
        odds = design @ beta
        probability = 1.0 / (1.0 + np.exp(-np.clip(odds, -30, 30)))
        weight = probability * (1 - probability)
        gradient = design.T @ (y - probability)
        hessian = design.T @ (design * weight[:, None])
        try:
            step = np.linalg.solve(
                hessian + 1e-8 * np.eye(design.shape[1]), gradient
            )
        except np.linalg.LinAlgError:
            return None
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break

    def log_likelihood(scores: np.ndarray) -> float:
        probability = 1.0 / (1.0 + np.exp(-np.clip(scores, -30, 30)))
        probability = np.clip(probability, 1e-12, 1 - 1e-12)
        return float(np.sum(y * np.log(probability) + (1 - y) * np.log(1 - probability)))

    base_rate = float(y.mean())
    null_scores = np.full(n, np.log(base_rate / (1 - base_rate))) if 0 < base_rate < 1 else None
    if null_scores is None:
        return None
    fitted, null = log_likelihood(design @ beta), log_likelihood(null_scores)
    return {
        "measure": "McFadden pseudo-R-squared",
        "value": _round(1 - fitted / null) if null else None,
        "n": n,
        "note": "a pseudo-R-squared is not an R-squared and the two are not comparable",
    }


def individual_level_validation(
    X: np.ndarray, kpi: dict[str, Any], index: np.ndarray
) -> dict[str, Any]:
    """§11.6: a row-grain model beside the centroid fit, to show the relationship is not an
    artefact of four or five points.

    The model family follows the KPI's declared encoding, and the measure is named accordingly --
    an ordinary R-squared for a numeric or ordinal KPI, a named pseudo-R-squared otherwise.
    """
    rows = X[index]
    y = kpi["values"]
    if rows.shape[0] < rows.shape[1] + 2:
        return {"fitted": False, "reason": f"only {rows.shape[0]} row(s) for {rows.shape[1]} attributes"}

    if kpi["encoding"] in _FITTABLE_ENCODINGS:
        design = np.column_stack([np.ones(rows.shape[0]), rows])
        coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
        predicted = design @ coefficients
        total_ss = float(((y - y.mean()) ** 2).sum())
        residual_ss = float(((y - predicted) ** 2).sum())
        return {
            "fitted": True,
            "model": "ordinary least squares at row grain",
            "measure": "R-squared",
            "value": _round(1 - residual_ss / total_ss) if total_ss else None,
            "n": int(rows.shape[0]),
            "grain": "respondent x product row",
            "pearson_with_first_component": None,
        }

    # Non-numeric: one-vs-rest on the most common category, reported as a named pseudo-R-squared.
    categories = kpi.get("categories") or {}
    if not categories:
        return {"fitted": False, "reason": "no observed categories to model"}
    target = max(categories, key=lambda label: categories[label])
    indicator = np.array([1.0 if str(value) == target else 0.0 for value in y], dtype=float)
    logistic = _logistic_mcfadden(rows, indicator)
    if logistic is None:
        return {"fitted": False, "reason": "the logistic model did not converge"}
    return {
        "fitted": True,
        "model": f"binary logistic, {target!r} versus the rest",
        "grain": "respondent x product row",
        **logistic,
    }


def fit_persona_kpi(
    X: np.ndarray,
    labels: np.ndarray,
    grain_keys: Sequence[tuple[str, str]],
    columns: Sequence[dict[str, Any]],
    kpi: dict[str, Any],
    measure_name: str,
    encoding: dict[str, Any],
    withhold: Sequence[str] = (),
    assignment: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Stages L, M and N for one KPI: both grains, Q², per-persona readout, correlations, VIP."""
    if not kpi["fittable"]:
        return {
            "kpi": measure_name,
            "encoding": kpi["encoding"],
            "fitted": False,
            "reason": kpi["reason"],
            "category_distribution": kpi["categories"],
            "excluded": kpi["excluded_counts"],
        }

    position = {key: index for index, key in enumerate(grain_keys)}
    index = np.array([position[key] for key in kpi["keys"]])
    rows, y = X[index], kpi["values"]
    row_labels = labels[index]
    clusters = sorted({int(label) for label in row_labels})
    attribute_labels = [column["label"] for column in columns]

    centroids = np.vstack([rows[row_labels == cluster].mean(axis=0) for cluster in clusters])
    centroid_y = np.array([y[row_labels == cluster].mean() for cluster in clusters])
    cap = component_cap(len(clusters), rows.shape[1])
    centroid = plsr_fit(centroids, centroid_y, cap)
    centroid_fit = {
        "grain": "cluster centroid",
        "n": centroid["n"],
        "n_components": centroid["n_components"],
        "component_cap": cap,
        # The saturation curve itself, because it is the evidence for the claim beside it: R²
        # climbing towards 1.0000 as components are added is what shows the figure counts
        # components rather than measuring the relationship.
        "r2_by_components": [
            {"n_components": count, "r2": plsr_fit(centroids, centroid_y, count)["r2"]}
            for count in range(1, cap + 1)
        ],
        # R² NEVER travels without n, grain and component count -- it saturates to exactly 1.0000
        # at n-1 components here, so the number is meaningless without them.
        "r2": centroid["r2"],
        "r2_is": "a diagnostic only; at this grain it counts components, not fit",
        "q2": loo_q2(centroids, centroid_y, cap),
        "q2_is": "the primary measure of the relationship, cross-validated leave-one-out",
        "descriptive": True,
        "descriptive_note": (
            f"built on {centroid['n']} centroids, so this is DESCRIPTIVE -- not predictive, "
            "significant or validated"
        ),
    }

    products = sorted({key[1] for key in kpi["keys"] if key[1]})
    cluster_product = None
    if len(products) >= 2:
        grid_x, grid_y, grid_cells = [], [], []
        for cluster in clusters:
            for product in products:
                mask = (row_labels == cluster) & np.array(
                    [key[1] == product for key in kpi["keys"]]
                )
                if mask.sum():
                    grid_x.append(rows[mask].mean(axis=0))
                    grid_y.append(float(y[mask].mean()))
                    grid_cells.append({"cluster": cluster, "product": product,
                                       "n": int(mask.sum())})
        grid_x, grid_y = np.array(grid_x), np.array(grid_y)
        grid_cap = component_cap(len(grid_y), rows.shape[1])
        grid = plsr_fit(grid_x, grid_y, grid_cap)
        # The per-persona readout comes off the ONE-component model, which is the parsimonious fit
        # the quotable R² is stated at; taking it from a higher-component fit would compare
        # personas through a model nobody quotes.
        parsimonious = plsr_fit(grid_x, grid_y, 1)
        per_persona_products = [
            per_persona_product_readout(
                grid_x, grid_y, grid_cells, parsimonious["predicted"], cluster
            )
            for cluster in clusters
        ]
        cluster_product = {
            "grain": "cluster x product",
            "n": grid["n"],
            "n_components": grid["n_components"],
            "r2": grid["r2"],
            "r2_by_components": [
                {"n_components": count, "r2": plsr_fit(grid_x, grid_y, count)["r2"]}
                for count in range(1, min(grid_cap, 3) + 1)
            ],
            "r2_is": "does not saturate at this grain, so this is the quotable R-squared",
            "q2": loo_q2(grid_x, grid_y, grid_cap),
            "cells": grid_cells,
            "per_persona_products": per_persona_products,
            "per_persona_basis": "one-component model, the parsimonious quotable fit",
        }

    # Per-persona readout: the X-score signed against the Y-loading (§11.3). Not a loading
    # question -- loadings are per attribute, and the question asked is about personas.
    first_score = centroid["scores"][:, 0] if centroid["n_components"] else np.zeros(len(clusters))
    first_y_loading = float(centroid["y_loadings"][0]) if centroid["n_components"] else 0.0
    per_persona = []
    for offset, cluster in enumerate(clusters):
        persona = (assignment or {}).get(cluster, f"cluster {cluster}")
        score = float(first_score[offset])
        words = alignment_in_words(score * first_y_loading, encoding, measure_name)
        mask = row_labels == cluster
        entry = {
            "cluster": cluster,
            "persona": persona,
            "n": int(mask.sum()),
            "t1": _round(score),
            "q1": _round(first_y_loading),
            "mean_kpi_normalized": _round(float(y[mask].mean())),
            **words,
        }
        if persona in set(withhold):
            entry["withheld"] = (
                f"persona {persona!r} holds more than one cluster, so a single alignment for it "
                "would average groups the overlap analysis could not separate"
            )
        per_persona.append(entry)

    correlations = [
        {
            "attribute": attribute_labels[column],
            "pearson": _pearson(centroids[:, column], centroid_y),
            "spearman": _spearman(centroids[:, column], centroid_y),
            "n": len(clusters),
            "grain": "cluster centroid",
            "vip": _round(float(centroid["vip"][column])) if centroid["vip"].size else None,
            "exploratory": True,
        }
        for column in range(len(attribute_labels))
    ]

    return {
        "kpi": measure_name,
        "encoding": kpi["encoding"],
        "direction": encoding.get("direction"),
        "fitted": True,
        "primary_measure": "q2",
        "centroid_grain": centroid_fit,
        "cluster_product_grain": cluster_product,
        "per_persona": per_persona,
        "correlations": correlations,
        "correlations_are": "exploratory, not causal",
        "individual_level": individual_level_validation(X, kpi, index),
        "excluded": kpi["excluded_counts"],
        "analysis_n": kpi["n"],
    }


# The number of products a persona must span before a per-persona Q² is even attempted. Measured:
# with 4 products, leave-one-out inside a persona trains on 3 points and EVERY persona's Q² came
# back negative -- worse than predicting the persona's own mean, so no predictive validity at all.
# Ranking personas on that number would be ranking noise, so it is emitted as null with its reason
# rather than computed and quietly published.
PER_PERSONA_Q2_MIN_PRODUCTS = int(
    os.getenv("ISOLATED_WORKFLOW_PER_PERSONA_Q2_MIN_PRODUCTS", "6")
)


def per_persona_product_readout(
    cells: np.ndarray,
    cell_y: np.ndarray,
    cell_info: Sequence[dict[str, Any]],
    global_predictions: np.ndarray,
    cluster: int,
) -> dict[str, Any]:
    """The "which persona fits this product best" figures for one cluster (§11.8).

    Three separate things, because the question turns out to need all three and the obvious single
    answer does not work:

      - a WITHIN-persona fit across its products, reported as in-sample R² and descriptive. This is
        the persona's own attribute-to-KPI relationship.
      - the correlation between that fit's first score and the persona's mean KPI, across products,
        with n_products attached. This is the "better" half of the question.
      - the prediction error the GLOBAL model makes on this persona's cells. This is the "fit" half,
        and it is what actually compares personas: small and comparable errors mean the global
        relationship explains every persona about equally well.

    `per_persona_q2` is deliberately null below the product threshold -- see the constant above.
    """
    rows = [index for index, info in enumerate(cell_info) if info["cluster"] == cluster]
    if not rows:
        return {"cluster": cluster, "n_products": 0,
                "reason": "this cluster has no product cells"}
    persona_x, persona_y = cells[rows], cell_y[rows]
    n_products = len({cell_info[index]["product"] for index in rows})
    errors = np.abs(persona_y - global_predictions[rows])

    readout: dict[str, Any] = {
        "cluster": cluster,
        "n_products": n_products,
        "prediction_error": {
            "mean_absolute": _round(float(errors.mean())),
            "max_absolute": _round(float(errors.max())),
            "grain": "cluster x product, from the global model",
            "note": (
                "comparable errors across personas mean the global relationship explains them "
                "about equally well, which is itself the answer to which fits best"
            ),
        },
        "per_persona_q2": None,
        "per_persona_q2_reason": (
            f"n_products too small ({n_products}); needs >= {PER_PERSONA_Q2_MIN_PRODUCTS}. "
            "Measured at 4 products, leave-one-out inside a persona trains on 3 points and every "
            "persona's Q-squared came back negative, so ranking on it would rank noise"
        ),
    }
    if n_products >= 3:
        within = plsr_fit(persona_x, persona_y, 1)
        readout["within_persona_fit"] = {
            "r2": within["r2"],
            "n": within["n"],
            "n_components": within["n_components"],
            "grain": "product cells inside this persona",
            "descriptive": True,
            "descriptive_note": f"in-sample on {within['n']} product(s); not cross-validated",
        }
        readout["correlation_across_products"] = {
            "pearson": _pearson(within["scores"][:, 0], persona_y),
            "spearman": _spearman(within["scores"][:, 0], persona_y),
            "n_products": n_products,
            "basis": "the persona's own first PLS score against its mean KPI, across products",
            "descriptive": True,
        }
    if n_products >= PER_PERSONA_Q2_MIN_PRODUCTS:
        readout["per_persona_q2"] = loo_q2(persona_x, persona_y, 1)
        readout["per_persona_q2_reason"] = None
    return readout


# ============================================================================
# Persona profiling (§11.7)
# ============================================================================
#
# Profiles are EXHAUSTIVE, not differential. Measured on Herbalife -- 4 clusters x 5 demographic
# questions, 450 joinable rows -- the exhaustive form is 20 cells and 2,574 chars while the
# differential form (only deviations of 10 points or more from the base) is a SINGLE row of 104
# chars. That near-emptiness is an honest finding, since these personas barely differ
# demographically, but it answers almost nothing: a later "what defines persona 3" has nothing to
# read. So the full distributions ship and `distinguishing` sits on top of them.
#
# Demographics are discovered rather than required from the caller. A screener named with
# role="screener" is never a clustering dimension (§15), but a profile that omitted the survey's
# own demographic questions would be useless for the question profiles exist to answer.
_DEMOGRAPHICS_SQL = """
SELECT a.enrollment_id::text AS enrollment_id,
       q.id::text            AS question_id,
       COALESCE(NULLIF(BTRIM(q.settings->>'chartTitle'), ''), q.prompt) AS question,
       qo.label              AS label
FROM answer a
JOIN question q ON q.id = a.question_id
JOIN answered_question_options aqo ON aqo.answer_id = a.id
JOIN question_option qo
  ON qo.id = COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)
WHERE q."surveyId" = CAST(:survey_id AS uuid)
  AND q."typeOfQuestion" = 'multiple-choice'
  AND a.product_id IS NULL
  AND NOT COALESCE(a."isSkipped", false)
  AND NULLIF(BTRIM(qo.label), '') IS NOT NULL
"""

# A fact deviating from the survey base by this much or more is called out in `distinguishing`.
# The value is §11.7's own measured cut, kept so the differential view stays comparable with the
# figures that motivated the exhaustive one.
DISTINGUISHING_DELTA = float(os.getenv("ISOLATED_WORKFLOW_DISTINGUISHING_DELTA", "0.10"))


def fetch_demographics(
    connect: Callable[[], Any], survey_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Every non-product categorical answer in the survey, keyed by enrollment."""
    with connect() as conn:
        rows = conn.execute(text(_DEMOGRAPHICS_SQL), {"survey_id": survey_id}).mappings().all()
    by_enrollment: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_enrollment.setdefault(row["enrollment_id"], []).append({
            "question_id": row["question_id"],
            "question": row["question"],
            "label": row["label"],
        })
    return by_enrollment


def assess_joinability(
    analysis_enrollments: set[str], demographic_enrollments: set[str]
) -> dict[str, Any]:
    """Can the demographics be attached to the analysed respondents at all? (§11.7b)

    Measured DB-wide: of 82 surveys holding both product answers and non-product categorical
    questions, 76 join by enrollment and 6 do not. The primary fixture is one of the 6 -- its
    1,200 attribute enrollments and its 1,200 screener enrollments are DISJOINT, overlap zero --
    and there is no fallback key, because user_id and panelist_id are null for all 2,400 of its
    enrollments.
    """
    overlap = analysis_enrollments & demographic_enrollments
    if not demographic_enrollments:
        verdict, note = "no_demographics", "the survey has no non-product categorical questions"
    elif not overlap:
        verdict, note = "disjoint", (
            "the demographic questions were answered by a different set of enrollments than the "
            "attributes, and there is no fallback key, so no demographic claim can be made about "
            "these respondents. Joining the other set would describe different people"
        )
    elif len(overlap) < len(analysis_enrollments):
        verdict, note = "partial", (
            f"only {len(overlap)} of {len(analysis_enrollments)} analysed enrollments answered "
            "the demographic questions; shares below are over the joined subset"
        )
    else:
        verdict, note = "joined", "every analysed enrollment answered the demographic questions"
    return {
        "joinability": verdict,
        "analysis_enrollments": len(analysis_enrollments),
        "demographic_enrollments": len(demographic_enrollments),
        "joined_enrollments": len(overlap),
        "note": note,
    }


def _distribution(labels: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _raw_units(value: float, design_range: Sequence[float] | None) -> str | None:
    """A normalized value rendered the way a person reads it: "8.1 of 9"."""
    if not design_range or len(design_range) != 2:
        return None
    low, high = float(design_range[0]), float(design_range[1])
    return f"{low + value * (high - low):.1f} of {high:g}"


def profile_personas(
    X: np.ndarray,
    labels: np.ndarray,
    grain_keys: Sequence[tuple[str, str]],
    columns: Sequence[dict[str, Any]],
    assignment: dict[int, str],
    sections_report: Sequence[dict[str, Any]],
    overlap_rows: Sequence[dict[str, Any]],
    kpi_fits: Sequence[dict[str, Any]],
    demographics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build one exhaustive profile per persona (§11.7).

    Every field exists so a later question is answerable from the profile alone, without re-running
    the analysis: that is the whole point of (a), and it is why the demographics are full
    distributions rather than only the deviations.
    """
    labels = np.asarray(labels)
    analysis_enrollments = {key[0] for key in grain_keys}
    joinability = assess_joinability(analysis_enrollments, set(demographics))
    joinable = joinability["joinability"] in {"joined", "partial"}

    # Base shares over the JOINED population, so a persona share and its base are comparable.
    base_counts: dict[str, dict[str, int]] = {}
    question_names: dict[str, str] = {}
    if joinable:
        for enrollment in analysis_enrollments & set(demographics):
            for answer in demographics[enrollment]:
                question_names[answer["question_id"]] = answer["question"]
                base_counts.setdefault(answer["question_id"], {})
                base_counts[answer["question_id"]][answer["label"]] = (
                    base_counts[answer["question_id"]].get(answer["label"], 0) + 1
                )
    base_shares = {
        question_id: {
            label: _round(count / sum(counts.values()))
            for label, count in counts.items()
        }
        for question_id, counts in base_counts.items()
    }

    specs = {entry["persona"]: entry for entry in sections_report}
    overlap_by_cluster = {row["cluster"]: row for row in overlap_rows}
    grouped: dict[str, list[int]] = {}
    for cluster, persona in assignment.items():
        grouped.setdefault(persona, []).append(int(cluster))

    profiles = []
    for persona in sorted(grouped):
        clusters = sorted(grouped[persona])
        mask = np.isin(labels, clusters)
        members = [key for key, keep in zip(grain_keys, mask, strict=False) if keep]
        centroid = X[mask].mean(axis=0)
        spec = specs.get(persona, {})

        demographic_block = None
        distinguishing: list[dict[str, Any]] = []
        if joinable:
            persona_enrollments = {key[0] for key in members} & set(demographics)
            per_question: dict[str, list[str]] = {}
            for enrollment in persona_enrollments:
                for answer in demographics[enrollment]:
                    per_question.setdefault(answer["question_id"], []).append(answer["label"])
            demographic_block = []
            for question_id, answers in per_question.items():
                counts = _distribution(answers)
                total = sum(counts.values())
                shares = {label: _round(count / total) for label, count in counts.items()}
                demographic_block.append({
                    "question_id": question_id,
                    "question": question_names.get(question_id),
                    "n": total,
                    "distribution": counts,
                    "shares": shares,
                    "base_shares": base_shares.get(question_id, {}),
                })
                for label, share in shares.items():
                    base = base_shares.get(question_id, {}).get(label, 0.0)
                    if share is not None and abs(share - base) >= DISTINGUISHING_DELTA:
                        distinguishing.append({
                            "question": question_names.get(question_id),
                            "label": label,
                            "share": share,
                            "base": base,
                            "delta": _round(share - base),
                        })
            distinguishing.sort(key=lambda item: -abs(item["delta"] or 0))

        kpi_summary = []
        for fit in kpi_fits:
            entry: dict[str, Any] = {"kpi": fit.get("kpi"), "encoding": fit.get("encoding")}
            if fit.get("fitted"):
                matching = [
                    row for row in fit["per_persona"] if row["persona"] == persona
                ]
                entry["mean_normalized"] = (
                    _round(float(np.mean([row["mean_kpi_normalized"] for row in matching])))
                    if matching else None
                )
                entry["n"] = int(sum(row["n"] for row in matching))
                entry["alignment"] = [
                    {"cluster": row["cluster"], "alignment": row["alignment"],
                     "in_words": row["in_words"], "withheld": row.get("withheld")}
                    for row in matching
                ]
            else:
                entry["not_fitted"] = fit.get("reason")
                entry["category_distribution"] = fit.get("category_distribution")
            kpi_summary.append(entry)

        profiles.append({
            "persona": persona,
            "clusters": clusters,
            "n": int(mask.sum()),
            "share_of_respondents": _round(float(mask.mean())),
            "region": {
                "spec": spec.get("resolved"),
                "bounded_attributes": spec.get("bounded_attributes"),
                "mass": spec.get("mass"),
                "divergences": spec.get("divergences"),
                "overlap": {
                    cluster: overlap_by_cluster.get(cluster, {}).get("best_overlap")
                    for cluster in clusters
                },
            },
            "centroid": {
                "normalized": {
                    columns[index]["label"]: _round(float(centroid[index]))
                    for index in range(len(columns))
                },
                # Raw units are what a human reads: "8.1 of 9" rather than 0.89.
                "raw": {
                    columns[index]["label"]: _raw_units(
                        float(centroid[index]), columns[index].get("design_range")
                    )
                    for index in range(len(columns))
                },
            },
            "kpi_summary": kpi_summary,
            "joinability": joinability["joinability"],
            # Explicitly null when disjoint, never omitted and never filled from the other set.
            "demographics": demographic_block,
            "distinguishing": distinguishing,
            "spans_multiple_clusters": len(clusters) > 1,
        })

    return {"personas": profiles, "joinability": joinability,
            "distinguishing_delta": DISTINGUISHING_DELTA}


# ============================================================================
# Step 9.4 -- the report turn
# ============================================================================

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
# Numbers a report may use without them appearing in the payload: section counts, ordinals in prose
# and the like. Kept deliberately tiny -- every genuine figure has to come from a stage output.
_FREE_NUMBERS = frozenset({"0", "1", "2", "3", "100"})


def _payload_numbers(payload: Any) -> set[str]:
    """Every numeric token in the stage outputs, in the forms a report might quote them."""
    serialized = json.dumps(payload, default=str)
    numbers: set[str] = set()
    for token in _NUMBER_RE.findall(serialized):
        numbers.add(token)
        numbers.add(token.lstrip("-"))
        if "." in token:
            # A figure written without its trailing zeros is still the same figure.
            numbers.add(token.rstrip("0").rstrip("."))
    return numbers


def invented_numbers(report: str, payload: Any) -> list[str]:
    """Numbers in `report` that do not appear in the stage outputs (§9.4).

    This is what makes "every number must come from a stage output" a check rather than a hope. It
    catches the two failures that matter: a figure derived on the spot (a percentage, a total, a
    difference) and a figure re-rounded from the one that was measured -- 0.51 in place of 0.5071
    reads as precision the analysis never had.
    """
    available = _payload_numbers(payload)
    missing = []
    for token in _NUMBER_RE.findall(report or ""):
        if token in _FREE_NUMBERS or token in available:
            continue
        if token.lstrip("-") in available or token.rstrip("0").rstrip(".") in available:
            continue
        missing.append(token)
    # Deduplicated, order preserved, so the retry message names each once.
    seen: set[str] = set()
    return [n for n in missing if not (n in seen or seen.add(n))]


def validate_report(report: str, payload: Any) -> list[str]:
    """Return every contract violation in the report turn's output."""
    errors: list[str] = []
    text_value = report or ""
    for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
        if section not in text_value:
            errors.append(f"the {section.rstrip(':')} section is missing")
    order = [text_value.find(section) for section in
             ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:")]
    if all(position >= 0 for position in order) and order != sorted(order):
        errors.append("the three sections must appear in the order RESULT, DIAGNOSTICS, "
                      "STILL MISSING")
    invented = invented_numbers(text_value, payload)
    if invented:
        errors.append(
            "these numbers do not appear in the stage outputs, so they were computed, converted or "
            f"re-rounded rather than reported: {invented[:12]}. Quote the measured figures exactly"
        )
    for banned, why in (
        ("{{", "clickable suggestions belong to the calling agent, not to this report"),
        ("```gpi-", "chart blocks belong to the calling agent"),
    ):
        if banned in text_value:
            errors.append(f"remove {banned!r}: {why}")
    return errors


def compose_report(
    model: Any,
    payload: dict[str, Any],
    fallback: str,
    system_prompt: str = WORKFLOW_SYSTEM_PROMPT,
) -> dict[str, Any]:
    """Run the §9.4 turn, falling back to the deterministic report if it cannot comply.

    The fallback is not a silent substitution: it is recorded, and it is a CORRECT report -- the
    deterministic block is assembled from the same stage outputs. Failing the whole run because the
    prose turn misbehaved would discard a complete analysis over its presentation.
    """
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=REPORT_STEP_TEMPLATE.format(payload=rendered)),
    ]
    attempts: list[dict[str, Any]] = []
    for attempt in range(2):
        # No tools bound: the tool-free final turn. There is nothing left to fetch, and a turn that
        # could still call a tool could still change the numbers it is meant to be reporting.
        text_value = _message_text(model.invoke(messages))
        errors = validate_report(text_value, payload)
        attempts.append({"attempt": attempt + 1, "errors": errors})
        if not errors:
            return {"text": text_value, "source": "model", "attempts": attempts}
        if attempt == 0:
            messages.extend([
                AIMessage(content=text_value),
                HumanMessage(content=REPORT_RETRY_TEMPLATE.format(
                    errors="\n".join(f"- {error}" for error in errors)
                )),
            ])
    print(f"[attribute-kpi] report turn did not comply after a retry: {attempts[-1]['errors']}")
    return {"text": fallback, "source": "deterministic_fallback", "attempts": attempts}


# ============================================================================
# Stage nodes
# ============================================================================


def _park(state: PipelineState, node: str, rows: list[dict[str, Any]]) -> dict[str, str]:
    """Park a stage's output and return the updated artifact map.

    Rows go to `output_store` by reference and only the id travels on. The rest of the manifest is
    dropped here on purpose: this is one stage handing an id to the next, not a tool result being
    shown to a model.
    """
    manifest = output_store.store_rows(
        rows, sql_query=f"stage {_STAGE_LABELS.get(node, node)}"
    )
    match = _RESULT_ID_RE.search(manifest)
    if match is None:
        raise RuntimeError(f"output_store manifest carried no result_id: {manifest[:120]!r}")
    artifacts = dict(state.get("artifacts") or {})
    artifacts[node] = match.group(1)
    return artifacts


def _stage_node(node: str, body: Callable[..., dict[str, Any]]):
    """Wrap a stage body so a raised exception becomes a stage-named short circuit.

    An exception is caught rather than propagated so the run can still return the three required
    sections, name the stage that failed, and leave every already-parked artifact loadable. A
    traceback escaping to the caller would give the main agent an opaque tool error and would
    strand the artifacts with no id to reach them by.
    """

    def run_stage(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
        if state.get("failed_stage"):
            return {}
        try:
            update = dict(body(state, config))
        except Exception as exc:  # noqa: BLE001 - every stage failure is reportable, not fatal
            label = _STAGE_LABELS.get(node, node)
            print(f"[attribute-kpi] stage {label} failed: {type(exc).__name__}: {exc}")
            return {
                "failed_stage": label,
                "failure_detail": f"{type(exc).__name__}: {exc}",
                "log": list(state.get("log") or []) + [f"{label}: FAILED"],
            }
        update["stage"] = node
        update.setdefault("log", list(state.get("log") or []) + [f"{label_of(node)}: ok"])
        return update

    return run_stage


def label_of(node: str) -> str:
    """The human-readable label for a node key."""
    return _STAGE_LABELS.get(node, node)


def _stub_body(node: str):
    """A placeholder stage: parks one row and records that it ran.

    Parking for real even in the skeleton is what makes the artifact lifecycle testable now --
    preserved on failure, discarded on success -- rather than after the statistics land.
    """

    def body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
        rows = [{"stage": node, "status": "stub"}]
        return {"artifacts": _park(state, node, rows)}

    return body


# Implemented stages, by node key. Everything absent is still a stub, and the graph shape does not
# change as they land -- only what each node does.
def _stage_body(node: str):
    """The real body for an implemented stage, or a stub."""
    real = _STAGE_BODIES.get(node)
    return real if real is not None else _stub_body(node)


def stage_a_body(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage A node: extract, validate, detect the grain, park the rows."""
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    report = extract_dataset(
        connect=_runtime(config, "connect"),
        survey_id=request["survey_id"],
        variables=request["variables"],
    )
    if not report["rows"]:
        # Name the variable and the filter that emptied it (§18); never substitute another.
        raise RuntimeError(
            "stage A produced 0 usable rows for "
            f"{[v['question_id'] for v in request['variables']]} - "
            f"unsupported: {report['unsupported']}, missing: {report['missing_variables']}, "
            f"invalid: {len(report['invalid_values'])}"
        )
    if report["duplicate_observations"]:
        raise RuntimeError(
            f"{len(report['duplicate_observations'])} duplicate observation(s) at "
            "(enrollment, product, question, component) - the grain assumption is wrong, and "
            "averaging them would hide that"
        )
    artifacts = _park(state, "stage_a", report["rows"])
    return {
        "artifacts": artifacts,
        "dataset_id": artifacts["stage_a"],
        "decisions": {**decisions, "grain": report["grain"]},
        # Everything that did NOT become a row, carried forward for the diagnostics section.
        "distribution_report": {
            "invalid_values": report["invalid_values"],
            "unsupported": report["unsupported"],
            "missing_variables": report["missing_variables"],
            "enrollment_statuses": report["enrollment_statuses"],
        },
    }


def report_node(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Compose the final text: either the stage-named failure or the stub result.

    In the skeleton this returns fixed text. The §9.4 model turn replaces the success branch in a
    later step; the failure branch is already final, because a failure report must never depend on
    a model call that may itself be what failed.
    """
    failed = state.get("failed_stage")
    if failed:
        text = workflow_stage_failure(failed, state.get("failure_detail", "no detail recorded"))
        return {"analysis_result": {**(state.get("analysis_result") or {}),
                                   "status": "failed", "stage": failed},
                "still_missing": "the requested attribute-to-KPI analysis",
                "messages": [_ai(text)]}
    # Placeholder until the §9.4 model turn lands, but it reports what ACTUALLY ran rather than
    # claiming every stage is a stub -- a diagnostics line that overstates or understates the run
    # is the same evidence problem as a silently dropped measure.
    decisions = state.get("decisions") or {}
    report = state.get("distribution_report") or {}
    grain = decisions.get("grain") or {}
    normalization = decisions.get("normalization") or {}
    columns = report.get("columns") or []
    implemented = [label_of(node) for node in _STAGE_SEQUENCE if node in _STAGE_BODIES]
    pending = [label_of(node) for node in _STAGE_SEQUENCE if node not in _STAGE_BODIES]

    diagnostics = [f"pipeline={PIPELINE_CONTRACT}"]
    if grain:
        diagnostics.append(
            f"grain={grain.get('regime')} "
            f"(max products per enrollment {grain.get('max_products_per_enrollment')}), "
            f"{grain.get('rows')} rows, {grain.get('respondents')} respondents, "
            f"{grain.get('products')} products"
        )
    if normalization:
        diagnostics.append(
            f"normalization={normalization.get('applied')} "
            f"(sd_ddof={normalization.get('sd_ddof')}, "
            f"near-constant floor sd<{normalization.get('near_constant_sd_floor')} "
            f"or modal share>={normalization.get('near_constant_modal_share_floor')})"
        )
        # Stated explicitly, because it is not implied by `applied`: z-scores are computed and
        # reported, and clustering still runs on the design-range matrix.
        if normalization.get("clustering_domain"):
            diagnostics.append(
                f"clustering domain={normalization['clustering_domain']} "
                "(z-scores computed and reported per attribute, not used for the distance metric)"
            )
    if columns:
        flagged = [c["label"] for c in columns if c.get("near_constant")]
        diagnostics.append(
            f"{len(columns)} attribute column(s); near-constant: "
            + (", ".join(flagged) if flagged else "none")
        )
    # Encodings carry the labels they rest on into the output, not just into state (§8.1). A
    # decision whose evidence is not visible cannot be checked, and this one sets the sign of
    # every later figure.
    for question_id, decision in (decisions.get("encodings") or {}).items():
        parts = [f"{question_id} -> {decision.get('encoding')}"]
        if decision.get("direction"):
            parts.append(f"direction: {decision['direction']}")
        if decision.get("ordered_categories"):
            parts.append("sequence: " + " < ".join(decision["ordered_categories"]))
        if decision.get("jar_ideal") is not None:
            parts.append(f"jar ideal: {decision['jar_ideal']}")
        if decision.get("labels_read"):
            parts.append("labels read: " + ", ".join(str(x) for x in decision["labels_read"]))
        if (decision.get("source") or {}).get("encoding") == "supplied":
            parts.append(f"CALLER-SUPPLIED (workflow read {decision.get('derived_encoding')})")
        diagnostics.append("encoding " + "; ".join(parts))
    # Every exclusion with its respondent count. An exclusion that does not state its size is
    # indistinguishable from a category that was never there.
    for entry in report.get("excluded_categories") or []:
        diagnostics.append(
            f"excluded {entry['label']!r} from {entry['question_id']} (n={entry['n']}): "
            f"{entry['reason']}"
        )
    for name, entries in (
        ("invalid values", report.get("invalid_values")),
        ("unsupported variables", report.get("unsupported")),
        ("missing variables", report.get("missing_variables")),
    ):
        if entries:
            diagnostics.append(f"{len(entries)} {name} reported")
    # The persona structure: K and why, the regions with their achieved mass, the full overlap
    # table, and everything the assignment could not resolve cleanly.
    level = report.get("level_vs_shape") or {}
    if level:
        diagnostics.append(
            f"level_vs_shape={level.get('verdict')} "
            f"(PC1 variance share {level.get('pc1_variance_share')})"
        )
    if decisions.get("k"):
        request = decisions.get("request") or {}
        diagnostics.append(
            f"K={decisions['k']} (seed {request.get('random_state')}, "
            f"{decisions.get('sections_source', 'derived')}): {decisions.get('k_rationale', '')}"
        )
    clusters = (report.get("clusters") or {}).get("clusters") or []
    if clusters:
        diagnostics.append("cluster sizes: " + ", ".join(
            f"c{c['cluster']} n={c['n']} dispersion={c['dispersion']}" for c in clusters
        ))
    sections = state.get("persona_sections") or {}
    for entry in sections.get("report") or []:
        line = (
            f"region {entry['persona']}: mass {entry['mass']} (n={entry['n']}), "
            f"bounded on {', '.join(entry['bounded_attributes'])}"
        )
        if entry.get("empty"):
            line += " -- EMPTY, no respondent satisfies it"
        diagnostics.append(line)
        for divergence in entry.get("divergences") or []:
            diagnostics.append(f"  region {entry['persona']} divergence: {divergence}")
    if sections.get("distinct_regions") is not None:
        diagnostics.append(
            f"distinct regions={sections['distinct_regions']}, "
            f"coverage={sections.get('coverage')}"
        )
    for pair in sections.get("pairwise_overlap") or []:
        diagnostics.append(
            f"regions {pair['personas'][0]} and {pair['personas'][1]} overlap on "
            f"{pair['n_in_both']} respondent(s): {pair['note']}"
        )
    for row in sections.get("overlap_matrix") or []:
        diagnostics.append(
            f"cluster {row['cluster']} (n={row['n']}) overlaps " + ", ".join(
                f"{persona} {value}" for persona, value in row["overlaps"].items()
            ) + f" -> {row['assigned']}"
        )
    for entry in sections.get("unassigned") or []:
        diagnostics.append(
            f"cluster {entry['cluster']} is UNASSIGNED (best overlap "
            f"{entry['best_overlap']}): {entry['reason']}"
        )
    for entry in sections.get("many_to_one") or []:
        diagnostics.append(
            f"MANY-TO-ONE: persona {entry['persona']} holds clusters {entry['clusters']} "
            f"(n={entry['n']}). {entry['consequence']}"
        )
    for persona in sections.get("personas_matched_by_no_cluster") or []:
        diagnostics.append(f"region {persona} was matched by no cluster")
    for centroid in sections.get("centroids") or []:
        diagnostics.append(
            f"persona {centroid['persona']}: n={centroid['n']} share={centroid['share']} "
            f"dispersion={centroid['dispersion']} clusters={centroid['clusters']}"
        )

    # The KPI fits. Q² leads, R² follows as a diagnostic with n, grain and component count
    # attached, and every centroid figure is labelled descriptive.
    results = []
    for fit in (state.get("analysis_result") or {}).get("targets") or []:
        if not fit.get("fitted"):
            results.append(
                f"{fit['kpi']}: not fitted ({fit['encoding']}) -- {fit.get('reason')}"
            )
            continue
        centroid = fit["centroid_grain"]
        line = [
            f"{fit['kpi']} ({fit['encoding']}, direction: {fit.get('direction')})",
            f"  primary measure Q2={centroid['q2']} at the {centroid['grain']} grain "
            f"(n={centroid['n']}, {centroid['n_components']} component(s))",
            f"  R2={centroid['r2']} at the same grain -- {centroid['r2_is']}; "
            f"{centroid['descriptive_note']}",
            "  R2 by component count: " + ", ".join(
                f"{entry['n_components']}->{entry['r2']}"
                for entry in centroid.get("r2_by_components") or []
            ),
        ]
        grid = fit.get("cluster_product_grain")
        if grid:
            line.append(
                f"  {grid['grain']} grain: n={grid['n']}, R2={grid['r2']} at "
                f"{grid['n_components']} component(s), Q2={grid['q2']} -- {grid['r2_is']}"
            )
        individual = fit.get("individual_level") or {}
        if individual.get("fitted"):
            line.append(
                f"  individual-level validation: {individual['measure']}="
                f"{individual['value']} ({individual['model']}, n={individual['n']})"
            )
        else:
            line.append(f"  individual-level validation not fitted: {individual.get('reason')}")
        for persona in fit["per_persona"]:
            entry = (
                f"  persona {persona['persona']} (cluster {persona['cluster']}, "
                f"n={persona['n']}): t1={persona['t1']}, {persona['alignment']} -- "
                f"{persona['in_words']}, mean KPI {persona['mean_kpi_normalized']}"
            )
            if persona.get("withheld"):
                entry += f" [ALIGNMENT WITHHELD: {persona['withheld']}]"
            line.append(entry)
        ranked = sorted(
            fit["correlations"], key=lambda item: -(item["vip"] or 0)
        )[:3]
        line.append(
            "  strongest attributes by VIP: " + ", ".join(
                f"{item['attribute']} (VIP {item['vip']}, pearson {item['pearson']}, "
                f"n={item['n']})" for item in ranked
            ) + " -- exploratory, not causal"
        )
        for label, count in (fit.get("excluded") or {}).items():
            line.append(f"  excluded {label!r} from the fit: n={count}")
        results.extend(line)

    diagnostics.append(f"stages implemented: {', '.join(implemented) or 'none'}")
    diagnostics.append(f"stages still placeholders: {', '.join(pending) or 'none'}")

    result_block = (
        "\n".join(results) if results
        else f"partial - this build implements {len(implemented)} of {len(_STAGE_SEQUENCE)} stages."
    )
    missing = (
        "nothing" if not pending else
        "the stages listed as placeholders above have not run: " + ", ".join(pending)
    )
    deterministic = (
        f"RESULT: {result_block}\n"
        "DIAGNOSTICS: " + "; ".join(diagnostics) + "\n"
        f"STILL MISSING: {missing}"
    )

    # The §9.4 turn composes the report from the stage outputs. It is given the structured results
    # of I, J, K, L, M and N -- never the rows -- and every number it writes is checked against
    # them. If it cannot comply after one retry the deterministic block above stands, because that
    # block is already a correct report assembled from the same outputs and discarding a complete
    # analysis over its presentation would be the worse failure.
    text = deterministic
    report_source = "deterministic"
    report_attempts: list[dict[str, Any]] = []
    payload = {
        "clusters": (report.get("clusters") or {}).get("clusters"),
        "level_vs_shape": report.get("level_vs_shape"),
        "k": decisions.get("k"),
        "k_rationale": decisions.get("k_rationale"),
        "random_state": (decisions.get("request") or {}).get("random_state"),
        "normalization": normalization,
        "encodings": decisions.get("encodings"),
        "excluded_categories": report.get("excluded_categories"),
        "regions": sections.get("report"),
        "overlap_matrix": sections.get("overlap_matrix"),
        "unassigned": sections.get("unassigned"),
        "many_to_one": sections.get("many_to_one"),
        "centroids": sections.get("centroids"),
        "targets": (state.get("analysis_result") or {}).get("targets"),
        "persona_profiles": (state.get("analysis_result") or {}).get("persona_profiles"),
    }
    try:
        composed = compose_report(_runtime(config, "model"), payload, deterministic)
        text, report_source = composed["text"], composed["source"]
        report_attempts = composed["attempts"]
    except Exception as exc:  # noqa: BLE001 - a failed report turn must not lose the analysis
        print(f"[attribute-kpi] report turn unavailable ({type(exc).__name__}: {exc})")
        report_source = "deterministic_no_model"

    return {
        # Merged, not replaced: the stage outputs this summarises -- the KPI fits and the persona
        # profiles -- live in this same field, and overwriting it would drop the profile the caller
        # needs for durable state.
        "analysis_result": {
            **(state.get("analysis_result") or {}),
            "status": "complete" if not pending else "partial",
            "implemented": implemented,
            "report_source": report_source,
            "report_attempts": report_attempts,
        },
        "still_missing": (
            "the persona structure and KPI fits (stages not yet implemented)"
        ),
        "messages": [_ai(text)],
    }


def _ai(text: str):
    return AIMessage(content=text)


# ============================================================================
# Graph
# ============================================================================


def stage_b_body(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage B node: re-fetch the design scales, normalize, park the wide matrix."""
    connect = _runtime(config, "connect")
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    rows = output_store.load_rows(state["dataset_id"])
    if rows is None:
        raise RuntimeError(
            f"stage A's dataset {state['dataset_id']} is no longer in the result store; it was "
            "evicted before stage B could read it"
        )
    metadata = fetch_scale_metadata(
        connect, [v["question_id"] for v in request["variables"]]
    )
    result = normalize_dataset(
        rows, metadata, normalization=request.get("normalization", "design_range_zscore")
    )
    if not result["grain_keys"]:
        raise RuntimeError(
            "stage B produced 0 complete observations: "
            f"{result['excluded']['incomplete_keys']} key(s) were missing an attribute and "
            f"{result['excluded']['non_numeric_rows']} row(s) were non-numeric"
        )

    # Only the per-column report travels in state; the matrix is parked. The DESIGN-RANGE matrix
    # is the one that travels on -- see `clustering_domain` in normalize_dataset for why.
    matrix = result["design_range_matrix"]
    parked = [
        {
            "enrollment_id": key[0], "product_id": key[1] or None,
            **{
                result["columns"][index]["label"]: float(matrix[position][index])
                for index in range(len(result["columns"]))
            },
        }
        for position, key in enumerate(result["grain_keys"])
    ]
    artifacts = _park(state, "stage_b", parked)
    report = dict(state.get("distribution_report") or {})
    report.update({
        "columns": result["columns"],
        "normalization": result["normalization"],
        "excluded": result["excluded"],
    })
    return {
        "artifacts": artifacts,
        "distribution_report": report,
        "decisions": {
            **decisions,
            "grain_keys": [list(key) for key in result["grain_keys"]],
            "normalization": {
                **result["normalization"],
                "clustering_domain": result["clustering_domain"],
            },
        },
    }


def step_encoding_body(
    state: PipelineState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """The §9.2 turn: decide each variable's encoding, sequence, exclusions and direction.

    Runs between stage A and stage B because normalization needs to know what each value MEANS --
    which end is "more", which categories are not scale points -- before it scales anything.
    """
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    rows = output_store.load_rows(state["dataset_id"])
    if rows is None:
        raise RuntimeError(
            f"stage A's dataset {state['dataset_id']} is no longer in the result store; it was "
            "evicted before the encoding step could read it"
        )
    inputs = build_encoding_inputs(
        _runtime(config, "connect"), request["variables"], rows
    )
    resolved = resolve_encodings(_runtime(config, "model"), inputs)
    excluded = excluded_category_counts(resolved["encodings"], rows)

    report = dict(state.get("distribution_report") or {})
    report["excluded_categories"] = excluded
    report["encoding_attempts"] = resolved["attempts"]
    return {
        "decisions": {**decisions, "encodings": resolved["encodings"]},
        "variable_specs": resolved["encodings"],
        "distribution_report": report,
    }


def _load_matrix(state: PipelineState) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Rebuild stage B's wide matrix from its parked artifact, in the recorded column order."""
    result_id = (state.get("artifacts") or {}).get("stage_b")
    rows = output_store.load_rows(result_id) if result_id else None
    if rows is None:
        raise RuntimeError(
            f"stage B's matrix {result_id} is no longer in the result store; it was evicted "
            "before this stage could read it"
        )
    columns = (state.get("distribution_report") or {}).get("columns") or []
    if not columns:
        raise RuntimeError("stage B recorded no column metadata, so the matrix cannot be rebuilt")
    labels = [column["label"] for column in columns]
    matrix = np.array(
        [[float(row[label]) for label in labels] for row in rows], dtype=float
    )
    return matrix, columns


def step_sections_body(
    state: PipelineState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """The §9.3 turn: choose K and specify the persona regions. Runs before any clustering."""
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    matrix, columns = _load_matrix(state)
    inputs = build_sections_inputs(matrix, columns)
    resolved = resolve_sections(
        _runtime(config, "model"),
        inputs,
        [column["label"] for column in columns],
        supplied_sections=request.get("persona_sections"),
        supplied_k=request.get("k"),
    )
    report = dict(state.get("distribution_report") or {})
    report["level_vs_shape"] = inputs["level_vs_shape"]
    report["sections_attempts"] = resolved["attempts"]
    return {
        "decisions": {
            **decisions,
            "k": resolved["k"],
            "k_rationale": resolved["k_rationale"],
            "section_specs": resolved["persona_sections"],
            "section_rationales": resolved["persona_rationales"],
            "sections_source": resolved["source"],
        },
        "distribution_report": report,
    }


def stage_g_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage G: resolve the quantile specifications to tie-aware value thresholds."""
    decisions = dict(state.get("decisions") or {})
    matrix, columns = _load_matrix(state)
    resolved = resolve_persona_sections(decisions["section_specs"], matrix, columns)
    return {
        "persona_sections": {
            "report": resolved["report"],
            "attribute_order": resolved["attribute_order"],
            "pairwise_overlap": resolved["pairwise_overlap"],
            "coverage": resolved["coverage"],
            "distinct_regions": resolved["distinct_regions"],
        },
        # The bounds themselves are numpy arrays, so they stay out of the message channel and
        # travel as plain lists for the later stages to rebuild.
        "decisions": {
            **decisions,
            "resolved_sections": {
                persona: [lower.tolist(), upper.tolist()]
                for persona, (lower, upper) in resolved["sections"].items()
            },
        },
    }


def stage_d_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage D: deterministic K-means at the requested seed."""
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    matrix, _columns = _load_matrix(state)
    labels = kmeans(matrix, int(decisions["k"]), random_state=int(request["random_state"]))
    artifacts = _park(state, "stage_d", [
        {"row": index, "cluster": int(label)} for index, label in enumerate(labels)
    ])
    return {"artifacts": artifacts, "decisions": {**decisions, "cluster_labels": labels.tolist()}}


def stage_f_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage F: per-cluster size, share, dispersion and centroid."""
    decisions = dict(state.get("decisions") or {})
    matrix, columns = _load_matrix(state)
    labels = np.array(decisions["cluster_labels"])
    report = dict(state.get("distribution_report") or {})
    report["clusters"] = cluster_report(matrix, labels, columns)
    return {"distribution_report": report}


def stage_i_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stages I and J: the overlap matrix and the persona assignment."""
    decisions = dict(state.get("decisions") or {})
    matrix, _columns = _load_matrix(state)
    labels = np.array(decisions["cluster_labels"])
    sections = {
        persona: (np.array(lower), np.array(upper))
        for persona, (lower, upper) in decisions["resolved_sections"].items()
    }
    analysis = overlap_analysis(matrix, labels, sections)
    return {"decisions": {**decisions, "overlap": analysis}}


def stage_j_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage J: record the assignment and everything it could not resolve cleanly."""
    decisions = dict(state.get("decisions") or {})
    analysis = decisions["overlap"]
    sections = dict(state.get("persona_sections") or {})
    sections.update({
        "overlap_matrix": analysis["overlap_matrix"],
        "assignment": analysis["assignment"],
        "unassigned": analysis["unassigned"],
        "many_to_one": analysis["many_to_one"],
        "withhold_alignment": analysis["withhold_alignment"],
        "distinct_personas": analysis["distinct_personas"],
        "personas_matched_by_no_cluster": analysis["personas_matched_by_no_cluster"],
        "min_overlap": analysis["min_overlap"],
    })
    return {"persona_sections": sections}


def stage_k_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage K: one centroid per persona, with size, share and dispersion."""
    decisions = dict(state.get("decisions") or {})
    matrix, columns = _load_matrix(state)
    labels = np.array(decisions["cluster_labels"])
    assignment = {int(k): v for k, v in decisions["overlap"]["assignment"].items()}
    sections = dict(state.get("persona_sections") or {})
    sections["centroids"] = persona_centroids(matrix, labels, assignment, columns)
    return {"persona_sections": sections}


def stage_ce_body(state: PipelineState, _config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stage C/E: record the attribute/KPI column split.

    The split itself already happened -- stage A tagged every row with its role from the caller's
    request, and stage B built its matrix from the attribute rows only. This node records what the
    split produced rather than redoing it, so the report can state the counts and a reader can see
    that the KPI columns were separated from the clustering dimensions rather than assumed.
    """
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    columns = (state.get("distribution_report") or {}).get("columns") or []
    by_role: dict[str, list[str]] = {}
    for variable in request.get("variables") or []:
        by_role.setdefault(variable.get("role", "attribute"), []).append(
            str(variable["question_id"])
        )
    report = dict(state.get("distribution_report") or {})
    report["column_split"] = {
        "attribute_columns": [column["label"] for column in columns],
        "variables_by_role": by_role,
        "note": (
            "attributes are the clustering dimensions; KPIs are fitted against the persona "
            "structure and never enter the distance metric"
        ),
    }
    return {"distribution_report": report}


def stage_mn_body(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stages M and N: fit statistics and correlations.

    Produced by the same PLSR pass as stage L, because R², Q², the per-persona scores and the
    per-attribute correlations all come out of one decomposition -- refitting to separate them
    would risk two slightly different models behind one report. This node checks that pass
    happened and records it, rather than implying separate work.
    """
    targets = (state.get("analysis_result") or {}).get("targets")
    if targets is None:
        raise RuntimeError("stage L produced no fit for M/N to report")
    fitted = [target for target in targets if target.get("fitted")]

    # Profiling runs here rather than in its own node because it needs the KPI summaries the fit
    # produced, and because a profile is the workflow's OUTPUT -- the thing that has to survive
    # into durable state so a later question is answerable without re-running any of this.
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    matrix, columns = _load_matrix(state)
    labels = np.array(decisions["cluster_labels"])
    keys = [tuple(key) for key in decisions["grain_keys"]]
    sections = state.get("persona_sections") or {}
    profile = profile_personas(
        matrix, labels, keys, columns,
        assignment={int(k): v for k, v in (decisions.get("overlap") or {}).get(
            "assignment", {}).items()},
        sections_report=sections.get("report") or [],
        overlap_rows=sections.get("overlap_matrix") or [],
        kpi_fits=targets,
        demographics=fetch_demographics(_runtime(config, "connect"), request["survey_id"]),
    )
    return {
        "analysis_result": {
            **(state.get("analysis_result") or {}),
            "mn": {
                "kpis_fitted": len(fitted),
                "kpis_not_fitted": len(targets) - len(fitted),
                "produced_by": "the stage L PLSR pass; R2, Q2, scores and correlations share one "
                               "decomposition",
            },
            "persona_profiles": profile,
        }
    }


def stage_l_body(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Stages L and M/N: fit every KPI against the persona structure, at both grains."""
    decisions = dict(state.get("decisions") or {})
    request = decisions.get("request") or {}
    matrix, columns = _load_matrix(state)
    labels = np.array(decisions["cluster_labels"])
    keys = [tuple(key) for key in decisions["grain_keys"]]
    rows = output_store.load_rows(state["dataset_id"])
    if rows is None:
        raise RuntimeError(
            f"stage A's dataset {state['dataset_id']} is no longer in the result store; it was "
            "evicted before the fit could read it"
        )
    encodings = decisions.get("encodings") or {}
    sections = state.get("persona_sections") or {}
    assignment = {int(k): v for k, v in (decisions.get("overlap") or {}).get(
        "assignment", {}).items()}
    metadata = fetch_scale_metadata(
        _runtime(config, "connect"), [v["question_id"] for v in request["variables"]]
    )

    fits = []
    for variable in request["variables"]:
        if variable.get("role") != "kpi":
            continue
        question_id = str(variable["question_id"])
        encoding = encodings.get(question_id) or {}
        values = kpi_values(
            rows, keys, question_id, encoding, metadata.get(question_id)
        )
        fits.append(fit_persona_kpi(
            matrix, labels, keys, columns, values,
            measure_name=(metadata.get(question_id) or {}).get("label") or question_id,
            encoding=encoding,
            withhold=sections.get("withhold_alignment") or (),
            assignment=assignment,
        ))
    return {"analysis_result": {**(state.get("analysis_result") or {}), "targets": fits}}


_STAGE_BODIES: dict[str, Callable[..., dict[str, Any]]] = {
    "stage_a": stage_a_body,
    "step_encoding": step_encoding_body,
    "stage_b": stage_b_body,
    "step_sections": step_sections_body,
    "stage_g": stage_g_body,
    "stage_d": stage_d_body,
    "stage_f": stage_f_body,
    "stage_i": stage_i_body,
    "stage_j": stage_j_body,
    "stage_k": stage_k_body,
    "stage_ce": stage_ce_body,
    "stage_l": stage_l_body,
    "stage_mn": stage_mn_body,
}


def _route_after(node: str):
    """Route to the next stage, or short-circuit to the report node on a stage failure."""
    index = _STAGE_SEQUENCE.index(node)
    following = (
        _STAGE_SEQUENCE[index + 1] if index + 1 < len(_STAGE_SEQUENCE) else REPORT_NODE
    )

    def route(state: PipelineState) -> str:
        return REPORT_NODE if state.get("failed_stage") else following

    return route


def build_pipeline_graph():
    """Compile the fixed-order stage graph.

    No checkpointer, ever: a checkpointer would give this workflow durable state that outlives one
    invocation, which is precisely the isolation the design forbids. Every run is a fresh
    invocation (§8.2, isolation mechanism two).
    """
    graph = StateGraph(PipelineState)
    for node in _STAGE_SEQUENCE:
        graph.add_node(node, _stage_node(node, _stage_body(node)))
    graph.add_node(REPORT_NODE, report_node)

    graph.set_entry_point(_STAGE_SEQUENCE[0])
    for node in _STAGE_SEQUENCE:
        index = _STAGE_SEQUENCE.index(node)
        following = (
            _STAGE_SEQUENCE[index + 1] if index + 1 < len(_STAGE_SEQUENCE) else REPORT_NODE
        )
        graph.add_conditional_edges(
            node, _route_after(node), {following: following, REPORT_NODE: REPORT_NODE}
        )
    graph.add_edge(REPORT_NODE, END)
    return graph.compile()


PIPELINE_GRAPH = build_pipeline_graph()


# ============================================================================
# Run entry point
# ============================================================================


def run_attribute_kpi_analysis(
    survey_id: str,
    attributes: Sequence[dict[str, Any]],
    kpis: Sequence[dict[str, Any]],
    screeners: Sequence[dict[str, Any]] | None = None,
    k: int | None = None,
    normalization: str = "design_range_zscore",
    random_state: int = 20260819,
    requested_metrics: Sequence[str] | None = None,
    persona_sections: dict[str, Any] | None = None,
    model_name: str | None = None,
    connect: Callable[[], Any] | None = None,
    model: Any = None,
) -> WorkflowRun:
    """Run one isolated analysis and return only what may cross back.

    `model`, `model_name` and `connect` are passed in rather than imported so this module keeps no
    import edge to the agent (see the module docstring). `model` is the language model the three
    judgment turns run on -- constructed by the caller, which owns the API credentials, and
    injected so a test can substitute a scripted one without touching the network. `connect` is a zero-argument callable returning
    a context-manager database connection; the stages use it to re-fetch catalog rows by qid and
    to extract responses, which is what keeps a budget-trimmed label list recoverable instead of
    trusting whatever the main agent happened to hold.

    Artifacts are discarded on success and preserved on failure. That asymmetry is deliberate:
    on success the report holds everything anyone needs and the parked rows are dead weight,
    while on failure they are the only record of how far the run got.
    """
    seed = build_seed(
        survey_id=survey_id,
        attributes=attributes,
        kpis=kpis,
        screeners=screeners,
        k=k,
        normalization=normalization,
        random_state=random_state,
        requested_metrics=requested_metrics,
        persona_sections=persona_sections,
    )
    final = PIPELINE_GRAPH.invoke(
        {
            "messages": [seed],
            "steps": 0,
            "stage": "S0",
            # The request itself, so a stage reads its inputs from state rather than from a
            # closure -- which keeps every stage runnable in isolation for a test.
            "decisions": {"request": {
                "survey_id": survey_id,
                "normalization": normalization,
                "random_state": random_state,
                "k": k,
                "persona_sections": persona_sections,
                "variables": [
                    *({**dict(v), "role": dict(v).get("role", "attribute")} for v in attributes),
                    *({**dict(v), "role": dict(v).get("role", "kpi")} for v in kpis),
                    *({**dict(v), "role": "screener"} for v in (screeners or [])),
                ],
            }},
            "artifacts": {},
            "log": [],
        },
        config={
            "recursion_limit": WORKFLOW_RECURSION_LIMIT,
            "configurable": {
                "connect": connect, "model_name": model_name, "model": model,
            },
        },
    )

    artifacts = dict(final.get("artifacts") or {})
    failed_stage = final.get("failed_stage", "")
    text = _last_text(final.get("messages") or [])
    if not failed_stage:
        for result_id in artifacts.values():
            output_store.discard_result(result_id)
        artifacts = {}
    return WorkflowRun(
        text=text,
        artifacts=artifacts,
        still_missing=final.get("still_missing", ""),
        failed_stage=failed_stage,
        persona_profiles=(final.get("analysis_result") or {}).get("persona_profiles"),
    )


def _last_text(messages: Sequence[BaseMessage]) -> str:
    """The last non-empty message text -- the only value that may reach the main conversation."""
    for message in reversed(list(messages)):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            joined = "\n".join(p for p in parts if p).strip()
            if joined:
                return joined
    return ""


__all__ = [
    'PIPELINE_CONTRACT',
    'PIPELINE_GRAPH',
    'PipelineState',
    'REPORT_NODE',
    'WORKFLOW_MAX_STEPS',
    'WORKFLOW_RECURSION_LIMIT',
    'WorkflowRun',
    'build_pipeline_graph',
    'build_seed',
    'label_of',
    'run_attribute_kpi_analysis',
]
