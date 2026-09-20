# AGENTS.md — entry point for any coding agent working in this directory

**Read `PROTOCOL.md` in full before changing anything.** It is the governing document: what
each file does, how the working tree relates to `deployment/`, what must be synced and what
must never be, and the gated workflow (§6) every change follows. This file exists only so a
host that auto-loads `AGENTS.md` reaches `PROTOCOL.md` and the procedures below. It adds no
rules of its own and never overrides `PROTOCOL.md`.

## Scope

This directory — `final_agent_work_v5_base_2_optim3_exp/` — is the whole working area.
Nothing above it is in scope: do not read, edit, commit or reason about anything outside it.
The one exception `PROTOCOL.md` names is the local DB helper and `../BE/.env`, both read-only.

## Procedures

Four procedure documents live under `.claude/skills/`. They are plain Markdown and are
**host-neutral** — nothing in them requires a particular agent host. `.codex/skills/` holds
relative symlinks to the same four folders, so both Claude Code and Codex discover them
natively; there is one canonical copy and nothing to keep in sync.

Invoke a procedure by name (Codex also accepts `$name`). Whichever host you are on, **read the
named `SKILL.md` in full before acting**, and follow it literally — do not delegate reading or
interpreting it to a subagent.

| Procedure | Path | What it is for |
|---|---|---|
| `oracle_agent` | `.claude/skills/oracle_agent/SKILL.md` | **Producer, grades nothing.** Phases 0-5 derive ground truth from `gpi-db` + the Charts API; Phase 6 produces the reference trace over the agent's own tools |
| `trace_compare` | `.claude/skills/trace_compare/SKILL.md` | Judges **content** (against the ground-truth answer) and **trajectory** (against the reference trace), in two separate blocks |
| `contract_review` | `.claude/skills/contract_review/SKILL.md` | Judges a transcript's **presentation/behaviour only**, plus causal localization |
| `agent-prompt-mapper` | `.claude/skills/agent-prompt-mapper/SKILL.md` | Builds and refreshes `docs/generated/agent-prompt-map.{md,json}` |

`PROTOCOL.md` §0 says how each host supplies the capabilities these procedures need — loading
a procedure, and running a grader in a context with no memory of the fix. **The session that
made a change may never grade it** (§6 step 4). On Claude Code the grader is a subagent; on
Codex it is a separate `codex exec` process, because `codex exec` has no delegation tool and
Codex's own rules forbid handing skill instructions to a subagent. §2 says why the producer
never judges and the two judges never grade each other's area.

## Environment

- `.venv/bin/python` — never a bare `python`. No `pytest`; tests run with
  `.venv/bin/python -m unittest`.
- Local DB: Docker container `gpi-db`, database `gpi_sample_db`, host port **5433**.
- `numpy` is the only numeric dependency. `scipy`, `scikit-learn`, `statsmodels` and `pandas`
  are deliberately absent; adding any requires explicit user approval (§5).

## Hard constraints

- Never write into `deployment/` before the user has run and approved the change (§6, §7).
- Never `git add -A`; stage explicit paths only.
- Pushing is the user's call. Prepare the commit, show what it contains, and ask (§7).
- Never introduce a `.env`, DB URI, or credential into `deployment/`. `funda_agent_exp.py` in
  this directory has a hardcoded API key fallback — do not propagate it (§4).
