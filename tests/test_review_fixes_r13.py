# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the Round 13 code-review findings (branch round13/review-fixes)."""

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from census import errors, rpp
from census.cache import QueryCache

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "reference_workflow", "frozen_api_responses.json")
PARITY_DIR = os.path.join(ROOT, "data", "price_parities")
INDEX_DIR = os.path.join(ROOT, "data", "price_indexes")
REP_COLS = [f"rep_{i}" for i in range(1, 81)]
NO_CUSTOM_DIRS = {"CENSUS_PRICE_INDEX_DIR": "", "CENSUS_PRICE_PARITY_DIR": ""}


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, *parts):
        p = os.path.join(self.dir, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def write_json(self, doc, *parts):
        p = self.path(*parts)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return p


def _cli(*argv, env=None, remove=()):
    from census.__main__ import main

    buf = io.StringIO()
    with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(buf):
        for key in remove:
            os.environ.pop(key, None)
        code = main(list(argv))
    return code, json.loads(buf.getvalue())


def _state_frame(directory, reps):
    """A one-state frame (areas='state', no boundaries needed) and a replicate file."""
    frame = pd.DataFrame({"GEOID": ["26001", "26003"], "Year": 2022, "income": [50000.0, 60000.0],
                          "income_se": [500.0, 600.0]})
    fpath = os.path.join(directory, "frame.parquet")
    frame.to_parquet(fpath, index=False)
    rpath = os.path.join(directory, "reps.csv")
    reps.to_csv(rpath, index=False)
    return fpath, rpath


# Finding 1 ---------------------------------------------------------------------------------
class TestPlaceAdjustIntegerReplicates(_TmpDir):
    def test_whole_number_replicate_csv(self):
        """read_replicates of a whole-number CSV gives int64 columns; scaling must not raise."""
        from census.workflows_microdata import place_adjust_frame

        reps = pd.DataFrame({"GEOID": ["26001", "26003"], "Year": 2022, "variable": "income",
                             "estimate": [50000, 60000]})
        for i, c in enumerate(REP_COLS):
            reps[c] = reps["estimate"] + (i % 3 - 1) * 10
        fpath, rpath = _state_frame(self.dir, reps)
        res = place_adjust_frame(fpath, ["income"], 2022, areas="state", replicates_path=rpath,
                                 output_path=os.path.join(self.dir, "out.parquet"),
                                 replicates_output_path=os.path.join(self.dir, "reps_out.parquet"))
        self.assertTrue(res["success"], res)
        factor = 100 / rpp.place_parity("26", 2022)["rpp"]
        out = pd.read_parquet(res["replicates_output_path"]).sort_values("GEOID")
        np.testing.assert_allclose(out["estimate"].to_numpy(float), [50000 * factor, 60000 * factor])
        np.testing.assert_allclose(out["rep_1"].to_numpy(float), [(50000 - 10) * factor, (60000 - 10) * factor])


# Finding 4 ---------------------------------------------------------------------------------
class TestPlaceAdjustProvenance(_TmpDir):
    def setUp(self):
        super().setUp()
        rpp.clear_cache()
        self.addCleanup(rpp.clear_cache)

    def test_custom_rpp_table_recorded(self):
        from census.workflows_microdata import place_adjust_frame

        custom = os.path.join(self.dir, "parities")
        shutil.copytree(PARITY_DIR, custom, ignore=shutil.ignore_patterns("source"))
        frame = pd.DataFrame({"GEOID": ["26001"], "income": [50000.0], "income_se": [500.0]})
        fpath = os.path.join(self.dir, "f.parquet")
        frame.to_parquet(fpath, index=False)
        with mock.patch.dict(os.environ, {"CENSUS_PRICE_PARITY_DIR": custom}):
            rpp.clear_cache()
            res = place_adjust_frame(fpath, ["income"], 2022, areas="state",
                                     output_path=os.path.join(self.dir, "o.parquet"))
        self.assertTrue(res["success"], res)
        table = res["custom_table"]
        self.assertFalse(table["verified"])
        self.assertEqual(os.path.normcase(table["path"]), os.path.normcase(os.path.join(custom, "rpp.csv")))
        self.assertEqual(len(table["sha256"]), 64)
        self.assertTrue(any(n.startswith("CUSTOM RPP TABLE") and table["sha256"] in n for n in res["notes"]))
        self.assertEqual(res["advisories"], [])

    def test_bundled_table_has_no_custom_record(self):
        from census.workflows_microdata import place_adjust_frame

        frame = pd.DataFrame({"GEOID": ["26001"], "income": [50000.0], "income_se": [500.0]})
        fpath = os.path.join(self.dir, "f.parquet")
        frame.to_parquet(fpath, index=False)
        with mock.patch.dict(os.environ, NO_CUSTOM_DIRS):
            rpp.clear_cache()
            res = place_adjust_frame(fpath, ["income"], 2022, areas="state",
                                     output_path=os.path.join(self.dir, "o.parquet"))
        self.assertTrue(res["success"], res)
        self.assertIsNone(res["custom_table"])
        self.assertFalse(any("CUSTOM RPP TABLE" in n for n in res["notes"]))


# Finding 2, 5, 7 ---------------------------------------------------------------------------
def _meta(table, state, source=None):
    doc = {"table_code": table, "dataset": "acs5", "years": [2024], "geo_level": "county",
           "endpoint": "https://api.census.gov/data/{year}/acs/acs5", "entities": [{"state_code": state}],
           "failed_calls": []}
    if source is not None:
        doc["data_source"] = source
    return doc


REAL = {"kind": "census_api", "synthetic": False}
SYNTH = {"kind": "synthetic", "synthetic": True, "description": "generated test data"}


class TestResultsBundleProvenance(_TmpDir):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": os.path.join(self.dir, "cache.db")})
        env.start()
        self.addCleanup(env.stop)
        self.result = self.write_json({"success": True, "summary": "fit"}, "results", "fit.json")

    def bundle(self, inputs, results=None, **kw):
        from census.workflows_inference import make_results_bundle

        return make_results_bundle(os.path.join(self.dir, "bundle"), results or {"fit": self.result},
                                   inputs=inputs, **kw)

    def provenance(self):
        with open(os.path.join(self.dir, "bundle", "bundle.json"), encoding="utf-8") as f:
            return json.load(f)["provenance"]

    def test_same_named_inputs_are_distinguishable(self):
        a = self.write_json(_meta("B19013", "26", REAL), "extract", "26", "B19013", "B19013_extraction_metadata.json")
        b = self.write_json(_meta("B19013", "36", REAL), "extract", "36", "B19013", "B19013_extraction_metadata.json")
        res = self.bundle([a, b])
        self.assertTrue(res["success"], res)
        paths = [r["path"] for r in self.provenance()["inputs"]]
        self.assertEqual(paths, ["26/B19013/B19013_extraction_metadata.json",
                                 "36/B19013/B19013_extraction_metadata.json"])
        self.assertTrue(all(len(r["sha256"]) == 64 for r in self.provenance()["inputs"]))
        self.assertNotIn(self.dir, json.dumps(self.provenance()))

    def test_unrecorded_source_is_unknown_not_the_current_cache(self):
        QueryCache(os.environ["CENSUS_CACHE_PATH"]).mark_synthetic(description="unrelated cache")
        meta = self.write_json(_meta("B19013", "26"), "B19013_extraction_metadata.json")
        res = self.bundle([meta])
        self.assertTrue(res["success"], res)
        self.assertEqual(res["data_source"]["kind"], "unknown")
        self.assertIsNone(res["data_source"]["synthetic"])
        self.assertNotIn("Retrieved via the Census Data API", res["citation"])
        self.assertNotIn("unrelated cache", json.dumps(res))

    def test_recorded_sources(self):
        real = self.write_json(_meta("B19013", "26", REAL), "real", "B19013_extraction_metadata.json")
        res = self.bundle([real])
        self.assertEqual(res["data_source"], {"kind": "census_api", "synthetic": False})
        self.assertIn("Retrieved via the Census Data API", res["citation"])
        synth = self.write_json(_meta("B19013", "26", SYNTH), "synth", "B19013_extraction_metadata.json")
        from census.workflows_inference import make_results_bundle
        res = make_results_bundle(os.path.join(self.dir, "bundle_synthetic"), {"fit": self.result}, inputs=[synth])
        self.assertEqual(res["data_source"]["kind"], "synthetic")
        self.assertEqual(res["data_source"]["description"], "generated test data")
        self.assertNotIn("Retrieved via the Census Data API", res["citation"])
        # a saved run_manifest result also carries its observed source
        run = self.write_json({"success": True, "summary": "run", "data_source": {"synthetic": True}}, "run.json")
        res = make_results_bundle(os.path.join(self.dir, "bundle_run"), {"run": run})
        self.assertEqual(res["data_source"]["kind"], "synthetic")

    def test_mixed_sources_warn(self):
        real = self.write_json(_meta("B19013", "26", REAL), "real", "B19013_extraction_metadata.json")
        synth = self.write_json(_meta("B19013", "36", SYNTH), "synth", "B19013_extraction_metadata.json")
        res = self.bundle([real, synth])
        self.assertEqual(res["data_source"]["kind"], "mixed")
        self.assertTrue(res["warnings"] and "MIXED DATA SOURCES" in res["warnings"][0])
        self.assertIn("synth/B19013_extraction_metadata.json", res["warnings"][0])
        self.assertNotIn("Retrieved via the Census Data API", res["citation"])
        self.assertEqual(self.provenance()["warnings"], res["warnings"])
        with open(os.path.join(self.dir, "bundle", "methods.md"), encoding="utf-8") as f:
            self.assertIn("MIXED DATA SOURCES", f.read())

    def test_failed_result_refused(self):
        failed = self.write_json({"success": False, "error_code": "INVALID_INPUT", "error": "bad formula",
                                  "summary": "bad formula"}, "results", "model.json")
        res = self.bundle([], results={"fit": self.result, "model": failed})
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], errors.INVALID_INPUT)
        self.assertIn("model.json", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "bundle", "bundle.json")))
        res = self.bundle([], results={"fit": self.result, "model": failed}, allow_failed_results=True)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["results"], ["fit"])
        with open(os.path.join(self.dir, "bundle", "methods.md"), encoding="utf-8") as f:
            methods = f.read()
        self.assertNotIn("## model", methods)
        self.assertIn("FAILED", methods)
        self.assertIn("bad formula", methods)
        self.assertEqual(self.provenance()["failed_results"][0]["name"], "model")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "bundle", "results", "model.json")))


