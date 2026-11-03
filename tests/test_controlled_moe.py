# SPDX-License-Identifier: AGPL-3.0-or-later
"""Controlled-estimate MOEs kept as 0 with a <base>_CONTROLLED flag (Round 9, G1)."""

import unittest

import numpy as np
import pandas as pd

from census import validation as V
from census.panel import classify_column, make_slug_map, measurement_type
from census.stats import add_reliability_columns, clean_census_sentinels, controlled_column


def _raw():
    return pd.DataFrame({
        "NAME": ["A", "B", "C"],
        "B01003_001E": ["1000", "2000", "-555555555"],
        "B01003_001M": ["-555555555", "35", "-222222222"],
        "DP05_0001PE": ["100", "100", "100"],
        "DP05_0001PM": ["-555555555", "0.5", "0.5"],
        "B19013_001E": ["50000", "60000", "-666666666"],
        "B19013_001M": ["1200", "1300", "-333333333"],
    })


class TestCleanSentinels(unittest.TestCase):
    def test_controlled_moe_is_zero_and_flagged(self):
        out = clean_census_sentinels(_raw())
        self.assertEqual(out["B01003_001M"].iloc[0], 0.0)
        self.assertEqual(out["B01003_001_CONTROLLED"].tolist(), [True, False, False])
        self.assertEqual(out["B01003_001_CONTROLLED"].dtype, bool)
        self.assertEqual(out["DP05_0001PM"].iloc[0], 0.0)
        self.assertEqual(out["DP05_0001P_CONTROLLED"].tolist(), [True, False, False])

    def test_other_sentinels_still_missing(self):
        out = clean_census_sentinels(_raw())
        self.assertTrue(np.isnan(out["B01003_001M"].iloc[2]))  # -222222222
        self.assertTrue(np.isnan(out["B01003_001E"].iloc[2]))  # -555555555 in an ESTIMATE column
        self.assertTrue(out["B19013_001M"].iloc[2:].isna().all())
        self.assertNotIn("B19013_001_CONTROLLED", out.columns)  # no controlled cell, no flag column

    def test_opt_out_restores_round8_behavior(self):
        out = clean_census_sentinels(_raw(), preserve_controlled=False)
        self.assertTrue(np.isnan(out["B01003_001M"].iloc[0]))
        self.assertFalse([c for c in out.columns if c.endswith("_CONTROLLED")])

    def test_idempotent_and_notes(self):
        once = clean_census_sentinels(_raw(), add_notes=True)
        self.assertIn("controlled", once["B01003_001M_NOTE"].iloc[0])
        twice = clean_census_sentinels(once)
        pd.testing.assert_frame_equal(once, twice)

    def test_reliability_after_cleaning_is_zero_error(self):
        out = add_reliability_columns(clean_census_sentinels(_raw()))
        self.assertEqual(out["B01003_001_SE"].iloc[0], 0.0)
        self.assertEqual(out["B01003_001_CV"].iloc[0], 0.0)

    def test_controlled_column_name(self):
        self.assertEqual(controlled_column("B01003_001M"), "B01003_001_CONTROLLED")
        self.assertEqual(controlled_column("DP05_0001PM"), "DP05_0001P_CONTROLLED")
        self.assertIsNone(controlled_column("B01003_001E"))


class TestPanelColumns(unittest.TestCase):
    def test_classified_and_slugged(self):
        self.assertEqual(classify_column("B01003_001_CONTROLLED"), ("B01003_001", "CONTROLLED", False))
        self.assertEqual(classify_column("DP05_0001P_CONTROLLED"), ("DP05_0001", "CONTROLLED", True))
        self.assertEqual(measurement_type("B01003_001_CONTROLLED"), "Controlled Flag")
        slugs = make_slug_map(["B01003_001E", "B01003_001M", "B01003_001_CONTROLLED"], {}, style="code")
        self.assertTrue(slugs["B01003_001_CONTROLLED"].endswith("_controlled"))


class TestF1AcceptsRepresentation(unittest.TestCase):
    def test_f1_checks_controlled_cells(self):
        res = V.run_battery(checks=["F1"])[0]
        self.assertEqual(res.status, V.STATUS_PASSED, res.failures[:3])
        for fmt in ("panel", "parquet"):
            self.assertGreater(res.details["formats"][fmt]["controlled_moe_cells"], 0)


if __name__ == "__main__":
    unittest.main()
