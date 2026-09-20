"""Check the {{suggestion}} contract the UI parses: does it appear when it should, is it
well-formed, and does it stay away when the request is unambiguous.

    python scripts/check_suggestions.py

Validates the rules the client depends on, so a malformed button is caught here rather than
rendering broken in the UI.
"""
import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

HERBALIFE = ("37cbf852-a2b7-4f8b-96b4-7f67432f88cd", "c75d846a-e265-4f69-92c1-91308e0697f6",
             "6263cf71-23b7-4462-9ccf-4a00a7267672")

SUGGESTION = re.compile(r"\{\{(.*?)\}\}", re.S)

CASES = {
    # Materially underspecified -> name the exact gap softly and offer valid choices.
    "ambiguous": {"prompt": "Run stats", "expect_suggestions": True,
                  "expect_soft_gap": ("measure", "test")},
    # Safe ambiguity -> assume a DB-supported meaning, answer it, then offer other readings.
    "unclear_performance": {"prompt": "Which product performed best?",
                            "expect_suggestions": True,
                            "expect_soft_gap": ("measure", "best")},
    # Names the measure, so it runs the test. Whether a real choice remains depends on the
    # RESULT, not on the prompt: a delivered Tukey verdict settles the question and correctly
    # takes no buttons, while a tool that could not run leaves a genuine retry-or-other-measure
    # choice. Pinning either state here made the case flip with the Charts API's data, so the
    # expectation is read back from the tool output instead.
    "specific": {"prompt": "Run ANOVA and Tukey on overall liking and tell me which "
                           "products differ.",
                 "expect_suggestions": "if_stats_had_no_data"},
    # A complete factual answer decides nothing further -> no buttons.
    "factual": {"prompt": "Name the products tested, one per line.",
                "expect_suggestions": False},
    # Also complete: sorting from the inventory needs no follow-up choice.
    "factual2": {"prompt": "Sort the products by overall liking mean, figures only.",
                 "expect_suggestions": False},
}


