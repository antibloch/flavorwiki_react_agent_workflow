"""Reference K-Means and persona-overlap implementation for the attribute-to-KPI workflow.

SUPPLIED BY THE ANALYST as the starting point for `cluster_attributes` and
`label_persona_clusters` (see new_tool.md §16). Kept verbatim so the intended behaviour is
unambiguous. It is NOT wired into the agent and nothing imports it yet.

Verified against the local sample DB on Tasting Survey e14528a9-01ac-4dff-844e-0914dbdb3759
(7 attributes, 1,200 rows). Reproducible with a fixed random_state; cross-seed best-permutation
agreement 0.998 at K=4 and 0.993 at K=5. Cluster sizes at random_state=20260819:
K=4 -> [455, 355, 160, 230], K=5 -> [81, 317, 412, 171, 219].

THREE DEFECTS were reproduced and must be fixed before this is used in the workflow.
They are marked DEFECT-1/2/3 below and specified in new_tool.md §13.
"""

import numpy as np

__all__ = ['kmeans_clustering', 'maximum_overlap_analysis']


def kmeans_clustering(
    X: np.ndarray,
    k: int,
    *,
    random_state: int | None = None,
    n_init: int = 10,
    max_iter: int = 300,
    tol: float = 1e-4,
) -> np.ndarray:
    """Return one anonymous K-Means cluster label for each row of X.

    NOTE: `random_state=None` must never be used by the workflow -- pass and report a seed
    (new_tool.md §13).
    """
    X = np.asarray(X, dtype=float)
    n, d = X.shape
    rng = np.random.default_rng(random_state)
    best_labels = None
    best_inertia = np.inf

    for _ in range(n_init):
        centroids = np.empty((k, d))
        centroids[0] = X[rng.integers(n)]
        closest_sq = np.sum((X - centroids[0]) ** 2, axis=1)

        for centroid_index in range(1, k):
            # DEFECT-3 (new_tool.md §13.3): float `==` is a fragile "already chosen" test, and
            # this broadcast materializes an n x centroid_index x d array. Fine at n=1,200 x d=7;
            # replace with a set of chosen row indices before running at ~50k rows.
            represented = np.any(
                np.all(
                    X[:, None, :] == centroids[None, :centroid_index, :],
                    axis=2,
                ),
                axis=1,
            )
            candidates = np.flatnonzero(~represented)
            weights = closest_sq[candidates]
            if np.max(weights) == 0:
                point_index = rng.choice(candidates)
            else:
                weights = weights / np.max(weights)
                point_index = rng.choice(candidates, p=weights / weights.sum())
            centroids[centroid_index] = X[point_index]
            new_sq = np.sum((X - centroids[centroid_index]) ** 2, axis=1)
            closest_sq = np.minimum(closest_sq, new_sq)

        for _ in range(max_iter):
            distances = np.sum(
                (X[:, None, :] - centroids[None, :, :]) ** 2,
                axis=2,
            )
            labels = np.argmin(distances, axis=1)
            errors = distances[np.arange(n), labels]
            counts = np.bincount(labels, minlength=k)

            for empty_cluster in np.flatnonzero(counts == 0):
                for point_index in np.argsort(-errors, kind='stable'):
                    donor = labels[point_index]
                    if counts[donor] > 1:
                        labels[point_index] = empty_cluster
                        counts[donor] -= 1
                        counts[empty_cluster] += 1
                        break

            updated = np.vstack(
                [np.mean(X[labels == cluster], axis=0) for cluster in range(k)]
            )
            movement = np.max(np.linalg.norm(updated - centroids, axis=1))
            centroids = updated
            if movement <= tol:
                break

        distances = np.sum(
            (X[:, None, :] - centroids[None, :, :]) ** 2,
            axis=2,
        )
        labels = np.argmin(distances, axis=1)
        errors = distances[np.arange(n), labels]
        counts = np.bincount(labels, minlength=k)

        for empty_cluster in np.flatnonzero(counts == 0):
            for point_index in np.argsort(-errors, kind='stable'):
                donor = labels[point_index]
                if counts[donor] > 1:
                    labels[point_index] = empty_cluster
                    counts[donor] -= 1
                    counts[empty_cluster] += 1
                    break

        final_centroids = np.vstack(
            [np.mean(X[labels == cluster], axis=0) for cluster in range(k)]
        )
        inertia = np.sum((X - final_centroids[labels]) ** 2)
        if inertia < best_inertia:
            best_labels = labels.copy()
            best_inertia = inertia

    return best_labels


def maximum_overlap_analysis(
    X: np.ndarray,
    labels: np.ndarray,
    predefined_sections: dict[
        object,
        tuple[np.ndarray, np.ndarray],
    ],
) -> dict[str, object]:
    """Map anonymous clusters to persona IDs using inclusive point containment.

    `predefined_sections` maps a persona id to (lower[], upper[]) bounds over the SAME columns
    and domain as `X`. The workflow resolves those bounds from quantile specifications
    tie-aware (new_tool.md §10 G, §11.4) -- never from raw or z-score numbers.
    """
    X = np.asarray(X, dtype=float)
    labels = np.asarray(labels)
    cluster_ids = np.unique(labels)
    persona_ids = tuple(predefined_sections)
    bounds = [
        (np.asarray(lower, dtype=float), np.asarray(upper, dtype=float))
        for lower, upper in predefined_sections.values()
    ]
    overlap_matrix = np.empty((len(cluster_ids), len(persona_ids)))

    for row, cluster_id in enumerate(cluster_ids):
        cluster_points = X[labels == cluster_id]
        for column, (lower, upper) in enumerate(bounds):
            inside = np.all(
                (cluster_points >= lower) & (cluster_points <= upper),
                axis=1,
            )
            overlap_matrix[row, column] = np.mean(inside)

    # DEFECT-1 (new_tool.md §13.1): np.argmax on an all-zero row returns index 0, so when no
    # section contains any of a cluster's points every cluster is silently assigned the FIRST
    # persona. Reproduced with two deliberately empty boxes. Required fix: when
    # overlap_matrix[row].max() == 0 (or < a minimum threshold), mark the cluster `unassigned`
    # and report it -- an empty region is a section-proposal error and must surface as one.
    #
    # DEFECT-2 (new_tool.md §13.2): each row's argmax is independent, so several clusters can
    # collapse onto one persona. Observed on real data: tercile boxes at K=4 mapped clusters 2
    # AND 3 to 'all_low'. Required fix: permit many-to-one only with an explicit report, or run a
    # one-to-one assignment maximizing total overlap. A persona holding two clusters makes the
    # per-persona PLSR readout uninterpretable.
    cluster_to_persona = {
        int(cluster_id): persona_ids[np.argmax(overlap_matrix[row])]
        for row, cluster_id in enumerate(cluster_ids)
    }
    return {
        'cluster_to_persona': cluster_to_persona,
        'overlap_matrix': overlap_matrix,
        'cluster_ids': cluster_ids,
        'persona_ids': persona_ids,
    }
