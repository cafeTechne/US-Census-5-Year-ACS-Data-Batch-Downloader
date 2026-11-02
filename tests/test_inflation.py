"""Unit tests for census.inflation (price indexes, deflator, advisories)."""

import csv
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from census import inflation
from census.inflation import (
    CPI_U,
    R_CPI_U_RS,
    PriceIndexUnavailable,
    adjust_inflation,
    boundary_shift_advisory,
    deflate_dataframe,
    dollar_columns,
    home_value_advisory,
    inflation_factor,
    is_dollar_variable,
    load_price_index,
    price_index_dir,
)


class TestVendoredCPIU(unittest.TestCase):
    def setUp(self):
        load_price_index.cache_clear()

    def test_matches_bls_published_annual_averages(self):
        # BLS published CPI-U annual averages (CUUR0000SA0). Values computed from
        # monthly indexes may differ from the published average by 0.001.
        cpi = load_price_index(CPI_U)
        for year, published in {2015: 237.017, 2019: 255.657, 2022: 292.655, 2024: 313.689}.items():
            self.assertAlmostEqual(cpi[year], published, delta=0.0015)
        self.assertAlmostEqual(cpi[2010], 218.056, delta=0.0015)

    def test_csv_matches_sidecar_checksum(self):
        path = os.path.join(price_index_dir(), "cpi_u.csv")
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(price_index_dir(), "cpi_u.json"), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(digest, sidecar["csv_sha256"])
        self.assertEqual(sidecar["series_id"], "CUUR0000SA0")

    def test_years_contiguous(self):
        years = sorted(load_price_index(CPI_U))
        self.assertEqual(years, list(range(years[0], years[-1] + 1)))

    def test_cpi_u_conversion(self):
        cpi = load_price_index(CPI_U)
        adjusted = adjust_inflation(50000, 2010, 2022, index=CPI_U)
        self.assertAlmostEqual(adjusted, 50000 * cpi[2022] / cpi[2010])


class TestIndexSelection(unittest.TestCase):
    """R-CPI-U-RS is the default; exercised here with a synthetic fixture file."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "r_cpi_u_rs.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["year", "value"])
            writer.writerows([[2010, 320.0], [2022, 432.0]])  # fixture values, not BLS data
        self.env = patch.dict(os.environ, {"CENSUS_PRICE_INDEX_DIR": self.dir})
        self.env.start()
        load_price_index.cache_clear()

    def tearDown(self):
        self.env.stop()
        load_price_index.cache_clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_default_is_r_cpi_u_rs(self):
        self.assertEqual(inflation.DEFAULT_PRICE_INDEX, R_CPI_U_RS)
        self.assertAlmostEqual(adjust_inflation(100, 2010, 2022), 100 * 432.0 / 320.0)

    def test_missing_year_and_missing_index(self):
        with self.assertRaises(PriceIndexUnavailable):
            inflation_factor(2015, 2022)
        with self.assertRaises(PriceIndexUnavailable):
            inflation_factor(2010, 2022, index=CPI_U)  # no cpi_u.csv in fixture dir

    def test_custom_table(self):
        self.assertAlmostEqual(
            adjust_inflation(100, 2024, 2030, cpi_table={2024: 100.0, 2030: 120.0}), 120.0
        )

    def test_unknown_index(self):
        with self.assertRaises(ValueError):
            load_price_index("ppi")


class TestDollarDetection(unittest.TestCase):
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

    def test_excludes_bracket_counts(self):
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

    def test_includes_dollar_statistics(self):
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
        table = {2010: 200.0, 2022: 300.0}
        out = deflate_dataframe(df, 2010, 2022, ["B19013_001E", "B19013_001M"], table)
        self.assertAlmostEqual(out["B19013_001E"].iloc[0], 1500.0)
        self.assertAlmostEqual(out["B19013_001M"].iloc[0], 150.0)
        self.assertEqual(out["X"].iloc[0], 5)


class TestAdvisories(unittest.TestCase):
    def test_boundary_spanning_range_warns(self):
        msg = boundary_shift_advisory(2015, 2022, "tract")
        self.assertIn("2019/2020", msg)

    def test_boundary_within_vintage_no_warning(self):
        self.assertIsNone(boundary_shift_advisory(2012, 2019, "tract"))
        self.assertIsNone(boundary_shift_advisory(2020, 2023, "tract"))
        self.assertIsNone(boundary_shift_advisory(2015, 2022, "state"))

    def test_home_value_advisory(self):
        self.assertIn("approximate", home_value_advisory("B25077", [2018, 2022]))
        self.assertIsNone(home_value_advisory("B25077", [2022, 2023]))
        self.assertIsNone(home_value_advisory("B19013", [2015, 2022]))


if __name__ == "__main__":
    unittest.main()