def validate(answer: str) -> list[tuple[str, bool, str]]:
    """The format rules the client's parser relies on."""
    out = []
    found = SUGGESTION.findall(answer)
    n_open, n_close = answer.count("{{"), answer.count("}}")
    out.append(("braces balanced", n_open == n_close, f"{n_open} open / {n_close} close"))
    if not found:
        looks_like_menu = bool(
            re.search(r"(?im)^\s*(?:\d+[.)]|[-*])\s+.+(?:influenc|driver)", answer)
        )
        out.append((
            "choice menu uses braces", not looks_like_menu,
            "unwrapped numbered/bulleted influence choices" if looks_like_menu else "clean",
        ))
        return out
    out.append(("count is 1-5", 1 <= len(found) <= 5, f"{len(found)} found"))
    out.append(("no newline inside", all("\n" not in s for s in found),
                "; ".join(s[:40] for s in found if "\n" in s) or "clean"))
    out.append(("no nested braces", all("{" not in s and "}" not in s for s in found),
                "clean" if all("{" not in s for s in found) else "nested"))
    out.append(("no qid leaked into a button",
                not any(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", s) for s in found), "clean"))
    out.append(("not placeholder text",
                not any(re.fullmatch(r"(?i)\s*(yes|no|option\s*\d+)\s*", s) for s in found),
                "clean"))

    # Self-containment: clicking a button posts this text back as the WHOLE next question, so
    # each one needs an action and a subject and must read cold with the menu gone.
    ACTIONS = (r"analy[sz]e|break down|compare|correlate|determine|evaluate|find|rank|report|"
               r"rerun|retry|run|show|summari[sz]e|test|what|which|who")
    no_action = [s for s in found if not re.search(ACTIONS, s, re.I)]
    out.append(("names an action", not no_action, "; ".join(no_action[:2]) or "clean"))

    # A measure has to be identified too -- "Run ANOVA" alone lands back on the same ambiguity.
    too_short = [s for s in found if len(s.split()) < 4]
    out.append(("names a measure too (>=4 words)", not too_short,
                "; ".join(too_short[:2]) or "clean"))

    # Dangling references: fine mid-conversation, useless as a standalone prompt.
    DANGLING = r"(?i)^\s*(retry|run it|do it|that one|the same|the first|this one|again)\s*$"
    dangling = [s for s in found if re.match(DANGLING, s)]
    out.append(("no dangling reference", not dangling,
                "; ".join(dangling[:2]) or "clean"))

    # Every suggestion must be distinguishable from the others, or two buttons do the same thing.
    lowered = [s.strip().lower() for s in found]
    out.append(("suggestions are distinct", len(set(lowered)) == len(lowered),
                f"{len(set(lowered))} unique of {len(lowered)}"))
    # every suggestion on its own line, and all of them at the end of the answer
    lines = [ln.strip() for ln in answer.strip().splitlines() if ln.strip()]
    sug_lines = [i for i, ln in enumerate(lines) if SUGGESTION.search(ln)]
    own_line = all(re.fullmatch(r"[-*\s]*\{\{.*?\}\}[\s.]*", lines[i]) for i in sug_lines)
    out.append(("each on its own line", own_line,
                "" if own_line else repr(lines[sug_lines[0]][:70])))
    contiguous_tail = bool(sug_lines) and sug_lines == list(
        range(len(lines) - len(sug_lines), len(lines)))
    out.append(("all at the very end", contiguous_tail,
                f"lines {sug_lines} of {len(lines)}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="all", choices=["all", *CASES])
    a = ap.parse_args()

    import funda_agent_exp as agent
    from langchain_core.messages import HumanMessage
    failed = False

    for name in (list(CASES) if a.case == "all" else [a.case]):
        case = CASES[name]
        client, org, survey = case.get("scope", HERBALIFE)
        agent._SCOPE.clear()
        agent._SCOPE.update({"client_id": client, "organization_id": org, "survey_id": survey})
        text = agent.scoped_query(case["prompt"], client, org, survey) \
            + agent.survey_inventory(survey)
        res = agent.build_graph().invoke(
            {"messages": [HumanMessage(content=text)], "number_of_steps": 0,
             "last_sql_observations": {}, "zero_row_streak": 0},
            config={"recursion_limit": 2 * agent.MAX_LLM_STEPS + 2})
        answer = agent.message_to_text(res["messages"][-1])
        found = SUGGESTION.findall(answer)

        expect = case["expect_suggestions"]
        if expect == "if_stats_had_no_data":
            tool_text = "\n".join(agent.message_to_text(m) for m in res["messages"]
                                  if getattr(m, "type", "") == "tool")
            expect = "Tukey grouping" not in tool_text

        print(f"\n{'=' * 76}\n{name}: {case['prompt'][:60]}\n{'=' * 76}")
        print(f"  suggestions expected={expect} found={len(found)}")
        for s in found:
            print(f"    -> {s!r}")

        ok = bool(found) == expect
        print(f"  [{'PASS' if ok else 'FAIL'}] presence matches expectation")
        failed |= not ok

        if gap_terms := case.get("expect_soft_gap"):
            has_bridge = bool(re.search(r"\byou (?:haven't|have not) specified\b", answer, re.I))
            names_gap = any(re.search(rf"\b{re.escape(term)}\b", answer, re.I)
                            for term in gap_terms)
            good = has_bridge and names_gap
            opening = answer.strip().splitlines()[0][:160] if answer.strip() else "<empty>"
            note = ("soft bridge names the open detail" if good
                    else f"missing soft exact-gap bridge; opening={opening!r}")
            print(f"  [{'PASS' if good else 'FAIL'}] states the exact missing information  {note}")
            failed |= not good

        # No internal identifier may reach the reader, in a button or in the prose around it.
        # UUIDs are meaningless to a survey analyst and were never asked for.
        uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                           answer, re.I)
        print(f"  [{'PASS' if not uuids else 'FAIL'}] no uuid anywhere in the answer  "
              f"{'clean' if not uuids else f'{len(uuids)} found: ' + uuids[0]}")
        failed |= bool(uuids)
        if expect:
            for label, good, note in validate(answer):
                print(f"  [{'PASS' if good else 'FAIL'}] {label}  {note}")
                failed |= not good
        elif found:
            print("  (unexpected suggestions above)")

    print(f"\n{'SOME CHECKS FAILED' if failed else 'All suggestion-format checks passed.'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
