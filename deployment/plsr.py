"""Partial least squares regression for attribute-to-KPI analysis at product grain.

The public function is independent of LangChain and the database so synthetic tests can
exercise it directly. The NIPALS fit, the VIP formula and the reported metrics reproduce
sklearn's ``PLSRegression`` applied to ``StandardScaler``-scaled predictors; equivalence is
pinned by tests/plsr_sklearn_fixture.json, generated from scikit-learn 1.9.0. scikit-learn
itself is deliberately not a dependency -- see the numpy-only constraint the rest of this
bundle is built under.

The cross-validation block mirrors the reference's ``cross_val_score(pls, X_scaled, Y, cv=...)``:
the outer z-scoring is fitted once on every row and only the PLS fit is repeated per fold, so the
held-out metrics carry the same mild scaler leak the reference has. K-fold reproduces
``KFold(n_splits=k, shuffle=False)`` and the reference's unweighted mean over the per-fold scores;
leave-one-out keeps that MSE recipe but reports a pooled R2, because a one-row fold has no
variance and the reference's own per-fold R2 is NaN there. Equivalence for both is pinned by
tests/plsr_sklearn_fixture.json and tests/plsr_sklearn_kfold_fixture.json.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np


_EPS = 1e-12

# PLS needs at least one degree of freedom left after the components it extracts, and the
# component count below is fixed rather than tuned.
DEFAULT_COMPONENTS = 2
DEFAULT_NORMALIZATION = "reference"
DEFAULT_CV_METHOD = "loo"


def _center_scale(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Centre and unit-scale columns the way sklearn's _center_scale_xy does (ddof=1)."""
    mean = matrix.mean(axis=0)
    centred = matrix - mean
    std = centred.std(axis=0, ddof=1)
    std[std < _EPS] = 1.0
    return centred / std, mean, std


def _nipals(x: np.ndarray, y: np.ndarray, n_components: int):
    """NIPALS with regression ("mode A") deflation. y is single-column here."""
    xd, yd = x.copy(), y.copy()
    n_features = x.shape[1]
    x_weights = np.zeros((n_features, n_components))
    x_scores = np.zeros((x.shape[0], n_components))
    x_loadings = np.zeros((n_features, n_components))
    y_loadings = np.zeros((y.shape[1], n_components))
    for component in range(n_components):
        weight = xd.T @ yd[:, [0]]
        norm = np.linalg.norm(weight)
        if norm < _EPS:
            break
        weight = weight / norm
        score = xd @ weight
        denominator = (score.T @ score).item()
        if denominator < _EPS:
            break
        x_loading = (xd.T @ score) / denominator
        y_loading = (yd.T @ score) / denominator
        xd = xd - score @ x_loading.T
        yd = yd - score @ y_loading.T
        x_weights[:, [component]] = weight
        x_scores[:, [component]] = score
        x_loadings[:, [component]] = x_loading
        y_loadings[:, [component]] = y_loading
    return x_weights, x_scores, x_loadings, y_loadings


def calculate_vip(
    x_scores: np.ndarray,
    x_weights: np.ndarray,
    y_loadings: np.ndarray,
    n_features_total: int | None = None,
) -> np.ndarray:
    """Variable importance in projection, one score per fitted predictor.

    n_features_total is the predictor count before any column was dropped. sklearn keeps a
    constant column as an all-zero one -- zero weight, hence zero VIP -- and still counts it
    in the formula's leading p, so dropping it and letting p shrink scales every surviving
    score by sqrt(p_kept / p_total). Pass the original count to keep the magnitudes on the
    reference's scale; the returned array still holds one score per fitted predictor.
    """
    t, w, q = x_scores, x_weights, y_loadings
    n_fitted, h = w.shape
    p = n_fitted if n_features_total is None else int(n_features_total)
    s = np.diag(t.T @ t @ q.T @ q).reshape(h, -1)
    total = s.sum()
    if abs(total) < _EPS:
        return np.full(n_fitted, np.nan)
    norms = np.linalg.norm(w, axis=0)
    w_norm = w / np.where(norms < _EPS, 1.0, norms)
    return np.sqrt(p * (w_norm**2 @ s).flatten() / total)


