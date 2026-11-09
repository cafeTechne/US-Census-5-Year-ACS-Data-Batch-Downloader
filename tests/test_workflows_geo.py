"""Offline end-to-end tests for the Round 9 geography workflows (DC tract fixture)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

FIXTURE_GEO = os.path.join(os.path.dirname(__file__), "fixtures", "geo")


@patch.dict(os.environ, {"CENSUS_GEO_DIR": FIXTURE_GEO})
class TestGeoWorkflows(unittest.TestCase):
    def setUp(self):
        from census import boundaries

        self.dir = tempfile.mkdtemp()
        with patch.dict(os.environ, {"CENSUS_GEO_DIR": FIXTURE_GEO}):
            b = boundaries.get_boundaries("tract", 2023, state="11")
        rng = np.random.default_rng(1)
        n = len(b)
        df = pd.DataFrame({"GEOID": b["GEOID"].astype(str), "rate": rng.uniform(5, 30, n)})
        df["rate_se"] = df["rate"] * rng.uniform(0.05, 0.4, n)
        df["x"] = df["rate"] * 0.5 + rng.normal(0, 3, n)
        self.path = os.path.join(self.dir, "f.csv")
        df.to_csv(self.path, index=False)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def guided(self, res):
        self.assertTrue(res["success"], res)
        self.assertTrue(res["summary"])
        self.assertTrue(res["next_step"])
        json.dumps(res)

    def test_maps(self):
        from census import workflows_geo as g

        res = g.make_map(self.path, "rate", "tract", 2023, se_col="rate_se", output_dir=os.path.join(self.dir, "m"))
        self.guided(res)
        self.assertIn("class separability", res["caption"])
        self.assertIn("2023 cartographic boundary", res["caption"])
        self.guided(g.make_map(self.path, "rate", "tract", 2023, kind="uncertainty", se_col="rate_se",
                               output_dir=os.path.join(self.dir, "m")))
        self.assertEqual(g.make_map(self.path, "rate", "tract", 2023, kind="pie")["error_code"], "INVALID_INPUT")

    def test_spatial(self):
        from census import workflows_geo as g

        res = g.spatial_autocorrelation(self.path, "rate", "tract", 2023, local=True,
                                        output_path=os.path.join(self.dir, "lisa.csv"))
        self.guided(res)
        # PLAN_ROUND_15 B3: measured caveats reach the tool output
        self.assertIn("local_morans_size", " ".join(res["local_notes"]))
        res = g.spatial_regression(self.path, "rate ~ x", "tract", 2023, model="error")
        self.guided(res)
        self.assertIn("understates the true lambda", " ".join(res["notes"]))
        res = g.spatial_regression(self.path, "rate ~ x", "tract", 2023)  # lag default: GMM
        self.guided(res)
        self.assertIn("GMM", res["method"])
        self.assertIn("the default for the lag model", " ".join(res["notes"]))
        # PLAN_ROUND_16 A2: effects come before the coefficients, and beta is labelled as not the marginal effect
        keys = list(res)
        self.assertLess(keys.index("effects"), keys.index("coefficients"))
        self.assertEqual({r["effect"] for r in res["effects"]}, {"direct", "indirect", "total"})
        self.assertIn("NOT its marginal effect", res["notes"][0])
        self.assertIn("effects table first", res["next_step"])
        res = g.spatial_regression(self.path, "rate ~ x", "tract", 2023, method="ml")
        self.guided(res)
        self.assertIn("ML lag intervals for rho covered 0.48", " ".join(res["notes"]))
        self.assertFalse(g.spatial_regression(self.path, "rate ~ x", "tract", 2023, method="ols")["success"])
        # PLAN_ROUND_16 A1 option: lambda corrected for independent outcome sampling error
        res = g.spatial_regression(self.path, "rate ~ x", "tract", 2023, model="error", correct_outcome_error=True,
                                   se_col="rate_se")
        self.guided(res)
        self.assertIn("known outcome sampling variances", res["method"])
        self.assertIn("independent across areas", " ".join(res["notes"]))
        self.assertIn("corrected lambda", res["next_step"])
        self.assertIn("lambda_uncorrected", res["diagnostics"]["outcome_error_correction"])
        self.assertFalse(res["diagnostics"]["controlled_total_warning"])
        flagged = g.spatial_regression(self.path, "rate ~ x", "tract", 2023, model="error",
                                       correct_outcome_error=True, se_col="rate_se", controlled_total=True)
        self.assertTrue(flagged["diagnostics"]["controlled_total_warning"])
        fail = g.spatial_regression(self.path, "rate ~ x", "tract", 2023, model="error", correct_outcome_error=True)
        self.assertEqual(fail["error_code"], "INVALID_INPUT")
        self.assertIn("needs se_col", json.dumps(fail))
        self.guided(g.spatial_small_area_estimates(self.path, "rate ~ x", "rate_se", "tract", 2023,
                                                   output_path=os.path.join(self.dir, "sfh.csv")))
        self.assertFalse(g.spatial_regression(self.path, "rate ~ nope", "tract", 2023)["success"])


if __name__ == "__main__":
    unittest.main()
