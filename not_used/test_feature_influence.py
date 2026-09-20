from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

import funda_agent_exp as agent
from agent_instructions import AGENT_BACKSTORY
from feature_influence import _group_folds, feature_influence, holm_adjust
from scripts.check_suggestions import validate as validate_suggestions


class FeatureInfluenceMathTests(unittest.TestCase):
    @staticmethod
    def _significant(report, info, alpha=0.05):
        if info["global_raw_p_value"] > alpha:
            return set()
        adjusted = holm_adjust([row["raw_p_value"] for row in report])
        return {
            row["feature"] for row, p_value in zip(report, adjusted, strict=True)
            if p_value <= alpha
        }

    def test_holm_adjustment_matches_known_values(self):
        self.assertEqual(
            [round(value, 3) for value in holm_adjust([0.01, 0.04, 0.03, 0.20])],
            [0.04, 0.09, 0.09, 0.20],
        )

    def test_returns_all_and_only_qualifying_independent_features(self):
        rng = np.random.default_rng(12)
        x = rng.normal(size=(160, 4))
        y = 0.9 * x[:, 0] - 0.55 * x[:, 1] + rng.normal(scale=0.35, size=160)
        report, info, groups = feature_influence(
            x, y, groups=np.arange(len(y)),
            feature_names=["aroma", "flavor", "texture", "appearance"],
            n_boot=100, n_resamples=199,
        )
        influential = self._significant(report, info)
        self.assertEqual(influential, {"aroma", "flavor"})
        self.assertEqual(groups, [])
        self.assertEqual(info["n_used"], 160)
        flavor = next(row for row in report if row["feature"] == "flavor")
        self.assertEqual(flavor["direction"], "negative")

    def test_null_features_honestly_return_none(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=(180, 3))
        y = rng.normal(size=180)
        report, _info, groups = feature_influence(
            x, y, groups=np.arange(len(y)), feature_names=["a", "b", "c"],
            n_boot=100, n_resamples=199,
        )
        self.assertEqual(self._significant(report, _info), set())
        self.assertFalse(any(group["jointly_influential"] for group in groups))

    def test_correlated_signal_can_be_global_without_individual_attribution(self):
        rng = np.random.default_rng(8)
        signal = rng.normal(size=150)
        x = np.column_stack([
            signal,
            signal + rng.normal(scale=0.01, size=150),
            rng.normal(size=150),
        ])
        y = signal + rng.normal(scale=0.3, size=150)
        report, info, groups = feature_influence(
            x, y, groups=np.arange(len(y)), feature_names=["aroma", "flavor", "texture"],
            n_boot=100, n_resamples=199,
        )
        self.assertLessEqual(info["global_raw_p_value"], 0.05)
        self.assertEqual(self._significant(report, info), set())
        self.assertTrue(any(set(group["features"]) == {"aroma", "flavor"} for group in groups))

    def test_group_folds_do_not_leak_respondents(self):
        respondents = np.repeat(np.arange(24), 3)
        for train, validation in _group_folds(respondents, 5, 0):
            self.assertTrue(
                set(respondents[train]).isdisjoint(set(respondents[validation]))
            )

    def test_threaded_and_serial_results_are_deterministic(self):
        rng = np.random.default_rng(22)
        x = rng.normal(size=(100, 3))
        y = 0.7 * x[:, 0] + rng.normal(scale=0.4, size=100)
        kwargs = dict(
            groups=np.arange(len(y)), feature_names=["a", "b", "c"],
            n_boot=100, n_resamples=99,
        )
        serial = feature_influence(x, y, max_workers=1, **kwargs)
        threaded = feature_influence(x, y, max_workers=4, **kwargs)
        self.assertEqual(serial, threaded)

    def test_product_controls_remove_product_only_signal(self):
        rng = np.random.default_rng(44)
        respondents = np.repeat(np.arange(80), 2)
        products = np.tile(np.array(["A", "B"]), 80)
        product_effect = (products == "B").astype(float)
        x = (product_effect + rng.normal(scale=0.08, size=len(products))).reshape(-1, 1)
        y = product_effect + rng.normal(scale=0.12, size=len(products))
        uncontrolled, _info, _groups = feature_influence(
            x, y, groups=respondents, feature_names=["feature"],
            n_boot=100, n_resamples=199,
        )
        uncontrolled_info = _info
        controlled, controlled_info, _groups = feature_influence(
            x, y, groups=respondents, products=products,
            feature_names=["feature"], n_boot=100, n_resamples=199,
        )
        self.assertEqual(self._significant(uncontrolled, uncontrolled_info), {"feature"})
        self.assertEqual(self._significant(controlled, controlled_info), set())

    def test_original_minimum_and_constant_target_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "at least eight"):
            feature_influence(
                np.ones((7, 1)), np.arange(7), groups=np.arange(7), n_boot=10
            )
        with self.assertRaisesRegex(ValueError, "target has no variation"):
            feature_influence(
                np.arange(8).reshape(-1, 1), np.ones(8), groups=np.arange(8), n_boot=10
            )


