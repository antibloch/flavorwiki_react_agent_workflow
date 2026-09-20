"""K-means segmentation of respondent rating profiles.

Hand-rolled on numpy for the same reason `plsr.py` is: scipy / scikit-learn are deliberately
absent from this agent's dependencies (PROTOCOL.md section 5), and the numeric surface here is
small enough to write out and test against a committed fixture.

Two properties the rest of the agent depends on:

* **Determinism.** Three things together, none sufficient alone: rows are sorted into a canonical
  order before fitting, so the result cannot depend on the order the database returned them;
  k-means++ draws from a seeded generator and the fit restarts `N_INIT` times, keeping the lowest
  inertia; and clusters are relabelled by size afterwards, because otherwise the *partition* is
  stable while the cluster *ids* shuffle between runs and every regression diff becomes noise.
* **Honesty about near-ties.** On real liking data the objective can be almost flat across quite
  different partitions -- measured on this repo's Herbalife survey, 197/140/113 and 226/112/112
  sat 0.04% apart in inertia. `stability` reports the mean share of respondents that restarts
  place in the same cluster as the winning solution, so a near-degenerate partition is visible
  rather than presented as the only one.
* **Descriptive, not inferential.** `separation_d` and `core_range_lift` describe a partition this
  module just constructed; they are not tests against a null hypothesis. Nothing here produces a
  p-value, and the reporting rules forbid the model from implying one.
"""

from __future__ import annotations

import numpy as np

DEFAULT_K = 3
DEFAULT_SEED = 0
N_INIT = 50          # single-init k-means is seed-fragile even when seeded
MAX_ITER = 100
NEAR_OPTIMAL = 0.01  # inertia within 1% of the best counts as a competing solution

# Cohen's conventional large/medium cut-points. They are echoed into the payload so the model
# reads the threshold that was actually applied instead of inventing its own.
DEFINING_D = 0.8
SECONDARY_D = 0.5


def _standardize(matrix: np.ndarray) -> np.ndarray:
    """Z-score each column. A zero-variance attribute is left at 0 and contributes nothing."""
    spread = matrix.std(axis=0)
    spread[spread == 0] = 1.0
    return (matrix - matrix.mean(axis=0)) / spread


