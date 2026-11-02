import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd

import acs_downloader as ad


class TestACSDownloader(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_extract_state_abbr(self):
        self.assertEqual(
            ad.extract_state_abbr_from_filename("st36_ny_place2020.txt"), "36"
        )
        self.assertEqual(
            ad.extract_state_abbr_from_filename("st01_al_place2020.txt"), "01"
        )

    def test_sanitize_filename(self):
        self.assertEqual(
            ad.sanitize_filename("New York / Manhattan: 1"), "New_York___Manhattan__1"
        )
        self.assertEqual(ad.sanitize_filename("Austin city"), "Austin_city")

    def test_handle_duplicate_columns(self):
        df = pd.DataFrame([[1, 2, 3]], columns=["col", "col", "other"])
        deduped = ad.handle_duplicate_columns(df)
        self.assertEqual(list(deduped.columns), ["col", "col_1", "other"])

    def test_enhanced_melt(self):
        df = pd.DataFrame(
            {
                "Year": [2019],
                "NAME": ["Austin city, Texas"],
                "state": ["48"],
                "place": ["05000"],
                "Total": [950000],
                "Male": [480000],
            }
        )
        id_vars = [c for c in ["Year", "NAME", "state", "place"] if c in df.columns]
        melted = ad.enhanced_melt(df, id_vars, var_name="Variable", value_name="Value")
        self.assertEqual(len(melted), 2)
        self.assertIn("Variable", melted.columns)
        self.assertIn("Value", melted.columns)
        self.assertEqual(set(melted["Variable"].tolist()), {"Total", "Male"})

    def test_enhanced_melt_missing_id_vars(self):
        df = pd.DataFrame({"NAME": ["Place A"], "Total": [100]})
        melted = ad.enhanced_melt(
            df, id_vars=["NON_EXISTENT"], var_name="Var", value_name="Val"
        )
        self.assertTrue(melted.empty)

    def test_format_dataframe(self):
        # Setup mock variable definition
        ad.VARIABLE_DEFINITIONS["B01001_001E"] = "Estimate!!Total:"
        raw_df = pd.DataFrame([{"NAME": "Albany", "B01001_001E": 12345, "state": "36"}])
        formatted = ad.format_dataframe(raw_df, "B01001")
        self.assertIn("Estimate!!Total:", formatted.columns)
        self.assertNotIn("B01001_001E", formatted.columns)

    def test_load_tables_from_local(self):
        groups_path = ad.get_resource_path("groups.json")
        tables = ad.load_tables_from_local(groups_path)
        self.assertIsInstance(tables, dict)
        self.assertIn("B01001", tables)

    def test_load_places_from_local(self):
        ny_file = ad.get_local_places_file("36")
        self.assertIsNotNone(ny_file)
        self.assertTrue(os.path.exists(ny_file))
        places = ad.load_places_from_local(ny_file)
        self.assertGreater(len(places), 0)
        # Check structure (state_code, place_code, place_name, place_type, counties)
        first_place = places[0]
        self.assertEqual(len(first_place), 5)
        self.assertEqual(first_place[0], "36")

    def test_process_data_and_save_both(self):
        folder = os.path.join(self.test_dir, "Austin_B01001")
        os.makedirs(folder, exist_ok=True)
        raw_df = pd.DataFrame(
            [
                {
                    "NAME": "Austin city, Texas",
                    "state": "48",
                    "place": "05000",
                    "Estimate!!Total:": 950000,
                }
            ]
        )
        ad.process_data_and_save(
            formatted_df=raw_df,
            folder_name=folder,
            entity_name="Austin city",
            table_code="B01001",
            year=2019,
            output_format="both",
        )
        compiled_csv = os.path.join(folder, "compiled_unmelted_data.csv")
        melted_csv = os.path.join(folder, "Austin_city_B01001_2019.csv")
        self.assertTrue(os.path.exists(compiled_csv))
        self.assertTrue(os.path.exists(melted_csv))

        # Check year column presence
        cdf = pd.read_csv(compiled_csv)
        self.assertIn("Year", cdf.columns)
        self.assertEqual(cdf["Year"].iloc[0], 2019)

        mdf = pd.read_csv(melted_csv)
        self.assertIn("Year", mdf.columns)
        self.assertEqual(mdf["Year"].iloc[0], 2019)

    def test_process_data_and_save_wide_only(self):
        folder = os.path.join(self.test_dir, "Austin_WideOnly")
        os.makedirs(folder, exist_ok=True)
        raw_df = pd.DataFrame(
            [
                {
                    "NAME": "Austin city, Texas",
                    "state": "48",
                    "place": "05000",
                    "Estimate!!Total:": 950000,
                }
            ]
        )
        ad.process_data_and_save(
            formatted_df=raw_df,
            folder_name=folder,
            entity_name="Austin city",
            table_code="B01001",
            year=2019,
            output_format="wide_only",
        )
        compiled_csv = os.path.join(folder, "compiled_unmelted_data.csv")
        melted_csv = os.path.join(folder, "Austin_city_B01001_2019.csv")
        self.assertTrue(os.path.exists(compiled_csv))
        self.assertFalse(os.path.exists(melted_csv))

    def test_process_data_and_save_melted_only(self):
        folder = os.path.join(self.test_dir, "Austin_MeltedOnly")
        os.makedirs(folder, exist_ok=True)
        raw_df = pd.DataFrame(
            [
                {
                    "NAME": "Austin city, Texas",
                    "state": "48",
                    "place": "05000",
                    "Estimate!!Total:": 950000,
                }
            ]
        )
        ad.process_data_and_save(
            formatted_df=raw_df,
            folder_name=folder,
            entity_name="Austin city",
            table_code="B01001",
            year=2019,
            output_format="melted_only",
        )
        compiled_csv = os.path.join(folder, "compiled_unmelted_data.csv")
        melted_csv = os.path.join(folder, "Austin_city_B01001_2019.csv")
        self.assertFalse(os.path.exists(compiled_csv))
        self.assertTrue(os.path.exists(melted_csv))

    def test_process_data_and_save_excel(self):
        folder = os.path.join(self.test_dir, "Austin_Excel")
        os.makedirs(folder, exist_ok=True)
        raw_df = pd.DataFrame(
            [
                {
                    "NAME": "Austin city, Texas",
                    "state": "48",
                    "place": "05000",
                    "Estimate!!Total:": 950000,
                }
            ]
        )
        accumulator = {"wide": [], "melted": []}
        ad.process_data_and_save(
            formatted_df=raw_df,
            folder_name=folder,
            entity_name="Austin city",
            table_code="B01001",
            year=2019,
            output_format="excel",
            excel_accumulator=accumulator,
        )
        self.assertEqual(len(accumulator["wide"]), 1)
        self.assertEqual(len(accumulator["melted"]), 1)
        excel_path = ad.save_excel_workbook(
            folder_name=folder,
            entity_name="Austin city",
            table_code="B01001",
            excel_accumulator=accumulator,
        )
        self.assertIsNotNone(excel_path)
        self.assertTrue(os.path.exists(excel_path))

        with pd.ExcelFile(excel_path) as xl:
            self.assertIn("Wide_Data", xl.sheet_names)
            self.assertIn("Melted_Data", xl.sheet_names)

    @patch("requests.get")
    def test_fetch_census_data_geographies(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [["NAME", "B01001_001E"], ["Entity", "100"]]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Test place level
        res_place = ad.fetch_census_data(
            2019, "36", "B01001", geo_level="place", place_code="51000"
        )
        self.assertIsNotNone(res_place)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["for"], "place:51000")
        self.assertEqual(kwargs["params"]["in"], "state:36")

        # Test state level
        res_state = ad.fetch_census_data(2019, "36", "B01001", geo_level="state")
        self.assertIsNotNone(res_state)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["for"], "state:36")
        self.assertNotIn("in", kwargs["params"])

        # Test county level
        res_county = ad.fetch_census_data(
            2019, "36", "B01001", geo_level="county", county_code="*"
        )
        self.assertIsNotNone(res_county)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["for"], "county:*")
        self.assertEqual(kwargs["params"]["in"], "state:36")


if __name__ == "__main__":
    unittest.main()
