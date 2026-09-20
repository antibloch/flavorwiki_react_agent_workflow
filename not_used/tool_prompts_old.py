"""All model-facing text associated with the agent's data tools.

Structured by direction:
  1. tool descriptions sent to the model with tool schemas
  2. SQL/statistics feedback returned to the model by tools
  3. generic tool-dispatch failures
"""

import re


# ============================================================================
# 1. Tool descriptions
# ============================================================================

GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION = """Return the complete analysis packet for one resolved survey.

    Use this after another survey's id has been resolved, instead of spending one SQL turn
    listing candidate questions and another SQL turn aggregating the selected question. The
    fixed benchmark survey id is already in your instructions, so benchmark work can call this
    tool directly. For the current survey, prefer the pre-fetched inventory already in the
    conversation when it is present.

    The packet has the same JSON contract as the startup inventory: products,
    scored_measures_by_product, other_answered_measures, and benchmark_context. Read every
    candidate, choose the question by meaning from its full prompt, and compare its available
    type/scale/attribution signature before using cross-survey figures. benchmark_context already
    resolves benchmark assignment/configuration, so do not query it again. The common per-product
    aggregates are already computed; do not re-query them. Use nl2sql_tool only when the packet is
    unavailable or the requested analysis is not among those aggregates.

    survey_id must be a UUID already established from the conversation or a prior scoped survey
    lookup. Never invent or copy an id from memory. The server permits only the current survey,
    another survey in the current organization, or the configured fixed benchmark survey.
    """


ANALYSIS_PACKET_RESULT_HEAD = "ANALYSIS PACKET FOR SURVEY"


def analysis_packet_preamble(survey_title: str) -> str:
    return (
        f'{ANALYSIS_PACKET_RESULT_HEAD} "{survey_title}".\n'
        "This is the complete, cached-compatible inventory for that survey. Choose from the full "
        "candidate list and answer from its common aggregates where sufficient; do not re-query "
        "the same catalog or aggregates.\n"
    )


def packet_bad_survey_id(survey_id: str) -> str:
    return f"Analysis packet unavailable: survey_id must be a UUID, got {survey_id!r}."


PACKET_SCOPE_UNAVAILABLE = (
    "Analysis packet unavailable: the run's survey scope is not established, so access cannot "
    "be authorized."
)

PACKET_SCOPE_CHECK_FAILED = (
    "Analysis packet unavailable: survey ownership could not be verified. Do not retry with a "
    "different id; use only a survey resolved inside the current scope."
)


def packet_unknown_survey(survey_id: str) -> str:
    return f"Analysis packet unavailable: no survey exists for id {survey_id}."


def packet_survey_out_of_scope(survey_id: str) -> str:
    return (
        f"Analysis packet refused: survey {survey_id} is outside the current organization and is "
        "not the configured benchmark survey."
    )


def packet_not_available(survey_title: str) -> str:
    return (
        f'Analysis packet unavailable for survey "{survey_title}" (a database error occurred '
        "or the complete packet exceeded the safe size limit). Fall back to the "
        "scoped two-query candidate-and-aggregate workflow."
    )