def _rows_for_vertical_variables(n: int = 12):
    qids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]
    prompts = ["Overall liking", "Aroma", "Flavor"]
    rows = []
    for slot, (qid, prompt) in enumerate(zip(qids, prompts, strict=True)):
        for respondent in range(n):
            value = respondent if slot != 2 else n - respondent
            rows.append({
                "slot": slot,
                "requested_component": None,
                "question_id": qid,
                "prompt": prompt,
                "question_type": "vertical-rating",
                "survey_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "survey_title": "Test survey",
                "in_scope": True,
                "components": [{"id": f"option-{slot}", "label": ""}],
                "enrollment_id": f"respondent-{respondent}",
                "product_id": None,
                "answer_component_id": f"option-{slot}",
                "raw_value": str(value),
            })
    variables = [agent.InfluenceVariable(question_id=qid) for qid in qids]
    return rows, variables


class FeatureInfluenceDataTests(unittest.TestCase):
    def test_fetch_rejects_different_authorized_surveys_before_response_query(self):
        target_id = "11111111-1111-1111-1111-111111111111"
        feature_id = "22222222-2222-2222-2222-222222222222"
        target_survey = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        feature_survey = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        scope = {
            "survey_id": target_survey,
            "organization_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "client_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "authorized_survey_ids": (target_survey, feature_survey),
        }
        question_result = MagicMock()
        question_result.mappings.return_value.all.return_value = [
            {"question_id": target_id, "survey_id": target_survey},
            {"question_id": feature_id, "survey_id": feature_survey},
        ]
        connection = MagicMock()
        connection.execute.side_effect = [None, None, question_result]
        connection.begin.return_value.__enter__.return_value = connection
        connection.begin.return_value.__exit__.return_value = False
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        engine.connect.return_value.__exit__.return_value = False

        with (
            patch.object(agent, "_sql_engine", return_value=engine),
            patch.object(agent, "_resolve_authorized_scope", return_value=scope),
        ):
            with self.assertRaisesRegex(ValueError, "must belong to the same survey"):
                agent._fetch_influence_rows(
                    agent.InfluenceVariable(question_id=target_id),
                    [agent.InfluenceVariable(question_id=feature_id)],
                )

        self.assertEqual(connection.execute.call_count, 3)

    def test_preparation_aligns_all_variables_without_imputation(self):
        rows, variables = _rows_for_vertical_variables()
        # Remove one feature response: exactly one target entity must be dropped.
        rows = [
            row for row in rows
            if not (row["slot"] == 2 and row["enrollment_id"] == "respondent-0")
        ]
        targets, features, excluded = agent._resolve_influence_specs(
            rows, variables[0], variables[1:]
        )
        x, y, respondent, products, names, provenance = agent._prepare_target_data(
            targets[0], features
        )
        self.assertEqual(len(x), 11)
        self.assertEqual(len(y), 11)
        self.assertEqual(len(respondent), 11)
        self.assertIsNone(products)
        self.assertEqual(names, ["Aroma", "Flavor"])
        self.assertEqual(provenance["rows_removed_by_alignment"], 1)
        self.assertEqual(excluded, [])

    def test_omitted_feature_component_expands_every_line_scale_component(self):
        rows, variables = _rows_for_vertical_variables()
        variables[1] = agent.InfluenceVariable(question_id=variables[1].question_id)
        for row in rows:
            if row["slot"] == 1:
                row["question_type"] = "line-scale"
                row["components"] = [
                    {"id": "aroma", "label": "Aroma"},
                    {"id": "texture", "label": "Texture"},
                ]
                row["answer_component_id"] = (
                    "aroma" if int(row["enrollment_id"].split("-")[-1]) % 2 == 0
                    else "texture"
                )
        _targets, features, _excluded = agent._resolve_influence_specs(
            rows, variables[0], variables[1:]
        )
        self.assertEqual(
            [feature["name"] for feature in features],
            ["Aroma — Aroma", "Aroma — Texture", "Flavor"],
        )

    def test_mixed_grain_is_rejected(self):
        rows, variables = _rows_for_vertical_variables()
        for row in rows:
            if row["slot"] == 1:
                row["product_id"] = "product-1"
        with self.assertRaisesRegex(ValueError, "different respondent/product grains"):
            agent._resolve_influence_specs(rows, variables[0], variables[1:])

    def test_tool_returns_significant_subset_with_automatic_alpha(self):
        rows, variables = _rows_for_vertical_variables()
        fake_report = [{
            "feature": "Aroma", "raw_p_value": 0.01, "adjusted_p_value": None,
            "partial_r2": 0.4, "significant": False, "influential": False,
            "collinearity_flag": False,
        }, {
            "feature": "Flavor", "raw_p_value": 0.40, "adjusted_p_value": None,
            "partial_r2": 0.01, "significant": False, "influential": False,
            "collinearity_flag": False,
        }]
        fake_info = {
            "n_used": 12, "cv_r2": 0.5,
            "global_raw_p_value": 0.01,
            "global_adjusted_p_value": None,
            "global_significant": False,
            "max_abs_quadratic_residual_corr": 0.1,
        }
        scope = {"survey_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        with (
            patch.dict(agent._SCOPE, scope, clear=True),
            patch.object(agent, "_fetch_influence_rows", return_value=rows),
            patch.object(
                agent, "feature_influence", return_value=(fake_report, fake_info, [])
            ) as calculate,
        ):
            result = agent.analyze_feature_influence.invoke({
                "target": variables[0].model_dump(),
                "features": [variable.model_dump() for variable in variables[1:]],
            })
        self.assertTrue(result.startswith(agent.FEATURE_INFLUENCE_RESULT_HEAD))
        payload = json.loads(result.split("\n", 1)[1])
        self.assertEqual(
            payload["conclusion"]["qualifying_by_target"][0]["independent_features"],
            ["Aroma"],
        )
        self.assertEqual(len(payload["analyses"]), 1)
        self.assertEqual(payload["alpha"], 0.05)
        self.assertEqual(payload["multiple_testing_correction"], "holm_fwer")
        self.assertNotIn("thresholds", payload)
        self.assertEqual(calculate.call_args.kwargs["alpha"], 0.05)
        self.assertEqual(
            calculate.call_args.kwargs["n_resamples"], agent._INFLUENCE_RESAMPLES
        )

    def test_input_defaults_alpha_and_accepts_explicit_alpha(self):
        default = agent.FeatureInfluenceInput.model_validate({
            "target": {"question_id": "11111111-1111-1111-1111-111111111111"},
        })
        explicit = agent.FeatureInfluenceInput.model_validate({
            "target": {"question_id": "11111111-1111-1111-1111-111111111111"},
            "alpha": 0.01,
        })
        self.assertEqual(default.alpha, 0.05)
        self.assertEqual(explicit.alpha, 0.01)

    def test_whole_line_scale_target_runs_every_component_with_one_fetch(self):
        survey_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        target_qid = "11111111-1111-1111-1111-111111111111"
        feature_qid = "22222222-2222-2222-2222-222222222222"
        target_components = [
            "Chewiness", "Creaminess", "Firmness", "Juiciness", "Smoothness",
        ]
        feature_components = ["Aroma Intensity", "Fresh Aroma"]
        rows = []
        for respondent in range(12):
            for index, label in enumerate(target_components):
                rows.append({
                    "slot": 0, "request_role": "target", "requested_component": None,
                    "question_id": target_qid, "prompt": "Texture",
                    "question_type": "line-scale", "survey_id": survey_id,
                    "survey_title": "Test", "in_scope": True,
                    "components": [
                        {"id": f"target-{i}", "label": component}
                        for i, component in enumerate(target_components)
                    ],
                    "enrollment_id": f"respondent-{respondent}", "product_id": None,
                    "answer_component_id": f"target-{index}",
                    "raw_value": str(respondent + index),
                })
            for index, label in enumerate(feature_components):
                rows.append({
                    "slot": 1000, "request_role": "feature", "requested_component": None,
                    "question_id": feature_qid, "prompt": "Aroma",
                    "question_type": "line-scale", "survey_id": survey_id,
                    "survey_title": "Test", "in_scope": True,
                    "components": [
                        {"id": f"feature-{i}", "label": component}
                        for i, component in enumerate(feature_components)
                    ],
                    "enrollment_id": f"respondent-{respondent}", "product_id": None,
                    "answer_component_id": f"feature-{index}",
                    "raw_value": str(respondent - index),
                })

        fake_report = [{
            "feature": "Aroma — Fresh Aroma", "raw_p_value": 0.5,
            "adjusted_p_value": None, "partial_r2": 0.01,
            "significant": False, "influential": False,
            "collinearity_flag": False,
        }]
        fake_info = {
            "n_used": 12, "cv_r2": 0.2,
            "global_raw_p_value": 0.5,
            "global_adjusted_p_value": None,
            "global_significant": False,
            "max_abs_quadratic_residual_corr": 0.1,
        }
        with (
            patch.dict(agent._SCOPE, {"survey_id": survey_id}, clear=True),
            patch.object(agent, "_fetch_influence_rows", return_value=rows) as fetch,
            patch.object(
                agent, "feature_influence", return_value=(fake_report, fake_info, [])
            ) as calculate,
        ):
            result = agent.analyze_feature_influence.invoke({
                "target": {"question_id": target_qid},
                "features": [],
            })
        payload = json.loads(result.split("\n", 1)[1])
        fetch.assert_called_once()
        self.assertEqual(calculate.call_count, 5)
        self.assertEqual(len(payload["analyses"]), 5)
        self.assertEqual(
            [analysis["target"]["label"] for analysis in payload["analyses"]],
            [f"Texture — {component}" for component in target_components],
        )
        self.assertEqual(payload["feature_selection"], "all_other_compatible_numeric")
        self.assertEqual(
            [feature["label"] for feature in payload["candidate_features"]],
            ["Aroma — Aroma Intensity", "Aroma — Fresh Aroma"],
        )


