"""Validate the candidate funda_agent_exp fixes against regression.xlsx.

The needle set is entirely measure-selection questions, which is exactly what pre-fetch
optimises for. regression.xlsx is a broader mix -- survey metadata, cross-survey historical
comparisons, explicit statistical tests, benchmark comparisons -- so it can show whether a
fix that wins there is a general win or an overfit.

Variants:
  none            the agent as it ships
  bindings        run_survey_stats reachable from turn 0. As shipped, model_lean /
                  weak_model_lean bind ONLY nl2sql_tool and are used whenever nothing has
                  parked, so the stats tool is unreachable for most of a run.
  prefetch_fixed  inject the survey inventory (product roster + per-product stats for numeric
                  product-linked measures + every other answered measure with counts and
                  sample option labels) into turn 0
  both            bindings + prefetch_fixed
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SCORED_SQL = """
WITH scored AS (
  SELECT q.id qid,q.prompt,q."typeOfQuestion" qtype,
         jsonb_array_length(q.settings->'positionLabels') sp,
         p.name product,(aqo."answerData"->>'optionAnswer')::numeric score,a.enrollment_id
  FROM question q JOIN answer a ON a.question_id=q.id
  JOIN answered_question_options aqo ON aqo.answer_id=a.id
  JOIN product p ON p.id=a.product_id
  WHERE q."surveyId"=:survey_id
    AND aqo."answerData"->>'optionAnswer' ~ '^-?[0-9]+(\\.[0-9]+)?$'
), per AS (
  SELECT qid,prompt,qtype,sp,product,COUNT(score) n,COUNT(DISTINCT enrollment_id) r,
         ROUND(AVG(score),2) mean,ROUND(STDDEV_SAMP(score),2) sd,MIN(score) lo,MAX(score) hi
  FROM scored GROUP BY qid,prompt,qtype,sp,product
)
SELECT jsonb_agg(x ORDER BY x->>'prompt') FROM (
  SELECT jsonb_build_object('qid',qid,'prompt',prompt,'type',qtype,'scale_points',sp,
         'observed',jsonb_build_array(MIN(lo),MAX(hi)),
         'by_product',jsonb_agg(jsonb_build_object('product',product,'n',n,
           'respondents',r,'mean',mean,'sd',sd) ORDER BY mean DESC)) x
  FROM per GROUP BY qid,prompt,qtype,sp) y
"""

PRODUCTS_SQL = """
SELECT jsonb_agg(jsonb_build_object('product',name,'blindingNumber',"blindingNumber") ORDER BY name)
FROM product WHERE "surveyId"=:survey_id
"""

OTHER_SQL = """
WITH scored_q AS (
  SELECT DISTINCT q.id qid
  FROM question q JOIN answer a ON a.question_id=q.id
  JOIN answered_question_options aqo ON aqo.answer_id=a.id
  JOIN product p ON p.id=a.product_id
  WHERE q."surveyId"=:survey_id
    AND aqo."answerData"->>'optionAnswer' ~ '^-?[0-9]+(\\.[0-9]+)?$'
)
SELECT jsonb_agg(jsonb_build_object('qid',qid,'prompt',prompt,'type',qtype,
       'product_linked',prod_linked,'answers',answers,'respondents',resp,
       'sample_option_labels',to_jsonb(labels)) ORDER BY prompt)
FROM (
  SELECT q.id qid,q.prompt,q."typeOfQuestion" qtype,
         bool_or(a.product_id IS NOT NULL) prod_linked,
         COUNT(*) answers,COUNT(DISTINCT a.enrollment_id) resp,
         (array_agg(DISTINCT aqo."answerData"->>'optionLabel')
            FILTER (WHERE aqo."answerData" ? 'optionLabel'))[1:12] labels
  FROM question q JOIN answer a ON a.question_id=q.id
  JOIN answered_question_options aqo ON aqo.answer_id=a.id
  WHERE q."surveyId"=:survey_id AND q.id NOT IN (SELECT qid FROM scored_q)
  GROUP BY q.id,q.prompt,q."typeOfQuestion") t
