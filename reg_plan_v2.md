# reg_plan_v2.md — provider-regression plan, round 2: Gemini against the OpenAI baseline

**Supersedes `reg_plan.md` for all new runs.** Round 1 (11 Sep 2026, 14 cases / 15 requests per
provider) is reported in `gemini_vs_openai.md` with its parameters in `comp_param.md`; `reg_plan.md`
and `probes.md` remain the record of *how that round was run* and are not edited. This file is the
method and the case selection for round 2. Read it, `comp_param.md` §2 (what round 1 already
covered) and `regression.xlsx` (`regression_cleaned`, authoritative pass criteria) before Stage B.

**No credentials in this file.** Round 1's plan pasted live `OPENAI_API_KEY` and `GOOGLE_API_KEY`
values into a Markdown document; both should be rotated before round 2 and the rotated values kept
in an untracked env file (§2). This file carries survey scope ids only, which are already in the
tracked `comp_param.md`.

**Self-contained.** Everything needed to run and grade round 2 is here. `PROTOCOL.md` is required
for one thing only — §17, what happens after acceptance — and that is summarised well enough here
to know when to go read it.

---

## 0. Why there is a round 2, and what changed under it

Round 1 ended at *do not accept on this evidence alone*: one narrow finding on the authorization
boundary (Gemini emitted a cross-survey query with no client predicate on P13; the code guard
refused it, so no data escaped), against an otherwise favourable result — no wrong numbers in 12
content cases, no case-level contract FAIL, several clauses passed that OpenAI failed. Two
things prevent that from being a decision:

1. **The evidence was 15 single runs per provider on a narrow slice.** Round 1 deliberately
   excluded large-SQL, needle-in-haystack, persona, long-memory, guardrail-recovery and
   showcase-thread suites from the minimum gate. Those are exactly where a cheaper model is
   expected to break, and where the product's real traffic lives.
2. **Nothing measured whether the one finding was a tendency or an artefact**, because nothing
   looked at more than the two transcripts it appeared in.

Round 2 answers both: a **representative-workload tier** and a **difficult-challenge tier** (§7),
plus a **suite-wide deterministic audit** (§9) that scores *every* SQL statement and *every*
emitted form across all 60 runs instead of reasoning from two transcripts.

**Four things changed in the repo since round 1, and each invalidates part of round 1's setup.**

| change | consequence for this plan |
|---|---|
| Three of the four agent modules moved: `funda_agent_exp.py` `33f81ed1…`→`c7e39c90…`, `agent_instructions.py` `8fd6aaf4…`→`fa3baf6e…`, `tool_prompts.py` `fe5f9ac5…`→`07487ebf…` (`plsr.py` unchanged) | Round 1's verdicts are **stale pending an applicability check** for both arms. Round 2 re-runs **both** providers; no round-1 number is reused as a baseline. |
| A **sixth tool**, `cluster_rating_profiles`, plus a new `CLUSTER_RULES` block in the assembled prompt, and a new local import `from clustering import …` | The §4 bootstrap must copy `clustering.py` or the sandbox dies with `ModuleNotFoundError`; the sandbox verification must expect **six** tools, not five; and the clustering path has never been exercised under Gemini at all (B12). |
| The grader split completed on 14 Sep: `oracle_agent` is now a **producer only**; `trace_compare` owns the **content** and **trajectory** verdicts; `contract_review` owns presentation/behaviour plus task fulfilment and harness self-consistency | Round 1's §11 ("`oracle_agent` — content only") describes a grading contract that no longer exists. §11 below replaces it, and round 2 gains a **trajectory verdict**, which round 1 had none of. |
| `regression.xlsx` rows 52, 53, 54, 83, 89, 100, 121 had their criteria rewritten on 16 Sep against the live rules | The four "stale criterion" caveats in `gemini_vs_openai.md` do not apply to round 2. Row numbers did not move, so every citation in `comp_param.md` still resolves. |

**Also corrected here:** round 1 discovered adapter fix A1 (`prompt_cache_key` is an OpenAI
invoke-time kwarg that `GenerateContentConfig` rejects) only when Stage A crashed. It is folded
into the bootstrap patch below as a third anchored edit, so Stage A no longer pays for it.

---

## 1. The decision this plan supports

Whether `gemini-3.5-flash` can replace `gpt-5.5` in this agent, at one evaluated revision, one
database state and one configuration — judged on a workload that resembles what the product is
actually asked, and on a challenge set chosen where a cheaper model is most likely to fail.

The design is unchanged from round 1 and still correct: **paired execution with independent
grading.**

> Run OpenAI as the baseline and Gemini as the candidate on the same probe, then grade **each**
> transcript on its own. Compare verdicts, not prose.

The OpenAI transcript is a **comparator, not ground truth**. Gemini is not accepted because it
resembles the OpenAI answer, and not rejected because it differs in wording, paragraph order,
internal reasoning, or a tool ordering the contract leaves free. What must match is externally
observable correctness and contract compliance. Where the contract fixes a form — chart payload
shape, nested-table structure, benchmark banner, suggestion chips, disclosure timing — it is fixed
for both.

**What this round can conclude, stated before it runs.** 30 single runs per provider support a
regression claim over the selected surface, not statistical equivalence and not a stability claim.
The one place a *rate* is defensible is §9's audit, whose denominator is SQL statements and emitted
forms (hundreds across the suite), not probes.

---

## 2. Providers, models and credentials

| | OpenAI (baseline) | Gemini (candidate) |
|---|---|---|
| model | `gpt-5.5` — the current `MODEL_NAME` in `funda_agent_exp.py`, unchanged | `gemini-3.5-flash` |
| temperature | `0.0` (`MODEL_TEMPERATURE`) | `0.0` — do not inherit Gemini's default of 1 |
| key env | `OPENAI_API_KEY` | `GOOGLE_API_KEY` |
| package | `langchain-openai==1.4.1` (pinned) | `langchain-google-genai` (round 1 resolved `4.3.2`) |
| class | `ChatOpenAI` | `ChatGoogleGenerativeAI` |

Keys come from an untracked file, never from this document:

```bash
# ~/.flavorai_eval_env  (chmod 600, never committed, never pasted into a grader packet)
export OPENAI_API_KEY='sk-…'
export GOOGLE_API_KEY='…'
```
```bash
source ~/.flavorai_eval_env
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY" \
  | grep -o '"name": "models/[^"]*"' | grep -c 'gemini-3.5-flash'   # expect >= 1
```

`funda_agent_exp.py` in this directory still carries a hardcoded OpenAI key fallback and a
hardcoded charts secret in source. The `deployment/` copy does not and must never gain them. Do
not propagate either; prefer rotating them.

---

## 3. Preconditions — blocking, in order

1. **`gpi-db` is up.** Container `gpi-db`, database `gpi_sample_db`, user `gpi`, host port
   **5433**. It stops on its own: `docker ps` then `docker start gpi-db`. Without it neither
   reference can be produced and every content row becomes `UNVERIFIED — reference unavailable`,
   which is not a pass.
2. **The Charts statistics API is reachable.** `CHARTS_API_BASE_URL` defaults to
   `https://charts-api.gpisurveys.com`; the root `.env` supplies DB and Charts settings. The
   statistics tool and `oracle_agent` both call the live API.
3. **The OpenAI account has credit.** `probe_scratchpad.md` records an `insufficient_quota` (429)
   that stopped an earlier re-probe mid-flight. No credit means no baseline half; say so plainly
   rather than presenting a Gemini-only run as a paired result.
4. **The sandbox is built** — §4's bootstrap *and* its verification, which now expects six tools.
5. **Stage 0 has run** (§9.1): the audit script is calibrated against round 1's preserved log
   before it is trusted on round 2's.
6. **Nothing else moves between the two halves of a pair**: same code revision, same database
   state, same inventory setting, same scope, same thread sequence, same environment overrides.
7. **Python and disk.** There is no bare `python` on this machine — use a venv's interpreter or
   activate it first. `uv` is at `.venv/bin/uv`. The sandbox venv costs ~220 MB. `pytest` is not
   installed; unit tests run with `python -m unittest`.

---

## 4. The adapter and the isolated sandbox

The change under test is one switch — which chat model the agent builds — applied to **copies**,
never to the working tree. Nothing in the working tree is modified; if Gemini is rejected,
`rm -rf exp/` is the entire cleanup.

Three differences from round 1's bootstrap, all forced by §0's table: **`clustering.py` is
copied**, **adapter fix A1 is applied up front**, and the verification expects **six** tools.
All three patch anchors were confirmed present in `funda_agent_exp.py` at `c7e39c90…` on
16 Sep 2026 (`MODEL_NAME = "gpt-5.5"` at :139, `llm = ChatOpenAI(` at :2937,
`cache_key = _conversation_prompt_cache_key(config)` at :4173).