NL2SQL_TOOL_DESCRIPTION = """Run one read-only SQL query against the survey PostgreSQL database.

    more_sql_expected: set True if, after seeing these rows, you will probably need
    another SQL query before you can answer; set False if this is the last data you
    need and your next step is writing the final answer. Judge it honestly -- it
    only affects internal routing and never constrains what you may do next.

    information_gathered and information_still_needed are REQUIRED on every call, and
    are a running ledger, not a one-off note: before you write this call, a "PROGRESS
    TRACKER" message shows you the exact information_gathered/information_still_needed
    text you reported on your OWN previous call. Read it, then write the UPDATED
    version here -- fold in what the previous still-needed items resolved to now that
    you have their data, and state what remains open after THIS query. Do not simply
    repeat the previous call's text unchanged unless this query genuinely left nothing
    resolved; that is treated as a stall, not real progress.
    Keep BOTH concise and structured as notes, not prose -- no preamble and no
    restating the SQL. Accuracy takes priority over brevity: preserve every key figure,
    resolved requirement and open gap needed to plan the next query.
    - information_gathered: which parts of the question are answered so far (across
      ALL calls so far, not just this one), with the key figures you are relying on.
    - information_still_needed: which parts are still open AFTER this query's rows are
      in hand, and what data would close them. Write "nothing" when this query
      finishes the job. Do NOT list this query's own output as still needed -- you are
      describing the state once it returns, not the state before it runs. ("Need
      results", "need the returned rows" is always wrong here: those are arriving.)
    Keep them concrete and specific to this question. Getting them wrong can cause
    an unnecessary query or leave part of the request unanswered.

    Pass a single complete SELECT statement (CTEs are fine; no semicolon-separated
    batches). The transaction is read-only, so INSERT/UPDATE/DELETE/DDL will fail --
    this tool observes data, it never changes it.

    When several independent facts are already known to be needed, issue their
    nl2sql_tool calls together in the SAME assistant turn. Parallel calls are allowed
    only when every query can be written completely from information already in the
    conversation. Never parallelize a discovery query with a query that needs an id,
    label or value returned by that discovery -- wait for the result, choose from it,
    and make the dependent call on the next turn. In one parallel set, give every call
    the same more_sql_expected and information_still_needed assessment describing what
    will remain after the WHOLE set returns. If the set finishes the job, every call's
    information_still_needed MUST be exactly "nothing" -- do not wrap it in a sentence
    such as "after both queries return, nothing else is needed."

    Write PostgreSQL dialect against the schema in your instructions, and:
    - Double-quote camelCase identifiers exactly as the schema spells them
      ("surveyId", "blindingNumber"); unquoted names fold to lowercase and error.
    - Always constrain the query to the survey it is about: :survey_id for this
      one, the resolved id of a sibling inside :organization_id, or the benchmark
      ids. Reaching another survey in your organization is allowed; an UNSCOPED
      query is not -- it silently aggregates every survey in the database.
      Reference ids as the bound placeholders :client_id, :organization_id and
      :survey_id rather than retyping the literal values -- a mistyped id is a
      wasted turn, and the placeholders cannot be mistyped.
    - Obey the PostgreSQL dialect limits listed in your instructions; the query is
      parsed by the planner before it runs, so a rejected construct costs a turn.
    - Aggregate in SQL (COUNT, AVG, GROUP BY) rather than fetching raw rows and
      counting them yourself -- it is faster and the answer comes back smaller.
    - When coding verbatims into themes, theme membership is multi-label: one response
      may count in every matching theme. Use independent COUNT(...) FILTER clauses or
      UNION ALL followed by COUNT(DISTINCT respondent); never use one CASE expression
      that assigns each response only to its first matching theme. Say when theme counts
      overlap and therefore need not sum to the response base. Keep each theme semantically
      coherent; do not pool unrelated cues into one broad bucket merely to raise its count.
    - Filter on STRUCTURE, never on MEANING. SQL may narrow by "typeOfQuestion",
      has-answers, scope ids; it may NOT pattern-match question.prompt or
      question_option.label against the concept the user asked about
      (LIKE/ILIKE/SIMILAR TO/~ on those columns is rejected outright). Deciding
      which question means "overall liking" is YOUR judgement, not a WHERE clause --
      and a pattern hides every question it missed, so you cannot tell a concept that
      is absent from one that is merely worded differently.
      When no startup inventory or on-demand analysis packet is available and you must identify
      which question measures a concept, spend two queries:
      (1) list candidates structurally -- id, FULL prompt (never truncated: the words
      that distinguish one measure from another are often at the END of a long prompt),
      "typeOfQuestion", scale (jsonb_array_length(question_option."optionSettings"->
      'positionLabels') -- join question_option; that key is NOT on question.settings, which
      never holds it, so reading it there silently returns NULL --, plus the
      observed MIN/MAX), respondent count, and COUNT(answer.product_id) as the
      attributable count -- then choose from the prompts you get back; (2) aggregate
      only the id(s) you chose. Two small queries beat one wrong one; do not spend more
      than two on identification.
      Report attributability, do not filter on it: write COUNT(a.product_id) as a
      COLUMN rather than putting `a.product_id IS NOT NULL` in the WHERE clause. A
      measure that exists but is not attributable to a product is a real finding the
      user needs (say it exists and cannot be linked); filtering it out hides it and
      makes "no such measure exists" indistinguishable from "it exists but cannot be
      tied to a product". Never aggregate a measure whose attributable count is 0 as
      though it were per-product -- name it, and say why it cannot be used.
    - Keep identity and spread attached to every statistic: GROUP BY the question id
      (not just the product) and return COUNT plus STDDEV beside every AVG. Two
      questions can share a prompt and use different scales -- if their scale or
      observed MIN/MAX differ they are different instruments, so report them
      separately and never average across them. Without STDDEV you cannot tell a real
      difference from noise, so a mean with no spread beside it is an unfinished
      answer.

    A 0-row result is as often an empty JOIN as a wrong filter, so diagnose before you
    rewrite; the tool tells you how when it happens.

    Returns a JSON array of row objects, e.g. [{"name": "Product 1", "n": 42}].
    If the result is too large to inline, it is stored instead and you get a manifest
    with a result_id, the row count and the column list -- then use describe_result /
    query_result / slice_result to inspect it. When that happens, prefer re-running
    this tool with GROUP BY / aggregates over paging through raw rows.
    On failure it returns an error string starting with "SQL error:" -- read it,
    correct the query, and call again. Queries are killed after 20 seconds.
    """


