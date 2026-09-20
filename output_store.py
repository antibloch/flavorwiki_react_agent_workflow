"""In-process storage for oversized SQL results, with explicit discard after drilling.

Same job and the same public surface as `result_store.py` / `result_store_old.py`: when a
query returns more than TOOL_CHAR_LIMIT chars, `nl2sql_tool` parks the full rows here and
hands the model a compact manifest + result_id, then the drill reads them back through
describe_result / aggregate_result / query_result / slice_result. Those two files are left
untouched as the record of the Redis-backed design; this one replaces the backend.

WHY DROP REDIS
Measured, not assumed:
  - The container had been down for 9 hours and every drill in that period logged
    `backend=memory` and returned exact figures. The fallback path was already the real
    path, and nothing observed it.
  - Store+load of a 400-row / 64,856-char result: Redis 0.85 ms, in-process JSON string
    0.56 ms, in-process objects 0.00 ms. All three are noise against an 8-25s LLM turn, so
    speed was never the argument in either direction.
  - Both agents are single-process CLI runs: the rows are parked and read back inside one
    process, in one run. A network hop to fetch data that never left memory buys nothing.

WHY OBJECTS, NOT A JSON STRING
`result_store.py` stored `json.dumps(rows, default=str)`, so Decimal/date/UUID came back as
strings -- which is why it had to capture the true column types at store time and re-parse
numerics on every read (`_as_float`). Its own comment says so. Holding the row dicts means
types survive: a NUMERIC column is still Decimal when `aggregate_result` averages it, and
serialization happens once, at the edge, when a tool renders JSON for the model.

DISCARDING THE DUMP
The point of parking is that the raw rows never enter the conversation; once the drill has
extracted what the question needs, the dump is dead weight held only by this module. Call
`discard_result(result_id)` when the drill returns -- in `call_tool`, right after
`run_drill(...)`. Note the consequence: the manifest that travels back to the analyst
advertises the result_id for further drill-down, so a discarded id must not be advertised.
Trim that tail when wiring the discard, or the model will call a tool that now answers
"No stored result".

`MAX_STORED_RESULTS` bounds memory the way Redis's TTL used to: the oldest parked result is
evicted when a new one arrives over the cap. For a one-shot CLI run nothing is ever evicted;
it matters only if this module is ever imported into a long-lived process.
"""

import json
import os
import uuid
from collections import Counter, OrderedDict
from itertools import combinations
from typing import Any

from langchain_core.tools import tool

SAMPLE_ROWS = 2       # rows shown inline in the manifest
MAX_RETURN_ROWS = 50  # cap on rows any single drill-down returns
MAX_GROUP_ROWS = 200  # cap on groups any single aggregate_result call returns
# Bounded so a long-lived process cannot accumulate dumps forever. Insertion-ordered, so the
# oldest goes first -- there is no access-time bookkeeping to get wrong, and a drill reads one
# result repeatedly within a single turn anyway.
MAX_STORED_RESULTS = int(os.getenv("MAX_STORED_RESULTS", "32"))

# Types whose values are genuinely numeric, so min/max/mean are meaningful. Captured at store
# time from the live Python objects. Unlike the Redis-backed version this is no longer a
# workaround for serialization -- the values stay Decimal/int/float -- but it is still the gate
# that stops an identifier-ish string like "554" from being averaged.
NUMERIC_TYPES = {"int", "float", "Decimal"}

# result_id -> {"columns": {col: typename}, "rows": [dict, ...], "sql": str}
_STORE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _describe_columns(rows: list[dict]) -> dict[str, str]:
    """Infer a column -> type map from the first non-null value of each column.

    Runs on the raw DB rows, where Decimal/UUID/date are still their real Python types.
    """
    types: dict[str, str] = {}
    for row in rows:
        for key, value in row.items():
            if key not in types and value is not None:
                types[key] = type(value).__name__
    for row in rows[:1]:
        for key in row:
            types.setdefault(key, "null")
    return types


def _load(result_id: str) -> tuple[dict[str, str], list[dict]] | tuple[None, None]:
    """Return (column types, rows) for a stored result, or (None, None) if absent."""
    entry = _STORE.get(result_id)
    if entry is None:
        return None, None
    return entry["columns"], entry["rows"]


