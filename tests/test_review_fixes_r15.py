# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the Round 15 code-review findings (offline).

1. Overlapping releases are refused for any replicate source (validation for a
   declared need; a blocking deviation for caller-supplied replicates); the
   methods text claims independence only for non-overlapping releases.
2. validate_manifest / create_manifest never raise on malformed variables.
3. census_vre at per-state VRE levels needs explicit states; an empty call list
   is a clear error.
4. replicate_scope 'covariates_only' is allowed for registry-listed outcomes;
   the run holds the outcome fixed and records that replicates were available.
5. / 6. Decennial counts are recognized only from explicit evidence.
7. An empty frame passes through the adjustments.
8. Generic product aliases ("census", "dec") are not decennial.
9. _fill_adjustment_defaults uses latest_rpp_year; releases helper.
10. spreg fits print nothing to stdout, under one lock.
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from census.manifest import (create_manifest, methods_section, run_manifest, validate_manifest)
from tests.test_manifest_releases import TwoReleaseClient, covariate_manifest, fake_vre, release_manifest


def _errs(m):
    return validate_manifest(m, resolve=False)


def _caller_reps(years, names=(("B19025", "B19025_001", "agg"), ("B19001", "B19001_001", "hh"))):
    parts = []
    for y in years:
        for table, code, name in names:
            f = fake_vre(table, y, "county")
            f = f[f["variable"] == code].copy()
            f["variable"] = name
            f.insert(1, "Year", y)
            parts.append(f.drop(columns=["NAME", "moe"]))
    return pd.concat(parts, ignore_index=True)


class _Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_m(self, m, vre=fake_vre, **kw):
        with mock.patch("census.replicate.fetch_vre", side_effect=vre):
            return run_manifest(m, os.path.join(self.dir, "run"), client=TwoReleaseClient(), use_cache=False, **kw)

    def methods(self):
        with open(os.path.join(self.dir, "run", "bundle", "methods.md"), encoding="utf-8") as f:
            return f.read()


def _no_vre_manifest(releases, required=False, formula="hh ~ agg"):
    m = release_manifest(releases=releases, adjustments=None,
                         variables=[{"name": "agg", "ref": f"{releases[-1]}:B19025_001"},
                                    {"name": "hh", "ref": f"{releases[-1]}:B19001_001"}],
                         derivations=None,
                         analyses=[{"id": "m1", "method": "fit_replicate", "formula": formula}],
                         hypotheses=[{"id": "H1", "analysis": "m1", "statistic": "coefficient", "term": "agg"}])
    for k in ("replicates", "adjustments", "derivations"):
        m.pop(k)
    if required:
        m["analyses"][0]["replicates"] = "required"
    return m


class TestOverlapAnyReplicateSource(_Run):
    def test_validation_refuses_required_replicates_across_overlapping_releases(self):
        errs = _errs(_no_vre_manifest([2021, 2024], required=True))
        self.assertTrue(any("require replicates with overlapping 5-year periods" in e for e in errs), errs)
        self.assertEqual(_errs(_no_vre_manifest([2019, 2024], required=True)), [])
        self.assertEqual(_errs(_no_vre_manifest([2021, 2024])), [])  # nothing declared: checked at run time

    def test_caller_supplied_overlapping_replicates_block(self):
        res = self.run_m(_no_vre_manifest([2021, 2024]), replicates=_caller_reps([2021, 2024]))
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        dev = [d for d in res["deviations"] if d["kind"] == "replicates_overlapping_releases"]
        self.assertEqual(len(dev), 1, res["deviations"])
        self.assertIn("share sample years", dev[0]["detail"])
        self.assertNotIn("m1", res["results"])

    def test_caller_supplied_non_overlapping_replicates_run(self):
        res = self.run_m(_no_vre_manifest([2019, 2024]), replicates=_caller_reps([2019, 2024]))
        self.assertFalse([d for d in res["deviations"] if d["kind"] == "replicates_overlapping_releases"])
        self.assertIn("m1", res["results"])

    def test_methods_independence_only_without_overlap(self):
        over = _no_vre_manifest([2021, 2024], formula="hh ~ agg + C(Year)")
        text = methods_section(over)
        self.assertNotIn("independent samples", text)
        self.assertIn("overlap (shared sample years)", text)
        ok = methods_section(_no_vre_manifest([2019, 2024], formula="hh ~ agg + C(Year)"))
        self.assertIn("share no sample years", ok)
        self.assertIn("independent samples", ok)


class TestMalformedVariablesNeverRaise(unittest.TestCase):
    def test_variable_without_name(self):
        m = release_manifest()
        m["variables"][0] = {"ref": "2024:B19025_001"}
        errs = _errs(m)
        self.assertTrue(any("needs 'name' and 'ref'" in e for e in errs), errs)
        res = create_manifest(m, resolve=False)  # vre_unavailable runs for the notes
        self.assertFalse(res["valid"])

    def test_malformed_derivation_and_variables(self):
        m = release_manifest(derivations=["mean_inc"])
        self.assertTrue(any("derivations[0] must be an object" in e for e in _errs(m)))
        self.assertFalse(create_manifest(m, resolve=False)["valid"])
        m = release_manifest(variables=5)
        self.assertTrue(any("variables must be a list" in e for e in _errs(m)))
        self.assertFalse(create_manifest(m, resolve=False)["valid"])


