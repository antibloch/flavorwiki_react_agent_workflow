from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import pathlib
import re
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
import numpy as np
from sqlalchemy import text

import attribute_kpi_workflow as workflow
import funda_agent_exp as agent
import agent_instructions as instructions
import isolated_workflow_instructions as workflow_instructions
import output_store
import tool_prompts as prompts


SYSTEM_HASH = "bed0d36aec7c9cd7882815c03d608cd5f3a9c63466a08cf236275f48a5f213f0"


class _FakeResult:
    def __init__(self, *, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self._row

    def scalar(self):
        return self._scalar


class _FakeConnection:
    def __init__(self, lifecycle: dict):
        self.lifecycle = lifecycle
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params):
        self.calls += 1
        sql = str(statement)
        if "WITH scored AS MATERIALIZED" not in sql:
            raise AssertionError("inventory must execute the combined statement")
        if not self.lifecycle.get("found", True):
            return _FakeResult(row=None)
        answered = self.lifecycle.get("answered", True)
        # `unanswered` is a survey that HAS questions and no responses -- distinct from
        # `answered: False`, which is a survey with no rows in either section at all.
        unanswered = self.lifecycle.get("unanswered", False)
        labels = (
            [f"Option {index}" for index in range(self.lifecycle["wide_labels"])]
            if self.lifecycle.get("wide_labels") else ["Yes", "No"]
        )
        packet = {
            "products": self.lifecycle.get(
                "products", [{"product": "P1", "blindingNumber": "101"}]
            ),
            # Both measure sections are columnar: a `columns` header plus positional `rows`.
            "scored_measures_by_product": {
                "columns": ["qid", "attributes_pooled", "observed", "by_product",
                            "by_attribute", "order_differs"],
                "by_product_columns": ["product", "n", "respondents", "mean", "sd"],
                "by_attribute_columns": ["attribute", "product", "n", "mean", "sd"],
                "rows": (
                    [["q1", 1, [1, 9], [["P1", 10, 10, 5.5, 1.2]], None, None]]
                    if answered and not unanswered
                    and self.lifecycle.get("scored", True) else None
                ),
            },
            "catalog": {
                "columns": ["qid", "prompt", "type", "section_name", "product_section",
                            "product_linked", "screen_out_actions", "answered", "respondents",
                            "submissions", "answers", "multi_select", "components",
                            "response_categories", "scale", "derived_from", "role_conflict",
                            "categories_omitted"],
                "scale_columns": ["labelled_positions", "anchor_lo", "anchor_hi",
                                  "slider_min", "slider_max"],
                "rows": (
                    [["q2", "Comment", "multiple-choice", "Start questions", False, False,
                      None, not unanswered, 0 if unanswered else 5, 0 if unanswered else 5,
                      0 if unanswered else 25, False, None, labels, None, "config",
                      False, False]]
                    if answered and self.lifecycle.get("other", True) else None
                ),
            },
            "benchmark_context": self.lifecycle.get("benchmark_context", {
                "current_survey_is_benchmark_source": False,
                "current_survey_category": None,
                "has_assigned_benchmark": False,
                "assigned_benchmark": None,
                "active_benchmarks": [{
                    "category": "Protein Bars",
                    "survey_id": "benchmark-survey",
                    "product_id": "benchmark-product",
                }],
            }),
        }
        return _FakeResult(row={
            "state": self.lifecycle["state"],
            "is_active": self.lifecycle["is_active"],
            "packet": packet,
        })


class _FakeEngine:
    def __init__(self, lifecycle: dict):
        self.connection = _FakeConnection(lifecycle)

    def connect(self):
        return self.connection


class _Nl2SqlResult:
    returns_rows = True

    def keys(self):
        return ["value"]

    def fetchall(self):
        return [(1,)]


class _Nl2SqlConnection:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return self

    def execute(self, statement, _params=None):
        sql = str(statement)
        self.statements.append(sql)
        return _Nl2SqlResult() if sql.startswith("SELECT") else SimpleNamespace()


class _Nl2SqlEngine:
    def __init__(self):
        self.connection = _Nl2SqlConnection()
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return self.connection


def _pca_payload(component_count: int = 3) -> dict:
    pcs = [f"PC{index}" for index in range(1, component_count + 1)]
    variance = [60.0, 30.0, 10.0][:component_count]
    components = [
        {
            "pc": pc,
            "eigenvalue": float(component_count - index + 1),
            "varianceExplained": variance[index - 1],
            "loadings": [
                {
                    "attribute": attribute,
                    "loading": 0.1 * index * attr_index,
                    "correlationLoading": 0.2 * index * attr_index,
                }
                for attr_index, attribute in enumerate(("Gloss", "Freshness"), start=1)
            ],
        }
        for index, pc in enumerate(pcs, start=1)
    ]
    product_points = [
        {"product": product, **{pc: float(index * pc_index) for pc_index, pc in enumerate(pcs, 1)}}
        for index, product in enumerate(("Product A", "Product B"), 1)
    ]
    attribute_points = [
        {"attribute": attribute, **{pc: 0.1 * index * pc_index for pc_index, pc in enumerate(pcs, 1)}}
        for index, attribute in enumerate(("Gloss", "Freshness"), 1)
    ]
    return {
        "questionId": "11111111-1111-1111-1111-111111111111",
        "questionType": "line-scale",
        "respondentCount": 42,
        "productSampleData": [{"must": "not leak"}],
        "stats": {"pca": {"0.05": {"pcaAnalysis": {
            "attributes": ["Gloss", "Freshness"],
            "productLabels": ["Product A", "Product B"],
            "principalComponents": components,
            "pcScores": [
                {"product": row["product"], "scores": [
                    {"pc": pc, "score": row[pc]} for pc in pcs
                ]} for row in product_points
            ],
            "biplotData": {
                "productScores": product_points,
                "attributeLoadings": attribute_points,
            },
            "cumulativeVariance": [sum(variance[:index]) for index in range(1, component_count + 1)],
            "matrixRank": component_count,
            "numberOfComponents": component_count,
            "retentionPolicy": "positive components",
            "descriptiveStats": [
                {"attribute": attribute, "analysisN": 2}
                for attribute in ("Gloss", "Freshness")
            ],
            "numericalDiagnostics": {"converged": True},
            "warnings": [],
            "droppedZeroVarianceAttributes": [],
        }}}},
    }


class Nl2SqlLatencyTests(unittest.TestCase):
    def test_authoritative_scope_is_derived_from_initial_survey_not_request_tenant_ids(self):
        initial = "11111111-1111-1111-1111-111111111111"
        neighbor = "22222222-2222-2222-2222-222222222222"

        class ScopeResult:
            def mappings(self):
                return self

            def first(self):
                return {
                    "survey_id": initial,
                    "organization_id": "33333333-3333-3333-3333-333333333333",
                    "client_id": "44444444-4444-4444-4444-444444444444",
                    "authorized_survey_ids": [initial, neighbor],
                }

        seen = {}

        def execute(_statement, params):
            seen.update(params)
            return ScopeResult()

        with patch.dict(agent._SCOPE, {
            "survey_id": initial,
            "organization_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "client_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        }, clear=True):
            resolved = agent._resolve_authorized_scope(SimpleNamespace(execute=execute))

        self.assertEqual(resolved["authorized_survey_ids"], (initial, neighbor))
        self.assertEqual(seen["__fw_scope_initial_survey_id"], initial)
        self.assertNotIn("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", seen.values())
        self.assertIn("candidate_account.client_id = i.client_id", agent._AUTHORIZED_SCOPE_SQL)
        self.assertNotIn("candidate.organization_id = i.organization_id", agent._AUTHORIZED_SCOPE_SQL)

    def test_clientless_scope_has_no_automatic_benchmark_exception(self):
        self.assertRegex(
            agent._AUTHORIZED_SCOPE_SQL,
            r"i\.client_id IS NOT NULL\s+AND candidate\.id = CAST\(",
        )

    def test_query_uses_one_checkout_and_no_explain(self):
        engine = _Nl2SqlEngine()
        scope = {"survey_id": "11111111-1111-1111-1111-111111111111"}
        with (
            patch.object(agent, "_sql_engine", return_value=engine),
            patch.object(agent, "_resolve_authorized_scope", return_value={
                "survey_id": scope["survey_id"],
                "organization_id": None,
                "client_id": None,
                "authorized_survey_ids": (scope["survey_id"],),
            }),
            patch.dict(agent._SCOPE, scope, clear=True),
        ):
            result = agent.nl2sql_tool.invoke({
                "sql_query": "SELECT :survey_id AS value",
            })

        self.assertEqual(json.loads(result), [{"value": 1}])
        self.assertEqual(engine.connect_calls, 1)
        self.assertEqual(len(engine.connection.statements), 3)
        self.assertTrue(engine.connection.statements[0].startswith("SET TRANSACTION"))
        self.assertTrue(engine.connection.statements[1].startswith("SET LOCAL"))
        self.assertTrue(engine.connection.statements[2].startswith("SELECT"))
        self.assertFalse(any("EXPLAIN" in sql for sql in engine.connection.statements))

    def test_ast_guard_wraps_every_tenant_table_and_preserves_ctes(self):
        scope = {
            "survey_id": "11111111-1111-1111-1111-111111111111",
            "organization_id": "33333333-3333-3333-3333-333333333333",
            "client_id": "44444444-4444-4444-4444-444444444444",
            "authorized_survey_ids": ("11111111-1111-1111-1111-111111111111",),
        }
        connection = SimpleNamespace(execute=lambda *_args, **_kwargs: None)
        guarded = agent._guard_and_scope_sql(
            "WITH chosen AS (SELECT q.id FROM question q) "
            "SELECT chosen.id FROM chosen JOIN answer a ON a.question_id = chosen.id",
            connection,
            scope,
        )
        self.assertIn("public.question", guarded)
        self.assertIn("public.answer", guarded)
        self.assertGreaterEqual(guarded.count("__fw_scope_authorized_survey_ids"), 3)
        self.assertIn("FROM chosen", guarded)

        quoted_case = agent._guard_and_scope_sql(
            'WITH "Answer" AS (SELECT 1) SELECT * FROM answer',
            connection,
            scope,
        )
        self.assertIn("public.answer", quoted_case)

    def test_ast_guard_fails_closed_for_unknown_sources_and_reserved_binds(self):
        scope = {
            "survey_id": "11111111-1111-1111-1111-111111111111",
            "organization_id": None,
            "client_id": None,
            "authorized_survey_ids": ("11111111-1111-1111-1111-111111111111",),
        }
        connection = SimpleNamespace(execute=lambda *_args, **_kwargs: None)
        for sql in (
            "SELECT * FROM migrations",
            "SELECT * FROM pg_catalog.pg_user",
            "SELECT :__fw_scope_authorized_survey_ids",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT public.some_custom_function()",
        ):
            with self.subTest(sql=sql), self.assertRaises(agent.SurveyBoundaryError):
                agent._guard_and_scope_sql(sql, connection, scope)

    def test_ast_guard_refuses_a_real_foreign_survey_literal(self):
        foreign = "99999999-9999-9999-9999-999999999999"
        scope = {
            "survey_id": "11111111-1111-1111-1111-111111111111",
            "organization_id": None,
            "client_id": None,
            "authorized_survey_ids": ("11111111-1111-1111-1111-111111111111",),
        }

        class LiteralResult:
            def mappings(self):
                return self

            def all(self):
                return [{"survey_id": foreign}]

        connection = SimpleNamespace(execute=lambda *_args, **_kwargs: LiteralResult())
        with self.assertRaisesRegex(agent.SurveyBoundaryError, "outside"):
            agent._guard_and_scope_sql(
                f"SELECT * FROM survey WHERE id = '{foreign}'",
                connection,
                scope,
            )

    def test_stats_refuses_outside_question_before_http(self):
        question = "99999999-9999-9999-9999-999999999999"
        with (
            patch.dict(agent._SCOPE, {
                "survey_id": "11111111-1111-1111-1111-111111111111",
            }, clear=True),
            patch.object(agent, "_authorize_question_ids", return_value=([], [question])),
            patch.object(agent.requests, "get") as request,
        ):
            result = agent.run_survey_stats.invoke({
                "question_id": question,
                "stats_types": "anova,tukey",
            })
        self.assertIn("outside the initial survey's client boundary", result)
        request.assert_not_called()

    def test_engine_creation_does_not_enable_pool_pre_ping(self):
        sentinel = object()
        with (
            patch.object(agent, "_ENGINE", None),
            patch.object(agent, "create_engine", return_value=sentinel) as create_engine,
        ):
            self.assertIs(agent._sql_engine(), sentinel)

        self.assertNotIn("pool_pre_ping", create_engine.call_args.kwargs)


class PcaStatsToolTests(unittest.TestCase):
    @staticmethod
    def _artifact_payload(artifact: str) -> dict:
        prefix = "```gpi-chart\n"
        assert artifact.startswith(prefix) and artifact.endswith("\n```")
        return json.loads(artifact[len(prefix):-4])

    def test_default_pca_builds_trusted_2d_chart_and_suggests_3d(self):
        text_value, artifacts = agent._prepare_stats_response(
            _pca_payload(3),
            question_id="11111111-1111-1111-1111-111111111111",
            question_label="Appearance",
        )

        self.assertEqual(len(artifacts), 1)
        chart = artifacts[0]
        payload = self._artifact_payload(chart["artifact"])
        self.assertEqual(chart["dimensions"], "2d")
        self.assertEqual(payload["type"], "pca_biplot_2d")
        self.assertNotIn("z_axis", payload)
        self.assertEqual(chart["three_d_suggestion"], "Show a 3D PCA plot of Appearance")
        self.assertEqual(payload["metadata"]["base_size"], 42)
        self.assertIn("analysis observations=2 products", text_value)
        self.assertIn("correlation_loading", text_value)
        self.assertNotIn("must", text_value)

    def test_default_pca_falls_back_to_truthful_2d_when_pc3_is_absent(self):
        _text, artifacts = agent._prepare_stats_response(
            _pca_payload(2), question_label="Appearance"
        )
        chart = artifacts[0]
        payload = self._artifact_payload(chart["artifact"])
        self.assertEqual(chart["dimensions"], "2d")
        self.assertFalse(chart["fallback_reason"])
        self.assertEqual(payload["type"], "pca_biplot_2d")
        self.assertNotIn("z_axis", payload)
        self.assertFalse(chart["three_d_suggestion"])

    def test_explicit_2d_and_final_assembly_keep_suggestions_after_chart(self):
        _text, artifacts = agent._prepare_stats_response(
            _pca_payload(3), question_label="Appearance", pca_plot_dimensions="2d"
        )
        final_text = agent._assemble_pca_final(
            "Interpretation.\n\n{{Run PCA on Aroma}}", artifacts
        )
        self.assertIn("```gpi-chart", final_text)
        self.assertTrue(final_text.endswith("{{Run PCA on Aroma}}"))
        self.assertIn("{{Show a 3D PCA plot of Appearance}}", final_text)

    def test_3d_final_assembly_injects_one_deduplicated_2d_suggestion(self):
        _text, artifacts = agent._prepare_stats_response(
            _pca_payload(3), question_label="Appearance", pca_plot_dimensions="3d"
        )
        final_text = agent._assemble_pca_final(
            "Interpretation.\n\n{{Show a 2D PCA plot of Appearance}}", artifacts
        )
        self.assertEqual(final_text.count("{{Show a 2D PCA plot of Appearance}}"), 1)
        self.assertLess(
            final_text.index("```gpi-chart"),
            final_text.index("{{Show a 2D PCA plot of Appearance}}"),
        )

    def test_malformed_pca_is_visible_and_produces_no_chart(self):
        payload = _pca_payload()
        payload["stats"]["pca"]["0.05"] = {}
        text_value, artifacts = agent._prepare_stats_response(payload)
        self.assertIn("malformed API payload", text_value)
        self.assertEqual(artifacts, [])

    def test_pca_biplot_alias_is_canonicalized_before_http(self):
        question = "11111111-1111-1111-1111-111111111111"
        response = SimpleNamespace(
            status_code=200, text="", json=lambda: _pca_payload(3)
        )
        with (
            patch.dict(agent._SCOPE, {"survey_id": question}, clear=True),
            patch.object(agent, "_authorize_question_ids", return_value=([], [])),
            patch.object(agent, "_authorized_question_label", return_value="Appearance"),
            patch.object(agent.requests, "get", return_value=response) as request,
        ):
            result = agent.run_survey_stats.invoke({
                "question_id": question,
                "stats_types": "pca-biplot,pca",
            })

        self.assertTrue(result.startswith(agent._PCA_TOOL_RESULT_PREFIX))
        self.assertEqual(request.call_args.kwargs["params"]["statsTypes"], "pca")
        self.assertNotIn("referenceQuestionId", request.call_args.kwargs["params"])

    def test_stats_tool_schema_exposes_pca_plot_dimension_enum(self):
        schema = agent.run_survey_stats.args_schema.model_json_schema()
        dimensions = schema["properties"]["pca_plot_dimensions"]
        self.assertEqual(dimensions["default"], "2d")
        self.assertEqual(dimensions["enum"], ["2d", "3d"])
        self.assertIn("stats_types='pca'", agent.RUN_SURVEY_STATS_DESCRIPTION)
        self.assertIn("not a valid Charts API stats type", agent.RUN_SURVEY_STATS_DESCRIPTION)

    def test_tool_node_keeps_pca_artifact_out_of_model_evidence(self):
        formatted, artifacts = agent._prepare_stats_response(
            _pca_payload(3), question_label="Appearance"
        )
        envelope = agent._PCA_TOOL_RESULT_PREFIX + json.dumps({
            "text": formatted,
            "artifacts": artifacts,
        })
        call = {
            "name": "run_survey_stats",
            "id": "pca",
            "args": {"question_id": "ignored", "stats_types": "pca"},
        }
        state = {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "number_of_steps": 1,
            "last_sql_observations": {},
            "zero_row_streak": 0,
        }
        with patch.object(agent, "_invoke_tool_call", return_value=envelope):
            update = agent.call_tool(state)
        self.assertEqual(update["messages"][0].content, formatted)
        self.assertNotIn("gpi-chart", update["messages"][0].content)
        self.assertEqual(update["pca_chart_artifacts"], artifacts)

    def test_supported_model_scatterplot_payload_is_kept_and_invalid_chart_is_withheld(self):
        valid = """```gpi-chart
{"version":1,"type":"scatterplot","x_axis":{"label":"Aroma"},"y_axis":{"label":"Liking"},"series":[{"name":"Products","data":[{"label":"A","x":1.2,"y":3.4}]}]}
```"""
        sanitized, withheld = agent._sanitize_model_chart_blocks("Summary.\n\n" + valid)
        self.assertFalse(withheld)
        self.assertIn('"type":"scatterplot"', sanitized)

        invalid = """```gpi-chart
{"version":1,"type":"scatterplot","series":[{"data":[{"label":"A","x":"unknown","y":2}]}]}
```"""
        sanitized, withheld = agent._sanitize_model_chart_blocks(invalid)
        self.assertTrue(withheld)
        self.assertEqual(sanitized, "")

    def test_chart_suggestions_drop_unrelated_analysis_actions(self):
        value = (
            "Chart summary.\n\n"
            "{{Plot Dairy Aroma vs Cooked Aroma by product}}\n"
            "{{Run ANOVA and Tukey on Texture}}"
        )
        filtered = agent._filter_chart_suggestions(value)
        self.assertIn("Plot Dairy Aroma vs Cooked Aroma by product", filtered)
        self.assertNotIn("Texture", filtered)


class ModelReasoningConfigurationTests(unittest.TestCase):
    def test_model_has_single_default(self):
        with patch.dict(agent.os.environ, {}, clear=True):
            effort = agent._reasoning_effort(
                "MODEL_REASONING_EFFORT", "low", "STRONG_REASONING_EFFORT"
            )

        self.assertEqual(effort, "low")

    def test_canonical_setting_overrides_legacy_fallback(self):
        env = {
            "OPENAI_REASONING_EFFORT": "medium",
            "STRONG_REASONING_EFFORT": "high",
            "MODEL_REASONING_EFFORT": "low",
        }
        with patch.dict(agent.os.environ, env, clear=True):
            effort = agent._reasoning_effort(
                "MODEL_REASONING_EFFORT", "medium", "STRONG_REASONING_EFFORT"
            )

        self.assertEqual(effort, "low")

    def test_legacy_strong_setting_precedes_shared_fallback(self):
        env = {
            "OPENAI_REASONING_EFFORT": "medium",
            "STRONG_REASONING_EFFORT": "high",
        }
        with patch.dict(agent.os.environ, env, clear=True):
            effort = agent._reasoning_effort(
                "MODEL_REASONING_EFFORT", "low", "STRONG_REASONING_EFFORT"
            )

        self.assertEqual(effort, "high")


class InventoryCacheTests(unittest.TestCase):
    def setUp(self):
        agent._clear_inventory_cache()

    def tearDown(self):
        agent._clear_inventory_cache()

    def test_active_inventory_is_byte_identical_until_short_ttl_expires(self):
        clock = [100.0]
        engine = _FakeEngine({"state": "published", "is_active": True})
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(agent, "INVENTORY_CACHE_ACTIVE_TTL_S", 300)):
            first = agent.survey_inventory("survey-active")
            second = agent.survey_inventory("survey-active")
            self.assertEqual(first, second)
            self.assertEqual(engine.connection.calls, 1)

            clock[0] += 301
            refreshed = agent.survey_inventory("survey-active")
            self.assertEqual(refreshed, first)
            self.assertEqual(engine.connection.calls, 2)

    def test_inactive_closed_inventory_uses_long_ttl(self):
        clock = [200.0]
        engine = _FakeEngine({"state": "closed", "is_active": False})
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(agent, "INVENTORY_CACHE_CLOSED_TTL_S", 86400)):
            first = agent.survey_inventory("survey-closed")
            clock[0] += 3600
            self.assertEqual(agent.survey_inventory("survey-closed"), first)
            self.assertEqual(engine.connection.calls, 1)
            clock[0] += 86401
            agent.survey_inventory("survey-closed")
            self.assertEqual(engine.connection.calls, 2)

    def test_lru_is_bounded(self):
        with (patch.object(agent, "INVENTORY_CACHE_MAX_ENTRIES", 2),
              patch.object(agent.time, "monotonic", return_value=10.0)):
            agent._inventory_cache_put("a", "ok", "A", 100)
            agent._inventory_cache_put("b", "ok", "B", 100)
            self.assertEqual(agent._inventory_cache_get("a"), ("ok", "A"))
            agent._inventory_cache_put("c", "degraded", "C", 100)
            self.assertIsNone(agent._inventory_cache_get("b"))
            self.assertEqual(agent._inventory_cache_get("a"), ("ok", "A"))
            # The status is cached with the bytes: a degraded packet must not come back as ok.
            self.assertEqual(agent._inventory_cache_get("c"), ("degraded", "C"))

    def test_oversized_or_failed_inventory_is_not_cached(self):
        # A cap nothing can fit under: every label is dropped and it still does not fit, so
        # there is nothing safe to inject and nothing is cached.
        oversized = _FakeEngine({"state": "published", "is_active": True})
        with (patch.object(agent, "_sql_engine", return_value=oversized),
              patch.object(agent, "INVENTORY_MAX_CHARS", 1)):
            self.assertEqual(agent.survey_inventory("too-big"), "")
            result = agent._survey_analysis_packet_payload("too-big")
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.payload, "")
        self.assertEqual(oversized.connection.calls, 2)
        self.assertIsNone(agent._inventory_cache_get("too-big"))

        class _BrokenEngine:
            def connect(self):
                raise RuntimeError("database unavailable")

        with patch.object(agent, "_sql_engine", return_value=_BrokenEngine()):
            self.assertEqual(agent.survey_inventory("broken"), "")
            # A data-layer failure is `unavailable` and says nothing about the survey -- it must
            # never be reported as a survey that has no data.
            self.assertEqual(agent._survey_analysis_packet_payload("broken").status,
                             "unavailable")
        self.assertIsNone(agent._inventory_cache_get("broken"))

    def test_five_outcomes_are_discriminated_not_one_empty_string(self):
        cases = (
            ({"state": "published", "is_active": True}, "ok", True),
            ({"state": "published", "is_active": True, "unanswered": True}, "empty", True),
            ({"state": "published", "is_active": True, "answered": False,
              "benchmark_context": None}, "empty", False),
            ({"state": "published", "is_active": True, "found": False}, "not_found", False),
        )
        for lifecycle, expected, has_payload in cases:
            with self.subTest(expected=expected):
                agent._clear_inventory_cache()
                engine = _FakeEngine(lifecycle)
                with patch.object(agent, "_sql_engine", return_value=engine):
                    result = agent._survey_analysis_packet_payload(f"s-{expected}")
                self.assertEqual(result.status, expected)
                self.assertEqual(bool(result.payload), has_payload)

    def test_survey_with_questions_but_no_responses_still_ships_its_catalog(self):
        # `empty` is a reportable fact, not a failure: the configured questions are still worth
        # naming, each with answered=false. Returning nothing made "nobody answered this yet"
        # indistinguishable from "the database was down".
        engine = _FakeEngine({"state": "published", "is_active": True, "unanswered": True})
        with patch.object(agent, "_sql_engine", return_value=engine):
            inventory = agent.survey_inventory("no-responses")
        self.assertTrue(inventory)
        payload = json.loads(inventory.removeprefix(agent.INVENTORY_PREAMBLE))
        self.assertEqual(payload["result"], "empty")
        catalog = payload["catalog"]
        row = dict(zip(catalog["columns"], catalog["rows"][0]))
        self.assertFalse(row["answered"])
        self.assertEqual(row["respondents"], 0)
        self.assertIsNone(payload["scored_measures_by_product"]["rows"])
        # And the preamble tells the model what to do with it.
        self.assertIn("`empty` means the survey exists and holds no", agent.INVENTORY_PREAMBLE)

    def test_over_budget_trims_labels_and_says_so_instead_of_dropping_everything(self):
        engine = _FakeEngine({
            "state": "published", "is_active": True, "scored": False, "wide_labels": 40,
        })
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent, "INVENTORY_MAX_CHARS", 1550)):
            result = agent._survey_analysis_packet_payload("wide")
        self.assertEqual(result.status, "degraded")
        self.assertLessEqual(len(result.payload), 1550)
        payload = json.loads(result.payload)
        self.assertEqual(payload["result"], "degraded")
        self.assertEqual(payload["omitted"]["questions"], 1)
        self.assertEqual(payload["omitted"]["labels_dropped"], 25)
        self.assertEqual(payload["omitted"]["option_labels_kept_per_question"], 15)
        catalog = payload["catalog"]
        row = dict(zip(catalog["columns"], catalog["rows"][0]))
        self.assertEqual(len(row["response_categories"]), 15)
        # The trimmed row SAYS it was trimmed. Without this the short list reads as the whole
        # list, which is the exact failure the catalog replaced.
        self.assertTrue(row["categories_omitted"])
        # The question itself is still named -- degrading never removes a candidate.
        self.assertEqual(row["qid"], "q2")
        self.assertEqual(row["prompt"], "Comment")

    def test_omitted_count_accumulates_across_tiers(self):
        # The tiers are cumulative: 60 labels -> 15 -> 5 drops 45 then 10. Reporting only the
        # last step said 10 and understated what the reader was missing.
        engine = _FakeEngine({
            "state": "published", "is_active": True, "scored": False, "wide_labels": 60,
        })
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent, "INVENTORY_MAX_CHARS", 1400)):
            result = agent._survey_analysis_packet_payload("tiers")
        omitted = json.loads(result.payload)["omitted"]
        self.assertEqual(omitted["option_labels_kept_per_question"], 5)
        self.assertEqual(omitted["labels_dropped"], 55)
        self.assertEqual(omitted["questions"], 1)

    def test_degradation_never_truncates_a_prompt(self):
        # A real prompt carries its attribute at the END ("...This product will... Taste
        # great"), so truncating one renames the measure. Only labels may degrade.
        engine = _FakeEngine({
            "state": "published", "is_active": True, "scored": False, "wide_labels": 60,
        })
        # Tight enough that the first tier is not sufficient and the ladder steps down again.
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent, "INVENTORY_MAX_CHARS", 1400)):
            result = agent._survey_analysis_packet_payload("prompts")
        catalog = json.loads(result.payload)["catalog"]
        self.assertEqual(dict(zip(catalog["columns"], catalog["rows"][0]))["prompt"], "Comment")

    def test_benchmark_context_survives_without_answered_measures(self):
        empty = _FakeEngine({"state": "published", "is_active": True, "answered": False})
        with patch.object(agent, "_sql_engine", return_value=empty):
            first = agent.survey_inventory("empty")
            second = agent.survey_inventory("empty")
        self.assertEqual(first, second)
        payload = json.loads(first.removeprefix(agent.INVENTORY_PREAMBLE))
        self.assertFalse(payload["benchmark_context"]["has_assigned_benchmark"])
        self.assertEqual(empty.connection.calls, 1)
        self.assertIsNotNone(agent._inventory_cache_get("empty"))

    def test_combined_statement_preserves_partial_packets_and_field_order(self):
        cases = (
            ("scored-only", {"scored": True, "other": False}),
            ("other-only", {"scored": False, "other": True}),
        )
        for survey_id, measures in cases:
            with self.subTest(survey_id=survey_id):
                engine = _FakeEngine({
                    "state": "published", "is_active": True, **measures,
                })
                with patch.object(agent, "_sql_engine", return_value=engine):
                    payload = agent._survey_analysis_packet_payload(survey_id).payload
                self.assertEqual(engine.connection.calls, 1)
                self.assertEqual(list(json.loads(payload)), [
                    "result", "products", "scored_measures_by_product", "catalog",
                    "benchmark_context",
                ])

    def test_benchmark_assignment_is_part_of_the_single_inventory_statement(self):
        self.assertIn("'has_assigned_benchmark',EXISTS", agent._INV_SQL)
        self.assertIn("FROM benchmark_registry br", agent._INV_SQL)
        self.assertIn("LEFT JOIN survey_nomenclature sn", agent._INV_SQL)
        engine = _FakeEngine({
            "state": "published", "is_active": True,
            "benchmark_context": {
                "current_survey_is_benchmark_source": False,
                "current_survey_category": "Protein Bars",
                "has_assigned_benchmark": True,
                "assigned_benchmark": {"category": "Protein Bars"},
                "active_benchmarks": [{"category": "Protein Bars"}],
            },
        })
        with patch.object(agent, "_sql_engine", return_value=engine):
            payload = json.loads(agent._survey_analysis_packet_payload("assigned").payload)
        self.assertTrue(payload["benchmark_context"]["has_assigned_benchmark"])
        self.assertEqual(payload["benchmark_context"]["assigned_benchmark"]["category"],
                         "Protein Bars")
        self.assertEqual(engine.connection.calls, 1)

    def test_catalog_contract_distinguishes_values_from_submissions(self):
        # The two count grains survived the move from Section B into the catalog, names
        # included, because the preamble teaches the model what they mean.
        self.assertIn("COUNT(DISTINCT a.id) subs,COUNT(*) answers", agent._INV_SQL)
        self.assertIn("'submissions','answers',", agent._INV_SQL)
        self.assertIn("use it when reporting how many", agent.INVENTORY_PREAMBLE)
        engine = _FakeEngine({
            "state": "published", "is_active": True, "scored": False,
        })
        with patch.object(agent, "_sql_engine", return_value=engine):
            payload = json.loads(
                agent._survey_analysis_packet_payload("answer-grains").payload)
        catalog = payload["catalog"]
        row = dict(zip(catalog["columns"], catalog["rows"][0]))
        self.assertEqual(row["answers"], 25)
        self.assertEqual(row["submissions"], 5)
        self.assertEqual(row["respondents"], 5)

    def test_catalog_names_the_labels_section_b_reported_as_null(self):
        # The regression this catalog exists to prevent: Section B filtered labels on
        # `answerData ? 'optionLabel'`, a key multiple-choice never sets, so 1,120 questions
        # DB-wide reported sample_option_labels NULL while their labels sat in question_option.
        self.assertNotIn("sample_option_labels", agent._INV_SQL)
        self.assertNotIn("other_answered_measures", agent._INV_SQL)
        self.assertNotIn("aqo.\"answerData\" ? 'optionLabel'", agent._INV_SQL)
        # Labels come from question_option, keyed on the component key that is correct for a
        # matrix, and roles stay in separate lists.
        self.assertIn("COALESCE(aqo.matrix_row_option_id,aqo.question_option_id)", agent._INV_SQL)
        self.assertIn("'components','response_categories'", agent._INV_SQL)
        engine = _FakeEngine({
            "state": "published", "is_active": True, "scored": False,
        })
        with patch.object(agent, "_sql_engine", return_value=engine):
            payload = json.loads(agent._survey_analysis_packet_payload("labels").payload)
        catalog = payload["catalog"]
        row = dict(zip(catalog["columns"], catalog["rows"][0]))
        self.assertEqual(row["response_categories"], ["Yes", "No"])
        self.assertTrue(row["answered"])
        # labelled_positions is NOT the scale point count -- it disagrees with the slider range
        # on 53% of questions holding both -- so the field carries the name of what it counts.
        self.assertEqual(catalog["scale_columns"][0], "labelled_positions")
        self.assertNotIn("'scale_points'", agent._INV_SQL)

    def test_catalog_includes_configured_but_unanswered_questions(self):
        # 289 configured-but-unanswered questions sit inside surveys that DO have responses.
        # `answered` must distinguish "configured, no responses" from "does not exist"; a
        # question is included on configuration alone, never on having answers.
        self.assertIn("COALESCE(cobs.resp,0)>0", agent._INV_SQL)
        self.assertIn("LEFT JOIN cobs ON cobs.qid=cq.qid", agent._INV_SQL)
        # info questions are the one exclusion: display copy, never a measure.
        self.assertIn("qu.\"typeOfQuestion\"<>'info'", agent._INV_SQL)

    def test_unknown_survey_fails_soft_after_one_statement(self):
        missing = _FakeEngine({
            "state": "published", "is_active": True, "found": False,
        })
        with patch.object(agent, "_sql_engine", return_value=missing):
            self.assertEqual(agent.survey_inventory("missing"), "")
        self.assertEqual(missing.connection.calls, 1)
        self.assertIsNone(agent._inventory_cache_get("missing"))

    def test_startup_and_on_demand_paths_share_one_serialized_packet(self):
        survey_id = "11111111-1111-1111-1111-111111111111"
        engine = _FakeEngine({"state": "published", "is_active": True})
        owner = {"survey_id": survey_id, "title": "Historical Wave", "in_scope": True}
        scope = {
            "survey_id": "22222222-2222-2222-2222-222222222222",
            "organization_id": "33333333-3333-3333-3333-333333333333",
        }
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent, "_survey_owner", return_value=owner),
              patch.dict(agent._SCOPE, scope, clear=True)):
            startup = agent.survey_inventory(survey_id)
            packet = agent.get_survey_analysis_packet.invoke({"survey_id": survey_id})

        payload = startup.removeprefix(agent.INVENTORY_PREAMBLE)
        self.assertTrue(payload.startswith('{"result": "ok", "products":'))
        self.assertTrue(packet.endswith(payload))
        self.assertEqual(engine.connection.calls, 1)

    def test_fixed_benchmark_uses_dedicated_long_ttl(self):
        clock = [500.0]
        engine = _FakeEngine({"state": "published", "is_active": True})
        with (patch.object(agent, "_sql_engine", return_value=engine),
              patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(agent, "INVENTORY_CACHE_ACTIVE_TTL_S", 5),
              patch.object(agent, "INVENTORY_CACHE_BENCHMARK_TTL_S", 100)):
            first = agent._survey_analysis_packet_payload(agent.BENCHMARK_SCOPE["survey_id"])
            clock[0] += 50
            self.assertEqual(
                agent._survey_analysis_packet_payload(agent.BENCHMARK_SCOPE["survey_id"]),
                first,
            )
            self.assertEqual(engine.connection.calls, 1)
            clock[0] += 51
            agent._survey_analysis_packet_payload(agent.BENCHMARK_SCOPE["survey_id"])
            self.assertEqual(engine.connection.calls, 2)


