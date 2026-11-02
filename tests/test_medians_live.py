# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check: medians rebuilt from distribution tables vs Census-published medians.

Skipped unless CENSUS_LIVE_TESTS=1 (needs network; CENSUS_API_KEY is read from
the environment or .env by census.api). Same tolerances as the offline check
in tests/test_medians.py, on a fresh pull of every county in New Jersey and
Colorado for the 2023 release.

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_medians_live.py
"""

import os
import unittest

import pandas as pd

from census.medians import median_for_rows

LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"
YEAR = 2023
STATES = ("34", "08")


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census API tests")
class TestLiveMedianAgreement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from census.api import CensusAPIClient

        cls.client = CensusAPIClient(cache=None)

    def _get(self, state, variables):
        endpoint, params = self.client.build_request(
            year=YEAR, state_code=state, table_code=variables[0].split("_")[0],
            geo_level="county", dataset="acs5", get=",".join(variables),
        )
        rows = self.client._get_json(endpoint, params)
        self.assertIsNotNone(rows, self.client.last_error)
        df = pd.DataFrame(rows[1:], columns=rows[0])
        return df.set_index(df["state"] + df["county"])

    def _frame(self, table, n_lines, published, extra_lines=()):
        frames = []
        for state in STATES:
            est = [f"{table}_{i:03d}E" for i in range(1, n_lines + 1)]
            moe = [f"{table}_{i:03d}M" for i in range(1, n_lines + 1)]
            a = self._get(state, est + [f"{published}E", f"{published}M"])
            b = self._get(state, moe)
            frames.append(a.join(b.drop(columns=["state", "county"])))
        df = pd.concat(frames)
        pub = pd.to_numeric(df[f"{published}E"], errors="coerce")
        return df[pub > 0], pub[pub > 0], pd.to_numeric(df[f"{published}M"])[pub > 0]

    def test_gross_rent(self):
        df, pub, pub_moe = self._frame("B25063", 27, "B25064_001")
        out = median_for_rows(df, "B25063", YEAR)
        self.assertLessEqual((out["estimate"] - pub).abs().median(), 0.5)
        self.assertGreaterEqual(((out["estimate"] - pub).abs() <= 1.0).mean(), 0.95)
        ratio = (out["moe"] / pub_moe).dropna()
        self.assertTrue(0.9 <= ratio.median() <= 1.3, ratio.describe())

    def test_household_income(self):
        df, pub, pub_moe = self._frame("B19001", 17, "B19013_001")
        out = median_for_rows(df, "B19001", YEAR)
        rel = ((out["estimate"] - pub) / pub).abs()
        self.assertLess(rel.median(), 0.01)
        self.assertGreaterEqual((rel <= 0.02).mean(), 0.8)
        ratio = (out["moe"] / pub_moe).dropna()
        self.assertTrue(0.85 <= ratio.median() <= 1.3, ratio.describe())


if __name__ == "__main__":
    unittest.main()
