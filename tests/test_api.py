"""Unit tests for census.api module."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from census.api import CensusAPIClient, build_geo_params, fetch_census_data, infer_dataset
from census.cache import QueryCache, make_cache_key


def _mock_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


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



class TestExtendedGeographies(unittest.TestCase):
    def test_tract_params(self):
        params = build_geo_params("tract", state_code="36", county_code="061")
        self.assertEqual(params["for"], "tract:*")
        self.assertEqual(params["in"], "state:36 county:061")
        all_tracts = build_geo_params("tract", state_code="36")
        self.assertEqual(all_tracts["in"], "state:36 county:*")
        with self.assertRaises(ValueError):
            build_geo_params("tract")

    def test_zcta_params_respect_vintage(self):
        old = build_geo_params("zcta", state_code="36", zcta_code="10001", year=2019)
        self.assertEqual(old["for"], "zip code tabulation area:10001")
        self.assertEqual(old["in"], "state:36")
        new = build_geo_params("zcta", state_code="36", zcta_code="10001", year=2020)
        self.assertNotIn("in", new)
        self.assertEqual(build_geo_params("zcta")["for"], "zip code tabulation area:*")

    def test_msa_and_us_params(self):
        msa = build_geo_params("msa", msa_code="12420")
        self.assertEqual(
            msa["for"],
            "metropolitan statistical area/micropolitan statistical area:12420",
        )
        self.assertEqual(build_geo_params("us"), {"for": "us:1"})

    def test_infer_dataset(self):
        self.assertEqual(infer_dataset("B19013"), "acs5")
        self.assertEqual(infer_dataset("C15002"), "acs5")
        self.assertEqual(infer_dataset("DP03"), "profile")
        self.assertEqual(infer_dataset("S1701"), "subject")
        self.assertEqual(infer_dataset("CP03"), "cprofile")

    @patch("requests.Session.get")
    def test_profile_endpoint(self, mock_get):
        mock_get.return_value = _mock_response([["NAME", "DP03_0062E"], ["NY", "1"]])
        client = CensusAPIClient(api_key="k")
        client.fetch(year=2022, state_code="36", table_code="DP03", geo_level="state")
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://api.census.gov/data/2022/acs/acs5/profile")
        self.assertEqual(kwargs["params"]["get"], "NAME,group(DP03)")
        self.assertEqual(kwargs["params"]["key"], "k")

    @patch("requests.Session.get")
    def test_empty_api_key_omitted(self, mock_get):
        mock_get.return_value = _mock_response([["NAME"], ["US"]])
        CensusAPIClient(api_key="").fetch(2022, None, "B01001", geo_level="us")
        _, kwargs = mock_get.call_args
        self.assertNotIn("key", kwargs["params"])

    def test_unknown_dataset(self):
        with self.assertRaises(ValueError):
            CensusAPIClient(api_key="").fetch(
                2022, "36", "B01001", geo_level="state", dataset="nope"
            )

    @patch("requests.Session.get")
    def test_list_geographies(self, mock_get):
        mock_get.return_value = _mock_response(
            [["NAME", "state", "county"], ["Kings County, New York", "36", "047"]]
        )
        rows = CensusAPIClient(api_key="").list_geographies(2022, "county", "36")
        self.assertEqual(rows, [{"NAME": "Kings County, New York", "state": "36", "county": "047"}])
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["get"], "NAME")

    @patch("requests.Session.get")
    def test_fetch_variable_metadata(self, mock_get):
        mock_get.return_value = _mock_response(
            {"variables": {"DP03_0062E": {"label": "Estimate!!Median household income"}}}
        )
        meta = CensusAPIClient(api_key="").fetch_variable_metadata(2022, "DP03")
        self.assertIn("DP03_0062E", meta)
        args, _ = mock_get.call_args
        self.assertTrue(args[0].endswith("/2022/acs/acs5/profile/groups/DP03.json"))


class TestQueryCache(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cache = QueryCache(os.path.join(self.test_dir, "c", "cache.db"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cache_key_ignores_api_key(self):
        self.assertEqual(
            make_cache_key("u", {"get": "x", "key": "a"}),
            make_cache_key("u", {"get": "x", "key": "b"}),
        )
        self.assertNotEqual(
            make_cache_key("u", {"get": "x"}), make_cache_key("u", {"get": "y"})
        )

    def test_set_get_clear(self):
        self.assertIsNone(self.cache.get("u", {"a": "1"}))
        self.cache.set("u", {"a": "1"}, [["NAME"], ["X"]])
        self.assertEqual(self.cache.get("u", {"a": "1"}), [["NAME"], ["X"]])
        self.assertEqual(self.cache.stats()["entries"], 1)
        self.assertEqual(self.cache.clear(), 1)
        self.assertIsNone(self.cache.get("u", {"a": "1"}))

    @patch("requests.Session.get")
    def test_client_uses_cache(self, mock_get):
        mock_get.return_value = _mock_response([["NAME", "B01001_001E"], ["NY", "5"]])
        client = CensusAPIClient(api_key="k", cache=self.cache)
        first = client.fetch(2020, "36", "B01001", geo_level="state")
        second = client.fetch(2020, "36", "B01001", geo_level="state")
        self.assertEqual(first, second)
        self.assertEqual(mock_get.call_count, 1)
        client.fetch(2021, "36", "B01001", geo_level="state")
        self.assertEqual(mock_get.call_count, 2)

    @patch("requests.Session.get")
    def test_failures_not_cached(self, mock_get):
        import requests

        mock_get.side_effect = requests.ConnectionError("offline")
        client = CensusAPIClient(api_key="k", cache=self.cache)
        self.assertIsNone(client.fetch(2020, "36", "B01001", geo_level="state"))
        self.assertIn("offline", client.last_error)
        self.assertEqual(self.cache.stats()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
