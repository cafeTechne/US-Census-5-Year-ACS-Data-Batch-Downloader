"""Unit tests for census.panel (slugs, codebooks, tidy panels, Parquet)."""

import os
import shutil
import tempfile
import unittest

import pandas as pd

from census.panel import (
    CODEBOOK_COLUMNS,
    build_tidy_panel,
    classify_column,
    describe_column,
    export_parquet,
    generate_codebook,
    infer_unit,
    make_slug_map,
    slugify,
)

METADATA = {
    "B19013_001E": {
        "label": "Estimate!!Median household income in the past 12 months "
        "(in 2021 inflation-adjusted dollars)",
        "concept": "MEDIAN HOUSEHOLD INCOME IN THE PAST 12 MONTHS "
        "(IN 2021 INFLATION-ADJUSTED DOLLARS)",
    },
    "B01001_001E": {"label": "Estimate!!Total:", "concept": "SEX BY AGE"},
    "B01001_002E": {"label": "Estimate!!Total:!!Male:", "concept": "SEX BY AGE"},
    "DP03_0009PE": {
        "label": "Percent!!EMPLOYMENT STATUS!!Civilian labor force!!Unemployment Rate",
        "concept": "SELECTED ECONOMIC CHARACTERISTICS",
    },
}


def _frame(year, income):
    return pd.DataFrame(
        {
            "GEOID": ["36061", "48453"],
            "Year": [year, year],
            "NAME": ["New York County", "Travis County"],
            "state": ["36", "48"],
            "B19013_001E": [income, income + 1],
            "B19013_001EA": [None, None],
            "B19013_001M": [1000, 1200],
        }
    )


class TestClassification(unittest.TestCase):
    def test_classify_column(self):
        self.assertEqual(classify_column("B19013_001E"), ("B19013_001", "E", False))
        self.assertEqual(classify_column("DP03_0009PM"), ("DP03_0009", "PM", True))
        self.assertEqual(classify_column("B19013_001_SE"), ("B19013_001", "SE", False))
        self.assertEqual(classify_column("DP03_0009P_CV"), ("DP03_0009", "CV", True))
        self.assertEqual(classify_column("B19013_001EA"), ("B19013_001", "ANN", False))
        self.assertEqual(
            classify_column("B19013_001E_NOTE"), ("B19013_001", "NOTE:E", False)
        )
        self.assertIsNone(classify_column("NAME"))

    def test_describe_column(self):
        self.assertTrue(describe_column("B19013_001M", METADATA).startswith("Margin of Error"))
        self.assertIn("Standard Error", describe_column("B19013_001_SE", METADATA))
        # The metadata vintage year is stripped from dollar labels
        self.assertNotIn("2021", describe_column("B19013_001E", METADATA))

    def test_infer_unit(self):
        self.assertTrue(infer_unit("B19013_001E", METADATA).startswith("Dollars"))
        self.assertIn("constant 2022", infer_unit("B19013_001E", METADATA, 2022))
        self.assertEqual(infer_unit("B01001_001E", METADATA), "Count")
        self.assertEqual(infer_unit("DP03_0009PE", METADATA), "Percent")
        self.assertEqual(infer_unit("B19013_001_CV", METADATA), "Percent")


