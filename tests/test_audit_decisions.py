# SPDX-License-Identifier: AGPL-3.0-or-later
"""PLAN_ROUND_11 owner decisions: Q1/D8 (DiD serial correlation), Q2/D7
(replicate_policy strict/flag), R2 (PUMS all-columns memory warning)."""

import os
import shutil
import tempfile
import unittest
import warnings
from dataclasses import replace
from unittest import mock

import numpy as np
import pandas as pd

from census import models, pums
from census.did import difference_in_differences
from census.manifest import load_manifest, run_manifest
from census.multilevel import fit_multilevel
from census.results import IncompleteReplicatesError, replicate_completeness
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
        self.assertIn("rows without replicates held fixed: {'y': 2}", replicate_flag(r))
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
        self.assertEqual(replicate_flag(r), " (REPLICATES INCOMPLETE: sampling SE may be understated "
                                            "(2 replicate refit(s) failed))")
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
        res = pums.pums_regression("y ~ x", df, max_failed_replicates=1)
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
        rows = study_did_serial(FAST, 1, rhos=(0.5,), layouts={"2 periods": [2014, 2019]},
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


def _did_case(years=(2014, 2019), seed=3, period_years=5):
    rng = np.random.default_rng(seed)
    years = list(years)
    T, alpha, se = _did_panel(FAST, rng, years)
    th = _ar1_truth(T, alpha, years, 0.0, rng)
    y = {yr: th[yr] + se[yr] * rng.standard_normal(len(T)) for yr in years}
    g = [f"{i:05d}" for i in range(len(T))]
    rl = pd.concat([replicate_long(g, ["y"], y[yr][:, None], make_replicates(y[yr][:, None], se[yr][:, None], rng)
                                   ).assign(Year=yr) for yr in years], ignore_index=True)
    return _did_frame(T, years, y, se), rl


def _pums_df(n=300, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"x": rng.normal(10, 2, n)})
    df["y"] = 1 + 0.5 * df["x"] + rng.normal(0, 1, n)
    df["PWGTP"] = 20.0
    for r in range(1, 81):
        df[f"PWGTP{r}"] = 20.0 * rng.uniform(0.5, 1.5, n)
    return df


