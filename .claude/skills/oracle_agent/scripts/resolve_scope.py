#!/usr/bin/env python3
"""Resolve a client / organization / survey given as UUIDs *or* names.

The oracle skill is invoked with human-typed scope ("client=Frito-Lay,
survey=Chips Test 2025"), so every run starts by turning that into the exact
tenancy triple and verifying the chain survey -> organization -> account ->
client actually holds in the live database.

    resolve_scope.py --client "Frito-Lay" --survey "chips"
    resolve_scope.py --survey 4ec4b648-99bd-4d72-89ee-9cf1b7626e4c
    resolve_scope.py --benchmark
    resolve_scope.py --list-clients

Output is JSON on stdout:
    {"status": "resolved",  "scope": {...}}
    {"status": "ambiguous", "candidates": [...]}
    {"status": "not_found", "suggestions": {...}}

Exit code is 0 for "resolved" and 2 otherwise, so a caller can branch on it.
Stdlib only; the database is reached with `docker exec psql`, so no host
Postgres client or Python package is required.

This queries gpi-db - the same live database the agent under test uses, not
a private copy - so every query here runs read-only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))
import trace_log  # noqa: E402

CONTAINER = os.getenv("GPI_DB_CONTAINER", "gpi-db")
DB_USER = os.getenv("GPI_DB_USER", "gpi")
DB_NAME = os.getenv("GPI_DB_NAME", "gpi_sample_db")

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def lit(value: str) -> str:
    """Quote a value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def run_sql(sql: str) -> list[dict]:
    proc = subprocess.run(
        [
            "docker", "exec",
            # Read-only via PGOPTIONS, not a leading `SET`: a SET command tag
            # would be printed ahead of the result and parsed as the CSV header.
            # Enforced here (not just in query.sh) because this hits the same
            # live, shared gpi-db - never a disposable copy.
            "-e", "PGOPTIONS=-c default_transaction_read_only=on",
            "-i", CONTAINER,
            "psql", "-U", DB_USER, "-d", DB_NAME,
            "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "--csv",
            "-c", sql,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(
            json.dumps(
                {
                    "status": "db_error",
                    "error": (proc.stderr or proc.stdout).strip()[:2000],
                    "hint": "Run scripts/db_up.sh first (checks container %s)." % CONTAINER,
                }
            )
        )
        sys.exit(2)
    return list(csv.DictReader(io.StringIO(proc.stdout)))


# One row per survey, carrying its whole tenancy chain plus the volume facts
# that decide between same-named duplicates (drafts / "Copy - " re-runs).
BASE_SELECT = """
SELECT s.id                              AS survey_id,
       s.title                           AS survey_title,
       s."internalName"                  AS survey_internal_name,
       s.state                           AS survey_state,
       s.type                            AS survey_type,
       o.id                              AS organization_id,
       o.name                            AS organization_name,
       a.id                              AS account_id,
       a.name                            AS account_name,
       c.id                              AS client_id,
       c.name                            AS client_name,
       c.is_benchmark_source             AS is_benchmark_source,
       (SELECT count(*) FROM public.enrollment e WHERE e.survey_id = s.id)      AS enrollments,
       (SELECT count(*) FROM public.answer  ans
          JOIN public.enrollment e2 ON e2.id = ans.enrollment_id
         WHERE e2.survey_id = s.id)                                             AS answers,
       (SELECT count(*) FROM public.question q WHERE q."surveyId" = s.id)       AS questions,
       (SELECT count(*) FROM public.product p WHERE p."surveyId" = s.id)        AS products
FROM public.survey       AS s
JOIN public.organization AS o ON o.id = s.organization_id
JOIN public.account      AS a ON a.id = o.account_id
JOIN public.client       AS c ON c.id = a.client_id
"""

INT_FIELDS = ("enrollments", "answers", "questions", "products")


def match_clause(term: str, id_col: str, name_cols: tuple[str, ...]) -> str:
    """Match a term as an exact id when it looks like a UUID, else by name."""
    if UUID_RE.match(term):
        return f"{id_col} = {lit(term)}"
    like = " OR ".join(f"{col} ILIKE {lit('%' + term + '%')}" for col in name_cols)
    return f"({like})"


def find_surveys(client=None, organization=None, survey=None, benchmark=False) -> list[dict]:
    where = []
    if benchmark:
        where.append("c.is_benchmark_source IS TRUE")
    if client:
        where.append(match_clause(client, "c.id", ("c.name", "a.name")))
    if organization:
        where.append(match_clause(organization, "o.id", ("o.name",)))
    if survey:
        where.append(match_clause(survey, "s.id", ('s.title', 's."internalName"')))
    sql = BASE_SELECT
    if where:
        sql += "WHERE " + "\n  AND ".join(where) + "\n"
    # Prefer the candidate that actually holds response data - same-titled
    # drafts and copies are common and the empty one is rarely the one meant.
    sql += "ORDER BY answers DESC, enrollments DESC, s.title\nLIMIT 50;"
    rows = run_sql(sql)
    for row in rows:
        for field in INT_FIELDS:
            row[field] = int(row[field] or 0)
        row["is_benchmark_source"] = row.get("is_benchmark_source") == "t"
    return rows


def suggest(term: str | None, kind: str) -> list[dict]:
    """Loose per-token lookup used when a strict match found nothing."""
    if not term:
        return []
    tokens = [t for t in re.split(r"[^\w]+", term) if len(t) > 2][:4]
    if not tokens:
        return []
    ors = lambda cols: " OR ".join(  # noqa: E731
        f"{col} ILIKE {lit('%' + tok + '%')}" for tok in tokens for col in cols
    )
    if kind == "client":
        sql = f"SELECT id, name FROM public.client WHERE {ors(['name'])} LIMIT 15;"
    elif kind == "organization":
        sql = f"SELECT id, name FROM public.organization WHERE {ors(['name'])} LIMIT 15;"
    else:
        sql = (
            'SELECT id, title, "internalName" FROM public.survey '
            f'WHERE {ors(["title", chr(34) + "internalName" + chr(34)])} LIMIT 15;'
        )
    return run_sql(sql)


def listing(kind: str) -> list[dict]:
    if kind == "clients":
        sql = """
        SELECT c.id, c.name, c.is_benchmark_source,
               (SELECT count(*) FROM public.organization o
                  JOIN public.account a ON a.id = o.account_id
                 WHERE a.client_id = c.id) AS organizations,
               (SELECT count(*) FROM public.survey s
                  JOIN public.organization o ON o.id = s.organization_id
                  JOIN public.account a ON a.id = o.account_id
                 WHERE a.client_id = c.id) AS surveys
        FROM public.client c ORDER BY c.name;"""
    elif kind == "organizations":
        sql = """
        SELECT o.id, o.name, c.name AS client_name,
               (SELECT count(*) FROM public.survey s WHERE s.organization_id = o.id) AS surveys
        FROM public.organization o
        JOIN public.account a ON a.id = o.account_id
        JOIN public.client  c ON c.id = a.client_id
        ORDER BY c.name, o.name;"""
    else:
        sql = BASE_SELECT + "ORDER BY c.name, o.name, s.title LIMIT 200;"
    return run_sql(sql)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", help="Client UUID or name fragment.")
    ap.add_argument("--organization", help="Organization UUID or name fragment.")
    ap.add_argument("--survey", help="Survey UUID, title fragment, or internal name.")
    ap.add_argument(
        "--benchmark",
        action="store_true",
        help="Resolve the FlavorWiki benchmark survey (client.is_benchmark_source).",
    )
    ap.add_argument("--list-clients", action="store_true")
    ap.add_argument("--list-organizations", action="store_true")
    ap.add_argument("--list-surveys", action="store_true")
    ap.add_argument("--why", default="",
                    help="why this call was made (recorded in the trajectory log)")
    args = ap.parse_args()

    if args.list_clients or args.list_organizations or args.list_surveys:
        kind = (
            "clients" if args.list_clients
            else "organizations" if args.list_organizations
            else "surveys"
        )
        print(json.dumps({"status": "listing", "kind": kind, "rows": listing(kind)}, indent=2))
        return 0

    if not (args.client or args.organization or args.survey or args.benchmark):
        ap.error("give at least one of --client / --organization / --survey / --benchmark")

    rows = find_surveys(args.client, args.organization, args.survey, args.benchmark)

    if not rows:
        # A failed resolution is the decisive step of any unresolvable-scope probe, so it must
        # reach the trajectory log too — logging only the success path hid it entirely.
        trace_log.log("scope_resolution", "resolve_scope.py", purpose=args.why,
                      args={"client": args.client, "organization": args.organization,
                            "survey": args.survey},
                      outcome="error",
                      error="not_found: no survey satisfies all given terms together")
        print(
            json.dumps(
                {
                    "status": "not_found",
                    "asked": {
                        "client": args.client,
                        "organization": args.organization,
                        "survey": args.survey,
                    },
                    "suggestions": {
                        "clients": suggest(args.client, "client"),
                        "organizations": suggest(args.organization, "organization"),
                        "surveys": suggest(args.survey, "survey"),
                    },
                    "note": (
                        "No survey satisfies all given terms together. The suggestions "
                        "are per-term loose matches - confirm the intended one with the "
                        "user, or widen the search with --list-surveys."
                    ),
                },
                indent=2,
            )
        )
        return 2

    if len(rows) > 1:
        # Unambiguous in practice when exactly one candidate holds data.
        with_data = [r for r in rows if r["answers"] > 0]
        if len(with_data) == 1:
            chosen, others = with_data[0], [r for r in rows if r is not with_data[0]]
            print(
                json.dumps(
                    {
                        "status": "resolved",
                        "scope": chosen,
                        "note": (
                            "%d other survey(s) matched the same terms but have zero "
                            "answers (drafts/copies); picked the one with response data."
                            % len(others)
                        ),
                        "rejected": others,
                    },
                    indent=2,
                )
            )
            return 0
        trace_log.log("scope_resolution", "resolve_scope.py", purpose=args.why,
                      args={"client": args.client, "organization": args.organization,
                            "survey": args.survey},
                      outcome="error",
                      error=f"ambiguous: {len(rows)} surveys match the given terms")
        print(
            json.dumps(
                {
                    "status": "ambiguous",
                    "count": len(rows),
                    "candidates": rows,
                    "note": (
                        "Several surveys match. Ask the user which one, or pass a more "
                        "specific --survey / --organization. Candidates are ordered by "
                        "answer volume."
                    ),
                },
                indent=2,
            )
        )
        return 2

    trace_log.log("scope_resolution", "resolve_scope.py", purpose=args.why,
                  args={"client": args.client, "organization": args.organization,
                        "survey": args.survey},
                  outcome="ok",
                  detail=f"resolved survey {rows[0].get('survey_id', '')}")
    print(json.dumps({"status": "resolved", "scope": rows[0]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