class TestExtractionRecordsDataSource(_TmpDir):
    def run_with(self, cache):
        from census.pipeline import ResearchOptions, build_entities, run_batch
        from tests.test_pipeline_national import fake_client

        out = os.path.join(self.dir, "out")
        res = run_batch("B19013", build_entities("county", "all"), [2022], out, "panel",
                        ResearchOptions(continuity_check=False), client=fake_client(cache))
        self.assertTrue(res["success"], res)
        with open(res["metadata_path"], encoding="utf-8") as f:
            return json.load(f)["data_source"]

    def test_unmarked_and_marked_cache(self):
        cache = QueryCache(os.path.join(self.dir, "cache.db"))
        self.assertEqual(self.run_with(cache), {"kind": "census_api", "synthetic": False})
        cache.mark_synthetic(description="generated", generator="test")
        self.assertEqual(self.run_with(cache), {"kind": "synthetic", "synthetic": True,
                                                "description": "generated", "generator": "test"})


# Finding 3 ---------------------------------------------------------------------------------
class TestCacheImportSafety(_TmpDir):
    def env(self):
        return {"CENSUS_CACHE_PATH": os.path.join(self.dir, "cache.db")}

    def test_clear_removes_synthetic_marker(self):
        code, out = _cli("cache", "import", FIXTURE, "--json", env=self.env())
        self.assertEqual(code, 0, out)
        self.assertTrue(out["synthetic"])
        code, out = _cli("cache", "clear", "--json", env=self.env())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["cleared"], 2)
        self.assertTrue(out["synthetic_marker_cleared"])
        self.assertIsNone(QueryCache(self.env()["CENSUS_CACHE_PATH"]).synthetic_marker())
        code, out = _cli("cache", "clear", "--json", env=self.env())
        self.assertFalse(out["synthetic_marker_cleared"])

    def test_import_is_atomic(self):
        src = os.path.join(self.dir, "src")
        os.makedirs(src)
        shutil.copyfile(FIXTURE, os.path.join(src, "a_good.json"))
        with open(os.path.join(src, "b_bad.json"), "w", encoding="utf-8") as f:
            json.dump({"request": {"year": 2019, "colour": "red"}, "requests": {"B19013": [["GEO_ID"]]}}, f)
        code, out = _cli("cache", "import", src, "--json", env=self.env())
        self.assertNotEqual(code, 0)
        self.assertIn("b_bad.json", out["error"])
        cache = QueryCache(self.env()["CENSUS_CACHE_PATH"])
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertIsNone(cache.synthetic_marker())

    def test_synthetic_into_default_cache_needs_flag(self):
        from census import __main__ as cli

        default = QueryCache(os.path.join(self.dir, "default.db"))
        with mock.patch.object(cli, "get_default_cache", return_value=default):
            code, out = _cli("cache", "import", FIXTURE, "--json", remove=("CENSUS_CACHE_PATH",))
            self.assertNotEqual(code, 0)
            self.assertIn("CENSUS_CACHE_PATH", out["error"])
            self.assertIn("--allow-default-cache", out["error"])
            self.assertEqual(default.stats()["entries"], 0)
            self.assertIsNone(default.synthetic_marker())
            # real (unmarked) responses need no flag
            with open(FIXTURE, encoding="utf-8") as f:
                doc = json.load(f)
            del doc["synthetic"]
            real = self.write_json(doc, "real.json")
            code, out = _cli("cache", "import", real, "--json", remove=("CENSUS_CACHE_PATH",))
            self.assertEqual(code, 0, out)
            self.assertFalse(out["synthetic"])
            code, out = _cli("cache", "import", FIXTURE, "--allow-default-cache", "--json",
                             remove=("CENSUS_CACHE_PATH",))
            self.assertEqual(code, 0, out)
            self.assertTrue(default.synthetic_marker())


