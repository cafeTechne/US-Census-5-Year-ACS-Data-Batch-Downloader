"""Regression tests for the PR #2 (Round 8) code-review findings."""

import copy
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

from census import workflows as wf
from census import workflows_inference as w
from census.mcp_profiles import with_guidance


class TestFayHerriotControlledArea(unittest.TestCase):
    def test_zero_variance_area_does_not_collapse_sigma2_u(self):
        from census.sae import estimate_sigma2_u

        rng = np.random.default_rng(3)
        m = 40
        X = np.column_stack([np.ones(m), rng.normal(size=m)])
        D = rng.uniform(0.2, 0.6, m)
        y = X @ np.array([1.0, 0.5]) + rng.normal(0, np.sqrt(0.5), m) + rng.normal(0, np.sqrt(D))
        for method in ("REML", "ML"):
            base = estimate_sigma2_u(y[1:], X[1:], D[1:], method)
            D0 = D.copy()
            D0[0] = 0.0
            got = estimate_sigma2_u(y, X, D0, method)
            self.assertGreater(base, 0.1)
            self.assertAlmostEqual(got, base, delta=0.5 * base)


class _Frame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        rng = np.random.default_rng(0)
        n = 60
        x = rng.normal(10, 2, n)
        cls.df = pd.DataFrame({
            "GEOID": [f"36061{i:06d}" for i in range(n)],
            "y": 2 + 0.5 * x + rng.normal(0, 0.5, n), "y_se": np.full(n, 0.3),
            "x": x, "x_se": np.full(n, 0.2),
            "wt": rng.uniform(0.5, 2, n), "pop": rng.uniform(1000, 5000, n),
        })
        cls.df["count"] = rng.poisson(cls.df["pop"] * 0.01)
        cls.path = os.path.join(cls.dir, "af.csv")
        cls.df.to_csv(cls.path, index=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)


class TestWorkflowResults(_Frame):
    def test_results_json_serializable(self):
        outs = [
            w.fit_model(self.path, "y ~ x"),
            w.small_area_estimates(self.path, "y ~ x", "y_se", output_path=os.path.join(self.dir, "sae.csv")),
            w.correlation_matrix(self.path, ["y", "x"]),
            w.reliability_profile(self.path, ["y", "x"]),
            w.minimum_detectable_difference(10.0, 12.0),
            w.trend_test([2013, 2018, 2023], [14.1, 13.0, 11.8], [0.4, 0.4, 0.3]),
            w.fit_model(self.path, "y ~ nope"),
        ]
        for out in outs:
            json.dumps(out)  # raises on DataFrame / numpy leftovers
        self.assertTrue(outs[1]["success"], outs[1])
        self.assertIsInstance(outs[1]["coefficients"], list)

    def test_jsonable_handles_frames_series_arrays(self):
        payload = wf._jsonable({"df": pd.DataFrame({"a": [1.0, np.nan]}), "s": pd.Series([1, 2]),
                                "arr": np.array([1.5, np.inf])})
        self.assertEqual(json.loads(json.dumps(payload)),
                         {"df": [{"a": 1.0}, {"a": None}], "s": {"0": 1, "1": 2}, "arr": [1.5, None]})

    def test_bad_formula_is_invalid_input(self):
        res = w.fit_model(self.path, "y ~ x +")
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "INVALID_INPUT")
        self.assertTrue(res["summary"] and res["next_step"])

    def test_weights_and_exposure_forwarded(self):
        wls = w.fit_model(self.path, "y ~ x", model="wls", weights="wt")
        self.assertTrue(wls["success"], wls)
        import statsmodels.formula.api as smf
        ref = smf.wls("y ~ x", self.df, weights=self.df["wt"]).fit()
        coef = {r["term"]: r["estimate"] for r in wls["coefficients"]}
        self.assertAlmostEqual(coef["x"], ref.params["x"], places=8)
        poi = w.fit_model(self.path, "count ~ x", model="poisson", exposure="pop")
        self.assertTrue(poi["success"], poi)
        coef = {r["term"]: r["estimate"] for r in poi["coefficients"]}
        self.assertLess(coef["Intercept"], -2)  # log rate per person, not log count

    def test_target_rejected_for_corrected_methods(self):
        res = w.fit_model(self.path, "y ~ x", method="regression_calibration", error_covariates=["x"],
                          target="these_areas")
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "INVALID_INPUT")

    def test_cli_tool_unexpected_exception_is_structured(self):
        import argparse
        import contextlib
        import io

        from census import __main__ as cli

        orig = w.reliability_profile
        w.reliability_profile = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = cli.cmd_tool(argparse.Namespace(name="reliability_profile", arguments="{}", json=True))
        finally:
            w.reliability_profile = orig
        out = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "RuntimeError")


