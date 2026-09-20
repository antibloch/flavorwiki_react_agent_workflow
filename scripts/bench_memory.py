"""Cost of the short-term-memory patch, end to end.

Three modes:

  single    one question, one process -- the A/B against the pre-memory build
            (funda_agent_exp_oracle_duo.py). The patched build is driven exactly as main()
            drives it, checkpointer and thread_id included, so the comparison is of the
            shipped configuration and not of a stripped-down one.

  rounds    several questions on ONE thread, per-round wall and tokens. This is the thing
            memory is for, and the thing that has to stay bounded as the thread grows.

  nomemory  the same questions as independent runs, each with a fresh state and a fresh
            scoped_query + inventory preamble. This is what a user without memory has to do
            to ask a follow-up, and it is the honest baseline for what memory costs or saves.

Counting is class-level (ChatOpenAI.invoke / BaseTool.invoke), identical to bench_needle.py,
so both builds are measured the same way regardless of their internal structure.
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Gpiexperience: 3 products, small survey, short runs -- keeps the LLM-noise floor low.
SCOPE = ("c0b3b212-bc35-45ef-b69d-8257d3735a90",
         "46671273-0b12-49a5-b9de-60f81d192818",
         "39af3240-42a8-4e35-8c7d-c61703d5ce3f")

# Round 1 needs real work. The follow-ups are deliberately answerable from the thread, which
# is the whole point: without memory they are unanswerable, with memory they should be cheap.
CONVERSATION = [
    "What are the products tested in this survey? Just list their names.",
    "Now give me the mean Aroma score for each of those products.",
    "Which product had the highest Aroma mean, per what you just said?",
    "And what was its standard deviation?",
    "Remind me which products you listed at the very start, in the same order.",
    "Of the products you listed, which had the lowest Aroma mean?",
    # Rounds 7+ exist to push the thread past HISTORY_MAX_MESSAGES+1, which is the only place
    # the trim actually engages. Below that the window is the whole history and nothing is
    # being tested; the input-token plateau across these rounds is the bound working.
    "What was the Texture mean for that same product?",
    "How many respondents answered the Appearance question?",
    "Between Aroma and Texture, which had the wider spread across products?",
    "Name the product with the highest Texture mean.",
    "Was that the same product that led on Aroma?",
    "Summarise, in one line each, every figure you have given me so far.",
]

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
            # messages actually sent -- the direct measure of whether trimming is bounding
            # the request, independent of how the model happens to tokenise it
            "sent_messages": len(inp) if isinstance(inp, list) else None,
            "input_tokens": u.get("input_tokens"),
            "cached_input_tokens": (u.get("input_token_details") or {}).get("cache_read", 0),
            "output_tokens": u.get("output_tokens"),
            "reasoning_tokens": (u.get("output_token_details") or {}).get("reasoning", 0)})
        return r
    ChatOpenAI.invoke = li

    ot = BaseTool.invoke

    def ti(self, inp, config=None, **kw):
        t0 = time.perf_counter()
        out = ot(self, inp, config, **kw)
        TOOL_CALLS.append({"tool": self.name, "latency_s": round(time.perf_counter() - t0, 2)})
        return out
    BaseTool.invoke = ti


def totals(calls):
    f = lambda k: sum(c.get(k) or 0 for c in calls)  # noqa: E731
    return {"input": f("input_tokens"), "cached_input": f("cached_input_tokens"),
            "output": f("output_tokens"), "reasoning": f("reasoning_tokens"),
            "llm_s": round(sum(c["latency_s"] for c in calls), 2)}


def fresh_scratch():
    return {"number_of_steps": 0, "last_sql_observations": {},
            "zero_row_streak": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="funda_agent_exp")
    ap.add_argument("--mode", required=True, choices=("single", "rounds", "nomemory"))
    ap.add_argument("--rounds", type=int, default=len(CONVERSATION))
    ap.add_argument("--rep", type=int, default=0, help="repetition index, recorded only")
    # A 1-turn question never revisits call_model, so it cannot show per-turn overhead. Dropping
    # the inventory forces the agent to discover the survey by SQL, which is the reliable way to
    # get a multi-turn loop without inventing a question that may or may not trigger one.
    ap.add_argument("--no-inventory", action="store_true")
    ap.add_argument("--order", default="", help="recorded only: which build ran first in the pair")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    client, org, survey = SCOPE

    probes()
    t_import = time.perf_counter()
    m = __import__(a.agent)
    import_s = time.perf_counter() - t_import
    from langchain_core.messages import HumanMessage

    m._SCOPE.update({"client_id": client, "organization_id": org, "survey_id": survey})
    m.PARKED_RESULT_IDS.clear()

    # The patched build is driven the way main() drives it. The pre-memory build has no
    # checkpointer parameter at all, which is itself the no-regression check on build_graph().
    has_memory = "checkpointer" in getattr(m.build_graph, "__code__").co_varnames
    if has_memory:
        from langgraph.checkpoint.memory import InMemorySaver
        graph = m.build_graph(InMemorySaver())
        cfg = {"recursion_limit": 2 * m.MAX_LLM_STEPS + 2,
               "configurable": {"thread_id": f"bench{a.rep}"}}
    else:
        graph = m.build_graph()
        cfg = {"recursion_limit": 2 * m.MAX_LLM_STEPS + 2}

    t_inv = time.perf_counter()
    inventory = "" if a.no_inventory else m.survey_inventory(survey)
    inventory_s = time.perf_counter() - t_inv

    questions = CONVERSATION[:1] if a.mode == "single" else CONVERSATION[:a.rounds]
    rounds, err = [], None
    t_all = time.perf_counter()
    try:
        for i, q in enumerate(questions):
            # With memory, only round 1 carries the preamble -- it stays in message 0. Without
            # it, every question must re-state scope and inventory or it cannot be answered.
            if i == 0 or a.mode == "nomemory":
                text = m.scoped_query(q, client, org, survey) + inventory
            else:
                text = q
            before = len(LLM_CALLS)
            t0 = time.perf_counter()
            state = {"messages": [HumanMessage(content=text)], **fresh_scratch()}
            res = graph.invoke(state, config=cfg)
            wall = time.perf_counter() - t0
            turns = LLM_CALLS[before:]
            rounds.append({
                "round": i + 1, "question": q, "wall_s": round(wall, 2),
                "llm_turns": len(turns), "tokens": totals(turns),
                "sent_messages_per_turn": [t["sent_messages"] for t in turns],
                "state_messages_after": len(res["messages"]),
                "sql_calls": sum(1 for t in TOOL_CALLS if t["tool"] == "nl2sql_tool"),
                "answer": m.message_to_text(res["messages"][-1])[:600]})
            if a.mode == "nomemory":  # each question is its own run: no thread to resume
                m.PARKED_RESULT_IDS.clear()
                if has_memory:
                    cfg["configurable"]["thread_id"] = f"bench{a.rep}_{i + 1}"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"

    rec = {"agent": a.agent, "mode": a.mode, "rep": a.rep, "order": a.order,
           "no_inventory": a.no_inventory, "has_memory_param": has_memory,
           "history_max_messages": getattr(m, "HISTORY_MAX_MESSAGES", None),
           "import_s": round(import_s, 2), "inventory_s": round(inventory_s, 2),
           "inventory_chars": len(inventory),
           "wall_s": round(time.perf_counter() - t_all, 2),
           "error": err, "totals": totals(LLM_CALLS), "llm_turns": len(LLM_CALLS),
           "rounds": rounds, "llm_call_detail": LLM_CALLS}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    t = rec["totals"]
    print(f"\n##### {a.agent} [{a.mode} rep{a.rep}] wall={rec['wall_s']}s "
          f"turns={rec['llm_turns']} in={t['input']} cached={t['cached_input']} "
          f"out={t['output']} llm_s={t['llm_s']} err={bool(err)}")


if __name__ == "__main__":
    main()