class InfluenceSuggestionContractTests(unittest.TestCase):
    def test_prompt_automates_alpha_and_allows_feasible_follow_ups(self):
        self.assertIn("let the tool use 0.05", AGENT_BACKSTORY)
        self.assertIn("After EVERY influence request", AGENT_BACKSTORY)
        self.assertIn("If at least one exists, you MUST offer 1-3", AGENT_BACKSTORY)

    def test_one_feasible_influence_button_is_valid(self):
        answer = (
            "One alternative is statistically feasible:\n\n"
            "{{Analyze which aroma attributes influence Firmness}}"
        )
        self.assertTrue(all(good for _label, good, _note in validate_suggestions(answer)))

    def test_influence_buttons_are_valid_complete_follow_ups(self):
        answer = (
            "Choose a component:\n\n"
            "{{Analyze which survey features influence Creaminess}}\n"
            "{{Analyze which survey features influence Firmness}}"
        )
        self.assertTrue(all(good for _label, good, _note in validate_suggestions(answer)))

    def test_unwrapped_influence_menu_is_caught_by_regression_check(self):
        answer = (
            "Please choose one target:\n"
            "1. What influences Creaminess?\n"
            "2. What influences Firmness?"
        )
        checks = {label: good for label, good, _note in validate_suggestions(answer)}
        self.assertFalse(checks["choice menu uses braces"])


if __name__ == "__main__":
    unittest.main()
