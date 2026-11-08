# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the Round 12 code-review findings (branch phase12/verifiable-workflows)."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd
import requests

from census.api import CensusAPIClient
from census.cache import QueryCache
from census.manifest import (load_manifest, read_replicates, replicates_digest, replicates_sha256,
                             resolve_data_source, run_manifest)
from census.results import ModelResult, interval_description, interval_row
from tests.test_manifest import EXAMPLE, FakeClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "reference_workflow", "frozen_api_responses.json")
SCRIPT = os.path.join(ROOT, "scripts", "inference_validation.py")
REAL_CITATION = "Retrieved via the Census Data API"


def _refuse(*args, **kwargs):
    raise OSError("network refused in tests")


def _frozen_cache(path, synthetic):
    """The reference workflow's frozen responses in a cache, marked synthetic or not."""
    with open(FIXTURE, encoding="utf-8") as f:
        frozen = json.load(f)
    cache = QueryCache(path)
    client = CensusAPIClient(api_key="", cache=cache)
    for table, rows in frozen["requests"].items():
        endpoint, params = client.build_request(year=2019, state_code="36", table_code=table,
                                                geo_level="tract", county_code="061")
        cache.set(endpoint, params, rows)
    if synthetic:
        cache.mark_synthetic(description="generated tracts", generator="seed 7")
    return cache


