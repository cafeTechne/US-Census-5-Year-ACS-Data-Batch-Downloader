"""End-to-end tests for the Round 8 inference workflows (MCP/CLI layer)."""

import os
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

from census import workflows_inference as w


class TestInferenceWorkflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        rng = np.random.default_rng(0)
        n = 60
        den = rng.integers(800, 3000, n).astype(float)
        num = np.round(den * rng.uniform(0.05, 0.3, n))
        rent = rng.normal(1200, 200, n).round()
        df = pd.DataFrame({
            "GEOID": [f"36061{i:06d}" for i in range(n)], "NAME": "x",
            "B17001_001E": den, "B17001_001M": den * 0.05,
            "B17001_002E": num, "B17001_002M": np.sqrt(num) * 3,
            "B25064_001E": rent, "B25064_001M": rent * 0.08,
        })
        cls.input = os.path.join(cls.dir, "in.csv")
        df.to_csv(cls.input, index=False)
        res = w.build_analysis_frame(
            cls.input, variables=["B25064_001E"],
            derive=[{"name": "poverty_rate", "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}],
            output_path=os.path.join(cls.dir, "af.parquet"),
        )
        assert res["success"], res
        cls.frame = res["output_path"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def assertGuided(self, res):
        self.assertTrue(res["success"], res)
        self.assertTrue(res["summary"])
        self.assertTrue(res["next_step"])

    def test_models_and_figures(self):
        fig_dir = os.path.join(self.dir, "fig")
        res = w.fit_model(self.frame, "poverty_rate ~ B25064_001", figure_dir=fig_dir)
        self.assertGuided(res)
        self.assertIn("model", res["uncertainty_sources"])
        self.assertTrue(any("No replicates" in n for n in res["notes"]))
        self.assertTrue(os.path.exists(os.path.join(fig_dir, "coefficients.pdf")))
        sae = w.small_area_estimates(self.frame, "poverty_rate ~ B25064_001", "poverty_rate_se")
        self.assertGuided(sae)
        corr = w.correlation_matrix(self.frame, ["poverty_rate", "B25064_001"], disattenuate=True)
        self.assertGuided(corr)

    def test_bad_inputs_have_error_codes(self):
        self.assertEqual(w.fit_model(self.frame, "y ~ x", method="magic")["error_code"], "INVALID_INPUT")
        self.assertEqual(w.correlation_matrix(self.frame, ["poverty_rate"])["error_code"], "INVALID_INPUT")
        self.assertEqual(w.make_figure("pie", self.frame)["error_code"], "INVALID_INPUT")

    def test_planning_trend_figure_table(self):
        self.assertGuided(w.minimum_detectable_difference(2.1, 2.4))
        trend = w.trend_test([2013, 2018, 2023], [14.1, 13.0, 11.8], [0.4, 0.4, 0.3])
        self.assertGuided(trend)
        overlapping = w.trend_test([2021, 2022, 2023], [1, 2, 3], [0.1, 0.1, 0.1])
        self.assertFalse(overlapping["success"])
        self.assertGuided(w.reliability_profile(self.frame))
        fig = w.make_figure("estimates", self.frame, os.path.join(self.dir, "f2"), label_col="GEOID",
                            estimate_col="poverty_rate", se_col="poverty_rate_se")
        self.assertGuided(fig)
        tab = w.make_table("descriptive", self.frame, os.path.join(self.dir, "t"), variables=["poverty_rate"])
        self.assertGuided(tab)


if __name__ == "__main__":
    unittest.main()
