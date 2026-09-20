#!/usr/bin/env python3
"""PLSR for the oracle — the SAME tool the agent uses, exposed to the Claude Code harness.

Unlike the oracle's other counterparts, this one does not reimplement anything. It invokes
`funda_agent_exp.analyze_plsr` directly, with the same arguments the agent's model would pass,
and prints the same payload the agent's model would see. The two runs therefore reach
identical PLSR information, which is the point: PLSR is a deterministic analysis with a fixed
payload contract, and the oracle's job here is to *have* that analysis, not to second-guess how
it is fetched.

**What that costs, stated plainly.** The PLSR path is now shared end to end — data assembly,
`plsr.py` kernel, payload. So neither the trajectory nor the content comparison can detect a
defect inside it: both runs would be wrong together and agree. The independent check for PLSR
is the one `PROTOCOL.md` §5 already prescribes — regenerate against scikit-learn in a throwaway
venv and compare to `tests/plsr_sklearn_fixture.json`. Say so in the answer whenever a PLSR
figure is load-bearing.

`_SCOPE` is published before the call because `analyze_plsr` reaches the database through the
agent's own scope-bound path; without it the tool refuses.

    plsr_ref.py --survey <uuid> --kpi <uuid> --attributes <uuid,uuid,...> \\
                [--cv loo|kfold|none] [--folds N] [--components N]
                [--normalization reference|zscore_once|pls_internal|none] [--why "..."]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import trace_log  # noqa: E402
import funda_agent_exp as agent  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run funda_agent_exp.analyze_plsr — the agent's own PLSR tool.")
    ap.add_argument("--survey", required=True, help="survey UUID (publishes the tool's scope)")
    ap.add_argument("--client", default="", help="client UUID")
    ap.add_argument("--org", default="", help="organization UUID")
    ap.add_argument("--kpi", required=True, help="target question UUID")
    ap.add_argument("--attributes", required=True,
                    help="comma-separated predictor question UUIDs")
    ap.add_argument("--cv", choices=["loo", "kfold", "none"], default="loo",
                    help="the tool's required cv_method choice")
    ap.add_argument("--folds", type=int, default=None, help="cv_folds when --cv kfold")
    ap.add_argument("--components", type=int, default=None,
                    help="n_components; omit to let the tool default")
    ap.add_argument("--normalization",
                    choices=["reference", "zscore_once", "pls_internal", "none"],
                    default="reference", help="the tool's required normalization choice")
    ap.add_argument("--why", default="", help="why this call was made (recorded in the trace)")
    args = ap.parse_args()

    # The tool binds ids from the process-global scope, exactly as it does for the agent.
    agent._SCOPE.clear()
    agent._SCOPE.update({
        "client_id": args.client,
        "organization_id": args.org,
        "survey_id": args.survey,
    })

    attrs = [a.strip() for a in args.attributes.split(",") if a.strip()]
    payload = {
        "kpi": {"question_id": args.kpi.strip(), "component_label": ""},
        "attributes": [{"question_id": a, "component_label": ""} for a in attrs],
        "normalization": args.normalization,
        "cv_method": args.cv,
    }
    if args.folds is not None:
        payload["cv_folds"] = args.folds
    if args.components is not None:
        payload["n_components"] = args.components

    try:
        result = agent.analyze_plsr.invoke(payload)
    except Exception as exc:  # noqa: BLE001 - reported to the analyst and to the trace
        trace_log.log("plsr_analysis", "plsr_ref.py", purpose=args.why,
                      args={"kpi": args.kpi, "n_attributes": len(attrs), "cv": args.cv},
                      outcome="error", error=str(exc)[:300])
        print(f"PLSR ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    text = result if isinstance(result, str) else str(result)
    # The tool reports its own refusals as ordinary text rather than raising; treat those as
    # failed steps so the trajectory log shows what the agent's model would also have seen.
    failed = text.lstrip().startswith(("PLSR unavailable", "PLSR refused"))
    trace_log.log("plsr_analysis", "plsr_ref.py", purpose=args.why,
                  args={"kpi": args.kpi.strip(), "n_attributes": len(attrs),
                        "cv": args.cv, "normalization": args.normalization,
                        "components": args.components},
                  outcome="error" if failed else "ok",
                  detail="" if failed else f"{len(attrs)} attribute(s), cv={args.cv}",
                  error=text.splitlines()[0][:200] if failed else None)

    print("Tool: funda_agent_exp.analyze_plsr (shared with the agent — identical path)")
    print(text)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
