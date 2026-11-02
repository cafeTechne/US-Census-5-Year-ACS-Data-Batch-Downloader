"""Unit tests for census.export module."""

import os
import shutil
import tempfile
import unittest
import pandas as pd
from census.export import (
    enhanced_melt,
    format_dataframe,
    handle_duplicate_columns,
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)


class TestCensusExport(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("Austin city, Texas"), "Austin_city__Texas")
        self.assertEqual(sanitize_filename("A/B\\C:D?E*F"), "A_B_C_D_E_F")

    def test_handle_duplicate_columns(self):
        df = pd.DataFrame([[1, 2, 3, 4]], columns=["A", "A", "B", "A"])
        deduped = handle_duplicate_columns(df)
        self.assertEqual(list(deduped.columns), ["A", "A_1", "B", "A_2"])

    def test_format_dataframe(self):
        df = pd.DataFrame([{"NAME": "Albany", "B01001_001E": 100}])
        desc = {"B01001_001E": "Total Population"}
        formatted = format_dataframe(df, "B01001", variable_descriptions=desc)
        self.assertIn("Total Population", formatted.columns)
        self.assertNotIn("B01001_001E", formatted.columns)

    def test_enhanced_melt(self):
        df = pd.DataFrame(
            {
                "Year": [2021],
                "NAME": ["City A"],
                "state": ["36"],
                "Var1": [10],
                "Var2": [20],
            }
        )
        melted = enhanced_melt(df, id_vars=["Year", "NAME", "state"])
        self.assertEqual(len(melted), 2)
        self.assertIn("Variable", melted.columns)
        self.assertIn("Value", melted.columns)

    def test_process_data_and_save_wide_and_melted(self):
        folder = os.path.join(self.test_dir, "test_out")
        os.makedirs(folder, exist_ok=True)
        raw_df = pd.DataFrame(
            [
                {
                    "NAME": "City A",
                    "state": "36",
                    "Total": 500,
                }
            ]
        )
        process_data_and_save(
            formatted_df=raw_df,
            folder_name=folder,
            entity_name="City A",
            table_code="B01001",
            year=2021,
            output_format="both",
        )
        compiled = os.path.join(folder, "compiled_unmelted_data.csv")
        melted = os.path.join(folder, "City_A_B01001_2021.csv")
        self.assertTrue(os.path.exists(compiled))
        self.assertTrue(os.path.exists(melted))

    def test_save_excel_workbook(self):
        folder = os.path.join(self.test_dir, "test_excel")
        os.makedirs(folder, exist_ok=True)
        acc = {
            "wide": [
                pd.DataFrame(
                    [{"Year": 2021, "NAME": "City A", "state": "36", "Val": 1}]
                )
            ],
            "melted": [
                pd.DataFrame(
                    [{"Year": 2021, "NAME": "City A", "Variable": "Val", "Value": 1}]
                )
            ],
        }
        excel_path = save_excel_workbook(folder, "City A", "B01001", acc)
        self.assertIsNotNone(excel_path)
        self.assertTrue(os.path.exists(excel_path))

        with pd.ExcelFile(excel_path) as xl:
            self.assertIn("Wide_Data", xl.sheet_names)
            self.assertIn("Melted_Data", xl.sheet_names)


if __name__ == "__main__":
    unittest.main()
