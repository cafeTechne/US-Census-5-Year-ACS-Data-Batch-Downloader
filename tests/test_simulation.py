# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reduced (CI) configuration of the Round 8 inference-validation harness.

The full study (scripts/inference_validation.py) is the acceptance gate; these
tests check the harness itself and that each method is in the right range
with the small, fast configuration (wide tolerances: few replications).
"""

import math
import unittest

import numpy as np

from census.simulation import (
    FAST,
    STUDIES,
    coverage_summary,
    draw_cv,
    make_replicates,
    replicate_long,
    run_study,
)
from census.variance import REPLICATE_FACTOR


class TestHarness(unittest.TestCase):
    def test_replicates_match_target_se_exactly(self):
        rng = np.random.default_rng(1)
        est = rng.normal(100, 10, (50, 2))
        se = rng.uniform(1, 20, (50, 2))
        corr = np.array([[1, 0.6], [0.6, 1]])
        reps = make_replicates(est, se, rng, corr)
        self.assertEqual(reps.shape, (50, 2, 80))
        var = REPLICATE_FACTOR * ((reps - est[:, :, None]) ** 2).sum(axis=2)
        np.testing.assert_allclose(np.sqrt(var), se, rtol=1e-12)
        dev = (reps - est[:, :, None]) / se[:, :, None]
        r = np.corrcoef(dev[:, 0, :].ravel(), dev[:, 1, :].ravel())[0, 1]
        self.assertAlmostEqual(r, 0.6, delta=0.05)
        long = replicate_long([str(i) for i in range(50)], ["a", "b"], est, reps)
        self.assertEqual(len(long), 100)
        self.assertIn("rep_80", long.columns)

    def test_cv_profiles(self):
        cv = draw_cv(20000, "county", np.random.default_rng(0))
        q = np.percentile(cv, [25, 50, 75])
        self.assertAlmostEqual(q[1], 14.3, delta=1.0)  # matches fixture median
        self.assertTrue(4 < q[0] < 8 and 25 < q[2] < 36)

    def test_coverage_summary(self):
        row = coverage_summary("m", "t", [True] * 90 + [False] * 10)
        self.assertAlmostEqual(row["coverage"], 0.9)
        self.assertAlmostEqual(row["mc_se"], math.sqrt(0.09 / 100))
        self.assertEqual(row["status"], "pass")


class TestFastStudy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = run_study(FAST)
        cls.rows = {(r.study, r.method.split(" [")[0], r.target.split(" (")[0]): r
                    for r in cls.df.itertuples()}

    def get(self, study, method_prefix, target_prefix=""):
        for (s, m, t), r in self.rows.items():
            if s == study and m.startswith(method_prefix) and t.startswith(target_prefix):
                return r
        raise KeyError((study, method_prefix, target_prefix))

    def near(self, row, tol_se=3.5, floor=0.03):
        tol = max(tol_se * math.sqrt(0.09 / row.reps), floor)
        self.assertLess(abs(row.coverage - 0.90), tol, f"{row.method} / {row.target}: {row.coverage}")

    def test_all_studies_ran(self):
        expected = {k for k, v in FAST.reps.items() if v > 0}
        self.assertEqual(set(self.df["study"]), expected)
        self.assertTrue(set(STUDIES) >= expected)

    def test_deterministic(self):
        again = run_study(FAST, studies=["correlation", "linear_trend"])
        mine = self.df[self.df["study"].isin(["linear_trend", "correlation"])]
        np.testing.assert_allclose(again["coverage"].to_numpy(), mine["coverage"].to_numpy())

    def test_fit_replicate(self):
        self.near(self.get("fit_replicate", "fit_replicate", "finite-population"))
        # Default 'population' target uses se_model; se_total ('conservative') over-covers.
        self.near(self.get("fit_replicate", "fit_replicate", "superpopulation beta, se_model"))
        self.assertGreaterEqual(self.get("fit_replicate", "fit_replicate", "superpopulation beta, se_total").coverage, 0.85)

    def test_measurement_error_corrections_remove_attenuation(self):
        for study in ("regression_calibration", "simex"):
            corrected = self.get(study, study)
            naive = self.get(study, "naive OLS")
            self.assertLess(naive.bias, -0.05)  # attenuation
            self.assertLess(abs(corrected.bias), abs(naive.bias) / 2, study)
            self.assertGreater(corrected.coverage, naive.coverage)

    def test_fay_herriot(self):
        row = self.get("fay_herriot", "fay_herriot")
        self.assertLess(abs(row.coverage - 0.90), 0.03)
        self.assertLess(abs(row.mse_rel_bias_mean), 0.15)
        self.assertLess(row.eblup_mse_vs_direct, 1.0)  # shrinkage helps

    def test_correlation_and_trend(self):
        self.near(self.get("correlation", "correlation_matrix", "observed-variable"))
        self.near(self.get("linear_trend", "linear_trend_test"))


if __name__ == "__main__":
    unittest.main()