```bash
cat > /tmp/bootstrap_gemini_eval2.sh <<'BOOT'
#!/usr/bin/env bash
# Build the isolated provider-comparison sandbox. Nothing in the working tree is modified.
set -euo pipefail
ROOT="$(pwd)"
SBX="$ROOT/exp/gemini_eval"
[ -f "$ROOT/funda_agent_exp.py" ] || { echo "run this from the agent working directory"; exit 1; }

rm -rf "$SBX"; mkdir -p "$SBX"
# clustering.py is NEW since round 1 -- funda_agent_exp.py imports it at module scope.
cp funda_agent_exp.py agent_instructions.py tool_prompts.py plsr.py clustering.py \
   requirements.txt "$SBX/"

{ echo "# sandbox built $(date -Iseconds)"; echo "# source hashes (working tree, unmodified):";
  sha256sum funda_agent_exp.py agent_instructions.py tool_prompts.py plsr.py clustering.py; } \
  > "$SBX/BASELINE.txt"

python3 - "$SBX/funda_agent_exp.py" <<'PATCH'
import sys
p = sys.argv[1]
src = open(p, encoding="utf-8").read()

OLD_MODEL = 'MODEL_NAME = "gpt-5.5"'
NEW_MODEL = '''LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
_DEFAULT_MODELS = {"openai": "gpt-5.5", "gemini": "gemini-3.5-flash"}
MODEL_NAME = os.getenv("LLM_MODEL", _DEFAULT_MODELS.get(LLM_PROVIDER, "gpt-5.5"))'''

OLD_LLM = '''llm = ChatOpenAI(
            model=MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            api_key=OPENAI_API_KEY,
            **_LLM_TUNING
        )

model = llm.bind_tools(tools=tools, parallel_tool_calls=True)'''
NEW_LLM = '''if LLM_PROVIDER == "gemini":
    # OpenAI-only tuning (reasoning_effort / use_responses_api / verbosity) is NOT forwarded.
    from langchain_google_genai import ChatGoogleGenerativeAI
    llm = ChatGoogleGenerativeAI(
            model=MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        )
    # parallel_tool_calls is an OpenAI kwarg; Gemini decides fan-out itself.
    model = llm.bind_tools(tools=tools)
else:
    llm = ChatOpenAI(
            model=MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            api_key=OPENAI_API_KEY,
            **_LLM_TUNING
        )
    model = llm.bind_tools(tools=tools, parallel_tool_calls=True)'''

# Adapter fix A1, folded in from round 1: prompt_cache_key is an OpenAI invoke-time kwarg and
# GenerateContentConfig rejects unknown fields ("Extra inputs are not permitted").
OLD_CACHE = '    cache_key = _conversation_prompt_cache_key(config)'
NEW_CACHE = ('    cache_key = _conversation_prompt_cache_key(config) '
             'if LLM_PROVIDER == "openai" else None')

for old, new in ((OLD_MODEL, NEW_MODEL), (OLD_LLM, NEW_LLM), (OLD_CACHE, NEW_CACHE)):
    assert src.count(old) == 1, f"anchor matched {src.count(old)} times, expected 1:\n{old[:80]}"
    src = src.replace(old, new)
open(p, "w", encoding="utf-8").write(src)
print("provider switch + A1 applied")
PATCH

uv venv "$SBX/.venv" --python 3.12 >/dev/null
VENV_PY="$SBX/.venv/bin/python"
uv pip install --python "$VENV_PY" -q -r "$SBX/requirements.txt" langchain-google-genai
uv pip freeze --python "$VENV_PY" | grep -iE "langchain|langgraph|google" >> "$SBX/BASELINE.txt"

{ echo "# sandbox hashes (patched):"; sha256sum "$SBX"/*.py | sed "s|$SBX/||"; } >> "$SBX/BASELINE.txt"
diff -u funda_agent_exp.py "$SBX/funda_agent_exp.py" > "$SBX/provider_switch.diff" || true
echo "sandbox ready: $SBX (diff vs tree: $(grep -c '^[+-][^+-]' "$SBX/provider_switch.diff") lines)"
BOOT
bash /tmp/bootstrap_gemini_eval2.sh
```

**Verify before spending anything** — both providers must construct and bind the same **six**
tools against the same database, with no LLM call made:

```bash
cd exp/gemini_eval
for prov in openai gemini; do
  LLM_PROVIDER=$prov GOOGLE_API_KEY="$GOOGLE_API_KEY" ./.venv/bin/python -c "
import funda_agent_exp as a
print(a.LLM_PROVIDER, '|', a.MODEL_NAME, '|', type(a.llm).__name__)
print(len(a.build_tools()), [t.name for t in a.build_tools()])
print(a.DATABASE_URI.split('@')[-1])"
done
cd - >/dev/null
```

Expect `6 ['nl2sql_tool', 'get_survey_analysis_packet', 'generate_word_cloud',
'run_survey_stats', 'analyze_plsr', 'cluster_rating_profiles']` on both, and
`localhost:5433/gpi_sample_db` on both. A five-tool list means the bootstrap copied a stale tree.

The sandbox sits two directories below the tree root, so `_BE_ENV` does not resolve and
`DATABASE_URI` falls back to `postgresql://gpi:gpi_local@localhost:5433/gpi_sample_db`, which is
byte-identical to what `BE/.env` supplies. Confirm the printed host says so.

**Do not use `.claude/skills/oracle_agent/scripts/capture_agent.py` for sandbox runs.** It
computes `REPO_ROOT` from its own location (`parents[4]` = the working tree) and inserts that on
`sys.path`, so `import funda_agent_exp` inside it resolves to the **unpatched working-tree
module** — it would silently run OpenAI while the environment says `LLM_PROVIDER=gemini`. It also
has no `--chat`, which Tier A needs. Round 2 captures the manifest separately (§13) and runs the
sandbox CLI directly.

**What the switch does and deliberately does not do.**

- `LLM_PROVIDER=openai|gemini` picks the branch; `LLM_MODEL` overrides the model id. No code edit
  between the halves of a pair.
- OpenAI-only tuning (`reasoning_effort`, `use_responses_api`, `verbosity`) stays on the OpenAI
  branch, as does `prompt_cache_key`. If a Gemini thinking control is ever added, fix it once,
  record it in the baseline identity (§5), and never vary it between probes.
- `parallel_tool_calls=True` is an OpenAI kwarg and is not passed to Gemini, which decides fan-out
  itself. Whether Gemini fans out is an acceptance criterion (§10), not a detail to smooth over.
- Streaming stays on. `message_to_text()` skips typed content blocks, so reasoning parts must not
  reach the transcript; Stage A confirms Gemini's thought parts hit that same branch.
- **No prompt text, tool description or rule block changes.** The assembled system prompt must
  hash identically for both providers (§13).

**Shim neutrality** (four OpenAI requests, outside the 60). Both providers run inside the sandbox, so the switch is common to both halves
of every pair — but it is a confound until ruled out at *this* revision. Round 1's control passed
at the old revision and does not transfer. Re-run it: A2 and B08 under `LLM_PROVIDER=openai` in the
sandbox versus the same two from the unmodified working tree (§13). Same tool calls, same figures,
same artifact shape = neutral shim. A difference is a **sandbox defect**, not a provider finding.

**Dependency approval.** The instruction to evaluate `gemini-3.5-flash` is approval for
`langchain-google-genai` and nothing else. Inside the sandbox it is unpinned by design — record
the resolved version from `BASELINE.txt`. It becomes a pinned entry in both trees'
`requirements.txt` only if the migration is accepted (§17).

---

## 5. Baseline identity — record once per provider, at capture time

Without secrets, computed when the run happens, never back-dated:

- provider, model id, temperature, any thinking/reasoning setting, provider-specific kwargs;
- SHA-256 of `funda_agent_exp.py`, `agent_instructions.py`, `tool_prompts.py`, `plsr.py`,
  `clustering.py` — `BASELINE.txt` holds both working-tree and patched hashes plus the resolved
  `langchain-google-genai` version; cite it rather than recomputing;
- the provider switch itself: `exp/gemini_eval/provider_switch.diff`;
- the assembled-system-prompt hash (§13), which must match across arms;
- command, entrypoint, run locator, thread id;
- environment overrides in force (`LLM_PROVIDER`, `MAX_PARALLEL_SQL_CALLS`,
  `MODEL_REASONING_EFFORT`, `HISTORY_MAX_ROUNDS`, `LOG_TOOL_CALL_CHARS`, …);
- database state identifier.

A later edit to an implicated file makes that run's verdict **stale pending an applicability
check**. A demonstrated behaviour-neutral edit (formatting) may retain its evidence with the diff
and reason recorded; a behaviour-affecting edit requires a fresh run and fresh grading.

---

## 6. Budget: one run per probe, and what replaces replication

- **30 requests per provider, 60 runs total**, plus §4's four shim-neutrality control requests,
  which sit outside that count. One run per probe per provider, including
  model-chosen branches — chart type, nesting depth, roster vs single snapshot, which valid tool
  order is taken. A single pass or fail is evidence **for that run only**; mark it a single-run
  result and never report it as a stability or failure rate.
- **No repeats are scheduled in advance, and none are reserved.** A second run happens only if the
  user asks, or if the single run is *technically* inconclusive: infrastructure failure, an
  unrecoverable clipped field, or an adapter fault that stopped the agent path executing. A verdict
  you dislike is not inconclusive.
- **What replaces replication is §9's audit.** Round 1's open question — *is unscoped cross-survey
  SQL a Gemini tendency or a one-run artefact?* — is answered by scoring every SQL statement the
  suite produces, not by re-running two probes. That keeps `PROTOCOL.md` §6.1's one-run budget
  intact and still yields a denominator worth quoting.
- **Recover before re-running.** If a decisive field is clipped, extract it from the same run's
  conversation log first (full tool arguments and public tool outputs, keyed by `thread_id`),
  keeping the run locator. If it was never captured, name the smallest focused check and leave the
  item unverified. An evidence gap does not authorise repeated stochastic trials.
- If a repeat is ever authorised, repeat only the **smallest case that separates the candidate
  causes** — adapter fault, model choice, external dependency — not the tier.

Grading, not running, is this suite's dominant cost: 60 `contract_review` grades, ~24 ground-truth
derivations and 8 Phase 6 reference solves (§11). Stage the grading in §8's order so an early stop
saves grader spend too.

---

## 7. The probe set — 30 requests per provider

Selection rules, applied to `regression.xlsx` (`regression_cleaned`):

1. **Do not re-litigate round 1.** Every case here is a workbook row round 1 did not run, with two
   deliberate re-entries: **row 86 as C1**, to re-test the one thing round 1 found, and **row 121
   inside A1's thread** (round 1's P10), which returns as turn 7 of the showcase session rather than
   as a single shot — and which round 1 left with an open, never-exercised correction obligation.
2. **Tier A mirrors real traffic**, including its shape: a real session is a *thread*, not fifteen
   independent single-shots. Round 1 never tested context accumulation beyond two turns.
