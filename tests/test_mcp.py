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
        res = srv.adjust_inflation(50000, 2010, 2022, index="cpi-u")
        self.assertTrue(res["success"])
        self.assertAlmostEqual(res["adjusted_value"], 50000 * res["index_to"] / res["index_from"])
        self.assertIn("api.bls.gov", res["source"])
        bad = srv.adjust_inflation(1, 1900, 2022, index="cpi-u")
        self.assertEqual(bad["error_code"], "PRICE_INDEX_UNAVAILABLE")
        self.assertEqual(srv.adjust_inflation(1, 2010, 2022, index="ppi")["error_code"], "INVALID_INPUT")

    def test_adjust_inflation_defaults_to_r_cpi_u_rs(self):
        from census.inflation import load_price_index

        fixture = os.path.join(self.test_dir, "idx")
        os.makedirs(fixture)
        with open(os.path.join(fixture, "r_cpi_u_rs.csv"), "w") as f:
            f.write("year,value\n2010,320.0\n2022,432.0\n")  # fixture values
        with patch.dict(os.environ, {"CENSUS_PRICE_INDEX_DIR": fixture}):
            load_price_index.cache_clear()
            try:
                res = srv.adjust_inflation(100, 2010, 2022)
            finally:
                load_price_index.cache_clear()
        self.assertEqual(res["index"], "r-cpi-u-rs")
        self.assertAlmostEqual(res["adjusted_value"], 135.0)

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
        # Labels come from the 2022 vintage in the offline archive.
        cv_key = "Coefficient of Variation (%)!!EMPLOYMENT STATUS!!Civilian labor force!!Unemployment Rate"
        self.assertIn("Percent!!EMPLOYMENT STATUS!!Civilian labor force!!Unemployment Rate", row)
        self.assertAlmostEqual(row[cv_key], 0.1 / 1.645 / 6.3 * 100)
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
            price_index="cpi-u",
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


    # ---- Round 6: protocol, vintages, concordance review ---------------------

    def test_server_instructions_document_review(self):
        text = srv.mcp.instructions
        self.assertIn("plan_extraction", text)
        self.assertIn("DIFFERENT identity", text)
        protocol = srv.get_research_protocol()["protocol"]
        self.assertIn("Reviewer (agent or human)", protocol)
        self.assertIn("When in doubt, choose `split`", protocol)

    def test_list_vintages_and_describe_variable(self):
        v = srv.list_vintages("profile")
        self.assertIn(2019, v["years"])
        d2018 = srv.describe_variable("DP02_0002PE", 2018)
        d2019 = srv.describe_variable("DP02_0002PE", 2019)
        self.assertIn("Family households", d2018["label"])
        self.assertIn("Married-couple", d2019["label"])

    def test_list_variables_is_vintage_specific(self):
        res = srv.list_variables("B19013", year=2015)
        self.assertEqual(res["year"], 2015)
        self.assertEqual(res["metadata_source"], "archive")

    def test_check_variable_concordance(self):
        res = srv.check_variable_concordance("B27001", 2016, 2017, variables=["B27001_006"])
        self.assertTrue(res["success"])
        self.assertEqual(res["issues"][0]["kind"], "relabeled")
        self.assertIn("evidence", res["issues"][0])

    @patch("requests.Session.get")
    def test_plan_extraction_blocks_without_downloading(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [["NAME", "us"], ["United States", "1"]]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        plan = srv.plan_extraction(
            state_code="36", table_code="B27001", start_year=2016, end_year=2017,
            geo_level="state", variables=["B27001_006"],
        )
        self.assertTrue(plan["would_block"])
        self.assertNotIn("_concordance", plan)
        for call in mock_get.call_args_list:  # only national continuity calls
            self.assertEqual(call.kwargs["params"]["for"], "us:1")

    def test_draft_and_independent_agent_review(self):
        xw = os.path.join(self.test_dir, "crosswalks")
        with patch.dict(os.environ, {"CENSUS_CROSSWALK_DIR": xw}):
            draft = srv.draft_crosswalk_override(
                "B27001", ["2016:B27001_006", "2017:B27001_006"], "split",
                drafted_by="agent:model-a/session-1",
                rationale="Age band changed from 6-17 to 6-18; different measures.",
            )
            self.assertTrue(draft["success"])
            row = draft["draft_rows"][0]
            same = srv.record_crosswalk_review(row, "agent:model-a/session-1", "B27001")
            self.assertFalse(same["success"])
            ok = srv.record_crosswalk_review(row, "agent:model-b/session-2", "B27001")
            self.assertTrue(ok["success"], ok.get("error"))
            self.assertTrue(srv.validate_crosswalks()["valid"])
        bad = srv.draft_crosswalk_override("B27001", ["B27001_006"], "split", "agent:x/1", "r")
        self.assertEqual(bad["error_code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()