# Finding 6 ---------------------------------------------------------------------------------
class TestLoadFramesMissingKeys(_TmpDir):
    def write(self, name, df):
        p = os.path.join(self.dir, name)
        df.to_csv(p, index=False)
        return p

    def test_missing_year_column_in_one_file(self):
        from census.workflows_inference import build_analysis_frame

        a = self.write("a.csv", pd.DataFrame({"GEOID": ["26001"], "Year": 2024, "B19013_001E": [1],
                                              "B19013_001M": [1]}))
        b = self.write("b.csv", pd.DataFrame({"GEOID": ["26001"], "B15002_001E": [2], "B15002_001M": [1]}))
        res = build_analysis_frame([a, b], output_path=os.path.join(self.dir, "f.parquet"))
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], errors.INVALID_INPUT)
        self.assertIn("Year", res["error"])
        self.assertIn("b.csv", res["error"])

    def test_missing_geoid_values(self):
        from census.workflows_inference import build_analysis_frame

        a = self.write("a.csv", pd.DataFrame({"GEOID": ["26001", None], "Year": 2024, "B19013_001E": [1, 3],
                                              "B19013_001M": [1, 1]}))
        b = self.write("b.csv", pd.DataFrame({"GEOID": ["26001"], "Year": 2024, "B15002_001E": [2],
                                              "B15002_001M": [1]}))
        res = build_analysis_frame([a, b], output_path=os.path.join(self.dir, "f.parquet"))
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], errors.INVALID_INPUT)
        self.assertIn("GEOID", res["error"])
        self.assertIn("a.csv", res["error"])


