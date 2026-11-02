"""Unit tests for census.api module."""

import unittest
from unittest.mock import MagicMock, patch
from census.api import CensusAPIClient, fetch_census_data


class TestCensusAPI(unittest.TestCase):
    def test_client_init(self):
        client = CensusAPIClient(api_key="test_key", timeout=15, max_retries=2)
        self.assertEqual(client.api_key, "test_key")
        self.assertEqual(client.timeout, 15)

    @patch("requests.Session.get")
    def test_fetch_place(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [["NAME", "B01001_001E"], ["Place A", "5000"]]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        client = CensusAPIClient(api_key="dummy")
        data = client.fetch(
            year=2020,
            state_code="36",
            table_code="B01001",
            geo_level="place",
            place_code="51000",
        )
        self.assertIsNotNone(data)
        self.assertEqual(len(data), 2)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["for"], "place:51000")
        self.assertEqual(kwargs["params"]["in"], "state:36")

    @patch("requests.Session.get")
    def test_fetch_state(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "B01001_001E"],
            ["New York", "19000000"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        client = CensusAPIClient(api_key="dummy")
        data = client.fetch(
            year=2020,
            state_code="36",
            table_code="B01001",
            geo_level="state",
        )
        self.assertIsNotNone(data)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["for"], "state:36")
        self.assertNotIn("in", kwargs["params"])

    @patch("requests.Session.get")
    def test_fetch_county(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            ["NAME", "B01001_001E"],
            ["County A", "30000"],
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        client = CensusAPIClient(api_key="dummy")
        data = client.fetch(
            year=2020,
            state_code="36",
            table_code="B01001",
            geo_level="county",
            county_code="*",
        )
        self.assertIsNotNone(data)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["for"], "county:*")
        self.assertEqual(kwargs["params"]["in"], "state:36")

    def test_invalid_geo_level(self):
        client = CensusAPIClient(api_key="dummy")
        with self.assertRaises(ValueError):
            client.fetch(
                year=2020,
                state_code="36",
                table_code="B01001",
                geo_level="invalid_level",
            )

    def test_missing_place_code(self):
        client = CensusAPIClient(api_key="dummy")
        with self.assertRaises(ValueError):
            client.fetch(
                year=2020,
                state_code="36",
                table_code="B01001",
                geo_level="place",
                place_code=None,
            )


if __name__ == "__main__":
    unittest.main()
