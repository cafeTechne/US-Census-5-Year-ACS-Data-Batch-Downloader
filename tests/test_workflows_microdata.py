"""Offline end-to-end tests for the Round 10 workflows (PUMS, designs, places, web maps)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from census import pums
from census import workflows_microdata as w

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PUMS_FIXTURE = os.path.join(FIXTURES, "pums", "psam_p56_2024_1yr_subset.csv.gz")


def _fixture_pums(*_args, columns=None, **_kwargs):
    return pums.read_pums_csv(PUMS_FIXTURE, columns=columns)


class TestMicrodataWorkflows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def guided(self, res):
        self.assertTrue(res["success"], res)
        self.assertTrue(res["summary"])
        self.assertTrue(res["next_step"])
        json.dumps(res)

    def test_pums_tabulate_and_regression(self):
        with patch.object(pums, "fetch_pums", side_effect=_fixture_pums):
            res = w.pums_tabulate("56", 2024, "median", var="PINCP", where="AGEP >= 15",
                                  output_path=os.path.join(self.dir, "t.csv"))
            self.guided(res)
            self.assertGreater(res["preview"][0]["se"], 0)
            reg = w.pums_regression("56", 2024, "WAGP ~ AGEP", where="WAGP > 0")
            self.guided(reg)
            self.assertIn("sampling_replicate", reg["uncertainty_sources"])
            bad = w.pums_tabulate("56", 2024, "mode", var="PINCP")
            self.assertEqual(bad["error_code"], "INVALID_INPUT")

    def test_did_and_multilevel(self):
        rng = np.random.default_rng(3)
        rows = []
        for g in range(30):
            treated = int(g < 15)
            for year in (2013, 2018, 2023):
                effect = 2.0 if (treated and year == 2023) else 0.0
                rows.append({"GEOID": f"{g:05d}", "county": f"c{g % 6}", "treated": treated, "Year": year,
                             "x": rng.normal(),
                             "y": 10 + 0.5 * year / 1000 + effect + rng.normal(0, 0.5), "y_se": 0.4})
        path = os.path.join(self.dir, "panel.csv")
        pd.DataFrame(rows).to_csv(path, index=False)
        did = w.difference_in_differences(path, "y", "treated", 2023, se="y_se", reference=2018)
        self.guided(did)
        ml = w.multilevel_model(path, "y ~ x", "county", sampling_se="y_se")
        self.guided(ml)
        self.assertFalse(w.difference_in_differences(path, "y", "treated", 2023, period="nope")["success"])

    def test_place_adjustment(self):
        res = w.adjust_for_place(75000, "06", 2023)
        self.guided(res)
        self.assertLess(res["adjusted_value"], 75000)  # California prices exceed the national average

    @patch.dict(os.environ, {"CENSUS_GEO_DIR": os.path.join(FIXTURES, "geo")})
    def test_web_map_is_self_contained(self):
        from census import boundaries

        b = boundaries.get_boundaries("tract", 2023, state="11")
        df = pd.DataFrame({"GEOID": b["GEOID"].astype(str), "rate": np.linspace(5, 30, len(b))})
        df["rate_se"] = df["rate"] * 0.1
        path = os.path.join(self.dir, "f.csv")
        df.to_csv(path, index=False)
        res = w.make_web_map(path, "rate", "tract", 2023, se_col="rate_se",
                             output_path=os.path.join(self.dir, "m.html"))
        self.guided(res)
        html = open(os.path.join(self.dir, "m.html"), encoding="utf-8").read()
        self.assertNotIn("<script src=", html)


if __name__ == "__main__":
    unittest.main()
