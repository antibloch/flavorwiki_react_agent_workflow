"""Does serving funda_agent_exp over the API change how the agent runs?

The wrapper makes exactly one change to the agent's execution: `streaming=True` on the two
ChatOpenAI instances, so LangChain fetches over the streaming endpoint and emits token
callbacks. Everything else -- prompts, routing, tools, thresholds -- is untouched.

That one change is not obviously free. It swaps a single non-streaming HTTP response for an SSE
stream of deltas, and the things that matter for this agent are whether it damages
  * prompt caching (cached_input is where this agent's economics live),
  * turn count / routing,
  * wall clock.

Arms alternate off/on so prompt-cache warmth and service drift land on both equally.
Run in-process, not through the server, so the tunnel and HTTP are not in the measurement.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SCOPE = ("c0b3b212-bc35-45ef-b69d-8257d3735a90",
         "46671273-0b12-49a5-b9de-60f81d192818",
         "39af3240-42a8-4e35-8c7d-c61703d5ce3f")

QUESTIONS = {
    # answered straight from the pre-fetched inventory: isolates a single generation
    "simple": ("Name the products tested, one per line.", False),
    # forces SQL discovery, so several turns and a tool loop
    "multiturn": ("Which product scored highest on Aroma, and what was its mean?", True),
}


def run_once(agent, question: str, no_inventory: bool, streaming: bool) -> dict:
    from langchain_core.messages import HumanMessage
    client, org, survey = SCOPE
    agent.llm.streaming = streaming
    agent._SCOPE.clear()
    agent._SCOPE.update({"client_id": client, "organization_id": org, "survey_id": survey})
    agent.PARKED_RESULT_IDS.clear()
    agent.TURN_LOG.clear()

    text = agent.scoped_query(question, client, org, survey)
    if not no_inventory:
        text += agent.survey_inventory(survey)

    graph = agent.build_graph()
    t0 = time.perf_counter()
    res = graph.invoke(
        {"messages": [HumanMessage(content=text)], "number_of_steps": 0,
         "last_sql_observations": {}, "zero_row_streak": 0},
        config={"recursion_limit": 2 * agent.MAX_LLM_STEPS + 2})
    wall = time.perf_counter() - t0
    turns = list(agent.TURN_LOG)
    return {
        "streaming": streaming, "wall_s": round(wall, 2), "turns": len(turns),
        "input": sum(t.get("input_tokens") or 0 for t in turns),
        "cached": sum(t.get("cached_input_tokens") or 0 for t in turns),
        "output": sum(t.get("output_tokens") or 0 for t in turns),
        "reasoning": sum(t.get("reasoning_tokens") or 0 for t in turns),
        "models": [t.get("model") for t in turns],
        "answer": agent.message_to_text(res["messages"][-1])[:200],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="simple", choices=sorted(QUESTIONS))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    import funda_agent_exp as agent
    question, no_inv = QUESTIONS[a.case]

    recs = []
    for i in range(a.reps):
        # alternate which arm goes first, so neither systematically inherits a warm cache
        order = (False, True) if i % 2 == 0 else (True, False)
        for streaming in order:
            r = run_once(agent, question, no_inv, streaming)
            r["rep"] = i
            recs.append(r)
            print(f"  rep{i} streaming={str(streaming):5} wall={r['wall_s']:6.2f}s "
                  f"turns={r['turns']} in={r['input']:6} cached={r['cached']:6} "
                  f"out={r['output']:4} reasoning={r['reasoning']:4} models={r['models']}")
    agent.llm.streaming = False

    print(f"\n=== {a.case} ===")
    hdr = f"{'arm':10} {'n':>2} {'wall med':>9} {'turns':>7} {'input med':>10} " \
          f"{'cached med':>11} {'cache %':>8} {'output med':>11}"
    print(hdr)
    for streaming in (False, True):
        g = [r for r in recs if r["streaming"] is streaming]
        med = lambda k: statistics.median([r[k] for r in g])  # noqa: E731
        pct = 100 * med("cached") / med("input") if med("input") else 0
        print(f"{'streaming ' + ('ON' if streaming else 'OFF'):10} {len(g):>2} "
              f"{med('wall_s'):8.2f}s {str(sorted({r['turns'] for r in g})):>7} "
              f"{med('input'):10.0f} {med('cached'):11.0f} {pct:7.1f}% {med('output'):11.0f}")

    off = [r for r in recs if not r["streaming"]]
    on = [r for r in recs if r["streaming"]]
    dw = statistics.median([r["wall_s"] for r in on]) - statistics.median([r["wall_s"] for r in off])
    print(f"\nwall delta (ON - OFF): {dw:+.2f}s")
    allw = [r["wall_s"] for r in recs]
    print(f"noise floor across all runs: {min(allw):.2f}-{max(allw):.2f}s "
          f"({max(allw)/max(min(allw), .01):.1f}x)")
    print(f"turn counts identical: "
          f"{sorted({r['turns'] for r in off}) == sorted({r['turns'] for r in on})}")
    if a.out:
        Path(a.out).write_text(json.dumps(recs, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
