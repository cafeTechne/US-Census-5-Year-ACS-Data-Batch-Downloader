# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.multitest (p-value corrections and batch Z tests)."""

import math
import unittest

import numpy as np
import pandas as pd

from census.multitest import adjust_pvalues, batch_z_test
from census.stats import z_test

try:
    from statsmodels.stats.multitest import multipletests

    HAVE_STATSMODELS = True
except Exception:  # pragma: no cover - optional dependency
    HAVE_STATSMODELS = False


P = [0.01, 0.04, 0.03, 0.005]


class TestAdjustHandComputed(unittest.TestCase):
    def test_bonferroni(self):
        np.testing.assert_allclose(adjust_pvalues(P, "bonferroni"), [0.04, 0.16, 0.12, 0.02])

    def test_holm(self):
        # sorted 0.005,0.01,0.03,0.04 x 4,3,2,1 = .02,.03,.06,.04 -> cummax .02,.03,.06,.06
        np.testing.assert_allclose(adjust_pvalues(P, "holm"), [0.03, 0.06, 0.06, 0.02])

    def test_bh(self):
        # sorted x m/rank = .02,.02,.04,.04 -> reverse cummin unchanged
        np.testing.assert_allclose(adjust_pvalues(P, "bh"), [0.02, 0.04, 0.04, 0.02])

    def test_by(self):
        c = 1 + 1 / 2 + 1 / 3 + 1 / 4
        np.testing.assert_allclose(adjust_pvalues(P, "by"), np.array([0.02, 0.04, 0.04, 0.02]) * c)

    def test_cap_and_aliases(self):
        np.testing.assert_allclose(adjust_pvalues([0.5, 0.9], "bonferroni"), [1.0, 1.0])
        np.testing.assert_allclose(adjust_pvalues(P, "fdr_bh"), adjust_pvalues(P, "bh"))

    def test_nan_excluded_from_family(self):
        out = adjust_pvalues([0.01, np.nan, 0.04, 0.03, 0.005], "holm")
        self.assertTrue(math.isnan(out[1]))
        np.testing.assert_allclose(np.delete(out, 1), [0.03, 0.06, 0.06, 0.02])

    def test_series_index_kept(self):
        s = pd.Series(P, index=list("abcd"))
        out = adjust_pvalues(s)
        self.assertIsInstance(out, pd.Series)
        self.assertEqual(list(out.index), list("abcd"))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            adjust_pvalues([0.5, 1.2])
        with self.assertRaises(ValueError):
            adjust_pvalues(P, "sidak-ish")

    def test_empty_and_all_nan(self):
        self.assertEqual(adjust_pvalues([]).size, 0)
        self.assertTrue(np.isnan(adjust_pvalues([np.nan, np.nan])).all())


@unittest.skipUnless(HAVE_STATSMODELS, "statsmodels not installed")
class TestAgainstStatsmodels(unittest.TestCase):
    def test_random_vectors(self):
        rng = np.random.default_rng(42)
        mapping = {"bonferroni": "bonferroni", "holm": "holm", "bh": "fdr_bh", "by": "fdr_by"}
        for trial in range(50):
            m = int(rng.integers(1, 60))
            p = rng.uniform(0, 1, m) ** rng.uniform(0.5, 4)
            if trial % 5 == 0:
                p[: m // 2] = p[0]  # ties
            for ours, theirs in mapping.items():
                expected = multipletests(p, method=theirs)[1]
                np.testing.assert_allclose(adjust_pvalues(p, ours), expected, rtol=0, atol=1e-12)


class TestBatchZTest(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame({
            "GEOID": ["01", "02", "04", "05", "06"],
            "e1": [100.0, 200.0, 300.0, 50.0, np.nan],
            "m1": [10.0, 20.0, 30.0, -555555555, 5.0],
            "e2": [120.0, 205.0, 400.0, 60.0, 10.0],
            "m2": [10.0, 20.0, -222222222, 5.0, 5.0],
        })

    def test_matches_z_test_and_family(self):
        out = batch_z_test(self.df, "e1", "m1", "e2", "m2")
        self.assertEqual(list(out["GEOID"]), list(self.df["GEOID"]))
        # Rows 0, 1, 3 are testable; row 2 has a ** MOE, row 4 a missing estimate.
        self.assertEqual(int(out["family_size"].iloc[0]), 3)
        for i in (0, 1):
            ref = z_test(self.df.e1[i], self.df.m1[i], self.df.e2[i], self.df.m2[i])
            self.assertAlmostEqual(abs(out["z"][i]), ref["z_statistic"])
            self.assertAlmostEqual(out["p_value"][i], ref["p_value"])
            self.assertAlmostEqual(out["se_difference"][i], ref["se_difference"])
        self.assertAlmostEqual(out["moe_difference"][0], math.sqrt(200))
        self.assertLess(out["z"][0], 0)  # X1 < X2
        # Controlled MOE (*****) counts as 0.
        self.assertAlmostEqual(out["se_difference"][3], 5 / 1.645)
        self.assertIn("controlled", out["notes"][3])
        self.assertTrue(pd.isna(out["significant_raw"][2]))
        self.assertTrue(pd.isna(out["p_value"][4]))
        self.assertIn("not appropriate", out["notes"][2])
        np.testing.assert_allclose(
            out["p_adjusted"].dropna().to_numpy(),
            adjust_pvalues(out["p_value"].dropna().to_numpy(), "holm"),
        )
        self.assertEqual(out["correction"].iloc[0], "holm")

    def test_overlap_factor(self):
        out = batch_z_test(self.df.iloc[:2], "e1", "m1", "e2", "m2", year1=2022, year2=2019)
        ref = z_test(100, 10, 120, 10, overlap_years=2)
        self.assertAlmostEqual(out["overlap_factor"][0], math.sqrt(1 - 2 / 5))
        self.assertAlmostEqual(abs(out["z"][0]), ref["z_statistic"])
        self.assertIn("non-overlapping", out["notes"][0])
        out = batch_z_test(self.df.iloc[:2], "e1", "m1", "e2", "m2", year1=2022, year2=2017)
        self.assertEqual(out["overlap_factor"][0], 1.0)
        self.assertEqual(out["notes"][0], "")

    def test_significance_columns(self):
        df = pd.DataFrame({"a": [100.0, 100.0, 100.0], "am": [5.0, 5.0, 5.0],
                           "b": [112.0, 104.0, 100.0], "bm": [5.0, 5.0, 5.0]})
        out = batch_z_test(df, "a", "am", "b", "bm", correction="bonferroni")
        raw = out["p_value"].to_numpy()
        self.assertEqual(list(out["significant_raw"]), list(raw < 0.10))
        self.assertEqual(list(out["significant_adjusted"]), list(np.minimum(raw * 3, 1) < 0.10))
        out95 = batch_z_test(df, "a", "am", "b", "bm", confidence_level=95)
        self.assertEqual(out95["confidence_level"].iloc[0], 0.95)

    def test_year_columns(self):
        df = self.df.iloc[:2].assign(y1=[2022, 2022], y2=[2021, 2012])
        out = batch_z_test(df, "e1", "m1", "e2", "m2", year1="y1", year2="y2")
        self.assertEqual(list(out["overlap_years"]), [4.0, 0.0])


if __name__ == "__main__":
    unittest.main()