def _kmeans_plus_plus(points: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    centroids = [points[int(rng.integers(len(points)))]]
    for _ in range(k - 1):
        gap = np.min(((points[:, None, :] - np.array(centroids)[None]) ** 2).sum(-1), axis=1)
        total = gap.sum()
        # Every point already sits on a centroid: any further pick is arbitrary, so take the
        # first one rather than dividing by zero.
        probabilities = gap / total if total > 0 else None
        index = int(rng.choice(len(points), p=probabilities))
        centroids.append(points[index])
    return np.array(centroids)


def _lloyd(points: np.ndarray, centroids: np.ndarray) -> tuple[np.ndarray, float]:
    labels = np.zeros(len(points), dtype=int)
    for _ in range(MAX_ITER):
        labels = np.argmin(((points[:, None, :] - centroids[None]) ** 2).sum(-1), axis=1)
        moved = np.array([
            points[labels == index].mean(axis=0) if (labels == index).any() else centroids[index]
            for index in range(len(centroids))
        ])
        if np.allclose(moved, centroids):
            break
        centroids = moved
    inertia = float(((points - centroids[labels]) ** 2).sum())
    return labels, inertia


def fit_kmeans(
    points: np.ndarray, k: int, seed: int = DEFAULT_SEED
) -> tuple[np.ndarray, float]:
    """Cluster labels in canonical order, plus the share of restarts that reproduced them.

    Rows are fitted in a canonical sort order and mapped back afterwards. k-means++ seeds its
    draws from the row order, so without this the same respondents in a different order can land
    on a different one of several near-tied optima.
    """
    order = np.lexsort(tuple(points[:, column] for column in reversed(range(points.shape[1]))))
    sorted_points = points[order]

    solutions = []
    for restart in range(N_INIT):
        rng = np.random.default_rng(seed + restart)
        labels, inertia = _lloyd(sorted_points, _kmeans_plus_plus(sorted_points, k, rng))
        solutions.append((inertia, _canonical_labels(labels, sorted_points, k)))

    best_inertia, best_labels = min(solutions, key=lambda item: item[0])
    # Mean share of respondents that a COMPETING restart puts in the same canonical cluster as the
    # winner. Only restarts within NEAR_OPTIMAL of the best inertia count: a restart that landed
    # 20% worse is a failed search `min` has already discarded, not a rival answer, and averaging
    # those in would measure how often k-means++ succeeds rather than whether the winning
    # partition is well determined. Exact-partition agreement is no use either -- one respondent
    # moving breaks it -- so this is a per-respondent share.
    rivals = [
        labels for inertia, labels in solutions
        if inertia <= best_inertia * (1.0 + NEAR_OPTIMAL)
    ]
    stability = float(np.mean([np.mean(labels == best_labels) for labels in rivals]))

    restored = np.empty_like(best_labels)
    restored[order] = best_labels
    return restored, round(float(stability), 4)


def _canonical_labels(labels: np.ndarray, points: np.ndarray, k: int) -> np.ndarray:
    """Size descending, ties broken on the centroid vector, so ids survive a re-run."""
    def sort_key(index: int) -> tuple:
        member = labels == index
        centroid = tuple(points[member].mean(axis=0)) if member.any() else ()
        return (-int(member.sum()), centroid)

    remap = {old: new for new, old in enumerate(sorted(range(k), key=sort_key))}
    return np.array([remap[int(label)] for label in labels])


def _cohens_d(inside: np.ndarray, outside: np.ndarray) -> float:
    """Standardized mean difference of a cluster against the rest of the sample.

    Where both groups are internally constant -- one cluster all 8s, the rest all 2s -- the
    pooled within-group SD is 0 while the groups are as separated as they can be. Dividing by it
    would report total separation as zero, so the attribute's own overall SD stands in. That
    fallback fires only in that degenerate case; a genuinely constant attribute still scores 0.
    """
    if len(inside) < 2 or len(outside) < 2:
        return 0.0
    pooled = np.sqrt(
        ((len(inside) - 1) * inside.var(ddof=1) + (len(outside) - 1) * outside.var(ddof=1))
        / (len(inside) + len(outside) - 2)
    )
    if pooled == 0:
        pooled = float(np.concatenate([inside, outside]).std())
    if pooled == 0:
        return 0.0
    return float((inside.mean() - outside.mean()) / pooled)


def _attribute_profile(values: np.ndarray, member: np.ndarray, attribute: str) -> dict:
    """The cluster's dominant area on one feature dimension, plus how well it separates."""
    inside, outside = values[member], values[~member]
    low, high = (float(bound) for bound in np.percentile(inside, [25, 75]))
    separation = _cohens_d(inside, outside)
    # How much likelier the cluster's own core range is to hold its members than non-members.
    # Undefined rather than infinite when no outsider falls in the range at all.
    outside_share = float(((outside >= low) & (outside <= high)).mean()) if len(outside) else 0.0
    inside_share = float(((inside >= low) & (inside <= high)).mean())
    lift = inside_share / outside_share if outside_share > 0 else None
    # A null lift is the strongest separation there is, not a missing measurement, so the share it
    # divides by is reported alongside it: 0.0 means no non-member scored inside the core range.

    strength = abs(separation)
    role = "defining" if strength >= DEFINING_D else (
        "secondary" if strength >= SECONDARY_D else "shared"
    )
    return {
        "attribute": attribute,
        "mean": round(float(inside.mean()), 4),
        "sd": round(float(inside.std(ddof=1)), 4) if len(inside) > 1 else 0.0,
        "median": round(float(np.median(inside)), 4),
        "core_range": [round(low, 4), round(high, 4)],
        "full_range": [round(float(inside.min()), 4), round(float(inside.max()), 4)],
        "separation_d": round(separation, 4),
        "core_range_lift": round(lift, 4) if lift is not None else None,
        "core_range_outside_share": round(outside_share, 4),
        "role": role,
        "direction": "above" if separation > 0 else ("below" if separation < 0 else "flat"),
    }


def segment_profiles(
    matrix: np.ndarray,
    attributes: list[str],
    k: int = DEFAULT_K,
    seed: int = DEFAULT_SEED,
    companion: dict | None = None,
    products: list[str] | None = None,
) -> dict:
    """Cluster respondents on their standardized rating profile and describe each cluster.

    `matrix` is respondents x attributes of raw scale values. `companion` is an optional
    {"label": str, "values": np.ndarray} measure reported per cluster but never clustered on.
    """
    labels, stability = fit_kmeans(_standardize(matrix), k, seed)

    clusters = []
    for index in range(k):
        member = labels == index
        cluster = {
            "cluster_id": index,
            "n": int(member.sum()),
            "share": round(float(member.mean()), 4),
            "attributes": [
                _attribute_profile(matrix[:, position], member, attribute)
                for position, attribute in enumerate(attributes)
            ],
        }
        cluster["defining_attributes"] = [
            profile["attribute"] for profile in
            sorted(cluster["attributes"], key=lambda item: -abs(item["separation_d"]))
            if profile["role"] == "defining"
        ]
        if companion is not None:
            # mean, sd and n together: REPORTING_RULES requires the spread beside every mean, and
            # a prompt rule cannot reach a figure the tool never emitted.
            values = companion["values"][member]
            cluster[companion["label"]] = {
                "mean": round(float(values.mean()), 4),
                "sd": round(float(values.std(ddof=1)), 4) if len(values) > 1 else 0.0,
                "n": int(len(values)),
            } if member.any() else None
        if products is not None:
            composition: dict[str, int] = {}
            for product, in_cluster in zip(products, member, strict=True):
                if in_cluster:
                    composition[product] = composition.get(product, 0) + 1
            cluster["product_composition"] = dict(sorted(composition.items()))
        clusters.append(cluster)

    return {
        "k": k,
        "seed": seed,
        "n_respondents": int(len(matrix)),
        "stability": stability,
        "basis_attributes": list(attributes),
        "role_thresholds": {"defining": DEFINING_D, "secondary": SECONDARY_D},
        "clusters": clusters,
    }
