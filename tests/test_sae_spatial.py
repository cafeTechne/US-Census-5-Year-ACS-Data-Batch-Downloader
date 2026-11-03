# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.sae_spatial: spatial Fay-Herriot vs the published R sae grapes example.

Fixture provenance: tests/fixtures/sae_spatial/SOURCES.txt and the
``data_source`` / ``published_source`` URLs in grapes_reference.json.
"""

import json
import math
import os
import unittest

import numpy as np
import pandas as pd

from census.results import SMALL_AREA_COLUMNS
from census.sae import fay_herriot
from census.sae_spatial import proximity_matrix, spatial_fay_herriot

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sae_spatial")
FORMULA = "grapehect ~ area + workdays - 1"


def _grapes():
    g = pd.read_csv(os.path.join(FIX, "grapes.csv"))
    g.insert(0, "GEOID", [str(i + 1) for i in range(len(g))])
    g["se"] = np.sqrt(g["var"])
    e = pd.read_csv(os.path.join(FIX, "grapesprox_nonzero.csv"))
    W = np.zeros((len(g), len(g)))
    W[e["row"] - 1, e["col"] - 1] = e["value"]
    return g, W


class TestGrapesReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FIX, "grapes_reference.json"), encoding="utf-8") as fh:
            cls.ref = json.load(fh)
        cls.g, cls.W = _grapes()
        cls.fits = {m: spatial_fay_herriot(FORMULA, cls.g, "se", cls.W, method=m) for m in ("REML", "ML")}

    def test_fixture(self):
        self.assertEqual(len(self.g), 274)
        np.testing.assert_allclose(self.W.sum(axis=1), 1.0)
        self.assertTrue(np.all(np.diag(self.W) == 0))

    def test_parameters(self):
        # sae stops Fisher scoring at PRECISION=1e-4, so compare at ~1e-5 relative.
        for m in ("REML", "ML"):
            res, ref = self.fits[m], self.ref[m]
            self.assertAlmostEqual(res.sigma2_u / ref["refvar"], 1.0, delta=2e-6, msg=m)
            self.assertAlmostEqual(res.provenance["rho"], ref["spatialcorr"], delta=5e-6, msg=m)
            coef = res.coefficients.set_index("term")
            for term in ("area", "workdays"):
                self.assertAlmostEqual(coef.loc[term, "estimate"] / ref["estcoef"][term]["beta"], 1, delta=1e-5)
                self.assertAlmostEqual(coef.loc[term, "se_total"] / ref["estcoef"][term]["std.error"], 1, delta=1e-5)
            self.assertAlmostEqual(coef.loc["area", "p_value"] / ref["estcoef"]["area"]["pvalue"], 1, delta=1e-3)
            gd = ref["goodness"]
            self.assertAlmostEqual(res.provenance["loglike"], gd["loglike"], delta=1e-3)
            self.assertAlmostEqual(res.provenance["aic"], gd["AIC"], delta=1e-3)
            self.assertAlmostEqual(res.provenance["bic"], gd["BIC"], delta=1e-3)
            self.assertIn("rho", res.notes[0])

    def test_eblups(self):
        for m in ("REML", "ML"):
            np.testing.assert_allclose(self.fits[m].estimates["eblup"], self.ref[m]["eblup"], rtol=1e-5, err_msg=m)

    def test_analytic_mse_reml(self):
        np.testing.assert_allclose(self.fits["REML"].estimates["mse"], self.ref["REML"]["mse"], rtol=5e-5)

    def test_result_shape(self):
        res = self.fits["REML"]
        self.assertEqual(list(res.estimates.columns), SMALL_AREA_COLUMNS)
        self.assertEqual(res.method, "spatial_fay_herriot_eblup")
        e = res.estimates
        np.testing.assert_allclose(e["ci_upper"] - e["eblup"], 1.6448536269514722 * e["rmse"], rtol=1e-12)
        self.assertTrue(e["gamma"].between(0, 1).all())
        d = res.to_dict()
        self.assertIn("rho", d["provenance"])


def _toy(seed=3, side=6):
    from census.simulation import lattice_weights, sar_field
    rng = np.random.default_rng(seed)
    w = lattice_weights(side)
    m = side * side
    x = rng.uniform(0, 10, m)
    D = rng.uniform(1, 20, m)
    y = 15 + 1.5 * x + sar_field(w.full()[0], 0.5, 3.0, rng) + np.sqrt(D) * rng.standard_normal(m)
    return pd.DataFrame({"GEOID": w.id_order, "y": y, "y_se": np.sqrt(D), "x": x}), w


class TestReductionAndInterface(unittest.TestCase):
    def test_rho_zero_is_fay_herriot(self):
        f, w = _toy()
        for m in ("REML", "ML"):
            a = fay_herriot("y ~ x", f, "y_se", method=m)
            b = spatial_fay_herriot("y ~ x", f, "y_se", w, method=m, fix_rho=0.0)
            self.assertAlmostEqual(b.sigma2_u / a.sigma2_u, 1, delta=1e-6)
            np.testing.assert_allclose(b.estimates["eblup"], a.estimates["eblup"], rtol=1e-7)
            np.testing.assert_allclose(b.estimates["gamma"], a.estimates["gamma"], rtol=1e-6)
            np.testing.assert_allclose(b.coefficients["se_total"], a.coefficients["se_total"], rtol=1e-6)
            # MSE differs only through the information used for g3 (REML vs ML-type
            # 2/sum (A+D)^-2 in census.sae) and the ML bias term: second order.
            np.testing.assert_allclose(b.estimates["mse"], a.estimates["mse"], rtol=0.03)

    def test_weights_inputs_agree(self):
        f, w = _toy()
        dense = proximity_matrix(w, f["GEOID"])
        frame_w = pd.DataFrame(dense, index=f["GEOID"], columns=f["GEOID"])
        a = spatial_fay_herriot("y ~ x", f, "y_se", w)
        b = spatial_fay_herriot("y ~ x", f, "y_se", dense)
        shuffled = f.sample(frac=1, random_state=2)
        c = spatial_fay_herriot("y ~ x", shuffled, "y_se", frame_w)
        np.testing.assert_allclose(a.estimates["eblup"], b.estimates["eblup"], rtol=1e-10)
        merged = c.estimates.set_index("GEOID").loc[a.estimates["GEOID"]]
        np.testing.assert_allclose(merged["eblup"].to_numpy(), a.estimates["eblup"].to_numpy(), rtol=1e-6)
        self.assertGreater(a.provenance["rho"], 0)

    def test_bootstrap_mse_close_to_analytic(self):
        f, w = _toy()
        a = spatial_fay_herriot("y ~ x", f, "y_se", w)
        b = spatial_fay_herriot("y ~ x", f, "y_se", w, mse_method="bootstrap", n_boot=150, seed=1)
        ratio = b.estimates["mse"].mean() / a.estimates["mse"].mean()
        self.assertTrue(0.7 < ratio < 1.4, ratio)
        self.assertIn("bootstrap", b.mse_method)

    def test_errors(self):
        f, w = _toy()
        with self.assertRaises(ValueError):
            spatial_fay_herriot("y ~ x", f, "y_se", w, method="FH")
        with self.assertRaises(ValueError):
            spatial_fay_herriot("y ~ x", f, "y_se", np.eye(len(f)))  # nonzero diagonal
        with self.assertRaises(ValueError):
            spatial_fay_herriot("y ~ x", f, "y_se", w, fix_rho=1.0)
        bad = f.copy()
        bad.loc[0, "y_se"] = np.nan
        with self.assertRaises(ValueError):
            spatial_fay_herriot("y ~ x", bad, "y_se", w)


class TestSpatialFHSimulation(unittest.TestCase):
    def test_fast_coverage(self):
        from census.simulation import run_study, spatial_config
        df = run_study(spatial_config(fast=True), studies=["spatial_fay_herriot"])
        row = df.iloc[0]
        # Fast mode: 40 reps x 49 areas; full mode targets the 88-92% band.
        tol = max(3.5 * row["mc_se"], 0.03)
        self.assertLess(abs(row["coverage"] - 0.90), tol, row["coverage"])
        self.assertLess(abs(row["mse_rel_bias_mean"]), 0.25)
        self.assertGreater(row["rho_mean"], 0.2)
        self.assertLess(row["eblup_mse_vs_fh"], 1.0)  # borrowing from neighbors helps under SAR effects


if __name__ == "__main__":
    unittest.main()