class _Offline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.m = load_manifest(EXAMPLE)
        self._patches = [mock.patch.object(requests.Session, "request", _refuse)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _bundle(self, folder):
        with open(os.path.join(folder, "bundle", "bundle.json"), encoding="utf-8") as f:
            b = json.load(f)
        with open(os.path.join(folder, "bundle", "methods.md"), encoding="utf-8") as f:
            return b, f.read()


class TestProvenance(_Offline):
    """Item 1: labels follow where the data came from, not only the declaration."""

    def _run_with_cache(self, m, synthetic, name):
        cache_path = os.path.join(self.dir, f"{name}.db")
        _frozen_cache(cache_path, synthetic)
        with mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": cache_path}):
            return run_manifest(m, os.path.join(self.dir, name))

    def test_example_manifest_declares_no_synthetic_source(self):
        self.assertNotIn("data_source", self.m)

    def test_synthetic_cache_labels_synthetic(self):
        res = self._run_with_cache(self.m, True, "syn")
        self.assertTrue(res["success"], res["deviations"])
        self.assertTrue(res["data_source"]["synthetic"])
        self.assertEqual(res["data_source"]["description"], "generated tracts")
        b, methods = self._bundle(os.path.join(self.dir, "syn"))
        self.assertIn("SYNTHETIC", b["citation"])
        self.assertNotIn(REAL_CITATION, methods)
        self.assertIn("Synthetic data generator: seed 7", methods)

    def test_real_source_labels_census(self):
        res = self._run_with_cache(self.m, False, "real")
        self.assertTrue(res["success"], res["deviations"])
        self.assertFalse(res["data_source"]["synthetic"])
        b, methods = self._bundle(os.path.join(self.dir, "real"))
        self.assertNotIn("SYNTHETIC", b["citation"])
        self.assertIn(REAL_CITATION, methods)
        # a real client (plain FakeClient is a CensusAPIClient without a marker) is labelled real too
        res = run_manifest(self.m, os.path.join(self.dir, "fake"), client=FakeClient(), use_cache=False)
        self.assertFalse(res["data_source"]["synthetic"])

    def test_declared_synthetic_real_source_is_mismatch(self):
        m = copy.deepcopy(self.m)
        m["data_source"] = {"synthetic": True, "description": "fake tracts", "generator": "g"}
        res = self._run_with_cache(m, False, "mismatch")
        kinds = [d["kind"] for d in res["deviations"]]
        self.assertIn("data_source_mismatch", kinds)
        self.assertFalse(res["success"])
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        self.assertFalse(res["data_source"]["synthetic"])
        self.assertTrue(res["data_source"]["declared_synthetic"])
        self.assertNotIn("description", res["data_source"])
        b, methods = self._bundle(os.path.join(self.dir, "mismatch"))
        self.assertNotIn("SYNTHETIC", b["citation"])
        self.assertIn(REAL_CITATION, methods)
        self.assertIn("data_source_mismatch", methods)

    def test_declared_real_synthetic_client_is_mismatch(self):
        m = copy.deepcopy(self.m)
        m["data_source"] = {"synthetic": False}
        client = FakeClient()
        client.synthetic = True
        res = run_manifest(m, os.path.join(self.dir, "m2"), client=client, use_cache=False)
        self.assertIn("data_source_mismatch", [d["kind"] for d in res["deviations"]])
        self.assertTrue(res["data_source"]["synthetic"])
        b, methods = self._bundle(os.path.join(self.dir, "m2"))
        self.assertIn("SYNTHETIC", b["citation"])
        self.assertNotIn(REAL_CITATION, methods)

    def test_client_flag_and_cache_marker(self):
        cache = QueryCache(os.path.join(self.dir, "c.db"))
        self.assertIsNone(cache.synthetic_marker())
        self.assertFalse(CensusAPIClient(api_key="", cache=cache).synthetic)
        cache.mark_synthetic("d", "g")
        self.assertTrue(CensusAPIClient(api_key="", cache=cache).synthetic)
        self.assertTrue(CensusAPIClient(api_key="", synthetic=True).synthetic)
        src, mismatch = resolve_data_source(self.m, CensusAPIClient(api_key="", cache=cache))
        self.assertTrue(src["synthetic"])
        self.assertIsNone(mismatch)
        self.assertEqual(src["generator"], "g")


class TestFailedCallsBlocking(_Offline):
    """Item 2: units missing from the registered geography block confirmatory status."""

    def test_failed_calls_make_run_incomplete(self):
        import census.pipeline as pipeline
        real = pipeline.run_batch

        def run_batch(*a, **kw):
            res = real(*a, **kw)
            res["failed_calls"] = [{"entity": "tract 999999", "year": 2019, "error": "HTTP 500"}]
            return res

        with mock.patch.object(pipeline, "run_batch", run_batch):
            res = run_manifest(self.m, self.dir, client=FakeClient(), use_cache=False)
        self.assertIn("failed_calls", [d["kind"] for d in res["deviations"]])
        self.assertFalse(res["success"])
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")


def _load_script():
    spec = importlib.util.spec_from_file_location("inference_validation_r12", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestValidationScript(unittest.TestCase):
    """Items 3/4: spatial-only --gate does not crash; --studies needs names."""

    def setUp(self):
        self.iv = _load_script()

    def _main(self, argv, run_study):
        with mock.patch.object(self.iv, "run_study", side_effect=run_study), \
                mock.patch.object(sys, "argv", ["inference_validation.py", "--fast", *argv]), \
                mock.patch("builtins.print"):
            return self.iv.main()

    def test_run_study_with_nothing_to_run_returns_empty_frame(self):
        from census.simulation import FAST, run_study
        df = run_study(FAST, studies=["spatial_fay_herriot"])  # 0 reps in the main configuration
        self.assertEqual(len(df), 0)
        self.assertIn("status", df.columns)

    def test_gate_spatial_only(self):
        calls = []

        def run_study(cfg, studies=None, log=None):
            calls.append(studies)
            return pd.DataFrame([{"study": "spatial_fay_herriot", "method": "m", "target": "t", "reps": 10,
                                  "nominal": 0.9, "coverage": 0.9, "mc_se": 0.01, "status": "pass"}])

        self.assertEqual(self._main(["--gate", "--studies", "spatial_fay_herriot"], run_study), 0)
        self.assertEqual(calls, [["spatial_fay_herriot"]])  # the main configuration is not run

    def test_gate_spatial_only_empty_results_fail_cleanly(self):
        empty = lambda cfg, studies=None, log=None: pd.DataFrame(  # noqa: E731
            columns=["study", "method", "target", "reps", "nominal", "coverage", "mc_se", "status"])
        self.assertEqual(self._main(["--gate", "--studies", "spatial_fay_herriot"], empty), 1)

    def test_studies_without_names_rejected(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            self._main(["--gate", "--studies"], lambda *a, **k: None)
        self.assertEqual(cm.exception.code, 2)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            self._main(["--studies", "no_such_study"], lambda *a, **k: None)


class TestReplicatesHash(unittest.TestCase):
    """Item 5 (and CI failure B): never raises; dtype- and version-independent."""

    def _frame(self):
        return pd.DataFrame({"GEOID": ["01", "02"], "variable": ["y", "y"], "Year": [2019, 2019],
                             "rep_1": [1.5, np.nan], "rep_2": [3, 4]})

    def test_golden_value_stable_across_versions(self):
        # fixed expected digest: must not change with pandas 2.x/3.x or numpy 1.x/2.x
        self.assertEqual(replicates_sha256(self._frame()),
                         "383534ed8a2de0852352052e412c359c9d9fc2d66a04c1e06a21f92f35199ef2")

    def test_int_and_float_hash_alike_order_free(self):
        a = self._frame()
        b = a.copy()
        b["rep_2"] = b["rep_2"].astype(float)
        b["Year"] = b["Year"].astype(float)
        self.assertEqual(replicates_sha256(a), replicates_sha256(b))
        self.assertEqual(replicates_sha256(a), replicates_sha256(a.iloc[::-1][list(reversed(a.columns))]))
        self.assertNotEqual(replicates_sha256(a), replicates_sha256(a.assign(rep_2=[3, 5])))

    def test_never_raises(self):
        clash = pd.DataFrame([[1, 2]], columns=[1, "1"])
        digest, reason = replicates_digest(clash)
        self.assertIsNone(digest)
        self.assertIn("collide", reason)
        mixed = pd.DataFrame({"x": [1, "a", None, 2.5], "y": [{"k": 1}, [1], (2,), object()]})
        self.assertIsNotNone(replicates_digest(mixed[["x"]])[0])
        replicates_digest(mixed)  # unsortable / odd objects: no exception
        self.assertIsNone(replicates_digest(_Unframeable())[0])

    def test_csv_round_trip_is_exact(self):
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"GEOID": ["36061000100", "36061000200"] * 5, "variable": "income",
                           "rep_1": rng.normal(50000, 5000, 10) / 3.0})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.csv")
            df.to_csv(path, index=False)
            back = read_replicates(path)
        self.assertEqual(back["GEOID"].iloc[0], "36061000100")
        self.assertEqual(replicates_sha256(back), replicates_sha256(df))

    def test_run_records_unavailable_hash(self):
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch("census.manifest.replicates_digest", return_value=(None, "boom")):
                reps = pd.DataFrame({"GEOID": [], "variable": []})
                res = run_manifest(load_manifest(EXAMPLE), tmp, client=FakeClient(), use_cache=False,
                                   replicates=reps)
            self.assertIsNone(res["replicates"]["sha256"])
            self.assertEqual(res["replicates"]["sha256_unavailable"], "boom")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _Unframeable:
    def __iter__(self):
        raise RuntimeError("cannot iterate")


class TestRunManifestAdapter(_Offline):
    """Item 6: the CLI/MCP adapter reports status, source, replicates; accepts replicates_path."""

    def _manifest_requiring_replicates(self):
        m = copy.deepcopy(self.m)
        m["analyses"][0]["replicates"] = "required"
        path = os.path.join(self.dir, "m.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f)
        return path

    def _replicates_csv(self):
        from census.simulation import make_replicates, replicate_long
        frame = run_manifest(self.m, os.path.join(self.dir, "probe"), client=FakeClient(), use_cache=False)["frame"]
        est = frame[["income", "pop"]].to_numpy(float)
        se = frame[["income_se", "pop_se"]].to_numpy(float)
        reps = replicate_long(list(frame["GEOID"]), ["income", "pop"], est,
                              make_replicates(est, se, np.random.default_rng(1)))
        reps["Year"] = 2019
        path = os.path.join(self.dir, "reps.csv")
        reps.to_csv(path, index=False)
        return path

    def test_blocked_for_missing_replicates_then_supplied(self):
        from census import workflows_inference as w
        path = self._manifest_requiring_replicates()
        cache_path = os.path.join(self.dir, "cache.db")
        _frozen_cache(cache_path, synthetic=True)
        with mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": cache_path}):
            out = w.run_manifest(path, os.path.join(self.dir, "a"))
            self.assertFalse(out["success"])
            self.assertEqual(out["analysis_status"], "confirmatory_incomplete")
            self.assertIn("replicates_unavailable", [d["kind"] for d in out["deviations"]])
            self.assertTrue(out["data_source"]["synthetic"])
            self.assertIsNone(out["replicates"])
            self.assertIn("replicates_path", out["next_step"])
        reps_path = self._replicates_csv()
        with mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": cache_path}):
            out = w.run_manifest(path, os.path.join(self.dir, "b"), replicates_path=reps_path)
        self.assertTrue(out["success"], out.get("deviations"))
        self.assertEqual(out["analysis_status"], "confirmatory")
        self.assertEqual(out["replicates"]["source"], "caller_supplied")
        self.assertTrue(out["replicates"]["synthetic"])
        self.assertEqual(out["replicates"]["sha256"], replicates_sha256(read_replicates(reps_path)))

    def test_unreadable_replicates_path(self):
        from census import workflows_inference as w
        out = w.run_manifest(EXAMPLE, os.path.join(self.dir, "c"), replicates_path=os.path.join(self.dir, "nope.csv"))
        self.assertFalse(out["success"])
        self.assertIn("replicates_path", out["error"])


class TestCliArgPortable(unittest.TestCase):
    """CI failure A: Windows paths passed without JSON escaping."""

    def test_arg_flags_keep_backslashes(self):
        from census import workflows_inference as w
        from census.__main__ import main
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return {"success": True}

        win = r"C:\Users\runner\AppData\Local\Temp\tmpab12\cli_run"
        with mock.patch.object(w, "run_manifest", fake), contextlib.redirect_stdout(io.StringIO()):
            code = main(["tool", "run_manifest", "--arg", "manifest_path=examples/m.json",
                         "--arg", f"output_dir={win}", "--arg", "replicates_synthetic=true", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"manifest_path": "examples/m.json", "output_dir": win, "replicates_synthetic": True})

    def test_bad_arg_flag(self):
        from census.__main__ import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["tool", "run_manifest", "--arg", "novalue", "--json"])
        self.assertNotEqual(code, 0)
        self.assertIn("KEY=VALUE", buf.getvalue())

    def test_reference_doc_uses_portable_cli_arguments(self):
        with open(os.path.join(ROOT, "docs", "reference_workflow.md"), encoding="utf-8") as f:
            doc = f.read()
        self.assertNotIn('\\"output_dir\\": \\"$RW', doc)
        self.assertIn('--arg "output_dir=$RW/run1"', doc)


def _result(model_type="ols", method="replicate_refit", target="population", incomplete=False):
    coefs = pd.DataFrame({"term": ["Intercept", "x"], "estimate": [1.0, 2.0], "se_model": [0.1, 0.2],
                          "se_sampling": [0.3, np.nan], "se_total": [0.4, 0.5], "ci_lower": [0.8, 1.6],
                          "ci_upper": [1.2, 2.4], "p_value": [0.01, 0.02]})
    diag = {"interval_target": target}
    notes = []
    if incomplete:
        diag["replicates_incomplete"] = {"note": "sampling SE may be understated (x)"}
        notes = ["REPLICATES INCOMPLETE: sampling SE may be understated (x)", "other note"]
    return ModelResult(model_type=model_type, formula="y ~ x", n_obs=10, coefficients=coefs, method=method,
                       uncertainty_sources=["model", "sampling_replicate"], notes=notes, diagnostics=diag)


class TestEstimandAndIntervals(unittest.TestCase):
    """Items 7-10."""

    def test_method_aware_estimand(self):
        area = interval_description(_result())["estimand"]
        self.assertIn("among areas", area)
        self.assertIn("not a causal effect", area)
        pums = interval_description(_result("pums_ols", "pums_replicate", "these_areas"))["estimand"]
        self.assertIn("surveyed population (persons or housing units)", pums)
        self.assertIn("weighted", pums)
        self.assertNotIn("among areas", pums)
        did = interval_description(_result("did_twfe", "did_replicate"))["estimand"]
        self.assertIn("difference-in-differences contrast", did)
        self.assertIn("causal interpretation requires the DiD identifying assumptions", did)

    def test_replicates_incomplete_once(self):
        from census.report_tables import regression_table
        from census.workflows_inference import result_summary
        r = _result(incomplete=True)
        self.assertEqual(r.summary().count("REPLICATES INCOMPLETE"), 1)
        self.assertEqual(result_summary(r).count("REPLICATES INCOMPLETE"), 1)
        notes = " ".join(regression_table(r, correction=None).notes)
        self.assertEqual(notes.count("REPLICATES INCOMPLETE"), 1)
        self.assertIn("other note", notes)

    def test_interval_description_computed_once(self):
        from census import report_tables, results
        r = _result()
        with mock.patch.object(results, "interval_description", wraps=results.interval_description) as spy:
            r.to_dict()
        self.assertEqual(spy.call_count, 1)
        with mock.patch.object(report_tables, "interval_description",
                               wraps=report_tables.interval_description) as spy:
            report_tables.regression_table({"a": r, "b": _result()}, correction=None)
        self.assertEqual(spy.call_count, 2)

    def test_interval_row(self):
        r = _result(target="these_areas")
        info = interval_description(r)
        row = r.coefficients.set_index("term").loc["x"]
        out = interval_row(info, "x", row)
        self.assertEqual(out["se_basis"], "se_sampling")
        self.assertTrue(np.isnan(out["se_reported"]))
        self.assertFalse(out["interval_defined"])
        self.assertEqual(out["distribution"], "normal")
        self.assertTrue(np.isnan(out["df"]))
        self.assertTrue(np.isnan(interval_row(info, "Intercept")["se_reported"]))
        self.assertEqual(interval_row(info, "Intercept", r.coefficients.iloc[0])["se_reported"], 0.3)


if __name__ == "__main__":
    unittest.main()
