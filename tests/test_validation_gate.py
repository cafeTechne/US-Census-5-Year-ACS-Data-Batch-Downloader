# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release-gate semantics of scripts/inference_validation.py (PLAN_ROUND_12 A2, decision P12-3).

Synthetic results frames only: no simulation is run. The gate fails on any
gated FAIL row or a missing required study; info rows never fail it; the
default command stays a report (exit 0).
"""

import importlib.util
import os
import sys
import unittest
from unittest import mock

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "inference_validation.py")
_spec = importlib.util.spec_from_file_location("inference_validation", SCRIPT)
iv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iv)

REQ = ("alpha", "beta")


def _row(study, status, method="m", target="t"):
    return {"study": study, "method": method, "target": target, "reps": 100, "coverage": 0.9, "mc_se": 0.03,
            "status": status}


def _df(*rows):
    return pd.DataFrame(list(rows), columns=["study", "method", "target", "reps", "coverage", "mc_se", "status"])


class TestGate(unittest.TestCase):
    def test_all_required_pass(self):
        ok, problems = iv.gate(_df(_row("alpha", "pass"), _row("beta", "pass")), REQ)
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_gated_fail_fails(self):
        ok, problems = iv.gate(_df(_row("alpha", "pass"), _row("beta", "FAIL", target="slope")), REQ)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)
        self.assertIn("beta", problems[0])
        self.assertIn("slope", problems[0])

    def test_info_rows_never_fail(self):
        # Info rows (any coverage, in required or optional studies) do not fail the gate.
        df = _df(_row("alpha", "pass"), _row("alpha", "info"), _row("beta", "pass"), _row("simex", "info"))
        df.loc[df["status"] == "info", "coverage"] = 0.2
        self.assertTrue(iv.gate(df, REQ)[0])

    def test_missing_required_study_fails(self):
        ok, problems = iv.gate(_df(_row("alpha", "pass")), REQ)
        self.assertFalse(ok)
        self.assertIn("beta: required study missing", problems[0])

    def test_required_study_with_only_info_rows_counts_as_missing(self):
        ok, problems = iv.gate(_df(_row("alpha", "pass"), _row("beta", "info")), REQ)
        self.assertFalse(ok)
        self.assertIn("beta", problems[0])

    def test_empty_results_fail_every_required_study(self):
        ok, problems = iv.gate(_df(), REQ)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 2)

    def test_default_required_set_matches_gated_studies_registered(self):
        from census.simulation import STUDIES
        self.assertTrue(set(iv.REQUIRED_STUDIES) <= set(STUDIES))


class TestExitCodes(unittest.TestCase):
    """main(): report-only by default (exit 0 even on FAIL), --gate exits 1."""

    def _main(self, argv, df):
        with mock.patch.object(iv, "run_study", return_value=df), \
                mock.patch.object(sys, "argv", ["inference_validation.py", "--fast", *argv]), \
                mock.patch("builtins.print"):
            return iv.main()

    def _all_pass(self):
        return _df(*[_row(s, "pass") for s in iv.REQUIRED_STUDIES])

    def test_report_mode_exits_zero_on_failure(self):
        self.assertEqual(self._main([], _df(_row("fit_replicate", "FAIL"))), 0)

    def test_gate_mode(self):
        self.assertEqual(self._main(["--gate"], self._all_pass()), 0)
        failing = self._all_pass()
        failing.loc[0, "status"] = "FAIL"
        self.assertEqual(self._main(["--gate"], failing), 1)
        self.assertEqual(self._main(["--gate"], self._all_pass().iloc[1:]), 1)  # one required study missing


if __name__ == "__main__":
    unittest.main()
