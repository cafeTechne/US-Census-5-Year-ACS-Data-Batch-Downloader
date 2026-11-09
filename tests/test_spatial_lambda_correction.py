# SPDX-License-Identifier: AGPL-3.0-or-later
"""spatial_regression(model='error', correct_outcome_error=True): lambda corrected for outcome sampling error.

PLAN_ROUND_16 A1 (owner-approved option). The tool path must be the A1
study's estimator: the spatial Fay-Herriot REML (or ML) fit with diagonal
Sigma = diag(SE^2) (census.simulation_spreg.me_sem_fit), whose coverage is in
docs/audits/spatial_regression_validation.md. Checked here on the study's own
seeded draws, plus option handling, warnings, boundary and convergence.
"""

import json
import unittest
from unittest.mock import patch

import numpy as np

try:
    import libpysal  # noqa: F401
    import spreg  # noqa: F401
    HAVE_PYSAL = True
except ImportError:  # pragma: no cover
    HAVE_PYSAL = False


def setUpModule():
    # census.simulation registers (imports) census.simulation_spreg; import it first
    import census.simulation  # noqa: F401


def _a1_draw(kind="rook", lam=0.6, cv_scale=1.0, reps=1, seed=None):
    """The first ``reps`` draws of the A1 'spatial_error_attenuation' cell (same generator order and seed)."""
    from census.simulation import FULL, _geoids, draw_cv, sar_inverse
    from census.simulation_spreg import (SPREG16_CV_SCALES, SPREG16_LAMBDAS, SPREG16_SIDE, SPREG16_WEIGHTS,
                                         lattice_w)
    cfg = FULL
    m = SPREG16_SIDE * SPREG16_SIDE
    g = _geoids(m)
    w = lattice_w(SPREG16_SIDE, kind, g)
    W = w.full()[0]
    if seed is None:
        seed = (cfg.seed + 1600 + 100 * SPREG16_WEIGHTS.index(kind) + 10 * SPREG16_LAMBDAS.index(lam)
                + SPREG16_CV_SCALES.index(cv_scale))
    rng = np.random.default_rng(seed)
    Binv = sar_inverse(W, lam)
    mean_y = 10.0 + cfg.beta * cfg.x_mean
    out = []
    for _ in range(reps):
        x = cfg.x_mean + cfg.x_sd * rng.standard_normal(m)
        ystar = 10.0 + cfg.beta * x + Binv @ (cfg.sigma_u * rng.standard_normal(m))
        s = draw_cv(m, "tract", rng) / 100 * mean_y * cv_scale
        y = ystar + s * rng.standard_normal(m)
        out.append((x, y, s))
    return cfg, seed, g, w, W, out


