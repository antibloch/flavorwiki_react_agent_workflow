#!/usr/bin/env python3
"""Live statistics for a survey question via the FlavorWiki Charts API.

This is the oracle's equivalent of `funda_agent_exp.py`'s `run_survey_stats`
tool: same endpoint, same secret header, same validation and ownership rules
(including the fail-closed scope check), same human-readable rendering - so the
oracle's statistical answers are directly comparable to that agent's.

    stats_api.py --question <uuid> --types anova,tukey
    stats_api.py --question <uuid> --types pearson,spearman --reference <uuid>
    stats_api.py --question <uuid> --types chi-square --alpha 0.01
    stats_api.py --question <uuid> --types anova --survey <uuid>   # ownership check
    stats_api.py --question <uuid> --types anova --raw             # untouched JSON

GET {CHARTS_API_BASE_URL}/chartingAPI/charts/public/stats/{question_id}
    ?statsTypes=&alphaValue=&referenceQuestionId=&penaltyLevel=&boxingStrategy=
    header: x-charts-stats-secret

Caveat worth knowing before judging an answer: the endpoint computes over the
*production* charts database, not the local gpi-db. A question that exists
locally can legitimately answer "No data found for the given question ID".
That is not a tool failure - fall back to scripts/sql_stats.py, which
computes the same tests from the local data.

Stdlib only (urllib), so it runs with no virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_log  # noqa: E402

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
REQUIRES_REFERENCE = frozenset({"pearson", "spearman", "penalty"})
TIMEOUT_S = 60


def load_env() -> None:
    """Read KEY=VALUE pairs from the project .env without overriding the shell."""
    env_path = Path(__file__).resolve().parents[4] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def question_belongs_to_survey(question_id: str, survey_id: str) -> bool | None:
    """True/False if checkable against gpi-db, None if the DB is unreachable."""
    container = os.getenv("GPI_DB_CONTAINER", "gpi-db")
    proc = subprocess.run(
        [
            "docker", "exec",
            # Read-only: this is the live, shared gpi-db, not a private copy.
            "-e", "PGOPTIONS=-c default_transaction_read_only=on",
            "-i", container,
            "psql", "-U", os.getenv("GPI_DB_USER", "gpi"),
            "-d", os.getenv("GPI_DB_NAME", "gpi_sample_db"),
            "-tAc",
            'SELECT 1 FROM public.question WHERE id = \'%s\' AND "surveyId" = \'%s\''
            % (question_id, survey_id),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() == "1"


def fetch(question_id: str, params: dict) -> tuple[int, dict | str]:
    base = os.getenv("CHARTS_API_BASE_URL", "https://charts-api.gpisurveys.com").rstrip("/")
    secret = os.getenv("CHARTS_STATS_EXTERNAL_ACCESS_SECRET", "")
    url = f"{base}/chartingAPI/charts/public/stats/{question_id}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-charts-stats-secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body
    except urllib.error.URLError as exc:
        return 0, f"network error: {exc.reason}"
    except TimeoutError:
        return 0, "request timed out"


def fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.5g}"
    return str(value)


def render(data: dict) -> str:
    """Human-readable rendering of the confirmed response shape."""
    out: list[str] = [
        "Question type: %s | respondents: %s | alphas: %s"
        % (
            data.get("questionType", "unknown"),
            data.get("respondentCount", "?"),
            data.get("computedAlphas", "?"),
        )
    ]
    stats = data.get("stats", {}) or {}

    # --- ANOVA: read `reject` directly, never re-derive from p ---
    for alpha, result in (stats.get("anova") or {}).items():
        if "error" in result:
            out.append(f"\nANOVA [alpha={alpha}]: not available - {result['error']}")
            continue
        for attr, r in (result.get("anovaResults") or {}).items():
            label = "" if attr == "Value" else f" ({attr})"
            out.append(
                "\nANOVA [alpha=%s]%s: F=%s, p=%s, df=(%s, %s) -> %s"
                % (
                    alpha, label, fmt(r.get("F")), fmt(r.get("pVal")),
                    r.get("df_between"), r.get("df_within"),
                    "SIGNIFICANT (reject H0: group means differ)"
                    if r.get("reject") else "not significant",
                )
            )

    # --- Tukey: letters are authoritative; shared letter => not different ---
    for alpha, result in (stats.get("tukey") or {}).items():
        if "error" in result:
            out.append(f"\nTukey [alpha={alpha}]: not available - {result['error']}")
            continue
        hsd = result.get("tukeyHSD", {}) or {}
        for attr, letters in (hsd.get("assignedLetters") or {}).items():
            label = "" if attr == "Value" else f" ({attr})"
            out.append(f"\nTukey groups [alpha={alpha}]{label}:")
            means = (hsd.get("meansByAttribute") or {}).get(attr, {}) or {}
            for product, letter in letters.items():
                out.append(f"  {product}: group {letter} (mean={fmt(means.get(product))})")
            out.append("  (products sharing a letter are NOT significantly different)")
        for attr, rows in (hsd.get("tukeyTables") or {}).items():
            label = "" if attr == "Value" else f" ({attr})"
            out.append(f"\nTukey pairwise [alpha={alpha}]{label}:")
            for row in rows:
                out.append(
                    "  %s vs %s: meandiff=%s, p-adj=%s, CI=[%s, %s] -> %s"
                    % (
                        row.get("group1"), row.get("group2"), fmt(row.get("meandiff")),
                        fmt(row.get("p-adj")), fmt(row.get("lowerCI")), fmt(row.get("upperCI")),
                        "significant" if row.get("reject") else "not significant",
                    )
                )

    # --- Correlations ---
    for test in ("pearson", "spearman"):
        for alpha, result in (stats.get(test) or {}).items():
            if "error" in result:
                out.append(f"\n{test.upper()} [alpha={alpha}]: not available - {result['error']}")
                continue
            corr = result.get("correlationAnalysis", {}) or {}
            rows = ((corr.get("table") or {}).get("dataSource") or [])
            ref_prompt = corr.get("referenceQuestionPrompt")
            out.append(
                f"\n{test.upper()} [alpha={alpha}]"
                + (f" vs reference question: \"{ref_prompt}\"" if ref_prompt else "")
            )
            if not rows:
                out.append("  " + json.dumps(result)[:800])
                continue
            for row in rows:
                coeff = row.get("coefficient")
                n_points = len(row.get("points") or []) or None
                strength = ""
                if isinstance(coeff, (int, float)):
                    a = abs(coeff)
                    strength = " (strong)" if a > 0.7 else " (moderate)" if a > 0.5 else " (weak)"
                out.append(
                    "  %s: r=%s, p=%s%s%s"
                    % (
                        row.get("attribute", "?"), fmt(coeff), fmt(row.get("p-val")),
                        strength, f", n={n_points}" if n_points else "",
                    )
                )

    # --- Chi-square and anything else, generically ---
    handled = {"anova", "tukey", "pearson", "spearman"}
    for name, by_alpha in stats.items():
        if name in handled:
            continue
        for alpha, result in (by_alpha or {}).items():
            if "error" in result:
                out.append(f"\n{name.upper()} [alpha={alpha}]: not available - {result['error']}")
                continue
            out.append(f"\n{name.upper()} [alpha={alpha}]:")
            body = result.get("chiSquareTest") if name == "chi-square" else result
            if isinstance(body, dict):
                res = body.get("result")
                if res is not None:
                    out.append("  result: " + json.dumps(res)[:600])
                for key in ("row_labels", "column_labels", "params"):
                    if body.get(key) is not None:
                        out.append(f"  {key}: " + json.dumps(body[key])[:400])
                table = body.get("contingencyTable")
                if table is not None:
                    out.append("  contingencyTable: " + json.dumps(table)[:800])
                if res is None and not body.get("contingencyTable"):
                    out.append("  " + json.dumps(body)[:800])
            else:
                out.append("  " + json.dumps(body)[:800])

    # --- Per-product means and overall summary ---
    means = data.get("means") or {}
    if means:
        out.append("\nMeans by product:")
        for product, attrs in means.items():
            parts = [
                f"{a}={fmt((v or {}).get('Mean'))}" if isinstance(v, dict) else f"{a}={fmt(v)}"
                for a, v in (attrs or {}).items()
            ]
            out.append(f"  {product}: " + ", ".join(parts))

    summary = data.get("summaryStatistics") or {}
    if summary:
        out.append("\nSummary statistics:")
        for attr, vals in summary.items():
            if isinstance(vals, dict):
                out.append(
                    "  %s: N=%s, Missing=%s, Mean=%s, StdDev=%s, Min=%s, Max=%s, Sig=%s"
                    % (
                        attr, vals.get("N"), vals.get("Missing"), fmt(vals.get("Mean")),
                        fmt(vals.get("StdDev")), vals.get("Min"), vals.get("Max"),
                        vals.get("Sig", ""),
                    )
                )

    sample = data.get("productSampleData") or {}
    if sample:
        out.append("\nSample sizes per product (from productSampleData):")
        for product, attrs in sample.items():
            for attr, values in (attrs or {}).items():
                if isinstance(values, list):
                    out.append(f"  {product} / {attr}: n={len(values)}")
    return "\n".join(out)


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--question", required=True, help="Question UUID to analyze.")
    ap.add_argument(
        "--types", required=True,
        help="Comma-separated: anova,tukey | pearson,spearman | chi-square | penalty | ...",
    )
    ap.add_argument("--alpha", type=float, default=0.05, help="Significance level (default 0.05).")
    ap.add_argument("--reference", default="", help="Reference question UUID (pearson/spearman/penalty).")
    ap.add_argument("--penalty-level", default="")
    ap.add_argument("--boxing-strategy", default="", help="'Top Box' or thresholds like '0,3,7,10'.")
    ap.add_argument("--survey", default="", help="Survey UUID; verifies the question belongs to it.")
    ap.add_argument("--raw", action="store_true", help="Print the untouched JSON response.")
    ap.add_argument("--why", default="",
                    help="why this call was made (recorded in the trajectory log)")
    args = ap.parse_args()

    if not UUID_RE.match(args.question):
        print(f"Error: '{args.question}' is not a valid question UUID.", file=sys.stderr)
        return 2

    requested = {t.strip().lower() for t in args.types.split(",") if t.strip()}
    if requested & REQUIRES_REFERENCE and not args.reference:
        print(
            "Error: %s requires --reference (a second question to correlate against). "
            "Resolve it from the survey's questions first; do not invent one."
            % ",".join(sorted(requested & REQUIRES_REFERENCE)),
            file=sys.stderr,
        )
        return 2
    if args.reference and not UUID_RE.match(args.reference):
        print(f"Error: reference '{args.reference}' is not a valid UUID.", file=sys.stderr)
        return 2
    if "penalty" in requested and not args.penalty_level:
        print("Error: 'penalty' also requires --penalty-level.", file=sys.stderr)
        return 2

    if args.survey:
        for qid, label in ((args.question, "--question"), (args.reference, "--reference")):
            if not qid:
                continue
            owned = question_belongs_to_survey(qid, args.survey)
            if owned is False:
                print(
                    f"Error: {label} {qid} does not belong to survey {args.survey}.",
                    file=sys.stderr,
                )
                return 2
            if owned is None:
                # Fail closed, matching the agent's ScopeLookupUnavailable path:
                # an unverifiable scope check must not widen the tenancy envelope
                # exactly when it is blind. db_up.sh makes this reachable.
                print(
                    "Error: could not verify question ownership - the local DB is "
                    "unreachable, so --survey cannot be enforced. Run "
                    "scripts/db_up.sh and retry (or drop --survey only if you have "
                    "already confirmed ownership another way).",
                    file=sys.stderr,
                )
                return 2

    params = {"statsTypes": args.types, "alphaValue": args.alpha}
    if args.reference:
        params["referenceQuestionId"] = args.reference
    if args.penalty_level:
        params["penaltyLevel"] = args.penalty_level
    if args.boxing_strategy:
        params["boxingStrategy"] = args.boxing_strategy

    status, body = fetch(args.question, params)

    if status != 200:
        message = body.get("message", body) if isinstance(body, dict) else body
        trace_log.log("survey_statistics", "stats_api.py", purpose=args.why,
                      args={"question": args.question, "types": args.types,
                            "alpha": args.alpha, "reference": args.reference},
                      outcome="error", error=f"HTTP {status}: {str(message)[:200]}")
        print(f"Charts API error ({status}): {message}", file=sys.stderr)
        if isinstance(message, str) and "No data found" in message:
            print(
                "Note: the endpoint reads the production charts database. This question "
                "has no data there - compute the same statistics locally with "
                "scripts/sql_stats.py instead of reporting 'unavailable'.",
                file=sys.stderr,
            )
        return 3

    trace_log.log("survey_statistics", "stats_api.py", purpose=args.why,
                  args={"question": args.question, "types": args.types,
                        "alpha": args.alpha, "reference": args.reference},
                  outcome="ok", detail=f"tests: {args.types}")

    if args.raw:
        print(json.dumps(body, indent=2))
        return 0

    print(render(body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
