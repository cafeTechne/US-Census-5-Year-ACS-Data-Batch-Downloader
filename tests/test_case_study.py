# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline checks for docs/case_study_mi_ny.md (PLAN_ROUND_13 E1) and the
toolkit routes it added: multi-file analysis frames, ``place_adjust_frame``,
``make_results_bundle`` and ``tool --save``.

The document itself runs on live Census data; executing it is the opt-in live
test tests/test_case_study_live.py (CENSUS_LIVE_TESTS=1). Here only its form
is checked: CLI-only, and every command parses with valid tool arguments.
"""

import contextlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOC = os.path.join(ROOT, "docs", "case_study_mi_ny.md")
REP_COLS = [f"rep_{i}" for i in range(1, 81)]


def bash_blocks(path=DOC):
    with open(path, encoding="utf-8") as f:
        return re.findall(r"```bash\n(.*?)```", f.read(), flags=re.S)


def commands(path=DOC):
    return [ln.strip() for block in bash_blocks(path) for ln in block.splitlines() if ln.strip()]


def run_document(workdir):
    """Execute the document's commands with ``workdir`` as the working directory
    (outputs land in workdir/case_study_output). Returns (command name, JSON output)."""
    from census.__main__ import main

    # the confirmatory step reads examples/manifests/ relative to the working directory
    manifests = os.path.join(workdir, "examples", "manifests")
    if not os.path.exists(manifests):
        shutil.copytree(os.path.join(ROOT, "examples", "manifests"), manifests)
    outputs = []
    cwd = os.getcwd()
    os.chdir(workdir)
    try:
        for line in commands():
            argv = shlex.split(line)[3:]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                code = main(argv)
            out = json.loads(buf.getvalue())
            if code != 0 or not out.get("success", True):
                raise AssertionError(f"{line[:120]} -> exit {code}: {out.get('error') or out.get('summary')}")
            outputs.append((argv[1] if argv[0] == "tool" else argv[0], out))
    finally:
        os.chdir(cwd)
    return outputs


def _tool(name):
    from census import workflows, workflows_geo, workflows_inference, workflows_microdata

    for mod in (workflows_microdata, workflows_geo, workflows_inference, workflows):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


class TestCaseStudyDocument(unittest.TestCase):
    def test_document_is_cli_only(self):
        """Like the reference workflow: no Python steps, only `python -m census` commands."""
        with open(DOC, encoding="utf-8") as f:
            doc = f.read()
        self.assertNotIn("python - <<", doc)
        self.assertNotIn("```python", doc)
        lines = commands()
        self.assertGreater(len(lines), 15)
        for line in lines:
            self.assertTrue(line.startswith("python -m census "), line)

    def test_commands_parse_with_valid_arguments(self):
        from census.__main__ import build_parser

        parser = build_parser()
        seen = set()
        for line in commands():
            argv = shlex.split(line)[3:]
            args = parser.parse_args(argv)
            seen.add(args.command)
            if args.command != "tool":
                continue
            fn = _tool(args.name)
            self.assertIsNotNone(fn, args.name)
            params = inspect.signature(fn).parameters
            for item in args.arg or []:
                key, _, raw = item.partition("=")
                self.assertIn(key, params, f"{args.name}: {key}")
                if raw[:1] in "[{":
                    json.loads(raw)  # JSON arguments must be valid JSON
        self.assertTrue({"extract", "tool", "replay"} <= seen)

    def test_states_are_arguments_only(self):
        """D13-2: no state-specific code; Michigan and New York appear only as arguments."""
        for name in ("workflows_microdata.py", "workflows_inference.py", "boundaries.py", "rpp.py"):
            with open(os.path.join(ROOT, "census", name), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("Michigan", src, name)


class TestMultiFileFrame(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, df):
        path = os.path.join(self.dir, name)
        df.to_csv(path, index=False)
        return path

    def test_tables_joined_and_states_stacked(self):
        from census.workflows_inference import build_analysis_frame

        a1 = self.write("a1.csv", pd.DataFrame({"GEOID": ["26001", "26003"], "Year": 2024,
                                                "B19013_001E": [50000, 60000], "B19013_001M": [1000, 1200]}))
        a2 = self.write("a2.csv", pd.DataFrame({"GEOID": ["36001"], "Year": 2024,
                                                "B19013_001E": [70000], "B19013_001M": [1500]}))
        b = self.write("b.csv", pd.DataFrame({"GEOID": ["26001", "26003", "36001"], "Year": 2024,
                                              "B15002_001E": [1000, 2000, 3000], "B15002_001M": [10, 20, 30],
                                              "B15002_015E": [100, 500, 900], "B15002_015M": [5, 9, 12]}))
        out = os.path.join(self.dir, "frame.parquet")
        res = build_analysis_frame([a1, a2, b], variables={"income": "B19013_001"},
                                   derive=[{"name": "pct", "op": "proportion", "num": "B15002_015E",
                                            "den": "B15002_001E", "as_percent": True}], output_path=out)
        self.assertTrue(res["success"], res)
        frame = pd.read_parquet(out).set_index("GEOID")
        self.assertEqual(sorted(frame.index), ["26001", "26003", "36001"])
        self.assertEqual(frame.loc["36001", "income"], 70000)
        self.assertAlmostEqual(frame.loc["26003", "pct"], 25.0)

    def test_conflicting_cells_are_an_error(self):
        from census.workflows_inference import build_analysis_frame

        a = self.write("a.csv", pd.DataFrame({"GEOID": ["26001"], "B19013_001E": [1], "B19013_001M": [1]}))
        b = self.write("b.csv", pd.DataFrame({"GEOID": ["26001"], "B19013_001E": [2], "B19013_001M": [1]}))
        res = build_analysis_frame([a, b], output_path=os.path.join(self.dir, "f.parquet"))
        self.assertFalse(res["success"])
        self.assertIn("disagree", res["error"])

    def test_replicates_from_several_tables(self):
        from census import workflows_inference

        frames = {"T1": pd.DataFrame({"GEOID": ["26001"], "variable": ["X"]}),
                  "T2": pd.DataFrame({"GEOID": ["26001"], "variable": ["Y"]})}
        path = self.write("a.csv", pd.DataFrame({"GEOID": ["26001"], "Year": 2024, "B1_001E": [1], "B1_001M": [1]}))
        calls = []
        with mock.patch.object(workflows_inference, "_replicates_for",
                               side_effect=lambda t, *a: calls.append(t) or frames[t]), \
                mock.patch("census.analysis.attach_replicates", side_effect=ValueError("stop")):
            workflows_inference.build_analysis_frame(path, use_replicates=True, table_code=["T1", "T2"], year=2024,
                                                     output_path=os.path.join(self.dir, "f.parquet"))
        self.assertEqual(calls, ["T1", "T2"])


class TestPlaceAdjustFrame(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_county_rpp_area(self):
        from census.rpp import county_rpp_area

        self.assertEqual(county_rpp_area("26163", "19820", "M1"), "19820")
        self.assertEqual(county_rpp_area("26003", "26340", "M2"), "26999")  # micropolitan -> nonmetro
        self.assertEqual(county_rpp_area("26001"), "26999")
        with self.assertRaises(ValueError):
            county_rpp_area("2616")

    def test_county_cbsa_membership(self):
        import geopandas as gpd
        from shapely.geometry import box

        from census.boundaries import county_cbsa_membership

        counties = gpd.GeoDataFrame({"GEOID": ["26001", "26003", "26005"]},
                                    geometry=[box(-85, 43, -84.9, 43.1), box(-84.9, 43, -84.8, 43.1),
                                              box(-84, 45, -83.9, 45.1)], crs="EPSG:4269")
        cbsas = gpd.GeoDataFrame({"GEOID": ["11111", "22222"], "NAME": ["Metro", "Micro"], "LSAD": ["M1", "M2"]},
                                 geometry=[box(-85.0, 43, -84.8, 43.1), box(-84.0, 45, -83.96, 45.1)],
                                 crs="EPSG:4269")
        out = county_cbsa_membership(counties, cbsas).set_index("GEOID")
        self.assertEqual(out.loc["26001", "CBSA"], "11111")
        self.assertEqual(out.loc["26003", "LSAD"], "M1")
        self.assertTrue(pd.isna(out.loc["26005", "CBSA"]))  # 40% covered: below min_share
        self.assertAlmostEqual(out.loc["26001", "share"], 1.0, places=3)

    def test_adjusts_estimates_ses_and_replicates(self):
        from census import rpp, workflows_microdata

        frame = pd.DataFrame({"GEOID": ["26163", "26001"], "Year": 2022, "income": [60000.0, 50000.0],
                              "income_se": [600.0, 1000.0], "income_cv": [1.0, 2.0], "pct": [30.0, 20.0],
                              "pct_se": [1.0, 1.0]})
        fpath = os.path.join(self.dir, "frame.parquet")
        frame.to_parquet(fpath, index=False)
        reps = pd.concat([pd.DataFrame({"GEOID": frame["GEOID"], "variable": v, "estimate": frame[v]})
                          for v in ("income", "pct")], ignore_index=True)
        for i, c in enumerate(REP_COLS):
            reps[c] = reps["estimate"] * (1 + 0.001 * (i % 3 - 1))
        rpath = os.path.join(self.dir, "reps.parquet")
        reps.to_parquet(rpath, index=False)
        membership = pd.DataFrame({"CBSA": ["19820", None], "CBSA_NAME": ["Detroit", None], "LSAD": ["M1", None],
                                   "share": [1.0, None]}, index=pd.Index(["26163", "26001"], name="GEOID"))
        with mock.patch.object(workflows_microdata, "_county_rpp_areas", return_value=(membership, "test://cbsa")):
            res = workflows_microdata.place_adjust_frame(fpath, ["income"], 2022, replicates_path=rpath,
                                                         output_path=os.path.join(self.dir, "out.parquet"))
        self.assertTrue(res["success"], res)
        out = pd.read_parquet(res["output_path"]).set_index("GEOID")
        metro = rpp.place_parity("19820", 2022)["rpp"]
        nonmetro = rpp.place_parity("26999", 2022)["rpp"]
        self.assertAlmostEqual(out.loc["26163", "income"], 60000 * 100 / metro)
        self.assertAlmostEqual(out.loc["26001", "income_se"], 1000 * 100 / nonmetro)
        self.assertEqual(out.loc["26001", "income_cv"], 2.0)
        self.assertEqual(out.loc["26163", "pct"], 30.0)  # not a dollar variable: untouched
        self.assertEqual(out.loc["26001", "rpp_area"], "26999")
        self.assertEqual(out.loc["26001", "cbsa_type"], "none")
        new = pd.read_parquet(res["replicates_output_path"])
        row = new[(new["GEOID"] == "26163") & (new["variable"] == "income")].iloc[0]
        old = reps[(reps["GEOID"] == "26163") & (reps["variable"] == "income")].iloc[0]
        np.testing.assert_allclose(row[["estimate"] + REP_COLS].to_numpy(float),
                                   old[["estimate"] + REP_COLS].to_numpy(float) * 100 / metro)
        untouched = new[new["variable"] == "pct"].sort_values("GEOID")
        np.testing.assert_allclose(untouched["rep_5"].to_numpy(float),
                                   reps[reps["variable"] == "pct"].sort_values("GEOID")["rep_5"].to_numpy(float))
        self.assertTrue(any("own estimation error" in n for n in res["notes"]))
        self.assertFalse(res["applied_by_default"])

    def test_state_areas_and_errors(self):
        from census import rpp
        from census.workflows_microdata import place_adjust_frame

        frame = pd.DataFrame({"GEOID": ["26163001100"], "income": [50000.0], "income_se": [500.0]})
        fpath = os.path.join(self.dir, "tracts.parquet")
        frame.to_parquet(fpath, index=False)
        res = place_adjust_frame(fpath, ["income"], 2022, areas="county_metro")
        self.assertFalse(res["success"])
        self.assertIn("county GEOIDs", res["error"])
        res = place_adjust_frame(fpath, ["income"], 2022, areas="state",
                                 output_path=os.path.join(self.dir, "s.parquet"))
        self.assertTrue(res["success"], res)
        out = pd.read_parquet(res["output_path"])
        self.assertAlmostEqual(out["income"].iloc[0], 50000 * 100 / rpp.place_parity("26", 2022)["rpp"])
        self.assertFalse(place_adjust_frame(fpath, ["rent"], 2022, areas="state")["success"])
        self.assertFalse(place_adjust_frame(fpath, ["income"], 2022, areas="msa")["success"])
        frame.assign(Year=[2021]).to_parquet(fpath, index=False)
        res = place_adjust_frame(fpath, ["income"], 2022, areas="state")
        self.assertFalse(res["success"])
        self.assertIn("releases other than 2022", res["error"])


class TestResultsBundleAndSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": os.path.join(self.dir, "cache.db")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, *argv):
        from census.__main__ import main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(argv))
        return code, json.loads(buf.getvalue())

    def _inputs(self, source=None):
        meta = os.path.join(self.dir, "B19013_extraction_metadata.json")
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"table_code": "B19013", "dataset": "acs5", "years": [2024], "geo_level": "county",
                       "endpoint": "https://api.census.gov/data/{year}/acs/acs5",
                       "entities": [{"state_code": "26"}, {"state_code": "36"}], "failed_calls": [],
                       "data_source": source or {"kind": "census_api", "synthetic": False}}, f)
        figs = []
        for folder in ("26", "36"):
            os.makedirs(os.path.join(self.dir, folder), exist_ok=True)
            figs.append(os.path.join(self.dir, folder, "map.png"))
            with open(figs[-1], "wb") as f:
                f.write(folder.encode())
        return meta, figs

    def test_save_and_bundle_census_api(self):
        saved = os.path.join(self.dir, "results", "trend.json")
        code, out = self.cli("tool", "trend_test", "--arg", "years=[2014, 2019, 2024]",
                             "--arg", "estimates=[10, 12, 15]", "--arg", "moes=[1, 1, 1]", "--save", saved, "--json")
        self.assertEqual(code, 0)
        with open(saved, encoding="utf-8") as f:
            self.assertEqual(json.load(f), out)
        meta, fig = self._inputs()
        bdir = os.path.join(self.dir, "bundle")
        code, res = self.cli("tool", "make_results_bundle", "--arg", f"output_dir={bdir}",
                             "--arg", "results=" + json.dumps({"trend": saved}),
                             "--arg", "figure_paths=" + json.dumps(fig),
                             "--arg", "inputs=" + json.dumps([meta]), "--arg", "title=Test",
                             "--arg", 'notes=["RPP error not included."]', "--json")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["data_source"], {"kind": "census_api", "synthetic": False})
        with open(os.path.join(bdir, "bundle.json"), encoding="utf-8") as f:
            bundle = json.load(f)
        prov = bundle["provenance"]
        self.assertEqual(prov["analysis_status"], "exploratory")
        self.assertEqual(prov["extractions"][0]["states"], ["26", "36"])
        # same file name from two folders: both kept, prefixed with the folder name
        self.assertIn("figures/26_map.png", bundle["files"])
        self.assertIn("figures/36_map.png", bundle["files"])
        self.assertIn("results/trend.json", bundle["files"])
        self.assertIn("2020-2024", bundle["citation"])
        self.assertIn("Retrieved via the Census Data API", bundle["citation"])
        with open(os.path.join(bdir, "methods.md"), encoding="utf-8") as f:
            methods = f.read()
        self.assertIn("exploratory", methods)
        self.assertIn("RPP error not included.", methods)

    def test_bundle_follows_recorded_synthetic_source(self):
        """The source recorded in the extraction metadata, not the cache present at bundle time."""
        meta, _ = self._inputs({"kind": "synthetic", "synthetic": True, "description": "generated test data"})
        saved = os.path.join(self.dir, "r.json")
        with open(saved, "w", encoding="utf-8") as f:
            json.dump({"success": True, "summary": "x"}, f)
        code, res = self.cli("tool", "make_results_bundle", "--arg", f"output_dir={os.path.join(self.dir, 'b')}",
                             "--arg", "results=" + json.dumps({"r": saved}), "--arg", "inputs=" + json.dumps([meta]),
                             "--json")
        self.assertEqual(code, 0, res)
        self.assertTrue(res["data_source"]["synthetic"])
        self.assertEqual(res["data_source"]["kind"], "synthetic")
        self.assertIn("generated test data", res["citation"])
        self.assertNotIn("Retrieved via the Census Data API", res["citation"])

    def test_bundle_missing_result_file(self):
        code, res = self.cli("tool", "make_results_bundle", "--arg", f"output_dir={self.dir}",
                             "--arg", "results=" + json.dumps({"r": os.path.join(self.dir, "nope.json")}), "--json")
        self.assertNotEqual(code, 0)
        self.assertFalse(res["success"])


if __name__ == "__main__":
    unittest.main()
