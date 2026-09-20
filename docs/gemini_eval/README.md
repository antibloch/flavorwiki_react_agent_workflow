# Gemini-vs-OpenAI provider suite — preserved evidence

The sandbox `exp/gemini_eval/` was removed on 16 Sep 2026 (220 MB: its own venv plus a
patched fork of the four agent modules, already diverged from the working tree and a
standing risk of being grepped or edited by mistake). It is fully reproducible from
`reg_plan.md` §4's idempotent bootstrap script.

These four files are what the reports cite and what `reg_plan.md` §6 names as the
evidence-recovery path, so they are kept here instead:

| file | why |
|---|---|
| `BASELINE.txt` | source hashes (working tree + patched rev1/rev2) and resolved package versions — the baseline identity cited by `gemini_vs_openai.md` and `comp_param.md` §1 |
| `provider_switch.diff` | the 23-line provider switch, the only edit under test |
| `requirements.txt` | what the sandbox venv was built from |
| `provider_suite_conversation.sqlite3` | the sandbox conversation log for all 30 runs: full tool arguments and public tool outputs keyed by `thread_id`. `reg_plan.md` §6 requires recovering a clipped field from here rather than re-running. Renamed from `conversation.sqlite3` because the root `.gitignore` pattern matches that name at any depth. |

Reports: `gemini_vs_openai.md` (verdict), `comp_param.md` (parameters, probes, criteria),
`reg_plan.md` (method, local-only — it carries live API keys), `probes.md` (case selection).
