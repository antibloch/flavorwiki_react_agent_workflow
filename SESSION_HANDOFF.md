# Session Handoff

## Objective and Current State

The current objective is to keep the survey analyst useful across surveys while enforcing a hard client boundary. The agent may move from the initial survey to another survey only when the user explicitly requests it or a comparison requires it; ordinary work stays on the initial survey. Same-client surveys across any of that client's organizations are accessible. A foreign-client survey must be refused in code and treated as terminal by the agent. Response-count comparisons now consistently mean distinct people who submitted at least one answer.

The code-level guard, minimal prompt rules, response-count semantics, terminal-refusal behavior, and deployment mirrors are implemented. Recent user-supplied live traces show the intended same-client success and foreign-client refusal paths.

## Repository and Runtime

- Workspace: `/home/junaid/codework/Flavorwiki/flavorai_v2/final_agent_work_v5_base_2_optim3_exp`.
- Primary files: `funda_agent_exp.py`, `agent_instructions.py`, `tool_prompts.py`, `feature_influence.py`, `api_funda_agent_exp.py`, and `tests/test_agent_optimizations.py`.
- Deployment is a nested Git repository at `deployment/`; it is clean at verified commit `327da16` (`Compare surveys within the same client`).
- Root and deployment copies of `agent_instructions.py`, `tool_prompts.py`, `feature_influence.py`, and `requirements.txt` are byte-identical. Both requirements files pin `sqlglot==30.16.0`.
- Runner files intentionally differ: deployment requires environment-provided credentials, supports configurable model names and DB connection timeout, and contains formatting differences. Do not replace it wholesale with the development runner or copy development credential defaults into it.
- The parent worktree is heavily dirty and treats this workspace as untracked. Preserve all unrelated/user-owned changes and do not assume anything is committed outside the nested deployment repository.
- Regression instructions are in `../AGENTS.md`; full live regression must follow `../.claude/skills/regression_test/SKILL.md` and needs Docker Postgres plus valid environment configuration.

## User Decisions and Preferences

- Security enforcement must be code-level; prompts are supplementary, not the boundary.
- Derive the authoritative client from the initial survey through `survey -> organization -> account.client_id`; never trust a caller-supplied client ID as authorization.
- Allow explicit navigation/comparisons to any survey owned by that client across organizations. Do not restrict all work to the initial survey.
- Keep the configured benchmark exception, but do not use it when the user excludes it.
- When the initial survey has no resolvable client, allow only that initial survey rather than disabling the guard.
- Foreign-survey refusal is a settled result: no retry, current-side partial query, substitute survey, table, UUID repetition, benchmark discussion unless asked, or clickable suggestion.
- For unclear ordinary intent, softly name exactly what was not supplied, make the strongest supported framing assumption, answer, and offer self-contained `{{clickable suggestions}}` only for genuine remaining alternatives. Never invent a required tool parameter.
- Prefer minimal patches. The user currently wants to run behavioral tests personally; do not run test plans or live agent tests unless requested.

## Completed Work

- Added fail-closed SQL authorization derived from the initial survey. The authorized set contains the initial survey, all same-client surveys across organizations, and the configured benchmark exception when applicable.
- Added `sqlglot` parsing and AST rewriting so every exposed physical table is wrapped with a verified survey-ownership filter. Multiple statements, unsafe functions/schemas, reserved scope parameters, foreign survey literals, and tables without an ownership route are refused.
- Applied the same authoritative scope to analysis packets, statistics, and feature-influence question IDs; clientless runs remain initial-survey-only.
- Updated prompts so explicit cross-survey requests and comparisons may navigate within the client boundary while normal requests stay on the initial survey.
- Defined unqualified survey response counts as `COUNT(DISTINCT a.enrollment_id)` through answers, aliased `answered_people`. Enrollment, completed-person, and answer-row counts use explicit distinct names; generic `response_count` is prohibited.
- Directed title/date/count comparisons into one `nl2sql_tool` call rather than a parallel analysis-packet call.
- Made literal foreign-survey refusals terminal at both standing-prompt and tool-result levels. Other SQL parser/guard errors remain repairable.
- Updated current prompt fingerprints in `tests/test_agent_optimizations.py` and synchronized all relevant deployment files without weakening deployment-only configuration.

## Verification

- Repository inspection on 2026-08-12 confirmed matching root/deployment hashes for instructions, tool prompts, feature influence, and requirements; terminal-refusal code is present in both runners.
- A prior focused automated run recorded in the visible conversation passed **79/79** tests after the main boundary implementation. It predates the latest response-count and terminal-refusal prompt refinements.
- No automated or live tests were run after those latest refinements, honoring the user's request to test personally.
- User-supplied live same-client trace: one SQL tool call and two LLM calls selected the most recently published other same-client survey across organizations and correctly returned 450 versus 2 answered people.
- User-supplied live foreign-client trace: the guard refused the requested survey before query execution; the agent made no second tool call and returned one concise sentence. This is the intended two-LLM-call terminal path.

## Known Issues and Next Steps

1. The latest refinements do not yet have an automated rerun; treat the supplied live traces as strong behavioral examples, not a complete regression suite.
2. Any newly exposed database table needs a verified entry in the tenant table-scope map or it will correctly fail closed.
3. One successful answer said “225× larger” for 450 versus 2; “225 times as many” is mathematically clearer. No code change was requested for this wording.
4. Preserve the historical Markdown/default-formatting rules already present in the prompts unless a new task explicitly changes them.

## Resume Instructions

Start with: “Continue from `SESSION_HANDOFF.md`; preserve the code-enforced client boundary, explicit same-client navigation, precise response-count grain, and terminal foreign-survey refusal.”

1. Inspect root and `deployment/` status before editing; preserve user changes and deployment-only environment configuration.
2. For boundary changes, review `funda_agent_exp.py`, its deployment mirror, `agent_instructions.py`, `tool_prompts.py`, and focused tests together. Never rely on prompt wording as authorization.
3. Keep agent-facing mirrors synchronized, but port runner changes selectively rather than copying the development runner wholesale.
4. Do not run tests unless requested. If full regression is requested, first read and follow `../.claude/skills/regression_test/SKILL.md` exactly.
