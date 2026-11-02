"""Offline tests for the Round 7 MCP tools (derived statistics, medians, testing, geography)."""

import math
import os
import shutil
import tempfile
import unittest

import pandas as pd

import mcp_server as srv


class TestRound7Tools(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _csv(self, df, name="in.csv"):
        path = os.path.join(self.dir, name)
        df.to_csv(path, index=False)
        return path

    def test_derive_proportion_and_ratio(self):
        res = srv.derive_estimate("proportion", [400, 1000], [50, 80])
        self.assertTrue(res["success"])
        self.assertAlmostEqual(res["estimate"], 0.4)
        expected = math.sqrt(50**2 - 0.4**2 * 80**2) / 1000
        self.assertAlmostEqual(res["moe"], expected)
        self.assertEqual(res["method"], "proportion")
        ratio = srv.derive_estimate("ratio", [400, 1000], [50, 80])
        self.assertAlmostEqual(ratio["moe"], math.sqrt(50**2 + 0.4**2 * 80**2) / 1000)
        total = srv.derive_estimate("sum", [10, 20, 30], [3, 4, 12])
        self.assertAlmostEqual(total["moe"], 13.0)
        self.assertEqual(srv.derive_estimate("mean", [1], [1])["error_code"], "INVALID_INPUT")
        self.assertEqual(srv.derive_estimate("ratio", [1, 2, 3], [1, 1, 1])["error_code"], "INVALID_INPUT")

    def test_proportion_fallback_recorded(self):
        # Radicand negative: MOE_num small relative to p^2 * MOE_den.
        res = srv.derive_estimate("proportion", [900, 1000], [10, 300])
        self.assertEqual(res["method"], "proportion_ratio_fallback")

    def test_check_replicate_availability(self):
        res = srv.check_replicate_availability("B19001", 2023, "county")
        self.assertTrue(res["success"])
        self.assertTrue(res["available"])
        med = srv.check_replicate_availability("B19013", 2023, "county")
        self.assertFalse(med["available"])

    def test_aggregate_geographies_then_rate(self):
        df = pd.DataFrame(
            {
                "GEOID": ["36061000100", "36061000200", "36061000300"],
                "NAME": ["a", "b", "c"],
                "B17001_001E": [1000, 2000, 1500],
                "B17001_001M": [100, 150, 120],
                "B17001_002E": [200, 300, 450],
                "B17001_002M": [40, 60, 50],
            }
        )
        path = self._csv(df)
        out_path = os.path.join(self.dir, "agg.csv")
        res = srv.aggregate_geographies(
            path,
            {"36061000100": "north", "36061000200": "north", "36061000300": "south"},
            variables=["B17001_001E", "B17001_002E"],
            derive=[{"name": "poverty_rate", "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}],
            table_code="B17001",
            year=2023,
            output_path=out_path,
        )
        self.assertTrue(res["success"], res)
        out = pd.read_csv(out_path).set_index("group_id")
        self.assertEqual(out.loc["north", "B17001_001E"], 3000)
        self.assertAlmostEqual(out.loc["north", "B17001_001M"], math.sqrt(100**2 + 150**2))
        self.assertAlmostEqual(out.loc["north", "poverty_rate_estimate"], 500 / 3000)

    def test_aggregate_refuses_medians(self):
        df = pd.DataFrame({"GEOID": ["36061000100", "36061000200"], "B19013_001E": [50000, 60000],
                           "B19013_001M": [5000, 6000]})
        res = srv.aggregate_geographies(
            self._csv(df), {"36061000100": "g", "36061000200": "g"}, table_code="B19013", year=2023
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "INVALID_VARIABLES")
        self.assertIn("median", res["error"].lower())

    def test_median_from_counts(self):
        from census.medians import distribution_brackets

        brackets = distribution_brackets("B25063", 2023)
        counts = [0.0] * len(brackets)
        # Put everything in one closed bracket: the median is its midpoint.
        idx = next(i for i, b in enumerate(brackets) if b[1] >= 1000 and math.isfinite(b[2]))
        counts[idx] = 100.0
        res = srv.median_from_distribution("B25063", 2023, counts=counts)
        self.assertTrue(res["success"], res)
        lo, hi = brackets[idx][1], brackets[idx][2]
        self.assertAlmostEqual(res["estimate"], (lo + hi) / 2)
        bad = srv.median_from_distribution("B25063", 2023, counts=[1, 2])
        self.assertEqual(bad["error_code"], "INVALID_INPUT")

    def test_batch_significance(self):
        df = pd.DataFrame(
            {
                "GEOID": [f"{i:05d}" for i in range(20)],
                "e1": [100.0] * 20,
                "m1": [10.0] * 20,
                "e2": [100.0] * 19 + [200.0],
                "m2": [10.0] * 20,
            }
        )
        res = srv.batch_significance(self._csv(df), "e1", "m1", "e2", "m2", 2013, 2023,
                                     output_path=os.path.join(self.dir, "sig.csv"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["family_size"], 20)
        self.assertEqual(res["significant_adjusted"], 1)

    def test_apportion_tracts(self):
        df = pd.DataFrame(
            {
                "GEOID": ["01001020100", "01001020100"],
                "Year": [2019, 2020],
                "B01003_001E": [1000.0, 1200.0],
                "B01003_001M": [100.0, 110.0],
            }
        )
        res = srv.apportion_tracts(self._csv(df), output_path=os.path.join(self.dir, "h.csv"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["harmonization"]["target_vintage"], 2020)

    def test_adjust_inflation_c_cpi_u_and_provisional(self):
        res = srv.adjust_inflation(100, 2015, 2024, index="c-cpi-u")
        self.assertTrue(res["success"], res)
        self.assertGreater(res["adjusted_value"], 100)
        blocked = srv.adjust_inflation(100, 2024, 2025)
        self.assertEqual(blocked["error_code"], "PRICE_INDEX_UNAVAILABLE")
        allowed = srv.adjust_inflation(100, 2024, 2025, allow_provisional=True)
        self.assertTrue(allowed["success"])
        self.assertIn("PROVISIONAL", allowed["advisory"])

    def test_replay_rejects_missing_metadata(self):
        res = srv.replay_extraction(os.path.join(self.dir, "nope.json"))
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
