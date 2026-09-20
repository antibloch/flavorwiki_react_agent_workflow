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
    benchmark survey id comes from benchmark_context.assigned_benchmark in the startup
    inventory, so benchmark work can call this tool directly. For the current survey, prefer the pre-fetched inventory already in the
    conversation when it is present.

    The packet has the same JSON contract as the startup inventory: products,
    scored_measures_by_product, catalog, and benchmark_context, with both measure sections
    columnar (a `columns` header plus positional `rows`). The catalog holds every configured
    question of that survey. Read every candidate, choose the question by meaning from its full
    prompt, and compare its available type/scale/attribution signature before using cross-survey
    figures. benchmark_context already
    resolves benchmark assignment/configuration, so do not query it again. The common per-product
    aggregates are already computed; do not re-query them. Use nl2sql_tool only when the packet is
    unavailable or the requested analysis is not among those aggregates.

    Do NOT use this tool for survey titles, dates, publication state, enrollment counts,
    response counts, or answer-row counts. Those are survey metadata/count requests and belong
    together in one nl2sql_tool query; do not call this packet in parallel with that query.

    survey_id must be a UUID already established from the conversation or a prior scoped survey
    lookup. Never invent or copy an id from memory. The server permits only the current survey,
    another survey owned by the initial survey's client, or the benchmark survey assigned to
    the initial survey's benchmark category. Client ownership is derived again in code from the initial survey id.
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
        f"Analysis packet refused: survey {survey_id} is outside the initial survey's client and is "
        "not the benchmark assigned to this survey's category. TERMINAL: make no further tool call, do not fetch "
        "only the current side or substitute another survey, and answer in one or two sentences "
        "without repeating the UUID, adding a table, or mentioning the benchmark unless asked."
    )


# The reason is stated because the right next move differs per reason: a data-layer failure is
# worth one retry, a survey with no responses is a finished answer, and an over-budget packet
# means the data is there and needs a scoped query. One message for all three told the model
# nothing it could act on.
_PACKET_UNAVAILABLE_REASONS = {
    "unavailable": (
        "a database error occurred. This says nothing about the survey itself; the questions and "
        "responses may well exist"
    ),
    "not_found": "no survey row was returned for that id",
    "empty": (
        "the survey holds no questions and no benchmark configuration. That is a complete "
        "answer, not a failure -- report it and do not query further"
    ),
    "degraded": (
        "the packet exceeded the safe size limit even with every option label dropped. The data "
        "is there; it is too large to inline"
    ),
}


def packet_not_available(survey_title: str, status: str = "unavailable") -> str:
    reason = _PACKET_UNAVAILABLE_REASONS.get(status, _PACKET_UNAVAILABLE_REASONS["unavailable"])
    suffix = (
        "" if status == "empty"
        else " Fall back to the scoped two-query candidate-and-aggregate workflow."
    )
    return f'Analysis packet unavailable for survey "{survey_title}" ({reason}).{suffix}'


GENERATE_WORD_CLOUD_DESCRIPTION = """Build the word-cloud artifact for one resolved open-ended question.

    Use this tool for every word-cloud, tag-cloud, most-common-words or word-frequency request.
    Do not use nl2sql_tool to tokenize answers or count terms. Select question_id only from the
    current inventory or a previously retrieved authorized survey packet; never invent it.

    The backend validates ownership and question type, retrieves the complete response set,
    redacts email/URL material, tokenizes deterministically, removes the fixed function-word and
    subject-term sets, counts DISTINCT answers containing each term, sorts deterministically, and
    builds the exact gpi-chart word_cloud version-1 artifact. Raw answers are never returned to you.

    Set group_by_product=true only when the user explicitly asks to compare product word clouds;
    the tool will refuse grouping when the answers are not product-linked. max_terms may be 30-60
    and defaults to 50. After a successful call, do not call another tool and do not recreate,
    quote, summarize, edit or manually emit the chart JSON: the runtime appends the trusted fenced
    artifact to your final response without allowing model-generated counts to replace it.
    """


WORD_CLOUD_SCOPE_UNAVAILABLE = (
    "Word cloud unavailable: the run's survey scope is not established, so the question cannot "
    "be authorized."
)


