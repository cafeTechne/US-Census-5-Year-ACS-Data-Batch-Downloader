# SPDX-License-Identifier: AGPL-3.0-or-later
"""PLAN_ROUND_16 workstream A: lag-model effects (A2) and the study estimators (A1, A4).

Effects are checked against an independent computation from
S(W) = (I - rho W)^-1 beta (direct = mean diagonal, total = mean row sum,
indirect = total - direct) and against spreg's own multipliers; the study
likelihoods against spreg's ML_Error / ML_Lag(slx_lags=1) and the
measurement-error SEM against census.sae_spatial (spatial Fay-Herriot, ML).
"""

import unittest
import warnings

import numpy as np
import pandas as pd

try:
    import libpysal
    import spreg
    HAVE_PYSAL = True
except ImportError:  # pragma: no cover
    HAVE_PYSAL = False


def _lag_data(seed=2, side=8, rho=0.5, island=False):
    from census.simulation import lattice_weights, sar_inverse
    rng = np.random.default_rng(seed)
    w = lattice_weights(side)
    if island:
        nb = dict(w.neighbors)
        nb["island"] = []
        w = libpysal.weights.W(nb, id_order=list(w.id_order) + ["island"], silence_warnings=True)
        w.transform = "r"
    n = w.n
    W = w.full()[0]
    x, z = rng.normal(size=n), rng.normal(size=n)
    y = sar_inverse(W, rho) @ (1 + 2 * x - z + rng.normal(size=n))
    return w, W, pd.DataFrame({"GEOID": w.id_order, "y": y, "x": x, "z": z})


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestLagEffects(unittest.TestCase):
    def _independent(self, res, W):
        tab = res.coefficients.set_index("term")["estimate"]
        n = W.shape[0]
        S = np.linalg.inv(np.eye(n) - tab["rho"] * W)
        out = {}
        for v in ("x", "z"):
            direct, total = tab[v] * np.trace(S) / n, tab[v] * S.sum() / n
            out[v] = {"direct": direct, "total": total, "indirect": total - direct}
        return out

    def test_effects_match_independent_computation(self):
        from census.spatial import spatial_regression
        for island in (False, True):
            w, W, df = _lag_data(island=island)
            for method in ("gm", "ml"):
                res = spatial_regression("y ~ x + z", df, w, model="lag", method=method)
                ref = self._independent(res, W)
                eff = res.effects.set_index(["variable", "effect"])["estimate"]
                for v in ("x", "z"):
                    for e in ("direct", "indirect", "total"):
                        self.assertAlmostEqual(eff[(v, e)], ref[v][e], places=10, msg=f"{method} {v} {e} {island}")

    def test_effects_match_spreg_multipliers(self):
        from census.spatial import spatial_regression
        w, W, df = _lag_data()
        res = spatial_regression("y ~ x + z", df, w, model="lag", method="ml")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = spreg.ML_Lag(df[["y"]].to_numpy(), df[["x", "z"]].to_numpy(), w, spat_impacts="full")
        adi, aii, ati = r.sp_multipliers["full"]
        beta = res.coefficients.set_index("term")["estimate"]
        eff = res.effects.set_index(["variable", "effect"])["estimate"]
        for v in ("x", "z"):
            self.assertAlmostEqual(eff[(v, "direct")], beta[v] * float(adi), places=6)
            self.assertAlmostEqual(eff[(v, "indirect")], beta[v] * float(aii), places=6)
            self.assertAlmostEqual(eff[(v, "total")], beta[v] * float(ati), places=6)

    def test_delta_method_se(self):
        # (1) the analytic gradient equals a numerical gradient of the effects (exact check of the
        # delta method); (2) where rho is well determined (n = 400) the delta-method SEs agree with
        # Monte Carlo draws of (beta, rho) from N(estimate, vm). With an imprecise rho the effects
        # are visibly nonlinear in rho (1 / (1 - rho)) and the two differ; coverage of the
        # delta-method intervals is measured by the 'spatial_dgp_recovery' study.
        from census.spatial import _lag_effect_values, _silence_spreg_output, lag_effects, weights_eigenvalues
        _silence_spreg_output()
        for side, check_mc in ((8, False), (20, True)):
            w, W, df = _lag_data(side=side, rho=0.4)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = spreg.GM_Lag(df[["y"]].to_numpy(), df[["x", "z"]].to_numpy(), w=w)
            params, vm = np.asarray(r.betas, float).ravel(), np.asarray(r.vm, float)
            eig = weights_eigenvalues(w)
            out = lag_effects(params, vm, ["x", "z"], eig)
            h = 1e-6
            J = np.column_stack([(_lag_effect_values(params + h * e, eig, 2, np.asarray(1.0))
                                  - _lag_effect_values(params - h * e, eig, 2, np.asarray(1.0))) / (2 * h)
                                 for e in np.eye(len(params))])
            np.testing.assert_allclose(out["se"], np.sqrt(np.diag(J @ vm @ J.T)), rtol=1e-6)
            if check_mc:
                draws = np.random.default_rng(0).multivariate_normal(params, vm, size=20000)
                sim = np.vstack([_lag_effect_values(d, eig, 2, np.asarray(1.0)) for d in draws])
                np.testing.assert_allclose(out["se"], sim.std(axis=0), rtol=0.05)

    def test_effects_first_and_labels(self):
        from census.spatial import LAG_EFFECTS_NOTE, spatial_regression
        w, W, df = _lag_data()
        res = spatial_regression("y ~ x + z", df, w)
        d = res.to_dict()
        keys = list(d)
        self.assertLess(keys.index("effects"), keys.index("coefficients"))
        self.assertEqual(res.notes[0], LAG_EFFECTS_NOTE)
        self.assertIn("simulation 'spatial_dgp_recovery'", res.notes[1])
        self.assertIn("not a specification test", " ".join(res.notes))
        self.assertTrue(res.summary().startswith("EFFECTS FIRST"))
        interp = res.coefficients.set_index("term")["interpretation"]
        self.assertIn("NOT the marginal effect", interp["x"])
        self.assertIn("not a marginal effect", res.provenance["coefficient_meaning"])
        self.assertEqual(list(res.effects.columns[:3]), ["term", "variable", "effect"])
        self.assertTrue(np.isfinite(res.effects["se_model"]).all())
        self.assertTrue((res.effects["ci_lower"] < res.effects["ci_upper"]).all())
        cv = res.diagnostics["coefficient_vcov"]
        self.assertEqual(cv["terms"], ["Intercept", "x", "z", "rho"])
        np.testing.assert_allclose(np.sqrt(np.diag(cv["matrix"])), res.coefficients["se_model"])
        err = spatial_regression("y ~ x + z", df, w, model="error")
        self.assertIsNone(err.effects)
        self.assertNotIn("effects", err.to_dict())
        self.assertNotIn("interpretation", err.coefficients.columns)

    def test_replicate_effects_and_size_limit(self):
        from census import spatial
        from census.simulation import make_replicates, replicate_long
        w, W, df = _lag_data(side=7)
        rng = np.random.default_rng(1)
        se = np.full(w.n, 0.3)
        reps = replicate_long(w.id_order, ["y"], df[["y"]].to_numpy(), make_replicates(df[["y"]].to_numpy(),
                                                                                        se[:, None], rng))
        res = spatial.spatial_regression("y ~ x + z", df, w, replicates=reps)
        self.assertTrue(np.isfinite(res.effects["se_sampling"]).all())
        self.assertTrue((res.effects["se_sampling"] > 0).all())
        old = spatial.EFFECTS_MAX_N
        try:
            spatial.EFFECTS_MAX_N = 10
            res = spatial.spatial_regression("y ~ x + z", df, w)
        finally:
            spatial.EFFECTS_MAX_N = old
        self.assertIsNone(res.effects)
        self.assertIn("Effects were NOT computed", res.notes[0])

    def test_multipliers_outside_unit_interval(self):
        from census.spatial import lag_multipliers
        self.assertTrue(np.isnan(lag_multipliers(np.array([1.0, -0.5]), 1.0)["direct"]))
        m = lag_multipliers(np.array([1.0, 0.0, -1.0]), 0.0)
        self.assertEqual((m["direct"], m["total"], m["indirect"]), (1.0, 1.0, 0.0))

    def test_effects_table(self):
        from census.report_tables import effects_table, regression_table
        from census.spatial import spatial_regression
        w, W, df = _lag_data()
        res = spatial_regression("y ~ x + z", df, w)
        t = effects_table(res)
        self.assertEqual(list(t.display.columns), ["Variable", "Direct", "Indirect", "Total"])
        self.assertEqual(t.display["Variable"].tolist(), ["x", "", "z", ""])
        self.assertIn("not marginal effects", t.notes[0])
        self.assertIn("[", effects_table(res, uncertainty="ci").display.iloc[1, 1])
        self.assertIn("not marginal effects", " ".join(regression_table({"lag": res}).notes))
        with self.assertRaises(ValueError):
            effects_table(spatial_regression("y ~ x + z", df, w, model="error"))


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestStudyEstimators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from census.simulation import sar_inverse
        from census.simulation_spreg import lattice_w
        cls.data = {}
        rng = np.random.default_rng(7)
        for kind in ("rook", "queen", "knn"):
            w = lattice_w(10, kind)
            W = w.full()[0]
            x = 50 + 24 * rng.standard_normal(w.n)
            y = 10 + 0.5 * x + sar_inverse(W, 0.5) @ (8 * rng.standard_normal(w.n)) + 4 * rng.standard_normal(w.n)
            cls.data[kind] = (w, W, np.linalg.eigvals(W), x, y)

    def test_lattice_weights(self):
        from census.simulation_spreg import lattice_w
        card = {k: np.mean(list(lattice_w(10, k).cardinalities.values())) for k in ("rook", "queen", "knn")}
        self.assertAlmostEqual(card["rook"], 3.6)
        self.assertAlmostEqual(card["queen"], (4 * 3 + 32 * 5 + 64 * 8) / 100)
        self.assertEqual(card["knn"], 6)
        np.testing.assert_array_equal(lattice_w(10, "knn").full()[0], lattice_w(10, "knn").full()[0])

    def test_likelihoods_match_spreg(self):
        from census.simulation_spreg import sdm_ml, sem_ml
        for kind, (w, W, eig, x, y) in self.data.items():
            X = np.column_stack([np.ones(w.n), x])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                e = spreg.ML_Error(y[:, None], x[:, None], w)
                lag = spreg.ML_Lag(y[:, None], x[:, None], w, slx_lags=1)
            sem, sdm = sem_ml(y, X, W, eig), sdm_ml(y, X, W, eig)
            self.assertAlmostEqual(sem["loglik"], float(np.ravel(e.logll)[0]), places=5, msg=kind)
            self.assertAlmostEqual(sdm["loglik"], float(lag.logll), places=5, msg=kind)
            self.assertAlmostEqual(sdm["rho"], float(lag.rho), places=5, msg=kind)
            np.testing.assert_allclose(sdm["coef"], np.ravel(lag.betas)[:-1], rtol=1e-4)

    def test_common_factor_nesting(self):
        # SEM = SDM with rho = lambda and theta = -lambda beta: the SDM likelihood at the
        # restricted SEM estimates equals the SEM likelihood (same data, same Jacobian), so LR >= 0
        from census.simulation_spreg import _logdet_terms, common_factor_lr
        for kind, (w, W, eig, x, y) in self.data.items():
            X = np.column_stack([np.ones(w.n), x])
            lr, sdm, sem = common_factor_lr(y, X, W, eig)
            lam, (a, b) = sem["lambda"], sem["beta"]
            e = y - lam * W @ y - (a * (1 - lam) + b * x - lam * b * (W @ x))
            n = w.n
            s2 = float(e @ e) / n
            ll = -0.5 * n * (np.log(2 * np.pi * s2) + 1) + _logdet_terms(eig, lam)
            self.assertAlmostEqual(ll, sem["loglik"], places=6, msg=kind)
            self.assertGreaterEqual(sdm["loglik"], sem["loglik"] - 1e-9)
            self.assertGreaterEqual(lr, 0.0)

    def test_me_sem_matches_spatial_fay_herriot(self):
        from census import sae_spatial
        from census.simulation_spreg import me_sem_fit
        w, W, eig, x, y = self.data["rook"]
        D = np.random.default_rng(3).uniform(5, 30, w.n)
        f = me_sem_fit(y, x[:, None], W, np.diag(D), method="ML")
        g = sae_spatial._fit(y, np.column_stack([np.ones(w.n), x]), D, W, "ML")
        self.assertTrue(f["converged"])
        self.assertAlmostEqual(f["lambda"], g["rho"], places=4)
        self.assertAlmostEqual(f["A"] / g["A"], 1.0, places=3)
        np.testing.assert_allclose(f["beta"], g["beta"], rtol=1e-4)
        self.assertTrue(np.isfinite(f["se_lambda"]))

    def test_me_sem_reml_matches_spatial_fay_herriot(self):
        # REML is spatial_fay_herriot's default; rho_se there uses the same REML information
        from census import sae_spatial
        from census.simulation_spreg import me_sem_fit
        w, W, eig, x, y = self.data["queen"]
        D = np.random.default_rng(4).uniform(5, 30, w.n)
        f = me_sem_fit(y, x[:, None], W, np.diag(D))
        frame = pd.DataFrame({"GEOID": w.id_order, "y": y, "x": x, "y_se": np.sqrt(D)})
        res = sae_spatial.spatial_fay_herriot("y ~ x", frame, "y_se", W)
        self.assertAlmostEqual(f["lambda"], res.provenance["rho"], places=4)
        self.assertAlmostEqual(f["se_lambda"], res.provenance["rho_se"], places=4)
        self.assertAlmostEqual(f["A"] / res.sigma2_u, 1.0, places=3)

    def test_residual_bootstrap(self):
        from census.simulation_spreg import common_factor_lr, lr_residual_bootstrap
        w, W, eig, x, y = self.data["rook"]
        X = np.column_stack([np.ones(w.n), x])
        lr, _, sem = common_factor_lr(y, X, W, eig)
        a = lr_residual_bootstrap(y, X, W, eig, lr, sem, np.random.default_rng(0), n_boot=20)
        b = lr_residual_bootstrap(y, X, W, eig, lr, sem, np.random.default_rng(0), n_boot=20)
        self.assertEqual(a, b)
        self.assertFalse(lr_residual_bootstrap(y, X, W, eig, 0.0, sem, np.random.default_rng(1), n_boot=20))

    def test_me_sem_without_error_is_sem(self):
        from census.simulation_spreg import me_sem_fit, sem_ml
        w, W, eig, x, y = self.data["queen"]
        f = me_sem_fit(y, x[:, None], W, np.zeros((w.n, w.n)), method="ML")
        s = sem_ml(y, np.column_stack([np.ones(w.n), x]), W, eig)
        self.assertAlmostEqual(f["lambda"], s["lambda"], places=4)
        self.assertAlmostEqual(f["loglik"], s["loglik"], places=4)

    def test_true_effects(self):
        from census.simulation_spreg import true_effects
        from census.spatial import lag_multipliers
        w, W, eig, x, y = self.data["rook"]
        self.assertEqual(true_effects(W, 0.0, 0.5), {"direct": 0.5, "indirect": 0.0, "total": 0.5})
        m = lag_multipliers(eig, 0.6)
        t = true_effects(W, 0.6, 0.5)
        self.assertAlmostEqual(t["direct"], 0.5 * m["direct"], places=10)
        self.assertAlmostEqual(t["total"], 0.5 * m["total"], places=10)
        sdm = true_effects(W, 0.4, 0.5, 0.3)
        self.assertAlmostEqual(sdm["total"], 0.8 / 0.6, places=10)
        # SEM written as SDM (theta = -rho beta): total effect beta
        self.assertAlmostEqual(true_effects(W, 0.4, 0.5, -0.2)["total"], 0.5, places=10)

    def test_rs_additivity(self):
        # Koley & Bera (2024): RS_rho,gamma = RS_rho + RS*_gamma = RS*_rho + RS_gamma (in spreg 1.9.1 the
        # robust tests are defined by these differences, so this checks the wiring, not the theory)
        from census.spatial import _silence_spreg_output
        _silence_spreg_output()
        w, W, eig, x, y = self.data["rook"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            lm = spreg.LMtests(spreg.OLS(y[:, None], x[:, None]), w,
                               tests=["lml", "lmwx", "lmspdurbin", "rlmwx", "rlmdurlag"])
        joint = lm.lmspdurbin[0]
        self.assertAlmostEqual(joint, lm.lml[0] + lm.rlmwx[0], places=8)
        self.assertAlmostEqual(joint, lm.rlmdurlag[0] + lm.lmwx[0], places=8)

    def test_sar_correlation(self):
        from census.simulation_spreg import neighbour_correlation, sar_correlation
        w, W, *_ = self.data["rook"]
        R = sar_correlation(W, 0.6)
        np.testing.assert_allclose(np.diag(R), 1.0)
        self.assertTrue(np.all(np.linalg.eigvalsh(R) > 0))
        self.assertGreater(neighbour_correlation(R, W), neighbour_correlation(sar_correlation(W, 0.3), W))


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestStudies(unittest.TestCase):
    def test_registered_on_demand_only(self):
        from census.simulation import FULL, STUDIES, group_config, spatial_config
        from census.simulation_spreg import SPREG16_REPS_FULL, SPREG_VALIDATION_STUDIES
        for name in SPREG_VALIDATION_STUDIES:
            self.assertIn(name, STUDIES)
            self.assertEqual(FULL.reps.get(name, 0), 0)
            self.assertEqual(spatial_config(fast=False).reps.get(name, 0), 0)
        self.assertEqual({k: v for k, v in group_config("spreg_validation", False).reps.items() if v},
                         SPREG16_REPS_FULL)

    def test_fast_studies_info_and_order_independent(self):
        from census.simulation import run_study
        from census.simulation_spreg import spreg_validation_config
        cfg = spreg_validation_config(fast=True)
        cfg.reps.update({"spatial_dgp_recovery": 1, "common_factor_lr_acs": 1})
        names = ["common_factor_lr_acs", "spatial_dgp_recovery"]
        a = run_study(cfg, studies=names)
        b = run_study(cfg, studies=names[::-1])
        self.assertTrue((a["status"] == "info").all())
        cols = ["method", "target", "coverage", "bias"]
        pd.testing.assert_frame_equal(
            a[a["study"] == "spatial_dgp_recovery"][cols].reset_index(drop=True),
            b[b["study"] == "spatial_dgp_recovery"][cols].reset_index(drop=True))
        lag = a[(a["study"] == "spatial_dgp_recovery") & (a["model"] == "lag")]
        self.assertTrue(set(lag["term"]) >= {"x (direct)", "x (indirect)", "x (total)", "rho", "x"})
        self.assertEqual(set(a[a["study"] == "common_factor_lr_acs"]["critical"]),
                         {"chi2", "bootstrap_residual", "bootstrap_me"})
        self.assertIn("juhl", set(a["dgp"].dropna()))


if __name__ == "__main__":
    unittest.main()
