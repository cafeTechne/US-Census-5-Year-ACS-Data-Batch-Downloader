"""Unit tests for census.inflation (CPI-U deflator, boundary advisory)."""

import unittest

import pandas as pd

from census.inflation import (
    CPI_U_ANNUAL,
    adjust_inflation,
    boundary_shift_advisory,
    deflate_dataframe,
    dollar_columns,
    inflation_factor,
    is_dollar_variable,
)


class TestCPIDeflator(unittest.TestCase):
    def test_official_cpi_values(self):
        # BLS CPI-U annual averages (CUUR0000SA0)
        self.assertEqual(CPI_U_ANNUAL[2010], 218.056)
        self.assertEqual(CPI_U_ANNUAL[2022], 292.655)

    def test_2010_to_2022(self):
        adjusted = adjust_inflation(50000, 2010, 2022)
        self.assertAlmostEqual(adjusted, 50000 * 292.655 / 218.056, places=6)
        self.assertAlmostEqual(adjusted, 67105.7, delta=0.5)

    def test_identity_and_reverse(self):
        self.assertEqual(adjust_inflation(100, 2015, 2015), 100)
        there = adjust_inflation(100, 2012, 2020)
        self.assertAlmostEqual(adjust_inflation(there, 2020, 2012), 100)

    def test_missing_year(self):
        with self.assertRaises(KeyError):
            inflation_factor(1990, 2022)
        # Custom tables extend coverage
        self.assertAlmostEqual(
            adjust_inflation(100, 2024, 2030, cpi_table={2024: 100.0, 2030: 120.0}),
            120.0,
        )

    def test_dollar_columns(self):
        meta = {
            "B19013_001E": {
                "label": "Estimate!!Median household income (in 2021 inflation-adjusted dollars)",
                "concept": "MEDIAN HOUSEHOLD INCOME",
            },
            "B01003_001E": {"label": "Estimate!!Total", "concept": "TOTAL POPULATION"},
        }
        cols = ["NAME", "B19013_001E", "B19013_001M", "B19013_001EA", "B01003_001E"]
        self.assertEqual(dollar_columns(cols, meta), ["B19013_001E", "B19013_001M"])

    def test_dollar_detection_excludes_bracket_counts(self):
        # Household counts in income brackets must never be deflated
        self.assertFalse(
            is_dollar_variable(
                "Estimate!!Total:!!Less than $10,000",
                "HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2021 INFLATION-ADJUSTED DOLLARS)",
            )
        )
        self.assertFalse(
            is_dollar_variable(
                "Estimate!!INCOME AND BENEFITS (IN 2022 INFLATION-ADJUSTED DOLLARS)"
                "!!Total households!!Less than $10,000"
            )
        )
        self.assertFalse(
            is_dollar_variable(
                "Estimate!!Median gross rent as a percentage of household income",
                "MEDIAN GROSS RENT AS A PERCENTAGE OF HOUSEHOLD INCOME IN THE PAST 12 MONTHS (DOLLARS)",
            )
        )
        self.assertFalse(is_dollar_variable("Estimate!!Median age --!!Total:", "MEDIAN AGE BY SEX"))

    def test_dollar_detection_includes_dollar_statistics(self):
        self.assertTrue(
            is_dollar_variable(
                "Estimate!!INCOME AND BENEFITS (IN 2022 INFLATION-ADJUSTED DOLLARS)"
                "!!Total households!!Median household income (dollars)"
            )
        )
        self.assertTrue(is_dollar_variable("Estimate!!Median gross rent", "MEDIAN GROSS RENT (DOLLARS)"))
        self.assertTrue(
            is_dollar_variable(
                "Estimate!!Total:!!Householder under 25 years",
                "MEDIAN HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2021 INFLATION-ADJUSTED "
                "DOLLARS) BY AGE OF HOUSEHOLDER",
            )
        )

    def test_deflate_dataframe(self):
        df = pd.DataFrame({"B19013_001E": [1000.0], "B19013_001M": [100.0], "X": [5]})
        out = deflate_dataframe(df, 2010, 2022, ["B19013_001E", "B19013_001M"])
        factor = 292.655 / 218.056
        self.assertAlmostEqual(out["B19013_001E"].iloc[0], 1000 * factor)
        self.assertAlmostEqual(out["B19013_001M"].iloc[0], 100 * factor)
        self.assertEqual(out["X"].iloc[0], 5)


class TestBoundaryAdvisory(unittest.TestCase):
    def test_spanning_range_warns(self):
        msg = boundary_shift_advisory(2015, 2022, "tract")
        self.assertIsNotNone(msg)
        self.assertIn("2019/2020", msg)
        self.assertIn("tract", msg.lower())

    def test_within_vintage_no_warning(self):
        self.assertIsNone(boundary_shift_advisory(2012, 2019, "tract"))
        self.assertIsNone(boundary_shift_advisory(2020, 2023, "tract"))

    def test_stable_geographies_no_warning(self):
        self.assertIsNone(boundary_shift_advisory(2015, 2022, "state"))
        self.assertIsNone(boundary_shift_advisory(2015, 2022, "us"))


if __name__ == "__main__":
    unittest.main()
