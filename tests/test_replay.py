"""Extraction metadata completeness and replay_extraction (offline, fake client)."""

import json
import os
import shutil
import tempfile
import unittest

from census.api import CensusAPIClient
from census.pipeline import (
    REPLAY_REQUIRED_FIELDS,
    ResearchOptions,
    build_entities,
    diff_frames,
    file_sha256,
    replay_extraction,
    run_batch,
)

TRACT_HEADER = [
    "GEO_ID", "NAME", "B19013_001E", "B19013_001EA", "B19013_001M",
    "B19013_001MA", "state", "county", "tract",
]


class FakeClient(CensusAPIClient):
    """Canned tract payload; ``income`` simulates a Census revision."""

    def __init__(self, income="50000", cache=None, **_kwargs):
        super().__init__(api_key="", cache=None)
        self.income = income
        self.calls = []

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        self.calls.append((year, table_code, dataset, kwargs))
        self.last_error = None
        return [
            TRACT_HEADER,
            ["1400000US36061000100", "Tract 1", self.income, None, "4935", None, "36", "061", "000100"],
            ["1400000US36061000200", "Tract 2", "62000", None, "5100", None, "36", "061", "000200"],
        ]

    def fetch_variable_metadata(self, year, table_code, dataset=None):  # type: ignore[override]
        return {}


class ReplayTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.orig = os.path.join(self.dir, "orig")
        self.entities = build_entities("tract", "36", ["061"], {"061": "New York County"})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def extract(self, fmt="panel", variables=None, **opts):
        opts.setdefault("use_cache", False)
        res = run_batch(
            "B19013", self.entities, [2018, 2019, 2020], self.orig, fmt,
            ResearchOptions(**opts), client=FakeClient(), variables=variables,
        )
        self.assertTrue(res["success"], res)
        with open(res["metadata_path"], encoding="utf-8") as f:
            return res, json.load(f)

    def replay(self, res, client=None, **kwargs):
        kwargs.setdefault("output_dir", os.path.join(self.dir, "replay"))
        return replay_extraction(res["metadata_path"], client=client or FakeClient(), **kwargs)


class TestMetadata(ReplayTestCase):
    def test_replay_fields(self):
        res, meta = self.extract(variables=["B19013_001"], compute_reliability=True)
        for field in REPLAY_REQUIRED_FIELDS:
            self.assertIn(field, meta)
        self.assertEqual(meta["variables"], ["B19013_001"])
        self.assertEqual(meta["entities"][0]["county_code"], "061")
        self.assertTrue(meta["options"]["compute_reliability"])
        self.assertNotIn("cpi_table", meta["options"])
        self.assertIsNone(meta["cpi_table"])
        self.assertIn("replay", meta["replay_command"])
        self.assertEqual(res["replay_command"], meta["replay_command"])
        panel_rel = os.path.basename(res["panel_path"])
        self.assertEqual(meta["output_files"][panel_rel], file_sha256(res["panel_path"]))
        self.assertIn("B19013_concordance.csv", meta["output_files"])
        self.assertNotIn("B19013_extraction_metadata.json", meta["output_files"])
        self.assertIn("metadata_vintages", meta["concordance"])

    def test_custom_cpi_table_recorded(self):
        table = {2018: 100.0, 2019: 110.0, 2020: 120.0}
        _res, meta = self.extract(inflation_base_year=2020, cpi_table=table)
        self.assertEqual(meta["cpi_table"], {"2018": 100.0, "2019": 110.0, "2020": 120.0})

    def test_legacy_folder_files_hashed(self):
        _res, meta = self.extract("both")
        self.assertTrue(any(k.endswith("compiled_unmelted_data.csv") for k in meta["output_files"]))
        self.assertTrue(all("/" in k for k in meta["output_files"] if k.endswith("melted.csv")))


