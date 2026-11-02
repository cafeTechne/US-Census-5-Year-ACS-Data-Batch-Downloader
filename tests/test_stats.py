"""Unit tests for census.stats (MOE/SE/CV, Z-tests, sentinels, aggregation)."""

import math
import unittest

import pandas as pd

from census.stats import (
    add_reliability_columns,
    acs5_overlap_years,
    aggregate_columns,
    aggregate_estimates,
    calculate_cv,
    clean_census_sentinels,
    coefficient_of_variation,
    normal_ppf,
    overlap_factor,
    pair_estimates_and_moes,
    parse_variable_code,
    reliability_flag,
    standard_error,
    two_tailed_p_value,
    z_test,
)


class TestVariableCodes(unittest.TestCase):
    def test_parse_variable_code(self):
        self.assertEqual(parse_variable_code("B19013_001E"), ("B19013_001", "E"))
        self.assertEqual(parse_variable_code("B19013_001M"), ("B19013_001", "M"))
        self.assertEqual(parse_variable_code("B19013_001EA"), ("B19013_001", "EA"))
        self.assertEqual(parse_variable_code("B01001A_003E"), ("B01001A_003", "E"))
        self.assertEqual(parse_variable_code("DP03_0062PE"), ("DP03_0062", "PE"))
        self.assertEqual(parse_variable_code("DP03_0062PM"), ("DP03_0062", "PM"))
        self.assertEqual(parse_variable_code("S1701_C01_001E"), ("S1701_C01_001", "E"))
        self.assertIsNone(parse_variable_code("NAME"))
        self.assertIsNone(parse_variable_code("state"))
        self.assertIsNone(parse_variable_code("GEO_ID"))

    def test_pair_estimates_and_moes(self):
        cols = ["NAME", "B01001_001E", "B01001_001M", "DP03_0001PE", "DP03_0001PM", "X_1E"]
        pairs = pair_estimates_and_moes(cols)
        self.assertIn(("B01001_001", "B01001_001E", "B01001_001M"), pairs)
        self.assertIn(("DP03_0001P", "DP03_0001PE", "DP03_0001PM"), pairs)
        self.assertEqual(len(pairs), 2)


class TestScalarStatistics(unittest.TestCase):
    def test_standard_error(self):
        self.assertAlmostEqual(standard_error(1645), 1000.0)
        self.assertAlmostEqual(standard_error(-1645), 1000.0)
        self.assertAlmostEqual(standard_error(1960, moe_confidence=0.95), 1000.0)
        self.assertTrue(math.isnan(standard_error(float("nan"))))

    def test_coefficient_of_variation(self):
        # SE = 1000, estimate = 10000 -> CV = 10%
        self.assertAlmostEqual(coefficient_of_variation(10000, 1645), 10.0)
        self.assertTrue(math.isnan(coefficient_of_variation(0, 100)))

    def test_reliability_thresholds(self):
        self.assertEqual(reliability_flag(15.0), "High")
        self.assertEqual(reliability_flag(15.01), "Moderate")
        self.assertEqual(reliability_flag(30.0), "Moderate")
        self.assertEqual(reliability_flag(30.01), "Low")
        self.assertEqual(reliability_flag(float("nan")), "")

    def test_calculate_cv(self):
        res = calculate_cv(50000, 4935)  # SE = 3000, CV = 6%
        self.assertAlmostEqual(res["standard_error"], 3000.0)
        self.assertAlmostEqual(res["cv_percent"], 6.0)
        self.assertEqual(res["reliability"], "High")
        self.assertEqual(res["lower_90"], 45065)
        self.assertEqual(res["upper_90"], 54935)


class TestSignificance(unittest.TestCase):
    def test_p_values_match_standard_normal(self):
        self.assertAlmostEqual(two_tailed_p_value(1.959964), 0.05, places=5)
        self.assertAlmostEqual(two_tailed_p_value(1.644854), 0.10, places=5)
        self.assertAlmostEqual(two_tailed_p_value(2.575829), 0.01, places=5)
        self.assertAlmostEqual(normal_ppf(0.975), 1.959964, places=5)

    def test_z_test_significant(self):
        # SE1 = SE2 = 1000 -> SE_diff = 1414.2; diff 3000 -> Z = 2.121
        res = z_test(53000, 1645, 50000, 1645)
        self.assertAlmostEqual(res["z_statistic"], 3000 / math.sqrt(2e6), places=6)
        self.assertTrue(res["significant"])
        self.assertTrue(res["significant_at"]["95%"])
        self.assertFalse(res["significant_at"]["99%"])
        self.assertIn("90%", res["verdict"])

    def test_z_test_not_significant(self):
        res = z_test(51000, 1645, 50000, 1645, confidence_level=0.95)
        self.assertFalse(res["significant"])
        self.assertAlmostEqual(res["critical_value"], 1.96)
        self.assertGreater(res["p_value"], 0.05)

    def test_z_test_accepts_percent_confidence(self):
        res = z_test(53000, 1645, 50000, 1645, confidence_level=99)
        self.assertEqual(res["confidence_level"], 0.99)
        self.assertFalse(res["significant"])

    def test_overlap_adjustment(self):
        self.assertEqual(acs5_overlap_years(2019, 2022), 2)
        self.assertEqual(acs5_overlap_years(2012, 2022), 0)
        self.assertAlmostEqual(overlap_factor(2), math.sqrt(0.6))
        base = z_test(53000, 1645, 50000, 1645)
        adj = z_test(53000, 1645, 50000, 1645, overlap_years=2)
        self.assertAlmostEqual(
            adj["z_statistic"], base["z_statistic"] / math.sqrt(0.6), places=6
        )

    def test_zero_standard_error(self):
        res = z_test(10, 0, 5, 0)
        self.assertTrue(math.isinf(res["z_statistic"]))
        self.assertTrue(res["significant"])