class _FakePersonaResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _FakePersonaConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params):
        self.calls += 1
        self.assert_persona_sql(str(statement))
        return _FakePersonaResult(self.rows)

    @staticmethod
    def assert_persona_sql(sql):
        if "eligible_questions AS" not in sql:
            raise AssertionError("persona inventory must execute its combined statement")


class _FakePersonaEngine:
    def __init__(self, rows):
        self.connection = _FakePersonaConnection(rows)

    def connect(self):
        return self.connection


def _persona_row(label, count, *, prompt="What is your age group?", qid="age-q",
                 respondents=100, configured="one", max_selections=1):
    return {
        "state": "published", "is_active": True,
        "survey_enrollments": 110, "survey_respondents": 100,
        "qid": qid, "prompt": prompt, "qtype": "multiple-choice",
        "configured_answer_type": configured,
        "max_selections_per_answer": max_selections,
        "respondents": respondents, "label": label,
        "category_respondents": count,
    }


class PersonaInventoryTests(unittest.TestCase):
    def setUp(self):
        agent._clear_inventory_cache()

    def tearDown(self):
        agent._clear_inventory_cache()

    def test_persona_detection_is_specific_to_persona_requests(self):
        self.assertTrue(agent.is_persona_request("What is the persona of this survey?"))
        self.assertTrue(agent.is_persona_request("Create a consumer profile"))
        self.assertTrue(agent.is_persona_request("Give me the survey demographics"))
        self.assertTrue(agent.is_persona_request("Show respondent demographics"))
        self.assertTrue(agent.is_persona_request("Summarize motivations and loyalty"))
        self.assertTrue(agent.is_persona_request("Show audience attitudes"))
        self.assertFalse(agent.is_persona_request("Show the survey profile metadata"))
        self.assertFalse(agent.is_persona_request("What motivates purchase of product 772?"))

    def test_wilson_ranges_and_dominance_are_evidence_not_largest_bucket(self):
        self.assertEqual(agent._wilson_interval(60, 100), [50.2, 69.1])
        clear = agent._persona_dominance([
            {"label": "25-34", "respondents": 80},
            {"label": "35-44", "respondents": 20},
        ], "single")
        mixed = agent._persona_dominance([
            {"label": "25-34", "respondents": 55},
            {"label": "35-44", "respondents": 45},
        ], "single")
        overlapping = agent._persona_dominance([
            {"label": "Flavor", "respondents": 80},
            {"label": "Price", "respondents": 70},
        ], "multiple")
        self.assertTrue(clear["significant"])
        self.assertFalse(mixed["significant"])
        self.assertFalse(overlapping["available"])

    def test_packet_builds_complete_distributions_and_uses_cache(self):
        rows = [
            _persona_row("25-34", 80),
            _persona_row("35-44", 20),
        ]
        engine = _FakePersonaEngine(rows)
        with patch.object(agent, "_sql_engine", return_value=engine):
            first = agent.survey_persona_inventory("survey-persona")
            second = agent.survey_persona_inventory("survey-persona")

        self.assertEqual(first, second)
        self.assertEqual(engine.connection.calls, 1)
        payload = json.loads(first.removeprefix(agent.PERSONA_INVENTORY_PREAMBLE))
        self.assertEqual(payload["survey_respondents"], 100)
        question = payload["categorical_questions"][0]
        self.assertEqual(question["selection_mode"], "single")
        self.assertEqual(question["coverage_pct"], 100.0)
        self.assertEqual(question["distribution"][0]["label"], "25-34")
        self.assertEqual(question["distribution"][0]["ci95_pct"], [71.1, 86.7])
        self.assertTrue(question["dominance"]["significant"])

    def test_empty_failed_and_oversized_packets_fail_soft_without_caching(self):
        empty = _FakePersonaEngine([])
        with patch.object(agent, "_sql_engine", return_value=empty):
            self.assertEqual(agent.survey_persona_inventory("missing"), "")
        self.assertEqual(empty.connection.calls, 1)

        rows = [_persona_row("25-34", 80), _persona_row("35-44", 20)]
        oversized = _FakePersonaEngine(rows)
        with (patch.object(agent, "_sql_engine", return_value=oversized),
              patch.object(agent, "PERSONA_INVENTORY_MAX_CHARS", 1)):
            self.assertEqual(agent.survey_persona_inventory("oversized"), "")
            self.assertEqual(agent.survey_persona_inventory("oversized"), "")
        self.assertEqual(oversized.connection.calls, 2)

    def test_persona_prompt_allows_only_evidence_grounded_inferences(self):
        rules = " ".join(agent.PERSONA_RULES.split())
        self.assertIn("Never fill a blank with a stereotype", rules)
        self.assertIn("Marginal distributions do not prove that traits belong to the same people", agent.PERSONA_RULES)
        self.assertIn("dominance.significant=true", agent.PERSONA_RULES)
        self.assertIn("packet failure is not evidence", agent.PERSONA_RULES)
        self.assertIn("judge whether that evidence is sufficient for the depth", rules)
        self.assertIn('For a bare "persona" request', rules)
        self.assertIn("Categorical demographics alone are not sufficient", rules)
        self.assertIn("use scoped SQL to inspect that response evidence", rules)
        self.assertIn("same tool-call turn so they run in parallel", rules)
        self.assertIn("retrieve the complete in-scope response set", rules)
        self.assertIn("Do not mentally tally theme counts", rules)
        self.assertIn("dependent aggregate theme-count query", rules)
        self.assertIn("internal progress delta", rules)
        self.assertIn("counts distinct respondents per theme", rules)
        self.assertIn("Theme coding is multi-label", rules)
        self.assertIn("never one mutually exclusive CASE expression", rules)
        self.assertIn("overlapping theme counts need not sum", rules)
        self.assertIn("Keep each theme to one coherent idea and polarity", rules)
        self.assertIn("do not inflate a catch-all", rules)
        self.assertIn("source question wording or SQL-derived field", rules)
        self.assertIn("warm, lively, plain language", rules)
        self.assertIn("use emojis generously", rules)
        self.assertIn("never show p-values", rules)
        self.assertIn("Do not say `marginal composite`", rules)
        self.assertIn("should not be read as one confirmed individual", rules)
        self.assertIn('Start with `> **PERSONA**`', rules)
        self.assertIn('`###` persona name', rules)
        self.assertIn('`## 👤 WHO THEY ARE`', rules)
        self.assertIn("👤 Profile | ✨ Persona snapshot | 🔎 Based on", rules)
        self.assertIn("`## 💡 MOTIVATIONS & BEHAVIORS`", rules)
        self.assertIn("🎯 Motivation / behavior | 💬 Persona insight", rules)
        self.assertNotIn("persona-card", rules)
        self.assertNotIn("Use HTML, not Markdown", rules)
        self.assertIn("Not available from this survey", rules)
        self.assertIn("recurring verbatim themes", rules)
        self.assertIn("Label every such inference `Inferred`", rules)
        self.assertIn("descriptive intensity ratings alone do not show what people want", rules)
        self.assertIn("after-work snacking", rules)
        self.assertIn("Inferred loyalty tendency", rules)
        self.assertIn("at least two relevant behavioral signals", rules)
        self.assertIn("`📋 Direct answer:`", rules)
        self.assertIn("`🔎 Inferred from:`", rules)
        self.assertIn("Past-month consumption does not support", rules)
        self.assertIn("most-often-eaten brand question supports only that subgroup ranking", rules)
        self.assertIn("no direct or defensible inferential evidence", rules)
        self.assertIn("lists answered open-text", agent.PERSONA_INVENTORY_PREAMBLE)
        self.assertIn("cannot be produced from this survey", agent.PERSONA_INVENTORY_PREAMBLE)


class AnalysisPacketToolTests(unittest.TestCase):
    TARGET = "11111111-1111-1111-1111-111111111111"
    CURRENT = "22222222-2222-2222-2222-222222222222"
    ORG = "33333333-3333-3333-3333-333333333333"

    def test_tool_schema_and_registration(self):
        self.assertIn("get_survey_analysis_packet", agent.tools_by_name)
        schema = agent.get_survey_analysis_packet.args_schema.model_json_schema()
        self.assertEqual(schema["required"], ["survey_id"])
        self.assertEqual(schema["properties"]["survey_id"]["type"], "string")

    def test_tool_accepts_authorized_target_and_attributes_source(self):
        owner = {"survey_id": self.TARGET, "title": "Historical Wave", "in_scope": True}
        with (patch.dict(agent._SCOPE, {
                  "survey_id": self.CURRENT, "organization_id": self.ORG,
              }, clear=True),
              patch.object(agent, "_survey_owner", return_value=owner),
              patch.object(agent, "_survey_analysis_packet_payload",
                           return_value=agent._PacketResult("ok", '{"products":[]}'))):
            result = agent.get_survey_analysis_packet.invoke({"survey_id": self.TARGET})
        self.assertTrue(result.startswith(prompts.ANALYSIS_PACKET_RESULT_HEAD))
        self.assertIn('"Historical Wave"', result)
        self.assertTrue(result.endswith('{"products":[]}'))

    def test_tool_fails_closed_for_bad_missing_and_out_of_scope_targets(self):
        self.assertIn(
            "must be a UUID",
            agent.get_survey_analysis_packet.invoke({"survey_id": "not-a-uuid"}),
        )
        with patch.dict(agent._SCOPE, {}, clear=True):
            self.assertEqual(
                agent.get_survey_analysis_packet.invoke({"survey_id": self.TARGET}),
                agent.PACKET_SCOPE_UNAVAILABLE,
            )

        scope = {"survey_id": self.CURRENT, "organization_id": self.ORG}
        with (patch.dict(agent._SCOPE, scope, clear=True),
              patch.object(agent, "_survey_owner", return_value=None)):
            self.assertIn(
                "no survey exists",
                agent.get_survey_analysis_packet.invoke({"survey_id": self.TARGET}),
            )
        with (patch.dict(agent._SCOPE, scope, clear=True),
              patch.object(agent, "_survey_owner", return_value={
                  "survey_id": self.TARGET, "title": "Other Tenant", "in_scope": False,
              }),
              patch.object(agent, "_survey_analysis_packet_payload") as fetch):
            result = agent.get_survey_analysis_packet.invoke({"survey_id": self.TARGET})
        self.assertIn("outside the initial survey's client", result)
        fetch.assert_not_called()

    def test_tool_fails_closed_when_ownership_lookup_breaks(self):
        scope = {"survey_id": self.CURRENT, "organization_id": self.ORG}
        with (patch.dict(agent._SCOPE, scope, clear=True),
              patch.object(agent, "_survey_owner",
                           side_effect=agent.ScopeLookupUnavailable("db down"))):
            result = agent.get_survey_analysis_packet.invoke({"survey_id": self.TARGET})
        self.assertEqual(result, agent.PACKET_SCOPE_CHECK_FAILED)

    def test_missing_configured_benchmark_is_not_assumed_to_exist(self):
        benchmark_id = agent.BENCHMARK_SCOPE["survey_id"]
        scope = {"survey_id": self.CURRENT, "organization_id": self.ORG}
        with (patch.dict(agent._SCOPE, scope, clear=True),
              patch.object(agent, "_survey_owner", return_value=None),
              patch.object(agent, "_survey_analysis_packet_payload") as fetch):
            result = agent.get_survey_analysis_packet.invoke({"survey_id": benchmark_id})

        self.assertEqual(result, agent.packet_unknown_survey(benchmark_id))
        fetch.assert_not_called()


class _BarrierTool:
    def __init__(self, name: str, barrier: threading.Barrier | None = None):
        self.name = name
        self.barrier = barrier
        self.calls = []

    def invoke(self, args):
        self.calls.append(args)
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        return args.get("result", "ok")


class _RecordingTool(_BarrierTool):
    def __init__(self, name: str, order: list[str]):
        super().__init__(name)
        self.order = order

    def invoke(self, args):
        self.order.append(self.name)
        return args.get("result", "ok")


class _PartiallyFailingTool(_BarrierTool):
    def invoke(self, args):
        if args.get("fail"):
            raise RuntimeError("expected failure")
        return args.get("result", "ok")


class _RecordingModel:
    def __init__(self, content="answer"):
        self.calls = []
        self.content = content

    def invoke(self, messages, config=None, **kwargs):
        self.calls.append((messages, config, kwargs))
        return AIMessage(content=self.content)


class _MessageModel(_RecordingModel):
    def __init__(self, message: AIMessage):
        super().__init__()
        self.message = message

    def invoke(self, messages, config=None, **kwargs):
        self.calls.append((messages, config, kwargs))
        return self.message


VALID_PERSONA_RESPONSE = """> **PERSONA**

### 👥 Survey Respondent Snapshot

## 👤 WHO THEY ARE

| 👤 Profile | ✨ Persona snapshot | 🔎 Based on | 👥 Evidence base | 📊 Response coverage |
|---|---|---|---|---|
| 🎂 Age | The responses were mixed; no age group had a clear lead. | 📋 Direct answer: “What is your age?” | 50 of 100 people | 50% |

## 💡 MOTIVATIONS & BEHAVIORS

| 🎯 Motivation / behavior | 💬 Persona insight | 🔎 Based on | 👥 Evidence base | 📊 Response coverage |
|---|---|---|---|---|
| 🤝 Loyalty | Not available from this survey | 🚫 Not available | — | — |

👥 Mixed profile
"""


def _sql_call(
    call_id: str,
    result: str,
    *,
    sql: str | None = None,
    gathered: str = "",
    needed: str = "",
) -> dict:
    args = {
        "sql_query": sql or f"SELECT '{call_id}'",
        "result": result,
    }
    return {"name": "nl2sql_tool", "id": call_id, "args": args}


def _packet_call(call_id: str = "packet") -> dict:
    return {
        "name": "get_survey_analysis_packet",
        "id": call_id,
        "args": {"survey_id": "11111111-1111-1111-1111-111111111111"},
    }


def _stats_call(call_id: str, result: str) -> dict:
    return {
        "name": "run_survey_stats",
        "id": call_id,
        "args": {"result": result},
    }


def _tool_state(
    calls: list[dict], *, needed="", zero_streak=0,
    last_sql_observations: dict[str, str] | None = None,
):
    return {
        "messages": [AIMessage(content="", tool_calls=calls)],
        "thread_survey_id": "survey",
        "inventory_attached": False,
        "inventory_chars": 0,
        "number_of_steps": 1,
        "last_sql_observations": last_sql_observations or {},
        "zero_row_streak": zero_streak,
        "word_cloud_artifact": "",
        "word_cloud_intro": "",
    }


class WordCloudToolTests(unittest.TestCase):
    @staticmethod
    def _source(rows, *, question_type="open-answer"):
        return {
            "question_id": "11111111-1111-1111-1111-111111111111",
            "survey_id": "22222222-2222-2222-2222-222222222222",
            "prompt": "What did you like most about the {{sample}}?",
            "question_type": question_type,
            "language": "en",
            "survey_title": "Test survey",
            "category": "Protein Bars",
            "rows": rows,
        }

    def test_counts_distinct_answers_and_filters_deterministically(self):
        result = agent._build_word_cloud_artifact(self._source([
            {
                "answer_id": "a1", "answer_value":
                "Crunch crunch GOOD sample don't test@example.com https://example.com/x",
                "option_answer": None, "product_name": "Product 1",
            },
            {
                "answer_id": "a2", "answer_value": "good flavor crunch and sample",
                "option_answer": None, "product_name": "Product 2",
            },
            {
                "answer_id": "a3", "answer_value": "   ",
                "option_answer": None, "product_name": "Product 2",
            },
        ]), group_by_product=False, max_terms=50)

        self.assertEqual(result["payload"]["data"], [
            {"label": "crunch", "count": 2},
            {"label": "good", "count": 2},
            {"label": "flavor", "count": 1},
        ])
        self.assertEqual(result["payload"]["meta"], {
            "answer_count": 3,
            "filtered_answer_count": 2,
            "count_method": "answers_containing_term",
        })
        self.assertEqual(
            result["payload"]["subtitle"], "What did you like most about the [sample]?"
        )
        self.assertNotIn("example", result["artifact"])

    def test_product_grouping_uses_only_linked_nonblank_answers(self):
        result = agent._build_word_cloud_artifact(self._source([
            {"answer_id": "a1", "answer_value": "flavor crisp", "option_answer": None,
             "product_name": "Product 2"},
            {"answer_id": "a2", "answer_value": "flavor", "option_answer": None,
             "product_name": "Product 1"},
            {"answer_id": "a3", "answer_value": "flavor", "option_answer": None,
             "product_name": None},
        ]), group_by_product=True, max_terms=50)

        self.assertEqual(result["payload"]["data"], [
            {"group": "Product 1", "values": [{"label": "flavor", "count": 1}]},
            {"group": "Product 2", "values": [
                {"label": "crisp", "count": 1},
                {"label": "flavor", "count": 1},
            ]},
        ])
        self.assertEqual(result["payload"]["meta"]["answer_count"], 3)
        self.assertEqual(result["payload"]["meta"]["filtered_answer_count"], 2)

    def test_multiple_open_components_dedupe_at_answer_grain(self):
        result = agent._build_word_cloud_artifact(self._source([
            {"answer_id": "a1", "answer_value": None, "option_answer": "creamy smooth",
             "product_name": None},
            {"answer_id": "a1", "answer_value": None, "option_answer": "creamy rich",
             "product_name": None},
            {"answer_id": "a2", "answer_value": None, "option_answer": "creamy",
             "product_name": None},
        ], question_type="multiple-open-answer"), group_by_product=False, max_terms=50)
        counts = {item["label"]: item["count"] for item in result["payload"]["data"]}
        self.assertEqual(counts["creamy"], 2)
        self.assertEqual(result["payload"]["meta"]["answer_count"], 2)

    def test_tool_returns_internal_artifact_envelope_without_raw_answers(self):
        source = self._source([
            {"answer_id": "a1", "answer_value": "privateword flavor",
             "option_answer": None, "product_name": None},
        ])
        scope = {"survey_id": source["survey_id"]}
        with (
            patch.dict(agent._SCOPE, scope, clear=True),
            patch.object(agent, "_fetch_word_cloud_source", return_value=source),
        ):
            output = agent.generate_word_cloud.invoke({
                "question_id": source["question_id"],
                "group_by_product": False,
                "max_terms": 50,
            })
        self.assertTrue(output.startswith(agent._WORD_CLOUD_ARTIFACT_PREFIX))
        envelope = json.loads(output[len(agent._WORD_CLOUD_ARTIFACT_PREFIX):])
        self.assertIn("```gpi-chart", envelope["artifact"])
        self.assertNotIn("rows", envelope)

    def test_tool_node_hides_artifact_and_finalizer_skips_model(self):
        prepared = agent._build_word_cloud_artifact(self._source([
            {"answer_id": "a1", "answer_value": "flavor",
             "option_answer": None, "product_name": None},
        ]), group_by_product=False, max_terms=50)
        envelope = agent._WORD_CLOUD_ARTIFACT_PREFIX + json.dumps(prepared)
        fake = _BarrierTool("generate_word_cloud")
        call = {
            "name": "generate_word_cloud", "id": "cloud",
            "args": {"result": envelope},
        }
        with patch.dict(agent.tools_by_name, {"generate_word_cloud": fake}, clear=True):
            tool_update = agent.call_tool(_tool_state([call]))
        self.assertEqual(tool_update["word_cloud_artifact"], prepared["artifact"])
        self.assertNotIn("```gpi-chart", tool_update["messages"][0].content)

        state = _tool_state([])
        state["messages"] = [tool_update["messages"][0]]
        state["word_cloud_artifact"] = prepared["artifact"]
        state["word_cloud_intro"] = prepared["intro"]
        recording_model = _RecordingModel("This response must not be used.")
        with patch.object(agent, "model", recording_model):
            final_update = agent.call_model(state, config={})
        final = final_update["messages"][0].content
        self.assertEqual(final, prepared["intro"] + "\n\n" + prepared["artifact"])
        self.assertEqual(recording_model.calls, [])
        self.assertEqual(final.count("```gpi-chart"), 1)
        self.assertEqual(
            agent.should_continue({**state, **final_update}),
            "end",
        )

    def test_untrusted_model_word_cloud_is_withheld(self):
        state = _tool_state([])
        state["messages"] = [HumanMessage(content="make a word cloud")]
        draft = "```gpi-chart\n{\"type\":\"word_cloud\",\"data\":[{\"count\":999}]}\n```"
        with patch.object(agent, "model", _RecordingModel(draft)):
            final = agent.call_model(state, config={})["messages"][0].content
        self.assertNotIn("\"count\": 999", final)
        self.assertNotIn("999", final)
        self.assertEqual(final, agent.UNTRUSTED_WORD_CLOUD_BLOCK)


class ParallelSqlTests(unittest.TestCase):
    def test_active_sql_tool_schema_has_only_sql_query(self):
        properties = agent.nl2sql_tool.args_schema.model_json_schema()["properties"]
        self.assertEqual(set(properties), {"sql_query"})
        self.assertNotIn("internal routing", agent.NL2SQL_TOOL_DESCRIPTION)
        self.assertNotIn("information_still_needed", agent.NL2SQL_TOOL_DESCRIPTION)

    def test_agent_state_contains_no_routing_channels(self):
        # The point of this fixture is that no channel decides WHICH agent or path handles a
        # turn -- there is one graph and one model. Channels carrying durable DATA are fine:
        # parked_ids and analysis_missing hold what survived a failed isolated workflow run and
        # the gap it could not close, both of which outlive the ToolMessage that delivered them.
        self.assertEqual(
            set(agent.AgentState.__annotations__),
            {
                "messages", "thread_survey_id", "inventory_attached", "inventory_chars",
                "number_of_steps", "last_sql_observations",
                "zero_row_streak", "word_cloud_artifact", "word_cloud_intro",
                "pca_chart_artifacts", "parked_ids", "analysis_missing", "persona_profiles",
            },
        )

    def test_parallel_calls_overlap_but_outputs_keep_call_order(self):
        barrier = threading.Barrier(2)
        fake = _BarrierTool("nl2sql_tool", barrier)
        calls = [
            _sql_call("one", "first-result", gathered="products", needed="nothing"),
            _sql_call("two", "second-result", gathered="demographics", needed="nothing"),
        ]
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            update = agent.call_tool(_tool_state(calls))

        self.assertEqual([m.content for m in update["messages"]],
                         ["first-result", "second-result"])
        self.assertEqual([m.tool_call_id for m in update["messages"]], ["one", "two"])
        self.assertEqual(len(update["last_sql_observations"]), 2)
        self.assertNotIn("sql_done", update)

    def test_parallel_stats_calls_overlap_but_outputs_keep_call_order(self):
        barrier = threading.Barrier(2)
        fake = _BarrierTool("run_survey_stats", barrier)
        calls = [
            _stats_call("one", "first-stats-result"),
            _stats_call("two", "second-stats-result"),
        ]
        with patch.dict(agent.tools_by_name, {"run_survey_stats": fake}, clear=True):
            update = agent.call_tool(_tool_state(calls))

        self.assertEqual(
            [message.content for message in update["messages"]],
            ["first-stats-result", "second-stats-result"],
        )
        self.assertEqual(
            [message.tool_call_id for message in update["messages"]], ["one", "two"]
        )

    def test_parallel_stats_failure_is_isolated_and_order_is_preserved(self):
        fake = _PartiallyFailingTool("run_survey_stats")
        calls = [
            _stats_call("one", "first-stats-result"),
            {
                "name": "run_survey_stats",
                "id": "two",
                "args": {"result": "unused", "fail": True},
            },
            _stats_call("three", "third-stats-result"),
        ]
        with patch.dict(agent.tools_by_name, {"run_survey_stats": fake}, clear=True):
            update = agent.call_tool(_tool_state(calls))

        contents = [message.content for message in update["messages"]]
        self.assertEqual(contents[0], "first-stats-result")
        self.assertIn("expected failure", contents[1])
        self.assertEqual(contents[2], "third-stats-result")
        self.assertEqual(
            [message.tool_call_id for message in update["messages"]],
            ["one", "two", "three"],
        )

    def test_parallel_tool_executor_uses_configured_worker_limit(self):
        self.assertEqual(
            agent._TOOL_EXECUTOR._max_workers, agent.MAX_PARALLEL_TOOL_CALLS
        )

    def test_stats_batch_skips_database_and_http_requests_overlap(self):
        qids = [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]
        http_barrier = threading.Barrier(2)

        def fake_get(*_args, **_kwargs):
            http_barrier.wait(timeout=2)
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {
                    "questionType": "line-scale",
                    "respondentCount": 7,
                    "stats": {},
                },
            )

        scope = {
            "client_id": "client",
            "organization_id": "organization",
            "survey_id": "survey",
        }
        calls = [
            {
                "name": "run_survey_stats",
                "id": str(index),
                "args": {"question_id": qid, "stats_types": "anova,tukey"},
            }
            for index, qid in enumerate(qids)
        ]
        with (
            patch.dict(agent._SCOPE, scope, clear=True),
            patch.object(agent, "_authorize_question_ids", return_value=([], [])),
            patch.object(agent.requests, "get", side_effect=fake_get),
        ):
            update = agent.call_tool(_tool_state(calls))

        self.assertTrue(
            all("Question type: line-scale" in message.content
                for message in update["messages"])
        )

    def test_parallel_sql_observations_union_every_query(self):
        fake = _BarrierTool("nl2sql_tool")
        calls = [
            _sql_call("one", "ok", gathered="products", needed="nothing"),
            _sql_call("two", "ok", gathered="products", needed="need ages"),
        ]
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            update = agent.call_tool(_tool_state(calls))
        self.assertNotIn("sql_done", update)
        self.assertEqual(len(update["last_sql_observations"]), 2)

    def test_same_canonical_query_and_result_warns_on_next_sql_batch(self):
        fake = _BarrierTool("nl2sql_tool")
        first = _sql_call(
            "one", '[{"value":1}]', sql="SELECT :survey_id AS value"
        )
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            first_update = agent.call_tool(_tool_state([first]))
            repeated = _sql_call(
                "two", '[{"value":1}]', sql=" select :survey_id as VALUE ; "
            )
            second_update = agent.call_tool(_tool_state(
                [repeated], last_sql_observations=first_update["last_sql_observations"]
            ))

        self.assertTrue(
            second_update["messages"][0].content.startswith(agent.REPEATED_SQL_WARNING)
        )

    def test_same_query_with_changed_result_does_not_warn(self):
        fake = _BarrierTool("nl2sql_tool")
        first = _sql_call("one", '[{"value":1}]', sql="SELECT 1 AS value")
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            first_update = agent.call_tool(_tool_state([first]))
            changed = _sql_call("two", '[{"value":2}]', sql="select 1 as VALUE")
            second_update = agent.call_tool(_tool_state(
                [changed], last_sql_observations=first_update["last_sql_observations"]
            ))

        self.assertNotIn(agent.REPEATED_SQL_WARNING, second_update["messages"][0].content)

    def test_duplicate_query_and_result_inside_parallel_batch_warns(self):
        fake = _BarrierTool("nl2sql_tool", threading.Barrier(2))
        calls = [
            _sql_call("one", "same", sql="SELECT 1"),
            _sql_call("two", "same", sql=" select 1 ;"),
        ]
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            update = agent.call_tool(_tool_state(calls))
        self.assertTrue(update["messages"][0].content.startswith(agent.REPEATED_SQL_WARNING))

    def test_sql_error_is_returned_and_parallel_zeroes_count_once(self):
        fake = _BarrierTool("nl2sql_tool")
        error_calls = [
            _sql_call("one", "SQL error: bad query", gathered="x", needed="nothing"),
            _sql_call("two", "ok", gathered="x", needed="nothing"),
        ]
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            error_update = agent.call_tool(_tool_state(error_calls))
        self.assertNotIn("sql_done", error_update)
        self.assertEqual(error_update["messages"][0].content, "SQL error: bad query")

        zero_calls = [
            _sql_call("three", agent.ZERO_ROW_MSG, gathered="x", needed="need diagnosis"),
            _sql_call("four", agent.ZERO_ROW_MSG, gathered="x", needed="need diagnosis"),
        ]
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            zero_update = agent.call_tool(_tool_state(zero_calls, zero_streak=0))
        self.assertEqual(zero_update["zero_row_streak"], 1)

    def test_zero_row_updates_guardrail_only(self):
        fake = _BarrierTool("nl2sql_tool")
        call = _sql_call(
            "zero", agent.ZERO_ROW_MSG, gathered="matched both questions",
            needed="nothing",
        )
        with patch.dict(agent.tools_by_name, {"nl2sql_tool": fake}, clear=True):
            update = agent.call_tool(_tool_state([call]))

        self.assertNotIn("sql_done", update)
        self.assertEqual(update["zero_row_streak"], 1)

    def test_zero_row_prompt_directs_evidence_use_after_diagnostic(self):
        self.assertIn("After the diagnostic counts return", agent.ZERO_ROW_MSG)
        self.assertIn("do not repeat the same retrieval", agent.ZERO_ROW_MSG)
        self.assertNotIn("information_still_needed", agent.ZERO_ROW_MSG)

    def test_mixed_tool_batch_keeps_sequential_call_order(self):
        order: list[str] = []
        sql = _RecordingTool("nl2sql_tool", order)
        stats = _RecordingTool("run_survey_stats", order)
        calls = [
            _sql_call("one", "sql-result", gathered="x", needed="nothing"),
            {"name": "run_survey_stats", "id": "two", "args": {"result": "stats-result"}},
        ]
        with patch.dict(agent.tools_by_name, {
            "nl2sql_tool": sql,
            "run_survey_stats": stats,
        }, clear=True):
            update = agent.call_tool(_tool_state(calls))
        self.assertEqual(order, ["nl2sql_tool", "run_survey_stats"])
        self.assertEqual([m.content for m in update["messages"]],
                         ["sql-result", "stats-result"])

    def test_packet_result_does_not_create_routing_state(self):
        fake = _BarrierTool("get_survey_analysis_packet")
        call = _packet_call()
        with patch.dict(agent.tools_by_name, {"get_survey_analysis_packet": fake}, clear=True):
            update = agent.call_tool(_tool_state([call]))
        self.assertNotIn("sql_done", update)
        self.assertNotIn("analysis_packet_done", update)

    def test_every_ordinary_turn_uses_single_model_and_full_prompt(self):
        single = _RecordingModel()
        state = _tool_state([])
        state["messages"] = [HumanMessage(content="packet evidence")]
        with patch.object(agent, "model", single):
            agent.call_model(state, config={})
        self.assertEqual(len(single.calls), 1)
        self.assertIs(single.calls[0][0][0], agent.SYSTEM_PROMPT)

    def test_final_budget_turn_uses_unbound_single_model_and_full_prompt(self):
        single = _RecordingModel()
        state = _tool_state([])
        state["messages"] = [HumanMessage(content="partial evidence")]
        state["number_of_steps"] = agent.MAX_LLM_STEPS - 1
        with patch.object(agent, "llm", single):
            agent.call_model(state, config={})
        self.assertEqual(len(single.calls), 1)
        self.assertIs(single.calls[0][0][0], agent.SYSTEM_PROMPT)
        self.assertEqual(single.calls[0][0][-1].content, agent.STEP_BUDGET_NOTICE)


