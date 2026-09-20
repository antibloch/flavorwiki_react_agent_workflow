"""Survey-aware statistical tests for conditional linear feature influence.

The public function is independent of LangChain and the database so synthetic tests can
exercise it directly. It fits all candidate features together, tests the complete candidate
set and each feature's added contribution with respondent-clustered wild resampling, and
leaves response-wide multiple-testing correction to the tool wrapper.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

import numpy as np


_EPS = 1e-12


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if np.std(a) < _EPS or np.std(b) < _EPS:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _r2(y: np.ndarray, prediction: np.ndarray) -> float:
    denominator = float(np.sum((y - np.mean(y)) ** 2))
    if denominator < _EPS:
        return float("nan")
    return 1.0 - float(np.sum((y - prediction) ** 2)) / denominator


def _design(features: np.ndarray, controls: np.ndarray | None) -> np.ndarray:
    parts = [np.ones((len(features), 1)), features]
    if controls is not None and controls.shape[1]:
        parts.append(controls)
    return np.column_stack(parts)


def _fit_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    train_controls: np.ndarray | None,
    test_controls: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    mean = np.mean(train_x, axis=0) if train_x.shape[1] else np.empty(0)
    scale = np.std(train_x, axis=0) if train_x.shape[1] else np.empty(0)
    scale = np.where(scale < _EPS, 1.0, scale)
    z_train = (train_x - mean) / scale
    z_test = (test_x - mean) / scale
    coefficients = np.linalg.lstsq(
        _design(z_train, train_controls), train_y, rcond=None
    )[0]
    prediction = _design(z_test, test_controls) @ coefficients
    return prediction, coefficients


def _group_folds(groups: np.ndarray, requested_folds: int, random_state: int):
    unique_groups = np.unique(groups)
    # Two validation respondents per fold where possible. This keeps the original eight-row
    # minimum useful for respondent-grain data instead of producing singleton R-squared folds.
    n_folds = min(requested_folds, max(2, len(unique_groups) // 2))
    if len(unique_groups) < 4 or n_folds < 2:
        raise ValueError("At least four distinct respondents are required for grouped CV.")
    shuffled = unique_groups.copy()
    np.random.default_rng(random_state).shuffle(shuffled)
    folds = []
    for validation_groups in np.array_split(shuffled, n_folds):
        validation = np.isin(groups, validation_groups)
        folds.append((np.flatnonzero(~validation), np.flatnonzero(validation)))
    return folds


def _cv_result(
    x: np.ndarray,
    y: np.ndarray,
    controls: np.ndarray | None,
    folds: Sequence[tuple[np.ndarray, np.ndarray]],
    drop: tuple[int, ...] = (),
) -> tuple[np.ndarray, np.ndarray]:
    kept = np.ones(x.shape[1], dtype=bool)
    if drop:
        kept[list(drop)] = False
    scores = np.full(len(folds), np.nan)
    predictions = np.full(len(y), np.nan)
    for fold_number, (train, validation) in enumerate(folds):
        train_controls = controls[train] if controls is not None else None
        validation_controls = controls[validation] if controls is not None else None
        prediction, _ = _fit_predict(
            x[train][:, kept], y[train], x[validation][:, kept],
            train_controls, validation_controls,
        )
        predictions[validation] = prediction
        scores[fold_number] = _r2(y[validation], prediction)
    return scores, predictions


def _mean_finite(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    return float(np.mean(finite)) if len(finite) else float("nan")


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Return Holm step-down adjusted p-values in the original order."""
    p = np.asarray(p_values, dtype=float)
    if not len(p):
        return []
    order = np.argsort(p)
    ranked = p[order]
    adjusted_ranked = np.maximum.accumulate(
        ranked * np.arange(len(p), 0, -1, dtype=float)
    )
    adjusted = np.empty(len(p), dtype=float)
    adjusted[order] = np.clip(adjusted_ranked, 0.0, 1.0)
    return adjusted.tolist()