class TestAnalysisFrameSummary(unittest.TestCase):
    def test_noisy_variables_counted(self):
        d = tempfile.mkdtemp()
        try:
            n = 20
            df = pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(n)], "B01003_001E": np.full(n, 100.0),
                               "B01003_001M": np.full(n, 90.0)})  # CV ~ 55%
            p = os.path.join(d, "in.csv")
            df.to_csv(p, index=False)
            res = w.build_analysis_frame(p, output_path=os.path.join(d, "af.parquet"))
            self.assertTrue(res["success"], res)
            self.assertIn("1 variable(s)", res["summary"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestCoreSummaries(unittest.TestCase):
    def test_undefined_values_do_not_raise(self):
        derive = with_guidance("derive_estimate", wf.derive_estimate)
        res = derive("ratio", [10, 0], [1, 1])
        self.assertIn("undefined", res["summary"])
        from census.stats import z_test

        def sig(est1, moe1, est2, moe2):
            r = z_test(est1, moe1, est2, moe2)
            return {"success": True, "difference": r.get("difference"), "p_value": r.get("p_value"),
                    "verdict": "undetermined"}

        out = with_guidance("calculate_significance", sig)(500, -222222222, 400, 16.45)
        self.assertTrue(out["summary"])
        out = with_guidance("calculate_significance", lambda: {"difference": None, "p_value": float("nan"),
                                                               "verdict": "v"})()
        self.assertIn("undefined", out["summary"])


class TestSentinelHelper(unittest.TestCase):
    def test_moe_to_se(self):
        from census.stats import moe_to_se

        se = moe_to_se([16.45, -555555555, -222222222, np.nan, -5])
        self.assertAlmostEqual(se[0], 10.0)
        self.assertEqual(se[1], 0.0)
        self.assertTrue(np.isnan(se[2:]).all())

    def test_percent_change_replicate_undefined_is_nan(self):
        from census.analysis import _eval_spec_replicates

        x = np.array([[110.0, 120.0], [100.0, 120.0]])
        y = np.array([[100.0, 100.0], [0.0, 100.0]])
        vals = {"a": x, "b": y}
        out = _eval_spec_replicates({"op": "percent_change", "num": "a", "den": "b"}, lambda k: vals[k])
        self.assertTrue(np.isnan(out[1, 0]))
        self.assertAlmostEqual(out[0, 0], 10.0)


class TestReportSE(unittest.TestCase):
    def test_table_se_matches_p_value_basis(self):
        from scipy import stats as sps

        from census.models import fit_replicate
        from census.report_tables import regression_table

        rng = np.random.default_rng(1)
        n = 50
        x = rng.normal(size=n)
        df = pd.DataFrame({"GEOID": [str(i) for i in range(n)], "x": x, "y": 1 + x + rng.normal(size=n)})
        for target, col in (("population", "se_model"), ("conservative", "se_total")):
            res = fit_replicate("y ~ x", df, None, target=target)
            t = regression_table(res, correction=None)
            row = t.data[t.data["term"] == "x"].iloc[0]
            self.assertEqual(row["se_basis"], col)
            p = 2 * sps.norm.sf(abs(row["estimate"] / row["se_reported"]))
            self.assertAlmostEqual(p, row["p_value"], places=10)


class TestManifestFormulaInArgs(unittest.TestCase):
    def test_formula_in_args_runs_and_duplicates_rejected(self):
        from census.manifest import load_manifest, run_manifest, validate_manifest
        from tests.test_manifest import EXAMPLE, FakeClient

        m = load_manifest(EXAMPLE)
        a = m["analyses"][0]
        a["args"]["formula"] = a.pop("formula")
        self.assertEqual(validate_manifest(m, resolve=False), [])
        d = tempfile.mkdtemp()
        try:
            res = run_manifest(m, d, client=FakeClient(), use_cache=False)
            self.assertTrue(res["success"], res["deviations"])
            h = res["hypotheses"].set_index("id")
            self.assertEqual(h.loc["H1", "status"], "tested")
        finally:
            shutil.rmtree(d, ignore_errors=True)
        dup = copy.deepcopy(m)
        dup["analyses"][0]["formula"] = dup["analyses"][0]["args"]["formula"]
        self.assertTrue(any("both" in e for e in validate_manifest(dup, resolve=False)))


if __name__ == "__main__":
    unittest.main()
