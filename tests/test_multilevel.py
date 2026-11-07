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


class TestKnownVarianceDenseReference(unittest.TestCase):
    """Independent dense-matrix reference for the known-variance GLS algebra (PLAN_ROUND_12 A1).

    Scope: this validates the FIXED-VARIANCE algebra layer only -- the
    BlockGLS solve, the log-Cholesky/scale parametrization of _known_vfun,
    size-class batching of unequal blocks and the analytic sampling
    sandwich -- at variance components chosen here and held fixed. It does
    NOT validate REML estimation of the components or the Kenward-Roger
    small-sample adjustment.

    Model: y = X beta + Z b + u + e with b_j ~ N(0, G) (random intercept and
    slope), u ~ N(0, s2 I) (area effect), e ~ N(0, diag(D)) known,
    heterogeneous D. The expected answer assembles the dense n x n matrix
    V = Z_blockdiag G Z' + s2 I + diag(D) directly and uses textbook GLS:
    beta = (X'V^-1X)^-1 X'V^-1 y, cov(beta) = (X'V^-1X)^-1, and the
    sampling-only part Q X'V^-1 diag(D) V^-1 X Q. Nothing from
    census.multilevel is used to build the expected values.
    """

    # Groups deliberately interleaved and of unequal size (2, 3, 4) so row
    # ordering and multiple size classes are exercised.
    groups = np.array(["a", "b", "c", "a", "b", "c", "c", "b", "c"])
    x = np.array([1.0, 2.0, 0.5, 3.0, 4.0, 1.5, 2.5, 5.0, 4.0])
    y = np.array([2.1, 3.9, 1.0, 5.2, 6.8, 2.9, 4.4, 8.1, 6.0])
    se = np.array([0.3, 1.2, 0.6, 0.9, 0.4, 1.5, 0.7, 0.5, 1.1])  # heterogeneous known SEs
    G = np.array([[0.8, -0.25], [-0.25, 0.15]])  # nonzero intercept-slope covariance
    s2 = 0.35
    c = 2.5  # production variance scale; the parametrization below must absorb it

    def _dense_reference(self):
        n = len(self.y)
        X = np.column_stack([np.ones(n), self.x])
        Z = X  # random intercept and slope on x
        D = self.se ** 2
        V = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                if self.groups[i] == self.groups[j]:
                    V[i, j] = Z[i] @ self.G @ Z[j]
        V += np.diag(self.s2 + D)
        Vi = np.linalg.inv(V)
        Q = np.linalg.inv(X.T @ Vi @ X)
        beta = Q @ X.T @ Vi @ self.y
        S = Q @ X.T @ Vi @ np.diag(D) @ Vi @ X @ Q
        return X, Z, D, V, beta, Q, S

    def _theta(self):
        # Invert the documented parametrization: G = c L L' with L lower
        # triangular (log diagonal, raw off-diagonal); s2 = c exp(2 theta_last).
        L = np.linalg.cholesky(self.G / self.c)
        return np.array([np.log(L[0, 0]), L[1, 0], np.log(L[1, 1]), 0.5 * np.log(self.s2 / self.c)])

    def test_fixed_component_gls_matches_dense_matrices(self):
        X, Z, D, V, beta, Q, S = self._dense_reference()
        eng = BlockGLS(self.groups, self.y, X, _known_vfun(2, True, self.c), D=D, Z=Z)
        sol = eng._solve(self._theta(), keep=True)
        # Tolerance: cond(V) and cond(X'V^-1X) are asserted < 1e3 below, so
        # both computations agree to ~cond * 1e-16 = 1e-13 relative; rtol
        # 1e-9 leaves four orders of margin yet is far below the 0.13-0.32
        # relative changes the mistakes in the next test produce.
        self.assertLess(np.linalg.cond(V), 1e3)
        self.assertLess(np.linalg.cond(np.linalg.inv(Q)), 1e3)
        np.testing.assert_allclose(sol["beta"], beta, rtol=1e-9, atol=1e-12)
        np.testing.assert_allclose(sol["Q"], Q, rtol=1e-9, atol=1e-12)
        np.testing.assert_allclose(eng.sandwich_sampling_cov(sol["vinv"], sol["Q"]), S, rtol=1e-9, atol=1e-12)
        # Replicate-refit path (V held fixed, new y) agrees too.
        y2 = self.y[::-1].copy()
        np.testing.assert_allclose(eng.gls(self._theta(), y2, vinv=sol["vinv"]),
                                   Q @ X.T @ np.linalg.inv(V) @ y2, rtol=1e-9, atol=1e-12)

    def test_reference_is_sensitive_to_the_errors_it_guards(self):
        """The test above would fail if known variance, within-block covariance or heterogeneity were mishandled."""
        X, Z, D, V, beta, Q, _ = self._dense_reference()

        def gls(Vx):
            Vi = np.linalg.inv(Vx)
            Qx = np.linalg.inv(X.T @ Vi @ X)
            return Qx @ X.T @ Vi @ self.y, Qx

        within = V - np.diag(np.diag(V))
        wrong = {
            "known variance omitted": V - np.diag(D),
            "within-block covariance omitted": V - within,
            "homogeneous (mean) D": V - np.diag(D) + D.mean() * np.eye(len(D)),
            "intercept-slope covariance dropped": V - self._cross_term(Z),
        }
        for label, Vw in wrong.items():
            bw, Qw = gls(Vw)
            gap = max(np.max(np.abs(bw - beta) / np.abs(beta)), np.max(np.abs(Qw - Q) / np.abs(Q)))
            # Measured gaps 0.14 (within-block) to 0.32 (D omitted); 1e-2 is
            # still seven orders above the rtol used above.
            self.assertGreater(gap, 1e-2, label)

    def _cross_term(self, Z):
        """Contribution of G[0, 1] to V: G01 (z0_i z1_j + z1_i z0_j) within each block."""
        n = len(self.y)
        M = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                if self.groups[i] == self.groups[j]:
                    M[i, j] = self.G[0, 1] * (Z[i, 0] * Z[j, 1] + Z[i, 1] * Z[j, 0])
        return M
