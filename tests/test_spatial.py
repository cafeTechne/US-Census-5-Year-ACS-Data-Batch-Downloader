# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.spatial: weights, Moran's I (with replicate SE), LISA, spatial regression.

Reference values are PySAL's published examples (tests/fixtures/spatial/
pysal_reference.json, with source URLs) on libpysal's bundled example data.
"""

import json
import math
import os
import tempfile
import unittest

import numpy as np
import pandas as pd

try:
    import geopandas as gpd
    import libpysal
    from libpysal import examples
    from shapely.geometry import box
    HAVE_PYSAL = True
except ImportError:  # pragma: no cover
    HAVE_PYSAL = False

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "spatial", "pysal_reference.json")


def _read_gal(name):
    return libpysal.io.open(examples.get_path(name)).read()


def _dbf_frame(name, cols, w):
    db = libpysal.io.open(examples.get_path(name))
    df = pd.DataFrame({c: np.asarray(db.by_col(c), float) for c in cols})
    df.insert(0, "GEOID", [str(i) for i in w.id_order])  # GAL order = file row order (as in esda/spreg)
    return df


def _grid(n=3, island=True, crs="EPSG:5070"):
    geoms = [box(c, r, c + 1, r + 1) for r in range(n) for c in range(n)]
    ids = [f"g{i}" for i in range(len(geoms))]
    if island:
        geoms.append(box(10, 10, 11, 11))
        ids.append("island")
    return gpd.GeoDataFrame({"GEOID": ids}, geometry=geoms, crs=crs)


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestWeights(unittest.TestCase):
    def test_contiguity_counts_and_islands(self):
        from census.spatial import spatial_weights
        g = _grid()
        q = spatial_weights(g, "queen")
        r = spatial_weights(g, "rook")
        self.assertEqual(q.cardinalities["g4"], 8)
        self.assertEqual(r.cardinalities["g4"], 4)
        self.assertEqual(q.census_report["islands"], ["island"])
        self.assertEqual(q.census_report["n_islands"], 1)
        self.assertEqual(q.transform, "R")
        self.assertAlmostEqual(sum(q.weights["g4"]), 1.0)

    def test_knn_distance_and_projection_guard(self):
        from census.spatial import spatial_weights
        g = _grid(island=False)
        k = spatial_weights(g, "knn", k=2)
        self.assertTrue(all(c == 2 for c in k.cardinalities.values()))
        d = spatial_weights(g, "distance", threshold=1.01)
        self.assertEqual(d.cardinalities["g4"], 4)  # centroid distance 1 only to rook neighbors
        with self.assertRaises(ValueError):
            spatial_weights(g.to_crs(4326), "knn", k=2)
        with self.assertRaises(ValueError):
            spatial_weights(g, "distance")

    def test_cache_memory_and_disk(self):
        from census.spatial import spatial_weights
        g = _grid()
        a = spatial_weights(g, "queen", cache=True)
        b = spatial_weights(g, "queen", cache=True)
        self.assertEqual(b.census_report["cache"], "memory_hit")
        with tempfile.TemporaryDirectory() as tmp:
            c = spatial_weights(g, "rook", cache=tmp)
            d = spatial_weights(g, "rook", cache=tmp)
            self.assertEqual(c.census_report["cache"], "disk_stored")
            self.assertEqual(d.census_report["cache"], "disk_hit")
            self.assertEqual({k: sorted(v) for k, v in c.neighbors.items()},
                             {k: sorted(v) for k, v in d.neighbors.items()})
            self.assertEqual(d.id_order, list(g["GEOID"]))
        self.assertEqual(a.neighbors, b.neighbors)

    def test_rejects_duplicates(self):
        from census.spatial import spatial_weights
        g = _grid(island=False)
        g.loc[1, "GEOID"] = "g0"
        with self.assertRaises(ValueError):
            spatial_weights(g)


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestMoranReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIX, encoding="utf-8") as fh:
            cls.ref = json.load(fh)

    def test_sids2_opengeoda(self):
        from census.spatial import morans_i
        w = _read_gal("sids2.gal")
        df = _dbf_frame("sids2.dbf", ["SIDR74"], w)
        res = morans_i(df, "SIDR74", w, permutations=99)
        ref = self.ref["moran_sids2"]
        self.assertEqual(round(res.I, 3), ref["I_rounded_3"])
        self.assertAlmostEqual(res.p_norm, ref["p_norm"], places=12)

    def test_stl(self):
        from census.spatial import morans_i
        w = _read_gal("stl.gal")
        f = libpysal.io.open(examples.get_path("stl_hom.txt"))
        df = pd.DataFrame({"GEOID": [str(i) for i in w.id_order], "HR8893": np.asarray(f.by_col["HR8893"], float)})
        res = morans_i(df, "HR8893", w, permutations=0)
        ref = self.ref["moran_stl"]
        self.assertEqual(round(res.I, 3), ref["I_rounded_3"])
        self.assertAlmostEqual(res.expected_I, ref["EI"], places=14)
        self.assertAlmostEqual(res.p_norm, ref["p_norm"], places=12)
        self.assertIsNone(res.p_sim)

    def test_checkerboard_closed_form(self):
        # Rook lattice, alternating +/-1: every neighbor has the opposite sign, so W z = -z and I = -1.
        from census.simulation import lattice_weights
        w = lattice_weights(6)
        z = np.array([(-1) ** (r + c) for r in range(6) for c in range(6)], float)
        df = pd.DataFrame({"GEOID": w.id_order, "v": z})
        from census.spatial import morans_i
        self.assertAlmostEqual(morans_i(df, "v", w, permutations=0).I, -1.0, places=12)


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestReplicateMoranAndLisa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from census.simulation import lattice_weights, make_replicates, replicate_long, sar_field
        rng = np.random.default_rng(7)
        cls.w = lattice_weights(8)
        ids = cls.w.id_order
        y = 50 + sar_field(cls.w.full()[0], 0.7, 10.0, rng)
        se = np.full(len(y), 3.0)
        R = make_replicates(y[:, None], se[:, None], rng)
        cls.df = pd.DataFrame({"GEOID": ids, "y": y, "y_se": se})
        cls.reps = replicate_long(ids, ["y"], y[:, None], R)
        cls.R = R[:, 0, :]

    def test_replicate_se_matches_esda_per_replicate(self):
        from esda.moran import Moran
        from census.spatial import morans_i
        res = morans_i(self.df, "y", self.w, permutations=99, replicates=self.reps)
        Ir = np.array([Moran(self.R[:, k], self.w, permutations=0).I for k in range(80)])
        expected = math.sqrt(4 / 80 * np.sum((Ir - res.I) ** 2))
        self.assertAlmostEqual(res.se_replicate, expected, places=12)
        self.assertAlmostEqual(res.ci_upper - res.I, 1.6448536269514722 * expected, places=10)
        self.assertGreater(res.I, 0.3)
        self.assertLess(res.p_sim, 0.05)
        self.assertIn("se_replicate", " ".join(res.notes))
        self.assertIsInstance(res.to_dict()["replicate_I"], list)

    def test_alignment_and_missing(self):
        from census.spatial import morans_i
        shuffled = self.df.sample(frac=1, random_state=1)
        a = morans_i(self.df, "y", self.w, permutations=0).I
        self.assertAlmostEqual(morans_i(shuffled, "y", self.w, permutations=0).I, a, places=12)
        with self.assertRaises(ValueError):
            morans_i(self.df.iloc[1:], "y", self.w, permutations=0)
        bad = self.df.copy()
        bad.loc[0, "y"] = np.nan
        with self.assertRaises(ValueError):
            morans_i(bad, "y", self.w, permutations=0)

    def test_local_morans_correction(self):
        from census.spatial import local_morans
        out = local_morans(self.df, "y", self.w, permutations=199, correction="bh", replicates=self.reps)
        self.assertEqual(len(out), 64)
        self.assertTrue((out["p_adjusted"] >= out["p_value"] - 1e-15).all())
        self.assertTrue(set(out["quadrant"]) <= {"HH", "LH", "LL", "HL"})
        self.assertTrue(((out["cluster"] == "ns") | out["significant"]).all())
        self.assertTrue(out["replicate_quadrant_share"].between(0, 1).all())
        from census.multitest import adjust_pvalues
        # default: two-sided p_value = min(1, 2 p_sim), BH-adjusted (PLAN_ROUND_15 B, owner decision)
        np.testing.assert_allclose(out["p_value"], np.minimum(1.0, 2 * out["p_sim"]))
        np.testing.assert_allclose(out["p_adjusted"], adjust_pvalues(out["p_value"].to_numpy(), "bh"))
        np.testing.assert_array_equal(out["significant"], out["p_adjusted"] <= 0.10)
        self.assertIn("two-sided", out.attrs["notes"][0])
        old = local_morans(self.df, "y", self.w, permutations=199, sided="folded")
        np.testing.assert_allclose(old["p_value"], old["p_sim"])
        np.testing.assert_allclose(old["p_adjusted"], adjust_pvalues(old["p_sim"].to_numpy(), "bh"))
        with self.assertRaises(ValueError):
            local_morans(self.df, "y", self.w, permutations=19, sided="one")

    def test_local_morans_defaults(self):
        import inspect

        from census.spatial import local_morans
        sig = inspect.signature(local_morans).parameters
        self.assertEqual(sig["permutations"].default, 9999)
        self.assertEqual(sig["sided"].default, "two")
        self.assertEqual(sig["correction"].default, "bh")
        self.assertEqual(sig["alpha"].default, 0.10)


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestSpatialRegressionReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIX, encoding="utf-8") as fh:
            cls.ref = json.load(fh)
        from census.spatial import spatial_weights
        cls.col = gpd.read_file(examples.get_path("columbus.shp"))
        cls.col["GEOID"] = cls.col["POLYID"].astype(str)
        cls.rook = spatial_weights(cls.col, "rook")

    def _check(self, res, ref, rtol=1e-6, se_key="se"):
        np.testing.assert_allclose(res.coefficients["estimate"], ref["betas"], rtol=rtol)
        if se_key in ref:
            np.testing.assert_allclose(res.coefficients["se_model"], ref[se_key], rtol=rtol)

    def test_gm_lag_columbus(self):
        from census.spatial import spatial_regression
        res = spatial_regression("HOVAL ~ INC + CRIME", self.col, self.rook, model="lag", method="gm", w_lags=2)
        self._check(res, self.ref["gm_lag_columbus"])
        self.assertEqual(list(res.coefficients["term"]), ["Intercept", "INC", "CRIME", "rho"])
        self.assertIn("lm_tests", res.diagnostics)

    def test_gm_error_het_columbus(self):
        from census.spatial import spatial_regression
        res = spatial_regression("HOVAL ~ INC + CRIME", self.col, self.rook, model="error", method="gm", step1c=True)
        self._check(res, self.ref["gm_error_het_columbus"], rtol=6e-4)  # published to 4 decimals

    def _baltim(self):
        w = _read_gal("baltim_q.gal")
        return _dbf_frame("baltim.dbf", ["PRICE", "NROOM", "AGE", "SQFT"], w), w

    def test_ml_lag_and_error_baltim(self):
        from census.spatial import spatial_regression
        df, w = self._baltim()
        lag = spatial_regression("PRICE ~ NROOM + AGE + SQFT", df, w, model="lag", method="ml")
        ref = self.ref["ml_lag_baltim"]
        np.testing.assert_allclose(lag.coefficients["estimate"], ref["betas"], rtol=1e-5)
        np.testing.assert_allclose(lag.coefficients["se_model"] ** 2, ref["vm_diag"], rtol=1e-5)
        err = spatial_regression("PRICE ~ NROOM + AGE + SQFT", df, w, model="error", method="ml")
        np.testing.assert_allclose(err.coefficients["estimate"], self.ref["ml_error_baltim"]["betas"], rtol=1e-4)
        self.assertEqual(err.coefficients["term"].iloc[-1], "lambda")
        self.assertIn("logll", lag.diagnostics)

    def test_lm_tests_columbus(self):
        from census.spatial import lm_diagnostics
        w = _read_gal("columbus.gal")
        w.transform = "r"
        df = _dbf_frame("columbus.dbf", ["HOVAL", "INC", "CRIME"], w)
        out = lm_diagnostics(df[["HOVAL"]].to_numpy(), df[["INC", "CRIME"]].to_numpy(), w)
        ref = self.ref["lm_tests_columbus"]
        for key in ("lm_error", "lm_lag", "robust_lm_error", "robust_lm_lag", "lm_sarma"):
            self.assertAlmostEqual(out[key]["statistic"], ref[key][0], places=4)
            self.assertAlmostEqual(out[key]["p_value"], ref[key][1], places=4)
        self.assertIn(out["suggested_model"], ("error", "lag", "ols (neither LM test significant at 0.05)"))

    def test_replicate_refits(self):
        from census.simulation import lattice_weights, make_replicates, replicate_long, sar_field
        from census.spatial import spatial_regression
        rng = np.random.default_rng(3)
        w = lattice_weights(7)
        ids = w.id_order
        x = rng.uniform(0, 10, 49)
        y = 5 + 2 * x + sar_field(w.full()[0], 0.4, 2.0, rng)
        se = np.full(49, 1.0)
        reps = replicate_long(ids, ["y"], y[:, None], make_replicates(y[:, None], se[:, None], rng))
        df = pd.DataFrame({"GEOID": ids, "y": y, "x": x})
        for method in ("gm", "ml"):
            res = spatial_regression("y ~ x", df, w, model="error", method=method, replicates=reps)
            self.assertIn("sampling_replicate", res.uncertainty_sources)
            self.assertTrue(np.isfinite(res.coefficients["se_sampling"]).all())
            self.assertEqual(res.diagnostics["replicate_failures"], 0)
        with self.assertRaises(ValueError):
            spatial_regression("y ~ x", df, w, target="these_areas")

    def test_default_method_per_model(self):
        # PLAN_ROUND_15 B (owner decision): GMM for lag, ML for error; ML lag stays available with its caveat
        from census.simulation import lattice_weights, sar_field
        from census.spatial import DEFAULT_METHODS, spatial_regression
        self.assertEqual(DEFAULT_METHODS, {"lag": "gm", "error": "ml"})
        rng = np.random.default_rng(5)
        w = lattice_weights(7)
        x = rng.uniform(0, 10, 49)
        df = pd.DataFrame({"GEOID": w.id_order, "x": x, "y": 5 + 2 * x + sar_field(w.full()[0], 0.4, 2.0, rng)})
        lag = spatial_regression("y ~ x", df, w)
        self.assertIn("GMM", lag.method)
        self.assertIsNone(lag.provenance["ml_method"])
        self.assertIn("the default for the lag model", " ".join(lag.notes))
        explicit = spatial_regression("y ~ x", df, w, method="gm")
        np.testing.assert_allclose(lag.coefficients["estimate"], explicit.coefficients["estimate"])
        ml = spatial_regression("y ~ x", df, w, method="ml")
        self.assertIn("ML", ml.method)
        self.assertIn("ML lag intervals for rho covered 0.48", " ".join(ml.notes))
        err = spatial_regression("y ~ x", df, w, model="error")
        self.assertIn("ML", err.method)
        self.assertIn("understates the true lambda", " ".join(err.notes))


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestMoranReplicateSimulation(unittest.TestCase):
    def test_fast_study(self):
        from census.simulation import run_study, spatial_config
        df = run_study(spatial_config(fast=True), studies=["morans_i_replicate_se"])
        row = df.iloc[0]
        # Validated finding: the replicate SE is conservative under ACS-like CVs (never anti-conservative).
        self.assertGreaterEqual(row["coverage"], 0.88)
        self.assertGreater(row["se_ratio"], 0.95)
        self.assertLess(row["se_ratio"], 2.0)
        self.assertLess(row["E_I_estimates"], row["I_true_values"])  # attenuation by sampling noise


if __name__ == "__main__":
    unittest.main()
