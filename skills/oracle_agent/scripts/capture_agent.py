#!/usr/bin/env python3
"""Capture an AGENT-UNDER-TEST probe run with a PROTOCOL.md §6 manifest header.

**This runs `funda_agent_exp.py`, not the oracle.** It lives among the oracle's scripts only because
the evaluation harness is kept together; what it captures is the thing being judged, not an
oracle product.

It changes nothing about the run. It publishes the scope, computes the same turn-0 composition
`main()` will compute, prints the manifest, and then calls `funda_agent_exp.main()` with the
arguments you gave — the real CLI entrypoint, not a reimplementation of it. The inventory is
fetched once here and once by `main()`; the second is a cache hit
(`[analysis-packet] cache hit` in the log), which is itself evidence the two agree.

Why this exists: the first `trace_compare` calibration could not bind the agent trace to a
revision, and could not confirm `--no-inventory` was in force, because the agent's own log
carries no header. `funda_agent_exp.py` is not edited to fix that — it is agent code under
§4's sync contract, and the capture harness has no business in it.

    .venv/bin/python .claude/skills/oracle_agent/scripts/capture_agent.py \
      --survey-id ... --client-id ... --org-id ... \
      --thread-id smoke2 --prompt "..." 2>&1 | tee /tmp/agent-smoke2.log
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import probe_manifest  # noqa: E402

import funda_agent_exp as agent  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run funda_agent_exp.py's CLI with a PROTOCOL.md §6 manifest header."
    )
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--survey-id", required=True)
    parser.add_argument("--client-id", default="")
    parser.add_argument("--org-id", default="")
    parser.add_argument("--thread-id", default="probe")
    parser.add_argument("--no-inventory", action="store_true")
    args = parser.parse_args()

    if args.thread_id.startswith("oracle"):
        parser.error("--thread-id must NOT start with 'oracle' — that prefix is reserved for "
                     "oracle-side runs, and the two must stay separable in "
                     "conversation.sqlite3 and in a graded packet")

    # Publish scope before composing turn 0, exactly as main() does, so the inventory this
    # measures is the inventory main() will attach.
    agent._SCOPE.clear()
    agent._SCOPE.update({
        "client_id": args.client_id,
        "organization_id": args.org_id,
        "survey_id": args.survey_id,
    })
    question = agent.scoped_query(
        args.prompt, args.client_id, args.org_id, args.survey_id
    )
    inventory = "" if args.no_inventory else agent.survey_inventory(args.survey_id)

    probe_manifest.emit(
        "AGENT PROBE RUN",
        repo_root=REPO_ROOT,
        argv=sys.argv,
        prompt=args.prompt,
        survey_id=args.survey_id,
        client_id=args.client_id,
        org_id=args.org_id,
        thread_id=args.thread_id,
        tool_names=[t.name for t in agent.build_tools()],
        excluded_tool=None,
        model=agent.MODEL_NAME,
        max_llm_steps=agent.MAX_LLM_STEPS,
        log_call_chars=agent.LOG_TOOL_CALL_CHARS,
        log_output_chars=agent.LOG_TOOL_OUTPUT_CHARS,
        question_chars=len(question) + len(inventory),
        inventory_chars=len(inventory),
        policy="build_system_prompt() — the production agent prompt",
    )

    # Hand over to the real entrypoint. Anything this script got wrong about the composition
    # above is cosmetic; what actually runs is main(), unmodified.
    forwarded = [
        "funda_agent_exp.py",
        "--prompt", args.prompt,
        "--survey-id", args.survey_id,
        "--client-id", args.client_id,
        "--org-id", args.org_id,
        "--thread-id", args.thread_id,
    ]
    if args.no_inventory:
        forwarded.append("--no-inventory")
    sys.argv = forwarded
    agent.main()
    print("\n===== AGENT PROBE RUN COMPLETE =====")


if __name__ == "__main__":
    main()
