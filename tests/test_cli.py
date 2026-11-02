"""Tests for the python -m census command-line interface."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from census.__main__ import EXIT_INPUT, EXIT_OK, main


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), patch.dict(os.environ, {"CENSUS_CACHE_DISABLED": "1"}):
        code = main(argv)
    return code, buf.getvalue()


class TestCLI(unittest.TestCase):
    def test_describe_json(self):
        code, out = run(["describe", "--code", "DP02_0002PE", "--year", "2019", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Married-couple", json.loads(out)["label"])

    def test_concordance_reports_relabel(self):
        code, out = run(
            ["concordance", "--table", "B27001", "--years", "2016-2017", "--variables", "B27001_006", "--json"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["issues"][0]["kind"], "relabeled")

    def test_invalid_geo_exit_code(self):
        code, out = run(["plan", "--table", "B19013", "--geo", "bogus", "--years", "2020", "--json"])
        self.assertEqual(code, EXIT_INPUT)
        self.assertEqual(json.loads(out)["error_code"], "INVALID_GEO_LEVEL")

    def test_vintages(self):
        code, out = run(["vintages", "--dataset", "subject", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(2017, json.loads(out)["years"])

    def test_plan_nationwide(self):
        base = ["plan", "--table", "B19013", "--years", "2022", "--json"]
        code, out = run(base + ["--geo", "county", "--state", "all"])
        self.assertEqual(code, EXIT_OK)
        plan = json.loads(out)
        self.assertEqual((plan["api_calls"], plan["states_covered"]), (1, 51))
        code, out = run(base + ["--geo", "tract", "--state", "all", "--include-pr"])
        self.assertEqual(json.loads(out)["api_calls"], 52)
        code, out = run(base + ["--geo", "tract", "--state", "all", "--codes", "001"])
        self.assertEqual(code, EXIT_INPUT)


class TestReplayCLI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_replay_roundtrip_and_old_metadata(self):
        from census.pipeline import ResearchOptions, build_entities, run_batch
        from tests.test_replay import FakeClient

        res = run_batch(
            "B19013", build_entities("tract", "36", ["061"]), [2020], os.path.join(self.dir, "o"),
            "panel", ResearchOptions(use_cache=False), client=FakeClient(),
        )
        with patch("census.pipeline.CensusAPIClient", FakeClient):
            code, out = run(["replay", res["metadata_path"], "--output-dir", os.path.join(self.dir, "r"), "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(json.loads(out)["verified"])

        with open(res["metadata_path"], encoding="utf-8") as f:
            meta = json.load(f)
        del meta["replay_schema_version"]
        old = os.path.join(self.dir, "old.json")
        with open(old, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        code, out = run(["replay", old, "--no-verify", "--json"])
        self.assertEqual(code, EXIT_INPUT)
        self.assertEqual(json.loads(out)["missing_fields"], ["replay_schema_version"])


if __name__ == "__main__":
    unittest.main()


class TestCLIRound7(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_derive(self):
        code, out = run(["derive", "ratio", "--estimates", "400,1000", "--moes", "50,80", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["method"], "ratio")
        code, _ = run(["derive", "ratio", "--estimates", "1,2,3", "--moes", "1,1,1", "--json"])
        self.assertEqual(code, EXIT_INPUT)

    def test_aggregate_with_csv_groups(self):
        import pandas as pd

        data = os.path.join(self.dir, "d.csv")
        pd.DataFrame(
            {"GEOID": ["36061000100", "36061000200"], "B01003_001E": [100, 300], "B01003_001M": [30, 40]}
        ).to_csv(data, index=False)
        groups = os.path.join(self.dir, "g.csv")
        pd.DataFrame({"GEOID": ["36061000100", "36061000200"], "group": ["x", "x"]}).to_csv(groups, index=False)
        out_path = os.path.join(self.dir, "agg.csv")
        code, out = run(["aggregate", "--input", data, "--groups", groups, "--table", "B01003",
                         "--year", "2023", "--out", out_path, "--json"])
        self.assertEqual(code, EXIT_OK, out)
        agg = pd.read_csv(out_path)
        self.assertEqual(agg["B01003_001E"].iloc[0], 400)
        self.assertAlmostEqual(agg["B01003_001M"].iloc[0], 50.0)

    def test_replicates_availability(self):
        code, out = run(["replicates", "--table", "B19001", "--year", "2023", "--geo", "tract", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(json.loads(out)["per_state_files"])
