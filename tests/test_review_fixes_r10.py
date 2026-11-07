# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the Round 10 code-review findings (phase10 branch)."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from census import pums as P
from census import workflows_microdata as wm
from census.multitest import batch_z_test
from census.stats import acs_overlap_years, z_test

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PUMS_FIXTURE = os.path.join(FIXTURES, "pums", "psam_p56_2024_1yr_subset.csv.gz")
SE_INDEPENDENT = (2 ** 0.5) * 10 / 1.645  # 8.597


def _fixture_pums(*_args, columns=None, **_kwargs):
    return P.read_pums_csv(PUMS_FIXTURE, columns=columns)


def _flat_weights(n):
    df = pd.DataFrame({"PWGTP": np.full(n, 10.0)})
    for r in range(1, 81):
        df[f"PWGTP{r}"] = 10.0
    return df


class TestProductAwareSignificance(unittest.TestCase):
    """Items 1 and 6: 1-year comparisons use the 1-year period length."""

    def test_calculate_significance_one_year(self):
        import mcp_server

        r = mcp_server.calculate_significance(100, 10, 90, 10, year1=2022, year2=2023, product="1-year")
        self.assertAlmostEqual(r["se_difference"], SE_INDEPENDENT, places=6)
        self.assertEqual(r["overlap_years"], 0)
        five = mcp_server.calculate_significance(100, 10, 90, 10, year1=2022, year2=2023)
        self.assertEqual(five["overlap_years"], 4)
        bad = mcp_server.calculate_significance(100, 10, 90, 10, year1=2022, year2=2023, product="10-year")
        self.assertFalse(bad["success"])

    def test_batch_significance_one_year(self):
        from census import workflows

        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "in.csv")
            pd.DataFrame({"GEOID": ["01", "02"], "e1": [100, 50], "m1": [10, 5],
                          "e2": [90, 40], "m2": [10, 5]}).to_csv(path, index=False)
            res = workflows.batch_significance(path, "e1", "m1", "e2", "m2", 2022, 2023,
                                               output_path=os.path.join(d, "o.csv"), product="1-year")
            self.assertTrue(res["success"], res)
            out = pd.read_csv(res["output_path"])
            self.assertTrue((out["overlap_years"] == 0).all())
            self.assertTrue((out["overlap_factor"] == 1.0).all())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_batch_z_test_same_one_year_release_independent(self):
        df = pd.DataFrame({"e1": [100.0], "m1": [10.0], "e2": [90.0], "m2": [10.0]})
        out = batch_z_test(df, "e1", "m1", "e2", "m2", 2023, 2023, product="1-year")
        self.assertAlmostEqual(out["se_difference"].iloc[0], SE_INDEPENDENT, places=6)
        self.assertEqual(out["overlap_factor"].iloc[0], 1.0)

    def test_z_test_same_one_year_release(self):
        r = z_test(100, 10, 90, 10, overlap_years=acs_overlap_years(2023, 2023, "1-year"), product="1-year")
        self.assertAlmostEqual(r["se_difference"], 8.60, places=2)
        self.assertAlmostEqual(r["se_difference"], SE_INDEPENDENT, places=9)