3. **Tier B maximises discrimination**: cases whose criteria are deterministic and whose failure
   modes are the ones cheaper models exhibit — large tool output, error recovery, multi-grain SQL,
   semantic traps, rule-heavy rendering with no runtime validator.
4. **Prefer rows with explicit, currently-live pass criteria**; where a row is marked NOT YET RUN,
   say so — the paired OpenAI run establishes its baseline in-suite and it cannot lean on a
   documented history.
5. **No known-open blemish is used as a clean gate.** Where a row carries an accepted residual
   (B07), only regression past the documented residual counts as a fail.

### Tier A — representative workload (12 requests)

| ID | Row(s) | Suite / Case | Scope | Prompt | What it discriminates |
|---|---:|---|---|---|---|
| A1 | 115-124 | `Herbalife showcase thread / turn01…turn10` | HERB | the ten turns verbatim in the workbook, in order, **one process** | The product's own showcase session end to end: orientation from inventory, grouped nested table, chart of the same comparison, benchmark describe-banner, benchmark compare-banner, a superlative check with a documented counter-example (Product D beats the benchmark on flavor), PLSR, a PCA that **cannot** be computed, word cloud, personas. By turn 10, `HISTORY_MAX_ROUNDS=5` has trimmed the opening turns — the one place the suite tests whether a cheaper model holds a thread together. |
| A2 | 87 | `Unclear intent handling / supported_assumption` | HERB | `Which product performed best?` | The commonest real shape: underspecified. Must open with the exact gap, assume overall product liking, run ANOVA/Tukey **before** any significance verdict, and end with feasible self-contained `{{…}}` alternatives. |
| A3 | 43 | `Needle in haystack / T3a` | PEPS-A | `Which product has the highest overall liking score?` | Measure selection with a scale-pooling trap under a different phrasing, on a third survey. Round 1's only pooling failure was OpenAI's. |

A1 is ten requests, A2 and A3 one each.

### Tier B — difficult challenge set (15 requests)

| ID | Row | Suite / Case | Scope | Flags | What it discriminates |
|---|---:|---|---|---|---|
| B01 | 44 | `Large SQL output / E1` | HERB | — | 900 rows / 210 KB of verbatims that cannot be aggregated away. Context and condenser stress; the failure mode is silent truncation or a summary passed off as the verbatims asked for. |
| B02 | 71 | `Optimal SQL edge cases / O1` | TAST | — | Multi-grain paired analysis: two demographics × two structured ratings, pre- against post-tasting, without child-row multiplication. The hardest single SQL in the catalogue. |
| B03 | 144 | `SQL guardrails and recovery / pg_dialect_rejection` | TAST | — | The natural shape is `COUNT(DISTINCT …) OVER ()`, which Postgres rejects. Passes either by avoiding it or by reading `SQL_ERROR_HINTS` and applying **that** fix next call. Re-sending the same rejected shape is the cheap-model failure. |
| B04 | 145 | `SQL guardrails and recovery / semantic_filter_rejection` | HERB | `--no-inventory` | The guardrail rejects `LIKE`/`ILIKE` against question prompts. Passing needs the two-query structural route the rejection describes — recovery by reasoning, not by retry. |
| B05 | 147 | `SQL guardrails and recovery / zero_row_diagnostic` | TAST | — | Disjoint cohorts (1,200 / 1,200, overlap 0) reached through household income. `ZERO_ROW_MSG` requires **one** diagnostic counting each side and the shared key. Directly aimed at round 1's observation that Gemini fired nine diagnostics on the sibling case. |
| B06 | 79 | `Demographic linkage / gender_overall_liking_unlinked` | TAST | — | Second disjoint-enrollment trap under different phrasing. Must diagnose, not fabricate subgroup means. |
| B07 | 108 | `Interpretation traps / dropout_ladder` | DESC | — | Cross-question share: 9 answered the awareness question, 5 the rating-change question, 4 of those said yes → 80%. The 4 who never reached it cannot be counted as negatives. Accepted residual: the answer may still lead with *4 of 9 — 44.4%* and render *0.0%*; only regression past that fails. |
| B08 | 89 | `Nested result rows / recursive_product_area_attributes` | MEM | — | Two-level hierarchy, 39 product-attribute rows, **no SQL** — inventory only. Round 1's nested case was one level deep. The heaviest rendering contract in the catalogue, and `gpi-nested-table` has no runtime validator, so the model is on its own. |
| B09 | 114 | `Charts / chartcap` | TAST | — | 80 measures × 4 products = 320 cells against `_MAX_CHART_POINTS=300`. Pass = subset under the cap and say which measures were charted; fail = the runtime deletes the chart and appends the withheld-chart sentence. NOT YET RUN on either provider. |
| B10 | 104 | `Descriptive comparison / a1reg` | DESC | — | Direction semantics: 4 of 5 taste attributes are pure intensity scales where *higher is better* is meaningless. Any claim that more bitterness/saltiness/sourness/sweetness is better fails. NOT YET RUN. |
| B11 | 126 | `PLSR / ncomp3` | PLSRS | — | Names both sides *and* normalization, CV and 3 components, so the tool must actually run — which round 1's P10 never did, leaving the `ncomp3`/`ncomp_cap` correction obligation UNVERIFIED. Tool call must carry `n_components: 3`. |
| B12 | 133 | `Respondent clustering / liking_clusters` | HERB | — | The sixth tool, added after round 1 and never exercised under Gemini. Default `k` must be disclosed as the agent's own choice, N and spread with every mean, no silently dropped attribute, no satisfaction-tier framing. |
| B13 | 129 | `Output form / code_request` | HERB | — | Asked for a Python script. Output-form guardrail — and the form a code-trained model is most likely to over-serve. |
| B14 | 134 | `Refusal rendering / nonexistent_survey_refusal` | NOSURV | — | Survey id ends `…7600` and does not exist; client and org are valid and paired. PASS says *does not exist*; naming an authorization cause fails, and so does silently substituting the near-identical `…7672`. |
| B15 | 143 | `Progress memory / delta_block` | HERB | `--no-inventory` | The only probe for `PROGRESS_MEMORY_RULES`. Every response that makes a further tool call must open with exactly one `<progress_gathered>` block, append-only, ≤12 bullets / 2,000 chars, each sourced. A structured-emission contract with no validator behind it. |

### Tier C — the round-1 carry-over: scope discipline (3 requests)

C1 is one of rule 1's two re-entries; C2 and C3 are new. They are grouped because round 1's single
finding lives here, and because §9's audit needs cases that *generate* cross-survey statements.

| ID | Row | Suite / Case | Scope | What it settles |
|---|---:|---|---|---|
| C1 | 86 | `Client-wide survey boundary / foreign_terminal` | HERB | Round 1's finding, re-probed at the current revision: the guard must refuse before data retrieval, and the **query itself** must carry the tenancy chain. `tool_prompts.py:186-187` forecloses the "the executor catches it" defence. |
| C2 | 85 | `Client-wide survey boundary / same_client_latest` | HERB | The mirror image, which round 1 never ran: cross-organization navigation that is **legitimate**. A model that over-refuses here is as wrong as one that under-scopes at C1, and only running both tells them apart. |
| C3 | 146 | `SQL guardrails and recovery / scope_id_repair` | HERB | A near-miss literal (`…7673` for `…7672`) is the only reliable way to reach `_repair_scope_ids`. The repair must fire **and** be disclosed back to the model. |

### Deliberately excluded, and why

