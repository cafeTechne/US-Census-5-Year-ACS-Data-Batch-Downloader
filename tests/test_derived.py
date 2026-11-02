# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.derived, anchored on Census-published worked examples.

Sources (numbers below are copied from these documents, rounded as published):

* HB2020: ACS General Handbook (2020), ch. 8, "Calculating Measures of Error
  for Derived Estimates" (acs_general_handbook_2020_ch08.pdf), Examples 1-4
  and the product example (2015 ACS 1-year data).
* WE2025: "Worked Examples for Approximating Margins of Error" (2025 ACS
  Accuracy Document Worked Examples), Examples 1-5.
* SL2016: S. Fuller, "Using ACS Estimates and Margins of Error", Census
  training slides, April 6, 2016, slides 25-26 (old zero-estimate rule).
"""

import math
import unittest

import numpy as np
import pandas as pd

from census.derived import (
    METHOD_SCALE,
    derive_columns,
    difference,
    percent_change,
    product,
    proportion,
    ratio,
    scale,
    subset_relationship,
    sum_estimates,
)
from census.variance import DerivedEstimate


class TestHandbook2020(unittest.TestCase):
    """ACS General Handbook 2020, chapter 8."""

    def setUp(self):
        # Table 8.1: never-married females, Fairfax, Arlington, Alexandria.
        self.females_nm = sum_estimates([135173, 43104, 24842], [3860, 2642, 1957])
        # Table 8.4: total females 15+.
        self.females = sum_estimates([466037, 97360, 67101], [391, 572, 459])
        # Table 8.5: never-married males.
        self.males_nm = sum_estimates([156720, 44613, 25507], [4222, 2819, 2259])

    def test_example1_aggregate_count(self):
        r = self.females_nm
        self.assertIsInstance(r, DerivedEstimate)
        self.assertEqual(r.method, "sum")
        self.assertEqual(r.estimate, 203119)
        self.assertEqual(round(r.moe), 5070)
        self.assertEqual(round(r.se), 3082)
        self.assertEqual(round(r.cv_percent, 1), 1.5)
        # Component SEs: 3,860/1.645 = 2,347 etc.
        self.assertEqual([round(m / 1.645) for m in (3860, 2642, 1957)], [2347, 1606, 1190])

    def test_example2_income_subgroups(self):
        # Table 8.2 -> new subgroups (estimate, MOE, SE, CV).
        cases = [
            ([1502, 1995], [743, 722], 3497, 1036, 630, 18.0),
            ([1756, 1781], [685, 631], 3537, 931, 566, 16.0),
            ([2708, 1981, 2581], [1007, 647, 996], 7270, 1557, 947, 13.0),
        ]
        for ests, moes, est, moe, se, cv in cases:
            r = sum_estimates(ests, moes)
            self.assertEqual(r.estimate, est)
            self.assertEqual(round(r.moe), moe)
            self.assertEqual(round(r.se), se)
            self.assertEqual(round(r.cv_percent, 1), cv)

    def test_example3_proportion_percent(self):
        self.assertEqual(self.females.estimate, 630498)
        self.assertEqual(round(self.females.moe), 831)
        p = proportion(self.females_nm.estimate, self.females_nm.moe,
                       self.females.estimate, self.females.moe)
        self.assertEqual(p.method, "proportion")
        self.assertEqual(round(p.estimate, 3), 0.322)
        pct = proportion(self.females_nm.estimate, self.females_nm.moe,
                         self.females.estimate, self.females.moe, as_percent=True)
        self.assertEqual(round(pct.estimate, 1), 32.2)
        self.assertEqual(round(pct.moe, 1), 0.8)
        self.assertEqual(round(pct.se, 3), 0.488)
        self.assertEqual(round(pct.cv_percent, 1), 1.5)
        # Same result from the rounded inputs the handbook substitutes.
        self.assertEqual(round(proportion(203119, 5070, 630498, 831, as_percent=True).moe, 1), 0.8)

    def test_example4_ratio(self):
        self.assertEqual(self.males_nm.estimate, 226840)
        self.assertEqual(round(self.males_nm.moe), 5557)
        r = ratio(self.males_nm.estimate, self.males_nm.moe,
                  self.females_nm.estimate, self.females_nm.moe)
        self.assertEqual(r.method, "ratio")
        self.assertEqual(round(r.estimate, 3), 1.117)
        self.assertEqual(round(r.moe, 3), 0.039)
        self.assertEqual(round(r.se, 3), 0.024)
        self.assertEqual(round(r.cv_percent, 1), 2.1)
        self.assertEqual(round(ratio(226840, 5557, 203119, 5070).moe, 3), 0.039)

    def test_product(self):
        r = product(74506512, 228238, 0.824, 0.001)
        self.assertEqual(r.method, "product")
        self.assertEqual(round(r.estimate), 61393366)
        self.assertEqual(round(r.moe), 202289)
        self.assertEqual(round(r.estimate - r.moe), 61191077)
        self.assertEqual(round(r.estimate + r.moe), 61595655)
        self.assertEqual(round(r.se), 122972)
        self.assertEqual(round(r.cv_percent, 1), 0.2)

    def test_percent_change_reduces_to_ratio(self):
        # Formula (8): MOE(100*(C-P)/P) = 100 * MOE(C/P).
        pc = percent_change(226840, 5557, 203119, 5070)
        r = ratio(226840, 5557, 203119, 5070)
        self.assertAlmostEqual(pc.estimate, 100 * (r.estimate - 1), places=10)
        self.assertAlmostEqual(pc.moe, 100 * r.moe, places=10)
        self.assertEqual(pc.method, "percent_change")


class TestWorkedExamples2025(unittest.TestCase):
    """Worked Examples for Approximating Margins of Error (2025)."""

    def test_examples(self):
        # Example 1: SE from MOE.
        self.assertEqual(round(89037 / 1.645), 54126)
        # Example 2: never-married males + females.
        s = sum_estimates([47194876, 41142530], [89037, 84363])
        self.assertEqual(s.estimate, 88337406)
        self.assertEqual(round(s.se), 74563)
        # Example 3: percent of never-married people who are female.
        p = proportion(41142530, 84363, s.estimate, s.moe, as_percent=True)
        self.assertEqual(round(p.estimate, 2), 46.57)
        self.assertEqual(round(p.se, 2), 0.04)
        # Example 4: ratio of never-married males to females.
        r = ratio(47194876, 89037, 41142530, 84363)
        self.assertEqual(round(r.estimate, 3), 1.147)
        self.assertEqual(round(r.se, 3), 0.002)
        self.assertEqual(round(r.moe, 3), 0.003)
        # Example 5: product (2017 S2504); the CI uses the rounded estimate and SE.
        pr = product(75022569, 227992, 0.825, 0.001)
        self.assertEqual(round(pr.estimate), 61893619)
        self.assertEqual(round(pr.se), 123102)
        self.assertEqual(round(round(pr.estimate) - 1.645 * round(pr.se)), 61691116)
        self.assertEqual(round(round(pr.estimate) + 1.645 * round(pr.se)), 62096122)


class TestZeroEstimateRule(unittest.TestCase):
    def test_default_includes_every_moe(self):
        r = sum_estimates([36, 0, 0, 0], [56, 27, 29, 29])
        self.assertAlmostEqual(r.moe, math.sqrt(56**2 + 27**2 + 29**2 + 29**2))

    def test_largest_rule_reproduces_2016_slide(self):
        r = sum_estimates([36, 0, 0, 0], [56, 27, 29, 29], zero_estimate_rule="largest")
        self.assertEqual(r.estimate, 36)
        self.assertEqual(round(r.moe, 2), 63.06)
        self.assertTrue(any("largest" in n for n in r.notes))

    def test_slide25_sum(self):
        r = sum_estimates([10247162, 9804950], [4089, 3460])
        self.assertEqual(r.estimate, 20052112)
        self.assertEqual(round(r.moe, 1), 5356.4)

    def test_bad_rule(self):
        with self.assertRaises(ValueError):
            sum_estimates([1], [1], zero_estimate_rule="max")


class TestEdgeCases(unittest.TestCase):
    def test_zero_denominator(self):
        for fn in (proportion, ratio):
            r = fn(5, 1, 0, 1)
            self.assertTrue(math.isnan(r.estimate) and math.isnan(r.moe))
            self.assertTrue(any("denominator is zero" in n for n in r.notes))
        r = percent_change(5, 1, 0, 1)
        self.assertTrue(math.isnan(r.estimate) and math.isnan(r.moe))

    def test_sentinel_moe_is_undefined(self):
        r = difference(10, -222222222, 5, 1)
        self.assertEqual(r.estimate, 5)
        self.assertTrue(math.isnan(r.moe))
        self.assertTrue(any(n.startswith("X MOE undefined") for n in r.notes))
        r = ratio(10, 1, 5, float("nan"))
        self.assertTrue(math.isnan(r.moe))
        self.assertTrue(any("denominator MOE is missing" in n for n in r.notes))

    def test_sentinel_estimate(self):
        r = ratio(-666666666, 1, 5, 1)
        self.assertTrue(math.isnan(r.estimate) and math.isnan(r.moe))
        self.assertTrue(any("sentinel -666666666" in n for n in r.notes))

    def test_controlled_moe_is_zero(self):
        r = difference(100, -555555555, 40, 3)
        self.assertEqual(r.moe, 3)
        self.assertTrue(any("controlled" in n for n in r.notes))
        s = sum_estimates([100, 40], [-555555555, 3])
        self.assertEqual(s.moe, 3)
        self.assertTrue(any("controlled" in n for n in s.notes))

    def test_proportion_fallback(self):
        # MOE_X^2 - P^2 MOE_Y^2 < 0: 10^2 - 0.5^2 * 30^2 = -125.
        r = proportion(50, 10, 100, 30)
        self.assertEqual(r.method, "proportion_ratio_fallback")
        self.assertAlmostEqual(r.moe, ratio(50, 10, 100, 30).moe)
        self.assertTrue(r.notes)

    def test_proportion_equal_one(self):
        # Instructions for Statistical Testing (2025): P = 1 -> SE(P) = SE(A)/B.
        r = proportion(200, 20, 200, 25)
        self.assertEqual(r.estimate, 1)
        self.assertAlmostEqual(r.moe, 20 / 200)
        self.assertEqual(r.method, "proportion")

    def test_proportion_above_one_noted(self):
        r = proportion(120, 5, 100, 5)
        self.assertTrue(any("not a subset" in n for n in r.notes))

    def test_scale(self):
        r = scale(1000, 50, -2.5)
        self.assertEqual((r.estimate, r.moe, r.method), (-2500, 125, METHOD_SCALE))

    def test_same_table_note(self):
        r = ratio(10, 1, 20, 2, codes=("B12001_003E", "B12001_012E"))
        self.assertTrue(any("covariance" in n for n in r.notes))
        r = ratio(10, 1, 20, 2, codes=("B12001_003E", "B01001_001E"))
        self.assertFalse(r.notes)

    def test_many_components_note(self):
        r = sum_estimates([1, 2, 3, 4, 5], [1, 1, 1, 1, 1])
        self.assertTrue(any("5 components" in n for n in r.notes))
        self.assertFalse(sum_estimates([1, 2, 3, 4], [1, 1, 1, 1]).notes)

    def test_percent_change_overlap_note(self):
        r = percent_change(110, 5, 100, 5, year_current=2022, year_prior=2019)
        self.assertTrue(any("share 2 of 5" in n for n in r.notes))
        r = percent_change(110, 5, 100, 5, year_current=2022, year_prior=2017)
        self.assertFalse(r.notes)


class TestVectorised(unittest.TestCase):
    def test_series_return_dataframe(self):
        idx = pd.Index(["a", "b", "c"])
        num = pd.Series([10, 0, 5], index=idx)
        den = pd.Series([100, 0, 50], index=idx)
        out = proportion(num, pd.Series([2, 1, -555555555], index=idx), den, pd.Series([5, 1, 4], index=idx))
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(list(out.columns), ["estimate", "moe", "se", "cv_percent", "method", "notes"])
        self.assertEqual(list(out.index), list(idx))
        self.assertTrue(np.isnan(out.loc["b", "estimate"]))
        self.assertIn("denominator is zero", out.loc["b", "notes"])
        self.assertIn("controlled", out.loc["c", "notes"])
        scalar = proportion(10, 2, 100, 5)
        self.assertAlmostEqual(out.loc["a", "moe"], scalar.moe)
        self.assertAlmostEqual(out.loc["a", "cv_percent"], scalar.cv_percent)

    def test_misaligned_series_rejected(self):
        with self.assertRaises(ValueError):
            ratio(pd.Series([1, 2], index=[0, 1]), 1, pd.Series([1, 2], index=[1, 2]), 1)

    def test_rowwise_sum(self):
        est = pd.DataFrame({"a": [1, 0], "b": [2, 0]})
        moe = pd.DataFrame({"a_m": [3, 4], "b_m": [4, 3]})
        out = sum_estimates(est, moe)
        self.assertEqual(list(out["estimate"]), [3, 0])
        self.assertEqual(list(out["moe"]), [5, 5])
        out = sum_estimates(est, moe, zero_estimate_rule="largest")
        self.assertEqual(list(out["moe"]), [5, 4])

    def test_list_of_component_arrays(self):
        out = sum_estimates([np.array([1, 2]), np.array([3, 4])], [np.array([3, 6]), np.array([4, 8])])
        self.assertEqual(list(out["estimate"]), [4, 6])
        self.assertEqual(list(out["moe"]), [5, 10])


class TestProperties(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        n = 500
        self.y = rng.uniform(100, 10000, n)
        self.x = self.y * rng.uniform(0, 1, n)
        self.mx = rng.uniform(1, 500, n)
        self.my = rng.uniform(1, 500, n)

    def test_proportion_le_ratio(self):
        p = proportion(self.x, self.mx, self.y, self.my)
        r = ratio(self.x, self.mx, self.y, self.my)
        ok = (p["method"] == "proportion").to_numpy()
        self.assertTrue(ok.any())
        self.assertTrue(np.all(p["moe"].to_numpy()[ok] <= r["moe"].to_numpy()[ok] + 1e-12))
        # Fallback rows equal the ratio MOE.
        fb = ~ok
        np.testing.assert_allclose(p["moe"].to_numpy()[fb], r["moe"].to_numpy()[fb])

    def test_scale_invariance(self):
        k = 3.7
        a = proportion(self.x, self.mx, self.y, self.my)
        b = proportion(k * self.x, k * self.mx, k * self.y, k * self.my)
        np.testing.assert_allclose(a["estimate"], b["estimate"])
        np.testing.assert_allclose(a["moe"], b["moe"])
        r1 = ratio(self.x, self.mx, self.y, self.my)
        r2 = ratio(k * self.x, k * self.mx, self.y, self.my)
        np.testing.assert_allclose(r2["estimate"], k * r1["estimate"])
        np.testing.assert_allclose(r2["moe"], k * r1["moe"])
        s = scale(self.x, self.mx, k)
        np.testing.assert_allclose(s["moe"], k * self.mx)

    def test_difference_symmetry(self):
        a = difference(self.x, self.mx, self.y, self.my)
        b = difference(self.y, self.my, self.x, self.mx)
        np.testing.assert_allclose(a["estimate"], -b["estimate"])
        np.testing.assert_allclose(a["moe"], b["moe"])

    def test_se_cv_consistency(self):
        r = ratio(self.x, self.mx, self.y, self.my)
        np.testing.assert_allclose(r["se"], r["moe"] / 1.645)
        np.testing.assert_allclose(r["cv_percent"], r["se"] / r["estimate"].abs() * 100)


METADATA = {
    "B12001_001E": {"label": "Estimate!!Total:", "concept": "Sex by Marital Status"},
    "B12001_002E": {"label": "Estimate!!Total:!!Male:", "concept": "Sex by Marital Status"},
    "B12001_003E": {"label": "Estimate!!Total:!!Male:!!Never married", "concept": "Sex by Marital Status"},
    "B12001_011E": {"label": "Estimate!!Total:!!Female:", "concept": "Sex by Marital Status"},
    "B12001_012E": {"label": "Estimate!!Total:!!Female:!!Never married", "concept": "Sex by Marital Status"},
    "B01003_001E": {"label": "Estimate!!Total", "concept": "Total Population"},
}


class TestSubsetAndDeriveColumns(unittest.TestCase):
    def test_subset_relationship(self):
        self.assertTrue(subset_relationship("B12001_012E", "B12001_011E", METADATA))
        self.assertTrue(subset_relationship("B12001_012", "B12001_001", METADATA))
        self.assertFalse(subset_relationship("B12001_003E", "B12001_012E", METADATA))
        self.assertFalse(subset_relationship("B12001_011E", "B12001_012E", METADATA))
        self.assertFalse(subset_relationship("B12001_001E", "B01003_001E", METADATA))
        with self.assertRaises(KeyError):
            subset_relationship("B99999_001E", "B12001_001E", METADATA)

    def test_derive_columns(self):
        df = pd.DataFrame({
            "GEOID": ["1", "2"],
            "B12001_003E": [156720, 44613], "B12001_003M": [4222, 2819],
            "B12001_012E": [135173, 43104], "B12001_012M": [3860, 2642],
            "B12001_011E": [466037, 97360], "B12001_011M": [391, 572],
        })
        spec = [
            {"name": "nm_f_pct", "op": "auto", "num": "B12001_012E", "den": "B12001_011E", "as_percent": True},
            {"name": "m_to_f", "op": "auto", "num": "B12001_003E", "den": "B12001_012E"},
            {"name": "nm_total", "op": "sum", "num": ["B12001_003", "B12001_012"]},
            {"name": "nm_share_m", "op": "proportion", "num": "B12001_003", "den": "nm_total"},
        ]
        out = derive_columns(df, spec, metadata=METADATA)
        self.assertEqual(list(out["nm_f_pct_method"]), ["proportion", "proportion"])
        self.assertEqual(list(out["m_to_f_method"]), ["ratio", "ratio"])
        expected = proportion(135173, 3860, 466037, 391, as_percent=True)
        self.assertAlmostEqual(out.loc[0, "nm_f_pct_moe"], expected.moe)
        self.assertIn("covariance", out.loc[0, "m_to_f_notes"])
        self.assertEqual(out.loc[0, "nm_total_estimate"], 156720 + 135173)
        self.assertAlmostEqual(out.loc[1, "nm_share_m_estimate"], 44613 / (44613 + 43104))
        for col in ("nm_f_pct_estimate", "nm_f_pct_moe", "nm_f_pct_se", "nm_f_pct_cv_percent", "nm_f_pct_notes"):
            self.assertIn(col, out.columns)

    def test_auto_needs_metadata(self):
        df = pd.DataFrame({"B12001_012E": [1], "B12001_012M": [1], "B12001_011E": [2], "B12001_011M": [1]})
        with self.assertRaises(ValueError):
            derive_columns(df, [{"name": "x", "num": "B12001_012E", "den": "B12001_011E"}])

    def test_missing_moe_column(self):
        df = pd.DataFrame({"B12001_012E": [1], "B12001_011E": [2], "B12001_011M": [1]})
        with self.assertRaises(KeyError):
            derive_columns(df, [{"name": "x", "op": "ratio", "num": "B12001_012E", "den": "B12001_011E"}])


if __name__ == "__main__":
    unittest.main()