class HistoryTrimmingTests(unittest.TestCase):
    @staticmethod
    def _round(number: int) -> list:
        first = f"r{number}-sql-1"
        second = f"r{number}-sql-2"
        third = f"r{number}-sql-3"
        return [
            HumanMessage(content=f"round {number}"),
            AIMessage(content="", tool_calls=[{
                "name": "nl2sql_tool", "id": first,
                "args": {"sql_query": f"SELECT {number}1"},
            }]),
            ToolMessage(content=f"evidence {number}.1", name="nl2sql_tool",
                        tool_call_id=first),
            AIMessage(content="", tool_calls=[{
                "name": "nl2sql_tool", "id": second,
                "args": {"sql_query": f"SELECT {number}2"},
            }]),
            ToolMessage(content=f"evidence {number}.2", name="nl2sql_tool",
                        tool_call_id=second),
            AIMessage(content="", tool_calls=[{
                "name": "nl2sql_tool", "id": third,
                "args": {"sql_query": f"SELECT {number}3"},
            }]),
            ToolMessage(content=f"evidence {number}.3", name="nl2sql_tool",
                        tool_call_id=third),
            AIMessage(content=f"answer {number}"),
        ]

    def test_default_history_limits_cover_five_four_turn_rounds(self):
        self.assertEqual(agent.HISTORY_MAX_ROUNDS, 5)
        self.assertEqual(agent.HISTORY_MAX_MESSAGES, 47)
        history = [message for number in range(1, 6) for message in self._round(number)]
        self.assertEqual(len(history), 40)
        self.assertEqual(agent._trim_history(history), history)

    def test_sixth_round_drops_only_oldest_round_but_keeps_anchor(self):
        history = [message for number in range(1, 6) for message in self._round(number)]
        history += [HumanMessage(content="round 6")]
        trimmed = agent._trim_history(history)

        self.assertIs(trimmed[0], history[0])
        self.assertEqual(trimmed[1].content, "round 2")
        self.assertNotIn("answer 1", [getattr(message, "content", "") for message in trimmed])
        self.assertEqual(
            [message.content for message in trimmed if isinstance(message, HumanMessage)],
            ["round 1", "round 2", "round 3", "round 4", "round 5", "round 6"],
        )

    def test_trim_boundary_stays_fixed_during_active_round(self):
        history = [message for number in range(1, 6) for message in self._round(number)]
        history += [HumanMessage(content="round 6")]
        first_request = agent._trim_history(history)

        active_call = {"name": "nl2sql_tool", "id": "active", "args": {
            "sql_query": "SELECT 6"
        }}
        extended = history + [
            AIMessage(content="", tool_calls=[active_call]),
            ToolMessage(content="active evidence", name="nl2sql_tool",
                        tool_call_id="active"),
        ]
        second_request = agent._trim_history(extended)

        self.assertEqual(second_request[:len(first_request)], first_request)
        self.assertEqual(second_request[1].content, "round 2")

    def test_message_ceiling_drops_additional_complete_rounds(self):
        history = [message for number in range(1, 4) for message in self._round(number)]
        with (patch.object(agent, "HISTORY_MAX_ROUNDS", 5),
              patch.object(agent, "HISTORY_MAX_MESSAGES", 10)):
            trimmed = agent._trim_history(history)

        self.assertIs(trimmed[0], history[0])
        self.assertEqual(trimmed[1].content, "round 2")
        self.assertIsInstance(trimmed[1], HumanMessage)
        self.assertNotIsInstance(trimmed[1], ToolMessage)


class ProgressMemoryTests(unittest.TestCase):
    DELTA = (
        "<progress_gathered>\n"
        "- [source: nl2sql_tool / product roster] Products A and B were tested.\n"
        "- [source: run_survey_stats / liking] The overall test was significant.\n"
        "</progress_gathered>"
    )

    def test_prompt_defines_append_only_evidence_delta_contract(self):
        rules = instructions.PROGRESS_MEMORY_RULES
        self.assertIn("authoritative evidence", rules)
        self.assertIn("append-only DELTA", rules)
        self.assertIn("unioning useful evidence", rules)
        self.assertIn("at most 12", rules)
        self.assertIn("at most 2,000 characters", rules)
        self.assertIn("final answer", rules)
        self.assertIn(rules, agent.SYSTEM_PROMPT_TEXT)

    def test_post_tool_delta_is_persisted_with_next_tool_calls(self):
        next_call = {"name": "nl2sql_tool", "id": "next", "args": {"sql_query": "SELECT 2"}}
        response = AIMessage(content=self.DELTA, tool_calls=[next_call])
        recording = _MessageModel(response)
        state = _tool_state([])
        state["messages"] = [
            HumanMessage(content="compare products"),
            AIMessage(content="", tool_calls=[
                {"name": "nl2sql_tool", "id": "first", "args": {"sql_query": "SELECT 1"}}
            ]),
            ToolMessage(content="first evidence", name="nl2sql_tool", tool_call_id="first"),
        ]
        with patch.object(agent, "model", recording):
            update = agent.call_model(state, config={})

        stored = update["messages"][0]
        self.assertIs(stored, response)
        self.assertEqual(stored.content, self.DELTA)
        self.assertEqual(stored.tool_calls[0]["name"], next_call["name"])
        self.assertEqual(stored.tool_calls[0]["id"], next_call["id"])
        self.assertEqual(stored.tool_calls[0]["args"], next_call["args"])
        self.assertFalse(any(
            isinstance(message, HumanMessage) and "PROGRESS TRACKER" in message.content
            for message in recording.calls[0][0]
        ))

    def test_responses_api_serializes_delta_before_function_call(self):
        from langchain_openai.chat_models.base import _construct_responses_api_input

        assistant = AIMessage(content=self.DELTA, tool_calls=[{
            "name": "nl2sql_tool", "id": "next", "args": {"sql_query": "SELECT 2"}
        }])
        payload = _construct_responses_api_input([
            HumanMessage(content="question"),
            assistant,
            ToolMessage(content="result", name="nl2sql_tool", tool_call_id="next"),
        ])

        assistant_text_index = next(
            index for index, item in enumerate(payload)
            if item.get("role") == "assistant"
        )
        function_index = next(
            index for index, item in enumerate(payload)
            if item.get("type") == "function_call"
        )
        self.assertLess(assistant_text_index, function_index)
        self.assertEqual(payload[assistant_text_index]["content"][0]["text"], self.DELTA)

    def test_later_request_extends_earlier_request_exactly(self):
        first_tool_call = {
            "name": "nl2sql_tool", "id": "first", "args": {"sql_query": "SELECT 1"}
        }
        next_tool_call = {
            "name": "nl2sql_tool", "id": "next", "args": {"sql_query": "SELECT 2"}
        }
        base_history = [
            HumanMessage(content="question"),
            AIMessage(content="", tool_calls=[first_tool_call]),
            ToolMessage(content="evidence one", name="nl2sql_tool", tool_call_id="first"),
        ]
        intermediate = AIMessage(content=self.DELTA, tool_calls=[next_tool_call])
        first_model = _MessageModel(intermediate)
        state = _tool_state([])
        state["messages"] = base_history
        with patch.object(agent, "model", first_model):
            agent.call_model(state, config={})
        request_one = first_model.calls[0][0]

        second_model = _RecordingModel("final answer")
        state["messages"] = base_history + [
            intermediate,
            ToolMessage(content="evidence two", name="nl2sql_tool", tool_call_id="next"),
        ]
        state["number_of_steps"] = 2
        with patch.object(agent, "model", second_model):
            agent.call_model(state, config={})
        request_two = second_model.calls[0][0]

        self.assertEqual(request_two[:len(request_one)], request_one)

    def test_completed_and_streaming_filters_hide_internal_blocks(self):
        public = agent.message_to_text(AIMessage(
            content="Before\n" + self.DELTA + "\nAfter"
        ))
        self.assertEqual(public, "Before\n\nAfter")
        self.assertNotIn("progress_gathered", public)

        stream_filter = agent.ProgressDeltaStreamFilter()
        chunks = ["Before<pro", "gress_gathered>secret", "</progress_", "gathered>After"]
        visible = "".join(stream_filter.feed(chunk) for chunk in chunks)
        visible += stream_filter.finish()
        self.assertEqual(visible, "BeforeAfter")

    def test_brace_stream_filter_emits_balanced_spans_atomically(self):
        stream_filter = agent.BraceDeltaStreamFilter()
        chunks = ["Before {\"x\": ", "{\"y\": 1", "}} After"]
        visible = "".join(stream_filter.feed(chunk) for chunk in chunks)
        visible += stream_filter.finish()
        self.assertEqual(visible, 'Before {"x": {"y": 1}} After')
        self.assertEqual(agent.BraceDeltaStreamFilter().feed("plain text"), "plain text")

    def test_missing_malformed_and_oversized_deltas_are_observational(self):
        self.assertEqual(agent._progress_delta_status("ordinary text")[0], "missing")
        self.assertEqual(
            agent._progress_delta_status("<progress_gathered>- fact")[0], "malformed"
        )
        oversized = (
            "<progress_gathered>\n- [source: tool] "
            + ("x" * 2100)
            + "\n</progress_gathered>"
        )
        self.assertEqual(agent._progress_delta_status(oversized)[0], "oversized")


class PersonaSingleModelTests(unittest.TestCase):
    def test_reference_persona_uses_markdown_contract(self):
        self.assertIn("> **PERSONA**", VALID_PERSONA_RESPONSE)
        self.assertIn("### 👥 Survey Respondent Snapshot", VALID_PERSONA_RESPONSE)
        self.assertIn("|---|---|---|---|---|", VALID_PERSONA_RESPONSE)
        self.assertNotIn("<table>", VALID_PERSONA_RESPONSE)
        self.assertNotIn("persona-card", VALID_PERSONA_RESPONSE)

    def test_persona_closing_turn_uses_single_model(self):
        single = _RecordingModel(VALID_PERSONA_RESPONSE)
        state = _tool_state([])
        state["messages"] = [HumanMessage(content="persona evidence")]
        with patch.object(agent, "model", single):
            update = agent.call_model(state, config={})
        self.assertEqual(update["messages"][0].content, VALID_PERSONA_RESPONSE)
        self.assertEqual(len(single.calls), 1)
        self.assertIs(single.calls[0][0][0], agent.SYSTEM_PROMPT)

    def test_persona_final_draft_is_returned_without_format_repair(self):
        draft = "Persona as prose without the preferred table format"
        initial = _RecordingModel(draft)
        state = _tool_state([])
        state["messages"] = [HumanMessage(content="persona evidence")]
        with patch.object(agent, "model", initial):
            update = agent.call_model(state, config={})
        self.assertEqual(update["messages"][0].content, draft)
        self.assertEqual(len(initial.calls), 1)


