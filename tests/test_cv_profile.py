# SPDX-License-Identifier: AGPL-3.0-or-later
"""The simulation tract CV profile matches real tract MOEs (Round 9, G3)."""

import hashlib
import json
import os
import unittest

import numpy as np

from census.simulation import CV_PROFILES, derive_cv_profile, draw_cv

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "cv_profile", "tract_moes_2023.csv")


class TestTractCVProfile(unittest.TestCase):
    def test_fixture_sidecar(self):
        with open(FIXTURE + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        with open(FIXTURE, "rb") as f:
            raw = f.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertTrue(all(u.startswith("https://api.census.gov/data/2023/acs/acs5?") for u in meta["source_urls"]))
        self.assertNotIn(b"key=", raw)
        self.assertFalse(any("key=" in u for u in meta["source_urls"]))

    def test_profile_matches_fixture(self):
        derived = derive_cv_profile(FIXTURE)
        stored = CV_PROFILES["tract"]
        self.assertGreater(derived["n_pairs"], 1000)
        self.assertAlmostEqual(stored["median"], derived["median"], delta=0.05)
        self.assertAlmostEqual(stored["sigma"], derived["sigma"], delta=0.005)
        self.assertAlmostEqual(stored["clip"][0], derived["clip"][0], delta=0.05)
        self.assertAlmostEqual(stored["clip"][1], derived["clip"][1], delta=0.05)
        self.assertIn("cv_profile", stored["source"])

    def test_draws_reproduce_quartiles(self):
        cv = draw_cv(200_000, "tract", np.random.default_rng(1))
        q25, q50, q75 = np.percentile(cv, [25, 50, 75])
        d = derive_cv_profile(FIXTURE)["quartiles"]
        self.assertAlmostEqual(q50, d[1], delta=0.5)
        self.assertAlmostEqual(q25, d[0], delta=1.0)
        # Real tract log-CVs are right-skewed (q75/q50 1.95 vs q50/q25 1.60); the
        # symmetric log-normal fit puts q75 about 10% low (23.5 vs 26.2).
        self.assertAlmostEqual(q75, d[2], delta=3.0)


if __name__ == "__main__":
    unittest.main()
