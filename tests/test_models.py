# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.models (PLAN_ROUND_8 B1-B4) on synthetic data with known answers."""

import json
import unittest

import numpy as np
import pandas as pd
import statsmodels.api as sm

from census.models import effective_trials, fit_replicate, regression_calibration, reliability_screen, simex
from census.results import COEFFICIENT_COLUMNS, ModelResult
from census.variance import REPLICATE_COLUMNS


def _frame_and_reps(n=150, seed=3):
    """Analysis frame (y, x, z) and replicate long frame for y and x."""
    rng = np.random.default_rng(seed)
    x = rng.normal(10, 2, n)
    z = rng.normal(0, 1, n)
    y = 1.0 + 0.5 * x - 0.3 * z + rng.normal(0, 1, n)
    se_x, se_y = rng.uniform(0.2, 0.6, n), rng.uniform(0.3, 0.8, n)
    geo = [f"{i:05d}" for i in range(n)]
    frame = pd.DataFrame({"GEOID": geo, "y": y, "y_se": se_y, "x": x, "x_se": se_x, "z": z})
    frame["y_cv"] = frame["y_se"] / frame["y"].abs() * 100
    frame["x_cv"] = frame["x_se"] / frame["x"].abs() * 100
    parts = []
    for v, est, se in (("y", y, se_y), ("x", x, se_x)):
        # SDR replicates: deviations with variance se^2 under the 4/80 factor
        dev = rng.normal(0, 1, (n, 80)) * se[:, None] * np.sqrt(80 / 4 / 80)
        p = pd.DataFrame(est[:, None] + dev, columns=REPLICATE_COLUMNS)
        p.insert(0, "estimate", est)
        p.insert(0, "variable", v)
        p.insert(0, "GEOID", geo)
        parts.append(p)
    return frame, pd.concat(parts, ignore_index=True)


class TestReplicateFit(unittest.TestCase):
    def test_hand_computed_replicate_se_and_statsmodels_agreement(self):
        frame, reps = _frame_and_reps()
        res = fit_replicate("y ~ x + z", frame, reps)
        self.assertIsInstance(res, ModelResult)
        self.assertEqual(list(res.coefficients.columns[:8]), COEFFICIENT_COLUMNS)
        X = sm.add_constant(frame[["x", "z"]])
        full = sm.OLS(frame["y"], X).fit()
        c = res.coefficients.set_index("term")
        np.testing.assert_allclose(c["estimate"], full.params.to_numpy(), rtol=1e-10)
        np.testing.assert_allclose(c["se_model"], full.bse.to_numpy(), rtol=1e-10)
        # hand formula: refit on each replicate, Var = 4/80 sum (b_r - b)^2
        ry = reps[reps.variable == "y"][REPLICATE_COLUMNS].to_numpy()
        rx = reps[reps.variable == "x"][REPLICATE_COLUMNS].to_numpy()
        b0 = full.params.to_numpy()
        acc = np.zeros(3)
        for r in range(80):
            Xr = np.column_stack([np.ones(len(frame)), rx[:, r], frame["z"]])
            br = np.linalg.lstsq(Xr, ry[:, r], rcond=None)[0]
            acc += (br - b0) ** 2
        np.testing.assert_allclose(c["se_sampling"], np.sqrt(4 / 80 * acc), rtol=1e-8)
        np.testing.assert_allclose(c["se_total"], np.sqrt(c["se_model"] ** 2 + c["se_sampling"] ** 2))
        z90 = 1.6448536269514722
        # Default target "population": intervals from se_model (validated coverage 0.896).
        np.testing.assert_allclose(c["ci_upper"] - c["estimate"], z90 * c["se_model"])
        self.assertEqual(res.diagnostics["interval_target"], "population")
        areas = fit_replicate("y ~ x + z", frame, reps, target="these_areas").coefficients
        np.testing.assert_allclose(areas["ci_upper"] - areas["estimate"], z90 * areas["se_sampling"])
        cons = fit_replicate("y ~ x + z", frame, reps, target="conservative").coefficients
        np.testing.assert_allclose(cons["ci_upper"] - cons["estimate"], z90 * cons["se_total"])
        with self.assertRaises(ValueError):
            fit_replicate("y ~ x + z", frame, target="these_areas")  # needs replicates
        self.assertEqual(res.uncertainty_sources, ["model", "sampling_replicate"])
        self.assertIn("replicate_stability", res.diagnostics)
        self.assertIn("sampling_share_of_residual_variance", res.diagnostics)
        json.dumps(res.to_dict())

    def test_without_replicates_reports_model_only(self):
        frame, _ = _frame_and_reps()
        res = fit_replicate("y ~ x", frame, confidence_level=0.95)
        self.assertEqual(res.uncertainty_sources, ["model"])
        self.assertTrue(res.coefficients["se_sampling"].isna().all())
        self.assertTrue(any("fh_regression" in n for n in res.notes))

    def test_binomial_and_poisson_agree_with_statsmodels(self):
        rng = np.random.default_rng(5)
        n = 120
        x = rng.normal(0, 1, n)
        p = 1 / (1 + np.exp(-(-1 + 0.8 * x)))
        nn = rng.integers(200, 2000, n)
        phat = rng.binomial(nn, p) / nn
        se = np.sqrt(phat * (1 - phat) / nn)
        expo = rng.uniform(1000, 5000, n)
        cnt = rng.poisson(expo * np.exp(-6 + 0.4 * x))
        frame = pd.DataFrame({"GEOID": [str(i) for i in range(n)], "rate": phat, "rate_se": se,
                              "x": x, "cnt": cnt, "pop": expo})
        res = fit_replicate("rate ~ x", frame, model="binomial")
        ref = sm.GLM(phat, sm.add_constant(x), family=sm.families.Binomial(),
                     var_weights=effective_trials(phat, se)).fit()
        np.testing.assert_allclose(res.coefficients["estimate"], ref.params, rtol=1e-6)
        np.testing.assert_allclose(res.coefficients["estimate"], [-1, 0.8], atol=0.15)
        pois = fit_replicate("cnt ~ x", frame, model="poisson", exposure="pop")
        ref = sm.GLM(cnt, sm.add_constant(x), family=sm.families.Poisson(), exposure=expo).fit()
        np.testing.assert_allclose(pois.coefficients["estimate"], ref.params, rtol=1e-6)
        nb = fit_replicate("cnt ~ x", frame, model="negbin", exposure="pop")
        self.assertIn("alpha", list(nb.coefficients["term"]))
        with self.assertRaises(ValueError):
            fit_replicate("cnt ~ x", frame.assign(rate=frame["rate"] * 100), model="binomial")

    def test_reliability_screen_reports_not_drops(self):
        frame, reps = _frame_and_reps()
        frame.loc[:4, "x_cv"] = 50.0
        screen = reliability_screen(frame, "y ~ x + z")
        self.assertEqual(len(screen[screen.variable == "x"]), 5)
        res = fit_replicate("y ~ x + z", frame, reps)
        self.assertEqual(res.n_obs, len(frame))
        self.assertEqual(res.diagnostics["reliability_screen"]["rows_flagged_by_variable"]["x"], 5)
        expected = int(((frame["x_cv"] > 30) | (frame["y_cv"] > 30)).sum())
        self.assertEqual(res.diagnostics["high_cv_influence"]["n_high_cv_rows"], expected)
        self.assertIn("coef_shift_in_se_total_without_high_cv", res.diagnostics["high_cv_influence"])