# Finding 8 ---------------------------------------------------------------------------------
def _tampered_copy(src, dst, name):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("source"))
    path = os.path.join(dst, name)
    with open(path, "ab") as f:
        f.write(b"\n")
    return dst


class TestIntegrityErrorsAreStructured(_TmpDir):
    def setUp(self):
        super().setUp()
        from census.inflation import load_price_index

        env = mock.patch.dict(os.environ, NO_CUSTOM_DIRS)
        env.start()
        self.addCleanup(env.stop)
        for clear in (load_price_index.cache_clear, rpp.clear_cache):
            clear()
            self.addCleanup(clear)

    def assert_integrity(self, res, name):
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], errors.BUNDLED_DATA_INTEGRITY)
        self.assertIn(name, res["error"])
        self.assertIn("git checkout", res["next_step"])

    def test_adjust_inflation_tool(self):
        try:
            import mcp_server
        except ImportError:  # pragma: no cover - MCP extra not installed
            self.skipTest("mcp not installed")
        from census import inflation

        copy = _tampered_copy(INDEX_DIR, os.path.join(self.dir, "idx"), "r_cpi_u_rs.csv")
        with mock.patch.object(inflation, "bundled_price_index_dir", return_value=copy):
            self.assert_integrity(mcp_server.adjust_inflation(100, 2010, 2020), "r_cpi_u_rs.csv")

    def test_extraction_plan(self):
        from census import inflation
        from census.pipeline import ResearchOptions, _price_index_error

        copy = _tampered_copy(INDEX_DIR, os.path.join(self.dir, "idx"), "r_cpi_u_rs.csv")
        with mock.patch.object(inflation, "bundled_price_index_dir", return_value=copy):
            res = _price_index_error(ResearchOptions(inflation_base_year=2022), [2020])
        self.assert_integrity(res, "r_cpi_u_rs.csv")

    def test_place_tools(self):
        from census import workflows_microdata

        copy = _tampered_copy(PARITY_DIR, os.path.join(self.dir, "rpp"), "rpp.csv")
        frame = pd.DataFrame({"GEOID": ["26001"], "income": [50000.0], "income_se": [500.0]})
        fpath = os.path.join(self.dir, "f.parquet")
        frame.to_parquet(fpath, index=False)
        with mock.patch.object(rpp, "bundled_price_parity_dir", return_value=copy):
            self.assert_integrity(workflows_microdata.adjust_for_place(50000, "26", 2022), "rpp.csv")
            rpp.clear_cache()
            self.assert_integrity(workflows_microdata.place_adjust_frame(fpath, ["income"], 2022, areas="state"),
                                  "rpp.csv")


# Finding 9 ---------------------------------------------------------------------------------
class TestLegendTitleWrap(unittest.TestCase):
    def test_identifiers_not_split_mid_token(self):
        from census.maps import LEGEND_TITLE_WIDTH, wrap_legend_title

        label = "median_hh_income_rpp_adjusted (2024 dollars, county-level estimates)"
        wrapped = wrap_legend_title(label)
        lines = wrapped.split("\n")
        self.assertGreater(len(lines), 1)
        tokens = set(re.split(r"[_\s-]+", label))
        for line in lines:
            for piece in re.split(r"[_\s-]+", line):
                if piece:
                    self.assertIn(piece, tokens, f"{piece!r} is a fragment of a token ({lines})")
        self.assertEqual(re.sub(r"\s+", "", wrapped), re.sub(r"\s+", "", label))
        long_id = "a_very_long_identifier_without_any_spaces_at_all"
        for line in wrap_legend_title(long_id).split("\n"):
            self.assertLessEqual(len(line), LEGEND_TITLE_WIDTH)
        self.assertEqual(wrap_legend_title("x" * 40), "x" * 40)  # one unbreakable token: kept whole


if __name__ == "__main__":
    unittest.main()
