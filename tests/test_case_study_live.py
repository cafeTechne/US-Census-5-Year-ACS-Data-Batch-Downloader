# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live run of docs/case_study_mi_ny.md (PLAN_ROUND_13 E1), opt-in.

Skipped unless CENSUS_LIVE_TESTS=1 (CENSUS_API_KEY from the environment or
.env). Executes every ``bash`` block of the document in order, in a temporary
working directory (or CASE_STUDY_WORKDIR), then:

- spot-checks B19013_001 estimate and MOE for five counties per state against
  the Census Data API, requested directly with ``requests`` (not the toolkit,
  not its cache): they must be identical;
- checks that the bundle records the Census Data API as the data source and the
  regression states its interval target.

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_case_study_live.py
"""

import json
import os
import shutil
import tempfile
import unittest

import pandas as pd
import requests

from tests.test_case_study import run_document

LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"
SPOT_CHECK = {"26": ["003", "081", "125", "161", "163"], "36": ["029", "041", "047", "055", "061"]}
API = "https://api.census.gov/data/2024/acs/acs5"


def api_values(state, counties):
    """{GEOID: (name, estimate, moe)} straight from the Census Data API."""
    from census.api import DEFAULT_API_KEY

    params = {"get": "NAME,B19013_001E,B19013_001M", "for": "county:" + ",".join(counties),
              "in": f"state:{state}"}
    if DEFAULT_API_KEY:
        params["key"] = DEFAULT_API_KEY
    for attempt in range(3):
        try:
            resp = requests.get(API, params=params, timeout=60)
        except requests.RequestException as exc:  # its message holds the URL with the key: never show it
            if attempt == 2:
                raise AssertionError(f"Census API request failed ({type(exc).__name__})") from None
            continue
        if resp.status_code == 200:
            break
    if resp.status_code != 200:  # never raise_for_status: its message holds the URL with the key
        raise AssertionError(f"Census API returned HTTP {resp.status_code} for state {state}")
    header, *rows = resp.json()
    i = {h: k for k, h in enumerate(header)}
    return {r[i["state"]] + r[i["county"]]: (r[i["NAME"]], int(r[i["B19013_001E"]]), int(r[i["B19013_001M"]]))
            for r in rows}


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census API tests")
class TestCaseStudyLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = os.getenv("CASE_STUDY_WORKDIR") or tempfile.mkdtemp(prefix="case_study_")
        cls.outputs = run_document(cls.workdir)

    @classmethod
    def tearDownClass(cls):
        if not os.getenv("CASE_STUDY_WORKDIR"):
            shutil.rmtree(cls.workdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.workdir, "case_study_output", *parts)

    def test_spot_check_against_api(self):
        rows = []
        for state, counties in SPOT_CHECK.items():
            panel = pd.read_csv(self.path("extract", state, "B19013", "B19013_county_panel_2024.csv"),
                                dtype={"geoid": str}).set_index("geoid")
            for geoid, (name, est, moe) in api_values(state, counties).items():
                self.assertEqual(int(panel.loc[geoid, "b19013_001_est"]), est, geoid)
                self.assertEqual(int(panel.loc[geoid, "b19013_001_moe"]), moe, geoid)
                rows.append(f"| {name} | {geoid} | {est:,} | {moe:,} |")
        self.assertEqual(len(rows), 10)
        print("\n" + "\n".join(rows))

    def test_bundle_and_regression(self):
        with open(self.path("bundle", "bundle.json"), encoding="utf-8") as f:
            bundle = json.load(f)
        prov = bundle["provenance"]
        self.assertEqual(prov["data_source"], {"kind": "census_api", "synthetic": False})
        self.assertEqual(prov["analysis_status"], "exploratory")
        self.assertEqual(sorted({e["table"] for e in prov["extractions"]}), ["B15002", "B19013"])
        self.assertTrue(all(e["failed_calls"] == 0 for e in prov["extractions"]))
        self.assertIn("Retrieved via the Census Data API", bundle["citation"])
        with open(self.path("results", "regression.json"), encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual(reg["n_obs"], 145)
        self.assertEqual(reg["interval"]["target"], "population")
        coef = {c["term"]: c for c in reg["coefficients"]}["pct_bachelors"]
        self.assertGreater(coef["ci_lower"], 0)
        replay = [o for n, o in self.outputs if n == "replay"][0]
        self.assertTrue(replay["verified"])


if __name__ == "__main__":
    unittest.main()