class TestMeasurementError(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(2026)
        n = 1500
        x = rng.normal(0, 1, n)
        z = rng.normal(0, 1, n) + 0.5 * x
        s = rng.uniform(0.4, 0.9, n)  # known, heteroscedastic sampling SE of x
        w = x + s * rng.normal(0, 1, n)
        y = 1.0 + 2.0 * x + 1.0 * z + rng.normal(0, 0.5, n)
        cls.frame = pd.DataFrame({"y": y, "w": w, "w_se": s, "z": z})
        cls.true = 2.0

    def _bias(self, res, col="estimate"):
        return abs(res.coefficients.set_index("term").loc["w", col] - self.true)

    def test_regression_calibration_removes_attenuation(self):
        res = regression_calibration("y ~ w + z", self.frame, ["w"], n_boot=100, seed=1)
        naive_bias = self._bias(res, "naive_estimate")
        self.assertGreater(naive_bias, 0.4)  # real attenuation
        self.assertLess(self._bias(res), 0.25 * naive_bias)
        self.assertIn("naive", res.diagnostics)
        c = res.coefficients.set_index("term").loc["w"]
        self.assertTrue(c["ci_lower"] < self.true < c["ci_upper"])
        self.assertEqual(res.method, "regression_calibration")
        json.dumps(res.to_dict())

    def test_simex_removes_most_attenuation(self):
        res = simex("y ~ w + z", self.frame, ["w"], n_sim=40, seed=1)
        naive_bias = self._bias(res, "naive_estimate")
        self.assertLess(self._bias(res), 0.5 * naive_bias)
        self.assertTrue(np.isfinite(res.coefficients["se_total"]).all())
        self.assertGreater(res.coefficients.set_index("term").loc["w", "se_total"],
                           res.coefficients.set_index("term").loc["w", "naive_se"])
        json.dumps(res.to_dict())


if __name__ == "__main__":
    unittest.main()