def store_rows(rows: list[dict], sql_query: str = "") -> str:
    """Park `rows` in memory and return the manifest string to hand back to the model.

    The rows are stored by reference -- no copy, no serialization. The caller has already
    finished with the list (it built it from the cursor and is handing it over), so sharing
    the object is safe and free.
    """
    result_id = uuid.uuid4().hex[:12]
    _STORE[result_id] = {
        "columns": _describe_columns(rows),
        "rows": rows,
        "sql": sql_query,
    }
    while len(_STORE) > max(MAX_STORED_RESULTS, 1):
        _STORE.popitem(last=False)  # oldest out first

    columns = _STORE[result_id]["columns"]
    sample = json.dumps(rows[:SAMPLE_ROWS], default=str, ensure_ascii=False)
    # "RESULT STORED" and "result_id=" are load-bearing: both agents detect a parked result by
    # matching those in the tool output (_parked_result_id). Keep the wording.
    return (
        f"RESULT STORED (too large to inline). result_id={result_id} "
        f"rows={len(rows)} backend=memory\n"
        f"columns={json.dumps(columns)}\n"
        f"first_{min(SAMPLE_ROWS, len(rows))}_rows={sample}\n"
        "The full rows are NOT in this message. To use them, call describe_result, "
        "query_result, or slice_result with this result_id. Prefer re-running "
        "nl2sql_tool with GROUP BY / aggregates if you need survey-wide numbers -- "
        "that is cheaper than paging through rows."
    )


def load_rows(result_id: str) -> list[dict] | None:
    """Return the stored rows for `result_id`, or None if absent or discarded."""
    _types, rows = _load(result_id)
    return rows


def discard_result(result_id: str) -> bool:
    """Drop a parked result. True if something was actually there.

    This is the half the Redis version got for free from TTL and never did explicitly. Call it
    once the drill has taken what it needs: the refined findings are in the conversation, and
    the dump behind them is unreachable from the answer, so keeping it only holds memory and
    keeps a stale result_id callable.
    """
    return _STORE.pop(result_id, None) is not None


def discard_all() -> int:
    """Drop every parked result and return how many there were. For a fresh run."""
    count = len(_STORE)
    _STORE.clear()
    return count


def store_stats() -> dict[str, Any]:
    """What is currently parked -- for a log line, not for the model."""
    return {
        "stored_results": len(_STORE),
        "total_rows": sum(len(e["rows"]) for e in _STORE.values()),
        "result_ids": list(_STORE),
    }


# ---- digest: exact statistics over a parked result -------------------------------
# Fuel for the drill. An LLM handed 600+ raw rows does not compute means, it transcribes
# value-frequency tables and miscounts the row total (measured: 6/39 group means correct, row
# count off by 66). So every number is computed here, in Python, and the model's job shrinks to
# choosing which of them answer the question.
DIGEST_KEY_MAX_DISTINCT = 25  # more distinct values than this and it is not a group key
DIGEST_MAX_GROUPS = 120       # cap on lines in one cross-tab
DIGEST_MAX_KEY_DEPTH = 3      # widest key combination cross-tabbed
DIGEST_MAX_GROUPINGS = 12     # cap on how many cross-tabs are built
DIGEST_TEXT_RESERVE = 300     # margin kept free when spending the rest on text
DIGEST_VALUE_CHARS = 200      # per-value truncation in those samples
DIGEST_CHAR_CAP = 16000       # hard bound on the digest handed to the model