def _center_only(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Centre columns without scaling, matching PLSRegression(scale=False)."""
    mean = matrix.mean(axis=0)
    return matrix - mean, mean, np.ones(matrix.shape[1], dtype=float)


def _fit_model(
    x_input: np.ndarray,
    y: np.ndarray,
    n_components: int,
    *,
    internal_scale: bool,
):
    """Fit PLS after the caller has selected the outer predictor normalization."""
    scale = _center_scale if internal_scale else _center_only
    xs, xs_mean, xs_std = scale(x_input)
    ys, ys_mean, ys_std = _center_scale(y.reshape(-1, 1))
    x_weights, x_scores, x_loadings, y_loadings = _nipals(xs, ys, n_components)
    rotations = x_weights @ np.linalg.pinv(x_loadings.T @ x_weights)
    # sklearn folds its internal x-scaling into coef_, so these coefficients map the
    # standardised predictors straight onto the KPI's own units.
    coefficients = (rotations @ y_loadings.T) * ys_std / xs_std.reshape(-1, 1)
    return coefficients, xs_mean, ys_mean, x_scores, x_weights, y_loadings


def _predict_model(
    x_input: np.ndarray, coefficients: np.ndarray, xs_mean: np.ndarray, ys_mean: np.ndarray
) -> np.ndarray:
    return ((x_input - xs_mean) @ coefficients + ys_mean).reshape(-1)


def max_kfold_folds(n_units: int) -> int:
    """Largest fold count whose blocks all still hold two units. Below 2, K-fold cannot run."""
    return n_units // 2


def _cv_fold_indices(n_units: int, cv_method: str, cv_folds: int | None) -> list[np.ndarray]:
    """Deterministic held-out blocks: one row per fold for LOO, contiguous blocks for K-fold.

    The K-fold split reproduces sklearn's ``KFold(n_splits=k, shuffle=False)`` -- contiguous
    blocks in row order, the first ``n % k`` of them one row longer. Every K-fold block must
    hold at least two units, because the reference scores each fold on its own held-out rows
    and R2 is undefined for a single one.
    """
    if cv_method == "loo":
        return [np.array([index]) for index in range(n_units)]
    if cv_method == "kfold":
        if cv_folds is None or cv_folds < 2 or cv_folds > n_units:
            raise ValueError("kfold cross-validation requires 2 <= cv_folds <= number of units.")
        folds = [fold for fold in np.array_split(np.arange(n_units), cv_folds) if len(fold)]
        if any(len(fold) < 2 for fold in folds):
            ceiling = max_kfold_folds(n_units)
            remedy = (
                f"cv_folds must be at most {ceiling}, not {cv_folds}: choose a smaller fold "
                f"count, or use loo."
                if ceiling >= 2 else
                f"{n_units} unit(s) cannot be split into two blocks of two, so kfold cannot "
                f"run here at all -- use loo or no cross-validation."
            )
            raise ValueError(
                f"kfold cross-validation scores every fold on its own held-out block, so each "
                f"fold needs at least two units: {remedy}"
            )
        return folds
    raise ValueError(f"Unsupported cross-validation method: {cv_method!r}.")


def _cv_predictions(
    x_input: np.ndarray,
    y: np.ndarray,
    n_components: int,
    *,
    cv_method: str,
    cv_folds: int | None,
    internal_scale: bool,
) -> tuple[np.ndarray, list[np.ndarray]]:
    """Held-out predictions using deterministic LOO or unshuffled K-fold splits.

    Outer normalization is intentionally performed before this function, matching the
    reference's scaler.fit_transform(X) before cross_val_score. Each fold refits the PLS
    model and keeps the requested component count. The folds are returned alongside the
    predictions because the reference scores per fold, not over the pooled vector.
    """
    folds = _cv_fold_indices(len(y), cv_method, cv_folds)
    predictions = np.full(len(y), np.nan)
    for held_out_indices in folds:
        train = np.ones(len(y), dtype=bool)
        train[held_out_indices] = False
        coefficients, xs_mean, ys_mean, *_ = _fit_model(
            x_input[train], y[train], n_components, internal_scale=internal_scale
        )
        predictions[held_out_indices] = _predict_model(
            x_input[held_out_indices], coefficients, xs_mean, ys_mean
        )
    return predictions, folds


def _r2_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """sklearn's r2_score for a single target, including its constant-y_true conventions."""
    numerator = float(np.sum((y_true - y_pred) ** 2))
    denominator = float(np.sum((y_true - y_true.mean()) ** 2))
    if denominator > _EPS:
        return 1.0 - numerator / denominator
    # sklearn returns 1.0 when a constant y_true is predicted exactly and 0.0 otherwise.
    return 1.0 if numerator < _EPS else 0.0


def _cv_scores(
    x_input: np.ndarray,
    y: np.ndarray,
    n_components: int,
    *,
    cv_method: str,
    cv_folds: int | None,
    internal_scale: bool,
) -> tuple[float, float, str]:
    """Held-out MSE and R2, plus the aggregation that produced them.

    K-fold follows the reference's own ``cross_val_score(...).mean()`` recipe exactly: each
    fold is scored on its own held-out block and the fold scores are averaged unweighted.
    LOO cannot use that recipe for R2 -- a one-row fold has no variance of its own, so
    sklearn's r2_score is NaN for every fold and their mean is NaN too -- so LOO reports the
    pooled R2 over all held-out predictions instead. LOO's MSE is unaffected by the choice:
    with one row per fold the unweighted fold mean and the pooled mean are the same number.
    """
    predictions, folds = _cv_predictions(
        x_input,
        y,
        n_components,
        cv_method=cv_method,
        cv_folds=cv_folds,
        internal_scale=internal_scale,
    )
    residual = y - predictions
    if cv_method == "loo":
        return float(np.mean(residual**2)), _r2_score(y, predictions), "pooled"
    fold_mse = [float(np.mean(residual[fold] ** 2)) for fold in folds]
    fold_r2 = [_r2_score(y[fold], predictions[fold]) for fold in folds]
    return float(np.mean(fold_mse)), float(np.mean(fold_r2)), "mean_of_folds"


def _loo_predictions(x_input: np.ndarray, y: np.ndarray, n_components: int) -> np.ndarray:
    """Backward-compatible test helper for the default reference-style LOOCV."""
    normalized, internal_scale = _outer_normalize(x_input, DEFAULT_NORMALIZATION)
    return _cv_predictions(
        normalized,
        np.asarray(y, dtype=float),
        n_components,
        cv_method="loo",
        cv_folds=None,
        internal_scale=internal_scale,
    )[0]


def _outer_normalize(x: np.ndarray, normalization: str) -> tuple[np.ndarray, bool]:
    """Apply the user-selected predictor normalization and return internal-scale setting."""
    if normalization == "reference" or normalization == "zscore_once":
        mean = x.mean(axis=0)
        sd = np.where(x.std(axis=0) < _EPS, 1.0, x.std(axis=0))
        return (x - mean) / sd, normalization == "reference"
    if normalization == "pls_internal":
        return x, True
    if normalization == "none":
        return x, False
    raise ValueError(
        "normalization must be one of: reference, zscore_once, pls_internal, none."
    )


def aggregate_by_unit(
    x: Any, y: Any, units: Sequence[Any]
) -> tuple[np.ndarray, np.ndarray, list[str], list[int]]:
    """Average every attribute and the KPI within each unit, one output row per unit.

    Respondent rows are the collection grain; the fit runs on these unit means. Rows holding a
    non-finite value contribute nothing to that column's mean rather than voiding the unit.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float).reshape(-1)
    keys = np.asarray([str(unit) for unit in units], dtype=object)
    if len(keys) != len(y) or len(x) != len(y):
        raise ValueError("x, y and units must contain the same number of rows.")
    ordered = sorted(set(keys.tolist()))
    means_x = np.full((len(ordered), x.shape[1]), np.nan)
    means_y = np.full(len(ordered), np.nan)
    counts: list[int] = []
    for index, unit in enumerate(ordered):
        mask = keys == unit
        block = x[mask]
        finite = np.isfinite(block)
        present = finite.sum(axis=0)
        totals = np.where(finite, block, 0.0).sum(axis=0)
        means_x[index] = np.where(present > 0, totals / np.maximum(present, 1), np.nan)
        column = y[mask]
        column = column[np.isfinite(column)]
        means_y[index] = float(np.mean(column)) if len(column) else np.nan
        counts.append(int(mask.sum()))
    return means_x, means_y, ordered, counts


def plsr_analysis(
    x: Any,
    y: Any,
    *,
    feature_names: Sequence[str] | None = None,
    n_components: int = DEFAULT_COMPONENTS,
    normalization: str = DEFAULT_NORMALIZATION,
    cv_method: str = DEFAULT_CV_METHOD,
    cv_folds: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return VIP-ranked attributes and metrics using explicit normalization/CV choices."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float).reshape(-1)
    if x.ndim != 2 or len(y) != len(x):
        raise ValueError("X must be (N,d) and y must contain N values.")
    n_original, n_features_original = x.shape
    names = np.asarray(
        feature_names if feature_names is not None
        else [f"x{i}" for i in range(n_features_original)],
        dtype=object,
    )
    if len(names) != n_features_original:
        raise ValueError("feature_names must have length d.")

    valid = np.isfinite(y) & np.all(np.isfinite(x), axis=1)
    x, y = x[valid], y[valid]

    keep = np.var(x, axis=0) > _EPS
    dropped = names[~keep].tolist()
    x, names = x[:, keep], names[keep]
    if x.shape[1] == 0:
        raise ValueError("All candidate attributes are constant across the fitted units.")
    if np.std(y) < _EPS:
        raise ValueError("The KPI has no variation across the fitted units.")

    observations = len(y)
    # Centred X has rank at most N-1, so more components than that fit nothing new.
    usable_components = min(n_components, observations - 1, x.shape[1])
    if observations < 3 or usable_components < 1:
        raise ValueError(
            f"PLS regression needs at least three units with complete data and one usable "
            f"component; {observations} unit(s) are available."
        )

    x_input, internal_scale = _outer_normalize(x, normalization)

    coefficients, xs_mean, ys_mean, x_scores, x_weights, y_loadings = _fit_model(
        x_input, y, usable_components, internal_scale=internal_scale
    )
    prediction = _predict_model(x_input, coefficients, xs_mean, ys_mean)

    residual = y - prediction
    mse = float(np.mean(residual**2))
    total = float(np.sum((y - y.mean()) ** 2))

    # In-sample fit on a handful of product means is near-perfect whatever the attributes
    # say, so the held-out error is the only one that shows whether the model predicts.
    if cv_method == "none":
        mse_cv = float("nan")
        r2_cv = float("nan")
        cv_aggregation = None
    else:
        mse_cv, r2_cv, cv_aggregation = _cv_scores(
            x_input,
            y,
            usable_components,
            cv_method=cv_method,
            cv_folds=cv_folds,
            internal_scale=internal_scale,
        )

    vip = calculate_vip(x_scores, x_weights, y_loadings, n_features_original)

    rows = [
        {
            "feature": str(name),
            "pls_coefficient": float(coefficients[index, 0]),
            "vip_score": float(vip[index]),
        }
        for index, name in enumerate(names)
    ]
    rows.sort(key=lambda row: row["vip_score"], reverse=True)

    metrics = {
        "mse": mse,
        "rmse": float(np.sqrt(mse)),
        "r2": 1.0 - float(np.sum(residual**2)) / total if total > _EPS else float("nan"),
        # K-fold averages the per-fold scores, which is what the reference's
        # cross_val_score(...).mean() computes. LOO averages nothing for R2 -- a one-product
        # fold has no variance, so the reference's raw mean is NaN -- and reports the pooled
        # value instead. cv_aggregation records which of the two produced these numbers.
        "rmse_cv": float(np.sqrt(mse_cv)),
        "r2_cv": r2_cv,
        "normalization": normalization,
        "cv_method": cv_method,
        "cv_aggregation": cv_aggregation,
        "cv_folds": cv_folds if cv_method == "kfold" else None,
        "n_units": observations,
        "n_units_dropped": n_original - observations,
        "n_features_used": int(x.shape[1]),
        "n_features_original": n_features_original,
        "dropped_constant_features": dropped,
        "n_components": int(usable_components),
        "n_components_requested": int(n_components),
    }
    return rows, metrics


__all__ = [
    "plsr_analysis", "aggregate_by_unit", "calculate_vip", "max_kfold_folds",
    "DEFAULT_COMPONENTS", "DEFAULT_NORMALIZATION", "DEFAULT_CV_METHOD",
]
