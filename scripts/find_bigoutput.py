"""Find survey+question combinations whose SQL result is large enough to fire a condenser.

The drilling trigger is one number: a result whose serialized payload exceeds TOOL_CHAR_LIMIT
is parked, and a parked result is what run_drill() condenses. So "which combination triggers
drilling" is answerable directly against the DB -- no LLM needed -- by running candidate
queries and measuring exactly what call_tool would measure:

    json.dumps(rows, default=str, ensure_ascii=False)

Reported per candidate: rows, payload chars, approx tokens, and which gate it lands in at the
current limits. Use it to pick the cases for a workflow-vs-agent condenser A/B: the useful band
is payloads over the park threshold but under the context ceiling, where BOTH condensers can
actually complete and can therefore be compared.

    python scripts/find_bigoutput.py                # rank the built-in candidates
    python scripts/find_bigoutput.py --band-only    # only those that park at current limits
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The one survey in this database with a complete scope chain (survey -> organization ->
# account -> client), so the same ids work through the HTTP API, which derives scope.
PEPSICO = ("f6b05cb4-cea2-4855-816e-c92e5e5d22ff", "17b6da66-3c2e-42f1-bb64-2a09bdfbc183",
           "e14528a9-01ac-4dff-844e-0914dbdb3759")
HERBALIFE = ("37cbf852-a2b7-4f8b-96b4-7f67432f88cd", "c75d846a-e265-4f69-92c1-91308e0697f6",
             "6263cf71-23b7-4462-9ccf-4a00a7267672")

SURVEY = PEPSICO[2]
HERB = HERBALIFE[2]

# Each candidate is the SQL a competent analyst would actually write for the question beside
# it -- row-level detail that cannot be aggregated away without destroying what was asked for.
# That matters: a candidate the model would never generate is not a test of the trigger.
CANDIDATES = [
    ("verbatims-herbalife",
     "Show me the verbatim open-ended comments respondents wrote about each product, with the "
     "respondent identifier alongside each comment.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, a.value AS comment
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     WHERE q."surveyId" = '{HERB}' AND q."typeOfQuestion" = 'open-answer'
       AND a.value IS NOT NULL AND a.value <> ''
     """),

    ("respondent-scores-1q",
     "For the overall packaging liking question, list every individual respondent's score for "
     "each product.",
     """
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, qo.label AS response, qo.analytical_value AS score
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q.id = '24d1c8fe-2553-40cd-beb1-07f4cb9cc6af'
     """),

    # The band right at the trigger. Multiple-choice "select all that apply" fans one answer
    # out to several option rows, so row count -- and payload -- scales with how many boxes
    # people ticked, which is what puts these just over the threshold rather than far past it.
    ("tortilla-chips-selections",
     "Which plain or salted tortilla chips has each respondent eaten in the past 3 months? "
     "One row per respondent per selection.",
     """
     SELECT a.enrollment_id AS respondent, q.prompt AS question, qo.label AS selection
     FROM answer a
     JOIN question q ON q.id = a.question_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q.id = 'd4b504fd-a087-4598-bacb-c21c4c96e6f4'
     """),

    ("favourite-flavours",
     "Which salty snack flavours are each respondent's favourites? Give me every selection "
     "each respondent made.",
     """
     SELECT a.enrollment_id AS respondent, q.prompt AS question, qo.label AS selection
     FROM answer a
     JOIN question q ON q.id = a.question_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q.id = 'd1fe2a79-c436-445f-b8d9-60ce8f4b8c53'
     """),

    ("product-feelings",
     "How did the product make each respondent feel? Give me every option each respondent "
     "selected, by product.",
     """
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, qo.label AS selection
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q.id = '39bb03f5-1a41-45a6-bc56-6b7186ba2203'
     """),

    ("purchase-reasons",
     "Why would each respondent buy this product? Give me every reason each respondent "
     "selected, by product.",
     """
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, qo.label AS selection
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q.id = '5920daf7-ae6e-41b2-8ec4-3c98a438df9e'
     """),

    ("verbatims-pepsico",
     "Show me the verbatim open-ended comments respondents wrote about each product, with the "
     "respondent identifier alongside each comment.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, a.value AS comment
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     WHERE q."surveyId" = '{SURVEY}' AND q."typeOfQuestion" = 'open-answer'
       AND a.value IS NOT NULL AND a.value <> ''
     """),

    ("packaging-matrix-functional",
     "For the functional packaging statements, give me each respondent's selected option for "
     "every statement, broken down by product.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, mro.label AS statement, qo.label AS response
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     LEFT JOIN question_option mro ON mro.id = aqo.matrix_row_option_id
     WHERE q.id = '71d243a3-69ed-4281-92ac-8a283a969d22'
     """),

    ("packaging-matrix-overall",
     "For the overall packaging impression statements, give me the respondent-level detail: "
     "each respondent's selected option for every statement, by product.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, mro.label AS statement, qo.label AS response
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     LEFT JOIN question_option mro ON mro.id = aqo.matrix_row_option_id
     WHERE q.id = 'd0c494cb-3abf-43f6-9dbf-ee5644a8a7eb'
     """),

    ("consumption-multichoice",
     "Which salty snacks has each respondent consumed? Give me every selection each respondent "
     "made, one row per selection.",
     f"""
     SELECT a.enrollment_id AS respondent, q.prompt AS question, qo.label AS selection
     FROM answer a
     JOIN question q ON q.id = a.question_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q.id = 'cb4270a3-8297-4ba2-a22d-c3c56b3b0e96'
     """),

    ("agreement-matrix-single",
     "For the agreement statements, give me each respondent's rating on every statement, "
     "broken down by product.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, mro.label AS statement, qo.label AS response
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     LEFT JOIN question_option mro ON mro.id = aqo.matrix_row_option_id
     WHERE q.id = 'e5f5b384-5b78-4cbb-b3b5-42398a32d894'
     """),

    ("all-matrix-detail",
     "For the matrix packaging statements, give me the respondent-level detail: each "
     "respondent's selected option for every statement, broken down by product.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, mro.label AS statement, qo.label AS response
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     LEFT JOIN question_option mro ON mro.id = aqo.matrix_row_option_id
     WHERE q."surveyId" = '{SURVEY}' AND q."typeOfQuestion" = 'matrix'
     """),

    ("all-ratings-respondent-level",
     "Give me every rating every respondent gave, for every rating question and every product.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, qo.label AS response, qo.analytical_value AS score
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q."surveyId" = '{SURVEY}' AND q."typeOfQuestion" = 'vertical-rating'
     """),

    ("full-export",
     "Export the complete raw response data for this survey: every respondent, every question, "
     "every product, every answer.",
     f"""
     SELECT a.enrollment_id AS respondent, p."blindingNumber" AS product,
            q.prompt AS question, q."typeOfQuestion" AS qtype,
            a.value AS raw_value, qo.label AS response
     FROM answer a
     JOIN question q ON q.id = a.question_id
     LEFT JOIN product p ON p.id = a.product_id
     LEFT JOIN answered_question_options aqo ON aqo.answer_id = a.id
     LEFT JOIN question_option qo ON qo.id = aqo.question_option_id
     WHERE q."surveyId" = '{SURVEY}'
     """),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band-only", action="store_true",
                    help="only print candidates that park at the current limits")
    a = ap.parse_args()

    import funda_agent_exp as m
    from sqlalchemy import text

    engine = m._sql_engine()
    print(f"TOOL_CHAR_LIMIT={m.TOOL_CHAR_LIMIT}  SMALL_RESULT_ROWS={m.SMALL_RESULT_ROWS}  "
          f"SMALL_RESULT_CHARS={m.SMALL_RESULT_CHARS}  "
          f"CANDIDATE_LIST_CHARS={m.CANDIDATE_LIST_CHARS}\n")
    print(f"{'candidate':<30} {'rows':>8} {'chars':>10} {'~tokens':>9} {'sql_s':>7}  gate")
    print("-" * 92)

    out = []
    for name, question, sql in CANDIDATES:
        t0 = time.perf_counter()
        with engine.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = 120000"))
            res = conn.execute(text(sql))
            cols = list(res.keys())
            rows = [dict(zip(cols, r, strict=False)) for r in res.fetchall()]
        sql_s = time.perf_counter() - t0
        # Exactly what call_tool serializes and measures.
        payload = json.dumps(rows, default=str, ensure_ascii=False)
        chars = len(payload)

        small_shape = len(rows) <= m.SMALL_RESULT_ROWS and chars <= m.SMALL_RESULT_CHARS
        choice_set = m._is_candidate_list(rows) and chars <= m.CANDIDATE_LIST_CHARS
        parks = chars > m.TOOL_CHAR_LIMIT and not small_shape and not choice_set
        gate = "PARKS -- condenser fires" if parks else "inline"
        if a.band_only and not parks:
            continue
        print(f"{name:<30} {len(rows):>8,} {chars:>10,} {chars // 4:>9,} {sql_s:>7.2f}  {gate}")
        out.append({"name": name, "question": question, "rows": len(rows), "chars": chars,
                    "approx_tokens": chars // 4, "sql_s": round(sql_s, 2), "parks": parks,
                    "sql": " ".join(sql.split())})

    dest = REPO / "needle_haystack_results" / "bigoutput_candidates.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {dest}")


if __name__ == "__main__":
    main()
