"""Edge cases where the SQL result is very large, with the drilling agent on and off.

Ordinary questions never exercise the drill: the pre-fetched inventory answers most of them
with no SQL at all, and what SQL does run comes back well under the 8,000-char park threshold
(largest measured on the needle set: 5,266 chars). These four questions all demand row-level
detail that cannot be aggregated away, so the result has to park -- or, with drilling off, has
to be carried inline through the rest of the conversation.

Payload sizes measured against the DB before writing the cases:
  E3   97 KB    356 rows   per-question x per-product audit
  E2  148 KB  1,200 rows   respondent-level scores for one question
  E1  210 KB    900 rows   verbatim open-ended comments
  E4  6.2 MB 33,600 rows   respondent-level matrix detail  (deliberate extreme)

Arms:
  drill_on   defaults -- results over the limit park and the drilling agent runs
  drill_off  every ceiling raised to 10^8 -- nothing can park, everything inlines
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

HERBALIFE = ("37cbf852-a2b7-4f8b-96b4-7f67432f88cd", "c75d846a-e265-4f69-92c1-91308e0697f6",
             "6263cf71-23b7-4462-9ccf-4a00a7267672")
PEPSICO = ("f6b05cb4-cea2-4855-816e-c92e5e5d22ff", "17b6da66-3c2e-42f1-bb64-2a09bdfbc183",
           "e14528a9-01ac-4dff-844e-0914dbdb3759")

CASES = {
    # 900 rows / 210 KB. Verbatims cannot be aggregated without destroying the thing asked for.
    "E1": (*HERBALIFE,
           "Show me the verbatim open-ended comments respondents wrote about each product, "
           "with the respondent identifier alongside each comment. I want the actual text, "
           "not a summary count."),
    # 1,200 rows / 148 KB. Explicitly refuses the aggregate the agent would prefer to compute.
    "E2": (*PEPSICO,
           "For the overall liking question, list every individual respondent's score for each "
           "product. I want the respondent-level detail, not averages."),
    # 356 rows / 97 KB. Wide rather than deep, and every row carries a long prompt string.
    "E3": (*PEPSICO,
           "Produce a complete audit of this survey: for every question and every product, "
           "give the number of answers and the number of distinct respondents."),
    # 33,600 rows / 6.2 MB. Deliberately past anything the context can hold inline.
    "E4": (*PEPSICO,
           "For the matrix packaging statements, give me the respondent-level detail: each "
           "respondent's selected option for every statement, broken down by product."),

    # ---- Condenser A/B set (workflow vs agent) ---------------------------------------------
    # E1-E3 all land UNDER the current 250,000-char limit, so at defaults they inline and no
    # condenser runs at all -- they only exercised drilling back when the limit was 8,000.
    # These four DO park at defaults, and they are chosen to discriminate between the two
    # condensers rather than merely to be big. Payloads measured by scripts/find_bigoutput.py.
    #
    # Each is answerable from the DB, so accuracy is scorable rather than a judgement call.
    #
    # PHRASING IS LOad-BEARING. A big table is not enough: asked for counts, the agent computes
    # them in SQL and comes back with 50 rows / 6.5 KB, which inlines and reaches no condenser
    # at all (measured -- L1's first phrasing did exactly that). Only a question whose answer
    # cannot be collapsed by a GROUP BY forces the row-level result that parks, so each case
    # below asks for the detail explicitly, as E2 and E4 already do.
    #
    # L1  626 KB   3,505 rows  aggregate answer over row-level input -- the workflow computes
    #                          counts over every row, the agent must reach them through a
    #                          50-row query_result cap.
    "L1": (*PEPSICO,
           "Pull the respondent-level selections for the question about how the product made "
           "you feel: one row per respondent per feeling selected, with the product. Do not "
           "aggregate in the query -- I want the individual selections. Then, from that "
           "detail, tell me for each product how many respondents chose each feeling."),
    # L2  1.0 MB   4,822 rows  specific records -- the opposite shape: statistics over the rows
    #                          destroy the thing asked for, and only sampling can answer it.
    "L2": (*PEPSICO,
           "Show me the actual text of every open-ended response in this survey, with the "
           "respondent identifier beside each one. I want the verbatim text, not a summary."),
    # L3  6.0 MB  20,400 rows  aggregate at a size where inlining is fatal, so the condenser is
    #                          not an optimisation but the only path to an answer.
    "L3": (*PEPSICO,
           "For the agreement statements, pull each individual respondent's rating on every "
           "statement broken down by product -- the respondent-level rows, not averages. From "
           "that detail, tell me which statement scores highest for each product."),
    # L4   61 MB 207,542 rows  the whole survey, raw. Tests whether either condenser degrades
    #                          gracefully or silently drops most of the result. Kept distinct
    #                          in SIZE from L3: an earlier L4 phrased around the matrix
    #                          questions produced the same 33,600 rows L3 did, because the
    #                          agent widened L3 to every matrix question anyway.
    "L4": (*PEPSICO,
           "Export the complete raw response data for this survey -- every respondent, every "
           "question, every product, every answer, one row each, no aggregation -- then tell "
           "me how many responses it covers and how they split across the products."),
}

LLM_CALLS, TOOL_CALLS, DRILL_RUNS = [], [], []
IN_DRILL = False


def probes():
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import BaseTool
    oi = ChatOpenAI.invoke

    def li(self, inp, config=None, *, stop=None, **kw):
        t0 = time.perf_counter()
        try:
            r = oi(self, inp, config, stop=stop, **kw)
        except Exception as exc:  # a context-overflow refusal is a result, not a crash
            LLM_CALLS.append({"model": self.model_name,
                              "latency_s": round(time.perf_counter() - t0, 2),
                              "error": f"{type(exc).__name__}: {exc}"[:500],
                              "in_drill": IN_DRILL})
            raise
        u = getattr(r, "usage_metadata", None) or {}
        LLM_CALLS.append({
            "model": self.model_name, "latency_s": round(time.perf_counter() - t0, 2),
            "input_tokens": u.get("input_tokens"),
            "cached_input_tokens": (u.get("input_token_details") or {}).get("cache_read", 0),
            "output_tokens": u.get("output_tokens"),
            "reasoning_tokens": (u.get("output_token_details") or {}).get("reasoning", 0),
            "in_drill": IN_DRILL})
        return r
    ChatOpenAI.invoke = li

    ot = BaseTool.invoke

    def ti(self, inp, config=None, **kw):
        t0 = time.perf_counter()
        out = ot(self, inp, config, **kw)
        s = out if isinstance(out, str) else str(out)
        TOOL_CALLS.append({
            "tool": self.name, "latency_s": round(time.perf_counter() - t0, 2),
            "sql": str(inp.get("sql_query", ""))[:2500] if isinstance(inp, dict) else "",
            "out_chars": len(s), "parked": "RESULT STORED" in s,
            "error": "SQL error" in s[:800], "out_head": s[:500], "in_drill": IN_DRILL})
        return out
    BaseTool.invoke = ti


def drill_probe(module):
    original = getattr(module, "run_drill", None)
    if original is None:
        return

    def probed(*a, **k):
        global IN_DRILL
        before = len(LLM_CALLS)
        t0 = time.perf_counter()
        IN_DRILL = True
        try:
            out = original(*a, **k)
        finally:
            IN_DRILL = False
        DRILL_RUNS.append({
            "latency_s": round(time.perf_counter() - t0, 2),
            "llm_turns": len(LLM_CALLS) - before,
            "out_chars": len(out) if isinstance(out, str) else None,
            "fell_back_to_manifest": isinstance(out, str) and "RESULT STORED" in out,
            "refined_head": (out if isinstance(out, str) else str(out))[:1500]})
        return out
    module.run_drill = probed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, choices=sorted(CASES))
    # Free-form label. The actual configuration comes from the environment
    # (TOOL_CHAR_LIMIT, ENABLE_DIGEST, ...), which the record captures under "limits".
    ap.add_argument("--arm", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    client, org, survey, prompt = CASES[a.case]

    probes()
    import funda_agent_exp as m
    from langchain_core.messages import HumanMessage
    drill_probe(m)

    m._SCOPE.update({"client_id": client, "organization_id": org, "survey_id": survey})
    m.PARKED_RESULT_IDS.clear()

    t_inv = time.perf_counter()
    inventory = m.survey_inventory(survey)
    inventory_s = time.perf_counter() - t_inv

    state = {"messages": [HumanMessage(
                 content=m.scoped_query(prompt, client, org, survey) + inventory)],
             "number_of_steps": 0, "last_sql_observations": {},
             "zero_row_streak": 0}

    err, final = None, ""
    t0 = time.perf_counter()
    try:
        res = m.build_graph().invoke(state, config={"recursion_limit": 2 * m.MAX_LLM_STEPS + 2})
        final = m.message_to_text(res["messages"][-1])
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
    wall = time.perf_counter() - t0

    tot = lambda f, d=None: sum(c.get(f) or 0 for c in LLM_CALLS  # noqa: E731
                                if d is None or bool(c.get("in_drill")) == d)
    rec = {"case": a.case, "arm": a.arm, "prompt": prompt, "survey_id": survey,
           # ENABLE_DIGEST is what selects the condenser, so a record that omits it cannot say
           # which arm produced it.
           "limits": {n: getattr(m, n) for n in
                      ("TOOL_CHAR_LIMIT", "SMALL_RESULT_ROWS", "SMALL_RESULT_CHARS",
                       "CANDIDATE_LIST_CHARS", "ENABLE_DIGEST")},
           "inventory_chars": len(inventory), "inventory_s": round(inventory_s, 2),
           "wall_s": round(wall, 2), "error": err, "llm_turns": len(LLM_CALLS),
           "failed_llm_calls": sum(1 for c in LLM_CALLS if c.get("error")),
           "tokens": {"input": tot("input_tokens"), "cached_input": tot("cached_input_tokens"),
                      "output": tot("output_tokens"), "reasoning": tot("reasoning_tokens")},
           "drill": {"fired": len(DRILL_RUNS),
                     "llm_turns": sum(d["llm_turns"] for d in DRILL_RUNS),
                     "latency_s": round(sum(d["latency_s"] for d in DRILL_RUNS), 2),
                     "fell_back": sum(1 for d in DRILL_RUNS if d["fell_back_to_manifest"]),
                     "input_tokens": tot("input_tokens", True),
                     "output_tokens": tot("output_tokens", True),
                     "detail": DRILL_RUNS},
           "sql_calls": sum(1 for t in TOOL_CALLS if t["tool"] == "nl2sql_tool"),
           "sql_errors": sum(1 for t in TOOL_CALLS if t.get("error")),
           "parked": sum(1 for t in TOOL_CALLS if t.get("parked")),
           "max_tool_out_chars": max([t["out_chars"] for t in TOOL_CALLS], default=0),
           "tools": [t["tool"] for t in TOOL_CALLS],
           "final_answer": final, "llm_call_detail": LLM_CALLS, "tool_call_detail": TOOL_CALLS}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n##### {a.case} [{a.arm}] wall={rec['wall_s']}s turns={rec['llm_turns']} "
          f"sql={rec['sql_calls']} parked={rec['parked']} drill={rec['drill']['fired']} "
          f"maxout={rec['max_tool_out_chars']} in={rec['tokens']['input']} "
          f"out={rec['tokens']['output']} failed_calls={rec['failed_llm_calls']} "
          f"err={bool(err)}")


if __name__ == "__main__":
    main()