"""

LLM_CALLS, TOOL_CALLS = [], []


def load_rows():
    import openpyxl
    ws = openpyxl.load_workbook(REPO / "regression.xlsx").worksheets[0]
    return [
        {"prompt": str(r[2]).strip(), "type": r[3], "client": r[1],
         "client_id": str(r[4]).strip(), "survey_id": str(r[5]).strip(),
         "organization_id": str(r[6]).strip()}
        for r in ws.iter_rows(min_row=2, values_only=True)
        if r[2] and r[4] and r[5] and r[6]
    ]


def probes():
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import BaseTool
    oi = ChatOpenAI.invoke

    def li(self, inp, config=None, *, stop=None, **kw):
        t0 = time.perf_counter()
        r = oi(self, inp, config, stop=stop, **kw)
        u = getattr(r, "usage_metadata", None) or {}
        LLM_CALLS.append({
            "model": self.model_name, "latency_s": round(time.perf_counter() - t0, 2),
            "input_tokens": u.get("input_tokens"),
            "cached_input_tokens": (u.get("input_token_details") or {}).get("cache_read", 0),
            "output_tokens": u.get("output_tokens"),
            "reasoning_tokens": (u.get("output_token_details") or {}).get("reasoning", 0)})
        return r
    ChatOpenAI.invoke = li

    ot = BaseTool.invoke

    def ti(self, inp, config=None, **kw):
        out = ot(self, inp, config, **kw)
        s = out if isinstance(out, str) else str(out)
        TOOL_CALLS.append({
            "tool": self.name,
            "sql": str(inp.get("sql_query", ""))[:3000] if isinstance(inp, dict) else "",
            "args": {k: str(v)[:120] for k, v in inp.items()} if isinstance(inp, dict) else {},
            "out_chars": len(s), "parked": "RESULT STORED" in s,
            "error": s.lstrip("[").startswith("Error") or "SQL error" in s[:600],
            "out_head": s[:600]})
        return out
    BaseTool.invoke = ti


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--row", type=int, required=True, help="1-based index into populated rows")
    ap.add_argument("--variant", required=True,
                    choices=["none", "bindings", "prefetch_fixed", "both"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    row = load_rows()[a.row - 1]
    probes()
    import funda_agent_exp as m
    from langchain_core.messages import HumanMessage
    from sqlalchemy import text

    if a.variant in ("bindings", "both"):
        m.model_lean = m.model
        m.weak_model_lean = m.weak_model

    m._SCOPE.update({k: row[k] for k in ("client_id", "organization_id", "survey_id")})
    m.PARKED_RESULT_IDS.clear()

    payload, prefetch_s = "", 0.0
    if a.variant in ("prefetch_fixed", "both"):
        t0 = time.perf_counter()
        with m._sql_engine().connect() as conn:
            p = {"survey_id": row["survey_id"]}
            block = {"products": conn.execute(text(PRODUCTS_SQL), p).scalar(),
                     "scored_measures_by_product": conn.execute(text(SCORED_SQL), p).scalar(),
                     "other_answered_measures": conn.execute(text(OTHER_SQL), p).scalar()}
        prefetch_s = time.perf_counter() - t0
        payload = json.dumps(block, ensure_ascii=False, default=str)

    user = m.scoped_query(row["prompt"], row["client_id"], row["organization_id"],
                          row["survey_id"])
    if payload:
        user += (
            "\n\nPRE-FETCHED SURVEY INVENTORY for THIS survey only (computed before this turn). "
            "`products` is the roster with blinding numbers; `scored_measures_by_product` holds "
            "every numeric product-attributable measure with per-product statistics already "
            "aggregated; `other_answered_measures` lists every remaining answered question -- "
            "ranking, matrix, and anything not product-linked -- with counts and a sample of its "
            "option labels. A measure listed there is not necessarily usable; judge that "
            "yourself. Nothing about any OTHER survey is included. Answer from these figures "
            "where they suffice; call nl2sql_tool for anything genuinely not here.\n" + payload)

    state = {"messages": [HumanMessage(content=user)], "number_of_steps": 0,
             "last_sql_observations": {}, "zero_row_streak": 0}

    err, final = None, ""
    t0 = time.perf_counter()
    try:
        res = m.build_graph().invoke(state, config={"recursion_limit": 2 * m.MAX_LLM_STEPS + 2})
        final = m.message_to_text(res["messages"][-1])
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
    wall = time.perf_counter() - t0

    tot = lambda f: sum(c.get(f) or 0 for c in LLM_CALLS)  # noqa: E731
    rec = {"row": a.row, "variant": a.variant, "type": row["type"], "client": row["client"],
           "prompt": row["prompt"], "survey_id": row["survey_id"],
           "prefetch_sql_s": round(prefetch_s, 2), "prefetch_chars": len(payload),
           "wall_s": round(wall, 2), "error": err, "llm_turns": len(LLM_CALLS),
           "tokens": {"input": tot("input_tokens"), "cached_input": tot("cached_input_tokens"),
                      "output": tot("output_tokens"), "reasoning": tot("reasoning_tokens")},
           "sql_calls": sum(1 for t in TOOL_CALLS if t["tool"] == "nl2sql_tool"),
           "stats_calls": sum(1 for t in TOOL_CALLS if t["tool"] == "run_survey_stats"),
           "tool_errors": sum(1 for t in TOOL_CALLS if t.get("error")),
           "tools": [t["tool"] for t in TOOL_CALLS],
           "final_answer": final, "llm_call_detail": LLM_CALLS, "tool_call_detail": TOOL_CALLS}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n##### R{a.row:02d} [{a.variant}] {row['type']} wall={rec['wall_s']}s "
          f"turns={rec['llm_turns']} sql={rec['sql_calls']} stats={rec['stats_calls']} "
          f"in={rec['tokens']['input']} out={rec['tokens']['output']} err={bool(err)}")


if __name__ == "__main__":
    main()
