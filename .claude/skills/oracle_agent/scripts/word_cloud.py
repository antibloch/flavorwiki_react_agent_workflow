#!/usr/bin/env python3
"""The oracle's counterpart to `funda_agent_exp.py`'s `generate_word_cloud`.

Same information, independently derived: it pulls the open-text answers for one question
straight out of `gpi-db` and counts terms with **its own** tokenizer. That independence is the
point — a shared tokenizer could not catch a tokenizer defect, and term selection is exactly
where a word cloud goes wrong.

Deliberate parity with the agent's tool, so the two term lists are comparable:

- one term counted at most once per answer (respondent reach, not raw frequency);
- optional per-product grouping;
- a term cap.

Deliberate difference, so a defect is visible rather than shared: the stop-word list and the
token rule are this script's own, and it reports what it dropped.

    word_cloud.py --question <uuid> [--group-by-product] [--max-terms 40] [--why "..."]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_log  # noqa: E402

CONTAINER = "gpi-db"
DB_USER = "gpi"
DB_NAME = "gpi_sample_db"

# This script's own stop list. Kept small and explicit: a term the agent drops and this one
# keeps (or the reverse) is a finding worth seeing, not a bug to paper over.
STOP_WORDS = frozenset("""
a about after all also am an and any are as at be because been before being but by can
cant could did do does doing dont down during each few for from further had has have having
he her here hers him his how i if in into is it its itself just me more most my no nor not
now of off on once only or other our out over own same she should so some such than that
the their them then there these they this those through to too under until up very was we
were what when where which while who whom why will with would you your yours
like really would also get got one two lot bit much many thing things
""".split())

TOKEN_RE = re.compile(r"[^a-z]+")


def fetch_rows(sql: str) -> tuple[list[str], list[list[str]]]:
    proc = subprocess.run(
        ["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
         "-i", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
         "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "--csv", "-c", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    import csv, io
    rows = list(csv.reader(io.StringIO(proc.stdout)))
    return (rows[0], rows[1:]) if rows else ([], [])


ANSWER_SQL = """
SELECT a.enrollment_id::text AS respondent,
       COALESCE(p.name, '') AS product,
       COALESCE(aqo."answerData"->>'openAnswer', aqo."answerData"->>'optionAnswer',
                a.value, '') AS text
FROM answer a
LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
LEFT JOIN product p ON p.id = a.product_id
WHERE a.question_id = '{qid}'
"""

PROMPT_SQL = "SELECT prompt, \"typeOfQuestion\" FROM question WHERE id = '{qid}'"


def tokens(text: str, subject_terms: set[str]) -> set[str]:
    """One answer -> the set of terms it contributes. A set, so reach is counted, not frequency."""
    letters = TOKEN_RE.sub(" ", (text or "").lower())
    return {
        t for t in letters.split()
        if len(t) >= 3 and t not in STOP_WORDS and t not in subject_terms
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Oracle word cloud over gpi-db open text.")
    ap.add_argument("--question", required=True, help="open-answer question UUID")
    ap.add_argument("--group-by-product", action="store_true")
    ap.add_argument("--max-terms", type=int, default=40)
    ap.add_argument("--why", default="", help="why this call was made (recorded in the trace)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    qid = args.question.strip()
    try:
        _, prow = fetch_rows(PROMPT_SQL.format(qid=qid))
        if not prow:
            raise RuntimeError(f"question {qid} not found")
        prompt, qtype = prow[0][0], prow[0][1]
        header, rows = fetch_rows(ANSWER_SQL.format(qid=qid))
    except Exception as exc:  # noqa: BLE001 - reported to the analyst and to the trace
        trace_log.log("word_cloud", "word_cloud.py", purpose=args.why,
                      args={"question": qid, "group_by_product": args.group_by_product},
                      outcome="error", error=str(exc)[:300])
        print(f"WORD CLOUD ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    idx = {name: i for i, name in enumerate(header)}
    products = {r[idx["product"]] for r in rows if r[idx["product"]]}
    subject = set()
    for p in products:
        subject |= tokens(p, set())

    groups: dict[str, Counter] = {}
    answered = 0
    for r in rows:
        text = r[idx["text"]]
        if not text.strip():
            continue
        answered += 1
        key = r[idx["product"]] if args.group_by_product else "ALL"
        groups.setdefault(key, Counter()).update(tokens(text, subject))

    result = {
        "question_id": qid, "prompt": prompt, "type": qtype,
        "answers_with_text": answered, "answers_total": len(rows),
        "grouped": args.group_by_product,
        "subject_terms_excluded": sorted(subject),
        "clouds": {k: [{"label": t, "count": c} for t, c in v.most_common(args.max_terms)]
                   for k, v in sorted(groups.items())},
    }

    trace_log.log("word_cloud", "word_cloud.py", purpose=args.why,
                  args={"question": qid, "group_by_product": args.group_by_product,
                        "max_terms": args.max_terms},
                  outcome="ok",
                  detail=f"{answered} texts, {len(groups)} cloud(s), "
                         f"{sum(len(v) for v in result['clouds'].values())} terms")

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False)); return
    print(f"Question: {prompt}  [{qtype}]")
    print(f"Answers with text: {answered} of {len(rows)}")
    if subject:
        print(f"Product terms excluded: {', '.join(sorted(subject))}")
    for key, terms in result["clouds"].items():
        print(f"\n--- {key} ({len(terms)} terms) ---")
        for t in terms:
            print(f"  {t['count']:>5}  {t['label']}")


if __name__ == "__main__":
    main()
