"""Three-way test on funda_agent_exp: no prefetch / prefetch / prefetch + join-gap fix.

variant=none            -- the agent as it ships
variant=prefetch        -- inject per-product stats for numeric, product-linked measures.
                           Its blind spot: `JOIN product` plus a numeric-optionAnswer filter
                           drops ranking questions, matrix questions (values live in
                           optionLabel) and any non-product-linked scale.
variant=prefetch_fixed  -- same, plus the product roster (with blindingNumbers) and a
                           section listing every OTHER answered measure with its counts and
                           a sample of its option labels. Nothing is hidden from the model;
                           it still has to judge what is usable.
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path("/home/junaid/codework/Flavorwiki/flavorai_v2")
sys.path.insert(0, str(REPO))

CASES = {
    "T1": ("37cbf852-a2b7-4f8b-96b4-7f67432f88cd", "c75d846a-e265-4f69-92c1-91308e0697f6",
           "6263cf71-23b7-4462-9ccf-4a00a7267672",
           "Sort the products in order top to bottom based on overall liking."),
    "T2": ("f6b05cb4-cea2-4855-816e-c92e5e5d22ff", "17b6da66-3c2e-42f1-bb64-2a09bdfbc183",
           "e14528a9-01ac-4dff-844e-0914dbdb3759",
           "Sort the products in order top to bottom based on overall liking."),
    "T3": ("7b445d7f-3fc1-4154-8a23-586e09c781a3", "8dd4fd29-cd84-46a1-abcd-1459c547023b",
           "dedf24e1-5c82-4aeb-b0c6-d6bb4e2d104e",
           "Sort the products from most to least liked overall."),
    "T4": ("c0b3b212-bc35-45ef-b69d-8257d3735a90", "46671273-0b12-49a5-b9de-60f81d192818",
           "39af3240-42a8-4e35-8c7d-c61703d5ce3f",
           "Sort the products in order top to bottom based on overall liking."),
    "T5": ("26ebcbaf-4850-491b-9064-2c8b7f11aaac", "ff890ed1-b484-4ebe-b102-6ebfd40ca275",
           "e84bde45-40ca-4863-b000-a9905c8644bb",
           "Sort the products in order top to bottom based on overall liking."),
    "T6": ("37cbf852-a2b7-4f8b-96b4-7f67432f88cd", "c75d846a-e265-4f69-92c1-91308e0697f6",
           "6263cf71-23b7-4462-9ccf-4a00a7267672",
           "Sort the products by overall liking and compare each against the benchmark."),
}

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
        TOOL_CALLS.append({"tool": self.name,
                           "sql": str(inp.get("sql_query", ""))[:3000] if isinstance(inp, dict) else "",
                           "out_chars": len(s), "parked": "RESULT STORED" in s})
        return out
    BaseTool.invoke = ti


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, choices=sorted(CASES))
    ap.add_argument("--variant", required=True, choices=["none", "prefetch", "prefetch_fixed"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    client, org, survey, prompt = CASES[a.case]

    probes()
    import funda_agent_exp as m
    from langchain_core.messages import HumanMessage
    from sqlalchemy import text

    m._SCOPE.update({"client_id": client, "organization_id": org, "survey_id": survey})
    m.PARKED_RESULT_IDS.clear()

    payload, prefetch_s = "", 0.0
    if a.variant != "none":
        t0 = time.perf_counter()
        with m._sql_engine().connect() as conn:
            scored = conn.execute(text(SCORED_SQL), {"survey_id": survey}).scalar()
            block = {"scored_measures_by_product": scored}
            if a.variant == "prefetch_fixed":
                block = {
                    "products": conn.execute(text(PRODUCTS_SQL), {"survey_id": survey}).scalar(),
                    "scored_measures_by_product": scored,
                    "other_answered_measures": conn.execute(
                        text(OTHER_SQL), {"survey_id": survey}).scalar(),
                }
        prefetch_s = time.perf_counter() - t0
        payload = json.dumps(block, ensure_ascii=False, default=str)

    user = m.scoped_query(prompt, client, org, survey)
    if payload:
        note = (
            "\n\nPRE-FETCHED SURVEY INVENTORY (computed for you before this turn).\n"
            "`scored_measures_by_product` holds every numeric, product-attributable measure "
            "with per-product statistics already aggregated.\n")
        if a.variant == "prefetch_fixed":
            note += (
                "`products` is the product roster with blinding numbers. "
                "`other_answered_measures` lists every remaining answered question -- including "
                "ranking and matrix questions, and anything not linked to a product -- with its "
                "counts and a sample of its option labels. A measure appearing there is NOT "
                "necessarily usable; judge that yourself.\n")
        note += ("Answer from these figures where they suffice. Call nl2sql_tool only for what "
                 "is genuinely not here.\n")
        user += note + payload

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
    rec = {"agent": "funda_agent_exp", "case": a.case, "variant": a.variant,
           "prefetch_sql_s": round(prefetch_s, 2), "prefetch_chars": len(payload),
           "wall_s": round(wall, 2), "error": err, "llm_turns": len(LLM_CALLS),
           "tokens": {"input": tot("input_tokens"), "cached_input": tot("cached_input_tokens"),
                      "output": tot("output_tokens"), "reasoning": tot("reasoning_tokens")},
           "sql_calls": sum(1 for t in TOOL_CALLS if t["tool"] == "nl2sql_tool"),
           "tools": [t["tool"] for t in TOOL_CALLS],
           "final_answer": final, "llm_call_detail": LLM_CALLS, "tool_call_detail": TOOL_CALLS}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n##### {a.case} [{a.variant}] wall={rec['wall_s']}s turns={rec['llm_turns']} "
          f"sql={rec['sql_calls']} prefetch={len(payload)}ch in={rec['tokens']['input']} "
          f"cached={rec['tokens']['cached_input']} out={rec['tokens']['output']} err={bool(err)}")


if __name__ == "__main__":
    main()
