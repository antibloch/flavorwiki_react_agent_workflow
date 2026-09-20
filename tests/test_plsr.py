"""plsr.plsr_analysis must reproduce scikit-learn's PLSRegression without depending on it.

plsr_sklearn_fixture.json was generated with scikit-learn 1.9.0 running the reference body
(StandardScaler -> PLSRegression(n_components=k) -> coef_/VIP/MSE/RMSE/R2, then
cross_val_score/cross_val_predict under LeaveOneOut). Regenerating it requires sklearn; the
bundle itself never imports it.

Two fixture details worth knowing before changing anything here:

* `r2_cv_pooled` is sklearn's leave-one-out R2 computed over all held-out predictions at once.
  The reference's own `cross_val_score(..., scoring='r2').mean()` is NaN in all ten cases --
  every LOO fold holds out one sample and `r2_score` warns "R^2 score is not well-defined with
  less than two samples" -- so the pooled form is the only usable rendering of that number and
  is what `plsr_analysis` reports as `r2_cv`.
* plsr_sklearn_kfold_fixture.json is the K-fold companion, generated from the same X/Y with
  `cross_val_score(pls, X_scaled, Y, cv=KFold(k))` for every fold count whose blocks all hold
  at least two rows. K-fold R2 is well defined per fold, so unlike LOO it is pinned against the
  reference's own `.mean()` -- no pooled substitute.
* The last two cases carry constant predictor columns, which the first eight do not. sklearn
  keeps such a column as an all-zero one and still counts it in the VIP formula's leading p;
  `plsr_analysis` drops it from the fit but passes the original count, so the surviving scores
  stay on sklearn's scale. Without those cases this file cannot see that difference.
"""

import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plsr import (  # noqa: E402
    _cv_predictions, _loo_predictions, _outer_normalize, aggregate_by_unit, calculate_vip,
    plsr_analysis,
)



FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plsr_sklearn_fixture.json")
KFOLD_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "plsr_sklearn_kfold_fixture.json"
)
TOLERANCE = 1e-9


def _by_original_order(rows, n_features):
    """plsr_analysis returns VIP-sorted rows; put them back in feature order.

    Constant predictors are dropped from the fit, so the indices actually returned come back
    alongside the values for the caller to line up against the reference.
    """
    lookup = {row["feature"]: row for row in rows}
    kept = [index for index in range(n_features) if f"x{index}" in lookup]
    return (
        np.array([lookup[f"x{index}"]["pls_coefficient"] for index in kept]),
        np.array([lookup[f"x{index}"]["vip_score"] for index in kept]),
        kept,
    )


class SklearnEquivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as handle:
            cls.cases = json.load(handle)

    def test_fixture_is_present_and_non_trivial(self):
        self.assertGreaterEqual(len(self.cases), 10)
        with_constants = [
            case for case in self.cases
            if np.any(np.var(np.array(case["X"], dtype=float), axis=0) <= 1e-12)
        ]
        self.assertTrue(with_constants, "no fixture case exercises constant-column dropping")

    def test_matches_sklearn_on_every_fixture_case(self):
        for case in self.cases:
            with self.subTest(n=case["n"], p=case["p"], components=case["n_components"]):
                rows, metrics = plsr_analysis(
                    np.array(case["X"]), np.array(case["Y"]),
                    n_components=case["n_components"],
                )
                coefficients, vip, kept = _by_original_order(rows, case["p"])
                expected = case["expected"]
                np.testing.assert_allclose(
                    coefficients, [expected["coef"][i] for i in kept],
                    atol=TOLERANCE, rtol=0,
                )
                np.testing.assert_allclose(
                    vip, [expected["vip"][i] for i in kept], atol=TOLERANCE, rtol=0
                )
                self.assertAlmostEqual(metrics["r2"], expected["r2"], delta=TOLERANCE)
                self.assertAlmostEqual(metrics["mse"], expected["mse"], delta=TOLERANCE)
                self.assertAlmostEqual(metrics["rmse"], expected["rmse"], delta=TOLERANCE)
                self.assertAlmostEqual(
                    metrics["rmse_cv"], expected["rmse_cv"], delta=TOLERANCE
                )
                self.assertAlmostEqual(
                    metrics["r2_cv"], expected["r2_cv_pooled"], delta=TOLERANCE
                )

    def test_dropped_constant_predictors_score_zero_under_sklearn(self):
        """The columns plsr drops are exactly the ones sklearn scores at zero."""
        seen = 0
        for case in self.cases:
            rows, _ = plsr_analysis(
                np.array(case["X"]), np.array(case["Y"]),
                n_components=case["n_components"],
            )
            _, _, kept = _by_original_order(rows, case["p"])
            for index in set(range(case["p"])) - set(kept):
                seen += 1
                self.assertAlmostEqual(case["expected"]["vip"][index], 0.0, delta=TOLERANCE)
                self.assertAlmostEqual(case["expected"]["coef"][index], 0.0, delta=TOLERANCE)
        self.assertGreater(seen, 0)

    def test_rows_are_sorted_by_descending_vip(self):
        case = self.cases[-1]
        rows, _ = plsr_analysis(
            np.array(case["X"]), np.array(case["Y"]), n_components=case["n_components"]
        )
        scores = [row["vip_score"] for row in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))