def _as_float(value: Any) -> float | None:
    """Coerce one stored value to float, or None if it is not numeric.

    Kept even though values are no longer strings: Decimal needs the cast anyway, and a
    numeric column can still hold a None or an odd value from a UNION or a CASE branch.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_key(value: Any) -> tuple:
    """Sort key that cannot raise on a mixed-type column.

    The Redis version sorted values that JSON had already flattened to strings, so comparison
    was always defined. Real objects can mix Decimal and str in one column (UNION, CASE, a
    NULL-heavy join), and Python raises on that. Nulls last, then numerics before strings,
    then the value itself.
    """
    if value is None:
        return (2, 0.0, "")
    number = _as_float(value)
    if number is not None and not isinstance(value, (str, bytes)):
        return (0, number, "")
    return (1, 0.0, str(value))


def _stats(rows: list[dict], col: str) -> str | None:
    nums = [x for x in (_as_float(r.get(col)) for r in rows) if x is not None]
    if not nums:
        return None
    return (
        f"n={len(nums)} min={min(nums)} max={max(nums)} "
        f"mean={round(sum(nums) / len(nums), 4)}"
    )


def digest_result(result_id: str) -> str | None:
    """Return an exact statistical digest of a parked result, or None if it is gone.

    Reports the row count, per-column stats, and -- the part that matters -- counts and means
    grouped by every low-cardinality column and by all of them combined. Bounded by
    DIGEST_MAX_GROUPS / DIGEST_CHAR_CAP regardless of how large the result is.
    """
    types, rows = _load(result_id)
    if not rows:
        return None

    numeric_cols = [c for c, kind in types.items() if kind in NUMERIC_TYPES]
    key_cols, distinct = [], {}
    for col, _kind in types.items():
        if col in numeric_cols:
            continue
        distinct[col] = {str(r.get(col)) for r in rows if r.get(col) is not None}
        if len(distinct[col]) <= DIGEST_KEY_MAX_DISTINCT:
            key_cols.append(col)

    unkeyed = [c for c in types if c not in numeric_cols and c not in key_cols]
    text_cols: list[str] = []  # high-cardinality, non-identifier: values listed at the end

    out = ["EXACT DIGEST (computed in Python over all rows; every figure below is exact)",
           f"rows={len(rows)}", f"columns={json.dumps(types)}"]
    if unkeyed:
        out.append(
            f"NOT GROUPABLE (over {DIGEST_KEY_MAX_DISTINCT} distinct values, so no "
            f"aggregates by them are below): {', '.join(unkeyed)}. If the question needs "
            "numbers per one of these, say so -- the analyst must re-query with GROUP BY."
        )
    out += ["", "-- per column --"]

    for col, kind in types.items():
        if col in numeric_cols:
            out.append(f"{col} ({kind}): {_stats(rows, col) or 'no numeric values'}")
        elif len(distinct[col]) <= DIGEST_KEY_MAX_DISTINCT:
            counts = Counter(str(r.get(col)) for r in rows if r.get(col) is not None)
            listed = ", ".join(f"{v} (n={n})" for v, n in counts.most_common())
            out.append(f"{col} ({kind}): {len(distinct[col])} distinct -> {listed}")
        elif kind == "UUID":
            # Identifiers: the count is informative, a sample of them never is.
            out.append(f"{col} (UUID): {len(distinct[col])} distinct identifiers, not sampled")
        else:
            # Deferred to the end, where the leftover char budget is known.
            text_cols.append(col)
            out.append(f"{col} ({kind}): {len(distinct[col])} distinct values, listed below")

    # Coarse groupings first, then finer: the model usually wants the coarse numbers, and this
    # order survives truncation better. Every combination up to DIGEST_MAX_KEY_DEPTH is offered
    # (plus the full tuple) because a grouping the model needs but cannot find is the one case
    # where it starts deriving figures -- measured: averaging subgroup means to fake a
    # product x attribute_group row, one of nine of them wrong.
    groupings = []
    for size in range(1, min(len(key_cols), DIGEST_MAX_KEY_DEPTH) + 1):
        groupings.extend(list(combo) for combo in combinations(key_cols, size))
    if len(key_cols) > DIGEST_MAX_KEY_DEPTH:
        groupings.append(key_cols)

    # Never drop a grouping silently. A cap the model cannot see reads to it as "this grouping
    # does not exist", and an absent grouping is what makes it invent figures.
    dropped = groupings[DIGEST_MAX_GROUPINGS:]
    groupings = groupings[:DIGEST_MAX_GROUPINGS]
    if dropped:
        out.append(
            f"\n-- NOT COMPUTED ({len(dropped)} groupings over the "
            f"{DIGEST_MAX_GROUPINGS}-cross-tab cap): "
            + "; ".join(" x ".join(keys) for keys in dropped)
            + ". Re-query with GROUP BY for any of these -- do not derive them. --"
        )

    for keys in groupings:
        buckets: dict[tuple, list[dict]] = {}
        for row in rows:
            buckets.setdefault(tuple(str(row.get(k)) for k in keys), []).append(row)
        label = " x ".join(keys)
        if len(buckets) > DIGEST_MAX_GROUPS:
            out.append(f"\n-- by {label}: {len(buckets)} groups, over the "
                       f"{DIGEST_MAX_GROUPS}-group cap, NOT computed --")
            continue
        out.append(f"\n-- exact aggregates by {label} ({len(buckets)} groups) --")
        for key, group in sorted(buckets.items()):
            parts = [f"n={len(group)}"]
            for col in numeric_cols:
                stat = _stats(group, col)
                if stat:
                    parts.append(f"{col}[{stat}]")
            out.append(" | ".join(key) + " -> " + " ".join(parts))

    # Free text goes last, and gets whatever char budget the aggregates left unspent: a
    # text-heavy result has few useful means to report, so the cap is better spent showing
    # actual values than held in reserve.
    for index, col in enumerate(text_cols):
        left = DIGEST_CHAR_CAP - len("\n".join(out)) - DIGEST_TEXT_RESERVE
        share = max(0, left // (len(text_cols) - index))
        values, shown, used = sorted(distinct[col]), [], 0
        for value in values:
            clipped = value[:DIGEST_VALUE_CHARS]
            if used + len(clipped) + 3 > share:
                break
            shown.append(clipped)
            used += len(clipped) + 3
        omitted = len(values) - len(shown)
        out.append(
            f"\n-- {col}: {len(shown)} of {len(values)} distinct values"
            + (" (ALL of them, so this column can be characterized in full) --"
               if not omitted else
               f", {omitted} NOT shown -- describe only what is listed and say the rest "
               "were not seen; do not characterize the column as a whole --")
        )
        out.append(" | ".join(shown))

    digest = "\n".join(out)
    if len(digest) > DIGEST_CHAR_CAP:
        digest = digest[:DIGEST_CHAR_CAP] + "\n... DIGEST TRUNCATED at the char cap ..."
    return digest


def _missing(result_id: str) -> str:
    return (
        f"No stored result for result_id={result_id} (it was discarded after the refinement "
        "pass, or never existed). Re-run the query with nl2sql_tool if you need those rows."
    )


@tool
def describe_result(result_id: str) -> str:
    """Summarize a stored SQL result without returning its rows.

    Reports the row count, every column with its inferred type, and -- for numeric
    columns -- count/min/max/mean, plus the distinct-value count for low-cardinality
    columns. Use this first to decide what to pull.
    """
    types, rows = _load(result_id)
    if rows is None:
        return _missing(result_id)

    summary: dict[str, Any] = {"result_id": result_id, "rows": len(rows), "columns": {}}
    for col, kind in types.items():
        values = [r.get(col) for r in rows if r.get(col) is not None]
        info: dict[str, Any] = {"type": kind, "non_null": len(values)}

        # Only summarize numerically when the column really is numeric. Identifier-ish
        # strings ("554") coerce cleanly but a mean of them is meaningless.
        numeric = None
        if kind in NUMERIC_TYPES:
            numeric = [x for x in (_as_float(v) for v in values) if x is not None]
            if len(numeric) != len(values):
                numeric = None

        if numeric:
            info |= {
                "min": min(numeric),
                "max": max(numeric),
                "mean": round(sum(numeric) / len(numeric), 4),
            }
        else:
            distinct = {str(v) for v in values}
            info["distinct"] = len(distinct)
            if len(distinct) <= 12:
                info["values"] = sorted(distinct)
        summary["columns"][col] = info
    return json.dumps(summary, default=str, ensure_ascii=False)


def _matches(row: dict, filters: dict | None) -> bool:
    """Exact match, or case-insensitive substring when both sides are strings.

    One deliberate difference from the Redis version: there, JSON had turned every value into
    a string, so a string filter substring-matched even numeric columns. Here a Decimal stays a
    Decimal and falls to the exact `str(got) != str(want)` branch, which is what the docstring
    always promised.
    """
    for col, want in (filters or {}).items():
        got = row.get(col)
        if isinstance(want, str) and isinstance(got, str):
            if want.lower() not in got.lower():
                return False
        elif str(got) != str(want):
            return False
    return True


@tool
def aggregate_result(
    result_id: str,
    group_by: list[str] | None = None,
    metrics: list[str] | None = None,
    filters: dict | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """GROUP BY over a stored SQL result: exact counts and statistics per group.

    This is how you obtain any number. It computes in Python over every matching row, so
    its figures are exact -- unlike averaging rows you fetched yourself, which is wrong.

    group_by: columns to group on (any columns, any cardinality; omit for one total row).
    metrics: numeric columns to aggregate (default: all numeric columns). For each you
    get <col>_n, <col>_min, <col>_max, <col>_mean, <col>_sum.
    filters: {column: value} -- exact match, or case-insensitive substring for strings.
    sort_by: a group column or a metric key such as "rating_mean". limit caps returned
    groups (hard cap 200); `groups_total` is exact even when the rows are capped.
    offset: skip this many groups, so a grouping wider than the cap can be paged in full.
    The response carries `next_offset` (null when the last group has been returned) --
    keep calling with it until it is null if you need complete coverage.

    Returns e.g. {"group_by": ["product"], "groups_total": 3, "rows": [{"product":
    "Product 1", "n": 221, "rating_mean": 8.9118, ...}]}. On a bad column name it returns
    an error naming the available columns -- read it and call again.
    """
    types, rows = _load(result_id)
    if rows is None:
        return _missing(result_id)

    keys = list(group_by or [])
    unknown = [c for c in keys + list(metrics or []) if c not in types]
    if unknown:
        return f"Unknown column(s) {unknown}. Available columns: {list(types)}."

    numeric = [c for c, kind in types.items() if kind in NUMERIC_TYPES]
    wanted = list(metrics) if metrics is not None else numeric
    # Gate on the stored type: identifier-ish strings coerce to float but must never be
    # averaged, and silently doing so is exactly the kind of wrong number to avoid.
    not_numeric = [c for c in wanted if c not in numeric]
    if not_numeric:
        return (f"Columns {not_numeric} are not numeric, so min/max/mean/sum are not "
                f"meaningful. Numeric columns: {numeric}. Group by them instead to count.")

    hits = [r for r in rows if _matches(r, filters)]
    buckets: dict[tuple, list[dict]] = {}
    for row in hits:
        buckets.setdefault(tuple(str(row.get(k)) for k in keys), []).append(row)

    grouped = []
    for key, group in buckets.items():
        record: dict[str, Any] = dict(zip(keys, key, strict=False))
        record["n"] = len(group)
        for col in wanted:
            nums = [x for x in (_as_float(r.get(col)) for r in group) if x is not None]
            if nums:
                record |= {
                    f"{col}_n": len(nums),
                    f"{col}_min": min(nums),
                    f"{col}_max": max(nums),
                    f"{col}_mean": round(sum(nums) / len(nums), 4),
                    f"{col}_sum": round(sum(nums), 4),
                }
        grouped.append(record)

    if sort_by:
        grouped.sort(key=lambda r: _sort_key(r.get(sort_by)), reverse=descending)
    else:
        grouped.sort(key=lambda r: [str(r.get(k)) for k in keys])

    capped = min(max(limit, 1), MAX_GROUP_ROWS)
    start = max(offset, 0)
    page = grouped[start : start + capped]
    next_offset = start + len(page)
    return json.dumps(
        {
            "result_id": result_id,
            "group_by": keys,
            "rows_matched": len(hits),
            "groups_total": len(grouped),
            "offset": start,
            "groups_returned": len(page),
            "next_offset": next_offset if next_offset < len(grouped) else None,
            "rows": page,
        },
        default=str,
        ensure_ascii=False,
    )


@tool
def query_result(
    result_id: str,
    columns: list[str] | None = None,
    filters: dict | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = 20,
) -> str:
    """Project, filter and sort a stored SQL result, returning matching rows as JSON.

    columns: keep only these columns (default: all).
    filters: {column: value} -- exact match, or case-insensitive substring for strings.
    sort_by / descending: order the matches before applying limit.
    limit: max rows to return (hard cap 50). The reported match count is exact even
    when the returned rows are capped, so counts stay trustworthy.
    """
    _types, rows = _load(result_id)
    if rows is None:
        return _missing(result_id)

    hits = [r for r in rows if _matches(r, filters)]
    if sort_by:
        hits = sorted(hits, key=lambda r: _sort_key(r.get(sort_by)), reverse=descending)
    capped = min(max(limit, 1), MAX_RETURN_ROWS)
    out = hits[:capped]
    if columns:
        out = [{c: r.get(c) for c in columns} for r in out]

    return json.dumps(
        {"result_id": result_id, "matched": len(hits), "returned": len(out), "rows": out},
        default=str,
        ensure_ascii=False,
    )


@tool
def slice_result(result_id: str, offset: int = 0, limit: int = 20) -> str:
    """Return a contiguous page of a stored SQL result, in its original row order.

    Use for paging through rows when no filter applies. limit is capped at 50; the
    response reports total rows and the next offset so you can page deterministically.
    """
    _types, rows = _load(result_id)
    if rows is None:
        return _missing(result_id)

    start = max(offset, 0)
    capped = min(max(limit, 1), MAX_RETURN_ROWS)
    page = rows[start : start + capped]
    next_offset = start + len(page)
    return json.dumps(
        {
            "result_id": result_id,
            "total_rows": len(rows),
            "offset": start,
            "returned": len(page),
            "next_offset": next_offset if next_offset < len(rows) else None,
            "rows": page,
        },
        default=str,
        ensure_ascii=False,
    )


# The reader tools the main loop gets: no aggregation, so it cannot be tempted to average
# fetched rows -- it is told to re-run nl2sql_tool with GROUP BY instead.
RESULT_TOOLS = [describe_result, query_result, slice_result]
# The drill's toolset: the same readers plus real aggregation, which is what lets it answer
# with computed figures instead of averaging fetched rows by eye.
DRILL_TOOLS = [describe_result, aggregate_result, query_result, slice_result]