class TestPumsFixes(unittest.TestCase):
    def test_proportion_expression_excludes_missing(self):
        """Item 2: blanks in a referenced variable leave numerator and denominator."""
        df = _flat_weights(200)
        wagp = np.full(200, np.nan)
        wagp[:55] = 60000.0
        wagp[55:100] = 1000.0
        df["WAGP"] = wagp
        out = P.tabulate(df, "proportion", "WAGP > 50000")
        self.assertAlmostEqual(float(out["estimate"].iloc[0]), 0.55)
        self.assertEqual(int(out["n_unweighted"].iloc[0]), 100)

    def test_regression_on_concatenated_states(self):
        """Item 7: duplicate row labels from pd.concat must not break alignment."""
        one = P.read_pums_csv(PUMS_FIXTURE, columns=["WAGP", "AGEP"])
        both = pd.concat([one, one])
        single = P.pums_regression("WAGP ~ AGEP", one, where="WAGP > 0")
        res = P.pums_regression("WAGP ~ AGEP", both, where="WAGP > 0")
        self.assertEqual(res.n_obs, 2 * single.n_obs)
        b1 = single.coefficients.set_index("term")["estimate"]
        b2 = res.coefficients.set_index("term")["estimate"]
        np.testing.assert_allclose(b1.to_numpy(), b2.to_numpy(), rtol=1e-8)

    def test_cache_check_skips_rehash_when_unchanged(self):
        """Item 8: size+mtime recorded; full SHA only when they change."""
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "x.zip")
            meta = P.write_cache_entry(path, b"abc" * 1000, "https://example.invalid/x.zip")
            self.assertIn("mtime_ns", meta)
            with patch.object(P, "_sha256_file", side_effect=AssertionError("rehashed")):
                self.assertIsNotNone(P.read_cache_entry(path))
            with open(path, "wb") as f:  # changed content and size: hash is checked and fails
                f.write(b"tampered")
            self.assertIsNone(P.read_cache_entry(path))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_product_normalization_shared_with_api(self):
        """Item 9: pums uses census.api.normalize_product (e.g. '1' and 'one-year')."""
        self.assertFalse(hasattr(P, "PRODUCTS"))
        self.assertIn("/1-Year/", P.pums_url("WY", 2024, "one-year"))
        self.assertEqual(P.release_label(2024, "1"), "ACS 2024 1-year PUMS")

    def test_puma_vintage_2018_2022_is_mixed(self):
        """Item 3: 2018-2022 5-year PUMS carries PUMA10 and PUMA20 (data dictionary)."""
        with self.assertRaises(ValueError):
            P.puma_vintage(2022, "5-year")
        self.assertEqual(P.puma_vintage(2023, "5-year"), 2020)
        self.assertEqual(P.puma_vintage(2024, "5-year"), 2020)
        self.assertEqual(P.puma_vintage(2021, "5-year"), 2010)
        self.assertEqual(P.puma_vintage(2022, "1-year"), 2020)
        df = pd.DataFrame({"STATE": ["56", "56"], "PUMA10": ["00100", "-0009"], "PUMA20": ["-0009", "00200"]})
        with self.assertRaises(ValueError):
            P.puma_geoid(df)
        g = P.puma_geoid(df, puma="PUMA20")
        self.assertTrue(pd.isna(g.iloc[0]))
        self.assertEqual(g.iloc[1], "5600200")


class TestMicrodataWorkflowFixes(unittest.TestCase):
    def test_pums_tabulate_ratio_denominator(self):
        """Item 4."""
        with patch.object(P, "fetch_pums", side_effect=_fixture_pums):
            res = wm.pums_tabulate("56", 2024, "ratio", var="WAGP", denominator="PINCP",
                                   where="PINCP > 0", output_path=os.path.join(tempfile.gettempdir(),
                                                                               "r10_ratio.csv"))
        self.assertTrue(res["success"], res)
        self.assertTrue(0 < res["preview"][0]["estimate"] <= 1.5)

    def test_did_one_year_panel(self):
        """Item 5: consecutive 1-year releases are not overlapping periods."""
        rng = np.random.default_rng(1)
        rows = []
        for g in range(30):
            t = int(g < 15)
            for year in (2019, 2021, 2022, 2023):
                rows.append({"GEOID": f"{g:05d}", "treated": t, "Year": year,
                             "y": 10 + (2.0 if t and year >= 2022 else 0) + rng.normal(0, 0.5), "y_se": 0.4})
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "panel.csv")
            pd.DataFrame(rows).to_csv(path, index=False)
            self.assertFalse(wm.difference_in_differences(path, "y", "treated", 2022, se="y_se")["success"])
            res = wm.difference_in_differences(path, "y", "treated", 2022, se="y_se", product="1-year")
            self.assertTrue(res["success"], res)
            res = wm.difference_in_differences(path, "y", "treated", 2022, se="y_se", period_years=1)
            self.assertTrue(res["success"], res)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_adjust_for_place_one_year_caption(self):
        """Item 10: 1-year dollars do not get the multi-year period note."""
        one = wm.adjust_for_place(75000, "06", 2023, product="1-year")
        five = wm.adjust_for_place(75000, "06", 2023)
        self.assertTrue(one["success"], one)
        self.assertFalse(any("5-year" in n for n in one["adjustment"]["notes"]))
        self.assertTrue(any("5-year" in n for n in five["adjustment"]["notes"]))

    def test_web_map_citation_names_product(self):
        """Item 10."""
        from census.web_maps import census_citation

        self.assertIn("1-year", census_citation("1-year"))
        self.assertIn("5-year", census_citation(None))

    @patch.dict(os.environ, {"CENSUS_GEO_DIR": os.path.join(FIXTURES, "geo")})
    def test_make_web_map_one_year_footer(self):
        from census import boundaries

        b = boundaries.get_boundaries("tract", 2023, state="11")
        df = pd.DataFrame({"GEOID": b["GEOID"].astype(str), "rate": np.linspace(5, 30, len(b))})
        df["rate_se"] = df["rate"] * 0.1
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "f.csv")
            df.to_csv(path, index=False)
            out = os.path.join(d, "m.html")
            res = wm.make_web_map(path, "rate", "tract", 2023, se_col="rate_se", output_path=out,
                                  product="1-year")
            self.assertTrue(res["success"], res)
            html = open(out, encoding="utf-8").read()
            self.assertIn("American Community Survey 1-year estimates", html)
            self.assertNotIn("5-year estimates", html)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
