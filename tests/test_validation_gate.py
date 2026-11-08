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

    def test_did_ar1_sensitivity_runs_only_on_demand(self):
        # D13-3: registered and runnable with --studies, never gated or run by default
        from census.simulation import DESIGN_STUDIES, STUDIES
        self.assertIn("did_ar1_sensitivity", DESIGN_STUDIES)
        self.assertIn("did_ar1_sensitivity", STUDIES)
        self.assertNotIn("did_ar1_sensitivity", iv.DEFAULT_DESIGN_STUDIES)
        self.assertNotIn("did_ar1_sensitivity", iv.REQUIRED_STUDIES)


class TestFastSmokeGate(unittest.TestCase):
    """--fast --gate (PLAN_ROUND_13 C1): band widened by 3 MC SE at nominal; full gate unchanged."""

    # reps=100: MC SE at nominal = sqrt(0.9 * 0.1 / 100) = 0.03
    def _one(self, coverage, status="FAIL", mc_se=0.03, reps=100):
        r = _row("alpha", status)
        r.update(coverage=coverage, mc_se=mc_se, reps=reps)
        return _df(r, _row("beta", "pass"))

    def test_two_se_outside_passes_fast_fails_full(self):
        for cov in (0.92 + 2 * 0.03, 0.88 - 2 * 0.03):
            df = self._one(cov)
            self.assertTrue(iv.gate(df, REQ, fast=True)[0], cov)
            self.assertFalse(iv.gate(df, REQ)[0], cov)

    def test_four_se_outside_fails_both(self):
        df = self._one(0.88 - 4 * 0.03)
        ok, problems = iv.gate(df, REQ, fast=True)
        self.assertFalse(ok)
        self.assertIn("fast-smoke band", problems[0])
        self.assertFalse(iv.gate(df, REQ)[0])

    def test_missing_study_fails_both(self):
        for fast in (True, False):
            ok, problems = iv.gate(_df(_row("alpha", "pass")), REQ, fast=fast)
            self.assertFalse(ok)
            self.assertIn("beta: required study missing", problems[0])

    def test_non_finite_coverage_fails_fast(self):
        for cov in (float("nan"), float("inf")):
            self.assertFalse(iv.gate(self._one(cov, status="pass"), REQ, fast=True)[0])

    def test_larger_reported_mc_se_is_used(self):
        # pooled rows (areas or terms sharing a fit): row reps overstate the information
        df = self._one(0.80, mc_se=0.04, reps=2400)
        self.assertTrue(iv.gate(df, REQ, fast=True)[0])
        self.assertFalse(iv.gate(self._one(0.80, mc_se=0.0, reps=2400), REQ, fast=True)[0])


class TestExitCodes(unittest.TestCase):
    """main(): report-only by default (exit 0 even on FAIL), --gate exits 1."""

    def _main(self, argv, df, fast=True):
        out = []
        with mock.patch.object(iv, "run_study", return_value=df), \
                mock.patch.object(sys, "argv", ["inference_validation.py", *(["--fast"] if fast else []), *argv]), \
                mock.patch.object(iv, "OUT_MD", None), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = iv.main()
        self.printed = "\n".join(out)
        return code

    def _all_pass(self):
        return _df(*[_row(s, "pass") for s in iv.REQUIRED_STUDIES])

    def test_report_mode_exits_zero_on_failure(self):
        self.assertEqual(self._main([], _df(_row("fit_replicate", "FAIL"))), 0)

    def test_gate_mode(self):
        self.assertEqual(self._main(["--gate"], self._all_pass()), 0)
        self.assertIn("gate (fast smoke, band widened by 3 MC SE at nominal): PASS", self.printed)
        self.assertIn("not release evidence", self.printed)
        failing = self._all_pass()
        failing.loc[0, ["status", "coverage"]] = ["FAIL", 0.5]
        self.assertEqual(self._main(["--gate"], failing), 1)
        self.assertIn("gate (fast smoke", self.printed)
        self.assertEqual(self._main(["--gate"], self._all_pass().iloc[1:]), 1)  # one required study missing

    def test_full_gate_label_and_strict_band(self):
        near = self._all_pass()
        near.loc[0, ["status", "coverage"]] = ["FAIL", 0.93]  # inside the fast tolerance, outside the band
        self.assertEqual(self._main(["--gate"], near, fast=False), 1)  # OUT_MD patched to None: no files
        self.assertIn("gate: FAIL", self.printed)
        self.assertNotIn("fast smoke", self.printed)
        self.assertEqual(self._main(["--gate"], near), 0)


if __name__ == "__main__":
    unittest.main()


def test_default_design_studies_are_required_and_registered():
    import importlib.util
    import os

    from census.simulation import DESIGN_STUDIES

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "inference_validation.py")
    spec = importlib.util.spec_from_file_location("inference_validation_check", path)
    iv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(iv)
    # Default-method multilevel and DiD studies gate releases; replicate variants stay on demand.
    assert set(iv.DEFAULT_DESIGN_STUDIES) <= set(iv.REQUIRED_STUDIES)
    assert set(iv.DEFAULT_DESIGN_STUDIES) <= set(DESIGN_STUDIES)
    assert "did_replicate" not in iv.REQUIRED_STUDIES and "multilevel_replicate" not in iv.REQUIRED_STUDIES


class TestParallelJobs(unittest.TestCase):
    """--jobs (PLAN_ROUND_13 C3): worker processes reproduce the serial frames exactly."""

    def test_parallel_matches_serial(self):
        groups = [("main", ["linear_trend", "correlation"]), ("designs", ["did_event"])]
        serial = iv._run_groups(groups, True, 1, lambda m: None)
        par = iv._run_groups(groups, True, 2, lambda m: None)
        a = pd.concat(serial, ignore_index=True).drop(columns="seconds")
        b = pd.concat(par, ignore_index=True).drop(columns="seconds")
        self.assertEqual(a.to_csv(index=False), b.to_csv(index=False))
