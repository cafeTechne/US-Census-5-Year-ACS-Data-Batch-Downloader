# SPDX-License-Identifier: AGPL-3.0-or-later
"""PLAN_ROUND_16 workstream B: building blocks and registration of the LISA validation studies."""

import importlib.util
import math
import os
import unittest

import numpy as np
import pandas as pd
from scipy import stats as sps

from census import lisa_validation as lv
from census.simulation import FAST, STUDIES, SPATIAL_STUDIES, group_config, lattice_weights
from census.variance import REPLICATE_FACTOR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script():
    spec = importlib.util.spec_from_file_location("inference_validation_lv", os.path.join(ROOT, "scripts",
                                                                                          "inference_validation.py"))
    iv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(iv)
    return iv


class TestErrorCorrelation(unittest.TestCase):
    W = lattice_weights(lv.LV_SIDE).full()[0]
    A = lv.rook_adjacency(lv.LV_SIDE)

    def test_rook_adjacency_matches_lattice_weights(self):
        np.testing.assert_array_equal(self.A, (self.W > 0).astype(float))

    def test_neighbour_correlation_hits_each_target(self):
        for r1 in lv.LV_NEIGHBOUR_CORRS:
            st = lv.error_correlation(self.W, self.A, r1)
            self.assertAlmostEqual(st["r1"], r1, places=6)
            np.testing.assert_allclose(np.diag(st["R"]), 1.0)
            np.testing.assert_allclose(st["L"] @ st["L"].T, st["R"], atol=1e-10)
        self.assertEqual(lv.error_correlation(self.W, self.A, 0.0)["lam"], 0.0)

    def test_negative_neighbour_correlation_is_a_checkerboard(self):
        st = lv.error_correlation(self.W, self.A, -0.2)
        self.assertLess(st["lam"], 0)
        self.assertGreater(st["r2"], 0)  # second-order neighbours positively correlated

    def test_replicates_follow_acs_scaling_and_the_error_correlation(self):
        st = lv.error_correlation(self.W, self.A, 0.5)
        rng = np.random.default_rng(3)
        se = rng.uniform(2, 9, self.W.shape[0])
        dev = lv.correlated_replicates(se, st["L"], rng.standard_normal((len(se), 80)))
        np.testing.assert_allclose(np.sqrt(REPLICATE_FACTOR * (dev ** 2).sum(axis=1)), se, rtol=1e-12)
        # replicate covariance 4/80 sum d d' reproduces the neighbour correlation on average
        C = REPLICATE_FACTOR * dev @ dev.T
        R = C / np.outer(se, se)
        self.assertAlmostEqual(lv.mean_neighbour_correlation(R, self.A), 0.5, delta=0.05)


class TestWong(unittest.TestCase):
    # a 1-D chain 0-1-2-3
    NB = [[1], [0, 2], [1, 3], [2]]

    def test_join_rule_is_p_at_least_cl_as_in_the_worked_example(self):
        # Wong (2021) sec. 5.1: Louisiana joins Mississippi's hot spot at CL 0.648 (t-test p = 0.65)
        y = np.array([35.5, 35.5 - sps.norm.isf(0.65 / 2) * math.sqrt(2), 0.0, 0.0])
        se = np.ones(4)
        lab, seeds, seed_of = lv.wong_hot_cold_spots(y, se, self.NB, high=35.0, low=-100.0, confidence=0.648)
        self.assertEqual(list(lab[:2]), ["hot", "hot"])
        self.assertEqual(list(seeds), [True, False, False, False])
        self.assertEqual(list(seed_of), [0, 0, -1, -1])
        lab, _, _ = lv.wong_hot_cold_spots(y, se, self.NB, high=35.0, low=-100.0, confidence=0.66)
        self.assertEqual(list(lab[:2]), ["hot", ""])  # a higher CL needs a larger p-value to join

    def test_growth_compares_with_the_seed_not_the_member(self):
        y = np.array([10.0, 9.7, 9.4, 8.4])  # 2 is close to 1 (p 0.67) but not to the seed 0 (p 0.40)
        lab, _, _ = lv.wong_hot_cold_spots(y, np.full(4, 0.5), self.NB, high=9.9, low=-1.0, confidence=0.5)
        self.assertEqual(list(lab), ["hot", "hot", "", ""])

    def test_cold_spots_and_no_seeds(self):
        y = np.array([1.0, 1.1, 5.0, 5.0])
        lab, _, seed_of = lv.wong_hot_cold_spots(y, np.full(4, 0.5), self.NB, high=9.0, low=2.0, confidence=0.5)
        self.assertEqual(list(lab), ["cold", "cold", "", ""])
        self.assertEqual(list(seed_of), [0, 1, -1, -1])  # both are seeds
        lab, seeds, _ = lv.wong_hot_cold_spots(y, np.full(4, 0.5), self.NB, high=9.0, low=0.0, confidence=0.5)
        self.assertFalse(seeds.any() or (lab != "").any())