# run_survey_stats stays deliberately short. Question ids come from the scoped inventory or
# discovery contract; the tool validates UUID shape and calls the Charts API directly.
RUN_SURVEY_STATS_DESCRIPTION = """Run statistical tests (anova, tukey, pearson, spearman, chi-square) on a survey question.

    question_id: a UUID from the pre-fetched inventory or an nl2sql_tool
    result, never from memory. pearson/spearman/penalty also need
    reference_question_id, a second question from the SAME survey.
    """


ANALYZE_FEATURE_INFLUENCE_DESCRIPTION = """Find which numeric survey features influence a numeric target.

    Use this for driver/influence/importance questions such as whether aroma, flavor, or
    texture most influences overall liking. Pass the target and any USER-NAMED candidate
    features in one call; the server fetches and aligns their raw responses, so never retrieve
    those rows with nl2sql_tool and never call this tool separately for each feature.

    Every question_id must come from the complete inventory/packet or a prior structural
    nl2sql_tool discovery result, never from memory. Each variable has question_id and an
    optional component_label. For a line-scale, supply the exact component when the user named
    one; OMIT it when the user named the whole question. An omitted target component analyzes
    every component separately--it never invents an average score. An omitted feature component
    includes every component as a separate candidate. If the user names no candidate features,
    pass features=[]: the server uses every other compatible numeric measure in the target
    survey and excludes the target question itself. V1 supports vertical-rating and line-scale
    variables at one shared respondent or respondent-product grain.

    Put all K candidates in one additive linear model; this is not a combinatorial subset
    search. If the user gives no alpha, omit it and let the server automatically use 0.05--do
    not ask. If the user explicitly gives alpha, pass that value unchanged. The server tests
    the candidate set globally, tests each feature conditional on the other candidates, and
    applies Holm family-wise correction across the response. A feature belongs to the reported
    subset only when the global model and that feature's adjusted p-value pass alpha. The subset
    can be empty. The result can also say the set matters jointly while individual attribution
    is unresolved, or that data is insufficient. Preserve those outcomes; never promote a
    near-miss. Explain adjusted p-values and direction only if useful, in ordinary language.
    This is statistical association in this survey, not proof of causation.
    """


