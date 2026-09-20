"""Does short-term memory stay faithful when a condenser fires mid-conversation?

Both condensers rewrite what the main loop sees, so the question is whether they disturb the
two things memory depends on:

  1. MESSAGE SHAPE. _trim_history finds the current round by scanning back for the last
     HumanMessage, because only round openers are HumanMessages in this state. If a condenser
     injected one, that assumption -- and with it the "never cut into the round in progress"
     clamp -- would silently break.

  2. THE DRILL'S QUESTION. call_tool passes _last_user_query(state["messages"]) into the drill,
     and the drill's context is isolated by design: question + the SQL author's notes + digest,
     no main-loop history. In round 1 the last human message is the whole scoped preamble. In a
     follow-up round it is the bare follow-up, which may be elliptical ("and its SD?").

TOOL_CHAR_LIMIT is forced low so ordinary results park and the condenser runs every round.
ENABLE_DIGEST selects which condenser is under test; both are run by the shell driver.
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SCOPE = ("c0b3b212-bc35-45ef-b69d-8257d3735a90",
         "46671273-0b12-49a5-b9de-60f81d192818",
         "39af3240-42a8-4e35-8c7d-c61703d5ce3f")

# Round 1 is self-contained. Rounds 2 and 3 are deliberately elliptical -- "those products",
# "that one" -- which the main loop can resolve from history and an isolated drill cannot.
CONVERSATION = [
    "List every individual respondent's Aroma score for each product, respondent by "
    "respondent. I want the row-level detail, not averages.",
    "Now do the same for Texture, for those same products.",
    "And for that one, which product came out highest?",
]

DRILL_CALLS = []


def instrument(m):
    """Record what each condenser is actually asked, without changing what it returns."""
    original = m.run_drill

    def probed(**kw):
        t0 = time.perf_counter()
        out = original(**kw)
        DRILL_CALLS.append({
            "user_query_seen_by_drill": kw.get("user_query", "")[:400],
            "user_query_chars": len(kw.get("user_query", "")),
            "sql_query": (kw.get("sql_query") or "")[:200],
            "latency_s": round(time.perf_counter() - t0, 2),
            "returned_chars": len(out) if isinstance(out, str) else None,
            "returned_type": type(out).__name__,
            "fell_back_to_manifest": isinstance(out, str) and "RESULT STORED" in out,
            "returned_head": (out if isinstance(out, str) else str(out))[:300]})
        return out
    m.run_drill = probed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    client, org, survey = SCOPE

    import funda_agent_exp as m
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    instrument(m)

    m._SCOPE.update({"client_id": client, "organization_id": org, "survey_id": survey})
    m.PARKED_RESULT_IDS.clear()
    graph = m.build_graph(InMemorySaver())
    cfg = {"recursion_limit": 2 * m.MAX_LLM_STEPS + 2,
           "configurable": {"thread_id": "drillmem"}}
    inventory = m.survey_inventory(survey)

    rounds = []
    for i, q in enumerate(CONVERSATION):
        text = (m.scoped_query(q, client, org, survey) + inventory) if i == 0 else q
        before_drills = len(DRILL_CALLS)
        t0 = time.perf_counter()
        res = graph.invoke({"messages": [HumanMessage(content=text)],
                            "number_of_steps": 0, "last_sql_observations": {},
                            "zero_row_streak": 0},
                           config=cfg)
        msgs = list(res["messages"])

        # The two invariants, checked against the live state rather than argued from the code.
        kinds = [x.type for x in msgs]
        human_idx = [j for j, x in enumerate(msgs) if x.type == "human"]
        window = m._trim_history(msgs)
        round_start = max(human_idx)
        current_round = msgs[round_start:]
        rounds.append({
            "round": i + 1, "question": q, "wall_s": round(time.perf_counter() - t0, 2),
            "drills_this_round": len(DRILL_CALLS) - before_drills,
            "message_kinds": kinds,
            "human_message_indices": human_idx,
            "n_human_messages": len(human_idx),
            "expected_n_human_messages": i + 1,
            "shape_ok": len(human_idx) == i + 1,
            "window_len": len(window),
            "window_kinds": [x.type for x in window],
            # the clamp: the whole current round must survive trimming
            "current_round_intact": window[-len(current_round):] == current_round,
            "parked_ids_open": sorted(m.PARKED_RESULT_IDS),
            "answer": m.message_to_text(msgs[-1])[:500]})

    rec = {"enable_digest": m.ENABLE_DIGEST, "tool_char_limit": m.TOOL_CHAR_LIMIT,
           "history_max_messages": m.HISTORY_MAX_MESSAGES,
           "condenser": "digest_workflow" if m.ENABLE_DIGEST else "drill_agent",
           "total_drills": len(DRILL_CALLS), "drill_calls": DRILL_CALLS, "rounds": rounds}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n##### condenser={rec['condenser']} drills={rec['total_drills']} "
          f"limit={m.TOOL_CHAR_LIMIT}")
    for r in rounds:
        print(f"  r{r['round']}: drills={r['drills_this_round']} "
              f"humans={r['n_human_messages']}/{r['expected_n_human_messages']} "
              f"shape_ok={r['shape_ok']} current_round_intact={r['current_round_intact']} "
              f"msgs={len(r['message_kinds'])} window={r['window_len']}")
    for i, d in enumerate(DRILL_CALLS):
        print(f"  drill{i+1} asked: {d['user_query_seen_by_drill'][:110]!r} "
              f"({d['user_query_chars']} chars)")


if __name__ == "__main__":
    main()