def word_cloud_bad_question_id(question_id: str) -> str:
    return (
        f"Word cloud unavailable: {question_id!r} is not a valid question UUID. "
        "Choose an open-ended question id from the survey inventory."
    )


def word_cloud_question_unavailable(reason: str) -> str:
    return f"Word cloud unavailable: {reason}."


def word_cloud_ready(question: str, answer_count: int, grouped: bool) -> str:
    grouping = "grouped by product" if grouped else "not grouped"
    return (
        f"Trusted word-cloud artifact prepared for {question!r} from {answer_count} non-blank "
        f"answer(s), {grouping}. Do not reproduce its JSON or counts; the runtime will append "
        "the exact artifact to the final response."
    )


UNTRUSTED_WORD_CLOUD_BLOCK = (
    "A word-cloud block was withheld because it was not produced by the deterministic "
    "word-cloud tool."
)

NL2SQL_TOOL_DESCRIPTION = """Run one read-only SQL query against the survey PostgreSQL database.

    Pass a single complete SELECT statement (CTEs are fine; no semicolon-separated
    batches). The transaction is read-only, so INSERT/UPDATE/DELETE/DDL will fail --
    this tool observes data, it never changes it.

    When several independent facts are already known to be needed, issue their
    nl2sql_tool calls together in the SAME assistant turn. Parallel calls are allowed
    only when every query can be written completely from information already in the
    conversation. Never parallelize a discovery query with a query that needs an id,
    label or value returned by that discovery -- wait for the result, choose from it,
    and make the dependent call on the next turn.

    Write PostgreSQL dialect against the schema in your instructions, and:
    - Double-quote camelCase identifiers exactly as the schema spells them
      ("surveyId", "blindingNumber"); unquoted names fold to lowercase and error.
    - Always constrain the query to the survey it is about: :survey_id for this
      one, a resolved survey under :client_id when the user requested another survey or a
      cross-survey comparison, or the benchmark id. Ordinary questions stay on :survey_id;
      comparing products or measures inside it is not permission to search other surveys.
      Same-client survey discovery may cross organizations. The executor derives the client
      boundary from the initial survey and refuses or row-scopes every physical table source;
      this does not make an unscoped query semantically correct.
      Reference ids as the bound placeholders :client_id, :organization_id and
      :survey_id rather than retyping the literal values -- a mistyped id is a
      wasted turn, and the placeholders cannot be mistyped.
    - Obey the PostgreSQL dialect limits listed in your instructions; the query is
      parsed by the planner before it runs, so a rejected construct costs a turn.
    - Aggregate in SQL (COUNT, AVG, GROUP BY) rather than fetching raw rows and
      counting them yourself -- it is faster and the answer comes back smaller.
    - Make a multidimensional result readable to the final-answer hierarchy rules. Return one
      separate, human-readable column for every grouping dimension that matters to the answer --
      for example survey, product, question/domain, attribute/measure label, segment, wave or
      category -- with a stable descriptive alias. Never concatenate several levels into one display
      label, and never make a UUID or database id the only column identifying a level. Prefer tidy
      aggregate rows (grouping columns + measure label + scalar values) when two or more dimensions
      may support recursive grouping, so repeated values and their row mappings remain explicit.
      Wide rows are valid only when every pivoted dimension remains recoverable from stable,
      human-readable column headers; the final model must be able to conceptually unpivot them. Do not
      hide grouping levels inside prose or an opaque JSON string, because the final answer must be
      able to recognize parents, multiple children and children of children from the returned values.
    - Construct checks may need scale metadata even when the inventory already has the aggregates.
      If a generic prompt such as Appearance, Aroma or Texture does not establish whether the user-
      named construct is liking, intensity or another rating, run one metadata query on the already
      resolved question id(s): return the option/attribute label plus the first and last entries of
      `question_option."optionSettings"->'positionLabels'`. Do not pattern-match meaning in SQL and
      do not re-aggregate the answers; the endpoint wording is what the final model classifies.
    - When you DO have to pull raw text -- verbatims to design theme codes is the
      usual case -- select ONLY the columns you will actually read. An id you never
      reference again is pure cost, and every column name is repeated on every row:
      measured on one 1,183-row verbatim pull, `enrollment_id` was 67 KB of a 201 KB
      result, a third of the payload, and nothing downstream touched it because the
      counting was a separate aggregate that re-derived its own grain. Take the text
      alone; add a group label only when you actually intend to read the themes
      differently per group, and never carry a respondent or answer id into a pass
      whose only purpose is reading what people wrote.
    - For an unqualified survey "response count", count distinct `answer.enrollment_id` and
      alias it `answered_people`. An unfiltered enrollment count is `enrollments`, not responses;
      `COUNT(answer.id)` is `answer_rows`, not people. Say `completed_people` only when the user
      asked for completion and the query filters `enrollment_status = 'completed'`. Never use an
      unlabeled `response_count` alias whose grain could be enrollment, person, or answer row.
    - When coding verbatims into themes, theme membership is multi-label: one response
      may count in every matching theme. Use independent COUNT(...) FILTER clauses or
      UNION ALL followed by COUNT(DISTINCT respondent); never use one CASE expression
      that assigns each response only to its first matching theme. Say when theme counts
      overlap and therefore need not sum to the response base. Keep each theme semantically
      coherent; do not pool unrelated cues into one broad bucket merely to raise its count.
      ANCHOR EVERY THEME PATTERN TO WORD BOUNDARIES -- write `txt ~ '\\m(thin|light|airy)\\M'`,
      never `txt LIKE '%thin%'` or a bare `~ '(thin|light)'`. An unanchored alternation matches
      inside longer words and silently inflates the theme: measured on one survey, `%thin%` and
      `%light%` counted "something", "nothing", "slightly" and "slight" as mentions of a thin,
      light texture (164 respondents reported, 131 real), and an "overall|nothing" praise pattern
      was 81% false because "overall the flavors are balanced" is a flavour comment. Check each
      alternative you write against this: if the word appears inside a commoner word, anchor it.
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
RUN_SURVEY_STATS_DESCRIPTION = """Run statistical tests (anova, tukey, pearson, spearman, chi-square, pca) on a survey question.

    question_id: a UUID from the pre-fetched inventory or an nl2sql_tool
    result, never from memory. pearson/spearman/penalty also need
    reference_question_id, a second question from the SAME survey.

    For PCA use stats_types='pca' and the parent UUID of ONE numeric, product-linked,
    multi-attribute question. Products are observations and the question's attributes are
    variables. PCA needs no reference_question_id. A biplot request still uses 'pca':
    'pca-biplot' is a visualization alias, not a valid Charts API stats type. The runtime builds
    a trusted gpi-chart payload for every plottable PCA. pca_plot_dimensions defaults to '2d';
    pass '3d' only when the user explicitly requests a 3D PCA plot.
    """


ANALYZE_PLSR_DESCRIPTION = """Run partial least squares regression from survey attributes onto one KPI.

    Use this ONLY when the user explicitly asks for PLSR/PLS regression, VIP scores, or partial
    least squares between attributes and a KPI. It ranks attributes; it does not test significance.

    Responses are collected at respondent grain and then averaged to ONE ROW PER PRODUCT; the fit
    runs on those product means, which is why the KPI and the attributes must all be
    product-linked measures in the same survey. Pass the KPI as kpi and the USER-NAMED attributes
    as attributes; the server fetches and aligns the raw responses, so never retrieve those rows
    with nl2sql_tool. Leave attributes empty to use every other compatible numeric measure in the
    survey. Each variable takes question_id plus an optional component_label: supply the exact
    component when the user named one, omit it to expand every component separately.

    Every question_id must come from the complete inventory/packet or a prior structural
    nl2sql_tool discovery result, never from memory. WHERE THE USER HAS NOT CLEARLY NAMED THE KPI,
    or names attributes you cannot resolve to specific questions, DO NOT GUESS AND DO NOT CALL
    THIS TOOL. List the survey's compatible numeric measures from the inventory, say which could
    serve as the KPI and which as attributes, and ask the user to choose. A returned
    candidate_attributes list is the server's own view of what was usable; use it the same way.

    Before calling, obtain two method choices from the user whenever they were not already
    supplied: normalization and cross-validation. normalization must be one of `reference`
    (outer predictor z-score plus PLS internal scaling; reproduces the supplied reference),
    `zscore_once` (outer predictor z-score only), `pls_internal` (PLS internal scaling only),
    or `none` (centering only). cross-validation must be `loo`, `kfold` or `none`. `kfold`
    additionally requires a FOLD COUNT FROM THE USER in `cv_folds` -- never pick one yourself.
    Each fold is scored on its own held-out products, so `cv_folds` must be at most half the
    product count; a fold count that does not fit comes back as `invalid_cv_folds` with that
    KPI's product count and the largest usable fold count, which you take back to the user.
    Do not silently choose a default. The selected choices are echoed in the result's model
    block.

    n_components is NOT one of those choices: it defaults to 2, so pass it only when the user
    names a component count and never ask for one. The data caps it at one fewer than the
    product count and at the number of attributes fitted, so a larger request is fitted at that
    ceiling rather than refused; the model block reports the count used as `n_components` and
    the count asked for as `n_components_requested`.

    The result carries one VIP-ranked row per attribute with its PLS coefficient, plus the fit's
    MSE, RMSE and R-squared and, when cross-validation is selected, held-out RMSE and R-squared.
    `kfold` scores each fold on its own held-out products and averages the fold scores; `loo`
    holds out one product at a time, which leaves a fold with no variance of its own, so its
    R-squared is pooled over all held-out predictions instead. `cv_aggregation` in the model
    block records which of the two produced the reported pair. Report every attribute the tool
    returns, never a top-few subset.
    Coefficients are in standardised-attribute units, so their sign gives direction and VIP gives
    rank. This is statistical association among this survey's products, not proof of causation.
    """


PLSR_RESULT_HEAD = "PLSR ANALYSIS COMPLETE"


CLUSTER_RATING_PROFILES_DESCRIPTION = """Group this survey's respondents by the SHAPE of their
    rating profile across several attribute questions, using k-means.

    Use it when the user asks to cluster, segment or group respondents by their scores -- "find
    clusters in the liking scores", "which groups of people rate this differently". It answers
    questions of the form "these people like the flavor but not the texture".

    Pass `question_ids`: two or more resolved question UUIDs, each a numeric vertical-rating
    question measuring ONE attribute, all on the same scale. Take them from the pre-fetched
    inventory; the tool retrieves every respondent's answers itself, so write no SQL for this and
    pass no data. `k` is the number of clusters and defaults to 3.

    Pass an overall/summary liking question as `overall_question_id`, never inside
    `question_ids`. A summary measure is a restatement of the attributes it summarises, and
    clustering on it manufactures a split that the attributes do not support; the tool refuses it
    in the basis and reports its mean per cluster instead.

    Each cluster comes back with its size and share, and for every attribute: mean, sd, median,
    `core_range` (the interquartile range -- the cluster's dominant area on that dimension),
    `full_range`, `separation_d` against the rest of the sample, `core_range_lift`, and a `role`
    of defining, secondary or shared. `defining_attributes` ranks the dimensions that actually
    separate the cluster. Also returned: `stability`, `product_composition` and the
    `role_thresholds` that assigned every role.

    These are descriptive measures of a partition the tool just constructed, not statistical
    tests. There are no p-values here and none can be derived from these numbers.
    """


CLUSTER_RESULT_HEAD = "RESPONDENT CLUSTERS COMPLETE"


def cluster_bad_question_id(question_ids: list[str]) -> str:
    return f"Clustering unavailable: invalid question UUID(s): {question_ids}."


def cluster_too_few_questions() -> str:
    return (
        "Clustering unavailable: a rating profile needs at least two attribute questions. "
        "Name every attribute question the user wants clustered."
    )


def cluster_bad_k(k: int, n_respondents: int | None = None) -> str:
    if n_respondents is not None:
        return (
            f"Clustering unavailable: k={k} is not usable with {n_respondents} respondents "
            "who answered every named question."
        )
    return f"Clustering unavailable: k must be between 2 and 8; got {k}."


def cluster_unsupported_type(question_id: str, question_type: str) -> str:
    return (
        f"Clustering unavailable: question {question_id} is {question_type!r}. This tool clusters "
        "numeric vertical-rating questions; a line-scale carries several attributes in one "
        "question and is not supported yet."
    )


def cluster_mixed_scales(scales: list[str]) -> str:
    return (
        f"Clustering unavailable: the named questions use different scales ({', '.join(scales)}). "
        "Cluster one set of questions that share a scale."
    )


def cluster_summary_measure(question_id: str, label: str) -> str:
    return (
        f"Clustering refused: question {question_id} ({label!r}) reads as an overall/summary "
        "measure, which cannot sit in the clustering basis. Call again with it passed as "
        "overall_question_id and the attribute questions in question_ids."
    )


def cluster_multi_product(question_id: str) -> str:
    return (
        f"Clustering unavailable: respondents evaluated more than one product on question "
        f"{question_id}. This tool clusters one profile per respondent and does not yet split a "
        "respondent across the products they rated."
    )


def cluster_no_data(n_respondents: int) -> str:
    return (
        f"Clustering unavailable: only {n_respondents} respondent(s) answered every named "
        "question, which is too few to cluster."
    )


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
    "answer what the data does support -- do not keep retrying the join. After the diagnostic "
    "counts return, use them as evidence or change the data relationship; do not repeat the "
    "same retrieval."
)


# Prepended by nl2sql_tool when the rows it is about to return are a wide hierarchical candidate.
#
# The system prompt defines the full adaptive algorithm. This short result-adjacent reminder keeps
# the measured benefit of attaching the trigger to the data, but deliberately does not guess how
# many semantic dimensions the rows contain. Same channel as the zero-row notes, which the model
# acts on reliably.
def wide_result_note(entities: int, measures: int) -> str:
    return (
        f"[shape candidate: {entities} returned rows x {measures} numeric measure columns. This "
        "is a high-confidence signal to apply the adaptive hierarchical JSON rules. Conceptually "
        "unpivot both row labels and human-readable measure headers into atomic records, then judge "
        "the actual width, repeated column families, semantic dimensions and requested comparison. "
        "Use one `gpi-nested-table` JSON artifact when grouping improves navigation. Reassess every "
        "child independently and add another `Details` table only while a useful semantic dimension "
        "materially reduces clutter; there is no mandatory minimum depth. Preserve the requested "
        "comparison axis where practical and give EVERY measure for EVERY entity. Do not invent "
        "groups, collapse the result to a rollup or top-few prose list, or offer to break it down "
        "later.]\n"
    )


# Prepended by nl2sql_tool when a near-miss scope-id literal was silently corrected.
def scope_repair_note(repairs: list[str]) -> str:
    return (
        "[scope-id repair: " + "; ".join(repairs) + ". The ids are supplied to you -- write "
        ":client_id / :organization_id / :survey_id instead of retyping them.]\n"
    )


# Kept for compatibility with older traces; the executor now enforces the boundary itself.
SCOPE_WARNING = (
    "[scope warning: this query references none of :client_id / :organization_id / "
    ":survey_id (nor any id literal), so nothing constrains it to this run's survey -- "
    "every figure it returns may mix surveys inside the authorized client. If reaching outside "
    "this survey is deliberate, constrain it to a resolved survey id, and say in your "
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


__all__ = [
 'ANALYSIS_PACKET_RESULT_HEAD',
 'BIG_SQL_CHARS',
 'CLUSTER_RATING_PROFILES_DESCRIPTION',
 'CLUSTER_RESULT_HEAD',
 'GENERATE_WORD_CLOUD_DESCRIPTION',
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
 'UNTRUSTED_WORD_CLOUD_BLOCK',
 'WORD_CLOUD_SCOPE_UNAVAILABLE',
 'ZERO_ROW_HEAD',
 'ZERO_ROW_MSG',
 'analysis_packet_preamble',
 'cluster_bad_k',
 'cluster_bad_question_id',
 'cluster_mixed_scales',
 'cluster_multi_product',
 'cluster_no_data',
 'cluster_summary_measure',
 'cluster_too_few_questions',
 'cluster_unsupported_type',
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
 'unknown_tool',
 'word_cloud_bad_question_id',
 'word_cloud_question_unavailable',
 'word_cloud_ready']
