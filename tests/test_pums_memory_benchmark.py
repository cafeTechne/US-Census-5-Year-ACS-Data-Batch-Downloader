# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/pums_memory_benchmark.py in --small mode (2,000 synthetic rows; PLAN_ROUND_14 C).

The full benchmark (100k-1.9M rows) is run by hand; docs/audits/pums_memory.md
holds its results.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

import numpy as np

from census import pums

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "pums_memory_benchmark.py")
_spec = importlib.util.spec_from_file_location("pums_memory_benchmark", SCRIPT)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


class TestPumsMemoryBenchmark(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_synthetic_file_reads_like_pums(self):
        path = os.path.join(self.dir, "p.csv.gz")
        info = bench.generate(path, 1_500)
        self.assertEqual((info["rows"], info["columns"]), (1_500, 94))
        df = pums.read_pums_csv(path, "person", bench.ANALYSIS_COLUMNS, chunksize=400)
        self.assertEqual(len(df), 1_500)
        self.assertTrue(df["PUMA"].str.len().eq(5).all() and df["STATE"].eq("06").all())
        self.assertEqual(df["PWGTP80"].dtype, np.float32)
        self.assertTrue(df.loc[df["AGEP"] < 15, "PINCP"].isna().all())
        self.assertTrue(df.loc[df["AGEP"] >= 15, "PINCP"].notna().all())

    def test_small_mode(self):
        out, raw = os.path.join(self.dir, "report.md"), os.path.join(self.dir, "raw.json")
        code = bench.main(["--small", "--workdir", self.dir, "--out", out, "--json", raw])
        self.assertEqual(code, 0)
        with open(raw, encoding="utf-8") as f:
            res = json.load(f)
        self.assertEqual(res["equivalence"], {"2000": {"tabulate": True, "read_hash": True, "chunksizes": 2}})
        phases = sorted(r["phase"] for r in res["runs"])
        self.assertEqual(phases, ["baseline", "read", "read", "tabulate", "tabulate", "weights"])
        weights = next(r for r in res["runs"] if r["phase"] == "weights")
        self.assertEqual(weights["matrix_bytes"], 2_000 * 81 * 8)
        for r in res["runs"]:
            self.assertGreater(r["peak"], 0)
        with open(out, encoding="utf-8") as f:
            report = f.read()
        self.assertIn("## Chunk-size equivalence", report)
        self.assertIn("| 2,000 | 2 | yes | yes |", report)
        self.assertFalse(any(name.endswith(".csv.gz") for name in os.listdir(self.dir)))  # deleted afterwards


if __name__ == "__main__":
    unittest.main()
