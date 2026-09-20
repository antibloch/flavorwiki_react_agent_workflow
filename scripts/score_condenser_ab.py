"""Score the workflow-vs-agent condenser A/B on accuracy, time and tokens.

Accuracy is checked against the database rather than judged, so the two arms are scored on
the same facts. Ground truth is recomputed live on every run -- hardcoding it would let the
scores drift silently the moment the dump changes.

Each case declares checks of two kinds:
  fact      a value that must appear in the answer (a count, a mean, a label)
  no_claim  a wrong value that must NOT appear -- catches a condenser that reports the
            payload's row count as if it were the number of things asked about

    python scripts/score_condenser_ab.py
    python scripts/score_condenser_ab.py --dir needle_haystack_results/condenser_ab
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SURVEY = "e14528a9-01ac-4dff-844e-0914dbdb3759"
MATRIX_AGREE = ("e5f5b384-5b78-4cbb-b3b5-42398a32d894", "fdebd104-2746-4ff4-a556-a002ff119f6b")
FEELINGS_Q = "39bb03f5-1a41-45a6-bc56-6b7186ba2203"


def _rows(engine, sql):
    from sqlalchemy import text
    with engine.connect() as conn:
        res = conn.execute(text(sql))
        cols = list(res.keys())
        return [dict(zip(cols, r, strict=False)) for r in res.fetchall()]


def ground_truth(engine):
    """Everything the answers are scored against, straight from the DB."""
    gt = {}

    # A product may legitimately be named or numbered -- REPORTING_RULES tells the agent to
    # prefer the name and keeps the blinding number allowed -- so identity has to accept
    # either. Scoring only the number marked a correct answer wrong four times over.
    prods = _rows(engine, f"""
        SELECT "blindingNumber" blind, name FROM product WHERE "surveyId"='{SURVEY}'
          AND "blindingNumber" <> ''""")
    gt["product_alias"] = {p["blind"]: [p["blind"], p["name"]] for p in prods}

    # L1 -- feelings by product. The top three per product are what any correct answer must
    # get right; the full 13x4 grid is too much to demand of a summary.
    feel = _rows(engine, f"""
        SELECT p."blindingNumber" product, qo.label selection, count(*) n
        FROM answer a JOIN question q ON q.id=a.question_id
        LEFT JOIN product p ON p.id=a.product_id
        JOIN answered_question_options aqo ON aqo.answer_id=a.id
        LEFT JOIN question_option qo ON qo.id=aqo.question_option_id
        WHERE q.id='{FEELINGS_Q}' GROUP BY 1,2 ORDER BY 1, n DESC""")
    by_prod = {}
    for r in feel:
        by_prod.setdefault(r["product"], []).append((r["selection"], int(r["n"])))
    gt["L1"] = {"top3": {p: v[:3] for p, v in by_prod.items()},
                "n_rows": sum(int(r["n"]) for r in feel)}

    # L2 -- open-ended. Only one of the six open-answer questions is a real verbatim; the rest
    # are data-entry fields, so reporting all 4,822 as "comments" is the failure to catch.
    oa = _rows(engine, f"""
        SELECT q.prompt, count(*) n, round(avg(length(a.value))) avg_len
        FROM answer a JOIN question q ON q.id=a.question_id
        WHERE q."surveyId"='{SURVEY}' AND q."typeOfQuestion"='open-answer'
          AND a.value IS NOT NULL AND a.value<>'' GROUP BY 1 ORDER BY n DESC""")
    verbatim = [r for r in oa if "like about this product" in r["prompt"].lower()]
    # Sample from EVERY open-answer row, not just the longest. The longest all come from the
    # 1,183-row question, so a run that fully quoted the two small questions -- 39 verbatims,
    # every one of them real -- scored zero for quoting nothing.
    samples = _rows(engine, f"""
        SELECT DISTINCT a.value FROM answer a JOIN question q ON q.id=a.question_id
        WHERE q."surveyId"='{SURVEY}' AND q."typeOfQuestion"='open-answer'
          AND length(a.value) > 24""")
    gt["L2"] = {"verbatim_n": int(verbatim[0]["n"]) if verbatim else 0,
                "total_open_rows": sum(int(r["n"]) for r in oa),
                "breakdown": [(r["prompt"], int(r["n"])) for r in oa],
                "samples": [s["value"] for s in samples]}

    # L3 -- top agreement statement per product.
    agree = _rows(engine, f"""
        WITH d AS (
          SELECT p."blindingNumber" product, mro.label statement,
                 avg(qo.analytical_value) mean
          FROM answer a JOIN question q ON q.id=a.question_id
          LEFT JOIN product p ON p.id=a.product_id
          JOIN answered_question_options aqo ON aqo.answer_id=a.id
          LEFT JOIN question_option qo ON qo.id=aqo.question_option_id
          LEFT JOIN question_option mro ON mro.id=aqo.matrix_row_option_id
          WHERE q.id IN {MATRIX_AGREE} AND mro.label IS NOT NULL
            AND p."blindingNumber" IS NOT NULL
          GROUP BY 1,2),
        r AS (SELECT *, row_number() OVER (PARTITION BY product ORDER BY mean DESC) rk FROM d)
        SELECT product, statement, round(mean,3) mean FROM r WHERE rk=1 ORDER BY product""")
    gt["L3"] = {"top": {r["product"]: (r["statement"], float(r["mean"])) for r in agree}}

    # L4 -- the export's own shape. 300 respondents per product against 2,400 in the survey is
    # the trap: only half the panel ever reached product evaluation.
    split = _rows(engine, f"""
        SELECT coalesce(p."blindingNumber",'(none)') product, count(*) answers,
               count(DISTINCT a.enrollment_id) respondents
        FROM answer a JOIN question q ON q.id=a.question_id
        LEFT JOIN product p ON p.id=a.product_id
        WHERE q."surveyId"='{SURVEY}' GROUP BY 1 ORDER BY 1""")
    gt["L4"] = {"per_product": {r["product"]: (int(r["answers"]), int(r["respondents"]))
                                for r in split},
                "total_answers": sum(int(r["answers"]) for r in split)}
    return gt


def _has_num(text, n, tol=0):
    """True if `n` appears as a standalone number, comma-grouped or not."""
    for cand in range(n - tol, n + tol + 1):
        plain, grouped = str(cand), f"{cand:,}"
        if re.search(rf"(?<![\d.]){re.escape(plain)}(?![\d.])", text):
            return True
        if grouped != plain and grouped in text:
            return True
    return False


def score(case, answer, gt):
    """-> (list of (label, passed), n_pass, n_total). Empty answer scores zero, not an error."""
    a = answer or ""
    low = a.lower()
    checks = []

    def names(prod):
        """Product identified by blinding number OR by name -- both are correct."""
        return any(alias.lower() in low for alias in gt["product_alias"].get(prod, [prod]))

    if case == "L1":
        for prod, top3 in sorted(gt["L1"]["top3"].items()):
            checks.append((f"product {prod} identified", names(prod)))
            for label, n in top3:
                checks.append((f"{prod}: {label}={n}",
                               label.lower() in low and _has_num(a, n)))

    elif case == "L2":
        # The point of a verbatim request is the text itself, so quoting real answers is the
        # check that matters -- a correct count with no text is not the thing asked for.
        quoted = sum(1 for s in gt["L2"]["samples"] if s[:40].lower() in low)
        n_s = len(gt["L2"]["samples"])
        checks.append((f"quotes real verbatim text ({quoted} of {n_s} distinct)", quoted > 0))
        checks.append((f"quotes many ({quoted}>=10)", quoted >= 10))
        checks.append(("identifies the real verbatim question",
                       "like about this product" in low))
        checks.append((f"verbatim count {gt['L2']['verbatim_n']}",
                       _has_num(a, gt["L2"]["verbatim_n"], tol=2)))
        # Both arms correctly called 4,822 the open-ended ROW total and broke it down, so the
        # earlier "must not mention 4822" check was penalising a true statement. What actually
        # distinguishes a good answer is separating the real verbatim from the data-entry
        # fields, which is what this counts.
        got = sum(1 for prompt, n in gt["L2"]["breakdown"] if _has_num(a, n, tol=1))
        checks.append((f"breaks the 6 open-ended questions out by count ({got}/6)", got >= 5))

    elif case == "L3":
        for prod, (stmt, mean) in sorted(gt["L3"]["top"].items()):
            key = stmt.lower()[:22]
            checks.append((f"{prod}: top = {stmt[:34]!r}", names(prod) and key in low))
            checks.append((f"{prod}: mean ~{mean}", f"{mean:.2f}" in a or f"{mean:.3f}" in a))

    elif case == "L4":
        for prod, (ans, resp) in sorted(gt["L4"]["per_product"].items()):
            if prod == "(none)":
                continue
            checks.append((f"{prod}: {ans} answers", _has_num(a, ans, tol=1)))
        checks.append((f"total {gt['L4']['total_answers']} answers",
                       _has_num(a, gt["L4"]["total_answers"], tol=2)))
        checks.append(("300 respondents per product", _has_num(a, 300)))
        checks.append(("does not claim 2400 tasted every product",
                       not re.search(r"2,?400\s+respondents?\s+(?:per|for each)", low)))

    return checks, sum(1 for _, ok in checks if ok), len(checks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="needle_haystack_results/condenser_ab")
    ap.add_argument("--verbose", action="store_true", help="print every check")
    a = ap.parse_args()
    d = REPO / a.dir

    import funda_agent_exp as m
    gt = ground_truth(m._sql_engine())

    # maxtool is the largest string a tool RETURNED, which for a parked result is the manifest,
    # not the payload -- the payload never enters the loop. Payload sizes are in the [sql] log
    # lines and in scripts/find_bigoutput.py.
    print(f"{'case':<5} {'arm':<9} {'acc':>9} {'fired':>6} {'wall_s':>8} {'turns':>6} "
          f"{'in_tok':>9} {'out_tok':>8} {'drill_s':>8} {'drill_in':>9} {'sql':>4} "
          f"{'maxtool':>9}")
    print("-" * 102)

    totals, fired_totals = {}, {}
    for case in ("L1", "L2", "L3", "L4"):
        for arm in ("agent", "workflow"):
            reps = sorted(d.glob(f"{case}_{arm}_r*.json")) or sorted(
                d.glob(f"{case}_{arm}.json"))
            if not reps:
                print(f"{case:<5} {arm:<9} {'MISSING':>9}")
                continue
            for f in reps:
                r = json.loads(f.read_text(encoding="utf-8"))
                checks, npass, ntot = score(case, r.get("final_answer", ""), gt)
                drill = r.get("drill", {})
                # Whether a condenser ran at all. A row with fired=NO is NOT a condenser
                # comparison -- the payload inlined and both arms took the same path, so it
                # is kept out of the condenser totals below rather than silently averaged in.
                did_fire = bool(drill.get("fired"))
                rep = re.search(r"_r(\d+)\.json$", f.name)
                tag = f"{arm}" + (f" r{rep.group(1)}" if rep else "")
                print(f"{case:<5} {tag:<9} {f'{npass}/{ntot}':>9} "
                      f"{'yes' if did_fire else 'NO':>6} {r['wall_s']:>8.1f} "
                      f"{r['llm_turns']:>6} "
                      f"{r['tokens']['input']:>9,} {r['tokens']['output']:>8,} "
                      f"{drill.get('latency_s', 0):>8.1f} {drill.get('input_tokens', 0):>9,} "
                      f"{r['sql_calls']:>4} {r['max_tool_out_chars']:>9,}")
                for bucket in ((totals,) if not did_fire else (totals, fired_totals)):
                    t = bucket.setdefault(arm, {"pass": 0, "tot": 0, "wall": [], "in": 0,
                                                "out": 0, "drill_s": 0.0, "err": 0, "n": 0})
                    t["pass"] += npass; t["tot"] += ntot; t["wall"].append(r["wall_s"])
                    t["in"] += r["tokens"]["input"]; t["out"] += r["tokens"]["output"]
                    t["drill_s"] += drill.get("latency_s", 0)
                    t["err"] += bool(r.get("error")); t["n"] += 1
                if a.verbose:
                    for label, ok in checks:
                        print(f"        [{'PASS' if ok else 'FAIL'}] {label}")

    def report(title, book):
        print("-" * 102)
        for arm, t in book.items():
            pct = 100 * t["pass"] / t["tot"] if t["tot"] else 0
            w = sorted(t["wall"])
            # Report the spread, not just the mean: identical runs here differ by multiples,
            # so a mean alone would imply precision the measurement does not have.
            print(f"{title:<9} {arm:<9} {t['pass']}/{t['tot']} ({pct:.0f}%)  n={t['n']}  "
                  f"wall_mean={sum(w) / len(w):.1f}s [{w[0]:.1f}-{w[-1]:.1f}]  "
                  f"in={t['in']:,}  out={t['out']:,}  drill={t['drill_s']:.1f}s  "
                  f"errors={t['err']}")

    report("ALL", totals)
    print("\n(condenser-only: runs where a condenser actually fired -- the real comparison)")
    report("FIRED", fired_totals)


if __name__ == "__main__":
    main()
