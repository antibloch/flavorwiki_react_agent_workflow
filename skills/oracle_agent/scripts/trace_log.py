#!/usr/bin/env python3
"""The oracle's trajectory log — machine truth about what it retrieved, in what order.

The oracle runs in the **Claude Code harness**, not the target agent's LangGraph loop, so
there is no `print_message` transcript to diff and no post-hoc session file to harvest (the
task output files are empty). What the two runs *do* share is the set of information they can
reach. So this logs the retrieval trajectory at the level that is actually comparable across
two different harnesses: **which capability was exercised, why, with what arguments, and what
came back** — not which framework function was invoked.

Every oracle script calls `log()` on the way out. Nothing depends on the model reporting its
own behaviour, which is what makes this evidence rather than narration.

Set `ORACLE_TRACE_FILE` before a graded run; without it logging is a silent no-op, so ordinary
ad-hoc use of the scripts costs nothing and writes nothing.

    export ORACLE_TRACE_FILE=/tmp/oracle-<case>.trace.jsonl

One JSON object per line:

    {"seq": 1, "ts": "...", "capability": "sql_retrieval", "script": "query.sh",
     "purpose": "locate the gender question", "args": {...}, "outcome": "rows",
     "detail": "2 rows", "error": null}

`capability` is the normalized vocabulary below. It is the join key `trace_compare` uses to
line an oracle step up against a `funda_agent_exp.py` tool call, because `query.sh` and
`nl2sql_tool` are the same *act* performed by different machinery.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# Normalized capability vocabulary. Left: what the oracle does. Right: the funda_agent_exp.py
# tool that performs the same act, or None where the harnesses genuinely differ.
CAPABILITIES = {
    "sql_retrieval": "nl2sql_tool",
    "survey_statistics": "run_survey_stats",
    "plsr_analysis": "analyze_plsr",
    "word_cloud": "generate_word_cloud",
    "survey_inventory": "get_survey_analysis_packet",
    # Oracle-only: the agent is handed its scope, it never resolves one.
    "scope_resolution": None,
    "db_health": None,
}

_LOCK = threading.Lock()
_ENV = "ORACLE_TRACE_FILE"


def _digest(value, limit: int = 300):
    """Keep arguments readable and bounded; never let a payload bury the trajectory."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… [+{len(value) - limit}]"
    if isinstance(value, dict):
        return {k: _digest(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digest(v, limit) for v in value[:20]]
    return value


def log(capability: str, script: str, *, purpose: str = "", args=None,
        outcome: str = "ok", detail: str = "", error: str | None = None) -> None:
    """Append one trajectory step. No-op unless ORACLE_TRACE_FILE is set."""
    path = os.environ.get(_ENV)
    if not path:
        return
    if capability not in CAPABILITIES:
        raise ValueError(
            f"unknown capability {capability!r}; add it to CAPABILITIES with its "
            "funda_agent_exp.py counterpart (or None) before logging it"
        )
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capability": capability,
        "counterpart": CAPABILITIES[capability],
        "script": script,
        "purpose": purpose,
        "args": _digest(args or {}),
        "outcome": outcome,
        "detail": detail,
        "error": error,
    }
    p = Path(path)
    with _LOCK:
        # Count steps only: the manifest is seq 0 and must not shift the first step to 2.
        seq = 1
        if p.exists():
            seq += sum(1 for line in p.open(encoding="utf-8")
                       if line.strip() and '"kind": "manifest"' not in line)
        entry = {"seq": seq, **entry}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def manifest(path: str, *, prompt: str, survey_id: str, client_id: str = "",
             org_id: str = "") -> None:
    """Write the trace's header line, binding it to a revision (PROTOCOL.md §6 step 3).

    The agent side gets this from `probe_manifest.py`; without the same on the oracle side a
    grader can only infer scope from SQL literals and cannot bind the run to source hashes at
    all — which is exactly what the first paired grading reported as a gap.
    """
    import probe_manifest
    repo_root = Path(__file__).resolve().parents[4]
    entry = {
        "seq": 0,
        "kind": "manifest",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "harness": "claude-code",
        "prompt": prompt,
        "scope": {"survey_id": survey_id, "client_id": client_id, "organization_id": org_id},
        "sources": {name: probe_manifest.sha256_short(repo_root / name)
                    for name in probe_manifest.TRACKED_SOURCES},
        "capabilities": {k: v for k, v in sorted(CAPABILITIES.items())},
    }
    pth = Path(path)
    if pth.exists() and pth.read_text(encoding="utf-8").strip():
        raise SystemExit(f"{path} already has entries; write the manifest before any step")
    with pth.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def render(path: str) -> str:
    """Render a trace file as the step table `trace_compare` reads."""
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        e = json.loads(raw)
        if e.get("kind") == "manifest":
            sc = e.get("scope", {})
            lines += [
                "===== ORACLE TRAJECTORY (claude-code harness) =====",
                f"started   : {e['ts']}",
                f"prompt    : {e.get('prompt','')}",
                f"scope     : survey={sc.get('survey_id','')} client={sc.get('client_id') or '(none)'} "
                f"org={sc.get('organization_id') or '(none)'}",
                *[f"sha256 {k:<22}: {v}" for k, v in e.get("sources", {}).items()],
                "",
                "seq | capability (funda counterpart) | purpose | outcome | detail",
            ]
            continue
        if not lines:
            lines = ["seq | capability (funda counterpart) | purpose | outcome | detail"]
        cp = e.get("counterpart") or "—"
        lines.append(f"{e['seq']} | {e['capability']} ({cp}) | {e.get('purpose','')} | "
                     f"{e['outcome']} | {e.get('detail','')}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Render an oracle trace, or write its manifest.")
    ap.add_argument("path", nargs="?", help="trace file to render")
    ap.add_argument("--manifest", action="store_true", help="write the header line instead")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--survey", default="")
    ap.add_argument("--client", default="")
    ap.add_argument("--org", default="")
    a = ap.parse_args()
    if a.manifest:
        target = a.path or os.environ.get(_ENV)
        if not target:
            raise SystemExit("--manifest needs a path or ORACLE_TRACE_FILE")
        manifest(target, prompt=a.prompt, survey_id=a.survey,
                 client_id=a.client, org_id=a.org)
        print(f"manifest written to {target}")
    elif a.path:
        print(render(a.path))
    else:
        print(__doc__)
