# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live validation battery against api.census.gov and www2.census.gov.

Skipped unless CENSUS_LIVE_TESTS=1 (CENSUS_API_KEY is read from the
environment or .env by census.api). Same checks and tolerances as
tests/test_validation.py; fixtures are not modified.

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_validation_live.py
"""

import os
import unittest

from census import validation as V

LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census validation")
class TestLiveBattery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {r.check_id: r for r in V.run_battery(live=True, record=False)}

    def test_no_errors(self):
        for r in self.results.values():
            self.assertNotEqual(r.status, V.STATUS_ERROR, f"{r.check_id}: {r.notes}")

    def test_checks_pass(self):
        # F5 has a documented medians.py discrepancy (see tests/test_validation.py).
        for cid in ("F1", "F2", "F3", "F4", "F6", "F7", "F8", "F9", "F10", "F11"):
            with self.subTest(check=cid):
                r = self.results[cid]
                self.assertTrue(r.passed, f"{cid}: {r.notes} {r.failures[:3]}")

    def test_f5_passes(self):
        self.assertTrue(self.results["F5"].passed)


if __name__ == "__main__":
    unittest.main()
