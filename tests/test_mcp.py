"""Unit tests for mcp_server tools."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import mcp_server as srv


class TestMCPServer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # Keep the query cache out of the workspace during tests
        self.env = patch.dict(os.environ, {"CENSUS_CACHE_DISABLED": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_list_states(self):
        states = srv.list_states()
        self.assertIsInstance(states, list)
        self.assertGreater(len(states), 50)
        first = states[0]
        self.assertIn("state_code", first)
        self.assertIn("state_name", first)

    def test_list_places(self):
        places = srv.list_places(state_code="36", search="Albany")
        self.assertIsInstance(places, list)
        self.assertGreater(len(places), 0)
        self.assertTrue(any("Albany" in p["place_name"] for p in places))

    def test_search_tables(self):
        matches = srv.search_tables(query="B01001", limit=10)
        self.assertIsInstance(matches, list)
        self.assertGreater(len(matches), 0)
        self.assertEqual(matches[0]["table_code"], "B01001")

        keyword_matches = srv.search_tables(query="poverty", limit=5)
        self.assertGreater(len(keyword_matches), 0)

    def test_list_variables(self):
        res = srv.list_variables(table_code="B01001", limit=10)
        self.assertIn("table_code", res)
        self.assertEqual(res["table_code"], "B01001")
        self.assertGreater(res["total_variables"], 0)
        self.assertLessEqual(len(res["variables"]), 10)

    @patch("requests.Session.get")
    def test_fetch_table_data(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "B01001_001E", "state"],
            ["New York", "19500000", "36"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        res = srv.fetch_table_data(
            year=2020,
            state_code="36",
            table_code="B01001",
            geo_level="state",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["row_count"], 1)
        self.assertIn("data", res)

    @patch("requests.Session.get")
    def test_batch_download(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "B01001_001E", "state"],
            ["New York", "19500000", "36"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        res = srv.batch_download(
            state_code="36",
            table_code="B01001",
            start_year=2019,
            end_year=2020,
            geo_level="state",
            output_format="both",
            output_dir=self.test_dir,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["total_calls_attempted"], 2)
        self.assertEqual(res["successful_calls"], 2)


    # ---- Phase 5 research tools -------------------------------------------

    def test_search_tables_includes_profiles(self):
        matches = srv.search_tables(query="DP03")
        self.assertEqual(matches[0]["table_code"], "DP03")
        self.assertEqual(matches[0]["dataset"], "profile")

    def test_calculate_cv(self):
        res = srv.calculate_cv(estimate=10000, moe=1645)
        self.assertAlmostEqual(res["standard_error"], 1000.0)
        self.assertAlmostEqual(res["cv_percent"], 10.0)
        self.assertEqual(res["reliability"], "High")

    def test_calculate_significance(self):
        res = srv.calculate_significance(53000, 1645, 50000, 1645, 0.95)
        self.assertTrue(res["significant"])
        self.assertLess(res["p_value"], 0.05)
        overlapped = srv.calculate_significance(
            53000, 1645, 50000, 1645, 0.95, year1=2019, year2=2022
        )
        self.assertEqual(overlapped["overlap_years"], 2)
        self.assertGreater(overlapped["z_statistic"], res["z_statistic"])

    def test_adjust_inflation(self):
        res = srv.adjust_inflation(50000, 2010, 2022)
        self.assertTrue(res["success"])
        self.assertAlmostEqual(res["adjusted_value"], 50000 * 292.655 / 218.056)
        self.assertFalse(srv.adjust_inflation(1, 1900, 2022)["success"])

    def test_list_zctas(self):
        res = srv.list_zctas(state_code="36", county_code="061")
        self.assertTrue(res["success"])
        self.assertIn("10001", res["zctas"])
        resolved = srv.list_zctas(zip_code="78701")
        self.assertEqual(resolved["resolved"]["county_geoid"], "48453")
        self.assertFalse(srv.list_zctas()["success"])

    @patch("requests.Session.get")
    def test_list_tracts(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "state", "county", "tract"],
            ["Census Tract 1", "36", "061", "000100"],
            ["Census Tract 2.01", "36", "061", "000201"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        res = srv.list_tracts(state_code="36", county_code="061")
        self.assertTrue(res["success"])
        self.assertEqual(res["tract_count"], 2)
        self.assertEqual(res["tracts"][0]["geoid"], "36061000100")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["in"], "state:36 county:061")

    def test_generate_codebook(self):
        out = os.path.join(self.test_dir, "cb.csv")
        res = srv.generate_codebook("B19013", include_reliability=True, output_path=out)
        self.assertTrue(res["success"])
        codes = [r["Variable_Code"] for r in res["codebook"]]
        self.assertIn("B19013_001E", codes)
        self.assertIn("B19013_001M", codes)
        self.assertIn("B19013_001_CV", codes)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(res["codebook"][0]["Universe"], "Households")

    @patch("requests.Session.get")
    def test_query_data_profile(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "DP03_0009PE", "DP03_0009PM", "state"],
            ["New York", "6.3", "0.1", "36"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        res = srv.query_data_profile("dp03", year=2022, state_code="36", compute_reliability=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["dataset"], "profile")
        self.assertEqual(res["data"][0]["GEOID"], "36")
        row = res["data"][0]
        # Without API labels, estimates keep their raw codes; derived columns
        # are labelled from them.
        self.assertIn("DP03_0009PE", row)
        self.assertIn("Coefficient of Variation (%)!!DP03_0009PE", row)
        self.assertAlmostEqual(row["Coefficient of Variation (%)!!DP03_0009PE"], 0.1 / 1.645 / 6.3 * 100)
        urls = [c.args[0] for c in mock_get.call_args_list]
        self.assertTrue(any(u.endswith("/2022/acs/acs5/profile") for u in urls))
        self.assertFalse(srv.query_data_profile("DP99")["success"])

    @patch("requests.Session.get")
    def test_batch_download_panel(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "B19013_001E", "B19013_001M", "state", "county", "tract"],
            ["Tract 1", "50000", "4935", "36", "061", "000100"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        res = srv.batch_download(
            state_code="36",
            table_code="B19013",
            start_year=2021,
            end_year=2022,
            geo_level="tract",
            county_codes=["061"],
            output_format="parquet",
            output_dir=self.test_dir,
            compute_reliability=True,
            inflation_base_year=2022,
            include_codebook=True,
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["successful_calls"], 2)
        self.assertTrue(os.path.exists(res["panel_path"]))
        self.assertTrue(os.path.exists(res["codebook_path"]))
        import pandas as pd

        panel = pd.read_parquet(res["panel_path"])
        self.assertEqual(panel["geoid"].tolist(), ["36061000100", "36061000100"])

    def test_batch_download_validation(self):
        res = srv.batch_download(
            state_code="36", table_code="B01001", start_year=2020, end_year=2020,
            geo_level="place", output_dir=self.test_dir,
        )
        self.assertFalse(res["success"])


if __name__ == "__main__":
    unittest.main()