class PrefixTests(unittest.TestCase):
    def test_prompt_cache_key_is_stable_and_scoped_to_conversation_thread(self):
        thread_id = "customer@example.com/private-thread"
        config = {"configurable": {"thread_id": thread_id}}
        first = agent._conversation_prompt_cache_key(config)
        second = agent._conversation_prompt_cache_key(config)
        other = agent._conversation_prompt_cache_key({
            "configurable": {"thread_id": "another-thread"}
        })

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith("funda-thread:"))
        self.assertLessEqual(len(first), 64)
        self.assertNotIn(thread_id, first)
        self.assertIsNone(agent._conversation_prompt_cache_key({}))

        recording = _RecordingModel()
        state = _tool_state([])
        state["messages"] = [HumanMessage(content="question")]
        with patch.object(agent, "model", recording):
            agent.call_model(state, config=config)
        self.assertEqual(recording.calls[0][2]["prompt_cache_key"], first)

        payload = agent.llm._get_request_payload(
            [HumanMessage(content="question")], prompt_cache_key=first
        )
        self.assertEqual(payload["prompt_cache_key"], first)

    def test_prompt_keeps_current_default_but_allows_requested_same_client_jumps(self):
        prompt = instructions.SYSTEM_PROMPT_TEXT
        self.assertIn("an ordinary question stays in that survey", prompt)
        self.assertIn("the comparison request itself is sufficient authorization", prompt)
        self.assertIn("does NOT imply a survey jump", prompt)
        self.assertIn("across every organization owned by client_id", prompt)
        self.assertIn("Never reference or query data belonging to any other client_id", prompt)
        self.assertIn(
            "Same-client survey discovery may cross organizations",
            agent.NL2SQL_TOOL_DESCRIPTION,
        )

    def test_general_unclear_intent_policy_is_always_on(self):
        prompt = instructions.SYSTEM_PROMPT_TEXT
        normalized = " ".join(prompt.split())
        self.assertIn("GENERAL UNCLEAR-INTENT POLICY -- ASSUME, ANSWER, OFFER REFINEMENT", prompt)
        self.assertIn("Apply this on every turn", prompt)
        self.assertIn("whether or not a pre-fetched inventory is available", prompt)
        self.assertIn("identify EXACTLY what is open", prompt)
        self.assertIn("non-confrontational OPENING clause", prompt)
        self.assertIn("do not replace it with only a generic", prompt)
        self.assertIn("You haven't specified exactly which measure to use", normalized)
        self.assertIn("explicit wording and conversation context", prompt)
        self.assertIn("current survey inventory or DB evidence", prompt)
        self.assertIn("excluding the interpretation already answered", prompt)
        self.assertIn("One real alternative is better than filler", prompt)
        self.assertIn("must NEVER fabricate a required tool input", prompt)
        self.assertIn("When a required tool input cannot be resolved uniquely", prompt)
        self.assertIn("do not make the call", prompt)
        self.assertIn("UNCLEAR INTENT IN THE FINAL ANSWER", prompt)
        self.assertIn("the final answer MUST open by naming that exact detail softly", normalized)
        self.assertIn("An answer completed under a material assumption MUST", agent.INVENTORY_PREAMBLE)
        self.assertIn("One suggestion is", agent.INVENTORY_PREAMBLE)
        self.assertIn("valid when exactly one genuine alternative exists", agent.INVENTORY_PREAMBLE)
        self.assertNotIn("never offer exactly ONE", agent.INVENTORY_PREAMBLE)

    def test_one_genuine_clarification_suggestion_is_valid(self):
        from scripts.check_suggestions import validate

        checks = validate(
            "You haven't specified which measure defines performance, so I'm assuming overall "
            "liking because it is the survey's overall evaluation measure.\n\n"
            "{{Rank products by purchase intent}}"
        )
        self.assertTrue(all(good for _, good, _ in checks), checks)

    def test_summary_rule_is_scoped_to_the_current_request(self):
        """A summary turn must not suppress nesting on later turns.

        Deliberately separate from the hash test: that method asserts the hash first, so a
        hash mismatch short-circuits before any wording check runs. Bumping a failed hash
        would otherwise retire this rule silently -- which is how the over-broad trigger
        shipped in the first place.
        """
        normalized_prompt = " ".join(instructions.SYSTEM_PROMPT_TEXT.split())
        self.assertIn("Classify the CURRENT user message against this rule", normalized_prompt)
        self.assertIn("never carries over from an earlier turn", normalized_prompt)
        self.assertIn("even when an earlier turn in this conversation was a summary",
                      normalized_prompt)
        self.assertIn("sets no precedent for any later request", normalized_prompt)
        # Part of the same block: the audience clause. An always-on variant was tried and
        # reverted -- measured over 12 --no-inventory runs it changed nothing (0/6 jargon-clean
        # in both arms), so the fuller rule in INVENTORY_PREAMBLE stays the general carrier.
        self.assertIn("count PEOPLE rather than rows", normalized_prompt)
        # The retired trigger: "summary or product comparison" pulled the flat-table rule
        # onto the request class that most needs nesting.
        self.assertNotIn("survey summary or product comparison", normalized_prompt)

    def test_system_prompt_hash_matches_intentional_prompt_update(self):
        self.assertEqual(agent.SYSTEM_PROMPT_TEXT, instructions.SYSTEM_PROMPT_TEXT)
        self.assertFalse(hasattr(instructions, "SYSTEM_PROMPT_LEAN_TEXT"))
        self.assertIn(instructions.SCHEMA_OVERVIEW, agent.SYSTEM_PROMPT_TEXT)
        self.assertIn("Use Markdown, including flat tables", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Make the answer easy to scan with SELECTIVE emphasis",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Keep each value and unit together", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Do not bold whole paragraphs, every table cell",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("at most one or two short phrases with `<u>...</u>`",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("never underline headings, tables, whole", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("never put Markdown formatting inside `<u>`",
                      instructions.SYSTEM_PROMPT_TEXT)
        # Hierarchy is selected semantically from a conceptual tidy view of any tool result, and
        # each branch recurses only while another useful dimension improves readability.
        self.assertIn("ADAPTIVE HIERARCHICAL JSON RESULTS",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("from EVERY tool", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("NORMALIZE BEFORE CHOOSING DEPTH", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Conceptually unpivot wide rows", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("wide column header is still a", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("A GROUPING DIMENSION is a human-readable categorical label",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("A MEASURE-LABEL DIMENSION", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("It IS eligible as a grouping node", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("A LEAF VALUE is the scalar fact", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("JUDGE COMPLEXITY AT EVERY NODE", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("THERE IS NO FIXED ROW OR COLUMN THRESHOLD",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("repeated or compound column families", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Column count alone does not license invented groups",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("BUILD AN ADAPTIVE FORM", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("There is NO mandatory minimum depth", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("A readable child may stop after one `Details` edge",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Reassess every child independently", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Do not stop merely because one nesting level exists",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Use at most FOUR", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("A node may have MANY sibling rows", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("one branch may recurse deeper than another", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("RECURSE AT EVERY NODE", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("column-header and nested-key dimension", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("RECURSIVE JSON GRAMMAR", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("opening fence is `gpi-nested-table`", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn('`{"type":"table","columns":[...],"rows":[...]}`',
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("an array of OBJECTS only -- never positional arrays",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("reserve `Details` as the final column",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("At a terminal table, omit the `Details` column",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("The example shows one readable nesting level",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("BUILD EVERY TERMINAL TABLE SEMANTICALLY",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("SCALE METADATA HAS STRICT PRECEDENCE", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Keep N,", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("JSON SELF-CHECK BEFORE SENDING", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("every returned fact appears exactly once",
                         instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Never emit `<details>`, `<summary>`, HTML table tags, CSS",
                         instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("RECURSIVE HTML GRAMMAR", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("STYLE THE RECURSIVE HTML INLINE", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("HTML TOKEN INTEGRITY", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("use AT LEAST TWO nested", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("background:#5063A2", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("font-size:11.5px", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Make a multidimensional result readable",
                      prompts.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("Prefer tidy\n      aggregate rows", prompts.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("conceptually unpivot them", prompts.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("children of children", prompts.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("Construct checks may need scale metadata",
                      prompts.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("is a high-confidence signal", prompts.wide_result_note(2, 10))
        self.assertIn("there is no mandatory minimum depth",
                      prompts.wide_result_note(2, 10))
        self.assertIn("add another `Details` table only while",
                      prompts.wide_result_note(2, 10))
        self.assertIn("REQUESTED-CONSTRUCT GATE", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("A descriptive or intensity scale is NOT liking",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("construct mismatch is not a framing gap".lower(),
                      instructions.SYSTEM_PROMPT_TEXT.lower())
        self.assertIn("conceptually normalize these", instructions.INVENTORY_PREAMBLE)
        self.assertIn("Do not blindly make Product the first level",
                      instructions.INVENTORY_PREAMBLE)
        self.assertIn("Treat by_attribute labels as candidate child",
                      instructions.INVENTORY_PREAMBLE)
        self.assertIn("everything else, including personas, stays Markdown",
                      instructions.SYSTEM_PROMPT_TEXT)
        # Word-cloud selection remains semantic, while data, counting and serialization are
        # deterministic; generic chart payloads are allowed only after bounded JSON validation.
        self.assertIn("fenced block opened by ```gpi-chart",
                      " ".join(instructions.SYSTEM_PROMPT_TEXT.split()))
        self.assertIn("call `generate_word_cloud` exactly once",
                      instructions.SYSTEM_PROMPT_TEXT)
        normalized_prompt = " ".join(instructions.SYSTEM_PROMPT_TEXT.split())
        self.assertIn("Never use `nl2sql_tool`", normalized_prompt)
        self.assertIn("runtime owns PCA and word-cloud chart JSON", normalized_prompt)
        self.assertIn("CHART RECOVERY", normalized_prompt)
        self.assertIn("MUST render the requested chart", normalized_prompt)
        self.assertIn("MUST still end, after the chart block", normalized_prompt)
        self.assertIn("counts DISTINCT answers containing each term",
                      prompts.GENERATE_WORD_CLOUD_DESCRIPTION)
        self.assertIn("fixed function-word and subject-term sets",
                      " ".join(prompts.GENERATE_WORD_CLOUD_DESCRIPTION.split()))
        # The UNION/ORDER BY dialect rule was retired: _repair_setop_order_by fixes that shape
        # deterministically in the executor, so carrying it in the prompt is dead weight.
        self.assertNotIn("UNION`/`INTERSECT", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("Render tables as semantic HTML", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("persona-card", agent.SYSTEM_PROMPT_TEXT)
        self.assertIn("`observed` is min/max response data, not scale metadata",
                      agent.INVENTORY_PREAMBLE)
        self.assertIn("only as an observed range, never as scale endpoints",
                      agent.INVENTORY_PREAMBLE)
        # The scale block names three separate things and the model must not conflate them:
        # slider_min/max is the range, the anchors are the endpoint labels, and
        # labelled_positions counts labels -- it is not a scale-point count (it disagrees with
        # the configured range on 53% of the questions carrying both).
        self.assertIn("State a scale range or its endpoint", agent.INVENTORY_PREAMBLE)
        self.assertIn("slider_min/slider_max for the range", agent.INVENTORY_PREAMBLE)
        self.assertIn("never from labelled_positions", agent.INVENTORY_PREAMBLE)
        self.assertIn("otherwise omit the scale", agent.INVENTORY_PREAMBLE)
        self.assertIn("labelled_positions` counts only how many positions carry a label",
                      instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("scale_points", instructions.SYSTEM_PROMPT_TEXT)
        # The catalog contract the packet now ships, and the rule that keeps a budget-trimmed
        # label list from reading as an absent one -- the original bug in a new costume.
        self.assertIn("catalog", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("other_answered_measures", instructions.SYSTEM_PROMPT_TEXT)
        self.assertNotIn("sample_option_labels", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("categories_omitted=true means NOT FETCHED, never ABSENT",
                      agent.INVENTORY_PREAMBLE)
        self.assertIn("answered=false means CONFIGURED WITH NO RESPONSES",
                      agent.INVENTORY_PREAMBLE)
        self.assertIn("components are the question's SUB-ITEMS", agent.INVENTORY_PREAMBLE)
        self.assertIn("response_categories are the VALUES", agent.INVENTORY_PREAMBLE)
        self.assertIn("Both measure sections are COLUMNAR", agent.INVENTORY_PREAMBLE)
        self.assertIn("configured benchmark ids grant access", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Never report a benchmark figure unless", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("no configured benchmark is available", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("do not invent or substitute another benchmark", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("Never parallelize a discovery query", agent.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("theme membership is multi-label", agent.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("never use one CASE expression", agent.NL2SQL_TOOL_DESCRIPTION)
        self.assertNotIn("information_still_needed", agent.NL2SQL_TOOL_DESCRIPTION)
        self.assertIn("append-only DELTA", instructions.SYSTEM_PROMPT_TEXT)
        self.assertIn("same JSON contract", agent.GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION)
        # LAST on purpose. This assertion used to run first, which made every content
        # check above it unreachable whenever the hash moved: a failed hash got bumped and
        # any clause dropped in the same edit went unreported. Keep it at the end so the
        # prompt is described by name before it is described by digest.
        self.assertEqual(hashlib.sha256(agent.SYSTEM_PROMPT_TEXT.encode()).hexdigest(), SYSTEM_HASH)

    def test_nested_table_prompt_example_is_valid_recursive_object_rows(self):
        match = re.search(
            r"EXAMPLE SHAPE:\n```gpi-nested-table\n(.*?)\n```",
            instructions.NESTED_RESULT_RULES,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        root = json.loads(match.group(1))

        def check_table(node, *, expect_nested):
            self.assertEqual(list(node), ["type", "columns", "rows"])
            self.assertEqual(node["type"], "table")
            self.assertTrue(node["columns"])
            self.assertEqual(len(node["columns"]), len(set(node["columns"])))
            for row in node["rows"]:
                self.assertIsInstance(row, dict)
                self.assertEqual(list(row), node["columns"])
            if expect_nested:
                self.assertEqual(node["columns"][-1], "Details")
                for row in node["rows"]:
                    check_table(row["Details"], expect_nested=False)
            else:
                self.assertNotIn("Details", node["columns"])

        check_table(root, expect_nested=True)


class _FakeApiGraph:
    def __init__(self, previous: dict):
        self.previous = previous
        self.inputs = []

    def get_state(self, _config):
        return SimpleNamespace(values=self.previous)

    async def astream(self, inputs, **_kwargs):
        self.inputs.append(inputs)
        if False:
            yield None


class _StreamingFakeApiGraph(_FakeApiGraph):
    async def astream(self, inputs, **_kwargs):
        self.inputs.append(inputs)
        answer = VALID_PERSONA_RESPONSE.strip()
        yield "messages", (AIMessageChunk(content=answer), {})
        yield "updates", {"LLM": {"messages": [AIMessage(content=answer)]}}


class _ProgressStreamingFakeApiGraph(_FakeApiGraph):
    async def astream(self, inputs, **_kwargs):
        self.inputs.append(inputs)
        internal = "<progress_gathered>\n- secret evidence\n</progress_gathered>"
        for chunk in ["<pro", "gress_gathered>\n- secret", " evidence\n</progress_gathered>"]:
            yield "messages", (AIMessageChunk(content=chunk), {})
        yield "updates", {"LLM": {"messages": [AIMessage(
            content=internal,
            tool_calls=[{"name": "nl2sql_tool", "id": "call", "args": {"sql_query": "SELECT 1"}}],
        )]}}
        final = "Answer " + internal + " done"
        for chunk in ["Answer <progress_", "gathered>secret</progress_", "gathered> done"]:
            yield "messages", (AIMessageChunk(content=chunk), {})
        yield "updates", {"LLM": {"messages": [AIMessage(content=final)]}}


class _FinalizedArtifactFakeApiGraph(_FakeApiGraph):
    async def astream(self, inputs, **_kwargs):
        self.inputs.append(inputs)
        streamed = "PCA summary.\n\n{{Show a 2D PCA plot of Appearance}}"
        finalized = (
            "PCA summary.\n\n```gpi-chart\n"
            '{"version":1,"type":"pca_biplot_3d"}\n```\n\n'
            "{{Show a 2D PCA plot of Appearance}}"
        )
        yield "messages", (AIMessageChunk(content=streamed), {})
        yield "updates", {"LLM": {"messages": [AIMessage(content=finalized)]}}


class ApiThreadTests(unittest.TestCase):
    @staticmethod
    async def _collect(api, request):
        return [json.loads(line) async for line in api._run(request)]

    def test_follow_up_uses_raw_prompt_and_never_refetches_inventory(self):
        import api_funda_agent_exp as api

        graph = _FakeApiGraph({
            "messages": [HumanMessage(content="original-prefix")],
            "thread_survey_id": "survey-1",
            "inventory_attached": True,
            "inventory_chars": 1234,
        })
        request = api.AskRequest(
            prompt="follow up",
            survey_id="survey-1",
            client_id="client",
            organization_id="org",
            thread_id="thread",
        )
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.agent, "survey_inventory",
                           side_effect=AssertionError("inventory was refetched"))):
            events = asyncio.run(self._collect(api, request))

        self.assertEqual(graph.inputs[0]["messages"][0].content, "follow up")
        self.assertNotIn("PRE-FETCHED INVENTORY", graph.inputs[0]["messages"][0].content)
        inventory_event = next(e for e in events if e.get("stage") == "inventory")
        self.assertEqual(inventory_event["chars"], 0)
        self.assertTrue(inventory_event["reused"])

    def test_fresh_thread_attaches_inventory_once(self):
        import api_funda_agent_exp as api

        async def immediate(func, *args):
            return func(*args)

        graph = _FakeApiGraph({})
        request = api.AskRequest(
            prompt="first question",
            survey_id="survey-1",
            client_id="client",
            organization_id="org",
            thread_id="thread",
        )
        inventory = agent.INVENTORY_PREAMBLE + '{"products":[]}'
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.asyncio, "to_thread", side_effect=immediate),
              patch.object(api.agent, "survey_inventory", return_value=inventory) as fetch):
            events = asyncio.run(self._collect(api, request))

        fetch.assert_called_once_with("survey-1")
        message = graph.inputs[0]["messages"][0].content
        self.assertTrue(message.endswith(inventory))
        self.assertNotIn("reused", message)
        self.assertTrue(graph.inputs[0]["inventory_attached"])
        self.assertEqual(graph.inputs[0]["last_sql_observations"], {})
        self.assertNotIn("progress_gathered", graph.inputs[0])
        self.assertNotIn("progress_needed", graph.inputs[0])
        self.assertNotIn("analysis_packet_done", graph.inputs[0])
        self.assertNotIn("sql_done", graph.inputs[0])
        inventory_event = next(e for e in events if e.get("stage") == "inventory")
        self.assertFalse(inventory_event["reused"])

    def test_persona_request_attaches_dedicated_inventory_on_fresh_and_follow_up_turns(self):
        import api_funda_agent_exp as api

        async def immediate(func, *args):
            return func(*args)

        persona = agent.PERSONA_INVENTORY_PREAMBLE + '{"categorical_questions":[]}'
        fresh_graph = _FakeApiGraph({})
        fresh_request = api.AskRequest(
            prompt="Create a persona", survey_id="survey-1",
            client_id="client", organization_id="org", thread_id="fresh",
        )
        with (patch.object(api, "GRAPH", fresh_graph),
              patch.object(api.asyncio, "to_thread", side_effect=immediate),
              patch.object(api.agent, "survey_inventory", return_value=""),
              patch.object(api.agent, "survey_persona_inventory", return_value=persona) as fetch):
            fresh_events = asyncio.run(self._collect(api, fresh_request))
        fetch.assert_called_once_with("survey-1")
        self.assertTrue(fresh_graph.inputs[0]["messages"][0].content.endswith(persona))
        self.assertNotIn("persona_request", fresh_graph.inputs[0])
        self.assertEqual(
            next(e for e in fresh_events if e.get("stage") == "persona_inventory")["chars"],
            len(persona),
        )

        follow_graph = _FakeApiGraph({
            "messages": [HumanMessage(content="original-prefix")],
            "thread_survey_id": "survey-1", "inventory_attached": True,
            "inventory_chars": 123,
        })
        follow_request = api.AskRequest(
            prompt="What is the respondent persona?", survey_id="survey-1",
            client_id="client", organization_id="org", thread_id="follow",
        )
        with (patch.object(api, "GRAPH", follow_graph),
              patch.object(api.asyncio, "to_thread", side_effect=immediate),
              patch.object(api.agent, "survey_inventory",
                           side_effect=AssertionError("general inventory was refetched")),
              patch.object(api.agent, "survey_persona_inventory", return_value=persona)):
            asyncio.run(self._collect(api, follow_request))
        self.assertEqual(follow_graph.inputs[0]["messages"][0].content,
                         follow_request.prompt + persona)
        self.assertNotIn("persona_request", follow_graph.inputs[0])

    def test_persona_api_streams_final_answer_normally(self):
        import api_funda_agent_exp as api

        async def immediate(func, *args):
            return func(*args)

        graph = _StreamingFakeApiGraph({})
        request = api.AskRequest(
            prompt="Summarize motivations and loyalty", survey_id="survey-1",
            client_id="client", organization_id="org", thread_id="streamed",
        )
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.asyncio, "to_thread", side_effect=immediate),
              patch.object(api.agent, "survey_inventory", return_value=""),
              patch.object(api.agent, "survey_persona_inventory", return_value="persona")):
            events = asyncio.run(self._collect(api, request))
        tokens = [event["text"] for event in events if event.get("type") == "token"]
        self.assertEqual(tokens, [VALID_PERSONA_RESPONSE.strip()])
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual("".join(tokens), done["answer"])
        self.assertNotIn("persona_request", graph.inputs[0])

    def test_api_resets_and_replays_finalized_trusted_artifact(self):
        import api_funda_agent_exp as api

        async def immediate(func, *args):
            return func(*args)

        graph = _FinalizedArtifactFakeApiGraph({})
        request = api.AskRequest(
            prompt="Run PCA on Appearance", survey_id="survey-1",
            client_id="client", organization_id="org", thread_id="pca-stream",
        )
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.asyncio, "to_thread", side_effect=immediate),
              patch.object(api.agent, "survey_inventory", return_value="")):
            events = asyncio.run(self._collect(api, request))

        reset_index = max(
            index for index, event in enumerate(events) if event.get("type") == "reset"
        )
        visible = "".join(
            event["text"] for event in events[reset_index + 1:]
            if event.get("type") == "token"
        )
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual(visible, done["answer"])
        self.assertIn("```gpi-chart", done["answer"])
        self.assertTrue(done["answer"].endswith("{{Show a 2D PCA plot of Appearance}}"))
        self.assertEqual(graph.inputs[0]["pca_chart_artifacts"], [])

    def test_api_never_streams_or_returns_internal_progress_blocks(self):
        import api_funda_agent_exp as api

        async def immediate(func, *args):
            return func(*args)

        graph = _ProgressStreamingFakeApiGraph({})
        request = api.AskRequest(
            prompt="question", survey_id="survey-1", client_id="client",
            organization_id="org", thread_id="progress-filter",
        )
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.asyncio, "to_thread", side_effect=immediate),
              patch.object(api.agent, "survey_inventory", return_value="")):
            events = asyncio.run(self._collect(api, request))

        serialized = json.dumps(events)
        self.assertNotIn("secret evidence", serialized)
        self.assertNotIn("progress_gathered", serialized)
        self.assertTrue(any(event.get("type") == "reset" for event in events))
        tokens = "".join(event["text"] for event in events if event.get("type") == "token")
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual(tokens, "Answer  done")
        self.assertEqual(done["answer"], "Answer  done")

    def test_health_reports_only_canonical_model_configuration(self):
        import api_funda_agent_exp as api

        payload = asyncio.run(api.health())
        self.assertEqual(payload["model"], agent.llm.model_name)
        self.assertEqual(payload["reasoning_effort"], agent.MODEL_REASONING_EFFORT)
        self.assertEqual(payload["history_max_rounds"], 5)
        self.assertEqual(payload["history_max_messages"], 47)
        self.assertNotIn("model_strong", payload)
        self.assertNotIn("model_weak", payload)

    def test_no_inventory_disables_persona_prefetch_too(self):
        import api_funda_agent_exp as api

        graph = _FakeApiGraph({})
        request = api.AskRequest(
            prompt="Persona", survey_id="survey-1", client_id="client",
            organization_id="org", thread_id="thread", no_inventory=True,
        )
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.agent, "survey_persona_inventory",
                           side_effect=AssertionError("persona inventory was fetched"))):
            asyncio.run(self._collect(api, request))
        self.assertNotIn("PRE-FETCHED PERSONA INVENTORY",
                         graph.inputs[0]["messages"][0].content)

    def test_thread_started_without_inventory_stays_inventory_free(self):
        import api_funda_agent_exp as api

        graph = _FakeApiGraph({
            "messages": [HumanMessage(content="scope without inventory")],
            "thread_survey_id": "survey-1",
            "inventory_attached": False,
            "inventory_chars": 0,
        })
        request = api.AskRequest(
            prompt="follow up",
            survey_id="survey-1",
            client_id="client",
            organization_id="org",
            thread_id="thread",
        )
        with (patch.object(api, "GRAPH", graph),
              patch.object(api.agent, "survey_inventory",
                           side_effect=AssertionError("inventory was fetched"))):
            events = asyncio.run(self._collect(api, request))
        self.assertEqual(graph.inputs[0]["messages"][0].content, "follow up")
        inventory_event = next(e for e in events if e.get("stage") == "inventory")
        self.assertFalse(inventory_event["reused"])

    def test_different_survey_is_rejected_before_scope_or_graph_mutation(self):
        import api_funda_agent_exp as api

        graph = _FakeApiGraph({
            "messages": [HumanMessage(content="original-prefix")],
            "thread_survey_id": "survey-1",
        })
        request = api.AskRequest(
            prompt="follow up",
            survey_id="survey-2",
            client_id="client",
            organization_id="org",
            thread_id="thread",
        )
        with patch.object(api, "GRAPH", graph):
            events = asyncio.run(self._collect(api, request))
        self.assertEqual(events[0]["code"], "thread_survey_mismatch")
        self.assertEqual(graph.inputs, [])


class IsolatedWorkflowInstructionTests(unittest.TestCase):
    """The two audits new_tool.md §8.1 requires of the isolated workflow's instruction surface.

    Isolation here is a property of the SOURCE, not of a runtime check, so both tests read the
    module rather than exercising it: an import edge added later is caught even if no test
    happens to invoke the offending path.
    """

    MAIN_INSTRUCTION_MODULES = ("agent_instructions", "tool_prompts")

    def test_no_import_edge_to_the_main_instruction_modules(self):
        source = pathlib.Path(
            workflow_instructions.__file__
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for module in self.MAIN_INSTRUCTION_MODULES:
            self.assertNotIn(module, imported)
        # ast.walk covers late imports inside function bodies too, so the only remaining dodge
        # is a dynamic one.
        self.assertNotIn("importlib", imported)
        self.assertNotIn("__import__", source)
        # The module may NAME the main modules -- its docstring documents what must stay out, and
        # that documentation is the point. What it may not do is pull anything from them.

    def test_no_main_prompt_constant_appears_in_the_workflow_instructions(self):
        # Assert on the constants themselves, per §8.1 -- not on a paraphrase, which would pass
        # while a copied block sat in the file.
        workflow_text = "\n".join(
            value for value in vars(workflow_instructions).values()
            if isinstance(value, str)
        )
        main_constants = {
            "SYSTEM_PROMPT_TEXT": instructions.SYSTEM_PROMPT_TEXT,
            "INVENTORY_PREAMBLE": instructions.INVENTORY_PREAMBLE,
            "PERSONA_INVENTORY_PREAMBLE": instructions.PERSONA_INVENTORY_PREAMBLE,
            "SCHEMA_OVERVIEW": instructions.SCHEMA_OVERVIEW,
            "REPORTING_RULES": instructions.REPORTING_RULES,
            "PERSONA_RULES": instructions.PERSONA_RULES,
            "WORD_CLOUD_RULES": instructions.WORD_CLOUD_RULES,
            "NESTED_RESULT_RULES": instructions.NESTED_RESULT_RULES,
            "PG_DIALECT_RULES": instructions.PG_DIALECT_RULES,
            "PROGRESS_MEMORY_RULES": instructions.PROGRESS_MEMORY_RULES,
            "STEP_BUDGET_NOTICE": instructions.STEP_BUDGET_NOTICE,
            "NL2SQL_TOOL_DESCRIPTION": prompts.NL2SQL_TOOL_DESCRIPTION,
            "RUN_SURVEY_STATS_DESCRIPTION": prompts.RUN_SURVEY_STATS_DESCRIPTION,
            "GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION":
                prompts.GET_SURVEY_ANALYSIS_PACKET_DESCRIPTION,
            "GENERATE_WORD_CLOUD_DESCRIPTION": prompts.GENERATE_WORD_CLOUD_DESCRIPTION,
        }
        for name, constant in main_constants.items():
            with self.subTest(constant=name):
                self.assertNotIn(constant, workflow_text)
                # Substring containment catches a verbatim copy; a shared opening paragraph
                # would not trip it, so check a long leading slice too.
                head = " ".join(constant.split())[:120]
                self.assertNotIn(head, " ".join(workflow_text.split()))

    def test_workflow_prompt_states_every_mandated_clause(self):
        # §8.1 enumerates what this module must say explicitly. Each clause below exists because
        # omitting it produced a specific wrong output, so they are asserted individually.
        prompt = workflow_instructions.WORKFLOW_SYSTEM_PROMPT
        self.assertIn("Only the survey, attributes and KPIs named in the seed", prompt)
        self.assertIn("cannot ask the caller anything", prompt)
        self.assertIn("ONLY the tools bound to this run", prompt)
        self.assertIn("NEVER compute a statistic yourself", prompt)
        self.assertIn("pseudo-R-squared", prompt)
        self.assertIn("DESCRIPTIVE", prompt)
        for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
            self.assertIn(section, prompt)
        # It must not drift into the main agent's analyst-facing voice.
        self.assertIn("no clickable", prompt)
        self.assertNotIn("{{", prompt)

    def test_seed_template_carries_only_the_request_and_no_main_context(self):
        template = workflow_instructions.WORKFLOW_SEED_TEMPLATE
        for field in ("survey_id", "attributes", "kpis", "screeners", "k", "normalization",
                      "random_state", "requested_metrics", "persona_sections", "stage_order"):
            self.assertIn("{" + field + "}", template)
        # The seed is the whole context the workflow gets, so no main-side channel may have a
        # slot in it. Asserted on the field markers that would carry one -- the drill's seed
        # passes USER QUESTION and the SQL that produced its result, and neither belongs here,
        # because this workflow is triggered intentionally with a resolved request rather than
        # reactively from something the main agent was already doing.
        for absent in ("USER QUESTION", "ALREADY GATHERED", "SQL THAT PRODUCED",
                       "PRE-FETCHED INVENTORY", "{messages}", "{thread", "{user"):
            self.assertNotIn(absent, template)

    def test_stage_failure_diagnostic_names_the_stage_and_keeps_artifacts(self):
        diagnostic = workflow_instructions.workflow_stage_failure(
            "D (kmeans, K=4)", "n_init must be >= 1"
        )
        self.assertIn("stage D (kmeans, K=4) failed", diagnostic)
        self.assertIn("n_init must be >= 1", diagnostic)
        self.assertIn("preserved for inspection", diagnostic)
        for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
            self.assertIn(section, diagnostic)


class IsolatedWorkflowGraphTests(unittest.TestCase):
    """The harness itself: fixed stage order, deterministic seed, artifact lifecycle."""

    def setUp(self):
        output_store.discard_all()

    def tearDown(self):
        output_store.discard_all()

    def test_stage_order_matches_the_published_contract(self):
        # The emitted pipeline string is part of the output contract (§17), so the graph order and
        # the string cannot be allowed to drift apart.
        self.assertEqual(workflow.PIPELINE_CONTRACT,
                         "A->B->C/E->G->D->F->I->J->K->L->M/N")
        stages = [
            workflow.label_of(node).split(" ")[0]
            for node in workflow._STAGE_SEQUENCE
            if node.startswith("stage_")
        ]
        self.assertEqual("->".join(stages), workflow.PIPELINE_CONTRACT)
        # The three model steps sit at their specified points, not bolted on at the end.
        sequence = list(workflow._STAGE_SEQUENCE)
        self.assertLess(sequence.index("step_encoding"), sequence.index("stage_b"))
        self.assertLess(sequence.index("step_sections"), sequence.index("stage_d"))
        self.assertEqual(sequence[-1], "stage_mn")

    def test_workflow_module_does_not_import_the_agent_or_its_instructions(self):
        # Importing funda_agent_exp here would be circular (the agent imports the main-facing tool
        # from this module) AND would transitively pull in both main instruction modules, so the
        # model name and DB access are passed in as arguments instead.
        tree = ast.parse(pathlib.Path(workflow.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("funda_agent_exp", "agent_instructions", "tool_prompts"):
            self.assertNotIn(banned, imported)
        # It may import its own instruction module and the result store, and must.
        self.assertIn("isolated_workflow_instructions", imported)
        self.assertIn("output_store", imported)
        # The entry point takes the model name rather than reaching for it.
        self.assertIn(
            "model_name",
            workflow.run_attribute_kpi_analysis.__code__.co_varnames,
        )

    def test_runtime_dependencies_arrive_through_config_not_imports(self):
        # The connection factory and model name are handed in, because this module imports nothing
        # from the agent. They travel in `configurable` rather than in PipelineState: a per-run
        # callable in state would be checkpointed if a checkpointer were ever attached, and module
        # state would not survive two concurrent runs.
        seen = {}

        def body(_state, config):
            seen["connect"] = workflow._runtime(config, "connect")
            seen["model_name"] = workflow._runtime(config, "model_name")
            return {}

        sentinel = object()
        with _stub_pipeline(stage_a=body):
            workflow.run_attribute_kpi_analysis(
                survey_id="s", attributes=[{"question_id": "a"}],
                kpis=[{"question_id": "k"}],
                connect=lambda: sentinel, model_name="gpt-test",
            )
        self.assertIs(seen["connect"](), sentinel)
        self.assertEqual(seen["model_name"], "gpt-test")

    def test_a_missing_runtime_dependency_fails_by_name(self):
        # A stage that needs the database and did not get one must say so, not fail obscurely
        # somewhere deeper. It surfaces through the normal stage-named failure path.
        def needs_db(_state, config):
            workflow._runtime(config, "connect")
            return {}

        with _stub_pipeline(stage_a=needs_db):
            run = workflow.run_attribute_kpi_analysis(
                survey_id="s", attributes=[{"question_id": "a"}],
                kpis=[{"question_id": "k"}],
            )
        self.assertIn("stage A (extract survey responses) failed", run.text)
        self.assertIn("without 'connect'", run.text)

    def test_the_isolated_model_defaults_to_the_main_tier_and_is_reused(self):
        # §9.1: same model as the main agent by default, never a cheaper tier, and the same client
        # object when the names match so the common case adds no second connection pool.
        self.assertEqual(agent.ISOLATED_WORKFLOW_MODEL, agent.MODEL_NAME)
        self.assertIs(agent._isolated_workflow_llm(), agent.llm)
        self.assertIs(agent._isolated_workflow_llm(), agent._isolated_workflow_llm())

    def test_the_agent_supplies_the_connection_factory(self):
        captured = {}
        real = workflow.run_attribute_kpi_analysis

        def spy(**kwargs):
            captured.update(kwargs)
            return real(**kwargs)

        with patch.object(agent, "_scope_published", return_value=True), \
             patch.object(agent, "_authorize_question_ids", return_value=([], [])), \
             patch.object(agent, "run_attribute_kpi_analysis", side_effect=spy):
            agent.attribute_kpi_analysis.invoke({
                "survey_id": "e14528a9-01ac-4dff-844e-0914dbdb3759",
                "attributes": [{"question_id": "8397f6e4-7d8d-44b4-8623-3a379df0b38c",
                                "role": "attribute"}],
                "kpis": [{"question_id": "adf03347-4cc9-4e2b-82fb-ef3bc0985174", "role": "kpi"}],
            })
        self.assertTrue(callable(captured["connect"]))
        self.assertEqual(captured["model_name"], agent.ISOLATED_WORKFLOW_MODEL)
        self.assertIs(captured["model"], agent._isolated_workflow_llm())

    def test_graph_has_no_checkpointer_and_no_cycle(self):
        # A checkpointer would give the workflow durable state outliving one invocation, which is
        # the isolation the design forbids.
        self.assertIsNone(getattr(workflow.PIPELINE_GRAPH, "checkpointer", None))

    def test_seed_is_byte_identical_across_runs_and_carries_no_main_context(self):
        kwargs = {
            "survey_id": "e14528a9-01ac-4dff-844e-0914dbdb3759",
            "attributes": [{"question_id": "b", "role": "attribute"},
                           {"question_id": "a", "role": "attribute"}],
            "kpis": [{"role": "kpi", "question_id": "k"}],
            "random_state": 20260819,
        }
        first = workflow.build_seed(**kwargs).content
        second = workflow.build_seed(**kwargs).content
        self.assertEqual(first, second)
        # Caller order is preserved (it is the analyst's ordering) while keys are sorted, so the
        # seed is reproducible without silently reordering the variables.
        self.assertLess(first.index('"question_id": "b"'), first.index('"question_id": "a"'))
        self.assertIn("20260819", first)
        # None of the main agent's context has a path into the seed.
        self.assertNotIn(instructions.SYSTEM_PROMPT_TEXT, first)
        self.assertNotIn(instructions.INVENTORY_PREAMBLE, first)
        self.assertNotIn(" ".join(instructions.INVENTORY_PREAMBLE.split())[:80],
                         " ".join(first.split()))

    def test_stub_run_returns_the_three_sections_and_discards_its_artifacts(self):
        with _stub_pipeline():
            run = workflow.run_attribute_kpi_analysis(
                survey_id="s", attributes=[{"question_id": "a"}], kpis=[{"question_id": "k"}],
            )
        for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
            self.assertIn(section, run.text)
        self.assertEqual(run.failed_stage, "")
        # Not "nothing": the report names the work the placeholder stages have not done, rather
        # than presenting a partial run as a complete analysis.
        self.assertIn("stages not yet implemented", run.still_missing)
        # Discarded on success: the report holds what anyone needs and the rows are dead weight.
        self.assertEqual(run.artifacts, {})
        self.assertEqual(output_store.store_stats()["stored_results"], 0)

    def test_a_failing_stage_is_named_and_every_earlier_artifact_stays_loadable(self):
        # §20 Step 2: "an exception in any stage returns a stage-named diagnostic with every
        # artifact still loadable via load_rows".
        for failing in ("stage_a", "stage_d", "stage_mn"):
            with self.subTest(stage=failing):
                output_store.discard_all()

                def boom(_state, _config=None):
                    raise ValueError("synthetic stage failure")

                with _stub_pipeline(**{failing: boom}):
                    run = workflow.run_attribute_kpi_analysis(
                        survey_id="s", attributes=[{"question_id": "a"}],
                        kpis=[{"question_id": "k"}],
                    )

                label = workflow.label_of(failing)
                self.assertEqual(run.failed_stage, label)
                self.assertIn(f"stage {label} failed", run.text)
                self.assertIn("synthetic stage failure", run.text)
                for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
                    self.assertIn(section, run.text)
                # Preserved, not discarded -- a failed run is when the artifacts matter most.
                expected = list(workflow._STAGE_SEQUENCE).index(failing)
                self.assertEqual(len(run.artifacts), expected)
                for node, result_id in run.artifacts.items():
                    self.assertIsNotNone(output_store.load_rows(result_id), node)

    def test_stages_after_a_failure_do_not_run(self):
        def boom(_state, _config=None):
            raise RuntimeError("stop here")

        with _stub_pipeline(stage_b=boom):
            run = workflow.run_attribute_kpi_analysis(
                survey_id="s", attributes=[{"question_id": "a"}],
                kpis=[{"question_id": "k"}],
            )
        # Only the two nodes before stage_b parked anything.
        self.assertEqual(set(run.artifacts), {"stage_a", "step_encoding"})

    def test_no_stage_output_is_serialized_into_the_workflow_messages(self):
        # §8.5: stages pass result ids, never data. The messages must therefore carry no row
        # payload -- only ids ever travel, and in the stub run not even those.
        with _stub_pipeline():
            run = workflow.run_attribute_kpi_analysis(
                survey_id="s", attributes=[{"question_id": "a"}], kpis=[{"question_id": "k"}],
            )
        self.assertNotIn('"status": "stub"', run.text)


class IsolatedWorkflowIntegrationTests(unittest.TestCase):
    """Step 2's headline acceptance: the main thread sees one tool observation, nothing more."""

    SURVEY = "e14528a9-01ac-4dff-844e-0914dbdb3759"
    QID_A = "8397f6e4-7d8d-44b4-8623-3a379df0b38c"
    QID_K = "adf03347-4cc9-4e2b-82fb-ef3bc0985174"

    def setUp(self):
        output_store.discard_all()

    def tearDown(self):
        output_store.discard_all()

    def _authorized(self):
        """Patch the scope guards so the tool reaches the workflow."""
        return (
            patch.object(agent, "_scope_published", return_value=True),
            patch.object(agent, "_authorize_question_ids", return_value=([], [])),
        )

    def _call(self, stub=True, **overrides):
        """Invoke the real tool. `stub=False` when the caller already stubbed the stages."""
        args = {
            "survey_id": self.SURVEY,
            "attributes": [{"question_id": self.QID_A, "role": "attribute"}],
            "kpis": [{"question_id": self.QID_K, "role": "kpi"}],
        }
        args.update(overrides)
        scope, authorize = self._authorized()
        with scope, authorize:
            # Stubbed stages on purpose: these tests are about the MAIN-side wiring -- envelope,
            # message count, state updates, tool binding -- not about extraction, which has its
            # own tests. Stubbing also keeps them offline and fast.
            if not stub:
                return agent.attribute_kpi_analysis.invoke(args)
            with _stub_pipeline():
                return agent.attribute_kpi_analysis.invoke(args)

    def test_main_history_holds_exactly_four_messages(self):
        # Human / AI tool_call / ToolMessage / AI. The workflow's own seed, turns and tool
        # messages are absent -- from the main agent's side the event is one tool returning text.
        envelope = self._call()
        call = {"name": "attribute_kpi_analysis", "args": {}, "id": "call-1"}
        state = {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "number_of_steps": 1,
            "last_sql_observations": {},
            "zero_row_streak": 0,
        }
        with patch.dict(agent.tools_by_name,
                        {"attribute_kpi_analysis": _FixedTool("attribute_kpi_analysis", envelope)},
                        clear=True):
            update = agent.call_tool(state)

        main_history = [
            HumanMessage(content="which personas are in this survey?"),
            AIMessage(content="", tool_calls=[call]),
            *update["messages"],
            AIMessage(content="final answer"),
        ]
        self.assertEqual(len(main_history), 4)
        self.assertIsInstance(main_history[0], HumanMessage)
        self.assertTrue(getattr(main_history[1], "tool_calls", None))
        self.assertIsInstance(main_history[2], ToolMessage)
        self.assertIsInstance(main_history[3], AIMessage)
        # The envelope is unwrapped: the raw prefix never enters the conversation.
        self.assertNotIn(agent._ANALYSIS_TOOL_RESULT_PREFIX, main_history[2].content)
        for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
            self.assertIn(section, main_history[2].content)

    def test_no_main_prompt_or_tool_description_reaches_the_workflow(self):
        # §8.1's second audit, asserted on the constants. The workflow's rendered messages are
        # built from the seed alone, so a main-side string can only appear if something imported
        # it -- which is exactly what this is here to catch.
        seed = workflow.build_seed(
            survey_id=self.SURVEY,
            attributes=[{"question_id": self.QID_A, "role": "attribute"}],
            kpis=[{"question_id": self.QID_K, "role": "kpi"}],
        )
        rendered = "\n".join([
            workflow_instructions.WORKFLOW_SYSTEM_PROMPT,
            str(seed.content),
            self._call(),
        ])
        for name, constant in (
            ("SYSTEM_PROMPT_TEXT", instructions.SYSTEM_PROMPT_TEXT),
            ("INVENTORY_PREAMBLE", instructions.INVENTORY_PREAMBLE),
            ("PERSONA_INVENTORY_PREAMBLE", instructions.PERSONA_INVENTORY_PREAMBLE),
            ("SCHEMA_OVERVIEW", instructions.SCHEMA_OVERVIEW),
            ("REPORTING_RULES", instructions.REPORTING_RULES),
            ("STEP_BUDGET_NOTICE", instructions.STEP_BUDGET_NOTICE),
            ("NL2SQL_TOOL_DESCRIPTION", prompts.NL2SQL_TOOL_DESCRIPTION),
            ("RUN_SURVEY_STATS_DESCRIPTION", prompts.RUN_SURVEY_STATS_DESCRIPTION),
            ("ATTRIBUTE_KPI_ANALYSIS_DESCRIPTION",
             prompts.ATTRIBUTE_KPI_ANALYSIS_DESCRIPTION),
        ):
            with self.subTest(constant=name):
                self.assertNotIn(constant, rendered)
                head = " ".join(constant.split())[:120]
                self.assertNotIn(head, " ".join(rendered.split()))

    def test_the_persona_profile_is_lifted_into_durable_state(self):
        # §11.7c: the ToolMessage carrying the profile is dropped after HISTORY_MAX_ROUNDS rounds,
        # so it has to land in state or a later question can only be answered by re-clustering.
        envelope = self._call()
        payload = json.loads(envelope.removeprefix(agent._ANALYSIS_TOOL_RESULT_PREFIX))
        self.assertIn("persona_profiles", payload)

        call = {"name": "attribute_kpi_analysis", "args": {}, "id": "c"}
        state = {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "number_of_steps": 1, "last_sql_observations": {}, "zero_row_streak": 0,
        }
        with patch.dict(agent.tools_by_name,
                        {"attribute_kpi_analysis": _FixedTool("attribute_kpi_analysis", envelope)},
                        clear=True):
            update = agent.call_tool(state)
        if payload["persona_profiles"]:
            self.assertIn("persona_profiles", update)
            self.assertEqual(update["persona_profiles"], payload["persona_profiles"])
        # And the profile is not in the message the model reads -- only the report text is.
        self.assertNotIn("persona_profiles", update["messages"][0].content)

    def test_parked_ids_is_empty_after_a_successful_run(self):
        envelope = self._call()
        payload = json.loads(envelope.removeprefix(agent._ANALYSIS_TOOL_RESULT_PREFIX))
        self.assertEqual(payload["parked_ids"], [])
        self.assertIn("stages not yet implemented", payload["still_missing"])
        self.assertEqual(output_store.store_stats()["stored_results"], 0)

        call = {"name": "attribute_kpi_analysis", "args": {}, "id": "c"}
        state = {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "number_of_steps": 1, "last_sql_observations": {}, "zero_row_streak": 0,
        }
        with patch.dict(agent.tools_by_name,
                        {"attribute_kpi_analysis": _FixedTool("attribute_kpi_analysis", envelope)},
                        clear=True):
            update = agent.call_tool(state)
        self.assertNotIn("parked_ids", update)

    def test_a_failed_stage_parks_ids_into_state_and_keeps_them_loadable(self):
        def boom(_state, _config=None):
            raise ValueError("synthetic failure")

        with _stub_pipeline(stage_d=boom):
            envelope = self._call(stub=False)

        payload = json.loads(envelope.removeprefix(agent._ANALYSIS_TOOL_RESULT_PREFIX))
        self.assertTrue(payload["parked_ids"])
        self.assertIn("stage D (k-means) failed", payload["text"])
        for result_id in payload["parked_ids"]:
            self.assertIsNotNone(output_store.load_rows(result_id))

        call = {"name": "attribute_kpi_analysis", "args": {}, "id": "c"}
        state = {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "number_of_steps": 1, "last_sql_observations": {}, "zero_row_streak": 0,
        }
        with patch.dict(agent.tools_by_name,
                        {"attribute_kpi_analysis": _FixedTool("attribute_kpi_analysis", envelope)},
                        clear=True):
            update = agent.call_tool(state)
        self.assertEqual(update["parked_ids"], set(payload["parked_ids"]))
        self.assertIn("stage D (k-means) failed", update["messages"][0].content)
        # The gap is carried durably, because the ToolMessage above is dropped after
        # HISTORY_MAX_ROUNDS rounds.
        self.assertIn("attribute-to-KPI analysis", update["analysis_missing"])

    @staticmethod
    def _bound_tool_names(bound):
        return [
            entry.get("function", {}).get("name")
            for entry in (getattr(bound, "kwargs", {}) or {}).get("tools") or []
        ]

    def test_result_reader_tools_are_bound_only_while_something_is_parked(self):
        # The default roster must NOT advertise the readers: with nothing parked they would be
        # three tool descriptions on every turn that can only answer "No stored result".
        default_names = self._bound_tool_names(agent.model)
        self.assertNotIn("describe_result", default_names)
        self.assertIn("attribute_kpi_analysis", default_names)

        reader_names = self._bound_tool_names(agent._result_reader_model())
        self.assertEqual(reader_names, default_names + ["describe_result", "query_result",
                                                        "slice_result"])
        # Built once and reused rather than rebound per turn.
        self.assertIs(agent._result_reader_model(), agent._result_reader_model())
        self.assertIsNot(agent._result_reader_model(), agent.model)

    def test_call_model_binds_the_readers_only_when_parked_ids_is_present(self):
        base = {
            "messages": [HumanMessage(content="q")],
            "number_of_steps": 1,
            "last_sql_observations": {},
            "zero_row_streak": 0,
        }
        for parked, expected in ((None, "default"), (set(), "default"), ({"abc123"}, "reader")):
            with self.subTest(parked=parked):
                used: list[str] = []
                state = dict(base)
                if parked is not None:
                    state["parked_ids"] = parked
                with patch.object(agent, "model", _RosterProbe("default", used)), \
                     patch.object(agent, "_result_reader_model",
                                  lambda: _RosterProbe("reader", used)):
                    agent.call_model(state, {})
                self.assertEqual(used, [expected])

    def test_parked_ids_does_not_change_the_final_tool_free_turn(self):
        # The step-budget turn binds NO tools at all, and a parked result must not reintroduce
        # any: on the last turn the agent has to answer from what it holds.
        used: list[str] = []
        state = {
            "messages": [HumanMessage(content="q")],
            "number_of_steps": agent.MAX_LLM_STEPS - 1,
            "last_sql_observations": {},
            "zero_row_streak": 0,
            "parked_ids": {"abc123"},
        }
        with patch.object(agent, "llm", _RosterProbe("bare", used)), \
             patch.object(agent, "model", _RosterProbe("default", used)), \
             patch.object(agent, "_result_reader_model",
                          lambda: _RosterProbe("reader", used)):
            agent.call_model(state, {})
        self.assertEqual(used, ["bare"])

    def test_tool_refuses_bad_input_before_reaching_the_workflow(self):
        scope, authorize = self._authorized()
        with scope, authorize:
            self.assertIn("must be a UUID", agent.attribute_kpi_analysis.invoke({
                "survey_id": "nope",
                "attributes": [{"question_id": self.QID_A, "role": "attribute"}],
                "kpis": [{"question_id": self.QID_K, "role": "kpi"}],
            }))
            self.assertIn("no attributes were supplied", agent.attribute_kpi_analysis.invoke({
                "survey_id": self.SURVEY, "attributes": [],
                "kpis": [{"question_id": self.QID_K, "role": "kpi"}],
            }))
            self.assertIn("no KPI was supplied", agent.attribute_kpi_analysis.invoke({
                "survey_id": self.SURVEY,
                "attributes": [{"question_id": self.QID_A, "role": "attribute"}], "kpis": [],
            }))
        # Out-of-scope questions are refused, and nothing is parked by a refused call.
        with patch.object(agent, "_scope_published", return_value=True), \
             patch.object(agent, "_authorize_question_ids", return_value=([], [self.QID_K])):
            refused = agent.attribute_kpi_analysis.invoke({
                "survey_id": self.SURVEY,
                "attributes": [{"question_id": self.QID_A, "role": "attribute"}],
                "kpis": [{"question_id": self.QID_K, "role": "kpi"}],
            })
        self.assertIn("outside the initial survey's client boundary", refused)
        self.assertEqual(output_store.store_stats()["stored_results"], 0)

    def test_tool_description_keeps_methodology_out_of_the_main_agents_turn(self):
        description = prompts.ATTRIBUTE_KPI_ANALYSIS_DESCRIPTION
        self.assertIn("YOUR JOB IS SELECTION", description)
        self.assertIn("NEVER from memory", description)
        self.assertIn("EVERYTHING ELSE IS THIS TOOL'S JOB", description)
        self.assertIn('role="screener"', description)
        self.assertIn("categorical KPI is as valid", description)
        # LangChain strips the docstring when it becomes the tool description.
        self.assertEqual(agent.attribute_kpi_analysis.description, description.strip())


class _stub_pipeline:
    """Run the workflow with stubbed stages, optionally failing a chosen one.

    The graph selects each node's body from workflow._STAGE_BODIES, so that mapping -- not
    _stub_body -- is the seam for a test that wants a stage replaced. Patching it also rebuilds
    the graph, because the bodies are bound at build time.
    """

    def __init__(self, **overrides):
        self._overrides = overrides
        self._patches = []

    def __enter__(self):
        self._patches = [patch.object(workflow, "_STAGE_BODIES", dict(self._overrides))]
        for item in self._patches:
            item.start()
        graph = patch.object(workflow, "PIPELINE_GRAPH", workflow.build_pipeline_graph())
        graph.start()
        self._patches.append(graph)
        return self

    def __exit__(self, *_args):
        for item in reversed(self._patches):
            item.stop()
        return False


_PROFILE_FIXTURE = {
    "joinability": {"joinability": "joined", "note": "every analysed enrollment answered"},
    "personas": [
        {
            "persona": "all_high", "clusters": [1], "n": 355, "share_of_respondents": 0.2958,
            "region": {"mass": 0.2658, "bounded_attributes": ["Aroma"], "overlap": {1: 0.8254}},
            "centroid": {"normalized": {"Aroma": 0.97}, "raw": {"Aroma": "8.8 of 9"}},
            "kpi_summary": [{
                "kpi": "Frequency of Purchase", "mean_normalized": 0.71, "n": 355,
                "alignment": [{"cluster": 1, "alignment": "positive",
                               "in_words": "sits toward 'Once a day'", "withheld": None}],
            }],
            "joinability": "joined",
            "demographics": [{"question": "Age", "n": 355, "distribution": {"75+": 100},
                              "shares": {"75+": 0.28}, "base_shares": {"75+": 0.30}}],
            "distinguishing": [],
        },
        {
            "persona": "all_low", "clusters": [2, 3], "n": 390, "share_of_respondents": 0.325,
            "region": {"mass": 0.2258, "bounded_attributes": ["Aroma"], "overlap": {2: 0.86}},
            "centroid": {"normalized": {"Aroma": 0.6}, "raw": {"Aroma": "5.8 of 9"}},
            "kpi_summary": [{
                "kpi": "Frequency of Purchase", "mean_normalized": 0.53, "n": 390,
                "alignment": [{"cluster": 2, "alignment": "negative",
                               "in_words": "sits toward 'Less often'",
                               "withheld": "persona 'all_low' holds more than one cluster"}],
            }],
            "joinability": "joined",
            "demographics": None,
            "distinguishing": [{"question": "Age", "label": "75+", "share": 0.44,
                                "base": 0.30, "delta": 0.14}],
        },
    ],
}


class PersonaRoutingTests(unittest.TestCase):
    """§11.9: one word, three routes, with the free one checked first."""

    def test_a_survey_demographic_request_stays_on_the_cheap_route(self):
        for prompt in (
            "what are this survey's demographics?",
            "give me a demographic breakdown",
            "demographics of this survey please",
        ):
            with self.subTest(prompt=prompt):
                # Must NOT trigger clustering: that would be a large cost for an answer nobody
                # asked for, and the one-query inventory already covers it.
                self.assertFalse(agent.is_persona_cluster_request(prompt))
        # The existing inventory predicate is unchanged by this split. Note: it does not match the
        # possessive "this survey's demographics" -- a pre-existing gap in _PERSONA_REQUEST_RE,
        # left alone here because widening it is unrelated to the persona-cluster routing.
        self.assertTrue(agent.is_persona_request("give me a demographic breakdown"))
        self.assertTrue(agent.is_persona_request("demographics of this survey please"))

    def test_a_persona_cluster_request_routes_to_the_workflow(self):
        for prompt in (
            "what are the personas of this survey?",
            "which persona is a better fit for this product?",
            "give more details on this particular persona",
            "segment the respondents for me",
            "show me respondent clusters",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(agent.is_persona_cluster_request(prompt))

    def test_an_unrelated_request_triggers_neither_route(self):
        for prompt in ("what products were tested?", "run stats on flavor liking"):
            with self.subTest(prompt=prompt):
                self.assertFalse(agent.is_persona_cluster_request(prompt))

    def test_a_demographic_request_that_also_names_personas_does_cluster(self):
        prompt = "give me the demographic breakdown of each persona"
        self.assertTrue(agent.is_persona_cluster_request(prompt))

    def test_the_cluster_directive_names_the_tool_and_forbids_the_alternatives(self):
        # Measured: withholding the demographic inventory alone did NOT route the request -- the
        # agent wrote four nl2sql_tool queries instead and built a persona from demographic
        # marginals. The directive has to close those paths by name.
        directive = instructions.PERSONA_CLUSTER_DIRECTIVE
        self.assertIn("attribute_kpi_analysis", directive)
        self.assertIn("ONLY source of persona structure", directive)
        self.assertIn("Do not answer this question with nl2sql_tool", directive)
        self.assertIn("do not answer it from the inventory", directive)
        # And it names the three fabrications actually observed in live runs.
        self.assertIn("NOT a demographic majority", directive)
        self.assertIn("NOT the most common answer", directive)
        self.assertIn("NOT a theme counted out of open-text", directive)
        # Methodology stays the tool's job, per §15.
        self.assertIn("leave those arguments unset", directive)
        # A failure must not become a demographic summary.
        self.assertIn("Do not substitute a demographic summary", directive)
        # It is user-turn text, not system-prompt text, so the prompt hash is untouched.
        self.assertNotIn(directive, instructions.SYSTEM_PROMPT_TEXT)

    def test_a_follow_up_naming_an_existing_persona_hits_durable_state(self):
        # The point of the durable path: this must not re-run the pipeline to learn what it has.
        matched = agent.is_persona_profile_request(
            "tell me more about all_low", _PROFILE_FIXTURE)
        self.assertEqual(matched, ["all_low"])
        # Underscores are not what a person types.
        self.assertEqual(
            agent.is_persona_profile_request("what about all low?", _PROFILE_FIXTURE),
            ["all_low"],
        )
        # A bare roster question is also answerable from state.
        self.assertEqual(
            set(agent.is_persona_profile_request("what are the personas?", _PROFILE_FIXTURE)),
            {"all_high", "all_low"},
        )

    def test_no_stored_profile_means_no_durable_hit(self):
        self.assertEqual(agent.is_persona_profile_request("what are the personas?", None), [])
        self.assertEqual(agent.is_persona_profile_request("", _PROFILE_FIXTURE), [])
        # A persona name that was never computed does not match.
        self.assertEqual(
            agent.is_persona_profile_request("tell me about spicy_lovers", _PROFILE_FIXTURE), [])

    def test_the_reinjected_block_carries_what_a_later_question_needs(self):
        block = agent.persona_profile_block(_PROFILE_FIXTURE, ["all_low"])
        self.assertIn("do not re-run it", block)
        self.assertIn("all_low: n=390", block)
        self.assertIn("5.8 of 9", block)          # raw units, what a human reads
        self.assertIn("region mass 0.2258", block)
        self.assertIn("sits toward 'Less often'", block)
        self.assertIn("WITHHELD", block)          # the many-to-one caveat travels with it
        self.assertIn("distinguishing: 75+ 0.44 vs base 0.3", block)
        # all_high was not asked about, so it is not attached.
        self.assertNotIn("all_high", block)

    def test_a_disjoint_persona_says_so_rather_than_omitting_demographics(self):
        block = agent.persona_profile_block(_PROFILE_FIXTURE, ["all_low"])
        self.assertIn("demographics: none available", block)
        self.assertIn("no demographic claim can be made", block)

    def test_the_profile_survives_history_trimming_because_it_is_state(self):
        # _trim_history drops whole rounds, so the ToolMessage carrying the profile is gone after
        # HISTORY_MAX_ROUNDS. State is not a message, so it is untouched.
        rounds = []
        for index in range(agent.HISTORY_MAX_ROUNDS + 3):
            rounds.append(HumanMessage(content=f"question {index}"))
            rounds.append(AIMessage(content=f"answer {index}"))
        kept = agent._trim_history(rounds)
        self.assertLess(len(kept), len(rounds))
        self.assertNotIn("question 1", [str(m.content) for m in kept])
        # And the field is declared on the state, which trimming never touches.
        self.assertIn("persona_profiles", agent.AgentState.__annotations__)


class _FakeConn:
    """A connection returning prepared mappings, so value-path rules test without a database."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement, _params=None):
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: self._rows))


def _raw_row(**overrides):
    """One raw stage-A row with every value field present and empty."""
    row = {
        "enrollment_id": "e1", "product_id": "p1", "product_name": "Product A",
        "question_id": "q1", "question_type": "vertical-rating",
        "enrollment_status": "completed", "component_id": "c1", "component_label": None,
        "value_label": None, "value_code": None, "option_answer": None,
        "option_label": None, "option_rank": None, "text_value": None,
    }
    row.update(overrides)
    return row


_DB_AVAILABLE = None


def _db_available() -> bool:
    """True when the local sample database is reachable. Checked once."""
    global _DB_AVAILABLE
    if _DB_AVAILABLE is None:
        try:
            with agent._sql_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            _DB_AVAILABLE = True
        except Exception:  # noqa: BLE001 - absence is a skip, not a failure
            _DB_AVAILABLE = False
    return _DB_AVAILABLE


class StageAExtractionTests(unittest.TestCase):
    """Stage A: the §4.1 value paths, validation, and grain detection."""

    def _extract(self, rows, variables=None):
        return workflow.extract_dataset(
            connect=lambda: _FakeConn(rows),
            survey_id="s",
            variables=variables or [{"question_id": "q1", "role": "attribute"}],
        )

    def test_multiple_choice_value_comes_from_the_joined_label_not_the_payload(self):
        # The regression this pipeline exists for: multiple-choice carries optionAnswer on only
        # 95% of rows, so reading the payload silently loses 5% of valid answers. The joined
        # question_option.label is always there.
        report = self._extract([
            _raw_row(question_type="multiple-choice", value_label="Once a week",
                     option_answer=None),
        ])
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["kind"], "category")
        self.assertEqual(report["rows"][0]["value"], "Once a week")
        self.assertEqual(report["invalid_values"], [])

    def test_matrix_keys_the_row_as_component_and_the_column_as_value(self):
        # Keyed on question_option_id alone a matrix reports its scale points as attributes.
        report = self._extract([
            _raw_row(question_type="matrix", component_id="row1", component_label="Is modern",
                     value_label="Agree", value_code=4),
        ])
        row = report["rows"][0]
        self.assertEqual(row["component_label"], "Is modern")
        self.assertEqual(row["value"], 4.0)
        self.assertEqual(row["kind"], "numeric")

    def test_corrupt_analytical_value_is_reported_with_its_raw_value_never_clamped(self):
        report = self._extract([
            _raw_row(question_type="matrix", component_label="row", value_label="7",
                     value_code=7777),
        ])
        self.assertEqual(report["rows"], [])
        self.assertEqual(len(report["invalid_values"]), 1)
        bad = report["invalid_values"][0]
        self.assertIn("7777", bad["reason"])
        self.assertIn("not clamped", bad["reason"])
        # Not turned into 7 either -- a clamped code becomes a plausible scale point and moves
        # a mean without saying so.
        self.assertNotIn(7.0, [r["value"] for r in report["rows"]])

    def test_unsupported_types_are_refused_by_name_never_skipped_silently(self):
        for question_type, fragment in (
            ("triangle-test", "no option row"),
            ("tcata", "only action and t_ms"),
            ("time-intensity-slider", "must be requested"),
        ):
            with self.subTest(question_type=question_type):
                report = self._extract([_raw_row(question_type=question_type)])
                self.assertEqual(report["rows"], [])
                self.assertEqual(len(report["unsupported"]), 1)
                self.assertEqual(report["unsupported"][0]["question_type"], question_type)
                self.assertIn(fragment, report["unsupported"][0]["reason"])

    def test_a_requested_variable_with_no_rows_is_reported_missing(self):
        report = self._extract(
            [_raw_row(option_answer="7")],
            variables=[{"question_id": "q1", "role": "attribute"},
                       {"question_id": "absent", "role": "kpi"}],
        )
        self.assertEqual(report["missing_variables"], ["absent"])

    def test_duplicate_observations_are_reported_and_never_averaged(self):
        report = self._extract([
            _raw_row(option_answer="7"),
            _raw_row(option_answer="9"),
        ])
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(len(report["duplicate_observations"]), 1)
        # The first observation stands; the second is reported, not blended into a 8.0.
        self.assertEqual(report["rows"][0]["value"], 7.0)

    def test_a_duplicate_fails_the_stage_rather_than_proceeding(self):
        state = {"decisions": {"request": {
            "survey_id": "s", "variables": [{"question_id": "q1", "role": "attribute"}]}},
            "artifacts": {}, "log": []}
        config = {"configurable": {"connect": lambda: _FakeConn([
            _raw_row(option_answer="7"), _raw_row(option_answer="9"),
        ])}}
        with self.assertRaises(RuntimeError) as caught:
            workflow.stage_a_body(state, config)
        self.assertIn("duplicate observation", str(caught.exception))
        self.assertIn("averaging them would hide that", str(caught.exception))

    def test_zero_usable_rows_names_the_variable_and_the_filter(self):
        state = {"decisions": {"request": {
            "survey_id": "s", "variables": [{"question_id": "q1", "role": "attribute"}]}},
            "artifacts": {}, "log": []}
        config = {"configurable": {"connect": lambda: _FakeConn([])}}
        with self.assertRaises(RuntimeError) as caught:
            workflow.stage_a_body(state, config)
        self.assertIn("0 usable rows", str(caught.exception))
        self.assertIn("q1", str(caught.exception))

    def test_grain_regime_is_detected_not_assumed(self):
        single = self._extract([_raw_row(option_answer="7")])
        self.assertEqual(single["grain"]["regime"], "single_product")
        multi = self._extract([
            _raw_row(option_answer="7", product_id="p1"),
            _raw_row(option_answer="8", product_id="p2"),
        ])
        self.assertEqual(multi["grain"]["regime"], "multi_product")
        self.assertEqual(multi["grain"]["max_products_per_enrollment"], 2)

    def test_enrollment_status_is_exposed_rather_than_filtered(self):
        report = self._extract([
            _raw_row(option_answer="7", enrollment_status="active"),
        ])
        self.assertEqual(report["enrollment_statuses"], ["active"])
        self.assertEqual(len(report["rows"]), 1)

    def test_skipped_answers_are_excluded_in_sql_not_in_python(self):
        # The filter belongs in the statement so a skipped answer never becomes a row at all.
        self.assertIn('NOT COALESCE(a."isSkipped", false)', workflow._STAGE_A_SQL)
        # And the component key is the matrix-correct one.
        self.assertIn("COALESCE(aqo.matrix_row_option_id, aqo.question_option_id)",
                      workflow._STAGE_A_SQL)


class _CompliantWorkflowModel:
    """A fake model answering every workflow turn with a contract-valid payload.

    For tests whose subject is NOT a model judgment -- grain reporting, normalization, the
    main-side wiring, the artifact lifecycle -- so they neither hit the network nor need a
    hand-written payload per fixture. It makes the cheapest valid choice each time, and it
    dispatches on the turn's own marker so a newly landed turn fails loudly here rather than
    silently receiving the wrong shape.
    """

    def __init__(self):
        self.calls = []

    def invoke(self, messages, **_kwargs):
        self.calls.append(messages)
        payload = str(messages[-1].content)
        if "VARIABLES:" in payload:
            return AIMessage(content=self._encodings(payload))
        if "DISTRIBUTION:" in payload:
            return AIMessage(content=self._sections(payload))
        if "STAGE OUTPUTS:" in payload:
            # A minimal compliant report: the three sections, in order, quoting no figure at all.
            # A report that states no number cannot state a wrong one, which is what keeps this
            # fake from silently passing a check the real turn has to satisfy.
            return AIMessage(content=(
                "RESULT: the stage outputs carry the figures for this run.\n"
                "DIAGNOSTICS: composed by the test fake; see the stage outputs.\n"
                "STILL MISSING: nothing"
            ))
        raise AssertionError(
            "the fake model received a turn it does not recognise; teach it that turn's shape "
            f"rather than letting the test pass by accident: {payload[:120]!r}"
        )

    @staticmethod
    def _encodings(payload):
        variables = json.loads(payload[payload.index("VARIABLES:") + len("VARIABLES:"):])
        return json.dumps({"decisions": [
            {
                "question_id": variable["question_id"],
                "encoding": "nominal" if (variable.get("labels") or []) else "numeric",
                "ordered_categories": [], "jar_ideal": None, "excluded_categories": [],
                "direction": "" if (variable.get("labels") or []) else "higher = more",
                "labels_read": (variable.get("labels") or ["configured range"])[:2],
                "rationale": "test fake",
            }
            for variable in variables
        ]})

    @staticmethod
    def _sections(payload):
        distribution = json.loads(
            payload[payload.index("DISTRIBUTION:") + len("DISTRIBUTION:"):])
        names = [entry["attribute"] for entry in distribution["attributes"]]
        # Level-based tercile bands, which is what a level-dominated matrix supports.
        return json.dumps({
            "k": 4,
            "k_rationale": "test fake: four level bands",
            "persona_sections": {
                "all_low": {name: [None, 1 / 3] for name in names},
                "middle": {name: [1 / 3, 2 / 3] for name in names},
                "all_high": {name: [2 / 3, None] for name in names},
            },
            "persona_rationales": {
                "all_low": "bottom third throughout",
                "middle": "middle band throughout",
                "all_high": "top third throughout",
            },
        })


class _ScriptedModel:
    """A model returning prepared texts in order, recording what it was asked."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def invoke(self, messages, **_kwargs):
        self.calls.append(messages)
        return AIMessage(content=self._responses[min(len(self.calls) - 1,
                                                    len(self._responses) - 1)])


# The four §20 Step 4 cases, with the label sets measured from the sample database.
_PURCHASE_FREQUENCY = {
    "question_id": "adf03347-4cc9-4e2b-82fb-ef3bc0985174",
    "role": "kpi", "question_type": "multiple-choice", "encoding": "auto",
    "labels": ["Once a day", "Once a week", "Once a month", "Once every two months",
               "Once every three months", "Once every six months",
               "Less often than six months", "Never"],
    "counts": {"Once a day": 14, "Once a week": 345, "Once a month": 597,
               "Once every two months": 104, "Once every three months": 55,
               "Once every six months": 40, "Less often than six months": 32, "Never": 13},
}
_GENDER = {
    "question_id": "08e899de-1e57-4dca-aeb5-3b1f4dc73ca0",
    "role": "screener", "question_type": "multiple-choice", "encoding": "auto",
    "labels": ["Female", "Male", "Prefer not to say", "Prefer to self-describe"],
}
_REVERSED_HEDONIC = {
    "question_id": "096cb7d9-31f8-4f6f-87bb-00782aaee141",
    "role": "attribute", "question_type": "multiple-choice", "encoding": "auto",
    "labels": ["Like Extremely", "Dislike Extremely"],
    "anchors": ["Like Extremely", "Dislike Extremely"],
}


def _decision(question_id, encoding, **extra):
    decision = {
        "question_id": question_id, "encoding": encoding, "ordered_categories": [],
        "jar_ideal": None, "excluded_categories": [], "direction": "",
        "labels_read": [], "rationale": "because",
    }
    decision.update(extra)
    return decision


def _payload(*decisions):
    return json.dumps({"decisions": list(decisions)})


class EncodingStepTests(unittest.TestCase):
    """The §9.2 encoding turn: what it is shown, and what its output must satisfy."""

    def test_the_prompt_withholds_the_stored_codes_it_must_not_use(self):
        # Structural, not hopeful: order and analytical_value are not in the prompt, so a decision
        # cannot rest on them. Ordinal ranks come from the sequence the turn states instead.
        prompt = workflow_instructions.ENCODING_STEP_TEMPLATE
        self.assertIn("You are NOT given the stored numeric codes", prompt)
        self.assertIn("AUTHORED SEQUENCE IS NOT DIRECTION", prompt)
        self.assertIn("DEFAULT TO NOMINAL", prompt)
        self.assertIn("DIRECTION IS MANDATORY", prompt)
        self.assertIn("labels_read", prompt)
        for banned in ("analytical_value", "question_option.order"):
            self.assertNotIn(banned, prompt)
        rendered = prompt.format(variables=json.dumps([_PURCHASE_FREQUENCY]))
        self.assertNotIn("analytical_value", rendered)

    def test_an_ordinal_kpi_records_its_sequence_direction_and_exclusion(self):
        sequence = ["Less often than six months", "Once every six months",
                    "Once every three months", "Once every two months", "Once a month",
                    "Once a week", "Once a day"]
        model = _ScriptedModel(_payload(_decision(
            _PURCHASE_FREQUENCY["question_id"], "ordinal",
            ordered_categories=sequence,
            excluded_categories=["Never"],
            direction="higher code = less often; the sequence runs least to most frequent",
            labels_read=["Once a day", "Never", "Less often than six months"],
        )))
        result = workflow.resolve_encodings(model, [_PURCHASE_FREQUENCY])
        decision = result["encodings"][_PURCHASE_FREQUENCY["question_id"]]
        self.assertEqual(decision["encoding"], "ordinal")
        self.assertEqual(decision["ordered_categories"], sequence)
        self.assertEqual(decision["excluded_categories"], ["Never"])
        self.assertIn("less often", decision["direction"])
        self.assertTrue(decision["labels_read"])
        self.assertEqual(result["attempts"][-1]["errors"], [])

    def test_a_dense_order_does_not_make_a_nominal_variable_ordinal(self):
        model = _ScriptedModel(_payload(_decision(
            _GENDER["question_id"], "nominal",
            labels_read=["Female", "Male", "Prefer not to say"],
        )))
        result = workflow.resolve_encodings(model, [_GENDER])
        decision = result["encodings"][_GENDER["question_id"]]
        self.assertEqual(decision["encoding"], "nominal")
        # A nominal variable must NOT carry a sequence -- one would assert an order that the
        # labels do not support.
        self.assertEqual(decision["ordered_categories"], [])

    def test_a_nominal_decision_carrying_a_sequence_is_rejected(self):
        bad = _payload(_decision(
            _GENDER["question_id"], "nominal",
            ordered_categories=["Female", "Male", "Prefer not to say",
                                "Prefer to self-describe"],
            labels_read=["Female"],
        ))
        errors = workflow.validate_encodings(json.loads(bad)["decisions"], [_GENDER])
        self.assertTrue(any("must be empty" in e for e in errors))

    def test_a_reversed_hedonic_keeps_the_direction_its_labels_imply(self):
        # The stored codes run Like Extremely=0 .. Dislike Extremely=8, so a code-order reading
        # would call ascending "more liking". The labels say the opposite.
        model = _ScriptedModel(_payload(_decision(
            _REVERSED_HEDONIC["question_id"], "ordinal",
            ordered_categories=["Dislike Extremely", "Like Extremely"],
            direction="higher = more liking; the sequence runs from Dislike to Like",
            labels_read=["Like Extremely", "Dislike Extremely"],
        )))
        result = workflow.resolve_encodings(model, [_REVERSED_HEDONIC])
        decision = result["encodings"][_REVERSED_HEDONIC["question_id"]]
        # The sequence is lowest-first in MEANING, which is the reverse of the stored order.
        self.assertEqual(decision["ordered_categories"][0], "Dislike Extremely")
        self.assertIn("more liking", decision["direction"])

    def test_a_missing_direction_is_a_hard_error(self):
        for encoding, extra in (
            ("ordinal", {"ordered_categories": ["Dislike Extremely", "Like Extremely"]}),
            ("numeric", {}),
        ):
            with self.subTest(encoding=encoding):
                errors = workflow.validate_encodings([_decision(
                    _REVERSED_HEDONIC["question_id"], encoding,
                    labels_read=["Like Extremely"], **extra,
                )], [_REVERSED_HEDONIC])
                self.assertTrue(any("direction is mandatory" in e for e in errors))

    def test_a_missing_labels_read_is_a_hard_error(self):
        errors = workflow.validate_encodings([_decision(
            _GENDER["question_id"], "nominal",
        )], [_GENDER])
        self.assertTrue(any("labels_read is empty" in e for e in errors))

    def test_an_incomplete_sequence_is_rejected_with_what_was_expected(self):
        errors = workflow.validate_encodings([_decision(
            _PURCHASE_FREQUENCY["question_id"], "ordinal",
            ordered_categories=["Once a day", "Once a week"],
            labels_read=["Once a day"],
            direction="higher = more often",
        )], [_PURCHASE_FREQUENCY])
        message = "\n".join(errors)
        self.assertIn("exactly once", message)
        self.assertIn("Expected these 8", message)

    def test_a_jar_needs_an_ideal_inside_its_sequence(self):
        jar = {
            "question_id": "jar1", "role": "attribute", "question_type": "multiple-choice",
            "encoding": "auto",
            "labels": ["Not sweet enough", "Just about right", "Much too sweet"],
        }
        errors = workflow.validate_encodings([_decision(
            "jar1", "jar",
            ordered_categories=jar["labels"], labels_read=jar["labels"],
            direction="middle is ideal",
        )], [jar])
        self.assertTrue(any("needs jar_ideal" in e for e in errors))
        clean = workflow.validate_encodings([_decision(
            "jar1", "jar", ordered_categories=jar["labels"], jar_ideal="Just about right",
            labels_read=jar["labels"], direction="middle is ideal, worse in both directions",
        )], [jar])
        self.assertEqual(clean, [])

    def test_excluding_a_label_the_variable_does_not_have_is_rejected(self):
        errors = workflow.validate_encodings([_decision(
            _GENDER["question_id"], "nominal",
            excluded_categories=["Nonexistent"], labels_read=["Female"],
        )], [_GENDER])
        self.assertTrue(any("labels this variable does not have" in e for e in errors))

    def test_a_missing_variable_is_rejected(self):
        errors = workflow.validate_encodings([], [_GENDER, _PURCHASE_FREQUENCY])
        self.assertTrue(any("no decision for" in e for e in errors))

    def test_one_retry_is_allowed_and_it_is_told_exactly_what_was_wrong(self):
        bad = _payload(_decision(_GENDER["question_id"], "nominal"))  # no labels_read
        good = _payload(_decision(_GENDER["question_id"], "nominal",
                                  labels_read=["Female", "Male"]))
        model = _ScriptedModel(bad, good)
        result = workflow.resolve_encodings(model, [_GENDER])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result["attempts"][0]["errors"] != [], True)
        self.assertEqual(result["attempts"][1]["errors"], [])
        # The retry carries the specific failure, not a generic instruction.
        retry_text = model.calls[1][-1].content
        self.assertIn("labels_read is empty", retry_text)

    def test_a_second_failure_raises_rather_than_looping(self):
        bad = _payload(_decision(_GENDER["question_id"], "nominal"))
        model = _ScriptedModel(bad, bad)
        with self.assertRaises(RuntimeError) as caught:
            workflow.resolve_encodings(model, [_GENDER])
        self.assertIn("after a retry", str(caught.exception))
        self.assertEqual(len(model.calls), 2)

    def test_non_json_and_fenced_json_are_handled(self):
        fenced = _ScriptedModel(
            "```json\n" + _payload(_decision(_GENDER["question_id"], "nominal",
                                             labels_read=["Female"])) + "\n```"
        )
        self.assertIn(_GENDER["question_id"],
                      workflow.resolve_encodings(fenced, [_GENDER])["encodings"])
        prose = _ScriptedModel("I think gender is nominal.", "still not json")
        with self.assertRaises(RuntimeError) as caught:
            workflow.resolve_encodings(prose, [_GENDER])
        self.assertIn("did not return JSON", str(caught.exception))

    def test_a_caller_supplied_encoding_wins_and_both_readings_are_recorded(self):
        supplied = {**_GENDER, "encoding": "ordinal",
                    "ordered_categories": ["Female", "Male", "Prefer not to say",
                                           "Prefer to self-describe"]}
        model = _ScriptedModel(_payload(_decision(
            supplied["question_id"], "nominal", labels_read=["Female"],
        )))
        decision = workflow.resolve_encodings(model, [supplied])["encodings"][
            supplied["question_id"]]
        self.assertEqual(decision["encoding"], "ordinal")
        self.assertEqual(decision["source"]["encoding"], "supplied")
        # The workflow's own reading is kept, so the override is visible rather than silent.
        self.assertEqual(decision["derived_encoding"], "nominal")
        self.assertEqual(decision["ordered_categories"], supplied["ordered_categories"])

    def test_a_numeric_jar_names_its_ideal_as_a_number_not_a_label(self):
        # Found by running the real model: the reference fixture's JAR attribute is a 1-5 slider
        # anchored "Not crispy/crunchy enough" to "Much too crispy/crunchy", with NO category
        # labels. Its ideal is a point on the range, so demanding a label was wrong.
        numeric_jar = {
            "question_id": "60652a5e-82c1-4493-b6cd-453d38099790",
            "role": "attribute", "question_type": "vertical-rating", "encoding": "auto",
            "labels": [],
            "anchors": ["Not crispy/crunchy enough", "Much too crispy/crunchy"],
            "configured_range": [1.0, 5.0],
        }
        model = _ScriptedModel(_payload(_decision(
            numeric_jar["question_id"], "jar",
            jar_ideal="3.0",
            direction="higher = more crispy; the ideal is the midpoint",
            labels_read=["Not crispy/crunchy enough", "Much too crispy/crunchy"],
        )))
        decision = workflow.resolve_encodings(model, [numeric_jar])["encodings"][
            numeric_jar["question_id"]]
        self.assertEqual(decision["encoding"], "jar")
        # Stored as a number: downstream this is arithmetic, not a label.
        self.assertEqual(decision["jar_ideal"], 3.0)
        self.assertEqual(decision["ordered_categories"], [])

    def test_a_numeric_jar_ideal_must_be_numeric_and_inside_the_range(self):
        numeric_jar = {
            "question_id": "j", "role": "attribute", "question_type": "vertical-rating",
            "encoding": "auto", "labels": [], "configured_range": [1.0, 5.0],
            "anchors": ["not enough", "much too much"],
        }
        errors = workflow.validate_encodings([_decision(
            "j", "jar", jar_ideal="Just about right", direction="d", labels_read=["not enough"],
        )], [numeric_jar])
        self.assertTrue(any("must be a NUMBER on its configured range" in e for e in errors))
        outside = workflow.validate_encodings([_decision(
            "j", "jar", jar_ideal=9, direction="d", labels_read=["not enough"],
        )], [numeric_jar])
        self.assertTrue(any("outside the configured range" in e for e in outside))

    def test_labels_read_may_name_anchors_when_a_variable_has_no_categories(self):
        # A numeric scale's evidence IS its anchors, so labels_read is checked against the label
        # list only when there is one.
        numeric = {
            "question_id": "n", "role": "attribute", "question_type": "vertical-rating",
            "encoding": "auto", "labels": [], "configured_range": [1.0, 9.0],
            "anchors": ["Dislike Extremely", "Like Extremely"],
        }
        errors = workflow.validate_encodings([_decision(
            "n", "numeric", direction="higher = more liking",
            labels_read=["Dislike Extremely", "Like Extremely"],
        )], [numeric])
        self.assertEqual(errors, [])

    def test_variables_are_resolved_concurrently_by_default(self):
        # Measured at 8 variables: 13.8s sequential (spiking to 35.5s) against 6.4s concurrent.
        # Each decision rests on its own variable's labels -- the prompt never asks the turn to
        # compare one with another -- so splitting the request changes nothing but the latency.
        model = _ScriptedModel(
            _payload(_decision(_GENDER["question_id"], "nominal", labels_read=["Female"])),
            _payload(_decision(_PURCHASE_FREQUENCY["question_id"], "nominal",
                               labels_read=["Once a day"])),
        )
        out = workflow.resolve_encodings(model, [_GENDER, _PURCHASE_FREQUENCY])
        # One request per variable rather than one covering both.
        self.assertEqual(len(model.calls), 2)
        for messages in model.calls:
            payload = str(messages[-1].content)
            variables = json.loads(payload[payload.index("VARIABLES:") + len("VARIABLES:"):])
            self.assertEqual(len(variables), 1)
        self.assertEqual(set(out["encodings"]),
                         {_GENDER["question_id"], _PURCHASE_FREQUENCY["question_id"]})

    def test_a_single_variable_takes_the_sequential_path(self):
        model = _ScriptedModel(_payload(_decision(
            _GENDER["question_id"], "nominal", labels_read=["Female"])))
        workflow.resolve_encodings(model, [_GENDER])
        self.assertEqual(len(model.calls), 1)

    def test_one_variable_failing_names_it_rather_than_failing_silently(self):
        bad = _payload(_decision(_GENDER["question_id"], "nominal"))  # no labels_read
        model = _ScriptedModel(bad)
        with self.assertRaises(RuntimeError) as caught:
            workflow.resolve_encodings(model, [_GENDER, _PURCHASE_FREQUENCY])
        self.assertIn("variable(s)", str(caught.exception))

    def test_the_turn_gets_the_workflow_system_prompt_and_nothing_from_the_main_agent(self):
        model = _ScriptedModel(_payload(_decision(
            _GENDER["question_id"], "nominal", labels_read=["Female"])))
        workflow.resolve_encodings(model, [_GENDER])
        rendered = "\n".join(str(m.content) for m in model.calls[0])
        self.assertIn("isolated statistical analysis pipeline", rendered)
        for constant in (instructions.SYSTEM_PROMPT_TEXT, instructions.INVENTORY_PREAMBLE,
                         prompts.NL2SQL_TOOL_DESCRIPTION):
            self.assertNotIn(constant, rendered)


class StageBNormalizationTests(unittest.TestCase):
    """Stage B: design-range scaling, z-scoring, and the near-constant flag."""

    @staticmethod
    def _rows(values_by_question):
        rows = []
        for question_id, values in values_by_question.items():
            for index, value in enumerate(values):
                rows.append({
                    "enrollment_id": f"e{index}", "product_id": "p1",
                    "question_id": question_id, "question_type": "vertical-rating",
                    "component_label": None, "role": "attribute",
                    "kind": "numeric", "value": float(value),
                })
        return rows

    @staticmethod
    def _meta(ranges):
        return {
            qid: {"question_id": qid, "label": qid, "slider_min": lo, "slider_max": hi,
                  "question_type": "vertical-rating", "labelled_positions": None}
            for qid, (lo, hi) in ranges.items()
        }

    def test_different_design_ranges_land_on_one_common_range(self):
        # The whole point: a 1-9 and a 1-5 attribute must be comparable afterwards. Both at their
        # scale maximum must normalize to 1.0, not to 9 and 5.
        result = workflow.normalize_dataset(
            self._rows({"wide": [1, 5, 9], "narrow": [1, 3, 5]}),
            self._meta({"wide": (1, 9), "narrow": (1, 5)}),
            normalization="design_range",
        )
        scaled = result["design_range_matrix"]
        self.assertEqual(scaled.shape, (3, 2))
        for column in range(2):
            self.assertAlmostEqual(scaled[0][column], 0.0)
            self.assertAlmostEqual(scaled[1][column], 0.5)
            self.assertAlmostEqual(scaled[2][column], 1.0)

    def test_the_observed_range_is_never_used_as_the_design_range(self):
        # Observed values are response data. If they set the scale, a battery where nobody used
        # the low end silently becomes a different measure.
        result = workflow.normalize_dataset(
            self._rows({"q": [7, 8, 9]}), self._meta({"q": (1, 9)}),
            normalization="design_range",
        )
        scaled = result["design_range_matrix"]
        # Scaled against 1-9, so 7 is 0.75 -- not 0.0, which is what an observed-range scaling
        # would have produced.
        self.assertAlmostEqual(scaled[0][0], 0.75)
        self.assertAlmostEqual(scaled[2][0], 1.0)

    def test_a_missing_design_range_refuses_rather_than_falling_back(self):
        metadata = self._meta({"q": (1, 9)})
        metadata["q"]["slider_min"] = None
        with self.assertRaises(RuntimeError) as caught:
            workflow.normalize_dataset(self._rows({"q": [1, 5, 9]}), metadata)
        self.assertIn("no configured design range", str(caught.exception))
        self.assertIn("must never stand in for scale metadata", str(caught.exception))

    def test_zscoring_equalizes_every_attributes_pull(self):
        # After design-range scaling a low-variance attribute still contributes a fraction of the
        # squared distance; z-scoring is what makes the contributions equal.
        result = workflow.normalize_dataset(
            self._rows({"wide": [1, 5, 9, 5], "flat": [3, 3, 3, 4]}),
            self._meta({"wide": (1, 9), "flat": (1, 5)}),
        )
        scaled_sds = [c["normalized_sd"] for c in result["columns"]]
        self.assertGreater(max(scaled_sds), min(scaled_sds) * 2)
        zscored = result["zscored_matrix"]
        self.assertTrue(np.allclose(zscored.std(axis=0, ddof=1), 1.0))
        self.assertTrue(np.allclose(zscored.mean(axis=0), 0.0))

    def test_both_normalizations_are_recorded_and_the_applied_one_is_stated(self):
        rows = self._rows({"q": [1, 5, 9]})
        metadata = self._meta({"q": (1, 9)})
        plain = workflow.normalize_dataset(rows, metadata, normalization="design_range")
        self.assertEqual(plain["normalization"]["applied"], "design_range")
        self.assertIsNone(plain["zscored_matrix"])
        both = workflow.normalize_dataset(rows, metadata)
        self.assertEqual(both["normalization"]["requested"], "design_range_zscore")
        self.assertEqual(both["normalization"]["applied"], "design_range_zscore")
        self.assertIsNotNone(both["zscored_matrix"])
        # The design-range matrix survives either way, so both are reportable.
        self.assertIsNotNone(both["design_range_matrix"])

    def test_near_constant_is_flagged_by_either_trigger_and_never_dropped(self):
        result = workflow.normalize_dataset(
            self._rows({
                "varied": [1, 2, 3, 4, 5, 6, 7, 8, 9, 5],
                "flat": [3, 3, 3, 3, 3, 3, 3, 3, 3, 4],
            }),
            self._meta({"varied": (1, 9), "flat": (1, 5)}),
        )
        columns = {c["label"]: c for c in result["columns"]}
        self.assertTrue(columns["flat"]["near_constant"])
        self.assertFalse(columns["varied"]["near_constant"])
        # Both triggers fire on this column: sd 0.0791 < 0.10 and modal share 0.9 >= 0.9.
        self.assertLess(columns["flat"]["normalized_sd"], workflow.NEAR_CONSTANT_SD)
        self.assertGreaterEqual(columns["flat"]["modal_share"],
                                workflow.NEAR_CONSTANT_MODAL_SHARE)
        # Flagged, not removed: whether to exclude it is a clustering decision with its own
        # trade-off, so both columns are still present.
        self.assertEqual(len(result["columns"]), 2)
        self.assertEqual(result["design_range_matrix"].shape[1], 2)

    def test_incomplete_keys_are_excluded_and_counted_never_imputed(self):
        rows = self._rows({"a": [1, 5, 9], "b": [1, 5, 9]})
        rows = [r for r in rows if not (r["question_id"] == "b" and r["enrollment_id"] == "e2")]
        result = workflow.normalize_dataset(rows, self._meta({"a": (1, 9), "b": (1, 9)}))
        self.assertEqual(len(result["grain_keys"]), 2)
        self.assertEqual(result["excluded"]["incomplete_keys"], 1)
        # An imputed value would enter the distance metric as if it had been measured.
        self.assertEqual(result["design_range_matrix"].shape, (2, 2))

    def test_only_attributes_enter_the_matrix(self):
        rows = self._rows({"attr": [1, 5, 9]})
        rows.append({
            "enrollment_id": "e0", "product_id": "p1", "question_id": "kpi",
            "question_type": "multiple-choice", "component_label": None, "role": "kpi",
            "kind": "category", "value": "Once a week",
        })
        result = workflow.normalize_dataset(rows, self._meta({"attr": (1, 9)}))
        self.assertEqual([c["label"] for c in result["columns"]], ["attr"])

    def test_stage_b_fails_when_stage_as_dataset_was_evicted(self):
        # 13 parking nodes against MAX_STORED_RESULTS=32 means concurrent runs can evict an
        # earlier artifact. That must be a named failure, not a silent empty matrix.
        state = {"dataset_id": "never-stored", "decisions": {"request": {
            "survey_id": "s", "variables": [{"question_id": "q1"}]}}, "artifacts": {}}
        with self.assertRaises(RuntimeError) as caught:
            workflow.stage_b_body(state, {"configurable": {"connect": lambda: _FakeConn([])}})
        self.assertIn("no longer in the result store", str(caught.exception))
        self.assertIn("evicted", str(caught.exception))


class PersonaSectionResolverTests(unittest.TestCase):
    """Stage G: quantile specifications resolved tie-aware to value thresholds."""

    COLUMNS = [{"label": "a"}, {"label": "b"}]

    @staticmethod
    def _grid():
        # Column a: an even spread. Column b: a huge tie atom at 0.5 (90% of the mass), which is
        # the shape that makes a tercile boundary unattainable.
        a = np.repeat([0.0, 0.25, 0.5, 0.75, 1.0], 20)
        b = np.array([0.0] * 5 + [0.5] * 90 + [1.0] * 5)
        return np.column_stack([a, b])

    def test_a_bound_outside_zero_to_one_is_a_hard_error(self):
        # A raw or normalized value usually betrays itself by being out of range; that is the
        # detectable case, and it is refused with the reason.
        errors = workflow.validate_section_specs({"p": {"a": [7, None]}}, ["a", "b"])
        self.assertTrue(any("outside [0,1]" in e for e in errors))
        self.assertTrue(any("rather than a QUANTILE" in e for e in errors))

    def test_malformed_specs_are_refused_with_what_was_wrong(self):
        cases = (
            ({}, "non-empty object"),
            ({"p": {}}, "non-empty object of attribute"),
            ({"p": {"zzz": [0.1, 0.9]}}, "is not one of the attributes"),
            ({"p": {"a": 0.5}}, "two-item list"),
            ({"p": {"a": ["low", None]}}, "not a number or null"),
            ({"p": {"a": [0.9, 0.1]}}, "is above upper"),
            ({"p": {"a": [None, None]}}, "the whole space"),
        )
        for specs, fragment in cases:
            with self.subTest(specs=specs):
                errors = workflow.validate_section_specs(specs, ["a", "b"])
                self.assertTrue(any(fragment in e for e in errors), errors)

    def test_resolution_keeps_every_tie_group_whole(self):
        # The property rank-percentiles violate: 90 identical answers must not be cut in two by
        # sort order. A grid threshold with inclusive bounds puts them all on one side.
        X = self._grid()
        out = workflow.resolve_persona_sections(
            {"top": {"b": [2 / 3, None]}, "bottom": {"b": [None, 1 / 3]}}, X, self.COLUMNS
        )
        tied = X[:, 1] == 0.5
        for persona, (lower, upper) in out["sections"].items():
            inside = np.all((X >= lower) & (X <= upper), axis=1)
            with self.subTest(persona=persona):
                # Every tied row shares one verdict: all in, or all out.
                self.assertIn(len(set(inside[tied].tolist())), (1,))

    def test_an_unattainable_quantile_is_reported_not_absorbed(self):
        out = workflow.resolve_persona_sections(
            {"top": {"b": [2 / 3, None]}}, self._grid(), self.COLUMNS
        )
        entry = out["report"][0]
        bound = entry["resolved"]["b"]["lower"]
        self.assertEqual(bound["requested_quantile"], 0.6667)
        # The atom sits at 0.95 cumulative, so the cut cannot happen at 2/3.
        self.assertEqual(bound["achieved_mass"], 0.95)
        self.assertTrue(entry["divergences"])
        self.assertIn("too much mass to cut there", entry["divergences"][0])

    def test_an_empty_region_is_flagged(self):
        # Both bounds on the same tiny top slice of one axis and the bottom of another.
        out = workflow.resolve_persona_sections(
            {"impossible": {"a": [1.0, None], "b": [None, 0.0]}}, self._grid(), self.COLUMNS
        )
        entry = out["report"][0]
        self.assertTrue(entry["empty"])
        self.assertEqual(entry["n"], 0)

    def test_unbounded_attributes_stay_unbounded(self):
        out = workflow.resolve_persona_sections(
            {"only_a": {"a": [2 / 3, None]}}, self._grid(), self.COLUMNS
        )
        lower, upper = out["sections"]["only_a"]
        self.assertEqual(lower[1], -np.inf)
        self.assertEqual(upper[1], np.inf)
        self.assertEqual(out["report"][0]["bounded_attributes"], ["a"])


class SectionsTurnTests(unittest.TestCase):
    """The §9.3 turn: K with a rationale, quantile-only regions, level-aware naming."""

    LABELS = ["a", "b"]

    @staticmethod
    def _payload(**overrides):
        payload = {
            "k": 4,
            "k_rationale": "four ordered bands fit the level structure",
            "persona_sections": {
                "all_low": {"a": [None, 1 / 3], "b": [None, 1 / 3]},
                "all_high": {"a": [2 / 3, None], "b": [2 / 3, None]},
            },
            "persona_rationales": {
                "all_low": "bottom third on both", "all_high": "top third on both",
            },
        }
        payload.update(overrides)
        return payload

    def _errors(self, payload, verdict="level-dominated"):
        return workflow.validate_sections_turn(payload, self.LABELS, verdict)

    def test_a_valid_turn_passes(self):
        self.assertEqual(self._errors(self._payload()), [])

    def test_k_must_be_four_or_five_with_a_rationale(self):
        self.assertTrue(any("k must be one of [4, 5]" in e for e in self._errors(
            self._payload(k=3))))
        self.assertTrue(any("k_rationale is required" in e for e in self._errors(
            self._payload(k_rationale="  "))))

    def test_every_persona_needs_a_rationale(self):
        errors = self._errors(self._payload(persona_rationales={"all_low": "x"}))
        self.assertTrue(any("all_high: a rationale is required" in e for e in errors))

    def test_a_value_masquerading_as_a_quantile_is_caught_when_out_of_range(self):
        errors = self._errors(self._payload(persona_sections={
            "high": {"a": [7.0, None]}}))
        self.assertTrue(any("rather than a QUANTILE" in e for e in errors))

    def test_a_mixed_direction_region_is_refused_under_level_dominance(self):
        # The structural form of "use level-based names when PC1 dominates": a region high on one
        # attribute and low on another IS a profile type, and this data has no profile structure.
        errors = self._errors(self._payload(
            persona_sections={"spicy_lover": {"a": [2 / 3, None], "b": [None, 1 / 3]}},
            persona_rationales={"spicy_lover": "high aroma, low sweetness"},
        ))
        self.assertTrue(any("describes a profile type" in e for e in errors))
        self.assertTrue(any("differ in LEVEL, not type" in e for e in errors))

    def test_the_same_mixed_region_is_allowed_when_shape_is_available(self):
        errors = self._errors(
            self._payload(
                persona_sections={"spicy_lover": {"a": [2 / 3, None], "b": [None, 1 / 3]}},
                persona_rationales={"spicy_lover": "high a, low b"},
            ),
            verdict="shape-bearing",
        )
        self.assertEqual(errors, [])

    def test_the_turn_never_sees_a_cluster_assignment(self):
        # Specifying regions after seeing the partition would fit them to it, making the later
        # overlap analysis circular.
        X = np.column_stack([np.linspace(0, 1, 60), np.linspace(0, 1, 60)])
        inputs = workflow.build_sections_inputs(X, [{"label": "a"}, {"label": "b"}])
        # Asserted on the structure, not on words: "clusters differ mainly in LEVEL" is legitimate
        # guidance text, while an assignment would have to arrive as a key carrying per-row data.
        self.assertEqual(set(inputs), {
            "observations", "attributes", "pairwise_correlations", "level_vs_shape", "allowed_k",
        })
        for entry in inputs["attributes"]:
            self.assertEqual(set(entry), {
                "attribute", "quantile_grid", "largest_tie_share", "skew", "near_constant",
                "context_mean", "context_sd",
            })
        # Nothing in the payload is as long as the row count, so no per-row array is present.
        self.assertTrue(all(
            len(entry["quantile_grid"]) <= inputs["observations"]
            for entry in inputs["attributes"]
        ))
        self.assertIn("quantile_grid", json.dumps(inputs))

    def test_the_input_carries_the_grid_not_just_mean_and_sd(self):
        X = np.column_stack([np.repeat([0.0, 0.5, 1.0], 20)])
        inputs = workflow.build_sections_inputs(X, [{"label": "a"}])
        attribute = inputs["attributes"][0]
        self.assertEqual(len(attribute["quantile_grid"]), 3)
        self.assertEqual(attribute["largest_tie_share"], 0.3333)
        self.assertIn("skew", attribute)
        # Mean and sd are present but labelled as context, never as the basis for a bound.
        self.assertIn("context_mean", attribute)
        self.assertIn("context_sd", attribute)

    def test_caller_supplied_sections_are_validated_not_replaced(self):
        supplied = {"pinned": {"a": [0.5, None]}}
        model = _ScriptedModel("{}")  # must not be called
        out = workflow.resolve_sections(
            model, {"level_vs_shape": {"verdict": "level-dominated"}},
            self.LABELS, supplied_sections=supplied, supplied_k=5,
        )
        self.assertEqual(out["source"], "supplied")
        self.assertEqual(out["persona_sections"], supplied)
        self.assertEqual(out["k"], 5)
        self.assertEqual(model.calls, [])

    def test_caller_supplied_sections_that_are_invalid_are_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            workflow.resolve_sections(
                _ScriptedModel("{}"), {"level_vs_shape": {"verdict": "level-dominated"}},
                self.LABELS, supplied_sections={"bad": {"a": [9, None]}},
            )
        self.assertIn("caller-supplied persona_sections are not usable", str(caught.exception))

    def test_one_retry_then_a_hard_failure(self):
        bad = json.dumps(self._payload(k=9))
        good = json.dumps(self._payload())
        model = _ScriptedModel(bad, good)
        out = workflow.resolve_sections(
            model, {"level_vs_shape": {"verdict": "level-dominated"}}, self.LABELS)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(out["k"], 4)
        self.assertIn("k must be one of", model.calls[1][-1].content)

        model = _ScriptedModel(bad, bad)
        with self.assertRaises(RuntimeError) as caught:
            workflow.resolve_sections(
                model, {"level_vs_shape": {"verdict": "level-dominated"}}, self.LABELS)
        self.assertIn("after a retry", str(caught.exception))


class RegionDistinctnessTests(unittest.TestCase):
    """Overlapping and nested regions are a specification problem, surfaced before clustering."""

    COLUMNS = [{"label": "a"}, {"label": "b"}]

    @staticmethod
    def _X():
        rng = np.random.default_rng(1)
        return rng.uniform(0, 1, size=(200, 2))

    def test_a_nested_region_is_named(self):
        # A region containing another means no cluster can match the inner one without matching
        # both, so which persona it gets comes down to tie-breaking rather than the data.
        out = workflow.resolve_persona_sections(
            {"wide": {"a": [0.2, None]}, "narrow": {"a": [0.8, None]}}, self._X(), self.COLUMNS
        )
        self.assertEqual(out["distinct_regions"], 1)
        pair = out["pairwise_overlap"][0]
        self.assertEqual(pair["nested"], "narrow")
        self.assertEqual(pair["share_of_smaller"], 1.0)
        self.assertIn("entirely contained", pair["note"])

    def test_disjoint_regions_report_no_overlap_and_stay_distinct(self):
        out = workflow.resolve_persona_sections(
            {"low": {"a": [None, 0.3]}, "high": {"a": [0.7, None]}}, self._X(), self.COLUMNS
        )
        self.assertEqual(out["pairwise_overlap"], [])
        self.assertEqual(out["distinct_regions"], 2)

    def test_coverage_reports_the_share_matched_by_no_region(self):
        out = workflow.resolve_persona_sections(
            {"tiny": {"a": [0.99, None]}}, self._X(), self.COLUMNS
        )
        self.assertLess(out["coverage"], 0.1)


class ClusteringTests(unittest.TestCase):
    """Stages D and F: deterministic k-means, cluster reporting, level-vs-shape."""

    @staticmethod
    def _blobs(seed=0):
        rng = np.random.default_rng(seed)
        return np.vstack([
            rng.normal(0.2, 0.02, size=(40, 3)),
            rng.normal(0.5, 0.02, size=(40, 3)),
            rng.normal(0.8, 0.02, size=(40, 3)),
        ])

    @staticmethod
    def _columns(count):
        return [{"label": f"a{index}"} for index in range(count)]

    def test_a_seed_is_required_because_an_unreported_one_is_unreproducible(self):
        with self.assertRaises(RuntimeError) as caught:
            workflow.kmeans(self._blobs(), 3, random_state=None)
        self.assertIn("random_state is required", str(caught.exception))

    def test_the_same_seed_gives_byte_identical_assignments(self):
        first = workflow.kmeans(self._blobs(), 3, random_state=20260819)
        second = workflow.kmeans(self._blobs(), 3, random_state=20260819)
        self.assertTrue(np.array_equal(first, second))

    def test_it_recovers_separated_groups(self):
        labels = workflow.kmeans(self._blobs(), 3, random_state=20260819)
        self.assertEqual(sorted(np.bincount(labels, minlength=3).tolist()), [40, 40, 40])

    def test_an_unusable_k_is_refused(self):
        for k in (1, 500):
            with self.subTest(k=k):
                with self.assertRaises(RuntimeError) as caught:
                    workflow.kmeans(self._blobs(), k, random_state=1)
                self.assertIn("not usable", str(caught.exception))

    def test_no_cluster_is_left_empty(self):
        # Heavily duplicated rows are the case that empties a cluster: k-means++ cannot find k
        # distinct seeds. K must still mean K, or the run reports personas that do not exist.
        duplicated = np.repeat(np.array([[0.5, 0.5], [0.5, 0.5], [0.9, 0.9]]), 10, axis=0)
        labels = workflow.kmeans(duplicated, 3, random_state=5)
        counts = np.bincount(labels, minlength=3)
        self.assertTrue((counts > 0).all(), counts)

    def test_every_cluster_carries_size_and_dispersion(self):
        X = self._blobs()
        report = workflow.cluster_report(X, workflow.kmeans(X, 3, random_state=1),
                                         self._columns(3))
        self.assertEqual(len(report["clusters"]), 3)
        for cluster in report["clusters"]:
            self.assertIn("n", cluster)
            self.assertIn("dispersion", cluster)
            self.assertIn("share", cluster)
            self.assertEqual(set(cluster["centroid"]), {"a0", "a1", "a2"})
            self.assertGreater(cluster["n"], 0)
            self.assertIsNotNone(cluster["dispersion"])

    def test_level_dominated_and_shape_bearing_are_distinguished(self):
        # One shared factor: every column rises together, so PC1 carries almost everything and
        # only the LEVEL differs between groups.
        rng = np.random.default_rng(3)
        level = rng.normal(0, 1, size=(200, 1)) @ np.ones((1, 4)) + rng.normal(0, 0.01, (200, 4))
        verdict = workflow.level_vs_shape(level)
        self.assertEqual(verdict["verdict"], "level-dominated")
        self.assertGreater(verdict["pc1_variance_share"], 0.9)
        self.assertIn("level-based persona names", verdict["naming_guidance"])

        # Independent columns: no single factor dominates, so profile shape is available.
        shape = rng.normal(0, 1, size=(400, 4))
        self.assertEqual(workflow.level_vs_shape(shape)["verdict"], "shape-bearing")


class OverlapAssignmentTests(unittest.TestCase):
    """Stages I, J and K, including the two reference defects."""

    COLUMNS = [{"label": "a"}, {"label": "b"}]

    @staticmethod
    def _two_groups():
        # Two well-separated groups: low on both, high on both.
        rng = np.random.default_rng(2)
        X = np.vstack([rng.normal(0.15, 0.02, (50, 2)), rng.normal(0.85, 0.02, (50, 2))])
        labels = np.array([0] * 50 + [1] * 50)
        return X, labels

    @staticmethod
    def _boxes(**named):
        return {name: (np.array(lower), np.array(upper)) for name, (lower, upper) in named.items()}

    def test_the_full_overlap_matrix_is_emitted_not_just_the_winner(self):
        X, labels = self._two_groups()
        out = workflow.overlap_analysis(X, labels, self._boxes(
            low=([-np.inf, -np.inf], [0.4, 0.4]), high=([0.6, 0.6], [np.inf, np.inf])))
        self.assertEqual(len(out["overlap_matrix"]), 2)
        for row in out["overlap_matrix"]:
            # Every persona's overlap, so the reader can see whether the choice was close.
            self.assertEqual(set(row["overlaps"]), {"low", "high"})
            self.assertIn("best_overlap", row)
            self.assertIn("n", row)
        self.assertEqual(out["assignment"], {0: "low", 1: "high"})
        self.assertEqual(out["distinct_personas"], 2)

    def test_defect_1_an_empty_region_yields_unassigned_not_the_first_persona(self):
        # The reference's np.argmax on an all-zero row returns index 0, silently labelling every
        # cluster with the FIRST persona. Reproduced in ClusteringLiveRegressionTests; here the
        # fixed behaviour is pinned.
        X, labels = self._two_groups()
        out = workflow.overlap_analysis(X, labels, self._boxes(
            p_first=([1.5, 1.5], [2.0, 2.0]), p_second=([3.0, 3.0], [4.0, 4.0])))
        self.assertEqual(set(out["assignment"].values()), {"unassigned"})
        self.assertNotIn("p_first", out["assignment"].values())
        self.assertEqual(len(out["unassigned"]), 2)
        self.assertIn("specification error", out["unassigned"][0]["reason"])
        self.assertEqual(out["distinct_personas"], 0)
        self.assertEqual(out["personas_matched_by_no_cluster"], ["p_first", "p_second"])

    def test_one_empty_region_among_several_does_not_capture_everything(self):
        X, labels = self._two_groups()
        out = workflow.overlap_analysis(X, labels, self._boxes(
            impossible=([1.5, 1.5], [2.0, 2.0]), low=([-np.inf, -np.inf], [0.4, 0.4])))
        # Cluster 0 matches `low`; cluster 1 matches nothing and must not fall into `impossible`.
        self.assertEqual(out["assignment"][0], "low")
        self.assertEqual(out["assignment"][1], "unassigned")

    def test_defect_2_a_many_to_one_collapse_is_reported_and_withholds_alignment(self):
        X, labels = self._two_groups()
        # One region wide enough to contain both groups.
        out = workflow.overlap_analysis(X, labels, self._boxes(
            everything=([-np.inf, -np.inf], [np.inf, np.inf])))
        self.assertEqual(out["assignment"], {0: "everything", 1: "everything"})
        self.assertEqual(len(out["many_to_one"]), 1)
        collapse = out["many_to_one"][0]
        self.assertEqual(collapse["persona"], "everything")
        self.assertEqual(collapse["clusters"], [0, 1])
        self.assertEqual(collapse["n"], 100)
        self.assertIn("uninterpretable", collapse["consequence"])
        # §14.3: the per-persona readout is withheld until the collapse is resolved.
        self.assertEqual(out["withhold_alignment"], ["everything"])
        self.assertEqual(out["distinct_personas"], 1)

    def test_a_clean_assignment_withholds_nothing(self):
        X, labels = self._two_groups()
        out = workflow.overlap_analysis(X, labels, self._boxes(
            low=([-np.inf, -np.inf], [0.4, 0.4]), high=([0.6, 0.6], [np.inf, np.inf])))
        self.assertEqual(out["withhold_alignment"], [])
        self.assertEqual(out["many_to_one"], [])

    def test_a_raised_threshold_unassigns_a_weak_match(self):
        X, labels = self._two_groups()
        # A box covering roughly the lower half of the high group: a partial match, which is what
        # a threshold is for. (A box capturing nothing is DEFECT-1's case, tested separately.)
        boxes = self._boxes(half_high=([0.6, 0.6], [0.85, 0.85]))
        loose = workflow.overlap_analysis(X, labels, boxes, min_overlap=0.0)
        strict = workflow.overlap_analysis(X, labels, boxes, min_overlap=0.9)
        self.assertEqual(loose["assignment"][1], "half_high")
        self.assertEqual(strict["assignment"][1], "unassigned")
        self.assertTrue(any("does not clear the minimum" in u["reason"]
                            for u in strict["unassigned"]))

    def test_persona_centroids_carry_size_share_dispersion_and_member_clusters(self):
        X, labels = self._two_groups()
        out = workflow.overlap_analysis(X, labels, self._boxes(
            everything=([-np.inf, -np.inf], [np.inf, np.inf])))
        centroids = workflow.persona_centroids(X, labels, out["assignment"], self.COLUMNS)
        self.assertEqual(len(centroids), 1)
        entry = centroids[0]
        self.assertEqual(entry["n"], 100)
        self.assertEqual(entry["share"], 1.0)
        self.assertEqual(entry["clusters"], [0, 1])
        self.assertIsNotNone(entry["dispersion"])
        self.assertEqual(set(entry["centroid"]), {"a", "b"})
        # A centroid averaging two clusters says so, because it averages groups the overlap
        # analysis could not separate.
        self.assertTrue(entry["spans_multiple_clusters"])

    def test_unassigned_clusters_get_their_own_centroid_entry(self):
        X, labels = self._two_groups()
        out = workflow.overlap_analysis(X, labels, self._boxes(
            impossible=([1.5, 1.5], [2.0, 2.0])))
        centroids = workflow.persona_centroids(X, labels, out["assignment"], self.COLUMNS)
        # Reported rather than dropped: 100 respondents are in no persona and that is the finding.
        self.assertEqual([c["persona"] for c in centroids], ["unassigned"])
        self.assertEqual(centroids[0]["n"], 100)


class CategoricalAttributeTests(unittest.TestCase):
    """A multiple-choice or matrix question used as a CLUSTERING attribute.

    The live failure this covers: six 5-level product descriptors were requested as attributes,
    stage A made every answer LEVEL its own column (29 of them), none had a scale, and stage B
    refused the whole run. The agent then silently retried without those six -- an answer built on
    a narrowed attribute set that read as complete.
    """

    LEVELS = ["Level 1 (low)", "Level 2", "Level 3 (moderate)", "Level 4", "Level 5 (high)"]

    @staticmethod
    def _rows(question_id, labels, question_type="multiple-choice", component=None):
        return [
            {
                "enrollment_id": f"e{index}", "product_id": "p1",
                "question_id": question_id, "question_type": question_type,
                "component_label": component, "role": "attribute",
                "kind": "category", "value": label,
            }
            for index, label in enumerate(labels)
        ]

    @staticmethod
    def _meta(question_id, **overrides):
        meta = {
            "question_id": question_id, "label": question_id, "question_type": "multiple-choice",
            "slider_min": None, "slider_max": None, "labelled_positions": None,
            "column_value_min": None, "column_value_max": None,
        }
        meta.update(overrides)
        return {question_id: meta}

    @staticmethod
    def _extract(raw_rows):
        return workflow.extract_dataset(
            connect=lambda: _FakeConn(raw_rows),
            survey_id="s",
            variables=[{"question_id": "q1", "role": "attribute"}],
        )["rows"]

    def test_the_picked_option_is_the_value_not_a_sub_item(self):
        # Stage A level: a multiple-choice question has no components, so nothing may arrive
        # carrying the chosen option as one. Otherwise each answer becomes its own column.
        rows = self._extract([
            _raw_row(question_type="multiple-choice", component_id="opt-3",
                     component_label="Level 3 (moderate)", value_label="Level 3 (moderate)"),
        ])
        self.assertIsNone(rows[0]["component_label"])
        self.assertEqual(rows[0]["value"], "Level 3 (moderate)")
        # component_id survives: it keys deduplication, and a multi-select needs one row per pick.
        self.assertEqual(rows[0]["component_id"], "opt-3")

    def test_a_matrix_row_stays_a_sub_item(self):
        # The opposite case, and the reason this is not a blanket rule: a matrix ROW genuinely is
        # a separate attribute, so its label must survive.
        rows = self._extract([
            _raw_row(question_type="matrix", component_id="row-2",
                     component_label="Aroma Liking", value_label="4", value_code=4),
        ])
        self.assertEqual(rows[0]["component_label"], "Aroma Liking")

    def test_an_ordered_categorical_becomes_one_column_at_rank_over_span(self):
        rows = self._rows("q1", self.LEVELS)
        encodings = {"q1": {"encoding": "ordinal", "ordered_categories": self.LEVELS}}
        result = workflow.normalize_dataset(
            rows, self._meta("q1"), normalization="design_range", encodings=encodings)
        self.assertEqual(len(result["columns"]), 1)
        self.assertEqual(result["columns"][0]["design_range"], (0.0, 4.0))
        self.assertEqual(result["columns"][0]["value_source"], "category_rank")
        # rank/span, which is exactly what the ordinal KPI encoder produces for the same labels.
        self.assertEqual(
            [round(v, 4) for v in result["design_range_matrix"][:, 0].tolist()],
            [0.0, 0.25, 0.5, 0.75, 1.0],
        )

    def test_without_an_ordered_sequence_it_refuses_and_says_what_is_missing(self):
        # An unordered categorical has no defensible spacing. Inventing one would put a
        # fabricated distance into the clustering metric, so this must stay a refusal -- but the
        # message has to name the sequence, or the reader looks for a slider that never existed.
        with self.assertRaises(RuntimeError) as caught:
            workflow.normalize_dataset(
                self._rows("q1", self.LEVELS), self._meta("q1"),
                normalization="design_range", encodings={"q1": {"encoding": "nominal"}})
        self.assertIn("no ordered sequence from the encoding step", str(caught.exception))

    def test_an_excluded_category_yields_no_value_and_is_counted(self):
        rows = self._rows("q1", self.LEVELS)
        encodings = {"q1": {
            "encoding": "ordinal", "ordered_categories": self.LEVELS,
            "excluded_categories": ["Level 3 (moderate)"],
        }}
        result = workflow.normalize_dataset(
            rows, self._meta("q1"), normalization="design_range", encodings=encodings)
        # Four of five keys survive; the excluded one is reported, never imputed to the middle.
        self.assertEqual(len(result["grain_keys"]), 4)
        self.assertEqual(result["columns"][0]["n_excluded"], 1)
        self.assertEqual(result["excluded"]["non_numeric_rows"], 1)

    def test_a_matrix_takes_its_design_range_from_its_column_options(self):
        # A matrix has no settingSlider at all. Its scale is the configured analytical_value of
        # its COLUMN options -- configuration, not response data.
        rows = [
            {
                "enrollment_id": f"e{index}", "product_id": "p1", "question_id": "m1",
                "question_type": "matrix", "component_label": "Aroma", "role": "attribute",
                "kind": "numeric", "value": float(value),
            }
            for index, value in enumerate([1, 3, 5])
        ]
        meta = self._meta("m1", question_type="matrix",
                          column_value_min=1, column_value_max=5)
        result = workflow.normalize_dataset(
            rows, meta, normalization="design_range")
        self.assertEqual(result["columns"][0]["design_range"], (1.0, 5.0))
        self.assertEqual(
            [round(v, 4) for v in result["design_range_matrix"][:, 0].tolist()],
            [0.0, 0.5, 1.0],
        )

    def test_a_slider_still_wins_over_column_options(self):
        # Precedence, stated: settingSlider is the question's own configured range.
        meta = self._meta("q1", slider_min=1, slider_max=9,
                          column_value_min=1, column_value_max=5)
        self.assertEqual(workflow._design_range(meta["q1"]), (1.0, 9.0))


class Q2WordingTests(unittest.TestCase):
    """A negative Q-squared is a statement, not a measurement to quote."""

    def test_a_negative_q2_is_reported_as_no_predictive_validity(self):
        # Observed live: -670.9736 printed as "primary measure Q2=-670.9736", which reads as a
        # precise finding. Leave-one-out over 4 centroids drops a quarter of the data per fold.
        words = workflow.q2_in_words(-670.9736, 4)
        self.assertIn("NO PREDICTIVE VALIDITY", words)
        self.assertNotIn("primary measure", words)
        # The number stays: it is the audit trail for the claim.
        self.assertIn("-670.9736", words)

    def test_a_positive_q2_is_still_quoted_plainly(self):
        self.assertEqual(workflow.q2_in_words(0.4411, 4), "Q2=0.4411")

    def test_an_uncomputable_q2_says_why(self):
        self.assertIn("at least 4", workflow.q2_in_words(None))


class UnnamedScaleEndTests(unittest.TestCase):
    """When neither end of a scale can be named, the alignment sentence is withheld."""

    def test_no_sequence_and_no_anchors_withholds_the_wording(self):
        # The live case: a 1-9 vertical rating whose configured anchors are null. The old
        # fallback said "sits toward 'the low end of the scale'" -- a sign with no meaning
        # attached, phrased as a finding.
        result = workflow.alignment_in_words(
            -0.5349, {"encoding": "numeric", "direction": "not established"}, "LIKING")
        self.assertEqual(result["alignment"], "withheld")
        self.assertIn("withheld", result["in_words"])
        self.assertNotIn("the low end of the scale", result["in_words"])
        # The sign is kept for audit even though the sentence is not made.
        self.assertEqual(result["sign"], -0.5349)

    def test_a_stated_sequence_still_names_the_end(self):
        result = workflow.alignment_in_words(
            -1.0, {"encoding": "ordinal", "ordered_categories": ["Never", "Daily"]}, "FREQUENCY")
        self.assertEqual(result["alignment"], "negative")
        self.assertIn("'Never'", result["in_words"])

    def test_two_anchors_are_enough_to_name_the_ends(self):
        result = workflow.alignment_in_words(
            1.0, {"encoding": "numeric", "anchors": ["Dislike a lot", "Like a lot"]}, "LIKING")
        self.assertEqual(result["alignment"], "positive")
        self.assertIn("'Like a lot'", result["in_words"])


class PlsrTests(unittest.TestCase):
    """Stages L, M and N: the fit, its honest reporting, and the alignment wording."""

    def test_the_component_cap_is_two_below_the_observation_count(self):
        # min(n-2, p): at n-1 components the centroid fit is already saturated and reports 1.0000
        # whatever the data, so it is not a cap worth having.
        self.assertEqual(workflow.component_cap(4, 7), 2)
        self.assertEqual(workflow.component_cap(5, 7), 3)
        self.assertEqual(workflow.component_cap(16, 7), 7)
        self.assertEqual(workflow.component_cap(3, 7), 1)

    def test_r2_saturates_at_the_observation_count_which_is_why_q2_is_the_measure(self):
        # §11.2's central fact, on synthetic data: with p >= n-1 the fit reaches 1.0000 by
        # construction. Any pipeline quoting a centroid R² as goodness of fit is misreporting.
        rng = np.random.default_rng(4)
        X = rng.normal(0, 1, (4, 7))
        y = rng.normal(0, 1, 4)
        r2_by_components = [workflow.plsr_fit(X, y, n)["r2"] for n in (1, 2, 3)]
        self.assertLess(r2_by_components[0], 1.0)
        self.assertAlmostEqual(r2_by_components[2], 1.0, places=6)
        self.assertEqual(sorted(r2_by_components), r2_by_components)

    def test_q2_is_far_below_r2_when_the_relationship_is_weak(self):
        rng = np.random.default_rng(5)
        X = rng.normal(0, 1, (12, 4))
        y = rng.normal(0, 1, 12)  # no real relationship
        fit = workflow.plsr_fit(X, y, 2)
        q2 = workflow.loo_q2(X, y, 2)
        self.assertGreater(fit["r2"], 0.2)
        self.assertLess(q2, fit["r2"])
        # Pure noise: cross-validation should be at or below zero.
        self.assertLess(q2, 0.2)

    def test_q2_is_none_when_there_are_too_few_points_to_cross_validate(self):
        rng = np.random.default_rng(6)
        self.assertIsNone(workflow.loo_q2(rng.normal(0, 1, (3, 2)), rng.normal(0, 1, 3), 1))

    def test_every_fit_carries_n_and_its_component_count(self):
        # "R² is never emitted without n, grain and component count" -- n and the count come from
        # the fit itself; the grain is attached by the stage that names it.
        rng = np.random.default_rng(7)
        fit = workflow.plsr_fit(rng.normal(0, 1, (10, 3)), rng.normal(0, 1, 10), 2)
        self.assertEqual(fit["n"], 10)
        self.assertEqual(fit["n_components"], 2)
        self.assertIsNotNone(fit["r2"])
        self.assertIsNotNone(fit["mean_absolute_error"])

    def test_vip_ranks_the_driving_attribute_highest(self):
        rng = np.random.default_rng(8)
        X = rng.normal(0, 1, (40, 3))
        y = 3.0 * X[:, 1] + rng.normal(0, 0.1, 40)  # column 1 drives y
        vip = workflow.plsr_fit(X, y, 2)["vip"]
        self.assertEqual(int(np.argmax(vip)), 1)

    def test_alignment_is_rendered_from_the_stated_sequence_not_from_prose(self):
        # The trap §11.3 names: on a scale whose codes run "Once a day"=1 to "Never"=8, a positive
        # alignment with the coded variable means buying LESS often. Deriving the wording from the
        # stated sequence makes that impossible to get backwards.
        ascending = {
            "ordered_categories": ["Less often than six months", "Once a month", "Once a day"],
            "direction": "higher means more frequent purchase",
        }
        positive = workflow.alignment_in_words(1.0, ascending, "purchase frequency")
        self.assertEqual(positive["alignment"], "positive")
        self.assertIn("Once a day", positive["in_words"])
        negative = workflow.alignment_in_words(-1.0, ascending, "purchase frequency")
        self.assertEqual(negative["alignment"], "negative")
        self.assertIn("Less often than six months", negative["in_words"])

        # The same sign against a sequence stated the other way must read the other way.
        descending = {
            "ordered_categories": ["Once a day", "Once a month", "Less often than six months"],
            "direction": "higher means less frequent purchase",
        }
        flipped = workflow.alignment_in_words(1.0, descending, "purchase frequency")
        self.assertIn("Less often than six months", flipped["in_words"])
        self.assertNotEqual(positive["in_words"], flipped["in_words"])

    def test_alignment_falls_back_to_anchors_for_a_numeric_scale(self):
        numeric = {"anchors": ["Dislike Extremely", "Like Extremely"], "direction": "higher = more"}
        words = workflow.alignment_in_words(1.0, numeric, "liking")
        self.assertIn("Like Extremely", words["in_words"])
        self.assertEqual(workflow.alignment_in_words(0.0, numeric, "liking")["alignment"], "none")

    def test_the_recorded_direction_travels_with_the_alignment(self):
        words = workflow.alignment_in_words(
            1.0, {"ordered_categories": ["a", "b"], "direction": "higher = more of it"}, "kpi")
        self.assertEqual(words["direction_recorded"], "higher = more of it")

    def test_spearman_averages_tied_ranks(self):
        # Without tie averaging, identical values get arbitrary distinct ranks and the correlation
        # depends on sort order.
        a = np.array([1.0, 1.0, 1.0, 2.0, 3.0])
        b = np.array([5.0, 5.0, 5.0, 6.0, 7.0])
        self.assertAlmostEqual(workflow._spearman(a, b), 1.0, places=6)
        self.assertIsNone(workflow._pearson(np.zeros(5), np.arange(5.0)))


class KpiFitTests(unittest.TestCase):
    """KPI encoding into a fittable vector, and the two-grain fit that reports it."""

    COLUMNS = [{"label": "a"}, {"label": "b"}]
    ORDINAL = {
        "encoding": "ordinal",
        "ordered_categories": ["rarely", "sometimes", "often"],
        "excluded_categories": ["never"],
        "direction": "higher means more often",
    }

    @staticmethod
    def _rows(values, products=("p1",)):
        rows = []
        for index, value in enumerate(values):
            product = products[index % len(products)]
            rows.append({
                "enrollment_id": f"e{index}", "product_id": product,
                "question_id": "kpi", "role": "kpi", "kind": "category", "value": value,
            })
        return rows

    def _keys(self, count, products=("p1",)):
        return [(f"e{index}", products[index % len(products)]) for index in range(count)]

    def test_an_ordinal_kpi_maps_to_its_rank_position(self):
        rows = self._rows(["rarely", "sometimes", "often"])
        out = workflow.kpi_values(rows, self._keys(3), "kpi", self.ORDINAL)
        self.assertTrue(out["fittable"])
        self.assertEqual(out["values"].tolist(), [0.0, 0.5, 1.0])

    def test_an_excluded_category_is_dropped_and_counted_not_ranked(self):
        # "never" must not become the lowest frequency: it is not a point on the scale.
        rows = self._rows(["rarely", "never", "often"])
        out = workflow.kpi_values(rows, self._keys(3), "kpi", self.ORDINAL)
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["excluded_counts"], {"never": 1})
        self.assertNotIn(0.0, out["values"].tolist()[1:])
        self.assertEqual(sorted(out["values"].tolist()), [0.0, 1.0])

    def test_a_numeric_kpi_is_scaled_by_its_design_range(self):
        rows = [{"enrollment_id": f"e{i}", "product_id": "p1", "question_id": "kpi",
                 "role": "kpi", "kind": "numeric", "value": v} for i, v in enumerate([1, 5, 9])]
        out = workflow.kpi_values(
            rows, self._keys(3), "kpi", {"encoding": "numeric"},
            {"slider_min": 1, "slider_max": 9},
        )
        self.assertEqual(out["values"].tolist(), [0.0, 0.5, 1.0])

    def test_an_unfittable_encoding_reports_its_reason_rather_than_a_number(self):
        for encoding in ("nominal", "jar", "binary"):
            with self.subTest(encoding=encoding):
                out = workflow.kpi_values(
                    self._rows(["x", "y"]), self._keys(2), "kpi", {"encoding": encoding})
                self.assertFalse(out["fittable"])
                self.assertTrue(out["reason"])
                # The distribution is still reported -- the KPI is not silently dropped.
                self.assertEqual(set(out["categories"]), {"x", "y"})

    def _fitted(self, products=("p1", "p2")):
        rng = np.random.default_rng(11)
        count = 80
        X = rng.uniform(0, 1, (count, 2))
        labels = np.array([0] * 20 + [1] * 20 + [2] * 20 + [3] * 20)
        keys = self._keys(count, products)
        order = ["rarely", "sometimes", "often"]
        values = [order[min(int(x * 3), 2)] for x in X[:, 0]]
        kpi = workflow.kpi_values(self._rows(values, products), keys, "kpi", self.ORDINAL)
        return workflow.fit_persona_kpi(
            X, labels, keys, self.COLUMNS, kpi, "frequency", self.ORDINAL,
            assignment={0: "all_low", 1: "all_low", 2: "middle", 3: "all_high"},
            withhold=["all_low"],
        )

    def test_q2_is_the_primary_measure_and_r2_is_labelled_a_diagnostic(self):
        fit = self._fitted()
        self.assertEqual(fit["primary_measure"], "q2")
        centroid = fit["centroid_grain"]
        self.assertIn("diagnostic", centroid["r2_is"])
        self.assertIn("primary measure", centroid["q2_is"])

    def test_no_r2_travels_without_its_n_grain_and_component_count(self):
        fit = self._fitted()
        for grain in (fit["centroid_grain"], fit["cluster_product_grain"]):
            self.assertIn("r2", grain)
            self.assertIn("n", grain)
            self.assertIn("grain", grain)
            self.assertIn("n_components", grain)
        # And the saturation curve is shown, which is the evidence for calling R² a diagnostic.
        self.assertGreaterEqual(len(fit["centroid_grain"]["r2_by_components"]), 1)

    def test_every_centroid_figure_is_labelled_descriptive(self):
        centroid = self._fitted()["centroid_grain"]
        self.assertTrue(centroid["descriptive"])
        self.assertIn("DESCRIPTIVE", centroid["descriptive_note"])
        self.assertIn("not predictive", centroid["descriptive_note"])

    def test_the_cluster_product_grain_is_emitted_when_products_allow_it(self):
        self.assertIsNotNone(self._fitted(products=("p1", "p2"))["cluster_product_grain"])
        # With a single product there is no second grain to fit, and it must not be invented.
        self.assertIsNone(self._fitted(products=("p1",))["cluster_product_grain"])

    def test_a_persona_holding_two_clusters_has_its_alignment_withheld(self):
        fit = self._fitted()
        withheld = [p for p in fit["per_persona"] if p.get("withheld")]
        self.assertTrue(withheld)
        for persona in withheld:
            self.assertEqual(persona["persona"], "all_low")
            self.assertIn("more than one cluster", persona["withheld"])
        # A cleanly assigned persona keeps its alignment.
        clean = [p for p in fit["per_persona"] if p["persona"] == "all_high"]
        self.assertTrue(clean)
        self.assertNotIn("withheld", clean[0])

    def test_each_persona_carries_its_score_alignment_and_words(self):
        for persona in self._fitted()["per_persona"]:
            self.assertIn("t1", persona)
            self.assertIn("q1", persona)
            self.assertIn(persona["alignment"], {"positive", "negative", "none"})
            self.assertTrue(persona["in_words"])
            self.assertIn("mean_kpi_normalized", persona)

    def test_correlations_carry_n_and_are_marked_exploratory(self):
        fit = self._fitted()
        self.assertEqual(len(fit["correlations"]), 2)
        for entry in fit["correlations"]:
            self.assertIn("n", entry)
            self.assertTrue(entry["exploratory"])
            self.assertIn("vip", entry)
        self.assertEqual(fit["correlations_are"], "exploratory, not causal")

    def test_the_individual_level_model_uses_an_ordinary_r2_for_an_ordinal_kpi(self):
        individual = self._fitted()["individual_level"]
        self.assertTrue(individual["fitted"])
        self.assertEqual(individual["measure"], "R-squared")
        self.assertIn("row", individual["grain"])
        self.assertGreater(individual["n"], 4)

    def test_a_non_numeric_kpi_reports_a_named_pseudo_r2_never_an_r2(self):
        # §20 Step 6: "a non-numeric KPI reports a NAMED pseudo-R²".
        rng = np.random.default_rng(12)
        count = 60
        X = rng.uniform(0, 1, (count, 2))
        keys = self._keys(count)
        values = ["yes" if x > 0.5 else "no" for x in X[:, 0]]
        kpi = workflow.kpi_values(
            self._rows(values), keys, "kpi", {"encoding": "nominal"})
        index = np.arange(count)
        kpi = {**kpi, "values": np.array(values, dtype=object)}
        individual = workflow.individual_level_validation(X, kpi, index)
        self.assertTrue(individual["fitted"])
        self.assertEqual(individual["measure"], "McFadden pseudo-R-squared")
        self.assertNotEqual(individual["measure"], "R-squared")
        self.assertIn("not comparable", individual["note"])
        self.assertIn("logistic", individual["model"])


class ReportTurnTests(unittest.TestCase):
    """§9.4: the turn that may select and phrase figures, but never derive them."""

    PAYLOAD = {"targets": [{"kpi": "frequency", "centroid_grain": {
        "n": 4, "r2": 0.9979, "q2": 0.5071, "n_components": 2}}]}
    GOOD = (
        "RESULT: Q-squared 0.5071 at the cluster centroid grain, n=4. R-squared 0.9979 at the "
        "same grain with 2 components, a diagnostic only.\n"
        "DIAGNOSTICS: one KPI fitted.\n"
        "STILL MISSING: nothing"
    )

    def test_a_compliant_report_passes(self):
        self.assertEqual(workflow.validate_report(self.GOOD, self.PAYLOAD), [])

    def test_a_missing_or_misordered_section_is_caught(self):
        self.assertTrue(any("STILL MISSING section is missing" in error for error in
                            workflow.validate_report("RESULT: x\nDIAGNOSTICS: y", self.PAYLOAD)))
        scrambled = "DIAGNOSTICS: y\nRESULT: x\nSTILL MISSING: nothing"
        self.assertTrue(any("in the order RESULT" in error for error in
                            workflow.validate_report(scrambled, self.PAYLOAD)))

    def test_a_computed_number_is_rejected(self):
        # A percentage the model worked out is exactly the failure this check exists for.
        report = self.GOOD.replace("n=4", "n=4, which is 50.7% of variance")
        errors = workflow.validate_report(report, self.PAYLOAD)
        self.assertTrue(any("do not appear in the stage outputs" in e for e in errors))
        self.assertIn("50.7", "".join(errors))

    def test_a_re_rounded_figure_is_rejected(self):
        # 0.51 in place of 0.5071 reads as precision the analysis never had.
        report = self.GOOD.replace("0.5071", "0.51")
        errors = workflow.validate_report(report, self.PAYLOAD)
        self.assertTrue(any("re-rounded" in e for e in errors))

    def test_trailing_zeros_are_the_same_figure(self):
        payload = {"r2": 1.0}
        self.assertEqual(workflow.invented_numbers("R-squared is 1.0", payload), [])
        self.assertEqual(workflow.invented_numbers("R-squared is 1", payload), [])

    def test_analyst_facing_furniture_is_rejected(self):
        for banned in ("{{Run stats on flavor}}", "```gpi-chart\n{}\n```"):
            with self.subTest(banned=banned):
                errors = workflow.validate_report(self.GOOD + "\n" + banned, self.PAYLOAD)
                self.assertTrue(any("remove" in e for e in errors))

    def test_a_compliant_turn_is_used(self):
        model = _ScriptedModel(self.GOOD)
        out = workflow.compose_report(model, self.PAYLOAD, "FALLBACK")
        self.assertEqual(out["source"], "model")
        self.assertEqual(out["text"], self.GOOD)
        self.assertEqual(len(model.calls), 1)

    def test_one_retry_is_offered_with_the_specific_failure(self):
        bad = self.GOOD.replace("0.5071", "0.51")
        model = _ScriptedModel(bad, self.GOOD)
        out = workflow.compose_report(model, self.PAYLOAD, "FALLBACK")
        self.assertEqual(out["source"], "model")
        self.assertEqual(len(model.calls), 2)
        self.assertIn("re-rounded", model.calls[1][-1].content)

    def test_a_persistently_non_compliant_turn_falls_back_rather_than_losing_the_analysis(self):
        bad = "RESULT: made up 0.4242\nDIAGNOSTICS: x\nSTILL MISSING: nothing"
        model = _ScriptedModel(bad, bad)
        out = workflow.compose_report(model, self.PAYLOAD, "FALLBACK TEXT")
        # The deterministic report is itself correct, assembled from the same stage outputs, so
        # discarding a complete analysis over its presentation would be the worse failure.
        self.assertEqual(out["source"], "deterministic_fallback")
        self.assertEqual(out["text"], "FALLBACK TEXT")
        self.assertEqual(len(out["attempts"]), 2)

    def test_the_model_report_is_opt_in_and_the_deterministic_one_is_the_default(self):
        # Measured: the model turn was 63% of the workflow's 69s latency, producing 19,000 chars of
        # prose that the calling agent then rewrites. The deterministic block renders the same
        # payload, so the default costs nothing statistically and saves ~44 seconds.
        self.assertFalse(workflow.REPORT_WITH_MODEL,
                         "the model report turn should be off unless explicitly enabled")
        source = pathlib.Path(workflow.__file__).read_text(encoding="utf-8")
        self.assertIn("if REPORT_WITH_MODEL:", source)
        # The turn itself still works when enabled -- it is gated, not removed.
        model = _ScriptedModel(self.GOOD)
        out = workflow.compose_report(model, self.PAYLOAD, "FALLBACK")
        self.assertEqual(out["source"], "model")

    def test_the_report_prompt_demands_terseness(self):
        # The second lever the codebase names: write fewer tokens. Generation IS the latency here.
        prompt = workflow_instructions.REPORT_STEP_TEMPLATE
        self.assertIn("BE TERSE", prompt)
        self.assertIn("One line per figure", prompt)
        self.assertIn("no summary paragraph", prompt)
        self.assertIn("top three only", prompt)
        self.assertIn("Do not list the per-cell breakdowns", prompt)
        # And terseness must not cost a required caveat.
        self.assertIn("alignment IN WORDS", prompt)
        self.assertIn("labelled a diagnostic", prompt)
        self.assertIn("say withheld and why", prompt)

    def test_the_report_prompt_forbids_deriving_and_names_the_three_sections(self):
        prompt = workflow_instructions.REPORT_STEP_TEMPLATE
        self.assertIn("YOU MAY NOT PRODUCE A NUMBER THAT IS NOT ALREADY IN THE DATA", prompt)
        self.assertIn("re-round", prompt)
        self.assertIn("alignment IN WORDS", prompt)
        for section in ("RESULT:", "DIAGNOSTICS:", "STILL MISSING:"):
            self.assertIn(section, prompt)
        # It is the tool-free final turn, so it must not be told to call anything.
        self.assertNotIn("call the tool", prompt.lower())


class PerPersonaReadoutTests(unittest.TestCase):
    """§11.8: the figures that answer "which persona fits this product best"."""

    @staticmethod
    def _cells(products=4, clusters=2):
        rng = np.random.default_rng(21)
        cells, cell_y, info = [], [], []
        for cluster in range(clusters):
            for product in range(products):
                cells.append(rng.uniform(0, 1, 3))
                cell_y.append(0.3 + 0.1 * cluster + 0.05 * product + rng.normal(0, 0.01))
                info.append({"cluster": cluster, "product": f"p{product}"})
        return np.array(cells), np.array(cell_y), info

    def test_per_persona_q2_is_null_with_its_reason_below_the_product_threshold(self):
        # Measured negative for every persona at 4 products, so it is never computed silently.
        cells, cell_y, info = self._cells(products=4)
        predictions = workflow.plsr_fit(cells, cell_y, 1)["predicted"]
        readout = workflow.per_persona_product_readout(cells, cell_y, info, predictions, 0)
        self.assertIsNone(readout["per_persona_q2"])
        self.assertIn("n_products too small (4)", readout["per_persona_q2_reason"])
        self.assertIn(f"needs >= {workflow.PER_PERSONA_Q2_MIN_PRODUCTS}",
                      readout["per_persona_q2_reason"])
        self.assertIn("would rank noise", readout["per_persona_q2_reason"])

    def test_per_persona_q2_is_computed_once_enough_products_exist(self):
        cells, cell_y, info = self._cells(products=workflow.PER_PERSONA_Q2_MIN_PRODUCTS)
        predictions = workflow.plsr_fit(cells, cell_y, 1)["predicted"]
        readout = workflow.per_persona_product_readout(cells, cell_y, info, predictions, 0)
        self.assertIsNotNone(readout["per_persona_q2"])
        self.assertIsNone(readout["per_persona_q2_reason"])

    def test_the_prediction_error_comes_from_the_global_model(self):
        cells, cell_y, info = self._cells()
        predictions = workflow.plsr_fit(cells, cell_y, 1)["predicted"]
        readout = workflow.per_persona_product_readout(cells, cell_y, info, predictions, 1)
        error = readout["prediction_error"]
        self.assertIn("from the global model", error["grain"])
        self.assertGreaterEqual(error["max_absolute"], error["mean_absolute"])
        self.assertIn("equally well", error["note"])

    def test_the_correlation_carries_its_product_count_and_is_descriptive(self):
        cells, cell_y, info = self._cells()
        predictions = workflow.plsr_fit(cells, cell_y, 1)["predicted"]
        readout = workflow.per_persona_product_readout(cells, cell_y, info, predictions, 0)
        correlation = readout["correlation_across_products"]
        self.assertEqual(correlation["n_products"], 4)
        self.assertTrue(correlation["descriptive"])
        self.assertIn("across products", correlation["basis"])
        # And the within-persona fit is labelled in-sample, not validated.
        self.assertTrue(readout["within_persona_fit"]["descriptive"])
        self.assertIn("not cross-validated", readout["within_persona_fit"]["descriptive_note"])

    def test_a_cluster_with_too_few_products_gets_no_fabricated_correlation(self):
        cells, cell_y, info = self._cells(products=2)
        predictions = workflow.plsr_fit(cells, cell_y, 1)["predicted"]
        readout = workflow.per_persona_product_readout(cells, cell_y, info, predictions, 0)
        self.assertEqual(readout["n_products"], 2)
        self.assertNotIn("correlation_across_products", readout)
        # The prediction error still works -- it needs no within-persona model.
        self.assertIn("prediction_error", readout)


class PersonaProfileTests(unittest.TestCase):
    """§11.7: exhaustive profiles, and the joinability check that stops fabrication."""

    COLUMNS = [
        {"label": "aroma", "design_range": (1.0, 9.0)},
        {"label": "crisp", "design_range": (1.0, 5.0)},
    ]

    @staticmethod
    def _setup(demographics=None):
        X = np.array([[0.9, 0.5], [0.85, 0.55], [0.2, 0.4], [0.25, 0.45]])
        labels = np.array([0, 0, 1, 1])
        keys = [("e0", "p1"), ("e1", "p1"), ("e2", "p1"), ("e3", "p1")]
        assignment = {0: "all_high", 1: "all_low"}
        sections = [
            {"persona": "all_high", "resolved": {"aroma": {"lower": {}}},
             "bounded_attributes": ["aroma"], "mass": 0.5, "divergences": []},
            {"persona": "all_low", "resolved": {"aroma": {"upper": {}}},
             "bounded_attributes": ["aroma"], "mass": 0.5, "divergences": []},
        ]
        overlap = [{"cluster": 0, "best_overlap": 0.9}, {"cluster": 1, "best_overlap": 0.8}]
        return X, labels, keys, assignment, sections, overlap, [], demographics or {}

    def test_joinability_names_each_case(self):
        self.assertEqual(
            workflow.assess_joinability({"a", "b"}, {"a", "b"})["joinability"], "joined")
        self.assertEqual(
            workflow.assess_joinability({"a", "b"}, {"a"})["joinability"], "partial")
        self.assertEqual(
            workflow.assess_joinability({"a"}, {"b"})["joinability"], "disjoint")
        self.assertEqual(
            workflow.assess_joinability({"a"}, set())["joinability"], "no_demographics")

    def test_disjoint_reports_both_counts_and_refuses_to_join(self):
        verdict = workflow.assess_joinability({"a", "b"}, {"c", "d"})
        self.assertEqual(verdict["analysis_enrollments"], 2)
        self.assertEqual(verdict["demographic_enrollments"], 2)
        self.assertEqual(verdict["joined_enrollments"], 0)
        self.assertIn("would describe different people", verdict["note"])

    def test_a_disjoint_survey_gets_null_demographics_not_invented_ones(self):
        # The failure this exists to prevent: attaching a different set of people's answers.
        demographics = {"other1": [{"question_id": "age", "question": "Age", "label": "75+"}]}
        out = workflow.profile_personas(
            self._setup(demographics)[0], self._setup(demographics)[1],
            self._setup(demographics)[2], self.COLUMNS, *self._setup(demographics)[3:])
        self.assertEqual(out["joinability"]["joinability"], "disjoint")
        for persona in out["personas"]:
            self.assertIsNone(persona["demographics"])
            self.assertEqual(persona["distinguishing"], [])
            self.assertEqual(persona["joinability"], "disjoint")

    def test_a_joined_survey_gets_exhaustive_distributions_with_base_shares(self):
        demographics = {
            "e0": [{"question_id": "age", "question": "Age", "label": "75+"}],
            "e1": [{"question_id": "age", "question": "Age", "label": "75+"}],
            "e2": [{"question_id": "age", "question": "Age", "label": "25-34"}],
            "e3": [{"question_id": "age", "question": "Age", "label": "75+"}],
        }
        out = workflow.profile_personas(
            self._setup(demographics)[0], self._setup(demographics)[1],
            self._setup(demographics)[2], self.COLUMNS, *self._setup(demographics)[3:])
        self.assertEqual(out["joinability"]["joinability"], "joined")
        high = next(p for p in out["personas"] if p["persona"] == "all_high")
        age = high["demographics"][0]
        # Exhaustive: every observed category, with counts, shares AND the base to compare to.
        self.assertEqual(age["distribution"], {"75+": 2})
        self.assertEqual(age["shares"]["75+"], 1.0)
        self.assertEqual(age["base_shares"]["75+"], 0.75)
        self.assertEqual(age["n"], 2)

    def test_distinguishing_fires_only_past_the_stated_threshold(self):
        demographics = {
            "e0": [{"question_id": "age", "question": "Age", "label": "75+"}],
            "e1": [{"question_id": "age", "question": "Age", "label": "75+"}],
            "e2": [{"question_id": "age", "question": "Age", "label": "25-34"}],
            "e3": [{"question_id": "age", "question": "Age", "label": "25-34"}],
        }
        out = workflow.profile_personas(
            self._setup(demographics)[0], self._setup(demographics)[1],
            self._setup(demographics)[2], self.COLUMNS, *self._setup(demographics)[3:])
        high = next(p for p in out["personas"] if p["persona"] == "all_high")
        # 100% against a 50% base is a 50-point deviation, well past the 10-point cut.
        self.assertTrue(high["distinguishing"])
        entry = high["distinguishing"][0]
        self.assertEqual(entry["label"], "75+")
        self.assertEqual(entry["share"], 1.0)
        self.assertEqual(entry["base"], 0.5)
        self.assertEqual(entry["delta"], 0.5)
        self.assertEqual(out["distinguishing_delta"], workflow.DISTINGUISHING_DELTA)

    def test_every_persona_carries_the_required_fields(self):
        out = workflow.profile_personas(
            self._setup()[0], self._setup()[1], self._setup()[2], self.COLUMNS,
            *self._setup()[3:])
        for persona in out["personas"]:
            for field in ("persona", "clusters", "n", "share_of_respondents", "region",
                          "centroid", "kpi_summary", "joinability", "demographics",
                          "distinguishing"):
                self.assertIn(field, persona)
            self.assertIn("spec", persona["region"])
            self.assertIn("overlap", persona["region"])
            self.assertIn("mass", persona["region"])

    def test_the_centroid_is_reported_in_raw_units_a_person_can_read(self):
        out = workflow.profile_personas(
            self._setup()[0], self._setup()[1], self._setup()[2], self.COLUMNS,
            *self._setup()[3:])
        high = next(p for p in out["personas"] if p["persona"] == "all_high")
        # 0.875 normalized on a 1-9 scale is 8.0 of 9 -- what a human reads.
        self.assertEqual(high["centroid"]["raw"]["aroma"], "8.0 of 9")
        self.assertEqual(high["centroid"]["raw"]["crisp"], "3.1 of 5")
        self.assertAlmostEqual(high["centroid"]["normalized"]["aroma"], 0.875, places=3)

    def test_a_missing_design_range_yields_no_raw_reading_rather_than_a_wrong_one(self):
        X, labels, keys, assignment, sections, overlap, fits, demographics = self._setup()
        columns = [{"label": "aroma"}, {"label": "crisp"}]
        out = workflow.profile_personas(
            X, labels, keys, columns, assignment, sections, overlap, fits, demographics)
        self.assertIsNone(out["personas"][0]["centroid"]["raw"]["aroma"])


@unittest.skipUnless(_db_available(), "local sample database not reachable")
class ClusteringLiveRegressionTests(unittest.TestCase):
    """The measured cluster sizes -- the baseline every later number rests on."""

    ATTRIBUTES = (
        "8397f6e4-7d8d-44b4-8623-3a379df0b38c", "4e40f7f4-8662-47f1-b94e-11da2fcf1865",
        "9b4547dd-c5d3-437f-ba59-b2f870e485e6", "ffe5b05d-0402-4986-8ecd-bf2de5e5f384",
        "bf89c26a-727f-426f-bb3a-c0788b728f80", "60652a5e-82c1-4493-b6cd-453d38099790",
        "6c3268c0-ae29-4a19-9ed6-0f7de4e46e65",
    )
    SEED = 20260819

    @classmethod
    def setUpClass(cls):
        connect = lambda: agent._sql_engine().connect()  # noqa: E731
        extracted = workflow.extract_dataset(
            connect, "e14528a9-01ac-4dff-844e-0914dbdb3759",
            [{"question_id": q, "role": "attribute"} for q in cls.ATTRIBUTES],
        )
        metadata = workflow.fetch_scale_metadata(connect, list(cls.ATTRIBUTES))
        # Design-range only: the measured baseline was taken pre-z-score, so clustering on the
        # z-scored matrix would silently invalidate every figure below.
        result = workflow.normalize_dataset(
            extracted["rows"], metadata, normalization="design_range"
        )
        cls.X = result["design_range_matrix"]
        cls.columns = result["columns"]
        cls.keys = result["grain_keys"]

    def test_measured_cluster_sizes_are_reproduced_at_both_k(self):
        for k, expected in ((4, [455, 355, 160, 230]), (5, [81, 317, 412, 171, 219])):
            with self.subTest(k=k):
                labels = workflow.kmeans(self.X, k, random_state=self.SEED)
                self.assertEqual(np.bincount(labels, minlength=k).tolist(), expected)

    def test_the_defect_3_fix_did_not_change_the_result(self):
        # The reference is kept verbatim in the repo precisely so this comparison is possible.
        import persona_clustering_reference as reference
        for k in (4, 5):
            with self.subTest(k=k):
                self.assertTrue(np.array_equal(
                    workflow.kmeans(self.X, k, random_state=self.SEED),
                    reference.kmeans_clustering(self.X, k, random_state=self.SEED),
                ))

    def test_tercile_specs_reproduce_the_measured_box_masses(self):
        # §20 Step 5: masses ~0.266 / 0.155 / 0.226 on all 7 dimensions.
        labels = [column["label"] for column in self.columns]
        specs = {
            "all_low": {label: [None, 1 / 3] for label in labels},
            "middle": {label: [1 / 3, 2 / 3] for label in labels},
            "all_high": {label: [2 / 3, None] for label in labels},
        }
        out = workflow.resolve_persona_sections(specs, self.X, self.columns)
        masses = {entry["persona"]: entry["mass"] for entry in out["report"]}
        self.assertAlmostEqual(masses["all_high"], 0.2658, places=4)
        self.assertAlmostEqual(masses["middle"], 0.1550, places=4)
        self.assertAlmostEqual(masses["all_low"], 0.2258, places=4)
        # No region is empty at terciles -- the property that makes this domain usable, where
        # z-score bounds produced regions empty by construction.
        self.assertFalse(any(entry["empty"] for entry in out["report"]))

    def test_the_crispiness_atom_makes_its_tercile_boundary_unattainable(self):
        labels = [column["label"] for column in self.columns]
        out = workflow.resolve_persona_sections(
            {"all_high": {label: [2 / 3, None] for label in labels}}, self.X, self.columns
        )
        entry = out["report"][0]
        jar = entry["resolved"]["Crispy/Crunchy JAR"]["lower"]
        self.assertEqual(jar["requested_quantile"], 0.6667)
        # 95.4% of respondents sit on one value, so the cut lands at 0.9725 instead.
        self.assertEqual(jar["achieved_mass"], 0.9725)
        self.assertTrue(any("Crispy/Crunchy JAR" in d for d in entry["divergences"]))
        # A well-spread attribute does not diverge.
        aroma = entry["resolved"]["Aroma Liking"]["lower"]
        self.assertAlmostEqual(aroma["achieved_mass"], 0.7375, places=4)

    def test_tercile_overlaps_and_the_known_collapse_are_reproduced(self):
        # §20 Step 5: overlap ~[0.409, 0.825, 0.856, 0.396], and §13.2's observed collapse where
        # clusters 2 AND 3 both map to all_low.
        labels = workflow.kmeans(self.X, 4, random_state=self.SEED)
        column_labels = [column["label"] for column in self.columns]
        specs = {
            "all_low": {label: [None, 1 / 3] for label in column_labels},
            "middle": {label: [1 / 3, 2 / 3] for label in column_labels},
            "all_high": {label: [2 / 3, None] for label in column_labels},
        }
        sections = workflow.resolve_persona_sections(specs, self.X, self.columns)["sections"]
        out = workflow.overlap_analysis(self.X, labels, sections)

        best = [row["best_overlap"] for row in out["overlap_matrix"]]
        for actual, expected in zip(best, [0.4088, 0.8254, 0.8562, 0.3957], strict=True):
            self.assertAlmostEqual(actual, expected, places=4)

        self.assertEqual(out["assignment"],
                         {0: "middle", 1: "all_high", 2: "all_low", 3: "all_low"})
        self.assertEqual(out["unassigned"], [])
        self.assertEqual(len(out["many_to_one"]), 1)
        collapse = out["many_to_one"][0]
        self.assertEqual(collapse["persona"], "all_low")
        self.assertEqual(collapse["clusters"], [2, 3])
        self.assertEqual(collapse["n"], 390)
        self.assertEqual(out["withhold_alignment"], ["all_low"])
        self.assertEqual(out["distinct_personas"], 3)

        # §17's example values for this persona, reproduced exactly.
        centroids = workflow.persona_centroids(
            self.X, labels, out["assignment"], self.columns
        )
        all_low = next(c for c in centroids if c["persona"] == "all_low")
        self.assertEqual(all_low["n"], 390)
        self.assertEqual(all_low["share"], 0.325)
        self.assertEqual(all_low["clusters"], [2, 3])
        self.assertTrue(all_low["spans_multiple_clusters"])

    def test_plsr_reproduces_every_r2_in_the_measured_table(self):
        """§11.2's table, all eight R² figures, at both K and both grains.

        Run with `Never` INCLUDED as the top rank, which is the convention those figures were
        measured under. The pipeline itself EXCLUDES `Never` -- a non-consumer is not the
        least-frequent purchaser -- so this exists purely to prove the PLSR implementation matches
        the analyst's, independently of that modelling choice.
        """
        connect = lambda: agent._sql_engine().connect()  # noqa: E731
        extracted = workflow.extract_dataset(
            connect, "e14528a9-01ac-4dff-844e-0914dbdb3759",
            [{"question_id": q, "role": "attribute"} for q in self.ATTRIBUTES]
            + [{"question_id": "adf03347-4cc9-4e2b-82fb-ef3bc0985174", "role": "kpi"}],
        )
        sequence = [
            "Once a day", "Once a week", "Once a month", "Once every two months",
            "Once every three months", "Once every six months",
            "Less often than six months", "Never",
        ]
        raw = {
            (row["enrollment_id"], row["product_id"] or ""): sequence.index(row["value"])
            for row in extracted["rows"] if row["role"] == "kpi"
        }
        y = np.array([raw[key] / 7 for key in
                      workflow.normalize_dataset(
                          extracted["rows"],
                          workflow.fetch_scale_metadata(connect, list(self.ATTRIBUTES)),
                          normalization="design_range")["grain_keys"]])
        products = np.array([
            key[1] for key in workflow.normalize_dataset(
                extracted["rows"],
                workflow.fetch_scale_metadata(connect, list(self.ATTRIBUTES)),
                normalization="design_range")["grain_keys"]
        ])

        expectations = {
            4: {"centroid": [0.9665, 0.9981, 1.0000], "cluster_product": [0.8855, 0.9169], "n": 16},
            5: {"centroid": [0.9447, 0.9998, 1.0000], "cluster_product": [0.8237, 0.9004], "n": 20},
        }
        for k, expected in expectations.items():
            with self.subTest(k=k):
                labels = workflow.kmeans(self.X, k, random_state=self.SEED)
                centroids = np.vstack([self.X[labels == c].mean(axis=0) for c in range(k)])
                centroid_y = np.array([y[labels == c].mean() for c in range(k)])
                for index, components in enumerate((1, 2, k - 1)):
                    self.assertAlmostEqual(
                        workflow.plsr_fit(centroids, centroid_y, components)["r2"],
                        expected["centroid"][index], places=4,
                    )
                # The centroid fit saturates to exactly 1.0000 at K-1: it counts components.
                self.assertEqual(
                    workflow.plsr_fit(centroids, centroid_y, k - 1)["r2"], 1.0
                )

                grid_x, grid_y = [], []
                for cluster in range(k):
                    for product in sorted(set(products)):
                        mask = (labels == cluster) & (products == product)
                        if mask.sum():
                            grid_x.append(self.X[mask].mean(axis=0))
                            grid_y.append(y[mask].mean())
                grid_x, grid_y = np.array(grid_x), np.array(grid_y)
                self.assertEqual(len(grid_y), expected["n"])
                for index, components in enumerate((1, 2)):
                    self.assertAlmostEqual(
                        workflow.plsr_fit(grid_x, grid_y, components)["r2"],
                        expected["cluster_product"][index], places=4,
                    )
                # And it does NOT saturate, which is why it is the quotable R².
                self.assertLess(workflow.plsr_fit(grid_x, grid_y, 2)["r2"], 1.0)

    def test_q2_is_far_below_r2_on_the_fixture(self):
        # The substantive finding §11.2 draws from Q²: the relationship is real but much weaker
        # than R² implies. Asserted as the inequality rather than an exact value, because the
        # analyst's leave-one-out bookkeeping differs from this one by ~0.01.
        labels = workflow.kmeans(self.X, 4, random_state=self.SEED)
        rng = np.random.default_rng(0)
        centroid_y = np.array([0.297, 0.247, 0.427, 0.320])
        centroids = np.vstack([self.X[labels == c].mean(axis=0) for c in range(4)])
        r2 = workflow.plsr_fit(centroids, centroid_y, 1)["r2"]
        q2 = workflow.loo_q2(centroids, centroid_y, 1)
        self.assertGreater(r2, 0.9)
        self.assertLess(q2, 0.6)
        self.assertLess(q2, r2 / 2)

    def test_the_scope_guard_authorizes_questions_not_the_survey(self):
        """The real guard, unmocked -- because mocking it hid a bug that blocked the whole tool.

        `attribute_kpi_analysis` was calling _authorize_question_ids(question_ids + [survey_id]).
        That helper resolves QUESTION ids, so a survey id is always unknown to it and every call
        was refused with "unknown question UUID(s)" naming the survey. Every unit test patched the
        helper to return ([], []), so the argument error was invisible until an end-to-end run.
        """
        with patch.dict(agent._SCOPE, {
            "survey_id": "e14528a9-01ac-4dff-844e-0914dbdb3759",
            "organization_id": "17b6da66-3c2e-42f1-bb64-2a09bdfbc183",
            "client_id": "f6b05cb4-cea2-4855-816e-c92e5e5d22ff",
        }, clear=False):
            unknown, outside = agent._authorize_question_ids(list(self.ATTRIBUTES))
            self.assertEqual((unknown, outside), ([], []))
            # A survey id passed as a question id is unknown -- the shape of the original bug.
            unknown_with_survey, _ = agent._authorize_question_ids(
                list(self.ATTRIBUTES) + ["e14528a9-01ac-4dff-844e-0914dbdb3759"])
            self.assertEqual(unknown_with_survey, ["e14528a9-01ac-4dff-844e-0914dbdb3759"])

        # And the tool itself reaches the workflow with the real guard in place.
        with patch.dict(agent._SCOPE, {
            "survey_id": "e14528a9-01ac-4dff-844e-0914dbdb3759",
            "organization_id": "17b6da66-3c2e-42f1-bb64-2a09bdfbc183",
            "client_id": "f6b05cb4-cea2-4855-816e-c92e5e5d22ff",
        }, clear=False), patch.object(agent, "_scope_published", return_value=True):
            result = agent.attribute_kpi_analysis.invoke({
                "survey_id": "e14528a9-01ac-4dff-844e-0914dbdb3759",
                "attributes": [{"question_id": q, "role": "attribute"}
                               for q in self.ATTRIBUTES[:3]],
                "kpis": [{"question_id": "adf03347-4cc9-4e2b-82fb-ef3bc0985174",
                          "role": "kpi"}],
            })
        self.assertNotIn("unknown question UUID", result)
        self.assertNotIn("Persona analysis unavailable", result)
        self.assertTrue(result.startswith(agent._ANALYSIS_TOOL_RESULT_PREFIX))

    def test_the_per_persona_product_figures_reproduce_the_measured_values(self):
        # §11.8's twelve figures: the within-persona in-sample R², the correlation across products,
        # and the prediction error from the global model. Run with `Never` INCLUDED, the convention
        # they were measured under -- the pipeline itself excludes it.
        connect = lambda: agent._sql_engine().connect()  # noqa: E731
        extracted = workflow.extract_dataset(
            connect, "e14528a9-01ac-4dff-844e-0914dbdb3759",
            [{"question_id": q, "role": "attribute"} for q in self.ATTRIBUTES]
            + [{"question_id": "adf03347-4cc9-4e2b-82fb-ef3bc0985174", "role": "kpi"}],
        )
        sequence = [
            "Once a day", "Once a week", "Once a month", "Once every two months",
            "Once every three months", "Once every six months",
            "Less often than six months", "Never",
        ]
        encoding = {
            "encoding": "ordinal", "ordered_categories": sequence,
            "excluded_categories": [], "direction": "higher code means less often",
        }
        metadata = workflow.fetch_scale_metadata(
            connect, list(self.ATTRIBUTES) + ["adf03347-4cc9-4e2b-82fb-ef3bc0985174"])
        values = workflow.kpi_values(
            extracted["rows"], self.keys, "adf03347-4cc9-4e2b-82fb-ef3bc0985174",
            encoding, metadata.get("adf03347-4cc9-4e2b-82fb-ef3bc0985174"),
        )
        labels = workflow.kmeans(self.X, 4, random_state=self.SEED)
        fit = workflow.fit_persona_kpi(
            self.X, labels, self.keys, self.columns, values,
            "Frequency of Purchase", encoding,
        )
        readouts = {
            entry["cluster"]: entry
            for entry in fit["cluster_product_grain"]["per_persona_products"]
        }
        expected = {
            0: (0.4958, 0.7041, 0.0241),
            1: (0.5090, 0.7135, 0.0116),
            2: (0.8040, 0.8966, 0.0236),
            3: (0.9106, 0.9543, 0.0215),
        }
        for cluster, (r2, correlation, error) in expected.items():
            with self.subTest(cluster=cluster):
                entry = readouts[cluster]
                self.assertAlmostEqual(entry["within_persona_fit"]["r2"], r2, places=4)
                self.assertAlmostEqual(
                    entry["correlation_across_products"]["pearson"], correlation, places=4)
                self.assertAlmostEqual(
                    entry["prediction_error"]["mean_absolute"], error, places=4)
                self.assertEqual(entry["n_products"], 4)
                # Never computed at 4 products: it was measured negative for every persona.
                self.assertIsNone(entry["per_persona_q2"])
                self.assertIn("n_products too small", entry["per_persona_q2_reason"])

        # The measurement behind that refusal, on the real KPI: leave-one-out inside a persona
        # trains on 3 points and predicts WORSE than the persona's own mean. Shown rather than
        # asserted about, because it is the whole reason the figure is withheld.
        products = sorted({key[1] for key in values["keys"]})
        position = {key: index for index, key in enumerate(self.keys)}
        index = np.array([position[key] for key in values["keys"]])
        row_labels = labels[index]
        for cluster in range(4):
            cells, cell_y = [], []
            for product in products:
                mask = (row_labels == cluster) & np.array(
                    [key[1] == product for key in values["keys"]])
                if mask.sum():
                    cells.append(self.X[index][mask].mean(axis=0))
                    cell_y.append(float(values["values"][mask].mean()))
            with self.subTest(per_persona_q2_for=cluster):
                self.assertLess(workflow.loo_q2(np.array(cells), np.array(cell_y), 1), 0.0)

    def test_the_tasting_survey_is_disjoint_and_fabricates_no_demographics(self):
        # The measured case: 1,200 attribute enrollments and 1,200 screener enrollments with ZERO
        # overlap, and no fallback key -- user_id and panelist_id are null for all 2,400. Joining
        # the other set would describe different people.
        connect = lambda: agent._sql_engine().connect()  # noqa: E731
        labels = workflow.kmeans(self.X, 4, random_state=self.SEED)
        column_labels = [column["label"] for column in self.columns]
        resolved = workflow.resolve_persona_sections(
            {"all_low": {label: [None, 1 / 3] for label in column_labels},
             "all_high": {label: [2 / 3, None] for label in column_labels}},
            self.X, self.columns,
        )
        overlap = workflow.overlap_analysis(self.X, labels, resolved["sections"])
        demographics = workflow.fetch_demographics(
            connect, "e14528a9-01ac-4dff-844e-0914dbdb3759")
        profile = workflow.profile_personas(
            self.X, labels, self.keys, self.columns, overlap["assignment"],
            resolved["report"], overlap["overlap_matrix"], [], demographics,
        )
        verdict = profile["joinability"]
        self.assertEqual(verdict["joinability"], "disjoint")
        self.assertEqual(verdict["analysis_enrollments"], 1200)
        self.assertEqual(verdict["demographic_enrollments"], 1200)
        self.assertEqual(verdict["joined_enrollments"], 0)
        for persona in profile["personas"]:
            self.assertIsNone(persona["demographics"])
            self.assertEqual(persona["joinability"], "disjoint")
        # Raw units are present regardless, since they come from the attributes themselves.
        raw = profile["personas"][0]["centroid"]["raw"]
        self.assertTrue(any(value and " of " in value for value in raw.values()))

    def test_the_reference_would_have_misassigned_where_this_reports_unassigned(self):
        # DEFECT-1, shown against the code it was found in rather than only asserted about.
        import persona_clustering_reference as reference
        labels = workflow.kmeans(self.X, 4, random_state=self.SEED)
        width = self.X.shape[1]
        empty = {
            "p_first": (np.full(width, 1.5), np.full(width, 2.0)),
            "p_second": (np.full(width, 3.0), np.full(width, 4.0)),
        }
        broken = reference.maximum_overlap_analysis(self.X, labels, empty)
        self.assertTrue((broken["overlap_matrix"] == 0).all())
        # Every cluster silently labelled with the first persona.
        self.assertEqual(set(broken["cluster_to_persona"].values()), {"p_first"})

        fixed = workflow.overlap_analysis(self.X, labels, empty)
        self.assertEqual(set(fixed["assignment"].values()), {"unassigned"})
        self.assertEqual(len(fixed["unassigned"]), 4)

    def test_the_fixture_is_level_dominated_so_type_names_are_unsupported(self):
        # §12: the dominant variance is a general liking factor. Clusters differ in DEGREE, so
        # "spicy lover vs sweet lover" names would describe structure that is not there.
        verdict = workflow.level_vs_shape(self.X)
        self.assertEqual(verdict["verdict"], "level-dominated")
        self.assertGreaterEqual(verdict["pc1_variance_share"], 0.5)
        self.assertIn("do not invent type-based names", verdict["naming_guidance"])


@unittest.skipUnless(_db_available(), "local sample database not reachable")
class StageALiveFixtureTests(unittest.TestCase):
    """Stage A against the real fixture, reproducing §20 Step 3's measured expectations."""

    SURVEY = "e14528a9-01ac-4dff-844e-0914dbdb3759"
    ATTRIBUTES = (
        "8397f6e4-7d8d-44b4-8623-3a379df0b38c", "4e40f7f4-8662-47f1-b94e-11da2fcf1865",
        "9b4547dd-c5d3-437f-ba59-b2f870e485e6", "ffe5b05d-0402-4986-8ecd-bf2de5e5f384",
        "bf89c26a-727f-426f-bb3a-c0788b728f80", "60652a5e-82c1-4493-b6cd-453d38099790",
        "6c3268c0-ae29-4a19-9ed6-0f7de4e46e65",
    )
    KPI = "adf03347-4cc9-4e2b-82fb-ef3bc0985174"

    @classmethod
    def setUpClass(cls):
        cls.report = workflow.extract_dataset(
            connect=lambda: agent._sql_engine().connect(),
            survey_id=cls.SURVEY,
            variables=[{"question_id": q, "role": "attribute"} for q in cls.ATTRIBUTES]
                      + [{"question_id": cls.KPI, "role": "kpi"}],
        )

    def test_fixture_extracts_1200_complete_rows_with_no_partials(self):
        keys = {}
        for row in self.report["rows"]:
            if row["role"] == "attribute":
                keys.setdefault((row["enrollment_id"], row["product_id"]), set()).add(
                    row["question_id"]
                )
        complete = [k for k, v in keys.items() if len(v) == len(self.ATTRIBUTES)]
        self.assertEqual(len(keys), 1200)
        self.assertEqual(len(complete), 1200)
        self.assertEqual(len(keys) - len(complete), 0)

    def test_grain_is_reported_as_single_product(self):
        grain = self.report["grain"]
        self.assertEqual(grain["regime"], "single_product")
        self.assertEqual(grain["max_products_per_enrollment"], 1)
        self.assertEqual(grain["respondents"], 1200)
        self.assertEqual(grain["products"], 4)

    def test_fixture_has_no_invalid_values_unsupported_or_duplicates(self):
        self.assertEqual(self.report["invalid_values"], [])
        self.assertEqual(self.report["unsupported"], [])
        self.assertEqual(self.report["missing_variables"], [])
        self.assertEqual(self.report["duplicate_observations"], [])

    def test_categorical_kpi_resolves_to_labels(self):
        kpi_rows = [r for r in self.report["rows"] if r["role"] == "kpi"]
        self.assertEqual(len(kpi_rows), 1200)
        self.assertTrue(all(r["kind"] == "category" for r in kpi_rows))
        self.assertIn("Never", {r["value"] for r in kpi_rows})

    def test_the_encoding_turn_is_shown_configured_labels_and_observed_counts(self):
        connect = lambda: agent._sql_engine().connect()  # noqa: E731 - one-line factory
        variables = [
            {"question_id": self.KPI, "role": "kpi"},
            {"question_id": "60652a5e-82c1-4493-b6cd-453d38099790", "role": "attribute"},
            {"question_id": "08e899de-1e57-4dca-aeb5-3b1f4dc73ca0", "role": "screener"},
        ]
        inputs = workflow.build_encoding_inputs(connect, variables, self.report["rows"])
        by_id = {entry["question_id"]: entry for entry in inputs}

        kpi = by_id[self.KPI]
        self.assertEqual(len(kpi["labels"]), 8)
        self.assertEqual(kpi["labels"][0], "Once a day")
        self.assertEqual(kpi["labels"][-1], "Never")
        # Counts come from the extracted dataset, so they describe what is being analysed.
        self.assertEqual(kpi["respondents_per_label"]["Never"], 13)
        self.assertEqual(sum(kpi["respondents_per_label"].values()), 1200)

        # The JAR attribute has no category labels; its anchors and range are what identify it.
        jar = by_id["60652a5e-82c1-4493-b6cd-453d38099790"]
        self.assertEqual(jar["labels"], [])
        self.assertEqual(jar["anchors"],
                         ["Not crispy/crunchy enough", "Much too crispy/crunchy"])
        self.assertEqual(jar["configured_range"], [1.0, 5.0])

        # A variable absent from the dataset reports NO counts rather than a table of zeros --
        # all-zeros would read as "nobody chose any of these".
        gender = by_id["08e899de-1e57-4dca-aeb5-3b1f4dc73ca0"]
        self.assertEqual(len(gender["labels"]), 4)
        self.assertIsNone(gender["respondents_per_label"])

        # The stored codes are absent from every entry, by construction.
        rendered = json.dumps(inputs)
        for banned in ("analytical_value", '"order"'):
            self.assertNotIn(banned, rendered)

    def test_exclusion_accounting_reports_the_respondent_count_it_removes(self):
        encodings = {self.KPI: {"encoding": "ordinal", "excluded_categories": ["Never"]}}
        report = workflow.excluded_category_counts(encodings, self.report["rows"])
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["label"], "Never")
        self.assertEqual(report[0]["n"], 13)
        self.assertIn("not a point on the scale", report[0]["reason"])

    def test_the_corrupt_analytical_values_are_reported_invalid_not_clamped(self):
        # The one matrix question DB-wide whose answers reference the corrupt options.
        report = workflow.extract_dataset(
            connect=lambda: agent._sql_engine().connect(),
            survey_id="b4bc2e7d-84c4-4eb1-a24f-70345f19aadc",
            variables=[{"question_id": "1e00ef48-0d95-42ba-a281-b53d8990979c",
                        "role": "attribute"}],
        )
        reasons = [b["reason"] for b in report["invalid_values"]]
        corrupt = [r for r in reasons if "not clamped" in r]
        self.assertEqual(len(corrupt), 3)
        self.assertTrue(any("7777" in r for r in corrupt))
        self.assertTrue(any("999" in r for r in corrupt))
        # Nothing out of range survived into the data.
        numeric = [r["value"] for r in report["rows"] if isinstance(r["value"], float)]
        self.assertTrue(numeric)
        self.assertTrue(all(abs(v) < 100 for v in numeric))

    def test_normalization_reproduces_the_measured_design_range_figures(self):
        # §10 B and §21's measured expectations, to 4 decimal places. These are the numbers the
        # whole normalization argument rests on: design-range scaling alone leaves CRISPINESS at
        # a sixth of AFTERTASTE's sd, which is why z-scoring is not cosmetic.
        metadata = workflow.fetch_scale_metadata(
            lambda: agent._sql_engine().connect(), list(self.ATTRIBUTES) + [self.KPI]
        )
        result = workflow.normalize_dataset(self.report["rows"], metadata)
        self.assertEqual(len(result["grain_keys"]), 1200)
        self.assertEqual(result["excluded"]["incomplete_keys"], 0)

        by_label = {c["label"]: c for c in result["columns"]}
        expected = {
            "Crispy/Crunchy JAR": (1, 5, 0.2515, 0.5029, 0.0629, 0.9542, True),
            "Freshness Rating": (1, 5, 0.5737, 0.9225, 0.1434, 0.7400, False),
            "Flavor Liking": (1, 9, 0.9526, 0.8875, 0.1191, 0.3975, False),
            "Texture Liking": (1, 9, 0.9754, 0.8728, 0.1219, 0.4400, False),
            "Appearance Liking": (1, 9, 0.9790, 0.8701, 0.1224, 0.4308, False),
            "Aroma Liking": (1, 9, 1.2122, 0.8302, 0.1515, 0.3717, False),
            "Aftertaste Liking": (1, 9, 1.2368, 0.8119, 0.1546, 0.3533, False),
        }
        self.assertEqual(set(by_label), set(expected))
        for label, (lo, hi, raw_sd, mean, sd, modal, near) in expected.items():
            with self.subTest(attribute=label):
                column = by_label[label]
                self.assertEqual(column["design_range"], (float(lo), float(hi)))
                self.assertAlmostEqual(column["raw_sd"], raw_sd, places=4)
                self.assertAlmostEqual(column["normalized_mean"], mean, places=4)
                self.assertAlmostEqual(column["normalized_sd"], sd, places=4)
                self.assertAlmostEqual(column["modal_share"], modal, places=4)
                self.assertIs(column["near_constant"], near)

        # Exactly one attribute is near-constant, and it is the JAR scale that is 95.4% one value.
        flagged = [c["label"] for c in result["columns"] if c["near_constant"]]
        self.assertEqual(flagged, ["Crispy/Crunchy JAR"])
        # The 1-9 and 1-5 attributes now share a range.
        scaled = result["design_range_matrix"]
        self.assertGreaterEqual(scaled.min(), 0.0)
        self.assertLessEqual(scaled.max(), 1.0)
        # And z-scoring equalized the pull that design-range scaling left uneven.
        self.assertTrue(np.allclose(result["zscored_matrix"].std(axis=0, ddof=1), 1.0))

    def test_the_full_run_reports_grain_and_normalization_truthfully(self):
        # Asserted on the DETERMINISTIC report, which is both the fallback and the thing the model
        # turn is checked against. Forced here by making the report turn unavailable, so this test
        # keeps testing what it is about -- whether the run describes itself truthfully.
        with patch.object(workflow, "compose_report",
                          side_effect=RuntimeError("report turn disabled for this test")):
            run = workflow.run_attribute_kpi_analysis(
                survey_id=self.SURVEY,
                attributes=[{"question_id": q, "role": "attribute"}
                            for q in self.ATTRIBUTES],
                kpis=[{"question_id": self.KPI, "role": "kpi"}],
                connect=lambda: agent._sql_engine().connect(),
                model_name="gpt-test",
                model=_CompliantWorkflowModel(),
            )
        self.assertEqual(run.failed_stage, "")
        self.assertIn("grain=single_product", run.text)
        self.assertIn("1200 respondents", run.text)
        self.assertIn("normalization=design_range_zscore", run.text)
        self.assertIn("near-constant: Crispy/Crunchy JAR", run.text)
        # Every stage now runs, so nothing is a placeholder and nothing is owed. Asserted on the
        # stage names rather than a count, so landing another stage does not break this test.
        self.assertIn("stages implemented: A (extract survey responses)", run.text)
        self.assertIn("L (PLSR), M/N (fit and correlation)", run.text)
        self.assertIn("stages still placeholders: none", run.text)
        self.assertIn("STILL MISSING: nothing", run.text)
        # No row-level or respondent-level data crossed back. Question ids DO appear, and must:
        # each encoding decision names the variable it applies to, and the caller supplied those
        # ids in the first place. What may never appear is anyone's data.
        for marker in ("enrollment_id", "0.5029", "product_id"):
            self.assertNotIn(marker, run.text)
        self.assertIn("encoding 8397f6e4-7d8d-44b4-8623-3a379df0b38c", run.text)

    def test_no_matrix_row_data_reaches_a_transcript(self):
        # §20 Step 3: "no matrix appears in any transcript". Structural -- stage rows are parked
        # and only an id travels, so the run's messages cannot carry them.
        run = workflow.run_attribute_kpi_analysis(
            survey_id="b4bc2e7d-84c4-4eb1-a24f-70345f19aadc",
            attributes=[{"question_id": "1e00ef48-0d95-42ba-a281-b53d8990979c",
                         "role": "attribute"}],
            kpis=[{"question_id": "1e00ef48-0d95-42ba-a281-b53d8990979c", "role": "kpi"}],
            connect=lambda: agent._sql_engine().connect(),
            model_name="gpt-test",
            model=_CompliantWorkflowModel(),
        )
        for marker in ("matrix_row_option_id", "component_id", "enrollment_id", "7777"):
            self.assertNotIn(marker, run.text)


class _FixedTool:
    """A stand-in tool returning a prepared string, so no LLM or DB is touched."""

    def __init__(self, name, result):
        self.name = name
        self._result = result

    def invoke(self, _args):
        return self._result


class _RosterProbe:
    """A stand-in bound model that records which roster call_model reached for."""

    def __init__(self, tag, sink):
        self._tag = tag
        self._sink = sink

    def invoke(self, _messages, config=None, **_kwargs):
        self._sink.append(self._tag)
        return AIMessage(content="ok")


_REPO_ROOT = pathlib.Path(agent.__file__).resolve().parent
_DEPLOYMENT = _REPO_ROOT / "deployment"


@unittest.skipUnless(_DEPLOYMENT.is_dir(), "deployment/ mirror not present")
class DeploymentParityTests(unittest.TestCase):
    """The root/deployment mirror contract (§20 Step 8).

    Replaces a manual diff. Two different rules apply and conflating them is how a mirror breaks:
    the instruction and library modules must be byte-identical, while funda_agent_exp.py diverges
    on purpose and must diverge ONLY in the four known ways.
    """

    # Byte-identical: every module that carries model-facing text or shared logic. A drift here
    # means production is running different instructions than the ones tested in this suite.
    IDENTICAL = (
        "agent_instructions.py",
        "tool_prompts.py",
        "output_store.py",
        "isolated_workflow_instructions.py",
        "attribute_kpi_workflow.py",
    )

    def test_shared_modules_are_byte_identical(self):
        for name in self.IDENTICAL:
            with self.subTest(module=name):
                mirrored = _DEPLOYMENT / name
                self.assertTrue(mirrored.is_file(), f"{name} is missing from deployment/")
                self.assertEqual(
                    (_REPO_ROOT / name).read_bytes(), mirrored.read_bytes(),
                    f"{name} differs between root and deployment/",
                )

    @staticmethod
    def _module_symbols(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        return names

    def test_the_agent_module_diverges_only_in_the_four_known_ways(self):
        """funda_agent_exp.py is allowed to differ, but only where it is meant to.

        root-only: the BE/.env probe, which exists so the CLI can find a local database.
        deployment-only: _required_env, which refuses to start without credentials, and
        DB_CONNECT_TIMEOUT_S, which bounds a production connection attempt.

        Anything else appearing here means a feature landed in one copy and not the other.
        """
        root = self._module_symbols(_REPO_ROOT / "funda_agent_exp.py")
        mirrored = self._module_symbols(_DEPLOYMENT / "funda_agent_exp.py")
        self.assertEqual(sorted(root - mirrored), ["_BE_ENV", "_BE_ENV_VALUES"])
        self.assertEqual(sorted(mirrored - root), ["DB_CONNECT_TIMEOUT_S", "_required_env"])

    def test_the_persona_feature_reached_the_mirror(self):
        # The specific thing this sync was for: if the workflow is missing from deployment, the
        # tool exists in the roster and every call fails at import time.
        mirrored = self._module_symbols(_DEPLOYMENT / "funda_agent_exp.py")
        for symbol in ("attribute_kpi_analysis", "is_persona_cluster_request",
                       "is_persona_profile_request", "persona_profile_block",
                       "_isolated_workflow_llm", "_result_reader_model"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, mirrored)
        source = (_DEPLOYMENT / "funda_agent_exp.py").read_text(encoding="utf-8")
        self.assertIn("from attribute_kpi_workflow import", source)
        self.assertIn("PERSONA_CLUSTER_DIRECTIVE", source)

    def test_the_mirror_carries_no_source_code_credentials(self):
        """The rule that decides the sync direction, so it is enforced rather than remembered.

        The root copy keeps local-dev fallbacks for the OpenAI key and the charts secret. Copying
        root over deployment would publish both into the deployable repo, which is why changes are
        ported in and this test exists to catch the shortcut.
        """
        for path in sorted(_DEPLOYMENT.glob("*.py")):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("sk-proj-", source)
                self.assertNotIn("EkLwAyEKSCE", source)
        # And credentials are required rather than defaulted.
        agent_source = (_DEPLOYMENT / "funda_agent_exp.py").read_text(encoding="utf-8")
        self.assertIn('OPENAI_API_KEY = _required_env("OPENAI_API_KEY")', agent_source)
        self.assertIn('CHARTS_STATS_EXTERNAL_ACCESS_SECRET = _required_env(', agent_source)
        self.assertNotIn('os.getenv("OPENAI_API_KEY", "sk-', agent_source)


if __name__ == "__main__":
    unittest.main()