def _frame(g, x, y, s, outcome="y"):
    from census.simulation import acs_frame
    f = acs_frame(g, {"y": y, "x": x}, {"y": s})
    return f.rename(columns={"y": outcome, "y_se": f"{outcome}_se"})


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestReproducesStudyEstimator(unittest.TestCase):
    def test_equals_me_sem_fit_on_study_draws(self):
        """The tool's lambda, its SE, beta and its SE equal me_sem_fit (diagonal SE^2) on A1's seeded draws."""
        from census.simulation_spreg import me_sem_fit
        from census.spatial import spatial_regression
        for kind, lam in (("rook", 0.6), ("queen", 0.3), ("knn", 0.0)):
            _, _, g, w, W, draws = _a1_draw(kind, lam)
            x, y, s = draws[0]
            for meth in ("REML", "ML"):
                f = me_sem_fit(y, x[:, None], W, np.diag(s ** 2), method=meth)
                res = spatial_regression("y ~ x", _frame(g, x, y, s), w, model="error", correct_outcome_error=True,
                                         se_col="y_se", method=None if meth == "REML" else "ml")
                tab = res.coefficients.set_index("term")
                msg = f"{kind} {meth}"
                self.assertTrue(f["converged"], msg)
                self.assertAlmostEqual(tab.loc["lambda", "estimate"], f["lambda"], places=6, msg=msg)
                self.assertAlmostEqual(tab.loc["lambda", "se_model"], f["se_lambda"], places=6, msg=msg)
                np.testing.assert_allclose(tab.loc[["Intercept", "x"], "estimate"], f["beta"], rtol=1e-7, err_msg=msg)
                np.testing.assert_allclose(tab.loc[["Intercept", "x"], "se_model"], f["se_beta"], rtol=1e-6,
                                           err_msg=msg)
                self.assertAlmostEqual(res.diagnostics["outcome_error_correction"]["sigma2"] / f["A"], 1.0, places=6)
                # the validated interval: Wald, z at the 90% level (the study's lam_cov / b_cov)
                z = 1.6448536269514722
                self.assertAlmostEqual(tab.loc["lambda", "ci_lower"], f["lambda"] - z * f["se_lambda"], places=6)
                self.assertAlmostEqual(tab.loc["x", "ci_upper"], f["beta"][1] + z * f["se_beta"][1], places=6)

    def test_equals_study_cell_row(self):
        """One repetition of the A1 study cell reports the same lambda estimates as the tool on that draw."""
        from census.simulation_spreg import _attenuation_cell
        from census.spatial import spatial_regression
        cfg, seed, g, w, W, draws = _a1_draw("rook", 0.6)
        rows = _attenuation_cell(cfg, 1, seed, "rook", 0.6, 1.0, None)
        lam_rows = {r["estimator"]: r for r in rows if r["term"] == "lambda"}
        x, y, s = draws[0]
        fr = _frame(g, x, y, s)
        reml = spatial_regression("y ~ x", fr, w, model="error", correct_outcome_error=True, se_col="y_se")
        ml = spatial_regression("y ~ x", fr, w, model="error", correct_outcome_error=True, se_col="y_se", method="ml")
        naive = spatial_regression("y ~ x", fr, w, model="error")
        get = lambda r: float(r.coefficients.set_index("term").loc["lambda", "estimate"])  # noqa: E731
        self.assertAlmostEqual(get(reml), lam_rows[next(k for k in lam_rows if "REML" in k)]["mean_estimate"], places=6)
        self.assertAlmostEqual(get(ml), lam_rows[next(k for k in lam_rows if " ML rho" in k)]["mean_estimate"],
                               places=6)
        self.assertAlmostEqual(get(naive), lam_rows["uncorrected ML_Error [tool default]"]["mean_estimate"], places=6)
        self.assertAlmostEqual(reml.diagnostics["outcome_error_correction"]["lambda_uncorrected"], get(naive), places=8)

    def test_small_coverage_check(self):
        """Fast-size coverage smoke check on the study's first draws (rook, lambda 0.6, CV x 1).

        Not an acceptance band: the full study (300 reps) is the evidence. This
        only guards against a tool path that diverges from it.
        """
        from census.spatial import spatial_regression
        reps = 20
        _, _, g, w, W, draws = _a1_draw("rook", 0.6, reps=reps)
        cov, est, naive = [], [], []
        for x, y, s in draws:
            fr = _frame(g, x, y, s)
            tab = spatial_regression("y ~ x", fr, w, model="error", correct_outcome_error=True,
                                     se_col="y_se").coefficients.set_index("term")
            est.append(tab.loc["lambda", "estimate"])
            cov.append(tab.loc["lambda", "ci_lower"] <= 0.6 <= tab.loc["lambda", "ci_upper"])
            naive.append(spatial_regression("y ~ x", fr, w, model="error").coefficients.set_index("term")
                         .loc["lambda", "estimate"])
        self.assertLess(abs(np.mean(est) - 0.6), 0.1)          # study: bias -0.017 (SFH REML)
        self.assertLess(np.mean(naive), np.mean(est) - 0.1)    # study: naive bias -0.274
        self.assertGreaterEqual(np.mean(cov), 0.7)             # study: 0.863 (300 reps)


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestOption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, _, cls.g, cls.w, cls.W, draws = _a1_draw("queen", 0.3)
        cls.x, cls.y, cls.s = draws[0]

    def fit(self, outcome="y", **kw):
        from census.spatial import spatial_regression
        kw.setdefault("se_col", f"{outcome}_se")
        return spatial_regression(f"{outcome} ~ x", _frame(self.g, self.x, self.y, self.s, outcome), self.w,
                                  model="error", correct_outcome_error=True, **kw)

    def test_result_contract(self):
        res = self.fit()
        d = res.to_dict()
        json.dumps(d)
        self.assertEqual(res.model_type, "spatial_error")
        self.assertEqual(list(res.coefficients["term"]), ["Intercept", "x", "lambda"])
        self.assertEqual(res.uncertainty_sources, ["model", "sampling_model"])
        self.assertIn("REML", res.method)
        self.assertEqual(d["interval"]["basis_column"], "se_model")
        self.assertEqual(d["interval"]["undefined_terms"], [])
        diag = res.diagnostics
        self.assertTrue(diag["converged"])
        self.assertFalse(diag["boundary"])
        self.assertFalse(diag["controlled_total_warning"])
        self.assertIn("lm_tests", diag)
        oc = diag["outcome_error_correction"]
        self.assertEqual(oc["variance_method"], "REML")
        self.assertGreater(oc["lambda"], oc["lambda_uncorrected"])  # attenuation removed
        text = " ".join(res.notes)
        self.assertIn("independent across areas", text)
        self.assertIn("known (= SE^2", text)
        self.assertIn("acs_sampling_covariance.md", text)
        self.assertIn("spatial_regression_validation.md", text)
        self.assertIn("diffusion of shocks", text)
        self.assertIn("not a spillover", text)
        self.assertIn("positively correlated sampling errors", text)
        self.assertIn("+0.19 to +0.22", text)
        self.assertNotIn("WARNING", text)
        self.assertTrue(res.provenance["outcome_error_correction"])

    def test_default_unchanged(self):
        from census import spatial
        fr = _frame(self.g, self.x, self.y, self.s)
        a = spatial.spatial_regression("y ~ x", fr, self.w, model="error")
        b = spatial.spatial_regression("y ~ x", fr, self.w, model="error", correct_outcome_error=False)
        self.assertEqual(a.method, "spreg ML spatial error")
        self.assertEqual(a.to_dict()["coefficients"], b.to_dict()["coefficients"])
        self.assertIn(spatial.ERROR_CAVEAT, a.notes)
        self.assertIn("correct_outcome_error=True", spatial.ERROR_CAVEAT)
        self.assertIn("understates the true lambda", spatial.ERROR_CAVEAT)
        self.assertNotIn("outcome_error_correction", a.diagnostics)

    def test_invalid_options(self):
        from census.spatial import spatial_regression
        fr = _frame(self.g, self.x, self.y, self.s)
        with self.assertRaisesRegex(ValueError, "needs se_col"):
            self.fit(se_col=None)
        with self.assertRaisesRegex(ValueError, "model='error' only"):
            spatial_regression("y ~ x", fr, self.w, model="lag", correct_outcome_error=True, se_col="y_se")
        with self.assertRaisesRegex(ValueError, "method must be one of"):
            self.fit(method="gm")
        with self.assertRaisesRegex(ValueError, "only with correct_outcome_error"):
            spatial_regression("y ~ x", fr, self.w, model="error", se_col="y_se")
        with self.assertRaisesRegex(ValueError, "target='population'"):
            self.fit(target="conservative")
        with self.assertRaisesRegex(KeyError, "nope"):
            self.fit(se_col="nope")
        with self.assertRaisesRegex(ValueError, "plain outcome column"):
            spatial_regression("np.log(y) ~ x", fr, self.w, model="error", correct_outcome_error=True, se_col="y_se")
        bad = fr.copy()
        bad.loc[3, "y_se"] = -1.0
        with self.assertRaisesRegex(ValueError, "finite and >= 0"):
            spatial_regression("y ~ x", bad, self.w, model="error", correct_outcome_error=True, se_col="y_se")

    def test_controlled_total_warning(self):
        from census.spatial import controlled_total_reason
        for name in ("B01003_001E", "B01003_001", "B11001_001E", "B25003_001", "population", "Households"):
            self.assertIsNotNone(controlled_total_reason(name), name)
        for name in ("poverty_rate", "B25003_002E", "owner_share", "median_hh_income", "y"):
            self.assertIsNone(controlled_total_reason(name), name)
        res = self.fit(outcome="B01003_001E")
        self.assertTrue(res.diagnostics["controlled_total_warning"])
        self.assertTrue(res.notes[0].startswith("WARNING: the outcome looks like a controlled total"))
        self.assertIn("B01003_001", res.diagnostics["outcome_error_correction"]["controlled_total_reason"])
        flagged = self.fit(controlled_total=True)
        self.assertTrue(flagged.diagnostics["controlled_total_warning"])
        self.assertIn("flagged by the caller", flagged.notes[0])
        off = self.fit(outcome="B01003_001E", controlled_total=False)
        self.assertFalse(off.diagnostics["controlled_total_warning"])
        # the estimate itself does not depend on the warning
        self.assertEqual(off.to_dict()["coefficients"], res.to_dict()["coefficients"])

    def test_non_convergence_is_an_error(self):
        from census import sae_spatial
        real = sae_spatial._fit

        def unconverged(*a, **k):
            out = real(*a, **k)
            out["converged"] = False
            return out
        with patch.object(sae_spatial, "_fit", unconverged):
            with self.assertRaisesRegex(RuntimeError, "did not converge.*not substituted"):
                self.fit()

    def test_boundary_flag(self):
        # sampling variances far above the outcome's variation: sigma^2 -> 0, lambda not identified
        from census.spatial import spatial_regression
        fr = _frame(self.g, self.x, self.y, self.s * 40)
        res = spatial_regression("y ~ x", fr, self.w, model="error", correct_outcome_error=True, se_col="y_se")
        self.assertTrue(res.diagnostics["boundary"])
        self.assertTrue(any("sigma^2 estimated at 0" in r
                            for r in res.diagnostics["outcome_error_correction"]["boundary_reasons"]))
        self.assertIn("lambda", res.to_dict()["interval"]["undefined_terms"])
        self.assertTrue(any(n.startswith("boundary fit:") for n in res.notes))


if __name__ == "__main__":
    unittest.main()
