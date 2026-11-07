# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.did: published reference, analytic special cases, overlap enforcement, replicates.

Reference: Card, D. & Krueger, A.B. (1994), "Minimum Wages and Employment: A
Case Study of the Fast-Food Industry in New Jersey and Pennsylvania", AER
84(4), 772-793, Table 3 (mean FTE employment per store, SEs in parentheses;
also reproduced in Angrist & Pischke 2009, Mostly Harmless Econometrics,
Table 5.2.1):

                         PA            NJ            NJ - PA
    1. before         23.33 (1.35)  20.44 (0.51)  -2.89 (1.44)
    2. after          21.17 (0.94)  21.03 (0.52)  -0.14 (1.07)
    3. change         -2.16 (1.25)   0.59 (0.54)   2.76 (1.36)

Rows 1-2 combine independent state samples, so their difference SEs are
sqrt(se_PA^2 + se_NJ^2); row 3 uses the change estimates (the same stores
in both waves, so the change SEs are not functions of the level SEs).
"""

import math
import unittest

import numpy as np
import pandas as pd

from census.did import did_from_changes, difference_in_differences
from census.simulation import make_replicates, replicate_long, run_study, designs_config

CK = {"PA": {"before": (23.33, 1.35), "after": (21.17, 0.94), "change": (-2.16, 1.25)},
      "NJ": {"before": (20.44, 0.51), "after": (21.03, 0.52), "change": (0.59, 0.54)}}


class TestCardKrueger(unittest.TestCase):
    def test_did_from_published_changes(self):
        r = did_from_changes([CK["PA"]["change"][0], CK["NJ"]["change"][0]],
                             [CK["PA"]["change"][1], CK["NJ"]["change"][1]], [0, 1])
        # 0.59 - (-2.16) = 2.75 from the rounded table entries; published 2.76 from unrounded means
        self.assertAlmostEqual(r["estimate"], 2.76, delta=0.011)
        self.assertAlmostEqual(r["se"], 1.36, delta=0.005)

    def test_level_differences_independent_samples(self):
        for wave, (est, se) in (("before", (-2.89, 1.44)), ("after", (-0.14, 1.07))):
            r = did_from_changes([0.0, CK["NJ"][wave][0] - CK["PA"][wave][0]],
                                 [CK["PA"][wave][1], CK["NJ"][wave][1]], [0, 1])
            self.assertAlmostEqual(r["estimate"], est, delta=0.006)
            self.assertAlmostEqual(r["se"], se, delta=0.005)


def _panel(n=120, years=(2008, 2013, 2018, 2023), delta=3.0, sigma_u=1.0, seed=0, se_scale=2.0):
    rng = np.random.default_rng(seed)
    T = (np.arange(n) < n // 2).astype(int)
    alpha = rng.normal(50, 10, n)
    rows = []
    for k, yr in enumerate(years):
        se = rng.uniform(0.5, 1.5, n) * se_scale
        th = alpha + 2 * k + delta * T * (yr >= 2018) + sigma_u * rng.standard_normal(n)
        y = th + se * rng.standard_normal(n)
        rows.append(pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(n)], "Year": yr, "y": y, "y_se": se,
                                  "treated": T, "theta": th}))
    return pd.concat(rows, ignore_index=True)


def _coef(res, term, col="estimate"):
    return float(res.coefficients.set_index("term").loc[term, col])


class TestAnalyticCases(unittest.TestCase):
    def test_two_period_equal_variance_equals_difference_of_mean_changes(self):
        f = _panel(years=(2013, 2018))
        f["y_se"] = 1.0  # equal variances: GLS = OLS on changes = difference of mean changes
        r = difference_in_differences(f, "y", "treated", 2018)
        w = f.pivot(index="GEOID", columns="Year", values="y")
        ch = w[2018] - w[2013]
        tr = f.drop_duplicates("GEOID").set_index("GEOID")["treated"]
        expect = ch[tr == 1].mean() - ch[tr == 0].mean()
        self.assertAlmostEqual(_coef(r, "did"), expect, places=8)
        # SE: residual variance of changes pooled across groups (REML)
        resid = np.concatenate([ch[tr == 1] - ch[tr == 1].mean(), ch[tr == 0] - ch[tr == 0].mean()])
        s2 = resid @ resid / (len(ch) - 2)
        se = math.sqrt(s2 * (1 / (tr == 1).sum() + 1 / (tr == 0).sum()))
        self.assertAlmostEqual(_coef(r, "did", "se_model"), se, delta=1e-4 * se)
        self.assertIsNone(r.diagnostics["parallel_trends"])

    def test_noise_free_recovers_delta(self):
        f = _panel(sigma_u=0.0, se_scale=1e-6)
        f["y"] = f["theta"]
        r = difference_in_differences(f, "y", "treated", 2018)
        self.assertAlmostEqual(_coef(r, "did"), 3.0, places=4)

    def test_event_design_and_parallel_trends(self):
        f = _panel(years=(2003, 2008, 2013, 2018, 2023))
        r = difference_in_differences(f, "y", "treated", 2018, design="event")
        terms = set(r.coefficients["term"])
        self.assertTrue({"lead[2003]", "lead[2008]", "lag[2018]", "lag[2023]"} <= terms)
        self.assertNotIn("lead[2013]", terms)  # reference period
        pt = r.diagnostics["parallel_trends"]
        self.assertEqual(pt["df"], 2)
        self.assertGreater(pt["p_value"], 0.001)
        # violated trends are flagged
        g = f.copy()
        g.loc[(g.treated == 1), "y"] += (g.loc[g.treated == 1, "Year"] - 2013) * 1.0
        r2 = difference_in_differences(g, "y", "treated", 2018)
        self.assertLess(r2.diagnostics["parallel_trends"]["p_value"], 0.01)
        self.assertTrue(any("PARALLEL TRENDS" in n for n in r2.notes))

    def test_fewer_than_three_pre_periods_no_diagnostic(self):
        r = difference_in_differences(_panel(), "y", "treated", 2018)
        self.assertIsNone(r.diagnostics["parallel_trends"])
        self.assertTrue(any(">= 3" in n for n in r.notes))


class TestValidation(unittest.TestCase):
    def test_overlapping_periods_refused(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            difference_in_differences(_panel(years=(2010, 2013, 2018, 2023)), "y", "treated", 2018)

    def test_one_year_periods_allowed(self):
        r = difference_in_differences(_panel(years=(2016, 2017, 2018, 2019)), "y", "treated", 2018, period_years=1)
        self.assertEqual(r.model_type, "did_two_group")

    def test_varying_treatment_refused(self):
        f = _panel()
        f.loc[0, "treated"] = 1 - f.loc[0, "treated"]
        with self.assertRaises(ValueError):
            difference_in_differences(f, "y", "treated", 2018)

    def test_these_areas_needs_replicates(self):
        with self.assertRaises(ValueError):
            difference_in_differences(_panel(), "y", "treated", 2018, target="these_areas")


class TestReplicates(unittest.TestCase):
    def test_replicate_se_close_to_analytic(self):
        f = _panel(n=200, seed=2)
        rng = np.random.default_rng(9)
        parts = []
        for yr, sub in f.groupby("Year"):
            y = sub["y"].to_numpy()[:, None]
            parts.append(replicate_long(sub["GEOID"], ["y"], y,
                                        make_replicates(y, sub[["y_se"]].to_numpy(), rng)).assign(Year=yr))
        r = difference_in_differences(f, "y", "treated", 2018, replicates=pd.concat(parts), target="these_areas")
        se_rep = _coef(r, "did", "se_sampling")
        self.assertLess(abs(se_rep / r.diagnostics["se_sampling_analytic"]["did"] - 1), 0.35)
        self.assertLess(se_rep, _coef(r, "did", "se_model"))
        self.assertIn("sampling_replicate", r.uncertainty_sources)
        self.assertEqual(r.method, "gls_reml_replicate_full")
        g = difference_in_differences(f, "y", "treated", 2018, replicates=pd.concat(parts), target="these_areas",
                                      replicate_refit="gls")
        self.assertEqual(g.method, "gls_reml_replicate_gls")
        with self.assertRaises(ValueError):
            difference_in_differences(f, "y", "treated", 2018, replicate_refit="bogus")


class TestSimulationSmoke(unittest.TestCase):
    def test_did_coverage_fast(self):
        df = run_study(designs_config(fast=True), studies=["did_two_group"])
        self.assertGreater(float(df.iloc[0]["coverage"]), 0.75)


if __name__ == "__main__":
    unittest.main()
