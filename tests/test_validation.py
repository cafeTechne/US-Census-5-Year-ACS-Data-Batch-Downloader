# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline validation battery (PLAN_ROUND_7.md section 8) against frozen Census responses.

Every check F1-F11 runs on tests/fixtures/validation/ (captured from
api.census.gov and www2.census.gov, each with a .json sidecar: source URL
without the API key, retrieval date, SHA-256). No network access.

F5 first found that census.medians returned the lower edge of an empty-bracket
gap when the cumulative count is exactly 50% at a bracket boundary, while Census
publishes the gap midpoint (Sterling and Stonewall counties, TX, B25064 2019).
That is fixed; test_f5_details pins the corrected behavior.
"""

import glob
import gzip
import json
import hashlib
import math
import os
import tempfile
import unittest

import pandas as pd

from census import validation as V

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "validation")


class TestOfflineBattery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {r.check_id: r for r in V.run_battery(live=False)}

    def assertPassed(self, cid):
        r = self.results[cid]
        self.assertEqual(r.status, V.STATUS_PASSED, f"{cid}: {r.notes} {r.failures[:3]}")
        self.assertTrue(r.passed)
        self.assertGreater(r.n_compared, 0)
        self.assertEqual(r.n_failed, 0)

    def test_every_check_ran(self):
        self.assertEqual(sorted(self.results), sorted(V.CHECK_IDS))
        for r in self.results.values():
            self.assertNotEqual(r.status, V.STATUS_ERROR, f"{r.check_id}: {r.notes}")

    def test_f1_pipeline_cells_equal_api_cells(self):
        self.assertPassed("F1")
        fmts = self.results["F1"].details["formats"]
        self.assertEqual(set(fmts), {"panel", "parquet"})
        for d in fmts.values():
            self.assertGreater(d["cells_compared"], 1000)
            self.assertEqual(d["mismatched_cells"] + d["rows_missing"] + d["rows_extra"], 0)

    def test_f2_profile_proportions(self):
        self.assertPassed("F2")
        d = self.results["F2"].details
        self.assertEqual(d["formula_mismatches"], 0)
        self.assertLessEqual(self.results["F2"].max_abs_error, 0.05 + 1e-9)  # DP rounding bound
        for key, stat in d["per_construction"].items():
            self.assertEqual(stat["moe_failures"], 0, key)

    def test_f3_replicate_moe(self):
        self.assertPassed("F3")
        self.assertLessEqual(self.results["F3"].max_abs_error, 0.5)
        self.assertEqual(self.results["F3"].details["api_cell_mismatches"], 0)

    def test_f4_replicate_proportions(self):
        self.assertPassed("F4")
        self.assertLessEqual(self.results["F4"].details["max_estimate_error"], 0.05 + 1e-9)
        self.assertLessEqual(self.results["F4"].details["max_moe_error"], 0.1)

    def test_f5_medians_pass(self):
        self.assertPassed("F5")

    def test_f5_details(self):
        r = self.results["F5"]
        pairs = r.details["pairs"]
        # Exact-50% boundaries followed by empty brackets now return the gap midpoint,
        # which reproduces the two 2019 gross-rent medians F5 first caught.
        for year in (2019, 2023):
            rent = pairs[f"B25063->B25064_001 {year}"]
            self.assertEqual(rent["failures"], 0, year)
            self.assertLessEqual(rent["max_abs_error"], 1.0, year)
        inc19, inc23 = pairs["B19001->B19013_001 2019"], pairs["B19001->B19013_001 2023"]
        for inc in (inc19, inc23):
            self.assertTrue(inc["criterion_met"])
            self.assertLess(inc["median_rel_error"], 0.01)
            self.assertGreaterEqual(inc["share_within_published_moe"], 0.98)
        # Reported, not asserted: B19001 is coarser than Census's internal distribution.
        self.assertAlmostEqual(inc23["share_within_2pct"], 0.8589, places=3)
        self.assertEqual(r.n_failed, 0)

    def test_f6_cp_inflation(self):
        self.assertPassed("F6")
        for key, f in self.results["F6"].details["factors"].items():
            self.assertLess(abs(f["implied_minus_ours_percent"]), 0.05, key)
            self.assertEqual(f["current_period_cp_ne_dp"], 0, key)

    def test_f7_cp_significance(self):
        self.assertPassed("F7")
        d = self.results["F7"].details
        self.assertEqual(d["agreement_rate_decidable"], 1.0)
        self.assertGreater(d["decidable_tests"], 400)

    def test_f8_f9_f10(self):
        for cid in ("F8", "F9", "F10", "F11"):
            self.assertPassed(cid)
        self.assertEqual(self.results["F8"].max_abs_error, 0.0)

    def test_reports(self):
        with tempfile.TemporaryDirectory() as d:
            results = list(self.results.values())
            md = V.write_markdown_report(results, os.path.join(d, "r.md"))
            js = V.write_json_report(results, os.path.join(d, "r.json"))
            with open(js, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertTrue(payload["summary"]["all_passed"])
            self.assertEqual(payload["summary"]["failed"], [])
            with open(md, encoding="utf-8") as f:
                text = f.read()
            for cid in V.CHECK_IDS:
                self.assertIn(f"## {cid}:", text)


class TestFixtures(unittest.TestCase):
    def test_sidecars_hash_and_no_key(self):
        files = [f for f in glob.glob(os.path.join(FIXTURES, "*")) if not f.endswith(".json")]
        self.assertGreater(len(files), 50)
        total = 0
        for path in files:
            with open(path, "rb") as f:
                raw = f.read()
            total += len(raw)
            with open(path + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["sha256"], hashlib.sha256(raw).hexdigest(), path)
            self.assertIn("source_url", meta)
            self.assertIn("retrieved", meta)
            self.assertNotIn("key=", meta["source_url"].lower())
            self.assertNotIn(b"key=", gzip.decompress(raw) if path.endswith(".gz") else raw)
        self.assertLess(total, 2 * 1024 * 1024)

    def test_missing_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            src = V.CensusSource(live=False, fixture_dir=d)
            with self.assertRaises(V.FixtureMissing):
                src.get_json("https://api.census.gov/data/2023/acs/acs5", {"get": "NAME", "for": "us:1"})
            r = V.run_battery("F8", fixture_dir=d)[0]
            self.assertEqual(r.status, V.STATUS_ERROR)
            self.assertFalse(r.passed)

    def test_tampered_fixture_is_refused(self):
        name = os.path.basename(sorted(glob.glob(os.path.join(FIXTURES, "api_*.json.gz")))[0])
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(FIXTURES, name), "rb") as f:
                raw = f.read()
            with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
                meta = f.read()
            with open(os.path.join(d, name), "wb") as f:
                f.write(raw + b"\0")
            with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
                f.write(meta)
            with self.assertRaises(V.SourceError):
                V.CensusSource(live=False, fixture_dir=d).read_fixture(name)


class TestHelpers(unittest.TestCase):
    def test_redact_and_public_url(self):
        self.assertEqual(V.redact("https://x?get=A&key=abc123&for=us:1", "abc123"), "https://x?get=A&key=REDACTED&for=us:1")
        self.assertEqual(V.redact("url?key=zzz"), "url?key=REDACTED")
        self.assertEqual(
            V.public_url("https://e", {"key": "secret", "get": "NAME", "for": "us:1"}),
            "https://e?for=us%3A1&get=NAME",
        )

    def test_parse_checks(self):
        self.assertEqual(V.parse_checks(None), list(V.CHECK_IDS))
        self.assertEqual(V.parse_checks("f4, F1,F4"), ["F1", "F4"])
        with self.assertRaises(ValueError):
            V.parse_checks("F11")

    def test_z_rounding_range(self):
        # 63.7 vs 63.8 with MOEs 0.1: published values cannot decide significance.
        lo, hi = V._z_rounding_range(63.7, 0.1, 63.8, 0.1, percent=True)
        self.assertEqual(lo, 0.0)
        self.assertTrue(math.isinf(hi))
        # Large difference with small MOEs: decidable and significant.
        lo, hi = V._z_rounding_range(70.0, 0.5, 60.0, 0.5, percent=True)
        self.assertGreater(lo, 1.645)
        lo, hi = V._z_rounding_range(100000, 2000, 100100, 2000, percent=False)
        self.assertLess(hi, 1.645)

    def test_gap_midpoint_diagnosis(self):
        brackets = [("X_001", 0.0, 100.0, ""), ("X_002", 100.0, 200.0, ""), ("X_003", 200.0, 300.0, ""),
                    ("X_004", 300.0, math.inf, "")]
        row = pd.Series({"X_001E": 5, "X_002E": 5, "X_003E": 0, "X_004E": 10})
        self.assertIn("midpoint 250", V._gap_midpoint_diagnosis(row, brackets, 250.0))
        self.assertIsNone(V._gap_midpoint_diagnosis(row, brackets, 200.0))
        row2 = pd.Series({"X_001E": 5, "X_002E": 6, "X_003E": 0, "X_004E": 10})
        self.assertIsNone(V._gap_midpoint_diagnosis(row2, brackets, 250.0))

    def test_norm_label(self):
        a = V._norm_label("2014-2018 Estimates!!INCOME AND BENEFITS (IN 2023 INFLATION-ADJUSTED DOLLARS)!!Total households:")
        b = V._norm_label("Estimate!!INCOME AND BENEFITS (IN 2018 INFLATION-ADJUSTED DOLLARS)!!Total households")
        self.assertEqual(a, b)

    def test_constructions_verified_against_labels(self):
        for year in (2019, 2023):
            for con in V.CONSTRUCTIONS:
                problems, _ = V._verify_construction(con, year)
                self.assertEqual(problems, [], f"{con.id} {year}")


if __name__ == "__main__":
    unittest.main()
