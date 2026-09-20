"""clustering.segment_profiles -- the invariants funda_agent_exp.py relies on.

No scikit-learn fixture here, unlike tests/test_plsr.py. Lloyd's algorithm has no reference
subtleties worth pinning the way NIPALS PLS did; what can actually break this module is the
determinism contract (seed, restart count, row order, canonical label order) and the profile
arithmetic the model renders. Those are what these cases cover, plus recovery of a planted
partition to show the fit itself works.
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clustering import DEFINING_D, fit_kmeans, segment_profiles


def _planted(seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Three well-separated blobs on a 1-9 scale, with their true membership."""
    rng = np.random.default_rng(seed)
    centres = [(8.0, 8.0, 8.0), (8.0, 3.0, 8.0), (3.0, 8.0, 8.0)]
    blocks, truth = [], []
    for index, centre in enumerate(centres):
        block = np.clip(rng.normal(centre, 0.4, size=(40, 3)), 1, 9)
        blocks.append(block)
        truth.extend([index] * len(block))
    return np.vstack(blocks), np.array(truth)


ATTRS = ["appearance", "flavor", "texture"]


class SegmentProfilesTest(unittest.TestCase):
    def test_recovers_a_planted_partition(self):
        matrix, truth = _planted()
        labels, _ = fit_kmeans(matrix, 3, seed=0)
        # Labels are canonical, not the planting order, so compare the partitions themselves.
        found = {frozenset(np.flatnonzero(labels == index)) for index in range(3)}
        planted = {frozenset(np.flatnonzero(truth == index)) for index in range(3)}
        self.assertEqual(found, planted)

    def test_repeated_calls_are_identical(self):
        matrix, _ = _planted()
        self.assertEqual(
            segment_profiles(matrix, ATTRS, k=3, seed=0),
            segment_profiles(matrix, ATTRS, k=3, seed=0),
        )

    def test_row_order_does_not_change_assignments(self):
        matrix, _ = _planted()
        shuffle = np.random.default_rng(11).permutation(len(matrix))
        labels, _ = fit_kmeans(matrix, 3, seed=0)
        shuffled, _ = fit_kmeans(matrix[shuffle], 3, seed=0)
        np.testing.assert_array_equal(labels[shuffle], shuffled)

    def test_clusters_are_labelled_largest_first(self):
        rng = np.random.default_rng(5)
        matrix = np.vstack([
            rng.normal(8.0, 0.3, size=(60, 3)),
            rng.normal(3.0, 0.3, size=(25, 3)),
        ])
        sizes = [cluster["n"] for cluster in
                 segment_profiles(matrix, ATTRS, k=2, seed=0)["clusters"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_a_separated_partition_is_stable(self):
        matrix, _ = _planted()
        self.assertGreater(segment_profiles(matrix, ATTRS, k=3, seed=0)["stability"], 0.95)

    def test_profile_arithmetic_on_a_hand_checked_case(self):
        # Cluster 0 is the six 8s, cluster 1 the four 2s -- sizes force that canonical order.
        matrix = np.array([[8.0]] * 6 + [[2.0]] * 4)
        result = segment_profiles(matrix, ["flavor"], k=2, seed=0)
        first = result["clusters"][0]["attributes"][0]
        self.assertEqual(first["mean"], 8.0)
        self.assertEqual(first["median"], 8.0)
        self.assertEqual(first["core_range"], [8.0, 8.0])
        self.assertEqual(first["full_range"], [8.0, 8.0])
        self.assertEqual(first["role"], "defining")
        self.assertEqual(first["direction"], "above")
        # Nothing outside the cluster scores inside its core range: total separation, which is
        # reported as an undefined lift over a zero outside share, never as a missing measurement.
        self.assertIsNone(first["core_range_lift"])
        self.assertEqual(first["core_range_outside_share"], 0.0)

    def test_a_shared_attribute_is_not_called_defining(self):
        rng = np.random.default_rng(2)
        split = np.concatenate([np.full(50, 8.0), np.full(50, 2.0)])
        noise = rng.normal(5.0, 1.0, size=100)          # identical in both groups
        matrix = np.column_stack([split, noise])
        result = segment_profiles(matrix, ["flavor", "aroma"], k=2, seed=0)
        for cluster in result["clusters"]:
            roles = {item["attribute"]: item["role"] for item in cluster["attributes"]}
            self.assertEqual(roles["flavor"], "defining")
            self.assertEqual(roles["aroma"], "shared")
            self.assertEqual(cluster["defining_attributes"], ["flavor"])

    def test_role_thresholds_are_published_with_the_result(self):
        matrix, _ = _planted()
        result = segment_profiles(matrix, ATTRS, k=3, seed=0)
        self.assertEqual(result["role_thresholds"]["defining"], DEFINING_D)

    def test_companion_measure_is_reported_but_never_clustered_on(self):
        matrix, truth = _planted()
        companion = np.where(truth == 0, 9.0, 1.0)
        result = segment_profiles(
            matrix, ATTRS, k=3, seed=0,
            companion={"label": "overall_liking_mean", "values": companion},
            products=["A"] * 60 + ["B"] * 60,
        )
        self.assertEqual(result["basis_attributes"], ATTRS)
        for cluster in result["clusters"]:
            # mean alone is not enough: REPORTING_RULES wants the spread and base with it, and
            # the model can only report what the payload carries.
            readout = cluster["overall_liking_mean"]
            self.assertEqual(sorted(readout), ["mean", "n", "sd"])
            self.assertEqual(readout["n"], cluster["n"])
            self.assertEqual(sum(cluster["product_composition"].values()), cluster["n"])

    def test_a_constant_attribute_does_not_divide_by_zero(self):
        matrix, _ = _planted()
        matrix[:, 2] = 7.0
        result = segment_profiles(matrix, ATTRS, k=3, seed=0)
        flat = [item for cluster in result["clusters"]
                for item in cluster["attributes"] if item["attribute"] == "texture"]
        self.assertTrue(all(item["role"] == "shared" for item in flat))


if __name__ == "__main__":
    unittest.main()
