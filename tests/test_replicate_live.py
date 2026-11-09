# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live Census VRE tests (opt-in).

Skipped unless CENSUS_LIVE_TESTS=1. Downloads real Variance Replicate Estimate
files from www2.census.gov into a temporary cache and checks that replicate
MOEs reproduce the published MOEs (validation check F3) and that the frozen
fixture still matches the live file. Requests are spaced out as a courtesy
(census.http also paces, retries, and handles the firewall page).

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_replicate_live.py
"""

import os
import shutil
import tempfile
import time
import unittest

import pandas as pd

LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "vre")


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census VRE tests")
class TestLiveVRE(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        time.sleep(2)  # be polite to www2.census.gov

    def _fetch(self, *args, **kw):
        from census.replicate import fetch_vre

        return fetch_vre(*args, cache_dir=os.path.join(self.tmp, str(args[1])), **kw)

    def test_state_file_reproduces_published_moe_and_fixture(self):
        from census.replicate import read_vre_csv

        frame = self._fetch("B19001", 2023, "state")
        check = frame.attrs["moe_check"]
        self.assertTrue(check["passed"], check)
        self.assertEqual(check["rows_checked"], 52 * 17)
        fixture = read_vre_csv(os.path.join(FIXTURES, "B19001_040_2023_subset.csv.zip"))
        live = frame[frame["GEOID"].isin(fixture["GEOID"].unique())].reset_index(drop=True)
        pd.testing.assert_frame_equal(live, fixture, check_like=False)

    def test_one_state_tracts(self):
        frame = self._fetch("B17001", 2023, "tract", state_code="10")
        check = frame.attrs["moe_check"]
        self.assertTrue(check["passed"], check)
        self.assertGreater(check["rows_checked"], 1000)
        self.assertTrue(frame["GEOID"].str.match(r"^10\d{9}$").all())

    def test_gzip_release(self):
        frame = self._fetch("B01003", 2014, "state")
        check = frame.attrs["moe_check"]
        self.assertTrue(check["passed"], check)
        self.assertEqual(check["controlled_rows"], 52)  # state total population is controlled

    def test_published_table_list_matches_registry(self):
        from census.replicate import vre_tables

        frozen = vre_tables(2024)
        live = vre_tables(2024, refresh=True, cache_dir=os.path.join(self.tmp, "lists"))
        self.assertEqual(live, frozen)


if __name__ == "__main__":
    unittest.main()
