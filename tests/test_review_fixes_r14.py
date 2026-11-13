# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the Round 14 code-review findings (branch round14/review-fixes)."""

import json
import os
import shutil
import tempfile
import unittest
import warnings
from unittest import mock

import numpy as np
import pandas as pd

from census import manifest as M
from tests.test_manifest_multistate import REP_COLS, TwoStateClient, two_state_manifest


class _Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_m(self, m, client=None, **kw):
        number = getattr(self, "_run_number", 0)
        self._run_number = number + 1
        name = "run" if number == 0 else f"run-{number}"
        return M.run_manifest(m, os.path.join(self.dir, name), client=client or TwoStateClient(),
                              use_cache=False, **kw)

    def probe(self):
        return self.run_m(two_state_manifest(adjustments=None))["frame"]

    @staticmethod
    def replicates(frame, codes=None, seed=11):
        """Replicates of income, total and college; ``codes`` maps name -> replicate variable label."""
        rng = np.random.default_rng(seed)
        parts = []
        for name in ("income", "total", "college"):
            est = frame[name].to_numpy(float)
            part = pd.DataFrame({"GEOID": frame["GEOID"], "variable": (codes or {}).get(name, name),
                                 "estimate": est})
            reps = est[:, None] * (1 + rng.normal(0, 0.02, (len(est), 80)))
            parts.append(pd.concat([part, pd.DataFrame(reps, columns=REP_COLS)], axis=1))
        return pd.concat(parts, ignore_index=True)


