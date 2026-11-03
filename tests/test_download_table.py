# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slim core download and native summary/next_step in Round 7 tools (Round 9, G2)."""

import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from census import mcp_profiles
from census import workflows as wf


def _plan(would_block=False):
    return {"success": True, "years": [2023], "would_block": would_block, "block_reason": "drift",
            "issues": [{"blocking": True}], "issue_count": 1, "_concordance": object()}


def _batch(*_a, **_k):
    return {"success": True, "table_code": "B17001", "total_calls_attempted": 1, "successful_calls": 1,
            "failed_calls": 0, "panel_path": "out/B17001_panel.csv"}


class TestDownloadTable(unittest.TestCase):
    def test_blocks_on_would_block(self):
        with mock.patch("census.pipeline.plan_extraction", return_value=_plan(True)), \
                mock.patch("census.pipeline.run_batch") as rb:
            out = wf.download_table("B17001", "county:453", state_code="48", years=[2018, 2023])
        rb.assert_not_called()
        self.assertFalse(out["success"])
        self.assertEqual(out["error_code"], "CONCORDANCE_REVIEW_REQUIRED")
        self.assertTrue(out["would_block"])
        self.assertIn("would block", out["summary"])
        self.assertIn("variables", out["next_step"])

    def test_downloads_panel_with_defaults(self):
        with mock.patch("census.pipeline.plan_extraction", return_value=_plan()) as pe, \
                mock.patch("census.pipeline.run_batch", side_effect=_batch) as rb:
            out = wf.download_table("b17001", "county:453,491", state_code="48", years=[2023],
                                    variables=["B17001_002"], output_dir="out")
        self.assertTrue(out["success"])
        self.assertIn("out/B17001_panel.csv", out["summary"])
        self.assertTrue(out["next_step"])
        args = rb.call_args
        self.assertEqual(args.args[0], "B17001")
        self.assertEqual(args.args[4], "panel")
        options = args.args[5]
        self.assertTrue(options.compute_reliability and options.include_codebook)
        self.assertIsNone(options.harmonize_tracts_to)
        self.assertEqual(args.kwargs["variables"], ["B17001_002"])
        entities = pe.call_args.args[1]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].geo_level, "county")

    def test_tracts_across_2020_are_harmonized(self):
        with mock.patch("census.pipeline.plan_extraction", return_value=_plan()), \
                mock.patch("census.pipeline.run_batch", side_effect=_batch) as rb:
            wf.download_table("B17001", "tract:453", state_code="48", years=[2013, 2018, 2023])
        self.assertEqual(rb.call_args.args[5].harmonize_tracts_to, 2020)

    def test_bad_geography(self):
        out = wf.download_table("B17001", "planet")
        self.assertEqual(out["error_code"], "INVALID_GEO_LEVEL")
        self.assertTrue(out["summary"] and out["next_step"])

    def test_core_profile(self):
        core = mcp_profiles.PROFILES["core"]
        self.assertIn("download_table", core)
        self.assertNotIn("batch_download", core)
        self.assertIsNone(mcp_profiles.PROFILES["full"])
        desc = mcp_profiles.DESCRIPTION_OVERRIDES["download_table"]
        self.assertLessEqual(len(desc), mcp_profiles.DESCRIPTION_BUDGET)
        examples = [ln for ln in desc.splitlines() if ln.startswith("Example:")]
        self.assertEqual(len(examples), 1)
        self.assertIn("download_table(", examples[0])


class TestRound7Guidance(unittest.TestCase):
    def assertGuided(self, out):
        self.assertIsInstance(out["summary"], str)
        self.assertTrue(out["summary"])
        self.assertTrue(out["next_step"])
        self.assertEqual(list(out)[:2], ["summary", "next_step"])

    def test_success_paths(self):
        self.assertGuided(wf.derive_estimate("proportion", [10, 100], [2, 5]))
        self.assertGuided(wf.check_replicate_availability("B17001", 2023, "county"))
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.csv")
            pd.DataFrame({"GEOID": ["1", "2"], "a": [10.0, 20.0], "am": [1.0, 1.0],
                          "b": [12.0, 20.0], "bm": [1.0, 1.0]}).to_csv(src, index=False)
            out = wf.batch_significance(src, "a", "am", "b", "bm", output_path=os.path.join(d, "o.csv"))
            self.assertGuided(out)
            self.assertIn("holm", out["summary"])

    def test_failure_paths(self):
        for out in (
            wf.derive_estimate("bogus", [1], [1]),
            wf.aggregate_geographies("does/not/exist.csv", {"1": "a"}),
            wf.median_from_distribution("B19001", 2023),
            wf.apportion_tracts("does/not/exist.csv"),
            wf.replay_extraction("does/not/exist.json"),
            wf.run_validation("F99"),
        ):
            self.assertFalse(out["success"])
            self.assertGuided(out)

    def test_wrapped_signature_kept(self):
        import inspect

        self.assertIn("est1", inspect.signature(wf.batch_significance).parameters)
        self.assertIn("Test many geographies", wf.batch_significance.__doc__)


if __name__ == "__main__":
    unittest.main()
