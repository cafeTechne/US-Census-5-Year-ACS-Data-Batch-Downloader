"""Live Census API contract tests (opt-in).

Skipped unless CENSUS_LIVE_TESTS=1. These exercise real response shapes that
mocks cannot (duplicate NAME columns, comparison-profile codes, ZCTA lists with
missing codes, catalog discovery). Responses are not cached.

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_live.py
"""

import os
import shutil
import tempfile
import unittest

import pandas as pd

LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census API tests")
class TestLiveContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from census.api import CensusAPIClient

        cls.client = CensusAPIClient(cache=None)

    def _fetch(self, **kw):
        data = self.client.fetch(**kw)
        self.assertIsNotNone(data, self.client.last_error)
        self.assertGreater(len(data), 1)
        return data

    def test_every_geography_level(self):
        cases = [
            dict(geo_level="us", state_code=None),
            dict(geo_level="state", state_code="36"),
            dict(geo_level="county", state_code="36", county_code="061"),
            dict(geo_level="tract", state_code="36", county_code="061"),
            dict(geo_level="place", state_code="48", place_code="05000"),
            dict(geo_level="zcta", state_code=None, zcta_code="10001,10002"),
            dict(geo_level="msa", state_code=None, msa_code="12420"),
        ]
        for case in cases:
            with self.subTest(case=case["geo_level"]):
                header = self._fetch(year=2022, table_code="B19013", **case)[0]
                self.assertEqual(header.count("NAME"), 2)  # pipeline must dedupe

    def test_zcta_list_with_missing_code(self):
        data = self._fetch(year=2019, table_code="B19013", geo_level="zcta", state_code="36", zcta_code="10001,99999")
        self.assertEqual(len(data) - 1, 1)

    def test_each_dataset(self):
        for table in ("DP03", "S1701", "CP03"):
            with self.subTest(table=table):
                self._fetch(year=2022, table_code=table, geo_level="state", state_code="36")

    def test_catalog_lists_archived_vintages(self):
        from census.metadata import archived_vintages, available_vintages

        live = available_vintages("profile", self.client, refresh=True)
        self.assertTrue(set(archived_vintages("profile")) <= set(live))

    def test_archive_matches_live_metadata(self):
        from census.metadata import table_variables

        archived, _ = table_variables("DP02", "profile", 2019)
        live = self.client.fetch_variable_metadata(2019, "DP02", "profile")
        self.assertEqual(archived["DP02_0016E"]["label"], str(live["DP02_0016E"]["label"]).strip())

    def test_end_to_end_renumbered_panel(self):
        from census.pipeline import ResearchOptions, build_entities, run_batch

        out = tempfile.mkdtemp()
        try:
            res = run_batch(
                "DP02", build_entities("state", "36"), [2018, 2019], out, "panel",
                ResearchOptions(use_cache=False), client=self.client, variables=["DP02_0016"],
            )
            self.assertTrue(res["success"], res.get("error"))
            panel = pd.read_csv(res["panel_path"])
            cols = [c for c in panel.columns if "average_household_size" in c and c.endswith("_est")]
            self.assertEqual(len(cols), 1)
            self.assertEqual(panel[cols[0]].notna().sum(), 2)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
