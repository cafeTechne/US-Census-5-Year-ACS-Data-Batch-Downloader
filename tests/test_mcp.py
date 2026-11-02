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

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