class Guards(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)

    def test_named_features_are_carried_through(self):
        x = self.rng.normal(size=(9, 3))
        y = x @ np.array([1.0, -2.0, 0.5]) + self.rng.normal(scale=0.1, size=9)
        rows, _ = plsr_analysis(x, y, feature_names=["Hardness", "Denseness", "Salt"])
        self.assertEqual({row["feature"] for row in rows}, {"Hardness", "Denseness", "Salt"})

    def test_too_few_units_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            plsr_analysis(self.rng.normal(size=(2, 4)), self.rng.normal(size=2))
        self.assertIn("at least three units", str(caught.exception))

    def test_components_are_capped_below_the_unit_count(self):
        # Three units leave rank 2, so a request for five components yields two.
        x = self.rng.normal(size=(3, 6))
        _, metrics = plsr_analysis(x, self.rng.normal(size=3), n_components=5)
        self.assertEqual(metrics["n_components"], 2)
        self.assertEqual(metrics["n_components_requested"], 5)

    def test_constant_attributes_are_dropped_and_reported(self):
        x = self.rng.normal(size=(8, 3))
        x[:, 1] = 4.0
        rows, metrics = plsr_analysis(x, self.rng.normal(size=8),
                                      feature_names=["a", "constant", "c"])
        self.assertEqual(metrics["dropped_constant_features"], ["constant"])
        self.assertEqual(metrics["n_features_used"], 2)
        self.assertNotIn("constant", {row["feature"] for row in rows})

    def test_constant_kpi_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            plsr_analysis(self.rng.normal(size=(8, 3)), np.full(8, 5.0))
        self.assertIn("no variation", str(caught.exception))

    def test_non_finite_rows_are_dropped_not_propagated(self):
        x = self.rng.normal(size=(10, 3))
        y = x @ np.array([1.0, 0.0, -1.0])
        x[4, 1] = np.nan
        y[7] = np.inf
        rows, metrics = plsr_analysis(x, y)
        self.assertEqual(metrics["n_units"], 8)
        self.assertEqual(metrics["n_units_dropped"], 2)
        self.assertTrue(all(np.isfinite(row["vip_score"]) for row in rows))

    def test_a_held_out_row_never_informs_its_own_prediction(self):
        """Moving one KPI value must not move that row's own held-out prediction.

        This is the one bug the fixture cannot catch: leaking the held-out row into its own
        fold still reproduces sklearn on every training metric.
        """
        x_scaled = self.rng.normal(size=(9, 4))
        y = x_scaled @ np.array([1.0, -0.5, 0.0, 0.3])
        moved = y.copy()
        moved[3] += 25.0
        base = _loo_predictions(x_scaled, y, 2)
        shifted = _loo_predictions(x_scaled, moved, 2)
        self.assertAlmostEqual(base[3], shifted[3], delta=1e-9)
        # Row 3 is training data for every other fold, so those predictions must move.
        others = [index for index in range(9) if index != 3]
        self.assertTrue(np.all(np.abs(base[others] - shifted[others]) > 1e-6))

    def test_held_out_error_exceeds_the_in_sample_fit_on_noise(self):
        # Six units against twelve unrelated attributes: the training fit is near-perfect and
        # says nothing, which is the reason the cross-validated pair is reported at all.
        x = self.rng.normal(size=(6, 12))
        y = self.rng.normal(size=6)
        _, metrics = plsr_analysis(x, y)
        self.assertGreater(metrics["r2"], 0.9)
        self.assertGreater(metrics["rmse_cv"], metrics["rmse"])
        self.assertLess(metrics["r2_cv"], metrics["r2"])

    def test_cross_validated_metrics_survive_the_minimum_unit_count(self):
        # Three units leave two-row training folds; the fold must still produce a number.
        x = self.rng.normal(size=(3, 5))
        _, metrics = plsr_analysis(x, self.rng.normal(size=3))
        self.assertTrue(np.isfinite(metrics["rmse_cv"]))
        self.assertTrue(np.isfinite(metrics["r2_cv"]))

    def test_normalization_choices_are_recorded_and_usable(self):
        x = self.rng.normal(size=(8, 4))
        y = self.rng.normal(size=8)
        for choice in ("reference", "zscore_once", "pls_internal", "none"):
            with self.subTest(normalization=choice):
                _, metrics = plsr_analysis(x, y, normalization=choice, cv_method="none")
                self.assertEqual(metrics["normalization"], choice)
                self.assertTrue(np.isnan(metrics["rmse_cv"]))

    def test_kfold_cross_validation_is_deterministic_and_recorded(self):
        x = self.rng.normal(size=(10, 4))
        y = self.rng.normal(size=10)
        _, metrics = plsr_analysis(
            x, y, normalization="zscore_once", cv_method="kfold", cv_folds=5
        )
        self.assertEqual(metrics["cv_method"], "kfold")
        self.assertEqual(metrics["cv_folds"], 5)
        self.assertTrue(np.isfinite(metrics["rmse_cv"]))
        self.assertTrue(np.isfinite(metrics["r2_cv"]))

    def test_kfold_matches_sklearn_cross_val_score_on_every_fixture_case(self):
        """K-fold must reproduce the reference recipe, not a pooled stand-in for it.

        Averaging the held-out residuals over all rows instead of over folds agrees with
        sklearn only when every fold is the same size, so the fixture deliberately includes
        fold counts that do not divide the row count evenly.
        """
        with open(KFOLD_FIXTURE, encoding="utf-8") as handle:
            cases = json.load(handle)
        self.assertTrue(cases, "kfold fixture is empty")
        uneven = [entry for case in cases for entry in case["kfold"]
                  if len(set(entry["fold_sizes"])) > 1]
        self.assertTrue(uneven, "no fixture entry exercises unequal fold sizes")
        for case in cases:
            for entry in case["kfold"]:
                with self.subTest(n=case["n"], p=case["p"], folds=entry["cv_folds"]):
                    _, metrics = plsr_analysis(
                        case["X"], case["Y"], n_components=case["n_components"],
                        normalization="reference", cv_method="kfold",
                        cv_folds=entry["cv_folds"],
                    )
                    self.assertAlmostEqual(
                        metrics["rmse_cv"], entry["rmse_cv"], delta=TOLERANCE
                    )
                    self.assertAlmostEqual(metrics["r2_cv"], entry["r2_cv"], delta=TOLERANCE)
                    self.assertEqual(metrics["cv_aggregation"], "mean_of_folds")

    def test_kfold_scores_per_fold_not_over_the_pooled_vector(self):
        # With 10 rows in 3 folds the blocks are 4/3/3, so the two aggregations differ; this
        # is the difference the fixture test above would still catch, stated directly.
        x = self.rng.normal(size=(10, 4))
        y = self.rng.normal(size=10)
        _, metrics = plsr_analysis(x, y, cv_method="kfold", cv_folds=3)
        predictions, folds = _cv_predictions(
            _outer_normalize(x, "reference")[0], y, 2,
            cv_method="kfold", cv_folds=3, internal_scale=True,
        )
        residual = y - predictions
        pooled = float(np.mean(residual**2))
        per_fold = float(np.mean([np.mean(residual[fold] ** 2) for fold in folds]))
        self.assertNotAlmostEqual(pooled, per_fold, delta=1e-6)
        self.assertAlmostEqual(metrics["rmse_cv"], np.sqrt(per_fold), delta=TOLERANCE)

    def test_kfold_rejects_a_fold_that_would_hold_one_unit(self):
        # Six units in five folds gives 2/1/1/1/1; a one-row fold has no variance, so the
        # reference's per-fold R2 is undefined there. The tool must say so, not return NaN.
        with self.assertRaises(ValueError) as caught:
            plsr_analysis(
                self.rng.normal(size=(6, 3)), self.rng.normal(size=6),
                cv_method="kfold", cv_folds=5,
            )
        self.assertIn("at most 3", str(caught.exception))
        # Three units cannot make two blocks of two at all, so "at most 1" would be advice
        # nobody can follow; the message has to send the user to loo instead.
        with self.assertRaises(ValueError) as tiny:
            plsr_analysis(
                self.rng.normal(size=(3, 3)), self.rng.normal(size=3),
                cv_method="kfold", cv_folds=2,
            )
        self.assertIn("kfold cannot run here at all", str(tiny.exception))
        self.assertNotIn("at most 1", str(tiny.exception))

    def test_cv_aggregation_is_reported_for_every_method(self):
        x = self.rng.normal(size=(8, 4))
        y = self.rng.normal(size=8)
        for method, folds, expected in (
            ("loo", None, "pooled"), ("kfold", 4, "mean_of_folds"), ("none", None, None),
        ):
            with self.subTest(cv_method=method):
                _, metrics = plsr_analysis(x, y, cv_method=method, cv_folds=folds)
                self.assertEqual(metrics["cv_aggregation"], expected)

    def test_kfold_requires_a_valid_fold_count(self):
        with self.assertRaises(ValueError):
            plsr_analysis(
                self.rng.normal(size=(6, 3)), self.rng.normal(size=6),
                cv_method="kfold", cv_folds=1,
            )

    def test_vip_is_nan_safe_when_scores_collapse(self):
        zero = np.zeros((4, 2))
        result = calculate_vip(zero, np.ones((2, 2)), np.zeros((1, 2)))
        self.assertTrue(np.all(np.isnan(result)))



