# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.analysis (PLAN_ROUND_8 A1-A4)."""

import os
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

from census.analysis import analysis_frame, attach_replicates, coverage_report
from census.derived import proportion
from census.panel import build_tidy_panel
from census.replicate import replicate_proportion
from census.variance import REPLICATE_COLUMNS, Z90, replicate_se


def _replicate_frame(seed=1, geoids=("01001", "01003", "01005", "01007")):
    rng = np.random.default_rng(seed)
    rows = []
    for g in geoids:
        tot = float(rng.integers(2000, 5000))
        num = float(rng.integers(100, 1000))
        for var, est, sd in (("B17001_001", tot, 40.0), ("B17001_002", num, 30.0)):
            reps = est + rng.normal(0, sd, 80)
            row = {"GEOID": g, "NAME": f"County {g}", "variable": var, "estimate": est}
            row.update(dict(zip(REPLICATE_COLUMNS, reps)))
            row["moe"] = float(Z90 * replicate_se(est, reps))
            rows.append(row)
    return pd.DataFrame(rows)


def _wide(reps):
    est = reps.pivot(index="GEOID", columns="variable", values="estimate").add_suffix("E")
    moe = reps.pivot(index="GEOID", columns="variable", values="moe").add_suffix("M")
    return pd.concat([est, moe], axis=1).reset_index()


SPEC = [{"name": "pov_rate", "op": "proportion", "num": "B17001_002", "den": "B17001_001"}]


class TestAnalysisFrame(unittest.TestCase):
    def test_published_se_cv_and_sentinels(self):
        df = pd.DataFrame({
            "GEO_ID": ["0500000US01001", "0500000US01003", "0500000US01005"],
            "B01003_001E": [1000, 500, -666666666],
            "B01003_001M": [164.5, -555555555, 10],
        })
        f = analysis_frame(df)
        self.assertEqual(list(f["GEOID"]), ["01001", "01003", "01005"])
        self.assertAlmostEqual(f["B01003_001_se"][0], 100.0)
        self.assertAlmostEqual(f["B01003_001_cv"][0], 10.0)
        self.assertEqual(f["B01003_001_se"][1], 0.0)  # controlled
        self.assertTrue(np.isnan(f["B01003_001"][2]) and np.isnan(f["B01003_001_se"][2]))
        self.assertEqual(f["B01003_001_method"][0], "published")

    def test_derived_matches_census_derived(self):
        wide = _wide(_replicate_frame())
        f = analysis_frame(wide, derive=SPEC)
        ref = proportion(wide["B17001_002E"], wide["B17001_002M"], wide["B17001_001E"], wide["B17001_001M"])
        np.testing.assert_allclose(f["pov_rate"], ref["estimate"])
        np.testing.assert_allclose(f["pov_rate_se"], ref["se"])
        self.assertIn("pov_rate", f.attrs["variables"])

    def test_files_and_slugged_panel(self):
        tmp = tempfile.mkdtemp()
        try:
            panel = pd.DataFrame({"GEOID": ["01001", "01001"], "Year": [2021, 2022], "NAME": ["a", "a"],
                                  "pov_est": [10.0, 12.0], "pov_moe": [1.645, 3.29]})
            path = os.path.join(tmp, "p.csv")
            panel.to_csv(path, index=False)
            f = analysis_frame(path, variables={"poverty": "pov"})
            np.testing.assert_allclose(f["poverty_se"], [1.0, 2.0])
            self.assertEqual(list(f["release"]), ["acs5_2021", "acs5_2022"])
            self.assertEqual(f["GEOID"][0], "01001")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestReplicates(unittest.TestCase):
    def test_attach_exact_se_and_derived_replicates(self):
        reps = _replicate_frame()
        f = analysis_frame(_wide(reps), derive=SPEC)
        out, long = attach_replicates(f, reps)
        self.assertEqual(set(long["variable"]), {"B17001_001", "B17001_002", "pov_rate"})
        self.assertEqual(len(long), 12)
        exact = replicate_proportion(reps, "B17001_002", "B17001_001")
        np.testing.assert_allclose(out["pov_rate_se"], exact["se"], rtol=1e-12)
        np.testing.assert_allclose(out["pov_rate"], exact["estimate"], rtol=1e-12)
        self.assertTrue((out["pov_rate_method"] == "replicate").all())
        sub = reps[reps["variable"] == "B17001_002"]
        np.testing.assert_allclose(out["B17001_002_se"],
                                   replicate_se(sub["estimate"], sub[REPLICATE_COLUMNS].to_numpy()))
        self.assertEqual(long.attrs["notes"], [])

    def test_panel_replicates_by_year(self):
        r21, r22 = _replicate_frame(1), _replicate_frame(2)
        w = pd.concat([_wide(r21).assign(Year=2021), _wide(r22).assign(Year=2022)], ignore_index=True)
        f = analysis_frame(w, derive=SPEC)
        out, long = attach_replicates(f, {2021: r21, 2022: r22})
        self.assertIn("Year", long.columns)
        e22 = replicate_proportion(r22, "B17001_002", "B17001_001")
        np.testing.assert_allclose(out.loc[out["Year"] == 2022, "pov_rate_se"], e22["se"], rtol=1e-12)
        with self.assertRaises(ValueError):
            attach_replicates(f, r21)

    def test_coverage_report(self):
        reps = _replicate_frame()
        wide = _wide(reps)
        wide.loc[0, "B17001_002M"] = -222222222
        f = analysis_frame(wide)
        rep = coverage_report(f, cv_threshold=5.0).set_index("variable")
        self.assertAlmostEqual(rep.loc["B17001_002", "share_se_undefined"], 0.25)
        self.assertFalse(rep.loc["B17001_002", "replicates_available"])
        _, long = attach_replicates(f, reps, replace_se=False)
        rep2 = coverage_report(f, long).set_index("variable")
        self.assertEqual(rep2.loc["B17001_001", "share_with_replicates"], 1.0)


class TestPanelSE(unittest.TestCase):
    def test_panel_gains_se_columns(self):
        frame = pd.DataFrame({"GEOID": ["1", "2"], "Year": [2022, 2022], "NAME": ["a", "b"],
                              "B1_001E": [10, 20], "B1_001M": [3.29, -555555555],
                              "DP1_0001PE": [5.0, 6.0], "DP1_0001PM": [1.645, -222222222]})
        panel, _ = build_tidy_panel([frame])
        cols = list(panel.columns)
        self.assertEqual(cols.index("B1_001_SE"), cols.index("B1_001M") + 1)
        np.testing.assert_allclose(panel["B1_001_SE"], [2.0, 0.0])
        self.assertAlmostEqual(panel["DP1_0001P_SE"][0], 1.0)
        self.assertTrue(np.isnan(panel["DP1_0001P_SE"][1]))
        panel2, _ = build_tidy_panel([frame], add_se=False)
        self.assertNotIn("B1_001_SE", panel2.columns)


if __name__ == "__main__":
    unittest.main()