FEATURE_INFLUENCE_RESULT_HEAD = "FEATURE INFLUENCE ANALYSIS COMPLETE"


# ============================================================================
# 2. Tool feedback
# ============================================================================

# Pattern-matching a concept against question.prompt / question_option.label decides
# MEANING inside SQL, and the same WHERE clause then destroys the evidence needed to check
# the decision: whatever the pattern excluded is indistinguishable from what does not exist.
# Measured: on the 143-question PepsiCo survey `prompt ILIKE '%overall%' AND ILIKE '%lik%'`
# returned 3 of the 4 liking-type questions and silently dropped the pre-tasting expectation
# item ("...how much do you EXPECT you are going to like this product?") -- it has no
# "overall" in it. The agent answered correctly only because it never saw the one question it
# could have confused. Structural filters ("typeOfQuestion", product-linked, has answers) are
# the right narrowing; the model does the semantic narrowing on the prompts that come back.
#
# What exactly the rejection fires on is _SEMANTIC_FILTER_RE in funda_agent_exp.py; this is
# only what the model is told when it does.
SEMANTIC_FILTER_REASON = (
    "do not pattern-match question.prompt or question_option.label against the concept you "
    "are looking for (no LIKE/ILIKE/SIMILAR TO/~ on those columns) -- that decides meaning "
    "inside SQL and hides every question the pattern missed. If no complete inventory/packet is "
    "available, use two queries instead: "
    "(1) list the candidate measures with a STRUCTURAL filter only -- \"typeOfQuestion\" in "
    "the rating/matrix/ranking types for this survey, product-linked, has answers -- "
    "returning question id, prompt, \"typeOfQuestion\", scale "
    "(jsonb_array_length(question_option.\"optionSettings\"->'positionLabels') -- that key is "
    "on question_option, NOT on question.settings, which never holds it -- plus observed "
    "MIN/MAX) and "
    "respondent count, then pick the right question yourself from the prompts you get back; "
    "(2) aggregate only the id(s) you chose, GROUP BY that question id, returning COUNT and "
    "STDDEV beside every AVG. If you already have the exact prompt text from step 1, "
    "prompt = '<exact text>' is fine -- it is only the pattern match that is rejected"
)


#---------- (2)+(3) dialect rejections: name the fix instead of blaming the schema ----------
# Every pattern here was reproduced against this database. The previous generic tail
# ("Re-check table/column names against the schema") was actively misleading: in none of
# these cases is any table or column name at fault, so it sent the model looking in the
# wrong place and cost a full strong-model turn each time.
SQL_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (r"DISTINCT is not implemented for window functions",
     "Postgres has no COUNT(DISTINCT x) OVER (...). Aggregate at the finer grain in a CTE "
     "with GROUP BY, then aggregate that CTE again at the coarser grain."),
    (r"aggregate function calls cannot contain window function calls",
     "A window call cannot sit inside an aggregate. Compute the window in a CTE, then "
     "aggregate that CTE's output in the outer query."),
    (r"aggregate function calls cannot be nested",
     "avg(count(*)) is invalid. Compute the inner aggregate in a CTE, then aggregate it."),
    (r"aggregate functions are not allowed in (WHERE|GROUP BY|JOIN)",
     "An aggregate cannot appear there. Use HAVING for conditions on aggregates, GROUP BY "
     "the plain column, or compute the aggregate in a CTE and filter/join over that."),
    (r"function (avg|sum|min|max|stddev\w*)\((text|character varying|uuid)\) does not exist",
     "You are aggregating a text/uuid column. Cast first and exclude non-numerics, e.g. "
     "AVG(NULLIF(v,'')::numeric) guarded by v ~ '^-?[0-9]+(\\.[0-9]+)?$'."),
    (r"operator does not exist: (uuid|text|character varying|integer|numeric) [=<>]+ "
     r"(uuid|text|character varying|integer|numeric)",
     "Type mismatch across the comparison. Cast one side explicitly, e.g. "
     "a.id = b.some_text::uuid (or a.id::text = b.some_text)."),
    (r"(missing|invalid reference to) FROM-clause entry for table",
     "A subquery cannot see an alias from an enclosing FROM at that position. Either join "
     "that table inside the subquery, or lift the subquery into a CTE and JOIN it."),
    (r"SELECT DISTINCT ON expressions must match initial ORDER BY",
     "DISTINCT ON (x) requires the ORDER BY to begin with x."),
    (r"must appear in the GROUP BY clause",
     "Add the column to GROUP BY, or wrap it in MIN()/MAX() if it is functionally dependent."),
    (r"invalid input syntax for type uuid",
     "That id is not a valid UUID -- do not retype ids. Use the :client_id, "
     ":organization_id and :survey_id placeholders, which are bound for you."),
)