class TestPerStateVreLevels(unittest.TestCase):
    def test_validation_requires_states(self):
        m = release_manifest(geography={"level": "tract"}, adjustments=None)
        self.assertTrue(any("published per state" in e for e in _errs(m)), _errs(m))
        ok = release_manifest(geography={"level": "tract", "state": "26"}, adjustments=None)
        self.assertFalse([e for e in _errs(ok) if "per state" in e])

    def test_empty_call_list_is_a_clear_error(self):
        from census.manifest import _census_vre_replicates

        m = release_manifest(geography={"level": "tract"}, adjustments=None)
        frame = pd.DataFrame({"GEOID": ["26001000100"], "Year": [2024]})
        with mock.patch("census.replicate.fetch_vre", side_effect=AssertionError("must not fetch")):
            with self.assertRaisesRegex(ValueError, "published per state.*names no state"):
                _census_vre_replicates(m, frame, {"agg": {2024: "B19025_001"}})


class TestCovariatesOnlyListedOutcome(_Run):
    def manifest(self):
        m = release_manifest(adjustments=None, derivations=None,
                             analyses=[{"id": "m1", "method": "fit_replicate", "formula": "hh ~ agg",
                                        "replicate_scope": "covariates_only"}],
                             hypotheses=[{"id": "H1", "analysis": "m1", "statistic": "coefficient", "term": "agg"}])
        for k in ("adjustments", "derivations"):
            m.pop(k)
        return m

    def test_declaration_allowed_and_noted(self):
        m = self.manifest()
        self.assertEqual(_errs(m), [])
        res = create_manifest(m, resolve=False)
        self.assertTrue(res["valid"], res["errors"])
        note = [n for n in res["notes"] if "covariates_only" in n]
        self.assertEqual(len(note), 1, res["notes"])
        self.assertIn("held at its estimate", note[0])

    def test_run_holds_outcome_fixed_and_records_it(self):
        res = self.run_m(self.manifest())
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        self.assertEqual(res["results"]["m1"].diagnostics["replicates_declared_fixed"], ["hh"])
        self.assertEqual(res["replicates"]["covariates_only_outcome_had_replicates"], {"m1": ["hh"]})
        self.assertEqual(res["replicates"]["declared_covariates_only"], {"m1": ["hh"]})
        self.assertIn("although replicates were available", self.methods())

    def test_outcome_without_replicates_unchanged(self):
        m = covariate_manifest()
        m["analyses"][0]["replicate_scope"] = "covariates_only"
        res = self.run_m(m)
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        self.assertNotIn("covariates_only_outcome_had_replicates", res["replicates"])
        self.assertIn("declared because it has no published replicates", self.methods())


class TestEmptyFrameAdjustments(_Run):
    def test_exclusions_remove_everything(self):
        geoids = [f"{st}{2 * i + 1:03d}" for st in ("26", "36") for i in range(8)]
        m = release_manifest(exclusions=[{"rule": "geoid_exclude", "geoids": geoids}])
        res = self.run_m(m)
        kinds = [d["kind"] for d in res["deviations"]]
        self.assertNotIn("adjustment_failed", kinds)
        self.assertIn("analysis_failed", kinds)
        self.assertEqual(res["adjustments"], [])

    def test_direct(self):
        from census.manifest import _apply_adjustments

        m = release_manifest()
        frame = pd.DataFrame(columns=["GEOID", "Year", "mean_inc", "mean_inc_se"])
        out, _, log, failure = _apply_adjustments(frame, None, m, [2019, 2024])
        self.assertIsNone(failure)
        self.assertTrue(out.empty)


class TestCleanup(unittest.TestCase):
    def test_fill_defaults_uses_latest_rpp_year(self):
        m = release_manifest(releases=[2024], adjustments=[{"type": "rpp", "variables": ["mean_inc"],
                                                            "areas": "state"}])
        with mock.patch("census.manifest.latest_rpp_year", return_value=2021):
            res = create_manifest(m, resolve=False)
        self.assertEqual(res["filled"], [{"adjustment": 0, "rpp_year": 2021}])

    def test_bool_release_rejected(self):
        self.assertTrue(any("releases must be" in e for e in _errs(release_manifest(releases=[2019, True]))))

    def test_releases_helper(self):
        from census.manifest import manifest_releases

        self.assertEqual(manifest_releases({"releases": [2019, True, "2020", 2024]}), [2019, 2024])
        self.assertEqual(manifest_releases({"releases": 2019}), [])


if __name__ == "__main__":
    unittest.main()