class AggregateByUnit(unittest.TestCase):
    def test_rows_are_averaged_within_each_unit(self):
        x = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [9.0, 10.0]])
        y = np.array([10.0, 20.0, 30.0, 50.0])
        unit_x, unit_y, units, counts = aggregate_by_unit(x, y, ["B", "B", "A", "A"])
        self.assertEqual(units, ["A", "B"])           # deterministic, sorted
        self.assertEqual(counts, [2, 2])
        np.testing.assert_allclose(unit_x, [[7.0, 8.0], [2.0, 3.0]])
        np.testing.assert_allclose(unit_y, [40.0, 15.0])

    def test_uneven_group_sizes_are_means_not_sums(self):
        x = np.array([[2.0], [4.0], [9.0]])
        y = np.array([1.0, 3.0, 8.0])
        unit_x, unit_y, units, counts = aggregate_by_unit(x, y, ["A", "A", "B"])
        self.assertEqual(counts, [2, 1])
        np.testing.assert_allclose(unit_x.ravel(), [3.0, 9.0])
        np.testing.assert_allclose(unit_y, [2.0, 8.0])

    def test_a_non_finite_cell_does_not_void_the_whole_unit(self):
        x = np.array([[np.nan, 2.0], [4.0, 6.0]])
        unit_x, _, _, counts = aggregate_by_unit(x, np.array([1.0, 3.0]), ["A", "A"])
        self.assertEqual(counts, [2])
        np.testing.assert_allclose(unit_x, [[4.0, 4.0]])

    def test_a_fully_missing_column_stays_nan_for_that_unit(self):
        x = np.array([[np.nan, 2.0], [np.nan, 6.0]])
        unit_x, _, _, _ = aggregate_by_unit(x, np.array([1.0, 3.0]), ["A", "A"])
        self.assertTrue(np.isnan(unit_x[0, 0]))
        self.assertEqual(unit_x[0, 1], 4.0)

    def test_length_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_by_unit(np.zeros((3, 2)), np.zeros(3), ["A", "B"])

    def test_aggregation_feeds_the_fit_at_product_grain(self):
        rng = np.random.default_rng(3)
        products = np.repeat([f"p{i}" for i in range(6)], 20)
        x = rng.normal(size=(120, 4))
        y = x @ np.array([2.0, -1.0, 0.0, 0.5]) + rng.normal(scale=0.3, size=120)
        unit_x, unit_y, units, counts = aggregate_by_unit(x, y, products)
        self.assertEqual(len(units), 6)
        self.assertEqual(counts, [20] * 6)
        rows, metrics = plsr_analysis(unit_x, unit_y,
                                      feature_names=["a", "b", "c", "d"])
        self.assertEqual(metrics["n_units"], 6)          # products, not respondents
        self.assertEqual(metrics["n_components"], 2)
        self.assertEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()