Naming these keeps the conclusion honest about its surface: rows 61-70 (the 12-round memory
thread — 12 requests, and A1 already tests long-context retention with a known-failure row 70
excluded); rows 45-51 (larger SQL exports up to 112 MB — B01 covers the class); rows 98, 111, 117
variants, 141 (known-open or uncharacterised rows that cannot serve as clean gates; 141's
positional-relabel anomaly is instead watched by §9's audit); the whole benchmark block 25-34
(round 1 covered both benchmark branches); the API surface and the `scoped_query_survey_only`
degraded path (CLI-only round, by decision — that path still has **no** probe coverage on either
provider, and any claim about it remains code-read only).

---

## 8. Stages and stop rules

| stage | what runs | stop rule |
|---|---|---|
| **0. Audit calibration** | §9's script over `docs/gemini_eval/provider_suite_conversation.sqlite3` (round 1's preserved 30 runs) | It must reproduce §9.1 exactly: **flagged Gemini 1 (`gemini_p13`), OpenAI 0**, with both P04 statements unflagged because they carry `:client_id`. Any other total means the classifier is miscounting — fix it before Stage B. An audit that flags in-survey `question_id` literals is noise, and that is the failure an earlier draft of the script actually had. |
| **A. Adapter smoke** | A2 on Gemini (one request), checked against §8.1's list | A failure here is an **adapter compatibility failure**, not a regression finding. Stop, fix, restart Stage A. Do not spend content budget on a broken adapter. |
| **B1. Carry-over** | Tier C, both providers (6 runs) | If Gemini returns **foreign data** (as opposed to emitting an under-scoped query the guard refuses), stop the suite immediately and report: that is a data-boundary event, not a regression finding. |
| **B2. Difficult set** | Tier B, both providers (30 runs) | If Gemini accumulates **three case-level FAILs that the paired OpenAI run passes**, stop and report REGRESSION FOUND rather than spending Tier A. |
| **B3. Representative workload** | Tier A, both providers (**23 runs** — Gemini's A2 was already spent as Stage A) | Runs last because it is the most expensive per verdict and the least diagnostic if the agent is already failing. A1's ten turns are one process; a crash mid-thread makes the remaining turns UNVERIFIED, not failed. |
| **C. Audit + assembly** | §9 over round 2's log, then §11 grading, then §18's template | Round 2 is completed and reported **standalone** here. Nothing from round 1 enters any verdict in this stage. |
| **D. Combined report** | §19 — pool round 1's results with round 2's and write `comp_report.md` | Runs only after Stage C's standalone report exists and the user has read it. Pooling is assembly, not judging: no verdict may be created, revised or reconciled at this stage. |

### 8.1 Stage A checks (asserted separately from A2's own verdict)

The Gemini chat model imports and the process starts; system and user messages are accepted in the
expected LangChain form; streamed chunks are recognised by the caller; tool calls arrive with the
expected name and JSON-compatible arguments; tool results can be appended and followed by another
model turn; the final no-tools synthesis turn works; thought/reasoning parts are **not** rendered
as user-visible text; OpenAI-only tuning kwargs and `prompt_cache_key` are not forwarded; usage
metadata is normalised or explicitly marked unavailable.

---

## 9. The suite-wide audit — the instrument that replaces replication

Deterministic measurement over all 60 runs, computed from the sandbox `conversation.sqlite3` and
the tee'd logs. It produces **counts with denominators**, never verdicts. The running session does
not read it as a pass: its output goes to the graders with the transcripts (§11), for the same
reason §6.1 of `PROTOCOL.md` gives — grepping your own probe and calling it passed is grading your
own work.

**A — scope discipline** (the round-1 carry-over). For every `nl2sql_tool` call: classify the
statement as **data-reading** (touches `answer`, `enrollment`, `answered_question_options`,
`product`) or **catalogue-discovery** (reads `survey`/`organization`/`account` metadata only), then
record whether it binds `:survey_id`, carries a survey literal, and carries a tenancy predicate
(`:client_id`/`:organization_id`, or a join through `organization`→`account` to `client_id`). The
number that matters: **data-reading statements that reach a survey other than the scoped one and
carry no tenancy predicate**, per provider, over all SQL the suite produced. Catalogue-discovery
statements legitimately scan and must not be counted against either arm.

**B — emitted forms.** Per provider across all runs: inline TeX (`$…$`, `\alpha`, `\approx`) in
prose; `x_axis.title`/`y_axis.title` where the contract defines `label`; legacy `gpi-word-cloud`
fences; `could not render that chart` sentences; `gpi-chart` and `gpi-nested-table` block counts;
any product named `Product B` in a Herbalife-scoped answer (the recorded positional-relabel
anomaly, watched so a fourth instance is countable rather than anecdotal).

**C — execution shape.** Per run: LLM steps, tool calls by name, `[tool-batch-timing] … calls=N`
lines, refused/failed calls, `zero_row_streak` warnings, wall-clock. Aggregated per provider.

**D — halt reason.** Per run, which branch ended the loop: model-selected answer, budget-forced
synthesis with tools removed (`STEP_BUDGET_NOTICE` / `MAX_LLM_STEPS`), trusted artifact returned
without invoking the model, or execution failure. Round 1 measured none of this, and *gave up
early* is a cheaper model's characteristic failure. Where the evidence cannot distinguish them,
record the halt reason as unverified rather than guessing.

```bash
# audit_suite.py -- run against any provider-suite conversation log.
# Stage 0 passes the preserved round-1 log explicitly; the default is round 2's sandbox log.
.venv/bin/python - docs/gemini_eval/provider_suite_conversation.sqlite3 <<'EOF'
import sqlite3, json, re, collections, sys
DB = sys.argv[1] if len(sys.argv) > 1 else "exp/gemini_eval/conversation.sqlite3"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A survey literal only counts when it sits in a survey-identifying position.
SURVEY_LIT = re.compile(
    r"""(?:\bs\.id|\bsurvey\.id|\bsurvey_id|"surveyId")\s*
        (?:=\s*'""" + UUID + r"""'|IN\s*\([\s:,'\-0-9a-zA-Z_]*'""" + UUID + r"""')""",
    re.I | re.X)
# Data-reading = respondent data in FROM/JOIN, not the mere appearance of the word.
DATA = re.compile(r"\b(?:from|join)\s+(?:answer|enrollment|answered_question_options|product)\b", re.I)
# Tenancy = the bound parameters only. A literal client id is not compliance.
TENANT = re.compile(r":client_id|:organization_id", re.I)

agg = collections.defaultdict(collections.Counter); flagged = []
for tid, turn, name, content in sqlite3.connect(DB).execute(
        "select thread_id,turn,name,content from conversation_events where kind='tool_call'"):
    prov = tid.split('_')[0]; a = agg[prov]; a[f"tool:{name}"] += 1
    if name != 'nl2sql_tool':
        continue
    try:    sql = json.loads(content).get("sql_query", "")
    except Exception: sql = content
    a["sql_total"] += 1
    data_reading, cross_survey, tenant = bool(DATA.search(sql)), bool(SURVEY_LIT.search(sql)), bool(TENANT.search(sql))
    a["data_reading" if data_reading else "catalogue_discovery"] += 1
    if ":survey_id" in sql: a["survey_bound"] += 1
    if tenant: a["tenant_bound"] += 1
    if cross_survey:
        a["cross_survey"] += 1
        if data_reading and not tenant:
            a["UNSCOPED_CROSS_SURVEY"] += 1; flagged.append((tid, turn, sql[:200]))
for p in sorted(agg): print(p, dict(agg[p]))
print("\n-- data-reading statements reaching another survey with no tenancy predicate --")
for f in flagged: print(f)
if not flagged: print("  (none)")
EOF
```

```bash
# B and C, from the tee'd logs. grep -c counts LINES; these count OCCURRENCES, which is what
# a rate needs. The Product B watch is HERB-scoped only -- that is the whole point of it.
HERB_LOGS="/tmp/${P}_a1.log /tmp/${P}_a2.log /tmp/${P}_b01.log /tmp/${P}_b04.log /tmp/${P}_b12.log
           /tmp/${P}_b13.log /tmp/${P}_b15.log /tmp/${P}_c1.log /tmp/${P}_c2.log /tmp/${P}_c3.log"
for P in openai gemini; do
  echo "== $P"
  cat /tmp/${P}_*.log > /tmp/${P}_all.log
  occ () { grep -oE "$1" /tmp/${P}_all.log | wc -l; }
  echo -n "inline TeX: ";             occ '\$[^$]*\$|\\alpha|\\approx'
  echo -n "axis .title: ";            occ '"(x|y)_axis"[^}]*"title"'
  echo -n "legacy wordcloud fence: "; occ 'gpi-word-cloud'
  echo -n "withheld charts: ";        occ 'could not render that chart'
  echo -n "batch lines: ";            occ 'tool-batch-timing'
  echo -n "zero-row streaks: ";       occ 'zero_row_streak'
  echo -n "step-budget halts: ";      occ 'STEP_BUDGET_NOTICE|step budget'
  echo -n "Product B (HERB logs only): "; grep -oE 'Product B' $HERB_LOGS | wc -l
done
```

**The withheld-chart count is not a sound negative for a PCA answer.** Round 1 recorded this
itself: `_assemble_pca_final` strips model-authored chart fences with **no** withheld sentence, so
`withheld charts: 0` proves nothing about A1 turn 8 or any other PCA case. Read that number as
covering the non-PCA answers only, and treat a silently stripped PCA chart as UNVERIFIABLE from the
log — it needs the unassembled turn, which nothing captures.

### 9.1 Stage 0 result — what the audit already says about round 1

The script above, run against round 1's preserved log
(`docs/gemini_eval/provider_suite_conversation.sqlite3`, 30 threads, 42 tool calls) on 16 Sep 2026,
before round 2 spends anything:

- **21 `nl2sql_tool` statements in the whole of round 1** — 14 Gemini, 7 OpenAI. That is the true
  denominator behind round 1's authorization finding, and it is small: no rate claim was ever
  available from it.
- **A tenancy predicate appears on 1 of 14 Gemini statements and 2 of 7 OpenAI statements.** The gap
  is real but far narrower than "OpenAI joins the full tenancy chain" suggests as a general habit;
  most statements on both arms are single-survey and bind `:survey_id`.
- **Exactly one statement per arm names another survey, and the pair reproduces the reported finding
  verbatim.** OpenAI's carries `JOIN organization … JOIN account … WHERE ac.client_id=:client_id`;
  Gemini's carries no tenancy predicate at all. Flagged: **Gemini 1, OpenAI 0.**
- **P04 on both arms is same-client catalogue navigation** — it scans the survey table and
  subselects response counts from `answer`/`enrollment`, so it does read respondent data — and both
  carry `:client_id`, so neither is flagged. That pair is the classifier's calibration: an earlier
  draft counted any UUID literal as reaching another survey and flagged 9 Gemini + 4 OpenAI
  statements, nearly all of them `question_id` literals *inside* the scoped survey.

**What the classifier can and cannot see.** It flags a statement only when a survey literal sits in
a survey-identifying position (`s.id`/`survey_id`/`"surveyId"`, by `=` or in an `IN` list). A
cross-survey reach built from a subquery rather than a literal is invisible to it, and a tenancy
predicate is recognised only as the bound parameters `:client_id`/`:organization_id` — a literal
client id does not count as compliance, which is deliberate but will read as a miss if the agent
ever writes one legitimately. It is a screen that routes statements to the graders, never a verdict,
and any flag it raises is evidence for `contract_review`, not a finding on its own.

This is a retrospective measurement over a small denominator and it does not change round 1's
recorded verdicts. Its value is that Tier C and the navigation cases in round 2 are chosen to
*generate* cross-survey statements, so the same measurement lands on a denominator worth quoting.

---

## 10. Acceptance criteria

Gemini passes only when, for every case:

1. every **content** verdict from `trace_compare` is PASS, or is a documented false-mismatch
   confirmed against the tool's own contract in `tool_prompts.py` (§11). B12 is excluded by
   construction — no oracle can derive a clustering reference (§11) — and the cases whose answers
   assert no figure carry no content verdict at all;
2. every applicable **presentation/behaviour** verdict passes in `contract_review`;
3. **the answer fulfils the task as asked** — no quietly dropped measures, no silently substituted
   question, nothing narrower than the prompt. Judged against the harness's own completeness rules
   (report every returned measure or name each omission and why), not taste. An answer whose
   numbers are all correct still fails here if it did not do the job;
4. **every form it emits is one the harness itself defines.** A form the system prompt authorises
   while neither the prompt nor the runtime validator defines its shape is a **harness defect**,
   not an untestable gap;
5. no new authorization, cross-thread or cross-survey contamination, and **§9's audit shows no
   data-reading statement reaching another survey without a tenancy predicate** on the Gemini arm;
6. no new tool-call, streaming, artifact or termination failure — and §9's halt-reason column shows
   no case ending in budget-forced synthesis on Gemini where OpenAI answered by model choice;
7. **the parallel-tool-call batch path is not lost.** Round 1 fired it once on Gemini and **zero
   times on OpenAI**, so "still fires where it fired for OpenAI" sets a bar the baseline has never
   met. The criterion is therefore: assert `[tool-batch-timing] … calls=N>1` at least once on the
   **Gemini** arm across the suite; record the OpenAI arm's count as evidence either way, and if
   Gemini fires zero times, record it as a named, accepted provider gap rather than a fail;
8. **the sixth tool is reachable** — `cluster_rating_profiles` is selected and called with valid
   arguments at least once on Gemini (B12);
9. **A1 holds the thread** — no turn in the showcase thread loses a fact established in an earlier
   turn that the prompt depends on, and turn 10 obeys the persona contract after
   `HISTORY_MAX_ROUNDS` trimming;
10. any OpenAI-known blemish is preserved within its documented `probe_scratchpad.md` scope, or
    improved — never newly widened;
11. all §5 baseline-identity evidence is captured for the evaluated revision.

**Not acceptance criteria.** Latency, step count and token usage are **recorded as evidence, not
gated** — no grader verdicts them, no threshold is defined, and the CLI measures none of them
(round timing, `lock_wait_ms` and token counts exist only on the API surface, which this round does
not run). This is a deliberate decision carried over from round 1, and it has a consequence worth
stating in the report rather than burying: round 1 saw Gemini 2-10× slower typically and ~30×
on two cases, so a "no observed regression" conclusion from this suite is a **correctness**
conclusion and does not by itself say the swap is operationally viable. Wall-clock is recorded per
case so that judgement can be made separately.

---

## 11. Grading — one producer, two judges

**Every transcript is graded by an `ISOLATED-GRADER` — a context with no memory of the adapter work
that did not write it.** In Claude Code that is a subagent; in Codex CLI a `codex exec` child
process. Never inline. All three procedures live in `.claude/skills/` (`.codex/skills/` symlinks to
the same text) and need the local `gpi-db` and Charts API.

**This is the part of round 1's plan that is obsolete.** Round 1 asked `oracle_agent` for content
verdicts. Since 14 Sep the roles are split three ways, and a procedure that produced its own
standard would be checking its own work:

- **`oracle_agent` — the producer. It grades nothing.** Given a `(prompt, client, organization,
  survey)` question it makes two references: **Phases 0-5** re-derive the correct answer from
  `gpi-db` and the live Charts API independently of the agent's tool calls — the **content
  standard**; **Phase 6** runs the oracle itself over its own counterparts of the agent's tools,
  logging every step to `ORACLE_TRACE_FILE` — the **trajectory and coverage standard**.
- **`trace_compare` — the judge over both references**, returning two separately labelled blocks.
  **CONTENT**: `NUMBERS / MATCHED QUESTION / RANKING / SIGNIFICANCE / AGREEMENT`, each PASS, FAIL
  or UNVERIFIED, decided against the Phases 0-5 answer — never by the two arms agreeing. Both arms
  agreeing *and* both contradicting ground truth is a tool-layer defect, attributed to `tool
  payload` or `external dependency`, never to a prompt rule. **TRAJECTORY**: `TOOL SELECTION AND
  ARGUMENTS / CALL ORDER / REDUNDANT CALLS / FAILED CALLS / ERROR RECOVERY / OUTPUT COVERAGE DELTA
  / EFFICIENCY`, plus the halt reason for both runs.
- **`contract_review` — presentation and behaviour only**, against the live rule text in
  `agent_instructions.py`/`tool_prompts.py`, cross-checked against `probe_scratchpad.md`'s accepted
  blemishes so a known gap is not re-reported as new. It also owns §10's criteria 3 and 4 — task
  fulfilment and harness self-consistency — and the tool calls *as measured against the written
  contract*. It does not verdict numbers; a wrong number it notices gets named and routed.

**What runs where.**

- `contract_review` on **all 60 transcripts**, both providers, unconditionally.
- `trace_compare` **content** on every transcript whose answer asserts a figure, a matched
  question, a ranking or a significance verdict — which is most of them. It needs an
  `oracle_agent` Phases 0-5 ground truth per case (one derivation serves both arms). Where the
  answer asserts none — B13's code-form guardrail, B14's and C1's refusals — write
  `content unaffected — answer asserts no figure` on the row instead of running it.
  **B12 is a separate case and must not be scored as a content failure.** Workbook row 133 states
  it outright: *"Content is UNVERIFIABLE by `oracle_agent` (no clustering capability exists there),
  so grade contract only."* `trace_compare` maps four capabilities — `sql_retrieval`,
  `survey_statistics`, `plsr_analysis`, `word_cloud` — and clustering is none of them. Record
  `content UNVERIFIABLE — no oracle clustering capability (row 133)`; §10's criterion 1 does not
  apply to B12, whose verdict is `contract_review`'s alone.
- `trace_compare` **trajectory** on the eight cases where *was this a sensible way to answer at
  all* is the question: **A3, B01, B02, B03, B04, B05, C1, C2**. Each needs one `oracle_agent`
  Phase 6 solve on the same prompt and scope with `ORACLE_TRACE_FILE` set; one solve serves both
  arms. Elsewhere, record `trajectory unaffected — not a retrieval-shaped case` on the row.
  **C1 is the awkward one and its asymmetry is by design.** It carries a trajectory verdict but no
  Phases 0-5 answer (its answer asserts no figure), so `PROTOCOL.md`'s rule that the Phase 6
  reference is itself checked against ground truth before anything is graded against it cannot be
  satisfied there. And the oracle's `query.sh` has no survey-boundary guard, so the reference will
  legitimately *retrieve* where the agent must *refuse*. Both belong in C1's grader packet as stated
  asymmetries: the trajectory question for C1 is only whether the agent's own sequence was a sensible
  way to reach a refusal, never whether it matched the reference's coverage.
  A provider swap reaches tool-call behaviour by definition, so this is a budget decision, not a
  blast-radius claim, and it is recorded as such.

```bash
export ORACLE_TRACE_FILE=/tmp/oracle-<case>.trace.jsonl
bash .claude/skills/oracle_agent/scripts/query.sh --why "<reason>" --tuples "SELECT …"
python3 .claude/skills/oracle_agent/scripts/stats_api.py --question <uuid> --types anova,tukey --why "<reason>"
python3 .claude/skills/oracle_agent/scripts/trace_log.py /tmp/oracle-<case>.trace.jsonl
```

Hand `trace_compare` three things: the ground-truth answer, the rendered trajectory log with the
oracle's own final answer, and the agent's captured trace.

**Grader packet** — exactly this, and no credentials:

- the case: workbook row, `Suite`/`Case`, prompt, scope, and the row's pass criteria **as they read
  today** (rows 52/53/54/83/89/100/121 were rewritten on 16 Sep; quote the live cell);
- the transcript at the §12 budget, with the full final answer;
- the §9 audit output for that run;
- the §5 baseline identity;
- the note: **this is a provider migration, not a defect fix — there is no prior defect to
  localize, no discrimination probe, and no causal verdict to produce.** Say it explicitly, or a
  grader may invent one.

**Read each verdict the way that grader fails.**

- A **content FAIL from `trace_compare` means investigate, not automatic reject.** Its ground truth
  was derived without reading `funda_agent_exp.py`'s tool code, so a number that is correct under a
  documented tool convention (a rounding rule, a default parameter) can surface as a false
  mismatch — check `tool_prompts.py` first. Conversely, a statistic the oracle computed on its own
  engine may be one the agent has no route to produce: a capability gap, not an agent error.
- A **trajectory FINDING is a claim about one pair of runs**, against one stochastic reference of a
  different policy. A difference from the reference is **not** a defect — only a difference that
  violates a stated criterion is a finding. Efficiency is judged on unbatched batchable work,
  budget exhaustion and unused retrieval, never on a raw call-count delta. The structural
  asymmetries (no `nl2sql_tool` on the reference side, no presentation contract there, PLSR shared
  end to end) are by design and are never findings.
- A **PASS from `contract_review` means the output matched the rule as written**, which is not
  evidence the rule is adequate. No prompt file changes in this migration, so its usual false-PASS
  risk does not apply — unless the adapter ever needs prompt edits, in which case it returns in
  full.
- **Two paths are invisible to comparison**: the Charts API (both sides call the same endpoint) and
  PLSR (one shared tool end to end). A figure from either that Phases 0-5 cannot re-derive is
  **UNVERIFIED**, not agreed.

Report every verdict verbatim, including anything marked UNVERIFIED or UNTESTED — neither is a
pass. A cross-domain observation one judge names but does not verdict must be re-put to the judge
that owns it before the case is closed; unrouted, it is UNVERIFIED. Something with no trace in the
transcript at all cannot be reached by any of them — say so plainly and use a code-level read.

**When the host cannot produce a reference.** `oracle_agent` needs `gpi-db` and the live Charts
API. A host without them cannot produce a content verdict at all, because `trace_compare` is
forbidden to derive its own standard. Record `content UNVERIFIED — reference unavailable on
<host>`, do not report the change as verified, and say so. Never substitute an inline reading of
the numbers for the missing reference.

---

## 12. Execution protocol per case

1. Run OpenAI once. 2. Run Gemini once — same prompt, scope, inventory setting, thread sequence,
configuration. 3. Capture both transcripts. 4. Grade both (§11). 5. Record (§14).

**Transcript budget.** Keep the user prompt, scope and rendered final answer **intact**; truncate
each serialized **tool call to 500 characters** and each **tool output to 1000 characters**, marking
truncation explicitly. If decisive evidence lies past a cutoff, extract only that field or those
lines separately from the same run's conversation log — do not widen the budget wholesale and do
not re-run. B01's 210 KB of verbatims will hit this repeatedly; extract the row count and the first
and last rows rather than the payload.

**Surface: the CLI** (`funda_agent_exp.py`) for all 30 requests, which is what `regression.xlsx`
scopes. Three consequences, stated rather than papered over:

- The CLI emits no token or latency telemetry. Wall-clock is recorded as context only (§10).
- Short-term memory is an in-process `InMemorySaver`, so **A1's ten turns must run in one
  process** — one `--chat` session with turns 2-10 piped in. Ten separate invocations sharing a
  `--thread-id` each start with empty history and would pass or fail for the wrong reason.
- `capture_agent.py` cannot be used (§4), so each run's manifest is emitted by §13's
  `manifest` helper immediately before the run, from the sandbox interpreter. Tier A shows the call
  explicitly; for every Tier B and C case, prefix the command with
  `manifest ${P}_<case> | tee /tmp/${P}_<case>.log` and change that case's own redirect to
  `tee -a`. Without it, no run can be bound to a revision or to a system-prompt hash — which is
  also what would catch a stale `LLM_MODEL` leaking across arms.

**Two run gotchas.** A long-running agent process holds the pre-change module in memory — restart
it before concluding anything about the adapter. And the `gpi-db` container stops on its own; check
`docker ps` first.

---

## 13. Commands

All probes run **inside the sandbox** (§4), against its own venv and its own conversation log.

```bash
AGENT=/home/junaid/codework/Flavorwiki/flavorai_v2/final_agent_work_v5_base_2_optim3_exp
cd "$AGENT/exp/gemini_eval"
source .venv/bin/activate
source ~/.flavorai_eval_env
docker ps --format '{{.Names}}' | grep -qx gpi-db || docker start gpi-db

HERB=(   --client-id 37cbf852-a2b7-4f8b-96b4-7f67432f88cd
         --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267672
         --org-id    c75d846a-e265-4f69-92c1-91308e0697f6 )  # A1 A2 B01 B04 B12 B13 B15 C1 C2 C3
TAST=(   --client-id f6b05cb4-cea2-4855-816e-c92e5e5d22ff
         --survey-id e14528a9-01ac-4dff-844e-0914dbdb3759
         --org-id    17b6da66-3c2e-42f1-bb64-2a09bdfbc183 )  # B02 B03 B05 B06 B09
MEM=(    --client-id c0b3b212-bc35-45ef-b69d-8257d3735a90
         --survey-id 39af3240-42a8-4e35-8c7d-c61703d5ce3f
         --org-id    46671273-0b12-49a5-b9de-60f81d192818 )  # B08
PEPSA=(  --client-id 7b445d7f-3fc1-4154-8a23-586e09c781a3
         --survey-id dedf24e1-5c82-4aeb-b0c6-d6bb4e2d104e
         --org-id    8dd4fd29-cd84-46a1-abcd-1459c547023b )  # A3
DESC=(   --client-id "" --survey-id d49a19d9-4cfa-4c1d-889b-5cb2b60d2fdc --org-id "" )  # B07 B10
PLSRS=(  --client-id "" --survey-id d67ed874-fb40-446f-a645-6edfbfbdef53 --org-id "" )  # B11
NOSURV=( --client-id 37cbf852-a2b7-4f8b-96b4-7f67432f88cd
         --survey-id 6263cf71-23b7-4462-9ccf-4a00a7267600
         --org-id    c75d846a-e265-4f69-92c1-91308e0697f6 )  # B14 -- id ends 7600, does not exist

# Run the whole block once with each of these, and change nothing else between them:
P=openai;  export LLM_PROVIDER=openai; unset LLM_MODEL   # LLM_MODEL persists from the
                                                        # Gemini arm otherwise, and the
                                                        # patch reads it for BOTH providers
P=gemini;  export LLM_PROVIDER=gemini LLM_MODEL=gemini-3.5-flash
```

**`DESC` and `PLSRS` have NULL client/org in the workbook.** The empty strings do bind scope, but
they produce a preamble production never sends: `scoped_query()` interpolates them, so the model is
handed `client_id = ` and `organization_id = ` as *authoritative blanks*. The API resolves no chain
for these surveys and sends `scoped_query_survey_only()` instead, which adds four rules this recipe
omits. Read B07, B10 and B11 as graded on the CLI shape, not the deployed one, and say so on their
rows.

**Manifest, immediately before each run** (replaces `capture_agent.py`, which would import the
working-tree module):

```bash
manifest () {  # usage: manifest <thread-id>
  ./.venv/bin/python - "$1" <<'EOF'
import hashlib, sys, funda_agent_exp as a, agent_instructions as ai
print("== PROBE MANIFEST", sys.argv[1])
print("provider/model:", a.LLM_PROVIDER, a.MODEL_NAME, "temp", a.MODEL_TEMPERATURE)
print("tools:", [t.name for t in a.build_tools()])
print("max_llm_steps:", a.MAX_LLM_STEPS, "| db:", a.DATABASE_URI.split('@')[-1])
print("system prompt sha256:", hashlib.sha256(ai.build_system_prompt().encode()).hexdigest())
for f in ("funda_agent_exp.py","agent_instructions.py","tool_prompts.py","plsr.py","clustering.py"):
    print(" ", hashlib.sha256(open(f,'rb').read()).hexdigest()[:16], f)
EOF
}
```

The system-prompt hash must be **identical across both arms**; a difference means configuration
drifted between the halves of a pair.

### Tier A

```bash
# A1 showcase thread, rows 115-124 -- ONE process, ten turns, turns 2-10 piped into --chat
manifest ${P}_a1 | tee /tmp/${P}_a1.log
printf '%s\n' \
 "Compare all three products across every descriptive attribute in this survey. Group the attributes so the table is readable." \
 "Now show me the same comparison as a chart." \
 "Tell me about the benchmark for this survey." \
 "Compare this survey's liking scores against the benchmark." \
 "Which single measure has the widest gap to the benchmark, and which product is furthest behind?" \
 "Compute PLSR between the liking attributes and overall liking." \
 "Show a 3D PCA plot of the product liking measures." \
 "What did people particularly like about these products? Show it as a word cloud." \
 "What are the personas of this survey?" \
 | python funda_agent_exp.py "${HERB[@]}" --chat --thread-id ${P}_a1 \
   --prompt "What can you tell me about this survey — how many people took it, which products were tested, and what was measured?" \
   2>&1 | tee -a /tmp/${P}_a1.log

# A2 unclear intent (row 87) -- also the Stage A adapter smoke on Gemini
manifest ${P}_a2 | tee /tmp/${P}_a2.log
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_a2 \
  --prompt "Which product performed best?" 2>&1 | tee -a /tmp/${P}_a2.log

# A3 needle, scale-pooling trap (row 43)
manifest ${P}_a3 | tee /tmp/${P}_a3.log
python funda_agent_exp.py "${PEPSA[@]}" --thread-id ${P}_a3 \
  --prompt "Which product has the highest overall liking score?" 2>&1 | tee -a /tmp/${P}_a3.log
```

### Tier B

```bash
# B01 large SQL output, verbatims (row 44)
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_b01 \
  --prompt "Show me the verbatim open-ended comments respondents wrote about each product, with the respondent identifier alongside each comment. I want the actual text, not a summary count." \
  2>&1 | tee /tmp/${P}_b01.log

# B02 multi-grain paired SQL (row 71)
python funda_agent_exp.py "${TAST[@]}" --thread-id ${P}_b02 \
  --prompt "For each gender and age group, and for each product, compare the paired mean pre-tasting expected liking ('Before beginning this tasting...') with post-tasting overall snack liking ('Thinking about EVERYTHING ALL TOGETHER...'). Include respondents who answered both questions for the same product only, and report paired respondent count, both means, and mean change (post minus pre)." \
  2>&1 | tee /tmp/${P}_b02.log

# B03 PG dialect rejection and recovery (row 144)
python funda_agent_exp.py "${TAST[@]}" --thread-id ${P}_b03 \
  --prompt "For each product, show the respondent count and what percentage that is of all respondents in the survey." \
  2>&1 | tee /tmp/${P}_b03.log

# B04 semantic-filter rejection (row 145) -- needs --no-inventory
python funda_agent_exp.py "${HERB[@]}" --no-inventory --thread-id ${P}_b04 \
  --prompt "What is the average rating for the question about sweetness, and how many people answered it?" \
  2>&1 | tee /tmp/${P}_b04.log

# B05 zero-row diagnostic discipline (row 147)
python funda_agent_exp.py "${TAST[@]}" --thread-id ${P}_b05 \
  --prompt "Compare overall liking across household income brackets." 2>&1 | tee /tmp/${P}_b05.log

# B06 disjoint enrollment keys, second phrasing (row 79)
python funda_agent_exp.py "${TAST[@]}" --thread-id ${P}_b06 \
  --prompt "How does the product differ between male and female based on their overall liking" \
  2>&1 | tee /tmp/${P}_b06.log

# B07 dropout ladder (row 108)
python funda_agent_exp.py "${DESC[@]}" --thread-id ${P}_b07 \
  --prompt "Of the people who answered 'Did you know anything about this product before this study', what share would change their rating if they evaluated the sample again?" \
  2>&1 | tee /tmp/${P}_b07.log

# B08 two-level recursive nested table, no SQL (row 89)
python funda_agent_exp.py "${MEM[@]}" --thread-id ${P}_b08 \
  --prompt "For each product, show the mean, SD, and N for every descriptive attribute measured under Appearance, Aroma, and Texture. Present each product as an expandable parent level, each question area as a nested expandable child level, and its individual attributes as a leaf table." \
  2>&1 | tee /tmp/${P}_b08.log

# B09 300-point chart cap (row 114)
python funda_agent_exp.py "${TAST[@]}" --thread-id ${P}_b09 \
  --prompt "Compare the products across every attribute measured in this survey" 2>&1 | tee /tmp/${P}_b09.log

# B10 direction semantics on intensity scales (row 104)
python funda_agent_exp.py "${DESC[@]}" --thread-id ${P}_b10 \
  --prompt "Compare the products on every taste attribute." 2>&1 | tee /tmp/${P}_b10.log

# B11 PLSR with explicit components (row 126)
python funda_agent_exp.py "${PLSRS[@]}" --thread-id ${P}_b11 \
  --prompt "Compute PLSR between the sensory attributes and overall liking using reference normalization, LOO cross-validation and 3 components" \
  2>&1 | tee /tmp/${P}_b11.log

# B12 clustering -- the sixth tool, never run under Gemini (row 133)
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_b12 \
  --prompt "Find clusters in the different liking scores of this survey" 2>&1 | tee /tmp/${P}_b12.log

# B13 output-form guardrail (row 129)
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_b13 \
  --prompt "Write a Python script that computes the mean overall liking for each product in this survey" \
  2>&1 | tee /tmp/${P}_b13.log

# B14 nonexistent survey refusal (row 134) -- NOTE the ...7600 survey id
python funda_agent_exp.py "${NOSURV[@]}" --thread-id ${P}_b14 \
  --prompt "What is the average overall liking per product in this survey?" 2>&1 | tee /tmp/${P}_b14.log

# B15 progress-memory delta block (row 143) -- needs --no-inventory for two tool batches
python funda_agent_exp.py "${HERB[@]}" --no-inventory --thread-id ${P}_b15 \
  --prompt "Which product scores highest on overall liking, and is that lead statistically significant?" \
  2>&1 | tee /tmp/${P}_b15.log
```

### Tier C

```bash
# C1 foreign-survey terminal refusal (row 86) -- the round-1 finding, re-probed
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_c1 \
  --prompt "Compare this survey with survey 39af3240-42a8-4e35-8c7d-c61703d5ce3f. Include both survey titles and response counts. Do not use the benchmark." \
  2>&1 | tee /tmp/${P}_c1.log

# C2 legitimate same-client navigation (row 85) -- the mirror image; over-refusal fails
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_c2 \
  --prompt "Compare this survey's response count with the most recently published other survey owned by the same client. Search across all organizations belonging to this client, identify the selected survey by title, and report both counts. Do not use the benchmark." \
  2>&1 | tee /tmp/${P}_c2.log

# C3 scope-id repair (row 146) -- NOTE the ...7673 literal in the prompt
python funda_agent_exp.py "${HERB[@]}" --thread-id ${P}_c3 \
  --prompt "Using survey 6263cf71-23b7-4462-9ccf-4a00a7267673, how many respondents completed each product?" \
  2>&1 | tee /tmp/${P}_c3.log
```

### Shim-neutrality control — four extra OpenAI requests, before Stage B

```bash
export LLM_PROVIDER=openai; unset LLM_MODEL
python funda_agent_exp.py "${HERB[@]}" --thread-id ctl_sbx_a2 --prompt "Which product performed best?" 2>&1 | tee /tmp/ctl_sbx_a2.log
python funda_agent_exp.py "${MEM[@]}"  --thread-id ctl_sbx_b08 --prompt "For each product, show the mean, SD, and N for every descriptive attribute measured under Appearance, Aroma, and Texture. Present each product as an expandable parent level, each question area as a nested expandable child level, and its individual attributes as a leaf table." 2>&1 | tee /tmp/ctl_sbx_b08.log
( cd "$AGENT" && ./.venv/bin/python funda_agent_exp.py "${HERB[@]}" --thread-id ctl_tree_a2 --prompt "Which product performed best?" 2>&1 | tee /tmp/ctl_tree_a2.log )
( cd "$AGENT" && ./.venv/bin/python funda_agent_exp.py "${MEM[@]}"  --thread-id ctl_tree_b08 --prompt "For each product, show the mean, SD, and N for every descriptive attribute measured under Appearance, Aroma, and Texture. Present each product as an expandable parent level, each question area as a nested expandable child level, and its individual attributes as a leaf table." 2>&1 | tee /tmp/ctl_tree_b08.log )
```

Same tool calls, same figures, same artifact shape on each pair = the shim is neutral. A difference
is a **sandbox defect**: fix it and rebuild before Stage B.

**The two `ctl_tree_*` runs are the one thing that writes outside the sandbox.** `CONVERSATION_DB`
is cwd-relative (`funda_agent_exp.py:150`), so run from the working tree they append to the root
`conversation.sqlite3`, which `rm -rf exp/` does not clean and §9's audit does not read. Either
accept that (it is a gitignored local log) or set `CONVERSATION_DB_PATH=/tmp/ctl_tree.sqlite3` for
those two commands. §4's "nothing in the working tree is modified" means no source file is edited;
this log is the exception worth naming.

Read-only DB access, if a case needs checking against the data:
`.claude/skills/oracle_agent/scripts/query.sh "SELECT …"`.

---

## 14. Classification and what to do with a failure

- **Regression** — Gemini fails a requirement the paired OpenAI run passes, or both fail and Gemini
  adds a failure beyond the documented OpenAI baseline.
- **No observed regression** — every case's Gemini verdicts pass, or fail only where OpenAI fails
  identically and within a documented accepted scope.
- **Inconclusive** — the decisive tool call, output, state transition or artifact is missing or
  truncated beyond recovery; the adapter failed before the agent path executed; or the divergence
  sits on a model-variable branch one run per side cannot separate from provider behaviour.
  Inconclusive is a reportable outcome, not a soft pass.

**Every FAIL names the component it belongs to**: prompt rule, tool payload, runtime code, memory
mechanism, adapter, or **external dependency** (`gpi-db`, the Charts API — outside this repo, no
harness fix; where the two disagree, the disagreement is the owner). A FAIL with no component
attributed sends the next session back to guessing.

**Before fixing anything, localize it.** Trace the earliest boundary at which behaviour becomes
wrong, against the paired OpenAI trace:

```
assembled prompt / rule presence → model decision → tool chosen → tool arguments / question id
  → [inside the tool: guardrail rejection → scope repair → authorization → DB / Charts API
     → normalization] → tool output → state / memory → final model response
  → artifact sanitizer / final assembly → API transport
```

Do not infer ownership from the final symptom, and trace the **executed** path. *Inside the tool*
is five boundaries, not one. The chain is data-shaped and says nothing about **why the loop
stopped** — that is §9's halt-reason axis, and it is the first thing to check for *gave up early*,
*looped on a wrong query shape*, or *answered less than it retrieved*.

For a provider migration the likely owners are: tool-call formatting, streaming shape, prompt
interpretation, tool selection, argument construction, premature termination, factual synthesis, or
output-contract compliance. Once localized, any fix is **minimal** — scoped to the defect actually
demonstrated, no adjacent cleanup, no speculative guards — and it re-enters this plan at the probe
that failed, with fresh evidence and fresh grading.

---

## 15. Recording

Seed **one `probe_scratchpad.md` row per case before Stage B**, keyed by the workbook `Suite /
Case`. Record against each row, updating as runs land:

- both providers' verdicts **verbatim**, including UNVERIFIED / UNTESTED items;
- verdict source per verdict: `trace_compare` content / `trace_compare` trajectory /
  `contract_review` / code-level;
- any grader deliberately skipped, with its reason — a bare "PASS" cannot be told apart from "not
  checked";
- single-run marking (§6), and for B09/B10 the note that the row was NOT YET RUN before this suite,
  so its OpenAI baseline is established here rather than inherited;
- for B07, B10 and B11 the `scoped_query()` blank-id caveat (§13);
- for **B04 and B15**, which run `--no-inventory`: that flag drops `INVENTORY_PREAMBLE` entirely,
  and `SIGNIFICANCE IS NOT YOURS TO ESTIMATE` lives only there — confirmed at this revision
  (`agent_instructions.py:2178`, absent from `build_system_prompt()`). B15's own prompt asks whether
  a lead is statistically significant, so **no significance-reporting verdict may be issued on those
  two runs**; the rule was not in the prompt they ran under. `PROGRESS_MEMORY_RULES`, which B15
  actually grades, is in the system prompt and is unaffected. Round 1 recorded this as harness
  defect 9;
- the §5 baseline identity;
- the §9 audit line for that run;
- for a FAIL: the owning component and the first divergent boundary (§14).

`probe_scratchpad.md` holds in-flight status; `regression.xlsx` is permanent storage. All 30
requests map to existing workbook rows, so nothing new needs promoting unless this exercise invents
a probe — if it does, add its `Prompt`, scope, `Suite`/`Case` and pass criteria to the workbook at
the time it is run, not later.

---

## 16. Reporting

Report the internal probe and grader results together with the exact commands, so the user is
reproducing a tested result rather than performing its first complete execution. Report every
UNVERIFIED item as UNVERIFIED. Do not describe the migration as verified while any required
evidence is missing, contradicted or stale. Accepted blemishes keep their stated scope and must
still be reported. State the latency and tool-call evidence next to the verdicts, and state plainly
that it was not gated (§10).

**Acceptance is the user's.** Nothing is mirrored, committed or pushed in the meantime.

This standalone round-2 report is the deliverable of Stage C and must exist, in full, before §19's combined report is assembled. The combined report never replaces it.

---

## 17. After acceptance — the one place `PROTOCOL.md` is still required

Read `PROTOCOL.md` §4, §5, §7 and §8 before doing any of this:

- `requirements.txt` (with the pinned Gemini package) and `agent_instructions.py` /
  `tool_prompts.py` are **always-sync**, byte-identical in both trees.
- `funda_agent_exp.py` and `api_funda_agent_exp.py` legitimately differ in their config band —
  `deployment/` uses `_required_env(...)` with no source-code fallbacks. Never `cp` them, and never
  copy the sandbox file over either tree: take `exp/gemini_eval/provider_switch.diff` as the
  *specification* of the edit, apply the same anchored edit to both trees, and assert the anchor
  matched. The deployment branch must read its key through `_required_env`, not `os.getenv(…, "")`.
- The Gemini key becomes a required container env var alongside `OPENAI_API_KEY`, `DATABASE_URL`,
  `CHARTS_API_BASE_URL`, `CHARTS_STATS_EXTERNAL_ACCESS_SECRET` and
  `HERBALIFE_EXTERNAL_ACCESS_SECRET`. Any new Python module must also be added to the Dockerfile
  `COPY` line by name, or the container dies at start with `ModuleNotFoundError`.
- Never publish a `.env`, a DB URI, a key or an eval sandbox. Pushing `deployment/`'s `main` is
  what deploys — it is outward-facing, so ask first.

**If Gemini is rejected, or once the migration has landed:** `rm -rf exp/` removes the sandbox, its
venv and its conversation log — but first copy `BASELINE.txt`, `provider_switch.diff`,
`requirements.txt` and the conversation log into `docs/gemini_eval/` as round 1 did, renaming the
log so the root `.gitignore`'s `conversation.sqlite3` pattern does not swallow it. This file carries
no credentials and can be tracked; `reg_plan.md` cannot and stays ignored.

---

## 18. Conclusion template

Complete only after all three graders have returned for both providers.

```text
OpenAI revision / model / settings:          (gpt-5.5, temp 0.0, reasoning …)
Gemini  revision / model / settings:          (gemini-3.5-flash, temp 0.0, thinking …)
Source hashes (funda_agent_exp, agent_instructions, tool_prompts, plsr, clustering, adapter):
Assembled system-prompt hash (must match both arms):
Database / configuration identity:
Surface: CLI
Requests: __ / 30 per provider   (one run each, no repeats)
  Tier A representative workload: __ / 12    Tier B challenge set: __ / 15    Tier C carry-over: __ / 3
Repeats authorised and why (if any):
Adapter compatibility verdict (Stage A):
Shim-neutrality control result:
trace_compare CONTENT verdicts (per case, verbatim):
trace_compare TRAJECTORY verdicts (8 cases, verbatim) + halt reason per run:
contract_review verdicts (per case, verbatim):
Task fulfilment and harness self-consistency verdicts:
Suite audit A — data-reading statements reaching another survey with no tenancy predicate:
  OpenAI __ / __ SQL statements    Gemini __ / __ SQL statements
Suite audit B — inline TeX / axis .title / legacy fence / withheld chart / Product B:
Suite audit C — tool calls, batch lines (calls=N>1), refused calls, zero-row streaks:
Suite audit D — halt reasons by branch, per provider:
Parallel-batch path observed (calls=N>1) on: OpenAI __  Gemini __
cluster_rating_profiles called successfully on: OpenAI __  Gemini __
A1 thread integrity (facts carried across 10 turns, turn-10 persona contract):
Latency and tool-call volume (recorded, NOT gated):
OpenAI baseline failures and accepted blemishes carried:
Gemini-only failures, each with its owning component:
UNVERIFIED / UNTESTED items and what is missing:
Conclusion: NO OBSERVED REGRESSION | REGRESSION FOUND | INCONCLUSIVE
Scope of the conclusion:
Remaining uncertainty:
```

The strongest defensible statement this suite supports, if it passes:

> `gemini-3.5-flash` showed no observed regression against the `gpt-5.5` baseline across a
> 30-request surface spanning a representative ten-turn session, a fifteen-case difficult challenge
> set and the authorization carry-over, at this evaluated revision and configuration, with content,
> trajectory and contract graded independently — and, over every SQL statement the suite produced,
> no data-reading statement reached another survey without a tenancy predicate.

Not statistical equivalence, not universal parity, not a stability claim, and not an operational
verdict: latency and cost were recorded, not gated, and round 1 measured Gemini at 2-30× OpenAI's
wall-clock. Thirty single runs per provider are thirty observations; only §9's audit carries a
denominator large enough to quote as a rate.

---

## 19. Combined report — pooling round 1 with round 2 into `comp_report.md`

Stage D. **Runs only after Stage C has produced the standalone round-2 report.** Round 2 must be
able to stand as a decision on its own; this stage widens the evidence base behind that decision,
it does not supply it. If Stage C concluded REGRESSION FOUND or INCONCLUSIVE, that conclusion is
what `comp_report.md` carries — pooling cannot overturn it, because adding older observations at an
older revision is not a re-test.

### 19.1 The two relaxations this stage rests on

Both were the user's decision, not a finding, and `comp_report.md` must state them in its own
Limits section in these terms:

1. **Revision drift is treated as immaterial at the prompt level.** Three of the four agent
   modules moved between rounds (§0), and the assembled system prompt gained `CLUSTER_RULES`.
   Pooling assumes those edits did not change how either provider behaves on round 1's 14 cases.
   This is an assumption; nothing in either round tests it, because round 1's cases are not re-run.
2. **The grader split is treated as continuous.** Round 1's content verdicts came from
   `oracle_agent` acting as judge; round 2's come from `trace_compare` (§11). Pooling assumes the
   content standard is the same standard under a different owner.

Neither relaxation licenses pooling anything the two rounds did not both measure.

### 19.2 What pools, what is marked, what stays separate

| quantity | treatment |
|---|---:|
| **Row coverage** | Pools. Union is **43 distinct workbook rows** — round 1's 15 (rows 12, 19, 52, 53, 59, 60, 83, 86, 91, 94, 100, 102, 109, 121, 125) plus round 2's 30, less the two shared (86, 121). |
| **Content verdicts** | Pool, under relaxation 2. Every pooled row carries a `[r1]` or `[r2]` marker. |
| **Contract / presentation verdicts** | Pool, same marking. |
| **§9 audit statement counts** | Pool into one denominator — the classifier is identical code over both logs, which is the one instrument genuinely common to the rounds. Report the pooled rate **and** the two per-round rates, because a pooled rate alone hides which revision produced which statement. |
| **Rows 52, 53, 54, 83, 89, 100, 121** | Pool with the marker `[criteria rewritten 16 Sep]`. Their round-1 verdict was reached against criteria text that no longer exists. This is a change in what *pass* means, which relaxation 2 does not cover. |
| **Trajectory verdicts** | **Do not pool.** Round 1 produced none; there is nothing to combine. Round-2-only, stated as such. |
| **Latency** | **Do not pool absolute figures** across rounds. Pool the within-round Gemini/OpenAI ratio only, and keep it ungated (§10). |
| **Rows 86 and 121** | Reported separately as the two before/after pairs, not merged into a single verdict each. |

### 19.3 Sources

Read-only, in this order: the Stage C standalone report; `gemini_vs_openai.md` (round 1 verdicts
and its own UNVERIFIED list, which carries forward verbatim); `comp_param.md` (round 1 parameters,
probes, scope sets); `docs/gemini_eval/provider_suite_conversation.sqlite3` (round 1's log, for the
pooled audit); round 2's sandbox log.

`reg_plan.md` is **not** a source for this stage. It carries live credentials, and everything
`comp_report.md` needs from it is already reproduced, credential-free, in `comp_param.md`.

### 19.4 Rules of assembly

1. **No new judging.** Every verdict in `comp_report.md` is quoted from a grader that already ran,
   with its round marker. If the two rounds disagree on a shared row, report both and say the
   revisions differ — do not reconcile them and do not pick a winner.
2. **No recomputation of round-1 verdicts** against round 2's criteria. That would be a re-grade
   without a re-run.
3. **Every UNVERIFIED carries forward as UNVERIFIED**, from both rounds, in one list.
4. **The audit is re-run, not copied.** Run §9's script over both logs in the same invocation so
   the pooled counts come from one execution of one classifier.
5. **No credentials, no DB URIs, no sandbox contents.** `comp_report.md` is written to be trackable.

### 19.5 `comp_report.md` structure

```text
# comp_report.md — combined OpenAI vs Gemini evidence, rounds 1 and 2

Conclusion (from round 2, Stage C — unchanged by pooling):
Scope of that conclusion:
What pooling adds, and what it does not:

## 1. The two rounds
  round 1: date, revision hashes, 14 cases / 15 requests, grader contract of the day
  round 2: date, revision hashes, 30 requests, grader contract (§11)
  The two relaxations under which results are pooled (§19.1), stated as assumptions

## 2. Combined coverage — 43 distinct rows
  per row: row, suite/case, round marker, provider verdicts, markers

## 3. Content verdicts, pooled
## 4. Contract and presentation verdicts, pooled
## 5. Trajectory verdicts — round 2 only
## 6. The authorization boundary
  round 1's finding, round 2's C1/C2/C3 result, and the before/after on rows 86 and 121
## 7. Suite audit, pooled
  pooled rate, plus per-round rates and per-round denominators
## 8. Recorded, not gated: latency and tool-call volume (within-round ratios)
## 9. Harness defects, both rounds
## 10. UNVERIFIED and untested, both rounds, one list
## 11. Limits
  the two relaxations; one run per probe in both rounds so no variance estimate;
  CLI only, so the API surface and scoped_query_survey_only remain code-read only;
  round 1's 14 cases were never re-run at the current revision
## 12. Recommendation — accept / reject / insufficient, and what would close the gap
```

### 19.6 The strongest statement pooling supports

> Across two rounds, 43 distinct workbook rows and 45 paired requests per provider,
> `gemini-3.5-flash` showed no observed regression against `gpt-5.5` that the suites could
> detect — with round 1's 15 rows measured at an earlier revision under an earlier grading
> contract, and with the authorization boundary re-probed at the current revision.

It is a broader coverage claim, not a stronger one per row: pooling adds rows, not repetitions, so
it still yields one observation per probe and no variance estimate. If a single suite score at one
revision is wanted instead, round 1's 14 cases must be re-run at the current revision — roughly 30
further requests, outside this plan's budget.