class TestReviewFixes(unittest.TestCase):
    """Code-review findings on round11/audit-decisions (items 1-10)."""

    # 1. pums_regression max_failed_replicates
    def test_pums_failed_refit_raises_by_default(self):
        df = _pums_df()
        df.loc[0, "PWGTP7"] = np.nan
        with self.assertRaisesRegex(RuntimeError, "1 of 80 replicate refits failed"):
            pums.pums_regression("y ~ x", df)
        res = pums.pums_regression("y ~ x", df, max_failed_replicates=1)
        self.assertEqual(res.diagnostics["replicates_incomplete"]["failed_refits"], 1)

    def test_pums_all_refits_failed(self):
        df = _pums_df()
        df.loc[0, [f"PWGTP{r}" for r in range(1, 81)]] = np.nan
        with self.assertRaisesRegex(RuntimeError, "all 80 replicate refits failed"):
            pums.pums_regression("y ~ x", df, max_failed_replicates=80)
        with self.assertRaises(IncompleteReplicatesError):
            pums.pums_regression("y ~ x", df, replicate_policy="strict")

    # 2. correlation completeness; manifest runs it strict
    def test_correlation_completeness(self):
        from census.correlation import correlation_matrix
        frame, reps = _xy()
        part = reps[~((reps["variable"] == "y") & (reps["GEOID"] == "00000"))]
        r = correlation_matrix(frame, ["y", "x"], replicates=part)
        self.assertEqual(r.provenance["replicates_incomplete"]["rows_held_fixed"], {"y": 1})
        self.assertIn(" (REPLICATES INCOMPLETE:", replicate_flag(r))
        with self.assertRaises(IncompleteReplicatesError):
            correlation_matrix(frame, ["y", "x"], replicates=part, replicate_policy="strict")
        with self.assertRaises(IncompleteReplicatesError):   # x has an SE column but no replicates
            correlation_matrix(frame, ["y", "x"], replicates=reps[reps["variable"] == "y"],
                               replicate_policy="strict")
        self.assertNotIn("replicates_incomplete",
                         correlation_matrix(frame, ["y", "x"], replicates=reps, replicate_policy="strict").provenance)

    def test_manifest_runs_correlation_strict(self):
        from census import manifest
        frame, reps = _xy()
        part = reps[~((reps["variable"] == "y") & (reps["GEOID"] == "00000"))]
        with self.assertRaises(IncompleteReplicatesError):
            manifest._run_analysis({"id": "r1", "method": "correlation_matrix", "variables": ["y", "x"]},
                                   frame, 0.9, part, "holm")
        self.assertIn("correlation_matrix", manifest.REPLICATE_METHODS)

    # 3. DiD serial-correlation note depends on the number of periods
    def test_did_serial_note_by_periods(self):
        frame, _ = _did_case((2009, 2014, 2019, 2024))
        r = difference_in_differences(frame, "y", "treated", 2019)
        self.assertTrue(any("validated with AR(1)" in n for n in r.notes))
        self.assertFalse(any("NOT VALIDATED" in n for n in r.notes))
        frame6, _ = _did_case(range(2015, 2021), period_years=1)
        r6 = difference_in_differences(frame6, "y", "treated", 2019, period_years=1)
        self.assertFalse(any("validated with AR(1)" in n for n in r6.notes))
        warn = [n for n in r6.notes if "SERIAL CORRELATION NOT VALIDATED" in n]
        self.assertEqual(len(warn), 1)
        self.assertIn("Bertrand, Duflo & Mullainathan 2004", warn[0])

    # 4 + 5. study_did_serial: no clusters argument; reps counts repetitions
    def test_did_serial_signature_and_reps(self):
        with self.assertRaises(TypeError):
            study_did_serial(FAST, 1, clusters=(None,))
        rows = study_did_serial(replace(FAST, n_areas=40), 2, rhos=(0.5,),
                                layouts={"4 periods": [2009, 2014, 2019, 2024]}, replicate_reps=1)
        ev = {r["interval_target"]: r for r in rows if r["design"] == "event"}
        self.assertEqual((ev["population"]["reps"], ev["population"]["n_terms"]), (2, 3))
        self.assertEqual((ev["these_areas"]["reps"], ev["these_areas"]["n_terms"]), (1, 3))

    def test_these_areas_truth_uses_estimator_A(self):
        # With no sampling error, the target equals the estimate exactly.
        from census.did import _did_vfun, _differences, _prepare
        from census.multilevel import BlockGLS
        frame, _ = _did_case((2009, 2014, 2019, 2024))
        res = difference_in_differences(frame, "y", "treated", 2019, design="event")
        P = _prepare(frame, "y", "treated", "Year", "GEOID", 2019, None, "event", None, 5)
        d = _differences(P, "y", "treated", "Year", "GEOID", "event")
        eng = BlockGLS(d["unit"], d["dy"], d["X"], _did_vfun, D=d["D"], extra={"Dr": d["Dr"]})
        b = eng.gls(np.array([res.diagnostics["A_area_period_var"]]), d["dy"])
        np.testing.assert_allclose(b, res.coefficients["estimate"].to_numpy(), rtol=1e-6, atol=1e-8)

    # 6. every refit failed: clear error before np.vstack
    def test_all_refits_failed_models(self):
        frame, reps = _xy()
        real, calls = models._fit, {"n": 0}

        def fail_refits(*a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise np.linalg.LinAlgError("singular")
            return real(*a, **k)
        with mock.patch.object(models, "_fit", fail_refits):
            with self.assertRaisesRegex(RuntimeError, "all 80 replicate refits failed"):
                models.fit_replicate("y ~ x", frame, reps, max_failed_replicates=80)
        calls["n"] = 0
        with mock.patch.object(models, "_fit", fail_refits), self.assertRaises(IncompleteReplicatesError):
            models.fit_replicate("y ~ x", frame, reps, max_failed_replicates=80, replicate_policy="strict")

    def test_all_refits_failed_did(self):
        frame, rl = _did_case()
        with mock.patch("census.did._refit_A", side_effect=np.linalg.LinAlgError("x")):
            with self.assertRaisesRegex(RuntimeError, "all 80 replicate refits failed"):
                difference_in_differences(frame, "y", "treated", 2019, replicates=rl, max_failed_replicates=80)
            with self.assertRaises(IncompleteReplicatesError):
                difference_in_differences(frame, "y", "treated", 2019, replicates=rl, replicate_policy="strict")

    def test_all_refits_failed_multilevel(self):
        from census.multilevel import BlockGLS
        frame, reps = _xy()
        frame["county"] = [str(i // 6) for i in range(len(frame))]
        with mock.patch.object(BlockGLS, "gls", side_effect=np.linalg.LinAlgError("x")):
            with self.assertRaisesRegex(RuntimeError, "all 80 replicate refits failed"):
                fit_multilevel("y ~ x", frame, "county", sampling_se="y_se", replicates=reps,
                               max_failed_replicates=80)
            with self.assertRaises(IncompleteReplicatesError):
                fit_multilevel("y ~ x", frame, "county", sampling_se="y_se", replicates=reps,
                               replicate_policy="strict")

    # 7. dedicated exception class
    def test_incomplete_replicates_error_class(self):
        self.assertTrue(issubclass(IncompleteReplicatesError, ValueError))
        with self.assertRaises(IncompleteReplicatesError):
            replicate_completeness("strict", failed_refits=1)
        self.assertIsNone(replicate_completeness("strict"))

    # 8. strict refuses missing rows/variables before running any refit
    def test_strict_precheck_before_refits_models(self):
        frame, reps = _xy()
        part = reps[~((reps["variable"] == "y") & (reps["GEOID"] == "00000"))]
        with mock.patch.object(models, "_fit", wraps=models._fit) as f, self.assertRaises(IncompleteReplicatesError):
            models.fit_replicate("y ~ x", frame, part, replicate_policy="strict")
        self.assertEqual(f.call_count, 1)   # full-sample fit only

    def test_strict_precheck_before_refits_multilevel(self):
        from census.multilevel import BlockGLS
        frame, reps = _xy()
        frame["county"] = [str(i // 6) for i in range(len(frame))]
        part = reps[~((reps["variable"] == "y") & (reps["GEOID"] == "00003"))]
        with mock.patch.object(BlockGLS, "gls", autospec=True, side_effect=BlockGLS.gls) as g:
            with self.assertRaises(IncompleteReplicatesError):
                fit_multilevel("y ~ x", frame, "county", sampling_se="y_se", replicates=part,
                               replicate_policy="strict")
        self.assertEqual(g.call_count, 0)

    def test_strict_precheck_before_refits_spatial(self):
        try:
            from census import spatial
            from census.simulation import lattice_weights
        except ImportError:   # pragma: no cover
            self.skipTest("spatial dependencies unavailable")
        rng = np.random.default_rng(2)
        w = lattice_weights(6)
        ids = w.id_order
        x = rng.normal(10, 2, len(ids))
        y = 3 + 0.5 * x + rng.normal(0, 1, len(ids))
        df = pd.DataFrame({"GEOID": ids, "Year": 2023, "y": y, "x": x, "x_se": 1.0, "y_se": 1.0})
        rl = replicate_long(ids, ["y"], y[:, None], make_replicates(y[:, None], np.ones((len(ids), 1)), rng)
                            ).assign(Year=2023)
        with mock.patch.object(spatial, "_spreg_fit", wraps=spatial._spreg_fit) as f:
            with self.assertRaises(IncompleteReplicatesError):
                spatial.spatial_regression("y ~ x", df, w, replicates=rl, replicate_policy="strict")
        self.assertEqual(f.call_count, 1)   # full-sample fit only

    # 9. one combined note, no duplicate per-fact notes
    def test_no_duplicate_notes(self):
        frame, reps = _xy()
        part = reps[~((reps["variable"] == "y") & (reps["GEOID"] == "00000"))]
        r = models.fit_replicate("y ~ x", frame, part)
        self.assertEqual(sum("held" in n for n in r.notes), 1)
        frame2, rl = _did_case()
        r2 = difference_in_differences(frame2, "y", "treated", 2019, replicates=rl.iloc[1:], replicate_refit="gls")
        self.assertEqual(sum("held fixed" in n for n in r2.notes), 1)
        self.assertEqual(replicate_flag(r2), f" (REPLICATES INCOMPLETE: "
                                             f"{r2.diagnostics['replicates_incomplete']['note']})")

    # 10. PUMS memory note: per-call warning and surfaced in workflow payloads
    def test_memory_warning_every_call_names_file(self):
        path = TestPumsMemoryWarning._csv(self)
        with mock.patch("census.pums.os.path.getsize", return_value=250 * 1024 ** 2), \
                warnings.catch_warnings(record=True) as rec:
            pums.read_pums_csv(path, "person")
            pums.read_pums_csv(path, "person")
        msgs = [str(x.message) for x in rec if issubclass(x.category, pums.PumsMemoryWarning)]
        self.assertEqual(len(msgs), 2)
        self.assertIn("psam_p56.csv", msgs[0])

    def test_memory_note_in_workflow_payload(self):
        from census import workflows_microdata as wm
        df = _pums_df()
        df["SERIALNO"], df["SPORDER"], df["PUMA"] = "1", "1", "00100"
        df.attrs["memory_note"] = "columns=None reads all columns of big.csv"
        with mock.patch.object(pums, "fetch_pums", return_value=df):
            reg = wm.pums_regression("56", 2024, "y ~ x")
            tab = wm.pums_tabulate("56", 2024, "mean", var="x")
        for res in (reg, tab):
            self.assertTrue(res["success"], res)
            self.assertEqual(res["warnings"], ["MEMORY: columns=None reads all columns of big.csv"])
            self.assertIn("WARNING: MEMORY", res["summary"])
        df.attrs.pop("memory_note")
        with mock.patch.object(pums, "fetch_pums", return_value=df):
            self.assertEqual(wm.pums_regression("56", 2024, "y ~ x")["warnings"], [])


if __name__ == "__main__":
    unittest.main()
