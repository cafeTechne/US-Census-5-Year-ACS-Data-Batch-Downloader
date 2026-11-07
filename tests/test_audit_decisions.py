# SPDX-License-Identifier: AGPL-3.0-or-later
"""PLAN_ROUND_11 owner decisions: Q1/D8 (DiD serial correlation), Q2/D7
(replicate_policy strict/flag), R2 (PUMS all-columns memory warning)."""

import os
import shutil
import tempfile
import unittest
import warnings
from unittest import mock

import numpy as np
import pandas as pd

from census import models, pums
from census.did import difference_in_differences
from census.manifest import load_manifest, run_manifest
from census.multilevel import fit_multilevel
from census.results import replicate_completeness
from census.simulation import (FAST, _did_frame, _did_panel, _ar1_truth, acs_frame, make_replicates,
                               replicate_long, run_study, serial_config, study_did_serial)
from census.workflows_inference import replicate_flag

from tests.test_manifest import EXAMPLE, FakeClient


def _xy(n=60, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.uniform(10, 50, n)
    y = 5 + 0.7 * x + rng.normal(0, 3, n)
    sx, sy = np.full(n, 1.5), np.full(n, 2.0)
    g = [f"{i:05d}" for i in range(n)]
    frame = acs_frame(g, {"x": x, "y": y}, {"x": sx, "y": sy})
    est = np.column_stack([y, x])
    reps = replicate_long(g, ["y", "x"], est, make_replicates(est, np.column_stack([sy, sx]), rng))
    return frame, reps


class TestReplicatePolicy(unittest.TestCase):
    def test_complete_replicates_not_flagged(self):
        frame, reps = _xy()
        for pol in ("flag", "strict"):
            r = models.fit_replicate("y ~ x", frame, reps, replicate_policy=pol)
            self.assertNotIn("replicates_incomplete", r.diagnostics)
            self.assertEqual(replicate_flag(r), "")

    def test_rows_held_fixed(self):
        frame, reps = _xy()
        reps = reps[~((reps["variable"] == "y") & reps["GEOID"].isin(["00000", "00001"]))]
        r = models.fit_replicate("y ~ x", frame, reps)
        info = r.diagnostics["replicates_incomplete"]
        self.assertEqual(info["rows_held_fixed"], {"y": 2})
        self.assertEqual(info["failed_refits"], 0)
        self.assertIn("sampling SE may be understated", info["note"])
        self.assertIn("2 rows held fixed", replicate_flag(r))
        with self.assertRaisesRegex(ValueError, "replicate_policy='strict'"):
            models.fit_replicate("y ~ x", frame, reps, replicate_policy="strict")

    def test_stochastic_variable_without_replicates(self):
        frame, reps = _xy()
        frame["z"] = np.arange(len(frame), dtype=float)   # fixed covariate: no SE column
        reps_y = reps[reps["variable"] == "y"]
        r = models.fit_replicate("y ~ x + z", frame, reps_y)
        self.assertEqual(r.diagnostics["replicates_incomplete"]["variables_without_replicates"], ["x"])
        with self.assertRaises(ValueError):
            models.fit_replicate("y ~ x + z", frame, reps_y, replicate_policy="strict")
        # z alone (no SE) is not stochastic: strict passes
        models.fit_replicate("y ~ z", frame, reps_y, replicate_policy="strict")

    def test_failed_refits(self):
        frame, reps = _xy()
        real = models._fit
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] in (3, 9):   # 1st call is the full-sample fit
                raise np.linalg.LinAlgError("singular")
            return real(*a, **k)
        with mock.patch.object(models, "_fit", flaky):
            r = models.fit_replicate("y ~ x", frame, reps, max_failed_replicates=5)
        self.assertEqual(r.diagnostics["replicates_incomplete"]["failed_refits"], 2)
        self.assertIn("(sampling SE may be understated: 2 replicate refits failed)", replicate_flag(r))
        calls["n"] = 0
        with mock.patch.object(models, "_fit", flaky), self.assertRaises(ValueError):
            models.fit_replicate("y ~ x", frame, reps, max_failed_replicates=5, replicate_policy="strict")

    def test_bad_policy(self):
        frame, reps = _xy()
        with self.assertRaises(ValueError):
            models.fit_replicate("y ~ x", frame, reps, replicate_policy="lenient")
        with self.assertRaises(ValueError):
            replicate_completeness("bogus")

    def test_multilevel_rows_held_fixed(self):
        frame, reps = _xy()
        frame["county"] = [str(i // 6) for i in range(len(frame))]
        reps = reps[~((reps["variable"] == "y") & (reps["GEOID"] == "00003"))]
        r = fit_multilevel("y ~ x", frame, "county", sampling_se="y_se", replicates=reps)
        self.assertEqual(r.diagnostics["replicates_incomplete"]["rows_held_fixed"], {"y": 1})
        with self.assertRaises(ValueError):
            fit_multilevel("y ~ x", frame, "county", sampling_se="y_se", replicates=reps, replicate_policy="strict")

    def test_did_rows_held_fixed(self):
        rng = np.random.default_rng(3)
        years = [2014, 2019]
        T, alpha, se = _did_panel(FAST, rng, years)
        th = _ar1_truth(T, alpha, years, 0.0, rng)
        y = {yr: th[yr] + se[yr] * rng.standard_normal(len(T)) for yr in years}
        g = [f"{i:05d}" for i in range(len(T))]
        rl = pd.concat([replicate_long(g, ["y"], y[yr][:, None], make_replicates(y[yr][:, None], se[yr][:, None], rng)
                                       ).assign(Year=yr) for yr in years], ignore_index=True)
        rl = rl.iloc[1:]
        frame = _did_frame(T, years, y, se)
        r = difference_in_differences(frame, "y", "treated", 2019, replicates=rl, replicate_refit="gls")
        self.assertEqual(r.diagnostics["replicates_incomplete"]["rows_held_fixed"], {"y": 1})
        with self.assertRaises(ValueError):
            difference_in_differences(frame, "y", "treated", 2019, replicates=rl, replicate_refit="gls",
                                      replicate_policy="strict")

    def test_pums_failed_refit(self):
        rng = np.random.default_rng(0)
        n = 300
        df = pd.DataFrame({"x": rng.normal(10, 2, n)})
        df["y"] = 1 + 0.5 * df["x"] + rng.normal(0, 1, n)
        df["PWGTP"] = 20.0
        for r in range(1, 81):
            df[f"PWGTP{r}"] = 20.0 * rng.uniform(0.5, 1.5, n)
        df.loc[0, "PWGTP7"] = np.nan
        res = pums.pums_regression("y ~ x", df)
        self.assertEqual(res.diagnostics["replicates_incomplete"]["failed_refits"], 1)
        with self.assertRaises(ValueError):
            pums.pums_regression("y ~ x", df, replicate_policy="strict")


class TestManifestStrict(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.m = load_manifest(EXAMPLE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_incomplete_replicates_are_a_deviation(self):
        base = run_manifest(self.m, os.path.join(self.dir, "a"), client=FakeClient(), use_cache=False)
        frame = base["frame"]
        fa = [a for a in self.m["analyses"] if a["method"] == "fit_replicate"][0]
        yv = fa["formula"].split("~")[0].strip()
        keep = frame.iloc[1:]   # first area has no replicates -> would be held fixed
        rng = np.random.default_rng(0)
        est = keep[yv].to_numpy(float)[:, None]
        rl = replicate_long(keep["GEOID"].astype(str).tolist(), [yv], est,
                            make_replicates(est, np.maximum(keep[f"{yv}_se"].to_numpy(float), 1.0)[:, None], rng))
        if "Year" in frame.columns:
            rl["Year"] = keep["Year"].to_numpy()
        res = run_manifest(self.m, os.path.join(self.dir, "b"), client=FakeClient(), replicates=rl, use_cache=False)
        kinds = {d["kind"] for d in res["deviations"]}
        self.assertIn("replicates_incomplete", kinds)
        self.assertFalse(res["success"])


class TestPumsMemoryWarning(unittest.TestCase):
    def _csv(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        df = pd.DataFrame({"SERIALNO": ["1", "2"], "SPORDER": ["1", "1"], "PUMA": ["00100", "00100"],
                           "ST": ["56", "56"], "AGEP": ["30", "40"], "PWGTP": ["10", "12"]})
        for i in range(1, 81):
            df[f"PWGTP{i}"] = "11"
        path = os.path.join(d, "psam_p56.csv")
        df.to_csv(path, index=False)
        return path

    def test_large_all_columns_warns(self):
        path = self._csv()
        with mock.patch("census.pums.os.path.getsize", return_value=250 * 1024 ** 2):
            with self.assertWarnsRegex(UserWarning, "columns="):
                f = pums.read_pums_csv(path, "person")
        self.assertIn("memory_note", f.attrs)
        with mock.patch("census.pums.os.path.getsize", return_value=250 * 1024 ** 2), warnings.catch_warnings():
            warnings.simplefilter("error")
            f2 = pums.read_pums_csv(path, "person", columns=["AGEP"])   # columns given: no warning
        self.assertNotIn("memory_note", f2.attrs)

    def test_small_file_silent(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            f = pums.read_pums_csv(self._csv(), "person")
        self.assertNotIn("memory_note", f.attrs)


class TestDidSerial(unittest.TestCase):
    def test_reduced_study_runs(self):
        df = run_study(serial_config(fast=True, n_areas=60), studies=["did_serial"])
        self.assertEqual(set(df["se_method"]), {"model"})
        self.assertEqual(set(df["rho"]), {0.0, 0.5, 0.8})
        self.assertEqual(set(df["design"]), {"two_group", "event"})
        self.assertTrue(((df["coverage"] >= 0) & (df["coverage"] <= 1)).all())

    def test_replicate_target_runs(self):
        rows = study_did_serial(FAST, 1, rhos=(0.5,), layouts={"2 periods": [2014, 2019]}, clusters=(None,),
                                replicate_reps=1)
        self.assertIn("these_areas", {r["interval_target"] for r in rows})

    def test_independence_note_and_no_cluster_option(self):
        rng = np.random.default_rng(5)
        years = [2009, 2014, 2019, 2024]
        T, alpha, se = _did_panel(FAST, rng, years)
        th = _ar1_truth(T, alpha, years, 0.8, rng)
        y = {yr: th[yr] + se[yr] * rng.standard_normal(len(T)) for yr in years}
        r = difference_in_differences(_did_frame(T, years, y, se), "y", "treated", 2019, design="event")
        self.assertTrue(any("independent across periods" in n for n in r.notes))
        with self.assertRaises(TypeError):
            difference_in_differences(_did_frame(T, years, y, se), "y", "treated", 2019, cluster="unit")

if __name__ == "__main__":
    unittest.main()