# Long single statements are where dialect edges get hit; the single-statement rule pushes
# the model toward them, so the counter-pressure is applied where it is cheap -- on failure
# only, so a large query that works is never penalised.
BIG_SQL_CHARS = 2000


def error_hint(message: str) -> str:
    for pattern, hint in SQL_ERROR_HINTS:
        if re.search(pattern, message, re.I):
            return f"\nHINT: {hint}"
    return ""


def sql_failure(message: str, sql: str, stage: str = "") -> str:
    where = f" ({stage})" if stage else ""
    tail = error_hint(message)
    if len(sql) > BIG_SQL_CHARS:
        tail += (
            f"\nNOTE: this statement is {len(sql)} chars. Long single statements fail far more "
            "often than short ones. You have several turns -- split this into smaller queries "
            "and build up the answer step by step."
        )
    if not tail:
        tail = "\nRe-check table/column names against the schema."
    return f"SQL error{where}: {message}{tail}\nCorrect the query and retry."


ZERO_ROW_HEAD = "Query succeeded but matched 0 rows"

# An empty result is as often an empty JOIN as a wrong filter. The old text ("Loosen the
# filters or check the id values") pointed at the ids, so a run whose ids were correct
# and whose join could never match spent ten turns loosening a query that was already
# right. This wording is deliberately question-agnostic: it names the diagnostic, not a
# table or a domain.
ZERO_ROW_MSG = (
    f"{ZERO_ROW_HEAD}. The ids are not necessarily wrong -- an empty result is just as "
    "often an empty JOIN as a bad filter. Before loosening scope or rewriting variants of "
    "this query, run ONE diagnostic that counts each side of every join independently and "
    "counts how many join-key values the two sides actually share (COUNT(DISTINCT key) per "
    "side, plus the COUNT of keys present in both). If a side is non-empty on its own but "
    "the shared-key count is 0, the two things you are trying to cross are not linked at "
    "that grain in this data: report that as the finding, with the per-side counts, and "
    "answer what the data does support -- do not keep retrying the join. On that diagnostic "
    "call, describe the state AFTER its counts return. If those counts will settle whether "
    "the join is possible, set information_still_needed exactly to 'nothing' and "
    "more_sql_expected to false; do not list the diagnostic's own results as still needed."
)


# Prepended by nl2sql_tool when a near-miss scope-id literal was silently corrected.
def scope_repair_note(repairs: list[str]) -> str:
    return (
        "[scope-id repair: " + "; ".join(repairs) + ". The ids are supplied to you -- write "
        ":client_id / :organization_id / :survey_id instead of retyping them.]\n"
    )


