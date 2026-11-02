"""Regression tests for findings from the PR #1 code review."""

import math
import os
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

from census.aggregate import aggregate_geographies, aggregate_then_derive
from census.inflation import boundary_shift_advisory
from census.medians import median_for_group, median_from_distribution
from census.multitest import batch_z_test
from census.stats import z_test
from census.variance import REPLICATE_COLUMNS

INF = float("inf")


def _rep_frame(rows):
    """rows: (GEOID, variable, estimate, moe, replicate values (scalar or 80-list))."""
    out = []
    for geoid, var, est, moe, reps in rows:
        reps = [reps] * 80 if np.isscalar(reps) else list(reps)
        out.append({"GEOID": geoid, "NAME": geoid, "variable": var, "estimate": est, "moe": moe,
                    **dict(zip(REPLICATE_COLUMNS, reps))})
    return pd.DataFrame(out)


class TestMedianReplicateSelection(unittest.TestCase):
    BRACKETS = [("T_002", 0, 100, "a"), ("T_003", 100, 200, "b"), ("T_004", 200, INF, "c")]

    def test_replicates_restricted_to_selected_rows(self):
        wide = pd.DataFrame({"GEOID": ["01"], "T_002E": [5.0], "T_003E": [10.0], "T_004E": [5.0]})
        reps = _rep_frame(
            [("01", "T_002", 5, 3, 5), ("01", "T_003", 10, 3, 10), ("01", "T_004", 5, 3, 5)]
            + [("99", v, 1000, 3, 1000) for v in ("T_002", "T_003", "T_004")]  # other tracts in the state file
        )
        res = median_for_group(wide, "T", brackets=self.BRACKETS, replicate_frame=reps)
        self.assertAlmostEqual(res.estimate, 150.0)
        self.assertAlmostEqual(res.moe, 0.0)  # replicates equal the estimate for GEOID 01 only

    def test_missing_geoid_in_replicates_raises(self):
        wide = pd.DataFrame({"GEOID": ["01", "02"], "T_002E": [10.0, 5.0], "T_003E": [10.0, 5.0],
                             "T_004E": [0.0, 0.0]})
        reps = _rep_frame([("01", v, 1, 1, 1) for v in ("T_002", "T_003", "T_004")])
        with self.assertRaises(KeyError):
            median_for_group(wide, "T", brackets=self.BRACKETS, replicate_frame=reps)

    def test_large_totals_not_treated_as_exact_boundary(self):
        # Cumulative 1,000,008 vs target 1,000,000: interpolate, do not jump to the gap.
        counts = [1_000_008, 0, 999_992]
        r = median_from_distribution([0, 800, 900], [800, 900, INF], counts)
        self.assertLess(r.estimate, 800.0)


class TestSamePeriodComparisons(unittest.TestCase):
    def test_z_test_same_year_is_not_degenerate(self):
        res = z_test(100, 50, 101, 50, overlap_years=5)
        self.assertTrue(math.isfinite(res["z_statistic"]))
        self.assertFalse(res["significant"])

    def test_batch_same_year(self):
        df = pd.DataFrame({"e1": [100.0], "m1": [50.0], "e2": [101.0], "m2": [50.0]})
        out = batch_z_test(df, "e1", "m1", "e2", "m2", year1=2022, year2=2022)
        self.assertFalse(bool(out["significant_raw"].iloc[0]))
        self.assertEqual(out["overlap_factor"].iloc[0], 1.0)


class TestZTestSentinels(unittest.TestCase):
    def test_controlled_moe_is_zero(self):
        res = z_test(500, -555555555, 400, 16.45)
        self.assertAlmostEqual(res["se_difference"], 10.0)
        self.assertTrue(res["significant"])

    def test_undefined_moe_not_tested(self):
        for bad in (float("nan"), -222222222):
            res = z_test(500, bad, 400, 10)
            self.assertIsNone(res["significant"])
            self.assertTrue(math.isnan(res["p_value"]))


class TestAggregateReplicateEdgeCases(unittest.TestCase):
    def test_all_zero_group_uses_largest_member_moe(self):
        df = pd.DataFrame({"GEOID": ["01", "02", "03"], "B17001_010E": [0, 0, 0], "B17001_010M": [11, 11, 13]})
        reps = _rep_frame([(g, "B17001_010", 0, m, 0) for g, m in (("01", 11), ("02", 11), ("03", 13))])
        out = aggregate_geographies(df, {"01": "g", "02": "g", "03": "g"}, method="replicate",
                                    replicate_frame=reps, assume_additive=["B17001_010E"])
        self.assertEqual(out["B17001_010E"].iloc[0], 0)
        self.assertAlmostEqual(out["B17001_010M"].iloc[0], 13.0)
        self.assertIn("largest published", out["B17001_010_NOTES"].iloc[0])

    def test_zero_replicate_denominator_gives_finite_moe(self):
        den_reps = [5.0] * 79 + [0.0]
        df = pd.DataFrame({"GEOID": ["01"], "B17001_001E": [5], "B17001_001M": [3],
                           "B17001_002E": [2], "B17001_002M": [2]})
        reps = _rep_frame([("01", "B17001_001", 5, 3, den_reps),
                           ("01", "B17001_002", 2, 2, [2.0] * 79 + [0.0])])
        out = aggregate_then_derive(
            df, {"01": "g"}, [{"name": "p", "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}],
            method="replicate", replicate_frame=reps, assume_additive=["B17001_001E", "B17001_002E"],
        )
        self.assertAlmostEqual(out["p_estimate"].iloc[0], 0.4)
        self.assertTrue(np.isfinite(out["p_moe"].iloc[0]))
        expected = 1.645 * math.sqrt(4 / 80 * (0.0 - 0.4) ** 2)
        self.assertAlmostEqual(out["p_moe"].iloc[0], expected)


class TestCodebookSelection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_uses_only_the_panels_own_codebook(self):
        from census.workflows import _load_frame

        pd.DataFrame({"Variable_Code": ["B01001_001E"], "Variable_Slug": ["total_est"]}).to_csv(
            os.path.join(self.dir, "B01001_codebook.csv"), index=False)
        pd.DataFrame({"Variable_Code": ["B19001_001E"], "Variable_Slug": ["total_est"]}).to_csv(
            os.path.join(self.dir, "B19001_codebook.csv"), index=False)
        path = os.path.join(self.dir, "B19001_county_panel_2023.csv")
        pd.DataFrame({"geoid": ["01001"], "year": [2023], "total_est": [10]}).to_csv(path, index=False)
        df = _load_frame(path)
        self.assertIn("B19001_001E", df.columns)
        self.assertNotIn("B01001_001E", df.columns)


class TestConnecticutAdvisory(unittest.TestCase):
    def test_ct_change_flagged_without_2020_crossing(self):
        adv = boundary_shift_advisory(2021, 2023, "county")
        self.assertIn("CONNECTICUT", adv)
        self.assertIsNone(boundary_shift_advisory(2021, 2023, "place"))
        both = boundary_shift_advisory(2019, 2023, "tract")
        self.assertIn("BOUNDARY ADVISORY", both)
        self.assertIn("CONNECTICUT", both)
        self.assertIsNone(boundary_shift_advisory(2022, 2024, "county"))


if __name__ == "__main__":
    unittest.main()