# 1. replicate derivation errors are deviations, not exceptions
class TestDeriveReplicatesErrors(_Run):
    def test_duplicate_replicate_rows_recorded_not_raised(self):
        reps = self.replicates(self.probe())
        reps = pd.concat([reps, reps[reps["variable"] == "college"].head(1)], ignore_index=True)
        res = self.run_m(two_state_manifest(), replicates=reps)  # _rep_cube raises ValueError
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        dev = [d for d in res["deviations"] if d["kind"] == "replicates_incomplete"
               and "derivations on replicates" in str(d.get("detail"))]
        self.assertTrue(dev, res["deviations"])
        self.assertIn("duplicate rows", dev[0]["detail"])
        self.assertTrue(os.path.exists(os.path.join(res["output_dir"], "bundle", "bundle.json")))

    def test_key_error_recorded_not_raised(self):
        reps = self.replicates(self.probe())
        with mock.patch("census.analysis.attach_replicates", side_effect=KeyError("rep_7")):
            res = self.run_m(two_state_manifest(), replicates=reps)
        self.assertIn("replicates_incomplete", {d["kind"] for d in res["deviations"]})
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")

    def test_mcp_wrapper_survives_key_error(self):
        from census import workflows_inference as W

        path = os.path.join(self.dir, "m.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(two_state_manifest(), f)
        with mock.patch("census.manifest.run_manifest", side_effect=KeyError("boom")):
            out = W.run_manifest(path, os.path.join(self.dir, "out"))
        self.assertFalse(out["success"])
        self.assertIn("boom", out["error"])


# 2. inputs given by ACS code with an E/PE suffix
class TestDeriveByCodeSuffix(_Run):
    def test_suffixed_codes_match_normalized_replicate_names(self):
        frame = self.probe()
        reps = self.replicates(frame, codes={"income": "B19013_001E", "total": "B15002_001E",
                                             "college": "B15002_015E"})
        m = two_state_manifest(variables=[{"name": "income", "ref": "2024:B19013_001E"},
                                          {"name": "total", "ref": "2024:B15002_001E"},
                                          {"name": "college", "ref": "2024:B15002_015E"}])
        _, _, derived = M._derive_replicates(frame, reps, m)
        self.assertEqual(list(derived), ["pct"])

    def test_percent_code(self):
        frame = self.probe()
        reps = self.replicates(frame, codes={"total": "DP02_0001E", "college": "DP02_0068PE"})
        m = two_state_manifest(variables=[{"name": "income", "ref": "2024:B19013_001"},
                                          {"name": "total", "ref": "2024:DP02_0001E"},
                                          {"name": "college", "ref": "2024:DP02_0068PE"}])
        _, _, derived = M._derive_replicates(frame, reps, m)
        self.assertEqual(list(derived), ["pct"])


# 3. derived_on_replicates lists only derivations that received replicate SEs, with counts
class TestDerivedOnReplicatesCounts(_Run):
    def test_no_rows_with_replicates_not_listed(self):
        frame = self.probe()
        reps = self.replicates(frame)
        sel = reps["variable"] == "college"
        reps.loc[sel, "GEOID"] = [f"99{i:03d}" for i in range(int(sel.sum()))]  # no unit has college replicates
        _, _, derived = M._derive_replicates(frame, reps, two_state_manifest())
        self.assertEqual(dict(derived), {})

    def test_partial_counts_and_methods_text(self):
        frame = self.probe()
        reps = self.replicates(frame)
        drop = reps["GEOID"].isin(["26001", "26003", "36001"]) & (reps["variable"] == "college")
        res = self.run_m(two_state_manifest(), replicates=reps[~drop].reset_index(drop=True))
        info = res["replicates"]
        self.assertEqual(info["derived_on_replicates"], ["pct"])
        self.assertEqual(info["derived_on_replicates_rows"], {"pct": {"rows_replicate": 21, "rows": 24}})
        with open(os.path.join(res["output_dir"], "bundle", "results", "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        text = M.methods_section(two_state_manifest(), run)
        self.assertIn("replicate SE for 21 of 24 units; approximate SE (ACS formulas, Handbook ch. 8) elsewhere",
                      text)

    def test_full_coverage_text(self):
        frame = self.probe()
        res = self.run_m(two_state_manifest(), replicates=self.replicates(frame))
        self.assertEqual(res["replicates"]["derived_on_replicates_rows"], {"pct": {"rows_replicate": 24,
                                                                                    "rows": 24}})
        with open(os.path.join(res["output_dir"], "bundle", "results", "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        self.assertIn("SE from the derivation evaluated on each replicate (all 24 units)",
                      M.methods_section(two_state_manifest(), run))


def _errs(m):
    return M.validate_manifest(m, resolve=False)


# 4-5. geography.states validation
class TestStatesValidation(unittest.TestCase):
    def test_one_digit_state_rejected(self):
        for states in (["6", "06"], ["6", "36"], ["026", "36"], [" 26", "36"]):
            errs = _errs(two_state_manifest(geography={"level": "county", "states": states}))
            self.assertTrue(any("exactly 2 digits" in e for e in errs), (states, errs))

    def test_one_digit_single_state_rejected(self):
        errs = _errs(two_state_manifest(geography={"level": "county", "state": "6"}, adjustments=None))
        self.assertTrue(any("exactly 2 digits" in e for e in errs), errs)
        self.assertEqual(_errs(two_state_manifest(geography={"level": "county", "state": "all"},
                                                  adjustments=None)), [])

    def test_zcta_with_states_rejected(self):
        errs = _errs(two_state_manifest(geography={"level": "zcta", "states": ["26", "36"]}, adjustments=None))
        self.assertTrue(any("zcta" in e.lower() and "cross state lines" in e for e in errs), errs)

    def test_example_hashes_unchanged(self):
        from tests.test_manifest_multistate import MANIFEST, MANIFEST_HASH
        from tests.test_reference_workflow import MANIFEST as SINGLE, MANIFEST_HASH as SINGLE_HASH

        for path, h in ((MANIFEST, MANIFEST_HASH), (SINGLE, SINGLE_HASH)):
            m = M.load_manifest(path)
            self.assertEqual(M.validate_manifest(m), [])
            self.assertEqual(M.manifest_hash(m), h)


# 6. rpp_year must be explicit; create_manifest fills it before hashing
class TestRppYearExplicit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_validation_requires_rpp_year(self):
        m = two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"]}])
        errs = _errs(m)
        self.assertTrue(any("rpp_year is required" in e for e in errs), errs)

    def test_create_fills_latest_year_before_hash(self):
        m = two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"]}])
        path = os.path.join(self.dir, "m.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f)
        res = M.create_manifest(path, resolve=False)
        self.assertTrue(res["valid"], res["errors"])
        year = M.latest_rpp_year()
        self.assertEqual(res["filled"], [{"adjustment": 0, "rpp_year": year}])
        self.assertTrue(any("rpp_year" in n and str(year) in n for n in res["notes"]))
        with open(path, encoding="utf-8") as f:  # the input file is never overwritten
            self.assertEqual(json.load(f), m)
        completed = os.path.join(self.dir, "m.completed.json")
        self.assertEqual(res["path"], completed)
        written = M.load_manifest(completed)
        self.assertEqual(written["adjustments"][0]["rpp_year"], year)
        self.assertEqual(M.manifest_hash(written), res["hash"])
        self.assertEqual(_errs(written), [])

    def test_create_dict_returns_filled_manifest(self):
        m = two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"]}])
        res = M.create_manifest(m, resolve=False)
        self.assertTrue(res["valid"], res["errors"])
        self.assertEqual(res["manifest"]["adjustments"][0]["rpp_year"], M.latest_rpp_year())
        self.assertEqual(M.manifest_hash(res["manifest"]), res["hash"])
        self.assertNotIn("rpp_year", m["adjustments"][0])  # caller's dict untouched


# 7. exclusions before adjustments
class TestExclusionsBeforeAdjustments(_Run):
    @staticmethod
    def membership(skip=()):
        from census import workflows_microdata

        def lookup(geoids, year):
            missing = sorted(set(geoids) & set(skip))
            if missing:
                raise ValueError(f"no county boundaries for {missing}")
            idx = sorted(set(geoids))
            return pd.DataFrame({"CBSA": [np.nan] * len(idx), "LSAD": [np.nan] * len(idx)},
                                index=pd.Index(idx, name="GEOID")), "test"

        return mock.patch.object(workflows_microdata, "_county_rpp_areas", side_effect=lookup)

    def manifest(self, exclude=True):
        return two_state_manifest(
            adjustments=[{"type": "rpp", "variables": ["income"], "rpp_year": 2023, "areas": "county_metro"}],
            exclusions=[{"rule": "geoid_exclude", "geoids": ["26001"]}] if exclude else [])

    def test_excluded_unit_needs_no_rpp_area(self):
        with self.membership(skip=("26001",)):
            res = self.run_m(self.manifest())
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        self.assertNotIn("26001", set(res["frame"]["GEOID"]))
        self.assertEqual(res["adjustments"][0]["rows"], 23)

    def test_unit_without_rpp_area_still_blocks(self):
        with self.membership(skip=("26001",)):
            res = self.run_m(self.manifest(exclude=False))
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        self.assertIn("adjustment_failed", {d["kind"] for d in res["deviations"]})

    def test_cv_screen_unchanged_by_order(self):
        thr = float(np.median(self.probe()["income_cv"]))  # CV is scale invariant: same rows either way
        m = two_state_manifest(exclusions=[{"rule": "cv_screen", "threshold": thr, "variables": ["income"],
                                            "action": "exclude"}])
        res = self.run_m(m)
        unadj = self.run_m(two_state_manifest(adjustments=None, exclusions=m["exclusions"]))
        self.assertEqual(sorted(res["frame"]["GEOID"]), sorted(unadj["frame"]["GEOID"]))
        self.assertLess(len(res["frame"]), 24)

    def test_methods_text_order(self):
        text = M.methods_section(two_state_manifest(exclusions=[{"rule": "geoid_exclude", "geoids": ["26001"]}]))
        self.assertLess(text.index("pre-specified GEOIDs were excluded"), text.index("Price adjustment"))


# 8. PUMS medians: one sort of the group ids, one gather per group
class TestPumsMedianGroups(unittest.TestCase):
    def frame(self, n=3000, groups=150, seed=5):
        from tests.test_pums import synthetic

        df = synthetic(n=n, seed=seed)
        rng = np.random.default_rng(seed)
        df["g"] = rng.integers(0, groups, n)
        df["x"] = np.round(df["x"])  # ties
        df.loc[rng.random(n) < 0.1, "x"] = np.nan
        return df

    def test_many_groups_identical_and_gathered_once(self):
        from census import pums as P
        from tests.test_pums import REPS, _weighted_median_v0_12

        df = self.frame()
        seen = []
        real = P.weighted_median

        def spy(values, weights):
            seen.append(len(values))
            return real(values, weights)

        with mock.patch.object(P, "weighted_median", side_effect=spy):
            t = P.tabulate(df, "median", "x", by="g", return_replicates=True)
        defined = int(df["x"].notna().sum())
        self.assertEqual(sum(seen), defined)  # each group's rows only, not G x N
        for _, row in t.iterrows():
            sub = df[(df.g == row.g) & df.x.notna()]
            ref = _weighted_median_v0_12(sub.x.to_numpy(), sub[["PWGTP"] + REPS].to_numpy(np.float64))
            got = np.array([row.estimate] + [row[f"rep_{i}"] for i in range(1, 81)])
            self.assertTrue(np.array_equal(got, ref, equal_nan=True), row.g)

    def test_binned_many_groups_identical(self):
        from census import pums as P
        from tests.test_pums import REPS

        df = self.frame(n=1500, groups=60, seed=9)
        bins = [-np.inf, 5, 8, 10, 12, 15, np.inf]
        t = P.tabulate(df, "median", "x", by="g", bins=bins, return_replicates=True)
        for _, row in t.iterrows():
            sub = df[(df.g == row.g) & df.x.notna()]
            ref = P.binned_median(sub.x.to_numpy(), sub[["PWGTP"] + REPS].to_numpy(np.float64), bins)
            got = np.array([row.estimate] + [row[f"rep_{i}"] for i in range(1, 81)])
            self.assertTrue(np.array_equal(got, ref, equal_nan=True), row.g)


# 9. one owner of the default RPP areas rule
class TestDefaultRppAreas(unittest.TestCase):
    def test_helper(self):
        self.assertEqual(M.default_rpp_areas("county"), "county_metro")
        self.assertEqual(M.default_rpp_areas("tract"), "state")

    def test_validation_and_execution_use_helper(self):
        m = two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"], "rpp_year": 2023}])
        with mock.patch.object(M, "default_rpp_areas", return_value="bogus") as helper:
            errs = _errs(m)
        self.assertTrue(helper.called)
        self.assertTrue(any("areas must be" in e for e in errs), errs)

    def test_place_adjust_frame_bad_areas_reported_once(self):
        from census import workflows_microdata as WM

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "frame.csv")
        pd.DataFrame({"GEOID": ["26001"], "income": [1.0], "income_se": [0.1]}).to_csv(path, index=False)
        out = WM.place_adjust_frame(path, ["income"], 2024, areas="msa")
        self.assertFalse(out["success"])
        self.assertIn("areas must be one of", out["error"])


# 10. export relabelling without a zero-row slice
class TestFormatDataframeNoSlice(unittest.TestCase):
    def test_no_slice_same_object(self):
        from census.export import format_dataframe

        df = pd.DataFrame([[1, 2, 3]], columns=["NAME", "B01001_001E", "B01001_002E"])
        with warnings.catch_warnings(), \
                mock.patch.object(pd.DataFrame, "iloc", new_callable=mock.PropertyMock,
                                  side_effect=AssertionError("data sliced")):
            warnings.simplefilter("error")
            out = format_dataframe(df, "B01001", variable_descriptions={"B01001_001E": "Total"})
        self.assertIs(out, df)
        self.assertEqual(list(df.columns), ["NAME", "Total", "B01001_002E"])


if __name__ == "__main__":
    unittest.main()