class TestAggregation(unittest.TestCase):
    def test_aggregate_estimates_quadrature(self):
        total, moe = aggregate_estimates([100, 200], [30, 40])
        self.assertEqual(total, 300)
        self.assertAlmostEqual(moe, 50.0)

    def test_aggregate_zero_estimates_use_largest_moe_once(self):
        total, moe = aggregate_estimates([100, 0, 0], [30, 20, 40])
        self.assertEqual(total, 100)
        self.assertAlmostEqual(moe, 50.0)  # sqrt(30^2 + 40^2)

    def test_aggregate_columns(self):
        df = pd.DataFrame(
            {
                "B01001_007E": [100],
                "B01001_007M": [30],
                "B01001_008E": [200],
                "B01001_008M": [40],
            }
        )
        out = aggregate_columns(df, ["B01001_007", "B01001_008"], "AGE_18_21_")
        self.assertEqual(out["AGE_18_21_E"].iloc[0], 300)
        self.assertAlmostEqual(out["AGE_18_21_M"].iloc[0], 50.0)

    def test_aggregate_columns_missing(self):
        with self.assertRaises(KeyError):
            aggregate_columns(pd.DataFrame({"B01001_007E": [1]}), ["B01001_007"], "X")


class TestSentinels(unittest.TestCase):
    def test_clean_census_sentinels(self):
        df = pd.DataFrame(
            {
                "NAME": ["A", "B", "C"],
                "state": ["06", "06", "06"],
                "B19013_001E": ["75000", "-666666666", "250001"],
                "B19013_001M": ["5000", "-222222222", "-333333333"],
                "B19013_001EA": [None, None, "+"],
            }
        )
        out = clean_census_sentinels(df, add_notes=True)
        self.assertEqual(out["B19013_001E"].iloc[0], 75000)
        self.assertTrue(pd.isna(out["B19013_001E"].iloc[1]))
        self.assertTrue(pd.isna(out["B19013_001M"].iloc[2]))
        self.assertEqual(out["state"].iloc[0], "06")  # geography untouched
        self.assertIn("insufficient sample", out["B19013_001E_NOTE"].iloc[1])
        self.assertIn("Top-coded", out["B19013_001E_NOTE"].iloc[2])
        self.assertIn("open-ended", out["B19013_001M_NOTE"].iloc[2])
        # Annotation column is left as-is
        self.assertEqual(out["B19013_001EA"].iloc[2], "+")

    def test_no_negative_values_survive(self):
        df = pd.DataFrame({"B01002_001E": ["-999999999", "-888888888", "-555555555"]})
        out = clean_census_sentinels(df)
        self.assertTrue(out["B01002_001E"].isna().all())


class TestReliabilityColumns(unittest.TestCase):
    def test_add_reliability_columns(self):
        df = pd.DataFrame(
            {
                "NAME": ["A", "B", "C"],
                "B19013_001E": [10000, 10000, 0],
                "B19013_001M": [1645, 6580, 100],
                "DP03_0001PE": [50.0, 50.0, 50.0],
                "DP03_0001PM": [3.29, 3.29, 3.29],
            }
        )
        out = add_reliability_columns(df)
        self.assertAlmostEqual(out["B19013_001_SE"].iloc[0], 1000.0)
        self.assertAlmostEqual(out["B19013_001_CV"].iloc[0], 10.0)
        self.assertEqual(out["B19013_001_RELIABILITY"].tolist(), ["High", "Low", ""])
        self.assertAlmostEqual(out["DP03_0001P_CV"].iloc[0], 4.0)

    def test_sentinels_do_not_become_huge_standard_errors(self):
        df = pd.DataFrame({"B19013_001E": [50000], "B19013_001M": [-555555555]})
        out = add_reliability_columns(df)
        self.assertTrue(pd.isna(out["B19013_001_SE"].iloc[0]))


if __name__ == "__main__":
    unittest.main()