# Advisory only -- see _scope_note() in funda_agent_exp.py, which decides when it fires.
# A statement constrained to nothing is the one failure class here with no symptom: it
# does not error, it just aggregates every survey in the database.
SCOPE_WARNING = (
    "[scope warning: this query references none of :client_id / :organization_id / "
    ":survey_id (nor any id literal), so nothing constrains it to this run's survey -- "
    "every figure it returns may mix data across surveys and clients. If reaching outside "
    "this survey is deliberate, constrain it to :organization_id at least, and say in your "
    "answer which surveys the numbers cover. Otherwise add the scope filter and re-run.]\n"
)


# The two structural rejections from _reject_reason(), phrased as the instruction to follow
# rather than as a complaint. They reach the model wrapped in multi_statement_error().
REJECT_BATCH = "pass exactly one statement -- semicolon-separated batches are not run"
REJECT_NOT_SELECT = "pass a single read-only SELECT (or WITH ... SELECT) statement"


# psycopg2 returns only the LAST result set of a batched statement, so a batch silently
# discards earlier results -- rejected before execution rather than run.
def multi_statement_error(reason: str) -> str:
    return f"SQL error: {reason}. Rewrite as one query (use JOINs or CTEs) and retry."


NON_SELECT_RESULT = (
    "That statement returned no rows. This tool is read-only: use SELECT queries."
)


#---------- run_survey_stats ----------
# Argument-validation refusals, raised before the Charts API is called. Each one names what
# to do instead, because the model cannot see why the call was refused otherwise -- and a
# stats call it cannot fix is a call it will invent numbers around (measured: asked outright
# to run ANOVA and Tukey with the tool unreachable, it produced a full Tukey letter table for
# a test that never ran).
def stats_bad_question_id(question_id: str) -> str:
    return (f"Error: '{question_id}' is not a valid question UUID. "
            "Pick an id from the provided question list.")


def stats_missing_reference(stats_types: str) -> str:
    return (
        f"Error: stats_types={stats_types} requires reference_question_id "
        "(pearson/spearman/penalty all need a second question to compare against)."
    )


def stats_bad_reference_id(reference_question_id: str) -> str:
    return f"Error: reference_question_id '{reference_question_id}' is not a valid UUID."


# Transport failures. "Try again" is deliberate on the timeout: the Charts API is flaky
# rather than absent, and a retry often succeeds.
STATS_TIMEOUT = "Error: Charts API timed out. Try again."


def stats_request_failed(exc: object) -> str:
    return f"Error: Charts API request failed: {exc}"


def stats_http_error(status_code: int, body: str) -> str:
    return f"Error ({status_code}): {body}"


# ============================================================================
# 3. Generic tool-dispatch failures
# ============================================================================

def unknown_tool(name: str) -> str:
    return f"Unknown tool: {name}"


def tool_error(exc: BaseException) -> str:
    return f"Tool error: {type(exc).__name__}: {exc}"


__all__ = ['ANALYSIS_PACKET_RESULT_HEAD',
 'BIG_SQL_CHARS',
 'GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION',
 'NL2SQL_TOOL_DESCRIPTION',
 'NON_SELECT_RESULT',
 'PACKET_SCOPE_CHECK_FAILED',
 'PACKET_SCOPE_UNAVAILABLE',
 'REJECT_BATCH',
 'REJECT_NOT_SELECT',
 'RUN_SURVEY_STATS_DESCRIPTION',
 'SCOPE_WARNING',
 'SEMANTIC_FILTER_REASON',
 'SQL_ERROR_HINTS',
 'STATS_TIMEOUT',
 'ZERO_ROW_HEAD',
 'ZERO_ROW_MSG',
 'analysis_packet_preamble',
 'error_hint',
 'multi_statement_error',
 'packet_bad_survey_id',
 'packet_not_available',
 'packet_survey_out_of_scope',
 'packet_unknown_survey',
 'scope_repair_note',
 'sql_failure',
 'stats_bad_question_id',
 'stats_bad_reference_id',
 'stats_http_error',
 'stats_missing_reference',
 'stats_request_failed',
 'tool_error',
 'unknown_tool']
