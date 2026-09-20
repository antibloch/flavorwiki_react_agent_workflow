#!/usr/bin/env python3
"""The manifest header both probe captures emit, from one implementation.

`PROTOCOL.md` §6 step 3 requires every retained trace to be bound to the revision it ran on.
`trace_compare` reads both traces and compares their headers, so the two must have the same
shape — which is why this lives in one module instead of being written twice. A field that
exists on one side and not the other is a field the grader cannot compare, and that is exactly
the gap the first calibration run found on the agent side.

Not imported by the agent. Capture-harness only, local-only (§2, §4).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

# Hashed into every manifest: the agent modules whose content can change a run's behaviour.
TRACKED_SOURCES = ("funda_agent_exp.py", "agent_instructions.py", "tool_prompts.py")


def sha256_short(path: Path) -> str:
    """First 16 hex characters of a file's SHA-256, or `unavailable` if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unavailable"


def emit(
    kind: str,
    *,
    repo_root: Path,
    argv: list[str],
    prompt: str,
    survey_id: str,
    client_id: str,
    org_id: str,
    thread_id: str,
    tool_names: list[str],
    excluded_tool: str | None,
    model: str,
    max_llm_steps: int,
    log_call_chars: int,
    log_output_chars: int,
    question_chars: int,
    inventory_chars: int,
    policy: str,
    extra_sources: tuple[str, ...] = (),
) -> None:
    """Print the manifest for one probe capture. Identical field order on both sides."""
    print(f"===== {kind} =====")
    print(f"run_locator        : thread={thread_id} started={time.strftime('%Y-%m-%dT%H:%M:%S')}")
    print(f"command            : {' '.join(argv)}")
    print(f"prompt             : {prompt}")
    print(f"scope              : survey={survey_id} client={client_id or '(none)'} "
          f"org={org_id or '(none)'}")
    print(f"tools bound        : {', '.join(tool_names)}")
    print(f"tool excluded      : {excluded_tool or '(none)'}")
    print(f"model              : {model}  max_llm_steps={max_llm_steps}")
    print(f"log budget         : call={log_call_chars} output={log_output_chars}")
    # inventory=0 is the only positive evidence that --no-inventory was actually in force;
    # asserting it in a case record is not evidence, which is what the first calibration said.
    print(f"turn-0 chars       : question={question_chars} inventory={inventory_chars}")
    for name in TRACKED_SOURCES + extra_sources:
        print(f"sha256 {name:<22}: {sha256_short(repo_root / name)}")
    print(f"policy             : {policy}")