def _projection_parts(design: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return design, np.linalg.pinv(design)


def _rss_many(parts: tuple[np.ndarray, np.ndarray], outcomes: np.ndarray) -> np.ndarray:
    """Residual sums of squares for one or many row-wise outcome vectors."""
    design, inverse = parts
    coefficients = outcomes @ inverse.T
    residual = outcomes - coefficients @ design.T
    return np.sum(residual * residual, axis=1)


def _partial_r2(reduced_rss: np.ndarray, full_rss: np.ndarray) -> np.ndarray:
    denominator = np.maximum(reduced_rss, _EPS)
    return np.maximum(0.0, (reduced_rss - full_rss) / denominator)


def _wild_partial_r2_test(
    y: np.ndarray,
    full_parts: tuple[np.ndarray, np.ndarray],
    reduced_parts: tuple[np.ndarray, np.ndarray],
    wild_weights: np.ndarray,
) -> tuple[float, float]:
    """Cluster-wild-bootstrap p-value for one nested-model partial R-squared."""
    reduced_design, reduced_inverse = reduced_parts
    reduced_fit = reduced_design @ (reduced_inverse @ y)
    reduced_residual = y - reduced_fit
    generated = reduced_fit[None, :] + wild_weights * reduced_residual[None, :]

    observed_reduced = _rss_many(reduced_parts, y[None, :])
    observed_full = _rss_many(full_parts, y[None, :])
    observed = float(_partial_r2(observed_reduced, observed_full)[0])
    null_reduced = _rss_many(reduced_parts, generated)
    null_full = _rss_many(full_parts, generated)
    null_statistic = _partial_r2(null_reduced, null_full)
    p_value = (1 + int(np.sum(null_statistic >= observed - _EPS))) / (
        len(null_statistic) + 1
    )
    return observed, float(p_value)


def _vif_for(
    feature_index: int,
    z: np.ndarray,
    controls: np.ndarray | None,
) -> float:
    others = np.arange(z.shape[1]) != feature_index
    predictors = z[:, others]
    if controls is not None and controls.shape[1]:
        predictors = np.column_stack([predictors, controls])
    if predictors.shape[1] == 0:
        return 1.0
    prediction, _ = _fit_predict(
        predictors, z[:, feature_index], predictors, None, None
    )
    explained = _r2(z[:, feature_index], prediction)
    if not np.isfinite(explained):
        return float("inf")
    return float("inf") if explained >= 1 - _EPS else 1.0 / (1.0 - explained)


def _connected_correlated_groups(correlation: np.ndarray, cutoff: float) -> list[tuple[int, ...]]:
    remaining = set(range(len(correlation)))
    groups: list[tuple[int, ...]] = []
    while remaining:
        start = remaining.pop()
        component = {start}
        frontier = [start]
        while frontier:
            current = frontier.pop()
            neighbours = {
                other for other in tuple(remaining)
                if abs(correlation[current, other]) > cutoff
            }
            remaining.difference_update(neighbours)
            component.update(neighbours)
            frontier.extend(neighbours)
        if len(component) > 1:
            groups.append(tuple(sorted(component)))
    return groups


def _bootstrap_chunk(
    jobs: Sequence[tuple[int, np.random.SeedSequence]],
    z: np.ndarray,
    y: np.ndarray,
    controls: np.ndarray | None,
    groups: np.ndarray,
    y_scale: float,
) -> list[tuple[int, np.ndarray]]:
    unique_groups = np.unique(groups)
    row_indices = {group: np.flatnonzero(groups == group) for group in unique_groups}
    output = []
    for output_index, seed in jobs:
        rng = np.random.default_rng(seed)
        sampled_groups = rng.choice(unique_groups, size=len(unique_groups), replace=True)
        sample = np.concatenate([row_indices[group] for group in sampled_groups])
        sample_controls = controls[sample] if controls is not None else None
        coefficients = np.linalg.lstsq(
            _design(z[sample], sample_controls), y[sample], rcond=None
        )[0]
        output.append((output_index, coefficients[1:1 + z.shape[1]] / y_scale))
    return output


def feature_influence(
    x: Any,
    y: Any,
    *,
    groups: Any,
    products: Any | None = None,
    feature_names: Sequence[str] | None = None,
    cv: int = 5,
    n_boot: int = 500,
    n_resamples: int = 999,
    alpha: float = 0.05,
    random_state: int = 0,
    max_workers: int = 4,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Return conditional feature tests, global diagnostics, and attribution warnings."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float).reshape(-1)
    groups = np.asarray(groups)
    if x.ndim != 2 or len(y) != len(x) or len(groups) != len(y):
        raise ValueError("X must be (N,d), and y/groups must each contain N values.")
    if products is not None:
        products = np.asarray(products)
        if len(products) != len(y):
            raise ValueError("products must contain N values.")
    n_original, original_feature_count = x.shape
    names = np.asarray(
        feature_names if feature_names is not None
        else [f"x{i}" for i in range(original_feature_count)],
        dtype=object,
    )
    if len(names) != original_feature_count:
        raise ValueError("feature_names must have length d.")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between zero and one.")
    if n_resamples < 19:
        raise ValueError("At least 19 significance resamples are required.")

    valid = np.isfinite(y) & np.all(np.isfinite(x), axis=1)
    x, y, groups = x[valid], y[valid], groups[valid]
    if products is not None:
        products = products[valid]
    if len(y) < 8:
        raise ValueError("Too few complete observations; at least eight are required.")
    if np.std(y) < _EPS:
        raise ValueError("The target has no variation.")

    keep = np.var(x, axis=0) > _EPS
    dropped_features = names[~keep].tolist()
    original_indices = np.arange(original_feature_count)[keep]
    x, names = x[:, keep], names[keep]
    if x.shape[1] == 0:
        info = {
            "n_original": n_original,
            "n_used": len(y),
            "n_removed": n_original - len(y),
            "respondents": int(len(np.unique(groups))),
            "n_features_original": original_feature_count,
            "n_features_used": 0,
            "dropped_constant_features": dropped_features,
            "cv_r2": None,
            "max_abs_quadratic_residual_corr": None,
            "warning": "All candidate features are constant.",
        }
        return [], info, []

    controls = None
    if products is not None:
        product_levels = np.unique(products)
        # One reference product is omitted; the controls are intentionally never reported as
        # candidate features.
        controls = np.column_stack([
            (products == product).astype(float) for product in product_levels[1:]
        ]) if len(product_levels) > 1 else np.empty((len(y), 0))

    z_for_rank = (x - np.mean(x, axis=0)) / np.std(x, axis=0)
    full_design = _design(z_for_rank, controls)
    design_rank = int(np.linalg.matrix_rank(full_design))
    respondents = int(len(np.unique(groups)))
    minimum_respondents = max(12, design_rank + 2)
    if respondents < minimum_respondents:
        raise ValueError(
            f"Too few distinct respondents for significance inference: {respondents} "
            f"available, at least {minimum_respondents} required for this model."
        )

    folds = _group_folds(groups, cv, random_state)
    full_scores, out_of_fold_prediction = _cv_result(x, y, controls, folds)
    full_cv = _mean_finite(full_scores)
    if not np.isfinite(full_cv):
        raise ValueError("Grouped CV could not produce a valid R-squared value.")

    feature_mean = np.mean(x, axis=0)
    feature_scale = np.std(x, axis=0)
    z = (x - feature_mean) / feature_scale
    y_scale = float(np.std(y))
    full_coefficients = np.linalg.lstsq(_design(z, controls), y, rcond=None)[0]
    beta_y_units = full_coefficients[1:1 + x.shape[1]]
    beta_std = beta_y_units / y_scale
    beta_raw = beta_y_units / feature_scale

    if x.shape[1] == 1:
        correlation_matrix = np.eye(1)
        max_feature_corr = np.zeros(1)
    else:
        correlation_matrix = np.corrcoef(z, rowvar=False)
        without_diagonal = np.abs(correlation_matrix.copy())
        np.fill_diagonal(without_diagonal, 0.0)
        max_feature_corr = np.nanmax(without_diagonal, axis=1)
    corr_y = np.asarray([_corr(z[:, j], y) for j in range(x.shape[1])])

    workers = max(1, min(int(max_workers), x.shape[1], 4))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="influence") \
        if workers > 1 else None
    try:
        seeds = np.random.SeedSequence(random_state).spawn(n_boot)
        jobs = list(enumerate(seeds))
        if executor is not None and n_boot >= 100:
            chunks = [jobs[i::workers] for i in range(workers)]
            futures = [
                executor.submit(_bootstrap_chunk, chunk, z, y, controls, groups, y_scale)
                for chunk in chunks if chunk
            ]
            boot_pairs = [pair for future in futures for pair in future.result()]
        else:
            boot_pairs = _bootstrap_chunk(jobs, z, y, controls, groups, y_scale)
        boot = np.empty((n_boot, x.shape[1]))
        for output_index, coefficients in boot_pairs:
            boot[output_index] = coefficients

        def feature_diagnostics(j: int):
            reduced_scores, _ = _cv_result(x, y, controls, folds, (j,))
            fold_delta = full_scores - reduced_scores
            return j, _vif_for(j, z, controls), _mean_finite(fold_delta), fold_delta

        if executor is not None:
            diagnostics = list(executor.map(feature_diagnostics, range(x.shape[1])))
        else:
            diagnostics = [feature_diagnostics(j) for j in range(x.shape[1])]

        vif = np.empty(x.shape[1])
        delta_r2 = np.empty(x.shape[1])
        for j, feature_vif, feature_delta, _fold_delta in diagnostics:
            vif[j] = feature_vif
            delta_r2[j] = feature_delta

        correlated_indices = _connected_correlated_groups(correlation_matrix, 0.90)

        def group_diagnostics(indices: tuple[int, ...]):
            reduced_scores, _ = _cv_result(x, y, controls, folds, indices)
            fold_delta = full_scores - reduced_scores
            finite = fold_delta[np.isfinite(fold_delta)]
            positive = float(np.mean(finite > 0)) if len(finite) else float("nan")
            return indices, _mean_finite(fold_delta), positive

        if executor is not None and correlated_indices:
            raw_groups = list(executor.map(group_diagnostics, correlated_indices))
        else:
            raw_groups = [group_diagnostics(indices) for indices in correlated_indices]
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    ci_low, ci_high = np.quantile(boot, [0.025, 0.975], axis=0)
    sign_stability = np.maximum(np.mean(boot > 0, axis=0), np.mean(boot < 0, axis=0))
    residual = y - out_of_fold_prediction
    residual_corr = np.asarray([_corr(residual, z[:, j]) for j in range(x.shape[1])])
    quadratic_residual_corr = np.asarray([
        _corr(residual, z[:, j] ** 2) for j in range(x.shape[1])
    ])

    collinearity_flag = (vif > 10) | (max_feature_corr > 0.90)

    # Significance tests use the fixed full model and one reduced null per hypothesis. One
    # Rademacher multiplier is shared by every row from a respondent, preserving repeated
    # product measurements while breaking no within-respondent structure.
    unique_groups, group_inverse = np.unique(groups, return_inverse=True)
    sign_rng = np.random.default_rng(random_state + 10_003)
    cluster_signs = sign_rng.choice(
        np.array([-1.0, 1.0]), size=(n_resamples, len(unique_groups))
    )
    wild_weights = cluster_signs[:, group_inverse]
    full_parts = _projection_parts(_design(z, controls))
    control_features = np.empty((len(y), 0))
    controls_only_parts = _projection_parts(_design(control_features, controls))
    global_partial_r2, global_p_value = _wild_partial_r2_test(
        y, full_parts, controls_only_parts, wild_weights
    )

    def significance_for_feature(j: int) -> tuple[int, float, float]:
        reduced = np.delete(z, j, axis=1)
        reduced_parts = _projection_parts(_design(reduced, controls))
        partial, p_value = _wild_partial_r2_test(
            y, full_parts, reduced_parts, wild_weights
        )
        return j, partial, p_value

    significance_workers = max(1, min(int(max_workers), x.shape[1], 4))
    if significance_workers > 1:
        with ThreadPoolExecutor(
            max_workers=significance_workers, thread_name_prefix="influence-significance"
        ) as significance_executor:
            significance_rows = list(
                significance_executor.map(significance_for_feature, range(x.shape[1]))
            )
    else:
        significance_rows = [significance_for_feature(j) for j in range(x.shape[1])]
    partial_r2 = np.empty(x.shape[1])
    raw_p_value = np.empty(x.shape[1])
    for j, partial, p_value in significance_rows:
        partial_r2[j] = partial
        raw_p_value[j] = p_value

    report = []
    for j, name in enumerate(names):
        report.append({
            "feature": str(name),
            "index": int(original_indices[j]),
            "beta_raw": float(beta_raw[j]),
            "beta_std": float(beta_std[j]),
            "abs_beta_std": float(abs(beta_std[j])),
            "direction": "positive" if beta_std[j] > 0 else "negative",
            "corr_y": float(corr_y[j]),
            "bootstrap_ci_low": float(ci_low[j]),
            "bootstrap_ci_high": float(ci_high[j]),
            "sign_stability": float(sign_stability[j]),
            "delta_r2_cv": float(delta_r2[j]),
            "vif": float(vif[j]),
            "max_feature_corr": float(max_feature_corr[j]),
            "residual_corr": float(residual_corr[j]),
            "quadratic_residual_corr": float(quadratic_residual_corr[j]),
            "collinearity_flag": bool(collinearity_flag[j]),
            "partial_r2": float(partial_r2[j]),
            "raw_p_value": float(raw_p_value[j]),
            # Holm correction spans every target-feature hypothesis in the tool response,
            # so the wrapper fills these after all target components finish.
            "adjusted_p_value": None,
            "significant": False,
            "influential": False,
        })
    report.sort(
        key=lambda row: (
            -row["raw_p_value"], row["partial_r2"], row["abs_beta_std"],
        ),
        reverse=True,
    )

    correlated_groups = [{
        "features": [str(names[j]) for j in indices],
        "delta_r2_cv": float(group_delta),
        "positive_fold_stability": float(positive_fold_stability),
        "jointly_influential": False,
    } for indices, group_delta, positive_fold_stability in raw_groups]

    finite_quadratic = np.abs(quadratic_residual_corr[np.isfinite(quadratic_residual_corr)])
    info = {
        "n_original": n_original,
        "n_used": len(y),
        "n_removed": n_original - len(y),
        "respondents": respondents,
        "n_features_original": original_feature_count,
        "n_features_used": x.shape[1],
        "dropped_constant_features": dropped_features,
        "cv_folds": len(folds),
        "cv_r2": full_cv,
        "design_rank": design_rank,
        "rank_deficient": design_rank != full_design.shape[1],
        "minimum_respondents_required": minimum_respondents,
        "alpha": alpha,
        "n_resamples": n_resamples,
        "global_partial_r2": global_partial_r2,
        "global_raw_p_value": global_p_value,
        "global_adjusted_p_value": None,
        "global_significant": False,
        "max_abs_quadratic_residual_corr": (
            float(np.max(finite_quadratic)) if len(finite_quadratic) else None
        ),
    }
    return report, info, correlated_groups


__all__ = ["feature_influence", "holm_adjust"]
