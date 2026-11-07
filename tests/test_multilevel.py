# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.multilevel: reference reproduction, special cases, replicates, simulation smoke test.

Reference values (tests/fixtures/multilevel/dietox_reference.json) are copied
from the printed output of the statsmodels "Linear Mixed Effects Models"
example notebook,
https://www.statsmodels.org/stable/examples/notebooks/generated/mixed_lm_example.html
(dietox data, R geepack), verified against the page on 2026-11-03.
"""

import json
import os
import unittest

import numpy as np
import pandas as pd

from census.multilevel import BlockGLS, _known_vfun, _spd_inverse, fit_multilevel
from census.sae import fay_herriot
from census.simulation import (
    DESIGN_STUDIES, coverage_summary, designs_config, make_replicates, replicate_long, run_study,
)

HERE = os.path.dirname(__file__)
DIETOX = os.path.join(HERE, "fixtures", "multilevel", "dietox.csv")


def _coef(res, term, col="estimate"):
    return float(res.coefficients.set_index("term").loc[term, col])


REF = os.path.join(HERE, "fixtures", "multilevel", "dietox_reference.json")


class TestDietoxReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = pd.read_csv(DIETOX)
        cls.d["Weight_se"] = 0.0
        with open(REF, encoding="utf-8") as f:
            cls.ref = json.load(f)

    def _check_fixed(self, r, ref):
        un = r.diagnostics.get("se_model_unadjusted") or {}
        for term in ("Intercept", "Time"):
            self.assertAlmostEqual(_coef(r, term), ref[term]["estimate"], places=3)
            se = un.get(term, _coef(r, term, "se_model"))
            self.assertAlmostEqual(se, ref[term]["se"], places=3)

    def test_mixedlm_path_reproduces_documented_example(self):
        ref = self.ref["random_intercept"]
        r = fit_multilevel("Weight ~ Time", self.d, "Pig")
        self._check_fixed(r, ref)
        vc = r.diagnostics["variance_components"]
        self.assertAlmostEqual(vc["group_cov"]["Intercept"]["Intercept"], ref["group_var"], delta=0.0015)
        self.assertAlmostEqual(vc["residual_var"], ref["scale"], delta=0.0005)
        self.assertAlmostEqual(r.diagnostics["loglik"], ref["loglik"], places=3)

    def test_known_variance_engine_with_zero_variance_matches_documented_example(self):
        ref = self.ref["random_intercept"]
        r = fit_multilevel("Weight ~ Time", self.d, "Pig", sampling_se="Weight_se")
        self._check_fixed(r, ref)
        vc = r.diagnostics["variance_components"]
        self.assertAlmostEqual(vc["group_cov"]["Intercept"]["Intercept"], ref["group_var"], delta=0.005)
        self.assertAlmostEqual(vc["area_var"], ref["scale"], delta=0.001)
        self.assertAlmostEqual(r.diagnostics["loglik"], ref["loglik"], places=3)

    def test_random_slope_matches_documented_example(self):
        ref = self.ref["random_slope"]
        r = fit_multilevel("Weight ~ Time", self.d, "Pig", re_formula="~Time", sampling_se="Weight_se")
        self._check_fixed(r, ref)
        G = r.diagnostics["variance_components"]["group_cov"]
        self.assertAlmostEqual(G["Time"]["Intercept"], ref["group_time_cov"], places=3)
        self.assertAlmostEqual(G["Time"]["Time"], ref["time_var"], places=3)
        self.assertAlmostEqual(G["Intercept"]["Intercept"], ref["group_var_lmer"], places=2)
        self.assertAlmostEqual(r.diagnostics["variance_components"]["area_var"], ref["scale"], delta=0.001)
        self.assertAlmostEqual(r.diagnostics["loglik"], ref["loglik"], places=3)

    def test_kenward_roger_balanced_random_slope_df(self):
        # Balanced design, slope random across 72 groups: KR/Satterthwaite df = groups - 1.
        r = fit_multilevel("Weight ~ Time", self.d, "Pig", re_formula="~Time", sampling_se="Weight_se")
        self.assertAlmostEqual(r.diagnostics["df"]["Time"], 71.0, delta=0.05)
        row = r.coefficients.set_index("term").loc["Time"]
        from scipy import stats as sps
        tq = sps.t.ppf(0.95, row.df)
        self.assertAlmostEqual(row.ci_upper - row.estimate, tq * row.se_model, places=8)
        self.assertGreaterEqual(row.se_model, r.diagnostics["se_model_unadjusted"]["Time"])


def _areas(m=40, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.uniform(0, 10, m)
    D = rng.uniform(1, 9, m)
    y = 3 + x + rng.normal(0, 2, m) + rng.normal(0, np.sqrt(D))
    return pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(m)], "y": y, "y_se": np.sqrt(D), "x": x,
                         "county": [f"C{i // 4}" for i in range(m)]})


class TestSpecialCases(unittest.TestCase):
    def test_one_area_per_group_equals_fay_herriot(self):
        f = _areas()
        fh = fay_herriot("y ~ x", f, "y_se")
        ml = fit_multilevel("y ~ x", f, "GEOID", sampling_se=True, area_effect=False)
        self.assertAlmostEqual(ml.diagnostics["variance_components"]["group_cov"]["Intercept"]["Intercept"],
                               fh.sigma2_u, delta=1e-4 * fh.sigma2_u)
        np.testing.assert_allclose(ml.coefficients["estimate"], fh.coefficients["estimate"], rtol=1e-6)
        self.assertEqual(ml.uncertainty_sources[:2], ["model", "sampling_model"])

    def test_spd_inverse(self):
        rng = np.random.default_rng(0)
        V = rng.normal(size=(7, 5, 5))
        V = V @ V.transpose(0, 2, 1) + np.eye(5)
        np.testing.assert_allclose(_spd_inverse(np.linalg.cholesky(V)), np.linalg.inv(V), atol=1e-10)

    def test_validation_errors(self):
        f = _areas()
        with self.assertRaises(ValueError):
            fit_multilevel("y ~ x", f, "county", sampling_se=True, target="these_areas")
        with self.assertRaises(KeyError):
            fit_multilevel("y ~ x", f, "county", sampling_se="nope")
        with self.assertRaises(ValueError):
            fit_multilevel("y ~ x", f, "county", target="bogus")


class TestReplicates(unittest.TestCase):
    def test_gls_replicate_se_matches_analytic_sampling_se(self):
        f = _areas(m=120, seed=3)
        rng = np.random.default_rng(5)
        y = f["y"].to_numpy()[:, None]
        R = make_replicates(y, f[["y_se"]].to_numpy(), rng)
        reps = replicate_long(f["GEOID"], ["y"], y, R)
        r = fit_multilevel("y ~ x", f, "county", sampling_se=True, replicates=reps, target="these_areas")
        se_rep = _coef(r, "x", "se_sampling")
        se_an = r.diagnostics["se_sampling_analytic"]["x"]
        self.assertGreater(se_rep, 0)
        self.assertLess(abs(se_rep / se_an - 1), 0.35)  # 80 replicates: SE of an SE ~ 8%
        row = r.coefficients.set_index("term").loc["x"]
        self.assertAlmostEqual(row.ci_upper - row.estimate, 1.6448536 * se_rep, places=6)
        self.assertIn("sampling_replicate", r.uncertainty_sources)
        self.assertEqual(r.diagnostics["interval_target"], "these_areas")

    def test_gls_refit_is_linear(self):
        f = _areas(m=60, seed=4)
        X = np.column_stack([np.ones(60), f["x"]])
        eng = BlockGLS(f["county"], f["y"].to_numpy(), X, _known_vfun(1, True, 1.0), D=f["y_se"].to_numpy() ** 2)
        th = np.array([0.0, 0.0])
        a, b = f["y"].to_numpy(), np.random.default_rng(0).normal(size=60)
        np.testing.assert_allclose(eng.gls(th, a + b), eng.gls(th, a) + eng.gls(th, b), atol=1e-9)


class TestSimulationSmoke(unittest.TestCase):
    def test_design_studies_registered_without_default_reps(self):
        from census.simulation import FAST, FULL
        for name in DESIGN_STUDIES:
            self.assertEqual(FULL.reps.get(name, 0), 0)
            self.assertEqual(FAST.reps.get(name, 0), 0)
        cfg = designs_config(fast=True)
        self.assertTrue(all(cfg.reps[k] > 0 for k in DESIGN_STUDIES))

    def test_multilevel_coverage_fast(self):
        cfg = designs_config(fast=True)
        df = run_study(cfg, studies=["multilevel_known_variance"])
        cov = float(df.iloc[0]["coverage"])
        self.assertGreater(cov, 0.75)  # 40 reps: wide tolerance; full run in the validation report
        self.assertLess(cov, 1.0)


if __name__ == "__main__":
    unittest.main()