class TestReplay(ReplayTestCase):
    def test_identical_replay_verifies(self):
        res, _ = self.extract(compute_reliability=True, include_codebook=True)
        out = self.replay(res)
        self.assertTrue(out["success"], out)
        self.assertTrue(out["verified"], out)
        self.assertEqual(set(out["file_status_counts"]), {"identical"})
        self.assertEqual(out["n_cells_changed"], 0)
        self.assertTrue(os.path.exists(out["metadata_path"]))

    def test_custom_cpi_table_replayed(self):
        res, _ = self.extract(inflation_base_year=2020, cpi_table={2018: 100.0, 2019: 110.0, 2020: 120.0})
        out = self.replay(res)
        self.assertTrue(out["verified"], out)

    def test_variables_and_entities_reused(self):
        res, _ = self.extract(variables=["B19013_001"])
        client = FakeClient()
        out = self.replay(res, client=client)
        self.assertTrue(out["verified"])
        self.assertEqual({c[0] for c in client.calls}, {2018, 2019, 2020})
        tract_calls = [c for c in client.calls if c[3]["geo_level"] == "tract"]
        self.assertEqual(len(tract_calls), 3)
        self.assertEqual({c[3]["county_code"] for c in tract_calls}, {"061"})

    def test_revision_reported_cell_by_cell(self):
        res, _ = self.extract()
        out = self.replay(res, client=FakeClient(income="51000"))
        self.assertTrue(out["success"])
        self.assertFalse(out["verified"])
        self.assertIn("revises", out["explanation"])
        panel_rel = os.path.basename(res["panel_path"])
        entry = out["files"][panel_rel]
        self.assertEqual(entry["status"], "changed")
        diff = entry["diff"]
        self.assertEqual(diff["n_cells_changed"], 3)  # tract 1 x 3 years
        sample = diff["changes_sample"][0]
        self.assertEqual(sample["GEOID"], "36061000100")
        self.assertIn(sample["year"], ("2018", "2019", "2020"))
        self.assertEqual((sample["old"], sample["new"]), ("50000", "51000"))
        self.assertTrue(sample["column"].endswith("_est"))
        self.assertEqual(out["n_cells_changed"], 3)

    def test_parquet_revision(self):
        res, _ = self.extract("parquet")
        out = self.replay(res, client=FakeClient(income="49000"))
        entry = out["files"][os.path.basename(res["panel_path"])]
        self.assertEqual(entry["diff"]["n_cells_changed"], 3)

    def test_legacy_and_excel_formats(self):
        res, _ = self.extract("both")
        self.assertTrue(self.replay(res)["verified"])
        shutil.rmtree(self.orig)
        res, _ = self.extract("excel")
        out = self.replay(res, output_dir=os.path.join(self.dir, "replay_xlsx"))
        self.assertTrue(out["verified"], out["files"])

    def test_default_output_dir_and_same_dir_refused(self):
        res, _ = self.extract()
        out = replay_extraction(res["metadata_path"], client=FakeClient())
        self.assertTrue(out["output_dir"].startswith(os.path.join(self.orig, "replays")))
        same = replay_extraction(res["metadata_path"], output_dir=self.orig, client=FakeClient())
        self.assertEqual(same["error_code"], "INVALID_INPUT")

    def test_no_verify(self):
        res, _ = self.extract()
        out = self.replay(res, verify=False)
        self.assertTrue(out["success"])
        self.assertIsNone(out["verified"])
        self.assertNotIn("files", out)

    def test_old_metadata_rejected(self):
        res, meta = self.extract()
        for key in ("replay_schema_version", "variables", "output_files", "replay_command"):
            meta.pop(key)
        old = os.path.join(self.dir, "old_metadata.json")
        with open(old, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        out = replay_extraction(old, client=FakeClient())
        self.assertFalse(out["success"])
        self.assertEqual(out["error_code"], "INVALID_INPUT")
        self.assertEqual(out["missing_fields"], ["replay_schema_version", "variables"])

    def test_missing_and_malformed_files(self):
        self.assertEqual(replay_extraction(os.path.join(self.dir, "nope.json"))["error_code"], "INVALID_INPUT")
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(replay_extraction(bad)["error_code"], "INVALID_INPUT")


class TestDiffFrames(unittest.TestCase):
    def test_rows_and_columns(self):
        import pandas as pd

        old = pd.DataFrame({"GEOID": ["1", "2"], "Year": ["2020", "2020"], "x": ["a", "b"]})
        new = pd.DataFrame({"GEOID": ["1", "3"], "Year": ["2020", "2020"], "x": ["z", "c"], "y": ["q", "r"]})
        d = diff_frames(old, new)
        self.assertEqual(d["matched_on"], ["GEOID", "Year"])
        self.assertEqual(d["n_cells_changed"], 1)
        self.assertEqual((d["rows_removed"], d["rows_added"]), (1, 1))
        self.assertEqual(d["columns_added"], ["y"])
        self.assertEqual(d["changes_sample"][0], {"GEOID": "1", "year": "2020", "column": "x", "old": "a", "new": "z"})

    def test_positional_fallback(self):
        import pandas as pd

        old = pd.DataFrame({"GEOID": ["1", "1"], "v": ["a", "b"]})
        new = pd.DataFrame({"GEOID": ["1", "1"], "v": ["a", "c"]})
        d = diff_frames(old, new)
        self.assertEqual(d["matched_on"], "row_position")
        self.assertEqual(d["changes_sample"][0]["row"], 1)
        self.assertEqual(d["changes_sample"][0]["GEOID"], "1")


if __name__ == "__main__":
    unittest.main()
