# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manifests across releases with price adjustment and Census VRE replicates
(PLAN_ROUND_15 workstream A).

- A1: several releases with a declared ``dollar_year`` / ``price_index`` /
  ``rpp_years`` (CPI to the dollar year first, then each release's own RPP;
  D15-2); validation, ``create_manifest`` defaults, and the arithmetic on a
  synthetic two-release, two-state fake client.
- A2: ``"replicates": {"source": "census_vre"}``: run_manifest fetches the VRE
  files itself (``census.replicate.fetch_vre``, mocked here), records URLs and
  SHA-256s, reports variables without published replicates, blocks them under
  the strict policy unless an analysis declares ``replicate_scope:
  "covariates_only"``; supplying replicates as well is an error; overlapping
  periods are refused.
- A3: the whole route of examples/manifests/mi_ny_income_change.json offline:
  ``cache import`` of frozen real Census API responses
  (tests/fixtures/mi_ny_change/, trimmed to the manifest's variables), the
  real Census VRE files trimmed to the Michigan and New York county rows of the
  manifest's lines (tests/fixtures/mi_ny_change/vre/, used as the VRE cache),
  and the frozen CBSA membership of tests/fixtures/mi_ny_manifest/, with
  network access refused; then ``python -m census tool run_manifest`` and the
  MCP tool.

Re-record the fixtures (live; needs CENSUS_API_KEY; never prints the key)::

    python -m tests.test_manifest_releases --record
"""

import contextlib
import copy
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import numpy as np
import pandas as pd

from census.api import CensusAPIClient
from census.manifest import (create_manifest, load_manifest, manifest_hash, methods_section, run_manifest,
                             validate_manifest)
from tests.test_manifest_multistate import patched_membership

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANIFEST = os.path.join(ROOT, "examples", "manifests", "mi_ny_income_change.json")
MANIFEST_HASH = "c420fcf08fcb0c758b050fea18229ffdbff6b0ec22335914c00b69558c923f8f"
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "mi_ny_change")
VRE_FIXTURES = os.path.join(FIXTURES, "vre")
STATES = ("26", "36")
RELEASES = (2019, 2024)
REP_COLS = [f"rep_{i}" for i in range(1, 81)]
LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"
# live result of the manifest (2026-11-08): change in RPP- and CPI-adjusted (2024 dollars)
# county mean household income, 2015-2019 -> 2020-2024, these_areas target (replicate SE)
LIVE_RESULT = {"estimate": 3110.80, "se": 244.32, "ci_lower": 2708.93, "ci_upper": 3512.67}


# ---------------------------------------------------------------------------
# synthetic two-release, two-state client and VRE files (no fixtures)
# ---------------------------------------------------------------------------

N = 8
VALUES = {  # (table, code) -> function of (state, year) -> estimates
    ("B19025", "B19025_001"): lambda st, y: np.linspace(1e8, 9e8, N) * (1.0 + 0.1 * (y - 2019) / 5) * (
        1.1 if st == "36" else 1.0),
    ("B19001", "B19001_001"): lambda st, y: np.linspace(2000, 15000, N).round(),
    ("B19013", "B19013_001"): lambda st, y: np.linspace(40000, 80000, N).round(),
}


def _payload(table, state, year):
    cols = {code: fn(state, year) for (t, code), fn in VALUES.items() if t == table}
    header = ["GEO_ID", "NAME"] + [f"{c}{s}" for c in cols for s in ("E", "EA", "M", "MA")] + ["state", "county"]
    rows = [header]
    for i in range(N):
        county = f"{2 * i + 1:03d}"
        vals = []
        for est in cols.values():
            vals += [f"{est[i]:.0f}", None, f"{max(est[i] * 0.03, 1):.0f}", None]
        rows.append([f"0500000US{state}{county}", f"County {i} {state}"] + vals + [state, county])
    return rows


class TwoReleaseClient(CensusAPIClient):
    def __init__(self):
        super().__init__(api_key="", cache=None)
        self.calls = []

    def fetch(self, year, state_code, table_code, geo_level="place", **kwargs):  # type: ignore[override]
        self.calls.append((table_code, state_code, year))
        self.last_error = None
        return _payload(table_code, state_code, year)

    def fetch_variable_metadata(self, year, table_code, dataset=None):  # type: ignore[override]
        return {}


def fake_vre(table, year, geo_level, state_code=None, client=None, **kw):
    """Synthetic replicate frame in the fetch_vre format for both states."""
    rng = np.random.default_rng(year + len(table))
    parts = []
    for st in STATES:
        payload = pd.DataFrame(_payload(table, st, year)[1:], columns=_payload(table, st, year)[0])
        for (t, code), _ in VALUES.items():
            if t != table:
                continue
            est = pd.to_numeric(payload[f"{code}E"]).to_numpy(float)
            reps = est[:, None] * (1 + rng.normal(0, 0.02, (len(est), 80)))
            part = pd.DataFrame({"GEOID": payload["state"] + payload["county"], "NAME": payload["NAME"],
                                 "variable": code, "estimate": est, "moe": est * 0.03})
            parts.append(pd.concat([part, pd.DataFrame(reps, columns=REP_COLS)], axis=1))
    out = pd.concat(parts, ignore_index=True)
    out.attrs.update(table=table, year=year, summary_level="050", state_code=state_code,
                     source_url=f"https://example.invalid/{year}/050/{table}.csv.zip", sha256=f"sha-{table}-{year}",
                     moe_check={"passed": True})
    return out


def release_manifest(**changes):
    m = {
        "schema_version": 1, "title": "Two-release synthetic test", "authors": ["agent:test"],
        "created": "2026-11-08T00:00:00Z", "releases": [2019, 2024],
        "geography": {"level": "county", "states": ["26", "36"]},
        "variables": [{"name": "agg", "ref": "2024:B19025_001"}, {"name": "hh", "ref": "2024:B19001_001"}],
        "derivations": [{"name": "mean_inc", "op": "ratio", "num": "agg", "den": "hh"}],
        "adjustments": [{"type": "rpp", "variables": ["mean_inc"], "areas": "state", "dollar_year": 2024,
                         "price_index": "r-cpi-u-rs", "rpp_years": {"2019": 2019, "2024": 2024}}],
        "replicates": {"source": "census_vre"},
        "analyses": [{"id": "m1", "method": "fit_replicate", "formula": "mean_inc ~ C(Year)",
                      "args": {"target": "these_areas"}}],
        "hypotheses": [{"id": "H1", "analysis": "m1", "statistic": "coefficient", "term": "C(Year)[T.2024]",
                        "direction": "greater"}],
    }
    m.update(changes)
    return m


def covariate_manifest(**changes):
    """Outcome B19013 (no published VRE) with a covariate that has replicates."""
    return release_manifest(
        variables=[{"name": "income", "ref": "2024:B19013_001"}, {"name": "agg", "ref": "2024:B19025_001"},
                   {"name": "hh", "ref": "2024:B19001_001"}],
        adjustments=[{"type": "rpp", "variables": ["income"], "areas": "state", "dollar_year": 2024,
                      "price_index": "r-cpi-u-rs", "rpp_years": {"2019": 2019, "2024": 2024}}],
        analyses=[{"id": "m1", "method": "fit_replicate", "formula": "income ~ hh"}],
        hypotheses=[{"id": "H1", "analysis": "m1", "statistic": "coefficient", "term": "hh"}], **changes)


class TestValidation(unittest.TestCase):
    def check(self, m, fragment):
        errs = validate_manifest(m, resolve=False)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_example_valid_and_hash(self):
        m = load_manifest(MANIFEST)
        self.assertEqual(validate_manifest(m), [])
        self.assertEqual(manifest_hash(m), MANIFEST_HASH)

    def test_round14_manifests_unchanged(self):
        from tests.test_manifest_multistate import MANIFEST as MINY, MANIFEST_HASH as MINY_HASH
        from tests.test_reference_workflow import MANIFEST as SINGLE, MANIFEST_HASH as SINGLE_HASH

        self.assertEqual(manifest_hash(load_manifest(MINY)), MINY_HASH)
        self.assertEqual(manifest_hash(load_manifest(SINGLE)), SINGLE_HASH)
        self.assertEqual(validate_manifest(load_manifest(MINY)), [])

    def test_multi_release_adjustment_errors(self):
        def adj(**kw):
            base = {"type": "rpp", "variables": ["mean_inc"], "areas": "state", "dollar_year": 2024,
                    "price_index": "r-cpi-u-rs", "rpp_years": {"2019": 2019, "2024": 2024}}
            base.update(kw)
            return release_manifest(adjustments=[{k: v for k, v in base.items() if v is not None}])

        self.assertEqual(validate_manifest(adj(), resolve=False), [])
        self.assertEqual(validate_manifest(adj(rpp_years={"2019": 2024, "2024": 2024}), resolve=False), [])
        self.check(adj(dollar_year=None, price_index=None), "dollar_year is required")
        self.check(adj(price_index=None), "price_index is required")
        self.check(adj(price_index="ppi"), "price_index must be")
        self.check(adj(dollar_year="2024"), "dollar_year must be an integer")
        self.check(adj(dollar_year=1990), "no complete r-cpi-u-rs value")
        self.check(adj(rpp_years=None), "rpp_years is required")
        self.check(adj(rpp_years={"2019": 2019}), "map every release")
        self.check(adj(rpp_years={"2019": 2019, "2024": 1990}), "rpp_years['2024']")
        self.check(adj(rpp_years=None, rpp_year=2024), "give rpp_years")
        self.check(release_manifest(aggregation={"mapping": {"26001": "a"}}), "aggregation")
        one = release_manifest(releases=[2024], adjustments=[{"type": "rpp", "variables": ["mean_inc"],
                                                              "rpp_year": 2024, "rpp_years": {"2024": 2024}}])
        self.check(one, "not both")
        # one release with a dollar year: CPI conversion of that release (price_index required)
        one = release_manifest(releases=[2024], adjustments=[{"type": "rpp", "variables": ["mean_inc"],
                                                              "rpp_year": 2024, "dollar_year": 2023}])
        self.check(one, "price_index is required")

    def test_replicates_errors(self):
        self.assertEqual(validate_manifest(release_manifest(), resolve=False), [])
        self.check(release_manifest(replicates="census_vre"), "must be an object")
        self.check(release_manifest(replicates={"source": "api"}), "replicates.source must be")
        self.check(release_manifest(replicates={"source": "census_vre", "year": 1}), "unknown key")
        self.check(release_manifest(releases=[2012, 2024], variables=[
            {"name": "agg", "ref": "2024:B19025_001"}, {"name": "hh", "ref": "2024:B19001_001"}],
            adjustments=None), "no Census VRE release")
        self.check(release_manifest(geography={"level": "school district (unified)", "state": "26"},
                                    adjustments=None), "replicates.source census_vre: Unsupported geo_level")
        bg = release_manifest(geography={"level": "block group", "state": "26"}, adjustments=None)
        self.assertFalse([e for e in validate_manifest(bg, resolve=False) if "replicates" in e])
        # the overlap rule: periods sharing sample years are refused, with the non-overlapping suggestion
        over = release_manifest(releases=[2021, 2024], adjustments=None)
        errs = validate_manifest(over, resolve=False)
        self.assertTrue(any("overlapping 5-year periods" in e and "[2024]" in e for e in errs), errs)

    def test_overlap_rule_of_trend_tests_still_enforced(self):
        from census.trends import linear_trend_test

        with self.assertRaisesRegex(ValueError, "periods overlap"):
            linear_trend_test([2020, 2022, 2024], [1.0, 2.0, 3.0], ses=[0.1, 0.1, 0.1])

    def test_covariates_only_declaration(self):
        m = covariate_manifest()
        self.assertEqual(validate_manifest(m, resolve=False), [])  # declared nothing: valid, blocks at run
        ok = copy.deepcopy(m)
        ok["analyses"][0]["replicate_scope"] = "covariates_only"
        self.assertEqual(validate_manifest(ok, resolve=False), [])
        bad = copy.deepcopy(ok)
        bad["analyses"][0]["replicate_scope"] = "outcome"
        self.check(bad, "replicate_scope must be")
        bad = copy.deepcopy(ok)
        bad["analyses"][0]["args"] = {"target": "these_areas"}
        self.check(bad, "cannot use target 'these_areas'")
        bad = copy.deepcopy(ok)
        listed = copy.deepcopy(ok)
        listed["analyses"][0]["formula"] = "hh ~ income"  # outcome hh is on the VRE list: allowed, noted
        listed["hypotheses"][0]["term"] = "income"
        self.assertEqual(validate_manifest(listed, resolve=False), [])
        bad = copy.deepcopy(ok)
        bad["analyses"] = [{"id": "m1", "method": "correlation_matrix", "variables": ["income", "hh"],
                            "replicate_scope": "covariates_only"}]
        bad["hypotheses"] = [{"id": "H1", "analysis": "m1", "statistic": "correlation", "variables": ["income", "hh"]}]
        self.check(bad, "fit_replicate only")

    def test_year_and_geoid_allowed_in_formulas(self):
        m = release_manifest(analyses=[{"id": "m1", "method": "fit_replicate",
                                        "formula": "mean_inc ~ C(Year) + C(GEOID)"}])
        self.assertEqual(validate_manifest(m, resolve=False), [])


class TestCreateManifestDefaults(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_defaults_filled_into_completed_file(self):
        m = release_manifest()
        for k in ("dollar_year", "price_index", "rpp_years"):
            del m["adjustments"][0][k]
        path = os.path.join(self.dir, "study.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f)
        res = create_manifest(path, resolve=False)
        self.assertTrue(res["valid"], res["errors"])
        self.assertEqual(res["filled"], [{"adjustment": 0, "dollar_year": 2024},
                                         {"adjustment": 0, "price_index": "r-cpi-u-rs"},
                                         {"adjustment": 0, "rpp_years": {"2019": 2019, "2024": 2024}}])
        self.assertEqual(res["path"], os.path.join(self.dir, "study.completed.json"))
        self.assertEqual(res["hash"], manifest_hash(release_manifest()))  # same as declaring them
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), m)  # input never overwritten
        self.assertEqual(manifest_hash(load_manifest(res["path"])), res["hash"])
        self.assertTrue(any("each release's own year" in n for n in res["notes"]))

    def test_vre_note_lists_variables_without_replicates(self):
        res = create_manifest(covariate_manifest(), resolve=False)
        self.assertTrue(res["valid"], res["errors"])
        note = [n for n in res["notes"] if n.startswith("replicates (census_vre)")]
        self.assertEqual(len(note), 1)
        self.assertIn("income (B19013, 2019)", note[0])
        self.assertIn("covariates_only", note[0])


class TestTwoReleaseRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_m(self, m, vre=fake_vre, **kw):
        with mock.patch("census.replicate.fetch_vre", side_effect=vre):
            return run_manifest(m, os.path.join(self.dir, "run"), client=TwoReleaseClient(), use_cache=False, **kw)

    def test_cpi_then_rpp_and_replicates(self):
        from census import rpp
        from census.inflation import get_cpi

        res = self.run_m(release_manifest())
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        frame = res["frame"].set_index(["GEOID", "Year"])
        self.assertEqual(len(frame), 2 * 2 * N)
        cpi = get_cpi(2024) / get_cpi(2019)
        for (g, y), row in frame.iterrows():
            st, i = g[:2], (int(g[2:]) - 1) // 2
            raw = VALUES[("B19025", "B19025_001")](st, y)[i] / VALUES[("B19001", "B19001_001")](st, y)[i]
            expected = raw * (cpi if y == 2019 else 1.0) * 100.0 / rpp.place_parity(st, y)["rpp"]
            self.assertAlmostEqual(row["mean_inc"] / expected, 1.0, places=6)  # API values are rounded
            self.assertEqual(row["mean_inc_method"], "replicate")
        adj = res["adjustments"][0]
        self.assertEqual((adj["dollar_year"], adj["rpp_years"]), (2024, {"2019": 2019, "2024": 2024}))
        self.assertAlmostEqual(adj["releases"][0]["cpi"]["factor"], cpi)
        self.assertEqual(adj["releases"][1]["cpi"]["factor"], 1.0)
        self.assertEqual(adj["price_index"]["index"], "r-cpi-u-rs")
        self.assertTrue(adj["notes"][0].startswith("PRICE INDEX: release 2019"))
        self.assertEqual(sorted({a["release"] for a in adj["rpp_areas"]}), [2019, 2024])
        # the replicate SE of the contrast, from the fetched replicates with the same CPI and RPP factors
        reps = {}
        for y in RELEASES:
            vals = []
            for st in STATES:
                a, h = (fake_vre(t, y, "county") for t in ("B19025", "B19001"))
                a, h = (f[f["GEOID"].str.startswith(st)] for f in (a, h))
                cols = ["estimate"] + REP_COLS
                r = a[cols].to_numpy(float) / h[cols].to_numpy(float)
                vals.append(r * (cpi if y == 2019 else 1.0) * 100.0 / rpp.place_parity(st, y)["rpp"])
            reps[y] = np.vstack(vals)
        d = (reps[2024] - reps[2019]).mean(axis=0)
        h1 = res["hypotheses"].iloc[0]
        self.assertAlmostEqual(h1["estimate"], d[0], places=4)
        self.assertAlmostEqual(h1["se"], np.sqrt(4 / 80 * ((d[1:] - d[0]) ** 2).sum()), places=6)
        self.assertEqual(h1["se_basis"], "se_sampling")
        info = res["replicates"]
        self.assertEqual(info["source"], "census_vre")
        self.assertEqual(sorted((f["table"], f["release"]) for f in info["files"]),
                         [("B19001", 2019), ("B19001", 2024), ("B19025", 2019), ("B19025", 2024)])
        self.assertEqual(info["files"][0]["url"], "https://example.invalid/2019/050/B19025.csv.zip")
        self.assertEqual(info["unavailable"], [])
        self.assertEqual(info["derived_on_replicates"], ["mean_inc"])
        self.assertEqual(info["scaled_by_adjustments"], ["mean_inc"])
        with open(os.path.join(self.dir, "run", "bundle", "bundle.json"), encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertEqual(prov["replicates"]["files"][1]["sha256"], "sha-B19025-2024")
        self.assertEqual(prov["adjustments"][0]["rpp_years"], {"2019": 2019, "2024": 2024})
        with open(os.path.join(self.dir, "run", "bundle", "methods.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("in this order: (1)", text)
        self.assertIn("variance replicate estimate (VRE) files fetched by the toolkit", text)
        self.assertIn("do not overlap", text)

    def test_one_release_dollar_year(self):
        """One release with a declared dollar year: CPI conversion, then the RPP (Round 14 form otherwise)."""
        from census import rpp
        from census.inflation import get_cpi

        m = release_manifest(releases=[2019], variables=[{"name": "agg", "ref": "2019:B19025_001"},
                                                          {"name": "hh", "ref": "2019:B19001_001"}],
                             adjustments=[{"type": "rpp", "variables": ["mean_inc"], "areas": "state",
                                           "rpp_year": 2019, "dollar_year": 2024, "price_index": "r-cpi-u-rs"}],
                             analyses=[{"id": "m1", "method": "fit_replicate", "formula": "mean_inc ~ hh"}],
                             hypotheses=[{"id": "H1", "analysis": "m1", "statistic": "coefficient", "term": "hh"}])
        res = self.run_m(m)
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        row = res["frame"].set_index("GEOID").loc["36003"]
        raw = VALUES[("B19025", "B19025_001")]("36", 2019)[1] / VALUES[("B19001", "B19001_001")]("36", 2019)[1]
        self.assertAlmostEqual(row["mean_inc"] / (raw * get_cpi(2024) / get_cpi(2019) * 100.0
                                                  / rpp.place_parity("36", 2019)["rpp"]), 1.0, places=6)
        self.assertEqual(res["adjustments"][0]["rpp_year"], 2019)

    def test_supplying_replicates_too_is_an_error(self):
        res = self.run_m(release_manifest(), replicates=fake_vre("B19025", 2019, "county"))
        self.assertFalse(res["success"])
        self.assertTrue(any("do not also supply replicates" in e for e in res["errors"]), res["errors"])

    def test_cli_replicates_path_and_census_vre_is_invalid_input(self):
        from census.workflows_inference import run_manifest as tool

        path = os.path.join(self.dir, "m.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(release_manifest(), f)
        reps = os.path.join(self.dir, "reps.csv")
        fake_vre("B19025", 2019, "county").to_csv(reps, index=False)
        with mock.patch("census.manifest.validate_manifest", return_value=[]):
            out = tool(path, os.path.join(self.dir, "out"), replicates_path=reps)
        self.assertFalse(out["success"])
        self.assertIn("do not also supply replicates", out["error"])

    def test_outcome_without_vre_blocks_unless_declared(self):
        res = self.run_m(covariate_manifest())
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        kinds = [d["kind"] for d in res["deviations"]]
        self.assertIn("replicates_incomplete", kinds)
        missing = res["replicates"]["unavailable"]
        self.assertEqual(sorted((u["variable"], u["release"]) for u in missing), [("income", 2019), ("income", 2024)])
        self.assertIn("not in the", missing[0]["reason"])
        with open(os.path.join(self.dir, "run", "bundle", "methods.md"), encoding="utf-8") as f:
            self.assertIn("No Census VRE replicates are published for income (B19013)", f.read())

        m = covariate_manifest()
        m["analyses"][0]["replicate_scope"] = "covariates_only"
        res = self.run_m(m)
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        m1 = res["results"]["m1"]
        self.assertEqual(m1.diagnostics["replicates_declared_fixed"], ["income"])
        self.assertEqual(m1.provenance["replicate_variables"], ["hh"])
        self.assertTrue(any(n.startswith("DECLARED COVARIATE-ONLY REPLICATES") for n in m1.notes))
        self.assertFalse(m1.diagnostics.get("replicates_incomplete"))
        self.assertEqual(res["replicates"]["declared_covariates_only"], {"m1": ["income"]})
        with open(os.path.join(self.dir, "run", "bundle", "methods.md"), encoding="utf-8") as f:
            self.assertIn("replicate scope `covariates_only`", f.read())

    def test_fixed_variables_refuse_these_areas(self):
        from census.models import fit_replicate

        frame = pd.DataFrame({"GEOID": ["1", "2", "3", "4"], "y": [1.0, 2.0, 3.5, 4.0], "y_se": 0.1,
                              "x": [1.0, 2.0, 3.0, 4.0], "x_se": 0.1})
        reps = pd.DataFrame({"GEOID": frame["GEOID"], "variable": "x", "estimate": frame["x"]})
        reps = pd.concat([reps, pd.DataFrame(np.outer(frame["x"], np.ones(80)) * 1.01, columns=REP_COLS)], axis=1)
        with self.assertRaisesRegex(ValueError, "these_areas"):
            fit_replicate("y ~ x", frame, reps, target="these_areas", fixed_variables=["y"])
        res = fit_replicate("y ~ x", frame, reps, replicate_policy="strict", fixed_variables=["y"])
        self.assertEqual(res.diagnostics["replicates_declared_fixed"], ["y"])

    def test_fetch_failure_blocks(self):
        from census.replicate import VREDownloadError

        res = self.run_m(release_manifest(), vre=mock.Mock(side_effect=VREDownloadError("HTTP 503")))
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        kinds = [d["kind"] for d in res["deviations"]]
        self.assertIn("replicates_unavailable", kinds)
        self.assertIn("hypothesis_not_tested", kinds)
        self.assertIn("HTTP 503", res["replicates"]["error"])

    def test_caller_supplied_replicates_without_year_fail_multi_release_adjustment(self):
        m = release_manifest(replicates=None)
        del m["replicates"]
        reps = fake_vre("B19025", 2019, "county")
        reps["variable"] = "agg"
        res = self.run_m(m, replicates=reps)
        self.assertIn("adjustment_failed", [d["kind"] for d in res["deviations"]])


# ---------------------------------------------------------------------------
# frozen real-data route (examples/manifests/mi_ny_income_change.json)
# ---------------------------------------------------------------------------

def run_cli(argv):
    from census.__main__ import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = main(argv)
    return code, json.loads(buf.getvalue())


def vre_fixture_files():
    out = []
    for dirpath, _, files in os.walk(VRE_FIXTURES):
        out += [os.path.join(dirpath, f) for f in files if f.endswith(".zip")]
    return sorted(out)


class TestFrozenChangeRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_reference_workflow import offline

        cls.dir = tempfile.mkdtemp(prefix="mi_ny_change_")
        vre_dir = os.path.join(cls.dir, "vre")
        shutil.copytree(VRE_FIXTURES, vre_dir)
        cls.env = mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": os.path.join(cls.dir, "cache.db"),
                                               "CENSUS_VRE_DIR": vre_dir})
        cls.env.start()
        with offline(), patched_membership():
            code, cls.imported = run_cli(["cache", "import", FIXTURES, "--json"])
            assert code == 0, cls.imported
            code, cls.created = run_cli(["tool", "create_manifest", "--arg", f"manifest_path={MANIFEST}", "--json"])
            assert code == 0, cls.created
            code, cls.cli = run_cli(["tool", "run_manifest", "--arg", f"manifest_path={MANIFEST}",
                                     "--arg", f"output_dir={os.path.join(cls.dir, 'cli')}", "--json"])
            cls.cli_code = code

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_inputs(self):
        self.assertEqual(self.imported["imported"], 12)  # 2 tables x (2 states + nation) x 2 releases
        self.assertFalse(self.imported["synthetic"])
        self.assertEqual(self.created["hash"], MANIFEST_HASH)
        self.assertEqual(self.created["notes"], [])
        self.assertEqual(len(vre_fixture_files()), 4)

    def test_confirmatory_result(self):
        out = self.cli
        self.assertEqual(self.cli_code, 0, out.get("error"))
        self.assertEqual(out["analysis_status"], "confirmatory")
        self.assertEqual(out["deviations"], [])
        self.assertEqual(out["manifest_hash"], MANIFEST_HASH)
        self.assertFalse(out["data_source"]["synthetic"])
        h1 = out["hypotheses"][0]
        self.assertEqual((h1["id"], h1["status"], h1["se_basis"]), ("H1", "tested", "se_sampling"))
        for k, v in LIVE_RESULT.items():
            self.assertAlmostEqual(h1[k], v, places=2)
        self.assertTrue(h1["significant"])

    def test_vre_provenance(self):
        rep = self.cli["replicates"]
        self.assertEqual(rep["source"], "census_vre")
        self.assertEqual(rep["unavailable"], [])
        self.assertEqual(rep["variables"], {"agg_income": [2019, 2024], "households": [2019, 2024]})
        sidecars = {}
        for path in vre_fixture_files():
            with open(path + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            sidecars[meta["source_url"]] = meta["sha256"]
        self.assertEqual({f["url"]: f["sha256"] for f in rep["files"]}, sidecars)
        self.assertTrue(all(u.startswith("https://www2.census.gov/programs-surveys/acs/replicate_estimates/")
                            for u in sidecars))
        self.assertTrue(all(f["moe_check"]["passed"] for f in rep["files"]))
        with open(os.path.join(self.dir, "cli", "bundle", "results", "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["n_units"], 290)
        self.assertEqual(run["replicates"]["derived_on_replicates_rows"],
                         {"mean_hh_income": {"rows_replicate": 290, "rows": 290}})
        adj = run["adjustments"][0]
        self.assertEqual(adj["rpp_years"], {"2019": 2019, "2024": 2024})
        self.assertAlmostEqual(adj["releases"][0]["cpi"]["factor"], 462.511 / 375.813)
        with open(os.path.join(self.dir, "cli", "bundle", "bundle.json"), encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertEqual(prov["replicates"]["files"], run["replicates"]["files"])
        self.assertEqual(prov["adjustments"][0]["price_index"]["index"], "r-cpi-u-rs")

    def test_mcp_route_agrees(self):
        from tests.test_reference_workflow import _mcp_call, offline

        import mcp_server

        with offline(), patched_membership():
            out = _mcp_call(mcp_server, "run_manifest", {"manifest_path": MANIFEST,
                                                         "output_dir": os.path.join(self.dir, "mcp")})
        self.assertEqual(out["analysis_status"], "confirmatory", out.get("error"))
        a, b = out["hypotheses"][0], self.cli["hypotheses"][0]
        for k in ("estimate", "se", "ci_lower", "ci_upper", "p_value"):
            self.assertAlmostEqual(a[k], b[k], places=9)


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census API tests")
class TestLiveChangeRoute(unittest.TestCase):
    """The same route on the live Census API, VRE files and boundary files (default caches)."""

    def test_live_confirmatory_run(self):
        d = tempfile.mkdtemp(prefix="mi_ny_change_live_")
        self.addCleanup(shutil.rmtree, d, True)
        code, out = run_cli(["tool", "run_manifest", "--arg", f"manifest_path={MANIFEST}",
                             "--arg", f"output_dir={d}", "--json"])
        self.assertEqual(code, 0, out.get("error"))
        self.assertEqual(out["analysis_status"], "confirmatory")
        self.assertEqual(out["data_source"]["synthetic"], False)
        h1 = out["hypotheses"][0]
        for k, v in LIVE_RESULT.items():
            self.assertAlmostEqual(h1[k], v, places=2)
        self.assertEqual(len(out["replicates"]["files"]), 4)


# ---------------------------------------------------------------------------
# recording (live)
# ---------------------------------------------------------------------------

def _trim_vre(raw, geoid_prefixes, orders):
    """CSV bytes of a VRE download with the header, the table-title rows and only the rows of
    the given GEOID prefixes (after the summary-level prefix) and line ORDER values."""
    from census.replicate import _decompress, normalize_vre_geoid

    text = _decompress(raw).decode("latin-1")
    lines = text.splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        row = next(csv.reader([line]))
        geoid = row[1].strip()
        if not geoid:
            out.append(line)
        elif normalize_vre_geoid(geoid)[:2] in geoid_prefixes and row[3].strip().isdigit() \
                and int(row[3]) in orders:
            out.append(line)
    return "".join(out).encode("latin-1")


def record():
    """Fetch the eight API responses (2019 and 2024 acs5, all counties of 26 and 36; B19025 and
    B19001) through the toolkit client, trim each to the manifest's variables, and write trimmed
    copies of the four Census VRE county files (Michigan and New York rows of line 001) with
    sidecars. Never writes or prints the API key."""
    from census import replicate as vre
    from census.manifest import tables_and_refs

    m = load_manifest(MANIFEST)
    keep = {t: [r.split(":")[1] for r in refs] for t, refs in tables_and_refs(m).items()}
    client = CensusAPIClient()
    os.makedirs(FIXTURES, exist_ok=True)
    for year in RELEASES:
        for st in STATES:
            request = {"year": year, "dataset": "acs5", "geo_level": "county", "state_code": st,
                       "county_code": "*"}
            doc = {"_about": (f"REAL Census Data API responses ({year} acs5, all counties of state {st}, "
                              "get=NAME,group(<table>)), recorded by `python -m tests.test_manifest_releases "
                              "--record` for examples/manifests/mi_ny_income_change.json; columns trimmed to the "
                              "manifest's variables (estimate, MOE and annotations) plus GEO_ID, NAME, state, county."),
                   "request": request, "requests": {}}
            for table, codes in keep.items():
                rows = client.fetch(table_code=table, **request)
                if rows is None:
                    raise SystemExit(f"fetch failed for {table} {year} state {st}")
                header = rows[0]
                cols = [i for i, h in enumerate(header)
                        if h in ("GEO_ID", "NAME", "state", "county")
                        or any(h == f"{c}{s}" for c in codes for s in ("E", "EA", "M", "MA"))]
                doc["requests"][table] = [[r[i] for i in cols] for r in rows]
            with open(os.path.join(FIXTURES, f"frozen_api_responses_{year}_{st}.json"), "w", encoding="utf-8",
                      newline="\n") as f:
                json.dump(doc, f, separators=(",", ":"))
                f.write("\n")
    for year in RELEASES:  # national rows of each table: the cross-release continuity check reads them
        request = {"year": year, "dataset": "acs5", "geo_level": "us", "state_code": None}
        doc = {"_about": (f"REAL Census Data API responses ({year} acs5, nation, get=NAME,group(<table>)), "
                          "untrimmed, read by the continuity check of a multi-release extraction; recorded by "
                          "`python -m tests.test_manifest_releases --record`."),
               "request": request, "requests": {}}
        for table in keep:
            rows = client.fetch(table_code=table, **request)
            if rows is None:
                raise SystemExit(f"fetch failed for {table} {year} nation")
            doc["requests"][table] = rows
        with open(os.path.join(FIXTURES, f"frozen_api_responses_{year}_us.json"), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.write("\n")
    for table, codes in keep.items():
        orders = {int(c.split("_")[1]) for c in codes}
        for year in RELEASES:
            frame = vre.fetch_vre(table, year, "county")  # downloads into the VRE cache if needed
            with open(frame.attrs["cache_path"], "rb") as f:
                raw = f.read()
            data = _trim_vre(raw, STATES, orders)
            out_dir = os.path.join(VRE_FIXTURES, str(year), frame.attrs["summary_level"])
            os.makedirs(out_dir, exist_ok=True)
            name = os.path.basename(frame.attrs["cache_path"])
            member = name[: -len(".zip")] if name.endswith(".zip") else name
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                info = zipfile.ZipInfo(member, date_time=(2026, 11, 8, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
            trimmed = buf.getvalue()
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(trimmed)
            import hashlib

            meta = {"source_url": frame.attrs["source_url"], "sha256": hashlib.sha256(trimmed).hexdigest(),
                    "size_bytes": len(trimmed), "content_type": "application/zip",
                    "downloaded_at": "2026-11-08T00:00:00Z",
                    "_about": (f"TRIMMED copy of the Census VRE file at source_url (original SHA-256 "
                               f"{frame.attrs['sha256']}): header, table-title rows, and the rows of the counties "
                               f"of states {', '.join(STATES)} for line(s) {sorted(orders)}; recorded by "
                               "`python -m tests.test_manifest_releases --record`.")}
            with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump(meta, f, indent=2)
                f.write("\n")
    print(f"recorded fixtures into {FIXTURES}")


if __name__ == "__main__":
    if "--record" in sys.argv:
        record()
    else:
        unittest.main()