class TestWongMetrics(unittest.TestCase):
    def test_scored_against_latent_elevation(self):
        theta = np.array([12.0, 11.0, 4.0, 0.0, 5.0])
        labels = np.array(["hot", "hot", "hot", "", ""])
        seeds = np.array([True, False, False, False, False])
        seed_of = np.array([0, 0, 0, -1, -1])
        m = lv.wong_metrics(labels, seeds, seed_of, theta, high=10.0, low=-5.0, truth=np.zeros(5, bool))
        self.assertEqual(m["seed_precision"], 1.0)
        self.assertEqual(m["exceed_recall"], 1.0)        # both areas with theta > 10 flagged hot
        self.assertEqual(m["growth_per_seed"], 2.0)
        self.assertEqual(m["member_beyond_share"], 0.5)  # area 1 is beyond 10, area 2 is not
        self.assertEqual(m["member_near_share"], 0.5)    # |11 - 12| <= LV_WONG_NEAR, |4 - 12| is not
        self.assertNotIn("fpr", m)
        self.assertNotIn("power", m)


class TestMetrics(unittest.TestCase):
    NB = [[1], [0, 2], [1, 3], [2]]

    def test_null_map(self):
        flag = np.array([True, True, False, True])
        lab = np.array(["HH", "HH", "LL", "LH"])
        m = lv.map_metrics(flag, lab, np.zeros(4, bool), "HH", ("HH", "LL"), self.NB)
        self.assertAlmostEqual(m["fpr"], 0.75)
        self.assertEqual(m["fdr"], 1.0)
        self.assertEqual(m["false_cluster_map_k2"], 1.0)  # areas 0-1 (HH) form a component of size 2
        self.assertEqual(m["false_cluster_map_k4"], 0.0)
        self.assertTrue(math.isnan(m["power"]))

    def test_cluster_map(self):
        truth = np.array([False, True, True, False])
        flag = np.array([False, True, True, True])
        lab = np.array(["HH", "HH", "LL", "HH"])
        m = lv.map_metrics(flag, lab, truth, "HH", ("HH", "LL"), self.NB, halo=np.array([True, False, False, True]))
        self.assertAlmostEqual(m["fpr"], 0.5)
        self.assertAlmostEqual(m["fdr"], 1 / 3)
        self.assertAlmostEqual(m["power"], 0.5)          # only area 1 is flagged with the correct label
        self.assertAlmostEqual(m["jaccard"], 1 / 3)      # {1} vs {1, 2} plus false area 3: |{1}| / |{1, 2, 3}|
        self.assertEqual(m["false_cluster_map_k2"], 0.0)  # the null area 3 is a single-area component
        self.assertEqual(m["halo_share"], 1.0)

    def test_misspecified_weights_keep_the_rook_degrees(self):
        ws = lv.analysis_weights(6, [f"{i:05d}" for i in range(36)], seed=1)
        rook, mis = ws["rook"], ws["misspecified"]
        self.assertEqual(sorted(rook.cardinalities.values()), sorted(mis.cardinalities.values()))
        for i, nb in mis.neighbors.items():
            for j in nb:
                self.assertIn(i, mis.neighbors[j])
        self.assertNotEqual(rook.neighbors, mis.neighbors)
        self.assertEqual(set(ws), {"rook", "queen", f"knn{lv.LV_KNN}", "misspecified"})


class TestRegistration(unittest.TestCase):
    """Info studies run on demand only (--studies NAME): not in the default report, never required."""

    def test_registered_on_demand_only(self):
        iv = _script()
        for name in lv.LISA_STUDIES:
            self.assertIn(name, STUDIES)
            self.assertNotIn(name, SPATIAL_STUDIES)
            self.assertNotIn(name, iv.REQUIRED_STUDIES)
            self.assertNotIn(name, iv.DEFAULT_DESIGN_STUDIES)
            self.assertGreater(lv.LISA_REPS_FULL[name], 0)
        cfg = group_config("lisa", True)
        self.assertEqual({k for k, v in cfg.reps.items() if v > 0}, set(lv.LISA_STUDIES))

    def test_tiny_runs_are_info_only_and_seeded(self):
        # a 6 x 6 lattice keeps the dense 144-area algebra out of the unit tests
        a = pd.DataFrame(lv.study_lisa_validation_clusters(FAST, 1, side=6))
        b = pd.DataFrame(lv.study_lisa_validation_clusters(FAST, 1, side=6))
        self.assertEqual(a.to_csv(index=False), b.to_csv(index=False))
        self.assertEqual(set(a["status"]), {"info"})
        self.assertEqual(set(a["lv_method"]), {"lisa", "lisa_stable", "stable_only", "lisa_eblup", "wong_cl0.5",
                                               "wong_cl0.75"})
        self.assertEqual(set(a["scenario"]), {"no_sampling_error", "cluster_independent", "cluster_correlated"})
        self.assertEqual(set(a.loc[a["lv_method"] == "lisa", "weights"]),
                         {"rook", "queen", f"knn{lv.LV_KNN}", "misspecified"})
        iv = _script()
        self.assertTrue(iv.gate(a.assign(study="x"), ())[0])
        null = pd.DataFrame(lv.study_lisa_validation_null(FAST, 1, side=6))
        self.assertEqual(set(null["scenario"]), {"no_sampling_error", "independent_homogeneous",
                                                 "heterogeneous_independent", "correlated"})
        self.assertTrue(null.loc[null["metric"] == "power", "value"].empty)  # no true clusters


if __name__ == "__main__":
    unittest.main()