class TestSlugs(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Estimate!!Total:!!Male:"), "total_male")
        self.assertEqual(
            slugify("Estimate!!Median household income (in 2021 inflation-adjusted dollars)"),
            "median_household_income",
        )
        self.assertTrue(slugify("Estimate!!5 to 9 years").startswith("v_"))

    def test_slug_map_unique_and_paired(self):
        cols = [
            "GEOID", "Year", "NAME", "B19013_001E", "B19013_001M",
            "B19013_001_SE", "B19013_001_CV", "B19013_001_RELIABILITY",
            "B01001_001E", "B01001_002E",
        ]
        slugs = make_slug_map(cols, METADATA)
        self.assertEqual(len(set(slugs.values())), len(cols))
        self.assertEqual(slugs["GEOID"], "geoid")
        self.assertEqual(slugs["B19013_001E"], "median_household_income_in_the_past_12_months_est")
        self.assertEqual(slugs["B19013_001M"], "median_household_income_in_the_past_12_months_moe")
        self.assertTrue(slugs["B19013_001_CV"].endswith("_cv"))
        self.assertEqual(slugs["B01001_001E"], "total_est")
        self.assertEqual(slugs["B01001_002E"], "total_male_est")

    def test_code_style_slugs_fit_stata(self):
        slugs = make_slug_map(["B19013_001E", "B19013_001M", "DP03_0009PE"], METADATA, "code")
        self.assertEqual(slugs["B19013_001E"], "b19013_001_est")
        self.assertEqual(slugs["DP03_0009PE"], "dp03_0009_pct")
        self.assertTrue(all(len(s) <= 32 for s in slugs.values()))

    def test_colliding_labels_disambiguated(self):
        meta = {
            "B1_001E": {"label": "Estimate!!Total:"},
            "B1_002E": {"label": "Estimate!!Total"},
        }
        slugs = make_slug_map(["B1_001E", "B1_002E"], meta)
        self.assertNotEqual(slugs["B1_001E"], slugs["B1_002E"])


class TestCodebook(unittest.TestCase):
    def test_codebook_complete(self):
        cols = ["GEOID", "Year", "B19013_001E", "B19013_001M", "B19013_001_CV"]
        cb = generate_codebook(cols, METADATA, universe="Households", inflation_base_year=2022)
        self.assertEqual(list(cb.columns), CODEBOOK_COLUMNS)
        self.assertEqual(cb["Variable_Code"].tolist(), cols)
        self.assertFalse((cb["Variable_Slug"] == "").any())
        row = cb.set_index("Variable_Code").loc["B19013_001M"]
        self.assertEqual(row["Measurement_Type"], "Margin of Error")
        self.assertEqual(row["Universe"], "Households")
        self.assertIn("constant 2022", row["Unit"])
        self.assertEqual(
            cb.set_index("Variable_Code").loc["GEOID", "Measurement_Type"], "Identifier"
        )


class TestTidyPanel(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_panel_index_unique(self):
        panel, dropped = build_tidy_panel(
            [_frame(2021, 70000), _frame(2022, 75000), _frame(2022, 75000)]
        )
        self.assertEqual(dropped, 2)
        self.assertEqual(len(panel), 4)
        self.assertFalse(panel.duplicated(["GEOID", "Year"]).any())
        self.assertEqual(list(panel.columns[:3]), ["GEOID", "Year", "NAME"])
        self.assertNotIn("B19013_001EA", panel.columns)  # annotations dropped
        self.assertNotIn("state", panel.columns)
        self.assertEqual(panel["B19013_001E"].dtype.kind, "i")

    def test_panel_requires_ids(self):
        with self.assertRaises(ValueError):
            build_tidy_panel([pd.DataFrame({"NAME": ["x"], "B1_001E": [1]})])

    def test_empty_panel(self):
        panel, dropped = build_tidy_panel([])
        self.assertTrue(panel.empty)
        self.assertEqual(dropped, 0)

    def test_parquet_schema(self):
        panel, _ = build_tidy_panel([_frame(2021, 70000), _frame(2022, 75000)])
        panel["B19013_001_RELIABILITY"] = ["High", "", "Low", "Moderate"]
        path = export_parquet(panel, os.path.join(self.test_dir, "p.parquet"))
        back = pd.read_parquet(path)
        self.assertEqual(len(back), 4)
        self.assertEqual(back["Year"].dtype, "int64")
        self.assertEqual(back["B19013_001E"].dtype.kind, "i")
        self.assertEqual(str(back["GEOID"].dtype), "string")
        self.assertEqual(back["GEOID"].iloc[0], "36061")  # leading zeros intact


if __name__ == "__main__":
    unittest.main()
